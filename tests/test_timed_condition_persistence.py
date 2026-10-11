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
from test_timed_condition_projection import END, FIRE, START, WINDOW, timed_session


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [False, True])
@pytest.mark.parametrize("available,fail_save", [(True, False), (False, False), (True, True)])
async def test_timed_rules_drive_harness_and_persist_without_candidate_leaks(
    tmp_path, monkeypatch, trigger, available, fail_save,
):
    session = timed_session(WINDOW if available else f"game_time > {FIRE}", trigger=trigger)
    session.save_id = "synthetic-clock-save"
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = ["test_room"]
    script = session.script
    requests = []
    narratives = {
        True: ("You finish waiting and follow the instruction displayed on the practice screen. "
               "You make a brief note, check the clock, and remain seated while the room stays quiet."),
        False: ("You finish waiting but no instruction appears on the practice screen. "
                "You leave the notebook closed, check the clock, and remain seated while the room stays quiet."),
    }

    async def respond(messages, **kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tool_names == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "clock-intent", "name": "classify_intent",
                                                   "arguments": {"intent": "action"}}]}
        if tool_names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "clock-notes", "name": "extract_notes",
                                                   "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("clock-step-")]
        step = len(completed)
        if step == 0:
            name, arguments = "submit_plan", {"plan": "Check the timed rule before completing the wait."}
        elif step in (1, 3, 5, 7):
            name, arguments = "query_lorebook", {"entry_id": "clock_rule"}
        elif step in (2, 6):
            assert json.loads(completed[step - 1])["entries"] == []
            name, arguments = "update_state", {"end_time": END}
        elif step == 4:
            assert bool(json.loads(completed[3])["entries"]) is available
            name, arguments = "undo_my_last_action", {"reason": "Recheck the rule before accepting the time advance."}
        elif step == 8:
            result = json.loads(completed[7])
            assert bool(result["entries"]) is available
            assert result["projection"]["through"] == END
            assert result["projection"]["event_ids"] == (["clock_notice"] if available or trigger else [])
            name, arguments = "write_narrative", {"text": narratives[bool(result["entries"])]}
        else:
            assert step == 9
            name, arguments = "finalize_turn", {"summary": "Completed the wait after checking the clock rule."}
        assert session.current_state["game_time"] == START
        assert not session.current_state.get("fired_one_time_events")
        assert not session.current_state.get("lorebook_enabled_overrides")
        assert name in tool_names
        return {"content": "", "tool_calls": [{"id": f"clock-step-{step}", "name": name, "arguments": arguments}]}

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
    root_id = session.world_tree.add_node(None, START, 0, ai_response="You sit in the test room.", state_snapshot=baseline)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "clock-condition.db")
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
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                                     (script["script_id"], script["name"], json.dumps(script)))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                                     (session.save_id, script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": baseline})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Wait ten minutes and check the practice screen.",
                      "action_id": "synthetic-clock-action", "expected_node_id": root_id}
            if fail_save:
                save_node = game_routes._save_node

                async def reject_save(current, result, action_id=None):
                    assert current.current_state["game_time"] == END
                    assert current.current_state["lorebook_enabled_overrides"]["clock_rule"] is True
                    assert result["narrative"] == narratives[available]
                    raise RuntimeError("Synthetic clock save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failure = await client.post(endpoint, json=action)
                error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
                assert error["discard_partial"] is True
                assert session.current_state == baseline and session.world_tree.active_node_id == root_id
                lore = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "clock_rule"}))
                assert lore["entries"] == []
                requests.clear()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json=action)
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] == "error"]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narratives[available] and len(requests) == 10
            assert "SYNTHETIC_CLOCK_RULE" not in response.text
            settled = copy.deepcopy(session.current_state)
            assert settled["game_time"] == END
            assert settled["fired_one_time_events"] == (["clock_notice"] if available or trigger else [])
            assert bool(settled.get("lorebook_enabled_overrides", {}).get("clock_rule")) is available
            calls = provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert provider.generate_with_tools.await_count == calls and session.current_state == settled
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == settled
            inspect_action = {"type": "system", "text": "Inspect the clock rule."}
            context = restored._prepare_turn(inspect_action)
            prompt = restored._build_unified_context(context, inspect_action)
            assert ("SYNTHETIC_CLOCK_RULE" in prompt) is available
            for node_id, expected in ((root_id, False), (final["node_id"], available)):
                await restored.branch_to_node(node_id)
                lore = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "clock_rule"}))
                assert bool(lore["entries"]) is expected
            assert restored.current_state["game_time"] == END
        async with database.get_db() as connection:
            count = await (await connection.execute(
                "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,),
            )).fetchone()
            assert count[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
