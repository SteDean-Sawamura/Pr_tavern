import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession


def make_session(trees=(), **sections):
    script = {
        "script_id": "synthetic-story-rules", "name": "Synthetic story rules", "settings": {},
        "variables": [{"id": "test_gate", "type": "number", "default": 0},
                      {"id": "test_progress", "type": "number", "default": 0}],
        "story_tree": {"trees": copy.deepcopy(list(trees))},
        **sections,
    }
    session = GameSession(script, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session, script


def story_node(node_id="test_step", **fields):
    return {
        "id": node_id, "name": "Synthetic step", "type": "auto",
        "condition": "script_variables.test_gate >= 7",
        "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 3}]},
        "rule_notes": "SYNTHETIC_STORY_RULE_DETAIL",
        **fields,
    }


def query_rules(session, **arguments):
    return json.loads(session._handle_consult_rules({"category": "story_nodes", **arguments}))


@pytest.mark.parametrize("has_raw_declarations", [False, True])
def test_story_catalog_is_empty_without_runtime(has_raw_declarations):
    trees = [{"id": "test_unloaded", "nodes": [story_node()]}] if has_raw_declarations else []
    session, _ = make_session(trees)
    session.story_tree_engine = None
    assert session.story_tree_engine is None
    result = query_rules(session)
    assert result["source"] == "current_branch"
    assert result["entries"] == []
    assert result["total"] == 0
    assert result["next_offset"] is None


@pytest.mark.parametrize("fields", [
    {"type": "auto"},
    {"type": "choice", "choices": [{"id": "test_option", "text": "Synthetic option",
                                     "effects": {"activate_lore": ["test_detail"]}}]},
    {"type": "timed", "duration_turns": 4},
    {"type": "quest", "completion_condition": "script_variables.test_gate >= 9"},
    {"type": "trigger", "event": ["before_generation"]},
    {"type": "periodic", "repeatable": True, "cooldown_turns": 2, "interval_turns": 3},
], ids=["auto", "choice", "timed", "quest", "trigger", "periodic"])
def test_story_catalog_keeps_complete_node_and_tree_definitions_without_execution(fields, monkeypatch):
    node = story_node(requires=["test_anchor"], custom_rules={"test_constraint": "Synthetic constraint"}, **fields)
    tree = {"id": "test_tree", "name": "Synthetic tree", "custom_rules": {"test_scope": "tree"},
            "nodes": [{"id": "test_anchor", "type": "quest"}, node]}
    session, _ = make_session([tree])
    original_state = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(session.script)
    original_runtime = copy.deepcopy(session.story_tree_engine.trees)
    monkeypatch.setattr(session.story_tree_engine, "evaluate", lambda *args, **kwargs: pytest.fail("Read executed rules"))
    result = query_rules(session, query="test_step")
    entry, = result["entries"]
    assert entry["rule_index"] == 1
    assert entry["next_offset"] is None
    assert json.loads(entry["content"]) == {"definition": {
        "tree": {key: value for key, value in tree.items() if key != "nodes"}, "node": node,
    }}
    assert session.current_state == original_state
    assert session.script == original_script
    assert session.story_tree_engine.trees == original_runtime


def test_story_catalog_reads_accepted_dynamic_nodes_not_unprocessed_input():
    session, _ = make_session([{"id": "test_static", "nodes": [{"id": "test_anchor", "type": "quest"}]}])
    accepted = story_node("test_choice", type="choice", choices=[{"id": "test_option"}],
                          effects={"notify": "SYNTHETIC_REJECTED_NOTICE", "activate_lore": ["test_detail"]})
    rejected = story_node("test_rejected", requires=["test_missing"])
    session._apply_director_results({"tree": {"new_nodes": [accepted, rejected, story_node("test_anchor")]}})
    original_state = copy.deepcopy(session.current_state)
    result = query_rules(session, query="test_choice")
    payload = json.loads(result["entries"][0]["content"])["definition"]
    assert payload["tree"]["id"] in session._dynamic_story_tree_ids
    assert payload["node"] == session.story_tree_engine._nodes["test_choice"]
    assert payload["node"]["type"] == "choice"
    assert payload["node"]["choices"] == accepted["choices"]
    assert payload["node"]["auto_resolve"] is False
    assert "notify" not in payload["node"]["effects"]
    assert query_rules(session, query="test_rejected")["entries"] == []
    assert query_rules(session)["total"] == 2
    assert session.current_state == original_state
    assert accepted["type"] == "choice"
    assert accepted["effects"]["notify"] == "SYNTHETIC_REJECTED_NOTICE"


def test_story_catalog_includes_compatible_system_nodes():
    session, _ = make_session(triggers=[{
        "event": "test_tick", "action": "notify", "params": {"message": "Synthetic tick"},
    }])
    result = query_rules(session)
    entry, = result["entries"]
    payload = json.loads(entry["content"])["definition"]
    assert payload["tree"]["id"] == "__system_triggers"
    assert payload["node"] == session.story_tree_engine.trees[0]["nodes"][0]


def test_story_catalog_paginates_across_trees_and_searches_full_definitions():
    trees = [{"id": f"test_tree_{tree_index}", "name": f"Synthetic catalog {tree_index} 合成目录",
              "nodes": [story_node(f"test_node_{tree_index * 4 + node_index}") for node_index in range(4)]}
             for tree_index in range(3)]
    session, _ = make_session(trees)
    entries = []
    offset = 0
    cursor = {}
    while True:
        result = query_rules(session, offset=offset, **cursor)
        assert result["total"] == 12
        assert len(result["entries"]) <= 5
        entries.extend(result["entries"])
        if result["next_offset"] is None:
            break
        assert result["next_offset"] == offset + 5
        offset = result["next_offset"]
        cursor = {"snapshot": result["snapshot"]}
    assert [entry["rule_index"] for entry in entries] == list(range(12))
    assert [json.loads(entry["content"])["definition"]["node"] for entry in entries] == [
        node for tree in trees for node in tree["nodes"]
    ]
    for query, indexes in [("TeSt_NoDe_11", [11]), ("SYNTHETIC catalog 1", [4, 5, 6, 7]), ("合成目录", list(range(5)))]:
        result = query_rules(session, query=query)
        assert [entry["rule_index"] for entry in result["entries"]] == indexes
    assert query_rules(session, offset=100, **cursor)["entries"] == []


def test_long_tree_metadata_and_node_rules_can_be_reassembled_without_siblings():
    node = story_node(rule_notes="SYNTHETIC_STORY_RULE_BODY " * 180 + "合成终段")
    tree = {"id": "test_long_tree", "rule_notes": "SYNTHETIC_TREE_RULE_BODY " * 120,
            "nodes": [node, story_node("test_sibling", rule_notes="SYNTHETIC_SIBLING_RULE")]}
    session, _ = make_session([tree])
    result = query_rules(session, query="合成终段")
    entry, = result["entries"]
    assert entry["rule_index"] == 0
    chunks = [entry["content"]]
    while entry["next_offset"] is not None:
        result = query_rules(session, rule_index=entry["rule_index"], offset=entry["next_offset"], snapshot=entry["snapshot"])
        entry, = result["entries"]
        assert len(entry["content"]) <= 2000
        chunks.append(entry["content"])
    content = "".join(chunks)
    assert len(content) == entry["total_chars"]
    assert json.loads(content) == {"definition": {
        "tree": {key: value for key, value in tree.items() if key != "nodes"}, "node": node,
    }}
    assert "SYNTHETIC_SIBLING_RULE" not in content


@pytest.mark.asyncio
async def test_story_queries_follow_reload_sibling_branches_and_failed_rollback():
    session, script = make_session()
    root = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                       state_snapshot=copy.deepcopy(session.current_state))
    definitions = [story_node(condition=f"script_variables.test_gate >= {threshold}") for threshold in (7, 13)]
    branches = []
    snapshots = []
    for definition in definitions:
        await session.branch_to_node(root)
        assert query_rules(session)["entries"] == []
        session._apply_director_results({"tree": {"new_nodes": [definition]}})
        snapshots.append(copy.deepcopy(session.current_state))
        branches.append(session.world_tree.add_node(root, session.current_state["game_time"], 1,
                                                    state_snapshot=copy.deepcopy(session.current_state)))
    restored = GameSession(script, None)
    restored.current_state = json.loads(json.dumps(snapshots[0]))
    restored._restore_dynamic_story()
    restored._hydrate_branch_runtime(restored.current_state)
    assert json.loads(query_rules(restored)["entries"][0]["content"])["definition"]["node"] == definitions[0]
    for index in (0, 1, 0):
        await session.branch_to_node(branches[index])
        assert json.loads(query_rules(session)["entries"][0]["content"])["definition"]["node"] == definitions[index]
    snapshot = game_routes._capture_generation_state(session)
    session.turn_number += 1
    session._apply_director_results({"tree": {"new_nodes": [story_node("test_failed")]}})
    assert query_rules(session, query="test_failed")["total"] == 1
    await game_routes._rollback_generation_state(session, snapshot)
    assert query_rules(session, query="test_failed")["entries"] == []
    assert json.loads(query_rules(session)["entries"][0]["content"])["definition"]["node"] == definitions[0]


