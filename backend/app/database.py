import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
