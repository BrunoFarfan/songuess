import json
import sqlite3
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from dataset.artist_expansion import (
    ALGORITHM_VERSION,
    _persist_spotify_batch,
    _targeted_apple_match,
    canonical_artist_releases,
    checkpoint_saturated_artists,
    collaborative_release_group,
    enrich_accepted_apple,
    fetch_artist_releases,
    fetch_canonical_release,
    import_reusable_enrichment,
    initialize_checkpoint,
    prepare_artist_candidate_metadata,
    reconcile_baseline_exact_identities,
    reconcile_candidate_provider_owners,
    recording_version_decision,
    relevant_release_group,
    reset_retryable_apple_failures,
    run_bounded_pipeline,
    select_target_candidates,
    trim_catalog_artist_caps,
)


def test_apple_fallback_http_error_is_checkpointed_per_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.execute(
            "INSERT INTO candidates(recording_mbid,title,display_artist,stream_count,"
            "spotify_status,apple_status,version_status,accepted,updated_at) "
            "VALUES('recording','Song','Artist',300000000,'complete','pending',"
            "'eligible',1,'now')"
        )
    monkeypatch.setattr(
        "dataset.artist_expansion.fetch_musicbrainz_metadata",
        lambda *_args, **_kwargs: {"recording": {}},
    )
    monkeypatch.setattr(
        "dataset.artist_expansion.search_apple_track",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "dataset.artist_expansion._musicbrainz_apple_relationship_match",
        lambda *_args, **_kwargs: None,
    )

    def raise_not_found(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://itunes.apple.com/search", 404, "Not Found", {}, None)

    monkeypatch.setattr("dataset.artist_expansion._targeted_apple_match", raise_not_found)

    result = enrich_accepted_apple(checkpoint, tmp_path, limit=1)

    assert result == {"queued": 1, "complete": 0, "failed": 1}
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT apple_status FROM candidates WHERE recording_mbid='recording'"
        ).fetchone() == ("failure:lookup_failure:HTTPError",)


def test_bounded_pipeline_propagates_apple_worker_failure(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    attempts = 0

    def fail_apple(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("apple failed")

    monkeypatch.setattr("dataset.artist_expansion._ready_apple_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr("dataset.artist_expansion.enrich_accepted_apple", fail_apple)
    monkeypatch.setattr(
        "dataset.artist_expansion.scrape_ready_artists_parallel",
        lambda _args: {"inventory_complete": 0},
    )
    monkeypatch.setattr(
        "dataset.artist_expansion.reconcile_baseline_exact_identities", lambda _p: 0
    )
    monkeypatch.setattr(
        "dataset.artist_expansion.reconcile_candidate_provider_owners", lambda _p: {}
    )
    monkeypatch.setattr("dataset.artist_expansion.reconcile_isrc_duplicates", lambda *_args: {})
    monkeypatch.setattr(
        "dataset.artist_expansion.select_target_candidates",
        lambda *_args, **_kwargs: {"accepted": 0},
    )
    args = SimpleNamespace(
        target_total=10_000,
        checkpoint=checkpoint,
        cache=tmp_path,
        database=tmp_path / "catalog.sqlite3",
        trim_report=tmp_path / "trim.json",
        poll_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="Apple enrichment worker failed") as error:
        run_bounded_pipeline(args)

    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == "apple failed"
    assert attempts == 3


def test_metadata_preparation_trusts_canonical_release_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,updated_at) VALUES(?,?,?)",
            [
                ("studio", "Studio Song", "2026-01-01"),
                ("lb-only", "Chart Song", "2026-01-01"),
            ],
        )
        connection.executemany(
            "INSERT INTO candidate_sources(recording_mbid,source,source_artist_mbid,source_rank) "
            "VALUES(?,?,?,?)",
            [
                ("studio", "musicbrainz_studio_discography", "artist", 1),
                ("lb-only", "listenbrainz_top_recordings", "artist", 1),
            ],
        )
    requested: list[str] = []

    def fake_metadata(_cache, candidates):
        requested.extend(str(candidate["recording_mbid"]) for candidate in candidates)
        return {}

    monkeypatch.setattr("dataset.artist_expansion.fetch_musicbrainz_metadata", fake_metadata)

    result = prepare_artist_candidate_metadata(
        checkpoint,
        tmp_path,
        "artist",
        lb_priority_limit=100,
    )

    assert requested == ["lb-only"]
    assert result["trusted_studio"] == 1
    assert result["looked_up"] == 1
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT enrichment_status FROM candidates WHERE recording_mbid='studio'"
        ).fetchone() == ("musicbrainz_release_complete",)


