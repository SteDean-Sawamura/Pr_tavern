import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from api import game_routes
from test_actor_goal_settlement import result_from
from test_event_narrative_consistency import SAFE, claim_context, narrative_provider
from test_scheduled_npc_overrides import make_session
from test_story_choice_settlement import choice_client, stored_state


START = "2030-01-01T09:00:00"
END = "2030-01-01T09:15:00"
INSTRUCTION = "Synthetic guidance: describe the practice signal without revealing its hidden cause."
REVISED = "You hear a brief practice signal beside the desk and mark the time in your notebook. Its cause remains unclear, so you record only the sound and continue watching the clock until the observation ends."


def instruction_session(category="one_time_events", *, hidden=False):
    script = copy.deepcopy(make_session(category, threshold=0).script)
    event = script[category][0]
    event["visibility"] = "hidden" if hidden else "public"
    event["effects"] = {
        "inject_prompt": INSTRUCTION,
        "set_var": [{"var_id": "test_ready", "op": "inc", "value": 1}],
    }
    session = GameSession(script, None)
    session.current_state = ScriptLoader.create_initial_state(script)
    session.script_variables.init_state(session.current_state)
    session.current_state["_pipeline_mode"] = "agentic_unified"
    context = claim_context(session, explicit=False)
    context.update(_finalize_narrative=SAFE, requested_wait_minutes=None)
    return session, context


def feedback_instructions(result):
    marker = " | 定时事件叙事指令: "
    assert marker in result
    return json.JSONDecoder().raw_decode(result.split(marker, 1)[1])[0]


@pytest.mark.parametrize("category,count", [("one_time_events", 1), ("cyclic_events", 3)])
@pytest.mark.parametrize("hidden", [False, True])
def test_projected_instructions_match_settlement_without_mutation(category, count, hidden):
    session, context = instruction_session(category, hidden=hidden)
    context["_accepted_state_updates"] = [{"end_time": END}]
    before = copy.deepcopy((session.current_state, session.script, context))
    instructions = []
    events, state = session._projected_events(context, with_state=True, event_instructions=instructions)
    assert len(instructions) == count
    assert instructions == [{"event_id": "test_shift", "fire_time": event["fire_time"], "text": INSTRUCTION}
                            for event in events]
    assert state["script_variables"]["test_ready"] == count
    assert (session.current_state, session.script, context) == before
    _, _, result = session._settle_timed_events(START, END, [])
    assert result.scheduled_instructions == instructions
    assert result.inject_prompts == [INSTRUCTION] * count
    assert session.current_state["script_variables"]["test_ready"] == count


@pytest.mark.parametrize("delegated", [False, True])
def test_accepted_update_delivers_instructions_and_retraction(delegated):
    session, context = instruction_session(hidden=True)
    before = copy.deepcopy(session.current_state)
    if delegated:
        result = session._dispatch_settlement_tool("update_state", {"end_time": END}, context)
    else:
        result = session._handle_update_state({"end_time": END}, context)
    feedback = feedback_instructions(result)
    assert feedback["provisional"] is True
    assert feedback["instructions"][0]["text"] == INSTRUCTION
    context["_accepted_state_updates"].clear()
    withdrawn = feedback_instructions(session._state_projection_feedback(context))
    assert withdrawn["instructions"] == []
    assert session.current_state == before


def test_finalize_delivers_unseen_instructions_before_accepting_state():
    session, context = instruction_session()
    before = copy.deepcopy(session.current_state)
    arguments = {"end_time": END, "variable_changes": [{"id": "test_ready", "op": "inc", "value": 2}]}
    result = json.loads(session._handle_finalize_turn(context, arguments))
    assert result["error_code"] == "event_instructions_available"
    assert result["event_instructions"][0]["text"] == INSTRUCTION
    assert result["state_update_accepted"] is False
    assert context["_accepted_state_updates"] == []
    assert session._handle_finalize_turn(context, arguments).startswith("已同步完成")
    assert context["_accepted_state_updates"] == [arguments]
    assert session.current_state == before


def test_seen_instructions_do_not_add_an_extra_finalize_round():
    session, context = instruction_session()
    assert INSTRUCTION in session._handle_update_state({"end_time": END}, context)
    assert session._handle_finalize_turn(context, {}).startswith("已同步完成")


@pytest.mark.parametrize("mode", ["early", "unmet", "already_fired", "empty"])
def test_inactive_instructions_do_not_block_finalize(mode):
    session, context = instruction_session()
    arguments = {"end_time": END}
    if mode == "early":
        arguments["end_time"] = "2030-01-01T09:04:00"
    elif mode == "unmet":
        session.current_state["script_variables"]["test_ready"] = -1
    elif mode == "already_fired":
        session._settle_timed_events(START, END, [])
    else:
        session.event_engine.events["test_shift"].effects["inject_prompt"] = ""
    assert session._handle_finalize_turn(context, arguments).startswith("已同步完成")


def test_new_cyclic_occurrence_requires_its_own_instruction_feedback():
    session, context = instruction_session("cyclic_events")
    result = session._handle_update_state({"end_time": "2030-01-01T09:05:00"}, context)
    assert len(feedback_instructions(result)["instructions"]) == 1
    result = json.loads(session._handle_finalize_turn(context, {"end_time": END}))
    assert len(result["event_instructions"]) == 3
    assert context["_accepted_state_updates"] == [{"end_time": "2030-01-01T09:05:00"}]


