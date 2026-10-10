"""PromptBuilder Mixin: 提示词各Section构建"""
from __future__ import annotations
from typing import TYPE_CHECKING

from engine.event_scheduler import parse_time

if TYPE_CHECKING:
    from engine.lorebook import LorebookEntry

# Duplicated from prompt_builder to avoid circular import
CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"
_CACHE_STABLE_KEYS = frozenset({"role", "world"})


class PromptSectionsMixin:
    """系统提示词各段落（section）的构建方法"""

    def _build_narrative_role_section(self) -> str:
        """叙事专用 role section — 只含写作规则，不含 JSON 格式定义。"""
        if self._cached_narrative_role_section:
            return self._cached_narrative_role_section
        result = """你是一个交互式文字游戏的游戏主持人（GM）。你需要根据以下世界设定和当前状态，为玩家描述场景、推进剧情。

【重要】直接输出叙事内容，不要输出任何JSON、结构化数据、代码块或选项列表。系统会单独处理选项和状态变更。

用第二人称（"你"）描述场景和事件，注重细节、氛围和角色互动，充分展开叙述（800-1200字）。

规则：
- 所有角色（包括主角和NPC）的言行举止必须贴合其性格设定。活泼的角色要表现活泼，温柔的要温柔，严肃的才严肃。不要让所有角色都用同一种冷静严肃的语气
- 主角的内心独白、反应和行动方式要体现其性格特征
- 角色对话必须使用中文弯引号 “……” 包裹（直接说出口的话），内心独白和心理活动可不加引号或使用『……』。这是渲染层识别对话的依据，不要省略引号也不要使用裸文本对白
- 如果本回合有骰子结果，必须在叙事中体现骰子的影响。高骰值=正面结果，低骰值=负面结果
- NPC不是被动存在的。根据NPC的性格和对主角的态度主动表现行为——好感高的NPC主动帮助/友好，好感低的会刁难/冷淡
- 如果系统提供了技能检定结果，必须在叙事中体现。成功=行动达成，失败=行动受阻。但禁止在叙事中出现具体数值（如"87的政治嗅觉""d100=73"），数值由系统UI单独展示，用角色的感受和行为自然体现结果
- 信息边界：严格遵守角色的认知边界。叙事只能包含主角当前能合理知道的信息。主角不知道的事件（他人的秘密计划、未来将发生的事）不得暗示或透露。不要用全知视角写作
- 角色描写要含蓄自然，通过行为和细节暗示性格，不要直接点明角色在"演绎"或"扮演"某种形象
- 天气影响叙事氛围和行动难度
- 如果玩家的自由输入不合理，在叙事中巧妙解释为什么没有成功
- 保持叙事与世界观一致
- 如果系统提示了"延迟后果"或"待定伏线"，在叙事中适度铺垫
- 某些NPC初始不认识主角(known=false)，首次互动时以自然方式描述相识过程
- 主角台词边界：你可以描写主角的动作、表情、内心感受，但不可替主角说出具体台词。若场景需要主角回应，用动作暗示（"你点了点头""你应了一声"）或省略号代替（"你说了句……"）。只有玩家在行动中明确写出的话才能作为主角台词出现在叙事中
- 禁止输出任何思考过程（如"Let me think..."等）。如果你需要思考，请使用<think>标签包裹"""
        self._cached_narrative_role_section = result
        return result

    def build_narrative_system_prompt(
        self,
        state: dict,
        activated_lore: list[LorebookEntry] | None = None,
        authors_note: str = "",
        turn_number: int = 0,
        authors_note_position: str = "end",
        macro_expander=None,
        negative_prompt: str = "",
        event_sections: dict[str, str] | None = None,
        pc_discovered_lore: list[str] | None = None,
    ) -> str:
        """Build system prompt for narrative-only generation.

        turn > 1 时精简：跳过世界背景全文（历史上下文已隐含）。
        组织信息通过 lorebook 按需注入。
        """
        section_map = {
            "role": self._build_narrative_role_section,
            "world": (
                (lambda: self._build_world_brief_section(state))
                if turn_number > 1
                else (lambda: self._build_world_section(state))
            ),
            "character": lambda: self._build_character_section(state),
            "npc": lambda: self._build_npc_section(state),
            "states": lambda: self._build_active_states_section(state),
            "story_context": lambda: self._build_story_context_section(state),
            "narrative_callbacks": lambda: self._build_callback_hints_section(state, turn_number),
            "tone": lambda: self._build_tone_section(state),
            "pacing": lambda: self._build_pacing_section(state),
            "moral_alignment": lambda: self._build_moral_alignment_section(state),
            "faction_reputation": lambda: self._build_faction_reputation_section(state),
            "available_quests": lambda: self._build_available_quests_section(state),
            "memory_echo": lambda: self._build_memory_echo_section(state),
            "skill_growth": lambda: self._build_skill_growth_section(state),
            "time_atmosphere": lambda: self._build_time_atmosphere_section(state),
            "difficulty_awareness": lambda: self._build_difficulty_section(state),
            "npc_relationship_depth": lambda: self._build_npc_relationship_depth_section(state),
            "discovery_hints": lambda: self._build_discovery_hints_section(state),
            "lorebook_after_world": lambda: self._build_lorebook_section(
                activated_lore, "after_world",
                pc_discovered_lore=pc_discovered_lore, scope="pc",
                visibility_markers=True,
            ),
            "lorebook_at_end": lambda: self._build_lorebook_section(
                activated_lore, "at_end",
                pc_discovered_lore=pc_discovered_lore, scope="pc",
                visibility_markers=True,
            ),
            "authors_note": lambda: self._build_authors_note_section(authors_note) if authors_note_position == "end" else "",
        }

        order = self.script.get("settings", {}).get(
            "prompt_order", self._default_order
        )
        sep = "\n\n---\n\n"
        stable_sections = []
        dynamic_sections = []
        in_stable = True
        for key in order:
            builder = section_map.get(key)
            if not builder:
                continue
            text = builder() if callable(builder) else builder
            if not text:
                continue
            if in_stable and key in _CACHE_STABLE_KEYS:
                stable_sections.append(text)
            else:
                in_stable = False
                dynamic_sections.append(text)

        # Opening context — turn <= 5 时注入；之后已沉淀到历史上下文中，节省 token
        if turn_number <= 5:
            opening_ctx = state.get("opening_context", "")
            if opening_ctx:
                stable_sections.append(f"## 开局设定\n{opening_ctx}")

        # 玩家风格引导叙事节奏
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            style_desc = play_style.get("description", "")
            style_text = f"## 玩家风格\n{play_style['tag']}"
            if style_desc:
                style_text += f"（{style_desc}）"
            style_text += "\n请适当调整叙事节奏和侧重以贴合此风格。"
            dynamic_sections.append(style_text)

        result = sep.join(stable_sections)
        if dynamic_sections:
            result += CACHE_SENTINEL + sep.join(dynamic_sections)

        if negative_prompt:
            result += "\n\n---\n\n## 绝对禁止事项\n以下内容绝对不允许出现在叙事中：\n" + negative_prompt

        if macro_expander:
            result = macro_expander(result)

        return result

    def _build_world_brief_section(self, state: dict) -> str:
        """精简版世界 section（turn > 1 时使用）— 只保留已知地点列表。"""
        raw_location = state.get("player", {}).get("location", "未知")
        location = self._resolve_location_name(raw_location)
        visible_ids = set(state.get("visible_locations", []))
        locations = self.script.get("locations", [])
        loc_descriptions = []
        for loc in locations:
            if loc["id"] in visible_ids:
                if self._has_kg:
                    desc = f"- {loc.get('name', loc['id'])}({loc['id']})"
                else:
                    desc = f"- {loc.get('name', loc['id'])}: {loc.get('description', '')}"
                travel = loc.get("travel_time")
                if travel and loc["id"] != raw_location:
                    desc += f" (移动需{travel})"
                if loc.get("access_condition"):
                    desc += f" [条件: {loc['access_condition']}]"
                loc_descriptions.append(desc)

        locs_text = "\n".join(loc_descriptions) if loc_descriptions else "暂无已知地点"
        return f"## 已知地点\n{locs_text}"

    def _build_world_section(self, state: dict) -> str:
        bg = self.script.get("world_background", "")
        raw_location = state.get("player", {}).get("location", "未知")
        location = self._resolve_location_name(raw_location)

        # Build visible locations list with travel info
        visible_ids = set(state.get("visible_locations", []))
        locations = self.script.get("locations", [])
        loc_descriptions = []
        for loc in locations:
            if loc["id"] in visible_ids:
                if self._has_kg:
                    desc = f"- {loc.get('name', loc['id'])}({loc['id']})"
                else:
                    desc = f"- {loc.get('name', loc['id'])}: {loc.get('description', '')}"
                travel = loc.get("travel_time")
                if travel and loc["id"] != raw_location:
                    desc += f" (移动需{travel})"
                if loc.get("access_condition"):
                    desc += f" [条件: {loc['access_condition']}]"
                loc_descriptions.append(desc)

        locs_text = "\n".join(loc_descriptions) if loc_descriptions else "暂无已知地点"

        return f"""## 世界背景
{bg}

## 当前位置
{location}

## 已知地点
{locs_text}
注意：`end_time` 是叙事结束时刻；如已抵达新地点，必须包含旅途耗时，不会再额外叠加 travel_time。"""

    def _build_character_section(self, state: dict) -> str:
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        rels = player.get("relationships", {})

        # Get max values from script rules
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})
        rel_rules = pc.get("relationships", {})

        dn = state.get("display_names", {})
        attrs_lines = []
        for k, v in attrs.items():
            rule = attr_rules.get(k, {})
            mx = rule.get("max", 100) if isinstance(rule, dict) else 100
            label = dn.get(k, k)
            attrs_lines.append(f"- {label}({k}): {v}/{mx}")
        attrs_text = "\n".join(attrs_lines)

        rels_lines = []
        for rel_id, val in rels.items():
            npc_name = self._get_npc_name(rel_id)
            if isinstance(val, dict) and any(k in val for k in ("trust", "affection", "fear")):
                t = val.get("trust", 50)
                a = val.get("affection", 50)
                f = val.get("fear", 0)
                rels_lines.append(f"- {npc_name}: 信任{t}/好感{a}/畏惧{f}")
            else:
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                rule = rel_rules.get(rel_id, {})
                mx = rule.get("max", 100) if isinstance(rule, dict) else 100
                rels_lines.append(f"- {npc_name}: {v}/{mx}")
        rels_text = "\n".join(rels_lines)

        # Inventory
        inventory = state.get("inventory", [])
        if inventory:
            inv_lines = [f"- {it['item']} x{it.get('quantity', 1)}" for it in inventory]
            inv_text = "\n".join(inv_lines)
        else:
            inv_text = "- 无"

        personality = player.get('personality', '')
        personality_line = f"\n- 性格: {personality}" if personality else ""

        # Class/profession display
        class_section = ""
        class_name = player.get("class_name")
        if class_name:
            level = player.get("level", 1)
            lines_cls = [f"\n## 职业与技能", f"- 职业: {class_name} (Lv.{level})"]
            skills = player.get("skills", {})
            if skills:
                proficient = [f"{s.get('name', sid)}" for sid, s in skills.items()
                              if isinstance(s, dict) and s.get("proficient")]
                if proficient:
                    lines_cls.append(f"- 熟练技能: {', '.join(proficient)}")
            tags = player.get("narrative_tags", [])
            if tags:
                lines_cls.append(f"- 特性: {', '.join(tags)}")
            abilities = player.get("abilities", [])
            if abilities:
                lines_cls.append(f"- 能力: {', '.join(abilities)}")
            class_section = "\n".join(lines_cls)

        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )

        lines = ["## 主角状态", f"- 姓名: {player.get('name', '未知')}"]
        if not has_pc_lore:
            lines.append(f"- 身份: {player.get('bio', '未知')}{personality_line}")
        if attrs_text:
            lines.append(attrs_text)
        lines.append(f"- 当前位置: {self._resolve_location_name(player.get('location', '未知'))}")
        if not has_pc_lore:
            lines.append(f"- 长期目标: {player.get('long_term_goal', '无')}")

        return "\n".join(lines) + f"""

## 背包
{inv_text}

## 人际关系
{rels_text}{class_section}"""

    def _build_npc_section(self, state: dict) -> str:
        lines = ["## 重要NPC"]
        npc_states = state.get("npcs", {})
        player_loc = state.get("player", {}).get("location", "")
        game_time = state.get("game_time", "")
        encounter_counts = state.get("npc_encounter_counts", {}) or {}
        dialogue_counts = state.get("npc_dialogue_counts", {}) or {}

        # Determine time of day from game_time
        time_period = ""
        if game_time:
            try:
                t = parse_time(game_time)
                if t:
                    hour = t.hour
                    if 6 <= hour < 12:
                        time_period = "上午"
                    elif 12 <= hour < 14:
                        time_period = "中午"
                    elif 14 <= hour < 18:
                        time_period = "下午"
                    elif 18 <= hour < 22:
                        time_period = "晚上"
                    elif 22 <= hour or hour < 2:
                        time_period = "深夜"
                    else:
                        time_period = "凌晨"
            except Exception:
                pass

        # --- 分层：将 NPC 分为在场 / 近期相关 / 其余 ---
        recent_npc_ids = set()
        offscreen_log = state.get("npc_offscreen_log", {})
        recent_npc_ids.update(offscreen_log.keys())
        for npc_id in encounter_counts:
            if encounter_counts[npc_id] > 0:
                recent_npc_ids.add(npc_id)

        present_npcs = []
        recent_npcs = []
        background_npcs = []

        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            if npc_id not in self._initial_npc_ids and npc_id not in npc_states:
                continue
            npc_loc = self._resolve_npc_location(npc_id, state, game_time)
            is_present = npc_loc and player_loc and npc_loc.lower() == player_loc.lower()
            if is_present:
                present_npcs.append((npc, npc_loc, True))
            elif npc_id in recent_npc_ids:
                recent_npcs.append((npc, npc_loc, False))
            else:
                background_npcs.append((npc, npc_loc, False))

        # --- 在场 NPC：完整信息 ---
        if present_npcs:
            lines.append("\n### 当前在场（可直接互动）")
        for npc, npc_loc, _ in present_npcs:
            lines.append(self._format_npc_detail(
                npc, state, npc_states, player_loc, game_time,
                encounter_counts, dialogue_counts, is_present=True,
            ))

        # --- 近期相关 NPC：中等信息 ---
        if recent_npcs:
            lines.append("\n### 近期相关（不在场）")
        for npc, npc_loc, _ in recent_npcs:
            lines.append(self._format_npc_brief(
                npc, state, npc_states, npc_loc, encounter_counts, dialogue_counts,
            ))

        # --- 其余 NPC：有知识库时不注入，无知识库时极简一行 ---
        if background_npcs:
            if self._has_kg:
                names = [npc.get("name", npc["id"]) for npc, _, _ in background_npcs]
                lines.append(f"\n### 其他NPC（详情通过知识图谱按需注入）")
                lines.append(f"- {', '.join(names)}")
            else:
                lines.append("\n### 其他NPC")
                for npc, npc_loc, _ in background_npcs:
                    lines.append(self._format_npc_minimal(npc, npc_states, npc_loc))

        # NPC-NPC relationship network — skip when lorebook has relationship entries
        has_rel_lore = self.lorebook and any(
            e.id.startswith("_npc_rel_") and e.enabled for e in self.lorebook.entries
        )
        if not self._has_kg and not has_rel_lore:
            npc_rels_global = state.get("npc_relationships_global", {})
            npc_rels_known = state.get("npc_relationships_known", {})
            known_keys = set(npc_rels_known.keys()) if npc_rels_known else set()
            if npc_rels_global and isinstance(npc_rels_global, dict):
                def _fmt_rel(rel, compact=False, *, is_known=True):
                    tag = "" if is_known else " [玩家未知·仅供行为逻辑参考]"
                    met_tag = ""
                    if rel.get("initially_met") is False or rel.get("met") is False:
                        met_tag = " [未接触]"
                    elif rel.get("initially_known") is False or rel.get("known") is False:
                        met_tag = " [不认识]"
                    if "from" in rel:
                        fn = self._get_npc_name(rel["from"])
                        tn = self._get_npc_name(rel["to"])
                        t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                        desc = rel.get("description", "")
                        if compact:
                            return f"{fn}→{tn}:信{t}/感{a}/畏{f}" + (f"({desc[:15]})" if desc else "") + met_tag + tag
                        return f"- {fn} → {tn}: 信任{t}/好感{a}/畏惧{f}" + (f" ({desc})" if desc else "") + met_tag + tag
                    else:
                        an = self._get_npc_name(rel.get("a", ""))
                        bn = self._get_npc_name(rel.get("b", ""))
                        rel_type = rel.get("type", "中立")
                        desc = rel.get("description", "")
                        if compact:
                            return f"{an}↔{bn}:{rel_type}" + (f"({desc[:15]})" if desc else "") + met_tag + tag
                        return f"- {an} ↔ {bn}: {rel_type}" + (f" ({desc})" if desc else "") + met_tag + tag

                if len(npc_rels_global) > 20:
                    parts = [_fmt_rel(rel, compact=True, is_known=k in known_keys) for k, rel in npc_rels_global.items()]
                    lines.append(f"\n### NPC间关系网 (共{len(npc_rels_global)}条)")
                    lines.append("标注[玩家未知]的关系：NPC行为可受其影响，但叙事中不得让主角直接知晓")
                    lines.append(" | ".join(parts))
                else:
                    lines.append("\n### NPC间关系网")
                    lines.append("标注[玩家未知]的关系：NPC行为可受其影响，但叙事中不得让主角直接知晓")
                    for k, rel in npc_rels_global.items():
                        lines.append(_fmt_rel(rel, is_known=k in known_keys))

        # NPC offscreen log
        if offscreen_log:
            lines.append("\n### NPC近期动态（离场行动）")
            for npc_id, entries in offscreen_log.items():
                if entries:
                    latest = entries[-1]
                    npc_name = self._get_npc_name(npc_id)
                    lines.append(f"- {npc_name}: {latest.get('action', '?')} (位于{latest.get('location', '?')})")

        return "\n".join(lines)

    def _npc_three_dim_suffix(self, npc_id: str, state: dict) -> str:
        rel_data = state.get("player", {}).get("relationships", {}).get(npc_id, {})
        parts = []
        if isinstance(rel_data, dict):
            for dim_key, dim_label in (("trust", "信任"), ("affection", "好感"), ("fear", "畏惧")):
                v = rel_data.get(dim_key)
                if isinstance(v, (int, float)):
                    parts.append(f"{dim_label} {int(v)}")
        return f" [{' / '.join(parts)}]" if parts else ""

    def _npc_behavior_hint(self, attitude: int) -> str:
        if attitude >= 80:
            return "忠诚/热情——会主动帮助、分享秘密、提供资源"
        elif attitude >= 60:
            return "友好——乐于交谈、可能提供帮助"
        elif attitude >= 40:
            return "中立——公事公办、不会主动帮忙也不刁难"
        elif attitude >= 20:
            return "冷淡/警惕——不愿交流、可能设置小障碍"
        return "敌对——会刁难、阻挠、甚至攻击"

    def _format_org_tags(self, npc_orgs: list[dict], *, sep: str = "、", compact: bool = False) -> str:
        """从 NPC organizations 数组构建组织标签字符串。"""
        org_by_id = {o["id"]: o for o in self.script.get("organizations", []) if o.get("id")}
        tags = []
        for om in npc_orgs:
            oid = om.get("org_id", "")
            org_obj = org_by_id.get(oid, {})
            org_name = org_obj.get("name", oid)
            npc_rank = om.get("rank")
            rank_title = None
            if npc_rank is not None:
                for h in org_obj.get("hierarchy", []):
                    if h.get("rank") == npc_rank:
                        rank_title = h.get("title")
                        break
            if compact:
                tags.append(f"{org_name}·{rank_title}" if rank_title else org_name)
            else:
                role = om.get("role", "")
                label = org_name
                if rank_title:
                    label += f"（{rank_title}）"
                if role:
                    label += f"[{role}]"
                tags.append(label)
        return sep.join(tags)

    def _format_npc_detail(self, npc, state, npc_states, player_loc, game_time,
                           encounter_counts, dialogue_counts, *, is_present):
        """在场 NPC 的完整信息。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        known = npc_st.get("known", npc.get("known", True))
        encounter_count = encounter_counts.get(npc_id, 0)
        three_dim = self._npc_three_dim_suffix(npc_id, state)
        behavior = self._npc_behavior_hint(attitude)
        att_trigger = ""
        if attitude >= 90:
            att_trigger = " [好感极高：可触发特殊剧情/告白/结盟]"
        elif attitude <= 10:
            att_trigger = " [好感极低：可能主动攻击/背叛]"

        has_npc_lore = self.lorebook and any(
            e.id == f"_npc_profile_{npc_id}" and e.enabled for e in self.lorebook.entries
        )

        if has_npc_lore or self._has_kg:
            npc_name = npc_st.get('name') or npc.get('name', npc_id)
            detail = f"\n- **{npc_name}**: 态度 {attitude}/100{three_dim} → {behavior}{att_trigger}"
            if not has_npc_lore:
                npc_personality = npc_st.get("personality") or npc.get("personality", "")
                if npc_personality:
                    detail = f"\n- **{npc_name}**: 性格:{npc_personality} 态度 {attitude}/100{three_dim} → {behavior}{att_trigger}"
            npc_orgs = npc_st.get("organizations") or npc.get("organizations", [])
            if npc_orgs:
                detail += f" | {self._format_org_tags(npc_orgs, compact=True)}"
        else:
            npc_bio = npc_st.get("bio") or npc.get("bio", "")
            npc_personality = npc_st.get("personality") or npc.get("personality", "")
            npc_caps = npc_st.get("capabilities") or npc.get("capabilities", "未知")
            detail = (
                f"\n### {npc_st.get('name') or npc.get('name', npc_id)}\n"
                f"- 简介: {npc_bio}\n"
                f"- 性格: {npc_personality}\n"
                f"- 能力: {npc_caps}\n"
                f"- 对主角态度: {attitude}/100{three_dim} → {behavior}{att_trigger}"
            )
            npc_title = npc_st.get("title") or npc.get("title", "")
            if npc_title:
                detail += f"\n- 头衔: {npc_title}"
            npc_orgs = npc_st.get("organizations") or npc.get("organizations", [])
            if npc_orgs:
                detail += f"\n- 所属组织: {self._format_org_tags(npc_orgs)}"
            npc_sup = npc_st.get("superior") or npc.get("superior", "")
            if npc_sup:
                sup_name = self._get_npc_name(npc_sup)
                detail += f"\n- 上级: {sup_name}"

        # NPC 自主目标
        completed_goals = set(state.get("completed_npc_goals", []))
        npc_goals = npc.get("goals", [])
        active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
        if active_goals:
            goal_parts = []
            for g in active_goals[:2]:
                gtext = g.get("description", "")
                conflict = g.get("conflict_with_player", "")
                if conflict:
                    gtext += f"（{conflict}）"
                goal_parts.append(gtext)
            detail += f"\n  动机/目标: {'; '.join(goal_parts)}"

        # NPC 已知信息（信息传播网络）
        info_network = state.get("information_network", [])
        npc_known_info = [i for i in info_network if npc_id in i.get("known_by", [])]
        if npc_known_info:
            info_lines = []
            for info in npc_known_info[-3:]:
                fact = info.get("fact", "")
                if info.get("distortion", 0) >= 2:
                    info_lines.append(f"[谣言/失真] {fact}")
                else:
                    info_lines.append(fact)
            detail += f"\n  已知信息: {'; '.join(info_lines)}"

        # 交互历史标注
        if not known and encounter_count == 0:
            detail += " ★未认识主角（尚未碰面）"
        elif encounter_count > 0:
            dlg = dialogue_counts.get(npc_id, 0)
            detail += f" [已交互{encounter_count}回合"
            if dlg:
                detail += f"+{dlg}次专属对话"
            detail += "，已熟识，勿当首次出场介绍]"

        detail += " ★在场"
        return detail

    def _format_npc_brief(self, npc, state, npc_states, npc_loc, encounter_counts, dialogue_counts):
        """近期相关但不在场的 NPC：中等信息。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        three_dim = self._npc_three_dim_suffix(npc_id, state)
        encounter_count = encounter_counts.get(npc_id, 0)
        dn = state.get("display_names", {})
        loc_display = dn.get(npc_loc, npc_loc) if npc_loc else "未知"
        detail = f"- {npc_st.get('name') or npc.get('name', npc_id)}: 态度{attitude}/100{three_dim} (在{loc_display})"
        if encounter_count > 0:
            dlg = dialogue_counts.get(npc_id, 0)
            detail += f" [已交互{encounter_count}回合"
            if dlg:
                detail += f"+{dlg}对话"
            detail += "]"
        # 不在场 NPC 已知信息
        info_network = state.get("information_network", [])
        npc_known_info = [i for i in info_network if npc_id in i.get("known_by", [])]
        if npc_known_info:
            latest = npc_known_info[-1]
            fact = latest.get("fact", "")
            tag = "[谣言]" if latest.get("distortion", 0) >= 2 else ""
            detail += f" 已知:{tag}{fact[:40]}"
        detail += " ★不在场"
        return detail

    @staticmethod
    def _format_npc_minimal(npc, npc_states, npc_loc):
        """无知识库时其余 NPC 的极简一行。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        loc_text = f" (在{npc_loc})" if npc_loc else ""
        return f"- {npc_st.get('name') or npc.get('name', npc_id)}: 态度{attitude}{loc_text}"

    def _build_active_states_section(self, state: dict) -> str:
        lines = ["## 当前持续状态"]
        active_ids = set(state.get("active_persistent_states", []))
        for ps in self.script.get("persistent_states", []):
            if ps["id"] in active_ids:
                lines.append(f"- {ps['description']}")

        # Day/night cycle effects
        game_time = state.get("game_time", "")
        if game_time:
            try:
                t = parse_time(game_time)
                if t:
                    hour = t.hour
                    if 6 <= hour < 12:
                        period = "上午"
                        period_effect = "大部分商店已开门，NPC正常活动"
                    elif 12 <= hour < 14:
                        period = "中午"
                        period_effect = "正午时分，部分人在休息"
                    elif 14 <= hour < 18:
                        period = "下午"
                        period_effect = "NPC正常活动，日照充足"
                    elif 18 <= hour < 22:
                        period = "晚上"
                        period_effect = "天色渐暗，商店陆续关门，酒馆/夜市活跃"
                    elif 22 <= hour or hour < 2:
                        period = "深夜"
                        period_effect = "大部分NPC已入睡，商店关闭，能见度低，危险增加"
                    else:
                        period = "凌晨"
                        period_effect = "天色最暗，几乎无人活动，适合隐秘行动"
                    lines.append(f"\n## 时段\n当前时段: {period} ({self.format_game_time(game_time)})")
                    lines.append(f"时段影响: {period_effect}")
            except Exception:
                pass

        # Weather with gameplay effects
        weather = state.get("current_weather")
        if weather:
            weather_effects = {
                "暴雨": "户外行动困难，视野受限，NPC减少外出",
                "大雪": "道路难行，移动时间翻倍，注意保暖",
                "极端天气（暴雨/大雪）": "户外行动极为困难，移动时间翻倍，有受伤风险",
                "阴雨": "氛围阴郁，户外略有不便",
                "多云": "天气一般",
                "晴朗": "天气晴好，适合活动",
            }
            effect = weather_effects.get(weather, "")
            lines.append(f"\n## 当前天气\n{weather}")
            if effect:
                lines.append(f"天气影响: {effect}")

        # Event countdowns (time pressure)
        self._append_event_countdowns(lines, state)

        # World properties
        world_props = state.get("world_properties", {})
        if world_props:
            lines.append("\n## 世界属性")
            for prop in self.script.get("world_properties", []):
                val = world_props.get(prop["id"], prop.get("value", ""))
                lines.append(f"- {prop.get('name', prop['id'])}: {val}")

        # Pending consequences
        if event_sections and event_sections.get("consequences"):
            lines.append(f"\n## 潜在后果（玩家不可见，仅供GM参考）\n{event_sections['consequences']}")

        # Active deadlines
        if event_sections and event_sections.get("deadlines"):
            lines.append(f"\n## 活跃时限（叙事中应自然体现紧迫感）\n{event_sections['deadlines']}")

        # Companions (AI should include them in scenes naturally)
        companion_ids = state.get("companions", [])
        if companion_ids:
            npcs_state = state.get("npcs", {})
            dn = state.get("display_names", {})
            lines.append("\n## 同行同伴（始终跟随主角，场景中应自然出现）")
            for cid in companion_ids:
                ns = npcs_state.get(cid, {})
                cname = dn.get(cid) or (ns.get("name", cid) if isinstance(ns, dict) else cid)
                personality = ns.get("personality", "") if isinstance(ns, dict) else ""
                lines.append(f"- {cname}" + (f"（{personality[:30]}）" if personality else ""))

        # Attribute alerts
        alerts = self._check_attribute_alerts(state)
        if alerts:
            lines.append("\n## 属性警告")
            for alert in alerts:
                lines.append(f"- {alert}")

        # Narrative threads — lorebook 存在时由 lorebook 按需激活
        if not self.lorebook and event_sections and event_sections.get("threads"):
            lines.append(f"\n## 叙事线索（帮助保持多线叙事的连贯性）\n{event_sections['threads']}")

        return "\n".join(lines)

    def _build_stage2_story_hint(self, state: dict) -> str:
        """Lightweight story tree hint for Stage 2 env/char prompts."""
        sts = state.get("story_tree_state")
        if not sts:
            return ""
        story_tree_def = self.script.get("story_tree", {})
        all_nodes = {}
        for tree in story_tree_def.get("trees", []):
            for node in tree.get("nodes", []):
                all_nodes[node["id"]] = node
        active = sts.get("active", [])
        if not active:
            return ""
        hints = []
        for nid in active[:3]:
            node = all_nodes.get(nid)
            if not node:
                continue
            direction = node.get("stage_direction", "") or node.get("narrative_hint", "")
            desc = direction or node.get("description", "") or node.get("name", nid)
            hints.append(f"- {desc[:80]}")
        if not hints:
            return ""
        return "当前剧情走向（描写应呼应这些方向）:\n" + "\n".join(hints)

    def _build_story_context_section(self, state: dict) -> str:
        """Build story tree context section for active storylines."""
        sts = state.get("story_tree_state")
        if not sts:
            return ""
        story_tree_def = self.script.get("story_tree", {})
        trees = story_tree_def.get("trees", [])
        if not trees:
            return ""

        active = set(sts.get("active", []))
        completed = set(sts.get("completed", []))
        choices_made = sts.get("choices_made", {})
        if not active and not completed:
            return ""

        lines = ["## 当前剧情线"]
        for tree in trees:
            tree_active = []
            tree_completed_recent = []
            for node in tree.get("nodes", []):
                nid = node["id"]
                if nid in active:
                    tree_active.append(node)
                elif nid in completed:
                    tree_completed_recent.append(node)

            if not tree_active and not tree_completed_recent:
                continue

            lines.append(f"\n### {tree.get('name', tree['id'])}")
            if tree.get("description"):
                lines.append(tree["description"])

            if tree_completed_recent:
                recent = tree_completed_recent[-3:]
                for node in recent:
                    nid = node["id"]
                    choice = choices_made.get(nid)
                    mark = "✓"
                    if choice:
                        choice_label = ""
                        for c in node.get("choices", []):
                            if c["id"] == choice:
                                choice_label = c.get("label", choice)
                                break
                        mark = f"✓ 选择了: {choice_label}"
                    lines.append(f"- [{mark}] {node.get('name', nid)}")

            for node in tree_active:
                ntype = node.get("type", "auto")
                if ntype == "quest":
                    lines.append(f"- [进行中] {node.get('name', node['id'])}: {node.get('description', '')}")
                elif ntype == "choice":
                    lines.append(f"- [待选择] {node.get('name', node['id'])}")
                elif ntype == "timed":
                    lines.append(f"- [进行中] {node.get('name', node['id'])}")

        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _build_callback_hints_section(state: dict, turn_number: int) -> str:
        callbacks = state.get("narrative_callbacks", [])
        if not callbacks:
            return ""
        eligible = []
        for cb in callbacks:
            if cb.get("used"):
                continue
            turn_diff = turn_number - cb.get("turn", 0)
            if turn_diff < cb.get("callback_after", 3):
                continue
            if turn_diff > cb.get("callback_before", 25):
                continue
            eligible.append(cb)
        if not eligible:
            return ""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        eligible.sort(key=lambda c: (priority_order.get(c.get("priority", "low"), 2), c.get("turn", 0)))
        hints = eligible[:3]
        lines = ["## 叙事回引提示（自然融入对话或描写，不要生硬提及）"]
        for h in hints:
            ago = turn_number - h.get("turn", 0)
            lines.append(f"- （{ago}回合前）{h['text']}")
        return "\n".join(lines)

    @staticmethod
    def _build_tone_section(state: dict) -> str:
        tone_parts = state.get("active_tone", [])
        if not tone_parts:
            return ""
        lines = ["## 本回合叙事基调"]
        for part in tone_parts:
            lines.append(f"- {part}")
        return "\n".join(lines)

    @staticmethod
    def _build_pacing_section(state: dict) -> str:
        pacing = state.get("pacing_state")
        if not pacing:
            return ""
        tension = pacing.get("tension", 50)
        trend = pacing.get("trend", "stable")
        trend_label = {"rising": "上升中", "falling": "下降中", "stable": "平稳"}.get(trend, trend)
        rec = pacing.get("recommendation", "")
        lines = [
            "## 叙事节奏提示",
            f"当前紧张度: {tension}/100（{trend_label}）",
        ]
        if tension < 40:
            lines.append(
                "节奏指导：当前处于低紧张期。叙事以白描和日常细节为主，"
                "用环境声响、光线变化、人物小动作传递氛围。"
                "如果骨架中有异常信息，以平淡笔触带过——一笔写完，不渲染，不重复。"
                "张力来自'正常表面下的不对劲'，而非显性对抗。"
            )
        elif tension < 70:
            lines.append(
                "节奏指导：中等紧张期。可以有信息交换和暗示性细节，"
                "但每回合只着重呈现一个信息点，其余一笔带过。"
                "NPC表现应符合其日常行为模式，异常举动需要充分铺垫。"
            )
        else:
            lines.append(
                "节奏指导：高紧张期。可以有明确冲突和对抗，节奏加快。"
                "但仍需保持物理真实性——不要为了戏剧效果牺牲合理性。"
            )
        if rec:
            lines.append(f"特别建议: {rec}")
        return "\n".join(lines)

    @staticmethod
    def _build_moral_alignment_section(state: dict) -> str:
        ma = state.get("moral_alignment")
        if not ma:
            return ""
        _LABELS = {
            "mercy_vs_cruelty": ("仁慈", "残忍"),
            "honesty_vs_deception": ("诚实", "欺骗"),
            "order_vs_chaos": ("秩序", "混沌"),
        }
        tags = []
        for axis, (pos, neg) in _LABELS.items():
            v = ma.get(axis, 0)
            if v >= 40:
                tags.append(pos)
            elif v <= -40:
                tags.append(neg)
        if not tags:
            return ""
        label = "、".join(tags)
        lines = [
            "## 玩家道德画像",
            f"倾向: {label}",
            "NPC 应根据玩家的道德声誉产生差异化反应（信任/警惕/尊敬/厌恶等）。",
        ]
        return "\n".join(lines)

    def _build_faction_reputation_section(self, state: dict) -> str:
        rep = state.get("faction_reputation", {})
        if not rep:
            return ""
        org_map = {}
        for org in self.script.get("organizations", []):
            org_map[org.get("id", "")] = org.get("name", org.get("id", ""))
        lines = ["## 阵营声望"]
        for fid, data in rep.items():
            fname = org_map.get(fid, state.get("display_names", {}).get(fid, fid))
            val = data.get("value", 50)
            title = data.get("title", "中立")
            lines.append(f"- {fname}: {title}({val})")
        return "\n".join(lines)

    @staticmethod
    def _build_memory_echo_section(state: dict) -> str:
        echoes = state.get("memory_echoes", [])
        if not echoes:
            return ""
        _WEIGHT_MAP = {"high": "强烈", "medium": "淡淡", "low": "隐约"}
        lines = ["## 记忆回响（请在叙事中自然融入这些回忆，不要生硬列举）"]
        for echo in echoes[:3]:
            weight = _WEIGHT_MAP.get(echo.get("emotional_weight", "medium"), "")
            lines.append(f"- [{echo.get('type', '')}] {echo.get('trigger', '')}：{echo.get('memory', '')}（{weight}的记忆，{echo.get('turns_ago', 0)}回合前）")
        return "\n".join(lines)

    @staticmethod
    def _build_time_atmosphere_section(state: dict) -> str:
        atmo = state.get("time_atmosphere")
        if not atmo:
            return ""
        weather = state.get("current_weather", "")
        lines = [f"## 时段氛围：{atmo.get('period_label', '')}（{atmo.get('light_level', '')}）"]
        lines.append(f"环境基调: {atmo.get('mood_hint', '')}")
        if weather:
            lines.append(f"天气: {weather}")
        effects = atmo.get("gameplay_effects", [])
        if effects:
            lines.append(f"环境影响: {', '.join(effects)}")
        lines.append("请让叙事的光线、氛围、声音描写与当前时段吻合。")
        return "\n".join(lines)

    @staticmethod
    def _build_difficulty_section(state: dict) -> str:
        da = state.get("difficulty_awareness")
        if not da or da.get("adjustment") == "neutral":
            return ""
        hint = da.get("hint_for_ai", "")
        if not hint:
            return ""
        momentum = da.get("player_momentum", "balanced")
        label = {"struggling": "苦战中", "dominating": "势如破竹"}.get(momentum, "")
        lines = [f"## 难度感知（{label}）", hint]
        return "\n".join(lines)

    @staticmethod
    def _build_npc_relationship_depth_section(state: dict) -> str:
        depths = state.get("npc_relationship_depths", {})
        if not depths:
            return ""
        dn = state.get("display_names", {})
        entries = []
        for npc_id, info in depths.items():
            name = dn.get(npc_id, npc_id)
            label = info.get("label", "陌生人")
            entries.append(f"- {name}: {label}（交流{info.get('talks', 0)}次）")
        if not entries:
            return ""
        lines = ["## NPC关系深度"] + entries
        lines.append("请根据关系深度调整NPC互动模式：陌生人应礼貌疏离，朋友/挚友应自然亲密、分享秘密或主动帮助。")
        return "\n".join(lines)

    @staticmethod
    def _build_discovery_hints_section(state: dict) -> str:
        dh = state.get("discovery_hints")
        if not dh:
            return ""
        nearby = dh.get("nearby_hints", [])
        if not nearby:
            return ""
        lines = ["## 附近的探索线索"]
        for h in nearby[:3]:
            lines.append(f"- {h.get('hint', '')}")
        lines.append("可以在选项中巧妙融入探索方向的暗示，引导玩家发现新区域或新NPC。")
        return "\n".join(lines)

    @staticmethod
    def _build_skill_growth_section(state: dict) -> str:
        growth = state.get("skill_growth", {})
        if not growth:
            return ""
        entries = [(name, info) for name, info in growth.items() if info.get("level", 0) > 0]
        if not entries:
            return ""
        lines = ["## 角色技能熟练度"]
        for name, info in entries:
            lv = info["level"]
            lines.append(f"- {name}: Lv.{lv}（+{info.get('bonus', 0)}加成）")
        lines.append("可在叙事中体现角色因经验积累而更加熟练的表现。")
        return "\n".join(lines)

    @staticmethod
    def _build_available_quests_section(state: dict) -> str:
        quests = state.get("available_quests", [])
        if not quests:
            return ""
        lines = ["## 可触发支线"]
        for q in quests[-3:]:
            name = q.get("name", q.get("id", ""))
            hint = q.get("trigger_hint", "")
            line = f"- {name}"
            if hint:
                line += f"（引入提示: {hint}）"
            lines.append(line)
        lines.append("请在叙事中自然引出上述支线机会，不要生硬提及。")
        return "\n".join(lines)
