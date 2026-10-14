import asyncio
import copy
import json
import re
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.review_evidence import format_execution_snapshot
from test_agent_harness_execution import _stream_events
from test_agent_review_budget import review_session
from test_story_choice_settlement import choice_client


CASES = [(mode, outcome) for mode in ("batched", "late", "preaccepted", "delegated")
         for outcome in ("consistent", "reject", "repair") if (mode, outcome) != ("delegated", "repair")]
CASES.append(("batched", "reject_state_changes"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,outcome", CASES)
async def test_review_binds_candidate_settlement_and_survives_reload(tmp_path, monkeypatch, mode, outcome):
    session = review_session()
    initial = copy.deepcopy(session.current_state)
    good = {"end_time": initial["game_time"],
            "variable_changes": [{"id": "test_inspected", "op": "set", "value": 0}]}
    attempted = {"end_time": "2030-01-01T09:05:00",
                 "variable_changes": [{"id": "test_inspected", "op": "inc", "value": 1}]}
    narrative = ("You inspect the empty shelf once. Five minutes pass as you examine it. "
                 "You finish the inspection without making any further decision or disturbing anything."
                 if outcome == "consistent" else
                 "You keep the existing decision unchanged. You do not inspect anything or alter any record. "
                 "Your hands remain still. No time has passed and no inspection has been completed.")
    plan = [("submit_plan", {"plan": "Resolve this one action without adding any other decision."})]
    write = ("write_narrative", {"text": narrative})
    finish = ("finalize_turn", {**attempted, "summary": "Resolved the action."})
    batches = deque([plan])
    if mode == "batched":
        batches.append([write, finish])
    elif mode == "late":
        batches.extend([[write], [finish]])
    elif mode == "preaccepted":
        batches.extend([[("update_state", attempted)], [write], [("finalize_turn", {"summary": "Resolved."})]])
    else:
        batches.extend([[write], [("delegate_settlement", {"summary": "Resolve this action."})]])
    reviews, responses = [], []
    retries, background_calls = 0, 0

    async def tools(messages, **kwargs):
        nonlocal retries, background_calls
        names = {entry["function"]["name"] for entry in kwargs["tools"]}
        if names == {"extract_notes"}:
            operations = [("extract_notes", {"notes": []})]
        elif mode == "delegated" and not names.intersection({"submit_plan", "delegate_settlement"}):
            background_calls += 1
            if background_calls > 1:
                return {"content": "Finished the synthetic settlement.", "tool_calls": []}
            operations = [("update_state", attempted)]
        else:
            if not batches:
                retries += 1
                assert retries <= 3 and outcome != "consistent"
                previous = next(message["content"] for message in reversed(messages) if message["role"] == "tool")
                assert "error" in json.loads(previous)
                if outcome == "repair":
                    assert retries == 1
                    if mode == "preaccepted":
                        batches.extend([[("update_state", good)], [("finalize_turn", {"summary": "Unchanged."})]])
                    else:
                        batches.append([("finalize_turn", {**good, "summary": "Unchanged."})])
                elif outcome == "reject_state_changes":
                    batches.append([("finalize_turn", {**attempted, "end_time": f"2030-01-01T09:0{5 + retries}:00"})])
                else:
                    pending = {} if mode == "preaccepted" else attempted
                    batches.append([("write_narrative", {"text": narrative + f" You reconsider this {retries} times."}),
                                    ("finalize_turn", {**pending, "summary": "Resolved."})])
            operations = batches.popleft()
        return {"content": "", "tool_calls": [
            {"id": f"synthetic-execution-{len(responses)}-{index}", "name": name, "arguments": copy.deepcopy(arguments)}
            for index, (name, arguments) in enumerate(operations)
        ]}

    async def generate(messages, **kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        if "<narrative>" in content and "<protagonist_name>" in content:
            match = re.search(r"<execution_candidate>\s*(.*?)\s*</execution_candidate>", content, re.S)
            snapshot = json.loads(match.group(1)) if match else None
            reviews.append(snapshot)
            counter = next((entry["candidate"] for entry in (snapshot or {}).get("values", [])
                            if entry["path"] == ["script_variables", "test_inspected"]), None)
            rejected = outcome != "consistent" and snapshot is not None and (
                counter == 1 or snapshot["time"]["source"] == "explicit"
                and snapshot["time"]["candidate"] != initial["game_time"])
            return json.dumps({"pass": not rejected, "violations": [
                {"type": "settlement_mismatch", "detail": "The candidate contradicts the unchanged action."},
            ] if rejected else []})
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [],
        })

    async def record_tools(messages, **kwargs):
        response = await tools(messages, **kwargs)
        responses.append(copy.deepcopy(response))
        return response

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=record_tools), generate=AsyncMock(side_effect=generate))
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        action = {"type": "freeform", "text": "Inspect once for five minutes." if outcome == "consistent" else
                  "Keep the current decision and records unchanged. Do not let time elapse.",
                  "intent_hint": "explicit_action", "stream_id": "synthetic-execution-stream",
                  "action_id": "synthetic-execution-action", "expected_node_id": session.world_tree.active_node_id}
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json=action), 15)
        events = _stream_events(response.text)
        assert reviews and all(snapshot is not None for snapshot in reviews)
        assert len(reviews) <= 4
        rejected_reviews = [snapshot for snapshot in reviews if outcome != "consistent" and (
            any(entry["path"] == ["script_variables", "test_inspected"] and entry["candidate"] == 1
                for entry in snapshot["values"])
            or snapshot["time"]["source"] == "explicit" and snapshot["time"]["candidate"] != initial["game_time"])]
        assert len(rejected_reviews) <= 3
        if outcome.startswith("reject"):
            assert not any(event["type"] == "final" for event in events), events
            assert any(event["type"] == "error" for event in events), events
            assert session.current_state == initial
            if outcome == "reject_state_changes":
                assert len(reviews) == 3
            return
        assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
        final, = [event for event in events if event["type"] == "final"]
        expected_time = attempted["end_time"] if outcome == "consistent" else initial["game_time"]
        expected_counter = int(outcome == "consistent")
        assert final["narrative"] == narrative
        assert final["state"]["game_time"] == expected_time
        assert session.current_state["script_variables"]["test_inspected"] == expected_counter
        assert reviews[-1]["time"]["candidate"] == expected_time
        if outcome == "consistent" and mode in {"batched", "preaccepted"}:
            assert len(reviews) == 1
        if outcome == "repair":
            assert len(reviews) >= 2
        await session._drain_background_tasks()
        calls = provider.generate_with_tools.await_count
        replay = await client.post(f"/api/game/{session.save_id}/action/stream", json=action)
        assert replay.text == response.text and provider.generate_with_tools.await_count == calls
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.current_state["game_time"] == expected_time
        assert restored.current_state["script_variables"]["test_inspected"] == expected_counter
        assert "_review_execution" not in json.dumps(restored.current_state)
        assert "execution_candidate" not in response.text


