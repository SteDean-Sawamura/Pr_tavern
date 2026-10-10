import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from api import game_routes


class ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.acquire_started = asyncio.Event()

    async def acquire(self):
        self.acquire_started.set()
        return await super().acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), [
    ("/api/game/save-stream/action/stream", {"type": "freeform", "text": "Look around"}),
    ("/api/game/save-stream/talk/npc-test/stream", {"message": "Hello"}),
    ("/api/game/save-stream/regenerate/stream", {}),
], ids=["action", "npc", "regenerate"])
@pytest.mark.parametrize("transition", [
    "cancel_waiting", "cancel_restoring", "missing", "restore_http_error", "restore_failure",
])
async def test_stream_setup_releases_only_its_own_lock(monkeypatch, path, body, transition):
    lock = ObservedLock()
    restore_started = asyncio.Event()
    restore_blocked = asyncio.Event()

    async def restore_session(save_id):
        restore_started.set()
        if transition == "cancel_restoring":
            await restore_blocked.wait()
        if transition == "restore_http_error":
            raise HTTPException(status_code=409, detail="Restore unavailable")
        if transition == "restore_failure":
            raise RuntimeError("Restore failed")
        return None

    async def missing_session(save_id):
        return None

    monkeypatch.setattr(game_routes, "_get_session_lock", lambda save_id: lock)
    monkeypatch.setattr(game_routes, "_sessions", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_restore_session", restore_session)
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")

    if transition == "cancel_waiting":
        await lock.acquire()
        lock.acquire_started.clear()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(client.post(path, json=body))
        try:
            if transition in ("cancel_waiting", "cancel_restoring"):
                entered = (lock.acquire_started if transition == "cancel_waiting"
                           else restore_started)
                await asyncio.wait_for(entered.wait(), timeout=2)
                assert lock.locked()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert lock.locked() == (transition == "cancel_waiting")
                if transition == "cancel_waiting":
                    assert not restore_started.is_set()
                    lock.release()
            else:
                response = await asyncio.wait_for(pending, timeout=2)
                expected = {"missing": 404, "restore_http_error": 409, "restore_failure": 500}
                assert response.status_code == expected[transition]
                assert not lock.locked()

            monkeypatch.setattr(game_routes, "_restore_session", missing_session)
            retry = await asyncio.wait_for(client.post(path, json=body), timeout=2)
            assert retry.status_code == 404
            assert not lock.locked()
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if lock.locked():
                lock.release()
