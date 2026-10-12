import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from engine.session.agentic_mixin import _bounded_tool_content, _public_agent_trace
from engine.tool_schemas import _unified_tools
from test_actor_goal_settlement import result_from
from test_agent_state_compaction import compact
from test_director_goal_persistence import goal_client
from test_scheduled_npc_overrides import expected_override, make_session
from test_story_choice_settlement import choice_client, persist_node, stored_state


CATEGORY = "npc_schedules"
MARKER = "SYNTHETIC_SCHEDULE_READING_DETAIL"


def query(session, **arguments):
    result = json.loads(session._handle_consult_rules({"category": CATEGORY, **arguments}))
    assert "error" not in result, result
    return result


def definition(session):
    entry, = query(session)["entries"]
    return json.loads(entry["content"])["definition"]


@pytest.mark.parametrize("overrides_only,dynamic", [(False, False), (True, False), (False, True)])
def test_schedule_catalog_reads_registered_declarations_without_resolving_them(monkeypatch, overrides_only, dynamic):
    session = make_session()
    actor = session.script["npcs"][0]
    actor["schedule"][0]["rule_notes"] = MARKER
    if overrides_only:
        actor.pop("schedule")
    if dynamic:
        session.current_state["npcs"]["test_visitor"] = {**copy.deepcopy(actor), "name": "Synthetic visitor"}
        session.current_state["npcs"]["test_visitor"].pop("id")
        session._index_dynamic_npcs_from_state(session.current_state)
    override = {**expected_override(session), "expires": "2029-12-31T23:00:00", "condition": "test_ready >= 2"}
    session.current_state["npc_schedule_overrides"] = {"test_guide": [override]}
    session.current_state["npcs"]["test_guide"]["schedule"] = [{"activity": "SYNTHETIC_STALE_STATE_SCHEDULE"}]
    before = copy.deepcopy((session.current_state, session.script))

    def forbidden(*args, **kwargs):
        raise AssertionError("Reading declarations must not resolve or execute schedules")

    for name in ("_evaluate_condition", "_get_npc_location", "_project_interaction_state"):
        monkeypatch.setattr(session, name, forbidden)
    monkeypatch.setattr(session.prompt_builder, "_get_schedule_entry", forbidden)
    result = query(session)
    records = [json.loads(entry["content"])["definition"] for entry in result["entries"]]
    assert records[0] == {"npc_id": "test_guide", "npc_name": "Practice guide",
                          "schedule": actor.get("schedule", []), "overrides": [override]}
    if dynamic:
        assert records[1]["npc_id"] == "test_visitor" and records[1]["overrides"] == []
        assert records[1]["schedule"] == actor["schedule"]
    assert "SYNTHETIC_STALE_STATE_SCHEDULE" not in json.dumps(result)
    assert "非角色知情" in result["notice"] and "到期" in result["notice"]
    assert (session.current_state, session.script) == before


def test_empty_schedule_catalog_does_not_expose_unknown_override_owners():
    session = make_session()
    session.script["npcs"][0].pop("schedule")
    session.current_state["npc_schedule_overrides"] = {"test_unknown": [expected_override(session)]}
    result = query(session)
    assert result["total"] == 0 and result["entries"] == [] and result["next_offset"] is None


@pytest.mark.parametrize("arguments", [{"include_candidates": True}, {"view": "runtime"}, {"node_status": "active"}])
def test_schedule_catalog_does_not_claim_candidate_or_runtime_resolution(arguments):
    session = make_session()
    result = json.loads(session._handle_consult_rules({"category": CATEGORY, **arguments},
                                                    {"old_time": session.current_state["game_time"]}))
    assert "error" in result and "entries" not in result


def test_schedule_search_pagination_and_field_reads_preserve_declarations():
    session = make_session()
    actor = session.script["npcs"][0]
    actor["schedule"][0]["rule_notes"] = (MARKER + "\n") * 180
    for index in range(6):
        session.current_state["npcs"][f"test_reader_{index}"] = {**copy.deepcopy(actor), "name": f"Synthetic reader {index}"}
        session.current_state["npcs"][f"test_reader_{index}"].pop("id")
    session._index_dynamic_npcs_from_state(session.current_state)
    first = query(session, query=MARKER.lower())
    assert first["total"] == 7 and first["next_offset"] == 5
    second = query(session, query=MARKER.lower(), offset=5, snapshot=first["snapshot"])
    assert [entry["rule_index"] for entry in second["entries"]] == [5, 6]
    entry = first["entries"][0]
    coordinates = {"rule_index": entry["rule_index"], "snapshot": entry["snapshot"],
                   "result_path": ["definition", "schedule", 0, "rule_notes"]}
    fragments, offset = [], 0
    while offset is not None:
        part, = query(session, **coordinates, offset=offset)["entries"]
        fragments.append(part["content"])
        offset = part["next_offset"]
    assert "".join(fragments) == actor["schedule"][0]["rule_notes"]


