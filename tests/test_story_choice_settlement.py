import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import copy
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from api import config_routes, game_routes
from db import database
from engine.event_engine import GameEvent
from engine.game_session import GameSession
from test_declared_choice_preview import make_session, preview


def choice_session(threshold=7, dynamic=False):
    original = make_session(threshold)
    script = copy.deepcopy(original.script)
    nodes = script["story_tree"]["trees"][0]["nodes"]
    signalled = {"id": "test_signalled", "type": "quest", "activate_events": ["test_signal"]}
    if not dynamic:
        nodes.append(signalled)
    session = GameSession(script, None)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state.pop("story_tree_state")
    if dynamic:
        session._apply_director_results({"tree": {"new_nodes": [signalled]}})
    session.story_tree_engine.evaluate(session.current_state, 0, session._evaluate_condition)
    session.event_engine.get_visible_trees(session.current_state)
    return session


async def persist_node(session, parent_id=None):
    state = copy.deepcopy(session.current_state)
    node_id = session.world_tree.add_node(parent_id, state["game_time"], session.turn_number,
                                          state_snapshot=state)
    await game_routes._save_node(session, {"node_id": node_id, "state": state, "narrative": "Synthetic checkpoint."})
    session.world_tree.get_node(node_id)["_state_hash"] = session.world_tree.state_hash(state)
    return node_id


async def stored_state(session):
    async with database.get_db() as db:
        cursor = await db.execute("SELECT state_snapshot FROM tree_nodes WHERE id = ? AND save_id = ?",
                                  (session.world_tree.active_node_id, session.save_id))
        return json.loads((await cursor.fetchone())["state_snapshot"])


@asynccontextmanager
async def choice_client(session, tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-choice.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (session.script["script_id"], session.script["name"], json.dumps(session.script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, session.script["script_id"]))
            await db.commit()
        await persist_node(session)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, sessions
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()


def assert_settled(session):
    state = session.current_state
    assert state["script_variables"]["test_reward"] == 4
    story_state = state["story_tree_state"]
    assert story_state["completed"].count("test_decision") == 1
    assert "test_decision" not in story_state["active"]
    assert story_state["choices_made"]["test_decision"] == "test_open"
    assert {"test_followup", "test_other", "test_signalled"} <= set(story_state["unlocked"])
    assert state["events"]["test_decision"]["status"] == "completed"
    assert state["events"]["test_decision"]["choices_made"] == "test_open"
    assert state["events"]["test_other"]["status"] == "unlocked"
    assert state["events"]["test_signalled"]["status"] == "unlocked"
    assert "test_status" in state["active_persistent_states"]
    assert any(callback["text"] == "SYNTHETIC_PREVIEW_CALLBACK" for callback in state["narrative_callbacks"])
    entries = {entry.id: entry for entry in session.prompt_builder.lorebook.entries}
    assert entries["test_detail"].enabled
    assert entries["test_added"].content == "SYNTHETIC_PREVIEW_EFFECT"


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [7, 13])
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("summary_only", [False, True])
async def test_declared_preview_matches_actual_choice_reload_and_branch(tmp_path, monkeypatch, threshold, dynamic, summary_only):
    session = choice_session(threshold, dynamic)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        session.current_state["script_variables"]["test_gate"] = threshold
        final_id = await persist_node(session, root_id)
        assert (await preview(session))["available"] is True
        endpoint = f"/api/game/{session.save_id}/story-tree/choice?summary_only={str(summary_only).lower()}"
        payload = {"node_id": "test_decision", "choice_id": "test_open"}
        response = await client.post(endpoint, json=payload)
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert_settled(session)
        node = session.world_tree.get_node(final_id)
        assert node["_state_hash"] == session.world_tree.state_hash(node["state_snapshot"])
        if summary_only:
            assert response.json() == {"success": True}
        else:
            nodes = [node for tree in response.json()["trees"] for node in tree["nodes"]]
            decision = next(node for node in nodes if node["id"] == "test_decision")
            assert decision["status"] == "completed"
            assert decision["choices"][0]["chosen"] is True
            assert response.json()["completed"] == [{"id": "test_decision", "name": "Synthetic decision"}]
        for marker in ("SYNTHETIC_PREVIEW_DETAIL", "SYNTHETIC_PREVIEW_EFFECT", "SYNTHETIC_PREVIEW_CALLBACK"):
            assert marker not in response.text
        settled = copy.deepcopy(session.current_state)
        assert (await preview(session))["available"] is False
        replay = await client.post(endpoint, json=payload)
        assert replay.status_code == 200 and replay.json()["success"] is False
        assert session.current_state == settled
        assert (await stored_state(session))["script_variables"]["test_reward"] == 4
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored is not None
        assert_settled(restored)
        replay = await client.post(endpoint, json=payload)
        assert replay.status_code == 200 and replay.json()["success"] is False
        assert_settled(restored)
        for current in (session, restored):
            await current.branch_to_node(root_id)
            assert current.current_state["script_variables"]["test_reward"] == 0
            assert (await preview(current))["available"] is False
            assert not any(entry.id == "test_added" for entry in current.prompt_builder.lorebook.entries)
            assert not next(entry for entry in current.prompt_builder.lorebook.entries if entry.id == "test_detail").enabled
            await current.branch_to_node(final_id)
            assert_settled(current)
            assert (await preview(current))["available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id,choice_id,gate", [
    ("test_decision", "test_open", 0), ("test_decision", "missing", 7),
    ("missing", "test_open", 7), ("test_followup", "test_open", 7),
])
async def test_invalid_choice_does_not_change_state_or_storage(tmp_path, monkeypatch, node_id, choice_id, gate):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = gate
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": node_id, "choice_id": choice_id})
        assert response.status_code == 200 and response.json()["success"] is False
        assert session.current_state == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["effects", "database"])
