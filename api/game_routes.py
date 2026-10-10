"""Game play API routes."""

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
from uuid import uuid4
from collections import OrderedDict
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

from config import DATA_DIR
from db.database import get_db
from engine.game_session import GameSession
from engine.event_scheduler import is_public_event, is_public_story_node
from engine.session.agentic_mixin import (IncompleteAgentTurnError, MapTravelSettlementError,
                                         NarrativeSettlementError, PendingConsequenceDisclosureError, SemanticReviewError,
                                         _public_agent_trace)
from engine.script_loader import ScriptLoader
from engine.class_system import ClassRegistry

router = APIRouter()
logger = logging.getLogger("tavern.game")

# In-memory session store with LRU eviction (save_id -> GameSession)
_MAX_SESSIONS = 50
_sessions: OrderedDict[str, GameSession] = OrderedDict()
# Per-session locks to prevent concurrent modifications
_session_locks: dict[str, asyncio.Lock] = {}
_action_streams = {}


class _ActionStreamJournal:
    def __init__(self, request):
        self.request = request
        self.events = []
        self.changed = asyncio.Event()
        self.done = False
        self.completed_at = None
        self.snapshot = None
        self.failed = False

    def append(self, event):
        self.events.append(event)
        self.changed.set()
        self.changed = asyncio.Event()

    def finish(self):
        self.done = True
        self.completed_at = time.monotonic()
        self.snapshot = None
        self.changed.set()


def _replay_action_stream(journal):
    async def events():
        index = 0
        while True:
            while index < len(journal.events):
                event = journal.events[index]
                index += 1
                yield f"id: {index}\n{event}"
            if journal.done:
                return
            changed = journal.changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=20)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _existing_action_stream(save_id, req):
    now = time.monotonic()
    for key, journal in list(_action_streams.items()):
        if journal.completed_at is not None and now - journal.completed_at > 600:
            _action_streams.pop(key, None)
    completed = sorted((journal.completed_at, key) for key, journal in _action_streams.items()
                       if journal.completed_at is not None)
    for _, key in completed[:-50]:
        _action_streams.pop(key, None)
    if not req.action_id:
        return None
    journal = _action_streams.get((save_id, req.action_id))
    if journal is None:
        return None
    if journal.request != req.model_dump():
        raise HTTPException(status_code=409, detail="此行动的重连参数不一致")
    if journal.done and journal.failed:
        _action_streams.pop((save_id, req.action_id), None)
        return None
    return _replay_action_stream(journal)

_ROLLBACK_FIELDS = (
    ("player_model", ("choice_history", "style_tags", "avg_action_length", "preferred_pace")),
    ("npc_autonomy", ("npc_states",)),
    ("narrative_graph", ("nodes", "edges")),
    ("director_notes", ("notes",)),
    ("world_clock", ("last_tick_time", "pending_events")),
    ("prompt_builder", ("_npc_name_map", "_branch_lore_ids", "_branch_npc_ids")),
    ("prompt_builder.lorebook", ("entries", "_precomputed")),
    ("", ("_pending_world_events", "_last_tool_result",
          "_settlement_messages", "_last_activity_time", "_stable_prefix",
          "_narrative_buffer", "_delegated_settlement", "_agent_traces")),
)
_MISSING = object()
_RESTORED_MODELS = ("player_model", "npc_autonomy", "narrative_graph", "director_notes")


def _capture_generation_state(session):
    fields = []
    for owner_path, names in _ROLLBACK_FIELDS:
        owner = session
        for part in owner_path.split(".") if owner_path else ():
            owner = getattr(owner, part, None)
            if owner is None:
                break
        if owner is None:
            continue
        for name in names:
            value = getattr(owner, name, _MISSING)
            fields.append((owner, name, copy.deepcopy(value) if value is not _MISSING else _MISSING))
    tree = session.world_tree
    active = tree.active_node_id
    active_node = tree.get_node(active) if active else None
    return {
        "state": copy.deepcopy(session.current_state),
        "turn": getattr(session, "turn_number", None),
        "active": active,
        "active_node": copy.deepcopy(active_node),
        "models": {name: getattr(session, name) for name in _RESTORED_MODELS
                   if hasattr(session, name)},
        "fields": fields,
        "tasks": set(getattr(session, "_background_tasks", ())),
        "deferred_story_task": getattr(session, "_deferred_story_task", None),
        "deferred_story_source": getattr(session, "_deferred_story_source", None),
    }


async def _rollback_generation_state(session, snapshot):
    tasks = set(getattr(session, "_background_tasks", ())) - snapshot["tasks"]
    deferred = getattr(session, "_deferred_story_task", None)
    if deferred is not None and deferred is not snapshot["deferred_story_task"]:
        tasks.add(deferred)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    session._deferred_story_task = snapshot["deferred_story_task"]
    session._deferred_story_source = snapshot["deferred_story_source"]

    tree = session.world_tree
    current_active = tree.active_node_id
    if current_active and current_active != snapshot["active"]:
        node = tree.get_node(current_active)
        if node and node.get("parent_id") == snapshot["active"] and not node.get("children_ids"):
            tree.remove_node(current_active)
    active = snapshot["active"]
    if snapshot["active_node"] is not None:
        tree.nodes[active] = snapshot["active_node"]
        parent_id = snapshot["active_node"].get("parent_id")
        if parent_id and parent_id in tree.nodes and active not in tree.nodes[parent_id]["children_ids"]:
            tree.nodes[parent_id]["children_ids"].append(active)
    if active in tree.nodes:
        tree.set_active_node(active)
    session.current_state = snapshot["state"]
    if hasattr(session, "_hydrate_mechanics_runtime"):
        session._hydrate_mechanics_runtime(session.current_state)
    if snapshot["turn"] is not None:
        session.turn_number = snapshot["turn"]
    for name, model in snapshot["models"].items():
        setattr(session, name, model)
    for owner, name, value in snapshot["fields"]:
        if value is _MISSING:
            if hasattr(owner, name):
                delattr(owner, name)
        else:
            setattr(owner, name, value)
    if hasattr(session, "_index_dynamic_npcs_from_state"):
        session._index_dynamic_npcs_from_state(session.current_state)
    if hasattr(session, "prompt_builder"):
        session.prompt_builder.sync_shelved_pc_knowledge(session.current_state)


def _get_session_lock(save_id: str) -> asyncio.Lock:
    """Get or create a per-session lock.

    P0-5: 使用 setdefault 保证原子性——避免两个并发请求各自创建不同的 Lock 实例
    导致互斥失效。eviction 不再删除 lock（见 _sessions_put）。"""
    return _session_locks.setdefault(save_id, asyncio.Lock())


def _set_agent_stream_active(session: GameSession, active: bool, stream_id: str = ""):
    session._active_agent_stream = active
    session._active_agent_stream_id = (stream_id or uuid4().hex) if active else None
    session._active_agent_action_id = None
    session._agent_inject_enabled = active
    session._pending_agent_plan_id = None
    session._pending_agent_action_id = None
    session._pending_agent_plan_signature = None
    session._queued_agent_plan_reply_id = None
    session._abort_flag = False
    queue = getattr(session, "_inject_queue", None)
    dropped = list(queue) if queue and not active else []
    if queue is None:
        session._inject_queue = []
    else:
        queue.clear()
    control_event = getattr(session, "_agent_control_event", None)
    if control_event is not None:
        control_event.clear()
    stop_event = getattr(session, "_agent_stop_event", None)
    if stop_event is not None:
        stop_event.clear()
    session._abort_request_pending = False
    return dropped


def _close_agent_control_events(session: GameSession):
    stop_missed = getattr(session, "_abort_request_pending", False)
    plan_reply_id = getattr(session, "_queued_agent_plan_reply_id", None)
    for index, message in enumerate(_set_agent_stream_active(session, False)):
        event = {"type": "agent_inject_dropped", "message": message}
        if index == 0 and plan_reply_id:
            event["plan_id"] = plan_reply_id
        yield event
    if stop_missed:
        yield {"type": "agent_stop_missed"}


