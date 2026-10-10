import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api import game_routes
from engine.session import agentic_mixin
from engine.session.agentic_mixin import AgenticMixin, _AgentResult


class PlanLoop(AgenticMixin):
    def __init__(self, questions, plan="Proceed"):
        self._inject_queue = []
        self.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=[
            {"content": "", "tool_calls": [{
                "id": "plan-1", "name": "submit_plan",
                "arguments": {"plan": plan, "questions": questions},
            }]},
            {"content": "The scene continues.", "tool_calls": []},
        ]))

    def _stage_kwargs(self, stage_key):
        return {}

    def _map_travel_destination(self, player_action):
        return None

    def _build_unified_plan_system(self):
        return "system"

    def _build_unified_plan_context(self, ctx, player_action):
        return "Go"

    def _narrative_review_evidence(self, ctx):
        return [], ""


@pytest.mark.asyncio
async def test_stream_reports_tool_preparation_before_response_and_stops_cleanly():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._abort_flag = False
    prepared = asyncio.Event()
    cancelled = asyncio.Event()

    async def streamed(*_args, **_kwargs):
        try:
            yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
            prepared.set()
            await asyncio.Future()
        finally:
            cancelled.set()

    loop.ai_provider.generate_with_tools_stream = streamed
    stream = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [], AsyncMock())
    try:
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "model_waiting"
        assert await asyncio.wait_for(anext(stream), 0.5) == {
            "type": "tool_preparing", "tool_name": "query_lorebook", "label": "agent", "round": 1,
        }
        assert prepared.is_set()
        loop._abort_flag = True
        loop._agent_stop_event.set()
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "aborted"
        assert cancelled.is_set()
        loop.ai_provider.generate_with_tools.assert_not_awaited()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_fast_stream_preserves_tool_preparation_before_tool_start():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._abort_flag = False
    calls = 0

    async def streamed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
            yield {"type": "response", "response": {"content": "", "tool_calls": [
                {"id": "lore-1", "name": "query_lorebook", "arguments": {}},
            ]}}
        else:
            yield {"type": "response", "response": {"content": "Done", "tool_calls": []}}

    loop.ai_provider.generate_with_tools_stream = streamed
    events = [event async for event in loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], AsyncMock(return_value="Found"),
        max_rounds=2, report_tool_start=True,
    )]
    event_types = [event["type"] for event in events]
    assert event_types.index("tool_preparing") < event_types.index("tool_started")
    assert events[event_types.index("tool_preparing")]["tool_name"] == "query_lorebook"


@pytest.mark.asyncio
async def test_fast_stream_preserves_multiple_preparations_in_order():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._abort_flag = False

    async def streamed(*_args, **_kwargs):
        yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
        yield {"type": "tool_preparing", "tool_name": "peek_upcoming_events"}
        yield {"type": "response", "response": {"content": "Done", "tool_calls": []}}

    loop.ai_provider.generate_with_tools_stream = streamed
    events = [event async for event in loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], AsyncMock(),
    )]
    preparations = [event for event in events if event["type"] == "tool_preparing"]
    assert [event["tool_name"] for event in preparations] == [
        "query_lorebook", "peek_upcoming_events",
    ]
    assert all(event["round"] == 1 for event in preparations)
    assert events.index(preparations[-1]) < next(
        index for index, event in enumerate(events) if event["type"] == "agent_done"
    )


@pytest.mark.asyncio
async def test_abort_does_not_flush_queued_tool_preparation():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._abort_flag = False
    cancelled = asyncio.Event()

    async def streamed(*_args, **_kwargs):
        try:
            yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
            yield {"type": "tool_preparing", "tool_name": "peek_upcoming_events"}
            await asyncio.Future()
        finally:
            cancelled.set()

    loop.ai_provider.generate_with_tools_stream = streamed
    stream = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [], AsyncMock())
    try:
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "model_waiting"
        assert (await asyncio.wait_for(anext(stream), 0.5))["tool_name"] == "query_lorebook"
        loop._abort_flag = True
        loop._agent_stop_event.set()
        assert [event["type"] async for event in stream] == ["aborted"]
        assert cancelled.is_set()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_interjection_discards_queued_preparation_from_cancelled_round():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    loop._abort_flag = False
    cancelled = asyncio.Event()
    requests = []

    async def streamed(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            try:
                yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
                yield {"type": "tool_preparing", "tool_name": "peek_upcoming_events"}
                await asyncio.Future()
            finally:
                cancelled.set()
        else:
            yield {"type": "tool_preparing", "tool_name": "read_director_notes"}
            yield {"type": "response", "response": {"content": "New path", "tool_calls": []}}

    loop.ai_provider.generate_with_tools_stream = streamed
    stream = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [], AsyncMock())
    try:
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "model_waiting"
        assert (await asyncio.wait_for(anext(stream), 0.5))["tool_name"] == "query_lorebook"
        loop._inject_queue.append("Use new information")
        loop._agent_control_event.set()
        events = [event async for event in stream]
        assert cancelled.is_set()
        assert {"role": "user", "content": "Use new information"} in requests[1]
        assert [event["tool_name"] for event in events if event["type"] == "tool_preparing"] == [
            "read_director_notes",
        ]
        assert [event["type"] for event in events if event["type"] == "user_inject"] == ["user_inject"]
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_interjection_at_response_boundary_drops_queued_stale_preparation():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    loop._abort_flag = False
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def streamed(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
            yield {"type": "tool_preparing", "tool_name": "peek_upcoming_events"}
            await release.wait()
            tool_name = "stale_tool"
        else:
            yield {"type": "tool_preparing", "tool_name": "read_director_notes"}
            tool_name = "fresh_tool"
        yield {"type": "response", "response": {"content": "", "tool_calls": [
            {"id": tool_name, "name": tool_name, "arguments": {}},
        ]}}

    loop.ai_provider.generate_with_tools_stream = streamed
    stream = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(name), max_rounds=2, report_tool_start=True,
    )
    try:
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "model_waiting"
        assert (await asyncio.wait_for(anext(stream), 0.5))["tool_name"] == "query_lorebook"
        loop._inject_queue.append("Use new information")
        loop._agent_control_event.set()
        release.set()
        events = [event async for event in stream]
        assert [event["tool_name"] for event in events if event["type"] == "tool_preparing"] == [
            "read_director_notes",
        ]
        assert dispatched == ["fresh_tool"]
        assert {"role": "user", "content": "Use new information"} in requests[1]
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_injection_discards_incomplete_streamed_tool_response():
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    loop._abort_flag = False
    cancelled = asyncio.Event()
    requests = []

    async def streamed(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            try:
                yield {"type": "tool_preparing", "tool_name": "query_lorebook"}
                await asyncio.Future()
            finally:
                cancelled.set()
        else:
            yield {"type": "response", "response": {"content": "按插话继续", "tool_calls": []}}

    loop.ai_provider.generate_with_tools_stream = streamed
    holder = _AgentResult()
    stream = loop._agent_loop(
        [{"role": "user", "content": "查阅"}], "system", [], AsyncMock(), result_holder=holder,
    )
    try:
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "model_waiting"
        assert (await asyncio.wait_for(anext(stream), 0.5))["type"] == "tool_preparing"
        loop._inject_queue.append("不要采用旧资料")
        loop._agent_control_event.set()
        events = [event async for event in stream]
        assert cancelled.is_set()
        assert {"role": "user", "content": "不要采用旧资料"} in requests[1]
        assert [event["type"] for event in events if event["type"] == "user_inject"] == ["user_inject"]
        assert holder.text == "按插话继续"
        assert holder.model_calls == len(requests) == 2
        loop.ai_provider.generate_with_tools.assert_not_awaited()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("questions", [[], [{"question": "Where?", "options": ["Left", "Right"]}]])
async def test_non_stream_plan_does_not_wait_for_unavailable_interaction(questions):
    loop = PlanLoop(questions)
    holder = _AgentResult()

    async def collect():
        return [event async for event in loop._agent_loop(
            [{"role": "user", "content": "Go"}], "system", [],
            lambda name, args: "Recorded", max_rounds=2, result_holder=holder,
        )]

    events = await asyncio.wait_for(collect(), timeout=0.5)

    assert [event["type"] for event in events] == ["tool_call", "agent_done"]
    assert holder.text == "The scene continues."
    assert loop.ai_provider.generate_with_tools.await_count == 2


@pytest.mark.asyncio
async def test_agent_loop_records_executed_tool_duration():
    loop = PlanLoop([])
    holder = _AgentResult()

    async def dispatch(name, args):
        await asyncio.sleep(0.03)
        return "Recorded"

    await _collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=2, result_holder=holder,
    ))

    assert holder.records[0]["name"] == "submit_plan"
    assert holder.records[0]["duration_ms"] >= 20


