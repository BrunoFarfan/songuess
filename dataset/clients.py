from __future__ import annotations

import hashlib
import html
import http.client
import json
import os
import re
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

from dataset.metrics import record as record_metric

USER_AGENT = "Songuess/0.2 (https://github.com/BrunoFarfan/songuess)"
LISTENBRAINZ_CACHE_VERSION = 3
LISTENBRAINZ_RADIO_CACHE_VERSION = 4
APPLE_CACHE_VERSION = 2
APPLE_SQLITE_CACHE_VERSION = 1
DEFAULT_APPLE_NEGATIVE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_APPLE_ARTIST_TTL_SECONDS = DEFAULT_APPLE_NEGATIVE_TTL_SECONDS
DEFAULT_PREVIEW_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_PREVIEW_TRANSIENT_TTL_SECONDS = 5 * 60

_APPLE_LOCK = threading.Lock()
_APPLE_LAST_REQUEST = 0.0
SPOTIFY_TRACK_URL_PATTERN = re.compile(
    r"^https://open\.spotify\.com/track/[A-Za-z0-9]{22}(?:\?.*)?$"
)
APPLE_EXPLICITNESS_PRIORITY = {"cleaned": 0, "notExplicit": 1, "explicit": 2}


def _request_headers(url: str, *, json_content: bool = False) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if json_content:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
    if token and urllib.parse.urlparse(url).hostname == "api.listenbrainz.org":
        headers["Authorization"] = f"Token {token}"
    return headers


