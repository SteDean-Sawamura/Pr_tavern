import asyncio
import copy
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.session.agentic_mixin import _tool_result_failed
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import NARRATIVE
from test_generation_baseline import restore
from test_partial_stage_directives import install_stage_agent
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages, stage_session


RULE_MARKERS = ("SYNTHETIC_PARTIAL_VARIABLE_RULE", "SYNTHETIC_PARTIAL_WORLD_RULE")


def rules_session(variable_id="test_review_count", increment=2):
    original = stage_session("native", True)
    script = copy.deepcopy(original.script)
    script["variables"] = [{"id": variable_id, "type": "number", "default": 7,
                            "rule_notes": RULE_MARKERS[0], "review_increment": increment}]
    script["world_properties"][0].update(rule_notes=RULE_MARKERS[1], recovery_value="verified")
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.script_variables.init_state(session.current_state)
    session._restore_session_models_from_state()
    return session


def tool(name, arguments, identifier):
    return {"id": f"synthetic-recovery-{identifier}", "name": name, "arguments": arguments}


def rejected_update(variable_id):
    return {"variable_changes": [{"id": variable_id, "op": "inc", "value": 9},
                                 {"id": "test_undeclared_counter", "op": "inc", "value": 1}],
            "world_property_changes": [{"id": "test_inspection_status", "value": "rejected"}]}


