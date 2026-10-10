import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.tool_schemas import _unified_tools


def make_session():
    script = {
        "script_id": "synthetic-rule-retrieval", "name": "Synthetic rule retrieval", "settings": {},
        "variables": [{"id": "test_capacity", "type": "number", "default": 2, "min": 0, "max": 10}],
        "triggers": [{"event": "test_tick", "action": "notify", "params": {"message": "Synthetic tick."}}],
        "random_items": [{
            "id": "test_roll", "dice": "1d1", "trigger_type": "per_turn",
            "description": "SYNTHETIC_RULE_DETAIL",
        }],
    }
    session = GameSession(script, None)
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "player": {},
        "inventory": [{"item": "test token"}],
    }
    session.script_variables.init_state(session.current_state)
    session.script_variables.set(session.current_state, "test_capacity", 7)
    return session, script


@pytest.mark.parametrize("category", ["variables", "triggers", "random_items"])
def test_catalog_returns_runtime_definitions_without_executing_them(category):
    session, _ = make_session()
    original_state = copy.deepcopy(session.current_state)
    original_rules = copy.deepcopy(session._mechanics_definitions())
    result = json.loads(session._handle_consult_rules({"category": category}))
    assert result["source"] == "current_branch"
    assert result["game_time"] == original_state["game_time"]
    assert result["total"] == 1
    assert result["next_offset"] is None
    entry, = result["entries"]
    assert entry["rule_index"] == 0
    assert json.loads(entry["content"])["definition"] == original_rules[category][0]
    assert entry["next_offset"] is None
    if category == "variables":
        assert json.loads(entry["content"])["current_value"] == 7
    assert session.current_state == original_state
    assert session._mechanics_definitions() == original_rules


def test_search_and_pagination_keep_original_rule_indexes():
    session, _ = make_session()
    session._apply_variable_updates({"add": [
        {"id": f"test_capacity_{index}", "type": "number", "default": index, "description": "Synthetic quota."}
        for index in range(11)
    ]})
    entries = []
    offset = 0
    while offset is not None:
        result = json.loads(session._handle_consult_rules({"category": "variables", "query": "QUOTA", "offset": offset}))
        assert result["total"] == 11
        assert len(result["entries"]) <= 5
        entries.extend(result["entries"])
        offset = result["next_offset"]
    assert [entry["rule_index"] for entry in entries] == list(range(1, 12))
    assert [json.loads(entry["content"])["current_value"] for entry in entries] == list(range(11))
    missing = json.loads(session._handle_consult_rules({"category": "variables", "query": "test_missing_marker"}))
    assert missing["entries"] == [] and missing["total"] == 0


def test_long_rule_and_idless_trigger_can_be_read_completely():
    session, _ = make_session()
    session.trigger_engine.triggers[0]["params"]["message"] = "Synthetic detail. " * 350 + "FINAL_RULE_CLAUSE"
    result = json.loads(session._handle_consult_rules({"category": "triggers"}))
    entry, = result["entries"]
    chunks = [entry["content"]]
    while entry["next_offset"] is not None:
        result = json.loads(session._handle_consult_rules({
            "category": "triggers", "rule_index": entry["rule_index"], "offset": entry["next_offset"],
        }))
        entry, = result["entries"]
        assert len(entry["content"]) <= 2000
        chunks.append(entry["content"])
    assert json.loads("".join(chunks))["definition"] == session.trigger_engine.triggers[0]
    assert "FINAL_RULE_CLAUSE" in chunks[-1]


def test_long_variable_value_is_searchable_and_can_be_read_completely():
    session, _ = make_session()
    session._apply_variable_updates({"add": [{"id": "test_long_value", "type": "string", "default": ""}]})
    value = "Synthetic value 中文。" * 350 + "FINAL_VALUE_CLAUSE"
    session.script_variables.set(session.current_state, "test_long_value", value)
    result = json.loads(session._handle_consult_rules({"category": "variables", "query": "final_value_clause"}))
    entry, = result["entries"]
    chunks = []
    while True:
        assert len(entry["content"]) <= 2000
        assert entry["offset"] == sum(map(len, chunks))
        chunks.append(entry["content"])
        if entry["next_offset"] is None:
            break
        result = json.loads(session._handle_consult_rules({
            "category": "variables", "rule_index": entry["rule_index"], "offset": entry["next_offset"],
        }))
        entry, = result["entries"]
    payload = json.loads("".join(chunks))
    assert payload["current_value"] == value
    assert payload["definition"] == session.script_variables.definitions["test_long_value"]
    assert sum(map(len, chunks)) == entry["total_chars"]
    end = json.loads(session._handle_consult_rules({
        "category": "variables", "rule_index": entry["rule_index"], "offset": entry["total_chars"],
    }))
    assert end["entries"][0]["content"] == ""
    assert end["entries"][0]["next_offset"] is None


