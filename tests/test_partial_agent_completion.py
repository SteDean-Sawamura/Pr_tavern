import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import IncompleteAgentTurnError, _tool_result_failed
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import NARRATIVE
from test_generation_baseline import restore
from test_partial_rule_recovery import RULE_MARKERS, install_recovery, rules_session, tool
from test_partial_stage_directives import install_stage_agent
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages


def install_incomplete_agent(session, ending, stage="state"):
    provider = install_stage_agent(session)
    provider.rounds = 0
    provider.waiting = asyncio.Event()
    provider.closed = False

    async def respond(messages, **kwargs):
        provider.rounds += 1
        if provider.rounds == 1:
            if stage == "choices":
                calls = [tool("peek_upcoming_events", {}, "peek")]
            else:
                calls = [tool("update_state", {
                    "variable_changes": [{"id": "test_review_count", "op": "inc", "value": 6}],
                    "world_property_changes": [{"id": "test_inspection_status", "value": "checking"}],
                    "end_time": "2030-01-01T09:01:00",
                }, "candidate"), tool("manage_npcs", {"operations": [{
                    "op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 8,
                    "reason": "Synthetic unfinished review.",
                }]}, "npc")]
        else:
            if stage == "state":
                results = {message["tool_call_id"]: message["content"]
                           for message in messages if message["role"] == "tool"}
                assert not _tool_result_failed(results["synthetic-recovery-candidate"])
                assert not _tool_result_failed(results["synthetic-recovery-npc"])
            if ending == "provider_error":
                raise RuntimeError("SYNTHETIC_PARTIAL_PROVIDER_ERROR")
            if ending == "waiting":
                provider.waiting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    provider.closed = True
            if ending == "abort":
                session._abort_flag = True
                return {"content": "Stopped before commit.", "tool_calls": []}
            if ending == "text":
                return {"content": "The review is unfinished.", "tool_calls": []}
            if ending == "rejected":
                calls = [tool("finalize_turn", {"narrative": "Forbidden replacement."}, "rejected")]
            else:
                assert ending == "rounds"
                calls = [tool("peek_upcoming_events", {}, f"peek-{provider.rounds}")]
        assert {call["name"] for call in calls} <= {entry["function"]["name"] for entry in kwargs["tools"]}
        return {"content": "", "tool_calls": calls}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["state", "choices"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("ending", ["text", "rounds", "rejected", "abort"])
async def test_partial_agent_requires_successful_finalization(stage, streaming, ending):
    session = rules_session()
    provider = install_incomplete_agent(session, ending, stage)
    baseline = copy.deepcopy(session.current_state)
    events = []

    async def run():
        async for event in session._regen_partial_agent(stage, NARRATIVE, ACTION["text"], baseline, streaming):
            events.append(event)

    if ending == "abort":
        await run()
        assert [event["type"] for event in events].count("aborted") == 1
    else:
        with pytest.raises(IncompleteAgentTurnError, match="未完成结算"):
            await run()
    assert not any(event["type"] == "partial_regen_done" for event in events)
    assert provider.rounds == (5 if ending in ("rounds", "rejected") else 2)
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["state", "choices"])
@pytest.mark.parametrize("stop_event", ["tool_call", "agent_done"])
async def test_partial_stop_after_terminal_tool_still_prevents_completion(stage, stop_event):
    session = rules_session()
    provider = install_stage_agent(session)
    arguments = {"choices": CHOICES} if stage == "choices" else {"end_time": "2030-01-01T09:01:00"}
    provider.generate_with_tools = AsyncMock(return_value={
        "content": "", "tool_calls": [tool("finalize_turn", arguments, "final")],
    })
    baseline = copy.deepcopy(session.current_state)
    events = []
    async for event in session._regen_partial_agent(stage, NARRATIVE, ACTION["text"], baseline):
        events.append(event)
        if event["type"] == stop_event:
            session._abort_flag = True
    terminal, = [event for event in events if event.get("tool_name") == "finalize_turn"]
    assert not _tool_result_failed(terminal["tool_result"])
    assert [event["type"] for event in events].count("aborted") == 1
    assert not any(event["type"] == "partial_regen_done" for event in events)
    assert provider.generate_with_tools.await_count == 1
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("ending", ["text", "rounds", "rejected", "abort", "provider_error"])
async def test_partial_incomplete_preserves_save_and_retry(tmp_path, monkeypatch, mode, ending):
    session = rules_session()
    session.current_state["_pipeline_mode"] = mode
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        session = await restore(session, sessions)
        provider = install_incomplete_agent(session, ending)
        before, stored = runtime_state(session), await database_state(session)
        models = copy.deepcopy(session._snapshot_session_models())
        response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", True) + "?stage=state"), 20)
        events = _stream_events(response.text)
        assert response.status_code == 200
        assert not any(event["type"] == "final" for event in events)
        assert any(event["type"] == ("aborted" if ending == "abort" else "error") for event in events)
        if ending == "abort":
            assert not any(event["type"] == "error" for event in events)
        else:
            error, = [event for event in events if event["type"] == "error"]
            if ending == "provider_error":
                assert error == {"type": "error", "message": "生成失败，请稍后重试", "discard_partial": True}
            else:
                assert error == {"type": "error", "message": "局部Agent未完成结算，重生成失败，请重试"}
            assert [event["type"] for event in events].count("agent_controls_closed") == 1
        assert "data: [DONE]" not in response.text
        assert "SYNTHETIC_PARTIAL_PROVIDER_ERROR" not in response.text
        assert not any(marker in response.text for marker in RULE_MARKERS)
        assert provider.rounds == (5 if ending in ("rounds", "rejected") else 2)
        assert runtime_state(session) == before
        assert session._snapshot_session_models() == models
        assert await database_state(session) == stored
        assert_controls_closed(session)
        install_recovery(session)
        recovered = result_from(await asyncio.wait_for(client.post(
            endpoint(session, "regenerate", True) + "?stage=state"), 20))
        assert recovered["node_id"] == initial["node_id"]
        assert recovered["swipe_index"] == 1 and recovered["total_swipes"] == 2
        assert recovered["narrative"] == initial["narrative"] and recovered["choices"] == initial["choices"]
        for index in (0, 1, 0, 1):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            for state in (session.current_state, await stored_state(session)):
                assert state["script_variables"] == {"test_review_count": 9 if index else 7}
                assert state["world_properties"]["test_inspection_status"] == ("verified" if index else "reviewed")
                assert state["player"]["relationships"]["test_guide"]["trust"] == 2
                assert state["game_time"] == "2030-01-01T09:01:00"
        parent = await session._load_snapshot_from_db(initial["node_id"])
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        continued = result_from(await client.post(endpoint(session, "action", True), json={
            "type": "freeform", "text": "Continue after the completed practice review.",
        }))
        session = await restore(session, sessions)
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == initial["node_id"]
        assert await session._load_snapshot_from_db(initial["node_id"]) == parent
        assert session.current_state["script_variables"] == {"test_review_count": 9}
        assert session.current_state["player"]["relationships"]["test_guide"]["trust"] == 4
        assert session.current_state["game_time"] == "2030-01-01T09:02:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("interruption", ["cancel", "close"])
