import asyncio
from contextlib import asynccontextmanager
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from test_actor_goal_retrieval import CATEGORIES, payloads, query_goals
from test_agent_harness_execution import _stream_events
from test_director_goal_persistence import goal_client, make_session, updates
from test_story_choice_settlement import stored_state


NARRATIVE = (
    "You remain beside the reading room shelf and inspect it carefully. "
    "You leave all the books in place and keep the aisle clear, then finish the inspection "
    "without disturbing anyone in the room."
)


def settlement_session(dynamic=False):
    session, script = make_session(dynamic)
    session.current_state["script_variables"]["test_gate"] = 0
    session.current_state["player"]["gold"] = 10
    declaration = updates("Inspect the catalog.")
    for category, _ in CATEGORIES:
        declaration[category][0]["goals"][0]["rule_notes"] = "SYNTHETIC_GOAL_SETTLEMENT_DETAIL"
    declaration["org_goals"][0]["goals"][0]["on_complete_effects"] = {
        "state_change": [{"target": "player.gold", "op": "add", "value": 3}],
        "set_reputation": [{"faction": "test_circle", "value": 60}],
    }
    session._apply_director_results({"goals": declaration})
    return session, script


def install_agent(session, value):
    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments = "classify_intent", {"intent": "action"}
            step = "intent"
        elif names == ["extract_notes"]:
            name, arguments = "extract_notes", {"notes": []}
            step = "notes"
        else:
            steps = [int(message["tool_call_id"].removeprefix("goal-settle-"))
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").removeprefix("goal-settle-").isdigit()
                     and message["tool_call_id"].startswith("goal-settle-")]
            step = max(steps, default=-1) + 1
            operations = [
                ("submit_plan", {"plan": "Inspect the catalog."}),
                ("update_state", {"end_time": "2030-01-01T09:01:00",
                                  "variable_changes": [{"id": "test_gate", "op": "set", "value": value}]}),
                ("write_narrative", {"text": NARRATIVE}),
                ("finalize_turn", {"summary": "Inspected the catalog."}),
            ]
            assert step < len(operations)
            name, arguments = operations[step]
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"goal-settle-{step}", "name": name, "arguments": arguments}]}

    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )


def result_from(response, stream=True):
    assert response.status_code == 200, response.text
    if not stream:
        return response.json()
    events = _stream_events(response.text)
    assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], response.text
    result, = [event for event in events if event["type"] == "final"]
    return result


