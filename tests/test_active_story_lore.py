import copy
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client, persist_node, stored_state


START = "2030-01-01T09:00:00"
MARKER = "SYNTHETIC_ACTIVE_STORY_DETAIL"
ENTRY_ID = "_st_active_test_instruction"
CASES = [(source, kind) for source in ("static", "dynamic")
         for kind in ("choice", "quest", "timed", "pending_review")
         if (source, kind) != ("dynamic", "choice")]
CASES += [("event", kind) for kind in ("choice", "quest")]


def make_session(source="static", kind="choice"):
    node = {
        "id": "test_instruction", "name": "Practice instruction", "type": kind,
        "description": MARKER + " Keep the practice notebook open until the check is complete.",
        "activate_events": ["test_open"], "duration_turns": 5,
        "choices": [{"id": "test_continue", "label": "Continue"}],
        "effects": {"set_var": [{"var_id": "test_progress", "op": "inc", "value": 1}]},
    }
    script = {
        "script_id": "synthetic-active-story", "name": "Synthetic active story", "settings": {},
        "start_time": START, "variables": [{"id": "test_progress", "type": "number", "default": 0}],
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Practice room"}],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [node]}]} if source != "dynamic" else {},
    }
    session = GameSession(script, None, save_id="synthetic-active-story-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    if source == "dynamic":
        session._apply_director_results({"tree": {"new_nodes": [node]}})
    if source == "event":
        session.story_tree_engine = None
        session.event_engine.shadow_story_nodes = False
    session._ensure_identity_lore()
    session._fire_lifecycle_event("before_generation")
    return session


def activate(session):
    session._fire_event_dual("test_open")
    session._fire_lifecycle_event("before_generation")
    if session.story_tree_engine is None:
        result = session.event_engine.tick(session.current_state, session.turn_number,
                                           condition_eval=session._evaluate_condition, advance_timed=False)
        session._apply_event_result(result)


async def read_active(session):
    result = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": ENTRY_ID}))
    return [entry["content"] for entry in result["entries"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,kind", CASES)
async def test_active_story_lore_is_derived_from_saved_state_without_replaying_lifecycle(monkeypatch, source, kind):
    session = make_session(source, kind)
    activate(session)
    expected = await read_active(session)
    assert expected and MARKER in expected[0]
    saved = json.loads(json.dumps(session.current_state))
    restored = GameSession(copy.deepcopy(session.script), None)
    restored.current_state = copy.deepcopy(saved)
    if source == "event":
        restored.story_tree_engine = None
        restored.event_engine.shadow_story_nodes = False
    replay = Mock(side_effect=AssertionError("Hydration must not advance story lifecycle"))
    monkeypatch.setattr(restored, "_fire_lifecycle_event", replay)
    for _ in range(2):
        restored._hydrate_branch_runtime(restored.current_state)
        assert await read_active(restored) == expected
        assert restored.current_state == saved
        assert [entry.id for entry in restored.prompt_builder.lorebook.entries].count(ENTRY_ID) == 1
    replay.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,kind", CASES)
async def test_active_story_lore_does_not_leak_across_branches(source, kind):
    session = make_session(source, kind)
    idle = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, START, 0, state_snapshot=idle)
    activate(session)
    expected = await read_active(session)
    assert expected and MARKER in expected[0]
    active = copy.deepcopy(session.current_state)
    child_id = session.world_tree.add_node(root_id, START, 1, state_snapshot=active)
    for node_id, contents in ((root_id, []), (child_id, expected), (root_id, []), (child_id, expected)):
        await session.branch_to_node(node_id)
        assert await read_active(session) == contents
        scanned, _ = session.prompt_builder.scan_lorebook("Practice instruction", [], state=session.current_state)
        assert (ENTRY_ID in {entry.id for entry in scanned}) is bool(contents)
    assert session.current_state == active


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["updated", "disabled", "removed"])
async def test_active_story_hydration_respects_lore_overrides(change):
    session = make_session()
    activate(session)
    state = copy.deepcopy(session.current_state)
    if change == "updated":
        entry = asdict(next(entry for entry in session.prompt_builder.lorebook.entries if entry.id == ENTRY_ID))
        entry["content"] = "SYNTHETIC_REVISED_ACTIVE_DETAIL"
        state["lorebook_entry_overrides"] = {ENTRY_ID: entry}
    elif change == "disabled":
        state["lorebook_enabled_overrides"] = {ENTRY_ID: False}
    else:
        state["lorebook_entry_overrides"] = {ENTRY_ID: None}
    original = copy.deepcopy(state)
    session._hydrate_branch_runtime(state)
    session.current_state = state
    assert await read_active(session) == (["SYNTHETIC_REVISED_ACTIVE_DETAIL"] if change == "updated" else [])
    assert state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_completed_story_does_not_revive_overridden_active_lore(fresh):
    session = make_session()
    activate(session)
    session._apply_lorebook_changes(updates=[{"id": ENTRY_ID, "content": "SYNTHETIC_REVISED_ACTIVE_DETAIL"}])
    result = session.story_tree_engine.make_choice(session.current_state, "test_instruction", "test_continue")
    session._apply_story_tree_result(result)
    session.event_engine.sync_story_tree_state(session.current_state)
    assert await read_active(session) == []
    saved = copy.deepcopy(session.current_state)
    if fresh:
        session = GameSession(copy.deepcopy(session.script), None)
        session.current_state = copy.deepcopy(saved)
    session._hydrate_branch_runtime(session.current_state)
    assert await read_active(session) == []
    assert session.current_state == saved


@pytest.mark.asyncio
async def test_active_story_hydration_uses_explicit_target_and_removes_absent_definitions():
    session = make_session("dynamic", "quest")
    activate(session)
    active = copy.deepcopy(session.current_state)
    original = session.current_state
    target = copy.deepcopy(active)
    target.pop("dynamic_story_content")
    session._hydrate_branch_runtime(target)
    assert session.current_state is original and session.current_state == active
    assert await read_active(session) == []
    session._hydrate_branch_runtime(active)
    assert MARKER in (await read_active(session))[0]
    assert session.current_state is original and session.current_state == active


@pytest.mark.asyncio
async def test_active_story_vector_index_tracks_branch_and_completion():
    session = make_session()
    idle = copy.deepcopy(session.current_state)
    activate(session)
    active = copy.deepcopy(session.current_state)
    indexed = {}
    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=lambda batch: indexed.update({entry_id: content for entry_id, content, _ in batch}),
        remove_lorebook=lambda entry_id: indexed.pop(entry_id, None),
    )
    for state, present in ((active, True), (idle, False), (active, True)):
        session.current_state = copy.deepcopy(state)
        session._hydrate_branch_runtime(session.current_state)
        await session._drain_background_tasks()
        assert (ENTRY_ID in indexed) is present
    result = session.story_tree_engine.make_choice(session.current_state, "test_instruction", "test_continue")
    session._apply_story_tree_result(result)
    session.event_engine.sync_story_tree_state(session.current_state)
    saved = copy.deepcopy(session.current_state)
    session._hydrate_branch_runtime(session.current_state)
    await session._drain_background_tasks()
    assert ENTRY_ID not in indexed
    assert await read_active(session) == []
    assert session.current_state == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["static", "dynamic"])
