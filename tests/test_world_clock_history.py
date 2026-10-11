import copy

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


def make_session(case="minute", events=None):
    rest = {"time_range": "00:00-24:00", "location": "test_origin", "activity": "Practice rest"}
    shift = {"time_range": "09:10-09:30", "location": "test_desk", "activity": "Practice shift", "priority": 100}
    schedule = [rest, shift]
    if case in ("condition_false", "condition_true", "history"):
        shift["condition"] = "test_ready >= 1"
    if case == "priority":
        schedule.append({**shift, "time_range": "09:15-09:20", "location": "test_origin", "priority": 10})
    if case == "alias":
        shift["time"] = shift.pop("time_range")
    if case == "midnight":
        shift["time_range"] = "23:30-00:30"
    if case in ("explicit", "override_only"):
        schedule = [shift] if case == "explicit" else []
    if case == "persistent_expiry":
        shift["condition"] = "status.test_permit == active"
    script = {
        "script_id": "synthetic-clock", "name": "Synthetic clock", "settings": {},
        "start_time": "2030-01-01T23:00:00" if case == "midnight" else "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_origin"},
        "locations": [{"id": "test_origin", "name": "Origin"}, {"id": "test_desk", "name": "Desk"}],
        "npcs": [{"id": "test_guide", "name": "Practice guide", "default_location": "test_origin", "schedule": schedule}],
        "variables": [{"id": "test_ready", "type": "number", "default": int(case == "condition_true")}],
        "one_time_events": events or [],
        "persistent_states": [{"id": "test_permit", "name": "Practice permit", "expires_at": "2030-01-01T09:20:00"}],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    if case == "explicit":
        session.current_state["npcs"]["test_guide"]["current_location"] = "test_origin"
    if case == "dead":
        session.current_state["npcs"]["test_guide"]["alive"] = False
    if case == "persistent_expiry":
        session.current_state["active_persistent_states"] = ["test_permit"]
    if case == "override_only":
        session.current_state["npc_schedule_overrides"] = {"test_guide": [{
            **shift, "time_range": "09:10-10:00", "expires": "2030-01-01T09:25:00",
        }]}
    return session


async def settle(session, end_time="2030-01-01T09:40:00", regen=False):
    action = {"type": "freeform", "text": "Wait and inspect the practice area."}
    parsed = {"end_time": end_time, "narrative": "Time passes in the practice area.", "choices": []}
    try:
        if regen:
            original = copy.deepcopy((session.current_state, session.world_clock.snapshot()))
            state, _, _ = session._settle_regen_state(parsed, session.current_state, [])
            assert (session.current_state, session.world_clock.snapshot()) == original
            assert state["game_time"] == end_time
            return state
        context = session._prepare_turn(action)
        result = await session._apply_parsed_response(parsed, parsed["narrative"], action, context)
        assert result["state"]["game_time"] == end_time
        return result["state"]
    finally:
        await session._drain_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
