import asyncio
from contextlib import asynccontextmanager
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from engine.session.agentic_mixin import IncompleteAgentTurnError
from test_actor_goal_chains import install_state_agent
from test_actor_goal_settlement import install_agent, result_from, settlement_session
from test_agent_harness_execution import _stream_events
from test_director_goal_persistence import goal_client


STAGES = ["all", "state", "choices"]
PRIVATE_ERROR = "SYNTHETIC_REGEN_INTERNAL_ERROR"


def install_regeneration_agent(session, stage):
    if stage == "all":
        install_agent(session, 9)
    elif stage == "state":
        install_state_agent(session, 9)
    else:
        session.ai_provider.generate_with_tools = AsyncMock(return_value={
            "content": "", "tool_calls": [{
                "id": "synthetic-regen-choices", "name": "finalize_turn", "arguments": {
                    "summary": "Choose the next inspection.",
                    "choices": [{"id": f"test_choice_{index}", "text": f"Inspect shelf {index}."}
                                for index in range(4)],
                },
            }],
        })


def runtime_state(session):
    return copy.deepcopy((
        session.current_state, session.world_tree.nodes, session.world_tree.active_node_id,
        session.turn_number, session.script, session._npc_by_id, session._npc_name_to_id,
        session._agent_traces,
        [(entry.id, entry.content, entry.enabled) for entry in session.prompt_builder.lorebook.entries],
    ))


async def database_state(session):
    async with database.get_db() as db:
        saved = await (await db.execute("SELECT * FROM saves WHERE id = ?", (session.save_id,))).fetchall()
        nodes = await (await db.execute("SELECT * FROM tree_nodes WHERE save_id = ? ORDER BY id",
                                       (session.save_id,))).fetchall()
        return [dict(row) for row in saved], [dict(row) for row in nodes]


def assert_controls_closed(session):
    assert not game_routes._get_session_lock(session.save_id).locked()
    assert not session._active_agent_stream
    assert not session._active_agent_stream_id
    assert not session._agent_inject_enabled
    assert not session._inject_queue
    assert not session._pending_agent_plan_id
    assert not session._abort_request_pending


@asynccontextmanager
async def regeneration_client(tmp_path, monkeypatch, stage):
    session, script = settlement_session(dynamic=True)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        install_agent(session, 0)
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-before-regen-error",
        }))
        install_regeneration_agent(session, stage)
        yield session, client, sessions


