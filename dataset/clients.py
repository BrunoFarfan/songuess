from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

USER_AGENT = "Songuess/0.1 (https://github.com/BrunoFarfan/songuess)"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(path)
    return payload


def fetch_listenbrainz_candidates(cache_dir: Path, count: int) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    ranges = ("all_time", "year", "half_yearly", "quarter", "month", "week")
    for statistics_range in ranges:
        query = urllib.parse.urlencode({"range": statistics_range, "count": 1000, "offset": 0})
        url = f"https://api.listenbrainz.org/1/stats/sitewide/recordings?{query}"
        payload = cached_json(cache_dir / f"listenbrainz-{statistics_range}.json", url)
        recordings = payload.get("payload", {}).get("recordings", [])
        for recording in recordings:
            recording_mbid = recording.get("recording_mbid")
            if not recording_mbid:
                continue
            existing = candidates.get(recording_mbid)
            if existing is None or recording.get("listen_count", 0) > existing.get(
                "listen_count", 0
            ):
                candidates[recording_mbid] = recording
    ranked = sorted(candidates.values(), key=lambda item: item.get("listen_count", 0), reverse=True)
    if len(ranked) < count:
        overflow = fetch_listenbrainz_artist_overflow(
            cache_dir, set(candidates), count - len(ranked)
        )
        ranked.extend(overflow)
    return ranked[:count]


def fetch_listenbrainz_artist_overflow(
    cache_dir: Path, existing_mbids: set[str], count: int
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"range": "all_time", "count": 200, "offset": 0})
    artists_payload = cached_json(
        cache_dir / "listenbrainz-top-artists.json",
        f"https://api.listenbrainz.org/1/stats/sitewide/artists?{query}",
    )
    overflow: dict[str, dict[str, Any]] = {}
    for artist in artists_payload.get("payload", {}).get("artists", []):
        artist_mbid = artist.get("artist_mbid")
        if not artist_mbid:
            continue
        parameters = urllib.parse.urlencode(
            {
                "mode": "easy",
                "max_similar_artists": 1,
                "max_recordings_per_artist": 40,
                "pop_begin": 70,
                "pop_end": 100,
            }
        )
        payload = cached_json(
            cache_dir / "listenbrainz-radio" / f"{artist_mbid}.json",
            f"https://api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}?{parameters}",
        )
        for recording in payload.get(artist_mbid, []):
            recording_mbid = recording.get("recording_mbid")
            if not recording_mbid or recording_mbid in existing_mbids:
                continue
            listen_count = int(recording.get("total_listen_count") or 0)
            previous = overflow.get(recording_mbid)
            if previous is None or listen_count > previous["listen_count"]:
                overflow[recording_mbid] = {
                    "artist_mbids": [artist_mbid],
                    "artist_name": recording.get("similar_artist_name")
                    or artist.get("artist_name", ""),
                    "listen_count": listen_count,
                    "recording_mbid": recording_mbid,
                    "release_mbid": "",
                    "release_name": "",
                    "track_name": "",
                }
        if len(overflow) >= count:
            break
    return sorted(overflow.values(), key=lambda item: item.get("listen_count", 0), reverse=True)[
        :count
    ]


def fetch_musicbrainz_metadata(
    cache_dir: Path, candidates: list[dict[str, Any]], *, batch_size: int = 20
) -> dict[str, dict[str, Any]]:
    cache_path = cache_dir / "musicbrainz-recordings.json"
    metadata: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        metadata = json.loads(cache_path.read_text(encoding="utf-8"))

    missing = [
        candidate["recording_mbid"]
        for candidate in candidates
        if candidate["recording_mbid"] not in metadata
    ]
    for index in range(0, len(missing), batch_size):
        batch = missing[index : index + batch_size]
        query = "rid:(" + " OR ".join(batch) + ")"
        parameters = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 100})
        payload = read_json(f"https://musicbrainz.org/ws/2/recording/?{parameters}")
        found = {recording["id"]: recording for recording in payload.get("recordings", [])}
        for recording_mbid in batch:
            metadata[recording_mbid] = found.get(recording_mbid, {})
        cache_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        if index + batch_size < len(missing):
            time.sleep(1.05)
    return metadata


def search_apple_track(
    cache_dir: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    *,
    country: str,
    year_min: int,
    year_max: int,
) -> dict[str, Any] | None:
    recording_mbid = candidate["recording_mbid"]
    cache_path = cache_dir / "apple" / f"{recording_mbid}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return cached or None

    title = metadata.get("title") or candidate.get("track_name", "")
    artist = _musicbrainz_artist(metadata) or candidate.get("artist_name", "")
    artist_cache_key = hashlib.sha256(f"{country}:{artist.casefold()}".encode()).hexdigest()
    artist_cache_path = cache_dir / "apple-artists" / f"{artist_cache_key}.json"
    parameters = urllib.parse.urlencode(
        {
            "term": artist,
            "media": "music",
            "entity": "song",
            "attribute": "artistTerm",
            "country": country,
            "limit": 200,
        }
    )
    search_url = "https://itunes.apple.com/WebObjects/MZStoreServices.woa/wa/wsSearch"
    if artist_cache_path.exists():
        payload = json.loads(artist_cache_path.read_text(encoding="utf-8"))
    else:
        _throttle_apple()
        payload = cached_json(artist_cache_path, f"{search_url}?{parameters}")
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
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(selected or {}, ensure_ascii=False), encoding="utf-8")
    return selected


def preview_is_available(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.status in {200, 206} and (
                content_type.startswith("audio/") or "octet-stream" in content_type
            )
    except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError):
        return False


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
