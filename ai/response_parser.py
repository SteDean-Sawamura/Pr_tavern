"""AI response parser: extracts structured data from AI output."""

import json
import logging
import re

logger = logging.getLogger("tavern.parser")


class ResponseParser:
    _GAME_STATE_FIELDS = [
        "choices", "state_changes", "end_time", "location_change",
        "reveal_locations", "npc_attitude_changes", "activate_states",
        "deactivate_states", "world_property_changes",
        "inventory_changes", "game_over",
        "offscreen_npc_updates", "new_npcs", "npc_relationship_updates",
        "npc_met_changes", "invalidate_lore",
        "scene_details",
        "faction_reputation_changes", "moral_alignment_changes",
        "npc_location_changes", "room_changes",
        "recruit_companions", "dismiss_companions", "npc_interjections",
    ]

    # Unique prefixes for fuzzy key matching (shortest unambiguous prefix → full key)
    _KEY_PREFIX_MAP: dict[str, str] = {}

    @classmethod
    def _build_prefix_map(cls):
        if cls._KEY_PREFIX_MAP:
            return
        fields = cls._GAME_STATE_FIELDS
        for f in fields:
            # Find shortest unique prefix (min 1 char)
            for length in range(1, len(f) + 1):
                prefix = f[:length]
                clashes = [g for g in fields if g != f and g.startswith(prefix)]
                if not clashes:
                    # All prefixes from this length onwards are unique for this field
                    for pl in range(length, len(f) + 1):
                        cls._KEY_PREFIX_MAP[f[:pl]] = f
                    break

    def __init__(self):
        self._build_prefix_map()

    # ---- Fuzzy code-fence matching ----
    # Matches ```game_state, but also truncated variants like ```_state, ```game_s, ```gam
    _FENCE_FULL = re.compile(r'```game_state\s*(.*?)\s*```', re.DOTALL)
    _FENCE_INCOMPLETE = re.compile(r'```game_state\s*(.*)', re.DOTALL)
    _FENCE_FUZZY = re.compile(r'```(?:game_s\w*|_state|gam\w*)\s*(.*)', re.DOTALL)

    def parse(self, raw_response: str) -> dict:
        """Parse AI response into structured data.

        The AI is instructed to include a ```game_state JSON block.
        Falls back gracefully if parsing fails.
        """
        result = self._empty_result()

        # Try to extract game_state JSON block (full fence)
        match = self._FENCE_FULL.search(raw_response)

        if match:
            json_str = match.group(1).strip()
            narrative = raw_response[:match.start()].strip()
            result["narrative"] = narrative
            self._parse_json_into(result, json_str)
        else:
            # Try incomplete fence (no closing ```)
            match = self._FENCE_INCOMPLETE.search(raw_response)
            if not match:
                # Fuzzy fence match for truncated markers
                match = self._FENCE_FUZZY.search(raw_response)

            if match:
                json_str = match.group(1).strip()
                json_str = re.sub(r'`{0,2}$', '', json_str).strip()
                narrative = raw_response[:match.start()].strip()
                result["narrative"] = narrative
                self._parse_json_into(result, json_str)
            else:
                # No structured block found — try fallback
                result = self._fallback_parse(raw_response, result)

        # Validate choices
        if not result["choices"]:
            result["choices"] = [
                {"id": "c1", "text": "继续探索"},
                {"id": "c2", "text": "与周围的人交谈"},
                {"id": "c3", "text": "做其他事情"},
            ]

        # Ensure all choices have IDs
        for i, choice in enumerate(result["choices"]):
            if "id" not in choice:
                choice["id"] = f"c{i + 1}"

        return result

    def _parse_json_into(self, result: dict, json_str: str):
        """Try all recovery strategies to parse json_str and apply to result."""
        # 0. 规范化全角/智能标点为半角，处理AI输出中的"" ：，等
        json_str = self._normalize_punctuation(json_str)

        # 1. Fix truncated keys first (always — even valid JSON may have short keys)
        fixed = self._fix_truncated_keys(json_str)

        # 2. Try parsing the key-fixed JSON
        data = self._try_parse_json(fixed)
        if data:
            self._apply_data(result, data)
            return

        # 3. Try closing the key-fixed JSON (handles truncated brackets)
        data = self._try_close_json(fixed)
        if data:
            self._apply_data(result, data)
            return

        # 4. Regex extraction from original (handles severely garbled JSON)
        data = self._extract_fields_regex(json_str)
        if data:
            self._apply_data(result, data)
            return

        # 5. Last resort: recover at least choices from garbled text
        choices = self._recover_choices_from_garbled(json_str)
        if choices:
            result["choices"] = choices

    def _empty_result(self) -> dict:
        return {
            "narrative": "",
            "choices": [],
            "state_changes": [],
            "end_time": None,
            "location_change": None,
            "reveal_locations": [],
            "npc_attitude_changes": [],
            "activate_states": [],
            "deactivate_states": [],
            "world_property_changes": [],
            "inventory_changes": [],
            "game_over": None,
            "offscreen_npc_updates": [],
            "new_npcs": [],
            "npc_relationship_updates": [],
            "npc_met_changes": [],
            "invalidate_lore": [],
            "scene_details": None,
            "faction_reputation_changes": [],
            "moral_alignment_changes": [],
            "npc_location_changes": [],
            "recruit_companions": [],
            "dismiss_companions": [],
            "npc_interjections": [],
        }

    def _apply_data(self, result: dict, data: dict):
        """Apply parsed data dict to result."""
        for field in self._GAME_STATE_FIELDS:
            val = data.get(field)
            if val is not None:
                result[field] = val

    def _fallback_parse(self, raw_response: str, result: dict) -> dict:
        """Fallback parsing when structured block is missing or malformed."""
        # Try to find any JSON block
        json_pattern = r'```(?:json)?\s*(.*?)\s*```'
        match = re.search(json_pattern, raw_response, re.DOTALL)

        if match:
            json_str = match.group(1)
            data = self._try_parse_json(json_str)
            if data:
                result["narrative"] = raw_response[:match.start()].strip()
                self._apply_data(result, data)
                return result

        # Strip any partial/garbled JSON blocks from narrative
        narrative = raw_response.strip()
        narrative = re.sub(r'```(?:game_state|json)\s*\{.*$', '', narrative, flags=re.DOTALL).strip()
        narrative = re.sub(r'```\s*$', '', narrative).strip()
        result["narrative"] = narrative
        return result

    @staticmethod
    def _normalize_punctuation(text: str) -> str:
        """规范化全角/智能标点为半角 ASCII，让 json.loads 能识别。

        智能双引号只替换 JSON 结构位置（定界符），保留 value 内部的。
        处理全角冒号/逗号/括号在 JSON 结构位置的场景。
        """
        # \u667a\u80fd\u53cc\u5f15\u53f7\uff1a\u53ea\u66ff\u6362 JSON \u7ed3\u6784\u4f4d\u7f6e\u7684\uff08key/value \u5b9a\u754c\u7b26\uff09\uff0c\u4fdd\u7559 value \u5185\u90e8\u7684
        text = re.sub(r'(?<=[\{\[,:])(\s*)\u201c', r'\1"', text)
        text = re.sub(r'^\s*\u201c', '"', text, flags=re.MULTILINE)
        text = re.sub(r'\u201d(\s*)(?=[:,\}\]])', r'"\1', text)
        text = re.sub(r'\u201d\s*$', '"', text, flags=re.MULTILINE)
        text = re.sub(r'\u201c(?=")', '"', text)
        text = re.sub(r'(?<=")\u201d', '"', text)
        # 智能单引号 → 直引号
        text = text.replace('\u2018', "'").replace('\u2019', "'")
        # 全角冒号/逗号 → 半角（仅在 JSON 结构位置替换，避免破坏中文叙事值）
        text = re.sub(r'(?<=")\uff1a', ':', text)   # "：  → ":
        text = re.sub(r'\uff1a(?=")', ':', text)     #  ：" →  :"
        text = re.sub(r'(?<=[\]}"\d])\uff0c', ',', text)  # ]，  → ],
        text = re.sub(r'\uff0c(?=[\[{"a-z])', ',', text)  # ，[  → ,[
        # 全角括号
        text = text.replace('\uff5b', '{').replace('\uff5d', '}')
        text = text.replace('\uff3b', '[').replace('\uff3d', ']')
        return text

    @staticmethod
    def _try_parse_json(text: str) -> dict | None:
        """Try parsing JSON with progressive cleanup."""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Clean trailing commas and control chars
        cleaned = text
        cleaned = re.sub(r',\s*([\]}])', r'\1', cleaned)
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        return None

    def _fix_truncated_keys(self, text: str) -> str:
        """Replace truncated JSON keys with their full field names.

        Only game_state fields at the root may be expanded; nested object keys
        such as a new NPC's ``location`` must remain unchanged.
        """
        depth = 0
        last = 0
        parts = []
        for match in re.finditer(r'"(?:\\.|[^"\\])*"|[{}]', text):
            token = match.group()
            if token == '{':
                depth += 1
            elif token == '}':
                depth -= 1
            elif depth == 1 and re.match(r'\s*:', text[match.end():]):
                key = token[1:-1]
                full = self._KEY_PREFIX_MAP.get(key)
                if full and full != key:
                    parts.extend((text[last:match.start()], f'"{full}"'))
                    last = match.end()
        parts.append(text[last:])
        return ''.join(parts)

    @classmethod
    def _try_close_json(cls, text: str) -> dict | None:
        """Try to fix truncated JSON by closing open strings/arrays/objects."""
        if not text.strip().startswith('{'):
            return None

        repaired = text.rstrip()

        # Walk the JSON to find the last structurally-valid position
        in_string = False
        last_good = 0
        depth_brace = 0
        depth_bracket = 0
        i = 0
        while i < len(repaired):
            c = repaired[i]
            if c == '\\' and in_string:
                i += 2
                continue
            if c == '"':
                in_string = not in_string
            elif not in_string:
                if c == '{':
                    depth_brace += 1
                elif c == '}':
                    depth_brace -= 1
                elif c == '[':
                    depth_bracket += 1
                elif c == ']':
                    depth_bracket -= 1
                if c in (',', '}', ']'):
                    last_good = i
            i += 1

        if in_string or depth_brace > 0 or depth_bracket > 0:
            if last_good > 0:
                repaired = repaired[:last_good + 1]

            repaired = repaired.rstrip().rstrip(',').rstrip()

            # Recount what needs closing
            in_string = False
            stack = []
            i = 0
            while i < len(repaired):
                c = repaired[i]
                if c == '\\' and in_string:
                    i += 2
                    continue
                if c == '"':
                    in_string = not in_string
                elif not in_string:
                    if c == '{':
                        stack.append('}')
                    elif c == '[':
                        stack.append(']')
                    elif c in ('}', ']'):
                        if stack and stack[-1] == c:
                            stack.pop()
                i += 1

            if in_string:
                repaired += '"'

            while stack:
                repaired += stack.pop()

            result = cls._try_parse_json(repaired)
            if result:
                return result

        return None

    @classmethod
    def _extract_fields_regex(cls, text: str) -> dict | None:
        """Extract individual game_state fields from garbled JSON using regex."""
        result = {}

        # Try each known field with flexible patterns
        for field in cls._GAME_STATE_FIELDS:
            val = cls._extract_single_field(text, field)
            if val is not None:
                result[field] = val

        return result if result else None

    @classmethod
    def _extract_single_field(cls, text: str, field: str):
        """Extract a single field value from (possibly garbled) JSON text.

        Handles both full field names and truncated prefixes.
        """
        # Build regex pattern that matches the field name or any unique prefix
        # For array fields: "field_name" : [...]
        # For string fields: "field_name" : "..."
        # For null: "field_name" : null

        # Try full field name first, then progressively shorter prefixes
        candidates = [field]
        for length in range(len(field) - 1, 1, -1):
            prefix = field[:length]
            if prefix in cls._KEY_PREFIX_MAP and cls._KEY_PREFIX_MAP[prefix] == field:
                candidates.append(prefix)

        array_fields = {
            "choices", "state_changes", "reveal_locations",
            "npc_attitude_changes", "activate_states", "deactivate_states",
            "world_property_changes", "inventory_changes",
            "offscreen_npc_updates", "new_npcs", "npc_relationship_updates",
            "npc_met_changes", "invalidate_lore",
            "faction_reputation_changes", "moral_alignment_changes",
            "npc_location_changes", "room_changes",
            "recruit_companions", "dismiss_companions", "npc_interjections",
        }
        string_fields = {"end_time", "location_change"}

        for cand in candidates:
            escaped = re.escape(cand)
            if field in array_fields:
                # Match array: "key" : [...] — use bracket counting, not greedy regex
                m = re.search(rf'"{escaped}"\s*:\s*\[', text)
                if m:
                    arr_str = cls._extract_balanced(text, m.end() - 1, '[', ']')
                    if arr_str:
                        try:
                            val = json.loads(arr_str)
                            if isinstance(val, list):
                                return val
                        except json.JSONDecodeError:
                            # Try fixing the array
                            fixed = cls._try_close_bracket(arr_str)
                            if fixed:
                                try:
                                    val = json.loads(fixed)
                                    if isinstance(val, list):
                                        return val
                                except json.JSONDecodeError:
                                    pass
            elif field in string_fields:
                m = re.search(rf'"{escaped}"\s*:\s*"([^"]*)"', text)
                if m:
                    return m.group(1)
            elif field in ("game_over", "scene_details"):
                # Can be null or object
                m = re.search(rf'"{escaped}"\s*:\s*null', text)
                if m:
                    return None
                m = re.search(rf'"{escaped}"\s*:\s*\{{', text)
                if m:
                    obj_str = cls._extract_balanced(text, m.end() - 1, '{', '}')
                    if obj_str:
                        try:
                            return json.loads(obj_str)
                        except json.JSONDecodeError:
                            pass

        return None

    @staticmethod
    def _extract_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
        """Extract a balanced bracket/brace pair starting at `start`."""
        if start >= len(text) or text[start] != open_ch:
            return None
        depth = 0
        in_str = False
        i = start
        while i < len(text):
            c = text[i]
            if c == '\\' and in_str:
                i += 2
                continue
            if c == '"':
                in_str = not in_str
            elif not in_str:
                if c == open_ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
            i += 1
        # Unbalanced — return what we have (caller may try to close it)
        return text[start:]

    @staticmethod
    def _try_close_bracket(text: str) -> str | None:
        """Try closing an unbalanced array or object."""
        repaired = text.rstrip().rstrip(',').rstrip()
        in_str = False
        stack = []
        i = 0
        while i < len(repaired):
            c = repaired[i]
            if c == '\\' and in_str:
                i += 2
                continue
            if c == '"':
                in_str = not in_str
            elif not in_str:
                if c in ('{', '['):
                    stack.append('}' if c == '{' else ']')
                elif c in ('}', ']'):
                    if stack and stack[-1] == c:
                        stack.pop()
            i += 1
        if in_str:
            repaired += '"'
        while stack:
            repaired += stack.pop()
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _recover_choices_from_garbled(text: str) -> list[dict] | None:
        """Last-resort: extract choice text patterns from severely garbled JSON.

        Scans for "text": "..." patterns and builds choice objects from them.
        Also handles smart quotes and missing delimiters.
        """
        # Normalize smart quotes for matching
        norm = text.replace('\u201c', '"').replace('\u201d', '"')

        # Strategy 1: standard "text":"..." pattern
        matches = re.findall(r'"text"\s*:\s*"([^"]{2,})"', norm)

        # Strategy 2: if nothing found, try looser pattern —
        # look for choice-like Chinese text between quotes that are 5+ chars
        # and contain common choice keywords
        if not matches:
            # Find any quoted Chinese text that looks like a player choice
            all_quoted = re.findall(r'"([^"]{5,80})"', norm)
            choice_keywords = ("尝试", "前往", "继续", "调查", "询问", "查看",
                               "攻击", "逃跑", "交谈", "观察", "寻找", "等待",
                               "趁", "借口", "悄悄", "仔细", "找机会")
            matches = [q for q in all_quoted
                       if any(kw in q for kw in choice_keywords)]

        if not matches:
            return None

        choices = []
        seen = set()
        for i, text_val in enumerate(matches):
            # 去重
            if text_val in seen:
                continue
            seen.add(text_val)
            choices.append({"id": f"c{i + 1}", "text": text_val})

        return choices if choices else None

    # ---- Split-mode parsing (three-request architecture) ----

    def parse_split(self, narrative: str, raw_choices: str, raw_state: str) -> dict:
        """Parse three separate AI responses into unified result dict."""
        result = self._empty_result()
        result["narrative"] = narrative.strip()

        # Parse choices JSON
        choices_data = self._extract_json_from_raw(raw_choices)
        if choices_data and isinstance(choices_data.get("choices"), list):
            result["choices"] = choices_data["choices"]
        else:
            # Fallback: try garbled recovery on raw_choices text
            recovered = self._recover_choices_from_garbled(raw_choices)
            if recovered:
                result["choices"] = recovered

        # Parse state JSON
        state_data = self._extract_json_from_raw(raw_state)
        if state_data:
            self._apply_data(result, state_data)
        elif raw_state:
            logger.warning("state JSON 解析失败，本回合状态不会更新。原文前100字: %s", raw_state[:100])
            result["_state_parse_failed"] = True

        # Fallback choices
        if not result["choices"]:
            result["choices"] = [
                {"id": "c1", "text": "继续探索"},
                {"id": "c2", "text": "与周围的人交谈"},
                {"id": "c3", "text": "做其他事情"},
            ]

        # Ensure choice IDs
        for i, c in enumerate(result["choices"]):
            if "id" not in c:
                c["id"] = f"c{i + 1}"

        return result

    def _extract_json_from_raw(self, text: str) -> dict | None:
        """从原始 AI 输出中提取 JSON 对象（复用现有解析逻辑）。"""
        if not text:
            return None
        text = text.strip()
        text = self._normalize_punctuation(text)
        # 去除 code fences
        text = re.sub(r'^```(?:json|game_state)\s*\n?', '', text)
        text = re.sub(r'\n?\s*```\s*$', '', text)
        text = text.strip()
        # 修复截断 key
        text = self._fix_truncated_keys(text)
        data = self._try_parse_json(text)
        if data:
            return data
        data = self._try_close_json(text)
        if data:
            return data
        # Regex field extraction as last resort
        data = self._extract_fields_regex(text)
        if data:
            return data
        return None

    # ---- Split-v2 parsing (8-stage pipeline) ----

    _NPC_REACTION_FIELDS = [
        "npc_attitude_changes", "npc_met_changes",
        "npc_relationship_updates", "new_npcs", "scene_details",
        "npc_interjections",
    ]

    _WORLD_STATE_RESOURCE_FIELDS = [
        "state_changes", "inventory_changes",
        "activate_states", "deactivate_states", "game_over",
    ]

    _WORLD_STATE_SPATIAL_FIELDS = [
        "location_change", "reveal_locations",
        "npc_location_changes", "room_changes", "scene_details",
    ]

    _WORLD_STATE_TEMPORAL_FIELDS = [
        "end_time",
    ]

    _WORLD_STATE_WORLD_FIELDS = [
        "world_property_changes",
    ]

    _WORLD_STATE_CORE_FIELDS = (
        _WORLD_STATE_RESOURCE_FIELDS + _WORLD_STATE_SPATIAL_FIELDS + _WORLD_STATE_TEMPORAL_FIELDS + _WORLD_STATE_WORLD_FIELDS
    )

    _WORLD_STATE_EXT_FIELDS = [
        "offscreen_npc_updates", "new_npcs",
        "faction_reputation_changes", "moral_alignment_changes",
        "recruit_companions", "dismiss_companions",
        "invalidate_lore",
    ]

    _WORLD_STATE_FIELDS = list(_WORLD_STATE_CORE_FIELDS) + _WORLD_STATE_EXT_FIELDS

    def parse_split_v2(
        self, narrative: str, raw_npc: str, raw_world: str,
        raw_world_ext: str = "",
    ) -> dict:
        """Parse 8-stage pipeline outputs: narrative + NPC reactions + world state.

        Merges scene_details from both NPC and world responses.
        When raw_world_ext is provided, parses it as extended world state fields.
        """
        result = self._empty_result()
        result["narrative"] = narrative.strip()

        # Parse NPC reaction JSON
        npc_data = self._extract_json_from_raw(raw_npc)
        if npc_data:
            for field in self._NPC_REACTION_FIELDS:
                val = npc_data.get(field)
                if val is not None:
                    result[field] = val
        elif raw_npc and raw_npc.strip() and raw_npc.strip() != "{}":
            logger.warning("NPC reaction JSON 解析失败。原文前100字: %s", raw_npc[:100])

        # Parse world state JSON (core or unified)
        world_data = self._extract_json_from_raw(raw_world)
        if world_data:
            fields = self._WORLD_STATE_CORE_FIELDS if raw_world_ext else self._WORLD_STATE_FIELDS
            for field in fields:
                val = world_data.get(field)
                if val is not None:
                    if field == "scene_details" and result.get("scene_details"):
                        merged = {**val}
                        npc_scene = result["scene_details"]
                        if isinstance(npc_scene, dict):
                            if npc_scene.get("npc_expressions"):
                                merged.setdefault("npc_expressions", npc_scene["npc_expressions"])
                            if npc_scene.get("pending_tension"):
                                merged.setdefault("pending_tension", npc_scene["pending_tension"])
                        result[field] = merged
                    else:
                        result[field] = val
        elif raw_world:
            logger.warning("world state JSON 解析失败，本回合状态不会更新。原文前100字: %s", raw_world[:100])
            result["_state_parse_failed"] = True

        # Parse extended world state JSON
        if raw_world_ext:
            ext_data = self._extract_json_from_raw(raw_world_ext)
            if ext_data:
                for field in self._WORLD_STATE_EXT_FIELDS:
                    val = ext_data.get(field)
                    if val is not None:
                        result[field] = val
            elif raw_world_ext.strip() and raw_world_ext.strip() != "{}":
                logger.warning("world ext JSON 解析失败。原文前100字: %s", raw_world_ext[:100])

        return result

    def parse_split_v3(
        self, narrative: str, raw_npc: str,
        raw_resource: str, raw_spatial: str,
        raw_temporal: str, raw_world: str, raw_ext: str,
    ) -> dict:
        """Parse 5-way split 4b outputs: resource + spatial + temporal + world + ext."""
        result = self._empty_result()
        result["narrative"] = narrative.strip()

        npc_data = self._extract_json_from_raw(raw_npc)
        if npc_data:
            for field in self._NPC_REACTION_FIELDS:
                val = npc_data.get(field)
                if val is not None:
                    result[field] = val
        elif raw_npc and raw_npc.strip() and raw_npc.strip() != "{}":
            logger.warning("NPC reaction JSON 解析失败。原文前100字: %s", raw_npc[:100])

        for raw, fields, label in [
            (raw_resource, self._WORLD_STATE_RESOURCE_FIELDS, "resource"),
            (raw_spatial, self._WORLD_STATE_SPATIAL_FIELDS, "spatial"),
            (raw_temporal, self._WORLD_STATE_TEMPORAL_FIELDS, "temporal"),
            (raw_world, self._WORLD_STATE_WORLD_FIELDS, "world"),
            (raw_ext, self._WORLD_STATE_EXT_FIELDS, "ext"),
        ]:
            if not raw or raw.strip() in ("", "{}"):
                continue
            data = self._extract_json_from_raw(raw)
            if data:
                for field in fields:
                    val = data.get(field)
                    if val is not None:
                        if field == "scene_details" and result.get("scene_details"):
                            merged = {**val}
                            existing = result["scene_details"]
                            if isinstance(existing, dict):
                                if existing.get("npc_expressions"):
                                    merged.setdefault("npc_expressions", existing["npc_expressions"])
                                if existing.get("pending_tension"):
                                    merged.setdefault("pending_tension", existing["pending_tension"])
                            result[field] = merged
                        else:
                            result[field] = val
            else:
                logger.warning("world %s JSON 解析失败。原文前100字: %s", label, raw[:100])
                if label == "resource":
                    result["_state_parse_failed"] = True

        return result

    def parse_npc_reaction(self, raw_npc: str) -> dict:
        """Parse NPC reaction JSON independently. Returns dict of NPC fields."""
        if not raw_npc or raw_npc.strip() in ("", "{}"):
            return {}
        data = self._extract_json_from_raw(raw_npc)
        if not data:
            return {}
        result = {}
        for field in self._NPC_REACTION_FIELDS:
            val = data.get(field)
            if val is not None:
                result[field] = val
        return result

    def parse_choices(self, raw_choices: str) -> list[dict]:
        """Parse choices from a separate AI response. Returns list of choice dicts."""
        choices_data = self._extract_json_from_raw(raw_choices)
        if choices_data and isinstance(choices_data.get("choices"), list):
            choices = choices_data["choices"]
        elif choices_data and isinstance(choices_data, list):
            choices = choices_data
        else:
            recovered = self._recover_choices_from_garbled(raw_choices)
            choices = recovered if recovered else []

        if not choices:
            choices = [
                {"id": "c1", "text": "继续探索"},
                {"id": "c2", "text": "与周围的人交谈"},
                {"id": "c3", "text": "做其他事情"},
            ]

        for i, c in enumerate(choices):
            if "id" not in c:
                c["id"] = f"c{i + 1}"

        return choices
