import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from api import config_routes, game_routes
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_regenerate_stream_errors import assert_controls_closed, database_state, regeneration_client, runtime_state


def make_session(provider):
    session = GameSession({"script_id": "synthetic-settings", "settings": {}, "story_tree": {}}, provider)
    session._profile_ai_provider = provider
    session.stage_models = {"review": "old-review"}
    session.task_model_routes = {"summary": "old-summary"}
    return session


@pytest.mark.asyncio
async def test_provider_and_routes_come_from_one_profile_read(monkeypatch):
    first = {"id": 1, "provider_type": "openai_compatible", "model": "first-default",
             "stage_models": json.dumps({"review": "first-review"})}
    second = {**first, "id": 2, "model": "second-default", "stage_models": json.dumps({"review": "second-review"})}
    read_profile = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(config_routes, "get_active_profile", read_profile)
    monkeypatch.setattr(config_routes, "_provider_cache", {"fingerprint": None, "instance": None})
    monkeypatch.setattr(config_routes, "_create_provider_from_profile",
                        lambda profile: SimpleNamespace(model=profile["model"]))
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", {"summary": "new-summary"})
    session = make_session(object())

    await game_routes._refresh_session_ai(session)

    assert read_profile.await_count == 1
    assert session.ai_provider.model == "first-default"
    assert session.stage_models == {"review": "first-review"}
    session.stage_models["review"] = "session-local-edit"
    assert await game_routes._get_stage_models_async(session.ai_provider) == {"review": "first-review"}
    config.TASK_MODEL_ROUTES["summary"] = "later-summary"
    assert session._stage_kwargs("summary") == {"model": "new-summary"}
    await game_routes._refresh_session_ai(session)
    assert read_profile.await_count == 2
    assert session.ai_provider.model == "second-default"
    assert session.stage_models == {"review": "second-review"}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_settings_refresh_waits_for_previous_background_work(monkeypatch, streaming):
    old_provider = SimpleNamespace(generate=AsyncMock(return_value="old"))
    new_provider = SimpleNamespace(generate=AsyncMock(return_value="new"))
    session = make_session(old_provider)
    read_provider = AsyncMock(return_value=new_provider)
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", read_provider)
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={"review": "new-review"}))
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", {"summary": "new-summary"})
    drain_started = asyncio.Event()
    release_background = asyncio.Event()
    background_finished = asyncio.Event()
    original_drain = session._drain_background_tasks

    async def background():
        await release_background.wait()
        assert session.ai_provider is old_provider
        assert session._stage_kwargs("review") == {"model": "old-review"}
        assert session._stage_kwargs("summary") == {"model": "old-summary"}
        await session.ai_provider.generate([], **session._stage_kwargs("review"))
        background_finished.set()

    async def drain(*args, **kwargs):
        drain_started.set()
        return await original_drain(*args, **kwargs)

    async def refresh():
        assert background_finished.is_set()
        await game_routes._refresh_session_ai(session)

    async def query(*args, **kwargs):
        reply = await session.ai_provider.generate([], **session._stage_kwargs("review"))
        return "query", {"query": True, "reply": reply}

    async def act():
        action = {"type": "freeform", "text": "Inspect the empty room."}
        if streaming:
            return [event async for event in session.process_action_stream(action, on_background_drained=refresh)]
        return await session.process_action(action, on_background_drained=refresh)

    monkeypatch.setattr(session, "_drain_background_tasks", drain)
    monkeypatch.setattr(session, "_dispatch_meta_action", query)
    prior = session._schedule_background_task(background())
    current = asyncio.create_task(act())
    try:
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert not current.done()
        assert session.ai_provider is old_provider
        assert read_provider.await_count == 0
        release_background.set()
        result = await asyncio.wait_for(current, timeout=5)
        await prior
        assert session.ai_provider is new_provider
        assert session._stage_kwargs("summary") == {"model": "new-summary"}
        old_provider.generate.assert_awaited_once_with([], model="old-review")
        new_provider.generate.assert_awaited_once_with([], model="new-review")
        if streaming:
            final, = [event for event in result if event["type"] == "final"]
            assert final["narrative"] == "new"
        else:
            assert result["reply"] == "new"
    finally:
        release_background.set()
        if not current.done():
            current.cancel()
        await asyncio.gather(current, prior, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_read", ["provider", "routes"])
async def test_failed_refresh_cannot_partially_replace_settings(monkeypatch, failing_read):
    original_provider = object()
    session = make_session(original_provider)
    before = copy.deepcopy((session.stage_models, session.task_model_routes))
    read_provider = AsyncMock(return_value=object())
    read_routes = AsyncMock(return_value={"review": "new-review"})
    (read_provider if failing_read == "provider" else read_routes).side_effect = RuntimeError("Synthetic read failure")
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", read_provider)
    monkeypatch.setattr(game_routes, "_get_stage_models_async", read_routes)
    with pytest.raises(RuntimeError, match="Synthetic read failure"):
        await game_routes._refresh_session_ai(session)
    assert session.ai_provider is original_provider
    assert session._profile_ai_provider is original_provider
    assert (session.stage_models, session.task_model_routes) == before


@pytest.mark.asyncio
async def test_removing_all_profiles_clears_the_cached_provider(monkeypatch):
    session = make_session(object())
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=None))
    await game_routes._refresh_session_ai(session)
    assert session.ai_provider is None
    assert session.stage_models == {}
    with pytest.raises(RuntimeError, match="AI provider not configured"):
        await session.process_action({"type": "freeform", "text": "Wait."})


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_managed", [False, True])
async def test_directly_injected_provider_is_not_replaced_from_web_settings(monkeypatch, previously_managed):
    session = make_session(object())
    if previously_managed:
        session.ai_provider = object()
    else:
        del session._profile_ai_provider
    original_provider = session.ai_provider
    read_provider = AsyncMock()
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", read_provider)
    await game_routes._refresh_session_ai(session)
    assert session.ai_provider is original_provider
    assert session.stage_models == {"review": "old-review"}
    read_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_aborting_during_async_refresh_prevents_generation(monkeypatch):
    session = make_session(SimpleNamespace(generate=AsyncMock()))
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    new_provider = SimpleNamespace(generate=AsyncMock())

    async def read_provider():
        read_started.set()
        await release_read.wait()
        return new_provider

    async def refresh():
        await game_routes._refresh_session_ai(session)

    async def act():
        return [event async for event in session.process_action_stream(
            {"type": "freeform", "text": "Wait."}, on_background_drained=refresh,
        )]

    monkeypatch.setattr(game_routes, "_get_ai_provider_async", read_provider)
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    query = AsyncMock()
    monkeypatch.setattr(session, "_dispatch_meta_action", query)
    current = asyncio.create_task(act())
    try:
        await asyncio.wait_for(read_started.wait(), timeout=5)
        session._abort_flag = True
        session._agent_stop_event.set()
        release_read.set()
        events = await asyncio.wait_for(current, timeout=5)
        assert [event["type"] for event in events][-2:] == ["aborted", "agent_controls_closed"]
        query.assert_not_awaited()
        new_provider.generate.assert_not_awaited()
        assert session.turn_number == 0
    finally:
        release_read.set()
        if not current.done():
            current.cancel()
        await asyncio.gather(current, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["all", "state", "choices"])
