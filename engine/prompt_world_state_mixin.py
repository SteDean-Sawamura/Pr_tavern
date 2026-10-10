"""PromptBuilder Mixin: 世界状态推演 prompt 构建"""
from __future__ import annotations

from typing import TYPE_CHECKING

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


class PromptWorldStateMixin:
    """Stage 4b 世界状态推演: 资源/空间/时间/世界属性/扩展状态, 事件阶段, Stage 2 合并"""

    # Stage 4b 字段→系统映射，用于动态裁剪
    # group: "resource" = 属性/物品/状态, "spatial" = 位置/场景, "temporal" = 时间/世界属性, "ext" = 低频扩展
    _FIELD_DEFS: dict[str, dict] = {
        "state_changes": {"system": None, "group": "resource", "desc": '- state_changes: [{"target":"player.属性名","op":"add","value":数值,"reason":"原因"}] 也可修改NPC属性: target="npcs.{npc_id}.字段名" op="set"'},
        "end_time": {"system": None, "group": "temporal", "desc": '- end_time: 叙事文本最后一句描写所对应的时刻，ISO 8601格式（如1979-10-26T10:00:00）。必填。严格对齐叙事末尾——如果叙事写到"躺在床上闭上眼"但尚未入睡，end_time就是闭眼那一刻（如23:30），绝不可跳到次日早晨。只有叙事明确描写了醒来/天亮的场景，end_time才可以是第二天。不要输出时段/时长，输出绝对时间。时间推进量必须与玩家行动规模匹配：签字/签收/查看=5~10分钟，对话/交谈=10~30分钟，移动/前往=30~120分钟，睡觉/过夜=6~10小时。玩家说"立即/马上"时推进量不超过15分钟'},
        "location_change": {"system": "location", "group": "spatial", "desc": '- location_change: 玩家在本回合结束时所在的位置ID（必须从上文「已知地点」列表复制精确ID，禁止自造）。必须与叙事中主角最终所在地点一致。若叙事中主角未移动则省略此字段'},
        "reveal_locations": {"system": "location", "group": "spatial", "desc": '- reveal_locations: [{"id":"位置ID","name":"显示名称"}]'},
        "inventory_changes": {"system": "inventory", "group": "resource", "desc": '- inventory_changes: [{"item":"物品名","action":"add|remove","quantity":1,"description":"可选简述"}] add限制：只能添加叙事中明确描写角色获取的物品，禁止添加叙事未提及的物品。移除物品时item字段必须与背包中的物品名完全一致（复制粘贴），不要缩写或改写'},
        "activate_states": {"system": None, "group": "resource", "desc": '- activate_states: [{"id":"状态ID","name":"中文显示名称（必填，不可用英文ID）","description":"一句话描述"}] 预定义状态可只填ID字符串'},
        "deactivate_states": {"system": None, "group": "resource", "desc": '- deactivate_states: [状态ID]'},
        "world_property_changes": {"system": None, "group": "world", "desc": '- world_property_changes: [{"id":"属性ID","value":"新值"}] 宏观世界级变量（戒严等级、势力影响等），不写物件状态'},
        "npc_location_changes": {"system": "npc", "group": "spatial", "desc": '- npc_location_changes: [{"npc_id":"","new_location":"位置ID（必须从已知地点列表复制）","reason":"离开/到达原因"}] 本回合在场NPC的位置变动'},
        "room_changes": {"system": None, "group": "spatial", "desc": '- room_changes: [{"id":"npc_id或player","new_room":"房间名","reason":"移动原因"}] 同一建筑内的房间级移动（如从办公室走到会议室）。player表示玩家自己的房间变化'},
        "scene_details": {"system": None, "group": "spatial", "desc": '- scene_details: {"atmosphere":"","sensory":"","key_objects":[],"physical":{"lighting":"照明条件(如日光灯/烛光/自然光)","floor":"地面材质(如地毯/大理石/木板)","spatial_note":"空间布局要点(如狭长走廊/开阔大厅，关键门窗朝向)"}} physical子对象描述当前地点的稳定物理特征，同一地点内不应改变；换地点时重新描述'},
        "game_over": {"system": "game_over", "group": "resource", "desc": '- game_over: {"reason":"","ending_type":""} 或省略'},
        "offscreen_npc_updates": {"system": None, "group": "ext", "desc": '- offscreen_npc_updates: [{"name":"NPC中文全名","action":"简述行动","location":"当前位置"}] 最多2-3个与本回合事件因果相关的离场NPC。name字段必须使用NPC的中文全名（如"人物甲""人物乙"），不要用英文ID。注意信息传播延迟：远处NPC不可能立即知道刚发生的事件并做出反应，除非有明确的通讯渠道'},
        "new_npcs": {"system": None, "group": "ext", "desc": '- new_npcs: [{"id":"英文蛇形ID","name":"完整全名（符合世界观的真实姓名，禁止用X某/小X/路人甲等占位符）","title":"职位/头衔","bio":"2-3句简介：外貌特征、背景经历、与当前场景的关联","personality":"性格关键词,逗号分隔","location":"必填-当前所在位置ID","trust":0-100初始信任,"affection":0-100初始好感,"fear":0-100初始畏惧}] 关键规则：叙事中任何有名有姓、有台词或有具体互动的非预设角色必须在此注册，否则下一回合将丢失该角色的身份信息。路人甲/背景群众不需要。三维关系值根据NPC对玩家的初次印象和互动方式判断'},
        "faction_reputation_changes": {"system": "faction", "group": "ext", "desc": '- faction_reputation_changes: [{"faction_id":"组织ID","change":±数值,"reason":"原因"}] 玩家与组织声望变化(±1到±15)'},
        "moral_alignment_changes": {"system": "moral", "group": "ext", "desc": '- moral_alignment_changes: [{"axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":"原因"}] 本回合玩家行为的道德维度影响(±1到±15)'},
        "recruit_companions": {"system": "companion", "group": "ext", "desc": '- recruit_companions: ["npc_id"] 当NPC明确表示愿意加入队伍/跟随玩家时添加'},
        "dismiss_companions": {"system": "companion", "group": "ext", "desc": '- dismiss_companions: ["npc_id"] 当同伴离队时添加'},
        "invalidate_lore": {"system": "_deprecated", "group": "ext", "desc": '- invalidate_lore: ["词条ID"]'},
    }

    _GROUP_META = {
        "resource": ("资源状态引擎", "属性/物品/持续状态/胜负"),
        "spatial":  ("空间状态引擎", "位置移动/地点发现/NPC位置/房间/场景描写"),
        "temporal": ("时间状态引擎", "时间推进"),
        "world":    ("世界属性引擎", "宏观世界变量变化"),
        "ext":      ("扩展状态引擎", "后果/NPC/阵营/线索/同伴/剧情线"),
    }

    _EVENT_STAGE_SYSTEM = (
        "你是游戏事件引擎。根据叙事内容和已有事件状态，输出事件变更。\n"
        "输出紧凑JSON，只含 event_changes 数组。\n\n"
        "每条变更格式:\n"
        '- {"action":"create","id":"唯一ID","category":"consequence|deadline|clue|thread","description":"描述","name":"名称(thread必填)","fields":{按类型补充}}\n'
        '- {"action":"update","id":"已有事件ID","fields":{"status":"新状态","description":"新描述",...}}\n'
        '- {"action":"delete","id":"已有事件ID"}\n\n'
        "category 字段说明:\n"
        "- consequence: 延迟后果。fields可含 trigger_chance(0-1), turns_delay, max_turns, importance(normal|high), related_npcs, on_trigger\n"
        "- deadline: 限时任务。fields可含 turns_remaining, on_expire:{description,state_changes,fire_events}, on_complete:{condition,description}\n"
        "- clue: 线索。fields可含 category(人物|地点|事件|物品|动机), source, linked_to:[已有线索ID]\n"
        "- thread: 剧情线。fields可含 status(active|dormant|resolved|failed), related_npcs, related_orgs\n\n"
        "规则:\n"
        "- 更新已有事件用 update（相同id），不要重复创建\n"
        "- 如果叙事中玩家已经完成了某个 deadline 描述的目标，必须 update 它的 status 为 completed\n"
        "- thread resolved/failed 表示结束\n"
        "- consequence 仅在叙事暗示延迟后果时创建\n"
        "- deadline 仅在叙事中有明确时间压力时创建\n"
        "- clue 仅提取有调查价值的信息\n"
        '省略无变化时输出 {"event_changes":[]}\n'
    ) + CACHE_SENTINEL

    # ------------------------------------------------------------------
    # _build_world_state_user_message
    # ------------------------------------------------------------------
    def _build_world_state_user_message(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        group: str = "all",
        use_plot_decision: bool = False,
        event_sections: dict[str, str] | None = None,
    ) -> str:
        """构建 Stage 4b 系列共用的 user message。

        group="resource": 属性/持续状态/背包/位置（紧凑）
        group="spatial":  位置详情/已知地点/NPC房间
        group="temporal": 位置（紧凑）
        group="ext":  伏线/声望/blueprint/道德/历史词条
        group="all"/"core": 全量核心字段（兼容旧调用）
        """
        ctx = self._collect_state_context(state)
        _core_groups = ("all", "core", "resource", "spatial", "temporal", "world")
        include_resource = group in ("all", "core", "resource")
        include_spatial = group in ("all", "core", "spatial")
        include_ext = group in ("all", "ext")
        include_world = group in ("all", "core", "world")

        sections = []

        # ── resource 段落 ──
        if include_resource:
            res_parts = []
            attrs_text = "\n".join(ctx["attrs_lines"])
            res_parts.append(f"角色属性（含范围）:\n{attrs_text}")

            active_ids = ctx["active_ids"]
            predefined_ids = set()
            ps_lines = []
            for ps in self.script.get("persistent_states", []):
                predefined_ids.add(ps["id"])
                status = "激活" if ps["id"] in active_ids else "未激活"
                ps_lines.append(f"- {ps.get('name', ps['id'])}({ps['id']}): {status}")
            ps_descs = state.get("persistent_state_descriptions", {})
            for sid in active_ids - predefined_ids:
                desc = ps_descs.get(sid, "")
                label = f"- {sid}: 激活"
                if desc:
                    label += f" — {desc}"
                ps_lines.append(label)
            ps_text = "\n".join(ps_lines) if ps_lines else "无"
            res_parts.append(f"持续状态:\n{ps_text}")
            res_parts.append(f"背包: {ctx['inv_compact']}")
            sections.append(_xml("resource_state", "\n\n".join(res_parts)))

        # 位置信息
        loc_line = f"{ctx['location']}({ctx['location_id']})"
        if ctx["location_desc"]:
            loc_line += f" — {ctx['location_desc']}"
        player_room = state.get("player", {}).get("current_room", "")
        room_line = f"\n当前房间: {player_room}" if player_room else ""
        npc_states = state.get("npcs", {})
        if include_spatial:
            spatial_parts = [f"当前位置: {loc_line}{room_line}"]
            npc_room_entries = []
            for nid, ns in npc_states.items():
                if isinstance(ns, dict) and ns.get("current_room"):
                    npc_room_entries.append(f"{ns.get('name', nid)}→{ns['current_room']}")
            if npc_room_entries:
                spatial_parts.append(f"NPC房间: {'; '.join(npc_room_entries)}")
            visible_ids = set(state.get("visible_locations", []))
            loc_lines = []
            for loc in self.script.get("locations", []):
                if loc["id"] in visible_ids:
                    loc_lines.append(f"- {loc.get('name', loc['id'])}({loc['id']})")
            loc_text = "\n".join(loc_lines) if loc_lines else "无"
            spatial_parts.append(f"已知地点:\n{loc_text}")
            sections.append(_xml("spatial_state", "\n\n".join(spatial_parts)))
        else:
            sections.append(_xml("location", f"{loc_line}{room_line}"))

        # ── world 段落 ──
        if include_world:
            world_props = state.get("world_properties", {})
            wp_lines = []
            for prop in self.script.get("world_properties", []):
                val = world_props.get(prop["id"], prop.get("value", ""))
                wp_lines.append(f"- {prop.get('name', prop['id'])}({prop['id']}): {val}")
            predefined_wp_ids = {p["id"] for p in self.script.get("world_properties", [])}
            for wp_id, wp_val in world_props.items():
                if wp_id not in predefined_wp_ids:
                    dn = state.get("display_names", {}).get(wp_id, wp_id)
                    wp_lines.append(f"- {dn}({wp_id}): {wp_val}")
            wp_text = "\n".join(wp_lines) if wp_lines else "无"
            sections.append(_xml("world_properties", wp_text))

        # ── ext 段落 ──
        if include_ext:
            ext_parts = []
            _es = event_sections or {}
            ext_parts.append(_es.get("consequences", "待定伏线:\n无"))
            if _es.get("deadlines"):
                ext_parts.append(_es["deadlines"])
            if _es.get("threads"):
                ext_parts.append(_es["threads"])
            if _es.get("clues"):
                ext_parts.append(_es["clues"])
            faction_rep = state.get("faction_reputation", {})
            if faction_rep:
                org_name_map = {o.get("id", ""): o.get("name", o.get("id", "")) for o in self.script.get("organizations", [])}
                rep_lines = []
                for fid, fdata in faction_rep.items():
                    fname = org_name_map.get(fid, state.get("display_names", {}).get(fid, fid))
                    rep_lines.append(f"- {fname}({fid}): {fdata.get('title', '中立')}({fdata.get('value', 50)})")
                ext_parts.append("阵营声望:\n" + "\n".join(rep_lines))
            bp = state.get("plot_blueprint", {})
            bp_threads = bp.get("plot_threads", [])
            if bp_threads:
                bp_lines = []
                for thread in bp_threads[:5]:
                    stages = thread.get("stages", [])
                    for stage in stages:
                        status = stage.get("status", "pending")
                        if status == "active":
                            bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: 进行中 — {stage.get('description', '')}")
                            break
                        elif status == "pending":
                            bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: 待触发 — {stage.get('description', '')}")
                            break
                if bp_lines:
                    ext_parts.append("活跃剧情线:\n" + "\n".join(bp_lines))
            ma = state.get("moral_alignment")
            if ma:
                ma_text = f"仁慈↔残忍:{ma.get('mercy_vs_cruelty',0)} | 诚实↔欺骗:{ma.get('honesty_vs_deception',0)} | 秩序↔混沌:{ma.get('order_vs_chaos',0)}"
                ext_parts.append(f"道德画像(-100到+100):\n{ma_text}")
            sections.append(_xml("extended_state", "\n\n".join(ext_parts)))

        # ── 共享段落 ──
        if group == "temporal":
            gt = state.get("game_time", "")
            if gt:
                sections.append(_xml("game_time", f"{self.format_game_time(gt)} ({gt})"))

        sections.append(_xml("player_action", action_text if action_text else ""))

        if use_plot_decision:
            sections.append(_xml("plot_decision", narrative))
        else:
            sections.append(_xml("narrative", narrative))

        if include_ext:
            dynamic_lore = state.get("dynamic_lorebook", [])
            speculative_lines = []
            for dl in dynamic_lore:
                if dl.get("speculative"):
                    speculative_lines.append(f"- {dl['id']}: {dl.get('comment', dl.get('content', '')[:30])}")
            if speculative_lines:
                sections.append(_xml("speculative_lore", "可通过invalidate_lore废止:\n" + "\n".join(speculative_lines)))

        if check_result and check_result.get("outcome") in ("critical_success", "critical_failure"):
            crit_label = "大成功" if check_result["outcome"] == "critical_success" else "大失败"
            crit_attr = check_result.get("related_attribute", "")
            sections.append(_xml("check_result", f"{crit_label}（{crit_attr}）— 请在状态变更中体现持续影响（大成功=额外奖励/正面状态；大失败=惩罚/负面状态/添加后果）。"))

        return "\n\n".join(s for s in sections if s)

    # ------------------------------------------------------------------
    # build_world_state_prompt
    # ------------------------------------------------------------------
    def build_world_state_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b: 世界状态推演 - 除NPC关系外的所有状态变更。"""
        system = (
            "你是游戏状态引擎。严格根据叙事中实际描写的事件推演本回合世界状态变更。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n"
            + "\n".join(fdef["desc"] for fdef in self._FIELD_DEFS.values()) + "\n\n"
            "大成功时可额外给予奖励（物品/属性提升/开启新区域）；大失败时应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            "省略无变化的字段。紧凑JSON输出。\n\n"
            "禁止事项：\n"
            "- activate_states 不得包含天气或时段（weather/dawn/dusk/night等）——这些由引擎自动管理\n"
            "- world_property_changes 只写宏观世界级变量（戒严等级、势力影响、社会压力等），不写物件状态（台灯/闹钟/门窗/文件散落等）——物件状态属于 scene_details\n"
            "- npc_attitude_changes 中的 npc_id 必须是已存在的NPC ID，不可凭空创造新角色ID\n"
            "- location_change 必须使用「已知地点」列表中的精确ID（如上文所示），禁止自行编造ID或使用下划线拼接的英文描述"
        )
        system += CACHE_SENTINEL

        content = self._build_world_state_user_message(narrative, action_text, state, check_result)
        messages = [{"role": "user", "content": content}]
        return messages, system

    # ------------------------------------------------------------------
    # _format_events_for_prompt
    # ------------------------------------------------------------------
    @staticmethod
    def _format_events_for_prompt(event_data: dict) -> dict[str, str]:
        """将 event_engine.get_events_for_prompt() 输出格式化为 prompt 文本段。"""
        sections: dict[str, str] = {}
        cons = event_data.get("consequences", [])
        if cons:
            lines = [f"- {c['description']}（已酝酿{c.get('turns_waited', 0)}回合）" for c in cons]
            sections["consequences"] = "待定伏线:\n" + "\n".join(lines)
        dls = event_data.get("deadlines", [])
        if dls:
            lines = [f"- {d['description']}（剩余{d.get('turns_remaining', '?')}回合）" for d in dls]
            urgent = [d for d in dls if isinstance(d.get("turns_remaining"), int) and d["turns_remaining"] <= 3]
            if len(urgent) >= 2:
                names = "、".join(d["description"][:20] for d in urgent[:3])
                lines.append(f"\n⚠ 时间冲突：{names} 同时逼近！玩家不可能全部完成，必须在选项中体现取舍")
            sections["deadlines"] = "活跃限时:\n" + "\n".join(lines)
        threads = event_data.get("threads", [])
        if threads:
            active_t = [t for t in threads if t.get("status") == "active"]
            dormant_t = [t for t in threads if t.get("status") == "dormant"]
            t_lines = []
            for t in active_t:
                t_lines.append(f"- 【进行中】{t.get('name', t.get('id', ''))}：{t.get('description', '')}")
            for t in dormant_t[:2]:
                t_lines.append(f"- 【搁置】{t.get('name', t.get('id', ''))}：{t.get('description', '')}")
            if t_lines:
                sections["threads"] = "叙事线程:\n" + "\n".join(t_lines)
        clues = event_data.get("clues", [])
        if clues:
            recent = clues[-8:]
            lines = [f"- [{c.get('category', '?')}] {c.get('text', '')[:40]}（{c.get('source', '')}）" for c in recent]
            sections["clues"] = "已知线索:\n" + "\n".join(lines)
        nodes = event_data.get("story_nodes", [])
        if nodes:
            lines = [f"- {n.get('name', n['id'])}：{n.get('description', '')[:60]}" for n in nodes]
            sections["story_nodes"] = "当前剧情走向:\n" + "\n".join(lines)
        return sections

    # ------------------------------------------------------------------
    # build_event_stage_prompt
    # ------------------------------------------------------------------
    def build_event_stage_prompt(
        self, narrative: str, action_text: str,
        event_data: dict, parsed_summary: str = "",
    ) -> tuple[list[dict], str]:
        """异步事件阶段 prompt：统一的 event_changes 格式。"""
        event_sections = self._format_events_for_prompt(event_data)
        xml_parts = []
        if action_text:
            xml_parts.append(_xml("player_action", action_text))
        xml_parts.append(_xml("narrative", narrative))
        if parsed_summary:
            xml_parts.append(_xml("state_summary", parsed_summary))
        event_parts = []
        for key in ("consequences", "deadlines", "threads", "clues"):
            if event_sections.get(key):
                event_parts.append(event_sections[key])
        if event_parts:
            xml_parts.append(_xml("existing_events", "\n\n".join(event_parts)))
        content = "\n\n".join(s for s in xml_parts if s)
        return [{"role": "user", "content": content}], self._EVENT_STAGE_SYSTEM

    # ------------------------------------------------------------------
    # build_pruned_world_state_prompt
    # ------------------------------------------------------------------
    def build_pruned_world_state_prompt(
        self, narrative: str, action_text: str, state: dict,
        active_systems: list[str],
        check_result: dict | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b 动态裁剪版：只包含 route.systems 命中的字段说明。"""
        active_set = set(active_systems)
        field_lines = []
        for fdef in self._FIELD_DEFS.values():
            if fdef["system"] is None or fdef["system"] in active_set:
                field_lines.append(fdef["desc"])

        system = (
            "你是游戏状态引擎。严格根据叙事中实际描写的事件推演本回合世界状态变更。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n" + "\n".join(field_lines) + "\n\n"
            "大成功时可额外给予奖励（物品/属性提升/开启新区域）；大失败时应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            "省略无变化的字段。紧凑JSON输出。"
        )

        content = self._build_world_state_user_message(narrative, action_text, state, check_result)
        messages = [{"role": "user", "content": content}]
        return messages, system

    # ------------------------------------------------------------------
    # _build_world_state_system
    # ------------------------------------------------------------------
    def _build_world_state_system(self, group: str, active_systems: list[str] | None = None) -> str:
        """构建指定 group 的 Stage 4b system prompt，可选 route pruning。"""
        loader = PromptLoader.get()

        active_set = set(active_systems) if active_systems else None
        field_lines = []
        for fdef in self._FIELD_DEFS.values():
            if fdef["group"] != group:
                continue
            if active_set is not None and fdef["system"] is not None and fdef["system"] not in active_set:
                continue
            field_lines.append(fdef["desc"])

        role, focus = self._GROUP_META.get(group, ("状态引擎", "状态变更"))

        # group 专属后缀
        group_suffix = ""
        if group == "resource":
            group_suffix = (
                "物品变更原则：只有叙事中明确描写了角色获得（拾取/购买/赠予/搜获/制作）的物品才能add；"
                "不得凭空发明叙事中未出现的物品。\n"
                "仅当下方标注[检定结果: 大成功]时才可额外给予奖励；"
                "仅当标注[检定结果: 大失败]时才应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            )
        elif group == "temporal":
            group_suffix = (
                "关键：end_time 是叙事结束时的绝对时间戳，不是时长。"
                "找到叙事最后场景的时间点，直接输出该时间。"
                "当前游戏时间已在下方提供。\n"
            )
        elif group == "world":
            group_suffix = (
                "只修改叙事中有明确变化依据的世界属性。"
                "下方提供了当前世界属性列表及其值。如无变化则返回空JSON {}。\n"
            )

        system = loader.render_system(
            "world_state/_shared_system",
            role=role,
            focus=focus,
            field_lines="\n".join(field_lines),
            group_suffix=group_suffix,
        )
        system += CACHE_SENTINEL
        return system

    # ------------------------------------------------------------------
    # build_world_state_core_prompt
    # ------------------------------------------------------------------
    def build_world_state_core_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-core: 兼容入口，委托 resource 引擎。"""
        return self.build_world_state_resource_prompt(
            narrative, action_text, state, check_result, active_systems, plot_decision,
        )

    # ------------------------------------------------------------------
    # build_world_state_resource_prompt
    # ------------------------------------------------------------------
    def build_world_state_resource_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-resource: 属性/物品/持续状态/胜负。"""
        system = self._build_world_state_system("resource", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="resource", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    # ------------------------------------------------------------------
    # build_world_state_spatial_prompt
    # ------------------------------------------------------------------
    def build_world_state_spatial_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-spatial: 位置移动/地点发现/NPC位置/房间/场景描写。"""
        system = self._build_world_state_system("spatial", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="spatial", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    # ------------------------------------------------------------------
    # build_world_state_temporal_prompt
    # ------------------------------------------------------------------
    def build_world_state_temporal_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-temporal: 时间推进。"""
        system = self._build_world_state_system("temporal", active_systems)
        # end_time 须从叙事全文推断（具体钟表时间在 Stage 2a/3 中产生，骨架中没有）
        source = narrative if narrative else plot_decision
        use_pd = not bool(narrative)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="temporal", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    # ------------------------------------------------------------------
    # build_world_state_world_prompt
    # ------------------------------------------------------------------
    def build_world_state_world_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-world: 世界属性变化。"""
        system = self._build_world_state_system("world", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="world", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    # ------------------------------------------------------------------
    # build_world_state_ext_prompt
    # ------------------------------------------------------------------
    def build_world_state_ext_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b-ext: 扩展状态 -- NPC/阵营/同伴。"""
        system = self._build_world_state_system("ext", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="ext", use_plot_decision=use_pd, event_sections=event_sections)
        return [{"role": "user", "content": content}], system

    # ------------------------------------------------------------------
    # build_merged_stage2_prompt
    # ------------------------------------------------------------------
    def build_merged_stage2_prompt(
        self, plot_decision: str, state: dict,
        present_npc_ids: list[str] | None = None,
        prev_narrative: str = "",
        action_text: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 2 合并模式：scope=minor 时环境+角色合为一次调用。"""
        system = (
            "你是文字游戏的场景编导。根据剧情骨架，生成两部分内容：\n"
            "第一部分：环境和氛围描写（200字以内，覆盖2-3种感官）\n"
            "第二部分：角色对话和行为（300字以内，第二人称主角，第三人称NPC）\n\n"
            "用 --- 分隔两部分。用中文弯引号。不得捏造新角色。\n"
            "时代约束：科技、通讯工具、交通方式、日用物品必须与世界背景的时代吻合"
        )
        system += CACHE_SENTINEL

        player = state.get("player", {})
        loc_id = player.get("location", "")
        location_name = self._resolve_location_name(loc_id)
        loc_def = self._location_by_id.get(loc_id, {})
        loc_desc = loc_def.get("description", "")
        game_time = state.get("game_time", "")
        npc_states = state.get("npcs", {})

        npc_brief = ""
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for nid in (present_npc_ids or [])[:4]:
            ns = npc_states.get(nid, {})
            if isinstance(ns, dict):
                name = ns.get("name", nid)
                title = ns.get("title", "") or npc_defs.get(nid, {}).get("title", "")
                personality = ns.get("personality", "")
                room = ns.get("current_room", "")
                att = ns.get("attitude_toward_player", 50)
                tag_parts = []
                if title:
                    tag_parts.append(title)
                if personality:
                    tag_parts.append(f"性格={personality}")
                tag_parts.append(f"态度{att}")
                room_str = f" → {room}" if room else ""
                npc_brief += f"- {name}{room_str}（{'，'.join(tag_parts)}）\n"

        player_room = player.get("current_room", "")
        world_bg = self.script.get("world_background", "")

        # 上一轮氛围（连续性）
        scene_details = state.get("scene_details", {})
        prev_atmosphere = scene_details.get("atmosphere", "")

        # <scene> 区块
        scene_parts = [f"位置: {location_name}" + (f" - {loc_desc[:60]}" if loc_desc else "")]
        if player_room:
            scene_parts.append(f"当前房间: {player_room}")
        if world_bg:
            scene_parts.append(f"世界背景: {world_bg[:200]}")
        scene_parts.append(f"时间: {game_time or '未知'}")
        if prev_atmosphere:
            scene_parts.append(f"上一轮氛围: {prev_atmosphere}")

        # 组装 XML
        sections = [
            _xml("scene", "\n".join(scene_parts)),
            _xml("npcs_present", npc_brief.strip() or "无"),
            _xml("continuity", prev_narrative if prev_narrative else ""),
            _xml("player_action", action_text if action_text else ""),
            _xml("plot_decision", plot_decision),
        ]
        user_content = "\n\n".join(s for s in sections if s)

        messages = [{"role": "user", "content": user_content}]
        return messages, system
