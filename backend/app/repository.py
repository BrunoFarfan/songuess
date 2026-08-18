import sqlite3

from app.models import FilterMetadata, RoundRequest, RoundResponse, SongReveal, SongSearchResult


def choose_round(connection: sqlite3.Connection, request: RoundRequest) -> RoundResponse | None:
    clauses = [
        "s.enabled = 1",
        "s.release_year BETWEEN ? AND ?",
        "s.popularity_score BETWEEN ? AND ?",
    ]
    parameters: list[object] = [
        request.year_min,
        request.year_max,
        request.popularity_min,
        request.popularity_max,
    ]

    normalized_genres = sorted({genre.strip().lower() for genre in request.genres if genre.strip()})
    if normalized_genres:
        placeholders = ", ".join("?" for _ in normalized_genres)
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM song_genres sg "
            "JOIN genres g ON g.id = sg.genre_id "
            f"WHERE sg.song_id = s.id AND g.name IN ({placeholders})"
            ")"
        )
        parameters.extend(normalized_genres)

    excluded_ids = sorted({song_id for song_id in request.exclude_ids if song_id > 0})
    if excluded_ids:
        placeholders = ", ".join("?" for _ in excluded_ids)
        clauses.append(f"s.id NOT IN ({placeholders})")
        parameters.extend(excluded_ids)

    row = connection.execute(
        "SELECT s.id, s.preview_url "
        "FROM songs s "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY RANDOM() LIMIT 1",
        parameters,
    ).fetchone()
    if row is None:
        return None
    return RoundResponse(song_id=row["id"], preview_url=row["preview_url"])


def search_songs(
    connection: sqlite3.Connection, query: str, limit: int = 8
) -> list[SongSearchResult]:
    normalized_query = query.strip().casefold()
    if len(normalized_query) < 2:
        return []

    contains = f"%{_escape_like(normalized_query)}%"
    prefix = f"{_escape_like(normalized_query)}%"
    rows = connection.execute(
        "SELECT id, title, artist "
        "FROM songs "
        "WHERE enabled = 1 AND preview_url <> '' "
        "AND (lower(title) LIKE ? ESCAPE '\\' OR lower(artist) LIKE ? ESCAPE '\\') "
        "ORDER BY "
        "CASE "
        "WHEN lower(title) = ? THEN 0 "
        "WHEN lower(title) LIKE ? ESCAPE '\\' THEN 1 "
        "WHEN lower(artist) LIKE ? ESCAPE '\\' THEN 2 "
        "ELSE 3 END, "
        "title COLLATE NOCASE, artist COLLATE NOCASE "
        "LIMIT ?",
        (contains, contains, normalized_query, prefix, prefix, limit),
    ).fetchall()
    return [
        SongSearchResult(id=row["id"], title=row["title"], artist=row["artist"]) for row in rows
    ]


def get_song(connection: sqlite3.Connection, song_id: int) -> SongReveal | None:
    row = connection.execute(
        "SELECT id, title, artist, album, release_year, artwork_url, preview_url "
        "FROM songs WHERE id = ? AND enabled = 1",
        (song_id,),
    ).fetchone()
    if row is None:
        return None

    genre_rows = connection.execute(
        "SELECT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "WHERE sg.song_id = ? ORDER BY g.name COLLATE NOCASE",
        (song_id,),
    ).fetchall()
    return SongReveal(
        id=row["id"],
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        release_year=row["release_year"],
        artwork_url=row["artwork_url"],
        genres=[genre["name"] for genre in genre_rows],
        preview_url=row["preview_url"],
    )


def get_filter_metadata(connection: sqlite3.Connection) -> FilterMetadata:
    summary = connection.execute(
        "SELECT MIN(release_year) AS year_min, MAX(release_year) AS year_max, "
        "COUNT(*) AS song_count "
        "FROM songs WHERE enabled = 1"
    ).fetchone()
    genre_rows = connection.execute(
        "SELECT DISTINCT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "JOIN songs s ON s.id = sg.song_id "
        "WHERE s.enabled = 1 ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    return FilterMetadata(
        genres=[row["name"] for row in genre_rows],
        year_min=summary["year_min"],
        year_max=summary["year_max"],
        popularity_min=0,
        popularity_max=100,
        song_count=summary["song_count"],
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
