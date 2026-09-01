import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError
from sqlalchemy import func, select, text

from ustc_crawler.db import ALEMBIC_HEAD
from ustc_crawler.db.models import SyncBatch, SyncBatchItem, SyncOutbox, SyncRun
from ustc_crawler.models import ArticleDocument, SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.sync.models import (
    IngestionBatch,
    ObjectManifest,
    PublicationObjectCompleteRequest,
    PublicationObjectPlanRequest,
    PublicationSourceDescriptor,
    build_ingestion_batch,
    build_publication,
    normalize_publication_timestamp,
)
from ustc_crawler.sync.outbox import IngestionOutbox, spool_article_objects, wire_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "ingestion_batch.json"


class IngestionProtocolTests(unittest.TestCase):
    def test_wire_fixture_matches_strict_camel_case_contract(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        batch = IngestionBatch.model_validate(payload)
        expected_wire = json.loads(json.dumps(payload))
        for item in expected_wire["items"]:
            for key in [key for key, value in item.items() if value is None]:
                item.pop(key)
        self.assertEqual(batch.payload_dict(), expected_wire)
        self.assertEqual(IngestionBatch.model_validate(batch.payload_dict()), batch)
        self.assertEqual(
            set(payload),
            {"protocolVersion", "producerVersion", "clientRunId", "batchId", "observedAt", "sources", "items"},
        )
        self.assertNotIn("local_path", batch.payload_bytes().decode())
        with self.assertRaises(ValidationError):
            IngestionBatch.model_validate(payload | {"article_id": "legacy"})
        with self.assertRaises(ValidationError):
            IngestionBatch.model_validate(
                payload
                | {
                    "items": [
                        payload["items"][0] | {"article_id": "legacy"},
                    ]
                }
            )
        plan = PublicationObjectPlanRequest(
            batchId="batch-0001",
            objects=[{"kind": "body_html", "sha256": "b" * 64}],
        )
        self.assertEqual(
            plan.model_dump(by_alias=True),
            {"batchId": "batch-0001", "objects": [{"kind": "body_html", "sha256": "b" * 64}]},
        )
        complete = PublicationObjectCompleteRequest(
            batchId="batch-0001", kind="body_html", sha256="b" * 64
        )
        self.assertEqual(
            complete.model_dump(by_alias=True),
            {"batchId": "batch-0001", "kind": "body_html", "sha256": "b" * 64},
        )

    def test_dates_are_normalized_from_both_legacy_forms(self) -> None:
        self.assertEqual(
            normalize_publication_timestamp("2026-08-20"),
            "2026-08-20T00:00:00+08:00",
        )
        self.assertEqual(
            normalize_publication_timestamp("2026-08-20T09:30:00"),
            "2026-08-20T09:30:00+08:00",
        )
        self.assertEqual(
            normalize_publication_timestamp("2026-08-20T01:30:00Z"),
            "2026-08-20T09:30:00+08:00",
        )

    def test_tombstone_branch_has_no_publication_fields(self) -> None:
        source = PublicationSourceDescriptor(
            id="source",
            name="Source",
            organizationLevel="department",
            allowedHosts=["example.edu"],
            seedUrls=["https://example.edu/"],
        )
        item = {
            "sourceId": "source",
            "canonicalUrl": "https://example.edu/a",
            "revisionHash": "a" * 64,
            "observedAt": "2026-08-20",
            "tombstone": True,
        }
        batch = build_ingestion_batch(
            [IngestionBatch.model_validate({
                "protocolVersion": "1",
                "producerVersion": "test",
                "clientRunId": "run",
                "batchId": "batch",
                "observedAt": "2026-08-20",
                "sources": [source.model_dump(by_alias=True)],
                "items": [item],
            }).items[0]],
            sources=[source],
            client_run_id="run",
            batch_id="batch",
            observed_at="2026-08-20",
            producer_version="test",
        )
        self.assertEqual(batch.payload_dict()["items"], [item | {"observedAt": "2026-08-20T00:00:00+08:00"}])

    def test_publication_builder_bounds_malformed_parser_fields(self) -> None:
        article = ArticleDocument(
            url="https://example.edu/news/oversized",
            source_id="source",
            title="T" * 1_001,
            author="A" * 501,
            published_at="",
            updated_at="",
            category="C" * 501,
            summary="S" * 20_001,
            body_html="",
            body_text="B" * 5_000_001,
            body_markdown="",
            extraction_method="E" * 201,
            source_page_url="https://example.edu/news/oversized",
        )
        objects = [
            ObjectManifest(
                kind="asset",
                sha256=f"{index:064x}",
                size=1,
                contentType="application/octet-stream",
            )
            for index in range(101)
        ]

        publication = build_publication(article, objects=objects)

        self.assertEqual(len(publication.title), 1_000)
        self.assertEqual(len(publication.author or ""), 500)
        self.assertEqual(len(publication.category or ""), 500)
        self.assertEqual(len(publication.summary or ""), 20_000)
        self.assertEqual(len(publication.body_text or ""), 5_000_000)
        self.assertEqual(len(publication.extraction_method or ""), 200)
        self.assertEqual(len(publication.objects), 100)


class OrmAndOutboxTests(unittest.TestCase):
    def _source(self) -> SourceConfig:
        return SourceConfig(
            id="source",
            name="Source",
            organization_level="department",
            seed_urls=["https://example.edu/"],
            allowed_hosts=["example.edu"],
            max_images_per_page=2,
        )

    def _article(self) -> ArticleDocument:
        return ArticleDocument(
            url="https://example.edu/news/1",
            source_id="source",
            title="A notice",
            author="",
            published_at="2026-08-20",
            updated_at="2026-08-20T09:30:00",
            category="通知公告",
            summary="Summary",
            body_html="<p>Hello</p>",
            body_text="Hello",
            body_markdown="Hello",
            extraction_method="article",
            source_page_url="https://example.edu/news/1",
        )

    def test_migration_pragmas_and_uow_rollback(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                with store.database.session_factory() as session:
                    self.assertEqual(session.scalar(text("PRAGMA foreign_keys")), 1)
                    self.assertEqual(session.scalar(text("PRAGMA busy_timeout")), 30000)
                    self.assertEqual(session.scalar(text("PRAGMA journal_mode")), "wal")
                    self.assertEqual(
                        session.scalar(
                            text(
                                "SELECT COUNT(*) FROM sqlite_master "
                                "WHERE type='table' AND name='alembic_version'"
                            )
                        ),
                        1,
                    )
                    self.assertEqual(
                        session.scalar(text("SELECT version_num FROM alembic_version")),
                        ALEMBIC_HEAD,
                    )
                store.add_source(self._source())
                source = store.source_descriptor("source")
                self.assertEqual(source.max_images_per_page, 2)
                with self.assertRaises(RuntimeError):
                    with store.database.session_factory.begin() as session:
                        session.add(SyncRun(id="rollback", started_at="now"))
                        raise RuntimeError("abort")
                with store.database.session_factory() as session:
                    self.assertIsNone(session.get(SyncRun, "rollback"))
            finally:
                store.close()

    def test_spool_and_outbox_are_idempotent_and_batch_is_immutable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                store.start_sync_run("run", mode="full", source_config_revision="c", digest="d")
                article = self._article()
                asset_path = store.save_asset(
                    url="https://example.edu/files/guide.pdf",
                    source_url=article.url,
                    body=b"%PDF-asset",
                    mime_type="application/pdf",
                )
                store.save_asset(
                    url="https://example.edu/files/unrelated.pdf",
                    source_url="https://example.edu/news/other",
                    body=b"%PDF-unrelated",
                    mime_type="application/pdf",
                )
                asset_paths = store.asset_paths_for_article(article.url, article.source_page_url)
                self.assertEqual(set(asset_paths), {"https://example.edu/files/guide.pdf"})
                local = spool_article_objects(
                    article,
                    root / "data",
                    asset_paths=asset_paths,
                )
                asset_objects = [item for item in local if item.kind == "asset"]
                self.assertEqual(len(asset_objects), 1)
                self.assertEqual(asset_objects[0].size, len(b"%PDF-asset"))
                self.assertEqual(Path(asset_objects[0].local_path), asset_path)
                publication = build_publication(
                    article,
                    objects=[wire_manifest(item) for item in local],
                    observed_at=datetime(2026, 8, 20, 10, 0),
                )
                source = store.source_descriptor("source")
                outbox = IngestionOutbox(store.database)
                event_id = outbox.enqueue_publication(
                    publication,
                    source=source,
                    local_objects=local,
                    run_id="run",
                )
                self.assertEqual(
                    event_id,
                    outbox.enqueue_publication(
                        publication,
                        source=source,
                        local_objects=local,
                        run_id="run",
                    ),
                )
                self.assertTrue(Path(local[0].local_path).is_file())
                with store.database.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(SyncOutbox)), 1)
                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                )
                assert batch is not None
                digest = batch.payload_sha256()
                payload = batch.payload_bytes().decode("utf-8")
                self.assertNotIn("local_path", payload)
                self.assertNotIn("uploadUrl", payload)
                self.assertEqual(
                    outbox.build_batch(
                        run_id="run",
                        batch_id="batch",
                        producer_version="other",
                        observed_at="2030-01-01",
                    ).payload_sha256(),
                    digest,
                )
                with store.database.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(SyncBatch)), 1)
                    item = session.scalar(select(SyncBatchItem))
                    self.assertEqual(item.source_id, "source")
            finally:
                store.close()

    def test_discovery_only_source_is_rejected_at_outbox_boundary(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                source_config = self._source()
                source_config.discovery_only = True
                store.add_source(source_config)
                source = store.source_descriptor(source_config.id)
                outbox = IngestionOutbox(store.database)

                with self.assertRaisesRegex(
                    ValueError,
                    "discovery-only source cannot enqueue publications",
                ):
                    outbox.enqueue_article(
                        self._article(),
                        store.data_dir,
                        source=source,
                    )
                with store.database.session_factory() as session:
                    self.assertEqual(session.scalar(select(func.count()).select_from(SyncOutbox)), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
