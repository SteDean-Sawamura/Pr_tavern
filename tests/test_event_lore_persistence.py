import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import config_routes, game_routes
from db import database
from engine.event_engine import EventResult
from engine.game_session import GameSession
from engine.story_tree import StoryTreeResult


@pytest.fixture(params=["add_lore", "update_lore", "remove_lore"])
def event_session(request):
    original = {"id": "test_notice", "keys": ["OldNotice"], "content": "The counter opens at nine."}
    replacement = {"id": "test_notice", "keys": ["NewNotice"], "content": "The counter opens at ten."}
    effect = {"type": request.param, **replacement}
    if request.param == "add_lore":
        effect = {"type": "add_lore", "entry": replacement}
    script = {
        "script_id": "synthetic-event-lore", "name": "Synthetic event lore", "settings": {},
        "lorebook": [] if request.param == "add_lore" else [original],
        "one_time_events": [{
            "id": "test_notice_change", "trigger_time": "2030-01-01T09:05:00",
            "effects": [effect],
        }],
    }
    session = GameSession(copy.deepcopy(script), None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    expected = [] if request.param == "remove_lore" else [replacement["content"]]
    return session, script, expected


async def read_notice(session):
    result = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "test_notice"}))
    return [entry["content"] for entry in result["entries"]]


@pytest.mark.asyncio
async def test_scheduled_lore_change_survives_fresh_session(event_session):
    session, script, expected = event_session
    triggered = []
    session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", triggered)
    assert [event["event_id"] for event in triggered] == ["test_notice_change"]
    assert await read_notice(session) == expected
    restored = GameSession(copy.deepcopy(script), None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._hydrate_branch_runtime(restored.current_state)
    assert await read_notice(restored) == expected
    assert restored.prompt_builder.lorebook.primary_key_match_count("test_notice", "OldNotice") == 0
    assert restored.prompt_builder.lorebook.primary_key_match_count("test_notice", "NewNotice") == bool(expected)


@pytest.mark.asyncio
async def test_scheduled_lore_change_does_not_cross_branches(event_session):
    session, _, expected = event_session
    initial = await read_notice(session)
    root_id = session.world_tree.add_node(
        None, session.current_state["game_time"], 0, state_snapshot=copy.deepcopy(session.current_state),
    )
    session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", [])
    assert await read_notice(session) == expected
    final_id = session.world_tree.add_node(
        root_id, session.current_state["game_time"], 1, state_snapshot=copy.deepcopy(session.current_state),
    )
    await session.branch_to_node(root_id)
    assert await read_notice(session) == initial
    assert session.prompt_builder.lorebook.primary_key_match_count("test_notice", "OldNotice") == bool(initial)
    assert session.prompt_builder.lorebook.primary_key_match_count("test_notice", "NewNotice") == 0
    await session.branch_to_node(final_id)
    assert await read_notice(session) == expected


def test_scheduled_lore_effect_does_not_mutate_script_metadata(event_session):
    session, _, _ = event_session
    original = copy.deepcopy(session.script)
    session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", [])
    assert session.script == original


@pytest.mark.asyncio
async def test_scheduled_lore_projection_stays_provisional(event_session):
    session, _, expected = event_session
    original_state = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(session.script)
    original_notice = await read_notice(session)
    context = {
        "old_time": "2030-01-01T09:00:00",
        "_accepted_state_updates": [{"end_time": "2030-01-01T09:05:00"}],
    }
    result = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_notice"}))
    assert [entry["content"] for entry in result["entries"]] == expected
    assert result["projection"]["provisional"] is True
    assert result["projection"]["event_ids"] == ["test_notice_change"]
    assert session.current_state == original_state
    assert session.script == original_script
    assert await read_notice(session) == original_notice
    assert not session.prompt_builder._lorebook_override_ids
    session._settle_timed_events(context["old_time"], "2030-01-01T09:05:00", [])
    assert await read_notice(session) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["result", "sync"])
