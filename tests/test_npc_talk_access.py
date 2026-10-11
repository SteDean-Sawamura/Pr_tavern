import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from test_scheduled_npc_overrides import make_session


def talk_session():
    session = make_session()
    actor = session.current_state["npcs"]["test_guide"]
    actor.update({"known": True, "met": True})
    session._refresh_npc_locations()
    calls = []

    async def generate_stream(messages, **kwargs):
        calls.append(messages)
        yield "<think></think>"
        yield "Welcome to the practice desk."

    session.ai_provider = SimpleNamespace(
        generate=AsyncMock(return_value="Welcome to the practice desk."),
        generate_stream=generate_stream, stream_calls=calls,
    )
    return session


async def talk(session, streaming, npc_id="test_guide"):
    if not streaming:
        return await session.talk_to_npc(npc_id, "Hello.")
    chunks = [chunk async for chunk in session.talk_to_npc_stream(npc_id, "Hello.")]
    assert chunks[-1]["type"] == "final"
    if chunks[-1].get("error"):
        assert len(chunks) == 1
    return chunks[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("case,reason", [
    ("missing", "尚未认识"),
    ("unknown", "尚未认识"),
    ("unmet", "未接触"),
    ("dead", "当前无法对话"),
    ("dead_companion", "当前无法对话"),
    ("unknown_companion", "尚未认识"),
    ("away", "不在当前位置"),
    ("room", "不在同一房间"),
    ("unlocated", "位置待确认"),
    ("player_unlocated", "位置待确认"),
])
async def test_talk_rejects_before_model_or_state_mutation(streaming, case, reason):
    session = talk_session()
    state = session.current_state
    actor = state["npcs"]["test_guide"]
    if case == "missing":
        state["npcs"].pop("test_guide")
    elif case.startswith("unknown"):
        actor["known"] = False
    elif case.startswith("dead"):
        actor["alive"] = False
    elif case == "unmet":
        actor["met"] = False
    elif case == "away":
        actor["current_location"] = "test_desk"
    elif case == "room":
        actor["current_room"] = "test_inner"
        state["player"]["current_room"] = "test_outer"
    elif case == "unlocated":
        actor.pop("current_location", None)
        actor.pop("default_location", None)
        session.script["npcs"][0].pop("default_location")
        session.script["npcs"][0].pop("schedule")
    elif case == "player_unlocated":
        state["player"]["location"] = ""
    if case.endswith("companion"):
        state["companions"] = ["test_guide"]
    original = copy.deepcopy(state)
    original_tree = copy.deepcopy(session.world_tree.nodes)
    result = await talk(session, streaming)
    assert result["error"] == reason
    session.ai_provider.generate.assert_not_awaited()
    assert session.ai_provider.stream_calls == []
    assert session.current_state == original
    assert session.world_tree.nodes == original_tree


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("case", ["same_place", "legacy", "same_room", "companion", "location_name", "child_location"])
async def test_direct_talk_preserves_legacy_entry_without_prior_contact(streaming, case):
    session = talk_session()
    state = session.current_state
    actor = state["npcs"]["test_guide"]
    if case == "legacy":
        actor.pop("known")
        actor.pop("met")
    elif case == "same_room":
        state["player"]["current_room"] = actor["current_room"] = "test_shared"
    elif case == "companion":
        state["companions"] = ["test_guide"]
        actor["current_location"] = "test_desk"
        actor["current_room"] = "test_inner"
        state["player"]["current_room"] = "test_outer"
    elif case == "location_name":
        state["player"]["location"] = "Origin"
    elif case == "child_location":
        session.script["locations"][1]["parent"] = "test_origin"
        actor["current_location"] = "test_desk"
    assert not state.get("npc_contacts")
    result = await talk(session, streaming)
    assert "error" not in result
    assert result["response"] == "Welcome to the practice desk."
    assert result["state"]["game_time"] == "2030-01-01T09:05:00"
    assert result["dialogue_count"] == 1
    assert result["quick_replies"]
    assert len(session.world_tree.nodes) == 1
    assert session.ai_provider.generate.await_count + len(session.ai_provider.stream_calls) == 1
    if case in {"same_place", "legacy", "same_room"}:
        assert session._npc_contact_is_current("test_guide", state)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_talk_checks_access_after_background_work(streaming):
    session = talk_session()

    async def drain():
        session.current_state["npcs"]["test_guide"]["alive"] = False

    session._drain_background_tasks = AsyncMock(side_effect=drain)
    assert (await talk(session, streaming))["error"] == "当前无法对话"
    session.ai_provider.generate.assert_not_awaited()
    assert session.ai_provider.stream_calls == []


def test_public_access_and_contacts_resolve_the_requested_snapshot():
    session = talk_session()
    original = copy.deepcopy(session.current_state)
    session._apply_npc_contact_changes(session.current_state, [{"npc_id": "test_guide", "in_contact": True}])
    before = copy.deepcopy(session.current_state)
    session.current_state["game_time"] = "2030-01-01T10:05:00"
    session.script["npcs"][0]["schedule"].append({"time_range": "10:00-11:00", "location": "test_desk"})
    after = copy.deepcopy(session.current_state)
    public_after = game_routes._enrich_state(session, session.current_state)
    assert public_after["npc_contacts"] == {}
    assert public_after["npc_talk_block_reasons"] == {"test_guide": "不在当前位置"}
    assert game_routes._public_result({"state": before}, session)["state"]["npc_contacts"] == {"test_guide": True}
    assert game_routes._public_result({"state": before}, session)["state"]["npc_talk_block_reasons"] == {"test_guide": ""}
    assert session.current_state == after
    assert original["game_time"] == before["game_time"]


def test_public_access_does_not_reveal_unknown_characters_or_remote_details():
    session = talk_session()
    state = session.current_state
    state["npcs"]["test_guide"]["known"] = False
    state["npcs"]["test_private"] = {"name": "SYNTHETIC_PRIVATE_NAME", "known": False, "alive": False}
    state["npc_talk_block_reasons"] = {"test_private": "SYNTHETIC_STALE_REASON"}
    public = game_routes._enrich_state(session, state)
    assert public["npc_talk_block_reasons"] == {}
    state["npcs"]["test_guide"].update({"known": True, "alive": False})
    assert game_routes._enrich_state(session, state)["npc_talk_block_reasons"] == {"test_guide": "当前无法对话"}


def test_room_change_invalidates_contact_before_the_next_reply():
    session = talk_session()
    state = session.current_state
    state["player"]["current_room"] = "test_outer"
    state["npcs"]["test_guide"]["current_room"] = "test_outer"
    session._apply_npc_contact_changes(state, [{"npc_id": "test_guide", "in_contact": True}])
    assert session._npc_contact_is_current("test_guide", state)
    state["npcs"]["test_guide"]["current_room"] = "test_inner"
    assert not session._npc_contact_is_current("test_guide", state)
    assert game_routes._enrich_state(session, state)["npc_contacts"] == {}
    session._apply_npc_contact_changes(state, [{"npc_id": "test_guide", "in_contact": True}])
    assert not state.get("npc_contacts")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_talk_settlement_refreshes_locations_before_contact_and_next_reply(streaming):
    session = talk_session()
    session.current_state["script_variables"]["test_ready"] = 2
    session._apply_npc_contact_changes(session.current_state, [{"npc_id": "test_guide", "in_contact": True}])
    result = await talk(session, streaming)
    state = result["state"]
    assert state["npcs"]["test_guide"]["current_location"] == "test_desk"
    assert not state.get("npc_contacts")
    assert result["quick_replies"] == []
    assert "test_shift" in result["triggered_events"]
    assert game_routes._public_result(result, session)["state"]["npc_talk_block_reasons"] == {"test_guide": "不在当前位置"}
    assert (await talk(session, streaming))["error"] == "不在当前位置"
    assert session.ai_provider.generate.await_count + len(session.ai_provider.stream_calls) == 1