def test_execution_snapshot_projects_clamped_values_without_applying_them():
    session = review_session()
    session.script_variables.definitions["test_inspected"]["max"] = 3
    state = copy.deepcopy(session.current_state)
    context = {"old_time": state["game_time"], "new_time": "2030-01-01T09:05:00"}
    pending = {"end_time": context["new_time"], "variable_changes": [{"id": "test_inspected", "op": "inc", "value": 10}]}
    snapshot = session._review_execution_snapshot(context, pending)
    variable, = [entry for entry in snapshot["values"] if entry["path"] == ["script_variables", "test_inspected"]]
    assert variable["before"] == 0 and variable["candidate"] == 3
    assert variable["definition"]["max"] == 3
    assert session.current_state == state and "_accepted_state_updates" not in context
    session.script_variables.definitions["test_inspected"]["max"] = 9
    assert variable["definition"]["max"] == 3


def test_large_execution_evidence_is_bounded_and_explicitly_partial():
    session = review_session()
    session.current_state["script_variables"].update({f"synthetic_{index}": "x" * 2000 for index in range(30)})
    snapshot = session._review_execution_snapshot({"old_time": session.current_state["game_time"]}, {
        "variable_changes": [{"id": "test_inspected", "op": "inc", "value": 1}],
    })
    rendered = format_execution_snapshot(snapshot)
    assert len(rendered) <= 12000
    limited = json.loads(rendered)
    assert limited["omitted_values"] > 0
    assert limited["values"][0]["path"] == ["script_variables", "test_inspected"]
    assert len(snapshot["values"]) > len(limited["values"])


def test_oversized_execution_metadata_is_not_presented_as_a_complete_projection():
    snapshot = {"scope": "uncommitted_state_tool_projection", "time": {"candidate": "x" * 20000}, "values": []}
    rendered = format_execution_snapshot(snapshot)
    assert len(rendered) <= 12000 and json.loads(rendered)["unavailable"] is True


