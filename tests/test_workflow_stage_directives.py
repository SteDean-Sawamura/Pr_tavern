import asyncio
import copy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_choice_instructions import NARRATIVE
from test_generation_baseline import assert_effects, restore
from test_lifecycle_cascade_instructions import instructions
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root, workflow_session


DIRECTIVES = {stage: f"SYNTHETIC_STAGE_{stage.upper()}" for stage in ("plot", "env", "char", "compose", "world", "npc")}
DETAILS = {"env": "The practice-room lamps are steady.",
           "char": "The practice guide acknowledges your checklist.",
           "compose": "The inspection record is complete."}


def stage_session(mode, active):
    original = workflow_session()
    script = copy.deepcopy(original.script)
    script["settings"]["ai_tools_enabled"] = mode != "disabled"
    script["story_tree"]["trees"][0]["nodes"].append({
        "id": "test_stage_guide", "name": "Synthetic stage guidance", "type": "quest",
        "stage_directives": DIRECTIVES.copy(),
    })
    script["npcs"] = [{"id": "test_guide", "name": "Practice guide", "location": "test_room"}]
    script["world_properties"] = [{"id": "test_inspection_status", "name": "Practice inspection status", "value": "pending"}]
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["npcs"] = {"test_guide": {"location": "test_room"}}
    session.current_state["player"]["relationships"] = {"test_guide": {"trust": 0, "affection": 0, "fear": 0}}
    session.current_state["world_properties"] = {"test_inspection_status": "pending"}
    if not active:
        session.current_state["story_tree_state"] = {"completed": ["test_stage_guide"]}
    session._restore_session_models_from_state()
    return session


def install_stages(session, mode, scope, scene, *, failed_stages=(), failure=None):
    observations = []
    loader = PromptLoader.get()
    prefixes = {stage: loader.render_system(template) for stage, template in {
        "plot": "stage_plot_decision", "env": "stage_env_render", "char": "stage_character_action",
        "npc": "npc_reaction", "choices": "stage_choices",
    }.items()}
    prefixes.update({group: session.prompt_builder._build_world_state_system(group, ["location"])
                     for group in ("resource", "spatial", "temporal", "world", "ext")})
    route = {"scene_type": scene, "scope": scope, "systems": ["location"], "focus_npcs": ["test_guide"],
             "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
             "skip_npc_reaction": False, "skip_choices": False, "skip_state_settlement": False,
             "context_depth": "normal", "recall_hints": [], "pass": True, "violations": []}

    def observe(stage, messages):
        text = json.dumps(messages, ensure_ascii=False)
        observations.append({"stage": stage, "directives": {key for key, value in DIRECTIVES.items() if value in text},
                             "instructions": [value for value in instructions(2) if value in text]})
        return text

    def narration(text):
        return NARRATIVE + " " + " ".join(detail for stage, detail in DETAILS.items()
                                          if DIRECTIVES[stage] in text or detail in text)

    def end_time():
        return (datetime.fromisoformat(session.current_state["game_time"]) + timedelta(minutes=1)).isoformat()

    def world_change(text):
        return {"world_property_changes": [{"id": "test_inspection_status", "value": "reviewed"}]} if DIRECTIVES["world"] in text else {}

    def npc_change(text):
        return {"npc_id": "test_guide", "dimension": "trust", "change": 2, "reason": "Synthetic inspection acknowledged."} if DIRECTIVES["npc"] in text else None

    def tool(name, arguments, identifier=None):
        return {"id": identifier or "synthetic-stage-" + name, "name": name, "arguments": arguments}

    async def generate(messages, **kwargs):
        if kwargs.get("raw"):
            return narration(observe("narrative", messages))
        system = kwargs.get("system", "")
        stage = next((key for key, prefix in prefixes.items() if system.startswith(prefix)), None)
        if stage is None:
            return json.dumps(route)
        text = observe(stage, messages)
        if stage == "plot":
            return "[行动结果] Inspect the practice room.\n[关键事件] The practice guide observes quietly."
        if stage in ("env", "char"):
            if stage in failed_stages:
                if failure == "exception":
                    raise RuntimeError("Synthetic stage generation failed")
                return {"empty": "", "reasoning_only": "<think>Synthetic private reasoning</think>"}[failure]
            return DETAILS[stage] if DIRECTIVES[stage] in text else "The practice inspection continues."
        if stage == "world":
            return json.dumps(world_change(text))
        if stage == "temporal":
            return json.dumps({"end_time": end_time()})
        if stage == "npc":
            change = npc_change(text)
            return json.dumps({"npc_attitude_changes": [change] if change else []})
        if stage == "choices":
            return json.dumps({"choices": CHOICES})
        return "{}"

    async def generate_stream(messages, **kwargs):
        narrative = await generate(messages, **kwargs)
        yield "<think></think>"
        yield narrative

    async def generate_with_tools(messages, **kwargs):
        names = {item["function"]["name"] for item in kwargs["tools"]}
        if names == {"classify_intent"}:
            return {"content": "", "tool_calls": [tool("classify_intent", {"intent": "action"})]}
        if names == {"extract_notes"}:
            return {"content": "", "tool_calls": [tool("extract_notes", {"notes": []})]}
        if "query_lorebook" in names:
            text = observe("plot", messages)
            if not any(message.get("role") == "tool" for message in messages):
                return {"content": "", "tool_calls": [tool("get_time", {})]}
            assert session.current_state["game_time"] in text
            return {"content": "[行动结果] Inspect the practice room.\n[关键事件] The practice guide observes quietly.", "tool_calls": []}
        if "set_atmosphere" in names:
            return {"content": narration(observe("narrative", messages)), "tool_calls": []}
        if "update_time" in names:
            text = observe("state", messages)
            calls = []
            if not any(message.get("role") == "tool" for message in messages):
                calls.append(tool("update_time", {"end_time": end_time()}))
                if world_change(text):
                    calls.append(tool("update_world", world_change(text)))
            return {"content": "", "tool_calls": calls}
        if "update_npc_attitude" in names:
            change = npc_change(observe("npc", messages))
            return {"content": "", "tool_calls": [tool("update_npc_attitude", change)] if change else []}
        assert "add_choice" in names, sorted(names)
        observe("choices", messages)
        return {"content": "", "tool_calls": [tool("add_choice", choice, f"synthetic-stage-choice-{index}")
                                                for index, choice in enumerate(CHOICES)]}

    provider = SimpleNamespace(observations=observations, generate=AsyncMock(side_effect=generate),
                               generate_stream=generate_stream)
    if mode != "unsupported":
        provider.generate_with_tools = AsyncMock(side_effect=generate_with_tools)
    session.ai_provider = provider
    return provider


