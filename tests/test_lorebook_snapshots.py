import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.lorebook import Lorebook
from engine.script_loader import ScriptLoader
from engine.session.agentic_mixin import _bounded_tool_content
from test_agent_lore_context import large_session
from test_agent_lore_reveals import make_reveal_scenario
from test_agent_state_compaction import compact, lore_query_statuses


def snapshot_lorebook():
    return Lorebook([
        {"id": f"test_archive_{index}", "keys": ["synthetic_archive"],
         "content": "SYNTHETIC_PAGED_ARCHIVE " * 300, "related_entries": ["test_archive_6"]}
        for index in range(7)
    ])


def test_search_batch_and_exact_reads_have_distinct_reusable_snapshots():
    lorebook = snapshot_lorebook()
    initial = lorebook.query(keyword="synthetic_archive")
    second = lorebook.query(keyword="synthetic_archive", page=initial["next_page"], snapshot=initial["snapshot"])
    assert len(initial["entries"]) == 5 and len(second["entries"]) == 2
    assert second["snapshot"] == initial["snapshot"]
    assert second["next_page"] is None
    entry = initial["entries"][0]
    batch = lorebook.query(entry_ids=[entry["id"], "test_archive_1"])
    assert batch["entries"][0]["snapshot"] == entry["snapshot"]
    for wrong_snapshot in (initial["snapshot"], batch["snapshot"], second["entries"][0]["snapshot"]):
        assert lorebook.query(entry_id=entry["id"], snapshot=wrong_snapshot)["error_code"] == "snapshot_expired"
    read = lorebook.query(entry_id=entry["id"], snapshot=entry["snapshot"])
    content = []
    while True:
        page, = read["entries"]
        content.append(page["content"])
        assert page["snapshot"] == entry["snapshot"]
        if page["next_offset"] is None:
            break
        read = lorebook.query(entry_id=page["id"], offset=page["next_offset"], snapshot=page["snapshot"])
    assert "".join(content) == lorebook.entries[0].content


@pytest.mark.parametrize("arguments", [
    {"keyword": "synthetic_archive", "page": 1},
    {"entry_id": "test_archive_0", "offset": 2000},
])
def test_continuations_require_snapshot_and_restart_from_beginning(arguments):
    lorebook = snapshot_lorebook()
    result = lorebook.query(**arguments)
    assert result["error_code"] == "snapshot_required"
    assert result["restart_required"] is True
    assert "snapshot" not in result["restart"]
    restarted = lorebook.query(**result["restart"])
    assert restarted["entries"][0]["offset"] == 0
    assert "error" not in restarted


@pytest.mark.parametrize("snapshot", ["", 1, True, [], {}, "invalid", "!" * 22])
def test_invalid_snapshot_is_rejected(snapshot):
    assert "error" in snapshot_lorebook().query(entry_id="test_archive_0", snapshot=snapshot)


@pytest.mark.parametrize("change", ["content", "keys", "title", "visibility", "removed", "disabled", "excluded", "related"])
def test_entry_snapshot_rejects_changed_content_and_availability(change):
    lorebook = snapshot_lorebook()
    initial = lorebook.query(entry_id="test_archive_0")
    arguments = {"entry_id": "test_archive_0", "offset": 2000, "snapshot": initial["snapshot"]}
    if change == "content":
        lorebook.update_entry("test_archive_0", "SYNTHETIC_NEW_ARCHIVE " * 400)
    elif change == "keys":
        lorebook.update_entry("test_archive_0", lorebook.entries[0].content, keys=["new_synthetic_key"])
    elif change == "title":
        lorebook.entries[0].comment = "Synthetic revised title"
    elif change == "visibility":
        lorebook.entries[0].visibility = "hidden"
    elif change == "removed":
        lorebook.remove_entry("test_archive_0")
    elif change == "disabled":
        lorebook.update_entry_enabled("test_archive_0", False)
    elif change == "excluded":
        arguments["excluded_ids"] = {"test_archive_0"}
    else:
        arguments["excluded_ids"] = {"test_archive_6"}
    result = lorebook.query(**arguments)
    assert result["error_code"] == "snapshot_expired"
    assert "entries" not in result
    assert result["restart"] == {"entry_id": "test_archive_0"}


