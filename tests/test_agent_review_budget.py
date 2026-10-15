import asyncio
import copy
from contextlib import aclosing
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events
from test_agent_plan_wait import PlanLoop
from test_generation_baseline import restore
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_story_choice_settlement import choice_client, stored_state


DRAFTS = [
    f"You examine the empty {surface} beside the wall. Its surface is clean and dry. "
    "You look along its edges without moving anything, then step back to leave the room undisturbed."
    for surface in ("shelf", "table", "chair", "desk")
]
INJECTION = "Inspect only the desk; leave the other furniture alone."


def review_session():
    script = {
        "script_id": "synthetic-review-budget", "name": "Synthetic review budget", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "variables": [{"id": "test_inspected", "type": "number", "default": 0}],
        "story_tree": {}, "random_items": [], "npcs": [],
    }
    session = GameSession(copy.deepcopy(script), None, save_id="synthetic-review-budget-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_reviews", [1, 2, 3])
@pytest.mark.parametrize("terminal", ["finalize_turn", "delegate_settlement"])
@pytest.mark.parametrize("revised", [False, True])
@pytest.mark.parametrize("rejections,recovery", [(0, None), (2, None), (3, "warm"), (3, "reload")])
async def test_interjection_gets_fresh_review_budget_and_persists(
    tmp_path, monkeypatch, prior_reviews, terminal, revised, rejections, recovery,
):
    session = review_session()
    terminal_entered = asyncio.Event()
    review_started = asyncio.Event()
    release_review = asyncio.Event()
    cancelled_review = asyncio.Event()
    reviews = []
    expected = DRAFTS[-1] if revised else DRAFTS[prior_reviews - 1]
    revised_drafts = [expected + " You pause beside the door." * attempt for attempt in range(rejections + 1)]
    steps = [("submit_plan", {"plan": "Inspect the room without moving anything."}),
             ("update_state", {"variable_changes": [{"id": "test_inspected", "op": "set", "value": 1}]})]
    steps.extend(("write_narrative", {"text": draft}) for draft in DRAFTS[:prior_reviews])
    steps.append((terminal, {"summary": "Inspected the room."}))
    initial_steps = len(steps)
    steps.extend([
        ("update_state", {"variable_changes": [{"id": "test_inspected", "op": "set", "value": 2}],
                          "end_time": "2030-01-01T09:01:00"}),
    ])
    for draft in revised_drafts:
        steps.extend([("write_narrative", {"text": draft}),
                      ("finalize_turn", {"summary": "Inspected the requested surface."})])

    async def review(narrative, action, *_args, **_kwargs):
        if _args and _args[0].get("_review_execution", {}).get("scope") == "applied_turn":
            assert INJECTION in action
            return {"pass": True, "violations": []}
        reviews.append((narrative, action))
        if len(reviews) == prior_reviews:
            review_started.set()
            try:
                await release_review.wait()
            except asyncio.CancelledError:
                cancelled_review.set()
                raise
        if prior_reviews < len(reviews) <= prior_reviews + rejections:
            return {"pass": False, "violations": [{
                "type": "unsupported_fact", "detail": "Revise this synthetic inspection description.",
            }]}
        return {"pass": True, "violations": []}

    async def respond(messages, **kwargs):
        names = {item["function"]["name"] for item in kwargs["tools"]}
        if names == {"extract_notes"}:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            step = sum(message.get("role") == "tool" for message in messages)
            if step >= initial_steps:
                assert {"role": "user", "content": INJECTION} in messages
            name, arguments = steps[min(step, len(steps) - 1)]
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"synthetic-review-budget-{step}",
                                                "name": name, "arguments": arguments}]}

    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        })),
    )
    original_loop = session._agent_loop

    async def monitored_loop(messages, system, tools, dispatch, **kwargs):
        async def monitored_dispatch(name, arguments):
            if name == terminal and not terminal_entered.is_set():
                terminal_entered.set()
            result = await dispatch(name, arguments)
            if name in {"finalize_turn", "delegate_settlement"} and result.startswith("{"):
                feedback = json.loads(result)
                if feedback.get("error", "").startswith("叙事语义审查未通过"):
                    assert feedback["state_update_accepted"] is False
                    assert "重传仍适用的参数" in feedback["retry_hint"]
                    assert "不要重复此前已接收的 update_state" in feedback["retry_hint"]
            return result

        async with aclosing(original_loop(messages, system, tools, monitored_dispatch, **kwargs)) as events:
            async for event in events:
                yield event

    monkeypatch.setattr(session, "_agent_loop", monitored_loop)
    monkeypatch.setattr(session, "_review_narrative", review)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_state(session)
        persisted_before = await database_state(session)
        models_before = session._snapshot_session_models()
        root_id = session.world_tree.active_node_id
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Inspect the room.", "intent_hint": "explicit_action",
                  "stream_id": "synthetic-budget-stream", "action_id": "synthetic-budget-action",
                  "expected_node_id": root_id}
        pending = asyncio.create_task(client.post(endpoint, json=action))
        try:
            await asyncio.wait_for(review_started.wait(), 5)
            await asyncio.wait_for(terminal_entered.wait(), 5)
            assert session.world_tree.active_node_id == root_id
            injected = await client.post(f"/api/game/{session.save_id}/inject", json={
                "stream_id": action["stream_id"], "message": INJECTION,
            })
            assert injected.status_code == 200
            assert injected.json() == {"queued": True}
            response = await asyncio.wait_for(pending, 10)
        finally:
            release_review.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        events = _stream_events(response.text)
        errors = [event for event in events if event["type"] in ("error", "agent_max_rounds", "aborted")]
        assert cancelled_review.is_set()
        assert [event["message"] for event in events if event["type"] == "user_inject"] == [INJECTION]
        assert_controls_closed(session)
        if rejections == 3:
            assert errors == [{"type": "error", "message": "叙事语义审查连续未通过，本轮未保存，请重试",
                               "discard_partial": True}]
            assert not any(event["type"] == "final" for event in events)
            assert [item[0] for item in reviews] == DRAFTS[:prior_reviews] + revised_drafts[:3]
            assert all(INJECTION in item[1] for item in reviews[prior_reviews:])
            assert runtime_state(session) == before
            assert session._snapshot_session_models() == models_before
            assert await database_state(session) == persisted_before
            classification = session.ai_provider.generate
            if recovery == "reload":
                game_routes._action_streams.clear()
                session = await restore(session, sessions)
            assert session.world_tree.active_node_id == root_id
            assert session.current_state["script_variables"]["test_inspected"] == 0
            snapshot_url = f"/api/game/{session.save_id}/snapshot?action_id={action['action_id']}"
            snapshot = (await client.get(snapshot_url)).json()
            pending_action = snapshot["pending_action"]
            assert pending_action == {
                "action": {"type": "freeform", "text": action["text"], "intent_hint": "explicit_action"},
                "actionId": action["action_id"], "streamId": action["stream_id"], "baseNodeId": root_id,
            }
            assert snapshot["node_id"] == root_id
            assert snapshot["action_completed"] is False
            mismatched = await client.post(endpoint, json={**action, "text": "Inspect a different room."})
            assert mismatched.status_code == 409
            assert (await client.get(snapshot_url)).json()["pending_action"] == pending_action
            async with database.get_db() as db:
                stored_injections = await (await db.execute(
                    "SELECT action_id, base_node_id, message FROM pending_action_injections ORDER BY id",
                )).fetchall()
            assert [dict(row) for row in stored_injections] == [{
                "action_id": action["action_id"], "base_node_id": root_id, "message": INJECTION,
            }]
            retry_steps = [
                ("submit_plan", {"plan": "Inspect only the requested surface without moving anything."}),
                ("update_state", {"variable_changes": [{"id": "test_inspected", "op": "inc", "value": 2}],
                                  "end_time": "2030-01-01T09:01:00"}),
                ("write_narrative", {"text": DRAFTS[-1]}),
                ("finalize_turn", {"summary": "Inspected the requested surface after retrying."}),
            ]
            retry_inputs = []

            async def retry_respond(messages, **kwargs):
                names = {item["function"]["name"] for item in kwargs["tools"]}
                if names == {"extract_notes"}:
                    name, arguments, step = "extract_notes", {"notes": []}, "notes"
                else:
                    retry_inputs.append(copy.deepcopy(messages))
                    step = sum(message.get("role") == "tool" for message in messages)
                    name, arguments = retry_steps[min(step, len(retry_steps) - 1)]
                assert name in names
                return {"content": "", "tool_calls": [{"id": f"synthetic-review-retry-{step}",
                                                        "name": name, "arguments": arguments}]}

            retry_review = AsyncMock(return_value={"pass": True, "violations": []})
            monkeypatch.setattr(session, "_review_narrative", retry_review)
            session.ai_provider = SimpleNamespace(
                generate_with_tools=AsyncMock(side_effect=retry_respond), generate=classification,
            )
            retry_action = {**pending_action["action"], "action_id": pending_action["actionId"],
                            "stream_id": pending_action["streamId"], "expected_node_id": pending_action["baseNodeId"]}
            retried = await asyncio.wait_for(client.post(endpoint, json=retry_action), 10)
            retry_events = _stream_events(retried.text)
            assert retried.status_code == 200
            assert not [event for event in retry_events if event["type"] in ("error", "agent_max_rounds", "aborted")]
            final, = [event for event in retry_events if event["type"] == "final"]
            assert final["narrative"] == DRAFTS[-1]
            assert [event["message"] for event in retry_events if event["type"] == "user_inject"] == [INJECTION]
            candidate_calls = [call for call in retry_review.await_args_list
                               if call.args[2].get("_review_execution", {}).get("scope") != "applied_turn"]
            assert len(candidate_calls) == 1
            assert INJECTION in retry_review.call_args.args[1]
            assert final["agent_trace"]["semantic_review_calls"] == 1
            assert_controls_closed(session)
            provider_calls = session.ai_provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=retry_action)).text == retried.text
            assert session.ai_provider.generate_with_tools.await_count == provider_calls
            async with database.get_db() as db:
                counts = {table: (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]
                          for table in ("tree_nodes", "action_requests", "pending_action_injections", "pending_action_plans")}
            assert counts == {"tree_nodes": 2, "action_requests": 1,
                              "pending_action_injections": 0, "pending_action_plans": 0}
            game_routes._action_streams.clear()
            assert (await client.post(endpoint, json=retry_action)).status_code == 409
            assert session.ai_provider.generate_with_tools.await_count == provider_calls
            completed = (await client.get(snapshot_url)).json()
            assert completed["action_completed"] is True
            assert completed["pending_action"] is None
            assert completed["node_id"] == final["node_id"]
            restored = await restore(session, sessions)
            for current in (session, restored):
                assert current.world_tree.active_node_id == final["node_id"]
                assert current.current_state["script_variables"]["test_inspected"] == 2
                assert current.current_state["game_time"] == "2030-01-01T09:01:00"
            assert (await stored_state(restored))["_node_context"]["agent_trace"] == final["agent_trace"]
            await restored.branch_to_node(root_id)
            assert restored.current_state["script_variables"]["test_inspected"] == 0
            await restored.branch_to_node(final["node_id"])
            assert restored.current_state["script_variables"]["test_inspected"] == 2
            assert restored.world_tree.get_node(final["node_id"])["ai_response"] == DRAFTS[-1]
            assert all(messages.count({"role": "user", "content": INJECTION}) == 1 for messages in retry_inputs)
            return
        assert not errors, errors
        final, = [event for event in events if event["type"] == "final"]
        expected = revised_drafts[-1]
        assert final["narrative"] == expected
        assert [item[0] for item in reviews] == DRAFTS[:prior_reviews] + revised_drafts
        assert all(INJECTION in item[1] for item in reviews[prior_reviews:])
        assert all(INJECTION not in item[1] for item in reviews[:prior_reviews])
        assert session._inject_queue == []
        trace = final["agent_trace"]
        assert trace["semantic_review_calls"] == prior_reviews + rejections + 1
        assert trace["semantic_review_model_calls"] == prior_reviews + rejections + 1
        assert trace["semantic_review_status"] == "passed"
        assert trace["primary_model_calls"] >= trace["model_calls"] + prior_reviews + rejections + 1
        model_calls = session.ai_provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert session.ai_provider.generate_with_tools.await_count == model_calls
        async with database.get_db() as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM tree_nodes")).fetchone())[0] == 2
            assert (await (await db.execute("SELECT COUNT(*) FROM action_requests")).fetchone())[0] == 1
            assert (await (await db.execute("SELECT COUNT(*) FROM pending_action_injections")).fetchone())[0] == 0
        restored = await restore(session, sessions)
        for current in (session, restored):
            assert current.world_tree.active_node_id == final["node_id"]
            assert current.current_state["script_variables"]["test_inspected"] == 2
            assert current.current_state["game_time"] == "2030-01-01T09:01:00"
        persisted = await stored_state(restored)
        assert persisted["script_variables"]["test_inspected"] == 2
        assert persisted["_node_context"]["agent_trace"] == trace
        await restored.branch_to_node(root_id)
        assert restored.current_state["script_variables"]["test_inspected"] == 0
        await restored.branch_to_node(final["node_id"])
        assert restored.current_state["script_variables"]["test_inspected"] == 2
        assert restored.world_tree.get_node(final["node_id"])["ai_response"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("failed", [False, True])
async def test_prequeued_instructions_reach_first_request_before_acknowledgment(count, failed):
    loop = PlanLoop([])
    injections = [f"Synthetic pending instruction {index}." for index in range(count)]
    loop._inject_queue.extend(injections)
    entered = asyncio.Event()
    release = asyncio.Event()
    inputs = []
    events = []

    async def respond(messages, **kwargs):
        inputs.append(copy.deepcopy(messages))
        entered.set()
        await release.wait()
        if failed:
            raise RuntimeError("SYNTHETIC_RETRY_PROVIDER_FAILURE")
        return {"content": "The requested inspection is complete.", "tool_calls": []}

    async def collect():
        async with aclosing(loop._agent_loop(
            [{"role": "user", "content": "Resume the inspection."}], "system", [], AsyncMock(), max_rounds=1,
        )) as stream:
            async for event in stream:
                events.append(event)

    loop.ai_provider.generate_with_tools = AsyncMock(side_effect=respond)
    pending = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert loop._inject_queue == injections
        assert not [event for event in events if event["type"] == "user_inject"]
        release.set()
        if failed:
            with pytest.raises(RuntimeError, match="SYNTHETIC_RETRY_PROVIDER_FAILURE"):
                await asyncio.wait_for(pending, 5)
        else:
            await asyncio.wait_for(pending, 5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert len(inputs) == 1
    assert inputs[0] == [{"role": "user", "content": "Resume the inspection."}] + [
        {"role": "user", "content": message} for message in injections
    ]
    acknowledged = [event["message"] for event in events if event["type"] == "user_inject"]
    assert acknowledged == ([] if failed else injections)
    assert loop._inject_queue == (injections if failed else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_review_budget_without_interjection_remains_three(tmp_path, monkeypatch, rejected):
    session = review_session()
    steps = [("submit_plan", {"plan": "Inspect the room without moving anything."})]
    for draft in DRAFTS:
        steps.append(("write_narrative", {"text": draft}))
        if rejected:
            steps.append(("finalize_turn", {"summary": "Inspected the room."}))
    steps.append(("finalize_turn", {"summary": "Inspected the room."}))

    async def respond(messages, **kwargs):
        names = {item["function"]["name"] for item in kwargs["tools"]}
        if names == {"extract_notes"}:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            step = sum(message.get("role") == "tool" for message in messages)
            name, arguments = steps[step]
            if not rejected and step == len(steps) - 1:
                result = json.loads([message["content"] for message in messages if message.get("role") == "tool"][-1])
                assert "error" in result and "上限" in result["error"]
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"synthetic-review-limit-{step}",
                                                "name": name, "arguments": arguments}]}

    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
        })),
    )
    review = AsyncMock(return_value={"pass": not rejected, "violations": [{
        "type": "unsupported_fact", "detail": "Revise this synthetic inspection description.",
    }] if rejected else []})
    monkeypatch.setattr(session, "_review_narrative", review)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_state(session)
        persisted_before = await database_state(session)
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the room.", "intent_hint": "explicit_action",
        }), 10)
        events = _stream_events(response.text)
        candidate_calls = [call for call in review.await_args_list
                           if call.args[2].get("_review_execution", {}).get("scope") != "applied_turn"]
        assert len(candidate_calls) == 3
        assert_controls_closed(session)
        if rejected:
            assert [event for event in events if event["type"] == "error"] == [
                {"type": "error", "message": "叙事语义审查连续未通过，本轮未保存，请重试", "discard_partial": True},
            ]
            assert not any(event["type"] == "final" for event in events)
            assert runtime_state(session) == before
            assert await database_state(session) == persisted_before
        else:
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == DRAFTS[2]
            assert final["agent_trace"]["semantic_review_calls"] == 3
            writes = [event for event in events
                      if event["type"] == "tool_call" and event.get("tool_name") == "write_narrative"]
            assert len(writes) == 4
            restored = await restore(session, sessions)
            assert restored.world_tree.get_node(final["node_id"])["ai_response"] == DRAFTS[2]
