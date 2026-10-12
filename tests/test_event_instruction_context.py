import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _AgentResult, _bounded_tool_content
from engine.tool_schemas import _unified_tools
from test_agent_state_compaction import compact, state_feedback
from test_candidate_contacts import CONTACT, contact_feedback
from test_player_spatial_feedback import spatial_feedback
from test_scheduled_event_instructions import END, INSTRUCTION, REVISED, feedback_instructions, instruction_session


def context_session(category="one_time_events", repeat=1):
    session, context = instruction_session(category, hidden=True)
    text = INSTRUCTION * repeat
    session.script[category][0]["effects"]["inject_prompt"] = text
    session.event_engine.events["test_shift"].effects["inject_prompt"] = text
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    return session, context, text


def deliver(session, context, name):
    arguments = {"end_time": END}
    result = (session._handle_finalize_turn(context, arguments) if name == "finalize_turn"
              else session._handle_update_state(arguments, context))
    return name, arguments, result


def finalization_feedback(summary):
    prefix = "[工具失败] finalize_turn: "
    line, = [line for line in summary.splitlines() if line.startswith(prefix)]
    return json.loads(line.removeprefix(prefix))


def compressed_instructions(summary):
    return feedback_instructions(" | " + state_feedback(summary).removeprefix("[状态投影反馈] "))


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["one_time_events", "cyclic_events"])
@pytest.mark.parametrize("name", ["update_state", "finalize_turn"])
async def test_repeated_compaction_keeps_instruction_text_and_acceptance_boundary(category, name):
    session, context, text = context_session(category, repeat=8)
    record = deliver(session, context, name)
    before = copy.deepcopy((session.current_state, context))
    summary = await compact(session, [record])
    summary = await compact(session, [], summary)
    feedback = (finalization_feedback(summary) if name == "finalize_turn"
                else compressed_instructions(summary))
    key = "event_instructions" if name == "finalize_turn" else "instructions"
    assert [instruction["text"] for instruction in feedback[key]] == [text] * (3 if category == "cyclic_events" else 1)
    original = json.loads(record[2]) if name == "finalize_turn" else feedback_instructions(record[2])
    assert feedback[key] == original[key]
    assert feedback["provisional"] is True
    if name == "finalize_turn":
        assert feedback["error_code"] == "event_instructions_available"
        assert feedback["state_update_accepted"] is False
        assert "重传" in feedback["retry_hint"]
    assert (session.current_state, context) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [1, 500])
