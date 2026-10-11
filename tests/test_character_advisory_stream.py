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
from test_character_advisory import character_session, invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_save", [False, True])
async def test_character_advice_drives_stream_with_undo_restore_branch_and_replay(tmp_path, monkeypatch, fail_save):
    session = character_session()
    session.save_id = "synthetic-character-save"
    script = session.script
    requests = []
    actor_prompts = []
    narrative = ("You read the practice instructions and quietly prepare the equipment at your original seat. "
                 "After waiting for the practice shift to begin, you leave a written question for a later meeting. "
                 "You do not assume that anyone has answered or agreed to help yet.")
    updates = {"variable_changes": [{"id": "test_ready", "op": "set", "value": 2}],
               "end_time": "2030-01-01T09:05:00"}

    async def generate(messages, **kwargs):
        system = kwargs.get("system", "")
        if "## 已提交状态中的角色知识" in system:
            actor_context = json.loads(next(line for line in system.splitlines() if line.startswith('{"npc_id":')))
            actor_prompts.append(system)
            assert "SYNTHETIC_ACTOR_KNOWN" in system
            assert "SYNTHETIC_OTHER_KNOWN" not in system
            assert "SYNTHETIC_PENDING_EVENT_FACT" not in system
            assert session.current_state["game_time"] == script["start_time"]
            return ("SYNTHETIC_PRIVATE_ADVICE_AWAY" if actor_context["context"]["location"] == "test_desk"
                    else "SYNTHETIC_PRIVATE_ADVICE_HERE")
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })

    async def respond(messages, **kwargs):
        tools = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tools == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-intent", "name": "classify_intent", "arguments": {"intent": "action"}}]}
        if tools == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("synthetic-character-")]
        index = len(completed)
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Prepare for the practice shift and consider a later meeting."}
        elif index in (1, 6):
            if index == 6:
                result = json.loads(completed[5])
                assert result["response"] == "SYNTHETIC_PRIVATE_ADVICE_HERE"
                assert result["projection"]["event_ids"] == []
                assert result["game_time"] == script["start_time"]
            name, arguments = "update_state", copy.deepcopy(updates)
        elif index in (2, 3, 5, 7):
            if index == 3:
                result = json.loads(completed[2])
                assert result["response"] == "SYNTHETIC_PRIVATE_ADVICE_AWAY"
                assert result["projection"]["event_ids"] == ["test_shift"]
                assert "npc_schedule_overrides" not in session.current_state
            if index == 5:
                assert completed[4].startswith("已撤销上一个工具调用（update_state）")
            name, arguments = "invoke_character", {
                "npc_id": "test_guide", "situation": "Consider whether to answer a later written question.",
                "include_candidates": index != 3,
            }
        elif index == 4:
            result = json.loads(completed[3])
            assert result["response"] == "SYNTHETIC_PRIVATE_ADVICE_HERE" and "projection" not in result
            name, arguments = "undo_my_last_action", {"reason": "Reconsider the proposed wait."}
        elif index == 8:
            result = json.loads(completed[7])
            assert result["mode"] == "advisory" and result["projection"]["provisional"] is True
            text = (narrative if result["response"] == "SYNTHETIC_PRIVATE_ADVICE_AWAY" else
                    "You stay at your seat and consider how to arrange a meeting at another time.")
            name, arguments = "write_narrative", {"text": text}
        else:
            assert index == 9
            name, arguments = "finalize_turn", {"summary": "Prepared the equipment and left a question for a later meeting."}
        assert name in tools
        return {"content": "", "tool_calls": [{"id": f"synthetic-character-{index}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(side_effect=generate))
    session.ai_provider = provider
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, script["start_time"], 0,
                                         ai_response="You are at the practice origin.", state_snapshot=root_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "character-advice.db")
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
            action = {"type": "freeform", "text": "Prepare the equipment, wait for the practice shift and leave a question for later.",
                      "action_id": "synthetic-character-action", "expected_node_id": root_id}
            if fail_save:
                save_node = game_routes._save_node
                rejected_saves = []

                async def reject_save(current, result, action_id=None):
                    assert current._get_npc_location("test_guide") == "test_desk"
                    rejected_saves.append(action_id)
                    raise RuntimeError("Synthetic save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failed = await client.post(endpoint, json=action)
                assert [event for event in _stream_events(failed.text) if event["type"] == "error"]
                assert rejected_saves == [action["action_id"]]
                assert session.current_state == root_state
                assert session.world_tree.active_node_id == root_id
                requests.clear()
                actor_prompts.clear()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json=action)
            events = _stream_events(response.text)
            assert response.status_code == 200 and not [event for event in events if event["type"] == "error"]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative and len(requests) == 10 and len(actor_prompts) == 4
            assert session._get_npc_location("test_guide") == "test_desk"
            assert not session.current_state.get("npc_contacts")
            final_state = copy.deepcopy(session.current_state)
            call_counts = (provider.generate.await_count, provider.generate_with_tools.await_count)
            assert (await client.post(endpoint, json=action)).text == response.text
            assert call_counts == (provider.generate.await_count, provider.generate_with_tools.await_count)
            assert session.current_state == final_state
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == final_state
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            for marker in ("SYNTHETIC_PRIVATE_ADVICE", "SYNTHETIC_ACTOR_KNOWN", "SYNTHETIC_ACTIVE_GOAL", "accepted_updates_and_scheduled_events"):
                assert marker in json.dumps(requests + actor_prompts, ensure_ascii=False)
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"], ensure_ascii=False)
            restored.ai_provider = SimpleNamespace(generate=AsyncMock(return_value="Synthetic restored advice."))
            assert (await invoke(restored))["context"]["location"] == "test_desk"
            assert restored.current_state == final_state
            for node_id, location, game_time in ((root_id, "test_origin", script["start_time"]),
                                                  (final["node_id"], "test_desk", updates["end_time"])):
                await restored.branch_to_node(node_id)
                branch_state = copy.deepcopy(restored.current_state)
                advice = await invoke(restored)
                assert advice["context"]["location"] == location and advice["game_time"] == game_time
                assert restored.current_state == branch_state
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
            assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
