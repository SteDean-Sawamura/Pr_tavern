"""NPC interaction, attitude, relationship and information network logic."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ai.base import stream_split_think, strip_think_tags
from engine.event_scheduler import parse_time

logger = logging.getLogger(__name__)

# 从 game_session 模块级常量中迁移
NPC_CHAT_HISTORY_DEPTH = 6
NPC_CHAT_HISTORY_MAX = 20
NPC_CHAT_TIME_MINUTES = 5

# 预编译 CJK 检测正则
_CJK_RE = re.compile(r'[一-鿿㐀-䶿]')
_SPEAKER_RE = re.compile(
    r'([一-鿿㐀-䶿]{2,4}(?:先生|女士|小姐|老师|医生|教授|队长|部长|局长|秘书|管理员|主任|课长|处长|组长|师傅|大人|阁下)?)'
    r'(?:说道|说|喊道|低声道|问道|答道|叹道|笑道|冷笑道|怒道|回答|开口|补充道|解释道|提醒道|嘟囔道|低声说|轻声说)'
    r'|(?:^|\n)\s*([一-鿿㐀-䶿]{2,6})(?:：|:)\s*[「"\'"]'
)
_SPEAKER_REJECT_CHARS = set("的地得了着过来去在从到把被给让叫是有没不也就都还又才能会要想")
_SPEAKER_REJECT_WORDS = {
    "此刻", "没人", "含混", "压低", "拿起", "放下", "抬头", "低头",
    "转身", "起身", "伸手", "点头", "摇头", "挥手", "终于", "突然",
    "然后", "随后", "最终", "忽然", "立刻", "马上", "连忙", "赶紧",
    "只见", "那人", "此人", "某人", "对方", "身边", "旁边", "门外",
    "有人", "无人", "众人", "一人", "他人", "路人", "几人", "两人",
    "话筒", "嗓子", "手指", "眉头", "肩膀", "脑袋", "身子", "腰身",
}


class NpcMixin:
    """Mixin for NPC dialogue, attitude tracking, relationship management
    and information-network propagation."""

    # ---- class-level constants migrated from GameSession ----
    _ATTITUDE_THRESHOLDS = [
        (90, "亲密", "友好"),
        (70, "友好", "中立"),
        (50, "中立", "冷淡"),
        (30, "冷淡", "敌对"),
    ]

    _NPC_SYNC_FIELDS = ("name", "organizations", "personality", "bio", "capabilities", "title", "superior")
    _ORG_MUTABLE_FIELDS = frozenset({"leader", "stance", "description"})
    _REL_NET_MAX = 80

    def _refresh_npc_locations(self, state: dict | None = None) -> None:
        state = self.current_state if state is None else state
        for npc_id, npc_data in state.get("npcs", {}).items():
            if not isinstance(npc_data, dict):
                continue
            current = npc_data.get("current_location")
            if current and current != npc_data.get("_derived_location"):
                continue
            resolved = self._get_npc_location(npc_id, state)
            if resolved:
                npc_data["current_location"] = resolved
                npc_data["_derived_location"] = resolved
            elif current == npc_data.get("_derived_location"):
                npc_data.pop("current_location", None)
                npc_data.pop("_derived_location", None)

    def _record_npc_sightings(self) -> None:
        location = self.current_state.get("player", {}).get("location")
        if not location:
            return
        sightings = self.current_state.setdefault("npc_last_seen", {})
        for npc_id, npc in self.current_state.get("npcs", {}).items():
            if not isinstance(npc, dict) or npc.get("known") is False:
                continue
            if self._get_npc_location(npc_id) == location:
                sightings[npc_id] = {
                    "location": location,
                    "game_time": self.current_state.get("game_time", ""),
                    "turn": self.turn_number,
                }

    def _npc_talk_block_reason(self, npc_id: str, state: dict | None = None) -> str:
        state = self.current_state if state is None else state
        npc = state.get("npcs", {}).get(npc_id)
        if not isinstance(npc, dict) or npc.get("known") is False:
            return "尚未认识"
        if npc.get("met") is False:
            return "未接触"
        if npc.get("alive") is False:
            return "当前无法对话"
        if npc_id in state.get("companions", []):
            return ""
        player = state.get("player", {})
        location = player.get("location", "")
        npc_location = self._get_npc_location(npc_id, state)
        if not location or not npc_location:
            return "位置待确认"
        if not self._locations_match(location, npc_location):
            return "不在当前位置"
        player_room = player.get("current_room", "")
        npc_room = npc.get("current_room", "")
        if player_room and npc_room and player_room != npc_room:
            return "不在同一房间"
        return ""

    def _npc_contact_block_reason(self, npc_id: str, state: dict) -> str:
        npc = state.get("npcs", {}).get(npc_id)
        if not isinstance(npc, dict):
            return "角色不存在"
        if npc.get("alive") is False:
            return "当前无法接触"
        player = state.get("player", {})
        location = player.get("location", "")
        npc_location = self._get_npc_location(npc_id, state)
        if not location or not npc_location:
            return "位置待确认"
        if npc_location != location:
            return "不在当前位置"
        if player.get("current_room") and npc.get("current_room") and player["current_room"] != npc["current_room"]:
            return "不在同一房间"
        return ""

    def _npc_contact_is_current(self, npc_id: str, state: dict) -> bool:
        contacts = state.get("npc_contacts", {})
        contact = contacts.get(npc_id) if isinstance(contacts, dict) else None
        npc = state.get("npcs", {}).get(npc_id)
        player = state.get("player", {})
        location = player.get("location", "")
        return (isinstance(contact, dict) and isinstance(npc, dict) and bool(location)
                and npc.get("known") is not False and npc.get("met") is not False
                and npc.get("alive") is not False
                and contact.get("location") == location
                and contact.get("player_room", "") == player.get("current_room", "")
                and contact.get("npc_room", "") == npc.get("current_room", "")
                and not (player.get("current_room") and npc.get("current_room")
                         and player["current_room"] != npc["current_room"])
                and self._get_npc_location(npc_id, state) == location)

    def _prune_npc_contacts(self, state: dict) -> None:
        contacts = state.get("npc_contacts")
        if not isinstance(contacts, dict):
            state.pop("npc_contacts", None)
            return
        for npc_id in list(contacts):
            if not self._npc_contact_is_current(npc_id, state):
                contacts.pop(npc_id)
        if not contacts:
            state.pop("npc_contacts", None)

    def _apply_npc_contact_changes(self, state: dict, changes: list[dict]) -> None:
        self._prune_npc_contacts(state)
        for change in changes:
            if not isinstance(change, dict):
                continue
            npc_id = change.get("npc_id", "")
            if not isinstance(npc_id, str) or not npc_id:
                continue
            if change.get("in_contact") is False:
                state.get("npc_contacts", {}).pop(npc_id, None)
                continue
            if change.get("in_contact") is not True:
                continue
            if self._npc_contact_block_reason(npc_id, state):
                continue
            npc = state.get("npcs", {}).get(npc_id)
            player = state.get("player", {})
            location = player.get("location", "")
            player_room = player.get("current_room", "")
            npc_room = npc.get("current_room", "")
            npc["known"] = True
            npc["met"] = True
            state.setdefault("npc_contacts", {})[npc_id] = {
                "location": location, "player_room": player_room, "npc_room": npc_room,
            }

    # ================================================================
    #  NPC Dialogue (batch & 1-on-1)
    # ================================================================

    async def npc_dialogue_round(self, topic: str = "") -> list[dict]:
        """Generate batch NPC speeches via single AI call with rich character prompts."""
        present_ids = self._get_present_npc_ids()
        if not present_ids:
            return []

        speaking_npcs = []
        for npc_id in present_ids:
            npc = self._npc_by_id.get(npc_id)
            if not npc:
                npc = self.current_state.get("npcs", {}).get(npc_id)
                if not npc or not isinstance(npc, dict):
                    continue
            talkativeness = npc.get("talkativeness", 50) / 100
            if random.random() > talkativeness:
                continue
            speaking_npcs.append((npc_id, npc))

        if not speaking_npcs:
            return []

        # --- Build merged system prompt with full character settings ---
        npc_profiles = []
        for npc_id, npc in speaking_npcs:
            profile = self.prompt_builder.build_npc_talk_prompt(self.current_state, npc_id)
            if profile:
                npc_profiles.append(f"=== 角色: {npc.get('name', npc_id)} (ID: {npc_id}) ===\n{profile}")

        if not npc_profiles:
            return []

        system_prompt = (
            "你需要同时扮演以下多个角色，为每人各写一段群聊对话。\n"
            "每个角色都有独立的性格、说话风格和态度，你必须严格区分。\n\n"
            + "\n\n".join(npc_profiles)
            + "\n\n## 输出要求\n"
            "- 返回纯JSON列表，格式: [{\"npc_id\":\"角色ID\",\"speech\":\"对话内容\"},...]"
            "\n- 每人50-150字，保持各自性格和说话风格"
            "\n- NPC之间可以互相回应、接话、争论"
            "\n- 不要包含```或其他标记，直接输出JSON"
        )

        # --- Build enhanced user message ---
        loc_id = self.current_state.get("player", {}).get("location", "")
        loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id) if loc_id else "未知"
        game_time = self.current_state.get("game_time", "未知")
        weather = self.current_state.get("current_weather", "")
        atmo = self.current_state.get("time_atmosphere", {})
        period = atmo.get("period_label", "")

        ctx_parts = [f"地点: {loc_name}", f"时间: {game_time}"]
        if weather:
            ctx_parts.append(f"天气: {weather}")
        if period:
            ctx_parts.append(f"时段: {period}")
        if topic:
            ctx_parts.append(f"话题: {topic}")

        # Dialogue history per NPC (last 1 round each)
        hist_lines = []
        for npc_id, npc in speaking_npcs:
            chat_hist = self.current_state.get("npc_chat_history", {}).get(npc_id, [])
            if chat_hist:
                last = [h for h in chat_hist if not h.get("_summary")][-1:]
                for h in last:
                    npc_msg = h.get("npc", "")
                    if npc_msg:
                        hist_lines.append(f"{npc.get('name', npc_id)}: {npc_msg[:60]}")
        if hist_lines:
            ctx_parts.append("近期对话:\n" + "\n".join(hist_lines))

        # Recent narrative snippet
        narr = self.current_state.get("last_narrative", "")
        if narr:
            ctx_parts.append(f"最近叙事: {narr[:100]}")

        # Information network: unique info held by present NPCs
        network = self.current_state.get("information_network", [])
        npc_id_set = {nid for nid, _ in speaking_npcs}
        info_lines = []
        current_time = parse_time(game_time)
        for info in network[-10:]:
            holders = set(info.get("known_by", [])) & npc_id_set
            event_id = info.get("source_event_id")
            if event_id:
                event = self._event_def_by_id.get(event_id)
                fire_time = parse_time(event.get("trigger_time")) if event else None
                known_at = info.get("known_at")
                if not current_time or not fire_time or not isinstance(known_at, dict):
                    continue
                holders = {npc_id for npc_id in holders
                           if (learned := parse_time(known_at.get(npc_id)))
                           and fire_time <= learned <= current_time}
            if holders:
                names = ", ".join(self._npc_by_id.get(h, {}).get("name", h) for h in holders)
                info_lines.append(f"[{names}知道] {info['fact'][:50]}")
        if info_lines:
            ctx_parts.append("NPC掌握的情报（可自然融入对话）:\n" + "\n".join(info_lines))

        user_msg = "\n".join(ctx_parts) + "\n\n请以上述角色各生成一段对话。"

        raw = await self.ai_provider.generate(
            [{"role": "user", "content": user_msg}],
            system=system_prompt,
            max_tokens=2000,
            **self._stage_kwargs("state"),
        )
        raw = strip_think_tags(raw or "").strip()

        parsed = self._parse_simple_json_list(raw)
        if not parsed:
            return []

        results = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            npc_id = item.get("npc_id", "")
            speech = item.get("speech", "")
            if npc_id and speech and npc_id in npc_id_set:
                npc_name = self._npc_by_id.get(npc_id, {}).get("name", npc_id)
                results.append({"npc_id": npc_id, "name": npc_name, "speech": speech})

        if results:
            self.current_state["last_npc_speeches"] = results

        return results

    def _get_present_npc_ids(self) -> list[str]:
        """Get IDs of NPCs currently present at player's location."""
        loc = self.current_state.get("player", {}).get("location", "")
        if not loc:
            return []
        present = []
        npcs_state = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs_state.items():
            if not isinstance(npc_data, dict):
                continue
            npc_loc = npc_data.get("current_location", npc_data.get("default_location", ""))
            if npc_data.get("alive") is False:
                continue
            if npc_loc and self._locations_match(loc, npc_loc):
                present.append(npc_id)
        return present

    async def talk_to_npc(self, npc_id: str, message: str, *, on_background_drained=None) -> dict:
        """Have a dedicated conversation with an NPC.

        Flow#3: 维护每个NPC的对话历史，提供上下文连续性。
        与主线整合：
        - 推进 5 分钟游戏时间（避免对话发生在时间真空中）
        - 维护对话计数 npc_dialogue_counts
        - 关系阈值跨越时检查并触发主线事件
        """
        await self._drain_background_tasks()  # P0-3
        if on_background_drained is not None:
            on_background_drained()
        reason = self._npc_talk_block_reason(npc_id)
        if reason:
            return {"error": reason}
        npc_state = self.current_state["npcs"][npc_id]
        npc_name = npc_state.get("name", npc_id)

        # 记录对话前关系快照（用于阈值跨越检测）
        rels_before = copy.deepcopy(
            self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        )

        # Build NPC-specific prompt
        system_prompt = self.prompt_builder.build_npc_talk_prompt(
            self.current_state, npc_id
        )
        if not system_prompt:
            return {"error": f"NPC not in script: {npc_id}"}

        # Flow#3: 获取对话历史，构建多轮消息
        npc_chat_history = self.current_state.setdefault("npc_chat_history", {})
        history = npc_chat_history.get(npc_id, [])

        # 构建包含历史的消息列表
        messages = []
        # 如果有压缩摘要，先注入为上下文
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-NPC_CHAT_HISTORY_DEPTH:]:
            if h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h["player"]})
            messages.append({"role": "assistant", "content": h["npc"]})
        messages.append({"role": "user", "content": message})

        raw_response = await self.ai_provider.generate(
            messages, system=system_prompt, **self._stage_kwargs("narrative")
        )
        raw_response = strip_think_tags(raw_response)

        # Parse attitude changes from response
        npc_att_changes = self._parse_npc_talk_response(raw_response)

        # Strip the JSON block from the response for display
        clean_response = re.sub(
            r'```npc_talk\s*\{.*?\}\s*```', '', raw_response, flags=re.DOTALL
        ).strip()

        result = self._finalize_npc_talk(
            npc_id, npc_name, npc_state, message, clean_response,
            npc_att_changes, npc_chat_history, history, rels_before,
        )
        return result

    async def talk_to_npc_stream(self, npc_id: str, message: str, *, on_background_drained=None):
        """Streaming version of talk_to_npc. Yields dicts with type='text'/'thinking'/'final'."""
        await self._drain_background_tasks()
        if on_background_drained is not None:
            on_background_drained()
        reason = self._npc_talk_block_reason(npc_id)
        if reason:
            yield {"type": "final", "error": reason}
            return
        npc_state = self.current_state["npcs"][npc_id]
        npc_name = npc_state.get("name", npc_id)

        rels_before = copy.deepcopy(
            self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        )

        system_prompt = self.prompt_builder.build_npc_talk_prompt(
            self.current_state, npc_id
        )
        if not system_prompt:
            yield {"type": "final", "error": f"NPC not in script: {npc_id}"}
            return

        npc_chat_history = self.current_state.setdefault("npc_chat_history", {})
        history = npc_chat_history.get(npc_id, [])

        messages = []
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-NPC_CHAT_HISTORY_DEPTH:]:
            if h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h["player"]})
            messages.append({"role": "assistant", "content": h["npc"]})
        messages.append({"role": "user", "content": message})

        # Stream response, collecting full text (text only, not thinking)
        full_text = []
        async for kind, text in stream_split_think(
            self.ai_provider.generate_stream(messages, system=system_prompt, raw=True, **self._stage_kwargs("narrative"))
        ):
            if kind != "think":
                full_text.append(text)
            yield {"type": "thinking" if kind == "think" else "text", "content": text}

        raw_response = "".join(full_text)

        # Parse attitude changes and apply (same as non-streaming)
        npc_att_changes = self._parse_npc_talk_response(raw_response)

        clean_response = re.sub(
            r'```npc_talk\s*\{.*?\}\s*```', '', raw_response, flags=re.DOTALL
        ).strip()

        if clean_response != raw_response.strip():
            yield {"type": "narrative_revised", "content": clean_response}

        result = self._finalize_npc_talk(
            npc_id, npc_name, npc_state, message, clean_response,
            npc_att_changes, npc_chat_history, history, rels_before,
        )
        yield {"type": "final", **result}

    @staticmethod
    def _detect_relationship_crossings(before: dict, after: dict) -> list[dict]:
        """检测三维关系（trust/affection/fear）跨越关键阈值（30/60/80）。

        返回示例：[{"dim": "trust", "from": 28, "to": 32, "threshold": 30, "direction": "up"}]
        前端可用此触发UI提示或剧情节点。
        """
        if not isinstance(before, dict) or not isinstance(after, dict):
            return []
        thresholds = [30, 60, 80]
        out = []
        for dim in ("trust", "affection", "fear"):
            b = before.get(dim)
            a = after.get(dim)
            if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
                continue
            if a == b:
                continue
            for th in thresholds:
                if b < th <= a:
                    out.append({"dim": dim, "from": b, "to": a, "threshold": th, "direction": "up"})
                elif b >= th > a:
                    out.append({"dim": dim, "from": b, "to": a, "threshold": th, "direction": "down"})
        return out

    def _parse_npc_talk_response(self, response: str) -> list:
        """Extract npc_attitude_changes from NPC talk response."""
        match = re.search(r'```npc_talk\s*(\{.*?\})\s*```', response, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
            return data.get("npc_attitude_changes", [])
        except (json.JSONDecodeError, KeyError):
            return []

    def _finalize_npc_talk(
        self, npc_id: str, npc_name: str, npc_state: dict,
        message: str, clean_response: str, npc_att_changes: list,
        npc_chat_history: dict, history: list, rels_before: dict,
    ) -> dict:
        """Shared post-processing for talk_to_npc / talk_to_npc_stream."""
        all_state_changes = []

        clean_response = self.regex_engine.apply(clean_response, "ai_output")

        # 对话技能检定：检测说服/威胁/欺骗/询问机密等意图
        talk_check = self._maybe_talk_skill_check(message)
        if talk_check and npc_att_changes:
            bonus = 2 if talk_check["outcome"] in ("success", "critical_success") else -2
            for ac in npc_att_changes:
                if isinstance(ac.get("change"), (int, float)):
                    ac["change"] = ac["change"] + bonus

        if npc_att_changes:
            att_as_state = self._npc_attitude_to_state_changes(npc_att_changes)
            MAX_ATTITUDE_DELTA = 20
            for sc in att_as_state:
                val = sc.get("value", 0)
                if sc.get("op") in ("add", "subtract") and isinstance(val, (int, float)):
                    if abs(val) > MAX_ATTITUDE_DELTA:
                        sc["value"] = MAX_ATTITUDE_DELTA if val > 0 else -MAX_ATTITUDE_DELTA
            self.current_state, att_log = self.state_manager.apply_changes(
                self.current_state, att_as_state, inplace=True
            )
            all_state_changes.extend(att_log)
            self._sync_npc_attitudes()

        history.append({"player": message, "npc": clean_response})
        if len(history) > NPC_CHAT_HISTORY_MAX:
            # 压缩最早的对话为摘要，保留近期完整对话
            overflow = history[:-NPC_CHAT_HISTORY_DEPTH]
            kept = history[-NPC_CHAT_HISTORY_DEPTH:]
            summary_parts = []
            for h in overflow[-5:]:
                player_brief = h.get("player", "")[:30]
                npc_brief = h.get("npc", "")[:50]
                summary_parts.append(f"玩家:{player_brief}→NPC:{npc_brief}")
            existing_summary = history[0].get("_summary", "") if history and history[0].get("_summary") else ""
            new_summary = existing_summary + "; ".join(summary_parts)
            if len(new_summary) > 500:
                new_summary = new_summary[-500:]
            history = [{"_summary": new_summary}] + kept
        npc_chat_history[npc_id] = history

        dlg_counts = self.current_state.setdefault("npc_dialogue_counts", {})
        dlg_counts[npc_id] = dlg_counts.get(npc_id, 0) + 1
        enc_counts = self.current_state.setdefault("npc_encounter_counts", {})
        enc_counts[npc_id] = enc_counts.get(npc_id, 0) + 1

        interaction_log = self.current_state.setdefault("npc_interaction_log", {})
        npc_ilog = interaction_log.setdefault(npc_id, [])
        npc_ilog.append({
            "turn": self.turn_number,
            "player": message[:80],
            "npc": clean_response[:80],
            "attitude_delta": sum(ac.get("change", 0) for ac in npc_att_changes) if npc_att_changes else 0,
        })
        if len(npc_ilog) > 15:
            interaction_log[npc_id] = npc_ilog[-10:]

        if isinstance(npc_state, dict):
            if not npc_state.get("known"):
                npc_state["known"] = True
            if not npc_state.get("met"):
                npc_state["met"] = True

        # 扫描对话内容触发 lorebook 词条
        _, new_lore_ts = self.prompt_builder.scan_lorebook(
            message, [clean_response],
            timed_state=self.current_state.get("lorebook_timed_state"),
            turn_number=self.turn_number,
            state=self.current_state,
        )
        self.current_state["lorebook_timed_state"] = new_lore_ts

        old_time = self.current_state.get("game_time", "")
        triggered_events = []
        if old_time:
            chat_minutes = min(NPC_CHAT_TIME_MINUTES + len(clean_response) // 100 * 2, 30)
            new_time = self._advance_game_time(old_time, timedelta(minutes=chat_minutes))
            try:
                triggered_events = self.event_scheduler.check_events(
                    self.current_state, old_time, new_time,
                    condition_eval=self._evaluate_condition,
                ) or []
            except Exception as e:
                logging.getLogger(__name__).warning("NPC对话事件检查失败: %s", e)
                triggered_events = []
            if self.event_engine:
                try:
                    ee_npc = self.event_engine.tick(
                        self.current_state, self.turn_number,
                        game_time=new_time, old_time=old_time,
                        condition_eval=self._evaluate_condition,
                        player_action=message,
                        advance_timed=False,
                    )
                    self._apply_event_result(ee_npc)
                except Exception as e:
                    logging.getLogger(__name__).warning("NPC对话事件引擎tick失败: %s", e)
            self.current_state["game_time"] = new_time
            if triggered_events:
                self.current_state = self.event_scheduler.update_trackers(
                    self.current_state, triggered_events, new_time, inplace=True
                )
                for evt in triggered_events:
                    eid = evt.get("event_id", "") if isinstance(evt, dict) else ""
                    evt_def = self._event_def_by_id.get(eid)
                    if isinstance(evt_def, dict):
                        log = self._apply_event_def_effects(evt_def, eid)
                        all_state_changes.extend(log)
                event_lines = []
                for ev in triggered_events:
                    eid = ev.get("event_id", "") if isinstance(ev, dict) else str(ev)
                    desc = self._get_event_description(eid) if eid else ""
                    if desc:
                        event_lines.append(f"・{desc}")
                if event_lines:
                    clean_response = (
                        clean_response.rstrip()
                        + "\n\n[此期间发生]\n"
                        + "\n".join(event_lines)
                    )

        rels_after = self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        crossed = self._detect_relationship_crossings(rels_before, rels_after)

        self._refresh_npc_locations()
        self._ensure_rooms_initialized()
        self._apply_npc_contact_changes(self.current_state, [{"npc_id": npc_id, "in_contact": True}])

        # 优化6: NPC对话计入世界树节点，支持撤销和分支回溯
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id,
            game_time=self.current_state.get("game_time", ""),
            turn_number=self.turn_number,
            player_action={"type": "npc_talk", "npc_id": npc_id, "text": message},
            ai_response=clean_response,
            choices_presented=[],
            dice_rolls=[],
            state_changes=all_state_changes,
            triggered_events=[
                e.get("event_id", e) if isinstance(e, dict) else e
                for e in triggered_events
            ],
            state_snapshot=self.current_state,
        )
        node = self.world_tree.get_node(node_id)
        if node and talk_check:
            node["talk_check"] = talk_check

        return {
            "node_id": node_id,
            "npc_id": npc_id,
            "npc_name": npc_name,
            "response": clean_response,
            "state_changes": all_state_changes,
            "triggered_events": [
                e.get("event_id", e) if isinstance(e, dict) else e
                for e in triggered_events
            ],
            "relationship_crossings": crossed,
            "dialogue_count": dlg_counts[npc_id],
            "talk_check": talk_check,
            "quick_replies": ([] if self._npc_talk_block_reason(npc_id)
                              else self._generate_npc_quick_replies(npc_id, npc_name, history)),
            "state": self.current_state,
        }

    def _generate_npc_quick_replies(self, npc_id: str, npc_name: str, history: list) -> list[str]:
        """根据NPC态度和对话上下文生成快速回复建议。"""
        att = (self.current_state.get("npcs", {}).get(npc_id) or {}).get("attitude_toward_player", 50)
        turn_count = len(history)

        if att >= 70:
            replies = [f"向{npc_name}请求帮助", f"打听最近的消息", "继续聊天"]
        elif att >= 40:
            replies = [f"向{npc_name}打听消息", "友好地闲聊几句", "告辞离开"]
        else:
            replies = [f"尝试向{npc_name}解释", "保持沉默", "告辞离开"]

        # 根据最近NPC回复内容生成针对性选项
        if history:
            last_resp = ""
            for msg in reversed(history):
                if msg.get("npc"):
                    last_resp = msg["npc"]
                    break
            if last_resp and len(last_resp) > 10:
                contextual = self._extract_contextual_reply(last_resp, npc_name)
                if contextual:
                    replies[0] = contextual

        if turn_count >= 5:
            replies[-1] = "告辞离开"
        return replies

    @staticmethod
    def _extract_contextual_reply(npc_response: str, npc_name: str) -> str:
        """从NPC回复中提取可追问的话题。"""
        # 检测NPC提到的名词/话题（引号内容、书名号内容）
        quoted = re.findall(r'[「""]([^」""]{2,10})[」""]', npc_response)
        if quoted:
            return f"追问关于「{quoted[0]}」的事"
        book_quoted = re.findall(r'《([^》]{2,10})》', npc_response)
        if book_quoted:
            return f"追问关于《{book_quoted[0]}》的事"
        # 检测疑问句——NPC问了问题，玩家可以回应
        if "？" in npc_response[-80:] or "?" in npc_response[-80:]:
            return f"回应{npc_name}的问题"
        # 检测方位/地点提及
        loc_match = re.search(r'(?:去|到|在|前往)([^\s，。,]{2,8})', npc_response[-120:])
        if loc_match:
            return f"询问{loc_match.group(1)}的情况"
        return ""

    # ================================================================
    #  NPC Attitude & Relationship
    # ================================================================

    def _npc_attitude_to_state_changes(self, npc_att_changes: list, state: dict | None = None) -> list:
        """Convert npc_attitude_changes into state_changes format.

        Supports three-dimensional relationships (trust/affection/fear).
        Input:  [{"npc_id": "guard", "dimension": "trust", "change": 10, "reason": "helped"}]
        Output: [{"target": "player.relationships.guard.trust", "op": "add", "value": 10, "reason": "helped"}]
        Falls back to trust dimension if no dimension specified.

        Args:
            state: 目标 state dict。为 None 时回退到 self.current_state（主回合路径）。
        """
        target_state = state if state is not None else self.current_state
        changes = []
        known_npcs = set(self._npc_by_id.keys()) | set(target_state.get("npcs", {}).keys())
        for item in npc_att_changes:
            npc_id = item.get("npc_id") or item.get("npc") or item.get("name", "")
            if not npc_id:
                continue
            # Reject attitude changes for non-existent NPCs
            if npc_id not in known_npcs:
                continue
            change_val = item.get("change", 0)
            reason = item.get("reason", "")
            dimension = item.get("dimension", "")
            op = "add"
            if isinstance(change_val, str):
                try:
                    change_val = int(change_val)
                except (ValueError, TypeError):
                    change_val = 0
            if change_val == 0 and item.get("attitude") is not None:
                change_val = item["attitude"]
                op = "set"

            # Check if current relationship is 3D format
            rels = target_state.get("player", {}).get("relationships", {})
            rel_val = rels.get(npc_id)
            is_3d = isinstance(rel_val, dict) and any(k in rel_val for k in ("trust", "affection", "fear"))

            if is_3d and dimension in ("trust", "affection", "fear"):
                changes.append({
                    "target": f"player.relationships.{npc_id}.{dimension}",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })
            elif is_3d:
                # No dimension specified — default to trust
                changes.append({
                    "target": f"player.relationships.{npc_id}.trust",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })
            else:
                # Legacy single-value relationship
                changes.append({
                    "target": f"player.relationships.{npc_id}",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })

            # Store opinion and relationship_desc on NPC state
            opinion = item.get("opinion", "")
            rel_desc = item.get("relationship_desc", "")
            npc_state = target_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                if opinion:
                    npc_state["opinion"] = opinion
                if rel_desc:
                    npc_state["relationship_desc"] = rel_desc
        return changes

    def _auto_mark_npcs_known(self, narrative: str) -> None:
        """扫描叙事文本，自动把出现过的NPC标记为 known=true，并维护出场计数。

        修复"同一NPC反复被描述为首次出场"的问题：
        - AI 不一定可靠地输出 npcs.X.known=true 的 state_change
        - 凡是NPC名字出现在叙事中，即视为已与玩家建立认识
        - npc_encounter_counts 记录每个NPC在叙事中出现的回合数，供prompt builder提示AI不要重复介绍
        """
        if not narrative:
            return
        npcs_state = self.current_state.setdefault("npcs", {})
        encounter_counts = self.current_state.setdefault("npc_encounter_counts", {})
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            name = npc.get("name", npc_id)
            if not name:
                continue
            name_ok = (len(name) >= 2 if _CJK_RE.search(name) else len(name) >= 3) if name else False
            id_ok = len(npc_id) >= 3 if npc_id else False
            if not name_ok and not id_ok:
                continue
            matched = (name_ok and name in narrative) or (id_ok and npc_id in narrative)
            if matched:
                st = npcs_state.setdefault(npc_id, {})
                if isinstance(st, dict):
                    if not st.get("known"):
                        st["known"] = True
                    if not st.get("met"):
                        st["met"] = True
                encounter_counts[npc_id] = encounter_counts.get(npc_id, 0) + 1
        for npc_id, npc_st in list(npcs_state.items()):
            if npc_id in self._npc_by_id:
                continue
            if not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", "")
            if not name:
                continue
            name_ok = (len(name) >= 2 if _CJK_RE.search(name) else len(name) >= 3)
            if not name_ok:
                continue
            if name in narrative:
                if not npc_st.get("known"):
                    npc_st["known"] = True
                if not npc_st.get("met"):
                    npc_st["met"] = True
                encounter_counts[npc_id] = encounter_counts.get(npc_id, 0) + 1

    def _extract_unregistered_speakers(self, narrative: str) -> None:
        """Scan narrative for named speakers not in state["npcs"] and auto-register them.

        Prevents NPC inconsistency where a minor character (e.g. 金秘书) appears in
        one turn's narrative but isn't registered, causing the next turn to invent
        a different person at the same location.
        """
        if not narrative:
            return
        state = self.current_state
        npcs_dict = state.setdefault("npcs", {})
        player_loc = state.get("player", {}).get("location", "")
        player_name = state.get("player", {}).get("name", "")

        known_names = {player_name} if player_name else set()
        for npc_id, npc_st in npcs_dict.items():
            if isinstance(npc_st, dict):
                known_names.add(npc_st.get("name", ""))
        for npc in self.script.get("npcs", []):
            known_names.add(npc.get("name", ""))
        known_names.discard("")

        candidates: set[str] = set()
        for m in _SPEAKER_RE.finditer(narrative):
            name = m.group(1) or m.group(2)
            if not name or name in known_names:
                continue
            # 过滤明显不是人名的匹配
            if name in _SPEAKER_REJECT_WORDS:
                continue
            if any(c in _SPEAKER_REJECT_CHARS for c in name):
                continue
            # 纯动词/副词短语通常不含姓氏常见字，额外长度检查
            if len(name) > 4:
                continue
            candidates.add(name)

        for name in candidates:
            npc_id = self._slugify_name(name)
            if npc_id in npcs_dict or npc_id in self._npc_by_id:
                continue
            npcs_dict[npc_id] = {
                "name": name,
                "attitude_toward_player": 50,
                "known": True,
                "met": True,
                "default_location": player_loc,
                "current_location": player_loc,
                "bio": "",
                "personality": "",
                "capabilities": "",
                "title": "",
                "organizations": [],
                "superior": "",
            }
            state.setdefault("display_names", {})[npc_id] = name
            rels = state.get("player", {}).setdefault("relationships", {})
            if npc_id not in rels:
                rels[npc_id] = {"trust": 50, "affection": 50, "fear": 0}
            self.prompt_builder._npc_name_map[npc_id] = name

    @staticmethod
    def _slugify_name(name: str) -> str:
        """Convert a CJK name to a stable ID suitable for use as a dict key."""
        try:
            from pypinyin import lazy_pinyin
            return "_".join(lazy_pinyin(name))
        except Exception:
            return "npc_" + name

    @staticmethod
    def _reputation_title(value: int) -> str:
        if value >= 90:
            return "崇拜"
        if value >= 70:
            return "友好"
        if value >= 50:
            return "中立"
        if value >= 30:
            return "冷淡"
        if value >= 10:
            return "敌对"
        return "通缉"

    @staticmethod
    def _calc_attitude_from_3d(trust: float, affection: float, fear: float) -> int:
        """3D 关系 → attitude 统一公式：fear 超过 30 时产生负面影响。"""
        fear_penalty = max(0, fear - 30) * 0.5
        return int((trust + affection) / 2 - fear_penalty)

    def _sync_dirty_npc_attitudes(self, state: dict, npc_ids: set[str]) -> None:
        relationships = state.get("player", {}).get("relationships", {})
        npcs = state.get("npcs", {})
        for npc_id in npc_ids:
            npc_data = npcs.get(npc_id)
            if not isinstance(npc_data, dict) or npc_id not in relationships:
                continue
            relationship = relationships[npc_id]
            if isinstance(relationship, dict) and any(
                key in relationship for key in ("trust", "affection", "fear")
            ):
                attitude = self._calc_attitude_from_3d(
                    relationship.get("trust", 50), relationship.get("affection", 50),
                    relationship.get("fear", 0),
                )
            elif isinstance(relationship, (int, float)):
                attitude = int(relationship)
            else:
                continue
            npc_data["attitude_toward_player"] = max(0, min(100, attitude))

    def _sync_world_changes_to_lorebook(self, old_attitudes: dict):
        """Detect significant world state changes and create/update lorebook entries."""
        npcs = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs.items():
            if not isinstance(npc_data, dict):
                continue
            att = npc_data.get("attitude_toward_player", 50)
            old_att = old_attitudes.get(npc_id, 50)
            if att == old_att:
                continue
            lore_id = f"_world_npc_att_{npc_id}"
            if att <= 15:
                name = npc_data.get("name", npc_id)
                content = f"{name}对玩家极度敌对（好感度{att}），可能拒绝合作甚至发起攻击。"
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.update_entry(lore_id, content=content)
                else:
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": [name], "content": content,
                        "entry_type": "world_sync", "priority": 90,
                        "position": "after_world", "scan_depth": 5,
                    }])
            elif att >= 85:
                name = npc_data.get("name", npc_id)
                content = f"{name}对玩家极度友好（好感度{att}），愿意提供特殊帮助。"
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.update_entry(lore_id, content=content)
                else:
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": [name], "content": content,
                        "entry_type": "world_sync", "priority": 90,
                        "position": "after_world", "scan_depth": 5,
                    }])
            else:
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.remove_entry(lore_id)

        aps = set(self.current_state.get("active_persistent_states", []))
        for ps in self.script.get("persistent_states", []):
            ps_id = ps["id"]
            lore_id = f"_world_ps_{ps_id}"
            if ps_id in aps:
                content = f"持续状态「{ps.get('name', ps_id)}」生效中。{ps.get('description', '')[:80]}"
                if not any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    keys = [ps.get("name", ps_id)]
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": keys, "content": content,
                        "entry_type": "world_sync", "priority": 75,
                        "position": "after_world", "scan_depth": 3,
                    }])
            else:
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.remove_entry(lore_id)

    def _check_faction_reputation_events(self, old_reps: dict):
        """Fire story tree events when faction reputation crosses configured thresholds."""
        thresholds = self.script.get("settings", {}).get("faction_reputation_events", {})
        if not thresholds:
            return
        if not self.story_tree_engine and not self.event_engine:
            return
        faction_rep = self.current_state.get("faction_reputation", {})
        for fid, rules in thresholds.items():
            fd = faction_rep.get(fid, {})
            new_val = fd.get("value", 50) if isinstance(fd, dict) else 50
            old_val = old_reps.get(fid, 50)
            if new_val == old_val:
                continue
            for rule in rules:
                evt = rule.get("event", "")
                val = rule.get("value", 0)
                op = rule.get("op", ">=")
                if not evt:
                    continue
                fired_key = f"_fac_rep_evt_{fid}_{evt}"
                if self.current_state.get(fired_key):
                    continue
                crossed = False
                if op == ">=" and old_val < val <= new_val:
                    crossed = True
                elif op == "<=" and old_val > val >= new_val:
                    crossed = True
                elif op == ">" and old_val <= val < new_val:
                    crossed = True
                elif op == "<" and old_val >= val > new_val:
                    crossed = True
                if crossed:
                    self._fire_event_dual(evt)
                    self.current_state[fired_key] = True

    def _sync_npc_attitudes(self):
        """Sync relationship values back to state.npcs[].attitude_toward_player.

        For 3D relationships, attitude = (trust + affection) / 2 - fear_penalty.
        """
        rels = self.current_state.get("player", {}).get("relationships", {})
        npcs = self.current_state.get("npcs", {})
        faction_rep = self.current_state.get("faction_reputation", {})
        for npc_id, npc_data in npcs.items():
            if isinstance(npc_data, dict) and npc_id in rels:
                val = rels[npc_id]
                if isinstance(val, dict) and any(k in val for k in ("trust", "affection", "fear")):
                    attitude = self._calc_attitude_from_3d(
                        val.get("trust", 50), val.get("affection", 50), val.get("fear", 0),
                    )
                elif isinstance(val, (int, float)):
                    attitude = int(val)
                else:
                    continue
                if faction_rep:
                    npc_def = self._npc_by_id.get(npc_id, {})
                    npc_orgs = npc_def.get("organizations") or npc_data.get("organizations", [])
                    for org_entry in (npc_orgs or []):
                        org_id = org_entry.get("id", org_entry) if isinstance(org_entry, dict) else org_entry
                        rep_data = faction_rep.get(org_id)
                        if isinstance(rep_data, dict):
                            attitude += int((rep_data.get("value", 50) - 50) * 0.3)
                        break
                npc_data["attitude_toward_player"] = max(0, min(100, attitude))

    def _check_attitude_thresholds(self, old_attitudes: dict, att_changes: list = None) -> list[dict]:
        """检测NPC态度是否跨过关键阈值，返回通知列表。"""
        reason_map = {}
        for item in (att_changes or []):
            nid = item.get("npc_id") or item.get("npc") or item.get("name", "")
            reason = item.get("reason", "")
            if nid and reason:
                reason_map[nid] = reason

        notifications = []
        npcs = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs.items():
            if not isinstance(npc_data, dict):
                continue
            new_att = npc_data.get("attitude_toward_player")
            if new_att is None:
                continue
            if npc_id not in old_attitudes:
                # 新 NPC：不产生"降至/升至"通知，改为"建立关系"
                npc_name = self._get_npc_display_name(npc_id)
                reason = reason_map.get(npc_id, "")
                for threshold, above_label, below_label in self._ATTITUDE_THRESHOLDS:
                    if new_att >= threshold:
                        level = above_label
                        break
                else:
                    level = "冷淡"
                msg = f"与{npc_name}建立了联系（{level}）"
                if reason:
                    msg += f"（{reason}）"
                notifications.append({
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "direction": "up",
                    "new_level": level,
                    "reason": reason,
                    "message": msg,
                })
                continue
            old_att = old_attitudes[npc_id]
            if old_att == new_att:
                continue
            for threshold, above_label, below_label in self._ATTITUDE_THRESHOLDS:
                if old_att < threshold <= new_att:
                    npc_name = self._get_npc_display_name(npc_id)
                    reason = reason_map.get(npc_id, "")
                    msg = f"{npc_name}对你的态度提升至【{above_label}】"
                    if reason:
                        msg += f"（{reason}）"
                    notifications.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "direction": "up",
                        "new_level": above_label,
                        "reason": reason,
                        "message": msg,
                    })
                    break
                elif old_att >= threshold > new_att:
                    npc_name = self._get_npc_display_name(npc_id)
                    reason = reason_map.get(npc_id, "")
                    msg = f"{npc_name}对你的态度降至【{below_label}】"
                    if reason:
                        msg += f"（{reason}）"
                    notifications.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "direction": "down",
                        "new_level": below_label,
                        "reason": reason,
                        "message": msg,
                    })
                    break
        return notifications

    def _get_npc_display_name(self, npc_id: str) -> str:
        """Get NPC display name: state > script > fallback to ID."""
        npc_st = self.current_state.get("npcs", {}).get(npc_id)
        if isinstance(npc_st, dict) and npc_st.get("name"):
            return npc_st["name"]
        npc = self._npc_by_id.get(npc_id)
        return npc.get("name", npc_id) if npc else npc_id

    # ================================================================
    #  NPC Secrets, Companions, Reputation
    # ================================================================

    def _check_npc_secrets(self):
        """Check NPC secret unlock conditions based on attitude thresholds and game state."""
        unlocked = self.current_state.get("npc_unlocked_secrets", {})
        npcs_state = self.current_state.get("npcs", {})
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_secrets = dyn_st.get("secrets", [])
                if dyn_secrets:
                    all_npc_defs.append({
                        "id": dyn_id,
                        "name": dyn_st.get("name", dyn_id),
                        "secrets": dyn_secrets,
                    })
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            secrets = npc_def.get("secrets", [])
            if not secrets:
                continue
            npc_state = npcs_state.get(npc_id, {})
            if not isinstance(npc_state, dict) or not npc_state.get("known"):
                continue
            attitude = npc_state.get("attitude_toward_player", 50)
            npc_name = npc_state.get("name") or npc_def.get("name", npc_id)
            npc_unlocked = unlocked.get(npc_id, [])
            unlocked_ids = set(npc_unlocked)
            for secret in secrets:
                sid = secret.get("id", "")
                if not sid or sid in unlocked_ids:
                    continue
                req_att = secret.get("unlock_attitude", 0)
                if attitude < req_att:
                    continue
                cond = secret.get("unlock_condition", "")
                if cond and not self._evaluate_condition(cond):
                    continue
                npc_unlocked.append(sid)
                unlocked_ids.add(sid)
                desc = secret.get("content", sid)[:80]
                self._record_narrative_callback(
                    f"「{npc_name}」向你透露了秘密：{desc}",
                    ["npc_secret", npc_id], priority="high",
                )
            unlocked[npc_id] = npc_unlocked
        self.current_state["npc_unlocked_secrets"] = unlocked
        secret_counts = {}
        for npc_def in all_npc_defs:
            s = npc_def.get("secrets", [])
            if s:
                secret_counts[npc_def["id"]] = len(s)
        if secret_counts:
            self.current_state["_npc_secret_counts"] = secret_counts

    def _update_companion_loyalty(self):
        """Update companion loyalty based on turn events."""
        companions = self.current_state.get("companions", [])
        if not companions:
            return
        loyalty = self.current_state.setdefault("companion_loyalty", {})
        pacing = self.current_state.get("pacing_state", {})
        threshold_events = self.current_state.get("_last_threshold_events", [])

        for cid in companions:
            loy = loyalty.setdefault(cid, {"value": 50})
            delta = 0
            npc_st = self.current_state.get("npcs", {}).get(cid, {})
            attitude = npc_st.get("attitude_toward_player", 50) if isinstance(npc_st, dict) else 50

            if attitude >= 75:
                delta += 1
            elif attitude <= 30:
                delta -= 3

            if pacing.get("consecutive_high", 0) >= 3:
                delta -= 2

            for te in (threshold_events or []):
                if te.get("direction") == "below":
                    attr = te.get("attribute", "")
                    if any(k in attr for k in ("hp", "health", "生命", "体力")):
                        delta += 2
                        break

            if delta != 0:
                loy["value"] = max(0, min(100, loy["value"] + delta))

    def _sync_companions(self):
        """Sync companion locations to player and check loyalty thresholds."""
        companions = self.current_state.get("companions", [])
        if not companions:
            return
        player_loc = self.current_state.get("player", {}).get("location", "")
        loyalty = self.current_state.get("companion_loyalty", {})
        departures: list[str] = []
        for cid in companions:
            # Keep companion location synced to player
            npc_st = self.current_state.get("npcs", {}).get(cid)
            if isinstance(npc_st, dict) and player_loc:
                npc_st.pop("_derived_location", None)
                npc_st["current_location"] = player_loc
            # Check loyalty threshold for departure
            loy = loyalty.get(cid, {})
            val = loy.get("value", 50)
            if val <= 10:
                departures.append(cid)
                npc_name = npc_st.get("name", cid) if isinstance(npc_st, dict) else cid
                self._record_narrative_callback(
                    f"「{npc_name}」因对你极度失望而离开了队伍。",
                    ["companion_leave", cid], priority="high",
                )
        for cid in departures:
            companions.remove(cid)
            loyalty.pop(cid, None)
        self.current_state["companions"] = companions
        self.current_state["companion_loyalty"] = loyalty

    def _apply_reputation_attitude_modifier(self):
        """Adjust NPC attitudes based on their faction's reputation with the player.

        Uses delta from previous modifier to avoid cumulative drift.
        """
        faction_rep = self.current_state.get("faction_reputation", {})
        if not faction_rep:
            return
        npcs_state = self.current_state.get("npcs", {})
        prev_mods = self.current_state.get("_rep_attitude_mods", {})
        new_mods = {}
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_faction = dyn_st.get("faction", "")
                if dyn_faction:
                    all_npc_defs.append({"id": dyn_id, "faction": dyn_faction})
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            faction = npc_def.get("faction", "")
            if not npc_id or not faction:
                continue
            rep_entry = faction_rep.get(faction)
            if not rep_entry:
                continue
            rep_value = rep_entry.get("value", 50)
            modifier = max(-5, min(5, int((rep_value - 50) / 5)))
            old_modifier = prev_mods.get(npc_id, 0)
            delta = modifier - old_modifier
            if delta == 0:
                new_mods[npc_id] = modifier
                continue
            npc_st = npcs_state.get(npc_id)
            if isinstance(npc_st, dict):
                base = npc_st.get("attitude_toward_player", 50)
                npc_st["attitude_toward_player"] = max(0, min(100, base + delta))
            new_mods[npc_id] = modifier
        self.current_state["_rep_attitude_mods"] = new_mods

    # ================================================================
    #  Information Network
    # ================================================================

    def _record_information(self, parsed: dict, ctx: dict):
        """Record significant events as information entries for NPC propagation."""
        network = self.current_state.setdefault("information_network", [])
        present_npcs = ctx.get("present_npc_ids", [])

        if not present_npcs:
            return

        new_infos = []
        # Significant attitude changes
        for ac in parsed.get("npc_attitude_changes", []):
            if abs(ac.get("change", 0)) >= 10:
                npc_name = self._get_npc_display_name(ac.get("npc_id", ""))
                new_infos.append({
                    "fact": f"玩家与{npc_name}关系发生变化({ac.get('reason','')})",
                    "tags": ["social", "attitude"],
                    "spread_chance": 0.3,
                })
        # Triggered consequences
        if ctx.get("triggered_consequences"):
            for csq in ctx["triggered_consequences"]:
                desc = csq.get("description", csq.get("id", ""))
                new_infos.append({
                    "fact": f"发生了: {desc}",
                    "tags": ["consequence"],
                    "spread_chance": 0.5,
                })
        # Milestones
        if ctx.get("achieved_milestones"):
            for ms in ctx["achieved_milestones"]:
                desc = ms.get("description", ms.get("id", ""))
                new_infos.append({
                    "fact": f"里程碑达成: {desc}",
                    "tags": ["milestone"],
                    "spread_chance": 0.4,
                })
        # Combat dice rolls
        for d in ctx.get("dice_dicts", []):
            dtype = d.get("type", "")
            if any(k in dtype for k in ("combat", "attack", "战斗")):
                new_infos.append({
                    "fact": f"玩家参与了战斗（{d.get('description', '')}）",
                    "tags": ["violence", "combat"],
                    "spread_chance": 0.5,
                })
                break

        for info in new_infos:
            info_id = f"info_{self.turn_number}_{len(network)}"
            network.append({
                "id": info_id,
                "origin_turn": self.turn_number,
                "fact": info["fact"][:200],
                "known_by": list(present_npcs),
                "spread_chance": info.get("spread_chance", 0.3),
                "distortion": 0,
                "max_spread": 5,
                "tags": info.get("tags", []),
            })
        # Cap network size
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _propagate_information(self):
        """Spread information between NPCs based on relationships."""
        network = self.current_state.get("information_network", [])
        if not network:
            return
        npc_rels = self.current_state.get("npc_relationships_global", {})
        npcs_state = self.current_state.get("npcs", {})

        for info in network:
            known = set(info.get("known_by", []))
            max_spread = info.get("max_spread", 5)
            if len(known) >= max_spread:
                continue
            if info.get("source_event_id"):
                event = self._event_def_by_id.get(info["source_event_id"])
                fire_time = parse_time(event.get("trigger_time")) if event else None
                current_time = parse_time(self.current_state.get("game_time"))
                known_at = info.get("known_at")
                if not fire_time or not current_time or not isinstance(known_at, dict):
                    continue
            new_knowers = set()
            for knower in list(known):
                if npcs_state.get(knower, {}).get("alive") is False:
                    continue
                if info.get("source_event_id"):
                    learned = parse_time(known_at.get(knower))
                    if not learned or not fire_time <= learned < current_time:
                        continue
                # Organization spread
                for oid, members in self._org_members.items():
                    if knower in members:
                        for m in members:
                            if (len(known) + len(new_knowers) < max_spread
                                    and npcs_state.get(m, {}).get("alive") is not False
                                    and m not in known and m not in new_knowers):
                                if random.random() < 0.6:
                                    new_knowers.add(m)
                # Relationship spread
                for _rk, rel in (npc_rels or {}).items():
                    a = rel.get("from") or rel.get("a", "")
                    b = rel.get("to") or rel.get("b", "")
                    partner = ""
                    if a == knower:
                        partner = b
                    elif b == knower:
                        partner = a
                    if (len(known) + len(new_knowers) < max_spread
                            and npcs_state.get(partner, {}).get("alive") is not False
                            and partner and partner not in known and partner not in new_knowers):
                        if random.random() < info.get("spread_chance", 0.3):
                            new_knowers.add(partner)
                if len(known) + len(new_knowers) >= max_spread:
                    break

            for nk in new_knowers:
                info["known_by"].append(nk)
                if isinstance(info.get("known_at"), dict):
                    info["known_at"][nk] = self.current_state.get("game_time", "")
                if random.random() < 0.3:
                    info["distortion"] = min(3, info.get("distortion", 0) + 1)

    def _consolidate_information_memory(self):
        """Consolidate old information_network entries into NPC lorebook (long-term memory).

        - Entries older than 8 turns with high spread (known_by >= 3) or important tags
          get written into the NPC's lorebook entry as persistent memory.
        - Low-importance old entries are simply forgotten (deleted).
        - Keeps information_network as a short-term buffer.
        """
        network = self.current_state.get("information_network", [])
        if not network:
            return

        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        _IMPORTANT_TAGS = {"lie", "broken_promise", "betrayal", "secret", "faction_event"}
        _AGE_THRESHOLD = 8
        _SPREAD_THRESHOLD = 3

        to_consolidate: dict[str, list[str]] = {}  # npc_id -> [fact_lines]
        to_keep: list[dict] = []

        for info in network:
            age = self.turn_number - info.get("origin_turn", self.turn_number)
            if info.get("source_event_id"):
                to_keep.append(info)
                continue
            if age < _AGE_THRESHOLD:
                to_keep.append(info)
                continue

            tags = set(info.get("tags", []))
            known_by = info.get("known_by", [])
            is_important = bool(tags & _IMPORTANT_TAGS) or len(known_by) >= _SPREAD_THRESHOLD

            if is_important and lb:
                fact = info.get("fact", "")
                distortion = info.get("distortion", 0)
                if distortion >= 2:
                    fact = f"（传言）{fact}"
                for npc_id in known_by:
                    to_consolidate.setdefault(npc_id, []).append(fact)
            # else: forgotten -- not kept, not consolidated

        if not to_consolidate:
            if len(to_keep) != len(network):
                self.current_state["information_network"] = to_keep
            return

        display_names = self.current_state.get("display_names", {})
        for npc_id, facts in to_consolidate.items():
            if not facts:
                continue
            npc_name = display_names.get(npc_id, npc_id)
            entry_id = f"_memory_{npc_id}"
            fact_text = "\n".join(f"- {f}" for f in facts[-5:])
            existing = next((e for e in lb.entries if e.id == entry_id), None)
            if existing:
                old_content = existing.content or ""
                old_lines = [l for l in old_content.split("\n") if l.startswith("- ")]
                combined = old_lines + [f"- {f}" for f in facts[-5:]]
                if len(combined) > 8:
                    combined = combined[-8:]
                new_content = f"{npc_name}知道的事:\n" + "\n".join(combined)
                lb.update_entry(entry_id, new_content)
            else:
                content = f"{npc_name}知道的事:\n{fact_text}"
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 55,
                    "position": "after_world",
                    "entry_type": "npc_memory",
                }])

        self.current_state["information_network"] = to_keep

    def _check_memory_echoes(self, ctx: dict):
        """Check for memory echo triggers and write matching echoes into state.

        Triggers:
        - location_revisit: player returned to a location with significant past events
        - npc_reunion: player re-encounters an NPC after 5+ turns of separation
        - consequence_echo: a pending consequence just triggered, trace its origin
        - moral_echo: a moral alignment axis crossed the +/-50 threshold
        """
        echoes = []
        state = self.current_state
        log = state.get("adventure_log", [])
        player_loc = state.get("player", {}).get("location", "")

        # --- location_revisit ---
        if player_loc and self.turn_number > 3:
            prev_loc = ctx.get("rollback_state", {}).get("player", {}).get("location", "")
            if prev_loc and prev_loc != player_loc:
                loc_events = []
                for entry in log:
                    if entry.get("type") == "chapter":
                        continue
                    e_turn = entry.get("turn", 0)
                    if self.turn_number - e_turn < 5:
                        continue
                    e_loc = entry.get("location", "")
                    if not e_loc or not self._locations_match(player_loc, e_loc):
                        continue
                    significant = [
                        ev for ev in entry.get("events", [])
                        if ev.get("type") in ("consequence", "milestone", "relationship", "event")
                    ]
                    if significant:
                        loc_events.append((e_turn, significant[0].get("text", "")))
                if loc_events:
                    best = max(loc_events, key=lambda x: x[0])
                    loc_name = state.get("display_names", {}).get(player_loc, player_loc)
                    echoes.append({
                        "type": "location_revisit",
                        "trigger": f"重返{loc_name}",
                        "memory": f"第{best[0]}回合：{best[1]}",
                        "turns_ago": self.turn_number - best[0],
                        "emotional_weight": "high" if self.turn_number - best[0] >= 10 else "medium",
                    })

        # --- npc_reunion ---
        present_npcs = ctx.get("present_npc_ids", [])
        npc_last_seen = state.get("_npc_last_seen_turn", {})
        for npc_id in present_npcs:
            last_turn = npc_last_seen.get(npc_id, 0)
            gap = self.turn_number - last_turn if last_turn else 0
            if gap < 5:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            memory_text = ""
            chat_hist = state.get("npc_chat_history", {}).get(npc_id, [])
            real_chats = [h for h in chat_hist if not h.get("_summary")]
            if real_chats:
                last_chat = real_chats[-1]
                npc_brief = last_chat.get("npc", "")[:60]
                memory_text = f"上次对话中{npc_name}说：「{npc_brief}」"
            if not memory_text:
                for entry in reversed(log):
                    for ev in entry.get("events", []):
                        if npc_id in ev.get("text", "") or npc_name in ev.get("text", ""):
                            memory_text = f"第{entry.get('turn', '?')}回合：{ev['text']}"
                            break
                    if memory_text:
                        break
            if memory_text:
                echoes.append({
                    "type": "npc_reunion",
                    "trigger": f"再次见到{npc_name}",
                    "memory": memory_text,
                    "turns_ago": gap,
                    "emotional_weight": "high" if gap >= 10 else "medium",
                })
        for npc_id in present_npcs:
            npc_last_seen[npc_id] = self.turn_number
        state["_npc_last_seen_turn"] = npc_last_seen

        # --- consequence_echo ---
        triggered_cons = ctx.get("triggered_consequences", [])
        for csq in (triggered_cons or []):
            origin = csq.get("origin_description", csq.get("description", ""))
            origin_turn = csq.get("origin_turn", 0)
            if origin and origin_turn:
                echoes.append({
                    "type": "consequence_echo",
                    "trigger": f"伏线触发：{csq.get('description', '')[:30]}",
                    "memory": f"第{origin_turn}回合种下的因：{origin[:80]}",
                    "turns_ago": self.turn_number - origin_turn,
                    "emotional_weight": "high",
                })

        # --- moral_echo ---
        ma = state.get("moral_alignment", {})
        prev_ma = ctx.get("rollback_state", {}).get("moral_alignment", {})
        _AXIS_LABELS = {
            "mercy_vs_cruelty": ("仁慈", "残忍"),
            "honesty_vs_deception": ("诚实", "欺骗"),
            "order_vs_chaos": ("秩序", "混沌"),
        }
        for axis, (pos, neg) in _AXIS_LABELS.items():
            cur = ma.get(axis, 0)
            old = prev_ma.get(axis, 0)
            if abs(cur) >= 50 and abs(old) < 50:
                label = pos if cur > 0 else neg
                echoes.append({
                    "type": "moral_echo",
                    "trigger": f"你的「{label}」倾向已经根深蒂固",
                    "memory": f"一路走来的选择塑造了你{label}的名声",
                    "turns_ago": 0,
                    "emotional_weight": "high",
                })

        state["memory_echoes"] = echoes[:3]

    # ================================================================
    #  NPC Interaction Memory & Lorebook Sync
    # ================================================================

    def _sync_npc_interaction_memory(self):
        """将 npc_interaction_log 中的互动记录浓缩为 lorebook 长期记忆。"""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        interaction_log = self.current_state.get("npc_interaction_log", {})
        for npc_id, logs in interaction_log.items():
            if len(logs) < 3:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            significant = sorted(logs, key=lambda x: abs(x.get("attitude_delta", 0)), reverse=True)[:3]
            recent = logs[-2:]
            merged = list({id(x): x for x in significant + recent}.values())
            lines = []
            for entry in sorted(merged, key=lambda x: x["turn"]):
                delta = entry.get("attitude_delta", 0)
                delta_mark = f"(好感{'+'if delta>0 else ''}{delta})" if delta else ""
                lines.append(f"- 第{entry['turn']}回合: 玩家说「{entry['player'][:30]}」{delta_mark}")
            entry_id = f"_interact_{npc_id}"
            content = f"{npc_name}与玩家的互动记忆:\n" + "\n".join(lines)
            existing = any(e.id == entry_id for e in lb.entries)
            if existing:
                lb.update_entry(entry_id, content)
            else:
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 58,
                    "position": "after_world",
                    "entry_type": "npc_memory",
                }])

    def _build_lore_context_for_core(self, activated_lore: list) -> str:
        """Build lorebook context relevant to state inference (location, danger, NPC status)."""
        if not activated_lore:
            return ""
        relevant_types = {
            "scene_physical", "npc_offscreen", "npc_profile", "npc_relationship",
            "npc_memory", "consequence_context", "key_event", "manual", "location_desc", "pc_identity",
        }
        lines = []
        for entry in activated_lore[:6]:
            if entry.entry_type not in relevant_types:
                continue
            snippet = entry.content[:100].replace("\n", " ")
            lines.append(f"- {snippet}")
        if not lines:
            return ""
        return "\n\n## 世界知识（state推演参考）\n" + "\n".join(lines)

    def _build_lore_summary_for_plot(self, activated_lore: list) -> str:
        """Build a compact lorebook summary for Stage 1 plot_decision context.

        P1: 只预注入 constant=True 的常驻条目。
        非常驻条目由 Agent 通过 query_lorebook 工具按需获取。
        """
        if not activated_lore:
            return ""
        # P1: 仅保留常驻条目，非常驻条目通过 query_lorebook 工具按需拉取
        constant_lore = [e for e in activated_lore if getattr(e, "constant", False)]
        if not constant_lore:
            return ""
        _PLOT_TYPE_PRIORITY = {
            "event_context": 0, "story_event": 0, "key_event": 0,
            "npc_relationship": 1, "npc_profile": 2, "location_desc": 2,
            "pc_identity": 3, "player_behavior": 4,
        }
        sorted_lore = sorted(
            constant_lore,
            key=lambda e: (_PLOT_TYPE_PRIORITY.get(e.entry_type, 2), -e.priority),
        )
        lines = []
        for entry in sorted_lore[:8]:
            label = entry.comment or entry.id
            snippet = entry.content[:80].replace("\n", " ")
            vis = getattr(entry, "visibility", "public")
            if vis == "hidden":
                knowers = getattr(entry, "known_by_npcs", [])
                if knowers:
                    names = "/".join(self._get_npc_display_name(n) for n in knowers[:3])
                    lines.append(f"- [机密-仅{names}知道][{label}] {snippet}")
                else:
                    lines.append(f"- [机密-未公开][{label}] {snippet}")
            elif vis == "world":
                lines.append(f"- [世界设定][{label}] {snippet}")
            else:
                lines.append(f"- [{label}] {snippet}")
        if not lines:
            return ""
        return "## 已激活世界知识（决策时可参考）\n" + "\n".join(lines)

    def _accumulate_npc_dialogue_style(self, narrative: str, present_npc_ids: list[str]):
        """Extract NPC dialogue from narrative and write style lorebook entries after enough samples."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        npc_dialogues = self._extract_npc_dialogues(narrative, present_npc_ids)
        cache = self.current_state.setdefault("_npc_dialogue_samples", {})
        for npc_id, texts in npc_dialogues.items():
            if not texts:
                continue
            samples = cache.setdefault(npc_id, [])
            samples.extend(texts[:3])
            if len(samples) > 20:
                cache[npc_id] = samples[-15:]
            if len(samples) < 5:
                continue
            combined = "".join(samples)
            clen = len(combined)
            if clen < 10:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            traits = []
            modal_counts = {}
            for p in self._MODAL_PARTICLES:
                cnt = combined.count(p)
                if cnt >= 2:
                    modal_counts[p] = cnt
            if modal_counts:
                top = sorted(modal_counts.items(), key=lambda x: -x[1])[:3]
                traits.append(f"常用语气词: {'、'.join(p for p, _ in top)}")
            avg_len = sum(len(t) for t in samples) / len(samples)
            if avg_len < 8:
                traits.append("句式简短")
            elif avg_len > 25:
                traits.append("句式较长")
            excl_rate = combined.count("！") / clen
            ques_rate = combined.count("？") / clen
            if excl_rate > 0.05:
                traits.append("语气强烈，多用感叹")
            if ques_rate > 0.05:
                traits.append("多用疑问")
            if not traits:
                continue
            entry_id = f"_voice_{npc_id}"
            content = f"{npc_name}的说话风格: {'; '.join(traits)}"
            existing = any(e.id == entry_id for e in lb.entries)
            if existing:
                lb.update_entry(entry_id, content)
            else:
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 45,
                    "position": "after_world",
                    "entry_type": "npc_voice_style",
                }])

    def _sync_attitude_to_lorebook(self, npc_att_changes: list[dict]):
        """Write lorebook entries for significant NPC attitude shifts (|change| >= 10)."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        for item in npc_att_changes:
            npc_id = item.get("npc_id") or item.get("npc") or item.get("name", "")
            change = item.get("change", 0)
            if isinstance(change, str):
                try:
                    change = int(change)
                except (ValueError, TypeError):
                    continue
            if abs(change) < 10:
                continue
            reason = item.get("reason", "")
            dim = item.get("dimension", "trust")
            opinion = item.get("opinion", "")
            npc_name = self._get_npc_display_name(npc_id)
            entry_id = f"_att_{npc_id}"
            dim_zh = {"trust": "信任", "affection": "好感", "fear": "畏惧"}.get(dim, dim)
            direction = "上升" if change > 0 else "下降"
            line = f"{npc_name}对玩家的{dim_zh}{direction}了{abs(change)}点"
            if reason:
                line += f"，原因: {reason}"
            if opinion:
                line += f"。{npc_name}的看法: {opinion}"
            existing = next((e for e in lb.entries if e.id == entry_id), None)
            if existing:
                old = existing.content
                lines = old.split("\n")
                lines.append(f"- 第{self.turn_number}回合: {line}")
                if len(lines) > 6:
                    lines = lines[:1] + lines[-5:]
                lb.update_entry(entry_id, "\n".join(lines))
            else:
                content = f"{npc_name}与玩家的关系变化:\n- 第{self.turn_number}回合: {line}"
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 65,
                    "position": "after_world",
                    "entry_type": "npc_attitude",
                }])

    def _sync_key_events_to_lorebook(self):
        """Sync key_events to lorebook: one entry per event, keyed by entity names found in text."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        key_events = self.current_state.get("key_events", [])
        if not key_events:
            return
        npc_names = {n.get("name", n["id"]): n["id"] for n in self.script.get("npcs", []) if n.get("name")}
        for dyn_id, dyn_st in self.current_state.get("npcs", {}).items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict) and dyn_st.get("name"):
                npc_names[dyn_st["name"]] = dyn_id
        loc_names = set(self.current_state.get("display_names", {}).values())
        existing_ids = {e.id for e in lb.entries}
        for evt in key_events[-20:]:
            turn = evt.get("turn", 0)
            text = evt.get("event", "")
            if not text:
                continue
            entry_id = f"_keyevt_{turn}_{hash(text) % 10000}"
            if entry_id in existing_ids:
                continue
            keys = []
            for name in npc_names:
                if name in text:
                    keys.append(name)
            for loc in loc_names:
                if loc and len(loc) >= 2 and loc in text:
                    keys.append(loc)
            if not keys:
                words = [w for w in text.split() if len(w) >= 3]
                keys = words[:2] if words else [text[:8]]
            lb.add_entries([{
                "id": entry_id,
                "keys": keys,
                "content": f"第{turn}回合关键事件: {text}",
                "priority": 50,
                "position": "after_world",
                "entry_type": "key_event",
            }])
            existing_ids.add(entry_id)

    # ================================================================
    #  Dynamic NPC Registration & Script Sync
    # ================================================================

    def _index_dynamic_npcs_from_state(self, state: dict):
        """Rebuild dynamic NPC lookups when loading a saved branch."""
        script_npcs = self.script.setdefault("npcs", [])
        self.prompt_builder._branch_npc_ids.update(set(self._npc_by_id) - self._initial_npc_ids)
        script_npcs[:] = [npc for npc in script_npcs
                          if not isinstance(npc, dict) or npc.get("id") in self._initial_npc_ids]
        for index in (self._npc_by_id, self.prompt_builder._npc_by_id,
                      self.prompt_builder._npc_name_map):
            for npc_id in list(index):
                if npc_id not in self._initial_npc_ids:
                    del index[npc_id]
        for name, npc_id in list(self._npc_name_to_id.items()):
            if npc_id not in self._initial_npc_ids:
                del self._npc_name_to_id[name]
        script_indices = {
            npc["id"]: index for index, npc in enumerate(script_npcs)
            if isinstance(npc, dict) and npc.get("id")
        }
        for npc_id, npc_st in state.get("npcs", {}).items():
            if npc_id in self._initial_npc_ids or not isinstance(npc_st, dict):
                continue
            npc_def = {"id": npc_id, **npc_st}
            if npc_id in script_indices:
                script_npcs[script_indices[npc_id]] = npc_def
            else:
                script_indices[npc_id] = len(script_npcs)
                script_npcs.append(npc_def)
            self._npc_by_id[npc_id] = npc_def
            if npc_def.get("name"):
                self._npc_name_to_id[npc_def["name"]] = npc_id
            self.prompt_builder._npc_name_map[npc_id] = npc_def.get("name", npc_id)
            self.prompt_builder._npc_by_id[npc_id] = npc_def

    def _register_dynamic_npc(self, npc_data: dict):
        """Register a dynamically created NPC into the game state."""
        npc_id = npc_data.get("id", "")
        if not npc_id:
            return
        npcs = self.current_state.setdefault("npcs", {})
        # Don't overwrite existing NPC
        if npc_id in npcs:
            return
        npcs[npc_id] = {
            "name": npc_data.get("name", npc_id),
            "attitude_toward_player": npc_data.get("attitude_toward_player", 50),
            "known": npc_data.get("known", True),
            "met": npc_data.get("met", npc_data.get("known", True)),
            "default_location": npc_data.get("location", ""),
            "current_location": npc_data.get("location", ""),
            "bio": npc_data.get("bio", ""),
            "personality": npc_data.get("personality", ""),
            "capabilities": npc_data.get("capabilities", ""),
            "title": npc_data.get("title", ""),
            "organizations": npc_data.get("organizations") or self._migrate_npc_org_fields(npc_data),
            "superior": npc_data.get("superior", ""),
        }
        # Add to display_names
        dn = self.current_state.setdefault("display_names", {})
        dn[npc_id] = npc_data.get("name", npc_id)
        # Initialize relationship
        rels = self.current_state.get("player", {}).setdefault("relationships", {})
        if npc_id not in rels:
            attitude = npc_data.get("attitude_toward_player", 50)
            rels[npc_id] = {
                "trust": npc_data.get("trust", attitude),
                "affection": npc_data.get("affection", attitude),
                "fear": npc_data.get("fear", 0),
            }
        # P1-9: 同步更新 script.npcs 列表和 prompt_builder 缓存，
        # 确保 _build_npc_section 能迭代到动态 NPC。
        script_npcs = self.script.setdefault("npcs", [])
        if not any(n.get("id") == npc_id for n in script_npcs):
            script_npcs.append(npc_data)
        self._npc_by_id[npc_id] = npc_data
        self._npc_name_to_id[npc_data.get("name", npc_id)] = npc_id
        self.prompt_builder._npc_name_map[npc_id] = npc_data.get("name", npc_id)
        self.prompt_builder._npc_by_id[npc_id] = npc_data

    @staticmethod
    def _migrate_npc_org_fields(npc: dict) -> list[dict]:
        """AI 返回旧格式 organization/rank 时转为 organizations 数组。"""
        org_id = npc.get("organization", "")
        rank = npc.get("rank")
        if org_id:
            entry: dict = {"org_id": org_id}
            if rank is not None:
                entry["rank"] = rank
            return [entry]
        return []

    def _sync_npc_fields_to_script(self, state: dict, *, dirty_npc_ids: set[str] | None = None):
        """Sync mutable NPC fields from runtime state back to script.npcs.

        If dirty_npc_ids is provided, only check those NPCs instead of all.
        """
        script_npcs = self.script.get("npcs", [])
        script_npc_map = {n["id"]: n for n in script_npcs if n.get("id")}
        changed_npc_ids = []
        changed_org_ids = set()
        npc_items = state.get("npcs", {}).items()
        if dirty_npc_ids:
            npc_items = ((nid, state.get("npcs", {}).get(nid)) for nid in dirty_npc_ids)
        for npc_id, npc_st in npc_items:
            if not isinstance(npc_st, dict):
                continue
            script_npc = script_npc_map.get(npc_id)
            if not script_npc:
                continue
            dirty = False
            for field in self._NPC_SYNC_FIELDS:
                if field in npc_st and script_npc.get(field) != npc_st[field]:
                    if field == "organizations":
                        for om in (script_npc.get("organizations") or []):
                            if om.get("org_id"):
                                changed_org_ids.add(om["org_id"])
                        for om in (npc_st[field] if isinstance(npc_st[field], list) else []):
                            if isinstance(om, dict) and om.get("org_id"):
                                changed_org_ids.add(om["org_id"])
                    script_npc[field] = npc_st[field]
                    dirty = True
            if "name" in npc_st:
                self.prompt_builder._npc_name_map[npc_id] = npc_st["name"]
            if dirty:
                changed_npc_ids.append(npc_id)
                for om in (npc_st.get("organizations") or script_npc.get("organizations") or []):
                    if isinstance(om, dict) and om.get("org_id"):
                        changed_org_ids.add(om["org_id"])
        if changed_npc_ids or changed_org_ids:
            self.prompt_builder.refresh_kg_entries(changed_npc_ids, list(changed_org_ids))
            if self.vector_memory:
                self._sync_kg_entries_to_vector(changed_npc_ids, list(changed_org_ids))

    def _sync_kg_entries_to_vector(self, npc_ids: list[str], org_ids: list[str]):
        """Re-index updated KG lorebook entries into vector memory (non-blocking)."""
        if not self.vector_memory:
            return
        batch = []
        for npc_id in npc_ids:
            entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_npc_{npc_id}"), None)
            if entry and entry.content and len(entry.content) >= 20:
                batch.append((entry.id, entry.content, {"entry_type": entry.entry_type, "comment": entry.comment}))
        for org_id in org_ids:
            entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_org_{org_id}"), None)
            if entry and entry.content and len(entry.content) >= 20:
                batch.append((entry.id, entry.content, {"entry_type": entry.entry_type, "comment": entry.comment}))
        if batch:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.vector_memory.add_lorebook_batch(batch)
            else:
                self._schedule_index_task(asyncio.to_thread(self.vector_memory.add_lorebook_batch, batch))

    def _apply_org_changes_to_script(self, changes: list[dict]):
        """Apply state_changes targeting organizations.{id}.{field} directly to script."""
        org_by_id = {o["id"]: o for o in self.script.get("organizations", []) if o.get("id")}
        changed_org_ids = []
        for change in changes:
            target = change.get("target", "")
            if not target.startswith("organizations."):
                continue
            parts = target.split(".", 2)
            if len(parts) < 3:
                continue
            org_id, field = parts[1], parts[2]
            org = org_by_id.get(org_id)
            if org and field in self._ORG_MUTABLE_FIELDS:
                org[field] = change.get("value", "")
                if org_id not in changed_org_ids:
                    changed_org_ids.append(org_id)
        if changed_org_ids:
            self.prompt_builder.refresh_kg_entries([], changed_org_ids)
            if self.vector_memory:
                self._sync_kg_entries_to_vector([], changed_org_ids)

    @staticmethod
    def _normalize_rel_key(a: str, b: str, directed: bool = False) -> str:
        """归一化关系 key，无向关系按字典序排列防止 a_b / b_a 重复。"""
        if directed:
            return f"{a}_{b}"
        return f"{min(a, b)}_{max(a, b)}"

    def _apply_npc_relationship_updates(
        self, updates: list, *, state: dict | None = None,
    ):
        """Apply NPC-NPC relationship updates to global + known networks.

        Each update has:
          a, b: NPC IDs
          type: relationship type (友好, 敌对, etc.)
          description: relationship description
          scope: "global" | "known" | "both"
          discovery_reason: (for known scope) how the player discovered it

        Includes key normalization (undirected relations use sorted key)
        and cap enforcement to prevent unbounded growth.
        """
        target = state if state is not None else self.current_state
        global_net = target.setdefault("npc_relationships_global", {})
        known_net = target.setdefault("npc_relationships_known", {})

        for upd in updates:
            a = upd.get("a", "")
            b = upd.get("b", "")
            if not a or not b:
                continue
            key = self._normalize_rel_key(a, b)
            existing = global_net.get(key)
            entry = {
                "a": a,
                "b": b,
                "type": upd.get("type", existing.get("type", "中立") if existing else "中立"),
                "description": upd.get("description", existing.get("description", "") if existing else ""),
                "intensity": upd.get("intensity", existing.get("intensity", 50) if existing else 50),
                "_turn": self.turn_number,
            }
            if upd.get("known") is not None:
                entry["known"] = bool(upd["known"])
            elif existing and "known" in existing:
                entry["known"] = existing["known"]
            if upd.get("met") is not None:
                entry["met"] = bool(upd["met"])
            elif existing and "met" in existing:
                entry["met"] = existing["met"]
            scope = upd.get("scope", "global")

            if scope in ("global", "both"):
                global_net[key] = entry
            if scope in ("known", "both"):
                known_entry = dict(entry)
                if upd.get("discovery_reason"):
                    known_entry["discovery_reason"] = upd["discovery_reason"]
                known_net[key] = known_entry

        # 淘汰机制：超过上限时删除动态 NPC 间最旧的条目
        for net in (global_net, known_net):
            if len(net) <= self._REL_NET_MAX:
                continue
            predefined_npc_ids = {
                n["id"] for n in self.script.get("npcs", [])
                if isinstance(n, dict) and n.get("id")
            }
            evict_candidates = []
            for k, v in net.items():
                ids = {v.get("a", ""), v.get("b", ""), v.get("from", ""), v.get("to", "")} - {""}
                if not ids & predefined_npc_ids:
                    evict_candidates.append((k, v.get("_turn", 0)))
            evict_candidates.sort(key=lambda x: x[1])
            to_remove = len(net) - self._REL_NET_MAX
            for k, _ in evict_candidates[:to_remove]:
                net.pop(k, None)
