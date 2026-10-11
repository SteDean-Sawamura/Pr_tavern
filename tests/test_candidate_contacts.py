import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _bounded_tool_content
from test_agent_state_compaction import compact, state_feedback
from test_scheduled_npc_overrides import make_session


CONTACT = {"npc_contact_changes": [{"npc_id": "test_guide", "in_contact": True}]}
LABEL = "NPC接触候选（未提交，非角色知情）: "


def contact_feedback(result):
    assert LABEL in result
    value, _ = json.JSONDecoder().raw_decode(result.split(LABEL, 1)[1])
    return value


def consult_contacts(session, context=None, **arguments):
    return json.loads(session._handle_consult_rules({"question": "test_guide 接触", **arguments}, context))


@pytest.mark.parametrize("position,reason", [
    ({}, ""), ({"known": False, "met": False}, ""),
    ({"current_location": "test_desk"}, "不在当前位置"),
    ({"current_room": "hall"}, "不在同一房间"),
    ({"alive": False}, "当前无法接触"),
])
def test_contact_feedback_and_queries_share_settlement_rules_without_mutation(position, reason):
    session = make_session()
    session.current_state["player"]["current_room"] = "desk"
    session.current_state["npcs"]["test_guide"].update(position)
    baseline = copy.deepcopy((session.current_state, session.script))
    context = {"old_time": session.current_state["game_time"], "new_time": "2030-01-01T09:30:00"}
    result = contact_feedback(session._handle_update_state(copy.deepcopy(CONTACT), context))
    assert result["through"] == context["old_time"]
    assert result["contacts"]["test_guide"] == {
        "requested_in_contact": True, "in_contact": not bool(reason), "block_reason": reason,
    }
    events, projected = session._project_interaction_state(context)
    assert events == []
    assert session._npc_contact_is_current("test_guide", projected) is not bool(reason)
    if not reason:
        assert projected["npcs"]["test_guide"]["known"] is True
        assert projected["npcs"]["test_guide"]["met"] is True
    query = consult_contacts(session, context, include_candidates=True)
    assert query["contacts"]["test_guide"] == {"in_contact": not bool(reason), "block_reason": reason}
    assert query["projection"]["provisional"] is True
    assert consult_contacts(session)["contacts"]["test_guide"]["in_contact"] is False
    assert (session.current_state, session.script) == baseline
    assert session._project_interaction_state(context) == (events, projected)


@pytest.mark.parametrize("end_time,expected,reason", [
    (None, True, ""), ("2030-01-01T09:04:00", True, ""),
    ("2030-01-01T09:10:00", False, "不在当前位置"),
    ("2030-01-01T09:30:00", True, ""),
])
def test_contacts_follow_accepted_time_events_and_schedule_expiry(end_time, expected, reason):
    session = make_session()
    context = {"old_time": session.current_state["game_time"], "new_time": "2030-01-01T09:30:00"}
    update = {**copy.deepcopy(CONTACT), **session.script["one_time_events"][0]["preparation"]}
    if end_time:
        update["end_time"] = end_time
    result = contact_feedback(session._handle_update_state(update, context))
    assert result["through"] == (end_time or context["old_time"])
    assert result["contacts"]["test_guide"]["in_contact"] is expected
    assert result["contacts"]["test_guide"]["block_reason"] == reason
    assert consult_contacts(session, context, include_candidates=True)["contacts"]["test_guide"]["in_contact"] is expected
    assert "npc_contacts" not in session.current_state


def test_contact_does_not_unlock_events_before_formal_contact_settlement():
    session = make_session()
    session.script["one_time_events"][0]["condition"] = "npcs.test_guide.met == true"
    session.current_state["npcs"]["test_guide"].update(known=False, met=False)
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {**CONTACT, "end_time": "2030-01-01T09:10:00"},
    ]}
    events, projected = session._project_interaction_state(context)
    assert events == []
    assert projected["npcs"]["test_guide"]["met"] is True
    assert session._npc_contact_is_current("test_guide", projected)
    assert session.current_state["npcs"]["test_guide"]["met"] is False


def test_rooms_are_initialized_before_contact_and_explicit_rooms_take_precedence():
    session = make_session()
    session._location_by_id["test_origin"]["default_room"] = "desk"
    session.script["npcs"][0]["schedule"][0]["activity"] = "在会议室练习"
    context = {"old_time": session.current_state["game_time"]}
    blocked = contact_feedback(session._handle_update_state(copy.deepcopy(CONTACT), context))
    assert blocked["contacts"]["test_guide"]["block_reason"] == "不在同一房间"
    ready = contact_feedback(session._handle_update_state({
        "room_changes": [{"id": "test_guide", "new_room": "desk"}],
    }, context))
    assert ready["contacts"]["test_guide"]["in_contact"] is True
    assert len(context["_accepted_state_updates"]) == 2


@pytest.mark.parametrize("location,reason", [("Origin", "不在当前位置"), ("", "位置待确认")])
def test_contact_keeps_exact_location_contract_instead_of_presence_aliases(location, reason):
    session = make_session()
    session.current_state["player"]["location"] = location
    session.current_state["companions"] = ["test_guide"]
    context = {"old_time": session.current_state["game_time"]}
    assert contact_feedback(session._handle_update_state(copy.deepcopy(CONTACT), context))["contacts"]["test_guide"]["block_reason"] == reason


