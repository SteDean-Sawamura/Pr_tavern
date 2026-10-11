import asyncio
from collections import OrderedDict
import copy
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.session.agentic_mixin import _bounded_tool_content, _public_agent_trace
from engine.tool_schemas import _unified_tools


def make_session(threshold=7):
    session = GameSession({
        "script_id": "synthetic-choice-preview", "name": "Synthetic choice preview", "settings": {},
        "variables": [
            {"id": "test_gate", "type": "number", "default": 0},
            {"id": "test_reward", "type": "number", "default": 0},
        ],
        "locations": [{"id": "test_room", "name": "Reading room"}],
        "lorebook": [{"id": "test_detail", "enabled": False, "keys": ["shelf"],
                      "content": "SYNTHETIC_PREVIEW_DETAIL"}],
        "story_tree": {"trees": [{"id": "test_tree", "nodes": [
            {"id": "test_decision", "type": "choice", "name": "Synthetic decision",
             "effects": {"set_var": [{"var_id": "test_reward", "op": "inc", "value": 1}]},
             "on_complete_unlock": ["test_followup"],
             "choices": [{"id": "test_open", "label": "Inspect the shelf",
                          "condition": f"script_variables.test_gate >= {threshold}",
                          "unlock": ["test_other"],
                          "effects": {
                              "set_var": [{"var_id": "test_reward", "op": "inc", "value": 3}],
                              "activate_lore": ["test_detail"],
                              "add_lore": [{"id": "test_added", "content": "SYNTHETIC_PREVIEW_EFFECT"}],
                              "fire_events": ["test_signal"],
                              "narrative_callback": "SYNTHETIC_PREVIEW_CALLBACK",
                              "activate_state": ["test_status"],
                          }}]},
            {"id": "test_followup", "type": "quest", "requires": ["test_decision"]},
            {"id": "test_other", "type": "quest", "requires": ["test_decision"]},
        ]}]},
    }, None)
    session.current_state = {
        "_pipeline_mode": "agentic_unified", "game_time": "2030-01-01T09:00:00",
        "player": {"name": "Visitor", "location": "test_room"},
        "visible_locations": ["test_room"], "display_names": {"test_room": "Reading room"},
    }
    session.script_variables.init_state(session.current_state)
    session.story_tree_engine.evaluate(session.current_state, 0, session._evaluate_condition)
    return session


def arguments(**overrides):
    return {"node_id": "test_decision", "choice_id": "test_open", **overrides}


async def preview(session, context=None, **overrides):
    return json.loads(await session._dispatch_unified_tool(
        context or {}, "preview_choice_outcome", arguments(**overrides),
    ))


def test_schema_exposes_declaration_identity_and_candidate_view():
    schema = next(tool["function"]["parameters"] for tool in _unified_tools()
                  if tool["function"]["name"] == "preview_choice_outcome")
    assert {"node_id", "choice_id", "include_candidates"} <= set(schema["properties"])
    assert "choice_text" not in schema.get("required", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [7, 13])
@pytest.mark.parametrize("candidate", [False, True])
async def test_preview_uses_declared_conditions_without_live_settlement(threshold, candidate, monkeypatch):
    session = make_session(threshold)
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": threshold}]},
    ]}
    state = session.current_state
    before = copy.deepcopy((state, session.script, session.story_tree_engine.__dict__,
                            session.event_engine.__dict__, session.prompt_builder.lorebook.__dict__, context))

    def unexpected(*args, **kwargs):
        raise AssertionError("Read-only preview must not schedule external work")

    for method in ("_schedule_index_task", "_schedule_background_task"):
        monkeypatch.setattr(session, method, unexpected)
    result = await preview(session, context, include_candidates=candidate)
    assert result["mode"] == "declared_choice"
    assert result["source"] == "runtime_story_tree"
    assert result["available"] is candidate
    assert result["projection"]["state_view"] == ("candidate" if candidate else "committed")
    assert result["projection"]["effects_applied"] is candidate
    assert result["projection"]["excludes_unsettled_events"] is True
    assert result["projection"]["persisted"] is False
    assert result["projection"]["scope"] == "choice_and_emitted_events"
    assert "predicted_risk" not in result
    if candidate:
        assert result["completed"] == ["test_decision"]
        assert result["unlocked"] == ["test_followup", "test_other"]
        assert [effect["params"]["value"] for effect in result["pending_effects"]["effects"]] == [1, 3]
        assert result["pending_effects"]["lore_activations"] == ["test_detail"]
        assert result["pending_effects"]["game_events"] == ["test_signal"]
    else:
        assert result["reason"] == "condition_not_met"
        assert result["pending_effects"] == {}
    assert session.current_state is state
    assert (state, session.script, session.story_tree_engine.__dict__, session.event_engine.__dict__,
            session.prompt_builder.lorebook.__dict__, context) == before


