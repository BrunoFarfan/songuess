from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from dataset import clients
from dataset.clients import fetch_musicbrainz_spotify_urls
from dataset.populate import (
    Progress,
    backfill_catalog_artists,
    backfill_catalog_countries,
    backfill_catalog_explicit_versions,
    backfill_catalog_streaming_links,
    catalog_state,
    import_until_target,
    initialize_database,
    known_catalog_identities,
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
            "trackViewUrl": f"https://music.apple.com/us/album/example/10?i={apple_id}",
            "primaryGenreName": "Pop",
        },
        "rank": apple_id,
    }


def _recording(*credits: tuple[str, str, str, str]) -> dict[str, Any]:
    return {
        "artist-credit": [
            {
                "name": credited_name,
                "joinphrase": join_phrase,
                "artist": {
                    "id": artist_mbid,
                    "name": canonical_name,
                    "sort-name": canonical_name,
                },
            }
            for artist_mbid, canonical_name, credited_name, join_phrase in credits
        ]
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


def test_catalog_state_keeps_disabled_identities_out_of_future_population(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO songs (title, artist, release_year, musicbrainz_id, "
            "apple_track_id, preview_url, enabled) VALUES "
            "('Unavailable', 'Artist', 2000, 'disabled-mbid', 'disabled-apple', "
            "'https://preview', 0)"
        )

    state = catalog_state(database)
    known_mbids, known_apple_ids = known_catalog_identities(database)

    assert state.enabled_count == 0
    assert state.musicbrainz_ids == set()
    assert state.apple_track_ids == set()
    assert known_mbids == {"disabled-mbid"}
    assert known_apple_ids == {"disabled-apple"}


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

    with sqlite3.connect(database) as connection:
        popularity = connection.execute(
            "SELECT popularity_score, stream_count, stream_count_status FROM songs ORDER BY id"
        ).fetchall()
        evidence = connection.execute(
            "SELECT g.name, e.rank, e.score, e.source FROM song_genre_evidence e "
            "JOIN genres g ON g.id = e.genre_id ORDER BY e.song_id, e.rank"
        ).fetchall()
    assert popularity == [
        (None, None, "missing_link"),
        (None, None, "missing_link"),
    ]
    assert evidence == [
        ("pop", 1, 100, "apple"),
        ("pop", 1, 100, "apple"),
    ]


def test_streaming_link_backfill_uses_exact_cached_apple_and_musicbrainz_relations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    cache = tmp_path / "cache"
    initialize_database(database)
    write_catalog(database, [_match("mbid-a", 1), _match("mbid-b", 2)], {})
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE songs SET apple_music_url = NULL, spotify_url = NULL")
    (cache / "apple").mkdir(parents=True)
    for mbid, apple_id in (("mbid-a", 1), ("mbid-b", 2)):
        (cache / "apple" / f"{mbid}.json").write_text(
            json.dumps(
                {
                    "trackId": apple_id,
                    "trackViewUrl": (
                        f"https://music.apple.com/us/album/exact-{mbid}/10?i={apple_id}"
                    ),
                }
            ),
            encoding="utf-8",
        )

    def exact_spotify_links(mbids: list[str]) -> dict[str, str | None]:
        assert mbids == ["mbid-a", "mbid-b"]
        return {
            "mbid-a": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b",
            "mbid-b": None,
        }

    summary = backfill_catalog_streaming_links(
        database,
        cache,
        spotify_fetcher=exact_spotify_links,
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT musicbrainz_id, apple_music_url, spotify_url FROM songs ORDER BY musicbrainz_id"
        ).fetchall()
    assert rows == [
        (
            "mbid-a",
            "https://music.apple.com/us/album/exact-mbid-a/10?i=1",
            "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b",
        ),
        ("mbid-b", "https://music.apple.com/us/album/exact-mbid-b/10?i=2", None),
    ]
    assert summary == {
        "songs": 2,
        "apple_music_links": 2,
        "spotify_relationships_checked": 2,
        "spotify_links_added": 1,
    }


def test_spotify_link_resolution_keeps_only_exact_track_relationships() -> None:
    requested: list[str] = []

    def fake_request(url: str) -> dict[str, Any]:
        requested.append(url)
        return {
            "relations": [
                {
                    "url": {
                        "resource": (
                            "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b?si=test"
                        )
                    }
                },
                {"url": {"resource": "https://open.spotify.com/artist/not-a-track"}},
                {"url": {"resource": "https://music.apple.com/us/album/example/1"}},
            ]
        }

    assert fetch_musicbrainz_spotify_urls(
        ["recording"], request_json=fake_request, sleeper=lambda _: None
    ) == {"recording": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"}
    assert requested == ["https://musicbrainz.org/ws/2/recording/recording?inc=url-rels&fmt=json"]


def test_apple_selection_prefers_explicit_only_among_equivalent_matches() -> None:
    def track(track_id: int, title: str, explicitness: str) -> dict[str, Any]:
        return {
            "trackId": track_id,
            "trackName": title,
            "artistName": "Kendrick Lamar",
            "trackExplicitness": explicitness,
            "previewUrl": f"https://preview/{track_id}",
            "releaseDate": "2024-05-04T00:00:00Z",
        }

    selected = clients._select_apple_track(
        [
            track(1, "Not Like Us", "cleaned"),
            track(2, "Not Like Us", "explicit"),
            track(3, "Not Like Us (Karaoke Version)", "explicit"),
        ],
        title="Not Like Us",
        artist="Kendrick Lamar",
        canonical_year=2024,
        year_min=1950,
        year_max=2026,
    )

    assert selected is not None
    assert selected["trackId"] == 2


def test_apple_clean_track_resolves_structured_explicit_alternate() -> None:
    clean = {
        "trackId": 10,
        "trackName": "Not Like Us",
        "artistName": "Kendrick Lamar",
        "trackExplicitness": "cleaned",
        "trackTimeMillis": 274_192,
        "releaseDate": "2024-05-04T00:00:00Z",
        "trackViewUrl": "https://music.apple.com/us/album/not-like-us/10?i=11",
    }
    page = (
        '<a aria-label="Explicit, Not Like Us - Single, 1 song" '
        'href="https://music.apple.com/us/album/not-like-us-single/20">Explicit</a>'
    )
    explicit = {
        **clean,
        "trackId": 21,
        "trackExplicitness": "explicit",
        "previewUrl": "https://preview/explicit",
        "trackViewUrl": "https://music.apple.com/us/album/not-like-us/20?i=21",
    }

    selected = clients.find_explicit_apple_equivalent(
        clean,
        country="US",
        request_text=lambda _url: page,
        request_json=lambda _url: {"results": [explicit]},
    )

    assert selected is not None
    assert selected["trackId"] == 21


def test_cached_clean_apple_match_is_upgraded_before_population(tmp_path: Path) -> None:
    cache_path = tmp_path / "apple-v2" / "US" / "recording.json"
    cache_path.parent.mkdir(parents=True)
    clean = {
        "trackId": 10,
        "trackName": "Song",
        "artistName": "Artist",
        "trackExplicitness": "cleaned",
        "releaseDate": "2024-01-01T00:00:00Z",
        "previewUrl": "https://preview/clean",
    }
    explicit = {
        **clean,
        "trackId": 20,
        "trackExplicitness": "explicit",
        "previewUrl": "https://preview/explicit",
    }
    cache_path.write_text(
        json.dumps(
            {
                "version": 2,
                "status": "matched",
                "checked_at": 1,
                "track": clean,
            }
        ),
        encoding="utf-8",
    )

    selected = clients.search_apple_track(
        tmp_path,
        {"recording_mbid": "recording"},
        {"title": "Song", "first-release-date": "2024"},
        country="US",
        year_min=1950,
        year_max=2026,
        now=2,
        explicit_equivalent_fetcher=lambda *_args, **_kwargs: explicit,
    )

    assert selected is not None
    assert selected["trackId"] == 20
    assert clients.read_cached_apple_track(tmp_path, "recording")["trackId"] == 20


def test_explicit_backfill_replaces_clean_provider_fields_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "catalog.sqlite3"
    cache = tmp_path / "cache"
    initialize_database(database)
    clean_match = _match("recording", 10)
    clean_match["apple"]["trackExplicitness"] = "cleaned"
    write_catalog(database, [clean_match], {})
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE songs SET apple_explicitness_checked_at = NULL")
    explicit = {
        **clean_match["apple"],
        "trackId": 20,
        "trackExplicitness": "explicit",
        "previewUrl": "https://preview/explicit",
        "trackViewUrl": "https://music.apple.com/us/album/explicit/20?i=20",
    }
    monkeypatch.setattr(
        "dataset.populate.fetch_apple_tracks_by_ids",
        lambda *_args, **_kwargs: {"10": clean_match["apple"]},
    )
    monkeypatch.setattr(
        "dataset.populate.find_explicit_apple_equivalents",
        lambda *_args, **_kwargs: ({1: explicit}, {1}),
    )
    monkeypatch.setattr(
        "dataset.populate.validate_previews",
        lambda *_args, **_kwargs: {"https://preview/explicit": "valid"},
    )

    first = backfill_catalog_explicit_versions(database, cache)
    second = backfill_catalog_explicit_versions(database, cache)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT apple_track_id, apple_explicitness, preview_url FROM songs"
        ).fetchone()
    assert row == ("20", "explicit", "https://preview/explicit")
    assert first["upgraded_to_explicit"] == 1
    assert second["checked"] == 0


def test_reimport_replaces_structured_artist_relationships_idempotently(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    match = _match("recording", 1)
    match["apple"]["artistName"] = "The Weeknd & Anitta"
    initial_metadata = {
        "recording": _recording(
            ("weeknd", "The Weeknd", "The Weeknd", " & "),
            ("anitta", "Anitta", "Anitta", ""),
        )
    }

    assert write_catalog(database, [match], initial_metadata) == 1
    assert write_catalog(database, [match], initial_metadata) == 0

    updated_metadata = {
        "recording": _recording(
            ("weeknd", "The Weeknd", "The Weeknd", " feat. "),
            ("gesaffelstein", "Gesaffelstein", "Gesaffelstein", ""),
        )
    }
    assert write_catalog(database, [match], updated_metadata) == 0

    with sqlite3.connect(database) as connection:
        display_artist = connection.execute(
            "SELECT artist FROM songs WHERE musicbrainz_id = 'recording'"
        ).fetchone()[0]
        credits = connection.execute(
            "SELECT a.musicbrainz_id, sa.credit_order, sa.credited_name, sa.join_phrase "
            "FROM song_artists sa JOIN artists a ON a.id = sa.artist_id "
            "ORDER BY sa.credit_order"
        ).fetchall()
        relationship_count = connection.execute("SELECT COUNT(*) FROM song_artists").fetchone()[0]

    assert display_artist == "The Weeknd & Anitta"
    assert credits == [
        ("weeknd", 0, "The Weeknd", " feat. "),
        ("gesaffelstein", 1, "Gesaffelstein", ""),
    ]
    assert relationship_count == 2


def test_artist_backfill_is_resumable_idempotent_and_reports_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "catalog.sqlite3"
    cache = tmp_path / "cache"
    initialize_database(database)
    write_catalog(database, [_match("resolved", 1), _match("unresolved", 2)], {})
    metadata_calls: list[list[dict[str, Any]]] = []

    def fake_metadata(cache_dir: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        metadata_calls.append(candidates)
        return {
            "resolved": _recording(("artist", "Artist", "Artist", "")),
            "unresolved": {},
        }

    monkeypatch.setattr("dataset.populate.fetch_musicbrainz_metadata", fake_metadata)

    backfill_catalog_artists(database, cache, batch_size=2)
    backfill_catalog_artists(database, cache, batch_size=2)

    with sqlite3.connect(database) as connection:
        artists = connection.execute(
            "SELECT musicbrainz_id, name FROM artists ORDER BY musicbrainz_id"
        ).fetchall()
        relationships = connection.execute("SELECT COUNT(*) FROM song_artists").fetchone()[0]
    output = capsys.readouterr().out

    assert artists == [("artist", "Artist")]
    assert relationships == 1
    assert len(metadata_calls) == 2
    assert metadata_calls[0] == [
        {"recording_mbid": "resolved"},
        {"recording_mbid": "unresolved"},
    ]
    assert metadata_calls[1] == [{"recording_mbid": "unresolved"}]
    assert "song_id=2 recording_mbid=unresolved" in output


def test_listenbrainz_cache_paths_include_discovery_parameters(tmp_path: Path) -> None:
    assert clients.listenbrainz_top_artists_cache_path(
        tmp_path, 200
    ) != clients.listenbrainz_top_artists_cache_path(tmp_path, 1000)
    assert clients.listenbrainz_radio_cache_path(
        tmp_path, "artist", 40
    ) != clients.listenbrainz_radio_cache_path(tmp_path, "artist", 60)
    assert clients.listenbrainz_radio_cache_path(
        tmp_path, "artist", 60, similar_artists=0
    ) != clients.listenbrainz_radio_cache_path(tmp_path, "artist", 60, similar_artists=1)
    assert "count-1000" in clients.listenbrainz_top_artists_cache_path(tmp_path, 1000).name
    assert "r60" in str(clients.listenbrainz_radio_cache_path(tmp_path, "artist", 60))


def test_discovery_identity_merge_never_copies_popularity_counts() -> None:
    candidates: dict[str, dict[str, Any]] = {}

    clients._merge_candidate_identities(
        candidates,
        [
            {
                "recording_mbid": "recording",
                "track_name": "Song",
                "listen_count": 123,
                "total_listen_count": 456,
                "total_user_count": 78,
            }
        ],
        source="sitewide_all_time",
    )
    clients._merge_candidate_identities(
        candidates,
        [{"recording_mbid": "recording", "total_listen_count": 999}],
        source="artist_top_recordings",
    )

    assert candidates == {
        "recording": {
            "recording_mbid": "recording",
            "track_name": "Song",
            "discovery_sources": ["sitewide_all_time", "artist_top_recordings"],
        }
    }


def test_primary_discovery_overflow_uses_artist_top_recordings_not_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "configured-for-test")
    requested_urls: list[str] = []

    def fake_cached_json(path: Path, url: str) -> Any:
        requested_urls.append(url)
        if "/stats/sitewide/recordings" in url:
            return {
                "payload": {
                    "recordings": [
                        {
                            "recording_mbid": "seed",
                            "track_name": "Seed",
                            "listen_count": 999,
                        }
                    ]
                }
            }
        if "/stats/sitewide/artists" in url:
            return {"payload": {"artists": [{"artist_mbid": "artist", "artist_name": "A"}]}}
        if "/popularity/top-recordings-for-artist/" in url:
            return [
                {
                    "recording_mbid": "artist-song-1",
                    "recording_name": "One",
                    "total_listen_count": 500,
                    "total_user_count": 50,
                },
                {
                    "recording_mbid": "artist-song-2",
                    "recording_name": "Two",
                    "total_listen_count": 400,
                    "total_user_count": 40,
                },
            ]
        pytest.fail(f"unexpected discovery URL: {url}")

    monkeypatch.setattr(clients, "cached_json", fake_cached_json)
    candidates = clients.fetch_listenbrainz_candidates(
        tmp_path, 3, artist_count=1, recordings_per_artist=2
    )

    assert [candidate["recording_mbid"] for candidate in candidates] == [
        "seed",
        "artist-song-1",
        "artist-song-2",
    ]
    assert all("listen_count" not in candidate for candidate in candidates)
    assert all("total_listen_count" not in candidate for candidate in candidates)
    assert any("/popularity/top-recordings-for-artist/" in url for url in requested_urls)
    assert all("/lb-radio/" not in url for url in requested_urls)


def test_token_free_discovery_expands_requested_artists_through_public_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LISTENBRAINZ_TOKEN", raising=False)
    requested_urls: list[str] = []

    def fake_cached_json(path: Path, url: str) -> Any:
        requested_urls.append(url)
        if "/stats/sitewide/recordings" in url:
            return {"payload": {"recordings": [{"recording_mbid": "seed", "track_name": "Seed"}]}}
        if "/stats/sitewide/artists" in url:
            return {"payload": {"artists": [{"artist_mbid": "artist", "artist_name": "Artist"}]}}
        if "/lb-radio/artist/artist" in url:
            return {
                "artist": [
                    {
                        "recording_mbid": "radio-low",
                        "similar_artist_mbid": "artist",
                        "similar_artist_name": "Artist",
                        "total_listen_count": 10,
                    },
                    {
                        "recording_mbid": "radio-high",
                        "similar_artist_mbid": "artist",
                        "similar_artist_name": "Artist",
                        "total_listen_count": 100,
                    },
                ]
            }
        pytest.fail(f"unexpected discovery URL: {url}")

    monkeypatch.setattr(clients, "cached_json", fake_cached_json)
    candidates = clients.fetch_listenbrainz_candidates(
        tmp_path, 3, artist_count=1, recordings_per_artist=2
    )

    assert [candidate["recording_mbid"] for candidate in candidates] == [
        "seed",
        "radio-high",
        "radio-low",
    ]
    assert candidates[1]["artist_mbids"] == ["artist"]
    assert all("total_listen_count" not in candidate for candidate in candidates)
    assert any("max_similar_artists=0" in url for url in requested_urls)
    assert all("/popularity/top-recordings-for-artist/" not in url for url in requested_urls)


def test_listenbrainz_token_is_scoped_and_missing_token_skips_artist_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "secret-token")
    assert (
        clients._request_headers("https://api.listenbrainz.org/1/example")["Authorization"]
        == "Token secret-token"
    )
    assert "Authorization" not in clients._request_headers("https://musicbrainz.org/ws/2")

    monkeypatch.delenv("LISTENBRAINZ_TOKEN")

    def unauthorized(path: Path, url: str) -> Any:
        if "/stats/sitewide/artists" in url:
            return {"payload": {"artists": [{"artist_mbid": "artist", "artist_name": "Artist"}]}}
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(clients, "cached_json", unauthorized)
    assert (
        clients.fetch_listenbrainz_artist_top_recordings(
            tmp_path, set(), 1, artist_count=1, recordings_per_artist=1
        )
        == []
    )


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
            "SELECT title, artist, stream_count, popularity_score, enabled "
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
            "SELECT title, artist, stream_count, popularity_score, enabled "
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
