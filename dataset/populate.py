from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dataset.clients import (
    canonical_genres,
    fetch_listenbrainz_candidates,
    fetch_musicbrainz_metadata,
    search_apple_track,
    validate_previews,
)

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPOSITORY_DIR / "backend" / "data" / "songuess.sqlite3"
DEFAULT_CACHE = REPOSITORY_DIR / "dataset" / "cache"


@dataclass(frozen=True)
class CatalogState:
    enabled_count: int
    musicbrainz_ids: set[str]
    apple_track_ids: set[str]


@dataclass
class Progress:
    candidates_discovered: int = 0
    eligible_songs: int = 0
    existing_songs: int = 0
    candidates_checked: int = 0
    new_validated_songs: int = 0
    apple_misses: int = 0
    apple_failures: int = 0
    preview_invalid: int = 0
    preview_transient: int = 0
    duplicates: int = 0

    @property
    def failures(self) -> int:
        return (
            self.apple_misses
            + self.apple_failures
            + self.preview_invalid
            + self.preview_transient
            + self.duplicates
        )


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally build the naturally ranked Songuess catalog."
    )
    parser.add_argument(
        "--target-total",
        type=int,
        default=1000,
        help="Desired final number of enabled unique songs",
    )
    parser.add_argument(
        "--candidates", type=int, default=4000, help="Top ListenBrainz candidates to consider"
    )
    parser.add_argument("--country", default="US", help="Two-letter Apple storefront country")
    parser.add_argument("--year-min", type=int, default=1950)
    parser.add_argument("--year-max", type=int, default=2026)
    parser.add_argument("--artist-count", type=int, default=1000)
    parser.add_argument("--recordings-per-artist", type=int, default=60)
    parser.add_argument("--preview-workers", type=int, default=8)
    parser.add_argument("--match-batch-size", type=int, default=64)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    _validate_args(args)
    initialize_database(args.database)
    initial_state = catalog_state(args.database)
    needed = target_gap(args.database, args.target_total)
    if needed < 0:
        raise SystemExit(
            f"Catalog already has {initial_state.enabled_count:,} enabled songs, above the "
            f"requested total of {args.target_total:,}; refusing to remove existing songs."
        )
    if needed == 0:
        recompute_catalog_popularity(args.database)
        print(f"Catalog already contains exactly {args.target_total:,} enabled songs.")
        return

    progress = Progress(existing_songs=initial_state.enabled_count)
    print(
        f"Existing enabled songs: {initial_state.enabled_count:,}; "
        f"target total: {args.target_total:,}; new songs needed: {needed:,}",
        flush=True,
    )
    print(f"Discovering up to {args.candidates:,} naturally ranked ListenBrainz candidates...")
    candidates = fetch_listenbrainz_candidates(
        args.cache,
        args.candidates,
        artist_count=args.artist_count,
        recordings_per_artist=args.recordings_per_artist,
    )
    progress.candidates_discovered = len(candidates)
    new_candidates = [
        candidate
        for candidate in candidates
        if candidate["recording_mbid"] not in initial_state.musicbrainz_ids
    ]
    print(
        f"Candidates discovered: {len(candidates):,}; not already enabled: {len(new_candidates):,}",
        flush=True,
    )

    print(f"Enriching {len(new_candidates):,} candidate recording IDs with MusicBrainz...")
    metadata = fetch_musicbrainz_metadata(args.cache, new_candidates)
    eligible = [
        candidate
        for candidate in new_candidates
        if _year_in_range(
            metadata.get(candidate["recording_mbid"], {}), args.year_min, args.year_max
        )
    ]
    progress.eligible_songs = len(eligible)
    print(
        f"Eligible songs in {args.year_min}–{args.year_max}: {len(eligible):,}; "
        f"estimated candidates remaining: {len(eligible):,}",
        flush=True,
    )

    try:
        import_until_target(
            args.database,
            args.cache,
            eligible,
            metadata,
            country=args.country,
            year_min=args.year_min,
            year_max=args.year_max,
            target_total=args.target_total,
            preview_workers=args.preview_workers,
            batch_size=args.match_batch_size,
            progress=progress,
        )
    except KeyboardInterrupt:
        _write_progress(args.cache, args, progress, catalog_state(args.database).enabled_count)
        print("\nInterrupted safely. Rerun the same just command to resume from caches and DB.")
        raise SystemExit(130) from None

    final_state = catalog_state(args.database)
    _write_progress(args.cache, args, progress, final_state.enabled_count)
    if final_state.enabled_count != args.target_total:
        raise SystemExit(
            f"Stopped at {final_state.enabled_count:,}/{args.target_total:,} enabled songs after "
            f"checking {progress.candidates_checked:,} eligible candidates. Increase "
            "--candidates and rerun; cached work will resume."
        )
    print(
        f"Complete: exactly {final_state.enabled_count:,} enabled songs; "
        f"added {progress.new_validated_songs:,}; failures {progress.failures:,}.",
        flush=True,
    )