@pytest.mark.asyncio
async def test_stream_reports_tool_start_before_dispatch_finishes():
    loop = PlanLoop([])
    release_tool = asyncio.Event()

    async def dispatch(name, args):
        await release_tool.wait()
        return "Recorded"

    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=2, report_tool_start=True,
    )
    started = await anext(events)
    assert started == {"type": "tool_started", "label": "agent", "round": 1,
                       "tool_name": "submit_plan"}
    pending = asyncio.create_task(anext(events))
    await asyncio.sleep(0)
    assert not pending.done()
    release_tool.set()
    assert (await pending)["type"] == "tool_call"
    assert (await anext(events))["type"] == "agent_done"


@pytest.mark.asyncio
async def test_slow_stream_tool_sends_heartbeat_and_finishes(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    release_tool = asyncio.Event()
    monkeypatch.setattr(agentic_mixin, "_MODEL_WAIT_HEARTBEAT_SECONDS", 0.01)

    async def dispatch(name, args):
        await release_tool.wait()
        return "Recorded"

    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=2, report_tool_start=True,
    )
    try:
        assert (await anext(events))["type"] == "tool_started"
        assert await asyncio.wait_for(anext(events), timeout=0.5) == {"type": "heartbeat"}
        release_tool.set()
        assert (await anext(events))["type"] == "tool_call"
        assert (await anext(events))["type"] == "agent_done"
    finally:
        release_tool.set()
        await events.aclose()


@pytest.mark.asyncio
async def test_closing_stream_cancels_waiting_tool(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    cancelled = asyncio.Event()
    monkeypatch.setattr(agentic_mixin, "_MODEL_WAIT_HEARTBEAT_SECONDS", 0.01)

    async def dispatch(name, args):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=2, report_tool_start=True,
    )
    assert (await anext(events))["type"] == "tool_started"
    assert await asyncio.wait_for(anext(events), timeout=0.5) == {"type": "heartbeat"}
    await events.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["recall_history", "update_state"])
async def test_stream_stop_only_cancels_readonly_history_tool(monkeypatch, tool_name):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "slow-tool"
    loop._agent_stop_event = asyncio.Event()
    loop.ai_provider.generate_with_tools = AsyncMock(return_value={
        "content": "", "tool_calls": [{"id": "slow", "name": tool_name, "arguments": {}}],
    })
    monkeypatch.setitem(game_routes._sessions, "slow-tool", loop)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(name, args):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "Recorded"

    task = asyncio.create_task(_collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=2, report_tool_start=True,
    )))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert await game_routes.abort_generation(
            "slow-tool", game_routes.StreamControlRequest(stream_id="slow-tool"),
        ) == {"requested": True}
        if tool_name == "recall_history":
            events = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            assert cancelled.is_set()
            assert [event["type"] for event in _control_events(events)] == ["tool_started", "aborted"]
        else:
            await asyncio.sleep(0)
            assert not task.done()
            assert not cancelled.is_set()
            release.set()
            events = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            assert [event["type"] for event in _control_events(events)] == [
                "tool_started", "tool_call", "aborted",
            ]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_context_compression_keeps_player_interjections():
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(return_value={"content": "Done", "tool_calls": []})
    messages = [
        {"role": "user", "content": "开场行动"},
        {"role": "assistant", "content": "叙事" * 16000},
        {"role": "user", "content": "先留在街口，不去车站"},
        {"role": "assistant", "content": "对话"},
        {"role": "user", "content": "不要告诉联系人真实姓名"},
        {"role": "assistant", "content": "继续"},
        {"role": "user", "content": "后续行动"},
        {"role": "assistant", "content": "现场反应"},
        {"role": "user", "content": "查看街口"},
    ]

    await _collect_events(loop._agent_loop(
        messages, "system", [], lambda name, args: "", max_rounds=1,
    ))

    compacted = loop.ai_provider.generate_with_tools.await_args.args[0]
    summary = compacted[1]["content"]
    assert "先留在街口，不去车站" in summary
    assert "不要告诉联系人真实姓名" in summary
    assert compacted[0] == messages[0]
    assert compacted[-4:] == messages[-4:]


@pytest.mark.asyncio
async def test_repeated_context_compression_keeps_earlier_player_decisions():
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [{"id": "lookup", "name": "lookup", "arguments": {}}]},
        {"content": "Done", "tool_calls": []},
    ])
    long_decision = "先留在街口，" + "仔细确认消息来源。" * 15 + "不要透露真实姓名"
    messages = [
        {"role": "user", "content": "开场行动"},
        {"role": "assistant", "content": "叙事" * 16000},
        {"role": "user", "content": long_decision},
        {"role": "assistant", "content": "对话"},
        {"role": "user", "content": "不要去车站"},
        {"role": "assistant", "content": "继续"},
        {"role": "user", "content": "后续行动"},
        {"role": "assistant", "content": "现场反应"},
        {"role": "user", "content": "查看街口"},
    ]

    await _collect_events(loop._agent_loop(
        messages, "system", [], lambda name, args: "工具结果" * 16000, max_rounds=2,
    ))

    first_call, second_call = loop.ai_provider.generate_with_tools.await_args_list
    first_summary = first_call.args[0][1]["content"]
    second_summary = second_call.args[0][1]["content"]
    for summary in (first_summary, second_summary):
        assert long_decision in summary
        assert "不要去车站" in summary
    assert second_summary.count("[玩家消息] " + long_decision) == 1


@pytest.mark.asyncio
async def test_stream_plan_waits_for_player_reply():
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    holder = _AgentResult()
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2, interactive_plan=True,
        result_holder=holder,
    )

    tool_event = await asyncio.wait_for(anext(events), timeout=0.5)
    waiting_event = await asyncio.wait_for(anext(events), timeout=0.5)
    assert tool_event["type"] == "tool_call"
    assert waiting_event["type"] == "agent_waiting"
    assert loop.ai_provider.generate_with_tools.await_count == 1

    await asyncio.sleep(0.03)
    loop._inject_queue.append("Right")
    remaining = await asyncio.wait_for(_collect_events(events), timeout=0.5)
    assert [event["type"] for event in remaining] == ["user_inject", "agent_done"]
    assert loop.ai_provider.generate_with_tools.await_count == 2
    assert holder.plan_waits == 1
    assert holder.plan_wait_ms >= 20


