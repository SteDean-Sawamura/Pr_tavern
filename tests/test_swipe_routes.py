import asyncio
import copy
from collections import OrderedDict
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import game_routes
from engine.game_session import GameSession


@pytest.mark.asyncio
async def test_swipe_jump_route_reaches_jump_handler(monkeypatch):
    session = GameSession({"script_id": "swipe-route", "settings": {}}, None, save_id="swipe-save")
    state = {"scene": "first"}
    node_id = session.world_tree.add_node(None, "day 1", 1, ai_response="First", state_snapshot=state)
    node = session.world_tree.get_node(node_id)
    node["swipes"] = [
        {"narrative": "First", "choices": [], "state_snapshot": state},
        {"narrative": "Second", "choices": [], "state_snapshot": {"scene": "second"}},
    ]
    node["active_swipe_index"] = 0
    node["agent_trace"] = {"mode": "original"}
    session.current_state = state
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    persist = AsyncMock()
    monkeypatch.setattr(game_routes, "_update_node_in_db", persist)
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for expected in (
            {"expected_node_id": "previous-node", "expected_swipe_index": 0, "expected_total_swipes": 2},
            {"expected_node_id": node_id, "expected_swipe_index": 1, "expected_total_swipes": 2},
            {"expected_node_id": node_id, "expected_swipe_index": 0, "expected_total_swipes": 1},
        ):
            stale = await client.post("/api/game/swipe-save/swipe/jump", params={"index": 1, **expected})
            assert stale.status_code == 409
            assert node["active_swipe_index"] == 0
            persist.assert_not_awaited()

        expected = {"expected_node_id": node_id, "expected_swipe_index": 0, "expected_total_swipes": 2}
        response = await client.post("/api/game/swipe-save/swipe/jump", params={"index": 1, **expected})
        stale = await client.post("/api/game/swipe-save/swipe/left", params=expected)

    assert response.status_code == 200, response.text
    assert response.json()["swipe_index"] == 1
    assert response.json()["agent_trace"] is None
    assert stale.status_code == 409
    assert session.world_tree.get_node(node_id)["active_swipe_index"] == 1
    persist.assert_awaited_once()
    assert session.swipe_to_index(0)["agent_trace"] == {"mode": "original"}


@pytest.mark.asyncio
async def test_save_snapshot_keeps_state_and_history_on_one_swipe(monkeypatch):
    session = GameSession({"script_id": "snapshot-route", "settings": {}}, None, save_id="snapshot-save")
    state = {"scene": "first"}
    node_id = session.world_tree.add_node(None, "day 1", 1, ai_response="First", state_snapshot=state)
    node = session.world_tree.get_node(node_id)
    node["swipes"] = [
        {"narrative": "First", "choices": [], "state_snapshot": state},
        {"narrative": "Second", "choices": [], "state_snapshot": {"scene": "second"}},
    ]
    node["active_swipe_index"] = 0
    session.current_state = state
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    started = asyncio.Event()
    finish = asyncio.Event()

    async def persist(_session, _result):
        started.set()
        await finish.wait()

    monkeypatch.setattr(game_routes, "_update_node_in_db", persist)
    monkeypatch.setattr(game_routes, "_stored_pending_action", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        swipe = asyncio.create_task(client.post("/api/game/snapshot-save/swipe/jump", params={"index": 1}))
        await asyncio.wait_for(started.wait(), 5)
        snapshot = asyncio.create_task(client.get("/api/game/snapshot-save/snapshot", params={"last": 10}))
        try:
            await asyncio.sleep(0)
            assert not snapshot.done()
        finally:
            finish.set()
        swipe_response, snapshot_response = await asyncio.gather(swipe, snapshot)
        history_response = await client.get("/api/game/snapshot-save/history", params={"last": 10})

    assert swipe_response.status_code == 200
    assert snapshot_response.status_code == 200, snapshot_response.text
    payload = snapshot_response.json()
    assert payload["node_id"] == node_id
    assert payload["state"]["scene"] == "second"
    assert payload["history"]["entries"][-1]["narrative"] == "Second"
    assert payload["history"]["swipe_index"] == 1
    assert history_response.json() == payload["history"]


@pytest.mark.asyncio
async def test_regenerate_persistence_failure_restores_active_swipe(monkeypatch):
    session = GameSession({"script_id": "regen-route", "settings": {}}, None, save_id="regen-save")
    parent_state = {"scene": "parent", "game_time": "day 1"}
    parent_id = session.world_tree.add_node(None, "day 1", 0, state_snapshot=parent_state)
    old_state = {"scene": "original", "game_time": "day 2"}
    node_id = session.world_tree.add_node(
        parent_id, "day 2", 1, ai_response="Original", state_snapshot=old_state,
    )
    session.current_state = copy.deepcopy(old_state)
    node_before = copy.deepcopy(session.world_tree.get_node(node_id))

    async def regenerate(*, stage, hint):
        node = session.world_tree.get_node(node_id)
        session._record_regen_swipe(node, "Replacement", [], {"scene": "replacement", "game_time": "day 3"}, [])
        return {"node_id": node_id, "narrative": "Replacement", "choices": [], "state_changes": []}

    async def fail_persistence(_session, _result):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(session, "regenerate", regenerate)
    monkeypatch.setattr(game_routes, "_update_node_in_db", fail_persistence)
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/game/regen-save/regenerate", params={"stage": "all"})

    assert response.status_code == 500
    assert session.world_tree.get_node(node_id) == node_before
    assert session.current_state == old_state
