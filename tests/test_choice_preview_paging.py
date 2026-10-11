import copy
import json
import random
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _AgentResult, _bounded_tool_content, _bound_tool_messages, _public_agent_trace
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import model_call, tool_call
from test_agent_state_compaction import compact
from test_choice_outcome_simulation import apply_preview_changes, lore_state, observed_state, simulation_session
from test_declared_choice_preview import arguments, preview
from test_story_choice_settlement import choice_client, persist_node, stored_state


def long_session():
    session = simulation_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.story_tree_engine._nodes["test_decision"]["choices"][0]["effects"]["add_lore"][0]["content"] = (
        'SYNTHETIC_PAGING_中文 "quoted" \\ path\n' * 150 + "SYNTHETIC_PAGING_END"
    )
    return session


def body(result):
    return {key: value for key, value in result.items() if key not in {"read", "snapshot", "total_chars"}}


async def reread(session, result, context=None):
    chunks = []
    coordinates = result["read"]
    while True:
        page = await preview(session, context, **coordinates)
        assert "error" not in page, page
        assert page["content_format"] == "json_fragment"
        assert page["offset"] == sum(map(len, chunks))
        assert page["snapshot"] == result["snapshot"]
        assert page["read"] == result["read"]
        assert 0 < len(page["content"]) <= 2000
        chunks.append(page["content"])
        if page["next_offset"] is None:
            break
        coordinates = {**page["read"], "offset": page["next_offset"]}
    assert sum(map(len, chunks)) == result["total_chars"]
    return json.loads("".join(chunks))


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
async def test_full_preview_can_be_reconstructed_without_mutating_session(candidate):
    session = long_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 13}]},
    ]}
    before, before_context, before_random = observed_state(session), copy.deepcopy(context), random.getstate()
    result = await preview(session, context, include_candidates=candidate)
    assert len(result["snapshot"]) == 22
    assert result["read"] == arguments(include_candidates=candidate, offset=0, snapshot=result["snapshot"])
    assert await reread(session, result, context) == body(result)
    assert result["total_chars"] > 10000
    assert observed_state(session) == before and context == before_context
    assert random.getstate() == before_random
    end = await preview(session, context, **{**result["read"], "offset": result["total_chars"]})
    assert end["content"] == "" and end["next_offset"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 1])
async def test_every_fragment_requires_snapshot_even_at_zero(offset):
    session = long_session()
    result = await preview(session, offset=offset)
    assert result["error_code"] == "snapshot_required" and result["restart_required"] is True
    assert result["restart"] == arguments(include_candidates=False)
    restarted = await preview(session, **result["restart"])
    assert await reread(session, restarted) == body(restarted)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"offset": True}, {"offset": -1}, {"offset": 1.5}, {"offset": "0"}, {"offset": None},
    {"snapshot": True}, {"snapshot": None}, {"snapshot": []}, {"snapshot": "bad"},
    {"snapshot": "a" * 23},
])
async def test_malformed_paging_arguments_are_not_silently_ignored(overrides):
    session = long_session()
    result = await preview(session, **overrides)
    assert "error" in result


@pytest.mark.asyncio
async def test_offset_out_of_range_is_rejected_after_valid_snapshot():
    session = long_session()
    result = await preview(session)
    invalid = await preview(session, **{**result["read"], "offset": result["total_chars"] + 1})
    assert "error" in invalid and "offset" in invalid["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parameters", [{"offset": 0}, {"snapshot": "a" * 22},
                                         {"node_id": "test_decision", "offset": 0}])