@pytest.mark.parametrize("value", [None, {}, "bad", [None], [{}],
    [{"npc_id": [], "in_contact": True}], [{"npc_id": " ", "in_contact": True}],
    [{"npc_id": "test_guide", "in_contact": "true"}], [{"npc_id": "test_guide", "in_contact": 1}],
])
def test_malformed_contact_request_is_rejected_atomically(value):
    session = make_session()
    context = {"old_time": session.current_state["game_time"]}
    baseline = copy.deepcopy((session.current_state, context))
    result = session._handle_update_state({"npc_contact_changes": value, "end_time": "2030-01-01T09:10:00"}, context)
    assert result.startswith("错误:")
    assert (session.current_state, context) == baseline


def test_unknown_target_and_cumulative_release_report_actual_candidate():
    session = make_session()
    context = {"old_time": session.current_state["game_time"]}
    missing = session._handle_update_state({"npc_contact_changes": [{"npc_id": "test_missing", "in_contact": True}]}, context)
    assert contact_feedback(missing)["contacts"]["test_missing"]["block_reason"] == "角色不存在"
    session._handle_update_state(copy.deepcopy(CONTACT), context)
    result = session._handle_update_state({"npc_contact_changes": [{"npc_id": "test_guide", "in_contact": False}]}, context)
    value = contact_feedback(result)["contacts"]["test_guide"]
    assert value["requested_in_contact"] is False and value["in_contact"] is False
    assert session._project_interaction_state(context)[1]["npcs"]["test_guide"]["met"] is True


@pytest.mark.parametrize("change", [
    {"location_change": "test_desk"},
    {"state_changes": [{"target": "npcs.test_guide.alive", "op": "set", "value": False}]},
])
def test_existing_contacts_are_pruned_in_candidate_without_new_declaration(change):
    session = make_session()
    session._apply_npc_contact_changes(session.current_state, CONTACT["npc_contact_changes"])
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": session.current_state["game_time"]}
    result = contact_feedback(session._handle_update_state(change, context))
    assert result["contacts"]["test_guide"]["requested_in_contact"] is None
    assert result["contacts"]["test_guide"]["in_contact"] is False
    assert session.current_state == baseline


@pytest.mark.asyncio
async def test_undo_and_compaction_replace_contact_feedback():
    session = make_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"]}
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(baseline), "npc_reactions": [], "new_npcs": [], "choices": [],
        "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": None, "conditioned_event_keys": None,
    }
    result = session._handle_update_state(copy.deepcopy(CONTACT), context)
    summary = await compact(session, [("update_state", CONTACT, _bounded_tool_content(result, "update_state", 1000))])
    assert contact_feedback(state_feedback(summary))["contacts"]["test_guide"]["in_contact"] is True
    session._last_tool_result = {"name": "update_state", "args": CONTACT}
    result = await session._dispatch_unified_tool(context, "undo_my_last_action", {"reason": "Withdraw candidate contact."})
    summary = await compact(session, [("undo_my_last_action", {}, result)], summary)
    value = contact_feedback(state_feedback(summary))["contacts"]["test_guide"]
    assert value["requested_in_contact"] is None and value["in_contact"] is False
    assert session.current_state == baseline


def test_large_contact_feedback_is_explicitly_omitted_and_can_be_queried_by_runtime_id():
    session = make_session()
    changes = []
    for index in range(90):
        npc_id = f"test_extra_{index}"
        session.current_state["npcs"][npc_id] = {"name": f"Practice actor {index}", "current_location": "test_origin"}
        changes.append({"npc_id": npc_id, "in_contact": True})
    context = {"old_time": session.current_state["game_time"]}
    result = session._handle_update_state({"npc_contact_changes": changes}, context)
    feedback = contact_feedback(result)
    assert feedback["feedback_omitted"] is True and "contacts" not in feedback
    bounded = _bounded_tool_content(result + " synthetic tail" * 2000, "update_state", 1000)
    assert contact_feedback(bounded)["feedback_omitted"] is True
    query = json.loads(session._handle_consult_rules({"question": "test_extra_89 接触", "include_candidates": True}, context))
    assert query["contacts"]["test_extra_89"]["in_contact"] is True


@pytest.mark.asyncio
async def test_character_advice_reads_candidate_contact_but_does_not_create_knowledge():
    session = make_session()
    session.current_state["npcs"]["test_guide"].update(known=False, met=False)
    session.ai_provider = SimpleNamespace(generate=AsyncMock(return_value="Synthetic advice."))
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [copy.deepcopy(CONTACT)]}
    baseline = copy.deepcopy(session.current_state)
    result = json.loads(await session._handle_invoke_character({
        "npc_id": "test_guide", "situation": "Consider a possible greeting.", "include_candidates": True,
    }, context))
    assert result["context"]["in_contact"] is True
    assert result["knowledge_source"] == "committed_state"
    assert result["mode"] == "advisory" and result["projection"]["provisional"] is True
    assert session.current_state == baseline
