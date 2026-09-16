"""Regression tests for sync pipeline hardening (Wave 1, stream S1)."""

from __future__ import annotations

import hashlib
import json
import os
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import event, select

from ustc_crawler import cli
from ustc_crawler.cli import _sigterm_as_keyboard_interrupt, build_parser, main
from ustc_crawler.db.models import SyncBatch, SyncBatchItem, SyncOutbox, SyncRun
from ustc_crawler.models import ArticleDocument, SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.sync.client import (
    DeliveryResult,
    ImmutableObjectChangedError,
    IngestionSyncClient,
    SyncOptions,
)
from ustc_crawler.sync.models import LocalObjectManifest
from ustc_crawler.sync.outbox import MAX_ERROR_DETAIL_CHARS, IngestionOutbox


class SyncHardeningTestCase(unittest.TestCase):
    server = "https://ingest.example.test"
    ingestion_secret = "machine-ingestion-secret"

    @staticmethod
    def _source() -> SourceConfig:
        return SourceConfig(
            id="source",
            name="Test source",
            organization_level="department",
            seed_urls=["https://example.edu/"],
            allowed_hosts=["example.edu"],
        )

    @staticmethod
    def _article(number: int, *, body_size: int = 0) -> ArticleDocument:
        url = f"https://example.edu/news/{number}"
        text = f"Text {number} " + ("x" * body_size if body_size else "")
        return ArticleDocument(
            url=url,
            source_id="source",
            title=f"Notice {number}",
            author="",
            published_at="2026-08-20",
            updated_at="2026-08-20T09:30:00",
            category="通知公告",
            summary="Summary",
            body_html=f"<p>HTML {number}</p>",
            body_text=text,
            body_markdown=f"Markdown {number}",
            extraction_method="article",
            source_page_url=url,
        )

    def _store(self, root: Path) -> Store:
        store = Store(root / "crawler.sqlite", root / "data")
        store.add_source(self._source())
        return store

    def _insert_batch(
        self,
        store: Store,
        batch_id: str,
        *,
        count: int,
        batch_status: str,
        item_status: str,
        outbox_status: str,
        last_error: str | None = None,
        attempts: int = 0,
    ) -> None:
        """Persist a batch row with ``count`` linked outbox events and items."""

        source = store.source_descriptor("source")
        for number in range(count):
            store.save_article_and_enqueue_for_sync(self._article(number))
        with store.database.session_factory.begin() as session:
            rows = session.scalars(
                select(SyncOutbox).order_by(SyncOutbox.created_at, SyncOutbox.event_id)
            ).all()
            session.add(
                SyncBatch(
                    id=batch_id,
                    run_id=None,
                    client_run_id="old-run",
                    sources_json=json.dumps(
                        [source.model_dump(by_alias=True, mode="json")],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    observed_at="2026-08-20T00:00:00+08:00",
                    payload_sha256="a" * 64,
                    protocol_version="1",
                    producer_version="old-producer",
                    status=batch_status,
                    attempts=attempts,
                    last_error=last_error,
                    created_at="2026-08-20T00:00:00+08:00",
                    updated_at="2026-08-20T00:00:00+08:00",
                )
            )
            for row in rows:
                publication = json.loads(row.payload_json)
                row.batch_id = batch_id
                row.status = outbox_status
                session.add(
                    SyncBatchItem(
                        batch_id=batch_id,
                        item_key=row.event_id,
                        source_id=publication["sourceId"],
                        canonical_url=publication["canonicalUrl"],
                        revision_hash=publication["revisionHash"],
                        status=item_status,
                    )
                )


class PoisonPillTests(SyncHardeningTestCase):
    def test_oversized_event_is_failed_terminally_and_skipped(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                # The poison event sorts first (oldest created_at).
                store.enqueue_article_for_sync(self._article(1, body_size=64 * 1024))
                store.enqueue_article_for_sync(self._article(2))
                outbox = IngestionOutbox(store.database)

                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                    max_payload_bytes=16 * 1024,
                )

                self.assertIsNotNone(batch)
                self.assertEqual(len(batch.items), 1)
                self.assertEqual(batch.items[0].canonical_url, "https://example.edu/news/2")
                with store.database.session_factory() as session:
                    rows = {
                        row.entity_key: row
                        for row in session.scalars(select(SyncOutbox)).all()
                    }
                    poison = rows["source:https://example.edu/news/1"]
                    self.assertEqual(poison.status, "failed")
                    self.assertEqual(poison.last_error, "event_too_large")
                    self.assertIsNone(poison.batch_id)
                    healthy = rows["source:https://example.edu/news/2"]
                    self.assertEqual(healthy.status, "batched")
                    self.assertEqual(healthy.batch_id, "batch")

                # The poison event must not wedge later runs: nothing pending.
                self.assertIsNone(
                    outbox.build_batch(
                        run_id="run",
                        batch_id="batch-2",
                        producer_version="test",
                        observed_at="2026-08-20",
                        max_payload_bytes=16 * 1024,
                    )
                )
            finally:
                store.close()

    def test_all_oversized_events_fail_without_blocking_the_queue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1, body_size=64 * 1024))
                store.enqueue_article_for_sync(self._article(2, body_size=64 * 1024))
                outbox = IngestionOutbox(store.database)

                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                    max_payload_bytes=16 * 1024,
                )

                self.assertIsNone(batch)
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"failed"})
                    self.assertEqual({row.last_error for row in rows}, {"event_too_large"})
            finally:
                store.close()


