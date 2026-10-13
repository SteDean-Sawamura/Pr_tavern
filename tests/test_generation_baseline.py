import asyncio
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest

from api import game_routes
from engine.game_session import GameSession
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import ACTION, NARRATIVE, install_agent
from test_delegated_event_dice import delegated_provider
from test_event_narrative_consistency import END as EVENT_END, SAFE, claim_session
from test_lifecycle_cascade_instructions import instructions, make_session
from test_regenerate_stream_errors import runtime_state
from test_story_choice_settlement import choice_client, stored_state


END = "2030-01-01T09:01:00"


def baseline_session(origin="lifecycle"):
    session = make_session(origin)
    session.script.update(npcs=[], random_items=[])
    return session


def install_state_agent(session, streaming, end_time=END):
    provider = install_agent(session, instructions(2))
    if streaming:
        provider.generate_with_tools = AsyncMock(return_value={
            "content": "", "tool_calls": [{
                "id": "synthetic-baseline-finalize", "name": "finalize_turn",
                "arguments": {"end_time": end_time, "summary": "Recorded the practice room."},
            }],
        })
    else:
        provider.generate = AsyncMock(return_value=json.dumps({"end_time": end_time}))
    return provider


async def restore(session, sessions):
    game_routes._sessions.clear()
    restored = await game_routes._restore_session(session.save_id)
    sessions.append(restored)
    return restored


def assert_effects(state):
    assert state["player"]["test_generation_count"] == 1
    assert state["player"]["test_generation_chain"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["lifecycle", "story"])
@pytest.mark.parametrize("mode", ["all", "state", "stream-all", "stream-state"])
@pytest.mark.parametrize("reload_before", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_generation_baseline_survives_regeneration_retry_reload_and_branch(
    tmp_path, monkeypatch, origin, mode, reload_before, fail_save,
):
    session = baseline_session(origin)
    install_agent(session, instructions(2))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        initial = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json=ACTION))
        if reload_before:
            session = await restore(session, sessions)
        streaming = mode.startswith("stream-")
        stage = "state" if mode.endswith("state") else "all"
        endpoint = f"/api/game/{session.save_id}/regenerate{'/stream' if streaming else ''}?stage={stage}"
        for attempt in range(2):
            provider = (install_state_agent(session, streaming) if stage == "state"
                        else install_agent(session, instructions(2)))
            if fail_save and attempt == 0:
                before, stored = runtime_state(session), await stored_state(session)

                async def reject_save(*args, **kwargs):
                    raise RuntimeError("Synthetic baseline save interruption")

                with monkeypatch.context() as patch:
                    patch.setattr(game_routes, "_save_node" if mode == "stream-all" else "_update_node_in_db", reject_save)
                    failed = await asyncio.wait_for(client.post(endpoint), 20)
                if streaming:
                    assert any(event["type"] == "error" for event in _stream_events(failed.text))
                else:
                    assert failed.status_code == 500
                assert runtime_state(session) == before
                assert await stored_state(session) == stored
            response = await asyncio.wait_for(client.post(endpoint), 20)
            result = result_from(response, stream=streaming)
            assert result["node_id"] == initial["node_id"]
            assert result["narrative"] == NARRATIVE
            assert session.turn_number == 1 and session.current_state["game_time"] == END
            assert_effects(session.current_state)
            persisted = await stored_state(session)
            assert_effects(persisted)
            assert_effects(persisted["_node_context"]["_generation_baseline"]["state"])
            assert "_generation_baseline" not in response.text
            if stage == "all":
                assert provider.observations and all(value == instructions(2) for value in provider.observations)
            session = await restore(session, sessions)
            assert_effects(session.current_state)
            await session.branch_to_node(root)
            assert session.current_state["player"]["test_generation_count"] == 0
            await session.branch_to_node(initial["node_id"])
            assert_effects(session.current_state)
        node = session.world_tree.get_node(initial["node_id"])
        assert len(node["swipes"]) == 3
        public = await client.get(f"/api/game/{session.save_id}/tree/{initial['node_id']}")
        assert public.status_code == 200 and "_generation_baseline" not in public.text
        assert all(instruction not in public.text for instruction in instructions(2))


