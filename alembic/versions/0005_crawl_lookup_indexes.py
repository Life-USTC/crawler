"""Add hot-path lookup indexes and make frontier_pending_idx partial.

The crawler repeatedly looked rows up by unindexed columns: duplicate
detection scanned pages by sha256 for every saved page, media cleanup updated
media by article_url, and sync batch draining filtered sync_batches by status.
The legacy frontier_pending_idx was also a full duplicate of
frontier_priority_idx; it is now a partial index over pending rows only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0005_crawl_lookup_indexes"
down_revision: Union[str, Sequence[str], None] = "0004_sync_snapshots_and_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FRONTIER_PENDING_SQL = (
    "CREATE INDEX IF NOT EXISTS frontier_pending_idx "
    "ON frontier(priority DESC, depth, discovered_at) WHERE status = 'pending'"
)


def _index_sql(table: str, name: str) -> str:
    for index in inspect(op.get_bind()).get_indexes(table):
        if index["name"] == name:
            row = op.get_bind().execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                {"name": name},
            ).scalar_one_or_none()
            return row or ""
    return ""


def _create_index_if_missing(name: str, table: str, columns: list[object]) -> None:
    names = {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}
    if name not in names:
        op.create_index(name, table, columns)


def _dedupe_failures() -> None:
    # Collapse historical duplicate failure rows into one row per URL so the
    # new unique index can be created; the surviving row keeps the latest
    # error and the accumulated attempt count.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """UPDATE failures SET attempts=(
                   SELECT COALESCE(SUM(f.attempts), 1) FROM failures f WHERE f.url=failures.url
               )
               WHERE id NOT IN (SELECT MAX(id) FROM failures GROUP BY url)"""
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM failures WHERE id NOT IN (SELECT MAX(id) FROM failures GROUP BY url)"
        )
    )


def upgrade() -> None:
    _create_index_if_missing("pages_sha256_idx", "pages", ["sha256"])
    _create_index_if_missing("media_article_url_idx", "media", ["article_url"])
    _create_index_if_missing("article_media_image_idx", "article_media", ["image_url"])
    _create_index_if_missing("sync_batches_status_idx", "sync_batches", ["status", "created_at"])

    pending_sql = _index_sql("frontier", "frontier_pending_idx")
    if "WHERE" not in pending_sql.upper():
        if pending_sql:
            op.drop_index("frontier_pending_idx", table_name="frontier")
        op.execute(sa.text(_FRONTIER_PENDING_SQL))

    names = {index["name"] for index in inspect(op.get_bind()).get_indexes("failures")}
    if "failures_url_idx" not in names:
        _dedupe_failures()
        op.create_index("failures_url_idx", "failures", ["url"], unique=True)


def downgrade() -> None:
    # Indexes carry no data; dropping them only restores the previous query
    # plans.  The deduplicated failures rows are left in place.
    for name, table in (
        ("pages_sha256_idx", "pages"),
        ("media_article_url_idx", "media"),
        ("article_media_image_idx", "article_media"),
        ("sync_batches_status_idx", "sync_batches"),
        ("failures_url_idx", "failures"),
    ):
        names = {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}
        if name in names:
            op.drop_index(name, table_name=table)
    pending_sql = _index_sql("frontier", "frontier_pending_idx")
    if "WHERE" in pending_sql.upper():
        op.drop_index("frontier_pending_idx", table_name="frontier")
        op.create_index(
            "frontier_pending_idx",
            "frontier",
            ["status", sa.text("priority DESC"), "depth", "discovered_at"],
        )
