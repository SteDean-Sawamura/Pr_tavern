import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_choice_committed_state import ACTION, ARRIVAL, KIT, STATE_KEYS, committed_session
from test_generation_baseline import restore
from test_regenerate_stream_errors import database_state, runtime_state
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages


def settled_session(mode):
    original = committed_session("workflow")
    script = copy.deepcopy(original.script)
    script["settings"]["ai_tools_enabled"] = mode != "disabled"
    script["story_tree"]["trees"][0]["nodes"].append({
        "id": "test_settled_reward", "type": "trigger", "event": ["after_ai"],
        "effects": {"set_var": [{"var_id": "player.synthetic_awards", "op": "add", "value": 1}]},
    })
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["player"]["synthetic_awards"] = 0
    session._restore_session_models_from_state()
    return session


def install_settlement(session, mode, outcome):
    provider = install_stages(session, mode, "minor", "exploration")
    generate = provider.generate.side_effect
    native = getattr(provider, "generate_with_tools", None)
    native_response = native.side_effect if native else None
    choice_prefix = PromptLoader.get().render_system("stage_choices")
    resource_prefix = session.prompt_builder._build_world_state_system("resource", ["location"])
    spatial_prefix = session.prompt_builder._build_world_state_system("spatial", ["location"])
    inventory = [{"item": KIT, "action": "add", "quantity": 1}]
    provider.choice_requests = []

    def choices(messages):
        text = messages[-1]["content"]
        provider.choice_requests.append(text)
        if outcome == "failure":
            raise RuntimeError("SYNTHETIC_SETTLED_CHOICES_UNAVAILABLE")
        if outcome == "empty":
            return []
        resources = text.partition("当前资源: ")[2].partition("\n")[0]
        phrase = "Use acquired kit." if KIT in resources else "No kit available."
        if outcome == "filtered":
            phrase = "Consult Unseen caretaker."
        return [{**choice, "text": f"{phrase} Shelf {index}."} for index, choice in enumerate(CHOICES)]

    async def text_response(messages, **kwargs):
        if kwargs.get("raw"):
            return ARRIVAL
        system = kwargs.get("system", "")
        if system.startswith(choice_prefix):
            return json.dumps({"choices": choices(messages)})
        if system.startswith(resource_prefix):
            return json.dumps({"inventory_changes": inventory})
        if system.startswith(spatial_prefix):
            return json.dumps({"location_change": "test_annex"})
        response = await generate(messages, **kwargs)
        if outcome == "skip" and '"skip_choices"' in response:
            route = json.loads(response)
            route["skip_choices"] = True
            return json.dumps(route)
        return response

    async def stream_response(messages, **kwargs):
        yield "<think></think>"
        yield await text_response(messages, **kwargs)

    async def tool_response(messages, **kwargs):
        names = {item["function"]["name"] for item in kwargs["tools"]}
        if names == {"add_choice"}:
            if outcome == "empty":
                return {"content": json.dumps({"choices": choices(messages)}), "tool_calls": []}
            return {"content": "", "tool_calls": [
                {"id": f"synthetic-settled-choice-{index}", "name": "add_choice", "arguments": choice}
                for index, choice in enumerate(choices(messages))
            ]}
        response = await native_response(messages, **kwargs)
        if "set_atmosphere" in names:
            response["content"] = ARRIVAL
        if "update_time" in names and not any(message["role"] == "tool" for message in messages):
            response["tool_calls"].extend([
                {"id": "synthetic-settled-space", "name": "update_spatial", "arguments": {"location_change": "test_annex"}},
                {"id": "synthetic-settled-resource", "name": "update_resources", "arguments": {"inventory_changes": inventory}},
            ])
        return response

    provider.generate = AsyncMock(side_effect=text_response)
    provider.generate_stream = stream_response
    if native:
        provider.generate_with_tools = AsyncMock(side_effect=tool_response)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome", ["normal", "filtered", "failure", "empty", "skip"])
