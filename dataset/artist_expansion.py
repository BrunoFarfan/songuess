"""Append-only expansion of artists represented in an immutable catalog baseline.

ListenBrainz provides only a popularity-ordered discovery queue. MusicBrainz
provides canonical identities and a studio-discography completeness pass.
Spotify web playcounts remain the sole acceptance/ranking metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataset.clients import (
    _similarity,
    cache_apple_track,
    fetch_musicbrainz_artist_countries,
    fetch_musicbrainz_metadata,
    read_json,
    search_apple_track,
    validate_previews,
)
from dataset.populate import (
    DEFAULT_CACHE,
    DEFAULT_DATABASE,
    initialize_database,
    write_catalog,
)
from dataset.spotify_streams_browser import (
    DEFAULT_BRAVE_EXECUTABLE,
    BrowserJob,
    _valid_track_url,
    recalculate_popularity_scores,
    run_playwright,
)

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
ALGORITHM_VERSION = "baseline-artist-expansion-v3"
BASELINE_SIZE = 10_000
DEFAULT_CHECKPOINT = DEFAULT_CACHE / f"artist-expansion-{ALGORITHM_VERSION}.sqlite3"
DEFAULT_BASELINE_SNAPSHOT = REPOSITORY_DIR / "dataset" / "reports" / "catalog-snapshot-10000.json"
DEFAULT_PILOT_REPORT = REPOSITORY_DIR / "dataset" / "reports" / "kanye-expansion-pilot.json"
DEFAULT_FINAL_REPORT = REPOSITORY_DIR / "dataset" / "reports" / "artist-expansion-audit.json"
DEFAULT_TRIM_REPORT = REPOSITORY_DIR / "dataset" / "reports" / "artist-cap-trim-20000.json"
ARCHIVED_AUDIT = (
    DEFAULT_CACHE / "obsolete" / "lb-radio-artist-expansion-v1" / "artist-cap-spotify-audit.sqlite3"
)
KANYE_MBID = "164f0d73-1234-4e2c-8743-d77bf2191051"
MINIMUM_STREAMS = 200_000_000
ARTIST_CAP = 30
PILOT_LB_PRIORITY_LIMIT = 100

EXCLUDED_SECONDARY_TYPES = {
    "audiobook",
    "compilation",
    "demo",
    "dj-mix",
    "interview",
    "live",
    "mixtape/street",
    "remix",
    "spokenword",
}
EXCLUDED_VERSION_PATTERN = re.compile(
    r"(?:^|[\s\[(\-])(?:acoustic|demo|instrumental|karaoke|live|remix|"
    r"radio edit|rehearsal|sped up|slowed|version|edit)(?:$|[\s\])\-:])",
    re.IGNORECASE,
)
MAJOR_KANYE_ALBUMS = (
    "The College Dropout",
    "Late Registration",
    "Graduation",
    "My Beautiful Dark Twisted Fantasy",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "snapshot",
            "discover",
            "scrape",
            "scrape-all",
            "pipeline",
            "select",
            "populate",
            "pilot",
            "status",
            "trim",
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--baseline-snapshot", type=Path, default=DEFAULT_BASELINE_SNAPSHOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_PILOT_REPORT)
    parser.add_argument("--final-report", type=Path, default=DEFAULT_FINAL_REPORT)
    parser.add_argument("--trim-report", type=Path, default=DEFAULT_TRIM_REPORT)
    parser.add_argument("--artist-mbid", default=KANYE_MBID)
    parser.add_argument("--limit-artists", type=int)
    parser.add_argument("--lb-priority-limit", type=int, default=PILOT_LB_PRIORITY_LIMIT)
    parser.add_argument("--scrape-limit", type=int)
    parser.add_argument("--target-total", type=int, default=20_000)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--browser-workers", type=int, default=10)
    parser.add_argument(
        "--spotify-sessions",
        type=int,
        default=2,
        help="Concurrent isolated Spotify browser sessions used by pipeline",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Delay while pipeline consumers wait for newly discovered artists",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--browser-timeout-seconds", type=int, default=1800)
    parser.add_argument("--browser-executable", type=Path, default=DEFAULT_BRAVE_EXECUTABLE)
    parser.add_argument("--playwright-cli", type=Path, default=Path("playwright-cli"))
    return parser.parse_args()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_checkpoint(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS baseline_songs (
                recording_mbid TEXT PRIMARY KEY,
                song_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                display_artist TEXT NOT NULL,
                spotify_url TEXT,
                apple_music_url TEXT,
                apple_track_id TEXT,
                isrcs_json TEXT NOT NULL DEFAULT '[]',
                isrc_status TEXT NOT NULL DEFAULT 'pending',
                stream_count INTEGER,
                identity_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artists (
                artist_mbid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                baseline_count INTEGER NOT NULL,
                eligibility_status TEXT NOT NULL DEFAULT 'eligible',
                discovery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(discovery_status IN ('pending','complete','failure')),
                metadata_status TEXT NOT NULL DEFAULT 'pending',
                spotify_status TEXT NOT NULL DEFAULT 'pending',
                failure_reason TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS candidates (
                recording_mbid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                display_artist TEXT,
                release_mbid TEXT,
                release_group_mbid TEXT,
                release_name TEXT,
                first_release_date TEXT,
                duration_ms INTEGER,
                isrcs_json TEXT NOT NULL DEFAULT '[]',
                discovery_status TEXT NOT NULL DEFAULT 'discovered',
                version_status TEXT NOT NULL DEFAULT 'pending',
                version_reason TEXT,
                spotify_url TEXT,
                spotify_track_id TEXT UNIQUE,
                stream_count INTEGER,
                spotify_status TEXT NOT NULL DEFAULT 'pending',
                spotify_fetched_at TEXT,
                apple_music_url TEXT,
                apple_track_id TEXT UNIQUE,
                apple_status TEXT NOT NULL DEFAULT 'pending',
                enrichment_status TEXT NOT NULL DEFAULT 'pending',
                accepted INTEGER CHECK(accepted IN (0,1)),
                decision_reason TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_artists (
                recording_mbid TEXT NOT NULL REFERENCES candidates(recording_mbid),
                artist_mbid TEXT NOT NULL,
                credited_name TEXT,
                credit_order INTEGER NOT NULL,
                PRIMARY KEY(recording_mbid, artist_mbid)
            );
            CREATE TABLE IF NOT EXISTS candidate_sources (
                recording_mbid TEXT NOT NULL REFERENCES candidates(recording_mbid),
                source TEXT NOT NULL,
                source_artist_mbid TEXT NOT NULL,
                source_rank INTEGER,
                source_payload_json TEXT,
                PRIMARY KEY(recording_mbid, source, source_artist_mbid)
            );
            CREATE TABLE IF NOT EXISTS release_groups (
                artist_mbid TEXT NOT NULL,
                release_group_mbid TEXT NOT NULL,
                title TEXT NOT NULL,
                primary_type TEXT,
                secondary_types_json TEXT NOT NULL,
                status TEXT NOT NULL,
                failure_reason TEXT,
                completed_at TEXT,
                PRIMARY KEY(artist_mbid, release_group_mbid)
            );
            CREATE INDEX IF NOT EXISTS candidates_spotify_status
                ON candidates(spotify_status, recording_mbid);
            CREATE INDEX IF NOT EXISTS sources_artist_rank
                ON candidate_sources(source_artist_mbid, source, source_rank);
            """
        )
        candidate_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(candidates)")
        }
        if "apple_payload_json" not in candidate_columns:
            connection.execute("ALTER TABLE candidates ADD COLUMN apple_payload_json TEXT")
        baseline_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(baseline_songs)")
        }
        if "apple_track_id" not in baseline_columns:
            connection.execute("ALTER TABLE baseline_songs ADD COLUMN apple_track_id TEXT")
        if "isrcs_json" not in baseline_columns:
            connection.execute(
                "ALTER TABLE baseline_songs ADD COLUMN isrcs_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "isrc_status" not in baseline_columns:
            connection.execute(
                "ALTER TABLE baseline_songs ADD COLUMN isrc_status TEXT NOT NULL DEFAULT 'pending'"
            )
        artist_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(artists)")}
        if "metadata_status" not in artist_columns:
            connection.execute(
                "ALTER TABLE artists ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "spotify_status" not in artist_columns:
            connection.execute(
                "ALTER TABLE artists ADD COLUMN spotify_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "eligibility_status" not in artist_columns:
            connection.execute(
                "ALTER TABLE artists ADD COLUMN eligibility_status TEXT NOT NULL DEFAULT 'eligible'"
            )
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='algorithm_version'"
        ).fetchone()
        if stored and str(stored[0]) != ALGORITHM_VERSION:
            raise RuntimeError(
                f"Checkpoint belongs to {stored[0]!r}, expected {ALGORITHM_VERSION!r}; "
                "use a new checkpoint path so reusable enrichment can be imported explicitly."
            )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('algorithm_version',?)",
            (ALGORITHM_VERSION,),
        )


def _baseline_rows(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id,musicbrainz_id,title,artist,spotify_url,apple_music_url,apple_track_id,"
            "stream_count "
            "FROM songs WHERE enabled=1 ORDER BY id"
        ).fetchall()
        artist_rows = connection.execute(
            "SELECT sa.song_id,a.musicbrainz_id,a.name,sa.credit_order,sa.credited_name,"
            "sa.join_phrase FROM song_artists sa JOIN artists a ON a.id=sa.artist_id "
            "JOIN songs s ON s.id=sa.song_id WHERE s.enabled=1 "
            "ORDER BY sa.song_id,sa.credit_order"
        ).fetchall()
    credits: dict[int, list[dict[str, Any]]] = {}
    for row in artist_rows:
        credits.setdefault(int(row[0]), []).append(
            {
                "artist_mbid": str(row[1]),
                "name": str(row[2]),
                "credit_order": int(row[3]),
                "credited_name": str(row[4]),
                "join_phrase": str(row[5]),
            }
        )
    return [
        {
            "song_id": int(row["id"]),
            "recording_mbid": str(row["musicbrainz_id"] or ""),
            "title": str(row["title"]),
            "artist": str(row["artist"]),
            "spotify_url": row["spotify_url"],
            "apple_music_url": row["apple_music_url"],
            "apple_track_id": row["apple_track_id"],
            "stream_count": row["stream_count"],
            "credits": credits.get(int(row["id"]), []),
        }
        for row in rows
    ]


def baseline_fingerprint(rows: list[dict[str, Any]]) -> str:
    identities = [str(row["recording_mbid"]) for row in rows]
    return hashlib.sha256(("\n".join(identities) + "\n").encode()).hexdigest()


def snapshot_baseline(database: Path, checkpoint: Path) -> dict[str, Any]:
    initialize_database(database)
    initialize_checkpoint(checkpoint)
    rows = _baseline_rows(database)
    if len(rows) != BASELINE_SIZE:
        raise RuntimeError(
            f"Expected immutable {BASELINE_SIZE:,}-song baseline; found {len(rows):,}"
        )
    if any(not row["recording_mbid"] for row in rows):
        raise RuntimeError("Every baseline row must have a MusicBrainz recording identity")
    if len({row["recording_mbid"] for row in rows}) != BASELINE_SIZE:
        raise RuntimeError("Baseline recording MBIDs are not unique")
    fingerprint = baseline_fingerprint(rows)

    artist_songs: dict[str, set[int]] = {}
    artist_names: dict[str, str] = {}
    with _connect(checkpoint) as connection:
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key='baseline_fingerprint'"
        ).fetchone()
        if existing and str(existing[0]) != fingerprint:
            raise RuntimeError("The live 10,000-song baseline differs from this checkpoint")
        for row in rows:
            connection.execute(
                "INSERT OR IGNORE INTO baseline_songs "
                "(recording_mbid,song_id,title,display_artist,spotify_url,apple_music_url,"
                "apple_track_id,isrcs_json,isrc_status,stream_count,identity_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["recording_mbid"],
                    row["song_id"],
                    row["title"],
                    row["artist"],
                    row["spotify_url"],
                    row["apple_music_url"],
                    row["apple_track_id"],
                    "[]",
                    "pending",
                    row["stream_count"],
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                ),
            )
            for credit in row["credits"]:
                artist_mbid = credit["artist_mbid"]
                artist_songs.setdefault(artist_mbid, set()).add(int(row["song_id"]))
                artist_names[artist_mbid] = credit["name"]
        for artist_mbid, song_ids in artist_songs.items():
            connection.execute(
                "INSERT INTO artists(artist_mbid,name,baseline_count) VALUES(?,?,?) "
                "ON CONFLICT(artist_mbid) DO UPDATE SET name=excluded.name,"
                "baseline_count=excluded.baseline_count",
                (artist_mbid, artist_names[artist_mbid], len(song_ids)),
            )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('baseline_fingerprint',?)",
            (fingerprint,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('baseline_size',?)",
            (str(len(rows)),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('baseline_snapshotted_at',?)",
            (utc_now(),),
        )
    return {"songs": len(rows), "artists": len(artist_songs), "fingerprint": fingerprint}


def ensure_baseline(database: Path, checkpoint: Path) -> dict[str, Any]:
    """Create the 10k snapshot once, then verify its identities still exist."""
    initialize_checkpoint(checkpoint)
    with _connect(checkpoint) as connection:
        count = int(connection.execute("SELECT COUNT(*) FROM baseline_songs").fetchone()[0])
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='baseline_fingerprint'"
        ).fetchone()
        artist_count = int(connection.execute("SELECT COUNT(*) FROM artists").fetchone()[0])
    if count == 0:
        return snapshot_baseline(database, checkpoint)
    if count != BASELINE_SIZE or stored is None:
        raise RuntimeError("Checkpoint contains an incomplete baseline snapshot")
    with sqlite3.connect(database) as connection:
        live_rows = connection.execute(
            "SELECT musicbrainz_id,apple_track_id FROM songs WHERE musicbrainz_id IS NOT NULL"
        ).fetchall()
        live_mbids = {str(row[0]) for row in live_rows}
        apple_ids = {str(row[0]): row[1] for row in live_rows}
    with _connect(checkpoint) as connection:
        baseline_mbids = {
            str(row[0]) for row in connection.execute("SELECT recording_mbid FROM baseline_songs")
        }
        connection.executemany(
            "UPDATE baseline_songs SET apple_track_id=? "
            "WHERE recording_mbid=? AND apple_track_id IS NULL",
            [(apple_ids.get(mbid), mbid) for mbid in baseline_mbids],
        )
    missing = baseline_mbids - live_mbids
    if missing:
        raise RuntimeError(f"Stored baseline lost {len(missing):,} recording identities")
    return {"songs": count, "artists": artist_count, "fingerprint": str(stored[0])}


