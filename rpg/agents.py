"""Agent 类：DirectorAgent, NPCAgent, OutlineAgent, ConflictDetector, SceneAgent, ContinuityValidator"""

import re
import json
from datetime import datetime
from typing import Dict, List, Any, Optional

from .llm_utils import llm_call, llm_call_with_tools, _parse_json_from_llm, _semantic_relationship, _semantic_attr_value
from .tool_system import _build_tool_schemas_and_executors
from .models import WorldStateManager


class DirectorAgent:
    """规划模型：理解玩家输入 → 分类/增强/路由。在 process_turn 之前执行。"""

    @staticmethod
    async def plan(
        raw_input: str,
        world_state: "WorldStateManager",
        script_data: dict,
        recent_turns: list,
    ) -> dict:
        sys_prompt, user_prompt = DirectorAgent._build_director_prompt(
            raw_input, world_state, script_data, recent_turns,
        )
        tools, executors = _build_tool_schemas_and_executors("director", world_state)
        raw, tool_calls = await llm_call_with_tools(sys_prompt, user_prompt, tools, executors, max_tokens=1200)
        parsed = _parse_json_from_llm(raw)
        if not parsed.get("intents"):
            return {"intents": [{"type": "game_action", "resolved_action": raw_input}], "reply_to_player": None, "tool_calls": tool_calls}
        parsed["tool_calls"] = tool_calls
        return parsed

    @staticmethod
    def _build_director_prompt(
        raw_input: str,
        world_state: "WorldStateManager",
        script_data: dict,
        recent_turns: list,
    ) -> tuple:
        npc_list = []
        for npc in script_data.get("npcs", []):
            npc_list.append(f"  - {npc['id']}: {npc.get('name', npc['id'])}")
        loc_list = []
        for loc in script_data.get("locations", []):
            loc_list.append(f"  - {loc['id']}: {loc.get('name', loc['id'])}")

        recent_summary = ""
        if recent_turns:
            lines = []
            for t in recent_turns[-3:]:
                lines.append(f"  轮次{t.turn_num}: {t.player_action}")
            recent_summary = "\n".join(lines)

        all_npc_ids = [n["id"] for n in script_data.get("npcs", [])]

        system_prompt = f"""你是RPG推演系统的调度员（Director）。你的任务是理解玩家的原始输入，进行分类、消解指代、增强描述，并路由到对应处理模块。

## 可识别的意图类型

- game_action: 游戏内行动（和NPC互动、移动、做事）
- ui_command: 前端UI操作（放大地图、显示面板、调字体等）
- meta_feedback: 对剧本或推演的反馈（人设不对、场景太短等）
- save_load: 存档/读档/回溯（保存、加载、回到上个节点等）
- query: 咨询/提问（我该干什么、这个NPC是谁、当前状况等）

## 你的增强能力

1. **指代消解**：如果玩家说"那个人"、"他"、"教练"等，根据上下文推断具体是哪个NPC
2. **地点推断**：如果玩家说"去训练"，推断目标地点
3. **NPC过滤**：标记本轮需要推理的NPC（target_npcs）和可以跳过的NPC（skip_npcs）
4. **情绪推断**：从输入推断玩家角色的mood（calm/anxious/angry/determined/playful/sad）
5. **时间推进**：根据行动性质判断时间步长（immediate/short/medium/half_day/full_day/next_day）
6. **焦点提示**：用focus_hint告诉NPC本轮核心话题
7. **NPC推理策略**：npc_strategies 为每个相关NPC分配推理策略：
   - aggressive: 积极参与，完整两轮推理（主要互动角色）
   - supportive: 辅助角色，仅一轮精简推理
   - minimal: 与场景无关，跳过推理
8. **流水线控制**：pipeline_hints 控制后续阶段是否执行：
   - skip_outline: true=跳过大纲分析（不涉及状态变更时）
   - skip_conflict: true=跳过冲突检测（≤1个NPC参与时）
   - skip_validator: true=跳过一致性校验（只涉及已知角色/地点时）
   - scene_budget: "short"(300字) / "normal"(600字) / "long"(1000字)

   决策参考：
   - "去训练场" → skip_outline=true, skip_conflict=true, scene_budget="short"
   - "和教练聊聊" → skip_conflict=true, skip_validator=true, scene_budget="normal"
   - "和三方谈判" → 全部false, scene_budget="long"
   - "在已知地点做日常" → skip_validator=true, scene_budget="short"

## 可用NPC

{chr(10).join(npc_list)}

## 可用地点

{chr(10).join(loc_list)}

## 输出格式

返回纯JSON：
{{
  "intents": [
    {{
      "type": "game_action",
      "resolved_action": "增强后的行动描述（将模糊输入具体化）",
      "target_npcs": ["相关NPC的id"],
      "target_location": "目标地点id或null",
      "mood": "calm|anxious|angry|determined|playful|sad",
      "time_hint": "immediate|short|medium|half_day|full_day|next_day",
      "skip_npcs": ["本轮不需要推理的NPC id"],
      "focus_hint": "本轮核心话题（一句话）",
      "npc_strategies": {{"npc_id": "aggressive|supportive|minimal"}},
      "pipeline_hints": {{
        "skip_outline": false,
        "skip_conflict": false,
        "skip_validator": false,
        "scene_budget": "normal"
      }}
    }}
  ],
  "reply_to_player": null
}}

如果是非游戏请求：
{{
  "intents": [
    {{
      "type": "query",
      "answer": "你的回答"
    }}
  ],
  "reply_to_player": "你的回答"
}}

混合输入则返回多个intent。

## 约束

- 不要发明游戏内容，只做理解和路由
- resolved_action 应该比原始输入更具体，但不要编造玩家没表达的意图
- skip_npcs 应该排除与本轮行动明显无关的NPC
- 当剧情需要新角色登场时，使用 spawn_npc 工具创建
- 当角色退场时，使用 remove_npc 工具移除
- 当需要触发叙事事件时，使用 trigger_event 工具
- 如果无法判断意图，默认为 game_action，resolved_action = 原始输入"""

        user_prompt = f"""## 当前世界状态

轮次: {world_state.current_turn}
时间: {world_state.current_date.isoformat()}
地点: {world_state.current_location}

## 最近推演

{recent_summary if recent_summary else "（游戏刚开始）"}

## 玩家输入

{raw_input}"""

        return system_prompt, user_prompt

    @staticmethod
    def plan_rules(raw_input: str, world_state: "WorldStateManager") -> dict:
        text = raw_input.strip()
        import re
        if re.search(r'存.*档|保存|save', text, re.IGNORECASE):
            return {"intents": [{"type": "save_load", "action": "save"}], "reply_to_player": None}
        if re.search(r'读.*档|加载|load|回.*档|回到', text, re.IGNORECASE):
            return {"intents": [{"type": "save_load", "action": "load"}], "reply_to_player": None}
        if any(kw in text for kw in ("放大", "缩小", "字体", "主题", "面板")):
            return {"intents": [{"type": "ui_command", "command": "toggle_panel", "params": {}}], "reply_to_player": None}
        return {"intents": [{"type": "game_action", "resolved_action": text}], "reply_to_player": None}


