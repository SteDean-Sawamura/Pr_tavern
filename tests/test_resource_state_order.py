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


def make_session(initial=0):
    script = {
        "script_id": "synthetic-resource-order", "name": "Synthetic resource order", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {
            "name": "Visitor", "initial_location": "test_room",
            "attributes": {"test_focus": {"value": initial, "min": -100, "max": 100}},
        },
        "locations": [{"id": "test_room", "name": "Reading room"}],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    return session


@pytest.mark.parametrize("initial,operations,expected", [
    (0, [("set", 60), ("set", 90)], 60),
    (0, [("set", -60), ("set", -90)], -60),
    (0, [("add", 10), ("set", 50)], 40),
    (0, [("subtract", 10), ("set", -50)], -40),
    (0, [("add", 100), ("set", 100)], 60),
    (0, [("set", 60), ("subtract", 10)], 20),
    (12.5, [("set", 60), ("set", 90)], 72.5),
    (80, [("set", 200), ("set", 60)], 70),
])
@pytest.mark.parametrize("separate_calls", [False, True])
@pytest.mark.parametrize("inplace", [False, True])
def test_resource_changes_use_each_preceding_value(initial, operations, expected, separate_calls, inplace):
    session = make_session(initial)
    changes = [{"target": "player.attributes.test_focus", "op": operation, "value": value,
                "reason": "Synthetic practice step."} for operation, value in operations]
    updates = ([{"state_changes": [change]} for change in changes] if separate_calls
               else [{"state_changes": changes}])
    original = copy.deepcopy(session.current_state)
    unchanged_updates = copy.deepcopy(updates)
    context = {"old_time": original["game_time"]}
    records = []
    for update in updates:
        result = session._handle_update_state(update, context)
        assert not result.startswith("错误:"), result
        records.append({"name": "update_state", "args": update, "result": result})
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    source = copy.deepcopy(original)
    settled, log = session._apply_common_parsed_changes(source, parsed, inplace=inplace)
    assert projected["player"]["attributes"]["test_focus"] == expected
    assert settled["player"]["attributes"]["test_focus"] == expected
    resource_log = [entry for entry in log if entry.get("target") == changes[0]["target"]]
    assert len(resource_log) == len(changes)
    assert resource_log[0]["old"] == initial
    assert resource_log[-1]["new"] == expected
    assert all(current["old"] == previous["new"] for previous, current in zip(resource_log, resource_log[1:]))
    assert updates == unchanged_updates
    assert session.current_state == original
    if not inplace:
        assert source == original


@pytest.mark.parametrize("exclusion", ["superseded", "deferred", "undone", "failed"])
def test_excluded_resource_updates_do_not_change_the_next_delta_baseline(exclusion):
    session = make_session()
    records = [{"name": "update_state", "args": {"state_changes": [{
        "target": "player.attributes.test_focus", "op": "set", "value": value,
    }]}, "result": "accepted"} for value in (60, -90, 90)]
    context = {}
    if exclusion == "undone":
        context["_undone_record_indexes"] = [1]
    elif exclusion == "failed":
        records[1]["result"] = "错误: rejected synthetic change"
    else:
        records[1][exclusion] = True
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["player"]["attributes"]["test_focus"] == 60


@pytest.mark.parametrize("merge_tools", [False, True])
def test_resource_filtering_paths_and_declarative_effects_keep_their_contracts(merge_tools):
    session = make_session()
    update = {"state_changes": [
        {"target": "player.attributes.test_focus", "op": "add", "value": 20},
        {"target": "player.test_focus", "op": "set", "value": 90},
        {"target": "player.attributes.test_focus", "op": "set", "value": 100},
        {"target": "player.attributes.test_other", "op": "set", "value": "ready"},
    ]}
    parsed = session._merge_state_tool_results(session._explode_update_state(update)) if merge_tools else update
    settled, log = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["player"]["attributes"]["test_focus"] == 60
    assert settled["player"]["attributes"]["test_other"] == "ready"
    assert len([entry for entry in log if entry.get("target", "").startswith("player.")]) == 3
    projected = session._project_state_tool_updates([update])
    assert projected["player"]["attributes"] == settled["player"]["attributes"]
    direct, _ = session.state_manager.apply_changes(session.current_state, [{
        "target": "player.attributes.test_focus", "op": "add", "value": 90,
    }])
    assert direct["player"]["attributes"]["test_focus"] == 90


@pytest.mark.parametrize("values,expected", [([60, 90], 60), ([25, 75], 55)])
def test_regenerated_resource_changes_use_the_same_sequence(values, expected):
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    updates = [{"state_changes": [{"target": "player.attributes.test_focus", "op": "set", "value": value}]}
               for value in values]
    parsed = session._merge_state_tool_results([
        call for update in updates for call in session._explode_update_state(update)
    ])
    parsed["end_time"] = "2030-01-01T09:01:00"
    state, changes, _ = session._settle_regen_state(parsed, baseline, [])
    assert state["player"]["attributes"]["test_focus"] == expected
    resource_log = [entry for entry in changes if entry.get("target") == "player.attributes.test_focus"]
    assert resource_log[-1]["old"] == resource_log[0]["new"]
    assert resource_log[-1]["new"] == expected
    assert baseline["player"]["attributes"]["test_focus"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("values,threshold,expected", [([60, 90], 50, 60), ([25, 75], 40, 55)])
async def test_declared_resource_steps_reach_projected_lore_and_saved_event(tmp_path, monkeypatch, values, threshold, expected):
    script = copy.deepcopy(make_session().script)
    manual = {"completion_event": "test_resource_gate", "steps": [{"state_changes": [{
        "target": "player.attributes.test_focus", "op": "set", "value": value,
    }]} for value in values]}
    script["lorebook"] = [
        {"id": "test_manual", "content": json.dumps(manual)},
        {"id": "test_notice", "enabled": False, "content": "SYNTHETIC_RESOURCE_OUTCOME_BODY"},
    ]
    script["one_time_events"] = [{
        "id": "test_resource_gate", "trigger_time": "2030-01-01T09:01:00",
        "condition": f"player.attributes.test_focus >= {threshold}",
        "rule_notes": "SYNTHETIC_RESOURCE_RULE_BODY",
        "effects": [{"type": "activate_lore", "id": "test_notice"}],
    }]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.save_id = "synthetic-resource-save"
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = []

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-resource-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = max(completed, default=-1) + 1
        if index >= 2:
            declaration = json.loads(json.loads(completed[1])["entries"][0]["content"])
        if index >= 3:
            definition = json.loads(json.loads(completed[2])["entries"][0]["content"])["definition"]
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the practice manual and its completion rule before acting."}
        elif index == 1:
            name, arguments = "query_lorebook", {"entry_id": "test_manual"}
        elif index == 2:
            name, arguments = "consult_rules", {"category": "one_time_events", "query": declaration["completion_event"]}
        elif index in (3, 4):
            name, arguments = "update_state", copy.deepcopy(declaration["steps"][index - 3])
            if index == 4:
                arguments["end_time"] = definition["trigger_time"]
        elif index == 5:
            name, arguments = "query_lorebook", {"entry_id": definition["effects"][0]["id"]}
        elif index == 6:
            lore = json.loads(completed[5])
            assert [entry["id"] for entry in lore["entries"]] == [definition["effects"][0]["id"]]
            assert lore["projection"]["provisional"] is True
            assert lore["projection"]["event_ids"] == [definition["id"]]
            assert session.current_state["player"]["attributes"]["test_focus"] == 0
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 7
            name, arguments = "finalize_turn", {"summary": "Inspected the shelf."}
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
        None, script["start_time"], 0, ai_response="You are in the reading room.", state_snapshot=root_state,
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "resource-order.db")
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
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": "You are in the reading room."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Read the test_manual and practice its declared steps.",
                      "intent_hint": "explicit_action", "stream_id": "synthetic-resource-stream",
                      "action_id": "synthetic-resource-action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert final["state"]["player"]["attributes"]["test_focus"] == expected
            assert session.current_state["fired_one_time_events"] == ["test_resource_gate"]
            assert session._evaluate_condition(script["one_time_events"][0]["condition"]) is True
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
            for marker in ("SYNTHETIC_RESOURCE_RULE_BODY", "SYNTHETIC_RESOURCE_OUTCOME_BODY"):
                assert marker in json.dumps(requests)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"])
            await restored.branch_to_node(root_id)
            assert restored.current_state["player"]["attributes"]["test_focus"] == 0
            assert restored._evaluate_condition(script["one_time_events"][0]["condition"]) is False
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["player"]["attributes"]["test_focus"] == expected
            assert restored.current_state["fired_one_time_events"] == ["test_resource_gate"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
