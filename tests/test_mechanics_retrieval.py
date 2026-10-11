import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.tool_schemas import _unified_tools


RULE_CATEGORIES = ["variables", "triggers", "random_items", "one_time_events", "cyclic_events", "persistent_states", "world_properties"]


def make_session():
    script = {
        "script_id": "synthetic-rule-retrieval", "name": "Synthetic rule retrieval", "settings": {},
        "variables": [{"id": "test_capacity", "type": "number", "default": 2, "min": 0, "max": 10}],
        "triggers": [{"event": "test_tick", "action": "notify", "params": {"message": "Synthetic tick."}}],
        "random_items": [{
            "id": "test_roll", "dice": "1d1", "trigger_type": "per_turn",
            "description": "SYNTHETIC_RULE_DETAIL",
        }],
        "one_time_events": [{
            "id": "test_notice", "trigger_time": "2030-01-01T09:10:00",
            "condition": "script_variables.test_capacity >= 8",
            "description": "Synthetic notice.", "rule_notes": "SYNTHETIC_RULE_DETAIL",
            "effects": [{"type": "activate_persistent_state", "target": "test_condition"}],
        }],
        "cyclic_events": [{
            "id": "test_cycle", "first_trigger": "2030-01-01T09:20:00",
            "frequency_value": 10, "frequency_unit": "minute",
            "condition": "script_variables.test_capacity >= 8",
            "description": "Synthetic cycle.", "rule_notes": "SYNTHETIC_RULE_DETAIL",
            "effects": [{"type": "deactivate_persistent_state", "target": "test_condition"}],
        }],
        "persistent_states": [{"id": "test_condition", "description": "SYNTHETIC_RULE_DETAIL"}],
        "world_properties": [{"id": "test_access", "value": "closed", "rule_notes": "SYNTHETIC_RULE_DETAIL"}],
    }
    session = GameSession(script, None)
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "player": {},
        "inventory": [{"item": "test token"}],
        "world_properties": {"test_access": "open"},
    }
    session.script_variables.init_state(session.current_state)
    session.script_variables.set(session.current_state, "test_capacity", 7)
    return session, script


@pytest.mark.parametrize("category", RULE_CATEGORIES)
def test_catalog_returns_runtime_definitions_without_executing_them(category):
    session, _ = make_session()
    original_state = copy.deepcopy(session.current_state)
    original_rules = copy.deepcopy({**session._mechanics_definitions(), **{
        key: session.script[key] for key in RULE_CATEGORIES[3:]
    }})
    original_script = copy.deepcopy(session.script)
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
    elif category == "world_properties":
        assert json.loads(entry["content"])["current_value"] == "open"
    assert session.current_state == original_state
    assert session.script == original_script
    assert session._mechanics_definitions() == {key: original_rules[key] for key in RULE_CATEGORIES[:3]}


def test_search_and_pagination_keep_original_rule_indexes():
    session, _ = make_session()
    session._apply_variable_updates({"add": [
        {"id": f"test_capacity_{index}", "type": "number", "default": index, "description": "Synthetic quota."}
        for index in range(11)
    ]})
    entries = []
    offset = 0
    cursor = {}
    while offset is not None:
        result = json.loads(session._handle_consult_rules({"category": "variables", "query": "QUOTA", "offset": offset, **cursor}))
        assert result["total"] == 11
        assert len(result["entries"]) <= 5
        entries.extend(result["entries"])
        offset = result["next_offset"]
        cursor = {"snapshot": result["snapshot"]}
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
            "snapshot": entry["snapshot"],
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
            "snapshot": entry["snapshot"],
        }))
        entry, = result["entries"]
    payload = json.loads("".join(chunks))
    assert payload["current_value"] == value
    assert payload["definition"] == session.script_variables.definitions["test_long_value"]
    assert sum(map(len, chunks)) == entry["total_chars"]
    end = json.loads(session._handle_consult_rules({
        "category": "variables", "rule_index": entry["rule_index"], "offset": entry["total_chars"],
        "snapshot": entry["snapshot"],
    }))
    assert end["entries"][0]["content"] == ""
    assert end["entries"][0]["next_offset"] is None


