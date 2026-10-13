import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from dataset.populate import initialize_database

from app import catalog_cache
from app.catalog_cache import CatalogCache, cache_catalog
from app.catalog_snapshot import SNAPSHOT_REFRESH_SQL
from app.database import D1Database, SQLiteDatabase, connect
from app.models import FilterContextRequest, RoundRequest
from app.repository import (
    _browse_ids_async,
    _permuted_browse_index,
    choose_round_async,
    get_contextual_filter_metadata_async,
    get_filter_metadata_async,
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
        connection.executescript(SNAPSHOT_REFRESH_SQL)
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


def test_random_browse_pages_share_ids_and_use_primary_key_reads(catalog) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    first, total = asyncio.run(search_songs_async(database, "", limit=2, seed=17))
    second, _ = asyncio.run(search_songs_async(database, "", limit=2, offset=2, seed=17))
    assert {song.id for song in first + second} == {1, 2, 3, 4}
    assert total == 4
    assert len(binding.calls) == 3  # One shared snapshot read and two detail page reads.
    asyncio.run(search_songs_async(database, "", limit=2, seed=17))
    assert len(binding.calls) == 3
    asyncio.run(search_songs_async(database, "", limit=2, seed=18))
    assert len(binding.calls) == 4  # A new seed shares the cached eligible ID list.
    import json

    plan = catalog.execute("EXPLAIN QUERY PLAN " + binding.calls[-1], (json.dumps([1, 2]),))
    details = " ".join(str(row["detail"]) for row in plan)
    assert "INTEGER PRIMARY KEY" in details
    assert "TEMP B-TREE" not in details
    assert not any("random()" in sql.lower() for sql in binding.calls)
    assert database.catalog_cache.rows <= database.catalog_cache.max_rows


def test_random_browse_reuses_warm_context_snapshot_without_scanning_ids(catalog) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    asyncio.run(get_contextual_filter_metadata_async(database, FilterContextRequest()))
    assert len(binding.calls) == 1
    assert "catalog_runtime" in binding.calls[0]
    results, total = asyncio.run(search_songs_async(database, "", 4, seed=17))
    assert {song.id for song in results} == {1, 2, 3, 4}
    assert total == 4
    assert len(binding.calls) == 2
    assert "json_each(?) page" in binding.calls[-1]
    assert not any(sql.startswith("SELECT id FROM songs") for sql in binding.calls)


def test_random_browse_falls_back_to_ids_when_snapshot_omits_missing_popularity(catalog) -> None:
    catalog.execute("UPDATE songs SET popularity_score = NULL WHERE id = 4")
    catalog.executescript(SNAPSHOT_REFRESH_SQL)
    binding = LocalD1(catalog)
    database = D1Database(binding)
    asyncio.run(get_contextual_filter_metadata_async(database, FilterContextRequest()))
    results, total = asyncio.run(search_songs_async(database, "", 4, seed=17))
    assert {song.id for song in results} == {1, 2, 3, 4}
    assert next(song for song in results if song.id == 4).popularity_score is None
    assert total == 4
    assert len(binding.calls) == 3
    assert binding.calls[1].startswith("SELECT id FROM songs")
    asyncio.run(search_songs_async(database, "", 2, seed=18))
    assert len(binding.calls) == 4  # The fallback IDs are also cached across client seeds.


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7, 16, 17, 37, 129])
@pytest.mark.parametrize("seed", [0, 17, 4_294_967_295])
def test_seeded_browse_permutation_is_bijective(total, seed) -> None:
    positions = [_permuted_browse_index(index, total, seed) for index in range(total)]
    assert sorted(positions) == list(range(total))


def test_compact_browse_ids_above_row_budget_are_still_cached() -> None:
    class LargeCatalog:
        catalog_cache = CatalogCache(max_rows=30_000)
        calls = 0

        async def fetch_all(self, _statement):
            self.calls += 1
            return [{"id": index} for index in range(1, 40_002)]

    database = LargeCatalog()
    ids = asyncio.run(_browse_ids_async(database))
    assert len(ids) == 40_001
    assert ids.itemsize == 8
    assert asyncio.run(_browse_ids_async(database)) is ids
    assert database.calls == 1
    assert database.catalog_cache.rows == 2_501


def test_random_browse_cache_stays_bounded_and_seed_survives_eviction(catalog) -> None:
    database = D1Database(LocalD1(catalog), catalog_cache=CatalogCache(max_rows=10))
    first, _ = asyncio.run(search_songs_async(database, "", 4, seed=17))
    for seed in range(18, 30):
        asyncio.run(search_songs_async(database, "", 4, seed=seed))
        assert database.catalog_cache.rows <= 10
    repeated, _ = asyncio.run(search_songs_async(database, "", 4, seed=17))
    assert [song.id for song in first] == [song.id for song in repeated]