class NPCAgent:
    """单个NPC Agent的推理和决策"""

    def __init__(self, npc_id: str, npc_data: Dict[str, Any], world_state: WorldStateManager):
        self.npc_id = npc_id
        self.npc_data = npc_data
        self.world_state = world_state
        self.name = npc_data.get("name", "Unknown")
        self.personality = npc_data.get("personality", "")

    def reason(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """NPC推理入口（仅规则兜底，LLM模式通过 _reason_llm 调用）"""
        return self._reason_rules(context)

    async def _reason_intent(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """第一轮：轻量意图推理，仅输出行动倾向和立场，无工具调用"""
        rel_value = self.world_state.relationships.get(self.npc_id, 0)
        visible_info = self._gather_visible_info(context)
        user_action = context.get("user_action", "")

        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        personality = npc.get("personality", "")
        goals = npc.get("goals", [])
        goals_text = "、".join(goals[:3]) if goals else "无"

        system_prompt = f"""你是NPC「{name}」。性格：{personality}。目标：{goals_text}。{_semantic_relationship("玩家", rel_value)}。
快速判断你对玩家行动的立场。只返回 JSON，不要其他文本。"""

        loc_name = visible_info.get("current_location", "?")
        for loc in self.world_state.script_data.get("locations", []):
            if loc.get("id") == loc_name:
                loc_name = loc.get("name", loc_name)
                break

        user_prompt = f"地点：{loc_name}\n玩家行动：{user_action}\n\n返回 JSON：{{\"action_type\": \"主动联系|被动反应|主动谈判|观望|警告|冲突\", \"stance\": \"一句话说明你的立场和意图（不超过20字）\"}}"

        raw = await llm_call(system_prompt, user_prompt, max_tokens=300)
        parsed = _parse_json_from_llm(raw)

        return {
            "npc_id": self.npc_id,
            "name": self.name,
            "action_type": parsed.get("action_type", "观望"),
            "stance": parsed.get("stance", ""),
        }

    async def _reason_llm(self, context: Dict[str, Any], session=None,
                          intent_context: list = None, strategy: str = "aggressive") -> Dict[str, Any]:
        """第二轮：完整推理，注入其他NPC的意图作为跨角色感知"""
        npc = self.npc_data
        user_action = context.get("user_action", "")
        history = context.get("history", {})
        rel_value = self.world_state.relationships.get(self.npc_id, 0)

        visible_info = self._gather_visible_info(context)
        system_prompt = self._build_npc_system_prompt(rel_value)
        user_prompt = self._build_npc_user_prompt(user_action, visible_info, context)

        if intent_context:
            others = [i for i in intent_context if i["npc_id"] != self.npc_id and i["action_type"] != "观望"]
            if others:
                lines = [f"- {i['name']}：{i['action_type']}（{i['stance']}）" for i in others]
                user_prompt += f"\n\n## 你察觉到的其他角色动向\n\n" + "\n".join(lines) + "\n\n注意：这些是你的主观感知，可能不完全准确。根据这些信息调整你的行动。"

        tool_type = "npc_extended" if strategy == "aggressive" else "npc"
        tools, executors = _build_tool_schemas_and_executors(tool_type, self.world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=2000)
        parsed = _parse_json_from_llm(raw)

        return {
            "agent_id": self.npc_id,
            "name": self.name,
            "thought": parsed.get("thought", ""),
            "emotion": parsed.get("emotion", ""),
            "action_type": parsed.get("action_type", "观望"),
            "dialogue": parsed.get("dialogue"),
            "side_effects": [],
            "uncertainty": parsed.get("uncertainty", []),
            "tool_calls": tool_calls,
        }

    def _gather_visible_info(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """收集 NPC 能看到的信息（有限视角）"""
        history = context.get("history", {})
        visible = {
            "current_location": self.world_state.current_location,
            "current_date": self.world_state.current_date.isoformat(),
            "relationship_with_player": self.world_state.relationships.get(self.npc_id, 0),
        }
        recent = history.get("recent_actions", [])[-3:]
        if recent:
            visible["recent_player_actions"] = recent
        return visible

    def _build_npc_system_prompt(self, rel_value: int) -> str:
        """构建强化的 NPC 系统提示（采用酒馆的清晰约束和示例）"""
        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        title = npc.get("title", "")
        personality = npc.get("personality", "")
        goals = npc.get("goals", [])
        backstory = npc.get("backstory", "")

        goals_text = "\n".join(f"  - {g}" for g in goals) if goals else "  （无特定目标）"

        prompt = f"""你是这个RPG推演系统中的一个NPC角色。

## 角色身份

姓名：{name}
身份/职位：{title if title else "（无特定身份）"}
性格特点：{personality if personality else "（待定）"}
你与玩家的关系值：{rel_value}/100

## 角色目标和动机

{goals_text}

## 背景故事

{backstory if backstory else "（背景待补充）"}

## 推理约束和指导

1. **有限视角**：你只知道这个NPC能知道的东西
   - 不可知道其他NPC的真实想法或行动
   - 不可知道玩家未明确告诉你的事情
   - 只能基于可观察的现象进行推断

2. **言辞真实**：
   - 用这个NPC的习惯表达方式说话
   - 如果不确定，要明确表达不确定（"我不太清楚"）
   - 对话要符合你的身份和当前关系

3. **行动选择**：选择最符合你的目标和当前局势的行动
   - 主动联系：主动找玩家或其他NPC交互
   - 被动反应：对玩家行动的反应
   - 主动谈判：提出某个交易或条件
   - 观望：在这个回合不采取行动
   - 警告：表达不满或担忧
   - 冲突：产生对抗或争执

4. **关系变化**：改变幅度应与行动强度匹配（-5到+5是常规范围）

5. **不可知信息示例**：
   ✗ 错误："其他NPC正在..."（你不能知道其他NPC的行动）
   ✓ 正确："我听到了..."或"我推断..."（基于可观察的信息）

## 输出格式

返回纯 JSON，不要有其他文本。必须包含以下字段：
{{
  "thought": "你对当前局势的内心分析（1-2句，不超过50字）",
  "emotion": "你当前的情绪状态（1-2个词）",
  "action_type": "主动联系|被动反应|主动谈判|观望|警告|冲突 之一",
  "dialogue": "你要说的话（不说话则为 null，对话必须符合身份）",
  "uncertainty": ["不确定的事项1", "不确定的事项2"]
}}

注意：所有状态修改（关系变化、物品给予等）请使用工具完成，不要在 JSON 中添加。"""
        return prompt

    def _build_npc_user_prompt(self, user_action: str, visible_info: Dict[str, Any],
                                context: Dict[str, Any]) -> str:
        """构建 NPC 用户提示"""
        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        rel_value = visible_info.get("relationship_with_player", 0)

        recent_acts = visible_info.get("recent_player_actions", [])
        if recent_acts:
            history_text = "\n".join(f"  - {a}" for a in recent_acts[-2:])
        else:
            history_text = "  （这是本次推演的开始）"

        loc_id = visible_info.get("current_location", "")
        loc_name = loc_id
        for loc in self.world_state.script_data.get("locations", []):
            if loc.get("id") == loc_id:
                loc_name = loc.get("name", loc_id)
                break

        prompt = f"""## 当前游戏状态

时间：{visible_info.get("current_date", "?")}
地点：{loc_name}
你与玩家的关系：{_semantic_relationship("玩家", rel_value)}

## 最近发生的事情

{history_text}

## 玩家的行动

{user_action}

## 任务
"""
        my_ctx = context.get("npc_contexts", {}).get(self.npc_id, {})
        shared = context.get("shared", {})

        # 核心段（不计入预算）：记忆
        npc_mem = my_ctx.get("memories", [])
        if npc_mem:
            prompt += "\n\n## 你的近期记忆\n\n" + "\n".join(f"- {m}" for m in npc_mem)

        # 扩展段按优先级排列，总预算 1500 字
        budget = 1500
        extensions = []

        # 扩展1 (高优先): 日程+计划+弧线 合并
        status_parts = []
        if my_ctx.get("agenda"):
            status_parts.append(f"日程：{my_ctx['agenda']}")
        npc_plan = my_ctx.get("plan")
        if npc_plan:
            status_parts.append(f"目标：{npc_plan.get('goal', '?')}，下一步：{npc_plan.get('next_step', '?')}")
        emotion_arc = my_ctx.get("emotion_arc", [])
        if len(emotion_arc) >= 3:
            status_parts.append(f"情绪：{'→'.join(emotion_arc[-5:])}")
        if status_parts:
            block = "\n".join(status_parts)
            extensions.append(("## 你的当前状态\n\n" + block, len(block)))

        # 扩展2: lorebook (截断到 500字)
        lore = shared.get("lorebook", "")
        if lore:
            truncated = lore[:500] + ("..." if len(lore) > 500 else "")
            extensions.append(("## 相关背景知识\n\n" + truncated, len(truncated)))

        # 扩展3: 事件
        ev = shared.get("event_inject", "")
        if ev:
            truncated = ev[:400]
            extensions.append(("## 当前事件\n\n" + truncated, len(truncated)))

        # 扩展4: NPC间关系
        if my_ctx.get("relationships"):
            extensions.append((my_ctx["relationships"], len(my_ctx["relationships"])))

        # 扩展5: 知道的信息
        if my_ctx.get("knowledge"):
            block = "; ".join(my_ctx["knowledge"])
            extensions.append(("## 你知道的信息\n\n" + block, len(block)))

        # 扩展6: 语义记忆（历史相关片段）
        sem = context.get("semantic_history", [])
        if sem:
            sem_lines = [s.get("text", s) if isinstance(s, dict) else str(s) for s in sem[:5]]
            block = "\n".join(f"- {l[:200]}" for l in sem_lines if l)
            if block:
                extensions.append(("## 相关历史片段\n\n" + block, len(block)))

        # 扩展7: 焦点
        focus = context.get("director", {}).get("focus_hint", "")
        if focus:
            extensions.append(("## 本轮焦点\n\n" + focus, len(focus)))

        # 按预算裁剪
        used = 0
        for text, size in extensions:
            if used + size > budget:
                break
            prompt += f"\n\n{text}"
            used += size

        if npc_plan:
            prompt += "\n如果情况有变，可以用 update_plan 工具调整你的计划。"
        prompt += f"""
以 {name} 的身份，根据上述信息进行推理和反应。注意：
- 只使用你能看到或推断的信息
- 明确表达你不知道的事物
- 选择最符合你目标和性格的行动"""
        return prompt

    def _reason_rules(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """泛化规则推理：从 NPC 数据动态生成反应"""
        output = {
            "agent_id": self.npc_id,
            "name": self.name,
            "thought": "",
            "emotion": "",
            "action_type": "观望",
            "dialogue": None,
            "side_effects": [],
            "uncertainty": []
        }

        user_action = context.get("user_action", "")
        npc = self.npc_data
        attitude = npc.get("attitude_toward_player", 50)
        caps = npc.get("capabilities", "")
        name = npc.get("name", "")

        mentioned = bool(name and name in user_action)

        relevant, matched_cap = False, ""
        if caps:
            for kw in re.split(r'[、,，。；;和]', caps):
                kw = kw.strip()
                if len(kw) >= 2 and kw in user_action:
                    relevant, matched_cap = True, kw
                    break

        if not mentioned and not relevant:
            output["thought"] = "这件事与我无关。"
            return output

        if attitude >= 70:
            output["emotion"] = "积极"
            output["action_type"] = "主动互动" if mentioned else "被动支持"
            if mentioned:
                output["dialogue"] = "你找我？我很乐意帮忙。"
            else:
                output["thought"] = "我注意到了，如果需要我会帮忙。"
            rel_delta = 1
        elif attitude >= 40:
            output["emotion"] = "平静"
            output["action_type"] = "被动反应" if mentioned else "观望"
            if mentioned:
                output["dialogue"] = "嗯，什么事？"
            rel_delta = 0
        else:
            output["emotion"] = "冷淡"
            output["action_type"] = "警告" if mentioned else "观望"
            if mentioned:
                output["dialogue"] = "你来找我？我们之间好像有些问题。"
            else:
                output["thought"] = "我对此不太感兴趣。"
            rel_delta = -1

        if rel_delta:
            output.setdefault("tool_calls", []).append({
                "name": "modify_relationship",
                "args": {"npc_id": self.npc_id, "delta": rel_delta},
                "result": {"type": "modify_relationship", "npc_id": self.npc_id, "delta": rel_delta, "status": "proposed"},
            })

        if matched_cap:
            output["thought"] = f"这涉及到{matched_cap}，正好是我的领域。"

        return output


class OutlineAgent:
    """大纲 Agent：综合 NPC 输出，提议状态变更"""

    @staticmethod
    async def synthesize_llm(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        session = None,
    ) -> Dict[str, Any]:
        # 精简 NPC 摘要
        npc_brief_lines = []
        npc_tool_calls_summary = []
        for npc_id, out in npc_outputs.items():
            if out.get("skipped"):
                continue
            line = f"- {out.get('name', npc_id)}: {out.get('action_type', '观望')}"
            if out.get("dialogue"):
                d = out["dialogue"]
                line += f"，说：「{d[:120]}{'...' if len(d) > 120 else ''}」"
            npc_brief_lines.append(line)
            for tc in out.get("tool_calls", []):
                npc_tool_calls_summary.append(f"- {out.get('name', npc_id)}: {tc.get('name', '')}({json.dumps(tc.get('args', {}), ensure_ascii=False)})")
        npc_text = "\n".join(npc_brief_lines) if npc_brief_lines else "（无NPC参与）"

        system_prompt = f"""你是RPG推演系统的大纲分析师（Outline Agent）。
你的任务是综合各NPC的行动结果，进行事实总结和状态变更提议。

## 职责

1. **事实总结**：用1-2句话总结本轮发生了什么（只陈述事实，无修辞）
2. **状态变更**：使用工具提议状态变更（移动玩家、修改属性等），玩家确认后生效

## 状态变更工具（confirm后才执行）

- apply_state_change: 修改玩家属性或关系值
- move_player: 移动玩家到新地点
- set_world_prop: 修改世界属性
- adjust_tension: 调节叙事紧张度（高紧张→快节奏，低紧张→慢节奏）
- change_faction_reputation: 修改玩家在某阵营的声望值
- request_re_reason: 当NPC输出存在严重矛盾时，请求该NPC重新推理（限用1次，仅在严重矛盾时使用）

## 环境修改

可通过 set_world_prop 设置环境参数（如 weather=stormy, lighting=dim, atmosphere=tense），SceneAgent会据此调整描写。

## 约束

- 不可凭空发明未由NPC提议的事件
- 状态变更必须有合理依据

## 输出格式

在使用工具后，返回纯JSON：
{{
  "summary": "本轮发生了什么（1-2句事实陈述）",
  "next_phase": "下一步应该发生什么"
}}"""

        user_prompt = f"""玩家行动：{context.get('user_action', '')}
当前位置：{world_state.current_location}
轮次：{world_state.current_turn}
日期：{world_state.current_date.isoformat()}

各NPC的行动：
{npc_text}
"""
        if npc_tool_calls_summary:
            user_prompt += "\nNPC已执行的工具调用：\n" + "\n".join(npc_tool_calls_summary) + "\n"

        history = context.get("history", {})
        recent_actions = history.get("recent_actions", [])
        if recent_actions:
            user_prompt += "\n## 近期历史\n\n" + "\n".join(f"- {a}" for a in recent_actions[-5:]) + "\n"

        faction_rep = context.get("shared", {}).get("faction_reputation")
        if faction_rep:
            rep_lines = [f"- {fid}: {fd.get('title','中立')}({fd.get('value',50)})" for fid, fd in faction_rep.items()]
            user_prompt += "\n## 阵营声望\n\n" + "\n".join(rep_lines) + "\n"

        sem = context.get("semantic_history", [])
        if sem:
            sem_lines = [s.get("text", s) if isinstance(s, dict) else str(s) for s in sem[:5]]
            sem_text = "\n".join(f"- {l[:200]}" for l in sem_lines if l)
            if sem_text:
                user_prompt += "\n## 相关历史记忆\n\n" + sem_text + "\n"

        # 混合供给：Push lorebook 到 OutlineAgent
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            user_prompt += f"\n## 相关背景知识\n{lore[:500]}\n"

        user_prompt += "\n请综合以上信息，使用工具提议状态变更，然后返回分析结果。"

        tools, executors = _build_tool_schemas_and_executors("outline", world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=1500)
        parsed = _parse_json_from_llm(raw)

        re_reason_requests = [
            tc["result"] for tc in tool_calls
            if tc.get("result", {}).get("type") == "request_re_reason"
        ]

        return {
            "summary": parsed.get("summary", ""),
            "next_phase": parsed.get("next_phase", ""),
            "tool_calls": [tc for tc in tool_calls if tc.get("result", {}).get("type") != "request_re_reason"],
            "re_reason_requests": re_reason_requests[:1],
        }

    @staticmethod
    def synthesize_rules(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
    ) -> Dict[str, Any]:
        active_npcs = []
        for npc_id, output in npc_outputs.items():
            if output.get("action_type") not in ("观望", "无关"):
                active_npcs.append({
                    "npc_id": npc_id,
                    "name": output.get("name"),
                    "action": output.get("action_type"),
                })

        if active_npcs:
            names = [npc["name"] for npc in active_npcs[:2]]
            summary = f"本轮有{len(active_npcs)}个关键人物参与：{', '.join(names)}等。"
        else:
            summary = "本轮没有重要事件发生。"

        all_tool_calls = []
        for npc_id, out in npc_outputs.items():
            all_tool_calls.extend(out.get("tool_calls", []))

        return {
            "summary": summary,
            "next_phase": "等待玩家做出选择或继续推进故事",
            "tool_calls": all_tool_calls,
        }


class ConflictDetector:
    """轻量冲突检测 Agent：分析 NPC 行动之间的对立关系"""

    @staticmethod
    async def detect(npc_outputs: dict) -> list:
        active = {k: v for k, v in npc_outputs.items()
                  if v.get("action_type") not in ("观望", "无关") and not v.get("skipped")}
        if len(active) < 2:
            return []

        npc_brief = []
        for npc_id, out in active.items():
            line = f"{out.get('name', npc_id)}: {out.get('action_type')}"
            if out.get("dialogue"):
                line += f"「{out['dialogue'][:80]}」"
            if out.get("thought"):
                line += f"（想：{out['thought'][:60]}）"
            npc_brief.append(line)

        system_prompt = "你是冲突分析师。判断以下NPC行动之间是否存在对立或冲突。只返回JSON。"
        user_prompt = (
            "NPC行动：\n" + "\n".join(npc_brief) +
            '\n\n返回：{"conflicts": [{"between": ["id1","id2"], "severity": "high|medium|low", "topic": "冲突主题"}]}'
            "\n规则：目标直接对立=high，立场不同但未正面冲突=medium，仅有潜在分歧=low。无冲突则返回空列表。"
        )
        try:
            raw = await llm_call(system_prompt, user_prompt, max_tokens=500)
            parsed = _parse_json_from_llm(raw)
            return parsed.get("conflicts", [])
        except Exception:
            return ConflictDetector.detect_rules(npc_outputs)

    @staticmethod
    def detect_rules(npc_outputs: dict) -> list:
        _OPPOSING = {"冲突", "警告", "威胁", "攻击"}
        conflicts = []
        ids = [k for k, v in npc_outputs.items()
               if v.get("action_type") in _OPPOSING and not v.get("skipped")]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                conflicts.append({
                    "between": [a, b],
                    "severity": "high" if npc_outputs[a]["action_type"] == "冲突"
                                       and npc_outputs[b]["action_type"] == "冲突" else "medium",
                    "topic": "",
                })
        return conflicts


class SceneAgent:
    """场景 Agent：基于大纲分析结果，生成沉浸式场景描写和选择分支"""

    @staticmethod
    async def generate_llm(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        outline: Dict[str, Any],
        session = None,
        scene_budget: str = "normal",
    ) -> Dict[str, Any]:
        _BUDGET_MAP = {"short": (150, 300, 1200), "normal": (300, 600, 2000), "long": (500, 1000, 3000)}
        min_len, max_len, max_tokens = _BUDGET_MAP.get(scene_budget, _BUDGET_MAP["normal"])
        # 精简 NPC 摘要
        npc_brief_lines = []
        for npc_id, out in npc_outputs.items():
            if out.get("skipped"):
                continue
            line = f"- {out.get('name', npc_id)}: {out.get('action_type', '观望')}"
            if out.get("dialogue"):
                d = out["dialogue"]
                line += f"，说：「{d[:120]}{'...' if len(d) > 120 else ''}」"
            elif out.get("emotion"):
                line += f"（{out['emotion']}）"
            npc_brief_lines.append(line)
        npc_text = "\n".join(npc_brief_lines) if npc_brief_lines else "（无NPC参与）"

        system_prompt = f"""你是RPG游戏的场景叙事师（Scene Agent）。
你需要基于大纲分析结果，生成沉浸式的场景描写和选择分支。

## 1. 生成场景描写

- **视角**：第二人称（"你"）
- **长度**：{min_len}-{max_len}字，精炼有力，融入感官细节和NPC微表情
- **融合**：自然融入NPC对话和行动后果
- **约束**：只描写当前场景，不跳出当前时空

## 2. 生成选择分支

- **数量**：3-4个选择
- **多样性**：不同策略和角色反应
- **后果清晰**：每个选择的immediate效果明确
- **影响平衡**：没有明显的"最优解"
- **具体性**：选择必须基于当前地点特征、在场人物、玩家属性和持有物品来设计，不给出"继续探索""看看周围"等泛化选项

## UI效果工具（立即执行）

- play_sound: 播放音效（ambient, tension, wonder, triumph, sorrow）
- set_weather: 设置天气（sunny, cloudy, rainy, stormy, night）
- set_mood_filter: 设置滤镜（calm, tense, mystical, dark, warm）

## 输出格式

在使用工具后，返回纯JSON：
{{"scene_text": "沉浸式场景描写（含感官细节和NPC微表情）", "atmosphere": "一句话概括本轮氛围", "choices": [{{"id": "A", "text": "选择内容", "effects": {{...}}}}]}}

注意：
- effects 可含字段：relationship_changes, skill_check, resource_cost, delayed_consequence
- skill_check 仅在该选择涉及有风险或不确定性的行动时才加（攻击、偷窃、说服等），日常对话不需要
- resource_cost 仅在该选择消耗玩家资源时才加（体力、金币等），为负值表示消耗
- delayed_consequence 仅在该选择会产生后续影响时才加，描述几回合后可能发生的后果
- 不是所有选择都需要这些字段，根据叙事合理性决定"""

        mood = context.get("director", {}).get("mood", "")
        mood_line = f"\n玩家情绪氛围：{mood}" if mood else ""
        pacing = context.get("pacing_hint", "")
        pacing_line = f"\n\n## 节奏指导\n{pacing}" if pacing else ""

        hijack_line = ""
        hijack = context.get("scene_hijack")
        if hijack:
            hijack_line = (
                f"\n\n## 场景劫持\n本回合由NPC主导场景。{hijack.get('npc_name', '')}因「{hijack.get('reason', '')}」"
                f"打断玩家行动。叙事焦点转移到NPC的主动行为上。\n"
                f"建议场景: {hijack.get('suggested_action', '')}"
            )

        plan_line = ""
        if context.get("has_plan_declaration"):
            plan_line = (
                "\n\n## 计划分解\n"
                "玩家声明了一个多步骤计划。在场景描写后，额外在choices中添加计划步骤拆解。"
                "将计划分解为3-5个具体可执行步骤作为选择分支。"
            )

        outline_summary = outline.get("summary", "")
        outline_conflicts = json.dumps(outline.get("conflicts", []), ensure_ascii=False)

        # 把 outline 的工具提议摘要注入 SceneAgent
        outline_actions_line = ""
        outline_tcs = outline.get("tool_calls", [])
        if outline_tcs:
            action_descs = []
            for tc in outline_tcs[:5]:
                r = tc.get("result", {})
                t = r.get("type", tc.get("name", ""))
                if t == "move_player":
                    action_descs.append(f"移动到{r.get('location_id', '?')}")
                elif t == "apply_state_change":
                    action_descs.append(f"{r.get('var', '?')}{r.get('op', 'set')}{r.get('value', '')}")
                elif t == "set_world_prop":
                    action_descs.append(f"世界属性变更：{r.get('key', '?')}={r.get('value', '')}")
                elif t == "spawn_npc":
                    action_descs.append(f"新角色登场：{r.get('name', '?')}")
                elif t == "change_faction_reputation":
                    action_descs.append(f"声望变化：{r.get('faction_id', '?')}{r.get('delta', 0):+d}")
                elif t == "adjust_tension":
                    action_descs.append(f"紧张度调整{r.get('delta', 0):+d}")
                else:
                    action_descs.append(t)
            if action_descs:
                outline_actions_line = f"\n状态变更提议：{'、'.join(action_descs)}（请将这些变化自然融入叙事中）"

        # 环境参数注入
        env_line = ""
        wp = world_state.world_props
        env_params = {k: v for k, v in wp.items() if k in ("weather", "lighting", "atmosphere", "season", "time_of_day", "天气", "光照", "氛围", "季节")}
        if env_params:
            env_line = "\n环境：" + "、".join(f"{k}={v}" for k, v in env_params.items())

        user_prompt = f"""玩家行动：{context.get('user_action', '')}
当前位置：{world_state.current_location}
轮次：{world_state.current_turn}
日期：{world_state.current_date.isoformat()}{env_line}{mood_line}{pacing_line}{hijack_line}{plan_line}

大纲分析：{outline_summary}{outline_actions_line}
冲突：{outline_conflicts}

各NPC的行动：
{npc_text}
"""
        passive_checks = context.get("passive_checks")
        if passive_checks:
            labels = {"success": "成功", "failure": "失败", "critical_success": "大成功", "critical_failure": "大失败"}
            check_lines = [f"- {pc['trigger_npc']}试图{pc['action_type']}，玩家{labels.get(pc['result']['outcome'], '?')}"
                           for pc in passive_checks]
            user_prompt += "\n## 被动检定\n\n" + "\n".join(check_lines) + "\n（成功=玩家察觉，失败=浑然不觉，融入叙事中）\n"

        npc_dialogues = context.get("npc_dialogues")
        if npc_dialogues:
            for nd in npc_dialogues:
                user_prompt += f"\n## {nd['npc_a_name']}与{nd['npc_b_name']}的冲突对话\n\n{nd['dialogue']}\n"
            user_prompt += "（请将以上NPC间的对话自然融入场景叙事中）\n"

        # 玩家具体上下文
        if world_state.player_attrs:
            attr_defs = world_state.script_data.get("player_character", {}).get("attributes", {})
            attrs_lines = []
            for k, v in list(world_state.player_attrs.items())[:8]:
                attrs_lines.append(_semantic_attr_value(k, v, attr_defs.get(k)))
            user_prompt += f"\n玩家属性：\n" + "\n".join(attrs_lines) + "\n"
        inventory = world_state.variables.get("inventory", [])
        if inventory:
            inv_names = [i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in inventory[:8]]
            user_prompt += f"玩家背包：{'、'.join(inv_names)}\n"
        loc_id = world_state.current_location
        loc_data = next((l for l in world_state.script_data.get("locations", []) if l.get("id") == loc_id), None)
        if loc_data and loc_data.get("description"):
            user_prompt += f"地点「{loc_data.get('name', loc_id)}」：{loc_data['description'][:300]}\n"
        present_npcs = []
        for npc_id, out in npc_outputs.items():
            if not out.get("skipped"):
                rel = world_state.relationships.get(npc_id, 0)
                present_npcs.append(_semantic_relationship(out.get('name', npc_id), rel))
        if present_npcs:
            user_prompt += f"在场角色：{'、'.join(present_npcs)}\n"

        user_prompt += "\n请基于以上信息，使用UI效果工具增强氛围，然后生成场景描写和选择分支。"

        # 混合供给：Push lorebook 和 world_props
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            user_prompt += f"\n\n## 相关背景知识\n{lore[:800]}\n"
        if world_state.world_props:
            wp_defs = {wp.get("id"): wp for wp in world_state.script_data.get("world_properties", []) if isinstance(wp, dict)}
            wp_lines = []
            for k, v in world_state.world_props.items():
                wp_def = wp_defs.get(k, {})
                wp_name = wp_def.get("name", k)
                rule = wp_def.get("rule", "")
                if rule:
                    wp_lines.append(f"{wp_name}={v} — {rule[:60]}")
                else:
                    wp_lines.append(f"{wp_name}={v}")
            user_prompt += f"\n世界状态：\n" + "\n".join(wp_lines) + "\n"

        tools, executors = _build_tool_schemas_and_executors("scene", world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=max_tokens)
        parsed = _parse_json_from_llm(raw)

        dialogues = []
        for npc_id, out in npc_outputs.items():
            if out.get("dialogue"):
                dialogues.append({"speaker": out.get("name"), "text": out["dialogue"]})

        return {
            "turn": world_state.current_turn,
            "timestamp": datetime.now().isoformat(),
            "location": world_state.current_location,
            "scene_text": parsed.get("scene_text", ""),
            "atmosphere": parsed.get("atmosphere", ""),
            "state_snapshot": world_state.get_snapshot(),
            "active_agents": [npc_id for npc_id, out in npc_outputs.items()
                             if out.get("action_type") not in ("观望", "无关")],
            "dialogue_log": dialogues,
            "choices": parsed.get("choices", []),
            "ui_effects": [{"type": tc["name"], "params": tc["args"], "result": tc["result"]} for tc in tool_calls],
        }

    @staticmethod
    def generate_rules(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        outline: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        scene_text = "故事继续推进，新的一页即将开启..."

        dialogues = []
        for npc_id, output in npc_outputs.items():
            if output.get("dialogue"):
                dialogues.append({"speaker": output.get("name"), "text": output["dialogue"]})

        choices = [
            {"id": "A", "text": "积极回应，表达真实想法", "preview": "这会增加部分NPC好感，但可能激化冲突", "effects": {"relationship_changes": {}}},
            {"id": "B", "text": "保守回应，暂时搁置决定", "preview": "保持平稳，但风险积累", "effects": {"relationship_changes": {}}},
            {"id": "C", "text": "转移话题，避免深谈", "preview": "短期回避，但被察觉风险高", "effects": {"relationship_changes": {}}},
        ]

        return {
            "turn": world_state.current_turn,
            "timestamp": datetime.now().isoformat(),
            "location": world_state.current_location,
            "scene_text": scene_text,
            "atmosphere": "",
            "state_snapshot": world_state.get_snapshot(),
            "active_agents": [npc_id for npc_id, out in npc_outputs.items()
                             if out.get("action_type") not in ("观望", "无关")],
            "dialogue_log": dialogues,
            "choices": choices,
            "ui_effects": [],
        }


class ContinuityValidator:
    """后验质量校验：检查场景叙事与世界状态的一致性"""

    @staticmethod
    async def validate(scene_text: str, world_state: "WorldStateManager",
                       script_data: dict) -> dict:
        existing_npcs = [n.get("name", "") for n in script_data.get("npcs", [])]
        existing_locs = [l.get("name", "") for l in script_data.get("locations", [])]
        player_attrs_brief = ", ".join(
            f"{k}={v}" for k, v in list(world_state.player_attrs.items())[:10]
        )

        system_prompt = "你是RPG世界一致性校验员。检查叙事文本与世界状态是否矛盾。只返回JSON。"
        user_prompt = f"""叙事文本：
{scene_text[:1200]}

已有角色：{', '.join(n for n in existing_npcs if n) or '无'}
已有地点：{', '.join(n for n in existing_locs if n) or '无'}
玩家属性：{player_attrs_brief}
当前位置：{world_state.current_location}

返回：
{{
  "needs_expansion": true/false,
  "new_names": ["叙事中出现但不在已有列表中的角色/地点名"],
  "inconsistencies": ["叙事与世界状态的矛盾（如位置不对、属性不符）"]
}}
不矛盾则 inconsistencies 为空。无新名字则 needs_expansion=false。"""
        try:
            raw = await llm_call(system_prompt, user_prompt, max_tokens=600)
            return _parse_json_from_llm(raw) or {"needs_expansion": False, "inconsistencies": []}
        except Exception:
            return ContinuityValidator.validate_rules(scene_text, script_data)

    @staticmethod
    def validate_rules(scene_text: str, script_data: dict) -> dict:
        existing = {n.get("name", "") for n in script_data.get("npcs", [])} | \
                   {l.get("name", "") for l in script_data.get("locations", [])}
        quoted = re.findall(r'「(.+?)」|"(.+?)"', scene_text)
        new_names = []
        for tup in quoted:
            name = tup[0] or tup[1]
            if name and len(name) >= 2 and name not in existing:
                new_names.append(name)
        return {"needs_expansion": bool(new_names), "new_names": new_names, "inconsistencies": []}
