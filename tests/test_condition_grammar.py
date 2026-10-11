import copy
import json

import pytest

from engine.conditions import condition_error
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_declarative_conditions import make_session


MALFORMED_CONDITIONS = [
    "not a condition", "test_ready = true", "test_ready === true",
    "test_ready == true OR", "OR test_ready == false", "test_ready == true AND",
    "test_count == 0 OR test_count >=", "test_count == 0 OR test_count >= 0 >= 0",
    "test_ready == true || || test_count == 0", "test_count == 0 OR ()",
    "test_count == 0 OR (test_ready == true", "test_ready == true)",
    "test_count == 0 OR test_ready == 'true", "test_text == 'true' extra",
]

DISPATCH_CONDITIONS = [(None, False), (False, False), (0, False), ({}, False), ([], False),
                       ("", True), (" \t", True), ("test_ready == false", False),
                       ("test_ready == true OR", False)]


def script_with_condition(category, condition):
    return {
        "script_id": "synthetic-condition-grammar", "script_name": "Synthetic condition grammar",
        "start_time": "2030-01-01T09:00:00", "world_background": "An empty room.",
        "player_character": {"name": "Test actor"},
        category: [{"id": "test_rule", "event": "test_signal", "condition": condition}],
    }


@pytest.mark.parametrize("condition", MALFORMED_CONDITIONS)
def test_invalid_condition_cannot_open_a_rule_even_with_a_true_or_branch(condition):
    session = make_session(True)
    assert session._evaluate_condition(condition) is False


@pytest.mark.parametrize("condition", [None, 1, False, {}, []])
def test_non_string_conditions_are_rejected_without_raising(condition):
    session = make_session()
    assert session._evaluate_condition(condition) is False


def test_excessive_group_nesting_is_rejected_without_raising():
    condition = "(" * 1000 + "test_ready == true" + ")" * 1000
    assert condition_error(condition) == "Condition nesting is too deep"
    assert make_session(True)._evaluate_condition(condition) is False


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("condition", [
    "(test_count == 0 OR test_count == 2) AND test_ready == true",
    "test_count == 1 OR (test_ready == true AND test_count == 0)",
    "((test_ready == true))", "(test_ready == true && test_count == 0) || test_count > 1",
])
def test_grouped_conditions_preserve_precedence_and_boolean_values(ready, condition):
    session = make_session(ready, condition)
    assert session._evaluate_condition(condition) is ready
    assert bool(session.trigger_engine.fire("test_signal", session.current_state)) is ready


@pytest.mark.parametrize("text", ["A OR (B)", "A && B", "old room", "TRUE", ""])
def test_quoted_values_do_not_become_operators_or_groups(text):
    session = make_session(False)
    session.current_state["script_variables"]["test_text"] = text
    condition = f'test_text == "{text}" AND (test_ready == false OR test_count > 0)'
    assert session._evaluate_condition(condition) is True


@pytest.mark.parametrize("condition", ["", " \t", "test_count == 0 OR test_ready == true AND test_count != 0"])
def test_unconditional_and_ungrouped_precedence_remain_compatible(condition):
    assert make_session()._evaluate_condition(condition) is True


@pytest.mark.parametrize("category", ["triggers", "one_time_events", "cyclic_events"])
@pytest.mark.parametrize("condition", [*MALFORMED_CONDITIONS, None, 1])
def test_script_validation_identifies_condition_field_without_echoing_expression(category, condition):
    errors = ScriptLoader.validate(script_with_condition(category, condition))
    assert len(errors) == 1
    assert errors[0]["field"] == "condition"
    assert "test_rule" in errors[0]["target"]
    assert errors[0]["msg"]
    if condition:
        assert str(condition) not in errors[0]["msg"]


@pytest.mark.parametrize("category", ["triggers", "one_time_events", "cyclic_events"])
def test_rule_query_exposes_the_same_private_diagnostic_as_author_validation(category):
    condition = "test_ready == true OR"
    script = script_with_condition(category, condition)
    session = make_session()
    session.script[category] = copy.deepcopy(script[category])
    if category == "triggers":
        session.trigger_engine.triggers = copy.deepcopy(script[category])
    original = copy.deepcopy(session.current_state)
    response = json.loads(session._handle_consult_rules({"category": category}))
    payload = json.loads(response["entries"][0]["content"])
    error, = ScriptLoader.validate(script)
    assert payload["definition"] == script[category][0]
    assert payload["condition_error"] == error["msg"]
    assert session.current_state == original


@pytest.mark.parametrize("condition", MALFORMED_CONDITIONS)
@pytest.mark.parametrize("unified", [False, True])
def test_malformed_lifecycle_condition_does_not_execute_actions(condition, unified):
    session = make_session(True, condition)
    if not unified:
        session.event_engine = None
    session._fire_lifecycle_event("test_signal")
    assert session.current_state["script_variables"]["test_count"] == 0


@pytest.mark.parametrize("ready", [False, True])
def test_grouped_timed_rule_uses_candidate_state_without_mutating_committed_state(ready):
    session = make_session(not ready, "(test_count == 0 OR test_count == 2) AND test_ready == true", scheduled=True)
    original = copy.deepcopy(session.current_state)
    updates = [{"variable_changes": [{"id": "test_ready", "op": "toggle"}], "end_time": "2030-01-01T09:01:00"}]
    events, projected = session._projected_events({
        "old_time": original["game_time"], "_accepted_state_updates": updates,
    }, with_state=True)
    assert [event["event_id"] for event in events] == ["test_signal"]
    assert projected["script_variables"]["test_count"] == int(ready)
    assert session.current_state == original
    session.current_state = session._project_state_tool_updates(updates)
    settled = []
    session._settle_timed_events(original["game_time"], updates[0]["end_time"], settled)
    assert session.current_state["script_variables"] == projected["script_variables"]


