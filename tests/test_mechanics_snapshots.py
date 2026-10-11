import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.session.agentic_mixin import _bounded_tool_content
from test_agent_state_compaction import compact
from test_world_property_declarations import make_session


def query_rules(session, context=None, **arguments):
    return json.loads(session._handle_consult_rules({"category": "world_properties", **arguments}, context))


def continuation(entry, **arguments):
    return {
        "rule_index": entry["rule_index"], "offset": entry["next_offset"],
        "snapshot": entry["snapshot"], **arguments,
    }


@pytest.mark.parametrize("change", ["committed", "candidate", "definition", "order", "view"])
def test_old_content_page_cannot_continue_after_its_view_changes(change):
    session, _ = make_session("test_access")
    session.current_state["world_properties"]["test_access"] = "SYNTHETIC_BEFORE_" * 500
    context = {"old_time": session.current_state["game_time"]}
    projected = change in {"candidate", "view"}
    first = query_rules(session, context, include_candidates=projected)
    entry = first["entries"][0]
    assert entry["next_offset"] == 2000
    if change == "committed":
        session.current_state["world_properties"]["test_access"] = "SYNTHETIC_AFTER_" * 500
    elif change == "candidate":
        result = session._handle_update_state({
            "world_property_changes": [{"id": "test_access", "value": "SYNTHETIC_AFTER_" * 500}],
        }, context)
        assert not result.startswith("错误:")
    elif change == "definition":
        session.script["world_properties"][0]["rule_notes"] = "Changed synthetic definition."
    elif change == "order":
        session.script["world_properties"].insert(0, {"id": "test_other", "value": "SYNTHETIC_OTHER_" * 500})
    else:
        projected = False
    result = query_rules(session, context, **continuation(entry, include_candidates=projected))
    assert "error" in result, "An old prefix can currently be joined to a different state or definition"
    assert result["error_code"] == "snapshot_expired"
    assert result["restart_required"] is True
    assert "entries" not in result


@pytest.mark.asyncio
async def test_undo_invalidates_candidate_content_continuation():
    session, _ = make_session("test_access")
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    session._last_state_snapshot = {
        "current_state": copy.deepcopy(original), "npc_reactions": [], "new_npcs": [], "choices": [],
        "graph": copy.deepcopy(session.narrative_graph.snapshot()),
        "accepted_state_updates": None, "conditioned_event_keys": None,
    }
    arguments = {"world_property_changes": [{"id": "test_access", "value": "SYNTHETIC_CANDIDATE_" * 500}]}
    session._handle_update_state(arguments, context)
    entry = query_rules(session, context, include_candidates=True)["entries"][0]
    session._last_tool_result = {"name": "update_state", "args": arguments}
    await session._dispatch_unified_tool(context, "undo_my_last_action", {"reason": "Discard synthetic update."})
    result = query_rules(session, context, **continuation(entry, include_candidates=True))
    assert result.get("error_code") == "snapshot_expired"
    assert result["restart_required"] is True
    assert "entries" not in result
    assert session.current_state == original


@pytest.mark.parametrize("arguments", [{"offset": 1}, {"rule_index": 0}, {"rule_index": 0, "offset": 2000}])
def test_snapshot_cannot_be_omitted_for_list_or_content_continuations(arguments):
    session, _ = make_session("test_access")
    result = query_rules(session, **arguments)
    assert result["error_code"] == "snapshot_required"
    assert result["restart_required"] is True
    assert "entries" not in result and "snapshot" not in result
    assert result["restart"] == {"category": "world_properties", "query": "", "include_candidates": False, "offset": 0}


@pytest.mark.parametrize("snapshot", [None, True, 0, {}, [], "", "a" * 31, "z" * 32, "A" * 32])
def test_malformed_snapshot_is_rejected_even_on_first_page(snapshot):
    session, _ = make_session("test_access")
    assert "error" in query_rules(session, snapshot=snapshot)


