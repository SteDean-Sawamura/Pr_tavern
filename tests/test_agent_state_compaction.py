import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.lorebook import Lorebook


@pytest.fixture
def session():
    session = GameSession({
        "script_id": "synthetic-state-compaction", "settings": {},
        "variables": [
            {"id": "test_counter", "type": "number", "default": 4},
            {"id": "test_flag", "type": "bool", "default": False},
        ],
    }, SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []})))
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


async def compact(session, records, previous_summary=None):
    messages = [{"role": "user", "content": "Continue the synthetic action."}]
    if previous_summary:
        messages.append({"role": "user", "content": previous_summary})
    messages.append({"role": "assistant", "content": "Synthetic prior context. " * 1600})
    for index, (name, arguments, result) in enumerate(records):
        call_id = f"synthetic-state-{index}"
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]},
            {"role": "tool", "tool_call_id": call_id, "content": result},
        ])
    messages.extend([
        {"role": "user", "content": "Keep the current plan."},
        {"role": "assistant", "content": "Continuing."},
        {"role": "user", "content": "Check the accepted changes."},
        {"role": "assistant", "content": "Ready."},
    ])
    events = [event async for event in session._agent_loop(
        messages, "Synthetic system.", [], lambda name, args: "", max_rounds=1,
    )]
    assert not any(event.get("type") == "error" for event in events)
    compacted = session.ai_provider.generate_with_tools.await_args.args[0]
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in compacted) < 30000
    summary = compacted[1]["content"]
    assert summary.startswith("[上下文压缩]")
    return summary


def state_feedback(summary):
    lines = [line for line in summary.splitlines() if line.startswith("[状态投影反馈] ")]
    assert len(lines) == 1
    return lines[0]


def lore_query_statuses(summary):
    prefix = "[世界书查询状态] "
    return [json.loads(line.removeprefix(prefix)) for line in summary.splitlines()
            if line.startswith(prefix)]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_compaction_keeps_target_of_empty_exact_query(session, batch):
    target = "test_requested_only_here"
    arguments = {"entry_ids": [target]} if batch else {"entry_id": target}
    result = Lorebook([]).query(**arguments)
    summary = await compact(session, [("query_lorebook", arguments, json.dumps(result))])
    assert target in summary, summary


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_reason", ["absent", "disabled", "excluded"])
@pytest.mark.parametrize("batch", [False, True])
async def test_compaction_preserves_exact_queries_without_results(session, missing_reason, batch):
    lorebook = Lorebook([{"id": "test_label", "keys": ["label"], "content": "Synthetic label."}])
    arguments = {"entry_ids": ["test_label"]} if batch else {"entry_id": "test_label"}
    previous = lorebook.query(**arguments)
    if missing_reason == "absent":
        lorebook.remove_entry("test_label")
    elif missing_reason == "disabled":
        lorebook.update_entry_enabled("test_label", False)
    result = lorebook.query(**arguments, excluded_ids={"test_label"} if missing_reason == "excluded" else None)
    result["projection"] = {
        "through": "2030-01-01T09:00:00", "event_ids": [], "provisional": True,
        "note": "Synthetic candidate projection, not settled state or player knowledge.",
    }
    assert result["entries"] == []
    summary = await compact(session, [
        ("query_lorebook", arguments, json.dumps(previous)),
        ("query_lorebook", arguments, json.dumps(result)),
    ])
    statuses = lore_query_statuses(summary)
    assert len(statuses) == 2
    assert statuses[0]["returned_ids"] == ["test_label"]
    assert statuses[1]["requested_ids"] == ["test_label"]
    assert statuses[1]["returned_ids"] == []
    assert statuses[1]["not_returned_ids"] == ["test_label"]
    assert statuses[1]["projection"] == result["projection"]
    assert "不证明词条不存在" in statuses[1]["note"]


