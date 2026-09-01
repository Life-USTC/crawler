from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from .engine import Database


class UnitOfWork:
    """Small explicit transaction boundary for crawler use cases."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.session: Session | None = None

    def __enter__(self) -> UnitOfWork:
        self.session = self.database.session_factory()
        return self

    def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        self.session.commit()

    def rollback(self) -> None:
        if self.session is not None:
            self.session.rollback()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.session is None:
            return
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
            self.session = None


@contextmanager
def transaction(database: Database) -> Iterator[Session]:
    """Yield one session and commit it exactly once on successful exit."""

    with UnitOfWork(database) as unit:
        assert unit.session is not None
        yield unit.session
