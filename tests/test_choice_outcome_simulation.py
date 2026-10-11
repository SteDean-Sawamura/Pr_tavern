import copy
from dataclasses import asdict
import json
import random

import pytest

from api import game_routes
from engine.event_engine import GameEvent
from engine.game_session import GameSession
from test_declared_choice_preview import preview
from test_story_choice_settlement import choice_client, choice_session, persist_node, stored_state


def lore_state(session):
    return {entry.id: asdict(entry) for entry in session.prompt_builder.lorebook.entries}


def apply_preview_changes(before, changes):
    after = copy.deepcopy(before)
    for change in changes:
        parent = after
        for part in change["path"][:-1]:
            parent = parent[part]
        key = change["path"][-1]
        if change["operation"] != "add":
            assert json.dumps(parent[key], sort_keys=True) == json.dumps(change["before"], sort_keys=True)
        if change["operation"] == "remove":
            del parent[key]
        else:
            parent[key] = copy.deepcopy(change["after"])
    return after


def observed_state(session):
    return copy.deepcopy((
        session.current_state, session.script, lore_state(session),
        session.story_tree_engine.__dict__, session.event_engine.__dict__,
        session.script_variables.__dict__, session.trigger_engine.triggers,
        session.trigger_engine.variables.__dict__,
        session.trigger_engine.variables is session.script_variables,
        session.prompt_builder._lorebook_override_ids, session.world_tree.to_dict(),
        session._last_tool_result, session._deferred_lore_vector_ids,
    ))


def simulation_session(threshold=7, dynamic=False):
    session = choice_session(threshold, dynamic)
    session.current_state["player"]["test_chain_reward"] = 0
    session.event_engine.add_event(GameEvent(
        id="test_simulated_signal", category="trigger",
        condition="events.test_decision.status == completed AND script_variables.test_reward == 4",
        metadata={"event": ["test_signal"]},
        effects={"set_var": [{"var_id": "player.test_chain_reward", "op": "add", "value": 5}]},
    ))
    session._last_tool_result = {"name": "update_state", "synthetic": True}
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [7, 13])
@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("dynamic", [False, True])
async def test_simulated_choice_matches_real_api_and_saved_state(tmp_path, monkeypatch, threshold, candidate, dynamic):
    session = simulation_session(threshold, dynamic)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        updates = [{"variable_changes": [{"id": "test_gate", "op": "set", "value": threshold}]}]
        context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": updates}
        if not candidate:
            session.current_state["script_variables"]["test_gate"] = threshold
        before = observed_state(session)
        before_context = copy.deepcopy(context)
        random_state = random.getstate()
        result = await preview(session, context, include_candidates=candidate)
        assert result["available"] is True
        assert result["projection"]["effects_applied"] is True
        assert result["projection"]["scope"] == "choice_and_emitted_events"
        assert result["projection"]["persisted"] is False
        assert observed_state(session) == before
        assert context == before_context and random.getstate() == random_state
        base = session._project_state_tool_updates(updates) if candidate else session.current_state
        expected = apply_preview_changes(base, result["state_changes"])
        expected_lore = apply_preview_changes(lore_state(session), result["lore_changes"])
        assert expected["script_variables"]["test_reward"] == 4
        assert expected["player"]["test_chain_reward"] == 5
        assert expected["events"]["test_signalled"]["status"] == "unlocked"
        assert expected_lore["test_detail"]["enabled"] is True
        assert expected_lore["test_added"]["content"] == "SYNTHETIC_PREVIEW_EFFECT"
        assert result["projection"]["base_state_hash"] == session.world_tree.state_hash(base)
        assert result["projection"]["result_state_hash"] == session.world_tree.state_hash(expected)
        assert await preview(session, context, include_candidates=candidate) == result
        session.current_state = copy.deepcopy(base)
        final_id = await persist_node(session, root_id)
        response = await client.post(f"/api/game/{session.save_id}/story-tree/choice", json={
            "node_id": "test_decision", "choice_id": "test_open",
        })
        assert response.status_code == 200 and response.json()["success"] is True
        assert session.current_state == expected
        assert lore_state(session) == expected_lore
        assert await stored_state(session) == expected
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state == expected
        for current in (session, restored):
            await current.branch_to_node(root_id)
            unavailable = await preview(current)
            assert unavailable["available"] is False and unavailable["state_changes"] == []
            await current.branch_to_node(final_id)
            completed = await preview(current)
            assert completed["available"] is False and completed["lore_changes"] == []