def test_target_selection_defers_artist_cap_and_replaces_apple_failures(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.execute(
            "INSERT INTO baseline_songs(recording_mbid,song_id,title,display_artist,"
            "identity_json) VALUES('baseline',1,'Baseline','Artist','{}')"
        )
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,stream_count,spotify_status,"
            "apple_status,version_status,updated_at) VALUES(?,?,?,?,?,?,?)",
            [
                ("baseline", "Baseline", 1_000, "complete", "complete", "eligible", "now"),
                ("best", "Best", 900, "complete", "pending", "eligible", "now"),
                (
                    "apple-failure",
                    "Unavailable",
                    800,
                    "complete",
                    "failure:lookup_failure",
                    "eligible",
                    "now",
                ),
                ("replacement", "Replacement", 700, "complete", "pending", "eligible", "now"),
                ("outside", "Outside", 600, "complete", "pending", "eligible", "now"),
            ],
        )

    result = select_target_candidates(checkpoint, target_new=2)

    assert result["accepted"] == 2
    with sqlite3.connect(checkpoint) as connection:
        accepted = connection.execute(
            "SELECT recording_mbid FROM candidates WHERE accepted=1 ORDER BY stream_count DESC"
        ).fetchall()
    assert accepted == [("best",), ("replacement",)]


def test_artist_cap_trim_keeps_highest_streamed_credited_songs(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "catalog.sqlite3"
    checkpoint = tmp_path / "expansion.sqlite3"
    report = tmp_path / "trim.json"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE songs(id INTEGER PRIMARY KEY,musicbrainz_id TEXT,title TEXT,"
            "artist TEXT,stream_count INTEGER,popularity_score INTEGER,enabled INTEGER);"
            "CREATE TABLE artists(id INTEGER PRIMARY KEY,musicbrainz_id TEXT,name TEXT);"
            "CREATE TABLE song_artists(song_id INTEGER,artist_id INTEGER,credit_order INTEGER);"
        )
        connection.executemany(
            "INSERT INTO artists VALUES(?,?,?)",
            [(1, "artist-a", "Artist A"), (2, "artist-b", "Artist B")],
        )
        connection.executemany(
            "INSERT INTO songs VALUES(?,?,?,?,?,?,1)",
            [
                (1, "one", "One", "Artist A", 400, None),
                (2, "two", "Two", "Artist A", 300, None),
                (3, "three", "Three", "Artist A", 200, None),
                (4, "four", "Four", "Artist B", 100, None),
            ],
        )
        connection.executemany(
            "INSERT INTO song_artists VALUES(?,?,?)",
            [(1, 1, 0), (2, 1, 0), (3, 1, 0), (4, 2, 0)],
        )
    monkeypatch.setattr("dataset.artist_expansion.ensure_baseline", lambda *_args: {})

    result = trim_catalog_artist_caps(
        database,
        checkpoint,
        report,
        artist_cap=2,
    )

    assert result["disabled_count"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id FROM songs WHERE enabled=1 ORDER BY id"
        ).fetchall() == [(1,), (2,), (4,)]


def test_reusable_enrichment_skips_provider_id_owned_by_another_candidate(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,spotify_track_id,spotify_status,"
            "updated_at) VALUES(?,?,?,?,?)",
            [
                ("owner", "Owner", "shared-track", "complete", "2026-01-01"),
                ("fragment", "Fragment", None, "pending", "2026-01-01"),
                ("new", "New", None, "pending", "2026-01-01"),
            ],
        )
    with sqlite3.connect(archive) as connection:
        connection.execute(
            "CREATE TABLE candidates(recording_mbid TEXT,status TEXT,spotify_url TEXT,"
            "stream_count INTEGER,attempted_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO candidates VALUES(?,?,?,?,?)",
            [
                (
                    "fragment",
                    "complete",
                    "https://open.spotify.com/track/shared-track",
                    300_000_000,
                    "2026-01-01",
                ),
                (
                    "new",
                    "complete",
                    "https://open.spotify.com/track/new-track",
                    400_000_000,
                    "2026-01-01",
                ),
            ],
        )

    assert import_reusable_enrichment(checkpoint, archive) == 1
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT spotify_track_id FROM candidates WHERE recording_mbid='fragment'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT spotify_track_id,stream_count FROM candidates WHERE recording_mbid='new'"
        ).fetchone() == ("new-track", 400_000_000)


