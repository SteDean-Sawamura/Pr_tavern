import asyncio
from collections import OrderedDict
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from api import config_routes, game_routes
from db import database
from engine.event_engine import EventEngine
from engine.game_session import GameSession
from engine.story_tree import StoryTreeEngine


def make_session(threshold=7):
    script = {
        "script_id": "synthetic-auto-choice", "name": "Synthetic auto choice", "settings": {},
        "variables": [
            {"id": "test_gate", "type": "number", "default": 0},
            {"id": "test_enabled", "type": "boolean", "default": True},
            {"id": "test_progress", "type": "number", "default": 0},
        ],
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "lorebook": [{"id": "test_detail", "name": "Synthetic detail", "enabled": False,
                      "content": "SYNTHETIC_AUTO_CHOICE_DETAIL", "keys": ["shelf"]}],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [
            {"id": "test_decision", "name": "Synthetic decision", "type": "choice", "auto_resolve": True,
             "condition": "script_variables.test_enabled == true",
             "rule_notes": "SYNTHETIC_AUTO_CHOICE_RULE",
             "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 1}]},
             "choices": [
                 {"id": "test_invalid", "condition": "test_gate == 0 OR"},
                 {"id": "test_closed", "condition": "test_gate < 0"},
                 {"id": "test_open", "condition": f"script_variables.test_gate >= {threshold}",
                  "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 3}],
                              "activate_lore": ["test_detail"]}, "unlock": ["test_followup"]},
             ]},
            {"id": "test_followup", "name": "Synthetic followup", "type": "quest", "requires": ["test_decision"]},
        ]}]},
    }
    session = GameSession(script, None)
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    return session


def make_engine(session, kind):
    script = copy.deepcopy(session.script)
    if kind == "story":
        return StoryTreeEngine(script["story_tree"])
    return EventEngine(script, shadow_story_nodes=kind == "shadow")


def evaluate(engine, session, turn):
    method = engine.evaluate if isinstance(engine, StoryTreeEngine) else engine.tick
    return method(session.current_state, turn, condition_eval=session._evaluate_condition, advance_timed=False)


@pytest.mark.parametrize("kind", ["story", "event", "shadow"])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("next_turn", [0, 1])
def test_waiting_auto_choice_rechecks_conditions_without_reactivation(kind, restore, next_turn):
    session = make_session()
    engine = make_engine(session, kind)
    first = evaluate(engine, session, 0)
    assert [node["id"] for node in first.newly_active] == ["test_decision"]
    assert not first.newly_completed and not first.effects and not first.notifications
    still_waiting = evaluate(engine, session, next_turn)
    assert not still_waiting.newly_active and not still_waiting.newly_completed
    assert not still_waiting.effects and not still_waiting.notifications
    if restore:
        session.current_state = copy.deepcopy(session.current_state)
        engine = make_engine(session, kind)
    session.current_state["script_variables"].update(test_gate=7, test_enabled=False)
    blocked = evaluate(engine, session, next_turn)
    assert not blocked.newly_completed and not blocked.effects
    session.current_state["script_variables"]["test_enabled"] = True
    resolved = evaluate(engine, session, next_turn)
    assert [node["id"] for node in resolved.newly_completed] == ["test_decision"]
    assert [node["id"] for node in resolved.newly_active] == ["test_followup"]
    if kind == "shadow":
        assert not resolved.effects and not resolved.lore_activations
    else:
        assert resolved.effects == [
            {"action": "set_var", "params": {"var_id": "test_progress", "op": "inc", "value": amount}}
            for amount in (1, 3)
        ]
        assert resolved.lore_activations == ["test_detail"]
    if kind == "story":
        state = session.current_state["story_tree_state"]
        assert state["choices_made"] == {"test_decision": "test_open"}
        assert "test_decision" not in state["active"]
    else:
        state = session.current_state["events"]["test_decision"]
        assert state["choices_made"] == "test_open"
        assert state["status"] == "completed"
    for turn in (next_turn, next_turn + 1):
        repeated = evaluate(engine, session, turn)
        assert not repeated.newly_active and not repeated.newly_completed
        assert not repeated.effects and not repeated.lore_activations and not repeated.notifications


