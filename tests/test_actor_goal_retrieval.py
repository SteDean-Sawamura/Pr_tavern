import asyncio
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.tool_schemas import _unified_tools
from test_agent_harness_execution import _stream_events
from test_director_goal_persistence import goal_client, make_session, updates
from test_story_choice_settlement import persist_node, stored_state


CATEGORIES = [("npc_goals", "test_reader"), ("org_goals", "test_circle")]


def query_goals(session, category, **arguments):
    return json.loads(session._handle_consult_rules({"category": category, **arguments}))


def payloads(result):
    assert "error" not in result, result
    return [json.loads(entry["content"]) for entry in result["entries"]]


@pytest.mark.parametrize("category, actor_id", CATEGORIES)
def test_empty_goal_catalog(category, actor_id):
    session, _ = make_session()
    section = "npcs" if category == "npc_goals" else "organizations"
    session.script[section][0].pop("goals")
    result = query_goals(session, category)
    assert result["entries"] == [] and result["total"] == 0 and result["next_offset"] is None


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("category, actor_id", CATEGORIES)
def test_catalog_exposes_complete_goals_without_evaluating_or_completing_them(monkeypatch, dynamic, category, actor_id):
    session, script = make_session(dynamic)
    original = copy.deepcopy(script)
    declaration = updates()
    declaration[category][0]["goals"][0]["author_metadata"] = {"nested": ["SYNTHETIC_GOAL_RULE_DETAIL"]}
    session._apply_director_results({"goals": declaration})
    session.current_state["script_variables"]["test_gate"] = 9
    before = copy.deepcopy(session.current_state)
    before_runtime = copy.deepcopy(session.script)

    def forbidden(*args, **kwargs):
        raise AssertionError("Rule discovery must not execute or evaluate goals")

    for name in ("_evaluate_condition", "_check_npc_goals", "_check_org_goals", "_apply_org_goal_effects"):
        monkeypatch.setattr(session, name, forbidden)
    result = query_goals(session, category, query="SYNTHETIC_GOAL_RULE_DETAIL")
    record, = payloads(result)
    assert record["definition"]["actor_id"] == actor_id
    assert record["definition"]["actor_name"].startswith("Synthetic")
    assert record["definition"]["goal"] == declaration[category][0]["goals"][0]
    assert record["goal_key"] == f"{actor_id}:test_new_goal"
    assert record["status"] == "pending" and record["completion"] is None
    assert session.current_state == before and session.script == before_runtime and script == original
    assert "未结算" in result["notice"] and "非角色知情" in result["notice"]


def test_catalog_matches_state_only_npc_fallback_without_overriding_static_definitions():
    session, _ = make_session()
    session.current_state["npcs"]["test_reader"]["goals"] = [{"id": "test_wrong", "description": "Stale state."}]
    session.current_state["npcs"]["test_visitor"] = {
        "name": "Synthetic visitor", "goals": [{"id": "test_original", "description": "Inspect the catalog."}],
    }
    result = payloads(query_goals(session, "npc_goals"))
    assert [record["goal_key"] for record in result] == ["test_reader:test_original", "test_visitor:test_original"]
    assert "test_visitor" not in session._npc_by_id
    session.current_state["completed_npc_goals"] = ["test_visitor:test_original"]
    result = payloads(query_goals(session, "npc_goals"))
    assert [record["status"] for record in result] == ["pending", "completed"]
    assert result[-1]["completion"] == "test_visitor:test_original"
    assert payloads(query_goals(session, "org_goals"))[0]["status"] == "pending"


@pytest.mark.parametrize("category, actor_id", CATEGORIES)
def test_goal_catalog_search_pages_and_long_field_reads(category, actor_id):
    session, _ = make_session()
    body = "SYNTHETIC_GOAL_RULE_DETAIL 测试字段 " * 180
    session._apply_director_results({"goals": {category: [{
        "npc_id" if category == "npc_goals" else "org_id": actor_id,
        "goals": [{"id": f"test_extra_{index}", "description": "Synthetic archive goal.", "rule_notes": body}
                  for index in range(7)],
    }]}})
    result = query_goals(session, category, query="ARCHIVE")
    assert result["total"] == 7 and result["next_offset"] == 5
    assert [entry["rule_index"] for entry in result["entries"]] == [1, 2, 3, 4, 5]
    next_page = query_goals(session, category, query="ARCHIVE", offset=5, snapshot=result["snapshot"])
    assert [entry["rule_index"] for entry in next_page["entries"]] == [6, 7]
    entry = result["entries"][0]
    parameters = {"rule_index": entry["rule_index"], "snapshot": entry["snapshot"]}
    fragments = []
    offset = 0
    while offset is not None:
        current, = query_goals(session, category, offset=offset, **parameters)["entries"]
        fragments.append(current["content"])
        offset = current["next_offset"]
    record = json.loads("".join(fragments))
    assert record["definition"]["goal"]["rule_notes"] == body
    directory, = query_goals(session, category, result_path=[], **parameters)["entries"]
    assert directory["content_format"] == "result_section"
    assert "definition" in [item["key"] for item in directory["entries"]]
    fragments = []
    offset = 0
    while offset is not None:
        current, = query_goals(session, category, offset=offset,
                              result_path=["definition", "goal", "rule_notes"], **parameters)["entries"]
        fragments.append(current["content"])
        offset = current["next_offset"]
    assert "".join(fragments) == body