@pytest.mark.parametrize("scope", ["list_to_content", "content_to_list", "other_entry", "other_category", "other_session"])
def test_snapshot_is_bound_to_its_query_scope(scope):
    session, _ = make_session("test_access")
    session.script["world_properties"].append({"id": "test_other", "value": "closed"})
    first = query_rules(session)
    arguments = {"rule_index": 0, "snapshot": first["entries"][0]["snapshot"]}
    if scope == "list_to_content":
        arguments["snapshot"] = first["snapshot"]
    elif scope == "content_to_list":
        arguments.pop("rule_index")
        arguments["offset"] = 1
    elif scope == "other_entry":
        arguments["rule_index"] = 1
    elif scope == "other_category":
        session.script["persistent_states"] = copy.deepcopy(session.script["world_properties"])
        arguments["category"] = "persistent_states"
    else:
        other, _ = make_session("test_access")
        other.script["world_properties"] = copy.deepcopy(session.script["world_properties"])
        session = other
    assert query_rules(session, **arguments)["error_code"] == "snapshot_expired"


@pytest.mark.parametrize("change", ["query", "order", "membership", "state"])
def test_list_pagination_rejects_changes_to_filter_order_membership_or_values(change):
    session, _ = make_session("test_access")
    session.script["world_properties"] = [
        {"id": f"test_access_{index}", "value": "closed", "rule_notes": "Synthetic quota."}
        for index in range(12)
    ]
    first = query_rules(session, query="quota")
    arguments = {"query": "quota", "offset": first["next_offset"], "snapshot": first["snapshot"]}
    assert arguments["offset"] == 5
    if change == "query":
        arguments["query"] = "access_1"
    elif change == "order":
        session.script["world_properties"].reverse()
    elif change == "membership":
        session.script["world_properties"][0]["rule_notes"] = "Not a match."
    else:
        session.current_state["world_properties"]["test_access_11"] = "changed"
    result = query_rules(session, **arguments)
    assert result["error_code"] == "snapshot_expired"
    fresh = json.loads(session._handle_consult_rules(result["restart"]))
    assert fresh["offset"] == 0 and len(fresh["entries"]) <= 5


def test_unchanged_snapshot_supports_full_read_and_safe_restart_after_update():
    session, _ = make_session("test_access")
    session.current_state["world_properties"]["test_access"] = "SYNTHETIC_OLD_" * 500
    original = copy.deepcopy(session.current_state)
    first = query_rules(session)
    assert query_rules(session) == first
    entry = first["entries"][0]
    assert first["snapshot"] != entry["snapshot"]
    next_page = query_rules(session, **continuation(entry))
    assert next_page["snapshot"] == next_page["entries"][0]["snapshot"] == entry["snapshot"]
    assert session.current_state == original
    value = 'SYNTHETIC_NEW_中文_"\\' * 500
    session.current_state["world_properties"]["test_access"] = value
    expired = query_rules(session, **continuation(entry))
    fresh = json.loads(session._handle_consult_rules(expired["restart"]))
    entry = fresh["entries"][0]
    chunks = [entry["content"]]
    while entry["next_offset"] is not None:
        result = query_rules(session, **continuation(entry))
        entry, = result["entries"]
        assert entry["offset"] == sum(map(len, chunks))
        chunks.append(entry["content"])
    assert json.loads("".join(chunks))["current_value"] == value
    assert "SYNTHETIC_OLD_" not in "".join(chunks)


def test_removed_index_reports_expired_snapshot_instead_of_reusing_coordinates():
    session, _ = make_session("test_access")
    entry = query_rules(session)["entries"][0]
    session.script["world_properties"].clear()
    result = query_rules(session, rule_index=0, snapshot=entry["snapshot"])
    assert result["error_code"] == "snapshot_expired"
    assert query_rules(session)["entries"] == []


@pytest.mark.asyncio
async def test_actual_branch_switch_invalidates_snapshot_even_when_values_match():
    session, _ = make_session("test_access")
    original = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, original["game_time"], 0, state_snapshot=original)
    session.world_tree.add_node(root_id, original["game_time"], 1, state_snapshot=original)
    entry = query_rules(session)["entries"][0]
    restored = await session.branch_to_node(root_id)
    assert restored["node_id"] == root_id
    assert query_rules(session)["entries"][0]["content"] == entry["content"]
    result = query_rules(session, rule_index=0, snapshot=entry["snapshot"])
    assert result["error_code"] == "snapshot_expired"


