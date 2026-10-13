import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_generation_baseline import assert_effects, restore
from test_partial_player_input_replay import outcome, recorded_inputs
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_turn_player_input_replay import PRIVATE_PLAN, context_block
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages, stage_session


LIVE_MESSAGES = ("Use the window for this synthetic inspection.",
                 "Use the bench instead of the window for this synthetic inspection.")


def live_outcome(entries):
    value, _ = outcome(entries)
    procedure, position = value.split("/")
    for entry in entries:
        if entry["message"] in LIVE_MESSAGES:
            position = ("window", "bench")[LIVE_MESSAGES.index(entry["message"])]
    choices = [{**choice, "text": f"{procedure} practice shelf {index} from the {position}."}
               for index, choice in enumerate(CHOICES)]
    return f"{procedure}/{position}", choices


def install_live_inputs(session, stage, ending=None):
    provider = install_stages(session, "native", "minor", "exploration")
    provider.waiting = [asyncio.Event(), asyncio.Event()]
    provider.consumed = asyncio.Event()
    provider.replay_inputs = []
    prefixes = {group: session.prompt_builder._build_world_state_system(group)
                for group in ("resource", "spatial", "temporal", "world", "ext")}
    prefixes["npc"] = PromptLoader.get().render_system("npc_reaction")

    def received(messages):
        entries = copy.deepcopy(context_block(messages, "turn_player_inputs") or [])
        fresh = [message["content"] for message in messages
                 if message["role"] == "user" and message.get("content") in LIVE_MESSAGES]
        entries.extend({"message": message} for message in fresh)
        if ending is None:
            provider.replay_inputs.append(copy.deepcopy(entries))
        return entries, fresh

    async def generate(messages, **kwargs):
        entries, _ = received(messages)
        value, choices = live_outcome(entries)
        if stage == "choices":
            return json.dumps({"choices": choices})
        group = next(group for group, prefix in prefixes.items() if kwargs["system"].startswith(prefix))
        if group == "world":
            return json.dumps({"world_property_changes": [{"id": "test_inspection_status", "value": value}]})
        return json.dumps({"end_time": "2030-01-01T09:01:00"} if group == "temporal" else {})

    async def respond(messages, **kwargs):
        entries, fresh = received(messages)
        if ending is not None and len(fresh) < len(LIVE_MESSAGES):
            provider.waiting[len(fresh)].set()
            await asyncio.Event().wait()
        if ending == "unconsumed":
            raise RuntimeError("SYNTHETIC_UNCONSUMED_PROVIDER_FAILURE")
        completed = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
        value, choices = live_outcome(entries)
        if not completed:
            name, arguments = "peek_upcoming_events", {}
        else:
            provider.consumed.set()
            if ending in {"abort", "cancel"}:
                await asyncio.Event().wait()
            if ending == "provider_error":
                raise RuntimeError("SYNTHETIC_CONSUMED_PROVIDER_FAILURE")
            if stage == "state" and "synthetic-live-update_state" not in completed:
                name, arguments = "update_state", {
                    "end_time": "2030-01-01T09:01:00",
                    "world_property_changes": [{"id": "test_inspection_status", "value": value}],
                }
            else:
                name, arguments = "finalize_turn", {"summary": "Recorded the synthetic inspection."}
                if stage == "choices":
                    arguments["choices"] = choices
        assert name in {tool["function"]["name"] for tool in kwargs["tools"]}
        return {"content": "", "tool_calls": [{"id": f"synthetic-live-{name}", "name": name, "arguments": arguments}]}

    provider.generate = AsyncMock(side_effect=generate)
    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["state", "choices"])
