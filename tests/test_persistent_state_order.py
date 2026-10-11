import copy
import json

import pytest

from test_persistent_state_declarations import make_session


@pytest.mark.parametrize("initially_active", [False, True])
@pytest.mark.parametrize("entry_form", ["id", "object"])
@pytest.mark.parametrize("operations", [
    ("deactivate", "activate"),
    ("activate", "deactivate"),
    ("activate", "deactivate", "activate"),
    ("deactivate", "activate", "deactivate"),
])
def test_merged_state_transitions_match_ordered_candidate_updates(initially_active, entry_form, operations):
    session, script = make_session()
    definition = script["persistent_states"][0]
    state_id = definition["id"]
    entry = state_id if entry_form == "id" else copy.deepcopy(definition)
    if initially_active:
        session.current_state["active_persistent_states"] = [state_id]
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    updates = [{f"{operation}_states": [entry if operation == "activate" else state_id]}
               for operation in operations]
    unchanged_updates = copy.deepcopy(updates)
    records = []
    for update in updates:
        result = session._handle_update_state(update, context)
        assert not result.startswith("错误:"), result
        records.append({"name": "update_state", "args": update, "result": result})
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(
        copy.deepcopy(original), json.loads(json.dumps(parsed)), inplace=True,
    )
    assert settled["active_persistent_states"] == projected["active_persistent_states"]
    assert (state_id in settled["active_persistent_states"]) is (operations[-1] == "activate")
    assert updates == unchanged_updates
    assert session.current_state == original


def test_interleaved_states_preserve_reactivation_order_and_activation_metadata():
    session, _ = make_session()
    session.current_state["active_persistent_states"] = ["test_knight_oath", "test_other"]
    original = copy.deepcopy(session.current_state)
    updates = [
        {"deactivate_states": ["test_knight_oath"]},
        {"activate_states": [{"id": "test_new", "name": "合成新状态", "description": "Synthetic new condition."}]},
        {"activate_states": ["test_knight_oath"]},
    ]
    calls = [call for update in updates for call in session._explode_update_state(update)]
    parsed = session._merge_state_tool_results(calls)
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(original), parsed, inplace=True)
    projected = session._project_state_tool_updates(updates)
    assert settled["active_persistent_states"] == projected["active_persistent_states"]
    assert settled["active_persistent_states"] == ["test_other", "test_new", "test_knight_oath"]
    assert settled["display_names"]["test_new"] == "合成新状态"
    assert settled["persistent_state_descriptions"]["test_new"] == "Synthetic new condition."
    assert session.current_state == original


@pytest.mark.parametrize("exclusion", ["superseded", "deferred", "undone", "failed"])
def test_excluded_records_cannot_override_the_last_accepted_state_operation(exclusion):
    session, _ = make_session()
    state_id = "test_knight_oath"
    records = [
        {"name": "update_state", "args": {"deactivate_states": [state_id]}, "result": "accepted"},
        {"name": "update_state", "args": {"activate_states": [state_id]}, "result": "accepted"},
        {"name": "update_state", "args": {"deactivate_states": [state_id]}, "result": "accepted"},
    ]
    context = {}
    if exclusion == "undone":
        context["_undone_record_indexes"] = [2]
    elif exclusion == "failed":
        records[-1]["result"] = "错误: rejected synthetic operation"
    else:
        records[-1][exclusion] = True
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["active_persistent_states"] == [state_id]


@pytest.mark.parametrize("merged", [False, True])
def test_single_update_keeps_activation_then_deactivation_semantics(merged):
    session, _ = make_session()
    update = {"activate_states": ["test_knight_oath"], "deactivate_states": ["test_knight_oath"]}
    parsed = session._merge_state_tool_results(session._explode_update_state(update)) if merged else update
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["active_persistent_states"] == []


def test_filtered_activation_does_not_reinstate_an_undeclared_environment_state():
    session, _ = make_session()
    session.current_state["active_persistent_states"] = ["test_night"]
    updates = [{"deactivate_states": ["test_night"]}, {"activate_states": ["test_night"]}]
    parsed = session._merge_state_tool_results([
        call for update in updates for call in session._explode_update_state(update)
    ])
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["active_persistent_states"] == []
