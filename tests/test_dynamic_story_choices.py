import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_declared_choice_preview import make_session as static_session, preview
from test_player_story_choices import player_choices
from test_story_choice_settlement import choice_client, stored_state


def make_session(threshold=7, *, auto_resolve=False):
    original = static_session(threshold)
    script = copy.deepcopy(original.script)
    nodes = script.pop("story_tree")["trees"][0]["nodes"]
    nodes[0]["description"] = "Choose how to inspect the shelf."
    nodes[0]["auto_resolve"] = auto_resolve
    nodes[0]["choices"].append({
        "id": "test_wait", "label": "Wait beside the shelf",
        "condition": f"script_variables.test_gate < {threshold}",
        "effects": {"set_var": [{"var_id": "test_reward", "op": "inc", "value": 10}]},
    })
    session = GameSession(script, None, save_id="synthetic-dynamic-choice-save")
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state.pop("story_tree_state")
    session.current_state["script_variables"]["test_gate"] = threshold
    session._ensure_identity_lore()
    return session, nodes


async def declare_choice(session, nodes):
    provider = SimpleNamespace(generate=AsyncMock(return_value=json.dumps({"new_nodes": nodes})))
    session.ai_provider = provider
    result = await session._director_task_tree({
        "current_bp": {"plot_threads": [{"name": "Shelf inspection", "stages": [
            {"status": "active", "description": "Let the visitor choose how to proceed."},
        ]}]},
        "world_bg": "A synthetic reading room.", "game_time": session.current_state["game_time"],
        "recent_events": "The visitor stopped beside the shelf.",
        "dynamic_node_ids": [], "script_tree_ids": [],
    })
    assert result == {"new_nodes": nodes}
    session._apply_director_results({"tree": result})
    return provider.generate.call_args.args[0][0]["content"]


@pytest.mark.asyncio
async def test_director_contract_and_registration_preserve_dynamic_choices():
    session, nodes = make_session()
    original = copy.deepcopy(nodes)
    prompt = await declare_choice(session, nodes)
    assert "- choice:" in prompt and '"choices"' in prompt
    saved = session.current_state["dynamic_story_content"]["trees"][0]["nodes"][0]
    assert saved["type"] == "choice" and saved["choices"] == original[0]["choices"]
    assert session.event_engine.events["test_decision"].category == "choice"
    assert session.event_engine.events["test_decision"].choices == saved["choices"]
    assert nodes == original


@pytest.mark.parametrize("choices", [None, [], "invalid", [None], [{}], [{"id": ""}],
                                     [{"id": []}], [{"id": "duplicate"}, {"id": "duplicate"}]])
def test_invalid_dynamic_choice_is_rejected_instead_of_autocompleting(choices):
    session, nodes = make_session()
    nodes[0]["choices"] = choices
    before = copy.deepcopy(nodes)
    session._apply_director_results({"tree": {"new_nodes": [nodes[0]]}})
    assert "test_decision" not in session.event_engine.events
    assert session.story_tree_engine is None or "test_decision" not in session.story_tree_engine._nodes
    assert session.current_state["dynamic_story_content"]["trees"] == []
    assert nodes == before


@pytest.mark.parametrize("source", ["static", "dynamic"])
def test_director_cannot_mark_player_choice_complete(source):
    if source == "static":
        session = static_session()
    else:
        session, nodes = make_session()
        session._apply_director_results({"tree": {"new_nodes": nodes}})
        session._fire_lifecycle_event("before_generation")
    assert "test_decision" in session.current_state["story_tree_state"]["active"]
    before = copy.deepcopy(session.current_state["story_tree_state"])
    session._apply_director_results({"tree": {"complete_nodes": ["test_decision"]}})
    assert session.current_state["story_tree_state"] == before
    assert session.current_state["script_variables"]["test_reward"] == 0


@pytest.mark.parametrize("value", ["false", "true", 1])
def test_nonboolean_auto_resolution_does_not_take_over_player_choice(value):
    session, nodes = make_session()
    nodes[0]["auto_resolve"] = value
    session._apply_director_results({"tree": {"new_nodes": nodes}})
    session._fire_lifecycle_event("before_generation")
    assert "test_decision" in session.current_state["story_tree_state"]["active"]
    assert session.current_state["script_variables"]["test_reward"] == 0
    assert session.current_state["dynamic_story_content"]["trees"][0]["nodes"][0]["auto_resolve"] is False


