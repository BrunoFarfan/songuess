from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dataset.clients import (
    cache_apple_track,
    fetch_apple_tracks_by_ids,
    fetch_listenbrainz_candidates,
    fetch_musicbrainz_artist_countries,
    fetch_musicbrainz_metadata,
    fetch_musicbrainz_spotify_urls,
    find_explicit_apple_equivalents,
    has_fresh_negative_apple_match,
    read_cached_apple_track,
    search_apple_track,
    stored_apple_explicitness,
    validate_previews,
)
from dataset.genres import (
    GenreClassification,
    apple_genre,
    classify_genres,
    musicbrainz_tag_genres,
    normalize_genre_label,
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
    parser.add_argument("--discovery-workers", type=int, default=12)
    parser.add_argument(
        "--include-radio-diversity",
        action="store_true",
        help="Use LB Radio only if primary discovery sources do not produce enough identities",
    )
    parser.add_argument("--preview-workers", type=int, default=8)
    parser.add_argument("--match-batch-size", type=int, default=64)
    parser.add_argument(
        "--backfill-countries",
        action="store_true",
        help="Resumably attach explicit MusicBrainz artist countries to existing songs",
    )
    parser.add_argument(
        "--backfill-artists",
        action="store_true",
        help="Resumably attach structured MusicBrainz artist credits to existing songs",
    )
    parser.add_argument(
        "--backfill-streaming-links",
        action="store_true",
        help="Backfill exact Apple Music and MusicBrainz-linked Spotify song URLs",
    )
    parser.add_argument(
        "--backfill-explicit-versions",
        action="store_true",
        help="Resumably replace clean Apple matches with verified explicit equivalents",
    )
    parser.add_argument(
        "--explicitness-stale-days",
        type=int,
        default=30,
        help="Recheck Apple explicitness after this many days",
    )
    parser.add_argument(
        "--spotify-link-limit",
        type=int,
        help="Limit missing Spotify relationships checked in this run; omit for all",
    )
    genre_action = parser.add_mutually_exclusive_group()
    genre_action.add_argument(
        "--audit-genres",
        action="store_true",
        help="Compare current genres with evidence-aware classifications without changing data",
    )
    genre_action.add_argument(
        "--backfill-genres",
        action="store_true",
        help="Replace current genre links with evidence-aware ranked classifications",
    )
    parser.add_argument(
        "--genre-report",
        type=Path,
        help="Write the full genre audit report as JSON (defaults inside the ignored cache)",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    _validate_args(args)
    initialize_database(args.database)
    if args.backfill_countries:
        backfill_catalog_countries(args.database, args.cache)
        return
    if args.backfill_artists:
        backfill_catalog_artists(args.database, args.cache)
        return
    if args.backfill_streaming_links:
        backfill_catalog_streaming_links(
            args.database,
            args.cache,
            country=args.country,
            spotify_limit=args.spotify_link_limit,
        )
        return
    if args.backfill_explicit_versions:
        backfill_catalog_explicit_versions(
            args.database,
            args.cache,
            country=args.country,
            stale_after_days=args.explicitness_stale_days,
            preview_workers=args.preview_workers,
        )
        return
    if args.audit_genres or args.backfill_genres:
        audit_catalog_genres(
            args.database,
            args.cache,
            country=args.country,
            apply=args.backfill_genres,
            report_path=args.genre_report,
        )
        return
    initial_state = catalog_state(args.database)
    known_musicbrainz_ids, _known_apple_track_ids = known_catalog_identities(args.database)
    needed = target_gap(args.database, args.target_total)
    if needed < 0:
        raise SystemExit(
            f"Catalog already has {initial_state.enabled_count:,} enabled songs, above the "
            f"requested total of {args.target_total:,}; refusing to remove existing songs."
        )
    if needed == 0:
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
        include_radio_diversity=args.include_radio_diversity,
        discovery_workers=args.discovery_workers,
    )
    progress.candidates_discovered = len(candidates)
    available_candidates = [
        candidate
        for candidate in candidates
        if candidate["recording_mbid"] not in known_musicbrainz_ids
        and not has_fresh_negative_apple_match(
            args.cache, candidate["recording_mbid"], country=args.country
        )
    ]
    enrichment_budget = max(needed * 4, needed + 250)
    new_candidates = available_candidates[:enrichment_budget]
    print(
        f"Candidates discovered: {len(candidates):,}; not already known: "
        f"{len(available_candidates):,}; enriching this pass: {len(new_candidates):,}",
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
    eligible_metadata = {
        candidate["recording_mbid"]: metadata.get(candidate["recording_mbid"], {})
        for candidate in eligible
    }
    print("Fetching explicit MusicBrainz countries for every credited artist...")
    countries_by_recording = fetch_musicbrainz_artist_countries(args.cache, eligible_metadata)

    try:
        import_until_target(
            args.database,
            args.cache,
            eligible,
            metadata,
            countries_by_recording,
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
        f"added {progress.new_validated_songs:,}; failures {progress.failures:,}. "
        "Run the Spotify stream backfill to link and score new rows.",
        flush=True,
    )


def import_until_target(
    database_path: Path,
    cache_dir: Path,
    candidates: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    countries_by_recording: dict[str, list[str]],
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
        artist_representatives: dict[str, dict[str, Any]] = {}

        def candidate_artist_key(candidate: dict[str, Any]) -> str:
            recording = metadata.get(candidate["recording_mbid"], {})
            credited_artist = "".join(
                credit.get("name", "") + credit.get("joinphrase", "")
                for credit in recording.get("artist-credit", [])
                if isinstance(credit, dict)
            )
            return (credited_artist or candidate.get("artist_name", "")).casefold()

        for candidate in chunk:
            artist_representatives.setdefault(candidate_artist_key(candidate), candidate)

        def prefetch_artist(candidate: dict[str, Any]) -> None:
            mbid = candidate["recording_mbid"]
            search_apple_track(
                cache_dir,
                candidate,
                metadata.get(mbid, {}),
                country=country,
                year_min=year_min,
                year_max=year_max,
            )

        # Requests remain globally spaced by _throttle_apple; workers only overlap
        # response latency, and one representative prevents duplicate artist searches.
        with ThreadPoolExecutor(
            max_workers=max(1, min(preview_workers, len(artist_representatives)))
        ) as executor:
            futures = {
                executor.submit(prefetch_artist, candidate): artist_key
                for artist_key, candidate in artist_representatives.items()
            }
            failed_artist_keys: set[str] = set()
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:  # noqa: BLE001 - later runs retry uncached artists.
                    failed_artist_keys.add(futures[future])

        apple_matches: list[dict[str, Any]] = []
        for rank, candidate in enumerate(chunk, start=start):
            progress.candidates_checked += 1
            mbid = candidate["recording_mbid"]
            if candidate_artist_key(candidate) in failed_artist_keys:
                progress.apple_failures += 1
                continue
            try:
                apple = search_apple_track(
                    cache_dir,
                    candidate,
                    metadata.get(mbid, {}),
                    country=country,
                    year_min=year_min,
                    year_max=year_max,
                )
            except Exception as error:  # noqa: BLE001 - one provider failure must not stop import.
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
        inserted = write_catalog(
            database_path,
            selected,
            metadata,
            countries_by_recording=countries_by_recording,
        )
        progress.new_validated_songs += inserted
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


def known_catalog_identities(database_path: Path) -> tuple[set[str], set[str]]:
    """Return enabled and disabled identities so resumable population never revives rejects."""
    with sqlite3.connect(database_path) as connection:
        musicbrainz_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT musicbrainz_id FROM songs WHERE musicbrainz_id IS NOT NULL"
            )
        }
        apple_track_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT apple_track_id FROM songs WHERE apple_track_id IS NOT NULL"
            )
        }
    return musicbrainz_ids, apple_track_ids


