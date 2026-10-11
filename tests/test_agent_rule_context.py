import asyncio
import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.session.agentic_mixin import _AgentResult, _bound_tool_messages
from engine.tool_schemas import _unified_tools
from test_agent_state_compaction import compact
from test_mechanics_retrieval import RULE_CATEGORIES, make_session


CATEGORIES = ["story_nodes", *RULE_CATEGORIES]
SYSTEM = "Inspect the synthetic declarations before proposing any change."
BODY = 'SYNTHETIC_RULE_BUDGET_BODY 合成约束 "quoted" \\path\\\n' * 120


def large_session(value=7):
    _, script = make_session()
    for category in RULE_CATEGORIES:
        template = script[category][0]
        script[category] = [
            {**copy.deepcopy(template), "id": f"test_{category}_{index}", "rule_notes": BODY}
            for index in range(7)
        ]
    script["story_tree"] = {"trees": [{"id": "test_budget_tree", "nodes": [
        {"id": f"test_node_{index}", "type": "quest", "rule_notes": BODY,
         "action_rule": {"variable_changes": [{"id": "test_variables_2", "op": "set", "value": value}]}}
        for index in range(7)
    ]}]}
    session = GameSession(script, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


def tool_call(name, arguments, call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def model_call(name, arguments, call_id):
    return {"id": call_id, "name": name, "arguments": arguments}


def rule_messages(session, arguments):
    return [
        {"role": "user", "content": "Inspect the synthetic rules."},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("consult_rules", arguments, "test_rule")]},
        {"role": "tool", "tool_call_id": "test_rule", "content": session._handle_consult_rules(arguments)},
    ]


@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("mode", ["catalog", "next_page", "detail"])
def test_bounded_rules_keep_json_pagination_and_complete_reread_coordinates(category, mode):
    session = large_session()
    original_state = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(session.script)
    original_trees = copy.deepcopy(session.story_tree_engine.trees)
    arguments = {"category": category}
    first = json.loads(session._handle_consult_rules(arguments))
    if mode == "detail":
        arguments.update(rule_index=2, offset=2000, snapshot=first["entries"][2]["snapshot"])
    elif mode == "next_page":
        arguments.update(offset=5, snapshot=first["snapshot"])
    messages = rule_messages(session, arguments)
    original = json.loads(messages[-1]["content"])
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) > 2000
    bounded = _bound_tool_messages(messages, 2000)
    result = json.loads(bounded[-1]["content"])
    for key in ("category", "source", "game_time", "offset", "next_offset", "total", "snapshot"):
        assert result[key] == original[key]
    assert result["truncated"] is True
    assert "offset=0" in result["notice"]
    assert len(result["entries"]) == len(original["entries"])
    for entry, previous in zip(result["entries"], original["entries"]):
        assert "content" not in entry
        assert entry == {key: previous[key] for key in ("rule_index", "offset", "next_offset", "total_chars", "snapshot")}
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in bounded) <= 2000
    assert len(json.dumps(bounded[-1], ensure_ascii=False)) <= 1000
    assert session.current_state == original_state
    assert session.script == original_script
    assert session.story_tree_engine.trees == original_trees


@pytest.mark.parametrize("category", CATEGORIES)
def test_rules_within_budget_are_not_changed(category):
    session = large_session()
    messages = rule_messages(session, {"category": category})
    original = copy.deepcopy(messages)
    assert _bound_tool_messages(messages, 30000) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("category", CATEGORIES)
async def test_bounded_rules_remain_retrievable_after_repeated_compaction(category):
    session = large_session()
    arguments = {"category": category}
    messages = _bound_tool_messages(rule_messages(session, arguments), 2000)
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    summary = await compact(session, [("consult_rules", arguments, messages[-1]["content"])])
    summary = await compact(session, [], previous_summary=summary)
    assert "[剧本机制检索]" in summary
    assert f'"category": "{category}"' in summary
    for index in range(5):
        assert f'"rule_index": {index}' in summary
    assert '"next_offset": 5' in summary
    assert "offset=0" in summary
    assert "SYNTHETIC_RULE_BUDGET_BODY" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [4, 9])
