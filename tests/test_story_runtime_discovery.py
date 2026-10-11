import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.session.agentic_mixin import _bounded_tool_content, _public_agent_trace
from engine.tool_schemas import _unified_tools
from test_agent_rule_context import model_call
from test_agent_state_compaction import compact
from test_declared_choice_preview import make_session as choice_session, preview
from test_story_choice_settlement import choice_client, stored_state
from test_story_rule_retrieval import make_session


def discovery_session():
    nodes = [
        {"id": f"test_decision_{index}", "name": f"Synthetic decision {index}", "type": "choice",
         "rule_notes": "SYNTHETIC_UNREAD_DISCOVERY_RULE\n" * 2000,
         "choices": [{"id": f"test_option_{index}", "label": "Inspect",
                      "condition": "script_variables.test_gate >= 7"}]}
        for index in range(9)
    ]
    session, _ = make_session([{"id": "test_tree", "nodes": nodes}])
    session.current_state["story_tree_state"] = {
        "active": [node["id"] for node in nodes[1:8]], "completed": [nodes[0]["id"]],
        "unlocked": [node["id"] for node in nodes[:8]],
        "choices_made": {nodes[0]["id"]: "test_option_0"},
    }
    return session


def runtime_query(session, context=None, **arguments):
    return json.loads(session._handle_consult_rules({
        "category": "story_nodes", "view": "runtime", **arguments,
    }, context))


def records(result):
    return [json.loads(entry["content"]) for entry in result["entries"]]


def test_runtime_discovery_filters_and_paginates_without_dumping_rules():
    session = discovery_session()
    first = runtime_query(session, node_status="active")
    assert first["view"] == "runtime"
    assert first["node_status"] == "active"
    assert first["total"] == 7
    assert first["next_offset"] == 5
    second = runtime_query(session, node_status="active", offset=5, snapshot=first["snapshot"])
    found = records(first) + records(second)
    assert [item["node_id"] for item in found] == [f"test_decision_{index}" for index in range(1, 8)]
    assert all(item["status"] == "active" and item["tree_id"] == "test_tree" for item in found)
    assert all(item["choices"][0]["reason"] == "condition_not_met" for item in found)
    assert all(item["choices"][0]["available"] is False for item in found)
    assert second["next_offset"] is None
    assert "SYNTHETIC_UNREAD_DISCOVERY_RULE" not in json.dumps([first, second])
    assert "test_decision_7" not in json.dumps(session._script_anchor_briefs())


@pytest.mark.parametrize("status,node_id", [("completed", "test_decision_0"), ("locked", "test_decision_8")])
def test_runtime_discovery_distinguishes_status_from_choice_eligibility(status, node_id):
    session = discovery_session()
    session.current_state["script_variables"]["test_gate"] = 7
    item, = records(runtime_query(session, node_status=status))
    assert item["node_id"] == node_id
    assert item["status"] == status
    assert item["choices"][0]["available"] is False
    assert item["choices"][0]["reason"] == "node_not_active"
    if status == "completed":
        assert item["chosen_choice_id"] == "test_option_0"


def test_runtime_discovery_never_initializes_or_evaluates_state(monkeypatch):
    session = discovery_session()
    session.current_state.pop("story_tree_state")
    before = copy.deepcopy((session.current_state, session.script, session.story_tree_engine.__dict__,
                            session.event_engine.__dict__, session.prompt_builder.lorebook.__dict__))

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not initialize, migrate, or settle state")

    with monkeypatch.context() as guard:
        for engine in (session.story_tree_engine, session.event_engine):
            for method in ("_init_state", "evaluate", "make_choice"):
                if hasattr(engine, method):
                    guard.setattr(type(engine), method, forbidden)
        guard.setattr(type(session.event_engine), "_migrate_state", forbidden)
        guard.setattr(session, "_settle_story_choice", forbidden)
        result = runtime_query(session, node_status="locked")
    assert result["total"] == 9
    assert all(item["status"] == "locked" for item in records(result))
    assert (session.current_state, session.script, session.story_tree_engine.__dict__,
            session.event_engine.__dict__, session.prompt_builder.lorebook.__dict__) == before
    assert "story_tree_state" not in session.current_state


@pytest.mark.asyncio
async def test_runtime_choices_match_existing_preview_and_candidate_undo():
    session = choice_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]},
    ]}
    before = copy.deepcopy(session.current_state)
    for candidate in (False, True):
        result = runtime_query(session, context, include_candidates=candidate, node_status="active")
        item, = records(result)
        simulated = await preview(session, context, include_candidates=candidate)
        assert item["choices"][0]["available"] is simulated["available"] is candidate
        if candidate:
            assert result["projection"]["provisional"] is True
    context["_accepted_state_updates"].clear()
    item, = records(runtime_query(session, context, include_candidates=True, node_status="active"))
    assert item["choices"][0]["available"] is False
    assert session.current_state == before


