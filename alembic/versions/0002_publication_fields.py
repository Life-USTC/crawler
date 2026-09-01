"""Add the persisted publication classifier fields."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002_publication_fields"
down_revision: Union[str, Sequence[str], None] = "0001_crawler_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("articles")}
    if "publication_type" not in columns:
        op.add_column(
            "articles",
            sa.Column("publication_type", sa.Text(), nullable=False, server_default=""),
        )
    if "classifier_version" not in columns:
        op.add_column(
            "articles",
            sa.Column("classifier_version", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("articles")}
    if "classifier_version" in columns:
        op.drop_column("articles", "classifier_version")
    if "publication_type" in columns:
        op.drop_column("articles", "publication_type")
