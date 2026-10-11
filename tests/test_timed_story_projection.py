import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from engine.session.agentic_mixin import _bounded_tool_content
from test_agent_state_compaction import compact
from test_story_runtime_discovery import runtime_query, records
from test_timed_condition_projection import START, FIRE, END, WINDOW


def story_session(*, dynamic=False, activation="event.test_bell", condition=WINDOW, node_condition=WINDOW):
    variable = {"id": "test_gate", "type": "number", "default": 0, "min": 0, "max": 20}
    script = {
        "script_id": "synthetic-timed-story", "name": "Synthetic timed story", "settings": {},
        "start_time": START,
        "player_character": {"name": "Visitor", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "Test room"}],
        "variables": [] if dynamic else [variable],
        "lorebook": [{"id": "test_notice", "enabled": False, "constant": True,
                      "content": "SYNTHETIC_TIMED_STORY_PRIVATE_RULE"}],
        "one_time_events": [{
            "id": "test_bell", "trigger_time": FIRE, "condition": condition,
            "fire_events": ["test_signal"],
            "effects": [
                ({"type": "add_variable", "variable": {**variable, "default": 7}} if dynamic else
                 {"type": "state_change", "target": "script_variables.test_gate", "op": "set", "value": 7}),
                {"type": "activate_lore", "id": "test_notice"},
            ],
        }],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [
            {"id": "test_active", "type": "choice", "choices": [{
                "id": "test_open", "label": "Inspect the practice screen", "condition": "test_gate >= 7",
                "effects": {"set_var": [{"var_id": "test_gate", "op": "inc", "value": 1}],
                            "deactivate_lore": ["test_notice"]},
            }]},
            {"id": "test_waiting", "type": "choice", "activate_events": [activation],
             "condition": node_condition, "choices": [{"id": "test_continue", "label": "Continue"}]},
            {"id": "test_timer", "type": "timed", "duration_turns": 3},
        ]}]},
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    session.script_variables.init_state(session.current_state)
    session.story_tree_engine.evaluate(session.current_state, 0, session._evaluate_condition)
    session.event_engine.sync_story_tree_state(session.current_state)
    return session


def preview(session, context, node_id="test_active", choice_id="test_open", **arguments):
    return json.loads(session._preview_declared_choice({
        "node_id": node_id, "choice_id": choice_id, "include_candidates": True, **arguments,
    }, context))


def context(end_time=END):
    return {"old_time": START, "new_time": END,
            "_accepted_state_updates": [{"end_time": end_time}] if end_time else []}


