import asyncio
from contextlib import asynccontextmanager
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_dynamic_story_choices import make_session as choice_session
from test_story_choice_settlement import choice_client, persist_node, stored_state


def make_session(dynamic=False):
    original, _ = choice_session()
    script = copy.deepcopy(original.script)
    actor = {
        "id": "test_reader", "name": "Synthetic reader", "known": True,
        "goals": [{"id": "test_original", "description": "Keep the reading room tidy."}],
    }
    script["npcs"] = [] if dynamic else [actor]
    script["organizations"] = [{
        "id": "test_circle", "name": "Synthetic reading circle",
        "goals": [{"id": "test_original", "description": "Keep the catalog organized."}],
    }]
    session = GameSession(script, None, save_id="synthetic-director-goal-save")
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["npcs"] = {"test_reader": copy.deepcopy(actor)}
    session._index_dynamic_npcs_from_state(session.current_state)
    session._ensure_identity_lore()
    return session, script


def updates(marker="SYNTHETIC_DIRECTOR_GOAL_DETAIL", threshold=9):
    goal = {
        "id": "test_new_goal", "description": marker, "priority": "high",
        "condition_met": f"script_variables.test_gate >= {threshold}",
    }
    return {
        "npc_goals": [{"npc_id": "test_reader", "goals": [copy.deepcopy(goal)]}],
        "org_goals": [{"org_id": "test_circle", "goals": [copy.deepcopy(goal)]}],
    }


def goals(session):
    return {
        "npcs": {"test_reader": session._npc_by_id["test_reader"].get("goals", [])},
        "organizations": {"test_circle": session.script["organizations"][0].get("goals", [])},
    }


@asynccontextmanager
async def goal_client(session, script, tmp_path, monkeypatch):
    async with choice_client(session, tmp_path, monkeypatch) as result:
        async with database.get_db() as db:
            await db.execute("UPDATE scripts SET content = ? WHERE id = ?",
                             (json.dumps(script), script["script_id"]))
            await db.commit()
        yield result