def target_gap(database_path: Path, target_total: int) -> int:
    """Return how many enabled songs must be added to reach a final total."""
    return target_total - catalog_state(database_path).enabled_count


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {str(row[0]) for row in connection.execute("SELECT name FROM schema_migrations")}
        for migration_path in sorted((REPOSITORY_DIR / "migrations").glob("*.sql")):
            if migration_path.name in applied:
                continue
            connection.executescript(migration_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (name, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (migration_path.name,),
            )


def write_catalog(
    database_path: Path,
    songs: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    *,
    countries_by_recording: dict[str, list[str]] | None = None,
) -> int:
    enabled_delta = 0
    explicitness_checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
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
                mbid,
                apple_id,
                apple["previewUrl"],
                artwork,
                apple.get("trackViewUrl"),
                stored_apple_explicitness(apple),
                explicitness_checked_at,
            )
            if existing_mbid:
                connection.execute(
                    "UPDATE songs SET title = ?, artist = ?, album = ?, release_year = ?, "
                    "musicbrainz_id = ?, apple_track_id = ?, preview_url = ?, "
                    "artwork_url = ?, apple_music_url = ?, apple_explicitness = ?, "
                    "apple_explicitness_checked_at = ?, enabled = 1 WHERE id = ?",
                    (*values, existing_mbid[0]),
                )
                song_id = int(existing_mbid[0])
                enabled_delta += 1 - int(existing_mbid[1])
            else:
                cursor = connection.execute(
                    "INSERT INTO songs (title, artist, album, release_year, popularity_score, "
                    "stream_count, stream_count_fetched_at, stream_count_source, "
                    "stream_count_status, musicbrainz_id, apple_track_id, preview_url, "
                    "artwork_url, apple_music_url, apple_explicitness, "
                    "apple_explicitness_checked_at, enabled) VALUES "
                    "(?, ?, ?, ?, NULL, NULL, NULL, NULL, 'missing_link', ?, ?, ?, ?, ?, ?, ?, 1)",
                    values,
                )
                song_id = int(cursor.lastrowid)
                enabled_delta += 1

            _replace_song_genres(
                connection,
                song_id,
                classify_genres(apple, metadata.get(mbid, {})),
            )
            if countries_by_recording is not None:
                _replace_song_countries(
                    connection,
                    song_id,
                    countries_by_recording.get(mbid, []),
                )
            _replace_song_artists(connection, song_id, metadata.get(mbid, {}))
        connection.execute(
            "DELETE FROM genres WHERE NOT EXISTS "
            "(SELECT 1 FROM song_genres WHERE genre_id = genres.id)"
        )
        connection.execute(
            "DELETE FROM countries WHERE NOT EXISTS "
            "(SELECT 1 FROM song_countries WHERE country_id = countries.id)"
        )
    return enabled_delta


