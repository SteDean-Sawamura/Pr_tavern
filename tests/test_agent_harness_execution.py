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


def _stream_events(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lore_effect", ["activate_lore", "add_lore", "update_lore"])
async def test_retrieval_settlement_stream_and_reload(tmp_path, monkeypatch, lore_effect):
    script = {
        "script_id": "synthetic-harness", "name": "Synthetic harness", "settings": {},
        "locations": [
            {"id": "test_atrium", "name": "Atrium"},
            {"id": "test_archive", "name": "Archive", "travel_time": "PT5M"},
        ],
        "variables": [{
            "id": "test_capacity", "type": "number", "default": 4, "min": 0, "max": 9,
            "description": "SYNTHETIC_MECHANICS_PRIVATE",
        }],
        "lorebook": [
            {"id": "test_label", "keys": ["shelf label"], "enabled": False,
             "visibility": "world", "content": "The shelf label reads LANTERN."},
            {"id": "test_future", "keys": ["later"], "enabled": False,
             "visibility": "secret", "content": "SYNTHETIC_UNREAD_DETAILS"},
        ],
        "one_time_events": [{
            "id": "test_arrival", "trigger_time": "2030-01-01T09:05:00",
            "condition": "player.location == test_archive",
            "effects": [{"type": "activate_lore", "id": "test_label"}],
        }],
    }
    if lore_effect != "activate_lore":
        revealed_entry = {**script["lorebook"][0], "enabled": True}
        if lore_effect == "add_lore":
            script["lorebook"] = script["lorebook"][1:]
            effect = {"type": "add_lore", "entry": revealed_entry}
        else:
            script["lorebook"][0].update({"enabled": True, "content": "The shelf label is blank."})
            effect = {"type": "update_lore", **revealed_entry}
        script["one_time_events"][0]["effects"] = [effect]
    script["one_time_events"][0]["effects"].extend([
        {"type": "add_variable", "variable": {
            "id": "test_visit_count", "type": "number", "default": 2, "min": 0, "max": 10,
        }},
        {"type": "add_trigger", "trigger": {
            "id": "test_visit_hook", "event": "test_visit_tick", "action": "set_var",
            "params": {"var_id": "test_visit_count", "op": "inc", "value": 1},
        }},
        {"type": "add_random_item", "item": {
            "id": "test_visit_roll", "dice": "1d1", "trigger_type": "per_turn",
        }},
    ])
    narrative = (
        "You leave the Atrium and reach the Archive after five minutes. "
        "Inside, you inspect the shelf without disturbing its contents. "
        "The shelf label reads LANTERN. You make a note of the label and remain in the room."
    )
    operations = [
        ("submit_plan", {"plan": "Choose a known room, then inspect its contents.",
                         "questions": [{"question": "Which room?", "options": ["Archive", "Atrium"]}]}),
        ("update_state", {"location_change": "test_archive", "end_time": "2030-01-01T09:05:00"}),
        ("consult_rules", {"category": "variables", "query": "test_capacity"}),
        ("consult_rules", {"category": "variables", "query": "test_visit_count"}),
        ("query_lorebook", {"entry_id": "test_label"}),
        ("write_narrative", {"text": narrative}),
        ("finalize_turn", {"summary": "Reached the Archive and read the shelf label."}),
    ]
    requests = []
    waiting = asyncio.Event()
    retrieved = asyncio.Event()
    release_narrative = asyncio.Event()

    async def generate_with_tools(messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        completed = {message.get("tool_call_id") for message in messages if message["role"] == "tool"}
        remaining = [(index, operation) for index, operation in enumerate(operations)
                     if f"synthetic-call-{index}" not in completed]
        assert remaining, json.dumps(messages[-4:], ensure_ascii=False)
        index, (name, arguments) = remaining[0]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        if name == "write_narrative":
            retrieved.set()
            await release_narrative.wait()
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-call-{index}", "name": name, "arguments": arguments,
        }]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location", "clue"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session = GameSession(script, provider, save_id="synthetic-harness-save")
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_atrium"},
        "visible_locations": ["test_atrium", "test_archive"],
        "display_names": {"test_atrium": "Atrium", "test_archive": "Archive"},
    }
    session.script_variables.init_state(session.current_state)
    root_id = session.world_tree.add_node(
        None, session.current_state["game_time"], 0,
        ai_response="You are in the Atrium. The Archive is accessible.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    append = game_routes._ActionStreamJournal.append

    def observe(journal, text):
        append(journal, text)
        if any(event["type"] == "agent_waiting" for event in _stream_events(text)):
            waiting.set()

    monkeypatch.setattr(game_routes._ActionStreamJournal, "append", observe)
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    action = {
        "type": "freeform", "text": "Inspect a nearby room.", "intent_hint": "explicit_action",
        "stream_id": "synthetic-stream", "action_id": "synthetic-action", "expected_node_id": root_id,
    }
    endpoint = f"/api/game/{session.save_id}"
    pending = None
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": session.current_state})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            pending = asyncio.create_task(client.post(f"{endpoint}/action/stream", json=action))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=5)
                assert provider.generate_with_tools.await_count == 1
                assert session.current_state["player"]["location"] == "test_atrium"
                assert session.world_tree.active_node_id == root_id
                async with database.get_db() as db:
                    plan = await (await db.execute(
                        "SELECT * FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert plan["base_node_id"] == root_id
                assert plan["action_id"] == action["action_id"]
                assert json.loads(plan["request"]) == game_routes.ActionRequest(**action).model_dump()
                assert plan["reply"] is None
                answered = await client.post(f"{endpoint}/inject", json={
                    "stream_id": action["stream_id"], "plan_id": plan["plan_id"], "message": "Archive",
                })
                assert answered.status_code == 200
                await asyncio.wait_for(retrieved.wait(), timeout=5)
                assert session.current_state["player"]["location"] == "test_atrium"
                assert session.current_state["game_time"] == "2030-01-01T09:00:00"
                assert "test_visit_count" not in session.script_variables.definitions
                assert not session.trigger_engine.triggers
                assert not session.script.get("random_items")
                async with database.get_db() as db:
                    plan_reply = await (await db.execute(
                        "SELECT reply FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                    node_count = await (await db.execute(
                        "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert plan_reply["reply"] == "Archive"
                assert node_count[0] == 1
                interjection = "Read the shelf label without taking anything."
                injected = await client.post(f"{endpoint}/inject", json={
                    "stream_id": action["stream_id"], "message": interjection,
                })
                assert injected.status_code == 200
                release_narrative.set()
                response = await asyncio.wait_for(asyncio.shield(pending), timeout=5)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                events = _stream_events(response.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert final["node_id"] != root_id
                assert final["state"]["player"]["location"] == "test_archive"
                assert final["state"]["game_time"] == "2030-01-01T09:05:00"
                assert "SYNTHETIC_UNREAD_DETAILS" not in response.text
                assert "SYNTHETIC_MECHANICS_PRIVATE" not in response.text
                assert "lorebook_entry_overrides" not in response.text
                assert "mechanics_overrides" not in response.text
                assert "_dyn_test_visit_hook" not in response.text
                assert any(event["type"] == "user_inject" and event["message"] == interjection for event in events)
                assert any({"role": "user", "content": interjection} in messages for messages in requests)
                assert any("Archive" in message.get("content", "") for message in requests[1] if message["role"] == "user")
                assert "SYNTHETIC_MECHANICS_PRIVATE" not in json.dumps(requests[0])
                for index, (name, arguments) in enumerate(operations):
                    if name != "consult_rules":
                        continue
                    rule_messages = [message for messages in requests for message in messages
                                     if message.get("tool_call_id") == f"synthetic-call-{index}"]
                    assert rule_messages
                    result = json.loads(rule_messages[0]["content"])
                    assert result["source"] == "current_branch"
                    assert result["game_time"] == "2030-01-01T09:00:00"
                    if arguments["query"] == "test_capacity":
                        payload = json.loads(result["entries"][0]["content"])
                        assert payload["definition"] == script["variables"][0]
                        assert payload["current_value"] == 4
                    else:
                        assert result["entries"] == [] and result["total"] == 0
                for event in events:
                    if event["type"] in ("tool_started", "tool_call"):
                        assert set(event) <= {"type", "label", "round", "tool_name"}
                tool_results = [json.loads(message["content"]) for messages in requests for message in messages
                                if message["role"] == "tool" and message.get("content", "").startswith('{"entries"')]
                assert any(result["entries"][0]["id"] == "test_label"
                           and result["projection"]["event_ids"] == ["test_arrival"]
                           and result["projection"]["provisional"] for result in tool_results if result["entries"])
                calls_before_replay = provider.generate_with_tools.await_count
                replay = await client.post(f"{endpoint}/action/stream", json=action)
                assert replay.status_code == 200
                assert _stream_events(replay.text) == events
                assert provider.generate_with_tools.await_count == calls_before_replay
            finally:
                release_narrative.set()
                if pending is not None and not pending.done():
                    await client.post(f"{endpoint}/abort", json={"stream_id": action["stream_id"]})
                    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5)
        await session._drain_background_tasks()
        async with database.get_db() as db:
            counts = {}
            for table in ("tree_nodes", "pending_action_plans", "pending_action_injections"):
                row = await (await db.execute(f"SELECT COUNT(*) FROM {table} WHERE save_id = ?", (session.save_id,))).fetchone()
                counts[table] = row[0]
        assert counts == {"tree_nodes": 2, "pending_action_plans": 0, "pending_action_injections": 0}
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        assert restored.world_tree.active_node_id == final["node_id"]
        assert restored.current_state["player"]["location"] == "test_archive"
        assert restored.current_state["game_time"] == "2030-01-01T09:05:00"
        assert restored.current_state["script_variables"]["test_visit_count"] == 2
        saved_mechanics = {
            category: json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"]
            for category in ("variables", "triggers", "random_items")
        }
        assert all(len(entries) == 1 for entries in saved_mechanics.values())
        assert "_dyn_test_visit_roll" in restored._random_item_by_id
        restored._fire_event_dual("test_visit_tick")
        assert restored.current_state["script_variables"]["test_visit_count"] == 3
        restored.script_variables.set(restored.current_state, "test_visit_count", 50)
        assert restored.current_state["script_variables"]["test_visit_count"] == 10
        node = restored.world_tree.get_node(final["node_id"])
        assert node["ai_response"] == narrative
        trace = node["agent_trace"]
        assert "SYNTHETIC_MECHANICS_PRIVATE" not in json.dumps(trace)
        assert [round_record["tool"] for round_record in trace["rounds"]] == [name for name, _ in operations]
        assert trace["plan_waits"] == 1
        assert trace["lore_queries"] == trace["lore_hits"] == trace["lore_entry_reads"] == 1
        assert trace["semantic_review_status"] == "passed"
        assert [(entry["message"], entry["plan_reply"]) for entry in trace["user_injections"]] == [
            ("Archive", True), (interjection, False),
        ]
        lore = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert [entry["id"] for entry in lore["entries"]] == ["test_label"]
        assert lore["entries"][0]["content"] == "The shelf label reads LANTERN."
        repeated = []
        restored._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", repeated)
        assert repeated == []
        branch = await restored.branch_to_node(root_id)
        assert branch["node_id"] == root_id
        assert "test_visit_count" not in restored.script_variables.definitions
        assert not restored.trigger_engine.triggers
        assert not restored._random_item_by_id
        for category in saved_mechanics:
            assert json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"] == []
        previous = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert [entry["content"] for entry in previous["entries"]] == (
            ["The shelf label is blank."] if lore_effect == "update_lore" else []
        )
        branch = await restored.branch_to_node(final["node_id"])
        assert branch["node_id"] == final["node_id"]
        for category, entries in saved_mechanics.items():
            assert json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"] == entries
        assert restored.current_state["script_variables"]["test_visit_count"] == 2
        assert "_dyn_test_visit_roll" in restored._random_item_by_id
        restored._fire_event_dual("test_visit_tick")
        assert restored.current_state["script_variables"]["test_visit_count"] == 3
        current = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert current["entries"] == lore["entries"]
    finally:
        release_narrative.set()
        await database.close_pool()
