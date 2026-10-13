import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.base import ModelOutputUnavailableError, with_retry
from ai.openai_provider import OpenAIProvider
from db import database
from test_actor_goal_settlement import install_agent, result_from, settlement_session
from test_agent_harness_execution import _stream_events
from test_director_goal_persistence import goal_client
from test_openai_tool_stream import FakeStream, chunk
from test_regenerate_stream_errors import assert_controls_closed, database_state, regeneration_client, runtime_state
from test_story_choice_settlement import stored_state


HIDDEN = "SYNTHETIC_HIDDEN_MODEL_TEXT"


def install_empty_stream(session):
    stream = FakeStream([
        chunk(reasoning=HIDDEN),
        chunk(finish_reason="length"),
        chunk(usage=SimpleNamespace(prompt_tokens=13, completion_tokens=19,
                                   completion_tokens_details=SimpleNamespace(reasoning_tokens=19))),
    ])
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "synthetic-credential", "model": "synthetic-model"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    session.ai_provider.generate_with_tools_stream = provider.generate_with_tools_stream
    return stream, create


def output_error(response):
    assert response.status_code == 200
    events = _stream_events(response.text)
    assert not any(event["type"] == "final" for event in events)
    error, = [event for event in events if event["type"] == "error"]
    assert error["code"] == "model_output_unavailable"
    assert error["discard_partial"] is True
    assert "长度限制" in error["message"] and "思考内容" in error["message"]
    diagnostic = error["model_output"]
    assert diagnostic["reason"] == "reasoning_only"
    assert diagnostic["finish_reasons"] == ["length"]
    assert diagnostic["choice_chunk_count"] == 2
    assert diagnostic["reasoning_characters"] == len(HIDDEN)
    assert diagnostic["visible_content_characters"] == diagnostic["tool_call_count"] == 0
    assert diagnostic["completion_tokens"] == diagnostic["reasoning_tokens"] == 19
    assert HIDDEN not in response.text
    return diagnostic


@pytest.mark.asyncio
async def test_action_stream_reports_safe_diagnostics_and_allows_explicit_retry(tmp_path, monkeypatch, caplog):
    session, script = settlement_session(dynamic=True)
    session._hydrate_branch_runtime(session.current_state)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _sessions):
        install_agent(session, 9)
        stream, create = install_empty_stream(session)
        before_runtime = runtime_state(session)
        before_database = await database_state(session)
        request = {"type": "freeform", "text": "Inspect the synthetic catalog.", "intent_hint": "explicit_action",
                   "action_id": "synthetic-output-action", "stream_id": "synthetic-output-stream",
                   "expected_node_id": session.world_tree.active_node_id}
        endpoint = f"/api/game/{session.save_id}/action/stream"
        response = await client.post(endpoint, json=request)
        diagnostic = output_error(response)
        assert diagnostic["requested_output_limit"] == create.await_args.kwargs["max_tokens"]
        assert runtime_state(session) == before_runtime
        assert await database_state(session) == before_database
        assert_controls_closed(session)
        assert stream.closed and create.await_count == 1
        install_agent(session, 9)
        retried = await client.post(endpoint, json=request)
        result = result_from(retried)
        assert "script_variables" not in result["state"]
        assert session.current_state["script_variables"]["test_gate"] == 9
        assert (await stored_state(session))["script_variables"]["test_gate"] == 9
        assert session.turn_number == before_runtime[3] + 1
        await session._drain_background_tasks(timeout=None)
        calls = (session.ai_provider.generate.await_count, session.ai_provider.generate_with_tools.await_count)
        replay = await client.post(endpoint, json=request)
        assert replay.text == retried.text
        assert (session.ai_provider.generate.await_count, session.ai_provider.generate_with_tools.await_count) == calls
        assert create.await_count == 1
        async with database.get_db() as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM action_requests WHERE save_id = ?", (session.save_id,))
            assert (await cursor.fetchone())[0] == 1
        assert HIDDEN not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["all", "state", "choices"])
async def test_regeneration_stream_keeps_its_snapshot_when_output_is_unavailable(tmp_path, monkeypatch, stage, caplog):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, _sessions):
        await session._drain_background_tasks(timeout=None)
        stream, create = install_empty_stream(session)
        before_runtime = runtime_state(session)
        before_database = await database_state(session)
        response = await client.post(f"/api/game/{session.save_id}/regenerate/stream", params={"stage": stage})
        output_error(response)
        assert runtime_state(session) == before_runtime
        assert await database_state(session) == before_database
        assert_controls_closed(session)
        assert stream.closed and create.await_count == 1
        assert HIDDEN not in caplog.text


@pytest.mark.asyncio
async def test_unavailable_output_is_not_retried_due_to_numeric_diagnostics():
    error = ModelOutputUnavailableError({"reasoning_characters": 20, "completion_tokens": 500,
                                       "requested_output_limit": 1500, "finish_reasons": ["length"]})
    operation = AsyncMock(side_effect=error)

    @with_retry(max_retries=2, base_delay=0)
    async def run():
        return await operation()

    with pytest.raises(ModelOutputUnavailableError):
        await run()
    operation.assert_awaited_once()


def test_exception_diagnostics_only_expose_allowed_fields():
    error = ModelOutputUnavailableError({"model": HIDDEN, "api_key": HIDDEN, "request": HIDDEN,
                                       "reasoning_content": HIDDEN, "finish_reasons": [HIDDEN, {"secret": HIDDEN}]})
    assert HIDDEN not in str(error)
    assert HIDDEN not in json.dumps(error.diagnostics)
    assert error.diagnostics["finish_reasons"] == ["other"]
    assert error.reason == "empty_output"
