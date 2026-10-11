import copy

import pytest

from engine.game_session import GameSession


def make_session(ready=False, condition=None, scheduled=False):
    script = {
        "script_id": "synthetic-typed-conditions", "settings": {},
        "variables": [
            {"id": "test_ready", "type": "bool", "default": ready},
            {"id": "test_count", "type": "number", "default": 0},
            {"id": "test_text", "type": "string", "default": "true"},
        ],
        "triggers": [] if condition is None else [{
            "id": "test_guarded_hook", "event": "test_signal", "condition": condition,
            "action": "set_var", "params": {"var_id": "test_count", "op": "inc", "value": 1},
        }],
        "one_time_events": [] if not scheduled else [{
            "id": "test_signal", "trigger_time": "2030-01-01T09:01:00", "effects": [],
        }],
    }
    session = GameSession(script, None)
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "player": {},
        "world_properties": {"test_access": "open"},
    }
    session.script_variables.init_state(session.current_state)
    return session


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("operator", ["==", "!="])
@pytest.mark.parametrize("literal,target", [
    ("true", True), ("TRUE", True), ("false", False), ("False", False),
    ("yes", True), ("no", False), ("1", True), ("0", False),
    ("'true'", True), ('"false"', False),
])
def test_boolean_variables_use_typed_comparisons(ready, operator, literal, target):
    session = make_session(ready)
    expected = ready == target if operator == "==" else ready != target
    assert session._evaluate_condition(f"script_variables.test_ready {operator} {literal}") is expected


@pytest.mark.parametrize("path", ["node_completed.test_done", "milestone.test_done", "event_fired.test_done", "event.test_done.fired"])
@pytest.mark.parametrize("operator", ["==", "!="])
def test_boolean_status_predicates_reject_unknown_boolean_literals(path, operator):
    session = make_session()
    assert session._evaluate_condition(f"{path} {operator} unknown") is False


def test_boolean_normalization_does_not_change_string_comparisons():
    session = make_session()
    assert session._evaluate_condition('script_variables.test_text == "true"') is True
    assert session._evaluate_condition('script_variables.test_text == "TRUE"') is False
    assert session._evaluate_condition("script_variables.test_text == false") is False


@pytest.mark.parametrize("operator", ["==", "!="])
def test_boolean_variable_rejects_an_unknown_literal_in_both_evaluators(operator):
    session = make_session(False, f"test_ready {operator} unknown")
    assert session._evaluate_condition(f"script_variables.test_ready {operator} unknown") is False
    assert session.trigger_engine.fire("test_signal", session.current_state) == []


@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("dispatch", ["_fire_lifecycle_event", "_fire_event_dual"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("condition", [
    "script_variables.test_ready == true", "test_ready == true",
    "world_properties.test_access == open AND script_variables.test_ready == true",
])
def test_lifecycle_conditions_agree_across_execution_paths(unified, dispatch, ready, condition):
    session = make_session(ready, condition)
    if not unified:
        session.event_engine = None
    getattr(session, dispatch)("test_signal")
    assert session.current_state["script_variables"]["test_count"] == int(ready)


def test_candidate_conditions_use_supplied_state_without_committing():
    session = make_session(False, "world_properties.test_access == open AND test_ready == true")
    original = copy.deepcopy(session.current_state)
    candidate = session._project_state_tool_updates([{
        "variable_changes": [{"id": "test_ready", "op": "toggle"}],
    }])
    condition = "world_properties.test_access == open AND script_variables.test_ready == true"
    assert session._evaluate_condition(condition, candidate) is True
    assert session._evaluate_condition(condition) is False
    actions = session.trigger_engine.fire("test_signal", candidate, condition_eval=session._evaluate_condition)
    assert len(actions) == 1
    assert session.current_state == original


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("access", ["open", "closed"])
def test_timed_trigger_conditions_agree_between_candidate_and_settlement(ready, access):
    condition = "world_properties.test_access == open AND test_ready == true"
    session = make_session(not ready, condition, scheduled=True)
    session.current_state["world_properties"]["test_access"] = access
    original_state = session.current_state
    original_snapshot = copy.deepcopy(original_state)
    original_triggers = session.trigger_engine
    updates = [{
        "variable_changes": [{"id": "test_ready", "op": "toggle"}],
        "end_time": "2030-01-01T09:01:00",
    }]
    context = {"old_time": original_state["game_time"], "_accepted_state_updates": updates}
    projected_events, projected_state = session._projected_events(context, with_state=True)
    repeated_events, repeated_state = session._projected_events(context, with_state=True)
    expected_count = int(ready and access == "open")
    assert projected_state["script_variables"]["test_ready"] is ready
    assert projected_state["script_variables"]["test_count"] == expected_count
    assert repeated_events == projected_events
    assert repeated_state == projected_state
    assert session.current_state is original_state
    assert session.current_state == original_snapshot
    assert session.trigger_engine is original_triggers
    session.current_state = session._project_state_tool_updates(updates)
    settled_events = []
    session._settle_timed_events(context["old_time"], updates[0]["end_time"], settled_events)
    assert [event["event_id"] for event in projected_events] == ["test_signal"]
    assert [event["event_id"] for event in settled_events] == ["test_signal"]
    assert session.current_state["script_variables"]["test_count"] == expected_count
    assert session.current_state["script_variables"] == projected_state["script_variables"]


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("literal,target", [("true", True), ("false", False), ("'true'", True), ("no", False)])
def test_standalone_variable_trigger_preserves_boolean_types(ready, literal, target):
    session = make_session(ready, f"test_ready == {literal}")
    actions = session.trigger_engine.fire("test_signal", session.current_state)
    assert bool(actions) is (ready == target)
