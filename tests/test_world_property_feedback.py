import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.session.agentic_mixin import _bounded_tool_content
from test_agent_state_compaction import compact, state_feedback
from test_world_property_declarations import make_session


@pytest.fixture
def session():
    session, _ = make_session("test_access")
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(
        return_value={"content": "Done.", "tool_calls": []},
    ))
    return session


async def read_candidate(session, context, **arguments):
    result = json.loads(await session._dispatch_unified_tool(context, "consult_rules", {
        "category": "world_properties", "include_candidates": True, **arguments,
    }))
    assert "error" not in result
    assert result["projection"]["provisional"] is True
    return result


async def read_full_candidate(session, context, **arguments):
    result = await read_candidate(session, context, **arguments)
    entry, = result["entries"]
    chunks = [entry["content"]]
    while entry["next_offset"] is not None:
        result = await read_candidate(session, context, category=arguments.get("category", "world_properties"),
                                      rule_index=entry["rule_index"], offset=entry["next_offset"], snapshot=entry["snapshot"])
        entry, = result["entries"]
        assert entry["offset"] == sum(map(len, chunks))
        chunks.append(entry["content"])
    assert sum(map(len, chunks)) == entry["total_chars"]
    return json.loads("".join(chunks))


@pytest.mark.asyncio
@pytest.mark.parametrize("with_time", [False, True])
async def test_world_feedback_accumulates_without_committing(session, with_time):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    updates = [
        {"world_property_changes": [{"id": "test_access", "value": "open"}]},
        {"world_property_changes": [{"id": "test_capacity", "value": "ready"}]},
    ]
    if with_time:
        updates.append({"end_time": "2030-01-01T09:01:00"})
    records = [("update_state", arguments, session._handle_update_state(arguments, context)) for arguments in updates]
    feedback = state_feedback(await compact(session, records))
    assert '"test_access": "open"' in feedback
    assert '"test_capacity": "ready"' in feedback
    assert "未提交" in feedback and "不含尚未结算的事件效果" in feedback
    assert f"已接受 {len(updates)} 次状态更新" in feedback
    committed = json.loads(session._handle_consult_rules({"category": "world_properties"}))
    assert json.loads(committed["entries"][0]["content"])["current_value"] == "closed"
    assert "projection" not in committed
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_refused_updates_do_not_become_candidate_values(session, rejected):
    context = {"old_time": session.current_state["game_time"]}
    accepted = {"world_property_changes": [{"id": "test_access", "value": "open"}]}
    records = [("update_state", accepted, session._handle_update_state(accepted, context))]
    update = {"world_property_changes": [{
        "id": "test_access" if rejected else "test_document_status", "value": "UNACCEPTED_VALUE",
    }]}
    if rejected:
        update["variable_changes"] = [{"id": "test_unknown", "op": "inc"}]
    result = session._handle_update_state(update, context)
    assert ("错误:" if rejected else "过滤物件级属性") in result
    records.append(("update_state", update, result))
    feedback = state_feedback(await compact(session, records))
    assert '"test_access": "open"' in feedback
    assert "UNACCEPTED_VALUE" not in feedback
    candidate = await read_full_candidate(session, context, query="test_access")
    assert candidate["candidate_value"] == "open"
    missing = await read_candidate(session, context, query="test_document_status")
    assert missing["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("property_id", ["test_access", "test_new_capacity"])
@pytest.mark.parametrize("rejected", [False, True])
async def test_undo_replaces_candidate_feedback_and_retrieval(session, property_id, rejected):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(original), "npc_reactions": [], "new_npcs": [], "choices": [],
        "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": None, "conditioned_event_keys": None,
    }
    arguments = {"world_property_changes": [{"id": property_id, "value": "open"}]}
    if rejected:
        arguments["variable_changes"] = [{"id": "test_unknown", "op": "inc"}]
    result = session._handle_update_state(arguments, context)
    summary = await compact(session, [("update_state", arguments, result)])
    session._last_tool_result = {"name": "update_state", "args": arguments}
    undo = {"reason": "Discard this synthetic update."}
    result = await session._dispatch_unified_tool(context, "undo_my_last_action", undo)
    summary = await compact(session, [("undo_my_last_action", undo, result)], summary)
    feedback = state_feedback(summary)
    assert '"open"' not in feedback
    assert "已接受 0 次状态更新" in feedback
    if not rejected:
        if property_id == "test_access":
            assert '"test_access": "closed"' in feedback
        else:
            assert "未设置" in feedback and property_id in feedback
    if property_id == "test_access" or not rejected:
        candidate = await read_full_candidate(session, context, query=property_id)
        assert candidate["candidate_exists"] is (property_id == "test_access")
        assert candidate["candidate_value"] == ("closed" if property_id == "test_access" else None)
    assert session.current_state == original


