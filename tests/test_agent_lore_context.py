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
from engine.lorebook import Lorebook
from engine.session.agentic_mixin import _AgentResult, _bound_tool_messages, _bounded_tool_content
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import model_call, tool_call
from test_agent_state_compaction import compact, lore_query_statuses


SYSTEM = "Read the synthetic archive before following its related declaration."
BODY = 'SYNTHETIC_LORE_BUDGET_BODY 合成约束 "quoted" \\path\\\n' * 60


def large_session(value=7, visibility="hidden"):
    target_id = f"test_declaration_{value}"
    session = GameSession({
        "script_id": "synthetic-lore-budget", "name": "Synthetic lore budget", "settings": {},
        "variables": [{"id": "test_counter", "type": "number", "default": 0}],
        "lorebook": [
            {"id": f"test_lore_{index}", "keys": [f"synthetic_archive_{index}"],
             "content": json.dumps({"notes": BODY, "clue": "合成线索 石门；铜锁；河边；青灯"}, ensure_ascii=False),
             "visibility": visibility, "related_entries": [target_id]}
            for index in range(12)
        ] + [{"id": target_id, "keys": [target_id], "visibility": "hidden", "content": json.dumps({
            "variable_changes": [{"id": "test_counter", "op": "set", "value": value}],
        })}],
    }, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


def lore_messages(arguments, result):
    return [
        {"role": "user", "content": "Inspect the synthetic archive."},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("query_lorebook", arguments, "test_lore")]},
        {"role": "tool", "tool_call_id": "test_lore", "content": json.dumps(result, ensure_ascii=False)},
    ]


@pytest.mark.parametrize("visibility", ["public", "world", "hidden"])
@pytest.mark.parametrize("mode", ["batch", "continuation", "search", "search_page", "approximate"])
def test_bounded_lore_preserves_retrieval_metadata_and_requires_complete_reread(visibility, mode):
    session = large_session(visibility=visibility)
    arguments = {
        "batch": {"entry_ids": ["test_lore_0", "test_lore_1", "test_lore_2"]},
        "continuation": {"entry_id": "test_lore_0", "offset": 2000},
        "search": {"keyword": "合成线索"},
        "search_page": {"keyword": "合成线索", "page": 1},
        "approximate": {"keyword": "石门铜锁河边青灯"},
    }[mode]
    original = session.prompt_builder.lorebook.query(**arguments)
    if mode == "approximate":
        assert original["approximate"] is True
    if "keyword" in arguments:
        assert original["next_page"] is not None
        assert all(entry["offset"] > 0 for entry in original["entries"])
    messages = lore_messages(arguments, original)
    budget = 2000 if mode == "continuation" else 3200
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) > budget - 1000
    bounded = _bound_tool_messages(messages, budget)
    result = json.loads(bounded[-1]["content"])
    assert result["content_omitted"] is True
    assert "offset=0" in result["notice"]
    for key in ("total_matches", "next_page", "approximate"):
        assert result.get(key) == original.get(key)
    assert len(result["entries"]) == len(original["entries"])
    for entry, previous in zip(result["entries"], original["entries"]):
        assert "content" not in entry
        for key in ("id", "visibility", "offset", "next_offset", "total_length", "truncated", "related_entries"):
            assert entry[key] == previous[key]
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in bounded) <= budget
    assert len(json.dumps(bounded[-1], ensure_ascii=False)) <= budget - 1000


@pytest.mark.parametrize("arguments", [{"entry_id": "test_lore_0"}, {"entry_id": "test_missing"},
                                      {"entry_id": "test_lore_0", "offset": 100000}])
def test_lore_within_budget_including_missing_and_eof_is_unchanged(arguments):
    result = large_session().prompt_builder.lorebook.query(**arguments)
    messages = lore_messages(arguments, result)
    original = copy.deepcopy(messages)
    assert _bound_tool_messages(messages, 30000) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["partial_batch", "search", "projection"])
