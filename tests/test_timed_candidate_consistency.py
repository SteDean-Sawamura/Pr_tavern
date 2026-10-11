import copy
import json
from collections import OrderedDict
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events
from test_candidate_contacts import CONTACT, consult_contacts


START = "2030-01-01T09:00:00"
FIRE = "2030-01-01T09:05:00"
END = "2030-01-01T09:10:00"
NOTICE = "SYNTHETIC_CHAIN_NOTICE"


def make_session(source="effect", release_time=FIRE):
    condition = "lorebook_enabled_overrides.test_gate == true"
    effect = {"type": "activate_lore", "id": "test_gate"}
    if source == "trigger":
        effect = {"type": "add_trigger", "trigger": {
            "id": "test_enable", "event": "test_configure", "action": "activate_lore",
            "params": {"entry_id": "test_gate"},
        }}
    elif source == "addition":
        effect = {"type": "add_lore", "entry": {"id": "test_generated", "content": "SYNTHETIC_GATE"}}
        condition = 'lorebook_entry_overrides.test_generated.content == "SYNTHETIC_GATE"'
    script = {
        "script_id": "synthetic-timed-candidate", "name": "Synthetic timed candidate", "settings": {},
        "start_time": START,
        "player_character": {"name": "Visitor", "initial_location": "test_origin"},
        "locations": [{"id": "test_origin", "name": "Origin"}, {"id": "test_desk", "name": "Practice desk"}],
        "npcs": [{"id": "test_guide", "name": "Practice guide", "default_location": "test_origin",
                  "schedule": [{"time_range": "09:00-10:00", "location": "test_origin"},
                               {"time_range": "09:00-10:00", "location": "test_desk", "priority": 100,
                                "condition": "event_fired.test_release == true"}]}],
        "lorebook": [{"id": "test_gate", "enabled": False, "content": "SYNTHETIC_GATE"},
                     {"id": "test_notice", "enabled": False, "content": NOTICE}],
        "one_time_events": [
            {"id": "test_configure", "trigger_time": FIRE, "effects": [effect]},
            {"id": "test_release", "trigger_time": release_time, "condition": condition,
             "effects": [{"type": "activate_lore", "id": "test_notice"}]},
        ],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [
            {"id": "test_waiting", "type": "choice", "activate_events": ["event.test_release"],
             "choices": [{"id": "test_continue", "label": "Continue"}]},
        ]}]},
    }
    session = GameSession(script, None, save_id="synthetic-timed-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    session.story_tree_engine.evaluate(session.current_state, 0, session._evaluate_condition)
    session.event_engine.sync_story_tree_state(session.current_state)
    session._ensure_identity_lore()
    return session


def lore_entries(session):
    return {entry.id: {key: value for key, value in asdict(entry).items() if key != "last_activated_turn"}
            for entry in session.prompt_builder.lorebook.entries}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["effect", "trigger", "addition"])
@pytest.mark.parametrize("release_time", [FIRE, "2030-01-01T09:06:00"])
@pytest.mark.parametrize("end_time", ["2030-01-01T09:04:00", END])
async def test_candidates_share_canonical_timed_effects_without_mutation(source, release_time, end_time):
    session = make_session(source, release_time)
    session.vector_memory = Mock()
    baseline = copy.deepcopy((session.current_state, session.script, lore_entries(session),
                              session._mechanics_definitions(), session.world_clock.snapshot()))
    original_state = session.current_state
    context = {"old_time": START, "new_time": END,
               "_accepted_state_updates": [copy.deepcopy(CONTACT), {"end_time": end_time}]}
    expected = ["test_configure", "test_release"] if end_time == END else []
    story, metadata = session._story_query_candidate(context)
    assert metadata["event_ids"] == expected
    events, state = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == expected
    assert state == story.current_state
    projected_lore = session._project_lorebook_for_turn(state, events)
    assert {entry.id: {key: value for key, value in asdict(entry).items() if key != "last_activated_turn"}
            for entry in projected_lore.entries} == lore_entries(story)
    query = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_notice"}))
    assert [entry["content"] for entry in query["entries"]] == ([NOTICE] if expected else [])
    assert query["projection"]["event_ids"] == expected
    contacts = consult_contacts(session, context, include_candidates=True)
    assert contacts["contacts"]["test_guide"]["in_contact"] is (not bool(expected))
    assert session._projected_events(context, with_state=True) == (events, state)
    assert session.current_state is original_state
    assert (session.current_state, session.script, lore_entries(session),
            session._mechanics_definitions(), session.world_clock.snapshot()) == baseline
    assert session.vector_memory.mock_calls == []
    session.vector_memory = None
    session.current_state = session._project_state_tool_updates(context["_accepted_state_updates"])
    fired = []
    session._settle_timed_events(START, end_time, fired)
    session.current_state["game_time"] = end_time
    assert [event["event_id"] for event in fired] == expected
    assert session.current_state == state
    assert lore_entries(session) == lore_entries(story)


