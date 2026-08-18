from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from dataset.clients import (
    canonical_genres,
    fetch_listenbrainz_candidates,
    fetch_musicbrainz_metadata,
    preview_is_available,
    search_apple_track,
)

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPOSITORY_DIR / "backend" / "data" / "songuess.sqlite3"
DEFAULT_CACHE = REPOSITORY_DIR / "dataset" / "cache"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local Songuess catalog from real APIs.")
    parser.add_argument("--target", type=int, default=1000, help="Playable songs to import")
    parser.add_argument(
        "--candidates", type=int, default=4000, help="Top ListenBrainz recordings to consider"
    )
    parser.add_argument("--country", default="US", help="Two-letter Apple storefront country")
    parser.add_argument("--year-min", type=int, default=2006)
    parser.add_argument("--year-max", type=int, default=2026)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--replace", action="store_true", help="Replace the existing local song catalog"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.target < 1 or args.candidates < args.target:
        raise SystemExit("--target must be positive and --candidates must be at least --target")

    print(f"Fetching up to {args.candidates:,} top ListenBrainz recordings...")
    candidates = fetch_listenbrainz_candidates(args.cache, args.candidates)
    print(f"Enriching {len(candidates):,} recording IDs with MusicBrainz...")
    metadata = fetch_musicbrainz_metadata(args.cache, candidates)

    eligible = [
        candidate
        for candidate in candidates
        if _year_in_range(
            metadata.get(candidate["recording_mbid"], {}), args.year_min, args.year_max
        )
    ]
    print(f"Matching {len(eligible):,} recordings from {args.year_min}–{args.year_max} on Apple...")
    matched = match_tracks(
        eligible,
        metadata,
        cache_dir=args.cache,
        country=args.country,
        year_min=args.year_min,
        year_max=args.year_max,
        target=args.target,
    )
    if len(matched) < args.target:
        raise SystemExit(
            f"Only {len(matched):,} validated songs were found. Increase --candidates and rerun."
        )

    selected = matched[: args.target]
    assign_popularity_scores(selected)
    initialize_database(args.database)
    imported = write_catalog(args.database, selected, metadata, replace=args.replace)
    write_manifest(args.cache, selected)
    print(f"Imported {imported:,} playable songs into {args.database}")


def match_tracks(
    candidates: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    *,
    cache_dir: Path,
    country: str,
    year_min: int,
    year_max: int,
    target: int,
) -> list[dict[str, Any]]:
    results: dict[int, dict[str, Any]] = {}
    seen_apple_ids: set[int] = set()

    def match(rank_and_candidate: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any] | None]:
        rank, candidate = rank_and_candidate
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
            print(f"  Apple lookup failed for {mbid}: {error}")
            return rank, None
        if apple and preview_is_available(apple["previewUrl"]):
            return rank, {"candidate": candidate, "apple": apple, "rank": rank}
        return rank, None

    chunk_size = 64
    for start in range(0, len(candidates), chunk_size):
        chunk = list(enumerate(candidates[start : start + chunk_size], start=start))
        for item in chunk:
            rank, matched = match(item)
            if not matched:
                continue
            apple_id = int(matched["apple"]["trackId"])
            if apple_id not in seen_apple_ids:
                seen_apple_ids.add(apple_id)
                results[rank] = matched
        print(f"  checked {min(start + chunk_size, len(candidates)):,}; validated {len(results):,}")
        if len(results) >= target:
            break
    return [results[rank] for rank in sorted(results)]


def assign_popularity_scores(songs: list[dict[str, Any]]) -> None:
    counts = [max(1, int(song["candidate"].get("listen_count") or 1)) for song in songs]
    low, high = math.log(min(counts)), math.log(max(counts))
    for song, count in zip(songs, counts, strict=True):
        normalized = 1 if high == low else (math.log(count) - low) / (high - low)
        song["popularity_score"] = round(100 * normalized)


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    migration = (REPOSITORY_DIR / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        connection.executescript(migration)


def write_catalog(
    database_path: Path,
    songs: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    *,
    replace: bool,
) -> int:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if replace:
            connection.execute("DELETE FROM songs")
            connection.execute("DELETE FROM genres")

        for song in songs:
            candidate, apple = song["candidate"], song["apple"]
            mbid = candidate["recording_mbid"]
            artwork = apple.get("artworkUrl100")
            if artwork:
                artwork = artwork.replace("100x100bb", "600x600bb")
            connection.execute(
                "INSERT INTO songs (title, artist, album, release_year, popularity_score, "
                "listener_count, listen_count, musicbrainz_id, apple_track_id, preview_url, "
                "artwork_url, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(musicbrainz_id) DO UPDATE SET title=excluded.title, "
                "artist=excluded.artist, album=excluded.album, release_year=excluded.release_year, "
                "popularity_score=excluded.popularity_score, listen_count=excluded.listen_count, "
                "apple_track_id=excluded.apple_track_id, preview_url=excluded.preview_url, "
                "artwork_url=excluded.artwork_url, enabled=1",
                (
                    apple["trackName"],
                    apple["artistName"],
                    apple.get("collectionName"),
                    apple["canonicalReleaseYear"],
                    song["popularity_score"],
                    None,
                    candidate.get("listen_count"),
                    mbid,
                    str(apple["trackId"]),
                    apple["previewUrl"],
                    artwork,
                ),
            )
            song_id = connection.execute(
                "SELECT id FROM songs WHERE musicbrainz_id = ?", (mbid,)
            ).fetchone()[0]
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
    return len(songs)


def write_manifest(cache_dir: Path, songs: list[dict[str, Any]]) -> None:
    manifest = [
        {
            "rank": song["rank"] + 1,
            "musicbrainz_id": song["candidate"]["recording_mbid"],
            "apple_track_id": song["apple"]["trackId"],
            "listen_count": song["candidate"].get("listen_count"),
            "popularity_score": song["popularity_score"],
        }
        for song in songs
    ]
    (cache_dir / "import-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _year_in_range(metadata: dict[str, Any], year_min: int, year_max: int) -> bool:
    value = metadata.get("first-release-date", "")
    return len(value) >= 4 and value[:4].isdigit() and year_min <= int(value[:4]) <= year_max


if __name__ == "__main__":
    main()
