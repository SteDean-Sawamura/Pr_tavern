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
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client, stored_state
from test_wait_event_validation import FIRE, START, wait_context, wait_session


END = "2030-01-01T09:15:00"
CLAIM = "The synthetic practice bell has rung."
DRAFT = CLAIM + " You put down your pencil and watch the clock beside the practice desk, recording the sound before closing the notebook."
SAFE = "You watch the clock beside the practice desk and keep your notebook open. The room stays quiet while you record the passing minutes and finish your observation."


def claim_session(mode="variable", *, cyclic=False):
    script = copy.deepcopy(wait_session(2).script)
    script.update(script_id="synthetic-event-narrative", name="Synthetic event narrative")
    event = script["one_time_events"][0]
    event.update(effects=[], narrative_claims=[CLAIM])
    if mode == "clock":
        event["condition"] = f"game_time < {FIRE}"
    if mode == "future":
        event["trigger_time"] = "2030-01-01T09:30:00"
    if cyclic:
        event["first_trigger"] = event.pop("trigger_time")
        event.update(frequency_value=5, frequency_unit="minute")
        script["cyclic_events"] = script.pop("one_time_events")
    if mode in ("chain", "dice", "unstaged_dice"):
        script.setdefault("one_time_events", []).insert(0, {
            "id": "test_cancel", "trigger_time": "2030-01-01T09:02:00",
            "description": "A synthetic preparation changes.",
        })
        if mode == "unstaged_dice":
            script["one_time_events"][0]["condition"] = "test_ready >= 3"
        if mode == "chain":
            script["triggers"] = [{
                "id": "test_cancel_rule", "event": "test_cancel", "action": "set_var",
                "params": {"var_id": "test_ready", "op": "set", "value": 0},
            }]
        else:
            script["random_items"] = [{
                "id": "test_cancel_roll", "trigger_type": "event_linked", "linked_event_id": "test_cancel",
                "dice": {"count": 1, "faces": 1}, "ranges": [{
                    "min": 1, "max": 1, "label": "Synthetic cancellation",
                    "state_changes": [{"target": "script_variables.test_ready", "op": "set", "value": 0}],
                }],
            }]
    session = GameSession(script, None, save_id="synthetic-event-narrative-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    return session


def proposal(mode):
    return {"end_time": END, **({"variable_changes": [
        {"id": "test_ready", "op": "set", "value": 3 if mode == "unstaged_dice" else 0},
    ]} if mode in ("variable", "unstaged_dice") else {})}


def claim_context(session, *, explicit=True):
    context = wait_context(session)
    context.update(new_time=END, requested_wait_minutes=None, estimated_minutes=15,
                   action_text="观察练习钟，记录变化", _finalize_narrative=DRAFT,
                   _narrative_timed_event_ids=["test_shift"] if explicit else [])
    context["triggered_events"] = session.event_scheduler.check_events(
        session.current_state, START, END, condition_eval=session._evaluate_condition,
    )
    return context


@pytest.mark.parametrize("mode", ["variable", "clock", "chain", "future"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("cyclic", [False, True])
def test_finalize_rejects_claims_not_supported_by_canonical_events(mode, explicit, cyclic):
    session = claim_session(mode, cyclic=cyclic)
    context = claim_context(session, explicit=explicit)
    before = copy.deepcopy((session.current_state, session.script))
    result = session._handle_finalize_turn(context, proposal(mode))
    assert result.startswith("{"), result
    feedback = json.loads(result)
    assert feedback.get("error") and {event["id"] for event in feedback["excluded_events"]} == {"test_shift"}
    assert context["_accepted_state_updates"] == []
    assert (session.current_state, session.script) == before
    context.update(_finalize_narrative=SAFE, _narrative_timed_event_ids=[])
    assert session._handle_finalize_turn(context, proposal(mode)).startswith("已同步完成")
    assert "test_shift" not in {event["event_id"] for event in session._projected_events(context)}
    assert (session.current_state, session.script) == before


@pytest.mark.parametrize("cyclic", [False, True])
def test_supported_and_historical_claims_remain_valid_without_mutation(cyclic):
    session = claim_session("allowed", cyclic=cyclic)
    context = claim_context(session)
    before = copy.deepcopy(session.current_state)
    assert session._handle_finalize_turn(context, {"end_time": END}).startswith("已同步完成")
    assert session.current_state == before
    fired = []
    session._settle_timed_events(START, END, fired)
    session.current_state["game_time"] = END
    context = {"old_time": END, "new_time": "2030-01-01T09:16:00", "triggered_events": []}
    assert session._timed_event_revision_feedback(context, DRAFT) == ""


def test_unknown_explicit_event_id_is_rejected():
    session = claim_session("allowed")
    context = claim_context(session)
    context["_narrative_timed_event_ids"] = ["test_missing"]
    feedback = session._timed_event_revision_feedback(context)
    assert feedback and json.loads(feedback)["excluded_events"] == [{"id": "test_missing"}]


def narrative_provider(session, mode, *, repair=True, safe_only=False):
    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        if "classify_intent" in names:
            name, arguments, step = "classify_intent", {"intent": "action"}, "intent"
        elif "extract_notes" in names:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith("claim-check-")]
            step = len(completed)
            if step == 0:
                name, arguments = "submit_plan", {"plan": "Read the practice event and record the observation."}
            elif step == 1:
                name, arguments = "consult_rules", {"category": "one_time_events", "query": "test_shift"}
            elif step == 2:
                name, arguments = (("update_state", proposal(mode)) if mode == "unstaged_dice"
                                   else ("peek_upcoming_events", {"hours_ahead": 1}))
            elif step == 3:
                entry, = json.loads(completed[1])["entries"]
                declaration = json.loads(entry["content"])["definition"]
                name, arguments = "write_narrative", {
                    "text": SAFE if safe_only else DRAFT,
                    "event_ids": [] if safe_only else [declaration["id"]],
                }
            elif step == 4:
                name, arguments = "finalize_turn", {**proposal(mode), "summary": "A synthetic observation."}
            elif step == 5:
                assert repair and not safe_only, completed
                feedback = json.loads(completed[4])
                assert feedback.get("error")
                assert {event["id"] for event in feedback["excluded_events"]} == {"test_shift"}
                name, arguments = "write_narrative", {"text": SAFE, "event_ids": []}
            else:
                assert step == 6
                name, arguments = "finalize_turn", {**proposal(mode), "summary": "A corrected observation."}
            assert session.current_state["game_time"] == START
            assert not session.current_state.get("fired_one_time_events")
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"claim-check-{step}", "name": name, "arguments": arguments}]}

    return SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value=json.dumps({
        "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
        "skill_check": {"needed": False}, "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
        "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        "pass": True, "violations": [],
    })))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fail_save", [("variable", False), ("clock", False), ("future", False), ("chain", True), ("dice", True), ("unstaged_dice", True)])
