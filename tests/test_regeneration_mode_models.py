import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import NARRATIVE, install_agent
from test_generation_baseline import END, baseline_session, install_state_agent, restore
from test_lifecycle_cascade_instructions import instructions
from test_regenerate_stream_errors import database_state, runtime_state
from test_regeneration_parent_runtime import CHOICE, add_model_history as add_history, model_snapshots
from test_story_choice_settlement import stored_state
from test_swipe_runtime_models import variant_client


MODES = ["all", "stream-all", "workflow-all", "state", "stream-state", "choices", "stream-choices"]
CHOICES = [{"id": f"test_shelf_{index}", "text": f"Inspect practice shelf {index}."} for index in range(4)]


def add_model_history(session, label):
    add_history(session, label)
    session.director_notes.notes[-1]["created_at"] = END


def endpoint(session, mode):
    stage = mode.rsplit("-", 1)[-1]
    return f"/api/game/{session.save_id}/regenerate{'/stream' if mode.startswith('stream-') else ''}?stage={stage}"


def install_mode_agent(session, mode, marker, observations, patch):
    stage = mode.rsplit("-", 1)[-1]
    streaming = mode.startswith("stream-")
    provider = install_state_agent(session, streaming) if stage == "state" else install_agent(session, instructions(2))

    def observe():
        observations.append(model_snapshots(session))

    if mode == "workflow-all":
        patch.setattr(session, "_agentic_unified_enabled", lambda: False)

        async def pipeline(*args, **kwargs):
            observe()
            add_model_history(session, marker)
            yield {"type": "pipeline_result", "narrative": NARRATIVE,
                   "parsed": {"narrative": NARRATIVE, "choices": CHOICES, "end_time": END}}

        patch.setattr(session, "_execute_pipeline", pipeline)
    elif stage == "all" or streaming:
        attribute = "_execute_unified_agent" if stage == "all" else "_regen_partial_agent"
        execute = getattr(session, attribute)
        if stage == "choices":
            provider.generate_with_tools = AsyncMock(return_value={
                "content": "", "tool_calls": [{"id": "synthetic-mode-choices", "name": "finalize_turn",
                    "arguments": {"summary": "Choose a practice shelf.", "choices": CHOICES}}],
            })

        async def generate(*args, **kwargs):
            observe()
            async for item in execute(*args, **kwargs):
                if item["type"] in {"pipeline_result", "partial_regen_done"}:
                    add_model_history(session, marker)
                yield item

        patch.setattr(session, attribute, generate)
    elif stage == "state":
        settle = session._settle_regen_state

        def settle_state(*args, **kwargs):
            observe()
            add_model_history(session, marker)
            return settle(*args, **kwargs)

        patch.setattr(session, "_settle_regen_state", settle_state)
    else:
        async def generate_choices(*args, **kwargs):
            observe()
            add_model_history(session, marker)
            return json.dumps({"choices": CHOICES})

        provider.generate = AsyncMock(side_effect=generate_choices)