@pytest.mark.asyncio
async def test_unavailable_simulation_discards_migration_and_candidate_changes():
    session = simulation_session()
    session.current_state.pop("events")
    before = observed_state(session)
    result = await preview(session)
    assert result["available"] is False
    assert result["projection"]["effects_applied"] is False
    assert result["state_changes"] == result["lore_changes"] == []
    assert observed_state(session) == before


@pytest.mark.asyncio
async def test_simulation_never_constructs_session_or_uses_external_resources(monkeypatch):
    session = simulation_session()
    session.current_state["script_variables"]["test_gate"] = 7

    class ExternalResource:
        def __deepcopy__(self, memo):
            raise AssertionError("Simulation must not clone external resources")

        def __getattr__(self, name):
            raise AssertionError("Simulation must not access external resources")

    external = ExternalResource()
    session.ai_provider = session.vector_memory = session.data_bank = external
    session._deferred_lore_vector_ids = {"existing-buffer"}
    before = observed_state(session)
    identities = (session.current_state, session.script_variables, session.trigger_engine,
                  session.story_tree_engine, session.event_engine, session.prompt_builder)

    def unexpected(*args, **kwargs):
        raise AssertionError("Simulation must not construct a live session or schedule indexing")

    monkeypatch.setattr(GameSession, "__init__", unexpected)
    monkeypatch.setattr(GameSession, "_schedule_index_task", unexpected)
    result = await preview(session)
    assert result["projection"]["effects_applied"] is True
    assert observed_state(session) == before
    assert all(original is current for original, current in zip(identities, (
        session.current_state, session.script_variables, session.trigger_engine,
        session.story_tree_engine, session.event_engine, session.prompt_builder,
    )))
    assert not session._background_tasks and not session._index_tasks
    assert session.prompt_builder.condition_eval.__self__ is session


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "_settle_story_choice", "_apply_story_tree_result", "_apply_lorebook_changes",
    "_evaluate_condition", "_fire_event_dual",
])
async def test_instance_bound_methods_are_rebound_to_candidate(method):
    session = simulation_session()
    setattr(session, method, getattr(session, method))
    before = observed_state(session)
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]},
    ]}
    result = await preview(session, context, include_candidates=True)
    assert result["available"] is True
    assert observed_state(session) == before
    base = session._project_state_tool_updates(context["_accepted_state_updates"])
    after = apply_preview_changes(base, result["state_changes"])
    assert after["script_variables"]["test_reward"] == 4
    assert after["player"]["test_chain_reward"] == 5
    assert getattr(session, method).__self__ is session


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_apply_story_tree_result", "_apply_lorebook_changes"])
async def test_partial_simulation_failure_preserves_live_objects(monkeypatch, method):
    session = simulation_session()
    session.current_state["script_variables"]["test_gate"] = 7
    before = observed_state(session)
    original = getattr(GameSession, method)

    def fail_after_effects(current, *args, **kwargs):
        original(current, *args, **kwargs)
        raise RuntimeError("Synthetic simulation failure")

    monkeypatch.setattr(GameSession, method, fail_after_effects)
    with pytest.raises(RuntimeError, match="Synthetic simulation failure"):
        await preview(session)
    assert observed_state(session) == before


def test_preview_differences_distinguish_absent_null_boolean_and_nested_lists():
    from engine.session.agentic_mixin import _choice_preview_changes

    before = {"missing_after": None, "value": 0, "nested": {"items": [False]}, "removed": "text"}
    after = {"created": None, "value": False, "nested": {"items": [0]}}
    changes = _choice_preview_changes(before, after)
    assert {tuple(change["path"]) for change in changes} == {
        ("created",), ("missing_after",), ("value",), ("nested", "items"), ("removed",),
    }
    assert json.dumps(apply_preview_changes(before, changes), sort_keys=True) == json.dumps(after, sort_keys=True)
