import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _bound_tool_messages
from test_agent_state_compaction import compact
from test_scheduled_npc_overrides import make_session


QUESTION = "Practice guide 在场吗？有没有 test_badge？"


def consult(session, context=None, **arguments):
    return json.loads(session._handle_consult_rules({"question": QUESTION, **arguments}, context))


@pytest.mark.parametrize("visitor_room,npc_room,expected", [
    ("desk", "desk", "在场"), ("desk", "hall", "附近"), ("", "", "在场"),
])
def test_committed_presence_uses_existing_room_semantics_without_mutation(visitor_room, npc_room, expected):
    session = make_session()
    session.script["npcs"][0]["name"] = "practice guide"
    session.current_state["player"]["current_room"] = visitor_room
    session.current_state["npcs"]["test_guide"]["current_room"] = npc_room
    original = copy.deepcopy(session.current_state)
    result = consult(session)
    assert result["answers"][0].startswith(f"practice guide {expected}")
    assert "projection" not in result
    assert result["game_time"] == original["game_time"]
    assert session.current_state == original


def test_missing_locations_do_not_count_as_presence():
    session = make_session()
    session.script["npcs"][0].update(name="practice guide", default_location="", schedule=[])
    session.current_state["npcs"]["test_guide"]["default_location"] = ""
    session.current_state["player"]["location"] = ""
    assert consult(session)["answers"][0].startswith("practice guide 不在场")


@pytest.mark.parametrize("player_location,npc_state,expected", [
    ("Origin", {}, "在场"), ("test_origin", {"alive": False}, "不在场"),
    ("test_origin", {"current_location": "test_origin"}, "在场"),
])
def test_presence_respects_location_alias_life_state_and_explicit_position(player_location, npc_state, expected):
    session = make_session()
    session.current_state["player"]["location"] = player_location
    session.current_state["npcs"]["test_guide"].update(npc_state)
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": []}
    if npc_state.get("current_location"):
        context["_accepted_state_updates"].append({
            **session.script["one_time_events"][0]["preparation"], "end_time": "2030-01-01T09:10:00",
        })
    assert consult(session, context, include_candidates=True)["answers"][0].startswith(f"Practice guide {expected}")


@pytest.mark.parametrize("end_time,ready,expected,event_ids", [
    (None, 2, "在场", []),
    ("2030-01-01T09:04:00", 2, "在场", []),
    ("2030-01-01T09:10:00", 0, "在场", []),
    ("2030-01-01T09:10:00", 2, "不在场", ["test_shift"]),
    ("2030-01-01T09:30:00", 2, "在场", ["test_shift"]),
])
def test_candidate_presence_projects_declared_schedule_and_expiry(end_time, ready, expected, event_ids):
    session = make_session()
    update = {"variable_changes": [{"id": "test_ready", "op": "set", "value": ready}]}
    if end_time:
        update["end_time"] = end_time
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [update],
               "new_time": "2030-01-01T09:30:00"}
    original = copy.deepcopy((session.current_state, session.script, context))
    engines = session.script_variables, session.trigger_engine
    result = consult(session, context, include_candidates=True)
    assert result["answers"][0].startswith(f"Practice guide {expected}")
    assert result["projection"]["event_ids"] == event_ids
    assert result["projection"]["provisional"] is True
    assert result["projection"]["through"] == (end_time or context["old_time"])
    assert result["projection"]["scope"] == "accepted_updates_and_scheduled_events"
    assert consult(session, context)["answers"][0].startswith("Practice guide 在场")
    assert consult(session, context, include_candidates=True) == result
    assert (session.current_state, session.script, context) == original
    assert (session.script_variables, session.trigger_engine) == engines
    context["_accepted_state_updates"].clear()
    assert consult(session, context, include_candidates=True)["answers"][0].startswith("Practice guide 在场")


def test_candidate_movement_rooms_inventory_and_query_time_room_initialization():
    session = make_session()
    session._location_by_id["test_origin"]["default_room"] = "desk"
    session.current_state["npcs"]["test_guide"]["current_room"] = "hall"
    original = copy.deepcopy(session.current_state)
    assert consult(session)["answers"][0].startswith("Practice guide 附近")
    assert session.current_state == original
    context = {"old_time": original["game_time"], "_accepted_state_updates": [{
        "location_change": "test_desk",
        "npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_desk"}],
        "room_changes": [{"id": "player", "new_room": "hall"}],
        "inventory_changes": [{"action": "add", "item": "test_badge", "quantity": 1}],
    }]}
    result = consult(session, context, include_candidates=True)
    assert result["answers"] == ["Practice guide 在场（当前位置：test_desk）", "持有 test_badge"]
    context["_accepted_state_updates"].append({
        "room_changes": [{"id": "player", "new_room": "desk"}],
        "inventory_changes": [{"action": "remove", "item": "test_badge", "quantity": 1}],
    })
    result = consult(session, context, include_candidates=True)
    assert result["answers"][0].startswith("Practice guide 附近")
    assert "持有 test_badge" not in result["answers"]
    assert session.current_state == original


@pytest.mark.parametrize("arguments,context", [
    ({"include_candidates": True}, None), ({"include_candidates": True}, {}),
    ({"include_candidates": "true"}, {"old_time": "2030-01-01T09:00:00"}),
    ({"include_candidates": 1}, {"old_time": "2030-01-01T09:00:00"}),
    ({"include_candidates": True, "snapshot": "stale"}, {"old_time": "2030-01-01T09:00:00"}),
    ({"include_candidates": True, "category": "one_time_events"}, {"old_time": "2030-01-01T09:00:00"}),
])
def test_presence_rejects_unsupported_query_modes(arguments, context):
    session = make_session()
    original = copy.deepcopy(session.current_state)
    assert "error" in consult(session, context, **arguments)
    assert session.current_state == original


@pytest.mark.asyncio
async def test_presence_context_compaction_requires_fresh_query_and_keeps_candidate_boundary():
    session = make_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    context = {"old_time": session.current_state["game_time"]}
    arguments = {"question": QUESTION, "include_candidates": True}
    result = session._handle_consult_rules(arguments, context)
    summary = await compact(session, [("consult_rules", arguments, result)])
    for text in (summary, await compact(session, [], summary)):
        assert "重新查询" in text and "候选" in text and "未提交" in text
        assert "Practice guide 在场" not in text
    oversized = json.loads(result)
    oversized["answers"] = ["SYNTHETIC_PRESENCE_BODY" * 1000]
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "test_presence", "function": {"name": "consult_rules"}}]},
        {"role": "tool", "tool_call_id": "test_presence", "content": json.dumps(oversized)},
    ]
    bounded = json.loads(_bound_tool_messages(messages, 2000)[-1]["content"])
    assert bounded["answers_omitted"] is True
    assert bounded["projection"]["provisional"] is True
    assert "重新查询" in bounded["notice"]
