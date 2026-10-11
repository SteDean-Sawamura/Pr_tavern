import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _AgentResult, _bounded_tool_content, _json_result_section, _public_agent_trace
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import model_call
from test_agent_state_compaction import compact
from test_choice_outcome_simulation import observed_state
from test_choice_preview_paging import body, long_session
from test_declared_choice_preview import arguments, preview
from test_story_choice_settlement import choice_client, persist_node, stored_state


def large_session():
    session = long_session()
    effects = session.story_tree_engine._nodes["test_decision"]["choices"][0]["effects"]
    effects["add_lore"][0]["content"] = "SYNTHETIC_INSPECTION_DETAIL\n" * 5000
    for index in range(70):
        key = f"test_bulk_{index:03}"
        session.current_state["script_variables"][key] = 0
        effects["set_var"].append({"var_id": key, "op": "inc", "value": index + 1})
    return session


@pytest.mark.asyncio
async def test_structural_pages_share_snapshot_and_do_not_mutate_session():
    session = large_session()
    before = observed_state(session)
    initial = await preview(session)
    coordinates = {**initial["read"], "result_path": []}
    entries = []
    while True:
        page = await preview(session, **coordinates)
        assert page["content_format"] == "result_section"
        assert page["value_type"] == "object"
        assert page["snapshot"] == initial["snapshot"]
        assert page["read"] == {**initial["read"], "result_path": []}
        entries.extend(page["entries"])
        if page["next_offset"] is None:
            break
        coordinates["offset"] = page["next_offset"]
    assert [entry["key"] for entry in entries] == sorted(body(initial))
    changes = next(entry for entry in entries if entry["key"] == "state_changes")
    assert changes["complete"] is False and changes["size"] == len(initial["state_changes"])
    assert "value" not in changes
    available = next(entry for entry in entries if entry["key"] == "available")
    assert available["complete"] is True and available["value"] is True
    assert observed_state(session) == before


@pytest.mark.asyncio
async def test_structural_reads_require_snapshot_and_identity():
    session = long_session()
    missing = await preview(session, result_path=[])
    assert missing["error_code"] == "snapshot_required"
    result = json.loads(await session._dispatch_unified_tool(
        {}, "preview_choice_outcome", {"choice_text": "观察", "result_path": []},
    ))
    assert "error" in result and "predicted_risk" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [None, {}, "state_changes", [True], [-1], [1.5], [{}]])
async def test_invalid_path_types_are_rejected(path):
    session = long_session()
    result = await preview(session, result_path=path)
    assert "error" in result and "state_changes" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [["missing"], ["state_changes", "0"], ["projection", 0],
                                  ["available", "child"], ["state_changes", 100000]])
async def test_invalid_path_traversal_is_rejected(path):
    session = long_session()
    initial = await preview(session)
    result = await preview(session, **{**initial["read"], "result_path": path})
    assert result["error_code"] == "invalid_result_path"
    assert "content" not in result and "entries" not in result


@pytest.mark.asyncio
async def test_real_agent_consumes_relevant_changes_larger_than_context_without_assembling_json():
    session = large_session()
    before = observed_state(session)
    initial = await preview(session)
    context = {"old_time": session.current_state["game_time"]}
    tools, system = _unified_tools(), "Synthetic selective simulation reader."
    budget = min(30000, max(20000, 40000 - len(system) - len(json.dumps(tools, ensure_ascii=False))))
    assert initial["total_chars"] > 4 * budget
    requests = []

    async def respond(messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if not results:
            return {"content": "", "tool_calls": [
                model_call("preview_choice_outcome", arguments(), f"synthetic_initial_{index}") for index in range(8)
            ]}
        page = results[-1]
        coordinates = dict(page["read"])
        if "result_path" not in coordinates:
            coordinates["result_path"] = []
        else:
            assert page["content_format"] == "result_section" and "content" not in page
            if coordinates["result_path"] == []:
                if any(entry["key"] == "state_changes" for entry in page["entries"]):
                    coordinates.update(result_path=["state_changes"], offset=0)
                else:
                    assert page["next_offset"] is not None
                    coordinates["offset"] = page["next_offset"]
            else:
                assert coordinates["result_path"] == ["state_changes"]
                for entry in page["entries"]:
                    if entry.get("complete") and entry["value"].get("path") == ["script_variables", "test_reward"]:
                        assert page["projection"]["persisted"] is False
                        return {"content": f"Synthetic decision after inspecting reward={entry['value']['after']}.",
                                "tool_calls": []}
                assert page["next_offset"] is not None
                coordinates["offset"] = page["next_offset"]
        return {"content": "", "tool_calls": [model_call(
            "preview_choice_outcome", coordinates, f"synthetic_section_{len(requests)}",
        )]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": "Inspect the declared effect on test_reward; do not read unrelated long text."}],
        system, tools, lambda name, parameters: session._dispatch_unified_tool(context, name, parameters),
        max_rounds=20, result_holder=holder,
    )]
    assert not any(event.get("type") == "error" for event in events), events
    assert holder.text == "Synthetic decision after inspecting reward=4."
    assert any("[上下文压缩]" in (message.get("content") or "") for request in requests for message in request)
    assert "SYNTHETIC_INSPECTION_DETAIL" not in json.dumps(requests)
    assert observed_state(session) == before and context == {"old_time": session.current_state["game_time"]}


