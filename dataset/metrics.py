"""Small SQLite telemetry ledger for offline catalog pipeline runs."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()


def metrics_database() -> Path | None:
    value = os.environ.get("SONGUESS_METRICS_DB", "").strip()
    return Path(value) if value else None


def current_run_id() -> str | None:
    return os.environ.get("SONGUESS_METRICS_RUN_ID", "").strip() or None


def _connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_runs ("
        "id TEXT PRIMARY KEY, operation TEXT NOT NULL, started_at TEXT NOT NULL, "
        "finished_at TEXT, status TEXT NOT NULL, accepted_songs INTEGER NOT NULL DEFAULT 0, "
        "details_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_metrics ("
        "run_id TEXT NOT NULL, provider TEXT NOT NULL, metric TEXT NOT NULL, "
        "value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id, provider, metric), "
        "FOREIGN KEY(run_id) REFERENCES pipeline_runs(id) ON DELETE CASCADE)"
    )
    return connection


def start_run(cache_dir: Path, operation: str) -> str:
    path = cache_dir / "pipeline-metrics.sqlite3"
    run_id = str(uuid.uuid4())
    with _LOCK, _connection(path) as connection:
        connection.execute(
            "INSERT INTO pipeline_runs (id, operation, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (run_id, operation, datetime.now(UTC).isoformat()),
        )
    os.environ["SONGUESS_METRICS_DB"] = str(path)
    os.environ["SONGUESS_METRICS_RUN_ID"] = run_id
    return run_id


def record(provider: str, metric: str, value: int = 1) -> None:
    path = metrics_database()
    run_id = current_run_id()
    if path is None or run_id is None:
        return
    with _LOCK, _connection(path) as connection:
        connection.execute(
            "INSERT INTO provider_metrics (run_id, provider, metric, value) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, provider, metric) DO UPDATE SET "
            "value = value + excluded.value",
            (run_id, provider, metric, int(value)),
        )


def finish_run(*, status: str, accepted_songs: int = 0, details_json: str | None = None) -> None:
    path = metrics_database()
    run_id = current_run_id()
    if path is None or run_id is None:
        return
    with _LOCK, _connection(path) as connection:
        connection.execute(
            "UPDATE pipeline_runs SET finished_at = ?, status = ?, accepted_songs = ?, "
            "details_json = ? WHERE id = ?",
            (
                datetime.now(UTC).isoformat(),
                status,
                int(accepted_songs),
                details_json,
                run_id,
            ),
        )
