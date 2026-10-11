import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _AgentResult, _bounded_tool_content, _public_agent_trace
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import CATEGORIES, large_session, model_call
from test_agent_state_compaction import compact
from test_story_choice_settlement import choice_client, stored_state


def rule_section(session, path, category="story_nodes", context=None):
    catalog = json.loads(session._handle_consult_rules({"category": category}, context))
    entry = catalog["entries"][2]
    arguments = {"category": category, "rule_index": entry["rule_index"],
                 "snapshot": entry["snapshot"], "result_path": path}
    return json.loads(session._handle_consult_rules(arguments, context))


@pytest.mark.parametrize("category", CATEGORIES)
def test_rule_sections_preserve_snapshot_and_do_not_execute(category):
    session = large_session()
    before = copy.deepcopy((session.current_state, session.script, session.story_tree_engine.trees))
    result = rule_section(session, [], category)
    entry, = result["entries"]
    assert entry["content_format"] == "result_section"
    assert entry["read"] == {"category": category, "rule_index": 2, "snapshot": entry["snapshot"],
                            "include_candidates": False, "result_path": [], "offset": 0}
    assert entry["snapshot"] == result["snapshot"]
    definition = next(member for member in entry["entries"] if member["key"] == "definition")
    assert definition["value_type"] == "object" and definition["complete"] is False
    assert "value" not in definition and entry["complete"] is False
    assert (session.current_state, session.script, session.story_tree_engine.trees) == before


@pytest.mark.parametrize("arguments", [
    {"result_path": []},
    {"category": "story_nodes", "result_path": []},
    {"category": "story_nodes", "rule_index": 2, "result_path": []},
])
def test_rule_sections_require_category_entry_and_snapshot(arguments):
    result = json.loads(large_session()._handle_consult_rules(arguments))
    assert "error" in result and "answers" not in result


@pytest.mark.parametrize("path", [None, "definition", {}, [True], [-1], [0.5], [[]],
                                 ["missing"], ["definition", 0], ["definition", "node", "id", 0]])
def test_invalid_rule_paths_never_fall_back_to_fragments(path):
    result = rule_section(large_session(), path)
    assert "error" in result and "entries" not in result


@pytest.mark.asyncio
async def test_agent_reads_action_from_oversized_rule_without_assembling_json():
    session = large_session(11)
    session.story_tree_engine._nodes["test_node_2"]["rule_notes"] = "SYNTHETIC_UNREAD_RULE_BODY\n" * 5000
    before = copy.deepcopy((session.current_state, session.script, session.story_tree_engine.trees))
    requests = []
    tools, system = _unified_tools(), "Read the third declared node's action_rule, not its unrelated long notes."
    budget = min(30000, max(20000, 40000 - len(system) - len(json.dumps(tools, ensure_ascii=False))))
    assert len(json.dumps(session.story_tree_engine._nodes["test_node_2"])) > 4 * budget

    async def respond(messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= budget
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if not results:
            return {"content": "", "tool_calls": [
                model_call("consult_rules", {"category": category}, f"synthetic_catalog_{index}")
                for index, category in enumerate(CATEGORIES)
            ]}
        page = next(result for result in reversed(results) if result.get("category") == "story_nodes")
        entry = next(entry for entry in page["entries"] if entry["rule_index"] == 2)
        if "read" not in entry:
            coordinates = {"category": "story_nodes", "rule_index": 2, "snapshot": entry["snapshot"], "result_path": []}
        else:
            assert entry["content_format"] == "result_section"
            coordinates = dict(entry["read"])
            path = coordinates["result_path"]
            key = "definition" if not path else "node" if path == ["definition"] else "action_rule"
            member = next(member for member in entry["entries"] if member["key"] == key)
            if key == "action_rule":
                assert member["complete"] is True
                change, = member["value"]["variable_changes"]
                return {"content": f"Declared action: {change['id']} {change['op']} {change['value']}.", "tool_calls": []}
            coordinates["result_path"] = [*path, member["key"]]
        return {"content": "", "tool_calls": [model_call("consult_rules", coordinates, f"synthetic_read_{len(requests)}")]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond))
    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": system}], system, tools,
        lambda name, arguments: session._dispatch_unified_tool({}, name, arguments),
        max_rounds=20, result_holder=holder,
    )]
    assert not any(event.get("type") == "error" for event in events), events
    assert holder.text == "Declared action: test_variables_2 set 11."
    assert any(json.loads(message["content"]).get("truncated") for request in requests
               for message in request if message["role"] == "tool")
    assert "SYNTHETIC_UNREAD_RULE_BODY" not in json.dumps(requests)
    assert (session.current_state, session.script, session.story_tree_engine.trees) == before


