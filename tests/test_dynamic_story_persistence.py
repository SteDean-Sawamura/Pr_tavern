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
from engine.game_session import GameSession


def make_session(static_tree=True):
    script = {
        "script_id": "synthetic-dynamic-story", "name": "Synthetic dynamic story", "settings": {},
        "variables": [{"id": "test_progress", "type": "number", "default": 0}],
        "one_time_events": [{"id": "test_reserved", "trigger_time": "2030-01-02T09:00:00"}],
    }
    if static_tree:
        script["story_tree"] = {"trees": [{"id": "test_static_tree", "nodes": [{
            "id": "test_anchor", "type": "quest", "name": "Synthetic anchor",
        }]}]}
    session = GameSession(script, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session, script


def dynamic_node(node_id, amount=1):
    return {
        "id": node_id, "name": "Synthetic step", "type": "auto",
        "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": amount}]},
    }


def inject_nodes(session, nodes):
    session._apply_director_results({"tree": {"new_nodes": nodes}})


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_count", [1, 25])
@pytest.mark.parametrize("static_tree", [False, True])
async def test_dynamic_tree_definitions_follow_snapshot_without_log_clipping(tree_count, static_tree):
    session, script = make_session(static_tree)
    root_id = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                         state_snapshot=copy.deepcopy(session.current_state))
    for index in range(tree_count):
        session.turn_number = index + 1
        inject_nodes(session, [dynamic_node(f"test_dynamic_{index}")])
    final_id = session.world_tree.add_node(root_id, session.current_state["game_time"], tree_count,
                                          state_snapshot=copy.deepcopy(session.current_state))
    saved = json.loads(json.dumps(session.current_state))
    restored = GameSession(script, None)
    restored.current_state = saved
    restored._restore_dynamic_story()
    restored._hydrate_branch_runtime(saved)
    assert len(saved["dynamic_story_content"]["trees"]) == tree_count
    assert "test_dynamic_0" in restored.story_tree_engine._nodes
    assert "test_dynamic_0" in restored.event_engine.events
    restored._fire_lifecycle_event("before_generation")
    assert restored.current_state["script_variables"]["test_progress"] == tree_count
    await session.branch_to_node(root_id)
    assert not session.story_tree_engine or "test_dynamic_0" not in session.story_tree_engine._nodes
    assert "test_dynamic_0" not in session.event_engine.events
    assert "test_dynamic_0" not in session.event_engine._tree_for_event
    assert all(not tree["id"].startswith("_dyn_director_") for tree in session.event_engine._trees)
    assert not session._dynamic_story_tree_ids
    assert (session.story_tree_engine is not None) is static_tree
    assert session.event_engine.shadow_story_nodes is static_tree
    session._fire_lifecycle_event("before_generation")
    assert session.current_state["script_variables"]["test_progress"] == 0
    await session.branch_to_node(final_id)
    assert session.event_engine.shadow_story_nodes is True
    session._fire_lifecycle_event("before_generation")
    assert session.current_state["script_variables"]["test_progress"] == tree_count
    completed = copy.deepcopy(session.current_state)
    session._hydrate_branch_runtime(session.current_state)
    session._fire_lifecycle_event("before_generation")
    assert session.current_state["script_variables"] == completed["script_variables"]
    if static_tree:
        assert "test_anchor" in session.story_tree_engine._nodes
        assert "test_anchor" in session.event_engine.events


@pytest.mark.asyncio
async def test_same_tree_and_node_ids_restore_sibling_definitions_and_effects():
    session, _ = make_session()
    root_id = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                         state_snapshot=copy.deepcopy(session.current_state))
    inject_nodes(session, [dynamic_node("test_shared", 2)])
    left_id = session.world_tree.add_node(root_id, session.current_state["game_time"], 1,
                                         state_snapshot=copy.deepcopy(session.current_state))
    await session.branch_to_node(root_id)
    inject_nodes(session, [dynamic_node("test_shared", 7)])
    right_id = session.world_tree.add_node(root_id, session.current_state["game_time"], 1,
                                          state_snapshot=copy.deepcopy(session.current_state))
    for node_id, amount in [(left_id, 2), (right_id, 7), (left_id, 2)]:
        await session.branch_to_node(node_id)
        assert session.story_tree_engine._nodes["test_shared"]["effects"] == dynamic_node("test_shared", amount)["effects"]
        assert session.event_engine.events["test_shared"].effects == dynamic_node("test_shared", amount)["effects"]
        session._fire_lifecycle_event("before_generation")
        assert session.current_state["script_variables"]["test_progress"] == amount


