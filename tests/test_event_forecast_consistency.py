import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_actor_goal_settlement import result_from
from test_story_choice_settlement import choice_client, stored_state
from test_timed_candidate_consistency import lore_entries, make_session as chain_session
from test_timed_condition_projection import timed_session
from test_wait_event_validation import END, FIRE, START, wait_session


def forecast(session, **arguments):
    return json.loads(session._handle_peek_upcoming_events({"hours_ahead": 1, **arguments}))


def from_script(script):
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    return session


@pytest.mark.parametrize("condition,allowed", [
    (f"game_time >= {FIRE}", True), ("game_time < 2030-01-01T09:04:00", False),
])
def test_forecast_checks_conditions_at_fire_time(condition, allowed):
    session = wait_session(2, condition=condition)
    before = copy.deepcopy(session.current_state)
    result = forecast(session)
    assert [event["id"] for event in result["events"]] == (["test_shift"] if allowed else [])
    assert [event["id"] for event in result["conditional_candidates"]] == ([] if allowed else ["test_shift"])
    assert result["total_matches"] == int(allowed)
    assert result["conditional_total_matches"] == int(not allowed)
    assert session.current_state == before


@pytest.mark.parametrize("source", ["effect", "trigger", "addition"])
@pytest.mark.parametrize("release_time", [FIRE, "2030-01-01T09:06:00"])
def test_forecast_follows_canonical_effect_chains_without_mutation(source, release_time):
    session = chain_session(source=source, release_time=release_time)
    session.vector_memory = Mock()
    state = session.current_state
    before = copy.deepcopy((state, session.script, lore_entries(session),
                            session._mechanics_definitions(), session.world_clock.snapshot()))
    result = forecast(session)
    assert [event["id"] for event in result["events"]] == ["test_configure", "test_release"]
    assert result["conditional_candidates"] == []
    canonical = chain_session(source=source, release_time=release_time)
    settled = []
    canonical._settle_timed_events(START, END, settled)
    assert [(event["id"], event["time"]) for event in result["events"]] == [
        (event["event_id"], event["fire_time"]) for event in settled
    ]
    assert session.current_state is state
    assert (state, session.script, lore_entries(session), session._mechanics_definitions(),
            session.world_clock.snapshot()) == before
    assert session.vector_memory.mock_calls == []


def test_forecast_preserves_causal_order_for_same_time_events():
    script = copy.deepcopy(chain_session().script)
    script["one_time_events"].reverse()
    result = forecast(from_script(script))
    assert [event["id"] for event in result["events"]] == ["test_configure", "test_release"]
    assert result["conditional_candidates"] == []


def test_earlier_effect_can_remove_a_currently_satisfied_event():
    script = copy.deepcopy(chain_session(release_time="2030-01-01T09:06:00").script)
    script["one_time_events"][1]["condition"] = "lorebook_enabled_overrides.test_gate == false"
    session = from_script(script)
    session._set_lore_enabled("test_gate", False)
    assert session._evaluate_condition(script["one_time_events"][1]["condition"])
    before = copy.deepcopy(session.current_state)
    result = forecast(session)
    assert [event["id"] for event in result["events"]] == ["test_configure"]
    assert [event["id"] for event in result["conditional_candidates"]] == ["test_release"]
    assert session.current_state == before


@pytest.mark.parametrize("release_time,in_second", [
    ("2030-01-02T09:00:00", False), ("2030-01-02T09:05:00", True),
])
def test_later_window_includes_prior_effects_without_repeating_boundary(release_time, in_second):
    session = chain_session(release_time=release_time)
    before = copy.deepcopy(session.current_state)
    first = forecast(session, hours_ahead=24)
    second = forecast(session, start_after_hours=24)
    assert [event["id"] for event in first["events"]] == ["test_configure", *([] if in_second else ["test_release"])]
    assert [event["id"] for event in second["events"]] == (["test_release"] if in_second else [])
    assert first["conditional_candidates"] == second["conditional_candidates"] == []
    assert session.current_state == before


def test_cyclic_forecast_paginates_past_scheduler_cap_with_per_occurrence_conditions():
    session = timed_session("game_time >= 2030-01-01T09:06:00", cyclic=True)
    before = copy.deepcopy(session.current_state)
    first = forecast(session, hours_ahead=24)
    last = forecast(session, hours_ahead=24, offset=1430)
    assert first["total_matches"] == last["total_matches"] == 1435
    assert [event["time"] for event in first["events"]] == [f"2030-01-01T09:{minute:02d}:00" for minute in range(6, 11)]
    assert [event["time"] for event in first["conditional_candidates"]] == [FIRE]
    assert first["conditional_total_matches"] == last["conditional_total_matches"] == 1
    assert first["truncated"] and not last["truncated"]
    assert len(last["events"]) == 5 and last["events"][-1]["time"] == "2030-01-02T09:00:00"
    assert last["conditional_candidates"] == []
    assert session.current_state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,threshold,fail_save", [(True, 3, False), (False, 8, False), (True, 8, True)])
