import copy
import json
import re
from unittest.mock import Mock

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_event_narrative_consistency import DRAFT, END, claim_session


def prepared_session(*, cyclic=False, cooldown=False):
    script = copy.deepcopy(claim_session("dice").script)
    script.update(script_id="synthetic-staged-event-dice", name="Synthetic staged event dice")
    item = script["random_items"][0]
    item["duration_turns"] = 3
    script["one_time_events"][0]["condition"] = "test_ready >= 2"
    if cyclic:
        source = script["one_time_events"].pop(0)
        source["first_trigger"] = source.pop("trigger_time")
        source.update(frequency_value=5, frequency_unit="minute")
        script["cyclic_events"] = [source]
        script["one_time_events"][0].update(trigger_time="2030-01-01T09:20:00", condition="test_ready >= 4")
        item["ranges"][0]["state_changes"] = [{"target": "script_variables.test_ready", "op": "add", "value": 1}]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    if cooldown:
        session.current_state["random_item_state"] = {item["id"]: {"cooldown_remaining": 2}}
    context = session._prepare_turn({"type": "freeform", "text": "等待15分钟，观察练习钟"})
    context.update(new_time=END, requested_wait_minutes=None, _accepted_state_updates=[{"end_time": END}])
    assert "test_cancel_roll" in context["candidate_event_dice_state"]
    session._roll_or_sustain = Mock(side_effect=AssertionError("Read-only projection must not roll dice"))
    return session, context


@pytest.mark.parametrize("reader", ["narrative", "forecast", "story"])
def test_staged_cancellation_is_shared_by_read_projections_without_mutation(reader):
    session, context = prepared_session()
    state = session.current_state
    before = copy.deepcopy((state, context, session.script))
    for _ in range(2):
        if reader == "narrative":
            context["_narrative_timed_event_ids"] = ["test_shift"]
            raw_feedback = session._timed_event_revision_feedback(context, DRAFT)
            assert raw_feedback, "Staged cancellation must reject the claim before settlement"
            feedback = json.loads(raw_feedback)
            assert [event["id"] for event in feedback["excluded_events"]] == ["test_shift"]
            context.pop("_narrative_timed_event_ids")
        elif reader == "forecast":
            forecast_context = {**context, "_accepted_state_updates": []}
            result = json.loads(session._peek_upcoming_events_for_turn(forecast_context, {"hours_ahead": 1}))
            assert [event["id"] for event in result["events"]] == ["test_cancel"]
            assert [event["id"] for event in result["conditional_candidates"]] == ["test_shift"]
        else:
            candidate, metadata = session._story_query_candidate(context)
            assert candidate.current_state["script_variables"]["test_ready"] == 0
            assert metadata["event_ids"] == ["test_cancel"]
            assert metadata["staged_event_dice_ids"] == ["test_cancel_roll"]
            assert metadata["excludes_event_dice"] is False
            assert metadata["excludes_unstaged_event_dice"] is True
    assert session.current_state is state
    assert (state, context, session.script) == before
    session._roll_or_sustain.assert_not_called()


def test_initial_prompt_uses_staged_forecast_but_standalone_query_does_not():
    session, context = prepared_session()
    context["_accepted_state_updates"] = []
    before = copy.deepcopy((session.current_state, context))
    prompt = session._build_unified_context(context, {"type": "freeform", "text": "观察练习钟"})
    section = re.search(r"<upcoming_events>.*?\n(\{.*?\})\n</upcoming_events>", prompt, re.S)
    assert section is not None
    forecast = json.loads(section.group(1))
    assert [event["id"] for event in forecast["events"]] == ["test_cancel"]
    assert forecast["staged_event_dice_ids"] == ["test_cancel_roll"]
    assert forecast["excludes_event_dice"] is False and forecast["excludes_unstaged_event_dice"] is True
    standalone = json.loads(session._handle_peek_upcoming_events({"hours_ahead": 1}))
    assert [event["id"] for event in standalone["events"]] == ["test_cancel", "test_shift"]
    assert standalone["staged_event_dice_ids"] == [] and standalone["excludes_event_dice"] is True
    assert session.current_state == before[0]
    for key in ("candidate_event_dice_results", "candidate_event_dice_state", "_accepted_state_updates"):
        assert context[key] == before[1][key]
    session._roll_or_sustain.assert_not_called()


def test_projection_uses_detached_staged_duration_state_once():
    session, context = prepared_session()
    before = copy.deepcopy((session.current_state, context))
    events, projected = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == ["test_cancel"]
    staged = context["candidate_event_dice_state"]["test_cancel_roll"]
    assert projected["random_item_state"]["test_cancel_roll"] == staged
    assert projected["random_item_state"]["test_cancel_roll"] is not staged
    projected["random_item_state"]["test_cancel_roll"]["remaining_turns"] = 99
    assert (session.current_state, context) == before
    session._roll_or_sustain.assert_not_called()


def test_cooldown_is_projected_without_inventing_a_dice_effect():
    session, context = prepared_session(cooldown=True)
    assert "test_cancel_roll" not in context["candidate_event_dice_results"]
    before = copy.deepcopy((session.current_state, context))
    events, projected = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == ["test_cancel", "test_shift"]
    assert projected["random_item_state"]["test_cancel_roll"]["cooldown_remaining"] == 1
    assert projected["script_variables"]["test_ready"] == 2
    assert (session.current_state, context) == before
    session._roll_or_sustain.assert_not_called()


@pytest.mark.parametrize("mode", ["disabled", "unstaged", "cancelled"])
def test_unused_staged_results_cannot_mutate_projection_or_roll(mode):
    session, context = prepared_session()
    if mode == "disabled":
        session.current_state["dice_check_enabled"] = False
    elif mode == "unstaged":
        context["candidate_event_dice_state"] = {}
        context["candidate_event_dice_results"] = {}
    else:
        context["_accepted_state_updates"][0]["variable_changes"] = [{"id": "test_ready", "op": "set", "value": 0}]
    before = copy.deepcopy((session.current_state, context))
    candidate, metadata = session._story_query_candidate(context)
    assert metadata["event_ids"] == ([] if mode == "cancelled" else ["test_cancel", "test_shift"])
    assert metadata["staged_event_dice_ids"] == []
    assert metadata["excludes_event_dice"] is True
    assert candidate.current_state.get("random_item_state", {}) == session.current_state.get("random_item_state", {})
    assert (session.current_state, context) == before
    session._roll_or_sustain.assert_not_called()


@pytest.mark.parametrize("accepted_end", [False, True])
def test_cyclic_staged_result_is_not_reapplied_across_forecast_windows(accepted_end):
    session, context = prepared_session(cyclic=True)
    events, projected = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == ["test_cancel"] * 3
    assert projected["script_variables"]["test_ready"] == 3
    if not accepted_end:
        context["_accepted_state_updates"] = []
    before = copy.deepcopy((session.current_state, context))
    for _ in range(2):
        result = json.loads(session._peek_upcoming_events_for_turn(context, {"hours_ahead": 1}))
        assert all(event["id"] == "test_cancel" for event in result["events"])
        assert [event["id"] for event in result["conditional_candidates"]] == ["test_shift"]
    assert (session.current_state, context) == before
    session._roll_or_sustain.assert_not_called()
