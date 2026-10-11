import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.session.agentic_mixin import _AgentResult
from engine.tool_schemas import _unified_tools


@pytest.fixture
def session():
    session = GameSession({
        "script_id": "synthetic-location-projection",
        "settings": {},
        "locations": [
            {"id": "test_start", "name": "Atrium"},
            {"id": "test_hidden", "name": "Archive", "travel_time": "PT5M"},
        ],
    }, object())
    session.current_state = {
        "game_time": "2030-01-01T09:00:00",
        "player": {"location": "test_start"},
        "visible_locations": ["test_start"],
        "display_names": {"test_start": "Atrium"},
    }
    return session


@pytest.mark.parametrize("location_id,location_name", [
    ("test_hidden", "Archive"), ("test_new", "Garden"),
])
def test_location_reveal_is_projected_without_changing_saved_state(
    session, location_id, location_name,
):
    original = copy.deepcopy(session.current_state)
    update = {"reveal_locations": [{"id": location_id, "name": location_name}]}
    projected = session._project_state_tool_updates([update])
    assert location_id in projected["visible_locations"]
    assert projected["display_names"][location_id] == location_name
    assert projected["player"]["location"] == "test_start"
    assert session.current_state == original


@pytest.mark.asyncio
@pytest.mark.parametrize("location_id,location_name", [
    ("test_hidden", "Archive"), ("test_new", "Garden"),
])
async def test_agent_can_reveal_then_move_in_separate_tools(
    session, location_id, location_name,
):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    updates = [
        {"reveal_locations": [{"id": location_id, "name": location_name}]},
        {"location_change": location_id},
    ]
    records = []
    for update in updates:
        result = await session._dispatch_unified_tool(context, "update_state", update)
        assert not result.startswith("错误:"), result
        records.append({"name": "update_state", "args": update, "result": result})

    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["player"]["location"] == location_id
    assert location_id in projected["visible_locations"]
    assert session.current_state == original

    parsed = session._build_unified_parsed(records, context, {"skip_choices": True})
    assert parsed["location_change"] == location_id
    settled, _ = session._apply_common_parsed_changes(copy.deepcopy(original), parsed, inplace=True)
    assert settled["player"]["location"] == projected["player"]["location"]
    assert settled["visible_locations"] == projected["visible_locations"]
    assert session.current_state == original


def test_merge_preserves_reveal_before_move_order(session):
    reveal = {"name": "update_spatial", "args": {
        "reveal_locations": [{"id": "test_hidden", "name": "Archive"}],
    }}
    move = {"name": "update_spatial", "args": {"location_change": "test_hidden"}}
    assert session._merge_state_tool_results([reveal, move])["location_change"] == "test_hidden"
    assert not session._merge_state_tool_results([move, reveal])["location_change"]


@pytest.mark.asyncio
async def test_unrevealed_location_is_still_rejected(session):
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    result = await session._dispatch_unified_tool(
        context, "update_state", {"location_change": "test_hidden"},
    )
    assert result.startswith("错误:")
    assert not context.get("_accepted_state_updates")
    assert session.current_state == original


@pytest.mark.asyncio
async def test_same_tool_reveal_and_move_remains_supported(session):
    context = {"old_time": session.current_state["game_time"]}
    result = await session._dispatch_unified_tool(context, "update_state", {
        "reveal_locations": [{"id": "test_hidden", "name": "Archive"}],
        "location_change": "test_hidden",
    })
    assert not result.startswith("错误:")
    projected = session._project_state_tool_updates(context["_accepted_state_updates"])
    assert projected["player"]["location"] == "test_hidden"
    assert "test_hidden" in projected["visible_locations"]


@pytest.mark.asyncio
async def test_discarded_reveal_cannot_authorize_a_later_turn(session):
    context = {"old_time": session.current_state["game_time"]}
    await session._dispatch_unified_tool(context, "update_state", {
        "reveal_locations": [{"id": "test_hidden", "name": "Archive"}],
    })
    fresh_context = {"old_time": session.current_state["game_time"]}
    result = await session._dispatch_unified_tool(
        fresh_context, "update_state", {"location_change": "test_hidden"},
    )
    assert result.startswith("错误:")
    assert not fresh_context.get("_accepted_state_updates")
    assert session.current_state["visible_locations"] == ["test_start"]


@pytest.mark.asyncio
async def test_newly_revealed_location_still_requires_configured_travel_time(session):
    context = {"old_time": session.current_state["game_time"]}
    for update in [
        {"end_time": "2030-01-01T09:01:00"},
        {"reveal_locations": [{"id": "test_hidden", "name": "Archive"}]},
    ]:
        result = await session._dispatch_unified_tool(context, "update_state", update)
        assert not result.startswith("错误:")
    accepted = copy.deepcopy(context["_accepted_state_updates"])
    result = await session._dispatch_unified_tool(
        context, "update_state", {"location_change": "test_hidden"},
    )
    assert "至少需要 5 分钟" in result
    assert context["_accepted_state_updates"] == accepted
    result = await session._dispatch_unified_tool(context, "update_state", {
        "location_change": "test_hidden", "end_time": "2030-01-01T09:05:00",
    })
    assert not result.startswith("错误:")


