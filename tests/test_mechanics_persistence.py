import copy
import json
from collections import OrderedDict
from unittest.mock import AsyncMock

import pytest

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession


def make_session():
    script = {
        "script_id": "synthetic-mechanics", "name": "Synthetic mechanics", "settings": {},
        "variables": [{"id": "test_static_count", "type": "number", "default": 2, "min": 0, "max": 10}],
        "triggers": [{
            "id": "test_static_trigger", "condition": "event.test_signal.fired",
            "actions": [{"type": "activate_persistent_state", "target": "test_static_state"}],
        }, {"event": "test_legacy_signal", "action": "notify", "params": {"message": "Synthetic signal."}}],
        "random_items": [{"id": "test_static_roll", "dice": "1d1", "trigger_type": "per_turn"}],
    }
    session = GameSession(script, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session, script


def dynamic_updates():
    return {
        "variables": {"add": [{"id": "test_dynamic_count", "type": "number", "default": 1, "min": 0, "max": 10}]},
        "triggers": {"add": [{
            "id": "test_dynamic_trigger", "condition": "event.test_signal.fired",
            "actions": [{"type": "activate_persistent_state", "target": "test_dynamic_state"}],
        }]},
        "random_items": {"add": [{"id": "test_dynamic_roll", "dice": "1d1", "trigger_type": "per_turn"}]},
    }


def definitions(session):
    return copy.deepcopy({
        "variables": session.script_variables.definitions,
        "triggers": session.trigger_engine.triggers,
        "random_items": session.script.get("random_items", []),
    })


def test_restore_preserves_latest_variable_value_and_bounds():
    session, script = make_session()
    session._apply_mechanics_results(dynamic_updates())
    session._apply_mechanics_results({"variables": {"update": [{"id": "test_dynamic_count", "value": 3}]}})
    session.script_variables.set(session.current_state, "test_dynamic_count", 7)
    saved = json.loads(json.dumps(session.current_state))
    restored = GameSession(script, None)
    restored.current_state = copy.deepcopy(saved)
    restored._restore_dynamic_story()
    restored._hydrate_branch_runtime(restored.current_state)
    assert restored.current_state["script_variables"] == saved["script_variables"]
    restored.script_variables.set(restored.current_state, "test_dynamic_count", 50)
    assert restored.current_state["script_variables"]["test_dynamic_count"] == 10
    assert definitions(restored) == definitions(session)


@pytest.mark.asyncio
async def test_mechanics_follow_saved_branch_instead_of_latest_runtime():
    session, _ = make_session()
    original = definitions(session)
    root = session.world_tree.add_node(None, "2030-01-01T09:00:00", 0, state_snapshot=copy.deepcopy(session.current_state))
    changes = dynamic_updates()
    changes["triggers"]["remove"] = ["test_static_trigger"]
    changes["random_items"]["remove"] = ["test_static_roll"]
    session._apply_mechanics_results(changes)
    changed = definitions(session)
    final = session.world_tree.add_node(root, "2030-01-01T09:01:00", 1, state_snapshot=copy.deepcopy(session.current_state))
    result = await session.branch_to_node(root)
    assert result["node_id"] == root
    assert definitions(session) == original
    result = await session.branch_to_node(final)
    assert result["node_id"] == final
    assert definitions(session) == changed
    state = copy.deepcopy(session.current_state)
    session.trigger_engine.execute_actions(session.trigger_engine.fire("test_signal", state), state)
    assert state["active_persistent_states"] == ["test_dynamic_state"]
    legacy = session.trigger_engine.fire("test_legacy_signal", state)
    assert session.trigger_engine.execute_actions(legacy, state)["notifications"] == ["Synthetic signal."]


def test_timed_effect_rules_survive_reload_without_director_history():
    session, script = make_session()
    changes = dynamic_updates()
    event = {"effects": [
        {"type": "add_variable", "variable": changes["variables"]["add"][0]},
        {"type": "add_trigger", "trigger": changes["triggers"]["add"][0]},
        {"type": "add_random_item", "item": changes["random_items"]["add"][0]},
    ]}
    original_event = copy.deepcopy(event)
    session._apply_event_def_effects(event, "test_rule_change")
    saved = json.loads(json.dumps(session.current_state))
    assert not saved.get("dynamic_story_content")
    restored = GameSession(script, None)
    restored.current_state = saved
    restored._hydrate_branch_runtime(saved)
    assert definitions(restored) == definitions(session)
    assert event == original_event


def test_definition_snapshots_survive_pruned_mechanics_history():
    session, script = make_session()
    session._apply_mechanics_results(dynamic_updates())
    for value in range(25):
        session._apply_mechanics_results({"variables": {"update": [{"id": "test_dynamic_count", "value": value}]}})
    session._apply_director_results({})
    session.current_state["dynamic_story_content"]["random_item_updates"] = []
    session.current_state["dynamic_story_content"]["trigger_updates"] = []
    restored = GameSession(script, None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._restore_dynamic_story()
    restored._hydrate_branch_runtime(restored.current_state)
    assert definitions(restored) == definitions(session)
    assert restored.current_state["script_variables"] == session.current_state["script_variables"]


def test_dynamic_additions_are_idempotent_and_leave_input_unchanged():
    session, script = make_session()
    changes = dynamic_updates()
    original = copy.deepcopy(changes)
    session._apply_mechanics_results(changes)
    expected = definitions(session)
    session._apply_mechanics_results(copy.deepcopy(original))
    assert definitions(session) == expected
    assert changes == original
    assert script["random_items"][0]["id"] == "test_static_roll"


def test_legacy_history_preserves_saved_values_when_loading():
    session, _ = make_session()
    changes = dynamic_updates()
    state = copy.deepcopy(session.current_state)
    state["script_variables"]["test_dynamic_count"] = 7
    state["dynamic_story_content"] = {
        "variable_updates": [{"turn": 1, "updates": changes["variables"]}, {
            "turn": 2, "updates": {"update": [{"id": "test_dynamic_count", "value": 3}]},
        }],
        "random_item_updates": [{"turn": 1, "updates": changes["random_items"]}],
        "trigger_updates": [{"turn": 1, "updates": changes["triggers"]}],
    }
    original = copy.deepcopy(state)
    session.current_state = state
    session._restore_dynamic_story()
    session._hydrate_branch_runtime(state)
    assert state == original
    assert "test_dynamic_count" in session.script_variables.definitions
    assert len(session.trigger_engine.triggers) == 3


@pytest.mark.parametrize("initial_triggers", [True, False])
def test_lifecycle_trigger_updates_reach_both_execution_caches(initial_triggers):
    session, script = make_session()
    if not initial_triggers:
        script["triggers"] = []
        initial = copy.deepcopy(session.current_state)
        session = GameSession(script, None)
        session.current_state = initial
    original = copy.deepcopy(session.current_state)
    session._apply_trigger_updates({"add": [{
        "id": "test_count_hook", "event": "test_tick", "action": "set_var",
        "params": {"var_id": "test_static_count", "op": "inc", "value": 1},
    }]})
    session._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 3
    session._apply_trigger_updates({"update": [{
        "id": "_dyn_test_count_hook", "params": {"var_id": "test_static_count", "op": "inc", "value": 2},
    }]})
    session._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 5
    enabled = copy.deepcopy(session.current_state)
    session._apply_trigger_updates({"update": [{"id": "_dyn_test_count_hook", "enabled": False}]})
    session._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 5
    session.current_state = copy.deepcopy(original)
    session._hydrate_branch_runtime(session.current_state)
    session._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 2
    session.current_state = enabled
    session._hydrate_branch_runtime(enabled)
    session._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 7


def test_trigger_execution_order_survives_restore():
    session, script = make_session()
    session._apply_trigger_updates({"add": [{
        "id": identifier, "event": "test_tick", "action": "set_var",
        "params": {"var_id": "test_static_count", "op": "set", "value": value},
    } for identifier, value in (("test_order_z", 4), ("test_order_a", 9))]})
    restored = GameSession(script, None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._hydrate_branch_runtime(restored.current_state)
    session._fire_event_dual("test_tick")
    restored._fire_event_dual("test_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 9
    assert restored.current_state["script_variables"]["test_static_count"] == 9


@pytest.mark.asyncio
async def test_failed_generation_restores_mechanics_and_execution_caches():
    session, _ = make_session()
    baseline = definitions(session)
    snapshot = game_routes._capture_generation_state(session)
    changes = dynamic_updates()
    changes["triggers"]["add"].append({
        "id": "test_failed_hook", "event": "test_failed_tick", "action": "set_var",
        "params": {"var_id": "test_static_count", "op": "inc", "value": 1},
    })
    session._apply_mechanics_results(changes)
    await game_routes._rollback_generation_state(session, snapshot)
    assert definitions(session) == baseline
    assert session.current_state == snapshot["state"]
    session._fire_event_dual("test_failed_tick")
    assert session.current_state["script_variables"]["test_static_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["scheduled", "director", "legacy"])
async def test_mechanics_roundtrip_through_real_sqlite(tmp_path, monkeypatch, source):
    session, script = make_session()
    changes = dynamic_updates()
    changes["triggers"]["update"] = [{"id": "test_static_trigger", "enabled": False}]
    if source == "scheduled":
        initial = copy.deepcopy(session.current_state)
        script["one_time_events"] = [{
            "id": "test_rule_change", "trigger_time": "2030-01-01T09:01:00", "effects": [
                {"type": "add_variable", "variable": changes["variables"]["add"][0]},
                {"type": "add_trigger", "trigger": changes["triggers"]["add"][0]},
                {"type": "update_trigger", "update": changes["triggers"]["update"][0]},
                {"type": "add_random_item", "item": changes["random_items"]["add"][0]},
            ],
        }]
        session = GameSession(script, None)
        session.current_state = initial
    baseline = definitions(session)
    root = session.world_tree.add_node(None, "2030-01-01T09:00:00", 0, state_snapshot=copy.deepcopy(session.current_state))
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "mechanics.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                                     (script["script_id"], script["name"], json.dumps(script)))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                                     (session.save_id, script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root, "state": session.current_state})
        if source == "scheduled":
            session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:01:00", [])
        else:
            session._apply_mechanics_results(changes)
        session.script_variables.set(session.current_state, "test_dynamic_count", 7)
        if source == "legacy":
            session.current_state.pop("mechanics_overrides")
        expected = definitions(session)
        final = session.world_tree.add_node(root, "2030-01-01T09:01:00", 1, state_snapshot=copy.deepcopy(session.current_state))
        await game_routes._save_node(session, {"node_id": final, "state": session.current_state})
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        assert definitions(restored) == expected
        assert restored.current_state["script_variables"]["test_dynamic_count"] == 7
        restored._fire_event_dual("test_signal")
        assert restored.current_state["active_persistent_states"] == ["test_dynamic_state"]
        game_routes._sessions[session.save_id] = restored
        public_state = await game_routes.get_state(session.save_id)
        public_node = await game_routes.get_tree_node(session.save_id, final)
        assert "mechanics_overrides" not in json.dumps([public_state, public_node])
        assert "test_dynamic_trigger" not in json.dumps([public_state, public_node])
        branch = await restored.branch_to_node(root)
        assert branch["node_id"] == root
        assert definitions(restored) == baseline
        branch = await restored.branch_to_node(final)
        assert branch["node_id"] == final
        assert definitions(restored) == expected
        restored.script_variables.set(restored.current_state, "test_dynamic_count", 50)
        assert restored.current_state["script_variables"]["test_dynamic_count"] == 10
    finally:
        await session._drain_background_tasks()
        await database.close_pool()