def read_json(url: str, *, timeout: float = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_request_headers(url))
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                record_metric(_provider_name(url), "requests")
                record_metric(_provider_name(url), "downloaded_bytes", len(body))
                return json.loads(body)
        except (
            ConnectionError,
            TimeoutError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            record_metric(_provider_name(url), "retries")
            if (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code not in {403, 429}
            ):
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Request failed after retries: {url}") from last_error


def read_text(url: str, *, timeout: float = 30) -> str:
    request = urllib.request.Request(url, headers=_request_headers(url))
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (
            ConnectionError,
            TimeoutError,
            http.client.IncompleteRead,
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


def post_json(url: str, payload: dict[str, Any], *, timeout: float = 30) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_request_headers(url, json_content=True),
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                record_metric(_provider_name(url), "requests")
                record_metric(_provider_name(url), "downloaded_bytes", len(body))
                return json.loads(body)
        except (
            ConnectionError,
            TimeoutError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            record_metric(_provider_name(url), "retries")
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
        record_metric(_provider_name(url), "cache_hits")
        return json.loads(path.read_text(encoding="utf-8"))
    payload = read_json(url)
    _write_json_atomic(path, payload)
    return payload


def _provider_name(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "unknown").casefold()
    if "listenbrainz" in host:
        return "listenbrainz"
    if "musicbrainz" in host:
        return "musicbrainz"
    if "apple.com" in host or "itunes.apple.com" in host:
        return "apple"
    if "spotify" in host:
        return "spotify"
    if "reccobeats" in host:
        return "reccobeats"
    return host


def listenbrainz_top_artists_cache_path(cache_dir: Path, artist_count: int) -> Path:
    return (
        cache_dir
        / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-top-artists-count-{artist_count}.json"
    )


def listenbrainz_top_artists_page_cache_path(
    cache_dir: Path, *, statistics_range: str, offset: int, count: int
) -> Path:
    return (
        cache_dir / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-top-artists-{statistics_range}-"
        f"count-{count}-offset-{offset}.json"
    )


def fetch_listenbrainz_popular_artists(cache_dir: Path, artist_count: int) -> list[dict[str, Any]]:
    artists: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranges = ("all_time", "year", "half_yearly", "quarter", "month", "week")
    for statistics_range in ranges:
        page_count = min(1000, artist_count)
        query = urllib.parse.urlencode(
            {"range": statistics_range, "count": page_count, "offset": 0}
        )
        page_path = listenbrainz_top_artists_page_cache_path(
            cache_dir,
            statistics_range=statistics_range,
            offset=0,
            count=page_count,
        )
        legacy_path = listenbrainz_top_artists_cache_path(cache_dir, page_count)
        active_path = (
            legacy_path if statistics_range == "all_time" and legacy_path.exists() else page_path
        )
        payload = cached_json(
            active_path,
            f"https://api.listenbrainz.org/1/stats/sitewide/artists?{query}",
        )
        page = payload.get("payload", {}).get("artists", [])
        for artist in page:
            artist_mbid = str(artist.get("artist_mbid") or "")
            if not artist_mbid or artist_mbid in seen:
                continue
            seen.add(artist_mbid)
            artists.append(artist)
            if len(artists) >= artist_count:
                return artists
    return artists


def listenbrainz_radio_cache_path(
    cache_dir: Path,
    artist_mbid: str,
    recordings_per_artist: int,
    *,
    similar_artists: int = 0,
) -> Path:
    return (
        cache_dir / f"listenbrainz-radio-v{LISTENBRAINZ_RADIO_CACHE_VERSION}-"
        f"s{similar_artists}-r{recordings_per_artist}" / f"{artist_mbid}.json"
    )


def listenbrainz_artist_top_recordings_cache_path(cache_dir: Path, artist_mbid: str) -> Path:
    return (
        cache_dir
        / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-artist-top-recordings"
        / f"{artist_mbid}.json"
    )


def fetch_listenbrainz_candidates(
    cache_dir: Path,
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
    include_radio_diversity: bool = False,
    discovery_workers: int = 12,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    ranges = ("all_time", "year", "half_yearly", "quarter", "month", "week")
    offset = 0
    while len(candidates) < count:
        added_this_page = 0
        for statistics_range in ranges:
            query = urllib.parse.urlencode(
                {"range": statistics_range, "count": 1000, "offset": offset}
            )
            url = f"https://api.listenbrainz.org/1/stats/sitewide/recordings?{query}"
            cache_path = (
                cache_dir / f"listenbrainz-v{LISTENBRAINZ_CACHE_VERSION}-sitewide-"
                f"{statistics_range}-count-1000-offset-{offset}.json"
            )
            payload = cached_json(cache_path, url)
            before = len(candidates)
            _merge_candidate_identities(
                candidates,
                payload.get("payload", {}).get("recordings", []),
                source=f"sitewide_{statistics_range}",
            )
            added_this_page += len(candidates) - before
            if len(candidates) >= count:
                break
        if added_this_page == 0:
            break
        offset += 1000

    token_configured = bool(os.environ.get("LISTENBRAINZ_TOKEN", "").strip())
    if len(candidates) < count and token_configured:
        overflow = fetch_listenbrainz_artist_top_recordings(
            cache_dir,
            set(candidates),
            count - len(candidates),
            artist_count=artist_count,
            recordings_per_artist=recordings_per_artist,
        )
        _merge_candidate_identities(candidates, overflow, source="artist_top_recordings")

    if len(candidates) < count:
        overflow = fetch_listenbrainz_artist_radio_recordings(
            cache_dir,
            set(candidates),
            count - len(candidates),
            artist_count=artist_count,
            recordings_per_artist=recordings_per_artist,
            workers=discovery_workers,
        )
        _merge_candidate_identities(candidates, overflow, source="artist_radio_recordings")

    if include_radio_diversity and len(candidates) < count:
        diversity = fetch_listenbrainz_radio_diversity(
            cache_dir,
            set(candidates),
            count - len(candidates),
            artist_count=artist_count,
            recordings_per_artist=recordings_per_artist,
        )
        _merge_candidate_identities(candidates, diversity, source="lb_radio_diversity")

    return list(candidates.values())[:count]


def fetch_listenbrainz_artist_radio_recordings(
    cache_dir: Path,
    existing_mbids: set[str],
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
    workers: int = 12,
) -> list[dict[str, Any]]:
    """Expand public popular artists without requiring a ListenBrainz token.

    ``max_similar_artists=0`` keeps each response on the requested artist. Listen
    counts only order discovery candidates and are stripped before returning.
    """
    artist_count = max(1, min(10000, artist_count))
    recordings_per_artist = max(1, recordings_per_artist)
    artists = fetch_listenbrainz_popular_artists(cache_dir, artist_count)
    overflow: dict[str, dict[str, Any]] = {}

    def fetch_artist(
        artist_index: int, artist: dict[str, Any]
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        artist_mbid = str(artist.get("artist_mbid") or "")
        if not artist_mbid:
            return artist_index, artist, []
        parameters = urllib.parse.urlencode(
            {
                "mode": "easy",
                "max_similar_artists": 0,
                "max_recordings_per_artist": recordings_per_artist,
                "pop_begin": 70,
                "pop_end": 100,
            }
        )
        payload = cached_json(
            listenbrainz_radio_cache_path(
                cache_dir,
                artist_mbid,
                recordings_per_artist,
                similar_artists=0,
            ),
            f"https://api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}?{parameters}",
        )
        recordings = payload.get(artist_mbid, [])
        if not isinstance(recordings, list):
            return artist_index, artist, []
        ranked_recordings = sorted(
            (recording for recording in recordings if isinstance(recording, dict)),
            key=lambda recording: -int(recording.get("total_listen_count") or 0),
        )
        return artist_index, artist, ranked_recordings[:recordings_per_artist]

    fetched: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    worker_count = max(1, min(workers, len(artists))) if artists else 1
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(fetch_artist, artist_index, artist): artist_index
            for artist_index, artist in enumerate(artists, start=1)
        }
        completed = 0
        for future in as_completed(futures):
            artist_index = futures[future]
            try:
                _index, artist, recordings = future.result()
            except Exception as error:  # noqa: BLE001 - failed pages remain retryable.
                print(
                    f"  ListenBrainz public artist radio failed for seed {artist_index:,}: {error}",
                    flush=True,
                )
            else:
                fetched[artist_index] = (artist, recordings)
            completed += 1
            if completed % 25 == 0 or completed == len(artists):
                print(
                    f"  ListenBrainz public artist radio fetched {completed:,}/{len(artists):,}",
                    flush=True,
                )

    for artist_index in sorted(fetched):
        artist, ranked_recordings = fetched[artist_index]
        artist_mbid = str(artist.get("artist_mbid") or "")
        for recording in ranked_recordings[:recordings_per_artist]:
            recording_mbid = str(recording.get("recording_mbid") or "")
            if not recording_mbid or recording_mbid in existing_mbids:
                continue
            candidate_artist_mbid = str(recording.get("similar_artist_mbid") or artist_mbid)
            candidate = {
                "artist_mbids": [candidate_artist_mbid],
                "artist_name": recording.get("similar_artist_name")
                or artist.get("artist_name", ""),
                "recording_mbid": recording_mbid,
                "release_mbid": "",
                "release_name": "",
                "track_name": recording.get("recording_name", ""),
            }
            _merge_candidate_identities(overflow, [candidate], source="artist_radio_recordings")
            if len(overflow) >= count:
                break
        if len(overflow) >= count:
            break

    return list(overflow.values())


def fetch_listenbrainz_artist_top_recordings(
    cache_dir: Path,
    existing_mbids: set[str],
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
) -> list[dict[str, Any]]:
    artist_count = max(1, min(10000, artist_count))
    recordings_per_artist = max(1, recordings_per_artist)
    artists = fetch_listenbrainz_popular_artists(cache_dir, artist_count)
    overflow: dict[str, dict[str, Any]] = {}

    for artist_index, artist in enumerate(artists, start=1):
        artist_mbid = artist.get("artist_mbid")
        if not artist_mbid:
            continue
        try:
            payload = cached_json(
                listenbrainz_artist_top_recordings_cache_path(cache_dir, artist_mbid),
                f"https://api.listenbrainz.org/1/popularity/"
                f"top-recordings-for-artist/{artist_mbid}",
            )
        except urllib.error.HTTPError as error:
            if error.code == 401 and not os.environ.get("LISTENBRAINZ_TOKEN", "").strip():
                print(
                    "  ListenBrainz artist expansion requires a token; "
                    "continuing with cached/optional diversity sources.",
                    flush=True,
                )
                break
            raise
        if not isinstance(payload, list):
            continue
        for recording in payload[:recordings_per_artist]:
            recording_mbid = recording.get("recording_mbid")
            if not recording_mbid or recording_mbid in existing_mbids:
                continue
            candidate = {
                "artist_mbids": recording.get("artist_mbids") or [artist_mbid],
                "artist_name": recording.get("artist_name") or artist.get("artist_name", ""),
                "recording_mbid": recording_mbid,
                "release_mbid": recording.get("release_mbid", ""),
                "release_name": recording.get("release_name", ""),
                "track_name": recording.get("recording_name", ""),
            }
            _merge_candidate_identities(overflow, [candidate], source="artist_top_recordings")
            if len(overflow) >= count:
                break
        if artist_index % 25 == 0:
            print(
                f"  ListenBrainz artist charts {artist_index:,}/{len(artists):,}; "
                f"unique recordings {len(overflow):,}",
                flush=True,
            )
        if len(overflow) >= count:
            break

    return list(overflow.values())


def fetch_listenbrainz_radio_diversity(
    cache_dir: Path,
    existing_mbids: set[str],
    count: int,
    *,
    artist_count: int = 1000,
    recordings_per_artist: int = 60,
) -> list[dict[str, Any]]:
    """Optionally add identities from LB Radio without using its counts for scoring."""
    artist_count = max(1, min(10000, artist_count))
    recordings_per_artist = max(1, recordings_per_artist)
    artists = fetch_listenbrainz_popular_artists(cache_dir, artist_count)
    diversity: dict[str, dict[str, Any]] = {}
    for artist in artists:
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
            listenbrainz_radio_cache_path(
                cache_dir,
                artist_mbid,
                recordings_per_artist,
                similar_artists=1,
            ),
            f"https://api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}?{parameters}",
        )
        for response_artist_mbid, recordings in payload.items():
            if not isinstance(recordings, list):
                continue
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                recording_mbid = recording.get("recording_mbid")
                if not recording_mbid or recording_mbid in existing_mbids:
                    continue
                candidate_artist_mbid = str(
                    recording.get("similar_artist_mbid") or response_artist_mbid
                )
                candidate = {
                    "artist_mbids": [candidate_artist_mbid],
                    "artist_name": recording.get("similar_artist_name")
                    or artist.get("artist_name", ""),
                    "recording_mbid": recording_mbid,
                    "release_mbid": "",
                    "release_name": "",
                    "track_name": recording.get("recording_name", ""),
                }
                _merge_candidate_identities(diversity, [candidate], source="lb_radio_diversity")
                if len(diversity) >= count:
                    return list(diversity.values())
    return list(diversity.values())


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


def fetch_musicbrainz_spotify_urls(
    recording_mbids: list[str],
    *,
    delay_seconds: float = 1.05,
    request_json: Callable[[str], dict[str, Any]] = read_json,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, str | None]:
    """Resolve exact Spotify track relationships for canonical recordings.

    MusicBrainz URL relationships are structured recording identities, so this deliberately
    does not fall back to title search or fuzzy matching. When several Spotify URLs identify
    the same recording, the deterministic first URL is used for the reveal link.
    """
    results: dict[str, str | None] = {}
    unique_mbids = list(dict.fromkeys(recording_mbids))
    for index, recording_mbid in enumerate(unique_mbids):
        parameters = urllib.parse.urlencode({"inc": "url-rels", "fmt": "json"})
        payload = request_json(
            f"https://musicbrainz.org/ws/2/recording/{recording_mbid}?{parameters}"
        )
        urls: set[str] = set()
        for relation in payload.get("relations", []):
            if not isinstance(relation, dict):
                continue
            resource = (relation.get("url") or {}).get("resource")
            if isinstance(resource, str) and SPOTIFY_TRACK_URL_PATTERN.fullmatch(resource):
                urls.add(resource.split("?", 1)[0])
        ordered_urls = sorted(urls)
        results[recording_mbid] = ordered_urls[0] if ordered_urls else None
        if index + 1 < len(unique_mbids):
            sleeper(delay_seconds)
    return results


def fetch_musicbrainz_artist_countries(
    cache_dir: Path,
    recordings: dict[str, dict[str, Any]],
    *,
    batch_size: int = 20,
    request_json: Callable[[str], dict[str, Any]] = read_json,
) -> dict[str, list[str]]:
    """Return explicit MusicBrainz country codes for every credited artist per recording."""
    cache_path = cache_dir / "musicbrainz-recordings.sqlite3"
    connection = _musicbrainz_cache_connection(cache_path)
    try:
        artist_ids = list(
            dict.fromkeys(
                artist_id
                for recording in recordings.values()
                for artist_id in _credited_artist_ids(recording)
            )
        )
        artists = _read_musicbrainz_artists_cache(connection, artist_ids)
        missing = [artist_id for artist_id in artist_ids if artist_id not in artists]
        print(
            f"  MusicBrainz artists cached {len(artists):,}; missing {len(missing):,}",
            flush=True,
        )

        for index in range(0, len(missing), batch_size):
            batch = missing[index : index + batch_size]
            query = "arid:(" + " OR ".join(batch) + ")"
            parameters = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 100})
            payload = request_json(f"https://musicbrainz.org/ws/2/artist/?{parameters}")
            found = {artist["id"]: artist for artist in payload.get("artists", [])}
            fetched_at = time.time()
            with connection:
                for artist_id in batch:
                    value = found.get(artist_id, {})
                    connection.execute(
                        "INSERT INTO artists (mbid, payload_json, fetched_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(mbid) DO UPDATE SET payload_json=excluded.payload_json, "
                        "fetched_at=excluded.fetched_at",
                        (artist_id, json.dumps(value, ensure_ascii=False), fetched_at),
                    )
                    artists[artist_id] = value
            completed = min(index + batch_size, len(missing))
            if completed % 200 == 0 or completed == len(missing):
                print(
                    f"  MusicBrainz fetched {completed:,}/{len(missing):,} missing artists",
                    flush=True,
                )
            if completed < len(missing):
                time.sleep(1.05)

        return {
            recording_mbid: sorted(
                {
                    country
                    for artist_id in _credited_artist_ids(recording)
                    if (country := _musicbrainz_country(artists.get(artist_id, {})))
                }
            )
            for recording_mbid, recording in recordings.items()
        }
    finally:
        connection.close()


def apple_explicitness(track: dict[str, Any]) -> str:
    value = str(track.get("trackExplicitness") or "").strip()
    return value if value in APPLE_EXPLICITNESS_PRIORITY else "unknown"


def stored_apple_explicitness(track: dict[str, Any]) -> str:
    return {"notExplicit": "not_explicit"}.get(apple_explicitness(track), apple_explicitness(track))


def _select_apple_track(
    results: list[dict[str, Any]],
    *,
    title: str,
    artist: str,
    canonical_year: int | None,
    year_min: int,
    year_max: int,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, float, float, dict[str, Any]]] = []
    for result in results:
        preview_url = result.get("previewUrl")
        apple_year = _release_year(result.get("releaseDate"))
        release_year = canonical_year or apple_year
        if not preview_url or not release_year or not year_min <= release_year <= year_max:
            continue
        title_score = _similarity(title, result.get("trackName", ""))
        artist_score = _similarity(artist, result.get("artistName", ""))
        if title_score < 0.78 or artist_score < 0.62:
            continue
        identity_score = title_score * 0.68 + artist_score * 0.32
        matched = dict(result)
        matched["canonicalReleaseYear"] = release_year
        candidates.append((identity_score, title_score, artist_score, matched))

    if not candidates:
        return None
    best_identity = max(item[0] for item in candidates)
    equivalent = [item for item in candidates if item[0] >= best_identity - 0.03]
    return max(
        equivalent,
        key=lambda item: (
            APPLE_EXPLICITNESS_PRIORITY.get(apple_explicitness(item[3]), 1),
            item[0],
            item[1],
            item[2],
            -int(item[3].get("trackId") or 0),
        ),
    )[3]


