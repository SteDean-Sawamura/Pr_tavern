"""Background task scheduling, meta-event dispatch and async handlers."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from engine.meta_events import MetaEvent, MetaEventTrigger
from engine.event_scheduler import parse_time
from engine.turn_publication import current_publication

logger = logging.getLogger(__name__)

OFFSCREEN_SIM_INTERVAL = 3
PLAY_STYLE_INTERVAL = 5


class BackgroundTasksMixin:
    """Mixin for background task scheduling, meta-event bus, and all async handler methods."""

    def _register_meta_events(self):
        bus = self.meta_event_bus
        # --- Migrated existing background tasks ---
        bus.register(MetaEvent(
            id="summarize_history",
            handler="_handle_summarize_history",
            trigger=MetaEventTrigger(requires_ai=False),
        ))
        bus.register(MetaEvent(
            id="analyze_play_style",
            handler="_handle_analyze_play_style",
            trigger=MetaEventTrigger(
                cooldown_turns=PLAY_STYLE_INTERVAL,
                min_turn=PLAY_STYLE_INTERVAL,
            ),
        ))
        bus.register(MetaEvent(
            id="offscreen_simulation",
            handler="_handle_offscreen_simulation",
            trigger=MetaEventTrigger(turn_interval=OFFSCREEN_SIM_INTERVAL),
        ))
        bus.register(MetaEvent(
            id="ai_polish_chapters",
            handler="_handle_ai_polish_chapters",
            trigger=MetaEventTrigger(),
        ))
        bus.register(MetaEvent(
            id="story_director",
            handler="_handle_story_director",
            trigger=MetaEventTrigger(turn_interval=5, min_turn=5),
            priority=80,
        ))
        bus.register(MetaEvent(
            id="story_director_flag",
            handler="_handle_story_director",
            trigger=MetaEventTrigger(
                state_flag="_pending_story_director", consume_flag=True,
            ),
        ))
        # --- New system-level meta-events ---
        bus.register(MetaEvent(
            id="lorebook_evolution",
            handler="_handle_lorebook_evolution",
            trigger=MetaEventTrigger(cooldown_turns=3, min_turn=5),
        ))
        bus.register(MetaEvent(
            id="lorebook_evolution_urgent",
            handler="_handle_lorebook_evolution",
            trigger=MetaEventTrigger(
                state_flag="_pending_lore_evolution", consume_flag=True,
            ),
        ))
        bus.register(MetaEvent(
            id="narrative_consistency",
            handler="_handle_narrative_consistency",
            trigger=MetaEventTrigger(
                cooldown_turns=2, min_turn=3, requires_vector_memory=True,
            ),
        ))
        bus.register(MetaEvent(
            id="player_behavior_profiling",
            handler="_handle_player_behavior_profiling",
            trigger=MetaEventTrigger(turn_interval=4, min_turn=4),
        ))
        bus.register(MetaEvent(
            id="story_feedback",
            handler="_handle_story_feedback",
            trigger=MetaEventTrigger(requires_ai=False),
            priority=200,
        ))
        bus.register(MetaEvent(
            id="event_stage",
            handler="_handle_event_stage",
            trigger=MetaEventTrigger(requires_ai=True),
            priority=250,
        ))
        # --- Gameplay innovation modules ---
        bus.register(MetaEvent(
            id="promise_extraction",
            handler="_handle_promise_extraction",
            trigger=MetaEventTrigger(turn_interval=1, requires_ai=True),
            priority=80,
        ))
        bus.register(MetaEvent(
            id="plan_progress_check",
            handler="_handle_plan_progress",
            trigger=MetaEventTrigger(turn_interval=1, requires_ai=True),
            priority=70,
        ))
        bus.register(MetaEvent(
            id="faction_warfare_tick",
            handler="_handle_faction_warfare",
            trigger=MetaEventTrigger(turn_interval=3, min_turn=5),
            priority=60,
        ))
        bus.register(MetaEvent(
            id="retroactive_revelation",
            handler="_handle_retroactive_revelation",
            trigger=MetaEventTrigger(
                state_flag="_pending_flashback", consume_flag=True,
                requires_ai=True,
            ),
            priority=90,
        ))
        bus.register(MetaEvent(
            id="npc_goal_conflict",
            handler="_handle_npc_goal_conflict",
            trigger=MetaEventTrigger(
                state_flag="_npc_conflict_pending", consume_flag=True,
                requires_ai=True,
            ),
            priority=85,
        ))
        bus.register(MetaEvent(
            id="director_notes",
            handler="_handle_director_notes",
            trigger=MetaEventTrigger(turn_interval=1, min_turn=2, requires_ai=True),
            priority=50,
        ))

    def _dispatch_meta_events(self, ctx: dict, parsed: dict,
                              player_action: dict, raw_response: str = ""):
        if ctx.get("route", {}).get("expand_story"):
            self.current_state["_pending_story_director"] = True
        events_to_fire = self.meta_event_bus.evaluate(
            turn_number=self.turn_number,
            state=self.current_state,
            condition_eval=self._evaluate_condition,
            ai_available=self.ai_provider is not None,
            vector_available=self.vector_memory is not None,
        )
        if not events_to_fire:
            return
        meta_ctx = {
            "turn_number": self.turn_number,
            "narrative": parsed.get("narrative", raw_response),
            "raw_response": raw_response,
            "parsed": parsed,
            "player_action": player_action,
            "old_time": ctx.get("old_time", ""),
            "new_time": self.current_state.get("game_time", ""),
            "state_changes": ctx.get("all_state_changes", []),
            "triggered_events": ctx.get("triggered_events", []),
            "activated_lore": ctx.get("activated_lore", []),
        }
        scheduled_handlers = set()
        for evt in events_to_fire:
            handler = getattr(self, evt.handler, None)
            if handler is None:
                continue
            if evt.trigger.state_flag and evt.trigger.consume_flag:
                self.current_state.pop(evt.trigger.state_flag, None)
            self.meta_event_bus.mark_fired(
                self.current_state, evt.id, self.turn_number,
            )
            if evt.handler in scheduled_handlers:
                continue
            scheduled_handlers.add(evt.handler)
            coroutine = handler(meta_ctx)
            publication = current_publication(self)
            if publication is None or not publication.defer(coroutine, "background"):
                self._schedule_background_task(coroutine)

    def _schedule_background_task(self, coro):
        """Schedule a background task with proper exception logging."""
        task_name = getattr(getattr(coro, "cr_code", None), "co_name", type(coro).__name__)
        started_at = time.monotonic()
        task = asyncio.create_task(coro, name=task_name)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            elapsed = time.monotonic() - started_at
            if elapsed >= 1.0:
                logger.info("后台任务耗时: %s %.2fs", t.get_name(), elapsed)
            exc = t.exception()
            if exc:
                logging.getLogger(__name__).error(
                    "后台任务异常: %s", exc, exc_info=exc
                )

        task.add_done_callback(_on_done)
        return task

    def _schedule_index_task(self, coro):
        publication = current_publication(self)
        if publication is not None and publication.defer(coro, "index"):
            return None
        task = self._schedule_background_task(coro)
        if task is not None:
            self._index_tasks.add(task)
            task.add_done_callback(self._index_tasks.discard)
        return task

    def _schedule_deferred_task(self, coro):
        task = self._schedule_background_task(coro)
        self._deferred_tasks.add(task)
        task.add_done_callback(self._deferred_tasks.discard)
        return task

    def _remove_lorebook_vector(self, entry_id: str):
        if not self.vector_memory:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.vector_memory.remove_lorebook(entry_id)
            return

        prior_index_tasks = tuple(self._index_tasks)

        async def remove_after_index():
            if prior_index_tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in prior_index_tasks),
                    return_exceptions=True,
                )
            await asyncio.to_thread(self.vector_memory.remove_lorebook, entry_id)

        self._schedule_index_task(remove_after_index())

    async def _drain_background_tasks(self, timeout: float | None = 10.0, *, include_index: bool = True,
                                      index_only: bool = False):
        """等待状态、索引或两类任务；读取索引时可只等待索引写入。"""
        if index_only:
            pending_tasks = self._index_tasks
        elif include_index:
            pending_tasks = self._background_tasks - self._deferred_tasks
        else:
            pending_tasks = self._background_tasks - self._index_tasks - self._deferred_tasks
        tasks = list(pending_tasks)
        if not tasks:
            return True
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        self._background_tasks -= done
        if pending:
            index_tasks = pending & self._index_tasks
            state_tasks = pending - index_tasks
            if state_tasks:
                logger.warning("_drain_background_tasks: %d 个后台任务超时(%ss)，强制取消: %s",
                               len(state_tasks), timeout, sorted(t.get_name() for t in state_tasks))
                for task in state_tasks:
                    task.cancel()
                await asyncio.gather(*state_tasks, return_exceptions=True)
                self._background_tasks -= state_tasks
            if index_tasks:
                logger.warning("_drain_background_tasks: %d 个索引任务超时(%ss)，保留写入: %s",
                               len(index_tasks), timeout, sorted(t.get_name() for t in index_tasks))
                if index_only:
                    return False
                await asyncio.gather(
                    *(asyncio.shield(task) for task in index_tasks), return_exceptions=True,
                )
                self._background_tasks -= index_tasks
        if include_index and not index_only:
            later_index_tasks = [task for task in self._index_tasks if not task.done()]
            if later_index_tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in later_index_tasks), return_exceptions=True,
                )
        return True

    async def _handle_summarize_history(self, meta_ctx: dict):
        await self._maybe_summarize_history()

    async def _handle_analyze_play_style(self, meta_ctx: dict):
        await self._maybe_analyze_play_style()

    async def _handle_offscreen_simulation(self, meta_ctx: dict):
        await self._run_offscreen_simulation()

    async def _handle_ai_polish_chapters(self, meta_ctx: dict):
        await self._maybe_ai_polish_chapters()

    async def _handle_story_director(self, meta_ctx: dict):
        await self._run_story_director()

    # --- New system-level handlers ---

    async def _handle_lorebook_evolution(self, meta_ctx: dict):
        """Unified lorebook maintenance via two parallel subtasks:

        1. Maintenance: rewrite/disable/enable existing entries affected by this turn
        2. Expansion: add new knowledge entries based on plot direction and time progression

        Absorbs responsibilities of the former _director_task_lorebook and
        _maybe_expand_lorebook into a single entry point.
        """
        state_changes = meta_ctx.get("state_changes", [])
        narrative = meta_ctx.get("narrative", "")
        lore_context = self.current_state.pop("_pending_lore_context", None)
        if not state_changes and not narrative and not lore_context:
            return

        game_time = self.current_state.get("game_time", "")
        old_time = meta_ctx.get("old_time", "")
        new_time = meta_ctx.get("new_time", "")

        # Detect significant time jump
        time_jump_desc = ""
        if old_time and new_time:
            try:
                dt_old = parse_time(old_time)
                dt_new = parse_time(new_time)
                if dt_old and dt_new and (dt_old.year != dt_new.year or dt_old.month != dt_new.month):
                    time_jump_desc = f"{old_time} → {new_time}"
            except Exception:
                pass

        # Run maintenance and expansion in parallel
        maintenance_task = self._lore_evo_maintenance(
            state_changes, narrative, game_time,
        )
        expansion_task = self._lore_evo_expansion(
            game_time, time_jump_desc, lore_context,
        )
        results = await asyncio.gather(maintenance_task, expansion_task, return_exceptions=True)

        maintenance_updates = results[0] if isinstance(results[0], list) else []
        expansion_updates = results[1] if isinstance(results[1], list) else []

        all_updates = maintenance_updates + expansion_updates
        if not all_updates:
            return

        await self._apply_lore_evolution_updates(all_updates)

    async def _lore_evo_maintenance(
        self, state_changes: list, narrative: str, game_time: str,
    ) -> list:
        """Subtask 1: Maintain existing lorebook entries (rewrite/disable/enable)."""
        if not state_changes and not narrative:
            return []

        EVOLVABLE_TYPES = {
            "pc_identity", "npc_profile", "npc_relationship",
            "location_desc", "event_context", "evolution",
            "dynamic", "world_sync", "story_event", "player_behavior",
        }

        # Build scan text for relevance filtering
        scan_text = (narrative[:500] + " " + json.dumps(state_changes[:10], ensure_ascii=False)[:500]).lower()

        # Split entries into "relevant" (full detail) vs "background" (id only)
        relevant_entries = []
        background_ids = []
        disabled_candidates = []

        for entry in self.prompt_builder.lorebook.entries:
            if not entry.enabled and entry.entry_type in EVOLVABLE_TYPES:
                disabled_candidates.append({
                    "id": entry.id, "comment": entry.comment,
                    "content": entry.content[:150], "status": "disabled",
                })
                continue
            if not entry.enabled:
                continue
            if entry.constant and entry.entry_type not in EVOLVABLE_TYPES:
                continue

            # Relevance check: activated this turn OR keywords in scan_text
            is_relevant = entry.last_activated_turn == self.turn_number
            if not is_relevant:
                for k in entry.keys[:5]:
                    if k and k.lower() in scan_text:
                        is_relevant = True
                        break

            if is_relevant:
                relevant_entries.append({
                    "id": entry.id, "type": entry.entry_type,
                    "comment": entry.comment, "content": entry.content[:300],
                })
            else:
                background_ids.append(f"{entry.id}({entry.comment or ','.join(entry.keys[:2])})")

        if not relevant_entries and not disabled_candidates:
            return []

        change_summary = json.dumps(state_changes[:20], ensure_ascii=False)[:800]
        system = (
            "你是知识库维护助手。根据本回合的叙事和状态变化，判断哪些条目需要更新。\n"
            "操作类型：\n"
            "- rewrite: 内容已过时，用新内容替换\n"
            "- disable: 条目完全失效（角色死亡/退场、地点永久毁坏等）\n"
            "- enable: 之前被禁用的条目重新生效\n\n"
            "只处理下方【相关条目】中的条目。如无需变更，输出空数组 []。\n"
            "输出 JSON 数组:\n"
            '[{"id":"条目ID","action":"rewrite|disable|enable",'
            '"new_content":"rewrite时提供新内容","new_keys":["更新后的关键词"]}]\n'
            "最多 3 条操作。"
        )
        user_msg = f"当前游戏时间: {game_time or '未知'}\n"
        if change_summary:
            user_msg += f"\n状态变化:\n{change_summary}\n"
        if narrative:
            user_msg += f"\n叙事:\n{narrative[:400]}\n"
        user_msg += f"\n【相关条目】（可能受本轮影响）:\n{json.dumps(relevant_entries, ensure_ascii=False)}\n"
        if disabled_candidates:
            user_msg += f"\n【已禁用条目】（可enable）:\n{json.dumps(disabled_candidates[:8], ensure_ascii=False)}\n"
        if background_ids:
            user_msg += f"\n【其他条目ID】（未受影响，仅供参考）: {'; '.join(background_ids[:30])}\n"

        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="lore_evo_maintenance", stage="knowledge_graph",
        )
        return result if isinstance(result, list) else []

    async def _lore_evo_expansion(
        self, game_time: str, time_jump_desc: str, lore_context: dict | None,
    ) -> list:
        """Subtask 2: Expand lorebook with new knowledge entries."""
        # Only expand when there's a reason: time jump, new dynamic content, or periodic
        evo_count = len(self.current_state.get("lorebook_evolutions", []))
        periodic_trigger = (evo_count % 3 == 0)
        if not time_jump_desc and not lore_context and not periodic_trigger:
            return []

        # Existing entry IDs for dedup (no content needed)
        existing_ids = [
            e.id for e in self.prompt_builder.lorebook.entries
            if e.enabled and not e.id.startswith("_kg_")
        ]

        # Plot blueprint for direction
        bp = self.current_state.get("plot_blueprint", {})
        active_stages = []
        if bp.get("plot_threads"):
            for t in bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        system = (
            "你是世界知识库扩展助手。根据剧情方向和世界进展，生成新的知识条目。\n"
            "规则：\n"
            "- 新条目用第三人称客观视角，每条 80-150 字\n"
            "- 不要与已有条目重复\n"
            "- 内容应是有长期参考价值的知识（角色背景/历史事件/地理/组织信息）\n"
            "- 不要记录琐碎日常或临时状态\n"
            "- 如果提供了外部参考资料，优先基于这些资料确保事实准确，但须适配世界观\n\n"
            "如无需新增，输出空数组 []。\n"
            "输出 JSON 数组:\n"
            '[{"id":"_dyn_xxx","action":"add","new_content":"条目内容",'
            '"new_keys":["关键词1","关键词2"],"comment":"简短标签",'
            '"entry_type":"npc_profile|location_desc|event_context|story_event"}]\n'
            "最多 3 条。"
        )

        user_msg = f"当前游戏时间: {game_time or '未知'}\n"
        if active_stages:
            user_msg += f"活跃剧情方向: {'; '.join(active_stages)}\n"
        if time_jump_desc:
            user_msg += f"时间跨度: {time_jump_desc}（请补充该期间的重大世界事件）\n"
        if lore_context:
            parts = []
            for desc in lore_context.get("new_nodes", []):
                if desc:
                    parts.append(f"新剧情节点: {desc}")
            for desc in lore_context.get("new_events", []):
                if desc:
                    parts.append(f"新事件: {desc}")
            if parts:
                user_msg += "本轮新增动态内容:\n" + "\n".join(f"- {p}" for p in parts) + "\n"
        user_msg += f"\n已有条目ID（避免重复）: {', '.join(existing_ids[:50])}\n"

        # Web search grounding
        should_search = bool(time_jump_desc) or bool(lore_context)
        if should_search:
            world_bg = self.script.get("world_background", "")[:200]
            recent_events = "；".join(
                e.get("event", "")[:40]
                for e in self.current_state.get("key_events", [])[-5:]
            )
            pc = self.current_state.get("player", {})
            try:
                reference = await self._search_for_expansion(world_bg, recent_events, pc)
                if reference:
                    user_msg += f"\n{reference}"
            except Exception:
                pass

        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="lore_evo_expansion", stage="knowledge_graph",
        )
        return result if isinstance(result, list) else []

    async def _apply_lore_evolution_updates(self, updates: list):
        """Apply combined results from maintenance + expansion subtasks."""
        async with self._state_lock:
            dynamic = self.current_state.setdefault("dynamic_lorebook", [])
            vector_batch = []
            applied = 0
            for upd in updates:
                if not isinstance(upd, dict) or applied >= 6:
                    continue
                entry_id = upd.get("id", "")
                action = upd.get("action", "")
                if action == "disable":
                    self._set_lore_enabled(entry_id, False)
                    applied += 1
                elif action == "enable":
                    self._set_lore_enabled(entry_id, True)
                    applied += 1
                elif action == "rewrite" and upd.get("new_content"):
                    self.prompt_builder.lorebook.update_entry(
                        entry_id, content=upd["new_content"],
                        keys=upd.get("new_keys"),
                    )
                    for dl in dynamic:
                        if isinstance(dl, dict) and dl.get("id") == entry_id:
                            dl["content"] = upd["new_content"]
                            if upd.get("new_keys"):
                                dl["keys"] = upd["new_keys"]
                            break
                    if self.vector_memory and len(upd["new_content"]) >= 20:
                        vector_batch.append((
                            entry_id, upd["new_content"],
                            {"entry_type": upd.get("entry_type", "")},
                        ))
                    applied += 1
                elif action == "add" and upd.get("new_content") and entry_id:
                    existing = any(
                        e.id == entry_id for e in self.prompt_builder.lorebook.entries
                    )
                    if not existing:
                        new_entry = {
                            "id": entry_id,
                            "keys": upd.get("new_keys", []),
                            "content": upd["new_content"],
                            "comment": upd.get("comment", ""),
                            "entry_type": upd.get("entry_type", "dynamic"),
                            "priority": 80,
                            "position": "after_world",
                            "scan_depth": 3,
                            "enabled": True,
                        }
                        self.prompt_builder.lorebook.add_entries([new_entry])
                        dynamic.append(new_entry)
                        if self.vector_memory and len(upd["new_content"]) >= 20:
                            vector_batch.append((
                                entry_id, upd["new_content"],
                                {"entry_type": new_entry["entry_type"]},
                            ))
                        applied += 1
            if vector_batch:
                self._schedule_index_task(
                    self._async_lorebook_vector_sync(vector_batch)
                )
            evolutions = self.current_state.setdefault("lorebook_evolutions", [])
            evolutions.append({
                "turn": self.turn_number,
                "updates": [
                    {"id": u.get("id"), "action": u.get("action")}
                    for u in updates[:6] if isinstance(u, dict)
                ],
            })

            # Smart capacity management for dynamic lorebook
            MAX_DYNAMIC = 40
            if len(dynamic) > MAX_DYNAMIC:
                PROTECTED_TYPES = {"npc_profile", "npc_relationship"}
                stale_threshold = self.turn_number - 5
                scored = []
                for i, dl in enumerate(dynamic):
                    if not isinstance(dl, dict):
                        scored.append((i, 999))
                        continue
                    dl_id = dl.get("id", "")
                    entry_obj = None
                    for e in self.prompt_builder.lorebook.entries:
                        if e.id == dl_id:
                            entry_obj = e
                            break
                    if entry_obj and entry_obj.entry_type in PROTECTED_TYPES:
                        scored.append((i, 999))
                        continue
                    last_active = entry_obj.last_activated_turn if entry_obj else 0
                    is_speculative = dl.get("speculative", False)
                    score = last_active
                    if is_speculative and last_active < stale_threshold:
                        score -= 1000
                    scored.append((i, score))
                scored.sort(key=lambda x: x[1])
                to_remove = len(dynamic) - MAX_DYNAMIC
                remove_indices = set(scored[i][0] for i in range(to_remove))
                for idx in remove_indices:
                    dl = dynamic[idx]
                    if isinstance(dl, dict) and dl.get("id"):
                        self.prompt_builder.lorebook.remove_entry(dl["id"])
                        self._remove_lorebook_vector(dl["id"])
                dynamic[:] = [dl for i, dl in enumerate(dynamic) if i not in remove_indices]

    async def _handle_narrative_consistency(self, meta_ctx: dict):
        """RAG-based contradiction detection against past history."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative or len(narrative) < 50:
            return
        loop = asyncio.get_running_loop()
        branch_node_ids = {node["id"] for node in self.world_tree.get_active_branch()}
        hits = await loop.run_in_executor(
            None, self.vector_memory.query,
            narrative[:300], 5, [self.turn_number], "turn", branch_node_ids,
        )
        if not hits:
            return
        past_context = "\n".join(
            f"[第{h['turn']}回合] {h['text'][:200]}" for h in hits
        )
        system = (
            "你是一个叙事一致性检查助手。对比当前叙事与过去的记录，"
            "找出事实矛盾（例如：已死的NPC又出现、已丢失的物品又被使用、"
            "地点描述前后不一致等）。\n"
            "如果没有矛盾，输出空数组 []。\n"
            "如有矛盾，输出 JSON 数组: "
            '[{"contradiction":"矛盾描述","correction_hint":"修正建议"}]'
        )
        user_msg = (
            f"当前叙事（第{self.turn_number}回合）:\n{narrative[:600]}\n\n"
            f"过去记录:\n{past_context}"
        )
        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="narrative_consistency", stage="summary",
        )
        if not result or not isinstance(result, list):
            return
        async with self._state_lock:
            for item in result[:3]:
                if not isinstance(item, dict):
                    continue
                hint = item.get("correction_hint", "")
                if hint:
                    self._record_narrative_callback(
                        f"[一致性修正] {hint}",
                        tags=["consistency"], priority="high",
                    )

    async def _handle_player_behavior_profiling(self, meta_ctx: dict):
        """Analyze player behavior patterns and update implicit lorebook entries."""
        branch = self.world_tree.get_active_branch()
        window = branch[-8:] if len(branch) >= 8 else branch
        actions = []
        for node in window:
            action = node.get("player_action")
            if action:
                text = action.get("text", "") if isinstance(action, dict) else str(action)
                if text:
                    actions.append(f"T{node.get('turn_number', '?')}: {text}")
        if len(actions) < 3:
            return

        existing_profile = self.current_state.get("player_behavior_profile", {})
        existing_tags = existing_profile.get("tags", [])

        system = (
            "你是一个玩家行为分析助手。根据玩家最近的行动序列，"
            "提取行为模式标签和隐性偏好。输出 JSON:\n"
            '{"tags":["外交倾向","收集癖",...], '
            '"preferences":"一句话总结", '
            '"lorebook_entries":[{"id":"player_pref_XXX","keys":[...],'
            '"content":"...","comment":"玩家倾向"}]}'
        )
        user_msg = (
            f"已有标签: {existing_tags}\n\n"
            f"最近行动:\n" + "\n".join(actions)
        )
        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json,
            label="player_profiling", stage="summary",
        )
        if not result or not isinstance(result, dict):
            return

        async with self._state_lock:
            self.current_state["player_behavior_profile"] = {
                "tags": result.get("tags", [])[:10],
                "preferences": result.get("preferences", "")[:100],
                "last_turn": self.turn_number,
            }
            new_entries = result.get("lorebook_entries", [])
            for entry_data in new_entries[:3]:
                if not isinstance(entry_data, dict) or not entry_data.get("id"):
                    continue
                entry_data.setdefault("priority", 60)
                entry_data.setdefault("position", "after_world")
                entry_data.setdefault("scan_depth", 3)
                entry_data["entry_type"] = "player_behavior"
                entry_data["enabled"] = True
                existing = any(
                    e.id == entry_data["id"]
                    for e in self.prompt_builder.lorebook.entries
                )
                if existing:
                    self.prompt_builder.lorebook.update_entry(
                        entry_data["id"],
                        content=entry_data.get("content", ""),
                        keys=entry_data.get("keys"),
                    )
                else:
                    self.prompt_builder.lorebook.add_entries([entry_data])

    async def _handle_story_feedback(self, meta_ctx: dict):
        """Pipeline output → story tree feedback: quest auto-completion + NPC attitude events."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative:
            return

        async with self._state_lock:
            self._check_quest_keywords(narrative)

            npc_thresholds = self.script.get("settings", {}).get("npc_attitude_events", {})
            if npc_thresholds:
                for npc_id, thresholds in npc_thresholds.items():
                    npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
                    if not isinstance(npc_data, dict):
                        continue
                    attitude = npc_data.get("attitude_toward_player", 50)
                    for threshold in thresholds:
                        evt = threshold.get("event", "")
                        val = threshold.get("value", 0)
                        op = threshold.get("op", ">=")
                        fired_key = f"_npc_att_evt_{npc_id}_{evt}"
                        if self.current_state.get(fired_key):
                            continue
                        should_fire = (op == ">=" and attitude >= val) or (op == "<=" and attitude <= val)
                        if should_fire:
                            self._fire_event_dual(evt)
                            self.current_state[fired_key] = True

    async def _handle_event_stage(self, meta_ctx: dict):
        """Async event CRUD stage: AI evaluates narrative and proposes event changes."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative:
            return

        # 读取阶段：短暂持锁
        async with self._state_lock:
            event_data = self.event_engine.get_events_for_prompt(self.current_state)
            parsed_summary = self._build_parsed_summary(meta_ctx)
            action_text = meta_ctx.get("player_action", {}).get("text", "")
            turn = self.turn_number

        messages, system = self.prompt_builder.build_event_stage_prompt(
            narrative, action_text, event_data, parsed_summary,
        )

        # AI 调用：不持锁
        try:
            raw = await self.ai_provider.generate(
                messages, system=system, max_tokens=4096,
                **self._stage_kwargs("state"),
            )
        except Exception as e:
            logger.error("Event stage AI 生成失败: %s", e)
            return

        parsed_data = self.response_parser._extract_json_from_raw(raw)
        if not isinstance(parsed_data, dict):
            return
        changes = parsed_data.get("event_changes", [])
        if not isinstance(changes, list) or not changes:
            return

        # 写入阶段：短暂持锁
        async with self._state_lock:
            self.event_engine.apply_event_changes(self.current_state, changes, turn)
            lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
            lb_ids = {e.id for e in lb.entries} if lb else None
            for change in changes:
                self._sync_event_to_lorebook(change, self.current_state, lb_ids)
                _fields = change.get("fields", {})
                if change.get("action") == "update" and _fields.get("status") in ("resolved", "failed"):
                    cat = change.get("category", "")
                    if not cat and change.get("id") in self.event_engine.events:
                        cat = self.event_engine.events[change["id"]].category
                    if cat == "thread":
                        self._fire_event_dual(f"thread.{_fields['status']}.{change.get('id', '')}")

    def _build_parsed_summary(self, meta_ctx: dict) -> str:
        """Extract a concise text summary of this turn's state changes from meta_ctx."""
        parts = []
        state_changes = meta_ctx.get("state_changes", [])
        if state_changes:
            sc_lines = [f"{c.get('target', '?')}: {c.get('new', c.get('value', ''))}" for c in state_changes[:5]]
            parts.append("属性变化: " + "; ".join(sc_lines))
        parsed = meta_ctx.get("parsed", {})
        if parsed.get("npc_attitude_changes"):
            att_lines = [f"{c.get('npc_id', '?')}: {c.get('change', 0):+d}" for c in parsed["npc_attitude_changes"][:5]]
            parts.append("NPC态度: " + "; ".join(att_lines))
        events = meta_ctx.get("triggered_events", [])
        if events:
            parts.append("触发事件: " + ", ".join(e.get("event_id", "?") for e in events[:5]))
        return "\n".join(parts) if parts else "无显著变化"

    def _sync_event_to_lorebook(self, change: dict, state: dict, _existing_ids: set | None = None):
        """Unified lorebook sync for event CRUD changes."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        action = change.get("action", "create")
        eid = change.get("id", "")
        category = change.get("category", "")
        if not eid:
            return

        if action == "delete":
            for prefix in ("_cons_", "_clue_", "_thread_"):
                lb.remove_entry(f"{prefix}{eid}")
            return

        existing_ids = _existing_ids if _existing_ids is not None else {e.id for e in lb.entries}

        if action == "update":
            _fields = change.get("fields", {})
            if not category and eid in self.event_engine.events:
                category = self.event_engine.events[eid].category
            if category == "thread":
                entry_id = f"_thread_{eid}"
                new_status = _fields.get("status", "")
                if new_status in ("resolved", "failed"):
                    lb.remove_entry(entry_id)
                    return
                ev = self.event_engine.events.get(eid)
                name = change.get("name") or (ev.name if ev else eid)
                desc = _fields.get("description") or (ev.description if ev else "")
                content = f"叙事线·{name}: {desc}"
                if new_status == "dormant":
                    content += "（暂时搁置）"
                keys = self._extract_event_lorebook_keys(f"{name} {desc}", state)
                if not keys:
                    keys = [name]
                if entry_id in existing_ids:
                    lb.update_entry(entry_id, content, keys=keys)
                else:
                    lb.add_entries([{
                        "id": entry_id, "keys": keys, "content": content,
                        "priority": 58, "position": "after_world",
                        "entry_type": "narrative_thread",
                    }])
            return

        if action != "create":
            return

        desc = change.get("description", "")
        if not desc:
            return
        keys = self._extract_event_lorebook_keys(desc, state)

        if category == "consequence":
            entry_id = f"_cons_{eid}"
            if entry_id in existing_ids:
                return
            if not keys:
                words = [w for w in desc.split() if len(w) >= 3]
                keys = words[:2] if words else [desc[:8]]
            importance = change.get("importance", "normal")
            content = f"潜在后果: {desc}"
            if importance == "high":
                content += "（高重要性）"
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 55 if importance == "normal" else 70,
                "position": "after_world", "entry_type": "consequence_context",
            }])

        elif category == "clue":
            entry_id = f"_clue_{eid}"
            if entry_id in existing_ids:
                return
            clue_cat = change.get("metadata", {}).get("category", "事件") if isinstance(change.get("metadata"), dict) else "事件"
            source = change.get("metadata", {}).get("source", "") if isinstance(change.get("metadata"), dict) else ""
            if not keys:
                keys = [clue_cat]
                if source:
                    keys.append(source[:10])
            content = f"[线索·{clue_cat}] {desc}"
            if source:
                content += f"（来源: {source}）"
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 55, "position": "after_world",
                "entry_type": "clue",
            }])

        elif category == "thread":
            entry_id = f"_thread_{eid}"
            if entry_id in existing_ids:
                return
            name = change.get("name", eid)
            content = f"叙事线·{name}: {desc}"
            if not keys:
                keys = [name]
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 58, "position": "after_world",
                "entry_type": "narrative_thread",
            }])

    def _extract_event_lorebook_keys(self, text: str, state: dict) -> list[str]:
        """Extract NPC names and location names from text as lorebook keys."""
        keys = []
        npc_names = {n.get("name", n["id"]) for n in self.script.get("npcs", []) if n.get("name")}
        loc_names = set(state.get("display_names", {}).values())
        for name in npc_names:
            if name in text:
                keys.append(name)
        for loc in loc_names:
            if loc and len(loc) >= 2 and loc in text:
                keys.append(loc)
        return keys

    @staticmethod
    def _parse_simple_json_list(text: str) -> list | None:
        """Extract a JSON list from text using bracket balancing."""
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
        start = text.find('[')
        if start == -1:
            return None
        depth = 0
        in_string = False
        i = start
        while i < len(text):
            c = text[i]
            if in_string:
                if c == '\\':
                    i += 2
                    continue
                if c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:i + 1])
                            if isinstance(data, list):
                                return data
                        except json.JSONDecodeError:
                            pass
                        return None
            i += 1
        return None

    async def _handle_promise_extraction(self, meta_ctx: dict):
        """Extract promises/appointments from narrative, register as deadline events."""
        route = self.current_state.get("_last_route", {})
        focus_npcs = route.get("focus_npcs", [])
        if not focus_npcs:
            return
        narrative = meta_ctx.get("narrative", "")
        player_action = meta_ctx.get("player_action", {})
        action_text = player_action.get("text", "") if isinstance(player_action, dict) else ""
        if not narrative:
            return

        # 迁移旧 promise_ledger → deadline events
        old_ledger = self.current_state.pop("promise_ledger", None)
        if old_ledger:
            for prm in old_ledger:
                if prm.get("status") != "pending":
                    continue
                npc_id = prm.get("npc_id", "")
                content = prm.get("content", "")
                npc_name = self._get_npc_display_name(npc_id)
                remaining = max(1, prm.get("deadline_turn", self.turn_number + 5) - self.turn_number)
                self.event_engine.add_deadline(self.current_state, {
                    "id": prm.get("id", f"_promise_migrated_{npc_id}"),
                    "description": f"[玩家承诺] 对{npc_name}: {content[:50]}",
                    "turns_remaining": remaining,
                    "on_expire": {"description": f"玩家未兑现对{npc_name}的承诺", "state_changes": [
                        {"target": f"npcs.{npc_id}.attitude_toward_player", "op": "add", "value": -15}
                    ]},
                    "visible": True,
                    "is_promise": True, "direction": "player_to_npc",
                    "npc_id": npc_id, "npc_name": npc_name, "content": content,
                    "is_lie": prm.get("is_lie", False),
                    "witnesses": prm.get("witnesses", []),
                }, self.turn_number)

        # 获取已有承诺 deadline，避免重复提取
        existing_promise_ids = [
            eid for eid, ev in self.event_engine.events.items()
            if ev.category == "deadline" and ev.metadata.get("is_promise")
            and self.current_state.get("events", {}).get(eid, {}).get("status") == "active"
        ]
        existing_descs = [self.event_engine.events[eid].description for eid in existing_promise_ids]

        prompt = (
            f"玩家行动: {action_text}\n"
            f"叙事结果: {narrative[:800]}\n"
            f"在场NPC: {', '.join(focus_npcs)}\n"
            f"已记录的未完成承诺/约定: {existing_descs[-5:] or '无'}\n\n"
            "分析本轮对话中的承诺和约定（双向）：\n"
            "1. 玩家→NPC：承诺、保证、威胁、谎言\n"
            "2. NPC→玩家：指令、约定、邀请、安排（如'明天来值班''今晚有宴'）\n"
            "3. 是否兑现了已记录的承诺\n\n"
            '返回JSON: {"promises":['
            '{"npc_id":"NPC的ID","content":"承诺/约定的具体内容",'
            '"direction":"player_to_npc或npc_to_player",'
            '"is_lie":false,"urgency":"immediate|today|tomorrow|days|weeks"}],'
            '"fulfilled":["已兑现承诺的content关键词"]}\n'
            "无承诺/兑现则返回空列表。只返回JSON。"
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是对话分析器，提取承诺、约定和指令。只返回JSON。",
                max_tokens=400,
                **self._stage_kwargs("knowledge_graph"),
            )
            data = json.loads(raw.strip().strip("```json").strip("```").strip())
        except Exception:
            return

        present_npcs = self.current_state.get("_last_present_npcs", focus_npcs)
        urgency_to_turns = {
            "immediate": 2, "today": 3, "tomorrow": 5, "days": 10, "weeks": 15,
        }

        for p in data.get("promises", [])[:3]:
            npc_id = p.get("npc_id", "")
            content = p.get("content", "")
            if not npc_id or not content:
                continue
            direction = p.get("direction", "player_to_npc")
            urgency = p.get("urgency", "days")
            turns = urgency_to_turns.get(urgency, 10)

            dl_id = f"_promise_{self.turn_number}_{npc_id}_{len(existing_promise_ids)}"
            npc_name = self._get_npc_display_name(npc_id)

            if direction == "player_to_npc":
                on_expire = {
                    "description": f"玩家未兑现对{npc_name}的承诺「{content[:30]}」",
                    "state_changes": [
                        {"target": f"npcs.{npc_id}.attitude_toward_player", "op": "add", "value": -15}
                    ],
                }
                desc = f"[玩家承诺] 对{npc_name}: {content[:50]}"
            else:
                on_expire = {
                    "description": f"{npc_name}安排的「{content[:30]}」已到时间",
                }
                desc = f"[{npc_name}安排] {content[:50]}"

            self.event_engine.add_deadline(self.current_state, {
                "id": dl_id,
                "description": desc,
                "turns_remaining": turns,
                "on_expire": on_expire,
                "on_complete": {"condition": "", "description": f"承诺已兑现: {content[:30]}"},
                "visible": True,
                "is_promise": True,
                "direction": direction,
                "npc_id": npc_id,
                "npc_name": npc_name,
                "content": content[:100],
                "is_lie": bool(p.get("is_lie")),
                "witnesses": list(present_npcs[:5]),
            }, self.turn_number)

            if p.get("is_lie"):
                network = self.current_state.setdefault("information_network", [])
                network.append({
                    "id": f"lie_{self.turn_number}_{npc_id}",
                    "origin_turn": self.turn_number,
                    "fact": f"玩家对{npc_name}说了谎「{content[:30]}」",
                    "known_by": [npc_id],
                    "spread_chance": 0.3, "distortion": 1, "max_spread": 3,
                    "tags": ["lie", "social", npc_id],
                })

        for keyword in data.get("fulfilled", [])[:3]:
            if not keyword:
                continue
            for eid in existing_promise_ids:
                ev = self.event_engine.events.get(eid)
                if not ev:
                    continue
                if keyword.lower() in ev.description.lower() or keyword.lower() in ev.metadata.get("content", "").lower():
                    es = self.current_state.setdefault("events", {})
                    es.setdefault(eid, {})["status"] = "completed"
                    if ev.metadata.get("direction") == "player_to_npc":
                        prm_npc = ev.metadata.get("npc_id", "")
                        if prm_npc:
                            self.current_state, _ = self.state_manager.apply_changes(
                                self.current_state,
                                [{"target": f"npcs.{prm_npc}.attitude_toward_player",
                                  "op": "add", "value": 10,
                                  "reason": f"兑现承诺: {ev.metadata.get('content', '')[:20]}"}],
                                inplace=True,
                            )
                    break

    # ================================================================
    # Director Notes: 叙事摘要中间层
    # ================================================================

    async def _handle_director_notes(self, meta_ctx: dict):
        """后台任务：从最近叙事中提取导演笔记"""
        if not hasattr(self, 'director_notes'):
            return
        if not self.world_tree:
            return
        recent = self.world_tree.get_recent_history(3)
        if not recent:
            return
        last = recent[-1]
        narrative = last.get("ai_response", "")
        if not narrative or len(narrative) < 100:
            return

        try:
            prompt = f"""从以下叙事中提取关键信息，调用 extract_notes 工具提交。最多3条，只提取重要信息。