class ReplayIsolationTests(SyncHardeningTestCase):
    def test_unrebuildable_replay_batch_is_failed_and_does_not_wedge(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"no server request expected: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                # Legacy batch above the 100-item protocol limit: replaying it
                # raises a pydantic ValidationError during rebuild.
                self._insert_batch(
                    store,
                    "legacy-oversized",
                    count=101,
                    batch_status="pending",
                    item_status="pending",
                    outbox_status="batched",
                )
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                try:
                    summary = sync.sync()
                finally:
                    sync.close()

                self.assertEqual(summary["failed"], 1)
                self.assertEqual(summary["status"], "completed")
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "legacy-oversized")
                    self.assertEqual(batch.status, "failed")
                    self.assertEqual(batch.last_error, "batch_rebuild_error")

                # The failed batch is terminal: the next run neither replays
                # it nor raises.
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                try:
                    resumed = sync.sync()
                finally:
                    sync.close()
                self.assertEqual(resumed["batches"], 0)
                self.assertEqual(resumed["status"], "completed")
            finally:
                store.close()


class RecoverOversizedCliTests(SyncHardeningTestCase):
    def test_cli_exposes_sync_recover_oversized(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["sync-recover-oversized", "--db", "db.sqlite", "batch-1"],
        )
        self.assertEqual(args.command, "sync-recover-oversized")
        self.assertEqual(args.batch_id, "batch-1")

    def test_cli_sync_recover_oversized_releases_batch_events(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                self._insert_batch(
                    store,
                    "oversized",
                    count=101,
                    batch_status="failed",
                    item_status="failed",
                    outbox_status="failed",
                )
            finally:
                store.close()

            exit_code = main(
                [
                    "sync-recover-oversized",
                    "--db",
                    str(root / "crawler.sqlite"),
                    "--data-dir",
                    str(root / "data"),
                    "oversized",
                ]
            )

            self.assertEqual(exit_code, 0)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "oversized")
                    self.assertEqual(batch.status, "superseded")
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"pending"})
            finally:
                store.close()


