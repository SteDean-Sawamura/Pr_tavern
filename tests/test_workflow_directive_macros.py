import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from test_actor_goal_settlement import result_from
from test_generation_baseline import restore
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import endpoint, prepare_root
from test_workflow_stage_directives import DIRECTIVES, assert_delivery, assert_stage_state, install_stages, stage_session


MACRO_DIRECTIVES = {
    stage: marker + ":scope={{var::test_stage_scope}};zero={{var::test_zero}};flag={{var::test_flag}};missing={{var::test_unknown}}"
    for stage, marker in DIRECTIVES.items()
}


def macro_session(mode, active=True):
    original = stage_session(mode, active)
    script = copy.deepcopy(original.script)
    script["variables"] = [{"id": "test_stage_scope", "default": "before"},
                           {"id": "test_zero", "default": 0}, {"id": "test_flag", "default": False}]
    nodes = script["story_tree"]["trees"][0]["nodes"]
    nodes[-1]["stage_directives"] = MACRO_DIRECTIVES.copy()
    nodes[0]["effects"]["set_var"].append({"var_id": "test_stage_scope", "op": "set", "value": "prepared"})
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.script_variables.init_state(session.current_state)
    session._restore_session_models_from_state()
    return session


def install_macro_stages(session, mode, scope="moderate", scene="exploration", failed=False):
    provider = install_stages(session, mode, scope, scene,
                              failed_stages=("env", "char") if failed else (), failure="exception")
    provider.macro_inputs = []

    def capture(messages):
        provider.macro_inputs.append(json.dumps(messages, ensure_ascii=False))

    original_generate = provider.generate

    async def generate(messages, **kwargs):
        capture(messages)
        return await original_generate(messages, **kwargs)

    original_stream = provider.generate_stream

    async def generate_stream(messages, **kwargs):
        capture(messages)
        async for chunk in original_stream(messages, **kwargs):
            yield chunk

    provider.generate = AsyncMock(side_effect=generate)
    provider.generate_stream = generate_stream
    if hasattr(provider, "generate_with_tools"):
        original_tools = provider.generate_with_tools

        async def generate_with_tools(messages, **kwargs):
            capture(messages)
            return await original_tools(messages, **kwargs)

        provider.generate_with_tools = AsyncMock(side_effect=generate_with_tools)
    return provider


def assert_macro_delivery(provider, variables, active=True):
    seen = set()
    for text in provider.macro_inputs:
        for stage, marker in DIRECTIVES.items():
            if marker not in text:
                continue
            seen.add(stage)
            expected = (marker + f":scope={variables.get('test_stage_scope', '')}"
                        + f";zero={variables.get('test_zero', '')};flag={variables.get('test_flag', '')};missing=")
            assert text.count(expected) == text.count(marker), (stage, expected, text)
    assert seen == (set(DIRECTIVES) if active else set())


def assert_saved_macros(session, active):
    node = session.world_tree.get_node(session.world_tree.active_node_id)
    assert node["stage_directives"] == ({stage: [text] for stage, text in MACRO_DIRECTIVES.items()} if active else {})
    assert session.script["story_tree"]["trees"][0]["nodes"][-1]["stage_directives"] == MACRO_DIRECTIVES


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,scenario", [
    (mode, scenario) for mode in ("native", "disabled", "unsupported")
    for scenario in ("normal", "minor", "social", "missing", "completed")
    if not (mode == "native" and scenario == "missing")
])
@pytest.mark.parametrize("streaming", [False, True])
async def test_workflow_expands_declared_macros_and_preserves_templates_through_reload(
    tmp_path, monkeypatch, mode, scenario, streaming,
):
    active = scenario != "completed"
    scope = "minor" if scenario == "minor" else "moderate"
    scene = "social" if scenario == "social" else "exploration"
    failed = scenario == "missing"
    session = macro_session(mode, active)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        for operation in ("action", "regenerate"):
            provider = install_macro_stages(session, mode, scope, scene, failed)
            result = result_from(await asyncio.wait_for(client.post(
                endpoint(session, operation, streaming), **({"json": ACTION} if operation == "action" else {}),
            ), 20), stream=streaming)
            expected = {"test_stage_scope": "prepared", "test_zero": 0, "test_flag": False}
            assert_macro_delivery(provider, expected, active)
            if not failed:
                assert_delivery(provider, mode, scope, scene, active, pending=True)
            assert_stage_state(session.current_state, active, 1)
            persisted = await stored_state(session)
            assert_stage_state(persisted, active, 1)
            assert persisted["script_variables"] == expected
            assert_saved_macros(session, active)
            if operation == "action":
                node_id = result["node_id"]
            else:
                assert result["node_id"] == node_id and result["swipe_index"] == 1
            session = await restore(session, sessions)
            assert_saved_macros(session, active)
            assert session.current_state["script_variables"] == expected
        for index in (0, 1):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert_saved_macros(session, active)
            assert session.current_state["script_variables"] == expected
        checkpoint = await session._load_snapshot_from_db(node_id)
        session.current_state["script_variables"]["test_stage_scope"] = "continued"
        provider = install_macro_stages(session, mode, scope, scene, failed)
        result_from(await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json={
            "type": "freeform", "text": "Continue the synthetic practice inspection.",
        }), 20), stream=streaming)
        expected["test_stage_scope"] = "continued"
        assert_macro_delivery(provider, expected, active)
        assert_saved_macros(session, active)
        assert_stage_state(await stored_state(session), active, 2)
        assert await session._load_snapshot_from_db(node_id) == checkpoint
        session = await restore(session, sessions)
        assert_saved_macros(session, active)
        assert session.current_state["script_variables"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("variables", [{"test_stage_scope": "baseline", "test_zero": 0, "test_flag": False}, {}])
async def test_workflow_macro_expansion_uses_explicit_candidate_not_outgoing_state(mode, streaming, variables):
    session = macro_session(mode)
    action = {"type": "freeform", "text": "Inspect the synthetic practice room."}
    context = session._prepare_turn(action)
    baseline = copy.deepcopy(session.current_state)
    baseline["script_variables"] = copy.deepcopy(variables)
    session.current_state["script_variables"]["test_stage_scope"] = "outgoing"
    raw_directives = copy.deepcopy(context["stage_directives"])
    provider = install_macro_stages(session, mode)
    route = {"scope": "moderate", "scene_type": "exploration", "systems": ["location"], "focus_npcs": ["test_guide"]}
    results = [item async for item in session._execute_pipeline(
        context, route, action, streaming=streaming, state_baseline=baseline,
    )]
    assert results[-1]["type"] == "pipeline_result"
    assert_macro_delivery(provider, variables)
    assert context["stage_directives"] == raw_directives
    assert baseline["script_variables"] == variables
    assert session.current_state["script_variables"]["test_stage_scope"] == "outgoing"