async def test_undo_replaces_compressed_instruction_snapshot_without_resurrection(repeat):
    session, context, text = context_session(repeat=repeat)
    baseline = copy.deepcopy(session.current_state)
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(baseline), "npc_reactions": [], "new_npcs": [], "choices": [],
        "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": None, "conditioned_event_keys": None,
    }
    record = deliver(session, context, "update_state")
    summary = await compact(session, [record])
    session._last_tool_result = {"name": record[0], "args": record[1]}
    arguments = {"reason": "Withdraw the synthetic event window."}
    result = await session._dispatch_unified_tool(context, "undo_my_last_action", arguments)
    summary = await compact(session, [("undo_my_last_action", arguments, result)], summary)
    summary = await compact(session, [], summary)
    assert compressed_instructions(summary)["instructions"] == []
    assert text not in summary
    assert context["_known_event_instructions"] == []
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["update_state", "finalize_turn"])
@pytest.mark.parametrize("category", ["one_time_events", "cyclic_events"])
async def test_oversized_instructions_keep_explicit_omission_and_recoverable_definitions(name, category):
    session, context, text = context_session(category, repeat=500)
    record = deliver(session, context, name)
    before = copy.deepcopy((session.current_state, context))
    bounded = _bounded_tool_content(record[2], name, 1400)
    assert len(bounded) <= 1400
    feedback = json.loads(bounded) if name == "finalize_turn" else feedback_instructions(bounded)
    assert feedback["instructions_omitted"] is True
    assert feedback["instruction_count"] == (3 if category == "cyclic_events" else 1)
    assert "instructions" not in feedback and "event_instructions" not in feedback
    assert feedback["instruction_refs"][0]["event_id"] == "test_shift"
    assert feedback["instruction_refs"][0]["fire_time"]
    assert "consult_rules" in feedback["read_hint"] and "snapshot" in feedback["read_hint"]
    summary = await compact(session, [record])
    summary = await compact(session, [], summary)
    assert '"instructions_omitted":true' in summary or '"instructions_omitted": true' in summary
    assert "test_shift" in summary and "inject_prompt" in summary
    if name == "finalize_turn":
        assert finalization_feedback(summary)["state_update_accepted"] is False
    catalog = json.loads(session._handle_consult_rules({"category": category, "query": "test_shift"}, context))
    entry, = catalog["entries"]
    coordinates = {"category": category, "rule_index": entry["rule_index"], "snapshot": entry["snapshot"],
                   "result_path": ["definition", "effects", "inject_prompt"], "offset": 0}
    chunks = []
    while True:
        section, = json.loads(session._handle_consult_rules(coordinates, context))["entries"]
        assert section["content_format"] == "result_section" and section["value_type"] == "string"
        chunks.append(section["content"])
        if section["next_offset"] is None:
            break
        coordinates["offset"] = section["next_offset"]
    assert "".join(chunks) == text
    assert (session.current_state, context) == before
    session.script[category][0]["effects"]["inject_prompt"] = "Replacement synthetic instruction."
    stale = json.loads(session._handle_consult_rules(coordinates, context))
    assert stale["error_code"] == "snapshot_expired" and stale["restart_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["update_state", "finalize_turn"])
async def test_actual_harness_revises_from_compressed_instructions_before_finalization(name):
    session, context, text = context_session()
    record = deliver(session, context, name)
    summary = await compact(session, [record])
    observed = []

    async def respond(messages, **kwargs):
        if not observed:
            compressed = next(message["content"] for message in messages
                              if message.get("content", "").startswith("[上下文压缩]"))
            feedback = (finalization_feedback(compressed) if name == "finalize_turn"
                        else compressed_instructions(compressed))
            instructions = feedback["event_instructions" if name == "finalize_turn" else "instructions"]
            observed.extend(instructions)
            assert observed[0]["text"] == text
            tool, arguments = "write_narrative", {"text": REVISED}
        else:
            tool, arguments = "finalize_turn", ({"end_time": END} if name == "finalize_turn" else {})
        return {"content": "", "tool_calls": [{"id": f"synthetic-compressed-{tool}", "name": tool, "arguments": arguments}]}

    session.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    holder = _AgentResult()
    before = copy.deepcopy(session.current_state)
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": summary}], "Review the synthetic feedback before committing.", _unified_tools(),
        lambda tool, arguments: session._dispatch_unified_tool(context, tool, arguments),
        max_rounds=3, result_holder=holder, terminal_tool_required=True,
    )]
    assert not any(event.get("type") == "error" for event in events)
    assert session.ai_provider.generate_with_tools.await_count == 2
    assert context["_finalize_narrative"] == REVISED
    assert events[-1]["type"] == "agent_done" and events[-1]["round"] == 2
    assert context["_accepted_state_updates"] == [{"end_time": END}]
    assert session.current_state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["update_state", "finalize_turn"])