class ArchivedObjectBytesTests(SyncHardeningTestCase):
    def test_archive_lookup_caches_only_hits_and_reuses_them(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(3):
                    article = self._article(number)
                    store.save_article(article)
                target = self._article(1)
                store.enqueue_article_for_sync(target)
                with store.database.session_factory() as session:
                    row = session.scalar(select(SyncOutbox))
                    manifests = json.loads(row.object_manifest_json)
                manifest = next(
                    LocalObjectManifest.model_validate(value)
                    for value in manifests
                    if value["kind"] == "body_html"
                )
                Path(manifest.local_path).unlink()
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )
                try:
                    body = sync._archived_object_bytes(manifest)
                    self.assertEqual(
                        hashlib.sha256(body).hexdigest(),
                        manifest.sha256,
                    )
                    # The cache holds only objects that actually matched,
                    # never a whole-table sha->bytes snapshot.
                    self.assertEqual(len(sync._archive_object_cache["body_html"]), 1)

                    statements: list[str] = []

                    def count_selects(
                        _connection, _cursor, statement, _parameters, _context, _executemany
                    ):
                        if statement.lstrip().upper().startswith("SELECT"):
                            statements.append(statement)

                    event.listen(store.database.engine, "before_cursor_execute", count_selects)
                    try:
                        again = sync._archived_object_bytes(manifest)
                    finally:
                        event.remove(store.database.engine, "before_cursor_execute", count_selects)
                    self.assertEqual(again, body)
                    self.assertEqual(statements, [])
                finally:
                    sync.close()
            finally:
                store.close()

    def test_archive_lookup_rebuilds_only_size_plausible_rows(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                article = self._article(1)
                store.save_article(article)
                store.enqueue_article_for_sync(article)
                with store.database.session_factory() as session:
                    row = session.scalar(select(SyncOutbox))
                    manifests = json.loads(row.object_manifest_json)
                manifest = next(
                    LocalObjectManifest.model_validate(value)
                    for value in manifests
                    if value["kind"] == "body_html"
                )
                Path(manifest.local_path).unlink()
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )
                try:
                    wrong_size = manifest.model_copy(update={"size": manifest.size + 10})
                    with self.assertRaises(ImmutableObjectChangedError):
                        sync._archived_object_bytes(wrong_size)
                    wrong_digest = manifest.model_copy(update={"sha256": "0" * 64})
                    with self.assertRaises(ImmutableObjectChangedError):
                        sync._archived_object_bytes(wrong_digest)
                finally:
                    sync.close()
            finally:
                store.close()


class AttemptsAndRequeueTests(SyncHardeningTestCase):
    def test_mark_batch_error_increments_attempts(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                outbox = IngestionOutbox(store.database)
                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                )
                self.assertIsNotNone(batch)

                outbox.mark_batch_error("batch", "network_error")
                outbox.mark_batch_error("batch", "network_error")

                with store.database.session_factory() as session:
                    persisted = session.get(SyncBatch, "batch")
                    self.assertEqual(persisted.attempts, 2)
                    self.assertEqual(persisted.last_error, "network_error")
            finally:
                store.close()

    def test_requeue_failed_batches_honors_attempt_threshold_unless_forced(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                self._insert_batch(
                    store,
                    "exhausted",
                    count=3,
                    batch_status="failed",
                    item_status="failed",
                    outbox_status="failed",
                    last_error="immutable_object_changed",
                    attempts=5,
                )
                outbox = IngestionOutbox(store.database)

                result = outbox.requeue_failed_batches(errors={"immutable_object_changed"})
                self.assertEqual(result, {"batches": 0, "events": 0, "skipped": 0})
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "exhausted")
                    self.assertEqual(batch.status, "failed")
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"failed"})

                forced = outbox.requeue_failed_batches(
                    errors={"immutable_object_changed"},
                    force=True,
                )
                self.assertEqual(forced, {"batches": 1, "events": 3, "skipped": 0})
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "exhausted")
                    self.assertEqual(batch.status, "superseded")
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"pending"})
            finally:
                store.close()

    def test_requeue_failed_batches_still_releases_batches_below_threshold(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                self._insert_batch(
                    store,
                    "recoverable",
                    count=2,
                    batch_status="failed",
                    item_status="failed",
                    outbox_status="failed",
                    last_error="immutable_object_changed",
                    attempts=4,
                )
                outbox = IngestionOutbox(store.database)
                result = outbox.requeue_failed_batches(errors={"immutable_object_changed"})
                self.assertEqual(result, {"batches": 1, "events": 2, "skipped": 0})
            finally:
                store.close()

    def test_cli_sync_requeue_failed_force_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["sync-requeue-failed"])
        self.assertFalse(args.force)
        args = parser.parse_args(["sync-requeue-failed", "--force"])
        self.assertTrue(args.force)


