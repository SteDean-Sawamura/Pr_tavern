import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

from api import game_routes
from engine.game_session import GameSession


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [
    "unchanged", "stopped", "finished", "new_stream", "new_plan",
    "new_action", "new_session", "injection_disabled", "interjection",
])
async def test_plan_reply_revalidates_after_database_wait(tmp_path, monkeypatch, transition):
    session = object.__new__(GameSession)
    session._agent_control_event = asyncio.Event()
    game_routes._set_agent_stream_active(session, True, "stream-initial")
    session._active_agent_action_id = "action-initial"
    session._pending_agent_action_id = "action-initial"
    session._pending_agent_plan_id = "plan-initial"
    monkeypatch.setitem(game_routes._sessions, "save-control", session)

    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    persisted = asyncio.Event()
    release_commit = asyncio.Event()

    async with aiosqlite.connect(tmp_path / "control.db") as connection:
        await connection.execute(
            "CREATE TABLE pending_action_plans "
            "(save_id TEXT, action_id TEXT, plan_id TEXT, reply TEXT)"
        )
        await connection.execute(
            "INSERT INTO pending_action_plans VALUES (?, ?, ?, NULL)",
            ("save-control", "action-initial", "plan-initial"),
        )
        await connection.commit()

        async def delayed_commit():
            await connection.commit()
            persisted.set()
            await release_commit.wait()

        @asynccontextmanager
        async def get_connection():
            yield SimpleNamespace(execute=connection.execute, commit=delayed_commit)

        monkeypatch.setattr(game_routes, "get_db", get_connection)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = {
                "stream_id": "stream-initial", "plan_id": "plan-initial",
                "message": "Take the left route",
            }
            pending = asyncio.create_task(client.post(
                "/api/game/save-control/inject", json=request,
            ))
            try:
                await asyncio.wait_for(persisted.wait(), timeout=2)
                if transition == "stopped":
                    stopped = await client.post("/api/game/save-control/abort", json={
                        "stream_id": "stream-initial",
                    })
                    assert stopped.status_code == 200
                elif transition == "finished":
                    game_routes._set_agent_stream_active(session, False)
                elif transition == "new_stream":
                    game_routes._set_agent_stream_active(session, True, "stream-next")
                    session._pending_agent_plan_id = "plan-next"
                    session._pending_agent_action_id = "action-next"
                    session._active_agent_action_id = "action-next"
                elif transition == "new_plan":
                    session._pending_agent_plan_id = "plan-next"
                elif transition == "new_action":
                    session._pending_agent_action_id = "action-next"
                    session._active_agent_action_id = "action-next"
                elif transition == "new_session":
                    replacement = object.__new__(GameSession)
                    game_routes._set_agent_stream_active(replacement, True, "stream-next")
                    monkeypatch.setitem(game_routes._sessions, "save-control", replacement)
                elif transition == "injection_disabled":
                    session._agent_inject_enabled = False
                elif transition == "interjection":
                    injected = await client.post("/api/game/save-control/inject", json={
                        "stream_id": "stream-initial", "message": "Check the surroundings",
                    })
                    assert injected.status_code == 200

                expected_plan = session._pending_agent_plan_id
                expected_queue = list(session._inject_queue)
                release_commit.set()
                response = await asyncio.wait_for(pending, timeout=2)
                cursor = await connection.execute(
                    "SELECT reply FROM pending_action_plans WHERE plan_id = ?",
                    ("plan-initial",),
                )
                reply = (await cursor.fetchone())[0]
                if transition == "unchanged":
                    assert response.status_code == 200
                    assert response.json() == {"queued": True}
                    assert session._inject_queue == [request["message"]]
                    assert session._queued_agent_plan_reply_id == "plan-initial"
                    assert session._pending_agent_plan_id is None
                    assert reply == request["message"]
                    duplicate = await client.post("/api/game/save-control/inject", json=request)
                    assert duplicate.status_code == 409
                else:
                    assert response.status_code == 409
                    assert session._inject_queue == expected_queue
                    assert session._pending_agent_plan_id == expected_plan
                    assert session._queued_agent_plan_reply_id is None
                    assert reply is None
                    if transition == "new_session":
                        assert replacement._inject_queue == []
            finally:
                release_commit.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
