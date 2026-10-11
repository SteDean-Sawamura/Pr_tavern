import copy
import json

import pytest

from engine.game_session import GameSession


@pytest.fixture(params=["add_variable", "add_trigger", "update_trigger"])
def mechanics_script(request):
    variable = {"id": "gate", "type": "number", "default": 0, "min": 0, "max": 1}
    trigger = {
        "id": "gate_rule", "event": "configure_gate", "action": "set_var",
        "params": {"var_id": "gate", "op": "set", "value": 9},
    }
    effect = {"type": request.param}
    variables = [variable]
    triggers = []
    if request.param == "add_variable":
        variables = []
        effect["variable"] = {**variable, "default": 9}
    elif request.param == "add_trigger":
        effect["trigger"] = trigger
    else:
        triggers = [{**trigger, "enabled": False}]
        effect["update"] = {"id": "gate_rule", "enabled": True}
    script = {
        "script_id": "synthetic-mechanics-projection", "settings": {},
        "variables": variables, "triggers": triggers,
        "lorebook": [{
            "id": "gate_notice", "content": "The gate is open.", "enabled": False,
        }],
        "one_time_events": [{
            "id": "configure_gate", "trigger_time": "2030-01-01T09:05:00",
            "effects": [effect],
        }, {
            "id": "release_notice", "trigger_time": "2030-01-01T09:05:00",
            "condition": "script_variables.gate == 1",
            "effects": [{"type": "activate_lore", "id": "gate_notice"}],
        }],
    }
    return script


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_time", ["2030-01-01T09:05:00", "2030-01-01T09:06:00"])
async def test_projected_mechanics_unlock_the_same_lore_as_settlement(mechanics_script, notice_time):
    script = copy.deepcopy(mechanics_script)
    script["one_time_events"][1]["trigger_time"] = notice_time
    session = GameSession(copy.deepcopy(script), None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    original_state = session.current_state
    original_snapshot = copy.deepcopy(original_state)
    original_script = copy.deepcopy(session.script)
    original_definitions = copy.deepcopy(session._mechanics_definitions())
    original_variables = session.script_variables
    original_triggers = session.trigger_engine
    context = {
        "old_time": original_state["game_time"],
        "_accepted_state_updates": [{"end_time": notice_time}],
    }
    earlier = {**context, "_accepted_state_updates": [{"end_time": "2030-01-01T09:04:00"}]}
    not_yet_available = json.loads(await session._query_lorebook_for_turn(earlier, {"entry_id": "gate_notice"}))
    assert not_yet_available["entries"] == []
    projected = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "gate_notice"}))
    repeated = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "gate_notice"}))
    projected_events, projected_state = session._projected_events(context, with_state=True)
    assert repeated == projected
    assert session.current_state is original_state
    assert session.current_state == original_snapshot
    assert session.script == original_script
    assert session._mechanics_definitions() == original_definitions
    assert session.script_variables is original_variables
    assert session.trigger_engine is original_triggers
    current = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "gate_notice"}))
    assert current["entries"] == []
    fired = []
    session.current_state["game_time"] = notice_time
    session._settle_timed_events(context["old_time"], notice_time, fired)
    settled = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "gate_notice"}))
    assert [event["event_id"] for event in fired] == ["configure_gate", "release_notice"]
    assert session.current_state["script_variables"]["gate"] == 1
    assert [entry["content"] for entry in settled["entries"]] == ["The gate is open."]
    assert projected["entries"] == settled["entries"]
    assert projected["projection"]["provisional"] is True
    assert projected["projection"]["event_ids"] == [event["event_id"] for event in fired]
    assert [event["event_id"] for event in projected_events] == [event["event_id"] for event in fired]
    assert projected_state["script_variables"] == session.current_state["script_variables"]
    assert projected_state["mechanics_overrides"] == session.current_state["mechanics_overrides"]
    restored = GameSession(copy.deepcopy(script), None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._hydrate_branch_runtime(restored.current_state)
    recovered = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "gate_notice"}))
    assert recovered["entries"] == settled["entries"]
    root_id = restored.world_tree.add_node(
        None, original_snapshot["game_time"], 0, state_snapshot=copy.deepcopy(original_snapshot),
    )
    final_id = restored.world_tree.add_node(
        root_id, notice_time, 1, state_snapshot=copy.deepcopy(restored.current_state),
    )
    await restored.branch_to_node(root_id)
    assert restored._mechanics_definitions() == original_definitions
    previous = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "gate_notice"}))
    assert previous["entries"] == []
    await restored.branch_to_node(final_id)
    recovered = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "gate_notice"}))
    assert recovered["entries"] == settled["entries"]


def test_failed_projection_restores_mechanics_runtime(mechanics_script, monkeypatch):
    session = GameSession(copy.deepcopy(mechanics_script), None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    original_state = session.current_state
    original_snapshot = copy.deepcopy(original_state)
    original_script = copy.deepcopy(session.script)
    original_definitions = copy.deepcopy(session._mechanics_definitions())
    original_variables = session.script_variables
    original_triggers = session.trigger_engine

    def fail_after_effects(*args, **kwargs):
        raise RuntimeError("Synthetic tracker failure")

    monkeypatch.setattr(session.event_scheduler, "update_trackers", fail_after_effects)
    with pytest.raises(RuntimeError, match="Synthetic tracker failure"):
        session._projected_events({
            "old_time": original_state["game_time"],
            "_accepted_state_updates": [{"end_time": "2030-01-01T09:05:00"}],
        })
    assert session.current_state is original_state
    assert session.current_state == original_snapshot
    assert session.script == original_script
    assert session._mechanics_definitions() == original_definitions
    assert session.script_variables is original_variables
    assert session.trigger_engine is original_triggers