def assert_delivery(provider, mode, scope, scene, active, pending):
    expected = {"plot": {"plot"}, "narrative": {"compose"}, "npc": {"npc"}, "choices": set()}
    if mode == "native":
        expected["narrative"].update(("env", "char"))
        expected["state"] = {"world"}
    else:
        expected.update({group: {"world"} if group == "world" else set()
                         for group in ("resource", "spatial", "temporal", "world", "ext")})
        for stage, skipped in (("env", scope == "minor" or scene in ("social", "rest")), ("char", scope == "minor")):
            if skipped:
                expected["narrative"].add(stage)
            else:
                expected[stage] = {stage}
    assert {item["stage"] for item in provider.observations} == set(expected)
    assert sum(item["stage"] == "narrative" for item in provider.observations) == 1
    actual = {stage: [item["directives"] for item in provider.observations if item["stage"] == stage] for stage in expected}
    assert actual == {stage: [values if active else set()] * len(actual[stage]) for stage, values in expected.items()}
    assert all(item["instructions"] == (instructions(2) if pending else [])
               for item in provider.observations if item["stage"] == "plot")


def assert_stage_state(state, active, turns):
    assert_effects(state)
    assert state["world_properties"]["test_inspection_status"] == ("reviewed" if active else "pending")
    assert state["player"]["relationships"]["test_guide"]["trust"] == (2 * turns if active else 0)
    assert state["game_time"] == f"2030-01-01T09:0{turns}:00"
    assert ("test_stage_guide" in state["story_tree_state"]["active"]) is active


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("scope,scene,streaming,active", [
    (scope, scene, streaming, True)
    for scope, scene in (("minor", "exploration"), ("moderate", "exploration"), ("moderate", "social"))
    for streaming in (False, True)
] + [("minor", "exploration", False, False)])
async def test_workflow_stage_directives_survive_routes_regeneration_and_reload(
    tmp_path, monkeypatch, mode, scope, scene, streaming, active,
):
    session = stage_session(mode, active)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        for operation in ("action", "regenerate"):
            provider = install_stages(session, mode, scope, scene)
            kwargs = {"json": ACTION} if operation == "action" else {}
            response = await asyncio.wait_for(client.post(endpoint(session, operation, streaming), **kwargs), 20)
            result = result_from(response, stream=streaming)
            assert_delivery(provider, mode, scope, scene, active, pending=True)
            for detail in DETAILS.values():
                assert (detail in result["narrative"]) is active
            assert not any(value in result["narrative"] for value in DIRECTIVES.values())
            assert [choice["id"] for choice in result["choices"]] == [choice["id"] for choice in CHOICES]
            assert_stage_state(session.current_state, active, 1)
            assert_stage_state(await stored_state(session), active, 1)
            assert session.turn_number == 1
            if operation == "action":
                initial_id = result["node_id"]
            else:
                assert result["node_id"] == initial_id and result["swipe_index"] == 1
            session = await restore(session, sessions)
            assert_stage_state(session.current_state, active, 1)
        for index in (0, 1):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert_stage_state(session.current_state, active, 1)
        checkpoint = await session._load_snapshot_from_db(initial_id)
        provider = install_stages(session, mode, scope, scene)
        continued = result_from(await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json={
            "type": "freeform", "text": "Continue the synthetic practice inspection.",
        }), 20), stream=streaming)
        assert_delivery(provider, mode, scope, scene, active, pending=False)
        assert_stage_state(session.current_state, active, 2)
        assert_stage_state(await stored_state(session), active, 2)
        assert session.turn_number == 2
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == initial_id
        assert await session._load_snapshot_from_db(initial_id) == checkpoint
        session = await restore(session, sessions)
        assert_stage_state(session.current_state, active, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "unsupported"])
