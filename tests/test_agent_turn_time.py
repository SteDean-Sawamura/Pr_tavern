import copy

import pytest

from engine.game_session import GameSession


@pytest.fixture
def session():
    session = GameSession({
        "script_id": "synthetic-turn-time", "settings": {},
        "variables": [{"id": "test_tick", "type": "number", "default": 0}],
        "one_time_events": [{
            "id": "test_future", "trigger_time": "2030-01-01T09:01:00",
            "description": "A synthetic scheduled change.",
            "effects": [{"type": "state_change", "target": "script_variables.test_tick", "value": 1}],
        }],
    }, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


def context(session):
    return {"old_time": session.current_state["game_time"], "new_time": "2030-01-01T09:05:00",
            "estimated_minutes": 5, "action_text": "Keep the current decision unchanged."}


def test_same_timestamp_is_valid_without_wait_or_travel(session):
    before = copy.deepcopy(session.current_state)
    assert session._validate_end_time(before["game_time"], context(session)) == ""
    assert session.current_state == before


def test_time_cannot_move_backwards(session):
    assert session._validate_end_time("2030-01-01T08:59:59", context(session))


def test_elapsed_time_still_fires_the_declared_event(session):
    events = []
    session._settle_timed_events(session.current_state["game_time"], "2030-01-01T09:01:00", events)
    assert [event["event_id"] for event in events] == ["test_future"]
    assert session.current_state["script_variables"]["test_tick"] == 1


@pytest.mark.parametrize("minutes", [5, 60])
def test_same_timestamp_cannot_bypass_requested_wait(session, minutes):
    pending = {**context(session), "requested_wait_minutes": minutes}
    error = session._validate_end_time(pending["old_time"], pending)
    assert "玩家要求等待" in error


def test_same_timestamp_cannot_bypass_travel(session, monkeypatch):
    monkeypatch.setattr(session, "_travel_minutes_for_updates", lambda *_args: (12, "test_destination"))
    error = session._validate_end_time(session.current_state["game_time"], context(session), "test_destination")
    assert "至少需要 12 分钟旅途" in error


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["update_state", "finalize_turn"])
async def test_zero_duration_stays_provisional_and_does_not_fire_future_events(session, tool):
    before = copy.deepcopy(session.current_state)
    pending = {**context(session), "_finalize_narrative": "You keep the current decision unchanged."}
    arguments = {"end_time": before["game_time"]}
    result = await session._dispatch_unified_tool(pending, tool, arguments)
    assert not result.startswith("错误:"), result
    assert session.current_state == before
    parsed = session._build_unified_parsed([{"name": tool, "args": arguments, "result": result}],
                                           pending, {"skip_choices": True})
    assert parsed["end_time"] == before["game_time"]
    events = []
    session._settle_timed_events(before["game_time"], parsed["end_time"], events)
    assert events == []
    assert session.current_state["script_variables"]["test_tick"] == 0


def test_player_input_policy_keeps_confirmations_scoped_and_resolved(session):
    inputs = [{"message": "Use the second option.", "plan_reply_context": {
        "plan": "Choose one procedure.", "questions": [{"question": "Which procedure?", "options": ["A", "B"]}],
        "reply": "Use the second option.",
    }}, {"message": "Do not change any other setting."}]
    rendered = session.prompt_builder.format_turn_player_inputs(inputs)
    assert "已明确回答的原问题视为已解决" in rendered
    assert "不要重复要求同一确认" in rendered
    assert "不代表玩家同意整个计划或未确认部分" in rendered
    assert "新问题仍需等待新确认" in rendered
    assert rendered.index("Use the second option.") < rendered.index("Do not change any other setting.")


def test_review_policy_distinguishes_unconfirmed_and_confirmed_decisions(session):
    _, policy = session.prompt_builder.build_narrative_review_prompt("You proceed.", "Proceed.", [])
    assert "confirmation_regression" in policy
    assert "不改变目标、风险或资源承诺的必要动作细节" in policy
    assert "已明确确认的执行范围" in policy


def test_loaded_temporal_prompt_allows_zero_time_without_waiving_costs(session):
    messages, policy = session.prompt_builder.build_world_state_temporal_prompt(
        "You keep the current decision unchanged.", "Keep the current decision.", session.current_state,
    )
    assert session.current_state["game_time"] in messages[0]["content"]
    assert "没有发生耗时行为时可等于当前时间" in policy
    assert "实际等待、旅途及剧本声明的成本仍须满足" in policy
