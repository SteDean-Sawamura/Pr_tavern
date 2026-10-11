import copy
import json

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


START = "2030-01-01T09:00:00"
FIRE = "2030-01-01T09:05:00"
END = "2030-01-01T09:10:00"
WINDOW = f"game_time >= {FIRE} AND game_time < 2030-01-01T09:06:00"


def timed_session(condition=WINDOW, *, cyclic=False, trigger=False):
    event = {
        "id": "clock_notice", "description": "A synthetic clock notice.",
        "condition": "" if trigger else condition,
        "effects": [] if trigger else [{"type": "activate_lore", "id": "clock_rule"}],
    }
    if cyclic:
        event.update(first_trigger=FIRE, frequency_value=1, frequency_unit="minute")
    else:
        event["trigger_time"] = FIRE
    script = {
        "script_id": "synthetic-clock-condition", "name": "Synthetic clock condition",
        "settings": {}, "start_time": START,
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Test room"}],
        "lorebook": [{"id": "clock_rule", "enabled": False, "constant": True,
                      "content": "SYNTHETIC_CLOCK_RULE"}],
        "cyclic_events" if cyclic else "one_time_events": [event],
        "triggers": [{
            "id": "clock_gate", "event": "clock_notice", "condition": condition,
            "action": "activate_lore", "params": {"entry_id": "clock_rule"},
        }] if trigger else [],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.event_engine.tick(session.current_state, 0, game_time=START, old_time=START)
    return session


async def observe(session, mode, end_time=END):
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": end_time}]}
    if mode == "projection":
        original = session.current_state
        snapshot = copy.deepcopy(original)
        lore = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "clock_rule"}))
        events, state = session._projected_events(context, with_state=True)
        assert session.current_state is original
        assert session.current_state == snapshot
        assert state["game_time"] == end_time
    else:
        initial_clock = START if mode == "settlement_start_clock" else end_time
        session.current_state["game_time"] = initial_clock
        events = []
        session._settle_timed_events(START, end_time, events)
        assert session.current_state["game_time"] == initial_clock
        session.current_state["game_time"] = end_time
        lore = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "clock_rule"}))
        state = session.current_state
    return events, state, lore


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["projection", "settlement_start_clock", "settlement_end_clock"])
@pytest.mark.parametrize("trigger", [False, True])
@pytest.mark.parametrize("condition,expected", [
    (WINDOW, True), ("game_time < 2030-01-01T09:06:00", True),
    ("game_time >= 2030-01-01T09:06:00", False),
])
async def test_scheduled_conditions_use_the_event_time(mode, trigger, condition, expected):
    session = timed_session(condition, trigger=trigger)
    events, state, lore = await observe(session, mode)
    assert [entry["content"] for entry in lore["entries"]] == (["SYNTHETIC_CLOCK_RULE"] if expected else [])
    assert [event["fire_time"] for event in events] == ([FIRE] if expected or trigger else [])
    assert ("clock_notice" in state.get("fired_one_time_events", [])) is (expected or trigger)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["projection", "settlement_start_clock", "settlement_end_clock"])
async def test_cyclic_time_window_is_checked_for_each_tick(mode):
    session = timed_session(
        "game_time >= 2030-01-01T09:06:00 AND game_time < 2030-01-01T09:08:00", cyclic=True,
    )
    events, state, lore = await observe(session, mode, "2030-01-01T09:08:00")
    assert [event["fire_time"] for event in events] == ["2030-01-01T09:06:00", "2030-01-01T09:07:00"]
    assert [entry["content"] for entry in lore["entries"]] == ["SYNTHETIC_CLOCK_RULE"]
    assert state["events"]["clock_notice"]["next_fire"] == "2030-01-01T09:09:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["projection", "settlement_start_clock", "settlement_end_clock"])