@pytest.mark.asyncio
@pytest.mark.parametrize("questions", [
    [{"question": "Where?", "options": []}],
    [{"question": "Where?"}],
    [{"question": "Where?", "options": ["Only one"]}],
    [{"question": "Where?", "options": ["Left", "  "]}],
    ["Where?"],
])
async def test_invalid_plan_questions_request_repair_without_waiting(questions):
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [{"id": "bad", "name": "submit_plan",
                                    "arguments": {"plan": "Choose", "questions": questions}}]},
        {"content": "", "tool_calls": [{"id": "good", "name": "submit_plan",
                                    "arguments": {"plan": "Choose", "questions": [
                                        {"question": "Where?", "options": ["Left", "Right"]},
                                    ]}}]},
    ])
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: loop._dispatch_unified_tool({}, name, args),
        max_rounds=2, interactive_plan=True,
    )
    try:
        rejected = await asyncio.wait_for(anext(events), timeout=0.5)
        assert rejected["type"] == "tool_call"
        assert 'error' in rejected["tool_result"]
        assert "plan_id" not in rejected
        offered = await asyncio.wait_for(anext(events), timeout=0.5)
        assert offered["plan_id"] == loop._pending_agent_plan_id
        assert (await asyncio.wait_for(anext(events), timeout=0.5))["type"] == "agent_waiting"
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_plan_reply_wakes_waiting_stream_without_polling(monkeypatch):
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop._agent_control_event = asyncio.Event()
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "prompt-reply"
    monkeypatch.setitem(game_routes._sessions, "prompt-reply", loop)
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2, interactive_plan=True,
    )
    waiting_task = None
    try:
        plan = await anext(events)
        assert (await anext(events))["type"] == "agent_waiting"
        waiting_task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        assert not waiting_task.done()
        await game_routes.inject_message("prompt-reply", game_routes.InjectMessageRequest(
            message="Right", stream_id="prompt-reply", plan_id=plan["plan_id"],
        ))
        assert (await asyncio.wait_for(waiting_task, timeout=0.25))["plan_id"] == plan["plan_id"]
        assert (await anext(events))["type"] == "agent_done"
    finally:
        if waiting_task is not None and not waiting_task.done():
            waiting_task.cancel()
            await asyncio.gather(waiting_task, return_exceptions=True)
        await events.aclose()


@pytest.mark.asyncio
async def test_abort_wakes_waiting_plan_immediately(monkeypatch):
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop._agent_control_event = asyncio.Event()
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "stop-plan"
    monkeypatch.setitem(game_routes._sessions, "stop-plan", loop)
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2, interactive_plan=True,
    )
    waiting_task = None
    try:
        assert (await anext(events))["type"] == "tool_call"
        assert (await anext(events))["type"] == "agent_waiting"
        waiting_task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        await game_routes.abort_generation("stop-plan", game_routes.StreamControlRequest(
            stream_id="stop-plan",
        ))
        assert (await asyncio.wait_for(waiting_task, timeout=0.25))["type"] == "aborted"
        assert loop.ai_provider.generate_with_tools.await_count == 1
    finally:
        if waiting_task is not None and not waiting_task.done():
            waiting_task.cancel()
            await asyncio.gather(waiting_task, return_exceptions=True)
        await events.aclose()


@pytest.mark.asyncio
async def test_interjection_after_stop_request_is_rejected(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "stopping-agent"
    loop._agent_stop_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "stopping-agent", loop)

    assert await game_routes.abort_generation(
        "stopping-agent", game_routes.StreamControlRequest(stream_id="stopping-agent"),
    ) == {"requested": True}
    with pytest.raises(HTTPException) as error:
        await game_routes.inject_message(
            "stopping-agent", game_routes.InjectMessageRequest(
                message="Proceed", stream_id="stopping-agent",
            ),
        )
    assert error.value.status_code == 409
    assert loop._inject_queue == []


@pytest.mark.asyncio
async def test_preplan_interjection_does_not_answer_an_unseen_question_plan(monkeypatch):
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [{"id": "first", "name": "submit_plan",
                                       "arguments": {"plan": "First", "questions": questions}}]},
        {"content": "", "tool_calls": [{"id": "second", "name": "submit_plan",
                                       "arguments": {"plan": "Revised", "questions": questions}}]},
        {"content": "Done", "tool_calls": []},
    ])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "preplan"
    monkeypatch.setitem(game_routes._sessions, "preplan", loop)
    await game_routes.inject_message("preplan", game_routes.InjectMessageRequest(
        message="Avoid the north gate", stream_id="preplan",
    ))
    holder = _AgentResult()
    events = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [],
                              lambda name, args: "Recorded", max_rounds=3,
                              interactive_plan=True, result_holder=holder)

    first = await anext(events)
    assert first["tool_name"] == "submit_plan"
    assert not first.get("show_plan")
    assert "plan_id" not in first
    assert (await anext(events)) == {
        "type": "user_inject", "label": "agent", "message": "Avoid the north gate",
    }
    second = await anext(events)
    assert second["tool_name"] == "submit_plan"
    assert second["plan_id"] == loop._pending_agent_plan_id
    assert (await anext(events))["type"] == "agent_waiting"
    assert {"role": "user", "content": "Avoid the north gate"} in (
        loop.ai_provider.generate_with_tools.await_args_list[1].args[0]
    )
    await game_routes.inject_message("preplan", game_routes.InjectMessageRequest(
        message="Right", stream_id="preplan", plan_id=second["plan_id"],
    ))
    assert [event["type"] async for event in events] == ["user_inject", "agent_done"]
    assert holder.plan_waits == 1
    assert "player_reply" not in holder.records[0]
    assert holder.records[1]["player_reply"] == "Right"
    assert holder.user_injections == [
        {"round": 2, "label": "agent", "message": "Avoid the north gate", "plan_reply": False},
        {"round": 3, "label": "agent", "message": "Right", "plan_reply": True},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_first,other_name", [
    (True, "update_state"),
    (False, "update_state"),
    (False, "finalize_turn"),
])
async def test_question_plan_defers_same_round_tools(other_first, other_name):
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    plan = {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Choose", "questions": questions}}
    other = {"id": "other", "name": other_name, "arguments": {"location_change": "archive"}}
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [other, plan] if other_first else [plan, other]},
        {"content": "Done", "tool_calls": []},
    ])
    executed = []

    def dispatch(name, args):
        executed.append(name)
        return "Recorded"

    events = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [],
                              dispatch, max_rounds=2, interactive_plan=True)
    before_reply = []
    while True:
        event = await asyncio.wait_for(anext(events), timeout=0.5)
        before_reply.append(event)
        if event["type"] == "agent_waiting":
            break
    assert executed == ["submit_plan"]
    assert sum(event.get("show_plan", False) for event in before_reply) == 1
    assert "收到玩家回复后重新调用" in next(
        event["tool_result"] for event in before_reply if event.get("tool_name") == other_name
    )
    loop._inject_queue.append("Left")
    assert [event["type"] async for event in events] == ["user_inject", "agent_done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_state", "query_lorebook"])
