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


def make_session():
    script = {
        "script_id": "synthetic-spatial-projection", "name": "Synthetic spatial projection", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_hall", "default_room": "entrance"},
        "locations": [{"id": location, "name": location} for location in ("test_hall", "test_annex")],
        "npcs": [{"id": "test_guide", "name": "Guide", "default_location": "test_hall", "default_room": "entrance"}],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["npcs"]["test_guide"].update({
        "current_location": "test_hall", "_derived_location": "test_hall", "_derived_room": "entrance",
    })
    return session


@pytest.mark.parametrize("updates,location,room,expected_player_room", [
    ([{"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]}], "test_annex", None, "entrance"),
    ([{"room_changes": [{"id": "test_guide", "new_room": "desk"}]}], "test_hall", "desk", "entrance"),
    ([{"room_changes": [{"id": "player", "new_room": "desk"}]}], "test_hall", "entrance", "desk"),
    ([{"room_changes": [{"id": "test_guide", "new_room": "entrance"}]},
      {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]}], "test_annex", "entrance", "entrance"),
    ([{"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]},
      {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_hall"}]},
      {"room_changes": [{"id": "test_guide", "new_room": "desk"}]}], "test_hall", "desk", "entrance"),
    ([{"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_dynamic"}]},
      {"reveal_locations": [{"id": "test_dynamic", "name": "Test extension"}]}], "test_dynamic", None, "entrance"),
    ([{"state_changes": [{"target": "npcs.test_guide.current_room", "op": "set", "value": "entrance"}]},
      {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]}], "test_annex", "entrance", "entrance"),
    ([{"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]},
      {"state_changes": [{"target": "npcs.test_guide.current_location", "op": "set", "value": "test_hall"}]}], "test_annex", None, "entrance"),
    ([{"state_changes": [{"target": "npcs.test_guide.current_location", "op": "set", "value": "test_hall"}]}], "test_hall", None, "entrance"),
])
@pytest.mark.parametrize("inplace", [False, True])
@pytest.mark.parametrize("batched", [False, True])
def test_projected_spatial_updates_match_settlement(updates, location, room, expected_player_room, inplace, batched):
    session = make_session()
    if batched:
        combined = {}
        for update in updates:
            for key, entries in update.items():
                combined.setdefault(key, []).extend(entries)
        updates = [combined]
    baseline = copy.deepcopy(session.current_state)
    original_updates = copy.deepcopy(updates)
    context = {"old_time": baseline["game_time"]}
    records = []
    for update in updates:
        result = session._handle_update_state(update, context)
        assert not result.startswith("错误:"), result
        records.append({"name": "update_state", "args": update, "result": result})
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    source = copy.deepcopy(baseline)
    settled, _ = session._apply_common_parsed_changes(source, parsed, inplace=inplace)
    npc = settled["npcs"]["test_guide"]
    assert npc["current_location"] == location
    assert npc.get("current_room") == room
    assert settled["player"]["current_room"] == expected_player_room
    assert projected["npcs"] == settled["npcs"]
    assert projected["player"] == settled["player"]
    assert session._evaluate_condition(f"npcs.test_guide.current_location == {location}", projected)
    if room:
        assert session._evaluate_condition(f"npcs.test_guide.current_room == {room}", projected)
    assert session.current_state == baseline
    assert updates == original_updates


def test_rejected_update_does_not_leave_candidate_spatial_changes():
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    result = session._handle_update_state({
        "location_change": "test_undiscovered",
        "npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}],
        "room_changes": [{"id": "test_guide", "new_room": "desk"}],
    }, context)
    assert result.startswith("错误:")
    assert not context.get("_accepted_state_updates")
    assert session._project_state_tool_updates([])["npcs"] == baseline["npcs"]
    assert session.current_state == baseline


@pytest.mark.parametrize("update", [
    {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_missing"}]},
    {"npc_location_changes": [{"npc_id": "test_missing", "new_location": "test_annex"}]},
    {"npc_location_changes": [{"npc_id": "test_guide", "new_location": ""}]},
    {"room_changes": [{"id": "test_missing", "new_room": "desk"}]},
    {"room_changes": [{"id": "test_guide", "new_room": ""}]},
])
def test_invalid_spatial_entries_do_not_change_entities(update):
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    projected = session._project_state_tool_updates([update])
    parsed = session._merge_state_tool_results(session._explode_update_state(update))
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(baseline), parsed, inplace=True)
    for state in (projected, settled, session.current_state):
        assert state["npcs"] == baseline["npcs"]
        assert state["player"] == baseline["player"]


@pytest.mark.parametrize("exclusion", ["superseded", "deferred", "undone", "failed"])
def test_excluded_spatial_records_are_not_settled(exclusion):
    session = make_session()
    records = [{"name": "update_state", "args": {"room_changes": [{"id": "test_guide", "new_room": room}]},
                "result": "accepted"} for room in ("desk", "shelf")]
    context = {}
    if exclusion == "undone":
        context["_undone_record_indexes"] = [1]
    elif exclusion == "failed":
        records[1]["result"] = "错误: rejected synthetic change"
    else:
        records[1][exclusion] = True
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["npcs"]["test_guide"]["current_room"] == "desk"


@pytest.mark.parametrize("derived", [False, True])
def test_explicit_location_clears_only_derived_rooms_and_survives_refresh(derived):
    session = make_session()
    if not derived:
        session.current_state["npcs"]["test_guide"]["current_room"] = "desk"
    update = {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_annex"}]}
    projected = session._project_state_tool_updates([update])
    npc = projected["npcs"]["test_guide"]
    assert npc["current_location"] == "test_annex"
    assert "_derived_location" not in npc and "_derived_room" not in npc
    assert npc.get("current_room") == (None if derived else "desk")
    session.current_state = projected
    session._refresh_npc_locations()
    assert npc["current_location"] == "test_annex"


@pytest.mark.parametrize("target,room", [("test_annex", "desk"), ("test_hall", "shelf")])
def test_regeneration_uses_the_same_spatial_settlement(target, room):
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    update = {"npc_location_changes": [{"npc_id": "test_guide", "new_location": target}],
              "room_changes": [{"id": "test_guide", "new_room": room}], "end_time": "2030-01-01T09:01:00"}
    parsed = session._merge_state_tool_results(session._explode_update_state(update))
    projected = session._project_state_tool_updates([update])
    settled, _, _ = session._settle_regen_state(parsed, baseline, [])
    assert settled["npcs"] == projected["npcs"]
    assert baseline["npcs"]["test_guide"]["current_location"] == "test_hall"


@pytest.mark.asyncio
async def test_undo_revokes_spatially_unlocked_lore():
    script = copy.deepcopy(make_session().script)
    script["lorebook"] = [{"id": "test_notice", "enabled": False, "content": "Synthetic room completion notice."}]
    script["one_time_events"] = [{
        "id": "test_room_gate", "trigger_time": "2030-01-01T09:01:00",
        "condition": "npcs.test_guide.current_room == desk",
        "effects": [{"type": "activate_lore", "id": "test_notice"}],
    }]
    operations = [
        ("submit_plan", {"plan": "Check a candidate room assignment and then withdraw it."}),
        ("update_state", {"room_changes": [{"id": "test_guide", "new_room": "desk"}], "end_time": "2030-01-01T09:01:00"}),
        ("query_lorebook", {"entry_id": "test_notice"}),
        ("undo_my_last_action", {"reason": "Withdraw the candidate room assignment."}),
        ("query_lorebook", {"entry_id": "test_notice"}),
        ("check_inventory", {}),
    ]
    requests = []

    async def respond(messages, **kwargs):
        index = len(requests)
        requests.append(copy.deepcopy(messages))
        name, arguments = operations[index]
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-spatial-undo-{index}", "name": name, "arguments": copy.deepcopy(arguments),
        }]}

    session = GameSession(script, SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond)))
    session.current_state = ScriptLoader.create_initial_state(script)
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"], "action_text": "Inspect a candidate room assignment."}
    completed = 0
    async with aclosing(session._execute_unified_agent(context, {}, {"text": context["action_text"]}, streaming=True)) as pipeline:
        async for event in pipeline:
            if event.get("type") == "tool_call":
                completed += 1
                if completed == len(operations) - 1:
                    break
    assert completed == len(operations) - 1
    results = {message["tool_call_id"]: message["content"] for message in requests[-1] if message.get("role") == "tool"}
    before = json.loads(results["synthetic-spatial-undo-2"])
    after = json.loads(results["synthetic-spatial-undo-4"])
    assert [entry["id"] for entry in before["entries"]] == ["test_notice"]
    assert before["projection"]["provisional"] is True
    assert results["synthetic-spatial-undo-3"].startswith("已撤销上一个工具调用（update_state）")
    assert after["entries"] == []
    assert context.get("_accepted_state_updates", []) == []
    assert context["_undone_record_indexes"] == {1}
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("target,room", [("test_annex", "desk"), ("test_hall", "shelf")])
async def test_declared_spatial_steps_unlock_projected_lore_and_persist(tmp_path, monkeypatch, target, room):
    session = make_session()
    script = copy.deepcopy(session.script)
    manual = {"completion_event": "test_spatial_gate", "steps": [
        {"npc_location_changes": [{"npc_id": "test_guide", "new_location": target}]},
        {"room_changes": [{"id": "test_guide", "new_room": room}, {"id": "player", "new_room": room}]},
    ]}
    script["lorebook"] = [
        {"id": "test_manual", "content": json.dumps(manual)},
        {"id": "test_notice", "enabled": False, "content": "SYNTHETIC_SPATIAL_OUTCOME_BODY"},
    ]
    script["one_time_events"] = [{
        "id": "test_spatial_gate", "trigger_time": "2030-01-01T09:01:00",
        "condition": f"npcs.test_guide.current_location == {target} AND npcs.test_guide.current_room == {room} AND player.current_room == {room}",
        "rule_notes": "SYNTHETIC_SPATIAL_RULE_BODY",
        "effects": [{"type": "activate_lore", "id": "test_notice"}],
    }]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.save_id = "synthetic-spatial-save"
    narrative = (
        "You examine the practice layout and arrange the marked positions according to its instructions. "
        "The guide takes the indicated place, and you check the assigned room carefully. "
        "The exercise is now complete, and you leave the surrounding shelves undisturbed."
    )
    requests = []

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-spatial-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = max(completed, default=-1) + 1
        if index >= 2:
            declaration = json.loads(json.loads(completed[1])["entries"][0]["content"])
        if index >= 3:
            definition = json.loads(json.loads(completed[2])["entries"][0]["content"])["definition"]
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Read the practice layout and its completion rule before acting."}
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
            assert not session._evaluate_condition(definition["condition"])
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 7
            name, arguments = "finalize_turn", {"summary": "Completed the layout exercise."}
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
        None, script["start_time"], 0, ai_response="You are in the test hall.", state_snapshot=root_state,
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "spatial-projection.db")
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
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": "You are in the test hall."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Read test_manual and follow its declared layout exercise.",
                      "intent_hint": "explicit_action", "stream_id": "synthetic-spatial-stream",
                      "action_id": "synthetic-spatial-action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert session.current_state["npcs"]["test_guide"]["current_location"] == target
            assert session.current_state["npcs"]["test_guide"]["current_room"] == room
            if target == final["state"]["player"]["location"]:
                assert final["state"]["npcs"]["test_guide"]["current_location"] == target
                assert final["state"]["npcs"]["test_guide"]["current_room"] == room
            else:
                assert "current_location" not in final["state"]["npcs"]["test_guide"]
                assert "current_room" not in final["state"]["npcs"]["test_guide"]
            assert final["state"]["player"]["current_room"] == room
            assert session.current_state["fired_one_time_events"] == ["test_spatial_gate"]
            assert session._evaluate_condition(script["one_time_events"][0]["condition"])
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
            for marker in ("SYNTHETIC_SPATIAL_RULE_BODY", "SYNTHETIC_SPATIAL_OUTCOME_BODY"):
                assert marker in json.dumps(requests)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"])
            await restored.branch_to_node(root_id)
            assert not restored._evaluate_condition(script["one_time_events"][0]["condition"])
            await restored.branch_to_node(final["node_id"])
            assert restored._evaluate_condition(script["one_time_events"][0]["condition"])
            assert restored.current_state["fired_one_time_events"] == ["test_spatial_gate"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