def find_explicit_apple_equivalent(
    clean_track: dict[str, Any],
    *,
    country: str,
    request_text: Callable[[str], str] = read_text,
    request_json: Callable[[str], dict[str, Any]] = read_json,
) -> dict[str, Any] | None:
    """Resolve Apple's explicitly labelled alternate for an otherwise exact clean track."""
    if apple_explicitness(clean_track) != "cleaned":
        return None
    track_url = clean_track.get("trackViewUrl")
    if not isinstance(track_url, str) or "music.apple.com/" not in track_url:
        return None

    _throttle_apple()
    page = html.unescape(request_text(track_url))
    explicit_album_ids = _explicit_apple_album_ids(page)
    expected_title = str(clean_track.get("trackName") or "")
    expected_artist = str(clean_track.get("artistName") or "")
    expected_duration = clean_track.get("trackTimeMillis")
    matches: list[dict[str, Any]] = []
    for album_id in explicit_album_ids[:5]:
        query = urllib.parse.urlencode(
            {"id": album_id, "entity": "song", "country": country.upper()}
        )
        _throttle_apple()
        payload = request_json(f"https://itunes.apple.com/lookup?{query}")
        for result in payload.get("results", []):
            if apple_explicitness(result) != "explicit" or not result.get("previewUrl"):
                continue
            if _similarity(expected_title, result.get("trackName", "")) < 0.97:
                continue
            if _similarity(expected_artist, result.get("artistName", "")) < 0.9:
                continue
            candidate_duration = result.get("trackTimeMillis")
            if (
                isinstance(expected_duration, (int, float))
                and isinstance(candidate_duration, (int, float))
                and abs(int(expected_duration) - int(candidate_duration)) > 2500
            ):
                continue
            matches.append(dict(result))
    unique_matches = {str(match.get("trackId")): match for match in matches}
    if len(unique_matches) != 1:
        return None
    explicit = next(iter(unique_matches.values()))
    explicit["canonicalReleaseYear"] = _release_year(
        clean_track.get("releaseDate")
    ) or _release_year(explicit.get("releaseDate"))
    return explicit


