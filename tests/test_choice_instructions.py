import asyncio
import copy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.event_engine import GameEvent
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client, persist_node, stored_state


NODE_INSTRUCTION = "SYNTHETIC_CHOICE_NODE_INSTRUCTION"
CHOICE_INSTRUCTION = "SYNTHETIC_CHOICE_EFFECT_INSTRUCTION"
ACTION = {"type": "freeform", "text": "Inspect the practice room."}
PENDING = "_pending_choice_instructions"
NARRATIVE = ("You inspect the practice room and carefully record the visible markings in your notebook. "
             "The room remains quiet while you check the workbench and prepare to continue your observations.")


def make_session(independent=False):
    node = {
        "id": "test_decision", "name": "Synthetic decision", "type": "choice",
        "effects": {"inject_prompt": NODE_INSTRUCTION},
        "choices": [{"id": "test_open", "label": "Inspect the room",
                     "effects": {"inject_prompt": CHOICE_INSTRUCTION}}],
    }
    script = {
        "script_id": "synthetic-choice-instructions", "name": "Synthetic choice instructions", "settings": {},
        "locations": [{"id": "test_room", "name": "Practice room"}],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [] if independent else [node]}]},
    }
    session = GameSession(script, None, save_id="synthetic-choice-instructions-save")
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Practice room"},
    }
    if independent:
        session.event_engine.add_event(GameEvent(
            id=node["id"], name=node["name"], category="choice",
            effects=node["effects"], choices=node["choices"],
        ))
        session.current_state["events"] = {node["id"]: {"status": "active"}}
    else:
        session.story_tree_engine.evaluate(session.current_state, 0, session._evaluate_condition)
        session.event_engine.sync_story_tree_state(session.current_state)
    return session


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("legacy_prompt", [False, True])
def test_choice_instructions_reach_next_generation_context(monkeypatch, independent, legacy_prompt):
    session = make_session(independent)
    if legacy_prompt:
        monkeypatch.setattr(session.prompt_builder, "build_narrative_system_prompt", lambda *args, **kwargs: "Synthetic system")
    result = session._settle_story_choice("test_decision", "test_open")
    assert result.inject_prompts == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
    context = session._prepare_turn(ACTION, legacy_prompt=legacy_prompt)
    for instruction in result.inject_prompts:
        assert instruction in context["turn_instructions"]
        assert instruction in context["history_context"]
        assert instruction in session._build_unified_context(context, ACTION)
        if legacy_prompt:
            assert instruction in context["system_prompt"]
    assert session.current_state[PENDING] == result.inject_prompts
    repeated = session._prepare_turn(ACTION)
    assert repeated["turn_instructions"] == result.inject_prompts
    assert session.current_state[PENDING] == result.inject_prompts


@pytest.mark.asyncio
async def test_choice_instructions_survive_save_reload_without_player_exposure(tmp_path, monkeypatch):
    session = make_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json={
            "node_id": "test_decision", "choice_id": "test_open",
        })
        assert response.status_code == 200 and response.json()["success"] is True
        assert NODE_INSTRUCTION not in response.text and CHOICE_INSTRUCTION not in response.text
        settled = copy.deepcopy(session.current_state)
        assert await stored_state(session) == settled
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        context = restored._prepare_turn(ACTION)
        assert context["turn_instructions"] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
        state_response = await client.get(f"/api/game/{session.save_id}/state")
        assert state_response.status_code == 200
        assert NODE_INSTRUCTION not in state_response.text and CHOICE_INSTRUCTION not in state_response.text
        assert NODE_INSTRUCTION not in json.dumps(game_routes._player_state(restored.current_state, restored.script))


@pytest.mark.parametrize("independent", [False, True])
def test_invalid_repeated_and_previewed_choices_do_not_enqueue_instructions(independent):
    session = make_session(independent)
    if not independent:
        before = copy.deepcopy(session.current_state)
        preview = json.loads(session._preview_declared_choice({"node_id": "test_decision", "choice_id": "test_open"}, {}))
        assert preview["available"] is True
        assert preview["pending_effects"]["inject_prompts"] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
        assert session.current_state == before
    assert not session._settle_story_choice("test_decision", "test_missing").newly_completed
    assert PENDING not in session.current_state
    assert session._settle_story_choice("test_decision", "test_open").newly_completed
    assert not session._settle_story_choice("test_decision", "test_open").newly_completed
    assert session.current_state[PENDING] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
    session._consume_choice_instructions(session.current_state, [NODE_INSTRUCTION])
    assert session.current_state[PENDING] == [CHOICE_INSTRUCTION]
    session._consume_choice_instructions(session.current_state, [])
    assert session.current_state[PENDING] == [CHOICE_INSTRUCTION]


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [False, True])
async def test_failed_choice_save_rolls_back_pending_instructions(tmp_path, monkeypatch, independent):
    session = make_session(independent)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)

        async def reject_save(current):
            assert current.current_state[PENDING] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
            raise RuntimeError("Synthetic choice save interruption")

        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "_persist_active_state", reject_save)
            response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json={
                "node_id": "test_decision", "choice_id": "test_open",
            })
        assert response.status_code == 500
        assert session.current_state == before and await stored_state(session) == before
        assert PENDING not in session.current_state


