import asyncio
import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events


def make_session(state_id="test_knight_oath", name="合成誓约"):
    script = {
        "script_id": "synthetic-state-declarations", "name": "Synthetic state declarations", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "persistent_states": [{
            "id": state_id, "name": name, "description": "A synthetic declared condition.",
            "rule_notes": "SYNTHETIC_DECLARED_STATE_RULE",
        }],
        "lorebook": [{"id": "test_notice", "enabled": False, "content": "SYNTHETIC_DECLARED_STATE_LORE"}],
        "one_time_events": [{
            "id": "test_check_state", "trigger_time": "2030-01-01T09:01:00",
            "condition": f"status.{state_id} == active",
            "effects": [{"type": "activate_lore", "id": "test_notice"}],
        }],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    return session, script


def builtin_weather_state(weather):
    session, _ = make_session()
    session.current_state["current_weather"] = weather
    session._apply_weather_effects()
    state_id, = session.current_state["active_persistent_states"]
    return state_id


@pytest.mark.parametrize("state_id,name", [
    ("test_knight_oath", "合成誓约"),
    ("test_mark", "黎明誓约"),
    ("test_weather_shield", "合成护盾"),
    ("builtin_weather", "合成降水机制"),
    ("test_neutral", "合成状态"),
])
@pytest.mark.parametrize("entry_form", ["id", "object"])
@pytest.mark.parametrize("stage", ["feedback", "projection", "settlement"])
def test_declared_states_survive_environment_name_heuristics(state_id, name, entry_form, stage):
    if state_id == "builtin_weather":
        state_id = builtin_weather_state("阴雨")
    session, script = make_session(state_id, name)
    definition = script["persistent_states"][0]
    entry = state_id if entry_form == "id" else {key: definition[key] for key in ("id", "name", "description")}
    update = {"activate_states": [entry]}
    original = copy.deepcopy(session.current_state)
    if stage == "feedback":
        result = session._run_state_tool("update_resources", update)
        assert "过滤" not in result
    elif stage == "projection":
        projected = session._project_state_tool_updates([update])
        assert state_id in projected["active_persistent_states"]
    else:
        state, _ = session._apply_common_parsed_changes(copy.deepcopy(original), update)
        assert state_id in state["active_persistent_states"]
        assert state["display_names"][state_id] == name
        assert state["persistent_state_descriptions"][state_id] == definition["description"]
    assert session.current_state == original


@pytest.mark.parametrize("entry", [
    "test_night", "test_weather", {"id": "test_night", "name": "合成时段"},
    {"id": "test_transient", "name": "天气"},
    {"id": "test_transient", "name": "", "description": "天气晴朗"},
])
def test_undeclared_environment_states_are_filtered_consistently(entry):
    session, _ = make_session()
    update = {"activate_states": [entry]}
    original = copy.deepcopy(session.current_state)
    assert "过滤天气/时段状态" in session._run_state_tool("update_resources", update)
    projected = session._project_state_tool_updates([update])
    assert projected["active_persistent_states"] == []
    state, _ = session._apply_common_parsed_changes(copy.deepcopy(original), update)
    assert state["active_persistent_states"] == []
    assert session.current_state == original


@pytest.mark.parametrize("weather", ["阴雨", "暴雨", "极端"])
@pytest.mark.parametrize("initially_active", [False, True])
@pytest.mark.parametrize("current_weather", ["", "matching"])
def test_weather_automation_does_not_own_declared_state_ids(weather, initially_active, current_weather):
    state_id = builtin_weather_state(weather)
    session, _ = make_session(state_id)
    session.current_state["current_weather"] = weather if current_weather else ""
    expected = [state_id] if initially_active else []
    session.current_state["active_persistent_states"] = expected.copy()
    session._apply_weather_effects()
    session._apply_weather_effects()
    assert session.current_state["active_persistent_states"] == expected


@pytest.mark.parametrize("weather", ["", "阴雨", "暴雨", "极端"])
def test_undeclared_weather_automation_keeps_existing_behavior(weather):
    session, _ = make_session()
    expected = [builtin_weather_state(weather)] if weather else []
    session.current_state["current_weather"] = weather
    session.current_state["active_persistent_states"] = ["test_knight_oath", builtin_weather_state("暴雨")]
    session._apply_weather_effects()
    session._apply_weather_effects()
    assert session.current_state["active_persistent_states"] == ["test_knight_oath", *expected]


def test_builtin_weather_categories_remain_distinct():
    assert len({builtin_weather_state(weather) for weather in ("阴雨", "暴雨", "极端")}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("state_id", ["test_knight_oath", "builtin_weather"])
async def test_retrieved_declaration_reaches_projection_stream_persistence_and_branches(tmp_path, monkeypatch, state_id):
    if state_id == "builtin_weather":
        state_id = builtin_weather_state("阴雨")
    session, script = make_session(state_id)
    session.save_id = "synthetic-state-save"
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = []

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-state-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = max(completed, default=-1) + 1
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the declared condition before proposing its activation."}
        elif index == 1:
            name, arguments = "consult_rules", {"category": "persistent_states"}
        elif index == 2:
            rules = json.loads(completed[1])
            definition = json.loads(rules["entries"][0]["content"])["definition"]
            assert definition == script["persistent_states"][0]
            name, arguments = "update_state", {
                "activate_states": [{key: definition[key] for key in ("id", "name", "description")}],
                "end_time": "2030-01-01T09:01:00",
            }
        elif index == 3:
            assert "过滤" not in completed[2]
            name, arguments = "query_lorebook", {"entry_id": "test_notice"}
        elif index == 4:
            lore = json.loads(completed[3])
            assert [entry["id"] for entry in lore["entries"]] == ["test_notice"]
            assert lore["projection"]["provisional"] is True
            assert lore["projection"]["event_ids"] == ["test_check_state"]
            assert state_id not in session.current_state["active_persistent_states"]
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 5
            name, arguments = "finalize_turn", {"summary": "Inspected the shelf."}
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{"id": f"{prefix}{index}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    root_id = session.world_tree.add_node(
        None, script["start_time"], 0, ai_response="You are in the reading room.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    root_state = copy.deepcopy(session.current_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state-declarations.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    endpoint = f"/api/game/{session.save_id}/action/stream"
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": root_state, "narrative": "You are in the reading room.",
        })
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            action = {
                "type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                "stream_id": "synthetic-state-stream", "action_id": "synthetic-state-action",
                "expected_node_id": root_id,
            }
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200
            assert response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert final["state"]["active_persistent_states"] == [state_id]
            assert session._evaluate_condition(script["one_time_events"][0]["condition"]) is True
            assert session.current_state["fired_one_time_events"] == ["test_check_state"]
            assert len(requests) == 6
            calls_before_replay = provider.generate_with_tools.await_count
            replay = await client.post(endpoint, json=action)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == calls_before_replay
            final_state = copy.deepcopy(session.current_state)
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == final_state
            rules = json.loads(restored._handle_consult_rules({"category": "persistent_states"}))
            assert json.loads(rules["entries"][0]["content"])["definition"] == script["persistent_states"][0]
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            assert node["agent_trace"]["rounds"]
            for marker in ("SYNTHETIC_DECLARED_STATE_RULE", "SYNTHETIC_DECLARED_STATE_LORE"):
                assert marker in json.dumps(requests)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"])
            await restored.branch_to_node(root_id)
            assert restored.current_state["active_persistent_states"] == []
            assert restored._evaluate_condition(script["one_time_events"][0]["condition"]) is False
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["active_persistent_states"] == [state_id]
            assert restored._evaluate_condition(script["one_time_events"][0]["condition"]) is True
            assert restored.current_state["fired_one_time_events"] == ["test_check_state"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
