from pathlib import Path

from app.config import get_settings


def test_default_paths_are_resolved_from_backend_and_repository_roots(monkeypatch) -> None:
    monkeypatch.delenv("SONGUESS_DATABASE_PATH", raising=False)
    backend_dir = Path(__file__).resolve().parents[1]
    repository_dir = backend_dir.parent

    settings = get_settings()

    assert settings.database_path == backend_dir / "data/songuess.sqlite3"
    assert settings.migrations_dir == repository_dir / "migrations"