@pytest.mark.asyncio
async def test_failed_generation_removes_only_unaccepted_dynamic_tree_runtime():
    session, _ = make_session()
    inject_nodes(session, [dynamic_node("test_accepted")])
    session._hydrate_branch_runtime(session.current_state)
    snapshot = game_routes._capture_generation_state(session)
    session.turn_number += 1
    inject_nodes(session, [dynamic_node("test_failed", 9)])
    await game_routes._rollback_generation_state(session, snapshot)
    assert "test_failed" not in session.story_tree_engine._nodes
    assert "test_failed" not in session.event_engine.events
    assert "test_failed" not in session.event_engine._tree_for_event
    session._fire_lifecycle_event("before_generation")
    assert session.current_state["script_variables"]["test_progress"] == 1
    assert "test_anchor" in session.story_tree_engine._nodes


def test_registration_uses_same_accepted_nodes_in_both_engines_without_mutating_input():
    session, _ = make_session()
    accepted = {**dynamic_node("test_choice"), "type": "choice", "choices": [{"id": "test_option"}],
                "effects": {"notify": "SYNTHETIC_INTERNAL_NOTICE", "narrative_callback": "SYNTHETIC_INTERNAL_RULE"}}
    invalid = {**dynamic_node("test_invalid"), "requires": ["test_missing"]}
    nodes = [accepted, invalid, dynamic_node("test_reserved"), dynamic_node("test_choice")]
    original = copy.deepcopy(nodes)
    inject_nodes(session, nodes)
    assert nodes == original
    saved_tree, = session.current_state["dynamic_story_content"]["trees"]
    assert [node["id"] for node in saved_tree["nodes"]] == ["test_choice"]
    assert "test_invalid" not in session.story_tree_engine._nodes
    assert "test_invalid" not in session.event_engine.events
    assert "test_reserved" not in session.story_tree_engine._nodes
    assert session.event_engine.events["test_reserved"].category == "scheduled"
    assert session.event_engine.events["test_choice"].category == "story"
    assert session.event_engine.events["test_choice"].effects == {}
    accepted["name"] = "Synthetic caller mutation"
    session.story_tree_engine._nodes["test_choice"]["name"] = "Synthetic runtime mutation"
    session._hydrate_branch_runtime(session.current_state)
    assert session.story_tree_engine._nodes["test_choice"]["name"] == "Synthetic step"


def test_same_turn_additions_merge_without_duplicate_tree_headers_or_nodes():
    session, _ = make_session()
    inject_nodes(session, [dynamic_node("test_first")])
    inject_nodes(session, [dynamic_node("test_first"), dynamic_node("test_second")])
    inject_nodes(session, [dynamic_node("test_first")])
    trees = session.current_state["dynamic_story_content"]["trees"]
    assert len(trees) == 1
    assert [node["id"] for node in trees[0]["nodes"]] == ["test_first", "test_second"]
    assert sum(tree["id"] == trees[0]["id"] for tree in session.story_tree_engine.trees) == 1
    assert sum(tree["id"] == trees[0]["id"] for tree in session.event_engine._trees) == 1


def test_dynamic_tree_cannot_overwrite_static_tree_identity():
    session, _ = make_session()
    original = copy.deepcopy(session.story_tree_engine.trees)
    session._inject_dynamic_tree({"id": "test_static_tree", "nodes": [dynamic_node("test_collision")]})
    assert session.story_tree_engine.trees == original
    assert "test_collision" not in session.event_engine.events