def test_runtime_schema_and_context_advertise_discovery(monkeypatch):
    schema = next(tool["function"]["parameters"] for tool in _unified_tools()
                  if tool["function"]["name"] == "consult_rules")
    assert schema["properties"]["view"]["enum"] == ["definition", "runtime"]
    assert set(schema["properties"]["node_status"]["enum"]) == {"active", "completed", "unlocked", "locked"}
    session = discovery_session()
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert 'view="runtime"' in context and 'node_status="active"' in context
    assert "SYNTHETIC_UNREAD_DISCOVERY_RULE" not in context


def test_runtime_snapshot_binds_state_and_preserves_field_reread_view():
    session = discovery_session()
    catalog = runtime_query(session, node_status="active")
    entry = catalog["entries"][0]
    arguments = {"rule_index": entry["rule_index"], "snapshot": entry["snapshot"], "result_path": ["choices"]}
    field = runtime_query(session, **arguments)
    assert field["entries"][0]["read"]["view"] == "runtime"
    compacted = json.loads(_bounded_tool_content(json.dumps(field), "consult_rules", 1000))
    assert compacted["view"] == "runtime"
    assert compacted["entries"][0]["read"]["view"] == "runtime"
    session.current_state["script_variables"]["test_gate"] = 1
    expired = runtime_query(session, **arguments)
    assert expired["error_code"] == "snapshot_expired"
    assert expired["restart"]["view"] == "runtime"
    wrong_view = json.loads(session._handle_consult_rules({"category": "story_nodes", **arguments}))
    assert wrong_view["error_code"] == "snapshot_expired"


@pytest.mark.parametrize("arguments", [
    {"view": "runtime"}, {"node_status": "active"},
    {"category": "variables", "view": "runtime"},
    {"category": "story_nodes", "view": None}, {"category": "story_nodes", "view": []},
    {"category": "story_nodes", "view": "definition", "node_status": "active"},
    {"category": "story_nodes", "view": "runtime", "node_status": None},
    {"category": "story_nodes", "view": "runtime", "node_status": []},
    {"category": "story_nodes", "view": "runtime", "node_status": "available"},
    {"category": "story_nodes", "view": "runtime", "node_status": "active", "rule_index": 0},
    {"category": "story_nodes", "view": "runtime", "include_candidates": True},
    {"category": "story_nodes", "view": "definition", "include_candidates": True},
])
def test_runtime_invalid_arguments_never_fall_back_to_definition_or_question(arguments):
    result = json.loads(discovery_session()._handle_consult_rules(arguments))
    assert "error" in result and "entries" not in result and "answers" not in result


@pytest.mark.parametrize("condition", [
    "", "script_variables.test_gate >= 7", "script_variables.test_gate >= 8",
    "script_variables.test_gate >= 7 AND node_completed.test_followup == true",
    "script_variables.test_gate >= 7 OR node_completed.test_followup == true", "invalid(",
])
@pytest.mark.asyncio
async def test_runtime_choice_conditions_use_canonical_semantics(condition):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.story_tree_engine._nodes["test_decision"]["choices"][0]["condition"] = condition
    item, = records(runtime_query(session, node_status="active"))
    expected = await preview(session)
    assert item["choices"][0]["available"] is expected["available"]
    assert item["choices"][0].get("condition_error") == expected.get("condition_error")


def test_runtime_candidates_do_not_activate_unlocked_nodes():
    session = choice_session()
    session.current_state["story_tree_state"]["active"].clear()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]},
    ]}
    item, = records(runtime_query(session, context, node_status="unlocked", include_candidates=True))
    assert item["node_id"] == "test_decision" and item["status"] == "unlocked"
    assert item["choices"][0]["reason"] == "node_not_active"
    assert runtime_query(session, context, node_status="active", include_candidates=True)["entries"] == []


def test_runtime_timers_and_cooldowns_are_observed_not_advanced():
    session, _ = make_session([{"id": "test_tree", "nodes": [
        {"id": "test_timer", "type": "timed", "duration_turns": 3},
        {"id": "test_periodic", "type": "periodic", "repeatable": True},
    ]}])
    session.turn_number = 4
    session.current_state["story_tree_state"] = {
        "active": ["test_timer"], "completed": ["test_periodic"],
        "timed_progress": {"test_timer": {"started_turn": 2, "remaining": 2}},
        "periodic_cooldowns": {"test_periodic": 3},
    }
    before = copy.deepcopy(session.current_state)
    timer, periodic = records(runtime_query(session))
    assert timer["timed_progress"] == {"started_turn": 2, "remaining": 2}
    assert periodic["status"] == "completed" and periodic["cooldown_remaining_turns"] == 0
    assert session.current_state == before


