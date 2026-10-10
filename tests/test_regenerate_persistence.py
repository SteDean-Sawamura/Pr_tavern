import copy
import json
from collections import OrderedDict
from unittest.mock import AsyncMock

import pytest

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession


@pytest.mark.asyncio
@pytest.mark.parametrize("lore_storage", ["dynamic_lorebook", "lorebook_entry_overrides"])
async def test_stream_regenerate_preserves_swipes_after_restore(tmp_path, monkeypatch, lore_storage):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "regen.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    script = {"script_id": "regen-test", "name": "Regen test", "settings": {}}
    save_id = "regen-persistence"
    session = GameSession(script, None, save_id=save_id)
    parent_state = {"scene": "before"}
    old_lore = {"id": "scene_fact", "keys": ["OldScene"], "content": "Original scene"}
    new_lore = {"id": "scene_fact", "keys": ["NewScene"], "content": "Replacement scene"}
    old_state = {"scene": "original", lore_storage: (
        [old_lore] if lore_storage == "dynamic_lorebook" else {old_lore["id"]: old_lore}
    )}
    new_state = {"scene": "new", lore_storage: (
        [new_lore] if lore_storage == "dynamic_lorebook" else {new_lore["id"]: new_lore}
    )}
    parent_id = session.world_tree.add_node(
        None, "day 1", 1, ai_response="Before", state_snapshot=copy.deepcopy(parent_state),
    )
    original_id = session.world_tree.add_node(
        parent_id, "day 2", 2, player_action={"type": "freeform", "text": "Go"},
        ai_response="Original", choices_presented=[{"id": "old"}],
        state_snapshot=copy.deepcopy(old_state),
    )
    session.current_state = copy.deepcopy(old_state)
    session.prompt_builder.lorebook.add_entries([old_lore])
    session.turn_number = 2
    game_routes._sessions[save_id] = session

    async def events(action):
        session.current_state = copy.deepcopy(new_state)
        session.turn_number = 2
        new_id = session.world_tree.add_node(
            session.world_tree.active_node_id, "day 2", 2, player_action=action,
            ai_response="New", choices_presented=[{"id": "new"}],
            state_snapshot=copy.deepcopy(session.current_state),
        )
        yield {
            "type": "final", "node_id": new_id, "narrative": "New",
            "choices": [{"id": "new"}], "state": session.current_state,
            "state_changes": [],
        }

    session.process_action_stream = events
    try:
        async with database.get_db() as db:
            await db.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (script["script_id"], script["name"], json.dumps(script)),
            )
            await db.execute(
                "INSERT INTO saves (id, script_id, active_node_id, total_nodes) "
                "VALUES (?, ?, ?, 2)", (save_id, script["script_id"], original_id),
            )
            for node_id in (parent_id, original_id):
                node = session.world_tree.get_node(node_id)
                await db.execute(
                    "INSERT INTO tree_nodes (id, save_id, parent_id, game_time, turn_number, "
                    "player_action, ai_response, choices_presented, state_snapshot, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (node_id, save_id, node["parent_id"], node["game_time"],
                     node["turn_number"], json.dumps(node["player_action"]),
                     node["ai_response"], json.dumps(node["choices_presented"]),
                     json.dumps(node["state_snapshot"]), "2020-01-01 00:00:00"),
                )
            await db.commit()

        response = await game_routes.regenerate_stream(save_id)
        output = [chunk async for chunk in response.body_iterator]
        final = json.loads(next(chunk[6:] for chunk in output if '"type": "final"' in chunk))

        assert final["node_id"] == original_id
        assert final["total_swipes"] == 2
        activated, _ = session.prompt_builder.scan_lorebook("NewScene", [], state=session.current_state)
        assert [(entry.id, entry.content) for entry in activated] == [
            ("scene_fact", "Replacement scene")
        ]
        assert len(session.world_tree.nodes) == 2
        async with database.get_db() as db:
            row = await (await db.execute(
                "SELECT active_node_id, total_nodes FROM saves WHERE id = ?", (save_id,),
            )).fetchone()
            count = await (await db.execute(
                "SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (save_id,),
            )).fetchone()
            original_row = await (await db.execute(
                "SELECT created_at FROM tree_nodes WHERE id = ?", (original_id,),
            )).fetchone()
        assert (row["active_node_id"], row["total_nodes"], count[0]) == (original_id, 2, 2)
        assert original_row["created_at"] == "2020-01-01 00:00:00"

        game_routes._sessions.pop(save_id)
        restored = await game_routes._restore_session(save_id)
        assert restored.world_tree.active_node_id == original_id
        assert restored.get_all_swipes()[0]["narrative_preview"] == "Original"
        assert restored.get_all_swipes()[1]["narrative_preview"] == "New"
        assert restored.swipe_to("left")["state"] == old_state
        activated, _ = restored.prompt_builder.scan_lorebook("OldScene", [], state=restored.current_state)
        assert [(entry.id, entry.content) for entry in activated] == [
            ("scene_fact", "Original scene")
        ]
        assert restored.swipe_to("right")["state"] == new_state
        activated, _ = restored.prompt_builder.scan_lorebook("NewScene", [], state=restored.current_state)
        assert [(entry.id, entry.content) for entry in activated] == [
            ("scene_fact", "Replacement scene")
        ]

        await game_routes.swipe_direction(save_id, "left")
        game_routes._sessions.pop(save_id)
        restored = await game_routes._restore_session(save_id)
        assert restored.get_all_swipes()[0]["active"] is True
        assert restored.current_state == old_state
    finally:
        await database.close_pool()


