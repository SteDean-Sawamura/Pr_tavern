import asyncio
import copy
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from api import config_routes, game_routes
from db import database
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader


def make_reveal_scenario(effect_type="add_lore"):
    entry = {
        "id": "test_catalog", "keys": ["蓝色目录"],
        "content": "档案柜的封条已经解除，现在可以查看蓝色目录。",
        "visibility": "hidden", "known_by_npcs": ["test_messenger"],
        "available_from": "2030-01-01T09:05:00",
        "discoverable": {
            "method": "npc_reveal", "npc_source": "test_messenger",
            "attitude_threshold": 60, "reveal_facts": ["蓝色目录"],
        },
    }
    original = copy.deepcopy(entry)
    if effect_type in ("activate_lore", "trigger_activate"):
        original["enabled"] = False
    if effect_type == "update_lore":
        original.update(keys=["黄色索引"], content="黄色索引暂时封存，请等待新的通知。")
    effect = {"type": effect_type, "id": entry["id"]}
    if effect_type == "add_lore":
        effect = {"type": effect_type, "entry": entry}
    elif effect_type == "update_lore":
        effect.update(content=entry["content"], keys=entry["keys"])
    elif effect_type.startswith("trigger_"):
        effect = {"type": "add_trigger", "trigger": {
            "id": "test_switch", "event": "test_delivery",
            "action": "activate_lore" if effect_type == "trigger_activate" else "deactivate_lore",
            "params": {"entry_id": entry["id"]},
        }}
    script = {
        "script_id": "synthetic-lore-reveal", "name": "Synthetic lore reveal", "settings": {},
        "start_time": "2030-01-01T09:00:00",
        "player_character": {"name": "访客", "initial_location": "test_room"},
        "locations": [{"id": "test_room", "name": "阅览室"}],
        "npcs": [{"id": "test_messenger", "name": "传信员", "default_location": "test_room",
                  "attitude_toward_player": 80, "met": True}],
        "lorebook": [] if effect_type == "add_lore" else [original],
        "one_time_events": [{"id": "test_delivery", "trigger_time": "2030-01-01T09:05:00",
                             "effects": [effect]}],
    }
    evidence = "“档案柜的封条已经解除，现在可以查看蓝色目录。”"
    narrative = (
        "你留在阅览室，等传信员把手边的空白纸张放好。窗户仍然关着，桌边没有其他人靠近。"
        f"2030年01月01日09:05，传信员说：{evidence}"
        "你听清这句话，仍坐在原处，没有碰触柜门，也没有翻动桌上的纸张。"
        "他把椅子向后挪了一点，留出桌边的位置，等你决定接下来要问什么。"
    )
    claim = {"entry_id": entry["id"], "evidence": evidence, "occurred_at": "2030-01-01T09:05:00"}
    return script, narrative, claim


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_type", [
    "add_lore", "update_lore", "activate_lore", "deactivate_lore", "remove_lore",
    "trigger_activate", "trigger_deactivate",
])
@pytest.mark.parametrize("time_source", ["accepted", "finalize"])
async def test_reveal_validation_matches_candidate_query_and_settlement(effect_type, time_source):
    script, narrative, claim = make_reveal_scenario(effect_type)
    session = GameSession(copy.deepcopy(script), None)
    session.current_state = ScriptLoader.create_initial_state(script)
    before = copy.deepcopy(session.current_state)
    before_entries = copy.deepcopy(session.prompt_builder.lorebook.entries)
    context = {"old_time": script["start_time"], "present_npc_ids": ["test_messenger"]}
    arguments = {"narrative": narrative, "lore_reveals": [claim]}
    end_time = claim["occurred_at"]
    if time_source == "accepted":
        context["_accepted_state_updates"] = [{"end_time": end_time}]
    else:
        arguments["end_time"] = end_time
    query_context = {**context, "_accepted_state_updates": [{"end_time": end_time}]}
    query = json.loads(await session._query_lorebook_for_turn(query_context, {"entry_id": "test_catalog"}))
    expected = [] if effect_type in ("remove_lore", "deactivate_lore", "trigger_deactivate") else ["test_catalog"]
    assert [entry["id"] for entry in query["entries"]] == expected
    invalid = session._invalid_lore_reveals(context, arguments)
    assert session.current_state == before
    assert session.prompt_builder.lorebook.entries == before_entries
    session._settle_timed_events(script["start_time"], end_time, [])
    settled = session._validated_npc_lore_reveal(
        session.current_state, claim, narrative, context["present_npc_ids"],
        turn_start_time=script["start_time"], turn_end_time=end_time,
    )
    assert bool(settled) is bool(expected)
    assert invalid == ([] if expected else ["test_catalog"])
    session._apply_npc_lore_reveals(
        session.current_state, arguments, context["present_npc_ids"],
        turn_start_time=script["start_time"], turn_end_time=end_time,
    )
    assert session.current_state.get("pc_discovered_lore", []) == expected


