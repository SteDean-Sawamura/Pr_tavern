import copy

import pytest

from api import game_routes
from engine.event_engine import GameEvent
from test_story_choice_settlement import choice_client, choice_session, persist_node, stored_state


def player_choices(response, surface):
    assert response.status_code == 200, response.text
    body = response.json()
    nodes = body["choices"] if surface == "active-choices" else [
        node for tree in body["trees"] for node in tree["nodes"]
    ]
    return {node["id"]: [choice["id"] for choice in node.get("choices", [])]
            for node in nodes if node.get("choices")}


def runtime_snapshot(session):
    return copy.deepcopy((
        session.current_state, session.script,
        session.story_tree_engine.__dict__ if session.story_tree_engine else None,
        session.event_engine.__dict__ if session.event_engine else None,
        session.prompt_builder.lorebook.__dict__,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("view_source", ["both", "story", "event"])
@pytest.mark.parametrize("surface", ["active-choices", "tree"])
@pytest.mark.parametrize("condition,available", [
    ("missing", True), ("", True), (None, False), (False, False), (0, False),
    ([], False), ({}, False), ("invalid expression", False),
    ("script_variables.test_gate >= 7", True), ("script_variables.test_gate > 7", False),
    ({"all": ["script_variables.test_gate >= 7"]}, False),
    ("(script_variables.test_gate >= 7 AND script_variables.test_reward == 0)", True),
])
async def test_player_choices_match_settlement_conditions(tmp_path, monkeypatch, view_source, surface, condition, available):
    session = choice_session()
    session.script.update({"random_items": [], "npcs": []})
    session.current_state["script_variables"]["test_gate"] = 7
    choice = session.story_tree_engine._nodes["test_decision"]["choices"][0]
    if condition == "missing":
        choice.pop("condition")
    else:
        choice["condition"] = condition
    session.event_engine.events["test_decision"].choices = copy.deepcopy([choice])
    event_engine = session.event_engine
    if view_source == "story":
        session.event_engine = None
    elif view_source == "event":
        session.story_tree_engine = None
        session.event_engine.shadow_story_nodes = False
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        base = f"/api/game/{session.save_id}/story-tree"
        response = await client.get(base + ("/active-choices" if surface == "active-choices" else ""))
        expected = {"test_decision": ["test_open"]} if available else {}
        assert player_choices(response, surface) == expected
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored
        assert "SYNTHETIC_PREVIEW_DETAIL" not in response.text
        assert "SYNTHETIC_PREVIEW_EFFECT" not in response.text
        if view_source == "story":
            session.event_engine = event_engine
        before = runtime_snapshot(session)
        result = await client.post(base + "/choice", json={"node_id": "test_decision", "choice_id": "test_open"})
        assert result.status_code == 200 and result.json()["success"] is available
        if available:
            decision = next(node for tree in result.json()["trees"] for node in tree["nodes"]
                            if node["id"] == "test_decision")
            assert decision["status"] == "completed"
            assert decision["choices"][0]["chosen"] is True
        else:
            assert player_choices(result, "tree") == {}
            assert runtime_snapshot(session) == before
            assert await stored_state(session) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["active-choices", "tree"])
@pytest.mark.parametrize("shadow", ["unrelated", "completed", "definition", "missing", "false_active", "false_choice"])
async def test_player_views_use_canonical_story_nodes(tmp_path, monkeypatch, surface, shadow):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    if shadow == "unrelated":
        session.current_state["events"] = {"test_external": {"status": "active"}}
    elif shadow == "completed":
        session.current_state["events"]["test_decision"] = {"status": "completed", "choices_made": "obsolete"}
    elif shadow == "definition":
        event = session.event_engine.events["test_decision"]
        event.name = "Obsolete decision"
        event.choices = [{"id": "test_obsolete", "label": "Obsolete option"}]
    elif shadow == "missing":
        session.event_engine.events.pop("test_decision")
        session.event_engine._tree_for_event.pop("test_decision")
    elif shadow == "false_active":
        session.current_state["story_tree_state"]["active"].remove("test_decision")
    elif shadow == "false_choice":
        session.story_tree_engine._nodes["test_decision"]["type"] = "quest"
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        base = f"/api/game/{session.save_id}/story-tree"
        response = await client.get(base + ("/active-choices" if surface == "active-choices" else ""))
        expected = {} if shadow in ("false_active", "false_choice") else {"test_decision": ["test_open"]}
        assert player_choices(response, surface) == expected
        if surface == "tree" and expected:
            decision = next(node for tree in response.json()["trees"] for node in tree["nodes"]
                            if node["id"] == "test_decision")
            assert decision["status"] == "active"
            assert not decision["choices"][0]["chosen"]
        assert "Obsolete" not in response.text and "obsolete" not in response.text
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["both", "story", "event"])
@pytest.mark.parametrize("surface", ["active-choices", "tree"])
async def test_player_queries_do_not_initialize_or_migrate_live_state(tmp_path, monkeypatch, source, surface):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    session.current_state.pop("events")
    session.current_state["story_tree_state"] = {"active": ["test_decision"]}
    if source == "story":
        session.event_engine = None
    elif source == "event":
        session.story_tree_engine = None
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        base = f"/api/game/{session.save_id}/story-tree"
        response = await client.get(base + ("/active-choices" if surface == "active-choices" else ""))
        assert player_choices(response, surface) == {"test_decision": ["test_open"]}
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_tree", [False, True])
async def test_event_only_choices_remain_visible_alongside_canonical_nodes(tmp_path, monkeypatch, separate_tree):
    session = choice_session()
    tree_id = "test_extra_tree" if separate_tree else "test_tree"
    if separate_tree:
        session.event_engine._trees.append({"id": tree_id, "name": "Extra decisions"})
    session.event_engine.add_event(GameEvent(
        id="test_independent", name="Independent decision", category="choice", tree_id=tree_id,
        choices=[{"id": "test_select", "effects": {"set_var": [{"var_id": "test_reward", "op": "inc", "value": 6}]}},
                 {"id": "test_blocked", "condition": False}],
    ))
    session.current_state["events"]["test_independent"] = {"status": "active"}
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        base = f"/api/game/{session.save_id}/story-tree"
        for suffix, surface in (("/active-choices", "active-choices"), ("", "tree")):
            response = await client.get(base + suffix)
            assert player_choices(response, surface) == {"test_independent": ["test_select"]}
        assert runtime_snapshot(session) == before
        result = await client.post(base + "/choice", json={"node_id": "test_independent", "choice_id": "test_select"})
        assert result.status_code == 200 and result.json()["success"] is True
        assert session.current_state["script_variables"]["test_reward"] == 6
        decision = next(node for tree in result.json()["trees"] for node in tree["nodes"]
                        if node["id"] == "test_independent")
        assert decision["status"] == "completed"
        assert [choice["id"] for choice in decision["choices"]] == ["test_select", "test_blocked"]
        assert decision["choices"][0]["chosen"] is True
        assert session.current_state["story_tree_state"] == before[0]["story_tree_state"]


