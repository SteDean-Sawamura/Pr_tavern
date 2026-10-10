import copy
import json

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.tool_schemas import _unified_tools


@pytest.fixture
def session():
    session = GameSession({
        "script_id": "synthetic-variable-updates", "settings": {},
        "variables": [
            {"id": "test_counter", "type": "number", "default": 4, "min": 0, "max": 9},
            {"id": "test_flag", "type": "bool", "default": False},
            {"id": "test_note", "type": "string", "default": "initial"},
        ],
    }, object())
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


@pytest.mark.parametrize("tool_name", ["update_state", "finalize_turn"])
def test_unified_tools_declare_variable_changes(tool_name):
    tool = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == tool_name)
    changes = tool["parameters"]["properties"]["variable_changes"]
    assert changes["type"] == "array"
    assert changes["items"]["required"] == ["id", "op"]
    assert changes["items"]["properties"]["op"]["enum"] == ["set", "inc", "dec", "toggle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_state", "finalize_turn"])
@pytest.mark.parametrize("change,expected", [
    ({"id": "test_counter", "op": "inc", "value": 0}, 4),
    ({"id": "test_counter", "op": "dec", "value": 0}, 4),
    ({"id": "test_counter", "op": "inc"}, 5),
    ({"id": "test_counter", "op": "dec"}, 3),
    ({"id": "test_counter", "op": "set", "value": 100}, 9),
    ({"id": "test_counter", "op": "dec", "value": 100}, 0),
    ({"id": "test_flag", "op": "toggle"}, True),
    ({"id": "test_flag", "op": "set", "value": True}, True),
    ({"id": "test_note", "op": "set", "value": "revised"}, "revised"),
])
async def test_variable_changes_project_and_settle_without_mutating_live_state(session, tool_name, change, expected):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"], "_finalize_narrative": "You remain in place."}
    update = {"variable_changes": [change]}
    original_update = copy.deepcopy(update)
    result = await session._dispatch_unified_tool(context, tool_name, update)
    assert not result.startswith("错误:"), result
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["script_variables"][change["id"]] == expected
    if tool_name == "update_state":
        assert "未提交" in result
        assert json.dumps({change["id"]: expected}, ensure_ascii=False) in result
    parsed = session._build_unified_parsed([
        {"name": tool_name, "args": update, "result": result},
    ], context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(original), parsed, inplace=True)
    assert settled["script_variables"] == projected["script_variables"]
    assert session.current_state == original
    assert update == original_update


@pytest.mark.parametrize("operation", ["inc", "dec"])
def test_script_variable_zero_delta_is_not_a_default_delta(session, operation):
    session.script_variables.apply_op(session.current_state, "test_counter", operation, 0)
    assert session.current_state["script_variables"]["test_counter"] == 4