@pytest.mark.parametrize(
    ("offset", "limit"), [(0, 2), (1, 2), (2, 2), (3, 3), (0, 10), (4, 2), (6, 2)]
)
def test_random_browse_preserves_pages(catalog, offset, limit) -> None:
    binding = LocalD1(catalog)
    expected = [1, 2, 3, 4]
    expected = [expected[_permuted_browse_index(index, 4, 17)] for index in range(4)]
    results, total = asyncio.run(search_songs_async(D1Database(binding), "", limit, offset, 17))
    assert [song.id for song in results] == expected[offset : offset + limit]
    assert total == 4
    if offset >= total:
        assert len(binding.calls) == 1


def test_random_browse_respects_disabled_songs(catalog) -> None:
    catalog.execute("UPDATE songs SET enabled = 0 WHERE id = 1")
    catalog.execute("UPDATE songs SET enabled = 0 WHERE id = 4")
    catalog.executescript(SNAPSHOT_REFRESH_SQL)
    results, total = asyncio.run(search_songs_async(D1Database(LocalD1(catalog)), "", 10))
    assert total == 2
    assert {song.id for song in results} == {2, 3}


def test_ranked_search_reuses_fixed_candidates_across_pages(catalog) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    first, total = asyncio.run(search_songs_async(database, "same", 2, seed=17))
    second, second_total = asyncio.run(search_songs_async(database, "same", 2, 2, seed=18))
    assert [song.id for song in first + second] == [2, 3, 1]
    assert total == second_total == 3
    assert len(binding.calls) == 1  # Search ignores shuffle seed and shares its candidate pool.
    assert "song_search_fts MATCH" in binding.calls[0]


def test_round_pool_preserves_uniform_choices_and_filter_boundaries(catalog, monkeypatch) -> None:
    binding = LocalD1(catalog)
    database = D1Database(binding)
    choices = []

    def choose(pool):
        choices.append(set(pool))
        return pool[-1]

    monkeypatch.setattr("app.repository.secrets.choice", choose)
    request = RoundRequest(year_min=1990, year_max=2005, popularity_min=50, popularity_max=100)
    first = asyncio.run(choose_round_async(database, request))
    second = asyncio.run(
        choose_round_async(database, request.model_copy(update={"exclude_ids": [first.song_id]}))
    )
    assert choices == [{1, 2, 3}, {1, 2, 3} - {first.song_id}]
    assert second.song_id != first.song_id
    assert len(binding.calls) == 3  # Snapshot plus two primary-key preview reads.
    other = request.model_copy(update={"year_max": 2020, "popularity_min": 0})
    asyncio.run(choose_round_async(database, other))
    assert choices[-1] == {1, 2, 3, 4}
    assert len(binding.calls) == 4