def install_recovery(session):
    provider = install_stage_agent(session)
    provider.requests = []
    provider.reads = {}
    session._last_tool_result = {"name": "synthetic_prior_action"}
    undo_target = copy.deepcopy(session._last_tool_result)
    baseline = None

    async def respond(messages, **kwargs):
        nonlocal baseline
        names = {item["function"]["name"] for item in kwargs["tools"]}
        provider.requests.append(names)
        if baseline is None:
            baseline = copy.deepcopy(session.current_state)
        results = {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"}
        attempt = len(provider.requests) - 1
        if attempt <= 3:
            assert session.current_state == baseline
            assert session._last_tool_result == undo_target
        if attempt == 0:
            variable_id = next(iter(session.script_variables.definitions))
            calls = [tool("update_state", rejected_update(variable_id), "rejected")]
        elif attempt in (1, 2):
            assert "consult_rules" in results["synthetic-recovery-rejected"]
            if "consult_rules" not in names:
                return {"content": "Rule lookup is unavailable.", "tool_calls": []}
            category = "variables" if attempt == 1 else "world_properties"
            calls = [tool("consult_rules", {"category": category}, category)]
        elif attempt == 3:
            for category in ("variables", "world_properties"):
                result = json.loads(results[f"synthetic-recovery-{category}"])
                assert "error" not in result
                entry, = result["entries"]
                provider.reads[category] = json.loads(entry["content"])
            variable = provider.reads["variables"]
            world = provider.reads["world_properties"]
            assert variable["current_value"] == 7
            assert world["current_value"] == "pending"
            calls = [tool("update_state", {
                "variable_changes": [{"id": variable["definition"]["id"], "op": "inc",
                                      "value": variable["definition"]["review_increment"]}],
                "world_property_changes": [{"id": world["definition"]["id"],
                                            "value": world["definition"]["recovery_value"]}],
                "end_time": (datetime.fromisoformat(baseline["game_time"]) + timedelta(minutes=1)).isoformat(),
            }, "accepted"), tool("manage_npcs", {"operations": [{
                "op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 2,
                "reason": "Synthetic review acknowledged.",
            }]}, "npc")]
        else:
            assert attempt == 4
            assert not _tool_result_failed(results["synthetic-recovery-accepted"])
            assert not _tool_result_failed(results["synthetic-recovery-npc"])
            calls = [tool("finalize_turn", {"summary": "Completed the practice review."}, "final")]
        assert all(call["name"] in names for call in calls)
        return {"content": "", "tool_calls": calls}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("action_stream", [False, True])
@pytest.mark.parametrize("variable_id,increment", [("test_review_count", 2), ("test_inspection_count", 4)])
async def test_partial_rules_recover_rejected_updates_after_reload_and_swipe(
    tmp_path, monkeypatch, mode, action_stream, variable_id, increment,
):
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
            provider = install_recovery(session)
            response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", True) + "?stage=state"), 20)
            assert provider.requests and all("consult_rules" in names for names in provider.requests)
            result = result_from(response)
            assert result["node_id"] == node_id and result["swipe_index"] == attempt + 1
            assert result["narrative"] == initial["narrative"] and result["choices"] == initial["choices"]
            assert len(provider.requests) == 5 and len(provider.reads) == 2
            assert not any(marker in response.text for marker in RULE_MARKERS)
            assert any(step["tool"] == "consult_rules" for step in result["agent_trace"]["rounds"])
            for state in (session.current_state, await stored_state(session)):
                assert state["script_variables"] == {variable_id: 7 + increment}
                assert state["world_properties"]["test_inspection_status"] == "verified"
                assert state["player"]["test_generation_count"] == 1
                assert state["player"]["test_generation_chain"] == 2
                assert state["player"]["relationships"]["test_guide"]["trust"] == 2
                assert state["game_time"] == "2030-01-01T09:01:00"
        for index in (0, 2, 1, 2):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert session.current_state["script_variables"] == {variable_id: 7 + (increment if index else 0)}
            assert session.current_state["world_properties"]["test_inspection_status"] == ("verified" if index else "reviewed")
        parent = await session._load_snapshot_from_db(node_id)
        (install_stage_agent(session) if mode == "agentic_unified"
         else install_stages(session, "native", "minor", "exploration"))
        continued = result_from(await client.post(endpoint(session, "action", action_stream), json={
            "type": "freeform", "text": "Continue the practice review.",
        }), stream=action_stream)
        session = await restore(session, sessions)
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == node_id
        assert await session._load_snapshot_from_db(root_id) == root
        assert await session._load_snapshot_from_db(node_id) == parent
        assert session.current_state["script_variables"] == {variable_id: 7 + increment}
        assert session.current_state["player"]["relationships"]["test_guide"]["trust"] == 4
        assert session.current_state["game_time"] == "2030-01-01T09:02:00" and session.turn_number == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_tool", ["update_state", "finalize_turn"])
@pytest.mark.parametrize("recover", [False, True])
async def test_partial_rejected_records_never_enter_settlement(failed_tool, recover):
    session = rules_session()
    provider = install_stage_agent(session)
    invalid = (rejected_update("test_review_count") if failed_tool == "update_state" else {
        "narrative": "This field is forbidden in state regeneration.",
        "state_changes": [{"type": "variable", "id": "test_review_count", "op": "inc", "value": 9}],
    })
    steps = [tool(failed_tool, invalid, "rejected")]
    accepted = {"end_time": "2030-01-01T09:01:00", "variable_changes": [{
        "id": "test_review_count", "op": "inc", "value": 2,
    }]}
    if recover:
        steps.append(tool("update_state", accepted, "accepted"))
    steps.append(tool("finalize_turn", {"summary": "Completed the practice review."}, "final"))
    provider.generate_with_tools = AsyncMock(side_effect=[{"content": "", "tool_calls": [step]} for step in steps])
    baseline = copy.deepcopy(session.current_state)

    async def run():
        return [event async for event in session._regen_partial_agent("state", NARRATIVE, ACTION["text"], baseline)]

    if recover:
        result = (await run())[-1]
        expected = session._build_unified_parsed([{"name": "update_state", "args": accepted, "result": "ok"}], {})
        assert result["parsed_state"]["state_changes"] == expected["state_changes"]
        assert not result["parsed_state"]["world_property_changes"]
    else:
        with pytest.raises(RuntimeError, match="状态结算为空"):
            await run()
    assert session.current_state == baseline


@pytest.mark.asyncio
async def test_choices_regeneration_does_not_open_state_tools():
    session = rules_session()
    provider = install_stage_agent(session)
    baseline = copy.deepcopy(session.current_state)

    async def respond(messages, **kwargs):
        assert {item["function"]["name"] for item in kwargs["tools"]} == {
            "query_lorebook", "peek_upcoming_events", "consult_rules", "finalize_turn",
        }
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            call = tool("update_state", {"variable_changes": [{"id": "test_review_count", "op": "set", "value": 99}]}, "denied")
        else:
            assert "未开放工具" in results[-1]["content"]
            call = tool("finalize_turn", {"choices": CHOICES, "summary": "Practice choices."}, "final")
        return {"content": "", "tool_calls": [call]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    events = [event async for event in session._regen_partial_agent("choices", NARRATIVE, ACTION["text"], baseline)]
    assert events[-1]["choices"] == CHOICES
    assert session.current_state == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_tool", ["update_state", "finalize_turn"])
async def test_rejected_partial_state_preserves_saved_swipes_and_allows_retry(tmp_path, monkeypatch, failed_tool):
    session = rules_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_stages(session, "native", "minor", "exploration")
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        session = await restore(session, sessions)
        before = await stored_state(session)
        node_before = copy.deepcopy(session.world_tree.get_node(initial["node_id"]))
        provider = install_stage_agent(session)
        invalid = (rejected_update("test_review_count") if failed_tool == "update_state" else {
            "narrative": "This field is forbidden in state regeneration.",
            "state_changes": [{"type": "variable", "id": "test_review_count", "op": "inc", "value": 9}],
        })
        provider.generate_with_tools = AsyncMock(side_effect=[{"content": "", "tool_calls": [call]} for call in (
            tool(failed_tool, invalid, "rejected"), tool("finalize_turn", {"summary": "No accepted update."}, "final"),
        )])
        response = await client.post(endpoint(session, "regenerate", True) + "?stage=state")
        events = _stream_events(response.text)
        assert response.status_code == 200 and not any(event["type"] == "final" for event in events)
        assert any(event["type"] == "error" and event.get("discard_partial") for event in events)
        assert await stored_state(session) == before
        assert session.world_tree.get_node(initial["node_id"]) == node_before
        session = await restore(session, sessions)
        install_recovery(session)
        result = result_from(await client.post(endpoint(session, "regenerate", True) + "?stage=state"))
        assert result["node_id"] == initial["node_id"] and result["swipe_index"] == 1
        assert result["narrative"] == initial["narrative"] and result["choices"] == initial["choices"]
        session = await restore(session, sessions)
        assert session.current_state["script_variables"] == {"test_review_count": 9}
        assert session.current_state["world_properties"]["test_inspection_status"] == "verified"
