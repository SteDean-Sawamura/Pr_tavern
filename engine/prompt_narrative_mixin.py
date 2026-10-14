"""PromptBuilder Mixin: 叙事与选项生成相关提示词"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from engine.lorebook import Lorebook
from engine.prompt_loader import PromptLoader

if TYPE_CHECKING:
    pass

# Sentinel inserted between cache-stable and per-turn-varying sections.
# ClaudeProvider splits on this to add cache_control; other providers strip it.
CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"


def _xml(tag: str, content: str) -> str:
    if not content or not content.strip():
        return ""
    return f"<{tag}>\n{content}\n</{tag}>"


class PromptNarrativeMixin:
    """叙事、选项、环境渲染、角色行动等提示词构建"""

    def _collect_state_context(self, state: dict) -> dict:
        """收集共享的状态上下文信息，供 build_choices_prompt / build_world_state_prompt 使用。"""
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        dn = state.get("display_names", {})
        pc_rules = self.script.get("player_character", {}).get("attributes", {})
        inventory = state.get("inventory", [])
        active_ids = set(state.get("active_persistent_states", []))

        # 紧凑属性
        attrs_compact = ", ".join(
            f"{dn.get(k, k)}={v}/{(pc_rules.get(k, {}).get('max', 100) if isinstance(pc_rules.get(k), dict) else 100)}"
            for k, v in attrs.items()
        )
        # 详细属性（含范围）
        attrs_lines = []
        for k, v in attrs.items():
            rule = pc_rules.get(k, {})
            mx = rule.get("max", 100) if isinstance(rule, dict) else 100
            mn = rule.get("min", 0) if isinstance(rule, dict) else 0
            attrs_lines.append(f"- {dn.get(k, k)}({k}): {v} [{mn}-{mx}]")

        # 背包
        inv_compact = ", ".join(
            f"{it['item']}x{it.get('quantity', 1)}{' [可用]' if it.get('use_effect') else ''}" for it in inventory
        ) if inventory else "无"

        # 持续状态（紧凑）
        active_names = []
        for ps in self.script.get("persistent_states", []):
            if ps["id"] in active_ids:
                active_names.append(ps.get("name", ps["id"]))

        # 位置
        loc_id = player.get("location", "")
        location = self._resolve_location_name(loc_id)
        loc_descs = state.get("location_descriptions", {})
        location_desc = loc_descs.get(loc_id, "")[:80] if loc_id else ""

        return {
            "player": player, "attrs": attrs, "dn": dn, "pc_rules": pc_rules,
            "active_ids": active_ids,
            "attrs_compact": attrs_compact,
            "attrs_lines": attrs_lines,
            "inv_compact": inv_compact,
            "active_names": active_names,
            "location": location,
            "location_id": loc_id,
            "location_desc": location_desc,
        }

    def build_choices_prompt(
        self, narrative: str, action_text: str, state: dict, *,
        turn_number: int = 0, activated_lore: list | None = None,
        event_sections: dict[str, str] | None = None,
        pc_discovered_lore: list[str] | None = None,
    ) -> tuple[list[dict], str]:
        """Build prompt for choices-only generation. Returns (messages, system).

        system 只含纯静态 rules（跨回合 100% 缓存命中），
        动态背景注入 user message。
        """
        loader = PromptLoader.get()
        system = loader.render_system("stage_choices")
        system += CACHE_SENTINEL

        # 动态背景注入 user message（而非 system），保持 system 纯静态
        bg_parts = []
        world_brief = self._build_world_brief_section(state)
        if world_brief:
            bg_parts.append(world_brief)
        known_npc_ids = {
            npc_id for npc_id, npc_state in state.get("npcs", {}).items()
            if isinstance(npc_state, dict)
            and npc_state.get("known", self._npc_by_id.get(npc_id, {}).get("known", True)) is not False
        }
        player = state.get("player", {})
        player_loc = player.get("location", "")
        player_room = player.get("current_room", "")
        visible_npcs = []
        visible_npc_ids = set()
        for npc_id, npc_state in state.get("npcs", {}).items():
            if npc_id not in known_npc_ids:
                continue
            npc_def = self._npc_by_id.get(npc_id, {})
            if not player_loc or self._resolve_npc_location(npc_id, state, state.get("game_time", "")) != player_loc:
                continue
            if player_room and npc_state.get("current_room") != player_room:
                continue
            visible_npc_ids.add(npc_id)
            visible_npcs.append(npc_state.get("name") or npc_def.get("name", npc_id))
        player_relationships = player.get("relationships", {})
        choice_state = {**state, "player": {
            **player,
            "relationships": {npc_id: value for npc_id, value in player_relationships.items()
                              if npc_id in visible_npc_ids},
        }}
        character = self._build_character_section(choice_state)
        if character:
            bg_parts.append(character)
        if visible_npcs:
            bg_parts.append("## 当前可见人物\n" + "、".join(visible_npcs))

        if activated_lore and self.lorebook:
            lore_entries = self.lorebook.get_entries_by_position(activated_lore, "after_world")
            lore_entries = Lorebook.filter_by_visibility(lore_entries, "pc_strict", pc_discovered_lore)
            lore_text = self.lorebook.format_for_prompt(lore_entries)
            if lore_text:
                bg_parts.append(lore_text)

        bg_text = "\n\n---\n\n".join(bg_parts) + "\n\n---\n\n" if bg_parts else ""

        # scene_details 摘要
        scene_text = ""
        scene = state.get("scene_details")
        if scene and isinstance(scene, dict):
            s_parts = []
            if scene.get("atmosphere"):
                s_parts.append(f"氛围:{scene['atmosphere']}")
            if s_parts:
                scene_text = f"\n\n场景状态: {' | '.join(s_parts)}"

        # 玩家人设摘要（帮助选项贴合角色，有 lorebook 词条时跳过已覆盖字段）
        player = state.get("player", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        pc_tag = ""
        pc_bits = []
        if player.get("name"):
            pc_bits.append(player["name"])
        if not has_pc_lore:
            if player.get("personality"):
                pc_bits.append(f"性格:{player['personality'][:15]}")
            if player.get("long_term_goal"):
                pc_bits.append(f"目标:{player['long_term_goal'][:25]}")
        if pc_bits:
            pc_tag = f"\n\n主角: {' | '.join(pc_bits)}"

        # 资源余量摘要：让AI能写出有参考价值的cost/benefit hint
        resource_tag = ""
        resource_bits = []
        attrs = player.get("attributes", {})
        for attr_name, attr_val in attrs.items():
            v = attr_val if isinstance(attr_val, (int, float)) else (attr_val.get("value") if isinstance(attr_val, dict) else None)
            if v is not None:
                resource_bits.append(f"{attr_name}={v}")
        inventory = state.get("inventory", [])
        if inventory:
            inv_summary = []
            for item in inventory[:8]:
                name = item.get("item", "") if isinstance(item, dict) else str(item)
                qty = item.get("quantity", 1) if isinstance(item, dict) else 1
                if name:
                    tag = " [可用]" if isinstance(item, dict) and item.get("use_effect") else ""
                    inv_summary.append(f"{name}×{qty}{tag}" if qty > 1 else f"{name}{tag}")
            if inv_summary:
                resource_bits.append(f"背包:[{','.join(inv_summary)}]")
        if resource_bits:
            resource_tag = f"\n\n当前资源: {' | '.join(resource_bits[:10])}"

        # 玩家风格回馈：让选项贴合玩家偏好
        style_hint = ""
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            style_hint = f"\n\n玩家风格: {play_style['tag']}" + (f"（{play_style['description']}）" if play_style.get('description') else "") + "。选项设计应适当倾向此风格偏好，但不排除其他方向。"

        # 环境互动元素
        interactable_hint = ""
        interactables = [ia for ia in state.get("location_interactables", []) if not ia.get("used")]
        if interactables:
            items = ["可互动元素（可作为选项来源）:"]
            for ia in interactables[:4]:
                items.append(f"- {ia.get('name', '')}: {ia.get('description', '')}")
            interactable_hint = "\n\n" + "\n".join(items)

        # 同伴意见提示
        companion_hint = ""
        companion_ids = state.get("companions", [])
        if companion_ids:
            npcs_state = state.get("npcs", {})
            dn = state.get("display_names", {})
            c_parts = ["同伴（可在选项hint中自然融入同伴的态度或建议）:"]
            for cid in companion_ids[:3]:
                ns = npcs_state.get(cid, {})
                if not isinstance(ns, dict) or cid not in known_npc_ids:
                    continue
                cname = dn.get(cid) or (ns.get("name", cid) if isinstance(ns, dict) else cid)
                c_parts.append(f"- {cname}")
            if len(c_parts) > 1:
                companion_hint = "\n\n" + "\n".join(c_parts)

        # 声望提示
        rep_tag = ""
        faction_rep = state.get("faction_reputation", {})
        if faction_rep:
            org_name_map = {o.get("id", ""): o.get("name", o.get("id", "")) for o in self.script.get("organizations", [])}
            rep_items = []
            for fid, fdata in faction_rep.items():
                fname = org_name_map.get(fid, state.get("display_names", {}).get(fid, fid))
                val = fdata.get("value", 50) if isinstance(fdata, dict) else 50
                rep_items.append(f"{fname}={val}")
            if rep_items:
                rep_tag = f"\n\n阵营声望: {' | '.join(rep_items)}。声望极低时某些选项可被locked"

        # 线索板摘要（辅助设计调查/推理类选项）
        clue_hint = ""
        if event_sections and event_sections.get("clues"):
            clue_hint = f"\n\n已发现线索（可设计调查/验证/追踪类选项）:\n{event_sections['clues']}"

        # 意外选项机制：只依据玩家可感知的天气
        surprise_hint = ""
        extreme_weather = any(w in (state.get("current_weather") or "") for w in ("暴雨", "大雪", "极端"))
        if extreme_weather:
            surprise_hint = "\n\n[特殊情境] 恶劣天气下，可以额外添加一个玩家主动避险的选项（id用c_event），不可编写新的叙事场景。"

        # 房间位置提示
        room_hint = ""
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            room_hint = f"\n\n当前房间: {player_room}（选项中涉及移动时应以此为起点）"

        # 排列：角色/资源框架(primacy) → 场景状态 → 行动 → 叙事(recency=设计依据)
        # 选项只关注叙事结尾状态，截断前部减少 token
        narrative_tail = narrative[-800:] if len(narrative) > 800 else narrative
        content = f"""{bg_text}{pc_tag}{resource_tag}{rep_tag}{style_hint}{scene_text}{interactable_hint}{companion_hint}{clue_hint}{surprise_hint}{room_hint}