@pytest.mark.parametrize("dynamic", [False, True])
def test_accepted_director_goals_are_saved_once_without_aliasing(dynamic):
    session, script = make_session(dynamic)
    original_script = copy.deepcopy(script)
    declaration = updates()
    declaration["npc_goals"][0]["goals"] *= 2
    declaration["org_goals"][0]["goals"] *= 2
    declaration["npc_goals"][0]["goals"].append({"id": "test_original", "description": "Do not replace."})
    original = copy.deepcopy(declaration)
    session._apply_director_results({"goals": declaration})
    session._apply_director_results({"goals": declaration})
    saved = copy.deepcopy(session.current_state["dynamic_story_content"]["goal_overrides"])
    assert saved == goals(session)
    for section in saved.values():
        assert [goal["id"] for goal in next(iter(section.values()))] == ["test_original", "test_new_goal"]
    assert declaration == original and script == original_script
    declaration["npc_goals"][0]["goals"][0]["description"] = "Caller mutation"
    session._npc_by_id["test_reader"]["goals"][-1]["description"] = "Runtime mutation"
    assert session.current_state["dynamic_story_content"]["goal_overrides"] == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_goal_definitions_follow_reload_and_sibling_branches(dynamic):
    session, script = make_session(dynamic)
    original = copy.deepcopy(goals(session))
    root = session.world_tree.add_node(None, session.current_state["game_time"], 0,
                                       state_snapshot=copy.deepcopy(session.current_state))
    branches = []
    for marker in ("SYNTHETIC_GOAL_BRANCH_LEFT", "SYNTHETIC_GOAL_BRANCH_RIGHT"):
        await session.branch_to_node(root)
        assert goals(session) == original
        session._apply_director_results({"goals": updates(marker)})
        branches.append(session.world_tree.add_node(root, session.current_state["game_time"], 1,
                                                    state_snapshot=copy.deepcopy(session.current_state)))
    for branch, marker in zip(branches, ("SYNTHETIC_GOAL_BRANCH_LEFT", "SYNTHETIC_GOAL_BRANCH_RIGHT")):
        await session.branch_to_node(branch)
        state = json.loads(json.dumps(session.current_state))
        restored = GameSession(script, None)
        restored.current_state = state
        restored._hydrate_branch_runtime(state)
        assert goals(restored) == goals(session)
        assert restored._npc_by_id["test_reader"]["goals"][-1]["description"] == marker
        assert marker in restored._gather_director_context()["goals_text"]
        await session.branch_to_node(root)
        assert goals(session) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_restored_goal_is_read_by_character_model_without_public_leakage(dynamic):
    session, script = make_session(dynamic)
    session._apply_director_results({"goals": updates()})
    restored = GameSession(script, SimpleNamespace(generate=AsyncMock(return_value="I will check the shelf.")))
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._hydrate_branch_runtime(restored.current_state)
    before = copy.deepcopy(restored.current_state)
    reply = json.loads(await restored._handle_invoke_character({"npc_id": "test_reader", "situation": "Inspect the shelf."}))
    assert [goal["id"] for goal in reply["context"]["active_goals"]] == ["test_original", "test_new_goal"]
    assert "SYNTHETIC_DIRECTOR_GOAL_DETAIL" in restored.ai_provider.generate.call_args.kwargs["system"]
    assert restored.current_state == before
    assert "SYNTHETIC_DIRECTOR_GOAL_DETAIL" not in json.dumps(game_routes._player_state(
        restored.current_state, restored.script, restored))


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_goal_completion_uses_reloaded_declaration_once(tmp_path, monkeypatch, dynamic):
    session, script = make_session(dynamic)
    async with goal_client(session, script, tmp_path, monkeypatch) as (_, sessions):
        root = session.world_tree.active_node_id
        session._apply_director_results({"goals": updates()})
        session.current_state["script_variables"]["test_gate"] = 9
        final = await persist_node(session, root)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        restored._check_npc_goals()
        restored._check_org_goals()
        assert any(goal["id"] == "test_reader:test_new_goal" for goal in restored.current_state["completed_npc_goals"])
        assert any(goal["id"] == "test_circle:test_new_goal" for goal in restored.current_state["completed_org_goals"])
        completed = copy.deepcopy(restored.current_state)
        restored._check_npc_goals()
        restored._check_org_goals()
        assert restored.current_state == completed
        await game_routes._persist_active_state(restored)
        assert (await stored_state(restored))["completed_npc_goals"] == completed["completed_npc_goals"]
        await restored.branch_to_node(root)
        assert all(goal["id"] != "test_new_goal" for goal in restored._npc_by_id["test_reader"]["goals"])
        await restored.branch_to_node(final)
        assert restored.current_state["completed_npc_goals"] == completed["completed_npc_goals"]


@pytest.mark.parametrize("invalid", [None, {}, "invalid", [None], [{}],
    [{"npc_id": [], "org_id": [], "goals": []}],
    [{"npc_id": "unknown", "org_id": "unknown", "goals": [{"id": "test_invalid"}]}],
    [{"npc_id": "test_reader", "org_id": "test_circle", "goals": "invalid"}],
    [{"npc_id": "test_reader", "org_id": "test_circle", "goals": [
        None, {}, {"id": ""}, {"id": "  "}, {"id": []}, {"id": 1},
    ]}],
])
def test_invalid_goal_updates_do_not_change_actors_or_save_declarations(invalid):
    session, _ = make_session()
    before = copy.deepcopy(goals(session))
    session._apply_director_results({"goals": {"npc_goals": invalid, "org_goals": invalid}})
    assert goals(session) == before
    assert "goal_overrides" not in session.current_state["dynamic_story_content"]


@pytest.mark.parametrize("dynamic", [False, True])
def test_goal_aggregate_survives_generation_log_pruning_and_restore(dynamic):
    session, script = make_session(dynamic)
    for index in range(25):
        declaration = updates(f"Synthetic goal batch {index}")
        for key in ("npc_goals", "org_goals"):
            declaration[key][0]["goals"][0]["id"] = f"test_batch_{index}"
        session._apply_director_results({"goals": declaration})
    assert len(session.current_state["dynamic_story_content"]["generation_log"]) == 20
    restored = GameSession(script, None)
    restored.current_state = json.loads(json.dumps(session.current_state))
    restored._restore_dynamic_story()
    assert goals(restored) == goals(session)
    assert len(restored._npc_by_id["test_reader"]["goals"]) == 26
    saved = copy.deepcopy(restored.current_state)
    restored.script["organizations"][0]["goals"][-1]["description"] = "Runtime mutation"
    assert restored.current_state == saved


