import asyncio
from contextlib import asynccontextmanager
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api import game_routes
from db import database
from engine.game_session import GameSession
from test_agent_harness_execution import _stream_events
from test_actor_goal_settlement import install_agent, result_from
from test_director_goal_persistence import goal_client, make_session, updates
from test_story_choice_settlement import stored_state


def chain_session(reverse=True, split=False, dynamic=False, length=4):
    original, script = make_session(dynamic)
    if split:
        script["organizations"].append({"id": "test_archive", "name": "Synthetic archive", "goals": []})
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["script_variables"].update(test_gate=0, test_reward=0)
    session.current_state["player"]["gold"] = 10
    session._index_dynamic_npcs_from_state(session.current_state)
    session._ensure_identity_lore()
    declaration = updates("Observe the catalog update.")
    declaration["npc_goals"][0]["goals"][0].update(
        condition_met=f"script_variables.test_reward >= {length}",
        rule_notes="SYNTHETIC_CHAIN_PRIVATE_RULE",
    )
    groups = {org["id"]: [] for org in script["organizations"]}
    keys = []
    for index in range(length):
        org_id = "test_archive" if split and index % 2 else "test_circle"
        goal_id = f"test_step_{index}"
        keys.append(f"{org_id}:{goal_id}")
        groups[org_id].append({
            "id": goal_id, "description": f"Record catalog step {index}.",
            "condition_met": "script_variables.test_gate >= 9" if index == 0
                             else f"script_variables.test_reward >= {index}",
            "rule_notes": "SYNTHETIC_CHAIN_PRIVATE_RULE",
            "on_complete_effects": {"state_change": [
                {"target": "script_variables.test_reward", "op": "set", "value": index + 1},
                {"target": "player.gold", "op": "add", "value": 3},
            ]},
        })
    declaration["org_goals"] = [
        {"org_id": org_id, "goals": list(reversed(goals)) if reverse else goals}
        for org_id, goals in groups.items()
    ]
    session._apply_director_results({"goals": declaration})
    return session, script, keys


def assert_chain(state, keys, completed=True):
    assert state["script_variables"]["test_reward"] == (len(keys) if completed else 0)
    assert state["player"]["gold"] == 10 + (3 * len(keys) if completed else 0)
    records = state.get("completed_org_goals", [])
    assert [record["id"] for record in records] == (keys if completed else [])
    npc_records = state.get("completed_npc_goals", [])
    assert [record["id"] for record in npc_records] == (["test_reader:test_new_goal"] if completed else [])
    if completed:
        assert all(record["turn"] == 1 for record in records + npc_records)
    count = len(keys) + 1 if completed else 0
    assert len([entry for entry in state.get("information_network", [])
                if {"npc_goal", "org_goal"}.intersection(entry.get("tags", []))]) == min(count, 30)
    assert len([entry for entry in state.get("narrative_callbacks", [])
                if {"npc_goal", "org_goal"}.intersection(entry.get("tags", []))]) == count


def assert_rewards(changes, count):
    rewards = [change for change in changes if change.get("target") == "player.gold"]
    assert [(change["old"], change["new"]) for change in rewards] == [
        (10 + 3 * index, 13 + 3 * index) for index in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse, split, dynamic, stream", [
    (False, False, False, False), (True, False, True, True),
    (False, True, True, False), (True, True, False, True),
])
async def test_goal_chain_settles_in_one_turn_and_survives_reload(
    tmp_path, monkeypatch, reverse, split, dynamic, stream,
):
    session, script, keys = chain_session(reverse, split, dynamic)
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        endpoint = f"/api/game/{session.save_id}/action" + ("/stream" if stream else "")
        action = {"type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-chain",
                  "expected_node_id": root}
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        result = result_from(response, stream)
        assert_chain(session.current_state, keys)
        assert_rewards(result["state_changes"], len(keys))
        node = session.world_tree.get_node(result["node_id"])
        assert_chain(node["state_snapshot"], keys)
        assert node["_state_hash"] == session.world_tree.state_hash(node["state_snapshot"])
        assert_rewards(node["state_changes"], len(keys))
        assert_chain(await stored_state(session), keys)
        async with database.get_db() as db:
            cursor = await db.execute("SELECT state_changes FROM tree_nodes WHERE id = ?", (result["node_id"],))
            assert_rewards(json.loads((await cursor.fetchone())["state_changes"]), len(keys))
        if stream:
            calls = session.ai_provider.generate_with_tools.await_count
            assert (await client.post(endpoint, json=action)).text == response.text
            assert session.ai_provider.generate_with_tools.await_count == calls
            assert_chain(await stored_state(session), keys)
        trace = await game_routes.get_tree_node(session.save_id, result["node_id"])
        assert "SYNTHETIC_CHAIN_PRIVATE_RULE" not in response.text + json.dumps(trace)
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        assert_chain(restored.current_state, keys)
        await restored.branch_to_node(root)
        assert_chain(restored.current_state, keys, False)
        await restored.branch_to_node(result["node_id"])
        assert_chain(restored.current_state, keys)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["update", "prepare", "after_ai"])