def _sessions_put(save_id: str, session: GameSession):
    """Add or update a session, persisting old sessions before eviction."""
    if save_id in _sessions:
        _sessions.move_to_end(save_id)
    _sessions[save_id] = session
    retiring = sum(bool(getattr(cached, "_eviction_task", None)) for cached in _sessions.values())
    excess = len(_sessions) - _MAX_SESSIONS - retiring
    for old_id, old_session in list(_sessions.items()):
        if excess <= 0:
            break
        if getattr(old_session, "_eviction_task", None):
            continue

        async def retire(evicted_id=old_id, evicted_session=old_session):
            async with _get_session_lock(evicted_id):
                if _sessions.get(evicted_id) is not evicted_session:
                    return
                await evicted_session._drain_background_tasks(timeout=None, include_index=False)
                if _sessions.get(evicted_id) is not evicted_session:
                    return
                evicted_session._consume_script_blueprint()
                pending_tasks = tuple(evicted_session._deferred_tasks)
                for pending in pending_tasks:
                    if not pending.done():
                        pending.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                evicted_session._consume_script_blueprint()
                await _persist_active_state(evicted_session)
                evicted_session._deferred_story_task = None
                evicted_session._deferred_story_source = None
                _sessions.pop(evicted_id, None)
                logger.info("会话淘汰 — save_id=%s (超出上限%d)", evicted_id, _MAX_SESSIONS)

        task = asyncio.create_task(retire())
        old_session._eviction_task = task

        def on_retired(done, evicted_id=old_id, evicted_session=old_session):
            evicted_session._eviction_task = None
            if not done.cancelled():
                error = done.exception()
                if error:
                    logger.error("会话淘汰持久化失败 — save=%s", evicted_id,
                                 exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(on_retired)
        excess -= 1


async def _persist_sessions_on_shutdown():
    retiring = [task for session in _sessions.values()
                if (task := getattr(session, "_eviction_task", None))]
    if retiring:
        await asyncio.gather(*retiring, return_exceptions=True)

    failed = []
    for save_id, session in list(_sessions.items()):
        try:
            pending = tuple(getattr(session, "_background_persist_tasks", ()))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            async with _get_session_lock(save_id):
                if _sessions.get(save_id) is not session:
                    continue
                await session._drain_background_tasks(timeout=None, include_index=False)
                session._consume_script_blueprint()
                pending_tasks = tuple(session._deferred_tasks)
                for task in pending_tasks:
                    if not task.done():
                        task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                session._consume_script_blueprint()
                await _persist_active_state(session)
                session._deferred_story_task = None
                session._deferred_story_source = None
        except Exception:
            logger.exception("关闭前持久化会话失败 — save=%s", save_id)
            failed.append(save_id)
    if failed:
        raise RuntimeError(f"Failed to persist {len(failed)} session(s) before shutdown")


async def _get_ai_provider_async():
    from api.config_routes import get_ai_provider_instance
    return await get_ai_provider_instance()


async def _get_stage_models_async():
    from api.config_routes import get_active_stage_models
    return await get_active_stage_models()


class NewGameRequest(BaseModel):
    script_id: str
    settings_overrides: Optional[dict] = None
    preset_id: Optional[str] = None
    custom_character: Optional[dict] = None
    opening_variant_id: Optional[str] = None
    authors_note: Optional[str] = None
    authors_note_position: Optional[str] = None
    authors_note_depth: Optional[int] = None
    npc_overrides: Optional[list[dict]] = None
    skip_opening: bool = False
    stream_opening: bool = False


class ActionRequest(BaseModel):
    type: str = "freeform"  # "choice" or "freeform"
    choice_id: Optional[str] = None
    text: Optional[str] = None
    intent_hint: Optional[str] = None
    target_id: Optional[str] = None
    stream_id: Optional[str] = None
    expected_node_id: Optional[str] = None
    action_id: Optional[str] = None


class AuthorsNoteRequest(BaseModel):
    note: str = ""
    position: str = "end"
    depth: int = 4


class NegativePromptRequest(BaseModel):
    text: str = ""


class LogitBiasRequest(BaseModel):
    entries: list[dict] = []  # [{"text": "暴力", "bias": -5}, ...]


class LorebookRecursionRequest(BaseModel):
    max_recursion: int = 4


class SwitchPersonaRequest(BaseModel):
    preset_id: str


class SwitchPovRequest(BaseModel):
    preset_id: str | None = None
    custom_character: dict | None = None


class NpcTalkRequest(BaseModel):
    message: str


class NpcDialogueRequest(BaseModel):
    topic: str = ""


@router.get("/classes/{system}")
async def list_classes(system: str):
    """List available classes for a game system (dnd5e or coc)."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    classes = registry.get_available_classes(system)
    return [{
        "id": c["id"],
        "name": c.get("name", c["id"]),
        "name_en": c.get("name_en", ""),
        "description": c.get("description", ""),
        "primary_attributes": c.get("primary_attributes", []),
        "hit_die": c.get("hit_die"),
        "narrative_tags": c.get("narrative_tags", []),
        "skill_proficiencies_choose": c.get("skill_proficiencies_choose"),
    } for c in classes]


@router.get("/classes/{system}/{class_id}")
async def get_class_detail(system: str, class_id: str):
    """Get detailed class information including skill tree."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    cls = registry.get_class(class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    return cls


@router.get("/skills/{system}")
async def list_skills(system: str):
    """List all skills for a game system."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    skills = registry.get_skills_for_system(system)
    return [{
        "id": s["id"],
        "name": s.get("name", s["id"]),
        "name_en": s.get("name_en", ""),
        "parent_attribute": s.get("parent_attribute", ""),
        "description": s.get("description", ""),
    } for s in skills]


@router.get("/opening-variants/{script_id}")
async def get_opening_variants(script_id: str):
    """List available opening variants for a script."""
    async with get_db() as db:
        cursor = await db.execute("SELECT content FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()
    if row:
        script = json.loads(row["content"])
    else:
        try:
            script = ScriptLoader.load(script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")
    variants = script.get("opening_variants", [])
    return [{"id": v.get("id", ""), "label": v.get("label", v.get("id", "")), "preview": v.get("text", "")[:200]} for v in variants]


@router.post("/new")
async def new_game(req: NewGameRequest):
    """Start a new game from a script."""
    logger.info("新游戏请求 — 剧本=%s", req.script_id)
    # Try loading from DB first, then from file
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT content FROM scripts WHERE id = ?", (req.script_id,)
        )
        row = await cursor.fetchone()

    file_script_content = None
    if row:
        script = json.loads(row["content"])
    else:
        try:
            script = ScriptLoader.load(req.script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")
        file_script_content = json.dumps(script, ensure_ascii=False)

    # Apply settings overrides
    if req.settings_overrides:
        script.setdefault("settings", {}).update(req.settings_overrides)

    # Apply character selection (preset or custom)
    if not req.preset_id and req.custom_character is not None and not (
        isinstance(req.custom_character.get('name'), str) and req.custom_character['name'].strip()
    ):
        raise HTTPException(status_code=400, detail='请输入自建角色名')
    if req.preset_id:
        presets = script.get("player_presets", [])
        preset = next((p for p in presets if p.get("id") == req.preset_id), None)
        if not preset:
            raise HTTPException(status_code=400, detail="预设角色不存在")
        if preset:
            pc = script.setdefault("player_character", {})
            pc["id"] = preset["id"]
            pc["name"] = preset.get("name", "")
            pc["bio"] = preset.get("bio", "")
            if preset.get("personality"):
                pc["personality"] = preset["personality"]
            if preset.get("portrait_desc"):
                pc["portrait_desc"] = preset["portrait_desc"]
            if preset.get("initial_location"):
                pc["initial_location"] = preset["initial_location"]
            if preset.get("long_term_goal"):
                pc["long_term_goal"] = preset["long_term_goal"]
            # Merge attribute overrides
            if preset.get("attributes"):
                base_attrs = pc.get("attributes", {})
                # Build reverse lookup: display_name → key for fuzzy matching
                _dn_to_key = {}
                for bk, bv in base_attrs.items():
                    if isinstance(bv, dict):
                        dn = bv.get("display_name") or bv.get("name", "")
                        if dn:
                            _dn_to_key[dn] = bk
                for k, v in preset["attributes"].items():
                    # Try direct key match first, then display_name reverse lookup
                    target_key = k if k in base_attrs else _dn_to_key.get(k, k)
                    if target_key in base_attrs:
                        if isinstance(base_attrs[target_key], dict):
                            base_attrs[target_key]["value"] = v if isinstance(v, (int, float)) else v.get("value", base_attrs[target_key].get("value", 50))
                        else:
                            base_attrs[target_key] = v
                    else:
                        base_attrs[target_key] = v
                pc["attributes"] = base_attrs
            # C4: pass preset known_npcs for social network filtering
            if preset.get("known_npcs"):
                script["_preset_known_npcs"] = preset["known_npcs"]
            # C5: pass preset persistent_state_overrides
            if preset.get("persistent_state_overrides"):
                script["_preset_ps_overrides"] = preset["persistent_state_overrides"]
            # C6: pass preset opening text override
            if preset.get("opening_text"):
                script["_preset_opening_text"] = preset["opening_text"]
            # C7: pass preset opening choices override
            if preset.get("opening_choices"):
                script["_preset_opening_choices"] = preset["opening_choices"]
            # Class system: pass class_id, level, skill_proficiencies to player_character
            if preset.get("class_id"):
                pc["class_id"] = preset["class_id"]
                pc["level"] = preset.get("level", 1)
                if preset.get("skill_proficiencies"):
                    pc["skill_proficiencies"] = preset["skill_proficiencies"]
            # C8: preset NPC overrides
            if preset.get("npc_overrides"):
                for override in preset["npc_overrides"]:
                    npc_id = override.get("id")
                    if not npc_id:
                        continue
                    for npc in script.get("npcs", []):
                        if npc["id"] == npc_id:
                            if override.get("name"):
                                npc["name"] = override["name"]
                            if override.get("personality"):
                                npc["personality"] = override["personality"]
                            if override.get("bio"):
                                npc["bio"] = override["bio"]
                            break
            logger.info("使用预设角色 — preset_id=%s, name=%s", req.preset_id, preset.get("name"))
    elif req.custom_character:
        pc = script.setdefault("player_character", {})
        if req.custom_character.get("name"):
            pc["name"] = req.custom_character["name"]
        if req.custom_character.get("bio"):
            pc["bio"] = req.custom_character["bio"]
        if req.custom_character.get("personality"):
            pc["personality"] = req.custom_character["personality"]
        if req.custom_character.get("portrait_desc"):
            pc["portrait_desc"] = req.custom_character["portrait_desc"]
        if req.custom_character.get("long_term_goal"):
            pc["long_term_goal"] = req.custom_character["long_term_goal"]
        if req.custom_character.get("initial_location"):
            pc["initial_location"] = req.custom_character["initial_location"]
        # Apply custom attribute values
        if req.custom_character.get("attributes"):
            base_attrs = pc.get("attributes", {})
            _dn_to_key = {}
            for bk, bv in base_attrs.items():
                if isinstance(bv, dict):
                    dn = bv.get("display_name") or bv.get("name", "")
                    if dn:
                        _dn_to_key[dn] = bk
            for k, v in req.custom_character["attributes"].items():
                target_key = k if k in base_attrs else _dn_to_key.get(k, k)
                if target_key in base_attrs and isinstance(base_attrs[target_key], dict):
                    base_attrs[target_key]["value"] = v
                elif target_key in base_attrs:
                    base_attrs[target_key] = v
            pc["attributes"] = base_attrs
        # Class system for custom character
        if req.custom_character.get("class_id"):
            pc["class_id"] = req.custom_character["class_id"]
            pc["level"] = req.custom_character.get("level", 1)
            if req.custom_character.get("skill_proficiencies"):
                pc["skill_proficiencies"] = req.custom_character["skill_proficiencies"]
        logger.info("自建角色 — name=%s", req.custom_character.get("name", "?"))

    player_name = script.get("player_character", {}).get("name")
    if not isinstance(player_name, str) or not player_name.strip():
        raise HTTPException(status_code=400, detail="请先选择预设角色或创建自建角色")

    # C9: request-level NPC overrides (player customization at game start)
    if req.npc_overrides:
        # Collect IDs to delete
        delete_ids = set()
        add_npcs = []
        for override in req.npc_overrides:
            npc_id = override.get("id")
            if not npc_id:
                continue
            if override.get("_delete"):
                delete_ids.add(npc_id)
                continue
            if override.get("_new"):
                add_npcs.append({
                    "id": npc_id,
                    "name": override.get("name", npc_id),
                    "personality": override.get("personality", ""),
                    "bio": override.get("bio", ""),
                    "role": override.get("role", ""),
                    "location": override.get("location", ""),
                })
                continue
            for npc in script.get("npcs", []):
                if npc["id"] == npc_id:
                    if override.get("name"):
                        npc["name"] = override["name"]
                    if override.get("personality"):
                        npc["personality"] = override["personality"]
                    if override.get("bio"):
                        npc["bio"] = override["bio"]
                    break
        if delete_ids:
            script["npcs"] = [n for n in script.get("npcs", []) if n.get("id") not in delete_ids]
        if add_npcs:
            script.setdefault("npcs", []).extend(add_npcs)

    # Validate
    errors = ScriptLoader.validate(script)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # Opening variant selection
    if req.opening_variant_id:
        script["_selected_opening_variant"] = req.opening_variant_id
    effective_script = json.dumps(script, ensure_ascii=False)

    # Create session
    ai_provider = await _get_ai_provider_async()
    stage_models = await _get_stage_models_async()
    session = GameSession(script, ai_provider, stage_models=stage_models)
    session._last_activity_time = time.time()
    stream_opening = req.stream_opening and (session._agentic_enabled() or session._agentic_unified_enabled())

    # Inject image provider if configured
    try:
        from api.config_routes import get_image_provider_instance
        session._image_provider = await get_image_provider_instance()
    except Exception:
        session._image_provider = None

    # Apply authors_note before initialization so opening generation respects it
    an_text = req.authors_note or script.get("settings", {}).get("authors_note", "")
    if an_text:
        session.set_authors_note(
            an_text,
            req.authors_note_position or script.get("settings", {}).get("authors_note_position", "end"),
            req.authors_note_depth or script.get("settings", {}).get("authors_note_depth", 4),
        )

    result = await session.initialize(skip_opening=req.skip_opening or stream_opening)
    # G1: 注入 skill_check_map 到初始 state
    skill_map = script.get("settings", {}).get("skill_check_map")
    if skill_map:
        result["state"]["_skill_check_map"] = skill_map
    logger.info("游戏初始化完成 — save_id=%s, 地点=%s",
                session.save_id, result["state"].get("player", {}).get("location", "?"))

    # Save to DB
    async with get_db() as db:
        if file_script_content is not None:
            await db.execute(
                """INSERT INTO scripts (id, name, content) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (req.script_id, script.get("script_name", req.script_id), file_script_content),
            )
        await db.execute(
            """INSERT INTO saves (id, script_id, name, active_node_id, total_nodes,
                                  authors_note, authors_note_position, authors_note_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.save_id,
                req.script_id,
                script.get("script_name", req.script_id),
                result["node_id"],
                0,
                session.authors_note,
                session.authors_note_position,
                session.authors_note_depth,
            ),
        )
        await db.execute(
            "INSERT INTO save_scripts (save_id, content) VALUES (?, ?)",
            (session.save_id, effective_script),
        )
        await db.commit()
    scene_image_path = await _save_node(session, result)
    _sessions_put(session.save_id, session)
    if scene_image_path:
        result["scene_image_path"] = scene_image_path
    result.pop("scene_image", None)
    result["state"] = _enrich_state(session, result["state"])
    result["opening_stream_required"] = stream_opening

    return result


def _validate_action_choice(session: GameSession, action: dict):
    if action["type"] == "choice":
        try:
            session._validate_choice(action["choice_id"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_action_base(session: GameSession, expected_node_id: Optional[str]):
    if expected_node_id is not None and expected_node_id != session.world_tree.active_node_id:
        raise HTTPException(status_code=409, detail="存档回合已变化，请重新加载存档后再行动")


async def _validate_action_request(session: GameSession, req: ActionRequest):
    if req.action_id:
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT 1 FROM action_requests WHERE save_id = ? AND action_id = ?",
                (session.save_id, req.action_id),
            )
            if await cursor.fetchone():
                raise HTTPException(status_code=409, detail="此行动已处理，请重新加载存档查看结果")
    _validate_action_base(session, req.expected_node_id)


async def _record_action_request(db, save_id: str, action_id: Optional[str]):
    if action_id:
        await db.execute(
            "INSERT INTO action_requests (save_id, action_id) VALUES (?, ?)",
            (save_id, action_id),
        )


async def _pending_action_plan(save_id: str, req: ActionRequest):
    if not req.action_id:
        return None
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM pending_action_plans WHERE save_id = ? AND action_id = ?",
            (save_id, req.action_id),
        )
        row = await cursor.fetchone()
    if row and row["request"] != json.dumps(req.model_dump(), sort_keys=True, ensure_ascii=False):
        raise HTTPException(status_code=409, detail="此行动的重连参数不一致")
    return row


async def _pending_action_injections(save_id: str, req: ActionRequest, node_id: str) -> list[str]:
    if not req.action_id:
        return []
    async with get_db() as db:
        await db.execute(
            "DELETE FROM pending_action_plans WHERE save_id = ? AND action_id <> ?",
            (save_id, req.action_id),
        )
        await db.execute(
            "DELETE FROM pending_action_injections WHERE save_id = ? AND action_id <> ?",
            (save_id, req.action_id),
        )
        await db.commit()
        cursor = await db.execute(
            """SELECT request, base_node_id, message FROM pending_action_injections
               WHERE save_id = ? AND action_id = ? ORDER BY id""",
            (save_id, req.action_id),
        )
        rows = await cursor.fetchall()
    request = json.dumps(req.model_dump(), sort_keys=True, ensure_ascii=False)
    if any(row["request"] != request or row["base_node_id"] != node_id for row in rows):
        raise HTTPException(status_code=409, detail="此行动的重连参数不一致")
    return [row["message"] for row in rows]


def _pending_action_context(request: dict, base_node_id: str) -> dict | None:
    if not request.get("action_id") or not request.get("stream_id"):
        return None
    action = {key: request[key] for key in ("type", "choice_id", "text", "intent_hint", "target_id")
              if request.get(key) is not None}
    return {"action": action, "actionId": request["action_id"],
            "streamId": request["stream_id"], "baseNodeId": base_node_id}


async def _stored_pending_action(save_id: str, node_id: str) -> dict | None:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT plan.request FROM pending_action_plans AS plan
               WHERE plan.save_id = ? AND plan.base_node_id = ?
                 AND NOT EXISTS (SELECT 1 FROM action_requests AS done
                                 WHERE done.save_id = plan.save_id AND done.action_id = plan.action_id)""",
            (save_id, node_id),
        )
        row = await cursor.fetchone()
        if row is None:
            cursor = await db.execute(
                """SELECT injection.request FROM pending_action_injections AS injection
                   WHERE injection.save_id = ? AND injection.base_node_id = ?
                     AND NOT EXISTS (SELECT 1 FROM action_requests AS done
                                     WHERE done.save_id = injection.save_id
                                       AND done.action_id = injection.action_id)
                   ORDER BY injection.id LIMIT 1""",
                (save_id, node_id),
            )
            row = await cursor.fetchone()
    return _pending_action_context(json.loads(row["request"]), node_id) if row else None