def test_exact_baseline_identity_dedupes_fragment_without_fuzzy_title_matching(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    identity = {
        "credits": [{"artist_mbid": "artist"}],
    }
    with sqlite3.connect(checkpoint) as connection:
        connection.execute(
            "INSERT INTO baseline_songs(recording_mbid,song_id,title,display_artist,"
            "identity_json) VALUES(?,?,?,?,?)",
            ("baseline", 1, "Father Stretch My Hands, Pt. 1", "Artist", json.dumps(identity)),
        )
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,version_status,updated_at) "
            "VALUES(?,?,'eligible','2026-01-01')",
            [
                ("duplicate", "Father Stretch My Hands Pt 1"),
                ("different", "Father Stretch My Hands Pt 2"),
                ("different-primary", "Father Stretch My Hands Pt 1"),
            ],
        )
        connection.execute(
            "INSERT INTO candidates(recording_mbid,title,version_status,version_reason,"
            "decision_reason,updated_at) VALUES(?,?,'excluded',"
            "'duplicate_candidate_spotify_track','duplicate_candidate_spotify_track',?)",
            ("provider-duplicate", "Father Stretch My Hands, Pt. 1", "2026-01-01"),
        )
        connection.executemany(
            "INSERT INTO candidate_artists(recording_mbid,artist_mbid,credit_order) VALUES(?,?,?)",
            [
                ("duplicate", "artist", 0),
                ("duplicate", "featured-artist", 1),
                ("different", "artist", 0),
                ("different-primary", "other-artist", 0),
                ("different-primary", "artist", 1),
                ("provider-duplicate", "artist", 0),
            ],
        )

    assert reconcile_baseline_exact_identities(checkpoint) == 2
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT version_status,version_reason FROM candidates WHERE recording_mbid='duplicate'"
        ).fetchone() == ("excluded", "duplicate_baseline_exact_identity")
        assert connection.execute(
            "SELECT version_status FROM candidates WHERE recording_mbid='different'"
        ).fetchone() == ("eligible",)
        assert connection.execute(
            "SELECT version_status FROM candidates WHERE recording_mbid='different-primary'"
        ).fetchone() == ("eligible",)
        assert connection.execute(
            "SELECT version_reason FROM candidates WHERE recording_mbid='provider-duplicate'"
        ).fetchone() == ("duplicate_baseline_exact_identity",)


def test_retryable_apple_failures_reset_only_viable_candidates(tmp_path: Path) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,stream_count,spotify_status,"
            "apple_status,version_status,accepted,decision_reason,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "viable",
                    "Song",
                    300_000_000,
                    "complete",
                    "unavailable",
                    "eligible",
                    0,
                    "apple_match_unavailable",
                    "2026-01-01",
                ),
                (
                    "below",
                    "Song",
                    100_000_000,
                    "complete",
                    "unavailable",
                    "eligible",
                    0,
                    "apple_match_unavailable",
                    "2026-01-01",
                ),
            ],
        )

    assert reset_retryable_apple_failures(checkpoint) == 1
    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT apple_status,accepted,decision_reason FROM candidates "
            "WHERE recording_mbid='viable'"
        ).fetchone() == ("pending", None, None)
        assert connection.execute(
            "SELECT apple_status FROM candidates WHERE recording_mbid='below'"
        ).fetchone() == ("unavailable",)