def _explicit_apple_album_ids(page: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(
                r'aria-label="Explicit,[^"]*"[^>]+href="https://music\.apple\.com/'
                r'[^/]+/album/[^"?]+/(\d+)',
                html.unescape(page),
            )
        )
    )


def find_explicit_apple_equivalents(
    clean_tracks: dict[int, dict[str, Any]],
    *,
    country: str,
    max_workers: int = 8,
    request_text: Callable[[str], str] = read_text,
    request_json: Callable[[str], dict[str, Any]] = read_json,
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """Bulk-resolve explicit alternates using concurrent pages and batched lookups."""

    def fetch_page(item: tuple[int, dict[str, Any]]) -> tuple[int, list[str], bool]:
        song_id, track = item
        track_url = track.get("trackViewUrl")
        if not isinstance(track_url, str) or "music.apple.com/" not in track_url:
            return song_id, [], True
        try:
            return song_id, _explicit_apple_album_ids(request_text(track_url)), True
        except (OSError, RuntimeError, ValueError):
            return song_id, [], False

    albums_by_song: dict[int, list[str]] = {}
    checked: set[int] = set()
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(clean_tracks)))) as executor:
        futures = [executor.submit(fetch_page, item) for item in clean_tracks.items()]
        for future in as_completed(futures):
            song_id, album_ids, completed = future.result()
            if completed:
                checked.add(song_id)
                albums_by_song[song_id] = album_ids

    unique_album_ids = list(
        dict.fromkeys(album_id for ids in albums_by_song.values() for album_id in ids)
    )
    tracks_by_album: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(unique_album_ids), 100):
        batch = unique_album_ids[start : start + 100]
        query = urllib.parse.urlencode(
            {"id": ",".join(batch), "entity": "song", "country": country.upper()}
        )
        _throttle_apple()
        payload = request_json(f"https://itunes.apple.com/lookup?{query}")
        for result in payload.get("results", []):
            collection_id = result.get("collectionId")
            if collection_id is not None and result.get("trackId") is not None:
                tracks_by_album.setdefault(str(collection_id), []).append(dict(result))

    resolved: dict[int, dict[str, Any]] = {}
    for song_id, clean_track in clean_tracks.items():
        expected_title = str(clean_track.get("trackName") or "")
        expected_artist = str(clean_track.get("artistName") or "")
        expected_duration = clean_track.get("trackTimeMillis")
        matches: dict[str, dict[str, Any]] = {}
        for album_id in albums_by_song.get(song_id, []):
            for result in tracks_by_album.get(album_id, []):
                if apple_explicitness(result) != "explicit" or not result.get("previewUrl"):
                    continue
                if _similarity(expected_title, result.get("trackName", "")) < 0.97:
                    continue
                if _similarity(expected_artist, result.get("artistName", "")) < 0.9:
                    continue
                candidate_duration = result.get("trackTimeMillis")
                if (
                    isinstance(expected_duration, (int, float))
                    and isinstance(candidate_duration, (int, float))
                    and abs(int(expected_duration) - int(candidate_duration)) > 2500
                ):
                    continue
                matches[str(result["trackId"])] = result
        if len(matches) == 1:
            resolved[song_id] = next(iter(matches.values()))
    return resolved, checked


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
    refresh_match: bool = False,
    explicit_equivalent_fetcher: Callable[..., dict[str, Any] | None] = (
        find_explicit_apple_equivalent
    ),
) -> dict[str, Any] | None:
    current_time = time.time() if now is None else now
    recording_mbid = candidate["recording_mbid"]
    country_key = country.upper()
    database_hit, database_value, _database_expired = _read_apple_match_database(
        cache_dir,
        country_key,
        recording_mbid,
        negative_ttl_seconds=negative_ttl_seconds,
        now=current_time,
    )
    if database_hit and not refresh_match:
        return database_value
    cache_path = cache_dir / "apple-v2" / country_key / f"{recording_mbid}.json"
    hit, cached_value, _expired_negative = _read_apple_match_cache(
        cache_path, negative_ttl_seconds, current_time
    )
    if hit and not refresh_match:
        if cached_value and apple_explicitness(cached_value) == "cleaned":
            explicit = explicit_equivalent_fetcher(cached_value, country=country_key)
            if explicit:
                explicit["canonicalReleaseYear"] = _release_year(
                    cached_value.get("releaseDate")
                ) or _release_year(explicit.get("releaseDate"))
                _write_apple_match_database(
                    cache_dir, country_key, recording_mbid, explicit, current_time
                )
                return explicit
        _write_apple_match_database(
            cache_dir, country_key, recording_mbid, cached_value, current_time
        )
        return cached_value

    legacy_path = cache_dir / "apple" / f"{recording_mbid}.json"
    if not refresh_match and not cache_path.exists() and legacy_path.exists():
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        if legacy.get("trackId"):
            if apple_explicitness(legacy) == "cleaned":
                explicit = explicit_equivalent_fetcher(legacy, country=country_key)
                if explicit:
                    explicit["canonicalReleaseYear"] = _release_year(
                        legacy.get("releaseDate")
                    ) or _release_year(explicit.get("releaseDate"))
                    _write_apple_match_database(
                        cache_dir, country_key, recording_mbid, explicit, current_time
                    )
                    return explicit
            _write_apple_match_database(
                cache_dir, country_key, recording_mbid, legacy, current_time
            )
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
    database_payload = _read_apple_artist_database(
        cache_dir, country_key, artist_cache_key, now=current_time
    )
    if database_payload is not None:
        payload = database_payload
    if payload is None:
        _throttle_apple()
        payload = read_json(f"{search_url}?{parameters}")
        _write_apple_artist_database(
            cache_dir, country_key, artist_cache_key, payload, current_time
        )
    elif database_payload is None:
        _write_apple_artist_database(
            cache_dir, country_key, artist_cache_key, payload, current_time
        )

    canonical_year = _release_year(metadata.get("first-release-date"))
    selected = _select_apple_track(
        payload.get("results", []),
        title=title,
        artist=artist,
        canonical_year=canonical_year,
        year_min=year_min,
        year_max=year_max,
    )
    if selected and apple_explicitness(selected) == "cleaned":
        explicit = explicit_equivalent_fetcher(selected, country=country_key)
        if explicit:
            explicit["canonicalReleaseYear"] = canonical_year or explicit.get(
                "canonicalReleaseYear"
            )
            selected = explicit
    _write_apple_match_database(cache_dir, country_key, recording_mbid, selected, current_time)
    return selected