async def test_workflow_choices_observe_settlement_and_survive_rollback_reload_and_swipe(
    tmp_path, monkeypatch, mode, streaming, outcome,
):
    session = settled_session(mode)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        successful_states, successful_choices = [], []
        for operation in ("action", "regenerate"):
            before, stored = runtime_state(session), await database_state(session)
            for failing in (True, False):
                provider = install_settlement(session, mode, outcome)
                prompt_states = []
                build_choices = session.prompt_builder.build_choices_prompt

                def observe(narrative, action, state, **kwargs):
                    prompt_states.append(copy.deepcopy(state))
                    return build_choices(narrative, action, state, **kwargs)

                save_function = "_update_node_in_db" if operation == "regenerate" and not streaming else "_save_node"
                with monkeypatch.context() as patch:
                    patch.setattr(session.prompt_builder, "build_choices_prompt", observe)
                    if failing:
                        patch.setattr(game_routes, save_function, AsyncMock(side_effect=RuntimeError("SYNTHETIC_SETTLED_SAVE_FAILURE")))
                    response = await asyncio.wait_for(client.post(
                        endpoint(session, operation, streaming), **({"json": ACTION} if operation == "action" else {}),
                    ), 30)
                assert not game_routes._get_session_lock(session.save_id).locked()
                assert len(prompt_states) == (0 if outcome == "skip" else 1)
                assert len(provider.choice_requests) == (0 if outcome == "skip" else 2 if outcome == "failure" else 1)
                if failing:
                    if streaming:
                        assert '"type": "error"' in response.text and '"type": "final"' not in response.text
                    else:
                        assert response.status_code == 500
                    assert runtime_state(session) == before
                    assert await database_state(session) == stored
                    continue

                result = result_from(response, stream=streaming)
                final_state = copy.deepcopy(session.current_state)
                assert final_state["player"]["location"] == "test_annex"
                assert final_state["inventory"] == [{"item": KIT, "quantity": 1}]
                if operation == "action" or streaming:
                    assert final_state["player"]["synthetic_awards"] == 1
                if outcome != "skip":
                    for key in STATE_KEYS:
                        assert prompt_states[0].get(key) == final_state.get(key), key
                    visible = provider.choice_requests[0].partition("## 当前可见人物\n")[2].partition("\n")[0]
                    assert "Annex observer" in visible and "Practice guide" not in visible
                expected = ([f"Use acquired kit. Shelf {index}." for index in range(4)] if outcome == "normal"
                            else [choice["text"] for choice in session._generate_context_choices(final_state)])
                assert [choice["text"] for choice in result["choices"]] == expected
                node = session.world_tree.get_node(result["node_id"])
                assert node["choices_presented"] == result["choices"]
                for key in STATE_KEYS:
                    assert node["state_snapshot"].get(key) == final_state.get(key), key
                saved = await stored_state(session)
                for key in STATE_KEYS:
                    assert saved.get(key) == final_state.get(key), key
                successful_states.append(final_state)
                successful_choices.append(result["choices"])
            session = await restore(session, sessions)
            for key in STATE_KEYS:
                assert session.current_state.get(key) == successful_states[-1].get(key), key
        for index in (0, 1, 0):
            response = await client.post(f"/api/game/{session.save_id}/swipe/jump", params={"index": index})
            assert response.status_code == 200
            session = await restore(session, sessions)
            for key in STATE_KEYS:
                assert session.current_state.get(key) == successful_states[index].get(key), key
            node = session.world_tree.get_node(session.world_tree.active_node_id)
            assert node["choices_presented"] == successful_choices[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "disabled", "unsupported"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("operation", ["action", "regenerate"])
async def test_cancelled_workflow_choices_roll_back_settlement_before_retry(
    tmp_path, monkeypatch, mode, streaming, operation,
):
    session = settled_session(mode)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        if operation == "regenerate":
            install_settlement(session, mode, "normal")
            result_from(await client.post(endpoint(session, "action", streaming), json=ACTION), stream=streaming)
            session = await restore(session, sessions)
        provider = install_settlement(session, mode, "normal")
        before, stored = runtime_state(session), await database_state(session)
        started, blocked = asyncio.Event(), asyncio.Event()
        observed = []
        method = "generate_with_tools" if mode == "native" else "generate"
        generate = getattr(provider, method).side_effect
        prefix = PromptLoader.get().render_system("stage_choices")

        async def wait_for_choices(messages, **kwargs):
            names = {item["function"]["name"] for item in kwargs.get("tools", [])}
            if names == {"add_choice"} or kwargs.get("system", "").startswith(prefix):
                observed.append(copy.deepcopy(session.current_state))
                started.set()
                await blocked.wait()
            return await generate(messages, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(provider, method, AsyncMock(side_effect=wait_for_choices))
            pending = asyncio.create_task(client.post(
                endpoint(session, operation, streaming), **({"json": ACTION} if operation == "action" else {}),
            ))
            try:
                await asyncio.wait_for(started.wait(), 10)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 10)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert runtime_state(session) == before
        assert await database_state(session) == stored
        assert not game_routes._get_session_lock(session.save_id).locked()
        assert len(observed) == 1 and observed[0]["player"]["location"] == "test_annex"
        assert observed[0]["inventory"] == [{"item": KIT, "quantity": 1}]
        if operation == "action" or streaming:
            assert observed[0]["player"]["synthetic_awards"] == 1
        install_settlement(session, mode, "normal")
        result = result_from(await asyncio.wait_for(client.post(
            endpoint(session, operation, streaming), **({"json": ACTION} if operation == "action" else {}),
        ), 30), stream=streaming)
        assert [choice["text"] for choice in result["choices"]] == [f"Use acquired kit. Shelf {index}." for index in range(4)]
        session = await restore(session, sessions)
        assert session.current_state["inventory"] == [{"item": KIT, "quantity": 1}]