@pytest.mark.parametrize("dynamic", [False, True])
def test_branch_without_goals_restores_absent_field(dynamic):
    source, script = make_session(dynamic)
    for actor in script["npcs"] + script["organizations"]:
        actor.pop("goals", None)
    session = GameSession(script, None)
    session.current_state = copy.deepcopy(source.current_state)
    session.current_state["npcs"]["test_reader"].pop("goals")
    original = copy.deepcopy(session.current_state)
    session._hydrate_branch_runtime(session.current_state)
    session._apply_director_results({"goals": updates()})
    assert len(session._npc_by_id["test_reader"]["goals"]) == 1
    session.current_state = original
    session._hydrate_branch_runtime(original)
    assert "goals" not in session._npc_by_id["test_reader"]
    assert "goals" not in session.script["organizations"][0]


def director_provider(declaration):
    async def generate(messages, **kwargs):
        if "你是目标管理器" in kwargs.get("system", ""):
            return json.dumps(declaration)
        return "{}"

    return SimpleNamespace(generate=AsyncMock(side_effect=generate))


def test_hydration_does_not_reset_goals_outside_director_overrides():
    session, _ = make_session()
    session._npc_by_id["test_reader"]["goals"] = [{"id": "test_runtime_npc"}]
    session.script["organizations"][0]["goals"] = [{"id": "test_runtime_org"}]
    before = copy.deepcopy(goals(session))
    session._hydrate_branch_runtime(session.current_state)
    assert goals(session) == before