def test_text_and_container_pages_keep_scoped_reread_coordinates():
    session = large_session()
    node = session.story_tree_engine._nodes["test_node_2"]
    node["test_members"] = [{"id": f"test_member_{index}", "value": index} for index in range(70)]
    for field in ("rule_notes", "test_members"):
        result = rule_section(session, ["definition", "node", field])
        entry, = result["entries"]
        coordinates = entry["read"]
        pages = []
        while True:
            assert entry["read"] == coordinates
            assert entry["complete"] is False
            pages.append(entry)
            if entry["next_offset"] is None:
                break
            result = json.loads(session._handle_consult_rules({**coordinates, "offset": entry["next_offset"]}))
            entry, = result["entries"]
        assert len(pages) > 1
        if field == "rule_notes":
            assert all(len(page["content"]) <= 2000 for page in pages)
            assert "".join(page["content"] for page in pages) == node[field]
        else:
            members = [member for page in pages for member in page["entries"]]
            assert [member["key"] for member in members] == list(range(70))
            assert [member["value"] for member in members] == node[field]
        assert json.loads(session._handle_consult_rules(coordinates))["entries"][0] == pages[0]


@pytest.mark.parametrize("value", [None, False, True, 0, 2.5, ""])
def test_rule_scalar_sections_preserve_json_types_and_zero_offset(value):
    session = large_session()
    session.current_state.setdefault("world_properties", {})["test_world_properties_2"] = value
    entry, = rule_section(session, ["current_value"], "world_properties")["entries"]
    assert entry["complete"] is True
    returned = entry.get("value") if entry["value_type"] != "string" else entry["content"]
    assert type(returned) is type(value) and returned == value
    invalid = json.loads(session._handle_consult_rules({**entry["read"], "offset": 1}))
    assert "error" in invalid


@pytest.mark.parametrize("change", ["definition", "other_entry", "value", "list_snapshot"])
def test_rule_sections_reject_stale_or_wrong_scope_snapshot(change):
    session = large_session()
    entry, = rule_section(session, ["current_value"], "variables")["entries"]
    coordinates = entry["read"]
    if change == "definition":
        session.script_variables.definitions["test_variables_2"]["rule_notes"] += " Synthetic revision."
    elif change == "other_entry":
        session.script_variables.definitions["test_variables_1"]["rule_notes"] += " Synthetic revision."
    elif change == "value":
        session.script_variables.set(session.current_state, "test_variables_2", 8)
    else:
        coordinates["snapshot"] = json.loads(session._handle_consult_rules({"category": "variables"}))["snapshot"]
    result = json.loads(session._handle_consult_rules(coordinates))
    assert result["error_code"] == "snapshot_expired"
    assert result["restart_required"] is True and "entries" not in result
    restarted = json.loads(session._handle_consult_rules(result["restart"]))
    assert restarted["entries"]


