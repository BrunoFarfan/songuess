import asyncio
import json
import secrets
import sqlite3
from array import array
from difflib import SequenceMatcher
from functools import lru_cache
from math import gcd

from app.catalog_cache import cache_catalog
from app.catalog_snapshot import CatalogSnapshot
from app.database import D1Database, Database, Row, SQLiteDatabase
from app.models import (
    ArtistOption,
    FilterContextRequest,
    FilterMetadata,
    RoundRequest,
    RoundResponse,
    SongReveal,
    SongSearchResult,
)
from app.search_index import normalize_search_text as _normalize_search_text


def _round_filter(request: RoundRequest) -> tuple[list[str], list[object]]:
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
        clauses.append("s.id NOT IN (SELECT CAST(value AS INTEGER) FROM json_each(?))")
        parameters.append(json.dumps(excluded_ids, separators=(",", ":")))

    return clauses, parameters


@cache_catalog()
async def _catalog_snapshot_async(database: Database) -> CatalogSnapshot:
    row = await database.fetch_one("SELECT version, payload FROM catalog_runtime WHERE id = 1")
    if row is None:
        raise RuntimeError("Catalog snapshot missing; refresh the catalog_runtime snapshot")
    return CatalogSnapshot.from_payload(str(row["payload"]), str(row["version"]))


async def choose_round_async(database: Database, request: RoundRequest) -> RoundResponse | None:
    if isinstance(database, D1Database):
        snapshot = await _catalog_snapshot_async(database)
        available = snapshot.eligible_ids(request, exclude=True)
        clauses, parameters = _round_filter(request.model_copy(update={"exclude_ids": []}))
        for attempt in range(3):
            if not available:
                break
            song_id = secrets.choice(available)
            row = await database.fetch_one(
                "SELECT s.id, s.preview_url FROM songs s WHERE s.id = ? "
                f"AND s.preview_url <> '' AND {' AND '.join(clauses)}",
                (song_id, *parameters),
            )
            if row is not None:
                return RoundResponse(song_id=int(row["id"]), preview_url=str(row["preview_url"]))
            # Validate the selected row against live filters: an import can reuse IDs.
            # Refresh once and bound retries during an in-progress catalog replacement.
            if attempt == 0:
                database.catalog_cache.clear()
                snapshot = await _catalog_snapshot_async(database)
                available = snapshot.eligible_ids(request, exclude=True)
            available = [candidate for candidate in available if candidate != song_id]
        return None

    clauses, parameters = _round_filter(request)
    where = " AND ".join(clauses)
    count_row = await database.fetch_one(
        f"SELECT COUNT(*) AS total FROM songs s WHERE {where}", parameters
    )
    total = int(count_row["total"]) if count_row is not None else 0
    if total == 0:
        return None

    row = await database.fetch_one(
        f"SELECT s.id, s.preview_url FROM songs s WHERE {where} ORDER BY s.id LIMIT 1 OFFSET ?",
        [*parameters, secrets.randbelow(total)],
    )
    if row is None:
        return None
    return RoundResponse(song_id=row["id"], preview_url=row["preview_url"])


def _fuzzy_similarity(query: str, candidate: str) -> float:
    if not candidate:
        return 0.0
    return SequenceMatcher(None, query, candidate, autojunk=False).ratio()


def _trigram_match_query(query: str) -> str:
    compact = query.replace(" ", "")
    trigrams = sorted({compact[index : index + 3] for index in range(len(compact) - 2)})
    return " OR ".join(f'"{trigram}"' for trigram in trigrams)


