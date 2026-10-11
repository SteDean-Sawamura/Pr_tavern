import copy

import pytest

from api import game_routes
from engine.prompt_builder import PromptBuilder
from test_candidate_contacts import CONTACT, contact_feedback, consult_contacts
from test_scheduled_npc_overrides import make_session


def snapshot_session():
    session = make_session(conditional_schedule=True)
    session.script["npcs"][0]["schedule"][1]["condition"] = "script_variables.test_ready >= 2"
    return session


@pytest.mark.parametrize("current_ready,snapshot_ready", [(0, 2), (2, 0)])
@pytest.mark.parametrize("reader", ["session", "info", "prompt", "presence", "dialogue"])
def test_schedule_readers_use_supplied_state_without_rebinding_session(current_ready, snapshot_ready, reader):
    session = snapshot_session()
    session.current_state["script_variables"]["test_ready"] = current_ready
    snapshot = copy.deepcopy(session.current_state)
    snapshot["script_variables"]["test_ready"] = snapshot_ready
    expected = "test_desk" if snapshot_ready else "test_origin"
    original = session.current_state
    baseline = copy.deepcopy((original, snapshot, session.script))
    builder = session.prompt_builder
    if reader == "session":
        assert session._get_npc_location("test_guide", snapshot) == expected
    elif reader == "info":
        assert builder.get_npc_current_info("test_guide", snapshot["game_time"], state=snapshot)["location"] == expected
    elif reader == "prompt":
        assert builder._resolve_npc_location("test_guide", snapshot, snapshot["game_time"]) == expected
    elif reader == "presence":
        assert builder._get_present_npcs(snapshot, expected) == ["Practice guide"]
    else:
        assert f"当前位置: {expected}" in builder.build_npc_talk_prompt(snapshot, "test_guide")
    assert session.current_state is original
    assert (original, snapshot, session.script) == baseline


@pytest.mark.parametrize("current_ready,snapshot_ready", [(0, 2), (2, 0)])
def test_room_refresh_uses_snapshot_condition(current_ready, snapshot_ready):
    session = snapshot_session()
    schedule = session.script["npcs"][0]["schedule"]
    schedule[0]["activity"] = "在大厅练习"
    schedule[1].update(location="test_origin", activity="在会议室练习")
    session.current_state["script_variables"]["test_ready"] = current_ready
    session._ensure_rooms_initialized()
    snapshot = copy.deepcopy(session.current_state)
    snapshot["script_variables"]["test_ready"] = snapshot_ready
    baseline = copy.deepcopy(session.current_state)
    session._ensure_rooms_initialized(snapshot)
    expected = "会议室" if snapshot_ready else "大厅"
    assert snapshot["npcs"]["test_guide"]["current_room"] == expected
    assert snapshot["npcs"]["test_guide"]["_derived_room"] == expected
    assert session.current_state == baseline


@pytest.mark.parametrize("source", ["accepted_update", "scheduled_event"])
def test_contact_feedback_uses_conditional_schedule_after_candidate_changes(source):
    session = snapshot_session() if source == "accepted_update" else make_session(conditional_schedule=True)
    baseline = copy.deepcopy(session.current_state)
    context = {"old_time": baseline["game_time"], "new_time": "2030-01-01T09:30:00"}
    update = {**copy.deepcopy(CONTACT), "variable_changes": [{"id": "test_ready", "op": "set", "value": 2}]}
    if source == "scheduled_event":
        update["end_time"] = "2030-01-01T09:10:00"
    feedback = contact_feedback(session._handle_update_state(update, context))
    assert feedback["contacts"]["test_guide"] == {
        "requested_in_contact": True, "in_contact": False, "block_reason": "不在当前位置",
    }
    events, candidate = session._project_interaction_state(context)
    assert [event["event_id"] for event in events] == (["test_shift"] if source == "scheduled_event" else [])
    assert candidate["npcs"]["test_guide"]["current_location"] == "test_desk"
    assert consult_contacts(session, context, include_candidates=True)["contacts"]["test_guide"]["in_contact"] is False
    assert session.current_state == baseline


def test_public_historical_contact_does_not_use_live_schedule_conditions():
    session = snapshot_session()
    session._apply_npc_contact_changes(session.current_state, copy.deepcopy(CONTACT["npc_contact_changes"]))
    historical = copy.deepcopy(session.current_state)
    session.current_state["script_variables"]["test_ready"] = 2
    baseline = copy.deepcopy((session.current_state, historical))
    assert game_routes._enrich_state(session, session.current_state)["npc_contacts"] == {}
    public = game_routes._public_result({"state": historical}, session)["state"]
    assert public["npc_contacts"] == {"test_guide": True}
    assert public["npc_talk_block_reasons"] == {"test_guide": ""}
    assert (session.current_state, historical) == baseline


@pytest.mark.parametrize("ready,expired", [(0, False), (2, False), (2, True)])
def test_dialogue_uses_snapshot_override_conditions_and_expiry(ready, expired):
    session = make_session()
    snapshot = copy.deepcopy(session.current_state)
    snapshot["script_variables"]["test_ready"] = ready
    snapshot["game_time"] = "2030-01-01T09:30:00" if expired else "2030-01-01T09:10:00"
    snapshot["npc_schedule_overrides"] = {"test_guide": [{
        "time_range": "09:00-10:00", "location": "test_desk", "activity": "SYNTHETIC_OVERRIDE_ACTIVITY",
        "priority": 100, "condition": "test_ready >= 2", "expires": "2030-01-01T09:30:00",
    }]}
    baseline = copy.deepcopy((session.current_state, snapshot))
    builder = session.prompt_builder
    expected = "test_desk" if ready and not expired else "test_origin"
    info = builder.get_npc_current_info("test_guide", snapshot["game_time"], state=snapshot)
    assert info["location"] == expected
    prompt = builder.build_npc_talk_prompt(snapshot, "test_guide")
    assert f"当前位置: {expected}" in prompt
    assert ("SYNTHETIC_OVERRIDE_ACTIVITY" in prompt) is bool(ready and not expired)
    assert (session.current_state, snapshot) == baseline


def test_snapshot_schedule_preserves_explicit_position_and_room():
    session = snapshot_session()
    snapshot = copy.deepcopy(session.current_state)
    snapshot["script_variables"]["test_ready"] = 2
    snapshot["npcs"]["test_guide"].update(current_location="test_origin", current_room="practice-room")
    session._refresh_npc_locations(snapshot)
    session._ensure_rooms_initialized(snapshot)
    assert session._get_npc_location("test_guide", snapshot) == "test_origin"
    assert session.prompt_builder._resolve_npc_location("test_guide", snapshot, snapshot["game_time"]) == "test_origin"
    assert snapshot["npcs"]["test_guide"]["current_room"] == "practice-room"


def test_schedule_lookup_without_snapshot_preserves_current_and_custom_conditions():
    session = snapshot_session()
    builder = session.prompt_builder
    session.current_state["script_variables"]["test_ready"] = 2
    assert builder.get_npc_current_info("test_guide", session.current_state["game_time"])["location"] == "test_desk"
    assert builder._resolve_npc_location("test_guide", session.current_state, session.current_state["game_time"],
                                         condition_eval=lambda condition: False) == "test_origin"
    standalone = PromptBuilder(session.script)
    assert standalone.get_npc_current_info("test_guide", session.current_state["game_time"],
                                           state=session.current_state)["location"] == "test_origin"