async def test_later_boundary_cannot_satisfy_an_earlier_event_time_condition(mode):
    session = timed_session("game_time >= 2030-01-01T09:06:00")
    script = copy.deepcopy(session.script)
    script["one_time_events"].append({"id": "later_tick", "trigger_time": "2030-01-01T09:06:00"})
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    events, state, lore = await observe(session, mode)
    assert [event["event_id"] for event in events] == ["later_tick"]
    assert state["fired_one_time_events"] == ["later_tick"]
    assert lore["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["projection", "settlement_start_clock", "settlement_end_clock"])
async def test_time_conditions_keep_same_instant_variable_unlocks(mode):
    script = copy.deepcopy(timed_session(f"({WINDOW}) AND script_variables.clock_gate == 1").script)
    script["variables"] = [{"id": "clock_gate", "type": "number", "default": 0}]
    script["one_time_events"].append({
        "id": "configure_clock", "trigger_time": FIRE, "condition": WINDOW,
        "effects": [{"type": "state_change", "target": "script_variables.clock_gate", "value": 1}],
    })
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    events, state, lore = await observe(session, mode)
    assert [event["event_id"] for event in events] == ["configure_clock", "clock_notice"]
    assert state["script_variables"]["clock_gate"] == 1
    assert [entry["content"] for entry in lore["entries"]] == ["SYNTHETIC_CLOCK_RULE"]


@pytest.mark.parametrize("initial_clock", [START, END])
@pytest.mark.parametrize("activation", ["event.clock_notice", "clock_signal"])
@pytest.mark.parametrize("condition,expected", [(WINDOW, True), (f"game_time > {FIRE}", False)])
def test_event_linked_story_unlocks_use_the_event_time(initial_clock, activation, condition, expected):
    script = copy.deepcopy(timed_session("").script)
    script["one_time_events"][0]["fire_events"] = ["clock_signal"]
    script["story_tree"] = {"trees": [{"id": "clock_tree", "nodes": [{
        "id": "clock_branch", "type": "auto", "activate_events": [activation], "condition": condition,
    }]}]}
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["game_time"] = initial_clock
    session._settle_timed_events(START, END, [])
    assert ("clock_branch" in session.current_state["story_tree_state"]["unlocked"]) is expected
    assert session.current_state["game_time"] == initial_clock


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [False, True])
@pytest.mark.parametrize("condition,expected", [(WINDOW, True), (f"game_time > {FIRE}", False)])
async def test_regeneration_preserves_timed_lore_and_restores_the_current_branch(trigger, condition, expected):
    session = timed_session(condition, trigger=trigger)
    original = session.current_state
    snapshot = copy.deepcopy(original)
    state, _, fired = session._settle_regen_state({"end_time": END}, snapshot, [])
    assert session.current_state is original and session.current_state == snapshot
    current = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "clock_rule"}))
    assert current["entries"] == []
    assert fired == (["clock_notice"] if expected or trigger else [])
    assert state["game_time"] == END
    restored = GameSession(copy.deepcopy(session.script), None)
    restored.current_state = json.loads(json.dumps(state))
    restored._hydrate_branch_runtime(restored.current_state)
    recovered = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "clock_rule"}))
    assert [entry["content"] for entry in recovered["entries"]] == (["SYNTHETIC_CLOCK_RULE"] if expected else [])


@pytest.mark.parametrize("explicit_state", [False, True])
def test_timed_evaluation_does_not_mutate_either_clock_even_on_failure(monkeypatch, explicit_state):
    session = timed_session()
    original = session.current_state
    snapshot = copy.deepcopy(original)
    supplied = {"game_time": END, "player": {"gold": 7}}
    provided = copy.deepcopy(supplied)

    def reject(condition, state):
        assert condition == WINDOW
        assert state is not original and state is not supplied
        assert state == {**(supplied if explicit_state else original), "game_time": FIRE}
        raise RuntimeError("Synthetic condition interruption")

    monkeypatch.setattr(session, "_evaluate_condition", reject)
    with pytest.raises(RuntimeError, match="Synthetic condition interruption"):
        session._evaluate_condition_at_time(FIRE, WINDOW, supplied if explicit_state else None)
    assert session.current_state is original and session.current_state == snapshot
    assert supplied == provided
