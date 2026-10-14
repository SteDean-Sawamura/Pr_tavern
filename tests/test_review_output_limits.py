import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes
from db import database
from test_review_model_routes import review_session


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["openai", "openai_compatible", "claude", "ollama"])
async def test_profile_review_limits_reach_both_attempts(provider_type):
    provider = config_routes._create_provider_from_profile({
        "provider_type": provider_type, "model": "synthetic-default", "max_tokens": 8192,
        "review_max_tokens": 1024, "review_retry_max_tokens": 4096,
    })
    provider.generate = AsyncMock(side_effect=["", '{"pass": true, "violations": []}'])
    session = review_session({"review": "synthetic-review"}, [])
    session.ai_provider = provider
    context = {}

    result = await session._review_narrative("You examine a blank page.", "Examine the page.", context)

    assert result["pass"] is True
    assert [call.kwargs["max_tokens"] for call in provider.generate.await_args_list] == [1024, 4096]
    assert all(call.kwargs["model"] == "synthetic-review" for call in provider.generate.await_args_list)
    assert context["_semantic_review_model_calls"] == 2
    assert provider.max_tokens == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("responses,expected", [
    (['{"pass": false, "violations": ["Rewrite the dialogue."]}'],
     {"pass": False, "violations": ["Rewrite the dialogue."]}),
    (["", '{"pass": true, "violations": []}'], {"pass": True, "violations": []}),
    (["", ""], {"available": False, "pass": None, "violations": []}),
    ([RuntimeError("Synthetic failure")], {"available": False, "pass": None, "violations": []}),
])
async def test_review_tool_uses_shared_route_and_unavailable_is_not_pass(responses, expected):
    session = review_session({"review": "synthetic-review", "state": "synthetic-state"}, responses)
    session.ai_provider.review_max_tokens = 1024
    session.ai_provider.review_retry_max_tokens = 4096
    context = {"action_text": "Examine the page."}

    result = json.loads(await session._review_narrative_tool("You examine a blank page.", context))

    assert result == expected
    calls = session.ai_provider.generate.await_args_list
    assert [call.kwargs["max_tokens"] for call in calls] == [1024, 4096][:len(responses)]
    assert all(call.kwargs["model"] == "synthetic-review" for call in calls)
    assert context["_semantic_review_model_calls"] == len(responses)


@pytest.mark.asyncio
async def test_review_limits_persist_reload_validate_and_invalidate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-review-limits.db")
    monkeypatch.setattr(database, "_pool", None)
    monkeypatch.setattr(database, "_pool_lock", None)
    monkeypatch.setattr(database, "_pool_created", 0)
    monkeypatch.setattr(config_routes, "_provider_cache", {"profile_id": None, "fingerprint": None, "instance": None})
    app = FastAPI()
    app.include_router(config_routes.router, prefix="/api/config")
    await database.init_db()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {"name": "Synthetic limits", "provider_type": "openai_compatible", "model": "synthetic-default"}
            created = await client.post("/api/config/profiles", json=payload)
            assert created.status_code == 200
            profile_id = created.json()["id"]
            previous = None
            for limits in ({}, {"review_max_tokens": 1024, "review_retry_max_tokens": 4096},
                           {"review_max_tokens": 2048}, {"review_retry_max_tokens": 8192}):
                if limits:
                    updated = await client.put(f"/api/config/profiles/{profile_id}", json=limits)
                    assert updated.status_code == 200
                await database.close_pool()
                profile = (await client.get("/api/config/profiles")).json()[0]
                expected = {"review_max_tokens": 500, "review_retry_max_tokens": 1500} if previous is None else previous.copy()
                expected.update(limits)
                assert {key: profile[key] for key in expected} == expected
                assert "api_key" not in profile
                provider = await config_routes.get_ai_provider_instance()
                assert {key: getattr(provider, key) for key in expected} == expected
                previous = expected
            for field in ("review_max_tokens", "review_retry_max_tokens"):
                for value in (0, -1, 32769, True, 1.5, "1024"):
                    invalid = {field: value}
                    assert (await client.post("/api/config/profiles", json={**payload, **invalid})).status_code == 422
                    assert (await client.put(f"/api/config/profiles/{profile_id}", json=invalid)).status_code == 422
            custom = await client.post("/api/config/profiles", json={
                **payload, "review_max_tokens": 1, "review_retry_max_tokens": 32768,
            })
            assert custom.status_code == 200
            profiles = (await client.get("/api/config/profiles")).json()
            custom_profile = next(profile for profile in profiles if profile["id"] == custom.json()["id"])
            assert custom_profile["review_max_tokens"] == 1
            assert custom_profile["review_retry_max_tokens"] == 32768
    finally:
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["review_max_tokens", "review_retry_max_tokens"])
async def test_external_review_limit_change_replaces_cached_provider(monkeypatch, field):
    profile = {"id": 1, "provider_type": "openai_compatible", "model": "synthetic-default",
               "review_max_tokens": 500, "review_retry_max_tokens": 1500}
    monkeypatch.setattr(config_routes, "get_active_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(config_routes, "_provider_cache", {"profile_id": None, "fingerprint": None, "instance": None})
    original = await config_routes.get_ai_provider_instance()
    profile[field] = 2048
    updated = await config_routes.get_ai_provider_instance()
    assert updated is not original
    assert getattr(updated, field) == 2048


@pytest.mark.asyncio
async def test_legacy_profile_migrates_with_unchanged_review_defaults(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-legacy-profile.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE ai_profiles (
                id INTEGER PRIMARY KEY, name TEXT, provider_type TEXT, api_key TEXT,
                model TEXT, max_tokens INTEGER, base_url TEXT, is_active INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, stage_models TEXT
            );
            INSERT INTO ai_profiles (id, name, provider_type, api_key, model, max_tokens, is_active)
            VALUES (1, 'Synthetic legacy', 'openai_compatible', '', 'synthetic-default', 8192, 1);
        """)
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    await database.init_db()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT review_max_tokens, review_retry_max_tokens, max_tokens FROM ai_profiles").fetchone() == (500, 1500, 8192)