def test_read_queries_do_not_acknowledge_instructions_and_long_text_is_not_truncated():
    session, context = instruction_session()
    instruction = INSTRUCTION * 80
    session.event_engine.events["test_shift"].effects["inject_prompt"] = instruction
    before = copy.deepcopy(context)
    session._projected_events(context)
    session._story_query_candidate(context)
    assert context == before
    feedback = json.loads(session._handle_finalize_turn(context, {"end_time": END}))
    assert feedback["event_instructions"][0]["text"] == instruction


def install_instruction_agent(session, mode):
    provider = narrative_provider(session, "clock")
    provider.observed_instructions = []

    async def respond(messages, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        if "classify_intent" in names:
            phase, step, name, arguments = "intent", 0, "classify_intent", {"intent": "action"}
        elif "extract_notes" in names:
            phase, step, name, arguments = "notes", 0, "extract_notes", {"notes": []}
        else:
            phase = "main" if "submit_plan" in names or "delegate_settlement" in names else "settlement"
            completed = [message["content"] for message in messages if message.get("role") == "tool"
                         and message.get("tool_call_id", "").startswith(f"instruction-{phase}-")]
            step = len(completed)
            if phase == "settlement":
                operations = [
                    ("update_state", {"end_time": END}),
                    ("write_narrative", {"text": REVISED}),
                    ("update_state", {"end_time": END}),
                ]
                if step == 1:
                    provider.observed_instructions.append(feedback_instructions(completed[-1])["instructions"])
                if step == len(operations):
                    return {"content": "Done.", "tool_calls": []}
            else:
                operations = [("submit_plan", {"plan": "Observe the synthetic signal using the event guidance."})]
                if mode == "staged":
                    operations += [
                        ("update_state", {"end_time": END}),
                        ("undo_my_last_action", {"reason": "Recheck the candidate window."}),
                        ("update_state", {"end_time": END}),
                        ("write_narrative", {"text": REVISED}),
                        ("finalize_turn", {"summary": "Recorded the synthetic signal."}),
                    ]
                    if step in (2, 4):
                        provider.observed_instructions.append(feedback_instructions(completed[-1])["instructions"])
                    if step == 3:
                        assert feedback_instructions(completed[-1])["instructions"] == []
                elif mode == "finalize":
                    operations += [
                        ("write_narrative", {"text": SAFE}),
                        ("finalize_turn", {"end_time": END}),
                        ("write_narrative", {"text": REVISED}),
                        ("finalize_turn", {"end_time": END, "summary": "Recorded the synthetic signal."}),
                    ]
                    if step == 3:
                        feedback = json.loads(completed[-1])
                        assert feedback["error_code"] == "event_instructions_available"
                        provider.observed_instructions.append(feedback["event_instructions"])
                else:
                    operations += [
                        ("write_narrative", {"text": SAFE}),
                        ("delegate_settlement", {"summary": "Review the synthetic event guidance."}),
                    ]
            assert step < len(operations), "Instruction delivery must not loop after narrative revision"
            name, arguments = operations[step]
            if name == "write_narrative" and arguments["text"] == REVISED:
                assert provider.observed_instructions[-1][0]["text"] == INSTRUCTION
            assert session.current_state["game_time"] == START
            assert session.current_state["script_variables"]["test_ready"] == 0
        assert name in names
        return {"content": "", "tool_calls": [{
            "id": f"instruction-{phase}-{step}", "name": name, "arguments": arguments,
        }]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    session.ai_provider = provider
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fail_save", [("staged", False), ("finalize", True), ("delegated", True)])
async def test_instruction_harness_revision_retry_replay_regeneration_and_reload(tmp_path, monkeypatch, mode, fail_save):
    session, _ = instruction_session(hidden=True)
    provider = install_instruction_agent(session, mode)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        root = session.world_tree.active_node_id
        before = copy.deepcopy(session.current_state)
        baseline = await stored_state(session)
        endpoint = f"/api/game/{session.save_id}/action/stream"
        action = {"type": "freeform", "text": "Wait and observe the synthetic practice signal.",
                  "action_id": "synthetic-instruction-action", "expected_node_id": root}
        if fail_save:
            async def reject_save(current, result, action_id=None):
                assert result["narrative"] == REVISED
                assert current.current_state["script_variables"]["test_ready"] == 1
                raise RuntimeError("Synthetic instruction save rejection")

            with monkeypatch.context() as patch:
                patch.setattr(game_routes, "_save_node", reject_save)
                failure = await asyncio.wait_for(client.post(endpoint, json=action), 20)
            assert '"type": "error"' in failure.text and '"type": "final"' not in failure.text
            assert session.current_state == before and await stored_state(session) == baseline
        response = await asyncio.wait_for(client.post(endpoint, json=action), 20)
        result = result_from(response)
        assert result["narrative"] == REVISED
        assert INSTRUCTION not in result["narrative"]
        settled = await stored_state(session)
        assert settled["script_variables"]["test_ready"] == 1
        assert "_known_event_instructions" not in settled
        assert provider.observed_instructions
        calls = provider.generate_with_tools.await_count
        assert (await client.post(endpoint, json=action)).text == response.text
        assert provider.generate_with_tools.await_count == calls
        assert await stored_state(session) == settled
        regenerated = await asyncio.wait_for(client.post(f"/api/game/{session.save_id}/regenerate?stage=all"), 20)
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.json()["narrative"] == REVISED
        assert (await stored_state(session))["script_variables"]["test_ready"] == 1
        game_routes._sessions.clear()
        restored = await game_routes._restore_session(session.save_id)
        sessions.append(restored)
        assert restored.current_state["script_variables"]["test_ready"] == 1
        await restored.branch_to_node(root)
        assert restored.current_state["script_variables"]["test_ready"] == 0
        await restored.branch_to_node(result["node_id"])
        assert restored.current_state["script_variables"]["test_ready"] == 1
