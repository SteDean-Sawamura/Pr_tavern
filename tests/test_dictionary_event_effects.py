import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_actor_goal_settlement import result_from
from test_npc_talk_access import talk, talk_session
from test_scheduled_npc_overrides import make_session as make_shift_session
from test_story_choice_settlement import choice_client, stored_state


START = "2030-01-01T09:00:00"
END = "2030-01-01T09:15:00"


def dictionary_session(category="one_time_events", *, hidden=False):
    script = copy.deepcopy(make_shift_session(category, threshold=0).script)
    script["variables"].extend([
        {"id": "test_count", "type": "number", "default": 0},
        {"id": "test_clamped", "type": "number", "default": 0, "max": 2},
        {"id": "test_dotted", "type": "number", "default": 0},
        {"id": "test_signalled", "type": "number", "default": 0},
    ])
    script["lorebook"].extend([
        {"id": "test_edit", "content": "Synthetic original."},
        {"id": "test_remove", "content": "Synthetic temporary."},
        {"id": "test_disable", "content": "Synthetic active."},
    ])
    script["triggers"] = [{
        "id": "test_receiver", "event": "test_signal", "action": "set_var",
        "params": {"var_id": "test_signalled", "op": "inc", "value": 1},
    }]
    event = script[category][0]
    event["visibility"] = "hidden" if hidden else "public"
    event["effects"] = {
        "set_var": [
            {"var_id": "test_ready", "value": 2},
            {"var_id": "test_count", "op": "inc", "value": 1},
            {"var_id": "test_clamped", "op": "inc", "value": 9},
        ],
        "state_change": [{"target": "script_variables.test_dotted", "op": "add", "value": 1}],
        "activate_lore": ["test_notice"], "deactivate_lore": ["test_disable"],
        "add_lore": [{"id": "test_added", "content": "Synthetic added."}],
        "update_lore": [{"id": "test_edit", "content": "Synthetic updated."}],
        "remove_lore": ["test_remove"], "reveal_lore": ["test_notice"],
        "unlock_locations": ["test_desk"], "reveal_npcs": ["test_guide"],
        "set_reputation": [{"faction_id": "test_circle", "op": "set", "value": 72}],
        "unlock_events": ["test_event"], "unlock_nodes": ["test_node"],
        "activate_state": ["test_active"], "deactivate_state": ["test_old"],
        "schedule_override": [{"action": "schedule_override", "params": {
            "npc_id": "test_guide", "time_range": "09:00-10:00", "location": "test_desk",
            "condition": "test_ready >= 2",
        }}],
        "narrative_callback": "Synthetic callback.", "fire_events": ["test_signal"],
        "notify": "Synthetic notification.", "inject_prompt": "Synthetic prompt.",
    }
    session = GameSession(script, None, save_id="synthetic-dictionary-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state.update({
        "visible_locations": ["test_origin"], "active_persistent_states": ["test_old"],
        "_pipeline_mode": "agentic_unified",
    })
    return session


def assert_effects(session, state, count):
    assert state["script_variables"]["test_ready"] == 2
    assert state["script_variables"]["test_count"] == count
    assert state["script_variables"]["test_clamped"] == 2
    assert state["script_variables"]["test_dotted"] == count
    assert state["lorebook_enabled_overrides"]["test_notice"] is True
    assert state["lorebook_enabled_overrides"]["test_disable"] is False
    assert state["pc_discovered_lore"] == ["test_notice"]
    assert state["visible_locations"] == ["test_origin", "test_desk"]
    assert state["npcs"]["test_guide"]["known"] is True
    assert state["faction_reputation"]["test_circle"]["value"] == 72
    assert state["active_persistent_states"] == ["test_active"]
    assert state["events"]["test_event"]["status"] == "unlocked"
    assert state["events"]["test_node"]["status"] == "unlocked"
    assert set(state["story_tree_state"]["unlocked"]) >= {"test_event", "test_node"}
    assert len(state["npc_schedule_overrides"]["test_guide"]) == count
    assert state["npc_schedule_overrides"]["test_guide"][0]["condition"] == "test_ready >= 2"
    assert session._get_npc_location("test_guide", state) == "test_desk"


