import asyncio
from collections import OrderedDict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import config
from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events


def review_session(stage_models, responses):
    provider = SimpleNamespace(generate=AsyncMock(side_effect=responses))
    return GameSession({
        "script_id": "synthetic-model-routing", "name": "Synthetic model routing",
        "settings": {}, "npcs": [], "locations": [], "story_tree": {},
    }, provider, stage_models=stage_models)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_routes,global_routes,expected_model", [
    ({"review": "profile-review", "state": "profile-state"},
     {"review": "global-review", "state": "global-state"}, "profile-review"),
    ({"review": "profile-review"}, {}, "profile-review"),
    ({"state": "profile-state"}, {"review": "global-review"}, "global-review"),
    ({"review": "", "state": "profile-state"}, {"review": "global-review"}, "global-review"),
    ({}, {"review": "global-review", "state": "global-state"}, "global-review"),
    ({"state": "profile-state"}, {"state": "global-state"}, "profile-state"),
    ({}, {"state": "global-state"}, "global-state"),
    ({"review": ""}, {"review": "", "state": "global-state"}, "global-state"),
    ({}, {}, None),
])
@pytest.mark.parametrize("responses,expected,budgets", [
    (['{"pass": true, "violations": []}'], {"pass": True, "violations": []}, [500]),
    (["", '{"pass": false, "violations": []}'], {"pass": False, "violations": []}, [500, 1500]),
    (["", ""], None, [500, 1500]),
    (['{"pass": "true"}', '{"pass": 1}'], None, [500, 1500]),
])
async def test_review_route_priority_and_retry_budget(
    monkeypatch, profile_routes, global_routes, expected_model, responses, expected, budgets,
):
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", global_routes)
    session = review_session(profile_routes, responses)
    context = {}

    result = await session._review_narrative("You inspect an empty table.", "Inspect the table.", context)

    assert result == expected
    calls = session.ai_provider.generate.await_args_list
    assert [call.kwargs["max_tokens"] for call in calls] == budgets
    assert context["_semantic_review_model_calls"] == len(budgets)
    for call in calls:
        if expected_model is None:
            assert "model" not in call.kwargs
        else:
            assert call.kwargs["model"] == expected_model
        assert call.args == calls[0].args
        assert call.kwargs["system"] == calls[0].kwargs["system"]