@pytest.mark.parametrize("kind", ["story", "event", "shadow"])
@pytest.mark.parametrize("node_type", ["choice", "quest"])
def test_manual_choices_and_quests_stay_active_without_repeated_prompts(kind, node_type):
    session = make_session()
    node = session.script["story_tree"]["trees"][0]["nodes"][0]
    node.update(type=node_type, auto_resolve=False)
    engine = make_engine(session, kind)
    first = evaluate(engine, session, 0)
    assert [entry["id"] for entry in first.newly_active] == ["test_decision"]
    assert bool(first.notifications) is (node_type == "choice" and kind != "shadow")
    session.current_state["script_variables"]["test_gate"] = 7
    for turn in (0, 1):
        result = evaluate(engine, session, turn)
        assert not result.newly_active and not result.newly_completed
        assert not result.effects and not result.notifications


def stream_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


def assert_choice_state(session, *, completed, gate):
    state = session.current_state
    assert state["script_variables"]["test_gate"] == gate
    assert state["script_variables"]["test_progress"] == (4 if completed else 0)
    story = state["story_tree_state"]
    assert ("test_decision" in story["active"]) is not completed
    assert ("test_decision" in story["completed"]) is completed
    assert story["choices_made"].get("test_decision") == ("test_open" if completed else None)
    assert ("test_followup" in story["active"]) is completed
    entry = next(entry for entry in session.prompt_builder.lorebook.entries if entry.id == "test_detail")
    assert entry.enabled is completed


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_mode", ["cached", "reload", "branch", "failed"])
@pytest.mark.parametrize("threshold", [7, 13])
@pytest.mark.parametrize("undo", [False, True])
async def test_agent_rule_update_resumes_auto_choice_and_persists_once(tmp_path, monkeypatch, resume_mode, threshold, undo):
    session = make_session(threshold)
    script = session.script
    session.save_id = "synthetic-auto-choice-save"
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    current = session
    sessions = [session]
    requests = []
    turn_index = 0
    completed_before = False
    gate_before = 0

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        requests.append(copy.deepcopy(messages))
        index = len(requests) - 1
        end_time = f"2030-01-01T09:0{turn_index + 1}:00"
        operations = [
            ("submit_plan", {"plan": "Inspect the shelf according to the declared decision rules."}),
            ("consult_rules", {"category": "story_nodes", "query": "test_decision"}),
            ("update_state", {"end_time": end_time}),
        ]
        if undo and turn_index == 1:
            operations.extend([
                ("undo_my_last_action", {"reason": "Defer the proposed decision until the next inspection."}),
                ("update_state", {"end_time": end_time}),
            ])
        operations.extend([
            ("consult_rules", {"category": "variables", "include_candidates": True}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Inspected the empty shelf."}),
        ])
        assert index < len(operations)
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        assert_choice_state(current, completed=completed_before, gate=gate_before)
        if index == 2:
            rules, = [json.loads(message["content"]) for message in messages
                      if message.get("tool_call_id") == "synthetic-decision"]
            definition = json.loads(rules["entries"][0]["content"])["definition"]["node"]
            variable_path, minimum = definition["choices"][-1]["condition"].split(" >= ")
            arguments["variable_changes"] = [{
                "id": variable_path.removeprefix("script_variables."), "op": "set",
                "value": 1 if turn_index == 0 else int(minimum),
            }]
        if name == "write_narrative":
            candidates, = [json.loads(message["content"]) for message in messages
                           if message.get("tool_call_id") == "synthetic-candidates"]
            values = {payload["definition"]["id"]: payload
                      for payload in (json.loads(entry["content"]) for entry in candidates["entries"])}
            assert values["test_gate"]["current_value"] == gate_before
            assert values["test_gate"]["candidate_value"] == (1 if turn_index == 0 or (undo and turn_index == 1) else threshold)
            assert values["test_progress"]["current_value"] == (4 if completed_before else 0)
            assert values["test_progress"]["candidate_value"] == (4 if completed_before else 0)
            assert candidates["projection"]["excludes_unsettled_events"] is True
            lore, = [message["content"] for message in messages if message.get("tool_call_id") == "synthetic-detail"]
            assert ("SYNTHETIC_AUTO_CHOICE_DETAIL" in lore) is completed_before
        call_id = "synthetic-decision" if index == 1 else (
            "synthetic-candidates" if name == "consult_rules" else f"synthetic-step-{index}"
        )
        calls = [{"id": call_id, "name": name, "arguments": arguments}]
        if call_id == "synthetic-candidates":
            calls.append({"id": "synthetic-detail", "name": "query_lorebook", "arguments": {"entry_id": "test_detail"}})
        return {"content": "", "tool_calls": calls}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, root_state["game_time"], 0, state_snapshot=root_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "auto-choice.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    endpoint = f"/api/game/{session.save_id}/action/stream"
    await database.init_db()
    waiting_id = None
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": narrative})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for turn_index in range(4):
                requests.clear()
                action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                          "stream_id": f"synthetic-choice-stream-{turn_index}", "action_id": f"synthetic-choice-action-{turn_index}",
                          "expected_node_id": current.world_tree.active_node_id}
                if resume_mode == "failed" and turn_index == (2 if undo else 1):
                    before_failure = copy.deepcopy(current.current_state)
                    original_apply = current._apply_story_tree_result
                    interrupted = []

                    def fail_after_resolution(result):
                        original_apply(result)
                        if any(node["id"] == "test_decision" for node in result.newly_completed):
                            interrupted.append(copy.deepcopy(current.current_state))
                            raise RuntimeError("Synthetic interruption after decision effects")

                    monkeypatch.setattr(current, "_apply_story_tree_result", fail_after_resolution)
                    try:
                        failure = await asyncio.wait_for(client.post(endpoint, json={
                            **action, "action_id": "synthetic-choice-failure", "stream_id": "synthetic-choice-failure",
                        }), 5)
                    finally:
                        monkeypatch.setattr(current, "_apply_story_tree_result", original_apply)
                    assert len(interrupted) == 1
                    assert interrupted[0]["script_variables"]["test_progress"] == 4
                    assert any(event["type"] == "error" for event in stream_events(failure.text))
                    assert current.current_state == before_failure
                    assert_choice_state(current, completed=False, gate=gate_before)
                    await current._drain_background_tasks()
                    requests.clear()
                response = await asyncio.wait_for(client.post(endpoint, json=action), 5)
                assert response.status_code == 200
                events = stream_events(response.text)
                assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                completed_before = turn_index >= (2 if undo else 1)
                gate_before = threshold if completed_before else 1
                assert_choice_state(current, completed=completed_before, gate=gate_before)
                if turn_index == 0:
                    waiting_id = final["node_id"]
                snapshot = current.world_tree.get_node(final["node_id"])
                assert snapshot["state_snapshot"]["script_variables"]["test_progress"] == (4 if completed_before else 0)
                assert snapshot["state_snapshot"] is not current.current_state
                assert snapshot["_state_hash"] == current.world_tree.state_hash(snapshot["state_snapshot"])
                assert snapshot["agent_trace"]
                for marker in ("SYNTHETIC_AUTO_CHOICE_RULE", "SYNTHETIC_AUTO_CHOICE_DETAIL"):
                    assert marker not in response.text
                    assert marker not in json.dumps(snapshot["agent_trace"])
                assert "SYNTHETIC_AUTO_CHOICE_RULE" in json.dumps(requests)
                await current._drain_background_tasks()
                model_calls = provider.generate_with_tools.await_count
                state = copy.deepcopy(current.current_state)
                replay = await client.post(endpoint, json=action)
                assert stream_events(replay.text) == events
                assert provider.generate_with_tools.await_count == model_calls
                assert current.current_state == state
                if resume_mode == "cached" and completed_before:
                    await current.branch_to_node(waiting_id)
                    assert_choice_state(current, completed=False, gate=1)
                    await current.branch_to_node(final["node_id"])
                    assert_choice_state(current, completed=completed_before, gate=gate_before)
                if resume_mode in {"reload", "branch"} or turn_index == 3:
                    game_routes._sessions.clear()
                    current = await game_routes._restore_session(session.save_id)
                    assert current is not None and current is not sessions[-1]
                    sessions.append(current)
                    game_routes._sessions[session.save_id] = current
                    assert_choice_state(current, completed=completed_before, gate=gate_before)
                    if resume_mode == "branch":
                        await current.branch_to_node(waiting_id)
                        assert_choice_state(current, completed=False, gate=1)
                        await current.branch_to_node(final["node_id"])
                        assert_choice_state(current, completed=completed_before, gate=gate_before)
    finally:
        for active in sessions:
            await active._drain_background_tasks()
        await database.close_pool()
