import asyncio
import copy

import pytest

from engine.game_session import GameSession
from test_actor_goal_settlement import result_from
from test_generation_baseline import restore
from test_lifecycle_cascade_instructions import instructions
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import endpoint, prepare_root
from test_workflow_directive_macros import install_macro_stages, macro_session
from test_workflow_stage_directives import assert_stage_state


SUFFIX = ":scope={{var::test_stage_scope}};zero={{var::test_zero}};flag={{var::test_flag}};missing={{var::test_unknown}}"
TEMPLATES = [text + SUFFIX for text in instructions(2)]
PREPARED = {"test_stage_scope": "prepared", "test_zero": 0, "test_flag": False}


def turn_session(mode, origin="lifecycle"):
    original = macro_session(mode)
    script = copy.deepcopy(original.script)
    nodes = script["story_tree"]["trees"][0]["nodes"]
    for node, template in zip(nodes, TEMPLATES):
        node["effects"]["inject_prompt"] = template
    if origin == "story":
        nodes[0]["type"] = "auto"
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session._restore_session_models_from_state()
    return session


def assert_turn_macros(provider, variables, pending=True):
    seen = set()
    for text in provider.macro_inputs:
        for marker in instructions(2):
            if marker not in text:
                continue
            seen.add(marker)
            expected = (marker + f":scope={variables.get('test_stage_scope', '')}"
                        + f";zero={variables.get('test_zero', '')};flag={variables.get('test_flag', '')};missing=")
            assert text.count(expected) == text.count(marker), (expected, text)
    assert seen == (set(instructions(2)) if pending else set())
    for stage in ("plot", "narrative"):
        calls = [item for item in provider.observations if item["stage"] == stage]
        assert calls
        assert all(item["instructions"] == (instructions(2) if pending else []) for item in calls)


def assert_saved_templates(session, pending=True):
    node = session.world_tree.get_node(session.world_tree.active_node_id)
    assert node["turn_instructions"] == (TEMPLATES if pending else [])
    nodes = session.script["story_tree"]["trees"][0]["nodes"]
    assert [node["effects"]["inject_prompt"] for node in nodes[:3]] == TEMPLATES


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("origin", ["lifecycle", "story"])
async def test_workflow_expands_turn_instructions_through_regeneration_reload_and_swipes(
    tmp_path, monkeypatch, mode, streaming, origin,
):
    session = turn_session(mode, origin)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        for operation in ("action", "regenerate"):
            provider = install_macro_stages(session, mode)
            result = result_from(await asyncio.wait_for(client.post(
                endpoint(session, operation, streaming), **({"json": ACTION} if operation == "action" else {}),
            ), 20), stream=streaming)
            assert_turn_macros(provider, PREPARED)
            assert_saved_templates(session)
            assert_stage_state(session.current_state, True, 1)
            persisted = await stored_state(session)
            assert_stage_state(persisted, True, 1)
            assert persisted["script_variables"] == PREPARED
            if operation == "action":
                node_id = result["node_id"]
            else:
                assert result["node_id"] == node_id and result["swipe_index"] == 1
            session = await restore(session, sessions)
            assert_saved_templates(session)
            assert session.current_state["script_variables"] == PREPARED
        for index in (0, 1):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert_saved_templates(session)
            assert_stage_state(session.current_state, True, 1)
        checkpoint = await session._load_snapshot_from_db(node_id)
        session.current_state["script_variables"]["test_stage_scope"] = "continued"
        provider = install_macro_stages(session, mode)
        result_from(await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json={
            "type": "freeform", "text": "Continue the synthetic inspection without replaying the hook.",
        }), 20), stream=streaming)
        assert_turn_macros(provider, {}, pending=False)
        assert_saved_templates(session, pending=False)
        assert_stage_state(await stored_state(session), True, 2)
        assert await session._load_snapshot_from_db(node_id) == checkpoint
        session = await restore(session, sessions)
        assert_saved_templates(session, pending=False)
        assert session.current_state["script_variables"] == {**PREPARED, "test_stage_scope": "continued"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("scene", ["exploration", "opening"])
@pytest.mark.parametrize("variables", [{**PREPARED, "test_stage_scope": "candidate"}, {}])
async def test_turn_instruction_macros_use_explicit_candidate_without_mutating_context(mode, streaming, scene, variables):
    session = turn_session(mode)
    action = {"type": "freeform", "text": "Inspect the synthetic practice room."}
    context = session._prepare_turn(action)
    baseline = copy.deepcopy(session.current_state)
    baseline["script_variables"] = copy.deepcopy(variables)
    session.current_state["script_variables"]["test_stage_scope"] = "outgoing"
    raw_context = {key: copy.deepcopy(context[key]) for key in ("turn_instructions", "base_history_context", "history_context")}
    script_before = copy.deepcopy(session.script)
    provider = install_macro_stages(session, mode, scene=scene)
    route = {"scope": "moderate", "scene_type": scene, "systems": ["location"], "focus_npcs": ["test_guide"]}
    results = [item async for item in session._execute_pipeline(
        context, route, action, streaming=streaming, state_baseline=baseline,
    )]
    assert results[-1]["type"] == "pipeline_result"
    assert_turn_macros(provider, variables)
    assert {key: context[key] for key in raw_context} == raw_context
    assert context["turn_instructions"] == TEMPLATES
    assert session.script == script_before
    assert baseline["script_variables"] == variables
    assert session.current_state["script_variables"]["test_stage_scope"] == "outgoing"
