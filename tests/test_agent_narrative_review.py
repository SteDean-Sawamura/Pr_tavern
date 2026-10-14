import asyncio
import copy
import json
from collections import OrderedDict
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_output", [False, True])
@pytest.mark.parametrize("review_timing", ["concurrent", "published_first"])
@pytest.mark.parametrize("review_outcome", [
    "passed", "rejected", "unavailable", "aborted", "aborted_finalize", "aborted_delegate",
    "interjected_finalize", "interjected_delegate",
])
async def test_replaced_review_cannot_publish_current_draft(tmp_path, monkeypatch, review_outcome, review_timing, declared_output):
    script = {
        "script_id": "synthetic-review-race", "name": "Synthetic review race", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "story_tree": {"trees": [{"id": "test_story", "nodes": [{
            "id": "test_anchor", "name": "Synthetic inspection", "type": "manual",
            "description": "SYNTHETIC_REVIEW_ANCHOR: a background test condition remains unresolved.",
        }]}]},
    }
    drafts = [
        "You examine the empty shelf beside the wall. The wooden surface feels smooth and dry. "
        "You look along its edges without moving anything, then step back to leave the room undisturbed.",
        "You examine the empty table beside the wall. Its corners are rounded and its surface is clean. "
        "You look beneath it without moving anything, then step back to leave the room undisturbed.",
        "You examine the empty chair beside the wall. Its legs rest evenly on the floor. "
        "You look at its back without moving anything, then step back to leave the room undisturbed.",
    ]
    raw_drafts = [text.replace("examine", "SYNTHETIC_INSPECT") if declared_output else text for text in drafts]
    if declared_output:
        script["post_processing_rules"] = [{"find": "SYNTHETIC_INSPECT", "replace": "examine"}]
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    first_visible = asyncio.Event()
    first_finished = asyncio.Event()
    continue_rewrite = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    second_dispatched = asyncio.Event()
    second_delivered = asyncio.Event()
    model_blocked = asyncio.Event()
    model_cancelled = asyncio.Event()
    continue_model = asyncio.Event()
    terminal_entered = asyncio.Event()
    terminal_dispatched = asyncio.Event()
    retry_started = asyncio.Event()
    release_retry = asyncio.Event()
    terminal_completed = asyncio.Event()
    release_terminal = asyncio.Event()
    stale_review_observed = asyncio.Event()
    approved_draft_visible = asyncio.Event()
    review_tasks = {}
    review_texts = []
    review_actions = []
    requests = []
    streamed = []
    body_buffer = ""
    writes_delivered = 0
    terminal_name = "delegate_settlement" if review_outcome.endswith("_delegate") else "finalize_turn"
    interjected = review_outcome.startswith("interjected")
    injection = "Inspect only the chair; leave the table alone."

    async def review_narrative(narrative, *_args, **_kwargs):
        review_texts.append(narrative)
        review_actions.append(_args[0])
        review_tasks[narrative] = asyncio.current_task()
        if narrative == drafts[0]:
            first_started.set()
            await release_first.wait()
            first_finished.set()
        elif narrative == drafts[1]:
            second_started.set()
            await release_second.wait()
            if review_outcome == "rejected":
                return {"pass": False, "violations": [{
                    "type": "unsupported_fact", "detail": "Use the corrected synthetic description.",
                }]}
            if review_outcome == "unavailable":
                return None
        return {"pass": True, "violations": []}

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        requests.append(copy.deepcopy(messages))
        operations = [
            ("submit_plan", {"plan": "Inspect the room without moving anything."}),
            ("write_narrative", {"text": raw_drafts[0]}),
            ("write_narrative", {"text": raw_drafts[1]}),
            (terminal_name, {"summary": "Inspected the room."}),
        ]
        if review_outcome == "rejected" or interjected:
            operations.extend([
                ("write_narrative", {"text": raw_drafts[2]}),
                ("finalize_turn", {"summary": "Inspected the room."}),
            ])
        index = len(requests) - 1
        assert index < len(operations)
        if interjected and index == 4:
            assert {"role": "user", "content": injection} in messages
            retry_started.set()
            await release_retry.wait()
        if index == 2 and review_timing == "published_first":
            await continue_rewrite.wait()
        if index == 3:
            model_blocked.set()
            try:
                await continue_model.wait()
            except asyncio.CancelledError:
                model_cancelled.set()
                raise
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-review-{index}", "name": name, "arguments": arguments,
        }]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session = GameSession(copy.deepcopy(script), provider, save_id="synthetic-review-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["story_tree_state"] = {"active": ["test_anchor"], "completed": []}
    assert session._narrative_review_evidence({})[0]
    root_id = session.world_tree.add_node(
        None, script["start_time"], 0, ai_response="You are in the reading room.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    original_dispatch = session._dispatch_unified_tool
    original_agent_loop = session._agent_loop
    original_wait = asyncio.wait

    async def dispatch(ctx, name, arguments):
        if name == terminal_name:
            terminal_dispatched.set()
        result = await original_dispatch(ctx, name, arguments)
        if name == "write_narrative" and ctx.get("_finalize_narrative") == drafts[1]:
            second_dispatched.set()
            release_first.set()
            await first_finished.wait()
        return result

    async def agent_loop(messages, system, tools, tool_dispatch, **kwargs):
        async def monitored_dispatch(name, arguments):
            if name == terminal_name:
                terminal_entered.set()
            return await tool_dispatch(name, arguments)

        async with aclosing(original_agent_loop(messages, system, tools, monitored_dispatch, **kwargs)) as events:
            async for event in events:
                if (interjected and event.get("type") == "tool_call"
                        and event.get("tool_name") == "finalize_turn"
                        and '"error"' not in event.get("tool_result", "")):
                    terminal_completed.set()
                    await release_terminal.wait()
                yield event

    async def wait_for_events(pending, **kwargs):
        done, remaining = await original_wait(pending, **kwargs)
        if review_tasks.get(drafts[0]) in done and second_dispatched.is_set():
            await second_started.wait()
            stale_review_observed.set()
        return done, remaining

    monkeypatch.setattr(session, "_review_narrative", review_narrative)
    monkeypatch.setattr(session, "_dispatch_unified_tool", dispatch)
    monkeypatch.setattr(session, "_agent_loop", agent_loop)
    monkeypatch.setattr(asyncio, "wait", wait_for_events)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "review-race.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")

    async def capture_app(scope, receive, send):
        if not scope["path"].endswith("/action/stream"):
            await app(scope, receive, send)
            return

        async def capture_send(message):
            nonlocal body_buffer, writes_delivered
            await send(message)
            if message["type"] != "http.response.body":
                return
            body_buffer += message.get("body", b"").decode("utf-8")
            while "\n\n" in body_buffer:
                frame, body_buffer = body_buffer.split("\n\n", 1)
                data = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), "")
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                streamed.append(event)
                if event["type"] == "tool_call" and event.get("tool_name") == "write_narrative":
                    writes_delivered += 1
                    if writes_delivered == 2:
                        second_delivered.set()
                if event["type"] in ("text", "narrative_revised") and event.get("reviewed"):
                    if event["content"] == drafts[0]:
                        first_visible.set()
                    if event["content"] == drafts[1]:
                        approved_draft_visible.set()
        await app(scope, receive, capture_send)

    sessions = [session]
    request_task = None
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": session.current_state, "narrative": "You are in the reading room.",
        })
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {
            "type": "freeform", "text": "Inspect the room.", "intent_hint": "explicit_action",
            "stream_id": "synthetic-review-stream", "action_id": "synthetic-review-action",
            "expected_node_id": root_id,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=capture_app), base_url="http://test",
        ) as client:
            request_task = asyncio.create_task(client.post(endpoint, json=action))
            try:
                initial_drafts = []
                if review_timing == "published_first":
                    await asyncio.wait_for(first_started.wait(), timeout=5)
                    release_first.set()
                    await asyncio.wait_for(first_visible.wait(), timeout=5)
                    initial_drafts.append(drafts[0])
                    continue_rewrite.set()
                await asyncio.wait_for(second_delivered.wait(), timeout=5)
                await asyncio.wait_for(model_blocked.wait(), timeout=5)
                assert stale_review_observed.is_set() is (review_timing == "concurrent")
                assert not review_tasks[drafts[1]].done()
                assert [event["content"] for event in streamed
                        if event["type"] in ("text", "narrative_revised")] == initial_drafts
                assert session.world_tree.active_node_id == root_id
                if interjected:
                    continue_model.set()
                    await asyncio.wait_for(terminal_entered.wait(), timeout=5)
                    assert not terminal_dispatched.is_set()
                    injected = await client.post(f"/api/game/{session.save_id}/inject", json={
                        "stream_id": action["stream_id"], "message": injection,
                    })
                    assert injected.status_code == 200
                    assert injected.json() == {"queued": True}
                    await asyncio.wait_for(retry_started.wait(), timeout=5)
                    assert review_tasks[drafts[1]].cancelled()
                    assert not terminal_dispatched.is_set()
                    assert not any(event["type"] in ("final", "user_inject") for event in streamed)
                    assert [event["content"] for event in streamed
                            if event["type"] in ("text", "narrative_revised")] == initial_drafts
                    release_retry.set()
                if review_outcome.startswith("aborted"):
                    if review_outcome != "aborted":
                        continue_model.set()
                        await asyncio.wait_for(terminal_entered.wait(), timeout=5)
                        assert not terminal_dispatched.is_set()
                        assert not review_tasks[drafts[1]].done()
                    abort = await client.post(f"/api/game/{session.save_id}/abort", json={
                        "stream_id": action["stream_id"],
                    })
                    assert abort.status_code == 200
                    response = await asyncio.wait_for(asyncio.shield(request_task), timeout=5)
                    assert response.status_code == 200
                    assert any(event["type"] == "aborted" for event in streamed)
                    assert not any(event["type"] in ("final", "error") for event in streamed)
                    assert [event["content"] for event in streamed
                            if event["type"] in ("text", "narrative_revised")] == initial_drafts
                    assert model_cancelled.is_set() is (review_outcome == "aborted")
                    assert review_tasks[drafts[1]].cancelled()
                    assert not terminal_dispatched.is_set()
                    assert session.world_tree.active_node_id == root_id
                    assert not session._active_agent_stream
                    assert not game_routes._get_session_lock(session.save_id).locked()
                    async with database.get_db() as db:
                        row = await (await db.execute(
                            "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,),
                        )).fetchone()
                    assert row[0] == 1
                    return
                release_second.set()
                if review_outcome == "passed":
                    await asyncio.wait_for(approved_draft_visible.wait(), timeout=5)
                    assert not request_task.done()
                    assert session.world_tree.active_node_id == root_id
                continue_model.set()
                if interjected:
                    await asyncio.wait_for(terminal_completed.wait(), timeout=5)
                    assert session._active_agent_stream
                    assert not request_task.done()
                    late_injection = await client.post(f"/api/game/{session.save_id}/inject", json={
                        "stream_id": action["stream_id"], "message": "A late synthetic instruction.",
                    })
                    assert late_injection.status_code == 409
                    assert session._inject_queue == []
                    release_terminal.set()
                response = await asyncio.wait_for(request_task, timeout=5)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                assert not [event for event in streamed if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in streamed if event["type"] == "final"]
                expected = drafts[2] if review_outcome == "rejected" or interjected else drafts[1]
                assert final["narrative"] == expected
                text_events = [event for event in streamed if event["type"] in ("text", "narrative_revised")]
                assert [event["content"] for event in text_events] == initial_drafts + [expected]
                assert text_events[-1]["type"] == ("narrative_revised" if initial_drafts else "text")
                if review_outcome == "unavailable":
                    assert not approved_draft_visible.is_set()
                assert review_texts == (drafts if review_outcome == "rejected" or interjected else drafts[:2])
                if interjected:
                    assert injection in review_actions[-1]
                    assert [event["message"] for event in streamed if event["type"] == "user_inject"] == [injection]
                    assert session._inject_queue == []
                    async with database.get_db() as db:
                        pending = await (await db.execute(
                            "SELECT COUNT(*) FROM pending_action_injections WHERE save_id = ?", (session.save_id,),
                        )).fetchone()
                    assert pending[0] == 0
                model_calls = provider.generate_with_tools.await_count
                replay = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
                assert replay.text == response.text
                assert provider.generate_with_tools.await_count == model_calls
            finally:
                release_first.set()
                continue_rewrite.set()
                release_second.set()
                continue_model.set()
                release_retry.set()
                release_terminal.set()
                if not request_task.done():
                    await asyncio.wait_for(request_task, timeout=5)
        await session._drain_background_tasks()
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.world_tree.active_node_id == final["node_id"]
        assert restored.world_tree.get_node(final["node_id"])["ai_response"] == expected
        await restored.branch_to_node(root_id)
        assert restored.world_tree.get_node(root_id)["ai_response"] == "You are in the reading room."
        await restored.branch_to_node(final["node_id"])
        assert restored.world_tree.get_node(final["node_id"])["ai_response"] == expected
        async with database.get_db() as db:
            rows = await (await db.execute(
                "SELECT ai_response FROM tree_nodes WHERE save_id = ?", (session.save_id,),
            )).fetchall()
        assert len(rows) == 2
        assert expected in [row[0] for row in rows]
        assert drafts[0] not in [row[0] for row in rows]
        assert all(task.done() for task in review_tasks.values())
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
