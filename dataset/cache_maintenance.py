"""Inspect, compact, and safely clean Songuess development caches."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataset.clients import (
    APPLE_CACHE_VERSION,
    _apple_cache_connection,
    _compact_apple_track,
)
from dataset.populate import DEFAULT_CACHE, DEFAULT_DATABASE, initialize_database

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPOSITORY_DIR / "dataset" / "reports"
ACTIVE_ROOT_FILES = {
    "artist-backfill.sqlite3",
    "country-backfill.sqlite3",
    "import-progress.json",
    "musicbrainz-recordings.sqlite3",
    "pipeline-metrics.sqlite3",
    "preview-validation.sqlite3",
    "spotify-streams-backfill.json",
}
DISPOSABLE_ROOT_FILES = {
    "genre-audit.json",
    "import-manifest.json",
    "musicbrainz-recordings.json",
    "popularity-rebuild-audit.json",
    "songuess-before-popularity.sqlite3",
    "spotify-streams-browser-500.json",
    "spotify-streams-mvp-500.json",
    "spotify-streams-mvp.json",
    "verify-5000.txt",
}
LEGACY_DIRECTORIES = {
    "apple",
    "apple-artists",
    "apple-artists-v2",
    "apple-v2",
    "listenbrainz-radio",
    "listenbrainz-radio-v2-r60",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("baseline", "compact", "cleanup"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--label", default="baseline-5000")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply cleanup; cleanup is a dry-run unless this is supplied",
    )
    return parser.parse_args()


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _catalog_statistics(database: Path) -> dict[str, Any]:
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        enabled = int(
            connection.execute("SELECT COUNT(*) FROM songs WHERE enabled = 1").fetchone()[0]
        )
        return {
            "database_bytes": _path_size(database),
            "songs_total": int(connection.execute("SELECT COUNT(*) FROM songs").fetchone()[0]),
            "songs_enabled": enabled,
            "distinct_musicbrainz_ids": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT musicbrainz_id) FROM songs WHERE enabled = 1"
                ).fetchone()[0]
            ),
            "distinct_apple_track_ids": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT apple_track_id) FROM songs WHERE enabled = 1"
                ).fetchone()[0]
            ),
            "distinct_spotify_urls": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT spotify_url) FROM songs WHERE enabled = 1"
                ).fetchone()[0]
            ),
            "songs_with_stream_counts": int(
                connection.execute(
                    "SELECT COUNT(*) FROM songs WHERE enabled = 1 AND stream_count IS NOT NULL"
                ).fetchone()[0]
            ),
            "artists": int(connection.execute("SELECT COUNT(*) FROM artists").fetchone()[0]),
            "genres": int(connection.execute("SELECT COUNT(*) FROM genres").fetchone()[0]),
            "countries": int(connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0]),
            "min_release_year": connection.execute(
                "SELECT MIN(release_year) FROM songs WHERE enabled = 1"
            ).fetchone()[0],
            "max_release_year": connection.execute(
                "SELECT MAX(release_year) FROM songs WHERE enabled = 1"
            ).fetchone()[0],
            "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
            "free_pages": int(connection.execute("PRAGMA freelist_count").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        }


def baseline(cache: Path, database: Path, report_dir: Path, *, label: str) -> dict[str, Any]:
    cache_entries = {
        child.name: _path_size(child)
        for child in sorted(cache.iterdir())
        if child.name not in {".DS_Store"} and not child.name.endswith(("-shm", "-wal"))
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "cache_bytes": _path_size(cache),
        "cache_entries": cache_entries,
        "catalog": _catalog_statistics(database),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"catalog-{label}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Baseline {label}: cache {_path_size(cache):,} bytes; report {path}")
    return report


def _compact_recording(payload: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("id", "title", "length", "video", "first-release-date", "isrcs", "tags"):
        if key in payload:
            compact[key] = payload[key]
    credits: list[dict[str, Any]] = []
    for credit in payload.get("artist-credit", []):
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist")
        if not isinstance(artist, dict):
            continue
        compact_artist = {
            key: artist[key]
            for key in ("id", "name", "sort-name", "disambiguation")
            if key in artist
        }
        credits.append(
            {
                key: value
                for key, value in {
                    "name": credit.get("name"),
                    "joinphrase": credit.get("joinphrase"),
                    "artist": compact_artist,
                }.items()
                if value not in (None, "")
            }
        )
    if credits:
        compact["artist-credit"] = credits
    return compact


def _compact_artist(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("id", "name", "sort-name", "disambiguation", "country", "area")
        if key in payload
    }


def compact(cache: Path, database: Path) -> dict[str, int]:
    musicbrainz = cache / "musicbrainz-recordings.sqlite3"
    before_musicbrainz = _path_size(musicbrainz)
    recording_rows = 0
    artist_rows = 0
    with sqlite3.connect(musicbrainz) as connection:
        recordings = connection.execute("SELECT mbid, payload_json FROM recordings").fetchall()
        artists = connection.execute("SELECT mbid, payload_json FROM artists").fetchall()
        with connection:
            connection.executemany(
                "UPDATE recordings SET payload_json = ? WHERE mbid = ?",
                [
                    (
                        json.dumps(
                            _compact_recording(json.loads(payload)),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        mbid,
                    )
                    for mbid, payload in recordings
                ],
            )
            connection.executemany(
                "UPDATE artists SET payload_json = ? WHERE mbid = ?",
                [
                    (
                        json.dumps(
                            _compact_artist(json.loads(payload)),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        mbid,
                    )
                    for mbid, payload in artists
                ],
            )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        recording_rows = len(recordings)
        artist_rows = len(artists)
    before_database = _path_size(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA optimize")
    apple_before = _path_size(cache / "apple-cache.sqlite3")
    migrated_tracks, migrated_artists = _migrate_apple_json_caches(cache)
    with _apple_cache_connection(cache) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
    result = {
        "musicbrainz_rows": recording_rows,
        "musicbrainz_artist_rows": artist_rows,
        "musicbrainz_before_bytes": before_musicbrainz,
        "musicbrainz_after_bytes": _path_size(musicbrainz),
        "database_before_bytes": before_database,
        "database_after_bytes": _path_size(database),
        "apple_track_rows_migrated": migrated_tracks,
        "apple_artist_rows_migrated": migrated_artists,
        "apple_sqlite_before_bytes": apple_before,
        "apple_sqlite_after_bytes": _path_size(cache / "apple-cache.sqlite3"),
    }
    print(json.dumps(result, indent=2))
    return result


def _migrate_apple_json_caches(cache: Path) -> tuple[int, int]:
    track_rows: dict[str, tuple[dict[str, Any] | None, float]] = {}
    for path in sorted((cache / "apple").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        track_rows[path.stem] = (
            payload if payload.get("trackId") else None,
            path.stat().st_mtime,
        )
    for path in sorted((cache / "apple-v2" / "US").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") == APPLE_CACHE_VERSION:
            track = payload.get("track") if payload.get("status") == "matched" else None
            track_rows[path.stem] = (
                track,
                float(payload.get("checked_at") or path.stat().st_mtime),
            )
        elif payload.get("trackId"):
            track_rows[path.stem] = (payload, path.stat().st_mtime)
    artist_rows: dict[str, tuple[dict[str, Any], float]] = {}
    for path in sorted((cache / "apple-artists").glob("*.json")):
        artist_rows[path.stem] = (
            json.loads(path.read_text(encoding="utf-8")),
            path.stat().st_mtime,
        )
    for path in sorted((cache / "apple-artists-v2" / "US").glob("*.json")):
        cached = json.loads(path.read_text(encoding="utf-8"))
        payload = cached.get("payload", cached)
        if isinstance(payload, dict):
            artist_rows[path.stem] = (
                payload,
                float(cached.get("checked_at") or path.stat().st_mtime),
            )
    with _apple_cache_connection(cache) as connection:
        connection.executemany(
            "INSERT INTO track_matches "
            "(country, recording_mbid, status, track_json, checked_at) "
            "VALUES ('US', ?, ?, ?, ?) ON CONFLICT(country, recording_mbid) DO UPDATE SET "
            "status=excluded.status, track_json=excluded.track_json, "
            "checked_at=excluded.checked_at",
            [
                (
                    recording_mbid,
                    "matched" if track else "negative",
                    json.dumps(
                        _compact_apple_track(track),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if track
                    else None,
                    checked_at,
                )
                for recording_mbid, (track, checked_at) in track_rows.items()
            ],
        )
        artist_records = []
        for cache_key, (payload, checked_at) in artist_rows.items():
            results = [
                _compact_apple_track(result)
                for result in payload.get("results", [])
                if isinstance(result, dict) and result.get("trackId")
            ]
            compact_payload = {"resultCount": len(results), "results": results}
            artist_records.append(
                (
                    cache_key,
                    json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")),
                    checked_at,
                )
            )
        connection.executemany(
            "INSERT INTO artist_searches (country, cache_key, payload_json, checked_at) "
            "VALUES ('US', ?, ?, ?) ON CONFLICT(country, cache_key) DO UPDATE SET "
            "payload_json=excluded.payload_json, checked_at=excluded.checked_at",
            artist_records,
        )
    return len(track_rows), len(artist_rows)


def _apple_migration_complete(cache: Path) -> bool:
    database = cache / "apple-cache.sqlite3"
    if not database.exists():
        return False
    track_keys = {path.stem for path in (cache / "apple").glob("*.json")} | {
        path.stem for path in (cache / "apple-v2" / "US").glob("*.json")
    }
    artist_keys = {path.stem for path in (cache / "apple-artists").glob("*.json")} | {
        path.stem for path in (cache / "apple-artists-v2" / "US").glob("*.json")
    }
    with _apple_cache_connection(cache) as connection:
        database_tracks = {
            str(row[0])
            for row in connection.execute(
                "SELECT recording_mbid FROM track_matches WHERE country = 'US'"
            )
        }
        database_artists = {
            str(row[0])
            for row in connection.execute(
                "SELECT cache_key FROM artist_searches WHERE country = 'US'"
            )
        }
    return track_keys <= database_tracks and artist_keys <= database_artists


def cleanup_candidates(cache: Path) -> list[Path]:
    candidates: list[Path] = []
    for child in cache.iterdir():
        name = child.name
        if name.endswith(("-shm", "-wal")):
            continue
        if name in DISPOSABLE_ROOT_FILES:
            candidates.append(child)
            continue
        if name in LEGACY_DIRECTORIES:
            if name.startswith("apple") and not _apple_migration_complete(cache):
                continue
            candidates.append(child)
            continue
        if name.startswith("listenbrainz-") and "-v3-" not in name:
            candidates.append(child)
            continue
        if name.startswith(("populate-", "spotify-streams-")) and name not in ACTIVE_ROOT_FILES:
            candidates.append(child)
    return sorted(set(candidates))


def cleanup(cache: Path, *, apply: bool) -> dict[str, Any]:
    candidates = cleanup_candidates(cache)
    bytes_recoverable = sum(_path_size(path) for path in candidates)
    print(
        f"Cache cleanup {'APPLY' if apply else 'DRY RUN'}: "
        f"{len(candidates)} paths; {bytes_recoverable:,} bytes recoverable"
    )
    for path in candidates:
        print(f"  {path.relative_to(cache)} ({_path_size(path):,} bytes)")
    if apply:
        for path in candidates:
            if path.is_dir():
                for item in sorted(path.rglob("*"), reverse=True):
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        item.rmdir()
                path.rmdir()
            else:
                path.unlink()
    return {
        "apply": apply,
        "paths": [str(path.relative_to(cache)) for path in candidates],
        "bytes_recoverable": bytes_recoverable,
    }


def main() -> None:
    args = parse_args()
    if args.operation == "baseline":
        baseline(args.cache, args.database, args.report_dir, label=args.label)
    elif args.operation == "compact":
        compact(args.cache, args.database)
    else:
        cleanup(args.cache, apply=args.apply)


if __name__ == "__main__":
    main()
