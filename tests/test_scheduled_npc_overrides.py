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


def make_session(category="one_time_events", threshold=2):
    event = {
        "id": "test_shift", "description": "A practice shift begins.",
        "condition": f"script_variables.test_ready >= {threshold}",
        "rule_notes": "SYNTHETIC_SHIFT_RULE_BODY",
        "preparation": {"variable_changes": [{"id": "test_ready", "op": "set", "value": threshold}]},
        "effects": [{
            "type": "schedule_override", "npc_id": "test_guide", "time_range": "09:00-10:00",
            "location": "test_desk", "activity": "Practice shift", "priority": 100,
            "expires": "2030-01-01T09:30:00",
        }, {"type": "activate_lore", "id": "test_notice"}],
    }
    if category == "one_time_events":
        event["trigger_time"] = "2030-01-01T09:05:00"
    else:
        event.update({"first_trigger": "2030-01-01T09:05:00", "frequency_value": 5, "frequency_unit": "minute"})
    script = {
        "script_id": "synthetic-scheduled-shift", "name": "Synthetic scheduled shift", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_origin"},
        "locations": [{"id": "test_origin", "name": "Origin"}, {"id": "test_desk", "name": "Practice desk"}],
        "npcs": [{"id": "test_guide", "name": "Practice guide", "default_location": "test_origin",
                  "schedule": [{"time_range": "09:00-10:00", "location": "test_origin", "activity": "Practice rest"}]}],
        "variables": [{"id": "test_ready", "type": "number", "default": 0}],
        "lorebook": [
            {"id": "test_manual", "keys": ["practice shift"], "content": json.dumps({"category": category, "query": event["id"]})},
            {"id": "test_notice", "enabled": False, "content": "SYNTHETIC_SHIFT_NOTICE_BODY"},
        ],
        category: [event],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = [location["id"] for location in script["locations"]]
    return session


def expected_override(session, category="one_time_events"):
    return {key: value for key, value in session.script[category][0]["effects"][0].items() if key != "type" and key != "npc_id"}


@pytest.mark.parametrize("path", ["definition", "projection", "settlement"])
def test_scheduled_effect_uses_existing_schedule_contract(path):
    session = make_session()
    session.current_state["script_variables"]["test_ready"] = 2
    original = copy.deepcopy(session.current_state)
    if path == "definition":
        session._apply_event_def_effects(session.script["one_time_events"][0], "test_shift")
        result = session.current_state
    elif path == "projection":
        _, result = session._projected_events({"old_time": original["game_time"],
                                              "_accepted_state_updates": [{"end_time": "2030-01-01T09:10:00"}]}, with_state=True)
        assert session.current_state == original
    else:
        session.current_state["game_time"] = "2030-01-01T09:10:00"
        session._settle_timed_events(original["game_time"], session.current_state["game_time"], [])
        result = session.current_state
    assert result.get("npc_schedule_overrides") == {"test_guide": [expected_override(session)]}
    result["game_time"] = "2030-01-01T09:10:00"
    assert session._get_npc_location("test_guide", result) == "test_desk"
    result["game_time"] = "2030-01-01T09:30:00"
    assert session._get_npc_location("test_guide", result) == "test_origin"


@pytest.mark.parametrize("category,count", [("one_time_events", 1), ("cyclic_events", 3)])
def test_projection_settlement_and_replay_have_identical_overrides(category, count):
    session = make_session(category)
    event = session.script[category][0]
    updates = [{**event["preparation"], "end_time": "2030-01-01T09:15:00"}]
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"], "_accepted_state_updates": updates}
    events, projected = session._projected_events(context, with_state=True)
    assert projected.get("npc_schedule_overrides") == {"test_guide": [expected_override(session, category)] * count}
    assert session.current_state == original
    assert session._projected_events(context, with_state=True) == (events, projected)
    records = [record for update in updates for record in session._explode_update_state(update)]
    parsed = session._merge_state_tool_results(records)
    session.current_state, _ = session._apply_common_parsed_changes(session.current_state, parsed, inplace=True)
    session.current_state["game_time"] = updates[0]["end_time"]
    fired = []
    session._settle_timed_events(original["game_time"], updates[0]["end_time"], fired)
    assert fired == [{key: value for key, value in event.items() if not key.startswith("_")} for event in events]
    assert session.current_state["npc_schedule_overrides"] == projected["npc_schedule_overrides"]
    settled = copy.deepcopy(session.current_state)
    session._settle_timed_events(original["game_time"], updates[0]["end_time"], [])
    assert session.current_state == settled


@pytest.mark.parametrize("missing", ["npc_id", "time_range", "location"])
def test_incomplete_scheduled_override_is_not_applied(missing):
    session = make_session()
    effect = copy.deepcopy(session.script["one_time_events"][0]["effects"][0])
    effect.pop(missing)
    session._apply_event_def_effects({"effects": [effect]}, "test_shift")
    assert "npc_schedule_overrides" not in session.current_state


@pytest.mark.parametrize("end_time,ready", [("2030-01-01T09:04:00", 2), ("2030-01-01T09:10:00", 0)])
def test_unfired_schedule_declaration_does_not_change_candidate(end_time, ready):
    session = make_session()
    original = copy.deepcopy(session.current_state)
    updates = [{"end_time": end_time, "variable_changes": [{"id": "test_ready", "op": "set", "value": ready}]}]
    events, projected = session._projected_events({"old_time": original["game_time"], "_accepted_state_updates": updates}, with_state=True)
    assert not events and "npc_schedule_overrides" not in projected
    assert session._get_npc_location("test_guide", projected) == "test_origin"
    assert session.current_state == original


@pytest.mark.parametrize("explicit_location", [None, "test_origin"])
def test_scheduled_override_preserves_priority_and_explicit_position(explicit_location):
    session = make_session()
    session.current_state["npc_schedule_overrides"] = {"test_guide": [{
        "time_range": "09:00-10:00", "location": "test_origin", "priority": 20, "activity": "Earlier shift",
    }]}
    if explicit_location:
        session.current_state["npcs"]["test_guide"]["current_location"] = explicit_location
    event = session.script["one_time_events"][0]
    context = {"old_time": session.current_state["game_time"],
               "_accepted_state_updates": [{**event["preparation"], "end_time": "2030-01-01T09:10:00"}]}
    _, projected = session._projected_events(context, with_state=True)
    assert len(projected["npc_schedule_overrides"]["test_guide"]) == 2
    assert session._get_npc_location("test_guide", projected) == (explicit_location or "test_desk")
    assert len(session.current_state["npc_schedule_overrides"]["test_guide"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("category,threshold,fail_save", [("one_time_events", 2, False), ("cyclic_events", 5, False),
                                                          ("one_time_events", 3, True)])
async def test_declared_shift_through_stream_restore_branch_and_replay(tmp_path, monkeypatch, category, threshold, fail_save):
    session = make_session(category, threshold)
    script = session.script
    session.save_id = "synthetic-shift-save"
    requests = []
    narrative = ("You read the posted practice instructions and make the necessary preparations. "
                 "You then wait quietly at your original seat until the practice shift begins. "
                 "The notice is ready for inspection, and you finish reading without disturbing the equipment.")

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-intent", "name": "classify_intent", "arguments": {"intent": "action"}}]}
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("synthetic-shift-")]
        index = len(completed)
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the practice instructions and prepare for the declared shift."}
        elif index == 1:
            name, arguments = "query_lorebook", {"entry_id": "test_manual"}
        elif index == 2:
            name, arguments = "consult_rules", json.loads(json.loads(completed[1])["entries"][0]["content"])
        elif index == 3:
            definition = json.loads(json.loads(completed[2])["entries"][0]["content"])["definition"]
            name, arguments = "update_state", {**definition["preparation"],
                                               "end_time": definition.get("trigger_time", definition.get("first_trigger"))}
        elif index == 4:
            definition = json.loads(json.loads(completed[2])["entries"][0]["content"])["definition"]
            assert "npc_schedule_overrides" not in session.current_state
            entry_id = next(effect["id"] for effect in definition["effects"] if effect["type"] == "activate_lore")
            name, arguments = "query_lorebook", {"entry_id": entry_id}
        elif index == 5:
            result = json.loads(completed[4])
            assert result["projection"]["provisional"] is True
            assert result["projection"]["event_ids"] == ["test_shift"]
            assert result["entries"][0]["content"] == "SYNTHETIC_SHIFT_NOTICE_BODY"
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 6
            name, arguments = "finalize_turn", {"summary": "Prepared for the declared practice shift and read the notice."}
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{"id": f"synthetic-shift-{index}", "name": name, "arguments": arguments}]}

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
    root_id = session.world_tree.add_node(None, script["start_time"], 0,
                                         ai_response="You are at the practice origin.", state_snapshot=root_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "scheduled-shift.db")
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
            action = {"type": "freeform", "text": "Read the practice shift instructions, prepare, and wait until the shift begins.",
                      "action_id": "synthetic-shift-action", "expected_node_id": root_id}
            if fail_save:
                save_node = game_routes._save_node

                async def reject_save(current, result):
                    assert current.current_state["npc_schedule_overrides"] == {"test_guide": [expected_override(current, category)]}
                    raise RuntimeError("Synthetic save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failed = await client.post(endpoint, json=action)
                assert [event for event in _stream_events(failed.text) if event["type"] == "error"]
                assert "npc_schedule_overrides" not in session.current_state
                assert session._get_npc_location("test_guide") == "test_origin"
                assert session.current_state["script_variables"]["test_ready"] == 0
                assert session.world_tree.active_node_id == root_id
                requests.clear()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json=action)
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] == "error"]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative and len(requests) == 7
            expected = {"test_guide": [expected_override(session, category)]}
            assert session.current_state["npc_schedule_overrides"] == expected
            assert session._get_npc_location("test_guide") == "test_desk"
            assert session.current_state["script_variables"]["test_ready"] == threshold
            final_state = copy.deepcopy(session.current_state)
            calls_before = provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert provider.generate_with_tools.await_count == calls_before
            assert session.current_state == final_state
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == final_state
            assert restored._get_npc_location("test_guide") == "test_desk"
            repeated = []
            restored._settle_timed_events(script["start_time"], final_state["game_time"], repeated)
            assert repeated == [] and restored.current_state == final_state
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            for marker in ("SYNTHETIC_SHIFT_RULE_BODY", "SYNTHETIC_SHIFT_NOTICE_BODY"):
                assert marker in json.dumps(requests, ensure_ascii=False)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"], ensure_ascii=False)
            await restored.branch_to_node(root_id)
            assert "npc_schedule_overrides" not in restored.current_state
            assert restored._get_npc_location("test_guide") == "test_origin"
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["npc_schedule_overrides"] == expected
            assert restored._get_npc_location("test_guide") == "test_desk"
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