async def test_event_result_lore_uses_explicit_target(event_session, source):
    session, script, expected = event_session
    effect = script["one_time_events"][0]["effects"][0]
    result = EventResult()
    if source == "sync":
        result.lorebook_sync = [{
            **effect, "action": effect["type"].removesuffix("_lore"), "entry_id": "test_notice",
        }]
    elif effect["type"] == "add_lore":
        result.lore_additions = [effect["entry"]]
    elif effect["type"] == "update_lore":
        result.lore_updates = [effect]
    else:
        result.lore_removals = ["test_notice"]
    original_result = copy.deepcopy(result)
    original_state = copy.deepcopy(session.current_state)
    target = {}
    session._apply_event_result(result, state=target)
    assert await read_notice(session) == expected
    assert session.current_state == original_state
    assert result == original_result
    restored = GameSession(copy.deepcopy(script), None)
    restored.current_state = json.loads(json.dumps(target))
    restored._hydrate_branch_runtime(restored.current_state)
    assert await read_notice(restored) == expected
    session._hydrate_branch_runtime(original_state)
    assert await read_notice(session) == ([] if effect["type"] == "add_lore" else [script["lorebook"][0]["content"]])


@pytest.mark.asyncio
async def test_story_tree_lore_changes_survive_restore(event_session):
    session, script, expected = event_session
    effect = script["one_time_events"][0]["effects"][0]
    result = StoryTreeResult()
    if effect["type"] == "add_lore":
        result.lore_additions = [effect["entry"]]
    elif effect["type"] == "update_lore":
        result.lore_updates = [effect]
    else:
        result.lore_removals = ["test_notice"]
    original_result = copy.deepcopy(result)
    session._apply_story_tree_result(result)
    assert result == original_result
    restored = GameSession(copy.deepcopy(script), None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._hydrate_branch_runtime(restored.current_state)
    assert await read_notice(restored) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("lore_source", ["static", "dynamic_lorebook", "dynamic_story_content"])
async def test_scheduled_lore_roundtrip_through_sqlite(event_session, tmp_path, monkeypatch, lore_source):
    session, script, expected = event_session
    if lore_source != "static":
        initial_state = copy.deepcopy(session.current_state)
        initial_lore = script.pop("lorebook")
        session = GameSession(copy.deepcopy(script), None)
        session.current_state = initial_state
        session.current_state[lore_source] = (
            {"lorebook": initial_lore} if lore_source == "dynamic_story_content" else initial_lore
        )
        session._restore_dynamic_story()
        session._hydrate_branch_runtime(session.current_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "event-lore.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    try:
        async with database.get_db() as connection:
            await connection.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (script["script_id"], script["name"], json.dumps(script)),
            )
            await connection.execute(
                "INSERT INTO saves (id, script_id) VALUES (?, ?)",
                (session.save_id, script["script_id"]),
            )
            await connection.commit()
        initial = await read_notice(session)
        root_id = session.world_tree.add_node(
            None, session.current_state["game_time"], 0,
            state_snapshot=copy.deepcopy(session.current_state),
        )
        await game_routes._save_node(session, {"node_id": root_id, "state": session.current_state})
        session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", [])
        session.current_state["game_time"] = "2030-01-01T09:05:00"
        final_id = session.world_tree.add_node(
            root_id, session.current_state["game_time"], 1,
            state_snapshot=copy.deepcopy(session.current_state),
        )
        await game_routes._save_node(session, {"node_id": final_id, "state": session.current_state})
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        assert restored is not session
        assert await read_notice(restored) == expected
        async with database.get_db() as connection:
            row = await (await connection.execute(
                "SELECT state_snapshot FROM tree_nodes WHERE id = ?", (final_id,),
            )).fetchone()
        snapshot = json.loads(row["state_snapshot"])
        assert "test_notice" in snapshot["lorebook_entry_overrides"]
        game_routes._sessions[session.save_id] = restored
        public_state = await game_routes.get_state(session.save_id)
        public_node = await game_routes.get_tree_node(session.save_id, final_id)
        assert "lorebook_entry_overrides" not in json.dumps([public_state, public_node])
        assert "The counter opens" not in json.dumps([public_state, public_node])
        await restored.branch_to_node(root_id)
        assert await read_notice(restored) == initial
        await restored.branch_to_node(final_id)
        assert await read_notice(restored) == expected
        triggered = []
        restored._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", triggered)
        assert triggered == []
    finally:
        await session._drain_background_tasks()
        await database.close_pool()


@pytest.mark.asyncio
async def test_lore_change_order_preserves_metadata_and_snapshots():
    session = GameSession({"script_id": "synthetic-lore-order", "settings": {}}, None)
    session.current_state = {"player": {}}
    entry = {
        "id": "test_notice", "keys": ["OldNotice"], "content": "Synthetic hidden notice.",
        "visibility": "hidden", "known_by_npcs": ["test_reader"],
    }
    session._apply_lorebook_changes(additions=[entry])
    session._set_lore_enabled("test_notice", False)
    session._apply_lorebook_changes(updates=[{"id": "test_notice", "content": "Amended hidden notice."}])
    disabled = copy.deepcopy(session.current_state)
    session._apply_lorebook_changes(removals=["test_notice"])
    session._apply_lorebook_changes(additions=[{**entry, "keys": ["NewNotice"]}])
    replacement = copy.deepcopy(session.current_state)
    session._hydrate_branch_runtime(disabled)
    assert await read_notice(session) == []
    session._hydrate_branch_runtime(replacement)
    session.current_state = replacement
    assert await read_notice(session) == [entry["content"]]
    restored_entry = next(entry for entry in session.prompt_builder.lorebook.entries if entry.id == "test_notice")
    assert restored_entry.visibility == "hidden"
    assert restored_entry.known_by_npcs == ["test_reader"]
    assert entry["keys"] == ["OldNotice"]
    assert disabled["lorebook_entry_overrides"]["test_notice"]["enabled"] is False
    assert replacement["lorebook_entry_overrides"]["test_notice"]["enabled"] is True
    session._apply_lorebook_changes(additions=[{**entry, "content": "Duplicate must not replace."}])
    assert await read_notice(session) == [entry["content"]]


@pytest.mark.asyncio
async def test_vector_sync_tracks_final_branch_and_ignores_stale_text(event_session):
    session, _, expected = event_session
    original = copy.deepcopy(session.current_state)
    indexed = {}

    def add_batch(batch):
        indexed.update({entry_id: content for entry_id, content, metadata in batch})

    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=add_batch, remove_lorebook=lambda entry_id: indexed.pop(entry_id, None),
        query=lambda *args: [{"doc_type": "lorebook", "entry_id": "test_notice", "text": "Stale index text."}],
    )
    session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", [])
    changed = copy.deepcopy(session.current_state)
    session._hydrate_branch_runtime(original)
    session._hydrate_branch_runtime(changed)
    await session._drain_background_tasks()
    assert list(indexed.values()) == expected
    activated = []
    context, _ = await session._enrich_with_rag("notice", [], activated, "")
    assert "Stale index text." not in context
    assert [entry.content for entry in activated] == expected
    session._hydrate_branch_runtime(original)
    await session._drain_background_tasks()
    assert list(indexed.values()) == [entry["content"] for entry in session.script.get("lorebook", [])]


@pytest.mark.asyncio
async def test_short_lore_update_removes_obsolete_vector():
    session = GameSession({"script_id": "synthetic-short-lore", "settings": {}}, None)
    indexed = {}
    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=lambda batch: indexed.update({entry_id: content for entry_id, content, metadata in batch}),
        remove_lorebook=lambda entry_id: indexed.pop(entry_id, None),
    )
    session._apply_lorebook_changes(additions=[{
        "id": "test_notice", "keys": ["Notice"], "content": "A sufficiently long synthetic notice.",
    }])
    session._apply_lorebook_changes(updates=[{"id": "test_notice", "content": "Revised."}])
    await session._drain_background_tasks()
    assert indexed == {}
    assert await read_notice(session) == ["Revised."]
