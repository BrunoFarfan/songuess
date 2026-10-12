from __future__ import annotations

import asyncio
from pathlib import Path

from dataset.populate import initialize_database

from app.database import SQLiteDatabase, connect
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

    assert {1, 4} <= {result.id for result in album_results}
    assert year_results[0].id == 3
    assert album_results[0].album == "In Rainbows"
    assert year_results[0].release_year == 1978


def test_song_search_tolerates_fuzzy_typo(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        results = search_songs(connection, "radiahed")

    assert results[0].artist == "Radiohead"


def test_song_search_exact_titles_rank_above_fuzzy_matches(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        _insert_song(connection, 8, "Mr. Briteside", "Cover Artist", "Covers", 2020)
        connection.commit()
        plain = search_songs(connection, "mr brightside")
        punctuated = search_songs(connection, "MR. BRIGHTSIDE")

    assert [result.id for result in plain[:2]] == [5, 8]
    assert [result.id for result in punctuated[:2]] == [5, 8]


def test_song_search_ranks_compound_title_artist_and_album_queries(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 40, "Go Flex", "Post Malone", "Stoney", 2016)
        _insert_song(connection, 41, "16 Mirrors", "Alex G", "Trick", 2012)
        _insert_song(connection, 42, "Flex", "Other Artist", "Postcards", 2020)
        _insert_song(connection, 43, "Go", "Post Malone", "Other Album", 2020)
        _insert_song(connection, 44, "Break", "alex_g_offline", "Unreleased", 2020)
        _insert_song(connection, 45, "Runner", "Alex G", "God Save the Animals", 2022)
        connection.commit()
        compound = search_songs(connection, "go flex post")
        fuzzy = search_songs(connection, "go flex post malonee")
        album = search_songs(connection, "go flex stoney")
        missing_term = search_songs(connection, "go flex radiohead")
        exact_title = search_songs(connection, "go flex")
        exact_artist = search_songs(connection, "post malone")

    assert compound[0].id == 40
    assert fuzzy[0].id == 40
    assert album[0].id == 40
    assert missing_term  # Weak candidates remain available without a similarity cutoff.
    assert exact_title[0].id == 40
    assert [result.id for result in exact_artist[:2]] == [43, 40]


def test_song_search_similarity_order_ignores_popularity(
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


def test_song_search_equal_normalized_titles_use_original_popularity_tiebreaker(
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


def test_song_search_artist_phrase_keeps_matching_titles_discoverable(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        results = search_songs(connection, "the killers")

    assert {5, 7} <= {result.id for result in results}


def test_song_search_title_typo_still_finds_the_intended_song(tmp_path: Path) -> None:
    database = _catalog(tmp_path)

    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        results = search_songs(connection, "mr briteside")

    assert 5 in {result.id for result in results}


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

        results = search_songs(connection, "catalog")

    assert len(results) == 12


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


def test_empty_song_search_is_seeded_random_and_pages_without_overlap(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 10, "Alone", "Zulu", "First", 2000)
        _insert_song(connection, 11, "Álone", "Alpha", "Second", 2001)
        connection.commit()
        expected = search_songs(connection, "", limit=6, seed=17)
        first_page = search_songs(connection, "", limit=3, seed=17)
        second_page = search_songs(connection, "", limit=3, offset=3, seed=17)
        other_seed = search_songs(connection, "", limit=6, seed=18)

    ids = [result.id for result in expected]
    assert [result.id for result in first_page + second_page] == ids
    assert len(ids) == len(set(ids)) == 6
    assert set(ids) == {1, 2, 3, 4, 10, 11}
    assert ids != [1, 11, 10, 2, 3, 4]
    assert [result.id for result in other_seed] != ids


def test_song_search_api_returns_page_metadata_and_declares_bounds(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        response = asyncio.run(
            songs_search(SQLiteDatabase(connection), q="radiohead", offset=0, limit=2)
        )
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
        "total": 2,
        "has_more": False,
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
    assert parameters["seed"]["minimum"] == 0
    assert parameters["seed"]["maximum"] == 4_294_967_295
    assert parameters["seed"]["default"] == 0


def test_song_search_api_defaults_to_empty_random_query(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        response = asyncio.run(songs_search(SQLiteDatabase(connection), q="", offset=0, limit=2))
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in app.openapi()["paths"]["/api/songs/search"]["get"]["parameters"]
    }

    assert len(response.items) == 2
    assert len({item.id for item in response.items}) == 2
    assert response.total == 4
    assert response.has_more is True
    assert parameters["q"]["default"] == ""
    assert "minLength" not in parameters["q"]


def test_empty_song_search_reports_full_catalog_total_beyond_candidate_cap(tmp_path: Path) -> None:
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
        response = asyncio.run(songs_search(SQLiteDatabase(connection), q="", offset=120, limit=9))

    assert response.total == 129
    assert len(response.items) == 9
    assert response.has_more is False


def test_song_search_keeps_weak_matches_but_ranks_similarity_first(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_search_regression_songs(connection)
        results = search_songs(connection, "mr brightside")
    assert results[0].id == 5
    assert 6 in {result.id for result in results}  # Might shares a trigram, below the old cutoff.


def test_song_search_equal_artist_scores_use_alphabetical_title_ties(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 40, "Zulu", "Same Artist", "Album", 2020)
        _insert_song(connection, 41, "Alpha", "Same Artist", "Album", 2020)
        connection.commit()
        results = search_songs(connection, "same artist")
    assert [song.id for song in results[:2]] == [41, 40]


def test_song_search_combined_title_artist_terms_rank_first(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 40, "Stronger", "Kanye West", "Graduation", 2007)
        _insert_song(connection, 41, "Stronger Than Me", "Amy Winehouse", "Frank", 2003)
        connection.commit()
        for query in ("stronger kanye west", "kanye west stronger"):
            results = search_songs(connection, query)
            assert results[0].id == 40
            assert 41 in {song.id for song in results}  # Keep weaker matches without a cutoff.


def test_song_search_original_term_priority_handles_featured_title(tmp_path: Path) -> None:
    database = _catalog(tmp_path)
    with connect(database) as connection:
        _insert_song(connection, 40, "Ghost Town (feat. PARTYNEXTDOOR)", "Kanye West", "ye", 2018)
        _insert_song(connection, 41, "Cotton Candy", "Lil Wayne & 2 Chainz", "Tha Carter VI", 2025)
        connection.commit()
        results = search_songs(connection, "ghost town kanye")
    assert results[0].id == 40
