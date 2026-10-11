import asyncio
import copy
import json
from collections import OrderedDict
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from test_agent_harness_execution import _stream_events
from test_npc_talk_access import talk_session


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_talk_access_survives_settlement_save_retry_reload_and_branch(tmp_path, monkeypatch, streaming, fail_save):
    session = talk_session()
    session.save_id = "synthetic-talk-save"
    session.current_state["script_variables"]["test_ready"] = 2
    session._apply_npc_contact_changes(session.current_state, [{"npc_id": "test_guide", "in_contact": True}])
    root_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, session.script["start_time"], 0,
                                         ai_response="Practice begins.", state_snapshot=root_state)
    provider = session.ai_provider
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "talk.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    await database.init_db()
    sessions = [session]
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                                     (session.script["script_id"], session.script["name"], json.dumps(session.script)))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                                     (session.save_id, session.script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": root_state, "narrative": "Practice begins."})
        endpoint = f"/api/game/{session.save_id}/talk/test_guide" + ("/stream" if streaming else "")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            if fail_save:
                save_node = game_routes._save_node
                rejected = []

                async def reject_save(current, result):
                    assert current.current_state["game_time"] == "2030-01-01T09:05:00"
                    assert current._get_npc_location("test_guide") == "test_desk"
                    assert not current.current_state.get("npc_contacts")
                    rejected.append(result["node_id"])
                    raise RuntimeError("Synthetic save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failed = await client.post(endpoint, json={"message": "Hello."})
                assert len(rejected) == 1
                if streaming:
                    assert not [event for event in _stream_events(failed.text) if event["type"] == "final"]
                else:
                    assert failed.status_code == 500
                assert session.current_state == root_state
                assert session.world_tree.active_node_id == root_id
                assert set(session.world_tree.nodes) == {root_id}
                assert not game_routes._get_session_lock(session.save_id).locked()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json={"message": "Hello."})
            assert response.status_code == 200
            if streaming:
                result, = [event for event in _stream_events(response.text) if event["type"] == "final"]
                assert response.text.endswith("data: [DONE]\n\n")
            else:
                result = response.json()
            assert not result.get("error")
            assert result["state"]["npc_contacts"] == {}
            assert result["state"]["npc_talk_block_reasons"] == {"test_guide": "不在当前位置"}
            assert "current_location" not in result["state"]["npcs"]["test_guide"]
            assert result["dialogue_count"] == 1
            final_state = copy.deepcopy(session.current_state)
            calls = provider.generate.await_count + len(provider.stream_calls)
            assert calls == (2 if fail_save else 1)
            refused = await client.post(endpoint, json={"message": "Continue."})
            if streaming:
                error, = [event for event in _stream_events(refused.text) if event["type"] == "final"]
                assert error["error"] == "不在当前位置"
            else:
                assert refused.status_code == 400
                assert refused.json()["detail"] == "不在当前位置"
            assert provider.generate.await_count + len(provider.stream_calls) == calls
            assert session.current_state == final_state
            assert not game_routes._get_session_lock(session.save_id).locked()
            for marker in ("SYNTHETIC_SHIFT_RULE_BODY", "SYNTHETIC_SHIFT_NOTICE_BODY"):
                assert marker not in response.text
            root = await game_routes.get_tree_node(session.save_id, root_id)
            assert root["state_snapshot"]["npc_contacts"] == {"test_guide": True}
            assert root["state_snapshot"]["npc_talk_block_reasons"] == {"test_guide": ""}
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == final_state
            assert restored._npc_talk_block_reason("test_guide") == "不在当前位置"
            await restored.branch_to_node(root_id)
            assert restored._npc_talk_block_reason("test_guide") == ""
            assert restored._npc_contact_is_current("test_guide", restored.current_state)
            await restored.branch_to_node(result["node_id"])
            assert restored._npc_talk_block_reason("test_guide") == "不在当前位置"
            assert not restored.current_state.get("npc_contacts")
            assert provider.generate.await_count + len(provider.stream_calls) == calls
        async with database.get_db() as connection:
            rows = await (await connection.execute("SELECT state_snapshot FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchall()
            assert len(rows) == 2
            assert all("npc_talk_block_reasons" not in json.loads(row[0]) for row in rows)
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()


@pytest.mark.asyncio
async def test_abandoned_talk_stream_rolls_back_without_saving_or_leaving_a_lock(monkeypatch):
    session = talk_session()
    session.save_id = "synthetic-abandoned-talk"
    original = copy.deepcopy(session.current_state)
    waiting = asyncio.Event()

    async def unfinished_stream(messages, **kwargs):
        yield "<think></think>Welcome to the practice desk."
        await waiting.wait()

    session.ai_provider.generate_stream = unfinished_stream
    monkeypatch.setattr(game_routes, "_sessions", {session.save_id: session})
    monkeypatch.setattr(game_routes, "_session_locks", {})
    save_node = AsyncMock()
    monkeypatch.setattr(game_routes, "_save_node", save_node)
    response = await game_routes.talk_to_npc_stream(session.save_id, "test_guide", game_routes.NpcTalkRequest(message="Hello."))
    iterator = response.body_iterator
    try:
        chunk = await asyncio.wait_for(anext(iterator), timeout=2)
        assert '"type": "text"' in chunk
        assert game_routes._get_session_lock(session.save_id).locked()
    finally:
        await iterator.aclose()
    assert not game_routes._get_session_lock(session.save_id).locked()
    assert session.current_state == original
    assert session.world_tree.nodes == {}
    save_node.assert_not_awaited()