@pytest.mark.parametrize("change", ["override", "definition", "time", "branch"])
def test_schedule_rereads_reject_stale_snapshots(change):
    session = make_session()
    entry, = query(session)["entries"]
    if change == "override":
        session.current_state["npc_schedule_overrides"] = {"test_guide": [expected_override(session)]}
    elif change == "definition":
        session.script["npcs"][0]["schedule"][0]["priority"] = 25
    elif change == "time":
        session.current_state["game_time"] = "2030-01-01T09:05:00"
    else:
        session.world_tree.add_node(None, session.current_state["game_time"], 0, state_snapshot=copy.deepcopy(session.current_state))
    result = json.loads(session._handle_consult_rules({"category": CATEGORY, "rule_index": 0,
                                                    "snapshot": entry["snapshot"], "result_path": ["definition"]}))
    assert result["error_code"] == "snapshot_expired" and result["restart_required"] is True
    assert result["restart"]["category"] == CATEGORY


@pytest.mark.asyncio
async def test_schedule_coordinates_survive_bounding_and_history_compaction():
    session = make_session()
    body = (MARKER + "\n") * 180
    session.script["npcs"][0]["schedule"][0]["rule_notes"] = body
    arguments = {"category": CATEGORY}
    raw = session._handle_consult_rules(arguments)
    bounded = _bounded_tool_content(raw, "consult_rules", 1000)
    result = json.loads(bounded)
    assert "content" not in result["entries"][0]
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    summary = await compact(session, [("consult_rules", arguments, bounded)])
    summary = await compact(session, [], previous_summary=summary)
    entry = result["entries"][0]
    assert CATEGORY in summary and entry["snapshot"] in summary and MARKER not in summary
    section, = query(session, rule_index=entry["rule_index"], snapshot=entry["snapshot"],
                    result_path=["definition", "schedule", 0, "activity"])["entries"]
    assert section["content"] == "Practice rest"


def test_schedule_schema_and_public_trace_preserve_internal_boundary():
    schema = next(tool["function"] for tool in _unified_tools() if tool["function"]["name"] == "consult_rules")
    assert CATEGORY in schema["parameters"]["properties"]["category"]["enum"]
    public = _public_agent_trace({"rounds": [{"step": 1, "tool": "consult_rules",
                                            "args": {"category": CATEGORY, "query": MARKER},
                                            "result": {"entries": [{"content": MARKER}]}}]})
    assert MARKER not in json.dumps(public)


def test_schedule_context_advertises_category_without_dumping_declarations(monkeypatch):
    session = make_session()
    session.script["npcs"][0]["schedule"][0]["rule_notes"] = MARKER
    monkeypatch.setattr(session, "_build_foreground_context", lambda *args, **kwargs: "Synthetic base.")
    context = session._build_unified_context({}, {"type": "freeform", "text": "Inspect."})
    assert f'"{CATEGORY}": 1' in context and MARKER not in context
    plan = session._build_unified_plan_context({}, {"type": "freeform", "text": "Inspect."})
    assert CATEGORY not in plan and MARKER not in plan


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_schedule_overrides_follow_saved_branches_and_reload(tmp_path, monkeypatch, dynamic):
    session = make_session()
    script = copy.deepcopy(session.script)
    if dynamic:
        actor = script["npcs"].pop()
        state = copy.deepcopy(session.current_state)
        state["npcs"][actor["id"]].update({key: value for key, value in actor.items() if key != "id"})
        session = GameSession(script, None)
        session.current_state = state
        session._index_dynamic_npcs_from_state(state)
    session.save_id = "synthetic-schedule-catalog-save"
    original = copy.deepcopy(definition(session))
    async with goal_client(session, script, tmp_path, monkeypatch) as (_, sessions):
        root = session.world_tree.active_node_id
        session._apply_event_def_effects(session.script["one_time_events"][0], "test_shift")
        assert definition(session)["overrides"] == [expected_override(session)]
        branch = await persist_node(session, root)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert ("test_guide" not in restored._initial_npc_ids) is dynamic
        assert definition(restored)["overrides"] == [expected_override(restored)]
        await restored.branch_to_node(root)
        assert definition(restored) == original
        sibling = await persist_node(restored, root)
        await restored.branch_to_node(branch)
        assert definition(restored)["overrides"] == [expected_override(restored)]
        await restored.branch_to_node(sibling)
        assert definition(restored) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,fail_save", [(3, False), (8, True)])