@pytest.mark.asyncio
async def test_dynamic_tree_restoration_preserves_system_trigger_definition_and_effects():
    _, script = make_session(False)
    script["triggers"] = [{
        "id": "test_system_signal", "event": "test_signal", "action": "set_var",
        "params": {"var_id": "test_progress", "op": "inc", "value": 2},
    }]
    session = GameSession(script, None)
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    root_id = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                         state_snapshot=copy.deepcopy(session.current_state))
    inject_nodes(session, [{**dynamic_node("test_waiting"), "condition": "script_variables.test_progress >= 100"}])
    final_id = session.world_tree.add_node(root_id, session.current_state["game_time"], 1,
                                          state_snapshot=copy.deepcopy(session.current_state))
    for node_id in [root_id, final_id, root_id]:
        await session.branch_to_node(node_id)
        assert session.story_tree_engine._tree_for_node["test_system_signal"] == "__system_triggers"
        assert session.event_engine._tree_for_event["test_system_signal"] == "__system_triggers"
        assert session.event_engine.shadow_story_nodes is True
        session._fire_lifecycle_event("test_signal")
        assert session.current_state["script_variables"]["test_progress"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind", ["tree", "event"])
async def test_sibling_tree_and_scheduled_event_can_reuse_id_after_runtime_is_cleared(first_kind):
    session, _ = make_session()
    root_id = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                         state_snapshot=copy.deepcopy(session.current_state))
    branches = {}
    for kind in [first_kind, "event" if first_kind == "tree" else "tree"]:
        await session.branch_to_node(root_id)
        if kind == "tree":
            inject_nodes(session, [dynamic_node("test_shared")])
        else:
            session._apply_director_results({"events": {"one_time": [{
                "id": "test_shared", "trigger_time": "2030-01-01T09:01:00",
            }]}})
        branches[kind] = session.world_tree.add_node(root_id, session.current_state["game_time"], 1,
                                                    state_snapshot=copy.deepcopy(session.current_state))
    for kind in ["tree", "event", "tree"]:
        await session.branch_to_node(branches[kind])
        assert session.event_engine.events["test_shared"].category == ("story" if kind == "tree" else "scheduled")
        assert ("test_shared" in session.story_tree_engine._nodes) is (kind == "tree")
        assert ("test_shared" in session._event_def_by_id) is (kind == "event")


