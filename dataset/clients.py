from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

USER_AGENT = "Songuess/0.2 (https://github.com/BrunoFarfan/songuess)"
LISTENBRAINZ_CACHE_VERSION = 2
APPLE_CACHE_VERSION = 2
DEFAULT_APPLE_NEGATIVE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_APPLE_ARTIST_TTL_SECONDS = DEFAULT_APPLE_NEGATIVE_TTL_SECONDS
DEFAULT_PREVIEW_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_PREVIEW_TRANSIENT_TTL_SECONDS = 5 * 60

_APPLE_LOCK = threading.Lock()
_APPLE_LAST_REQUEST = 0.0


def read_json(url: str, *, timeout: float = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (
            ConnectionError,
            TimeoutError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            if (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code not in {403, 429}
            ):
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Request failed after retries: {url}") from last_error


def cached_json(path: Path, url: str) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    payload = read_json(url)
    _write_json_atomic(path, payload)
    return payload


def listenbrainz_top_artists_cache_path(cache_dir: Path, artist_count: int) -> Path:
    return (
        cache_dir
        / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-top-artists-count-{artist_count}.json"
    )


def listenbrainz_radio_cache_path(
    cache_dir: Path, artist_mbid: str, recordings_per_artist: int
) -> Path:
    return (
        cache_dir
        / f"listenbrainz-radio-v{LISTENBRAINZ_CACHE_VERSION}-r{recordings_per_artist}"
        / f"{artist_mbid}.json"
    )


def fetch_listenbrainz_candidates(
    cache_dir: Path,
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    ranges = ("all_time", "year", "half_yearly", "quarter", "month", "week")
    for statistics_range in ranges:
        query = urllib.parse.urlencode({"range": statistics_range, "count": 1000, "offset": 0})
        url = f"https://api.listenbrainz.org/1/stats/sitewide/recordings?{query}"
        cache_path = (
            cache_dir / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-sitewide-"
            f"{statistics_range}-count-1000-offset-0.json"
        )
        payload = cached_json(cache_path, url)
        _merge_candidates(candidates, payload.get("payload", {}).get("recordings", []))

    if len(candidates) < count:
        overflow = fetch_listenbrainz_artist_overflow(
            cache_dir,
            set(candidates),
            count - len(candidates),
            artist_count=artist_count,
            recordings_per_artist=recordings_per_artist,
        )
        _merge_candidates(candidates, overflow)

    ranked = sorted(
        candidates.values(), key=lambda item: int(item.get("listen_count") or 0), reverse=True
    )
    return ranked[:count]


def fetch_listenbrainz_artist_overflow(
    cache_dir: Path,
    existing_mbids: set[str],
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
) -> list[dict[str, Any]]:
    artist_count = max(1, min(1000, artist_count))
    recordings_per_artist = max(1, recordings_per_artist)
    query = urllib.parse.urlencode({"range": "all_time", "count": artist_count, "offset": 0})
    artists_payload = cached_json(
        listenbrainz_top_artists_cache_path(cache_dir, artist_count),
        f"https://api.listenbrainz.org/1/stats/sitewide/artists?{query}",
    )
    artists = artists_payload.get("payload", {}).get("artists", [])
    overflow: dict[str, dict[str, Any]] = {}
    oversample_target = max(count + 1000, int(count * 1.2))

    for artist_index, artist in enumerate(artists, start=1):
        artist_mbid = artist.get("artist_mbid")
        if not artist_mbid:
            continue
        parameters = urllib.parse.urlencode(
            {
                "mode": "easy",
                "max_similar_artists": 1,
                "max_recordings_per_artist": recordings_per_artist,
                "pop_begin": 70,
                "pop_end": 100,
            }
        )
        payload = cached_json(
            listenbrainz_radio_cache_path(cache_dir, artist_mbid, recordings_per_artist),
            f"https://api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}?{parameters}",
        )
        for recording in payload.get(artist_mbid, []):
            recording_mbid = recording.get("recording_mbid")
            if not recording_mbid or recording_mbid in existing_mbids:
                continue
            listen_count = int(recording.get("total_listen_count") or 0)
            candidate = {
                "artist_mbids": [artist_mbid],
                "artist_name": recording.get("similar_artist_name")
                or artist.get("artist_name", ""),
                "listen_count": listen_count,
                "recording_mbid": recording_mbid,
                "release_mbid": "",
                "release_name": "",
                "track_name": recording.get("recording_name", ""),
            }
            previous = overflow.get(recording_mbid)
            if previous is None or listen_count > int(previous.get("listen_count") or 0):
                overflow[recording_mbid] = candidate
        if artist_index % 25 == 0:
            print(
                f"  ListenBrainz artists {artist_index:,}/{len(artists):,}; "
                f"unique overflow recordings {len(overflow):,}",
                flush=True,
            )
        if len(overflow) >= oversample_target:
            break

    return sorted(
        overflow.values(), key=lambda item: int(item.get("listen_count") or 0), reverse=True
    )


def fetch_musicbrainz_metadata(
    cache_dir: Path,
    candidates: list[dict[str, Any]],
    *,
    batch_size: int = 20,
    request_json: Callable[[str], dict[str, Any]] = read_json,
) -> dict[str, dict[str, Any]]:
    cache_path = cache_dir / "musicbrainz-recordings.sqlite3"
    connection = _musicbrainz_cache_connection(cache_path)
    try:
        _migrate_legacy_musicbrainz_cache(connection, cache_dir / "musicbrainz-recordings.json")
        mbids = list(dict.fromkeys(candidate["recording_mbid"] for candidate in candidates))
        metadata = _read_musicbrainz_cache(connection, mbids)
        missing = [mbid for mbid in mbids if mbid not in metadata]
        print(f"  MusicBrainz cached {len(metadata):,}; missing {len(missing):,}", flush=True)

        for index in range(0, len(missing), batch_size):
            batch = missing[index : index + batch_size]
            query = "rid:(" + " OR ".join(batch) + ")"
            parameters = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 100})
            payload = request_json(f"https://musicbrainz.org/ws/2/recording/?{parameters}")
            found = {recording["id"]: recording for recording in payload.get("recordings", [])}
            fetched_at = time.time()
            with connection:
                for recording_mbid in batch:
                    value = found.get(recording_mbid, {})
                    connection.execute(
                        "INSERT INTO recordings (mbid, payload_json, fetched_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(mbid) DO UPDATE SET payload_json=excluded.payload_json, "
                        "fetched_at=excluded.fetched_at",
                        (recording_mbid, json.dumps(value, ensure_ascii=False), fetched_at),
                    )
                    metadata[recording_mbid] = value
            completed = min(index + batch_size, len(missing))
            if completed % 200 == 0 or completed == len(missing):
                print(
                    f"  MusicBrainz fetched {completed:,}/{len(missing):,} missing records",
                    flush=True,
                )
            if completed < len(missing):
                time.sleep(1.05)
        return metadata
    finally:
        connection.close()


def search_apple_track(
    cache_dir: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    *,
    country: str,
    year_min: int,
    year_max: int,
    negative_ttl_seconds: float = DEFAULT_APPLE_NEGATIVE_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, Any] | None:
    current_time = time.time() if now is None else now
    recording_mbid = candidate["recording_mbid"]
    country_key = country.upper()
    cache_path = cache_dir / "apple-v2" / country_key / f"{recording_mbid}.json"
    hit, cached_value, _expired_negative = _read_apple_match_cache(
        cache_path, negative_ttl_seconds, current_time
    )
    if hit:
        return cached_value

    legacy_path = cache_dir / "apple" / f"{recording_mbid}.json"
    if not cache_path.exists() and legacy_path.exists():
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        if legacy.get("trackId"):
            _write_apple_match_cache(cache_path, legacy, current_time)
            return legacy

    title = metadata.get("title") or candidate.get("track_name", "")
    artist = _musicbrainz_artist(metadata) or candidate.get("artist_name", "")
    artist_cache_key = hashlib.sha256(f"{country_key}:{artist.casefold()}".encode()).hexdigest()
    artist_cache_path = cache_dir / "apple-artists-v2" / country_key / f"{artist_cache_key}.json"
    legacy_artist_cache_key = hashlib.sha256(f"{country}:{artist.casefold()}".encode()).hexdigest()
    legacy_artist_cache_path = cache_dir / "apple-artists" / f"{legacy_artist_cache_key}.json"
    parameters = urllib.parse.urlencode(
        {
            "term": artist,
            "media": "music",
            "entity": "song",
            "attribute": "artistTerm",
            "country": country_key,
            "limit": 200,
        }
    )
    search_url = "https://itunes.apple.com/WebObjects/MZStoreServices.woa/wa/wsSearch"
    payload = _read_apple_artist_cache(
        artist_cache_path,
        legacy_artist_cache_path,
        now=current_time,
    )
    if payload is None:
        _throttle_apple()
        payload = read_json(f"{search_url}?{parameters}")
        _write_json_atomic(
            artist_cache_path,
            {
                "version": APPLE_CACHE_VERSION,
                "checked_at": current_time,
                "payload": payload,
            },
        )

    best: tuple[float, dict[str, Any]] | None = None
    canonical_year = _release_year(metadata.get("first-release-date"))
    for result in payload.get("results", []):
        preview_url = result.get("previewUrl")
        apple_year = _release_year(result.get("releaseDate"))
        release_year = canonical_year or apple_year
        if not preview_url or not release_year or not year_min <= release_year <= year_max:
            continue
        title_score = _similarity(title, result.get("trackName", ""))
        artist_score = _similarity(artist, result.get("artistName", ""))
        if title_score < 0.78 or artist_score < 0.62:
            continue
        score = title_score * 0.68 + artist_score * 0.32
        if best is None or score > best[0]:
            matched = dict(result)
            matched["canonicalReleaseYear"] = release_year
            best = (score, matched)

    selected = best[1] if best else None
    _write_apple_match_cache(cache_path, selected, current_time)
    return selected


def validate_previews(
    cache_dir: Path,
    urls: list[str],
    *,
    max_workers: int = 8,
    valid_ttl_seconds: float = DEFAULT_PREVIEW_TTL_SECONDS,
    transient_ttl_seconds: float = DEFAULT_PREVIEW_TRANSIENT_TTL_SECONDS,
    now: float | None = None,
    checker: Callable[[str], tuple[str, str | None]] | None = None,
) -> dict[str, str]:
    current_time = time.time() if now is None else now
    checker = checker or _head_preview_status
    unique_urls = list(dict.fromkeys(urls))
    database_path = cache_dir / "preview-validation.sqlite3"
    connection = _preview_cache_connection(database_path)
    statuses: dict[str, str] = {}
    pending: list[str] = []
    try:
        for url in unique_urls:
            cached = connection.execute(
                "SELECT status, checked_at FROM preview_validation WHERE url = ?", (url,)
            ).fetchone()
            if cached is None:
                pending.append(url)
                continue
            status, checked_at = str(cached[0]), float(cached[1])
            ttl = transient_ttl_seconds if status == "transient" else valid_ttl_seconds
            if current_time - checked_at < ttl:
                statuses[url] = status
            else:
                pending.append(url)

        checked: dict[str, tuple[str, str | None]] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            futures = {executor.submit(checker, url): url for url in pending}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    checked[url] = future.result()
                except Exception as error:  # A worker failure is still retryable.
                    checked[url] = ("transient", str(error))

        with connection:
            for url, (status, error) in checked.items():
                connection.execute(
                    "INSERT INTO preview_validation (url, status, checked_at, error) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                    "status=excluded.status, checked_at=excluded.checked_at, error=excluded.error",
                    (url, status, current_time, error),
                )
                statuses[url] = status
        return statuses
    finally:
        connection.close()


def preview_is_available(url: str) -> bool:
    return _head_preview_status(url)[0] == "valid"


def canonical_genres(apple_track: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    values = [apple_track.get("primaryGenreName", "")]
    values.extend(genre.get("name", "") for genre in metadata.get("genres", []))
    values.extend(tag.get("name", "") for tag in metadata.get("tags", []))
    normalized = " | ".join(value.casefold() for value in values)
    keyword_groups = {
        "alternative": ("alternative", "indie"),
        "classical": ("classical",),
        "country": ("country",),
        "dance": ("dance", "house", "techno", "edm"),
        "electronic": ("electronic", "electronica", "synth"),
        "folk": ("folk", "singer-songwriter"),
        "hip-hop": ("hip hop", "hip-hop", "rap"),
        "jazz": ("jazz",),
        "k-pop": ("k-pop", "kpop"),
        "latin": ("latin", "reggaeton", "salsa", "bachata"),
        "metal": ("metal",),
        "pop": ("pop",),
        "punk": ("punk",),
        "r&b": ("r&b", "rnb", "rhythm and blues", "soul"),
        "reggae": ("reggae", "ska"),
        "rock": ("rock",),
        "soundtrack": ("soundtrack", "original score"),
    }
    genres = {
        genre
        for genre, keywords in keyword_groups.items()
        if any(keyword in normalized for keyword in keywords)
    }
    return sorted(genres or {"other"})


def _merge_candidates(
    destination: dict[str, dict[str, Any]], recordings: list[dict[str, Any]]
) -> None:
    for recording in recordings:
        recording_mbid = recording.get("recording_mbid")
        if not recording_mbid:
            continue
        listen_count = int(
            recording.get("listen_count") or recording.get("total_listen_count") or 0
        )
        normalized = dict(recording)
        normalized["listen_count"] = listen_count
        previous = destination.get(recording_mbid)
        if previous is None or listen_count > int(previous.get("listen_count") or 0):
            destination[recording_mbid] = normalized


def _musicbrainz_cache_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS recordings ("
        "mbid TEXT PRIMARY KEY, payload_json TEXT NOT NULL, fetched_at REAL NOT NULL)"
    )
    return connection


def _migrate_legacy_musicbrainz_cache(connection: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        return
    existing_count = connection.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
    if existing_count:
        return
    legacy = json.loads(path.read_text(encoding="utf-8"))
    fetched_at = path.stat().st_mtime
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO recordings (mbid, payload_json, fetched_at) VALUES (?, ?, ?)",
            [
                (mbid, json.dumps(payload, ensure_ascii=False), fetched_at)
                for mbid, payload in legacy.items()
            ],
        )
    print(f"  Migrated {len(legacy):,} legacy MusicBrainz cache rows to SQLite", flush=True)


def _read_musicbrainz_cache(
    connection: sqlite3.Connection, mbids: list[str]
) -> dict[str, dict[str, Any]]:
    if not mbids:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index in range(0, len(mbids), 800):
        batch = mbids[index : index + 800]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"SELECT mbid, payload_json FROM recordings WHERE mbid IN ({placeholders})", batch
        ).fetchall()
        result.update({str(row[0]): json.loads(row[1]) for row in rows})
    return result


def _read_apple_match_cache(
    path: Path, negative_ttl_seconds: float, now: float
) -> tuple[bool, dict[str, Any] | None, bool]:
    if not path.exists():
        return False, None, False
    cached = json.loads(path.read_text(encoding="utf-8"))
    if cached.get("version") == APPLE_CACHE_VERSION:
        if cached.get("status") == "matched":
            return True, cached.get("track"), False
        checked_at = float(cached.get("checked_at") or 0)
        if now - checked_at < negative_ttl_seconds:
            return True, None, False
        return False, None, True
    if cached.get("trackId"):
        return True, cached, False
    return False, None, True


def _write_apple_match_cache(path: Path, track: dict[str, Any] | None, checked_at: float) -> None:
    _write_json_atomic(
        path,
        {
            "version": APPLE_CACHE_VERSION,
            "status": "matched" if track else "negative",
            "checked_at": checked_at,
            "track": track,
        },
    )


def _read_apple_artist_cache(
    path: Path,
    legacy_path: Path,
    *,
    now: float,
) -> dict[str, Any] | None:
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked_at = float(cached.get("checked_at") or path.stat().st_mtime)
        if now - checked_at < DEFAULT_APPLE_ARTIST_TTL_SECONDS:
            return cached.get("payload", cached)
    if legacy_path.exists():
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        if now - legacy_path.stat().st_mtime >= DEFAULT_APPLE_ARTIST_TTL_SECONDS:
            return None
        _write_json_atomic(
            path,
            {
                "version": APPLE_CACHE_VERSION,
                "checked_at": legacy_path.stat().st_mtime,
                "payload": payload,
            },
        )
        return payload
    return None


def _preview_cache_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS preview_validation ("
        "url TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN "
        "('valid', 'invalid', 'transient')), checked_at REAL NOT NULL, error TEXT)"
    )
    return connection


def _head_preview_status(url: str) -> tuple[str, str | None]:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            valid = response.status in {200, 206} and (
                content_type.startswith("audio/") or "octet-stream" in content_type
            )
            return ("valid", None) if valid else ("invalid", f"content-type={content_type}")
    except urllib.error.HTTPError as error:
        if error.code == 429 or error.code >= 500:
            return "transient", f"HTTP {error.code}"
        return "invalid", f"HTTP {error.code}"
    except (ConnectionError, TimeoutError, urllib.error.URLError) as error:
        return "transient", str(error)


def _musicbrainz_artist(metadata: dict[str, Any]) -> str:
    return "".join(
        part.get("name", "") + part.get("joinphrase", "")
        for part in metadata.get("artist-credit", [])
    )


def _throttle_apple() -> None:
    global _APPLE_LAST_REQUEST
    with _APPLE_LOCK:
        wait = 3.1 - (time.monotonic() - _APPLE_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _APPLE_LAST_REQUEST = time.monotonic()


def _release_year(value: str | None) -> int | None:
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_name(left), _normalize_name(right)).ratio()


def _normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    characters = "".join(character if character.isalnum() else " " for character in value)
    return " ".join(characters.split())


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(path)
