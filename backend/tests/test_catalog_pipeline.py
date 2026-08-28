import json
import sqlite3
from argparse import Namespace
from pathlib import Path

from dataset.artist_cap_audit import SongOption, initialize_audit_database, select_with_cap
from dataset.catalog_pipeline import (
    disable_persistent_streaming_failures,
    discover,
    export_delta,
)
from dataset.populate import initialize_database
from dataset.verify import snapshot_catalog


def _insert_song(database: Path, song_id: int, mbid: str, apple_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO songs (id, title, artist, release_year, musicbrainz_id, "
            "apple_track_id, preview_url, apple_music_url, spotify_url, stream_count, "
            "stream_count_fetched_at, stream_count_source, stream_count_status, enabled) "
            "VALUES (?, ?, 'Artist', 2000, ?, ?, 'https://preview', "
            "'https://music.apple.com/track', "
            "'https://open.spotify.com/track/0000000000000000000001', 100, "
            "'2026-01-01T00:00:00Z', 'spotify_web_hydration', 'complete', 1)",
            (song_id, f"Song {song_id}", mbid, apple_id),
        )


def test_discover_writes_ranked_append_only_manifest(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    _insert_song(database, 1, "existing", "apple-existing")
    candidates = [
        {"recording_mbid": "existing", "discovery_sources": ["sitewide_all_time"]},
        {
            "recording_mbid": "new",
            "artist_mbids": ["artist-new"],
            "artist_name": "New Artist",
            "track_name": "New Song",
            "discovery_sources": ["sitewide_all_time", "sitewide_year"],
        },
    ]
    monkeypatch.setattr(
        "dataset.catalog_pipeline.fetch_listenbrainz_candidates",
        lambda *_args, **_kwargs: candidates,
    )
    manifest = tmp_path / "manifest.json"
    snapshot = tmp_path / "snapshot.json"
    args = Namespace(
        database=database,
        cache=tmp_path / "cache",
        manifest=manifest,
        snapshot=snapshot,
        candidate_count=10,
        artist_count=5,
        recordings_per_artist=15,
        discovery_workers=2,
        target_total=2,
    )

    payload = discover(args)

    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["recording_mbid"] == "new"
    assert payload["candidates"][0]["score"] > 1_000_000
    assert json.loads(manifest.read_text())["version"] == 1
    assert snapshot.exists()


def test_export_delta_contains_only_new_application_rows(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    _insert_song(database, 1, "baseline", "apple-baseline")
    snapshot = tmp_path / "snapshot.json"
    snapshot_catalog(database, snapshot)
    _insert_song(database, 2, "new", "apple-new")
    output = tmp_path / "delta.json"

    payload = export_delta(database, snapshot, output)

    assert payload["new_song_count"] == 1
    assert payload["songs"][0]["musicbrainz_id"] == "new"
    assert "discovery manifests" in payload["excludes"]


def test_disable_persistent_streaming_failures_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO songs (id, title, artist, release_year, musicbrainz_id, "
            "apple_track_id, preview_url, apple_music_url, stream_count_status, enabled) "
            "VALUES (1, 'Unresolved', 'Artist', 2000, 'mbid', 'apple', "
            "'https://preview', 'https://music.apple.com/track', 'missing_link', 1)"
        )
        connection.execute(
            "INSERT INTO spotify_backfill_failures "
            "(song_id, status, match_method, attempted_at, "
            "musicbrainz_relationship_checked_at, catalog_lookup_checked_at) "
            "VALUES (1, 'title_mismatch', 'exact_metadata', '2026-01-01', "
            "'2026-01-01', '2026-01-01')"
        )

    first = disable_persistent_streaming_failures(database)
    second = disable_persistent_streaming_failures(database)

    assert first["disabled_count"] == 1
    assert second["disabled_count"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT enabled FROM songs WHERE id = 1").fetchone()[0] == 0


def test_artist_cap_selection_counts_every_credited_artist() -> None:
    options = [
        SongOption("duet", "candidate", "Duet", "A & B", ("a", "b"), 300),
        SongOption("a-solo", "existing", "A Solo", "A", ("a",), 200),
        SongOption("b-solo", "existing", "B Solo", "B", ("b",), 100),
    ]

    selected = select_with_cap(options, artist_cap=1)

    assert [song.identity for song in selected] == ["duet"]


def test_artist_cap_audit_database_initialization_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"

    initialize_audit_database(database)
    initialize_audit_database(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='candidates'"
            ).fetchone()[0]
            == 1
        )
