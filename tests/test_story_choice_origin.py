import asyncio
import copy

import pytest

from api import game_routes
from engine.event_engine import GameEvent
from test_player_story_choices import runtime_snapshot
from test_story_choice_settlement import choice_client, choice_session, persist_node, stored_state


def origin_session(source="story"):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.current_state.setdefault("random_items", {})
    session.current_state.setdefault("npcs", {})
    if source == "event":
        session.event_engine.add_event(GameEvent(
            id="test_independent", category="choice", choices=[{
                "id": "test_select", "label": "Synthetic selection",
                "effects": {"set_var": [{"var_id": "test_reward", "op": "inc", "value": 6}]},
            }],
        ))
        session.current_state["events"]["test_independent"] = {"status": "active"}
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["", "/active-choices"])
async def test_choice_view_reports_the_source_node_without_mutation(tmp_path, monkeypatch, surface):
    session = origin_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        child_id = await persist_node(session, root_id)
        for node_id in (child_id, root_id, child_id):
            await session.branch_to_node(node_id)
            before = runtime_snapshot(session)
            stored = await stored_state(session)
            response = await client.get(f"/api/game/{session.save_id}/story-tree{surface}")
            assert response.status_code == 200
            assert response.json()["base_node_id"] == node_id
            assert runtime_snapshot(session) == before
            assert await stored_state(session) == stored
        game_routes._sessions.clear()
        response = await client.get(f"/api/game/{session.save_id}/story-tree{surface}")
        sessions.append(game_routes._sessions[session.save_id])
        assert response.status_code == 200
        assert response.json()["base_node_id"] == child_id


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["story", "event"])
@pytest.mark.parametrize("summary_only", [False, True])
@pytest.mark.parametrize("reload", [False, True])
async def test_old_choice_origin_is_rejected_before_any_effects(tmp_path, monkeypatch, source, summary_only, reload):
    session = origin_session(source)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        old_id = session.world_tree.active_node_id
        new_id = await persist_node(session, old_id)
        if reload:
            game_routes._sessions.clear()
            session = await game_routes._restore_session(session.save_id)
            sessions.append(session)
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        tree_before = copy.deepcopy(session.world_tree.__dict__)
        endpoint = f"/api/game/{session.save_id}/story-tree/choice?summary_only={str(summary_only).lower()}"
        payload = {
            "node_id": "test_decision" if source == "story" else "test_independent",
            "choice_id": "test_open" if source == "story" else "test_select",
            "expected_node_id": old_id,
        }
        def forbidden_settlement(*args):
            raise AssertionError("Stale requests must not enter settlement")
        with monkeypatch.context() as patch:
            patch.setattr(session, "_settle_story_choice", forbidden_settlement)
            response = await client.post(endpoint, json=payload)
        assert response.status_code == 409, response.text
        assert runtime_snapshot(session) == before
        assert session.world_tree.__dict__ == tree_before
        assert await stored_state(session) == stored
        assert session.world_tree.active_node_id == new_id
        if source == "story" or not reload:
            payload["expected_node_id"] = new_id
            response = await client.post(endpoint, json=payload)
            assert response.status_code == 200 and response.json()["success"]
            assert (await stored_state(session))["script_variables"]["test_reward"] == (4 if source == "story" else 6)
            await session.branch_to_node(old_id)
            assert session.current_state["script_variables"]["test_reward"] == 0
            before = runtime_snapshot(session)
            stored = await stored_state(session)
            response = await client.post(endpoint, json=payload)
            assert response.status_code == 409
            assert runtime_snapshot(session) == before
            assert await stored_state(session) == stored


@pytest.mark.asyncio
async def test_choice_origin_is_checked_after_waiting_for_session_lock(tmp_path, monkeypatch):
    session = origin_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        old_id = session.world_tree.active_node_id
        lock = game_routes._get_session_lock(session.save_id)
        async with lock:
            pending = asyncio.create_task(client.post(f"/api/game/{session.save_id}/story-tree/choice", json={
                "node_id": "test_decision", "choice_id": "test_open", "expected_node_id": old_id,
            }))
            await asyncio.sleep(0)
            assert not pending.done()
            await persist_node(session, old_id)
            before = runtime_snapshot(session)
            stored = await stored_state(session)
        response = await pending
        assert response.status_code == 409
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", ["missing", None, "current", "", "stale"])
async def test_choice_origin_compatibility(tmp_path, monkeypatch, expected):
    session = origin_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        payload = {"node_id": "test_decision", "choice_id": "test_open"}
        if expected != "missing":
            payload["expected_node_id"] = session.world_tree.active_node_id if expected == "current" else expected
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json=payload)
        if expected in ("", "stale"):
            assert response.status_code == 409
            assert (await stored_state(session))["script_variables"]["test_reward"] == 0
        else:
            assert response.status_code == 200 and response.json()["success"]
            assert (await stored_state(session))["script_variables"]["test_reward"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", ["missing", "current", "stale"])
async def test_map_state_refresh_checks_origin_without_changing_response_shape(tmp_path, monkeypatch, expected):
    session = origin_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        params = {} if expected == "missing" else {
            "expected_node_id": session.world_tree.active_node_id if expected == "current" else expected,
        }
        response = await client.get(f"/api/game/{session.save_id}/state", params=params)
        assert response.status_code == (409 if expected == "stale" else 200)
        if expected != "stale":
            baseline = await client.get(f"/api/game/{session.save_id}/state")
            assert response.json() == baseline.json()
            assert response.json()["game_time"] == session.current_state["game_time"]
        assert runtime_snapshot(session) == before