class MarkBatchVocabularyTests(SyncHardeningTestCase):
    def test_mark_batch_rejects_unknown_status(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                outbox = IngestionOutbox(store.database)
                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                )
                self.assertIsNotNone(batch)

                with self.assertRaisesRegex(ValueError, "unknown batch status"):
                    outbox.mark_batch("batch", status="bogus")

                with store.database.session_factory() as session:
                    persisted = session.get(SyncBatch, "batch")
                    self.assertEqual(persisted.status, "pending")

                for status in ("pending", "uploading", "acked", "partial", "failed", "superseded"):
                    outbox.mark_batch("batch", status=status)
                    with store.database.session_factory() as session:
                        self.assertEqual(session.get(SyncBatch, "batch").status, status)
            finally:
                store.close()


class SigtermTests(SyncHardeningTestCase):
    def test_sigterm_drains_inflight_batches_and_finishes_run(self) -> None:
        import signal

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(3):
                    store.enqueue_article_for_sync(self._article(number))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )
                delivered: list[str] = []

                def deliver(batch, _options):
                    delivered.append(batch.batch_id)
                    if len(delivered) == 1:
                        os.kill(os.getpid(), signal.SIGTERM)
                    sync.outbox.mark_batch(batch.batch_id, status="acked")
                    return DeliveryResult("acked", len(batch.items), 0)

                sync._deliver = deliver
                before = signal.getsignal(signal.SIGTERM)
                try:
                    summary = sync.sync(options=SyncOptions(batch_size=1))
                finally:
                    sync.close()

                # The in-flight batch drained and was acked; no new batches
                # were claimed after the signal.
                self.assertEqual(summary["status"], "partial")
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["batches"], 1)
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(
                        sum(row.status == "pending" for row in rows),
                        2,
                    )
                    run = session.scalar(select(SyncRun))
                    self.assertEqual(run.status, "partial")
                    self.assertIsNotNone(run.finished_at)

                # The previous disposition is restored after the run.
                self.assertEqual(signal.getsignal(signal.SIGTERM), before)
            finally:
                store.close()

    def test_cli_sigterm_wrapper_raises_keyboard_interrupt(self) -> None:
        import signal

        previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            with self.assertRaises(KeyboardInterrupt):
                with _sigterm_as_keyboard_interrupt():
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.1)
            # The wrapper restores the previous handler.
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        finally:
            signal.signal(signal.SIGTERM, previous)


class ProgressLogTests(SyncHardeningTestCase):
    def test_each_batch_writes_one_progress_line_to_stderr(self) -> None:
        import contextlib
        import io

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(2):
                    store.enqueue_article_for_sync(self._article(number))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )
                batch_ids: list[str] = []

                def deliver(batch, _options):
                    batch_ids.append(batch.batch_id)
                    sync.outbox.mark_batch(batch.batch_id, status="acked")
                    return DeliveryResult("acked", len(batch.items), 0)

                sync._deliver = deliver
                captured = io.StringIO()
                try:
                    with contextlib.redirect_stderr(captured):
                        sync.sync(options=SyncOptions(batch_size=1))
                finally:
                    sync.close()

                lines = [line for line in captured.getvalue().splitlines() if "sync batch" in line]
                self.assertEqual(len(lines), 2)
                for batch_id, line in zip(batch_ids, lines, strict=True):
                    self.assertIn(batch_id, line)
                self.assertIn("acked=1", lines[0])
                self.assertIn("acked=2", lines[1])
                self.assertIn("failed=0", lines[1])
            finally:
                store.close()