def install_agent(session):
    observations = []

    async def respond(messages, **kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tool_names == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "instruction-intent", "name": "classify_intent",
                                                   "arguments": {"intent": "action"}}]}
        if tool_names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "instruction-notes", "name": "extract_notes",
                                                   "arguments": {"notes": []}}]}
        completed = {message.get("tool_call_id") for message in messages if message.get("role") == "tool"}
        if "instruction-submit_plan" not in completed:
            name, arguments = "submit_plan", {"plan": "Inspect the room and record the visible result."}
        elif "instruction-write_narrative" not in completed:
            text = json.dumps(messages, ensure_ascii=False)
            observations.append([instruction for instruction in (NODE_INSTRUCTION, CHOICE_INSTRUCTION) if instruction in text])
            name, arguments = "write_narrative", {"text": NARRATIVE}
        else:
            end_time = (datetime.fromisoformat(session.current_state["game_time"]) + timedelta(minutes=1)).isoformat()
            name, arguments = "finalize_turn", {"summary": "Recorded the practice room.", "end_time": end_time}
        assert name in tool_names
        return {"content": "", "tool_calls": [{"id": f"instruction-{name}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(
        observations=observations,
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_choice_instructions_drive_one_turn_retry_reload_regeneration_and_branches(
    tmp_path, monkeypatch, independent, fail_save,
):
    session = make_session(independent)
    provider = install_agent(session)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        root = session.world_tree.active_node_id
        selected_node = await persist_node(session, root)
        choice = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json={
            "node_id": "test_decision", "choice_id": "test_open",
        })
        assert choice.json()["success"] is True
        selected = copy.deepcopy(session.current_state)
        assert selected[PENDING] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {**ACTION, "action_id": "synthetic-choice-instructions-action", "expected_node_id": selected_node}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert PENDING not in current.current_state
                assert result["narrative"] == NARRATIVE
                raise RuntimeError("Synthetic narrative save interruption")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failure = await asyncio.wait_for(client.post(endpoint, json=action), 20)
            error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
            assert error["discard_partial"] is True
            assert session.current_state == selected and await stored_state(session) == selected
            assert session.world_tree.active_node_id == selected_node
        response = await asyncio.wait_for(client.post(endpoint, json=action), 20)
        events = _stream_events(response.text)
        assert not [event for event in events if event["type"] == "error"], response.text
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == NARRATIVE
        assert all(observed == [NODE_INSTRUCTION, CHOICE_INSTRUCTION] for observed in provider.observations)
        assert provider.observations
        assert NODE_INSTRUCTION not in response.text and CHOICE_INSTRUCTION not in response.text
        assert PENDING not in session.current_state and PENDING not in await stored_state(session)
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        if fail_save:
            before_regen = copy.deepcopy(session.current_state)
            stored_before_regen = await stored_state(session)

            async def reject_regen_save(current, result):
                assert PENDING not in current.current_state
                raise RuntimeError("Synthetic regeneration save interruption")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_update_node_in_db", reject_regen_save)
                failure = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 20)
            assert failure.status_code == 500
            assert session.current_state == before_regen and await stored_state(session) == stored_before_regen
        for stage in ("all", "state"):
            regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage={stage}"), 20)
            assert regenerated.status_code == 200 and "error" not in regenerated.json(), regenerated.text
            assert PENDING not in session.current_state and PENDING not in await stored_state(session)
            assert NODE_INSTRUCTION not in regenerated.text and CHOICE_INSTRUCTION not in regenerated.text
        streamed = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate/stream?stage=all"), 20)
        streamed_events = _stream_events(streamed.text)
        assert not [event for event in streamed_events if event["type"] == "error"], streamed.text
        assert any(event["type"] == "final" for event in streamed_events), streamed.text
        assert PENDING not in session.current_state and PENDING not in await stored_state(session)
        assert NODE_INSTRUCTION not in streamed.text and CHOICE_INSTRUCTION not in streamed.text
        assert provider.observations[-1] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert PENDING not in restored.current_state
        await restored.branch_to_node(root)
        assert PENDING not in restored.current_state
        await restored.branch_to_node(selected_node)
        assert restored.current_state[PENDING] == [NODE_INSTRUCTION, CHOICE_INSTRUCTION]
        await restored.branch_to_node(final["node_id"])
        assert PENDING not in restored.current_state
        next_provider = install_agent(restored)
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=next_provider))
        next_response = await asyncio.wait_for(client.post(endpoint, json={**ACTION, "action_id": "synthetic-next-action"}), 20)
        assert any(event["type"] == "final" for event in _stream_events(next_response.text)), next_response.text
        assert next_provider.observations == [[]]
