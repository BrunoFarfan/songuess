import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher

from app.models import (
    ArtistOption,
    FilterContextRequest,
    FilterMetadata,
    RoundRequest,
    RoundResponse,
    SongReveal,
    SongSearchResult,
)


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

    normalized_countries = sorted(
        {country.strip().upper() for country in request.countries if country.strip()}
    )
    if normalized_countries:
        placeholders = ", ".join("?" for _ in normalized_countries)
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM song_countries sc "
            "JOIN countries c ON c.id = sc.country_id "
            f"WHERE sc.song_id = s.id AND c.code IN ({placeholders})"
            ")"
        )
        parameters.extend(normalized_countries)

    if request.artist_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM song_artists sa "
            "JOIN artists a ON a.id = sa.artist_id "
            "WHERE sa.song_id = s.id AND a.musicbrainz_id = ?)"
        )
        parameters.append(request.artist_id)

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


def _normalize_search_text(value: object) -> str:
    """Return a stable ASCII/caseless representation for catalog search."""
    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value))


def _fuzzy_similarity(query: str, candidate: str) -> float:
    if not candidate:
        return 0.0
    return SequenceMatcher(None, query, candidate, autojunk=False).ratio()


def _song_search_rank(row: sqlite3.Row, query: str) -> tuple[object, ...]:
    query_compact = query.replace(" ", "")
    title = _normalize_search_text(row["title"])
    artist = _normalize_search_text(row["artist"])
    album = _normalize_search_text(row["album"])
    year = str(row["release_year"])
    primary_fields = (title, artist, album, year)
    primary_compact = tuple(field.replace(" ", "") for field in primary_fields)
    field_priority = next(
        (index for index, field in enumerate(primary_compact) if field == query_compact),
        len(primary_fields),
    )

    if query_compact in primary_compact:
        tier = 0
    elif any(field.startswith(query_compact) for field in primary_compact):
        tier = 1
    elif any(query_compact in field for field in primary_compact):
        tier = 2
    else:
        combined = " ".join(field for field in primary_fields if field)
        query_tokens = query.split()
        tier = 3 if query_tokens and all(token in combined for token in query_tokens) else 4

    fuzzy_candidates = primary_compact
    combined_compact = "".join(primary_compact)
    fuzzy_score = max(
        (_fuzzy_similarity(query_compact, candidate) for candidate in fuzzy_candidates),
        default=0.0,
    )
    fuzzy_score = max(fuzzy_score, _fuzzy_similarity(query_compact, combined_compact))

    return (
        tier,
        field_priority,
        -fuzzy_score,
        -int(row["popularity_score"] or 0),
        title,
        artist,
        int(row["id"]),
    )


def search_songs(
    connection: sqlite3.Connection,
    query: str,
    limit: int = 40,
    offset: int = 0,
) -> list[SongSearchResult]:
    normalized_query = _normalize_search_text(query)
    rows = connection.execute(
        "SELECT id, title, artist, album, release_year, artwork_url, popularity_score "
        "FROM songs "
        "WHERE enabled = 1 AND preview_url <> ''"
    ).fetchall()
    if normalized_query:
        ordered_rows = sorted(rows, key=lambda row: _song_search_rank(row, normalized_query))
    else:
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                _normalize_search_text(row["title"]),
                _normalize_search_text(row["artist"]),
                int(row["id"]),
            ),
        )
    ranked_rows = ordered_rows[offset : offset + limit]
    return [
        SongSearchResult(
            id=row["id"],
            title=row["title"],
            artist=row["artist"],
            album=row["album"],
            release_year=row["release_year"],
            artwork_url=row["artwork_url"],
            popularity_score=row["popularity_score"],
        )
        for row in ranked_rows
    ]


def count_searchable_songs(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM songs WHERE enabled = 1 AND preview_url <> ''"
    ).fetchone()
    return int(row["total"])