async def test_question_plan_does_not_wait_for_deferred_tool_preparation(tool_name):
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Choose", "questions": questions}},
            {"id": "write", "name": tool_name, "arguments": {"location_change": "archive"}},
        ]},
        {"content": "Done", "tool_calls": []},
    ])
    preparation_started = asyncio.Event()

    async def prepare():
        preparation_started.set()

    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "public system", [],
        lambda name, args: "Recorded", max_rounds=2, interactive_plan=True,
        plan_before_tools=True, before_tool_calls=prepare,
        after_plan_context=("private system", "private context"),
    )
    try:
        first = await asyncio.wait_for(anext(events), timeout=0.5)
        assert first["type"] == "tool_call" and first["tool_name"] == "submit_plan"
        assert first["show_plan"] is True
        assert not preparation_started.is_set()
        while (await asyncio.wait_for(anext(events), timeout=0.5))["type"] != "agent_waiting":
            pass
        assert not preparation_started.is_set()
        loop._inject_queue.append("Left")
        assert [event["type"] async for event in events] == ["user_inject", "agent_done"]
        assert preparation_started.is_set()
    finally:
        await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,read_executes", [
    ("update_state", False),
    ("query_lorebook", True),
])
async def test_unquestioned_plan_defers_route_preparation_until_read(
    tool_name, read_executes,
):
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Proceed", "questions": []}},
            {"id": "other", "name": tool_name, "arguments": {}},
        ]},
        {"content": "Done", "tool_calls": []},
    ])
    preparation_started = asyncio.Event()
    dispatched = []

    async def prepare():
        preparation_started.set()

    def dispatch(name, args):
        if name == "query_lorebook":
            assert not preparation_started.is_set()
        dispatched.append(name)
        return "Recorded"

    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "public system", [],
        dispatch, max_rounds=2, interactive_plan=True,
        plan_before_tools=True, before_tool_calls=prepare,
        after_plan_context=("private system", "private context"),
    )
    try:
        first = await asyncio.wait_for(anext(events), timeout=0.5)
        assert first["type"] == "tool_call" and first["tool_name"] == "submit_plan"
        assert not preparation_started.is_set()
        assert [event["type"] async for event in events] == ["tool_call", "agent_done"]
        assert preparation_started.is_set()
        assert dispatched == (["submit_plan", "query_lorebook"] if read_executes
                              else ["submit_plan"])
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_late_plan_reply_cannot_answer_a_later_plan(monkeypatch):
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [{"id": "first", "name": "submit_plan",
                                       "arguments": {"plan": "First", "questions": questions}}]},
        {"content": "", "tool_calls": [{"id": "second", "name": "submit_plan",
                                       "arguments": {"plan": "Second", "questions": questions}}]},
        {"content": "Done", "tool_calls": []},
    ])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "round-1"
    monkeypatch.setitem(game_routes._sessions, "plan-scope", loop)
    events = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [],
                              lambda name, args: "Recorded", max_rounds=3, interactive_plan=True)

    first = await anext(events)
    assert first["type"] == "tool_call"
    assert first["plan_id"]
    assert (await anext(events))["type"] == "agent_waiting"
    await game_routes.inject_message("plan-scope", game_routes.InjectMessageRequest(
        message="Left", stream_id="round-1", plan_id=first["plan_id"],
    ))
    with pytest.raises(HTTPException) as duplicate:
        await game_routes.inject_message("plan-scope", game_routes.InjectMessageRequest(
            message="Duplicate", stream_id="round-1", plan_id=first["plan_id"],
        ))
    assert duplicate.value.status_code == 409
    assert loop._inject_queue == ["Left"]
    assert (await anext(events))["type"] == "user_inject"
    second = await anext(events)
    assert second["type"] == "tool_call"
    assert second["plan_id"] != first["plan_id"]
    assert (await anext(events))["type"] == "agent_waiting"

    with pytest.raises(HTTPException) as error:
        await game_routes.inject_message("plan-scope", game_routes.InjectMessageRequest(
            message="Late first reply", stream_id="round-1", plan_id=first["plan_id"],
        ))
    assert error.value.status_code == 409
    assert loop._inject_queue == []
    await game_routes.inject_message("plan-scope", game_routes.InjectMessageRequest(
        message="Right", stream_id="round-1", plan_id=second["plan_id"],
    ))
    assert [event["type"] async for event in events] == ["user_inject", "agent_done"]


@pytest.mark.asyncio
async def test_unquestioned_plan_continues_without_confirmation(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "round-1"
    monkeypatch.setitem(game_routes._sessions, "auto-plan", loop)
    events = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [],
                              lambda name, args: "Recorded", max_rounds=2, interactive_plan=True)
    plan = await anext(events)
    assert plan["show_plan"] is True
    assert "plan_id" not in plan
    assert getattr(loop, "_pending_agent_plan_id", None) is None
    assert [event["type"] async for event in events] == ["agent_done"]
    assert loop.ai_provider.generate_with_tools.await_count == 2
    with pytest.raises(HTTPException) as error:
        await game_routes.inject_message("auto-plan", game_routes.InjectMessageRequest(
            message="Late confirmation", stream_id="round-1", plan_id="not-offered",
        ))
    assert error.value.status_code == 409
    assert loop._inject_queue == []


@pytest.mark.asyncio
async def test_plan_reply_cannot_queue_behind_another_interjection(monkeypatch):
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "round-1"
    monkeypatch.setitem(game_routes._sessions, "queued-plan", loop)
    events = loop._agent_loop([{"role": "user", "content": "Go"}], "system", [],
                              lambda name, args: "Recorded", max_rounds=2, interactive_plan=True)
    plan = await anext(events)
    assert (await anext(events))["type"] == "agent_waiting"
    await game_routes.inject_message("queued-plan", game_routes.InjectMessageRequest(
        message="Earlier interjection", stream_id="round-1",
    ))
    with pytest.raises(HTTPException) as error:
        await game_routes.inject_message("queued-plan", game_routes.InjectMessageRequest(
            message="Late answer", stream_id="round-1", plan_id=plan["plan_id"],
        ))
    assert error.value.status_code == 409
    assert loop._inject_queue == ["Earlier interjection"]
    await events.aclose()


@pytest.mark.asyncio
async def test_side_interjection_during_plan_wait_is_not_a_plan_reply(monkeypatch):
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "side-plan"
    monkeypatch.setitem(game_routes._sessions, "side-plan", loop)
    holder = _AgentResult()
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2,
        interactive_plan=True, result_holder=holder,
    )
    plan = await anext(events)
    assert plan["plan_id"]
    assert (await anext(events))["type"] == "agent_waiting"
    await game_routes.inject_message("side-plan", game_routes.InjectMessageRequest(
        message="Change direction", stream_id="side-plan",
    ))
    reply = await anext(events)
    assert reply == {"type": "user_inject", "label": "agent", "message": "Change direction"}
    assert (await anext(events))["type"] == "agent_done"
    assert "player_reply" not in holder.records[0]
    assert holder.user_injections[0]["plan_reply"] is False


@pytest.mark.asyncio
async def test_unconsumed_plan_reply_keeps_its_id_in_drop_event(monkeypatch):
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "dropped-plan"
    monkeypatch.setitem(game_routes._sessions, "dropped-plan", loop)
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2, interactive_plan=True,
    )
    try:
        plan = await anext(events)
        assert (await anext(events))["type"] == "agent_waiting"
        await game_routes.inject_message("dropped-plan", game_routes.InjectMessageRequest(
            message="Right", stream_id="dropped-plan", plan_id=plan["plan_id"],
        ))
        assert list(game_routes._close_agent_control_events(loop)) == [{
            "type": "agent_inject_dropped", "message": "Right", "plan_id": plan["plan_id"],
        }]
        assert loop._queued_agent_plan_reply_id is None
    finally:
        await events.aclose()


async def _collect_events(events):
    return [event async for event in events]


def _control_events(events):
    return [event for event in events if event["type"] != "model_waiting"]


@pytest.mark.asyncio
async def test_terminal_tool_does_not_claim_queued_interjection():
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(return_value={
        "content": "", "tool_calls": [{"id": "end", "name": "finalize_turn", "arguments": {}}],
    })
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2,
    )
    assert (await anext(events))["type"] == "tool_call"
    loop._inject_queue.append("停下")
    assert [event["type"] async for event in events] == ["agent_done"]
    assert loop._inject_queue == ["停下"]


