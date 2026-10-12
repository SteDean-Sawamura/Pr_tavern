import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import ACTION, NARRATIVE, install_agent
from test_event_cascade_dispatch import cascade_session, counts, instructions
from test_scheduled_event_instructions import END, install_instruction_agent
from test_story_choice_settlement import choice_client, stored_state


@pytest.mark.asyncio
@pytest.mark.parametrize("category,count,depth", [("one_time_events", 1, 3), ("cyclic_events", 3, 2)])
@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.parametrize("regeneration", [None, "all", "all-reload", "state", "stream", "stream-state"])
async def test_cascades_survive_retry_replay_reload_regeneration_and_branches(
    tmp_path, monkeypatch, category, count, depth, fail_save, regeneration,
):
    session = cascade_session(category, depth)
    expected = instructions(depth)
    root_provider = install_instruction_agent(session, "staged")
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=root_provider))
        root = session.world_tree.active_node_id
        endpoint = f"/api/game/{session.save_id}/action/stream"
        queued_result = result_from(await asyncio.wait_for(client.post(endpoint, json={
            **ACTION, "action_id": "synthetic-cascade-root", "expected_node_id": root,
        }), 20))
        queued_node = queued_result["node_id"]
        assert session.current_state["game_time"] == END
        assert session.current_state["script_variables"]["test_ready"] == count
        assert counts(session, depth) == [0] * depth
        assert session.current_state["_pending_fire_events"] == ["test_signal_0"] * count
        game_routes._sessions.clear()
        session = await game_routes._restore_session(session.save_id)
        sessions.append(session)
        assert counts(session, depth) == [0] * depth
        assert session.current_state["_pending_fire_events"] == ["test_signal_0"] * count
        provider = install_agent(session, expected)
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
        before = copy.deepcopy(session.current_state)
        stored_before = await stored_state(session)
        action = {**ACTION, "action_id": "synthetic-cascade-dispatch", "expected_node_id": queued_node}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert counts(current, depth) == [count] * depth
                assert not current.current_state.get("_pending_fire_events")
                raise RuntimeError("Synthetic cascade save interruption")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failure = await asyncio.wait_for(client.post(endpoint, json=action), 20)
            error, = [event for event in _stream_events(failure.text) if event["type"] == "error"]
            assert error["discard_partial"] is True
            assert session.current_state == before and await stored_state(session) == stored_before
            assert session.world_tree.active_node_id == queued_node
        response = await asyncio.wait_for(client.post(endpoint, json=action), 20)
        final = result_from(response)
        assert final["narrative"] == NARRATIVE
        assert provider.observations == [expected] * (2 if fail_save else 1)
        assert all(instruction not in response.text for instruction in expected)
        assert counts(session, depth) == [count] * depth
        assert not session.current_state.get("_pending_fire_events")
        settled = await stored_state(session)
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        assert await stored_state(session) == settled
        if regeneration:
            if regeneration == "all-reload":
                game_routes._sessions.clear()
                session = await game_routes._restore_session(session.save_id)
                sessions.append(session)
                provider = install_agent(session, expected)
                monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
            streaming = regeneration.startswith("stream")
            stage = "state" if regeneration in ("state", "stream-state") else "all"
            suffix = "/stream" if streaming else ""
            regen_endpoint = f"/api/game/{session.save_id}/regenerate{suffix}?stage={stage}"
            if regeneration == "stream-state":
                end_time = session.current_state["game_time"]

                async def settle_partial(messages, **kwargs):
                    assert counts(session, depth) == [count] * depth
                    return {"content": "", "tool_calls": [{
                        "id": "synthetic-cascade-partial", "name": "finalize_turn",
                        "arguments": {"end_time": end_time, "summary": "Recorded the practice room."},
                    }]}

                provider.generate_with_tools = AsyncMock(side_effect=settle_partial)
            if fail_save:
                before_regen = copy.deepcopy(session.current_state)

                async def reject_regen_save(current, result, action_id=None):
                    raise RuntimeError("Synthetic cascade regeneration save interruption")

                with monkeypatch.context() as patch:
                    save_method = "_save_node" if regeneration == "stream" else "_update_node_in_db"
                    patch.setattr(game_routes, save_method, reject_regen_save)
                    failure = await asyncio.wait_for(client.post(regen_endpoint), 20)
                if streaming:
                    assert any(event["type"] == "error" for event in _stream_events(failure.text))
                else:
                    assert failure.status_code == 500
                assert session.current_state == before_regen and await stored_state(session) == settled
            regenerated = await asyncio.wait_for(client.post(regen_endpoint), 20)
            result = result_from(regenerated, stream=streaming)
            assert "error" not in result, result
            assert counts(session, depth) == [count] * depth
            assert not session.current_state.get("_pending_fire_events")
            assert all(instruction not in regenerated.text for instruction in expected)
            if stage == "all":
                assert provider.observations[-1] == expected
        persisted = await stored_state(session)
        persisted.pop("_node_context", None)
        assert persisted == session.current_state
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert counts(restored, depth) == [count] * depth
        assert not restored.current_state.get("_pending_fire_events")
        await restored.branch_to_node(root)
        assert counts(restored, depth) == [0] * depth
        assert not restored.current_state.get("_pending_fire_events")
        await restored.branch_to_node(queued_node)
        assert counts(restored, depth) == [0] * depth
        assert restored.current_state["_pending_fire_events"] == ["test_signal_0"] * count
        await restored.branch_to_node(final["node_id"])
        assert counts(restored, depth) == [count] * depth
        assert not restored.current_state.get("_pending_fire_events")
        next_provider = install_agent(restored, expected)
        monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=next_provider))
        result_from(await asyncio.wait_for(client.post(endpoint, json={
            **ACTION, "action_id": "synthetic-cascade-next", "expected_node_id": final["node_id"],
        }), 20))
        assert next_provider.observations == [[]]
        assert counts(restored, depth) == [count] * depth
        assert not restored.current_state.get("_pending_fire_events")
