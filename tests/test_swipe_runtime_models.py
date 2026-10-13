import asyncio
from contextlib import asynccontextmanager
import copy

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_choice_instructions import install_agent
from test_generation_baseline import baseline_session, restore
from test_lifecycle_cascade_instructions import instructions
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_regeneration_parent_runtime import ACTION, CHOICE, MODEL_NAMES, add_model_history, model_snapshots
from test_story_choice_settlement import choice_client, stored_state


@asynccontextmanager
async def variant_client(tmp_path, monkeypatch):
    session = baseline_session()
    add_model_history(session, "parent")
    session.current_state.update(model_snapshots(session))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        root = session.world_tree.get_node(root_id)
        root["choices_presented"] = [copy.deepcopy(CHOICE)]
        await game_routes._update_node_in_db(session, {
            "node_id": root_id, "narrative": "Synthetic checkpoint.", "choices": root["choices_presented"],
        })
        expected = []
        for label in ("original", "alternative"):
            install_agent(session, instructions(2))
            execute = session._execute_unified_agent

            async def generate_variant(*args, **kwargs):
                async for item in execute(*args, **kwargs):
                    if item["type"] == "pipeline_result":
                        add_model_history(session, label)
                        if label == "alternative":
                            session.player_model.record_choice({"risk": "risky"})
                    yield item

            with monkeypatch.context() as patch:
                patch.setattr(session, "_execute_unified_agent", generate_variant)
                endpoint = (f"/api/game/{session.save_id}/action/stream" if label == "original"
                            else f"/api/game/{session.save_id}/regenerate/stream?stage=all")
                result_from(await asyncio.wait_for(client.post(endpoint, json=ACTION), 20))
            await session._drain_background_tasks()
            expected.append(model_snapshots(session))
        assert expected[0] != expected[1]
        yield session, client, sessions, expected


def swipe_endpoint(session, method, index):
    suffix = f"jump?index={index}" if method == "jump" else "left" if index == 0 else "right"
    return f"/api/game/{session.save_id}/swipe/{suffix}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["jump", "direction"])