@pytest.mark.asyncio
async def test_repeated_compaction_preserves_only_latest_world_feedback(session):
    context = {"old_time": session.current_state["game_time"]}
    summary = None
    for value in ("open", "closed", "ready"):
        arguments = {"world_property_changes": [{"id": "test_access", "value": value}]}
        result = session._handle_update_state(arguments, context)
        summary = await compact(session, [("update_state", arguments, result)], summary)
        assert f'"test_access": "{value}"' in state_feedback(summary)
    assert '"test_access": "open"' not in summary
    assert '"test_access": "closed"' not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_only", [False, True])
async def test_large_candidate_values_are_bounded_and_fully_rereadable(session, runtime_only):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    property_id = "test_runtime_capacity" if runtime_only else "test_access"
    value = "Synthetic candidate text. " * 1400 + "FINAL_CANDIDATE_CLAUSE"
    update = {"world_property_changes": [{"id": property_id, "value": value}]}
    result = session._handle_update_state(update, context)
    assert len(result) < 5000
    assert "省略" in result and "include_candidates" in result
    summary = await compact(session, [("update_state", update, result)])
    assert "include_candidates" in state_feedback(summary)
    payload = await read_full_candidate(session, context, query=property_id)
    assert payload["candidate_value"] == value
    assert payload["candidate_exists"] is True
    assert payload["current_value"] == (None if runtime_only else "closed")
    if runtime_only:
        assert payload["definition"] is None
        assert payload["id"] == property_id
    assert session.current_state == original


@pytest.mark.asyncio
async def test_candidate_query_metadata_survives_bounding_and_compaction(session):
    context = {"old_time": session.current_state["game_time"]}
    update = {"world_property_changes": [{"id": "test_access", "value": "Synthetic long value. " * 200}]}
    session._handle_update_state(update, context)
    arguments = {"category": "world_properties", "include_candidates": True}
    result = await session._dispatch_unified_tool(context, "consult_rules", arguments)
    bounded = json.loads(_bounded_tool_content(result, "consult_rules", 1200))
    assert bounded["projection"]["provisional"] is True
    assert "include_candidates" in bounded["notice"]
    for result in (result, json.dumps(bounded, ensure_ascii=False)):
        summary = await compact(session, [("consult_rules", arguments, result)])
        assert '"projection"' in summary
        assert "include_candidates" in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,context", [
    ({"category": "world_properties", "include_candidates": "true"}, {"old_time": "2030-01-01T09:00:00"}),
    ({"category": "one_time_events", "include_candidates": True}, {"old_time": "2030-01-01T09:00:00"}),
    ({"category": "world_properties", "include_candidates": True}, {}),
    ({"question": "Check the inventory.", "include_candidates": True}, {"old_time": "2030-01-01T09:00:00"}),
])
async def test_candidate_query_rejects_ambiguous_or_unavailable_projection(session, arguments, context):
    original = copy.deepcopy(session.current_state)
    result = json.loads(await session._dispatch_unified_tool(context, "consult_rules", arguments))
    assert "error" in result
    assert session.current_state == original


