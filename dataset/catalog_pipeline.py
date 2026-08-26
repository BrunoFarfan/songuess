"""Explicit append-only catalog operations for discovery, population, and D1 deltas."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataset.clients import (
    fetch_listenbrainz_candidates,
    fetch_listenbrainz_popular_artists,
    fetch_musicbrainz_artist_countries,
    fetch_musicbrainz_metadata,
    has_fresh_negative_apple_match,
)
from dataset.metrics import finish_run, start_run
from dataset.populate import (
    DEFAULT_CACHE,
    DEFAULT_DATABASE,
    Progress,
    catalog_state,
    import_until_target,
    initialize_database,
    known_catalog_identities,
)
from dataset.verify import snapshot_catalog, verify_catalog

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_DIR / "dataset" / "cache" / "candidate-manifest-10000.json"
DEFAULT_SNAPSHOT = REPOSITORY_DIR / "dataset" / "reports" / "catalog-snapshot-5000.json"
DEFAULT_DELTA = REPOSITORY_DIR / "dataset" / "reports" / "catalog-delta-5000-10000.json"
DEFAULT_EVALUATION = REPOSITORY_DIR / "dataset" / "reports" / "catalog-evaluation-10000.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("discover", "populate", "refresh", "verify", "export-delta", "evaluate"),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-total", type=int, default=10000)
    parser.add_argument("--candidate-count", type=int, default=30000)
    parser.add_argument("--artist-count", type=int, default=5000)
    parser.add_argument("--recordings-per-artist", type=int, default=15)
    parser.add_argument("--discovery-workers", type=int, default=12)
    parser.add_argument("--checkpoint-size", type=int, default=1000)
    parser.add_argument("--country", default="US")
    parser.add_argument("--year-min", type=int, default=1950)
    parser.add_argument("--year-max", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--preview-workers", type=int, default=8)
    parser.add_argument("--match-batch-size", type=int, default=64)
    parser.add_argument("--browser-workers", type=int, default=12)
    return parser.parse_args()


def _existing_artist_counts(database: Path) -> Counter[str]:
    with sqlite3.connect(database) as connection:
        return Counter(
            {
                str(mbid): int(count)
                for mbid, count in connection.execute(
                    "SELECT a.musicbrainz_id, COUNT(*) FROM song_artists sa "
                    "JOIN artists a ON a.id = sa.artist_id JOIN songs s ON s.id = sa.song_id "
                    "WHERE s.enabled = 1 GROUP BY a.musicbrainz_id"
                )
            }
        )


def discover(args: argparse.Namespace) -> dict[str, Any]:
    initialize_database(args.database)
    if not args.snapshot.exists():
        snapshot_catalog(args.database, args.snapshot)
    known_mbids, _known_apple_ids = known_catalog_identities(args.database)
    artist_counts = _existing_artist_counts(args.database)
    popular_artists = fetch_listenbrainz_popular_artists(args.cache, args.artist_count)
    candidates = fetch_listenbrainz_candidates(
        args.cache,
        args.candidate_count,
        artist_count=args.artist_count,
        recordings_per_artist=args.recordings_per_artist,
        include_radio_diversity=False,
        discovery_workers=args.discovery_workers,
    )
    manifest_candidates: list[dict[str, Any]] = []
    for discovery_rank, candidate in enumerate(candidates, start=1):
        recording_mbid = str(candidate.get("recording_mbid") or "")
        if not recording_mbid or recording_mbid in known_mbids:
            continue
        artist_mbids = [str(value) for value in candidate.get("artist_mbids") or []]
        existing_artist_songs = max(
            (artist_counts.get(artist_mbid, 0) for artist_mbid in artist_mbids), default=0
        )
        sources = list(candidate.get("discovery_sources") or [])
        score = (
            1_000_000
            - discovery_rank
            + 1_000 * max(0, len(sources) - 1)
            - 5_000 * existing_artist_songs
        )
        manifest_candidates.append(
            {
                "recording_mbid": recording_mbid,
                "artist_mbids": artist_mbids,
                "artist_name": candidate.get("artist_name"),
                "track_name": candidate.get("track_name"),
                "release_mbid": candidate.get("release_mbid"),
                "release_name": candidate.get("release_name"),
                "discovery_rank": discovery_rank,
                "discovery_sources": sources,
                "existing_artist_song_count": existing_artist_songs,
                "coverage": {
                    "genre": None,
                    "decade": None,
                    "country": None,
                    "language": None,
                    "status": "deferred_until_structured_metadata",
                },
                "score": score,
                "reason": ("multi-window support; " if len(sources) > 1 else "")
                + (
                    "new or lightly represented artist"
                    if existing_artist_songs <= 2
                    else f"artist representation penalty ({existing_artist_songs})"
                ),
            }
        )
    manifest_candidates.sort(
        key=lambda item: (-int(item["score"]), int(item["discovery_rank"]), item["recording_mbid"])
    )
    required_new = max(0, args.target_total - catalog_state(args.database).enabled_count)
    status = "ready" if len(manifest_candidates) >= required_new * 2 else "insufficient"
    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "target_total": args.target_total,
        "required_new_songs": required_new,
        "strategy": {
            "sitewide_time_windows": True,
            "popular_artist_target": args.artist_count,
            "popular_artists_discovered": len(popular_artists),
            "recordings_per_artist": args.recordings_per_artist,
            "artist_expansion_source": "listenbrainz_public_artist_radio",
            "listenbrainz_token_required": False,
            "artist_representation_penalty": 5000,
            "coverage_bonuses": "second-stage after structured metadata",
            "lb_radio_similar_artist_diversity": False,
        },
        "candidate_count": len(manifest_candidates),
        "candidates": manifest_candidates,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"Manifest {status}: {len(manifest_candidates):,} new candidates for "
        f"{required_new:,} required; {args.manifest}"
    )
    return payload


def populate(args: argparse.Namespace) -> dict[str, Any]:
    initialize_database(args.database)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("status") != "ready":
        raise SystemExit("candidate manifest is not ready; resolve discovery gaps first")
    initial_count = catalog_state(args.database).enabled_count
    checkpoint_target = min(args.target_total, initial_count + args.checkpoint_size)
    if checkpoint_target <= initial_count:
        return {"before": initial_count, "after": initial_count, "accepted": 0}
    known_mbids, _known_apple_ids = known_catalog_identities(args.database)
    needed = checkpoint_target - initial_count
    candidates = [
        {
            "recording_mbid": item["recording_mbid"],
            "artist_mbids": item.get("artist_mbids") or [],
            "artist_name": item.get("artist_name") or "",
            "track_name": item.get("track_name") or "",
            "release_mbid": item.get("release_mbid") or "",
            "release_name": item.get("release_name") or "",
            "discovery_sources": item.get("discovery_sources") or [],
        }
        for item in manifest["candidates"]
        if item["recording_mbid"] not in known_mbids
        and not has_fresh_negative_apple_match(
            args.cache, item["recording_mbid"], country=args.country
        )
    ][: max(needed * 4, needed + 250)]
    metadata = fetch_musicbrainz_metadata(args.cache, candidates)
    eligible = [
        candidate
        for candidate in candidates
        if (
            (
                date := str(
                    metadata.get(candidate["recording_mbid"], {}).get("first-release-date") or ""
                )
            )
            and len(date) >= 4
            and date[:4].isdigit()
            and args.year_min <= int(date[:4]) <= args.year_max
        )
    ]
    countries = fetch_musicbrainz_artist_countries(
        args.cache,
        {
            candidate["recording_mbid"]: metadata[candidate["recording_mbid"]]
            for candidate in eligible
        },
    )
    progress = Progress(existing_songs=initial_count, eligible_songs=len(eligible))
    import_until_target(
        args.database,
        args.cache,
        eligible,
        metadata,
        countries,
        country=args.country,
        year_min=args.year_min,
        year_max=args.year_max,
        target_total=checkpoint_target,
        preview_workers=args.preview_workers,
        batch_size=args.match_batch_size,
        progress=progress,
    )
    after = catalog_state(args.database).enabled_count
    return {"before": initial_count, "after": after, "accepted": after - initial_count}


def refresh(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "-m",
        "dataset.spotify_streams_browser",
        "--database",
        str(args.database),
        "--browser-workers",
        str(args.browser_workers),
    ]
    subprocess.run(command, cwd=REPOSITORY_DIR, check=True)


def export_delta(database: Path, snapshot: Path, output: Path) -> dict[str, Any]:
    baseline = json.loads(snapshot.read_text(encoding="utf-8"))
    baseline_mbids = set(baseline["musicbrainz_ids"])
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        songs = [
            dict(row)
            for row in connection.execute("SELECT * FROM songs WHERE enabled = 1 ORDER BY id")
            if str(row["musicbrainz_id"]) not in baseline_mbids
        ]
        song_ids = [int(song["id"]) for song in songs]
        relations: dict[str, list[dict[str, Any]]] = {}
        for table in ("song_artists", "song_genres", "song_countries"):
            if not song_ids:
                relations[table] = []
                continue
            placeholders = ",".join("?" for _ in song_ids)
            relations[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE song_id IN ({placeholders}) ORDER BY song_id",
                    song_ids,
                )
            ]
        dimensions: dict[str, list[dict[str, Any]]] = {}
        for table, relation_table, foreign_key in (
            ("artists", "song_artists", "artist_id"),
            ("genres", "song_genres", "genre_id"),
            ("countries", "song_countries", "country_id"),
        ):
            ids = sorted(
                {
                    int(row[foreign_key])
                    for row in relations[relation_table]
                    if row.get(foreign_key) is not None
                }
            )
            if not ids:
                dimensions[table] = []
                continue
            placeholders = ",".join("?" for _ in ids)
            dimensions[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE id IN ({placeholders}) ORDER BY id",
                    ids,
                )
            ]
    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline_enabled_count": int(baseline["enabled_count"]),
        "new_song_count": len(songs),
        "songs": songs,
        "dimensions": dimensions,
        "relations": relations,
        "excludes": [
            "provider response caches",
            "discovery manifests",
            "population checkpoints",
            "pipeline telemetry",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"D1 application-data delta: {len(songs):,} songs; {output}")
    return payload


def evaluate(database: Path, metrics: Path, output: Path) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        enabled_count = int(
            connection.execute("SELECT COUNT(*) FROM songs WHERE enabled = 1").fetchone()[0]
        )
        years = {
            str(year): int(count)
            for year, count in connection.execute(
                "SELECT (release_year / 10) * 10, COUNT(*) FROM songs WHERE enabled = 1 "
                "GROUP BY 1 ORDER BY 1"
            )
        }
        artists = [
            dict(row)
            for row in connection.execute(
                "SELECT a.name, COUNT(*) song_count FROM song_artists sa "
                "JOIN artists a ON a.id=sa.artist_id JOIN songs s ON s.id=sa.song_id "
                "WHERE s.enabled=1 GROUP BY a.id ORDER BY song_count DESC, a.name LIMIT 25"
            )
        ]
        lowest = [
            dict(row)
            for row in connection.execute(
                "SELECT title, artist, stream_count FROM songs WHERE enabled=1 "
                "ORDER BY stream_count, id LIMIT 25"
            )
        ]
        genres = {
            str(name): int(count)
            for name, count in connection.execute(
                "SELECT g.name, COUNT(*) FROM song_genres sg JOIN genres g ON g.id=sg.genre_id "
                "JOIN songs s ON s.id=sg.song_id WHERE s.enabled=1 GROUP BY g.id "
                "ORDER BY COUNT(*) DESC, g.name"
            )
        }
        countries = {
            str(code): int(count)
            for code, count in connection.execute(
                "SELECT c.code, COUNT(*) FROM song_countries sc "
                "JOIN countries c ON c.id=sc.country_id JOIN songs s ON s.id=sc.song_id "
                "WHERE s.enabled=1 GROUP BY c.id ORDER BY COUNT(*) DESC, c.code"
            )
        }
        popularity = {
            str(bucket): int(count)
            for bucket, count in connection.execute(
                "SELECT (popularity_score / 10) * 10, COUNT(*) FROM songs WHERE enabled=1 "
                "GROUP BY 1 ORDER BY 1"
            )
        }
        duplicates = {
            provider: int(value)
            for provider, value in (
                (
                    "musicbrainz",
                    connection.execute(
                        "SELECT COUNT(*) - COUNT(DISTINCT musicbrainz_id) "
                        "FROM songs WHERE enabled=1"
                    ).fetchone()[0],
                ),
                (
                    "apple",
                    connection.execute(
                        "SELECT COUNT(*) - COUNT(DISTINCT apple_track_id) "
                        "FROM songs WHERE enabled=1"
                    ).fetchone()[0],
                ),
                (
                    "spotify",
                    connection.execute(
                        "SELECT COUNT(*) - COUNT(DISTINCT spotify_url) FROM songs WHERE enabled=1"
                    ).fetchone()[0],
                ),
            )
        }
        questionable_versions = [
            dict(row)
            for row in connection.execute(
                "SELECT id, title, artist, album FROM songs WHERE enabled=1 AND "
                "(lower(title) LIKE '%live%' OR lower(title) LIKE '%remix%' OR "
                "lower(title) LIKE '%instrumental%' OR lower(title) LIKE '%karaoke%') "
                "ORDER BY artist, title LIMIT 200"
            )
        ]
    metric_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []
    if metrics.exists():
        with sqlite3.connect(metrics) as connection:
            connection.row_factory = sqlite3.Row
            metric_rows = [
                dict(row) for row in connection.execute("SELECT * FROM provider_metrics")
            ]
            run_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT operation, started_at, finished_at, status, accepted_songs "
                    "FROM pipeline_runs ORDER BY started_at"
                )
            ]
    baseline_path = REPOSITORY_DIR / "dataset" / "reports" / "catalog-post-clean-5000.json"
    baseline = (
        json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    )
    baseline_count = int((baseline or {}).get("catalog", {}).get("songs_enabled", enabled_count))
    added_songs = max(0, enabled_count - baseline_count)
    cache_bytes = sum(path.stat().st_size for path in metrics.parent.rglob("*") if path.is_file())
    downloaded_bytes = sum(
        int(row["value"]) for row in metric_rows if row.get("metric") == "downloaded_bytes"
    )
    requests = sum(int(row["value"]) for row in metric_rows if row.get("metric") == "requests")
    per_song = None
    if baseline and added_songs:
        per_song = {
            "cache_bytes": (cache_bytes - int(baseline["cache_bytes"])) / added_songs,
            "database_bytes": (database.stat().st_size - int(baseline["catalog"]["database_bytes"]))
            / added_songs,
            "downloaded_bytes": downloaded_bytes / added_songs,
            "requests": requests / added_songs,
        }

    def projection(target: int) -> dict[str, int] | None:
        if per_song is None:
            return None
        additional = max(0, target - baseline_count)
        return {
            key: round(float(value) * additional)
            for key, value in per_song.items()
            if key.endswith("bytes")
        }

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "enabled_songs": enabled_count,
        "decades": years,
        "genres": genres,
        "countries": countries,
        "popularity_bands": popularity,
        "most_represented_artists": artists,
        "lowest_stream_songs": lowest,
        "duplicates": duplicates,
        "questionable_versions": questionable_versions,
        "storage": {
            "database_bytes": database.stat().st_size,
            "cache_bytes": cache_bytes,
            "added_since_baseline": added_songs,
            "per_added_song": per_song,
        },
        "provider_metrics": metric_rows,
        "pipeline_runs": run_rows,
        "projection": {
            "at_20000": projection(20000),
            "at_25000": projection(25000),
            "status": "measured" if per_song else "awaiting 10k pilot",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Evaluation report: {output}")
    return payload


def main() -> None:
    args = parse_args()
    run_id = start_run(args.cache, args.operation)
    del run_id
    try:
        if args.operation == "discover":
            result = discover(args)
            accepted = 0
        elif args.operation == "populate":
            result = populate(args)
            accepted = int(result["accepted"])
        elif args.operation == "refresh":
            refresh(args)
            result, accepted = {}, 0
        elif args.operation == "verify":
            result = verify_catalog(
                args.database,
                target_total=args.target_total,
                preserve_snapshot=args.snapshot if args.snapshot.exists() else None,
                require_streaming=True,
            )
            accepted = 0
        elif args.operation == "export-delta":
            result = export_delta(args.database, args.snapshot, args.output or DEFAULT_DELTA)
            accepted = 0
        else:
            result = evaluate(
                args.database,
                args.cache / "pipeline-metrics.sqlite3",
                args.output or DEFAULT_EVALUATION,
            )
            accepted = 0
        details = {
            key: result[key]
            for key in ("status", "candidate_count", "before", "after", "accepted")
            if key in result
        }
        finish_run(status="complete", accepted_songs=accepted, details_json=json.dumps(details))
    except BaseException as error:
        finish_run(status="failed", details_json=json.dumps({"error": str(error)}))
        raise


if __name__ == "__main__":
    main()
