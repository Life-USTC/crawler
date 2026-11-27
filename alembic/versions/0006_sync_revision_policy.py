"""Persist the revision policy required to replay an ingestion batch."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0006_sync_revision_policy"
down_revision: str | Sequence[str] | None = "0005_crawl_lookup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("sync_batches")}
    if "revision_policy" not in columns:
        op.add_column(
            "sync_batches",
            sa.Column(
                "revision_policy",
                sa.String(length=32),
                nullable=False,
                server_default="observedAt",
            ),
        )


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("sync_batches")}
    if "revision_policy" in columns:
        op.drop_column("sync_batches", "revision_policy")