async def test_paging_without_identity_cannot_fall_back_to_text_risk(parameters):
    result = json.loads(await long_session()._dispatch_unified_tool(
        {}, "preview_choice_outcome", {"choice_text": "观察", **parameters},
    ))
    assert "error" in result and "predicted_risk" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "state_tail", "save", "branch", "tree", "turn", "script", "lore", "runtime_node", "removed_node",
    "removed_choice", "runtime_variable", "runtime_trigger", "runtime_event", "candidates", "view",
])
async def test_full_input_revision_invalidates_fragments(change):
    session = long_session()
    session.current_state["aaa_padding"] = "x" * 3000
    session.current_state["zzz_tail"] = 0
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": []}
    result = await preview(session, context, include_candidates=True)
    short_hash = session.world_tree.state_hash(session.current_state)
    coordinates = result["read"]
    if change == "state_tail":
        session.current_state["zzz_tail"] = 1
        assert session.world_tree.state_hash(session.current_state) == short_hash
    elif change == "save":
        session.save_id = "synthetic-other-save"
    elif change == "branch":
        session.world_tree.active_node_id = "synthetic-other-branch"
    elif change == "tree":
        session.world_tree.tree_id = "synthetic-other-tree"
    elif change == "turn":
        session.turn_number += 1
    elif change == "script":
        session.script["settings"]["synthetic_revision"] = 1
    elif change == "lore":
        session.prompt_builder.lorebook.add_entries([
            {"id": "test_unrelated", "enabled": False, "content": "Synthetic unrelated lore."},
        ])
    elif change == "runtime_node":
        session.story_tree_engine._nodes["test_other"]["name"] = "Synthetic revised node"
    elif change == "removed_node":
        session.story_tree_engine._nodes.pop("test_decision")
    elif change == "removed_choice":
        session.story_tree_engine._nodes["test_decision"]["choices"] = []
    elif change == "runtime_variable":
        session.script_variables.definitions["test_gate"]["description"] = "Synthetic revision"
    elif change == "runtime_trigger":
        session.trigger_engine.triggers.append({"id": "test_new", "event": "test_other_signal", "actions": []})
    elif change == "runtime_event":
        session.event_engine.events["test_simulated_signal"].name = "Synthetic revision"
    elif change == "candidates":
        context["_accepted_state_updates"].append({"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]})
    elif change == "view":
        coordinates = {**coordinates, "include_candidates": False}
    expired = await preview(session, context, **coordinates)
    assert expired["error_code"] == "snapshot_expired" and expired["restart_required"] is True
    assert "content" not in expired and "snapshot" not in expired["restart"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fragment", [False, True])
async def test_budget_and_repeated_compaction_preserve_complete_reread(fragment):
    session = long_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    result = await preview(session)
    source = await preview(session, **{**result["read"], "offset": 2000}) if fragment else result
    record = json.dumps(source, ensure_ascii=False)
    messages = [{"role": "assistant", "tool_calls": [tool_call("preview_choice_outcome", arguments(), "synthetic-preview")]},
                {"role": "tool", "tool_call_id": "synthetic-preview", "content": record}]
    bounded = _bound_tool_messages(messages, 2000)[-1]
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 1000
    metadata = json.loads(bounded["content"])
    assert metadata["read"] == result["read"] and metadata["content_omitted"] is True
    assert "next_offset" not in metadata and "offset=0" in metadata["notice"]
    repeated = json.loads(_bounded_tool_content(bounded["content"], "preview_choice_outcome", 1000))
    assert repeated["state_change_count"] == len(result["state_changes"])
    assert repeated["lore_change_count"] == len(result["lore_changes"])
    assert "SYNTHETIC_PAGING_" not in json.dumps(repeated)
    summary = await compact(session, [("preview_choice_outcome", arguments(), record)])
    summary = await compact(session, [], previous_summary=summary)
    prefix = "[选择模拟] "
    saved, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert saved["read"] == result["read"] and "next_offset" not in saved
    assert await reread(session, saved) == body(result)


@pytest.mark.asyncio
async def test_tiny_budget_explicitly_discards_coordinates():
    result = await preview(long_session(), node_id="x" * 4000)
    result = {**result, "mode": "declared_choice", "node_id": "x" * 4000}
    metadata = json.loads(_bounded_tool_content(json.dumps(result), "preview_choice_outcome", 200))
    assert metadata["metadata_omitted"] is True and metadata["content_omitted"] is True
    assert "read" not in metadata and "next_offset" not in metadata
    assert "丢弃" in metadata["notice"]
    repeated = json.loads(_bounded_tool_content(json.dumps(metadata), "preview_choice_outcome", 1000))
    assert repeated["metadata_omitted"] is True and "丢弃" in repeated["notice"]


def test_schema_documents_full_result_reread():
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "preview_choice_outcome")
    assert {"offset", "snapshot"} <= schema["parameters"]["properties"].keys()
    assert "offset=0" in schema["description"] and "拼接" in schema["description"]


@pytest.mark.asyncio
async def test_complete_simulation_result_is_bound_to_snapshot(monkeypatch):
    session = long_session()
    settle = session._settle_story_choice.__func__
    output = {"text": "Synthetic first result."}

    def changed_result(candidate, node_id, choice_id):
        result = settle(candidate, node_id, choice_id)
        candidate.current_state["zzz_output"] = output["text"]
        return result

    monkeypatch.setattr(session, "_settle_story_choice", MethodType(changed_result, session))
    result = await preview(session)
    output["text"] = "Synthetic second result."
    expired = await preview(session, **result["read"])
    assert expired["error_code"] == "snapshot_expired"
    fresh = await preview(session, **expired["restart"])
    assert await reread(session, fresh) == body(fresh)
    assert "zzz_output" not in session.current_state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["snapshot_required", "snapshot_expired"])
async def test_restart_survives_budget_and_repeated_compaction(kind):
    session = long_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    initial = await preview(session)
    parameters = {"offset": 0} if kind == "snapshot_required" else {**initial["read"], "snapshot": "a" * 22}
    result = await preview(session, **parameters)
    bounded = _bounded_tool_content(json.dumps(result), "preview_choice_outcome", 1000)
    summary = await compact(session, [("preview_choice_outcome", arguments(**parameters), bounded)])
    summary = await compact(session, [], previous_summary=summary)
    prefix = "[选择模拟] "
    metadata, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert metadata["error_code"] == kind and metadata["restart_required"] is True
    assert metadata["restart"] == arguments(include_candidates=False)
    fresh = await preview(session, **metadata["restart"])
    assert await reread(session, fresh) == body(fresh)


@pytest.mark.asyncio
async def test_candidate_undo_and_ended_turn_invalidate_prior_fragments():
    session = long_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 13}]},
    ]}
    initial = await preview(session, context, include_candidates=True)
    context["_accepted_state_updates"].clear()
    expired = await preview(session, context, **initial["read"])
    assert expired["error_code"] == "snapshot_expired"
    fresh = await preview(session, context, **expired["restart"])
    assert await reread(session, fresh, context) == body(fresh)
    ended = await preview(session, {}, **fresh["read"])
    assert ended["error_code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_paged_result_matches_actual_api_persistence_and_expires_across_branches(tmp_path, monkeypatch):
    session = long_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        result = await preview(session)
        decoded = await reread(session, result)
        expected = apply_preview_changes(session.current_state, decoded["state_changes"])
        expected_lore = apply_preview_changes(lore_state(session), decoded["lore_changes"])
        final_id = await persist_node(session, root_id)
        expired = await preview(session, **result["read"])
        assert expired["error_code"] == "snapshot_expired"
        current = await preview(session, **expired["restart"])
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json=arguments())
        assert response.status_code == 200 and response.json()["success"] is True
        assert session.current_state == expected and lore_state(session) == expected_lore
        assert await stored_state(session) == expected
        assert "SYNTHETIC_PAGING_" not in response.text
        assert (await preview(session, **current["read"]))["error_code"] == "snapshot_expired"
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state == expected
        completed = await preview(restored)
        assert completed["available"] is False and await reread(restored, completed) == body(completed)
        await restored.branch_to_node(root_id)
        assert (await preview(restored, **completed["read"]))["error_code"] == "snapshot_expired"
        fresh = await preview(restored)
        assert fresh["available"] is True and await reread(restored, fresh) == body(fresh)
        await restored.branch_to_node(final_id)
        assert (await preview(restored, **fresh["read"]))["error_code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_real_agent_budget_follows_fragment_coordinates_before_deciding():
    session = long_session()
    session.story_tree_engine._nodes["test_decision"]["choices"][0]["effects"]["add_lore"][0]["content"] = (
        "SYNTHETIC_AGENT_FRAGMENT\n" * 22 + "SYNTHETIC_AGENT_END"
    )
    context = {"old_time": session.current_state["game_time"]}
    before = observed_state(session)
    expected = await preview(session)
    tools = _unified_tools()
    system = "Synthetic simulation reader."
    budget = min(30000, max(20000, 40000 - len(system) - len(json.dumps(tools, ensure_ascii=False))))
    requests = []
    decoded = None

    async def respond(messages, **kwargs):
        nonlocal decoded
        requests.append(copy.deepcopy(messages))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        if len(requests) == 1:
            return {"content": "", "tool_calls": [
                model_call("preview_choice_outcome", arguments(), f"test_initial_{index}") for index in range(8)
            ]}
        results = {message["tool_call_id"]: json.loads(message["content"])
                   for message in messages if message["role"] == "tool"}
        if len(requests) == 2:
            initial = results["test_initial_0"]
            assert initial["content_omitted"] is True and initial["read"]["offset"] == 0
            coordinates = initial["read"]
        else:
            page = json.loads(messages[-1]["content"])
            assert "content" in page and page["content_format"] == "json_fragment"
            if page["next_offset"] is None:
                fragments = sorted((value for key, value in results.items() if key.startswith("test_page_")),
                                   key=lambda value: value["offset"])
                assembled = ""
                for fragment in fragments:
                    assert fragment["offset"] == len(assembled) and fragment["snapshot"] == expected["snapshot"]
                    assembled += fragment["content"]
                assert len(assembled) == page["total_chars"]
                decoded = json.loads(assembled)
                assert decoded == body(expected)
                assert decoded["available"] is True and decoded["projection"]["persisted"] is False
                return {"content": "Synthetic decision after complete simulation reading.", "tool_calls": []}
            coordinates = {**page["read"], "offset": page["next_offset"]}
        return {"content": "", "tool_calls": [model_call(
            "preview_choice_outcome", coordinates, f"test_page_{len(requests)}",
        )]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": "Compare the synthetic simulation, reread complete results, then decide."}],
        system, tools, lambda name, parameters: session._dispatch_unified_tool(context, name, parameters),
        max_rounds=12, result_holder=holder,
    )]
    assert not any(event.get("type") == "error" for event in events), events
    assert decoded is not None and len(requests) >= 4
    assert observed_state(session) == before and context == {"old_time": session.current_state["game_time"]}
    assert holder.text == "Synthetic decision after complete simulation reading."
    public = json.dumps(_public_agent_trace({"rounds": [
        {"step": 1, "tool": "preview_choice_outcome", "args": expected["read"], "result": expected},
    ]}))
    assert "SYNTHETIC_AGENT_" not in public and expected["snapshot"] not in public