@pytest.mark.asyncio
async def test_player_choice_availability_follows_reloaded_branch(tmp_path, monkeypatch):
    session = choice_session()
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root_id = session.world_tree.active_node_id
        session.current_state["script_variables"]["test_gate"] = 7
        ready_id = await persist_node(session, root_id)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored is not None
        base = f"/api/game/{session.save_id}/story-tree"
        for node_id, expected in ((ready_id, {"test_decision": ["test_open"]}), (root_id, {}),
                                  (ready_id, {"test_decision": ["test_open"]})):
            await restored.branch_to_node(node_id)
            before = runtime_snapshot(restored)
            for suffix, surface in (("/active-choices", "active-choices"), ("", "tree")):
                response = await client.get(base + suffix)
                assert player_choices(response, surface) == expected
            assert runtime_snapshot(restored) == before


@pytest.mark.asyncio
async def test_player_tree_preserves_legacy_threads_and_clues_without_live_migration(tmp_path, monkeypatch):
    session = choice_session()
    session.current_state.pop("events")
    session.current_state["narrative_threads"] = [{"id": "test_thread", "status": "active", "last_updated": 4}]
    session.current_state["clue_board"] = [{"id": "test_clue", "category": "observation", "source": "look"}]
    for event_id, category in (("test_thread", "thread"), ("test_clue", "clue")):
        session.event_engine.add_event(GameEvent(id=event_id, category=category, description="Synthetic discovery", created_turn=3))

    def unexpected(*args, **kwargs):
        raise AssertionError("Player queries must not advance rules or schedule work")

    monkeypatch.setattr(session.story_tree_engine, "evaluate", unexpected)
    monkeypatch.setattr(session.event_engine, "tick", unexpected)
    for method in ("_settle_timed_events", "_fire_lifecycle_event", "_schedule_background_task"):
        monkeypatch.setattr(session, method, unexpected)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        response = await client.get(f"/api/game/{session.save_id}/story-tree")
        assert response.status_code == 200
        assert response.json()["narrative_threads"] == [{
            "id": "test_thread", "name": "", "description": "Synthetic discovery",
            "status": "active", "created_turn": 3, "last_updated": 4,
        }]
        assert response.json()["clue_board"] == [{
            "id": "test_clue", "text": "Synthetic discovery", "category": "observation", "source": "look", "turn_discovered": 3,
        }]
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored


@pytest.mark.asyncio
async def test_completed_choice_history_survives_later_condition_changes(tmp_path, monkeypatch):
    session = choice_session()
    session.current_state["script_variables"]["test_gate"] = 7
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        base = f"/api/game/{session.save_id}/story-tree"
        result = await client.post(base + "/choice", json={"node_id": "test_decision", "choice_id": "test_open"})
        assert result.status_code == 200 and result.json()["success"] is True
        session.current_state["script_variables"]["test_gate"] = 0
        await persist_node(session, session.world_tree.active_node_id)
        before = runtime_snapshot(session)
        stored = await stored_state(session)
        active = await client.get(base + "/active-choices")
        assert player_choices(active, "active-choices") == {}
        history = await client.get(base)
        assert player_choices(history, "tree") == {"test_decision": ["test_open"]}
        decision = next(node for tree in history.json()["trees"] for node in tree["nodes"] if node["id"] == "test_decision")
        assert decision["status"] == "completed" and decision["choices"][0]["chosen"] is True
        assert runtime_snapshot(session) == before
        assert await stored_state(session) == stored