async def _song_candidates(database: Database, query: str, candidate_limit: int) -> list[Row]:
    # Keep FTS's indexed rank/limit inside a materialized pool. All subsequent
    # ranking touches at most 500 rows, never the entire catalog.
    compact = query.replace(" ", "")
    fields = (
        "ss.normalized_title_compact",
        "ss.normalized_artist_compact",
        "ss.normalized_album_compact",
        "ss.normalized_year",
    )
    parameters: list[object]
    if len(compact) < 3:
        pool = (
            "SELECT song_id, 0 AS relevance FROM ("
            "SELECT song_id FROM song_search WHERE normalized_title_compact LIKE ? "
            "UNION SELECT song_id FROM song_search WHERE normalized_artist_compact LIKE ? "
            "UNION SELECT song_id FROM song_search WHERE normalized_album_compact LIKE ?"
            ") LIMIT ?"
        )
        parameters = [f"{compact}%"] * 3 + [candidate_limit]
    else:
        pool = (
            "SELECT CAST(f.song_id AS INTEGER) AS song_id, f.rank AS relevance "
            "FROM song_search_fts f WHERE song_search_fts MATCH ? "
            "ORDER BY f.rank LIMIT ?"
        )
        parameters = [_trigram_match_query(query), candidate_limit]

    exact = " OR ".join(f"{field} = ?" for field in fields)
    prefix = " OR ".join(f"{field} LIKE ?" for field in fields)
    substring = " OR ".join(f"instr({field}, ?) > 0" for field in fields)
    tokens = " AND ".join("instr(ss.normalized_text, ?) > 0" for _ in query.split())
    tier = f"CASE WHEN {exact} THEN 0 WHEN {prefix} THEN 1 WHEN {substring} THEN 2 "
    tier += f"WHEN {tokens} THEN 3 ELSE 4 END"
    priority = (
        "CASE "
        + " ".join(f"WHEN {field} = ? THEN {index}" for index, field in enumerate(fields))
        + " ELSE 4 END"
    )
    parameters += [compact] * 4 + [f"{compact}%"] * 4 + [compact] * 4
    parameters += query.split() + [compact] * 4
    return await database.fetch_all(
        "WITH candidates AS MATERIALIZED (" + pool + "), ranked AS ("
        "SELECT s.id, s.title, s.artist, s.album, s.release_year, s.artwork_url, "
        "s.popularity_score, ss.normalized_title, ss.normalized_artist, "
        f"ss.normalized_album, ss.normalized_year, c.relevance, "
        f"{tier} AS tier, {priority} AS priority "
        "FROM candidates c JOIN songs s ON s.id = c.song_id "
        "JOIN song_search ss ON ss.song_id = s.id "
        "WHERE s.enabled = 1 AND s.preview_url <> '') "
        "SELECT * FROM ranked ORDER BY tier, priority, "
        "CASE WHEN tier >= 3 THEN relevance ELSE 0 END, "
        "COALESCE(popularity_score, 0) DESC, normalized_title, normalized_artist, id",
        parameters,
    )


