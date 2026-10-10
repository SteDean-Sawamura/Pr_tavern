"""Prepare mixin – turn preparation and dice/condition evaluation for GameSession."""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import timedelta
from math import ceil

from engine.event_scheduler import parse_time, public_event_notifications
from engine.prompt_builder import PromptBuilder

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)
_DURATION_NUMBER = r'(?:\d+|[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])'


def _duration_value(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = "一二三四五六七八九"
    value = value.replace("两", "二")
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (digits.index(tens) + 1 if tens else 1) * 10 + (digits.index(ones) + 1 if ones else 0)
    return digits.index(value) + 1


class PrepareMixin:
    """Turn preparation: NPC computation, dice rolling, condition evaluation."""

    async def _safe_infer_initial_state(self, narrative: str) -> bool:
        try:
            return await self._infer_initial_state(narrative)
        except Exception as e:
            logging.getLogger(__name__).warning("初始状态推演失败，使用默认值: %s", e)
            return False

    async def _safe_personalize_narrative(self, base_text: str) -> str | None:
        try:
            result = await self._personalize_narrative(base_text)
            min_len = min(80, int(len(base_text) * 0.4))
            if result and len(result) < min_len:
                logging.getLogger(__name__).warning("叙事润色结果过短(%d字，阈值%d)，使用原文", len(result), min_len)
                return None
            return result
        except Exception as e:
            logging.getLogger(__name__).warning("叙事润色失败: %s", e)
            return None

    async def _safe_personalize_choices(self, narrative: str, base_choices: list) -> list | None:
        try:
            return await self._personalize_choices(narrative, base_choices)
        except Exception as e:
            logging.getLogger(__name__).warning("选项润色失败: %s", e)
            return None

    def _ensure_identity_lore(self):
        """从 state 中提取叙事性字段，生成/确认 lorebook 词条（幂等）。"""
        if not self.prompt_builder.lorebook:
            return
        if self.current_state.get("_identity_lore_initialized"):
            return
        existing_ids = {e.id for e in self.prompt_builder.lorebook.entries}
        entries_to_add = []

        # --- 主角档案 ---
        PC_ID = "_pc_identity"
        if PC_ID not in existing_ids:
            pc = self.current_state.get("player", {})
            parts = []
            if pc.get("name"):
                parts.append(f"姓名: {pc['name']}")
            if pc.get("title"):
                parts.append(f"职位: {pc['title']}（NPC应以此称呼主角）")
            if pc.get("bio"):
                parts.append(f"身份: {pc['bio']}")
            if pc.get("personality"):
                parts.append(f"性格: {pc['personality']}")
            if pc.get("long_term_goal"):
                parts.append(f"当前目标: {pc['long_term_goal']}")
            if pc.get("portrait_desc"):
                parts.append(f"外貌: {pc['portrait_desc']}")
            if parts:
                entries_to_add.append({
                    "id": PC_ID, "keys": [pc.get("name", "主角"), "主角"],
                    "content": "【主角档案】\n" + "\n".join(parts),
                    "comment": "主角身份", "entry_type": "pc_identity",
                    "constant": True, "priority": 200, "position": "after_world",
                })

        # --- NPC 档案 ---
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            if npc_id not in self._initial_npc_ids and npc_id not in self.current_state.get("npcs", {}):
                continue
            lore_id = f"_npc_profile_{npc_id}"
            if lore_id in existing_ids:
                continue
            npc_st = self.current_state.get("npcs", {}).get(npc_id, {})
            if not isinstance(npc_st, dict):
                npc_st = {}
            name = npc_st.get("name") or npc.get("name", npc_id)
            bio = npc_st.get("bio") or npc.get("bio", "")
            personality = npc_st.get("personality") or npc.get("personality", "")
            caps = npc_st.get("capabilities") or npc.get("capabilities", "")
            title = npc_st.get("title") or npc.get("title", "")
            if not bio and not personality:
                continue
            parts = [f"【{name}】"]
            if title:
                parts.append(f"职位: {title}")
            if bio:
                parts.append(f"背景: {bio}")
            if personality:
                parts.append(f"性格: {personality}")
            if caps:
                parts.append(f"能力: {caps}")
            entries_to_add.append({
                "id": lore_id, "keys": [name],
                "content": "\n".join(parts),
                "comment": f"NPC:{name}", "entry_type": "npc_profile",
                "priority": 120, "position": "after_world", "scan_depth": 3,
            })

        # --- NPC 关系 ---
        npc_rels = self.current_state.get("npc_relationships_known", {})
        dn = self.current_state.get("display_names", {})
        for key, rel in npc_rels.items():
            lore_id = f"_npc_rel_{key}"
            if lore_id in existing_ids:
                continue
            if not isinstance(rel, dict):
                continue
            if "from" in rel:
                fn = dn.get(rel["from"], rel["from"])
                tn = dn.get(rel["to"], rel["to"])
                desc = rel.get("description", "") if isinstance(rel, dict) else ""
                content = f"{fn}→{tn}: {desc}" if desc else f"{fn}→{tn}"
                keys = [fn, tn]
            else:
                an = dn.get(rel.get("a", ""), rel.get("a", ""))
                bn = dn.get(rel.get("b", ""), rel.get("b", ""))
                desc = rel.get("description", "") if isinstance(rel, dict) else ""
                content = f"{an}↔{bn}: {rel.get('type', '中立')}"
                if desc:
                    content += f"\n{desc}"
                keys = [an, bn]
            entries_to_add.append({
                "id": lore_id, "keys": keys,
                "content": content,
                "comment": "NPC关系", "entry_type": "npc_relationship",
                "priority": 80, "position": "after_world", "scan_depth": 3,
            })

        # --- 地点描述 ---
        for loc_id, loc_desc in self.current_state.get("location_descriptions", {}).items():
            lore_id = f"_loc_desc_{loc_id}"
            if lore_id in existing_ids or not loc_desc:
                continue
            loc_name = dn.get(loc_id, loc_id)
            entries_to_add.append({
                "id": lore_id, "keys": [loc_name, loc_id],
                "content": f"【{loc_name}】\n{loc_desc}",
                "comment": f"地点:{loc_name}", "entry_type": "location_desc",
                "priority": 90, "position": "after_world", "scan_depth": 2,
            })

        # --- 注册并持久化 ---
        if entries_to_add:
            self.prompt_builder.lorebook.add_entries(entries_to_add)
            dynamic = self.current_state.setdefault("dynamic_lorebook", [])
            dynamic.extend(entries_to_add)
            if self.vector_memory:
                batch = [
                    (e["id"], e["content"], {"entry_type": e.get("entry_type", "")})
                    for e in entries_to_add if len(e.get("content", "")) >= 20
                ]
                if batch:
                    self._schedule_index_task(
                        self._async_lorebook_vector_sync(batch)
                    )
        self.current_state["_identity_lore_initialized"] = True

    def _compute_present_npcs(self, state: dict) -> tuple[list[str], list[str]]:
        """计算给定 state 下的在场/附近 NPC 列表（room 感知）。"""
        self._ensure_rooms_initialized(state)
        player_loc = state.get("player", {}).get("location", "")
        player_room = state.get("player", {}).get("current_room", "")
        present: list[str] = []
        nearby: list[str] = []
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            npc_location = self._get_npc_location(npc_id, state)
            if not (npc_location and self._locations_match(player_loc, npc_location)):
                continue
            if player_room:
                npc_st = state.get("npcs", {}).get(npc_id, {})
                npc_room = npc_st.get("current_room", "") if isinstance(npc_st, dict) else ""
                if npc_room and npc_room != player_room:
                    nearby.append(npc_id)
                    continue
            present.append(npc_id)
        for npc_id, info in state.get("npcs", {}).items():
            if npc_id not in self._npc_by_id and npc_id not in present and npc_id not in nearby:
                if not isinstance(info, dict):
                    continue
                cur_loc = info.get("current_location", info.get("default_location", ""))
                if cur_loc and self._locations_match(player_loc, cur_loc):
                    if player_room:
                        npc_room = info.get("current_room", "")
                        if npc_room and npc_room != player_room:
                            nearby.append(npc_id)
                            continue
                    present.append(npc_id)
        for cid in state.get("companions", []):
            if cid not in present:
                present.append(cid)
        return present, nearby

    def _validate_choice(self, choice_id: str | None) -> dict:
        active_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not active_node:
            raise ValueError(f"选项不存在于当前展示列表: {choice_id}")
        matched = next((choice for choice in active_node.get("choices_presented", [])
                        if choice.get("id") == choice_id), None)
        if not matched:
            raise ValueError(f"选项不存在于当前展示列表: {choice_id}")
        if matched.get("locked"):
            raise ValueError(f"该选项已锁定: {matched.get('text', choice_id)}（{matched.get('lock_reason', '条件不满足')}）")
        return matched

    def _prepare_turn(self, player_action: dict, *, legacy_prompt: bool = False) -> dict:
        """Prepare all pre-AI data for a turn (dice, events, state changes, prompts).

        Returns a context dict used by process_action / process_action_stream.
        The returned dict includes 'rollback_state' — the state snapshot before
        any modifications, to be used by the caller for rollback protection.

        legacy_prompt: if True, builds the old single-shot narrative system_prompt
        and messages (used by regenerate stream fallback). New 8-stage pipeline
        skips this to save CPU.
        """
        self._mark_activity()
        self._ensure_identity_lore()
        old_time = self.current_state.get("game_time", "")

        # Flow#2: 验证选项 — 检查存在性和锁定状态
        if player_action.get("type") == "choice":
            matched = self._validate_choice(player_action.get("choice_id"))
            player_action["text"] = matched.get("text") or matched["id"]
            self.player_model.record_choice(matched)

        # B1: turn_number 在校验通过后才推进，校验失败时不消耗回合数
        self.turn_number += 1
        self._pending_vector_milestones = []

        opening_result = self._check_opening_choice(player_action)
        opening_description = opening_result.get("description", "") if opening_result else ""

        # 时间预估：仅用于事件检查窗口，最终时间由 Stage 4b AI 决定
        action_type = player_action.get("type", "freeform")
        action_text = player_action.get("text", "")
        requested_wait_minutes = None
        if action_type == "system":
            estimated_minutes = 0
        elif action_type == "choice":
            estimated_minutes = self._get_choice_time_hint(player_action) or 15
        else:
            estimated_minutes = 30
            _at = action_text
            # 尝试从文本中提取明确的时间量（如"等待1小时"、"等30分钟"）
            _explicit_time = re.search(rf'({_DURATION_NUMBER})\s*个?\s*(小时|hour|h)(半)?', _at)
            _explicit_min = re.search(rf'({_DURATION_NUMBER})\s*(分钟|minute|min|m)', _at)
            _half_hour = re.search(rf'(?:({_DURATION_NUMBER})\s*个?\s*半|半\s*个?)\s*小时', _at)
            _clock_target = re.search(
                r'(?:等到|(?:等待|等候|守候|静候)(?:到|至)?)\s*'
                r'(?:(?P<next_day>明天|次日|翌日)\s*)?(?P<hour>\d{1,2})\s*'
                r'(?:(?:时|点)\s*(?:(?P<minute>\d{1,2})\s*分?|(?P<half>半))?'
                r'|[:：]\s*(?P<colon_minute>\d{2}))', _at,
            )
            _clock_minutes = None
            if _clock_target:
                current_time = parse_time(old_time)
                next_day = bool(_clock_target.group("next_day"))
                hour = int(_clock_target.group("hour"))
                minute = (30 if _clock_target.group("half") else
                          int(_clock_target.group("minute") or _clock_target.group("colon_minute") or 0))
                if current_time and hour < 24 and minute < 60:
                    target_time = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if next_day:
                        target_time += timedelta(days=1)
                    if target_time > current_time:
                        _clock_minutes = ceil((target_time - current_time).total_seconds() / 60)
            if _explicit_time:
                estimated_minutes = _duration_value(_explicit_time.group(1)) * 60
                if _explicit_time.group(3):
                    estimated_minutes += 30
                if _explicit_min:
                    estimated_minutes += _duration_value(_explicit_min.group(1))
            elif _half_hour:
                estimated_minutes = (_duration_value(_half_hour.group(1)) if _half_hour.group(1) else 0) * 60 + 30
            elif _explicit_min:
                estimated_minutes = _duration_value(_explicit_min.group(1))
            elif _clock_minutes:
                estimated_minutes = _clock_minutes
            elif any(w in _at for w in ("睡", "休息", "过夜", "扎营", "入睡")):
                estimated_minutes = 480
            elif any(w in _at for w in ("等待", "等候", "守候", "静候", "等到")):
                estimated_minutes = 60
            elif any(w in _at for w in ("前往", "出发", "赶路", "旅行", "骑马", "驾车", "乘")):
                estimated_minutes = 180
            elif any(w in _at for w in ("观察", "查看", "翻阅", "检查", "环顾", "阅读", "端详")):
                estimated_minutes = 15
            elif any(w in _at for w in ("说", "问", "回答", "告诉", "聊", "谈", "交谈", "搭话")):
                estimated_minutes = 10
            if (_clock_minutes or re.search(
                rf'(?:等待|等候|守候|静候|等)\s*(?:{_DURATION_NUMBER}|半)\s*个?\s*'
                r'(?:半\s*)?(?:小时|hour|h|分钟|minute|min|m)', _at,
            )) and estimated_minutes > 0:
                requested_wait_minutes = estimated_minutes

        estimated_advance = timedelta(minutes=estimated_minutes)
        new_time = old_time if estimated_minutes == 0 else self._advance_game_time(old_time, estimated_advance)
        # P-1: 时间解析失败时 new_time == old_time，警告并继续
        if new_time == old_time and old_time and estimated_minutes:
            logging.getLogger(__name__).warning(
                "游戏时间未推进（可能格式错误），old_time=%r", old_time
            )

        triggered_events = self.event_scheduler.check_events(
            self.current_state, old_time, new_time,
            condition_eval=self._evaluate_condition,
        )
        conditional_event_candidates = []
        conditioned_ids = {
            event["id"] for section in ("one_time_events", "cyclic_events")
            for event in self.script.get(section, []) if event.get("condition")
        }
        if conditioned_ids:
            initial_events = {(event["event_id"], event["fire_time"]) for event in triggered_events}
            conditional_event_candidates = [
                event for event in self.event_scheduler.check_events(self.current_state, old_time, new_time)
                if event["event_id"] in conditioned_ids
                and (event["event_id"], event["fire_time"]) not in initial_events
            ]
        # 先捕获 rollback_state（event_engine 会就地修改 state）
        try:
            rollback_state = json.loads(json.dumps(self.current_state, ensure_ascii=False))
        except (TypeError, ValueError):
            rollback_state = copy.deepcopy(self.current_state)
        # Unified event engine tick (parallel with legacy systems during migration)
        event_engine_result = self.event_engine.tick(
            self.current_state, self.turn_number,
            game_time=old_time, old_time=old_time,
            condition_eval=self._evaluate_condition,
            player_action=player_action.get("text", ""),
            state_manager=self.state_manager,
            advance_timed=action_type != "system",
        )
        self._apply_event_result(event_engine_result)

        # NPC 自主行为 tick
        npc_autonomy_events = []
        candidate_npc_autonomy_events = []
        if hasattr(self, 'npc_autonomy'):
            npc_autonomy_events = self.npc_autonomy.tick(old_time, self.current_state)
            if new_time != old_time:
                candidate_npc_autonomy_events = copy.deepcopy(self.npc_autonomy).tick(
                    new_time, self.current_state,
                )

        # Roll dice
        dice_results = self._roll_always_active_dice()
        self._apply_weather(dice_results)
        candidate_event_ids = {event["event_id"] for event in triggered_events}
        candidate_event_items = {
            item["id"]: item["linked_event_id"]
            for item in self.script.get("random_items", [])
            if item.get("trigger_type") == "event_linked"
            and item.get("linked_event_id") in candidate_event_ids
            and self.current_state.get("dice_check_enabled", True)
        }
        candidate_event_dice = []
        candidate_event_dice_state = {}
        if candidate_event_items:
            random_item_state = self.current_state.setdefault("random_item_state", {})
            self.current_state["random_item_state"] = copy.deepcopy(random_item_state)
            candidate_event_dice = self._roll_event_linked_dice(triggered_events)
            candidate_event_dice_state = {
                item_id: copy.deepcopy(self.current_state["random_item_state"].get(item_id))
                for item_id in candidate_event_items
            }
            self.current_state["random_item_state"] = random_item_state
        dice_results.extend(candidate_event_dice)
        candidate_conditional_items = {
            item["id"]: item for item in self.script.get("random_items", [])
            if item.get("trigger_type") == "conditional"
            and self._condition_has_destination_change(item.get("trigger_condition"))
            and self.current_state.get("dice_check_enabled", True)
            and self._random_item_condition_matches(item.get("trigger_condition"), player_action.get("text", ""), old_time)
            and not self._random_item_condition_matches(
                item.get("trigger_condition"), player_action.get("text", ""), old_time,
                initial_location=self.current_state.get("player", {}).get("location"),
            )
        }
        candidate_conditional_dice = []
        candidate_conditional_state = {}
        if candidate_conditional_items:
            random_item_state = self.current_state.setdefault("random_item_state", {})
            self.current_state["random_item_state"] = copy.deepcopy(random_item_state)
            for item in candidate_conditional_items.values():
                result = self._roll_or_sustain(item)
                if result is not None:
                    candidate_conditional_dice.append(result)
            candidate_conditional_state = {
                item_id: copy.deepcopy(self.current_state["random_item_state"].get(item_id))
                for item_id in candidate_conditional_items
            }
            self.current_state["random_item_state"] = random_item_state
        dice_results.extend(candidate_conditional_dice)
        dice_results.extend(self._roll_conditional_dice(
            player_action.get("text", ""), old_time, skip_item_ids=candidate_conditional_items,
        ))

        # Apply opening choice + dice state changes
        all_state_changes = []
        if opening_result:
            opening_changes = opening_result.get("state_changes", [])
            if opening_changes:
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, opening_changes, inplace=True
                )
                all_state_changes.extend(log)
            # Persist opening choice result in state for later turns
            if opening_description:
                prev = self.current_state.get("opening_context", "")
                self.current_state["opening_context"] = (
                    prev + "\n\n[玩家开局选择] " + player_action.get("text", "") +
                    " → " + opening_description
                ).strip()[:800]

        dice_enabled = self.current_state.get("dice_check_enabled", True)
        for dr in dice_results:
            if dr.random_item_id in candidate_event_items or dr.random_item_id in candidate_conditional_items:
                continue
            if dr.range_state_changes:
                if not dice_enabled and getattr(dr, 'random_item_id', '') != 'weather':
                    continue
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, dr.range_state_changes, inplace=True
                )
                all_state_changes.extend(log)

        # Consequences from event engine (already processed in tick above)
        action_text_raw = player_action.get("text", "")
        triggered_consequences = event_engine_result.triggered_consequences
        check_result = None
        achieved_milestones, milestone_reward_changes = self._check_milestones()
        all_state_changes.extend(milestone_reward_changes)
        milestone_progress = self._check_milestone_progress()

        # Fire milestone events to story tree and event engine before evaluation
        if achieved_milestones:
            for ms in achieved_milestones:
                ms_id = ms.get("id", "")
                if ms_id:
                    self._fire_event_dual(f"milestone.{ms_id}")

        # Event def effects deferred to _apply_parsed_response (after AI finalizes time)

        # Choice → quest immediate feedback: match choice text against active quest completion_keywords
        if player_action.get("text"):
            self._check_quest_keywords(player_action["text"])

        # Story tree evaluation
        story_tree_result = None
        if self.story_tree_engine:
            story_tree_result = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
                advance_timed=action_type != "system",
            )
            self._apply_story_tree_result(story_tree_result)

        self._run_turn_computations(story_tree_result, check_result, achieved_milestones)

        # Build AI prompts
        if not dice_enabled:
            dice_results = [dr for dr in dice_results if getattr(dr, 'random_item_id', '') == 'weather']
        dice_dicts = []
        candidate_fire_times = {event["event_id"]: event.get("fire_time", "") for event in triggered_events}
        for dice_result in dice_results:
            dice_entry = self._dice_result_to_dict(dice_result)
            linked_event_id = candidate_event_items.get(dice_result.random_item_id)
            if linked_event_id:
                dice_entry["candidate_event_id"] = linked_event_id
                dice_entry["candidate_fire_time"] = candidate_fire_times.get(linked_event_id, "")
                staged_state = candidate_event_dice_state.get(dice_result.random_item_id) or {}
                dice_entry["remaining_turns"] = staged_state.get("remaining_turns", 0)
                if dice_entry["sustained"]:
                    dice_entry["duration_info"] = f"(持续中，剩余{dice_entry['remaining_turns']}回合)"
            if dice_result.random_item_id in candidate_conditional_items:
                dice_entry["candidate_destination"] = True
                staged_state = candidate_conditional_state.get(dice_result.random_item_id) or {}
                dice_entry["remaining_turns"] = staged_state.get("remaining_turns", 0)
                if dice_entry["sustained"]:
                    dice_entry["duration_info"] = f"(持续中，剩余{dice_entry['remaining_turns']}回合)"
            dice_dicts.append(dice_entry)
        recent = self.world_tree.get_recent_history(6)
        recent_messages = [n.get("ai_response", "") for n in recent]
        action_text = player_action.get("text", "")
        action_text = self.regex_engine.apply(action_text, "user_input")
        activated_lore, new_lore_ts = self.prompt_builder.scan_lorebook(
            action_text, recent_messages,
            timed_state=self.current_state.get("lorebook_timed_state"),
            turn_number=self.turn_number,
            state=self.current_state,
        )
        self.current_state["lorebook_timed_state"] = new_lore_ts
        prior_context_lore_ids = {
            entry.id for entry in activated_lore
            if not entry.constant and not self.prompt_builder.lorebook.primary_key_match_count(
                entry.id, action_text,
            )
        }

        # 地点/时间感知激活（不依赖关键词匹配）
        _loc_id = self.current_state.get("player", {}).get("location", "")
        _gt = self.current_state.get("game_time", "")
        _activated_ids = {entry.id for entry in activated_lore}
        _excluded_ids = self.prompt_builder._excluded_lore_ids(self.current_state)
        context_lore = self.prompt_builder.lorebook.activate_by_context(
            location_id=_loc_id, game_time=_gt,
            excluded_entry_ids=_excluded_ids,
        )
        if _loc_id:
            _scene_ids = {f"_kg_loc_{_loc_id}", f"_loc_desc_{_loc_id}"}
            context_lore.extend(entry for entry in self.prompt_builder.lorebook.entries
                                if entry.id in _scene_ids and entry.enabled
                                and entry.id not in _excluded_ids)
        _context_entries = {entry.id: entry for entry in context_lore}
        activated_lore.extend(entry for entry_id, entry in _context_entries.items()
                              if entry_id not in _activated_ids)
        prior_context_lore_ids.difference_update(_context_entries)

        # 触发事件关联 lorebook 条目激活
        event_lore_candidate_ids = []
        lore_events = triggered_events + conditional_event_candidates
        if lore_events and self.prompt_builder.lorebook:
            _already = {e.id for e in activated_lore}
            _evt_texts = [ev.get("description", "") for ev in lore_events if ev.get("description")]
            if _evt_texts:
                _evt_text = " ".join(_evt_texts)
                _evt_lore, _ = self.prompt_builder.scan_lorebook(
                    _evt_text, [], state=self.current_state,
                )
                for _el in _evt_lore:
                    if _el.id not in _already:
                        if not _el.constant and not self.prompt_builder.lorebook.primary_key_match_count(
                            _el.id, action_text,
                        ):
                            prior_context_lore_ids.add(_el.id)
                            if (not _el.id.startswith("_") and
                                    self.prompt_builder.lorebook.primary_key_match_count(
                                        _el.id, _evt_text,
                                    )):
                                event_lore_candidate_ids.append(_el.id)
                        boosted_entry = copy.copy(_el)
                        boosted_entry.priority = min(boosted_entry.priority + 20, 200)
                        activated_lore.append(boosted_entry)
                        _already.add(_el.id)

        # 剧情树关联 lorebook 条目优先级提升
        _story_lore_ids: set = set()
        if self.story_tree_engine and activated_lore:
            _story_keywords = set()
            sts = self.current_state.get("story_tree_state", {})
            for _sn_id in list(sts.get("active", [])):
                _sn = self.story_tree_engine._nodes.get(_sn_id)
                if _sn:
                    for _rid in _sn.get("related_npcs", []):
                        _story_keywords.add(_rid)
                        _rname = self._get_npc_or_org_name(_rid)
                        if _rname:
                            _story_keywords.add(_rname.lower())
                    for _oid in _sn.get("related_orgs", []):
                        _story_keywords.add(_oid)
                        _oname = self._get_npc_or_org_name(_oid)
                        if _oname:
                            _story_keywords.add(_oname.lower())
            if _story_keywords:
                _story_lore_ids = set()
                for index, _le in enumerate(activated_lore):
                    if any(k.lower() in _story_keywords for k in _le.keys if k):
                        boosted_entry = copy.copy(_le)
                        boosted_entry.priority = min(boosted_entry.priority + 30, 200)
                        activated_lore[index] = boosted_entry
                        _story_lore_ids.add(_le.id)
                activated_lore.sort(key=lambda e: e.priority, reverse=True)
        prior_context_lore_ids.difference_update(_story_lore_ids)

        # 仅工作流和 legacy 提示需要旧版历史上下文
        history_summary = self.current_state.get("history_summary", "")
        _is_agentic = self._agentic_enabled() or self._agentic_unified_enabled()
        if _is_agentic:
            # agentic 模式不使用 compose context，跳过构建
            history_context = ""
            context_memory = ""
        else:
            history_context = self.prompt_builder.build_history_context(recent, history_summary)
            context_memory = self.prompt_builder.build_context_memory(recent, self.current_state)

            # 宏展开: 对历史上下文和上下文记忆中的变量引用做替换
            history_context = self.script_variables.expand_macros(history_context, self.current_state)
            context_memory = self.script_variables.expand_macros(context_memory, self.current_state)

        # Reasoning 回注: 收集前几轮的剧情决策思路（可配置 lookback）
        if _is_agentic:
            recent_reasoning = []
            prev_plot_decision = ""
        else:
            _reasoning_lookback = self.script.get("settings", {}).get("reasoning_lookback", 2)
            recent_reasoning = []
            prev_plot_decision = ""
            for node in recent[-_reasoning_lookback:]:
                r = node.get("plot_reasoning", "")
                if r:
                    recent_reasoning.append({
                        "turn": node.get("turn_number", 0),
                        "reasoning": r[:300],
                    })
            if recent:
                prev_plot_decision = recent[-1].get("plot_decision", "")

        # 确保 NPC 房间信息已初始化
        self._ensure_rooms_initialized()

        # 计算在场NPC（room 感知：同房间=在场，同建筑不同房间=nearby）
        present_npc_ids, nearby_npc_ids = self._compute_present_npcs(self.current_state)

        # 叙事专用 system prompt（仅 legacy 模式构建）
        system_prompt = None
        messages = None
        _macro_exp = lambda t: self.script_variables.expand_macros(t, self.current_state)
        _event_data = self.event_engine.get_events_for_prompt(self.current_state)
        _event_sections = PromptBuilder._format_events_for_prompt(_event_data)
        if legacy_prompt:
            system_prompt = self.prompt_builder.build_narrative_system_prompt(
                self.current_state,
                activated_lore=activated_lore,
                authors_note=self.authors_note,
                turn_number=self.turn_number,
                authors_note_position=self.authors_note_position,
                macro_expander=_macro_exp,
                negative_prompt=getattr(self, "negative_prompt", ""),
                event_sections=_event_sections,
                pc_discovered_lore=self.current_state.get("pc_discovered_lore", []),
            )
            if history_context:
                system_prompt += "\n\n---\n\n" + _macro_exp(history_context)

            # D2: 注入近期上下文要点，帮助AI保持NPC/道具/地点一致性
            if context_memory:
                system_prompt += "\n\n" + context_memory

        user_message = self.prompt_builder.build_user_message(
            player_action, old_time, triggered_events, dice_dicts,
            check_result=check_result,
            triggered_consequences=triggered_consequences,
            achieved_milestones=achieved_milestones,
            opening_description=opening_description,
            state=self.current_state,
            event_sections=_event_sections,
            estimated_end_time=new_time,
        )

        if legacy_prompt:
            history_messages = self.prompt_builder.build_history_messages(
                recent, self.current_state.get("history_summary", ""),
                chapter_summaries=self.current_state.get("adventure_log", {}).get("chapter_summaries"),
            )
            messages = history_messages + [{"role": "user", "content": user_message}]
            # Author's Note at_depth injection
            if self.authors_note and self.authors_note_position == "at_depth":
                an_msg = {"role": "system", "content": f"[创作指令] {self.authors_note}"}
                idx = max(0, len(messages) - self.authors_note_depth)
                messages.insert(idx, an_msg)

        # Fire before_generation triggers (via unified story tree + event engine)
        bg_inject, bg_notifications = self._fire_lifecycle_event("before_generation")
        turn_instructions = list(bg_inject)
        if bg_inject:
            extra = "\n".join(bg_inject)
            if system_prompt:
                system_prompt += "\n\n" + extra
            history_context = (history_context + "\n\n## 触发器注入\n" + extra).strip()

        # Inject story tree prompts into system prompt
        all_notifications = list(bg_notifications)
        if story_tree_result:
            if story_tree_result.inject_prompts:
                turn_instructions.extend(story_tree_result.inject_prompts)
                st_extra = "\n".join(story_tree_result.inject_prompts)
                if system_prompt:
                    system_prompt += "\n\n" + st_extra
                history_context = (history_context + "\n\n## 剧情推进\n" + st_extra).strip()
            all_notifications.extend(public_event_notifications(self.script, story_tree_result))

        # Inject event engine prompts into context
        if event_engine_result.inject_prompts:
            turn_instructions.extend(event_engine_result.inject_prompts)
            ee_extra = "\n".join(event_engine_result.inject_prompts)
            if system_prompt:
                system_prompt += "\n\n" + ee_extra
            history_context = (history_context + "\n\n## 事件系统注入\n" + ee_extra).strip()
        all_notifications.extend(public_event_notifications(self.script, event_engine_result))

        # 收集活跃剧情线的阶段指令
        stage_directives: dict[str, list[str]] = {}
        # From event engine active events
        _es = self.current_state.get("events", {})
        for eid, evs in _es.items():
            if evs.get("status") != "active":
                continue
            ev = self.event_engine.events.get(eid)
            if ev and hasattr(ev, "metadata") and ev.metadata.get("stage_directives"):
                for stage, directive in ev.metadata["stage_directives"].items():
                    stage_directives.setdefault(stage, []).append(directive)
        # Also from legacy story tree
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            for nid in sts.get("active", []):
                node = self.story_tree_engine._nodes.get(nid)
                if node and node.get("stage_directives"):
                    for stage, directive in node["stage_directives"].items():
                        stage_directives.setdefault(stage, []).append(directive)

        return {
            "old_time": old_time,
            "new_time": new_time,
            "estimated_minutes": estimated_minutes,
            "requested_wait_minutes": requested_wait_minutes,
            "is_system_opening": action_type == "system",
            "triggered_events": triggered_events,
            "conditional_event_candidates": conditional_event_candidates,
            "initial_event_count": len(triggered_events),
            "expired": [],
            "all_state_changes": all_state_changes,
            "triggered_consequences": triggered_consequences,
            "check_result": check_result,
            "achieved_milestones": achieved_milestones,
            "milestone_progress": milestone_progress,
            "dice_dicts": dice_dicts,
            "candidate_event_dice_results": {result.random_item_id: result for result in candidate_event_dice},
            "candidate_event_dice_state": candidate_event_dice_state,
            "candidate_conditional_items": candidate_conditional_items,
            "candidate_conditional_dice_results": {result.random_item_id: result for result in candidate_conditional_dice},
            "candidate_conditional_dice_state": candidate_conditional_state,
            "activated_lore": activated_lore,
            "lore_prior_context_ids": prior_context_lore_ids,
            "event_lore_candidate_ids": event_lore_candidate_ids,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "messages": messages,
            "rollback_state": rollback_state,
            "present_npc_ids": present_npc_ids,
            "nearby_npc_ids": nearby_npc_ids,
            "history_context": history_context,
            "base_history_context": history_context,
            "context_memory": context_memory,
            "recent_nodes": recent,
            "recent_reasoning": recent_reasoning,
            "prev_plot_decision": prev_plot_decision,
            "trigger_notifications": all_notifications,
            "story_tree_updates": story_tree_result,
            "stage_directives": stage_directives,
            "turn_instructions": list(dict.fromkeys(turn_instructions)),
            "story_lore_ids": _story_lore_ids,
            "event_sections": _event_sections,
            "action_text": action_text,
            "opening_description": opening_description,
            "npc_autonomy_events": npc_autonomy_events,
            "candidate_npc_autonomy_events": candidate_npc_autonomy_events,
        }

    def _roll_always_active_dice(self) -> list:
        results = []
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") == "always":
                result = self._roll_or_sustain(item)
                if result is not None:
                    results.append(result)
        return results

    def _roll_event_linked_dice(self, triggered_events: list[dict]) -> list:
        if not self.current_state.get("dice_check_enabled", True):
            return []
        results = []
        triggered_ids = {e["event_id"] for e in triggered_events}
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") == "event_linked":
                linked_id = item.get("linked_event_id", "")
                if linked_id in triggered_ids:
                    result = self._roll_or_sustain(item)
                    if result is not None:
                        results.append(result)
        return results

    def _roll_conditional_dice(self, action_text: str = "", game_time: str = "", *, skip_item_ids=()) -> list:
        """Roll dice items with trigger_type='conditional' when their condition is met."""
        if not self.current_state.get("dice_check_enabled", True):
            return []
        results = []
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") != "conditional":
                continue
            if item["id"] in skip_item_ids:
                continue
            if self._random_item_condition_matches(item.get("trigger_condition"), action_text, game_time):
                result = self._roll_or_sustain(item)
                if result is not None:
                    results.append(result)
        return results

    def _condition_has_destination_change(self, condition) -> bool:
        if not isinstance(condition, dict):
            return False
        return bool(condition.get("destination_changed")) or any(
            self._condition_has_destination_change(part)
            for key in ("any", "all") for part in condition.get(key, [])
        )

    def _random_item_condition_matches(
        self, condition, action_text: str, game_time: str, *,
        initial_location: str | None = None, settled_location: str | None = None,
    ) -> bool:
        if isinstance(condition, str):
            return bool(re.search(r'==|!=|<=|>=|<|>', condition)) and self._evaluate_condition(condition)
        if not isinstance(condition, dict):
            return False
        if "all" in condition:
            return bool(condition["all"]) and all(self._random_item_condition_matches(
                part, action_text, game_time,
                initial_location=initial_location, settled_location=settled_location,
            ) for part in condition["all"])
        if "any" in condition:
            return any(self._random_item_condition_matches(
                part, action_text, game_time,
                initial_location=initial_location, settled_location=settled_location,
            )
                       for part in condition["any"])
        if not any(key in condition for key in ("location_ids", "action_keywords", "night", "destination_changed")):
            return False
        player_location = initial_location or self.current_state.get("player", {}).get("location")
        if "location_ids" in condition and player_location not in condition["location_ids"]:
            return False
        if "action_keywords" in condition and not any(
            keyword.lower() in action_text.lower() for keyword in condition["action_keywords"]
        ):
            return False
        if condition.get("night"):
            parsed_time = parse_time(game_time)
            if not parsed_time or 6 <= parsed_time.hour < 22:
                return False
        if condition.get("destination_changed") and not any(
            location["id"] != player_location
            and (location["id"] in action_text or location["name"] in action_text)
            and (initial_location is None or location["id"] == settled_location)
            for location in self.script.get("locations", [])
        ):
            return False
        return True

    def _evaluate_tone(self, story_tree_result=None):
        """Evaluate tone rules and auto-derived tone signals, store in state."""
        tone_parts = []

        tone_rules = self.script.get("tone_rules", [])
        if tone_rules:
            best = None
            for rule in tone_rules:
                cond = rule.get("condition", "")
                if cond and not self._evaluate_condition(cond):
                    continue
                prio = rule.get("priority", 0)
                if best is None or prio > best.get("priority", 0):
                    best = rule
            if best:
                tone_parts.append(f"基调「{best.get('name', '')}」: {best.get('tone', '')}")
                if best.get("narrative_style"):
                    tone_parts.append(f"叙事风格: {best['narrative_style']}")

        from engine.story_tree import StoryTreeResult
        if isinstance(story_tree_result, StoryTreeResult):
            for node in story_tree_result.newly_active:
                if node.get("type") == "quest":
                    tone_parts.append("当前有进行中的任务——叙事应保持紧迫感")
                    break

        if self.event_engine:
            _ev_data = self.event_engine.get_events_for_prompt(self.current_state)
            if _ev_data.get("consequences"):
                for c in _ev_data["consequences"]:
                    if c.get("importance") == "high":
                        tone_parts.append("暗流涌动——重要后果即将触发")
                        break
        else:
            consequences = self.current_state.get("pending_consequences", [])
            for c in consequences:
                if c.get("importance") == "high":
                    tone_parts.append("暗流涌动——重要后果即将触发")
                    break

        threshold_events = self.current_state.get("_last_threshold_events", [])
        if threshold_events:
            tone_parts.append("情感波动——NPC 关系刚发生重大变化")

        weather = self.current_state.get("current_weather", "")
        if weather and any(w in weather for w in ("暴", "雷", "冰雹", "飓风")):
            tone_parts.append("恶劣天气——环境描写应体现压迫感")

        self.current_state["active_tone"] = tone_parts if tone_parts else []

    def _evaluate_condition(self, condition: str) -> bool:
        """Evaluate a simple condition string against current state.

        Supports formats like:
          'player.health < 30'
          'player.location == tavern'
          'current_weather == 阴雨'
          'player.health < 30 AND current_weather == 暴雨'
          'player.gold > 100 OR player.reputation > 50'
        """
        stripped = condition.strip()
        for operator, aggregate in ((r'\bOR\b|\|\|', any), (r'\bAND\b|&&', all)):
            parts = []
            start = 0
            pattern = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|''' + operator
            for match in re.finditer(pattern, stripped):
                if match.group().startswith(('"', "'")):
                    continue
                parts.append(stripped[start:match.start()].strip())
                start = match.end()
            if parts:
                parts.append(stripped[start:].strip())
                return aggregate(self._evaluate_condition(part) for part in parts)
        return self._evaluate_single_condition(stripped)

    def _evaluate_single_condition(self, condition: str) -> bool:
        """Evaluate a single condition expression (no AND/OR)."""
        if re.fullmatch(r'event\.\w+\.fired', condition.strip()):
            condition = f'{condition.strip()} == true'
        m = re.match(r'([\w.]+)\s*(==|!=|<=|>=|<|>)\s*(.+)', condition.strip())
        if not m:
            return True  # No parseable condition — always trigger
        path, op, raw_val = m.group(1), m.group(2), m.group(3).strip().strip('"').strip("'")

        # node_completed.{node_id} == true
        if path.startswith("node_completed."):
            node_id = path[len("node_completed."):]
            sts = self.current_state.get("story_tree_state", {})
            is_done = node_id in sts.get("completed", [])
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_done == target
            elif op == "!=":
                return is_done != target
            return False

        # milestone.{milestone_id} == true
        if path.startswith("milestone."):
            ms_id = path[len("milestone."):]
            is_achieved = ms_id in self.current_state.get("achieved_milestones", [])
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_achieved == target
            elif op == "!=":
                return is_achieved != target
            return False

        if path.startswith("event_fired.") or (path.startswith("event.") and path.endswith(".fired")):
            event_id = path[len("event_fired."):] if path.startswith("event_fired.") else path[len("event."):-len(".fired")]
            fired_ot = self.current_state.get("fired_one_time_events", [])
            trackers = self.current_state.get("cyclic_event_trackers", {})
            is_fired = event_id in fired_ot or bool(trackers.get(event_id, {}).get("last_fired"))
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_fired == target
            elif op == "!=":
                return is_fired != target
            return False

        if path.startswith("status."):
            state_id = path[len("status."):]
            actual = "active" if state_id in self.current_state.get("active_persistent_states", []) else "inactive"
        else:
            actual = self.state_manager._get_value(self.current_state, path)
        if actual is None:
            return False

        # Try numeric comparison
        try:
            num_actual = float(actual) if not isinstance(actual, (int, float)) else actual
            num_val = float(raw_val)
            ops = {'==': num_actual == num_val, '!=': num_actual != num_val,
                   '<': num_actual < num_val, '>': num_actual > num_val,
                   '<=': num_actual <= num_val, '>=': num_actual >= num_val}
            return ops.get(op, False)
        except (ValueError, TypeError):
            pass

        # String comparison
        str_actual = str(actual)
        if op == '==':
            return str_actual == raw_val
        elif op == '!=':
            return str_actual != raw_val
        elif op == '>=':
            return str_actual >= raw_val
        elif op == '<=':
            return str_actual <= raw_val
        elif op == '>':
            return str_actual > raw_val
        elif op == '<':
            return str_actual < raw_val
        return False

    @staticmethod
    def _advance_game_time(time_str: str, delta: timedelta) -> str:
        t = parse_time(time_str)
        if not t:
            # P0-7: 升级为 ERROR — 时间无法推进意味着事件/昼夜系统失效，需要告警
            logging.getLogger(__name__).error(
                "无法解析游戏时间 %r，时间未推进（事件调度可能失效）", time_str
            )
            return time_str
        return (t + delta).isoformat()

    @staticmethod
    def _apply_iso_duration(time_str: str, duration: str) -> str:
        """Parse a simple ISO 8601 duration and apply it.

        P0-7: 也兼容 AI 常见的非标准格式（"30 minutes" / "2 hours" / "1 day"），
        而不是静默不推进。
        """
        t = parse_time(time_str)
        if not t:
            logging.getLogger(__name__).error(
                "无法解析游戏时间 %r 用于 duration %r，时间未推进", time_str, duration
            )
            return time_str

        if not isinstance(duration, str):
            return time_str

        dur = duration.strip()

        # Simple parser for PT30M, PT2H, P1D style durations
        m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$', dur)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            minutes = int(m.group(3) or 0)
            return (t + timedelta(days=days, hours=hours, minutes=minutes)).isoformat()

        # Fallback: 兼容自然语言（"30 minutes" / "2小时" / "1 day"）
        nat = re.match(
            r'(\d+)\s*(minute|min|m|hour|hr|h|day|d|分钟|分|小时|时|天|日)s?\b',
            dur, re.IGNORECASE,
        )
        if nat:
            n = int(nat.group(1))
            unit = nat.group(2).lower()
            if unit in ("minute", "min", "m", "分钟", "分"):
                return (t + timedelta(minutes=n)).isoformat()
            if unit in ("hour", "hr", "h", "小时", "时"):
                return (t + timedelta(hours=n)).isoformat()
            if unit in ("day", "d", "天", "日"):
                return (t + timedelta(days=n)).isoformat()

        logging.getLogger(__name__).warning(
            "无法识别的 duration %r，时间未推进", duration
        )
        return time_str