def backfill_catalog_streaming_links(
    database_path: Path,
    cache_dir: Path,
    *,
    country: str = "US",
    spotify_limit: int | None = None,
    spotify_batch_size: int = 25,
    spotify_fetcher: Callable[..., dict[str, str | None]] = fetch_musicbrainz_spotify_urls,
) -> dict[str, int]:
    """Persist exact provider URLs without using title search or fuzzy matching."""
    if spotify_limit is not None and spotify_limit < 0:
        raise ValueError("spotify_limit cannot be negative")
    if spotify_batch_size < 1:
        raise ValueError("spotify_batch_size must be positive")
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        songs = [
            {
                "id": int(row[0]),
                "recording_mbid": str(row[1]),
                "apple_track_id": str(row[2]),
                "apple_music_url": row[3],
                "spotify_url": row[4],
            }
            for row in connection.execute(
                "SELECT id, musicbrainz_id, apple_track_id, apple_music_url, spotify_url "
                "FROM songs WHERE enabled = 1 AND musicbrainz_id IS NOT NULL "
                "AND apple_track_id IS NOT NULL ORDER BY id"
            )
        ]

    apple_updates: list[tuple[str, int]] = []
    for song in songs:
        apple = read_cached_apple_track(cache_dir, str(song["recording_mbid"]), country=country)
        if (
            apple
            and str(apple.get("trackId") or "") == song["apple_track_id"]
            and isinstance(apple.get("trackViewUrl"), str)
            and apple["trackViewUrl"].startswith("https://music.apple.com/")
        ):
            apple_updates.append((str(apple["trackViewUrl"]), int(song["id"])))
    with sqlite3.connect(database_path) as connection:
        connection.executemany("UPDATE songs SET apple_music_url = ? WHERE id = ?", apple_updates)

    pending_spotify = [song for song in songs if not song["spotify_url"]]
    if spotify_limit is not None:
        pending_spotify = pending_spotify[:spotify_limit]
    spotify_linked = 0
    spotify_checked = 0
    for start in range(0, len(pending_spotify), spotify_batch_size):
        batch = pending_spotify[start : start + spotify_batch_size]
        mbids = [str(song["recording_mbid"]) for song in batch]
        urls = spotify_fetcher(mbids)
        updates = [
            (url, int(song["id"]))
            for song in batch
            if (url := urls.get(str(song["recording_mbid"]))) is not None
        ]
        with sqlite3.connect(database_path) as connection:
            connection.executemany("UPDATE songs SET spotify_url = ? WHERE id = ?", updates)
        spotify_linked += len(updates)
        spotify_checked += len(batch)
        print(
            f"  Spotify relationships {spotify_checked:,}/{len(pending_spotify):,}; "
            f"exact links {spotify_linked:,}",
            flush=True,
        )

    summary = {
        "songs": len(songs),
        "apple_music_links": len(apple_updates),
        "spotify_relationships_checked": spotify_checked,
        "spotify_links_added": spotify_linked,
    }
    print(f"Streaming-link backfill complete: {summary}", flush=True)
    return summary


def _fresh_iso_timestamp(value: str | None, stale_after_days: int) -> bool:
    if not value:
        return False
    try:
        checked_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return checked_at >= datetime.now(UTC) - timedelta(days=stale_after_days)


