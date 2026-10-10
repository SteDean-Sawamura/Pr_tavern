import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.openai_provider import OpenAIProvider


def chunk(*, content=None, reasoning=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)] if usage is None else [], usage=usage)


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