@pytest.mark.asyncio
async def test_profile_review_route_persists_updates_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-profiles.db")
    monkeypatch.setattr(database, "_pool", None)
    monkeypatch.setattr(database, "_pool_lock", None)
    monkeypatch.setattr(database, "_pool_created", 0)
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", {})
    app = FastAPI()
    app.include_router(config_routes.router, prefix="/api/config")
    await database.init_db()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial_routes = {"review": "synthetic-review", "state": "synthetic-state"}
            created = await client.post("/api/config/profiles", json={
                "name": "Synthetic profile", "provider_type": "openai_compatible",
                "model": "synthetic-default", "api_key": "synthetic-credential",
                "stage_models": initial_routes,
            })
            assert created.status_code == 200
            profile_id = created.json()["id"]
            route_versions = [initial_routes,
                              {"review": "synthetic-review-updated", "state": "synthetic-state"},
                              {"state": "synthetic-state"}, {}]
            for index, routes in enumerate(route_versions):
                if index:
                    updated = await client.put(f"/api/config/profiles/{profile_id}", json={
                        "stage_models": routes,
                    })
                    assert updated.status_code == 200
                await database.close_pool()
                listed = await client.get("/api/config/profiles")
                assert listed.status_code == 200
                assert listed.json()[0]["stage_models"] == routes
                assert "api_key" not in listed.json()[0]
                active_routes = await config_routes.get_active_stage_models()
                assert active_routes == routes
                session = review_session(active_routes, ['{"pass": true, "violations": []}'])
                assert await session._review_narrative("You wait.", "Wait.", {}) == {
                    "pass": True, "violations": [],
                }
                assert session.ai_provider.generate.await_args.kwargs.get("model") == (
                    routes.get("review") or routes.get("state")
                )
                async with database.get_db() as connection:
                    cursor = await connection.execute(
                        "SELECT api_key, stage_models FROM ai_profiles WHERE id = ?", (profile_id,),
                    )
                    stored = await cursor.fetchone()
                    assert stored["api_key"] == "synthetic-credential"
                    assert json.loads(stored["stage_models"] or "{}") == routes
    finally:
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("route_source", ["profile", "global", "state_fallback"])
@pytest.mark.parametrize("resume_mode", ["cached", "reload"])
@pytest.mark.parametrize("regeneration_streaming", [False, True])
async def test_review_routing_through_new_game_stream_and_resume(
    tmp_path, monkeypatch, route_source, resume_mode, regeneration_streaming,
):
    script = {
        "script_id": "synthetic-review-flow", "name": "Synthetic review flow", "settings": {},
        "script_name": "Synthetic review flow", "world_background": "An empty room.",
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "variables": [{"id": "test_inspections", "type": "number", "default": 0}],
    }
    narrative = (
        "You inspect the empty table beside the wall. Its wooden surface is clean and dry. "
        "You look along its rounded edges, leave everything in place, and step back toward the doorway."
    )
    turn_index = 0
    tool_requests = [[], []]
    review_requests = [[], []]

    async def generate_with_tools(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        tool_requests[turn_index].append(kwargs)
        operations = [
            ("submit_plan", {"plan": "Inspect the table without moving anything."}),
            ("update_state", {
                "variable_changes": [{"id": "test_inspections", "op": "inc", "value": 1}],
                "end_time": f"2030-01-01T09:0{turn_index + 1}:00",
            }),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Inspected the empty table."}),
        ]
        index = len(tool_requests[turn_index]) - 1
        assert index < len(operations)
        name, arguments = operations[index]
        assert name in names
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-review-{turn_index}-{index}", "name": name, "arguments": arguments,
        }]}

    async def generate(messages, **kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        if "<narrative>" in content and "<protagonist_name>" in content:
            review_requests[turn_index].append(kwargs)
            return "" if len(review_requests[turn_index]) == 1 else '{"pass": true, "violations": []}'
        return json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [],
        })

    provider = SimpleNamespace(generate=AsyncMock(side_effect=generate),
                               generate_with_tools=AsyncMock(side_effect=generate_with_tools))

    def create_provider(profile):
        nonlocal provider
        provider = SimpleNamespace(generate=AsyncMock(side_effect=generate),
                                   generate_with_tools=AsyncMock(side_effect=generate_with_tools))
        return provider

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-review-flow.db")
    monkeypatch.setattr(database, "_pool", None)
    monkeypatch.setattr(database, "_pool_lock", None)
    monkeypatch.setattr(database, "_pool_created", 0)
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict())
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(config_routes, "_provider_cache", {"instance": None, "fingerprint": None})
    monkeypatch.setattr(config_routes, "_create_provider_from_profile", create_provider)
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    monkeypatch.setattr(config, "PIPELINE_MODE", "agentic_unified")
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", {})
    app = FastAPI()
    app.include_router(config_routes.router, prefix="/api/config")
    app.include_router(game_routes.router, prefix="/api/game")
    sessions = []
    await database.init_db()
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                                     (script["script_id"], script["name"], json.dumps(script)))
            await connection.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            routes = {"narrative": "synthetic-main", "state": "synthetic-state"}
            if route_source != "global":
                routes["review" if route_source == "profile" else "state"] = "synthetic-review"
            else:
                response = await client.post("/api/config/model-routes", json={"routes": {"review": "synthetic-review"}})
                assert response.status_code == 200
            created = await client.post("/api/config/profiles", json={
                "name": "Synthetic flow profile", "provider_type": "openai_compatible",
                "model": "synthetic-default", "api_key": "synthetic-credential", "stage_models": routes,
            })
            assert created.status_code == 200
            profile_id = created.json()["id"]

            async def change_review_model(model):
                if route_source == "global":
                    return await client.post("/api/config/model-routes", json={"routes": {"review": model}})
                routes["review" if route_source == "profile" else "state"] = model
                return await client.put(f"/api/config/profiles/{profile_id}", json={"stage_models": routes})

            started = await client.post("/api/game/new", json={"script_id": script["script_id"], "skip_opening": True})
            assert started.status_code == 200, started.text
            save_id = started.json()["save_id"]
            current = game_routes._sessions[save_id]
            sessions.append(current)
            endpoint = f"/api/game/{save_id}/action/stream"
            for turn_index in range(2):
                action = {
                    "type": "freeform", "text": "Inspect the empty table.", "intent_hint": "explicit_action",
                    "stream_id": f"synthetic-review-stream-{turn_index}", "action_id": f"synthetic-review-action-{turn_index}",
                    "expected_node_id": current.world_tree.active_node_id,
                }
                response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=10)
                assert response.status_code == 200
                assert response.text.endswith("data: [DONE]\n\n")
                events = _stream_events(response.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                final, = [event for event in events if event["type"] == "final"]
                assert final["narrative"] == narrative
                assert current.current_state["script_variables"]["test_inspections"] == turn_index + 1
                assert current.current_state["game_time"] == f"2030-01-01T09:0{turn_index + 1}:00"
                assert current.ai_provider is provider
                assert "synthetic-credential" not in response.text
                await current._drain_background_tasks(timeout=None)
                expected_model = "synthetic-review" if turn_index == 0 else "synthetic-review-updated"
                assert [request["model"] for request in review_requests[turn_index]] == [expected_model] * 2
                assert [request["max_tokens"] for request in review_requests[turn_index]] == [500, 1500]
                assert {request["model"] for request in tool_requests[turn_index]} == {"synthetic-main"}
                counts = (provider.generate.await_count, provider.generate_with_tools.await_count)
                replay = await client.post(endpoint, json=action)
                assert replay.text == response.text
                assert (provider.generate.await_count, provider.generate_with_tools.await_count) == counts
                if turn_index == 0:
                    changed = await change_review_model("synthetic-review-updated")
                    assert changed.status_code == 200
                    if resume_mode == "reload":
                        game_routes._sessions.clear()
                        await database.close_pool()
                        restored = await game_routes._restore_session(save_id)
                        assert restored is not None and restored is not current
                        assert restored.current_state["script_variables"]["test_inspections"] == 1
                        sessions.append(restored)
                        current = restored
            changed = await change_review_model("synthetic-review-regenerated")
            assert changed.status_code == 200
            tool_requests[turn_index].clear()
            review_requests[turn_index].clear()
            node_id = current.world_tree.active_node_id
            regenerate_endpoint = f"/api/game/{save_id}/regenerate" + ("/stream" if regeneration_streaming else "")
            regenerated = await asyncio.wait_for(client.post(
                regenerate_endpoint, params={"stage": "all", "stream_id": "synthetic-review-regeneration"},
            ), timeout=10)
            assert regenerated.status_code == 200, regenerated.text
            if regeneration_streaming:
                assert regenerated.text.endswith("data: [DONE]\n\n")
                events = _stream_events(regenerated.text)
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                assert any(event["type"] == "final" for event in events)
            await current._drain_background_tasks(timeout=None)
            assert current.ai_provider is provider
            assert current.world_tree.active_node_id == node_id
            assert current.current_state["script_variables"]["test_inspections"] == 2
            assert current.turn_number == 2
            assert [request["model"] for request in review_requests[turn_index]] == ["synthetic-review-regenerated"] * 2
            assert [request["max_tokens"] for request in review_requests[turn_index]] == [500, 1500]
            async with database.get_db() as connection:
                cursor = await connection.execute("SELECT COUNT(*) FROM action_requests WHERE save_id = ?", (save_id,))
                assert (await cursor.fetchone())[0] == 2
    finally:
        for session in sessions:
            await session._drain_background_tasks(timeout=None)
        await database.close_pool()
