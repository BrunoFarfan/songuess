from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from dataset.export_d1 import export_d1
from dataset.populate import initialize_database

from app.catalog_cache import CatalogCache
from app.database import D1Database
from app.models import RoundRequest
from app.repository import choose_round_async


class FakeStatement:
    def __init__(self, binding: FakeD1, sql: str) -> None:
        self.binding = binding
        self.sql = sql
        self.parameters: tuple[object, ...] = ()

    def bind(self, *parameters: object) -> FakeStatement:
        self.parameters = parameters
        return self

    async def run(self) -> SimpleNamespace:
        self.binding.calls.append((self.sql, self.parameters))
        return SimpleNamespace(results=self.binding.run_results.pop(0))

    async def first(self) -> dict[str, object] | None:
        self.binding.calls.append((self.sql, self.parameters))
        return self.binding.first_results.pop(0)


class FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.first_results: list[dict[str, object] | None] = []
        self.run_results: list[list[dict[str, object]]] = []

    def prepare(self, sql: str) -> FakeStatement:
        return FakeStatement(self, sql)


def _round_request(exclude_ids: list[int]) -> RoundRequest:
    return RoundRequest(
        year_min=1950,
        year_max=2026,
        popularity_min=0,
        popularity_max=100,
        exclude_ids=exclude_ids,
    )


def test_d1_adapter_returns_worker_binding_rows() -> None:
    binding = FakeD1()
    binding.run_results.append([{"id": 1}, {"id": 2}])

    rows = asyncio.run(D1Database(binding).fetch_all("SELECT id FROM songs WHERE id > ?", [0]))

    assert rows == [{"id": 1}, {"id": 2}]


def test_round_pool_reuses_filters_and_applies_500_exclusions_in_memory() -> None:
    binding = FakeD1()
    binding.run_results.append(
        [{"id": song_id, "preview_url": f"https://audio/{song_id}"} for song_id in (1, 999)]
    )
    cache = CatalogCache()

    result = asyncio.run(
        choose_round_async(
            D1Database(binding, catalog_cache=cache), _round_request(list(range(1, 501)))
        )
    )

    assert result is not None and result.song_id == 999
    second = asyncio.run(
        choose_round_async(D1Database(binding, catalog_cache=cache), _round_request([999]))
    )
    assert second is not None and second.song_id == 1
    exhausted = asyncio.run(
        choose_round_async(D1Database(binding, catalog_cache=cache), _round_request([1, 999]))
    )
    assert exhausted is None
    assert len(binding.calls) == 1
    assert len(binding.calls[0][1]) == 4
    assert "COUNT" not in binding.calls[0][0]


def test_full_d1_export_is_deterministic_and_application_only(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    initialize_database(database)
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO songs (id, title, artist, release_year, popularity_score, "
            "stream_count, stream_count_fetched_at, stream_count_source, "
            "stream_count_status, musicbrainz_id, apple_track_id, preview_url, "
            "enabled, apple_explicitness, apple_explicitness_checked_at) "
            "VALUES (1, 'Song', 'Artist', 2000, 50, 123456, "
            "'2026-08-28T00:00:00Z', 'spotify_web_hydration', 'complete', "
            "'recording', 'apple', 'https://audio/1', 1, 'explicit', "
            "'2026-08-28T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO artists (id, musicbrainz_id, name) VALUES (1, 'artist', 'Artist')"
        )
        connection.execute(
            "INSERT INTO song_artists "
            "(song_id, artist_id, credit_order, credited_name) VALUES (1, 1, 0, 'Artist')"
        )
        connection.execute("INSERT INTO genres (id, name) VALUES (1, 'pop')")
        connection.execute("INSERT INTO song_genres (song_id, genre_id) VALUES (1, 1)")
        connection.execute(
            "INSERT INTO song_genre_evidence (song_id, genre_id, rank, score, source) "
            "VALUES (1, 1, 1, 100, 'apple')"
        )
        connection.commit()

    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first_manifest = export_d1(database, first)
    second_manifest = export_d1(database, second)

    sql = first.read_text(encoding="utf-8")
    assert first.read_bytes() == second.read_bytes()
    assert first_manifest == second_manifest
    assert first_manifest["counts"]["songs"] == 1
    assert first_manifest["quality"] == {"apple_explicit": 1, "apple_cleaned": 0}
    assert first_manifest["sql_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert "INSERT INTO song_genre_evidence" in sql
    assert "INSERT INTO song_search_fts" in sql
    assert "spotify_backfill_failures" not in sql

    destination = tmp_path / "destination.sqlite3"
    initialize_database(destination)
    with sqlite3.connect(destination) as connection:
        connection.executescript(sql)
        connection.executescript(sql)
        assert connection.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT stream_count, stream_count_source, apple_explicitness FROM songs"
        ).fetchone() == (123456, "spotify_web_hydration", "explicit")
        assert connection.execute("SELECT COUNT(*) FROM song_search_fts").fetchone()[0] == 1