async def test_clock_records_activity_changes_without_inventing_travel(regen):
    session = make_session()
    session.script["npcs"][0]["schedule"][1]["location"] = "test_origin"
    state = await settle(session, regen=regen)
    events = state["_pending_world_events"]
    assert [(event["previous_activity"], event["activity"]) for event in events] == [
        ("Practice rest", "Practice shift"), ("Practice shift", "Practice rest"),
    ]
    assert all(event["previous_location"] == event["location"] == "test_origin" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
async def test_same_time_events_are_observed_as_one_settled_boundary(regen):
    events = [{"id": event_id, "trigger_time": "2030-01-01T09:20:00", "effects": [
        {"type": "state_change", "target": "script_variables.test_ready", "op": "set", "value": value},
    ]} for event_id, value in [("test_enable", 1), ("test_disable", 0)]]
    session = make_session("history", events)
    state = await settle(session, regen=regen)
    assert set(state["fired_one_time_events"]) == {"test_enable", "test_disable"}
    assert transitions(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
async def test_day_end_and_settlement_endpoint_are_inclusive(regen):
    session = make_session("midnight")
    session.script["npcs"][0]["schedule"][1]["time_range"] = "23:30-24:00"
    state = await settle(session, "2030-01-02T00:00:00", regen)
    assert transitions(state) == [("2030-01-01T23:30:00", "test_desk"), ("2030-01-02T00:00:00", "test_origin")]


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
async def test_restored_dynamic_npc_uses_its_own_declared_boundaries(regen):
    session = make_session("override_only")
    session.current_state.pop("npc_schedule_overrides")
    session.current_state["npcs"]["test_dynamic"] = {
        "name": "Practice assistant", "default_location": "test_origin",
        "schedule": [{"time_range": "09:10-09:30", "location": "test_desk", "activity": "Practice shift"}],
    }
    session._index_dynamic_npcs_from_state(session.current_state)
    state = await settle(session, regen=regen)
    assert transitions(state) == [("2030-01-01T09:10:00", "test_desk"), ("2030-01-01T09:30:00", "test_origin")]
    assert all(event["npc_id"] == "test_dynamic" for event in state["_pending_world_events"])


def test_projection_does_not_produce_history_or_mutate_clock():
    session = make_session()
    clock = session.world_clock.snapshot()
    session._settle_timed_events(session.current_state["game_time"], "2030-01-01T09:40:00", [])
    assert "_pending_world_events" not in session.current_state
    assert session.world_clock.snapshot() == clock


def transitions(state):
    events = [event for event in state.get("_pending_world_events", []) if event["type"] != "world_passage"]
    assert all(event["type"] == "npc_schedule_change" for event in events)
    return [(event["time"], event["location"]) for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
@pytest.mark.parametrize("case", ["minute", "priority", "alias", "condition_false", "condition_true",
                                   "explicit", "dead", "override_only", "persistent_expiry", "midnight"])
async def test_clock_reports_only_resolved_changes_at_declared_boundaries(case, regen):
    session = make_session(case)
    end = "2030-01-02T00:40:00" if case == "midnight" else "2030-01-01T09:40:00"
    state = await settle(session, end, regen)
    if case in ("condition_false", "explicit", "dead"):
        expected = []
    elif case == "midnight":
        expected = [("2030-01-01T23:30:00", "test_desk"), ("2030-01-02T00:30:00", "test_origin")]
    else:
        end_minute = {"override_only": "25", "persistent_expiry": "20"}.get(case, "30")
        expected = [("2030-01-01T09:10:00", "test_desk"), (f"2030-01-01T09:{end_minute}:00", "test_origin")]
    assert transitions(state) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
@pytest.mark.parametrize("activation,reset", [("10", False), ("20", False), ("20", True)])
async def test_later_conditions_do_not_rewrite_earlier_schedule_history(activation, reset, regen):
    events = [{"id": "test_enable", "trigger_time": f"2030-01-01T09:{activation}:00", "effects": [
        {"type": "state_change", "target": "script_variables.test_ready", "op": "set", "value": 1},
    ]}]
    if reset:
        events.append({"id": "test_disable", "trigger_time": "2030-01-01T09:25:00", "effects": [
            {"type": "state_change", "target": "script_variables.test_ready", "op": "set", "value": 0},
        ]})
    session = make_session("history", events)
    state = await settle(session, regen=regen)
    end_minute = "25" if reset else "30"
    assert transitions(state) == [(f"2030-01-01T09:{activation}:00", "test_desk"),
                                   (f"2030-01-01T09:{end_minute}:00", "test_origin")]


@pytest.mark.asyncio
@pytest.mark.parametrize("regen", [False, True])
async def test_override_installation_and_expiry_are_observed_without_retroactive_start(regen):
    events = [{"id": "test_install", "trigger_time": "2030-01-01T09:20:00", "effects": [
        {"type": "schedule_override", "npc_id": "test_guide", "time_range": "09:00-10:00",
         "location": "test_desk", "activity": "Practice assignment", "priority": 200,
         "expires": "2030-01-01T09:25:00"},
    ]}]
    session = make_session("condition_false", events)
    state = await settle(session, regen=regen)
    assert transitions(state) == [("2030-01-01T09:20:00", "test_desk"), ("2030-01-01T09:25:00", "test_origin")]


@pytest.mark.asyncio
async def test_unverified_legacy_schedule_hints_are_not_presented_as_history():
    session = make_session("condition_false")
    session.current_state["_pending_world_events"] = [{
        "type": "npc_schedule_start", "npc_name": "Unverified guide", "activity": "Unverified shift", "location": "test_desk",
    }]
    try:
        action = {"type": "system", "text": "Inspect the practice area."}
        context = session._prepare_turn(action)
        prompt = session._build_unified_context(context, action)
        assert "Unverified shift" not in prompt
    finally:
        await session._drain_background_tasks()
