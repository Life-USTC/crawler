from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select

from ustc_crawler.cli import build_parser
from ustc_crawler.db.models import SyncBatch, SyncBatchItem, SyncOutbox
from ustc_crawler.models import ArticleDocument, SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.sync.client import (
    DEFAULT_HTTP_TIMEOUT,
    INGESTION_SECRET_HEADER,
    IngestionSyncClient,
    SyncClientError,
    SyncOptions,
    ingestion_secret_from_environment,
    sync_backfill,
)
from ustc_crawler.sync.models import (
    MAX_OBJECT_PLAN_OBJECTS,
    IngestionBatchResponse,
    build_publication,
)
from ustc_crawler.sync.outbox import IngestionOutbox, spool_bytes, wire_manifest


class SyncClientTests(unittest.TestCase):
    server = "https://ingest.example.test"
    ingestion_secret = "machine-ingestion-secret"

    def test_batch_response_digest_is_strict_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            IngestionBatchResponse.model_validate(
                {
                    "batchId": "batch",
                    "clientRunId": "run",
                    "payloadDigest": "not-a-digest",
                    "results": [],
                }
            )

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
    def _discovery_source() -> SourceConfig:
        return SourceConfig(
            id="discovery",
            name="Discovery source",
            organization_level="department",
            seed_urls=["https://discovery.example.edu/"],
            allowed_hosts=["discovery.example.edu"],
            discovery_only=True,
        )

    @staticmethod
    def _article(number: int, *, rejected: bool = False) -> ArticleDocument:
        url = f"https://example.edu/news/{number}"
        if rejected:
            url += "-rejected"
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
            body_text=f"Text {number}",
            body_markdown=f"Markdown {number}",
            extraction_method="article",
            source_page_url=url,
        )

    def _store(self, root: Path) -> Store:
        store = Store(root / "crawler.sqlite", root / "data")
        store.add_source(self._source())
        return store

    def test_default_http_timeout_covers_long_ingestion_transactions(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            sync = IngestionSyncClient(
                store.database,
                store.data_dir,
                self.server,
                self.ingestion_secret,
            )
            try:
                self.assertEqual(sync.http.timeout.read, DEFAULT_HTTP_TIMEOUT)
                self.assertEqual(sync.http.timeout.write, DEFAULT_HTTP_TIMEOUT)
            finally:
                sync.close()
                store.close()

    @staticmethod
    def _batch_response(request: httpx.Request, *, statuses: dict[str, str] | None = None) -> dict:
        payload = json.loads(request.content)
        results = []
        for item in payload["items"]:
            results.append(
                {
                    "sourceId": item["sourceId"],
                    "canonicalUrl": item["canonicalUrl"],
                    "revisionHash": item["revisionHash"],
                    "status": (statuses or {}).get(item["canonicalUrl"], "created"),
                    "publicationId": "publication-id",
                    "revisionId": "revision-id",
                    **(
                        {"error": "unsafe server detail containing secret-token"}
                        if (statuses or {}).get(item["canonicalUrl"]) == "rejected"
                        else {}
                    ),
                }
            )
        return {
            "batchId": payload["batchId"],
            "clientRunId": payload["clientRunId"],
            "payloadDigest": hashlib.sha256(request.content).hexdigest(),
            "results": results,
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
                    "uploadUrl": f"{self.server}/signed/{item['sha256']}" if upload else None,
                    "expiresAt": None,
                    "requiredHeaders": {
                        "Content-Type": "text/html" if item["kind"] == "body_html" else "text/markdown",
                        "x-amz-meta-kind": item["kind"],
                        "x-amz-meta-sha256": item["sha256"],
                    },
                }
            )
        return {"batchId": payload["batchId"], "objects": objects}

    def test_sync_uploads_plan_objects_with_exact_headers_and_no_secrets(self) -> None:
        batch_requests: list[httpx.Request] = []
        plan_requests: list[dict] = []
        upload_requests: list[httpx.Request] = []
        ingestion_secret = "machine-ingestion-secret"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                batch_requests.append(request)
                return httpx.Response(
                    200,
                    json=self._batch_response(request),
                    request=request,
                )
            if request.url.path == "/api/ingestion/publications/objects/plan":
                self.assertEqual(request.headers[INGESTION_SECRET_HEADER], ingestion_secret)
                plan_requests.append(json.loads(request.content))
                return httpx.Response(200, json=self._plan_response(request, upload=True), request=request)
            if request.url.path.startswith("/signed/"):
                upload_requests.append(request)
                return httpx.Response(200, request=request)
            if request.url.path == "/api/ingestion/publications/objects/complete":
                self.assertEqual(request.headers[INGESTION_SECRET_HEADER], ingestion_secret)
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={
                        "batchId": payload["batchId"],
                        "kind": payload["kind"],
                        "sha256": payload["sha256"],
                        "status": "verified",
                    },
                    request=request,
                )
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
                    ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(len(batch_requests), 1)
                batch_payload = json.loads(batch_requests[0].content)
                self.assertEqual(
                    batch_requests[0].headers["Idempotency-Key"],
                    batch_payload["batchId"],
                )
                self.assertEqual(
                    batch_requests[0].headers[INGESTION_SECRET_HEADER],
                    ingestion_secret,
                )
                self.assertEqual(len(plan_requests), 1)
                self.assertEqual(len(upload_requests), len(plan_requests[0]["objects"]))
                for request in upload_requests:
                    self.assertNotIn("authorization", request.headers)
                    self.assertIn(request.headers["x-amz-meta-kind"], {"body_html", "body_markdown"})
                    self.assertEqual(
                        request.headers["x-amz-meta-sha256"],
                        request.url.path.rsplit("/", 1)[-1],
                    )
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    outbox = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(batch.status, "acked")
                    self.assertTrue(all(row.status == "acked" for row in outbox))
                    durable = " ".join(
                        [
                            batch.response_json or "",
                            *(row.payload_json for row in outbox),
                            *(row.object_manifest_json for row in outbox),
                        ]
                    )
                    self.assertNotIn(ingestion_secret, durable)
                    self.assertNotIn("uploadUrl", durable)
                    self.assertNotIn("signed/", durable)
            finally:
                sync.close()
                store.close()

    def test_mixed_result_only_plans_accepted_objects_and_marks_partial(self) -> None:
        plan_requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                payload = json.loads(request.content)
                statuses = {item["canonicalUrl"]: "created" for item in payload["items"]}
                rejected_url = next(url for url in statuses if url.endswith("-rejected"))
                statuses[rejected_url] = "rejected"
                return httpx.Response(
                    200,
                    json=self._batch_response(request, statuses=statuses),
                    request=request,
                )
            if request.url.path == "/api/ingestion/publications/objects/plan":
                plan_requests.append(json.loads(request.content))
                return httpx.Response(200, json=self._plan_response(request, upload=False), request=request)
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={**payload, "status": "linked"},
                    request=request,
                )
            raise AssertionError(f"rejected item should not cause object request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                store.enqueue_article_for_sync(self._article(2, rejected=True))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(summary["rejected"], 1)
                self.assertEqual(len(plan_requests), 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    items = session.scalars(
                        select(SyncBatchItem).order_by(SyncBatchItem.canonical_url)
                    ).all()
                    rows = {
                        row.entity_key: row
                        for row in session.scalars(select(SyncOutbox)).all()
                    }
                    self.assertEqual(batch.status, "partial")
                    self.assertEqual(batch.last_error, "server_rejected")
                    self.assertEqual(
                        {item.status for item in items},
                        {"acked", "rejected"},
                    )
                    self.assertEqual(
                        {row.status for row in rows.values()},
                        {"acked", "failed"},
                    )
                    self.assertNotIn("unsafe server detail", batch.response_json or "")
                    self.assertNotIn("secret-token", batch.response_json or "")
            finally:
                sync.close()
                store.close()

    def test_large_batch_splits_object_plans_at_protocol_limit(self) -> None:
        plan_requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                payload = json.loads(request.content)
                plan_requests.append(payload)
                return httpx.Response(
                    200,
                    json=self._plan_response(request, upload=False),
                    request=request,
                )
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={**payload, "status": "linked"},
                    request=request,
                )
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                outbox = IngestionOutbox(store.database)
                source = store.source_descriptor("source")
                for number in range(100):
                    local_objects = tuple(
                        spool_bytes(
                            root / "data",
                            f"{number}-{index}".encode(),
                            kind="asset",
                            content_type="application/octet-stream",
                        )
                        for index in range(6)
                    )
                    outbox.enqueue_publication(
                        build_publication(
                            self._article(number),
                            objects=[wire_manifest(item) for item in local_objects],
                        ),
                        source=source,
                        local_objects=local_objects,
                    )
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(options=SyncOptions(batch_size=100))
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(
                    [len(request["objects"]) for request in plan_requests],
                    [MAX_OBJECT_PLAN_OBJECTS] * 6,
                )
                object_keys = [
                    (item["kind"], item["sha256"])
                    for request in plan_requests
                    for item in request["objects"]
                ]
                self.assertEqual(object_keys, sorted(object_keys))
                self.assertEqual(len(object_keys), len(set(object_keys)))
            finally:
                sync.close()
                store.close()

    def test_sync_rejects_batch_size_above_protocol_limit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    sync.sync(options=SyncOptions(batch_size=101))
            finally:
                sync.close()
                store.close()

    def test_object_plan_rejects_membership_outside_current_chunk(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                payload = self._plan_response(request, upload=False)
                payload["objects"][0]["sha256"] = "f" * 64
                return httpx.Response(200, json=payload, request=request)
            raise AssertionError(f"membership failure should stop object delivery: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(13))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.last_error, "object_plan_membership_mismatch")
            finally:
                sync.close()
                store.close()

    def test_object_delivery_is_concurrent_but_bounded(self) -> None:
        lock = threading.Lock()
        entered = threading.Barrier(3, timeout=5)
        active = 0
        max_active = 0
        put_started = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, max_active, put_started
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(
                    200,
                    json=self._plan_response(request, upload=True),
                    request=request,
                )
            if request.url.path.startswith("/signed/"):
                with lock:
                    put_started += 1
                    barrier_index = put_started
                    active += 1
                    max_active = max(max_active, active)
                try:
                    if barrier_index <= 3:
                        entered.wait()
                finally:
                    with lock:
                        active -= 1
                return httpx.Response(200, request=request)
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={**payload, "status": "verified"},
                    request=request,
                )
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(10))
                store.enqueue_article_for_sync(self._article(11))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(options=SyncOptions(object_concurrency=3))
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(max_active, 3)
            finally:
                sync.close()
                store.close()

    def test_concurrent_object_failures_propagate_in_plan_order(self) -> None:
        high_failure_finished = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(
                    200,
                    json=self._plan_response(request, upload=False),
                    request=request,
                )
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                if payload["kind"] == "body_markdown":
                    high_failure_finished.set()
                    return httpx.Response(
                        403,
                        json={"error": "forbidden"},
                        request=request,
                    )
                if payload["kind"] == "body_html":
                    if not high_failure_finished.wait(timeout=5):
                        raise AssertionError("object completions did not run concurrently")
                    return httpx.Response(
                        400,
                        json={"error": "invalid_request"},
                        request=request,
                    )
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(12))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(options=SyncOptions(object_concurrency=2))
                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.last_error, "invalid_request")
            finally:
                sync.close()
                store.close()

    def test_duplicate_url_revisions_use_triple_result_identity(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(200, json=self._plan_response(request, upload=False), request=request)
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                return httpx.Response(200, json={**payload, "status": "linked"}, request=request)
            raise AssertionError(f"unexpected request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                first = self._article(6)
                second = self._article(6)
                second.title = "Revised notice"
                second.body_text = "Revised text"
                second.body_html = "<p>Revised HTML</p>"
                second.body_markdown = "Revised markdown"
                store.enqueue_article_for_sync(first)
                store.enqueue_article_for_sync(second)
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["acked"], 1)
                with store.database.session_factory() as session:
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(SyncBatchItem)),
                        2,
                    )
                    self.assertEqual(
                        {
                            row.status for row in session.scalars(select(SyncOutbox)).all()
                        },
                        {"acked"},
                    )
            finally:
                sync.close()
                store.close()

    def test_lost_batch_response_retries_same_idempotent_request(self) -> None:
        requests: list[httpx.Request] = []
        sleeps: list[float] = []
        first = True

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal first
            if request.url.path == "/api/ingestion/publications/batches":
                requests.append(request)
                if first:
                    first = False
                    raise httpx.ReadError("response lost", request=request)
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(200, json=self._plan_response(request, upload=False), request=request)
            if request.url.path == "/api/ingestion/publications/objects/complete":
                payload = json.loads(request.content)
                return httpx.Response(200, json={**payload, "status": "linked"}, request=request)
            raise AssertionError(f"unexpected request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(3))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                    sleep=sleeps.append,
                )
                summary = sync.sync(options=SyncOptions(max_retries=1))
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(len(requests), 2)
                self.assertEqual(
                    requests[0].headers["Idempotency-Key"],
                    requests[1].headers["Idempotency-Key"],
                )
                self.assertEqual(requests[0].content, requests[1].content)
                self.assertEqual(sleeps, [1])
            finally:
                sync.close()
                store.close()

    def test_nonretryable_response_marks_batch_failed_without_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": "invalid_batch", "message": "contains secret-token"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(4))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["failed"], 1)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    row = session.scalar(select(SyncOutbox))
                    self.assertEqual(batch.status, "failed")
                    self.assertEqual(batch.last_error, "invalid_batch")
                    self.assertIsNone(batch.response_json)
                    self.assertEqual(row.last_error, "invalid_batch")
                    self.assertNotIn("secret-token", row.payload_json)
            finally:
                sync.close()
                store.close()

    def test_permanent_failure_stops_before_claiming_remaining_events(self) -> None:
        batch_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal batch_calls
            if request.url.path != "/api/ingestion/publications/batches":
                raise AssertionError(f"no follow-up request expected: {request.url}")
            batch_calls += 1
            return httpx.Response(
                403,
                json={"error": "forbidden", "message": "do not persist this detail"},
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(7))
                store.enqueue_article_for_sync(self._article(8))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(
                    options=SyncOptions(batch_size=1)
                )
                self.assertEqual(batch_calls, 1)
                self.assertEqual(summary["batches"], 1)
                self.assertEqual(summary["status"], "partial")
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox).order_by(SyncOutbox.entity_key)).all()
                    self.assertEqual({row.status for row in rows}, {"failed", "pending"})
                    self.assertEqual(sum(row.batch_id is not None for row in rows), 1)
            finally:
                sync.close()
                store.close()

    def test_local_object_mutation_is_rejected_before_put(self) -> None:
        put_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal put_count
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(200, json=self._plan_response(request, upload=True), request=request)
            if request.url.path.startswith("/signed/"):
                put_count += 1
                return httpx.Response(200, request=request)
            raise AssertionError(f"unexpected request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(5))
                with store.database.session_factory() as session:
                    row = session.scalar(select(SyncOutbox))
                    path = Path(json.loads(row.object_manifest_json)[0]["local_path"])
                path.write_bytes(b"changed after spool")
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(
                    options=SyncOptions(max_retries=0)
                )
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(put_count, 0)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.last_error, "immutable_object_changed")
            finally:
                sync.close()
                store.close()

    def test_backfill_is_keyset_paginated_and_idempotent_without_network(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                source_descriptor_calls = 0
                original_source_descriptor = store.source_descriptor

                def counted_source_descriptor(source_id: str):
                    nonlocal source_descriptor_calls
                    source_descriptor_calls += 1
                    return original_source_descriptor(source_id)

                store.source_descriptor = counted_source_descriptor
                for number in range(1, 4):
                    article = self._article(number)
                    with store.database.session_factory.begin() as session:
                        Store._save_article_record(
                            session,
                            article,
                            hashlib.sha256(article.body_text.encode()).hexdigest(),
                            f"2026-08-20T00:00:0{number}+08:00",
                        )
                first = sync_backfill(store, chunk_size=1)
                self.assertEqual(source_descriptor_calls, 1)
                second = sync_backfill(store, chunk_size=1)
                self.assertEqual(source_descriptor_calls, 2)
                self.assertEqual(first, {"scanned": 3, "enqueued": 3, "errors": 0})
                self.assertEqual(second, {"scanned": 3, "enqueued": 0, "errors": 0})
                with store.database.session_factory() as session:
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(SyncOutbox)),
                        3,
                    )
            finally:
                store.close()

    def test_discovery_only_article_is_archived_without_an_outbox_event(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                source = self._discovery_source()
                store.add_source(source)
                article = self._article(20)
                article.source_id = source.id

                store.save_article_and_enqueue_for_sync(article)
                self.assertTrue(store.article_exists(article.url))
                self.assertIsNone(store.enqueue_article_for_sync(article))
                with store.database.session_factory() as session:
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(SyncOutbox)),
                        0,
                    )
            finally:
                store.close()

    def test_backfill_skips_discovery_only_articles(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                regular = self._source()
                discovery = self._discovery_source()
                store.add_sources([regular, discovery])
                for number, source_id in ((21, regular.id), (22, discovery.id)):
                    article = self._article(number)
                    article.source_id = source_id
                    with store.database.session_factory.begin() as session:
                        Store._save_article_record(
                            session,
                            article,
                            hashlib.sha256(article.body_text.encode()).hexdigest(),
                            f"2026-08-20T00:00:{number - 20:02d}+08:00",
                        )

                self.assertEqual(
                    sync_backfill(store, chunk_size=1),
                    {"scanned": 2, "enqueued": 1, "errors": 0},
                )
                self.assertEqual(
                    sync_backfill(store, chunk_size=1),
                    {"scanned": 2, "enqueued": 0, "errors": 0},
                )
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(json.loads(rows[0].source_json)["id"], regular.id)
            finally:
                store.close()

    def test_same_revision_preserves_first_observation_timestamp(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                outbox = IngestionOutbox(store.database)
                article = self._article(9)
                source = store.source_descriptor("source")
                first_id, first_created = outbox.enqueue_article_with_status(
                    article,
                    store.data_dir,
                    source=source,
                    observed_at="2026-08-20T10:00:00+08:00",
                )
                second_id, second_created = outbox.enqueue_article_with_status(
                    article,
                    store.data_dir,
                    source=source,
                    observed_at="2026-08-21T10:00:00+08:00",
                )
                self.assertEqual(first_id, second_id)
                self.assertTrue(first_created)
                self.assertFalse(second_created)
                with store.database.session_factory() as session:
                    row = session.scalar(select(SyncOutbox))
                    self.assertEqual(
                        json.loads(row.payload_json)["observedAt"],
                        "2026-08-20T10:00:00+08:00",
                    )
            finally:
                store.close()

    def test_service_secret_is_read_from_environment_without_exposing_it(self) -> None:
        secret = ingestion_secret_from_environment(
            {"USTC_CRAWLER_INGESTION_SECRET": self.ingestion_secret}
        )
        self.assertEqual(secret, self.ingestion_secret)
        with self.assertRaisesRegex(SyncClientError, "missing_ingestion_secret") as raised:
            ingestion_secret_from_environment({})
        self.assertNotIn(self.ingestion_secret, str(raised.exception))

    def test_cli_exposes_service_sync_and_backfill_flags_without_auth_or_secret_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "sync",
                "--server",
                self.server,
                "--db",
                "db.sqlite",
                "--data-dir",
                "data",
            ]
        )
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.server, self.server)
        self.assertEqual(args.object_concurrency, 8)
        sync_parser = next(
            action.choices["sync"]
            for action in parser._actions
            if hasattr(action, "choices") and action.choices and "sync" in action.choices
        )
        self.assertIn("max: 100", sync_parser.format_help())
        self.assertEqual(
            parser.parse_args(
                [
                    "sync",
                    "--server",
                    self.server,
                    "--object-concurrency",
                    "3",
                ]
            ).object_concurrency,
            3,
        )
        self.assertNotIn("auth", parser.format_help())
        self.assertIn("USTC_CRAWLER_INGESTION_SECRET", parser.format_help())
        self.assertEqual(
            parser.parse_args(["sync-backfill", "--db", "db.sqlite"]).command,
            "sync-backfill",
        )


if __name__ == "__main__":
    unittest.main()
