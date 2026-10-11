import copy
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update_state", "write_narrative"])
@pytest.mark.parametrize("read_name,read_args", [
    ("recall_history", {"query": "synthetic"}),
    ("query_lorebook", {"entry_id": "test_label"}),
    ("query_lorebook", {"entry_id": "test_label", "offset": -1}),
    ("query_npc_history", {"npc_id": "test_librarian"}),
    ("check_inventory", {}),
    ("get_npc_attitude", {"npc_id": "test_librarian"}),
    ("peek_upcoming_events", {}),
    ("traverse_graph", {"entity": "test_librarian"}),
    ("read_director_notes", {}),
    ("consult_rules", {"category": "variables", "query": "test_counter"}),
    ("consult_rules", {"question": "Librarian 在场吗？", "include_candidates": True}),
    ("invoke_character", {"npc_id": "test_librarian", "situation": "Consider the shelf."}),
    ("invoke_character", {"npc_id": "test_librarian", "situation": "Consider the shelf.", "include_candidates": True}),
    ("trace_causality", {"entity": "test_librarian"}),
    ("preview_choice_outcome", {"choice_text": "Inspect the room."}),
])
async def test_read_only_tools_preserve_single_undo_target(operation, read_name, read_args, monkeypatch):
    previous_narrative = "You wait quietly in the room."
    arguments = ({"variable_changes": [{"id": "test_counter", "op": "inc", "value": 1}]}
                 if operation == "update_state" else {"text": "You inspect the empty shelf."})
    operations = [
        ("submit_plan", {"plan": "Inspect the synthetic room."}),
        (operation, arguments),
        (read_name, read_args),
        (read_name, read_args),
        ("undo_my_last_action", {"reason": "Correct the candidate after consulting evidence."}),
        (read_name, read_args),
        ("undo_my_last_action", {"reason": "There is no earlier rollback point."}),
    ]
    requests = []

    async def respond(messages, **kwargs):
        index = len(requests)
        requests.append(copy.deepcopy(messages))
        name, args = operations[index]
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-undo-{index}", "name": name, "arguments": copy.deepcopy(args),
        }]}

    session = GameSession({
        "script_id": "synthetic-undo-retrieval", "settings": {},
        "locations": [{"id": "test_room", "name": "Room"}],
        "npcs": [{"id": "test_librarian", "name": "Librarian", "default_location": "test_room"}],
        "variables": [{"id": "test_counter", "type": "number", "default": 2}],
        "lorebook": [{"id": "test_label", "keys": ["label"], "content": "A plain shelf label."}],
    }, SimpleNamespace(generate_with_tools=AsyncMock(side_effect=respond), generate=AsyncMock(return_value="Synthetic advice.")))
    session.current_state = {
        "game_time": "2030-01-01T09:00:00", "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"],
    }
    session.script_variables.init_state(session.current_state)
    original_state = copy.deepcopy(session.current_state)
    context = {
        "old_time": original_state["game_time"], "action_text": "Inspect the shelf.",
        "_finalize_narrative": previous_narrative,
    }
    monkeypatch.setattr(session, "_review_narrative", AsyncMock(return_value={"pass": True}))
    completed = 0
    async with aclosing(session._execute_unified_agent(
        context, {}, {"text": "Inspect the shelf."}, streaming=True,
    )) as pipeline:
        async for event in pipeline:
            if event.get("type") == "tool_call":
                completed += 1
                if completed == len(operations) - 1:
                    break
    assert completed == len(operations) - 1
    undo_results = [message["content"] for message in requests[5]
                    if message.get("tool_call_id") == "synthetic-undo-4"]
    assert undo_results and undo_results[0].startswith(f"已撤销上一个工具调用（{operation}）")
    assert "重新查询" in undo_results[0]
    assert context.get("_accepted_state_updates", []) == []
    assert context["_finalize_narrative"] == previous_narrative
    assert context["_undone_record_indexes"] == {1}
    assert session.current_state == original_state
    assert session._last_tool_result is None
    assert session._last_state_snapshot is None
    assert event["tool_result"] == "没有可撤销的操作"