def _cache_json(cache_path: Path, url: str, request_json: Callable[[str], Any]) -> Any:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    payload = request_json(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)
    return payload


def fetch_artist_top_recordings(
    cache: Path,
    artist_mbid: str,
    *,
    request_json: Callable[[str], Any] = read_json,
) -> list[dict[str, Any]]:
    if not os.environ.get("LISTENBRAINZ_TOKEN", "").strip():
        raise RuntimeError("LISTENBRAINZ_TOKEN is required in the process environment")
    path = cache / ALGORITHM_VERSION / "listenbrainz" / f"{artist_mbid}.json"
    url = (
        "https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/"
        + urllib.parse.quote(artist_mbid, safe="")
    )
    payload = _cache_json(path, url, request_json)
    if not isinstance(payload, list):
        raise RuntimeError(f"ListenBrainz returned a non-list for artist {artist_mbid}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _musicbrainz_pages(
    url: str,
    collection_key: str,
    count_key: str,
    *,
    request_json: Callable[[str], Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    offset = 0
    while True:
        separator = "&" if "?" in url else "?"
        payload = request_json(f"{url}{separator}limit=100&offset={offset}&fmt=json")
        page = payload.get(collection_key, []) if isinstance(payload, dict) else []
        results.extend(dict(item) for item in page if isinstance(item, dict))
        total = int(payload.get(count_key, len(results))) if isinstance(payload, dict) else 0
        offset += len(page)
        if not page or offset >= total:
            break
    return results


def relevant_release_group(group: dict[str, Any]) -> tuple[bool, str | None]:
    primary = str(group.get("primary-type") or group.get("type") or "").casefold()
    secondary = {str(value).casefold() for value in group.get("secondary-types") or []}
    if primary not in {"album", "single"}:
        return False, f"primary_type:{primary or 'missing'}"
    first_release_date = str(group.get("first-release-date") or "")
    if len(first_release_date) < 4 or not first_release_date[:4].isdigit():
        return False, "missing_first_release_date"
    excluded = sorted(secondary & EXCLUDED_SECONDARY_TYPES)
    if excluded:
        return False, "secondary_type:" + ",".join(excluded)
    title = str(group.get("title") or "")
    if EXCLUDED_VERSION_PATTERN.search(title):
        return False, "alternate_version_title"
    return True, None


def recording_version_decision(
    recording: dict[str, Any], *, from_canonical_studio_release: bool
) -> tuple[str, str | None]:
    title = str(recording.get("title") or "")
    if EXCLUDED_VERSION_PATTERN.search(title):
        return "excluded", "alternate_version_title"
    if from_canonical_studio_release:
        return "eligible", None
    releases = [item for item in recording.get("releases") or [] if isinstance(item, dict)]
    valid_official_release = False
    for release in releases:
        if str(release.get("status") or "").casefold() != "official":
            continue
        group = (
            release.get("release-group") if isinstance(release.get("release-group"), dict) else {}
        )
        secondary = {str(value).casefold() for value in group.get("secondary-types") or []}
        if secondary & EXCLUDED_SECONDARY_TYPES:
            continue
        if EXCLUDED_VERSION_PATTERN.search(str(release.get("title") or "")):
            continue
        valid_official_release = True
        break
    if valid_official_release:
        return "eligible", None
    if releases:
        return "excluded", "no_official_studio_release_evidence"
    if not recording.get("isrcs"):
        return "excluded", "insufficient_structured_version_evidence"
    return "eligible", None


def fetch_release_groups(
    cache: Path,
    artist_mbid: str,
    *,
    request_json: Callable[[str], Any] = read_json,
) -> list[dict[str, Any]]:
    path = (
        cache
        / ALGORITHM_VERSION
        / "musicbrainz-release-groups"
        / f"{artist_mbid}.artist-credits.json"
    )
    url = (
        "https://musicbrainz.org/ws/2/release-group?artist="
        + urllib.parse.quote(artist_mbid, safe="")
        + "&inc=artist-credits"
    )

    def fetch_all(_: str) -> list[dict[str, Any]]:
        return _musicbrainz_pages(
            url,
            "release-groups",
            "release-group-count",
            request_json=request_json,
        )

    payload = _cache_json(path, url, fetch_all)
    return [dict(item) for item in payload if isinstance(item, dict)]


def fetch_artist_releases(
    cache: Path,
    artist_mbid: str,
    *,
    request_json: Callable[[str], Any] = read_json,
) -> list[dict[str, Any]]:
    """Fetch every release for an artist in paginated, recording-rich batches."""
    path = cache / ALGORITHM_VERSION / "musicbrainz-artist-releases" / f"{artist_mbid}.json"
    query = urllib.parse.urlencode(
        {
            "artist": artist_mbid,
            "inc": "recordings+artist-credits+isrcs+release-groups",
        }
    )
    url = f"https://musicbrainz.org/ws/2/release?{query}"

    def fetch_all(_: str) -> list[dict[str, Any]]:
        return _musicbrainz_pages(
            url,
            "releases",
            "release-count",
            request_json=request_json,
        )

    payload = _cache_json(path, url, fetch_all)
    return [dict(item) for item in payload if isinstance(item, dict)]


def _release_sort_key(release: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(release.get("status") or "").casefold() != "official",
        bool(EXCLUDED_VERSION_PATTERN.search(str(release.get("title") or ""))),
        not bool(release.get("date")),
        str(release.get("date") or "9999-99-99"),
        str(release.get("country") or "ZZ"),
        str(release.get("id") or ""),
    )


def canonical_artist_releases(releases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Choose one canonical official edition per release group from a bulk browse."""
    by_group: dict[str, list[dict[str, Any]]] = {}
    for release in releases:
        group = release.get("release-group")
        if not isinstance(group, dict) or not group.get("id"):
            continue
        if str(release.get("status") or "").casefold() != "official":
            continue
        by_group.setdefault(str(group["id"]), []).append(release)
    return {group_mbid: min(items, key=_release_sort_key) for group_mbid, items in by_group.items()}


def collaborative_release_group(release_group: dict[str, Any]) -> bool:
    """Return whether canonical release selection must consider multiple release artists."""
    artist_mbids = {
        str(credit.get("artist", {}).get("id"))
        for credit in release_group.get("artist-credit", [])
        if isinstance(credit, dict)
        and isinstance(credit.get("artist"), dict)
        and credit["artist"].get("id")
    }
    return len(artist_mbids) > 1


def fetch_canonical_release(
    cache: Path,
    release_group: dict[str, Any],
    *,
    request_json: Callable[[str], Any] = read_json,
) -> dict[str, Any] | None:
    group_mbid = str(release_group.get("id") or "")
    path = cache / ALGORITHM_VERSION / "musicbrainz-releases" / f"{group_mbid}.json"
    # Reuse detail responses produced by the initial two-request prototype.
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, dict) else None

    # A release browse can include recordings for every edition in one request.
    # Select the canonical official edition locally instead of first looking up
    # the release group and then making a second release-detail request.
    query = urllib.parse.urlencode(
        {
            "release-group": group_mbid,
            "inc": "recordings+artist-credits+isrcs+release-groups",
            "limit": 100,
            "fmt": "json",
        }
    )
    browse_url = f"https://musicbrainz.org/ws/2/release?{query}"
    payload = _cache_json(path.with_suffix(".browse.json"), browse_url, request_json)
    releases = [
        dict(item)
        for item in payload.get("releases", [])
        if isinstance(item, dict) and str(item.get("status") or "").casefold() == "official"
    ]
    if not releases:
        return None
    return min(releases, key=_release_sort_key)


def release_recordings(release: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for medium in release.get("media", []):
        if not isinstance(medium, dict):
            continue
        for track in medium.get("tracks", []):
            if not isinstance(track, dict) or not isinstance(track.get("recording"), dict):
                continue
            recording = dict(track["recording"])
            title = str(recording.get("title") or track.get("title") or "").strip()
            if not recording.get("id") or not title:
                continue
            recording["track_title"] = str(track.get("title") or title)
            recording["position"] = track.get("position")
            results.append(recording)
    return results


def _artist_credits(recording: dict[str, Any]) -> list[dict[str, Any]]:
    credits: list[dict[str, Any]] = []
    for order, credit in enumerate(recording.get("artist-credit", [])):
        if not isinstance(credit, dict) or not isinstance(credit.get("artist"), dict):
            continue
        artist = credit["artist"]
        mbid = str(artist.get("id") or "")
        if mbid:
            credits.append(
                {
                    "artist_mbid": mbid,
                    "credited_name": str(credit.get("name") or artist.get("name") or ""),
                    "name": str(artist.get("name") or ""),
                    "order": order,
                    "joinphrase": str(credit.get("joinphrase") or ""),
                }
            )
    return credits


def _upsert_candidate(
    connection: sqlite3.Connection,
    recording: dict[str, Any],
    *,
    source: str,
    source_artist_mbid: str,
    source_rank: int | None,
    source_payload: dict[str, Any] | None,
    release: dict[str, Any] | None = None,
    release_group: dict[str, Any] | None = None,
) -> None:
    mbid = str(recording.get("recording_mbid") or recording.get("id") or "")
    title = str(recording.get("recording_name") or recording.get("title") or "").strip()
    if not mbid or not title:
        return
    credits = _artist_credits(recording)
    artist_mbids = [str(value) for value in recording.get("artist_mbids") or []]
    if not credits:
        credits = [
            {"artist_mbid": value, "credited_name": "", "name": "", "order": order}
            for order, value in enumerate(artist_mbids)
            if value
        ]
    display_artist = "".join(
        credit.get("credited_name", "") + credit.get("joinphrase", "") for credit in credits
    ) or str(recording.get("artist_name") or "")
    group = release_group or {}
    active_release = release or {}
    isrcs = sorted({str(value).strip().upper() for value in recording.get("isrcs") or [] if value})
    version_status = "excluded" if EXCLUDED_VERSION_PATTERN.search(title) else "eligible"
    version_reason = "alternate_version_title" if version_status == "excluded" else None
    connection.execute(
        "INSERT INTO candidates(recording_mbid,title,display_artist,release_mbid,"
        "release_group_mbid,release_name,first_release_date,duration_ms,isrcs_json,"
        "version_status,version_reason,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(recording_mbid) DO UPDATE SET "
        "title=CASE WHEN candidates.title='' THEN excluded.title ELSE candidates.title END,"
        "display_artist=COALESCE(NULLIF(candidates.display_artist,''),excluded.display_artist),"
        "release_mbid=COALESCE(candidates.release_mbid,excluded.release_mbid),"
        "release_group_mbid=COALESCE(candidates.release_group_mbid,excluded.release_group_mbid),"
        "release_name=COALESCE(candidates.release_name,excluded.release_name),"
        "first_release_date=COALESCE(candidates.first_release_date,excluded.first_release_date),"
        "duration_ms=COALESCE(candidates.duration_ms,excluded.duration_ms),"
        "isrcs_json=CASE WHEN candidates.isrcs_json='[]' THEN excluded.isrcs_json "
        "ELSE candidates.isrcs_json END,updated_at=excluded.updated_at",
        (
            mbid,
            title,
            display_artist,
            str(active_release.get("id") or recording.get("release_mbid") or "") or None,
            str(group.get("id") or "") or None,
            str(active_release.get("title") or recording.get("release_name") or "") or None,
            str(group.get("first-release-date") or "") or None,
            recording.get("length"),
            json.dumps(isrcs),
            version_status,
            version_reason,
            utc_now(),
        ),
    )
    for credit in credits:
        connection.execute(
            "INSERT OR IGNORE INTO candidate_artists"
            "(recording_mbid,artist_mbid,credited_name,credit_order) VALUES(?,?,?,?)",
            (mbid, credit["artist_mbid"], credit.get("credited_name"), credit["order"]),
        )
    connection.execute(
        "INSERT INTO candidate_sources(recording_mbid,source,source_artist_mbid,"
        "source_rank,source_payload_json) VALUES(?,?,?,?,?) "
        "ON CONFLICT(recording_mbid,source,source_artist_mbid) DO UPDATE SET "
        "source_rank=excluded.source_rank,source_payload_json=excluded.source_payload_json",
        (
            mbid,
            source,
            source_artist_mbid,
            source_rank,
            json.dumps(source_payload, ensure_ascii=False, sort_keys=True)
            if source_payload is not None
            else None,
        ),
    )


def import_reusable_enrichment(checkpoint: Path, archived_audit: Path = ARCHIVED_AUDIT) -> int:
    if not archived_audit.exists():
        return 0
    imported = 0
    with sqlite3.connect(archived_audit) as source, _connect(checkpoint) as target:
        rows = source.execute(
            "SELECT recording_mbid,spotify_url,stream_count,attempted_at FROM candidates "
            "WHERE status='complete' AND spotify_url IS NOT NULL AND stream_count IS NOT NULL"
        ).fetchall()
        for mbid, spotify_url, stream_count, attempted_at in rows:
            track_id = str(spotify_url).split("/track/", 1)[-1].split("?", 1)[0]
            cursor = target.execute(
                "UPDATE candidates SET spotify_url=?,spotify_track_id=?,stream_count=?,"
                "spotify_status='complete',spotify_fetched_at=?,updated_at=? "
                "WHERE recording_mbid=? AND (spotify_status!='complete' OR stream_count IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM candidates AS provider_owner "
                "WHERE provider_owner.spotify_track_id=? "
                "AND provider_owner.recording_mbid!=?)",
                (
                    spotify_url,
                    track_id,
                    stream_count,
                    attempted_at,
                    utc_now(),
                    mbid,
                    track_id,
                    mbid,
                ),
            )
            imported += cursor.rowcount
    return imported


def discover_artist(
    checkpoint: Path,
    cache: Path,
    artist_mbid: str,
    *,
    request_json: Callable[[str], Any] = read_json,
) -> dict[str, int]:
    initialize_checkpoint(checkpoint)
    top = fetch_artist_top_recordings(cache, artist_mbid, request_json=request_json)
    with ThreadPoolExecutor(max_workers=2) as executor:
        groups_future = executor.submit(
            fetch_release_groups,
            cache,
            artist_mbid,
            request_json=request_json,
        )
        releases_future = executor.submit(
            fetch_artist_releases,
            cache,
            artist_mbid,
            request_json=request_json,
        )
        groups = groups_future.result()
        bulk_releases = canonical_artist_releases(releases_future.result())
    included_groups = excluded_groups = studio_recordings = 0
    fallback_release_requests = 0
    with _connect(checkpoint) as connection:
        baseline = {
            str(row[0]) for row in connection.execute("SELECT recording_mbid FROM baseline_songs")
        }
        for rank, recording in enumerate(top, start=1):
            _upsert_candidate(
                connection,
                recording,
                source="listenbrainz_top_recordings",
                source_artist_mbid=artist_mbid,
                source_rank=rank,
                source_payload=recording,
            )
        connection.commit()
        for group_index, group in enumerate(groups, start=1):
            group_mbid = str(group.get("id") or "")
            relevant, reason = relevant_release_group(group)
            if not relevant:
                excluded_groups += 1
                connection.execute(
                    "INSERT OR REPLACE INTO release_groups VALUES(?,?,?,?,?,?,?,?)",
                    (
                        artist_mbid,
                        group_mbid,
                        str(group.get("title") or ""),
                        group.get("primary-type"),
                        json.dumps(group.get("secondary-types") or []),
                        "excluded",
                        reason,
                        utc_now(),
                    ),
                )
                connection.commit()
                continue
            included_groups += 1
            try:
                release = bulk_releases.get(group_mbid)
                if collaborative_release_group(group) or (
                    release is not None and not release_recordings(release)
                ):
                    fallback_release_requests += 1
                    release = fetch_canonical_release(cache, group, request_json=request_json)
            except Exception as error:  # noqa: BLE001 - checkpointed and retryable.
                connection.execute(
                    "INSERT OR REPLACE INTO release_groups VALUES(?,?,?,?,?,?,?,?)",
                    (
                        artist_mbid,
                        group_mbid,
                        str(group.get("title") or ""),
                        group.get("primary-type"),
                        json.dumps(group.get("secondary-types") or []),
                        "failure",
                        type(error).__name__,
                        utc_now(),
                    ),
                )
                connection.commit()
                continue
            if release is None:
                connection.execute(
                    "INSERT OR REPLACE INTO release_groups VALUES(?,?,?,?,?,?,?,?)",
                    (
                        artist_mbid,
                        group_mbid,
                        str(group.get("title") or ""),
                        group.get("primary-type"),
                        json.dumps(group.get("secondary-types") or []),
                        "failure",
                        "no_official_release",
                        utc_now(),
                    ),
                )
                connection.commit()
                continue
            for position, recording in enumerate(release_recordings(release), start=1):
                studio_recordings += 1
                _upsert_candidate(
                    connection,
                    recording,
                    source="musicbrainz_studio_discography",
                    source_artist_mbid=artist_mbid,
                    source_rank=position,
                    source_payload=None,
                    release=release,
                    release_group=group,
                )
            connection.execute(
                "INSERT OR REPLACE INTO release_groups VALUES(?,?,?,?,?,?,?,?)",
                (
                    artist_mbid,
                    group_mbid,
                    str(group.get("title") or ""),
                    group.get("primary-type"),
                    json.dumps(group.get("secondary-types") or []),
                    "complete",
                    None,
                    utc_now(),
                ),
            )
            connection.commit()
            if group_index % 25 == 0 or group_index == len(groups):
                print(
                    f"  MusicBrainz release groups {group_index:,}/{len(groups):,} "
                    f"for {artist_mbid}",
                    flush=True,
                )
        connection.execute(
            "UPDATE artists SET discovery_status='complete',failure_reason=NULL,completed_at=? "
            "WHERE artist_mbid=?",
            (utc_now(), artist_mbid),
        )
        unique_candidates = int(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
        new_candidates = int(
            connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE recording_mbid NOT IN "
                "(SELECT recording_mbid FROM baseline_songs)"
            ).fetchone()[0]
        )
    imported = import_reusable_enrichment(checkpoint)
    return {
        "listenbrainz_recordings": len(top),
        "release_groups": len(groups),
        "included_release_groups": included_groups,
        "excluded_release_groups": excluded_groups,
        "studio_track_occurrences": studio_recordings,
        "bulk_artist_releases": len(bulk_releases),
        "fallback_release_requests": fallback_release_requests,
        "unique_candidates": unique_candidates,
        "new_candidates": new_candidates,
        "baseline_candidates": unique_candidates - new_candidates,
        "reused_spotify_results": imported,
        "baseline_recordings_seen": len(baseline),
    }


def checkpoint_saturated_artists(checkpoint: Path) -> int:
    """Avoid provider work for artists that cannot accept another song."""
    initialize_checkpoint(checkpoint)
    with _connect(checkpoint) as connection:
        cursor = connection.execute(
            "UPDATE artists SET eligibility_status='cap_already_reached',"
            "discovery_status='complete',metadata_status='skipped_cap',"
            "spotify_status='skipped_cap',failure_reason=NULL,"
            "completed_at=COALESCE(completed_at,?) "
            "WHERE baseline_count>=? AND eligibility_status!='cap_already_reached'",
            (utc_now(), ARTIST_CAP),
        )
        return cursor.rowcount


def prepare_artist_candidate_metadata(
    checkpoint: Path,
    cache: Path,
    artist_mbid: str,
    *,
    lb_priority_limit: int,
) -> dict[str, int]:
    """Resolve prioritized discovery rows to exact MusicBrainz recording identities."""
    with _connect(checkpoint) as connection:
        rows = connection.execute(
            "SELECT c.recording_mbid,MIN(CASE WHEN cs.source='listenbrainz_top_recordings' "
            "THEN cs.source_rank END) lb_rank,MAX(cs.source='musicbrainz_studio_discography') "
            "studio FROM candidates c JOIN candidate_sources cs USING(recording_mbid) "
            "LEFT JOIN baseline_songs b USING(recording_mbid) "
            "WHERE cs.source_artist_mbid=? AND b.recording_mbid IS NULL "
            "GROUP BY c.recording_mbid HAVING studio=1 OR lb_rank<=? "
            "ORDER BY CASE WHEN lb_rank IS NULL THEN 1 ELSE 0 END,lb_rank,c.recording_mbid",
            (artist_mbid, lb_priority_limit),
        ).fetchall()
    identities = [str(row[0]) for row in rows]
    studio_mbids = {str(row[0]) for row in rows if int(row[2] or 0)}
    # Canonical studio candidates already came from structured MusicBrainz release
    # payloads, including their exact recording and artist identities. Refetching
    # each recording individually dominated expansion runtime without improving the
    # identity evidence. Only LB-only discoveries need a recording lookup here.
    lookup_mbids = [mbid for mbid in identities if mbid not in studio_mbids]
    metadata = fetch_musicbrainz_metadata(
        cache, [{"recording_mbid": mbid} for mbid in lookup_mbids]
    )
    resolved = len(studio_mbids)
    rejected = 0
    with _connect(checkpoint) as connection:
        if studio_mbids:
            placeholders = ",".join("?" for _ in studio_mbids)
            connection.execute(
                "UPDATE candidates SET enrichment_status=CASE "
                "WHEN enrichment_status IN ('verified','populated') THEN enrichment_status "
                "ELSE 'musicbrainz_release_complete' END,updated_at=? "
                f"WHERE recording_mbid IN ({placeholders})",
                (utc_now(), *sorted(studio_mbids)),
            )
        for mbid in lookup_mbids:
            recording = metadata.get(mbid) or {}
            credits = _artist_credits(recording)
            if not recording or artist_mbid not in {
                str(credit["artist_mbid"]) for credit in credits
            }:
                connection.execute(
                    "UPDATE candidates SET version_status='excluded',version_reason=?,"
                    "enrichment_status='identity_rejected',updated_at=? "
                    "WHERE recording_mbid=?",
                    ("missing_or_uncredited_musicbrainz_identity", utc_now(), mbid),
                )
                rejected += 1
                continue
            title = str(recording.get("title") or "").strip()
            display_artist = "".join(
                credit["credited_name"] + credit.get("joinphrase", "") for credit in credits
            )
            version_status, version_reason = recording_version_decision(
                recording,
                from_canonical_studio_release=mbid in studio_mbids,
            )
            isrcs = sorted(
                {
                    str(value).strip().upper()
                    for value in recording.get("isrcs") or []
                    if str(value).strip()
                }
            )
            connection.execute(
                "UPDATE candidates SET title=?,display_artist=?,first_release_date="
                "COALESCE(first_release_date,?),duration_ms=COALESCE(?,duration_ms),"
                "isrcs_json=?,"
                "version_status=CASE WHEN version_reason LIKE 'duplicate_%' "
                "THEN version_status ELSE ? END,"
                "version_reason=CASE WHEN version_reason LIKE 'duplicate_%' "
                "THEN version_reason ELSE ? END,"
                "enrichment_status=CASE WHEN enrichment_status IN ('verified','populated') "
                "THEN enrichment_status ELSE 'musicbrainz_complete' END,updated_at=? "
                "WHERE recording_mbid=?",
                (
                    title,
                    display_artist,
                    str(recording.get("first-release-date") or "") or None,
                    recording.get("length"),
                    json.dumps(isrcs),
                    version_status,
                    version_reason,
                    utc_now(),
                    mbid,
                ),
            )
            connection.execute("DELETE FROM candidate_artists WHERE recording_mbid=?", (mbid,))
            for credit in credits:
                connection.execute(
                    "INSERT INTO candidate_artists"
                    "(recording_mbid,artist_mbid,credited_name,credit_order) VALUES(?,?,?,?)",
                    (mbid, credit["artist_mbid"], credit["credited_name"], credit["order"]),
                )
            resolved += 1
        connection.execute(
            "UPDATE artists SET metadata_status='complete' WHERE artist_mbid=?",
            (artist_mbid,),
        )
    return {
        "queued": len(identities),
        "trusted_studio": len(studio_mbids),
        "looked_up": len(lookup_mbids),
        "resolved": resolved,
        "rejected": rejected,
    }


def _spotify_jobs_for_artist(
    checkpoint: Path,
    artist_mbid: str,
    *,
    lb_priority_limit: int,
    limit: int | None,
    retry_failures: bool,
) -> list[BrowserJob]:
    status_clause = (
        "(c.spotify_status='pending' OR c.spotify_status IN ("
        "'failure:navigation_or_hydration_error','failure:browser_batch_failed',"
        "'failure:spotify_http_429','failure:spotify_http_500',"
        "'failure:spotify_http_502','failure:spotify_http_503'))"
        if retry_failures
        else "c.spotify_status='pending'"
    )
    with _connect(checkpoint) as connection:
        rows = connection.execute(
            "SELECT c.rowid,c.title,c.display_artist,c.release_name,c.duration_ms,"
            "c.isrcs_json,c.spotify_url,"
            "MIN(CASE WHEN cs.source='listenbrainz_top_recordings' THEN cs.source_rank END) "
            "lb_rank,MAX(cs.source='musicbrainz_studio_discography') studio "
            "FROM candidates c JOIN candidate_sources cs USING(recording_mbid) "
            "JOIN candidate_artists ca ON ca.recording_mbid=c.recording_mbid "
            "LEFT JOIN baseline_songs b USING(recording_mbid) "
            "WHERE cs.source_artist_mbid=? AND ca.artist_mbid=? AND b.recording_mbid IS NULL "
            "AND c.version_status='eligible' AND " + status_clause + " "
            "GROUP BY c.recording_mbid HAVING studio=1 OR lb_rank<=? "
            "ORDER BY CASE WHEN lb_rank IS NULL THEN 1 ELSE 0 END,lb_rank,c.recording_mbid",
            (artist_mbid, artist_mbid, lb_priority_limit),
        ).fetchall()
        credits = {
            int(rowid): tuple(
                str(value[0])
                for value in connection.execute(
                    "SELECT credited_name FROM candidate_artists "
                    "WHERE recording_mbid=(SELECT recording_mbid FROM candidates WHERE rowid=?) "
                    "ORDER BY credit_order",
                    (rowid,),
                )
            )
            for rowid, *_rest in rows
        }
    if limit is not None:
        rows = rows[:limit]
    jobs: list[BrowserJob] = []
    for row in rows:
        rowid = int(row[0])
        spotify_url = str(row[6]) if row[6] else None
        isrcs = tuple(json.loads(row[5]))
        credited_artists = credits.get(rowid, ())
        if _valid_track_url(spotify_url):
            method = "existing_url"
            spotify_urls = (spotify_url,)
            search_urls: tuple[str, ...] = ()
        elif isrcs:
            method = "isrc"
            spotify_urls = ()
            search_urls = tuple(
                "https://open.spotify.com/search/"
                + urllib.parse.quote(f"isrc:{isrc}", safe="")
                + "/tracks"
                for isrc in isrcs[:3]
            )
        else:
            method = "exact_metadata"
            spotify_urls = ()
            primary_artist = credited_artists[0] if credited_artists else str(row[2] or "")
            query = f'track:"{row[1]}" artist:"{primary_artist}"'
            search_urls = (
                "https://open.spotify.com/search/" + urllib.parse.quote(query, safe="") + "/tracks",
            )
        jobs.append(
            BrowserJob(
                song_id=rowid,
                title=str(row[1]),
                artist=str(row[2] or ""),
                album=str(row[3]) if row[3] else None,
                credited_artists=credited_artists,
                duration_ms=int(row[4]) if row[4] is not None else None,
                match_method=method,
                spotify_urls=spotify_urls,
                search_urls=search_urls,
            )
        )
    return jobs


def _persist_spotify_batch(
    checkpoint: Path,
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    fetched_at = utc_now()
    with _connect(checkpoint) as connection:
        baseline_track_ids = {
            str(row[0]).split("/track/", 1)[-1].split("?", 1)[0]
            for row in connection.execute(
                "SELECT spotify_url FROM baseline_songs WHERE spotify_url LIKE "
                "'https://open.spotify.com/track/%'"
            )
        }
        for item in results:
            spotify_url = str(item["spotify_url"]).split("?", 1)[0]
            track_id = spotify_url.split("/track/", 1)[-1]
            target = connection.execute(
                "SELECT recording_mbid FROM candidates WHERE rowid=?", (int(item["song_id"]),)
            ).fetchone()
            if target is None:
                continue
            duplicate = connection.execute(
                "SELECT recording_mbid FROM candidates WHERE spotify_track_id=? AND rowid!=?",
                (track_id, int(item["song_id"])),
            ).fetchone()
            if track_id in baseline_track_ids or duplicate is not None:
                reason = (
                    "duplicate_baseline_spotify_track"
                    if track_id in baseline_track_ids
                    else "duplicate_candidate_spotify_track"
                )
                connection.execute(
                    "UPDATE candidates SET spotify_url=?,stream_count=?,spotify_status=?,"
                    "spotify_fetched_at=?,version_status='excluded',version_reason=?,"
                    "updated_at=? WHERE rowid=?",
                    (
                        spotify_url,
                        int(item["stream_count"]),
                        reason,
                        fetched_at,
                        reason,
                        fetched_at,
                        int(item["song_id"]),
                    ),
                )
                continue
            connection.execute(
                "UPDATE candidates SET spotify_url=?,spotify_track_id=?,stream_count=?,"
                "spotify_status='complete',spotify_fetched_at=?,updated_at=? WHERE rowid=?",
                (
                    spotify_url,
                    track_id,
                    int(item["stream_count"]),
                    fetched_at,
                    fetched_at,
                    int(item["song_id"]),
                ),
            )
        for item in failures:
            connection.execute(
                "UPDATE candidates SET spotify_status=?,spotify_fetched_at=?,updated_at=? "
                "WHERE rowid=? AND spotify_status!='complete'",
                (
                    "failure:" + str(item.get("status") or "unknown"),
                    fetched_at,
                    fetched_at,
                    int(item["song_id"]),
                ),
            )


RESTORABLE_PROVIDER_DUPLICATE_REASONS = {
    "duplicate_candidate_spotify_track",
    "duplicate_candidate_apple_track",
}


def _exact_title_key(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def reconcile_baseline_exact_identities(checkpoint: Path) -> int:
    """Reject exact-title duplicates anchored to the same primary canonical artist."""
    with _connect(checkpoint) as connection:
        # Re-evaluate earlier decisions so this remains safe when the reconciliation
        # rule changes between resumable runs.
        connection.execute(
            "UPDATE candidates SET version_status='eligible',version_reason=NULL,"
            "accepted=NULL,decision_reason=NULL,updated_at=? "
            "WHERE version_reason='duplicate_baseline_exact_identity'",
            (utc_now(),),
        )
        baseline_keys: set[tuple[str, str]] = set()
        baseline_mbids: set[str] = set()
        for row in connection.execute(
            "SELECT recording_mbid,title,identity_json FROM baseline_songs"
        ):
            identity = json.loads(row["identity_json"])
            credits = sorted(
                (credit for credit in identity.get("credits", []) if credit.get("artist_mbid")),
                key=lambda credit: int(credit.get("credit_order", 0)),
            )
            baseline_mbids.add(str(row["recording_mbid"]))
            if credits:
                baseline_keys.add((_exact_title_key(row["title"]), str(credits[0]["artist_mbid"])))
        changed = 0
        for row in connection.execute(
            "SELECT recording_mbid,title FROM candidates "
            "WHERE version_status='eligible' OR version_reason IN (?,?)",
            tuple(sorted(RESTORABLE_PROVIDER_DUPLICATE_REASONS)),
        ).fetchall():
            mbid = str(row["recording_mbid"])
            if mbid in baseline_mbids:
                continue
            primary_artist = connection.execute(
                "SELECT artist_mbid FROM candidate_artists WHERE recording_mbid=? "
                "ORDER BY credit_order LIMIT 1",
                (mbid,),
            ).fetchone()
            if (
                primary_artist is None
                or (_exact_title_key(row["title"]), str(primary_artist[0])) not in baseline_keys
            ):
                continue
            connection.execute(
                "UPDATE candidates SET accepted=0,"
                "decision_reason='duplicate_baseline_exact_identity',"
                "version_status='excluded',version_reason='duplicate_baseline_exact_identity',"
                "updated_at=? WHERE recording_mbid=?",
                (utc_now(), mbid),
            )
            changed += 1
        return changed


def _provider_owner_priority(row: sqlite3.Row) -> tuple[Any, ...]:
    version_status = str(row["version_status"] or "")
    version_reason = str(row["version_reason"] or "")
    version_score = (
        2
        if version_status == "eligible"
        else 1
        if version_reason in RESTORABLE_PROVIDER_DUPLICATE_REASONS
        else 0
    )
    return (
        version_score,
        int(row["studio"] or 0),
        bool(json.loads(row["isrcs_json"] or "[]")),
        -int(row["lb_rank"] if row["lb_rank"] is not None else 10**9),
        str(row["recording_mbid"]),
    )


def reconcile_candidate_provider_owners(checkpoint: Path) -> dict[str, int]:
    """Assign each exact Spotify track to the strongest structured candidate."""
    with _connect(checkpoint) as connection:
        rows = connection.execute(
            "SELECT c.recording_mbid,c.spotify_url,c.spotify_track_id,c.stream_count,"
            "c.spotify_fetched_at,c.spotify_status,c.apple_music_url,c.apple_track_id,"
            "c.apple_payload_json,c.apple_status,c.version_status,c.version_reason,"
            "c.isrcs_json,MAX(cs.source='musicbrainz_studio_discography') studio,"
            "MIN(CASE WHEN cs.source='listenbrainz_top_recordings' THEN cs.source_rank END) "
            "lb_rank FROM candidates c LEFT JOIN candidate_sources cs USING(recording_mbid) "
            "WHERE c.spotify_url LIKE 'https://open.spotify.com/track/%' "
            "AND c.stream_count IS NOT NULL GROUP BY c.recording_mbid"
        ).fetchall()
        by_track: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            track_id = str(row["spotify_url"]).split("/track/", 1)[-1].split("?", 1)[0]
            if track_id:
                by_track.setdefault(track_id, []).append(row)
        reassigned = duplicate_rows = apple_transfers = 0
        now = utc_now()
        for track_id, group in by_track.items():
            if len(group) < 2:
                continue
            winner = max(group, key=_provider_owner_priority)
            winner_mbid = str(winner["recording_mbid"])
            stream_count = max(int(row["stream_count"]) for row in group)
            apple_owner = next(
                (
                    row
                    for row in sorted(group, key=_provider_owner_priority, reverse=True)
                    if row["apple_track_id"] is not None and row["apple_status"] == "complete"
                ),
                None,
            )
            current_owner = next(
                (row for row in group if str(row["spotify_track_id"] or "") == track_id),
                None,
            )
            if current_owner is None or str(current_owner["recording_mbid"]) != winner_mbid:
                reassigned += 1
            connection.execute(
                "UPDATE candidates SET spotify_track_id=NULL WHERE spotify_track_id=?",
                (track_id,),
            )
            if apple_owner is not None:
                connection.execute(
                    "UPDATE candidates SET apple_track_id=NULL WHERE apple_track_id=?",
                    (str(apple_owner["apple_track_id"]),),
                )
                if str(apple_owner["recording_mbid"]) != winner_mbid:
                    connection.execute(
                        "UPDATE candidates SET apple_status="
                        "'failure:duplicate_candidate_apple_track',updated_at=? "
                        "WHERE recording_mbid=?",
                        (now, str(apple_owner["recording_mbid"])),
                    )
            for row in group:
                mbid = str(row["recording_mbid"])
                if mbid == winner_mbid:
                    continue
                connection.execute(
                    "UPDATE candidates SET spotify_status='duplicate_candidate_spotify_track',"
                    "version_status='excluded',"
                    "version_reason='duplicate_candidate_spotify_track',accepted=0,"
                    "decision_reason='duplicate_candidate_spotify_track',updated_at=? "
                    "WHERE recording_mbid=?",
                    (now, mbid),
                )
                duplicate_rows += 1
            restore_version = str(winner["version_reason"] or "") in (
                RESTORABLE_PROVIDER_DUPLICATE_REASONS
            )
            connection.execute(
                "UPDATE candidates SET spotify_track_id=?,spotify_status='complete',"
                "stream_count=?,accepted=NULL,decision_reason=NULL,"
                "version_status=CASE WHEN ? THEN 'eligible' ELSE version_status END,"
                "version_reason=CASE WHEN ? THEN NULL ELSE version_reason END,updated_at=? "
                "WHERE recording_mbid=?",
                (track_id, stream_count, restore_version, restore_version, now, winner_mbid),
            )
            if apple_owner is not None:
                connection.execute(
                    "UPDATE candidates SET apple_music_url=?,apple_track_id=?,apple_payload_json=?,"
                    "apple_status='complete',enrichment_status='verified',updated_at=? "
                    "WHERE recording_mbid=?",
                    (
                        apple_owner["apple_music_url"],
                        apple_owner["apple_track_id"],
                        apple_owner["apple_payload_json"],
                        now,
                        winner_mbid,
                    ),
                )
                apple_transfers += str(apple_owner["recording_mbid"]) != winner_mbid
        return {
            "spotify_owners_reassigned": reassigned,
            "duplicate_rows": duplicate_rows,
            "apple_owners_transferred": apple_transfers,
        }


def reset_retryable_apple_failures(checkpoint: Path, artist_mbid: str | None = None) -> int:
    """Return previously unavailable, otherwise viable candidates to the Apple queue."""
    with _connect(checkpoint) as connection:
        parameters: list[Any] = [utc_now(), MINIMUM_STREAMS]
        artist_clause = ""
        if artist_mbid is not None:
            artist_clause = (
                " AND EXISTS (SELECT 1 FROM candidate_artists ca "
                "WHERE ca.recording_mbid=candidates.recording_mbid AND ca.artist_mbid=?)"
            )
            parameters.append(artist_mbid)
        cursor = connection.execute(
            "UPDATE candidates SET apple_status='pending',accepted=NULL,decision_reason=NULL,"
            "updated_at=? WHERE version_status='eligible' AND spotify_status='complete' "
            "AND stream_count>=? AND (apple_status='unavailable' OR apple_status LIKE 'failure:%')"
            + artist_clause,
            parameters,
        )
        return cursor.rowcount


def reconcile_baseline_spotify_duplicates(checkpoint: Path) -> int:
    """Reject candidate MBIDs that map to an immutable baseline Spotify track."""
    with _connect(checkpoint) as connection:
        baseline_track_ids = {
            str(row[0]).split("/track/", 1)[-1].split("?", 1)[0]
            for row in connection.execute(
                "SELECT spotify_url FROM baseline_songs WHERE spotify_url LIKE "
                "'https://open.spotify.com/track/%'"
            )
        }
        changed = 0
        for rowid, track_id in connection.execute(
            "SELECT rowid,spotify_track_id FROM candidates WHERE spotify_track_id IS NOT NULL"
        ):
            if str(track_id) not in baseline_track_ids:
                continue
            connection.execute(
                "UPDATE candidates SET spotify_track_id=NULL,"
                "spotify_status='duplicate_baseline_spotify_track',"
                "version_status='excluded',"
                "version_reason='duplicate_baseline_spotify_track',updated_at=? "
                "WHERE rowid=?",
                (utc_now(), int(rowid)),
            )
            changed += 1
    return changed


def reconcile_isrc_duplicates(checkpoint: Path, cache: Path) -> dict[str, int]:
    """Deduplicate exact recordings through shared ISRCs, preferring verified rows."""
    with _connect(checkpoint) as connection:
        baseline_rows = connection.execute(
            "SELECT recording_mbid,isrcs_json,isrc_status FROM baseline_songs ORDER BY song_id"
        ).fetchall()
    missing_baseline = [str(row[0]) for row in baseline_rows if str(row[2]) != "complete"]
    if missing_baseline:
        metadata = fetch_musicbrainz_metadata(
            cache, [{"recording_mbid": mbid} for mbid in missing_baseline]
        )
        with _connect(checkpoint) as connection:
            for mbid in missing_baseline:
                isrcs = sorted(
                    {
                        str(value).strip().upper()
                        for value in (metadata.get(mbid) or {}).get("isrcs") or []
                        if str(value).strip()
                    }
                )
                connection.execute(
                    "UPDATE baseline_songs SET isrcs_json=?,isrc_status='complete' "
                    "WHERE recording_mbid=?",
                    (json.dumps(isrcs), mbid),
                )
    with _connect(checkpoint) as connection:
        baseline_isrcs = {
            str(isrc)
            for row in connection.execute("SELECT isrcs_json FROM baseline_songs")
            for isrc in json.loads(row[0])
        }
        rows = connection.execute(
            "SELECT c.recording_mbid,c.isrcs_json,c.apple_status,c.spotify_status,"
            "c.stream_count,MAX(cs.source='musicbrainz_studio_discography') studio,"
            "MIN(CASE WHEN cs.source='listenbrainz_top_recordings' THEN cs.source_rank END) "
            "lb_rank FROM candidates c LEFT JOIN candidate_sources cs USING(recording_mbid) "
            "WHERE c.version_status='eligible' AND c.isrcs_json!='[]' "
            "GROUP BY c.recording_mbid ORDER BY (c.apple_status='complete') DESC,"
            "(c.spotify_status='complete') DESC,COALESCE(c.stream_count,-1) DESC,studio DESC,"
            "CASE WHEN lb_rank IS NULL THEN 1 ELSE 0 END,lb_rank,c.recording_mbid"
        ).fetchall()
        owners: dict[str, str] = {}
        baseline_duplicates = candidate_duplicates = 0
        for row in rows:
            mbid = str(row[0])
            isrcs = {str(value) for value in json.loads(row[1])}
            if isrcs & baseline_isrcs:
                connection.execute(
                    "UPDATE candidates SET accepted=0,decision_reason='duplicate_baseline_isrc',"
                    "version_status='excluded',version_reason='duplicate_baseline_isrc',"
                    "updated_at=? WHERE recording_mbid=?",
                    (utc_now(), mbid),
                )
                baseline_duplicates += 1
                continue
            if any(isrc in owners for isrc in isrcs):
                connection.execute(
                    "UPDATE candidates SET accepted=0,decision_reason='duplicate_candidate_isrc',"
                    "version_status='excluded',version_reason='duplicate_candidate_isrc',"
                    "updated_at=? WHERE recording_mbid=?",
                    (utc_now(), mbid),
                )
                candidate_duplicates += 1
                continue
            for isrc in isrcs:
                owners[isrc] = mbid
    return {
        "baseline_duplicates": baseline_duplicates,
        "candidate_duplicates": candidate_duplicates,
    }


def scrape_artist_spotify(
    checkpoint: Path,
    artist_mbid: str,
    *,
    lb_priority_limit: int,
    limit: int | None,
    browser_workers: int,
    batch_size: int,
    playwright_cli: Path,
    browser_executable: Path,
    timeout_seconds: int,
    retry_failures: bool,
) -> dict[str, int]:
    reconciled = reconcile_baseline_spotify_duplicates(checkpoint)
    jobs = _spotify_jobs_for_artist(
        checkpoint,
        artist_mbid,
        lb_priority_limit=lb_priority_limit,
        limit=limit,
        retry_failures=retry_failures,
    )
    completed = failed = 0
    metric_totals: Counter[str] = Counter()
    print(
        f"Spotify pilot: {len(jobs):,} queued; {browser_workers} pages; "
        f"commits every {batch_size} candidates.",
        flush=True,
    )
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start : start + batch_size]
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                result = run_playwright(
                    batch,
                    workers=browser_workers,
                    search_candidates=3,
                    playwright_cli=playwright_cli,
                    browser_executable=browser_executable,
                    timeout_seconds=timeout_seconds,
                )
                break
            except (RuntimeError, TypeError, json.JSONDecodeError) as error:
                last_error = error
                print(
                    f"  Spotify browser attempt {attempt}/2 failed: {type(error).__name__}",
                    flush=True,
                )
        if result is None:
            if last_error is None:
                raise RuntimeError("Spotify browser batch returned no result")
            raise last_error
        successes = list(result.get("results", []))
        failures = list(result.get("failures", []))
        metric_totals.update(
            {str(key): int(value) for key, value in result.get("metrics", {}).items()}
        )
        _persist_spotify_batch(checkpoint, successes, failures)
        completed += len(successes)
        failed += len(failures)
        print(
            f"  Spotify committed {start + len(batch):,}/{len(jobs):,}: "
            f"{completed:,} complete, {failed:,} failed",
            flush=True,
        )
    with _connect(checkpoint) as connection:
        connection.execute(
            "UPDATE artists SET spotify_status='complete' WHERE artist_mbid=?",
            (artist_mbid,),
        )
    return {
        "queued": len(jobs),
        "completed": completed,
        "failed": failed,
        "baseline_duplicates_reconciled": reconciled,
        "browser_metrics": dict(metric_totals),
    }


def enrich_accepted_apple(
    checkpoint: Path,
    cache: Path,
    *,
    country: str = "US",
    limit: int | None = None,
    retry_failures: bool = False,
    include_unselected_above_threshold: bool = False,
) -> dict[str, int]:
    """Resolve only provisionally accepted recordings to verified Apple tracks."""
    apple_status_clause = (
        "c.apple_status!='complete'" if retry_failures else "c.apple_status='pending'"
    )
    with _connect(checkpoint) as connection:
        selection_clause = "c.accepted=1"
        parameters: list[Any] = []
        if include_unselected_above_threshold:
            selection_clause = (
                "(c.accepted=1 OR (c.accepted IS NULL AND c.version_status='eligible' "
                "AND c.spotify_status='complete' AND c.stream_count>=?))"
            )
            parameters.append(MINIMUM_STREAMS)
        parameters.append(limit if limit is not None else -1)
        rows = connection.execute(
            "SELECT recording_mbid,title,display_artist,release_name "
            "FROM candidates c WHERE " + selection_clause + " AND " + apple_status_clause + " "
            "ORDER BY stream_count DESC LIMIT ?",
            parameters,
        ).fetchall()
    candidates = [
        {
            "recording_mbid": str(row[0]),
            "track_name": str(row[1]),
            "artist_name": str(row[2] or ""),
            "release_name": str(row[3] or ""),
        }
        for row in rows
    ]
    metadata = fetch_musicbrainz_metadata(cache, candidates)
    matches: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for candidate in candidates:
        mbid = candidate["recording_mbid"]
        try:
            apple = search_apple_track(
                cache,
                candidate,
                metadata.get(mbid, {}),
                country=country,
                year_min=1800,
                year_max=2200,
            )
            if not apple:
                apple = _musicbrainz_apple_relationship_match(
                    cache,
                    candidate,
                    metadata.get(mbid, {}),
                    country=country,
                )
            if not apple:
                apple = _targeted_apple_match(
                    cache,
                    candidate,
                    metadata.get(mbid, {}),
                    country=country,
                )
        except Exception as error:  # noqa: BLE001 - candidate remains retryable.
            failures[mbid] = "lookup_failure:" + type(error).__name__
            continue
        if not apple or not apple.get("trackViewUrl") or not apple.get("previewUrl"):
            failures[mbid] = "unavailable"
            continue
        matches[mbid] = apple
    preview_statuses = validate_previews(
        cache,
        [str(value["previewUrl"]) for value in matches.values()],
        max_workers=5,
    )
    with _connect(checkpoint) as connection:
        baseline_apple_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT apple_track_id FROM baseline_songs WHERE apple_track_id IS NOT NULL"
            )
        }
        for mbid, apple in matches.items():
            preview_status = preview_statuses.get(str(apple["previewUrl"]), "transient")
            if preview_status != "valid":
                failures[mbid] = "preview_" + preview_status
                continue
            apple_track_id = str(apple["trackId"])
            duplicate = connection.execute(
                "SELECT recording_mbid FROM candidates WHERE apple_track_id=? "
                "AND recording_mbid!=?",
                (apple_track_id, mbid),
            ).fetchone()
            if apple_track_id in baseline_apple_ids or duplicate is not None:
                reason = (
                    "duplicate_baseline_apple_track"
                    if apple_track_id in baseline_apple_ids
                    else "duplicate_candidate_apple_track"
                )
                failures[mbid] = reason
                connection.execute(
                    "UPDATE candidates SET accepted=0,decision_reason=?,"
                    "version_status='excluded',version_reason=?,updated_at=? "
                    "WHERE recording_mbid=?",
                    (reason, reason, utc_now(), mbid),
                )
                continue
            connection.execute(
                "UPDATE candidates SET apple_music_url=?,apple_track_id=?,"
                "apple_payload_json=?,apple_status='complete',"
                "enrichment_status='verified',updated_at=? "
                "WHERE recording_mbid=?",
                (
                    str(apple["trackViewUrl"]),
                    apple_track_id,
                    json.dumps(
                        {
                            key: apple.get(key)
                            for key in (
                                "trackId",
                                "trackName",
                                "artistName",
                                "collectionName",
                                "releaseDate",
                                "canonicalReleaseYear",
                                "previewUrl",
                                "artworkUrl100",
                                "trackViewUrl",
                                "trackExplicitness",
                                "primaryGenreName",
                                "trackTimeMillis",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                    mbid,
                ),
            )
        for mbid, reason in failures.items():
            connection.execute(
                "UPDATE candidates SET apple_status=?,updated_at=? WHERE recording_mbid=? "
                "AND apple_status!='complete'",
                (
                    "unavailable" if reason == "unavailable" else "failure:" + reason,
                    utc_now(),
                    mbid,
                ),
            )
        connection.execute(
            "UPDATE candidates SET enrichment_status='verified',updated_at=? "
            "WHERE spotify_status='complete' AND apple_status='complete' "
            "AND enrichment_status!='populated'",
            (utc_now(),),
        )
    return {
        "queued": len(candidates),
        "complete": len(matches) - sum(mbid in failures for mbid in matches),
        "failed": len(failures),
    }


def _musicbrainz_apple_relationship_match(
    cache: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    *,
    country: str,
) -> dict[str, Any] | None:
    mbid = str(candidate["recording_mbid"])
    path = cache / ALGORITHM_VERSION / "musicbrainz-apple-relations" / f"{mbid}.json"
    query = urllib.parse.urlencode({"inc": "url-rels", "fmt": "json"})
    payload = _cache_json(
        path,
        f"https://musicbrainz.org/ws/2/recording/{mbid}?{query}",
        read_json,
    )
    apple_ids: list[str] = []
    for relation in payload.get("relations", []):
        resource = str((relation.get("url") or {}).get("resource") or "")
        if "music.apple.com/" not in resource and "itunes.apple.com/" not in resource:
            continue
        match = re.search(r"/song/(\d+)", resource) or re.search(r"[?&]i=(\d+)", resource)
        if match and match.group(1) not in apple_ids:
            apple_ids.append(match.group(1))
    if not apple_ids:
        return None
    parameters = urllib.parse.urlencode(
        {"id": ",".join(apple_ids), "entity": "song", "country": country.upper()}
    )
    lookup = read_json("https://itunes.apple.com/lookup?" + parameters)
    results = [
        dict(item)
        for item in lookup.get("results", [])
        if item.get("trackId") is not None and item.get("previewUrl")
    ]
    if not results:
        return None
    expected_duration = metadata.get("length")
    if isinstance(expected_duration, (int, float)):
        results = [
            item
            for item in results
            if not isinstance(item.get("trackTimeMillis"), (int, float))
            or abs(int(expected_duration) - int(item["trackTimeMillis"])) <= 3500
        ]
    if not results:
        return None
    selected = min(
        results,
        key=lambda item: abs(
            int(expected_duration or item.get("trackTimeMillis") or 0)
            - int(item.get("trackTimeMillis") or 0)
        ),
    )
    first_release_date = str(metadata.get("first-release-date") or "")
    apple_release_date = str(selected.get("releaseDate") or "")
    year_text = (
        first_release_date[:4]
        if len(first_release_date) >= 4 and first_release_date[:4].isdigit()
        else apple_release_date[:4]
    )
    if len(year_text) != 4 or not year_text.isdigit():
        return None
    selected["canonicalReleaseYear"] = int(year_text)
    cache_apple_track(cache, mbid, selected, country=country)
    return selected


def _targeted_apple_match(
    cache: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    *,
    country: str,
) -> dict[str, Any] | None:
    """Fallback to a track-targeted Apple search with duration validation."""
    title = str(metadata.get("title") or candidate.get("track_name") or "").strip()
    credits = _artist_credits(metadata)
    primary_artist = (
        str(credits[0]["credited_name"]) if credits else str(candidate.get("artist_name") or "")
    )
    if not title or not primary_artist:
        return None
    parameters = urllib.parse.urlencode(
        {
            "term": f"{title} {primary_artist}",
            "media": "music",
            "entity": "song",
            "country": country.upper(),
            "limit": 50,
        }
    )
    payload = read_json("https://itunes.apple.com/search?" + parameters)
    first_release_date = str(metadata.get("first-release-date") or "")
    canonical_year = (
        int(first_release_date[:4])
        if len(first_release_date) >= 4 and first_release_date[:4].isdigit()
        else None
    )
    expected_duration = metadata.get("length")
    results = list(payload.get("results", []))
    if isinstance(expected_duration, (int, float)):
        results = [
            item
            for item in results
            if not isinstance(item.get("trackTimeMillis"), (int, float))
            or abs(int(expected_duration) - int(item["trackTimeMillis"])) <= 3500
        ]

    def base_title(value: object) -> str:
        normalized = re.sub(
            r"\s*[\[(]\s*(?:feat\.?|featuring|ft\.?|with).*?[\])]\s*$",
            "",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        return "".join(character for character in normalized.casefold() if character.isalnum())

    def title_matches(expected: object, returned: object) -> bool:
        expected_key = base_title(expected)
        returned_key = base_title(returned)
        if expected_key == returned_key:
            return True
        pairs = (
            (str(expected or ""), returned_key),
            (str(returned or ""), expected_key),
        )
        for censored, full in pairs:
            parts = re.split(r"[*_•]+", censored)
            if len(parts) < 2:
                continue
            pattern = ".*".join(re.escape(base_title(part)) for part in parts)
            if re.fullmatch(pattern, full):
                return True
        return False

    candidates: list[dict[str, Any]] = []
    for item in results:
        returned_title = str(item.get("trackName") or "")
        if not title_matches(title, returned_title):
            continue
        if EXCLUDED_VERSION_PATTERN.search(returned_title) and not EXCLUDED_VERSION_PATTERN.search(
            title
        ):
            continue
        returned_artist = str(item.get("artistName") or "")
        primary_key = "".join(
            character for character in primary_artist.casefold() if character.isalnum()
        )
        returned_key = "".join(
            character for character in returned_artist.casefold() if character.isalnum()
        )
        if primary_key not in returned_key and _similarity(primary_artist, returned_artist) < 0.62:
            continue
        candidates.append(dict(item))
    if not candidates:
        return None
    selected = max(
        candidates,
        key=lambda item: (
            {"cleaned": 0, "notExplicit": 1, "explicit": 2}.get(
                str(item.get("trackExplicitness") or ""), 1
            ),
            -abs(
                int(expected_duration or item.get("trackTimeMillis") or 0)
                - int(item.get("trackTimeMillis") or 0)
            ),
            -int(item.get("trackId") or 0),
        ),
    )
    apple_release_date = str(selected.get("releaseDate") or "")
    apple_year = (
        int(apple_release_date[:4])
        if len(apple_release_date) >= 4 and apple_release_date[:4].isdigit()
        else None
    )
    if canonical_year is None and apple_year is None:
        return None
    selected["canonicalReleaseYear"] = canonical_year or apple_year
    candidate_duration = selected.get("trackTimeMillis")
    if (
        isinstance(expected_duration, (int, float))
        and isinstance(candidate_duration, (int, float))
        and abs(int(expected_duration) - int(candidate_duration)) > 3500
    ):
        return None
    cache_apple_track(
        cache,
        str(candidate["recording_mbid"]),
        selected,
        country=country,
    )
    return selected


def append_verified_candidates(
    database: Path,
    checkpoint: Path,
    cache: Path,
    *,
    target_total: int | None = None,
) -> dict[str, int]:
    """Append fully verified accepted rows without rebuilding the baseline."""
    ensure_baseline(database, checkpoint)
    with sqlite3.connect(database) as catalog:
        current_total = int(
            catalog.execute("SELECT COUNT(*) FROM songs WHERE enabled=1").fetchone()[0]
        )
    remaining = None if target_total is None else max(0, target_total - current_total)
    if remaining == 0:
        return {"queued": 0, "inserted_or_reenabled": 0}
    with _connect(checkpoint) as connection:
        rows = connection.execute(
            "SELECT c.recording_mbid,c.title,c.display_artist,c.apple_payload_json,"
            "c.spotify_url,c.stream_count FROM candidates c "
            "LEFT JOIN baseline_songs b USING(recording_mbid) "
            "WHERE c.accepted=1 AND c.spotify_status='complete' "
            "AND c.apple_status='complete' AND c.apple_payload_json IS NOT NULL "
            "AND b.recording_mbid IS NULL AND c.enrichment_status!='populated' "
            "ORDER BY c.stream_count DESC LIMIT ?",
            (remaining if remaining is not None else -1,),
        ).fetchall()
    candidates = [
        {
            "recording_mbid": str(row[0]),
            "track_name": str(row[1]),
            "artist_name": str(row[2] or ""),
        }
        for row in rows
    ]
    metadata = fetch_musicbrainz_metadata(cache, candidates)
    countries = fetch_musicbrainz_artist_countries(cache, metadata)
    prepared = [
        {
            "candidate": candidate,
            "apple": json.loads(row[3]),
        }
        for candidate, row in zip(candidates, rows, strict=True)
    ]
    inserted = write_catalog(
        database,
        prepared,
        metadata,
        countries_by_recording=countries,
    )
    fetched_at = utc_now()
    with sqlite3.connect(database) as catalog, _connect(checkpoint) as checkpoint_connection:
        for row in rows:
            mbid = str(row[0])
            song = catalog.execute(
                "SELECT id FROM songs WHERE musicbrainz_id=? AND enabled=1", (mbid,)
            ).fetchone()
            if song is None:
                checkpoint_connection.execute(
                    "UPDATE candidates SET enrichment_status='population_failed',updated_at=? "
                    "WHERE recording_mbid=?",
                    (fetched_at, mbid),
                )
                continue
            catalog.execute(
                "UPDATE songs SET spotify_url=?,stream_count=?,stream_count_fetched_at=?,"
                "stream_count_source='spotify_web_hydration',stream_count_status='complete' "
                "WHERE id=?",
                (str(row[4]), int(row[5]), fetched_at, int(song[0])),
            )
            checkpoint_connection.execute(
                "UPDATE candidates SET enrichment_status='populated',updated_at=? "
                "WHERE recording_mbid=?",
                (fetched_at, mbid),
            )
    recalculate_popularity_scores(database)
    ensure_baseline(database, checkpoint)
    return {"queued": len(rows), "inserted_or_reenabled": inserted}


def select_global_candidates(checkpoint: Path) -> dict[str, int]:
    """Apply the stream threshold and credited-artist cap in global stream order."""
    with _connect(checkpoint) as connection:
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT artist_mbid,baseline_count FROM artists")
        }
        credits = {
            str(row[0]): [
                str(value[0])
                for value in connection.execute(
                    "SELECT artist_mbid FROM candidate_artists WHERE recording_mbid=? "
                    "ORDER BY credit_order",
                    (row[0],),
                )
            ]
            for row in connection.execute("SELECT recording_mbid FROM candidates")
        }
        rows = connection.execute(
            "SELECT c.recording_mbid,c.stream_count,c.spotify_status,c.version_status,"
            "c.version_reason,c.apple_status,"
            "CASE WHEN b.recording_mbid IS NULL THEN 0 ELSE 1 END baseline "
            "FROM candidates c LEFT JOIN baseline_songs b USING(recording_mbid) "
            "ORDER BY COALESCE(c.stream_count,-1) DESC,c.recording_mbid"
        ).fetchall()
        connection.execute(
            "UPDATE candidates SET accepted=NULL,decision_reason=NULL "
            "WHERE enrichment_status!='populated'"
        )
        accepted = below_threshold = unresolved = capped = excluded = 0
        for row in rows:
            mbid = str(row[0])
            if int(row[6]):
                decision = (0, "immutable_baseline")
            elif str(row[3]) != "eligible":
                excluded += 1
                decision = (0, str(row[4] or "excluded_version"))
            elif str(row[5]) == "unavailable":
                excluded += 1
                decision = (0, "apple_match_unavailable")
            elif str(row[2]) != "complete" or row[1] is None:
                unresolved += 1
                decision = (0, "unresolved_spotify_stream_count")
            elif int(row[1]) < MINIMUM_STREAMS:
                below_threshold += 1
                decision = (0, "below_200m_spotify_streams")
            else:
                artist_credits = credits.get(mbid, [])
                if not artist_credits:
                    excluded += 1
                    decision = (0, "missing_canonical_artist_credit")
                elif any(counts.get(credit, 0) >= ARTIST_CAP for credit in artist_credits):
                    capped += 1
                    decision = (0, "credited_artist_cap_reached")
                else:
                    accepted += 1
                    decision = (1, "accepted_by_streams_and_artist_cap")
                    for credit in artist_credits:
                        counts[credit] = counts.get(credit, 0) + 1
            connection.execute(
                "UPDATE candidates SET accepted=?,decision_reason=?,updated_at=? "
                "WHERE recording_mbid=?",
                (decision[0], decision[1], utc_now(), mbid),
            )
    return {
        "accepted": accepted,
        "below_threshold": below_threshold,
        "unresolved": unresolved,
        "cap_rejected": capped,
        "excluded": excluded,
    }


def select_target_candidates(checkpoint: Path, *, target_new: int) -> dict[str, int]:
    """Select the strongest verified candidates for a bounded growth milestone.

    Artist caps are intentionally deferred until the catalog reaches its target.
    Permanent Apple failures are replaced by the next candidate in stream order.
    """
    if target_new < 0:
        raise ValueError("target_new must be non-negative")
    with _connect(checkpoint) as connection:
        rows = connection.execute(
            "SELECT c.recording_mbid,c.stream_count,c.spotify_status,c.version_status,"
            "c.version_reason,c.apple_status,c.enrichment_status,"
            "CASE WHEN b.recording_mbid IS NULL THEN 0 ELSE 1 END baseline "
            "FROM candidates c LEFT JOIN baseline_songs b USING(recording_mbid) "
            "ORDER BY COALESCE(c.stream_count,-1) DESC,c.recording_mbid"
        ).fetchall()
        connection.execute(
            "UPDATE candidates SET accepted=NULL,decision_reason=NULL "
            "WHERE enrichment_status!='populated'"
        )
        accepted = excluded = unresolved = apple_rejected = 0
        for row in rows:
            mbid = str(row[0])
            if int(row[7]):
                decision = (0, "baseline_not_growth_candidate")
            elif str(row[6]) == "population_failed":
                excluded += 1
                decision = (0, "population_failed")
            elif str(row[3]) != "eligible":
                excluded += 1
                decision = (0, str(row[4] or "excluded_version"))
            elif str(row[5]) == "unavailable" or str(row[5]).startswith("failure:"):
                apple_rejected += 1
                decision = (0, "apple_match_unavailable")
            elif str(row[2]) != "complete" or row[1] is None:
                unresolved += 1
                decision = (0, "unresolved_spotify_stream_count")
            elif accepted >= target_new:
                decision = (0, "outside_20k_stream_ranking")
            else:
                accepted += 1
                decision = (1, "accepted_for_20k_stream_ranking")
            connection.execute(
                "UPDATE candidates SET accepted=?,decision_reason=?,updated_at=? "
                "WHERE recording_mbid=?",
                (decision[0], decision[1], utc_now(), mbid),
            )
    return {
        "target_new": target_new,
        "accepted": accepted,
        "excluded": excluded,
        "unresolved": unresolved,
        "apple_rejected": apple_rejected,
    }


def trim_catalog_artist_caps(
    database: Path,
    checkpoint: Path,
    report: Path,
    *,
    artist_cap: int = ARTIST_CAP,
) -> dict[str, Any]:
    """Keep each credited artist's strongest enabled songs and disable the rest."""
    if artist_cap < 1:
        raise ValueError("artist_cap must be positive")
    ensure_baseline(database, checkpoint)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        songs = connection.execute(
            "SELECT id,musicbrainz_id,title,artist,stream_count FROM songs "
            "WHERE enabled=1 ORDER BY COALESCE(stream_count,-1) DESC,id"
        ).fetchall()
        credits: dict[int, list[tuple[str, str]]] = {}
        for row in connection.execute(
            "SELECT sa.song_id,a.musicbrainz_id,a.name FROM song_artists sa "
            "JOIN artists a ON a.id=sa.artist_id JOIN songs s ON s.id=sa.song_id "
            "WHERE s.enabled=1 ORDER BY sa.song_id,sa.credit_order"
        ):
            credits.setdefault(int(row[0]), []).append((str(row[1]), str(row[2])))

        artist_counts: Counter[str] = Counter()
        artist_names: dict[str, str] = {}
        removed: list[dict[str, Any]] = []
        for song in songs:
            song_credits = credits.get(int(song["id"]), [])
            for artist_mbid, artist_name in song_credits:
                artist_names[artist_mbid] = artist_name
            capped = [
                artist_mbid
                for artist_mbid, _artist_name in song_credits
                if artist_counts[artist_mbid] >= artist_cap
            ]
            if capped:
                removed.append(
                    {
                        "song_id": int(song["id"]),
                        "recording_mbid": str(song["musicbrainz_id"]),
                        "title": str(song["title"]),
                        "artist": str(song["artist"]),
                        "stream_count": int(song["stream_count"] or 0),
                        "capped_artist_mbids": capped,
                    }
                )
                continue
            for artist_mbid, _artist_name in song_credits:
                artist_counts[artist_mbid] += 1

        connection.executemany(
            "UPDATE songs SET enabled=0 WHERE id=?",
            [(item["song_id"],) for item in removed],
        )
        ending_count = int(
            connection.execute("SELECT COUNT(*) FROM songs WHERE enabled=1").fetchone()[0]
        )

    removed_mbids = [str(item["recording_mbid"]) for item in removed]
    if removed_mbids:
        with _connect(checkpoint) as connection:
            for start in range(0, len(removed_mbids), 800):
                batch = removed_mbids[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    "UPDATE candidates SET accepted=0,"
                    "decision_reason='trimmed_credited_artist_cap',updated_at=? "
                    f"WHERE recording_mbid IN ({placeholders})",
                    (utc_now(), *batch),
                )
    recalculate_popularity_scores(database)
    capped_artists = [
        {
            "artist_mbid": artist_mbid,
            "name": artist_names.get(artist_mbid, ""),
            "kept": count,
        }
        for artist_mbid, count in sorted(
            artist_counts.items(), key=lambda item: (-item[1], artist_names.get(item[0], ""))
        )
        if count >= artist_cap
    ]
    payload = {
        "generated_at": utc_now(),
        "artist_cap": artist_cap,
        "starting_count": len(songs),
        "ending_count": ending_count,
        "disabled_count": len(removed),
        "artists_at_cap": capped_artists,
        "disabled_songs": removed,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_suffix(report.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report)
    return payload


def build_final_audit(database: Path, checkpoint: Path, report: Path) -> dict[str, Any]:
    baseline = ensure_baseline(database, checkpoint)
    with sqlite3.connect(database) as catalog:
        ending_count = int(
            catalog.execute("SELECT COUNT(*) FROM songs WHERE enabled=1").fetchone()[0]
        )
    with _connect(checkpoint) as connection:
        per_artist = [
            dict(row)
            for row in connection.execute(
                "SELECT a.artist_mbid,a.name,a.baseline_count existing_count,"
                "COUNT(DISTINCT cs.recording_mbid) considered_count,"
                "COUNT(DISTINCT CASE WHEN c.accepted=1 THEN c.recording_mbid END) accepted_count "
                "FROM artists a LEFT JOIN candidate_sources cs "
                "ON cs.source_artist_mbid=a.artist_mbid LEFT JOIN candidates c "
                "ON c.recording_mbid=cs.recording_mbid GROUP BY a.artist_mbid "
                "ORDER BY accepted_count DESC,considered_count DESC,a.name"
            )
        ]
        accepted = [
            dict(row)
            for row in connection.execute(
                "SELECT recording_mbid,title,display_artist,stream_count,spotify_url,"
                "apple_music_url,enrichment_status FROM candidates WHERE accepted=1 "
                "ORDER BY stream_count DESC"
            )
        ]
        missing_spotify = [
            dict(row)
            for row in connection.execute(
                "SELECT recording_mbid,title,display_artist,spotify_status FROM candidates "
                "WHERE decision_reason='unresolved_spotify_stream_count'"
            )
        ]
        missing_apple = [
            dict(row)
            for row in connection.execute(
                "SELECT recording_mbid,title,display_artist,apple_status FROM candidates "
                "WHERE accepted=1 AND apple_status!='complete'"
            )
        ]
        alternates = [
            dict(row)
            for row in connection.execute(
                "SELECT recording_mbid,title,display_artist,version_reason FROM candidates "
                "WHERE version_status='excluded' ORDER BY title"
            )
        ]
        below = [
            dict(row)
            for row in connection.execute(
                "SELECT recording_mbid,title,display_artist,stream_count FROM candidates "
                "WHERE decision_reason='below_200m_spotify_streams' "
                "ORDER BY stream_count DESC"
            )
        ]
        at_cap = [
            dict(row)
            for row in connection.execute(
                "SELECT a.artist_mbid,a.name,a.baseline_count+COUNT(DISTINCT CASE "
                "WHEN c.accepted=1 THEN c.recording_mbid END) final_count FROM artists a "
                "LEFT JOIN candidate_artists ca ON ca.artist_mbid=a.artist_mbid "
                "LEFT JOIN candidates c ON c.recording_mbid=ca.recording_mbid "
                "GROUP BY a.artist_mbid HAVING final_count>=? ORDER BY final_count DESC,a.name",
                (ARTIST_CAP,),
            )
        ]
    payload = {
        "generated_at": utc_now(),
        "algorithm_version": ALGORITHM_VERSION,
        "starting_catalog_count": BASELINE_SIZE,
        "ending_catalog_count": ending_count,
        "baseline": {**baseline, "all_original_identities_present": True},
        "per_artist": per_artist,
        "accepted_songs": accepted,
        "missing_spotify_matches": missing_spotify,
        "missing_apple_matches": missing_apple,
        "excluded_alternate_versions": alternates,
        "artists_reaching_or_exceeding_30": at_cap,
        "candidates_rejected_below_200m": below,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_suffix(report.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report)
    return payload


def build_pilot_report(checkpoint: Path, report: Path, artist_mbid: str) -> dict[str, Any]:
    with _connect(checkpoint) as connection:
        artist = connection.execute(
            "SELECT name,baseline_count,discovery_status FROM artists WHERE artist_mbid=?",
            (artist_mbid,),
        ).fetchone()
        baseline = [
            dict(row)
            for row in connection.execute(
                "SELECT b.recording_mbid,b.title,b.display_artist,b.stream_count,b.spotify_url,"
                "b.apple_music_url FROM baseline_songs b WHERE EXISTS "
                "(SELECT 1 FROM json_each(b.identity_json,'$.credits') c "
                "WHERE json_extract(c.value,'$.artist_mbid')=?) "
                "ORDER BY b.stream_count DESC",
                (artist_mbid,),
            )
        ]
        lb = [
            {
                "rank": row["source_rank"],
                "recording_mbid": row["recording_mbid"],
                "title": row["title"],
                "artist": row["display_artist"],
                "response": json.loads(row["source_payload_json"]),
            }
            for row in connection.execute(
                "SELECT cs.source_rank,c.recording_mbid,c.title,c.display_artist,"
                "cs.source_payload_json FROM candidate_sources cs JOIN candidates c "
                "USING(recording_mbid) WHERE cs.source_artist_mbid=? "
                "AND cs.source='listenbrainz_top_recordings' ORDER BY cs.source_rank",
                (artist_mbid,),
            )
        ]
        candidates = [
            dict(row)
            for row in connection.execute(
                "SELECT DISTINCT c.recording_mbid,c.title,c.display_artist,c.release_name,"
                "c.first_release_date,c.stream_count,c.spotify_url,c.spotify_status,"
                "c.apple_music_url,c.apple_status,c.version_status,c.version_reason,"
                "c.enrichment_status,"
                "CASE WHEN b.recording_mbid IS NULL THEN 0 ELSE 1 END baseline "
                "FROM candidates c JOIN candidate_sources cs USING(recording_mbid) "
                "LEFT JOIN baseline_songs b USING(recording_mbid) "
                "WHERE cs.source_artist_mbid=? ORDER BY COALESCE(c.stream_count,-1) DESC,c.title",
                (artist_mbid,),
            )
        ]
        groups = [
            dict(row)
            for row in connection.execute(
                "SELECT release_group_mbid,title,primary_type,secondary_types_json,status,"
                "failure_reason FROM release_groups WHERE artist_mbid=? ORDER BY title",
                (artist_mbid,),
            )
        ]
        baseline_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT artist_mbid,baseline_count FROM artists")
        }
        candidate_credits = {
            str(row[0]): [
                str(value[0])
                for value in connection.execute(
                    "SELECT artist_mbid FROM candidate_artists WHERE recording_mbid=? "
                    "ORDER BY credit_order",
                    (row[0],),
                )
            ]
            for row in connection.execute(
                "SELECT DISTINCT c.recording_mbid FROM candidates c "
                "JOIN candidate_sources cs USING(recording_mbid) WHERE cs.source_artist_mbid=?",
                (artist_mbid,),
            )
        }
        album_tracks = {
            album: [
                dict(row)
                for row in connection.execute(
                    "SELECT DISTINCT c.recording_mbid,c.title,c.stream_count,c.spotify_status "
                    "FROM candidates c JOIN release_groups rg "
                    "ON rg.release_group_mbid=c.release_group_mbid "
                    "WHERE rg.artist_mbid=? AND lower(rg.title)=lower(?) ORDER BY c.title",
                    (artist_mbid, album),
                )
            ]
            for album in MAJOR_KANYE_ALBUMS
        }

        counts = dict(baseline_counts)
        decisions: dict[str, tuple[int, str]] = {}
        for candidate in sorted(
            candidates,
            key=lambda item: (
                -int(item["stream_count"] or -1),
                str(item["recording_mbid"]),
            ),
        ):
            mbid = str(candidate["recording_mbid"])
            if candidate["baseline"]:
                decisions[mbid] = (0, "immutable_baseline")
                continue
            if candidate["version_status"] != "eligible":
                decisions[mbid] = (0, str(candidate["version_reason"] or "excluded_version"))
                continue
            if candidate["apple_status"] == "unavailable":
                decisions[mbid] = (0, "apple_match_unavailable")
                continue
            if candidate["spotify_status"] != "complete" or candidate["stream_count"] is None:
                decisions[mbid] = (0, "unresolved_spotify_stream_count")
                continue
            if int(candidate["stream_count"]) < MINIMUM_STREAMS:
                decisions[mbid] = (0, "below_200m_spotify_streams")
                continue
            artist_credits = candidate_credits.get(mbid, [])
            capped = [credit for credit in artist_credits if counts.get(credit, 0) >= ARTIST_CAP]
            if capped:
                decisions[mbid] = (0, "credited_artist_cap_reached")
                continue
            decisions[mbid] = (1, "accepted_by_streams_and_artist_cap")
            for credit in artist_credits:
                counts[credit] = counts.get(credit, 0) + 1
        connection.execute("UPDATE candidates SET accepted=NULL,decision_reason=NULL")
        connection.executemany(
            "UPDATE candidates SET accepted=?,decision_reason=?,updated_at=? "
            "WHERE recording_mbid=?",
            [(accepted, reason, utc_now(), mbid) for mbid, (accepted, reason) in decisions.items()],
        )

    considered_albums = {
        str(item["title"] or "").casefold() for item in groups if item["status"] == "complete"
    }
    album_evidence = {
        album: any(album.casefold() == value for value in considered_albums)
        for album in MAJOR_KANYE_ALBUMS
    }
    decision_rows = [
        {
            **candidate,
            "credited_artist_mbids": candidate_credits.get(str(candidate["recording_mbid"]), []),
            "accepted": bool(decisions.get(str(candidate["recording_mbid"]), (0, ""))[0]),
            "decision_reason": decisions.get(str(candidate["recording_mbid"]), (0, "unknown"))[1],
        }
        for candidate in candidates
    ]
    payload = {
        "generated_at": utc_now(),
        "algorithm_version": ALGORITHM_VERSION,
        "artist": dict(artist) if artist else {"artist_mbid": artist_mbid},
        "rules": {
            "minimum_spotify_streams": MINIMUM_STREAMS,
            "maximum_enabled_per_credited_artist": ARTIST_CAP,
            "album_cap": None,
            "baseline_is_immutable": True,
            "listenbrainz_is_discovery_only": True,
        },
        "baseline_songs": baseline,
        "authenticated_listenbrainz_top_recordings": lb,
        "release_groups": groups,
        "candidates": decision_rows,
        "major_album_coverage": album_evidence,
        "major_album_tracks_considered": album_tracks,
        "accepted_songs": sorted(
            [item for item in decision_rows if item["accepted"]],
            key=lambda item: -int(item["stream_count"] or 0),
        ),
        "accepted_missing_apple": [
            item
            for item in decision_rows
            if item["accepted"] and item["apple_status"] != "complete"
        ],
        "rejected_below_200m": sorted(
            [
                item
                for item in decision_rows
                if item["decision_reason"] == "below_200m_spotify_streams"
            ],
            key=lambda item: -int(item["stream_count"] or 0),
        ),
        "unresolved_spotify": [
            item
            for item in decision_rows
            if item["decision_reason"] == "unresolved_spotify_stream_count"
        ],
        "attempted_spotify_failures": [
            item for item in decision_rows if str(item["spotify_status"]).startswith("failure:")
        ],
        "deferred_lower_priority_candidates": [
            item
            for item in decision_rows
            if item["spotify_status"] == "pending"
            and item.get("enrichment_status", "pending") == "pending"
        ],
        "spotify_gate_ready": all(album_evidence.values())
        and all(album_tracks[album] for album in MAJOR_KANYE_ALBUMS),
        "catalog_was_modified": False,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_suffix(report.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report)
    return payload


def status(checkpoint: Path) -> dict[str, Any]:
    initialize_checkpoint(checkpoint)
    with _connect(checkpoint) as connection:
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "baseline_songs": int(
                connection.execute("SELECT COUNT(*) FROM baseline_songs").fetchone()[0]
            ),
            "artists": dict(
                connection.execute(
                    "SELECT COUNT(*) total,SUM(discovery_status='complete') complete,"
                    "SUM(discovery_status='failure') failure FROM artists"
                ).fetchone()
            ),
            "candidates": {
                key: int(value or 0)
                for key, value in dict(
                    connection.execute(
                        "SELECT COUNT(*) total,SUM(spotify_status='complete') spotify_complete,"
                        "SUM(apple_status='complete') apple_complete,SUM(accepted=1) accepted "
                        "FROM candidates"
                    ).fetchone()
                ).items()
            },
        }


def _claim_ready_spotify_artist(checkpoint: Path) -> str | None:
    """Atomically reserve one discovered artist for a Spotify session."""
    with _connect(checkpoint) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT artist_mbid FROM artists WHERE discovery_status='complete' "
            "AND eligibility_status='eligible' AND spotify_status='pending' "
            "ORDER BY baseline_count DESC,artist_mbid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        artist_mbid = str(row[0])
        connection.execute(
            "UPDATE artists SET spotify_status='processing',failure_reason=NULL "
            "WHERE artist_mbid=? AND spotify_status='pending'",
            (artist_mbid,),
        )
        return artist_mbid


def _spotify_candidate_inventory(checkpoint: Path) -> int:
    with _connect(checkpoint) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM candidates c LEFT JOIN baseline_songs b "
                "USING(recording_mbid) WHERE b.recording_mbid IS NULL "
                "AND c.version_status='eligible' AND c.spotify_status='complete' "
                "AND c.stream_count IS NOT NULL"
            ).fetchone()[0]
        )


def scrape_ready_artists_parallel(args: argparse.Namespace) -> dict[str, Any]:
    """Consume the frozen discovered-artist pool with isolated browser sessions."""
    sessions = max(1, int(args.spotify_sessions))
    target_new = max(0, int(args.target_total) - BASELINE_SIZE)
    inventory_target = max(target_new, (target_new * 5 + 3) // 4)
    completed = failed = 0
    result_lock = threading.Lock()
    stop_event = threading.Event()
    with _connect(args.checkpoint) as connection:
        connection.execute(
            "UPDATE artists SET spotify_status='pending' WHERE spotify_status='processing'"
        )
        if args.retry_failures:
            connection.execute(
                "UPDATE artists SET spotify_status='pending' WHERE spotify_status='failure' "
                "AND discovery_status='complete' AND eligibility_status='eligible'"
            )
        initial = int(
            connection.execute(
                "SELECT COUNT(*) FROM artists WHERE discovery_status='complete' "
                "AND eligibility_status='eligible' AND spotify_status='pending'"
            ).fetchone()[0]
        )
    print(
        f"Spotify pool: {initial:,} artists; {sessions} isolated sessions; "
        f"{args.browser_workers} pages/session; {args.scrape_limit or 'all'} candidates/artist.",
        flush=True,
    )

    def worker(worker_number: int) -> dict[str, int]:
        worker_completed = worker_failed = 0
        while not stop_event.is_set():
            if _spotify_candidate_inventory(args.checkpoint) >= inventory_target:
                stop_event.set()
                break
            artist_mbid = _claim_ready_spotify_artist(args.checkpoint)
            if artist_mbid is None:
                break
            try:
                prepare_artist_candidate_metadata(
                    args.checkpoint,
                    args.cache,
                    artist_mbid,
                    lb_priority_limit=args.lb_priority_limit,
                )
                spotify = scrape_artist_spotify(
                    args.checkpoint,
                    artist_mbid,
                    lb_priority_limit=args.lb_priority_limit,
                    limit=args.scrape_limit,
                    browser_workers=args.browser_workers,
                    batch_size=args.batch_size,
                    playwright_cli=args.playwright_cli,
                    browser_executable=args.browser_executable,
                    timeout_seconds=args.browser_timeout_seconds,
                    retry_failures=args.retry_failures,
                )
            except Exception as error:  # noqa: BLE001 - artist remains resumable.
                worker_failed += 1
                with _connect(args.checkpoint) as connection:
                    connection.execute(
                        "UPDATE artists SET spotify_status='failure',failure_reason=? "
                        "WHERE artist_mbid=?",
                        ((f"{type(error).__name__}: {error}")[:500], artist_mbid),
                    )
                print(
                    f"Spotify session {worker_number}: {artist_mbid} failed "
                    f"({type(error).__name__})",
                    flush=True,
                )
            else:
                worker_completed += 1
                metrics = spotify.get("browser_metrics", {})
                print(
                    f"Spotify session {worker_number}: {artist_mbid} complete; "
                    f"search direct/nav {metrics.get('directSearches', 0):,}/"
                    f"{metrics.get('navigatedSearches', 0):,}; count direct/nav "
                    f"{metrics.get('directHydrations', 0):,}/"
                    f"{metrics.get('navigatedHydrations', 0):,}",
                    flush=True,
                )
        return {"completed": worker_completed, "failed": worker_failed}

    executor = ThreadPoolExecutor(max_workers=sessions)
    try:
        futures = [executor.submit(worker, number) for number in range(1, sessions + 1)]
        for future in futures:
            worker_result = future.result()
            with result_lock:
                completed += int(worker_result["completed"])
                failed += int(worker_result["failed"])
    except KeyboardInterrupt:
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()
    return {
        "initial": initial,
        "completed": completed,
        "failed": failed,
        "inventory_target": inventory_target,
        "inventory_complete": _spotify_candidate_inventory(args.checkpoint),
    }


def _accepted_apple_counts(checkpoint: Path) -> tuple[int, int]:
    with _connect(checkpoint) as connection:
        row = connection.execute(
            "SELECT SUM(accepted=1 AND apple_status='complete'),"
            "SUM(accepted=1 AND apple_status='pending') FROM candidates"
        ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _ready_apple_count(checkpoint: Path, *, include_unselected: bool) -> int:
    selection_clause = "accepted=1"
    parameters: tuple[Any, ...] = ()
    if include_unselected:
        selection_clause = (
            "(accepted=1 OR (accepted IS NULL AND version_status='eligible' "
            "AND spotify_status='complete' AND stream_count>=?))"
        )
        parameters = (MINIMUM_STREAMS,)
    with _connect(checkpoint) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE apple_status='pending' AND "
                + selection_clause,
                parameters,
            ).fetchone()[0]
        )


def run_bounded_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    """Grow to a fixed catalog milestone from the frozen candidate checkpoint."""
    if args.target_total < BASELINE_SIZE:
        raise ValueError(f"target_total must be at least {BASELINE_SIZE:,}")
    target_new = args.target_total - BASELINE_SIZE
    with _connect(args.checkpoint) as connection:
        connection.execute(
            "UPDATE candidates SET accepted=NULL,decision_reason=NULL "
            "WHERE enrichment_status!='populated'"
        )
    selection_ready = threading.Event()
    scraping_done = threading.Event()
    apple_summary: Counter[str] = Counter()
    apple_failure: list[BaseException] = []

    def apple_worker() -> None:
        replacement_rounds = 0
        consecutive_provider_failures = 0
        while True:
            include_unselected = not selection_ready.is_set()
            if _ready_apple_count(
                args.checkpoint,
                include_unselected=include_unselected,
            ):
                try:
                    result = enrich_accepted_apple(
                        args.checkpoint,
                        args.cache,
                        limit=100,
                        include_unselected_above_threshold=include_unselected,
                    )
                except RuntimeError as error:
                    consecutive_provider_failures += 1
                    if consecutive_provider_failures >= 3:
                        raise
                    print(
                        "Apple checkpoint provider request failed; retrying batch "
                        f"({consecutive_provider_failures}/3): {type(error).__name__}",
                        flush=True,
                    )
                    time.sleep(max(1.0, float(args.poll_seconds)))
                    continue
                else:
                    consecutive_provider_failures = 0
            else:
                result = {"queued": 0, "complete": 0, "failed": 0}
            apple_summary.update(result)
            if result["queued"]:
                print(
                    f"Apple checkpoint: {result['complete']:,} complete, "
                    f"{result['failed']:,} failed from {result['queued']:,} queued",
                    flush=True,
                )
                continue
            if not selection_ready.is_set():
                if scraping_done.wait(args.poll_seconds):
                    continue
                continue
            verified, pending = _accepted_apple_counts(args.checkpoint)
            if verified >= target_new:
                break
            selection = select_target_candidates(args.checkpoint, target_new=target_new)
            replacement_rounds += 1
            verified, pending = _accepted_apple_counts(args.checkpoint)
            print(
                f"20k selection round {replacement_rounds}: {selection['accepted']:,} selected; "
                f"{verified:,} Apple verified; {pending:,} pending",
                flush=True,
            )
            if pending == 0 or replacement_rounds >= 10:
                break

    def guarded_apple_worker() -> None:
        try:
            apple_worker()
        except BaseException as error:
            apple_failure.append(error)

    apple_thread = threading.Thread(
        target=guarded_apple_worker,
        name="artist-expansion-apple",
        daemon=True,
    )
    apple_thread.start()
    spotify = scrape_ready_artists_parallel(args)
    scraping_done.set()
    baseline_exact_duplicates = reconcile_baseline_exact_identities(args.checkpoint)
    provider_ownership = reconcile_candidate_provider_owners(args.checkpoint)
    isrc_deduplication = reconcile_isrc_duplicates(args.checkpoint, args.cache)
    selection = select_target_candidates(args.checkpoint, target_new=target_new)
    selection_ready.set()
    apple_thread.join()
    if apple_failure:
        raise RuntimeError("Apple enrichment worker failed") from apple_failure[0]
    selection = select_target_candidates(args.checkpoint, target_new=target_new)
    verified, pending = _accepted_apple_counts(args.checkpoint)
    population = {"queued": 0, "inserted_or_reenabled": 0}
    trim: dict[str, Any] | None = None
    enabled = 0
    population_rounds = 0
    while verified >= target_new and population_rounds < 5:
        population_rounds += 1
        population_pass = append_verified_candidates(
            args.database,
            args.checkpoint,
            args.cache,
            target_total=args.target_total,
        )
        population["queued"] += int(population_pass["queued"])
        population["inserted_or_reenabled"] += int(population_pass["inserted_or_reenabled"])
        with sqlite3.connect(args.database) as catalog:
            enabled = int(
                catalog.execute("SELECT COUNT(*) FROM songs WHERE enabled=1").fetchone()[0]
            )
        if enabled >= args.target_total or population_pass["queued"] == 0:
            break
        selection = select_target_candidates(args.checkpoint, target_new=target_new)
        while True:
            apple_pass = enrich_accepted_apple(args.checkpoint, args.cache, limit=100)
            apple_summary.update(apple_pass)
            if apple_pass["queued"] == 0:
                break
        verified, pending = _accepted_apple_counts(args.checkpoint)
    if enabled == args.target_total:
        trim = trim_catalog_artist_caps(
            args.database,
            args.checkpoint,
            args.trim_report,
        )
    return {
        "target_total": args.target_total,
        "spotify": spotify,
        "selection": selection,
        "apple": dict(apple_summary),
        "apple_verified": verified,
        "apple_pending": pending,
        "population": population,
        "population_rounds": population_rounds,
        "baseline_exact_duplicates": baseline_exact_duplicates,
        "provider_ownership": provider_ownership,
        "isrc_deduplication": isrc_deduplication,
        "trim": trim,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.lb_priority_limit < 1 or args.browser_workers < 1 or args.batch_size < 1:
        raise ValueError("priority limit, browser workers, and batch size must be positive")
    if args.scrape_limit is not None and args.scrape_limit < 1:
        raise ValueError("scrape limit must be positive")
    if args.spotify_sessions < 1 or args.poll_seconds <= 0:
        raise ValueError("spotify sessions and poll seconds must be positive")
    baseline = ensure_baseline(args.database, args.checkpoint)
    checkpoint_saturated_artists(args.checkpoint)
    if args.command == "snapshot":
        return {"baseline": baseline}
    if args.command == "status":
        return {"baseline": baseline, "status": status(args.checkpoint)}
    if args.command == "pipeline":
        return {"baseline": baseline, **run_bounded_pipeline(args)}
    if args.command == "trim":
        trim = trim_catalog_artist_caps(
            args.database,
            args.checkpoint,
            args.trim_report,
        )
        return {"baseline": baseline, "trim": trim}
    if args.command == "select":
        baseline_exact_duplicates = reconcile_baseline_exact_identities(args.checkpoint)
        provider_ownership = reconcile_candidate_provider_owners(args.checkpoint)
        isrc_deduplication = reconcile_isrc_duplicates(args.checkpoint, args.cache)
        apple_failures_reset = (
            reset_retryable_apple_failures(args.checkpoint) if args.retry_failures else 0
        )
        selection = select_global_candidates(args.checkpoint)
        apple = enrich_accepted_apple(args.checkpoint, args.cache)
        audit = build_final_audit(args.database, args.checkpoint, args.final_report)
        return {
            "baseline": baseline,
            "selection": selection,
            "baseline_exact_duplicates": baseline_exact_duplicates,
            "provider_ownership": provider_ownership,
            "isrc_deduplication": isrc_deduplication,
            "apple_failures_reset": apple_failures_reset,
            "apple": apple,
            "audit": {
                "path": str(args.final_report),
                "accepted": len(audit["accepted_songs"]),
                "missing_spotify": len(audit["missing_spotify_matches"]),
                "missing_apple": len(audit["missing_apple_matches"]),
            },
        }
    if args.command == "populate":
        population = append_verified_candidates(args.database, args.checkpoint, args.cache)
        audit = build_final_audit(args.database, args.checkpoint, args.final_report)
        return {
            "baseline": baseline,
            "population": population,
            "audit": {
                "path": str(args.final_report),
                "ending_catalog_count": audit["ending_catalog_count"],
            },
            "status": status(args.checkpoint),
        }
    if args.command in {"pilot", "scrape"}:
        discovery = (
            discover_artist(args.checkpoint, args.cache, args.artist_mbid)
            if args.command == "pilot"
            else None
        )
        metadata = prepare_artist_candidate_metadata(
            args.checkpoint,
            args.cache,
            args.artist_mbid,
            lb_priority_limit=args.lb_priority_limit,
        )
        spotify = scrape_artist_spotify(
            args.checkpoint,
            args.artist_mbid,
            lb_priority_limit=args.lb_priority_limit,
            limit=args.scrape_limit,
            browser_workers=args.browser_workers,
            batch_size=args.batch_size,
            playwright_cli=args.playwright_cli,
            browser_executable=args.browser_executable,
            timeout_seconds=args.browser_timeout_seconds,
            retry_failures=args.retry_failures,
        )
        baseline_exact_duplicates = reconcile_baseline_exact_identities(args.checkpoint)
        provider_ownership = reconcile_candidate_provider_owners(args.checkpoint)
        isrc_deduplication = reconcile_isrc_duplicates(args.checkpoint, args.cache)
        apple_failures_reset = (
            reset_retryable_apple_failures(args.checkpoint, args.artist_mbid)
            if args.retry_failures
            else 0
        )
        report = build_pilot_report(args.checkpoint, args.report, args.artist_mbid)
        apple_totals = {"queued": 0, "complete": 0, "failed": 0}
        for _attempt in range(5):
            missing_before = {
                str(item["recording_mbid"]) for item in report.get("accepted_missing_apple", [])
            }
            if not missing_before:
                break
            apple_pass = enrich_accepted_apple(args.checkpoint, args.cache)
            for key in apple_totals:
                apple_totals[key] += int(apple_pass[key])
            report = build_pilot_report(args.checkpoint, args.report, args.artist_mbid)
            missing_after = {
                str(item["recording_mbid"]) for item in report.get("accepted_missing_apple", [])
            }
            if not missing_after or missing_after == missing_before:
                break
        return {
            "baseline": baseline,
            "discovery": discovery,
            "metadata": metadata,
            "spotify": spotify,
            "baseline_exact_duplicates": baseline_exact_duplicates,
            "provider_ownership": provider_ownership,
            "isrc_deduplication": isrc_deduplication,
            "apple_failures_reset": apple_failures_reset,
            "apple": apple_totals,
            "report": report,
        }

    if args.command == "scrape-all":
        with _connect(args.checkpoint) as connection:
            artist_mbids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT artist_mbid FROM artists WHERE discovery_status='complete' "
                    "AND eligibility_status='eligible' AND spotify_status!='complete' "
                    "ORDER BY baseline_count DESC,artist_mbid"
                )
            ]
        if args.limit_artists is not None:
            artist_mbids = artist_mbids[: args.limit_artists]
        completed = failed = 0
        for index, artist_mbid in enumerate(artist_mbids, start=1):
            try:
                prepare_artist_candidate_metadata(
                    args.checkpoint,
                    args.cache,
                    artist_mbid,
                    lb_priority_limit=args.lb_priority_limit,
                )
                spotify_result = scrape_artist_spotify(
                    args.checkpoint,
                    artist_mbid,
                    lb_priority_limit=args.lb_priority_limit,
                    limit=args.scrape_limit,
                    browser_workers=args.browser_workers,
                    batch_size=args.batch_size,
                    playwright_cli=args.playwright_cli,
                    browser_executable=args.browser_executable,
                    timeout_seconds=args.browser_timeout_seconds,
                    retry_failures=args.retry_failures,
                )
            except Exception as error:  # noqa: BLE001 - artist remains retryable.
                failed += 1
                with _connect(args.checkpoint) as connection:
                    connection.execute(
                        "UPDATE artists SET spotify_status='failure',failure_reason=? "
                        "WHERE artist_mbid=?",
                        (type(error).__name__, artist_mbid),
                    )
            else:
                completed += 1
                metrics = spotify_result.get("browser_metrics", {})
                if metrics:
                    print(
                        "  Spotify request paths: "
                        f"search direct {metrics.get('directSearches', 0):,}, "
                        f"search navigation {metrics.get('navigatedSearches', 0):,}; "
                        f"count direct {metrics.get('directHydrations', 0):,}, "
                        f"count navigation {metrics.get('navigatedHydrations', 0):,}",
                        flush=True,
                    )
            print(
                f"Artist Spotify {index:,}/{len(artist_mbids):,}: "
                f"{completed:,} complete, {failed:,} failed",
                flush=True,
            )
        return {
            "baseline": baseline,
            "completed": completed,
            "failed": failed,
            "status": status(args.checkpoint),
        }

    with _connect(args.checkpoint) as connection:
        discovery_statuses = ("pending", "failure") if args.retry_failures else ("pending",)
        placeholders = ",".join("?" for _ in discovery_statuses)
        artist_mbids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT artist_mbid FROM artists WHERE discovery_status IN ({placeholders}) "
                "AND eligibility_status='eligible' ORDER BY baseline_count DESC,artist_mbid",
                discovery_statuses,
            )
        ]
    if args.limit_artists is not None:
        artist_mbids = artist_mbids[: args.limit_artists]
    completed = failed = 0
    for index, artist_mbid in enumerate(artist_mbids, start=1):
        try:
            discover_artist(args.checkpoint, args.cache, artist_mbid)
        except Exception as error:  # noqa: BLE001 - failure is checkpointed and retryable.
            failed += 1
            failure_reason = f"{type(error).__name__}: {error}"[:500]
            with _connect(args.checkpoint) as connection:
                connection.execute(
                    "UPDATE artists SET discovery_status='failure',failure_reason=?,"
                    "completed_at=? WHERE artist_mbid=?",
                    (failure_reason, utc_now(), artist_mbid),
                )
        else:
            completed += 1
        print(
            f"Artist discovery {index:,}/{len(artist_mbids):,}: "
            f"{completed} complete, {failed} failed",
            flush=True,
        )
    return {
        "baseline": baseline,
        "completed": completed,
        "failed": failed,
        "status": status(args.checkpoint),
    }


def main() -> None:
    args = parse_args()
    try:
        result = run(args)
    except KeyboardInterrupt:
        print(
            "\nInterrupted safely; committed artist/candidate checkpoints remain resumable.",
            flush=True,
        )
        raise SystemExit(130) from None
    summary = {key: value for key, value in result.items() if key != "report"}
    if "report" in result:
        report = result["report"]
        summary["report_summary"] = {
            "path": str(args.report),
            "major_album_coverage": report.get("major_album_coverage"),
            "accepted": len(report.get("accepted_songs", [])),
            "below_threshold": len(report.get("rejected_below_200m", [])),
            "unresolved_spotify": len(report.get("unresolved_spotify", [])),
            "deferred_lower_priority": len(report.get("deferred_lower_priority_candidates", [])),
            "missing_apple": len(report.get("accepted_missing_apple", [])),
            "spotify_gate_ready": report.get("spotify_gate_ready"),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