@pytest.mark.parametrize("violation", ["absent", "attitude", "quote", "speaker", "early", "future", "timestamp"])
def test_candidate_lore_reveal_preserves_source_and_time_guards(violation):
    script, narrative, claim = make_reveal_scenario()
    session = GameSession(copy.deepcopy(script), None)
    session.current_state = ScriptLoader.create_initial_state(script)
    context = {
        "old_time": script["start_time"], "present_npc_ids": ["test_messenger"],
        "_accepted_state_updates": [{"end_time": claim["occurred_at"]}],
    }
    if violation == "absent":
        context["present_npc_ids"] = []
    elif violation == "attitude":
        session.current_state["npcs"]["test_messenger"]["attitude_toward_player"] = 20
    elif violation == "quote":
        narrative = "你独自留在阅览室，没有听到任何人的说明。"
    elif violation == "speaker":
        narrative = narrative.replace("传信员说", "访客说")
    elif violation == "early":
        claim["occurred_at"] = "2030-01-01T09:04:00"
    elif violation == "future":
        claim["occurred_at"] = "2030-01-01T09:06:00"
    else:
        claim.pop("occurred_at")
    assert session._invalid_lore_reveals(context, {"narrative": narrative, "lore_reveals": [claim]}) == ["test_catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_type", ["add_lore", "update_lore", "activate_lore"])