def has_fresh_negative_apple_match(
    cache_dir: Path,
    recording_mbid: str,
    *,
    country: str,
    negative_ttl_seconds: float = DEFAULT_APPLE_NEGATIVE_TTL_SECONDS,
) -> bool:
    hit, cached_value, _expired = _read_apple_match_database(
        cache_dir,
        country.upper(),
        recording_mbid,
        negative_ttl_seconds=negative_ttl_seconds,
        now=time.time(),
    )
    if hit:
        return cached_value is None
    cache_path = cache_dir / "apple-v2" / country.upper() / f"{recording_mbid}.json"
    hit, cached_value, _expired = _read_apple_match_cache(
        cache_path, negative_ttl_seconds, time.time()
    )
    return hit and cached_value is None


def read_cached_apple_track(
    cache_dir: Path, recording_mbid: str, *, country: str = "US"
) -> dict[str, Any] | None:
    """Read a previously matched Apple track without refreshing or mutating caches."""
    hit, cached_value, _expired = _read_apple_match_database(
        cache_dir,
        country.upper(),
        recording_mbid,
        negative_ttl_seconds=DEFAULT_APPLE_NEGATIVE_TTL_SECONDS,
        now=time.time(),
    )
    if hit:
        return cached_value
    current_path = cache_dir / "apple-v2" / country.upper() / f"{recording_mbid}.json"
    if current_path.exists():
        cached = json.loads(current_path.read_text(encoding="utf-8"))
        if cached.get("version") == APPLE_CACHE_VERSION:
            return cached.get("track") if cached.get("status") == "matched" else None
        if cached.get("trackId"):
            return cached

    legacy_path = cache_dir / "apple" / f"{recording_mbid}.json"
    if not legacy_path.exists():
        return None
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    return legacy if legacy.get("trackId") else None


