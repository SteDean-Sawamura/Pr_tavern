import copy

import pytest

from engine.event_engine import GameEvent
from engine.game_session import GameSession
from test_choice_instructions import ACTION


ROOT_INSTRUCTION = "SYNTHETIC_GENERATION_ROOT_INSTRUCTION"


def instructions(depth):
    return [ROOT_INSTRUCTION] + [f"SYNTHETIC_GENERATION_CHILD_{index}" for index in range(depth)]


def make_session(origin="lifecycle", depth=2, *, allowed=True, direct=True):
    root_effects = {
        "set_var": [{"var_id": "player.test_generation_count", "op": "add", "value": 1}],
        "fire_events": ["test_generation_signal_0"],
    }
    if direct:
        root_effects["inject_prompt"] = ROOT_INSTRUCTION
    nodes = [{
        "id": "test_generation_root", "name": "Synthetic generation hook",
        "type": "trigger" if origin == "lifecycle" else "auto",
        "event": ["before_generation"], "condition": "player.test_generation_count == 0",
        "effects": root_effects,
    }]
    for index, instruction in enumerate(instructions(depth)[1:]):
        effects = {
            "inject_prompt": instruction,
            "set_var": [{"var_id": "player.test_generation_chain", "op": "add", "value": 1}],
        }
        if index + 1 < depth:
            effects["fire_events"] = [f"test_generation_signal_{index + 1}"]
        nodes.append({
            "id": f"test_generation_child_{index}", "name": "Synthetic generation receiver",
            "type": "trigger", "event": [f"test_generation_signal_{index}"], "effects": effects,
            "condition": ("player.test_generation_count > 1" if not allowed and index == depth - 1
                          else "player.test_generation_count == 1"),
        })
    script = {
        "script_id": "synthetic-generation-cascade", "name": "Synthetic generation cascade", "settings": {},
        "locations": [{"id": "test_room", "name": "Practice room"}],
        "story_tree": {"trees": [{"id": "test_generation_tree", "nodes": nodes}]},
    }
    session = GameSession(script, None, save_id="synthetic-generation-cascade-save")
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room",
                   "test_generation_count": 0, "test_generation_chain": 0},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Practice room"},
    }
    return session


@pytest.mark.parametrize("origin", ["lifecycle", "story"])
@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("allowed,direct", [(True, True), (False, True), (True, False)])
def test_preparation_delivers_only_executed_generation_cascade_instructions(origin, depth, allowed, direct):
    session = make_session(origin, depth, allowed=allowed, direct=direct)
    script_before = copy.deepcopy(session.script)
    expected = instructions(depth if allowed else depth - 1)[0 if direct else 1:]
    context = session._prepare_turn(ACTION)
    assert session.current_state["player"]["test_generation_chain"] == (depth if allowed else depth - 1)
    assert context["turn_instructions"] == expected
    for instruction in expected:
        assert instruction in context["history_context"]
        assert instruction in session._build_unified_context(context, ACTION)
    assert not session.current_state.get("_pending_fire_events")
    assert not session.current_state.get("_pending_choice_instructions")
    assert session._prepare_turn(ACTION)["turn_instructions"] == []
    assert session.current_state["player"]["test_generation_count"] == 1
    assert session.current_state["player"]["test_generation_chain"] == (depth if allowed else depth - 1)
    assert session.script == script_before


@pytest.mark.parametrize("origin", ["lifecycle", "story"])
def test_lifecycle_dispatch_collects_children_without_rewriting_raw_results(monkeypatch, origin):
    session = make_session(origin)
    engine = session.event_engine if origin == "lifecycle" else session.story_tree_engine
    method = "fire_event" if origin == "lifecycle" else "evaluate"
    dispatch = getattr(engine, method)
    results = []

    def capture(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        results.append((result, copy.deepcopy(result)))
        return result

    monkeypatch.setattr(engine, method, capture)
    delivered, notifications = session._fire_lifecycle_event("before_generation")
    assert session.current_state["player"]["test_generation_chain"] == 2
    assert delivered == instructions(2)
    assert all(current == original for current, original in results)
    assert all(instruction not in " ".join(notifications) for instruction in delivered)


@pytest.mark.parametrize("origin", ["lifecycle", "story"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_generation_sibling_instructions_keep_dispatch_order_and_context_deduplication(origin, duplicate):
    session = make_session(origin)
    sibling_instruction = instructions(2)[1] if duplicate else "SYNTHETIC_GENERATION_SIBLING"
    session.event_engine.add_event(GameEvent(
        id="test_generation_sibling", category="trigger", metadata={"event": ["test_generation_signal_0"]},
        effects={"inject_prompt": sibling_instruction},
    ))
    expected = instructions(2)
    if not duplicate:
        expected.insert(2, sibling_instruction)
    context = session._prepare_turn(ACTION)
    assert context["turn_instructions"] == expected
    assert session.current_state["player"]["test_generation_count"] == 1
    assert session.current_state["player"]["test_generation_chain"] == 2


@pytest.mark.parametrize("origin", ["lifecycle", "story"])
def test_rejected_generation_root_delivers_no_child_effects_or_instructions(origin):
    session = make_session(origin)
    session.current_state["player"]["test_generation_count"] = 2
    context = session._prepare_turn(ACTION)
    assert context["turn_instructions"] == []
    assert session.current_state["player"]["test_generation_count"] == 2
    assert session.current_state["player"]["test_generation_chain"] == 0
