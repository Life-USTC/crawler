"""Persist immutable source and batch snapshots for the sync outbox."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0004_sync_snapshots_and_indexes"
down_revision: Union[str, Sequence[str], None] = "0003_legacy_crawl_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_missing(table: str, definitions: dict[str, sa.Column]) -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns(table)}
    for name, column in definitions.items():
        if name not in columns:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing(
        "sources",
        {"max_images_per_page": sa.Column("max_images_per_page", sa.Integer())},
    )
    _add_missing(
        "sync_batches",
        {
            "client_run_id": sa.Column("client_run_id", sa.String(200), nullable=False, server_default=""),
            "sources_json": sa.Column("sources_json", sa.Text(), nullable=False, server_default="[]"),
            "observed_at": sa.Column("observed_at", sa.Text(), nullable=False, server_default=""),
        },
    )
    _add_missing(
        "sync_outbox",
        {"source_json": sa.Column("source_json", sa.Text(), nullable=False, server_default="{}")},
    )


def downgrade() -> None:
    # The columns are part of the durable outbox contract.  Keep them on
    # downgrade so a rollback cannot silently destroy immutable event data.
    pass
