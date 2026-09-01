"""Complete the typed baseline for databases created by early crawlers."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0003_legacy_crawl_fields"
down_revision: Union[str, Sequence[str], None] = "0002_publication_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_missing(table: str, definitions: dict[str, sa.Column]) -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns(table)}
    for name, column in definitions.items():
        if name not in columns:
            op.add_column(table, column)


def _create_index_if_missing(name: str, table: str, columns: list[object]) -> None:
    names = {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}
    if name not in names:
        op.create_index(name, table, columns)


def upgrade() -> None:
    _add_missing(
        "sources",
        {"blocked_hosts": sa.Column("blocked_hosts", sa.Text(), nullable=False, server_default="[]")},
    )
    _add_missing(
        "frontier",
        {"priority": sa.Column("priority", sa.Integer(), nullable=False, server_default="0")},
    )
    _add_missing(
        "pages",
        {
            "page_kind": sa.Column("page_kind", sa.Text(), nullable=False, server_default="unknown"),
            "access_mode": sa.Column("access_mode", sa.Text(), nullable=False, server_default="unknown"),
            "value_score": sa.Column("value_score", sa.Integer(), nullable=False, server_default="0"),
            "value_tier": sa.Column("value_tier", sa.Text(), nullable=False, server_default="not_indexed"),
            "score_reasons": sa.Column("score_reasons", sa.Text(), nullable=False, server_default="[]"),
            "published_at": sa.Column("published_at", sa.Text()),
            "duplicate_of": sa.Column("duplicate_of", sa.Text()),
        },
    )
    _add_missing(
        "assets",
        {
            "page_kind": sa.Column("page_kind", sa.Text(), nullable=False, server_default="document"),
            "access_mode": sa.Column("access_mode", sa.Text(), nullable=False, server_default="unknown"),
            "value_score": sa.Column("value_score", sa.Integer(), nullable=False, server_default="0"),
            "score_reasons": sa.Column("score_reasons", sa.Text(), nullable=False, server_default="[]"),
        },
    )
    _create_index_if_missing(
        "pages_value_idx",
        "pages",
        [sa.text("value_score DESC"), sa.text("fetched_at")],
    )
    _create_index_if_missing(
        "frontier_priority_idx",
        "frontier",
        ["status", sa.text("priority DESC"), "depth", "discovered_at"],
    )
    _create_index_if_missing(
        "frontier_pending_idx",
        "frontier",
        ["status", sa.text("priority DESC"), "depth", "discovered_at"],
    )


def downgrade() -> None:
    # These columns are part of the public baseline and are intentionally not
    # removed from a populated crawler database on downgrade.
    pass
