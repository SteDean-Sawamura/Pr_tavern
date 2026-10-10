import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from engine.game_session import GameSession
from engine.session import background_tasks_mixin


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reveal", "invalidate"])
async def test_state_lore_index_updates_run_outside_event_loop(operation):
    session = GameSession({"script_id": "lore-index-test", "settings": {}}, None)
    session.current_state = {"player": {"location": "street"}, "npcs": {}, "dynamic_lorebook": []}
    event_loop_thread = threading.get_ident()
    writes = []
    session.vector_memory = SimpleNamespace(
        add_lorebook=lambda *args: writes.append(("add", threading.get_ident())),
        remove_lorebook=lambda *args: writes.append(("remove", threading.get_ident())),
    )
    if operation == "reveal":
        parsed = {"reveal_locations": [{"id": "archive", "name": "Archive", "description": "Records room"}]}
    else:
        parsed = {"invalidate_lore": ["old_fact"]}

    session._apply_common_parsed_changes(session.current_state, parsed, inplace=True)
    await session._drain_background_tasks(index_only=True)

    assert len(writes) == 1
    assert writes[0][0] == ("add" if operation == "reveal" else "remove")
    assert writes[0][1] != event_loop_thread


@pytest.mark.asyncio
async def test_npc_kg_refresh_finishes_before_next_rag_query():
    session = GameSession({"script_id": "npc-kg-index-test", "settings": {}}, None)
    session.current_state = {"player": {"location": "street"}, "npcs": {}}
    session.data_bank = None
    session.prompt_builder.lorebook.add_entries([{
        "id": "_kg_npc_source", "keys": ["线人"],
        "content": "线人在街口等待记者，并掌握最新的公开线索。",
    }])
    index_started = threading.Event()
    release_index = threading.Event()
    queries = []

    def add_lorebook_batch(batch):
        index_started.set()
        release_index.wait(timeout=2)

    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=add_lorebook_batch,
        query=lambda *args: queries.append(args) or [],
    )
    session._sync_kg_entries_to_vector(["source"], [])
    lookup = asyncio.create_task(session._enrich_with_rag("询问线人", [], [], ""))
    try:
        assert await asyncio.to_thread(index_started.wait, 1)
        await asyncio.sleep(0)
        assert not lookup.done()
        assert not queries
        release_index.set()
        await asyncio.wait_for(lookup, timeout=1)
        assert len(queries) == 1
    finally:
        release_index.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
async def test_lore_evolution_overflow_removes_vector_outside_event_loop():
    session = GameSession({"script_id": "lore-overflow-test", "settings": {}}, None)
    session.current_state = {
        "dynamic_lorebook": [{"id": "stale"}] + [{"id": f"kept_{index}"} for index in range(40)],
    }
    loop_thread = threading.get_ident()
    removals = []
    session.vector_memory = SimpleNamespace(
        remove_lorebook=lambda entry_id: removals.append((entry_id, threading.get_ident())),
    )

    await session._apply_lore_evolution_updates([])
    await session._drain_background_tasks(index_only=True)

    assert len(removals) == 1
    assert removals[0][0] == "stale"
    assert removals[0][1] != loop_thread
    assert "stale" not in [entry["id"] for entry in session.current_state["dynamic_lorebook"]]


@pytest.mark.asyncio
async def test_event_lore_removal_waits_for_prior_index_write():
    session = GameSession({"script_id": "event-lore-removal-test", "settings": {}}, None)
    session.current_state = {"player": {"location": "street"}}
    session.data_bank = None
    index_started = threading.Event()
    release_index = threading.Event()
    operations = []
    queries = []
    loop_thread = threading.get_ident()

    def add_lorebook_batch(batch):
        index_started.set()
        release_index.wait(timeout=2)
        operations.append(("add", threading.get_ident()))

    session.vector_memory = SimpleNamespace(
        add_lorebook_batch=add_lorebook_batch,
        remove_lorebook=lambda entry_id: operations.append(("remove", threading.get_ident())),
        query=lambda *args: queries.append(args) or [],
    )
    session._schedule_index_task(session._async_lorebook_vector_sync([("stale", "old text " * 4, {})]))
    try:
        assert await asyncio.to_thread(index_started.wait, 1)
        session._apply_effects_and_lore(
            effects=[], lore_activations=[], lore_deactivations=[],
            lore_additions=[], lore_updates=[], lore_removals=["stale"],
            game_events=[], narrative_callbacks=[], state_activations=[],
        )
        lookup = asyncio.create_task(session._enrich_with_rag("old text", [], [], ""))
        await asyncio.sleep(0)
        assert not operations
        assert not queries
        release_index.set()
        await asyncio.wait_for(lookup, timeout=1)
        assert [operation for operation, _ in operations] == ["add", "remove"]
        assert operations[1][1] != loop_thread
        assert len(queries) == 1
    finally:
        release_index.set()
        await session._drain_background_tasks()