@pytest.mark.parametrize("value,value_type", [(None, "null"), (False, "boolean"), (True, "boolean"),
                                            (0, "number"), (-3, "number"), (0.5, "number"), ("", "string")])
def test_leaf_types_are_preserved(value, value_type):
    result = _json_result_section({"field": value}, ["field"], 0)
    assert result["value_type"] == value_type and result["complete"] is True
    actual = result["content"] if value_type == "string" else result["value"]
    assert actual == value and type(actual) is type(value)
    assert result["next_offset"] is None
    with pytest.raises(ValueError):
        _json_result_section({"field": value}, ["field"], 1)


@pytest.mark.parametrize("value", [{}, [], [None, False, 0, ""], {"0": 7, "": None}])
def test_small_containers_are_complete_but_empty_tail_is_not(value):
    result = _json_result_section({"field": value}, ["field"], 0)
    assert result["complete"] is True and result["size"] == len(value)
    assert len(result["entries"]) == len(value)
    tail = _json_result_section({"field": value}, ["field"], len(value))
    assert tail["entries"] == [] and tail["next_offset"] is None
    assert tail["complete"] is (not value)
    with pytest.raises(ValueError):
        _json_result_section({"field": value}, ["field"], len(value) + 1)


@pytest.mark.asyncio
async def test_string_pages_are_independent_text_and_preserve_types_and_privacy():
    session = long_session()
    initial = await preview(session)
    index = next(index for index, change in enumerate(initial["lore_changes"]) if change["path"] == ["test_added"])
    path = ["lore_changes", index, "after", "content"]
    expected = initial["lore_changes"][index]["after"]["content"]
    coordinates = {**initial["read"], "result_path": path}
    chunks = []
    while True:
        page = await preview(session, **coordinates)
        assert page["value_type"] == "string" and page["content_format"] == "result_section"
        assert page["size"] == len(expected) and page["total_chars"] == initial["total_chars"]
        assert page["read"]["offset"] == 0 and page["read"]["result_path"] == path
        assert page["complete"] is False and len(page["content"]) <= 2000
        chunks.append(page["content"])
        public = json.dumps(_public_agent_trace({"rounds": [
            {"step": 1, "tool": "preview_choice_outcome", "args": coordinates, "result": page},
        ]}))
        assert "SYNTHETIC_PAGING_" not in public and initial["snapshot"] not in public
        if page["next_offset"] is None:
            break
        coordinates["offset"] = page["next_offset"]
    assert "".join(chunks) == expected
    empty = await preview(session, **{**coordinates, "offset": len(expected)})
    assert empty["content"] == "" and empty["next_offset"] is None and empty["complete"] is False
    invalid = await preview(session, **{**coordinates, "offset": len(expected) + 1})
    assert "error" in invalid


