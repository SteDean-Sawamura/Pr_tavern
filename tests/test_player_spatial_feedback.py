import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _bounded_tool_content
from engine.tool_schemas import _unified_tools
from test_agent_state_compaction import compact, state_feedback
from test_player_location_access import make_session


def spatial_feedback(result):
    label = "玩家空间候选值（未提交，不含尚未结算的事件效果，非主角知情）: "
    assert label in result
    value, _ = json.JSONDecoder().raw_decode(result.split(label, 1)[1])
    return value


@pytest.mark.parametrize("level", [0, 2])
@pytest.mark.parametrize("destination,room", [("test_gate", "lobby"), ("test_origin", "seat")])
def test_candidate_feedback_reports_actual_position_without_committing(level, destination, room):
    session = make_session()
    session.current_state["script_variables"]["test_access_level"] = level
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    result = session._handle_update_state({"location_change": destination}, context)
    feedback = spatial_feedback(result)
    blocked = destination == "test_gate" and level < 2
    assert feedback["requested_location_id"] == destination
    assert feedback["movement_status"] == ("blocked" if blocked else "ready")
    assert feedback["location_id"] == ("test_origin" if blocked else destination)
    assert feedback["room_id"] == ("seat" if blocked else room)
    assert feedback["rule_query"] == {"category": "locations", "query": destination}
    assert feedback.get("reason") == (session.script["locations"][1]["access_blocked_reason"] if blocked else None)
    assert session.current_state == baseline


@pytest.mark.parametrize("source", ["variable", "attribute", "world", "status"])
def test_original_move_recomputes_when_requirements_change(source):
    conditions = {"variable": "test_access_level >= 2", "attribute": "player.test_entry_score >= 2",
                  "world": "world_properties.test_open == true", "status": "status.test_ready == active"}
    changes = {
        "variable": {"variable_changes": [{"id": "test_access_level", "op": "set", "value": 2}]},
        "attribute": {"state_changes": [{"target": "player.test_entry_score", "op": "set", "value": 2}]},
        "world": {"world_property_changes": [{"id": "test_open", "value": True}]},
        "status": {"activate_states": [{"id": "test_ready", "name": "Practice readiness"}]},
    }
    session = make_session(conditions[source])
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    assert spatial_feedback(session._handle_update_state({"location_change": "test_gate"}, context))["movement_status"] == "blocked"
    result = session._handle_update_state(changes[source], context)
    feedback = spatial_feedback(result)
    assert feedback["movement_status"] == "ready"
    assert feedback["location_id"] == "test_gate" and feedback["room_id"] == "lobby"
    assert "reason" not in feedback
    assert len(context["_accepted_state_updates"]) == 2
    assert session.current_state == baseline


@pytest.mark.parametrize("update", [
    {"room_changes": [{"id": "player", "new_room": "desk"}]},
    {"state_changes": [{"target": "player.current_room", "op": "set", "value": "desk"}]},
])
def test_room_only_feedback_does_not_reuse_old_blocked_status(update):
    session = make_session()
    session.current_state["location_access_blocked"] = {"location_id": "test_gate", "reason": "Old refusal."}
    baseline = copy.deepcopy(session.current_state)
    result = session._handle_update_state(update, {"old_time": baseline["game_time"]})
    feedback = spatial_feedback(result)
    assert feedback["movement_status"] == "none"
    assert feedback["requested_location_id"] is None
    assert feedback["location_id"] == "test_origin" and feedback["room_id"] == "desk"
    assert "reason" not in feedback and "rule_query" not in feedback
    assert session.current_state == baseline


def test_unrelated_updates_do_not_add_player_spatial_feedback():
    session = make_session()
    context = {"old_time": session.current_state["game_time"]}
    for update in ({"world_property_changes": [{"id": "test_open", "value": True}]},
                   {"room_changes": [{"id": "test_unknown_npc", "new_room": "desk"}]}):
        assert "玩家空间候选值" not in session._handle_update_state(update, context)