def test_swipe_restores_dynamic_lore_with_reused_id():
    session = GameSession({"script_id": "lore-swipe", "settings": {}, "lorebook": [
        {"id": "static_fact", "keys": ["StaticFact"], "content": "Script fact"},
    ]}, None)
    old_lore = {"id": "branch_fact", "keys": ["OldFact"],
                "content": "Original branch fact", "comment": "loc:hall"}
    new_lore = {"id": "branch_fact", "keys": ["NewFact"],
                "content": "Replacement branch fact", "comment": "loc:attic"}
    old_state = {"dynamic_lorebook": [old_lore]}
    new_state = {"dynamic_lorebook": [new_lore, {
        "id": "static_fact", "keys": ["WrongFact"], "content": "Not authoritative",
    }]}
    node_id = session.world_tree.add_node(None, "", 1, ai_response="Original",
                                          state_snapshot=copy.deepcopy(old_state))
    node = session.world_tree.get_node(node_id)
    session.current_state = copy.deepcopy(old_state)
    session.prompt_builder.lorebook.add_entries([old_lore])

    session._record_regen_swipe(node, "Replacement", [], copy.deepcopy(new_state), [])
    activated, _ = session.prompt_builder.scan_lorebook("NewFact", [], state=session.current_state)
    assert [(entry.id, entry.content) for entry in activated] == [
        ("branch_fact", "Replacement branch fact")
    ]
    activated, _ = session.prompt_builder.scan_lorebook("OldFact", [], state=session.current_state)
    assert not activated
    activated, _ = session.prompt_builder.scan_lorebook("StaticFact", [], state=session.current_state)
    assert [(entry.id, entry.content) for entry in activated] == [("static_fact", "Script fact")]
    context = session.prompt_builder.lorebook.activate_by_context(location_id="hall")
    assert not any(entry.id == "branch_fact" for entry in context)

    session.swipe_to_index(0)
    activated, _ = session.prompt_builder.scan_lorebook("OldFact", [], state=session.current_state)
    assert [(entry.id, entry.content) for entry in activated] == [
        ("branch_fact", "Original branch fact")
    ]
    session.swipe_to_index(1)
    activated, _ = session.prompt_builder.scan_lorebook("NewFact", [], state=session.current_state)
    assert [(entry.id, entry.content) for entry in activated] == [
        ("branch_fact", "Replacement branch fact")
    ]