@pytest.mark.parametrize("reload_before", [False, True])
@pytest.mark.parametrize("snapshot_kind", ["missing", "empty", "populated"])
@pytest.mark.parametrize("failure", [None, "hydrate", "save"])
async def test_swipe_models_survive_switch_retry_continuation_and_reload(
    tmp_path, monkeypatch, method, reload_before, snapshot_kind, failure,
):
    async with variant_client(tmp_path, monkeypatch) as (session, client, sessions, expected):
        node_id = session.world_tree.active_node_id
        node = session.world_tree.get_node(node_id)
        if snapshot_kind != "populated":
            state = node["swipes"][0]["state_snapshot"]
            for name in MODEL_NAMES:
                if snapshot_kind == "missing":
                    state.pop("_" + name, None)
                else:
                    state["_" + name] = {}
            expected[0] = model_snapshots(baseline_session())
            await game_routes._update_node_in_db(session, {"node_id": node_id})
        if reload_before:
            session = await restore(session, sessions)
        parent = await session._load_snapshot_from_db(node["parent_id"])

        if failure:
            before = runtime_state(session)
            models_before = model_snapshots(session)
            stored_before = await database_state(session)
            hydrate = session._hydrate_branch_runtime

            def reject_hydrate(state):
                hydrate(state)
                raise RuntimeError("Synthetic swipe hydration interruption")

            async def reject_save(*args, **kwargs):
                raise RuntimeError("Synthetic swipe persistence interruption")

            with monkeypatch.context() as patch:
                if failure == "hydrate":
                    patch.setattr(session, "_hydrate_branch_runtime", reject_hydrate)
                else:
                    patch.setattr(game_routes, "_update_node_in_db", reject_save)
                failed = await client.post(swipe_endpoint(session, method, 0))
            assert failed.status_code == 500
            assert runtime_state(session) == before
            assert model_snapshots(session) == models_before
            assert await database_state(session) == stored_before
            assert not game_routes._get_session_lock(session.save_id).locked()
            assert not getattr(session, "_active_agent_stream", False)

        for index in (0, 1, 0, 1, 0):
            response = await client.post(swipe_endpoint(session, method, index))
            result = result_from(response, stream=False)
            assert result["swipe_index"] == index and result["total_swipes"] == 2
            assert result["node_id"] == node_id
            assert session.turn_number == 1 and session.current_state["game_time"] == "2030-01-01T09:01:00"
            assert model_snapshots(session) == expected[index]
            persisted = await stored_state(session)
            assert {key: persisted.get(key) or {} for key in expected[index]} == {
                key: session.current_state.get(key) or {} for key in expected[index]
            }
            assert "_npc_autonomy" not in response.text and "_generation_baseline" not in response.text
            if reload_before:
                session = await restore(session, sessions)
                assert model_snapshots(session) == expected[index]

        before_continuation = copy.deepcopy(session.world_tree.get_node(node_id)["state_snapshot"])
        install_agent(session, instructions(2))
        prepare = session._prepare_turn
        observations = []

        def observe_prepare(action, **kwargs):
            observations.append(model_snapshots(session))
            return prepare(action, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(session, "_prepare_turn", observe_prepare)
            continued = result_from(await asyncio.wait_for(client.post(
                f"/api/game/{session.save_id}/action/stream",
                json={"type": "freeform", "text": "Inspect another practice shelf."},
            ), 20))
        assert observations == [expected[0]]
        assert session.turn_number == 2 and session.current_state["game_time"] == "2030-01-01T09:02:00"
        assert model_snapshots(session) == expected[0]
        assert session.world_tree.get_node(continued["node_id"])["parent_id"] == node_id
        assert await session._load_snapshot_from_db(node_id) == before_continuation
        assert_controls_closed(session)
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected[0]
        assert session.world_tree.get_node(node["parent_id"])["state_snapshot"] == parent


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["jump", "direction"])
async def test_swipe_saves_outgoing_live_models_without_aliasing_other_versions(tmp_path, monkeypatch, method):
    async with variant_client(tmp_path, monkeypatch) as (session, client, sessions, expected):
        result_from(await client.post(swipe_endpoint(session, method, 0)), stream=False)
        session = await restore(session, sessions)
        add_model_history(session, "late_original")
        updated = model_snapshots(session)
        assert updated != expected[0]

        result_from(await client.post(swipe_endpoint(session, method, 1)), stream=False)
        assert model_snapshots(session) == expected[1]
        persisted = await stored_state(session)
        original = persisted["_node_context"]["swipes"][0]["state_snapshot"]
        assert {key: original[key] for key in updated} == updated
        session = await restore(session, sessions)

        result_from(await client.post(swipe_endpoint(session, method, 0)), stream=False)
        assert model_snapshots(session) == updated
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        stored_variants = copy.deepcopy([swipe["state_snapshot"] for swipe in node["swipes"]])
        add_model_history(session, "later_mutation")
        assert [swipe["state_snapshot"] for swipe in node["swipes"]] == stored_variants


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["jump", "direction"])
async def test_swipe_cancellation_before_save_restores_runtime_and_allows_retry(tmp_path, monkeypatch, method):
    async with variant_client(tmp_path, monkeypatch) as (session, client, sessions, expected):
        before = runtime_state(session)
        models_before = model_snapshots(session)
        stored_before = await database_state(session)
        saving = asyncio.Event()

        async def delay_save(*args, **kwargs):
            saving.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "_update_node_in_db", delay_save)
            request = asyncio.create_task(client.post(swipe_endpoint(session, method, 0)))
            try:
                await asyncio.wait_for(saving.wait(), 5)
            finally:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
        assert runtime_state(session) == before
        assert model_snapshots(session) == models_before
        assert await database_state(session) == stored_before
        assert not game_routes._get_session_lock(session.save_id).locked()
        result_from(await client.post(swipe_endpoint(session, method, 0)), stream=False)
        assert model_snapshots(session) == expected[0]
        session = await restore(session, sessions)
        assert model_snapshots(session) == expected[0]