def cache_apple_track(
    cache_dir: Path,
    recording_mbid: str,
    track: dict[str, Any],
    *,
    country: str = "US",
) -> None:
    checked_at = time.time()
    _write_apple_match_database(cache_dir, country.upper(), recording_mbid, track, checked_at)


def fetch_apple_tracks_by_ids(
    track_ids: list[str],
    *,
    country: str,
    batch_size: int = 200,
    request_json: Callable[[str], dict[str, Any]] = read_json,
) -> dict[str, dict[str, Any]]:
    """Refresh current Apple metadata in bounded lookup batches."""
    unique_ids = list(dict.fromkeys(str(track_id) for track_id in track_ids))
    tracks: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[start : start + batch_size]
        query = urllib.parse.urlencode(
            {"id": ",".join(batch), "entity": "song", "country": country.upper()}
        )
        _throttle_apple()
        payload = request_json(f"https://itunes.apple.com/lookup?{query}")
        for result in payload.get("results", []):
            track_id = result.get("trackId")
            if track_id is not None:
                tracks[str(track_id)] = dict(result)
    return tracks


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
                except Exception as error:  # noqa: BLE001 - worker failures are retryable.
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


def _merge_candidate_identities(
    destination: dict[str, dict[str, Any]],
    recordings: list[dict[str, Any]],
    *,
    source: str,
) -> None:
    for recording in recordings:
        recording_mbid = recording.get("recording_mbid")
        if not recording_mbid:
            continue
        normalized = dict(recording)
        normalized.pop("listen_count", None)
        normalized.pop("total_listen_count", None)
        normalized.pop("total_user_count", None)
        normalized["discovery_sources"] = [source]
        previous = destination.get(recording_mbid)
        if previous is None:
            destination[recording_mbid] = normalized
            continue
        sources = list(previous.get("discovery_sources") or [])
        if source not in sources:
            sources.append(source)
        previous["discovery_sources"] = sources


