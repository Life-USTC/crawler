"""Execute the workflow's shell steps, including its delayed failure gate."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

WORKFLOW = yaml.safe_load(
    (Path(__file__).parents[1] / ".github/workflows/incremental-sync.yml").read_text()
)
STEPS = WORKFLOW["jobs"]["sync"]["steps"]


class WorkflowRun:
    def __init__(
        self, root: Path, *, codes=None, cancelled=False, checkpoint="0|0|0", integrity="ok"
    ):
        self.root = root
        self.cancelled = False
        self.cancel_at_save = cancelled
        self.failed = False
        self.steps = {}
        self.trace = []
        self.codes = codes or {}
        self.env = {
            **os.environ,
            "DB_PATH": str(root / "crawler.sqlite"),
            "DB_ARCHIVE": str(root / "state.zst"),
            "SYNC_BUDGET_MINUTES": "1",
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
            "FAKE_CODES": json.dumps(self.codes),
            "FAKE_TIMEOUT_LOG": str(root / "timeouts.jsonl"),
            "USTC_CRAWLER_SERVER": "https://example.test",
            "USTC_CRAWLER_INGESTION_SECRET": "test-secret",
            "FAKE_CHECKPOINT": checkpoint,
            "FAKE_INTEGRITY": integrity,
        }
        self.inputs = SimpleNamespace(
            reindex=False,
            retext=False,
            requeue_failed=False,
            rebuild_markdown=True,
            rebuild_after_url="",
            rebuild_limit="0",
        )
        connection = sqlite3.connect(self.env["DB_PATH"])
        connection.executescript("""
            CREATE TABLE sync_batches (id, status, last_error, attempts, created_at);
            CREATE TABLE sync_outbox (event_id, entity_key, status, last_error, updated_at, batch_id);
        """)
        connection.close()
        commands = root / "bin"
        commands.mkdir()
        self.env["PATH"] = str(commands) + os.pathsep + self.env["PATH"]
        fake = f"""#!{sys.executable}
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'uv':
    if args[1] == 'python':
        os.execv(sys.executable, [sys.executable, *args[2:]])
    sys.exit(json.loads(os.environ['FAKE_CODES']).get(args[2], 0))
if name == 'timeout':
    with open(os.environ['FAKE_TIMEOUT_LOG'], 'a') as log:
        log.write(json.dumps(args) + '\\n')
    os.execvp(args[3], args[3:])
if name == 'sqlite3':
    query = args[-1]
    if 'checkpoint' in query:
        print(os.environ['FAKE_CHECKPOINT'])
    elif 'integrity' in query:
        print(os.environ['FAKE_INTEGRITY'])
if name == 'zstd':
    shutil.copyfile(args[1], args[3])
