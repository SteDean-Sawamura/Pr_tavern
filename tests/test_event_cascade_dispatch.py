import copy

import pytest

from engine.event_engine import GameEvent
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_npc_talk_access import talk, talk_session
from test_scheduled_event_instructions import END, INSTRUCTION, START, instruction_session


ACTION = {"type": "system", "text": "Observe the synthetic signal queue."}


def instructions(depth):
    return [f"SYNTHETIC_TICK_CASCADE_INSTRUCTION_{index}" for index in range(depth)]


def cascade_session(category="one_time_events", depth=2, signal_field="effects", condition=""):
    source, _ = instruction_session(category)
    script = copy.deepcopy(source.script)
    script.update(script_id="synthetic-tick-cascade", name="Synthetic tick cascade")
    event = script[category][0]
    destination = event["effects"] if signal_field == "effects" else event
    destination["fire_events"] = ["test_signal_0"]
    nodes = []
    for index, instruction in enumerate(instructions(depth)):
        script["variables"].append({"id": f"test_count_{index}", "type": "number", "default": 0})
        effects = {
            "inject_prompt": instruction,
            "set_var": [{"var_id": f"test_count_{index}", "op": "inc", "value": 1}],
        }
        if index + 1 < depth:
            effects["fire_events"] = [f"test_signal_{index + 1}"]
        nodes.append({
            "id": f"test_receiver_{index}", "name": "Synthetic receiver", "type": "trigger",
            "event": [f"test_signal_{index}"], "effects": effects,
            "condition": condition if index == depth - 1 else "",
        })
    script["story_tree"] = {"trees": [{"id": "test_tree", "nodes": nodes}]}
    session = GameSession(script, None, save_id="synthetic-tick-cascade-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    return session


def counts(session, depth=2):
    return [session.current_state["script_variables"][f"test_count_{index}"] for index in range(depth)]


def settle_root(session):
    session.current_state["game_time"] = END
    _, _, result = session._settle_timed_events(START, END, [])
    session.current_state.setdefault("_pending_fire_events", []).extend(result.game_events)
    return result


@pytest.mark.parametrize("category,count", [("one_time_events", 1), ("cyclic_events", 3)])
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("signal_field", ["effects", "metadata"])
def test_scheduled_cascades_execute_once_with_same_turn_instructions(category, count, depth, signal_field):
    session = cascade_session(category, depth, signal_field)
    context = {"old_time": START, "_accepted_state_updates": [{"end_time": END}]}
    before = copy.deepcopy((session.current_state, session.script, context))
    receipts = []
    events, projected = session._projected_events(context, with_state=True, event_instructions=receipts)
    assert (session.current_state, session.script, context) == before
    assert len(events) == count
    assert [receipt["text"] for receipt in receipts] == [INSTRUCTION] * count
    assert projected["script_variables"]["test_ready"] == count
    assert all(projected["script_variables"][f"test_count_{index}"] == 0 for index in range(depth))
    result = settle_root(session)
    assert result.scheduled_instructions == receipts
    assert counts(session, depth) == [0] * depth
    assert session.current_state["_pending_fire_events"] == ["test_signal_0"] * count
    for turn in range(depth + 1):
        context = session._prepare_turn(ACTION)
        assert context["turn_instructions"] == (instructions(depth) if turn == 0 else [])
        assert counts(session, depth) == [count] * depth
        assert not session.current_state.get("_pending_fire_events")
    assert session.script == before[1]


@pytest.mark.parametrize("queue_cascades", [False, True])
def test_tick_cascade_ownership_preserves_standalone_default_and_raw_result(queue_cascades):
    session = cascade_session()
    session.current_state["_pending_fire_events"] = ["test_signal_0"]
    options = {} if queue_cascades else {"queue_cascades": False}
    result = session.event_engine.tick(session.current_state, 1, **options)
    assert result.game_events == ["test_signal_1"]
    assert result.inject_prompts == instructions(2)[:1]
    assert session.current_state.get("_pending_fire_events", []) == (["test_signal_1"] if queue_cascades else [])
    assert counts(session) == [0, 0]
    if not queue_cascades:
        original = copy.deepcopy(result)
        collected = list(result.inject_prompts)
        state_log = session._apply_event_result(result, inject_prompts=collected)
        assert isinstance(state_log, list)
        assert result == original
        assert counts(session) == [1, 1]
        assert collected == instructions(2)
        assert not session.current_state.get("_pending_fire_events")


@pytest.mark.parametrize("condition,expected", [("test_count_0 >= 1", 1), ("test_count_0 >= 2", 0)])
def test_descendant_condition_uses_applied_parent_effects(condition, expected):
    session = cascade_session(condition=condition)
    settle_root(session)
    context = session._prepare_turn(ACTION)
    assert counts(session) == [1, expected]
    assert context["turn_instructions"] == instructions(1 + expected)
    assert not session.current_state.get("_pending_fire_events")
    assert session._prepare_turn(ACTION)["turn_instructions"] == []
    assert counts(session) == [1, expected]


def test_deferred_receiver_keeps_dispatch_time_condition():
    session = cascade_session(depth=1, condition=f"game_time < {END}")
    settle_root(session)
    assert session._prepare_turn(ACTION)["turn_instructions"] == []
    assert counts(session, 1) == [0]
    assert not session.current_state.get("_pending_fire_events")


@pytest.mark.parametrize("legacy_prompt", [False, True])
def test_tick_cascade_instructions_reach_all_generation_contexts(monkeypatch, legacy_prompt):
    session = cascade_session(depth=3)
    settle_root(session)
    if legacy_prompt:
        monkeypatch.setattr(session.prompt_builder, "build_narrative_system_prompt", lambda *args, **kwargs: "Synthetic system")
    context = session._prepare_turn(ACTION, legacy_prompt=legacy_prompt)
    assert context["turn_instructions"] == instructions(3)
    for instruction in instructions(3):
        assert instruction in context["history_context"]
        assert instruction in session._build_unified_context(context, ACTION)
        if legacy_prompt:
            assert instruction in context["system_prompt"]


def test_regeneration_pending_replay_does_not_run_other_tick_phases():
    session = cascade_session()
    settle_root(session)
    session.event_engine.add_event(GameEvent(id="test_deadline", category="deadline"))
    deadline = {"status": "active", "turns_remaining": 1}
    session.current_state["events"]["test_deadline"] = copy.deepcopy(deadline)
    before_time = session.current_state["game_time"]
    before_turn = session.turn_number
    assert session._replay_pending_event_cascades() == instructions(2)
    assert counts(session) == [1, 1]
    assert session.current_state["events"]["test_deadline"] == deadline
    assert session.current_state["game_time"] == before_time and session.turn_number == before_turn
    assert session._replay_pending_event_cascades() == []
    assert counts(session) == [1, 1]


def test_sibling_instructions_keep_existing_effect_execution_order():
    session = cascade_session()
    session.event_engine.add_event(GameEvent(
        id="test_sibling", category="trigger", metadata={"event": ["test_signal_0"]},
        effects={"inject_prompt": "SYNTHETIC_TICK_SIBLING_INSTRUCTION"},
    ))
    settle_root(session)
    context = session._prepare_turn(ACTION)
    assert context["turn_instructions"] == [instructions(2)[0], "SYNTHETIC_TICK_SIBLING_INSTRUCTION", instructions(2)[1]]
    assert counts(session) == [1, 1]
    assert not session.current_state.get("_pending_fire_events")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_npc_dialogue_does_not_requeue_synchronously_applied_cascades(streaming):
    session = cascade_session()
    settle_root(session)
    session.current_state["npcs"]["test_guide"].update({"known": True, "met": True})
    session._refresh_npc_locations()
    session.ai_provider = talk_session().ai_provider
    try:
        result = await talk(session, streaming)
        assert "error" not in result, result
        assert counts(session) == [1, 1]
        assert not session.current_state.get("_pending_fire_events")
        session._prepare_turn(ACTION)
        assert counts(session) == [1, 1]
    finally:
        await session._drain_background_tasks()