class ObjectReadTests(SyncHardeningTestCase):
    def test_upload_reuses_prechecked_object_bytes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(
                    200, json=self._plan_response(request, upload=True), request=request
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                return httpx.Response(200, json=self._upload_response(request), request=request)
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                reads = 0
                original = sync._object_bytes

                def counted(manifest):
                    nonlocal reads
                    reads += 1
                    return original(manifest)

                sync._object_bytes = counted
                try:
                    summary = sync.sync()
                finally:
                    sync.close()
                self.assertEqual(summary["acked"], 1)
                # body_html + body_markdown, each read exactly once (the
                # precheck hands its bytes to the upload instead of the
                # upload re-reading the spool).
                self.assertEqual(reads, 2)
            finally:
                store.close()

    @staticmethod
    def _batch_response(request: httpx.Request) -> dict:
        payload = json.loads(request.content)
        return {
            "batchId": payload["batchId"],
            "clientRunId": payload["clientRunId"],
            "payloadDigest": hashlib.sha256(request.content).hexdigest(),
            "results": [
                {
                    "sourceId": item["sourceId"],
                    "canonicalUrl": item["canonicalUrl"],
                    "revisionHash": item["revisionHash"],
                    "status": "created",
                    "publicationId": "publication-id",
                    "revisionId": "revision-id",
                }
                for item in payload["items"]
            ],
        }

    def _plan_response(self, request: httpx.Request, *, upload: bool) -> dict:
        payload = json.loads(request.content)
        objects = []
        for item in payload["objects"]:
            objects.append(
                {
                    "kind": item["kind"],
                    "sha256": item["sha256"],
                    "r2Key": f"publications/{item['sha256']}",
                    "status": "upload_required" if upload else "already_present",
                    "uploadUrl": (
                        f"{self.server}/api/ingestion/publications/objects/"
                        f"{payload['batchId']}/{item['kind']}/{item['sha256']}"
                        if upload
                        else None
                    ),
                    "requiredHeaders": {
                        "Content-Type": "text/html"
                        if item["kind"] == "body_html"
                        else "text/markdown",
                    },
                }
            )
        return {"batchId": payload["batchId"], "objects": objects}

    @staticmethod
    def _upload_response(request: httpx.Request) -> dict:
        batch_id, kind, sha256 = request.url.path.rsplit("/", 3)[-3:]
        return {
            "batchId": batch_id,
            "kind": kind,
            "sha256": sha256,
            "status": "linked",
        }


class HttpErrorClassificationTests(SyncHardeningTestCase):
    def test_programming_httpx_errors_raise_without_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.LocalProtocolError("client misuse", request=request)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                    sleep=lambda _seconds: None,
                )
                try:
                    with self.assertRaises(httpx.LocalProtocolError):
                        sync.sync(options=SyncOptions(max_retries=3))
                finally:
                    sync.close()
                self.assertEqual(calls, 1)
            finally:
                store.close()

    def test_redirect_response_fails_batch_terminally(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example.test/"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                try:
                    summary = sync.sync(options=SyncOptions(max_retries=3))
                finally:
                    sync.close()
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(calls, 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.status, "failed")
                    self.assertEqual(batch.last_error, "http_redirect")
                    self.assertIsNone(batch.response_json)
            finally:
                store.close()

    def test_retry_after_http_date_is_interpreted_as_gmt(self) -> None:
        import time as time_module

        from ustc_crawler.sync.client import _retry_after

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "Wed, 01 Jan 2030 00:00:10 -0000"},
                request=request,
            )

        request = httpx.Request("GET", "https://ingest.example.test/")
        transport = httpx.MockTransport(handler)
        response = transport.handle_request(request)
        now = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp()
        previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time_module.tzset()
        try:
            self.assertEqual(_retry_after(response, lambda: now), 10.0)
        finally:
            if previous_tz is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = previous_tz
            time_module.tzset()