@pytest.mark.asyncio
async def test_last_agent_round_leaves_interjection_unacknowledged():
    loop = PlanLoop([])
    loop._inject_queue.append("太晚的插话")
    events = await _collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=1,
    ))
    assert [event["type"] for event in events] == ["tool_call", "agent_max_rounds"]
    assert loop._inject_queue == ["太晚的插话"]


@pytest.mark.asyncio
async def test_plain_confirmation_outside_a_plan_reaches_the_model():
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [{"id": "lookup", "name": "query_lorebook", "arguments": {}}]},
        {"content": "The scene continues.", "tool_calls": []},
    ])
    loop._inject_queue.append("确认")
    events = await _collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2,
    ))
    assert [event["type"] for event in events] == ["tool_call", "user_inject", "agent_done"]
    assert {"role": "user", "content": "确认"} in loop.ai_provider.generate_with_tools.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_interjection_acknowledgment_waits_for_model_response():
    loop = PlanLoop([])
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def respond(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "lookup", "name": "query_lorebook", "arguments": {}}]}
        started.set()
        await finish.wait()
        return {"content": "Next scene.", "tool_calls": []}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: "Recorded", max_rounds=2,
    )
    assert (await anext(events))["type"] == "tool_call"
    loop._inject_queue.append("改变方向")
    acknowledgment = asyncio.create_task(anext(events))
    await started.wait()
    assert not acknowledgment.done()
    assert loop._inject_queue == ["改变方向"]
    assert {"role": "user", "content": "改变方向"} in calls[1]
    finish.set()
    assert (await acknowledgment)["type"] == "user_inject"
    assert loop._inject_queue == []
    assert (await anext(events))["type"] == "agent_done"


@pytest.mark.asyncio
@pytest.mark.parametrize("injections", [
    ["请改走另一条路线"], ["请改走另一条路线", "不要透露姓名"],
])
async def test_interjection_during_model_call_discards_stale_terminal_response(injections):
    loop = PlanLoop([])
    started = asyncio.Event()
    finish = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) <= len(injections):
            if len(requests) == 1:
                started.set()
                await finish.wait()
            return {"content": "", "tool_calls": [{
                "id": "stale", "name": "finalize_turn", "arguments": {"narrative": "旧决定"},
            }], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        return {"content": "", "tool_calls": [{
            "id": "fresh", "name": "finalize_turn", "arguments": {"narrative": "按插话调整"},
        }], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    async def dispatch(name, args):
        dispatched.append(args["narrative"])
        return "已完成"

    holder = _AgentResult()
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [], dispatch,
        max_rounds=len(injections) + 2, result_holder=holder,
    )
    first_event = asyncio.create_task(anext(events))
    await started.wait()
    loop._inject_queue.extend(injections)
    finish.set()
    observed = [await first_event]
    observed.extend([event async for event in events])

    assert [event["type"] for event in observed] == [
        *["user_inject"] * len(injections), "tool_call", "agent_done",
    ]
    for index, message in enumerate(injections, 1):
        assert {"role": "user", "content": message} in requests[index]
    assert dispatched == ["按插话调整"]
    assert loop._inject_queue == []
    assert holder.prompt_tokens == 2 * len(requests)


@pytest.mark.asyncio
async def test_interjection_during_last_model_call_gets_a_recovery_round():
    loop = PlanLoop([])
    started = asyncio.Event()
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            started.set()
            await release.wait()
        narrative = "新决定" if len(requests) == 2 else "旧决定"
        return {"content": "", "tool_calls": [{
            "id": narrative, "name": "finalize_turn", "arguments": {"narrative": narrative},
        }], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    holder = _AgentResult()
    stream = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(args["narrative"]),
        max_rounds=1, result_holder=holder,
    )
    consumer = asyncio.create_task(_collect_events(stream))
    await asyncio.wait_for(started.wait(), timeout=2)
    loop._inject_queue.append("改变方向")
    release.set()
    events = await asyncio.wait_for(consumer, timeout=2)

    assert {"role": "user", "content": "改变方向"} in requests[1]
    assert [event["type"] for event in events] == ["user_inject", "tool_call", "agent_done"]
    assert dispatched == ["新决定"]
    assert loop._inject_queue == []
    assert holder.model_calls == 2
    assert holder.prompt_tokens == 4
    assert holder.user_injections == [
        {"round": 2, "label": "agent", "message": "改变方向", "plan_reply": False},
    ]


@pytest.mark.asyncio
async def test_last_round_interjections_stop_after_bounded_retries_without_stale_tools():
    loop = PlanLoop([])
    started = asyncio.Queue()
    release = asyncio.Queue()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        await started.put(list(messages))
        await release.get()
        return {"content": "", "tool_calls": [{
            "id": "stale", "name": "finalize_turn", "arguments": {"narrative": "旧决定"},
        }], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    holder = _AgentResult()
    stream = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(args["narrative"]),
        max_rounds=1, result_holder=holder,
    )
    consumer = asyncio.create_task(_collect_events(stream))
    for index in range(1, 5):
        messages = await asyncio.wait_for(started.get(), timeout=2)
        if index > 1:
            assert {"role": "user", "content": f"插话{index - 1}"} in messages
        loop._inject_queue.append(f"插话{index}")
        release.put_nowait(None)
    events = await asyncio.wait_for(consumer, timeout=2)

    assert [event["message"] for event in events if event["type"] == "user_inject"] == [
        "插话1", "插话2", "插话3",
    ]
    assert events[-1] == {"type": "agent_max_rounds", "label": "agent", "max_rounds": 4}
    assert dispatched == []
    assert loop._inject_queue == ["插话4"]
    assert holder.model_calls == 4
    assert holder.prompt_tokens == 8
    assert [entry["message"] for entry in holder.user_injections] == ["插话1", "插话2", "插话3"]


@pytest.mark.asyncio
async def test_stop_during_model_call_skips_terminal_tool():
    loop = PlanLoop([])
    started = asyncio.Event()
    finish = asyncio.Event()
    dispatched = []

    async def terminal_response(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return {"content": "", "tool_calls": [{"id": "end", "name": "finalize_turn", "arguments": {}}]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=terminal_response)

    async def collect():
        return await _collect_events(loop._agent_loop(
            [{"role": "user", "content": "Go"}], "system", [],
            lambda name, args: dispatched.append(name), max_rounds=2,
        ))

    task = asyncio.create_task(collect())
    await started.wait()
    loop._abort_flag = True
    finish.set()
    events = await task
    assert [event["type"] for event in events] == ["aborted"]
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("simultaneous_interjection", [False, True])
async def test_stream_stop_cancels_model_call_without_waiting_for_provider(
    monkeypatch, simultaneous_interjection,
):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "model-stop"
    loop._agent_stop_event = asyncio.Event()
    if simultaneous_interjection:
        loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "model-stop", loop)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    dispatched = []

    async def slow_response(*_args, **_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"content": "", "tool_calls": [{"id": "end", "name": "finalize_turn", "arguments": {}}]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=slow_response)
    task = asyncio.create_task(_collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(name), max_rounds=2,
    )))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        if simultaneous_interjection:
            await game_routes.inject_message(
                "model-stop", game_routes.InjectMessageRequest(
                    message="停下", stream_id="model-stop",
                ),
            )
        assert await game_routes.abort_generation(
            "model-stop", game_routes.StreamControlRequest(stream_id="model-stop"),
        ) == {"requested": True}
        assert [event["type"] for event in _control_events(
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        )] == ["aborted"]
        assert cancelled.is_set()
        assert dispatched == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_interjection_cancels_stale_model_call_immediately(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "model-inject"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "model-inject", loop)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            narrative = "旧决定"
        else:
            narrative = "按插话调整"
        return {"content": "", "tool_calls": [{
            "id": narrative, "name": "finalize_turn", "arguments": {"narrative": narrative},
        }]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    holder = _AgentResult()
    task = asyncio.create_task(_collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(args["narrative"]),
        max_rounds=1, result_holder=holder,
    )))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert await game_routes.inject_message(
            "model-inject", game_routes.InjectMessageRequest(
                message="不要去车站", stream_id="model-inject",
            ),
        ) == {"queued": True}
        events = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        assert cancelled.is_set()
        assert [event["type"] for event in _control_events(events)] == ["user_inject", "tool_call", "agent_done"]
        assert {"role": "user", "content": "不要去车站"} in requests[1]
        assert dispatched == ["按插话调整"]
        assert loop._inject_queue == []
        assert holder.model_calls == 2
        assert len(holder.model_timings_ms) == 2
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_interjections_share_one_cancelled_model_call(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "model-inject-batch"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "model-inject-batch", loop)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return {"content": "", "tool_calls": [{
            "id": "end", "name": "finalize_turn", "arguments": {"narrative": "按插话调整"},
        }]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    task = asyncio.create_task(_collect_events(loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(args["narrative"]), max_rounds=1,
    )))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.5)
        for message in ("不要去车站", "先找同伴"):
            assert await game_routes.inject_message(
                "model-inject-batch", game_routes.InjectMessageRequest(
                    message=message, stream_id="model-inject-batch",
                ),
            ) == {"queued": True}
        events = await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        assert cancelled.is_set()
        assert [event["message"] for event in events if event["type"] == "user_inject"] == [
            "不要去车站", "先找同伴",
        ]
        assert [event["type"] for event in _control_events(events)] == [
            "user_inject", "user_inject", "tool_call", "agent_done",
        ]
        assert len(requests) == 2
        assert {"role": "user", "content": "不要去车站"} in requests[1]
        assert {"role": "user", "content": "先找同伴"} in requests[1]
        assert dispatched == ["按插话调整"]
        assert loop._inject_queue == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_response", [False, True])
