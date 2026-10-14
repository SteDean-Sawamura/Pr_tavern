import asyncio
from collections import OrderedDict
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from test_agent_harness_execution import _stream_events


RAW = (
    "You read the SYNTHETIC_SIGN_CODE beside an empty cabinet. "
    "The cabinet stands against a plain wall and contains no objects. "
    "You inspect the wooden edges without moving anything, then step away and leave the room undisturbed."
)
VOICE_RAW = RAW.replace("cabinet", "display")
CASES = [
    ("none", {}, RAW),
    ("post", {"post_processing_rules": [{"find": "SYNTHETIC_SIGN_CODE", "replace": "plain notice"}]},
     RAW.replace("SYNTHETIC_SIGN_CODE", "plain notice")),
    ("regex", {"regex_scripts": [{"find": "SYNTHETIC_SIGN_CODE", "replace": "plain notice", "placement": "ai_output"}]},
     RAW.replace("SYNTHETIC_SIGN_CODE", "plain notice")),
    ("disabled_post", {"post_processing_rules": [{"find": "SYNTHETIC_SIGN_CODE", "replace": "plain notice", "enabled": False}]}, RAW),
    ("disabled_regex", {"regex_scripts": [{"find": "SYNTHETIC_SIGN_CODE", "replace": "plain notice", "enabled": False}]}, RAW),
    ("non_idempotent_post", {"post_processing_rules": [{"find": "room", "replace": "room annotation"}]},
     RAW.replace("room", "room annotation")),
    ("non_idempotent_regex", {"regex_scripts": [{"find": "room", "replace": "room annotation", "placement": "ai_output"}]},
     RAW.replace("room", "room annotation")),
    ("ordered", {"post_processing_rules": [{"find": "SYNTHETIC_SIGN_CODE", "replace": "SYNTHETIC_PUBLIC_LABEL"}],
                 "regex_scripts": [{"find": "SYNTHETIC_PUBLIC_LABEL", "replace": "plain notice", "placement": "ai_output"}]},
     RAW.replace("SYNTHETIC_SIGN_CODE", "plain notice")),
    ("voice_erased", {"post_processing_rules": [{"find": ".*display.*", "replace": ""}]}, RAW),
]


