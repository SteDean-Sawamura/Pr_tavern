import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _tool_result_failed
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_event_narrative_consistency import DRAFT, END, SAFE, START, claim_context, claim_session, narrative_provider
from test_story_choice_settlement import choice_client, stored_state


def update_arguments():
    return {"end_time": END, "variable_changes": [{"id": "test_ready", "op": "inc", "value": 1}]}


def dice_feedback(result):
    return json.JSONDecoder().raw_decode(result.split("事件骰子准备: ", 1)[1])[0]


@pytest.mark.parametrize("cooldown", [False, True])
def test_settlement_update_prepares_dice_before_feedback_without_rejecting_accepted_state(cooldown):
    session = claim_session("unstaged_dice")
    context = claim_context(session)
    session.script["random_items"][0]["duration_turns"] = 3
    if cooldown:
        session.current_state["random_item_state"] = {"test_cancel_roll": {"cooldown_remaining": 2}}
    roll = session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    before = copy.deepcopy((session.current_state, session.script))
    arguments = update_arguments()
    result = session._dispatch_settlement_tool("update_state", arguments, context)
    assert not _tool_result_failed(result)
    feedback = dice_feedback(result)
    assert feedback["state_update_accepted"] is True and feedback["provisional"] is True
    prepared, = feedback["prepared_event_dice"]
    assert prepared["random_item_id"] == "test_cancel_roll"
    assert context["_accepted_state_updates"] == [arguments]
    if cooldown:
        assert prepared["result"] is None and prepared["state"]["cooldown_remaining"] == 1
    else:
        assert prepared["result"]["total"] == 1 and prepared["state"]["remaining_turns"] == 2
        assert "test_shift (" not in result
        assert '"excluded_events": [{"id": "test_shift"' in result
    cached = copy.deepcopy((context["candidate_event_dice_state"], context.get("candidate_event_dice_results", {})))
    repeated = session._dispatch_settlement_tool("update_state", {"end_time": END}, context)
    assert "prepared_event_dice" not in repeated
    assert session._project_state_tool_updates(context["_accepted_state_updates"])["script_variables"]["test_ready"] == 3
    _, projected = session._projected_events(context, with_state=True)
    projected["random_item_state"]["test_cancel_roll"]["remaining_turns"] = 99
    assert (context["candidate_event_dice_state"], context.get("candidate_event_dice_results", {})) == cached
    assert (session.current_state, session.script) == before
    assert roll.call_count == (0 if cooldown else 1)


@pytest.mark.parametrize("mode", ["invalid", "missing_time", "unmet", "disabled", "foreground"])
def test_dice_preparation_requires_an_accepted_settlement_time_and_enabled_event(mode):
    session = claim_session("unstaged_dice")
    context = claim_context(session)
    arguments = update_arguments()
    if mode == "invalid":
        arguments["end_time"] = "invalid-time"
    elif mode == "missing_time":
        arguments.pop("end_time")
    elif mode == "unmet":
        arguments.pop("variable_changes")
    elif mode == "disabled":
        session.current_state["dice_check_enabled"] = False
    session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    before = copy.deepcopy(session.current_state)
    result = (session._handle_update_state(arguments, context) if mode == "foreground"
              else session._dispatch_settlement_tool("update_state", arguments, context))
    assert "prepared_event_dice" not in result
    assert not context.get("candidate_event_dice_state")
    session.dice.roll_and_resolve.assert_not_called()
    if mode == "missing_time":
        result = session._dispatch_settlement_tool("update_state", {"end_time": END}, context)
        assert dice_feedback(result)["state_update_accepted"] is True
        session.dice.roll_and_resolve.assert_called_once()
    assert session.current_state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("randomness", [False, True])
@pytest.mark.parametrize("earlier_update", [False, True])
async def test_background_revision_keeps_event_acknowledgement_and_prior_accepted_updates(randomness, earlier_update):
    session = claim_session("unstaged_dice")
    if not randomness:
        session.script["random_items"] = []
    context = claim_context(session)
    context["conditional_event_candidates"] = [
        event for event in session.event_scheduler.check_events(session.current_state, START, END)
        if event["event_id"] == "test_cancel"
    ]
    before = copy.deepcopy(session.current_state)
    earlier = update_arguments()
    if earlier_update:
        session._handle_update_state(earlier, context)
    arguments = {"end_time": END} if earlier_update else update_arguments()
    operations = [
        ("update_state", arguments),
        ("write_narrative", {"text": SAFE, "event_ids": []}),
        ("update_state", arguments),
    ]
    responses = [{"content": "", "tool_calls": [{
        "id": f"synthetic-settlement-revision-{index}", "name": name, "arguments": copy.deepcopy(values),
    }]} for index, (name, values) in enumerate(operations)]
    responses.append({"content": "Done.", "tool_calls": []})
    session.ai_provider = narrative_provider(session, "unstaged_dice")
    session.ai_provider.generate_with_tools = AsyncMock(side_effect=responses)
    roll = session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    events = [event async for event in session._run_background_agent(
        context, DRAFT, "", {"text": "Observe the practice clock."},
    )]
    holder = next(event["holder"] for event in events if event["type"] == "_bg_result")
    assert holder.records[0]["superseded"] is True
    assert context["_accepted_state_updates"] == ([earlier] if earlier_update else []) + [arguments]
    assert context["_finalize_narrative"] == SAFE
    assert context["_narrative_conditional_events"] == context["_conditioned_event_keys"] == [
        ("test_cancel", "2030-01-01T09:02:00"),
    ]
    assert session._conditional_event_revision_feedback(context, SAFE) == ""
    assert session._project_state_tool_updates(context["_accepted_state_updates"])["script_variables"]["test_ready"] == 3
    assert session.current_state == before and roll.call_count == int(randomness)


