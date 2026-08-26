"""Development-only benchmark for Spotify stream counts exposed in public HTML.

The benchmark deliberately does not cache response bodies, headers, cookies, Spotify IDs, or
MusicBrainz relationship payloads. Its report stores only the stream count tied to an existing
Songuess song ID, plus aggregate operational statuses needed to evaluate the run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html.parser
import json
import random
import re
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPOSITORY_DIR / "backend" / "data" / "songuess.sqlite3"
DEFAULT_REPORT = REPOSITORY_DIR / "dataset" / "cache" / "spotify-streams-mvp.json"
USER_AGENT = "Songuess/0.1 (development popularity benchmark)"
SPOTIFY_TRACK_PATTERN = re.compile(r"^https://open\.spotify\.com/track/([A-Za-z0-9]{22})")
COUNT_PATTERN = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})+$")


@dataclass(frozen=True)
class Song:
    id: int
    title: str
    artist: str
    musicbrainz_id: str
    popularity_score: int | None


@dataclass(frozen=True)
class SpotifyFetchResult:
    song_id: int
    status: str
    stream_count: int | None = None
    elapsed_seconds: float = 0.0


class TrackAnchorParser(html.parser.HTMLParser):
    """Read the title and count belonging to one exact Spotify track link."""

    def __init__(self, track_id: str) -> None:
        super().__init__()
        self.target_href = f"/track/{track_id}"
        self.anchor_depth = 0
        self.text_parts: list[str] = []
        self.matches: list[tuple[str, int]] = []
        self.page_title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.page_title = attributes.get("content")
        if self.anchor_depth:
            self.anchor_depth += 1
            return
        if tag == "a" and attributes.get("href") == self.target_href:
            self.anchor_depth = 1
            self.text_parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self.anchor_depth:
            return
        self.anchor_depth -= 1
        if self.anchor_depth:
            return
        values = [part.strip() for part in self.text_parts if part.strip()]
        for index, value in enumerate(values):
            if COUNT_PATTERN.fullmatch(value):
                title = " ".join(values[:index]).strip()
                self.matches.append((title, int(value.replace(",", ""))))
                break

    def handle_data(self, data: str) -> None:
        if self.anchor_depth:
            self.text_parts.append(data)


class RequestRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.minimum_interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_request_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_request_at - now)
            self.next_request_at = max(now, self.next_request_at) + self.minimum_interval
        if delay:
            time.sleep(delay)


class BlockingGuard:
    def __init__(self, maximum_block_responses: int) -> None:
        self.maximum_block_responses = maximum_block_responses
        self.block_responses = 0
        self.event = threading.Event()
        self.lock = threading.Lock()

    def record(self, status_code: int) -> None:
        if status_code not in {403, 429}:
            return
        with self.lock:
            self.block_responses += 1
            if self.block_responses >= self.maximum_block_responses:
                self.event.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260822)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--requests-per-second", type=float, default=4.0)
    parser.add_argument("--spotify-candidates-per-song", type=int, default=3)
    parser.add_argument("--musicbrainz-workers", type=int, default=4)
    parser.add_argument("--musicbrainz-delay", type=float, default=1.05)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--maximum-block-responses", type=int, default=5)
    return parser.parse_args()


def load_representative_sample(database: Path, sample_size: int, seed: int) -> list[Song]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, title, artist, musicbrainz_id, popularity_score "
            "FROM songs WHERE enabled = 1 AND musicbrainz_id IS NOT NULL "
            "ORDER BY COALESCE(popularity_score, -1), id"
        ).fetchall()
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    if not rows:
        raise ValueError("no enabled songs with MusicBrainz identities")

    target = min(sample_size, len(rows))
    selected: list[Song] = []
    for index in range(target):
        position = min(len(rows) - 1, int((index + 0.5) * len(rows) / target))
        row = rows[position]
        selected.append(
            Song(
                id=int(row[0]),
                title=str(row[1]),
                artist=str(row[2]),
                musicbrainz_id=str(row[3]),
                popularity_score=int(row[4]) if row[4] is not None else None,
            )
        )
    random.Random(seed).shuffle(selected)
    return selected


def extract_spotify_track_ids(payload: dict[str, Any]) -> list[str]:
    track_ids: list[str] = []
    for relation in payload.get("relations", []):
        resource = (relation.get("url") or {}).get("resource")
        if not isinstance(resource, str):
            continue
        match = SPOTIFY_TRACK_PATTERN.match(resource)
        if match and match.group(1) not in track_ids:
            track_ids.append(match.group(1))
    return track_ids


def resolve_spotify_track_ids(
    song: Song,
    *,
    timeout: float,
    rate_limiter: RequestRateLimiter | None = None,
    attempts: int = 3,
) -> tuple[list[str], str]:
    query = urllib.parse.urlencode({"inc": "url-rels+isrcs", "fmt": "json"})
    url = f"https://musicbrainz.org/ws/2/recording/{song.musicbrainz_id}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        if rate_limiter is not None:
            rate_limiter.wait()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            track_ids = extract_spotify_track_ids(payload)
            return track_ids, "resolved" if track_ids else "no_spotify_relationship"
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                return [], f"musicbrainz_http_{error.code}"
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            if attempt == attempts - 1:
                return [], "musicbrainz_transient_error"
        time.sleep(2**attempt)
    return [], "musicbrainz_error"


def normalize_title(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def parse_spotify_stream_count(
    body: str, track_id: str, expected_title: str
) -> tuple[int | None, str]:
    parser = TrackAnchorParser(track_id)
    parser.feed(body)
    expected = normalize_title(expected_title)
    page_title = normalize_title(parser.page_title or "")
    if page_title and not (
        page_title == expected or page_title.startswith(expected) or expected.startswith(page_title)
    ):
        return None, "track_title_mismatch"
    for returned_title, stream_count in parser.matches:
        returned = normalize_title(returned_title)
        if returned == expected or returned.startswith(expected) or expected.startswith(returned):
            return stream_count, "matched"
    # A missing value here only means this response did not expose the count in
    # the exact track markup we can validate. The interactive Spotify page may
    # still hydrate and display it client-side.
    return None, "stream_count_not_extracted"


def fetch_spotify_stream_count(
    song: Song,
    track_id: str,
    *,
    timeout: float,
    rate_limiter: RequestRateLimiter,
    blocking_guard: BlockingGuard,
) -> SpotifyFetchResult:
    if blocking_guard.event.is_set():
        return SpotifyFetchResult(song.id, "aborted_after_blocking")
    rate_limiter.wait()
    started = time.monotonic()
    request = urllib.request.Request(
        f"https://open.spotify.com/track/{track_id}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        stream_count, status = parse_spotify_stream_count(body, track_id, song.title)
        return SpotifyFetchResult(song.id, status, stream_count, time.monotonic() - started)
    except urllib.error.HTTPError as error:
        blocking_guard.record(error.code)
        return SpotifyFetchResult(
            song.id,
            f"spotify_http_{error.code}",
            elapsed_seconds=time.monotonic() - started,
        )
    except (TimeoutError, urllib.error.URLError):
        return SpotifyFetchResult(
            song.id,
            "spotify_transient_error",
            elapsed_seconds=time.monotonic() - started,
        )


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    position = round((len(values) - 1) * fraction)
    return sorted(values)[position]


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.workers <= 0 or args.musicbrainz_workers <= 0 or args.requests_per_second <= 0:
        raise ValueError("worker counts and requests_per_second must be positive")
    songs = load_representative_sample(args.database, args.sample_size, args.seed)
    started = time.monotonic()
    resolution_statuses: Counter[str] = Counter()
    candidates: list[tuple[Song, str]] = []

    print(f"Resolving Spotify relationships for {len(songs):,} representative songs...")
    musicbrainz_limiter = RequestRateLimiter(1.0 / args.musicbrainz_delay)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.musicbrainz_workers) as executor:
        future_songs = {
            executor.submit(
                resolve_spotify_track_ids,
                song,
                timeout=args.timeout,
                rate_limiter=musicbrainz_limiter,
            ): song
            for song in songs
        }
        for index, future in enumerate(concurrent.futures.as_completed(future_songs), start=1):
            song = future_songs[future]
            track_ids, status = future.result()
            resolution_statuses[status] += 1
            for track_id in track_ids[: args.spotify_candidates_per_song]:
                candidates.append((song, track_id))
            if index % 25 == 0 or index == len(songs):
                print(
                    f"  MusicBrainz {index:,}/{len(songs):,}; "
                    f"songs with Spotify relationships: {resolution_statuses['resolved']:,}",
                    flush=True,
                )

    print(
        f"Fetching {len(candidates):,} Spotify candidate pages with {args.workers} workers "
        f"at {args.requests_per_second:g} requests/second..."
    )
    limiter = RequestRateLimiter(args.requests_per_second)
    guard = BlockingGuard(args.maximum_block_responses)
    fetch_statuses: Counter[str] = Counter()
    results_by_song: dict[int, list[int]] = defaultdict(list)
    request_times: list[float] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                fetch_spotify_stream_count,
                song,
                track_id,
                timeout=args.timeout,
                rate_limiter=limiter,
                blocking_guard=guard,
            )
            for song, track_id in candidates
        ]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            fetch_statuses[result.status] += 1
            request_times.append(result.elapsed_seconds)
            if result.stream_count is not None:
                results_by_song[result.song_id].append(result.stream_count)
            if index % 50 == 0 or index == len(futures):
                print(
                    f"  Spotify {index:,}/{len(futures):,}; matched pages: "
                    f"{fetch_statuses['matched']:,}; blocking responses: {guard.block_responses:,}",
                    flush=True,
                )

    final_counts = {song_id: max(counts) for song_id, counts in results_by_song.items()}
    stream_counts = list(final_counts.values())
    elapsed = time.monotonic() - started
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "sample_size": len(songs),
            "seed": args.seed,
            "workers": args.workers,
            "requests_per_second": args.requests_per_second,
            "spotify_candidates_per_song": args.spotify_candidates_per_song,
            "musicbrainz_workers": args.musicbrainz_workers,
            "musicbrainz_delay": args.musicbrainz_delay,
        },
        "summary": {
            "selected_songs": len(songs),
            "songs_with_spotify_relationships": resolution_statuses["resolved"],
            "spotify_candidate_requests": len(candidates),
            "songs_with_stream_counts": len(final_counts),
            "coverage_percent": round(100 * len(final_counts) / len(songs), 2),
            "blocking_responses": guard.block_responses,
            "stopped_for_blocking": guard.event.is_set(),
            "elapsed_seconds": round(elapsed, 2),
            "mean_spotify_request_seconds": (
                round(statistics.fmean(request_times), 3) if request_times else None
            ),
        },
        "resolution_statuses": dict(sorted(resolution_statuses.items())),
        "fetch_statuses": dict(sorted(fetch_statuses.items())),
        "stream_count_distribution": {
            "minimum": min(stream_counts) if stream_counts else None,
            "p25": percentile(stream_counts, 0.25),
            "median": percentile(stream_counts, 0.5),
            "p75": percentile(stream_counts, 0.75),
            "maximum": max(stream_counts) if stream_counts else None,
        },
        "results": [
            {"song_id": song_id, "stream_count": count}
            for song_id, count in sorted(final_counts.items())
        ],
        "unmatched": [
            {"song_id": song.id, "status": "no_validated_stream_count"}
            for song in sorted(songs, key=lambda item: item.id)
            if song.id not in final_counts
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    args = parse_args()
    report = run(args)
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
