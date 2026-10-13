import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.openai_provider import OpenAIProvider
from ai.base import _is_retryable


def chunk(*, content=None, reasoning=None, tool_calls=None, usage=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)] if usage is None else [], usage=usage)


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.chunks.pop(0)


@pytest.mark.asyncio
async def test_streamed_tool_response_waits_for_complete_arguments():
    def tool(index, call_id=None, name=None, arguments=None):
        return SimpleNamespace(index=index, id=call_id,
                               function=SimpleNamespace(name=name, arguments=arguments))

    stream = FakeStream([
        chunk(reasoning="查阅资料。", tool_calls=[tool(0, "call-1", "query_lorebook", '{"entry')]),
        chunk(tool_calls=[tool(0, arguments='_id":"demo"}')]),
        chunk(content="已查证。"),
        chunk(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8)),
    ])
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "test", "model": "test"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    tools = [{"type": "function", "function": {"name": "query_lorebook"}}]

    events = [event async for event in provider.generate_with_tools_stream(
        [{"role": "user", "content": "查阅"}], tools=tools)]

    assert events == [
        {"type": "model_output_started"},
        {"type": "tool_preparing", "tool_name": "query_lorebook"},
        {"type": "response", "response": {
            "content": "已查证。", "reasoning_content": "查阅资料。",
            "tool_calls": [{"id": "call-1", "name": "query_lorebook", "arguments": {"entry_id": "demo"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }},
    ]
    assert create.await_args.kwargs["stream"] is True
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_does_not_emit_partial_text_or_unknown_tool_names():
    def tool(name, arguments):
        return SimpleNamespace(index=0, id=None,
                               function=SimpleNamespace(name=name, arguments=arguments))

    stream = FakeStream([
        chunk(content="<think>private</think>已", tool_calls=[tool("query_", None)]),
        chunk(content="核实。", tool_calls=[tool("lorebook", None)]),
        chunk(tool_calls=[tool(None, "{}")]),
    ])
    provider = OpenAIProvider({"api_key": "test"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=stream))))

    events = [event async for event in provider.generate_with_tools_stream(
        [], tools=[{"function": {"name": "query_lorebook"}}])]

    assert [event["type"] for event in events] == ["model_output_started", "tool_preparing", "response"]
    assert events[0] == {"type": "model_output_started"}
    assert events[-1]["response"]["content"] == "已核实。"
    assert events[-1]["response"]["tool_calls"][0]["name"] == "query_lorebook"


@pytest.mark.asyncio
async def test_stream_options_rejection_falls_back_to_complete_response():
    provider = OpenAIProvider({"api_key": "test"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("stream_options unsupported")))))
    response = {"content": "完成", "tool_calls": None, "usage": {"prompt_tokens": 1}}
    provider.generate_with_tools = AsyncMock(return_value=response)

    events = [event async for event in provider.generate_with_tools_stream([], tools=[])]

    assert events == [{"type": "response", "response": response}]
    provider.generate_with_tools.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "stop", None])
@pytest.mark.parametrize("include_usage", [False, True])
async def test_reasoning_only_stream_preserves_safe_terminal_diagnostics(finish_reason, include_usage):
    reasoning = "SYNTHETIC_HIDDEN_REASONING"
    chunks = [chunk(reasoning=reasoning), chunk(finish_reason=finish_reason)]
    if include_usage:
        chunks.append(chunk(usage=SimpleNamespace(
            prompt_tokens=11, completion_tokens=37, completion_tokens_details=SimpleNamespace(reasoning_tokens=37),
        )))
    stream = FakeStream(chunks)
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "synthetic-credential", "model": "synthetic-model"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = []
    with pytest.raises(RuntimeError) as caught:
        async for event in provider.generate_with_tools_stream([], max_tokens=500):
            events.append(event)
    error = caught.value
    assert type(error).__name__ == "ModelOutputUnavailableError"
    assert error.reason == "reasoning_only"
    assert error.diagnostics == {
        "chunk_count": 3 if include_usage else 2,
        "choice_chunk_count": 2,
        "raw_content_characters": 0,
        "visible_content_characters": 0,
        "reasoning_characters": len(reasoning),
        "tool_call_count": 0,
        "requested_output_limit": 500,
        "prompt_tokens": 11 if include_usage else None,
        "completion_tokens": 37 if include_usage else None,
        "reasoning_tokens": 37 if include_usage else None,
        "finish_reasons": [finish_reason] if finish_reason else [],
    }
    assert ("长度限制" in error.public_message) is (finish_reason == "length")
    assert "choices 为空" not in str(error)
    assert reasoning not in str(error)
    assert not _is_retryable(error)
    assert events == [{"type": "model_output_started"}]
    assert create.await_count == 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_chunks", "usage_only", "role_only", "whitespace", "inline_think", "unclosed_think"])