def test_unrelated_content_change_does_not_invalidate_exact_read():
    lorebook = snapshot_lorebook()
    initial = lorebook.query(entry_id="test_archive_0")
    lorebook.update_entry("test_archive_1", "Synthetic unrelated update.")
    read = lorebook.query(entry_id="test_archive_0", offset=2000, snapshot=initial["snapshot"])
    assert read["snapshot"] == initial["snapshot"]
    assert read["entries"][0]["offset"] == 2000


@pytest.mark.parametrize("change", ["query", "addition", "removal", "exclusion", "scope"])
def test_search_snapshot_binds_query_result_and_view(change):
    lorebook = snapshot_lorebook()
    initial = lorebook.query(keyword="synthetic_archive")
    arguments = {"keyword": "synthetic_archive", "page": 1, "snapshot": initial["snapshot"]}
    if change == "query":
        arguments["keyword"] = "test_archive_0"
    elif change == "addition":
        lorebook.add_entries([{"id": "test_archive_new", "keys": ["synthetic_archive"], "content": "Synthetic new page."}])
    elif change == "removal":
        lorebook.remove_entry("test_archive_1")
    elif change == "exclusion":
        arguments["excluded_ids"] = {"test_archive_1"}
    else:
        arguments["snapshot_scope"] = ("synthetic-other-view",)
    assert lorebook.query(**arguments)["error_code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_compaction_retains_search_progress_and_snapshot_failure_not_missing_entry():
    session = large_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    lorebook = snapshot_lorebook()
    search = {"keyword": "synthetic_archive"}
    catalog = lorebook.query(**search)
    exact = {"entry_id": catalog["entries"][0]["id"]}
    initial = lorebook.query(**exact)
    lorebook.update_entry(exact["entry_id"], "SYNTHETIC_NEW_ARCHIVE " * 300)
    continuation = {**exact, "offset": 2000, "snapshot": initial["snapshot"]}
    failure = lorebook.query(**continuation)
    summary = await compact(session, [
        ("query_lorebook", search, json.dumps(catalog)),
        ("query_lorebook", exact, json.dumps(initial)),
        ("query_lorebook", continuation, json.dumps(failure)),
    ])
    summary = await compact(session, [], summary)
    prefix = "[世界书搜索进度] "
    progress, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert progress["query"] == search
    assert progress["snapshot"] == catalog["snapshot"]
    assert progress["next_page"] == catalog["next_page"]
    prefix = "[世界书快照失效] "
    failed, = [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]
    assert failed == failure
    status, = lore_query_statuses(summary)
    assert status["returned_ids"] == [exact["entry_id"]] and status["not_returned_ids"] == []
    assert status["snapshot"] == initial["snapshot"]
    assert "丢弃旧正文片段" in summary


@pytest.mark.parametrize("large_query", [False, True])
def test_snapshot_failure_stays_structured_when_bounded(large_query):
    keyword = "synthetic_archive" * (1000 if large_query else 1)
    failure = snapshot_lorebook().query(keyword=keyword, page=1)
    bounded = json.loads(_bounded_tool_content(json.dumps(failure, ensure_ascii=False), "query_lorebook", 1000))
    assert bounded["error_code"] == "snapshot_required"
    assert bounded["restart_required"] is True
    if large_query:
        assert bounded["metadata_omitted"] is True
        assert "restart" not in bounded
    else:
        assert bounded["restart"] == failure["restart"]
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 1000


@pytest.mark.asyncio
async def test_rewritten_lore_cannot_mix_old_and_new_pages():
    session = large_session()
    context = {"old_time": session.current_state["game_time"]}
    initial = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_lore_0"}))
    entry, = initial["entries"]
    session.prompt_builder.lorebook.update_entry(entry["id"], "SYNTHETIC_REPLACED_BODY " * 300)
    result = json.loads(await session._query_lorebook_for_turn(context, {
        "entry_id": entry["id"], "offset": entry["next_offset"], "snapshot": entry.get("snapshot"),
    }))
    assert result.get("restart_required") is True
    assert result["error_code"] == "snapshot_expired"
    assert "SYNTHETIC_REPLACED_BODY" not in json.dumps(result)
    restarted = json.loads(await session._query_lorebook_for_turn(context, result["restart"]))
    assert restarted["entries"][0]["offset"] == 0
    assert restarted["entries"][0]["snapshot"] != entry["snapshot"]


@pytest.mark.asyncio
async def test_event_projection_cannot_continue_committed_lore_snapshot():
    script, _, _ = make_reveal_scenario("update_lore")
    script["lorebook"][0]["content"] = "SYNTHETIC_PRIOR_RULE " * 300
    script["lorebook"][0].pop("available_from", None)
    script["one_time_events"][0]["effects"][0]["content"] = "SYNTHETIC_CANDIDATE_RULE " * 300
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    state = copy.deepcopy(session.current_state)
    entries = copy.deepcopy(session.prompt_builder.lorebook.entries)
    context = {"old_time": state["game_time"]}
    initial = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_catalog"}))
    entry, = initial["entries"]
    context["_accepted_state_updates"] = [{"end_time": "2030-01-01T09:05:00"}]
    result = json.loads(await session._query_lorebook_for_turn(context, {
        "entry_id": entry["id"], "offset": entry["next_offset"], "snapshot": entry.get("snapshot"),
    }))
    assert result.get("restart_required") is True
    assert result["error_code"] == "snapshot_expired"
    restarted = json.loads(await session._query_lorebook_for_turn(context, result["restart"]))
    assert restarted["projection"]["provisional"] is True
    assert restarted["entries"][0]["content"].startswith("SYNTHETIC_CANDIDATE_RULE")
    assert session.current_state == state
    assert session.prompt_builder.lorebook.entries == entries


@pytest.mark.asyncio
async def test_candidate_updates_and_withdrawal_keep_view_snapshots_separate():
    session = large_session()
    arguments = {"entry_id": "test_lore_0"}
    context = {"old_time": session.current_state["game_time"]}
    committed = json.loads(await session._query_lorebook_for_turn(context, arguments))
    context["_accepted_state_updates"] = [{"variable_changes": [{"id": "test_counter", "op": "set", "value": 1}]}]
    candidate = json.loads(await session._query_lorebook_for_turn(context, arguments))
    assert candidate["entries"][0]["content"] == committed["entries"][0]["content"]
    assert candidate["snapshot"] != committed["snapshot"]
    context["_accepted_state_updates"].append({"variable_changes": [{"id": "test_counter", "op": "set", "value": 2}]})
    failed = json.loads(await session._query_lorebook_for_turn(context, {
        **arguments, "offset": 2000, "snapshot": candidate["snapshot"],
    }))
    assert failed["error_code"] == "snapshot_expired"
    context["_accepted_state_updates"] = []
    failed = json.loads(await session._query_lorebook_for_turn(context, {
        **arguments, "snapshot": candidate["snapshot"],
    }))
    assert failed["error_code"] == "snapshot_expired"
    resumed = json.loads(await session._query_lorebook_for_turn(context, {
        **arguments, "offset": 2000, "snapshot": committed["snapshot"],
    }))
    assert resumed["snapshot"] == committed["snapshot"]
    assert resumed["entries"][0]["offset"] == 2000
    assert session.current_state["script_variables"]["test_counter"] == 0


@pytest.mark.asyncio
async def test_player_query_dispatch_rejects_internal_snapshots_and_allows_own_continuation(monkeypatch):
    session = large_session(visibility="public")
    arguments = {"entry_id": "test_lore_0"}
    committed = json.loads(await session._run_tool_native_async("query_lorebook", arguments))
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [{
        "variable_changes": [{"id": "test_counter", "op": "set", "value": 1}],
    }]}
    candidate = json.loads(await session._query_lorebook_for_turn(context, arguments))

    async def agent_loop(messages, system, tools, dispatch, **kwargs):
        for internal in (committed, candidate):
            failed = json.loads(await dispatch("query_lorebook", {**arguments, "snapshot": internal["snapshot"]}))
            assert failed["error_code"] == "snapshot_expired" and failed["restart_required"] is True
        initial = json.loads(await dispatch("query_lorebook", failed["restart"]))
        assert initial["entries"][0]["content"] == committed["entries"][0]["content"]
        continued = json.loads(await dispatch("query_lorebook", {
            **arguments, "offset": initial["entries"][0]["next_offset"], "snapshot": initial["snapshot"],
        }))
        assert continued["snapshot"] == initial["snapshot"]
        assert continued["entries"][0]["offset"] == initial["entries"][0]["next_offset"]
        failed = json.loads(await session._run_tool_native_async("query_lorebook", {
            **arguments, "snapshot": initial["snapshot"],
        }))
        assert failed["error_code"] == "snapshot_expired"
        kwargs["result_holder"].text = "Checked the synthetic public archive."
        yield {"type": "agent_done"}

    monkeypatch.setattr(session, "_agent_loop", agent_loop)
    result = await session._agent_query("Read the public archive.")
    assert result["ok"] is True
