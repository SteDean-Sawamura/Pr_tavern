import asyncio
import copy
import json
import re
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_agent_rule_context import model_call


PRIVATE_PLAN = "SYNTHETIC_REPLAY_PRIVATE_PLAN"
PLAN = {
    "plan": "Determine the procedure without assuming a choice.",
    "questions": [{"question": "Which procedure?", "options": ["Inspect", "Leave"]}],
}
NEW_PLAN = {
    "plan": "Ask a new question with a different option order.",
    "questions": [{"question": "Which procedure now?", "options": ["Leave", "Inspect"]}],
}
EXTRA = "Remain beside the door throughout the procedure."


def context_block(messages, tag):
    content = "\n".join(message.get("content") or "" for message in messages)
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", content, re.S)
    return json.loads(match.group(1)) if match else None


def chosen_procedure(context):
    return context["questions"][0]["options"][("第一项", "第二项").index(context["reply"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("input_kind", ["reply", "injection", "both", "none"])
async def test_full_regeneration_replays_only_its_swipe_inputs(tmp_path, monkeypatch, streaming, delegated, input_kind):
    script = {
        "script_id": "synthetic-input-replay", "name": "Synthetic input replay", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Empty room"}],
        "story_tree": {},
        "variables": [{"id": name, "type": "number", "default": 0, "rule_notes": PRIVATE_PLAN}
                      for name in ("test_choice", "test_door")],
    }
    session = GameSession(script, None, save_id="synthetic-input-replay-save")
    session.current_state = {
        "game_time": script["start_time"], "_pipeline_mode": "agentic_unified",
        "player": {"name": "Visitor", "location": "test_room"}, "visible_locations": ["test_room"],
        "display_names": {"test_room": "Empty room"},
    }
    session.script_variables.init_state(session.current_state)
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, script["start_time"], 0, state_snapshot=root_state)
    has_reply, has_injection = input_kind in {"reply", "both"}, input_kind in {"injection", "both"}
    original_plan = copy.deepcopy(PLAN)
    if not has_reply:
        original_plan["questions"] = []
    ready, release = asyncio.Event(), asyncio.Event()
    run = {"number": 0, "phase": "catalog", "background": False}
    requests, reviews = [], []

    def derive_input(messages, background=False):
        inputs = context_block(messages, "turn_player_inputs") or []
        choice = "Inspect"
        for entry in inputs:
            if entry.get("plan_reply_context"):
                choice = chosen_procedure(entry["plan_reply_context"])
        current = context_block(messages, "plan_reply_context")
        if current:
            choice = chosen_procedure(current)
        if not background:
            reply = next((message["content"] for message in reversed(messages)
                          if message["role"] == "user" and message.get("content") in ("第一项", "第二项")), None)
            if reply:
                choice = chosen_procedure({**(NEW_PLAN if run["number"] == 2 else PLAN), "reply": reply})
        transcript = json.dumps(messages, ensure_ascii=False)
        return choice, int(EXTRA in transcript)

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if names == {"extract_notes"}:
            return {"content": "", "tool_calls": [model_call("extract_notes", {"notes": []}, "test_notes")]}
        background = "submit_plan" not in names and "delegate_settlement" not in names
        requests.append((run["number"], "background" if background else run["phase"], copy.deepcopy(messages)))
        if background:
            if run["background"]:
                return {"content": "Finished the synthetic settlement.", "tool_calls": []}
            run["background"] = True
            choice, door = derive_input(messages, background=True)
            name, arguments = "update_state", {
                "end_time": "2030-01-01T09:05:00", "variable_changes": [
                    {"id": "test_choice", "op": "set", "value": int(choice == "Leave")},
                    {"id": "test_door", "op": "set", "value": door},
                ],
            }
        elif run["phase"] == "catalog":
            run["phase"] = "plan"
            name, arguments = "consult_rules", {"category": "variables"}
        elif run["phase"] == "plan":
            run["phase"] = "decide"
            plan = NEW_PLAN if run["number"] == 2 else original_plan if run["number"] == 0 else {
                "plan": "Repeat the same action using the player's existing instructions.", "questions": [],
            }
            name, arguments = "submit_plan", copy.deepcopy(plan)
        elif run["phase"] == "decide":
            if run["number"] == 0 and has_injection and EXTRA not in json.dumps(messages, ensure_ascii=False):
                ready.set()
                await release.wait()
                return {"content": "", "tool_calls": [model_call(
                    "consult_rules", {"category": "variables"}, f"test_interrupted_{len(requests)}",
                )]}
            choice, door = derive_input(messages)
            run["narrative"] = (
                f"You {choice.lower()} the empty shelf without moving any objects. "
                + ("You stay beside the door throughout. " if door else "You stand quietly inside the room. ")
                + "The room remains silent. You finish the procedure carefully and leave everything undisturbed."
            )
            run["phase"] = "narrative"
            name, arguments = "update_state", {
                "end_time": "2030-01-01T09:05:00", "variable_changes": [
                    {"id": "test_choice", "op": "set", "value": int(choice == "Leave")},
                    {"id": "test_door", "op": "set", "value": door},
                ],
            }
            if delegated and run["number"]:
                run["phase"] = "finish"
                name, arguments = "write_narrative", {"text": run["narrative"]}
        elif run["phase"] == "narrative":
            run["phase"] = "finish"
            name, arguments = "write_narrative", {"text": run["narrative"]}
        else:
            run["phase"] = "done"
            name = "delegate_settlement" if delegated and run["number"] else "finalize_turn"
            arguments = {"summary": "Finished the synthetic procedure."}
        return {"content": "", "tool_calls": [model_call(name, arguments, f"test_input_{len(requests)}")]}

    async def generate(messages, **_kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        if "<narrative>" in content and "<protagonist_name>" in content:
            reviews.append((run["number"], copy.deepcopy(messages)))
            return json.dumps({"pass": True, "violations": []})
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [],
        })

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(side_effect=generate))
    session.ai_provider = provider
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "input-replay.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    await database.init_db()
    pending, sessions = None, [session]
    original_inputs = []
    if has_reply:
        original_inputs.append({"message": "第二项", "plan_reply_context": {**PLAN, "reply": "第二项"}})
    if has_injection:
        original_inputs.append({"message": EXTRA})
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)", (
                script["script_id"], script["name"], json.dumps(script),
            ))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": "You enter the room."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}"
            action = {"type": "freeform", "text": "Choose a procedure in the room.", "intent_hint": "explicit_action",
                      "stream_id": "test_input_0", "action_id": "test_input_action", "expected_node_id": root_id}
            original_id, saved_action = None, None
            for number in range(4):
                run.update(number=number, phase="catalog", background=False)
                if number:
                    await session._drain_background_tasks()
                    game_routes._sessions.clear()
                    session = await game_routes._restore_session(session.save_id)
                    sessions.append(session)
                    if number == 3:
                        response = await client.post(f"{endpoint}/swipe/jump", params={"index": 0})
                        assert response.status_code == 200, response.text
                        assert session.world_tree.get_node(original_id).get("_turn_player_inputs", []) == original_inputs
                use_stream = number == 0 or number == 2 or streaming
                stream_id = f"test_input_{number}"
                if number == 0:
                    pending = asyncio.create_task(client.post(f"{endpoint}/action/stream", json=action))
                else:
                    suffix = "/stream" if use_stream else ""
                    pending = asyncio.create_task(client.post(f"{endpoint}/regenerate{suffix}", params={
                        "stage": "all", "stream_id": stream_id,
                    }))
                if (number == 0 and has_reply) or number == 2:
                    async def wait_for_plan():
                        while not getattr(session, "_pending_agent_plan_id", None):
                            if pending.done():
                                raise AssertionError((await pending).text)
                            await asyncio.sleep(0.01)
                        return session._pending_agent_plan_id

                    plan_id = await asyncio.wait_for(wait_for_plan(), 8)
                    assert not pending.done()
                    response = await client.post(f"{endpoint}/inject", json={
                        "stream_id": stream_id, "plan_id": plan_id, "message": "第二项",
                    })
                    assert response.status_code == 200, response.text
                if number == 0 and has_injection:
                    await asyncio.wait_for(ready.wait(), 8)
                    response = await client.post(f"{endpoint}/inject", json={"stream_id": stream_id, "message": EXTRA})
                    assert response.status_code == 200, response.text
                    release.set()
                response = await asyncio.wait_for(pending, 15)
                assert response.status_code == 200, response.text
                if use_stream:
                    events = _stream_events(response.text)
                    assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
                    result, = [event for event in events if event["type"] == "final"]
                else:
                    result = response.json()
                original_id = original_id or result["node_id"]
                assert result["node_id"] == original_id
                expected_choice = int(has_reply and number != 2)
                assert session.current_state["script_variables"]["test_choice"] == expected_choice
                assert session.current_state["script_variables"]["test_door"] == int(has_injection)
                node = session.world_tree.get_node(original_id)
                saved_action = saved_action or copy.deepcopy(node["player_action"])
                assert node["player_action"] == saved_action
                expected_inputs = original_inputs + ([{
                    "message": "第二项", "plan_reply_context": {**NEW_PLAN, "reply": "第二项"},
                }] if number == 2 else [])
                if number:
                    assert node.get("_turn_player_inputs", []) == expected_inputs
                    assert all((context_block(messages, "turn_player_inputs") or []) == original_inputs
                               for iteration, phase, messages in requests if iteration == number and phase in {"catalog", "plan"})
                    iteration_reviews = [messages for iteration, messages in reviews if iteration == number]
                    assert iteration_reviews
                    assert all((context_block(messages, "turn_player_inputs") or []) == expected_inputs
                               for messages in iteration_reviews)
                    if delegated:
                        background_requests = [messages for iteration, phase, messages in requests
                                               if iteration == number and phase == "background"]
                        assert background_requests
                        assert all((context_block(messages, "turn_player_inputs") or []) == expected_inputs
                                   for messages in background_requests)
                assert PRIVATE_PLAN not in response.text
                assert "turn_player_inputs" not in response.text
                assert "_turn_player_inputs" not in json.dumps(session.current_state)
                assert {key: session.script[key] for key in script} == script
                await session._drain_background_tasks()
                if number == 0:
                    calls = provider.generate_with_tools.await_count
                    replay = await client.post(f"{endpoint}/action/stream", json=action)
                    assert replay.text == response.text
                    assert provider.generate_with_tools.await_count == calls
                for suffix in (f"tree/{original_id}", "history", "swipes"):
                    public = await client.get(f"{endpoint}/{suffix}")
                    assert public.status_code == 200
                    assert PRIVATE_PLAN not in public.text and "turn_player_inputs" not in public.text
            async with database.get_db() as connection:
                row = await (await connection.execute("SELECT state_snapshot FROM tree_nodes WHERE id = ?", (original_id,))).fetchone()
                context = json.loads(row["state_snapshot"])["_node_context"]
                assert context.get("_turn_player_inputs", []) == original_inputs
                assert context["swipes"][2]["_turn_player_inputs"] == original_inputs + [{
                    "message": "第二项", "plan_reply_context": {**NEW_PLAN, "reply": "第二项"},
                }]
                for table, expected_count in (("tree_nodes", 2), ("pending_action_plans", 0), ("pending_action_injections", 0)):
                    count = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
                    assert count[0] == expected_count
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        for saved_session in sessions:
            await saved_session._drain_background_tasks()
        await database.close_pool()