async def _store_action_plan(session: GameSession, req: ActionRequest, chunk: dict,
                             previous, allow_recovery: bool):
    signature = (getattr(session, "_pending_agent_plan_signature", None)
                 or hashlib.sha256(json.dumps([chunk.get("plan", ""), chunk.get("questions", [])],
                                            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest())
    reply = (previous["reply"] if allow_recovery and previous
             and previous["plan_signature"] == signature
             and previous["base_node_id"] == session.world_tree.active_node_id else None)
    async with get_db() as db:
        await db.execute(
            """INSERT INTO pending_action_plans
               (save_id, action_id, request, base_node_id, plan_id, plan_signature, reply)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(save_id) DO UPDATE SET
               action_id=excluded.action_id, request=excluded.request,
               base_node_id=excluded.base_node_id,
               plan_id=excluded.plan_id, plan_signature=excluded.plan_signature,
               reply=excluded.reply""",
            (session.save_id, req.action_id,
             json.dumps(req.model_dump(), sort_keys=True, ensure_ascii=False),
             session.world_tree.active_node_id, chunk["plan_id"], signature, reply),
        )
        await db.commit()
    session._pending_agent_action_id = req.action_id
    return reply


async def _clear_action_plan(save_id: str, action_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM pending_action_plans WHERE save_id = ? AND action_id = ?",
                         (save_id, action_id))
        await db.commit()


async def _clear_action_injections(save_id: str, action_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM pending_action_injections WHERE save_id = ? AND action_id = ?",
                         (save_id, action_id))
        await db.commit()


@router.post("/{save_id}/action")
async def game_action(save_id: str, req: ActionRequest):
    """Submit a player action."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        action = {
            "type": req.type,
            "choice_id": req.choice_id,
            "text": req.text or req.choice_id or "",
            **({"intent_hint": req.intent_hint} if req.intent_hint else {}),
            **({"target_id": req.target_id} if req.target_id else {}),
        }
        await _validate_action_request(session, req)
        _validate_action_choice(session, action)

        logger.info("玩家行动 [%s] save=%s — %s: %s",
                    session.turn_number + 1, save_id, req.type,
                    (req.text or req.choice_id or "")[:60])
        snapshot = None if isinstance(session, GameSession) else _capture_generation_state(session)
        previous_background_tasks = (
            bool(session._background_tasks - session._index_tasks - session._deferred_tasks)
            if isinstance(session, GameSession) else False
        )
        previous_blueprint = copy.deepcopy(session.current_state.get("plot_blueprint"))

        def capture_after_background():
            nonlocal snapshot
            snapshot = _capture_generation_state(session)

        try:
            if isinstance(session, GameSession):
                result = await session.process_action(action, on_background_drained=capture_after_background)
            else:
                result = await session.process_action(action)
            if not result or result.get("error"):
                raise HTTPException(status_code=400, detail=(result or {}).get("error", "无法完成行动"))
            logger.info("AI回复完成 — 选项数=%d, 状态变更=%d, 游戏时间=%s",
                        len(result.get("choices", [])),
                        len(result.get("state_changes", [])),
                        result.get("state", {}).get("game_time", "?"))

            # B6: 持久化必须在锁内执行，否则可能与其它请求并发写 DB
            if result.get("removed_node_id"):
                await _persist_undo(session, result, req.action_id)
            elif result.get("query"):
                if (previous_background_tasks or
                        session.current_state.get("plot_blueprint") != previous_blueprint):
                    await _persist_active_state(session)
                await _persist_query_exchange(session, action["text"], result.get("reply", ""), req.action_id)
            elif not result.get("query"):
                await _save_node(session, result, req.action_id)
        except IncompleteAgentTurnError as exc:
            if snapshot is not None:
                await _rollback_generation_state(session, snapshot)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BaseException:
            if snapshot is not None:
                await _rollback_generation_state(session, snapshot)
            raise

    return _public_result(result, session)


@router.post("/{save_id}/action/stream")
async def game_action_stream(save_id: str, req: ActionRequest):
    """Submit a player action with streaming response."""
    existing = _existing_action_stream(save_id, req)
    if existing is not None:
        return existing
    lock = _get_session_lock(save_id)
    # Acquire lock before session lookup; held through the entire stream
    await lock.acquire()

    # P1-3: lock.acquire() 和 StreamingResponse 之间的异常会导致 lock 泄露。
    # 将整个 setup 阶段包在 try 中，异常时释放锁。
    try:
        existing = _existing_action_stream(save_id, req)
        if existing is not None:
            lock.release()
            return existing
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        action = {
            "type": req.type,
            "choice_id": req.choice_id,
            "text": req.text or req.choice_id or "",
            **({"intent_hint": req.intent_hint} if req.intent_hint else {}),
            **({"target_id": req.target_id} if req.target_id else {}),
        }
        await _validate_action_request(session, req)
        _validate_action_choice(session, action)

        logger.info("流式行动 [%s] save=%s — %s: %s",
                    session.turn_number + 1, save_id, req.type,
                    (req.text or req.choice_id or "")[:60])

        previous_plan = (await _pending_action_plan(save_id, req)
                         if isinstance(session, GameSession) else None)
        previous_injections = (await _pending_action_injections(
            save_id, req, session.world_tree.active_node_id)
            if isinstance(session, GameSession) else [])

        persisted = False
        snapshot = None if isinstance(session, GameSession) else _capture_generation_state(session)
        previous_background_tasks = (
            tuple(session._background_tasks - session._index_tasks - session._deferred_tasks)
            if isinstance(session, GameSession) else ()
        )
        previous_blueprint = copy.deepcopy(session.current_state.get("plot_blueprint"))
        previous_node_id = session.world_tree.active_node_id
    except BaseException:
        lock.release()
        raise

    async def event_stream():
        nonlocal persisted, snapshot
        recovery_checked = False
        plan_recorded = False
        auto_reply_waiting = False
        injections_recovered = False

        def capture_after_background():
            nonlocal snapshot
            snapshot = _capture_generation_state(session)

        if isinstance(session, GameSession):
            action_stream = session.process_action_stream(action, on_background_drained=capture_after_background)
        else:
            action_stream = session.process_action_stream(action)
        try:
            async for chunk in action_stream:
                recovered_reply = None
                if (chunk["type"] == "agent_plan" and chunk.get("plan_id")
                        and req.action_id and isinstance(session, GameSession)):
                    recovered_reply = await _store_action_plan(
                        session, req, chunk, previous_plan, not recovery_checked)
                    recovery_checked = True
                    plan_recorded = True
                    session._pending_agent_plan_signature = None
                    if recovered_reply is not None:
                        auto_reply_waiting = True
                        session._pending_agent_plan_id = None
                        session._queued_agent_plan_reply_id = chunk["plan_id"]
                        session._inject_queue.insert(0, recovered_reply)
                        control_event = getattr(session, "_agent_control_event", None)
                        if control_event is not None:
                            control_event.set()
                if chunk["type"] == "agent_waiting" and auto_reply_waiting:
                    auto_reply_waiting = False
                    continue
                if chunk["type"] == "aborted":
                    session._abort_request_pending = False
                    if plan_recorded:
                        await _clear_action_plan(save_id, req.action_id)
                    if req.action_id and isinstance(session, GameSession):
                        await _clear_action_injections(save_id, req.action_id)
                if chunk["type"] == "agent_controls_ready":
                    if not getattr(session, "_active_agent_stream", False):
                        _set_agent_stream_active(session, True, req.stream_id)
                    session._active_agent_action_id = req.action_id
                    session._agent_inject_enabled = True
                    if not injections_recovered:
                        session._inject_queue.extend(previous_injections)
                        injections_recovered = True
                        if previous_injections:
                            session._agent_control_event.set()
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif chunk["type"] == "agent_controls_closed":
                    for control_event in _close_agent_control_events(session):
                        yield f"data: {json.dumps(control_event, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif chunk["type"] == "heartbeat":
                    yield ": heartbeat\n\n"
                elif chunk["type"] in ("text", "thinking", "narrative_revised"):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif chunk["type"] in ("preflight", "model_output_started", "tool_preparing", "tool_started", "tool_call", "agent_done", "aborted",
                                       "user_inject", "agent_max_rounds", "agent_plan",
                                       "agent_waiting", "model_waiting", "plan_auto_confirmed"):
                    yield f"data: {json.dumps(_public_agent_progress(chunk), ensure_ascii=False)}\n\n"
                    if recovered_reply is not None:
                        yield f"data: {json.dumps({'type': 'plan_auto_confirmed', 'plan_id': chunk['plan_id']}, ensure_ascii=False)}\n\n"
                elif chunk["type"] == "final":
                    logger.info("流式回复完成 — 选项数=%d, 状态变更=%d",
                                len(chunk.get("choices", [])),
                                len(chunk.get("state_changes", [])))
                    if chunk.get("is_undo"):
                        await _persist_undo(session, chunk, req.action_id)
                    elif chunk.get("is_query"):
                        if (previous_background_tasks or
                                session.current_state.get("plot_blueprint") != previous_blueprint):
                            await _persist_active_state(session)
                        await _persist_query_exchange(session, action["text"], chunk.get("narrative", ""), req.action_id)
                    elif not chunk.get("is_query"):
                        await _save_node(session, chunk, req.action_id)
                    persisted = True
                    if plan_recorded:
                        await _clear_action_plan(save_id, req.action_id)
                    if req.action_id and isinstance(session, GameSession):
                        await _clear_action_injections(save_id, req.action_id)
                    yield f"data: {json.dumps(_public_result(chunk, session), ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
        finally:
            await action_stream.aclose()

    async def saving_event_stream():
        _set_agent_stream_active(session, False)
        try:
            stop_available = (isinstance(session, GameSession)
                              and (session._agentic_enabled() or session._agentic_unified_enabled()))
            if stop_available:
                _set_agent_stream_active(session, True, req.stream_id)
                session._active_agent_action_id = req.action_id
                session._agent_inject_enabled = False
            ready = {"type": "stream_ready", "controls": False}
            if stop_available:
                ready["stop_available"] = True
            yield f"data: {json.dumps(ready)}\n\n"
            events = event_stream()
            try:
                async for data in events:
                    yield data
            except IncompleteAgentTurnError as exc:
                logger.warning("统一Agent未完成结算 — save=%s: %s", save_id, exc)
                if getattr(session, "_inject_queue", None):
                    for control_event in _close_agent_control_events(session):
                        yield f"data: {json.dumps(control_event, ensure_ascii=False)}\n\n"
                    yield 'data: {"type": "agent_controls_closed"}\n\n'
                error = {"type": "error", "message": str(exc)}
                if isinstance(exc, (NarrativeSettlementError, PendingConsequenceDisclosureError, SemanticReviewError,
                                    MapTravelSettlementError)):
                    error["discard_partial"] = True
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
            except Exception:
                logger.exception("流式行动失败 — save=%s", save_id)
                if getattr(session, "_inject_queue", None):
                    for control_event in _close_agent_control_events(session):
                        yield f"data: {json.dumps(control_event, ensure_ascii=False)}\n\n"
                    yield 'data: {"type": "agent_controls_closed"}\n\n'
                error = {"type": "error", "message": "生成失败，请稍后重试", "discard_partial": True}
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
            finally:
                await events.aclose()
        finally:
            try:
                if not persisted and snapshot is not None:
                    logger.warning("流式回合未持久化 — save=%s, 回滚 turn=%d",
                                   save_id, session.turn_number)
                    await _rollback_generation_state(session, snapshot)
                if not persisted and previous_background_tasks:
                    async def persist_previous_background():
                        await asyncio.gather(*previous_background_tasks, return_exceptions=True)
                        async with _get_session_lock(save_id):
                            if (session.world_tree.active_node_id == previous_node_id
                                    and _sessions.get(save_id, session) is session):
                                await _persist_active_state(session)

                    task = asyncio.create_task(persist_previous_background())
                    pending = getattr(session, "_background_persist_tasks", None)
                    if pending is None:
                        pending = session._background_persist_tasks = set()
                    pending.add(task)

                    def on_persist_done(done):
                        pending.discard(done)
                        if not done.cancelled():
                            error = done.exception()
                            if error:
                                logger.error("后台结算持久化失败 — save=%s", save_id,
                                             exc_info=(type(error), error, error.__traceback__))

                    task.add_done_callback(on_persist_done)
            finally:
                _set_agent_stream_active(session, False)
                lock.release()

    if not req.action_id:
        return StreamingResponse(saving_event_stream(), media_type="text/event-stream")

    try:
        journal = _ActionStreamJournal(req.model_dump())
        if isinstance(session, GameSession):
            journal.snapshot = {
                "state": copy.deepcopy(_enrich_state(session, session.current_state)),
                "node_id": session.world_tree.active_node_id,
                "history": copy.deepcopy(_history_response(session, 10)),
            }
        _action_streams[(save_id, req.action_id)] = journal
    except BaseException:
        lock.release()
        raise

    async def run_action():
        try:
            async for event in saving_event_stream():
                journal.append(event)
        except Exception as exc:
            logger.exception("流式行动失败 — save=%s, action=%s", save_id, req.action_id)
            journal.append(f"data: {json.dumps({'type': 'error', 'message': str(exc), 'discard_partial': True}, ensure_ascii=False)}\n\n")
        finally:
            journal.failed = not persisted
            journal.finish()

    asyncio.create_task(run_action())
    return _replay_action_stream(journal)


class StreamControlRequest(BaseModel):
    stream_id: str


class InjectMessageRequest(StreamControlRequest):
    message: str
    plan_id: Optional[str] = None


@router.post("/{save_id}/abort")
async def abort_generation(save_id: str, req: StreamControlRequest):
    """Signal the running agentic loop to cancel its current model call and stop."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    if not getattr(session, "_active_agent_stream", False) or req.stream_id != session._active_agent_stream_id:
        raise HTTPException(status_code=409, detail="没有正在运行的流式回合")
    session._abort_flag = True
    session._abort_request_pending = True
    stop_event = getattr(session, "_agent_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    control_event = getattr(session, "_agent_control_event", None)
    if control_event is not None:
        control_event.set()
    return {"requested": True}


@router.post("/{save_id}/inject")
async def inject_message(save_id: str, req: InjectMessageRequest):
    """Inject a user message into the running agentic loop between rounds."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    if (not getattr(session, "_active_agent_stream", False)
            or not getattr(session, "_agent_inject_enabled", True)
            or getattr(session, "_abort_flag", False)
            or req.stream_id != session._active_agent_stream_id):
        raise HTTPException(status_code=409, detail="没有正在运行的流式回合")
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="插话内容不能为空")
    if req.plan_id is not None:
        if req.plan_id != getattr(session, "_pending_agent_plan_id", None):
            raise HTTPException(status_code=409, detail="计划已失效，请等待当前计划")
        if session._inject_queue:
            raise HTTPException(status_code=409, detail="已有待处理插话，请等待 Agent 继续")
        action_id = getattr(session, "_pending_agent_action_id", None)
        if action_id and isinstance(session, GameSession):
            async with get_db() as db:
                cursor = await db.execute(
                    """UPDATE pending_action_plans SET reply = ?
                       WHERE save_id = ? AND action_id = ? AND plan_id = ? AND reply IS NULL""",
                    (req.message, save_id, action_id, req.plan_id),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="计划已失效，请等待当前计划")
                await db.commit()
            if (_sessions.get(save_id) is not session
                    or not session._active_agent_stream
                    or not session._agent_inject_enabled
                    or getattr(session, "_abort_flag", False)
                    or req.stream_id != session._active_agent_stream_id
                    or action_id != session._active_agent_action_id
                    or action_id != session._pending_agent_action_id
                    or req.plan_id != session._pending_agent_plan_id
                    or session._inject_queue):
                async with get_db() as db:
                    await db.execute(
                        """UPDATE pending_action_plans SET reply = NULL
                           WHERE save_id = ? AND action_id = ? AND plan_id = ? AND reply = ?""",
                        (save_id, action_id, req.plan_id, req.message),
                    )
                    await db.commit()
                raise HTTPException(status_code=409, detail="计划已失效，请等待当前计划")
        session._pending_agent_plan_id = None
        session._queued_agent_plan_reply_id = req.plan_id
    else:
        action_id = getattr(session, "_active_agent_action_id", None)
        journal = _action_streams.get((save_id, action_id)) if action_id else None
        if isinstance(session, GameSession) and journal is not None:
            async with get_db() as db:
                cursor = await db.execute(
                    """INSERT INTO pending_action_injections
                       (save_id, action_id, request, base_node_id, message)
                       VALUES (?, ?, ?, ?, ?)""",
                    (save_id, action_id,
                     json.dumps(journal.request, sort_keys=True, ensure_ascii=False),
                     session.world_tree.active_node_id, req.message),
                )
                injection_id = cursor.lastrowid
                await db.commit()
            if (_sessions.get(save_id) is not session
                    or not session._active_agent_stream
                    or not session._agent_inject_enabled
                    or getattr(session, "_abort_flag", False)
                    or req.stream_id != session._active_agent_stream_id
                    or action_id != session._active_agent_action_id
                    or _action_streams.get((save_id, action_id)) is not journal
                    or journal.done):
                async with get_db() as db:
                    await db.execute("DELETE FROM pending_action_injections WHERE id = ?", (injection_id,))
                    await db.commit()
                raise HTTPException(status_code=409, detail="没有正在运行的流式回合")
    session._inject_queue.append(req.message)
    control_event = getattr(session, "_agent_control_event", None)
    if control_event is not None:
        control_event.set()
    return {"queued": True}


@router.get("/{save_id}/settlement-messages")
async def get_settlement_messages(save_id: str):
    """D3: Get pending settlement messages from background agent."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    messages = getattr(session, '_settlement_messages', [])
    session._settlement_messages = []  # drain
    return {"messages": messages}


@router.get("/{save_id}/state")
async def get_state(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return _enrich_state(session, session.current_state)


def _private_world_property_names(script: dict) -> set[str]:
    names = {
        name for prop in script.get("world_properties", [])
        if prop.get("visibility") in ("world", "hidden")
        for name in (prop.get("id"), prop.get("name")) if name
    }
    names.update(script.get("visibility_policy", {}).get("private_world_properties", []))
    return names


def _player_state_changes(changes: list, script: dict, state: dict) -> list:
    private_targets = {
        f"{prefix}.{name}" for prefix in ("world", "world_properties")
        for name in _private_world_property_names(script)
    }
    visible_npc_fields = {"name", "known", "met", "attitude_toward_player"}
    known_npcs = {
        npc_id for npc_id, npc in state.get("npcs", {}).items()
        if isinstance(npc, dict) and npc.get("known") is not False
    }
    public_changes = []
    for change in changes:
        if isinstance(change, dict):
            if change.get("_source_event_id") and not is_public_event(script, change["_source_event_id"]):
                continue
            target = change.get("target")
            if target in private_targets:
                continue
            if isinstance(target, str) and target.startswith("npcs."):
                parts = target.split(".")
                if (len(parts) != 3 or parts[1] not in known_npcs
                        or parts[2] not in visible_npc_fields):
                    continue
            change = {key: value for key, value in change.items() if key != "_source_event_id"}
        public_changes.append(change)
    return public_changes


def _player_expired_states(expired: list, script: dict) -> list:
    private = {
        entry["id"] for entry in script.get("persistent_states", [])
        if entry.get("expiration_visibility") in ("world", "hidden")
    }
    private.update(script.get("visibility_policy", {}).get("private_expired_states", []))
    return [state_id for state_id in expired if state_id not in private]


def _player_triggered_events(events: list, script: dict, *, with_details: bool = False) -> list:
    public = [event for event in events if is_public_event(
        script, event.get("event_id", "") if isinstance(event, dict) else event
    )]
    if not with_details:
        return public
    definitions = {
        event["id"]: event for section in ("cyclic_events", "one_time_events")
        for event in script.get(section, []) if isinstance(event, dict) and event.get("id")
    }
    detailed = []
    for event in public:
        if not isinstance(event, str) or event not in definitions:
            detailed.append(event)
            continue
        definition = definitions[event]
        detailed.append({
            "event_id": event,
            "name": definition.get("name") or definition.get("title") or definition.get("description") or event,
            "description": definition.get("description", ""),
        })
    return detailed


def _player_adventure_log(log: list, script: dict) -> list:
    private_descriptions = {
        event.get("description") for section in ("cyclic_events", "one_time_events")
        for event in script.get(section, []) if not is_public_event(script, event.get("id", ""))
    }
    public_log = copy.deepcopy(log)
    for entry in public_log:
        if isinstance(entry, dict) and isinstance(entry.get("events"), list):
            entry["events"] = [event for event in entry["events"] if not (
                isinstance(event, dict) and event.get("type") == "event"
                and (not is_public_event(script, event.get("event_id", ""))
                     if event.get("event_id") else event.get("text") in private_descriptions)
            )]
    return public_log


def _player_state(current_state: dict, script: dict | None = None) -> dict:
    state = copy.deepcopy(current_state)
    script = script or {}
    if isinstance(state.get("pc_event_reports"), list):
        state["pc_event_reports"] = [
            {key: report[key] for key in ("npc_id", "npc_name", "evidence", "heard_at") if key in report}
            for report in state["pc_event_reports"] if isinstance(report, dict)
        ]
    if isinstance(state.get("adventure_log"), list):
        state["adventure_log"] = _player_adventure_log(state["adventure_log"], script)
    private_event_descriptions = {
        event.get("description", "")[:60]
        for section in ("cyclic_events", "one_time_events")
        for event in script.get(section, []) if not is_public_event(script, event.get("id", ""))
    }
    if isinstance(state.get("location_memory"), dict):
        state["location_memory"] = {
            location: [entry for entry in entries if not (
                isinstance(entry, dict) and entry.get("type") == "event"
                and entry.get("text") in private_event_descriptions
            )] if isinstance(entries, list) else entries
            for location, entries in state["location_memory"].items()
        }
    public_profiles = {
        npc["id"]: npc["public_profile"] for npc in script.get("npcs", [])
        if npc.get("public_profile")
    }
    for key in ("_pending_world_events", "npc_offscreen_log", "npc_relationships_global",
                "cyclic_event_trackers", "random_item_state", "fired_one_time_events",
                "lorebook_enabled_overrides", "lorebook_entry_overrides", "dynamic_lorebook", "script_variables",
                "dynamic_story_content", "mechanics_overrides", "_npc_autonomy", "_node_context",
                "pending_consequences", "events", "shelved_pc_data",
                "information_network"):
        state.pop(key, None)
    private_properties = _private_world_property_names(script)
    if "world_properties" in state:
        state["world_properties"] = {
            key: value for key, value in state["world_properties"].items()
            if key not in private_properties
        }
    player_location = state.get("player", {}).get("location")
    player_room = state.get("player", {}).get("current_room", "")
    contacts = current_state.get("npc_contacts", {})
    state["npc_contacts"] = {
        npc_id: True for npc_id, contact in contacts.items()
        if isinstance(contact, dict) and contact.get("location") == player_location
        and contact.get("player_room", "") == player_room
        and isinstance(current_state.get("npcs", {}).get(npc_id), dict)
        and current_state["npcs"][npc_id].get("known") is not False
        and current_state["npcs"][npc_id].get("met") is not False
        and current_state["npcs"][npc_id].get("current_location", player_location) == player_location
        and contact.get("npc_room", "") == current_state["npcs"][npc_id].get("current_room", "")
    } if isinstance(contacts, dict) else {}
    visible_npc_fields = ("name", "known", "met", "attitude_toward_player", "bio",
                          "personality", "title", "organizations", "superior", "opinion",
                          "relationship_desc")
    for npc_id, npc in state.get("npcs", {}).items():
        if not isinstance(npc, dict):
            continue
        if npc.get("known") is False:
            state["npcs"][npc_id] = {"known": False, "met": False}
            state.get("display_names", {}).pop(npc_id, None)
            continue
        visible_npc = {key: npc[key] for key in visible_npc_fields if key in npc}
        if npc_id in public_profiles or script.get("visibility_policy", {}).get("npc_public_profiles_only", False):
            for key in ("bio", "personality", "title", "organizations", "superior",
                        "opinion", "relationship_desc"):
                visible_npc.pop(key, None)
            visible_npc.update(copy.deepcopy(public_profiles.get(npc_id, {})))
        if player_location and npc.get("current_location") == player_location:
            for key in ("current_location", "current_room", "current_mood"):
                if key in npc:
                    visible_npc[key] = npc[key]
        state["npcs"][npc_id] = visible_npc
    known_npcs = {npc_id for npc_id, npc in state.get("npcs", {}).items()
                  if isinstance(npc, dict) and npc.get("known") is not False}
    scripted_relationships = {
        (rel.get("from", rel.get("a")), rel.get("to", rel.get("b"))): rel
        for rel in script.get("npc_relationships", [])
    }
    public_relationships = {}
    for key, rel in state.get("npc_relationships_known", {}).items():
        if not isinstance(rel, dict):
            continue
        first, second = rel.get("from", rel.get("a")), rel.get("to", rel.get("b"))
        if first not in known_npcs or second not in known_npcs:
            continue
        definition = scripted_relationships.get((first, second), {})
        if rel.get("_turn") is not None or definition.get("initially_known") is False:
            public_relationships[key] = rel
            continue
        public_rel = {field: rel[field] for field in ("from", "to", "a", "b") if field in rel}
        for field, source in (("description", "public_description"), ("type", "public_type")):
            if source in definition:
                public_rel[field] = definition[source]
        public_relationships[key] = public_rel
    if "npc_relationships_known" in state:
        state["npc_relationships_known"] = public_relationships

    visible_locations = set(state.get("visible_locations", []))
    if player_location:
        visible_locations.add(player_location)
    if "location_descriptions" in state:
        public_descriptions = {
            location["id"]: location["public_description"]
            for location in script.get("locations", []) if "public_description" in location
        }
        private_locations = set(script.get("visibility_policy", {}).get("private_location_descriptions", []))
        state["location_descriptions"] = {
            key: public_descriptions.get(key, value)
            for key, value in state["location_descriptions"].items()
            if key in visible_locations and (key not in private_locations or key in public_descriptions)
        }
    if "location_connections" in state:
        state["location_connections"] = {
            key: visible_targets for key, targets in state["location_connections"].items()
            if key in visible_locations and isinstance(targets, list)
            if (visible_targets := [target for target in targets if target in visible_locations])
        }
    active_states = set(state.get("active_persistent_states", []))
    if "persistent_state_descriptions" in state:
        state["persistent_state_descriptions"] = {
            key: value for key, value in state["persistent_state_descriptions"].items()
            if key in active_states
        }
    if "display_names" in state:
        public_ids = (visible_locations | known_npcs | active_states
                      | set(state.get("player", {}).get("attributes", {}))
                      | set(state.get("world_properties", {}))
                      | set(state.get("faction_reputation", {})))
        player_id = state.get("player", {}).get("id")
        if player_id:
            public_ids.add(player_id)
        for npc in state.get("npcs", {}).values():
            if not isinstance(npc, dict):
                continue
            for org in npc.get("organizations", []):
                org_id = org.get("org_id") if isinstance(org, dict) else org
                if org_id:
                    public_ids.add(org_id)
        state["display_names"] = {
            key: value for key, value in state["display_names"].items() if key in public_ids
        }
    return state


def _public_result(result: dict, session: GameSession) -> dict:
    if not isinstance(result, dict):
        return result
    public = result.copy()
    if isinstance(result.get("activated_lore"), list):
        state = result.get("state") if isinstance(result.get("state"), dict) else session.current_state
        discovered = set(state.get("pc_discovered_lore") or [])
        public["activated_lore"] = [entry for entry in result["activated_lore"]
                                    if isinstance(entry, dict) and entry.get("id") in discovered]
    if isinstance(result.get("state"), dict):
        public["state"] = _player_state(result["state"], session.script)
    if isinstance(result.get("state_changes"), list):
        state = result.get("state") if isinstance(result.get("state"), dict) else session.current_state
        public["state_changes"] = _player_state_changes(result["state_changes"], session.script, state)
    if isinstance(result.get("triggered_events"), list):
        public["triggered_events"] = _player_triggered_events(result["triggered_events"], session.script)
    if isinstance(result.get("expired_states"), list):
        public["expired_states"] = _player_expired_states(result["expired_states"], session.script)
    return public


def _public_agent_progress(chunk: dict) -> dict:
    if chunk.get("type") not in ("model_output_started", "tool_preparing", "tool_started", "tool_call", "agent_done"):
        return chunk
    keys = (("type", "label", "round") if chunk["type"] in ("model_output_started", "tool_preparing")
            else ("type", "label", "round", "tool_name") if chunk["type"] in ("tool_started", "tool_call")
            else ("type", "label", "round"))
    return {key: chunk[key] for key in keys if key in chunk}


def _enrich_state(session: GameSession, current_state: dict) -> dict:
    state = {**_player_state(current_state, session.script), "_pipeline_mode": session._get_pipeline_mode()}
    # G1: 注入 skill_check_map 供前端使用（构造新 dict 避免污染 session state）
    skill_map = session.script.get("settings", {}).get("skill_check_map")
    if skill_map:
        state = {**state, "_skill_check_map": skill_map}
    # Inject authors_note config for frontend restoration
    if session.authors_note:
        state["_authors_note"] = session.authors_note
        state["_authors_note_position"] = session.authors_note_position
        state["_authors_note_depth"] = session.authors_note_depth
    if session.negative_prompt:
        state["_negative_prompt"] = session.negative_prompt
    if session.logit_bias:
        state["_logit_bias"] = session.logit_bias
    # Expose lorebook max_recursion for frontend control
    recursion = session.script.get("settings", {}).get("lorebook_max_recursion")
    if recursion:
        state["_lorebook_max_recursion"] = recursion
    # Expose available POV targets for switching UI
    presets = session.script.get("player_presets", [])
    current_pc_id = state.get("player", {}).get("id", "player")
    # Fallback: match current PC by name if id is generic "player"
    current_pc_name = state.get("player", {}).get("name", "")
    existing_npcs = set(state.get("npcs", {}).keys())
    shelved = current_state.get("shelved_pc_data", {})
    available_povs = []
    for preset in presets:
        preset_id = preset["id"]
        if preset_id == current_pc_id or (current_pc_id == "player" and preset.get("name") == current_pc_name):
            continue
        if preset_id in shelved:
            available_povs.append({"id": preset_id, "name": preset.get("name", preset_id), "status": "shelved"})
        elif preset_id not in existing_npcs:
            available_povs.append({"id": preset_id, "name": preset.get("name", preset_id), "status": "available"})
    # Also list shelved PCs not in presets (custom characters previously played)
    for shelved_id, shelved_data in shelved.items():
        if shelved_id == current_pc_id:
            continue
        if not any(pov["id"] == shelved_id for pov in available_povs):
            available_povs.append({"id": shelved_id, "name": shelved_data.get("player", {}).get("name", shelved_id), "status": "shelved"})
    # Always expose POV switch if script has presets (custom char creation always available)
    if presets or available_povs:
        state["_available_povs"] = available_povs
        state["_pov_switches_remaining"] = 5 - len(state.get("pov_history", []))
        # Expose known organizations and titles for combo fields
        orgs_set = set()
        titles_set = set()
        for npc in state.get("npcs", {}).values():
            if not isinstance(npc, dict):
                continue
            if npc.get("title"):
                titles_set.add(npc["title"])
            for org in npc.get("organizations", []):
                org_id = org.get("org_id", "") if isinstance(org, dict) else org
                if org_id:
                    orgs_set.add(org_id)
        # Also get display names for orgs from faction_reputation or display_names
        display_names = state.get("display_names", {})
        state["_pov_orgs"] = [{"id": org_id, "name": display_names.get(org_id, org_id)} for org_id in sorted(orgs_set)]
        state["_pov_titles"] = sorted(titles_set)
    return state


@router.get("/{save_id}/tree")
async def get_tree(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.world_tree.get_tree_structure()


@router.get("/{save_id}/tree/{node_id}")
async def get_tree_node(save_id: str, node_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        node = session.world_tree.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        public_fields = ("id", "parent_id", "children_ids", "game_time", "turn_number",
                         "player_action", "ai_response", "choices_presented", "dice_rolls",
                         "state_changes", "triggered_events", "state_snapshot", "created_at",
                         "swipes", "active_swipe_index", "scene_image_path", "agent_trace")
        public_node = {key: node[key] for key in public_fields if key in node}
        if isinstance(node.get("state_snapshot"), dict):
            public_node["state_snapshot"] = _player_state(node["state_snapshot"], session.script)
        if isinstance(node.get("triggered_events"), list):
            public_node["triggered_events"] = _player_triggered_events(node["triggered_events"], session.script)
        if node.get("swipes"):
            swipe_fields = ("narrative", "choices", "state_changes", "state_snapshot",
                            "agent_trace", "game_time", "triggered_events", "dice_rolls",
                            "scene_image_path")
            public_node["swipes"] = [
                {key: swipe[key] for key in swipe_fields if key in swipe}
                for swipe in node["swipes"]
            ]
            for swipe in public_node["swipes"]:
                if isinstance(swipe.get("state_snapshot"), dict):
                    swipe["state_snapshot"] = _player_state(swipe["state_snapshot"], session.script)
                if isinstance(swipe.get("triggered_events"), list):
                    swipe["triggered_events"] = _player_triggered_events(swipe["triggered_events"], session.script)
                if isinstance(swipe.get("agent_trace"), dict):
                    swipe["agent_trace"] = _public_agent_trace(swipe["agent_trace"])
                if isinstance(swipe.get("state_changes"), list):
                    swipe["state_changes"] = _player_state_changes(
                        swipe["state_changes"], session.script,
                        swipe.get("state_snapshot") or {})
        if isinstance(node.get("agent_trace"), dict):
            public_node["agent_trace"] = _public_agent_trace(node["agent_trace"])
        if isinstance(node.get("state_changes"), list):
            public_node["state_changes"] = _player_state_changes(
                node["state_changes"], session.script, node.get("state_snapshot") or {})
        return public_node


@router.post("/{save_id}/branch/{node_id}")
async def branch_to(save_id: str, node_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if isinstance(session, GameSession):
            await session._drain_background_tasks()
        snapshot = _capture_generation_state(session)
        try:
            result = await session.branch_to_node(node_id)
            if not result:
                raise HTTPException(status_code=404, detail="Node not found")
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            # Bug#2: 同步 DB active_node_id，否则会话被 LRU 淘汰后再恢复会回到旧分支
            async with get_db() as db:
                await db.execute(
                    "UPDATE saves SET active_node_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (node_id, save_id),
                )
                await db.commit()
        except BaseException:
            await _rollback_generation_state(session, snapshot)
            raise
        logger.info("切换分支 — save=%s, node=%s, turn=%d",
                    save_id, node_id, session.turn_number)
    return _public_result(result, session)


@router.post("/{save_id}/toggle-dice")
async def toggle_dice(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        current = session.current_state.get("dice_check_enabled", True)
        await _set_state_flag(session, "dice_check_enabled", not current)
        return {"dice_check_enabled": not current}


def _history_response(session: GameSession, last: int, before_turn: int = 0) -> dict:
    branch = session.world_tree.get_active_branch()
    total = len(branch)
    if before_turn > 0:
        branch = [node for node in branch if node.get("turn_number", 0) < before_turn]
        branch = branch[-last:] if len(branch) > last else branch
    else:
        branch = branch[-last:] if len(branch) > last else branch
    entries = [
        {
            "id": node["id"],
            "turn": node["turn_number"],
            "time": node["game_time"],
            "action": node.get("player_action"),
            "narrative": node.get("ai_response", ""),
            "thinking": node.get("thinking", ""),
            "choices": node.get("choices_presented", []),
            "dice_rolls": node.get("dice_rolls", []),
            "state_changes": node.get("state_changes", []),
            "triggered_events": node.get("triggered_events", []),
            "check_result": node.get("check_result"),
            "triggered_consequences": node.get("triggered_consequences"),
            "achieved_milestones": node.get("achieved_milestones"),
            "scene_image_path": node.get("scene_image_path"),
            "agent_trace": node.get("agent_trace"),
            "query_exchanges": node.get("query_exchanges", []),
        }
        for node in branch
    ]
    for node, entry in zip(branch, entries):
        if isinstance(entry["triggered_events"], list):
            entry["triggered_events"] = _player_triggered_events(
                entry["triggered_events"], session.script, with_details=True)
        if isinstance(entry["agent_trace"], dict):
            entry["agent_trace"] = _public_agent_trace(entry["agent_trace"])
        if isinstance(entry["state_changes"], list):
            entry["state_changes"] = _player_state_changes(
                entry["state_changes"], session.script, node.get("state_snapshot") or {})
    active_node = session.world_tree.get_node(session.world_tree.active_node_id)
    swipe_index = 0
    total_swipes = 1
    if active_node:
        swipes = active_node.get("swipes", [])
        if swipes:
            swipe_index = active_node.get("active_swipe_index", 0)
            total_swipes = len(swipes)
    return {"entries": entries, "total": total, "swipe_index": swipe_index, "total_swipes": total_swipes}


async def _action_completed(save_id: str, action_id: Optional[str]) -> bool:
    if not action_id:
        return False
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT 1 FROM action_requests WHERE save_id = ? AND action_id = ?",
            (save_id, action_id),
        )).fetchone()
        return row is not None


@router.get("/{save_id}/snapshot")
async def get_save_snapshot(save_id: str, last: int = 10, action_id: Optional[str] = None):
    lock = _get_session_lock(save_id)
    if last == 10 and lock.locked():
        for (stream_save_id, _), journal in _action_streams.items():
            if stream_save_id == save_id and not journal.done and journal.snapshot is not None:
                snapshot = copy.deepcopy(journal.snapshot)
                snapshot["pending_action"] = _pending_action_context(journal.request, snapshot["node_id"])
                snapshot["action_completed"] = await _action_completed(save_id, action_id)
                return snapshot
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        node_id = session.world_tree.active_node_id
        return {"state": _enrich_state(session, session.current_state),
                "node_id": node_id, "history": _history_response(session, last),
                "pending_action": await _stored_pending_action(save_id, node_id),
                "action_completed": await _action_completed(save_id, action_id)}


@router.get("/{save_id}/history")
async def get_history(save_id: str, last: int = 10, before_turn: int = 0):
    """Return recent history. If before_turn > 0, return `last` entries before that turn."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return _history_response(session, last, before_turn)


@router.get("/{save_id}/adventure-log")
async def get_adventure_log(save_id: str):
    """Return the adventure log from the current game state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return _player_adventure_log(session.current_state.get("adventure_log", []), session.script)


@router.get("/{save_id}/summary")
async def get_summary(save_id: str):
    """Return the full history summary from the current game state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return {
            "summary": session.current_state.get("history_summary", ""),
            "turn": session.turn_number,
        }


@router.post("/{save_id}/summary/freeze")
async def toggle_summary_freeze(save_id: str, req: dict = {}):
    """Toggle the summary freeze state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        frozen = req.get("frozen", not session.current_state.get("summary_frozen", False))
        await _set_state_flag(session, "summary_frozen", bool(frozen))
        return {"frozen": session.current_state["summary_frozen"]}


@router.post("/{save_id}/use_item")
async def use_item(save_id: str, req: dict):
    """Use an item from inventory with deterministic effects."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        item_name = req.get("item_name", "")
        if not item_name:
            raise HTTPException(status_code=400, detail="item_name required")
        return await _run_state_action(session, lambda: session.use_item(item_name))


@router.post("/{save_id}/interact")
async def interact_object(save_id: str, req: dict):
    """Interact with a location interactable deterministically."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        iid = req.get("interactable_id", "")
        if not iid:
            raise HTTPException(status_code=400, detail="interactable_id required")
        return await _run_state_action(session, lambda: session.interact_with_object(iid))


@router.post("/{save_id}/talk/{npc_id}")
async def talk_to_npc(save_id: str, npc_id: str, req: NpcTalkRequest):
    """Have a dedicated conversation with an NPC."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        snapshot = None if isinstance(session, GameSession) else _capture_generation_state(session)

        def capture_after_background():
            nonlocal snapshot
            snapshot = _capture_generation_state(session)

        try:
            logger.info("NPC对话 save=%s npc=%s — %s", save_id, npc_id, req.message[:60])
            if isinstance(session, GameSession):
                result = await session.talk_to_npc(npc_id, req.message,
                                                   on_background_drained=capture_after_background)
            else:
                result = await session.talk_to_npc(npc_id, req.message)
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            logger.info("NPC回复完成 — npc=%s, 状态变更=%d",
                        npc_id, len(result.get("state_changes", [])))

            # 持久化：NPC对话现在创建独立节点，用 _save_node 保存
            node_id = result.get("node_id")
            if node_id:
                await _save_node(session, {
                    "node_id": node_id,
                    "narrative": result.get("response", ""),
                    "choices": [],
                    "dice_rolls": [],
                    "state_changes": result.get("state_changes", []),
                    "triggered_events": result.get("triggered_events", []),
                    "state": session.current_state,
                })
        except BaseException:
            if snapshot is not None:
                await _rollback_generation_state(session, snapshot)
            raise

        return _public_result(result, session)


@router.post("/{save_id}/talk/{npc_id}/stream")
async def talk_to_npc_stream(save_id: str, npc_id: str, req: NpcTalkRequest):
    """SSE streaming version of NPC talk."""
    lock = _get_session_lock(save_id)
    await lock.acquire()

    try:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        snapshot = None if isinstance(session, GameSession) else _capture_generation_state(session)
    except BaseException:
        lock.release()
        raise

    persisted = False

    async def event_stream():
        nonlocal persisted, snapshot

        def capture_after_background():
            nonlocal snapshot
            snapshot = _capture_generation_state(session)

        if isinstance(session, GameSession):
            npc_stream = session.talk_to_npc_stream(npc_id, req.message,
                                                    on_background_drained=capture_after_background)
        else:
            npc_stream = session.talk_to_npc_stream(npc_id, req.message)
        async for chunk in npc_stream:
            if chunk["type"] in ("text", "thinking", "narrative_revised"):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] == "final":
                if not chunk.get("error"):
                    node_id = chunk.get("node_id")
                    if not node_id:
                        raise RuntimeError("NPC流式对话缺少节点ID")
                    await _save_node(session, {
                        "node_id": node_id,
                        "narrative": chunk.get("response", ""),
                        "choices": [],
                        "dice_rolls": [],
                        "state_changes": chunk.get("state_changes", []),
                        "triggered_events": chunk.get("triggered_events", []),
                        "state": session.current_state,
                    })
                    persisted = True
                yield f"data: {json.dumps(_public_result(chunk, session), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

    async def saving_event_stream():
        try:
            async for data in event_stream():
                yield data
        finally:
            try:
                if not persisted and snapshot is not None:
                    logger.warning("NPC流式对话未持久化 — 回滚状态 save=%s npc=%s", save_id, npc_id)
                    await _rollback_generation_state(session, snapshot)
            finally:
                lock.release()

    return StreamingResponse(saving_event_stream(), media_type="text/event-stream")


@router.get("/{save_id}/npc-schedules")
async def get_npc_schedules(save_id: str):
    """Get current schedule info for known NPCs at the player's location."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        state = session.current_state
        game_time = state.get("game_time", "")
        player_location = state.get("player", {}).get("location")
        result = {}
        if not player_location:
            return result
        for npc in session.script.get("npcs", []):
            npc_id = npc["id"]
            npc_state = state.get("npcs", {}).get(npc_id)
            if not isinstance(npc_state, dict) or npc_state.get("known") is False:
                continue
            location = session._get_npc_location(npc_id)
            if location == player_location:
                info = session.prompt_builder.get_npc_current_info(npc_id, game_time, state=state)
                activity = info.get("activity", "") if info and info.get("location") == location else ""
                result[npc_id] = {"location": location, "activity": activity}
        return result


# ================================================
#  Swipe / Regenerate / Author's Note
# ================================================

@router.post("/{save_id}/regenerate")
async def regenerate(save_id: str, stage: str = "all", hint: str = ""):
    """Regenerate the AI response for the current node (Swipe system).

    stage: "all" = full regenerate, "choices" = choices only, "state" = state only.
    hint: optional narrative direction hint.
    """
    if stage not in ("all", "choices", "state"):
        raise HTTPException(status_code=400, detail="stage must be 'all', 'choices', or 'state'")
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if isinstance(session, GameSession):
            await session._drain_background_tasks()
        snapshot = _capture_generation_state(session)
        try:
            result = await session.regenerate(stage=stage, hint=hint)
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            # Save regenerated scene image to disk
            scene_img = result.get("scene_image")
            scene_image_path = None
            if scene_img and scene_img.get("base64"):
                try:
                    import base64 as b64mod
                    scenes_dir = os.path.join(str(DATA_DIR), "scenes", save_id)
                    os.makedirs(scenes_dir, exist_ok=True)
                    node_id = result.get("node_id", "unknown")
                    filename = f"{node_id}.png"
                    filepath = os.path.join(scenes_dir, filename)
                    with open(filepath, "wb") as imgf:
                        imgf.write(b64mod.b64decode(scene_img["base64"]))
                    scene_image_path = f"/api/game/scenes/{save_id}/{filename}"
                    node = session.world_tree.get_node(node_id)
                    if node:
                        node["scene_image_path"] = scene_image_path
                except Exception as e:
                    logger.warning("Failed to save regen scene image: %s", e)
            result["scene_image_path"] = scene_image_path
            result.pop("scene_image", None)
            # Update node in DB
            await _update_node_in_db(session, result)
            logger.info("重新生成完成 — stage=%s, swipe=%d/%d",
                        stage, result.get("swipe_index", 0) + 1, result.get("total_swipes", 1))
            return _public_result(result, session)
        except HTTPException:
            await _rollback_generation_state(session, snapshot)
            raise
        except Exception as e:
            await _rollback_generation_state(session, snapshot)
            logger.error("重新生成失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/{save_id}/regenerate/stream")
async def regenerate_stream(save_id: str, stage: str = "all", hint: str = "", stream_id: str = ""):
    """Streaming regenerate via agent: full revert+replay, or partial (choices/state only)."""
    if stage not in ("all", "choices", "state"):
        raise HTTPException(status_code=400, detail="stage must be 'all', 'choices', or 'state'")
    lock = _get_session_lock(save_id)
    await lock.acquire()
    try:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        node = session.world_tree.get_node(session.world_tree.active_node_id)
        if not node:
            raise HTTPException(status_code=400, detail="No active node")

        orig_action = node.get("player_action") or {"type": "freeform", "text": ""}
        orig_narrative = node.get("ai_response", "")
        orig_choices = node.get("choices_presented", [])
        orig_state_changes = node.get("state_changes", [])
        orig_snapshot = node.get("state_snapshot")
        replay_instructions = {
            key: copy.deepcopy(node[key])
            for key in ("turn_instructions", "stage_directives") if key in node
        }
        action_text = orig_action.get("text", "") if isinstance(orig_action, dict) else str(orig_action)
        if stage == "all":
            # Full regen: revert to parent + replay
            orig_node_id = session.world_tree.active_node_id
            parent_id = node.get("parent_id")
            if not parent_id:
                raise HTTPException(status_code=400, detail="Cannot regenerate the opening node")
            parent_node = session.world_tree.get_node(parent_id)
            if not parent_node:
                raise HTTPException(status_code=400, detail="Parent node not found")
            parent_snap = parent_node.get("state_snapshot")
            if not parent_snap:
                parent_snap = await session._load_snapshot_from_db(parent_id)
            if not parent_snap:
                raise HTTPException(status_code=400, detail="Previous state snapshot not found")
            action = dict(orig_action)
            if hint:
                action["text"] = action.get("text", "") + f"\n[重生成倾向] {hint}"
        else:
            partial_baseline = None
            if stage == "state":
                parent_id = node.get("parent_id")
                parent_node = session.world_tree.get_node(parent_id) if parent_id else None
                if not parent_node:
                    raise HTTPException(status_code=400, detail="Previous state snapshot not found")
                partial_baseline = parent_node.get("state_snapshot")
                if not partial_baseline:
                    partial_baseline = await session._load_snapshot_from_db(parent_id)
                if not partial_baseline:
                    raise HTTPException(status_code=400, detail="Previous state snapshot not found")
                partial_baseline = copy.deepcopy(partial_baseline)
    except HTTPException:
        lock.release()
        raise
    except asyncio.CancelledError:
        lock.release()
        raise
    except Exception as e:
        lock.release()
        logger.error("流式重生成准备失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    final_result = None
    persisted = False
    prepared = False
    snapshot = None
    pre_regen_tree = None
    pre_regen_state = None
    pre_regen_turn = None
    pre_regen_side_effects = None
    inner_stream = None

    async def event_stream_all():
        nonlocal final_result, persisted, inner_stream
        inner_stream = (session.process_action_stream(action, replay_instructions=replay_instructions)
                        if replay_instructions else session.process_action_stream(action))
        async for chunk in inner_stream:
            if chunk["type"] == "aborted":
                session._abort_request_pending = False
            if chunk["type"] == "agent_controls_ready":
                if not getattr(session, "_active_agent_stream", False):
                    _set_agent_stream_active(session, True, stream_id)
                session._agent_inject_enabled = True
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] == "agent_controls_closed":
                for control_event in _close_agent_control_events(session):
                    yield f"data: {json.dumps(control_event, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] == "heartbeat":
                yield ": heartbeat\n\n"
            elif chunk["type"] == "preflight" and chunk.get("stage") == "background":
                continue
            elif chunk["type"] in ("preflight", "text", "thinking", "narrative_revised", "model_output_started", "tool_preparing",
                                 "tool_started", "tool_call", "agent_done", "aborted",
                                 "user_inject", "agent_max_rounds",
                                 "agent_plan", "agent_waiting", "model_waiting", "plan_auto_confirmed"):
                yield f"data: {json.dumps(_public_agent_progress(chunk), ensure_ascii=False)}\n\n"
            elif chunk["type"] == "final":
                new_node_id = session.world_tree.active_node_id
                new_node = session.world_tree.get_node(new_node_id)
                original_node = session.world_tree.get_node(orig_node_id)
                if (not new_node or not original_node or new_node_id == orig_node_id
                        or new_node.get("parent_id") != parent_id or new_node.get("children_ids")):
                    raise RuntimeError("Regeneration did not create a replacement node")
                session._hydrate_branch_runtime(session.current_state)

                swipes = copy.deepcopy(original_node.get("swipes", []))
                if not swipes:
                    swipes.append({
                        "narrative": orig_narrative,
                        "choices": orig_choices,
                        "state_changes": orig_state_changes,
                        "state_snapshot": copy.deepcopy(orig_snapshot or pre_regen_state),
                        "agent_trace": copy.deepcopy(original_node.get("agent_trace")),
                        "game_time": original_node.get("game_time", ""),
                        "triggered_events": copy.deepcopy(original_node.get("triggered_events", [])),
                    })
                else:
                    active_index = original_node.get("active_swipe_index", 0)
                    if 0 <= active_index < len(swipes):
                        swipes[active_index]["agent_trace"] = copy.deepcopy(original_node.get("agent_trace"))
                swipes.append({
                    "narrative": new_node.get("ai_response", ""),
                    "choices": new_node.get("choices_presented", []),
                    "state_changes": new_node.get("state_changes", []),
                    "state_snapshot": copy.deepcopy(new_node.get("state_snapshot")),
                    "agent_trace": copy.deepcopy(chunk.get("agent_trace")),
                    "game_time": new_node.get("game_time", ""),
                    "triggered_events": copy.deepcopy(new_node.get("triggered_events", [])),
                })
                preserved = {
                    key: original_node.get(key)
                    for key in ("id", "parent_id", "children_ids", "created_at",
                                "player_action", "turn_number")
                }
                shared_strip_keys = original_node.get("_shared_strip_keys")
                original_node.clear()
                original_node.update(new_node)
                if chunk.get("agent_trace"):
                    original_node["agent_trace"] = chunk["agent_trace"]
                else:
                    original_node.pop("agent_trace", None)
                original_node.update(preserved)
                original_node["swipes"] = swipes
                original_node["active_swipe_index"] = len(swipes) - 1
                if shared_strip_keys is not None:
                    original_node["_shared_strip_keys"] = shared_strip_keys
                session.world_tree.remove_node(new_node_id)
                session.world_tree.active_node_id = orig_node_id
                session.world_tree._branch_dirty = True

                final_result = {**chunk, "node_id": orig_node_id,
                                "swipe_index": len(swipes) - 1, "total_swipes": len(swipes)}
                scene_image_path = await _save_node(session, final_result)
                if scene_image_path:
                    final_result["scene_image_path"] = scene_image_path
                final_result.pop("scene_image", None)
                persisted = True
                yield f"data: {json.dumps(_public_result(final_result, session), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

    async def event_stream_partial():
        nonlocal final_result, persisted, inner_stream
        if stage == "state":
            session.current_state = copy.deepcopy(partial_baseline)
            for key in session._SWIPE_STRIP_KEYS:
                if key not in session.current_state and key in pre_regen_state:
                    session.current_state[key] = copy.deepcopy(pre_regen_state[key])
        inner_stream = session._regen_partial_agent(
            stage, orig_narrative, action_text, session.current_state, streaming=True,
        )
        async for chunk in inner_stream:
            if chunk.get("type") == "aborted":
                session._abort_request_pending = False
            if chunk.get("type") == "partial_regen_done":
                for control_event in _close_agent_control_events(session):
                    yield f"data: {json.dumps(control_event, ensure_ascii=False)}\n\n"
                yield 'data: {"type": "agent_controls_closed"}\n\n'
                if stage == "choices":
                    choices = chunk.get("choices") or []
                    if not choices:
                        raise IncompleteAgentTurnError("选项生成为空，重生成失败")
                    state = copy.deepcopy(pre_regen_state)
                    state_changes = copy.deepcopy(orig_state_changes)
                else:
                    parsed = chunk.get("parsed_state") or {}
                    present_npc_ids, _ = session._compute_present_npcs(partial_baseline)
                    try:
                         state, state_changes, triggered_events = session._settle_regen_state(
                             parsed, partial_baseline, present_npc_ids,
                             is_system_opening=(isinstance(orig_action, dict)
                                                and orig_action.get("type") == "system"),
                             map_travel_destination=(session._map_travel_destination(
                                 orig_action, state=partial_baseline)
                                 if isinstance(orig_action, dict) else None),
                         )
                    except ValueError as exc:
                        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
                        return
                    choices = copy.deepcopy(orig_choices)
                trace = chunk.get("agent_trace")
                session._record_regen_swipe(
                    node, orig_narrative, choices, state, state_changes, trace,
                    triggered_events if stage == "state" else None,
                )
                final_result = {
                    "type": "final",
                    "node_id": session.world_tree.active_node_id,
                    "narrative": orig_narrative,
                    "choices": node["choices_presented"],
                    "state": session.current_state,
                    "state_changes": state_changes,
                    "stage": stage,
                    "swipe_index": node["active_swipe_index"],
                    "total_swipes": len(node["swipes"]),
                    "agent_trace": trace,
                }
                await _update_node_in_db(session, final_result)
                if trace:
                    session._agent_traces.append(trace)
                    if len(session._agent_traces) > 10:
                        session._agent_traces.pop(0)
                persisted = True
                yield f"data: {json.dumps(_public_result(final_result, session), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            elif chunk.get("type") == "heartbeat":
                yield ": heartbeat\n\n"
            elif chunk.get("type") in ("model_output_started", "tool_preparing", "tool_call", "agent_done", "agent_waiting",
                                       "model_waiting", "aborted", "agent_max_rounds", "user_inject"):
                yield f"data: {json.dumps(_public_agent_progress(chunk), ensure_ascii=False)}\n\n"

    async def saving_stream():
        nonlocal prepared, snapshot, pre_regen_tree, pre_regen_state, pre_regen_turn
        nonlocal pre_regen_side_effects, orig_action, orig_narrative, orig_choices
        nonlocal orig_state_changes, orig_snapshot, replay_instructions, action_text, action
        stop_available = (stage == "all" and isinstance(session, GameSession)
                          and (session._agentic_enabled() or session._agentic_unified_enabled()))
        source = None
        _set_agent_stream_active(session, stage != "all" or stop_available, stream_id)
        if stop_available:
            session._agent_inject_enabled = False
        try:
            ready = {"type": "stream_ready", "controls": stage != "all"}
            if stop_available:
                ready["stop_available"] = True
            yield f'data: {json.dumps(ready)}\n\n'
            if session._background_tasks - session._deferred_tasks:
                yield 'data: {"type": "preflight", "stage": "background"}\n\n'
            await session._drain_background_tasks()
            if stop_available and session._abort_flag:
                session._abort_request_pending = False
                yield 'data: {"type": "aborted", "label": "准备阶段"}\n\n'
                yield 'data: {"type": "agent_controls_closed"}\n\n'
                return
            snapshot = _capture_generation_state(session)
            pre_regen_side_effects = copy.deepcopy((
                session.script, session._npc_by_id, session._npc_name_to_id,
                session.prompt_builder._npc_by_id, session.prompt_builder._npc_name_map,
                session.narrative_graph, session.prompt_builder.lorebook,
                session.prompt_builder._branch_lore_ids,
            ))
            pre_regen_tree = copy.deepcopy(session.world_tree)
            pre_regen_state = copy.deepcopy(session.current_state)
            pre_regen_turn = session.turn_number
            orig_action = node.get("player_action") or {"type": "freeform", "text": ""}
            orig_narrative = node.get("ai_response", "")
            orig_choices = node.get("choices_presented", [])
            orig_state_changes = node.get("state_changes", [])
            orig_snapshot = node.get("state_snapshot")
            replay_instructions = {
                key: copy.deepcopy(node[key])
                for key in ("turn_instructions", "stage_directives") if key in node
            }
            action_text = orig_action.get("text", "") if isinstance(orig_action, dict) else str(orig_action)
            prepared = True
            session.prompt_builder.track_dynamic_lore(pre_regen_state)
            if stage == "all":
                action = dict(orig_action)
                if hint:
                    action["text"] = action.get("text", "") + f"\n[重生成倾向] {hint}"
                session.current_state = copy.deepcopy(parent_snap)
                session.turn_number = max(0, session.turn_number - 1)
                session.world_tree.active_node_id = parent_id
                session.world_tree._branch_dirty = True
            source = event_stream_all() if stage == "all" else event_stream_partial()
            async for data in source:
                yield data
        except IncompleteAgentTurnError as exc:
            logger.warning("流式重生成未通过结算校验 — save=%s: %s", save_id, exc)
            error = {"type": "error", "message": str(exc)}
            if isinstance(exc, (NarrativeSettlementError, PendingConsequenceDisclosureError, SemanticReviewError,
                                MapTravelSettlementError)):
                error["discard_partial"] = True
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
        finally:
            try:
                try:
                    if source is not None:
                        await source.aclose()
                finally:
                    if inner_stream is not None:
                        await inner_stream.aclose()
            finally:
                try:
                    if not persisted and prepared:
                        logger.warning("流式重生成中断 — save=%s, stage=%s", save_id, stage)
                        session.world_tree = pre_regen_tree
                        session.current_state = pre_regen_state
                        session.turn_number = pre_regen_turn
                        await _rollback_generation_state(session, snapshot)
                        (script, session._npc_by_id, session._npc_name_to_id,
                         session.prompt_builder._npc_by_id, session.prompt_builder._npc_name_map,
                         session.narrative_graph, session.prompt_builder.lorebook,
                         session.prompt_builder._branch_lore_ids) = pre_regen_side_effects
                        session.script.clear()
                        session.script.update(script)
                finally:
                    _set_agent_stream_active(session, False)
                    lock.release()

    return StreamingResponse(saving_stream(), media_type="text/event-stream")


@router.post("/{save_id}/feedback-regen")
async def feedback_regenerate(save_id: str, req: dict):
    """用户对当前叙事给出反馈，触发重新生成。
    Body: {"feedback": "让气氛更紧张一些"}
    """
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        feedback = (req.get("feedback") or "").strip()
        if not feedback:
            raise HTTPException(status_code=400, detail="请提供反馈内容")
        result = await session.regenerate_with_feedback(feedback)
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        # 同步更新数据库中的叙事文本
        await _update_node_in_db(session, {
            "node_id": session.world_tree.active_node_id,
            "narrative": result["narrative"],
            "choices": session.world_tree.get_node(session.world_tree.active_node_id).get("choices_presented", []),
            "state_changes": [],
        })
        return _public_result(result, session)


def _check_swipe_context(session: GameSession, expected_node_id: str | None,
                         expected_swipe_index: int | None, expected_total_swipes: int | None):
    if expected_node_id is None and expected_swipe_index is None and expected_total_swipes is None:
        return
    node_id = session.world_tree.active_node_id
    node = session.world_tree.get_node(node_id)
    swipes = node.get("swipes", []) if node else []
    if (expected_node_id is not None and expected_node_id != node_id
            or expected_swipe_index is not None and expected_swipe_index != (node.get("active_swipe_index", 0) if node else 0)
            or expected_total_swipes is not None and expected_total_swipes != (len(swipes) or 1)):
        raise HTTPException(status_code=409, detail="当前回合版本已变化，请重新加载后重试")


@router.post("/{save_id}/swipe/jump")
async def swipe_jump(save_id: str, index: int = 0, expected_node_id: str | None = None,
                     expected_swipe_index: int | None = None, expected_total_swipes: int | None = None):
    """Jump to a specific swipe by index."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        await session._drain_background_tasks()
        _check_swipe_context(session, expected_node_id, expected_swipe_index, expected_total_swipes)
        result = session.swipe_to_index(index)
        if not result:
            raise HTTPException(status_code=400, detail="Invalid swipe index")
        await _update_node_in_db(session, result)
        return _public_result(result, session)


@router.post("/{save_id}/swipe/{direction}")
async def swipe_direction(save_id: str, direction: str, expected_node_id: str | None = None,
                          expected_swipe_index: int | None = None, expected_total_swipes: int | None = None):
    """Switch to a different swipe (left/right)."""
    if direction not in ("left", "right"):
        raise HTTPException(status_code=400, detail="Direction must be 'left' or 'right'")
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        await session._drain_background_tasks()
        _check_swipe_context(session, expected_node_id, expected_swipe_index, expected_total_swipes)
        result = session.swipe_to(direction)
        if not result:
            raise HTTPException(status_code=400, detail="No swipes available")
        await _update_node_in_db(session, result)
        logger.info("Swipe %s — save=%s, swipe=%d/%d",
                    direction, save_id, result.get("swipe_index", 0) + 1, result.get("total_swipes", 1))
        return _public_result(result, session)


@router.get("/{save_id}/swipes")
async def get_swipes(save_id: str):
    """Get all swipe variants for the active node."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        swipes = session.get_all_swipes()
        node = session.world_tree.get_node(session.world_tree.active_node_id)
        variants = node.get("swipes", []) if node else []
        public_swipes = []
        for swipe in swipes:
            if isinstance(swipe.get("state_changes"), list):
                source = variants[swipe["index"]] if variants else node
                swipe = {**swipe, "state_changes": _player_state_changes(
                    swipe["state_changes"], session.script, source.get("state_snapshot") or {})}
            public_swipes.append(swipe)
        return public_swipes


@router.post("/{save_id}/continue")
async def continue_narrative(save_id: str):
    """Extend the current narrative without starting a new turn."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        try:
            result = await session.continue_narrative()
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            await _update_node_in_db(session, result)
            return _public_result(result, session)
        except Exception as e:
            logger.error("续写失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/{save_id}/authors-note")
async def set_authors_note(save_id: str, req: AuthorsNoteRequest):
    """Set the player's behind-the-scenes directive."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        depth = max(1, req.depth)
        # Architecture#2: 持久化 authors_note 到数据库
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET authors_note = ?, authors_note_position = ?, authors_note_depth = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (req.note, req.position, depth, save_id),
            )
            await db.commit()
        session.set_authors_note(req.note, req.position, depth)
        logger.info("设置作者笔记 — save=%s, note=%s, pos=%s", save_id, (req.note or "")[:40], req.position)
        return {"status": "ok", "note": req.note, "position": req.position, "depth": depth}


@router.post("/{save_id}/negative-prompt")
async def set_negative_prompt(save_id: str, req: NegativePromptRequest):
    """Set the CFG-style negative prompt (prohibition rules injected into system prompt)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET negative_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (req.text, save_id),
            )
            await db.commit()
        session.negative_prompt = req.text
        return {"status": "ok", "text": req.text}


@router.post("/{save_id}/logit-bias")
async def set_logit_bias(save_id: str, req: LogitBiasRequest):
    """Set logit bias entries (word-level encourage/suppress hints)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET logit_bias = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(req.entries, ensure_ascii=False), save_id),
            )
            await db.commit()
        session.logit_bias = req.entries
        return {"status": "ok", "entries": req.entries}


@router.post("/{save_id}/lorebook-recursion")
async def set_lorebook_recursion(save_id: str, req: LorebookRecursionRequest):
    """Adjust lorebook max recursion depth at runtime."""
    val = max(1, min(8, req.max_recursion))
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET lorebook_max_recursion = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (val, save_id),
            )
            await db.commit()
        if session.prompt_builder and session.prompt_builder.lorebook:
            session.prompt_builder.lorebook.max_recursion = val
        session.script.setdefault("settings", {})["lorebook_max_recursion"] = val
    return {"status": "ok", "max_recursion": val}


@router.post("/{save_id}/switch-persona")
async def switch_persona(save_id: str, req: SwitchPersonaRequest):
    """Legacy endpoint — redirects to switch-pov."""
    pov_req = SwitchPovRequest(preset_id=req.preset_id)
    return await switch_pov(save_id, pov_req)


@router.post("/{save_id}/switch-pov")
async def switch_pov(save_id: str, req: SwitchPovRequest):
    """Switch player POV: shelve current PC, init new PC in same world timeline."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        snapshot = _capture_generation_state(session)
        script_npcs = copy.deepcopy(session.script["npcs"]) if "npcs" in session.script else _MISSING
        script_orgs = copy.deepcopy(session.script["organizations"]) if "organizations" in session.script else _MISSING
        npc_by_id = session._npc_by_id.copy()
        prompt_npc_by_id = session.prompt_builder._npc_by_id.copy()
        loc_name_map = session.prompt_builder._loc_name_map.copy()
        try:
            result = await session.switch_pov(req.preset_id, req.custom_character)
            if not result:
                raise HTTPException(status_code=400, detail="POV switch failed (limit reached or invalid target)")
            enriched = _enrich_state(session, result["state"])
            await _save_node(session, result)
        except BaseException:
            await _rollback_generation_state(session, snapshot)
            for key, value in (("npcs", script_npcs), ("organizations", script_orgs)):
                if value is _MISSING:
                    session.script.pop(key, None)
                else:
                    session.script[key] = value
            session._npc_by_id = npc_by_id
            session.prompt_builder._npc_by_id = prompt_npc_by_id
            session.prompt_builder._loc_name_map = loc_name_map
            raise
        return {
            "status": "ok",
            "narrative": result["narrative"],
            "player": enriched.get("player", result["player"]),
            "choices": result["choices"],
            "state": enriched,
        }


@router.post("/{save_id}/npc-dialogue")
async def npc_dialogue(save_id: str, req: NpcDialogueRequest):
    """Trigger independent NPC dialogue round (group chat style)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if not session.ai_provider:
            raise HTTPException(status_code=400, detail="请先在设置中配置 AI 模型，再发起 NPC 对话")
        speeches = await _run_async_state_change(session, lambda: session.npc_dialogue_round(req.topic))
        return {"speeches": speeches}


@router.post("/{save_id}/undo")
async def undo_last_turn(save_id: str):
    """Flow#7: 撤销最近一次行动，回到上一步。"""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        snapshot = _capture_generation_state(session)
        try:
            result = await session.undo_last_turn()
            if not result or result.get("error"):
                raise HTTPException(status_code=400, detail=(result or {}).get("error", "无法撤销"))
            await _persist_undo(session, result)
        except BaseException:
            await _rollback_generation_state(session, snapshot)
            raise
        logger.info("撤销行动 — save=%s, 回退到node=%s, turn=%d",
                     save_id, result["node_id"], session.turn_number)
        return _public_result(result, session)


# ===== Story Tree endpoints =====

def _player_story_trees(trees: list[dict], script: dict) -> list[dict]:
    public_trees = []
    for tree in trees:
        nodes = [node for node in tree.get("nodes", [])
                 if node.get("status") in ("completed", "active", "available")
                  and node.get("type") != "trigger"
                  and (is_public_story_node(script, node["id"])
                       or node.get("type") == "choice" and node.get("status") == "active")]
        if not nodes:
            continue
        visible_ids = {node["id"] for node in nodes}
        public_nodes = []
        for node in nodes:
            view = {key: node[key] for key in (
                "id", "name", "description", "type", "status", "position",
                "progress", "cooldown_remaining",
            ) if key in node}
            view["requires"] = [ref for ref in node.get("requires", []) if ref in visible_ids]
            view["on_complete_unlock"] = [ref for ref in node.get("on_complete_unlock", [])
                                          if ref in visible_ids]
            if "choices" in node and node["status"] in ("active", "completed"):
                view["choices"] = [{
                    **{key: choice[key] for key in ("id", "label", "description", "chosen")
                       if key in choice},
                    "unlock": [ref for ref in choice.get("unlock", []) if ref in visible_ids],
                } for choice in node["choices"]]
            public_nodes.append(view)
        public_trees.append({
            "id": tree["id"], "name": tree.get("name", tree["id"]),
            "description": tree.get("description", "") if len(nodes) == len(tree["nodes"]) else "",
            "icon": tree.get("icon", ""), "nodes": public_nodes,
        })
    return public_trees


class StoryTreeChoiceRequest(BaseModel):
    node_id: str
    choice_id: str


@router.get("/{save_id}/story-tree/active-choices")
async def get_active_story_choices(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        trees = []
        if session.event_engine:
            trees = session.event_engine.get_visible_trees(session.current_state, session.turn_number)
        elif session.story_tree_engine:
            trees = session.story_tree_engine.get_visible_trees(session.current_state, session.turn_number)
        pending = []
        for tree in trees:
            for node in tree["nodes"]:
                if node.get("type") != "choice" or node.get("status") != "active":
                    continue
                if session.event_engine:
                    source_choices = session.event_engine.events[node["id"]].choices
                else:
                    source_choices = session.story_tree_engine._nodes[node["id"]].get("choices", [])
                available_ids = {
                    choice["id"] for choice in source_choices
                    if not choice.get("condition") or session._evaluate_condition(choice["condition"])
                }
                choices = [{
                    "id": choice["id"],
                    "label": choice["label"],
                    "description": choice.get("description", ""),
                } for choice in node.get("choices", []) if choice["id"] in available_ids]
                if choices:
                    pending.append({"id": node["id"], "name": node["name"], "choices": choices})
        return {"choices": pending}


@router.get("/{save_id}/story-tree")
async def get_story_tree(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        trees = []
        if session.event_engine:
            trees = session.event_engine.get_visible_trees(session.current_state, session.turn_number)
        elif session.story_tree_engine:
            trees = session.story_tree_engine.get_visible_trees(session.current_state, session.turn_number)

        ot_view = []
        fired_set = set(session.current_state.get("fired_one_time_events", []))
        for ev in session.script.get("one_time_events", []):
            if not isinstance(ev, dict) or not ev.get("id") or not is_public_event(session.script, ev["id"]):
                continue
            ot_view.append({
                "id": ev["id"],
                "name": ev.get("name", ev.get("description", ev["id"])[:20]),
                "description": ev.get("description", ""),
                "trigger_time": ev.get("trigger_time", ""),
                "condition": ev.get("condition", ""),
                "fire_events": ev.get("fire_events", []),
                "activate_events": ev.get("activate_events", []),
                "status": "fired" if ev["id"] in fired_set else "pending",
            })

        ce_view = []
        trackers = session.current_state.get("cyclic_event_trackers", {})
        game_time = session.current_state.get("game_time", "")
        for ev in session.script.get("cyclic_events", []):
            if not isinstance(ev, dict) or not ev.get("id") or not is_public_event(session.script, ev["id"]):
                continue
            eid = ev["id"]
            tracker = trackers.get(eid, {})
            from engine.event_scheduler import parse_time
            expires = parse_time(ev.get("expires_at"))
            gt = parse_time(game_time)
            expired = bool(expires and gt and expires < gt)
            ce_view.append({
                "id": eid,
                "name": ev.get("name", ev.get("description", eid)[:20]),
                "description": ev.get("description", ""),
                "frequency_value": ev.get("frequency_value", 1),
                "frequency_unit": ev.get("frequency_unit", "day"),
                "condition": ev.get("condition", ""),
                "fire_events": ev.get("fire_events", []),
                "activate_events": ev.get("activate_events", []),
                "status": "expired" if expired else "active",
                "next_fire": tracker.get("next_fire", ""),
                "last_fired": tracker.get("last_fired", ""),
            })

        threads = session.event_engine.get_events_by_category(session.current_state, "thread") if session.event_engine else []
        clues = session.event_engine.get_events_by_category(session.current_state, "clue") if session.event_engine else []
        return {
            "trees": _player_story_trees(trees, session.script),
            "one_time_events": ot_view,
            "cyclic_events": ce_view,
            "narrative_threads": [{
                "id": thread["id"], "name": thread["name"],
                "description": thread["description"], "status": thread["status"],
                "created_turn": session.event_engine.events[thread["id"]].created_turn,
                "last_updated": thread.get("last_updated"),
            } for thread in threads if thread["status"] in ("active", "dormant", "completed")],
            "clue_board": [{
                "id": clue["id"], "text": clue["description"],
                "category": clue["metadata"].get("category", ""),
                "source": clue["metadata"].get("source", ""),
                "turn_discovered": session.event_engine.events[clue["id"]].created_turn,
            } for clue in clues if clue["status"] == "completed"],
        }


@router.post("/{save_id}/story-tree/choice")
async def story_tree_choice(save_id: str, req: StoryTreeChoiceRequest, summary_only: bool = False):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if not session.story_tree_engine and not session.event_engine:
            raise HTTPException(status_code=400, detail="此剧本没有剧情树")
        if session.event_engine:
            node = session.event_engine.events.get(req.node_id)
            selectable = node is not None and node.category == "choice"
        else:
            node = session.story_tree_engine._nodes.get(req.node_id)
            selectable = node is not None and node.get("type") == "choice"
        if not selectable:
            if summary_only:
                return {"success": False}
            engine = session.event_engine or session.story_tree_engine
            trees = engine.get_visible_trees(session.current_state, session.turn_number)
            return {"success": False, "completed": [], "notifications": [],
                    "trees": _player_story_trees(trees, session.script)}
        snapshot = _capture_generation_state(session)
        try:
            if session.event_engine:
                result = session.event_engine.make_choice(
                    session.current_state, req.node_id, req.choice_id,
                    condition_eval=session._evaluate_condition,
                )
                session._apply_event_result(result)
                if not summary_only:
                    trees = session.event_engine.get_visible_trees(session.current_state, session.turn_number)
            else:
                result = session.story_tree_engine.make_choice(
                    session.current_state, req.node_id, req.choice_id,
                    condition_eval=session._evaluate_condition,
                )
                session._apply_story_tree_result(result)
                if not summary_only:
                    trees = session.story_tree_engine.get_visible_trees(session.current_state, session.turn_number)
            if session.current_state != snapshot["state"]:
                await _persist_active_state(session)
        except BaseException:
            await _rollback_generation_state(session, snapshot)
            raise
        success = len(result.newly_completed) > 0
        if summary_only:
            return {"success": success}
        return {
            "success": success,
            "completed": [{"id": n["id"], "name": n.get("name", n["id"])} for n in result.newly_completed],
            "notifications": result.notifications,
            "trees": _player_story_trees(trees, session.script),
        }


class DeductionRequest(BaseModel):
    clue_ids: list[str]

class ShopBuyRequest(BaseModel):
    shop_id: str
    item_id: str

class ShopSellRequest(BaseModel):
    shop_id: str
    item_name: str


@router.post("/{save_id}/deduce")
async def attempt_deduction(save_id: str, req: DeductionRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_async_state_change(session, lambda: session.attempt_deduction(req.clue_ids))


@router.get("/{save_id}/shop/{location_id}")
async def get_shop(save_id: str, location_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.get_location_shops(location_id)


@router.post("/{save_id}/shop/buy")
async def shop_buy(save_id: str, req: ShopBuyRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_state_action(session, lambda: session.buy_item(req.shop_id, req.item_id))


@router.post("/{save_id}/shop/sell")
async def shop_sell(save_id: str, req: ShopSellRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_state_action(session, lambda: session.sell_item(req.shop_id, req.item_name))


class UnlockSkillRequest(BaseModel):
    skill_id: str

@router.post("/{save_id}/unlock-skill")
async def unlock_skill(save_id: str, req: UnlockSkillRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_state_action(session, lambda: session.unlock_skill(req.skill_id))


@router.post("/{save_id}/newspaper")
async def generate_newspaper(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_async_state_change(session, session.generate_newspaper)


@router.post("/{save_id}/newspaper/regenerate")
async def regenerate_newspaper(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await _run_async_state_change(session, lambda: session.generate_newspaper(force=True))


@router.get("/scenes/{save_id}/{filename}")
async def get_scene_image(save_id: str, filename: str):
    """Serve a persisted scene image file."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = os.path.join(str(DATA_DIR), "scenes", save_id, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Scene image not found")
    return FileResponse(filepath, media_type="image/png")


@router.post("/{save_id}/generate-scene-image/{node_id}")
async def generate_scene_image_for_node(save_id: str, node_id: str):
    """Generate a scene image for an existing node that doesn't have one."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        node = session.world_tree.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        if node.get("scene_image_path"):
            return {"scene_image_path": node["scene_image_path"]}

        # Get image provider (always fetch latest config)
        from api.config_routes import get_image_provider_instance
        image_provider = await get_image_provider_instance()
        if not image_provider:
            raise HTTPException(status_code=400, detail="No image provider configured")
        session._image_provider = image_provider

        narrative = node.get("ai_response", "")
        if not narrative:
            raise HTTPException(status_code=400, detail="Node has no narrative text")

        # Build image prompt and generate
        from ai.image_prompt_builder import build_image_prompt
        from api.config_routes import get_image_style
        snapshot = node.get("state_snapshot", {})
        loc = snapshot.get("player", {}).get("location", "")
        loc_name = loc
        loc_desc = ""
        loc_data = session._location_by_id.get(loc)
        if loc_data:
            loc_name = loc_data.get("name", loc)
            loc_desc = loc_data.get("description", "")
        else:
            for l in session.script.get("locations", []):
                if l.get("id") == loc:
                    loc_name = l.get("name", loc)
                    loc_desc = l.get("description", "")
                    break
        tod = snapshot.get("time_of_day", "day")
        weather = snapshot.get("current_weather", "")

        # Gather character descriptions
        characters = []
        pc = session.script.get("player_character", {})
        pc_appearance = pc.get("appearance") or pc.get("bio") or ""
        if pc_appearance:
            characters.append(f"Player: {pc_appearance[:200]}")
        npc_states = snapshot.get("npcs", {})
        for nid, ns in list(npc_states.items())[:5]:
            if isinstance(ns, dict) and ns.get("current_location") == loc:
                npc_def = session._npc_by_id.get(nid, {})
                desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                name = ns.get("name") or npc_def.get("name", nid)
                if desc:
                    characters.append(f"{name}: {desc[:150]}")

        # Load image style preference
        style_data = await get_image_style()
        image_style = style_data.get("custom") or style_data.get("preset") or ""

        img_prompt = await build_image_prompt(
            narrative, loc_name, "atmospheric", tod, session.ai_provider,
            weather=weather, characters=characters,
            location_desc=loc_desc, image_style=image_style,
        )
        scene_img = await image_provider.generate_image(img_prompt)

        # Save to disk
        import base64 as b64mod
        scenes_dir = os.path.join(str(DATA_DIR), "scenes", save_id)
        os.makedirs(scenes_dir, exist_ok=True)
        filename = f"{node_id}.png"
        filepath = os.path.join(scenes_dir, filename)
        with open(filepath, "wb") as imgf:
            imgf.write(b64mod.b64decode(scene_img["base64"]))
        scene_image_path = f"/api/game/scenes/{save_id}/{filename}"

        # Update node and DB
        node["scene_image_path"] = scene_image_path
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state_snapshot FROM tree_nodes WHERE id = ?", (node_id,)
            )
            row = await cursor.fetchone()
            if row:
                snap = json.loads(row["state_snapshot"]) if row["state_snapshot"] else {}
                nc = snap.get("_node_context", {})
                nc["scene_image_path"] = scene_image_path
                snap["_node_context"] = nc
                await db.execute(
                    "UPDATE tree_nodes SET state_snapshot = ? WHERE id = ?",
                    (json.dumps(snap, ensure_ascii=False), node_id)
                )
                await db.commit()

        return {"scene_image_path": scene_image_path, "prompt": img_prompt}


# ================================================
#  Observability endpoints (E1/E2/E3)
# ================================================

@router.get("/{save_id}/agent-traces")
async def get_agent_traces(save_id: str):
    """E1: Return recent agent traces for visualization."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        traces = [node["agent_trace"] for node in session.world_tree.get_active_branch()
                  if isinstance(node.get("agent_trace"), dict)]
        return {"traces": [_public_agent_trace(trace) for trace in traces[-10:]]}


@router.get("/{save_id}/performance")
async def get_performance(save_id: str):
    """E3: Return performance statistics for the persisted active branch."""
    async with _get_session_lock(save_id):
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        traces = [node["agent_trace"] for node in session.world_tree.get_active_branch()
                  if isinstance(node.get("agent_trace"), dict)
                  and node["agent_trace"].get("mode") in ("agentic", "unified")]
        stats = {"total_turns": len(traces), "total_tokens": 0,
                 "avg_latency_ms": 0, "avg_active_latency_ms": 0, "tool_usage": {},
                  "avg_narrative_length": 0, "mode_distribution": {},
                  "avg_prepare_ms": None, "avg_first_text_active_ms": None,
                  "avg_agent_model_ms": None, "avg_semantic_review_ms": None,
                  "avg_preflight_retrieval_ms": None}
        if not traces:
            return stats
        duration = 0
        active_duration = 0
        narrative_length = 0
        timing_samples = {key: [] for key in (
            "prepare_ms", "first_text_active_ms", "agent_model_ms",
            "semantic_review_ms", "preflight_retrieval_ms",
        )}
        for trace in traces:
            tokens = trace.get("total_tokens") or {}
            stats["total_tokens"] += (tokens.get("prompt") or 0) + (tokens.get("completion") or 0)
            turn_duration = trace.get("duration_ms") or 0
            duration += turn_duration
            active_duration += max(0, turn_duration - (trace.get("plan_wait_ms") or 0))
            narrative_length += trace.get("narrative_length") or 0
            mode = trace["mode"]
            stats["mode_distribution"][mode] = stats["mode_distribution"].get(mode, 0) + 1
            for key in ("prepare_ms", "first_text_active_ms"):
                value = trace.get(key)
                if isinstance(value, (int, float)):
                    timing_samples[key].append(value)
            for field, key in (("agent_model_timings_ms", "agent_model_ms"),
                               ("semantic_review_timings_ms", "semantic_review_ms")):
                timings = trace.get(field)
                if isinstance(timings, list):
                    timing_samples[key].append(sum(value for value in timings
                                                   if isinstance(value, (int, float))))
            preflight = trace.get("preflight_timings_ms") or {}
            retrieval = preflight.get("retrieval") if isinstance(preflight, dict) else None
            if isinstance(retrieval, (int, float)):
                timing_samples["preflight_retrieval_ms"].append(retrieval)
            for step in trace.get("rounds", []):
                tool = step.get("tool")
                if tool:
                    stats["tool_usage"][tool] = stats["tool_usage"].get(tool, 0) + 1
        stats["avg_latency_ms"] = duration // len(traces)
        stats["avg_active_latency_ms"] = active_duration // len(traces)
        stats["avg_narrative_length"] = narrative_length // len(traces)
        for key, samples in timing_samples.items():
            if samples:
                stats[f"avg_{key}"] = sum(samples) // len(samples)
        return stats


@router.get("/{save_id}/usage-stats")
async def get_usage_stats(save_id: str):
    """#7: Return fine-grained token/cost usage statistics."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return session.usage_tracker.get_stats() if hasattr(session, 'usage_tracker') else {}


@router.post("/{save_id}/ab-test")
async def run_ab_test(save_id: str, req: dict):
    """E2: Run A/B test comparing two prompt variants."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.ab_tester.run_comparison(
            session, req.get("action", {}),
            req.get("variant_a", {}), req.get("variant_b", {}),
        )
        return result


@router.get("/{save_id}/ab-results")
async def get_ab_results(save_id: str):
    """E2: Return recent A/B test experiments."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return {"experiments": session.ab_tester.get_experiments()}


async def _run_state_action(session: GameSession, change) -> dict:
    snapshot = _capture_generation_state(session)
    try:
        result = change()
        if result.get("success") and result.get("state") is not None:
            await _persist_active_state(session)
        else:
            await _rollback_generation_state(session, snapshot)
        return _public_result(result, session)
    except BaseException:
        await _rollback_generation_state(session, snapshot)
        raise


async def _run_async_state_change(session: GameSession, change) -> dict:
    snapshot = _capture_generation_state(session)
    try:
        result = await change()
        if session.current_state != snapshot["state"]:
            await _persist_active_state(session)
        return _public_result(result, session)
    except BaseException:
        await _rollback_generation_state(session, snapshot)
        raise


async def _set_state_flag(session: GameSession, key: str, value: bool) -> None:
    snapshot = _capture_generation_state(session)
    try:
        session.current_state[key] = value
        await _persist_active_state(session)
    except BaseException:
        await _rollback_generation_state(session, snapshot)
        raise


async def _persist_active_state(session: GameSession) -> None:
    node_id = session.world_tree.active_node_id
    node = session.world_tree.get_node(node_id) if node_id else None
    if node is None:
        raise RuntimeError("Cannot persist state without an active node")
    state = copy.deepcopy(session.current_state)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT state_snapshot FROM tree_nodes WHERE id = ? AND save_id = ?",
            (node_id, session.save_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Active node is not persisted")
        stored = json.loads(row["state_snapshot"]) if row["state_snapshot"] else {}
        if stored.get("_node_context"):
            state["_node_context"] = stored["_node_context"]
        await db.execute(
            "UPDATE tree_nodes SET state_snapshot = ? WHERE id = ? AND save_id = ?",
            (json.dumps(state, ensure_ascii=False), node_id, session.save_id),
        )
        await db.execute(
            "UPDATE saves SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session.save_id,),
        )
        await db.commit()
    node["state_snapshot"] = copy.deepcopy(session.current_state)


async def _persist_query_exchange(session: GameSession, question: str, answer: str,
                                  action_id: Optional[str] = None) -> None:
    node_id = session.world_tree.active_node_id
    node = session.world_tree.get_node(node_id) if node_id else None
    if node is None:
        raise RuntimeError("Cannot persist query without an active node")
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT state_snapshot FROM tree_nodes WHERE id = ? AND save_id = ?",
            (node_id, session.save_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Active node is not persisted")
        snapshot = json.loads(row["state_snapshot"]) if row["state_snapshot"] else {}
        context = snapshot.setdefault("_node_context", {})
        exchanges = context.setdefault("query_exchanges", [])
        exchanges.append({"question": question, "answer": answer})
        await db.execute(
            "UPDATE tree_nodes SET state_snapshot = ? WHERE id = ? AND save_id = ?",
            (json.dumps(snapshot, ensure_ascii=False), node_id, session.save_id),
        )
        await db.execute(
            "UPDATE saves SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session.save_id,),
        )
        await _record_action_request(db, session.save_id, action_id)
        await db.commit()
    node["query_exchanges"] = copy.deepcopy(exchanges)


async def _save_node(session: GameSession, result: dict, action_id: Optional[str] = None) -> str | None:
    """Save a game turn node to the database. Returns scene_image_path if generated."""
    # Persist scene image to disk if present
    scene_image_path = None
    scene_img = result.get("scene_image")
    if scene_img and scene_img.get("base64"):
        try:
            import base64 as b64mod
            scenes_dir = os.path.join(str(DATA_DIR), "scenes", session.save_id)
            os.makedirs(scenes_dir, exist_ok=True)
            node_id = result.get("node_id", "unknown")
            filename = f"{node_id}.png"
            filepath = os.path.join(scenes_dir, filename)
            with open(filepath, "wb") as imgf:
                imgf.write(b64mod.b64decode(scene_img["base64"]))
            scene_image_path = f"/api/game/scenes/{session.save_id}/{filename}"
        except Exception as e:
            logger.warning("Failed to save scene image: %s", e)

    async with get_db() as db:
        node_id = result.get("node_id", "")
        node = session.world_tree.get_node(node_id)
        if node:
            # Check if node already exists (to avoid double-counting)
            cursor = await db.execute(
                "SELECT 1 FROM tree_nodes WHERE id = ?", (node_id,)
            )
            is_new = await cursor.fetchone() is None

            # 将 regenerate 需要的上下文嵌入 snapshot 序列化数据中（不改 DB schema）
            snapshot_to_save = result.get("state", {})
            node_context = {}
            if node.get("check_result"):
                node_context["check_result"] = node["check_result"]
            if node.get("triggered_consequences"):
                node_context["triggered_consequences"] = node["triggered_consequences"]
            if node.get("achieved_milestones"):
                node_context["achieved_milestones"] = node["achieved_milestones"]
            if node.get("thinking"):
                node_context["thinking"] = node["thinking"]
            for key in ("swipes", "active_swipe_index", "_shared_strip_keys", "agent_trace", "query_exchanges",
                        "turn_instructions", "stage_directives"):
                if key in node:
                    node_context[key] = node[key]
            if scene_image_path:
                node_context["scene_image_path"] = scene_image_path
            if node_context:
                snapshot_to_save = {**snapshot_to_save, "_node_context": node_context}

            await db.execute(
                """INSERT INTO tree_nodes
                   (id, save_id, parent_id, game_time, turn_number,
                    player_action, ai_response, choices_presented, dice_rolls,
                    state_changes, triggered_events, state_snapshot)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    save_id = excluded.save_id, parent_id = excluded.parent_id,
                    game_time = excluded.game_time, turn_number = excluded.turn_number,
                    player_action = excluded.player_action, ai_response = excluded.ai_response,
                    choices_presented = excluded.choices_presented,
                    dice_rolls = excluded.dice_rolls, state_changes = excluded.state_changes,
                    triggered_events = excluded.triggered_events,
                    state_snapshot = excluded.state_snapshot""",
                (
                    node_id,
                    session.save_id,
                    node.get("parent_id"),
                    node.get("game_time", ""),
                    node.get("turn_number", 0),
                    json.dumps(node.get("player_action"), ensure_ascii=False)
                    if node.get("player_action")
                    else None,
                    result.get("narrative", ""),
                    json.dumps(result.get("choices", []), ensure_ascii=False),
                    json.dumps(result.get("dice_rolls", []), ensure_ascii=False),
                    json.dumps(result.get("state_changes", []), ensure_ascii=False),
                    json.dumps(
                        [e.get("event_id", e) if isinstance(e, dict) else e
                         for e in result.get("triggered_events", [])],
                        ensure_ascii=False
                    ),
                    json.dumps(snapshot_to_save, ensure_ascii=False),
                ),
            )

            # Update save metadata — only increment total_nodes for new nodes
            play_delta = session._mark_activity()
            if is_new:
                await db.execute(
                    """UPDATE saves SET active_node_id = ?, total_nodes = total_nodes + 1,
                       play_time_seconds = play_time_seconds + ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (node_id, play_delta, session.save_id),
                )
            else:
                await db.execute(
                    """UPDATE saves SET active_node_id = ?,
                       play_time_seconds = play_time_seconds + ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (node_id, play_delta, session.save_id),
                )
            await _record_action_request(db, session.save_id, action_id)
            await db.commit()

        # Bug#1: 快照已持久化到 DB，现在提交内存驱逐
        parent_id = node.get("parent_id") if node else None
        if parent_id:
            session.world_tree.commit_pending_eviction(parent_id)

    return scene_image_path


async def _persist_undo(session: GameSession, result: dict, action_id: Optional[str] = None) -> None:
    """Persist a successful undo without rewriting the restored parent node."""
    async with get_db() as db:
        await db.execute("DELETE FROM tree_nodes WHERE id = ?", (result["removed_node_id"],))
        await db.execute(
            """UPDATE saves SET active_node_id = ?,
               total_nodes = MAX(total_nodes - 1, 0),
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (result["node_id"], session.save_id),
        )
        await _record_action_request(db, session.save_id, action_id)
        await db.commit()


def _fill_snapshot_metadata(script: dict, current_script: dict) -> None:
    policy = current_script.get("visibility_policy", {})
    if not policy.get("inherit_snapshot_metadata", False):
        return
    snapshot_policy = script.setdefault("visibility_policy", {})
    for field, value in policy.items():
        snapshot_policy.setdefault(field, copy.deepcopy(value))
    current_entries = {
        entry["id"]: entry for entry in current_script.get("lorebook", []) if entry.get("id")
    }
    for entry in script.get("lorebook", []):
        reference = current_entries.get(entry.get("id"), {})
        for field in ("visibility", "discoverable", "known_by_npcs"):
            if field not in entry and field in reference:
                entry[field] = copy.deepcopy(reference[field])
    current_events = {
        event["id"]: event
        for event in current_script.get("one_time_events", []) if event.get("id")
    }
    for event in script.get("one_time_events", []):
        reference = current_events.get(event.get("id"), {})
        for field in ("visibility", "known_by_npcs"):
            if (field not in event and field in reference
                    and event.get("trigger_time") == reference.get("trigger_time")):
                event[field] = copy.deepcopy(reference[field])
    current_cyclic = {event["id"]: event for event in current_script.get("cyclic_events", [])}
    for event in script.get("cyclic_events", []):
        reference = current_cyclic.get(event.get("id"), {})
        if "visibility" not in event and "visibility" in reference:
            event["visibility"] = reference["visibility"]
    current_nodes = {
        node["id"]: node for tree in current_script.get("story_tree", {}).get("trees", [])
        for node in tree.get("nodes", [])
    }
    for tree in script.get("story_tree", {}).get("trees", []):
        for node in tree.get("nodes", []):
            reference = current_nodes.get(node.get("id"), {})
            if "visibility" not in node and "visibility" in reference:
                node["visibility"] = reference["visibility"]
    current_npcs = {npc["id"]: npc for npc in current_script.get("npcs", []) if npc.get("id")}
    for npc in script.get("npcs", []):
        reference = current_npcs.get(npc.get("id"), {})
        if "public_profile" not in npc and "public_profile" in reference:
            npc["public_profile"] = copy.deepcopy(reference["public_profile"])
    current_relationships = {
        (rel.get("from"), rel.get("to")): rel
        for rel in current_script.get("npc_relationships", [])
    }
    for rel in script.get("npc_relationships", []):
        reference = current_relationships.get((rel.get("from"), rel.get("to")), {})
        for field in ("public_description", "public_type"):
            if field not in rel and field in reference:
                rel[field] = copy.deepcopy(reference[field])


async def _restore_session(save_id: str) -> GameSession | None:
    """Restore a game session from the database."""
    logger.info("恢复会话 — save_id=%s", save_id)
    async with get_db() as db:
        # Get save info
        cursor = await db.execute("SELECT * FROM saves WHERE id = ?", (save_id,))
        save_row = await cursor.fetchone()
        if not save_row:
            return None

        script_id = save_row["script_id"]

        # Load the save's starting script, falling back for older saves.
        cursor = await db.execute(
            "SELECT content FROM save_scripts WHERE save_id = ?", (save_id,)
        )
        script_row = await cursor.fetchone()
        has_saved_script = script_row is not None
        if not script_row:
            cursor = await db.execute(
                "SELECT content FROM scripts WHERE id = ?", (script_id,)
            )
            script_row = await cursor.fetchone()
        if script_row:
            script = json.loads(script_row["content"])
            if has_saved_script:
                cursor = await db.execute(
                    "SELECT content FROM scripts WHERE id = ?", (script_id,)
                )
                current_row = await cursor.fetchone()
                if current_row:
                    current_script = json.loads(current_row["content"])
                    _fill_snapshot_metadata(script, current_script)
        else:
            try:
                script = ScriptLoader.load(script_id)
            except FileNotFoundError:
                return None

        # Get AI provider
        ai_provider = await _get_ai_provider_async()
        stage_models = await _get_stage_models_async()

        # Create session
        session = GameSession(script, ai_provider, save_id=save_id, stage_models=stage_models)

        # Load tree nodes (ordered by created_at to ensure children_ids ordering)
        cursor = await db.execute(
            "SELECT * FROM tree_nodes WHERE save_id = ? ORDER BY created_at ASC, turn_number ASC",
            (save_id,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            node_data = {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "children_ids": [],
                "game_time": row["game_time"] or "",
                "turn_number": row["turn_number"] or 0,
                "player_action": json.loads(row["player_action"])
                if row["player_action"]
                else None,
                "ai_response": row["ai_response"] or "",
                "choices_presented": json.loads(row["choices_presented"])
                if row["choices_presented"]
                else [],
                "dice_rolls": json.loads(row["dice_rolls"])
                if row["dice_rolls"]
                else [],
                "state_changes": json.loads(row["state_changes"])
                if row["state_changes"]
                else [],
                "triggered_events": json.loads(row["triggered_events"])
                if row["triggered_events"]
                else [],
                "state_snapshot": json.loads(row["state_snapshot"])
                if row["state_snapshot"]
                else {},
                "created_at": row["created_at"] or "",
            }
            # 从 snapshot 中恢复 regenerate 上下文（_save_node 写入的 _node_context）
            nc = node_data["state_snapshot"].pop("_node_context", None)
            if nc and isinstance(nc, dict):
                node_data["check_result"] = nc.get("check_result")
                node_data["triggered_consequences"] = nc.get("triggered_consequences")
                node_data["achieved_milestones"] = nc.get("achieved_milestones")
                if nc.get("thinking"):
                    node_data["thinking"] = nc["thinking"]
                if nc.get("scene_image_path"):
                    node_data["scene_image_path"] = nc["scene_image_path"]
                for key in ("swipes", "active_swipe_index", "_shared_strip_keys", "agent_trace", "query_exchanges",
                            "turn_instructions", "stage_directives"):
                    if key in nc:
                        node_data[key] = nc[key]
            session.world_tree.nodes[row["id"]] = node_data

        # Rebuild parent-child relationships
        for node_id, node in session.world_tree.nodes.items():
            parent_id = node.get("parent_id")
            if parent_id and parent_id in session.world_tree.nodes:
                if node_id not in session.world_tree.nodes[parent_id]["children_ids"]:
                    session.world_tree.nodes[parent_id]["children_ids"].append(node_id)

        # Set root and active node
        for node_id, node in session.world_tree.nodes.items():
            if node["parent_id"] is None:
                session.world_tree.root_node_id = node_id
                break

        active_id = save_row["active_node_id"]
        if active_id and active_id in session.world_tree.nodes:
            session.world_tree.active_node_id = active_id
            active_node = session.world_tree.nodes[active_id]
            session.current_state = copy.deepcopy(active_node.get("state_snapshot", {}))
            session.turn_number = active_node.get("turn_number", 0)
            # 从存档恢复管线模式
            saved_mode = session.current_state.get("_pipeline_mode")
            if saved_mode:
                session.pipeline_mode = saved_mode

        root = session.world_tree.get_node(session.world_tree.root_node_id)
        if root:
            variant_id = root.get("state_snapshot", {}).get("_opening_variant_id", "")
            variant = next((item for item in script.get("opening_variants", [])
                            if item.get("id") == variant_id), None) if variant_id else None
            scripted_choices = session._opening_choice_source(variant)
            if scripted_choices and len(scripted_choices) == len(root.get("choices_presented", [])):
                root["choices_presented"] = session._build_opening_choices({"choices": scripted_choices})

        # Architecture#2: 恢复 authors_note
        authors_note = None
        an_position = "end"
        an_depth = 4
        try:
            authors_note = save_row["authors_note"]
            an_position = save_row["authors_note_position"] or "end"
            an_depth = save_row["authors_note_depth"] or 4
        except (KeyError, IndexError):
            pass
        if authors_note:
            session.set_authors_note(authors_note, an_position, an_depth)

        recursion = save_row["lorebook_max_recursion"]
        if recursion is not None:
            session.script.setdefault("settings", {})["lorebook_max_recursion"] = recursion
            session.prompt_builder.lorebook.max_recursion = recursion

        # Restore negative prompt
        try:
            neg_prompt = save_row["negative_prompt"] or ""
            if neg_prompt:
                session.negative_prompt = neg_prompt
        except (KeyError, IndexError):
            pass

        # Restore logit bias
        try:
            lb_raw = save_row["logit_bias"] or "[]"
            lb = json.loads(lb_raw)
            if lb:
                session.logit_bias = lb
        except (KeyError, IndexError, json.JSONDecodeError):
            pass

        # Restore dynamic story content (trees, events, lorebook from AI expansion)
        if session.current_state.get("dynamic_story_content"):
            session._restore_dynamic_story()

        session._hydrate_branch_runtime(session.current_state)

        # Re-index lorebook entries to VectorMemory (collection may be stale or missing)
        if session.vector_memory:
            lore_batch = []
            for entry in session.prompt_builder.lorebook.entries:
                if entry.content and len(entry.content) >= 20:
                    lore_batch.append((entry.id, entry.content, {
                        "entry_type": entry.entry_type,
                        "comment": entry.comment,
                    }))
            if lore_batch:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, session.vector_memory.add_lorebook_batch, lore_batch)

        # 初始化活跃时间
        session._last_activity_time = time.time()

        # Inject image provider if configured
        try:
            from api.config_routes import get_image_provider_instance
            session._image_provider = await get_image_provider_instance()
        except Exception:
            session._image_provider = None

        _sessions_put(save_id, session)
        logger.info("会话恢复完成 — save_id=%s, nodes=%d, turn=%d",
                     save_id, len(session.world_tree.nodes), session.turn_number)
        return session


async def _update_node_in_db(session: GameSession, result: dict):
    """Update an existing node in the database (used by regenerate/swipe)."""
    async with get_db() as db:
        node_id = result.get("node_id", "")
        node = session.world_tree.get_node(node_id)
        if node:
            snapshot_to_save = session.current_state
            node_context = {}
            if node.get("check_result"):
                node_context["check_result"] = node["check_result"]
            if node.get("triggered_consequences"):
                node_context["triggered_consequences"] = node["triggered_consequences"]
            if node.get("achieved_milestones"):
                node_context["achieved_milestones"] = node["achieved_milestones"]
            if node.get("thinking"):
                node_context["thinking"] = node["thinking"]
            for key in ("swipes", "active_swipe_index", "_shared_strip_keys", "agent_trace", "query_exchanges",
                        "turn_instructions", "stage_directives"):
                if key in node:
                    node_context[key] = node[key]
            if node_context:
                snapshot_to_save = {**snapshot_to_save, "_node_context": node_context}

            await db.execute(
                """UPDATE tree_nodes
                   SET ai_response = ?, choices_presented = ?,
                       state_changes = ?, state_snapshot = ?, game_time = ?, triggered_events = ?
                   WHERE id = ? AND save_id = ?""",
                (
                    result.get("narrative", ""),
                    json.dumps(result.get("choices", []), ensure_ascii=False),
                    json.dumps(result.get("state_changes", []), ensure_ascii=False),
                    json.dumps(snapshot_to_save, ensure_ascii=False),
                    node.get("game_time", ""),
                    json.dumps(node.get("triggered_events", []), ensure_ascii=False),
                    node_id,
                    session.save_id,
                ),
            )
            await db.commit()
