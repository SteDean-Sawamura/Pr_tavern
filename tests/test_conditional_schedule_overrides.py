import copy
import json

import pytest

from engine.event_engine import EventResult
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_candidate_contacts import CONTACT, contact_feedback, consult_contacts
from test_scheduled_npc_overrides import expected_override, make_session


CONDITION = "test_ready >= 3"


@pytest.mark.parametrize("source", ["trigger", "result", "converted_event"])
@pytest.mark.parametrize("condition,location", [
    (CONDITION, "test_origin"), ("test_ready == 2", "test_desk"),
    ("test_ready >=", "test_origin"), ("", "test_desk"),
])
def test_override_execution_preserves_schedule_condition(source, condition, location):
    session = make_session(override_condition=condition)
    session.current_state["script_variables"]["test_ready"] = 2
    event = session.script["one_time_events"][0]
    params = {key: value for key, value in event["effects"][0].items() if key != "type"}
    action = {"action": "schedule_override", "params": params}
    original = copy.deepcopy((action, event))
    if source == "trigger":
        session.trigger_engine.execute_actions([action], session.current_state)
    elif source == "result":
        session._apply_event_result(EventResult(effects=[action]))
    else:
        converted = session.event_engine._convert_scheduled_effects(event)
        session._apply_event_result(EventResult(effects=converted["schedule_override"]))
    assert session.current_state["npc_schedule_overrides"] == {"test_guide": [expected_override(session)]}
    assert session._get_npc_location("test_guide") == location
    assert (action, event) == original


@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
def test_lifecycle_installation_gate_is_distinct_from_schedule_gate(unified, dynamic):
    source = make_session(override_condition=CONDITION)
    params = {key: value for key, value in source.script["one_time_events"][0]["effects"][0].items() if key != "type"}
    trigger = {"id": "test_conditional_shift", "event": "after_ai", "condition": "test_ready >= 2",
               "action": "schedule_override", "params": params}
    script = copy.deepcopy(source.script)
    script["triggers"] = [] if dynamic else [trigger]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    if dynamic:
        session._apply_trigger_updates({"add": [copy.deepcopy(trigger)]})
    if not unified:
        session.event_engine = None
    session._fire_lifecycle_event("after_ai")
    assert "npc_schedule_overrides" not in session.current_state
    session.current_state["script_variables"]["test_ready"] = 2
    session._fire_lifecycle_event("after_ai")
    installed = copy.deepcopy(session.current_state["npc_schedule_overrides"])
    assert installed == {"test_guide": [expected_override(source)]}
    for ready, location in [(2, "test_origin"), (3, "test_desk"), (2, "test_origin"), (3, "test_desk")]:
        session.current_state["script_variables"]["test_ready"] = ready
        assert session._get_npc_location("test_guide") == location
        assert session.current_state["npc_schedule_overrides"] == installed
    session.current_state["game_time"] = "2030-01-01T09:30:00"
    assert session._get_npc_location("test_guide") == "test_origin"


def test_candidate_installed_schedule_tracks_retained_updates_without_duplicate_entries():
    session = make_session(override_condition=CONDITION)
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    initial = {**copy.deepcopy(CONTACT), "end_time": "2030-01-01T09:10:00",
               "variable_changes": [{"id": "test_ready", "op": "set", "value": 2}]}
    feedback = contact_feedback(session._handle_update_state(initial, context))
    assert feedback["contacts"]["test_guide"]["in_contact"] is True
    for ready, contact in [(3, False), (2, True), (3, False)]:
        feedback = contact_feedback(session._handle_update_state({
            "variable_changes": [{"id": "test_ready", "op": "set", "value": ready}],
        }, context))
        assert feedback["contacts"]["test_guide"]["in_contact"] is contact
        events, candidate = session._project_interaction_state(context)
        assert [event["event_id"] for event in events] == ["test_shift"]
        assert candidate["npc_schedule_overrides"] == {"test_guide": [expected_override(session)]}
        assert consult_contacts(session, context, include_candidates=True)["contacts"]["test_guide"]["in_contact"] is contact
    context["_accepted_state_updates"].pop()
    assert consult_contacts(session, context, include_candidates=True)["contacts"]["test_guide"]["in_contact"] is True
    assert session.current_state == baseline


def test_conditional_override_preserves_exact_expression_for_rule_retrieval_and_snapshot_reads():
    condition = '(test_ready >= 3 AND world_properties.test_shift == "ready") OR player.location == test_desk'
    session = make_session(override_condition=condition)
    session.current_state["script_variables"]["test_ready"] = 2
    session._apply_event_def_effects(session.script["one_time_events"][0], "test_shift")
    assert session.current_state["npc_schedule_overrides"]["test_guide"][0]["condition"] == condition
    source = copy.deepcopy(session.current_state)
    candidate = copy.deepcopy(source)
    candidate["script_variables"]["test_ready"] = 3
    candidate.setdefault("world_properties", {})["test_shift"] = "ready"
    assert session._get_npc_location("test_guide", source) == "test_origin"
    assert session._get_npc_location("test_guide", candidate) == "test_desk"
    rules = json.loads(session._handle_consult_rules({"category": "one_time_events", "query": "test_shift"}))
    entry, = rules["entries"]
    definition = json.loads(entry["content"])["definition"]
    assert definition["effects"][0]["condition"] == condition
    assert session.current_state == source
