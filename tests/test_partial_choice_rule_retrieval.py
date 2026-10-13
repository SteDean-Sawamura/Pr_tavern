import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from test_actor_goal_settlement import result_from
from test_agent_harness_execution import _stream_events
from test_generation_baseline import assert_effects, restore
from test_partial_rule_recovery import rules_session, tool
from test_regenerate_stream_errors import assert_controls_closed, database_state, runtime_state
from test_regeneration_parent_runtime import ACTION
from test_story_choice_settlement import choice_client, stored_state
from test_workflow_declaration_runtime import CHOICES, endpoint, prepare_root
from test_workflow_stage_directives import install_stages


PRIVATE_RULE = "SYNTHETIC_PARTIAL_CHOICE_RULE_BODY"


def make_choice_rules(category, alternate):
    original = rules_session(increment=5 if alternate else 2)
    script = copy.deepcopy(original.script)
    script[category][0]["rule_notes"] = (PRIVATE_RULE + "\n") * 2400
    script["world_properties"][0]["recovery_value"] = "recorded" if alternate else "verified"
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session.current_state["script_variables"]["test_review_count"] = 11 if alternate else 7
    session._restore_session_models_from_state()
    return session


def choice_text(category, current, declared):
    if category == "variables":
        return f"Propose a review count of {current + declared}."
    return f"Inspect the {current} record toward {declared}."


def install_choice_reader(session, category, reject_first):
    provider = install_stages(session, "native", "minor", "exploration")
    provider.reads = []
    provider.requests = []
    baseline = None

    async def respond(messages, **kwargs):
        nonlocal baseline
        if baseline is None:
            baseline = copy.deepcopy((session.current_state, session.script))
        assert (session.current_state, session.script) == baseline
        names = [entry["function"]["name"] for entry in kwargs["tools"]]
        assert len(names) == len(set(names))
        provider.requests.append(names)
        results = {message["tool_call_id"]: json.loads(message["content"])
                   for message in messages if message["role"] == "tool"}
        if "consult_rules" not in names:
            text = "Rule lookup is unavailable; inspect the area."
        else:
            assert set(names) == {"query_lorebook", "peek_upcoming_events", "consult_rules", "finalize_turn"}
            if reject_first and "synthetic-recovery-candidate" not in results:
                return {"content": "", "tool_calls": [
                    tool("update_state", {"variable_changes": [{"id": "test_review_count", "op": "set", "value": 99}]}, "write"),
                    tool("consult_rules", {"category": category, "include_candidates": True}, "candidate"),
                ]}
            if reject_first:
                assert "未开放工具" in results["synthetic-recovery-write"]["error"]
                assert "候选" in results["synthetic-recovery-candidate"]["error"]
            if "synthetic-recovery-catalog" not in results:
                return {"content": "", "tool_calls": [tool("consult_rules", {"category": category}, "catalog")]}
            catalog = results["synthetic-recovery-catalog"]
            assert "error" not in catalog
            entry, = catalog["entries"]
            assert entry["next_offset"] is not None
            coordinates = {"category": category, "rule_index": entry["rule_index"], "snapshot": entry["snapshot"]}
            if "synthetic-recovery-current" not in results:
                field = "review_increment" if category == "variables" else "recovery_value"
                return {"content": "", "tool_calls": [
                    tool("consult_rules", {**coordinates, "result_path": ["current_value"]}, "current"),
                    tool("consult_rules", {**coordinates, "result_path": ["definition", field]}, "declared"),
                ]}
            values = []
            for identifier in ("current", "declared"):
                page = results[f"synthetic-recovery-{identifier}"]
                assert "error" not in page
                section, = page["entries"]
                assert section["content_format"] == "result_section" and section["complete"]
                assert section["snapshot"] == entry["snapshot"]
                assert not section["read"]["include_candidates"]
                values.append(section["content"] if section["value_type"] == "string" else section["value"])
            provider.reads.append(values)
            text = choice_text(category, *values)
        return {"content": "", "tool_calls": [tool("finalize_turn", {
            "summary": "Proposed the next synthetic inspection.",
            "choices": [{**choice, "text": f"{text} Shelf {index}."} for index, choice in enumerate(CHOICES)],
        }, "final")]}

    provider.generate_with_tools = AsyncMock(side_effect=respond)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["variables", "world_properties"])
@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("reject_first", [False, True])
@pytest.mark.parametrize("fail_save", [False, True])
async def test_partial_choices_read_declared_rules_without_settling_state(
    tmp_path, monkeypatch, category, alternate, reject_first, fail_save,
):
    session = make_choice_rules(category, alternate)
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        install_stages(session, "native", "minor", "exploration")
        initial = result_from(await client.post(endpoint(session, "action", False), json=ACTION), stream=False)
        await session._drain_background_tasks()
        session = await restore(session, sessions)
        current = (session.current_state["script_variables"]["test_review_count"] if category == "variables"
                   else session.current_state["world_properties"]["test_inspection_status"])
        declared = (5 if alternate else 2) if category == "variables" else ("recorded" if alternate else "verified")
        expected = [f"{choice_text(category, current, declared)} Shelf {index}." for index in range(4)]
        before, stored = runtime_state(session), await database_state(session)
        for attempt in range(2 if fail_save else 1):
            failing = fail_save and attempt == 0
            provider = install_choice_reader(session, category, reject_first)
            with monkeypatch.context() as patch:
                if failing:
                    patch.setattr(game_routes, "_update_node_in_db", AsyncMock(side_effect=RuntimeError("SYNTHETIC_CHOICE_SAVE_FAILURE")))
                response = await asyncio.wait_for(client.post(endpoint(session, "regenerate", True), params={"stage": "choices"}), 20)
            assert response.status_code == 200
            assert_controls_closed(session)
            assert PRIVATE_RULE not in response.text
            if failing:
                events = _stream_events(response.text)
                assert any(event["type"] == "error" for event in events)
                assert not any(event["type"] == "final" for event in events)
                assert runtime_state(session) == before
                assert await database_state(session) == stored
            else:
                regenerated = result_from(response)
                assert [choice["text"] for choice in regenerated["choices"]] == expected
                assert regenerated["narrative"] == initial["narrative"]
                for key in ("game_time", "player", "npcs", "world_properties"):
                    assert regenerated["state"][key] == initial["state"][key]
                assert "script_variables" not in regenerated["state"]
                assert session.current_state["script_variables"] == before[0]["script_variables"]
                assert regenerated["state_changes"] == initial["state_changes"]
                assert regenerated["swipe_index"] == 1 and regenerated["total_swipes"] == 2
                assert_effects(session.current_state)
                saved = await stored_state(session)
                assert [choice["text"] for choice in saved["_node_context"]["swipes"][1]["choices"]] == expected
            assert provider.reads == [[current, declared]]
            assert all("consult_rules" in names for names in provider.requests)
            session = await restore(session, sessions)
        for index, texts in ((0, [choice["text"] for choice in initial["choices"]]), (1, expected)):
            switched = await client.post(f"/api/game/{session.save_id}/swipe/jump", params={"index": index})
            assert switched.status_code == 200
            session = await restore(session, sessions)
            node = session.world_tree.get_node(initial["node_id"])
            assert [choice["text"] for choice in node["choices_presented"]] == texts
            assert_effects(session.current_state)
            for suffix in (f"tree/{initial['node_id']}", "history", "swipes"):
                public = await client.get(f"/api/game/{session.save_id}/{suffix}")
                assert public.status_code == 200 and PRIVATE_RULE not in public.text