@pytest.mark.parametrize("gate,chosen,reward", [(0, "test_wait", 11), (7, "test_open", 4)])
def test_explicit_dynamic_auto_resolution_preserves_selected_effects(gate, chosen, reward):
    session, nodes = make_session(auto_resolve=True)
    session.current_state["script_variables"]["test_gate"] = gate
    session._apply_director_results({"tree": {"new_nodes": nodes}})
    session._fire_lifecycle_event("before_generation")
    state = session.current_state
    assert state["story_tree_state"]["choices_made"] == {"test_decision": chosen}
    assert state["script_variables"]["test_reward"] == reward
    saved = copy.deepcopy(state)
    session._hydrate_branch_runtime(state)
    session._fire_lifecycle_event("before_generation")
    assert state["script_variables"]["test_reward"] == reward
    assert state["story_tree_state"]["choices_made"] == saved["story_tree_state"]["choices_made"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", ["cached", "reload", "branch"])
@pytest.mark.parametrize("fail_save", [False, True])
@pytest.mark.parametrize("node_id", ["test_decision", "_dyn_test_decision"])
async def test_dynamic_choice_flows_from_director_through_player_commit_and_next_agent_turn(
    tmp_path, monkeypatch, resume, fail_save, node_id,
):
    session, nodes = make_session()
    nodes[0]["id"] = node_id
    for node in nodes:
        node["requires"] = [node_id if required == "test_decision" else required
                            for required in node.get("requires", [])]
    turn = 0
    steps = []
    narratives = [
        "You stop beside the shelf and consider the available options carefully. "
        "You leave everything in place while deciding how to proceed, keeping the area clear.",
        "You inspect the shelf after making your choice and read the newly available detail. "
        "You finish the inspection without disturbing the room, then record what you learned.",
    ]

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments = "classify_intent", {"intent": "action"}
        elif names == ["extract_notes"]:
            name, arguments = "extract_notes", {"notes": []}
        else:
            step = len(steps)
            steps.append(step)
            if step == 0:
                name, arguments = "submit_plan", {"plan": "Read the current instruction before proceeding."}
            elif step == 1:
                if turn == 0:
                    name, arguments = "consult_rules", {"category": "story_nodes", "query": node_id, "view": "runtime"}
                else:
                    name, arguments = "query_lorebook", {"entry_id": "test_detail"}
            elif step == 2:
                receipt = next(message for message in reversed(messages)
                               if message.get("tool_call_id") == f"choice-{turn}-1")
                entries = json.loads(receipt["content"])["entries"]
                assert len(entries) == 1
                name, arguments = "preview_choice_outcome", {"node_id": node_id, "choice_id": "test_open"}
                if turn == 0:
                    runtime = json.loads(entries[0]["content"])
                    assert runtime["node_id"] == node_id and runtime["status"] == "active"
                    available = [choice for choice in runtime["choices"] if choice["available"]]
                    assert [choice["choice_id"] for choice in available] == ["test_open"]
                    arguments = {"node_id": runtime["node_id"], "choice_id": available[0]["choice_id"]}
                else:
                    assert "SYNTHETIC_PREVIEW_DETAIL" in entries[0]["content"]
            elif step == 3:
                receipt = next(message for message in reversed(messages)
                               if message.get("tool_call_id") == f"choice-{turn}-2")
                outcome = json.loads(receipt["content"])
                assert outcome["available"] is (turn == 0)
                assert session.current_state["script_variables"]["test_reward"] == (0 if turn == 0 else 4)
                name, arguments = "write_narrative", {"text": narratives[turn]}
            else:
                assert step == 4
                name, arguments = "finalize_turn", {"summary": "Considered the shelf inspection."}
        assert name in names
        return {"content": "", "tool_calls": [{
            "id": f"choice-{turn}-{len(steps) - 1}", "name": name, "arguments": arguments,
        }]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        await declare_choice(session, nodes)
        session.ai_provider = provider
        endpoint = f"/api/game/{session.save_id}/action/stream"
        choice_endpoint = f"/api/game/{session.save_id}/story-tree"
        action = {"type": "freeform", "text": "Consider how to inspect the shelf.",
                  "action_id": "synthetic-before-choice", "expected_node_id": root_id}
        response = await client.post(endpoint, json=action)
        events = _stream_events(response.text)
        assert not [event for event in events if event["type"] == "error"], response.text
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == narratives[0]
        assert steps == list(range(5))
        ready_id = final["node_id"]
        assert session.current_state["script_variables"]["test_reward"] == 0
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        if resume == "reload":
            game_routes._sessions.clear()
            session = await game_routes._restore_session(session.save_id)
            assert session is not None
            sessions.append(session)
        elif resume == "branch":
            await session.branch_to_node(root_id)
            assert player_choices(await client.get(choice_endpoint + "/active-choices"), "active-choices") == {}
            await session.branch_to_node(ready_id)
        before = copy.deepcopy(session.current_state)
        stored_before = await stored_state(session)
        for key in ("story_tree_state", "script_variables", "events", "dynamic_story_content"):
            assert stored_before[key] == before[key]
        pending = await client.get(choice_endpoint + "/active-choices")
        assert player_choices(pending, "active-choices") == {node_id: ["test_open"]}
        assert pending.json()["base_node_id"] == ready_id
        assert player_choices(await client.get(choice_endpoint), "tree") == {node_id: ["test_open"]}
        assert (await preview(session, node_id=node_id))["available"] is True
        assert session.current_state == before
        assert await stored_state(session) == stored_before
        payload = {"node_id": node_id, "choice_id": "test_open", "expected_node_id": ready_id}
        rejected = await client.post(choice_endpoint + "/choice", json={**payload, "choice_id": "test_wait"})
        assert rejected.status_code == 200 and rejected.json()["success"] is False
        if fail_save:
            persist = game_routes._persist_active_state

            async def reject_save(current):
                assert current.current_state["script_variables"]["test_reward"] == 4
                raise RuntimeError("Synthetic choice persistence interruption")

            monkeypatch.setattr(game_routes, "_persist_active_state", reject_save)
            failed = await client.post(choice_endpoint + "/choice", json=payload)
            assert failed.status_code == 500
            assert session.current_state == before and await stored_state(session) == stored_before
            assert (await preview(session, node_id=node_id))["available"] is True
            monkeypatch.setattr(game_routes, "_persist_active_state", persist)
        chosen = await client.post(choice_endpoint + "/choice", json=payload)
        assert chosen.status_code == 200 and chosen.json()["success"] is True
        assert session.current_state["script_variables"]["test_reward"] == 4
        assert session.current_state["story_tree_state"]["choices_made"] == {node_id: "test_open"}
        assert session.current_state["events"][node_id]["choices_made"] == "test_open"
        assert (await stored_state(session))["script_variables"]["test_reward"] == 4
        replay = await client.post(choice_endpoint + "/choice", json={
            **payload, "expected_node_id": session.world_tree.active_node_id,
        })
        assert replay.status_code == 200 and replay.json()["success"] is False
        game_routes._sessions.clear()
        session = await game_routes._restore_session(session.save_id)
        assert session is not None
        sessions.append(session)
        session.ai_provider = provider
        turn = 1
        steps.clear()
        resumed = await client.post(endpoint, json={
            "type": "freeform", "text": "Read the result of the inspection.",
            "action_id": "synthetic-after-choice", "expected_node_id": session.world_tree.active_node_id,
        })
        events = _stream_events(resumed.text)
        assert not [event for event in events if event["type"] == "error"], resumed.text
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == narratives[1] and steps == list(range(5))
        assert (await stored_state(session))["script_variables"]["test_reward"] == 4
        trace = await game_routes.get_tree_node(session.save_id, final["node_id"])
        for marker in ("SYNTHETIC_PREVIEW_DETAIL", "SYNTHETIC_PREVIEW_EFFECT", "SYNTHETIC_PREVIEW_CALLBACK"):
            assert marker not in response.text + chosen.text + resumed.text
            assert marker not in json.dumps(trace["agent_trace"], ensure_ascii=False)
