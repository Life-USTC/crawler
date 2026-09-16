"""Single-instance lock tests for the sync CLI command."""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler import cli
from ustc_crawler.cli import _sync_instance_lock, main


class SyncInstanceLockTests(unittest.TestCase):
    server = "https://ingest.example.test"
    ingestion_secret = "machine-ingestion-secret"

    def test_second_instance_is_not_acquired(self) -> None:
        with TemporaryDirectory() as temp:
            with _sync_instance_lock(temp) as first:
                self.assertTrue(first)
                with _sync_instance_lock(temp) as second:
                    self.assertFalse(second)

    def test_lock_is_released_after_context_exit(self) -> None:
        with TemporaryDirectory() as temp:
            with _sync_instance_lock(temp) as first:
                self.assertTrue(first)
            with _sync_instance_lock(temp) as second:
                self.assertTrue(second)

    def test_cli_sync_skips_with_rc0_when_lock_held(self) -> None:
        class FakeClient:
            def __init__(self, *_args, **_kwargs) -> None:
                raise AssertionError("a locked sync run must not construct a client")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            holder = (data_dir / "sync.lock").open("a")
            try:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                argv = [
                    "sync",
                    "--server",
                    self.server,
                    "--db",
                    str(root / "crawler.sqlite"),
                    "--data-dir",
                    str(data_dir),
                ]
                env = {"USTC_CRAWLER_INGESTION_SECRET": self.ingestion_secret}
                captured = io.StringIO()
                with (
                    unittest.mock.patch.dict(os.environ, env),
                    unittest.mock.patch.object(cli, "IngestionSyncClient", FakeClient),
                    contextlib.redirect_stderr(captured),
                ):
                    self.assertEqual(main(argv), 0)
                self.assertIn("sync already running", captured.getvalue())
                self.assertIn("sync.lock", captured.getvalue())
            finally:
                holder.close()

    def test_cli_sync_runs_when_lock_is_free(self) -> None:
        class FakeClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def sync(self, **_kwargs) -> dict:
                return {"failed": 0}

            def close(self) -> None:
                pass

        with TemporaryDirectory() as temp:
            root = Path(temp)
            argv = [
                "sync",
                "--server",
                self.server,
                "--db",
                str(root / "crawler.sqlite"),
                "--data-dir",
                str(root / "data"),
            ]
            env = {"USTC_CRAWLER_INGESTION_SECRET": self.ingestion_secret}
            with (
                unittest.mock.patch.dict(os.environ, env),
                unittest.mock.patch.object(cli, "IngestionSyncClient", FakeClient),
            ):
                self.assertEqual(main(argv), 0)


if __name__ == "__main__":
    unittest.main()
