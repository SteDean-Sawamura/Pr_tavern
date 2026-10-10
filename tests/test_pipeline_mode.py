import asyncio
import copy
import json
import sqlite3
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import config
from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession


@pytest_asyncio.fixture
async def mode_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_MODE", "workflow")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    await database.init_db()

    script = {"script_id": "pipeline-test", "name": "Pipeline test", "settings": {}}
    sessions = []
    async with database.get_db() as connection:
        await connection.execute(
            "INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
            (script["script_id"], script["name"], json.dumps(script)),
        )
        for save_id, mode in (("first", "agentic"), ("second", "workflow")):
            session = GameSession(script, None, save_id=save_id)
            session.current_state = {"_pipeline_mode": mode, "player": {"health": 80}}
            node_id = session.world_tree.add_node(
                parent_id=None, game_time="2026-01-01", turn_number=0,
                ai_response="Opening narrative", choices_presented=[{"id": "wait"}],
                state_snapshot=copy.deepcopy(session.current_state),
            )
            snapshot = {**session.current_state, "_node_context": {"thinking": "Keep this"}}
            await connection.execute(
                "INSERT INTO saves (id, script_id, active_node_id) VALUES (?, ?, ?)",
                (save_id, script["script_id"], node_id),
            )
            await connection.execute(
                "INSERT INTO tree_nodes (id, save_id, ai_response, choices_presented, state_snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (node_id, save_id, "Opening narrative", '[{"id": "wait"}]', json.dumps(snapshot)),
            )
            game_routes._sessions[save_id] = session
            sessions.append(session)
        await connection.commit()

    application = FastAPI()
    application.include_router(config_routes.router, prefix="/api/config")
    application.include_router(game_routes.router, prefix="/api/game")
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield SimpleNamespace(client=client, first=sessions[0], second=sessions[1], script=script)
    finally:
        await database.close_pool()


def read_node(session):
    with sqlite3.connect(database.DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT * FROM tree_nodes WHERE id = ?", (session.world_tree.active_node_id,)
        ).fetchone())


@pytest.mark.asyncio
async def test_scoped_get_uses_session_mode(mode_app):
    response = await mode_app.client.get("/api/config/pipeline-mode?save_id=first")
    assert response.status_code == 200
    assert response.json()["mode"] == "agentic"
    assert config.PIPELINE_MODE == "workflow"


@pytest.mark.asyncio
async def test_default_change_only_affects_future_sessions(mode_app):
    response = await mode_app.client.post("/api/config/pipeline-mode", json={"mode": "agentic_unified"})
    assert response.status_code == 200
    assert config.PIPELINE_MODE == "agentic_unified"
    assert mode_app.first._get_pipeline_mode() == "agentic"
    assert mode_app.second._get_pipeline_mode() == "workflow"
    assert GameSession(mode_app.script, None)._get_pipeline_mode() == "agentic_unified"


@pytest.mark.asyncio
async def test_new_session_captures_default_before_start(mode_app, monkeypatch):
    session = GameSession(mode_app.script, None)
    monkeypatch.setattr(config, "PIPELINE_MODE", "agentic_unified")
    assert session._get_pipeline_mode() == "workflow"


@pytest.mark.asyncio
async def test_scoped_change_persists_only_target_without_losing_node_content(mode_app):
    response = await mode_app.client.post(
        "/api/config/pipeline-mode", json={"mode": "agentic_unified", "save_id": "first"}
    )
    assert response.status_code == 200
    assert config.PIPELINE_MODE == "workflow"
    assert mode_app.first._get_pipeline_mode() == "agentic_unified"
    assert mode_app.second._get_pipeline_mode() == "workflow"
    node = mode_app.first.world_tree.get_node(mode_app.first.world_tree.active_node_id)
    assert node["state_snapshot"]["_pipeline_mode"] == "agentic_unified"
    saved = read_node(mode_app.first)
    snapshot = json.loads(saved["state_snapshot"])
    assert snapshot["_pipeline_mode"] == "agentic_unified"
    assert snapshot["player"] == {"health": 80}
    assert snapshot["_node_context"] == {"thinking": "Keep this"}
    assert saved["ai_response"] == "Opening narrative"
    assert json.loads(saved["choices_presented"]) == [{"id": "wait"}]