async def test_long_chain_does_not_repeat_lifecycle_or_impending_consequences(tmp_path, monkeypatch, source):
    session, script, keys = chain_session(length=36 if source == "update" else 6)
    session.current_state["npc_goal_progress"] = {"test_reader": {"progress": 80}}
    if source == "prepare":
        session.current_state["script_variables"]["test_gate"] = 9
    if source == "after_ai":
        session._apply_trigger_updates({"add": [{
            "id": "test_chain_hook", "event": "after_ai", "action": "set_var",
            "params": {"var_id": "test_gate", "op": "set", "value": 9},
        }]})
    lifecycle = Mock(wraps=session._fire_lifecycle_event)
    consequence = Mock(wraps=session.event_engine.add_consequence)
    monkeypatch.setattr(session, "_fire_lifecycle_event", lifecycle)
    monkeypatch.setattr(session.event_engine, "add_consequence", consequence)
    install_agent(session, 9 if source == "update" else 0)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-long-chain",
        }), 15)
        result = result_from(response)
        assert_chain(session.current_state, keys)
        assert_chain(await stored_state(session), keys)
        assert_rewards(result["state_changes"], len(keys))
        assert len([call for call in lifecycle.call_args_list if call.args[0] == "after_ai"]) == 1
        assert len([call for call in consequence.call_args_list
                    if call.args[1]["id"] == "_npc_goal_imm_test_reader"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [False, True])
async def test_cyclic_goals_need_a_seed_and_pay_each_goal_once(tmp_path, monkeypatch, seeded):
    session, script, keys = chain_session(length=2)
    goals = {goal["id"]: goal for goal in session.script["organizations"][0]["goals"]}
    goals["test_step_0"]["condition_met"] = "script_variables.test_reward >= 1"
    goals["test_step_0"]["on_complete_effects"]["state_change"][0]["value"] = 2
    goals["test_step_0"]["on_complete_effects"]["state_change"].append(
        {"target": "script_variables.test_gate", "op": "add", "value": 1},
    )
    goals["test_step_1"]["condition_met"] = "script_variables.test_gate >= 9"
    goals["test_step_1"]["on_complete_effects"]["state_change"][0]["value"] = 1
    session.current_state["dynamic_story_content"]["goal_overrides"]["organizations"]["test_circle"] = (
        copy.deepcopy(list(goals.values()))
    )
    install_agent(session, 9 if seeded else 0)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        for index in range(2):
            response = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
                "type": "freeform", "text": "Inspect the catalog.", "action_id": f"synthetic-cycle-{index}",
            }), 15)
            result = result_from(response)
            assert_chain(session.current_state, list(reversed(keys)), seeded)
            assert_chain(await stored_state(session), list(reversed(keys)), seeded)
            assert_rewards(result["state_changes"], len(keys) if seeded and index == 0 else 0)


@pytest.mark.asyncio
async def test_rewards_cannot_erase_settlement_completion_records(tmp_path, monkeypatch):
    session, script, keys = chain_session()
    npc_goals = session._npc_by_id["test_reader"]["goals"]
    npc_goals[-1]["condition_met"] = "script_variables.test_gate >= 9"
    overrides = session.current_state["dynamic_story_content"]["goal_overrides"]
    overrides["npcs"]["test_reader"] = copy.deepcopy(npc_goals)
    for goal in session.script["organizations"][0]["goals"]:
        if "on_complete_effects" in goal:
            goal["on_complete_effects"]["state_change"].extend([
                {"target": "completed_npc_goals", "op": "set", "value": []},
                {"target": "completed_org_goals", "op": "set", "value": []},
            ])
    overrides["organizations"]["test_circle"] = copy.deepcopy(session.script["organizations"][0]["goals"])
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        for index in range(2):
            result = result_from(await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/action/stream", json={
                "type": "freeform", "text": "Inspect the catalog.", "action_id": f"synthetic-ledger-{index}",
            }), 15))
            assert_chain(session.current_state, keys)
            assert_chain(await stored_state(session), keys)
            assert_rewards(result["state_changes"], len(keys) if index == 0 else 0)


