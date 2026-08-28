import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request

from app.config import Settings, get_settings


def connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or get_settings().database_path
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


class Row(Protocol):
    def __getitem__(self, key: str, /) -> Any: ...


class Database(Protocol):
    async def fetch_all(self, statement: str, parameters: Sequence[object] = ()) -> list[Row]: ...

    async def fetch_one(self, statement: str, parameters: Sequence[object] = ()) -> Row | None: ...


class SQLiteDatabase:
    """Async-shaped adapter around local CPython SQLite.

    Local development deliberately stays synchronous underneath. The async
    boundary matches D1 without adding a second local database dependency.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_search_index(self) -> None:
        from app.search_index import ensure_search_index

        ensure_search_index(self.connection)

    async def fetch_all(self, statement: str, parameters: Sequence[object] = ()) -> list[Row]:
        return list(self.connection.execute(statement, parameters).fetchall())

    async def fetch_one(self, statement: str, parameters: Sequence[object] = ()) -> Row | None:
        return self.connection.execute(statement, parameters).fetchone()


class D1Database:
    """Thin adapter for the D1 binding exposed in a Python Worker request."""

    def __init__(self, binding: Any) -> None:
        self.binding = binding

    def _prepare(self, statement: str, parameters: Sequence[object]) -> Any:
        prepared = self.binding.prepare(statement)
        return prepared.bind(*parameters) if parameters else prepared

    async def fetch_all(self, statement: str, parameters: Sequence[object] = ()) -> list[Row]:
        result = await self._prepare(statement, parameters).run()
        return list(result.results)

    async def fetch_one(self, statement: str, parameters: Sequence[object] = ()) -> Row | None:
        return await self._prepare(statement, parameters).first()


async def request_database(request: Request) -> AsyncIterator[Database]:
    """Resolve D1 in Workers and local SQLite everywhere else."""

    environment = request.scope.get("env")
    if environment is not None and hasattr(environment, "DB"):
        yield D1Database(environment.DB)
        return

    with database_connection() as connection:
        yield SQLiteDatabase(connection)


def initialize_database(settings: Settings | None = None) -> Path:
    active_settings = settings or get_settings()
    active_settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    migration_files = sorted(active_settings.migrations_dir.glob("*.sql"))
    if not migration_files:
        raise RuntimeError(f"No migrations found in {active_settings.migrations_dir}")

    with connect(active_settings.database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {str(row[0]) for row in connection.execute("SELECT name FROM schema_migrations")}
        for migration_file in migration_files:
            if migration_file.name in applied:
                continue
            connection.executescript(migration_file.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (migration_file.name,),
            )

    return active_settings.database_path


if __name__ == "__main__":
    database_path = initialize_database()
    print(f"Initialized empty Songuess database at {database_path}")
