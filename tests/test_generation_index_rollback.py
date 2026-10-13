import asyncio
import copy
import threading
from unittest.mock import AsyncMock

import pytest

from api import game_routes
from engine.game_session import GameSession
from engine.prompt_loader import PromptLoader
from test_actor_goal_settlement import result_from
from test_event_lore_persistence import event_session
from test_generation_baseline import restore
from test_regenerate_stream_errors import database_state, runtime_state
from test_story_choice_settlement import choice_client
from test_workflow_declaration_runtime import endpoint, prepare_root
from test_workflow_settled_choices import ACTION, install_settlement, settled_session


class SyntheticIndex:
    def __init__(self, session, blocked=False):
        self.entries = {entry.id: entry.content for entry in session.prompt_builder.lorebook.entries
                        if len(entry.content) >= 20}
        self.blocked = blocked
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.queries = []
        if not blocked:
            self.release.set()

    def write(self, entry_id, content):
        if self.blocked:
            self.blocked = False
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError("SYNTHETIC_INDEX_RELEASE_TIMEOUT")
        if content is None:
            self.entries.pop(entry_id, None)
        else:
            self.entries[entry_id] = content
        self.finished.set()

    def add_lorebook_batch(self, batch):
        for entry_id, content, metadata in batch:
            self.write(entry_id, content)

    def add_lorebook(self, entry_id, content, metadata):
        self.write(entry_id, content)

    def remove_lorebook(self, entry_id):
        self.write(entry_id, None)

    def add(self, *args):
        pass

    def query(self, query_text, top_k=3, exclude_turns=None, doc_type=None, allowed_node_ids=None):
        self.queries.append((query_text, doc_type))
        if doc_type not in (None, "lorebook"):
            return []
        return [{"doc_type": "lorebook", "entry_id": entry_id, "text": content, "turn": 0, "game_time": ""}
                for entry_id, content in self.entries.items()]


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight", [False, True])
async def test_rollback_restores_completed_and_inflight_declared_lore_index(event_session, inflight):
    session, script, expected = event_session
    index = SyntheticIndex(session, blocked=inflight)
    session.vector_memory = index
    session.data_bank = None
    original = copy.deepcopy(index.entries)
    snapshot = game_routes._capture_generation_state(session)
    session._settle_timed_events("2030-01-01T09:00:00", "2030-01-01T09:05:00", [])
    try:
        if inflight:
            assert await asyncio.to_thread(index.started.wait, 2)
        else:
            await session._drain_background_tasks(index_only=True)
            assert list(index.entries.values()) == expected
        await game_routes._rollback_generation_state(session, snapshot)
        if inflight:
            assert any(not task.done() for task in session._index_tasks)
        index.release.set()
        activated = []
        await session._enrich_with_rag("SYNTHETIC_SEMANTIC_QUERY", [], activated, "")
        assert index.entries == original
        assert [entry.content for entry in activated] == [entry["content"] for entry in script["lorebook"]]
        assert session.current_state == snapshot["state"]
    finally:
        index.release.set()
        if index.started.is_set():
            assert await asyncio.to_thread(index.finished.wait, 2)
        await session._drain_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["save", "cancel"])
async def test_action_index_rollback_preserves_retrieval_then_commits_retry(tmp_path, monkeypatch, streaming, failure):
    original = settled_session("disabled")
    script = copy.deepcopy(original.script)
    notice = {"id": "test_notice", "keys": ["SyntheticNotice"], "content": "SYNTHETIC_COMMITTED_INDEX_NOTICE"}
    script.setdefault("lorebook", []).append(notice)
    script["story_tree"]["trees"][0]["nodes"].append({
        "id": "test_index_removal", "type": "trigger", "event": ["after_ai"],
        "effects": {"remove_lore": ["test_notice"]},
    })
    script["settings"]["vector_memory_enabled"] = False
    session = GameSession(script, None, save_id=original.save_id)
    session.current_state = copy.deepcopy(original.current_state)
    session._restore_session_models_from_state()
    index = SyntheticIndex(session)
    session.vector_memory = index
    session.data_bank = None
    async with choice_client(session, tmp_path, monkeypatch) as (client, sessions):
        await prepare_root(session)
        before, stored = runtime_state(session), await database_state(session)
        provider = install_settlement(session, "disabled", "normal")
        generate = provider.generate.side_effect
        prefix = PromptLoader.get().render_system("stage_choices")
        reached, blocked = asyncio.Event(), asyncio.Event()

        async def observe_choices(messages, **kwargs):
            if kwargs.get("system", "").startswith(prefix):
                await session._drain_background_tasks(index_only=True)
                assert "test_notice" not in index.entries
                reached.set()
                if failure == "cancel":
                    await blocked.wait()
            return await generate(messages, **kwargs)

        provider.generate = AsyncMock(side_effect=observe_choices)
        with monkeypatch.context() as patch:
            if failure == "save":
                patch.setattr(game_routes, "_save_node", AsyncMock(side_effect=RuntimeError("SYNTHETIC_INDEX_SAVE_FAILURE")))
                response = await asyncio.wait_for(client.post(endpoint(session, "action", streaming), json=ACTION), 20)
                if streaming:
                    assert '"type": "error"' in response.text and '"type": "final"' not in response.text
                else:
                    assert response.status_code == 500
            else:
                pending = asyncio.create_task(client.post(endpoint(session, "action", streaming), json=ACTION))
                try:
                    await asyncio.wait_for(reached.wait(), 10)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(pending, 10)
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        assert reached.is_set()
        assert runtime_state(session) == before
        assert await database_state(session) == stored
        assert not game_routes._get_session_lock(session.save_id).locked()
        activated = []
        await session._enrich_with_rag("SYNTHETIC_SEMANTIC_QUERY", [], activated, "")
        assert index.entries.get("test_notice") == notice["content"]
        assert any(entry.id == "test_notice" for entry in activated)
        install_settlement(session, "disabled", "normal")
        result_from(await client.post(endpoint(session, "action", streaming), json=ACTION), stream=streaming)
        await session._drain_background_tasks(index_only=True)
        assert "test_notice" not in index.entries
        session = await restore(session, sessions)
        assert not any(entry.id == "test_notice" for entry in session.prompt_builder.lorebook.entries)
