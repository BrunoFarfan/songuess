from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from dataset.populate import initialize_database

from app.database import connect
from app.main import app, songs_search
from app.repository import count_searchable_songs, search_songs


def _insert_song(
    connection,
    song_id: int,
    title: str,
    artist: str,
    album: str,
    year: int,
) -> None:
    connection.execute(
        "INSERT INTO songs "
        "(id, title, artist, album, release_year, popularity_score, stream_count, "
        "musicbrainz_id, apple_track_id, artwork_url, preview_url, enabled) "
        "VALUES (?, ?, ?, ?, ?, 80, 1000, ?, ?, ?, ?, 1)",
        (
            song_id,
            title,
            artist,
            album,
            year,
            f"mbid-{song_id}",
            f"apple-{song_id}",
            f"https://images/{song_id}",
            f"https://audio/{song_id}",
        ),
    )


def _catalog(tmp_path: Path) -> Path:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        _insert_song(connection, 1, "15 Step", "Radiohead", "In Rainbows", 2007)
        _insert_song(
            connection,
            2,
            "São Paulo",
            "The Weeknd & Anitta",
            "São Paulo - Single",
            2025,
        )
        _insert_song(
            connection,
            3,
            "September",
            "Earth, Wind & Fire",
            "The Best of Earth, Wind & Fire, Vol. 1",
            1978,
        )
        _insert_song(connection, 4, "Weird Fishes / Arpeggi", "Radiohead", "In Rainbows", 2007)
        connection.commit()
    return database


def _insert_search_regression_songs(connection) -> None:
    _insert_song(connection, 5, "Mr. Brightside", "The Killers", "Hot Fuss", 2003)
    _insert_song(connection, 6, "Might", "Modest Mouse", "This Is a Long Drive", 1996)
    _insert_song(connection, 7, "When You Were Young", "The Killers", "Sam's Town", 2006)
    connection.commit()