def make_dispatch_session(category, condition):
    script = copy.deepcopy(make_session(True, "").script)
    script["start_time"] = "2030-01-01T09:00:00"
    if category == "triggers":
        script["triggers"][0]["condition"] = condition
    else:
        script["triggers"] = []
        script[category] = [{
            "id": "test_signal", "condition": condition, "effects": [],
            "trigger_time": "2030-01-01T09:01:00", "first_trigger": "2030-01-01T09:01:00",
            "frequency_value": 1, "frequency_unit": "hour",
        }]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    return session


@pytest.mark.parametrize("condition,allowed", DISPATCH_CONDITIONS)
@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("dispatch", ["_fire_lifecycle_event", "_fire_event_dual"])
def test_lifecycle_dispatch_does_not_treat_invalid_falsy_conditions_as_unconditional(condition, allowed, unified, dispatch):
    session = make_dispatch_session("triggers", condition)
    if not unified:
        session.event_engine = None
    getattr(session, dispatch)("test_signal")
    assert session.current_state["script_variables"]["test_count"] == int(allowed)


@pytest.mark.parametrize("condition,allowed", DISPATCH_CONDITIONS)
@pytest.mark.parametrize("category", ["one_time_events", "cyclic_events"])
def test_timed_dispatch_checks_falsy_conditions_in_projection_and_settlement(condition, allowed, category):
    session = make_dispatch_session(category, condition)
    original = copy.deepcopy(session.current_state)
    updates = [{"end_time": "2030-01-01T09:01:00"}]
    expected = ["test_signal"] if allowed else []
    scheduled = session.event_scheduler.check_events(
        session.current_state, original["game_time"], updates[0]["end_time"], session._evaluate_condition,
    )
    assert [event["event_id"] for event in scheduled] == expected
    projected_events, projected_state = session._projected_events({
        "old_time": original["game_time"], "_accepted_state_updates": updates,
    }, with_state=True)
    assert [event["event_id"] for event in projected_events] == expected
    assert session.current_state == original
    session.current_state = session._project_state_tool_updates(updates)
    settled = []
    session._settle_timed_events(original["game_time"], updates[0]["end_time"], settled)
    assert [event["event_id"] for event in settled] == expected
    assert session.current_state.get("fired_one_time_events", []) == projected_state.get("fired_one_time_events", [])


@pytest.mark.parametrize("condition,allowed", DISPATCH_CONDITIONS)
@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("dispatch", ["tick", "activate", "choice", "auto_choice", "trigger"])
def test_story_dispatch_checks_falsy_conditions_at_each_entry(condition, allowed, unified, dispatch):
    script = copy.deepcopy(make_session(True).script)
    node = {"id": "test_node", "type": "auto", "condition": condition,
            "effects": {"notify": "Synthetic effect"}}
    if dispatch in {"choice", "auto_choice"}:
        node.update(type="choice", condition="", auto_resolve=dispatch == "auto_choice", choices=[{
            "id": "test_choice", "condition": condition, "effects": {"notify": "Synthetic choice"},
        }])
    elif dispatch == "activate":
        node["activate_events"] = ["test_signal"]
    elif dispatch == "trigger":
        node.update(type="trigger", event="test_signal")
    script["story_tree"] = {"trees": [{"id": "test_tree", "nodes": [node]}]}
    session = GameSession(script, None)
    state = session.current_state = copy.deepcopy(make_session(True).current_state)
    engine = session.event_engine if unified else session.story_tree_engine
    evaluator = session._evaluate_condition
    if dispatch == "choice":
        state["story_tree_state"] = {"active": ["test_node"]}
        result = engine.make_choice(state, "test_node", "test_choice", condition_eval=evaluator)
    elif dispatch == "activate":
        engine.fire_event("test_signal", state, condition_eval=evaluator)
        if unified:
            assert (state["events"].get("test_node", {}).get("status") == "unlocked") is allowed
        else:
            assert ("test_node" in state["story_tree_state"]["unlocked"]) is allowed
        return
    elif unified and dispatch == "trigger":
        result = engine.fire_event("test_signal", state, condition_eval=evaluator)
    elif unified:
        result = engine.tick(state, 1, condition_eval=evaluator)
    else:
        result = engine.evaluate(state, 1, condition_eval=evaluator, active_events=["test_signal"])
    if dispatch == "trigger":
        assert bool(result.notifications) is allowed
    else:
        assert bool(result.newly_completed) is allowed


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("default_fields", [{}, {"condition": " \t"}])
def test_auto_choice_prefers_matching_rule_then_real_unconditional_default(ready, unified, default_fields):
    original = make_session(ready)
    node = {"id": "test_node", "type": "choice", "choices": [
        {"id": "test_default", **default_fields},
        {"id": "test_match", "condition": "test_ready == true"},
        {"id": "test_blocked", "condition": "test_count > 100"},
    ]}
    script = copy.deepcopy(original.script)
    script["story_tree"] = {"trees": [{"id": "test_tree", "nodes": [node]}]}
    session = GameSession(script, None)
    session.current_state = copy.deepcopy(original.current_state)
    if unified:
        choice = session.event_engine._auto_resolve_choice(session.event_engine.events["test_node"], session._evaluate_condition)
    else:
        choice = session.story_tree_engine._auto_resolve_choice(node, session._evaluate_condition)
    assert choice["id"] == ("test_match" if ready else "test_default")