async def test_stream_interjection_is_acknowledged_only_after_retried_response(
    monkeypatch, stop_before_response,
):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "model-inject-ack"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "model-inject-ack", loop)
    first_started = asyncio.Event()
    retry_started = asyncio.Event()
    first_release = asyncio.Event()
    retry_release = asyncio.Event()
    observed = []
    dispatched = []

    async def respond(messages, **_kwargs):
        if not retry_started.is_set():
            if not first_started.is_set():
                first_started.set()
                await first_release.wait()
            else:
                assert {"role": "user", "content": "不要去车站"} in messages
                retry_started.set()
                await retry_release.wait()
        return {"content": "", "tool_calls": [{
            "id": "end", "name": "finalize_turn", "arguments": {"narrative": "新决定"},
        }]}

    async def consume():
        async for event in loop._agent_loop(
            [{"role": "user", "content": "Go"}], "system", [],
            lambda name, args: dispatched.append(args["narrative"]), max_rounds=1,
        ):
            observed.append(event)

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await game_routes.inject_message(
            "model-inject-ack", game_routes.InjectMessageRequest(
                message="不要去车站", stream_id="model-inject-ack",
            ),
        )
        await asyncio.wait_for(retry_started.wait(), timeout=0.5)
        assert not _control_events(observed)
        assert loop._inject_queue == ["不要去车站"]
        if stop_before_response:
            await game_routes.abort_generation(
                "model-inject-ack", game_routes.StreamControlRequest(stream_id="model-inject-ack"),
            )
            await asyncio.wait_for(task, timeout=0.5)
            assert [event["type"] for event in _control_events(observed)] == ["aborted"]
            assert dispatched == []
            assert list(game_routes._close_agent_control_events(loop))[0] == {
                "type": "agent_inject_dropped", "message": "不要去车站",
            }
        else:
            retry_release.set()
            await asyncio.wait_for(task, timeout=0.5)
            assert [event["type"] for event in _control_events(observed)] == ["user_inject", "tool_call", "agent_done"]
            assert loop._inject_queue == []
            assert dispatched == ["新决定"]
    finally:
        first_release.set()
        retry_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_second_interjection_cancels_retry_without_duplicate_ack(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "model-inject-again"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "model-inject-again", loop)
    started = asyncio.Queue()
    release = asyncio.Event()
    requests = []
    cancelled = []
    observed = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        call_number = len(requests)
        if call_number < 3:
            started.put_nowait(call_number)
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(call_number)
                raise
        return {"content": "", "tool_calls": [{
            "id": "end", "name": "finalize_turn", "arguments": {"narrative": "新决定"},
        }]}

    async def consume():
        async for event in loop._agent_loop(
            [{"role": "user", "content": "Go"}], "system", [],
            lambda name, args: dispatched.append(args["narrative"]), max_rounds=1,
        ):
            observed.append(event)

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    task = asyncio.create_task(consume())
    try:
        for call_number, message in enumerate(("改走小路", "不要透露姓名"), 1):
            assert await asyncio.wait_for(started.get(), timeout=0.5) == call_number
            if call_number == 2:
                assert {"role": "user", "content": "改走小路"} in requests[1]
                assert not _control_events(observed)
            await game_routes.inject_message(
                "model-inject-again", game_routes.InjectMessageRequest(
                    message=message, stream_id="model-inject-again",
                ),
            )
        await asyncio.wait_for(task, timeout=0.5)
        assert cancelled == [1, 2]
        assert {"role": "user", "content": "改走小路"} in requests[2]
        assert {"role": "user", "content": "不要透露姓名"} in requests[2]
        assert [event["message"] for event in observed if event["type"] == "user_inject"] == [
            "改走小路", "不要透露姓名",
        ]
        assert dispatched == ["新决定"]
        assert loop._inject_queue == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_interjection_during_plan_reply_cancels_stale_reply_model(monkeypatch):
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "plan-reply-interjection"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "plan-reply-interjection", loop)
    reply_started = asyncio.Event()
    reply_cancelled = asyncio.Event()
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            return {"content": "", "tool_calls": [{
                "id": "plan", "name": "submit_plan",
                "arguments": {"plan": "Proceed", "questions": questions},
            }]}
        if len(requests) == 2:
            reply_started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                reply_cancelled.set()
                raise
        return {"content": "", "tool_calls": [{
            "id": "end", "name": "finalize_turn", "arguments": {"narrative": "改走小路"},
        }]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    holder = _AgentResult()
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(name) or "Recorded",
        max_rounds=2, interactive_plan=True, result_holder=holder,
    )
    plan = await anext(events)
    assert plan["plan_id"]
    assert (await anext(events))["type"] == "agent_waiting"
    observed = []

    async def consume():
        async for event in events:
            observed.append(event)

    task = None
    try:
        await game_routes.inject_message(
            "plan-reply-interjection", game_routes.InjectMessageRequest(
                message="Right", stream_id="plan-reply-interjection", plan_id=plan["plan_id"],
            ),
        )
        task = asyncio.create_task(consume())
        await asyncio.wait_for(reply_started.wait(), timeout=0.5)
        await game_routes.inject_message(
            "plan-reply-interjection", game_routes.InjectMessageRequest(
                message="但不要透露姓名", stream_id="plan-reply-interjection",
            ),
        )
        await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        assert reply_cancelled.is_set()
        assert {"role": "user", "content": "Right"} in requests[2]
        assert {"role": "user", "content": "但不要透露姓名"} in requests[2]
        control_events = _control_events(observed)
        assert [event["type"] for event in control_events] == ["user_inject", "user_inject", "tool_call", "agent_done"]
        assert control_events[0]["plan_id"] == plan["plan_id"]
        assert "plan_id" not in control_events[1]
        assert [entry["plan_reply"] for entry in holder.user_injections] == [True, False]
        assert loop._inject_queue == []
        assert dispatched == ["submit_plan", "finalize_turn"]
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await events.aclose()


