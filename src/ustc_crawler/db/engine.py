from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    """Apply the SQLite settings required by a resumable single-writer crawl."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


class Database:
    """SQLAlchemy engine and session factory for the local crawler database.

    ``create_schema`` is deliberately explicit.  The application uses it for
    a new local database; production schema changes are represented by the
    Alembic revisions in ``alembic/`` rather than hidden runtime fallbacks.
    """

    def __init__(self, path: str | Path, *, create_schema: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            future=True,
        )
        event.listen(self.engine, "connect", _configure_sqlite)
        self.session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        if create_schema:
            self.create_schema()

    def create_schema(self) -> None:
        """Initialize a new database through the Alembic baseline only."""

        if self.path.exists() and self.path.stat().st_size > 0:
            raise RuntimeError(
                "create_schema is only valid for a new database; run ustc-crawler db-upgrade"
            )
        from .migrations import upgrade_database

        upgrade_database(self.path)

    def assert_schema_head(self, expected: str) -> None:
        """Assert that Alembic has recorded the requested schema revision."""

        from sqlalchemy import inspect, text

        inspector = inspect(self.engine)
        if "alembic_version" not in inspector.get_table_names():
            raise RuntimeError("SQLite schema is not managed by Alembic")
        with self.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        if revision != expected:
            raise RuntimeError(f"SQLite schema revision {revision!r} is not {expected!r}")

    def connect(self) -> Any:
        """Return a SQLAlchemy connection for Core operations."""

        return self.engine.connect()

    def close(self) -> None:
        self.engine.dispose()


def create_database(path: str | Path, *, create_schema: bool = True) -> Database:
    return Database(path, create_schema=create_schema)