def test_target_snapshot_restoration_preserves_timed_progress_and_current_state():
    session, _ = make_session()
    current = copy.deepcopy(session.current_state)
    target = copy.deepcopy(current)
    target["dynamic_story_content"] = {"trees": [{"id": "test_timed_tree", "nodes": [{
        "id": "test_timed", "type": "timed", "duration_turns": 6,
    }]}]}
    target["story_tree_state"] = {"active": ["test_timed"], "timed_progress": {
        "test_timed": {"started_turn": 1, "last_advanced_turn": 3, "remaining": 4},
    }}
    expected = copy.deepcopy(target)
    session._hydrate_branch_runtime(target)
    session._hydrate_branch_runtime(target)
    assert target == expected
    assert session.current_state == current
    assert session.story_tree_engine._nodes["test_timed"]["duration_turns"] == 6
    session._hydrate_branch_runtime(current)
    assert "test_timed" not in session.event_engine.events


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_mode", ["reload", "branch", "failed"])
@pytest.mark.parametrize("threshold", [7, 13])
async def test_dynamic_story_effects_reach_next_agent_turn_after_stream_reload(tmp_path, monkeypatch, resume_mode, threshold):
    _, script = make_session(False)
    script["locations"] = [{"id": "test_room", "name": "Reading room"}]
    script["variables"].append({"id": "test_gate", "type": "number", "default": 0})
    script["lorebook"] = [{"id": "test_detail", "name": "Synthetic detail", "enabled": False,
                           "content": "SYNTHETIC_DYNAMIC_STORY_DETAIL", "keys": ["shelf"]}]
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = [[], []]
    turn_index = 0
    inject_failure = False

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        if inject_failure:
            inject_nodes(current, [dynamic_node("test_failed_stream", 100)])
            raise RuntimeError("Synthetic dynamic story generation failure")
        requests[turn_index].append(copy.deepcopy(messages))
        index = len(requests[turn_index]) - 1
        operations = [
            ("submit_plan", {"plan": "Inspect the shelf according to the current rules."}),
            ("query_lorebook", {"entry_id": "test_detail"}),
            ("update_state", {"end_time": f"2030-01-01T09:0{turn_index + 1}:00"}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Inspected the empty shelf."}),
        ]
        assert index < len(operations)
        name, arguments = operations[index]
        if index == 2:
            rules, = [json.loads(message["content"]) for message in messages
                      if message.get("tool_call_id") == f"synthetic-node-{turn_index}"]
            node = json.loads(rules["entries"][0]["content"])["definition"]["node"]
            assert node["condition"].startswith("script_variables.")
            variable_id, minimum = node["condition"].removeprefix("script_variables.").split(" >= ")
            arguments["variable_changes"] = [{"id": variable_id, "op": "set", "value": int(minimum)}]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        calls = [{"id": f"synthetic-story-{turn_index}-{index}", "name": name, "arguments": arguments}]
        if index == 1:
            calls.append({"id": f"synthetic-progress-{turn_index}", "name": "consult_rules",
                          "arguments": {"category": "variables", "query": "test_progress"}})
            calls.append({"id": f"synthetic-node-{turn_index}", "name": "consult_rules",
                          "arguments": {"category": "story_nodes", "query": "test_story_step"}})
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
    session = GameSession(script, provider, save_id="synthetic-dynamic-story-save")
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, root_state["game_time"], 0, state_snapshot=root_state)
    definition = dynamic_node("test_story_step", 3)
    definition["condition"] = f"script_variables.test_gate >= {threshold}"
    definition["rule_notes"] = "SYNTHETIC_STORY_RULE_DETAIL"
    definition["effects"]["activate_lore"] = ["test_detail"]
    inject_nodes(session, [definition])
    current = session
    sessions = [session]
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dynamic-story.db")
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

    def stream_events(text):
        return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]

    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": narrative})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for turn_index in range(2):
                action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                          "stream_id": f"synthetic-story-stream-{turn_index}", "action_id": f"synthetic-story-action-{turn_index}",
                          "expected_node_id": current.world_tree.active_node_id}
                if turn_index and resume_mode == "failed":
                    before_failure = copy.deepcopy(current.current_state)
                    inject_failure = True
                    failed = await asyncio.wait_for(client.post(endpoint, json={**action, "action_id": "synthetic-failure"}), 5)
                    inject_failure = False
                    assert any(event["type"] == "error" for event in stream_events(failed.text))
                    assert current.current_state == before_failure
                    assert "test_failed_stream" not in current.story_tree_engine._nodes
                    assert "test_failed_stream" not in current.event_engine.events
                response = await asyncio.wait_for(client.post(endpoint, json=action), 5)
                assert response.status_code == 200
                events = stream_events(response.text)
                assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert current.current_state["script_variables"]["test_progress"] == 3
                assert current.current_state["script_variables"]["test_gate"] == threshold
                assert "test_story_step" in current.current_state["story_tree_state"]["completed"]
                assert "SYNTHETIC_DYNAMIC_STORY_DETAIL" not in response.text
                assert "SYNTHETIC_STORY_RULE_DETAIL" not in response.text
                assert "SYNTHETIC_STORY_RULE_DETAIL" in json.dumps(requests[turn_index][2])
                assert "dynamic_story_content" not in response.text
                trace = current.world_tree.get_node(final["node_id"])["agent_trace"]
                assert trace
                assert "SYNTHETIC_STORY_RULE_DETAIL" not in json.dumps(trace)
                assert sum(record["tool"] == "consult_rules" for record in current._agent_traces[-1]["rounds"]) == 2
                assert ("SYNTHETIC_DYNAMIC_STORY_DETAIL" in json.dumps(requests[turn_index][2])) is bool(turn_index)
                rules, = [json.loads(message["content"]) for message in requests[turn_index][2]
                          if message.get("tool_call_id") == f"synthetic-progress-{turn_index}"]
                assert json.loads(rules["entries"][0]["content"])["current_value"] == (3 if turn_index else 0)
                await current._drain_background_tasks()
                model_calls = provider.generate_with_tools.await_count
                state = copy.deepcopy(current.current_state)
                replay = await client.post(endpoint, json=action)
                assert stream_events(replay.text) == events
                assert provider.generate_with_tools.await_count == model_calls
                assert current.current_state == state
                if not turn_index and resume_mode in {"reload", "branch"}:
                    game_routes._sessions.clear()
                    current = await game_routes._restore_session(session.save_id)
                    assert current is not None and current is not session
                    sessions.append(current)
                    game_routes._sessions[session.save_id] = current
                    if resume_mode == "branch":
                        await current.branch_to_node(root_id)
                        assert not current.story_tree_engine or "test_story_step" not in current.story_tree_engine._nodes
                        assert "test_story_step" not in current.event_engine.events
                        await current.branch_to_node(final["node_id"])
                    assert current.story_tree_engine._nodes["test_story_step"]["effects"] == definition["effects"]
                    assert current.current_state["script_variables"]["test_progress"] == 3
    finally:
        for active in sessions:
            await active._drain_background_tasks()
        await database.close_pool()