async def dispatch_goals(session):
    session._dispatch_meta_events({"route": {"expand_story": True}}, {},
                                  {"type": "freeform", "text": "Inspect the shelf."})
    assert any(task.get_name() == "_handle_story_director" for task in session._background_tasks)
    await session._drain_background_tasks(timeout=None, include_index=False)
    assert "_pending_story_director" not in session.current_state
    assert "story_director_flag" in session.current_state["meta_event_trackers"]
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("existing", [False, True])
async def test_background_goal_save_failure_restores_declarations_and_runtime(tmp_path, monkeypatch, dynamic, existing):
    session, script = make_session(dynamic)
    session.turn_number = 1
    session.current_state["key_events"] = [{"event": "A visitor inspects the reading room."}]
    async with goal_client(session, script, tmp_path, monkeypatch):
        if existing:
            session._apply_director_results({"goals": updates("Synthetic existing goal")})
        await game_routes._persist_active_state(session)
        before = copy.deepcopy(session.current_state)
        before_goals = copy.deepcopy(goals(session))
        stored = await stored_state(session)
        declaration = updates("SYNTHETIC_RETRY_GOAL_DETAIL")
        for key in ("npc_goals", "org_goals"):
            declaration[key][0]["goals"][0]["id"] = "test_retry"
        session.ai_provider = director_provider(declaration)

        @asynccontextmanager
        async def fail_after_snapshot_update():
            async with database.get_db() as db:
                async def execute(sql, *args):
                    if sql.startswith("UPDATE saves"):
                        assert session._npc_by_id["test_reader"]["goals"][-1]["id"] == "test_retry"
                        raise RuntimeError("Synthetic goal transaction failure")
                    return await db.execute(sql, *args)
                yield SimpleNamespace(execute=execute, commit=db.commit)

        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "get_db", fail_after_snapshot_update)
            with pytest.raises(RuntimeError, match="Synthetic goal transaction failure"):
                await game_routes._run_async_state_change(session, lambda: dispatch_goals(session))
        assert session.current_state == before
        assert goals(session) == before_goals
        assert await stored_state(session) == stored
        await game_routes._run_async_state_change(session, lambda: dispatch_goals(session))
        assert session._npc_by_id["test_reader"]["goals"][-1]["id"] == "test_retry"
        assert (await stored_state(session))["dynamic_story_content"]["goal_overrides"] == goals(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_background_goal_reaches_next_stream_after_sqlite_reload(tmp_path, monkeypatch, dynamic):
    session, script = make_session(dynamic)
    session.turn_number = 1
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["key_events"] = [{"event": "A visitor inspects the reading room."}]
    session.ai_provider = director_provider(updates())
    narrative = (
        "You remain beside the reading room shelf and inspect it carefully. "
        "You leave all the books in place and keep the aisle clear, then finish the inspection "
        "without disturbing anyone in the room."
    )
    steps = []
    character_prompts = []

    async def generate(messages, **kwargs):
        system = kwargs.get("system", "")
        if system.startswith("你是Synthetic reader。"):
            character_prompts.append(system)
            assert "SYNTHETIC_DIRECTOR_GOAL_DETAIL" in system
            return "I will check the shelf."
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })

    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments = "classify_intent", {"intent": "action"}
        elif names == ["extract_notes"]:
            name, arguments = "extract_notes", {"notes": []}
        else:
            step = len(steps)
            steps.append(step)
            operations = [
                ("submit_plan", {"plan": "Consider the reader's current goals before inspecting the shelf."}),
                ("invoke_character", {"npc_id": "test_reader", "situation": "Inspect the shelf."}),
                ("update_state", {"end_time": "2030-01-01T09:01:00"}),
                ("write_narrative", {"text": narrative}),
                ("finalize_turn", {"summary": "Inspected the shelf."}),
            ]
            assert step < len(operations)
            name, arguments = operations[step]
            if step == 2:
                receipt = next(message for message in messages if message.get("tool_call_id") == "goal-1")
                result = json.loads(receipt["content"])
                assert result["context"]["active_goals"][-1]["description"] == "SYNTHETIC_DIRECTOR_GOAL_DETAIL"
        assert name in names
        return {"content": "", "tool_calls": [{"id": f"goal-{len(steps) - 1}", "name": name, "arguments": arguments}]}

    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        original_goals = copy.deepcopy(goals(session))
        branch_id = await persist_node(session, root_id)
        await dispatch_goals(session)
        assert session._npc_by_id["test_reader"]["goals"][-1]["description"] == "SYNTHETIC_DIRECTOR_GOAL_DETAIL"
        goal_calls = [call for call in session.ai_provider.generate.call_args_list
                      if "你是目标管理器" in call.kwargs.get("system", "")]
        assert len(goal_calls) == 1
        assert "只新增，不覆盖已有目标ID" in goal_calls[0].args[0][0]["content"]
        await game_routes._persist_sessions_on_shutdown()
        assert (await stored_state(session))["dynamic_story_content"]["goal_overrides"] == goals(session)
        game_routes._sessions.clear()
        session = await game_routes._restore_session(session.save_id)
        assert session is not None
        sessions.append(session)
        assert ("test_reader" not in session._initial_npc_ids) is dynamic
        await session.branch_to_node(root_id)
        assert goals(session) == original_goals
        await session.branch_to_node(branch_id)
        session.ai_provider = SimpleNamespace(generate=AsyncMock(side_effect=generate),
                                               generate_with_tools=AsyncMock(side_effect=respond))
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Inspect the shelf.", "action_id": "synthetic-goal-action",
                  "expected_node_id": branch_id}
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        assert response.status_code == 200
        events = _stream_events(response.text)
        assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}], response.text
        final, = [event for event in events if event["type"] == "final"]
        assert final["narrative"] == narrative and steps == list(range(5))
        assert len(character_prompts) == 1
        assert (await stored_state(session))["dynamic_story_content"]["goal_overrides"] == goals(session)
        calls = session.ai_provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert session.ai_provider.generate_with_tools.await_count == calls
        trace = await game_routes.get_tree_node(session.save_id, final["node_id"])
        assert "SYNTHETIC_DIRECTOR_GOAL_DETAIL" not in response.text + json.dumps(trace)
