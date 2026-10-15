import asyncio
import copy
import json
import re
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from engine.session.agentic_mixin import AppliedSettlementReviewError
from engine.turn_publication import begin_publication
from test_agent_harness_execution import _stream_events
from test_story_choice_settlement import choice_client


def applied_session(origin):
    script = {
        "script_id": "synthetic-applied-review", "name": "Synthetic applied review", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Practice room"}],
        "npcs": [{"id": "test_guide", "name": "Practice guide", "location": "test_room"}],
        "variables": [{"id": "test_counter", "type": "number", "default": 0}],
        "story_tree": {}, "random_items": [],
    }
    if origin == "after_ai":
        script["triggers"] = [{"id": "test_after", "event": "after_ai", "action": "set_var",
                               "params": {"var_id": "test_counter", "op": "inc", "value": 1}}]
    elif origin == "scheduled":
        script["one_time_events"] = [{"id": "test_scheduled", "trigger_time": "2030-01-01T09:00:30",
                                      "effects": [{"type": "state_change", "target": "script_variables.test_counter", "value": 1}]}]
    session = GameSession(script, None, save_id="synthetic-applied-review-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.current_state["player"]["relationships"] = {"test_guide": {"trust": 0, "affection": 0, "fear": 0}}
    return session


CASES = [(streaming, outcome) for streaming in (False, True) for outcome in ("pass", "reject", "save_failure")]
CASES.append((True, "abort"))
CASES.extend([(False, "drift_before_save"), (True, "drift_before_save")])
CASES.extend([(False, "unavailable"), (True, "unavailable")])


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["npc", "after_ai", "scheduled"])
@pytest.mark.parametrize("streaming,outcome", CASES)
async def test_applied_review_precedes_durable_publication(tmp_path, monkeypatch, origin, streaming, outcome):
    monkeypatch.setattr(database, "_pool_lock", None)
    session = applied_session(origin)
    baseline = copy.deepcopy(session.current_state)
    started, release = asyncio.Event(), asyncio.Event()
    publications, actual_reviews, lifecycle_calls = [], [], []
    foreground_calls = 0
    reject = outcome == "reject"
    fail_save = outcome == "save_failure"
    drift_before_save = outcome == "drift_before_save"
    narrative = ("One minute passes while you finish the permitted action. The declared update takes effect. "
                 "You make no additional decision and leave the rest of the room undisturbed.")
    if reject:
        narrative = "One minute passes. The practice counter is still zero and the guide's trust remains unchanged."
    expected_counter, expected_trust = int(origin != "npc"), 5 if origin == "npc" else 0
    operations = ([{"op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 5,
                    "reason": "Synthetic permitted change."}] if origin == "npc" else [])

    async def tools(messages, **kwargs):
        nonlocal foreground_calls
        names = {entry["function"]["name"] for entry in kwargs["tools"]}
        if names == {"extract_notes"}:
            batch = [("extract_notes", {"notes": []})]
        else:
            foreground_calls += 1
            assert foreground_calls <= 2
            batch = ([("submit_plan", {"plan": "Resolve the current practice action."})] if foreground_calls == 1 else
                     [("write_narrative", {"text": narrative}),
                      ("finalize_turn", {"end_time": "2030-01-01T09:01:00", "npc_operations": operations,
                                         "summary": "Resolved the permitted action."})])
        return {"content": "", "tool_calls": [
            {"id": f"synthetic-applied-{foreground_calls}-{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(batch)
        ]}

    async def generate(messages, **kwargs):
        content = "\n".join(message.get("content") or "" for message in messages)
        match = re.search(r"<execution_candidate>\s*(.*?)\s*</execution_candidate>", content, re.S)
        if match:
            snapshot = json.loads(match.group(1))
            if snapshot["scope"] == "applied_turn":
                actual_reviews.append(snapshot)
                values = {tuple(entry["path"]): entry["candidate"] for entry in snapshot["values"]}
                assert values[("script_variables", "test_counter")] == expected_counter
                if origin == "npc":
                    assert values[("player", "relationships", "test_guide", "trust")] == expected_trust
                started.set()
                await release.wait()
                if outcome == "unavailable":
                    return ""
                return json.dumps({"pass": not reject, "violations": [
                    {"type": "settlement_mismatch", "detail": "The final indicator contradicts the narrative."},
                ] if reject else []})
            return '{"pass":true,"violations":[]}'
        return json.dumps({"scene_type": "exploration", "scope": "minor", "systems": ["location"],
                           "focus_npcs": ["test_guide"], "skill_check": {"needed": False},
                           "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
                           "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal"})

    async def record_publication(kind):
        async with database.get_db() as connection:
            count = (await (await connection.execute("SELECT count(*) FROM tree_nodes")).fetchone())[0]
        publications.append((kind, count))

    async def metadata_job(context):
        await record_publication("metadata")

    event = SimpleNamespace(id="synthetic-publication", handler="_test_metadata_job",
                            trigger=SimpleNamespace(state_flag=None, consume_flag=False))
    monkeypatch.setattr(session.meta_event_bus, "evaluate", lambda **kwargs: [event])
    monkeypatch.setattr(session.meta_event_bus, "mark_fired", lambda *args: None)
    monkeypatch.setattr(session, "_test_metadata_job", metadata_job, raising=False)
    dispatch = session._dispatch_meta_events

    def dispatch_meta(context, parsed, action, raw_response=""):
        dispatch(context, parsed, action, raw_response)
        session._schedule_index_task(record_publication("index"))

    monkeypatch.setattr(session, "_dispatch_meta_events", dispatch_meta)
    lifecycle = session._fire_lifecycle_event.__func__

    def observe_lifecycle(current, name, *args, **kwargs):
        if name == "after_ai":
            lifecycle_calls.append(name)
        return lifecycle(current, name, *args, **kwargs)

    monkeypatch.setattr(session, "_fire_lifecycle_event", MethodType(observe_lifecycle, session))
    provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=tools), generate=AsyncMock(side_effect=generate))
    session.ai_provider = provider
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        original_save = game_routes._save_node

        async def save(current, result, *args):
            assert publications == []
            if fail_save:
                raise RuntimeError("Synthetic save rejection")
            if drift_before_save:
                current.current_state["script_variables"]["test_counter"] += 100
            return await original_save(current, result, *args)

        monkeypatch.setattr(game_routes, "_save_node", save)
        endpoint = f"/api/game/{session.save_id}/action" + ("/stream" if streaming else "")
        action = {"type": "freeform", "text": "Resolve the permitted action.", "intent_hint": "explicit_action",
                  "stream_id": "synthetic-applied-stream", "action_id": "synthetic-applied-action",
                  "expected_node_id": session.world_tree.active_node_id}
        request = asyncio.create_task(client.post(endpoint, json=action))
        reader = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert lifecycle_calls == ["after_ai"] and publications == []
            async with database.get_db() as connection:
                assert (await (await connection.execute("SELECT count(*) FROM tree_nodes")).fetchone())[0] == 1
            reader = asyncio.create_task(client.get(f"/api/game/{session.save_id}/state"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), 0.03)
            if outcome == "abort":
                injected = await client.post(f"/api/game/{session.save_id}/inject", json={
                    "stream_id": action["stream_id"], "message": "An input after settlement started.",
                })
                assert injected.status_code == 409
                aborted = await client.post(f"/api/game/{session.save_id}/abort", json={"stream_id": action["stream_id"]})
                assert aborted.status_code == 200
            else:
                release.set()
            response = await asyncio.wait_for(request, 5)
            await asyncio.wait_for(reader, 5)
        finally:
            release.set()
            for pending in (request, reader):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(pending for pending in (request, reader) if pending is not None), return_exceptions=True)
        if outcome not in {"pass", "unavailable"}:
            if streaming:
                events = _stream_events(response.text)
                assert not any(item["type"] == "final" for item in events)
                expected_type = "aborted" if outcome == "abort" else "error"
                assert any(item["type"] == expected_type and item.get("discard_partial") for item in events)
                if outcome == "abort":
                    assert not any(item["type"] in {"error", "agent_stop_missed"} for item in events)
            else:
                assert response.status_code >= 400
            assert session.current_state == baseline and publications == []
            assert session._pending_turn_publication is None
            foreground_calls, reject, fail_save, drift_before_save = 0, False, False, False
            narrative = "One minute passes. The permitted update takes effect and you make no further decision."
            response = await asyncio.wait_for(client.post(endpoint, json=action), 10)
            assert len(lifecycle_calls) == 2
        else:
            assert len(lifecycle_calls) == 1
        if streaming:
            final, = [item for item in _stream_events(response.text) if item["type"] == "final"]
        else:
            assert response.status_code == 200, response.text
            final = response.json()
        assert final["agent_trace"]["applied_review_status"] == ("unavailable" if outcome == "unavailable" else "passed")
        if outcome == "unavailable":
            assert any("不能视为已通过" in warning for warning in final["warnings"])
        await session._drain_background_tasks()
        assert sorted(publications) == [("index", 2), ("metadata", 2)]
        assert session.current_state["script_variables"]["test_counter"] == expected_counter
        assert session.current_state["player"]["relationships"]["test_guide"]["trust"] == expected_trust
        assert session._pending_turn_publication is None
        calls = provider.generate_with_tools.await_count
        if streaming:
            replay = await client.post(endpoint, json=action)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == calls
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_counter"] == expected_counter
        assert restored.current_state["player"]["relationships"]["test_guide"]["trust"] == expected_trust


