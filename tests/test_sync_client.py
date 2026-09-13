from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import ValidationError
from sqlalchemy import event, func, select

from ustc_crawler.cli import build_parser
from ustc_crawler.db.models import Media, SyncBatch, SyncBatchItem, SyncOutbox
from ustc_crawler.models import ArticleDocument, ImageRef, SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.sync.client import (
    DEFAULT_BATCH_CONCURRENCY,
    DEFAULT_HTTP_TIMEOUT,
    INGESTION_SECRET_HEADER,
    MAX_BATCH_CONCURRENCY,
    MAX_OBJECT_CONCURRENCY,
    DeliveryResult,
    IngestionSyncClient,
    SyncClientError,
    SyncOptions,
    SyncTransientError,
    ingestion_secret_from_environment,
    sync_backfill,
)
from ustc_crawler.sync.models import (
    MAX_OBJECT_PLAN_OBJECTS,
    IngestionBatch,
    IngestionBatchResponse,
    LocalObjectManifest,
    PublicationSourceDescriptor,
    build_ingestion_batch,
    build_publication,
)
from ustc_crawler.sync.outbox import (
    IngestionOutbox,
    spool_article_objects,
    spool_bytes,
    wire_manifest,
)


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

    def test_batch_digest_matches_server_after_trim_transform(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/ingestion/publications/batches")
            payload = json.loads(request.content)
            # Zod parses and trims the request before the server computes its
            # canonical payload digest.
            server_payload = IngestionBatch.model_validate(payload)
            item = payload["items"][0]
            return httpx.Response(
                200,
                json={
                    "batchId": payload["batchId"],
                    "clientRunId": payload["clientRunId"],
                    "payloadDigest": hashlib.sha256(server_payload.payload_bytes()).hexdigest(),
                    "results": [
                        {
                            "sourceId": item["sourceId"],
                            "canonicalUrl": item["canonicalUrl"],
                            "revisionHash": item["revisionHash"],
                            "status": "created",
                            "publicationId": "publication-id",
                            "revisionId": "revision-id",
                        }
                    ],
                },
                request=request,
            )

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            sync = IngestionSyncClient(
                store.database,
                store.data_dir,
                self.server,
                self.ingestion_secret,
                http_client=client,
            )
            try:
                article = self._article(1)
                article.title = "T" * 999 + " " + "overflow"
                publication = build_publication(
                    article,
                    publication_type="notice",
                    observed_at="2026-08-20",
                )
                batch = build_ingestion_batch(
                    [publication],
                    sources=[
                        PublicationSourceDescriptor(
                            id="source",
                            name=" Test source ",
                            allowedHosts=[" example.edu "],
                        )
                    ],
                    client_run_id=" run ",
                    batch_id=" batch ",
                    observed_at="2026-08-20",
                    producer_version=" crawler ",
                )
                response = sync._post_batch(batch, SyncOptions())
                self.assertEqual(response.payload_digest, batch.payload_sha256())
            finally:
                sync.close()
                store.close()

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

    def test_server_configuration_must_be_an_origin(self) -> None:
        invalid_servers = [
            "https://user@example.test",
            "https://example.test/api",
            "https://example.test?query=value",
            "https://example.test#fragment",
        ]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for server in invalid_servers:
                    with (
                        self.subTest(server=server),
                        self.assertRaisesRegex(ValueError, "must be an HTTP\\(S\\) origin"),
                    ):
                        IngestionSyncClient(
                            store.database,
                            store.data_dir,
                            server,
                            self.ingestion_secret,
                        )
            finally:
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
                return httpx.Response(
                    200, json=self._plan_response(request, upload=True), request=request
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                upload_requests.append(request)
                self.assertEqual(request.headers[INGESTION_SECRET_HEADER], ingestion_secret)
                return httpx.Response(
                    200,
                    json=self._upload_response(request),
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
                    self.assertEqual(
                        request.headers[INGESTION_SECRET_HEADER],
                        ingestion_secret,
                    )
                    self.assertIn(
                        request.headers["content-type"],
                        {"text/html", "text/markdown"},
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
            finally:
                sync.close()
                store.close()

    def test_sync_never_sends_secret_to_an_unexpected_upload_url(self) -> None:
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                response = self._plan_response(request, upload=True)
                response["objects"][0]["uploadUrl"] = "https://attacker.example/upload"
                return httpx.Response(200, json=response, request=request)
            raise AssertionError(f"unexpected request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(20))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["failed"], 1)
                self.assertFalse(
                    any(url.startswith("https://attacker.example") for url in requested_urls)
                )
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.last_error, "object_upload_url_mismatch")
            finally:
                sync.close()
                store.close()

    def test_sync_does_not_complete_objects_linked_during_planning(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(
                    200,
                    json=self._plan_response(request, upload=False),
                    request=request,
                )
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(2))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )

                summary = sync.sync()

                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
            finally:
                sync.close()
                store.close()

    def test_shared_media_deduplicates_plan_when_link_metadata_and_mime_differ(
        self,
    ) -> None:
        plan_requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                plan_requests.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    json=self._plan_response(request, upload=True),
                    request=request,
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                return httpx.Response(
                    200,
                    json=self._upload_response(request),
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
                first_manifest = spool_bytes(
                    root / "data",
                    b"shared media bytes",
                    kind="media",
                    content_type="image/png",
                    sort_order=0,
                    alt_text="first article",
                )
                second_manifest = spool_bytes(
                    root / "data",
                    b"shared media bytes",
                    kind="media",
                    content_type="image/jpeg",
                    sort_order=1,
                    alt_text="second article",
                )
                outbox.enqueue_publication(
                    build_publication(
                        self._article(1),
                        objects=[wire_manifest(first_manifest)],
                    ),
                    source=source,
                    local_objects=[first_manifest],
                )
                outbox.enqueue_publication(
                    build_publication(
                        self._article(2),
                        objects=[wire_manifest(second_manifest)],
                    ),
                    source=source,
                    local_objects=[second_manifest],
                )
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(options=SyncOptions(batch_size=2))
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(len(plan_requests), 1)
                media_objects = [
                    item for item in plan_requests[0]["objects"] if item["kind"] == "media"
                ]
                self.assertEqual(len(media_objects), 1)
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
                return httpx.Response(
                    200, json=self._plan_response(request, upload=False), request=request
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
                        row.entity_key: row for row in session.scalars(select(SyncOutbox)).all()
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

    def test_unchanged_items_skip_object_requests_and_ack(self) -> None:
        # The server only registers batch object claims when it creates or
        # updates a revision; items reported as "unchanged" have no claims,
        # so planning their objects would fail with http_404. Their bytes
        # were delivered with the batch that first created the revision.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ingestion/publications/batches":
                payload = json.loads(request.content)
                statuses = {item["canonicalUrl"]: "unchanged" for item in payload["items"]}
                return httpx.Response(
                    200,
                    json=self._batch_response(request, statuses=statuses),
                    request=request,
                )
            raise AssertionError(f"unchanged item must not cause object request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                store.enqueue_article_for_sync(self._article(2))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync()
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                with store.database.session_factory() as session:
                    batch = session.scalar(select(SyncBatch))
                    self.assertEqual(batch.status, "acked")
                    self.assertEqual(batch.last_error, None)
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"acked"})
            finally:
                sync.close()
                store.close()

    def test_large_batch_checks_existing_objects_at_protocol_limit(self) -> None:
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
                plan_sizes = [len(request["objects"]) for request in plan_requests]
                self.assertEqual(plan_sizes, [MAX_OBJECT_PLAN_OBJECTS] * 6)
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

    def test_missing_object_urls_are_refreshed_in_bounded_windows(self) -> None:
        lock = threading.Lock()
        plan_count = 0
        events: list[tuple[str, int]] = []
        generation_by_sha: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal plan_count
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                with lock:
                    plan_count += 1
                    generation = plan_count
                    events.append(("plan", generation))
                response = self._plan_response(request, upload=True)
                for item in response["objects"]:
                    generation_by_sha[item["sha256"]] = generation
                return httpx.Response(
                    200,
                    json=response,
                    request=request,
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                with lock:
                    events.append(("put", generation_by_sha[request.url.path.rsplit("/", 1)[-1]]))
                return httpx.Response(
                    200,
                    json=self._upload_response(request),
                    request=request,
                )
            raise AssertionError(f"unexpected sync request: {request.url}")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            client = httpx.Client(transport=httpx.MockTransport(handler))
            try:
                store.enqueue_article_for_sync(self._article(1))
                store.enqueue_article_for_sync(self._article(2))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                    http_client=client,
                )
                summary = sync.sync(options=SyncOptions(object_concurrency=2))
                self.assertEqual(summary["acked"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(plan_count, 2)
                second_plan = events.index(("plan", 2))
                self.assertEqual(
                    sum(kind == "put" for kind, _ in events[:second_plan]),
                    2,
                )
                self.assertEqual(
                    sorted(generation for kind, generation in events if kind == "put"),
                    [1, 1, 2, 2],
                )
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

    def test_sync_rejects_unsafe_concurrency_values(self) -> None:
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
                with self.assertRaisesRegex(
                    ValueError,
                    f"batch concurrency must be between 1 and {MAX_BATCH_CONCURRENCY}",
                ):
                    sync.sync(options=SyncOptions(batch_concurrency=MAX_BATCH_CONCURRENCY + 1))
                with self.assertRaisesRegex(
                    ValueError,
                    f"object concurrency must be between 1 and {MAX_OBJECT_CONCURRENCY}",
                ):
                    sync.sync(options=SyncOptions(object_concurrency=MAX_OBJECT_CONCURRENCY + 1))
            finally:
                sync.close()
                store.close()

    def test_batch_delivery_is_concurrent_but_claims_are_serial_and_bounded(self) -> None:
        lock = threading.Lock()
        entered = threading.Barrier(3, timeout=5)
        active = 0
        max_active = 0
        deliveries = 0

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(5):
                    store.enqueue_article_for_sync(self._article(number))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )

                def deliver(batch, _options):
                    nonlocal active, max_active, deliveries
                    with lock:
                        deliveries += 1
                        call_number = deliveries
                        active += 1
                        max_active = max(max_active, active)
                    try:
                        if call_number <= 3:
                            entered.wait()
                        self.assertEqual(
                            sync.outbox.mark_batch(batch.batch_id, status="acked"),
                            None,
                        )
                        return DeliveryResult("acked", len(batch.items), 0)
                    finally:
                        with lock:
                            active -= 1

                sync._deliver = deliver
                summary = sync.sync(
                    options=SyncOptions(batch_size=1, batch_concurrency=3),
                )
                self.assertEqual(
                    {
                        key: summary[key]
                        for key in (
                            "batches",
                            "replayed",
                            "created",
                            "acked",
                            "failed",
                            "rejected",
                            "pending",
                            "items",
                            "status",
                        )
                    },
                    {
                        "batches": 5,
                        "replayed": 0,
                        "created": 5,
                        "acked": 5,
                        "failed": 0,
                        "rejected": 0,
                        "pending": 0,
                        "items": 5,
                        "status": "completed",
                    },
                )
                self.assertEqual(max_active, 3)
                with store.database.session_factory() as session:
                    batches = session.scalars(select(SyncBatch)).all()
                    items = session.scalars(select(SyncBatchItem)).all()
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(len(batches), 5)
                    self.assertEqual(len(items), 5)
                    self.assertEqual(len({item.item_key for item in items}), 5)
                    self.assertEqual({row.status for row in rows}, {"acked"})
            finally:
                sync.close()
                store.close()

    def test_batch_concurrency_honors_exact_max_batches_and_summary(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(5):
                    store.enqueue_article_for_sync(self._article(number))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )

                def deliver(batch, _options):
                    sync.outbox.mark_batch(batch.batch_id, status="acked")
                    return DeliveryResult("acked", len(batch.items), 0)

                sync._deliver = deliver
                summary = sync.sync(
                    options=SyncOptions(batch_size=1, batch_concurrency=4, max_batches=2),
                )
                self.assertEqual(summary["batches"], 2)
                self.assertEqual(summary["created"], 2)
                self.assertEqual(summary["items"], 2)
                self.assertEqual(summary["acked"], 2)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(summary["pending"], 0)
                self.assertEqual(summary["status"], "completed")
                with store.database.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(SyncBatch)), 2)
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(
                        {row.status for row in rows},
                        {"acked", "pending"},
                    )
                    self.assertEqual(sum(row.status == "acked" for row in rows), 2)
            finally:
                sync.close()
                store.close()

    def test_batch_failure_drains_other_workers_and_is_recoverable(self) -> None:
        lock = threading.Lock()
        failed = False
        failed_batch_id = ""

        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(4):
                    store.enqueue_article_for_sync(self._article(number))
                sync = IngestionSyncClient(
                    store.database,
                    store.data_dir,
                    self.server,
                    self.ingestion_secret,
                )

                def fail_once(batch, _options):
                    nonlocal failed, failed_batch_id
                    with lock:
                        should_fail = not failed
                        if should_fail:
                            failed = True
                            failed_batch_id = batch.batch_id
                    if should_fail:
                        sync.outbox.mark_batch(batch.batch_id, status="uploading")
                        sync.outbox.mark_batch_error(batch.batch_id, "network_error")
                        raise SyncTransientError("network_error")
                    sync.outbox.mark_batch(batch.batch_id, status="acked")
                    return DeliveryResult("acked", len(batch.items), 0)

                sync._deliver = fail_once
                summary = sync.sync(
                    options=SyncOptions(batch_size=1, batch_concurrency=3),
                )
                self.assertEqual(summary["batches"], 3)
                self.assertEqual(summary["created"], 3)
                self.assertEqual(summary["acked"], 2)
                self.assertEqual(summary["pending"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertEqual(summary["status"], "partial")
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(
                        {row.status for row in rows}, {"uploading", "acked", "pending"}
                    )
                    replay = session.get(SyncBatch, failed_batch_id)
                    self.assertIsNotNone(replay)
                    self.assertEqual(replay.status, "uploading")

                seen: list[str] = []

                def retry(batch, _options):
                    seen.append("replay" if batch.batch_id == failed_batch_id else "new")
                    sync.outbox.mark_batch(batch.batch_id, status="acked")
                    return DeliveryResult("acked", len(batch.items), 0)

                sync._deliver = retry
                resumed = sync.sync(
                    options=SyncOptions(batch_size=1, batch_concurrency=2),
                )
                self.assertEqual(seen, ["replay", "new"])
                self.assertEqual(resumed["batches"], 2)
                self.assertEqual(resumed["replayed"], 1)
                self.assertEqual(resumed["created"], 1)
                self.assertEqual(resumed["acked"], 2)
                self.assertEqual(resumed["status"], "completed")
                with store.database.session_factory() as session:
                    self.assertEqual(
                        {row.status for row in session.scalars(select(SyncOutbox)).all()},
                        {"acked"},
                    )
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
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
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
                return httpx.Response(
                    200,
                    json=self._upload_response(request),
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
                    json=self._plan_response(request, upload=True),
                    request=request,
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                kind = request.url.path.rsplit("/", 3)[-2]
                if kind == "body_markdown":
                    high_failure_finished.set()
                    return httpx.Response(
                        403,
                        json={"error": "forbidden"},
                        request=request,
                    )
                if kind == "body_html":
                    if not high_failure_finished.wait(timeout=5):
                        raise AssertionError("object uploads did not run concurrently")
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
                return httpx.Response(
                    200, json=self._plan_response(request, upload=False), request=request
                )
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
                        {row.status for row in session.scalars(select(SyncOutbox)).all()},
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
                return httpx.Response(
                    200, json=self._plan_response(request, upload=False), request=request
                )
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

    def test_permanent_failure_does_not_stop_claiming_remaining_events(self) -> None:
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
                summary = sync.sync(options=SyncOptions(batch_size=1))
                self.assertEqual(batch_calls, 2)
                self.assertEqual(summary["batches"], 2)
                self.assertEqual(summary["failed"], 2)
                self.assertEqual(summary["status"], "completed")
                with store.database.session_factory() as session:
                    rows = session.scalars(select(SyncOutbox).order_by(SyncOutbox.entity_key)).all()
                    self.assertEqual({row.status for row in rows}, {"failed"})
                    self.assertEqual(sum(row.batch_id is not None for row in rows), 2)
            finally:
                sync.close()
                store.close()

    def test_object_bytes_resolves_legacy_cwd_relative_manifest_paths(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "repo"
            work.mkdir()
            # Alembic resolves its script directory relative to the cwd.
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "alembic", work / "alembic"
            )
            previous_cwd = Path.cwd()
            os.chdir(work)
            try:
                store = Store("data/crawler.sqlite", "data")
                store.add_source(self._source())
                store.enqueue_article_for_sync(self._article(11))
            finally:
                os.chdir(previous_cwd)
            try:
                with store.database.session_factory() as session:
                    row = session.scalar(select(SyncOutbox))
                manifest = LocalObjectManifest.model_validate(
                    json.loads(row.object_manifest_json)[0]
                )
                self.assertFalse(Path(manifest.local_path).is_absolute())
                sync = IngestionSyncClient(
                    store.database,
                    work / "data",
                    self.server,
                    self.ingestion_secret,
                )
                try:
                    os.chdir(root)
                    body = sync._object_bytes(manifest)
                finally:
                    os.chdir(previous_cwd)
                    sync.close()
                self.assertEqual(hashlib.sha256(body).hexdigest(), manifest.sha256)
            finally:
                store.close()

    def test_local_object_mutation_is_rejected_before_put(self) -> None:
        put_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal put_count
            if request.url.path == "/api/ingestion/publications/batches":
                return httpx.Response(200, json=self._batch_response(request), request=request)
            if request.url.path == "/api/ingestion/publications/objects/plan":
                return httpx.Response(
                    200, json=self._plan_response(request, upload=True), request=request
                )
            if request.url.path.startswith("/api/ingestion/publications/objects/"):
                put_count += 1
                return httpx.Response(200, json=self._upload_response(request), request=request)
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
                summary = sync.sync(options=SyncOptions(max_retries=0))
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

    def test_backfill_bulk_snapshot_preserves_payload_and_shared_media(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                shared_image = ImageRef(
                    url="https://example.edu/uploads/shared.png",
                    alt="shared",
                    title="Shared image",
                    caption="A shared image",
                )
                articles = [self._article(1), self._article(2)]
                for article in articles:
                    article.images = [shared_image]
                    store.save_article(article)
                media_path = store.save_media(
                    shared_image,
                    b"shared image bytes",
                    "image/png",
                    articles[0].url,
                    articles[0].source_page_url,
                )
                assert media_path is not None
                store.link_media(shared_image, articles[1].url, articles[1].source_page_url)
                with store.database.session_factory.begin() as session:
                    session.get(Media, shared_image.url).article_url = None
                asset_url = "https://example.edu/files/guide.pdf"
                asset_path = store.save_asset(
                    url=asset_url,
                    source_url=articles[1].url,
                    body=b"%PDF-guide",
                    mime_type="application/pdf",
                )
                assert asset_path is not None

                snapshots = store.sync_article_snapshot_page(limit=2)
                self.assertEqual(
                    [snapshot.article.url for snapshot in snapshots],
                    sorted(article.url for article in articles),
                )
                self.assertEqual(
                    snapshots[1].media_paths[shared_image.url],
                    (str(media_path), "image/png"),
                )
                self.assertEqual(
                    snapshots[1].asset_paths[asset_url],
                    (str(asset_path), "application/pdf"),
                )

                self.assertEqual(
                    sync_backfill(store, chunk_size=2),
                    {"scanned": 2, "enqueued": 2, "errors": 0},
                )
                with store.database.session_factory() as session:
                    actual_rows = {
                        json.loads(row.payload_json)["canonicalUrl"]: row
                        for row in session.scalars(select(SyncOutbox)).all()
                    }
                for snapshot in snapshots:
                    article = snapshot.article
                    local_objects = spool_article_objects(
                        article,
                        store.data_dir,
                        media_paths=store.media_paths_for_article(article.url),
                        asset_paths=store.asset_paths_for_article(
                            article.url,
                            article.source_page_url,
                        ),
                    )
                    expected = build_publication(
                        article,
                        objects=[wire_manifest(item) for item in local_objects],
                    )
                    actual_payload = json.loads(actual_rows[article.url].payload_json)
                    expected_payload = expected.model_dump(
                        by_alias=True,
                        mode="json",
                        exclude_none=True,
                    )
                    # Backfill observations are intentionally generated at
                    # enqueue time; all immutable publication fields must be
                    # identical to the pre-optimization snapshot.
                    expected_payload["observedAt"] = actual_payload["observedAt"]
                    self.assertEqual(actual_payload, expected_payload)
                    self.assertEqual(
                        json.loads(actual_rows[article.url].object_manifest_json),
                        [item.model_dump(mode="json") for item in local_objects],
                    )
            finally:
                store.close()

    def test_bulk_snapshot_query_count_is_bounded_and_keyset_order_is_stable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in range(10, 0, -1):
                    article = self._article(number)
                    with store.database.session_factory.begin() as session:
                        Store._save_article_record(
                            session,
                            article,
                            hashlib.sha256(article.body_text.encode()).hexdigest(),
                            f"2026-08-20T00:00:{number:02d}+08:00",
                        )

                statements: list[str] = []

                def count_selects(
                    _connection, _cursor, statement, _parameters, _context, _executemany
                ):
                    if statement.lstrip().upper().startswith("SELECT"):
                        statements.append(statement)

                event.listen(store.database.engine, "before_cursor_execute", count_selects)
                try:
                    first = store.sync_article_snapshot_page(limit=10)
                finally:
                    event.remove(store.database.engine, "before_cursor_execute", count_selects)

                self.assertEqual(len(first), 10)
                self.assertEqual(
                    [snapshot.article.url for snapshot in first],
                    sorted(snapshot.article.url for snapshot in first),
                )
                self.assertEqual(len(statements), 4)
                self.assertEqual(
                    [
                        snapshot.article.url
                        for snapshot in store.sync_article_snapshot_page(first[-1].article.url)
                    ],
                    [],
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
        self.assertEqual(args.batch_concurrency, DEFAULT_BATCH_CONCURRENCY)
        sync_parser = next(
            action.choices["sync"]
            for action in parser._actions
            if hasattr(action, "choices") and action.choices and "sync" in action.choices
        )
        self.assertIn("max: 100", sync_parser.format_help())
        self.assertIn("max: 64", sync_parser.format_help())
        self.assertIn(f"max: {MAX_BATCH_CONCURRENCY}", sync_parser.format_help())
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
        self.assertEqual(
            parser.parse_args(
                [
                    "sync",
                    "--server",
                    self.server,
                    "--batch-concurrency",
                    "3",
                ]
            ).batch_concurrency,
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
