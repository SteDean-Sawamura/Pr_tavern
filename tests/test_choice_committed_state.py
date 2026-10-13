import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from test_actor_goal_settlement import result_from
from test_choice_instructions import NARRATIVE, install_agent
from test_generation_baseline import assert_effects, restore
from test_lifecycle_cascade_instructions import instructions
from test_regenerate_stream_errors import database_state, runtime_state
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages, stage_session


KIT = "SYNTHETIC_CURRENT_KIT"
ACTION = {"type": "freeform", "text": f"Move to Practice annex and collect {KIT}."}
ARRIVAL = NARRATIVE + f" You move to Practice annex and receive {KIT}."
STATE_KEYS = ("player", "inventory", "npcs", "game_time", "world_properties", "script_variables", "visible_locations")


def committed_session(mode):
    original = stage_session("native", True)
    script = copy.deepcopy(original.script)
    script["locations"].append({"id": "test_annex", "name": "Practice annex"})
    script["npcs"][0]["default_location"] = "test_room"
    script["npcs"].extend([
        {"id": "test_observer", "name": "Annex observer", "default_location": "test_annex"},
        {"id": "test_unseen", "name": "Unseen caretaker", "default_location": "test_room", "known": False},
    ])
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["_pipeline_mode"] = mode
    session.current_state["visible_locations"].append("test_annex")
    session.current_state["npcs"]["test_guide"]["current_location"] = "test_room"
    session.current_state["npcs"].update({
        "test_observer": {"current_location": "test_annex"},
        "test_unseen": {"current_location": "test_room", "known": False},
    })
    session.current_state["inventory"] = []
    session._restore_session_models_from_state()
    return session


def install_arrival(session, mode):
    provider = (install_agent(session, instructions(2)) if mode == "agentic_unified"
                else install_stages(session, "native", "minor", "exploration"))
    native = provider.generate_with_tools.side_effect
    generate = provider.generate.side_effect
    inventory = [{"item": KIT, "action": "add", "quantity": 1}]

    async def respond(messages, **kwargs):
        response = await native(messages, **kwargs)
        names = {item["function"]["name"] for item in kwargs["tools"]}
        if "set_atmosphere" in names:
            response["content"] = ARRIVAL
        for call in response.get("tool_calls", []):
            if call["name"] == "write_narrative":
                call["arguments"]["text"] = ARRIVAL
            elif call["name"] == "finalize_turn":
                call["arguments"].update(location_change="test_annex", inventory_changes=inventory)
        if "update_time" in names and not any(message["role"] == "tool" for message in messages):
            response["tool_calls"].extend([
                {"id": "synthetic-arrival-spatial", "name": "update_spatial", "arguments": {"location_change": "test_annex"}},
                {"id": "synthetic-arrival-resource", "name": "update_resources", "arguments": {"inventory_changes": inventory}},
            ])
        return response

    async def text_response(messages, **kwargs):
        if kwargs.get("raw"):
            return ARRIVAL
        if generate is not None:
            return await generate(messages, **kwargs)
        return provider.generate.return_value

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    original_value = provider.generate.return_value
    provider.generate = AsyncMock(side_effect=text_response, return_value=original_value)
    return provider


def assert_committed(session, committed):
    for key in STATE_KEYS:
        assert session.current_state.get(key) == committed.get(key), key
    assert_effects(session.current_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "agentic_unified"])
