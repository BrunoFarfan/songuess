import sqlite3

from dataset.populate import initialize_database
from dataset.spotify_streams_mvp import (
    extract_spotify_track_ids,
    load_representative_sample,
    parse_spotify_stream_count,
)


def test_extract_spotify_track_ids_keeps_only_unique_track_relationships() -> None:
    payload = {
        "relations": [
            {"url": {"resource": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"}},
            {"url": {"resource": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"}},
            {"url": {"resource": "https://www.youtube.com/watch?v=fHI8X4OXluQ"}},
        ]
    }

    assert extract_spotify_track_ids(payload) == ["0VjIjW4GlUZAMYd2vXMi3b"]


def test_parse_spotify_stream_count_uses_exact_track_link_and_title() -> None:
    body = """
    <meta property="og:title" content="Blinding Lights" />
    <a href="/track/other"><span>Other</span><span>9,999,999</span></a>
    <a href="/track/0VjIjW4GlUZAMYd2vXMi3b">
      <span>Blinding Lights</span><span>5,556,748,096</span>
    </a>
    """

    assert parse_spotify_stream_count(body, "0VjIjW4GlUZAMYd2vXMi3b", "Blinding Lights") == (
        5_556_748_096,
        "matched",
    )
    assert parse_spotify_stream_count(body, "other", "Wrong title") == (
        None,
        "track_title_mismatch",
    )


def test_parse_spotify_stream_count_reports_valid_track_without_extracted_count() -> None:
    body = '<meta property="og:title" content="What It Takes" />'

    assert parse_spotify_stream_count(body, "2rVeiO8AI1ZDSv4wjS46OX", "What It Takes") == (
        None,
        "stream_count_not_extracted",
    )


def test_representative_sample_spans_popularity_order(tmp_path) -> None:
    database = tmp_path / "songs.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        for index in range(10):
            connection.execute(
                "INSERT INTO songs "
                "(id, title, artist, release_year, popularity_score, musicbrainz_id, preview_url) "
                "VALUES (?, ?, 'Artist', 2000, ?, ?, 'https://preview')",
                (index + 1, f"Song {index}", index, f"mbid-{index}"),
            )

    sample = load_representative_sample(database, 5, seed=1)

    assert sorted(song.popularity_score for song in sample) == [1, 3, 5, 7, 9]
