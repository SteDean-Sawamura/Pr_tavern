import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest

from test_actor_goal_settlement import result_from
from test_event_narrative_consistency import DRAFT, END, SAFE, claim_context, claim_session, narrative_provider, proposal
from test_story_choice_settlement import choice_client, stored_state
from test_wait_event_validation import FIRE


def unstaged_session():
    session = claim_session("unstaged_dice")
    context = claim_context(session)
    context.update(candidate_event_dice_state={}, candidate_event_dice_results={})
    session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    return session, context


def test_finalize_prepares_missing_dice_and_revises_without_reroll_or_state_mutation():
    session, context = unstaged_session()
    session.script["random_items"][0]["duration_turns"] = 3
    before = copy.deepcopy((session.current_state, session.script))
    arguments = {"end_time": END, "variable_changes": [{"id": "test_ready", "op": "inc", "value": 1}]}
    feedback = json.loads(session._handle_finalize_turn(context, arguments))
    assert feedback["error_code"] == "event_dice_prepared"
    assert feedback["excluded_events"] == [{"id": "test_shift", "time": FIRE}]
    prepared, = feedback["prepared_event_dice"]
    assert prepared["random_item_id"] == "test_cancel_roll" and prepared["result"]["total"] == 1
    assert prepared["state"]["remaining_turns"] == 2
    assert context["_accepted_state_updates"] == []
    cached = copy.deepcopy((context["candidate_event_dice_state"], context["candidate_event_dice_results"]))
    repeated = json.loads(session._handle_finalize_turn(context, arguments))
    assert repeated["excluded_events"] == feedback["excluded_events"]
    assert "prepared_event_dice" not in repeated
    context.update(_finalize_narrative=SAFE, _narrative_timed_event_ids=[])
    assert session._handle_finalize_turn(context, arguments).startswith("已同步完成")
    assert context["_accepted_state_updates"] == [arguments]
    events, projected = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == ["test_cancel"]
    assert projected["script_variables"]["test_ready"] == 0
    projected["random_item_state"]["test_cancel_roll"]["remaining_turns"] = 99
    assert (context["candidate_event_dice_state"], context["candidate_event_dice_results"]) == cached
    assert (session.current_state, session.script) == before
    session.dice.roll_and_resolve.assert_called_once()


@pytest.mark.parametrize("cooldown", [False, True])
def test_new_dice_require_confirmation_even_without_an_invalid_event_claim(cooldown):
    session, context = unstaged_session()
    context.update(_finalize_narrative=SAFE, _narrative_timed_event_ids=[])
    if cooldown:
        session.current_state["random_item_state"] = {"test_cancel_roll": {"cooldown_remaining": 2}}
    before = copy.deepcopy(session.current_state)
    feedback = json.loads(session._handle_finalize_turn(context, proposal("unstaged_dice")))
    assert feedback["error_code"] == "event_dice_prepared" and feedback["provisional"] is True
    prepared, = feedback["prepared_event_dice"]
    if cooldown:
        assert prepared["result"] is None and prepared["state"]["cooldown_remaining"] == 1
    else:
        assert prepared["result"]["total"] == 1
    assert session._handle_finalize_turn(context, proposal("unstaged_dice")).startswith("已同步完成")
    assert session.dice.roll_and_resolve.call_count == (0 if cooldown else 1)
    assert session.current_state == before


@pytest.mark.parametrize("mode", ["disabled", "unmet", "invalid_time"])
def test_finalize_does_not_prepare_dice_for_unaccepted_or_untriggered_events(mode):
    session, context = unstaged_session()
    context.update(_finalize_narrative=SAFE, _narrative_timed_event_ids=[])
    arguments = proposal("unstaged_dice")
    if mode == "disabled":
        session.current_state["dice_check_enabled"] = False
    elif mode == "unmet":
        arguments = {"end_time": END}
    else:
        arguments["end_time"] = "invalid-time"
    before = copy.deepcopy(session.current_state)
    result = session._handle_finalize_turn(context, arguments)
    expected_prefix = "错误:" if mode == "invalid_time" else "已同步完成"
    assert result.startswith(expected_prefix)
    assert context["candidate_event_dice_state"] == {} and context["candidate_event_dice_results"] == {}
    assert session.current_state == before
    session.dice.roll_and_resolve.assert_not_called()


def test_read_queries_never_prepare_missing_dice():
    session, context = unstaged_session()
    context["_accepted_state_updates"] = [proposal("unstaged_dice")]
    before = copy.deepcopy((session.current_state, context))
    session._projected_events(context)
    session._peek_upcoming_events_for_turn(context, {"hours_ahead": 1})
    session._story_query_candidate(context)
    assert (session.current_state, context) == before
    session.dice.roll_and_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_failed_finalize_invalidates_forecast_cache_and_settlement_reuses_the_roll(tmp_path, monkeypatch):
    session, _ = unstaged_session()
    provider = narrative_provider(session, "unstaged_dice")
    background = provider.generate_with_tools
    calls = [
        ("submit_plan", {"plan": "Inspect a synthetic conditional event before recording it."}),
        ("update_state", proposal("unstaged_dice")),
        ("peek_upcoming_events", {"hours_ahead": 1}),
        ("write_narrative", {"text": DRAFT, "event_ids": ["test_shift"]}),
        ("finalize_turn", {"end_time": END}),
        ("peek_upcoming_events", {"hours_ahead": 1}),
        ("write_narrative", {"text": SAFE, "event_ids": []}),
        ("finalize_turn", {"end_time": END}),
    ]

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        if "classify_intent" in names or "extract_notes" in names:
            return await background(messages, **kwargs)
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith("prepared-dice-")]
        step = len(completed)
        if step == 3:
            assert "test_shift" in json.loads(completed[2])["projection"]["event_ids"]
        if step == 5:
            assert json.loads(completed[4])["error_code"] == "event_dice_prepared"
        if step == 6:
            assert "test_shift" not in json.loads(completed[5])["projection"]["event_ids"]
        assert step < len(calls)
        name, arguments = calls[step]
        return {"content": "", "tool_calls": [{"id": f"prepared-dice-{step}", "name": name, "arguments": arguments}]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-late-dice",
            "expected_node_id": session.world_tree.active_node_id,
        }), 15)
        result = result_from(response)
        assert result["narrative"] == SAFE and DRAFT not in response.text
        state = await stored_state(session)
        assert state["script_variables"]["test_ready"] == 0
        assert state["fired_one_time_events"] == ["test_cancel"]
        session.dice.roll_and_resolve.assert_called_once()