@cache_catalog(row_count=lambda ids: (len(ids) + 15) // 16)
async def _browse_ids_async(database: Database) -> array:
    # Compact 64-bit IDs cost 128 bytes per 16 IDs, well below a hydrated song row.
    # Charge these blocks to the bounded row budget; hydrate only the requested page.
    if isinstance(database, D1Database):
        snapshot = await _catalog_snapshot_async(database)
        if len(snapshot.songs) == snapshot.browse_count:
            return array("q", sorted(snapshot.songs))
        # Round snapshots omit songs without popularity; browsing still includes them.
    rows = await database.fetch_all(
        "SELECT id FROM songs WHERE enabled = 1 AND preview_url <> '' ORDER BY id"
    )
    return array("q", (int(row["id"]) for row in rows))


@lru_cache(maxsize=128)
def _browse_permutation(total: int, seed: int) -> tuple[int, int]:
    # Mix once per catalog/seed, then choose an invertible affine permutation.
    # A coprime step visits every ID exactly once without hashing or cycle walks.
    mixed = (seed ^ 0x9E3779B9) & 0xFFFFFFFF
    mixed = ((mixed ^ (mixed >> 16)) * 0x85EBCA6B) & 0xFFFFFFFF
    mixed = ((mixed ^ (mixed >> 13)) * 0xC2B2AE35) & 0xFFFFFFFF
    mixed ^= mixed >> 16
    step = mixed % total or 1
    while gcd(step, total) != 1:
        step = (step + 1) % total or 1
    return step, ((mixed >> 16) ^ seed) % total


def _permuted_browse_index(index: int, total: int, seed: int) -> int:
    """Return a stable, seeded bijection without building a shuffled catalog."""
    if total <= 1:
        return index
    step, start = _browse_permutation(total, seed)
    return (start + index * step) % total


@cache_catalog(row_count=len)
async def _ranked_search_rows_async(database: Database, query: str) -> list[Row]:
    # Rank a bounded candidate pool once, then reuse it for stable pages.
    rows = await _song_candidates(database, query, 500)
    # Fuzzy matching is limited to the strongest 40 indexed candidates. Reuse
    # field scores (artists/albums recur) and stored normalized values; never
    # normalize or run edit matching for all 500 returned candidate documents.
    fuzzy = [row for row in rows if row["tier"] == 4][:40]
    scores: dict[str, float] = {}
    compact = query.replace(" ", "")

    def rank(row: Row) -> tuple[object, ...]:
        fields = tuple(
            row[name].replace(" ", "")
            for name in (
                "normalized_title",
                "normalized_artist",
                "normalized_album",
                "normalized_year",
            )
        )
        best = 0.0
        for value in (*fields, "".join(fields)):
            if value not in scores:
                scores[value] = _fuzzy_similarity(compact, value)
            best = max(best, scores[value])
        return (
            -best,
            -int(row["popularity_score"] or 0),
            row["normalized_title"],
            row["normalized_artist"],
            int(row["id"]),
        )

    ranked_fuzzy = sorted(fuzzy, key=rank)
    fuzzy_ids = {int(row["id"]) for row in fuzzy}
    # Insert reranked fuzzy candidates at their original tier boundary; weaker
    # FTS candidates remain available on subsequent stable pages.
    strong = [row for row in rows if row["tier"] < 4]
    remaining = [row for row in rows if row["tier"] == 4 and int(row["id"]) not in fuzzy_ids]
    return [*strong, *ranked_fuzzy, *remaining]


@cache_catalog(row_count=lambda result: len(result[0]) + 1)
async def search_songs_async(
    database: Database,
    query: str,
    limit: int = 40,
    offset: int = 0,
    seed: int = 0,
) -> tuple[list[SongSearchResult], int]:
    if isinstance(database, SQLiteDatabase):
        database.ensure_search_index()
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        ids = await _browse_ids_async(database)
        total = len(ids)
        page_ids = [
            ids[_permuted_browse_index(position, total, seed)]
            for position in range(offset, min(offset + limit, total))
        ]
        if not page_ids:
            return [], total
        rows = await database.fetch_all(
            "SELECT s.id, s.title, s.artist, s.album, s.release_year, "
            "s.artwork_url, s.popularity_score FROM json_each(?) page "
            "CROSS JOIN songs s ON s.id = CAST(page.value AS INTEGER) "
            "WHERE s.enabled = 1 AND s.preview_url <> ''",
            [json.dumps(page_ids, separators=(",", ":"))],
        )
        by_id = {int(row["id"]): row for row in rows}
        page_rows = [by_id[song_id] for song_id in page_ids if song_id in by_id]
    else:
        ordered_rows = await _ranked_search_rows_async(database, normalized_query)
        total = len(ordered_rows)
        page_rows = ordered_rows[offset : offset + limit]
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
        for row in page_rows
    ], total


async def search_artists_async(
    database: Database, query: str, limit: int = 10
) -> list[ArtistOption]:
    if isinstance(database, SQLiteDatabase):
        database.ensure_search_index()
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        return []

    compact_query = normalized_query.replace(" ", "")
    if len(compact_query) < 3:
        candidate_rows = await database.fetch_all(
            "SELECT DISTINCT artist_id FROM artist_search_aliases "
            "WHERE normalized_alias_compact LIKE ? LIMIT 100",
            [f"{compact_query}%"],
        )
    else:
        candidate_rows = await database.fetch_all(
            "SELECT DISTINCT CAST(artist_id AS INTEGER) AS artist_id "
            "FROM artist_search_fts WHERE artist_search_fts MATCH ? LIMIT 100",
            [_trigram_match_query(normalized_query)],
        )
    artist_ids = sorted({int(row["artist_id"]) for row in candidate_rows})
    if not artist_ids:
        return []

    rows = await database.fetch_all(
        "SELECT a.id, a.musicbrainz_id, a.name, a.disambiguation, "
        "sa.credited_name, sa.song_id FROM artists a "
        "JOIN song_artists sa ON sa.artist_id = a.id "
        "JOIN songs s ON s.id = sa.song_id "
        "WHERE s.enabled = 1 AND s.preview_url <> '' "
        "AND a.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
        "ORDER BY a.id, sa.song_id",
        [json.dumps(artist_ids, separators=(",", ":"))],
    )

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


