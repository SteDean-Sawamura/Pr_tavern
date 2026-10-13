import asyncio
import copy
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest

from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_choice_instructions import NARRATIVE
from test_generation_baseline import restore
from test_lifecycle_cascade_instructions import instructions
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import DIRECTIVES, assert_stage_state, install_stages, stage_session


def install_stage_agent(session):
    provider = install_stages(session, "native", "minor", "exploration")

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if names == {"classify_intent"}:
            name, arguments = "classify_intent", {"intent": "action"}
        elif names == {"extract_notes"}:
            name, arguments = "extract_notes", {"notes": []}
        else:
            text = json.dumps(messages, ensure_ascii=False) + kwargs.get("system", "")
            received = {stage for stage, directive in DIRECTIVES.items() if directive in text}
            full = "submit_plan" in names
            choices = not full and "update_state" not in names
            planning = full and not any(message.get("tool_call_id") == "synthetic-directive-submit_plan"
                                        for message in messages)
            provider.observations.append({
                "stage": "plan" if planning else "unified" if full else "choices" if choices else "state",
                "directives": received,
                "instructions": [value for value in instructions(2) if value in text],
            })
            steps = []
            if full:
                steps.append(("submit_plan", {"plan": "Inspect the practice room using the declared guidance."}))
            if not choices:
                update = {"end_time": (datetime.fromisoformat(session.current_state["game_time"])
                                       + timedelta(minutes=1)).isoformat()}
                if "world" in received:
                    update["world_property_changes"] = [{"id": "test_inspection_status", "value": "reviewed"}]
                steps.append(("update_state", update))
                if "npc" in received:
                    steps.append(("manage_npcs", {"operations": [{
                        "op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 2,
                        "reason": "Synthetic inspection acknowledged.",
                    }]}))
            if full:
                steps.append(("write_narrative", {"text": NARRATIVE}))
            final = {"summary": "Recorded the practice inspection."}
            if full or choices:
                final["choices"] = CHOICES
            steps.append(("finalize_turn", final))
            completed = sum(message.get("role") == "tool" for message in messages)
            assert completed < len(steps)
            name, arguments = steps[completed]
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"synthetic-directive-{name}",
                                                "name": name, "arguments": arguments}]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


def install_partial_provider(session, streaming):
    if streaming:
        return install_stage_agent(session)
    provider = install_stages(session, "native", "minor", "exploration")
    prefixes = {group: session.prompt_builder._build_world_state_system(group)
                for group in ("resource", "spatial", "temporal", "world", "ext")}
    prefixes["npc"] = PromptLoader.get().render_system("npc_reaction")

    async def generate(messages, **kwargs):
        stage = next(key for key, prefix in prefixes.items() if kwargs["system"].startswith(prefix))
        text = json.dumps(messages, ensure_ascii=False)
        received = {key for key, value in DIRECTIVES.items() if value in text}
        provider.observations.append({"stage": stage, "directives": received,
                                      "instructions": [value for value in instructions(2) if value in text]})
        result = {}
        if stage == "world" and "world" in received:
            result["world_property_changes"] = [{"id": "test_inspection_status", "value": "reviewed"}]
        if stage == "npc" and "npc" in received:
            result["npc_attitude_changes"] = [{"npc_id": "test_guide", "dimension": "trust", "change": 2,
                                               "reason": "Synthetic inspection acknowledged."}]
        if stage == "temporal":
            result["end_time"] = "2030-01-01T09:01:00"
        return json.dumps(result)

    provider.generate = AsyncMock(side_effect=generate)
    return provider


def assert_partial_delivery(provider, streaming, active):
    expected = ({"state": {"world", "npc"}} if streaming else {
        "npc": {"npc"}, "world": {"world"}, "resource": set(), "spatial": set(),
        "temporal": set(), "ext": set(),
    })
    assert {item["stage"] for item in provider.observations} == set(expected)
    assert all(item["directives"] == (expected[item["stage"]] if active else set())
               for item in provider.observations), provider.observations
    assert all(item["instructions"] == instructions(2) for item in provider.observations)