def test_event_lore_removal_without_event_loop_remains_synchronous():
    session = GameSession({"script_id": "sync-lore-removal-test", "settings": {}}, None)
    removals = []
    session.vector_memory = SimpleNamespace(remove_lorebook=removals.append)

    session._apply_effects_and_lore(
        effects=[], lore_activations=[], lore_deactivations=[],
        lore_additions=[], lore_updates=[], lore_removals=["obsolete"],
        game_events=[], narrative_callbacks=[], state_activations=[],
    )

    assert removals == ["obsolete"]


@pytest.mark.asyncio
async def test_cancelled_lore_removal_keeps_prior_index_write_alive():
    session = GameSession({"script_id": "index-cancellation-test", "settings": {}}, None)
    session.vector_memory = SimpleNamespace(remove_lorebook=lambda entry_id: None)
    index_started = asyncio.Event()
    release_index = asyncio.Event()

    async def index_turn():
        index_started.set()
        await release_index.wait()

    prior_task = session._schedule_index_task(index_turn())
    await index_started.wait()
    session._remove_lorebook_vector("stale")
    removal_task = next(task for task in session._index_tasks if task is not prior_task)
    try:
        await asyncio.sleep(0)
        removal_task.cancel()
        await asyncio.gather(removal_task, return_exceptions=True)
        assert not prior_task.cancelled()
        assert not prior_task.done()
    finally:
        release_index.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
async def test_index_only_timeout_keeps_write_registered_for_later_query():
    session = GameSession({"script_id": "index-timeout-test", "settings": {}}, None)
    session.current_state = {"player": {"location": "street"}, "npcs": {}}
    session.data_bank = None
    index_started = threading.Event()
    release_index = threading.Event()
    queries = []

    def write_index():
        index_started.set()
        release_index.wait(timeout=2)

    session.vector_memory = SimpleNamespace(query=lambda *args: queries.append(args) or [])
    index_task = session._schedule_index_task(asyncio.to_thread(write_index))
    original_drain = session._drain_background_tasks

    async def short_drain(*, index_only=False):
        return await original_drain(timeout=0.01, index_only=index_only)

    session._drain_background_tasks = short_drain
    try:
        assert await asyncio.to_thread(index_started.wait, 1)
        context = {}
        await session._enrich_with_rag("inspect", [], [], "", context)
        assert not queries
        assert "vector_query" not in context["retrieval_timings_ms"]
        assert not index_task.done()
        assert index_task in session._index_tasks

        release_index.set()
        await original_drain(index_only=True)
        await session._enrich_with_rag("inspect", [], [], "")
        assert len(queries) == 1
    finally:
        release_index.set()
        await original_drain()


@pytest.mark.asyncio
async def test_branch_drain_waits_for_index_after_timeout():
    session = GameSession({"script_id": "branch-index-timeout-test", "settings": {}}, None)
    index_started = asyncio.Event()
    release_index = asyncio.Event()

    async def index_turn():
        index_started.set()
        await release_index.wait()

    index_task = session._schedule_index_task(index_turn())
    await index_started.wait()
    drain = asyncio.create_task(session._drain_background_tasks(timeout=0.01))
    try:
        await asyncio.sleep(0.04)
        assert not drain.done()
        assert not index_task.cancelled()
    finally:
        release_index.set()
        await asyncio.wait_for(drain, timeout=1)


@pytest.mark.asyncio
async def test_branch_drain_includes_index_scheduled_by_state_task():
    session = GameSession({"script_id": "late-index-test", "settings": {}}, None)
    index_started = asyncio.Event()
    release_index = asyncio.Event()

    async def index_turn():
        index_started.set()
        await release_index.wait()

    async def update_state():
        session._schedule_index_task(index_turn())

    session._schedule_background_task(update_state())
    drain = asyncio.create_task(session._drain_background_tasks(timeout=0.01))
    try:
        await asyncio.wait_for(index_started.wait(), timeout=1)
        await asyncio.sleep(0.04)
        assert not drain.done()
    finally:
        release_index.set()
        await asyncio.wait_for(drain, timeout=1)