async def test_bounded_lore_repeated_compaction_keeps_query_and_unread_status(mode):
    session = large_session()
    arguments = ({"keyword": "合成线索", "page": 1} if mode == "search"
                 else {"entry_ids": ["test_lore_0", "test_missing", "test_lore_1"]})
    context = {"old_time": session.current_state["game_time"]}
    if mode == "projection":
        context["_accepted_state_updates"] = [{"variable_changes": [{"id": "test_counter", "op": "set", "value": 3}]}]
    state = copy.deepcopy(session.current_state)
    original = json.loads(await session._query_lorebook_for_turn(context, arguments))
    messages = _bound_tool_messages(lore_messages(arguments, original), 3200)
    bounded = json.loads(messages[-1]["content"])
    assert bounded["content_omitted"] is True
    if mode == "projection":
        assert bounded["projection"] == original["projection"]
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    summary = await compact(session, [("query_lorebook", arguments, messages[-1]["content"])])
    summary = await compact(session, [], previous_summary=summary)
    prefix = "[世界书正文省略] "
    record, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert record["query"] == arguments
    assert record["content_omitted"] is True
    assert "offset=0" in record["notice"]
    assert record["entries"] == bounded["entries"]
    assert record["total_matches"] == original["total_matches"]
    assert record["next_page"] == original["next_page"]
    if mode == "projection":
        assert record["projection"] == original["projection"]
    if mode != "search":
        status, = lore_query_statuses(summary)
        assert status["returned_ids"] == ["test_lore_0", "test_lore_1"]
        assert status["not_returned_ids"] == ["test_missing"]
    assert "SYNTHETIC_LORE_BUDGET_BODY" not in summary
    assert session.current_state == state


@pytest.mark.parametrize("large_field", ["title", "related_entries", "id", "projection"])
def test_oversized_lore_metadata_remains_structured_without_claiming_no_results(large_field):
    result = large_session().prompt_builder.lorebook.query(entry_id="test_lore_0")
    if large_field == "projection":
        result["projection"] = {"provisional": True, "event_ids": ["test_event_" + BODY]}
    elif large_field == "related_entries":
        result["entries"][0][large_field] = ["test_related_" + BODY]
    else:
        result["entries"][0][large_field] = BODY
    bounded = _bounded_tool_content(json.dumps(result, ensure_ascii=False), "query_lorebook", 800)
    preview = json.loads(bounded)
    assert len(bounded) <= 800
    assert preview["content_omitted"] is True
    assert "offset=0" in preview["notice"]
    if "projection" in result:
        assert preview["projection"]["provisional"] is True
    if "entries" not in preview:
        assert preview["metadata_omitted"] is True
        assert "未返回" in preview["notice"]
    else:
        assert [entry["id"] for entry in preview["entries"]] == [entry["id"] for entry in result["entries"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"entry_ids": ["test_lore_0", "test_missing"]},
                                      {"keyword": "synthetic_catalog", "page": 0}])
async def test_omitted_metadata_survives_compaction_without_inventing_missing_entries(arguments):
    session = large_session()
    related_id = "test_related_" + "long_identifier_" * 200
    lorebook = Lorebook([
        {"id": "test_lore_0", "keys": ["synthetic_catalog"], "content": BODY, "related_entries": [related_id]},
        {"id": related_id, "keys": ["synthetic_related"], "content": "Synthetic related declaration."},
    ])
    result = lorebook.query(**arguments)
    messages = _bound_tool_messages(lore_messages(arguments, result), 2000)
    preview = json.loads(messages[-1]["content"])
    assert preview["metadata_omitted"] is True and "entries" not in preview
    assert len(json.dumps(messages[-1], ensure_ascii=False)) <= 1000
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    summary = await compact(session, [("query_lorebook", arguments, messages[-1]["content"])])
    summary = await compact(session, [], previous_summary=summary)
    prefix = "[世界书正文省略] "
    record, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert record["query"] == arguments
    assert record["metadata_omitted"] is True and "entries" not in record
    assert "offset=0" in record["notice"]
    assert lore_query_statuses(summary) == []
    assert "SYNTHETIC_LORE_BUDGET_BODY" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [4, 9])
