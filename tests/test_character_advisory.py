import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _bound_tool_messages
from test_agent_state_compaction import compact
from test_scheduled_npc_overrides import make_session


def character_session():
    session = make_session()
    session.ai_provider = SimpleNamespace(generate=AsyncMock(return_value="SYNTHETIC_ACTOR_REPLY"))
    definition = session.script["npcs"][0]
    definition.update({"bio": "Script biography", "personality": "Script personality", "goals": [
        {"id": "test_done", "description": "SYNTHETIC_COMPLETED_GOAL"},
        {"id": "test_pending", "description": "SYNTHETIC_ACTIVE_GOAL",
         "condition": "SYNTHETIC_GOAL_CONDITION", "effects": ["SYNTHETIC_GOAL_EFFECT"]},
    ]})
    actor = session.current_state["npcs"]["test_guide"]
    actor.update({"name": "Runtime guide", "bio": "Runtime biography", "personality": "Runtime personality",
                  "current_mood": "focused", "current_room": "practice room", "known": False, "met": False})
    session.current_state["completed_npc_goals"] = [{"id": "test_guide:test_done"}]
    session.current_state["information_network"] = [
        {"fact": "SYNTHETIC_ACTOR_KNOWN", "known_by": ["test_guide"]},
        {"fact": "SYNTHETIC_OTHER_KNOWN", "known_by": ["test_other"]},
        {"fact": "SYNTHETIC_PENDING_EVENT_FACT", "known_by": ["test_guide"],
         "source_event_id": "test_shift", "known_at": {"test_guide": "2030-01-01T09:05:00"}},
    ]
    return session


async def invoke(session, context=None, **arguments):
    return json.loads(await session._dispatch_unified_tool(context or {}, "invoke_character", {
        "npc_id": "test_guide", "situation": "Consider your next practice task.", **arguments,
    }))


@pytest.mark.asyncio
async def test_character_advisory_uses_actor_runtime_goals_and_committed_knowledge_only():
    session = character_session()
    original = copy.deepcopy(session.current_state)
    result = await invoke(session)
    assert result["npc"] == "Runtime guide"
    assert result["mode"] == "advisory" and result["knowledge_source"] == "committed_state"
    assert result["context"]["location"] == "test_origin"
    assert result["context"]["current_room"] == "practice room"
    assert result["context"]["active_goals"] == [{"id": "test_pending", "description": "SYNTHETIC_ACTIVE_GOAL"}]
    assert result["game_time"] == original["game_time"] and "projection" not in result
    prompt = session.ai_provider.generate.await_args.kwargs["system"]
    for marker in ("Runtime biography", "Runtime personality", "focused", "SYNTHETIC_ACTOR_KNOWN",
                   "SYNTHETIC_ACTIVE_GOAL", "test_origin", "practice room"):
        assert marker in prompt
    for marker in ("Script biography", "SYNTHETIC_COMPLETED_GOAL", "SYNTHETIC_OTHER_KNOWN",
                   "SYNTHETIC_PENDING_EVENT_FACT", "SYNTHETIC_GOAL_CONDITION", "SYNTHETIC_GOAL_EFFECT"):
        assert marker not in prompt
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("end_time,ready,location,event_ids", [
    (None, 2, "test_origin", []),
    ("2030-01-01T09:05:00", 0, "test_origin", []),
    ("2030-01-01T09:05:00", 2, "test_desk", ["test_shift"]),
    ("2030-01-01T09:30:00", 2, "test_origin", ["test_shift"]),
])
async def test_character_candidate_state_uses_accepted_time_without_learning_pending_events(end_time, ready, location, event_ids):
    session = character_session()
    original = copy.deepcopy(session.current_state)
    update = {"variable_changes": [{"id": "test_ready", "op": "set", "value": ready}]}
    if end_time:
        update["end_time"] = end_time
    context = {"old_time": original["game_time"], "new_time": "2030-01-01T10:00:00",
               "_accepted_state_updates": [update]}
    result = await invoke(session, context, include_candidates=True)
    assert result["game_time"] == (end_time or original["game_time"])
    assert result["context"]["location"] == location
    assert result["projection"]["event_ids"] == event_ids
    assert result["projection"]["provisional"] is True
    assert result["projection"]["through"] == result["game_time"]
    prompt = session.ai_provider.generate.await_args.kwargs["system"]
    assert location in prompt and result["game_time"] in prompt
    assert "SYNTHETIC_PENDING_EVENT_FACT" not in prompt
    assert "SYNTHETIC_ACTOR_KNOWN" in prompt
    assert (await invoke(session, context))["context"]["location"] == "test_origin"
    context["_accepted_state_updates"].clear()
    assert (await invoke(session, context, include_candidates=True))["game_time"] == original["game_time"]
    assert session.current_state == original


@pytest.mark.asyncio
async def test_character_candidate_applies_cumulative_spatial_updates_without_mutating_state():
    session = character_session()
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"], "_accepted_state_updates": [
        {"npc_location_changes": [{"npc_id": "test_guide", "new_location": "test_desk"}]},
        {"room_changes": [{"id": "test_guide", "new_room": "practice hall"}]},
    ]}
    result = await invoke(session, context, include_candidates=True)
    assert result["context"]["location"] == "test_desk"
    assert result["context"]["current_room"] == "practice hall"
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,context", [
    ({"npc_id": "test_missing"}, None), ({"npc_id": ""}, None), ({"npc_id": []}, None),
    ({"situation": None}, None), ({"include_candidates": "true"}, None),
    ({"include_candidates": 1}, None), ({"include_candidates": True}, None),
])
async def test_character_rejects_invalid_requests_before_model_call(arguments, context):
    session = character_session()
    original = copy.deepcopy(session.current_state)
    assert "error" in await invoke(session, context, **arguments)
    session.ai_provider.generate.assert_not_awaited()
    assert session.current_state == original


@pytest.mark.asyncio
async def test_character_dynamic_actor_and_deceased_actor():
    session = character_session()
    session.current_state["npcs"]["test_dynamic"] = {"name": "Dynamic guide", "bio": "Dynamic biography"}
    assert (await invoke(session, npc_id="test_dynamic"))["npc"] == "Dynamic guide"
    session.ai_provider.generate.reset_mock()
    session.current_state["npcs"]["test_dynamic"]["alive"] = False
    assert "error" in await invoke(session, npc_id="test_dynamic")
    session.ai_provider.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_character_compaction_omits_advice_and_keeps_reinvocation_boundary():
    session = character_session()
    context = {"old_time": session.current_state["game_time"]}
    arguments = {"npc_id": "test_guide", "situation": "Consider your next task.", "include_candidates": True}
    result = await invoke(session, context, **arguments)
    session.ai_provider.generate_with_tools = AsyncMock(return_value={"content": "Done.", "tool_calls": []})
    summary = await compact(session, [("invoke_character", arguments, json.dumps(result))])
    for text in (summary, await compact(session, [], summary)):
        assert "重新调用" in text and "未提交" in text and "建议" in text
        assert "SYNTHETIC_ACTOR_REPLY" not in text
    result["response"] *= 2000
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "test_advice", "function": {"name": "invoke_character"}}]},
        {"role": "tool", "tool_call_id": "test_advice", "content": json.dumps(result)},
    ]
    bounded = json.loads(_bound_tool_messages(messages, 2000)[-1]["content"])
    assert bounded["response_omitted"] is True and bounded["projection"]["provisional"] is True
    assert bounded["mode"] == "advisory" and "重新调用" in bounded["notice"]