def search_artists(
    connection: sqlite3.Connection, query: str, limit: int = 10
) -> list[ArtistOption]:
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        return []

    rows = connection.execute(
        "SELECT a.id, a.musicbrainz_id, a.name, a.disambiguation, "
        "sa.credited_name, sa.song_id FROM artists a "
        "JOIN song_artists sa ON sa.artist_id = a.id "
        "JOIN songs s ON s.id = sa.song_id "
        "WHERE s.enabled = 1 AND s.preview_url <> '' "
        "ORDER BY a.id, sa.song_id",
    ).fetchall()

    artists: dict[int, dict[str, object]] = {}
    for row in rows:
        artist = artists.setdefault(
            int(row["id"]),
            {
                "musicbrainz_id": str(row["musicbrainz_id"]),
                "name": str(row["name"]),
                "disambiguation": row["disambiguation"],
                "credited_names": set(),
                "song_ids": set(),
            },
        )
        artist["credited_names"].add(str(row["credited_name"]))
        artist["song_ids"].add(int(row["song_id"]))

    ranked: list[tuple[tuple[object, ...], dict[str, object], str]] = []
    for artist in artists.values():
        canonical_name = str(artist["name"])
        credited_names = sorted(
            artist["credited_names"],
            key=lambda value: (_normalize_search_text(value), value),
        )
        aliases = [canonical_name, *credited_names]
        best_alias = min(
            aliases,
            key=lambda value: _artist_alias_rank(value, normalized_query),
        )
        alias_rank = _artist_alias_rank(best_alias, normalized_query)
        if alias_rank[0] == 4:
            continue
        ranked.append(
            (
                (
                    *alias_rank,
                    -len(artist["song_ids"]),
                    _normalize_search_text(canonical_name),
                    str(artist["musicbrainz_id"]),
                ),
                artist,
                best_alias,
            )
        )

    ranked.sort(key=lambda item: item[0])
    return [
        ArtistOption(
            id=str(artist["musicbrainz_id"]),
            name=display_name,
            disambiguation=artist["disambiguation"],
            song_count=len(artist["song_ids"]),
        )
        for _, artist, display_name in ranked[:limit]
    ]


def _artist_alias_rank(alias: str, query: str) -> tuple[object, ...]:
    """Rank a structured canonical or credited name without parsing punctuation."""
    normalized_alias = _normalize_search_text(alias)
    compact_alias = normalized_alias.replace(" ", "")
    compact_query = query.replace(" ", "")
    query_tokens = query.split()

    if compact_alias == compact_query:
        tier = 0
    elif compact_alias.startswith(compact_query):
        tier = 1
    elif compact_query in compact_alias:
        tier = 2
    elif query_tokens and all(token in normalized_alias for token in query_tokens):
        tier = 3
    else:
        tier = 4

    return (
        tier,
        -_fuzzy_similarity(compact_query, compact_alias),
        normalized_alias,
    )


def get_song(connection: sqlite3.Connection, song_id: int) -> SongReveal | None:
    row = connection.execute(
        "SELECT id, title, artist, album, release_year, artwork_url, popularity_score, "
        "preview_url, apple_music_url, spotify_url "
        "FROM songs WHERE id = ? AND enabled = 1",
        (song_id,),
    ).fetchone()
    if row is None:
        return None

    genre_rows = connection.execute(
        "SELECT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "LEFT JOIN song_genre_evidence sge "
        "ON sge.song_id = sg.song_id AND sge.genre_id = sg.genre_id "
        "WHERE sg.song_id = ? "
        "ORDER BY COALESCE(sge.rank, 999), g.name COLLATE NOCASE",
        (song_id,),
    ).fetchall()
    return SongReveal(
        id=row["id"],
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        release_year=row["release_year"],
        artwork_url=row["artwork_url"],
        popularity_score=row["popularity_score"],
        genres=[genre["name"] for genre in genre_rows],
        preview_url=row["preview_url"],
        apple_music_url=row["apple_music_url"],
        spotify_url=row["spotify_url"],
    )


def get_filter_metadata(connection: sqlite3.Connection) -> FilterMetadata:
    summary = connection.execute(
        "SELECT MIN(release_year) AS year_min, MAX(release_year) AS year_max, "
        "COUNT(*) AS song_count "
        "FROM songs WHERE enabled = 1 AND popularity_score IS NOT NULL"
    ).fetchone()
    genre_rows = connection.execute(
        "SELECT DISTINCT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "JOIN songs s ON s.id = sg.song_id "
        "WHERE s.enabled = 1 AND s.popularity_score IS NOT NULL "
        "ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    country_rows = connection.execute(
        "SELECT DISTINCT c.code FROM countries c "
        "JOIN song_countries sc ON sc.country_id = c.id "
        "JOIN songs s ON s.id = sc.song_id "
        "WHERE s.enabled = 1 AND s.popularity_score IS NOT NULL ORDER BY c.code"
    ).fetchall()
    return FilterMetadata(
        genres=[row["name"] for row in genre_rows],
        countries=[row["code"] for row in country_rows],
        year_min=summary["year_min"],
        year_max=summary["year_max"],
        popularity_min=0,
        popularity_max=100,
        song_count=summary["song_count"],
    )