async def test_real_agent_budget_rereads_then_discovers_related_declaration(value):
    session = large_session(value)
    original_state = copy.deepcopy(session.current_state)
    context = {"old_time": session.current_state["game_time"]}
    requests = []
    chunks = []
    target_id = None
    declaration = None
    tools = _unified_tools()
    budget = min(30000, max(20000, 40000 - len(SYSTEM) - len(json.dumps(tools, ensure_ascii=False))))

    async def respond(messages, **kwargs):
        nonlocal target_id, declaration
        requests.append(copy.deepcopy(messages))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        if len(requests) == 1:
            return {"content": "", "tool_calls": [model_call("query_lorebook", {
                "entry_ids": [f"test_lore_{index}" for index in range(start, start + 3)],
            }, f"test_catalog_{start}") for start in range(0, 12, 3)]}
        if len(requests) == 2:
            results = {message["tool_call_id"]: json.loads(message["content"])
                       for message in messages if message["role"] == "tool"}
            calls = {call["id"] for message in messages for call in message.get("tool_calls", [])}
            assert calls == set(results)
            assert len(results) == 4
            preview = results["test_catalog_0"]
            assert preview["content_omitted"] is True
            assert "offset=0" in preview["notice"]
            arguments = {"entry_id": preview["entries"][0]["id"], "offset": 0}
        elif declaration is not None:
            assert "变量候选值" in messages[-1]["content"]
            return {"content": "Synthetic action proposed after following the archive link.", "tool_calls": []}
        else:
            result = json.loads(messages[-1]["content"])
            entry, = result["entries"]
            if target_id is not None:
                assert entry["id"] == target_id
                declaration = json.loads(entry["content"])
                return {"content": "", "tool_calls": [model_call("update_state", declaration, "test_declared_update")]}
            assert entry["offset"] == sum(map(len, chunks))
            chunks.append(entry["content"])
            if entry["next_offset"] is None:
                assert json.loads("".join(chunks))["notes"] == BODY
                target_id, = entry["related_entries"]
                arguments = {"entry_id": target_id, "offset": 0}
            else:
                arguments = {"entry_id": entry["id"], "offset": entry["next_offset"]}
        return {"content": "", "tool_calls": [model_call("query_lorebook", arguments, f"test_read_{len(requests)}")]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": "Inspect the archives and follow the first entry's related declaration."}],
        SYSTEM, tools, lambda name, arguments: session._dispatch_unified_tool(context, name, arguments),
        max_rounds=12, result_holder=holder,
    )]
    assert not any(event.get("type") == "error" for event in events), events
    assert len(chunks) >= 2 and declaration is not None
    assert context["_accepted_state_updates"] == [declaration]
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert session.script_variables.get(projected, "test_counter") == value
    assert session.current_state == original_state
    assert holder.text == "Synthetic action proposed after following the archive link."


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [4, 9])
async def test_budgeted_lore_chain_settles_once_restores_and_keeps_body_out_of_public_stream(tmp_path, monkeypatch, value):
    session = large_session(value)
    session.save_id = "synthetic-lore-budget-save"
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
    target_id = None
    declaration = None

    async def respond(messages, **kwargs):
        nonlocal phase, target_id, declaration
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
            return {"content": "", "tool_calls": [model_call("query_lorebook", {
                "entry_ids": [f"test_lore_{index}" for index in range(start, start + 3)],
            }, f"test_stream_catalog_{start}") for start in range(0, 12, 3)]}
        elif phase == "preview":
            results = {message["tool_call_id"]: json.loads(message["content"])
                       for message in messages if message.get("tool_call_id", "").startswith("test_stream_catalog_")}
            assert len(results) == 4
            preview = results["test_stream_catalog_0"]
            assert preview["content_omitted"] is True
            assert "offset=0" in preview["notice"]
            phase = "read"
            name, arguments = "query_lorebook", {"entry_id": preview["entries"][0]["id"], "offset": 0}
        elif phase == "read":
            result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
            entry, = result["entries"]
            assert entry["offset"] == sum(map(len, chunks))
            chunks.append(entry["content"])
            if entry["next_offset"] is None:
                assert json.loads("".join(chunks))["notes"] == BODY
                target_id, = entry["related_entries"]
                assert not target_id.startswith("test_lore_")
                phase = "related"
                arguments = {"entry_id": target_id, "offset": 0}
            else:
                arguments = {"entry_id": entry["id"], "offset": entry["next_offset"]}
            name = "query_lorebook"
        elif phase == "related":
            result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
            entry, = result["entries"]
            assert entry["id"] == target_id
            declaration = json.loads(entry["content"])
            phase = "updated"
            name, arguments = "update_state", declaration
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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "lore-budget.db")
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
                      "stream_id": "test_lore_budget_stream", "action_id": "test_lore_budget_action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
            assert response.status_code == 200
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
            final, = [event for event in events if event["type"] == "final"]
            assert phase == "finished" and len(chunks) >= 2
            assert final["narrative"] == narrative
            assert declaration["variable_changes"][0]["value"] == value
            assert session.current_state["script_variables"]["test_counter"] == value
            assert "SYNTHETIC_LORE_BUDGET_BODY" in json.dumps(requests)
            assert "SYNTHETIC_LORE_BUDGET_BODY" not in response.text
            await session._drain_background_tasks()
            calls = provider.generate_with_tools.await_count
            replay = await client.post(endpoint, json=action)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == calls
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            assert restored.current_state["script_variables"]["test_counter"] == value
            trace = restored.world_tree.get_node(final["node_id"])["agent_trace"]
            assert trace
            assert sum(record["tool"] == "update_state" for record in trace["rounds"]) == 1
            assert "SYNTHETIC_LORE_BUDGET_BODY" not in json.dumps(trace)
    finally:
        await session._drain_background_tasks()
        if restored is not None:
            await restored._drain_background_tasks()
        await database.close_pool()