@pytest.mark.asyncio
async def test_switch_then_restore_keeps_mode_without_changing_default(mode_app):
    response = await mode_app.client.post(
        "/api/config/pipeline-mode", json={"mode": "agentic_unified", "save_id": "first"}
    )
    assert response.status_code == 200
    game_routes._sessions.pop("first")
    response = await mode_app.client.get("/api/config/pipeline-mode?save_id=first")
    assert response.status_code == 200
    assert response.json()["mode"] == "agentic_unified"
    assert "first" in game_routes._sessions
    assert config.PIPELINE_MODE == "workflow"
    snapshot = await mode_app.client.get("/api/game/first/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["state"]["_pipeline_mode"] == "agentic_unified"


@pytest.mark.asyncio
async def test_restoring_game_does_not_change_default(mode_app):
    game_routes._sessions.pop("first")
    response = await mode_app.client.get("/api/game/first/state")
    assert response.status_code == 200
    assert response.json()["_pipeline_mode"] == "agentic"
    assert config.PIPELINE_MODE == "workflow"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_missing_session_is_not_a_default_change(mode_app, method):
    if method == "get":
        response = await mode_app.client.get("/api/config/pipeline-mode?save_id=missing")
    else:
        response = await mode_app.client.post(
            "/api/config/pipeline-mode", json={"mode": "agentic_unified", "save_id": "missing"}
        )
    assert response.status_code == 404
    assert config.PIPELINE_MODE == "workflow"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"mode": "invalid", "save_id": "first"},
    {"mode": "agentic", "save_id": ""},
    {"mode": "agentic", "save_id": 12},
])
async def test_invalid_change_does_not_mutate_sessions(mode_app, payload):
    response = await mode_app.client.post("/api/config/pipeline-mode", json=payload)
    assert response.status_code == 400
    assert config.PIPELINE_MODE == "workflow"
    assert mode_app.second._get_pipeline_mode() == "workflow"


@pytest.mark.asyncio
async def test_busy_session_rejects_mode_change(mode_app):
    async with game_routes._get_session_lock("first"):
        response = await asyncio.wait_for(mode_app.client.post(
            "/api/config/pipeline-mode", json={"mode": "workflow", "save_id": "first"}
        ), timeout=1)
    assert response.status_code == 409
    assert mode_app.first._get_pipeline_mode() == "agentic"


@pytest.mark.asyncio
async def test_pending_settlement_rejects_mode_change(mode_app):
    task = asyncio.create_task(asyncio.Event().wait())
    mode_app.first._background_tasks.add(task)
    try:
        response = await mode_app.client.post(
            "/api/config/pipeline-mode", json={"mode": "workflow", "save_id": "first"}
        )
        assert response.status_code == 409
        assert mode_app.first._get_pipeline_mode() == "agentic"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_persistence_keeps_in_memory_mode(mode_app, monkeypatch):
    @asynccontextmanager
    async def failing_database():
        raise sqlite3.OperationalError("test write failure")
        yield

    monkeypatch.setattr(config_routes, "get_db", failing_database)
    response = await mode_app.client.post(
        "/api/config/pipeline-mode", json={"mode": "workflow", "save_id": "first"}
    )
    assert response.status_code == 500
    assert mode_app.first._get_pipeline_mode() == "agentic"
    assert json.loads(read_node(mode_app.first)["state_snapshot"])["_pipeline_mode"] == "agentic"


@pytest.mark.asyncio
async def test_branch_uses_snapshot_mode_instead_of_cached_mode(mode_app):
    session = mode_app.first
    session.pipeline_mode = "agentic_unified"
    result = await session.branch_to_node(session.world_tree.active_node_id)
    assert result["state"]["_pipeline_mode"] == "agentic"
    assert session._get_pipeline_mode() == "agentic"