async def test_regeneration_abort_during_refresh_preserves_the_save(tmp_path, monkeypatch, stage):
    async with regeneration_client(tmp_path, monkeypatch, stage) as (session, client, _sessions):
        await session._drain_background_tasks(timeout=None)
        before_runtime = runtime_state(session)
        before_database = await database_state(session)
        session._profile_ai_provider = session.ai_provider
        read_started = asyncio.Event()
        release_read = asyncio.Event()
        new_provider = SimpleNamespace(generate=AsyncMock(), generate_with_tools=AsyncMock())

        async def read_provider():
            read_started.set()
            await release_read.wait()
            return new_provider

        monkeypatch.setattr(game_routes, "_get_ai_provider_async", read_provider)
        stream_id = "synthetic-settings-stop"
        request = asyncio.create_task(client.post(
            f"/api/game/{session.save_id}/regenerate/stream", params={"stage": stage, "stream_id": stream_id},
        ))
        try:
            await asyncio.wait_for(read_started.wait(), timeout=5)
            stopped = await client.post(f"/api/game/{session.save_id}/abort", json={"stream_id": stream_id})
            assert stopped.status_code == 200
            release_read.set()
            response = await asyncio.wait_for(request, timeout=5)
            assert response.status_code == 200
            events = _stream_events(response.text)
            assert any(event["type"] == "aborted" for event in events)
            assert not any(event["type"] in ("final", "error") for event in events)
            new_provider.generate.assert_not_awaited()
            new_provider.generate_with_tools.assert_not_awaited()
            assert runtime_state(session) == before_runtime
            assert await database_state(session) == before_database
            assert_controls_closed(session)
        finally:
            release_read.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_previously_unconfigured_session_can_load_its_first_provider(monkeypatch, streaming):
    session = make_session(None)
    provider = SimpleNamespace(generate=AsyncMock())
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(session, "_dispatch_meta_action", AsyncMock(return_value=("query", {"reply": "Ready."})))

    async def refresh():
        await game_routes._refresh_session_ai(session)

    action = {"type": "freeform", "text": "Wait."}
    if streaming:
        events = [event async for event in session.process_action_stream(action, on_background_drained=refresh)]
        assert any(event["type"] == "final" for event in events)
    else:
        assert (await session.process_action(action, on_background_drained=refresh))["reply"] == "Ready."
    assert session.ai_provider is provider