async def expected_models(session, mode, marker):
    expected = baseline_session()
    if mode.endswith("all"):
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        expected.current_state = await session._load_snapshot_from_db(node["parent_id"])
        expected._restore_session_models_from_state()
        expected.player_model.record_choice(CHOICE)
    else:
        expected.current_state.update(model_snapshots(session))
        expected._restore_session_models_from_state()
    before = model_snapshots(expected)
    add_model_history(expected, marker)
    return before, model_snapshots(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("selected_index", [0, 1])
@pytest.mark.parametrize("reload_before", [False, True])
async def test_regeneration_modes_own_models_through_swipe_continuation_and_reload(
    tmp_path, monkeypatch, mode, selected_index, reload_before,
):
    async with variant_client(tmp_path, monkeypatch) as (session, client, sessions, variants):
        result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={selected_index}"), stream=False)
        if reload_before:
            session = await restore(session, sessions)
        add_model_history(session, "late_selected")
        outgoing = model_snapshots(session)
        node_id = session.world_tree.active_node_id
        node = session.world_tree.get_node(node_id)
        narrative, choices = node["ai_response"], copy.deepcopy(node["choices_presented"])
        expected_before, expected_after = await expected_models(session, mode, "replacement")
        observed = []
        with monkeypatch.context() as patch:
            install_mode_agent(session, mode, "replacement", observed, patch)
            result = result_from(await asyncio.wait_for(client.post(endpoint(session, mode)), 20),
                                 stream=mode.startswith("stream-"))
        assert observed == [expected_before]
        assert model_snapshots(session) == expected_after
        assert result["node_id"] == node_id and result["swipe_index"] == 2 and result["total_swipes"] == 3
        assert session.turn_number == 1 and session.current_state["game_time"] == END
        if not mode.endswith("all"):
            assert result["narrative"] == narrative
        if mode.endswith("state"):
            assert result["choices"] == choices
        stored = await stored_state(session)
        assert {key: stored[key] for key in expected_after} == expected_after
        old_state = stored["_node_context"]["swipes"][selected_index]["state_snapshot"]
        assert {key: old_state[key] for key in outgoing} == outgoing
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected_after
        for index, expected in ((selected_index, outgoing), (2, expected_after)):
            result_from(await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}"), stream=False)
            assert model_snapshots(session) == expected
            session = await restore(session, sessions)
            assert model_snapshots(session) == expected
        checkpoint = await session._load_snapshot_from_db(node_id)
        install_agent(session, instructions(2))
        prepare = session._prepare_turn
        observed = []

        def observe_prepare(action, **kwargs):
            observed.append(model_snapshots(session))
            return prepare(action, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(session, "_prepare_turn", observe_prepare)
            continued = result_from(await asyncio.wait_for(client.post(
                f"/api/game/{session.save_id}/action/stream",
                json={"type": "freeform", "text": "Inspect another practice shelf."},
            ), 20))
        assert observed == [expected_after]
        assert model_snapshots(session) == expected_after
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == node_id
        assert session.turn_number == 2 and session.current_state["game_time"] == "2030-01-01T09:02:00"
        assert await session._load_snapshot_from_db(node_id) == checkpoint
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected_after
        saved = copy.deepcopy(session.current_state)
        add_model_history(session, "later_mutation")
        assert session.current_state == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("failure", ["save", "cancel"])
async def test_regeneration_mode_model_changes_roll_back_before_save_and_retry(
    tmp_path, monkeypatch, mode, failure,
):
    async with variant_client(tmp_path, monkeypatch) as (session, client, sessions, variants):
        add_model_history(session, "late_selected")
        before, models_before, stored_before = runtime_state(session), model_snapshots(session), await database_state(session)
        saving = asyncio.Event()

        async def reject_save(*args, **kwargs):
            if failure == "save":
                raise RuntimeError("Synthetic mode persistence interruption")
            saving.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            install_mode_agent(session, mode, "failed_attempt", [], patch)
            patch.setattr(game_routes, "_save_node" if mode == "stream-all" else "_update_node_in_db", reject_save)
            if failure == "cancel":
                request = asyncio.create_task(client.post(endpoint(session, mode)))
                try:
                    await asyncio.wait_for(saving.wait(), 5)
                finally:
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
            else:
                response = await asyncio.wait_for(client.post(endpoint(session, mode)), 20)
                if mode.startswith("stream-"):
                    assert any(event["type"] == "error" for event in _stream_events(response.text))
                else:
                    assert response.status_code == 500
        assert runtime_state(session) == before
        assert model_snapshots(session) == models_before
        assert await database_state(session) == stored_before
        assert not game_routes._get_session_lock(session.save_id).locked()
        expected_before, expected_after = await expected_models(session, mode, "retry")
        observed = []
        with monkeypatch.context() as patch:
            install_mode_agent(session, mode, "retry", observed, patch)
            result_from(await asyncio.wait_for(client.post(endpoint(session, mode)), 20),
                        stream=mode.startswith("stream-"))
        assert observed == [expected_before]
        assert model_snapshots(session) == expected_after
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected_after