def _nonnegative_optional_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError("ListenBrainz popularity counts cannot be negative")
    return parsed


def _musicbrainz_cache_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS recordings ("
        "mbid TEXT PRIMARY KEY, payload_json TEXT NOT NULL, fetched_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS artists ("
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


def _read_musicbrainz_artists_cache(
    connection: sqlite3.Connection, artist_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not artist_ids:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index in range(0, len(artist_ids), 800):
        batch = artist_ids[index : index + 800]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"SELECT mbid, payload_json FROM artists WHERE mbid IN ({placeholders})", batch
        ).fetchall()
        result.update({str(row[0]): json.loads(row[1]) for row in rows})
    return result


def _compact_apple_track(track: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "wrapperType",
        "kind",
        "artistId",
        "collectionId",
        "trackId",
        "artistName",
        "collectionName",
        "trackName",
        "artistViewUrl",
        "collectionViewUrl",
        "trackViewUrl",
        "previewUrl",
        "artworkUrl100",
        "releaseDate",
        "trackExplicitness",
        "trackTimeMillis",
        "primaryGenreName",
        "isStreamable",
        "canonicalReleaseYear",
        "country",
    )
    return {key: track[key] for key in fields if key in track}


def _apple_cache_connection(cache_dir: Path) -> sqlite3.Connection:
    path = cache_dir / "apple-cache.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS track_matches ("
        "country TEXT NOT NULL, recording_mbid TEXT NOT NULL, status TEXT NOT NULL "
        "CHECK(status IN ('matched', 'negative')), track_json TEXT, "
        "checked_at REAL NOT NULL, PRIMARY KEY(country, recording_mbid))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS artist_searches ("
        "country TEXT NOT NULL, cache_key TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "checked_at REAL NOT NULL, PRIMARY KEY(country, cache_key))"
    )
    return connection


