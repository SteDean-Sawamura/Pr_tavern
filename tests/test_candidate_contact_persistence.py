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
from test_agent_harness_execution import _stream_events
from test_candidate_contacts import CONTACT, LABEL, contact_feedback, consult_contacts
from test_scheduled_npc_overrides import make_session


@pytest.mark.asyncio
@pytest.mark.parametrize("depart,fail_save", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("schedule_mode", ["override", "conditional", "gated_override"])
async def test_contact_feedback_drives_narrative_and_survives_rollback_reload_branch_replay(
        tmp_path, monkeypatch, depart, fail_save, schedule_mode):
    gate = "test_ready >= 3" if schedule_mode == "gated_override" else None
    session = make_session(conditional_schedule=schedule_mode == "conditional", override_condition=gate)
    session.save_id = "synthetic-contact-save"
    session.current_state["npcs"]["test_guide"].update(known=False, met=False)
    script = session.script
    history_activity = "SYNTHETIC_CLOCK_ACTIVITY"
    for slot in script["npcs"][0]["schedule"]:
        if slot["location"] == "test_desk":
            slot["activity"] = history_activity
    for effect in script["one_time_events"][0]["effects"]:
        if effect["type"] == "schedule_override":
            effect["activity"] = history_activity
    expected_world_events = [{
        "type": "npc_schedule_change", "npc_id": "test_guide", "npc_name": "Practice guide",
        "time": "2030-01-01T09:05:00", "previous_location": "test_origin",
        "previous_activity": "Practice rest", "location": "test_desk", "activity": history_activity,
    }] if depart else []
    requests = []
    expected_contact = not depart
    narratives = {True: (
        "The practice guide remains beside the desk and responds to your greeting. "
        "You introduce yourself, exchange a few words about the practice area, and stay nearby. "
        "The quiet conversation continues while the display clock advances ten minutes."
    ), False: (
        "The practice guide leaves for the assigned desk before you can exchange a greeting. "
        "You stay at the original seat and watch the display clock advance ten minutes. "
        "With nobody beside you, you postpone the introduction rather than speak to an absent guide."
    )}
    narrative = narratives[expected_contact]
    timed = {"end_time": "2030-01-01T09:10:00", "variable_changes": [
        {"id": "test_ready", "op": "set", "value": 2 if depart else 0},
    ]}
    if gate:
        timed["variable_changes"][0]["value"] = 3 if depart else 2

    async def respond(messages, **kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tool_names == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-intent", "name": "classify_intent", "arguments": {"intent": "action"}}]}
        if tool_names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("synthetic-contact-")]
        index = len(completed)
        if index == 0:
            assert "<npc_movements>" not in json.dumps(messages, ensure_ascii=False)
            name, arguments = "submit_plan", {"plan": "Check the guide before attempting an introduction."}
        elif index == 1:
            name, arguments = "update_state", copy.deepcopy(CONTACT)
        elif index in (2, 4, 6, 9):
            if index == 2:
                assert contact_feedback(completed[1])["contacts"]["test_guide"]["in_contact"] is True
            elif index == 4:
                assert contact_feedback(completed[3])["contacts"]["test_guide"]["in_contact"] is expected_contact
            elif index == 6:
                assert contact_feedback(completed[5])["contacts"]["test_guide"]["in_contact"] is True
            else:
                assert json.loads(completed[8])["contacts"]["test_guide"]["in_contact"] is False
            name, arguments = "consult_rules", {"question": "test_guide 接触", "include_candidates": True}
        elif index in (3, 7):
            assert json.loads(completed[index - 1])["contacts"]["test_guide"]["in_contact"] is True
            name, arguments = "update_state", copy.deepcopy(timed)
        elif index == 5:
            assert json.loads(completed[4])["contacts"]["test_guide"]["in_contact"] is expected_contact
            name, arguments = "undo_my_last_action", {"reason": "Recheck the contact before accepting the wait."}
        elif index == 8:
            name, arguments = "consult_rules", {"question": "test_guide 接触"}
        elif index == 10:
            contact = json.loads(completed[9])["contacts"]["test_guide"]["in_contact"]
            assert contact is expected_contact
            name, arguments = "write_narrative", {"text": narratives[contact]}
        else:
            assert index == 11
            name, arguments = "finalize_turn", {"summary": "Checked contact and completed the practice wait."}
        assert "npc_contacts" not in session.current_state
        assert session.current_state["npcs"]["test_guide"]["met"] is False
        assert session.current_state.get("_pending_world_events", []) == []
        assert session.world_clock.snapshot() == initial_clock
        assert name in tool_names
        return {"content": "", "tool_calls": [{"id": f"synthetic-contact-{index}", "name": name, "arguments": arguments}]}

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
    baseline = copy.deepcopy(session.current_state)
    initial_autonomy = copy.deepcopy(session.npc_autonomy.snapshot())
    initial_clock = session.world_clock.snapshot()
    root_id = session.world_tree.add_node(None, script["start_time"], 0,
                                         ai_response="You are seated at the practice area.", state_snapshot=baseline)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "candidate-contact.db")
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
        await game_routes._save_node(session, {"node_id": root_id, "state": baseline, "narrative": "Practice opening."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Check whether the guide is available and wait ten minutes.",
                      "action_id": "synthetic-contact-action", "expected_node_id": root_id}
            if fail_save:
                save_node = game_routes._save_node
                save_failures = []

                async def reject_save(current, result, action_id=None):
                    assert action_id == action["action_id"]
                    assert current.current_state["game_time"] == timed["end_time"]
                    assert current._npc_contact_is_current("test_guide", current.current_state) is expected_contact
                    assert current.current_state["npcs"]["test_guide"]["met"] is expected_contact
                    assert current.world_tree.active_node_id != root_id
                    assert current.npc_autonomy.get_npc_status("test_guide")["location"] == current._get_npc_location("test_guide")
                    assert current.current_state.get("_pending_world_events", []) == expected_world_events
                    assert current.world_clock.last_tick_time == timed["end_time"]
                    save_failures.append(action_id)
                    raise RuntimeError("Synthetic contact save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failure = await client.post(endpoint, json=action)
                error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
                assert error["discard_partial"] is True
                assert save_failures == [action["action_id"]]
                assert session.current_state == baseline
                assert session.npc_autonomy.snapshot() == initial_autonomy
                assert session.world_clock.snapshot() == initial_clock
                assert session.world_tree.active_node_id == root_id
                requests.clear()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json=action)
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] == "error"]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative and len(requests) == 12
            assert session._npc_contact_is_current("test_guide", session.current_state) is expected_contact
            assert session.current_state["npcs"]["test_guide"]["met"] is expected_contact
            settled = copy.deepcopy(session.current_state)
            assert settled.get("_pending_world_events", []) == expected_world_events
            assert "_pending_world_events" not in final["state"]
            assert "npc_schedule_change" not in response.text
            assert "previous_activity" not in response.text
            assert history_activity not in json.dumps([event for event in events if event["type"] != "final"], ensure_ascii=False)
            autonomy = copy.deepcopy(session.npc_autonomy.snapshot())
            assert autonomy["npc_states"]["test_guide"]["location"] == session._get_npc_location("test_guide")
            assert settled["_npc_autonomy"] == autonomy
            if gate:
                assert settled["npc_schedule_overrides"]["test_guide"][0]["condition"] == gate
                assert "test_shift" in settled["fired_one_time_events"]
            calls = provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert provider.generate_with_tools.await_count == calls and session.current_state == settled
            assert session.npc_autonomy.snapshot() == autonomy
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == settled
            assert restored.npc_autonomy.snapshot() == autonomy
            assert restored.npc_autonomy.npc_states is not restored.current_state["_npc_autonomy"]["npc_states"]
            inspect_action = {"type": "system", "text": "Inspect the saved position."}
            context = restored._prepare_turn(inspect_action)
            assert context["npc_autonomy_events"] == []
            prompt = restored._build_unified_context(context, inspect_action)
            assert "<npc_movements>" not in prompt
            assert (history_activity in prompt) is depart
            if depart:
                assert "2030-01-01T09:05:00" in prompt
                assert "test_origin / Practice rest → test_desk" in prompt
            assert "<world_events>" not in restored._build_unified_context(context, inspect_action)
            assert consult_contacts(restored)["contacts"]["test_guide"]["in_contact"] is expected_contact
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            assert history_activity not in json.dumps(node["agent_trace"], ensure_ascii=False)
            for marker in (LABEL, "requested_in_contact", "accepted_updates_and_scheduled_events"):
                assert marker in json.dumps(requests, ensure_ascii=False)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"], ensure_ascii=False)
            await restored.branch_to_node(root_id)
            assert restored.current_state.get("_pending_world_events", []) == []
            assert restored.npc_autonomy.snapshot() == initial_autonomy
            assert consult_contacts(restored)["contacts"]["test_guide"]["in_contact"] is False
            assert restored.current_state["npcs"]["test_guide"]["met"] is False
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state.get("_pending_world_events", []) == expected_world_events
            assert restored.npc_autonomy.snapshot() == autonomy
            assert restored._prepare_turn(inspect_action)["npc_autonomy_events"] == []
            assert consult_contacts(restored)["contacts"]["test_guide"]["in_contact"] is expected_contact
            assert restored.current_state["npcs"]["test_guide"]["met"] is expected_contact
        async with database.get_db() as db:
            count = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert count[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
