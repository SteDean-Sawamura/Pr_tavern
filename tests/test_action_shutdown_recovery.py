import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client, stored_state


PLAN = {
    "plan": "Confirm how to inspect the empty room.",
    "questions": [{"question": "How should the room be inspected?", "options": ["Inspect", "Leave"]}],
}
NARRATIVE = (
    "You inspect the empty shelf beside the wall without moving anything. "
    "Its wooden surface is clean and dry, and there is nothing behind it. "
    "You step back and remain in the quiet room, leaving everything undisturbed."
)


async def read_until_waiting(response):
    chunks = []
    async with asyncio.timeout(5):
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if any(event["type"] == "agent_waiting" for event in _stream_events(chunk)):
                return _stream_events("".join(chunks))
    pytest.fail("The action ended before reaching its persisted plan gate")


async def plan_record(save_id):
    async with database.get_db() as connection:
        cursor = await connection.execute("SELECT * FROM pending_action_plans WHERE save_id = ?", (save_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_start", "waiting", "replied"])
async def test_shutdown_preserves_pending_action_and_resumes_once(tmp_path, monkeypatch, phase):
    script = {
        "script_id": "synthetic-shutdown", "name": "Synthetic shutdown", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "variables": [{"id": "test_visits", "type": "number", "default": 0}],
    }
    updated, release = asyncio.Event(), asyncio.Event()
    pause_after_update = phase == "replied"
    replies = []

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if names == {"classify_intent"}:
            name, arguments = "classify_intent", {"intent": "action"}
        elif names == {"extract_notes"}:
            name, arguments = "extract_notes", {"notes": []}
        else:
            completed = {message.get("tool_call_id") for message in messages if message.get("role") == "tool"}
            if "synthetic-submit_plan" not in completed:
                name, arguments = "submit_plan", copy.deepcopy(PLAN)
            elif "synthetic-update_state" not in completed:
                replies.append(next(message["content"] for message in reversed(messages)
                                    if message["role"] == "user"))
                name, arguments = "update_state", {
                    "end_time": "2030-01-01T09:01:00",
                    "variable_changes": [{"id": "test_visits", "op": "inc", "value": 1}],
                }
            elif "synthetic-write_narrative" not in completed:
                if pause_after_update:
                    updated.set()
                    await release.wait()
                name, arguments = "write_narrative", {"text": NARRATIVE}
            else:
                name, arguments = "finalize_turn", {"summary": "Inspected the room."}
        assert name in names
        return {"content": "", "tool_calls": [{"id": "synthetic-" + name, "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value=json.dumps({
        "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
        "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
        "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
        "context_depth": "normal", "recall_hints": [], "pass": True, "violations": [],
    })))
    session = GameSession(script, provider, save_id="synthetic-shutdown-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    owned_tasks, responses = set(), []

    async def start_action(request):
        response = await game_routes.game_action_stream(session.save_id, request)
        responses.append(response)
        owned_tasks.update(task for task in asyncio.all_tasks()
                           if task.get_coro().__qualname__ == "game_action_stream.<locals>.run_action")
        return response

    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        root_id = session.world_tree.active_node_id
        request = game_routes.ActionRequest(
            text="Choose how to inspect the room.", intent_hint="explicit_action", expected_node_id=root_id,
            action_id="synthetic-shutdown-action", stream_id="synthetic-shutdown-stream",
        )
        endpoint = f"/api/game/{session.save_id}"

        async def reply_to_plan():
            record = await plan_record(session.save_id)
            assert record and record["base_node_id"] == root_id and record["reply"] is None
            response = await client.post(f"{endpoint}/inject", json={
                "stream_id": request.stream_id, "plan_id": record["plan_id"], "message": "Inspect",
            })
            assert response.status_code == 200 and response.json()["queued"] is True

        try:
            response = await start_action(request)
            initial_tasks = set(owned_tasks)
            assert len(initial_tasks) == 1
            if phase != "before_start":
                await read_until_waiting(response)
                record = await plan_record(session.save_id)
                assert record and record["reply"] is None and record["base_node_id"] == root_id
                if phase == "replied":
                    await reply_to_plan()
                    await asyncio.wait_for(updated.wait(), 5)
                    assert session.current_state["script_variables"]["test_visits"] == 0
                    assert (await plan_record(session.save_id))["reply"] == "Inspect"
            await response.body_iterator.aclose()
            assert game_routes._get_session_lock(session.save_id).locked()
            async with asyncio.timeout(3):
                await game_routes._persist_sessions_on_shutdown()

            assert all(task.done() and task.cancelled() for task in initial_tasks)
            assert not game_routes._get_session_lock(session.save_id).locked()
            journal = game_routes._action_streams[(session.save_id, request.action_id)]
            assert journal.done and journal.failed
            assert not getattr(session, "_active_agent_stream", False)
            if phase == "before_start":
                assert not provider.generate_with_tools.called
            assert session.turn_number == 0 and session.world_tree.active_node_id == root_id
            assert session.current_state["game_time"] == script["start_time"]
            assert session.current_state["script_variables"]["test_visits"] == 0
            persisted = await stored_state(session)
            assert persisted["game_time"] == script["start_time"]
            assert persisted["script_variables"]["test_visits"] == 0

            await database.close_pool()
            game_routes._sessions.clear()
            game_routes._session_locks.clear()
            game_routes._action_streams.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None
            sessions.append(restored)
            record = await plan_record(session.save_id)
            if phase == "before_start":
                assert record is None
            else:
                assert record["reply"] == ("Inspect" if phase == "replied" else None)
                snapshot = await client.get(f"{endpoint}/snapshot")
                assert snapshot.status_code == 200
                assert snapshot.json()["pending_action"]["actionId"] == request.action_id
                assert snapshot.json()["pending_action"]["baseNodeId"] == root_id

            pause_after_update = False
            resumed = await start_action(request)
            events = []
            if phase != "replied":
                events.extend(await read_until_waiting(resumed))
                await reply_to_plan()
            async with asyncio.timeout(10):
                async for chunk in resumed.body_iterator:
                    events.extend(_stream_events(chunk))
            assert not any(event["type"] in {"error", "aborted"} for event in events), events
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == NARRATIVE
            if phase == "replied":
                assert not any(event["type"] == "agent_waiting" for event in events)
            assert replies and all(reply == "Inspect" for reply in replies)
            assert restored.turn_number == 1
            assert restored.current_state["game_time"] == "2030-01-01T09:01:00"
            assert restored.current_state["script_variables"]["test_visits"] == 1
            assert (await stored_state(restored))["script_variables"]["test_visits"] == 1
            assert await plan_record(session.save_id) is None
            async with database.get_db() as connection:
                for table, count in (("tree_nodes", 2), ("action_requests", 1), ("pending_action_injections", 0)):
                    cursor = await connection.execute(f"SELECT COUNT(*) AS total FROM {table} WHERE save_id = ?", (session.save_id,))
                    assert (await cursor.fetchone())["total"] == count
            async with asyncio.timeout(3):
                await game_routes._persist_sessions_on_shutdown()
        finally:
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await asyncio.gather(*owned_tasks, return_exceptions=True)
            for response in responses:
                await response.body_iterator.aclose()