@pytest.mark.asyncio
async def test_narrative_revision_restores_world_feedback_and_background_query(session):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    operations = [
        ("update_state", {"world_property_changes": [{"id": "test_access", "value": "open"}]}),
        ("write_narrative", {"text": "You remain in the room without changing the access state."}),
        ("consult_rules", {"category": "world_properties", "include_candidates": True}),
        ("update_state", {"end_time": "2030-01-01T09:01:00"}),
    ]
    calls = []

    async def generate(messages, **kwargs):
        index = len(calls)
        calls.append(copy.deepcopy(messages))
        if index == len(operations):
            return {"content": "Done.", "tool_calls": []}
        name, arguments = operations[index]
        schema = next(tool["function"]["parameters"] for tool in kwargs["tools"] if tool["function"]["name"] == name)
        assert arguments.keys() <= schema["properties"].keys()
        assert set(schema.get("required", [])) <= arguments.keys()
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-world-revision-{index}", "name": name, "arguments": arguments,
        }]}

    session.ai_provider.generate_with_tools = AsyncMock(side_effect=generate)
    events = [event async for event in session._run_background_agent(
        context, "You check the access state.", "", {"text": "Stay here."},
    )]
    holder = next(event["holder"] for event in events if event["type"] == "_bg_result")
    assert holder.records[0]["superseded"] is True
    candidate = json.loads(holder.records[2]["result"])
    assert candidate["projection"]["accepted_updates"] == 0
    assert json.loads(candidate["entries"][0]["content"])["candidate_value"] == "closed"
    session.ai_provider.generate_with_tools = AsyncMock(return_value={"content": "Done.", "tool_calls": []})
    records = [(record["name"], record["args"], record["result"]) for record in holder.records[:2]]
    feedback = state_feedback(await compact(session, records))
    assert '"test_access": "closed"' in feedback and '"open"' not in feedback
    assert "已接受 0 次状态更新" in feedback
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["changed", "Synthetic long candidate. " * 500, None, 0, False])
async def test_explicit_variable_projection_keeps_committed_query_unchanged(session, value):
    script = {**session.script, "variables": [{"id": "test_value", "type": "custom", "default": "initial"}]}
    session = GameSession(script, session.ai_provider)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    arguments = {"category": "variables"}
    committed = session._handle_consult_rules(arguments)
    result = session._handle_update_state({"variable_changes": [{"id": "test_value", "op": "set", "value": value}]}, context)
    assert not result.startswith("错误:")
    payload = await read_full_candidate(session, context, category="variables", query="test_value")
    assert payload["current_value"] == "initial"
    assert payload["candidate_value"] == value
    assert payload["candidate_exists"] is True
    assert session._handle_consult_rules(arguments) == committed
    assert session._handle_consult_rules({**arguments, "include_candidates": False}, context) == committed
    assert session.current_state == original


@pytest.mark.asyncio
async def test_candidate_listing_paginates_without_forging_runtime_declarations(session):
    context = {"old_time": session.current_state["game_time"]}
    updates = {"world_property_changes": [{"id": f"test_capacity_{index}", "value": f"candidate_{index}"} for index in range(12)]}
    session._handle_update_state(updates, context)
    entries = []
    offset = 0
    cursor = {}
    while offset is not None:
        result = await read_candidate(session, context, offset=offset, **cursor)
        assert len(result["entries"]) <= 5
        entries.extend(json.loads(entry["content"]) for entry in result["entries"])
        offset = result["next_offset"]
        cursor = {"snapshot": result["snapshot"]}
    assert len(entries) == 13
    assert entries[0]["definition"] == session.script["world_properties"][0]
    assert all(entry["definition"] is None for entry in entries[1:])
    assert {entry["id"] for entry in entries[1:]} == {f"test_capacity_{index}" for index in range(12)}
    assert len(json.loads(session._handle_consult_rules({"category": "world_properties"}))["entries"]) == 1