async def test_harness_rewrites_invalid_event_claims_before_stream_and_persistence(tmp_path, monkeypatch, mode, fail_save):
    session = claim_session(mode)
    provider = narrative_provider(session, mode)
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-claim-check", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert result["narrative"] == SAFE
                raise RuntimeError("Synthetic narrative save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
            assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
            assert DRAFT not in failed.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response)
        assert result["narrative"] == SAFE and DRAFT not in response.text
        assert any(event["type"] == "text" and event["content"] == SAFE for event in _stream_events(response.text))
        state = await stored_state(session)
        assert state["game_time"] == END and "test_shift" not in state.get("fired_one_time_events", [])
        if mode in ("dice", "unstaged_dice"):
            assert state["script_variables"]["test_ready"] == 0
            assert state["fired_one_time_events"] == ["test_cancel"]
        count = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == count
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["game_time"] == END
        await restored.branch_to_node(root)
        assert restored.current_state["game_time"] == START and restored.current_state["script_variables"]["test_ready"] == 2
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["game_time"] == END
        node = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert node["ai_response"] == SAFE


@pytest.mark.asyncio
async def test_missing_preparation_still_rejects_at_actual_settlement_without_saving_claim(tmp_path, monkeypatch):
    session = claim_session("unstaged_dice")
    session.ai_provider = narrative_provider(session, "unstaged_dice", repair=False)
    monkeypatch.setattr(session, "_prepare_unstaged_event_dice", lambda context: [])
    prepare = session._prepare_turn

    def prepare_unstaged(action):
        context = prepare(action)
        assert context["estimated_minutes"] == 15
        assert [event["event_id"] for event in context["conditional_event_candidates"]] == ["test_cancel"]
        assert context["candidate_event_dice_state"] == {}
        assert context["candidate_event_dice_results"] == {}
        return context

    monkeypatch.setattr(session, "_prepare_turn", prepare_unstaged)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-dice-claim", "expected_node_id": root}
        failed = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        assert '"type": "error"' in failed.text and '"type": "final"' not in failed.text
        assert "叙事与实际事件或时间结算不符" in failed.text
        assert DRAFT not in failed.text
        assert not [event for event in _stream_events(failed.text) if event["type"] in ("text", "narrative_revised")]
        assert session.current_state == before and await stored_state(session) == baseline
        assert session.world_tree.active_node_id == root
        session.ai_provider = narrative_provider(session, "unstaged_dice", safe_only=True)
        result = result_from(await asyncio.wait_for(client.post(endpoint, json=action), 15))
        assert result["narrative"] == SAFE
        state = await stored_state(session)
        assert state["script_variables"]["test_ready"] == 0
        assert state["fired_one_time_events"] == ["test_cancel"]
        baseline = await stored_state(session)
        node_before = copy.deepcopy(session.world_tree.get_node(result["node_id"]))
        session.ai_provider = narrative_provider(session, "unstaged_dice", repair=False)
        regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 15)
        assert regenerated.status_code == 400 and regenerated.json().get("detail"), regenerated.text
        assert "叙事与实际事件或时间结算不符" in regenerated.json()["detail"]
        assert await stored_state(session) == baseline
        assert session.world_tree.get_node(result["node_id"]) == node_before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dice", "unstaged_dice"])