@pytest.mark.parametrize("category, actor_id", CATEGORIES)
def test_goal_completion_invalidates_old_reads_and_never_predicts_pending_updates(category, actor_id):
    session, _ = make_session()
    session._apply_director_results({"goals": updates()})
    result = query_goals(session, category, query="test_new_goal")
    entry, = result["entries"]
    arguments = {"rule_index": entry["rule_index"], "snapshot": entry["snapshot"]}
    assert query_goals(session, category, rule_index=entry["rule_index"])["error_code"] == "snapshot_required"
    session.current_state["script_variables"]["test_gate"] = 9
    assert payloads(query_goals(session, category, **arguments))[0]["status"] == "pending"
    session._check_npc_goals()
    session._check_org_goals()
    expired = query_goals(session, category, **arguments)
    assert expired["error_code"] == "snapshot_expired" and expired["restart_required"] is True
    fresh, = payloads(query_goals(session, category, query="test_new_goal"))
    assert fresh["status"] == "completed" and fresh["completion"]["id"] == f"{actor_id}:test_new_goal"
    assert query_goals(session, category, view="runtime").get("error")
    candidate = json.loads(session._handle_consult_rules({"category": category, "include_candidates": True},
                                                        {"old_time": session.current_state["game_time"]}))
    assert "error" in candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_goal_queries_follow_sqlite_reload_and_sibling_snapshots(tmp_path, monkeypatch, dynamic):
    session, script = make_session(dynamic)
    async with goal_client(session, script, tmp_path, monkeypatch) as (_, sessions):
        root = session.world_tree.active_node_id
        branches = []
        for index in range(2):
            await session.branch_to_node(root)
            session._apply_director_results({"goals": updates(f"SYNTHETIC_GOAL_BRANCH_{index}")})
            if index:
                session.current_state["script_variables"]["test_gate"] = 9
                session._check_npc_goals()
                session._check_org_goals()
            branches.append(await persist_node(session, root))
        old = query_goals(session, "npc_goals", query="test_new_goal")["entries"][0]
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        for index, branch in enumerate(branches):
            await restored.branch_to_node(branch)
            for category, actor_id in CATEGORIES:
                record, = payloads(query_goals(restored, category, query="test_new_goal"))
                assert record["goal_key"] == f"{actor_id}:test_new_goal"
                assert record["definition"]["goal"]["description"] == f"SYNTHETIC_GOAL_BRANCH_{index}"
                assert record["status"] == ("completed" if index else "pending")
            if not index:
                assert query_goals(restored, "npc_goals", rule_index=old["rule_index"],
                                   snapshot=old["snapshot"])["error_code"] == "snapshot_expired"
        await restored.branch_to_node(root)
        assert query_goals(restored, "npc_goals", query="test_new_goal")["total"] == 0
        assert query_goals(restored, "org_goals", query="test_new_goal")["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("category, actor_id", CATEGORIES)
async def test_goal_discovery_and_compaction_keep_reread_coordinates_not_rules(monkeypatch, category, actor_id):
    session, _ = make_session()
    declaration = updates()
    declaration[category][0]["goals"][0]["rule_notes"] = "SYNTHETIC_GOAL_RULE_DETAIL " * 220
    session._apply_director_results({"goals": declaration})
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert f'"{category}": 2' in context and "consult_rules" in context
    assert "SYNTHETIC_GOAL_RULE_DETAIL" not in context
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert category in schema["parameters"]["properties"]["category"]["enum"]
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    arguments = {"category": category, "query": "SYNTHETIC_GOAL_RULE_DETAIL"}
    messages = [
        {"role": "user", "content": "Inspect the synthetic goals."},
        {"role": "assistant", "content": "Synthetic previous context. " * 1600},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "synthetic-goal-read", "type": "function",
            "function": {"name": "consult_rules", "arguments": json.dumps(arguments)},
        }]},
        {"role": "tool", "tool_call_id": "synthetic-goal-read", "content": session._handle_consult_rules(arguments)},
        {"role": "user", "content": "Keep the plan."},
        {"role": "assistant", "content": "Continuing."},
        {"role": "user", "content": "Inspect the current goals."},
        {"role": "assistant", "content": "Ready."},
    ]
    events = [event async for event in session._agent_loop(messages, "Synthetic system.", [], lambda name, args: "", max_rounds=1)]
    assert not any(event.get("type") == "error" for event in events)
    summary = session.ai_provider.generate_with_tools.await_args.args[0][1]["content"]
    assert "[上下文压缩]" in summary and f'"category": "{category}"' in summary
    assert '"rule_index": 1' in summary and '"next_offset": 2000' in summary and '"snapshot":' in summary
    assert "offset=0" in summary and "SYNTHETIC_GOAL_RULE_DETAIL" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic, threshold, reward", [(False, 7, 3), (True, 9, 5)])
