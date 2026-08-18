from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from dataset import clients
from dataset.populate import (
    Progress,
    backfill_catalog_countries,
    catalog_state,
    import_until_target,
    initialize_database,
    target_gap,
    write_catalog,
)


def _match(mbid: str, apple_id: int, *, listen_count: int = 100) -> dict[str, Any]:
    return {
        "candidate": {"recording_mbid": mbid, "listen_count": listen_count},
        "apple": {
            "trackId": apple_id,
            "trackName": f"Track {mbid}",
            "artistName": "Artist",
            "collectionName": "Album",
            "canonicalReleaseYear": 2000,
            "previewUrl": f"https://audio.example/{apple_id}.m4a",
            "primaryGenreName": "Pop",
        },
        "rank": apple_id,
    }


def test_target_total_adds_only_the_gap_and_preserves_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "catalog.sqlite3"
    cache = tmp_path / "cache"
    initialize_database(database)
    write_catalog(database, [_match("existing", 1)], {})
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT id, title FROM songs WHERE musicbrainz_id = 'existing'"
        ).fetchone()

    candidates = [
        {"recording_mbid": f"new-{index}", "listen_count": 100 - index} for index in range(5)
    ]

    def fake_search(
        cache_dir: Path,
        candidate: dict[str, Any],
        metadata: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        index = int(candidate["recording_mbid"].split("-")[1])
        return _match(candidate["recording_mbid"], index + 2)["apple"]

    monkeypatch.setattr("dataset.populate.search_apple_track", fake_search)
    monkeypatch.setattr(
        "dataset.populate.validate_previews",
        lambda cache_dir, urls, **kwargs: {url: "valid" for url in urls},
    )
    progress = Progress(existing_songs=1, eligible_songs=len(candidates))
    assert target_gap(database, 3) == 2
    import_until_target(
        database,
        cache,
        candidates,
        {},
        {},
        country="US",
        year_min=1950,
        year_max=2026,
        target_total=3,
        preview_workers=2,
        batch_size=2,
        progress=progress,
    )

    assert catalog_state(database).enabled_count == 3
    assert progress.new_validated_songs == 2
    assert progress.candidates_checked == 2
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT id, title FROM songs WHERE musicbrainz_id = 'existing'"
        ).fetchone()
    assert after == before