async def test_full_regeneration_rewrites_from_staged_dice_and_restores_swipe(tmp_path, monkeypatch, mode):
    session = claim_session(mode)
    roll = Mock(wraps=session.dice.roll_and_resolve)
    monkeypatch.setattr(session.dice, "roll_and_resolve", roll)
    session.ai_provider = narrative_provider(session, mode)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        result = result_from(await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-staged-regen",
            "expected_node_id": root,
        }), 15))
        assert result["narrative"] == SAFE
        assert roll.call_count == 1
        before = copy.deepcopy(session.current_state)
        provider = narrative_provider(session, mode)
        session.ai_provider = provider
        regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 15)
        assert regenerated.status_code == 200, regenerated.text
        payload = regenerated.json()
        assert payload["narrative"] == SAFE and payload["total_swipes"] == 2
        assert roll.call_count == 2
        assert payload["swipe_index"] == 1
        assert provider.generate_with_tools.await_count >= 7
        assert session.current_state["script_variables"] == before["script_variables"]
        assert session.current_state["fired_one_time_events"] == ["test_cancel"]
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_ready"] == 0
        node = restored.world_tree.get_node(result["node_id"])
        assert node["active_swipe_index"] == 1 and len(node["swipes"]) == 2
        assert node["ai_response"] == SAFE
        await restored.branch_to_node(root)
        assert restored.current_state["script_variables"]["test_ready"] == 2
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["script_variables"]["test_ready"] == 0


@pytest.mark.asyncio
async def test_supported_event_text_waits_for_actual_settlement_and_keeps_stream_metrics(tmp_path, monkeypatch):
    session = claim_session("allowed")
    session.ai_provider = narrative_provider(session, "allowed", repair=False)
    original = session.process_action_stream
    observations = []

    async def observe(action, **kwargs):
        async for event in original(action, **kwargs):
            if event["type"] in ("text", "narrative_revised") and event.get("content") == DRAFT:
                observations.append(copy.deepcopy(session.current_state))
            yield event

    monkeypatch.setattr(session, "process_action_stream", observe)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "观察练习钟，记录变化", "action_id": "synthetic-allowed-claim",
            "expected_node_id": session.world_tree.active_node_id,
        }), 15)
        result = result_from(response)
        assert result["narrative"] == DRAFT
        assert len(observations) == 1
        assert observations[0]["game_time"] == END
        assert observations[0]["fired_one_time_events"] == ["test_shift"]
        assert result["agent_trace"]["first_text_ms"] is not None
        assert result["agent_trace"]["first_text_active_ms"] is not None
