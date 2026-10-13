import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _AgentResult, _bound_tool_messages
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import CATEGORIES, large_session, model_call, tool_call
from test_agent_state_compaction import compact


PLAN = "Inspect only the synthetic area. " * 45 + "Keep the sealed container closed."
QUESTIONS = [
    {"question": "Which route?", "options": ["Synthetic northern route", "Synthetic southern route"]},
    {"question": "Which evidence?", "options": ["Synthetic blue record", "Synthetic green record"]},
]
REPLY = "第二项；证据选第一项。不要透露身份。"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_questions", [False, True])
@pytest.mark.parametrize("repeat", [False, True])
async def test_plan_feedback_survives_repeated_context_compression(has_questions, repeat):
    session = large_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(
        return_value={"content": "Done.", "tool_calls": []},
    ))
    context = {}
    arguments = {"plan": PLAN, "questions": QUESTIONS if has_questions else []}
    receipt = await session._dispatch_unified_tool(context, "submit_plan", arguments)
    original_context = copy.deepcopy(context)
    summary = await compact(session, [("submit_plan", arguments, receipt)])
    if repeat:
        summary = await compact(session, [], previous_summary=summary)

    assert receipt.replace("\n", "\\n") in summary
    assert summary.count("[计划反馈] ") == 1
    assert context == original_context


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_first", [False, True])
@pytest.mark.parametrize("has_questions", [False, True])
async def test_hard_budget_prioritizes_plan_feedback(plan_first, has_questions):
    session = large_session()
    arguments = {"plan": PLAN, "questions": QUESTIONS if has_questions else []}
    receipt = await session._dispatch_unified_tool({}, "submit_plan", arguments)
    plan_result = {"role": "tool", "tool_call_id": "synthetic-plan", "content": receipt}
    lookup_result = {"role": "tool", "tool_call_id": "synthetic-notes", "content": "Synthetic lookup. " * 3000}
    messages = [
        {"role": "user", "content": REPLY},
        {"role": "assistant", "content": "", "tool_calls": [
            tool_call("submit_plan", arguments, "synthetic-plan"),
            tool_call("read_director_notes", {}, "synthetic-notes"),
        ]},
        *([plan_result, lookup_result] if plan_first else [lookup_result, plan_result]),
    ]
    original = copy.deepcopy(messages)

    bounded = _bound_tool_messages(messages, 20000)

    assert next(message for message in bounded if message.get("tool_call_id") == "synthetic-plan") == plan_result
    assert bounded[:2] == original[:2]
    assert len(next(message["content"] for message in bounded
                    if message.get("tool_call_id") == "synthetic-notes")) < len(lookup_result["content"])
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in bounded) <= 20000


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("has_questions", [False, True])
async def test_actual_plan_reply_survives_repeated_rule_lookups(monkeypatch, stream, has_questions):
    session = large_session()
    session._active_agent_stream = True
    session._active_agent_stream_id = "synthetic-plan-context-stream"
    session._agent_inject_enabled = True
    session._agent_stop_event = asyncio.Event()
    session._agent_control_event = asyncio.Event()
    monkeypatch.setitem(game_routes._sessions, "synthetic-plan-context-save", session)
    original_state = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(session.script)
    context = {}
    requests = []
    arguments = {"plan": PLAN, "questions": QUESTIONS if has_questions else []}
    private_head = {"role": "user", "content": "Synthetic private context after planning."}

    async def respond(messages, **_kwargs):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            calls = [model_call("submit_plan", arguments, "synthetic-plan")]
        elif len(requests) in (2, 3):
            calls = [model_call("consult_rules", {"category": category}, f"synthetic-read-{len(requests)}-{category}")
                     for category in CATEGORIES]
        else:
            return {"content": "Synthetic lookup complete.", "tool_calls": []}
        return {"content": "", "tool_calls": calls}

    async def streamed(messages, **kwargs):
        yield {"type": "model_output_started"}
        yield {"type": "response", "response": await respond(messages, **kwargs)}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    if stream:
        provider.generate_with_tools_stream = streamed
    session.ai_provider = provider
    holder = _AgentResult()
    tools = [tool for tool in _unified_tools() if tool["function"]["name"] in ("submit_plan", "consult_rules")]
    events = []

    async def collect():
        async for event in session._agent_loop(
                [{"role": "user", "content": "Inspect the synthetic area."}], "Synthetic system.", tools,
                lambda name, payload: session._dispatch_unified_tool(context, name, payload),
                max_rounds=4, result_holder=holder, interactive_plan=True, plan_before_tools=True,
                after_plan_context=("Synthetic private system.", private_head["content"]),
                on_plan_reply=lambda reply: context.__setitem__("_plan_reply", reply)):
            events.append(event)
            if (has_questions and event["type"] == "agent_waiting"
                    or not has_questions and event["type"] == "tool_call" and event["tool_name"] == "submit_plan"):
                queued = await game_routes.inject_message("synthetic-plan-context-save", game_routes.InjectMessageRequest(
                    message=REPLY, stream_id=session._active_agent_stream_id,
                    plan_id=session._pending_agent_plan_id if has_questions else None,
                ))
                assert queued == {"queued": True}

    await asyncio.wait_for(collect(), timeout=5)

    assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
    assert len(requests) == holder.model_calls == 4
    plan_record, = [record for record in holder.records if record["name"] == "submit_plan"]
    for messages in requests[2:]:
        summary, = [message["content"] for message in messages if (message.get("content") or "").startswith("[上下文压缩]")]
        assert plan_record["result"].replace("\n", "\\n") in summary
        assert summary.count("[计划反馈] ") == 1
        assert summary.count(REPLY) == 1
        assert messages[0] == private_head
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= 30000
        call_ids = {call["id"] for message in messages for call in message.get("tool_calls", [])}
        assert all(message["tool_call_id"] in call_ids for message in messages if message["role"] == "tool")
    assert holder.plan_waits == int(has_questions)
    assert holder.user_injections == [{"round": 2, "label": "agent", "message": REPLY, "plan_reply": has_questions}]
    assert context["_agent_plan"] == PLAN
    assert context["_agent_plan_questions"] == arguments["questions"]
    assert context.get("_plan_reply") == (REPLY if has_questions else None)
    assert session._inject_queue == []
    assert session.current_state == original_state
    assert session.script == original_script
    assert holder.text == "Synthetic lookup complete."
    assert provider.generate_with_tools.await_count == (0 if stream else 4)


@pytest.mark.asyncio
async def test_rejected_plan_feedback_is_not_recorded_as_accepted():
    session = large_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(
        return_value={"content": "Done.", "tool_calls": []},
    ))
    context = {}
    arguments = {"plan": PLAN, "questions": [{"question": "Synthetic invalid question", "options": ["Only option"]}]}
    receipt = await session._dispatch_unified_tool(context, "submit_plan", arguments)
    assert "error" in json.loads(receipt)
    summary = await compact(session, [("submit_plan", arguments, receipt)])
    assert receipt in summary
    assert PLAN not in summary
    assert context == {}