@pytest.mark.asyncio
async def test_partial_state_regenerate_persists_swipes_and_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "partial-regen.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    script = {"script_id": "partial-test", "name": "Partial test", "settings": {},
              "lorebook": [{"id": "static_fact", "keys": ["StaticFact"],
                            "content": "Available in every branch"}]}
    save_id = "partial-persistence"
    session = GameSession(script, None, save_id=save_id)
    parent_state = {"player": {"health": 10, "location": "hall"}, "game_time": "2026-01-01T10:00:00"}
    old_state = {"player": {"health": 5, "location": "hall"}, "game_time": "2026-01-01T10:15:00"}
    old_lore = {"id": "old_fact", "keys": ["OldFact"], "content": "Only the original branch knows"}
    old_state["dynamic_lorebook"] = [old_lore]
    parent_id = session.world_tree.add_node(
        None, parent_state["game_time"], 1, ai_response="Before",
        state_snapshot=copy.deepcopy(parent_state),
    )
    original_id = session.world_tree.add_node(
        parent_id, old_state["game_time"], 2,
        player_action={"type": "freeform", "text": "Go"},
        ai_response="Original", choices_presented=[{"id": "old", "text": "Old choice"}],
        state_changes=[{"target": "player.health", "op": "add", "value": -5}],
        state_snapshot=copy.deepcopy(old_state),
    )
    session.current_state = copy.deepcopy(old_state)
    session.prompt_builder.lorebook.add_entries([old_lore])
    session.turn_number = 2
    game_routes._sessions[save_id] = session

    async def partial(stage, narrative, action_text, state, streaming):
        yield {"type": "partial_regen_done", "parsed_state": {
            "state_changes": [{"target": "player.health", "op": "add", "value": -2,
                               "reason": "wounded"}],
            "end_time": "2026-01-01T10:30:00",
                "new_npcs": [{"id": "visitor", "name": "Visitor", "location": "hall",
                              "bio": "A new arrival"}],
        }}

    session._regen_partial_agent = partial
    try:
        async with database.get_db() as db:
            await db.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (script["script_id"], script["name"], json.dumps(script)),
            )
            await db.execute(
                "INSERT INTO saves (id, script_id, active_node_id, total_nodes) "
                "VALUES (?, ?, ?, 2)", (save_id, script["script_id"], original_id),
            )
            for node_id in (parent_id, original_id):
                node = session.world_tree.get_node(node_id)
                await db.execute(
                    "INSERT INTO tree_nodes (id, save_id, parent_id, game_time, turn_number, "
                    "player_action, ai_response, choices_presented, state_changes, "
                    "state_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (node_id, save_id, node["parent_id"], node["game_time"],
                     node["turn_number"], json.dumps(node["player_action"]),
                     node["ai_response"], json.dumps(node["choices_presented"]),
                     json.dumps(node["state_changes"]), json.dumps(node["state_snapshot"]),
                     "2020-01-01 00:00:00"),
                )
            await db.commit()

        response = await game_routes.regenerate_stream(save_id, stage="state")
        output = [part async for part in response.body_iterator]
        final = json.loads(next(part[6:] for part in output if '"type": "final"' in part))
        assert final["state"]["player"]["health"] == 8
        assert final["state"]["game_time"] == "2026-01-01T10:30:00"
        assert final["choices"] == [{"id": "old", "text": "Old choice"}]
        assert final["total_swipes"] == 2
        assert final["state_changes"]
        activated, _ = session.prompt_builder.scan_lorebook(
            "OldFact", [], state=session.current_state,
        )
        assert not any(entry.id == "old_fact" for entry in activated)

        async with database.get_db() as db:
            row = await (await db.execute(
                "SELECT state_snapshot, state_changes FROM tree_nodes WHERE id = ?",
                (original_id,),
            )).fetchone()
        assert json.loads(row["state_snapshot"])["player"]["health"] == 8
        assert json.loads(row["state_changes"]) == final["state_changes"]

        assert session._npc_name_to_id["Visitor"] == "visitor"
        assert session.prompt_builder._npc_by_id["visitor"]["name"] == "Visitor"
        assert "visitor" in session._compute_present_npcs(session.current_state)[0]
        assert "visitor" not in session.swipe_to("left")["state"].get("npcs", {})
        assert "visitor" not in session._compute_present_npcs(session.current_state)[0]
        assert "visitor" in session.swipe_to("right")["state"]["npcs"]

        game_routes._sessions.pop(save_id)
        restored = await game_routes._restore_session(save_id)
        assert restored.current_state["player"]["health"] == 8
        assert restored.current_state["npcs"]["visitor"]["name"] == "Visitor"
        assert restored._npc_by_id["visitor"]["name"] == "Visitor"
        assert restored._npc_name_to_id["Visitor"] == "visitor"
        assert restored.prompt_builder._npc_by_id["visitor"]["name"] == "Visitor"
        restored._ensure_identity_lore()
        assert any(entry.id == "_npc_profile_visitor" for entry in restored.prompt_builder.lorebook.entries)
        future_lore = {"id": "future_fact", "keys": ["FutureFact"],
                       "content": "The later branch fact", "comment": "loc:hall"}
        restored.current_state["dynamic_lorebook"].append(future_lore)
        restored.prompt_builder.lorebook.add_entries([future_lore])
        restored.prompt_builder.lorebook.add_entries([{
            "id": "_kg_event_shared", "keys": ["SharedEvent"],
            "content": "Runtime knowledge unrelated to the swipe",
        }])
        assert restored.swipe_to("left")["state"]["player"]["health"] == 5
        activated, _ = restored.prompt_builder.scan_lorebook("OldFact", [], state=restored.current_state)
        assert any(entry.id == "old_fact" for entry in activated)
        assert "visitor" not in restored.current_state.get("npcs", {})
        assert "visitor" not in restored._compute_present_npcs(restored.current_state)[0]
        assert "Visitor" not in restored.prompt_builder._get_present_npcs(restored.current_state, "hall")
        assert "Visitor" not in restored.prompt_builder._build_npc_section(restored.current_state)
        activated, _ = restored.prompt_builder.scan_lorebook("Visitor", [], state=restored.current_state)
        assert not any(entry.id == "_npc_profile_visitor" for entry in activated)
        activated, _ = restored.prompt_builder.scan_lorebook("FutureFact", [], state=restored.current_state)
        assert not any(entry.id == "future_fact" for entry in activated)
        activated, _ = restored.prompt_builder.scan_lorebook(
            "StaticFact SharedEvent", [], state=restored.current_state,
        )
        assert {entry.id for entry in activated} >= {"static_fact", "_kg_event_shared"}
        context = restored.prompt_builder.lorebook.activate_by_context(
            location_id="hall",
            excluded_entry_ids=restored.prompt_builder._excluded_lore_ids(restored.current_state),
        )
        assert not any(entry.id == "future_fact" for entry in context)
        assert restored.world_tree.get_node(original_id)["state_changes"][0]["value"] == -5
        assert restored.swipe_to("right")["state"]["player"]["health"] == 8
        activated, _ = restored.prompt_builder.scan_lorebook("OldFact", [], state=restored.current_state)
        assert not any(entry.id == "old_fact" for entry in activated)
        assert any(entry["id"] == "_npc_profile_visitor"
                   for entry in restored.current_state.get("dynamic_lorebook", []))
        assert "visitor" in restored._compute_present_npcs(restored.current_state)[0]
        assert "Visitor" in restored.prompt_builder._get_present_npcs(restored.current_state, "hall")
        assert "Visitor" in restored.prompt_builder._build_npc_section(restored.current_state)
        activated, _ = restored.prompt_builder.scan_lorebook("Visitor", [], state=restored.current_state)
        assert any(entry.id == "_npc_profile_visitor" for entry in activated)
        activated, _ = restored.prompt_builder.scan_lorebook("FutureFact", [], state=restored.current_state)
        assert any(entry.id == "future_fact" for entry in activated)
        context = restored.prompt_builder.lorebook.activate_by_context(
            location_id="hall",
            excluded_entry_ids=restored.prompt_builder._excluded_lore_ids(restored.current_state),
        )
        assert any(entry.id == "future_fact" for entry in context)
        assert restored.world_tree.get_node(original_id)["state_changes"] == final["state_changes"]

        await game_routes._update_node_in_db(restored, restored.swipe_to("left"))
        game_routes._sessions.pop(save_id, None)
        restored_from_old = await game_routes._restore_session(save_id)
        assert "visitor" not in restored_from_old.current_state.get("npcs", {})
        activated, _ = restored_from_old.prompt_builder.scan_lorebook(
            "OldFact", [], state=restored_from_old.current_state,
        )
        assert any(entry.id == "old_fact" for entry in activated)
        assert "visitor" in restored_from_old.swipe_to("right")["state"]["npcs"]
        activated, _ = restored_from_old.prompt_builder.scan_lorebook(
            "OldFact", [], state=restored_from_old.current_state,
        )
        assert not any(entry.id == "old_fact" for entry in activated)
        assert restored_from_old._npc_name_to_id["Visitor"] == "visitor"
        assert restored_from_old.prompt_builder._npc_by_id["visitor"]["name"] == "Visitor"
        activated, _ = restored_from_old.prompt_builder.scan_lorebook(
            "Visitor", [], state=restored_from_old.current_state,
        )
        assert any(entry.id == "_npc_profile_visitor" for entry in activated)
        activated, _ = restored_from_old.prompt_builder.scan_lorebook(
            "FutureFact", [], state=restored_from_old.current_state,
        )
        assert any(entry.id == "future_fact" for entry in activated)
    finally:
        await database.close_pool()