@pytest.mark.asyncio
async def test_candidate_field_reads_keep_committed_values_and_expire_after_update():
    session = large_session()
    context = {"old_time": session.current_state["game_time"]}
    original = copy.deepcopy(session.current_state)
    await session._dispatch_unified_tool(context, "update_state", {
        "variable_changes": [{"id": "test_variables_2", "op": "set", "value": 8}],
    })
    query = {"category": "variables", "include_candidates": True}
    catalog = json.loads(session._handle_consult_rules(query, context))
    coordinates = {**query, "rule_index": 2, "snapshot": catalog["entries"][2]["snapshot"], "result_path": ["candidate_value"]}
    result = json.loads(session._handle_consult_rules(coordinates, context))
    entry, = result["entries"]
    assert entry["value"] == 8 and entry["read"]["include_candidates"] is True
    assert result["projection"]["provisional"] is True
    bounded = json.loads(_bounded_tool_content(json.dumps(result), "consult_rules", 1000))
    assert bounded["entries"][0]["read"] == entry["read"]
    assert bounded["projection"]["provisional"] is True
    committed = json.loads(session._handle_consult_rules({**coordinates, "result_path": ["current_value"]}, context))
    assert committed["entries"][0]["value"] == original["script_variables"]["test_variables_2"]
    wrong_view = json.loads(session._handle_consult_rules({**coordinates, "include_candidates": False}, context))
    assert wrong_view["error_code"] == "snapshot_expired"
    await session._dispatch_unified_tool(context, "update_state", {
        "variable_changes": [{"id": "test_variables_2", "op": "set", "value": 9}],
    })
    stale = json.loads(session._handle_consult_rules(coordinates, context))
    assert stale["error_code"] == "snapshot_expired"
    assert session.current_state == original


@pytest.mark.asyncio
async def test_field_coordinates_survive_bounded_and_repeated_history_compaction():
    session = large_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    path = ["definition", "node", "rule_notes"]
    result = rule_section(session, path)
    coordinates = result["entries"][0]["read"]
    tail = session._handle_consult_rules({**coordinates, "offset": 2000})
    bounded = json.loads(_bounded_tool_content(tail, "consult_rules", 1000))
    assert bounded["truncated"] is True
    assert bounded["entries"][0]["read"] == coordinates
    assert "SYNTHETIC_RULE_BUDGET_BODY" not in json.dumps(bounded)
    small = json.loads(_bounded_tool_content(tail, "consult_rules", 250))
    assert small["metadata_omitted"] is True and "entries" not in small
    summary = await compact(session, [("consult_rules", coordinates, json.dumps(bounded))])
    summary = await compact(session, [], previous_summary=summary)
    assert json.dumps(coordinates, ensure_ascii=False) in summary
    assert "SYNTHETIC_RULE_BUDGET_BODY" not in summary
    assert json.loads(session._handle_consult_rules(coordinates))["entries"][0]["offset"] == 0


def test_rule_schema_and_public_trace_keep_field_reads_private():
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert schema["parameters"]["properties"]["result_path"]["type"] == "array"
    marker = "SYNTHETIC_INTERNAL_RULE_FIELD"
    public = json.dumps(_public_agent_trace({"rounds": [{
        "step": 1, "tool": "consult_rules", "args": {"category": "story_nodes", "result_path": [marker], "snapshot": marker},
        "result": {"category": "story_nodes", "entries": [{"value": marker, "read": {"result_path": [marker], "snapshot": marker}}]},
    }]}))
    assert marker not in public and "result_path" not in public