@pytest.mark.asyncio
async def test_standalone_review_tool_uses_the_same_candidate_projection():
    session = review_session()
    session.ai_provider = SimpleNamespace(generate=AsyncMock(return_value='{"pass":true,"violations":[]}'))
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [{
        "end_time": "2030-01-01T09:05:00", "variable_changes": [{"id": "test_inspected", "op": "inc", "value": 1}],
    }]}
    result = json.loads(await session._review_narrative_tool("You inspect once.", context))
    assert result["pass"] is True
    content = session.ai_provider.generate.call_args.args[0][0]["content"]
    snapshot = json.loads(re.search(r"<execution_candidate>\s*(.*?)\s*</execution_candidate>", content, re.S).group(1))
    counter = next(entry for entry in snapshot["values"] if entry["path"] == ["script_variables", "test_inspected"])
    assert counter["candidate"] == 1
    assert session.current_state["script_variables"]["test_inspected"] == 0


@pytest.mark.asyncio
async def test_truncated_candidate_changes_still_invalidate_the_full_review_key(tmp_path, monkeypatch):
    session = review_session()
    session.script_variables.definitions["test_payload"] = {"id": "test_payload", "type": "string"}
    session.current_state["script_variables"]["test_payload"] = "a" * 13000
    final_args = {"end_time": "2030-01-01T09:05:00", "variable_changes": [
        {"id": "test_payload", "op": "set", "value": "b" * 13000},
    ]}
    narrative = "You finish the permitted update without touching any other object or making another decision."
    foreground_calls = 0
    full_values, rendered = [], []
    dispatch, review = session._dispatch_unified_tool, session._review_narrative

    async def tools(messages, **kwargs):
        nonlocal foreground_calls
        names = {entry["function"]["name"] for entry in kwargs["tools"]}
        if names == {"extract_notes"}:
            operations = [("extract_notes", {"notes": []})]
        else:
            foreground_calls += 1
            assert foreground_calls <= 2
            operations = ([("submit_plan", {"plan": "Perform the permitted update."})] if foreground_calls == 1
                          else [("write_narrative", {"text": narrative}), ("finalize_turn", final_args)])
        return {"content": "", "tool_calls": [
            {"id": f"synthetic-truncated-{foreground_calls}-{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(operations)
        ]}

    async def observe_dispatch(context, name, arguments):
        result = await dispatch(context, name, arguments)
        if name == "write_narrative":
            final_args["variable_changes"][0]["value"] = "c" * 13000
        return result

    async def observe_review(text, action, context, **kwargs):
        payload = next(entry for entry in context["_review_execution"]["values"]
                       if entry["path"] == ["script_variables", "test_payload"])
        full_values.append(payload["candidate"])
        return await review(text, action, context, **kwargs)

    async def generate(messages, **kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        match = re.search(r"<execution_candidate>\s*(.*?)\s*</execution_candidate>", content, re.S)
        if match:
            rendered.append(match.group(1))
            return '{"pass":true,"violations":[]}'
        return json.dumps({"scene_type": "exploration", "scope": "minor", "systems": [], "focus_npcs": [],
                           "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
                           "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False})

    monkeypatch.setattr(session, "_dispatch_unified_tool", observe_dispatch)
    monkeypatch.setattr(session, "_review_narrative", observe_review)
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=tools), generate=AsyncMock(side_effect=generate))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Perform the permitted update.", "intent_hint": "explicit_action",
        }), 15)
        final, = [event for event in _stream_events(response.text) if event["type"] == "final"]
        assert full_values == ["b" * 13000, "c" * 13000]
        assert len(rendered) == 2 and rendered[0] == rendered[1]
        assert json.loads(rendered[0])["omitted_values"] == 2
        assert final["agent_trace"]["semantic_review_calls"] == 2
        assert session.current_state["script_variables"]["test_payload"] == "c" * 13000


def test_unprojected_state_operations_are_still_bound_and_supplied_to_review():
    session = review_session()
    context = {"old_time": session.current_state["game_time"],
               "_accepted_state_updates": [{"end_time": "2030-01-01T09:05:00"}]}
    original = session._review_execution_snapshot(context)
    context["_accepted_state_updates"][0]["game_over"] = {"reason": "Synthetic ending.", "ending_type": "neutral"}
    changed = session._review_execution_snapshot(context)
    assert original["values"] == changed["values"]
    assert original != changed
    rendered = json.loads(format_execution_snapshot(changed))
    operations = next(entry["candidate"] for entry in rendered["values"] if entry["path"] == ["state_tool_updates"])
    assert operations[0]["game_over"]["ending_type"] == "neutral"
    assert "game_over" not in original["state_updates"][0]