@pytest.mark.asyncio
async def test_preview_rechecks_after_candidate_undo_and_branch_state_changes():
    session = make_session()
    context = {"old_time": session.current_state["game_time"], "_accepted_state_updates": [
        {"variable_changes": [{"id": "test_gate", "op": "set", "value": 7}]},
    ]}
    assert (await preview(session, context, include_candidates=True))["available"] is True
    context["_accepted_state_updates"].clear()
    assert (await preview(session, context, include_candidates=True))["available"] is False
    session.current_state["script_variables"]["test_gate"] = 7
    assert (await preview(session))["available"] is True
    session.current_state["story_tree_state"]["active"].clear()
    result = await preview(session)
    assert result["available"] is False and result["reason"] == "node_not_active"
    assert result["pending_effects"] == {}


@pytest.mark.asyncio
async def test_pending_effects_match_canonical_choice_result():
    session = make_session()
    session.current_state["script_variables"]["test_gate"] = 7
    result = await preview(session)
    state = copy.deepcopy(session.current_state)
    expected = session.story_tree_engine.make_choice(
        state, "test_decision", "test_open", lambda condition: session._evaluate_condition(condition, state),
    )
    assert result["pending_effects"] == {
        key: value for key, value in asdict(expected).items()
        if value and key not in {"newly_completed", "newly_active"}
    }
    assert session.current_state["script_variables"]["test_reward"] == 0
    session.current_state = state
    session._apply_story_tree_result(expected)
    assert session.current_state["script_variables"]["test_reward"] == 4
    assert next(entry for entry in session.prompt_builder.lorebook.entries if entry.id == "test_detail").enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", [None, False, 0, [], {}, "test_gate >= 0 OR", "(test_gate >= 0)"])
async def test_preview_uses_existing_condition_parser(condition):
    session = make_session()
    session.story_tree_engine._nodes["test_decision"]["choices"][0]["condition"] = condition
    result = await preview(session)
    valid = condition == "(test_gate >= 0)"
    assert result["available"] is valid
    assert bool(result.get("condition_error")) is not valid


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"node_id": "unknown"}, {"choice_id": "unknown"}, {"node_id": "test_followup"},
    {"node_id": None}, {"choice_id": 1}, {"choice_id": ""},
    {"include_candidates": "true"}, {"include_candidates": True},
])
async def test_preview_rejects_invalid_identity_and_candidate_view(overrides):
    session = make_session()
    before = copy.deepcopy(session.current_state)
    assert "error" in await preview(session, **overrides)
    assert session.current_state == before


@pytest.mark.asyncio
async def test_preview_reads_runtime_nodes_and_rejects_missing_story_engine():
    session = make_session()
    node = session.story_tree_engine._nodes["test_decision"]
    node["choices"][0]["id"] = "test_new_choice"
    assert "error" in await preview(session)
    assert (await preview(session, choice_id="test_new_choice"))["available"] is False
    session.story_tree_engine = None
    assert "error" in await preview(session, choice_id="test_new_choice")


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"include_candidates": False}])
async def test_legacy_text_preview_remains_a_heuristic(options):
    result = json.loads(await make_session()._dispatch_unified_tool(
        {}, "preview_choice_outcome", {"choice_text": "观察房间", **options},
    ))
    assert result["predicted_risk"] == "safe"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"choice_text": None}, {"choice_text": " "},
                                  {"node_id": "test_decision", "choice_text": "Inspect the shelf"},
                                  {"choice_text": "Inspect the shelf", "include_candidates": True}])