@pytest.mark.parametrize("resume", ["reload", "branch"])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_agent_reads_active_story_after_resume_and_save_rollback(tmp_path, monkeypatch, source, resume, fail_save):
    session = make_session(source, "quest")
    steps = []
    narrative = ("You keep the practice notebook open while waiting for the check to finish. "
                 "You leave the marked page untouched and stay beside the desk, ready to continue when called.")

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            return {"content": "", "tool_calls": [{"id": "active-intent", "name": "classify_intent",
                                                    "arguments": {"intent": "action"}}]}
        if names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "active-notes", "name": "extract_notes",
                                                    "arguments": {"notes": []}}]}
        step = len(steps)
        steps.append(step)
        if step == 0:
            name, arguments = "submit_plan", {"plan": "Read the current instruction and follow it while waiting."}
        elif step == 1:
            name, arguments = "query_lorebook", {"keyword": "Practice instruction"}
        elif step == 2:
            receipt = next(message for message in reversed(messages) if message.get("tool_call_id") == "active-step-1")
            entries = json.loads(receipt["content"])["entries"]
            assert [entry["id"] for entry in entries] == [ENTRY_ID]
            assert MARKER in entries[0]["content"]
            name, arguments = "write_narrative", {"text": narrative}
        else:
            assert step == 3
            name, arguments = "finalize_turn", {"summary": "Kept the practice notebook ready for the check."}
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"active-step-{step}", "name": name, "arguments": arguments}]}

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
        activate(session)
        expected = await read_active(session)
        assert expected and MARKER in expected[0]
        active_id = await persist_node(session, root_id)
        if resume == "reload":
            game_routes._sessions.clear()
            session = await game_routes._restore_session(session.save_id)
            assert session is not None
            sessions.append(session)
        else:
            await session.branch_to_node(root_id)
            assert await read_active(session) == []
            await session.branch_to_node(active_id)
        session.ai_provider = provider
        baseline = copy.deepcopy(session.current_state)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Follow the current practice instruction while waiting.",
                  "action_id": "synthetic-active-story-action", "expected_node_id": active_id}
        if fail_save:
            save_node = game_routes._save_node

            async def reject_save(*args, **kwargs):
                assert await read_active(session) == expected
                raise RuntimeError("Synthetic active story save interruption")

            monkeypatch.setattr(game_routes, "_save_node", reject_save)
            failed = await client.post(endpoint, json=action)
            error, = [event for event in _stream_events(failed.text) if event["type"] == "error"]
            assert error["discard_partial"] is True
            assert session.current_state == baseline
            assert await stored_state(session) == baseline
            assert await read_active(session) == expected
            steps.clear()
            monkeypatch.setattr(game_routes, "_save_node", save_node)
        response = await client.post(endpoint, json=action)
        events = _stream_events(response.text)
        assert not [event for event in events if event["type"] == "error"]
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == narrative and steps == list(range(4))
        assert MARKER not in response.text
        node = await game_routes.get_tree_node(session.save_id, final["node_id"])
        assert MARKER not in json.dumps(node["agent_trace"], ensure_ascii=False)
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        await session.branch_to_node(root_id)
        assert await read_active(session) == []
        await session.branch_to_node(final["node_id"])
        assert await read_active(session) == expected