def test_provider_reconciliation_prefers_structured_studio_candidate(tmp_path: Path) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    spotify_url = "https://open.spotify.com/track/0000000000000000000001"
    with sqlite3.connect(checkpoint) as connection:
        connection.executemany(
            "INSERT INTO candidates(recording_mbid,title,isrcs_json,spotify_url,"
            "spotify_track_id,stream_count,spotify_status,apple_music_url,apple_track_id,"
            "apple_payload_json,apple_status,version_status,version_reason,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "canonical",
                    "Song",
                    '["USABC1234567"]',
                    spotify_url,
                    None,
                    500,
                    "duplicate_candidate_spotify_track",
                    None,
                    None,
                    None,
                    "pending",
                    "eligible",
                    None,
                    "2026-01-01",
                ),
                (
                    "fragment",
                    "Song",
                    "[]",
                    spotify_url,
                    "0000000000000000000001",
                    500,
                    "complete",
                    "https://music.apple.com/us/song/1",
                    "1",
                    '{"trackId": 1}',
                    "complete",
                    "excluded",
                    "no_official_studio_release_evidence",
                    "2026-01-01",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO candidate_sources(recording_mbid,source,source_artist_mbid) VALUES(?,?,?)",
            [
                ("canonical", "musicbrainz_studio_discography", "artist"),
                ("fragment", "listenbrainz_top_recordings", "artist"),
            ],
        )

    assert reconcile_candidate_provider_owners(checkpoint) == {
        "spotify_owners_reassigned": 1,
        "duplicate_rows": 1,
        "apple_owners_transferred": 1,
    }

    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT spotify_track_id,spotify_status,apple_track_id,apple_status,version_status "
            "FROM candidates WHERE recording_mbid='canonical'"
        ).fetchone() == (
            "0000000000000000000001",
            "complete",
            "1",
            "complete",
            "eligible",
        )
        assert connection.execute(
            "SELECT spotify_track_id,spotify_status,apple_track_id,apple_status,version_status "
            "FROM candidates WHERE recording_mbid='fragment'"
        ).fetchone() == (
            None,
            "duplicate_candidate_spotify_track",
            None,
            "failure:duplicate_candidate_apple_track",
            "excluded",
        )


def test_saturated_artists_skip_discovery_and_provider_work(tmp_path: Path) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.executemany(
            "INSERT INTO artists(artist_mbid,name,baseline_count) VALUES(?,?,?)",
            [("full", "Full Artist", 30), ("open", "Open Artist", 29)],
        )

    assert checkpoint_saturated_artists(checkpoint) == 1
    assert checkpoint_saturated_artists(checkpoint) == 0

    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT eligibility_status,discovery_status,metadata_status,spotify_status "
            "FROM artists WHERE artist_mbid='full'"
        ).fetchone() == (
            "cap_already_reached",
            "complete",
            "skipped_cap",
            "skipped_cap",
        )
        assert connection.execute(
            "SELECT eligibility_status,discovery_status FROM artists WHERE artist_mbid='open'"
        ).fetchone() == ("eligible", "pending")


def test_checkpoint_is_versioned_and_idempotent(tmp_path: Path) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"

    initialize_checkpoint(checkpoint)
    initialize_checkpoint(checkpoint)

    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='algorithm_version'"
        ).fetchone() == (ALGORITHM_VERSION,)
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone() == (0,)