def backfill_catalog_explicit_versions(
    database_path: Path,
    cache_dir: Path,
    *,
    country: str = "US",
    stale_after_days: int = 30,
    preview_workers: int = 8,
) -> dict[str, int]:
    """Refresh explicitness and replace clean Apple tracks with exact explicit alternates."""
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, musicbrainz_id, apple_track_id, apple_explicitness_checked_at "
            "FROM songs WHERE apple_track_id IS NOT NULL ORDER BY id"
        ).fetchall()
    pending = [row for row in rows if not _fresh_iso_timestamp(row[3], stale_after_days)]
    print(
        f"Apple explicitness: {len(rows):,} songs; {len(rows) - len(pending):,} fresh; "
        f"{len(pending):,} queued.",
        flush=True,
    )
    if not pending:
        return {
            "songs": len(rows),
            "checked": 0,
            "upgraded_to_explicit": 0,
            "clean_without_explicit_alternate": 0,
            "conflicts": 0,
            "failures": 0,
        }

    current_tracks = fetch_apple_tracks_by_ids([str(row[2]) for row in pending], country=country)
    checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    checked: list[tuple[str, str, int]] = []
    proposed: list[tuple[int, str, dict[str, Any]]] = []
    clean_tracks: dict[int, dict[str, Any]] = {}
    mbids_by_song: dict[int, str] = {}
    clean_without_alternate = 0
    failures = 0
    for index, (song_id, mbid, apple_track_id, _previous_check) in enumerate(pending, start=1):
        current = current_tracks.get(str(apple_track_id)) or read_cached_apple_track(
            cache_dir, str(mbid), country=country
        )
        if not current:
            failures += 1
            continue
        explicitness = stored_apple_explicitness(current)
        if explicitness == "cleaned":
            clean_tracks[int(song_id)] = current
            mbids_by_song[int(song_id)] = str(mbid)
        else:
            checked.append((explicitness, checked_at, int(song_id)))
        if index % 25 == 0 or index == len(pending):
            print(
                f"  Explicitness checked {index:,}/{len(pending):,}; "
                f"clean candidates {len(clean_tracks):,}",
                flush=True,
            )

    # Checkpoint the rows that need no alternate lookup. If Apple page work is
    # interrupted, the next run resumes from only the clean/unresolved subset.
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "UPDATE songs SET apple_explicitness = ?, "
            "apple_explicitness_checked_at = ? WHERE id = ?",
            checked,
        )

    explicit_alternates, checked_clean_ids = find_explicit_apple_equivalents(
        clean_tracks,
        country=country,
        max_workers=preview_workers,
    )
    failures += len(clean_tracks) - len(checked_clean_ids)
    for song_id in checked_clean_ids:
        checked.append(("cleaned", checked_at, song_id))
        alternate = explicit_alternates.get(song_id)
        if alternate:
            proposed.append((song_id, mbids_by_song[song_id], alternate))
        else:
            clean_without_alternate += 1
    print(
        f"  Clean alternates checked {len(checked_clean_ids):,}/{len(clean_tracks):,}; "
        f"explicit upgrades found {len(proposed):,}",
        flush=True,
    )

    preview_statuses = validate_previews(
        cache_dir,
        [track["previewUrl"] for _song_id, _mbid, track in proposed],
        max_workers=preview_workers,
    )
    upgraded = 0
    conflicts = 0
    valid_replacements: list[tuple[int, str, dict[str, Any]]] = []
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for song_id, mbid, track in proposed:
            if preview_statuses.get(track["previewUrl"]) != "valid":
                failures += 1
                continue
            existing = connection.execute(
                "SELECT id FROM songs WHERE apple_track_id = ? AND id <> ?",
                (str(track["trackId"]), song_id),
            ).fetchone()
            if existing:
                conflicts += 1
                continue
            artwork = track.get("artworkUrl100")
            if artwork:
                artwork = artwork.replace("100x100bb", "600x600bb")
            connection.execute(
                "UPDATE songs SET album = ?, apple_track_id = ?, preview_url = ?, "
                "artwork_url = ?, apple_music_url = ?, apple_explicitness = 'explicit', "
                "apple_explicitness_checked_at = ? WHERE id = ?",
                (
                    track.get("collectionName"),
                    str(track["trackId"]),
                    track["previewUrl"],
                    artwork,
                    track.get("trackViewUrl"),
                    checked_at,
                    song_id,
                ),
            )
            valid_replacements.append((song_id, mbid, track))
            upgraded += 1
        replacement_ids = {song_id for song_id, _mbid, _track in valid_replacements}
        connection.executemany(
            "UPDATE songs SET apple_explicitness = ?, "
            "apple_explicitness_checked_at = ? WHERE id = ?",
            [row for row in checked if row[2] not in replacement_ids],
        )
    for _song_id, mbid, track in valid_replacements:
        cache_apple_track(cache_dir, mbid, track, country=country)

    summary = {
        "songs": len(rows),
        "checked": len(checked),
        "upgraded_to_explicit": upgraded,
        "clean_without_explicit_alternate": clean_without_alternate,
        "conflicts": conflicts,
        "failures": failures,
    }
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def _replace_song_genres(
    connection: sqlite3.Connection,
    song_id: int,
    classifications: list[GenreClassification],
) -> None:
    connection.execute("DELETE FROM song_genre_evidence WHERE song_id = ?", (song_id,))
    connection.execute("DELETE FROM song_genres WHERE song_id = ?", (song_id,))
    for rank, classification in enumerate(classifications, start=1):
        connection.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (classification.name,))
        genre_id = connection.execute(
            "SELECT id FROM genres WHERE name = ?", (classification.name,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO song_genres (song_id, genre_id) VALUES (?, ?)",
            (song_id, genre_id),
        )
        connection.execute(
            "INSERT INTO song_genre_evidence (song_id, genre_id, rank, score, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                song_id,
                genre_id,
                rank,
                classification.score,
                classification.source,
            ),
        )