@pytest.mark.parametrize("category,count", [("one_time_events", 1), ("cyclic_events", 3)])
@pytest.mark.parametrize("hidden", [False, True])
def test_dictionary_effects_project_and_settle_once_with_logs(category, count, hidden):
    session = dictionary_session(category, hidden=hidden)
    before = copy.deepcopy((session.script, session.current_state))
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
    events, projected = session._projected_events(context, with_state=True)
    assert_effects(session, projected, count)
    assert session._projected_events(context, with_state=True) == (events, projected)
    assert (session.script, session.current_state) == before
    assert projected["script_variables"]["test_signalled"] == 0
    session.current_state["game_time"] = END
    fired = []
    changes, expired, result = session._settle_timed_events(START, END, fired)
    assert len(fired) == count and not expired
    assert_effects(session, session.current_state, count)
    assert len(changes) == 4 * count
    for index, change in enumerate(changes):
        assert change.get("_source_event_id") == ("test_shift" if hidden else None)
        if change["target"] == "script_variables.test_count":
            assert change == {
                "target": "script_variables.test_count", "op": "inc", "value": 1,
                "old": index // 4, "new": index // 4 + 1, "reason": "",
                **({"_source_event_id": "test_shift"} if hidden else {}),
            }
        if change["target"] == "script_variables.test_clamped":
            assert change["new"] == 2
    assert result.game_events.count("test_signal") == count
    assert result.notifications.count("Synthetic notification.") == count
    assert result.inject_prompts == ["Synthetic prompt."] * count
    assert session.current_state["script_variables"]["test_signalled"] == 0
    settled = copy.deepcopy(session.current_state)
    assert session._settle_timed_events(START, END, [])[0] == []
    assert session.current_state == settled
    session.current_state.setdefault("_pending_fire_events", []).extend(result.game_events)
    for turn in [1, 2]:
        session._apply_event_result(session.event_engine.tick(
            session.current_state, turn, game_time=END, old_time=END,
            condition_eval=session._evaluate_condition, advance_timed=False,
        ))
    assert session.current_state["script_variables"]["test_signalled"] == count
    assert session.script == before[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["definition", "projection", "settlement"])
async def test_dictionary_lore_and_direct_event_dispatch(path):
    session = dictionary_session()
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
    before = copy.deepcopy((session.script, session.current_state))
    if path == "definition":
        changes = session._apply_event_def_effects(session.script["one_time_events"][0], "test_shift")
        assert len(changes) == 4
        assert session.current_state["script_variables"]["test_signalled"] == 1
        assert_effects(session, session.current_state, 1)
    elif path == "settlement":
        session._settle_timed_events(START, END, [])
    for entry_id, content in [("test_added", "Synthetic added."), ("test_edit", "Synthetic updated."),
                              ("test_remove", None), ("test_disable", None)]:
        if path == "projection":
            result = json.loads(await session._query_lorebook_for_turn(context, {"entry_id": entry_id}))
        else:
            result = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": entry_id}))
        assert [entry["content"] for entry in result["entries"]] == ([] if content is None else [content])
    if path == "projection":
        assert (session.script, session.current_state) == before
    else:
        assert any(callback["text"] == "Synthetic callback." for callback in session.current_state["narrative_callbacks"])


def test_empty_dictionary_effects_are_noop():
    session = dictionary_session()
    before = copy.deepcopy(session.current_state)
    assert session._apply_event_def_effects({"effects": {}}, "test_empty") == []
    assert session.current_state == before


def test_dictionary_effects_enable_same_time_conditional_events():
    original = dictionary_session()
    script = copy.deepcopy(original.script)
    script["one_time_events"].insert(0, {
        "id": "test_follow", "trigger_time": "2030-01-01T09:05:00", "condition": "test_ready >= 2",
        "effects": {"state_change": [{"target": "script_variables.test_dotted", "op": "add", "value": 10}]},
    })
    session = GameSession(script, None)
    session.current_state = copy.deepcopy(original.current_state)
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
    events, state = session._projected_events(context, with_state=True)
    assert [event["event_id"] for event in events] == ["test_shift", "test_follow"]
    assert state["script_variables"]["test_dotted"] == 11
    assert session.current_state == original.current_state
    fired = []
    changes, _, _ = session._settle_timed_events(START, END, fired)
    assert [event["event_id"] for event in fired] == ["test_shift", "test_follow"]
    assert session.current_state["script_variables"] == state["script_variables"]
    assert len(changes) == 5


NARRATIVE = (
    "You wait beside the practice desk and watch the clock advance. "
    "You inspect the synthetic notice, record the updated indicator in your notebook, "
    "and remain in the room without disturbing the guide."
)


