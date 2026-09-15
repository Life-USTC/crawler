import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError
from sqlalchemy import func, select, text

from ustc_crawler.db import ALEMBIC_HEAD
from ustc_crawler.db.models import Article, SyncBatch, SyncBatchItem, SyncOutbox, SyncRun
from ustc_crawler.models import ArticleDocument, SourceConfig
from ustc_crawler.store import Store, article_bundle_path
from ustc_crawler.sync.models import (
    IngestionBatch,
    IngestionPublication,
    ObjectManifest,
    PublicationObjectPlanRequest,
    PublicationSourceDescriptor,
    TombstonePublication,
    build_ingestion_batch,
    build_publication,
    normalize_publication_timestamp,
    revision_hash_for_article,
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
            {
                "protocolVersion",
                "producerVersion",
                "clientRunId",
                "batchId",
                "observedAt",
                "sources",
                "items",
            },
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
        with self.assertRaises(ValidationError):
            PublicationObjectPlanRequest(
                batchId="batch-0001",
                objects=[{"kind": "body_html", "sha256": "b" * 64}] * 101,
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
            [
                IngestionBatch.model_validate(
                    {
                        "protocolVersion": "1",
                        "producerVersion": "test",
                        "clientRunId": "run",
                        "batchId": "batch",
                        "observedAt": "2026-08-20",
                        "sources": [source.model_dump(by_alias=True)],
                        "items": [item],
                    }
                ).items[0]
            ],
            sources=[source],
            client_run_id="run",
            batch_id="batch",
            observed_at="2026-08-20",
            producer_version="test",
        )
        self.assertEqual(
            batch.payload_dict()["items"], [item | {"observedAt": "2026-08-20T00:00:00+08:00"}]
        )

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

    def test_publication_builder_accepts_an_image_only_article_body(self) -> None:
        article = ArticleDocument(
            url="https://www.ustc.edu.cn/info/1029/25470.htm",
            source_id="university",
            title="校园班车运行时刻表（2026年8月30日试运行）",
            author="",
            published_at="",
            updated_at="",
            category="",
            summary="",
            body_html="<div><img src='/timetable.jpg'></div>",
            body_text="",
            body_markdown="",
            extraction_method="html",
            source_page_url="https://www.ustc.edu.cn/info/1029/25470.htm",
        )

        publication = build_publication(article)

        self.assertIsNone(publication.body_text)

    def test_publication_wire_normalization_is_idempotent_for_server_trim(self) -> None:
        title = "T" * 999 + " " + "truncated after the protocol bound"
        article = ArticleDocument(
            url=" https://example.edu/news/whitespace ",
            source_id=" source ",
            title=title,
            author="\t Author \n",
            published_at=" 2026-08-20 ",
            updated_at=" 2026-08-20T09:30:00 ",
            category=" Category ",
            summary=" Summary ",
            body_html="",
            body_text=" body whitespace is significant ",
            body_markdown="",
            extraction_method=" extractor ",
            source_page_url=" https://example.edu/news/whitespace ",
        )
        manifest = ObjectManifest(
            kind="media",
            sha256="a" * 64,
            size=1,
            contentType=" image/png ",
            altText=" image description ",
        )
        publication = build_publication(
            article,
            objects=[manifest],
            publication_type="notice",
            classifier_version=" classifier/v1 ",
            observed_at="2026-08-20",
        )
        source = PublicationSourceDescriptor(
            id=" source ",
            name=" Source name ",
            organizationLevel=" department ",
            allowedHosts=[" example.edu "],
            blockedHosts=[" blocked.example.edu "],
            seedUrls=[" https://example.edu/ "],
            aliases=[" alias "],
        )
        batch = build_ingestion_batch(
            [publication],
            sources=[source],
            client_run_id=" client-run ",
            batch_id=" batch-id ",
            observed_at="2026-08-20",
            producer_version=" crawler/v1 ",
        )

        payload = batch.payload_dict()
        self.assertEqual(payload["producerVersion"], "crawler/v1")
        self.assertEqual(payload["clientRunId"], "client-run")
        self.assertEqual(payload["batchId"], "batch-id")
        self.assertEqual(payload["sources"][0]["name"], "Source name")
        self.assertEqual(payload["sources"][0]["allowedHosts"], ["example.edu"])
        item = payload["items"][0]
        self.assertEqual(item["sourceId"], "source")
        self.assertEqual(item["canonicalUrl"], "https://example.edu/news/whitespace")
        self.assertEqual(item["title"], "T" * 999)
        self.assertEqual(item["author"], "Author")
        self.assertEqual(item["bodyText"], " body whitespace is significant ")
        self.assertEqual(item["objects"][0]["contentType"], "image/png")
        self.assertEqual(item["objects"][0]["altText"], "image description")

        # Applying the same string transforms a second time must not alter
        # the bytes whose SHA-256 is sent as the batch payload digest.
        reparsed = IngestionBatch.model_validate(payload)
        self.assertEqual(reparsed.payload_dict(), payload)
        self.assertEqual(reparsed.payload_bytes(), batch.payload_bytes())
        self.assertEqual(
            publication.revision_hash,
            revision_hash_for_article(
                article,
                publication_type="notice",
                classifier_version=" classifier/v1 ",
                objects=[manifest],
            ),
        )

    def test_publication_builder_rejects_blank_title_after_trim(self) -> None:
        article = ArticleDocument(
            url="https://example.edu/news/blank",
            source_id="source",
            title=" \t\n ",
            author="",
            published_at="",
            updated_at="",
            category="",
            summary="",
            body_html="",
            body_text="",
            body_markdown="",
            extraction_method="",
            source_page_url="https://example.edu/news/blank",
        )
        with self.assertRaisesRegex(ValueError, "must not be blank"):
            build_publication(article)

        with self.assertRaises(ValidationError):
            IngestionPublication.model_validate(
                {
                    "sourceId": "source",
                    "canonicalUrl": "https://example.edu/news/blank",
                    "revisionHash": "a" * 64,
                    "observedAt": "2026-08-20",
                    "publicationType": "notice",
                    "title": " \t\n ",
                }
            )

    def test_publication_removes_control_characters_without_losing_layout(self) -> None:
        article = ArticleDocument(
            url="http://scc.ustc.edu.cn/2021/1215/c398a539154/page.htm",
            source_id="source",
            title="Supercomputing title\x00",
            author="Author\x01 entry",
            published_at="2026-08-20",
            updated_at="",
            category="Category\x02",
            summary="Summary\x03",
            body_html="<p>Body\x00</p>",
            body_text="Body\x00\n\twith layout",
            body_markdown="Body\x00\n\twith layout",
            extraction_method="extractor\x04",
            source_page_url="http://scc.ustc.edu.cn/2021/1215/c398a539154/page.htm",
            raw_metadata={"language": "zh\x05-CN", "authors": ["A\x06"]},
        )

        self.assertEqual(article.title, "Supercomputing title")
        self.assertEqual(article.body_text, "Body\n\twith layout")
        self.assertEqual(article.raw_metadata, {"language": "zh-CN", "authors": ["A"]})
        publication = build_publication(article, publication_type="news")
        payload = publication.model_dump(by_alias=True, mode="json", exclude_none=True)
        self.assertNotIn("\x00", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["bodyText"], "Body\n\twith layout")

        with TemporaryDirectory() as temp:
            objects = spool_article_objects(article, Path(temp) / "data")
            body_bytes = [
                Path(manifest.local_path).read_bytes()
                for manifest in objects
                if manifest.kind in {"body_html", "body_markdown"}
            ]
            self.assertTrue(body_bytes)
            self.assertTrue(all(b"\x00" not in value for value in body_bytes))

    def test_publication_removes_byte_order_marks_before_digest(self) -> None:
        article = ArticleDocument(
            url="https://news.ustc.edu.cn/info/1049/95606.htm",
            source_id="news",
            title="\ufeffTitle with a leading byte-order mark",
            author="",
            published_at="2026-07-03",
            updated_at="",
            category="",
            summary="Summary\ufeffwith an embedded byte-order mark",
            body_html="<p>Body\ufefftext</p>",
            body_text="Body\ufefftext",
            body_markdown="Body\ufefftext",
            extraction_method="generic",
            source_page_url="https://news.ustc.edu.cn/info/1049/95606.htm",
            raw_metadata={"\ufefflanguage": "\ufeffzh-CN"},
        )

        self.assertEqual(article.title, "Title with a leading byte-order mark")
        self.assertEqual(article.summary, "Summarywith an embedded byte-order mark")
        self.assertEqual(article.body_text, "Bodytext")
        self.assertEqual(article.raw_metadata, {"language": "zh-CN"})
        publication = build_publication(article, publication_type="news")
        payload = publication.model_dump(by_alias=True, mode="json", exclude_none=True)
        self.assertNotIn("\ufeff", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(IngestionPublication.model_validate(payload), publication)

    def test_ingestion_batch_rejects_more_than_one_hundred_items(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["items"] = payload["items"] * 101

        with self.assertRaises(ValidationError):
            IngestionBatch.model_validate(payload)


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

    def test_save_article_sanitizes_controls_before_db_and_bundle(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            store.add_source(self._source())
            try:
                article = self._article()
                article.url = "http://scc.ustc.edu.cn/2021/1215/c398a539154/page.htm"
                article.source_page_url = article.url
                article.title = "Supercomputing title\x00"
                article.body_html = "<p>Body\x01</p>"
                article.body_text = "Body\x00\n\twith layout"
                article.body_markdown = "Body\x02\n\twith layout"
                article.raw_metadata = {"language": "zh\x03-CN", "authors": ["A\x04"]}

                store.save_article(article)
                with store.database.session_factory() as session:
                    row = session.get(Article, article.url)
                    self.assertIsNotNone(row)
                    self.assertEqual(row.title, "Supercomputing title")
                    self.assertEqual(row.body_text, "Body\n\twith layout")
                    self.assertEqual(row.body_html, "<p>Body</p>")
                    self.assertNotIn("\x00", row.raw_json or "")

                bundle = json.loads(
                    article_bundle_path(store.data_dir, article.url).read_text(encoding="utf-8")
                )
                self.assertEqual(bundle["body_text"], "Body\n\twith layout")
                self.assertEqual(bundle["raw_metadata"], {"language": "zh-CN", "authors": ["A"]})
            finally:
                store.close()

    def _insert_oversized_batch(
        self,
        store: Store,
        batch_id: str,
        *,
        batch_status: str = "failed",
        item_status: str = "failed",
        outbox_status: str = "failed",
        count: int = 101,
        url_prefix: str = "https://example.edu/news/",
    ) -> tuple[str, str]:
        source = store.source_descriptor("source")
        for number in range(count):
            article = self._article()
            article.url = f"{url_prefix}{number}"
            article.title = f"A notice {number}"
            article.body_html = f"<p>Hello {number}</p>"
            article.body_text = f"Hello {number}"
            article.body_markdown = f"Hello {number}"
            store.save_article_and_enqueue_for_sync(article)

        digest = "a" * 64
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
                    payload_sha256=digest,
                    protocol_version="1",
                    producer_version="old-producer",
                    status=batch_status,
                    attempts=4,
                    next_attempt_at="2026-08-20T00:01:00+08:00",
                    locked_until="2026-08-20T00:02:00+08:00",
                    response_json="old-response",
                    last_error="old-error",
                    created_at="2026-08-20T00:00:00+08:00",
                    updated_at="2026-08-20T00:00:00+08:00",
                )
            )
            for row in rows:
                publication = json.loads(row.payload_json)
                row.batch_id = batch_id
                row.status = outbox_status
                row.attempts = 4
                row.next_attempt_at = "2026-08-20T00:01:00+08:00"
                row.locked_until = "2026-08-20T00:02:00+08:00"
                row.response_json = "old-response"
                row.last_error = "old-error"
                session.add(
                    SyncBatchItem(
                        batch_id=batch_id,
                        item_key=row.event_id,
                        source_id=publication["sourceId"],
                        canonical_url=publication["canonicalUrl"],
                        revision_hash=publication["revisionHash"],
                        status=item_status,
                        error="old-item-error",
                    )
                )
        return digest, source.id

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
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(SyncOutbox)), 1
                    )
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

    def test_tombstone_outbox_event_builds_and_replays_without_objects(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                source = store.source_descriptor("source")
                tombstone = TombstonePublication(
                    sourceId="source",
                    canonicalUrl="https://example.edu/news/removed",
                    revisionHash="b" * 64,
                    observedAt="2026-08-20",
                )
                outbox = IngestionOutbox(store.database)
                event_id = outbox.enqueue_publication(tombstone, source=source)

                with store.database.session_factory() as session:
                    row = session.get(SyncOutbox, event_id)
                    assert row is not None
                    self.assertEqual(
                        json.loads(row.payload_json),
                        {
                            "canonicalUrl": "https://example.edu/news/removed",
                            "observedAt": "2026-08-20T00:00:00+08:00",
                            "revisionHash": "b" * 64,
                            "sourceId": "source",
                            "tombstone": True,
                        },
                    )
                    self.assertEqual(json.loads(row.object_manifest_json), [])

                batch = outbox.build_batch(
                    run_id="run",
                    batch_id="tombstone-batch",
                    producer_version="test",
                    observed_at="2026-08-20",
                )
                assert batch is not None
                self.assertIsInstance(batch.items[0], TombstonePublication)
                self.assertEqual(
                    batch.payload_dict()["items"],
                    [tombstone.model_dump(by_alias=True, mode="json")],
                )

                replayed = outbox.build_batch(
                    run_id="different-run",
                    batch_id="tombstone-batch",
                    producer_version="different-producer",
                    observed_at="2030-01-01",
                )
                assert replayed is not None
                self.assertIsInstance(replayed.items[0], TombstonePublication)
                self.assertEqual(replayed.payload_bytes(), batch.payload_bytes())
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
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(SyncOutbox)), 0
                    )
            finally:
                store.close()

    def test_recover_oversized_batch_preserves_audit_and_releases_outbox(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                digest, _ = self._insert_oversized_batch(store, "oversized")
                outbox = IngestionOutbox(store.database)

                self.assertEqual(outbox.recover_oversized_batch("oversized"), 101)
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "oversized")
                    items = session.scalars(
                        select(SyncBatchItem).where(SyncBatchItem.batch_id == "oversized")
                    ).all()
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual(batch.status, "superseded")
                    self.assertEqual(batch.last_error, "oversized_batch_superseded")
                    self.assertEqual(batch.payload_sha256, digest)
                    self.assertEqual(batch.response_json, "old-response")
                    self.assertIsNone(batch.next_attempt_at)
                    self.assertIsNone(batch.locked_until)
                    self.assertEqual(len(items), 101)
                    self.assertTrue(all(item.error == "old-item-error" for item in items))
                    self.assertTrue(all(row.batch_id is None for row in rows))
                    self.assertTrue(all(row.status == "pending" for row in rows))
                    self.assertTrue(all(row.next_attempt_at is None for row in rows))
                    self.assertTrue(all(row.locked_until is None for row in rows))
                    self.assertTrue(all(row.response_json is None for row in rows))
                    self.assertTrue(all(row.last_error is None for row in rows))
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(Article)),
                        101,
                    )

                with self.assertRaisesRegex(ValueError, "batch status cannot be recovered"):
                    outbox.recover_oversized_batch("oversized")
                rebuilt = outbox.build_batch(
                    run_id="new-run",
                    batch_id="replacement",
                    producer_version="new-producer",
                    observed_at="2026-08-20T00:00:00+08:00",
                    limit=100,
                )
                self.assertIsNotNone(rebuilt)
                self.assertEqual(len(rebuilt.items), 100)
            finally:
                store.close()

    def test_recover_oversized_batch_requires_exact_membership(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                self._insert_oversized_batch(store, "mismatch")
                with store.database.session_factory.begin() as session:
                    item = session.scalar(select(SyncBatchItem))
                    item.item_key = "missing-event"
                outbox = IngestionOutbox(store.database)
                with self.assertRaisesRegex(ValueError, "membership mismatch"):
                    outbox.recover_oversized_batch("mismatch")
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "mismatch")
                    row = session.scalar(select(SyncOutbox))
                    self.assertEqual(batch.status, "failed")
                    self.assertEqual(row.batch_id, "mismatch")
                    self.assertEqual(row.status, "failed")
            finally:
                store.close()

    def test_recover_oversized_batch_accepts_pending_and_uploading(self) -> None:
        cases = (
            ("pending", "pending", "batched"),
            ("uploading", "uploading", "uploading"),
        )
        for batch_status, item_status, outbox_status in cases:
            with self.subTest(batch_status=batch_status), TemporaryDirectory() as temp:
                root = Path(temp)
                store = Store(root / "crawler.sqlite", root / "data")
                try:
                    store.add_source(self._source())
                    self._insert_oversized_batch(
                        store,
                        batch_status,
                        batch_status=batch_status,
                        item_status=item_status,
                        outbox_status=outbox_status,
                    )
                    released = IngestionOutbox(store.database).recover_oversized_batch(batch_status)
                    self.assertEqual(released, 101)
                finally:
                    store.close()

    def test_recover_oversized_batch_refuses_completed_statuses(self) -> None:
        for status in ("acked", "partial", "success"):
            with self.subTest(status=status), TemporaryDirectory() as temp:
                root = Path(temp)
                store = Store(root / "crawler.sqlite", root / "data")
                try:
                    store.add_source(self._source())
                    self._insert_oversized_batch(
                        store,
                        status,
                        batch_status=status,
                        item_status="failed",
                        outbox_status="failed",
                    )
                    with self.assertRaisesRegex(ValueError, "batch status cannot be recovered"):
                        IngestionOutbox(store.database).recover_oversized_batch(status)
                    with store.database.session_factory() as session:
                        batch = session.get(SyncBatch, status)
                        row = session.scalar(select(SyncOutbox))
                        self.assertEqual(batch.status, status)
                        self.assertEqual(row.batch_id, status)
                        self.assertEqual(row.status, "failed")
                finally:
                    store.close()
    def test_requeue_failed_batches_releases_events_for_matching_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                self._insert_oversized_batch(store, "local-failure", count=50)
                self._insert_oversized_batch(
                    store, "server-rejected", count=2, url_prefix="https://example.edu/notice/"
                )
                with store.database.session_factory.begin() as session:
                    # The helper links every outbox row to the newest batch;
                    # restore each batch's own members by canonical URL prefix.
                    first_keys = set()
                    for row in session.scalars(select(SyncOutbox)).all():
                        if '"canonicalUrl":"https://example.edu/news/' in row.payload_json:
                            row.batch_id = "local-failure"
                            first_keys.add(row.event_id)
                    self.assertEqual(len(first_keys), 50)
                    for item in session.scalars(
                        select(SyncBatchItem).where(SyncBatchItem.batch_id == "server-rejected")
                    ):
                        if item.item_key in first_keys:
                            session.delete(item)
                    session.get(SyncBatch, "local-failure").last_error = "immutable_object_changed"
                    rejected = session.get(SyncBatch, "server-rejected")
                    rejected.last_error = "server_rejected"
                    rejected.status = "failed"

                outbox = IngestionOutbox(store.database)
                result = outbox.requeue_failed_batches(errors={"immutable_object_changed"})
                self.assertEqual(result, {"batches": 1, "events": 50, "skipped": 0})

                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "local-failure")
                    self.assertEqual(batch.status, "superseded")
                    self.assertEqual(
                        batch.last_error, "requeued_failed_batch:immutable_object_changed"
                    )
                    rejected_batch = session.get(SyncBatch, "server-rejected")
                    self.assertEqual(rejected_batch.status, "failed")
                    self.assertEqual(rejected_batch.last_error, "server_rejected")
                    rows = session.scalars(select(SyncOutbox)).all()
                    requeued = [row for row in rows if row.status == "pending"]
                    still_failed = [row for row in rows if row.status == "failed"]
                    self.assertEqual(len(requeued), 50)
                    self.assertEqual(len(still_failed), 2)
                    self.assertTrue(all(row.batch_id is None for row in requeued))
                    self.assertTrue(all(row.last_error is None for row in requeued))
                    self.assertTrue(all(row.response_json is None for row in requeued))
                    self.assertTrue(
                        all(row.batch_id == "server-rejected" for row in still_failed)
                    )

                rebuilt = outbox.build_batch(
                    run_id="new-run",
                    batch_id="replacement",
                    producer_version="new-producer",
                    observed_at="2026-08-20T00:00:00+08:00",
                    limit=50,
                )
                self.assertIsNotNone(rebuilt)
                self.assertEqual(len(rebuilt.items), 50)
            finally:
                store.close()

    def test_requeue_failed_batches_refuses_mixed_membership(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                self._insert_oversized_batch(store, "mismatch", count=50)
                with store.database.session_factory.begin() as session:
                    batch = session.get(SyncBatch, "mismatch")
                    batch.last_error = "immutable_object_changed"
                    session.scalars(
                        select(SyncBatchItem).where(SyncBatchItem.batch_id == "mismatch")
                    ).first().status = "acked"
                outbox = IngestionOutbox(store.database)
                with self.assertRaisesRegex(ValueError, "completed or rejected items"):
                    outbox.requeue_failed_batches(errors={"immutable_object_changed"})
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "mismatch")
                    self.assertEqual(batch.status, "failed")
            finally:
                store.close()

    def test_requeue_failed_batches_retires_already_redelivered_batches(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "crawler.sqlite", root / "data")
            try:
                store.add_source(self._source())
                self._insert_oversized_batch(store, "stale", count=2)
                source = store.source_descriptor("source")
                with store.database.session_factory.begin() as session:
                    # An earlier recovery already moved both events into a
                    # newer batch that has since been acked; only the stale
                    # batch's item rows still reference them.
                    session.add(
                        SyncBatch(
                            id="newer",
                            run_id=None,
                            client_run_id="new-run",
                            sources_json=json.dumps(
                                [source.model_dump(by_alias=True, mode="json")],
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            observed_at="2026-08-21T00:00:00+08:00",
                            payload_sha256="b" * 64,
                            protocol_version="1",
                            producer_version="new-producer",
                            status="acked",
                            attempts=1,
                            created_at="2026-08-21T00:00:00+08:00",
                            updated_at="2026-08-21T00:00:00+08:00",
                        )
                    )
                    for row in session.scalars(select(SyncOutbox)).all():
                        row.batch_id = "newer"
                        row.status = "acked"
                    stale = session.get(SyncBatch, "stale")
                    stale.last_error = "batch_rebuild_error"

                outbox = IngestionOutbox(store.database)
                result = outbox.requeue_failed_batches(errors={"batch_rebuild_error"})
                self.assertEqual(result, {"batches": 0, "events": 0, "skipped": 1})
                with store.database.session_factory() as session:
                    batch = session.get(SyncBatch, "stale")
                    self.assertEqual(batch.status, "superseded")
                    self.assertEqual(batch.last_error, "batch_rebuild_error")
                    rows = session.scalars(select(SyncOutbox)).all()
                    self.assertEqual({row.status for row in rows}, {"acked"})
                    self.assertEqual({row.batch_id for row in rows}, {"newer"})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
