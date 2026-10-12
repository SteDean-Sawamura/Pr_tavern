import copy
import json

import pytest

from engine.event_engine import EventResult, GameEvent
from test_choice_instructions import ACTION, PENDING, choice_instructions, make_session
from test_choice_outcome_simulation import apply_preview_changes, observed_state


@pytest.mark.parametrize("depth", [1, 2])
def test_signal_engine_returns_direct_instruction_and_numeric_effect(depth):
    session = make_session(cascade_depth=depth)
    result = session.event_engine.fire_event("test_signal_0", copy.deepcopy(session.current_state))
    assert result.inject_prompts == choice_instructions(depth)[2:3]
    assert result.effects == [{"action": "set_var", "params": {
        "var_id": "player.test_chain_reward", "op": "add", "value": 1,
    }}]
    assert result.game_events == (["test_signal_1"] if depth == 2 else [])


@pytest.mark.parametrize("depth", [1, 2])
def test_preview_simulates_cascade_queue_without_changing_live_session(depth):
    session = make_session(cascade_depth=depth)
    before = observed_state(session)
    preview = json.loads(session._preview_declared_choice({"node_id": "test_decision", "choice_id": "test_open"}, {}))
    assert preview["available"] is True and preview["projection"]["effects_applied"] is True
    projected = apply_preview_changes(before[0], preview["state_changes"])
    assert projected[PENDING] == choice_instructions(depth)
    assert projected["player"]["test_chain_reward"] == depth
    assert observed_state(session) == before
    assert preview["pending_effects"]["inject_prompts"] == choice_instructions(0)
    session._settle_story_choice("test_decision", "test_open")
    assert session.current_state == projected


@pytest.mark.parametrize("independent", [False, True])
def test_rejected_choice_does_not_execute_or_queue_cascade(independent):
    session = make_session(independent, cascade_depth=2)
    choices = (session.event_engine.events["test_decision"].choices if independent
               else session.story_tree_engine._nodes["test_decision"]["choices"])
    choices[0]["condition"] = "player.test_chain_reward >= 1"
    result = session._settle_story_choice("test_decision", "test_open")
    assert not result.newly_completed
    assert PENDING not in session.current_state
    assert session.current_state["player"]["test_chain_reward"] == 0
    assert session._prepare_turn(ACTION)["turn_instructions"] == []


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
def test_only_executed_cascade_instructions_are_queued_in_order(independent, allowed):
    session = make_session(independent, cascade_depth=2)
    session.event_engine.events["test_receiver_1"].condition = f"player.test_chain_reward >= {1 if allowed else 2}"
    result = session._settle_story_choice("test_decision", "test_open")
    expected = choice_instructions(2 if allowed else 1)
    assert result.inject_prompts == choice_instructions(0)
    assert session.current_state[PENDING] == expected
    assert session.current_state["player"]["test_chain_reward"] == (2 if allowed else 1)
    assert session._prepare_turn(ACTION)["turn_instructions"] == expected


@pytest.mark.parametrize("independent", [False, True])
def test_cascade_without_direct_choice_instructions_still_queues(independent):
    session = make_session(independent, cascade_depth=2)
    if independent:
        event = session.event_engine.events["test_decision"]
        effects, choices = event.effects, event.choices
    else:
        node = session.story_tree_engine._nodes["test_decision"]
        effects, choices = node["effects"], node["choices"]
    effects.pop("inject_prompt")
    choices[0]["effects"].pop("inject_prompt")
    result = session._settle_story_choice("test_decision", "test_open")
    assert result.inject_prompts == []
    assert session.current_state[PENDING] == choice_instructions(2)[2:]
    assert session._prepare_turn(ACTION)["turn_instructions"] == choice_instructions(2)[2:]


@pytest.mark.parametrize("independent", [False, True])
def test_sibling_signal_instructions_follow_existing_effect_execution_order(independent):
    session = make_session(independent, cascade_depth=2)
    session.event_engine.add_event(GameEvent(
        id="test_sibling_receiver", category="trigger", metadata={"event": ["test_signal_0"]},
        effects={"inject_prompt": "SYNTHETIC_SIBLING_INSTRUCTION"},
    ))
    session._settle_story_choice("test_decision", "test_open")
    instructions = choice_instructions(2)
    expected = instructions[:3] + ["SYNTHETIC_SIBLING_INSTRUCTION"] + instructions[3:]
    assert session.current_state[PENDING] == expected
    assert session._prepare_turn(ACTION)["turn_instructions"] == expected
    assert session.current_state["player"]["test_chain_reward"] == 2


def test_event_application_preserves_direct_result_and_scheduled_receipts():
    session = make_session(cascade_depth=2)
    receipt = {"event_id": "test_scheduled", "fire_time": "2030-01-01T09:05:00", "text": "SYNTHETIC_SCHEDULED"}
    result = EventResult(
        inject_prompts=[receipt["text"]], scheduled_instructions=[receipt], game_events=["test_signal_0"],
        effects=[{"action": "set_var", "params": {"var_id": "player.test_parent_reward", "op": "set", "value": 7}}],
    )
    before = copy.deepcopy(result)
    state_log = session._apply_event_result(result)
    assert result == before
    assert len(state_log) == 1 and state_log[0]["target"] == "player.test_parent_reward"
    assert session.current_state["player"]["test_parent_reward"] == 7
    assert session.current_state["player"]["test_chain_reward"] == 2
    assert PENDING not in session.current_state