async def test_real_agent_budget_rereads_declaration_before_proposing_its_action(value):
    session = large_session(value)
    original_state = copy.deepcopy(session.current_state)
    context = {"old_time": session.current_state["game_time"]}
    requests = []
    chunks = []
    decoded_rule = None
    tools = _unified_tools()
    budget = min(30000, max(20000, 40000 - len(SYSTEM) - len(json.dumps(tools, ensure_ascii=False))))

    async def respond(messages, **kwargs):
        nonlocal decoded_rule
        requests.append(copy.deepcopy(messages))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        if len(requests) == 1:
            return {"content": "", "tool_calls": [
                model_call("consult_rules", {"category": category}, f"test_catalog_{index}")
                for index, category in enumerate(CATEGORIES)
            ]}
        if len(requests) == 2:
            calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
            results = {message["tool_call_id"]: json.loads(message["content"])
                       for message in messages if message["role"] == "tool"}
            assert calls == set(results) and len(results) == len(CATEGORIES)
            preview = results["test_catalog_0"]
            assert preview.get("truncated") is True, preview
            assert "offset=0" in preview["notice"]
            arguments = {
                "category": preview["category"], "rule_index": preview["entries"][2]["rule_index"], "offset": 0,
                "snapshot": preview["entries"][2]["snapshot"],
            }
        elif decoded_rule is not None:
            assert "变量候选值" in messages[-1]["content"]
            return {"content": "Synthetic action proposed after reading its declaration.", "tool_calls": []}
        else:
            result = json.loads(messages[-1]["content"])
            entry, = result["entries"]
            assert entry["offset"] == sum(map(len, chunks))
            chunks.append(entry["content"])
            if entry["next_offset"] is None:
                decoded_rule = json.loads("".join(chunks))
                return {"content": "", "tool_calls": [model_call(
                    "update_state", decoded_rule["definition"]["node"]["action_rule"], "test_declared_action",
                )]}
            arguments = {
                "category": result["category"], "rule_index": entry["rule_index"], "offset": entry["next_offset"],
                "snapshot": entry["snapshot"],
            }
        return {"content": "", "tool_calls": [model_call("consult_rules", arguments, f"test_reread_{len(requests)}")]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": "Inspect the catalogs, then read and follow the third story declaration."}],
        SYSTEM, tools, lambda name, arguments: session._dispatch_unified_tool(context, name, arguments),
        max_rounds=12, result_holder=holder,
    )]
    assert not any(event.get("type") == "error" for event in events), events
    assert decoded_rule is not None
    assert len(chunks) >= 3
    assert context["_accepted_state_updates"] == [decoded_rule["definition"]["node"]["action_rule"]]
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert session.script_variables.get(projected, "test_variables_2") == value
    assert session.current_state == original_state
    assert holder.text == "Synthetic action proposed after reading its declaration."


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [4, 9])
async def test_budgeted_rule_reading_settles_once_and_restores_without_public_rule_text(tmp_path, monkeypatch, value):
    session = large_session(value)
    session.save_id = "synthetic-rule-budget-save"
    session.script["locations"] = [{"id": "test_room", "name": "Reading room"}]
    session.current_state.update({
        "_pipeline_mode": "agentic_unified", "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    })
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    phase = "plan"
    requests = []
    chunks = []
    declaration = None

    async def respond(messages, **kwargs):
        nonlocal phase, declaration
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [model_call("extract_notes", {"notes": []}, "test_notes")]}
        requests.append(copy.deepcopy(messages))
        overhead = len(kwargs["system"]) + len(json.dumps(kwargs["tools"], ensure_ascii=False))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= min(30000, max(20000, 40000 - overhead))
        if phase == "plan":
            phase = "catalog"
            name, arguments = "submit_plan", {"plan": "Inspect the shelf according to the declared rules."}
        elif phase == "catalog":
            phase = "preview"
            return {"content": "", "tool_calls": [
                model_call("consult_rules", {"category": category}, f"test_stream_catalog_{index}")
                for index, category in enumerate(CATEGORIES)
            ]}
        elif phase == "preview":
            results = {message["tool_call_id"]: json.loads(message["content"])
                       for message in messages if message.get("tool_call_id", "").startswith("test_stream_catalog_")}
            assert len(results) == len(CATEGORIES)
            preview = results["test_stream_catalog_0"]
            assert preview["truncated"] is True
            assert "offset=0" in preview["notice"]
            phase = "read"
            name, arguments = "consult_rules", {
                "category": preview["category"], "rule_index": preview["entries"][2]["rule_index"], "offset": 0,
                "snapshot": preview["entries"][2]["snapshot"],
            }
        elif phase == "read":
            result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
            entry, = result["entries"]
            assert entry["offset"] == sum(map(len, chunks))
            chunks.append(entry["content"])
            if entry["next_offset"] is None:
                declaration = json.loads("".join(chunks))["definition"]["node"]["action_rule"]
                phase = "updated"
                name, arguments = "update_state", declaration
            else:
                name, arguments = "consult_rules", {
                    "category": result["category"], "rule_index": entry["rule_index"], "offset": entry["next_offset"],
                    "snapshot": entry["snapshot"],
                }
        elif phase == "updated":
            assert "变量候选值" in next(message["content"] for message in reversed(messages) if message["role"] == "tool")
            phase = "narrated"
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert phase == "narrated"
            phase = "finished"
            name, arguments = "finalize_turn", {"summary": "Inspected the synthetic shelf."}
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [model_call(name, arguments, f"test_stream_step_{len(requests)}")]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, root_state["game_time"], 0, state_snapshot=root_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rule-budget.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    await database.init_db()
    restored = None
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)", (
                session.script["script_id"], session.script["name"], json.dumps(session.script),
            ))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, session.script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": narrative})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                      "stream_id": "test_rule_budget_stream", "action_id": "test_rule_budget_action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
            assert response.status_code == 200
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
            final, = [event for event in events if event["type"] == "final"]
            assert phase == "finished" and len(chunks) >= 3
            assert final["narrative"] == narrative
            assert declaration["variable_changes"][0]["value"] == value
            assert session.current_state["script_variables"]["test_variables_2"] == value
            assert "SYNTHETIC_RULE_BUDGET_BODY" in json.dumps(requests)
            assert "SYNTHETIC_RULE_BUDGET_BODY" not in response.text
            await session._drain_background_tasks()
            calls = provider.generate_with_tools.await_count
            replay = await client.post(endpoint, json=action)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == calls
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            assert restored.current_state["script_variables"]["test_variables_2"] == value
            trace = restored.world_tree.get_node(final["node_id"])["agent_trace"]
            assert trace
            assert sum(record["tool"] == "update_state" for record in trace["rounds"]) == 1
            assert "SYNTHETIC_RULE_BUDGET_BODY" not in json.dumps(trace)
    finally:
        await session._drain_background_tasks()
        if restored is not None:
            await restored._drain_background_tasks()
        await database.close_pool()
