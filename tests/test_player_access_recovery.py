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
from test_player_location_access import make_event_session
from test_player_spatial_feedback import spatial_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("requirement", ["variable", "world"])
async def test_feedback_guides_declared_recovery_through_stream_and_persistence(tmp_path, monkeypatch, requirement):
    script = copy.deepcopy(make_event_session(2).script)
    location = script["locations"][1]
    location.pop("description")
    location.update({"access_blocked_reason": "SYNTHETIC_RECOVERY_BLOCKED_BODY", "recovery_entry": "test_manual"})
    manual = json.loads(script["lorebook"][0]["content"])
    manual["request"].pop("location_change")
    if requirement == "world":
        script["world_properties"] = [{"id": "test_access_mode", "value": "disabled"}]
        location.update({"access_condition": "world_properties.test_access_mode == enabled", "default_room": "reading_desk"})
        manual["request"] = {"world_property_changes": [{"id": "test_access_mode", "value": "enabled"}]}
    script["lorebook"][0]["content"] = json.dumps(manual)
    script["one_time_events"][0]["condition"] = "player.location == test_gate AND player.current_room == " + location["default_room"]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = [entry["id"] for entry in script["locations"]]
    session.save_id = "synthetic-player-recovery-save"
    requests = []
    narrative = ("The first attempt at entry fails, so you stay in your original seat and read the practice instructions. "
                 "After satisfying the posted requirement, you enter the practice area and check its notice. "
                 "You rest there without disturbing any equipment.")

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        prefix = "synthetic-recovery-step-"
        completed = {int(message["tool_call_id"].removeprefix(prefix)): message["content"]
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)}
        index = max(completed, default=-1) + 1
        if index >= 3:
            declaration = json.loads(json.loads(completed[2])["entries"][0]["content"])["definition"]
        if index >= 4:
            instructions = json.loads(json.loads(completed[3])["entries"][0]["content"])
        if index >= 5:
            event_rule = json.loads(json.loads(completed[4])["entries"][0]["content"])["definition"]
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Inspect the practice entrance and follow its declared access requirements."}
        elif index == 1:
            name, arguments = "update_state", {"location_change": "test_gate"}
        elif index == 2:
            feedback = spatial_feedback(completed[1])
            assert feedback["movement_status"] == "blocked" and feedback["location_id"] == "test_origin"
            assert feedback["reason"] == "SYNTHETIC_RECOVERY_BLOCKED_BODY"
            name, arguments = "consult_rules", feedback["rule_query"]
        elif index == 3:
            assert "access_condition" in declaration and "default_room" in declaration
            name, arguments = "query_lorebook", {"entry_id": declaration["recovery_entry"]}
        elif index == 4:
            name, arguments = "consult_rules", {"category": "one_time_events", "query": instructions["event_id"]}
        elif index == 5:
            assert "location_change" not in instructions["request"]
            name, arguments = "update_state", {**instructions["request"], "end_time": event_rule["trigger_time"]}
        elif index == 6:
            feedback = spatial_feedback(completed[5])
            assert feedback["movement_status"] == "ready" and "reason" not in feedback
            assert feedback["location_id"] == declaration["id"] and feedback["room_id"] == declaration["default_room"]
            assert session.current_state["player"]["location"] == "test_origin"
            assert not session.current_state.get("location_access_blocked")
            name, arguments = "query_lorebook", {"entry_id": event_rule["effects"][0]["id"]}
        elif index == 7:
            lore = json.loads(completed[6])
            assert [entry["id"] for entry in lore["entries"]] == [event_rule["effects"][0]["id"]]
            assert lore["projection"]["provisional"] is True
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert index == 8
            name, arguments = "finalize_turn", {"summary": "Satisfied the declared entry requirement after an initial refusal."}
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
    root_id = session.world_tree.add_node(None, script["start_time"], 0,
                                         ai_response="You are at the practice origin.", state_snapshot=root_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "player-recovery.db")
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
            action = {"type": "freeform", "text": "Try test_gate and follow the declared instructions if entry is blocked.",
                      "intent_hint": "explicit_action", "stream_id": "synthetic-player-recovery-stream",
                      "action_id": "synthetic-player-recovery-action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert final["state"]["player"]["location"] == "test_gate"
            assert final["state"]["player"]["current_room"] == location["default_room"]
            assert session.current_state["fired_one_time_events"] == ["test_entry_gate"]
            assert not session.current_state.get("location_access_blocked")
            assert len(requests) == 9
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
            for marker in ("SYNTHETIC_RECOVERY_BLOCKED_BODY", "SYNTHETIC_PLAYER_ACCESS_RULE_BODY",
                           "SYNTHETIC_PLAYER_ACCESS_OUTCOME_BODY", "玩家空间候选值"):
                assert marker in json.dumps(requests, ensure_ascii=False)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"], ensure_ascii=False)
            await restored.branch_to_node(root_id)
            assert restored.current_state["player"] == root_state["player"]
            assert not restored.current_state.get("fired_one_time_events")
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["player"] == final_state["player"]
            assert restored.current_state["fired_one_time_events"] == ["test_entry_gate"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