def test_rule_paths_distinguish_array_indices_from_string_keys():
    session = large_session()
    node = session.story_tree_engine._nodes["test_node_2"]
    node["test_nested"] = {"0": [{"test_value": False}, {"test_value": None}]}
    path = ["definition", "node", "test_nested", "0", 1, "test_value"]
    entry, = rule_section(session, path)["entries"]
    assert entry["value_type"] == "null" and entry["value"] is None
    assert entry["read"]["result_path"] == path
    assert "error" in rule_section(session, [*path[:3], 0])
    assert "error" in rule_section(session, [*path[:4], "1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [3, 9])
async def test_long_rule_inspection_drives_stream_settlement_reload_and_branch(tmp_path, monkeypatch, value):
    session = large_session(value)
    session.story_tree_engine._nodes["test_node_2"]["rule_notes"] = "SYNTHETIC_UNREAD_RULE_BODY\n" * 5000
    session.save_id = "synthetic-rule-inspection-save"
    session.script["locations"] = [{"id": "test_room", "name": "Reading room"}]
    session.current_state.update({
        "_pipeline_mode": "agentic_unified", "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    })
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    phase = "plan"
    requests = []

    async def respond(messages, **kwargs):
        nonlocal phase
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [model_call("extract_notes", {"notes": []}, "synthetic_notes")]}
        requests.append(copy.deepcopy(messages))
        overhead = len(kwargs["system"]) + len(json.dumps(kwargs["tools"], ensure_ascii=False))
        assert sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) <= min(30000, max(20000, 40000 - overhead))
        if phase == "plan":
            phase = "catalog"
            name, arguments = "submit_plan", {"plan": "Read the declared action before inspecting the shelf."}
        elif phase == "catalog":
            phase = "inspect"
            return {"content": "", "tool_calls": [
                model_call("consult_rules", {"category": category}, f"synthetic_stream_catalog_{index}")
                for index, category in enumerate(CATEGORIES)
            ]}
        elif phase == "inspect":
            results = [json.loads(message["content"]) for message in messages
                       if message["role"] == "tool" and message["content"].startswith("{")]
            page = next(result for result in reversed(results) if isinstance(result, dict) and result.get("category") == "story_nodes")
            entry = next(entry for entry in page["entries"] if entry["rule_index"] == 2)
            name = "consult_rules"
            if "read" not in entry:
                arguments = {"category": "story_nodes", "rule_index": 2, "snapshot": entry["snapshot"], "result_path": []}
            else:
                arguments = dict(entry["read"])
                path = arguments["result_path"]
                key = "definition" if not path else "node" if path == ["definition"] else "action_rule"
                member = next(member for member in entry["entries"] if member["key"] == key)
                if key == "action_rule":
                    assert member["complete"] is True
                    phase = "updated"
                    name, arguments = "update_state", {**member["value"], "end_time": "2030-01-01T09:01:00"}
                else:
                    arguments["result_path"] = [*path, member["key"]]
        elif phase == "updated":
            phase = "narrated"
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert phase == "narrated"
            phase = "finished"
            name, arguments = "finalize_turn", {"summary": "Inspected the synthetic shelf."}
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [model_call(name, arguments, f"synthetic_step_{len(requests)}")]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [], "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        root_id = session.world_tree.active_node_id
        action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                  "stream_id": "synthetic_inspection_stream", "action_id": "synthetic_inspection_action", "expected_node_id": root_id}
        endpoint = f"/api/game/{session.save_id}/action/stream"
        response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
        final, = [event for event in events if event["type"] == "final"]
        assert phase == "finished" and final["narrative"] == narrative
        assert session.current_state["script_variables"]["test_variables_2"] == value
        assert (await stored_state(session))["script_variables"]["test_variables_2"] == value
        assert "SYNTHETIC_UNREAD_RULE_BODY" not in json.dumps(requests)
        assert "SYNTHETIC_RULE_BUDGET_BODY" not in response.text and "result_path" not in response.text
        await session._drain_background_tasks()
        calls = provider.generate_with_tools.await_count
        replay = await client.post(endpoint, json=action)
        assert replay.text == response.text and provider.generate_with_tools.await_count == calls
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_variables_2"] == value
        trace = restored.world_tree.get_node(final["node_id"])["agent_trace"]
        assert sum(record["tool"] == "update_state" for record in trace["rounds"]) == 1
        assert "SYNTHETIC_RULE_BUDGET_BODY" not in json.dumps(trace) and "result_path" not in json.dumps(trace)
        coordinates = rule_section(restored, ["current_value"], "variables")["entries"][0]["read"]
        await restored.branch_to_node(root_id)
        assert restored.current_state["script_variables"]["test_variables_2"] == 2
        assert json.loads(restored._handle_consult_rules(coordinates))["error_code"] == "snapshot_expired"
        await restored.branch_to_node(final["node_id"])
        assert restored.current_state["script_variables"]["test_variables_2"] == value
