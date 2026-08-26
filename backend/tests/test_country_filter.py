from __future__ import annotations

import sqlite3
from pathlib import Path

from dataset.populate import initialize_database

from app.database import connect
from app.models import RoundRequest
from app.repository import choose_round, get_filter_metadata


def _insert_song(
    connection: sqlite3.Connection,
    *,
    song_id: int,
    mbid: str,
    apple_id: str,
    enabled: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO songs (id, title, artist, release_year, popularity_score, stream_count, "
        "musicbrainz_id, apple_track_id, preview_url, enabled) "
        "VALUES (?, ?, 'Artist', 2000, 50, 100, ?, ?, ?, ?)",
        (song_id, f"Song {song_id}", mbid, apple_id, f"https://audio/{song_id}", enabled),
    )


def _link_country(connection: sqlite3.Connection, song_id: int, code: str) -> None:
    connection.execute("INSERT OR IGNORE INTO countries (code) VALUES (?)", (code,))
    country_id = connection.execute("SELECT id FROM countries WHERE code = ?", (code,)).fetchone()[
        0
    ]
    connection.execute(
        "INSERT INTO song_countries (song_id, country_id) VALUES (?, ?)",
        (song_id, country_id),
    )


def _request(*countries: str) -> RoundRequest:
    return RoundRequest(
        countries=list(countries),
        year_min=1950,
        year_max=2026,
        popularity_min=0,
        popularity_max=100,
    )


def test_round_country_filter_matches_any_credited_artist_country(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(connection, song_id=1, mbid="one", apple_id="1")
        _insert_song(connection, song_id=2, mbid="two", apple_id="2")
        _insert_song(connection, song_id=3, mbid="unknown", apple_id="3")
        _link_country(connection, 1, "US")
        _link_country(connection, 1, "GB")
        _link_country(connection, 2, "CL")
        connection.commit()

        assert choose_round(connection, _request("gb")).song_id == 1
        assert choose_round(connection, _request("US", "CL")).song_id in {1, 2}
        assert choose_round(connection, _request("DE")) is None
        assert choose_round(connection, _request()).song_id in {1, 2, 3}


def test_filter_metadata_lists_only_countries_on_enabled_songs(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(connection, song_id=1, mbid="one", apple_id="1")
        _insert_song(connection, song_id=2, mbid="two", apple_id="2", enabled=0)
        _insert_song(connection, song_id=3, mbid="unknown", apple_id="3")
        _link_country(connection, 1, "US")
        _link_country(connection, 1, "GB")
        _link_country(connection, 2, "CL")
        connection.commit()

        metadata = get_filter_metadata(connection)

    assert metadata.countries == ["GB", "US"]
    assert metadata.song_count == 2