@pytest.mark.parametrize("baseline,expected", [({"script_variables": {"test_scope": "baseline"}}, "baseline"), ({}, "")])
def test_instruction_macro_expansion_uses_explicit_baseline(baseline, expected):
    session = stage_session("native", True)
    session.current_state["script_variables"] = {"test_scope": "outgoing"}
    context = {"stage_directives": {"world": ["SYNTHETIC_SCOPE={{var::test_scope}}"]}}
    formatted = session._format_agent_instructions(context, ("world",), state=baseline)
    assert formatted == f"<turn_instructions>\n[world] SYNTHETIC_SCOPE={expected}\n</turn_instructions>"
    assert "outgoing" in session._format_agent_instructions(context, ("world",))
    assert session.current_state["script_variables"] == {"test_scope": "outgoing"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("action_stream", [False, True])
@pytest.mark.parametrize("partial_stream", [False, True])
@pytest.mark.parametrize("active", [False, True])
async def test_partial_state_replays_loaded_directives_without_reapplying_effects(
    tmp_path, monkeypatch, mode, action_stream, partial_stream, active,
):
    session = stage_session("native", active)
    session.current_state["_pipeline_mode"] = mode
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        provider = (install_stage_agent(session) if mode == "agentic_unified"
                    else install_stages(session, "native", "minor", "exploration"))
        initial = result_from(await asyncio.wait_for(client.post(
            endpoint(session, "action", action_stream), json=ACTION,
        ), 20), stream=action_stream)
        assert_stage_state(session.current_state, active, 1)
        if mode == "agentic_unified":
            observed = [item for item in provider.observations if item["stage"] == "unified"]
            assert observed and all(item["directives"] == (set(DIRECTIVES) if active else set())
                                    for item in observed)
            assert all(item["instructions"] == instructions(2) for item in observed)
        original_narrative, original_choices = initial["narrative"], initial["choices"]
        initial_id = initial["node_id"]
        root_id = session.world_tree.get_node(initial_id)["parent_id"]
        root_snapshot = await session._load_snapshot_from_db(root_id)
        for attempt in range(2):
            session = await restore(session, sessions)
            node = session.world_tree.get_node(initial_id)
            assert node["stage_directives"] == ({key: [value] for key, value in DIRECTIVES.items()} if active else {})
            assert node["turn_instructions"] == instructions(2)
            provider = install_partial_provider(session, partial_stream)
            response = await asyncio.wait_for(client.post(
                endpoint(session, "regenerate", partial_stream) + "?stage=state",
            ), 20)
            regenerated = result_from(response, stream=partial_stream)
            assert_partial_delivery(provider, partial_stream, active)
            assert regenerated["node_id"] == initial_id and regenerated["swipe_index"] == attempt + 1
            assert regenerated["narrative"] == original_narrative and regenerated["choices"] == original_choices
            assert not any(value in response.text for value in DIRECTIVES.values())
            assert_stage_state(session.current_state, active, 1)
            assert_stage_state(await stored_state(session), active, 1)
            assert session.turn_number == 1
        for index in (0, 2, 1, 2):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            session = await restore(session, sessions)
            assert_stage_state(session.current_state, active, 1)
        parent_snapshot = await session._load_snapshot_from_db(initial_id)
        provider = (install_stage_agent(session) if mode == "agentic_unified"
                    else install_stages(session, "native", "minor", "exploration"))
        continued = result_from(await asyncio.wait_for(client.post(
            endpoint(session, "action", action_stream), json={"type": "freeform", "text": "Continue the practice inspection."},
        ), 20), stream=action_stream)
        assert all(not item["instructions"] for item in provider.observations if item["stage"] in {"plot", "unified"})
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == initial_id
        assert await session._load_snapshot_from_db(initial_id) == parent_snapshot
        assert await session._load_snapshot_from_db(root_id) == root_snapshot
        session = await restore(session, sessions)
        assert_stage_state(session.current_state, active, 2)
        assert session.turn_number == 2 and session.current_state["_pipeline_mode"] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_choices_regeneration_does_not_receive_state_directives(tmp_path, monkeypatch, streaming):
    session = stage_session("native", True)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_stages(session, "native", "minor", "exploration")
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        session = await restore(session, sessions)
        before = copy.deepcopy(session.current_state)
        provider = (install_stage_agent(session) if streaming
                    else install_stages(session, "native", "minor", "exploration"))
        result = result_from(await client.post(endpoint(session, "regenerate", streaming) + "?stage=choices"), stream=streaming)
        assert provider.observations and all(item["stage"] == "choices" and not item["directives"]
                                             for item in provider.observations)
        assert result["narrative"] == initial["narrative"] and result["choices"] == initial["choices"]
        for key in ("player", "world_properties", "game_time", "story_tree_state"):
            assert session.current_state[key] == before[key]
        session = await restore(session, sessions)
        assert_stage_state(session.current_state, True, 1)
