"""Tests for engine.state_manager module."""

import pytest
from engine.state_manager import StateManager


class TestStateManager:
    def _make_manager(self, pc_attrs=None, pc_rels=None):
        script = {
            "player_character": {
                "attributes": pc_attrs or {},
                "relationships": pc_rels or {},
            },
            "persistent_states": [],
        }
        return StateManager(script)

    def test_apply_add(self):
        mgr = self._make_manager()
        state = {"player": {"health": 80}}
        changes = [{"target": "player.health", "op": "add", "value": -20}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["health"] == 60
        assert log[0]["old"] == 80
        assert log[0]["new"] == 60

    def test_apply_set(self):
        mgr = self._make_manager()
        state = {"player": {"health": 80}}
        changes = [{"target": "player.health", "op": "set", "value": 50}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["health"] == 50

    def test_apply_multiply(self):
        mgr = self._make_manager()
        state = {"player": {"gold": 100}}
        changes = [{"target": "player.gold", "op": "multiply", "value": 2}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["gold"] == 200

    def test_apply_does_not_mutate_original(self):
        mgr = self._make_manager()
        state = {"player": {"health": 80}}
        changes = [{"target": "player.health", "op": "set", "value": 10}]
        new_state, _ = mgr.apply_changes(state, changes)
        assert state["player"]["health"] == 80  # Original unchanged
        assert new_state["player"]["health"] == 10

    def test_clamp_to_rules(self):
        mgr = self._make_manager(pc_attrs={
            "health": {"min": 0, "max": 100},
        })
        state = {"player": {"health": 10}}

        # Over max
        changes = [{"target": "player.health", "op": "add", "value": 200}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["health"] == 100

        # Under min
        changes = [{"target": "player.health", "op": "add", "value": -300}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["health"] == 0

    def test_nested_path_creation(self):
        mgr = self._make_manager()
        state = {"player": {}}
        changes = [{"target": "player.inventory.gold", "op": "set", "value": 50}]
        new_state, _ = mgr.apply_changes(state, changes)
        assert new_state["player"]["inventory"]["gold"] == 50

    def test_dotted_path_with_attributes_shortcut(self):
        mgr = self._make_manager()
        state = {"player": {"attributes": {"health": 80}}}
        changes = [{"target": "player.health", "op": "add", "value": 10}]
        new_state, _ = mgr.apply_changes(state, changes)
        assert new_state["player"]["attributes"]["health"] == 90

    def test_dotted_path_with_relationships_shortcut(self):
        mgr = self._make_manager()
        state = {"player": {"relationships": {"guard": 50}}}
        changes = [{"target": "player.guard", "op": "add", "value": 10}]
        new_state, _ = mgr.apply_changes(state, changes)
        assert new_state["player"]["relationships"]["guard"] == 60

    def test_check_expirations(self):
        script = {
            "player_character": {"attributes": {}, "relationships": {}},
            "persistent_states": [
                {"id": "poisoned", "expires_at": "2024-01-15T10:00:00"},
                {"id": "blessed", "expires_at": "2024-01-16T00:00:00"},
            ],
        }
        mgr = StateManager(script)
        state = {"active_persistent_states": ["poisoned", "blessed"]}

        new_state, expired = mgr.check_expirations(state, "2024-01-15T12:00:00")
        assert "poisoned" in expired
        assert "blessed" not in expired
        assert "poisoned" not in new_state["active_persistent_states"]
        assert "blessed" in new_state["active_persistent_states"]

    def test_check_expirations_does_not_mutate_original(self):
        script = {
            "player_character": {"attributes": {}, "relationships": {}},
            "persistent_states": [
                {"id": "poisoned", "expires_at": "2024-01-15T10:00:00"},
            ],
        }
        mgr = StateManager(script)
        state = {"active_persistent_states": ["poisoned"]}

        new_state, _ = mgr.check_expirations(state, "2024-01-15T12:00:00")
        assert "poisoned" in state["active_persistent_states"]  # Original unchanged

    def test_reveal_location(self):
        mgr = self._make_manager()
        state = {"visible_locations": ["town"]}
        state = mgr.reveal_location(state, "forest")
        assert "forest" in state["visible_locations"]

    def test_reveal_location_no_duplicate(self):
        mgr = self._make_manager()
        state = {"visible_locations": ["town"]}
        state = mgr.reveal_location(state, "town")
        assert state["visible_locations"].count("town") == 1

    def test_apply_none_old_value_treated_as_zero(self):
        mgr = self._make_manager()
        state = {"player": {}}
        changes = [{"target": "player.score", "op": "add", "value": 10}]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["score"] == 10
        assert log[0]["old"] is None
        assert log[0]["new"] == 10

    def test_multiple_changes_applied_sequentially(self):
        mgr = self._make_manager()
        state = {"player": {"health": 50}}
        changes = [
            {"target": "player.health", "op": "add", "value": 20},
            {"target": "player.health", "op": "add", "value": -10},
        ]
        new_state, log = mgr.apply_changes(state, changes)
        assert new_state["player"]["health"] == 60
        assert log[0]["new"] == 70
        assert log[1]["old"] == 70
        assert log[1]["new"] == 60
