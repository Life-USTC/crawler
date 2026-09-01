from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from sqlalchemy.engine import Connection, CursorResult, Engine


class RowMapping(dict[str, Any]):
    """Small mapping row with positional access for legacy reporting code."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__(values)
        self._values = tuple(values.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class CoreResult:
    def __init__(self, result: CursorResult[Any], release: Callable[[], None]) -> None:
        self._result = result
        self._release = release
        self.rowcount = result.rowcount

    def fetchone(self) -> RowMapping | None:
        try:
            row = self._result.mappings().fetchone()
            return RowMapping(row) if row is not None else None
        finally:
            self._release()

    def fetchall(self) -> list[RowMapping]:
        try:
            return [RowMapping(row) for row in self._result.mappings().fetchall()]
        finally:
            self._release()

    def __iter__(self) -> Iterator[RowMapping]:
        return iter(self.fetchall())


class CoreConnection:
    """Private SQLAlchemy Core adapter used while Store methods are migrated.

    It deliberately accepts only SQLAlchemy's driver-level SQL execution; no
    raw DB-API connection or fallback API is exposed.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._connection: Connection | None = None
        self._dirty = False

    def _active_connection(self) -> Connection:
        if self._connection is None:
            self._connection = self.engine.connect()
        return self._connection

    def execute(self, statement: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> CoreResult:
        if isinstance(parameters, list):
            parameters = tuple(parameters)
        self._dirty = self._dirty or _is_write_statement(statement)
        return CoreResult(self._active_connection().exec_driver_sql(statement, parameters), self._release_read)

    def executemany(self, statement: str, parameters: Iterable[Sequence[Any]]) -> CoreResult:
        self._dirty = self._dirty or _is_write_statement(statement)
        return CoreResult(
            self._active_connection().exec_driver_sql(statement, list(parameters)),
            self._release_read,
        )

    def _release_read(self) -> None:
        """Return a connection once a result has been fully consumed."""

        if self._connection is not None and not self._dirty:
            self._connection.close()
            self._connection = None

    def commit(self) -> None:
        if self._connection is not None:
            self._connection.commit()
            self._connection.close()
            self._connection = None
            self._dirty = False

    def rollback(self) -> None:
        if self._connection is not None:
            self._connection.rollback()
            self._connection.close()
            self._connection = None
            self._dirty = False

    @property
    def total_changes(self) -> int:
        result = self.execute("SELECT total_changes()")
        row = result.fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._dirty = False


def _is_write_statement(statement: str) -> bool:
    first = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
    return first in {"ALTER", "CREATE", "DELETE", "DROP", "INSERT", "REPLACE", "UPDATE"}