@pytest.mark.parametrize("prior_inputs", [False, True])
@pytest.mark.parametrize("ending", ["success", "abort", "cancel", "provider_error", "save_error", "unconsumed"])
async def test_live_partial_inputs_commit_only_with_successful_swipe(tmp_path, monkeypatch, stage, prior_inputs, ending):
    session = stage_session("native", False)
    pending = None
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_stages(session, "native", "minor", "exploration")
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        await session._drain_background_tasks()
        node_id = initial["node_id"]
        original = recorded_inputs("both") if prior_inputs else []
        if original:
            session.world_tree.get_node(node_id)["_turn_player_inputs"] = copy.deepcopy(original)
        await game_routes._update_node_in_db(session, initial)
        session = await restore(session, sessions)
        provider = install_live_inputs(session, stage, ending)
        before, stored = runtime_state(session), await database_state(session)
        stream_id = "synthetic-live-partial"
        response, events = None, []
        try:
            with monkeypatch.context() as patch:
                if ending == "save_error":
                    patch.setattr(game_routes, "_update_node_in_db", AsyncMock(side_effect=RuntimeError("SYNTHETIC_SAVE_FAILURE")))
                pending = asyncio.create_task(client.post(endpoint(session, "regenerate", True), params={
                    "stage": stage, "stream_id": stream_id,
                }))
                for index, message in enumerate(LIVE_MESSAGES):
                    await asyncio.wait_for(provider.waiting[index].wait(), 8)
                    assert not pending.done()
                    injected = await client.post(f"/api/game/{session.save_id}/inject", json={
                        "stream_id": stream_id, "message": message,
                    })
                    assert injected.status_code == 200 and injected.json() == {"queued": True}
                if ending in {"abort", "cancel"}:
                    await asyncio.wait_for(provider.consumed.wait(), 8)
                    if ending == "cancel":
                        pending.cancel()
                    else:
                        aborted = await client.post(f"/api/game/{session.save_id}/abort", json={"stream_id": stream_id})
                        assert aborted.status_code == 200 and aborted.json() == {"requested": True}
                if ending == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    response = await asyncio.wait_for(pending, 20)
                    assert response.status_code == 200, response.text
                    events = _stream_events(response.text)
                    consumed = [event["message"] for event in events if event["type"] == "user_inject"]
                    assert consumed == ([] if ending == "unconsumed" else list(LIVE_MESSAGES))
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert_controls_closed(session)
        if response is not None:
            assert "_turn_player_inputs" not in response.text and PRIVATE_PLAN not in response.text
        if ending == "success":
            regenerated = result_from(response)
            expected = original + [{"message": message} for message in LIVE_MESSAGES]
            node = session.world_tree.get_node(node_id)
            assert node.get("_turn_player_inputs", []) == expected
            assert node["swipes"][0].get("_turn_player_inputs", []) == original
            assert node["swipes"][1]["_turn_player_inputs"] == expected
            assert (await stored_state(session))["_node_context"]["_turn_player_inputs"] == expected
            assert regenerated["swipe_index"] == 1 and regenerated["total_swipes"] == 2
            assert regenerated["narrative"] == initial["narrative"]
            value, choices = live_outcome(expected)
            if stage == "state":
                assert regenerated["state"]["world_properties"]["test_inspection_status"] == value
                assert regenerated["choices"] == initial["choices"]
            else:
                assert [choice["text"] for choice in regenerated["choices"]] == [choice["text"] for choice in choices]
                assert regenerated["state"]["world_properties"] == initial["state"]["world_properties"]
            versions = ((0, original), (1, expected), (0, original))
        else:
            assert not any(event["type"] == "final" for event in events)
            if ending != "cancel":
                assert any(event["type"] == ("aborted" if ending == "abort" else "error") for event in events)
            assert runtime_state(session) == before
            assert await database_state(session) == stored
            versions = ((None, original),)
        for index, expected in versions:
            session = await restore(session, sessions)
            if index is not None:
                switched = await client.post(f"/api/game/{session.save_id}/swipe/jump", params={"index": index})
                assert switched.status_code == 200, switched.text
                session = await restore(session, sessions)
            assert session.world_tree.get_node(node_id).get("_turn_player_inputs", []) == expected
            for streaming in (False, True):
                replay = install_live_inputs(session, stage)
                response = await asyncio.wait_for(client.post(
                    endpoint(session, "regenerate", streaming), params={"stage": stage},
                ), 20)
                regenerated = result_from(response, stream=streaming)
                assert replay.replay_inputs and all(entries == expected for entries in replay.replay_inputs)
                value, choices = live_outcome(expected)
                if stage == "state":
                    assert regenerated["state"]["world_properties"]["test_inspection_status"] == value
                else:
                    assert [choice["text"] for choice in regenerated["choices"]] == [choice["text"] for choice in choices]
                assert regenerated["narrative"] == initial["narrative"]
                assert_effects(session.current_state)
                assert session.world_tree.get_node(node_id).get("_turn_player_inputs", []) == expected
                assert "_turn_player_inputs" not in session.current_state
                assert "_turn_player_inputs" not in response.text and PRIVATE_PLAN not in response.text
            for suffix in (f"tree/{node_id}", "history", "swipes"):
                public = await client.get(f"/api/game/{session.save_id}/{suffix}")
                assert public.status_code == 200
                assert "_turn_player_inputs" not in public.text and PRIVATE_PLAN not in public.text