async def get_song_async(database: Database, song_id: int) -> SongReveal | None:
    row = await database.fetch_one(
        "SELECT id, title, artist, album, release_year, artwork_url, popularity_score, "
        "preview_url, apple_music_url, spotify_url "
        "FROM songs WHERE id = ? AND enabled = 1",
        (song_id,),
    )
    if row is None:
        return None

    genre_rows = await database.fetch_all(
        "SELECT g.name FROM genres g "
        "JOIN song_genres sg ON sg.genre_id = g.id "
        "LEFT JOIN song_genre_evidence sge "
        "ON sge.song_id = sg.song_id AND sge.genre_id = sg.genre_id "
        "WHERE sg.song_id = ? "
        "ORDER BY COALESCE(sge.rank, 999), g.name COLLATE NOCASE",
        (song_id,),
    )
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


@cache_catalog()
async def get_filter_metadata_async(database: Database) -> FilterMetadata:
    if isinstance(database, D1Database):
        # The initial page needs only small facets, not the full relation snapshot.
        # Indexed year endpoints cost one row each; JSON projections read one row.
        row = await database.fetch_one(
            "SELECT json_extract(payload, '$.genres') AS genres, "
            "json_extract(payload, '$.countries') AS countries, "
            "(SELECT release_year FROM songs WHERE enabled = 1 AND preview_url <> '' "
            "AND popularity_score IS NOT NULL ORDER BY release_year LIMIT 1) AS year_min, "
            "(SELECT release_year FROM songs WHERE enabled = 1 AND preview_url <> '' "
            "AND popularity_score IS NOT NULL ORDER BY release_year DESC LIMIT 1) AS year_max, "
            "json_array_length(payload, '$.songs') AS song_count "
            "FROM catalog_runtime WHERE id = 1"
        )
        if row is None:
            raise RuntimeError("Catalog snapshot missing; apply migration 014")
        return FilterMetadata(
            genres=sorted((name for _, name in json.loads(row["genres"])), key=str.casefold),
            countries=sorted(code for _, code in json.loads(row["countries"])),
            year_min=row["year_min"],
            year_max=row["year_max"],
            popularity_min=0,
            popularity_max=100,
            song_count=row["song_count"],
        )
    summary = await database.fetch_one(
        "SELECT MIN(release_year) AS year_min, MAX(release_year) AS year_max, "
        "COUNT(*) AS song_count "
        "FROM songs WHERE enabled = 1 AND popularity_score IS NOT NULL"
    )
    if summary is None:
        raise RuntimeError("Database did not return catalog metadata")
    genre_rows = await database.fetch_all(
        "SELECT g.name FROM genres g WHERE EXISTS ("
        "SELECT 1 FROM song_genres sg CROSS JOIN songs s ON s.id = sg.song_id "
        "WHERE sg.genre_id = g.id AND s.enabled = 1 AND s.popularity_score IS NOT NULL) "
        "ORDER BY g.name COLLATE NOCASE"
    )
    country_rows = await database.fetch_all(
        "SELECT c.code FROM countries c WHERE EXISTS ("
        "SELECT 1 FROM song_countries sc CROSS JOIN songs s ON s.id = sc.song_id "
        "WHERE sc.country_id = c.id AND s.enabled = 1 AND s.popularity_score IS NOT NULL) "
        "ORDER BY c.code"
    )
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