@pytest.mark.parametrize("activation", ["event.test_bell", "test_signal"])
@pytest.mark.parametrize("dynamic", [False, True])
def test_timed_story_view_and_preview_share_event_base_without_activating_nodes(activation, dynamic):
    session = story_session(activation=activation, dynamic=dynamic)
    state = session.current_state
    before = copy.deepcopy((state, session.script, session.story_tree_engine.__dict__,
                            session.event_engine.__dict__, session._mechanics_definitions(),
                            session.prompt_builder.lorebook.__dict__))
    projected = runtime_query(session, context(), include_candidates=True)
    indexed = {item["node_id"]: item for item in records(projected)}
    assert indexed["test_waiting"]["status"] == "unlocked"
    assert indexed["test_waiting"]["choices"][0]["reason"] == "node_not_active"
    assert indexed["test_active"]["choices"][0]["available"] is True
    assert indexed["test_timer"]["timed_progress"] == state["story_tree_state"]["timed_progress"]["test_timer"]
    assert projected["projection"]["event_ids"] == ["test_bell"]
    assert projected["projection"]["excludes_unsettled_events"] is False
    result = preview(session, context())
    assert result["available"] is True
    assert result["projection"]["event_ids"] == ["test_bell"]
    assert result["projection"]["excludes_unsettled_events"] is False
    assert result["projection"]["excludes_lifecycle"] is True
    assert result["projection"]["scope"] == "choice_and_emitted_events"
    reward_changes = [change for change in result["state_changes"] if change["path"] == ["script_variables", "test_gate"]]
    assert reward_changes == [{"path": ["script_variables", "test_gate"], "operation": "replace", "before": 7, "after": 8}]
    notice_changes = [change for change in result["lore_changes"] if change["path"][0] == "test_notice"]
    assert notice_changes == [{"path": ["test_notice", "enabled"], "operation": "replace", "before": True, "after": False}]
    assert "test_waiting" not in result["unlocked"]
    waiting = preview(session, context(), "test_waiting", "test_continue")
    assert waiting["available"] is False and waiting["reason"] == "node_not_active"
    assert session.current_state is state
    assert (state, session.script, session.story_tree_engine.__dict__, session.event_engine.__dict__,
            session._mechanics_definitions(), session.prompt_builder.lorebook.__dict__) == before
    assert records(runtime_query(session, query="test_waiting"))[0]["status"] == "locked"
    session._settle_timed_events(START, END, [])
    session.current_state["game_time"] = END
    base = copy.deepcopy(session.current_state)
    session._settle_story_choice("test_active", "test_open")
    assert session.current_state["script_variables"]["test_gate"] == 8
    assert result["projection"]["base_state_hash"] == session.world_tree.state_hash(base)
    assert result["projection"]["result_state_hash"] == session.world_tree.state_hash(session.current_state)


@pytest.mark.parametrize("end_time", [None, "2030-01-01T09:04:00", END])
@pytest.mark.parametrize("condition", [WINDOW, f"game_time > {FIRE}"])
def test_story_candidates_use_only_accepted_time_and_event_time_conditions(end_time, condition):
    session = story_session(condition=condition)
    available = end_time == END and condition == WINDOW
    result = runtime_query(session, context(end_time), include_candidates=True, query="test_waiting")
    assert records(result)[0]["status"] == ("unlocked" if available else "locked")
    assert preview(session, context(end_time))["available"] is available
    assert not session.current_state.get("fired_one_time_events")


def test_story_runtime_and_preview_snapshots_expire_after_time_undo_and_rule_changes():
    session = story_session()
    turn = context()
    runtime = runtime_query(session, turn, include_candidates=True, query="test_active")["entries"][0]
    outcome = preview(session, turn)
    assert outcome["available"] is True
    turn["_accepted_state_updates"].clear()
    assert runtime_query(session, turn, **runtime["read"])["error_code"] == "snapshot_expired"
    assert preview(session, turn, snapshot=outcome["snapshot"], result_path=[])["error_code"] == "snapshot_expired"
    assert preview(session, turn)["available"] is False
    turn["_accepted_state_updates"].append({"end_time": END})
    assert preview(session, turn)["available"] is True
    session.script["one_time_events"][0]["effects"][0]["value"] = 8
    assert runtime_query(session, turn, **runtime["read"])["error_code"] == "snapshot_expired"
    assert preview(session, turn, snapshot=outcome["snapshot"], result_path=[])["error_code"] == "snapshot_expired"


def test_variable_candidates_keep_their_existing_accepted_update_only_contract():
    session = story_session()
    result = json.loads(session._handle_consult_rules({
        "category": "variables", "include_candidates": True,
    }, context()))
    item = json.loads(result["entries"][0]["content"])
    assert item["current_value"] == item["candidate_value"] == 0
    assert result["projection"]["excludes_unsettled_events"] is True


