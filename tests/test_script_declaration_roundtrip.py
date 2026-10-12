import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import shutil
import subprocess

from fastapi import FastAPI
import httpx
import pytest

from api import script_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


START = "2030-01-01T09:00:00"
END = "2030-01-01T09:15:00"
LATER = "2030-01-01T10:45:00"


@pytest.fixture(params=["form", "graph"])
def saved_payload(request):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the actual wizard save path")
    fixture = Path(__file__).with_name("script_declaration_roundtrip.test.cjs")
    command = [node, str(fixture), "--emit-script"]
    if request.param == "graph":
        command.append("--graph")
    result = subprocess.run(command, check=True, capture_output=True, encoding="utf-8", timeout=30)
    return json.loads(result.stdout)


@asynccontextmanager
async def saved_session(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-declarations.db")
    app = FastAPI()
    app.include_router(script_routes.router, prefix="/api/scripts")
    session = None
    try:
        await database.init_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/scripts", json=payload)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "ok" or result.get("saved") is True, result
            await database.close_pool()
            response = await client.get(f"/api/scripts/{payload['id']}")
            assert response.status_code == 200, response.text
            script = response.json()["content"]
            response = await client.get(f"/api/scripts/{payload['id']}/export")
            assert response.status_code == 200, response.text
            assert response.json() == script == payload["content"]
        session = GameSession(script, None, save_id="synthetic-declarations-save")
        session.current_state = ScriptLoader.create_initial_state(script)
        session.script_variables.init_state(session.current_state)
        yield session
    finally:
        if session is not None:
            await session._drain_background_tasks()
        await database.close_pool()


def test_saved_event_effects_and_goal_rewards_settle(saved_payload, tmp_path, monkeypatch):
    async def run():
        async with saved_session(saved_payload, tmp_path, monkeypatch) as session:
            events = []
            session._settle_timed_events(START, END, events)
            assert session.current_state["script_variables"]["test_ready"] == 2
            assert [event["event_id"] for event in events] == ["test_once"]
            session._settle_actor_goals()
            assert session.current_state["faction_reputation"]["test_circle"]["value"] == 72
            assert [goal["id"] for goal in session.current_state["completed_npc_goals"]] == ["test_reader:test_goal"]
            assert [goal["id"] for goal in session.current_state["completed_org_goals"]] == ["test_circle:test_goal"]
            settled = copy.deepcopy(session.current_state)
            session._settle_actor_goals()
            assert session.current_state == settled
            events = []
            session._settle_timed_events(END, LATER, events)
            assert session.current_state["script_variables"]["test_ready"] == 3
            assert [event["event_id"] for event in events] == ["test_cycle", "test_cycle"]

    asyncio.run(run())


def test_saved_rule_notes_remain_available_to_the_harness(saved_payload, tmp_path, monkeypatch):
    async def run():
        async with saved_session(saved_payload, tmp_path, monkeypatch) as session:
            before = copy.deepcopy((session.script, session.current_state))
            for category, marker in (
                ("one_time_events", "SYNTHETIC_EVENT_RULE"),
                ("cyclic_events", "SYNTHETIC_CYCLE_RULE"),
                ("npc_goals", "SYNTHETIC_NPC_RULE"),
                ("org_goals", "SYNTHETIC_ORG_RULE"),
            ):
                result = json.loads(session._handle_consult_rules({"category": category, "query": marker}))
                assert "error" not in result, result
                assert len(result["entries"]) == 1, result
                definition = json.loads(result["entries"][0]["content"])["definition"]
                if category.endswith("_goals"):
                    definition = definition["goal"]
                assert definition["rule_notes"] == marker
            assert (session.script, session.current_state) == before

    asyncio.run(run())


def test_saved_claims_reject_future_events_until_they_settle(saved_payload, tmp_path, monkeypatch):
    async def run():
        async with saved_session(saved_payload, tmp_path, monkeypatch) as session:
            session._settle_timed_events(START, END, [])
            context = {"old_time": START, "new_time": END}
            assert session._timed_event_revision_feedback(
                context, "The synthetic indicator has changed.", settled_state=session.current_state,
            ) == ""
            feedback = session._timed_event_revision_feedback(
                context, "The synthetic reminder has repeated.", settled_state=session.current_state,
            )
            assert feedback, "A saved future-event claim must not become an unchecked narrative"
            assert json.loads(feedback)["excluded_events"] == [{"id": "test_cycle", "time": "2030-01-01T09:30:00"}]
            session._settle_timed_events(END, LATER, [])
            assert session._timed_event_revision_feedback(
                context, "The synthetic reminder has repeated.",
                settled_end_time=LATER, settled_state=session.current_state,
            ) == ""

    asyncio.run(run())
