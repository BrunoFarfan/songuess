import sqlite3

from dataset.populate import initialize_database
from dataset.spotify_streams_browser import (
    BrowserJob,
    CatalogSong,
    build_jobs,
    fetch_catalog_spotify_urls,
    persist_results,
    playwright_function,
)


def _song(
    song_id: int,
    *,
    isrcs: tuple[str, ...] = (),
    spotify_url: str | None = None,
    stream_count: int | None = None,
    fetched_at: str | None = None,
    status: str = "pending",
) -> CatalogSong:
    return CatalogSong(
        id=song_id,
        musicbrainz_id=f"mbid-{song_id}",
        title=f"Song {song_id}",
        artist="Artist",
        album="Album",
        credited_artists=("Artist",),
        duration_ms=180_000,
        isrcs=isrcs,
        spotify_url=spotify_url,
        stream_count=stream_count,
        stream_count_fetched_at=fetched_at,
        stream_count_status=status,
        has_retryable_failure=False,
        failure_status=None,
        musicbrainz_relationship_checked_at=None,
        catalog_lookup_checked_at=None,
    )


def test_jobs_skip_fresh_complete_rows_and_prefer_structured_resolution() -> None:
    jobs, skipped = build_jobs(
        [
            _song(
                1,
                spotify_url="https://open.spotify.com/track/0000000000000000000001",
                stream_count=100,
                fetched_at="2999-01-01T00:00:00Z",
                status="complete",
            ),
            _song(2, isrcs=("USABC1234567",)),
            _song(3),
        ],
        refresh=False,
        retry_failures=False,
        stale_after_days=30,
        limit=None,
    )

    assert skipped == 1
    assert [job.match_method for job in jobs] == ["isrc", "exact_metadata"]
    assert "isrc%3AUSABC1234567" in jobs[0].search_urls[0]


def test_playwright_function_uses_web_hydration_without_replaying_tokens() -> None:
    function = playwright_function(
        [
            BrowserJob(
                song_id=1,
                title="Jigsaw Falling Into Place",
                artist="Radiohead",
                album="In Rainbows",
                credited_artists=("Radiohead",),
                duration_ms=248_000,
                match_method="existing_url",
                spotify_urls=("https://open.spotify.com/track/0YJ9FWWHn9EfnN0lHwbzvV",),
                search_urls=(),
            )
        ],
        workers=4,
        search_candidates=3,
    )

    assert "workerPage.goto(spotifyUrl" in function
    assert 'operationName === "getTrack"' in function
    assert "authorization" not in function.casefold()
    assert "client-token" not in function.casefold()


def test_catalog_fallback_requires_exact_structured_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        "dataset.spotify_streams_browser.read_json",
        lambda *_args, **_kwargs: {
            "content": [
                {
                    "trackTitle": "Song 1",
                    "artists": [{"name": "Wrong Artist"}],
                    "durationMs": 180_000,
                    "isrc": "WRONG",
                    "href": "https://open.spotify.com/track/0000000000000000000001",
                },
                {
                    "trackTitle": "Song 1",
                    "artists": [{"name": "Artist"}],
                    "durationMs": 180_000,
                    "isrc": "USABC1234567",
                    "href": "https://open.spotify.com/track/0000000000000000000002",
                },
            ]
        },
    )

    resolved, checked = fetch_catalog_spotify_urls([_song(1, isrcs=("USABC1234567",))], workers=1)

    assert resolved == {1: "https://open.spotify.com/track/0000000000000000000002"}
    assert checked == {1}


def test_persist_results_updates_canonical_count_and_catalog_percentiles(tmp_path) -> None:
    database = tmp_path / "songs.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        for song_id in (1, 2):
            connection.execute(
                "INSERT INTO songs "
                "(id, title, artist, release_year, musicbrainz_id, apple_track_id, "
                "preview_url, apple_music_url, enabled) "
                "VALUES (?, ?, 'Artist', 2000, ?, ?, 'https://preview', "
                "'https://music.apple.com/example', 1)",
                (song_id, f"Song {song_id}", f"mbid-{song_id}", f"apple-{song_id}"),
            )

    persist_results(
        database,
        [
            {
                "song_id": 1,
                "spotify_url": "https://open.spotify.com/track/0000000000000000000001",
                "stream_count": 100,
            },
            {
                "song_id": 2,
                "spotify_url": "https://open.spotify.com/track/0000000000000000000002",
                "stream_count": 200,
            },
        ],
        [],
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT stream_count, popularity_score, stream_count_source, "
            "stream_count_status FROM songs ORDER BY id"
        ).fetchall()
    assert rows == [
        (100, 0, "spotify_web_hydration", "complete"),
        (200, 100, "spotify_web_hydration", "complete"),
    ]