@pytest.mark.asyncio
async def test_compaction_preserves_partial_batch_query_and_repeated_status(session):
    lorebook = Lorebook([
        {"id": "test_label", "keys": ["label"], "content": "Synthetic label."},
        {"id": "test_closed", "keys": ["closed"], "content": "SYNTHETIC_UNREAD", "enabled": False},
    ])
    arguments = {"entry_ids": ["test_label", "test_closed", "test_absent"]}
    result = lorebook.query(**arguments)
    summary = await compact(session, [("query_lorebook", arguments, json.dumps(result))])
    for _ in range(2):
        summary = await compact(session, [], summary)
        statuses = lore_query_statuses(summary)
        assert len(statuses) == 1
        assert statuses[0]["requested_ids"] == arguments["entry_ids"]
        assert statuses[0]["returned_ids"] == ["test_label"]
        assert statuses[0]["not_returned_ids"] == ["test_closed", "test_absent"]
        assert "SYNTHETIC_UNREAD" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("next_arguments", [
    {"entry_id": "test_other"},
    {"entry_id": "test_label", "offset": 2000},
    {"entry_id": "test_label", "offset": -1},
    {"entry_ids": []},
    {"keyword": "label", "page": 10},
])
async def test_compaction_distinguishes_missing_error_eof_and_search(session, next_arguments):
    content = "Synthetic evidence. " * 30 + "SYNTHETIC_EVIDENCE_END"
    lorebook = Lorebook([{"id": "test_label", "keys": ["label"], "content": content}])
    if next_arguments.get("offset", 0) > 0 or next_arguments.get("page", 0) > 0:
        initial = lorebook.query(**{**next_arguments, "page": 0, "offset": 0})
        next_arguments = {**next_arguments, "snapshot": initial["snapshot"]}
    result = lorebook.query(**next_arguments)
    summary = await compact(session, [
        ("query_lorebook", {"entry_id": "test_label"}, json.dumps(lorebook.query(entry_id="test_label"))),
        ("query_lorebook", next_arguments, json.dumps(result)),
    ])
    statuses = lore_query_statuses(summary)
    assert statuses
    assert statuses[0]["returned_ids"] == ["test_label"]
    assert statuses[0]["not_returned_ids"] == []
    assert "SYNTHETIC_EVIDENCE_END" in summary
    if next_arguments.get("entry_id") == "test_other":
        assert len(statuses) == 2
        assert statuses[1]["not_returned_ids"] == ["test_other"]
    elif next_arguments.get("offset") == 2000:
        assert len(statuses) == 2
        assert statuses[1]["returned_ids"] == ["test_label"]
        assert statuses[1]["not_returned_ids"] == []
        assert statuses[1]["offset"] == 2000
        assert "next_offset=None" in summary
    else:
        assert len(statuses) == 1


@pytest.mark.asyncio
async def test_repeated_compaction_retains_chronological_query_outcomes(session):
    lorebook = Lorebook([{"id": "test_label", "keys": ["label"], "content": "Synthetic old label."}])
    arguments = {"entry_id": "test_label"}
    summary = None
    for enabled in (True, False, True):
        lorebook.update_entry_enabled("test_label", enabled)
        result = lorebook.query(**arguments)
        summary = await compact(session, [("query_lorebook", arguments, json.dumps(result))], summary)
    statuses = lore_query_statuses(summary)
    assert [status["returned_ids"] for status in statuses] == [["test_label"], [], ["test_label"]]
    assert [status["not_returned_ids"] for status in statuses] == [[], ["test_label"], []]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_time", [False, True])
async def test_compaction_keeps_cumulative_candidate_variables(session, with_time):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    updates = [
        {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]},
        {"variable_changes": [{"id": "test_flag", "op": "toggle"}]},
    ]
    if with_time:
        updates.append({"end_time": "2030-01-01T09:01:00"})
    records = [("update_state", arguments, session._handle_update_state(arguments, context))
               for arguments in updates]
    feedback = state_feedback(await compact(session, records))
    assert '"test_counter": 6' in feedback
    assert '"test_flag": true' in feedback
    assert "未提交" in feedback and "不含尚未结算的事件效果" in feedback
    assert f"已接受 {len(updates)} 次状态更新" in feedback
    assert ("2030-01-01T09:01:00" if with_time else original["game_time"]) in feedback
    assert session.current_state == original