def test_artist_release_browse_is_paginated_and_cached(tmp_path: Path) -> None:
    requested: list[str] = []

    def request_json(url: str):
        requested.append(url)
        offset = 0 if "offset=0" in url else 2
        releases = [
            {
                "id": f"release-{offset + index}",
                "status": "Official",
                "release-group": {"id": f"group-{offset + index}"},
                "media": [],
            }
            for index in range(2 if offset == 0 else 1)
        ]
        return {"release-count": 3, "releases": releases}

    releases = fetch_artist_releases(tmp_path, "artist", request_json=request_json)
    cached = fetch_artist_releases(
        tmp_path,
        "artist",
        request_json=lambda _: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    assert releases == cached
    assert [release["id"] for release in releases] == ["release-0", "release-1", "release-2"]
    assert len(requested) == 2
    assert all("artist=artist" in url for url in requested)
    assert all("recordings%2Bartist-credits%2Bisrcs%2Brelease-groups" in url for url in requested)


def test_canonical_artist_releases_groups_and_prefers_official_earliest() -> None:
    releases = [
        {"id": "promotion", "status": "Promotion", "release-group": {"id": "group"}},
        {
            "id": "later",
            "status": "Official",
            "date": "2010-02-01",
            "release-group": {"id": "group"},
        },
        {
            "id": "earlier",
            "status": "Official",
            "date": "2010-01-01",
            "release-group": {"id": "group"},
        },
        {"id": "other", "status": "Official", "release-group": {"id": "other-group"}},
    ]

    assert {
        group: release["id"] for group, release in canonical_artist_releases(releases).items()
    } == {"group": "earlier", "other-group": "other"}


def test_collaborative_release_group_uses_structured_artist_credits() -> None:
    assert not collaborative_release_group({"artist-credit": [{"artist": {"id": "artist"}}]})
    assert collaborative_release_group(
        {
            "artist-credit": [
                {"artist": {"id": "artist"}},
                {"artist": {"id": "collaborator"}},
            ]
        }
    )


def test_release_group_filter_keeps_studio_albums_and_relevant_singles() -> None:
    assert relevant_release_group(
        {
            "primary-type": "Album",
            "secondary-types": [],
            "title": "Graduation",
            "first-release-date": "2007-09-11",
        }
    ) == (True, None)
    assert relevant_release_group(
        {
            "primary-type": "Single",
            "secondary-types": [],
            "title": "Stronger",
            "first-release-date": "2007-07-31",
        }
    ) == (True, None)
    assert relevant_release_group(
        {
            "primary-type": "Album",
            "secondary-types": ["Live"],
            "title": "Live Set",
            "first-release-date": "2007",
        }
    ) == (False, "secondary_type:live")
    assert relevant_release_group(
        {
            "primary-type": "Single",
            "secondary-types": [],
            "title": "Song (remix)",
            "first-release-date": "2007",
        }
    ) == (False, "alternate_version_title")


def test_canonical_release_uses_one_browse_request_and_prefers_official_earliest(
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def request_json(url: str):
        requested.append(url)
        return {
            "releases": [
                {"id": "later", "status": "Official", "date": "2010-02-01", "media": []},
                {"id": "bootleg", "status": "Bootleg", "date": "2001-01-01", "media": []},
                {"id": "earlier", "status": "Official", "date": "2010-01-01", "media": []},
            ]
        }

    result = fetch_canonical_release(
        tmp_path,
        {"id": "release-group", "title": "Album"},
        request_json=request_json,
    )

    assert result and result["id"] == "earlier"
    assert len(requested) == 1
    assert "/ws/2/release?" in requested[0]
    assert "recordings%2Bartist-credits%2Bisrcs%2Brelease-groups" in requested[0]
    cached = list((tmp_path / ALGORITHM_VERSION / "musicbrainz-releases").glob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text())["releases"][0]["id"] == "later"


def test_spotify_result_matching_baseline_track_is_checkpointed_as_duplicate(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "expansion.sqlite3"
    initialize_checkpoint(checkpoint)
    with sqlite3.connect(checkpoint) as connection:
        connection.execute(
            "INSERT INTO baseline_songs(recording_mbid,song_id,title,display_artist,"
            "spotify_url,apple_music_url,apple_track_id,stream_count,identity_json) "
            "VALUES('baseline',1,'Song','Artist',"
            "'https://open.spotify.com/track/0000000000000000000001',NULL,NULL,100,'{}')"
        )
        cursor = connection.execute(
            "INSERT INTO candidates(recording_mbid,title,updated_at) "
            "VALUES('fragment','Song','2026-01-01')"
        )
        rowid = int(cursor.lastrowid)

    _persist_spotify_batch(
        checkpoint,
        [
            {
                "song_id": rowid,
                "spotify_url": "https://open.spotify.com/track/0000000000000000000001",
                "stream_count": 123,
            }
        ],
        [],
    )

    with sqlite3.connect(checkpoint) as connection:
        assert connection.execute(
            "SELECT spotify_status,spotify_track_id,version_status FROM candidates"
        ).fetchone() == (
            "duplicate_baseline_spotify_track",
            None,
            "excluded",
        )


def test_targeted_apple_match_prefilters_remix_by_duration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "dataset.artist_expansion.read_json",
        lambda _url: {
            "results": [
                {
                    "trackId": 1,
                    "trackName": "Runaway (feat. Pusha T) [Remix]",
                    "artistName": "Kanye West & Pusha T",
                    "trackTimeMillis": 120_000,
                    "trackExplicitness": "explicit",
                    "releaseDate": "2010-01-01",
                    "previewUrl": "https://preview/remix",
                    "trackViewUrl": "https://music.apple.com/remix",
                },
                {
                    "trackId": 2,
                    "trackName": "Runaway (feat. Pusha T)",
                    "artistName": "Kanye West & Pusha T",
                    "trackTimeMillis": 547_667,
                    "trackExplicitness": "explicit",
                    "releaseDate": "2010-01-01",
                    "previewUrl": "https://preview/canonical",
                    "trackViewUrl": "https://music.apple.com/canonical",
                },
            ]
        },
    )
    candidate = {
        "recording_mbid": "recording",
        "track_name": "Runaway",
        "artist_name": "Kanye West",
    }
    metadata = {
        "title": "Runaway",
        "length": 548_200,
        "first-release-date": "2010-11-19",
        "artist-credit": [
            {
                "name": "Kanye West",
                "artist": {"id": "artist", "name": "Ye"},
            }
        ],
    }

    selected = _targeted_apple_match(tmp_path, candidate, metadata, country="US")

    assert selected and selected["trackId"] == 2


def test_targeted_apple_match_accepts_censored_title_and_prefers_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "dataset.artist_expansion.read_json",
        lambda _url: {
            "results": [
                {
                    "trackId": 1,
                    "trackName": "Ni**as in Paris",
                    "artistName": "Kanye West & JAŸ-Z",
                    "trackTimeMillis": 219_300,
                    "trackExplicitness": "cleaned",
                    "releaseDate": "2011-08-08",
                    "previewUrl": "https://preview/clean",
                    "trackViewUrl": "https://music.apple.com/clean",
                },
                {
                    "trackId": 2,
                    "trackName": "N****s in Paris",
                    "artistName": "Kanye West & JAŸ-Z",
                    "trackTimeMillis": 219_300,
                    "trackExplicitness": "explicit",
                    "releaseDate": "2011-08-08",
                    "previewUrl": "https://preview/explicit",
                    "trackViewUrl": "https://music.apple.com/explicit",
                },
            ]
        },
    )
    candidate = {
        "recording_mbid": "recording",
        "track_name": "Niggas in Paris",
        "artist_name": "Kanye West & JAŸ-Z",
    }
    metadata = {
        "title": "Niggas in Paris",
        "length": 219_000,
        "first-release-date": "2011-08-08",
        "artist-credit": [
            {"name": "Kanye West", "artist": {"id": "ye", "name": "Ye"}},
            {"name": "JAŸ-Z", "artist": {"id": "jay-z", "name": "JAŸ-Z"}},
        ],
    }

    selected = _targeted_apple_match(tmp_path, candidate, metadata, country="US")

    assert selected and selected["trackId"] == 2


def test_recording_version_requires_official_non_live_release_for_lb_only() -> None:
    live = {
        "title": "Homecoming",
        "releases": [
            {
                "status": "Official",
                "title": "VH1 Storytellers",
                "release-group": {"secondary-types": ["Live"]},
            }
        ],
    }
    studio = {
        "title": "Forever",
        "releases": [
            {
                "status": "Official",
                "title": "Relapse: Refill",
                "release-group": {"secondary-types": []},
            }
        ],
    }

    assert recording_version_decision(live, from_canonical_studio_release=False) == (
        "excluded",
        "no_official_studio_release_evidence",
    )
    assert recording_version_decision(studio, from_canonical_studio_release=False) == (
        "eligible",
        None,
    )
