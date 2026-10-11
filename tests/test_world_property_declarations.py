import asyncio
import copy
import json
import re
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


def make_session(property_id="test_flight_capacity", gate_value=None):
    condition = f"world_properties.{property_id} == open"
    if gate_value is not None:
        condition += f" AND script_variables.test_inspected == {str(gate_value).lower()}"
    script = {
        "script_id": "synthetic-world-declarations", "name": "Synthetic world declarations", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [
            {"id": "test_room", "name": "Reading room", "connections": [],
             "conditional_connections": [{"target": "test_archive", "condition": condition}]},
            {"id": "test_archive", "name": "Archive", "connections": []},
        ],
        "world_properties": [{
            "id": property_id, "name": "Synthetic access", "value": "closed",
            "rule_notes": "SYNTHETIC_WORLD_RULE", "inspection_result": "open",
        }],
        "lorebook": [{"id": "test_notice", "enabled": False, "content": "SYNTHETIC_WORLD_LORE"}],
        "one_time_events": [{
            "id": "test_check_access", "trigger_time": "2030-01-01T09:01:00", "condition": condition,
            "effects": [{"type": "activate_lore", "id": "test_notice"}],
        }],
    }
    if gate_value is not None:
        script["variables"] = [{"id": "test_inspected", "type": "bool", "default": not gate_value}]
        script["world_properties"][0]["inspection_changes"] = {
            "variable_changes": [{"id": "test_inspected", "op": "set", "value": gate_value}],
        }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    return session, script


@pytest.mark.parametrize("property_id", [
    "test_door_status", "test_flight_capacity", "test_document_access", "test_门禁", "test_capacity",
])
@pytest.mark.parametrize("stage", ["feedback", "projection", "settlement"])
def test_declared_properties_survive_name_heuristics(property_id, stage):
    session, script = make_session(property_id)
    original = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(script)
    update = {"world_property_changes": [{"id": property_id, "value": "open"}]}
    if stage == "feedback":
        assert "过滤" not in session._run_state_tool("update_world", update)
    elif stage == "projection":
        projected = session._project_state_tool_updates([update])
        assert projected["world_properties"][property_id] == "open"
    else:
        settled, _ = session._apply_common_parsed_changes(copy.deepcopy(original), update)
        assert settled["world_properties"][property_id] == "open"
    assert session.current_state == original
    assert script == original_script


@pytest.mark.parametrize("property_id", ["test_door_status", "test_flight_capacity", "test_门禁", "TEST_DOCUMENT_ACCESS"])
def test_runtime_presence_does_not_exempt_undeclared_object_properties(property_id):
    session, _ = make_session("test_document_access")
    session.current_state["world_properties"][property_id] = "closed"
    original = copy.deepcopy(session.current_state)
    update = {"world_property_changes": [{"id": property_id, "value": "open"}]}
    assert "过滤物件级属性" in session._run_state_tool("update_world", update)
    projected = session._project_state_tool_updates([update])
    assert projected["world_properties"][property_id] == "closed"
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(original), update)
    assert settled["world_properties"][property_id] == "closed"
    assert session.current_state == original


def test_undeclared_macro_properties_keep_existing_behavior():
    session, _ = make_session()
    update = {"world_property_changes": [{"id": "test_new_capacity", "value": "open"}]}
    assert "过滤" not in session._run_state_tool("update_world", update)
    assert session._project_state_tool_updates([update])["world_properties"]["test_new_capacity"] == "open"
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), update)
    assert settled["world_properties"]["test_new_capacity"] == "open"


