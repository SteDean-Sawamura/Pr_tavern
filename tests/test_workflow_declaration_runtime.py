import asyncio
import copy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import NARRATIVE
from test_generation_baseline import END, assert_effects, baseline_session, restore
from test_lifecycle_cascade_instructions import instructions
from test_regenerate_stream_errors import database_state, runtime_state
from test_regeneration_parent_runtime import ACTION, CHOICE, add_model_history, model_snapshots
from test_story_choice_settlement import choice_client, stored_state


CHOICES = [{"id": f"test_workflow_{index}", "text": f"Inspect practice shelf {index}.", "risk": "safe"}
           for index in range(4)]


def workflow_session(origin="lifecycle"):
    session = baseline_session(origin)
    session.script["settings"]["ai_tools_enabled"] = True
    session.current_state["_pipeline_mode"] = "workflow"
    add_model_history(session, "parent")
    session.current_state.update(model_snapshots(session))
    return session


def install_workflow(session):
    observations = []

    def call(name, arguments, identifier=None):
        return {"id": identifier or "synthetic-workflow-" + name, "name": name, "arguments": arguments}

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if names == {"classify_intent"}:
            return {"content": "", "tool_calls": [call("classify_intent", {"intent": "action"})]}
        if names == {"extract_notes"}:
            return {"content": "", "tool_calls": [call("extract_notes", {"notes": []})]}
        if "query_lorebook" in names:
            stage = "plot"
        elif "set_atmosphere" in names:
            stage = "narrative"
        elif "update_time" in names:
            stage = "state"
        elif "add_choice" in names:
            stage = "choices"
        else:
            raise AssertionError(f"Unexpected workflow tools: {sorted(names)}")
        text = json.dumps(messages, ensure_ascii=False)
        observations.append({"stage": stage, "instructions": [item for item in instructions(2) if item in text],
                             "instruction_counts": [text.count(item) for item in instructions(2)],
                             "models": model_snapshots(session)})
        if stage == "plot":
            if not any(message.get("role") == "tool" for message in messages):
                return {"content": "", "tool_calls": [call("get_time", {})]}
            assert session.current_state["game_time"] in text
            directive = "Follow the declared inspection procedure." if all(item in text for item in instructions(2)) else "Inspect the room."
            return {"content": "[行动结果] " + directive + "\n[关键事件] The room remains quiet.", "tool_calls": []}
        if stage == "narrative":
            return {"content": NARRATIVE, "tool_calls": []}
        if stage == "state":
            if any(message.get("role") == "tool" for message in messages):
                return {"content": "", "tool_calls": []}
            end = (datetime.fromisoformat(session.current_state["game_time"]) + timedelta(minutes=1)).isoformat()
            return {"content": "", "tool_calls": [call("update_time", {"end_time": end})]}
        return {"content": "", "tool_calls": [call("add_choice", choice, f"synthetic-workflow-choice-{index}")
                                                for index, choice in enumerate(CHOICES)]}

    provider = SimpleNamespace(observations=observations, generate_with_tools=AsyncMock(side_effect=respond),
                               generate=AsyncMock(return_value=json.dumps({
                                   "scene_type": "exploration", "scope": "minor", "systems": ["location"],
                                   "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
                                   "generate_image": False, "skip_npc_reaction": True, "skip_choices": False,
                                   "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
                                   "pass": True, "violations": [],
                               })))
    session.ai_provider = provider
    return provider


def assert_workflow(provider, expected_instructions, expected_models):
    assert {item["stage"] for item in provider.observations} == {"plot", "narrative", "state", "choices"}
    plot = [item for item in provider.observations if item["stage"] == "plot"]
    assert len(plot) == 2
    assert all(item["instructions"] == expected_instructions for item in plot)
    assert all(item["instruction_counts"] == [int(instruction in expected_instructions) for instruction in instructions(2)]
               for item in plot)
    assert all(item["models"] == expected_models for item in provider.observations)
    assert all(item["instructions"] == expected_instructions for item in provider.observations
               if item["stage"] == "narrative")


async def prepare_root(session):
    node_id = session.world_tree.active_node_id
    session.world_tree.get_node(node_id)["choices_presented"] = [copy.deepcopy(CHOICE)]
    await game_routes._update_node_in_db(session, {"node_id": node_id, "choices": [CHOICE]})


