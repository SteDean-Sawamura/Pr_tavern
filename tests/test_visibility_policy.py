import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import config_routes, game_routes, save_routes
from db import database
from engine.event_scheduler import (
    is_public_event, is_public_story_node, public_event_notifications,
)


def make_script():
    return {
        "script_id": "synthetic-policy", "name": "Synthetic policy fixture",
        "visibility_policy": {
            "default_event_visibility": "world",
            "default_story_visibility": "world",
            "require_notification_source": True,
            "npc_public_profiles_only": True,
            "private_world_properties": ["internal_counter"],
            "private_expired_states": ["internal_status"],
            "private_location_descriptions": ["restricted_room"],
            "inherit_snapshot_metadata": True,
        },
        "one_time_events": [
            {"id": "notice", "visibility": "public", "trigger_time": "2030-01-01T00:00:00"},
            {"id": "internal_event"},
        ],
        "cyclic_events": [{"id": "tick", "visibility": "world"}],
        "story_tree": {"trees": [{"nodes": [
            {"id": "public_step", "visibility": "public"}, {"id": "internal_step"},
        ]}]},
    }


@pytest.mark.parametrize("checker,visible,hidden", [
    (is_public_event, "notice", "internal_event"),
    (is_public_story_node, "public_step", "internal_step"),
])
def test_visibility_defaults_are_data_driven(checker, visible, hidden):
    script = make_script()
    assert checker(script, visible)
    assert not checker(script, hidden)
    assert not checker(script, "unknown")
    script.pop("visibility_policy")
    assert checker(script, hidden)
    assert checker(script, "unknown")


def test_notifications_preserve_legacy_defaults_and_respect_strict_policy():
    result = SimpleNamespace(
        notifications=["shown", "hidden", "shown step", "hidden step", "unsourced"],
        notification_sources=["notice", "internal_event", "public_step", "internal_step", ""],
    )
    assert public_event_notifications(make_script(), result) == ["shown", "shown step"]
    result.notification_sources = []
    assert public_event_notifications(make_script(), result) == []
    assert public_event_notifications({}, result) == result.notifications


def test_player_projection_uses_private_metadata_without_mutating_state():
    script = make_script()
    state = {
        "player": {"location": "public_room"},
        "world_properties": {"internal_counter": 7, "public_counter": 2},
        "npcs": {"visitor": {"name": "Visitor", "known": True, "bio": "Private detail"}},
        "visible_locations": ["public_room", "restricted_room"],
        "location_descriptions": {"public_room": "Public view", "restricted_room": "Private view"},
        "lorebook_entry_overrides": {"private_note": {"content": "Synthetic internal note."}, "removed_note": None},
    }
    original = copy.deepcopy(state)
    projected = game_routes._player_state(state, script)
    assert projected["world_properties"] == {"public_counter": 2}
    assert "bio" not in projected["npcs"]["visitor"]
    assert projected["location_descriptions"] == {"public_room": "Public view"}
    assert "lorebook_entry_overrides" not in projected
    assert game_routes._player_expired_states(["internal_status", "visible_status"], script) == ["visible_status"]
    assert state == original
    script["locations"] = [{"id": "restricted_room", "public_description": "Public outline"}]
    assert game_routes._player_state(state, script)["location_descriptions"]["restricted_room"] == "Public outline"


def test_snapshot_metadata_backfill_is_opt_in_and_does_not_overwrite():
    current = make_script()
    snapshot = {
        "one_time_events": [
            {"id": "notice", "trigger_time": "2030-01-01T00:00:00"},
        ],
        "cyclic_events": [{"id": "tick", "visibility": "public"}],
        "story_tree": {"trees": [{"nodes": [{"id": "public_step"}]}]},
    }
    unchanged = copy.deepcopy(snapshot)
    without_policy = copy.deepcopy(current)
    without_policy.pop("visibility_policy")
    game_routes._fill_snapshot_metadata(snapshot, without_policy)
    assert snapshot == unchanged
    game_routes._fill_snapshot_metadata(snapshot, current)
    assert snapshot["one_time_events"][0]["visibility"] == "public"
    assert snapshot["cyclic_events"][0]["visibility"] == "public"
    assert snapshot["story_tree"]["trees"][0]["nodes"][0]["visibility"] == "public"
    assert snapshot["visibility_policy"] == current["visibility_policy"]
    snapshot["visibility_policy"]["private_world_properties"].append("snapshot_only")
    assert "snapshot_only" not in current["visibility_policy"]["private_world_properties"]
    changed_time = {"one_time_events": [{"id": "notice", "trigger_time": "2031-01-01T00:00:00"}]}
    game_routes._fill_snapshot_metadata(changed_time, current)
    assert "visibility" not in changed_time["one_time_events"][0]


@pytest.mark.asyncio
async def test_restore_and_export_backfill_the_same_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "policy.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    current = make_script()
    snapshot = {"script_id": current["script_id"], "name": "Older synthetic snapshot"}
    try:
        async with database.get_db() as connection:
            await connection.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (current["script_id"], current["name"], json.dumps(current)),
            )
            await connection.execute(
                "INSERT INTO saves (id, script_id) VALUES (?, ?)",
                ("policy-save", current["script_id"]),
            )
            await connection.execute(
                "INSERT INTO save_scripts (save_id, content) VALUES (?, ?)",
                ("policy-save", json.dumps(snapshot)),
            )
            await connection.commit()
        restored = await game_routes._restore_session("policy-save")
        exported = json.loads((await save_routes.export_save("policy-save")).body)
        assert restored.script["visibility_policy"] == current["visibility_policy"]
        assert exported["effective_script"]["visibility_policy"] == restored.script["visibility_policy"]
        assert restored.script["name"] == snapshot["name"]
        async with database.get_db() as connection:
            row = await (await connection.execute(
                "SELECT content FROM save_scripts WHERE save_id = ?", ("policy-save",),
            )).fetchone()
        assert json.loads(row["content"]) == snapshot
    finally:
        await database.close_pool()