@pytest.mark.asyncio
async def test_legacy_completed_ids_are_not_awarded_again(tmp_path, monkeypatch):
    session, script, keys = chain_session()
    session.current_state["completed_org_goals"] = list(keys)
    session.current_state["completed_npc_goals"] = ["test_reader:test_new_goal"]
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        result = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-legacy-chain",
        }))
        state = await stored_state(session)
        assert state["completed_org_goals"] == keys
        assert state["completed_npc_goals"] == ["test_reader:test_new_goal"]
        assert state["player"]["gold"] == 10 and state["script_variables"]["test_reward"] == 0
        assert_rewards(result["state_changes"], 0)


@pytest.mark.asyncio
async def test_non_state_change_reward_unlocks_a_followup_goal(tmp_path, monkeypatch):
    session, script, keys = chain_session(length=2)
    goals = {goal["id"]: goal for goal in session.script["organizations"][0]["goals"]}
    goals["test_step_0"]["on_complete_effects"] = {
        "set_reputation": [{"faction": "test_circle", "value": 60}],
        "unlock_locations": ["test_entrance"],
    }
    goals["test_step_1"]["condition_met"] = "faction_reputation.test_circle.value >= 60"
    session.current_state["dynamic_story_content"]["goal_overrides"]["organizations"]["test_circle"] = (
        copy.deepcopy(list(goals.values()))
    )
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        result = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-reputation-chain",
        }))
        state = await stored_state(session)
        assert [record["id"] for record in state["completed_org_goals"]] == keys
        assert [record["id"] for record in state["completed_npc_goals"]] == ["test_reader:test_new_goal"]
        assert state["script_variables"]["test_reward"] == 2 and state["player"]["gold"] == 13
        assert state["faction_reputation"]["test_circle"]["value"] == 60
        assert "test_entrance" in state["visible_locations"]
        assert_rewards(result["state_changes"], 1)


@pytest.mark.asyncio
async def test_reward_introduced_npc_can_finish_via_offscreen_progress(tmp_path, monkeypatch):
    session, script, keys = chain_session(length=2)
    goals = session.script["organizations"][0]["goals"]
    last = next(goal for goal in goals if goal["id"] == "test_step_1")
    last["on_complete_effects"]["state_change"].extend([
        {"target": "npcs.test_visitor", "op": "set", "value": {
            "name": "Synthetic visitor", "known": False,
            "goals": [{"id": "test_arrive", "description": "Reach the reading room."}],
        }},
        {"target": "npc_goal_progress.test_visitor", "op": "set", "value": {"progress": 100}},
    ])
    session.current_state["dynamic_story_content"]["goal_overrides"]["organizations"]["test_circle"] = (
        copy.deepcopy(goals)
    )
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-new-npc-chain",
        }))
        state = await stored_state(session)
        assert [record["id"] for record in state["completed_org_goals"]] == keys
        completed = state["completed_npc_goals"]
        assert [record["id"] for record in completed] == ["test_reader:test_new_goal", "test_visitor:test_arrive"]
        assert [record["source"] for record in completed] == ["condition", "offscreen"]
        assert state["player"]["gold"] == 16 and state["script_variables"]["test_reward"] == 2
        assert len([entry for entry in state["information_network"] if "npc_goal" in entry["tags"]]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("regenerate", [False, True])
async def test_chain_commit_failure_rolls_back_and_retries_once(tmp_path, monkeypatch, stream, regenerate):
    session, script, keys = chain_session(split=True, dynamic=True)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, _):
        if regenerate:
            install_agent(session, 0)
            result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
                "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-chain-before-regen",
            }))
        install_agent(session, 9)
        before = copy.deepcopy(session.current_state)
        stored = await stored_state(session)
        tree = copy.deepcopy(session.world_tree.nodes)
        active = session.world_tree.active_node_id
        turn = session.turn_number
        attempts = []

        @asynccontextmanager
        async def fail_commit():
            async with database.get_db() as db:
                writes_turn = False

                async def execute(sql, *args):
                    nonlocal writes_turn
                    writes_turn |= (sql.lstrip().startswith("INSERT INTO tree_nodes")
                                    or (sql.lstrip().startswith("UPDATE tree_nodes") and "ai_response" in sql))
                    return await db.execute(sql, *args)

                async def commit():
                    if not writes_turn:
                        return await db.commit()
                    assert_chain(session.current_state, keys)
                    attempts.append(True)
                    raise RuntimeError("Synthetic goal chain commit failure")
                yield SimpleNamespace(execute=execute, commit=commit)

        endpoint = f"/api/game/{session.save_id}/" + ("regenerate" if regenerate else "action")
        endpoint += ("/stream" if stream else "") + ("?stage=all" if regenerate else "")
        action = {"type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-chain-retry",
                  "expected_node_id": active}
        with monkeypatch.context() as patch:
            patch.setattr(game_routes, "get_db", fail_commit)
            response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        if stream:
            events = _stream_events(response.text)
            assert any(event["type"] == "error" for event in events), response.text
            assert "data: [DONE]" not in response.text
            assert not any(event["type"] == "final" for event in events)
        else:
            assert response.status_code == 500
        assert attempts == [True]
        assert session.current_state == before and session.turn_number == turn
        assert session.world_tree.nodes == tree and session.world_tree.active_node_id == active
        assert await stored_state(session) == stored
        async with database.get_db() as db:
            cursor = await db.execute("SELECT id FROM tree_nodes WHERE save_id = ?", (session.save_id,))
            assert {row["id"] for row in await cursor.fetchall()} == set(tree)
        response = await asyncio.wait_for(client.post(endpoint, json=action), 15)
        assert not result_from(response, stream).get("error"), response.text
        assert_chain(session.current_state, keys)
        assert_chain(await stored_state(session), keys)
        assert_rewards(session.world_tree.get_node(session.world_tree.active_node_id)["state_changes"], len(keys))