@pytest.mark.asyncio
async def test_slow_background_task_log_identifies_handler(monkeypatch, caplog):
    clock = iter((10.0, 11.5))
    monkeypatch.setattr(background_tasks_mixin, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    session = GameSession({"script_id": "timing-test", "settings": {}}, None)

    async def index_turn():
        return None

    with caplog.at_level(logging.INFO, logger=background_tasks_mixin.__name__):
        session._schedule_background_task(index_turn())
        await session._drain_background_tasks()
        await asyncio.sleep(0)

    assert "后台任务耗时: index_turn 1.50s" in caplog.text
    assert not session._background_tasks


@pytest.mark.asyncio
async def test_background_timeout_log_identifies_cancelled_handler(caplog):
    session = GameSession({"script_id": "timeout-test", "settings": {}}, None)
    blocked = asyncio.Event()

    async def update_state():
        await blocked.wait()
        session.current_state["updated"] = True

    with caplog.at_level(logging.WARNING, logger=background_tasks_mixin.__name__):
        session._schedule_background_task(update_state())
        await session._drain_background_tasks(timeout=0.01)

    assert "update_state" in caplog.text
    assert not session.current_state.get("updated")
    assert not session._background_tasks


@pytest.mark.asyncio
async def test_index_write_overlaps_state_drain_but_finishes_before_rag_query():
    session = GameSession({"script_id": "index-test", "settings": {}}, None)
    session.current_state = {}
    session.data_bank = None
    queries = []
    session.vector_memory = SimpleNamespace(query=lambda *args: queries.append(args) or [])
    index_started = asyncio.Event()
    release_index = asyncio.Event()

    async def index_turn():
        index_started.set()
        await release_index.wait()

    async def update_state():
        session.current_state["ready"] = True

    session._schedule_index_task(index_turn())
    session._schedule_background_task(update_state())
    try:
        await session._drain_background_tasks(include_index=False)
        assert session.current_state["ready"] is True
        await asyncio.wait_for(index_started.wait(), timeout=1)
        assert len(session._index_tasks) == 1

        lookup = asyncio.create_task(session._enrich_with_rag("action", [], [], ""))
        await asyncio.sleep(0)
        assert not lookup.done()
        assert not queries
        release_index.set()
        await asyncio.wait_for(lookup, timeout=1)
        assert len(queries) == 1
    finally:
        release_index.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
async def test_rag_waits_for_index_without_waiting_for_other_background_tasks():
    session = GameSession({"script_id": "index-only-test", "settings": {}}, None)
    session.current_state = {}
    session.data_bank = None
    queries = []
    session.vector_memory = SimpleNamespace(query=lambda *args: queries.append(args) or [])
    index_started = asyncio.Event()
    background_started = asyncio.Event()
    release_index = asyncio.Event()
    release_background = asyncio.Event()

    async def index_turn():
        index_started.set()
        await release_index.wait()

    async def update_state():
        background_started.set()
        await release_background.wait()

    session._schedule_index_task(index_turn())
    background_task = session._schedule_background_task(update_state())
    try:
        await asyncio.wait_for(index_started.wait(), timeout=1)
        await asyncio.wait_for(background_started.wait(), timeout=1)
        lookup = asyncio.create_task(session._enrich_with_rag("action", [], [], ""))
        await asyncio.sleep(0)
        assert not lookup.done()
        assert not queries

        release_index.set()
        await asyncio.wait_for(lookup, timeout=1)
        assert len(queries) == 1
        assert not background_task.done()
    finally:
        release_index.set()
        release_background.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
async def test_rag_timing_separates_index_wait_vector_and_data_bank_queries():
    session = GameSession({"script_id": "retrieval-timing-test", "settings": {}}, None)
    session.current_state = {"player": {"location": "street"}, "npcs": {}}

    async def index_turn():
        await asyncio.sleep(0.02)

    def query(*args):
        time.sleep(0.02)
        return []

    session.vector_memory = SimpleNamespace(query=query)
    session.data_bank = SimpleNamespace(query=query)
    session._schedule_index_task(index_turn())
    context = {}
    try:
        await session._enrich_with_rag("查看线索", [], [], "", context)
    finally:
        await session._drain_background_tasks()

    assert context["rag_history_hits"] == context["databank_hits"] == []
    assert set(context["retrieval_timings_ms"]) == {"index_wait", "vector_query", "data_bank_query"}
    assert all(value >= 10 for value in context["retrieval_timings_ms"].values())


@pytest.mark.asyncio
async def test_unified_stream_reaches_intent_while_index_write_is_pending():
    session = GameSession(
        {"script_id": "index-progress-test", "settings": {}},
        SimpleNamespace(generate_with_tools=lambda *args: None),
    )
    session.current_state = {"_pipeline_mode": "agentic_unified"}
    release_index = asyncio.Event()

    async def index_turn():
        await release_index.wait()

    session._schedule_index_task(index_turn())
    stream = session.process_action_stream({"type": "freeform", "text": "Go"})
    try:
        assert await anext(stream) == {"type": "preflight", "stage": "background"}
        assert await asyncio.wait_for(anext(stream), timeout=1) == {"type": "preflight", "stage": "intent"}
        assert session._index_tasks
    finally:
        await stream.aclose()
        release_index.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_new_action_waits_for_previous_state_settlement(monkeypatch, streaming):
    session = GameSession(
        {"script_id": "background-settlement-test", "settings": {}},
        SimpleNamespace(generate_with_tools=lambda *args: None),
    )
    session.current_state = {"_pipeline_mode": "agentic_unified"}
    release_background = asyncio.Event()
    started = asyncio.Event()

    async def settle():
        started.set()
        await release_background.wait()
        session.current_state["settled"] = True

    background_task = session._schedule_background_task(settle())
    original_drain = session._drain_background_tasks

    async def drain_with_short_default(timeout=0.01, *, include_index=True, index_only=False):
        return await original_drain(timeout=timeout, include_index=include_index,
                                    index_only=index_only)

    async def query(*args, **kwargs):
        return "query", {"ok": True, "reply": "Checked"}

    monkeypatch.setattr(session, "_drain_background_tasks", drain_with_short_default)
    monkeypatch.setattr(session, "_dispatch_meta_action", query)

    async def run_action():
        action = {"type": "freeform", "text": "Check messages"}
        if streaming:
            return [event async for event in session.process_action_stream(action)]
        return await session.process_action(action)

    action_task = asyncio.create_task(run_action())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.03)
        assert not background_task.done()
        assert not action_task.done()
        release_background.set()
        result = await asyncio.wait_for(action_task, timeout=1)
        assert session.current_state["settled"] is True
        if streaming:
            assert next(event for event in result if event["type"] == "final")["state"]["settled"] is True
        else:
            assert result["ok"] is True
    finally:
        release_background.set()
        await asyncio.gather(action_task, background_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_query_and_branch_wait_for_pending_index_write(monkeypatch):
    session = GameSession({"script_id": "index-navigation-test", "settings": {}}, None)
    session.current_state = {}
    release_index = asyncio.Event()
    query_started = asyncio.Event()
    progress = []

    async def index_turn():
        await release_index.wait()

    async def agent_loop(*args, **kwargs):
        query_started.set()
        kwargs["result_holder"].text = "Checked"
        yield {"type": "agent_done"}

    monkeypatch.setattr(session, "_agent_loop", agent_loop)
    session._schedule_index_task(index_turn())
    query = asyncio.create_task(session._agent_query("current situation", on_event=progress.append))
    branch = asyncio.create_task(session.branch_to_node("missing"))
    try:
        await asyncio.sleep(0)
        assert progress == [{"type": "preflight", "stage": "query"}]
        assert not query_started.is_set()
        assert not query.done()
        assert not branch.done()
        release_index.set()
        assert (await asyncio.wait_for(query, timeout=1))["ok"] is True
        assert await asyncio.wait_for(branch, timeout=1) is None
        assert query_started.is_set()
    finally:
        release_index.set()
        await session._drain_background_tasks()


@pytest.mark.asyncio
async def test_route_runs_while_retrieval_waits_for_index_write(monkeypatch):
    session = GameSession({"script_id": "index-route-test", "settings": {}}, None)
    session.current_state = {}
    session.data_bank = None
    queried = []
    session.vector_memory = SimpleNamespace(query=lambda *args: queried.append(args) or [])
    release_index = asyncio.Event()
    route_started = asyncio.Event()

    async def index_turn():
        await release_index.wait()

    async def route_stage(*args):
        route_started.set()
        return {}

    monkeypatch.setattr(session, "_run_route_stage", route_stage)
    session._schedule_index_task(index_turn())
    context = {"recent_nodes": [], "activated_lore": [], "history_context": ""}
    route = asyncio.create_task(session._enrich_and_route({"text": "look"}, context))
    try:
        await asyncio.wait_for(route_started.wait(), timeout=1)
        assert not route.done()
        assert not queried
        release_index.set()
        assert await asyncio.wait_for(route, timeout=1) == {}
        assert len(queried) == 1
    finally:
        release_index.set()
        await session._drain_background_tasks()