@pytest.mark.parametrize("dynamic", [False, True])
async def test_failed_choice_rolls_back_state_lore_and_persisted_snapshot(tmp_path, monkeypatch, failure, dynamic):
    session = choice_session(dynamic=dynamic)
    session.current_state["script_variables"]["test_gate"] = 7
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        entries = [asdict(entry) for entry in session.prompt_builder.lorebook.entries]
        override_ids = session.prompt_builder._lorebook_override_ids.copy()
        apply_result = session._apply_story_tree_result

        def fail_after_effects(result):
            apply_result(result)
            raise RuntimeError("Synthetic effect failure")

        @asynccontextmanager
        async def fail_after_snapshot_update():
            async with database.get_db() as db:
                async def execute(sql, *args):
                    if sql.startswith("UPDATE saves"):
                        raise RuntimeError("Synthetic transaction failure")
                    return await db.execute(sql, *args)
                yield SimpleNamespace(execute=execute, commit=db.commit)

        with monkeypatch.context() as patch:
            if failure == "effects":
                patch.setattr(session, "_apply_story_tree_result", fail_after_effects)
            else:
                patch.setattr(game_routes, "get_db", fail_after_snapshot_update)
            response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                         json={"node_id": "test_decision", "choice_id": "test_open"})
        assert response.status_code == 500
        assert session.current_state == before
        assert await stored_state(session) == stored
        assert [asdict(entry) for entry in session.prompt_builder.lorebook.entries] == entries
        assert session.prompt_builder._lorebook_override_ids == override_ids
        assert (await preview(session))["available"] is True
        retry = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                  json={"node_id": "test_decision", "choice_id": "test_open"})
        assert retry.status_code == 200 and retry.json()["success"] is True
        assert_settled(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_story_tree", [False, True])
async def test_independent_event_choice_still_settles_once(tmp_path, monkeypatch, with_story_tree):
    session = choice_session()
    if not with_story_tree:
        session.story_tree_engine = None
        session.event_engine.shadow_story_nodes = False
    session.event_engine.add_event(GameEvent(
        id="test_independent", category="choice", choices=[{
            "id": "test_select", "effects": {"set_var": [{"var_id": "test_reward", "op": "inc", "value": 6}]},
        }],
    ))
    session.current_state["events"]["test_independent"] = {"status": "active"}
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        endpoint = f"/api/game/{session.save_id}/story-tree/choice"
        payload = {"node_id": "test_independent", "choice_id": "test_select"}
        response = await client.post(endpoint, json=payload)
        assert response.status_code == 200 and response.json()["success"] is True
        assert session.current_state["script_variables"]["test_reward"] == 6
        assert session.current_state["events"]["test_independent"]["choices_made"] == "test_select"
        repeat = await client.post(endpoint, json=payload)
        assert repeat.status_code == 200 and repeat.json()["success"] is False
        assert session.current_state["script_variables"]["test_reward"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow_state", ["empty", "unrelated", "stale"])
async def test_canonical_choice_owns_settlement_despite_shadow_state(tmp_path, monkeypatch, shadow_state):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.current_state["events"] = {}
    if shadow_state == "unrelated":
        session.current_state["events"]["test_external"] = {"status": "active", "metadata": {"test": 2}}
    elif shadow_state == "stale":
        session.current_state["events"]["test_decision"] = {"status": "completed", "choices_made": "obsolete"}
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": "test_decision", "choice_id": "test_open"})
        assert response.status_code == 200 and response.json()["success"] is True
        assert_settled(session)
        if shadow_state == "unrelated":
            assert session.current_state["events"]["test_external"] == {"status": "active", "metadata": {"test": 2}}
        for suffix in ("story-tree", "story-tree/active-choices"):
            view = await client.get(f"/api/game/{session.save_id}/{suffix}")
            assert view.status_code == 200
        assert_settled(session)


@pytest.mark.asyncio
async def test_story_choice_works_without_event_engine(tmp_path, monkeypatch):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.event_engine = None
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": "test_decision", "choice_id": "test_open"})
        assert response.status_code == 200 and response.json()["success"] is True
        assert session.current_state["script_variables"]["test_reward"] == 4
        assert session.current_state["story_tree_state"]["choices_made"]["test_decision"] == "test_open"


@pytest.mark.parametrize("shadow", [False, True])
def test_shadow_sync_preserves_independent_events_and_trigger_ownership(shadow):
    session = choice_session()
    engine = session.event_engine
    engine.shadow_story_nodes = shadow
    engine.add_event(GameEvent(id="test_trigger", category="trigger"))
    engine._tree_for_event["test_trigger"] = "test_tree"
    state = session.current_state
    state["events"].update({
        "test_trigger": {"status": "active", "choices_made": "trigger-owned"},
        "test_external": {"status": "active", "progress": {"remaining": 9}},
    })
    state["events"]["test_other"] = {"status": "completed", "choices_made": "stale", "progress": {}, "cooldown_until": 8}
    state["story_tree_state"].update({
        "completed": ["test_decision"], "active": ["test_decision", "test_followup"],
        "unlocked": ["test_decision"], "choices_made": {"test_decision": "test_open"},
        "timed_progress": {"test_followup": {"remaining": 2}}, "periodic_cooldowns": {"test_followup": 4},
    })
    before = copy.deepcopy(state)
    engine.sync_story_tree_state(state)
    if not shadow:
        assert state == before
        return
    assert state["events"]["test_decision"]["status"] == "completed"
    assert state["events"]["test_decision"]["choices_made"] == "test_open"
    assert state["events"]["test_other"] == {"status": "locked"}
    assert state["events"]["test_followup"] == {"status": "active", "progress": {"remaining": 2}, "cooldown_until": 4}
    assert state["events"]["test_trigger"] == before["events"]["test_trigger"]
    assert state["events"]["test_external"] == before["events"]["test_external"]
    assert state["story_tree_state"] == before["story_tree_state"]
    state["events"]["test_followup"]["progress"]["remaining"] = 1
    assert state["story_tree_state"]["timed_progress"]["test_followup"]["remaining"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_commit", [False, True])
async def test_choice_vector_updates_wait_for_commit(tmp_path, monkeypatch, fail_commit):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    indexed = {}
    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=lambda batch: indexed.update({entry_id: content for entry_id, content, metadata in batch}),
        remove_lorebook=lambda entry_id: indexed.pop(entry_id, None),
    )
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        persist = game_routes._persist_active_state
        observed = []

        async def inspect_before_commit(current):
            await current._drain_background_tasks(index_only=True)
            observed.append(copy.deepcopy(indexed))
            if fail_commit:
                raise RuntimeError("Synthetic commit failure")
            await persist(current)

        monkeypatch.setattr(game_routes, "_persist_active_state", inspect_before_commit)
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": "test_decision", "choice_id": "test_open"})
        await session._drain_background_tasks()
        assert observed == [{}]
        assert response.status_code == (500 if fail_commit else 200)
        assert indexed == ({} if fail_commit else {"test_added": "SYNTHETIC_PREVIEW_EFFECT"})


@pytest.mark.asyncio
async def test_concurrent_choice_requests_do_not_duplicate_rewards(tmp_path, monkeypatch):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        endpoint = f"/api/game/{session.save_id}/story-tree/choice"
        payload = {"node_id": "test_decision", "choice_id": "test_open"}
        responses = await asyncio.gather(client.post(endpoint, json=payload), client.post(endpoint, json=payload))
        assert all(response.status_code == 200 for response in responses)
        assert sorted(response.json()["success"] for response in responses) == [False, True]
        assert_settled(session)
        assert (await stored_state(session))["script_variables"]["test_reward"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["node_completed.test_decision == true", "events.test_decision.status == completed"])
async def test_choice_signal_observes_completed_node_in_both_state_views(tmp_path, monkeypatch, condition):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.current_state["player"]["test_chain_reward"] = 0
    session.event_engine.add_event(GameEvent(
        id="test_signal_handler", category="trigger", condition=condition,
        metadata={"event": ["test_signal"]},
        effects={"set_var": [{"var_id": "player.test_chain_reward", "op": "add", "value": 5}]},
    ))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        endpoint = f"/api/game/{session.save_id}/story-tree/choice"
        payload = {"node_id": "test_decision", "choice_id": "test_open"}
        response = await client.post(endpoint, json=payload)
        assert response.status_code == 200 and response.json()["success"] is True
        assert_settled(session)
        assert session.current_state["player"]["test_chain_reward"] == 5
        assert (await stored_state(session))["player"]["test_chain_reward"] == 5
        await client.post(endpoint, json=payload)
        assert session.current_state["player"]["test_chain_reward"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id", ["test_decision", "missing"])
async def test_rejected_choice_does_not_persist_legacy_state_migration(tmp_path, monkeypatch, node_id):
    session = choice_session()
    session.current_state.pop("events")
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": node_id, "choice_id": "test_open"})
        assert response.status_code == 200 and response.json()["success"] is False
        assert session.current_state == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
async def test_cancelled_choice_discards_effects_and_releases_transaction_state(tmp_path, monkeypatch):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        entered = asyncio.Event()
        waiting = asyncio.Event()

        async def wait_before_commit(current):
            entered.set()
            await waiting.wait()

        endpoint = f"/api/game/{session.save_id}/story-tree/choice"
        payload = {"node_id": "test_decision", "choice_id": "test_open"}
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "_persist_active_state", wait_before_commit)
            pending = asyncio.create_task(client.post(endpoint, json=payload))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert session.current_state == before
        assert await stored_state(session) == stored
        assert session._deferred_lore_vector_ids is None
        assert not session.prompt_builder._lorebook_override_ids
        assert not game_routes._get_session_lock(session.save_id).locked()
        retry = await client.post(endpoint, json=payload)
        assert retry.status_code == 200 and retry.json()["success"] is True
        assert_settled(session)


@pytest.mark.asyncio
async def test_normalized_dynamic_auto_node_is_not_a_manual_choice(tmp_path, monkeypatch):
    session = choice_session()
    session._apply_director_results({"tree": {"new_nodes": [{
        "id": "test_generated", "type": "choice", "choices": [{"id": "test_select"}],
    }]}})
    assert session.story_tree_engine._nodes["test_generated"]["type"] == "auto"
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = copy.deepcopy(session.current_state)
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice",
                                     json={"node_id": "test_generated", "choice_id": "test_select"})
        assert response.status_code == 200 and response.json()["success"] is False
        assert session.current_state == before