@pytest.mark.parametrize("initial_stream", [False, True])
@pytest.mark.parametrize("filtered_choices", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_choice_regeneration_uses_committed_state_after_action_and_reload(
    tmp_path, monkeypatch, mode, initial_stream, filtered_choices, fail_save,
):
    session = committed_session(mode)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        parent = copy.deepcopy(session.current_state)
        install_arrival(session, mode)
        initial = result_from(await client.post(endpoint(session, "action", initial_stream), json=ACTION), stream=initial_stream)
        await session._drain_background_tasks()
        session = await restore(session, sessions)
        committed = copy.deepcopy(session.current_state)
        assert committed["player"]["location"] == "test_annex"
        assert KIT in {item["item"] for item in committed["inventory"]}
        assert parent["player"]["location"] == "test_room" and not parent["inventory"]
        assert committed["game_time"] != parent["game_time"]
        expected = ([choice["text"] for choice in session._generate_context_choices(committed)] if filtered_choices
                    else [f"Inspect with the acquired kit. Shelf {index}." for index in range(4)])

        for repeat in range(2):
            before, stored = runtime_state(session), await database_state(session)
            failing_first = fail_save and repeat == 0
            for attempt in range(2 if failing_first else 1):
                failing = failing_first and attempt == 0
                requests, prompt_states = [], []
                build_choices = session.prompt_builder.build_choices_prompt

                def observe(narrative, action, state, **kwargs):
                    prompt_states.append(copy.deepcopy(state))
                    return build_choices(narrative, action, state, **kwargs)

                async def generate(messages, **kwargs):
                    text = messages[-1]["content"]
                    requests.append(text)
                    resources = text.partition("当前资源: ")[2].partition("\n")[0]
                    phrase = "Inspect with the acquired kit." if KIT in resources else "Inspect without the acquired kit."
                    if filtered_choices:
                        phrase = "Consult Unseen caretaker."
                    return json.dumps({"choices": [
                        {**choice, "text": f"{phrase} Shelf {index}."} for index, choice in enumerate(CHOICES)
                    ]})

                provider = install_stages(session, "native", "minor", "exploration")
                provider.generate = AsyncMock(side_effect=generate)
                with monkeypatch.context() as patch:
                    patch.setattr(session.prompt_builder, "build_choices_prompt", observe)
                    if failing:
                        patch.setattr(game_routes, "_update_node_in_db", AsyncMock(side_effect=RuntimeError("SYNTHETIC_COMMITTED_SAVE_FAILURE")))
                    response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", False), params={"stage": "choices"}), 20)
                assert not game_routes._get_session_lock(session.save_id).locked()
                for control in (
                    "_active_agent_stream", "_active_agent_stream_id", "_agent_inject_enabled",
                    "_inject_queue", "_pending_agent_plan_id", "_abort_request_pending",
                ):
                    assert not getattr(session, control, None), control
                if failing:
                    assert response.status_code == 500
                    assert runtime_state(session) == before
                    assert await database_state(session) == stored
                else:
                    regenerated = result_from(response, stream=False)
                    assert [choice["text"] for choice in regenerated["choices"]] == expected
                    assert regenerated["narrative"] == initial["narrative"]
                    assert regenerated["state_changes"] == initial["state_changes"]
                    assert regenerated["swipe_index"] == repeat + 1
                    assert regenerated["total_swipes"] == repeat + 2
                    saved = await stored_state(session)
                    assert [choice["text"] for choice in saved["_node_context"]["swipes"][-1]["choices"]] == expected
                assert len(prompt_states) == len(requests) == 1
                for key in STATE_KEYS:
                    assert prompt_states[0].get(key) == committed.get(key), key
                visible = requests[0].partition("## 当前可见人物\n")[2].partition("\n")[0]
                assert "Annex observer" in visible and "Practice guide" not in visible
                assert_committed(session, committed)
                session = await restore(session, sessions)
                assert_committed(session, committed)

        for index in (0, 1, 2, 0):
            switched = await client.post(f"/api/game/{session.save_id}/swipe/jump", params={"index": index})
            assert switched.status_code == 200
            session = await restore(session, sessions)
            assert_committed(session, committed)
            node = session.world_tree.get_node(initial["node_id"])
            texts = [choice["text"] for choice in initial["choices"]] if index == 0 else expected
            assert [choice["text"] for choice in node["choices_presented"]] == texts