def assert_goals(state, completed):
    assert state["player"]["gold"] == (13 if completed else 10)
    for category, actor_id in CATEGORIES:
        records = [record for record in state.get(f"completed_{category}", [])
                   if record["id"] == f"{actor_id}:test_new_goal"]
        assert len(records) == int(completed)
        if completed:
            assert records[0]["turn"] == 1
    assert len([entry for entry in state.get("information_network", [])
                if any(tag in {"npc_goal", "org_goal"} for tag in entry.get("tags", []))]) == (2 if completed else 0)
    assert len([entry for entry in state.get("narrative_callbacks", [])
                if "Inspect the catalog." in entry["text"]]) == (2 if completed else 0)
    if completed:
        assert state["faction_reputation"]["test_circle"]["value"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("source, value, completed", [
    ("update", 9, True), ("after_ai", 0, True), ("revoked", 9, False),
    ("unmet", 0, False), ("prepare", 0, True),
])
async def test_turn_settles_goals_after_lifecycle_and_persists(tmp_path, monkeypatch, stream, source, value, completed):
    session, script = settlement_session()
    if source == "prepare":
        session.current_state["script_variables"]["test_gate"] = 9
    if source in {"after_ai", "revoked"}:
        session._apply_trigger_updates({"add": [{
            "id": "test_goal_hook", "event": "after_ai", "action": "set_var",
            "params": {"var_id": "test_gate", "op": "set", "value": 9 if completed else 0},
        }]})
    install_agent(session, value)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        endpoint = f"/api/game/{session.save_id}/action" + ("/stream" if stream else "")
        response = await asyncio.wait_for(client.post(endpoint, json={
            "type": "freeform", "text": "Inspect the catalog.",
            "action_id": f"synthetic-goal-{source}", "expected_node_id": root,
        }), 15)
        result = result_from(response, stream)
        assert result["narrative"] == NARRATIVE
        assert result["state"]["player"]["gold"] == (13 if completed else 10)
        if source in {"after_ai", "revoked"}:
            assert session.current_state["script_variables"]["test_gate"] == (9 if completed else 0)
        assert_goals(session.current_state, completed)
        final = session.world_tree.get_node(result["node_id"])
        assert_goals(final["state_snapshot"], completed)
        assert final["_state_hash"] == session.world_tree.state_hash(final["state_snapshot"])
        assert_goals(await stored_state(session), completed)
        reward_changes = [change for change in result["state_changes"] if change.get("target") == "player.gold"]
        assert len(reward_changes) == int(completed)
        if completed:
            assert (reward_changes[0]["old"], reward_changes[0]["new"]) == (10, 13)
        assert final["state_changes"] == result["state_changes"]
        async with database.get_db() as db:
            cursor = await db.execute("SELECT state_changes FROM tree_nodes WHERE id = ?", (result["node_id"],))
            assert json.loads((await cursor.fetchone())["state_changes"]) == final["state_changes"]
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert_goals(restored.current_state, completed)
        await restored.branch_to_node(root)
        assert_goals(restored.current_state, False)
        await restored.branch_to_node(result["node_id"])
        assert_goals(restored.current_state, completed)
        trace = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert "SYNTHETIC_GOAL_SETTLEMENT_DETAIL" not in response.text + json.dumps(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_goal_save_rolls_back_effects_and_retries_once(tmp_path, monkeypatch, dynamic, stream):
    session, script = settlement_session(dynamic)
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        root = session.world_tree.active_node_id
        tree = copy.deepcopy(session.world_tree.nodes)
        attempted = []

        @asynccontextmanager
        async def fail_after_insert():
            async with database.get_db() as db:
                async def execute(sql, *args):
                    if sql.lstrip().startswith("UPDATE saves"):
                        assert_goals(session.current_state, True)
                        attempted.append(session.world_tree.active_node_id)
                        raise RuntimeError("Synthetic goal save failure")
                    return await db.execute(sql, *args)
                yield SimpleNamespace(execute=execute, commit=db.commit)

        endpoint = f"/api/game/{session.save_id}/action" + ("/stream" if stream else "")
        action = {"type": "freeform", "text": "Inspect the catalog.",
                  "action_id": "synthetic-goal-save-retry", "expected_node_id": root}
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "get_db", fail_after_insert)
            response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        if stream:
            events = _stream_events(response.text)
            assert any(event["type"] == "error" for event in events)
            assert not any(event["type"] == "final" for event in events)
        else:
            assert response.status_code == 500
        assert len(attempted) == 1
        assert session.current_state == before and session.turn_number == 0
        assert session.world_tree.active_node_id == root and session.world_tree.nodes == tree
        assert await stored_state(session) == stored
        for category, _ in CATEGORIES:
            assert payloads(query_goals(session, category, query="test_new_goal"))[0]["status"] == "pending"
        async with database.get_db() as db:
            cursor = await db.execute("SELECT id FROM tree_nodes WHERE save_id = ?", (session.save_id,))
            assert [row["id"] for row in await cursor.fetchall()] == [root]
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result_from(response, stream)
        assert_goals(session.current_state, True)
        assert_goals(await stored_state(session), True)
        if stream:
            calls = session.ai_provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert session.ai_provider.generate_with_tools.await_count == calls
            assert_goals(await stored_state(session), True)


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_regenerated_goal_outcomes_follow_swipe_and_reload(tmp_path, monkeypatch, dynamic, stream):
    session, script = settlement_session(dynamic)
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        result = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-goal-original",
        }))
        assert_goals(await stored_state(session), True)
        for value, completed in [(0, False), (9, True)]:
            install_agent(session, value)
            endpoint = f"/api/game/{session.save_id}/regenerate" + ("/stream" if stream else "")
            response = await asyncio.wait_for(client.post(endpoint + "?stage=all"), 15)
            assert not result_from(response, stream).get("error"), response.text
            assert_goals(session.current_state, completed)
            assert_goals(await stored_state(session), completed)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        for index, completed in [(0, True), (1, False), (2, True), (0, True)]:
            response = await client.post(f"/api/game/{restored.save_id}/swipe/jump?index={index}")
            assert response.status_code == 200, response.text
            assert_goals(restored.current_state, completed)
            assert_goals(await stored_state(restored), completed)
            assert restored.world_tree.active_node_id == result["node_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_failed_goal_regeneration_preserves_previous_swipe(tmp_path, monkeypatch, dynamic):
    session, script = settlement_session(dynamic)
    install_agent(session, 0)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-goal-before-regeneration",
        }))
        assert_goals(session.current_state, False)
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        tree = copy.deepcopy(session.world_tree.nodes)
        attempted = []
        install_agent(session, 9)

        @asynccontextmanager
        async def fail_commit():
            async with database.get_db() as db:
                async def commit():
                    assert_goals(session.current_state, True)
                    attempted.append(True)
                    raise RuntimeError("Synthetic goal regeneration commit failure")
                yield SimpleNamespace(execute=db.execute, commit=commit)

        endpoint = f"/api/game/{session.save_id}/regenerate?stage=all"
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "get_db", fail_commit)
            response = await asyncio.wait_for(client.post(endpoint), 15)
        assert response.status_code == 500 and attempted == [True]
        assert session.current_state == before and session.world_tree.nodes == tree
        assert await stored_state(session) == stored
        for category, _ in CATEGORIES:
            assert payloads(query_goals(session, category, query="test_new_goal"))[0]["status"] == "pending"
        response = await asyncio.wait_for(client.post(endpoint), 15)
        assert response.status_code == 200, response.text
        assert_goals(session.current_state, True)
        assert_goals(await stored_state(session), True)
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        assert len(node["swipes"]) == 2
