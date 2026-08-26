from __future__ import annotations

import sqlite3
from pathlib import Path

from dataset.populate import initialize_database

from app.database import connect
from app.models import FilterContextRequest
from app.repository import get_contextual_filter_metadata


def _song(
    connection: sqlite3.Connection,
    song_id: int,
    *,
    artist: str,
    genre: str,
    country: str,
    year: int,
    popularity: int,
) -> None:
    connection.execute(
        "INSERT INTO songs (id, title, artist, release_year, popularity_score, stream_count, "
        "musicbrainz_id, apple_track_id, preview_url, enabled) "
        "VALUES (?, ?, ?, ?, ?, 100, ?, ?, ?, 1)",
        (
            song_id,
            f"Song {song_id}",
            artist,
            year,
            popularity,
            f"recording-{song_id}",
            f"apple-{song_id}",
            f"https://audio/{song_id}",
        ),
    )
    connection.execute(
        "INSERT OR IGNORE INTO artists (musicbrainz_id, name) VALUES (?, ?)",
        (artist, artist),
    )
    artist_id = connection.execute(
        "SELECT id FROM artists WHERE musicbrainz_id = ?", (artist,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO song_artists "
        "(song_id, artist_id, credit_order, credited_name, join_phrase) "
        "VALUES (?, ?, 0, ?, '')",
        (song_id, artist_id, artist),
    )
    connection.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre,))
    genre_id = connection.execute("SELECT id FROM genres WHERE name = ?", (genre,)).fetchone()[0]
    connection.execute(
        "INSERT INTO song_genres (song_id, genre_id) VALUES (?, ?)", (song_id, genre_id)
    )
    connection.execute("INSERT OR IGNORE INTO countries (code) VALUES (?)", (country,))
    country_id = connection.execute(
        "SELECT id FROM countries WHERE code = ?", (country,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO song_countries (song_id, country_id) VALUES (?, ?)",
        (song_id, country_id),
    )


def test_contextual_metadata_applies_filters_in_wizard_order(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _song(
            connection,
            1,
            artist="artist-a",
            genre="rock",
            country="US",
            year=1990,
            popularity=40,
        )
        _song(
            connection,
            2,
            artist="artist-a",
            genre="pop",
            country="GB",
            year=2005,
            popularity=80,
        )
        _song(
            connection,
            3,
            artist="artist-b",
            genre="jazz",
            country="FR",
            year=1970,
            popularity=20,
        )
        connection.commit()

        metadata = get_contextual_filter_metadata(
            connection,
            FilterContextRequest(
                artist_id="artist-a",
                genres=["pop"],
                countries=["GB"],
                year_min=2000,
                year_max=2010,
                popularity_min=70,
                popularity_max=90,
            ),
        )

    assert metadata.genres == ["pop", "rock"]
    assert metadata.countries == ["GB"]
    assert (metadata.year_min, metadata.year_max) == (2005, 2005)
    assert (metadata.popularity_min, metadata.popularity_max) == (80, 80)
    assert metadata.song_count == 1


def test_contextual_metadata_reports_dead_end_without_inventing_ranges(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _song(
            connection,
            1,
            artist="artist-a",
            genre="rock",
            country="US",
            year=1990,
            popularity=40,
        )
        connection.commit()
        metadata = get_contextual_filter_metadata(
            connection,
            FilterContextRequest(artist_id="missing"),
        )

    assert metadata.genres == []
    assert metadata.countries == []
    assert metadata.year_min is None
    assert metadata.year_max is None
    assert metadata.song_count == 0
