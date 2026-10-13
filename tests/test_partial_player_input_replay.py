import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_generation_baseline import assert_effects, restore
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_turn_player_input_replay import EXTRA, NEW_PLAN, PLAN, PRIVATE_PLAN, chosen_procedure, context_block
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages, stage_session


def recorded_inputs(kind, plan=PLAN):
    entries = []
    if kind in {"reply", "both"}:
        context = copy.deepcopy(plan)
        context["plan"] += " " + PRIVATE_PLAN
        entries.append({"message": "第二项", "plan_reply_context": {**context, "reply": "第二项"}})
    if kind in {"injection", "both"}:
        entries.append({"message": EXTRA})
    return entries


def outcome(entries):
    procedure, position = "Inspect", "room"
    for entry in entries:
        if entry.get("plan_reply_context"):
            procedure = chosen_procedure(entry["plan_reply_context"])
        if entry["message"] == EXTRA:
            position = "door"
    choices = [{**choice, "text": f"{procedure} practice shelf {index} from the {position}."}
               for index, choice in enumerate(CHOICES)]
    return f"{procedure}/{position}", choices


def install_partial_inputs(session, target):
    provider = install_stages(session, "native", "minor", "exploration")
    provider.input_requests = []
    prefixes = {group: session.prompt_builder._build_world_state_system(group)
                for group in ("resource", "spatial", "temporal", "world", "ext")}
    prefixes["npc"] = PromptLoader.get().render_system("npc_reaction")

    def received(messages):
        entries = context_block(messages, "turn_player_inputs") or []
        provider.input_requests.append(copy.deepcopy(entries))
        return outcome(entries)

    async def generate(messages, **kwargs):
        value, choices = received(messages)
        if target == "choices":
            return json.dumps({"choices": choices})
        group = next(group for group, prefix in prefixes.items() if kwargs["system"].startswith(prefix))
        result = {}
        if group == "world":
            result["world_property_changes"] = [{"id": "test_inspection_status", "value": value}]
        if group == "temporal":
            result["end_time"] = "2030-01-01T09:01:00"
        return json.dumps(result)

    async def respond(messages, **kwargs):
        value, choices = received(messages)
        completed = sum(message.get("role") == "tool" for message in messages)
        if target == "state" and completed == 0:
            name = "update_state"
            arguments = {"end_time": "2030-01-01T09:01:00", "world_property_changes": [
                {"id": "test_inspection_status", "value": value},
            ]}
        else:
            name, arguments = "finalize_turn", {"summary": "Recorded the synthetic procedure."}
            if target == "choices":
                arguments["choices"] = choices
        assert completed < (2 if target == "state" else 1)
        assert name in {tool["function"]["name"] for tool in kwargs["tools"]}
        return {"content": "", "tool_calls": [{"id": f"synthetic-partial-input-{name}",
                                                "name": name, "arguments": arguments}]}

    provider.generate = AsyncMock(side_effect=generate)
    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stage", ["choices", "state"])
@pytest.mark.parametrize("input_kind", ["reply", "injection", "both", "none"])
async def test_partial_regeneration_uses_loaded_swipe_player_inputs(tmp_path, monkeypatch, streaming, stage, input_kind):
    session = stage_session("native", False)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_stages(session, "native", "minor", "exploration")
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        await session._drain_background_tasks()
        node_id = initial["node_id"]
        node = session.world_tree.get_node(node_id)
        saved_action = copy.deepcopy(node["player_action"])
        original = recorded_inputs(input_kind)
        if original:
            node["_turn_player_inputs"] = copy.deepcopy(original)
        else:
            node.pop("_turn_player_inputs", None)
        alternative = recorded_inputs("both", NEW_PLAN)
        if input_kind == "injection":
            alternative = recorded_inputs("reply", NEW_PLAN)
        session._record_regen_swipe(
            node, initial["narrative"], copy.deepcopy(initial["choices"]), copy.deepcopy(session.current_state),
            copy.deepcopy(initial["state_changes"]), generation_context={"_turn_player_inputs": alternative},
        )
        await game_routes._update_node_in_db(session, initial)

        for attempt, (index, expected_inputs) in enumerate(((0, original), (1, alternative), (0, original))):
            session = await restore(session, sessions)
            switched = await client.post(f"/api/game/{session.save_id}/swipe/jump", params={"index": index})
            assert switched.status_code == 200, switched.text
            session = await restore(session, sessions)
            node = session.world_tree.get_node(node_id)
            assert node.get("_turn_player_inputs", []) == expected_inputs
            before_state = copy.deepcopy(session.current_state)
            provider = install_partial_inputs(session, stage)
            response = await asyncio.wait_for(client.post(
                endpoint(session, "regenerate", streaming), params={"stage": stage},
            ), 20)
            regenerated = result_from(response, stream=streaming)
            assert len(provider.input_requests) == (1 if stage == "choices" else 2 if streaming else 6)
            assert all(entries == expected_inputs for entries in provider.input_requests), provider.input_requests
            assert regenerated["node_id"] == node_id and regenerated["swipe_index"] == attempt + 2
            assert regenerated["narrative"] == initial["narrative"]
            expected_value, expected_choices = outcome(expected_inputs)
            if stage == "state":
                assert session.current_state["world_properties"]["test_inspection_status"] == expected_value
                assert regenerated["choices"] == initial["choices"]
            else:
                assert [choice["text"] for choice in regenerated["choices"]] == [choice["text"] for choice in expected_choices]
                assert session.current_state["world_properties"] == before_state["world_properties"]
                assert session.current_state["game_time"] == before_state["game_time"]
            assert_effects(session.current_state)
            assert node["player_action"] == saved_action
            assert node.get("_turn_player_inputs", []) == expected_inputs
            assert node["swipes"][0].get("_turn_player_inputs", []) == original
            assert node["swipes"][1]["_turn_player_inputs"] == alternative
            context = (await stored_state(session))["_node_context"]
            assert context.get("_turn_player_inputs", []) == expected_inputs
            assert "_turn_player_inputs" not in session.current_state
            public_texts = [response.text]
            for suffix in (f"tree/{node_id}", "history", "swipes"):
                public = await client.get(f"/api/game/{session.save_id}/{suffix}")
                assert public.status_code == 200, public.text
                public_texts.append(public.text)
            for text in public_texts:
                assert PRIVATE_PLAN not in text and "turn_player_inputs" not in text
                assert EXTRA not in text
