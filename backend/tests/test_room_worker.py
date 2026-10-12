"""The deployed entrypoint must keep ASGI serving and scheduled retention."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock


def test_scheduled_cleanup_uses_entrypoint_database_and_preserves_asgi(monkeypatch):
    class ASGIEntrypoint:
        def __init__(self, ctx, env):
            self.ctx = ctx
            self.env = env

        async def fetch(self, request):
            return request

    workers = ModuleType("workers")
    workers.asgi = SimpleNamespace(entrypoint=lambda app: ASGIEntrypoint)
    monkeypatch.setitem(sys.modules, "workers", workers)

    from app import rooms

    cleanup = AsyncMock(return_value=3)
    monkeypatch.setattr(rooms, "cleanup_expired_rooms", cleanup)
    path = Path(__file__).resolve().parents[1] / "src" / "worker.py"
    spec = importlib.util.spec_from_file_location("room_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    binding = object()
    entrypoint = module.Default(None, SimpleNamespace(DB=binding))
    # The current Python runtime supplies legacy event env/ctx arguments as None.
    asyncio.run(entrypoint.scheduled(None, None, None))

    cleanup.assert_awaited_once()
    assert cleanup.call_args.args[0].binding is binding
    assert asyncio.run(entrypoint.fetch("request")) == "request"