玩家行动: {action_text}

叙事内容（选项必须基于叙事结尾时的场景状态设计）:
{narrative_tail}"""

        messages = [{"role": "user", "content": content}]
        return messages, system

    def build_plot_decision_prompt(
        self,
        player_action: str,
        state: dict,
        check_result: dict | None = None,
        dice_results: list[dict] | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
        achieved_milestones: list[dict] | None = None,
        present_npc_ids: list[str] | None = None,
        history_context: str = "",
        recent_reasoning: list[dict] | None = None,
        game_tools: dict | None = None,
        prev_narrative_tail: str = "",
        prev_plot_decision: str = "",
        recent_narratives: list[dict] | None = None,
        event_sections: dict[str, str] | None = None,
        recall_hints: list[str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 1: 剧情决策器 - 只决定发生了什么，不写文学描写。"""
        loader = PromptLoader.get()
        system = loader.render_system("stage_plot_decision")
        system += CACHE_SENTINEL

        has_events = bool(triggered_events or triggered_consequences or achieved_milestones)
        has_dice = bool(dice_results or check_result)
        has_npcs = bool(present_npc_ids)
        if not has_events and not has_dice and not has_npcs:
            system += (
                "\n\n## 本轮回合类型：日常/独处\n"
                "无触发事件、无检定、无在场NPC。\n"
                "- [关键事件] 只写行动的直接结果（0-1条），没有外部事件发生就写'无'\n"
                "- [NPC决策] 写'无'\n"
                "- [世界脉搏] 写'无'\n"
                "- [场景约束] 仅描述当前物理环境，不引入任何新元素"
            )
        elif not has_npcs and not has_events:
            system += (
                "\n\n## 本轮回合类型：独处行动\n"
                "无在场NPC、无触发事件。\n"
                "- [NPC决策] 写'无'\n"
                "- [世界脉搏] 最多1条，无合理推断则写'无'"
            )

        if game_tools:
            tool_lines = ["\n\n可用工具（在输出中使用 [TOOL_CALL: name(args)] 格式调用）:"]
            for name, info in game_tools.items():
                tool_lines.append(f"- {name}: {info['desc']}  示例: [TOOL_CALL: {info['example']}]")
            system += "\n".join(tool_lines)

        # 构建用户消息 — XML 标签分区
        # 排列策略：稳定框架(primacy) → 中间上下文 → 动态行动(recency)

        # ─── <world> 区块：世界框架 ───
        player = state.get("player", {})
        pc = self.script.get("player_character", {})
        world_parts = []
        world_bg = self.script.get("world_background", "")
        if world_bg:
            world_parts.append(f"背景: {world_bg[:200]}")
        game_time = state.get("game_time", "")
        if game_time:
            world_parts.append(f"时间: {self.format_game_time(game_time) or game_time}")
        location_id = player.get("location", "")
        if location_id:
            loc_def = self._location_by_id.get(location_id, {})
            location_name = loc_def.get("name", location_id)
            location_desc = loc_def.get("description", "")
            world_parts.append(f"位置: {location_name}" + (f" - {location_desc[:80]}" if location_desc else ""))
        active_ids = set(state.get("active_persistent_states", []))
        if active_ids:
            ps_defs = {ps["id"]: ps for ps in self.script.get("persistent_states", [])}
            display_names = state.get("display_names", {})
            ps_descs = state.get("persistent_state_descriptions", {})
            ps_lines = []
            for sid in active_ids:
                ps_def = ps_defs.get(sid, {})
                name = ps_def.get("name") or display_names.get(sid, sid)
                desc = ps_def.get("description") or ps_descs.get(sid, "")
                ps_lines.append(f"{name}({desc[:60]})" if desc else name)
            if ps_lines:
                world_parts.append(f"⚠ 持续状态（不可无视）: {'; '.join(ps_lines[:5])}")

        # ─── <protagonist> 区块：主角信息 ───
        player_name = player.get("name", "主角")
        player_title = player.get("title", "") or pc.get("title", "")
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        proto_parts = [f"主角: {player_name}"]
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_hint = f"（NPC称呼: {_surname}{_short_title}）" if _surname else ""
            proto_parts.append(f"头衔/职位: {player_title}{_call_hint}")
        if not has_pc_lore:
            player_bio = player.get("bio", "") or pc.get("bio", "")
            if player_bio:
                proto_parts.append(f"身份: {player_bio[:100]}")
        inventory = state.get("inventory", [])
        if inventory:
            inv_items = [f"{it.get('item', '?')}x{it.get('quantity', 1)}" for it in inventory]
            proto_parts.append(f"携带物品: {', '.join(inv_items)}")
        else:
            proto_parts.append("携带物品: 无")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            proto_parts.append(f"当前房间: {player_room}")

        # ─── <npcs_present> 区块：在场NPC ───
        present = present_npc_ids or []
        npc_states = state.get("npcs", {})
        npc_defs = {n.get("id", n.get("name", "")): n for n in self.script.get("npcs", [])}
        player_rels = state.get("player", {}).get("relationships", {})
        npc_lines = []
        if present:
            header = (
                "同一建筑内NPC（不在玩家同一房间的NPC不可直接出现，须描写移动过程）:"
                if player_room else "在场NPC:"
            )
            npc_lines.append(header)
            for npc_id in present:
                ns = npc_states.get(npc_id, {})
                if not isinstance(ns, dict):
                    continue
                npc_def = npc_defs.get(npc_id, {})
                name = ns.get("name", npc_id)
                title = (
                    ns.get("title", "")
                    or npc_def.get("title", "")
                    or npc_def.get("role", "")
                    or npc_def.get("occupation", "")
                )
                personality = ns.get("personality", "")
                npc_room = ns.get("current_room", "")
                if not npc_room:
                    info = self.get_npc_current_info(npc_id, game_time, state=state)
                    if info and info.get("activity"):
                        npc_room = info["activity"][:20]
                rel = player_rels.get(npc_id, {})
                if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                    att_str = f"T{rel.get('trust', 50)}/A{rel.get('affection', 50)}/F{rel.get('fear', 0)}"
                else:
                    att_str = f"态度{ns.get('attitude_toward_player', 50)}"
                rel_desc = rel.get("description", "") if isinstance(rel, dict) else ""
                rel_str = f"，关系={rel_desc}" if rel_desc else ""
                room_str = f" → {npc_room}" if npc_room else ""
                personality_str = f"性格={personality}，" if personality else ""
                npc_lines.append(f"- {name}{room_str}（{f'职位={title}，' if title else ''}{personality_str}{att_str}{rel_str}）")

        # ─── <npcs_offscreen> 区块：离场NPC ───
        offscreen_lines = []
        present_set = set(present)
        for npc_id in list(npc_defs.keys())[:20]:
            if npc_id in present_set:
                continue
            loc = self._resolve_npc_location(npc_id, state, game_time)
            if loc:
                name = npc_states.get(npc_id, {}).get("name", npc_id) if isinstance(npc_states.get(npc_id), dict) else npc_id
                loc_name = self._loc_name_map.get(loc, loc)
                offscreen_lines.append(f"{name}→{loc_name}")

        # ─── <knowledge> 区块：已知信息 ───
        knowledge_parts = []
        if event_sections and event_sections.get("clues"):
            knowledge_parts.append(f"已知线索: {event_sections['clues']}")
        if not self.lorebook:
            key_events = state.get("key_events", [])
            if key_events:
                recent_events = key_events[-8:]
                events_str = "；".join(f"第{e.get('turn', '?')}回合:{e.get('event', '')[:40]}" for e in recent_events)
                knowledge_parts.append(f"此前关键事件: {events_str}")
        persistent_facts = state.get("persistent_facts", [])
        if persistent_facts:
            facts_str = "；".join(
                f"[{f.get('type', '?')}]{f.get('fact', '')}" for f in persistent_facts[-15:]
            )
            knowledge_parts.append(f"持久事实（不可遗忘）: {facts_str}")
        bp = state.get("plot_blueprint", {})
        bp_threads = bp.get("plot_threads", [])
        if bp_threads:
            bp_lines = []
            for thread in bp_threads[:5]:
                tid = thread.get("id", "")
                stages = thread.get("stages", [])
                current_stage = None
                for stage in stages:
                    status = stage.get("status", "pending")
                    if status == "active":
                        current_stage = f"进行中: {stage.get('description', '')}"
                        break
                    elif status == "pending":
                        current_stage = f"待触发: {stage.get('description', '')}"
                        break
                if current_stage:
                    bp_lines.append(f"- {thread.get('name', tid)}: {current_stage}")
            if bp_lines:
                if bp.get("_source") == "script_anchored":
                    knowledge_parts.append("剧本锚定的推演方向（仅作局势参考，不得当作已发生事实）:\n" + "\n".join(bp_lines))
                else:
                    knowledge_parts.append("全局剧情走向（必须在本回合有所体现，至少一条）:\n" + "\n".join(bp_lines))

        # ─── <directives> 区块：动态提示/指令 ───
        directive_parts = []
        if history_context:
            directive_parts.append(history_context)
        if recent_reasoning:
            reasoning_lines = ["AI前几轮的决策思路（保持连贯，但不被束缚）:"]
            for r in recent_reasoning:
                reasoning_lines.append(f"第{r['turn']}回合思路: {r['reasoning']}")
            directive_parts.append("\n".join(reasoning_lines))
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            ps_tag = play_style["tag"]
            ps_desc = play_style.get("description", "")
            ps_line = f"玩家风格: {ps_tag}"
            if ps_desc:
                ps_line += f"（{ps_desc}）"
            ps_turn = play_style.get("turn", 0)
            approx_turn = len(state.get("adventure_log", []))
            if approx_turn - ps_turn >= 5 and approx_turn % 5 == 0:
                ps_line += "\n⚠ 本回合请设计至少一个选项挑战玩家的舒适区（与其惯性风格相反的行动方向），制造成长机会"
            else:
                ps_line += "\n剧情走向应自然回应此风格，但不必刻意迎合"
            directive_parts.append(ps_line)

        # ─── <previous_turn> 区块：上一轮骨架/叙事 ───
        prev_turn_text = ""
        if prev_plot_decision:
            prev_turn_text = prev_plot_decision
        elif prev_narrative_tail and not recent_narratives:
            prev_turn_text = f"上一轮叙事结尾:\n{prev_narrative_tail}"

        # ─── <turn_input> 区块：本轮输入 ───
        turn_parts = []
        # 检定/骰子
        dice_lines = []
        if check_result:
            outcome = check_result.get("outcome", "")
            success = "成功" if outcome in ("success", "critical_success") else "失败"
            if outcome == "critical_success":
                success = "大成功"
            elif outcome == "critical_failure":
                success = "大失败"
            rule = check_result.get("rule", "default")
            rule_hint = {"brp": " [BRP:骰点≤技能值=成功]",
                         "dnd": " [D&D:d20+修正≥DC=成功]"}.get(rule, "")
            dice_lines.append(f"检定结果: {success}{rule_hint}（{check_result.get('narrative_hint', '')}）")
        if dice_results:
            for d in dice_results:
                if d.get("candidate_event_id"):
                    dice_lines.append(
                        f"候选事件骰子（仅 {d['candidate_event_id']} 在 {d.get('candidate_fire_time', '触发时间')} 后实际发生时生效）: "
                        f"{d.get('source_label', '')} = {d.get('total', '')}（{d.get('range_label', '')}）"
                    )
                elif d.get("candidate_destination"):
                    dice_lines.append(
                        f"候选条件骰子（仅实际抵达行动所指目的地时生效）: "
                        f"{d.get('source_label', '')} = {d.get('total', '')}（{d.get('range_label', '')}）"
                    )
                else:
                    dice_lines.append(f"骰子: {d.get('source_label', '')} = {d.get('total', '')}（{d.get('range_label', '')}）")

        # 触发事件
        event_lines = []
        if triggered_events:
            event_lines.append("候选定时事件（仅在叙事推进到触发时间后才发生，未到时间不得提前叙述；不代表角色已知）:")
            for ev in triggered_events:
                event_lines.append(f"- {ev.get('description', ev.get('event_id', ''))} (触发时间: {ev.get('fire_time', '未知')})")
        if triggered_consequences:
            event_lines.append("延迟后果触发:")
            for c in triggered_consequences:
                event_lines.append(f"- {c.get('description', '')}")
        if achieved_milestones:
            event_lines.append("达成里程碑:")
            for m in achieved_milestones:
                event_lines.append(f"- {m.get('description', m.get('id', ''))}")

        # 动态事件
        dyn_event_lines = []
        dyn_events = state.get("_dynamic_events_this_turn", [])
        if dyn_events:
            for de in dyn_events:
                cb_texts = [ef.get("text", "") for ef in de.get("effects", []) if ef.get("type") == "narrative_callback" and ef.get("text")]
                if cb_texts:
                    dyn_event_lines.append(f"- 事件 {de['id']}: {'; '.join(cb_texts)}")

        # 时限
        deadline_text = ""
        if event_sections and event_sections.get("deadlines"):
            deadline_text = f"NPC必须记得这些约定，不可做出矛盾安排:\n{event_sections['deadlines']}"

        # NPC介入
        intervention_lines = []
        interventions = state.get("npc_interventions", [])
        if interventions:
            for itv in interventions[:2]:
                urgency = "【必须立即发生】" if itv.get("urgency") == "high" else "【可自然织入】"
                intervention_lines.append(f"- {itv['npc_name']}（{itv['type']}）{urgency}: {itv.get('suggested_action', '')}。原因: {itv.get('reason', '')}")

        # 场景快照
        snapshot_text = ""
        if prev_narrative_tail and not recent_narratives:
            tail_anchor = prev_narrative_tail[-300:] if len(prev_narrative_tail) > 300 else prev_narrative_tail
            snapshot_text = tail_anchor

        # 玩家行动
        _manner_kws = {"暗中": "秘密行动", "秘密": "秘密行动", "偷偷": "秘密行动",
                       "伪装": "伪装身份", "假扮": "伪装身份", "公开": "公开行动",
                       "强行": "强硬行动", "小心": "谨慎行动", "谨慎": "谨慎行动"}
        _manner_tags = []
        for kw, label in _manner_kws.items():
            if kw in player_action:
                _manner_tags.append(label)
        _manner_hint = ""
        if _manner_tags:
            _manner_hint = f"⚠ 行动方式: {'/'.join(set(_manner_tags))}（必须在骨架中体现，不可忽略）\n"
        action_text = f"{_manner_hint}{player_action}"
        if recall_hints:
            action_text += f"\n提示：本轮可能需要检索以下关键词：{'、'.join(recall_hints)}"

        # ─── 组装最终 XML 结构 ───
        sections = []
        sections.append(_xml("world", "\n".join(world_parts)))
        sections.append(_xml("protagonist", "\n".join(proto_parts)))
        sections.append(_xml("npcs_present", "\n".join(npc_lines)))
        if offscreen_lines:
            sections.append(_xml("npcs_offscreen",
                f"不在玩家建筑内，玩家无法感知其活动——不可描写来自这些地点的声音/气味/视觉:\n{'; '.join(offscreen_lines[:8])}"))
        sections.append(_xml("knowledge", "\n".join(knowledge_parts)))
        sections.append(_xml("directives", "\n".join(directive_parts)))
        sections.append(_xml("previous_turn", prev_turn_text))

        # turn_input 子标签
        ti_parts = []
        if dice_lines:
            ti_parts.append(_xml("dice", "\n".join(dice_lines)))
        if event_lines:
            ti_parts.append(_xml("events", "\n".join(event_lines)))
        if dyn_event_lines:
            ti_parts.append(_xml("dynamic_events", "必须在叙事中自然体现:\n" + "\n".join(dyn_event_lines)))
        if deadline_text:
            ti_parts.append(_xml("deadlines", deadline_text))
        if intervention_lines:
            ti_parts.append(_xml("interventions", "必须在本回合剧情中体现:\n" + "\n".join(intervention_lines)))
        if snapshot_text:
            ti_parts.append(_xml("scene_snapshot", snapshot_text))
        ti_parts.append(_xml("player_action", action_text))
        sections.append(_xml("turn_input", "\n".join(ti_parts)))

        content = "\n\n".join(s for s in sections if s)

        messages = []
        if recent_narratives:
            # P1: 只保留最近 2 轮历史，更早的历史由 Agent 通过 recall_history 工具按需拉取
            recent_narratives = recent_narratives[-2:]
            for rn in recent_narratives:
                action = rn.get("action", "")
                narrative = rn.get("narrative", "")
                turn_label = rn.get("turn", "?")
                if not action:
                    action = "(场景描写)"
                messages.append({"role": "user", "content": f"[第{turn_label}回合 玩家行动] {action}"})
                if narrative:
                    messages.append({"role": "assistant", "content": f"[第{turn_label}回合 叙事结果]\n{narrative}"})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_env_render_prompt(
        self, plot_decision: str, state: dict
    ) -> tuple[list[dict], str]:
        """Stage 2a: 环境渲染器 - 生成环境/氛围描写。"""
        loader = PromptLoader.get()
        system = loader.render_system("stage_env_render")
        system += CACHE_SENTINEL

        # 当前位置信息
        location_id = state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(location_id, {})
        location_name = loc_def.get("name", location_id) if loc_def else location_id
        location_desc = loc_def.get("description", "") if loc_def else ""

        # 时段和天气
        game_time = state.get("game_time", "")
        time_of_day = self.format_game_time(game_time) if game_time else ""
        weather = state.get("current_weather", "")

        # 上一轮 scene_details
        scene_details = state.get("scene_details", {})
        prev_atmosphere = scene_details.get("atmosphere", "")
        prev_sensory = scene_details.get("sensory", "")

        # <scene> 区块
        scene_parts = [f"位置: {location_name}"]
        if location_desc:
            scene_parts.append(f"位置描述: {location_desc[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            scene_parts.append(f"具体房间: {player_room}（环境描写应聚焦此房间）")
        world_bg = self.script.get("world_background", "")
        if world_bg:
            scene_parts.append(f"时代背景: {world_bg[:100]}")
        if time_of_day:
            scene_parts.append(f"时段: {time_of_day}")
        if game_time:
            try:
                month = int(game_time[5:7]) if len(game_time) >= 7 else 0
            except (ValueError, TypeError):
                month = 0
            season_map = {1: "冬季", 2: "冬季", 3: "初春", 4: "春季", 5: "春季",
                          6: "初夏", 7: "夏季", 8: "夏季", 9: "初秋", 10: "秋季",
                          11: "深秋", 12: "冬季"}
            season = season_map.get(month, "")
            year = game_time[:4] if len(game_time) >= 4 else ""
            if season:
                date_label = f"{year}年{month}月 {season}" if year else season
                scene_parts.append(f"⚠ 当前年月与季节: {date_label}（硬约束：天气、植被、气温必须符合该月份，"
                                   f"例如10月不可能有六月暴雨或盛夏酷暑）")
        if weather:
            scene_parts.append(f"天气: {weather}")
        else:
            scene_parts.append("天气: 未指定（沿用上一轮氛围中的天气，不可自行发明新天气）")

        # <continuity> 区块
        cont_parts = []
        loc_mem = state.get("location_memory", {}).get(location_id, [])
        if loc_mem:
            mem_lines = [f"- 第{m['turn']}回合: {m['text']}" for m in loc_mem[-3:]]
            cont_parts.append("此地的历史事件（环境中可微妙反映残留痕迹）:\n" + "\n".join(mem_lines))
        if prev_atmosphere or prev_sensory:
            cont_parts.append(f"上一轮氛围（仅供天气/光线连续参考）: {prev_atmosphere} {prev_sensory}")
            cont_parts.append("⚠ 位置隔离：上一轮的特有声源和物件（如某地的管风琴、某处的机器声）"
                              "不可搬到当前位置——不同地点的环境互相独立")
            _prev_text = f"{prev_atmosphere} {prev_sensory}"
            _imagery_kws = set()
            for _pat in [r'军靴声?', r'钟声', r'枪声', r'脚步声', r'雨[声滴]?',
                         r'霓虹[灯]?', r'烟[头蒂味]', r'月[光色]', r'阴云', r'探照灯',
                         r'荧光灯', r'铁丝网', r'水磨石', r'警报声?', r'挂钟']:
                _m = re.search(_pat, _prev_text)
                if _m:
                    _imagery_kws.add(_m.group())
            if _imagery_kws:
                cont_parts.append(f"⚠ 上轮已用意象（本轮换用其他）: {'、'.join(_imagery_kws)}")
        _story_hint = self._build_stage2_story_hint(state)
        if _story_hint:
            cont_parts.append(_story_hint)

        # 组装 XML
        sections = [
            _xml("scene", "\n".join(scene_parts)),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("plot_decision", plot_decision),
        ]
        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system

    def build_character_action_prompt(
        self, plot_decision: str, state: dict,
        present_npc_ids: list[str] | None = None,
        dice_results: list[dict] | None = None,
        check_result: dict | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 2b: 角色行为器 - 生成NPC对话和动作描写。"""
        loader = PromptLoader.get()
        system = loader.render_system("stage_character_action")
        system += CACHE_SENTINEL

        # 主角信息
        player = state.get("player", {})
        player_name = player.get("name", "主角")
        pc = self.script.get("player_character", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        if not has_pc_lore:
            player_personality = player.get("personality", "") or pc.get("personality", "")
            player_identity = pc.get("identity", "") or pc.get("background", "") or pc.get("bio", "")
        else:
            player_personality = ""
            player_identity = ""

        # NPC声纹速查表
        present = present_npc_ids or []
        voice_table = self._build_npc_voice_table(state, present)

        # NPC已交互次数（通过 met 状态和聊天历史判断）
        npc_states = state.get("npcs", {})
        interaction_info = []
        chat_histories = state.get("npc_chat_history", {})
        for npc_id in present:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            met = ns.get("met", False)
            chat_count = len(chat_histories.get(npc_id, []))
            if met or chat_count > 0:
                interaction_info.append(f"{name}已交互过（勿重复自我介绍）")

        player_title = player.get("title", "") or pc.get("title", "")

        # <protagonist> 区块
        world_bg = self.script.get("world_background", "")
        proto_parts = []
        if world_bg:
            proto_parts.append(f"世界背景: {world_bg[:150]}")
        game_time = state.get("game_time", "")
        if game_time:
            proto_parts.append(f"当前时间: {self.format_game_time(game_time) or game_time}")
        proto_parts.append(f"主角: {player_name}")
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_example = f"「{_surname}{_short_title}」" if _surname else f"「{_short_title}」"
            proto_parts.append(f"头衔/职位: {player_title}（NPC称呼主角为{_call_example}）")
        elif player_name and player_name != "主角":
            proto_parts.append(f"（NPC称呼主角时应使用姓氏「{player_name[0]}」加适当敬称）")
        if player_personality:
            proto_parts.append(f"性格: {player_personality}")
        if player_identity:
            proto_parts.append(f"身份: {player_identity}")
        inv_items = state.get("inventory", [])
        inv_str = ", ".join(it.get("item", "?") for it in inv_items) if inv_items else "无"
        proto_parts.append(f"携带物品: {inv_str}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            proto_parts.append(f"当前房间: {player_room}")

        # <npc_profiles> 区块
        npc_profile_parts = []
        if voice_table:
            npc_profile_parts.append(voice_table)
        if interaction_info:
            npc_profile_parts.append(f"交互记录: {'; '.join(interaction_info)}")
        npc_rooms = []
        for npc_id in present:
            ns = npc_states.get(npc_id, {})
            if isinstance(ns, dict):
                room = ns.get("current_room", "")
                if room:
                    npc_rooms.append(f"{ns.get('name', npc_id)} → {room}")
        if npc_rooms:
            npc_profile_parts.append(f"NPC房间位置: {'; '.join(npc_rooms)}")

        # <mechanics> 区块
        mechanics = self._format_mechanics_brief(
            dice_results, check_result, triggered_events, triggered_consequences,
        )

        # <continuity> 区块
        _story_hint = self._build_stage2_story_hint(state)

        # 组装 XML
        sections = [
            _xml("protagonist", "\n".join(proto_parts)),
            _xml("npc_profiles", "\n".join(npc_profile_parts)),
            _xml("mechanics", f"角色行为必须与之一致:\n{mechanics}" if mechanics else ""),
            _xml("continuity", _story_hint or ""),
            _xml("plot_decision", plot_decision),
        ]
        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system

    def _build_standard_narrative_system(
        self, scope: str, scene_type: str,
        negative_prompt: str, logit_bias_hint: str,
    ) -> str:
        """Stage 3 system prompt — moderate/major scope."""
        loader = PromptLoader.get()
        tpl = loader._load("stage_narrative")
        system = tpl.get("system_standard", "")
        system += CACHE_SENTINEL
        _rhythm_hints = {
            "combat": "\n\n## 节奏指引（战斗）\n用短句和断句制造紧迫感，动作描写干脆利落，不要在战斗中间插入大段环境或心理描写。",
            "exploration": "\n\n## 节奏指引（探索）\n发现关键事物时用短句突出，制造'发现感'。环境描写服务于线索呈现，不要喧宾夺主。",
            "social": "\n\n## 节奏指引（社交）\n以对话和人物反应驱动节奏，环境描写只在转场时出现。对话间留白，不要每句话后都跟心理旁白。",
        }
        if scene_type in _rhythm_hints:
            system += _rhythm_hints[scene_type]
        if negative_prompt:
            system += f"\n\n## 绝对禁止事项\n{negative_prompt}"
        if logit_bias_hint:
            system += f"\n\n{logit_bias_hint}"
        return system

    def _build_minor_narrative_system(
        self, negative_prompt: str, logit_bias_hint: str,
    ) -> str:
        """Stage 3 system prompt — minor scope (轻量行动)."""
        loader = PromptLoader.get()
        tpl = loader._load("stage_narrative")
        system = tpl.get("system_minor", "")
        system += CACHE_SENTINEL
        if negative_prompt:
            system += f"\n\n## 绝对禁止事项\n{negative_prompt}"
        if logit_bias_hint:
            system += f"\n\n{logit_bias_hint}"
        return system

    def build_narrative_compose_prompt(
        self,
        plot_decision: str,
        env_text: str,
        char_text: str,
        state: dict,
        recent_openings: list[str] | None = None,
        missing_env: bool = False,
        missing_char: bool = False,
        history_context: str = "",
        prev_narrative_tail: str = "",
        scene_type: str = "",
        prev_ending_type: str = "",
        authors_note: str = "",
        action_text: str = "",
        negative_prompt: str = "",
        logit_bias_hint: str = "",
        scope: str = "",
        estimated_minutes: int = 30,
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 3: 叙事润色器 - 将素材整合为流畅完整叙事。"""
        if scope == "minor":
            system = self._build_minor_narrative_system(negative_prompt, logit_bias_hint)
        else:
            system = self._build_standard_narrative_system(
                scope, scene_type, negative_prompt, logit_bias_hint
            )
        # <scene_context> 区块
        ctx_parts = []
        game_time = state.get("game_time", "")
        if game_time:
            try:
                from datetime import datetime as _dt
                _gt = _dt.fromisoformat(game_time.replace("Z", "+00:00"))
                _month = _gt.month
                _year = str(_gt.year)
                season_map = {1: "严冬", 2: "冬末", 3: "初春", 4: "春季",
                              5: "暮春", 6: "初夏", 7: "盛夏", 8: "夏末",
                              9: "初秋", 10: "深秋", 11: "深秋", 12: "冬季"}
                _season = season_map.get(_month, "")
                ctx_parts.append(
                    f"回合起始时间: {_year}年{self.format_game_time(game_time)}（{_season}），"
                    f"若剧情骨架推进了时间则以骨架为准"
                )
            except Exception:
                pass
        if scope:
            time_hints = {
                "minor": "本回合是短暂行动，叙事时间跨度不超过30分钟",
                "moderate": "本回合是常规行动，叙事可覆盖30分钟到2小时",
                "major": "本回合是重大行动，叙事可覆盖数小时甚至一整天，需要自然的时间过渡",
            }
            if scope in time_hints:
                ctx_parts.append(f"时间节奏: {time_hints[scope]}")
        if estimated_minutes and estimated_minutes > 0:
            if estimated_minutes >= 60:
                h = estimated_minutes // 60
                m = estimated_minutes % 60
                dur_str = f"{h}小时" + (f"{m}分钟" if m else "")
            else:
                dur_str = f"{estimated_minutes}分钟"
            ctx_parts.append(
                f"玩家行动预期时长: 约{dur_str}。叙事中的时间流逝应与此一致，"
                "若玩家明确要求等待特定时长，叙事必须覆盖该完整时段"
            )
        world_bg = self.script.get("world_background", "")
        if world_bg:
            ctx_parts.append(f"时代背景（地名/称谓须符合该时代）: {world_bg[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            ctx_parts.append(f"玩家当前房间: {player_room}（叙事场景应以此房间为起点）")
        inventory = state.get("inventory", [])
        if inventory:
            inv_str = ", ".join(it.get("item", "?") for it in inventory[:8])
            ctx_parts.append(f"玩家携带物品: {inv_str}")

        # <naming_reference> 区块
        pc_info = state.get("player", {})
        pc_name = pc_info.get("name", "")
        pc_title = pc_info.get("title", "") or pc_info.get("role", "") or pc_info.get("occupation", "")
        title_pairs = []
        if pc_name:
            pc_label = f"★主角(第二人称\"你\"): {pc_name}"
            if pc_title:
                pc_label += f"（{pc_title}）"
            title_pairs.append(pc_label)
        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for npc_id, ns in npc_states.items():
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            npc_def = npc_defs.get(npc_id, {})
            title = (
                ns.get("title", "")
                or npc_def.get("title", "")
                or npc_def.get("role", "")
                or npc_def.get("occupation", "")
            )
            if title:
                title_pairs.append(f"{name}（{title}）")
            elif name != npc_id:
                title_pairs.append(name)
        naming_text = ""
        if title_pairs:
            naming_text = (
                f"叙事中必须使用正确称谓，NPC称呼主角时必须用主角的姓氏，不可混用其他NPC的姓氏:\n"
                f"{'; '.join(title_pairs)}"
            )

        # <continuity> 区块
        cont_parts = []
        if history_context:
            cont_parts.append(f"前情提要:\n{history_context}")
        if scope != "minor" and recent_openings:
            cont_parts.append(f"近几轮叙事开头（请避免雷同）: {' / '.join(recent_openings[-3:])}")
        if scope != "minor" and prev_ending_type:
            cont_parts.append(f"上一轮结尾类型: {prev_ending_type}（本轮必须使用不同类型）")
        pending = state.get("scene_details", {}).get("pending_tension", "")
        if pending:
            cont_parts.append(f"待定伏线（适度铺垫）: {pending}")

        # <materials> 区块
        mat_parts = []
        if env_text:
            mat_parts.append(_xml("environment", env_text))
        elif missing_env:
            mat_parts.append(_xml("environment", "（环境渲染失败，请根据剧情骨架中的位置/时段自行补充2-3种感官描写）"))
        if char_text:
            mat_parts.append(_xml("characters", char_text))
        elif missing_char:
            mat_parts.append(_xml("characters", "（角色行为生成失败，请根据剧情骨架中的NPC决策自行补充对话和动作描写）"))

        # 组装 XML
        sections = [
            _xml("scene_context", "\n".join(ctx_parts)),
            _xml("naming_reference", naming_text),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("materials", "\n".join(mat_parts)),
            _xml("plot_decision", plot_decision),
            _xml("player_action", action_text if action_text else ""),
            _xml("authors_note", authors_note if authors_note else ""),
        ]
        content = "\n\n".join(s for s in sections if s)

        messages = []
        if prev_narrative_tail:
            messages.append({"role": "assistant", "content": prev_narrative_tail})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_narrative_prompt(
        self,
        ctx: dict,
        plot_decision: str,
        route: dict,
        state: dict,
        *,
        recent_openings: list[str] | None = None,
        history_context: str = "",
        prev_narrative_tail: str = "",
        prev_ending_type: str = "",
        authors_note: str = "",
        action_text: str = "",
        negative_prompt: str = "",
        logit_bias_hint: str = "",
        estimated_minutes: int = 30,
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """P2 合并叙事：将环境渲染 + 角色行为 + 叙事整合合为单次调用。

        返回 (messages, system)，供 generate_with_tools 使用。
        LLM 在生成叙事文本的同时可通过工具调用 set_atmosphere / set_scene_image。
        """
        scope = route.get("scope", "moderate")
        scene_type = route.get("scene_type", "")
        skip_env = scene_type in ("social", "rest") or scope == "minor"

        # --- system prompt: 叙事整合师 + 环境/角色要求 ---
        if scope == "minor":
            system = self._build_minor_narrative_system(negative_prompt, logit_bias_hint)
        else:
            system = self._build_standard_narrative_system(
                scope, scene_type, negative_prompt, logit_bias_hint
            )
            # 追加环境描写要求（原 build_env_render_prompt 的核心规则）
            if not skip_env:
                system += (
                    "\n\n## 环境描写要求\n"
                    "在叙事中自然融入环境/氛围描写，遵守以下规则：\n"
                    "- 覆盖2-3种感官（视觉、听觉、嗅觉、触觉、温度），"
                    "分散在不同段落中，不要在同一段内堆叠所有感官\n"
                    "- 与当前时段/天气/位置匹配\n"
                    "- 天气连续性（硬约束）：严格按提供的天气描写；"
                    "无天气字段时沿用上一轮氛围中的天气，不可自行发明天气变化\n"
                    "- 环境描写为剧情服务，不可违反物理常识和季节规律\n"
                    "- 场景锚定：以当前位置的物理特征为核心，窗外远景只能占1-2句\n"
                    "- 环境描写不超过总篇幅的30%"
                )
            # 追加角色行为要求（原 build_character_action_prompt 的核心规则）
            system += (
                "\n\n## 角色行为要求\n"
                "在叙事中自然融入角色对话和动作，遵守以下规则：\n"
                "- 用第二人称「你」描写主角，第三人称描写NPC\n"
                "- 对话使用中文弯引号包裹\n"
                "- 对话差异化：每个NPC的说话方式必须与其性格标签严格匹配。"
                "不同NPC用不同的句长、语气词和肢体语言区分\n"
                "- 如果有机制结果（骰子/检定/事件），角色行为必须与之一致\n"
                "- NPC对主角的称呼必须与主角的身份/头衔一致\n"
                "- NPC行为必须匹配其社会地位和权力层级\n"
                "- 只使用声纹速查表中列出的NPC，不得凭空捏造新角色\n"
                "- 道具来源：关键道具必须有合理来源\n"
                "- 时代约束：物品、通讯方式、交通工具必须与世界背景时代吻合\n"
                "- 对话禁用模式：禁止所有NPC都用长句书面语；"
                "禁止每句对话后跟微表情解读；真实对话短句为主"
            )

        system += CACHE_SENTINEL

        # --- user prompt 组装 ---

        # <scene_context> 区块（来自 narrative_compose + env_render）
        ctx_parts = []
        game_time = state.get("game_time", "")
        if game_time:
            try:
                from datetime import datetime as _dt
                _gt = _dt.fromisoformat(game_time.replace("Z", "+00:00"))
                _month = _gt.month
                _year = str(_gt.year)
                season_map = {1: "严冬", 2: "冬末", 3: "初春", 4: "春季",
                              5: "暮春", 6: "初夏", 7: "盛夏", 8: "夏末",
                              9: "初秋", 10: "深秋", 11: "深秋", 12: "冬季"}
                _season = season_map.get(_month, "")
                ctx_parts.append(
                    f"回合起始时间: {_year}年{_month}月（{_season}），"
                    f"若剧情骨架推进了时间则以骨架为准"
                )
            except Exception:
                pass
        time_of_day = self.format_game_time(game_time) if game_time else ""
        if time_of_day:
            ctx_parts.append(f"时段: {time_of_day}")
        if scope:
            time_hints = {
                "minor": "本回合是短暂行动，叙事时间跨度不超过30分钟",
                "moderate": "本回合是常规行动，叙事可覆盖30分钟到2小时",
                "major": "本回合是重大行动，叙事可覆盖数小时甚至一整天，需要自然的时间过渡",
            }
            if scope in time_hints:
                ctx_parts.append(f"时间节奏: {time_hints[scope]}")
        if estimated_minutes and estimated_minutes > 0:
            if estimated_minutes >= 60:
                h = estimated_minutes // 60
                m = estimated_minutes % 60
                dur_str = f"{h}小时" + (f"{m}分钟" if m else "")
            else:
                dur_str = f"{estimated_minutes}分钟"
            ctx_parts.append(
                f"玩家行动预期时长: 约{dur_str}。叙事中的时间流逝应与此一致，"
                "若玩家明确要求等待特定时长，叙事必须覆盖该完整时段"
            )

        # 位置信息（来自 env_render）
        location_id = state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(location_id, {})
        location_name = loc_def.get("name", location_id) if loc_def else location_id
        location_desc = loc_def.get("description", "") if loc_def else ""
        ctx_parts.append(f"位置: {location_name}")
        if location_desc:
            ctx_parts.append(f"位置描述: {location_desc[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            ctx_parts.append(f"玩家当前房间: {player_room}（叙事场景应以此房间为起点）")

        world_bg = self.script.get("world_background", "")
        if world_bg:
            ctx_parts.append(f"时代背景（地名/称谓须符合该时代）: {world_bg[:150]}")

        # 天气/季节（来自 env_render）
        weather = state.get("current_weather", "")
        if weather:
            ctx_parts.append(f"天气: {weather}")
        elif not skip_env:
            ctx_parts.append("天气: 未指定（沿用上一轮氛围中的天气，不可自行发明新天气）")
        if game_time:
            try:
                month = int(game_time[5:7]) if len(game_time) >= 7 else 0
            except (ValueError, TypeError):
                month = 0
            season_map2 = {1: "冬季", 2: "冬季", 3: "初春", 4: "春季", 5: "春季",
                           6: "初夏", 7: "夏季", 8: "夏季", 9: "初秋", 10: "秋季",
                           11: "深秋", 12: "冬季"}
            season2 = season_map2.get(month, "")
            year = game_time[:4] if len(game_time) >= 4 else ""
            if season2:
                date_label = f"{year}年{month}月 {season2}" if year else season2
                ctx_parts.append(
                    f"当前年月与季节: {date_label}（硬约束：天气、植被、气温必须符合该月份）"
                )
        inventory = state.get("inventory", [])
        if inventory:
            inv_str = ", ".join(it.get("item", "?") for it in inventory[:8])
            ctx_parts.append(f"玩家携带物品: {inv_str}")

        # <naming_reference> 区块（来自 narrative_compose）
        pc_info = state.get("player", {})
        pc_name = pc_info.get("name", "")
        pc_title = pc_info.get("title", "") or pc_info.get("role", "") or pc_info.get("occupation", "")
        title_pairs = []
        if pc_name:
            pc_label = f"★主角(第二人称\"你\"): {pc_name}"
            if pc_title:
                pc_label += f"（{pc_title}）"
            title_pairs.append(pc_label)
        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for npc_id, ns in npc_states.items():
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            npc_def = npc_defs.get(npc_id, {})
            title = (
                ns.get("title", "")
                or npc_def.get("title", "")
                or npc_def.get("role", "")
                or npc_def.get("occupation", "")
            )
            if title:
                title_pairs.append(f"{name}（{title}）")
            elif name != npc_id:
                title_pairs.append(name)
        naming_text = ""
        if title_pairs:
            naming_text = (
                f"叙事中必须使用正确称谓，NPC称呼主角时必须用主角的姓氏，不可混用其他NPC的姓氏:\n"
                f"{'; '.join(title_pairs)}"
            )

        # <protagonist> 区块（来自 character_action）
        player = state.get("player", {})
        player_name = player.get("name", "主角")
        pc = self.script.get("player_character", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        if not has_pc_lore:
            player_personality = player.get("personality", "") or pc.get("personality", "")
            player_identity = pc.get("identity", "") or pc.get("background", "") or pc.get("bio", "")
        else:
            player_personality = ""
            player_identity = ""

        proto_parts = []
        proto_parts.append(f"主角: {player_name}")
        player_title = player.get("title", "") or pc.get("title", "")
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_example = f"「{_surname}{_short_title}」" if _surname else f"「{_short_title}」"
            proto_parts.append(f"头衔/职位: {player_title}（NPC称呼主角为{_call_example}）")
        elif player_name and player_name != "主角":
            proto_parts.append(f"（NPC称呼主角时应使用姓氏「{player_name[0]}」加适当敬称）")
        if player_personality:
            proto_parts.append(f"性格: {player_personality}")
        if player_identity:
            proto_parts.append(f"身份: {player_identity}")

        # <npc_profiles> 区块（来自 character_action）
        present_npc_ids = ctx.get("present_npc_ids") or []
        voice_table = self._build_npc_voice_table(state, present_npc_ids)
        npc_profile_parts = []
        if voice_table:
            npc_profile_parts.append(voice_table)
        # NPC交互记录
        chat_histories = state.get("npc_chat_history", {})
        interaction_info = []
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            met = ns.get("met", False)
            chat_count = len(chat_histories.get(npc_id, []))
            if met or chat_count > 0:
                interaction_info.append(f"{name}已交互过（勿重复自我介绍）")
        if interaction_info:
            npc_profile_parts.append(f"交互记录: {'; '.join(interaction_info)}")
        npc_rooms = []
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if isinstance(ns, dict):
                room = ns.get("current_room", "")
                if room:
                    npc_rooms.append(f"{ns.get('name', npc_id)} → {room}")
        if npc_rooms:
            npc_profile_parts.append(f"NPC房间位置: {'; '.join(npc_rooms)}")

        # <mechanics> 区块（来自 character_action）
        mechanics = self._format_mechanics_brief(
            ctx.get("dice_dicts"),
            ctx.get("check_result"),
            ctx.get("triggered_events"),
            ctx.get("triggered_consequences"),
        )

        # <continuity> 区块（来自 env_render + narrative_compose）
        cont_parts = []
        if history_context:
            cont_parts.append(f"前情提要:\n{history_context}")
        if scope != "minor" and recent_openings:
            cont_parts.append(f"近几轮叙事开头（请避免雷同）: {' / '.join(recent_openings[-3:])}")
        if scope != "minor" and prev_ending_type:
            cont_parts.append(f"上一轮结尾类型: {prev_ending_type}（本轮必须使用不同类型）")
        pending = state.get("scene_details", {}).get("pending_tension", "")
        if pending:
            cont_parts.append(f"待定伏线（适度铺垫）: {pending}")
        # 上一轮氛围（来自 env_render）
        if not skip_env:
            scene_details = state.get("scene_details", {})
            prev_atmosphere = scene_details.get("atmosphere", "")
            prev_sensory = scene_details.get("sensory", "")
            if prev_atmosphere or prev_sensory:
                cont_parts.append(f"上一轮氛围（仅供天气/光线连续参考）: {prev_atmosphere} {prev_sensory}")
            loc_mem = state.get("location_memory", {}).get(location_id, [])
            if loc_mem:
                mem_lines = [f"- 第{m['turn']}回合: {m['text']}" for m in loc_mem[-3:]]
                cont_parts.append("此地的历史事件:\n" + "\n".join(mem_lines))
        _story_hint = self._build_stage2_story_hint(state)
        if _story_hint:
            cont_parts.append(_story_hint)

        # 组装 XML
        sections = [
            _xml("scene_context", "\n".join(ctx_parts)),
            _xml("naming_reference", naming_text),
            _xml("protagonist", "\n".join(proto_parts)),
            _xml("npc_profiles", "\n".join(npc_profile_parts)),
            _xml("mechanics", f"角色行为必须与之一致:\n{mechanics}" if mechanics else ""),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("plot_decision", plot_decision),
            _xml("player_action", action_text if action_text else ""),
            _xml("authors_note", authors_note if authors_note else ""),
        ]
        content = "\n\n".join(s for s in sections if s)

        messages = []
        if prev_narrative_tail:
            messages.append({"role": "assistant", "content": prev_narrative_tail})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_continue_prompt(
        self, current_narrative: str, state: dict,
    ) -> tuple[list[dict], str]:
        """Build prompt to continue/extend the current narrative without a new turn."""
        system = (
            "你是文字游戏的叙事续写师。玩家觉得上一段叙事太短或意犹未尽，请你自然地接续。\n\n"
            "规则：\n"
            "- 从上一段叙事末尾处无缝衔接，不要重复已有内容\n"
            "- 保持人称、时态、风格一致\n"
            "- 可以展开细节、补充感官描写、深化角色互动\n"
            "- 不要引入新的重大剧情转折或新角色\n"
            "- 输出300-600字的续写文本\n"
            "- 直接输出续写文本，不要任何JSON/标记/解释"
        )
        loc = state.get("player", {}).get("location", "未知")
        content = (
            f"当前场景: {loc}\n\n"
            f"== 需要续写的叙事 ==\n{current_narrative}\n\n"
            "请从上文末尾处自然续写，展开更多细节。"
        )
        messages = [{"role": "user", "content": content}]
        return messages, system

    @staticmethod
    def format_turn_player_inputs(inputs: list[dict] | None) -> str:
        if not inputs:
            return ""
        return (
            "以下是本次行动中已经收到的玩家补充，按发生顺序解释；重生成时仍保留这些意图。"
            "plan_reply_context 仅用于按原问题解释回复，不代表玩家同意整个计划或未确认部分。"
            "已明确回答的原问题视为已解决；在已确认范围内继续执行，不要重复要求同一确认。"
            "后来的明确修改优先；旧回复不是对新问题的回答，新问题仍需等待新确认。\n"
            + _xml("turn_player_inputs", json.dumps(inputs, ensure_ascii=False))
        )

    def build_narrative_review_prompt(
        self, narrative: str, action_text: str,
        present_npcs: list[dict],
        pc_name: str = "",
        private_script: list[dict] | None = None,
        known_context: str = "",
        plan_reply_context: dict | None = None,
        turn_player_inputs: list[dict] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 3.5: 叙事质量评审 prompt。"""
        loader = PromptLoader.get()
        system = loader.render_system("stage_review")
        if pc_name:
            pc_surname = pc_name[0] if pc_name else ""
            system += (
                f'\n10. wrong_name: NPC在对话或称呼中是否叫错了主角的姓名？'
                f'主角姓名是「{pc_name}」（姓{pc_surname}），'
                f'NPC称呼主角时必须使用正确姓氏「{pc_surname}」。'
                f'若叙事中NPC用其他姓氏+职务来称呼主角（如用在场其他NPC的姓氏），算违规。'
            )
        npc_info = ""
        if present_npcs:
            npc_lines = []
            for npc in present_npcs[:6]:
                role = npc.get("title") or npc.get("role") or npc.get("occupation") or ""
                line = f"- {npc.get('name', npc.get('id', '?'))}"
                if role:
                    line += f"（{role}）"
                npc_lines.append(line)
            npc_info = "\n".join(npc_lines)

        sections = [
            _xml("player_action", action_text),
            self.format_turn_player_inputs(turn_player_inputs),
            _xml("narrative", narrative),
            _xml("npcs_present", npc_info),
            _xml("protagonist_name", pc_name if pc_name else ""),
        ]
        if plan_reply_context:
            sections.append(_xml("plan_reply_context", json.dumps(plan_reply_context, ensure_ascii=False)))
            system += (
                "\nplan_reply_context 是玩家回复时的计划、问题、选项与回复原文，仅供解释玩家行动。"
                "按原问题理解序号等简短回复；不要把整个计划、未确认部分或默认选项当作玩家已确认。"
                "已明确确认的执行范围不应再被退回等待同一确认；若规则不允许执行，应说明实际阻碍，而非假定玩家尚未回答。"
            )
        if private_script:
            sections.append(_xml("active_script_private", json.dumps(private_script, ensure_ascii=False)))
            sections.append(_xml("recent_player_visible", known_context))
        content = "\n\n".join(s for s in sections if s)
        return [{"role": "user", "content": content}], system