@pytest.mark.asyncio
async def test_compaction_keeps_rejected_update_without_losing_candidate_values(session):
    context = {"old_time": session.current_state["game_time"]}
    updates = [
        {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]},
        {"variable_changes": [{"id": "test_missing_variable", "op": "inc"}]},
    ]
    records = [("update_state", arguments, session._handle_update_state(arguments, context))
               for arguments in updates]
    assert records[-1][2].startswith("错误:")
    summary = await compact(session, records)
    assert records[-1][2] in summary
    assert '"test_counter": 6' in state_feedback(summary)
    assert "已接受 1 次状态更新" in state_feedback(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("earlier_update", [False, True])
async def test_compaction_uses_restored_state_after_undo(session, rejected, earlier_update):
    context = {"old_time": session.current_state["game_time"]}
    records = []
    if earlier_update:
        arguments = {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]}
        records.append(("update_state", arguments, session._handle_update_state(arguments, context)))
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(session.current_state), "npc_reactions": [], "new_npcs": [],
        "choices": [], "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": copy.deepcopy(context.get("_accepted_state_updates")),
        "conditioned_event_keys": copy.deepcopy(context.get("_conditioned_event_keys")),
    }
    arguments = {"variable_changes": [{
        "id": "test_missing_variable" if rejected else "test_counter", "op": "inc",
    }], "end_time": "2030-01-01T09:01:00"}
    records.append(("update_state", arguments, session._handle_update_state(arguments, context)))
    session._last_tool_result = {"name": "update_state", "args": arguments}
    undo_arguments = {"reason": "Discard only the immediately preceding request."}
    undo_result = await session._dispatch_unified_tool(context, "undo_my_last_action", undo_arguments)
    records.append(("undo_my_last_action", undo_arguments, undo_result))
    summary = await compact(session, records)
    feedback = state_feedback(summary)
    expected = 6 if earlier_update else 4
    if earlier_update or not rejected:
        assert f'"test_counter": {expected}' in feedback
    assert f"已接受 {int(earlier_update)} 次状态更新" in feedback
    assert "2030-01-01T09:00:00" in feedback
    assert "2030-01-01T09:01:00" not in feedback
    assert "已撤销上一个工具调用（update_state）" in summary
    assert session.current_state["script_variables"]["test_counter"] == 4


@pytest.mark.asyncio
async def test_repeated_compaction_replaces_only_superseded_projection(session):
    context = {"old_time": session.current_state["game_time"]}
    arguments = {"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]}
    summary = await compact(session, [("update_state", arguments, session._handle_update_state(arguments, context))])
    rejected = {"variable_changes": [{"id": "test_missing_variable", "op": "inc"}]}
    error = session._handle_update_state(rejected, context)
    summary = await compact(session, [("update_state", rejected, error)], summary)
    assert '"test_counter": 6' in state_feedback(summary)
    assert error in summary
    arguments = {"variable_changes": [{"id": "test_flag", "op": "toggle"}]}
    summary = await compact(session, [("update_state", arguments, session._handle_update_state(arguments, context))], summary)
    feedback = state_feedback(summary)
    assert '"test_counter": 6' in feedback and '"test_flag": true' in feedback
    assert "已接受 2 次状态更新" in feedback


@pytest.mark.asyncio
async def test_narrative_revision_refreshes_candidate_feedback_after_settlement_rollback(session):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    operations = [
        ("update_state", {"end_time": "2030-01-01T09:01:00",
                          "variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]}),
        ("write_narrative", {"text": "You stay where you are without changing anything."}),
        ("update_state", {"end_time": "2030-01-01T09:01:00"}),
    ]
    responses = [{"content": "", "tool_calls": [{
        "id": f"synthetic-revision-{index}", "name": name, "arguments": arguments,
    }]} for index, (name, arguments) in enumerate(operations)]
    responses.append({"content": "Done.", "tool_calls": []})
    session.ai_provider.generate_with_tools = AsyncMock(side_effect=responses)
    events = [event async for event in session._run_background_agent(
        context, "You adjust the synthetic counter.", "", {"text": "Stay in place."},
    )]
    holder = next(event["holder"] for event in events if event["type"] == "_bg_result")
    assert holder.records[0]["superseded"] is True
    assert context["_accepted_state_updates"] == [{"end_time": "2030-01-01T09:01:00"}]
    session.ai_provider.generate_with_tools = AsyncMock(return_value={"content": "Done.", "tool_calls": []})
    records = [(record["name"], record["args"], record["result"]) for record in holder.records[:2]]
    summary = await compact(session, records)
    feedback = state_feedback(summary)
    assert '"test_counter": 4' in feedback
    assert "已接受 0 次状态更新" in feedback
    assert "2030-01-01T09:00:00" in feedback and "2030-01-01T09:01:00" not in feedback
    assert session.current_state == original