def dice_session():
    original = baseline_session()
    script = copy.deepcopy(original.script)
    script["random_items"] = [{
        "id": "test_prepared_roll", "trigger_type": "always", "dice": {"count": 1, "faces": 100},
        "ranges": [{"min": lower, "max": upper, "label": "Synthetic preparation",
                    "state_changes": [{"target": "player.test_prepared_reward", "op": "add", "value": reward}]}
                   for lower, upper, reward in [(1, 50, 1), (51, 100, 3)]],
    }]
    script["opening"] = {"choices": [{"text": "Inspect the practice room.", "result": {
        "description": "SYNTHETIC_OPENING_BASELINE",
        "state_changes": [{"target": "player.test_opening_reward", "op": "add", "value": 10}],
    }}]}
    session = GameSession(script, None, save_id="synthetic-generation-dice-save")
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["player"].update(test_prepared_reward=0, test_opening_reward=0)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("full_stream", [False, True])
@pytest.mark.parametrize("partial_stream", [False, True])
async def test_swipes_keep_distinct_prepared_dice_and_opening_effects(tmp_path, monkeypatch, full_stream, partial_stream):
    session = dice_session()
    install_agent(session, instructions(2))
    session.dice._rng.randint = Mock(side_effect=[10, 90])
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        session.world_tree.get_node(session.world_tree.active_node_id)["choices_presented"] = [
            {"id": "open_0", "text": "Inspect the practice room."},
        ]
        first = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "choice", "choice_id": "open_0", "text": "Inspect the practice room.",
        }))
        assert session.current_state["player"]["test_prepared_reward"] == 1
        endpoint = f"/api/game/{session.save_id}/regenerate{'/stream' if full_stream else ''}?stage=all"
        result_from(await client.post(endpoint), stream=full_stream)
        assert session.dice._rng.randint.call_count == 2
        assert session.current_state["player"]["test_prepared_reward"] == 3
        session = await restore(session, sessions)
        node = session.world_tree.get_node(first["node_id"])
        for index, reward in [(0, 1), (1, 3)]:
            session.swipe_to_index(index)
            assert node["_generation_baseline"]["state"]["player"]["test_prepared_reward"] == reward
            install_state_agent(session, partial_stream)
            session._roll_or_sustain = Mock(side_effect=AssertionError("State regeneration must reuse prepared dice"))
            result_from(await client.post(
                f"/api/game/{session.save_id}/regenerate{'/stream' if partial_stream else ''}?stage=state",
            ), stream=partial_stream)
            assert session.current_state["player"]["test_prepared_reward"] == reward
            assert session.current_state["player"]["test_opening_reward"] == 10
            assert session.current_state["opening_context"].count("SYNTHETIC_OPENING_BASELINE") == 1
            assert session.turn_number == 1 and session.current_state["game_time"] == END
            session._roll_or_sustain.assert_not_called()
        session = await restore(session, sessions)
        assert session.current_state["player"]["test_prepared_reward"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_legacy_node_requires_full_regeneration_before_state_replay(tmp_path, monkeypatch, streaming):
    session = baseline_session()
    provider = install_agent(session, instructions(2))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        initial = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json=ACTION))
        node = session.world_tree.get_node(initial["node_id"])
        node.pop("_generation_baseline")
        before, stored = runtime_state(session), await stored_state(session)
        calls = provider.generate_with_tools.await_count
        endpoint = f"/api/game/{session.save_id}/regenerate{'/stream' if streaming else ''}"
        failed = await client.post(endpoint + "?stage=state")
        assert failed.status_code == 400 and "生成前检查点" in failed.json()["detail"]
        assert runtime_state(session) == before and await stored_state(session) == stored
        assert provider.generate_with_tools.await_count == calls
        node = session.world_tree.get_node(initial["node_id"])
        result_from(await client.post(endpoint + "?stage=all"), stream=streaming)
        assert node["_generation_baseline"]["version"] == 1
        session.swipe_to_index(0)
        assert "_generation_baseline" not in node
        session.swipe_to_index(1)
        install_state_agent(session, streaming)
        result_from(await client.post(endpoint + "?stage=state"), stream=streaming)
        session = await restore(session, sessions)
        assert_effects(session.current_state)