@pytest.mark.asyncio
@pytest.mark.parametrize("cyclic", [False, True])
@pytest.mark.parametrize("effects,expected", [
    ([{"type": "add_lore", "entry": {"id": "test_gate", "content": "SYNTHETIC_DUPLICATE", "enabled": True}}], []),
    ([{"type": "update_lore", "id": "test_gate", "content": "SYNTHETIC_UPDATED", "keys": ["updated"]},
      {"type": "activate_lore", "id": "test_gate"}], ["SYNTHETIC_UPDATED"]),
    ([{"type": "remove_lore", "id": "test_gate"}], []),
    ([{"type": "remove_lore", "id": "test_gate"},
      {"type": "add_lore", "entry": {"id": "test_gate", "content": "SYNTHETIC_REPLACED"}}], ["SYNTHETIC_REPLACED"]),
])
async def test_candidate_lore_preserves_canonical_duplicates_updates_removals_and_repeats(cyclic, effects, expected):
    script = copy.deepcopy(make_session().script)
    event = {"id": "test_configure", "effects": effects}
    script["one_time_events"] = []
    if cyclic:
        event.update(first_trigger=FIRE, frequency_value=5, frequency_unit="minute")
        script["cyclic_events"] = [event]
    else:
        event["trigger_time"] = FIRE
        script["one_time_events"] = [event]
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    baseline = copy.deepcopy((session.current_state, session.script, lore_entries(session)))
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
    query = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_gate"}))
    assert [entry["content"] for entry in query["entries"]] == expected
    assert query["projection"]["event_ids"] == ["test_configure"] * (2 if cyclic else 1)
    assert json.loads(await session._query_lorebook_for_turn(context, {"entry_id": "test_gate"})) == query
    assert (session.current_state, session.script, lore_entries(session)) == baseline
    events, state = session._projected_events(context, with_state=True)
    fired = []
    session.current_state = session._project_state_tool_updates(context["_accepted_state_updates"])
    session._settle_timed_events(START, END, fired)
    session.current_state["game_time"] = END
    assert state == session.current_state and events == fired
    settled = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "test_gate"}))
    assert [entry["content"] for entry in settled["entries"]] == expected


