import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client, stored_state
from test_story_runtime_discovery import runtime_query, records
from test_timed_story_projection import END, FIRE, START, WINDOW, story_session


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("available,fail_save", [(True, False), (False, False), (True, True)])
async def test_candidate_story_discovery_drives_harness_and_survives_save_boundaries(
    tmp_path, monkeypatch, dynamic, available, fail_save,
):
    session = story_session(dynamic=dynamic, condition=WINDOW if available else f"game_time > {FIRE}",
                            node_condition=f"game_time >= {FIRE}")
    session.save_id = "synthetic-timed-story-save"
    requests = []
    narratives = {
        True: ("You finish waiting and note the new instruction on the practice screen. "
               "You leave the choice for a separate action, keeping the notebook ready while the room stays quiet."),
        False: ("You finish waiting but no new instruction appears on the practice screen. "
                "You leave the notebook closed and remain seated, checking the clock while the room stays quiet."),
    }

    async def respond(messages, **kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tool_names == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "story-intent", "name": "classify_intent",
                                                   "arguments": {"intent": "action"}}]}
        if tool_names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "story-notes", "name": "extract_notes",
                                                   "arguments": {"notes": []}}]}
        requests.append(copy.deepcopy(messages))
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("story-step-")]
        step = len(requests) - 1
        if step == 0:
            name, arguments = "submit_plan", {"plan": "Wait, discover the timed branch, and check a choice without submitting it."}
        elif step in (1, 3, 6, 8):
            name, arguments = "consult_rules", {"category": "story_nodes", "view": "runtime",
                                                "query": "test_waiting", "include_candidates": True}
        elif step in (2, 7):
            observed = json.loads(completed[-1])
            assert "entries" in observed, observed
            assert records(observed)[0]["status"] == "locked"
            name, arguments = "update_state", {"end_time": END}
        elif step in (4, 9):
            observed = json.loads(completed[-1])
            waiting, = records(observed)
            assert waiting["status"] == ("unlocked" if available else "locked")
            assert waiting["choices"][0]["reason"] == "node_not_active"
            assert observed["projection"]["scheduled_through"] == END
            name, arguments = "preview_choice_outcome", {
                "node_id": "test_active", "choice_id": "test_open", "include_candidates": True,
            }
        elif step == 5:
            assert json.loads(completed[-1])["available"] is available
            name, arguments = "undo_my_last_action", {"reason": "Recheck the branch before accepting the wait."}
        elif step == 10:
            outcome = json.loads(completed[-1])
            assert outcome["available"] is available
            assert outcome["projection"]["persisted"] is False
            assert outcome["projection"]["event_ids"] == (["test_bell"] if available else [])
            name, arguments = "write_narrative", {"text": narratives[outcome["available"]]}
        else:
            assert step == 11
            name, arguments = "finalize_turn", {"summary": "Completed the wait after discovering the timed branch."}
        assert session.current_state["game_time"] == START
        assert not session.current_state.get("fired_one_time_events")
        assert not session.current_state.get("lorebook_enabled_overrides")
        assert "test_waiting" not in session.current_state["story_tree_state"]["unlocked"]
        assert "test_active" not in session.current_state["story_tree_state"]["completed"]
        assert name in tool_names
        return {"content": "", "tool_calls": [{"id": f"story-step-{step}", "name": name, "arguments": arguments}]}

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
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        root_id = session.world_tree.active_node_id
        baseline = copy.deepcopy(session.current_state)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Wait ten minutes and inspect the practice screen.",
                  "action_id": "synthetic-timed-story-action", "expected_node_id": root_id}
        if fail_save:
            save_node = game_routes._save_node

            async def reject_save(current, result, action_id=None):
                assert current.current_state["game_time"] == END
                assert "test_waiting" in current.current_state["story_tree_state"]["unlocked"]
                assert result["narrative"] == narratives[available]
                raise RuntimeError("Synthetic timed story save interruption")

            monkeypatch.setattr(game_routes, "_save_node", reject_save)
            failure = await client.post(endpoint, json=action)
            error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
            assert error["discard_partial"] is True
            assert session.current_state == baseline and session.world_tree.active_node_id == root_id
            assert await stored_state(session) == baseline
            assert records(runtime_query(session, query="test_waiting"))[0]["status"] == "locked"
            assert ("test_gate" in session.script_variables.definitions) is (not dynamic)
            requests.clear()
            monkeypatch.setattr(game_routes, "_save_node", save_node)
        response = await client.post(endpoint, json=action)
        assert response.status_code == 200
        events = _stream_events(response.text)
        assert not [event for event in events if event["type"] == "error"]
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == narratives[available] and len(requests) == 12
        assert "SYNTHETIC_TIMED_STORY_PRIVATE_RULE" not in response.text
        assert "scheduled_through" not in response.text
        settled = copy.deepcopy(session.current_state)
        assert settled["game_time"] == END
        assert settled["fired_one_time_events"] == (["test_bell"] if available else [])
        assert ("test_waiting" in settled["story_tree_state"]["unlocked"]) is available
        assert settled.get("script_variables", {}).get("test_gate", 0) == (7 if available else 0)
        assert "test_active" not in settled["story_tree_state"]["completed"]
        persisted = await stored_state(session)
        node_context = persisted.pop("_node_context")
        assert node_context["agent_trace"] == session.world_tree.get_node(final["node_id"])["agent_trace"]
        assert persisted == settled
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls and session.current_state == settled
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        sessions.append(restored)
        assert restored.current_state == settled
        assert records(runtime_query(restored, query="test_active"))[0]["choices"][0]["available"] is available
        for node_id, expected in ((root_id, False), (final["node_id"], available)):
            await restored.branch_to_node(node_id)
            assert records(runtime_query(restored, query="test_active"))[0]["choices"][0]["available"] is expected
            lore = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_notice"}))
            assert bool(lore["entries"]) is expected
        assert restored.current_state["game_time"] == END
        choice = await client.post(f"/api/game/{session.save_id}/story-tree/choice?summary_only=true",
                                   json={"node_id": "test_active", "choice_id": "test_open"})
        assert choice.status_code == 200 and choice.json()["success"] is available
        assert (await stored_state(restored)).get("script_variables", {}).get("test_gate", 0) == (8 if available else 0)
        async with database.get_db() as connection:
            count = await (await connection.execute(
                "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,),
            )).fetchone()
            assert count[0] == 2