def delegated_provider(session, *, outcome="repair", safe=False):
    provider = narrative_provider(session, "unstaged_dice")
    provider.observed = []
    provider.settlement_steps = []

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        if "classify_intent" in names:
            phase, step, name, arguments = "intent", 0, "classify_intent", {"intent": "action"}
        elif "extract_notes" in names:
            phase, step, name, arguments = "notes", 0, "extract_notes", {"notes": []}
        else:
            phase = "main" if "submit_plan" in names or "delegate_settlement" in names else "settlement"
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith(f"synthetic-delegated-{phase}-")]
            step = len(completed)
            if phase == "main":
                operations = [
                    ("submit_plan", {"plan": "Read the practice event and delegate its state settlement."}),
                    ("consult_rules", {"category": "one_time_events", "query": "test_shift"}),
                    ("write_narrative", {"text": SAFE + " You check the blank page once more." if safe else DRAFT,
                                         "event_ids": [] if safe else ["test_shift"]}),
                    ("delegate_settlement", {"summary": "A synthetic delegated observation."}),
                ]
                assert step < len(operations)
                name, arguments = operations[step]
            else:
                provider.settlement_steps.append(step)
                if step == 1:
                    provider.observed.append(completed[0])
                if step == 0 or step == 2 and outcome == "repair":
                    name, arguments = "update_state", update_arguments()
                elif outcome == "exhaust":
                    name, arguments = "consult_rules", {"category": "one_time_events", "query": "test_shift"}
                elif step == 1 and outcome == "repair" and "prepared_event_dice" in completed[0]:
                    name, arguments = "write_narrative", {"text": SAFE, "event_ids": []}
                else:
                    return {"content": "Done.", "tool_calls": []}
        assert name in names
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-delegated-{phase}-{step}", "name": name, "arguments": arguments,
        }]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.parametrize("safe", [False, True])
async def test_delegated_dice_repair_persists_once_and_survives_retry_replay_reload_and_regeneration(tmp_path, monkeypatch, fail_save, safe):
    session = claim_session("unstaged_dice")
    provider = session.ai_provider = delegated_provider(session, safe=safe)
    roll = session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-delegated-dice", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert result["narrative"] == SAFE
                raise RuntimeError("Synthetic delegated save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
            assert DRAFT not in failed.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root and roll.call_count == 1
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response)
        assert result["narrative"] == SAFE and DRAFT not in response.text
        assert any(event["type"] == "text" and event["content"] == SAFE for event in _stream_events(response.text))
        assert roll.call_count == 1 + int(fail_save)
        assert all(dice_feedback(feedback)["state_update_accepted"] for feedback in provider.observed)
        state = await stored_state(session)
        assert state["game_time"] == END and state["script_variables"]["test_ready"] == 0
        assert state["fired_one_time_events"] == ["test_cancel"]
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 15)
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["narrative"] == SAFE and regenerated.json()["total_swipes"] == 2
        assert roll.call_count == 2 + int(fail_save)
        assert (await stored_state(session))["script_variables"]["test_ready"] == 0
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["game_time"] == END
        await restored.branch_to_node(root)
        assert restored.current_state["game_time"] == START and restored.current_state["script_variables"]["test_ready"] == 2
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["script_variables"]["test_ready"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ignore", "exhaust"])
async def test_unrepaired_delegated_claim_is_not_streamed_or_saved(tmp_path, monkeypatch, outcome):
    session = claim_session("unstaged_dice")
    provider = session.ai_provider = delegated_provider(session, outcome=outcome)
    roll = session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-delegated-reject", "expected_node_id": root,
        }), 15)
        assert '"type": "error"' in response.text and '"type": "final"' not in response.text
        assert not [event for event in _stream_events(response.text) if event["type"] in ("text", "narrative_revised")]
        assert DRAFT not in response.text
        assert session.current_state == before and await stored_state(session) == baseline
        assert session.world_tree.active_node_id == root and roll.call_count == 1
        assert dice_feedback(provider.observed[0])["state_update_accepted"] is True
        assert len(provider.settlement_steps) == (7 if outcome == "exhaust" else 2)