@pytest.mark.asyncio
@pytest.mark.parametrize("full_regeneration", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_late_event_dice_survive_reload_and_repeated_state_regeneration(
    tmp_path, monkeypatch, full_regeneration, streaming,
):
    session = claim_session("unstaged_dice")
    session.ai_provider = delegated_provider(session, safe=True)
    roll = session.dice.roll_and_resolve = Mock(wraps=session.dice.roll_and_resolve)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json=ACTION))
        if full_regeneration:
            result_from(await client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), stream=False)
        assert roll.call_count == 1 + int(full_regeneration)
        for attempt in range(2):
            session = await restore(session, sessions)
            node = session.world_tree.get_node(session.world_tree.active_node_id)
            baseline = node["_generation_baseline"]
            assert baseline["state"]["script_variables"]["test_ready"] == 2
            assert "test_cancel_roll" in baseline["dice"]["event_results"]
            assert node["ai_response"] == SAFE
            session._roll_or_sustain = Mock(side_effect=AssertionError("Existing event dice must be replayed"))
            provider = install_state_agent(session, streaming, EVENT_END)
            arguments = {"end_time": EVENT_END, "state_changes": [
                {"target": "script_variables.test_ready", "op": "set", "value": 3},
            ]}
            provider.generate = AsyncMock(return_value=json.dumps(arguments))
            provider.generate_with_tools = AsyncMock(side_effect=[
                {"content": "", "tool_calls": [{"id": "synthetic-late-update", "name": "update_state",
                                               "arguments": arguments}]},
                {"content": "", "tool_calls": [{"id": "synthetic-late-finish", "name": "finalize_turn",
                                               "arguments": {"summary": "Recorded the practice clock."}}]},
            ])
            result_from(await client.post(
                f"/api/game/{session.save_id}/regenerate{'/stream' if streaming else ''}?stage=state",
            ), stream=streaming)
            persisted = await stored_state(session)
            assert persisted["game_time"] == EVENT_END
            assert persisted["script_variables"]["test_ready"] == 0
            assert persisted["fired_one_time_events"] == ["test_cancel"]
            assert session.turn_number == 1
            session._roll_or_sustain.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["all", "state"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_generation_baseline_includes_route_skill_growth(tmp_path, monkeypatch, stage, streaming):
    session = baseline_session()
    install_agent(session, instructions(2))
    session._run_route_stage = AsyncMock(return_value={
        "scope": "minor", "skill_check": {"needed": True, "attr": "test_attention", "difficulty": "easy"},
    })
    session._resolve_check = Mock(side_effect=lambda *args: {
        "outcome": "success", "related_attribute": "test_attention", "roll": 10, "target": 50,
    })
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json=ACTION))
        assert session.current_state["skill_growth"]["test_attention"]["xp"] == 10
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        assert node["_generation_baseline"]["state"]["skill_growth"]["test_attention"]["xp"] == 10
        if stage == "state":
            install_state_agent(session, streaming)
        result_from(await client.post(
            f"/api/game/{session.save_id}/regenerate{'/stream' if streaming else ''}?stage={stage}",
        ), stream=streaming)
        assert session._resolve_check.call_count == (2 if stage == "all" else 1)
        session = await restore(session, sessions)
        assert session.current_state["skill_growth"]["test_attention"]["xp"] == 10
        assert session.current_state["_check_result_history"] == ["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_route", [False, True])
async def test_full_regeneration_preparation_restores_runtime_models(tmp_path, monkeypatch, fail_route):
    session = dice_session()
    install_agent(session, instructions(2))
    session.current_state["_player_model"] = copy.deepcopy(session.player_model.snapshot())
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.get_node(session.world_tree.active_node_id)
        root["choices_presented"] = [{"id": "open_0", "text": "Inspect the practice room.", "risk": "safe"}]
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "choice", "choice_id": "open_0", "text": "Inspect the practice room.",
        }))
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        before = runtime_state(session)
        original_model = session.player_model
        original_history = copy.deepcopy(original_model.snapshot())
        observed = []

        async def route(action, ctx):
            observed.append(copy.deepcopy(session.player_model.choice_history))
            if fail_route:
                raise RuntimeError("Synthetic route interruption")
            return {}

        monkeypatch.setattr(session, "_enrich_and_route", route)
        if fail_route:
            with pytest.raises(RuntimeError, match="Synthetic route interruption"):
                await session._prepare_full_regeneration(node, root["state_snapshot"])
        else:
            await session._prepare_full_regeneration(node, root["state_snapshot"])
        assert observed == [["safe"]]
        assert session.player_model is original_model
        assert session.player_model.snapshot() == original_history
        assert runtime_state(session) == before