def test_song_search_normalizes_accents_case_punctuation_and_spaces(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        punctuated = search_songs(connection, "SÃO---PAULO")
        compact = search_songs(connection, "saopaulo")

    assert punctuated[0].id == 2
    assert compact[0].id == 2
    assert punctuated[0].title == "São Paulo"
    assert punctuated[0].popularity_score == 80


def test_song_search_matches_partial_album_and_year(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        album_results = search_songs(connection, "rainb")
        year_results = search_songs(connection, "1978")

    assert {result.id for result in album_results[:2]} == {1, 4}
    assert year_results[0].id == 3
    assert album_results[0].album == "In Rainbows"
    assert year_results[0].release_year == 1978


def test_song_search_tolerates_fuzzy_typo(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        results = search_songs(connection, "radiahed")

    assert results[0].artist == "Radiohead"


def test_song_search_exact_normalized_title_outranks_loose_fuzzy_match(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        plain = search_songs(connection, "mr brightside")
        punctuated = search_songs(connection, "MR. BRIGHTSIDE")

    assert plain[0].id == 5
    assert punctuated[0].id == 5
    assert plain[0].title == "Mr. Brightside"
    assert next(result.id for result in plain if result.title == "Might") > 0


def test_song_search_exact_title_outranks_prefix_even_when_prefix_is_more_popular(
    tmp_path: Path,
) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_song(connection, 30, "I Wonder", "Kanye West", "Graduation", 2007)
        _insert_song(connection, 31, "I Wonder Why", "The Prefixes", "Questions", 2020)
        connection.execute("UPDATE songs SET popularity_score = 20 WHERE id = 30")
        connection.execute("UPDATE songs SET popularity_score = 99 WHERE id = 31")
        connection.commit()
        results = search_songs(connection, "i wonder")

    assert [result.id for result in results[:2]] == [30, 31]


def test_song_search_equally_exact_titles_use_popularity_as_tiebreaker(
    tmp_path: Path,
) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_song(connection, 32, "I Wonder", "Kanye West", "Graduation", 2007)
        _insert_song(
            connection,
            33,
            "I Wonder...",
            "j-hope & Jung Kook",
            "Hope on the Street",
            2024,
        )
        connection.execute("UPDATE songs SET popularity_score = 92 WHERE id = 32")
        connection.execute("UPDATE songs SET popularity_score = 68 WHERE id = 33")
        connection.commit()
        results = search_songs(connection, "I WONDER!!!")

    assert [result.id for result in results[:2]] == [32, 33]


def test_song_search_exact_artist_phrase_outranks_unrelated_fuzzy_titles(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        results = search_songs(connection, "the killers")

    assert {result.id for result in results[:2]} == {5, 7}
    assert all(result.artist == "The Killers" for result in results[:2])


def test_song_search_title_typo_still_prefers_the_intended_song(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        results = search_songs(connection, "mr briteside")

    assert results[0].id == 5


def test_song_search_ranks_single_character_query(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        results = search_songs(connection, "s")

    assert [result.id for result in results[:2]] == [2, 3]


def test_song_search_matches_individual_collaborators_without_rewriting_credit(
    tmp_path: Path,
) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        collaborator_results = search_songs(connection, "anitta")
        full_credit_results = search_songs(connection, "the weeknd anitta")

    assert collaborator_results[0].id == 2
    assert collaborator_results[0].artist == "The Weeknd & Anitta"
    assert full_credit_results[0].id == 2


def test_song_search_keeps_punctuated_band_credit_discoverable(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        results = search_songs(connection, "earth wind fire")

    assert results[0].id == 3
    assert results[0].artist == "Earth, Wind & Fire"


def test_song_search_has_no_global_ten_result_cap(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        for song_id in range(10, 22):
            _insert_song(
                connection,
                song_id,
                f"Catalog Song {song_id}",
                f"Catalog Artist {song_id}",
                f"Catalog Album {song_id}",
                1980 + song_id,
            )
        connection.commit()

        results = search_songs(connection, "unrelated typo")

    assert len(results) == 16


def test_song_search_pages_are_stable_continuous_and_do_not_overlap(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        for song_id in range(10, 135):
            _insert_song(
                connection,
                song_id,
                f"Catalog Song {song_id}",
                f"Catalog Artist {song_id}",
                f"Catalog Album {song_id}",
                1880 + song_id,
            )
        connection.commit()

        expected = search_songs(connection, "catalog", limit=200)
        pages = [
            search_songs(connection, "catalog", limit=17, offset=offset)
            for offset in range(0, count_searchable_songs(connection), 17)
        ]

    expected_ids = [result.id for result in expected]
    paged_ids = [result.id for page in pages for result in page]
    assert len(expected_ids) > 100
    assert paged_ids == expected_ids
    assert len(paged_ids) == len(set(paged_ids))


def test_empty_song_search_is_alphabetical_and_pages_without_overlap(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 10, "Alone", "Zulu", "First", 2000)
        _insert_song(connection, 11, "Álone", "Alpha", "Second", 2001)
        connection.commit()

        first_page = search_songs(connection, "", limit=3)
        second_page = search_songs(connection, "", limit=3, offset=3)

    first_ids = [result.id for result in first_page]
    second_ids = [result.id for result in second_page]
    assert first_ids == [1, 11, 10]
    assert second_ids == [2, 3, 4]
    assert not set(first_ids) & set(second_ids)


def test_song_search_api_returns_page_metadata_and_declares_bounds(
    tmp_path: Path, monkeypatch
) -> None:
    database = _catalog(tmp_path)

    @contextmanager
    def temporary_database_connection():
        with connect(database) as connection:
            yield connection

    monkeypatch.setattr("app.main.database_connection", temporary_database_connection)
    response = songs_search(q="radiohead", offset=0, limit=2)
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in app.openapi()["paths"]["/api/songs/search"]["get"]["parameters"]
    }

    assert response.model_dump() == {
        "items": [
            {
                "id": 1,
                "title": "15 Step",
                "artist": "Radiohead",
                "album": "In Rainbows",
                "release_year": 2007,
                "artwork_url": "https://images/1",
                "popularity_score": 80,
            },
            {
                "id": 4,
                "title": "Weird Fishes / Arpeggi",
                "artist": "Radiohead",
                "album": "In Rainbows",
                "release_year": 2007,
                "artwork_url": "https://images/4",
                "popularity_score": 80,
            },
        ],
        "offset": 0,
        "limit": 2,
        "total": 4,
        "has_more": True,
    }
    assert parameters["limit"] == {
        "type": "integer",
        "maximum": 100,
        "minimum": 1,
        "default": 40,
        "title": "Limit",
    }
    assert parameters["offset"]["minimum"] == 0
    assert parameters["offset"]["default"] == 0


def test_song_search_api_defaults_to_empty_alphabetical_query(tmp_path: Path, monkeypatch) -> None:
    database = _catalog(tmp_path)

    @contextmanager
    def temporary_database_connection():
        with connect(database) as connection:
            yield connection

    monkeypatch.setattr("app.main.database_connection", temporary_database_connection)
    response = songs_search(q="", offset=0, limit=2)
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in app.openapi()["paths"]["/api/songs/search"]["get"]["parameters"]
    }

    assert [item.id for item in response.items] == [1, 2]
    assert response.total == 4
    assert response.has_more is True
    assert parameters["q"]["default"] == ""
    assert "minLength" not in parameters["q"]