async def test_schedule_rules_drive_agent_updates_undo_settlement_and_reload(tmp_path, monkeypatch, threshold, fail_save):
    script = copy.deepcopy(make_session().script)
    script.pop("one_time_events")
    script["lorebook"] = []
    script["npcs"][0]["schedule"].append({
        "time_range": "09:05-10:00", "location": "test_desk", "activity": "Synthetic scheduled practice",
        "condition": f"script_variables.test_ready >= {threshold}", "priority": 50, "rule_notes": (MARKER + "\n") * 180,
    })
    session = GameSession(script, None, save_id="synthetic-schedule-agent-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    requests = []
    narrative = ("You finish preparing the practice materials and wait quietly at your original seat. "
                 "You inspect the items in front of you, leave the surrounding equipment undisturbed, "
                 "and finish the check without calling anyone over.")

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments, step = "classify_intent", {"intent": "action"}, "intent"
        elif names == ["extract_notes"]:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            requests.append(copy.deepcopy(messages))
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith("schedule-read-")]
            step = len(completed)
            if step == 0:
                name, arguments = "submit_plan", {"plan": "Prepare the practice materials and wait."}
            elif step == 1:
                name, arguments = "consult_rules", {"category": CATEGORY, "query": "test_guide"}
            elif step == 2:
                entry, = json.loads(completed[1])["entries"]
                name, arguments = "consult_rules", {"category": CATEGORY, "rule_index": entry["rule_index"],
                                                    "snapshot": entry["snapshot"], "result_path": ["definition", "schedule", 1]}
            elif step in (3, 7):
                section, = json.loads(completed[2])["entries"]
                fields = {entry["key"]: entry["value"] for entry in section["entries"] if "value" in entry}
                variable, required = fields["condition"].split(" >= ")
                name, arguments = "update_state", {"end_time": "2030-01-01T" + fields["time_range"].split("-")[0] + ":00",
                                                   "variable_changes": [{"id": variable.split(".")[-1], "op": "set", "value": int(required)}]}
                if step == 7:
                    assert json.loads(completed[6])["answers"][0].startswith("Practice guide 在场")
            elif step in (4, 6, 8):
                if step == 6:
                    assert completed[5].startswith("已撤销上一个工具调用（update_state）")
                name, arguments = "consult_rules", {"question": "Practice guide 在场吗？", "include_candidates": True}
            elif step == 5:
                assert json.loads(completed[4])["answers"][0].startswith("Practice guide 不在场")
                name, arguments = "undo_my_last_action", {"reason": "Check the original presence before accepting the wait."}
            elif step == 9:
                presence = json.loads(completed[8])
                name, arguments = "write_narrative", {"text": narrative if presence["answers"][0].startswith("Practice guide 不在场")
                                                    else "The guide remains beside your seat, so you ask a question instead."}
            else:
                assert step == 10
                name, arguments = "finalize_turn", {"summary": "Prepared the materials and waited."}
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"schedule-read-{step}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value=json.dumps({
        "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
        "skill_check": {"needed": False}, "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
        "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        "pass": True, "violations": [],
    })))
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Prepare the practice materials and wait.",
                  "action_id": "synthetic-schedule-read", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result):
                assert current.current_state["script_variables"]["test_ready"] == threshold
                raise RuntimeError("Synthetic schedule save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response)
        assert result["narrative"] == narrative
        assert session.current_state["script_variables"]["test_ready"] == threshold
        assert session._get_npc_location("test_guide") == "test_desk"
        assert (await stored_state(session))["script_variables"]["test_ready"] == threshold
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored._get_npc_location("test_guide") == "test_desk"
        await restored.branch_to_node(root)
        assert restored._get_npc_location("test_guide") == "test_origin"
        await restored.branch_to_node(result["node_id"])
        assert restored._get_npc_location("test_guide") == "test_desk"
        node = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert MARKER in json.dumps(requests) and MARKER not in response.text + json.dumps(node["agent_trace"])
