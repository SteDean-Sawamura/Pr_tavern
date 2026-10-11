import asyncio
import copy
import json
from collections import OrderedDict
from contextlib import aclosing
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


def make_session(condition="test_access_level >= 2"):
    script = {
        "script_id": "synthetic-player-access", "name": "Synthetic player access", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_origin", "default_room": "seat"},
        "variables": [{"id": "test_access_level", "type": "number", "default": 0}],
        "locations": [
            {"id": "test_origin", "name": "Origin", "default_room": "entry"},
            {"id": "test_gate", "name": "Practice gate", "default_room": "lobby",
             "description": "A synthetic practice entrance with a posted access requirement.",
             "access_condition": condition, "access_blocked_reason": "Synthetic access condition not met."},
            {"id": "test_stop", "name": "Practice stop", "default_room": "bench"},
            {"id": "test_empty", "name": "Empty practice area"},
        ],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["player"].setdefault("attributes", {})["test_entry_score"] = 0
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = [location["id"] for location in script["locations"]]
    return session


def compare_settlement(session, updates, expected_location, expected_room, blocked=False):
    baseline = copy.deepcopy(session.current_state)
    original_updates = copy.deepcopy(updates)
    projected = session._project_state_tool_updates(updates)
    records = [record for update in updates for record in session._explode_update_state(update)]
    parsed = session._merge_state_tool_results(records)
    for inplace in (False, True):
        settled, _ = session._apply_common_parsed_changes(copy.deepcopy(baseline), copy.deepcopy(parsed), inplace=inplace)
        assert settled["player"]["location"] == expected_location
        assert settled["player"]["current_room"] == expected_room
        assert projected["player"] == settled["player"]
        assert projected["inventory"] == settled["inventory"]
        assert projected.get("location_access_blocked") == settled.get("location_access_blocked")
        assert bool(settled.get("location_access_blocked")) is blocked
    assert session.current_state == baseline
    assert updates == original_updates
    return projected


@pytest.mark.parametrize("level", [0, 2])
def test_candidate_access_uses_declared_condition_and_default_room(level):
    session = make_session()
    session.current_state["script_variables"]["test_access_level"] = level
    compare_settlement(session, [{"location_change": "test_gate"}],
                       "test_gate" if level else "test_origin", "lobby" if level else "seat", not level)


@pytest.mark.parametrize("source", ["variable", "attribute", "world", "status"])
@pytest.mark.parametrize("order", ["before", "after", "together"])
def test_access_uses_all_pre_movement_changes(source, order):
    conditions = {"variable": "test_access_level >= 2", "attribute": "player.test_entry_score >= 2",
                  "world": "world_properties.test_open == true", "status": "status.test_ready == active"}
    updates_by_source = {
        "variable": {"variable_changes": [{"id": "test_access_level", "op": "set", "value": 2}]},
        "attribute": {"state_changes": [{"target": "player.test_entry_score", "op": "set", "value": 2}]},
        "world": {"world_property_changes": [{"id": "test_open", "value": True}]},
        "status": {"activate_states": [{"id": "test_ready", "name": "Practice readiness"}]},
    }
    session = make_session(conditions[source])
    resource = updates_by_source[source]
    movement = {"location_change": "test_gate"}
    updates = ([resource, movement] if order == "before" else [movement, resource] if order == "after"
               else [{**resource, **movement}])
    compare_settlement(session, updates, "test_gate", "lobby")


@pytest.mark.parametrize("updates,expected_location,expected_room,blocked", [
    ([{"location_change": "test_stop"}, {"location_change": "test_origin"}], "test_origin", "seat", False),
    ([{"location_change": "test_stop"}, {"location_change": "test_gate"}], "test_origin", "seat", True),
    ([{"location_change": "test_gate"}, {"location_change": "test_stop"}], "test_stop", "bench", False),
    ([{"location_change": "test_stop"}, {"location_change": "test_unknown"}], "test_stop", "bench", False),
    ([{"location_change": "test_empty"}], "test_empty", "", False),
    ([{"room_changes": [{"id": "player", "new_room": "desk"}]}, {"location_change": "test_stop"}], "test_stop", "desk", False),
    ([{"state_changes": [{"target": "player.current_room", "op": "set", "value": "desk"}]},
      {"location_change": "test_stop"}], "test_stop", "bench", False),
])
def test_only_final_valid_destination_is_applied(updates, expected_location, expected_room, blocked):
    compare_settlement(make_session(), updates, expected_location, expected_room, blocked)


@pytest.mark.parametrize("action,condition", [("add", 'inventory == "[]"'), ("remove", 'inventory != "[]"')])
@pytest.mark.parametrize("order", ["before", "after", "together"])
def test_inventory_operations_remain_after_access_check(action, condition, order):
    session = make_session(condition)
    if action == "remove":
        session.current_state["inventory"] = [{"item": "Practice token", "quantity": 1}]
    inventory = {"inventory_changes": [{"action": action, "item": "Practice token", "quantity": 1}]}
    movement = {"location_change": "test_gate"}
    updates = ([inventory, movement] if order == "before" else [movement, inventory] if order == "after"
               else [{**inventory, **movement}])
    compare_settlement(session, updates, "test_gate", "lobby")


@pytest.mark.parametrize("destination", ["test_stop", "test_origin"])
def test_success_clears_previous_block_without_resetting_same_location_room(destination):
    session = make_session()
    session.current_state["location_access_blocked"] = {"location_id": "test_gate", "reason": "Previous block"}
    compare_settlement(session, [{"location_change": destination}], destination,
                       "bench" if destination == "test_stop" else "seat")


def test_later_accepted_grant_recomputes_pending_move_without_resubmitting_it():
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    movement = {"location_change": "test_gate"}
    assert not session._handle_update_state(movement, context).startswith("错误:")
    blocked = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert blocked["player"]["location"] == "test_origin"
    assert blocked["location_access_blocked"]["location_id"] == "test_gate"
    grant = {"variable_changes": [{"id": "test_access_level", "op": "set", "value": 2}]}
    assert not session._handle_update_state(grant, context).startswith("错误:")
    assert context["_accepted_state_updates"] == [movement, grant]
    compare_settlement(session, context["_accepted_state_updates"], "test_gate", "lobby")
    assert session.current_state == baseline


@pytest.mark.parametrize("level", [0, 2])
@pytest.mark.parametrize("mode", ["copy", "inplace", "regenerate"])
@pytest.mark.parametrize("location_change", ["test_gate", {"id": "test_gate"}, {"location_id": "test_gate"}])
def test_settlement_checks_supplied_state_not_active_session(level, mode, location_change):
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    baseline["script_variables"]["test_access_level"] = level
    session.current_state["script_variables"]["test_access_level"] = 2 - level
    original = copy.deepcopy(session.current_state)
    parsed = {"location_change": location_change, "end_time": "2030-01-01T09:01:00"}
    if mode == "regenerate":
        settled, _, _ = session._settle_regen_state(parsed, baseline, [])
    else:
        settled, _ = session._apply_common_parsed_changes(copy.deepcopy(baseline), parsed, inplace=mode == "inplace")
    assert settled["player"]["location"] == ("test_gate" if level else "test_origin")
    assert settled["player"]["current_room"] == ("lobby" if level else "seat")
    assert bool(settled.get("location_access_blocked")) is (not level)
    assert baseline["player"]["location"] == "test_origin"
    assert session.current_state == original


def make_event_session(level):
    script = copy.deepcopy(make_session().script)
    manual = {"event_id": "test_entry_gate", "request": {
        "location_change": "test_gate",
        "variable_changes": [{"id": "test_access_level", "op": "set", "value": level}],
    }}
    script["lorebook"] = [
        {"id": "test_manual", "content": json.dumps(manual)},
        {"id": "test_notice", "enabled": False, "content": "SYNTHETIC_PLAYER_ACCESS_OUTCOME_BODY"},
    ]
    script["one_time_events"] = [{
        "id": "test_entry_gate", "trigger_time": "2030-01-01T09:01:00",
        "condition": "player.location == test_gate" + (" AND player.current_room == lobby" if level >= 2 else ""),
        "rule_notes": "SYNTHETIC_PLAYER_ACCESS_RULE_BODY",
        "effects": [{"type": "activate_lore", "id": "test_notice"}],
    }]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = [location["id"] for location in script["locations"]]
    return session


@pytest.mark.asyncio
async def test_undo_revokes_access_and_location_unlocked_lore():
    session = make_event_session(2)
    request = json.loads(session.script["lorebook"][0]["content"])["request"]
    operations = [
        ("submit_plan", {"plan": "Check a provisional move and then withdraw it."}),
        ("update_state", {**request, "end_time": session.script["one_time_events"][0]["trigger_time"]}),
        ("query_lorebook", {"entry_id": "test_notice"}),
        ("undo_my_last_action", {"reason": "Withdraw the provisional move."}),
        ("query_lorebook", {"entry_id": "test_notice"}),
        ("check_inventory", {}),
    ]
    requests = []

    async def respond(messages, **kwargs):
        index = len(requests)
        requests.append(copy.deepcopy(messages))
        name, arguments = operations[index]
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-access-undo-{index}", "name": name, "arguments": copy.deepcopy(arguments),
        }]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"], "action_text": "Inspect and withdraw a candidate movement."}
    completed = 0
    async with aclosing(session._execute_unified_agent(context, {}, {"text": context["action_text"]}, streaming=True)) as pipeline:
        async for event in pipeline:
            if event.get("type") == "tool_call":
                completed += 1
                if completed == len(operations) - 1:
                    break
    assert completed == len(operations) - 1
    results = {message["tool_call_id"]: message["content"] for message in requests[-1] if message.get("role") == "tool"}
    before = json.loads(results["synthetic-access-undo-2"])
    after = json.loads(results["synthetic-access-undo-4"])
    assert [entry["id"] for entry in before["entries"]] == ["test_notice"]
    assert before["projection"]["provisional"] is True
    assert results["synthetic-access-undo-3"].startswith("已撤销上一个工具调用（update_state）")
    assert after["entries"] == []
    assert context.get("_accepted_state_updates", []) == []
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [1, 2])
async def test_declared_player_access_flows_through_stream_and_persistence(tmp_path, monkeypatch, level):
    session = make_event_session(level)
    script = session.script
    session.save_id = "synthetic-player-access-save"
    requests = []
    narratives = {
        True: ("You read the practice instructions and satisfy the posted access requirement. "
               "You enter the practice gate and wait in its lobby, leaving the equipment undisturbed. "
               "The exercise is complete, and you check the available notice before resting."),
        False: ("You read the practice instructions and check the posted access requirement. "
                "The current value is insufficient, so you remain in your original seat without entering. "
                "You wait briefly and leave the surrounding equipment undisturbed while considering another attempt."),
    }

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-player-access-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = max(completed, default=-1) + 1
        if index >= 2:
            declaration = json.loads(json.loads(completed[1])["entries"][0]["content"])
        if index >= 3:
            location_text = json.loads(completed[2])["entries"][0]["content"]
            condition = next(line.removeprefix("进入条件: ") for line in location_text.splitlines() if line.startswith("进入条件: "))
            variable_id, required = condition.split(" >= ")
            change, = declaration["request"]["variable_changes"]
            assert change["id"] == variable_id
            permitted = change["value"] >= int(required)
        if index >= 4:
            definition = json.loads(json.loads(completed[3])["entries"][0]["content"])["definition"]
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the location access declaration and completion rule before attempting entry."}
        elif index == 1:
            name, arguments = "query_lorebook", {"entry_id": "test_manual"}
        elif index == 2:
            name, arguments = "query_lorebook", {"entry_id": "_kg_loc_" + declaration["request"]["location_change"]}
        elif index == 3:
            name, arguments = "consult_rules", {"category": "one_time_events", "query": declaration["event_id"]}
        elif index == 4:
            name, arguments = "update_state", {**copy.deepcopy(declaration["request"]), "end_time": definition["trigger_time"]}
        elif index == 5:
            name, arguments = "query_lorebook", {"entry_id": definition["effects"][0]["id"]}
        elif index == 6:
            lore = json.loads(completed[5])
            assert [entry["id"] for entry in lore["entries"]] == ([definition["effects"][0]["id"]] if permitted else [])
            if permitted:
                assert lore["projection"]["provisional"] is True
                assert lore["projection"]["event_ids"] == [definition["id"]]
            assert session.current_state["player"]["location"] == "test_origin"
            assert not session._evaluate_condition(definition["condition"])
            name, arguments = "write_narrative", {"text": narratives[permitted]}
        else:
            assert index == 7
            name, arguments = "finalize_turn", {"summary": "Checked the declared access requirement."}
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{"id": f"{prefix}{index}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(
        None, script["start_time"], 0, ai_response="You are at the practice origin.", state_snapshot=root_state,
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "player-access.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": "You are at the practice origin."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Read test_manual, inspect the posted access condition and attempt entry.",
                      "intent_hint": "explicit_action", "stream_id": "synthetic-player-access-stream",
                      "action_id": "synthetic-player-access-action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            permitted = level >= 2
            assert final["narrative"] == narratives[permitted]
            assert final["state"]["player"]["location"] == ("test_gate" if permitted else "test_origin")
            assert final["state"]["player"]["current_room"] == ("lobby" if permitted else "seat")
            assert session.current_state.get("fired_one_time_events", []) == (["test_entry_gate"] if permitted else [])
            assert bool(session.current_state.get("location_access_blocked")) is (not permitted)
            if not permitted:
                assert session.current_state["location_access_blocked"]["reason"] == script["locations"][1]["access_blocked_reason"]
            assert len(requests) == 8
            calls_before = provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert provider.generate_with_tools.await_count == calls_before
            final_state = copy.deepcopy(session.current_state)
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == final_state
            repeated = []
            restored._settle_timed_events(script["start_time"], final_state["game_time"], repeated)
            assert repeated == [] and restored.current_state == final_state
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            for marker in ("SYNTHETIC_PLAYER_ACCESS_RULE_BODY", "SYNTHETIC_PLAYER_ACCESS_OUTCOME_BODY"):
                if permitted or marker.endswith("RULE_BODY"):
                    assert marker in json.dumps(requests)
                else:
                    assert marker not in json.dumps(requests)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"])
            await restored.branch_to_node(root_id)
            assert restored.current_state["player"]["location"] == "test_origin"
            assert restored.current_state["player"]["current_room"] == "seat"
            assert not restored.current_state.get("location_access_blocked")
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["player"] == final_state["player"]
            assert restored.current_state.get("location_access_blocked") == final_state.get("location_access_blocked")
            assert restored.current_state.get("fired_one_time_events", []) == final_state.get("fired_one_time_events", [])
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
