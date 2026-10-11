import asyncio
import copy
import json
import re
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from test_agent_rule_context import model_call


@pytest.mark.asyncio
@pytest.mark.parametrize("compact_after_search", [False, True])
@pytest.mark.parametrize("value,subject,topic", [
    (4, "synthetic_archive", "filing"),
    (9, "合成资料索引", "存放"),
])
async def test_focused_current_message_chain_settles_and_restores_without_collecting_body(
    tmp_path, monkeypatch, value, subject, topic, compact_after_search,
):
    declaration_id = f"test_focus_declaration_{value}"
    filler = "SYNTHETIC_FOCUS_BODY unrelated inventory record.\n" * 1000
    content = f"{subject}: table of contents.\n{filler}{topic}: historical heading.\n{filler}"
    content += f"{subject} {topic}: relevant procedure.\n" + "Synthetic section detail.\n" * 24
    content += f"FOLLOW_UP_ENTRY={declaration_id}\n" + "Synthetic appendix.\n" * 100
    assert len(content) > 60000
    noise_entries = [{"id": f"test_noise_{index}", "keys": [f"test_noise_{index}"], "visibility": "hidden",
                      "content": "SYNTHETIC_COMPACTION_NOISE " * 120} for index in range(18)] if compact_after_search else []
    session = GameSession({
        "script_id": "synthetic-lore-focus", "name": "Synthetic lore focus", "settings": {},
        "variables": [{"id": "test_counter", "type": "number", "default": 0}],
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "lorebook": [
            {"id": "test_manual", "keys": [subject], "content": content, "visibility": "hidden"},
            {"id": declaration_id, "keys": [declaration_id], "visibility": "hidden", "content": json.dumps({
                "variable_changes": [{"id": "test_counter", "op": "inc", "value": value}],
            })},
        ] + noise_entries,
    }, None)
    session.save_id = "synthetic-lore-focus-save"
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "_pipeline_mode": "agentic_unified",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = []

    async def respond(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [model_call("extract_notes", {"notes": []}, "test_notes")]}
        requests.append(copy.deepcopy(messages))
        overhead = len(kwargs["system"]) + len(json.dumps(kwargs["tools"], ensure_ascii=False))
        budget = min(30000, max(20000, 40000 - overhead))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        previous = next((message for message in reversed(messages) if message["role"] == "tool"), None)
        if previous is None:
            name, arguments, call_id = "submit_plan", {"plan": "Read the declared archive procedure."}, "test_focus_plan"
        elif previous["tool_call_id"] == "test_focus_plan":
            user_text = "\n".join(message["content"] for message in messages if message["role"] == "user")
            keyword = re.search(r"SEARCH_TERMS=([^\n]+)", user_text).group(1).strip()
            name, arguments, call_id = "query_lorebook", {"keyword": keyword}, "test_focus_search"
        elif previous["tool_call_id"] == "test_focus_search" and compact_after_search:
            return {"content": "", "tool_calls": [model_call("query_lorebook", {
                "entry_ids": [f"test_noise_{index}" for index in range(start, start + 3)],
            }, f"test_focus_noise_{start // 3}") for start in range(0, 18, 3)]}
        elif previous["tool_call_id"] in {"test_focus_search", "test_focus_noise_5"}:
            if previous["tool_call_id"] == "test_focus_noise_5":
                assert not any(message.get("tool_call_id") == "test_focus_search" for message in messages)
                prefix = "[世界书搜索进度] "
                progress, = [json.loads(line.removeprefix(prefix)) for message in messages
                             if message["role"] == "user" for line in message["content"].splitlines()
                             if line.startswith(prefix)]
                entry, = progress["entries"]
                assert progress["content_omitted"] is True and "content" not in entry
            else:
                entry, = json.loads(previous["content"])["entries"]
            assert entry["offset"] > 40000 and entry["total_length"] > 2 * budget
            assert "FOLLOW_UP_ENTRY=" not in entry.get("content", "")
            name, call_id = "query_lorebook", "test_focus_read"
            arguments = {"entry_id": entry["id"], "offset": entry["offset"], "snapshot": entry["snapshot"]}
        elif previous["tool_call_id"] == "test_focus_read":
            entry, = json.loads(previous["content"])["entries"]
            assert entry["offset"] > 40000 and entry["next_offset"] is not None
            target = re.search(r"FOLLOW_UP_ENTRY=([^\n]+)", entry["content"]).group(1)
            name, arguments, call_id = "query_lorebook", {"entry_id": target}, "test_focus_related"
        elif previous["tool_call_id"] == "test_focus_related":
            entry, = json.loads(previous["content"])["entries"]
            assert entry["truncated"] is False
            name, arguments, call_id = "update_state", json.loads(entry["content"]), "test_focus_update"
        elif previous["tool_call_id"] == "test_focus_update":
            assert "变量候选值" in previous["content"]
            name, arguments, call_id = "write_narrative", {"text": narrative}, "test_focus_narrative"
        else:
            assert previous["tool_call_id"] == "test_focus_narrative"
            name, arguments, call_id = "finalize_turn", {"summary": "Inspected the synthetic shelf."}, "test_focus_final"
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [model_call(name, arguments, call_id)]}

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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "lore-focus.db")
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
            action = {"type": "freeform", "text": f"Inspect the empty shelf.\nSEARCH_TERMS={subject} {topic}\n",
                      "intent_hint": "explicit_action", "stream_id": "test_focus_stream",
                      "action_id": "test_focus_action", "expected_node_id": root_id}
            response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
            assert response.status_code == 200
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative and len(requests) == 7 + int(compact_after_search)
            assert session.current_state["script_variables"]["test_counter"] == value
            observed = {message["tool_call_id"]: json.loads(message["content"])
                        for messages in requests for message in messages
                        if message.get("tool_call_id") in {"test_focus_search", "test_focus_read", "test_focus_related"}}
            assert len(observed) == 3
            entries = [result["entries"][0] for result in observed.values()]
            assert sum(len(entry["content"]) for entry in entries) < 3000
            assert "FOLLOW_UP_ENTRY=" in json.dumps(requests)
            assert "FOLLOW_UP_ENTRY=" not in response.text and "SYNTHETIC_FOCUS_BODY" not in response.text
            assert "SYNTHETIC_COMPACTION_NOISE" not in response.text
            assert all(entry["snapshot"] not in response.text for entry in entries)
            await session._drain_background_tasks()
            calls = provider.generate_with_tools.await_count
            replay = await client.post(endpoint, json=action)
            assert replay.text == response.text and provider.generate_with_tools.await_count == calls
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            assert restored.current_state["script_variables"]["test_counter"] == value
            trace = restored.world_tree.get_node(final["node_id"])["agent_trace"]
            assert sum(record["tool"] == "update_state" for record in trace["rounds"]) == 1
            assert "FOLLOW_UP_ENTRY=" not in json.dumps(trace) and "SYNTHETIC_FOCUS_BODY" not in json.dumps(trace)
            assert "SYNTHETIC_COMPACTION_NOISE" not in json.dumps(trace)
            assert all(entry["snapshot"] not in json.dumps(trace) for entry in entries)
            await restored.branch_to_node(root_id)
            assert restored.current_state["script_variables"]["test_counter"] == 0
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["script_variables"]["test_counter"] == value
    finally:
        await session._drain_background_tasks()
        if restored is not None:
            await restored._drain_background_tasks()
        await database.close_pool()
