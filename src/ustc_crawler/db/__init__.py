"""Persistence primitives for the local crawler database.

The crawler still keeps its URL-keyed archive files outside SQLite, but all
database tables have a typed SQLAlchemy mapping.  New code should use
``Database.session_factory`` and the repositories in this package instead of
opening an ad-hoc SQLite connection.
"""

from .engine import Database, create_database
from .migrations import ALEMBIC_HEAD, upgrade_database
from .models import Base
from .uow import UnitOfWork

__all__ = ["ALEMBIC_HEAD", "Base", "Database", "UnitOfWork", "create_database", "upgrade_database"]