@pytest.mark.parametrize("source", ["effect", "trigger", "addition"])
def test_candidate_failure_after_event_effects_leaves_live_runtime_untouched(monkeypatch, source):
    session = make_session(source)
    session.vector_memory = Mock()
    references = (session.current_state, session.script_variables, session.trigger_engine, session.prompt_builder)
    baseline = copy.deepcopy((session.current_state, session.script, lore_entries(session), session._mechanics_definitions()))

    def reject_tracker(*args, **kwargs):
        raise RuntimeError("Synthetic post-effect interruption")

    monkeypatch.setattr(session.event_scheduler, "update_trackers", reject_tracker)
    with pytest.raises(RuntimeError, match="Synthetic post-effect interruption"):
        session._projected_events({"old_time": START, "_accepted_state_updates": [{"end_time": END}]})
    assert all(current is original for current, original in zip(
        (session.current_state, session.script_variables, session.trigger_engine, session.prompt_builder), references,
    ))
    assert (session.current_state, session.script, lore_entries(session), session._mechanics_definitions()) == baseline
    assert session.vector_memory.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["effect", "trigger", "addition"])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_candidate_feedback_drives_stream_decision_and_survives_storage(tmp_path, monkeypatch, source, fail_save):
    def event_lore(current):
        return {entry_id: entry for entry_id, entry in lore_entries(current).items() if entry_id.startswith("test_")}

    session = make_session(source)
    script = copy.deepcopy(session.script)
    baseline = copy.deepcopy(session.current_state)
    initial_lore = event_lore(session)
    requests = []
    narratives = {
        False: "The practice guide leaves the original seat and walks toward the assigned desk. "
               "You remain by the empty seat and wait quietly instead of calling out a greeting. "
               "After watching the display clock advance ten minutes, you postpone the conversation.",
        True: "The practice guide remains beside the original seat while the clock advances. "
              "You introduce yourself and exchange a greeting without moving to another desk. "
              "The short conversation ends as you settle back into your seat and continue waiting.",
    }

    async def respond(messages, **kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if tool_names == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        completed = [message["content"] for message in messages if message.get("role") == "tool"
                     and str(message.get("tool_call_id", "")).startswith("synthetic-chain-")]
        index = len(completed)
        requests.append(index)
        if index == 0:
            name, arguments = "submit_plan", {"plan": "Check the scheduled change before attempting an introduction."}
        elif index == 1:
            name, arguments = "update_state", copy.deepcopy(CONTACT)
        elif index in (2, 7):
            name, arguments = "update_state", {"end_time": END}
        elif index == 3:
            assert "test_release" in completed[2]
            name, arguments = "query_lorebook", {"entry_id": "test_notice"}
        elif index == 4:
            notice = json.loads(completed[3])
            assert [entry["content"] for entry in notice["entries"]] == [NOTICE]
            name, arguments = "undo_my_last_action", {"reason": "Verify the schedule before committing the wait."}
        elif index == 5:
            name, arguments = "query_lorebook", {"entry_id": "test_notice"}
        elif index == 6:
            assert json.loads(completed[5])["entries"] == []
            name, arguments = "consult_rules", {"question": "test_guide 接触", "include_candidates": True}
        elif index == 8:
            assert json.loads(completed[6])["contacts"]["test_guide"]["in_contact"] is True
            name, arguments = "consult_rules", {"question": "test_guide 接触", "include_candidates": True}
        elif index == 9:
            contact = json.loads(completed[8])["contacts"]["test_guide"]["in_contact"]
            name, arguments = "write_narrative", {"text": narratives[contact]}
        else:
            assert index == 10
            name, arguments = "finalize_turn", {"summary": "Checked the scheduled change and completed the wait."}
        assert session.current_state.get("fired_one_time_events", []) == []
        assert event_lore(session) == initial_lore
        assert name in tool_names
        return {"content": "", "tool_calls": [{"id": f"synthetic-chain-{index}", "name": name, "arguments": arguments}]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"], "focus_npcs": [],
            "skill_check": {"needed": False}, "expand_story": False, "generate_image": False,
            "skip_npc_reaction": True, "skip_choices": True, "skip_state_settlement": False,
            "context_depth": "normal", "recall_hints": [], "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    root_id = session.world_tree.add_node(None, START, 0, ai_response="You are seated at the practice area.",
                                         state_snapshot=copy.deepcopy(baseline))
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "timed-candidate.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    sessions = [session]
    await database.init_db()
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (script["script_id"], script["name"], json.dumps(script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": baseline, "narrative": "Practice opening."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/api/game/{session.save_id}/action/stream"
            action = {"type": "freeform", "text": "Check the guide before an introduction and wait ten minutes.",
                      "intent_hint": "explicit_action", "action_id": "synthetic-timed-action", "expected_node_id": root_id}
            if fail_save:
                save_node = game_routes._save_node
                failures = []

                async def reject_save(current, result, action_id=None):
                    assert current.current_state["game_time"] == END
                    assert "test_release" in current.current_state["fired_one_time_events"]
                    failures.append(action_id)
                    raise RuntimeError("Synthetic timed save interruption")

                monkeypatch.setattr(game_routes, "_save_node", reject_save)
                failed = await client.post(endpoint, json=action)
                error, = [event for event in _stream_events(failed.text) if event["type"] == "error"]
                assert error["discard_partial"] is True
                assert failures == [action["action_id"]]
                assert session.current_state == baseline
                assert event_lore(session) == initial_lore
                assert session.world_tree.active_node_id == root_id
                requests.clear()
                monkeypatch.setattr(game_routes, "_save_node", save_node)
            response = await client.post(endpoint, json=action)
            events = _stream_events(response.text)
            assert not [event for event in events if event["type"] == "error"]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narratives[False]
            assert requests == list(range(11))
            assert session._npc_contact_is_current("test_guide", session.current_state) is False
            assert set(session.current_state["fired_one_time_events"]) == {"test_configure", "test_release"}
            settled = copy.deepcopy(session.current_state)
            settled_lore = event_lore(session)
            calls = provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert provider.generate_with_tools.await_count == calls
            game_routes._sessions.clear()
            restored = await game_routes._restore_session(session.save_id)
            assert restored is not None and restored is not session
            sessions.append(restored)
            assert restored.current_state == settled
            assert event_lore(restored) == settled_lore
            assert consult_contacts(restored)["contacts"]["test_guide"]["in_contact"] is False
            game_routes._sessions[session.save_id] = restored
            node = await game_routes.get_tree_node(session.save_id, final["node_id"])
            for marker in (NOTICE, "SYNTHETIC_GATE"):
                assert marker not in response.text
                assert marker not in json.dumps(node["agent_trace"], ensure_ascii=False)
            await restored.branch_to_node(root_id)
            assert event_lore(restored) == initial_lore
            assert restored.current_state.get("fired_one_time_events", []) == []
            context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
            fresh = json.loads(await restored._query_lorebook_for_turn(context, {"entry_id": "test_notice"}))
            assert fresh["projection"]["event_ids"] == ["test_configure", "test_release"]
            assert [entry["content"] for entry in fresh["entries"]] == [NOTICE]
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