@pytest.mark.parametrize("query_kind", ["runtime", "preview"])
@pytest.mark.parametrize("fail", [False, True])
def test_scheduled_mechanics_lore_and_failures_stay_inside_query_copy(monkeypatch, query_kind, fail):
    session = story_session()
    session.script["one_time_events"][0]["effects"].extend([
        {"type": "add_random_item", "item": {"id": "test_random", "description": "Synthetic random item"}},
        {"type": "add_lore", "entry": {"id": "test_added", "content": "SYNTHETIC_TIMED_ADDED_RULE"}},
        {"type": "add_trigger", "trigger": {"id": "test_trigger", "event": "test_bell", "condition": WINDOW,
         "action": "set_var", "params": {"var_id": "test_gate", "op": "set", "value": 9}}},
    ])
    before = copy.deepcopy((session.current_state, session.script, session._random_item_by_id,
                            session._event_def_by_id, session.event_scheduler.__dict__,
                            session.story_tree_engine.__dict__, session.event_engine.__dict__,
                            session._mechanics_definitions(), session.prompt_builder.lorebook.__dict__,
                            session.prompt_builder._lorebook_override_ids))

    def forbidden(*args, **kwargs):
        raise AssertionError("Query must not create background work")

    for method in ("_schedule_index_task", "_schedule_background_task", "_schedule_deferred_task"):
        monkeypatch.setattr(session, method, forbidden)
    if fail:
        def reject_choice(*args, **kwargs):
            raise RuntimeError("Synthetic query interruption")

        monkeypatch.setattr(GameSession, "_story_node_runtime" if query_kind == "runtime" else "_settle_story_choice", reject_choice)

    def query():
        if query_kind == "runtime":
            return runtime_query(session, context(), include_candidates=True, query="test_active")
        return preview(session, context())

    if fail:
        with pytest.raises(RuntimeError, match="Synthetic query interruption"):
            query()
    else:
        result = query()
        assert result == query()
        assert (records(result)[0]["choices"][0]["available"] if query_kind == "runtime" else result["available"])
    assert (session.current_state, session.script, session._random_item_by_id, session._event_def_by_id,
            session.event_scheduler.__dict__, session.story_tree_engine.__dict__, session.event_engine.__dict__,
            session._mechanics_definitions(), session.prompt_builder.lorebook.__dict__,
            session.prompt_builder._lorebook_override_ids) == before
    assert not session._background_tasks and not session._index_tasks and not session._deferred_tasks


@pytest.mark.parametrize("initial_clock", [START, END])
def test_candidate_story_event_conditions_use_fire_time_not_current_clock(initial_clock):
    session = story_session()
    session.current_state["game_time"] = initial_clock
    result = runtime_query(session, context(), include_candidates=True, query="test_waiting")
    assert records(result)[0]["status"] == "unlocked"
    assert session.current_state["game_time"] == initial_clock


@pytest.mark.asyncio
async def test_story_query_compression_retains_scheduled_scope_without_body():
    session = story_session()
    session.ai_provider = SimpleNamespace(generate_with_tools=AsyncMock(return_value={"content": "Done.", "tool_calls": []}))
    arguments = {"category": "story_nodes", "view": "runtime", "include_candidates": True}
    result = session._handle_consult_rules(arguments, context())
    bounded = json.loads(_bounded_tool_content(result, "consult_rules", 1800))
    assert bounded["truncated"] is True and bounded["projection"]["scheduled_through"] == END
    assert bounded["projection"]["excludes_lifecycle"] is True
    assert "不含未结算效果" not in bounded["notice"]
    previous = None
    for _ in range(2):
        previous = await compact(session, [("consult_rules", arguments, result)], previous)
        assert "候选未提交且不含未结算事件" not in previous
        assert '"scheduled_through": "2030-01-01T09:10:00"' in previous
        assert "不提前激活节点" in previous
    outcome = json.dumps(preview(session, context()), ensure_ascii=False)
    shortened = json.loads(_bounded_tool_content(outcome, "preview_choice_outcome", 1400))
    assert shortened["content_omitted"] is True
    assert shortened["projection"]["scheduled_through"] == END
    assert shortened["projection"]["excludes_lifecycle"] is True
    assert shortened["projection"]["excludes_event_dice"] is True
