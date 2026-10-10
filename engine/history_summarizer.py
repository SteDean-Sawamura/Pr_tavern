"""History summarizer: compresses old game turns into layered summaries to save tokens."""

import asyncio
import json
import re


class HistorySummarizer:
    """When game history grows too long, use AI to compress older turns
    into a layered summary: key events list (structured) + narrative summary."""

    def __init__(self, threshold: int = 10, keep_recent: int = 5, word_threshold: int = 3000):
        self.threshold = threshold
        self.keep_recent = keep_recent
        self.word_threshold = word_threshold

    def needs_summary(self, turn_number: int, current_state: dict, recent_word_count: int = 0) -> bool:
        if turn_number < self.threshold:
            return False
        existing = current_state.get("history_summary", "")
        last_summarized = current_state.get("last_summarized_turn", 0)
        turn_trigger = (turn_number - last_summarized) >= self.threshold
        word_trigger = self.word_threshold > 0 and recent_word_count >= self.word_threshold
        if not turn_trigger and not word_trigger:
            return False
        # 保护窗口：紧张剧情进行中时延迟摘要
        scene = current_state.get("scene_details", {})
        if scene.get("pending_tension"):
            return False
        active_states = current_state.get("active_persistent_states", [])
        protection_keywords = ("combat", "battle", "战斗", "紧急", "emergency", "boss")
        if any(any(kw in sid for kw in protection_keywords) for sid in active_states):
            return False
        return True

    def build_summary_prompt(
        self, older_nodes: list[dict], existing_summary: str
    ) -> tuple[str, str]:
        parts = []

        if existing_summary:
            parts.append(f"之前的剧情摘要:\n{existing_summary}")

        parts.append("以下是需要压缩的近期剧情:")
        for node in older_nodes:
            turn = node.get("turn_number", 0)
            action = node.get("player_action")
            action_text = ""
            if action:
                action_text = (
                    action.get("text", "")
                    if isinstance(action, dict)
                    else str(action)
                )
            response = node.get("ai_response", "")
            if len(response) > 500:
                response = response[:500] + "..."

            parts.append(f"\n第{turn}回合:")
            if action_text:
                parts.append(f"玩家: {action_text}")
            if response:
                parts.append(f"剧情: {response}")

        system = (
            "你是一个剧情摘要助手。请将以下游戏剧情压缩为两部分内容：\n\n"
            "第一部分 [关键事件]：用JSON数组列出所有重要事件，每个事件包含 turn(回合数)、event(事件描述，15字以内)。"
            "只保留影响剧情走向的关键决策、重要发现、人物关系变化、地点转移。\n\n"
            "第二部分 [叙事摘要]：用第三人称叙述200-400字的连贯摘要，保留剧情脉络和关键细节。"
            "摘要末尾用一句话概括当前的情感基调和人物关系趋势（如'主角与XX的关系趋于紧张'、'整体氛围从轻松转为紧迫'）。\n\n"
            "第三部分 [持久事实]：用JSON数组列出需要跨回合记住的持久事实，每条包含 "
            "type(承诺/外观变化/物品放置/伤势/关系变化/伏笔)和 fact(描述，20字以内)。\n"
            "例如：[{\"type\":\"承诺\",\"fact\":\"答应金科长明天交报告\"},"
            "{\"type\":\"伤势\",\"fact\":\"左臂被碎玻璃划伤\"}]\n"
            "格式：\n"
            "```persistent_facts\n"
            "[...JSON数组...]\n"
            "```\n\n"
            "格式：\n"
            "```key_events\n"
            '[{"turn":1,"event":"在酒馆遇到神秘老人"},{"turn":3,"event":"决定前往北方"}]\n'
            "```\n\n"
            "然后换行写叙事摘要正文。"
        )

        return system, "\n".join(parts)

    def update_state_with_summary(
        self, current_state: dict, summary: str, turn_number: int
    ) -> dict:
        """Store the layered summary in game state."""

        # Extract key events from structured block
        key_events_match = re.search(r'```key_events\s*(.*?)\s*```', summary, re.DOTALL)
        key_events = current_state.get("key_events", [])

        if key_events_match:
            raw_events_str = key_events_match.group(1).strip()
            try:
                new_events = json.loads(raw_events_str)
                if isinstance(new_events, list):
                    key_events.extend(new_events)
                    # Keep last 50 key events
                    if len(key_events) > 50:
                        key_events = key_events[-50:]
            except json.JSONDecodeError:
                # P0-10: 解析失败不应静默丢弃——尝试逐行提取或保留原文为单条事件
                # 尝试逐行提取 {"turn":..., "event":...} 片段
                line_matches = re.findall(
                    r'\{[^}]*"turn"\s*:\s*(\d+)[^}]*"event"\s*:\s*"([^"]+)"[^}]*\}',
                    raw_events_str,
                )
                if line_matches:
                    for turn_str, event_text in line_matches:
                        key_events.append({"turn": int(turn_str), "event": event_text})
                else:
                    # 最后兜底：将整段文字作为一条未结构化事件保留
                    import logging
                    logging.getLogger(__name__).warning(
                        "key_events JSON 解析失败，保留原始文本作为兜底: %s",
                        raw_events_str[:200],
                    )
                    key_events.append({
                        "turn": turn_number,
                        "event": raw_events_str[:100],
                    })
            # Narrative is everything after the key_events block
            narrative = summary[key_events_match.end():].strip()
        else:
            narrative = summary.strip()

        current_state["history_summary"] = narrative
        current_state["key_events"] = key_events
        current_state["last_summarized_turn"] = turn_number

        # Extract persistent facts
        facts_match = re.search(r'```persistent_facts\s*(.*?)\s*```', summary, re.DOTALL)
        if facts_match:
            raw_facts = facts_match.group(1).strip()
            try:
                new_facts = json.loads(raw_facts)
                if isinstance(new_facts, list):
                    existing = current_state.get("persistent_facts", [])
                    existing.extend(new_facts)
                    if len(existing) > 30:
                        existing = existing[-30:]
                    current_state["persistent_facts"] = existing
            except json.JSONDecodeError:
                pass
            # Remove persistent_facts block from narrative if it leaked in
            narrative = re.sub(r'```persistent_facts\s*.*?\s*```', '', narrative, flags=re.DOTALL).strip()
            current_state["history_summary"] = narrative

        return current_state

    async def compress_dual_channel(self, narrative_history: list, state_history: list,
                                     ai_provider, turn_number: int) -> dict:
        """双通道压缩：叙事和状态分别压缩

        Returns: {
            "narrative_summary": str,   # 叙事摘要（人物/剧情/关系）
            "state_summary": str,       # 状态摘要（属性变化/事件/位置）
            "compressed_turns": int,    # 压缩了多少轮
        }
        """
        # 通道1：叙事压缩
        narrative_texts = [h.get("narrative", "") for h in narrative_history if h.get("narrative")]
        narrative_prompt = (
            "请概括以下叙事历史的关键信息：\n"
            "1. 主要人物形象、性格、关系变化\n"
            "2. 重要剧情发展和结果\n"
            "3. 未解的悬念和伏笔\n"
            "合并重复，省略琐碎，摘要应显著短于原文。\n\n"
            + "\n---\n".join(narrative_texts[-10:])
        )

        # 通道2：状态压缩
        state_texts = []
        for h in state_history[-10:]:
            changes = h.get("state_changes", [])
            if changes:
                state_texts.append(f"T{h.get('turn', '?')}: " +
                                 ", ".join(f"{c.get('target')}={c.get('value')}" for c in changes[:5]))

        state_prompt = (
            ("请概括以下游戏状态变化历史：\n"
             "1. 属性趋势（上升/下降/稳定）\n"
             "2. 重要事件触发\n"
             "3. 位置移动轨迹\n"
             "4. NPC 关系变化趋势\n\n"
             + "\n".join(state_texts)) if state_texts else "无状态变化"
        )

        # 并行压缩两个通道
        narrative_task = ai_provider.generate(
            [{"role": "user", "content": narrative_prompt}],
            system="你是叙事摘要专家。输出精炼的摘要。",
            max_tokens=1000,
        )
        state_task = ai_provider.generate(
            [{"role": "user", "content": state_prompt}],
            system="你是游戏状态分析师。输出结构化的状态趋势摘要。",
            max_tokens=500,
        )

        narrative_summary, state_summary = await asyncio.gather(narrative_task, state_task)

        return {
            "narrative_summary": (narrative_summary or "").strip(),
            "state_summary": (state_summary or "").strip(),
            "compressed_turns": len(narrative_history),
        }