@pytest.mark.asyncio
async def test_runtime_route_edit_does_not_split_a_review_retry(monkeypatch):
    monkeypatch.setattr(config, "TASK_MODEL_ROUTES", {"review": "first-review"})

    async def generate(*args, **kwargs):
        if provider.generate.await_count == 1:
            config.TASK_MODEL_ROUTES["review"] = "next-review"
            return ""
        return '{"pass": true, "violations": []}'

    provider = SimpleNamespace(generate=AsyncMock(side_effect=generate), _tavern_stage_models={})
    session = make_session(provider)
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    await game_routes._refresh_session_ai(session)
    assert await session._review_narrative("You inspect an empty table.", "Inspect.", {}) is not None
    assert [call.kwargs["model"] for call in provider.generate.await_args_list] == ["first-review", "first-review"]
    await game_routes._refresh_session_ai(session)
    assert await session._review_narrative("You inspect an empty table.", "Inspect.", {}) is not None
    assert provider.generate.await_args.kwargs["model"] == "next-review"


@pytest.mark.asyncio
@pytest.mark.parametrize("route_source", ["profile", "global"])
@pytest.mark.parametrize("retry", [False, True])
async def test_deferred_blueprint_keeps_its_scheduled_provider_and_routes(monkeypatch, route_source, retry):
    responses = [RuntimeError("Synthetic transient failure")] if retry else []
    responses.append('{"plot_threads": []}')
    old_provider = SimpleNamespace(generate=AsyncMock(side_effect=responses))
    new_provider = SimpleNamespace(generate=AsyncMock())
    session = make_session(old_provider)
    session.stage_models = {"knowledge_graph": "old-planner"} if route_source == "profile" else {}
    session.task_model_routes = {"knowledge_graph": "old-planner"}
    monkeypatch.setattr(session, "_script_blueprint_source", lambda: ("test-root", 0, "", (), "Synthetic visible history."))
    monkeypatch.setattr(session, "_script_anchor_nodes", lambda: [{"id": "test-anchor"}])
    monkeypatch.setattr(session, "_script_anchor_briefs", lambda: [{"alias": "A1", "description": "Synthetic test condition."}])
    session._start_script_blueprint({"game_time": "2030-01-01T09:00:00"})
    task = session._deferred_story_task
    assert task is not None
    session.ai_provider = new_provider
    session.stage_models = {"knowledge_graph": "new-planner"}
    session.task_model_routes = {"knowledge_graph": "new-planner"}
    try:
        assert await asyncio.wait_for(task, timeout=5) == {"plot_threads": []}
        assert old_provider.generate.await_count == (2 if retry else 1)
        assert all(call.kwargs["model"] == "old-planner" for call in old_provider.generate.await_args_list)
        new_provider.generate.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