def endpoint(session, operation, streaming):
    return f"/api/game/{session.save_id}/{operation}{'/stream' if streaming else ''}"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_workflow_opening_context_does_not_duplicate_turn_instructions(pending):
    session = workflow_session()
    action = {"type": "system", "text": "Begin a practice session."}
    if not pending:
        session._prepare_turn(action)
    context = session._prepare_turn(action)
    provider = install_workflow(session)
    route = json.loads(provider.generate.return_value)
    route["scene_type"] = "opening"
    expected = model_snapshots(session)
    results = [item async for item in session._execute_pipeline(context, route, action)]
    assert results[-1]["type"] == "pipeline_result"
    assert results[-1]["parsed"]["narrative"] == NARRATIVE
    assert results[-1]["parsed"]["choices"] == []
    assert "choices" not in {item["stage"] for item in provider.observations}
    result = await session._apply_parsed_response(results[-1]["parsed"], NARRATIVE, action, context)
    assert [choice["id"] for choice in result["choices"]] == [choice["id"] for choice in CHOICES]
    assert_workflow(provider, instructions(2) if pending else [], expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["lifecycle", "story"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reload_before", [False, True])
async def test_workflow_instructions_survive_regeneration_swipe_and_continuation(
    tmp_path, monkeypatch, origin, streaming, reload_before,
):
    session = workflow_session(origin)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        provider = install_workflow(session)
        initial = result_from(await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json=ACTION), 20),
                              stream=streaming)
        expected = model_snapshots(session)
        assert expected["_player_model"]["choice_history"] == ["moderate", "safe"]
        assert_workflow(provider, instructions(2), expected)
        assert_effects(session.current_state)
        assert initial["narrative"] == NARRATIVE and [choice["id"] for choice in initial["choices"]] == [choice["id"] for choice in CHOICES]
        if reload_before:
            session = await restore(session, sessions)
        outgoing = []
        for attempt in range(2):
            add_model_history(session, f"late_{attempt}")
            outgoing.append(model_snapshots(session))
            provider = install_workflow(session)
            response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", streaming)), 20)
            result = result_from(response, stream=streaming)
            assert_workflow(provider, instructions(2), expected)
            assert result["node_id"] == initial["node_id"] and result["swipe_index"] == attempt + 1
            assert result["narrative"] == NARRATIVE
            assert session.current_state["game_time"] == END and session.turn_number == 1
            assert_effects(session.current_state)
            assert model_snapshots(session) == expected
            assert not any(item in response.text for item in instructions(2))
            saved = await stored_state(session)
            assert saved["_pipeline_mode"] == "workflow"
            assert saved["_node_context"]["turn_instructions"] == instructions(2)
            variants = saved["_node_context"]["swipes"]
            assert {key: variants[attempt]["state_snapshot"][key] for key in expected} == outgoing[-1]
            session = await restore(session, sessions)
            assert model_snapshots(session) == expected
        for index, expected_variant in ((0, outgoing[0]), (2, expected)):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            assert model_snapshots(session) == expected_variant
            session = await restore(session, sessions)
            assert model_snapshots(session) == expected_variant
        checkpoint = await session._load_snapshot_from_db(initial["node_id"])
        provider = install_workflow(session)
        continued = result_from(await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json={
            "type": "freeform", "text": "Inspect another practice shelf.",
        }), 20), stream=streaming)
        assert_workflow(provider, [], expected)
        assert_effects(session.current_state)
        assert session.current_state["game_time"] == "2030-01-01T09:02:00" and session.turn_number == 2
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == initial["node_id"]
        assert await session._load_snapshot_from_db(initial["node_id"]) == checkpoint
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["save", "cancel"])
async def test_workflow_regeneration_rolls_back_before_save_and_retries(tmp_path, monkeypatch, streaming, failure):
    session = workflow_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_workflow(session)
        result_from(await client.post(endpoint(session, "action", True), json=ACTION))
        expected = model_snapshots(session)
        add_model_history(session, "late")
        before, models_before, stored_before = runtime_state(session), model_snapshots(session), await database_state(session)
        saving = asyncio.Event()

        async def reject_save(*args, **kwargs):
            if failure == "save":
                raise RuntimeError("Synthetic workflow persistence interruption")
            saving.set()
            await asyncio.Event().wait()

        provider = install_workflow(session)
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "_save_node" if streaming else "_update_node_in_db", reject_save)
            if failure == "cancel":
                request = asyncio.create_task(client.post(endpoint(session, "regenerate", streaming)))
                try:
                    await asyncio.wait_for(saving.wait(), 5)
                finally:
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
            else:
                response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", streaming)), 20)
                if streaming:
                    assert any(item["type"] == "error" for item in _stream_events(response.text))
                else:
                    assert response.status_code == 500
        assert_workflow(provider, instructions(2), expected)
        assert runtime_state(session) == before and model_snapshots(session) == models_before
        assert await database_state(session) == stored_before
        assert not game_routes._get_session_lock(session.save_id).locked()
        provider = install_workflow(session)
        result_from(await client.post(endpoint(session, "regenerate", streaming)), stream=streaming)
        assert_workflow(provider, instructions(2), expected)
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected
        assert_effects(session.current_state)
