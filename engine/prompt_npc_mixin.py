"""PromptBuilder Mixin: NPC相关提示词构建"""
from __future__ import annotations
from typing import TYPE_CHECKING

from engine.event_scheduler import parse_time
from engine.lorebook import Lorebook, LorebookEntry
from engine.prompt_loader import PromptLoader

if TYPE_CHECKING:
    pass


def _xml(tag: str, content: str) -> str:
    if not content or not content.strip():
        return ""
    return f"<{tag}>\n{content}\n</{tag}>"


CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"


class PromptNpcMixin:
    """NPC对话、反应、日程、声线等相关方法"""

    _VOICE_RULES: dict[str, str] = {
        "冷静": "短句，少语气词，陈述为主，偶尔反问",
        "热情": "长句，感叹号多，语气词丰富(呀/呢/啊)，爱用比喻",
        "傲慢": "居高临下，反问句多，'哼''切'，称呼对方用'你们'",
        "怯懦": "短句带省略号…，自我否定，'那个…''不好意思'",
        "豪爽": "大嗓门，粗犷用词，'哈哈''来来来'，不拐弯",
        "阴沉": "低语调，暗示性措辞，'呵''有意思'，话说一半",
        "天真": "简单句，好奇提问多，'哇''为什么呀'，口语化",
        "老练": "缓慢节奏，引用典故，'依我看''当年…'，不急不躁",
        "严肃": "命令式短句，军旅/官方用语，'报告''明白''执行'",
        "温柔": "轻声细语，关心式提问，'没事吧''慢慢来'，少否定",
    }

    def _append_event_countdowns(self, lines: list, state: dict):
        """Add countdown timers for upcoming one-time events."""
        game_time = state.get("game_time", "")
        if not game_time:
            return
        try:
            from datetime import datetime
            now = parse_time(game_time)
            if not now:
                return
            fired_events = set(state.get("fired_one_time_events", []))

            countdowns = []
            for evt in self.script.get("one_time_events", []):
                if evt["id"] in fired_events:
                    continue
                trigger = evt.get("trigger_time", "")
                if not trigger:
                    continue
                t = parse_time(trigger)
                if not t or t <= now:
                    continue
                delta = t - now
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    time_left = f"{int(delta.total_seconds() / 60)}分钟"
                elif hours < 24:
                    time_left = f"{hours:.1f}小时"
                else:
                    time_left = f"{delta.days}天{int(hours % 24)}小时"
                name = evt.get("name", evt.get("description", evt["id"]))
                countdowns.append(f"- 距离「{name}」还有{time_left}")

            if countdowns:
                lines.append("\n## 时间压力")
                lines.extend(countdowns)
        except Exception:
            pass

    def _get_npc_name(self, npc_id: str) -> str:
        return self._npc_name_map.get(npc_id, npc_id)

    def build_nearby_npc_hint(self, nearby_npc_ids: list[str], state: dict) -> str:
        """生成同建筑不同房间 NPC 的提示文本。"""
        if not nearby_npc_ids:
            return ""
        npc_states = state.get("npcs", {})
        lines = []
        for npc_id in nearby_npc_ids[:6]:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", self._npc_name_map.get(npc_id, npc_id))
            room = ns.get("current_room", "")
            title = ns.get("title", "") or self._npc_by_id.get(npc_id, {}).get("title", "")
            desc = f"{name}"
            if title:
                desc += f"（{title}）"
            if room:
                desc += f" → {room}"
            lines.append(f"- {desc}")
        if not lines:
            return ""
        return (
            "\n同建筑但不在同一房间的NPC（不可直接对话或互动，"
            "如需接触须描写前往对方房间或叫来的过程）:\n"
            + "\n".join(lines)
        )

    def _get_present_npcs(self, state: dict, player_loc: str) -> list[str]:
        """返回当前与玩家同一位置的NPC名字列表。"""
        present = []
        game_time = state.get("game_time", "")
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            if npc_id not in self._initial_npc_ids and npc_id not in state.get("npcs", {}):
                continue
            location = self._resolve_npc_location(npc_id, state, game_time)
            if location and location.lower() == player_loc.lower():
                present.append(npc.get("name", npc_id))
        # 也检查动态注册的NPC（不在script中的）
        for npc_id, info in state.get("npcs", {}).items():
            if npc_id not in self._npc_name_map:
                cur_loc = info.get("current_location", info.get("default_location", ""))
                if cur_loc and cur_loc.lower() == player_loc.lower():
                    present.append(info.get("name", npc_id))
        return present

    def _get_schedule_location(self, npc: dict, game_time: str,
                               condition_eval=None, overrides: list | None = None) -> str:
        """根据NPC的schedule和当前时间，返回NPC应在的位置。"""
        entry = self._get_schedule_entry(npc, game_time, condition_eval=condition_eval, overrides=overrides)
        return entry.get("location", "") if entry else ""

    def _check_attribute_alerts(self, state: dict) -> list[str]:
        """Check for attributes at critical levels."""
        alerts = []
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})

        for k, v in attrs.items():
            rule = attr_rules.get(k, {})
            if not isinstance(rule, dict):
                continue
            mx = rule.get("max", 100)
            mn = rule.get("min", 0)
            val = v if isinstance(v, (int, float)) else v.get("value", 0) if isinstance(v, dict) else 0

            # Thresholds from script or defaults
            thresholds = rule.get("thresholds", [])
            for th in thresholds:
                th_val = th.get("value", 0)
                th_dir = th.get("direction", "below")  # "below" or "above"
                if th_dir == "below" and val <= th_val:
                    alerts.append(f"{k}已降至{val}(阈值{th_val}): {th.get('description', '危险')}")
                elif th_dir == "above" and val >= th_val:
                    alerts.append(f"{k}已升至{val}(阈值{th_val}): {th.get('description', '触发')}")

            # Default critical alerts if no thresholds defined
            if not thresholds:
                if val <= mn + (mx - mn) * 0.1:
                    alerts.append(f"⚠ {k}极低({val}/{mx})——应在叙事中体现负面影响")
                elif val >= mx - (mx - mn) * 0.1:
                    alerts.append(f"★ {k}极高({val}/{mx})——可在叙事中体现正面效果")

        return alerts

    def _build_lorebook_section(
        self, activated_lore: list[LorebookEntry] | None, position: str,
        pc_discovered_lore: list[str] | None = None,
        scope: str = "all",
        visibility_markers: bool = False,
    ) -> str:
        if not activated_lore:
            return ""
        entries = self.lorebook.get_entries_by_position(activated_lore, position)
        if not entries:
            return ""
        if scope != "all":
            entries = Lorebook.filter_by_visibility(entries, scope, pc_discovered_lore)
        return self.lorebook.format_for_prompt(entries, visibility_markers=visibility_markers)

    def _build_authors_note_section(self, note: str) -> str:
        if not note:
            return ""
        return f"## 创作者指令\n[以下是玩家的幕后创作指令，请在叙事中体现但不要直接提及]\n{note}"

    def build_npc_talk_prompt(self, state: dict, npc_id: str) -> str:
        """Build a system prompt for NPC-specific conversation.

        The AI plays the role of the NPC and responds in-character.
        Conversation does not advance game time or trigger events.
        """
        loader = PromptLoader.get()

        npc_script = None
        for npc in self.script.get("npcs", []):
            if npc["id"] == npc_id:
                npc_script = npc
                break
        if not npc_script:
            dyn = state.get("npcs", {}).get(npc_id)
            if not dyn or not isinstance(dyn, dict):
                return ""
            npc_script = {"id": npc_id, **dyn}

        npc_state = state.get("npcs", {}).get(npc_id, {})
        npc_name = npc_state.get("name") or npc_script.get("name", npc_id)
        attitude = npc_state.get("attitude_toward_player", npc_script.get("attitude_toward_player", 50))

        # Attitude description
        if attitude >= 80:
            att_desc = "非常信任和喜爱玩家，会热情相待、主动分享信息"
        elif attitude >= 60:
            att_desc = "对玩家友好，愿意交谈和提供帮助"
        elif attitude >= 40:
            att_desc = "对玩家态度中立，公事公办"
        elif attitude >= 20:
            att_desc = "对玩家冷淡警惕，不太愿意交流"
        else:
            att_desc = "对玩家敌对，可能拒绝交流或出言不逊"

        faction_rep = state.get("faction_reputation", {})
        npc_orgs = npc_state.get("organizations") or npc_script.get("organizations", [])
        for org_entry in (npc_orgs or [])[:1]:
            org_id = org_entry.get("org_id", "") if isinstance(org_entry, dict) else org_entry
            rep_data = faction_rep.get(org_id)
            if isinstance(rep_data, dict):
                rep_val = rep_data.get("value", 50)
                if rep_val >= 70:
                    att_desc += "（所属阵营对玩家评价很高，即使首次见面也会表现友善）"
                elif rep_val <= 30:
                    att_desc += "（所属阵营对玩家评价很差，即使无私怨也会冷淡或敌视）"

        # Player info
        player = state.get("player", {})
        player_name = player.get("name", "玩家")

        # Relationship
        rel = player.get("relationships", {}).get(npc_id)
        rel_text = ""
        if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
            rel_text = f"信任{rel.get('trust', 50)}/好感{rel.get('affection', 50)}/畏惧{rel.get('fear', 0)}"
        elif rel is not None:
            rel_text = f"关系值: {rel}"

        # NPC schedule/current activity
        schedule_text = self._get_npc_schedule_text(
            npc_script, state.get("game_time", ""),
            overrides=state.get("npc_schedule_overrides", {}).get(npc_id), state=state,
        )

        # Location
        player_loc = player.get("location", "")

        # 条件行: identity_extra (mood/title/orgs/superior)
        identity_extra_parts = []
        current_mood = npc_state.get("current_mood", "")
        if current_mood:
            identity_extra_parts.append(f"- 当前情绪: {current_mood}（对话中应自然体现此情绪，但不必明说）")
        npc_title = npc_state.get("title") or npc_script.get("title", "")
        if npc_title:
            identity_extra_parts.append(f"- 头衔: {npc_title}")
        npc_orgs = npc_state.get("organizations") or npc_script.get("organizations", [])
        if npc_orgs:
            identity_extra_parts.append(f"- 所属组织: {self._format_org_tags(npc_orgs)}")
        npc_sup = npc_state.get("superior") or npc_script.get("superior", "")
        if npc_sup:
            sup_name = self._get_npc_name(npc_sup)
            identity_extra_parts.append(f"- 上级: {sup_name}")
        identity_extra = "\n".join(identity_extra_parts)

        # 条件行: relationship_line
        relationship_line = f"- 与主角的关系: {rel_text}" if rel_text else ""

        # 条件块: schedule_section
        schedule_section = f"## 当前状态\n{schedule_text}" if schedule_text else ""

        # 条件块: voice_section
        personality = npc_state.get("personality") or npc_script.get("personality", "")
        voice_hint = self._derive_voice_hint(personality)
        if voice_hint:
            voice_section = (
                f"## 说话风格\n"
                f"你的说话风格: {voice_hint}\n"
                f"严格遵循此风格，不要使用与之矛盾的语气词、句式或措辞。"
            )
        else:
            voice_section = ""

        # 条件块: secrets_section
        secrets = npc_script.get("secrets", [])
        secrets_section = ""
        if secrets:
            unlocked_ids = set(state.get("npc_unlocked_secrets", {}).get(npc_id, []))
            revealed = [s for s in secrets if s.get("id") in unlocked_ids]
            hidden = [s for s in secrets if s.get("id") not in unlocked_ids]
            if revealed or hidden:
                sec_parts = ["## 秘密与隐情"]
                if revealed:
                    sec_parts.append("你信任主角到可以透露以下信息（可以在对话中自然提及，但不要一次全说）：")
                    for s in revealed:
                        sec_parts.append(f"- {s.get('content', '')}")
                if hidden:
                    sec_parts.append("以下话题你会回避、否认或转移，绝不透露实质内容：")
                    for s in hidden:
                        sec_parts.append(f"- {s.get('hint', '有所隐瞒')}")
                secrets_section = "\n".join(sec_parts)

        system = loader.render_system(
            "npc_talk",
            npc_name=npc_name,
            player_name=player_name,
            npc_bio=npc_state.get("bio") or npc_script.get("bio", ""),
            npc_personality=npc_state.get("personality") or npc_script.get("personality", ""),
            npc_capabilities=npc_state.get("capabilities") or npc_script.get("capabilities", "未知"),
            npc_id=npc_id,
            attitude=attitude,
            attitude_description=att_desc,
            game_time_formatted=self.format_game_time(state.get("game_time", "")) or "未知",
            player_location=self._resolve_location_name(player_loc),
            world_background=self.script.get("world_background", "")[:200],
            identity_extra=identity_extra,
            relationship_line=relationship_line,
            schedule_section=schedule_section,
            voice_section=voice_section,
            secrets_section=secrets_section,
        )

        return system

    def build_npc_speak_prompt(self, npc_script: dict, state: dict,
                                context: str, previous_speeches: list[dict]) -> dict:
        """Build prompt for a single NPC's independent speech in a group dialogue round.

        Returns {"messages": list, "system": str}.
        """
        npc_id = npc_script.get("id", "")
        npc_state = state.get("npcs", {}).get(npc_id, {})
        npc_name = npc_state.get("name") or npc_script.get("name", npc_id)
        personality = npc_state.get("personality") or npc_script.get("personality", "")
        bio = npc_state.get("bio") or npc_script.get("bio", "")
        attitude = npc_state.get("attitude_toward_player", npc_script.get("attitude_toward_player", 50))

        voice_hint = self._derive_voice_hint(personality)
        player_name = state.get("player", {}).get("name", "主角")

        system = (
            f"你扮演「{npc_name}」，在一个多人场景中发言。\n\n"
            f"## 角色\n"
            f"- 名字: {npc_name}\n"
            f"- 简介: {bio[:200]}\n"
            f"- 性格: {personality}\n"
            f"- 对主角「{player_name}」的态度: {attitude}/100\n"
        )
        if voice_hint:
            system += f"- 说话风格: {voice_hint}\n"
        system += (
            f"\n## 规则\n"
            f"- 只输出{npc_name}的一句对话，不要旁白或动作描写\n"
            f"- 保持角色性格一致\n"
            f"- 根据态度值调整语气\n"
            f"- 对话30-100字\n"
            f"- 不要重复前面角色已说的内容"
        )

        user_parts = []
        if context:
            user_parts.append(f"当前场景:\n{context}")
        if previous_speeches:
            lines = [f"{s['name']}: {s['speech']}" for s in previous_speeches]
            user_parts.append("已有发言:\n" + "\n".join(lines))
        user_parts.append(f"现在轮到{npc_name}发言。")

        messages = [{"role": "user", "content": "\n\n".join(user_parts)}]
        return {"messages": messages, "system": system}

    def _get_npc_schedule_text(self, npc_script: dict, game_time: str, overrides: list | None = None,
                               *, state: dict | None = None) -> str:
        """Get NPC's current activity based on schedule and game time."""
        condition_eval = self.condition_eval
        if state is not None and condition_eval is not None:
            condition_eval = lambda condition: self.condition_eval(condition, state)
        entry = self._get_schedule_entry(npc_script, game_time, condition_eval=condition_eval, overrides=overrides)
        if not entry:
            return ""
        loc = entry.get("location", "")
        act = entry.get("activity", "")
        return f"- 当前位置: {loc}\n- 当前活动: {act}"

    def _get_schedule_entry(self, npc: dict, game_time: str,
                            condition_eval=None, overrides: list | None = None) -> dict | None:
        """Return the best matching schedule entry (with condition/priority support).

        overrides: runtime schedule entries injected by events, checked alongside
        script entries. Expired entries (expires < game_time) are skipped.
        """
        schedule = (npc.get("schedule") or []).copy()
        if overrides:
            for ov in overrides:
                expires = ov.get("expires", "")
                if expires and game_time and expires <= game_time:
                    continue
                schedule.append(ov)
        if not schedule or not game_time:
            return None
        try:
            t = parse_time(game_time)
            if not t:
                return None
            current_minutes = t.hour * 60 + t.minute
            candidates = []
            for entry in schedule:
                time_range = entry.get("time_range") or entry.get("time", "")
                if "-" not in time_range:
                    continue
                start_s, end_s = time_range.split("-", 1)
                sh, sm = (int(x) for x in start_s.strip().split(":"))
                eh, em = (int(x) for x in end_s.strip().split(":"))
                start_min = sh * 60 + sm
                end_min = eh * 60 + em
                if start_min <= end_min:
                    in_range = start_min <= current_minutes < end_min
                else:
                    in_range = current_minutes >= start_min or current_minutes < end_min
                if not in_range:
                    continue
                cond = entry.get("condition", "")
                if cond and condition_eval:
                    if not condition_eval(cond):
                        continue
                elif cond and not condition_eval:
                    continue
                candidates.append(entry)
            if not candidates:
                return None
            return max(candidates, key=lambda e: e.get("priority", 0))
        except Exception:
            return None

    def get_npc_current_info(self, npc_id: str, game_time: str, state: dict | None = None) -> dict | None:
        """Get NPC's current schedule info (location, activity) for display."""
        if state and state.get("npcs", {}).get(npc_id, {}).get("alive") is False:
            return None
        _overrides = (state or {}).get("npc_schedule_overrides", {}).get(npc_id) if state else None
        for npc in self.script.get("npcs", []):
            if npc["id"] == npc_id:
                text = self._get_npc_schedule_text(npc, game_time, overrides=_overrides, state=state)
                if text:
                    lines = text.split("\n")
                    loc = ""
                    act = ""
                    for line in lines:
                        if "当前位置:" in line:
                            loc = line.split("当前位置:", 1)[1].strip()
                        if "当前活动:" in line:
                            act = line.split("当前活动:", 1)[1].strip()
                    return {"location": loc, "activity": act}
                return None
        return None

    def _derive_voice_hint(self, personality: str) -> str:
        """从NPC性格描述中派生说话风格提示。"""
        if not personality:
            return ""
        p_lower = personality.lower()
        for key, hint in self._VOICE_RULES.items():
            if key in p_lower:
                return hint
        return ""

    @staticmethod
    def _format_mechanics_brief(
        dice_results: list[dict] | None = None,
        check_result: dict | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
    ) -> str:
        """为角色行为器提供精简机制摘要（仅标签，不含原始数值）。"""
        parts = []
        if triggered_events:
            descs = [
                f"{e['description']} (触发时间: {e.get('fire_time', '未知')})"
                for e in triggered_events if e.get("description")
            ]
            if descs:
                parts.append("候选定时事件（仅在叙事推进到触发时间后才发生，未到时间不得提前叙述；不代表角色已知）: " + "; ".join(descs))
        if triggered_consequences:
            descs = [c.get("description", "") for c in triggered_consequences if c.get("description")]
            if descs:
                parts.append("本轮触发后果: " + "; ".join(descs))
        if dice_results:
            labels = []
            for d in dice_results:
                src = d.get("source_label", "")
                rl = d.get("range_label", "")
                if rl:
                    labels.append(f"[{src} → {rl}]" if src else f"[→ {rl}]")
            if labels:
                parts.append("本轮骰子效果: " + "; ".join(labels))
        if check_result:
            outcome_map = {
                "critical_success": "大成功", "success": "成功",
                "failure": "失败", "critical_failure": "大失败",
            }
            outcome = outcome_map.get(check_result.get("outcome", ""), "")
            if outcome:
                hint = check_result.get("narrative_hint", "")
                rule = check_result.get("rule", "default")
                rule_tag = {"brp": "[BRP] ", "dnd": "[D&D] "}.get(rule, "")
                parts.append(f"技能检定结果: {rule_tag}{outcome}" + (f"（{hint}）" if hint else ""))
        return "\n".join(parts)

    def _build_npc_voice_table(self, state: dict, present_npc_ids: list[str]) -> str:
        """构建在场NPC声纹速查表，用于角色行为器。"""
        if not present_npc_ids:
            return ""

        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        lines = ["在场NPC声纹速查："]
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            npc_def = npc_defs.get(npc_id, {})
            name = ns.get("name", npc_id)
            title = ns.get("title", "") or npc_def.get("title", "") or npc_def.get("role", "")
            personality = ns.get("personality", "")
            voice_hint = self._derive_voice_hint(personality)
            attitude = ns.get("attitude_toward_player", 50)
            mood = ns.get("current_mood", "")

            title_str = f"（{title}）" if title else ""
            mood_str = f"，情绪={mood}" if mood else ""
            if voice_hint:
                lines.append(
                    f"- {name}{title_str}: 性格={personality} → 说话风格: {voice_hint}，当前态度={attitude}{mood_str}"
                )
            else:
                lines.append(
                    f"- {name}{title_str}: 性格={personality}，当前态度={attitude}{mood_str}（请根据性格推导说话风格）"
                )
        return "\n".join(lines)

    def build_npc_reaction_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        present_npc_ids: list[str] | None = None,
        npc_history: str = "",
        npc_lore: list | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4a: NPC关系推演 - 只处理NPC态度和关系变化。"""
        loader = PromptLoader.get()
        system = loader.render_system("npc_reaction")
        system += CACHE_SENTINEL

        # 在场NPC信息
        npc_states = state.get("npcs", {})
        player_rels = state.get("player", {}).get("relationships", {})
        dn = state.get("display_names", {})
        present = present_npc_ids or []
        npc_script_map = {n["id"]: n for n in self.script.get("npcs", []) if n.get("id")}

        npc_lines = []
        _npc_lore_ids = set()
        if self.lorebook:
            _npc_lore_ids = {
                e.id for e in self.lorebook.entries
                if e.id.startswith("_npc_profile_") and e.enabled
            }
        for npc_id in present:
            npc_st = npc_states.get(npc_id, {})
            if not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", dn.get(npc_id, npc_id))
            rel = player_rels.get(npc_id, {})
            if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                att_str = f"T{t}/A{a}/F{f}"
            else:
                att_str = f"态度{npc_st.get('attitude_toward_player', 50)}"
            talk_val = npc_script_map.get(npc_id, {}).get("talkativeness", 50)
            talk_label = "话多" if talk_val >= 70 else ("沉默寡言" if talk_val <= 30 else "")
            talk_suffix = f" 话痨度={talk_label}" if talk_label else ""
            if f"_npc_profile_{npc_id}" in _npc_lore_ids:
                npc_lines.append(f"- {name}({npc_id}): {att_str}{talk_suffix}")
            else:
                personality = npc_st.get("personality", "")
                npc_lines.append(f"- {name}({npc_id}): {att_str} 性格={personality[:20]}{talk_suffix}")

        # 非在场但被提及的NPC
        for npc_id, npc_st in npc_states.items():
            if npc_id in present or not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", dn.get(npc_id, npc_id))
            if name in narrative:
                rel = player_rels.get(npc_id, {})
                if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                    t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                    att_str = f"T{t}/A{a}/F{f}"
                else:
                    att_str = f"态度{npc_st.get('attitude_toward_player', 50)}"
                npc_lines.append(f"- {name}({npc_id}): {att_str} [不在场但被提及]")

        npc_text = "\n".join(npc_lines) if npc_lines else "无NPC"

        # NPC-NPC已知关系
        npc_rel_known = state.get("npc_relationships_known", {})
        npc_rel_lines = []
        for rel in npc_rel_known.values():
            if not isinstance(rel, dict):
                continue
            if "from" in rel:
                fn = dn.get(rel["from"], rel["from"])
                tn = dn.get(rel["to"], rel["to"])
                npc_rel_lines.append(f"- {fn}→{tn}: {rel.get('description', '')[:30]}")
            else:
                an = dn.get(rel.get("a", ""), rel.get("a", ""))
                bn = dn.get(rel.get("b", ""), rel.get("b", ""))
                npc_rel_lines.append(f"- {an}↔{bn}: {rel.get('type', '')} {rel.get('description', '')[:20]}")

        # 组装 XML
        sections = [_xml("current_location", state.get("player", {}).get("location", "")),
                    _xml("npc_states", npc_text)]
        if npc_rel_lines:
            sections.append(_xml("npc_relationships", "\n".join(npc_rel_lines[:10])))
        sections.append(_xml("player_action", action_text if action_text else ""))
        sections.append(_xml("narrative", narrative))
        if check_result and check_result.get("outcome") in ("critical_success", "critical_failure"):
            crit_label = "大成功" if check_result["outcome"] == "critical_success" else "大失败"
            sections.append(_xml("check_result", f"{crit_label} — NPC对此印象深刻，态度变化幅度应适当放大。"))
        if npc_history:
            sections.append(_xml("npc_history", npc_history))
        if npc_lore:
            lore_lines = [f"[{e.comment or e.id}] {e.content[:100]}" for e in npc_lore[:5]]
            sections.append(_xml("npc_lore", "\n".join(lore_lines)))

        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system