@pytest.mark.asyncio
async def test_publication_discard_keeps_prior_index_work_and_isolates_saves():
    first, second = applied_session("after_ai"), applied_session("after_ai")
    events = []
    release = asyncio.Event()

    async def record(label, wait=False):
        if wait:
            await release.wait()
        events.append(label)

    previous = first._schedule_index_task(record("prior", True))
    publication = begin_publication(first)
    assert first._schedule_index_task(record("discarded")) is None
    other = second._schedule_index_task(record("other"))
    publication.discard()
    release.set()
    await asyncio.gather(previous, other)
    assert sorted(events) == ["other", "prior"]
    publication = begin_publication(first)
    first._schedule_index_task(record("first"))
    first._schedule_index_task(record("second"))
    publication.publish()
    await first._drain_background_tasks()
    assert events[-2:] == ["first", "second"]


@pytest.mark.asyncio
async def test_unchanged_applied_state_reuses_candidate_without_another_model_call():
    session = applied_session("after_ai")
    session.ai_provider = SimpleNamespace(generate=AsyncMock())
    context = {"_unified_narrative_output": True, "_reviewed_candidate_state": copy.deepcopy(session.current_state)}
    status = await session._review_applied_turn("You wait.", {"text": "Wait."}, context, session.current_state)
    assert status == "candidate_unchanged"
    session.ai_provider.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_scheduler_bookkeeping_alone_does_not_repeat_semantic_review():
    session = applied_session("after_ai")
    before = copy.deepcopy(session.current_state)
    session.current_state["meta_event_trackers"] = {"synthetic_routine": {"last_fired": 1}}
    session.ai_provider = SimpleNamespace(generate=AsyncMock())
    context = {"_unified_narrative_output": True, "_reviewed_candidate_state": before}
    assert await session._review_applied_turn("You wait.", {"text": "Wait."}, context, session.current_state) == "candidate_unchanged"
    session.ai_provider.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_applied_state_cannot_change_during_review():
    session = applied_session("after_ai")
    before = copy.deepcopy(session.current_state)
    session.current_state["script_variables"]["test_counter"] = 1

    async def mutate_during_review(*args, **kwargs):
        session.current_state["script_variables"]["test_counter"] = 2
        return '{"pass":true,"violations":[]}'

    session.ai_provider = SimpleNamespace(generate=AsyncMock(side_effect=mutate_during_review))
    context = {"_unified_narrative_output": True, "_reviewed_candidate_state": before, "_review_state_before": before}
    with pytest.raises(AppliedSettlementReviewError):
        await session._review_applied_turn("You wait.", {"text": "Wait."}, context, session.current_state)