async def test_agent_reads_goal_rules_then_settles_once_after_reload(tmp_path, monkeypatch, dynamic, threshold, reward):
    session, script = make_session(dynamic)
    declaration = updates("Inspect the catalog.", threshold)
    for category, _ in CATEGORIES:
        declaration[category][0]["goals"][0]["rule_notes"] = "SYNTHETIC_GOAL_RULE_DETAIL"
    declaration["org_goals"][0]["goals"][0]["on_complete_effects"] = {
        "state_change": [{"target": "player.gold", "op": "add", "value": reward}],
    }
    session.current_state["player"]["gold"] = 10
    session.current_state["script_variables"]["test_gate"] = 0
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session._apply_director_results({"goals": declaration})
    narrative = (
        "You remain beside the reading room shelf and inspect it carefully. "
        "You leave all the books in place and keep the aisle clear, then finish the inspection "
        "without disturbing anyone in the room."
    )
    observed = []

    async def generate(messages, **kwargs):
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })

    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        initial = await persist_node(session, session.world_tree.active_node_id)
        for turn in range(2):
            game_routes._sessions.clear()
            session = await game_routes._restore_session(session.save_id)
            assert session is not None
            sessions.append(session)
            steps = []

            async def respond(messages, **kwargs):
                names = [tool["function"]["name"] for tool in kwargs["tools"]]
                if names == ["classify_intent"]:
                    name, arguments = "classify_intent", {"intent": "action"}
                elif names == ["extract_notes"]:
                    name, arguments = "extract_notes", {"notes": []}
                else:
                    step = len(steps)
                    steps.append(step)
                    operations = [
                        ("submit_plan", {"plan": "Read the current goals before inspecting the catalog."}),
                        ("consult_rules", {"category": "npc_goals", "query": "test_new_goal"}),
                        ("consult_rules", {"category": "org_goals", "query": "test_new_goal"}),
                        ("update_state", {"end_time": f"2030-01-01T09:0{turn + 1}:00"}),
                        ("write_narrative", {"text": narrative}),
                        ("finalize_turn", {"summary": "Inspected the catalog."}),
                    ]
                    assert step < len(operations)
                    name, arguments = operations[step]
                    if step == 3:
                        records = [payloads(json.loads(next(message for message in messages
                                   if message.get("tool_call_id") == f"goal-read-{index}")["content"]))[0]
                                   for index in (1, 2)]
                        observed.append([record["status"] for record in records])
                        assert all(record["definition"]["goal"]["rule_notes"] == "SYNTHETIC_GOAL_RULE_DETAIL" for record in records)
                        if records[1]["status"] == "pending":
                            values = [int(re.fullmatch(r"script_variables\.test_gate >= (\d+)",
                                                      record["definition"]["goal"]["condition_met"]).group(1))
                                      for record in records]
                            arguments["variable_changes"] = [{"id": "test_gate", "op": "set", "value": max(values)}]
                        assert session.current_state["script_variables"]["test_gate"] == (threshold if turn else 0)
                assert name in names
                return {"content": "", "tool_calls": [{"id": f"goal-read-{len(steps) - 1}", "name": name, "arguments": arguments}]}

            session.ai_provider = SimpleNamespace(generate=AsyncMock(side_effect=generate), generate_with_tools=AsyncMock(side_effect=respond))
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Inspect the catalog.", "action_id": f"synthetic-goal-rule-{turn}",
                      "expected_node_id": session.world_tree.active_node_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], response.text
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative and steps == list(range(6))
            state = await stored_state(session)
            assert state["script_variables"]["test_gate"] == threshold
            assert state["player"]["gold"] == (10 + reward if turn else 10)
            for category, actor_id in CATEGORIES:
                assert sum(record["id"] == f"{actor_id}:test_new_goal" for record in state.get(f"completed_{category}", [])) == turn
                record, = payloads(query_goals(session, category, query="test_new_goal"))
                assert record["status"] == ("completed" if turn else "pending")
            calls = session.ai_provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert session.ai_provider.generate_with_tools.await_count == calls
            trace = await game_routes.get_tree_node(session.save_id, final["node_id"])
            assert "SYNTHETIC_GOAL_RULE_DETAIL" not in response.text + json.dumps(trace)
        assert observed == [["pending", "pending"], ["completed", "completed"]]
        await session.branch_to_node(initial)
        for category, _ in CATEGORIES:
            assert payloads(query_goals(session, category, query="test_new_goal"))[0]["status"] == "pending"
        assert session.current_state["player"]["gold"] == 10