叙事：
{narrative[:1000]}"""
            extract_tool = [{"type": "function", "function": {
                "name": "extract_notes",
                "parameters": {"type": "object", "properties": {
                    "notes": {"type": "array", "items": {"type": "object", "properties": {
                        "category": {"type": "string", "enum": ["character", "plot", "world", "relationship", "clue"]},
                        "content": {"type": "string", "description": "一句话描述"},
                    }, "required": ["category", "content"]}},
                }, "required": ["notes"]},
            }}]
            resp = await self.ai_provider.generate_with_tools(
                [{"role": "user", "content": prompt}],
                system="你是叙事分析员。提取关键信息为结构化笔记。",
                tools=extract_tool,
                max_tokens=300,
                **self._stage_kwargs("knowledge_graph"),
            )
            tc_list = resp.get("tool_calls") or []
            if tc_list:
                notes = (tc_list[0].get("arguments") or {}).get("notes", [])
            else:
                raw = (resp.get("content") or "").strip().strip('```json').strip('```')
                notes = json.loads(raw) if raw else []
            if not isinstance(notes, list):
                return
            for note in notes[:3]:
                if not isinstance(note, dict):
                    continue
                self.director_notes.add_note(
                    self.turn_number,
                    note.get("category", "plot"),
                    note.get("content", ""),
                )
            logger.info("导演笔记：添加 %d 条", min(len(notes), 3))
        except Exception as e:
            logger.warning("导演笔记提取失败: %s", e)

    # ================================================================
    # Feature #8: NPC Goal Conflict Detection
    # ================================================================

    def _check_npc_goal_conflicts(self):
        """Detect conflicts between NPC goals and flag for escalation."""
        conflicts = self.current_state.setdefault("npc_conflicts", [])
        active_conflict_pairs = {
            (c["npc_a"], c["npc_b"]) for c in conflicts if c.get("status") != "resolved"
        }
        goal_progress = self.current_state.get("npc_goal_progress", {})
        completed_ids = {
            (g["id"] if isinstance(g, dict) else g)
            for g in self.current_state.get("completed_npc_goals", [])
        }

        npc_active_goals = {}
        all_npc_defs = list(self.script.get("npcs", []))
        npcs_state = self.current_state.get("npcs", {})
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_goals = dyn_st.get("goals", [])
                if dyn_goals:
                    all_npc_defs.append({"id": dyn_id, "name": dyn_st.get("name", dyn_id), "goals": dyn_goals})
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            goals = npc_def.get("goals", [])
            if not goals:
                continue
            for goal in goals:
                full_id = f"{npc_id}:{goal.get('id', '')}"
                if full_id in completed_ids:
                    continue
                conflicts_with = goal.get("conflicts_with", [])
                if conflicts_with:
                    npc_active_goals.setdefault(npc_id, []).append({
                        "goal": goal,
                        "conflicts_with": conflicts_with,
                    })

        for npc_a, goals_a in npc_active_goals.items():
            for entry_a in goals_a:
                for target in entry_a["conflicts_with"]:
                    parts = target.split(":", 1)
                    if len(parts) != 2:
                        continue
                    npc_b, goal_b_id = parts
                    if npc_b not in npc_active_goals:
                        continue
                    pair = tuple(sorted([npc_a, npc_b]))
                    if pair in active_conflict_pairs:
                        continue
                    prog_a = goal_progress.get(npc_a, {}).get("progress", 0)
                    prog_b = goal_progress.get(npc_b, {}).get("progress", 0)
                    if prog_a >= 30 or prog_b >= 30 or self.turn_number >= 10:
                        goal_b_desc = goal_b_id
                        for eg in npc_active_goals.get(npc_b, []):
                            if eg["goal"].get("id", "") == goal_b_id:
                                goal_b_desc = eg["goal"].get("description", goal_b_id)[:50]
                                break
                        conflicts.append({
                            "id": f"conflict_{npc_a}_{npc_b}_{self.turn_number}",
                            "npc_a": pair[0],
                            "npc_b": pair[1],
                            "goal_a": entry_a["goal"].get("description", "")[:50],
                            "goal_b": goal_b_desc,
                            "turn_started": self.turn_number,
                            "status": "brewing",
                            "player_sided_with": None,
                        })
                        active_conflict_pairs.add(pair)
                        self.current_state["_npc_conflict_pending"] = {
                            "npc_a": pair[0], "npc_b": pair[1],
                            "goal_a": entry_a["goal"].get("description", "")[:50],
                        }
                        break

        if len(conflicts) > 20:
            self.current_state["npc_conflicts"] = [
                c for c in conflicts if c.get("status") != "resolved"
            ][-20:]

    async def _handle_npc_goal_conflict(self, meta_ctx: dict):
        """Generate conflict escalation event between NPCs."""
        pending = self.current_state.get("_npc_conflict_pending")
        if not pending:
            conflict_list = [c for c in self.current_state.get("npc_conflicts", [])
                            if c.get("status") == "brewing"]
            if not conflict_list:
                return
            pending = conflict_list[0]

        npc_a = pending.get("npc_a", "")
        npc_b = pending.get("npc_b", "")
        name_a = self._get_npc_display_name(npc_a)
        name_b = self._get_npc_display_name(npc_b)
        goal_a = pending.get("goal_a", "")

        for c in self.current_state.get("npc_conflicts", []):
            if c.get("npc_a") == npc_a and c.get("npc_b") == npc_b and c.get("status") == "brewing":
                c["status"] = "active"
                break

        network = self.current_state.setdefault("information_network", [])
        network.append({
            "id": f"conflict_{npc_a}_{npc_b}_{self.turn_number}",
            "origin_turn": self.turn_number,
            "fact": f"{name_a}与{name_b}因目标冲突产生对立",
            "known_by": [npc_a, npc_b],
            "spread_chance": 0.6,
            "distortion": 0,
            "max_spread": 6,
            "tags": ["npc_conflict", npc_a, npc_b],
        })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

        npc_rels = self.current_state.setdefault("npc_relationships_global", {})
        rel_key = "_".join(sorted([npc_a, npc_b]))
        rel = npc_rels.setdefault(rel_key, {"from": npc_a, "to": npc_b, "type": "neutral", "value": 50})
        rel["value"] = max(0, rel.get("value", 50) - 20)
        rel["type"] = "hostile" if rel["value"] < 20 else "tense"

        self._record_narrative_callback(
            f"{name_a}与{name_b}的矛盾开始激化",
            ["npc_conflict", npc_a, npc_b], priority="high",
        )

    # ================================================================
    # Feature #3: Player Multi-turn Planning System
    # ================================================================

    @staticmethod
    def _extract_plan_steps(plot_decision: str) -> list[str]:
        """Extract plan_steps from Stage 1 plot decision text."""
        import re as _re
        match = _re.search(r'plan_steps\s*:\s*\[([^\]]*)\]', plot_decision)
        if not match:
            match = _re.search(r'"plan_steps"\s*:\s*\[([^\]]*)\]', plot_decision)
        if not match:
            return []
        try:
            items = json.loads("[" + match.group(1) + "]")
            return [s for s in items if isinstance(s, str)][:5]
        except Exception:
            raw = match.group(1)
            return [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()][:5]

    @staticmethod
    def _extract_plan_risk(plot_decision: str) -> str:
        """Extract plan_risk level from Stage 1 plot decision text."""
        import re as _re
        match = _re.search(r'plan_risk\s*:\s*"?(low|medium|high)"?', plot_decision)
        return match.group(1) if match else "medium"

    async def _handle_plan_progress(self, meta_ctx: dict):
        """Check if current turn advanced the player's active plan."""
        plan = self.current_state.get("pending_plan")
        if not plan or plan.get("status") != "active":
            return
        remaining = plan.get("steps_remaining", [])
        if not remaining:
            plan["status"] = "completed"
            self._record_narrative_callback(
                f"计划「{plan.get('goal', '')[:20]}」已完成",
                ["plan_complete"], priority="high",
            )
            return

        narrative = meta_ctx.get("narrative", "")
        action_text = ""
        pa = meta_ctx.get("player_action")
        if isinstance(pa, dict):
            action_text = pa.get("text", "")
        current_step = remaining[0]

        prompt = (
            f"玩家计划: {plan.get('goal', '')}\n"
            f"当前步骤: {current_step}\n"
            f"玩家行动: {action_text}\n"
            f"叙事结果: {narrative[:300]}\n\n"
            '判断玩家是否推进了当前步骤。返回JSON: '
            '{"advanced":true/false,"failed":false,"reason":"简短原因"}'
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是计划进度评估器。只返回JSON。",
                max_tokens=150,
                **self._stage_kwargs("knowledge_graph"),
            )
            result = json.loads(raw.strip().strip("```json").strip("```").strip())
        except Exception:
            return

        if result.get("failed"):
            plan["status"] = "failed"
            self._record_narrative_callback(
                f"计划「{plan.get('goal', '')[:20]}」失败: {result.get('reason', '')[:30]}",
                ["plan_failed"], priority="high",
            )
            pacing = self.current_state.setdefault("pacing_state", {})
            pacing["tension"] = min(100, pacing.get("tension", 50) + 15)
        elif result.get("advanced"):
            completed_step = remaining.pop(0)
            plan.setdefault("steps_completed", []).append(completed_step)
            if not remaining:
                plan["status"] = "completed"
                self._record_narrative_callback(
                    f"计划「{plan.get('goal', '')[:20]}」圆满完成！",
                    ["plan_complete"], priority="high",
                )
                pacing = self.current_state.setdefault("pacing_state", {})
                pacing["tension"] = min(100, pacing.get("tension", 50) + 10)

    # ================================================================
    # Feature #5: Retroactive Revelation (Flashbacks)
    # ================================================================

    async def _handle_retroactive_revelation(self, meta_ctx: dict):
        """Generate flashback narrative when hidden lore is discovered."""
        pending = self.current_state.pop("_pending_flashback", None)
        if not pending:
            return
        lore_id = pending.get("lore_id", "")
        lore_content = pending.get("content", "")
        if not lore_content:
            return

        game_time = self.current_state.get("game_time", "")
        player_name = self.current_state.get("player", {}).get("name", "主角")
        prompt = (
            f"当前时间: {game_time}\n"
            f"玩家角色: {player_name}\n"
            f"刚发现的秘密: {lore_content[:300]}\n\n"
            "请用200字以内生成一段闪回叙事（回忆/过去时态），描述这个秘密最初发生时的场景。\n"
            "要求：第三人称过去式，有氛围感，暗示真相但不直说全部。"
        )
        try:
            flashback_text = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是闪回叙事生成器。用沉浸的文学笔触描写过去的片段。",
                max_tokens=400,
                **self._stage_kwargs("narrative"),
            )
        except Exception:
            return
        if not flashback_text or len(flashback_text.strip()) < 20:
            return

        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if node:
            node.setdefault("flashbacks", []).append({
                "lore_id": lore_id,
                "narrative": flashback_text.strip()[:500],
                "turn": self.turn_number,
            })
        self._record_narrative_callback(
            "一段尘封的记忆浮现……",
            ["flashback", lore_id], priority="high",
        )

    # ================================================================
    # Feature #4: Faction Warfare Simulator
    # ================================================================

    async def _handle_faction_warfare(self, meta_ctx: dict):
        """Simulate faction warfare tick: battles, balance shifts, escalation."""
        wars = self.current_state.get("faction_wars", [])
        if not wars:
            orgs = self.script.get("organizations", [])
            for org in orgs:
                for war_def in org.get("wars", []):
                    enemy = war_def.get("enemy", "")
                    if enemy:
                        wars.append({
                            "id": f"war_{org['id']}_{enemy}",
                            "faction_a": org["id"],
                            "faction_b": enemy,
                            "status": war_def.get("initial_status", "cold_war"),
                            "balance": war_def.get("initial_balance", 0),
                            "territories": war_def.get("territories", {}),
                            "turn_started": self.turn_number,
                            "last_battle_turn": 0,
                            "casualties_a": 0,
                            "casualties_b": 0,
                        })
            if wars:
                self.current_state["faction_wars"] = wars
            else:
                return

        _rng = random
        network = self.current_state.setdefault("information_network", [])
        faction_rep = self.current_state.get("faction_reputation", {})

        for war in wars:
            if war.get("status") in ("resolved", "ceasefire"):
                continue
            fa = war["faction_a"]
            fb = war["faction_b"]
            rep_a = faction_rep.get(fa, {}).get("value", 50) if isinstance(faction_rep.get(fa), dict) else 50
            rep_b = faction_rep.get(fb, {}).get("value", 50) if isinstance(faction_rep.get(fb), dict) else 50
            members_a = len(self._org_members.get(fa, set()))
            members_b = len(self._org_members.get(fb, set()))
            power_a = members_a * 10 + rep_a + _rng.randint(-15, 15)
            power_b = members_b * 10 + rep_b + _rng.randint(-15, 15)

            delta = (power_a - power_b) // 5
            delta = max(-20, min(20, delta))
            war["balance"] = max(-100, min(100, war.get("balance", 0) + delta))
            war["last_battle_turn"] = self.turn_number

            if abs(delta) > 5:
                if delta > 0:
                    war["casualties_b"] = war.get("casualties_b", 0) + abs(delta)
                else:
                    war["casualties_a"] = war.get("casualties_a", 0) + abs(delta)

            balance = war["balance"]
            old_status = war["status"]
            if abs(balance) > 80:
                war["status"] = "resolved"
                winner = fa if balance > 0 else fb
                winner_name = self._get_org_name(winner)
                network.append({
                    "id": f"war_end_{fa}_{fb}_{self.turn_number}",
                    "origin_turn": self.turn_number,
                    "fact": f"{winner_name}在战争中取得决定性胜利",
                    "known_by": list(self._org_members.get(fa, set()) | self._org_members.get(fb, set()))[:8],
                    "spread_chance": 0.8,
                    "distortion": 0,
                    "max_spread": 8,
                    "tags": ["faction_war", fa, fb],
                })
                self._record_narrative_callback(
                    f"势力战争结束: {winner_name}获胜",
                    ["faction_war", "resolved"], priority="high",
                )
            elif abs(balance) > 50 and old_status == "cold_war":
                war["status"] = "skirmish"
                self._record_narrative_callback(
                    f"{self._get_org_name(fa)}与{self._get_org_name(fb)}之间爆发小规模冲突",
                    ["faction_war", "escalation"], priority="medium",
                )
            elif abs(balance) > 65 and old_status == "skirmish":
                war["status"] = "open_war"
                self._record_narrative_callback(
                    f"{self._get_org_name(fa)}与{self._get_org_name(fb)}全面开战",
                    ["faction_war", "escalation"], priority="high",
                )
                network.append({
                    "id": f"war_open_{fa}_{fb}_{self.turn_number}",
                    "origin_turn": self.turn_number,
                    "fact": f"{self._get_org_name(fa)}与{self._get_org_name(fb)}全面开战",
                    "known_by": list(self._org_members.get(fa, set()) | self._org_members.get(fb, set()))[:8],
                    "spread_chance": 0.9,
                    "distortion": 0,
                    "max_spread": 10,
                    "tags": ["faction_war", fa, fb],
                })

        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _get_org_name(self, org_id: str) -> str:
        for org in self.script.get("organizations", []):
            if org.get("id") == org_id:
                return org.get("name", org_id)
        return org_id

    def _get_npc_org(self, npc_id: str) -> str:
        """Return the org_id of the organization this NPC belongs to, or ''."""
        for org_id, members in self._org_members.items():
            if npc_id in members:
                return org_id
        return ""