def import_until_target(
    database_path: Path,
    cache_dir: Path,
    candidates: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    *,
    country: str,
    year_min: int,
    year_max: int,
    target_total: int,
    preview_workers: int,
    batch_size: int,
    progress: Progress,
) -> None:
    started_at = time.monotonic()
    state = catalog_state(database_path)
    seen_apple_ids = set(state.apple_track_ids)

    for start in range(0, len(candidates), batch_size):
        current_count = catalog_state(database_path).enabled_count
        needed = target_total - current_count
        if needed <= 0:
            break

        chunk = candidates[start : start + batch_size]
        apple_matches: list[dict[str, Any]] = []
        for rank, candidate in enumerate(chunk, start=start):
            progress.candidates_checked += 1
            mbid = candidate["recording_mbid"]
            try:
                apple = search_apple_track(
                    cache_dir,
                    candidate,
                    metadata.get(mbid, {}),
                    country=country,
                    year_min=year_min,
                    year_max=year_max,
                )
            except Exception as error:
                progress.apple_failures += 1
                print(f"  Apple lookup failed for {mbid}: {error}", flush=True)
                continue
            if apple is None:
                progress.apple_misses += 1
                continue
            apple_id = str(apple["trackId"])
            if apple_id in seen_apple_ids:
                progress.duplicates += 1
                continue
            seen_apple_ids.add(apple_id)
            apple_matches.append({"candidate": candidate, "apple": apple, "rank": rank})

        statuses = validate_previews(
            cache_dir,
            [match["apple"]["previewUrl"] for match in apple_matches],
            max_workers=preview_workers,
        )
        validated: list[dict[str, Any]] = []
        for match in apple_matches:
            status = statuses[match["apple"]["previewUrl"]]
            if status == "valid":
                validated.append(match)
            elif status == "transient":
                progress.preview_transient += 1
            else:
                progress.preview_invalid += 1

        selected = validated[:needed]
        inserted = write_catalog(database_path, selected, metadata)
        progress.new_validated_songs += inserted
        if selected:
            recompute_catalog_popularity(database_path)
        current_count = catalog_state(database_path).enabled_count
        _print_progress(
            progress,
            current_count=current_count,
            target_total=target_total,
            total_eligible=len(candidates),
            elapsed=time.monotonic() - started_at,
        )
        _write_progress_values(
            cache_dir,
            progress,
            current_count=current_count,
            target_total=target_total,
            year_min=year_min,
            year_max=year_max,
            candidates=len(candidates),
        )


def catalog_state(database_path: Path) -> CatalogState:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT musicbrainz_id), "
            "COUNT(DISTINCT apple_track_id) FROM songs WHERE enabled = 1"
        ).fetchone()
        musicbrainz_ids = {
            str(value[0])
            for value in connection.execute(
                "SELECT musicbrainz_id FROM songs WHERE enabled = 1 AND musicbrainz_id IS NOT NULL"
            )
        }
        apple_track_ids = {
            str(value[0])
            for value in connection.execute(
                "SELECT apple_track_id FROM songs WHERE enabled = 1 AND apple_track_id IS NOT NULL"
            )
        }
    enabled_count = int(row[0])
    if enabled_count != int(row[1]) or enabled_count != int(row[2]):
        raise RuntimeError(
            "Enabled catalog contains missing or duplicate MusicBrainz/Apple identifiers"
        )
    return CatalogState(enabled_count, musicbrainz_ids, apple_track_ids)