def audit_catalog_genres(
    database_path: Path,
    cache_dir: Path,
    *,
    country: str = "US",
    apply: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Audit or rebuild catalog genres from cached Apple and MusicBrainz evidence."""
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        songs = [
            {
                "id": int(row[0]),
                "title": str(row[1]),
                "artist": str(row[2]),
                "recording_mbid": str(row[3]),
                "apple_track_id": str(row[4]),
                "popularity_score": int(row[5] or 0),
            }
            for row in connection.execute(
                "SELECT id, title, artist, musicbrainz_id, apple_track_id, popularity_score "
                "FROM songs WHERE enabled = 1 AND musicbrainz_id IS NOT NULL "
                "AND apple_track_id IS NOT NULL ORDER BY id"
            )
        ]
        current_genres: dict[int, list[str]] = {}
        for row in connection.execute(
            "SELECT sg.song_id, g.name FROM song_genres sg "
            "JOIN genres g ON g.id = sg.genre_id "
            "LEFT JOIN song_genre_evidence e "
            "ON e.song_id = sg.song_id AND e.genre_id = sg.genre_id "
            "ORDER BY sg.song_id, COALESCE(e.rank, 999), g.name"
        ):
            current_genres.setdefault(int(row[0]), []).append(str(row[1]))

    candidates = [{"recording_mbid": song["recording_mbid"]} for song in songs]
    metadata = fetch_musicbrainz_metadata(cache_dir, candidates)
    before_counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    before_sizes: Counter[int] = Counter()
    after_sizes: Counter[int] = Counter()
    unmapped_apple: Counter[str] = Counter()
    unmapped_tags: Counter[str] = Counter()
    ignored_nonpositive_tags = 0
    unresolved: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    proposed: dict[int, list[GenreClassification]] = {}

    for song in songs:
        song_id = int(song["id"])
        before = current_genres.get(song_id, [])
        before_counts.update(before)
        before_sizes[len(before)] += 1

        recording_mbid = str(song["recording_mbid"])
        recording = metadata.get(recording_mbid, {})
        apple_track = read_cached_apple_track(cache_dir, recording_mbid, country=country)
        if apple_track is None or str(apple_track.get("trackId") or "") != song["apple_track_id"]:
            unresolved.append(
                {
                    "song_id": song_id,
                    "recording_mbid": recording_mbid,
                    "reason": "missing or mismatched Apple cache entry",
                }
            )
            after_counts.update(before)
            after_sizes[len(before)] += 1
            continue

        apple_label = str(apple_track.get("primaryGenreName") or "").strip()
        if apple_label and apple_genre(apple_label) is None:
            unmapped_apple[apple_label] += 1
        for tag in recording.get("tags", []):
            if not isinstance(tag, dict):
                continue
            try:
                votes = int(tag.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if votes <= 0:
                ignored_nonpositive_tags += 1
            elif not musicbrainz_tag_genres(tag.get("name")):
                normalized_tag = normalize_genre_label(tag.get("name"))
                if normalized_tag:
                    unmapped_tags[normalized_tag] += 1

        classifications = classify_genres(apple_track, recording)
        proposed[song_id] = classifications
        after = [classification.name for classification in classifications]
        after_counts.update(after)
        after_sizes[len(after)] += 1
        if set(before) != set(after) or before != after:
            changes.append(
                {
                    "song_id": song_id,
                    "title": song["title"],
                    "artist": song["artist"],
                    "popularity_score": song["popularity_score"],
                    "before": before,
                    "after": after,
                    "removed": sorted(set(before) - set(after)),
                    "added": sorted(set(after) - set(before)),
                }
            )

    changes.sort(
        key=lambda change: (
            -(len(change["removed"]) + len(change["added"])),
            -int(change["popularity_score"]),
            str(change["title"]).casefold(),
        )
    )
    report = {
        "applied": apply,
        "song_count": len(songs),
        "changed_song_count": len(changes),
        "unresolved_song_count": len(unresolved),
        "ignored_nonpositive_tag_count": ignored_nonpositive_tags,
        "before_genres_per_song": {str(key): value for key, value in sorted(before_sizes.items())},
        "after_genres_per_song": {str(key): value for key, value in sorted(after_sizes.items())},
        "before_genre_counts": dict(sorted(before_counts.items())),
        "after_genre_counts": dict(sorted(after_counts.items())),
        "unmapped_apple_genres": dict(unmapped_apple.most_common()),
        "top_unmapped_positive_tags": dict(unmapped_tags.most_common(100)),
        "unresolved_songs": unresolved,
        "changes": changes,
    }

    if apply:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for song_id, classifications in proposed.items():
                _replace_song_genres(connection, song_id, classifications)
            connection.execute(
                "DELETE FROM genres WHERE NOT EXISTS "
                "(SELECT 1 FROM song_genres WHERE genre_id = genres.id)"
            )

    default_report_name = "genre-backfill-report.json" if apply else "genre-audit.json"
    active_report_path = report_path or cache_dir / default_report_name
    active_report_path.parent.mkdir(parents=True, exist_ok=True)
    active_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _print_genre_audit(report, active_report_path)
    return report


def _print_genre_audit(report: dict[str, Any], report_path: Path) -> None:
    print(
        f"Genre audit: {report['song_count']:,} songs; "
        f"changed {report['changed_song_count']:,}; "
        f"unresolved {report['unresolved_song_count']:,}; "
        f"ignored nonpositive tags {report['ignored_nonpositive_tag_count']:,}.",
        flush=True,
    )
    print(f"  Before genres/song: {report['before_genres_per_song']}", flush=True)
    print(f"  After genres/song:  {report['after_genres_per_song']}", flush=True)
    if report["unmapped_apple_genres"]:
        print(f"  Unmapped Apple genres: {report['unmapped_apple_genres']}", flush=True)
    print("  Largest proposed changes:", flush=True)
    for change in report["changes"][:15]:
        print(
            f"    {change['artist']} — {change['title']}: {change['before']} -> {change['after']}",
            flush=True,
        )
    print(f"  Full report: {report_path}", flush=True)


def _musicbrainz_artist_credits(recording: dict[str, Any]) -> list[dict[str, Any]]:
    """Return valid structured credits without interpreting display punctuation."""
    credits: list[dict[str, Any]] = []
    seen_artist_ids: set[str] = set()
    for credit_order, credit in enumerate(recording.get("artist-credit", [])):
        if not isinstance(credit, dict) or not isinstance(credit.get("artist"), dict):
            continue
        artist = credit["artist"]
        artist_id = str(artist.get("id") or "").strip()
        canonical_name = str(artist.get("name") or "").strip()
        credited_name = str(credit.get("name") or canonical_name).strip()
        if not artist_id or not canonical_name or not credited_name or artist_id in seen_artist_ids:
            continue
        seen_artist_ids.add(artist_id)
        credits.append(
            {
                "credit_order": credit_order,
                "musicbrainz_id": artist_id,
                "name": canonical_name,
                "sort_name": _optional_text(artist.get("sort-name")),
                "disambiguation": _optional_text(artist.get("disambiguation")),
                "credited_name": credited_name,
                "join_phrase": str(credit.get("joinphrase") or ""),
            }
        )
    return credits


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _replace_song_artists(
    connection: sqlite3.Connection,
    song_id: int,
    recording: dict[str, Any],
) -> bool:
    credits = _musicbrainz_artist_credits(recording)
    connection.execute("DELETE FROM song_artists WHERE song_id = ?", (song_id,))
    for credit in credits:
        connection.execute(
            "INSERT INTO artists (musicbrainz_id, name, sort_name, disambiguation) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(musicbrainz_id) DO UPDATE SET "
            "name = excluded.name, sort_name = excluded.sort_name, "
            "disambiguation = excluded.disambiguation",
            (
                credit["musicbrainz_id"],
                credit["name"],
                credit["sort_name"],
                credit["disambiguation"],
            ),
        )
        artist_id = connection.execute(
            "SELECT id FROM artists WHERE musicbrainz_id = ?",
            (credit["musicbrainz_id"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO song_artists "
            "(song_id, artist_id, credit_order, credited_name, join_phrase) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                song_id,
                artist_id,
                credit["credit_order"],
                credit["credited_name"],
                credit["join_phrase"],
            ),
        )
    return bool(credits)


def backfill_catalog_artists(
    database_path: Path,
    cache_dir: Path,
    *,
    batch_size: int = 500,
) -> None:
    """Attach structured recording credits while leaving Apple display credits unchanged."""
    with sqlite3.connect(database_path) as connection:
        songs = [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT id, musicbrainz_id FROM songs WHERE musicbrainz_id IS NOT NULL ORDER BY id"
            )
        ]
    progress_connection = _artist_backfill_connection(cache_dir)
    database_key = _database_cache_key(database_path)
    completed_songs = {
        int(row[0]): str(row[1])
        for row in progress_connection.execute(
            "SELECT song_id, recording_mbid FROM completed WHERE database_key = ?",
            (database_key,),
        )
    }
    pending_songs = [row for row in songs if completed_songs.get(row[0]) != row[1]]
    print(
        f"Backfilling structured artist credits for {len(songs):,} existing songs; "
        f"already complete {len(songs) - len(pending_songs):,}; "
        f"remaining {len(pending_songs):,}...",
        flush=True,
    )
    linked_songs = 0
    unresolved: list[tuple[int, str]] = []
    try:
        for start in range(0, len(pending_songs), batch_size):
            batch = pending_songs[start : start + batch_size]
            candidates = [{"recording_mbid": mbid} for _, mbid in batch]
            metadata = fetch_musicbrainz_metadata(cache_dir, candidates)
            completed_batch: list[tuple[int, str]] = []
            unresolved_batch: list[tuple[int, str]] = []
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for song_id, mbid in batch:
                    recording = metadata.get(mbid, {})
                    if not _musicbrainz_artist_credits(recording):
                        unresolved_batch.append((song_id, mbid))
                        continue
                    _replace_song_artists(connection, song_id, recording)
                    completed_batch.append((song_id, mbid))
                    linked_songs += 1
            with progress_connection:
                progress_connection.executemany(
                    "INSERT OR REPLACE INTO completed "
                    "(database_key, song_id, recording_mbid, completed_at) VALUES (?, ?, ?, ?)",
                    [
                        (database_key, song_id, mbid, time.time())
                        for song_id, mbid in completed_batch
                    ],
                )
                progress_connection.executemany(
                    "INSERT OR REPLACE INTO unresolved "
                    "(database_key, song_id, recording_mbid, attempted_at) VALUES (?, ?, ?, ?)",
                    [
                        (database_key, song_id, mbid, time.time())
                        for song_id, mbid in unresolved_batch
                    ],
                )
                progress_connection.executemany(
                    "DELETE FROM unresolved WHERE database_key = ? AND song_id = ?",
                    [(database_key, song_id) for song_id, _ in completed_batch],
                )
            unresolved.extend(unresolved_batch)
            processed = min(start + batch_size, len(pending_songs))
            print(
                f"  Artist backfill {processed:,}/{len(pending_songs):,} remaining; "
                f"linked {linked_songs:,}; unresolved {len(unresolved):,}",
                flush=True,
            )
    finally:
        progress_connection.close()

    if unresolved:
        print("Artist credits could not be resolved for:", flush=True)
        for song_id, mbid in unresolved:
            print(f"  song_id={song_id} recording_mbid={mbid}", flush=True)


def backfill_catalog_countries(
    database_path: Path,
    cache_dir: Path,
    *,
    batch_size: int = 500,
) -> None:
    """Attach explicit credited-artist countries without changing song or ranking data."""
    with sqlite3.connect(database_path) as connection:
        songs = [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT id, musicbrainz_id FROM songs WHERE musicbrainz_id IS NOT NULL ORDER BY id"
            )
        ]
    progress_connection = _country_backfill_connection(cache_dir)
    database_key = _database_cache_key(database_path)
    completed_mbids = {
        str(row[0])
        for row in progress_connection.execute(
            "SELECT recording_mbid FROM completed WHERE database_key = ?", (database_key,)
        )
    }
    pending_songs = [(song_id, mbid) for song_id, mbid in songs if mbid not in completed_mbids]
    print(
        f"Backfilling artist-origin countries for {len(songs):,} existing songs; "
        f"already complete {len(songs) - len(pending_songs):,}; "
        f"remaining {len(pending_songs):,}...",
        flush=True,
    )
    linked_songs = 0
    missing_country = 0
    try:
        for start in range(0, len(pending_songs), batch_size):
            batch = pending_songs[start : start + batch_size]
            candidates = [{"recording_mbid": mbid} for _, mbid in batch]
            metadata = fetch_musicbrainz_metadata(cache_dir, candidates)
            countries_by_recording = fetch_musicbrainz_artist_countries(cache_dir, metadata)
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                for song_id, mbid in batch:
                    countries = countries_by_recording.get(mbid, [])
                    _replace_song_countries(connection, song_id, countries)
                    if countries:
                        linked_songs += 1
                    else:
                        missing_country += 1
                connection.execute(
                    "DELETE FROM countries WHERE NOT EXISTS "
                    "(SELECT 1 FROM song_countries WHERE country_id = countries.id)"
                )
            with progress_connection:
                progress_connection.executemany(
                    "INSERT OR REPLACE INTO completed "
                    "(database_key, recording_mbid, completed_at) VALUES (?, ?, ?)",
                    [(database_key, mbid, time.time()) for _, mbid in batch],
                )
            completed = min(start + batch_size, len(pending_songs))
            print(
                f"  Country backfill {completed:,}/{len(pending_songs):,} remaining; "
                f"with explicit country {linked_songs:,}; without {missing_country:,}",
                flush=True,
            )
    finally:
        progress_connection.close()


def _replace_song_countries(
    connection: sqlite3.Connection,
    song_id: int,
    country_codes: list[str],
) -> None:
    connection.execute("DELETE FROM song_countries WHERE song_id = ?", (song_id,))
    normalized_codes = sorted(
        {
            normalized
            for code in country_codes
            if len(normalized := code.strip().upper()) == 2 and normalized.isalpha()
        }
    )
    for code in normalized_codes:
        connection.execute("INSERT OR IGNORE INTO countries (code) VALUES (?)", (code,))
        country_id = connection.execute(
            "SELECT id FROM countries WHERE code = ?", (code,)
        ).fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO song_countries (song_id, country_id) VALUES (?, ?)",
            (song_id, country_id),
        )


def _country_backfill_connection(cache_dir: Path) -> sqlite3.Connection:
    path = cache_dir / "country-backfill.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS completed ("
        "database_key TEXT NOT NULL, recording_mbid TEXT NOT NULL, "
        "completed_at REAL NOT NULL, PRIMARY KEY (database_key, recording_mbid))"
    )
    return connection


def _artist_backfill_connection(cache_dir: Path) -> sqlite3.Connection:
    path = cache_dir / "artist-backfill.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS completed ("
        "database_key TEXT NOT NULL, song_id INTEGER NOT NULL, recording_mbid TEXT NOT NULL, "
        "completed_at REAL NOT NULL, PRIMARY KEY (database_key, song_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS unresolved ("
        "database_key TEXT NOT NULL, song_id INTEGER NOT NULL, recording_mbid TEXT NOT NULL, "
        "attempted_at REAL NOT NULL, PRIMARY KEY (database_key, song_id))"
    )
    return connection


def _database_cache_key(database_path: Path) -> str:
    stat = database_path.stat()
    return f"{database_path.resolve()}:{stat.st_dev}:{stat.st_ino}"


def percentile_scores(values: dict[int, int]) -> dict[int, float]:
    """Return tie-aware 0-100 percentiles for the supplied catalog values."""
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): 100.0}

    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    result: dict[int, float] = {}
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        average_rank = (index + end) / 2
        percentile = 100 * average_rank / (len(ordered) - 1)
        for position in range(index, end + 1):
            result[ordered[position][0]] = percentile
        index = end + 1
    return result


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
    if not 1 <= args.artist_count <= 10000:
        raise SystemExit("--artist-count must be between 1 and 10000")
    if args.recordings_per_artist < 1 or args.preview_workers < 1 or args.match_batch_size < 1:
        raise SystemExit("recording, preview worker, and batch counts must be positive")
    if args.spotify_link_limit is not None and args.spotify_link_limit < 0:
        raise SystemExit("spotify link limit cannot be negative")
    if args.explicitness_stale_days < 1:
        raise SystemExit("explicitness-stale-days must be positive")


def _year_in_range(metadata: dict[str, Any], year_min: int, year_max: int) -> bool:
    value = metadata.get("first-release-date", "")
    return len(value) >= 4 and value[:4].isdigit() and year_min <= int(value[:4]) <= year_max


if __name__ == "__main__":
    main()