@pytest.mark.parametrize("category", ["variables", "triggers", "random_items"])
def test_empty_categories_and_exhausted_pages(category):
    session = GameSession({"script_id": "synthetic-empty-rules", "settings": {}}, None)
    empty = json.loads(session._handle_consult_rules({"category": category}))
    assert empty["entries"] == [] and empty["total"] == 0
    assert empty["next_offset"] is None
    session, _ = make_session()
    exhausted = json.loads(session._handle_consult_rules({"category": category, "offset": 10**9}))
    assert exhausted["entries"] == [] and exhausted["total"] == 1
    assert exhausted["next_offset"] is None


@pytest.mark.asyncio
async def test_context_compaction_keeps_rule_reread_coordinates():
    session, _ = make_session()
    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}),
    )
    session.trigger_engine.triggers[0]["params"]["message"] = "SYNTHETIC_RULE_BODY " * 350
    result = session._handle_consult_rules({"category": "triggers"})
    messages = [
        {"role": "user", "content": "Inspect the synthetic rules."},
        {"role": "assistant", "content": "Synthetic prior context. " * 1600},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "synthetic-rule-read", "type": "function",
            "function": {"name": "consult_rules", "arguments": json.dumps({"category": "triggers"})},
        }]},
        {"role": "tool", "tool_call_id": "synthetic-rule-read", "content": result},
        {"role": "user", "content": "Keep the current plan."},
        {"role": "assistant", "content": "Continuing."},
        {"role": "user", "content": "Inspect the current branch."},
        {"role": "assistant", "content": "Ready."},
    ]
    events = [event async for event in session._agent_loop(
        messages, "Synthetic system.", [], lambda name, args: "", max_rounds=1,
    )]
    assert not any(event.get("type") == "error" for event in events)
    compacted = session.ai_provider.generate_with_tools.await_args.args[0]
    summary = compacted[1]["content"]
    assert "[上下文压缩]" in summary
    assert '"category": "triggers"' in summary
    assert '"rule_index": 0' in summary
    assert '"next_offset": 2000' in summary
    assert '"total_chars":' in summary
    assert "consult_rules" in summary and "offset=0" in summary
    assert "SYNTHETIC_RULE_BODY" not in summary


@pytest.mark.parametrize("arguments", [
    {"category": "unknown"}, {"category": []}, {"category": ""}, {"category": None},
    {"category": "variables", "offset": -1}, {"category": "variables", "offset": True},
    {"category": "variables", "offset": None}, {"category": "variables", "offset": "2"},
    {"category": "variables", "offset": 1.5},
    {"category": "variables", "rule_index": -1}, {"category": "variables", "rule_index": 99},
    {"category": "variables", "rule_index": False}, {"category": "variables", "rule_index": None},
    {"category": "variables", "rule_index": 0, "query": "test_capacity"},
    {"category": "variables", "rule_index": 0, "offset": 10**9},
    {"category": "variables", "query": []},
])
def test_invalid_retrieval_arguments_return_tool_errors(arguments):
    session, _ = make_session()
    assert "error" in json.loads(session._handle_consult_rules(arguments))


@pytest.mark.asyncio
async def test_rule_queries_follow_reloaded_branch_and_current_values():
    session, script = make_session()
    root = session.world_tree.add_node(None, "2030-01-01T09:00:00", 0, state_snapshot=copy.deepcopy(session.current_state))
    session._apply_variable_updates({"add": [{"id": "test_dynamic_limit", "type": "number", "default": 3, "max": 8}]})
    session.script_variables.set(session.current_state, "test_dynamic_limit", 6)
    final = session.world_tree.add_node(root, "2030-01-01T09:01:00", 1, state_snapshot=copy.deepcopy(session.current_state))
    saved = json.loads(json.dumps(session.current_state))
    restored = GameSession(script, None)
    restored.current_state = saved
    restored._hydrate_branch_runtime(saved)
    query = {"category": "variables", "query": "test_dynamic_limit"}
    expected = json.loads(restored._handle_consult_rules(query))
    payload = json.loads(expected["entries"][0]["content"])
    assert payload["current_value"] == 6
    assert payload["definition"]["max"] == 8
    await session.branch_to_node(root)
    assert json.loads(session._handle_consult_rules(query))["entries"] == []
    await session.branch_to_node(final)
    assert json.loads(session._handle_consult_rules(query))["entries"] == expected["entries"]


def test_full_context_advertises_rules_without_dumping_private_definitions(monkeypatch):
    session, _ = make_session()
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert "<script_mechanics>" in context
    assert "consult_rules" in context
    assert '"variables": 1' in context
    assert "SYNTHETIC_RULE_DETAIL" not in context
    plan = session._build_unified_plan_context({}, {"type": "freeform", "text": "Inspect."})
    assert "<script_mechanics>" not in plan
    assert "SYNTHETIC_RULE_DETAIL" not in plan


def test_consult_rules_schema_exposes_structured_retrieval_and_keeps_legacy_queries():
    schema = next(tool["function"]["parameters"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert set(schema["properties"]["category"]["enum"]) == {"variables", "triggers", "random_items"}
    assert {"query", "rule_index", "offset"} <= schema["properties"].keys()
    assert "question" not in schema.get("required", [])
    session, _ = make_session()
    assert json.loads(session._handle_consult_rules({"question": "有没有test token"}))["answers"] == ["持有 test token"]