async def test_short_history_bounds_raw_tool_response_before_actual_model_call(name):
    session, context, _ = context_session(repeat=500)
    _, arguments, result = deliver(session, context, name)
    before = copy.deepcopy((session.current_state, context))
    messages = [
        {"role": "user", "content": "Read the synthetic candidate feedback."},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "synthetic-raw-instruction", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]},
        {"role": "tool", "tool_call_id": "synthetic-raw-instruction", "content": result},
    ]
    events = [event async for event in session._agent_loop(
        messages, "Synthetic system.", [], lambda tool, args: "", max_rounds=1,
    )]
    assert not any(event.get("type") == "error" for event in events)
    sent = session.ai_provider.generate_with_tools.await_args.args[0]
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in sent) < 30000
    bounded, = [message["content"] for message in sent if message["role"] == "tool"]
    feedback = json.loads(bounded) if name == "finalize_turn" else feedback_instructions(bounded)
    assert feedback["instructions_omitted"] is True and feedback["instruction_count"] == 1
    assert feedback["instruction_refs"][0]["event_id"] == "test_shift"
    summary = await compact(session, [(name, arguments, bounded)])
    summary = await compact(session, [], summary)
    feedback = finalization_feedback(summary) if name == "finalize_turn" else compressed_instructions(summary)
    assert feedback["instructions_omitted"] is True
    if name == "finalize_turn":
        assert feedback["state_update_accepted"] is False and "重传" in feedback["retry_hint"]
    assert (session.current_state, context) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1400, 6000])
async def test_long_instruction_preserves_companion_spatial_and_contact_feedback(budget):
    session, context, _ = context_session(repeat=500)
    arguments = {"end_time": END, "location_change": "test_origin", **CONTACT}
    result = session._handle_update_state(arguments, context)
    before = copy.deepcopy((session.current_state, context))
    expected_space, expected_contact = spatial_feedback(result), contact_feedback(result)
    bounded = _bounded_tool_content(result, "update_state", budget)
    assert len(bounded) <= budget
    assert feedback_instructions(bounded)["instructions_omitted"] is True
    assert spatial_feedback(bounded) == expected_space
    assert contact_feedback(bounded) == expected_contact
    summary = await compact(session, [("update_state", arguments, bounded)])
    summary = await compact(session, [], summary)
    assert spatial_feedback(state_feedback(summary)) == expected_space
    assert contact_feedback(state_feedback(summary)) == expected_contact
    assert compressed_instructions(summary)["instructions_omitted"] is True
    assert (session.current_state, context) == before


@pytest.mark.parametrize("name", ["update_state", "finalize_turn"])
def test_large_reference_list_is_omitted_without_fabricating_withdrawal(name):
    session, context, _ = context_session()
    result = deliver(session, context, name)[2]
    if name == "update_state":
        feedback = feedback_instructions(result)
        key = "instructions"
        prefix = "Accepted synthetic update. | 定时事件叙事指令: "
    else:
        feedback = json.loads(result)
        key, prefix = "event_instructions", ""
    feedback[key] *= 100
    bounded = _bounded_tool_content(prefix + json.dumps(feedback), name, 1000)
    assert len(bounded) <= 1000
    preview = json.loads(bounded) if name == "finalize_turn" else feedback_instructions(bounded)
    assert preview["instructions_omitted"] is True and preview["instruction_count"] == 100
    assert preview["metadata_omitted"] is True and key not in preview
    assert "consult_rules" in preview["read_hint"] and "snapshot" in preview["read_hint"]
    if name == "finalize_turn":
        assert preview["state_update_accepted"] is False and "重传" in preview["retry_hint"]


@pytest.mark.asyncio
async def test_instruction_compaction_keeps_combined_dice_rejection_and_retry_boundary():
    session, context, _ = context_session(repeat=500)
    name, arguments, result = deliver(session, context, "finalize_turn")
    feedback = json.loads(result)
    prepared = [{"event_id": "test_shift", "dice_id": "test_roll", "result": 4}]
    feedback.update(error_code="event_dice_prepared", prepared_event_dice=prepared)
    summary = await compact(session, [(name, arguments, json.dumps(feedback))])
    summary = await compact(session, [], summary)
    receipt = finalization_feedback(summary)
    assert receipt["prepared_event_dice"] == prepared and receipt["error_code"] == "event_dice_prepared"
    assert receipt["instructions_omitted"] is True and receipt["state_update_accepted"] is False
    assert "重传" in receipt["retry_hint"] and receipt["provisional"] is True
