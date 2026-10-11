import asyncio
import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


def _stream_events(text):
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic_events", [False, True])
async def test_cyclic_lore_projection_matches_stream_settlement_after_reload(tmp_path, monkeypatch, dynamic_events):
    script = {
        "script_id": "synthetic-cyclic-harness", "name": "Synthetic cyclic harness", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "variables": [{"id": "test_gate", "type": "number", "default": 0}],
        "lorebook": [{"id": "test_notice", "enabled": False, "content": "SYNTHETIC_CYCLIC_NOTICE"}],
        "cyclic_events": [{
            "id": "test_tick", "first_trigger": "2030-01-01T09:05:00",
            "frequency_value": 1, "frequency_unit": "minute",
            "condition": "script_variables.test_gate == 1",
            "description": "Synthetic scheduled tick.", "rule_notes": "SYNTHETIC_CYCLIC_RULE",
            "effects": [{"type": "activate_lore", "id": "test_notice"}],
        }],
        "one_time_events": [{
            "id": "test_configure", "trigger_time": "2030-01-01T09:05:00",
            "description": "Synthetic configuration event.", "rule_notes": "SYNTHETIC_ONETIME_RULE",
            "effects": [{"type": "add_trigger", "trigger": {
                "id": "test_open_gate", "event": "test_configure", "action": "set_var",
                "params": {"var_id": "test_gate", "op": "set", "value": 1},
            }}],
        }],
        "persistent_states": [{"id": "test_condition", "description": "SYNTHETIC_PERSISTENT_RULE"}],
    }
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = [[], []]
    turn_index = 0

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [
                {"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}},
            ]}
        requests[turn_index].append(copy.deepcopy(messages))
        index = len(requests[turn_index]) - 1
        operations = [
            ("submit_plan", {"plan": "Inspect the shelf while waiting for the next scheduled tick."}),
            ("update_state", {"end_time": f"2030-01-01T09:0{turn_index + 5}:00"}),
            ("query_lorebook", {"entry_id": "test_notice"}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Inspected the shelf."}),
        ]
        assert index < len(operations)
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        calls = [{
            "id": f"synthetic-cycle-{turn_index}-{index}", "name": name, "arguments": arguments,
        }]
        if index == 1:
            calls.append({
                "id": f"synthetic-preview-{turn_index}", "name": "peek_upcoming_events", "arguments": {"hours_ahead": 1},
            })
            calls.extend({
                "id": f"synthetic-rule-{turn_index}-{category}", "name": "consult_rules",
                "arguments": {"category": category, "query": script[category][0]["id"]},
            } for category in ("one_time_events", "cyclic_events", "persistent_states"))
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
    stored_script = copy.deepcopy(script)
    if dynamic_events:
        stored_script.pop("one_time_events")
        stored_script.pop("cyclic_events")
    session = GameSession(stored_script, provider, save_id="synthetic-cyclic-save")
    session.current_state = ScriptLoader.create_initial_state(stored_script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    root_id = session.world_tree.add_node(
        None, script["start_time"], 0, ai_response="You are in the reading room.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    root_state = copy.deepcopy(session.current_state)
    if dynamic_events:
        session._apply_director_results({"events": {
            "one_time": script["one_time_events"], "cyclic": script["cyclic_events"],
        }})
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "cyclic-harness.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    endpoint = f"/api/game/{session.save_id}/action/stream"
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(stored_script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": root_state, "narrative": "You are in the reading room.",
        })
        current = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            for turn_index in range(2):
                action = {
                    "type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                    "stream_id": f"synthetic-cycle-stream-{turn_index}",
                    "action_id": f"synthetic-cycle-action-{turn_index}",
                    "expected_node_id": current.world_tree.active_node_id,
                }
                response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                events = _stream_events(response.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert final["state"]["game_time"] == f"2030-01-01T09:0{turn_index + 5}:00"
                assert current.current_state["script_variables"]["test_gate"] == 1
                assert current.current_state["events"]["test_tick"]["next_fire"] == f"2030-01-01T09:0{turn_index + 6}:00"
                settled = json.loads(await current._run_tool_native_async("query_lorebook", {"entry_id": "test_notice"}))
                assert bool(settled["entries"]) is bool(turn_index)
                received = [json.loads(message["content"]) for message in requests[turn_index][3]
                            if message.get("tool_call_id") == f"synthetic-cycle-{turn_index}-2"]
                assert len(received) == 1
                assert received[0]["entries"] == settled["entries"]
                assert received[0]["projection"]["event_ids"] == (["test_tick"] if turn_index else ["test_configure"])
                assert ("SYNTHETIC_CYCLIC_NOTICE" in json.dumps(requests[turn_index][3])) is bool(turn_index)
                preview, = [json.loads(message["content"]) for message in requests[turn_index][2]
                            if message.get("tool_call_id") == f"synthetic-preview-{turn_index}"]
                assert "SYNTHETIC_CYCLIC_RULE" not in json.dumps(preview)
                assert "SYNTHETIC_ONETIME_RULE" not in json.dumps(preview)
                for category in ("one_time_events", "cyclic_events", "persistent_states"):
                    rules, = [json.loads(message["content"]) for message in requests[turn_index][2]
                              if message.get("tool_call_id") == f"synthetic-rule-{turn_index}-{category}"]
                    assert rules["source"] == "current_branch"
                    assert rules["game_time"] == f"2030-01-01T09:0{5 if turn_index else 0}:00"
                    assert json.loads(rules["entries"][0]["content"])["definition"] == script[category][0]
                for marker in ("SYNTHETIC_CYCLIC_RULE", "SYNTHETIC_ONETIME_RULE", "SYNTHETIC_PERSISTENT_RULE"):
                    assert marker in json.dumps(requests[turn_index][2])
                    assert marker not in response.text
                assert sum(record["tool"] == "consult_rules" for record in current._agent_traces[-1]["rounds"]) == 3
                final_snapshot = copy.deepcopy(current.current_state)
                model_calls = provider.generate_with_tools.await_count
                replay = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
                assert replay.text == response.text
                assert provider.generate_with_tools.await_count == model_calls
                assert current.current_state == final_snapshot
                await current._drain_background_tasks()
                game_routes._sessions.clear()
                restored = await game_routes._restore_session(session.save_id)
                assert restored is not None and restored is not current
                sessions.append(restored)
                assert restored.current_state["events"]["test_tick"] == final_snapshot["events"]["test_tick"]
                recovered = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_notice"}))
                assert recovered["entries"] == settled["entries"]
                if dynamic_events:
                    final_id = restored.world_tree.active_node_id
                    await restored.branch_to_node(root_id)
                    for category in ("one_time_events", "cyclic_events"):
                        rules = json.loads(await restored._dispatch_unified_tool({}, "consult_rules", {"category": category}))
                        assert rules["entries"] == []
                        assert script[category][0]["id"] not in restored.event_engine.events
                    await restored.branch_to_node(final_id)
                    for category in ("one_time_events", "cyclic_events"):
                        rules = json.loads(await restored._dispatch_unified_tool({}, "consult_rules", {"category": category}))
                        assert json.loads(rules["entries"][0]["content"])["definition"] == script[category][0]
                    assert restored.current_state["events"]["test_tick"] == final_snapshot["events"]["test_tick"]
                game_routes._sessions[session.save_id] = restored
                current = restored
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
        assert row[0] == 3
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_mode", ["cached", "reload", "branch", "failed"])
async def test_condition_lifecycle_reaches_next_turn_agent(tmp_path, monkeypatch, resume_mode):
    guidance = "SYNTHETIC_NEXT_TURN_GUIDANCE"
    notice = "SYNTHETIC_UNSOURCED_NOTICE"
    script = {
        "script_id": "synthetic-lifecycle", "name": "Synthetic lifecycle", "settings": {},
        "visibility_policy": {"require_notification_source": True},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "variables": [
            {"id": "test_prepared", "type": "number", "default": 0},
            {"id": "test_settled", "type": "number", "default": 0},
        ],
        "one_time_events": [{
            "id": "test_enable_hooks", "trigger_time": "2030-01-01T09:01:00",
            "effects": [{"type": "add_trigger", "trigger": {
                "id": "test_prepare_hook", "condition": "event.before_generation.fired",
                "actions": [
                    {"type": "set_var", "var_id": "test_prepared", "op": "inc", "value": 1},
                    {"type": "inject_prompt", "text": guidance + " {{var::test_prepared}}"},
                    {"type": "notify", "message": notice},
                ],
            }}, {"type": "add_trigger", "trigger": {
                "id": "test_settle_hook", "condition": "event.after_ai.fired",
                "actions": [
                    {"type": "set_var", "var_id": "test_settled", "op": "inc", "value": 1},
                    {"type": "notify", "message": notice},
                ],
            }}],
        }],
    }
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = [[], []]
    turn_index = 0

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [
                {"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}},
            ]}
        requests[turn_index].append([{"role": "system", "content": kwargs["system"]}, *copy.deepcopy(messages)])
        prefix = f"synthetic-lifecycle-{turn_index}-"
        completed = [int(message["tool_call_id"].removeprefix(prefix))
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith(prefix)]
        index = max(completed, default=-1) + 1
        operations = [
            ("submit_plan", {"plan": "Inspect the shelf, following the active script rules."}),
            ("consult_rules", {"category": "variables", "query": "test_prepared"}),
            ("update_state", {"end_time": f"2030-01-01T09:0{turn_index + 1}:00"}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Inspected the empty shelf."}),
        ]
        assert index < len(operations)
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{"id": f"{prefix}{index}", "name": name, "arguments": arguments}]}

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
    session = GameSession(script, provider, save_id="synthetic-lifecycle-save")
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    root_id = session.world_tree.add_node(
        None, session.current_state["game_time"], 0, ai_response="You are in the reading room.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "lifecycle.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    endpoint = f"/api/game/{session.save_id}/action/stream"
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": session.current_state, "narrative": "You are in the reading room.",
        })
        current = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            for turn_index in range(2):
                action = {
                    "type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                    "stream_id": f"synthetic-lifecycle-stream-{turn_index}",
                    "action_id": f"synthetic-lifecycle-action-{turn_index}",
                    "expected_node_id": current.world_tree.active_node_id,
                }
                if turn_index == 1 and resume_mode == "failed":
                    before_failure = copy.deepcopy(current.current_state)
                    provider.generate_with_tools.side_effect = RuntimeError("Synthetic generation failure")
                    failed = await asyncio.wait_for(client.post(endpoint, json={
                        **action, "stream_id": "synthetic-failed-stream", "action_id": "synthetic-failed-action",
                    }), timeout=5)
                    provider.generate_with_tools.side_effect = generate_with_tools
                    failed_events = _stream_events(failed.text)
                    assert any(event["type"] == "error" for event in failed_events)
                    assert not any(event["type"] == "final" for event in failed_events)
                    assert current.world_tree.active_node_id == action["expected_node_id"]
                    assert current.current_state == before_failure
                response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                events = _stream_events(response.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert current.current_state["script_variables"] == {
                    "test_prepared": turn_index, "test_settled": turn_index + 1,
                }
                assert guidance not in response.text and notice not in response.text
                assert guidance not in json.dumps(requests[turn_index][0], ensure_ascii=False)
                context = json.dumps(requests[turn_index][1], ensure_ascii=False)
                assert (guidance in context) is bool(turn_index)
                if turn_index:
                    assert "<turn_instructions>" in context
                    assert guidance + " 1" in context
                rule_messages = [message for request in requests[turn_index] for message in request
                                 if message.get("tool_call_id") == f"synthetic-lifecycle-{turn_index}-1"]
                rules = json.loads(rule_messages[0]["content"])
                assert rules["source"] == "current_branch"
                assert json.loads(rules["entries"][0]["content"])["current_value"] == turn_index
                await current._drain_background_tasks()
                calls = provider.generate_with_tools.await_count
                state = copy.deepcopy(current.current_state)
                replay = await client.post(endpoint, json=action)
                assert _stream_events(replay.text) == events
                assert provider.generate_with_tools.await_count == calls
                assert current.current_state == state
                if turn_index == 0 and resume_mode in {"reload", "branch"}:
                    await current._drain_background_tasks()
                    game_routes._sessions.clear()
                    current = await game_routes._restore_session(session.save_id)
                    assert current is not None and current is not session
                    sessions.append(current)
                    game_routes._sessions[session.save_id] = current
                    if resume_mode == "branch":
                        await current.branch_to_node(root_id)
                        assert current.current_state["script_variables"] == {"test_prepared": 0, "test_settled": 0}
                        assert not current.trigger_engine.triggers
                        await current.branch_to_node(final["node_id"])
                    assert current.current_state["script_variables"] == {"test_prepared": 0, "test_settled": 1}
                    assert len(current.trigger_engine.triggers) == 2
        await current._drain_background_tasks()
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not current
        assert restored.current_state["script_variables"] == {"test_prepared": 1, "test_settled": 2}
        assert restored.world_tree.active_node_id == final["node_id"]
        callbacks = copy.deepcopy(restored.current_state["narrative_callbacks"])
        assert guidance in json.dumps(callbacks)
        game_routes._sessions[session.save_id] = restored
        public_state = await game_routes.get_state(session.save_id)
        public_node = await game_routes.get_tree_node(session.save_id, final["node_id"])
        assert guidance not in json.dumps([public_state, public_node])
        assert notice not in json.dumps([public_state, public_node])
        assert restored.current_state["narrative_callbacks"] == callbacks
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
        assert row[0] == 3
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("lore_effect", ["activate_lore", "add_lore", "update_lore"])
@pytest.mark.parametrize("compact_history", [False, True])
@pytest.mark.parametrize("interjection_phase", ["model", "tool"])
@pytest.mark.parametrize("chained_mechanics", [False, True])
async def test_retrieval_settlement_stream_and_reload(
    tmp_path, monkeypatch, lore_effect, compact_history, interjection_phase, chained_mechanics,
):
    script = {
        "script_id": "synthetic-harness", "name": "Synthetic harness", "settings": {},
        "locations": [
            {"id": "test_atrium", "name": "Atrium"},
            {"id": "test_archive", "name": "Archive", "travel_time": "PT5M"},
        ],
        "npcs": [{"id": "test_librarian", "name": "Librarian", "default_location": "test_atrium"}],
        "persistent_states": [
            {"id": "test_departure", "name": "Before arrival"},
            {"id": "test_arrived", "name": "After arrival"},
        ],
        "variables": [{
            "id": "test_capacity", "type": "number", "default": 4, "min": 0, "max": 9,
            "description": "SYNTHETIC_MECHANICS_PRIVATE",
        }],
        "lorebook": [
            {"id": "test_label", "keys": ["shelf label"], "enabled": False,
             "visibility": "world", "content": "The shelf label reads LANTERN."},
            {"id": "test_future", "keys": ["later"], "enabled": False,
             "visibility": "secret", "content": "SYNTHETIC_UNREAD_DETAILS"},
            {"id": "test_callback", "keys": ["callback"], "enabled": False,
             "visibility": "secret", "content": "SYNTHETIC_LIFECYCLE_DETAILS"},
        ],
        "one_time_events": [{
            "id": "test_arrival", "trigger_time": "2030-01-01T09:05:00",
            "condition": "player.location == test_archive AND script_variables.test_capacity == 7",
            "effects": [{"type": "activate_lore", "id": "test_label"}],
        }],
    }
    if lore_effect != "activate_lore":
        revealed_entry = {**script["lorebook"][0], "enabled": True}
        if lore_effect == "add_lore":
            script["lorebook"] = script["lorebook"][1:]
            effect = {"type": "add_lore", "entry": revealed_entry}
        else:
            script["lorebook"][0].update({"enabled": True, "content": "The shelf label is blank."})
            effect = {"type": "update_lore", **revealed_entry}
        script["one_time_events"][0]["effects"] = [effect]
    script["one_time_events"][0]["effects"].extend([
        {"type": "add_variable", "variable": {
            "id": "test_visit_count", "type": "number", "default": 2, "min": 0, "max": 10,
        }},
        {"type": "add_trigger", "trigger": {
            "id": "test_visit_hook", "event": "test_visit_tick", "action": "set_var",
            "params": {"var_id": "test_visit_count", "op": "inc", "value": 1},
        }},
        {"type": "add_random_item", "item": {
            "id": "test_visit_roll", "dice": "1d1", "trigger_type": "per_turn",
        }},
    ])
    schedule_params = {
        "npc_id": "test_librarian", "time_range": "09:00-10:00", "location": "test_archive",
        "activity": "Inspect the shelves.", "priority": 80, "expires": "2030-01-02T00:00:00",
    }
    for action_name, params in [
        ("reveal_lore", {"entry_id": "test_callback"}),
        ("schedule_override", schedule_params),
        ("activate_persistent_state", {"target": "test_arrived"}),
        ("deactivate_persistent_state", {"target": "test_departure"}),
    ]:
        script["one_time_events"][0]["effects"].append({"type": "add_trigger", "trigger": {
            "id": f"test_after_{action_name}", "event": "after_ai", "action": action_name, "params": params,
        }})

    expected_events = ["test_arrival"]
    if chained_mechanics:
        arrival_effects = script["one_time_events"][0]["effects"]
        label_effect = arrival_effects.pop(0)
        arrival_effects.extend([
            {"type": "add_variable", "variable": {
                "id": "test_gate", "type": "number", "default": 0, "min": 0, "max": 1,
            }},
            {"type": "add_trigger", "trigger": {
                "id": "test_gate_hook", "event": "test_arrival", "action": "set_var",
                "params": {"var_id": "test_gate", "op": "set", "value": 1},
            }},
        ])
        script["one_time_events"].append({
            "id": "test_label_ready", "trigger_time": "2030-01-01T09:05:00",
            "condition": "script_variables.test_gate == 1 AND script_variables.test_visit_count == 2",
            "effects": [label_effect],
        })
        expected_events.append("test_label_ready")

    def assert_lifecycle_state(current, *, settled):
        state = current.current_state
        assert state.get("active_persistent_states") == ["test_arrived" if settled else "test_departure"]
        overrides = {"test_librarian": [{key: value for key, value in schedule_params.items() if key != "npc_id"}]}
        assert state.get("npc_schedule_overrides", {}) == (overrides if settled else {})
        assert current.prompt_builder._resolve_npc_location("test_librarian", state, state["game_time"]) == (
            "test_archive" if settled else "test_atrium"
        )
        entry = next(entry for entry in current.prompt_builder.lorebook.entries if entry.id == "test_callback")
        assert entry.enabled is settled
        assert "test_callback" not in state.get("pc_discovered_lore", [])

    narrative = (
        "You leave the Atrium and reach the Archive after five minutes. "
        "Inside, you inspect the shelf without disturbing its contents. "
        "The shelf label reads LANTERN. You make a note of the label and remain in the room."
    )
    operations = [
        ("submit_plan", {"plan": "Choose a known room, then inspect its contents.",
                         "questions": [{"question": "Which room?", "options": ["Archive", "Atrium"]}]}),
        ("consult_rules", {"category": "variables", "query": "test_capacity"}),
        ("update_state", {"variable_changes": [{"id": "test_capacity", "op": "dec", "value": 1}]}),
        ("undo_my_last_action", {"reason": "Synthetic correction before settlement."}),
        ("update_state", {"variable_changes": [{"id": "test_capacity", "op": "inc", "value": 2}]}),
        ("update_state", {"variable_changes": [{"id": "test_visit_count", "op": "inc", "value": 1}]}),
        ("undo_my_last_action", {"reason": "Discard the rejected variable request only."}),
        ("update_state", {"location_change": "test_archive", "end_time": "2030-01-01T09:05:00",
                          "variable_changes": [{"id": "test_capacity", "op": "inc", "value": 1}]}),
        ("consult_rules", {"category": "variables", "query": "test_capacity"}),
        ("consult_rules", {"category": "variables", "query": "test_visit_count"}),
        ("query_lorebook", {"entry_id": "test_label"}),
        ("undo_my_last_action", {"reason": "Reconsider the candidate arrival after checking its evidence."}),
        ("query_lorebook", {"entry_id": "test_label"}),
        ("query_lorebook", {"entry_id": "test_label"}),
        ("update_state", {"location_change": "test_archive", "end_time": "2030-01-01T09:05:00",
                          "variable_changes": [{"id": "test_capacity", "op": "inc", "value": 1}]}),
        ("query_lorebook", {"entry_id": "test_label"}),
        ("write_narrative", {"text": narrative}),
        ("finalize_turn", {"summary": "Reached the Archive and read the shelf label.",
                           "variable_changes": [{"id": "test_capacity", "op": "inc", "value": 0}]}),
    ]
    narrative_call_index = len(operations) - 2
    if interjection_phase == "tool":
        operations.extend([
            ("write_narrative", {"text": narrative}), copy.deepcopy(operations[-1]),
        ])
    requests = []
    waiting = asyncio.Event()
    retrieved = asyncio.Event()
    release_narrative = asyncio.Event()

    async def generate_with_tools(messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        completed = [int(message["tool_call_id"].removeprefix("synthetic-call-"))
                     for message in messages if message.get("role") == "tool"
                     and message.get("tool_call_id", "").startswith("synthetic-call-")]
        index = max(completed, default=-1) + 1
        assert index < len(operations), json.dumps(messages[-4:], ensure_ascii=False)
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        if name == "write_narrative" and interjection_phase == "model":
            retrieved.set()
            await release_narrative.wait()
        content = "Synthetic prior context. " * 1600 if compact_history and index in (2, 5, 8, 12, 14) else ""
        tool_calls = [{
            "id": f"synthetic-call-{index}", "name": name, "arguments": arguments,
        }]
        if index == narrative_call_index and interjection_phase == "tool":
            tool_calls.append({
                "id": "stale-decision", "name": "finalize_turn",
                "arguments": {"summary": "Outdated decision", "variable_changes": [
                    {"id": "test_capacity", "op": "inc", "value": 1},
                ]},
            })
        return {"content": content, "tool_calls": tool_calls}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location", "clue"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session = GameSession(script, provider, save_id="synthetic-harness-save")
    review_narrative = AsyncMock(wraps=session._review_narrative)
    monkeypatch.setattr(session, "_review_narrative", review_narrative)
    dispatched = []
    dispatch = session._dispatch_unified_tool

    async def dispatch_with_interjection(ctx, name, args):
        dispatched.append((name, copy.deepcopy(args)))
        if name == "write_narrative" and interjection_phase == "tool":
            retrieved.set()
            await release_narrative.wait()
        return await dispatch(ctx, name, args)

    monkeypatch.setattr(session, "_dispatch_unified_tool", dispatch_with_interjection)
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_atrium"},
        "visible_locations": ["test_atrium", "test_archive"],
        "display_names": {"test_atrium": "Atrium", "test_archive": "Archive"},
        "active_persistent_states": ["test_departure"],
    }
    session.script_variables.init_state(session.current_state)
    root_id = session.world_tree.add_node(
        None, session.current_state["game_time"], 0,
        ai_response="You are in the Atrium. The Archive is accessible.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "harness.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    append = game_routes._ActionStreamJournal.append

    def observe(journal, text):
        append(journal, text)
        if any(event["type"] == "agent_waiting" for event in _stream_events(text)):
            waiting.set()

    monkeypatch.setattr(game_routes._ActionStreamJournal, "append", observe)
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    action = {
        "type": "freeform", "text": "Inspect a nearby room.", "intent_hint": "explicit_action",
        "stream_id": "synthetic-stream", "action_id": "synthetic-action", "expected_node_id": root_id,
    }
    endpoint = f"/api/game/{session.save_id}"
    pending = None
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": session.current_state,
            "narrative": session.world_tree.get_node(root_id)["ai_response"],
        })
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            pending = asyncio.create_task(client.post(f"{endpoint}/action/stream", json=action))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=5)
                assert provider.generate_with_tools.await_count == 1
                assert session.current_state["player"]["location"] == "test_atrium"
                assert session.world_tree.active_node_id == root_id
                async with database.get_db() as db:
                    plan = await (await db.execute(
                        "SELECT * FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert plan["base_node_id"] == root_id
                assert plan["action_id"] == action["action_id"]
                assert json.loads(plan["request"]) == game_routes.ActionRequest(**action).model_dump()
                assert plan["reply"] is None
                answered = await client.post(f"{endpoint}/inject", json={
                    "stream_id": action["stream_id"], "plan_id": plan["plan_id"], "message": "Archive",
                })
                assert answered.status_code == 200
                await asyncio.wait_for(retrieved.wait(), timeout=5)
                assert session.current_state["player"]["location"] == "test_atrium"
                assert session.current_state["game_time"] == "2030-01-01T09:00:00"
                assert session.current_state["script_variables"]["test_capacity"] == 4
                assert "test_visit_count" not in session.script_variables.definitions
                assert not session.trigger_engine.triggers
                assert not session.script.get("random_items")
                assert_lifecycle_state(session, settled=False)
                async with database.get_db() as db:
                    plan_reply = await (await db.execute(
                        "SELECT reply FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                    node_count = await (await db.execute(
                        "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert plan_reply["reply"] == "Archive"
                assert node_count[0] == 1
                interjection = "Read the shelf label without taking anything."
                injected = await client.post(f"{endpoint}/inject", json={
                    "stream_id": action["stream_id"], "message": interjection,
                })
                assert injected.status_code == 200
                release_narrative.set()
                response = await asyncio.wait_for(asyncio.shield(pending), timeout=5)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                events = _stream_events(response.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert final["node_id"] != root_id
                assert final["state"]["player"]["location"] == "test_archive"
                assert final["state"]["game_time"] == "2030-01-01T09:05:00"
                assert session.current_state["script_variables"]["test_capacity"] == 7
                assert_lifecycle_state(session, settled=True)
                assert final["state"]["active_persistent_states"] == ["test_arrived"]
                assert final["state"]["npc_schedule_overrides"] == session.current_state["npc_schedule_overrides"]
                assert all(args.get("summary") != "Outdated decision" for name, args in dispatched)
                assert sum(name == "finalize_turn" for name, _ in dispatched) == 1
                assert review_narrative.await_args.args[0] == narrative
                assert action["text"] in review_narrative.await_args.args[1]
                assert interjection in review_narrative.await_args.args[1]
                if interjection_phase == "tool":
                    deferred_results = [message["content"] for messages in requests for message in messages
                                        if message.get("tool_call_id") == "stale-decision"]
                    assert deferred_results and all(json.loads(result).get("error") for result in deferred_results)
                    unreviewed_results = [message["content"] for messages in requests for message in messages
                                          if message.get("tool_call_id") == f"synthetic-call-{narrative_call_index + 1}"]
                    assert unreviewed_results
                    assert all("write_narrative" in json.loads(result)["error"] for result in unreviewed_results)
                assert "SYNTHETIC_UNREAD_DETAILS" not in response.text
                assert "SYNTHETIC_MECHANICS_PRIVATE" not in response.text
                assert "SYNTHETIC_LIFECYCLE_DETAILS" not in response.text
                assert "test_callback" not in final["state"].get("pc_discovered_lore", [])
                assert "lorebook_entry_overrides" not in response.text
                assert "mechanics_overrides" not in response.text
                assert "_dyn_test_visit_hook" not in response.text
                assert any(event["type"] == "user_inject" and event["message"] == interjection for event in events)
                assert any({"role": "user", "content": interjection} in messages for messages in requests)
                assert any("Archive" in message.get("content", "") for message in requests[1] if message["role"] == "user")
                assert "SYNTHETIC_MECHANICS_PRIVATE" not in json.dumps(requests[0])
                for index, expected in [(2, 3), (4, 6), (7, 7), (14, 7)]:
                    messages = [message for request in requests for message in request
                                if message.get("tool_call_id") == f"synthetic-call-{index}"]
                    assert json.dumps({"test_capacity": expected}) in messages[0]["content"]
                    assert "未提交" in messages[0]["content"]
                assert any(message.get("tool_call_id") == "synthetic-call-3"
                           and message["content"].startswith("已撤销") for request in requests for message in request)
                assert any(message.get("tool_call_id") == "synthetic-call-6"
                           and message["content"].startswith("已撤销") for request in requests for message in request)
                assert any(message.get("tool_call_id") == "synthetic-call-5"
                           and message["content"].startswith("错误:") for request in requests for message in request)
                undo_messages = [message["content"] for request in requests for message in request
                                 if message.get("tool_call_id") == "synthetic-call-11"]
                assert undo_messages and undo_messages[0].startswith("已撤销上一个工具调用（update_state）")
                assert '"test_capacity": 6' in undo_messages[0]
                assert "已接受 1 次状态更新" in undo_messages[0]
                assert "重新查询" in undo_messages[0]
                for index in (12, 13):
                    revoked_results = [json.loads(message["content"]) for request in requests for message in request
                                       if message.get("tool_call_id") == f"synthetic-call-{index}"]
                    assert revoked_results and all(not result["projection"]["event_ids"] for result in revoked_results)
                    if lore_effect == "update_lore":
                        assert all(result["entries"][0]["content"] == "The shelf label is blank."
                                   for result in revoked_results)
                    else:
                        assert all(not result["entries"] for result in revoked_results)
                assert sum(name == "query_lorebook" for name, _ in dispatched) == 3
                if compact_history:
                    summaries = [message["content"] for request in requests for message in request
                                 if (message.get("content") or "").startswith("[上下文压缩]")]
                    assert summaries
                    final_context = json.dumps(requests[-1], ensure_ascii=False)
                    assert '"test_capacity": 7' in "\n".join(summaries)
                    assert "已接受 2 次状态更新" in final_context
                    assert "已撤销上一个工具调用（update_state）" in final_context
                    assert "请重新查询后再继续推演" in final_context
                    requery_context = next(request for request in requests
                                           if any(message.get("tool_call_id") == "synthetic-call-14"
                                                  for message in request)
                                           and not any(message.get("tool_call_id") == "synthetic-call-15"
                                                       for message in request))
                    assert not any(message.get("tool_call_id") == "synthetic-call-12"
                                   for message in requery_context)
                    query_statuses = [json.loads(line.removeprefix("[世界书查询状态] "))
                                      for message in requery_context
                                      for line in (message.get("content") or "").splitlines()
                                      if line.startswith("[世界书查询状态] ")]
                    assert query_statuses
                    assert query_statuses[-1]["requested_ids"] == ["test_label"]
                    assert query_statuses[-1]["not_returned_ids"] == (
                        [] if lore_effect == "update_lore" else ["test_label"]
                    )
                    assert query_statuses[-1]["projection"]["event_ids"] == []
                    assert query_statuses[-1]["projection"]["provisional"] is True
                    assert "[工具失败] update_state:" in final_context
                for index, (name, arguments) in enumerate(operations):
                    if name != "consult_rules":
                        continue
                    rule_messages = [message for messages in requests for message in messages
                                     if message.get("tool_call_id") == f"synthetic-call-{index}"]
                    assert rule_messages
                    result = json.loads(rule_messages[0]["content"])
                    assert result["source"] == "current_branch"
                    assert result["game_time"] == "2030-01-01T09:00:00"
                    if arguments["query"] == "test_capacity":
                        payload = json.loads(result["entries"][0]["content"])
                        assert payload["definition"] == script["variables"][0]
                        assert payload["current_value"] == 4
                    else:
                        assert result["entries"] == [] and result["total"] == 0
                for event in events:
                    if event["type"] in ("tool_started", "tool_call"):
                        assert set(event) <= {"type", "label", "round", "tool_name"}
                tool_results = [json.loads(message["content"]) for messages in requests for message in messages
                                if message["role"] == "tool" and message.get("content", "").startswith('{"entries"')]
                assert any(result["entries"][0]["id"] == "test_label"
                           and result["projection"]["event_ids"] == expected_events
                           and result["projection"]["provisional"] for result in tool_results if result["entries"])
                calls_before_replay = provider.generate_with_tools.await_count
                replay = await client.post(f"{endpoint}/action/stream", json=action)
                assert replay.status_code == 200
                assert _stream_events(replay.text) == events
                assert provider.generate_with_tools.await_count == calls_before_replay
                assert_lifecycle_state(session, settled=True)
            finally:
                release_narrative.set()
                if pending is not None and not pending.done():
                    await client.post(f"{endpoint}/abort", json={"stream_id": action["stream_id"]})
                    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5)
        await session._drain_background_tasks()
        async with database.get_db() as db:
            counts = {}
            for table in ("tree_nodes", "pending_action_plans", "pending_action_injections"):
                row = await (await db.execute(f"SELECT COUNT(*) FROM {table} WHERE save_id = ?", (session.save_id,))).fetchone()
                counts[table] = row[0]
        assert counts == {"tree_nodes": 2, "pending_action_plans": 0, "pending_action_injections": 0}
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        assert restored.world_tree.active_node_id == final["node_id"]
        assert restored.current_state["player"]["location"] == "test_archive"
        assert restored.current_state["game_time"] == "2030-01-01T09:05:00"
        assert restored.current_state["script_variables"]["test_capacity"] == 7
        assert restored.current_state["script_variables"]["test_visit_count"] == 2
        assert_lifecycle_state(restored, settled=True)
        saved_mechanics = {
            category: json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"]
            for category in ("variables", "triggers", "random_items")
        }
        assert all(len(entries) == 1 for entries in saved_mechanics.values())
        assert "_dyn_test_visit_roll" in restored._random_item_by_id
        restored._fire_event_dual("test_visit_tick")
        assert restored.current_state["script_variables"]["test_visit_count"] == 3
        restored.script_variables.set(restored.current_state, "test_visit_count", 50)
        assert restored.current_state["script_variables"]["test_visit_count"] == 10
        node = restored.world_tree.get_node(final["node_id"])
        assert node["ai_response"] == narrative
        trace = node["agent_trace"]
        assert "SYNTHETIC_MECHANICS_PRIVATE" not in json.dumps(trace)
        assert "SYNTHETIC_LIFECYCLE_DETAILS" not in json.dumps(trace)
        expected_tools = [name for name, _ in operations]
        if interjection_phase == "tool":
            expected_tools.insert(narrative_call_index + 1, "finalize_turn")
        assert [round_record["tool"] for round_record in trace["rounds"]] == expected_tools
        assert trace["plan_waits"] == 1
        assert trace["lore_queries"] == 4
        assert trace["lore_hits"] == trace["lore_entry_reads"] == (4 if lore_effect == "update_lore" else 2)
        assert trace["semantic_review_status"] == "passed"
        assert [(entry["message"], entry["plan_reply"]) for entry in trace["user_injections"]] == [
            ("Archive", True), (interjection, False),
        ]
        lore = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert [entry["id"] for entry in lore["entries"]] == ["test_label"]
        assert lore["entries"][0]["content"] == "The shelf label reads LANTERN."
        repeated = []
        restored._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", repeated)
        assert repeated == []
        branch = await restored.branch_to_node(root_id)
        assert branch["node_id"] == root_id
        assert restored.world_tree.get_node(root_id)["ai_response"] == "You are in the Atrium. The Archive is accessible."
        assert restored.current_state["script_variables"]["test_capacity"] == 4
        assert "test_visit_count" not in restored.script_variables.definitions
        assert not restored.trigger_engine.triggers
        assert not restored._random_item_by_id
        assert_lifecycle_state(restored, settled=False)
        for category in saved_mechanics:
            assert json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"] == []
        previous = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert [entry["content"] for entry in previous["entries"]] == (
            ["The shelf label is blank."] if lore_effect == "update_lore" else []
        )
        branch = await restored.branch_to_node(final["node_id"])
        assert branch["node_id"] == final["node_id"]
        assert_lifecycle_state(restored, settled=True)
        assert restored.current_state["script_variables"]["test_capacity"] == 7
        for category, entries in saved_mechanics.items():
            assert json.loads(restored._handle_consult_rules({"category": category, "query": "test_visit"}))["entries"] == entries
        assert restored.current_state["script_variables"]["test_visit_count"] == 2
        assert "_dyn_test_visit_roll" in restored._random_item_by_id
        restored._fire_event_dual("test_visit_tick")
        assert restored.current_state["script_variables"]["test_visit_count"] == 3
        current = json.loads(await restored._run_tool_native_async("query_lorebook", {"entry_id": "test_label"}))
        assert current["entries"] == lore["entries"]
    finally:
        release_narrative.set()
        await database.close_pool()