async def test_forecast_drives_wait_undo_save_retry_and_branch_reload(tmp_path, monkeypatch, allowed, threshold, fail_save):
    clock_condition = f"game_time >= {FIRE}" if allowed else "game_time < 2030-01-01T09:04:00"
    session = wait_session(0, threshold=threshold, condition=f"test_ready >= {threshold} AND {clock_condition}")
    calls = []
    narratives = {
        True: ("A practice shift begins. You stop waiting as the activity changes in the room, "
               "check the clock, and leave your materials on the table while considering your next action."),
        False: ("You remain seated and complete the full hour of waiting. You check the clock, "
                "put your materials away, and leave the surrounding equipment undisturbed while considering your next action."),
    }

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments, step = "classify_intent", {"intent": "action"}, "intent"
        elif names == ["extract_notes"]:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            calls.append(copy.deepcopy(messages))
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith("forecast-check-")]
            step = len(completed)
            if step == 0:
                name, arguments = "submit_plan", {"plan": "Read the declaration and forecast the wait before committing it."}
            elif step == 1:
                name, arguments = "consult_rules", {"category": "one_time_events", "query": "test_shift"}
            elif step in (2, 4, 6, 8, 11):
                if step == 6:
                    assert completed[5].startswith("已撤销上一个工具调用（update_state）")
                if step == 11:
                    assert bool(json.loads(completed[10])["entries"]) is allowed
                name, arguments = "peek_upcoming_events", {"hours_ahead": 1}
            elif step in (3, 7):
                assert json.loads(completed[step - 1])["events"] == []
                entry, = json.loads(completed[1])["entries"]
                declaration = json.loads(entry["content"])["definition"]
                name, arguments = "update_state", copy.deepcopy(declaration["preparation"])
            elif step == 5:
                observed = json.loads(completed[4])
                assert bool(observed["events"]) is allowed
                name, arguments = "undo_my_last_action", {"reason": "Recheck the forecast without the preparation."}
            elif step == 9:
                observed = json.loads(completed[8])
                assert bool(observed["events"]) is allowed
                name, arguments = "update_state", {"end_time": observed["events"][0]["time"] if observed["events"] else END}
            elif step == 10:
                assert not completed[9].startswith("错误:"), completed[9]
                name, arguments = "query_lorebook", {"entry_id": "test_notice"}
            elif step == 12:
                observed = json.loads(completed[11])
                assert observed["events"] == observed["conditional_candidates"] == []
                assert observed["projection"]["through"] == (FIRE if allowed else END)
                name, arguments = "write_narrative", {"text": narratives[bool(json.loads(completed[10])["entries"])]}
            else:
                assert step == 13
                name, arguments = "finalize_turn", {"summary": "Completed the wait using the event forecast."}
            assert session.current_state["game_time"] == START
            assert not session.current_state.get("fired_one_time_events")
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"forecast-check-{step}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value=json.dumps({
        "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
        "skill_check": {"needed": False}, "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
        "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        "pass": True, "violations": [],
    })))
    session.ai_provider = provider
    expected_time = FIRE if allowed else END
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "调整准备工作，然后等待1小时，观察周围变化",
                  "action_id": "synthetic-forecast-check", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result):
                assert current.current_state["game_time"] == expected_time
                raise RuntimeError("Synthetic forecast save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response)
        assert result["narrative"] == narratives[allowed]
        state = await stored_state(session)
        assert session.current_state["game_time"] == state["game_time"] == expected_time
        assert state["script_variables"]["test_ready"] == threshold
        assert ("test_shift" in state.get("fired_one_time_events", [])) is allowed
        count = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == count
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["game_time"] == expected_time
        assert forecast(restored)["events"] == []
        await restored.branch_to_node(root)
        assert restored.current_state["game_time"] == START
        assert restored.current_state["script_variables"]["test_ready"] == 0
        assert forecast(restored)["events"] == []
        assert [event["id"] for event in forecast(restored)["conditional_candidates"]] == ["test_shift"]
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["game_time"] == expected_time
        assert forecast(restored)["events"] == []
        node = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert "SYNTHETIC_SHIFT_RULE_BODY" in json.dumps(calls)
        assert "SYNTHETIC_SHIFT_RULE_BODY" not in response.text + json.dumps(node["agent_trace"])
