import copy

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


def make_session():
    script = {
        "script_id": "synthetic-autonomy", "name": "Synthetic autonomy", "settings": {},
        "start_time": "2030-01-01T09:15:00",
        "player_character": {"name": "Visitor", "initial_location": "test_origin"},
        "locations": [{"id": "test_origin", "name": "Origin"}, {"id": "test_desk", "name": "Desk"}],
        "npcs": [{"id": "test_guide", "name": "Practice guide", "default_location": "test_origin",
                  "schedule": [{"time_range": "09:00-10:00", "location": "test_origin", "activity": "Practice rest"}]}],
        "variables": [{"id": "test_ready", "type": "number", "default": 0}],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("case,location", [
    ("minute", "test_origin"), ("midnight", "test_desk"), ("priority", "test_desk"),
    ("condition_false", "test_origin"), ("condition_true", "test_desk"),
    ("override", "test_desk"), ("override_only", "test_desk"), ("expired", "test_origin"),
    ("explicit", "test_origin"), ("derived", "test_desk"), ("dead", ""),
])
async def test_prepare_autonomy_matches_authoritative_schedule(case, location):
    session = make_session()
    state = session.current_state
    npc = session.script["npcs"][0]
    shift = {"time_range": "09:00-10:00", "location": "test_desk", "activity": "Practice shift", "priority": 100}
    if case == "minute":
        npc["schedule"] = [{**shift, "time_range": "09:30-10:00"}]
    elif case == "midnight":
        npc["schedule"] = [{**shift, "time_range": "23:30-01:30"}]
        state["game_time"] = "2030-01-02T00:15:00"
    elif case in ("override", "override_only", "expired"):
        state["npc_schedule_overrides"] = {"test_guide": [{
            **shift, "condition": "test_ready >= 2",
            "expires": state["game_time"] if case == "expired" else "2030-01-01T09:30:00",
        }]}
        state["script_variables"]["test_ready"] = 2
        if case == "override_only":
            npc["schedule"] = []
    elif case == "condition_false":
        npc["schedule"].insert(0, {**shift, "condition": "test_ready >= 2"})
    elif case == "priority":
        npc["schedule"].append(shift)
    else:
        npc["schedule"].append({**shift, "condition": "test_ready >= 2"})
        state["script_variables"]["test_ready"] = 2
        if case in ("explicit", "derived"):
            npc["schedule"] = [shift]
            state["npcs"]["test_guide"]["current_location"] = "test_origin"
            if case == "derived":
                state["npcs"]["test_guide"]["_derived_location"] = "test_origin"
        if case == "dead":
            state["npcs"]["test_guide"]["alive"] = False
    session.npc_autonomy.npc_states["test_guide"] = {"location": "test_origin", "mood": "calm"}
    assert session._get_npc_location("test_guide") == location
    try:
        context = session._prepare_turn({"type": "system", "text": "Inspect the current schedule."})
        status = session.npc_autonomy.get_npc_status("test_guide")
        if not location:
            assert status == {} and context["npc_autonomy_events"] == []
        else:
            assert status["location"] == location
            assert status["last_updated"] == state["game_time"] and status["mood"] == "calm"
            if case == "explicit":
                assert status["activity"] == "空闲"
            assert [event["to"] for event in context["npc_autonomy_events"]] == ([location] if location != "test_origin" else [])
        assert context["candidate_npc_autonomy_events"] == []
        if context["npc_autonomy_events"]:
            prompt = session._build_unified_context(context, {"type": "system", "text": "Inspect."})
            assert "位置由test_origin变为test_desk" in prompt
            assert "不代表目击移动或主角知情" in prompt
    finally:
        await session._drain_background_tasks()


@pytest.mark.parametrize("current_ready,snapshot_ready", [(0, 2), (2, 0)])
def test_autonomy_reads_the_supplied_snapshot_without_rebinding_session(current_ready, snapshot_ready):
    session = make_session()
    session.script["npcs"][0]["schedule"].append({
        "time_range": "09:00-10:00", "location": "test_desk", "priority": 100,
        "condition": "test_ready >= 2", "activity": "Practice shift",
    })
    session.current_state["script_variables"]["test_ready"] = current_ready
    snapshot = copy.deepcopy(session.current_state)
    snapshot["script_variables"]["test_ready"] = snapshot_ready
    original = session.current_state
    baseline = copy.deepcopy((original, snapshot, session.npc_autonomy.snapshot()))
    candidate = copy.deepcopy(session.npc_autonomy)
    assert candidate.tick(snapshot["game_time"], snapshot, resolve_npc=session._get_npc_autonomy_info) == []
    status = candidate.get_npc_status("test_guide")
    assert status["location"] == ("test_desk" if snapshot_ready else "test_origin")
    assert session.current_state is original
    assert (original, snapshot, session.npc_autonomy.snapshot()) == baseline


@pytest.mark.asyncio
async def test_initial_observation_is_not_a_movement_and_estimated_time_is_only_a_candidate():
    session = make_session()
    session.script["npcs"][0]["schedule"] = [{
        "time_range": "09:30-10:00", "location": "test_desk", "activity": "Practice shift",
    }]
    action = {"type": "freeform", "text": "Wait 30 minutes."}
    try:
        context = session._prepare_turn(action)
        assert context["npc_autonomy_events"] == []
        event, = context["candidate_npc_autonomy_events"]
        assert (event["from"], event["to"], event["activity"]) == ("test_origin", "test_desk", "Practice shift")
        assert session.npc_autonomy.get_npc_status("test_guide")["location"] == "test_origin"
        assert session.current_state["game_time"] == "2030-01-01T09:15:00"
        prompt = session._build_unified_context(context, action)
        assert "<npc_movements>" not in prompt
        assert "<candidate_npc_movements>" in prompt and "可能移动到test_desk" in prompt
        assert "并非已发生或主角已知" in prompt
        assert session.npc_autonomy.get_npc_status("test_guide")["location"] == "test_origin"
    finally:
        await session._drain_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["branch", "undo"])
@pytest.mark.parametrize("snapshot_present", [False, True])
async def test_returning_to_root_discards_future_autonomy(operation, snapshot_present):
    session = make_session()
    baseline = copy.deepcopy(session.current_state)
    if snapshot_present:
        baseline["_npc_autonomy"] = {"npc_states": {}}
    root_id = session.world_tree.add_node(None, baseline["game_time"], 0, state_snapshot=baseline)
    session.npc_autonomy.npc_states["test_guide"] = {
        "location": "test_desk", "activity": "Practice shift", "mood": "calm",
        "last_updated": "2030-01-01T09:30:00",
    }
    future = copy.deepcopy(baseline)
    future["game_time"] = "2030-01-01T09:30:00"
    future["_npc_autonomy"] = copy.deepcopy(session.npc_autonomy.snapshot())
    session.world_tree.add_node(root_id, future["game_time"], 1, state_snapshot=future)
    session.current_state = copy.deepcopy(future)
    try:
        if operation == "branch":
            result = await session.branch_to_node(root_id)
        else:
            result = await session.undo_last_turn()
        assert result["node_id"] == root_id
        assert session.npc_autonomy.snapshot() == {"npc_states": {}}
        context = session._prepare_turn({"type": "system", "text": "Inspect the original position."})
        assert context["npc_autonomy_events"] == []
        assert session.npc_autonomy.get_npc_status("test_guide")["location"] == "test_origin"
    finally:
        await session._drain_background_tasks()