@pytest.mark.parametrize("stage", ["connections", "projection", "settlement"])
@pytest.mark.parametrize("initial_value,new_value", [("closed", "open"), ("open", "closed")])
def test_conditional_map_uses_the_supplied_state_without_committing_it(stage, initial_value, new_value):
    session, script = make_session("test_capacity")
    session.current_state["world_properties"]["test_capacity"] = initial_value
    if initial_value == "open":
        session.current_state["location_connections"] = {"test_room": ["test_archive"], "test_archive": ["test_room"]}
    original = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(script)
    update = {"world_property_changes": [{"id": "test_capacity", "value": new_value}]}
    if stage == "connections":
        candidate = copy.deepcopy(original)
        candidate["world_properties"]["test_capacity"] = new_value
        session._update_dynamic_connections(candidate)
    elif stage == "projection":
        candidate = session._project_state_tool_updates([update])
    else:
        candidate, _ = session._apply_common_parsed_changes(copy.deepcopy(original), update)
    connections = candidate["location_connections"]
    assert ("test_archive" in connections.get("test_room", [])) == (new_value == "open")
    assert ("test_room" in connections.get("test_archive", [])) == (new_value == "open")
    assert session.current_state == original
    assert script == original_script


def test_ordered_world_updates_recompute_candidate_connections():
    session, _ = make_session()
    original = copy.deepcopy(session.current_state)
    updates = [{"world_property_changes": [{"id": "test_flight_capacity", "value": value}]}
               for value in ("open", "closed", "open")]
    for count in range(1, 4):
        projected = session._project_state_tool_updates(updates[:count])
        assert projected["world_properties"]["test_flight_capacity"] == ("closed" if count == 2 else "open")
        assert ("test_archive" in projected["location_connections"].get("test_room", [])) == (count != 2)
        assert session.current_state == original


@pytest.mark.parametrize("condition", [
    "world_properties.test_capacity == open",
    "player.health >= 5",
    "node_completed.test_node == true",
    "milestone.test_goal == true",
    "event.test_notice.fired",
    "event_fired.test_notice == true",
    "event.test_cycle.fired == true",
    "status.test_condition == active",
    "world_properties.test_capacity == open AND player.health >= 5",
    "world_properties.test_capacity == closed OR player.health >= 5",
])
def test_explicit_condition_snapshot_is_used_for_all_predicates(condition):
    session, _ = make_session("test_capacity")
    candidate = {
        "world_properties": {"test_capacity": "open"}, "player": {"health": 9},
        "story_tree_state": {"completed": ["test_node"]}, "achieved_milestones": ["test_goal"],
        "fired_one_time_events": ["test_notice"],
        "cyclic_event_trackers": {"test_cycle": {"last_fired": "2030-01-01T09:01:00"}},
        "active_persistent_states": ["test_condition"],
    }
    original = copy.deepcopy(session.current_state)
    assert session._evaluate_condition(condition, candidate) is True
    assert session.current_state == original
    session.current_state = candidate
    assert session._evaluate_condition(condition) is True
    assert session._evaluate_condition(condition, {}) is False


