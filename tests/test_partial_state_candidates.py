import asyncio
import copy
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.session.agentic_mixin import _tool_result_failed
from test_actor_goal_settlement import result_from
from test_choice_instructions import NARRATIVE
from test_generation_baseline import restore
from test_partial_rule_recovery import RULE_MARKERS, rejected_update, rules_session, tool
from test_partial_stage_directives import install_stage_agent
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import endpoint, prepare_root
from test_workflow_stage_directives import install_stages


def install_candidates(session):
    provider = install_stage_agent(session)
    provider.reads = []
    provider.baseline = None
    provider.rounds = 0
    session._last_tool_result = {"name": "synthetic_prior_action"}
    undo_target = copy.deepcopy(session._last_tool_result)

    async def respond(messages, **kwargs):
        if provider.baseline is None:
            provider.baseline = copy.deepcopy(session.current_state)
        assert session.current_state == provider.baseline
        assert session._last_tool_result == undo_target
        results = {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"}
        variable_id = next(iter(session.script_variables.definitions))
        increment = session.script_variables.definitions[variable_id]["review_increment"]
        attempt = provider.rounds
        provider.rounds += 1
        if attempt == 0:
            calls = [tool("update_state", {
                "variable_changes": [{"id": variable_id, "op": "inc", "value": increment}],
                "world_property_changes": [{"id": "test_inspection_status", "value": "checking"}],
                "end_time": (datetime.fromisoformat(provider.baseline["game_time"]) + timedelta(minutes=1)).isoformat(),
            }, "first"), tool("manage_npcs", {"operations": [{
                "op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 2,
                "reason": "Synthetic candidate review acknowledged.",
            }]}, "npc")]
        elif attempt in (1, 3):
            accepted = "first" if attempt == 1 else "second"
            assert not _tool_result_failed(results[f"synthetic-recovery-{accepted}"])
            assert "未提交" in results[f"synthetic-recovery-{accepted}"]
            calls = [tool("consult_rules", {"category": category, "include_candidates": True}, f"{category}-{attempt}")
                     for category in ("variables", "world_properties")]
            if attempt == 1:
                calls.extend([
                    tool("consult_rules", {"category": "story_nodes", "view": "runtime", "include_candidates": True}, "story"),
                    tool("consult_rules", {"question": "test_guide 接触", "include_candidates": True}, "contact"),
                ])
            else:
                assert _tool_result_failed(results["synthetic-recovery-rejected"])
                calls.extend([
                    tool("consult_rules", {"category": "variables"}, "current"),
                    tool("consult_rules", {"category": "variables", "include_candidates": True,
                                           "rule_index": 0, "snapshot": provider.reads[0]["variables"]["entries"][0]["snapshot"]}, "stale"),
                ])
        else:
            query_round = attempt - 1
            reads = {category: json.loads(results[f"synthetic-recovery-{category}-{query_round}"])
                     for category in ("variables", "world_properties")}
            provider.reads.append(reads)
            for result in reads.values():
                assert "error" not in result, result
                assert result["projection"]["provisional"] is True
                assert result["projection"]["excludes_unsettled_events"] is True
                assert result["projection"]["accepted_updates"] == attempt // 2
                assert result["projection"]["as_of"] == "2030-01-01T09:01:00"
            variable = json.loads(reads["variables"]["entries"][0]["content"])
            world = json.loads(reads["world_properties"]["entries"][0]["content"])
            assert variable["current_value"] == 7
            assert variable["candidate_value"] == 7 + increment * (attempt // 2)
            assert world["current_value"] == "pending"
            assert world["candidate_value"] == ("checking" if attempt == 2 else "verified")
            if attempt == 2:
                assert "error" in json.loads(results["synthetic-recovery-story"])
                assert "error" in json.loads(results["synthetic-recovery-contact"])
                calls = [tool("update_state", rejected_update(variable_id), "rejected"), tool("update_state", {
                    "variable_changes": [{"id": variable["definition"]["id"], "op": "set",
                                          "value": variable["candidate_value"] + variable["definition"]["review_increment"]}],
                    "world_property_changes": [{"id": world["definition"]["id"], "value": world["definition"]["recovery_value"]}],
                }, "second")]
            else:
                assert attempt == 4
                current = json.loads(results["synthetic-recovery-current"])
                assert "projection" not in current
                assert json.loads(current["entries"][0]["content"])["current_value"] == 7
                assert json.loads(results["synthetic-recovery-stale"])["error_code"] == "snapshot_expired"
                calls = [tool("finalize_turn", {"state_changes": [{
                    "type": "variable", "id": variable_id, "op": "inc", "value": 1,
                }], "summary": "Completed the candidate review."}, "final")]
        return {"content": "", "tool_calls": calls}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("action_stream", [False, True])
@pytest.mark.parametrize("variable_id,increment", [("test_review_count", 2), ("test_inspection_count", 4)])
async def test_partial_candidates_roundtrip(tmp_path, monkeypatch, mode, action_stream, variable_id, increment):
    session = rules_session(variable_id, increment)
    session.current_state["_pipeline_mode"] = mode
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        initial = result_from(await client.post(endpoint(session, "action", action_stream), json=ACTION), stream=action_stream)
        node_id = initial["node_id"]
        root_id = session.world_tree.get_node(node_id)["parent_id"]
        root = await session._load_snapshot_from_db(root_id)
        for attempt in range(2):
            session = await restore(session, sessions)
            provider = install_candidates(session)
            response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", True) + "?stage=state"), 20)
            result = result_from(response)
            assert provider.rounds == 5 and len(provider.reads) == 2
            assert result["node_id"] == node_id and result["swipe_index"] == attempt + 1
            assert result["narrative"] == initial["narrative"] and result["choices"] == initial["choices"]
            assert not any(marker in response.text for marker in RULE_MARKERS)
            for state in (session.current_state, await stored_state(session)):
                assert state["script_variables"] == {variable_id: 8 + increment * 2}
                assert state["world_properties"]["test_inspection_status"] == "verified"
                assert state["player"]["test_generation_count"] == 1
                assert state["player"]["test_generation_chain"] == 2
                assert state["player"]["relationships"]["test_guide"]["trust"] == 2
                assert state["game_time"] == "2030-01-01T09:01:00"
        for index in (0, 2, 1, 2):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert session.current_state["script_variables"] == {variable_id: 8 + increment * 2 if index else 7}
            assert session.current_state["world_properties"]["test_inspection_status"] == ("verified" if index else "reviewed")
        parent = await session._load_snapshot_from_db(node_id)
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        continued = result_from(await client.post(endpoint(session, "action", action_stream), json={
            "type": "freeform", "text": "Continue the candidate review.",
        }), stream=action_stream)
        session = await restore(session, sessions)
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == node_id
        assert await session._load_snapshot_from_db(root_id) == root
        assert await session._load_snapshot_from_db(node_id) == parent
        assert session.current_state["script_variables"] == {variable_id: 8 + increment * 2}
        assert session.current_state["player"]["relationships"]["test_guide"]["trust"] == 4
        assert session.current_state["game_time"] == "2030-01-01T09:02:00" and session.turn_number == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("timed", [False, True])
@pytest.mark.parametrize("narrative_claim", [False, True])
async def test_partial_candidate_projection_does_not_enable_turn_simulation(timed, narrative_claim):
    original = rules_session()
    script = copy.deepcopy(original.script)
    script["variables"][0]["max"] = 10
    script["one_time_events"] = [{
        "id": "test_practice_bell", "description": "A synthetic practice bell rings.",
        "trigger_time": "2030-01-01T09:01:00", "narrative_claims": ["The practice bell rings."],
        "effects": {"inject_prompt": "SYNTHETIC_CANDIDATE_EVENT_INSTRUCTION"},
    }]
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session._restore_session_models_from_state()
    narrative = NARRATIVE + (" The practice bell rings." if narrative_claim else "")
    provider = install_stage_agent(session)
    update = {"variable_changes": [{"id": "test_review_count", "op": "inc", "value": 20}]}
    if timed:
        update["end_time"] = "2030-01-01T09:01:00"
    baseline = copy.deepcopy(session.current_state)

    async def respond(messages, **kwargs):
        results = {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"}
        if not results:
            calls = [tool("update_state", update, "first")]
        elif "synthetic-recovery-query" not in results:
            calls = [tool("consult_rules", {"category": "variables", "include_candidates": True}, "query")]
        else:
            result = json.loads(results["synthetic-recovery-query"])
            assert "error" not in result, result
            assert result["projection"]["as_of"] == update.get("end_time", baseline["game_time"])
            assert json.loads(result["entries"][0]["content"])["candidate_value"] == 10
            assert "write_narrative" not in {item["function"]["name"] for item in kwargs["tools"]}
            calls = [tool("finalize_turn", {"summary": "Candidate checked."}, "final")]
        return {"content": "", "tool_calls": calls}

    dispatch = session._dispatch_unified_tool

    async def inspect_context(context, name, arguments):
        assert not context.get("old_time")
        result = await dispatch(context, name, arguments)
        assert session.current_state == baseline
        assert not context.get("candidate_event_dice_state")
        assert not context.get("_known_event_instructions")
        if name == "finalize_turn":
            assert not _tool_result_failed(result), result
        return result

    session._dispatch_unified_tool = inspect_context
    provider.generate_with_tools = AsyncMock(side_effect=respond)
    events = [event async for event in session._regen_partial_agent("state", narrative, ACTION["text"], baseline)]
    assert provider.generate_with_tools.await_count == 3
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(baseline), events[-1]["parsed_state"], inplace=True)
    assert settled["script_variables"]["test_review_count"] == 10
    assert session.current_state == baseline
