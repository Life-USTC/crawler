import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ustc_crawler.db import upgrade_database
from ustc_crawler.store import Store


def _indexes(db_path: Path) -> dict[str, tuple[str, str | None]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {name: (table, sql) for name, table, sql in rows}
    finally:
        connection.close()


class SchemaIndexTests(unittest.TestCase):
    def test_fresh_database_has_lookup_indexes(self) -> None:
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "crawler.sqlite"
            store = Store(db_path, Path(temp) / "data")
            store.close()
            indexes = _indexes(db_path)

        self.assertIn("pages_sha256_idx", indexes)
        self.assertIn("media_article_url_idx", indexes)
        self.assertIn("article_media_image_idx", indexes)
        self.assertIn("sync_batches_status_idx", indexes)
        self.assertIn("failures_url_idx", indexes)

    def test_frontier_pending_index_is_partial(self) -> None:
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "crawler.sqlite"
            store = Store(db_path, Path(temp) / "data")
            store.close()
            indexes = _indexes(db_path)

        sql = indexes["frontier_pending_idx"][1] or ""
        self.assertIn("WHERE", sql.upper())
        self.assertIn("pending", sql)

    def test_upgrade_is_reentrant_and_converts_legacy_pending_index(self) -> None:
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "crawler.sqlite"
            # Simulate a pre-0005 database: the legacy frontier_pending_idx is
            # a full duplicate of frontier_priority_idx without a WHERE clause.
            store = Store(db_path, Path(temp) / "data")
            store.close()
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("DROP INDEX frontier_pending_idx")
                connection.execute(
                    "CREATE INDEX frontier_pending_idx ON frontier(status, priority DESC, depth, discovered_at)"
                )
                connection.execute(
                    "UPDATE alembic_version SET version_num='0004_sync_snapshots_and_indexes'"
                )
                connection.commit()
            finally:
                connection.close()

            upgrade_database(db_path)
            upgrade_database(db_path)
            indexes = _indexes(db_path)

        sql = indexes["frontier_pending_idx"][1] or ""
        self.assertIn("WHERE", sql.upper())
        self.assertIn("pages_sha256_idx", indexes)

    def test_dedupe_failures_accumulates_attempts_on_surviving_row(self) -> None:
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "crawler.sqlite"
            store = Store(db_path, Path(temp) / "data")
            store.close()
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "INSERT INTO sources(id,name,organization_level,allowed_hosts,blocked_hosts,seed_urls,aliases,discovery_only,max_images_per_page,created_at)"
                    " VALUES('news','n','university','[]','[]','[]','[]',0,NULL,'2026-01-01')"
                )
                connection.execute("DROP INDEX failures_url_idx")
                # Legacy pre-0005 history: one row per failure occurrence.
                for attempts, error in ((2, "http 503"), (3, "http 500")):
                    connection.execute(
                        "INSERT INTO failures(url,source_id,error,status,attempts,last_seen)"
                        " VALUES('https://example.test/x','news',?,500,?,'2026-01-01')",
                        (error, attempts),
                    )
                connection.execute(
                    "UPDATE alembic_version SET version_num='0004_sync_snapshots_and_indexes'"
                )
                connection.commit()
            finally:
                connection.close()

            upgrade_database(db_path)
            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT url, error, attempts FROM failures"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "http 500")
        self.assertEqual(rows[0][2], 5)


if __name__ == "__main__":
    unittest.main()
