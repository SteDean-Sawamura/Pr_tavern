import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_actor_goal_settlement import result_from
from test_scheduled_npc_overrides import make_session
from test_story_choice_settlement import choice_client, stored_state


START = "2030-01-01T09:00:00"
FIRE = "2030-01-01T09:05:00"
END = "2030-01-01T10:00:00"


def wait_session(ready=0, *, threshold=2, visibility="public", condition=None, prerequisite=False):
    script = copy.deepcopy(make_session(threshold=threshold).script)
    script["script_id"] = "synthetic-wait-validation"
    script["name"] = "Synthetic wait validation"
    script["variables"][0]["default"] = ready
    event = script["one_time_events"][0]
    event["visibility"] = visibility
    if condition is not None:
        event["condition"] = condition
    if prerequisite:
        script["one_time_events"].append({
            "id": "test_unlock", "description": "A synthetic backstage signal.",
            "visibility": "hidden", "trigger_time": "2030-01-01T09:02:00",
        })
    session = GameSession(script, None, save_id="synthetic-wait-validation-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["visible_locations"] = [location["id"] for location in script["locations"]]
    return session


def wait_context(session):
    return {
        "old_time": START, "new_time": END, "estimated_minutes": 60, "requested_wait_minutes": 60,
        "action_text": "等待1小时，观察周围变化", "_accepted_state_updates": [],
        "triggered_events": session.event_scheduler.check_events(
            session.current_state, START, END, condition_eval=session._evaluate_condition,
        ),
    }


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("initial,target,visibility,allowed", [
    (0, 2, "public", True), (2, 0, "public", False),
    (0, 2, "hidden", False), (2, 2, "public", True),
])
def test_wait_interruptions_follow_accepted_and_pending_conditions(separate, initial, target, visibility, allowed):
    session = wait_session(initial, visibility=visibility)
    context = wait_context(session)
    baseline = copy.deepcopy(session.current_state)
    changes = {"variable_changes": [{"id": "test_ready", "op": "set", "value": target}]}
    if separate:
        assert not session._handle_update_state(changes, context).startswith("错误:")
    previous = copy.deepcopy(context)
    result = session._handle_update_state({"end_time": FIRE, **({} if separate else changes)}, context)
    assert (not result.startswith("错误:")) is allowed, result
    assert session.current_state == baseline
    if not allowed:
        assert context == previous
        assert "未发生中断事件" in result
    else:
        events, projected = session._projected_events(context, with_state=True)
        canonical = wait_session(initial, visibility=visibility)
        canonical.current_state = session._project_state_tool_updates(context["_accepted_state_updates"])
        settled = []
        canonical._settle_timed_events(START, FIRE, settled)
        canonical.current_state["game_time"] = FIRE
        assert events == settled and [event["event_id"] for event in events] == ["test_shift"]
        assert projected == canonical.current_state


@pytest.mark.parametrize("condition,prerequisite,allowed", [
    (f"game_time >= {FIRE}", False, True),
    ("event_fired.test_unlock == true", True, True),
    ("game_time >= 2030-01-01T09:06:00", False, False),
])
def test_wait_interruptions_use_event_time_and_earlier_event_effects(condition, prerequisite, allowed):
    session = wait_session(2, condition=condition, prerequisite=prerequisite)
    context = wait_context(session)
    before = copy.deepcopy(session.current_state)
    result = session._handle_update_state({"end_time": FIRE}, context)
    assert (not result.startswith("错误:")) is allowed, result
    assert session.current_state == before
    if allowed:
        events = session._projected_events(context)
        assert events[-1]["event_id"] == "test_shift"


def test_later_state_update_cannot_invalidate_an_accepted_wait_interruption():
    session = wait_session(2)
    context = wait_context(session)
    assert not session._handle_update_state({"end_time": FIRE}, context).startswith("错误:")
    previous = copy.deepcopy(context)
    changes = {"variable_changes": [{"id": "test_ready", "op": "set", "value": 0}]}
    rejected = session._handle_update_state(changes, context)
    assert rejected.startswith("错误:") and "未发生中断事件" in rejected
    assert context == previous
    accepted = session._handle_update_state({**changes, "end_time": END}, context)
    assert not accepted.startswith("错误:"), accepted
    events, state = session._projected_events(context, with_state=True)
    assert events == [] and state["game_time"] == END and state["script_variables"]["test_ready"] == 0


def test_stale_preview_of_already_fired_event_cannot_interrupt_wait_again():
    session = wait_session(2)
    session._settle_timed_events(START, FIRE, [])
    session.current_state["game_time"] = FIRE
    context = wait_context(session)
    context.update(old_time=FIRE, new_time="2030-01-01T10:05:00")
    context["triggered_events"] = [{"event_id": "test_shift", "fire_time": "2030-01-01T09:10:00"}]
    result = session._handle_update_state({"end_time": "2030-01-01T09:10:00"}, context)
    assert result.startswith("错误:") and context["_accepted_state_updates"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,threshold,fail_save", [(True, 3, False), (False, 8, False), (True, 8, True)])
async def test_wait_feedback_drives_agent_retry_undo_and_saved_time(tmp_path, monkeypatch, enabled, threshold, fail_save):
    session = wait_session(0 if enabled else threshold, threshold=threshold)
    calls = []
    narratives = {
        True: ("A practice shift begins. You stop waiting as the activity changes in the room, "
               "check the clock, and leave your materials on the table while considering your next action."),
        False: ("You remain seated and complete the full hour of waiting. You check the clock, "
                "put your materials away, and leave the surrounding equipment undisturbed while considering your next action."),
    }

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments, step = "classify_intent", {"intent": "action"}, "intent"
        elif names == ["extract_notes"]:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            calls.append(copy.deepcopy(messages))
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith("wait-check-")]
            step = len(completed)
            if step == 0:
                name, arguments = "submit_plan", {"plan": "Adjust the preparation and check whether an event interrupts the wait."}
            elif step == 1:
                name, arguments = "consult_rules", {"category": "one_time_events", "query": "test_shift"}
            elif step in (2, 3, 6):
                entry, = json.loads(completed[1])["entries"]
                declaration = json.loads(entry["content"])["definition"]
                changes = copy.deepcopy(declaration["preparation"])
                if not enabled:
                    changes["variable_changes"][0]["value"] = 0
                if step == 2:
                    arguments = changes
                elif step == 3:
                    arguments = {"end_time": declaration["trigger_time"]}
                else:
                    assert completed[5].startswith("已撤销上一个工具调用")
                    interrupted = not completed[3].startswith("错误:")
                    assert interrupted is enabled
                    arguments = {**changes, "end_time": declaration["trigger_time"] if interrupted else END}
                name = "update_state"
            elif step in (4, 7):
                if step == 4:
                    assert (not completed[3].startswith("错误:")) is enabled, completed[3]
                else:
                    assert not completed[6].startswith("错误:"), completed[6]
                name, arguments = "query_lorebook", {"entry_id": "test_notice"}
            elif step == 5:
                assert bool(json.loads(completed[4])["entries"]) is enabled
                name, arguments = "undo_my_last_action", {"reason": "Recheck the time before committing the wait."}
            elif step == 8:
                result = json.loads(completed[7])
                assert result["projection"]["through"] == (FIRE if enabled else END)
                name, arguments = "write_narrative", {"text": narratives[bool(result["entries"])]}
            else:
                assert step == 9
                name, arguments = "finalize_turn", {"summary": "Completed the wait according to the observed event feedback."}
            assert session.current_state["game_time"] == START
            assert not session.current_state.get("fired_one_time_events")
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"wait-check-{step}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value=json.dumps({
        "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
        "skill_check": {"needed": False}, "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
        "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        "pass": True, "violations": [],
    })))
    session.ai_provider = provider
    expected_time = FIRE if enabled else END
    expected_ready = threshold if enabled else 0
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "调整准备工作，然后等待1小时，观察周围变化",
                  "action_id": "synthetic-wait-check", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result):
                assert current.current_state["game_time"] == expected_time
                raise RuntimeError("Synthetic wait save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response)
        assert result["narrative"] == narratives[enabled]
        state = await stored_state(session)
        assert session.current_state["game_time"] == state["game_time"] == expected_time
        assert state["script_variables"]["test_ready"] == expected_ready
        assert ("test_shift" in state.get("fired_one_time_events", [])) is enabled
        count = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == count
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["game_time"] == expected_time
        assert restored.current_state["script_variables"]["test_ready"] == expected_ready
        await restored.branch_to_node(root)
        assert restored.current_state["game_time"] == START
        assert not restored.current_state.get("fired_one_time_events")
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["game_time"] == expected_time
        assert ("test_shift" in restored.current_state.get("fired_one_time_events", [])) is enabled
        node = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert "SYNTHETIC_SHIFT_RULE_BODY" in json.dumps(calls)
        assert "SYNTHETIC_SHIFT_RULE_BODY" not in response.text + json.dumps(node["agent_trace"])