@cache_catalog()
async def get_contextual_filter_metadata_async(
    database: Database, request: FilterContextRequest
) -> FilterMetadata:
    """Return progressive facets; every facet only applies selections before it."""

    if isinstance(database, D1Database):
        return FilterMetadata.model_validate(
            (await _catalog_snapshot_async(database)).filter_metadata(request)
        )

    genre_clauses, genre_parameters = _context_clauses(request)
    genre_rows = await database.fetch_all(
        "SELECT g.name FROM genres g WHERE EXISTS ("
        "SELECT 1 FROM song_genres sg CROSS JOIN songs s ON s.id = sg.song_id "
        f"WHERE sg.genre_id = g.id AND {' AND '.join(genre_clauses)}) "
        "ORDER BY g.name COLLATE NOCASE",
        genre_parameters,
    )

    country_clauses, country_parameters = _context_clauses(request, genres=True)
    country_rows = await database.fetch_all(
        "SELECT c.code FROM countries c WHERE EXISTS ("
        "SELECT 1 FROM song_countries sc CROSS JOIN songs s ON s.id = sc.song_id "
        f"WHERE sc.country_id = c.id AND {' AND '.join(country_clauses)}) ORDER BY c.code",
        country_parameters,
    )

    year_clauses, year_parameters = _context_clauses(request, genres=True, countries=True)
    year_summary = await database.fetch_one(
        "SELECT MIN(s.release_year) AS year_min, MAX(s.release_year) AS year_max "
        f"FROM songs s WHERE {' AND '.join(year_clauses)}",
        year_parameters,
    )

    popularity_clauses, popularity_parameters = _context_clauses(
        request, genres=True, countries=True, years=True
    )
    popularity_summary = await database.fetch_one(
        "SELECT MIN(s.popularity_score) AS popularity_min, "
        "MAX(s.popularity_score) AS popularity_max "
        f"FROM songs s WHERE {' AND '.join(popularity_clauses)}",
        popularity_parameters,
    )

    count_clauses, count_parameters = _context_clauses(
        request, genres=True, countries=True, years=True, popularity=True
    )
    count_row = await database.fetch_one(
        f"SELECT COUNT(*) AS song_count FROM songs s WHERE {' AND '.join(count_clauses)}",
        count_parameters,
    )
    if year_summary is None or popularity_summary is None or count_row is None:
        raise RuntimeError("Database did not return contextual filter metadata")
    return FilterMetadata(
        genres=[row["name"] for row in genre_rows],
        countries=[row["code"] for row in country_rows],
        year_min=year_summary["year_min"],
        year_max=year_summary["year_max"],
        popularity_min=popularity_summary["popularity_min"] or 0,
        popularity_max=popularity_summary["popularity_max"] or 0,
        song_count=count_row["song_count"],
    )


# Preserve the synchronous repository API used by local catalog tooling and tests.
# The FastAPI runtime calls the async variants directly.
def choose_round(connection: sqlite3.Connection, request: RoundRequest) -> RoundResponse | None:
    return asyncio.run(choose_round_async(SQLiteDatabase(connection), request))


def search_songs(
    connection: sqlite3.Connection,
    query: str,
    limit: int = 40,
    offset: int = 0,
    seed: int = 0,
) -> list[SongSearchResult]:
    items, _ = asyncio.run(
        search_songs_async(SQLiteDatabase(connection), query, limit=limit, offset=offset, seed=seed)
    )
    return items


def count_searchable_songs(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM songs WHERE enabled = 1 AND preview_url <> ''"
    ).fetchone()
    return int(row["total"])


def search_artists(
    connection: sqlite3.Connection, query: str, limit: int = 10
) -> list[ArtistOption]:
    return asyncio.run(search_artists_async(SQLiteDatabase(connection), query, limit=limit))


def get_song(connection: sqlite3.Connection, song_id: int) -> SongReveal | None:
    return asyncio.run(get_song_async(SQLiteDatabase(connection), song_id))


def get_filter_metadata(connection: sqlite3.Connection) -> FilterMetadata:
    return asyncio.run(get_filter_metadata_async(SQLiteDatabase(connection)))


def get_contextual_filter_metadata(
    connection: sqlite3.Connection, request: FilterContextRequest
) -> FilterMetadata:
    return asyncio.run(get_contextual_filter_metadata_async(SQLiteDatabase(connection), request))