class ExitCodeTests(SyncHardeningTestCase):
    def test_sync_exit_code_reflects_failed_batches(self) -> None:
        import unittest.mock

        class FakeClient:
            summary: dict = {}

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def sync(self, **_kwargs) -> dict:
                return dict(self.summary)

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
                FakeClient.summary = {"failed": 0}
                self.assertEqual(main(argv), 0)
                FakeClient.summary = {"failed": 2}
                self.assertEqual(main(argv), 2)

    def test_sync_without_server_exits_cleanly_with_usage_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "sync",
                        "--db",
                        str(root / "crawler.sqlite"),
                        "--data-dir",
                        str(root / "data"),
                    ]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_crawl_exit_code_reflects_errors(self) -> None:
        import unittest.mock

        with unittest.mock.patch.object(cli, "run_crawl") as run_crawl:
            run_crawl.return_value = {"processed": 1, "errors": 0}
            self.assertEqual(main(["crawl"]), 0)
            run_crawl.return_value = {"processed": 1, "errors": 3}
            self.assertEqual(main(["crawl"]), 2)


class ErrorDetailPersistenceTests(SyncHardeningTestCase):
    def _run_sync(self, store: Store, handler) -> dict:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        sync = IngestionSyncClient(
            store.database,
            store.data_dir,
            self.server,
            self.ingestion_secret,
            http_client=client,
        )
        try:
            return sync.sync()
        finally:
            sync.close()

    def test_4xx_persists_sanitized_response_excerpt(self) -> None:
        body = (
            '{"issues": ['
            '{"path": ["items", 0, "title"], "message": "Required"}, '
            '{"path": ["items", 0, "publishedAt"], "message": "Invalid date"}'
            "]}"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                summary = self._run_sync(store, handler)

                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    row = session.scalar(select(SyncOutbox))
                    self.assertEqual(batch.status, "failed")
                    self.assertEqual(batch.last_error, "http_400")
                    persisted = json.loads(batch.response_json)
                    self.assertEqual(persisted["error"], "http_400")
                    self.assertIn('"title"', persisted["detail"])
                    self.assertIn("Required", persisted["detail"])
                    self.assertIn("publishedAt", persisted["detail"])
                    # The event rows keep the error code only.
                    self.assertEqual(row.last_error, "http_400")
                    self.assertIsNone(row.response_json)
            finally:
                store.close()

    def test_excerpt_is_flattened_and_truncated(self) -> None:
        body = "  " + "verbose detail with\nnewlines\tand   spaces  " * 40

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                summary = self._run_sync(store, handler)

                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    persisted = json.loads(batch.response_json)
                    detail = persisted["detail"]
                    self.assertLessEqual(len(detail), MAX_ERROR_DETAIL_CHARS)
                    self.assertGreater(len(detail), MAX_ERROR_DETAIL_CHARS - 10)
                    self.assertNotIn("\n", detail)
                    self.assertNotIn("\t", detail)
                    self.assertNotIn("  ", detail)
            finally:
                store.close()

    def test_excerpt_redacts_ingestion_secret(self) -> None:
        body = f"authentication failed for token {self.ingestion_secret} on account 42"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                content=body.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                summary = self._run_sync(store, handler)

                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertNotIn(self.ingestion_secret, batch.response_json)
                    persisted = json.loads(batch.response_json)
                    self.assertIn("[redacted]", persisted["detail"])
                    self.assertIn("authentication failed for token", persisted["detail"])
            finally:
                store.close()

    def test_empty_error_body_keeps_response_json_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=b"", request=request)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                summary = self._run_sync(store, handler)

                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.last_error, "http_400")
                    self.assertIsNone(batch.response_json)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
