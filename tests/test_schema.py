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


if __name__ == "__main__":
    unittest.main()
