"""Export the enabled application catalog as deterministic D1-compatible SQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

from app.search_index import rebuild_search_index  # noqa: E402
from dataset.populate import DEFAULT_DATABASE  # noqa: E402

APPLICATION_COLUMNS: dict[str, tuple[str, ...]] = {
    "songs": (
        "id",
        "title",
        "artist",
        "album",
        "release_year",
        "popularity_score",
        "musicbrainz_id",
        "apple_track_id",
        "preview_url",
        "artwork_url",
        "apple_music_url",
        "spotify_url",
        "enabled",
    ),
    "artists": ("id", "musicbrainz_id", "name", "sort_name", "disambiguation"),
    "genres": ("id", "name"),
    "countries": ("id", "code"),
    "song_artists": (
        "song_id",
        "artist_id",
        "credit_order",
        "credited_name",
        "join_phrase",
    ),
    "song_genres": ("song_id", "genre_id"),
    "song_countries": ("song_id", "country_id"),
    "song_genre_evidence": ("song_id", "genre_id", "rank", "score", "source"),
}

DERIVED_SEARCH_COLUMNS: dict[str, tuple[str, ...]] = {
    "song_search": (
        "song_id",
        "normalized_title",
        "normalized_title_compact",
        "normalized_artist",
        "normalized_artist_compact",
        "normalized_album",
        "normalized_album_compact",
        "normalized_year",
        "normalized_text",
    ),
    "song_search_fts": ("song_id", "normalized_text"),
    "artist_search_aliases": (
        "artist_id",
        "alias",
        "normalized_alias",
        "normalized_alias_compact",
    ),
    "artist_search_fts": ("artist_id", "normalized_alias"),
}

INSERT_BATCH_SIZE = 50


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    return parser.parse_args()


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    return "'" + str(value).replace("'", "''") + "'"


def _rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> list[tuple[Any, ...]]:
    selected = ", ".join(columns)
    if table == "songs":
        statement = f"SELECT {selected} FROM songs WHERE enabled = 1 ORDER BY id"
        parameters: tuple[object, ...] = ()
    elif table in {"artists", "genres", "countries"}:
        relation, foreign_key = {
            "artists": ("song_artists", "artist_id"),
            "genres": ("song_genres", "genre_id"),
            "countries": ("song_countries", "country_id"),
        }[table]
        statement = (
            f"SELECT {selected} FROM {table} WHERE id IN ("
            f"SELECT DISTINCT r.{foreign_key} FROM {relation} r "
            "JOIN songs s ON s.id = r.song_id WHERE s.enabled = 1) ORDER BY id"
        )
        parameters = ()
    elif table.startswith("song_") and table not in {"song_search", "song_search_fts"}:
        statement = (
            f"SELECT {selected} FROM {table} WHERE song_id IN ("
            "SELECT id FROM songs WHERE enabled = 1) ORDER BY song_id"
        )
        parameters = ()
    else:
        order_column = "artist_id" if table.startswith("artist_") else "song_id"
        statement = f"SELECT {selected} FROM {table} ORDER BY {order_column}"
        parameters = ()
    return [tuple(row) for row in connection.execute(statement, parameters)]


def _catalog_hash(table_rows: dict[str, list[tuple[Any, ...]]]) -> str:
    digest = hashlib.sha256()
    for table in APPLICATION_COLUMNS:
        digest.update(table.encode())
        digest.update(b"\0")
        for row in table_rows[table]:
            digest.update(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def export_d1(database: Path, output: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    if not database.is_file():
        raise FileNotFoundError(f"Catalog database does not exist: {database}")

    with sqlite3.connect(database) as source, sqlite3.connect(":memory:") as connection:
        source.backup(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {str(row[0]) for row in connection.execute("SELECT name FROM schema_migrations")}
        for migration_path in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
            if migration_path.name in applied:
                continue
            connection.executescript(migration_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (migration_path.name,),
            )
        rebuild_search_index(connection)
        table_rows = {
            table: _rows(connection, table, columns)
            for table, columns in {**APPLICATION_COLUMNS, **DERIVED_SEARCH_COLUMNS}.items()
        }

    # Wrangler's remote D1 bulk importer provides the transaction boundary and
    # rejects explicit BEGIN/COMMIT statements in uploaded SQL files.
    statements = ["PRAGMA foreign_keys = ON;"]
    for table, columns in {**APPLICATION_COLUMNS, **DERIVED_SEARCH_COLUMNS}.items():
        column_list = ", ".join(columns)
        rows = table_rows[table]
        for start in range(0, len(rows), INSERT_BATCH_SIZE):
            batch = rows[start : start + INSERT_BATCH_SIZE]
            values = ",\n".join(
                "(" + ", ".join(_sql_literal(value) for value in row) + ")" for row in batch
            )
            statements.append(f"INSERT INTO {table} ({column_list}) VALUES\n{values};")
    statements.extend(["PRAGMA optimize;", ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(statements), encoding="utf-8")

    manifest = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_database": str(database.resolve()),
        "catalog_sha256": _catalog_hash(table_rows),
        "counts": {table: len(rows) for table, rows in table_rows.items()},
        "excludes": [
            "provider caches",
            "browser telemetry",
            "candidate manifests",
            "pipeline checkpoints",
            "pipeline metrics",
            "spotify backfill failures",
        ],
    }
    active_manifest = manifest_path or output.with_suffix(output.suffix + ".manifest.json")
    active_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    args = parse_args()
    manifest = export_d1(args.database, args.output, args.manifest)
    print(
        f"Exported {manifest['counts']['songs']:,} songs to {args.output} "
        f"(catalog sha256 {manifest['catalog_sha256']})"
    )


if __name__ == "__main__":
    main()