@pytest.mark.parametrize("category", RULE_CATEGORIES)
def test_empty_categories_and_exhausted_pages(category):
    session = GameSession({"script_id": "synthetic-empty-rules", "settings": {}}, None)
    empty = json.loads(session._handle_consult_rules({"category": category}))
    assert empty["entries"] == [] and empty["total"] == 0
    assert empty["next_offset"] is None
    session, _ = make_session()
    first = json.loads(session._handle_consult_rules({"category": category}))
    exhausted = json.loads(session._handle_consult_rules({"category": category, "offset": 10**9, "snapshot": first["snapshot"]}))
    assert exhausted["entries"] == [] and exhausted["total"] == 1
    assert exhausted["next_offset"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("category", RULE_CATEGORIES)
async def test_context_compaction_keeps_rule_reread_coordinates(category):
    session, _ = make_session()
    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}),
    )
    definitions = {**session._mechanics_definitions(), **{
        key: session.script[key] for key in RULE_CATEGORIES[3:]
    }}
    definitions[category][0]["description"] = "SYNTHETIC_RULE_BODY " * 350
    result = session._handle_consult_rules({"category": category})
    messages = [
        {"role": "user", "content": "Inspect the synthetic rules."},
        {"role": "assistant", "content": "Synthetic prior context. " * 1600},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "synthetic-rule-read", "type": "function",
            "function": {"name": "consult_rules", "arguments": json.dumps({"category": category})},
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
    assert f'"category": "{category}"' in summary
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
    entries = json.loads(session._handle_consult_rules(query))["entries"]
    assert [{key: value for key, value in entry.items() if key != "snapshot"} for entry in entries] == [
        {key: value for key, value in entry.items() if key != "snapshot"} for entry in expected["entries"]
    ]
    assert entries[0]["snapshot"] != expected["entries"][0]["snapshot"]


def test_full_context_advertises_rules_without_dumping_private_definitions(monkeypatch):
    session, _ = make_session()
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert "<script_mechanics>" in context
    assert "consult_rules" in context
    for category in RULE_CATEGORIES:
        assert f'"{category}": 1' in context
    assert "SYNTHETIC_RULE_DETAIL" not in context
    plan = session._build_unified_plan_context({}, {"type": "freeform", "text": "Inspect."})
    assert "<script_mechanics>" not in plan
    assert "SYNTHETIC_RULE_DETAIL" not in plan


def test_consult_rules_schema_exposes_structured_retrieval_and_keeps_legacy_queries():
    schema = next(tool["function"]["parameters"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert set(schema["properties"]["category"]["enum"]) == set(RULE_CATEGORIES) | {"story_nodes", "locations"}
    assert {"query", "rule_index", "offset", "snapshot"} <= schema["properties"].keys()
    assert "question" not in schema.get("required", [])
    session, _ = make_session()
    assert json.loads(session._handle_consult_rules({"question": "有没有test token"}))["answers"] == ["持有 test token"]


@pytest.mark.parametrize("category", RULE_CATEGORIES[3:] + ["locations"])
def test_event_and_state_rules_support_search_pages_and_complete_rereads(category):
    session, _ = make_session()
    if category == "locations":
        session.script[category] = [{"id": "test_catalog", "default_room": "desk", "access_condition": "test_capacity >= 2"}]
    entries = session.script[category]
    entries.extend({
        **copy.deepcopy(entries[0]), "id": f"test_detail_{index}",
        "description": "Synthetic catalog 中文。" * 160 + f"FINAL_CLAUSE_{index}",
    } for index in range(11))
    original_state = copy.deepcopy(session.current_state)
    matches = []
    offset = 0
    cursor = {}
    while offset is not None:
        page = json.loads(session._handle_consult_rules({"category": category, "query": "final_clause", "offset": offset, **cursor}))
        assert page["total"] == 11
        assert len(page["entries"]) <= 5
        matches.extend(page["entries"])
        offset = page["next_offset"]
        cursor = {"snapshot": page["snapshot"]}
    assert [entry["rule_index"] for entry in matches] == list(range(1, 12))
    for entry in matches:
        chunks = [entry["content"]]
        while entry["next_offset"] is not None:
            page = json.loads(session._handle_consult_rules({
                "category": category, "rule_index": entry["rule_index"], "offset": entry["next_offset"],
                "snapshot": entry["snapshot"],
            }))
            entry, = page["entries"]
            assert entry["offset"] == sum(map(len, chunks))
            assert len(entry["content"]) <= 2000
            chunks.append(entry["content"])
        assert json.loads("".join(chunks))["definition"] == entries[entry["rule_index"]]
    assert session.current_state == original_state


@pytest.mark.asyncio
@pytest.mark.parametrize("category, kind", [("one_time_events", "one_time"), ("cyclic_events", "cyclic")])
@pytest.mark.parametrize("event_count", [1, 36])
async def test_dynamic_event_definitions_follow_branch_after_reload_without_log_clipping(category, kind, event_count):
    session, script = make_session()
    root = session.world_tree.add_node(None, "2030-01-01T09:00:00", 0, state_snapshot=copy.deepcopy(session.current_state))
    for index in range(event_count):
        definition = {**copy.deepcopy(script[category][0]), "id": f"test_dynamic_event_{index}"}
        session._apply_director_results({"events": {kind: [definition]}})
    final = session.world_tree.add_node(root, "2030-01-01T09:01:00", 1, state_snapshot=copy.deepcopy(session.current_state))
    expected = copy.deepcopy(session.script[category])
    saved = json.loads(json.dumps(session.current_state))
    restored = GameSession(script, None)
    restored.current_state = saved
    restored._restore_dynamic_story()
    restored._hydrate_branch_runtime(saved)
    query = {"category": category, "query": "test_dynamic_event_0"}
    result = json.loads(restored._handle_consult_rules(query))
    assert json.loads(result["entries"][0]["content"])["definition"] == expected[1]
    assert restored.script[category] == expected
    assert len(saved["dynamic_story_content"][category]) == event_count
    await session.branch_to_node(root)
    assert json.loads(session._handle_consult_rules(query))["entries"] == []
    assert session.script[category] == script[category]
    assert "test_dynamic_event_0" not in session._event_def_by_id
    assert "test_dynamic_event_0" not in session.event_engine.events
    assert "test_dynamic_event_0" not in session.event_scheduler._cyclic_by_id
    assert "test_dynamic_event_0" not in session.current_state.get("cyclic_event_trackers", {})
    scheduled = session.event_scheduler.check_events(
        session.current_state, "2030-01-01T09:00:00", "2030-01-01T09:30:00", lambda condition: True,
    )
    assert "test_dynamic_event_0" not in {event["event_id"] for event in scheduled}
    sibling_definition = {**copy.deepcopy(expected[1]), "description": "Synthetic alternate branch."}
    session._apply_director_results({"events": {kind: [sibling_definition]}})
    sibling = session.world_tree.add_node(root, "2030-01-01T09:01:00", 1, state_snapshot=copy.deepcopy(session.current_state))
    await session.branch_to_node(final)
    entries = json.loads(session._handle_consult_rules(query))["entries"]
    assert [{key: value for key, value in entry.items() if key != "snapshot"} for entry in entries] == [
        {key: value for key, value in entry.items() if key != "snapshot"} for entry in result["entries"]
    ]
    assert entries[0]["snapshot"] != result["entries"][0]["snapshot"]
    assert session.script[category] == expected
    assert session.event_engine.events["test_dynamic_event_0"].metadata["original_def"] == expected[1]
    scheduled = session.event_scheduler.check_events(
        session.current_state, "2030-01-01T09:00:00", "2030-01-01T09:30:00", lambda condition: True,
    )
    assert "test_dynamic_event_0" in {event["event_id"] for event in scheduled}
    await session.branch_to_node(sibling)
    sibling_result = json.loads(session._handle_consult_rules(query))
    assert json.loads(sibling_result["entries"][0]["content"])["definition"] == sibling_definition
    assert session.event_engine.events["test_dynamic_event_0"].metadata["original_def"] == sibling_definition
    restored._hydrate_branch_runtime(saved)
    assert restored.script[category] == expected


def test_dynamic_event_registration_rejects_duplicate_ids_and_keeps_snapshot_independent():
    session, script = make_session()
    definition = {**copy.deepcopy(script["cyclic_events"][0]), "id": "test_dynamic_cycle"}
    session._apply_director_results({"events": {"cyclic": [definition, definition]}})
    session._apply_director_results({"events": {"one_time": [definition, script["cyclic_events"][0]]}})
    saved = session.current_state["dynamic_story_content"]
    assert saved["cyclic_events"] == [definition]
    assert saved["one_time_events"] == []
    definition["description"] = "Synthetic mutated caller input."
    assert saved["cyclic_events"][0] == session._event_def_by_id["test_dynamic_cycle"]
    session._event_def_by_id["test_dynamic_cycle"]["description"] = "Synthetic temporary runtime."
    session._hydrate_branch_runtime(session.current_state)
    assert session._event_def_by_id["test_dynamic_cycle"] == saved["cyclic_events"][0]
    assert session.script["cyclic_events"][0] == script["cyclic_events"][0]


def test_dynamic_event_hydration_uses_target_snapshot_and_preserves_schedule_progress():
    session, script = make_session()
    current = copy.deepcopy(session.current_state)
    definition = {**copy.deepcopy(script["cyclic_events"][0]), "id": "test_dynamic_cycle"}
    target = copy.deepcopy(current)
    target["dynamic_story_content"] = {"cyclic_events": [definition]}
    target["events"] = {definition["id"]: {"next_fire": "2030-01-01T10:20:00", "last_fired": "2030-01-01T10:10:00"}}
    session._hydrate_branch_runtime(target)
    assert session.current_state == current
    assert target["cyclic_event_trackers"][definition["id"]] == target["events"][definition["id"]]
    target["cyclic_event_trackers"][definition["id"]]["next_fire"] = "2030-01-01T10:30:00"
    expected = copy.deepcopy(target)
    session._hydrate_branch_runtime(target)
    assert target == expected
    session._hydrate_branch_runtime(current)
    assert definition["id"] not in session.event_engine.events


@pytest.mark.asyncio
@pytest.mark.parametrize("category, kind", [("one_time_events", "one_time"), ("cyclic_events", "cyclic")])
async def test_failed_generation_rolls_back_dynamic_event_queries_and_execution_caches(category, kind):
    session, script = make_session()
    accepted = {**copy.deepcopy(script[category][0]), "id": "test_accepted_event"}
    session._apply_director_results({"events": {kind: [accepted]}})
    session._hydrate_branch_runtime(session.current_state)
    snapshot = game_routes._capture_generation_state(session)
    rejected = {**copy.deepcopy(script[category][0]), "id": "test_failed_event"}
    session._apply_director_results({"events": {kind: [rejected]}})
    assert rejected["id"] in session.event_engine.events
    await game_routes._rollback_generation_state(session, snapshot)
    assert session.current_state == snapshot["state"]
    assert session.script[category] == [*script[category], accepted]
    result = json.loads(session._handle_consult_rules({"category": category, "query": rejected["id"]}))
    assert result["entries"] == []
    assert rejected["id"] not in session.event_engine.events
    assert rejected["id"] not in session._event_def_by_id
    assert rejected["id"] not in session.event_scheduler._cyclic_by_id
    assert accepted["id"] in session.event_engine.events