async def test_stream_without_visible_output_cannot_return_a_successful_response(kind):
    chunks = {
        "no_chunks": [],
        "usage_only": [chunk(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=0))],
        "role_only": [chunk(finish_reason="stop")],
        "whitespace": [chunk(content="   \n")],
        "inline_think": [chunk(content="<think>SYNTHETIC_HIDDEN_REASONING</think>")],
        "unclosed_think": [chunk(content="<think>SYNTHETIC_HIDDEN_REASONING")],
    }[kind]
    stream = FakeStream(chunks)
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "synthetic-credential"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(RuntimeError) as caught:
        async for event in provider.generate_with_tools_stream([]):
            assert event["type"] != "response"
    assert type(caught.value).__name__ == "ModelOutputUnavailableError"
    assert caught.value.diagnostics["visible_content_characters"] == 0
    assert caught.value.diagnostics["tool_call_count"] == 0
    assert create.await_count == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_diagnostics_do_not_echo_arbitrary_upstream_values():
    secret = "SYNTHETIC_CREDENTIAL_NOT_FOR_LOGS"
    stream = FakeStream([
        chunk(reasoning=secret, finish_reason=secret),
        chunk(usage=SimpleNamespace(prompt_tokens=secret, completion_tokens=True,
                                   completion_tokens_details=SimpleNamespace(reasoning_tokens=-1))),
    ])
    provider = OpenAIProvider({"api_key": secret, "model": secret})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
    with pytest.raises(RuntimeError) as caught:
        async for event in provider.generate_with_tools_stream([{"role": "user", "content": secret}], max_tokens=secret):
            assert event["type"] != "response"
    assert type(caught.value).__name__ == "ModelOutputUnavailableError"
    diagnostic = caught.value.diagnostics
    assert all(diagnostic[name] is None for name in ("requested_output_limit", "prompt_tokens", "completion_tokens", "reasoning_tokens"))
    assert diagnostic["finish_reasons"] == ["other"]
    assert secret not in json.dumps(diagnostic)
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_stream_disconnect_is_not_misreported_as_completed_empty_output():
    class DisconnectedStream(FakeStream):
        async def __anext__(self):
            if not self.chunks:
                raise ConnectionError("Synthetic transport interruption")
            return await super().__anext__()

    stream = DisconnectedStream([chunk(reasoning="Synthetic reasoning")])
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "synthetic-credential"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ConnectionError, match="Synthetic transport interruption"):
        async for event in provider.generate_with_tools_stream([]):
            assert event["type"] != "response"
    assert stream.closed and create.await_count == 1


@pytest.mark.asyncio
async def test_closing_reasoning_stream_does_not_trigger_an_output_error_or_retry():
    stream = FakeStream([chunk(reasoning="Synthetic reasoning")])
    create = AsyncMock(return_value=stream)
    provider = OpenAIProvider({"api_key": "synthetic-credential"})
    provider._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = provider.generate_with_tools_stream([])
    assert await anext(events) == {"type": "model_output_started"}
    await events.aclose()
    assert stream.closed and create.await_count == 1
