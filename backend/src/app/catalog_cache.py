"""Bounded, short-lived catalog results in a single Worker isolate."""

import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from time import monotonic
from typing import Any

from pydantic import BaseModel

_MISSING = object()


class CatalogCache:
    def __init__(
        self,
        *,
        ttl: float = 300,
        max_entries: int = 128,
        max_rows: int = 30_000,
        coalesce: bool = True,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_rows = max_rows
        self.coalesce = coalesce
        self.entries: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self.rows = 0
        self.inflight: dict[str, asyncio.Task] = {}
        self.generation = 0

    def get(self, key: str) -> Any:
        entry = self.entries.get(key)
        if entry is None:
            return _MISSING
        expires, value, _ = entry
        if expires <= monotonic():
            self._remove(key)
            return _MISSING
        self.entries.move_to_end(key)
        return value

    def _remove(self, key: str) -> None:
        self.rows -= self.entries.pop(key)[2]

    def clear(self) -> None:
        self.generation += 1
        self.entries.clear()
        self.inflight.clear()
        self.rows = 0

    def put(self, key: str, value: Any, rows: int) -> None:
        if rows > self.max_rows:
            return
        now = monotonic()
        for expired_key, (expires, _, _) in list(self.entries.items()):
            if expires <= now:
                self._remove(expired_key)
        if key in self.entries:
            self._remove(key)
        while self.entries and (
            len(self.entries) >= self.max_entries or self.rows + rows > self.max_rows
        ):
            self._remove(next(iter(self.entries)))
        self.entries[key] = (now + self.ttl, value, rows)
        self.rows += rows


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        data = value.model_dump()
        if "genres" in data:
            data["genres"] = sorted({genre.strip().lower() for genre in data["genres"]})
        if "countries" in data:
            data["countries"] = sorted({country.upper() for country in data["countries"]})
        return data
    raise TypeError(f"Unsupported catalog cache key: {type(value).__name__}")


def cache_catalog(*, row_count: Callable[[Any], int] = lambda _: 1) -> Callable:
    """Opt in pure Python catalog results; never cache a randomly selected round."""

    def decorate(function: Callable) -> Callable:
        @wraps(function)
        async def cached(database: Any, *args: Any, **kwargs: Any) -> Any:
            cache = getattr(database, "catalog_cache", None)
            if cache is None:
                return await function(database, *args, **kwargs)
            key = function.__name__ + json.dumps(
                [args, kwargs], default=_serialize, sort_keys=True, separators=(",", ":")
            )
            value = cache.get(key)
            if value is _MISSING:
                if not cache.coalesce:
                    # Worker binding I/O must stay in the current request's task.
                    # Sharing a pending asyncio task across requests can leave a
                    # request with no runtime-owned events and trigger error 1101.
                    generation = cache.generation
                    value = await function(database, *args, **kwargs)
                    if generation == cache.generation:
                        cache.put(key, value, max(1, row_count(value)))
                    return value
                task = cache.inflight.get(key)
                if task is None:
                    generation = cache.generation

                    async def load() -> Any:
                        try:
                            result = await function(database, *args, **kwargs)
                            if generation == cache.generation:
                                cache.put(key, result, max(1, row_count(result)))
                            return result
                        finally:
                            if cache.inflight.get(key) is asyncio.current_task():
                                cache.inflight.pop(key, None)

                    task = asyncio.create_task(load())
                    cache.inflight[key] = task
                    # Retrieve errors even when every waiting request was cancelled.
                    task.add_done_callback(
                        lambda done: None if done.cancelled() else done.exception()
                    )
                value = await asyncio.shield(task)
            return value

        return cached

    return decorate
