import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_DIR = BACKEND_DIR.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    migrations_dir: Path


def get_settings() -> Settings:
    configured_path = Path(os.getenv("SONGUESS_DATABASE_PATH", "data/songuess.sqlite3"))
    if not configured_path.is_absolute():
        configured_path = BACKEND_DIR / configured_path

    return Settings(
        database_path=configured_path.resolve(),
        migrations_dir=REPOSITORY_DIR / "migrations",
    )