def _read_apple_match_database(
    cache_dir: Path,
    country: str,
    recording_mbid: str,
    *,
    negative_ttl_seconds: float,
    now: float,
) -> tuple[bool, dict[str, Any] | None, bool]:
    path = cache_dir / "apple-cache.sqlite3"
    if not path.exists():
        return False, None, False
    with _apple_cache_connection(cache_dir) as connection:
        row = connection.execute(
            "SELECT status, track_json, checked_at FROM track_matches "
            "WHERE country = ? AND recording_mbid = ?",
            (country, recording_mbid),
        ).fetchone()
    if row is None:
        return False, None, False
    status, track_json, checked_at = str(row[0]), row[1], float(row[2])
    if status == "matched" and track_json:
        return True, json.loads(track_json), False
    if now - checked_at < negative_ttl_seconds:
        return True, None, False
    return False, None, True


def _write_apple_match_database(
    cache_dir: Path,
    country: str,
    recording_mbid: str,
    track: dict[str, Any] | None,
    checked_at: float,
) -> None:
    compact = _compact_apple_track(track) if track else None
    with _apple_cache_connection(cache_dir) as connection:
        connection.execute(
            "INSERT INTO track_matches "
            "(country, recording_mbid, status, track_json, checked_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(country, recording_mbid) DO UPDATE SET status=excluded.status, "
            "track_json=excluded.track_json, checked_at=excluded.checked_at",
            (
                country,
                recording_mbid,
                "matched" if compact else "negative",
                json.dumps(compact, ensure_ascii=False, separators=(",", ":")) if compact else None,
                checked_at,
            ),
        )


def _read_apple_artist_database(
    cache_dir: Path, country: str, cache_key: str, *, now: float
) -> dict[str, Any] | None:
    path = cache_dir / "apple-cache.sqlite3"
    if not path.exists():
        return None
    with _apple_cache_connection(cache_dir) as connection:
        row = connection.execute(
            "SELECT payload_json, checked_at FROM artist_searches "
            "WHERE country = ? AND cache_key = ?",
            (country, cache_key),
        ).fetchone()
    if row is None or now - float(row[1]) >= DEFAULT_APPLE_ARTIST_TTL_SECONDS:
        return None
    return json.loads(row[0])


def _write_apple_artist_database(
    cache_dir: Path,
    country: str,
    cache_key: str,
    payload: dict[str, Any],
    checked_at: float,
) -> None:
    results = [
        _compact_apple_track(result)
        for result in payload.get("results", [])
        if isinstance(result, dict) and result.get("trackId")
    ]
    compact_payload = {"resultCount": len(results), "results": results}
    with _apple_cache_connection(cache_dir) as connection:
        connection.execute(
            "INSERT INTO artist_searches "
            "(country, cache_key, payload_json, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(country, cache_key) DO UPDATE SET "
            "payload_json=excluded.payload_json, checked_at=excluded.checked_at",
            (
                country,
                cache_key,
                json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")),
                checked_at,
            ),
        )


def _credited_artist_ids(recording: dict[str, Any]) -> list[str]:
    return [
        str(artist_id)
        for credit in recording.get("artist-credit", [])
        if (artist_id := credit.get("artist", {}).get("id"))
    ]


def _musicbrainz_country(artist: dict[str, Any]) -> str | None:
    country = artist.get("country")
    if not isinstance(country, str):
        return None
    normalized = country.strip().upper()
    if len(normalized) != 2 or not normalized.isalpha():
        return None
    return normalized


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
