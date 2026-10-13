import asyncio
import copy

import pytest

from api import game_routes
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_choice_instructions import install_agent
from test_generation_baseline import baseline_session, restore
from test_lifecycle_cascade_instructions import instructions
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_story_choice_settlement import choice_client, stored_state


MODEL_NAMES = ("player_model", "npc_autonomy", "narrative_graph", "director_notes")
CHOICE = {"id": "test_inspect", "text": "Inspect the practice room.", "risk": "safe"}
ACTION = {"type": "choice", "choice_id": CHOICE["id"], "text": CHOICE["text"]}


def model_snapshots(session):
    return {"_" + name: copy.deepcopy(getattr(session, name).snapshot()) for name in MODEL_NAMES}


def add_model_history(session, label):
    session.player_model.record_choice({"risk": "moderate"})
    session.narrative_graph.add_entity("test_" + label, "event", "Synthetic " + label)
    session.narrative_graph.add_relation("test_" + label, "test_room", "witnessed")
    session.director_notes.add_note(0, "plot", "Synthetic " + label)
    session.npc_autonomy.npc_states["test_" + label] = {
        "location": "test_room", "activity": "Synthetic inspection", "mood": "neutral",
    }


@pytest.mark.parametrize("snapshot_kind", ["missing", "empty", "populated"])
def test_parent_model_restore_resets_missing_data_and_owns_snapshot_contents(snapshot_kind):
    session = baseline_session()
    if snapshot_kind == "populated":
        add_model_history(session, "parent")
    expected = model_snapshots(session)
    source = (expected if snapshot_kind == "populated" else
              {"_" + name: {} for name in MODEL_NAMES} if snapshot_kind == "empty" else {})
    source = copy.deepcopy(source)
    original_source = copy.deepcopy(source)
    session.current_state.update(source)
    add_model_history(session, "discarded")

    session._restore_session_models_from_state()

    assert model_snapshots(session) == expected
    add_model_history(session, "later")
    assert source == original_source


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_kind", ["missing", "empty", "populated"])
@pytest.mark.parametrize("reload_before", [False, True])
@pytest.mark.parametrize("failure", [None, "prepare", "save"])
async def test_stream_full_regeneration_replays_parent_models_without_future_history(
    tmp_path, monkeypatch, snapshot_kind, reload_before, failure,
):
    session = baseline_session()
    if snapshot_kind == "populated":
        add_model_history(session, "parent")
    expected_parent = model_snapshots(session)
    if snapshot_kind == "populated":
        session.current_state.update(copy.deepcopy(expected_parent))
    elif snapshot_kind == "empty":
        session.current_state.update({"_" + name: {} for name in MODEL_NAMES})
    install_agent(session, instructions(2))

    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        root = session.world_tree.get_node(root_id)
        root["choices_presented"] = [copy.deepcopy(CHOICE)]
        await game_routes._update_node_in_db(session, {
            "node_id": root_id, "narrative": "Synthetic checkpoint.", "choices": root["choices_presented"],
        })
        parent_state = copy.deepcopy(root["state_snapshot"])
        initial = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json=ACTION))
        add_model_history(session, "discarded")
        session.current_state.update(model_snapshots(session))
        session.world_tree.get_node(initial["node_id"])["state_snapshot"] = copy.deepcopy(session.current_state)
        await game_routes._update_node_in_db(session, initial)
        if reload_before:
            session = await restore(session, sessions)

        endpoint = f"/api/game/{session.save_id}/regenerate/stream?stage=all"
        for attempt in range(2):
            install_agent(session, instructions(2))
            prepared_models = []
            prepare = session._prepare_turn

            def observe_prepare(action, **kwargs):
                prepared_models.append(model_snapshots(session))
                context = prepare(action, **kwargs)
                return context

            with monkeypatch.context() as patch:
                patch.setattr(session, "_prepare_turn", observe_prepare)
                if failure and attempt == 0:
                    before = runtime_state(session)
                    models_before = model_snapshots(session)
                    stored_before = await database_state(session)

                    def reject_prepare(action, **kwargs):
                        observe_prepare(action, **kwargs)
                        raise RuntimeError("Synthetic preparation interruption")

                    async def reject_save(*args, **kwargs):
                        raise RuntimeError("Synthetic save interruption")

                    with monkeypatch.context() as failed_patch:
                        if failure == "prepare":
                            failed_patch.setattr(session, "_prepare_turn", reject_prepare)
                        else:
                            failed_patch.setattr(game_routes, "_save_node", reject_save)
                        failed = await asyncio.wait_for(client.post(endpoint), 20)
                    assert any(event["type"] == "error" for event in _stream_events(failed.text))
                    assert runtime_state(session) == before
                    assert model_snapshots(session) == models_before
                    assert await database_state(session) == stored_before
                    assert_controls_closed(session)

                response = await asyncio.wait_for(client.post(endpoint), 20)
                regenerated = result_from(response)

            assert prepared_models and all(models == expected_parent for models in prepared_models)
            assert regenerated["node_id"] == initial["node_id"]
            assert regenerated["total_swipes"] == attempt + 2
            assert session.turn_number == 1
            assert session.current_state["game_time"] == "2030-01-01T09:01:00"
            expected_history = expected_parent["_player_model"]["choice_history"] + ["safe"]
            assert session.player_model.choice_history == expected_history
            persisted = await stored_state(session)
            assert persisted["_player_model"]["choice_history"] == expected_history
            assert "test_discarded" not in persisted["_narrative_graph"]["nodes"]
            assert "test_discarded" not in persisted["_npc_autonomy"]["npc_states"]
            assert all(note["content"] != "Synthetic discarded" for note in persisted["_director_notes"]["notes"])
            assert_controls_closed(session)
            session = await restore(session, sessions)
            assert session.player_model.choice_history == expected_history
            await session.branch_to_node(root_id)
            assert model_snapshots(session) == expected_parent
            await session.branch_to_node(initial["node_id"])
            assert session.player_model.choice_history == expected_history
            assert session.world_tree.get_node(root_id)["state_snapshot"] == parent_state
