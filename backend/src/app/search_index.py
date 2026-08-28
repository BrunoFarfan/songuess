import re
import sqlite3
import unicodedata


def normalize_search_text(value: object) -> str:
    """Return a stable ASCII/caseless representation for catalog search."""

    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value))


def rebuild_search_index(connection: sqlite3.Connection) -> None:
    """Rebuild application-only search data from the canonical catalog."""

    connection.execute("DELETE FROM song_search_fts")
    connection.execute("DELETE FROM song_search")
    song_rows = connection.execute(
        "SELECT id, title, artist, album, release_year FROM songs "
        "WHERE enabled = 1 AND preview_url <> '' ORDER BY id"
    ).fetchall()
    song_search_rows: list[tuple[object, ...]] = []
    song_fts_rows: list[tuple[object, ...]] = []
    for row in song_rows:
        song_id, title_value, artist_value, album_value, year_value = row
        title = normalize_search_text(title_value)
        artist = normalize_search_text(artist_value)
        album = normalize_search_text(album_value)
        year = str(year_value)
        compact_fields = [field.replace(" ", "") for field in (title, artist, album)]
        search_text = " ".join([title, artist, album, year, *compact_fields]).strip()
        song_search_rows.append(
            (
                song_id,
                title,
                compact_fields[0],
                artist,
                compact_fields[1],
                album,
                compact_fields[2],
                year,
                search_text,
            )
        )
        song_fts_rows.append((song_id, search_text))
    connection.executemany(
        "INSERT INTO song_search (song_id, normalized_title, normalized_title_compact, "
        "normalized_artist, normalized_artist_compact, normalized_album, "
        "normalized_album_compact, normalized_year, normalized_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        song_search_rows,
    )
    connection.executemany(
        "INSERT INTO song_search_fts (song_id, normalized_text) VALUES (?, ?)",
        song_fts_rows,
    )

    connection.execute("DELETE FROM artist_search_fts")
    connection.execute("DELETE FROM artist_search_aliases")
    alias_rows = connection.execute(
        "SELECT DISTINCT a.id, a.name FROM artists a "
        "JOIN song_artists sa ON sa.artist_id = a.id "
        "JOIN songs s ON s.id = sa.song_id "
        "WHERE s.enabled = 1 AND s.preview_url <> '' "
        "UNION "
        "SELECT DISTINCT a.id, sa.credited_name FROM artists a "
        "JOIN song_artists sa ON sa.artist_id = a.id "
        "JOIN songs s ON s.id = sa.song_id "
        "WHERE s.enabled = 1 AND s.preview_url <> '' "
        "ORDER BY 1, 2"
    ).fetchall()
    artist_rows: list[tuple[object, ...]] = []
    artist_fts_rows: list[tuple[object, ...]] = []
    for artist_id, alias_value in alias_rows:
        alias = str(alias_value)
        normalized = normalize_search_text(alias)
        artist_rows.append((artist_id, alias, normalized, normalized.replace(" ", "")))
        artist_fts_rows.append((artist_id, f"{normalized} {normalized.replace(' ', '')}"))
    connection.executemany(
        "INSERT INTO artist_search_aliases "
        "(artist_id, alias, normalized_alias, normalized_alias_compact) VALUES (?, ?, ?, ?)",
        artist_rows,
    )
    connection.executemany(
        "INSERT INTO artist_search_fts (artist_id, normalized_alias) VALUES (?, ?)",
        artist_fts_rows,
    )


def ensure_search_index(connection: sqlite3.Connection) -> None:
    enabled_song_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM songs WHERE enabled = 1 AND preview_url <> ''"
        ).fetchone()[0]
    )
    indexed_song_count = int(connection.execute("SELECT COUNT(*) FROM song_search").fetchone()[0])
    credited_artist_count = int(
        connection.execute(
            "SELECT COUNT(DISTINCT a.id) FROM artists a "
            "JOIN song_artists sa ON sa.artist_id = a.id "
            "JOIN songs s ON s.id = sa.song_id "
            "WHERE s.enabled = 1 AND s.preview_url <> ''"
        ).fetchone()[0]
    )
    indexed_artist_count = int(
        connection.execute(
            "SELECT COUNT(DISTINCT artist_id) FROM artist_search_aliases"
        ).fetchone()[0]
    )
    if enabled_song_count != indexed_song_count or credited_artist_count != indexed_artist_count:
        rebuild_search_index(connection)
