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
from test_agent_rule_context import CATEGORIES, large_session, model_call


PLAN = {
    "plan": "Resolve the requested procedure before inspecting the synthetic room; leave other objects alone.",
    "questions": [{"question": "Which procedure should be used?", "options": ["Inspect", "Leave"]}],
}
NARRATIVES = {
    "Inspect": "You inspect the empty shelf beside the wall. Its wooden surface is clean and dry. "
               "You examine its edges without moving anything, then step back and leave the room undisturbed.",
    "Leave": "You leave the empty shelf alone and remain beside the door. The room is quiet and still. "
             "You keep your hands at your sides without touching anything, then finish your visit to the room.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [4, 9])
@pytest.mark.parametrize("mode", ["first", "second", "replanned", "no_questions"])
@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("end_time", ["2030-01-01T09:00:00", "2030-01-01T09:05:00"], ids=["instant", "elapsed"])
async def test_plan_reply_reaches_review_and_declared_action_survives_reload(tmp_path, monkeypatch, value, mode, delegated, end_time):
    script = copy.deepcopy(large_session(value).script)
    script["locations"] = [{"id": "test_room", "name": "Reading room"}]
    script["story_tree"]["trees"][0]["nodes"][2]["action_rule"] = {
        option: {"end_time": end_time,
                 "variable_changes": [{"id": "test_variables_2", "op": "set", "value": value + index}]}
        for index, option in enumerate(PLAN["questions"][0]["options"])
    }
    session = GameSession(script, None, save_id="synthetic-plan-review-save")
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "_pipeline_mode": "agentic_unified",
        "player": {"name": "Visitor", "location": "test_room"}, "visible_locations": ["test_room"],
        "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    root_state = copy.deepcopy(session.current_state)
    original_script = copy.deepcopy(session.script)
    root_id = session.world_tree.add_node(None, root_state["game_time"], 0, state_snapshot=root_state)
    reply = None if mode == "no_questions" else "第一项" if mode == "first" else "第二项"
    plan = {**copy.deepcopy(PLAN), "questions": [] if reply is None else copy.deepcopy(PLAN["questions"])}
    waiting, resumed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, reviews, chunks = [], [], []
    phase, selected, declaration = "plan", None, None
    settlement_phase, settled_choice = "catalog", None
    settlement_requests, settlement_chunks = [], []

    async def respond(messages, **kwargs):
        nonlocal phase, selected, declaration, settlement_phase, settled_choice
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [model_call("extract_notes", {"notes": []}, "test_notes")]}
        requests.append(copy.deepcopy(messages))
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if delegated and not names.intersection({"submit_plan", "delegate_settlement"}):
            content = "\n".join(message.get("content") or "" for message in messages)
            match = re.search(r"<plan_reply_context>\s*(.*?)\s*</plan_reply_context>", content, re.S)
            context = json.loads(match.group(1)) if match else None
            settlement_requests.append((content, context))
            if settlement_phase == "catalog":
                settlement_phase = "preview"
                name, arguments = "consult_rules", {"category": "story_nodes"}
            elif settlement_phase == "preview":
                preview = json.loads(next(message["content"] for message in reversed(messages)
                                          if message["role"] == "tool"))
                assert preview["category"] == "story_nodes" and preview["source"] == "current_branch"
                settlement_phase = "read"
                name, arguments = "consult_rules", {
                    "category": preview["category"], "rule_index": preview["entries"][2]["rule_index"],
                    "offset": 0, "snapshot": preview["entries"][2]["snapshot"],
                }
            elif settlement_phase == "read":
                result = json.loads(next(message["content"] for message in reversed(messages)
                                         if message["role"] == "tool"))
                entry, = result["entries"]
                assert entry["offset"] == sum(map(len, settlement_chunks))
                settlement_chunks.append(entry["content"])
                if entry["next_offset"] is not None:
                    name, arguments = "consult_rules", {
                        "category": result["category"], "rule_index": entry["rule_index"],
                        "offset": entry["next_offset"], "snapshot": entry["snapshot"],
                    }
                else:
                    rules = json.loads("".join(settlement_chunks))["definition"]["node"]["action_rule"]
                    settled_choice = (context["questions"][0]["options"][("第一项", "第二项").index(context["reply"])]
                                      if context is not None else next(iter(rules)))
                    settlement_phase = "finished"
                    name, arguments = "update_state", rules[settled_choice]
            else:
                return {"content": "Finished the synthetic settlement.", "tool_calls": []}
            return {"content": "", "tool_calls": [model_call(name, arguments, f"test_settlement_step_{len(requests)}")]}
        if phase == "plan":
            phase = "catalog"
            name, arguments = "submit_plan", copy.deepcopy(plan)
        elif phase == "catalog":
            if reply is not None:
                resumed.set()
                await release.wait()
            phase = "preview"
            return {"content": "", "tool_calls": [
                model_call("consult_rules", {"category": category}, f"test_plan_catalog_{index}")
                for index, category in enumerate(CATEGORIES)
            ]}
        elif phase == "preview":
            preview = json.loads(next(message["content"] for message in messages
                                      if message.get("tool_call_id") == "test_plan_catalog_0"))
            assert preview["truncated"] is True
            phase = "read"
            name, arguments = "consult_rules", {
                "category": preview["category"], "rule_index": preview["entries"][2]["rule_index"],
                "offset": 0, "snapshot": preview["entries"][2]["snapshot"],
            }
        elif phase == "read":
            result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
            entry, = result["entries"]
            assert entry["offset"] == sum(map(len, chunks))
            chunks.append(entry["content"])
            if entry["next_offset"] is not None:
                name, arguments = "consult_rules", {
                    "category": result["category"], "rule_index": entry["rule_index"],
                    "offset": entry["next_offset"], "snapshot": entry["snapshot"],
                }
            else:
                rules = json.loads("".join(chunks))["definition"]["node"]["action_rule"]
                transcript = "\n".join(message.get("content") or "" for message in messages).replace("\\n", "\n")
                assert PLAN["plan"] in transcript
                answer = next((message["content"] for message in messages
                               if message["role"] == "user" and message.get("content") in ("第一项", "第二项")), None)
                if answer is None:
                    summary_answer = re.search(r"(?m)^\[玩家消息\] (第一项|第二项)$", transcript)
                    answer = summary_answer.group(1) if summary_answer else None
                question = re.search(r"Q: Which procedure should be used\? 选项: ([^\n]+)", transcript)
                if question:
                    assert answer is not None
                    options = question.group(1).split(", ")
                    selected = options[("第一项", "第二项").index(answer)]
                else:
                    selected = next(iter(rules))
                declaration = rules[selected]
                phase = "updated"
                name, arguments = "update_state", declaration
        elif phase == "updated" and mode == "replanned":
            phase = "replanned"
            name, arguments = "submit_plan", {
                "plan": "An unconfirmed replacement proposal.",
                "questions": [{"question": "Unconfirmed replacement?", "options": ["Other", "Another"]}],
            }
        elif phase in {"updated", "replanned"}:
            phase = "narrated"
            name, arguments = "write_narrative", {"text": NARRATIVES[selected]}
        else:
            phase = "finished"
            name, arguments = "finalize_turn", {"summary": "Finished the synthetic procedure."}
        if delegated and name == "update_state":
            if mode == "replanned":
                phase = "replanned"
                name, arguments = "submit_plan", {
                    "plan": "An unconfirmed replacement proposal.",
                    "questions": [{"question": "Unconfirmed replacement?", "options": ["Other", "Another"]}],
                }
            else:
                phase = "narrated"
                name, arguments = "write_narrative", {"text": NARRATIVES[selected]}
        elif delegated and name == "finalize_turn":
            name = "delegate_settlement"
        return {"content": "", "tool_calls": [model_call(name, arguments, f"test_plan_step_{len(requests)}")]}

    async def generate(messages, **_kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        if "<narrative>" in content and "<protagonist_name>" in content:
            match = re.search(r"<plan_reply_context>\s*(.*?)\s*</plan_reply_context>", content, re.S)
            context = json.loads(match.group(1)) if match else None
            reviews.append((content, context))
            if reply is None:
                passed = context is None
            elif context is None:
                passed = False
            else:
                choice = context["questions"][0]["options"][("第一项", "第二项").index(context["reply"])]
                passed = choice in NARRATIVES and NARRATIVES[choice] in content
            return json.dumps({"pass": passed, "violations": [] if passed else [{
                "type": "unsupported_fact", "detail": "The narrative cannot be matched to the plan reply.",
            }]})
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [],
        })

    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(side_effect=generate))
    session.ai_provider = provider
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "plan-review.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
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
    await database.init_db()
    pending, restored = None, None
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
            action = {"type": "freeform", "text": "Choose how to proceed in the room.", "intent_hint": "explicit_action",
                      "stream_id": "test_plan_review_stream", "action_id": "test_plan_review_action", "expected_node_id": root_id}
            pending = asyncio.create_task(client.post(f"{endpoint}/action/stream", json=action))
            if reply is not None:
                await asyncio.wait_for(waiting.wait(), 5)
                async with database.get_db() as connection:
                    record = await (await connection.execute(
                        "SELECT * FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert record["base_node_id"] == root_id and record["reply"] is None
                response = await client.post(f"{endpoint}/inject", json={
                    "stream_id": action["stream_id"], "plan_id": record["plan_id"], "message": reply,
                })
                assert response.status_code == 200
                await asyncio.wait_for(resumed.wait(), 5)
                async with database.get_db() as connection:
                    saved_reply = await (await connection.execute(
                        "SELECT reply FROM pending_action_plans WHERE save_id = ?", (session.save_id,),
                    )).fetchone()
                assert saved_reply["reply"] == reply
                assert session.current_state["script_variables"] == root_state["script_variables"]
                assert session.current_state["game_time"] == root_state["game_time"]
                release.set()
            response = await asyncio.wait_for(pending, 10)
            assert reviews
            expected_context = {**PLAN, "reply": reply} if reply is not None else None
            assert all(context == expected_context for _, context in reviews), reviews
            assert all(action["text"] in content for content, _ in reviews)
            if delegated:
                assert settlement_requests and len(settlement_chunks) >= 3
                assert all(context == expected_context for _, context in settlement_requests), settlement_requests
                assert all(f"<player_action>\n{action['text']}\n</player_action>" in content
                           for content, _ in settlement_requests)
                assert settled_choice == selected
            else:
                assert not settlement_requests
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], events
            final, = [event for event in events if event["type"] == "final"]
            assert phase == "finished" and len(chunks) >= 3
            assert selected == ("Leave" if mode in {"second", "replanned"} else "Inspect")
            assert final["narrative"] == NARRATIVES[selected]
            expected_value = value + int(selected == "Leave")
            assert declaration["variable_changes"][0]["value"] == expected_value
            assert session.current_state["script_variables"]["test_variables_2"] == expected_value
            assert final["state"]["game_time"] == session.current_state["game_time"] == end_time
            assert session.script == original_script
            assert "SYNTHETIC_RULE_BUDGET_BODY" in json.dumps(requests)
            assert "SYNTHETIC_RULE_BUDGET_BODY" not in response.text
            assert "plan_reply_context" not in response.text
            await session._drain_background_tasks()
            calls = provider.generate_with_tools.await_count
            replay = await client.post(f"{endpoint}/action/stream", json=action)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == calls
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            assert restored.current_state["script_variables"]["test_variables_2"] == expected_value
            assert restored.current_state["game_time"] == end_time
            trace = restored.world_tree.get_node(final["node_id"])["agent_trace"]
            assert sum(record["tool"] == "update_state" for record in trace["rounds"]) == 1
            assert "SYNTHETIC_RULE_BUDGET_BODY" not in json.dumps(trace)
            assert "plan_reply_context" not in json.dumps(restored.current_state)
            async with database.get_db() as connection:
                for table, count in (("tree_nodes", 2), ("pending_action_plans", 0), ("pending_action_injections", 0)):
                    stored = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
                    assert stored[0] == count
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await session._drain_background_tasks()
        if restored is not None:
            await restored._drain_background_tasks()
        await database.close_pool()
