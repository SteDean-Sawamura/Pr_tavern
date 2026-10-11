import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _AgentResult, _bound_tool_messages, _bounded_tool_content
from engine.tool_schemas import FOREGROUND_TOOLS_SCHEMA, GAME_TOOLS_SCHEMA, _unified_tools
from test_agent_lore_context import large_session, lore_messages
from test_agent_rule_context import model_call
from test_agent_state_compaction import compact
from test_lore_search_compaction import search_progress, search_session


def test_focused_recovery_contract_reaches_compiled_system_and_all_lore_tools():
    system = search_session()._build_unified_system()
    assert "搜索定位" in system and "完整阅读" in system and "offset=0" in system
    assert "next_offset=null" in system and "不证明此前正文已读" in system
    assert "按保留的 entry_id 从 offset=0 完整重读" not in system
    for collection in (FOREGROUND_TOOLS_SCHEMA, GAME_TOOLS_SCHEMA, _unified_tools()):
        tool, = [tool["function"] for tool in collection if tool["function"]["name"] == "query_lorebook"]
        parameters = tool["parameters"]["properties"]
        assert "定位" in tool["description"] and "定位" in parameters["entry_id"]["description"]
        assert "当前 offset" in parameters["offset"]["description"]
        assert "snapshot" in parameters["offset"]["description"]


@pytest.mark.parametrize("mode", ["continuation", "batch"])
def test_omitted_exact_reads_distinguish_current_fragment_from_complete_reading(mode):
    session = large_session()
    if mode == "continuation":
        initial = session.prompt_builder.lorebook.query(entry_id="test_lore_0")
        arguments = {"entry_id": "test_lore_0", "offset": 2000, "snapshot": initial["snapshot"]}
    else:
        arguments = {"entry_ids": ["test_lore_0", "test_lore_1", "test_lore_2"]}
    original = session.prompt_builder.lorebook.query(**arguments)
    bounded = json.loads(_bounded_tool_content(json.dumps(original), "query_lorebook", 2000))
    assert bounded["content_omitted"] is True and not bounded.get("metadata_omitted")
    assert "当前片段" in bounded["notice"] and "完整阅读" in bounded["notice"]
    assert "offset=0" in bounded["notice"]
    assert [entry["offset"] for entry in bounded["entries"]] == [entry["offset"] for entry in original["entries"]]
    omitted = json.loads(_bounded_tool_content(json.dumps(original), "query_lorebook", 100))
    assert omitted["metadata_omitted"] is True and "原查询" in omitted["notice"]
    assert "entries" not in omitted and "当前片段" not in omitted["notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("compacted", [False, True])
async def test_native_agent_recovers_bounded_search_from_current_messages(candidate, compacted):
    session = search_session()
    value = 9 if candidate else 4
    declaration = {"variable_changes": [{"id": "test_counter", "op": "inc", "value": value}]}
    lorebook = session.prompt_builder.lorebook
    content = lorebook.entries[0].content + "Synthetic section detail.\n" * 24
    content += "DECLARED_CHANGE=" + json.dumps(declaration) + "\n" + "Synthetic appendix.\n" * 120
    lorebook.update_entry("test_manual", content)
    for entry in lorebook.entries[1:]:
        lorebook.update_entry(entry.id, "filing " + "Synthetic unrelated note. " * 40)
    context = {"old_time": session.current_state["game_time"]}
    if candidate:
        context["_accepted_state_updates"] = [{"variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}]}]
    original_state = copy.deepcopy(session.current_state)
    arguments = {"keyword": "synthetic_archive filing"}
    catalog = json.loads(await session._query_lorebook_for_turn(context, arguments))
    messages = _bound_tool_messages(lore_messages(arguments, catalog), 2800)
    bounded = json.loads(messages[-1]["content"])
    assert bounded["content_omitted"] is True and not bounded.get("metadata_omitted")
    assert "DECLARED_CHANGE=" not in json.dumps(messages)
    if compacted:
        summary = await compact(session, [("query_lorebook", arguments, messages[-1]["content"])])
        summary = await compact(session, [], summary)
        messages = [{"role": "user", "content": "Follow the synthetic archive procedure."},
                    {"role": "user", "content": summary}]

    async def respond(current_messages, **kwargs):
        assert "搜索定位" in kwargs["system"]
        overhead = len(kwargs["system"]) + len(json.dumps(kwargs["tools"], ensure_ascii=False))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in current_messages) <= min(30000, max(20000, 40000 - overhead))
        previous = next((message for message in reversed(current_messages) if message["role"] == "tool"), None)
        if previous is None or previous["tool_call_id"] == "test_lore":
            if previous is None:
                progress, = search_progress("\n".join(message["content"] for message in current_messages if message["role"] == "user"))
                prefix = "[世界书正文省略] "
                omitted, = [json.loads(line.removeprefix(prefix)) for message in current_messages
                            if message["role"] == "user" for line in message["content"].splitlines()
                            if line.startswith(prefix)]
                assert "当前片段" in omitted["notice"] and "offset=0" in omitted["notice"]
            else:
                progress = json.loads(previous["content"])
                assert "当前片段" in progress["notice"]
            assert progress["content_omitted"] is True
            assert bool(progress.get("projection")) is candidate
            entry = progress["entries"][0]
            assert "content" not in entry and entry["offset"] > 40000
            assert entry["next_offset"] > entry["offset"]
            name, call_id = "query_lorebook", "test_recovery_read"
            request = {"entry_id": entry["id"], "offset": entry["offset"], "snapshot": entry["snapshot"]}
        elif previous["tool_call_id"] == "test_recovery_read":
            entry, = json.loads(previous["content"])["entries"]
            assert entry["offset"] > 40000 and entry["truncated"] is True
            request = json.loads(re.search(r"DECLARED_CHANGE=([^\n]+)", entry["content"]).group(1))
            name, call_id = "update_state", "test_recovery_update"
        else:
            assert previous["tool_call_id"] == "test_recovery_update" and "变量候选值" in previous["content"]
            return {"content": "Synthetic declaration proposed.", "tool_calls": []}
        return {"content": "", "tool_calls": [model_call(name, request, call_id)]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        messages, session._build_unified_system(), _unified_tools(),
        lambda name, request: session._dispatch_unified_tool(context, name, request), result_holder=holder,
    )]
    assert not any(event.get("type") in {"error", "agent_max_rounds", "aborted"} for event in events), events
    assert session.ai_provider.generate_with_tools.await_count == 3
    assert holder.text == "Synthetic declaration proposed."
    assert context["_accepted_state_updates"][-1] == declaration
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["script_variables"]["test_counter"] == value + int(candidate)
    assert session.current_state == original_state
