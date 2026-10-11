import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from test_agent_lore_context import large_session
from test_agent_state_compaction import compact


def search_session():
    content = "synthetic_archive: table of contents.\n" + "Synthetic unrelated record.\n" * 2000
    content += "synthetic_archive filing: SYNTHETIC_FOCUSED_RULE\n"
    session = GameSession({
        "script_id": "synthetic-search-bookmarks", "settings": {},
        "variables": [{"id": "test_counter", "type": "number", "default": 0}],
        "lorebook": [{"id": "test_manual", "keys": ["synthetic_archive"], "priority": 200,
                      "content": content, "visibility": "hidden"}]
                    + [{"id": f"test_other_{index}", "keys": ["synthetic_archive"], "content": "filing appendix"}
                       for index in range(7)],
    }, SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []})))
    session.current_state = {"game_time": "2030-01-01T09:00:00", "player": {}}
    session.script_variables.init_state(session.current_state)
    return session


def search_progress(summary):
    prefix = "[世界书搜索进度] "
    return [json.loads(line.removeprefix(prefix)) for line in summary.splitlines() if line.startswith(prefix)]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
async def test_compaction_keeps_focused_search_coordinates_and_view(candidate):
    session = search_session()
    context = {"old_time": session.current_state["game_time"]}
    if candidate:
        context["_accepted_state_updates"] = [{"variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}]}]
    original_state = copy.deepcopy(session.current_state)
    arguments = {"keyword": "synthetic_archive filing"}
    result = await session._query_lorebook_for_turn(context, arguments)
    catalog = json.loads(result)
    summary = await compact(session, [("query_lorebook", arguments, result)])
    for _ in range(2):
        progress, = search_progress(summary)
        assert progress["query"] == arguments and progress["snapshot"] == catalog["snapshot"]
        assert progress.get("projection") == catalog.get("projection")
        assert progress.get("approximate") == catalog.get("approximate")
        assert progress["content_omitted"] is True
        assert "完整阅读" in progress["notice"] and "定位" in progress["notice"]
        assert "SYNTHETIC_FOCUSED_RULE" not in json.dumps(progress)
        assert [entry["id"] for entry in progress["entries"]] == [entry["id"] for entry in catalog["entries"]]
        for saved, original in zip(progress["entries"], catalog["entries"]):
            assert saved == {key: original[key] for key in (
                "id", "snapshot", "offset", "next_offset", "total_length", "truncated", "visibility", "related_entries",
            )}
        bookmark = progress["entries"][0]
        assert bookmark["offset"] > 40000 and bookmark["snapshot"] != progress["snapshot"]
        reread = json.loads(await session._query_lorebook_for_turn(context, {
            "entry_id": bookmark["id"], "offset": bookmark["offset"], "snapshot": bookmark["snapshot"],
        }))
        assert "SYNTHETIC_FOCUSED_RULE" in reread["entries"][0]["content"]
        assert reread.get("projection") == catalog.get("projection")
        summary = await compact(session, [], summary)
    assert session.current_state == original_state


@pytest.mark.asyncio
async def test_repeated_compaction_preserves_each_search_page_bookmarks():
    session = search_session()
    arguments = {"keyword": "synthetic_archive filing"}
    first = json.loads(await session._run_tool_native_async("query_lorebook", arguments))
    continuation = {**arguments, "page": first["next_page"], "snapshot": first["snapshot"]}
    second = json.loads(await session._run_tool_native_async("query_lorebook", continuation))
    summary = await compact(session, [
        ("query_lorebook", arguments, json.dumps(first)),
        ("query_lorebook", continuation, json.dumps(second)),
    ])
    summary = await compact(session, [], summary)
    progress = search_progress(summary)
    assert len(progress) == 2
    for saved, original in zip(progress, [first, second]):
        assert [entry["id"] for entry in saved["entries"]] == [entry["id"] for entry in original["entries"]]
        assert saved["snapshot"] == original["snapshot"] and saved["next_page"] == original["next_page"]


@pytest.mark.asyncio
async def test_compaction_preserves_approximate_search_marker():
    session = large_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    arguments = {"keyword": "石门铜锁河边青灯"}
    result = await session._run_tool_native_async("query_lorebook", arguments)
    assert json.loads(result)["approximate"] is True
    summary = await compact(session, [("query_lorebook", arguments, result)])
    summary = await compact(session, [], summary)
    progress, = search_progress(summary)
    assert progress["approximate"] is True and progress["query"] == arguments
    assert all("content" not in entry for entry in progress["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "time", "candidate_update", "candidate_undo"])
async def test_compacted_bookmarks_do_not_bypass_snapshot_validation(change):
    session = search_session()
    context = {"old_time": session.current_state["game_time"]}
    if change.startswith("candidate"):
        context["_accepted_state_updates"] = [{"variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}]}]
    arguments = {"keyword": "synthetic_archive filing"}
    result = await session._query_lorebook_for_turn(context, arguments)
    summary = await compact(session, [("query_lorebook", arguments, result)])
    summary = await compact(session, [], summary)
    progress, = search_progress(summary)
    bookmark = progress["entries"][0]
    if change == "content":
        lorebook = session.prompt_builder.lorebook
        lorebook.update_entry(bookmark["id"], lorebook.entries[0].content + "\nSYNTHETIC_CHANGED_RULE")
    elif change == "time":
        session.current_state["game_time"] = "2030-01-01T09:05:00"
    elif change == "candidate_update":
        context["_accepted_state_updates"].append({"variable_changes": [{"id": "test_counter", "op": "inc", "value": 2}]})
    else:
        context["_accepted_state_updates"] = []
    for stale_arguments in (
        {"entry_id": bookmark["id"], "offset": bookmark["offset"], "snapshot": bookmark["snapshot"]},
        {**progress["query"], "page": progress["next_page"], "snapshot": progress["snapshot"]},
    ):
        stale = json.loads(await session._query_lorebook_for_turn(context, stale_arguments))
        assert stale["error_code"] == "snapshot_expired" and stale["restart_required"] is True
        assert "entries" not in stale and "snapshot" not in stale["restart"]
    fresh = json.loads(await session._query_lorebook_for_turn(context, {**progress["query"], "page": 0}))
    assert fresh["snapshot"] != progress["snapshot"]
    entry = fresh["entries"][0]
    reread = json.loads(await session._query_lorebook_for_turn(context, {
        "entry_id": entry["id"], "offset": entry["offset"], "snapshot": entry["snapshot"],
    }))
    assert "SYNTHETIC_FOCUSED_RULE" in reread["entries"][0]["content"]
    assert session.current_state["script_variables"]["test_counter"] == 0