@pytest.mark.asyncio
async def test_mixed_legacy_and_named_variable_updates_keep_order(session):
    context = {"old_time": session.current_state["game_time"]}
    records = []
    for update in [
        {"state_changes": [{"type": "variable", "id": "test_counter", "op": "inc", "value": 100}],
         "variable_changes": [{"id": "test_counter", "op": "dec", "value": 2}]},
        {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}]},
    ]:
        result = await session._dispatch_unified_tool(context, "update_state", update)
        assert not result.startswith("错误:"), result
        records.append({"name": "update_state", "args": update, "result": result})
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["script_variables"]["test_counter"] == 8
    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(session.current_state), parsed, inplace=True)
    assert settled["script_variables"] == projected["script_variables"]
    assert session.current_state["script_variables"]["test_counter"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_state", "finalize_turn"])
@pytest.mark.parametrize("invalid", [
    {"id": "test_missing", "op": "set", "value": 1},
    {"id": ["test_counter"], "op": "set", "value": 1},
    {"id": "test_counter", "op": "add", "value": 1},
    {"id": "test_counter", "op": "set"},
    {"id": "test_counter", "op": "set", "value": "7"},
    {"id": "test_counter", "op": "inc", "value": 1.5},
    {"id": "test_counter", "op": "inc", "value": True},
    {"id": "test_counter", "op": "inc", "value": None},
    {"id": "test_counter", "op": "toggle"},
    {"id": "test_flag", "op": "inc", "value": 1},
    {"id": "test_flag", "op": "set", "value": "false"},
    {"id": "test_note", "op": "set", "value": 1},
    {"id": "test_note", "op": "toggle"},
    {}, "invalid",
])
async def test_invalid_variable_change_rejects_entire_tool_before_any_projection(session, tool_name, invalid):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"], "_finalize_narrative": "You remain in place."}
    original_context = copy.deepcopy(context)
    update = {
        "end_time": "2030-01-01T09:01:00",
        "variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}, invalid],
        "lore_reveals": [{"entry_id": "test_unavailable", "evidence": "unverified"}],
    }
    result = await session._dispatch_unified_tool(context, tool_name, update)
    assert result.startswith("错误:"), result
    assert "variable_changes" in result
    assert context == original_context
    assert session.current_state == original
    parsed = session._build_unified_parsed([
        {"name": tool_name, "args": update, "result": result},
    ], context, {"skip_choices": True})
    assert not parsed["state_changes"]


@pytest.mark.parametrize("changes", [None, {}, "invalid"])
def test_variable_changes_requires_an_array(session, changes):
    result = session._handle_update_state({"variable_changes": changes})
    assert result.startswith("错误:"), result


@pytest.mark.asyncio
async def test_finalize_variable_changes_roll_back_when_narrative_requires_revision(session, monkeypatch):
    context = {"old_time": session.current_state["game_time"], "_finalize_narrative": "You remain in place."}
    await session._dispatch_unified_tool(context, "update_state", {
        "variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}],
    })
    accepted = copy.deepcopy(context["_accepted_state_updates"])
    feedback = json.dumps({"error": "Synthetic narrative revision required"})
    monkeypatch.setattr(session, "_conditional_event_revision_feedback", lambda *_: feedback)
    result = await session._dispatch_unified_tool(context, "finalize_turn", {
        "variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}],
    })
    assert result == feedback
    assert context["_accepted_state_updates"] == accepted
    assert session._project_state_tool_updates(accepted)["script_variables"]["test_counter"] == 5
    assert session.current_state["script_variables"]["test_counter"] == 4


@pytest.mark.asyncio
async def test_failed_generation_restores_applied_variable_changes(session):
    snapshot = game_routes._capture_generation_state(session)
    context = {"old_time": session.current_state["game_time"]}
    update = {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]}
    result = await session._dispatch_unified_tool(context, "update_state", update)
    parsed = session._build_unified_parsed([
        {"name": "update_state", "args": update, "result": result},
    ], context, {"skip_choices": True})
    session.current_state, _ = session._apply_common_parsed_changes(session.current_state, parsed, inplace=True)
    assert session.current_state["script_variables"]["test_counter"] == 6
    await game_routes._rollback_generation_state(session, snapshot)
    assert session.current_state == snapshot["state"]
    assert session.current_state["script_variables"]["test_counter"] == 4


def test_variable_validation_follows_branch_local_definitions(session):
    original = copy.deepcopy(session.current_state)
    update = {"variable_changes": [{"id": "test_dynamic", "op": "inc", "value": 1}]}
    assert session._handle_update_state(update).startswith("错误:")
    session._apply_variable_updates({"add": [{"id": "test_dynamic", "type": "number", "default": 0}]})
    assert not session._handle_update_state(update).startswith("错误:")
    session.current_state = original
    session._hydrate_branch_runtime(original)
    assert session._handle_update_state(update).startswith("错误:")


def test_legacy_variable_changes_cannot_bypass_validation(session):
    result = session._handle_update_state({"state_changes": [
        {"type": "variable", "id": "test_counter", "op": "inc", "value": "invalid"},
    ]})
    assert result.startswith("错误: state_changes[0]")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_state", "finalize_turn"])
async def test_incompatible_saved_variable_value_is_recoverable_without_partial_acceptance(session, tool_name):
    session.current_state["script_variables"]["test_counter"] = "invalid saved value"
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"], "_finalize_narrative": "You remain in place."}
    original_context = copy.deepcopy(context)
    update = {"variable_changes": [
        {"id": "test_flag", "op": "toggle"},
        {"id": "test_counter", "op": "inc", "value": 1},
    ]}
    result = await session._dispatch_unified_tool(context, tool_name, update)
    assert result.startswith("错误: variable_changes[1]"), result
    assert context == original_context
    assert session.current_state == original
    update["variable_changes"].insert(1, {"id": "test_counter", "op": "set", "value": 4})
    result = await session._dispatch_unified_tool(context, tool_name, update)
    assert not result.startswith("错误:"), result
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["script_variables"]["test_counter"] == 5
    assert projected["script_variables"]["test_flag"] is True
    assert session.current_state == original