def test_rule_query_searches_and_rereads_full_committed_world_values():
    session, script = make_session()
    value = "SYNTHETIC_VALUE_DETAIL " * 300 + "FINAL_VALUE_CLAUSE"
    session.current_state["world_properties"]["test_flight_capacity"] = value
    original = copy.deepcopy(session.current_state)
    result = json.loads(session._handle_consult_rules({"category": "world_properties", "query": "final_value_clause"}))
    entry, = result["entries"]
    chunks = [entry["content"]]
    while entry["next_offset"] is not None:
        result = json.loads(session._handle_consult_rules({
            "category": "world_properties", "rule_index": entry["rule_index"], "offset": entry["next_offset"],
            "snapshot": entry["snapshot"],
        }))
        entry, = result["entries"]
        assert entry["offset"] == sum(map(len, chunks))
        chunks.append(entry["content"])
    payload = json.loads("".join(chunks))
    assert payload["definition"] == script["world_properties"][0]
    assert payload["current_value"] == value
    assert sum(map(len, chunks)) == entry["total_chars"]
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_change", [None, "update", "undo"])
@pytest.mark.parametrize("gate_value", [None, True, False])
async def test_retrieved_world_rule_reaches_projection_stream_persistence_and_branches(tmp_path, monkeypatch, cursor_change, gate_value):
    session, script = make_session(gate_value=gate_value)
    session.save_id = "synthetic-world-save"
    property_id = script["world_properties"][0]["id"]
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = []
    base_index = 0
    cursor_phase = "seed"
    cursor_calls = 0
    cursor_entry = None
    recovered_payload = None
    chunks = []
    snapshots = set()

    async def generate_with_tools(messages, **kwargs):
        nonlocal base_index, cursor_phase, cursor_calls, cursor_entry, recovered_payload
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-world-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = base_index
        if cursor_change and index == 1 and cursor_phase != "done":
            operation = None
            if cursor_phase == "seed":
                operation = ("update_state", {"world_property_changes": [
                    {"id": property_id, "value": "SYNTHETIC_CURSOR_BEFORE_" * 200},
                ]})
                cursor_phase = "first_read"
            elif cursor_phase == "first_read":
                operation = ("consult_rules", {"category": "world_properties", "include_candidates": True})
                cursor_phase = "change"
            elif cursor_phase == "change":
                latest = next(message["content"] for message in reversed(messages) if message.get("role") == "tool")
                cursor_entry, = json.loads(latest)["entries"]
                assert cursor_entry["next_offset"] == 2000
                snapshots.add(cursor_entry["snapshot"])
                if cursor_change == "update":
                    operation = ("update_state", {"world_property_changes": [
                        {"id": property_id, "value": "SYNTHETIC_CURSOR_AFTER_" * 200},
                    ]})
                else:
                    operation = ("undo_my_last_action", {"reason": "Discard the synthetic provisional value."})
                cursor_phase = "stale_read"
            elif cursor_phase == "stale_read":
                operation = ("consult_rules", {
                    "category": "world_properties", "include_candidates": True,
                    "rule_index": cursor_entry["rule_index"], "offset": cursor_entry["next_offset"],
                    "snapshot": cursor_entry["snapshot"],
                })
                cursor_phase = "restart"
            elif cursor_phase == "restart":
                latest = next(message["content"] for message in reversed(messages) if message.get("role") == "tool")
                result = json.loads(latest)
                assert result["error_code"] == "snapshot_expired" and result["restart_required"] is True
                assert "entries" not in result
                operation = ("consult_rules", result["restart"])
                cursor_phase = "reread"
            else:
                latest = next(message["content"] for message in reversed(messages) if message.get("role") == "tool")
                result = json.loads(latest)
                entry, = result["entries"]
                assert entry["offset"] == sum(map(len, chunks))
                snapshots.add(entry["snapshot"])
                chunks.append(entry["content"])
                if entry["next_offset"] is not None:
                    operation = ("consult_rules", {
                        "category": "world_properties", "include_candidates": True,
                        "rule_index": entry["rule_index"], "offset": entry["next_offset"], "snapshot": entry["snapshot"],
                    })
                else:
                    recovered_payload = json.loads("".join(chunks))
                    assert recovered_payload["current_value"] == "closed"
                    expected = "SYNTHETIC_CURSOR_AFTER_" * 200 if cursor_change == "update" else "closed"
                    assert recovered_payload["candidate_value"] == expected
                    assert recovered_payload["definition"] == script["world_properties"][0]
                    assert "SYNTHETIC_CURSOR_BEFORE_" not in "".join(chunks)
                    cursor_phase = "done"
            if operation:
                name, arguments = operation
                schema = next(tool["function"]["parameters"] for tool in kwargs["tools"] if tool["function"]["name"] == name)
                assert arguments.keys() <= schema["properties"].keys()
                assert set(schema.get("required", [])) <= arguments.keys()
                if "snapshot" in arguments:
                    assert re.fullmatch(schema["properties"]["snapshot"]["pattern"], arguments["snapshot"])
                cursor_calls += 1
                return {"content": "", "tool_calls": [{
                    "id": f"synthetic-world-cursor-{cursor_calls}", "name": name, "arguments": arguments,
                }]}
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the declared world rule before proposing its update."}
        elif index == 1:
            name, arguments = "consult_rules", {"category": "world_properties"}
        elif index == 2:
            payload = json.loads(json.loads(completed[1])["entries"][0]["content"])
            definition = (recovered_payload or payload)["definition"]
            assert definition == script["world_properties"][0]
            assert payload["current_value"] == "closed"
            name, arguments = "update_state", {
                "world_property_changes": [{"id": definition["id"], "value": definition["inspection_result"]}],
                "end_time": "2030-01-01T09:01:00",
                **copy.deepcopy(definition.get("inspection_changes", {})),
            }
        elif index == 3:
            assert "过滤" not in completed[2]
            assert f'"{property_id}": "open"' in completed[2]
            name, arguments = "consult_rules", {"category": "world_properties", "include_candidates": True}
        elif index == 4:
            result = json.loads(completed[3])
            assert result["projection"]["provisional"] is True
            payload = json.loads(result["entries"][0]["content"])
            assert payload["current_value"] == "closed"
            assert payload["candidate_value"] == "open"
            assert payload["candidate_exists"] is True
            assert session.current_state["world_properties"][property_id] == "closed"
            name, arguments = "query_lorebook", {"entry_id": "test_notice"}
        elif index == 5:
            lore = json.loads(completed[4])
            assert [entry["id"] for entry in lore["entries"]] == ["test_notice"]
            assert lore["projection"]["provisional"] is True
            assert lore["projection"]["event_ids"] == ["test_check_access"]
            assert session.current_state["world_properties"][property_id] == "closed"
            assert "test_archive" not in session.current_state["location_connections"].get("test_room", [])
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 6
            name, arguments = "finalize_turn", {"summary": "Inspected the shelf.", "choices": []}
        parameters = next(tool["function"]["parameters"] for tool in kwargs["tools"] if tool["function"]["name"] == name)
        assert arguments.keys() <= parameters["properties"].keys()
        assert set(parameters.get("required", [])) <= arguments.keys()
        if name == "consult_rules":
            assert arguments["category"] in parameters["properties"]["category"]["enum"]
        if name == "update_state":
            item_schema = parameters["properties"]["world_property_changes"]["items"]
            for entry in arguments["world_property_changes"]:
                assert entry.keys() <= item_schema["properties"].keys()
                assert set(item_schema["required"]) <= entry.keys()
                assert all(isinstance(value, str) for value in entry.values())
        base_index += 1
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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "world-declarations.db")
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
                "stream_id": "synthetic-world-stream", "action_id": "synthetic-world-action", "expected_node_id": root_id,
            }
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200
            assert response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert final["state"]["world_properties"][property_id] == "open"
            if gate_value is not None:
                assert "script_variables" not in final["state"]
                assert session.current_state["script_variables"]["test_inspected"] is gate_value
            assert final["state"]["location_connections"]["test_room"] == ["test_archive"]
            assert session.current_state["fired_one_time_events"] == ["test_check_access"]
            assert len(requests) == 7 + cursor_calls
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
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            assert node["agent_trace"]["rounds"]
            for marker in ("SYNTHETIC_WORLD_RULE", "SYNTHETIC_WORLD_LORE"):
                assert marker in json.dumps(requests)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"])
            if cursor_change:
                assert recovered_payload is not None and cursor_phase == "done"
                for marker in ("SYNTHETIC_CURSOR_BEFORE_", "SYNTHETIC_CURSOR_AFTER_", *snapshots):
                    assert marker not in response.text
                    assert marker not in json.dumps(node["agent_trace"])
            for node_id, value in ((root_id, "closed"), (final["node_id"], "open")):
                await restored.branch_to_node(node_id)
                assert restored.current_state["world_properties"][property_id] == value
                if gate_value is not None:
                    assert restored.current_state["script_variables"]["test_inspected"] is (gate_value if value == "open" else not gate_value)
                rules = json.loads(restored._handle_consult_rules({"category": "world_properties"}))
                payload = json.loads(rules["entries"][0]["content"])
                assert payload["definition"] == script["world_properties"][0]
                assert payload["current_value"] == value
                assert restored._evaluate_condition(script["one_time_events"][0]["condition"]) == (value == "open")
                assert ("test_archive" in restored.current_state["location_connections"].get("test_room", [])) == (value == "open")
            assert restored.current_state["fired_one_time_events"] == ["test_check_access"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
