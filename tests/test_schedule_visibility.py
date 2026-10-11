import asyncio
import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import game_routes
from db import database
from test_scheduled_npc_overrides import make_session


ACTIVITY = "SYNTHETIC_PRIVATE_SCHEDULE_BODY"


def declared_session(declaration="effect"):
    session = make_session(override_condition="test_ready >= 2")
    session.current_state["script_variables"]["test_ready"] = 2
    session.current_state["npcs"]["test_guide"].update(known=True, met=True)
    event = session.script["one_time_events"][0]
    effect = event["effects"][0]
    effect["activity"] = ACTIVITY
    slot = {key: value for key, value in effect.items() if key not in ("type", "npc_id")}
    if declaration != "effect":
        target = "npc_schedule_overrides" + (".test_guide" if declaration == "entry" else "")
        value = [slot] if declaration == "entry" else {"test_guide": [slot]}
        event["effects"][0] = {"type": "state_change", "target": target, "op": "set", "value": value}
    return session, slot


@pytest.mark.parametrize("target", ["npc_schedule_overrides", "npc_schedule_overrides.test_guide",
                                    "npc_schedule_overrides.test_guide.0.activity"])
def test_schedule_change_logs_are_private_without_mutating_internal_records(target):
    session, slot = declared_session()
    private = {"target": target, "op": "set", "value": slot,
               "old": ACTIVITY, "new": slot, "reason": ACTIVITY}
    public = {"target": "player.gold", "op": "add", "value": 1, "reason": "Practice reward"}
    changes = [private, public]
    original = copy.deepcopy(changes)
    assert game_routes._player_state_changes(changes, session.script, session.current_state) == [public]
    assert changes == original


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", ["effect", "root", "entry"])
async def test_schedule_declarations_stay_internal_across_save_views(tmp_path, monkeypatch, declaration):
    session, slot = declared_session(declaration)
    session.save_id = "synthetic-schedule-visibility"
    script = session.script
    baseline = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, baseline["game_time"], 0, state_snapshot=baseline)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "schedule-visibility.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": baseline})
        changes, _, _ = session._settle_timed_events(baseline["game_time"], "2030-01-01T09:10:00", [])
        session.current_state["game_time"] = "2030-01-01T09:10:00"
        session._refresh_npc_locations()
        session.turn_number = 1
        node_id = session.world_tree.add_node(root_id, session.current_state["game_time"], 1,
                                             state_snapshot=session.current_state, state_changes=changes)
        candidate, candidate_changes, events = session._settle_regen_state(
            {"end_time": "2030-01-01T09:15:00"}, baseline, [],
        )
        node = session.world_tree.get_node(node_id)
        session._record_regen_swipe(node, "You wait quietly.", [], candidate, candidate_changes, triggered_events=events)
        await game_routes._save_node(session, {"node_id": node_id, "state": candidate,
                                              "state_changes": candidate_changes, "narrative": "You wait quietly."})
        expected = {"test_guide": [slot]}
        assert session.current_state["npc_schedule_overrides"] == expected
        assert (ACTIVITY in json.dumps(changes)) is (declaration != "effect")
        assert (ACTIVITY in json.dumps(candidate_changes)) is (declaration != "effect")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}"
            for reload in (False, True):
                if reload:
                    game_routes._sessions.clear()
                    restored = await game_routes._restore_session(session.save_id)
                    assert restored is not None and restored is not session
                    sessions.append(restored)
                    session = restored
                    assert session.current_state["npc_schedule_overrides"] == expected
                for path in ("state", "snapshot", f"tree/{node_id}", "history", "swipes"):
                    original = copy.deepcopy(session.current_state)
                    response = await client.get(f"{endpoint}/{path}")
                    assert response.status_code == 200
                    assert "npc_schedule_overrides" not in response.text, path
                    assert ACTIVITY not in response.text, path
                    assert session.current_state == original
                for path in (f"branch/{root_id}", f"branch/{node_id}", "swipe/left", "swipe/right"):
                    response = await client.post(f"{endpoint}/{path}")
                    assert response.status_code == 200
                    assert "npc_schedule_overrides" not in response.text, path
                    assert ACTIVITY not in response.text, path
                    if path == f"branch/{root_id}":
                        assert "npc_schedule_overrides" not in session.current_state
                    else:
                        assert session.current_state["npc_schedule_overrides"] == expected
                assert session.current_state["game_time"] == "2030-01-01T09:15:00"

            started, release = asyncio.Event(), asyncio.Event()

            async def blocked_action(action, on_background_drained):
                on_background_drained()
                started.set()
                await release.wait()
                yield {"type": "aborted", "discard_partial": True}

            monkeypatch.setattr(session, "process_action_stream", blocked_action)
            pending = asyncio.create_task(client.post(f"{endpoint}/action/stream", json={
                "type": "freeform", "text": "Wait for the next practice instruction.",
                "action_id": "synthetic-schedule-pending", "expected_node_id": node_id,
                "stream_id": "synthetic-schedule-stream",
            }))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                assert game_routes._get_session_lock(session.save_id).locked()
                snapshot = await asyncio.wait_for(client.get(f"{endpoint}/snapshot"), timeout=5)
                assert snapshot.status_code == 200
                assert snapshot.json()["pending_action"]["actionId"] == "synthetic-schedule-pending"
                assert "npc_schedule_overrides" not in snapshot.text
                assert ACTIVITY not in snapshot.text
            finally:
                release.set()
                response = await asyncio.wait_for(pending, timeout=5)
            assert response.status_code == 200
            assert session.current_state["npc_schedule_overrides"] == expected
        async with database.get_db() as db:
            row = await (await db.execute("SELECT state_snapshot, state_changes FROM tree_nodes WHERE id = ?",
                                          (node_id,))).fetchone()
        stored = json.loads(row["state_snapshot"])
        assert stored["npc_schedule_overrides"] == expected
        assert len(stored["_node_context"]["swipes"]) == 2
        for swipe in stored["_node_context"]["swipes"]:
            assert swipe["state_snapshot"]["npc_schedule_overrides"] == expected
            assert (ACTIVITY in json.dumps(swipe["state_changes"])) is (declaration != "effect")
        assert (ACTIVITY in row["state_changes"]) is (declaration != "effect")
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["present", "remote", "unknown", "dead", "condition_false",
                                  "future", "expired", "explicit", "unfired"])