def test_candidate_updates_do_not_expire_committed_only_reads():
    session, _ = make_session("test_access")
    context = {"old_time": session.current_state["game_time"]}
    first = query_rules(session)
    candidate = query_rules(session, context, include_candidates=True)
    session._handle_update_state({"world_property_changes": [{"id": "test_access", "value": "open"}]}, context)
    assert query_rules(session) == first
    assert query_rules(session, context, include_candidates=True)["snapshot"] != candidate["snapshot"]


@pytest.mark.asyncio
async def test_background_narrative_rollback_invalidates_a_candidate_cursor():
    session, _ = make_session("test_access")
    original = copy.deepcopy(session.current_state)
    context = {"old_time": original["game_time"]}
    calls = []
    captured_entry = None

    async def generate(messages, **kwargs):
        nonlocal captured_entry
        index = len(calls)
        calls.append(copy.deepcopy(messages))
        if index == 0:
            name, arguments = "update_state", {"world_property_changes": [{
                "id": "test_access", "value": "SYNTHETIC_BACKGROUND_VALUE_" * 200,
            }]}
        elif index == 1:
            name, arguments = "consult_rules", {"category": "world_properties", "include_candidates": True}
        elif index == 2:
            captured_entry, = json.loads(messages[-1]["content"])["entries"]
            name, arguments = "write_narrative", {"text": "You stay in the room without changing its access state."}
        elif index == 3:
            name, arguments = "consult_rules", {"category": "world_properties", **continuation(captured_entry, include_candidates=True)}
        elif index == 4:
            result = json.loads(messages[-1]["content"])
            assert result["error_code"] == "snapshot_expired"
            name, arguments = "consult_rules", result["restart"]
        elif index == 5:
            result = json.loads(messages[-1]["content"])
            assert result["projection"]["accepted_updates"] == 0
            assert json.loads(result["entries"][0]["content"])["candidate_value"] == "closed"
            name, arguments = "update_state", {"end_time": "2030-01-01T09:01:00"}
        else:
            return {"content": "Done.", "tool_calls": []}
        schema = next(tool["function"]["parameters"] for tool in kwargs["tools"] if tool["function"]["name"] == name)
        assert arguments.keys() <= schema["properties"].keys()
        return {"content": "", "tool_calls": [{"id": f"test_background_cursor_{index}", "name": name, "arguments": arguments}]}

    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(side_effect=generate))
    events = [event async for event in session._run_background_agent(context, "You inspect the room.", "", {"text": "Stay here."})]
    holder = next(event["holder"] for event in events if event["type"] == "_bg_result")
    assert holder.records[0]["superseded"] is True
    assert len(calls) == 7
    assert session.current_state == original


@pytest.mark.asyncio
async def test_bounded_and_repeatedly_compacted_candidates_keep_the_same_snapshot():
    session, _ = make_session("test_access")
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    context = {"old_time": session.current_state["game_time"]}
    session._handle_update_state({"world_property_changes": [{"id": "test_access", "value": "SYNTHETIC_LONG_" * 400}]}, context)
    arguments = {"category": "world_properties", "include_candidates": True}
    original = session._handle_consult_rules(arguments, context)
    bounded = _bounded_tool_content(original, "consult_rules", 1200)
    preview = json.loads(bounded)
    result = json.loads(original)
    assert preview["snapshot"] == result["snapshot"]
    assert preview["entries"][0]["snapshot"] == result["entries"][0]["snapshot"]
    summary = None
    for _ in range(2):
        summary = await compact(session, [("consult_rules", arguments, bounded)], summary)
        assert preview["snapshot"] in summary and preview["entries"][0]["snapshot"] in summary
        assert "offset=0" in summary
    fresh = query_rules(session, context, include_candidates=True, rule_index=0, snapshot=preview["entries"][0]["snapshot"])
    assert fresh["entries"][0]["content"] == result["entries"][0]["content"]
    omitted = json.loads(_bounded_tool_content(original, "consult_rules", 200))
    assert omitted["truncated"] is True and omitted["metadata_omitted"] is True
    assert "entries" not in omitted and "snapshot" not in omitted
    assert "offset=0" in omitted["notice"]