@pytest.mark.asyncio
async def test_compaction_preserves_scoped_reread_without_claiming_full_result():
    session = long_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    initial = await preview(session)
    index = next(index for index, change in enumerate(initial["lore_changes"]) if change["path"] == ["test_added"])
    path = ["lore_changes", index, "after", "content"]
    page = await preview(session, **{**initial["read"], "result_path": path, "offset": 2000})
    record = json.dumps(page, ensure_ascii=False)
    bounded = _bounded_tool_content(record, "preview_choice_outcome", 1000)
    for _ in range(2):
        bounded = _bounded_tool_content(bounded, "preview_choice_outcome", 1000)
    metadata = json.loads(bounded)
    assert len(bounded) <= 1000 and metadata["read"] == page["read"]
    assert metadata["content_omitted"] is True and "next_offset" not in metadata and "complete" not in metadata
    summary = await compact(session, [("preview_choice_outcome", page["read"], record)])
    summary = await compact(session, [], previous_summary=summary)
    saved, = [json.loads(line.removeprefix("[选择模拟] ")) for line in summary.splitlines() if line.startswith("[选择模拟] ")]
    assert saved["read"] == page["read"] and "SYNTHETIC_PAGING_" not in summary
    restarted = await preview(session, **saved["read"])
    assert restarted["offset"] == 0 and restarted["content"] == initial["lore_changes"][index]["after"]["content"][:2000]
    tiny = json.loads(_bounded_tool_content(record, "preview_choice_outcome", 200))
    assert tiny["metadata_omitted"] is True and "read" not in tiny


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
async def test_candidate_changes_only_invalidate_candidate_sections(candidate):
    session = long_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 13}]},
    ]}
    initial = await preview(session, context, include_candidates=candidate)
    coordinates = {**initial["read"], "result_path": ["projection", "persisted"]}
    section = await preview(session, context, **coordinates)
    assert section["value"] is False
    context["_accepted_state_updates"].append({"variable_changes": [{"id": "test_gate", "op": "set", "value": 19}]})
    result = await preview(session, context, **coordinates)
    if candidate:
        assert result["error_code"] == "snapshot_expired" and "result_path" not in result["restart"]
    else:
        assert result["snapshot"] == section["snapshot"] and result["value"] is False
    session.current_state["player"]["test_new_value"] = 1
    assert (await preview(session, context, **coordinates))["error_code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_scoped_outcome_matches_isolated_saved_choice_and_expires_on_branch_changes(tmp_path, monkeypatch):
    session = long_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        final_id = await persist_node(session, root_id)
        initial = await preview(session)
        index = next(index for index, change in enumerate(initial["state_changes"])
                     if change["path"] == ["script_variables", "test_reward"])
        coordinates = {**initial["read"], "result_path": ["state_changes", index, "after"]}
        projected = await preview(session, **coordinates)
        assert projected["value_type"] == "number" and projected["complete"] is True
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json=arguments())
        assert response.status_code == 200 and response.json()["success"] is True
        assert (await stored_state(session))["script_variables"]["test_reward"] == projected["value"]
        assert "SYNTHETIC_PAGING_" not in response.text
        assert (await preview(session, **coordinates))["error_code"] == "snapshot_expired"
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_reward"] == projected["value"]
        completed = await preview(restored)
        await restored.branch_to_node(root_id)
        stale = await preview(restored, **{**completed["read"], "result_path": []})
        assert stale["error_code"] == "snapshot_expired"
        fresh = await preview(restored)
        assert fresh["available"] is True
        await restored.branch_to_node(final_id)
        assert (await preview(restored, **{**fresh["read"], "result_path": []}))["error_code"] == "snapshot_expired"


def test_schema_exposes_json_paths_and_partial_read_boundary():
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "preview_choice_outcome")
    assert schema["parameters"]["properties"]["result_path"]["type"] == "array"
    assert "complete=false" in schema["description"] and "next_offset=null" in schema["description"]


def test_public_trace_hides_field_coordinates_and_scalar_values():
    private_marker = "SYNTHETIC_INTERNAL_FIELD"
    public = json.dumps(_public_agent_trace({"rounds": [{
        "step": 1, "tool": "preview_choice_outcome",
        "args": arguments(result_path=[private_marker], snapshot=private_marker),
        "result": {"mode": "declared_choice", "value": private_marker,
                   "read": arguments(result_path=[private_marker], snapshot=private_marker)},
    }]}))
    assert private_marker not in public


@pytest.mark.asyncio
async def test_path_can_start_without_offset_and_empty_directory_does_not_hide_long_values():
    session = long_session()
    initial = await preview(session)
    page = await preview(session, snapshot=initial["snapshot"], result_path=["available"])
    assert page["offset"] == 0 and page["value"] is True
    directory = _json_result_section({"field": "SYNTHETIC_LONG_VALUE" * 1000}, [], 0)
    assert directory["next_offset"] is None and directory["complete"] is False
    assert directory["entries"][0]["complete"] is False and "value" not in directory["entries"][0]