@pytest.mark.asyncio
async def test_published_background_updates_cannot_mutate_the_saved_response(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_pool_lock", None)
    session = applied_session("after_ai")
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        publication = begin_publication(session)
        result = {"node_id": session.world_tree.active_node_id, "narrative": "Synthetic stable result.", "state": session.current_state}
        publication.remember_review(result["narrative"], session.current_state)

        async def update_after_commit():
            session.current_state["script_variables"]["test_counter"] = 9

        session._schedule_index_task(update_after_commit())
        await game_routes._save_node(session, result)
        await session._drain_background_tasks()
        assert session.current_state["script_variables"]["test_counter"] == 9
        assert result["state"]["script_variables"]["test_counter"] == 0
        async with database.get_db() as connection:
            stored = await (await connection.execute("SELECT state_snapshot FROM tree_nodes")).fetchone()
        assert json.loads(stored[0])["script_variables"]["test_counter"] == 0


@pytest.mark.asyncio
async def test_normal_shutdown_persists_completed_post_commit_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_pool_lock", None)
    session = applied_session("after_ai")
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        publication = begin_publication(session)
        result = {"node_id": session.world_tree.active_node_id, "narrative": "Synthetic committed turn.", "state": session.current_state}

        async def metadata_update():
            session.current_state["script_variables"]["test_counter"] = 9

        publication.defer(metadata_update(), "background")
        await game_routes._save_node(session, result)
        await game_routes._persist_sessions_on_shutdown()
        assert result["state"]["script_variables"]["test_counter"] == 0
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_counter"] == 9


@pytest.mark.asyncio
async def test_acknowledged_stop_survives_the_transition_to_applied_review():
    session = applied_session("after_ai")
    session.ai_provider = SimpleNamespace(generate=AsyncMock())
    session._active_agent_stream = True
    session._abort_request_pending = True
    session._abort_flag = False
    context = {"_unified_narrative_output": True, "_reviewed_candidate_state": copy.deepcopy(session.current_state)}
    with pytest.raises(AppliedSettlementReviewError, match="已停止"):
        await session._review_applied_turn("You wait.", {"text": "Wait."}, context, session.current_state)
    session.ai_provider.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_stream_regeneration_preparation_discards_queued_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_pool_lock", None)
    session = applied_session("after_ai")
    published = []

    async def index_job():
        published.append(True)

    async def fail_refresh(current):
        current._schedule_index_task(index_job())
        raise RuntimeError("Synthetic preparation rejection")

    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        parent_id = session.world_tree.active_node_id
        child = session.world_tree.add_node(parent_id, session.current_state["game_time"], 1,
                                           state_snapshot=copy.deepcopy(session.current_state))
        session.turn_number = 1
        await game_routes._save_node(session, {"node_id": child, "state": session.current_state, "narrative": "Synthetic previous turn."})
        monkeypatch.setattr(game_routes, "_refresh_session_ai", fail_refresh)
        response = await client.post(f"/api/game/{session.save_id}/regenerate/stream?stage=all")
        assert any(event["type"] == "error" for event in _stream_events(response.text))
        assert published == [] and session._pending_turn_publication is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["npc", "after_ai", "scheduled"])