async def test_incomplete_declaration_does_not_fall_back_to_text_risk(args):
    result = json.loads(await make_session()._dispatch_unified_tool({}, "preview_choice_outcome", args))
    assert "error" in result and "predicted_risk" not in result


@pytest.mark.asyncio
async def test_bounded_preview_marks_omitted_effects_instead_of_partial_json():
    session = make_session()
    session.current_state["script_variables"]["test_gate"] = 7
    result = await preview(session)
    result["pending_effects"]["lore_additions"][0]["content"] = "Synthetic long declaration. " * 1000
    encoded = _bounded_tool_content(json.dumps(result), "preview_choice_outcome", 1000)
    bounded = json.loads(encoded)
    assert len(encoded) <= 1000 and bounded["content_omitted"] is True
    assert bounded["available"] is True and bounded["projection"]["effects_applied"] is True
    assert bounded["projection"]["persisted"] is False and bounded["changes_omitted"] is True
    assert bounded["state_change_count"] == len(result["state_changes"])
    assert bounded["lore_change_count"] == len(result["lore_changes"])
    assert "pending_effects" not in bounded and "consult_rules" in bounded["notice"]
    assert "Synthetic long declaration" not in encoded


@pytest.mark.asyncio
async def test_condition_failure_does_not_replace_or_mutate_live_state(monkeypatch):
    session = make_session()
    state, engine = session.current_state, session.story_tree_engine
    before = copy.deepcopy((state, engine.__dict__))

    def fail(condition, state=None):
        raise RuntimeError("Synthetic condition evaluation failure")

    monkeypatch.setattr(session, "_evaluate_condition", fail)
    with pytest.raises(RuntimeError, match="Synthetic condition"):
        await preview(session)
    assert session.current_state is state and session.story_tree_engine is engine
    assert (state, engine.__dict__) == before