def test_write_catalog_deduplicates_musicbrainz_and_apple_ids(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    songs = [
        _match("mbid-a", 1),
        _match("mbid-a", 1, listen_count=90),
        _match("mbid-b", 1),
        _match("mbid-c", 3),
    ]

    assert write_catalog(database, songs, {}) == 2
    state = catalog_state(database)
    assert state.enabled_count == 2
    assert state.musicbrainz_ids == {"mbid-a", "mbid-c"}
    assert state.apple_track_ids == {"1", "3"}
    assert write_catalog(database, songs, {}) == 0
    assert catalog_state(database).enabled_count == 2


def test_listenbrainz_cache_paths_include_discovery_parameters(tmp_path: Path) -> None:
    assert clients.listenbrainz_top_artists_cache_path(
        tmp_path, 200
    ) != clients.listenbrainz_top_artists_cache_path(tmp_path, 1000)
    assert clients.listenbrainz_radio_cache_path(
        tmp_path, "artist", 40
    ) != clients.listenbrainz_radio_cache_path(tmp_path, "artist", 60)
    assert "count-1000" in clients.listenbrainz_top_artists_cache_path(tmp_path, 1000).name
    assert "r60" in str(clients.listenbrainz_radio_cache_path(tmp_path, "artist", 60))


def test_musicbrainz_sqlite_cache_resumes_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = [{"recording_mbid": "a"}, {"recording_mbid": "b"}]
    calls: list[str] = []

    def interrupted_request(url: str) -> dict[str, Any]:
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return {"recordings": [{"id": "a", "title": "A"}]}

    monkeypatch.setattr(clients.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="interrupted"):
        clients.fetch_musicbrainz_metadata(
            tmp_path, candidates, batch_size=1, request_json=interrupted_request
        )

    resumed_calls: list[str] = []

    def resumed_request(url: str) -> dict[str, Any]:
        resumed_calls.append(url)
        return {"recordings": [{"id": "b", "title": "B"}]}

    metadata = clients.fetch_musicbrainz_metadata(
        tmp_path, candidates, batch_size=1, request_json=resumed_request
    )
    assert metadata["a"]["title"] == "A"
    assert metadata["b"]["title"] == "B"
    assert len(resumed_calls) == 1
    assert "rid%3A%28b%29" in resumed_calls[0]


def test_transient_preview_validation_is_cached_then_retryable(tmp_path: Path) -> None:
    url = "https://audio.example/preview.m4a"
    checks: list[str] = []

    def transient_checker(value: str) -> tuple[str, str | None]:
        checks.append(value)
        return "transient", "timeout"

    first = clients.validate_previews(
        tmp_path,
        [url],
        now=100,
        transient_ttl_seconds=300,
        checker=transient_checker,
    )
    cached = clients.validate_previews(
        tmp_path,
        [url],
        now=200,
        transient_ttl_seconds=300,
        checker=lambda value: pytest.fail("transient cache should still be fresh"),
    )
    retried = clients.validate_previews(
        tmp_path,
        [url],
        now=401,
        transient_ttl_seconds=300,
        checker=lambda value: ("valid", None),
    )

    assert first[url] == "transient"
    assert cached[url] == "transient"
    assert retried[url] == "valid"
    assert checks == [url]


def test_negative_apple_match_cache_expires(tmp_path: Path) -> None:
    path = tmp_path / "negative.json"
    clients._write_apple_match_cache(path, None, checked_at=100)
    assert clients._read_apple_match_cache(path, 300, now=200) == (True, None, False)
    assert clients._read_apple_match_cache(path, 300, now=401) == (False, None, True)


def test_musicbrainz_artist_countries_are_multi_value_explicit_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recordings = {
        "recording": {
            "artist-credit": [
                {"artist": {"id": "artist-a"}},
                {"artist": {"id": "artist-b"}},
                {"artist": {"id": "artist-c"}},
            ]
        }
    }
    calls: list[str] = []

    def interrupted_request(url: str) -> dict[str, Any]:
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return {"artists": [{"id": "artist-a", "country": "US"}]}

    monkeypatch.setattr(clients.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="interrupted"):
        clients.fetch_musicbrainz_artist_countries(
            tmp_path,
            recordings,
            batch_size=1,
            request_json=interrupted_request,
        )

    resumed_calls: list[str] = []

    def resumed_request(url: str) -> dict[str, Any]:
        resumed_calls.append(url)
        if "artist-b" in url:
            return {"artists": [{"id": "artist-b", "country": "GB"}]}
        return {"artists": [{"id": "artist-c"}]}

    countries = clients.fetch_musicbrainz_artist_countries(
        tmp_path,
        recordings,
        batch_size=1,
        request_json=resumed_request,
    )
    assert countries == {"recording": ["GB", "US"]}
    assert len(resumed_calls) == 2
    assert all("artist-a" not in url for url in resumed_calls)


def test_write_catalog_normalizes_multiple_countries_and_never_infers_missing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    songs = [_match("multi", 1), _match("unknown", 2)]

    write_catalog(
        database,
        songs,
        {},
        countries_by_recording={"multi": ["us", "GB", "US"], "unknown": []},
    )
    write_catalog(
        database,
        songs,
        {},
        countries_by_recording={"multi": ["GB", "US"], "unknown": []},
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT s.musicbrainz_id, c.code FROM songs s "
            "LEFT JOIN song_countries sc ON sc.song_id = s.id "
            "LEFT JOIN countries c ON c.id = sc.country_id "
            "ORDER BY s.musicbrainz_id, c.code"
        ).fetchall()
    assert rows == [("multi", "GB"), ("multi", "US"), ("unknown", None)]


def test_country_backfill_is_idempotent_and_preserves_song_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    write_catalog(database, [_match("existing", 1, listen_count=987)], {})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE songs SET popularity_score = 73 WHERE musicbrainz_id = 'existing'"
        )
        before = connection.execute(
            "SELECT title, artist, listen_count, popularity_score, enabled "
            "FROM songs WHERE musicbrainz_id = 'existing'"
        ).fetchone()

    metadata_calls: list[list[dict[str, Any]]] = []
    country_calls: list[dict[str, dict[str, Any]]] = []

    def fake_metadata(cache_dir: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        metadata_calls.append(candidates)
        return {"existing": {"artist-credit": [{"artist": {"id": "artist"}}]}}

    def fake_countries(cache_dir: Path, metadata: dict[str, Any]) -> dict[str, list[str]]:
        country_calls.append(metadata)
        return {"existing": ["CL"]}

    monkeypatch.setattr("dataset.populate.fetch_musicbrainz_metadata", fake_metadata)
    monkeypatch.setattr("dataset.populate.fetch_musicbrainz_artist_countries", fake_countries)

    backfill_catalog_countries(database, tmp_path / "cache", batch_size=1)
    backfill_catalog_countries(database, tmp_path / "cache", batch_size=1)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT title, artist, listen_count, popularity_score, enabled "
            "FROM songs WHERE musicbrainz_id = 'existing'"
        ).fetchone()
        countries = connection.execute(
            "SELECT c.code FROM countries c "
            "JOIN song_countries sc ON sc.country_id = c.id "
            "JOIN songs s ON s.id = sc.song_id WHERE s.musicbrainz_id = 'existing'"
        ).fetchall()
    assert after == before
    assert countries == [("CL",)]
    assert len(metadata_calls) == 1
    assert len(country_calls) == 1