def target_gap(database_path: Path, target_total: int) -> int:
    """Return how many enabled songs must be added to reach a final total."""
    return target_total - catalog_state(database_path).enabled_count


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    migration = (REPOSITORY_DIR / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        connection.executescript(migration)


def write_catalog(
    database_path: Path,
    songs: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
) -> int:
    enabled_delta = 0
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for song in songs:
            candidate, apple = song["candidate"], song["apple"]
            mbid = str(candidate["recording_mbid"])
            apple_id = str(apple["trackId"])
            existing_mbid = connection.execute(
                "SELECT id, enabled FROM songs WHERE musicbrainz_id = ?", (mbid,)
            ).fetchone()
            existing_apple = connection.execute(
                "SELECT id, musicbrainz_id FROM songs WHERE apple_track_id = ?", (apple_id,)
            ).fetchone()
            if existing_apple and (not existing_mbid or existing_apple[0] != existing_mbid[0]):
                continue

            artwork = apple.get("artworkUrl100")
            if artwork:
                artwork = artwork.replace("100x100bb", "600x600bb")
            values = (
                apple["trackName"],
                apple["artistName"],
                apple.get("collectionName"),
                apple["canonicalReleaseYear"],
                max(1, int(candidate.get("listen_count") or 1)),
                mbid,
                apple_id,
                apple["previewUrl"],
                artwork,
            )
            if existing_mbid:
                connection.execute(
                    "UPDATE songs SET title = ?, artist = ?, album = ?, release_year = ?, "
                    "listen_count = ?, musicbrainz_id = ?, apple_track_id = ?, preview_url = ?, "
                    "artwork_url = ?, enabled = 1 WHERE id = ?",
                    (*values, existing_mbid[0]),
                )
                song_id = int(existing_mbid[0])
                enabled_delta += 1 - int(existing_mbid[1])
            else:
                cursor = connection.execute(
                    "INSERT INTO songs (title, artist, album, release_year, popularity_score, "
                    "listener_count, listen_count, musicbrainz_id, apple_track_id, preview_url, "
                    "artwork_url, enabled) VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, 1)",
                    values,
                )
                song_id = int(cursor.lastrowid)
                enabled_delta += 1

            connection.execute("DELETE FROM song_genres WHERE song_id = ?", (song_id,))
            for genre in canonical_genres(apple, metadata.get(mbid, {})):
                connection.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre,))
                genre_id = connection.execute(
                    "SELECT id FROM genres WHERE name = ?", (genre,)
                ).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO song_genres (song_id, genre_id) VALUES (?, ?)",
                    (song_id, genre_id),
                )
        connection.execute(
            "DELETE FROM genres WHERE NOT EXISTS "
            "(SELECT 1 FROM song_genres WHERE genre_id = genres.id)"
        )
    return enabled_delta


def recompute_catalog_popularity(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, COALESCE(listen_count, 1) FROM songs WHERE enabled = 1"
        ).fetchall()
        if not rows:
            return
        counts = [max(1, int(row[1])) for row in rows]
        low, high = math.log(min(counts)), math.log(max(counts))
        updates = []
        for row, count in zip(rows, counts, strict=True):
            normalized = 1.0 if high == low else (math.log(count) - low) / (high - low)
            updates.append((round(100 * normalized), int(row[0])))
        connection.executemany("UPDATE songs SET popularity_score = ? WHERE id = ?", updates)


def _print_progress(
    progress: Progress,
    *,
    current_count: int,
    target_total: int,
    total_eligible: int,
    elapsed: float,
) -> None:
    remaining_target = max(0, target_total - current_count)
    remaining_candidates = max(0, total_eligible - progress.candidates_checked)
    rate = progress.new_validated_songs / max(1, progress.candidates_checked)
    estimated_candidates = math.ceil(remaining_target / rate) if rate else None
    eta_seconds = (
        elapsed / progress.new_validated_songs * remaining_target
        if progress.new_validated_songs
        else None
    )
    estimate = (
        f"~{estimated_candidates:,} candidates / {_duration(eta_seconds)}"
        if estimated_candidates is not None and eta_seconds is not None
        else "waiting for validation yield"
    )
    print(
        f"  Progress: checked {progress.candidates_checked:,}/{total_eligible:,}; "
        f"catalog {current_count:,}/{target_total:,}; new validated "
        f"{progress.new_validated_songs:,}; failures {progress.failures:,}; "
        f"eligible remaining {remaining_candidates:,}; estimated work {estimate}",
        flush=True,
    )


def _write_progress(
    cache_dir: Path, args: argparse.Namespace, progress: Progress, current_count: int
) -> None:
    _write_progress_values(
        cache_dir,
        progress,
        current_count=current_count,
        target_total=args.target_total,
        year_min=args.year_min,
        year_max=args.year_max,
        candidates=args.candidates,
    )


def _write_progress_values(
    cache_dir: Path,
    progress: Progress,
    *,
    current_count: int,
    target_total: int,
    year_min: int,
    year_max: int,
    candidates: int,
) -> None:
    path = cache_dir / "import-progress.json"
    payload = {
        "version": 2,
        "updated_at": time.time(),
        "current_total": current_count,
        "target_total": target_total,
        "year_min": year_min,
        "year_max": year_max,
        "candidate_limit": candidates,
        "progress": asdict(progress),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def _validate_args(args: argparse.Namespace) -> None:
    if args.target_total < 1:
        raise SystemExit("--target-total must be positive")
    if args.candidates < 1:
        raise SystemExit("--candidates must be positive")
    if args.year_min > args.year_max:
        raise SystemExit("--year-min cannot be greater than --year-max")
    if not 1 <= args.artist_count <= 1000:
        raise SystemExit("--artist-count must be between 1 and 1000")
    if args.recordings_per_artist < 1 or args.preview_workers < 1 or args.match_batch_size < 1:
        raise SystemExit("recording, preview worker, and batch counts must be positive")


def _year_in_range(metadata: dict[str, Any], year_min: int, year_max: int) -> bool:
    value = metadata.get("first-release-date", "")
    return len(value) >= 4 and value[:4].isdigit() and year_min <= int(value[:4]) <= year_max


if __name__ == "__main__":
    main()
