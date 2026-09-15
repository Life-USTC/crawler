"""Fresh-first sync: newest pending events batch first; stale duplicates coalesce."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select, update

from ustc_crawler.db.models import SyncOutbox
from ustc_crawler.models import ArticleDocument, SourceConfig
from ustc_crawler.store import Store
from ustc_crawler.sync.models import TombstonePublication
from ustc_crawler.sync.outbox import IngestionOutbox


class FreshFirstTests(unittest.TestCase):
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
    def _article(number: int, *, suffix: str = "") -> ArticleDocument:
        url = f"https://example.edu/news/{number}"
        return ArticleDocument(
            url=url,
            source_id="source",
            title=f"Notice {number}{suffix}",
            author="",
            published_at="2026-08-20",
            updated_at="2026-08-20T09:30:00",
            category="通知公告",
            summary="Summary",
            body_html=f"<p>HTML {number}{suffix}</p>",
            body_text=f"Text {number}{suffix}",
            body_markdown=f"Markdown {number}{suffix}",
            extraction_method="article",
            source_page_url=url,
        )

    def _store(self, root: Path) -> Store:
        store = Store(root / "crawler.sqlite", root / "data")
        store.add_source(self._source())
        return store

    @staticmethod
    def _set_created_at(store: Store, entity_key: str, created_at: str) -> None:
        with store.database.session_factory.begin() as session:
            session.execute(
                update(SyncOutbox)
                .where(SyncOutbox.entity_key == entity_key)
                .values(created_at=created_at)
            )

    def _build(self, outbox: IngestionOutbox, batch_id: str, *, limit: int = 50):
        return outbox.build_batch(
            run_id="run",
            batch_id=batch_id,
            producer_version="test",
            observed_at="2026-08-20",
            limit=limit,
        )

    def test_newest_events_are_batched_first_within_limit(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                for number in (1, 2, 3):
                    store.enqueue_article_for_sync(self._article(number))
                # Deterministic ages: article 1 is oldest, article 3 newest.
                for number, created_at in (
                    (1, "2026-08-20T00:00:01+08:00"),
                    (2, "2026-08-20T00:00:02+08:00"),
                    (3, "2026-08-20T00:00:03+08:00"),
                ):
                    self._set_created_at(store, f"source:https://example.edu/news/{number}", created_at)
                outbox = IngestionOutbox(store.database)

                first = self._build(outbox, "batch-1", limit=2)

                self.assertIsNotNone(first)
                self.assertEqual(
                    [item.canonical_url for item in first.items],
                    [
                        "https://example.edu/news/3",
                        "https://example.edu/news/2",
                    ],
                )
                second = self._build(outbox, "batch-2", limit=2)
                self.assertIsNotNone(second)
                self.assertEqual(
                    [item.canonical_url for item in second.items],
                    ["https://example.edu/news/1"],
                )
            finally:
                store.close()

    def test_same_identity_keeps_only_newest_pending_event(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                store.enqueue_article_for_sync(self._article(1, suffix=" v2"))
                key = "source:https://example.edu/news/1"
                with store.database.session_factory() as session:
                    rows = session.scalars(
                        select(SyncOutbox).where(SyncOutbox.entity_key == key)
                    ).all()
                    self.assertEqual(len(rows), 2)
                    by_title = {
                        json.loads(row.payload_json)["title"]: row for row in rows
                    }
                    stale = by_title["Notice 1"]
                    fresh = by_title["Notice 1 v2"]
                    stale_id, stale_sha = stale.event_id, stale.payload_sha256
                with store.database.session_factory.begin() as session:
                    session.get(SyncOutbox, stale_id).created_at = "2026-08-20T00:00:00+08:00"
                    session.get(SyncOutbox, fresh.event_id).created_at = (
                        "2026-08-20T00:00:01+08:00"
                    )
                outbox = IngestionOutbox(store.database)

                batch = self._build(outbox, "batch-1")

                self.assertIsNotNone(batch)
                self.assertEqual(len(batch.items), 1)
                self.assertEqual(batch.items[0].title, "Notice 1 v2")
                with store.database.session_factory() as session:
                    stale = session.get(SyncOutbox, stale_id)
                    self.assertEqual(stale.status, "superseded")
                    self.assertEqual(stale.last_error, "coalesced_by_newer")
                    self.assertIsNone(stale.batch_id)
                    # Immutability: supersede touches status metadata only.
                    self.assertEqual(stale.payload_sha256, stale_sha)
                # Nothing left to batch.
                self.assertIsNone(self._build(outbox, "batch-2"))
            finally:
                store.close()

    def test_tombstone_newest_is_not_coalesced_away(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                key = "source:https://example.edu/news/1"
                self._set_created_at(store, key, "2026-08-20T00:00:01+08:00")
                source = store.source_descriptor("source")
                outbox = IngestionOutbox(store.database)
                outbox.enqueue_publication(
                    TombstonePublication(
                        sourceId="source",
                        canonicalUrl="https://example.edu/news/1",
                        revisionHash="b" * 64,
                        observedAt="2026-08-21",
                    ),
                    source=source,
                    created_at="2026-08-21T00:00:02+08:00",
                )

                batch = self._build(outbox, "batch-1")

                self.assertIsNotNone(batch)
                self.assertEqual(len(batch.items), 1)
                self.assertIsInstance(batch.items[0], TombstonePublication)
                with store.database.session_factory() as session:
                    rows = session.scalars(
                        select(SyncOutbox).where(SyncOutbox.entity_key == key)
                    ).all()
                    tombstone_row = next(
                        row for row in rows if row.revision_hash == "b" * 64
                    )
                    body_row = next(row for row in rows if row is not tombstone_row)
                    self.assertEqual(tombstone_row.status, "batched")
                    self.assertEqual(body_row.status, "superseded")
                    self.assertEqual(body_row.last_error, "coalesced_by_newer")
            finally:
                store.close()

    def test_older_tombstone_yields_to_newer_body_event(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                source = store.source_descriptor("source")
                outbox = IngestionOutbox(store.database)
                outbox.enqueue_publication(
                    TombstonePublication(
                        sourceId="source",
                        canonicalUrl="https://example.edu/news/1",
                        revisionHash="b" * 64,
                        observedAt="2026-08-20",
                    ),
                    source=source,
                    created_at="2026-08-20T00:00:01+08:00",
                )
                store.enqueue_article_for_sync(self._article(1))
                key = "source:https://example.edu/news/1"
                with store.database.session_factory.begin() as session:
                    body_row = session.scalars(
                        select(SyncOutbox).where(
                            SyncOutbox.entity_key == key,
                            SyncOutbox.revision_hash != "b" * 64,
                        )
                    ).one()
                    body_row.created_at = "2026-08-20T00:00:02+08:00"

                batch = self._build(outbox, "batch-1")

                self.assertIsNotNone(batch)
                self.assertEqual(len(batch.items), 1)
                self.assertNotIsInstance(batch.items[0], TombstonePublication)
                with store.database.session_factory() as session:
                    tombstone_row = session.scalars(
                        select(SyncOutbox).where(SyncOutbox.revision_hash == "b" * 64)
                    ).one()
                    self.assertEqual(tombstone_row.status, "superseded")
                    self.assertEqual(tombstone_row.last_error, "coalesced_by_newer")
            finally:
                store.close()

    def test_replay_path_rebuilds_exact_batch_after_fresh_first_claim(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            try:
                store.enqueue_article_for_sync(self._article(1))
                store.enqueue_article_for_sync(self._article(2))
                outbox = IngestionOutbox(store.database)
                batch = self._build(outbox, "batch-1")
                self.assertIsNotNone(batch)

                replayed = outbox.build_batch(
                    run_id="other-run",
                    batch_id="batch-1",
                    producer_version="other-producer",
                    observed_at="2030-01-01",
                )

                self.assertIsNotNone(replayed)
                self.assertEqual(replayed.payload_sha256(), batch.payload_sha256())
                self.assertEqual(replayed.payload_bytes(), batch.payload_bytes())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