def install_dictionary_agent(session):
    async def respond(messages, **kwargs):
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        if names == ["classify_intent"]:
            name, arguments, step = "classify_intent", {"intent": "action"}, "intent"
        elif names == ["extract_notes"]:
            name, arguments, step = "extract_notes", {"notes": []}, "notes"
        else:
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and str(message.get("tool_call_id", "")).startswith("dictionary-step-")]
            step = len(completed)
            if step in (2, 4, 6, 8):
                assert bool(json.loads(completed[-1])["entries"]) is (step in (4, 8))
            operations = [
                ("submit_plan", {"plan": "Check the synthetic notice before and after waiting."}),
                ("query_lorebook", {"entry_id": "test_notice"}),
                ("update_state", {"end_time": END}),
                ("query_lorebook", {"entry_id": "test_notice"}),
                ("undo_my_last_action", {"reason": "Recheck the candidate clock."}),
                ("query_lorebook", {"entry_id": "test_notice"}),
                ("update_state", {"end_time": END}),
                ("query_lorebook", {"entry_id": "test_notice"}),
                ("write_narrative", {"text": NARRATIVE}),
                ("finalize_turn", {"summary": "Completed the synthetic wait."}),
            ]
            assert step < len(operations)
            name, arguments = operations[step]
            assert session.current_state["game_time"] == START
            assert session.current_state["script_variables"]["test_count"] == 0
            assert not session.current_state.get("lorebook_enabled_overrides")
        assert name in names
        return {"content": "", "tool_calls": [{
            "id": f"dictionary-step-{step}" if isinstance(step, int) else f"dictionary-{step}",
            "name": name, "arguments": arguments,
        }]}

    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("category,count", [("one_time_events", 1), ("cyclic_events", 3)])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_dictionary_harness_persistence_retry_replay_reload_and_regeneration(tmp_path, monkeypatch, category, count, fail_save):
    session = dictionary_session(category)
    install_dictionary_agent(session)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Wait and inspect the synthetic notice.",
                  "action_id": "synthetic-dictionary-action", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert result["narrative"] == NARRATIVE
                assert_effects(current, current.current_state, count)
                raise RuntimeError("Synthetic dictionary save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failure = await asyncio.wait_for(client.post(endpoint, json=action), 20)
            assert '"type": "error"' in failure.text and '"type": "final"' not in failure.text
            assert session.current_state == before and await stored_state(session) == baseline
            assert session.world_tree.active_node_id == root
            lore = json.loads(await session._run_tool_native_async("query_lorebook", {"entry_id": "test_added"}))
            assert lore["entries"] == []
        response = await asyncio.wait_for(client.post(endpoint, json=action), 20)
        result = result_from(response)
        assert result["narrative"] == NARRATIVE
        settled = await stored_state(session)
        assert_effects(session, settled, count)
        assert settled["_pending_fire_events"].count("test_signal") == count
        assert settled["script_variables"]["test_signalled"] == 0
        calls = session.ai_provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert session.ai_provider.generate_with_tools.await_count == calls
        assert await stored_state(session) == settled
        regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 20)
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["narrative"] == NARRATIVE
        assert regenerated.json()["total_swipes"] == 2
        assert_effects(session, await stored_state(session), count)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert_effects(restored, restored.current_state, count)
        await restored.branch_to_node(root)
        assert restored.current_state["script_variables"]["test_count"] == 0
        await restored.branch_to_node(result["node_id"])
        assert_effects(restored, restored.current_state, count)
        for _ in range(2):
            restored._prepare_turn({"type": "system", "text": "Inspect the synthetic checkpoint."})
            assert restored.current_state["script_variables"]["test_signalled"] == count


@pytest.mark.parametrize("effects", [
    {"activate_lore": ["test_notice"]},
    [{"type": "activate_lore", "id": "test_notice"}],
])
def test_timed_lore_is_initially_gated_in_both_effect_formats(effects):
    script = copy.deepcopy(dictionary_session().script)
    script["one_time_events"][0]["effects"] = effects
    next(entry for entry in script["lorebook"] if entry["id"] == "test_notice")["enabled"] = True
    session = GameSession(script, None)
    entry = next(entry for entry in session.prompt_builder.lorebook.entries if entry.id == "test_notice")
    assert entry.enabled is False
    assert entry.available_from == "2030-01-01T09:05:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["one_time_events", "cyclic_events"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("effect_format", ["dict", "list"])
async def test_npc_dialogue_settles_each_timed_effect_once(category, streaming, effect_format):
    session = dictionary_session(category)
    if effect_format == "list":
        script = copy.deepcopy(session.script)
        script[category][0]["effects"] = [{
            "type": "state_change", "target": "script_variables.test_count", "op": "add", "value": 1,
        }]
        initial = copy.deepcopy(session.current_state)
        session = GameSession(script, None)
        session.current_state = initial
    session.current_state["npcs"]["test_guide"].update({"known": True, "met": True})
    session._refresh_npc_locations()
    session.ai_provider = talk_session().ai_provider
    try:
        result = await talk(session, streaming)
        assert "error" not in result, result
        assert result["triggered_events"] == ["test_shift"]
        assert session.current_state["script_variables"]["test_count"] == 1
        changes = [change for change in result["state_changes"] if change["target"] == "script_variables.test_count"]
        assert len(changes) == 1 and changes[0]["old"] == 0 and changes[0]["new"] == 1
        if effect_format == "dict":
            assert_effects(session, session.current_state, 1)
            assert session.current_state["_pending_fire_events"].count("test_signal") == 1
            assert session.current_state["script_variables"]["test_signalled"] == 0
            for _ in range(2):
                session._prepare_turn({"type": "system", "text": "Inspect the synthetic dialogue checkpoint."})
                assert session.current_state["script_variables"]["test_signalled"] == 1
    finally:
        await session._drain_background_tasks()