@pytest.mark.parametrize("updates,expected,status", [
    ([{"location_change": "test_gate"}, {"location_change": "test_unknown"}], "test_gate", "blocked"),
    ([{"location_change": "test_unknown"}], "test_unknown", "not_applied"),
    ([{"location_change": "test_gate"}, {"location_change": "test_stop"}], "test_stop", "ready"),
])
def test_feedback_uses_the_effective_merged_destination(updates, expected, status):
    session = make_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": updates}
    feedback = spatial_feedback(session._state_projection_feedback(context))
    assert feedback["requested_location_id"] == expected
    assert feedback["movement_status"] == status


@pytest.mark.asyncio
async def test_rejected_update_does_not_replace_feedback_during_compaction():
    session = make_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    context = {"old_time": session.current_state["game_time"]}
    records = []
    for arguments in ({"location_change": "test_gate"}, {"location_change": "test_unknown"}):
        records.append(("update_state", arguments, session._handle_update_state(arguments, context)))
    assert records[-1][2].startswith("错误:")
    feedback = spatial_feedback(state_feedback(await compact(session, records)))
    assert feedback["requested_location_id"] == "test_gate"
    assert feedback["movement_status"] == "blocked"
    assert len(context["_accepted_state_updates"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [0, 2])
async def test_undo_and_repeated_compaction_replace_previous_space_feedback(level):
    session = make_session()
    session.current_state["script_variables"]["test_access_level"] = level
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(baseline), "npc_reactions": [], "new_npcs": [], "choices": [],
        "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": None, "conditioned_event_keys": None,
    }
    arguments = {"location_change": "test_gate"}
    result = session._handle_update_state(arguments, context)
    summary = await compact(session, [("update_state", arguments, result)])
    assert spatial_feedback(state_feedback(summary))["requested_location_id"] == "test_gate"
    session._last_tool_result = {"name": "update_state", "args": arguments}
    undo = {"reason": "Withdraw the provisional move."}
    result = await session._dispatch_unified_tool(context, "undo_my_last_action", undo)
    summary = await compact(session, [("undo_my_last_action", undo, result)], summary)
    feedback = spatial_feedback(state_feedback(summary))
    assert feedback == {"requested_location_id": None, "location_id": "test_origin", "room_id": "seat", "movement_status": "none"}
    assert "已接受 0 次状态更新" in state_feedback(summary)
    assert session.current_state == baseline


@pytest.mark.asyncio
async def test_long_reason_is_explicitly_partial_and_can_be_read_from_declaration():
    session = make_session()
    reason = "Synthetic access requirement. " * 240
    session.script["locations"][1]["access_blocked_reason"] = reason
    context = {"old_time": session.current_state["game_time"]}
    result = session._handle_update_state({"location_change": "test_gate"}, context)
    feedback = spatial_feedback(_bounded_tool_content(result, "update_state", 4000))
    assert feedback["reason_truncated"] is True
    assert reason.startswith(feedback["reason"]) and len(feedback["reason"]) <= 500
    page = json.loads(await session._dispatch_unified_tool(context, "consult_rules", feedback["rule_query"]))
    entry, = page["entries"]
    chunks = []
    offset = 0
    while offset is not None:
        page = json.loads(await session._dispatch_unified_tool(context, "consult_rules", {
            "category": "locations", "rule_index": entry["rule_index"], "snapshot": entry["snapshot"],
            "result_path": ["definition", "access_blocked_reason"], "offset": offset,
        }))
        section = page["entries"][0]
        chunks.append(section["content"])
        offset = section["next_offset"]
    assert "".join(chunks) == reason