@pytest.mark.asyncio
async def test_agent_location_lore_unlock_survives_settlement_and_restore(
    session, tmp_path, monkeypatch,
):
    script = copy.deepcopy(session.script)
    script["lorebook"] = [{
        "id": "test_note", "keys": ["archive record"], "content": "Synthetic archive record.",
        "enabled": False, "visibility": "world",
    }]
    script["one_time_events"] = [{
        "id": "test_arrival", "trigger_time": "2030-01-01T09:05:00",
        "condition": "player.location == test_hidden",
        "effects": [{"type": "activate_lore", "id": "test_note"}],
    }]
    original = copy.deepcopy(session.current_state)
    session = GameSession(script, object())
    session.current_state = copy.deepcopy(original)
    action = {"type": "freeform", "text": "Explore."}
    context = session._prepare_turn(action)
    original = copy.deepcopy(session.current_state)
    sequence = [
        ("submit_plan", {"plan": "Explore the new area.", "questions": []}),
        ("update_state", {"reveal_locations": [{"id": "test_hidden", "name": "Archive"}]}),
        ("update_state", {"location_change": "test_hidden", "end_time": "2030-01-01T09:05:00"}),
        ("query_lorebook", {"entry_id": "test_note"}),
    ]
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=[
        *({"content": "", "tool_calls": [{
            "id": f"call-{index}", "name": name, "arguments": arguments,
        }]} for index, (name, arguments) in enumerate(sequence)),
        {"content": "Done", "tool_calls": []},
    ]))

    async def dispatch(name, arguments):
        return await session._dispatch_unified_tool(context, name, arguments)

    holder = _AgentResult()
    events = [event async for event in session._agent_loop(
        [{"role": "user", "content": "Explore."}], "Synthetic test.", _unified_tools(),
        dispatch, max_rounds=5, result_holder=holder, plan_before_tools=True,
    )]
    assert events[-1]["type"] == "agent_done"
    assert [record["name"] for record in holder.records] == [name for name, _ in sequence]
    assert all(not record["result"].startswith("错误:") for record in holder.records)
    result = json.loads(holder.records[-1]["result"])
    assert [entry["id"] for entry in result["entries"]] == ["test_note"]
    assert result["projection"]["event_ids"] == ["test_arrival"]
    assert result["projection"]["provisional"] is True
    assert session.current_state == original
    baseline = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "test_note"}))
    assert baseline["entries"] == []
    session.ai_provider.generate_with_tools.assert_awaited()

    parsed = session._build_unified_parsed(holder.records, context, {"skip_choices": True})
    parsed["narrative"] = "You arrive at the archive."
    session.ai_provider = None
    settled = await session._apply_parsed_response(parsed, parsed["narrative"], action, context)
    await session._drain_background_tasks()
    assert settled["state"]["player"]["location"] == "test_hidden"
    assert settled["state"]["game_time"] == "2030-01-01T09:05:00"
    assert [event["event_id"] for event in settled["triggered_events"]] == ["test_arrival"]
    settled_lore = json.loads(await session._run_tool_native_async(
        "query_lorebook", {"entry_id": "test_note"},
    ))
    assert [entry["id"] for entry in settled_lore["entries"]] == ["test_note"]

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "location-projection.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute(
                "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                (script["script_id"], "Synthetic location projection", json.dumps(script)),
            )
            await db.execute(
                "INSERT INTO saves (id, script_id) VALUES (?, ?)",
                (session.save_id, script["script_id"]),
            )
            await db.commit()
        await game_routes._save_node(session, settled)
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        assert restored.world_tree.active_node_id == settled["node_id"]
        assert restored.current_state["player"]["location"] == "test_hidden"
        assert "test_hidden" in restored.current_state["visible_locations"]
        assert restored.current_state["display_names"]["test_hidden"] == "Archive"
        assert restored.current_state["game_time"] == "2030-01-01T09:05:00"
        restored_lore = json.loads(await restored._run_tool_native_async(
            "query_lorebook", {"entry_id": "test_note"},
        ))
        assert restored_lore["snapshot"] != settled_lore["snapshot"]
        assert [{key: value for key, value in entry.items() if key != "snapshot"} for entry in restored_lore["entries"]] == [
            {key: value for key, value in entry.items() if key != "snapshot"} for entry in settled_lore["entries"]
        ]
        repeated = []
        restored._settle_timed_events(original["game_time"], restored.current_state["game_time"], repeated)
        assert repeated == []
    finally:
        await database.close_pool()