@pytest.mark.parametrize("failure", ["exception", "empty", "reasoning_only"])
@pytest.mark.parametrize("failed_stages", [("env",), ("char",), ("env", "char")])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("active", [False, True])
async def test_workflow_missing_stage_output_preserves_declared_directives(
    tmp_path, monkeypatch, mode, failure, failed_stages, streaming, active,
):
    session = stage_session(mode, active)
    narratives = {}
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        for operation in ("action", "regenerate", "continue"):
            provider = install_stages(session, mode, "moderate", "exploration",
                                      failed_stages=failed_stages, failure=failure)
            url = endpoint(session, "action" if operation == "continue" else operation, streaming)
            kwargs = {"json": ACTION} if operation == "action" else {"json": {
                "type": "freeform", "text": "Continue the synthetic practice inspection.",
            }} if operation == "continue" else {}
            result = result_from(await asyncio.wait_for(client.post(url, **kwargs), 20), stream=streaming)
            composed = [item for item in provider.observations if item["stage"] == "narrative"]
            assert len(composed) == 1
            assert composed[0]["directives"] == ({"compose", *failed_stages} if active else set())
            for stage in ("env", "char"):
                attempted = [item for item in provider.observations if item["stage"] == stage]
                assert len(attempted) == 1
                assert attempted[0]["directives"] == ({stage} if active else set())
            for detail in DETAILS.values():
                assert (detail in result["narrative"]) is active
            assert not any(value in result["narrative"] for value in DIRECTIVES.values())
            assert "Synthetic private reasoning" not in result["narrative"]
            assert [choice["id"] for choice in result["choices"]] == [choice["id"] for choice in CHOICES]
            turns = 2 if operation == "continue" else 1
            assert_stage_state(session.current_state, active, turns)
            assert_stage_state(await stored_state(session), active, turns)
            assert session.turn_number == turns
            if operation == "action":
                initial_id = result["node_id"]
                narratives[0] = result["narrative"]
            elif operation == "regenerate":
                assert result["node_id"] == initial_id and result["swipe_index"] == 1
                narratives[1] = result["narrative"]
            else:
                assert session.world_tree.get_node(result["node_id"])["parent_id"] == initial_id
                assert await session._load_snapshot_from_db(initial_id) == checkpoint
            session = await restore(session, sessions)
            assert_stage_state(session.current_state, active, turns)
            restored_node = session.world_tree.get_node(result["node_id"])
            assert restored_node["ai_response"] == result["narrative"]
            assert restored_node["stage_directives"] == ({stage: [text] for stage, text in DIRECTIVES.items()} if active else {})
            if operation == "regenerate":
                for index in (0, 1):
                    result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
                    session = await restore(session, sessions)
                    assert_stage_state(session.current_state, active, 1)
                    restored_node = session.world_tree.get_node(initial_id)
                    assert restored_node["ai_response"] == narratives[index]
                    assert restored_node["stage_directives"] == ({stage: [text] for stage, text in DIRECTIVES.items()} if active else {})
                checkpoint = await session._load_snapshot_from_db(initial_id)