async def test_partial_candidate_interruption_rolls_back_real_loop(tmp_path, monkeypatch, mode, interruption):
    session = rules_session()
    session.current_state["_pipeline_mode"] = mode
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        session = await restore(session, sessions)
        provider = install_incomplete_agent(session, "waiting")
        before, stored = runtime_state(session), await database_state(session)
        models = copy.deepcopy(session._snapshot_session_models())
        response = await game_routes.regenerate_stream(session.save_id, stage="state")
        stream = response.body_iterator
        pending = None
        try:
            while True:
                data = await asyncio.wait_for(anext(stream), 5)
                events = _stream_events(data)
                if any(event.get("tool_name") == "manage_npcs" for event in events):
                    break
            if interruption == "cancel":
                async def drain():
                    async for data in stream:
                        assert not any(event["type"] in ("final", "error") for event in _stream_events(data))

                pending = asyncio.create_task(drain())
                await asyncio.wait_for(provider.waiting.wait(), 5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
        assert provider.closed == (interruption == "cancel")
        assert runtime_state(session) == before
        assert session._snapshot_session_models() == models
        assert await database_state(session) == stored
        assert_controls_closed(session)
        session = await restore(session, sessions)
        install_recovery(session)
        result = result_from(await asyncio.wait_for(client.post(
            endpoint(session, "regenerate", True) + "?stage=state"), 20))
        assert result["swipe_index"] == 1 and result["total_swipes"] == 2