def test_location_query_covers_hidden_descriptionless_declarations_without_revealing():
    session = make_session()
    session.script["locations"][1].pop("description")
    session.current_state["visible_locations"] = ["test_origin"]
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    result = json.loads(session._handle_consult_rules({"category": "locations", "query": "test_gate"}, context))
    entry, = result["entries"]
    assert json.loads(entry["content"]) == {"definition": session.script["locations"][1]}
    assert session.current_state == baseline and context == {"old_time": baseline["game_time"]}
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert "locations" in schema["parameters"]["properties"]["category"]["enum"]
    for option in ({"include_candidates": True}, {"view": "runtime"}):
        result = json.loads(session._handle_consult_rules({"category": "locations", **option}, context))
        assert "error" in result


def test_location_declaration_snapshot_expires_after_rule_changes():
    session = make_session()
    result = json.loads(session._handle_consult_rules({"category": "locations", "query": "test_gate"}))
    entry, = result["entries"]
    session.script["locations"][1]["access_condition"] = "test_access_level >= 3"
    result = json.loads(session._handle_consult_rules({
        "category": "locations", "rule_index": entry["rule_index"], "snapshot": entry["snapshot"], "offset": 0,
    }))
    assert result["restart_required"] is True


@pytest.mark.asyncio
async def test_location_rule_compaction_retains_full_reread_coordinates():
    session = make_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    arguments = {"category": "locations", "query": "test_gate"}
    result = session._handle_consult_rules(arguments)
    entry, = json.loads(result)["entries"]
    summary = await compact(session, [("consult_rules", arguments, result)])
    assert "[剧本机制检索]" in summary
    assert '"category": "locations"' in summary and entry["snapshot"] in summary
    assert "从 offset=0 重读" in summary
    assert session.script["locations"][1]["access_blocked_reason"] not in summary
    reread = json.loads(session._handle_consult_rules({
        "category": "locations", "rule_index": entry["rule_index"], "snapshot": entry["snapshot"], "offset": 0,
    }))
    assert reread["entries"][0]["content"] == entry["content"]


def test_reveal_then_move_feedback_uses_the_pending_discovery():
    session = make_session()
    session.current_state["visible_locations"] = ["test_origin"]
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    assert session._handle_update_state({"location_change": "test_gate"}, context).startswith("错误:")
    session._handle_update_state({"reveal_locations": [{"id": "test_gate", "name": "Practice gate"}]}, context)
    feedback = spatial_feedback(session._handle_update_state({"location_change": "test_gate"}, context))
    assert feedback["movement_status"] == "blocked"
    assert session.current_state == baseline


@pytest.mark.parametrize("level", [0, 2])
def test_explicit_player_room_is_reported_after_movement_check(level):
    session = make_session()
    session.current_state["script_variables"]["test_access_level"] = level
    context = {"old_time": session.current_state["game_time"]}
    feedback = spatial_feedback(session._handle_update_state({
        "location_change": "test_gate", "room_changes": [{"id": "player", "new_room": "desk"}],
    }, context))
    assert feedback["movement_status"] == ("ready" if level else "blocked")
    assert feedback["room_id"] == "desk"


@pytest.mark.parametrize("limit", [200, 500, 1000, 2000])
def test_tight_tool_budget_never_leaves_partial_spatial_json(limit):
    session = make_session()
    session.script["locations"][1]["access_blocked_reason"] = "Synthetic long access requirement. " * 200
    context = {"old_time": session.current_state["game_time"]}
    result = session._handle_update_state({"location_change": "test_gate"}, context)
    bounded = _bounded_tool_content(result, "update_state", limit)
    feedback = spatial_feedback(bounded)
    assert len(bounded) <= limit
    if feedback.get("feedback_omitted"):
        assert "movement_status" not in feedback and "不能据此判断通行成功" in bounded
    else:
        assert feedback["movement_status"] == "blocked" and feedback["location_id"] == "test_origin"
        assert feedback["reason_truncated"] is True
        assert feedback["rule_query"] == {"category": "locations", "query": "test_gate"}