def test_context_cache_refreshes_and_sqlite_stays_uncached(catalog, monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(catalog_cache, "monotonic", lambda: now[0])
    binding = LocalD1(catalog)
    database = D1Database(binding, catalog_cache=CatalogCache(ttl=5))
    request = FilterContextRequest()
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 4
    catalog.execute("UPDATE songs SET enabled = 0 WHERE id = 4")
    catalog.executescript(SNAPSHOT_REFRESH_SQL)
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 4
    assert len(binding.calls) == 1
    local = SQLiteDatabase(catalog)
    assert asyncio.run(get_contextual_filter_metadata_async(local, request)).song_count == 3
    now[0] = 5
    assert asyncio.run(get_contextual_filter_metadata_async(database, request)).song_count == 3
    assert len(binding.calls) == 2


def test_concurrent_cache_misses_share_load_and_cancelled_waiter_does_not_cancel_load() -> None:
    async def scenario():
        class Database:
            catalog_cache = CatalogCache()

        database = Database()
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        @cache_catalog()
        async def load(_database):
            calls.append(1)
            started.set()
            await finish.wait()
            return 42

        first = asyncio.create_task(load(database))
        await started.wait()
        second = asyncio.create_task(load(database))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        assert await second == 42
        assert await load(database) == 42
        assert calls == [1]
        assert database.catalog_cache.inflight == {}

    asyncio.run(scenario())


def test_snapshot_matches_sql_filters_and_shares_one_read_across_requests(catalog) -> None:
    catalog.executemany("INSERT INTO genres (id, name) VALUES (?, ?)", [(1, "pop"), (2, "rock")])
    catalog.executemany("INSERT INTO countries (id, code) VALUES (?, ?)", [(1, "US"), (2, "CL")])
    catalog.execute("INSERT INTO artists (id, musicbrainz_id, name) VALUES (1, 'artist-a', 'A')")
    catalog.executemany("INSERT INTO song_genres VALUES (?, ?)", [(1, 1), (2, 1), (2, 2), (4, 2)])
    catalog.executemany("INSERT INTO song_countries VALUES (?, ?)", [(1, 1), (2, 2), (4, 2)])
    catalog.executemany(
        "INSERT INTO song_artists (song_id, artist_id, credit_order, credited_name) "
        "VALUES (?, 1, 0, 'A')",
        [(1,), (2,)],
    )
    catalog.executescript(SNAPSHOT_REFRESH_SQL)
    binding = LocalD1(catalog)
    remote = D1Database(binding)
    local = SQLiteDatabase(catalog)
    assert asyncio.run(get_filter_metadata_async(remote)) == asyncio.run(
        get_filter_metadata_async(local)
    )
    for selection in [
        {},
        {"artist_id": "artist-a"},
        {"artist_id": "missing"},
        {"genres": [" POP ", "rock"], "countries": ["cl"]},
        {"genres": ["pop"], "countries": ["US"], "year_min": 2005},
        {"year_min": 2000, "year_max": 2005, "popularity_min": 90},
        {"genres": ["missing"]},
        {"countries": ["DE"]},
    ]:
        request = FilterContextRequest(**selection)
        assert asyncio.run(get_contextual_filter_metadata_async(remote, request)) == asyncio.run(
            get_contextual_filter_metadata_async(local, request)
        )
    assert len(binding.calls) == 2
    assert sum("SELECT version, payload" in sql for sql in binding.calls) == 1


def test_initial_metadata_does_not_decode_full_relation_snapshot(catalog, monkeypatch) -> None:
    from app.catalog_snapshot import CatalogSnapshot

    def unexpected_decode(*_args, **_kwargs):
        raise AssertionError("Initial filters must not decode the full catalog snapshot")

    monkeypatch.setattr(CatalogSnapshot, "from_payload", unexpected_decode)
    binding = LocalD1(catalog)
    database = D1Database(binding)
    first = asyncio.run(get_filter_metadata_async(database))
    assert first.song_count == 4
    assert asyncio.run(get_filter_metadata_async(database)) == first
    assert len(binding.calls) == 1


def test_round_refreshes_stale_snapshot_and_revalidates_live_filters(catalog, monkeypatch) -> None:
    binding = LocalD1(catalog)
    remote = D1Database(binding)
    request = RoundRequest(year_min=1990, year_max=2005, popularity_min=50, popularity_max=100)
    monkeypatch.setattr("app.repository.secrets.choice", lambda pool: pool[0])
    assert asyncio.run(choose_round_async(remote, request)).song_id == 1
    catalog.execute("UPDATE songs SET release_year = 2015 WHERE id = 1")
    catalog.executescript(SNAPSHOT_REFRESH_SQL)
    assert asyncio.run(choose_round_async(remote, request)).song_id == 2
    assert sum("catalog_runtime" in sql for sql in binding.calls) == 2


@pytest.mark.parametrize("coalesce", [True, False])
def test_clear_prevents_an_old_inflight_load_from_restoring_stale_values(coalesce) -> None:
    async def scenario():
        class Database:
            catalog_cache = CatalogCache(coalesce=coalesce)

        database = Database()
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        @cache_catalog()
        async def load(_database):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                await finish.wait()
                return "old"
            return "new"

        old = asyncio.create_task(load(database))
        await started.wait()
        database.catalog_cache.clear()
        assert await load(database) == "new"
        finish.set()
        assert await old == "old"
        assert await load(database) == "new"
        assert len(calls) == 2

    asyncio.run(scenario())


def test_worker_cache_loads_in_the_request_task_and_cancellation_allows_retry() -> None:
    from app.database import _worker_catalog_cache

    assert _worker_catalog_cache.coalesce is False
    assert D1Database(None).catalog_cache.coalesce is False

    async def scenario():
        class Database:
            catalog_cache = CatalogCache(coalesce=False)

        database = Database()
        started = asyncio.Event()
        finish = asyncio.Event()
        tasks = []

        @cache_catalog()
        async def load(_database):
            tasks.append(asyncio.current_task())
            started.set()
            await finish.wait()
            return 42

        request = asyncio.create_task(load(database))
        await started.wait()
        assert tasks == [request]
        assert database.catalog_cache.inflight == {}
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        finish.set()
        assert await load(database) == 42
        assert tasks[-1] is asyncio.current_task()
        assert await load(database) == 42
        assert len(tasks) == 2

    asyncio.run(scenario())


def test_fuzzy_search_bounds_edit_matching_and_keeps_all_candidate_pages(monkeypatch) -> None:
    from app import repository

    rows = [
        {
            "id": index,
            "tier": 4,
            "popularity_score": 0,
            "normalized_title": f"candidate {index}",
            "normalized_artist": "same artist",
            "normalized_album": "same album",
            "normalized_year": "2000",
        }
        for index in range(500)
    ]
    calls = []

    async def candidates(database, query, limit):
        assert limit == 500
        return rows

    def similarity(query, value):
        calls.append(value)
        return 0.0

    monkeypatch.setattr(repository, "_song_candidates", candidates)
    monkeypatch.setattr(repository, "_fuzzy_similarity", similarity)
    result = asyncio.run(repository._ranked_search_rows_async(object(), "typo"))
    assert len(result) == 500
    assert {row["id"] for row in result} == set(range(500))
    assert len(calls) <= 40 * 5
    assert len(calls) == len(set(calls))  # Shared artist/album scores are computed once.
    assert [row["id"] for row in result[40:]] == list(range(40, 500))


def test_seeded_browse_changes_order_between_seeds() -> None:
    first = [_permuted_browse_index(index, 10_617, 17) for index in range(40)]
    second = [_permuted_browse_index(index, 10_617, 18) for index in range(40)]
    assert first != second
    assert len(set(first)) == 40
    assert first == [_permuted_browse_index(index, 10_617, 17) for index in range(40)]
