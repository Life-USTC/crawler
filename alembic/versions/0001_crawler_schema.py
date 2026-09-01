"""Create the crawler and ingestion outbox schema.

The crawler's original schema is intentionally represented by the same typed
metadata as the runtime.  This revision is the baseline for new databases;
the following revision adds fields introduced after the first public crawler
database without requiring a destructive rebuild.
"""

from typing import Sequence, Union

from alembic import op

from ustc_crawler.db.models import Base

revision: str = "0001_crawler_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    # This baseline may be applied to an existing crawler database.  A
    # downgrade must never erase the crawl archive or immutable outbox.
    pass
