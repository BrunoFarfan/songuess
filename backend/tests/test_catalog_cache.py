import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from dataset.populate import initialize_database

from app import catalog_cache
from app.catalog_cache import CatalogCache, cache_catalog
from app.database import D1Database, SQLiteDatabase, connect
from app.models import FilterContextRequest, RoundRequest
from app.repository import (
    choose_round_async,
    get_contextual_filter_metadata_async,
    search_songs_async,
)
from app.search_index import rebuild_search_index


class LocalD1:
    """Run the real production queries through D1's binding shape."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.calls: list[str] = []

    def prepare(self, sql: str):
        binding = self

        class Statement:
            parameters: tuple[object, ...] = ()

            def bind(self, *parameters: object):
                self.parameters = parameters
                return self

            async def run(self):
                binding.calls.append(sql)
                rows = binding.connection.execute(sql, self.parameters).fetchall()
                return SimpleNamespace(results=[dict(row) for row in rows])

            async def first(self):
                result = await self.run()
                return result.results[0] if result.results else None

        return Statement()


@pytest.fixture
def catalog(tmp_path: Path):
    path = tmp_path / "catalog.sqlite3"
    initialize_database(path)
    with connect(path) as connection:
        connection.executemany(
            "INSERT INTO songs (id, title, artist, release_year, popularity_score, preview_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "Same", "B", 2000, 80, "https://audio/1"),
                (2, "Same", "A", 2000, 80, "https://audio/2"),
                (3, "Same", "A", 2000, 80, "https://audio/3"),
                (4, "Other", "C", 2010, 30, "https://audio/4"),
            ],
        )
        rebuild_search_index(connection)
        yield connection


def test_cache_expiry_and_bounded_eviction(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(catalog_cache, "monotonic", lambda: now[0])
    cache = CatalogCache(ttl=5, max_entries=2, max_rows=3)
    cache.put("a", "a", 2)
    cache.put("b", "b", 1)
    assert cache.get("a") == "a"  # Keep the recently used entry when capacity is reached.
    cache.put("c", "c", 1)
    assert cache.get("b") is catalog_cache._MISSING
    cache.put("too-big", "big", 4)
    assert cache.rows == 3 and len(cache.entries) == 2
    now[0] = 5
    assert cache.get("a") is catalog_cache._MISSING
    assert cache.get("c") is catalog_cache._MISSING
    assert cache.rows == 0


def test_failed_load_is_not_cached() -> None:
    class Database:
        catalog_cache = CatalogCache()

    attempts = []

    @cache_catalog()
    async def load(_database):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary database failure")
        return 42

    database = Database()
    with pytest.raises(RuntimeError):
        asyncio.run(load(database))
    assert asyncio.run(load(database)) == 42
    assert asyncio.run(load(database)) == 42
    assert len(attempts) == 2


def test_browse_pages_share_count_and_use_ordered_index(catalog) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    first, total = asyncio.run(search_songs_async(database, "", limit=2))
    second, _ = asyncio.run(search_songs_async(database, "", limit=2, offset=2))
    assert [song.id for song in first + second] == [4, 2, 3, 1]
    assert total == 4
    assert len(binding.calls) == 3  # One count and two ordered page reads.
    asyncio.run(search_songs_async(database, "", limit=2))
    assert len(binding.calls) == 3
    plan = catalog.execute("EXPLAIN QUERY PLAN " + binding.calls[-1], (2, 2)).fetchall()
    details = " ".join(str(row["detail"]) for row in plan)
    assert "idx_song_search_browse" in details
    assert "TEMP B-TREE" not in details


@pytest.mark.parametrize(
    ("offset", "limit"), [(0, 2), (1, 2), (2, 2), (3, 3), (0, 10), (4, 2), (6, 2)]
)
def test_browse_from_either_end_preserves_pages(catalog, offset, limit) -> None:
    binding = LocalD1(catalog)
    expected = [4, 2, 3, 1][offset : offset + limit]
    results, total = asyncio.run(search_songs_async(D1Database(binding), "", limit, offset))
    assert [song.id for song in results] == expected
    assert total == 4
    if offset >= total:
        assert len(binding.calls) == 1  # No page query past the end of the catalog.
    elif offset + min(limit, total - offset) == total and offset > 0:
        assert "ss.song_id DESC" in binding.calls[-1]


def test_reverse_browse_respects_disabled_songs_and_duplicate_titles(catalog) -> None:
    # A stale search row must not change eligibility or ascending title/artist/ID order.
    catalog.execute("UPDATE songs SET enabled = 0 WHERE id = 1")
    results, total = asyncio.run(search_songs_async(D1Database(LocalD1(catalog)), "", 2, 1))
    assert total == 3
    assert [song.id for song in results] == [2, 3]


def test_round_pool_preserves_uniform_choices_and_filter_boundaries(catalog, monkeypatch) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    choices = []

    def choose(pool):
        choices.append({song[0] for song in pool})
        return pool[-1]

    monkeypatch.setattr("app.repository.secrets.choice", choose)
    request = RoundRequest(year_min=1990, year_max=2005, popularity_min=50, popularity_max=100)
    first = asyncio.run(choose_round_async(database, request))
    second = asyncio.run(
        choose_round_async(database, request.model_copy(update={"exclude_ids": [first.song_id]}))
    )
    assert choices == [{1, 2, 3}, {1, 2, 3} - {first.song_id}]
    assert second.song_id != first.song_id
    assert len(binding.calls) == 1
    other = request.model_copy(update={"year_max": 2020, "popularity_min": 0})
    asyncio.run(choose_round_async(database, other))
    assert choices[-1] == {1, 2, 3, 4}
    assert len(binding.calls) == 2


def test_context_cache_refreshes_and_sqlite_stays_uncached(catalog, monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(catalog_cache, "monotonic", lambda: now[0])
    binding = LocalD1(catalog)
    database = D1Database(binding, catalog_cache=CatalogCache(ttl=5))
    request = FilterContextRequest()
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 4
    catalog.execute("UPDATE songs SET enabled = 0 WHERE id = 4")
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 4
    assert len(binding.calls) == 5
    local = SQLiteDatabase(catalog)
    assert asyncio.run(get_contextual_filter_metadata_async(local, request)).song_count == 3
    now[0] = 5
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 3
    assert len(binding.calls) == 10