def install_state_agent(session, value):
    arguments = {"end_time": "2030-01-01T09:01:00",
                 "state_changes": [{"target": "script_variables.test_gate", "op": "set", "value": value}]}

    async def respond(messages, **kwargs):
        updated = any(message.get("tool_call_id") == "synthetic-chain-update" for message in messages)
        name = "finalize_turn" if updated else "update_state"
        assert name in [tool["function"]["name"] for tool in kwargs["tools"]]
        return {"content": "", "tool_calls": [{
            "id": "synthetic-chain-finish" if updated else "synthetic-chain-update", "name": name,
            "arguments": {"summary": "Inspected the catalog."} if updated else arguments,
        }]}

    session.ai_provider = SimpleNamespace(
        generate_with_tools=AsyncMock(side_effect=respond),
        generate=AsyncMock(return_value=json.dumps(arguments)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("stage", ["all", "state"])
@pytest.mark.parametrize("dynamic", [False, True])
async def test_regenerated_chain_follows_swipe_and_reload(tmp_path, monkeypatch, stream, stage, dynamic):
    session, script, keys = chain_session(split=True, dynamic=dynamic)
    install_agent(session, 9)
    async with goal_client(session, script, tmp_path, monkeypatch) as (client, sessions):
        original = result_from(await client.post(f"/api/game/{session.save_id}/action/stream", json={
            "type": "freeform", "text": "Inspect the catalog.", "action_id": "synthetic-chain-versions",
        }))
        assert_chain(await stored_state(session), keys)
        for value, completed in [(0, False), (9, True)]:
            if stage == "all":
                install_agent(session, value)
            else:
                install_state_agent(session, value)
            endpoint = f"/api/game/{session.save_id}/regenerate" + ("/stream" if stream else "")
            response = await asyncio.wait_for(client.post(endpoint + f"?stage={stage}"), 15)
            assert not result_from(response, stream).get("error"), response.text
            assert_chain(session.current_state, keys, completed)
            assert_chain(await stored_state(session), keys, completed)
            node = session.world_tree.get_node(original["node_id"])
            assert_rewards(node["state_changes"], len(keys) if completed else 0)
            assert "SYNTHETIC_CHAIN_PRIVATE_RULE" not in response.text
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        assert restored is not None
        sessions.append(restored)
        for index, completed in [(0, True), (1, False), (2, True), (0, True)]:
            response = await client.post(f"/api/game/{session.save_id}/swipe/jump?index={index}")
            assert response.status_code == 200, response.text
            assert_chain(restored.current_state, keys, completed)
            assert_chain(await stored_state(restored), keys, completed)
            assert restored.world_tree.active_node_id == original["node_id"]