@pytest.mark.asyncio
async def test_new_interjection_at_ack_boundary_replays_before_terminal_tool(monkeypatch):
    loop = PlanLoop([])
    loop._active_agent_stream = True
    loop._active_agent_stream_id = "ack-boundary"
    loop._agent_stop_event = asyncio.Event()
    loop._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "ack-boundary", loop)
    first_started = asyncio.Event()
    release = asyncio.Event()
    requests = []
    dispatched = []

    async def respond(messages, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            first_started.set()
            await release.wait()
        narrative = "新决定" if len(requests) == 3 else "旧决定"
        return {"content": "", "tool_calls": [{
            "id": narrative, "name": "finalize_turn", "arguments": {"narrative": narrative},
        }]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(args["narrative"]), max_rounds=1,
    )
    first_event = asyncio.create_task(anext(events))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await game_routes.inject_message(
            "ack-boundary", game_routes.InjectMessageRequest(
                message="改走小路", stream_id="ack-boundary",
            ),
        )
        assert (await asyncio.wait_for(first_event, timeout=0.5))["type"] == "model_waiting"
        assert (await asyncio.wait_for(anext(events), timeout=0.5))["message"] == "改走小路"
        await game_routes.inject_message(
            "ack-boundary", game_routes.InjectMessageRequest(
                message="不要透露姓名", stream_id="ack-boundary",
            ),
        )
        remaining = await asyncio.wait_for(_collect_events(events), timeout=0.5)
        assert [event["type"] for event in _control_events(remaining)] == ["user_inject", "tool_call", "agent_done"]
        assert {"role": "user", "content": "不要透露姓名"} in requests[2]
        assert dispatched == ["新决定"]
        assert loop._inject_queue == []
    finally:
        release.set()
        if not first_event.done():
            first_event.cancel()
        await asyncio.gather(first_event, return_exceptions=True)
        await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("questions", [[], [{"question": "Where?", "options": ["Left", "Right"]}]])
async def test_stream_emits_plan_card_without_plan_text(questions):
    loop = PlanLoop(questions, plan="")
    if not questions:
        loop.ai_provider.generate_with_tools.side_effect = [
            {"content": "", "tool_calls": [{"id": "plan-1", "name": "submit_plan",
                                           "arguments": {"plan": "", "questions": questions}}]},
            {"content": "", "tool_calls": [{"id": "end", "name": "finalize_turn", "arguments": {}}]},
        ]
    loop.current_state = {}
    loop._reset_stage45_tool_buffers = lambda: None
    loop._build_unified_system = lambda: "system"
    loop._build_unified_context = lambda ctx, action: "Go"
    loop._build_adaptive_tools = lambda ctx: []
    loop._dispatch_unified_tool = AsyncMock(return_value="Recorded")

    pipeline = loop._execute_unified_agent(
        {"action_text": "Go"}, {}, {"text": "Go"}, streaming=True,
    )
    try:
        started = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        assert started["type"] == "tool_started"
        assert started["tool_name"] == "submit_plan"
        event = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        expected = {"type": "agent_plan", "plan": "", "questions": questions}
        if questions:
            expected["plan_id"] = loop._pending_agent_plan_id
        assert event == expected
        next_event = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        if not questions:
            assert next_event["type"] == "tool_started"
            assert next_event["tool_name"] == "finalize_turn"
            next_event = await asyncio.wait_for(anext(pipeline), timeout=0.5)
            assert next_event["type"] == "tool_call"
            assert next_event["tool_name"] == "finalize_turn"
            next_event = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        assert next_event["type"] == ("agent_waiting" if questions else "agent_done")
        assert loop.ai_provider.generate_with_tools.await_count == (1 if questions else 2)
    finally:
        await pipeline.aclose()


@pytest.mark.asyncio
async def test_same_round_multiple_plans_only_offer_the_final_one():
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(return_value={
        "content": "", "tool_calls": [
            {"id": "first", "name": "submit_plan", "arguments": {"plan": "First", "questions": []}},
            {"id": "second", "name": "submit_plan", "arguments": {"plan": "Second", "questions": questions}},
        ],
    })
    loop.current_state = {}
    loop._reset_stage45_tool_buffers = lambda: None
    loop._build_unified_system = lambda: "system"
    loop._build_unified_context = lambda ctx, action: "Go"
    loop._build_adaptive_tools = lambda ctx: []
    loop._dispatch_unified_tool = AsyncMock(return_value="Recorded")

    pipeline = loop._execute_unified_agent(
        {"action_text": "Go"}, {}, {"text": "Go"}, streaming=True,
    )
    try:
        started = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        assert started["type"] == "tool_started"
        assert started["tool_name"] == "submit_plan"
        event = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        assert event["type"] == "agent_plan"
        assert event["plan"] == "Second"
        assert event["questions"] == questions
        assert event["plan_id"] == loop._pending_agent_plan_id
        waiting = await asyncio.wait_for(anext(pipeline), timeout=0.5)
        assert waiting["type"] == "agent_waiting"
        assert waiting["reason"] == "等待用户选择"
    finally:
        await pipeline.aclose()


@pytest.mark.asyncio
async def test_unquestioned_plan_can_read_before_private_context_but_not_write():
    loop = PlanLoop([])
    responses = [
        {"content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Check the archive", "questions": []}},
            {"id": "read", "name": "query_lorebook", "arguments": {"entry_id": "archive"}},
            {"id": "write", "name": "update_state", "arguments": {"location_change": "archive"}},
        ]},
        {"content": "", "tool_calls": [
            {"id": "end", "name": "finalize_turn", "arguments": {}},
        ]},
    ]
    requests = []
    tools = [{"function": {"name": name}} for name in (
        "submit_plan", "query_lorebook", "update_state", "finalize_turn",
    )]

    async def respond(messages, **kwargs):
        requests.append((kwargs["system"], [dict(message) for message in messages],
                         [tool["function"]["name"] for tool in kwargs["tools"]]))
        if len(requests) == 1:
            return responses[0]
        if {"role": "tool", "tool_call_id": "read", "content": "private archive details"} in requests[-1][1]:
            return responses[1]
        return {"content": "", "tool_calls": [
            {"id": "retry-read", "name": "query_lorebook", "arguments": {"entry_id": "archive"}},
        ]}

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    dispatched = []
    holder = _AgentResult()

    def dispatch(name, args):
        dispatched.append(name)
        return "private archive details" if name == "query_lorebook" else "Recorded"

    events = await _collect_events(loop._agent_loop(
        [{"role": "user", "content": "public action"}], "public system", tools,
        dispatch, max_rounds=2, interactive_plan=True, plan_before_tools=True,
        after_plan_context=lambda: ("private system", "private context"), result_holder=holder,
    ))

    assert [event["tool_name"] for event in events if event["type"] == "tool_call"] == [
        "submit_plan", "query_lorebook", "update_state", "finalize_turn",
    ]
    assert dispatched == ["submit_plan", "query_lorebook", "finalize_turn"]
    assert events[0]["show_plan"] is True
    assert holder.records[1].get("deferred") is None
    assert holder.records[2]["deferred"] is True
    assert "下一轮完整推演上下文" in holder.records[2]["result"]
    assert holder.model_calls == 2
    assert requests[0][0] == "public system"
    assert requests[0][1][0]["content"] == "public action"
    assert requests[0][2] == ["submit_plan", "query_lorebook"]
    assert requests[1][0] == "private system"
    assert requests[1][1][0]["content"] == "private context"
    assert requests[1][2] == ["submit_plan", "query_lorebook", "update_state", "finalize_turn"]
    assert {"role": "tool", "tool_call_id": "read", "content": "private archive details"} in requests[1][1]