async def test_schedule_observation_only_returns_current_visible_activity(monkeypatch, case):
    session, _ = declared_session()
    effect = session.script["one_time_events"][0]["effects"][0]
    if case == "future":
        effect["time_range"] = "09:20-10:00"
    end_time = "2030-01-01T09:04:00" if case == "unfired" else "2030-01-01T09:10:00"
    session._settle_timed_events(session.current_state["game_time"], end_time, [])
    state = session.current_state
    state["game_time"] = "2030-01-01T09:30:00" if case == "expired" else end_time
    state["player"]["location"] = "test_desk" if case in ("present", "unknown", "dead") else "test_origin"
    if case == "unknown":
        state["npcs"]["test_guide"]["known"] = False
    elif case == "dead":
        state["npcs"]["test_guide"]["alive"] = False
    elif case == "condition_false":
        state["script_variables"]["test_ready"] = 0
    elif case == "explicit":
        state["npcs"]["test_guide"]["current_location"] = "test_origin"
    session._refresh_npc_locations()
    original = copy.deepcopy(state)
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([("synthetic-observation", session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/game/synthetic-observation/npc-schedules")
    assert response.status_code == 200
    if case in ("remote", "unknown", "dead"):
        assert response.json() == {}
    else:
        activity = ACTIVITY if case == "present" else "" if case == "explicit" else "Practice rest"
        assert response.json() == {"test_guide": {"location": state["player"]["location"], "activity": activity}}
    assert state == original
    if case != "unfired":
        assert state["npc_schedule_overrides"]["test_guide"][0]["activity"] == ACTIVITY