def stream_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [7, 13])
@pytest.mark.parametrize("undo", [False, True])
async def test_stream_reads_declared_choice_then_previews_without_committing_effects(tmp_path, monkeypatch, threshold, undo):
    session = make_session(threshold)
    session.save_id = "synthetic-choice-preview-save"
    narrative = (
        "You remain in the reading room and inspect the empty shelf carefully. "
        "The shelf is firmly attached to the wall, and there is nothing behind it. "
        "You leave everything in place and finish your inspection without disturbing the room."
    )
    requests = []
    operations = [
        ("submit_plan", {"plan": "Inspect the shelf according to the declared rules."}),
        ("consult_rules", {"category": "story_nodes", "query": "test_decision"}),
        ("preview_choice_outcome", {"include_candidates": False}),
        ("update_state", {"end_time": "2030-01-01T09:01:00"}),
        ("preview_choice_outcome", {"include_candidates": True}),
        ("preview_choice_outcome", {"include_candidates": False}),
    ]
    if undo:
        operations.extend([
            ("undo_my_last_action", {"reason": "Defer the proposed update after consulting its effects."}),
            ("preview_choice_outcome", {"include_candidates": True}),
        ])
    operations.extend([
        ("write_narrative", {"text": narrative}),
        ("finalize_turn", {"summary": "Inspected the empty shelf."}),
    ])

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{
                "id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []},
            }]}
        index = len(requests)
        requests.append(copy.deepcopy(messages))
        assert index < len(operations)
        name, original = operations[index]
        args = copy.deepcopy(original)
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        assert session.current_state["script_variables"] == {"test_gate": 0, "test_reward": 0}
        assert session.current_state["story_tree_state"]["choices_made"] == {}
        if name in {"preview_choice_outcome", "update_state"}:
            rules, = [json.loads(message["content"]) for message in messages
                      if message.get("tool_call_id") == "synthetic-preview-1"]
            node = json.loads(rules["entries"][0]["content"])["definition"]["node"]
            choice = node["choices"][0]
            if name == "preview_choice_outcome":
                args.update(node_id=node["id"], choice_id=choice["id"])
            else:
                path, minimum = choice["condition"].split(" >= ")
                args["variable_changes"] = [{"id": path.removeprefix("script_variables."),
                                            "op": "set", "value": int(minimum)}]
        if name == "write_narrative":
            results = {message["tool_call_id"]: message["content"] for message in messages
                       if message.get("role") == "tool"}
            assert json.loads(results["synthetic-preview-2"])["available"] is False
            assert json.loads(results["synthetic-preview-4"])["available"] is True
            assert json.loads(results["synthetic-preview-5"])["available"] is False
            simulated = json.loads(results["synthetic-preview-4"])
            assert simulated["projection"]["effects_applied"] is True
            assert simulated["projection"]["persisted"] is False
            assert {"operation": "replace", "path": ["script_variables", "test_reward"],
                    "before": 0, "after": 4} in simulated["state_changes"]
            assert any(change["path"] == ["test_detail", "enabled"] and change["after"] is True
                       for change in simulated["lore_changes"])
            assert json.loads(results["synthetic-preview-5"])["state_changes"] == []
            if undo:
                assert results["synthetic-preview-6"].startswith("已撤销上一个工具调用（update_state）")
                assert json.loads(results["synthetic-preview-7"])["available"] is False
                assert json.loads(results["synthetic-preview-7"])["lore_changes"] == []
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-preview-{index}", "name": name, "arguments": args,
        }]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "exploration", "scope": "minor", "systems": ["location"],
            "focus_npcs": [], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session.ai_provider = provider
    initial_state = copy.deepcopy(session.current_state)
    root_id = session.world_tree.add_node(None, initial_state["game_time"], 0, state_snapshot=initial_state)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "choice-preview.db")
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    await database.init_db()
    sessions = [session]
    try:
        async with database.get_db() as db:
            await db.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                             (session.script["script_id"], session.script["name"], json.dumps(session.script)))
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)", (session.save_id, session.script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": initial_state, "narrative": narrative})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            action = {"type": "freeform", "text": "Inspect the empty shelf.", "intent_hint": "explicit_action",
                      "stream_id": "synthetic-preview-stream", "action_id": "synthetic-preview-action",
                      "expected_node_id": root_id}
            endpoint = f"/api/game/{session.save_id}/action/stream"
            response = await asyncio.wait_for(client.post(endpoint, json=action), 5)
            assert response.status_code == 200
            events = stream_events(response.text)
            assert not [event for event in events if event["type"] in {"error", "aborted", "agent_max_rounds"}]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            node = session.world_tree.get_node(final["node_id"])
            public_trace = json.dumps(_public_agent_trace(node["agent_trace"]))
            for marker in ("SYNTHETIC_PREVIEW_DETAIL", "SYNTHETIC_PREVIEW_EFFECT", "SYNTHETIC_PREVIEW_CALLBACK"):
                assert marker not in response.text and marker not in public_trace
            assert len(requests) == len(operations)
            replay = await asyncio.wait_for(client.post(endpoint, json=action), 5)
            assert replay.status_code == 200 and len(requests) == len(operations)
        await session._drain_background_tasks()
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        for current in (session, restored):
            assert current.current_state["script_variables"] == {"test_gate": 0 if undo else threshold, "test_reward": 0}
            assert "test_decision" in current.current_state["story_tree_state"]["active"]
            assert current.current_state["story_tree_state"]["choices_made"] == {}
            assert (await preview(current))["available"] is not undo
            assert not next(entry for entry in current.prompt_builder.lorebook.entries if entry.id == "test_detail").enabled
            assert not any(entry.id == "test_added" for entry in current.prompt_builder.lorebook.entries)
            final_id = current.world_tree.active_node_id
            await current.branch_to_node(root_id)
            assert (await preview(current))["available"] is False
            await current.branch_to_node(final_id)
            assert (await preview(current))["available"] is not undo
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