@pytest.mark.asyncio
async def test_question_plan_still_defers_same_round_read_before_reply():
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Ask", "questions": questions}},
            {"id": "read", "name": "query_lorebook", "arguments": {"entry_id": "archive"}},
        ]},
        {"content": "Done", "tool_calls": []},
    ])
    dispatched = []
    tools = [{"function": {"name": name}} for name in (
        "submit_plan", "query_lorebook", "write_narrative", "finalize_turn",
    )]
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "public system", tools,
        lambda name, args: dispatched.append(name) or "Recorded",
        max_rounds=2, interactive_plan=True, plan_before_tools=True,
        after_plan_context=("private system", "private context"),
    )
    try:
        plan = await anext(events)
        read = await anext(events)
        assert plan["plan_id"]
        assert read["tool_name"] == "query_lorebook"
        assert "收到玩家回复后重新调用" in read["tool_result"]
        assert (await anext(events))["type"] == "agent_waiting"
        assert dispatched == ["submit_plan"]
        assert loop.ai_provider.generate_with_tools.await_count == 1
        assert [tool["function"]["name"] for tool in loop.ai_provider.generate_with_tools.call_args.kwargs["tools"]] == [
            "submit_plan", "query_lorebook",
        ]
        loop._inject_queue.append("Left")
        assert [event["type"] async for event in events] == ["user_inject", "agent_done"]
        assert [tool["function"]["name"] for tool in loop.ai_provider.generate_with_tools.call_args.kwargs["tools"]] == [
            "submit_plan", "query_lorebook", "write_narrative", "finalize_turn",
        ]
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_same_round_terminal_is_deferred_after_public_plan():
    loop = PlanLoop([])
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[{
        "content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Already over"}},
            {"id": "end", "name": "finalize_turn", "arguments": {}},
        ],
    }, {"content": "", "tool_calls": [
        {"id": "retry", "name": "finalize_turn", "arguments": {}},
    ]}])
    loop.current_state = {}
    loop._reset_stage45_tool_buffers = lambda: None
    loop._build_unified_system = lambda: "system"
    loop._build_unified_context = lambda ctx, action: "Go"
    loop._build_adaptive_tools = lambda ctx: []
    loop._dispatch_unified_tool = AsyncMock(return_value="Recorded")

    pipeline = loop._execute_unified_agent(
        {"action_text": "Go"}, {}, {"text": "Go"}, streaming=True,
    )
    try:
        events = [await asyncio.wait_for(anext(pipeline), timeout=0.5) for _ in range(6)]
        assert [event["type"] for event in events] == [
            "tool_started", "agent_plan", "tool_call", "tool_started", "tool_call", "agent_done",
        ]
        assert events[1]["plan"] == "Already over"
        assert "error" in events[2]["tool_result"]
        assert [event["tool_name"] for event in events if event["type"] == "tool_started"] == [
            "submit_plan", "finalize_turn",
        ]
        assert loop._dispatch_unified_tool.await_count == 2
        assert getattr(loop, "_pending_agent_plan_id", None) is None
    finally:
        await pipeline.aclose()


@pytest.mark.asyncio
async def test_last_round_question_plan_waits_before_batched_settlement():
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [
            {"id": "plan", "name": "submit_plan", "arguments": {"plan": "Ask first", "questions": questions}},
            {"id": "early", "name": "finalize_turn", "arguments": {"narrative": "Unconfirmed"}},
        ]},
        {"content": "", "tool_calls": [
            {"id": "settle", "name": "finalize_turn", "arguments": {"narrative": "Confirmed"}},
        ]},
    ])
    dispatched = []
    holder = _AgentResult()
    events = loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append((name, args)) or "Recorded",
        max_rounds=1, interactive_plan=True, result_holder=holder,
    )
    try:
        plan = await asyncio.wait_for(anext(events), timeout=0.5)
        deferred = await asyncio.wait_for(anext(events), timeout=0.5)
        waiting = await asyncio.wait_for(anext(events), timeout=0.5)
        assert plan["tool_name"] == "submit_plan" and plan["plan_id"]
        assert deferred["tool_name"] == "finalize_turn"
        assert waiting["type"] == "agent_waiting"
        assert [name for name, _ in dispatched] == ["submit_plan"]

        loop._inject_queue.append("Right")
        remaining = await asyncio.wait_for(_collect_events(events), timeout=0.5)
        assert [event["type"] for event in remaining] == ["user_inject", "tool_call", "agent_done"]
        assert [args["narrative"] for name, args in dispatched if name == "finalize_turn"] == ["Confirmed"]
        assert holder.records[1]["deferred"] is True
        assert "duration_ms" not in holder.records[1]
        assert holder.plan_waits == 1
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_repeated_last_round_questions_exhaust_budget_without_settling():
    questions = [{"question": "Where?", "options": ["Left", "Right"]}]
    loop = PlanLoop(questions)
    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=[
        {"content": "", "tool_calls": [
            {"id": f"plan-{index}", "name": "submit_plan",
             "arguments": {"plan": "Ask first", "questions": questions}},
            {"id": f"early-{index}", "name": "finalize_turn",
             "arguments": {"narrative": "Unconfirmed"}},
        ]} for index in range(4)
    ])
    dispatched = []
    holder = _AgentResult()
    observed = []
    async for event in loop._agent_loop(
        [{"role": "user", "content": "Go"}], "system", [],
        lambda name, args: dispatched.append(name) or "Recorded",
        max_rounds=1, interactive_plan=True, result_holder=holder,
    ):
        observed.append(event)
        if event["type"] == "agent_waiting":
            loop._inject_queue.append("Right")

    assert [event["type"] for event in observed].count("agent_waiting") == 3
    assert observed[-1]["type"] == "agent_max_rounds"
    assert "agent_done" not in [event["type"] for event in observed]
    assert dispatched == ["submit_plan"] * 4
    assert holder.records[-1]["deferred"] is True
    assert holder.plan_waits == 3


@pytest.mark.asyncio
async def test_last_round_plan_does_not_offer_an_unanswerable_card():
    loop = PlanLoop([{"question": "Where?", "options": ["Left", "Right"]}])
    loop.current_state = {}
    loop._reset_stage45_tool_buffers = lambda: None
    loop._build_unified_system = lambda: "system"
    loop._build_unified_context = lambda ctx, action: "Go"
    loop._build_adaptive_tools = lambda ctx: []

    async def last_round(*_args, **_kwargs):
        yield {"type": "tool_call", "tool_name": "submit_plan", "round": 20,
               "tool_args": {"plan": "Proceed", "questions": [{"question": "Where?"}]}}
        yield {"type": "agent_max_rounds", "max_rounds": 20}

    loop._agent_loop = last_round
    pipeline = loop._execute_unified_agent({"action_text": "Go"}, {}, {"text": "Go"}, streaming=True)
    try:
        assert (await anext(pipeline))["type"] == "agent_max_rounds"
    finally:
        await pipeline.aclose()