@pytest.mark.parametrize("streaming,outcome", [
    (streaming, outcome) for streaming in (False, True) for outcome in ("pass", "reject", "save_failure")
] + [(True, "abort")])
async def test_full_regeneration_checks_applied_state_before_publishing(tmp_path, monkeypatch, origin, streaming, outcome):
    monkeypatch.setattr(database, "_pool_lock", None)
    session = applied_session(origin)
    counter, trust = int(origin != "npc"), 5 if origin == "npc" else 0
    foreground_calls = 0
    reject = False
    stop_review = False
    started, release = asyncio.Event(), asyncio.Event()
    reviews, publications = [], []
    narrative = "A minute passes. The permitted update takes effect once, and you make no further decision."
    operations = ([{"op": "attitude", "npc_id": "test_guide", "dimension": "trust", "change": 5,
                    "reason": "Synthetic permitted update."}] if origin == "npc" else [])

    async def publication_job():
        publications.append(session.current_state["script_variables"]["test_counter"])

    async def tools(messages, **kwargs):
        nonlocal foreground_calls
        names = {entry["function"]["name"] for entry in kwargs["tools"]}
        if names == {"extract_notes"}:
            batch = [("extract_notes", {"notes": []})]
        else:
            foreground_calls += 1
            assert foreground_calls <= 2
            if foreground_calls == 1:
                session._schedule_index_task(publication_job())
                batch = [("submit_plan", {"plan": "Resolve the current practice action."})]
            else:
                batch = [("write_narrative", {"text": narrative}),
                         ("finalize_turn", {"end_time": "2030-01-01T09:01:00", "npc_operations": operations,
                                            "summary": "Resolved the action."})]
        return {"content": "", "tool_calls": [
            {"id": f"synthetic-applied-regen-{foreground_calls}-{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(batch)
        ]}

    async def generate(messages, **kwargs):
        text = "\n".join(message.get("content") or "" for message in messages)
        match = re.search(r"<execution_candidate>\s*(.*?)\s*</execution_candidate>", text, re.S)
        if match:
            snapshot = json.loads(match.group(1))
            if snapshot["scope"] == "applied_turn":
                assert publications == []
                reviews.append(snapshot)
                values = {tuple(entry["path"]): entry["candidate"] for entry in snapshot["values"]}
                assert values[("script_variables", "test_counter")] == counter
                if origin == "npc":
                    assert values[("player", "relationships", "test_guide", "trust")] == trust
                if stop_review:
                    started.set()
                    await release.wait()
                return json.dumps({"pass": not reject, "violations": [
                    {"type": "settlement_mismatch", "detail": "Synthetic final-state contradiction."},
                ] if reject else []})
            return '{"pass":true,"violations":[]}'
        return json.dumps({"scene_type": "exploration", "scope": "minor", "systems": ["location"],
                           "focus_npcs": ["test_guide"], "skill_check": {"needed": False},
                           "expand_story": False, "generate_image": False, "skip_npc_reaction": True,
                           "skip_choices": True, "skip_state_settlement": False, "context_depth": "normal"})

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=tools), generate=AsyncMock(side_effect=generate))
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        seeded = await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Resolve the permitted action.", "intent_hint": "explicit_action",
        })
        assert any(event["type"] == "final" for event in _stream_events(seeded.text))
        await session._drain_background_tasks()
        foreground_calls = 0
        publications.clear()
        reviews.clear()
        baseline = copy.deepcopy(session.current_state)
        node_id = session.world_tree.active_node_id
        original_node = copy.deepcopy(session.world_tree.get_node(node_id))
        reject = outcome == "reject"
        stop_review = outcome == "abort"
        endpoint = f"/api/game/{session.save_id}/regenerate" + ("/stream?stage=all" if streaming else "?stage=all")
        if outcome == "save_failure":
            async def fail_save(*args, **kwargs):
                assert publications == []
                raise RuntimeError("Synthetic regeneration save rejection")
            monkeypatch.setattr(game_routes, "_save_node" if streaming else "_update_node_in_db", fail_save)
        if stop_review:
            stream_id = "synthetic-applied-regen-stop"
            request = asyncio.create_task(client.post(endpoint + "&stream_id=" + stream_id))
            try:
                await asyncio.wait_for(started.wait(), 5)
                aborted = await client.post(f"/api/game/{session.save_id}/abort", json={"stream_id": stream_id})
                assert aborted.status_code == 200
                response = await asyncio.wait_for(request, 5)
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        else:
            response = await asyncio.wait_for(client.post(endpoint), 15)
        assert reviews
        if outcome != "pass":
            if streaming:
                events = _stream_events(response.text)
                assert not any(event["type"] == "final" for event in events)
                expected_type = "aborted" if stop_review else "error"
                assert any(event["type"] == expected_type for event in events)
                if stop_review:
                    assert not any(event["type"] in {"error", "agent_stop_missed"} for event in events)
            else:
                assert response.status_code >= 400
            assert session.current_state == baseline
            assert session.world_tree.get_node(node_id) == original_node
            assert publications == [] and session._pending_turn_publication is None
        else:
            if streaming:
                final, = [event for event in _stream_events(response.text) if event["type"] == "final"]
            else:
                assert response.status_code == 200, response.text
                final = response.json()
            assert final["agent_trace"]["applied_review_status"] == "passed"
            await session._drain_background_tasks()
            assert publications == [counter]
            assert session.current_state["script_variables"]["test_counter"] == counter
            assert session.current_state["player"]["relationships"]["test_guide"]["trust"] == trust
            assert len(session.world_tree.get_node(node_id)["swipes"]) == 2
        async with database.get_db() as connection:
            assert (await (await connection.execute("SELECT count(*) FROM tree_nodes")).fetchone())[0] == 2
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_counter"] == counter
        assert restored.current_state["player"]["relationships"]["test_guide"]["trust"] == trust