def declared_script(declarations):
    return {
        "script_id": "synthetic-output-rules", "name": "Synthetic output rules", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "Visitor", "initial_location": "synthetic_room"},
        "locations": [{"id": "synthetic_room", "name": "Empty room"}], "story_tree": {},
        **copy.deepcopy(declarations),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case,declarations,expected,mode", [
    pytest.param(case, declarations, expected, mode, id=f"{case}-{mode}")
    for case, declarations, expected in CASES
    for mode in ("action", "action_json", "voice", "regenerate", "regenerate_stream", "delegated")
    if not (case == "voice_erased" and mode == "delegated")
])
async def test_declared_output_matches_review_stream_final_and_swipes(tmp_path, monkeypatch, mode, case, declarations, expected):
    script = declared_script(declarations)
    operations = [
        ("submit_plan", {"plan": "Inspect the empty cabinet without moving anything."}),
        ("write_narrative", {"text": RAW}),
        ("delegate_settlement" if mode == "delegated" else "finalize_turn",
         {"summary": "Inspected an empty cabinet.", "narrative": RAW}),
    ]
    step = 0
    background_step = 0

    async def generate_with_tools(messages, **kwargs):
        nonlocal step, background_step
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [{"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}}]}
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        if "submit_plan" not in names and "set_turn_meta" in names:
            assert mode == "delegated"
            background_operations = [
                ("write_narrative", {"text": VOICE_RAW}),
                ("update_state", {"end_time": "2030-01-01T09:01:00"}),
                ("set_turn_meta", {"summary": "Inspected an empty display."}),
            ]
            background_step += 1
            if background_step == 4:
                return {"content": "完成。", "tool_calls": []}
            assert background_step <= len(background_operations)
            name, arguments = background_operations[background_step - 1]
            return {"content": "", "tool_calls": [{"id": f"synthetic-background-{background_step}", "name": name, "arguments": arguments}]}
        assert step < len(operations)
        name, arguments = operations[step]
        step += 1
        return {"content": "", "tool_calls": [{"id": f"synthetic-tool-{step}", "name": name, "arguments": copy.deepcopy(arguments)}]}

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
    session = GameSession(copy.deepcopy(script), provider, save_id="synthetic-output-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    root_id = session.world_tree.add_node(None, script["start_time"], 0,
        ai_response="You enter an empty room.", state_snapshot=copy.deepcopy(session.current_state))
    reviewed = []
    original_review = session._review_narrative

    async def observe_review(narrative, *args, **kwargs):
        reviewed.append(narrative)
        return await original_review(narrative, *args, **kwargs)

    monkeypatch.setattr(session, "_review_narrative", observe_review)
    if mode == "voice":
        monkeypatch.setattr(session, "_maybe_fix_npc_voices", AsyncMock(return_value=VOICE_RAW))
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "synthetic-output.db")
    monkeypatch.setattr(database, "_pool", None)
    monkeypatch.setattr(database, "_pool_lock", None)
    monkeypatch.setattr(database, "_pool_created", 0)
    monkeypatch.setattr(game_routes, "_sessions", OrderedDict([(session.save_id, session)]))
    monkeypatch.setattr(game_routes, "_session_locks", {})
    monkeypatch.setattr(game_routes, "_action_streams", {})
    monkeypatch.setattr(game_routes, "_get_ai_provider_async", AsyncMock(return_value=provider))
    monkeypatch.setattr(game_routes, "_get_stage_models_async", AsyncMock(return_value={}))
    monkeypatch.setattr(config_routes, "get_image_provider_instance", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(game_routes.router, prefix="/api/game")
    await database.init_db()
    try:
        async with database.get_db() as connection:
            await connection.execute("INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)",
                                     (script["script_id"], script["name"], json.dumps(script)))
            await connection.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                                     (session.save_id, script["script_id"]))
            await connection.commit()
        await game_routes._save_node(session, {"node_id": root_id, "state": copy.deepcopy(session.current_state),
                                              "narrative": "You enter an empty room."})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            action_suffix = "" if mode == "action_json" else "/stream"
            response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action{action_suffix}", json={
                "type": "freeform", "text": "Inspect the empty cabinet.", "intent_hint": "explicit_action",
                "stream_id": "synthetic-stream", "action_id": "synthetic-action", "expected_node_id": root_id,
            }), timeout=10)
            initial_node = None
            for attempt in range(3 if mode.startswith("regenerate") else 1):
                if attempt:
                    step = 0
                    reviewed.clear()
                    suffix = "/stream" if mode == "regenerate_stream" else ""
                    response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate{suffix}?stage=all"), timeout=15)
                assert response.status_code == 200, response.text
                assert "_unified_narrative_output" not in response.text
                streaming = (not attempt and mode != "action_json") or mode == "regenerate_stream"
                events = _stream_events(response.text) if streaming else []
                if mode == "voice" and case == "voice_erased":
                    assert any(event["type"] == "error" for event in events)
                    assert not any(event["type"] == "final" for event in events)
                    assert reviewed == [RAW]
                    await game_routes._persist_sessions_on_shutdown()
                    async with database.get_db() as connection:
                        assert (await (await connection.execute("SELECT count(*) FROM tree_nodes")).fetchone())[0] == 1
                        assert (await (await connection.execute("SELECT count(*) FROM action_requests")).fetchone())[0] == 0
                    continue
                assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
                if streaming:
                    result, = [event for event in events if event["type"] == "final"]
                else:
                    result = response.json()
                revised = mode in ("voice", "delegated")
                final_text = expected.replace("cabinet", "display") if revised else expected
                assert result["narrative"] == final_text
                assert reviewed == ([expected, final_text] if revised else [expected])
                if mode == "delegated":
                    assert background_step == 4
                streamed = [event["content"] for event in events if event["type"] in ("text", "narrative_revised")]
                if streaming:
                    assert streamed and streamed[-1] == final_text
                    assert all(text in (expected, final_text) for text in streamed)
                initial_node = initial_node or result["node_id"]
                assert result["node_id"] == initial_node
                await game_routes._persist_sessions_on_shutdown()
                async with database.get_db() as connection:
                    stored, snapshot = await (await connection.execute(
                        "SELECT ai_response, state_snapshot FROM tree_nodes WHERE id=?", (initial_node,),
                    )).fetchone()
                    assert stored == final_text
                    assert (await (await connection.execute("SELECT count(*) FROM tree_nodes")).fetchone())[0] == 2
                    if attempt:
                        swipes = json.loads(snapshot)["_node_context"]["swipes"]
                        assert [swipe["narrative"] for swipe in swipes] == [expected] * (attempt + 1)
                    stored_script = json.loads((await (await connection.execute("SELECT content FROM scripts")).fetchone())[0])
                    assert stored_script == script
    finally:
        await game_routes._persist_sessions_on_shutdown()
        await database.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_rules_cannot_turn_a_submitted_narrative_into_an_empty_candidate(delegated):
    session = GameSession(declared_script({"post_processing_rules": [{"find": ".+", "replace": ""}]}), None)
    context = {"_unified_narrative_output": True}
    raw = (session._dispatch_settlement_tool("write_narrative", {"text": RAW}, context) if delegated
           else await session._dispatch_unified_tool(context, "write_narrative", {"text": RAW}))
    result = json.loads(raw)
    assert result["error"] == "叙事不能为空"
    assert "_finalize_narrative" not in context


@pytest.mark.parametrize("unified", [False, True])
def test_delegated_rewrite_respects_the_narrative_processing_owner(unified):
    session = GameSession(declared_script({"regex_scripts": [{"find": "room", "replace": "room annotation"}]}), None)
    context = {"_unified_narrative_output": unified}
    for attempt in range(2):
        text = context["_finalize_narrative"] if attempt else RAW
        result = session._dispatch_settlement_tool("write_narrative", {"text": text}, context)
        assert "error" not in result
        assert context["_finalize_narrative"] == (RAW.replace("room", "room annotation") if unified else RAW)


@pytest.mark.asyncio
async def test_resubmitting_raw_or_prepared_draft_does_not_repeat_replacement():
    session = GameSession(declared_script({"post_processing_rules": [{"find": "room", "replace": "room annotation"}]}), None)
    context = {}
    expected = RAW.replace("room", "room annotation")
    for text in (RAW, expected, RAW):
        result = await session._dispatch_unified_tool(context, "write_narrative", {"text": text})
        assert "error" not in result
        assert context["_finalize_narrative"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_declared_replacement_does_not_mutate_reused_model_arguments(delegated):
    session = GameSession(declared_script({"post_processing_rules": [{"find": "room", "replace": "room annotation"}]}), None)
    arguments = {"text": RAW}
    for attempt in range(3):
        context = {"_unified_narrative_output": True}
        result = (session._dispatch_settlement_tool("write_narrative", arguments, context) if delegated
                  else await session._dispatch_unified_tool(context, "write_narrative", arguments))
        assert "error" not in result
        assert arguments == {"text": RAW}
        assert context["_finalize_narrative"] == RAW.replace("room", "room annotation")