def test_runtime_catalog_uses_accepted_dynamic_nodes_and_preserves_definition_view():
    session, _ = make_session()
    session._apply_director_results({"tree": {"new_nodes": [{
        "id": "test_dynamic", "type": "choice", "name": "Synthetic dynamic node",
        "choices": [{"id": "test_dynamic_option"}],
    }]}})
    result = runtime_query(session)
    item, = records(result)
    assert item["node_id"] == "test_dynamic"
    assert item["tree_id"] in session._dynamic_story_tree_ids
    assert item["type"] == "auto" and item["choices"] == []
    assert session._handle_consult_rules({"category": "story_nodes"}) == session._handle_consult_rules({
        "category": "story_nodes", "view": "definition",
    })
    session.story_tree_engine = None
    assert runtime_query(session)["entries"] == []


def test_runtime_filter_snapshots_do_not_cross_lists_or_candidate_views():
    session = discovery_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": []}
    first = runtime_query(session, node_status="active")
    for arguments in ({"node_status": "completed"}, {"node_status": "active", "include_candidates": True}):
        result = runtime_query(session, context, offset=5, snapshot=first["snapshot"], **arguments)
        assert result["error_code"] == "snapshot_expired"
        assert result["restart"]["view"] == "runtime"
        assert result["restart"]["node_status"] == arguments["node_status"]
    candidate = runtime_query(session, context, node_status="active", include_candidates=True)
    context["_accepted_state_updates"].append({"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]})
    assert runtime_query(session, context, node_status="active", include_candidates=True,
                         offset=5, snapshot=candidate["snapshot"])["error_code"] == "snapshot_expired"


@pytest.mark.parametrize("change", ["turn", "definition", "branch", "chosen"])
def test_runtime_snapshot_expires_on_non_time_changes(change):
    session = discovery_session()
    first = runtime_query(session, node_status="active")
    if change == "turn":
        session.turn_number += 1
    elif change == "definition":
        session.story_tree_engine._nodes["test_decision_7"]["rule_notes"] += "Changed."
    elif change == "branch":
        session.world_tree.active_node_id = "synthetic_other_branch"
    else:
        session.current_state["story_tree_state"]["choices_made"]["test_decision_0"] = "test_other"
    assert runtime_query(session, node_status="active", offset=5, snapshot=first["snapshot"])["error_code"] == "snapshot_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("long_labels", [False, True])
async def test_large_runtime_choice_list_supports_field_pages_and_repeated_compaction(long_labels):
    session = discovery_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    label = "SYNTHETIC_LONG_CHOICE_LABEL " * 40 if long_labels else "Synthetic option "
    session.story_tree_engine._nodes["test_decision_1"]["choices"] = [
        {"id": f"test_many_{index}", "label": f"{label}{index}"} for index in range(90)
    ]
    catalog = runtime_query(session, node_status="active")
    coordinates = {**catalog["entries"][0]["read"], "result_path": ["choices"]}
    initial = copy.deepcopy(coordinates)
    found = []
    while True:
        result = json.loads(session._handle_consult_rules(coordinates))
        section, = result["entries"]
        for entry in section["entries"]:
            choice = entry["summary"] if long_labels else entry["value"]
            found.append(choice["choice_id"])
            assert choice["available"] is True and choice["reason"] is None
            if long_labels:
                assert entry["complete"] is False and "value" not in entry
                assert set(choice) == {"choice_id", "available", "reason"}
        assert sum(len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                   for entry in section["entries"]) <= 2000
        assert section["read"]["offset"] == 0
        if section["next_offset"] is None:
            break
        coordinates["offset"] = section["next_offset"]
    assert found == [f"test_many_{index}" for index in range(90)]
    label_result = json.loads(session._handle_consult_rules({
        **initial, "result_path": ["choices", 0, "label"],
    }))["entries"][0]
    assert label_result["content"] == f"{label}0"
    definition_catalog = json.loads(session._handle_consult_rules({"category": "story_nodes", "query": "test_decision_1"}))
    definition = json.loads(session._handle_consult_rules({
        "category": "story_nodes", "rule_index": definition_catalog["entries"][0]["rule_index"],
        "snapshot": definition_catalog["entries"][0]["snapshot"], "result_path": ["definition", "node", "choices"],
    }))["entries"][0]
    assert all("summary" not in entry for entry in definition["entries"])
    content = session._handle_consult_rules(initial)
    summary = None
    for _ in range(2):
        summary = await compact(session, [("consult_rules", initial, content)], summary)
        assert '"view": "runtime"' in summary and '"result_path": ["choices"]' in summary
        assert "候选未提交且不含未结算事件" in summary and "非角色知情" in summary
        assert '"test_many_0"' not in summary
    tiny = json.loads(_bounded_tool_content(content, "consult_rules", 10))
    assert tiny["metadata_omitted"] is True
    public = json.dumps(_public_agent_trace({"rounds": [{
        "step": 1, "tool": "consult_rules", "args": initial, "result": json.loads(content),
    }]}))
    assert "test_many" not in public and initial["snapshot"] not in public and "result_path" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("long_choices", [False, True])
async def test_stream_discovers_late_choice_then_reads_previews_and_survives_reload(tmp_path, monkeypatch, long_choices):
    session = discovery_session()
    session.save_id = "synthetic-runtime-discovery-save"
    session.script["locations"] = [{"id": "test_room", "name": "Reading room"}]
    session.current_state.update({
        "_pipeline_mode": "agentic_unified", "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    })
    session.current_state["script_variables"]["test_gate"] = 7
    for node in session.story_tree_engine._nodes.values():
        choice = node["choices"][0]
        choice["condition"] = "script_variables.test_gate >= " + ("7" if node["id"] == "test_decision_7" else "13")
        choice["effects"] = {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 3}]}
    if long_choices:
        session.story_tree_engine._nodes["test_decision_7"]["choices"] = [
            {"id": f"test_late_option_{index}", "label": "SYNTHETIC_LONG_CHOICE_LABEL " * 40,
             "condition": "script_variables.test_gate >= " + ("7" if index == 89 else "13"),
             "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 3}]}}
            for index in range(90)
        ]
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
        results = []
        for message in messages:
            content = str(message.get("content", ""))
            if message["role"] == "tool" and content.startswith("{"):
                results.append(json.loads(content))
            for fragment in content.split("[剧本机制检索] ")[1:]:
                try:
                    result, _ = json.JSONDecoder().raw_decode(fragment)
                except ValueError:
                    continue
                results.append(result)
        if phase == "plan":
            phase = "discover"
            name, arguments = "submit_plan", {"plan": "Inspect the currently available declared options."}
        elif phase == "discover":
            phase = "search"
            name, arguments = "consult_rules", {"category": "story_nodes", "view": "runtime", "node_status": "active"}
        elif phase == "search":
            page = next(result for result in reversed(results) if result.get("view") == "runtime")
            incomplete = [entry for entry in page["entries"] if entry["next_offset"] is not None]
            available = [json.loads(entry["content"]) for entry in page["entries"] if entry not in incomplete
                         and any(choice["available"] for choice in json.loads(entry["content"])["choices"])]
            if incomplete:
                phase = "choices"
                name, arguments = "consult_rules", {**incomplete[0]["read"], "result_path": ["choices"]}
            elif available:
                phase = "definition"
                name, arguments = "consult_rules", {"category": "story_nodes", "query": available[0]["node_id"]}
            else:
                assert page["next_offset"] is not None
                name, arguments = "consult_rules", {
                    "category": "story_nodes", "view": "runtime", "node_status": "active",
                    "offset": page["next_offset"], "snapshot": page["snapshot"],
                }
        elif phase == "choices":
            section, = results[-1]["entries"]
            available = [entry for entry in section["entries"]
                         if entry.get("value", entry.get("summary", {})).get("available")]
            if available:
                phase = "choice"
                name, arguments = "consult_rules", {
                    **section["read"], "result_path": ["choices", available[0]["key"]],
                }
            else:
                assert section["next_offset"] is not None, "No available identity survived the choice directory"
                name, arguments = "consult_rules", {**section["read"], "offset": section["next_offset"]}
        elif phase == "choice":
            section, = results[-1]["entries"]
            assert section["summary"]["available"] is True
            phase = "node"
            name, arguments = "consult_rules", {**section["read"], "result_path": ["node_id"]}
        elif phase == "node":
            phase = "definition"
            name, arguments = "consult_rules", {"category": "story_nodes", "query": results[-1]["entries"][0]["content"]}
        elif phase == "definition":
            page = next(result for result in reversed(results) if result.get("category") == "story_nodes")
            entry, = page["entries"]
            phase = "preview"
            name, arguments = "consult_rules", {
                "category": "story_nodes", "rule_index": entry["rule_index"], "snapshot": entry["snapshot"],
                "result_path": ["definition", "node", "choices"],
            }
            if long_choices:
                selected = next(section for result in reversed(results) if result.get("view") == "runtime"
                                for section in result["entries"]
                                if len(section.get("read", {}).get("result_path", [])) == 2)
                arguments["result_path"].append(selected["read"]["result_path"][1])
        elif phase == "preview":
            section = results[-1]["entries"][0]
            if long_choices:
                section = next(entry for result in reversed(results) if result.get("view") != "runtime"
                               for entry in result.get("entries", [])
                               if entry.get("read", {}).get("result_path", [])[:3] == ["definition", "node", "choices"])
                identity = next(entry for result in reversed(results) if result.get("view") == "runtime"
                                for entry in result["entries"] if entry.get("read", {}).get("result_path") == ["node_id"])
                if "entries" not in section or "content" not in identity:
                    return {"content": "", "tool_calls": [
                        model_call("consult_rules", entry["read"], f"synthetic_discovery_{len(requests)}_{index}")
                        for index, entry in enumerate((section, identity))
                    ]}
                choice_id = next(entry["value"] for entry in section["entries"] if entry["key"] == "id")
                node_id = identity["content"]
            else:
                choice_id = section["entries"][0]["value"]["id"]
                page = next(result for result in reversed(results) if result.get("view") == "runtime")
                node, = [item for item in records(page) if any(option["available"] for option in item["choices"])]
                node_id = node["node_id"]
            phase = "narrative"
            name, arguments = "preview_choice_outcome", {"node_id": node_id, "choice_id": choice_id}
        elif phase == "narrative":
            assert results[-1]["available"] is True
            assert results[-1]["projection"]["persisted"] is False
            phase = "finalize"
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert phase == "finalize"
            phase = "finished"
            name, arguments = "finalize_turn", {"summary": "Inspected the synthetic shelf."}
        return {"content": "", "tool_calls": [model_call(name, arguments, f"synthetic_discovery_{len(requests)}")]}

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
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                  "stream_id": "synthetic_discovery_stream", "action_id": "synthetic_discovery_action", "expected_node_id": root_id}
        response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert response.status_code == 200
        assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
        final, = [event for event in events if event["type"] == "final"]
        assert phase == "finished" and final["narrative"] == narrative
        assert len(requests) <= 20
        if long_choices:
            assert any("[剧本机制检索]" in str(message.get("content", ""))
                       for request in requests for message in request)
        assert session.current_state["script_variables"]["test_progress"] == 0
        assert (await stored_state(session))["script_variables"]["test_progress"] == 0
        assert "SYNTHETIC_UNREAD_DISCOVERY_RULE" not in response.text and "result_path" not in response.text
        assert "SYNTHETIC_LONG_CHOICE_LABEL" not in response.text
        await session._drain_background_tasks()
        calls = provider.generate_with_tools.await_count
        replay = await client.post(endpoint, json=action)
        assert replay.text == response.text and provider.generate_with_tools.await_count == calls
        selected = session.story_tree_engine._nodes["test_decision_7"]
        choice_index = 89 if long_choices else 0
        payload = {"node_id": selected["id"], "choice_id": selected["choices"][choice_index]["id"]}

        def selected_available(current):
            catalog = runtime_query(current, query=payload["node_id"])
            return json.loads(current._handle_consult_rules({
                **catalog["entries"][0]["read"], "result_path": ["choices", choice_index, "available"],
            }))["entries"][0]["value"]

        choice_endpoint = f"/api/game/{session.save_id}/story-tree/choice?summary_only=true"
        assert (await client.post(choice_endpoint, json=payload)).json() == {"success": True}
        assert (await stored_state(session))["script_variables"]["test_progress"] == 3
        assert (await client.post(choice_endpoint, json=payload)).json() == {"success": False}
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        sessions.append(restored)
        completed = runtime_query(restored, query=payload["node_id"], node_status="completed")
        assert completed["total"] == 1
        assert selected_available(restored) is False
        read = completed["entries"][0]["read"]
        await restored.branch_to_node(root_id)
        assert json.loads(restored._handle_consult_rules(read))["error_code"] == "snapshot_expired"
        assert selected_available(restored) is True
        await restored.branch_to_node(final["node_id"])
        assert runtime_query(restored, query=payload["node_id"], node_status="completed")["total"] == 1
        assert restored.current_state["script_variables"]["test_progress"] == 3