def assert_failed_stream(response):
    assert response.status_code == 200
    events = _stream_events(response.text)
    assert not any(event["type"] == "final" for event in events)
    assert "data: [DONE]" not in response.text
    assert PRIVATE_ERROR not in response.text
    error, = [event for event in events if event["type"] == "error"]
    assert error == {"type": "error", "message": "生成失败，请稍后重试", "discard_partial": True}
    assert events[-1] == error
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_regeneration_commit_failure_reports_error_rolls_back_and_retries(tmp_path, monkeypatch, stage):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, sessions):
        before, stored = runtime_state(session), await database_state(session)
        attempts = []

        @asynccontextmanager
        async def fail_commit():
            async with database.get_db() as db:
                writes_turn = False

                async def execute(sql, *args):
                    nonlocal writes_turn
                    writes_turn |= (sql.lstrip().startswith("INSERT INTO tree_nodes")
                                    or (sql.lstrip().startswith("UPDATE tree_nodes") and "ai_response" in sql))
                    return await db.execute(sql, *args)

                async def commit():
                    if not writes_turn:
                        return await db.commit()
                    assert runtime_state(session) != before
                    attempts.append(True)
                    raise RuntimeError(PRIVATE_ERROR)

                yield SimpleNamespace(execute=execute, commit=commit)

        endpoint = f"/api/game/{session.save_id}/regenerate/stream?stage={stage}"
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "get_db", fail_commit)
            response = await asyncio.wait_for(client.post(endpoint), 15)
        events = assert_failed_stream(response)
        assert any(event["type"] == "agent_controls_closed" for event in events)
        assert attempts == [True]
        assert runtime_state(session) == before
        assert await database_state(session) == stored
        assert_controls_closed(session)

        final = result_from(await asyncio.wait_for(client.post(endpoint), 15))
        assert final["node_id"] == before[2]
        assert final["total_swipes"] == 2 and final["swipe_index"] == 1
        committed = await database_state(session)
        assert committed != stored
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.world_tree.active_node_id == final["node_id"]
        node = restored.world_tree.get_node(final["node_id"])
        assert len(node["swipes"]) == 2 and node["active_swipe_index"] == 1
        assert restored.current_state["player"]["gold"] == (10 if stage == "choices" else 13)
        assert restored.current_state["script_variables"]["test_gate"] == (0 if stage == "choices" else 9)
        assert await database_state(restored) == committed
        assert_controls_closed(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("failure", ["prepare", "generate", "incomplete"])
async def test_regeneration_errors_close_pending_controls_and_preserve_the_turn(tmp_path, monkeypatch, stage, failure):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, _):
        before, stored = runtime_state(session), await database_state(session)
        closed = []

        async def fail_prepare():
            raise RuntimeError(PRIVATE_ERROR)

        async def fail_generation(*args, **kwargs):
            try:
                session.current_state["script_variables"]["test_gate"] = -1
                session._inject_queue.append("Synthetic pending interjection")
                session._queued_agent_plan_reply_id = "synthetic-pending-plan"
                session._pending_agent_plan_id = "synthetic-pending-plan"
                session._abort_request_pending = True
                yield {"type": "model_waiting", "label": "Synthetic generator"}
                if failure == "incomplete":
                    raise IncompleteAgentTurnError("Synthetic incomplete turn")
                raise RuntimeError(PRIVATE_ERROR)
            finally:
                closed.append(True)

        method = "process_action_stream" if stage == "all" else "_regen_partial_agent"
        with monkeypatch.context() as patch:
            patch.setattr(session, "_drain_background_tasks" if failure == "prepare" else method,
                          fail_prepare if failure == "prepare" else fail_generation)
            response = await client.post(f"/api/game/{session.save_id}/regenerate/stream?stage={stage}")
        if failure == "incomplete":
            events = _stream_events(response.text)
            assert events[-1] == {"type": "error", "message": "Synthetic incomplete turn"}
            assert not any(event["type"] == "final" for event in events)
            assert "data: [DONE]" not in response.text
        else:
            events = assert_failed_stream(response)
        assert [event["type"] for event in events].count("agent_controls_closed") == 1
        if failure != "prepare":
            assert closed == [True]
            assert {"type": "agent_inject_dropped", "message": "Synthetic pending interjection",
                    "plan_id": "synthetic-pending-plan"} in events
            assert {"type": "agent_stop_missed"} in events
        assert runtime_state(session) == before
        assert await database_state(session) == stored
        assert_controls_closed(session)
        result_from(await asyncio.wait_for(client.post(
            f"/api/game/{session.save_id}/regenerate/stream?stage={stage}"), 15))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_regeneration_failure_after_commit_does_not_claim_rollback(tmp_path, monkeypatch, stage):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, sessions):
        before = await database_state(session)
        original = game_routes._public_result
        calls = []

        def fail_final_encoding(result, *args, **kwargs):
            if result.get("type") == "final":
                calls.append(True)
                raise RuntimeError(PRIVATE_ERROR)
            return original(result, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "_public_result", fail_final_encoding)
            response = await client.post(f"/api/game/{session.save_id}/regenerate/stream?stage={stage}")
        assert calls == [True]
        assert response.status_code == 200
        assert PRIVATE_ERROR not in response.text
        assert not any(event["type"] in {"error", "final"} for event in _stream_events(response.text))
        assert "data: [DONE]" not in response.text
        committed = await database_state(session)
        assert committed != before
        assert_controls_closed(session)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        node = restored.world_tree.get_node(restored.world_tree.active_node_id)
        assert len(node["swipes"]) == 2 and node["active_swipe_index"] == 1
        assert await database_state(restored) == committed


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("interruption", ["cancel", "close"])
async def test_regeneration_cancellation_is_not_converted_to_a_server_error(tmp_path, monkeypatch, stage, interruption):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, _):
        before, stored = runtime_state(session), await database_state(session)
        blocked, waiting = asyncio.Event(), asyncio.Event()
        closed = []

        async def interrupted_stream(*args, **kwargs):
            try:
                session.current_state["script_variables"]["test_gate"] = -1
                yield {"type": "model_waiting", "label": "Synthetic generator"}
                waiting.set()
                await blocked.wait()
            finally:
                closed.append(True)

        method = "process_action_stream" if stage == "all" else "_regen_partial_agent"
        with monkeypatch.context() as patch:
            patch.setattr(session, method, interrupted_stream)
            response = await game_routes.regenerate_stream(session.save_id, stage=stage)
            stream = response.body_iterator
            assert "stream_ready" in await anext(stream)
            assert "model_waiting" in await anext(stream)
            if interruption == "cancel":
                pending = asyncio.create_task(anext(stream))
                try:
                    await asyncio.wait_for(waiting.wait(), 2)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
        assert closed == [True]
        assert runtime_state(session) == before
        assert await database_state(session) == stored
        assert_controls_closed(session)
        result_from(await asyncio.wait_for(client.post(
            f"/api/game/{session.save_id}/regenerate/stream?stage={stage}"), 15))