@pytest.mark.asyncio
async def test_story_rule_compaction_keeps_full_reread_coordinates():
    session, _ = make_session([{"id": "test_tree", "nodes": [story_node(rule_notes="SYNTHETIC_STORY_RULE_BODY " * 350)]}])
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    messages = [
        {"role": "user", "content": "Inspect the synthetic story rules."},
        {"role": "assistant", "content": "Synthetic prior context. " * 1600},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "synthetic-story-read", "type": "function",
            "function": {"name": "consult_rules", "arguments": json.dumps({"category": "story_nodes"})},
        }]},
        {"role": "tool", "tool_call_id": "synthetic-story-read", "content": session._handle_consult_rules({"category": "story_nodes"})},
        {"role": "user", "content": "Keep the current plan."},
        {"role": "assistant", "content": "Continuing."},
        {"role": "user", "content": "Inspect the current branch."},
        {"role": "assistant", "content": "Ready."},
    ]
    events = [event async for event in session._agent_loop(messages, "Synthetic system.", [], lambda name, args: "", max_rounds=1)]
    assert not any(event.get("type") == "error" for event in events)
    summary = session.ai_provider.generate_with_tools.await_args.args[0][1]["content"]
    assert "[上下文压缩]" in summary
    assert '"category": "story_nodes"' in summary
    assert '"rule_index": 0' in summary
    assert '"next_offset": 2000' in summary
    assert '"total_chars":' in summary
    assert "consult_rules" in summary and "offset=0" in summary
    assert "SYNTHETIC_STORY_RULE_BODY" not in summary


def test_context_advertises_story_rule_count_without_dumping_definitions(monkeypatch):
    session, _ = make_session([{"id": "test_tree", "nodes": [story_node()]}])
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert '"story_nodes": 1' in context
    assert "consult_rules" in context
    assert "SYNTHETIC_STORY_RULE_DETAIL" not in context
    assert "SYNTHETIC_STORY_RULE_DETAIL" not in session._build_unified_plan_context({}, {"type": "freeform", "text": "Inspect."})
