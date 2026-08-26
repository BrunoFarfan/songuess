from __future__ import annotations

import sqlite3
from pathlib import Path

from dataset.populate import initialize_database

from app.database import connect
from app.models import RoundRequest
from app.repository import choose_round, search_artists


def _insert_song(
    connection: sqlite3.Connection,
    *,
    song_id: int,
    display_artist: str,
    credits: list[tuple[str, str]],
) -> None:
    connection.execute(
        "INSERT INTO songs (id, title, artist, release_year, popularity_score, stream_count, "
        "musicbrainz_id, apple_track_id, preview_url, enabled) "
        "VALUES (?, ?, ?, 2000, 50, 100, ?, ?, ?, 1)",
        (
            song_id,
            f"Song {song_id}",
            display_artist,
            f"recording-{song_id}",
            f"apple-{song_id}",
            f"https://audio/{song_id}",
        ),
    )
    for credit_order, (artist_mbid, name) in enumerate(credits):
        connection.execute(
            "INSERT OR IGNORE INTO artists (musicbrainz_id, name) VALUES (?, ?)",
            (artist_mbid, name),
        )
        artist_id = connection.execute(
            "SELECT id FROM artists WHERE musicbrainz_id = ?", (artist_mbid,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO song_artists "
            "(song_id, artist_id, credit_order, credited_name, join_phrase) "
            "VALUES (?, ?, ?, ?, '')",
            (song_id, artist_id, credit_order, name),
        )


def _request(artist_id: str, *, exclude_ids: list[int] | None = None) -> RoundRequest:
    return RoundRequest(
        artist_id=artist_id,
        year_min=1950,
        year_max=2026,
        popularity_min=0,
        popularity_max=100,
        exclude_ids=exclude_ids or [],
    )


def test_unique_artist_search_and_relationship_filter_include_collaborations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(
            connection,
            song_id=1,
            display_artist="The Weeknd",
            credits=[("weeknd", "The Weeknd")],
        )
        _insert_song(
            connection,
            song_id=2,
            display_artist="The Weeknd & Anitta",
            credits=[("weeknd", "The Weeknd"), ("anitta", "Anitta")],
        )
        connection.commit()

        weeknd_results = search_artists(connection, "The Weeknd")
        anitta_results = search_artists(connection, "Anitta")
        first = choose_round(connection, _request("weeknd"))
        assert first is not None
        second = choose_round(connection, _request("weeknd", exclude_ids=[first.song_id]))
        anitta_round = choose_round(connection, _request("anitta"))

    assert [result.model_dump() for result in weeknd_results] == [
        {
            "id": "weeknd",
            "name": "The Weeknd",
            "disambiguation": None,
            "song_count": 2,
        }
    ]
    assert [result.name for result in anitta_results] == ["Anitta"]
    assert second is not None
    assert {first.song_id, second.song_id} == {1, 2}
    assert anitta_round is not None and anitta_round.song_id == 2


def test_kanye_credit_alias_returns_one_identity_and_filters_all_credits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    kanye_mbid = "164f0d73-1234-4e2c-8743-d77bf2191051"
    with connect(database) as connection:
        _insert_song(
            connection,
            song_id=1,
            display_artist="Kanye West",
            credits=[(kanye_mbid, "Kanye West")],
        )
        _insert_song(
            connection,
            song_id=2,
            display_artist="Kanye West & Jay-Z",
            credits=[(kanye_mbid, "Kanye West"), ("jay-z", "JAY-Z")],
        )
        connection.execute(
            "UPDATE artists SET name = 'Ye', sort_name = 'Ye', "
            "disambiguation = 'formerly Kanye West' WHERE musicbrainz_id = ?",
            (kanye_mbid,),
        )
        connection.commit()

        results = search_artists(connection, "kanye")
        first = choose_round(connection, _request(kanye_mbid))
        assert first is not None
        second = choose_round(
            connection,
            _request(kanye_mbid, exclude_ids=[first.song_id]),
        )

    assert [result.model_dump() for result in results[:1]] == [
        {
            "id": kanye_mbid,
            "name": "Kanye West",
            "disambiguation": "formerly Kanye West",
            "song_count": 2,
        }
    ]
    assert sum(result.id == kanye_mbid for result in results) == 1
    assert second is not None
    assert {first.song_id, second.song_id} == {1, 2}


def test_punctuation_in_canonical_names_is_never_split(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(
            connection,
            song_id=1,
            display_artist="Tyler, The Creator",
            credits=[("tyler", "Tyler, The Creator")],
        )
        _insert_song(
            connection,
            song_id=2,
            display_artist="Earth, Wind & Fire",
            credits=[("earth-wind-fire", "Earth, Wind & Fire")],
        )
        connection.commit()

        assert [item.name for item in search_artists(connection, "Tyler")] == ["Tyler, The Creator"]
        assert [item.name for item in search_artists(connection, "Earth")] == ["Earth, Wind & Fire"]


def test_same_name_artists_remain_separate_and_combined_credits_are_absent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(
            connection,
            song_id=1,
            display_artist="Shared Name",
            credits=[("artist-one", "Shared Name")],
        )
        _insert_song(
            connection,
            song_id=2,
            display_artist="Shared Name feat. Guest",
            credits=[("artist-two", "Shared Name"), ("guest", "Guest")],
        )
        connection.commit()

        same_name = search_artists(connection, "Shared Name")
        combined = search_artists(connection, "Shared Name feat. Guest")

    assert {item.id for item in same_name} == {"artist-one", "artist-two"}
    assert [item.name for item in same_name] == ["Shared Name", "Shared Name"]
    assert combined == []