"""
        for name in ("uv", "timeout", "sqlite3", "zstd"):
            path = commands / name
            path.write_text(fake)
            path.chmod(0o755)

    def step(self, step_id):
        return self.steps.get(step_id, SimpleNamespace(outcome="skipped", outputs={}))

    def expression(self, expression):
        expression = expression.removeprefix("${{").removesuffix("}}").strip()
        expression = re.sub(
            r"steps\.(\w+)\.outputs\.([\w-]+)",
            lambda match: repr(self.step(match[1]).outputs.get(match[2], "")),
            expression,
        )
        expression = re.sub(
            r"steps\.(\w+)\.outcome", lambda match: repr(self.step(match[1]).outcome), expression
        )
        expression = expression.replace("always()", "True")
        expression = expression.replace("failure()", repr(self.failed))
        expression = expression.replace("cancelled()", repr(self.cancelled))
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = expression.replace("!", " not ")
        return eval(expression, {"__builtins__": {}}, {"inputs": self.inputs})

    def run(self):
        start = next(
            i for i, step in enumerate(STEPS) if step.get("name") == "Upgrade database schema"
        )
        validation = next(step for step in STEPS if step.get("id") == "validate")
        for step in [validation, *STEPS[start:]]:
            if step.get("name") == "Save crawler state" and self.cancel_at_save:
                self.cancelled = True
            condition = step.get("if", "True")
            explicit_status = any(
                token in condition for token in ("always()", "failure()", "cancelled()")
            )
            if not explicit_status and (self.failed or self.cancelled):
                continue
            if not self.expression(condition):
                continue
            name = step["name"]
            self.trace.append(name)
            if "uses" in step:
                if step["uses"].startswith("actions/cache/save"):
                    (self.root / "saved").write_bytes(Path(self.env["DB_ARCHIVE"]).read_bytes())
                continue
            env = dict(self.env)
            for key, value in step.get("env", {}).items():
                if key not in ("USTC_CRAWLER_SERVER", "USTC_CRAWLER_INGESTION_SECRET"):
                    resolved = self.expression(value)
                    env[key] = (
                        str(resolved).lower() if isinstance(resolved, bool) else str(resolved)
                    )
            output = self.root / "output"
            output.write_text("")
            env["GITHUB_OUTPUT"] = str(output)
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
            )
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            outcome = "failure" if result.returncode else "success"
            self.steps[step.get("id", name)] = SimpleNamespace(outcome=outcome, outputs=outputs)
            if result.returncode and not step.get("continue-on-error"):
                self.failed = True
        return self


@pytest.mark.parametrize(
    "code, saved, failed",
    [
        (0, True, False),
        (2, True, True),
        (124, True, True),
        (1, False, True),
        (137, False, True),
        (130, False, True),
    ],
)
def test_sync_status_saves_safe_progress_before_failure(tmp_path, code, saved, failed):
    run = WorkflowRun(tmp_path, codes={"sync": code}).run()
    assert (tmp_path / "saved").exists() is saved
    assert run.failed is failed
    assert run.step("sync").outputs["exit_code"] == str(code)
    if saved:
        assert run.trace.index("Upload state to cache") < run.trace.index(
            "Require complete synchronization"
        )


@pytest.mark.parametrize("command", ["db-upgrade", "rebuild-markdown", "crawl"])
def test_unexpected_writer_failure_keeps_previous_cache(tmp_path, command):
    run = WorkflowRun(tmp_path, codes={command: 1}).run()
    assert run.failed
    assert not (tmp_path / "saved").exists()
    assert "Save crawler state" not in run.trace


def test_cancellation_never_saves_state(tmp_path):
    run = WorkflowRun(tmp_path, cancelled=True).run()
    assert not (tmp_path / "saved").exists()
    assert "Save crawler state" not in run.trace
    assert "Upload state to cache" not in run.trace
    assert run.step("sync").outputs["exit_code"] == "0"


@pytest.mark.parametrize("checkpoint, integrity", [("1|20|4", "ok"), ("0|0|0", "malformed")])
def test_busy_or_corrupt_database_is_not_cached(tmp_path, checkpoint, integrity):
    run = WorkflowRun(tmp_path, checkpoint=checkpoint, integrity=integrity).run()
    assert run.failed
    assert not (tmp_path / "saved").exists()
    assert run.step("compress").outcome == "failure"


@pytest.mark.parametrize("code", [2, 124])
def test_controlled_rebuild_saves_progress_and_fails_final_gate(tmp_path, code):
    run = WorkflowRun(tmp_path, codes={"rebuild-markdown": code}).run()
    assert run.failed
    assert (tmp_path / "saved").exists()
    assert run.step("rebuild").outputs["exit_code"] == str(code)


def test_pending_and_failed_events_are_reported(tmp_path):
    run = WorkflowRun(tmp_path)
    connection = sqlite3.connect(run.env["DB_PATH"])
    connection.executemany(
        "INSERT INTO sync_outbox VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("new", "article-1", "pending", None, "2026-10-08", None),
            ("invalid", "article-2", "failed", "invalid_event", "2026-10-08", None),
        ],
    )
    connection.commit()
    connection.close()
    run.run()
    assert run.failed  # A zero CLI exit cannot hide a remaining queue.
    assert (tmp_path / "saved").exists()
    assert run.step("diagnostics").outputs == {"pending": "1", "failed": "1", "unfinished": "1"}
    failures = json.loads((tmp_path / "diag/failed-events.json").read_text())
    assert failures == [
        {"event_id": "invalid", "entity_key": "article-2", "error": "invalid_event"}
    ]
    assert '"pending": 1' in (tmp_path / "summary").read_text()


def test_historical_failed_events_and_crawl_item_errors_do_not_mask_success(tmp_path):
    run = WorkflowRun(tmp_path, codes={"crawl": 2})
    connection = sqlite3.connect(run.env["DB_PATH"])
    connection.execute(
        "INSERT INTO sync_outbox VALUES (?, ?, ?, ?, ?, ?)",
        ("old", "article", "failed", "server_rejected", "2026-09-01", "batch"),
    )
    connection.commit()
    connection.close()
    run.run()
    assert not run.failed
    assert (tmp_path / "saved").exists()
    assert run.step("diagnostics").outputs["failed"] == "1"


def test_tee_failure_prevents_cache_write(tmp_path):
    run = WorkflowRun(tmp_path)
    tee = tmp_path / "bin" / "tee"
    tee.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    tee.chmod(0o755)
    run.run()
    assert run.failed
    assert not (tmp_path / "saved").exists()


@pytest.mark.parametrize("command", ["reindex", "retext", "sync-requeue-failed"])
def test_maintenance_partial_completion_is_saved_but_not_green(tmp_path, command):
    run = WorkflowRun(tmp_path, codes={command: 2})
    run.inputs.rebuild_markdown = False
    setattr(run.inputs, "requeue_failed" if command == "sync-requeue-failed" else command, True)
    run.run()
    assert run.failed
    assert (tmp_path / "saved").exists()


def test_backfill_crash_after_completed_reindex_prevents_save(tmp_path):
    run = WorkflowRun(tmp_path, codes={"sync-backfill": 1})
    run.inputs.reindex = True
    run.inputs.rebuild_markdown = False
    run.run()
    assert run.failed
    assert not (tmp_path / "saved").exists()


@pytest.mark.parametrize(
    "repairs",
    [("reindex", "retext"), ("reindex", "rebuild_markdown"), ("retext", "rebuild_markdown")],
)
def test_rejects_combined_heavy_repairs_before_database_writes(tmp_path, repairs):
    run = WorkflowRun(tmp_path)
    run.inputs.rebuild_markdown = False
    for repair in repairs:
        setattr(run.inputs, repair, True)
    run.run()
    assert run.step("validate").outcome == "failure"
    assert "Upgrade database schema" not in run.trace
    assert not (tmp_path / "saved").exists()


@pytest.mark.parametrize("command", ["reindex", "retext", "sync-backfill", "sync-requeue-failed"])
@pytest.mark.parametrize("code", [124, 137])
def test_uncontrolled_maintenance_timeout_does_not_save(tmp_path, command, code):
    run = WorkflowRun(tmp_path, codes={command: code})
    run.inputs.rebuild_markdown = False
    setattr(
        run.inputs,
        {"sync-backfill": "reindex", "sync-requeue-failed": "requeue_failed"}.get(command, command),
        True,
    )
    run.run()
    assert run.failed
    assert not (tmp_path / "saved").exists()


@pytest.mark.parametrize("repair", [None, "reindex", "retext", "rebuild_markdown"])
@pytest.mark.parametrize("requeue", [False, True])
def test_writer_deadlines_leave_room_for_large_state_save(tmp_path, repair, requeue):
    run = WorkflowRun(tmp_path)
    run.inputs.rebuild_markdown = False
    run.inputs.requeue_failed = requeue
    if repair:
        setattr(run.inputs, repair, True)
    run.run()
    assert not run.failed
    timeouts = [json.loads(line) for line in (tmp_path / "timeouts.jsonl").read_text().splitlines()]
    writer_minutes = sum(int(args[2].removesuffix("m")) for args in timeouts)
    shutdown_minutes = len(timeouts)  # Each timeout reserves a 60-second kill-after grace.
    job_minutes = run.expression(WORKFLOW["jobs"]["sync"]["timeout-minutes"])
    # Sept 30's large-state save took about 10 minutes; reserve that plus 2
    # minutes for restore and 5 for setup, diagnostics and scheduler overhead.
    assert writer_minutes + shutdown_minutes + 10 + 2 + 5 <= job_minutes
    assert (tmp_path / "saved").exists()