async def test_event_lore_reveal_reaches_stream_save_and_restored_branch(tmp_path, monkeypatch, effect_type):
    script, narrative, claim = make_reveal_scenario(effect_type)
    requests = []

    async def generate_with_tools(messages, **kwargs):
        if [tool["function"]["name"] for tool in kwargs["tools"]] == ["extract_notes"]:
            return {"content": "", "tool_calls": [
                {"id": "synthetic-notes", "name": "extract_notes", "arguments": {"notes": []}},
            ]}
        index = len(requests)
        requests.append(copy.deepcopy(messages))
        operations = [
            ("submit_plan", {"plan": "Wait for the messenger and listen to the explanation."}),
            ("update_state", {"end_time": claim["occurred_at"]}),
            ("query_lorebook", {"entry_id": "test_catalog"}),
            ("undo_my_last_action", {"reason": "Recheck the candidate before committing."}),
            ("query_lorebook", {"entry_id": "test_catalog"}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"narrative": narrative, "lore_reveals": [claim]}),
            ("update_state", {"end_time": claim["occurred_at"]}),
            ("query_lorebook", {"entry_id": "test_catalog"}),
            ("write_narrative", {"text": narrative}),
            ("finalize_turn", {"summary": "Listened to the messenger.", "lore_reveals": [claim]}),
        ]
        assert index < len(operations), messages[-1]
        name, arguments = operations[index]
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{
            "id": f"synthetic-reveal-{index}", "name": name, "arguments": arguments,
        }]}

    provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=generate_with_tools),
        generate=AsyncMock(return_value=json.dumps({
            "scene_type": "dialogue", "scope": "minor", "systems": ["npc"],
            "focus_npcs": ["test_messenger"], "skill_check": {"needed": False}, "expand_story": False,
            "generate_image": False, "skip_npc_reaction": True, "skip_choices": True,
            "skip_state_settlement": False, "context_depth": "normal", "recall_hints": [],
            "pass": True, "violations": [],
        })),
    )
    session = GameSession(copy.deepcopy(script), provider, save_id="synthetic-reveal-save")
    session.current_state = ScriptLoader.create_initial_state(script)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    root_id = session.world_tree.add_node(
        None, script["start_time"], 0, ai_response="You are in the reading room.",
        state_snapshot=copy.deepcopy(session.current_state),
    )
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reveal-harness.db")
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
            await db.execute("INSERT INTO saves (id, script_id) VALUES (?, ?)",
                             (session.save_id, script["script_id"]))
            await db.commit()
        await game_routes._save_node(session, {
            "node_id": root_id, "state": session.current_state, "narrative": "You are in the reading room.",
        })
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            action = {
                "type": "freeform", "text": "等到九点零五分，听传信员说明。", "intent_hint": "explicit_action",
                "stream_id": "synthetic-reveal-stream", "action_id": "synthetic-reveal-action",
                "expected_node_id": root_id,
            }
            endpoint = f"/api/game/{session.save_id}/action/stream"
            response = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert response.status_code == 200
            assert response.text.endswith("data: [DONE]\n\n")
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert not [event for event in events if event["type"] in ("error", "aborted", "agent_max_rounds")]
            final, = [event for event in events if event["type"] == "final"]
            assert final["narrative"] == narrative
            assert final["state"]["pc_discovered_lore"] == ["test_catalog"]
            received, = [json.loads(message["content"]) for message in requests[9]
                          if message.get("tool_call_id") == "synthetic-reveal-8"]
            assert received["entries"][0]["content"] == "档案柜的封条已经解除，现在可以查看蓝色目录。"
            assert received["projection"]["event_ids"] == ["test_delivery"]
            revoked, = [json.loads(message["content"]) for message in requests[5]
                         if message.get("tool_call_id") == "synthetic-reveal-4"]
            assert revoked["entries"] == []
            undo_result, = [message["content"] for message in requests[4]
                            if message.get("tool_call_id") == "synthetic-reveal-3"]
            assert undo_result.startswith("已撤销上一个工具调用（update_state）")
            rejected, = [json.loads(message["content"]) for message in requests[7]
                          if message.get("tool_call_id") == "synthetic-reveal-6"]
            assert "词条揭露未通过叙事/来源核验: test_catalog" in rejected["error"]
            model_calls = provider.generate_with_tools.await_count
            replay = await asyncio.wait_for(client.post(endpoint, json=action), timeout=5)
            assert replay.text == response.text
            assert provider.generate_with_tools.await_count == model_calls
        await session._drain_background_tasks()
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None and restored is not session
        sessions.append(restored)
        assert restored.current_state["pc_discovered_lore"] == ["test_catalog"]
        final_node_id = restored.world_tree.active_node_id
        await restored.branch_to_node(root_id)
        assert "test_catalog" not in restored.current_state.get("pc_discovered_lore", [])
        assert restored._invalid_lore_reveals(
            {"old_time": script["start_time"], "present_npc_ids": ["test_messenger"]},
            {"narrative": narrative, "lore_reveals": [claim]},
        ) == ["test_catalog"]
        await restored.branch_to_node(final_node_id)
        assert restored.current_state["pc_discovered_lore"] == ["test_catalog"]
        async with database.get_db() as db:
            row = await (await db.execute("SELECT COUNT(*) FROM tree_nodes WHERE save_id = ?", (session.save_id,))).fetchone()
        assert row[0] == 2
    finally:
        for current in sessions:
            await current._drain_background_tasks()
        await database.close_pool()
