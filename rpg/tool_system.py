"""Tool Calling 系统：工具 schema 定义和 executor 构建"""

import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WorldStateManager
    from .session import TavernRPGSession


def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required if required is not None else list(props.keys()),
                       "additionalProperties": False}}}


def _build_tool_schemas_and_executors(agent_type: str, world_state: "WorldStateManager", session: "TavernRPGSession" = None) -> tuple[list[dict], dict]:
    """为指定 agent 类型构建工具 schemas 和 executors。"""

    def _get_skill_bonus(skill: str) -> int:
        player = world_state.script_data.get("player_character", {})
        attrs = player.get("attributes", {})
        for attr_name, attr_data in attrs.items():
            if isinstance(attr_data, dict):
                for syn in attr_data.get("synonyms", [attr_name]):
                    if skill.lower() == syn.lower():
                        val = attr_data.get("value", 50)
                        return (val - 10) // 2 if isinstance(val, (int, float)) else 0
        return 0

    # --- Executors ---
    async def query_state(category: str, key: str) -> dict:
        result = world_state.query_info(f"{category}_attr" if category == "player" else category, key)
        return {"value": result}

    async def query_npc_info(npc_id: str) -> dict:
        if not session:
            return {"error": "session not available"}
        npc_data = next((n for n in session.script_data.get("npcs", []) if n.get("id") == npc_id), None)
        if not npc_data:
            return {"error": f"NPC {npc_id} not found"}
        return {"id": npc_id, "name": npc_data.get("name"), "title": npc_data.get("title"),
                "personality": npc_data.get("personality"), "relationship": world_state.relationships.get(npc_id, 0)}

    async def query_location_info(location_id: str) -> dict:
        if not session:
            return {"error": "session not available"}
        loc_data = next((l for l in session.script_data.get("locations", []) if l.get("id") == location_id), None)
        if not loc_data:
            return {"error": f"Location {location_id} not found"}
        return {"id": location_id, "name": loc_data.get("name"), "description": loc_data.get("description")}

    async def roll_dice(skill: str, dc: int = 10) -> dict:
        import random
        roll = random.randint(1, 20)
        bonus = _get_skill_bonus(skill)
        total = roll + bonus
        success = total >= dc
        return {"roll": roll, "bonus": bonus, "total": total, "dc": dc, "success": success,
                "description": f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"}

    async def check_inventory(item_name: str) -> dict:
        inventory = world_state.variables.get("inventory", [])
        found = any(item_name in (i.get("name", i.get("item", "")) if isinstance(i, dict) else str(i)) for i in inventory)
        return {"has_item": found, "description": f"玩家{'持有' if found else '未持有'}{item_name}"}

    async def get_npc_attitude(npc_id: str) -> dict:
        rel = world_state.relationships.get(npc_id, 50)
        return {"npc_id": npc_id, "attitude": rel, "description": f"{npc_id}的态度: {rel}/100"}

    async def get_time() -> dict:
        time_str = world_state.current_date.isoformat()
        return {"time": time_str, "description": f"当前时间: {time_str}"}

    async def modify_relationship(npc_id: str, delta: int) -> dict:
        delta = max(-10, min(10, delta))
        return {"type": "modify_relationship", "npc_id": npc_id, "delta": delta, "status": "proposed"}

    async def propose_event(event_name: str, description: str, delay_turns: int = 0) -> dict:
        return {"type": "propose_event", "event_name": event_name, "description": description, "delay_turns": delay_turns, "status": "proposed"}

    async def give_item(item_name: str, description: str = "") -> dict:
        return {"type": "give_item", "item_name": item_name, "description": description, "status": "proposed"}

    async def take_item(item_name: str) -> dict:
        return {"type": "take_item", "item_name": item_name, "status": "proposed"}

    async def update_npc_relationship(from_npc: str, to_npc: str, rel_type: str, description: str = "") -> dict:
        return {"type": "update_npc_relationship", "from_npc": from_npc, "to_npc": to_npc,
                "rel_type": rel_type, "description": description, "status": "proposed"}

    async def share_information(fact: str, known_by: str = "") -> dict:
        return {"type": "share_information", "fact": fact, "known_by": known_by, "status": "proposed"}

    async def update_plan(goal: str, next_step: str, progress: str = "") -> dict:
        return {"type": "update_plan", "goal": goal, "next_step": next_step, "progress": progress, "status": "proposed"}

    async def apply_state_change(var: str, op: str, value: any) -> dict:
        return {"type": "apply_state_change", "var": var, "op": op, "value": value, "status": "proposed"}

    async def move_player(location_id: str) -> dict:
        return {"type": "move_player", "location_id": location_id, "status": "proposed"}

    async def set_world_prop(key: str, value: any) -> dict:
        return {"type": "set_world_prop", "key": key, "value": value, "status": "proposed"}

    async def trigger_event(event_name: str, description: str) -> dict:
        return {"type": "trigger_event", "event_name": event_name, "description": description, "status": "proposed"}

    async def spawn_npc(name: str, title: str, personality: str, location_id: str) -> dict:
        return {"type": "spawn_npc", "name": name, "title": title, "personality": personality, "location_id": location_id, "status": "proposed"}

    async def remove_npc(npc_id: str, reason: str = "") -> dict:
        return {"type": "remove_npc", "npc_id": npc_id, "reason": reason, "status": "proposed"}

    async def adjust_tension(delta: int) -> dict:
        delta = max(-30, min(30, delta))
        if session:
            session._pending_tension_adjustment += delta
        return {"type": "adjust_tension", "delta": delta, "status": "applied"}

    async def change_faction_reputation(faction_id: str, delta: int, reason: str = "") -> dict:
        delta = max(-20, min(20, delta))
        return {"type": "change_faction_reputation", "faction_id": faction_id, "delta": delta, "reason": reason, "status": "proposed"}

    async def request_re_reason(npc_id: str, reason: str) -> dict:
        return {"type": "request_re_reason", "npc_id": npc_id, "reason": reason, "status": "queued"}

    async def need_info(query: str, category: str = "general") -> dict:
        """Agent 请求补充信息，立即返回查询结果。"""
        result = {"type": "need_info", "query": query, "category": category}
        q = query.strip()
        ql = q.lower()
        if category == "npc" and session:
            npcs = session.script_data.get("npcs") or []
            def _fmt_npc(n):
                return f"{n.get('name')}: {n.get('personality','')}, 关系={world_state.relationships.get(n['id'], 0)}"
            # 精确 ID → 精确名字 → 子串 fallback
            hit = next((n for n in npcs if n.get("id", "").lower() == ql), None)
            if not hit:
                hit = next((n for n in npcs if n.get("name", "").lower() == ql), None)
            if not hit:
                hit = next((n for n in npcs if ql in n.get("name", "").lower() or ql in n.get("id", "").lower()), None)
            if hit:
                result["answer"] = _fmt_npc(hit)
                return result
        if category == "location" and session:
            locs = session.script_data.get("locations") or []
            def _fmt_loc(l):
                return f"{l.get('name')}: {l.get('description','')}"
            hit = next((l for l in locs if l.get("id", "").lower() == ql), None)
            if not hit:
                hit = next((l for l in locs if l.get("name", "").lower() == ql), None)
            if not hit:
                hit = next((l for l in locs if ql in l.get("name", "").lower() or ql in l.get("id", "").lower()), None)
            if hit:
                result["answer"] = _fmt_loc(hit)
                return result
        if category == "player":
            val = world_state.player_attrs.get(q)
            if val is not None:
                result["answer"] = f"{q}={val}"
                return result
            for k, v in world_state.player_attrs.items():
                if ql == k.lower():
                    result["answer"] = f"{k}={v}"
                    return result
        if category == "inventory":
            inv = world_state.variables.get("inventory", [])
            if q:
                found = [i for i in inv if q in (i.get("name", str(i)) if isinstance(i, dict) else str(i))]
                if found:
                    result["answer"] = ", ".join(i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in found)
                    return result
            result["answer"] = ", ".join(i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in inv[:10]) or "背包为空"
            return result
        info = world_state.query_info("general", q)
        result["answer"] = str(info) if info else "无相关信息"
        return result

    async def play_sound(sound_type: str) -> dict:
        return {"sound": sound_type, "status": "queued"}

    async def set_weather(weather: str) -> dict:
        return {"weather": weather, "status": "set"}

    async def set_mood_filter(mood: str) -> dict:
        return {"mood": mood, "status": "set"}

    # --- Schemas (data-driven) ---
    _S = {"type": "string"}
    _I = {"type": "integer"}

    game_schemas = [
        _tool("roll_dice", "掷骰子进行技能检定", {"skill": {**_S, "description": "技能名称"}, "dc": {**_I, "description": "难度等级", "default": 10}}, ["skill"]),
        _tool("check_inventory", "检查玩家背包中是否持有指定物品", {"item_name": _S}),
        _tool("get_npc_attitude", "查询NPC当前对玩家的好感度", {"npc_id": _S}),
        _tool("get_time", "获取当前游戏时间", {}),
    ]
    game_exec = {"roll_dice": roll_dice, "check_inventory": check_inventory, "get_npc_attitude": get_npc_attitude, "get_time": get_time}

    qs_schema = _tool("query_state", "查询玩家属性、关系值或世界属性",
                       {"category": {**_S, "enum": ["player", "relationship", "world"]}, "key": _S})

    need_info_schema = _tool("need_info", "查询游戏信息：NPC好感度/性格、地点描述、玩家属性/背包、世界状态。系统立即返回结果",
                              {"query": {**_S, "description": "要查询的具体内容（NPC名/属性名/物品名等）"},
                               "category": {**_S, "enum": ["npc", "location", "player", "inventory", "general"],
                                             "description": "信息类别"}})

    if agent_type == "director":
        return [
            qs_schema,
            _tool("query_npc_info", "查询NPC基本信息", {"npc_id": _S}),
            _tool("query_location_info", "查询地点信息", {"location_id": _S}),
            _tool("spawn_npc", "动态创建NPC（当剧情需要新角色登场时）",
                  {"name": _S, "title": _S, "personality": _S, "location_id": _S}),
            _tool("remove_npc", "移除NPC（角色退场时）", {"npc_id": _S, "reason": _S}, ["npc_id"]),
            _tool("trigger_event", "触发一个叙事事件", {"event_name": _S, "description": _S}),
        ], {"query_state": query_state, "query_npc_info": query_npc_info, "query_location_info": query_location_info,
            "spawn_npc": spawn_npc, "remove_npc": remove_npc, "trigger_event": trigger_event}

    elif agent_type in ("npc", "npc_extended"):
        core_schemas = [
            _tool("roll_dice", "掷骰子进行技能检定", {"skill": {**_S, "description": "技能名称"}, "dc": {**_I, "description": "难度等级", "default": 10}}, ["skill"]),
            _tool("modify_relationship", "改变与玩家的关系值（-10到+10）",
                  {"npc_id": {**_S, "description": "NPC ID"}, "delta": {**_I, "description": "关系变化量"}}),
            _tool("update_plan", "更新你的长期计划（目标/下一步）",
                  {"goal": {**_S, "description": "当前目标"}, "next_step": {**_S, "description": "下一步行动"},
                   "progress": {**_S, "description": "当前进度描述"}}, ["goal", "next_step"]),
        ]
        core_exec = {"roll_dice": roll_dice, "modify_relationship": modify_relationship, "update_plan": update_plan}

        if agent_type == "npc_extended":
            extended_schemas = core_schemas + [
                need_info_schema,
                _tool("propose_event", "提议一个事件",
                      {"event_name": _S, "description": _S, "delay_turns": _I}, ["event_name", "description"]),
                _tool("give_item", "给玩家一件物品",
                      {"item_name": _S, "description": _S}, ["item_name"]),
                _tool("take_item", "从玩家收回一件物品", {"item_name": _S}),
                _tool("update_npc_relationship", "更新你与另一个NPC之间的关系",
                      {"from_npc": {**_S, "description": "发起关系的NPC ID"}, "to_npc": {**_S, "description": "对象NPC ID"},
                       "rel_type": {**_S, "description": "关系类型：友好/敌对/警惕/合作/竞争/中立"}, "description": {**_S, "description": "原因"}},
                      ["from_npc", "to_npc", "rel_type"]),
                _tool("share_information", "将你知道的某条信息分享出去",
                      {"fact": {**_S, "description": "信息内容"}, "known_by": {**_S, "description": "你的NPC ID"}}),
            ]
            return extended_schemas, {**core_exec, "need_info": need_info,
                                       "propose_event": propose_event, "give_item": give_item, "take_item": take_item,
                                       "update_npc_relationship": update_npc_relationship, "share_information": share_information}
        return core_schemas, core_exec

    elif agent_type == "outline":
        return [
            need_info_schema,
            _tool("apply_state_change", "应用状态变更",
                  {"var": {**_S, "description": "变量路径"}, "op": {**_S, "enum": ["add", "set"]},
                   "value": {"type": ["number", "string", "boolean"]}}),
            _tool("move_player", "移动玩家到某个地点", {"location_id": _S}),
            _tool("set_world_prop", "设置世界属性",
                  {"key": _S, "value": {"type": ["string", "number", "boolean"]}}),
            _tool("adjust_tension", "调节叙事紧张度（-30到+30）",
                  {"delta": {**_I, "description": "紧张度变化量"}}),
            _tool("change_faction_reputation", "修改阵营声望值（-20到+20）",
                  {"faction_id": {**_S, "description": "阵营ID"}, "delta": {**_I, "description": "声望变化量"},
                   "reason": {**_S, "description": "变化原因"}}, ["faction_id", "delta"]),
            _tool("request_re_reason", "请求NPC重新推理（限用1次）",
                  {"npc_id": {**_S, "description": "NPC ID"}, "reason": {**_S, "description": "原因"}}),
        ], {"need_info": need_info, "apply_state_change": apply_state_change, "move_player": move_player,
            "set_world_prop": set_world_prop, "adjust_tension": adjust_tension,
            "change_faction_reputation": change_faction_reputation, "request_re_reason": request_re_reason}

    elif agent_type == "scene":
        return [
            _tool("play_sound", "播放音效",
                  {"sound_type": {**_S, "enum": ["ambient", "tension", "wonder", "triumph", "sorrow"]}}),
            _tool("set_weather", "设置场景天气",
                  {"weather": {**_S, "enum": ["sunny", "cloudy", "rainy", "stormy", "night"]}}),
            _tool("set_mood_filter", "设置场景氛围",
                  {"mood": {**_S, "enum": ["calm", "tense", "mystical", "dark", "warm"]}}),
        ], {"play_sound": play_sound, "set_weather": set_weather, "set_mood_filter": set_mood_filter}

    return [], {}