def _context_clauses(
    request: FilterContextRequest,
    *,
    genres: bool = False,
    countries: bool = False,
    years: bool = False,
    popularity: bool = False,
) -> tuple[list[str], list[object]]:
    clauses = ["s.enabled = 1", "s.preview_url <> ''", "s.popularity_score IS NOT NULL"]
    parameters: list[object] = []
    if request.artist_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM song_artists sa "
            "JOIN artists a ON a.id = sa.artist_id "
            "WHERE sa.song_id = s.id AND a.musicbrainz_id = ?)"
        )
        parameters.append(request.artist_id)
    if genres:
        normalized_genres = sorted(
            {genre.strip().lower() for genre in request.genres if genre.strip()}
        )
        if normalized_genres:
            placeholders = ", ".join("?" for _ in normalized_genres)
            clauses.append(
                "EXISTS (SELECT 1 FROM song_genres sg "
                "JOIN genres g ON g.id = sg.genre_id "
                f"WHERE sg.song_id = s.id AND g.name IN ({placeholders}))"
            )
            parameters.extend(normalized_genres)
    if countries:
        normalized_countries = sorted(
            {country.strip().upper() for country in request.countries if country.strip()}
        )
        if normalized_countries:
            placeholders = ", ".join("?" for _ in normalized_countries)
            clauses.append(
                "EXISTS (SELECT 1 FROM song_countries sc "
                "JOIN countries c ON c.id = sc.country_id "
                f"WHERE sc.song_id = s.id AND c.code IN ({placeholders}))"
            )
            parameters.extend(normalized_countries)
    if years:
        if request.year_min is not None:
            clauses.append("s.release_year >= ?")
            parameters.append(request.year_min)
        if request.year_max is not None:
            clauses.append("s.release_year <= ?")
            parameters.append(request.year_max)
    if popularity:
        if request.popularity_min is not None:
            clauses.append("s.popularity_score >= ?")
            parameters.append(request.popularity_min)
        if request.popularity_max is not None:
            clauses.append("s.popularity_score <= ?")
            parameters.append(request.popularity_max)
    return clauses, parameters


def get_contextual_filter_metadata(
    connection: sqlite3.Connection, request: FilterContextRequest
) -> FilterMetadata:
    """Return progressive facets; every facet only applies selections before it."""

    genre_clauses, genre_parameters = _context_clauses(request)
    genre_rows = connection.execute(
        "SELECT DISTINCT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "JOIN songs s ON s.id = sg.song_id "
        f"WHERE {' AND '.join(genre_clauses)} ORDER BY g.name COLLATE NOCASE",
        genre_parameters,
    ).fetchall()

    country_clauses, country_parameters = _context_clauses(request, genres=True)
    country_rows = connection.execute(
        "SELECT DISTINCT c.code FROM countries c "
        "JOIN song_countries sc ON sc.country_id = c.id "
        "JOIN songs s ON s.id = sc.song_id "
        f"WHERE {' AND '.join(country_clauses)} ORDER BY c.code",
        country_parameters,
    ).fetchall()

    year_clauses, year_parameters = _context_clauses(request, genres=True, countries=True)
    year_summary = connection.execute(
        "SELECT MIN(s.release_year) AS year_min, MAX(s.release_year) AS year_max "
        f"FROM songs s WHERE {' AND '.join(year_clauses)}",
        year_parameters,
    ).fetchone()

    popularity_clauses, popularity_parameters = _context_clauses(
        request, genres=True, countries=True, years=True
    )
    popularity_summary = connection.execute(
        "SELECT MIN(s.popularity_score) AS popularity_min, "
        "MAX(s.popularity_score) AS popularity_max "
        f"FROM songs s WHERE {' AND '.join(popularity_clauses)}",
        popularity_parameters,
    ).fetchone()

    count_clauses, count_parameters = _context_clauses(
        request, genres=True, countries=True, years=True, popularity=True
    )
    count_row = connection.execute(
        f"SELECT COUNT(*) AS song_count FROM songs s WHERE {' AND '.join(count_clauses)}",
        count_parameters,
    ).fetchone()
    return FilterMetadata(
        genres=[row["name"] for row in genre_rows],
        countries=[row["code"] for row in country_rows],
        year_min=year_summary["year_min"],
        year_max=year_summary["year_max"],
        popularity_min=popularity_summary["popularity_min"] or 0,
        popularity_max=popularity_summary["popularity_max"] or 0,
        song_count=count_row["song_count"],
    )
