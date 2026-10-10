"""ScriptBuilder：多轮对话构建完整剧本 JSON，并支持推演中动态扩展世界"""

import os
import re
import json
from datetime import datetime
from typing import Dict, List, Any

from .llm_utils import llm_call, _parse_json_from_llm, get_llm_state


class ScriptBuilder:
    """从主旨开始，多轮对话构建完整剧本 JSON，并支持推演中动态扩展世界"""

    PHASES = ["theme", "world_and_player", "npcs", "rules", "review"]

    def __init__(self):
        self.phase = "theme"
        self.theme = ""
        self.draft: Dict[str, Any] = {}
        self.history: List[Dict[str, str]] = []

    async def process_input(self, user_input: str) -> dict:
        self.history.append({"role": "user", "content": user_input})

        if self.phase == "theme":
            return await self._handle_theme(user_input)
        elif self.phase == "world_and_player":
            return await self._handle_world_and_player(user_input)
        elif self.phase == "npcs":
            return await self._handle_npcs(user_input)
        elif self.phase == "rules":
            return await self._handle_rules(user_input)
        elif self.phase == "review":
            return await self._handle_review(user_input)
        return {"phase": self.phase, "reply": "未知阶段", "done": False}

    # ---- 阶段处理 ----

    async def _handle_theme(self, user_input: str) -> dict:
        self.theme = user_input
        prompt = self._build_phase_prompt("world_and_player")
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2500, raise_on_error=True)
        parsed = _parse_json_from_llm(response)
        if not parsed:
            parsed = {}
        self.draft.update(parsed)
        self.phase = "world_and_player"
        reply = self._format_proposal("世界观与主角", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_world_and_player(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        prompt = self._build_phase_prompt("npcs", user_feedback=user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2500, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        if parsed.get("npcs"):
            self.draft["npcs"] = parsed["npcs"]
        self.phase = "npcs"
        reply = self._format_proposal("角色设定", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_npcs(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        prompt = self._build_phase_prompt("rules", user_feedback=user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2000, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        if parsed.get("attributes"):
            self.draft.setdefault("player_character", {})["attributes"] = parsed["attributes"]
        if parsed.get("world_properties"):
            self.draft["world_properties"] = parsed["world_properties"]
        self.phase = "rules"
        reply = self._format_proposal("属性与规则", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_rules(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        self.phase = "review"
        reply = self._format_full_review()
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": self.draft, "done": False}

    async def _handle_review(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        await self._generate_visual_profiles()
        script_data = self._finalize()
        script_id = self._save_script(script_data)
        return {"phase": "done", "reply": f"剧本「{script_data.get('script_name', '')}」已生成并保存！", "script_id": script_id, "done": True}

    async def _modify_current(self, user_input: str) -> dict:
        prompt = self._build_modify_prompt(user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2000, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        self._merge_modifications(parsed)
        reply = f"已根据你的意见修改。\n\n{self._format_full_review()}"
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": self.draft, "done": False}

    # ---- Prompt 构建 ----

    @staticmethod
    def _system_prompt() -> str:
        return """你是一个RPG剧本构建助手。根据用户主旨逐步构建完整RPG剧本。

## 输出格式
始终返回合法 JSON 对象，不要包含解释文本。

## 约束
- 贴合用户主旨
- NPC 3-8 个，地点 3-10 个，属性 3-8 个
- 每个 NPC 必须有 id、name、bio、personality、title、attitude_toward_player(0-100)、capabilities（顿号分隔）、default_location
- 每个地点必须有 id、name、description
- 属性值范围 0-100"""

    def _build_phase_prompt(self, target_phase: str, user_feedback: str = "") -> str:
        draft_json = json.dumps(self.draft, ensure_ascii=False, indent=2) if self.draft else "{}"

        if target_phase == "world_and_player":
            return f"""用户想创建RPG剧本，主旨：「{self.theme}」

请生成（JSON）：
1. script_name: 剧本名（简短）
2. world_background: 世界观（2-3段）
3. start_time: 起始时间（ISO格式，如 "2024-01-01T09:00:00"）
4. player_character: {{id, bio, initial_location, long_term_goal}}
5. locations: 3-6个地点 [{{id, name, description}}]
6. opening: {{text: 开场白（第二人称，3-5句）, choices: [{{id, text}}]}}"""

        if target_phase == "npcs":
            return f"""已确定世界观：
{draft_json}

用户反馈：{user_feedback}

请生成 3-6 个 NPC（JSON）：
{{"npcs": [{{id, name, bio, personality, title, attitude_toward_player, capabilities, default_location}}]}}
capabilities 用顿号分隔关键能力标签（如"谈判、情报、战斗"）。
default_location 必须是已有地点的 id。"""

        if target_phase == "rules":
            return f"""已确定世界和角色：
{draft_json}

用户反馈：{user_feedback}

请生成（JSON）：
{{"attributes": {{
  "属性名": {{"value": 初始值, "min": 0, "max": 100, "rule": "说明"}},
  ...
}},
"world_properties": [{{id, name, value, rule}}]}}"""

        return ""

    def _build_modify_prompt(self, user_input: str) -> str:
        draft_json = json.dumps(self.draft, ensure_ascii=False, indent=2)
        return f"""用户修改意见：{user_input}

当前剧本：
{draft_json}

请返回修改后的完整剧本 JSON（和当前格式一致）。"""

    # ---- 格式化 ----

    def _format_proposal(self, title: str, data: dict) -> str:
        lines = [f"## {title}\n"]
        if data.get("script_name"):
            lines.append(f"**剧本名**: {data['script_name']}")
        if data.get("world_background"):
            bg = data["world_background"]
            lines.append(f"**世界观**: {bg[:200]}{'...' if len(bg) > 200 else ''}")
        if data.get("player_character"):
            pc = data["player_character"]
            lines.append(f"**主角**: {pc.get('bio', '')}")
        if data.get("locations"):
            locs = data["locations"]
            lines.append(f"**地点** ({len(locs)}个):")
            for loc in locs:
                lines.append(f"  - {loc.get('name', '?')}: {loc.get('description', '')[:60]}")
        if data.get("npcs"):
            npcs = data["npcs"]
            lines.append(f"**角色** ({len(npcs)}个):")
            for npc in npcs:
                lines.append(f"  - {npc.get('name', '?')} ({npc.get('title', '')}): {npc.get('personality', '')[:40]}")
        if data.get("attributes"):
            attrs = data["attributes"]
            lines.append(f"**属性体系** ({len(attrs)}个):")
            for name, info in attrs.items():
                val = info.get("value", "?") if isinstance(info, dict) else info
                lines.append(f"  - {name}: {val}")
        if data.get("opening"):
            lines.append(f"**开场白**: {data['opening'].get('text', '')[:100]}...")
        lines.append("\n请确认或提出修改意见。输入「确认」进入下一步。")
        return "\n".join(lines)

    def _format_full_review(self) -> str:
        lines = ["## 剧本总览\n"]
        lines.append(f"**剧本名**: {self.draft.get('script_name', self.theme)}")
        bg = self.draft.get("world_background", "")
        lines.append(f"**世界观**: {bg[:150]}{'...' if len(bg) > 150 else ''}")
        pc = self.draft.get("player_character", {})
        if pc:
            lines.append(f"**主角**: {pc.get('bio', '')}")
        locs = self.draft.get("locations", [])
        if locs:
            lines.append(f"**地点** ({len(locs)}):")
            for loc in locs:
                lines.append(f"  - {loc.get('name', '?')}")
        npcs = self.draft.get("npcs", [])
        if npcs:
            lines.append(f"**角色** ({len(npcs)}):")
            for npc in npcs:
                lines.append(f"  - {npc.get('name', '?')} ({npc.get('title', '')}) 好感:{npc.get('attitude_toward_player', '?')}")
        attrs = pc.get("attributes", {})
        if attrs:
            lines.append(f"**属性** ({len(attrs)}):")
            for name, info in attrs.items():
                val = info.get("value", "?") if isinstance(info, dict) else info
                lines.append(f"  - {name}: {val}")
        opening = self.draft.get("opening", {})
        if opening.get("text"):
            lines.append(f"**开场白**: {opening['text'][:100]}...")
        lines.append("\n输入「确认」保存剧本并开始游戏，或提出修改意见。")
        return "\n".join(lines)

    # ---- 合并与保存 ----

    def _merge_modifications(self, parsed: dict):
        for key in ("script_name", "world_background", "start_time", "opening"):
            if key in parsed:
                self.draft[key] = parsed[key]
        if parsed.get("player_character"):
            self.draft.setdefault("player_character", {}).update(parsed["player_character"])
        if parsed.get("npcs"):
            self.draft["npcs"] = parsed["npcs"]
        if parsed.get("locations"):
            self.draft["locations"] = parsed["locations"]
        if parsed.get("attributes"):
            self.draft.setdefault("player_character", {})["attributes"] = parsed["attributes"]
        if parsed.get("world_properties"):
            self.draft["world_properties"] = parsed["world_properties"]

    def _finalize(self) -> dict:
        theme_slug = re.sub(r'[^a-z0-9]', '_', self.theme[:30].lower().strip())
        theme_slug = re.sub(r'_+', '_', theme_slug).strip('_') or "new_script"
        data = {
            "script_id": theme_slug,
            "script_name": self.draft.get("script_name", self.theme),
            "version": "1.0",
            "start_time": self.draft.get("start_time", datetime.now().isoformat()),
            "world_background": self.draft.get("world_background", ""),
            "settings": {"dice_check": False},
            "opening": self.draft.get("opening", {"text": "故事即将开始...", "choices": []}),
            "locations": self.draft.get("locations", []),
            "player_character": self.draft.get("player_character", {"id": "player", "bio": "", "attributes": {}, "relationships": {}}),
            "npcs": self.draft.get("npcs", []),
            "world_properties": self.draft.get("world_properties", []),
            "variables": [],
            "persistent_states": [],
            "cyclic_events": [],
            "one_time_events": [],
        }
        for i, npc in enumerate(data["npcs"]):
            if not npc.get("id"):
                npc["id"] = f"npc_{i}"
        pc = data["player_character"]
        if "relationships" not in pc:
            pc["relationships"] = {}
        for npc in data["npcs"]:
            npc_id = npc["id"]
            if npc_id not in pc["relationships"]:
                pc["relationships"][npc_id] = npc.get("attitude_toward_player", 50)
        if not pc.get("id"):
            pc["id"] = "player"
        return data

    async def _generate_visual_profiles(self) -> None:
        """为所有 NPC 和地点生成 AI visual profiles（颜色、风格等）"""
        if not get_llm_state().get("client"):
            return

        npcs = self.draft.get("npcs", [])
        locations = self.draft.get("locations", [])

        # 为 NPC 生成 visual profiles
        for npc in npcs:
            if npc.get("visual_profile"):
                continue
            sys_prompt = "你是视觉设计助手，基于角色描述生成像素艺术风格的颜色和特征。只返回 JSON。"
            user_prompt = f"""角色: {npc.get('name', '?')}
职位: {npc.get('title', '?')}
性格: {npc.get('personality', '?')}
能力: {npc.get('capabilities', '?')}

生成一个 visual_profile JSON，包含:
- shirt_color: 衣服颜色 (hex)
- hair_color: 头发颜色 (hex)
- hair_style: 发型 (short/long/bun/slick/buzz)
- pant_color: 裤子颜色 (hex)
- skin_color: 肤色 (hex)

只返回 JSON，不要其他文本。"""
            try:
                response = await llm_call(sys_prompt, user_prompt, max_tokens=300)
                profile = _parse_json_from_llm(response) or {}
                npc["visual_profile"] = profile
            except:
                npc["visual_profile"] = {}

        # 为地点生成 visual profiles
        for loc in locations:
            if loc.get("visual_profile"):
                continue
            sys_prompt = "你是视觉设计助手，基于地点描述生成等轴测立方体像素艺术的颜色和形状。只返回 JSON。"
            user_prompt = f"""地点: {loc.get('name', '?')}
描述: {loc.get('description', '?')}

生成一个 visual_profile JSON，包含:
- base_color: 基础颜色 (hex)
- dark_color: 暗色 (hex)
- light_color: 亮色 (hex)
- accent_color: 强调色 (hex)
- shape: 形状 (cube/tall/wide/pyramid/dome/multi)

只返回 JSON，不要其他文本。"""
            try:
                response = await llm_call(sys_prompt, user_prompt, max_tokens=300)
                profile = _parse_json_from_llm(response) or {}
                loc["visual_profile"] = profile
            except:
                loc["visual_profile"] = {}

    def _save_script(self, data: dict) -> str:
        from .routes import SCRIPTS_DIR
        script_id = data["script_id"]
        path = os.path.join(SCRIPTS_DIR, f"{script_id}.json")
        counter = 1
        while os.path.exists(path):
            sid = f"{script_id}_{counter}"
            path = os.path.join(SCRIPTS_DIR, f"{sid}.json")
            counter += 1
            data["script_id"] = sid
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[ScriptBuilder] 剧本已保存: {path}")
        return data["script_id"]

    @staticmethod
    def _is_modify(text: str) -> bool:
        return any(kw in text for kw in ("修改", "改一下", "换", "不要", "加上", "删掉", "去掉", "调整"))

    # ---- 推演中动态扩展世界 ----

    @staticmethod
    async def expand_world(scene_text: str, outline: dict, script_data: dict) -> dict:
        existing_npc_names = {n.get("name", "") for n in script_data.get("npcs", [])}
        existing_loc_names = {l.get("name", "") for l in script_data.get("locations", [])}

        sys_prompt = "你是世界扩展分析器。分析场景文本，找出尚未在剧本中定义的新角色和新地点。只返回 JSON。"
        user_prompt = f"""已有角色: {', '.join(n for n in existing_npc_names if n)}
已有地点: {', '.join(n for n in existing_loc_names if n)}

场景文本:
{scene_text[:800]}

大纲: {outline.get('summary', '')}

如果有新角色或新地点，生成定义；没有则返回空列表。
{{"new_npcs": [{{id, name, bio, personality, title, attitude_toward_player, capabilities, default_location}}], "new_locations": [{{id, name, description}}]}}"""

        response = await llm_call(sys_prompt, user_prompt, max_tokens=1000)
        return _parse_json_from_llm(response) or {"new_npcs": [], "new_locations": []}
