from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dataset.catalog_pipeline import export_delta
from dataset.export_d1 import export_d1
from dataset.populate import initialize_database
from dataset.verify import snapshot_catalog

from app.catalog_snapshot import SNAPSHOT_REFRESH_SQL, CatalogSnapshot
from app.models import FilterContextRequest, RoundRequest


def _insert_song(connection: sqlite3.Connection, song_id: int, score: int | None = 50) -> None:
    connection.execute(
        "INSERT INTO songs (id, title, artist, release_year, popularity_score, "
        "musicbrainz_id, apple_track_id, preview_url) "
        "VALUES (?, ?, 'Artist', ?, ?, ?, ?, 'https://preview')",
        (song_id, f"Song {song_id}", 1990 + song_id, score, f"mbid-{song_id}", f"apple-{song_id}"),
    )


def _snapshot(connection: sqlite3.Connection) -> CatalogSnapshot:
    version, payload = connection.execute(
        "SELECT version, payload FROM catalog_runtime WHERE id = 1"
    ).fetchone()
    return CatalogSnapshot.from_payload(payload, version)


def test_migration_backfills_only_eligible_rows_and_matches_shared_refresh(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        _insert_song(connection, 3)
        _insert_song(connection, 1)
        _insert_song(connection, 2, None)
        connection.execute("UPDATE songs SET enabled = 0 WHERE id = 3")
        migration = Path("migrations/014_runtime_catalog.sql").read_text()
        assert migration.endswith(SNAPSHOT_REFRESH_SQL)
        connection.executescript(migration)
        snapshot = _snapshot(connection)
        assert list(snapshot.songs) == [1]
        assert snapshot.browse_count == 2
        before = snapshot.version
        connection.executescript(SNAPSHOT_REFRESH_SQL)
        assert _snapshot(connection).version != before


def test_full_replacement_refreshes_snapshot_and_exports_deterministically(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    initialize_database(source)
    initialize_database(destination)
    with sqlite3.connect(source) as connection:
        _insert_song(connection, 2)
        _insert_song(connection, 3, None)
    with sqlite3.connect(destination) as connection:
        _insert_song(connection, 1)
        connection.executescript(SNAPSHOT_REFRESH_SQL)
        initial_version = _snapshot(connection).version
    output = tmp_path / "catalog.sql"
    first_manifest = export_d1(source, output)
    original_sql = output.read_text()
    assert export_d1(source, output) == first_manifest
    assert output.read_text() == original_sql
    with sqlite3.connect(destination) as connection:
        connection.executescript(original_sql)
        snapshot = _snapshot(connection)
        assert list(snapshot.songs) == [2]
        assert snapshot.browse_count == 2
        assert snapshot.version != initial_version
        payload = connection.execute("SELECT payload FROM catalog_runtime").fetchone()[0]
        import hashlib

        assert (
            first_manifest["catalog_runtime_sha256"] == hashlib.sha256(payload.encode()).hexdigest()
        )


def test_delta_refresh_includes_destination_baseline_and_new_song(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        _insert_song(connection, 1)
        connection.executescript(SNAPSHOT_REFRESH_SQL)
    baseline = tmp_path / "baseline.json"
    snapshot_catalog(database, baseline)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(database) as connection:
        _insert_song(connection, 2)
    delta = export_delta(database, baseline, tmp_path / "delta.json")
    with sqlite3.connect(destination) as connection:
        original_version = _snapshot(connection).version
        for song in delta["songs"]:
            columns = list(song)
            connection.execute(
                f"INSERT INTO songs ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                list(song.values()),
            )
        assert list(_snapshot(connection).songs) == [1]
        connection.executescript(delta["catalog_runtime_refresh_sql"])
        snapshot = _snapshot(connection)
        assert list(snapshot.songs) == [1, 2]
        assert snapshot.version != original_version


def test_progressive_facets_ignore_later_selections_and_bound_cache() -> None:
    snapshot = CatalogSnapshot.from_payload(
        json.dumps(
            {
                "songs": [[1, 1990, 20], [2, 2010, 80]],
                "genres": [[1, "rock"], [2, "pop"]],
                "countries": [[1, "US"], [2, "GB"]],
                "artists": [[1, "artist"]],
                "song_genres": [[1, 1], [2, 2]],
                "song_countries": [[1, 1], [2, 2]],
                "song_artists": [[1, 1], [2, 1]],
                "browse_count": 2,
            }
        ),
        "version",
    )
    request = FilterContextRequest(
        genres=[" POP "],
        countries=["gb"],
        year_min=2020,
        popularity_min=90,
    )
    metadata = snapshot.filter_metadata(request)
    assert metadata.genres == ["pop", "rock"]
    assert metadata.countries == ["GB"]
    assert (metadata.year_min, metadata.year_max) == (2010, 2010)
    assert metadata.popularity_max == 0
    assert metadata.song_count == 0
    for year in range(1800, 2100):
        snapshot.filter_metadata(FilterContextRequest(year_min=year))
    assert len(snapshot._facets) <= 128
    round_request = RoundRequest(
        year_min=1800,
        year_max=2200,
        popularity_min=0,
        popularity_max=100,
        exclude_ids=[1],
    )
    assert snapshot.eligible_ids(round_request) == [1, 2]
    assert snapshot.eligible_ids(round_request, exclude=True) == [2]
