"""Idempotently backfill Spotify links, stream counts, and catalog percentiles.

Spotify does not expose lifetime playcounts in its documented Web API. This
development workflow lets Spotify's public web client hydrate track data and
retains only the validated URL, playcount, retrieval metadata, and audit status.
Credentials, cookies, headers, and response bodies are never persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unicodedata
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dataset.clients import (
    fetch_musicbrainz_spotify_urls,
    read_cached_apple_track,
    read_json,
)
from dataset.populate import initialize_database, percentile_scores

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPOSITORY_DIR / "backend" / "data" / "songuess.sqlite3"
DEFAULT_METADATA_CACHE = REPOSITORY_DIR / "dataset" / "cache" / "musicbrainz-recordings.sqlite3"
DEFAULT_REPORT = REPOSITORY_DIR / "dataset" / "cache" / "spotify-streams-backfill.json"
DEFAULT_BRAVE_EXECUTABLE = Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
SPOTIFY_SOURCE = "spotify_web_hydration"


@dataclass(frozen=True)
class CatalogSong:
    id: int
    musicbrainz_id: str
    title: str
    artist: str
    album: str | None
    credited_artists: tuple[str, ...]
    duration_ms: int | None
    isrcs: tuple[str, ...]
    spotify_url: str | None
    stream_count: int | None
    stream_count_fetched_at: str | None
    stream_count_status: str
    has_retryable_failure: bool
    failure_status: str | None
    musicbrainz_relationship_checked_at: str | None
    catalog_lookup_checked_at: str | None


@dataclass(frozen=True)
class BrowserJob:
    song_id: int
    title: str
    artist: str
    album: str | None
    credited_artists: tuple[str, ...]
    duration_ms: int | None
    match_method: str
    spotify_urls: tuple[str, ...]
    search_urls: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--metadata-cache", type=Path, default=DEFAULT_METADATA_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, help="Process at most this many pending songs")
    parser.add_argument("--refresh", action="store_true", help="Refresh complete rows too")
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Include songs recorded in the retryable-failure ledger",
    )
    parser.add_argument("--stale-after-days", type=int, default=30)
    parser.add_argument("--browser-workers", type=int, default=24)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Commit this many songs per resumable browser batch",
    )
    parser.add_argument("--search-candidates", type=int, default=3)
    parser.add_argument("--browser-timeout-seconds", type=int, default=7200)
    parser.add_argument("--browser-executable", type=Path, default=DEFAULT_BRAVE_EXECUTABLE)
    parser.add_argument(
        "--playwright-cli",
        type=Path,
        default=Path(shutil.which("playwright-cli") or "playwright-cli"),
    )
    return parser.parse_args()


def _valid_track_url(value: str | None) -> bool:
    if not value or not value.startswith("https://open.spotify.com/track/"):
        return False
    track_id = value.split("/track/", 1)[1].split("?", 1)[0]
    return len(track_id) == 22 and track_id.isalnum()


def _is_fresh(value: str | None, stale_after_days: int) -> bool:
    if not value:
        return False
    try:
        fetched_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return fetched_at >= datetime.now(UTC) - timedelta(days=stale_after_days)


def load_catalog_songs(database: Path, metadata_cache: Path) -> list[CatalogSong]:
    metadata: dict[str, dict[str, Any]] = {}
    if metadata_cache.exists():
        with sqlite3.connect(metadata_cache) as connection:
            for mbid, payload_json in connection.execute(
                "SELECT mbid, payload_json FROM recordings"
            ):
                try:
                    payload = json.loads(payload_json)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict):
                    metadata[str(mbid)] = payload

    with sqlite3.connect(database) as connection:
        credited_artists: dict[int, list[str]] = {}
        for song_id, artist_name in connection.execute(
            "SELECT sa.song_id, sa.credited_name FROM song_artists sa "
            "ORDER BY sa.song_id, sa.credit_order"
        ):
            credited_artists.setdefault(int(song_id), []).append(str(artist_name))
        rows = connection.execute(
            "SELECT id, title, artist, musicbrainz_id, spotify_url, stream_count, "
            "stream_count_fetched_at, stream_count_status, f.status, "
            "f.musicbrainz_relationship_checked_at, f.catalog_lookup_checked_at, album "
            "FROM songs LEFT JOIN spotify_backfill_failures f ON f.song_id = songs.id "
            "WHERE enabled = 1 ORDER BY id"
        ).fetchall()

    songs: list[CatalogSong] = []
    for row in rows:
        recording = metadata.get(str(row[3]), {})
        apple_track = read_cached_apple_track(metadata_cache.parent, str(row[3]))
        raw_duration = (
            apple_track.get("trackTimeMillis") if apple_track else recording.get("length")
        )
        duration_ms = int(raw_duration) if isinstance(raw_duration, (int, float)) else None
        raw_isrcs = recording.get("isrcs")
        isrcs = tuple(
            dict.fromkeys(
                str(value).strip().upper() for value in raw_isrcs or [] if str(value).strip()
            )
        )
        songs.append(
            CatalogSong(
                id=int(row[0]),
                musicbrainz_id=str(row[3]),
                title=str(row[1]),
                artist=str(row[2]),
                album=str(row[11]) if row[11] else None,
                credited_artists=tuple(credited_artists.get(int(row[0]), [str(row[2])])),
                duration_ms=duration_ms,
                isrcs=isrcs,
                spotify_url=str(row[4]) if row[4] else None,
                stream_count=int(row[5]) if row[5] is not None else None,
                stream_count_fetched_at=str(row[6]) if row[6] else None,
                stream_count_status=str(row[7]),
                has_retryable_failure=row[8] is not None,
                failure_status=str(row[8]) if row[8] else None,
                musicbrainz_relationship_checked_at=str(row[9]) if row[9] else None,
                catalog_lookup_checked_at=str(row[10]) if row[10] else None,
            )
        )
    return songs


def _normalized_identity(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"[^\w]", "", decomposed.casefold(), flags=re.UNICODE)


def fetch_catalog_spotify_urls(
    songs: list[CatalogSong], *, workers: int = 4
) -> tuple[dict[int, str], set[int]]:
    """Resolve stubborn URLs from a public catalog; never use its popularity value.

    Exact ISRC is preferred. The fallback requires exact normalized title and primary
    artist plus Apple/Spotify durations within 3.5 seconds. Raw responses are discarded.
    """

    def lookup(song: CatalogSong) -> tuple[int, str | None, bool]:
        query = urllib.parse.urlencode({"searchText": song.title})
        try:
            payload = read_json(f"https://api.reccobeats.com/v1/track/search?{query}", timeout=20)
        except (OSError, RuntimeError, ValueError):
            return song.id, None, False
        expected_isrcs = set(song.isrcs)
        expected_title = _normalized_identity(song.title)
        primary_artist = _normalized_identity(
            song.credited_artists[0] if song.credited_artists else song.artist
        )
        metadata_matches: list[str] = []
        for candidate in payload.get("content", []):
            if not isinstance(candidate, dict):
                continue
            href = candidate.get("href")
            if not isinstance(href, str) or not _valid_track_url(href):
                continue
            candidate_isrc = str(candidate.get("isrc") or "").strip().upper()
            if candidate_isrc and candidate_isrc in expected_isrcs:
                return song.id, href.split("?", 1)[0], True
            if _normalized_identity(candidate.get("trackTitle")) != expected_title:
                continue
            candidate_artists = {
                _normalized_identity(artist.get("name"))
                for artist in candidate.get("artists", [])
                if isinstance(artist, dict)
            }
            if primary_artist not in candidate_artists:
                continue
            candidate_duration = candidate.get("durationMs")
            if (
                song.duration_ms
                and isinstance(candidate_duration, (int, float))
                and abs(song.duration_ms - int(candidate_duration)) <= 3500
            ):
                metadata_matches.append(href.split("?", 1)[0])
        unique_matches = list(dict.fromkeys(metadata_matches))
        return song.id, unique_matches[0] if len(unique_matches) == 1 else None, True

    resolved: dict[int, str] = {}
    checked: set[int] = set()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(songs)))) as executor:
        futures = [executor.submit(lookup, song) for song in songs]
        for future in as_completed(futures):
            song_id, spotify_url, completed = future.result()
            if completed:
                checked.add(song_id)
            if spotify_url:
                resolved[song_id] = spotify_url
    return resolved, checked


def build_jobs(
    songs: list[CatalogSong],
    *,
    refresh: bool,
    retry_failures: bool,
    stale_after_days: int,
    limit: int | None,
) -> tuple[list[BrowserJob], int]:
    jobs: list[BrowserJob] = []
    skipped = 0
    ordered_songs = sorted(
        songs,
        key=lambda song: (
            0 if _valid_track_url(song.spotify_url) else 1 if song.isrcs else 2,
            song.id,
        ),
    )
    for song in ordered_songs:
        complete = (
            song.stream_count_status == "complete"
            and song.stream_count is not None
            and _valid_track_url(song.spotify_url)
            and _is_fresh(song.stream_count_fetched_at, stale_after_days)
        )
        if complete and not refresh:
            skipped += 1
            continue
        if song.has_retryable_failure and not retry_failures:
            skipped += 1
            continue
        if limit is not None and len(jobs) >= limit:
            continue

        spotify_urls = (song.spotify_url,) if _valid_track_url(song.spotify_url) else ()
        if spotify_urls:
            method = "existing_url"
            search_urls: tuple[str, ...] = ()
        elif song.isrcs and not (retry_failures and song.has_retryable_failure):
            method = "isrc"
            search_urls = tuple(
                "https://open.spotify.com/search/"
                + urllib.parse.quote(f"isrc:{isrc}", safe="")
                + "/tracks"
                for isrc in song.isrcs[:3]
            )
        else:
            method = "exact_metadata"
            # Artist syntax is unreliable for multi-credit display strings; the
            # candidates are validated against normalized MusicBrainz credits below.
            primary_artist = song.credited_artists[0] if song.credited_artists else song.artist
            if retry_failures and song.has_retryable_failure:
                queries = [
                    f'track:"{song.title}" artist:"{primary_artist}"',
                    f"{song.title} {primary_artist}",
                ]
                if song.album:
                    queries.extend(
                        [
                            f'track:"{song.title}" album:"{song.album}"',
                            f"{song.title} {song.album}",
                        ]
                    )
            else:
                queries = [f'track:"{song.title}" artist:"{primary_artist}"']
            search_urls = tuple(
                "https://open.spotify.com/search/" + urllib.parse.quote(query, safe="") + "/tracks"
                for query in dict.fromkeys(queries)
            )
        jobs.append(
            BrowserJob(
                song_id=song.id,
                title=song.title,
                artist=song.artist,
                album=song.album,
                credited_artists=song.credited_artists,
                duration_ms=song.duration_ms,
                match_method=method,
                spotify_urls=spotify_urls,
                search_urls=search_urls,
            )
        )
    return jobs, skipped


def playwright_function(jobs: list[BrowserJob], workers: int, search_candidates: int) -> str:
    payload = [
        {
            "song_id": job.song_id,
            "title": job.title,
            "artist": job.artist,
            "album": job.album,
            "credited_artists": list(job.credited_artists),
            "duration_ms": job.duration_ms,
            "match_method": job.match_method,
            "spotify_urls": list(job.spotify_urls),
            "search_urls": list(job.search_urls),
        }
        for job in jobs
    ]
    return f"""async (page) => {{
  const jobs = {json.dumps(payload, ensure_ascii=False)};
  const queue = jobs.slice();
  const results = [];
  const failures = [];
  let hydrationRequest = null;
  let searchRequest = null;
  const metrics = {{
    directSearches: 0,
    directSearchResponses: 0,
    directSearchEmpty: 0,
    navigatedSearches: 0,
    directHydrations: 0,
    navigatedHydrations: 0
  }};
  const workerCount = Math.max(1, Math.min({workers}, jobs.length));
  const normalize = value => String(value || "")
    .normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase()
    .replace(/[^\\p{{L}}\\p{{N}}]/gu, "");
  const artistNames = track => {{
    const names = [
      ...(track.firstArtist?.items || []),
      ...(track.otherArtists?.items || [])
    ].map(value => value?.profile?.name).filter(Boolean);
    return [...new Set(names)];
  }};
  const findTrack = (payload, uri) => {{
    let match = null;
    const visit = value => {{
      if (match || !value || typeof value !== "object") return;
      if (value.uri === uri && Object.prototype.hasOwnProperty.call(value, "playcount")) {{
        const playcount = Number.parseInt(String(value.playcount), 10);
        if (Number.isSafeInteger(playcount)) {{
          match = {{
            name: value.name,
            playcount,
            duration_ms: Number(value.duration?.totalMilliseconds),
            album: value.albumOfTrack?.name,
            artists: artistNames(value)
          }};
          return;
        }}
      }}
      for (const child of Object.values(value)) visit(child);
    }};
    visit(payload);
    return match;
  }};
  const trackUrls = (payload, limit) => {{
    const urls = [];
    const seen = new Set();
    const visit = value => {{
      if (urls.length >= limit || !value || typeof value !== "object") return;
      if (typeof value.uri === "string" && value.uri.startsWith("spotify:track:")) {{
        const id = value.uri.slice("spotify:track:".length);
        if (id && !seen.has(id)) {{
          seen.add(id);
          urls.push("https://open.spotify.com/track/" + id);
        }}
      }}
      for (const child of Object.values(value)) visit(child);
    }};
    visit(payload);
    return urls;
  }};
  const operation = request => {{
    if (!request.url().includes("api-partner.spotify.com/pathfinder/v2/query")) return null;
    try {{ return request.postDataJSON()?.operationName || null; }}
    catch {{ return null; }}
  }};
  const requestTemplate = async request => ({{
    url: request.url(),
    headers: await request.allHeaders(),
    body: request.postDataJSON()
  }});
  const templatePost = async (workerPage, template, variables) => {{
    const headers = {{ ...template.headers }};
    for (const name of Object.keys(headers)) {{
      if (name.startsWith(":") || name.startsWith("sec-") || [
        "content-length", "host", "connection", "cookie", "accept-encoding",
        "origin", "referer", "priority", "user-agent"
      ].includes(name)) delete headers[name];
    }}
    const body = JSON.parse(JSON.stringify(template.body));
    body.variables = {{ ...(body.variables || {{}}), ...variables }};
    const result = await workerPage.evaluate(async request => {{
      try {{
        const response = await fetch(request.url, {{
          method: "POST",
          headers: request.headers,
          body: JSON.stringify(request.body),
          credentials: "include"
        }});
        return {{
          ok: response.ok,
          payload: response.ok ? await response.json() : null
        }};
      }} catch {{
        return {{ ok: false, payload: null }};
      }}
    }}, {{ url: template.url, headers, body }});
    return result?.ok ? result.payload : null;
  }};
  const searchTerm = searchUrl => {{
    const encoded = new URL(searchUrl).pathname.split("/search/")[1]?.split("/tracks")[0];
    return encoded ? decodeURIComponent(encoded) : "";
  }};
  const directSearch = async (workerPage, searchUrl, limit) => {{
    if (!searchRequest) return null;
    try {{
      const response = await templatePost(workerPage, searchRequest, {{
        searchTerm: searchTerm(searchUrl),
        offset: 0,
        limit: Math.max(limit, 5),
        numberOfTopResults: Math.max(limit, 5)
      }});
      if (!response) return null;
      metrics.directSearchResponses += 1;
      const urls = trackUrls(response, limit);
      if (!urls.length) {{
        metrics.directSearchEmpty += 1;
        return null;
      }}
      metrics.directSearches += 1;
      return urls;
    }} catch {{
      return null;
    }}
  }};
  const directHydrate = async (workerPage, trackId) => {{
    if (!hydrationRequest) return null;
    try {{
      const response = await templatePost(workerPage, hydrationRequest, {{
        uri: "spotify:track:" + trackId
      }});
      if (!response) return null;
      const track = findTrack(response, "spotify:track:" + trackId);
      if (!track) return null;
      metrics.directHydrations += 1;
      return {{ status: "complete", track }};
    }} catch {{
      return null;
    }}
  }};
  const hydrate = async (workerPage, spotifyUrl) => {{
    const trackId = spotifyUrl.split("/track/")[1]?.split("?")[0];
    if (!trackId) return {{ status: "invalid_spotify_url" }};
    const direct = await directHydrate(workerPage, trackId);
    if (direct) return direct;
    try {{
      const requestPromise = workerPage.waitForRequest(request => {{
        if (!request.url().includes("api-partner.spotify.com/pathfinder/v2/query")) return false;
        try {{
          const body = request.postDataJSON();
          return body?.operationName === "getTrack"
            && body?.variables?.uri === "spotify:track:" + trackId;
        }} catch {{ return false; }}
      }}, {{ timeout: 30000 }});
      const responsePromise = workerPage.waitForResponse(response => {{
        if (!response.url().includes("api-partner.spotify.com/pathfinder/v2/query")) return false;
        try {{
          const body = response.request().postDataJSON();
          return body?.operationName === "getTrack"
            && body?.variables?.uri === "spotify:track:" + trackId;
        }} catch {{ return false; }}
      }}, {{ timeout: 30000 }});
      await workerPage.goto(spotifyUrl, {{ waitUntil: "domcontentloaded", timeout: 45000 }});
      const [request, response] = await Promise.all([requestPromise, responsePromise]);
      hydrationRequest = await requestTemplate(request);
      metrics.navigatedHydrations += 1;
      if (!response.ok()) return {{ status: "spotify_http_" + response.status() }};
      const track = findTrack(await response.json(), "spotify:track:" + trackId);
      return track ? {{ status: "complete", track }} : {{ status: "no_playcount" }};
    }} catch {{
      return {{ status: "navigation_or_hydration_error" }};
    }}
  }};
  const discover = async (workerPage, job) => {{
    const urls = [...job.spotify_urls];
    const candidateLimit = job.match_method === "exact_metadata" ? {search_candidates} : 1;
    for (const searchUrl of job.search_urls) {{
      const direct = await directSearch(workerPage, searchUrl, candidateLimit);
      if (direct) {{
        for (const url of direct) if (!urls.includes(url)) urls.push(url);
        continue;
      }}
      try {{
        const requestPromise = workerPage.waitForRequest(
          request => operation(request) === "searchTracks",
          {{ timeout: 30000 }}
        );
        await workerPage.goto(searchUrl, {{ waitUntil: "domcontentloaded", timeout: 45000 }});
        const request = await requestPromise;
        if (!searchRequest) searchRequest = await requestTemplate(request);
        metrics.navigatedSearches += 1;
        const trackLinks = workerPage.locator('a[href*="/track/"]');
        await trackLinks.first().waitFor({{ state: "attached", timeout: 10000 }});
        const found = await trackLinks.evaluateAll(
          (nodes, limit) => nodes.slice(0, limit).map(node => node.href),
          candidateLimit
        );
        for (const url of found) if (!urls.includes(url)) urls.push(url);
      }} catch {{ /* Try another ISRC or report no candidate below. */ }}
    }}
    return urls.slice(0, candidateLimit * Math.max(1, job.search_urls.length));
  }};
  const validate = (job, track) => {{
    const expectedTitle = normalize(job.title);
    const candidateTitle = normalize(track.name);
    if (job.match_method === "existing_url") return {{ ok: true, status: "complete" }};
    if (job.match_method === "isrc") {{
      return {{ ok: true, status: "complete" }};
    }}
    if (!(expectedTitle === candidateTitle
      || expectedTitle.startsWith(candidateTitle)
      || candidateTitle.startsWith(expectedTitle))) {{
      return {{ ok: false, status: "title_mismatch" }};
    }}
    const candidateArtists = track.artists.map(normalize);
    const primaryArtist = normalize(job.credited_artists[0] || job.artist);
    const artistMatches = candidateArtists.includes(primaryArtist);
    const expectedAlbum = normalize(job.album);
    const candidateAlbum = normalize(track.album);
    const albumMatches = Boolean(expectedAlbum && candidateAlbum && (
      expectedAlbum === candidateAlbum
      || expectedAlbum.startsWith(candidateAlbum)
      || candidateAlbum.startsWith(expectedAlbum)
    ));
    if (!artistMatches && !albumMatches) return {{ ok: false, status: "artist_mismatch" }};
    if (job.duration_ms && Number.isFinite(track.duration_ms)) {{
      return {{
        ok: Math.abs(job.duration_ms - track.duration_ms) <= 3500,
        status: "duration_mismatch"
      }};
    }}
    return {{ ok: true, status: "complete" }};
  }};
  const runJob = async (workerPage, job) => {{
    const urls = await discover(workerPage, job);
    if (!urls.length) {{
      failures.push({{
        song_id: job.song_id,
        status: "no_spotify_candidate",
        match_method: job.match_method
      }});
      return;
    }}
    const candidates = [];
    let lastStatus = "candidate_mismatch";
    for (const spotifyUrl of urls) {{
      const hydrated = await hydrate(workerPage, spotifyUrl);
      lastStatus = hydrated.status;
      const validation = hydrated.track ? validate(job, hydrated.track) : null;
      if (hydrated.track && validation?.ok) {{
        candidates.push({{
          spotify_url: spotifyUrl,
          stream_count: hydrated.track.playcount,
          match_method: job.match_method
        }});
        break;
      }} else if (hydrated.track) {{
        lastStatus = validation?.status || "candidate_mismatch";
      }}
    }}
    if (!candidates.length) {{
      failures.push({{
        song_id: job.song_id,
        status: lastStatus,
        match_method: job.match_method
      }});
      return;
    }}
    candidates.sort((left, right) => right.stream_count - left.stream_count);
    results.push({{ song_id: job.song_id, ...candidates[0] }});
  }};
  const seedSearchUrl = jobs.find(job => job.search_urls.length)?.search_urls[0];
  if (seedSearchUrl) {{
    try {{
      const requestPromise = page.waitForRequest(
        request => operation(request) === "searchTracks",
        {{ timeout: 30000 }}
      );
      await page.goto(seedSearchUrl, {{ waitUntil: "domcontentloaded", timeout: 45000 }});
      searchRequest = await requestTemplate(await requestPromise);
      metrics.navigatedSearches += 1;
    }} catch {{ /* Normal navigation remains available as a fallback. */ }}
  }}
  const pages = [page];
  for (let index = 1; index < workerCount; index += 1) pages.push(await page.context().newPage());
  let completed = 0;
  await Promise.all(pages.map(async workerPage => {{
    while (queue.length) {{
      const job = queue.shift();
      if (job) await runJob(workerPage, job);
      completed += 1;
      if (completed % 100 === 0) console.log("SONGUESS_PROGRESS " + completed + "/" + jobs.length);
    }}
  }}));
  for (const workerPage of pages.slice(1)) await workerPage.close();
  return {{ results, failures, metrics }};
}}"""


def _parse_playwright_result(stdout: str) -> dict[str, Any]:
    """Parse a raw result even when Playwright forwards progress console lines."""
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as original_error:
        result = None
        decoder = json.JSONDecoder()
        for index, character in enumerate(stdout):
            if character != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        for line in reversed(stdout.splitlines()):
            if result is not None:
                break
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                result = candidate
                break
        if result is None:
            raise original_error
    if not isinstance(result, dict):
        raise TypeError("Spotify browser backfill returned an invalid result")
    return result


def run_playwright(
    jobs: list[BrowserJob],
    *,
    workers: int,
    search_candidates: int,
    playwright_cli: Path,
    browser_executable: Path,
    timeout_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    if not jobs:
        return {"results": [], "failures": []}
    if not browser_executable.exists():
        raise RuntimeError(f"Chromium browser executable not found: {browser_executable}")
    cli = str(playwright_cli)
    # Artist expansion can run multiple browser batches concurrently in one
    # process. Thread identity keeps their Playwright daemon sessions isolated.
    session_name = f"sg-{os.getpid()}-{threading.get_ident()}"
    subprocess_environment = {**os.environ, "TMPDIR": "/tmp"}
    seed_url = jobs[0].spotify_urls[0] if jobs[0].spotify_urls else jobs[0].search_urls[0]
    with tempfile.TemporaryDirectory(prefix="songuess-spotify-") as temporary:
        temporary_dir = Path(temporary)
        config_path = temporary_dir / "playwright-cli.json"
        function_path = temporary_dir / "backfill.js"
        config_path.write_text(
            json.dumps(
                {
                    "browser": {
                        "browserName": "chromium",
                        "launchOptions": {"executablePath": str(browser_executable)},
                        "contextOptions": {"viewport": {"width": 1280, "height": 800}},
                    }
                }
            ),
            encoding="utf-8",
        )
        function_path.write_text(
            playwright_function(jobs, workers, search_candidates), encoding="utf-8"
        )
        base = [cli, "--session", session_name]
        try:
            opened = subprocess.run(
                [*base, "open", seed_url, f"--config={config_path}"],
                cwd=temporary_dir,
                env=subprocess_environment,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if opened.returncode:
                raise RuntimeError(f"Could not open Playwright browser: {opened.stderr.strip()}")
            completed = subprocess.run(
                [
                    cli,
                    "--raw",
                    "--session",
                    session_name,
                    "run-code",
                    f"--filename={function_path}",
                ],
                cwd=temporary_dir,
                env=subprocess_environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode:
                raise RuntimeError(f"Spotify browser backfill failed: {completed.stderr.strip()}")
            return _parse_playwright_result(completed.stdout)
        finally:
            subprocess.run(
                [*base, "close"],
                cwd=temporary_dir,
                env=subprocess_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )


def recalculate_popularity_scores(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        counts = {
            int(song_id): int(stream_count)
            for song_id, stream_count in connection.execute(
                "SELECT id, stream_count FROM songs WHERE enabled = 1 AND stream_count IS NOT NULL"
            )
        }
        scores = percentile_scores(counts)
        connection.execute("UPDATE songs SET popularity_score = NULL WHERE enabled = 1")
        connection.executemany(
            "UPDATE songs SET popularity_score = ? WHERE id = ?",
            [(round(score), song_id) for song_id, score in scores.items()],
        )


def persist_results(
    database: Path,
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = [
        (
            str(item["spotify_url"]),
            int(item["stream_count"]),
            fetched_at,
            SPOTIFY_SOURCE,
            int(item["song_id"]),
        )
        for item in results
    ]
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "UPDATE songs SET spotify_url = ?, stream_count = ?, stream_count_fetched_at = ?, "
            "stream_count_source = ?, stream_count_status = 'complete' WHERE id = ?",
            rows,
        )
        connection.executemany(
            "DELETE FROM spotify_backfill_failures WHERE song_id = ?",
            [(int(item["song_id"]),) for item in results],
        )
        connection.executemany(
            "UPDATE songs SET stream_count_status = 'hydration_failed' "
            "WHERE id = ? AND stream_count IS NULL",
            [(int(item["song_id"]),) for item in failures],
        )
        connection.executemany(
            "INSERT INTO spotify_backfill_failures "
            "(song_id, status, match_method, attempted_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(song_id) DO UPDATE SET status=excluded.status, "
            "match_method=excluded.match_method, attempted_at=excluded.attempted_at",
            [
                (
                    int(item["song_id"]),
                    str(item.get("status") or "unknown"),
                    str(item.get("match_method") or "browser_batch"),
                    fetched_at,
                )
                for item in failures
            ],
        )
    recalculate_popularity_scores(database)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")
    if args.browser_workers < 1 or args.search_candidates < 1 or args.batch_size < 1:
        raise ValueError("worker, candidate, and batch counts must be positive")
    if args.stale_after_days < 1:
        raise ValueError("stale-after-days must be positive")
    initialize_database(args.database)
    songs = load_catalog_songs(args.database, args.metadata_cache)
    if args.retry_failures:
        unresolved_failure_ids = {
            song.id
            for song in songs
            if song.has_retryable_failure
            and not _valid_track_url(song.spotify_url)
            and not song.musicbrainz_relationship_checked_at
        }
        if unresolved_failure_ids:
            unresolved_failures = [song for song in songs if song.id in unresolved_failure_ids]
            print(
                "  Resolving structured MusicBrainz Spotify relationships for "
                f"{len(unresolved_failures):,} retryable failures...",
                flush=True,
            )
            relationships = fetch_musicbrainz_spotify_urls(
                [song.musicbrainz_id for song in unresolved_failures]
            )
            checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            with sqlite3.connect(args.database) as connection:
                connection.executemany(
                    "UPDATE spotify_backfill_failures "
                    "SET musicbrainz_relationship_checked_at = ? "
                    "WHERE song_id = ?",
                    [
                        (checked_at, song.id)
                        for song in unresolved_failures
                        if not _valid_track_url(relationships.get(song.musicbrainz_id))
                    ],
                )
            songs = [
                replace(song, spotify_url=relationships.get(song.musicbrainz_id))
                if song.id in unresolved_failure_ids
                and _valid_track_url(relationships.get(song.musicbrainz_id))
                else replace(song, musicbrainz_relationship_checked_at=checked_at)
                if song.id in unresolved_failure_ids
                else song
                for song in songs
            ]
        catalog_failures = [
            song
            for song in songs
            if song.has_retryable_failure
            and not _valid_track_url(song.spotify_url)
            and not _is_fresh(song.catalog_lookup_checked_at, 7)
        ]
        if catalog_failures:
            print(
                "  Resolving exact ISRC/metadata matches from the fallback catalog for "
                f"{len(catalog_failures):,} retryable failures...",
                flush=True,
            )
            catalog_urls, checked_ids = fetch_catalog_spotify_urls(catalog_failures)
            checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            with sqlite3.connect(args.database) as connection:
                connection.executemany(
                    "UPDATE spotify_backfill_failures SET catalog_lookup_checked_at = ? "
                    "WHERE song_id = ?",
                    [(checked_at, song_id) for song_id in checked_ids],
                )
            songs = [
                replace(song, spotify_url=catalog_urls[song.id])
                if song.id in catalog_urls
                else replace(song, catalog_lookup_checked_at=checked_at)
                if song.id in checked_ids
                else song
                for song in songs
            ]
    jobs, skipped = build_jobs(
        songs,
        refresh=args.refresh,
        retry_failures=args.retry_failures,
        stale_after_days=args.stale_after_days,
        limit=args.limit,
    )
    print(
        f"Spotify backfill: {len(songs):,} enabled; {skipped:,} fresh; "
        f"{len(jobs):,} queued across {args.browser_workers} browser pages.",
        flush=True,
    )
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for start in range(0, len(jobs), args.batch_size):
        batch = jobs[start : start + args.batch_size]
        print(
            f"  Browser batch {start + 1:,}-{start + len(batch):,}/{len(jobs):,}...",
            flush=True,
        )
        browser_result: dict[str, list[dict[str, Any]]] | None = None
        for attempt in range(1, 3):
            try:
                browser_result = run_playwright(
                    batch,
                    workers=args.browser_workers,
                    search_candidates=args.search_candidates,
                    playwright_cli=args.playwright_cli,
                    browser_executable=args.browser_executable,
                    timeout_seconds=args.browser_timeout_seconds,
                )
                break
            except (RuntimeError, TypeError, json.JSONDecodeError) as error:
                print(f"    browser attempt {attempt}/2 failed: {error}", flush=True)
        if browser_result is None:
            browser_result = {
                "results": [],
                "failures": [
                    {
                        "song_id": job.song_id,
                        "status": "browser_batch_failed",
                        "match_method": "browser_batch",
                    }
                    for job in batch
                ],
            }
        batch_results = browser_result.get("results", [])
        batch_failures = browser_result.get("failures", [])
        persist_results(args.database, batch_results, batch_failures)
        results.extend(batch_results)
        failures.extend(batch_failures)
        print(
            f"    committed {len(batch_results):,}; retryable failures {len(batch_failures):,}",
            flush=True,
        )
    with sqlite3.connect(args.database) as connection:
        total, links, counts = connection.execute(
            "SELECT COUNT(*), SUM(apple_music_url IS NOT NULL AND spotify_url IS NOT NULL), "
            "SUM(stream_count IS NOT NULL) FROM songs WHERE enabled = 1"
        ).fetchone()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": SPOTIFY_SOURCE,
        "summary": {
            "enabled_songs": int(total),
            "fresh_rows_skipped": skipped,
            "queued": len(jobs),
            "completed_this_run": len(results),
            "failed_this_run": len(failures),
            "songs_with_both_links": int(links or 0),
            "songs_with_stream_counts": int(counts or 0),
        },
        "failure_statuses": dict(
            sorted(Counter(str(item.get("status", "unknown")) for item in failures).items())
        ),
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2), flush=True)
    print(f"Report: {args.report}", flush=True)
    return report


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
