import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import ACTION, NARRATIVE, install_agent
from test_lifecycle_cascade_instructions import instructions, make_session
from test_story_choice_settlement import choice_client, stored_state


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["lifecycle", "story"])
@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.parametrize("regenerate", [False, True])
async def test_generation_cascades_reach_harness_and_survive_persistence(
    tmp_path, monkeypatch, origin, fail_save, regenerate,
):
    session = make_session(origin)
    expected = instructions(2)
    provider = install_agent(session, expected)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        root = session.world_tree.active_node_id
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {**ACTION, "action_id": "synthetic-generation-dispatch", "expected_node_id": root}
        before = copy.deepcopy(session.current_state)
        stored_before = await stored_state(session)
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert current.current_state["player"]["test_generation_chain"] == 2
                raise RuntimeError("Synthetic generation save interruption")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failure = await asyncio.wait_for(client.post(endpoint, json=action), 20)
            error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
            assert error["discard_partial"] is True
            assert session.current_state == before and await stored_state(session) == stored_before
            assert session.world_tree.active_node_id == root
        response = await asyncio.wait_for(client.post(endpoint, json=action), 20)
        final = result_from(response)
        assert final["narrative"] == NARRATIVE
        assert session.current_state["player"]["test_generation_count"] == 1
        assert session.current_state["player"]["test_generation_chain"] == 2
        assert provider.observations == [expected] * (2 if fail_save else 1)
        assert all(instruction not in response.text for instruction in expected)
        settled = await stored_state(session)
        assert settled["_node_context"]["turn_instructions"] == expected
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        assert await stored_state(session) == settled
        game_routes._sessions.clear()
        session = await game_routes._restore_session(session.save_id)
        sessions.append(session)
        provider = install_agent(session, expected)
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        assert session.world_tree.get_node(final["node_id"])["turn_instructions"] == expected
        if regenerate:
            regen_endpoint = f"/api/game/{session.save_id}/regenerate/stream"
            if fail_save:
                before_regen = copy.deepcopy(session.current_state)

                async def reject_regen_save(current, result, action_id=None):
                    raise RuntimeError("Synthetic generation replay save interruption")

                with monkeypatch.context() as patch:
                    patch.setattr(game_routes, "_save_node", reject_regen_save)
                    failure = await asyncio.wait_for(client.post(regen_endpoint), 20)
                assert any(event["type"] == "error" for event in _stream_events(failure.text))
                assert session.current_state == before_regen and await stored_state(session) == settled
            regenerated = await asyncio.wait_for(client.post(regen_endpoint), 20)
            result_from(regenerated)
            assert all(observation == expected for observation in provider.observations)
            assert provider.observations
            assert all(instruction not in regenerated.text for instruction in expected)
            assert session.current_state["player"]["test_generation_count"] == 1
            assert session.current_state["player"]["test_generation_chain"] == 2
        persisted = await stored_state(session)
        persisted.pop("_node_context", None)
        assert persisted == session.current_state
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["player"]["test_generation_chain"] == 2
        await restored.branch_to_node(root)
        assert restored.current_state["player"]["test_generation_count"] == 0
        assert restored.current_state["player"]["test_generation_chain"] == 0
        await restored.branch_to_node(final["node_id"])
        assert restored.current_state["player"]["test_generation_count"] == 1
        assert restored.current_state["player"]["test_generation_chain"] == 2
        next_provider = install_agent(restored, expected)
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=next_provider))
        result_from(await asyncio.wait_for(client.post(endpoint, json={
            **ACTION, "action_id": "synthetic-generation-next", "expected_node_id": final["node_id"],
        }), 20))
        assert next_provider.observations == [[]]
        assert restored.current_state["player"]["test_generation_count"] == 1
        assert restored.current_state["player"]["test_generation_chain"] == 2
