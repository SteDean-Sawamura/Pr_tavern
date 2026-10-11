"""Lorebook / World Info system — keyword-triggered context injection.

Inspired by SillyTavern's World Info system. Entries are only injected into
the AI prompt when their keywords appear in recent chat history or player input.
"""

import base64
import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, field


@dataclass
class LorebookEntry:
    id: str
    keys: list[str]
    content: str
    secondary_keys: list[str] = field(default_factory=list)
    enabled: bool = True
    constant: bool = False  # Always injected regardless of keywords
    priority: int = 100  # Higher = injected first
    scan_depth: int = 3  # How many recent messages to scan
    position: str = "after_world"  # Where to inject: before_char, after_world, at_end
    comment: str = ""  # Human-readable label
    entry_type: str = "manual"  # "manual" | "npc_profile" | "npc_relationship" | "event_context"
    related_entries: list[str] = field(default_factory=list)  # IDs of related entries (knowledge graph edges)
    sticky: int = 0  # Remain active for N turns after keyword match (0 = disabled)
    cooldown: int = 0  # After sticky expires, cannot re-activate for N turns (0 = disabled)
    probability: int = 100  # 0-100, chance of activation when keywords match
    group: str = ""  # Mutual exclusion group name; only one entry per group is selected
    group_weight: int = 100  # Weight for weighted random selection within group
    selective_logic: str = "AND_ANY"  # AND_ANY | NOT_ANY | AND_ALL | NOT_ALL
    depth: int = 0  # For position="at_depth": insert at this depth from end of messages
    role: str = "system"  # For position="at_depth": message role (system/user/assistant)
    visibility: str = "public"  # "public" | "world" | "hidden"
    known_by_npcs: list[str] = field(default_factory=list)
    discoverable: dict = field(default_factory=dict)
    available_from: str = ""
    last_activated_turn: int = 0


class Lorebook:
    """Scans text for keyword matches and returns activated entries."""

    # P0-9: 短关键字（如 "a"/"我"/"me"）会与几乎任何文本匹配，造成"雪崩激活"。
    # 对纯 ASCII 关键字要求 ≥3 字符，对 CJK 关键字要求 ≥2 字符。
    _CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
    _DATE_RE = re.compile(r'([一二三四五六七八九十]+|[0-9]{1,2})月([一二三四五六七八九十]+|[0-9]{1,2})[日号]')

    @classmethod
    def _normalize_query_dates(cls, text: str) -> str:
        digits = {character: value for value, character in enumerate("一二三四五六七八九", 1)}

        def number(value: str) -> int | None:
            if value.isascii() and value.isdigit():
                return int(value)
            tens, separator, ones = value.partition("十")
            if not separator:
                return digits.get(value)
            if "十" in ones or (tens and tens not in digits) or (ones and ones not in digits):
                return None
            return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)

        def replace(match: re.Match) -> str:
            month, day = (number(value) for value in match.groups())
            if month is None or day is None or not (1 <= month <= 12 and 1 <= day <= 31):
                return match.group()
            return f"{month}月{day}日"

        return cls._DATE_RE.sub(replace, text)

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Rough token count: CJK ~1.5 chars/token, ASCII ~4 chars/token."""
        cjk = len(cls._CJK_RE.findall(text))
        non_cjk = len(text) - cjk
        return int(cjk / 1.5 + non_cjk / 4) + 1

    @classmethod
    def _key_too_short(cls, key: str) -> bool:
        k = key.strip()
        if not k:
            return True
        if cls._CJK_RE.search(k):
            return len(k) < 2
        return len(k) < 3

    @classmethod
    def _primary_keys(cls, keys: list[str]) -> list[str]:
        return [cls._normalize_query_dates(alias.lower()) for key in keys
                for alias in (part.strip() for part in re.split(r"[,，、;；]+", key))
                if not cls._key_too_short(alias)]

    def primary_key_match_count(self, entry_id: str, text: str) -> int:
        normalized = self._normalize_query_dates(text.lower())
        primary, _ = self._precomputed.get(entry_id, ([], []))
        return sum(key in normalized for key in primary)

    @staticmethod
    def _content_fingerprint(content: str) -> str:
        """Short hash of entry content — used to detect edits during cooldown."""
        return hashlib.md5(content.encode()).hexdigest()[:8]

    @staticmethod
    def _check_secondary(sk: list[str], text_lower: str, logic: str) -> bool:
        """Check secondary keywords against text using the given logic mode."""
        if not sk:
            return True
        if logic == "AND_ANY":
            return any(k in text_lower for k in sk)
        if logic == "AND_ALL":
            return all(k in text_lower for k in sk)
        if logic == "NOT_ANY":
            return not any(k in text_lower for k in sk)
        if logic == "NOT_ALL":
            return not all(k in text_lower for k in sk)
        return any(k in text_lower for k in sk)

    def __init__(self, entries: list[dict], max_recursion: int = 2, token_budget: int = 0):
        self.entries = [self._parse_entry(e) for e in entries]
        self.max_recursion = max_recursion
        self.token_budget = token_budget  # 0 = unlimited
        # 预处理：缓存小写化 + 过滤后的关键词列表，避免每次 scan 重复计算
        self._precomputed: dict[str, tuple[list[str], list[str]]] = {}
        for entry in self.entries:
            pk = self._primary_keys(entry.keys)
            sk = [self._normalize_query_dates(k.lower()) for k in entry.secondary_keys
                  if k.strip() and not self._key_too_short(k)]
            self._precomputed[entry.id] = (pk, sk)

    @staticmethod
    def _parse_entry(data: dict) -> LorebookEntry:
        return LorebookEntry(
            id=data.get("id", ""),
            keys=data.get("keys", []),
            content=data.get("content", ""),
            secondary_keys=data.get("secondary_keys", []),
            enabled=data.get("enabled", True),
            constant=data.get("constant", False),
            priority=data.get("priority", 100),
            scan_depth=data.get("scan_depth", 3),
            position=data.get("position", "after_world"),
            comment=data.get("comment", ""),
            entry_type=data.get("entry_type", "manual"),
            related_entries=data.get("related_entries", []),
            sticky=data.get("sticky", 0),
            cooldown=data.get("cooldown", 0),
            probability=data.get("probability", 100),
            group=data.get("group", ""),
            group_weight=data.get("group_weight", 100),
            selective_logic=data.get("selective_logic", "AND_ANY"),
            depth=data.get("depth", 0),
            role=data.get("role", "system"),
            visibility=data.get("visibility", "public"),
            known_by_npcs=data.get("known_by_npcs", []),
            discoverable=data.get("discoverable", {}),
            available_from=data.get("available_from", ""),
        )

    def query(self, keyword: str = "", entry_id: str = "", page: int = 0,
              offset: int = 0, excluded_ids: set[str] | None = None,
              entry_ids: list[str] | None = None, snapshot: str | None = None,
              snapshot_scope: tuple = ()) -> dict:
        if not isinstance(keyword, str) or not isinstance(entry_id, str):
            return {"error": "keyword 和 entry_id 必须是字符串"}
        if type(page) is not int or page < 0 or type(offset) is not int or offset < 0:
            return {"error": "page 和 offset 必须是非负整数；搜索用 page，按 entry_id 阅读用 offset"}
        if snapshot is not None and (not isinstance(snapshot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", snapshot)):
            return {"error": "snapshot 必须是查询返回的快照字符串"}
        if entry_ids is not None:
            if (not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= 3
                    or any(not isinstance(item, str) or not item.strip() for item in entry_ids)
                    or len(set(entry_ids)) != len(entry_ids)):
                return {"error": "entry_ids 必须包含 1-3 个不重复的条目 ID"}
            if keyword or entry_id or page or offset:
                return {"error": "entry_ids 不可与 keyword、entry_id、page 或 offset 混用"}
        excluded = excluded_ids or set()
        available = [entry for entry in self.entries if entry.enabled and entry.id not in excluded]
        available_ids = {entry.id for entry in available}
        query_text = self._normalize_query_dates(keyword.casefold())
        keywords = list(dict.fromkeys(part for part in re.split(r"[\s,，、;；?？!！。]+", query_text) if part))
        approximate = False
        if entry_ids is not None:
            by_id = {entry.id: entry for entry in available}
            candidates = [by_id[item] for item in entry_ids if item in by_id]
        elif entry_id:
            candidates = [entry for entry in available if entry.id == entry_id]
        elif keywords:
            scored = []
            preview_terms = {}
            for entry in available:
                title = self._normalize_query_dates(
                    " ".join([entry.id, entry.comment, *entry.keys, *entry.secondary_keys]).casefold()
                )
                searchable = title + " " + self._normalize_query_dates(entry.content.casefold())
                matched = sum(term in searchable for term in keywords)
                matched_aliases = [
                    (alias, normalized) for key in entry.keys
                    for alias in (part.strip().casefold() for part in re.split(r"[,，、;；]+", key))
                    if not self._key_too_short(alias)
                    for normalized in (self._normalize_query_dates(alias),)
                    if normalized in query_text
                ]
                matched_keys = list(dict.fromkeys(normalized for _, normalized in matched_aliases))
                if matched or matched_keys:
                    primary_keys = self._precomputed[entry.id][0]
                    primary_key = primary_keys[0] if primary_keys else ""
                    primary_match = int(primary_key in matched_keys)
                    longest_match = max(
                        max((2 if re.fullmatch(r"\d{4}年\d{1,2}月", key) else len(key)
                             for key in matched_keys), default=0),
                        max((len(term) for term in keywords if term in searchable), default=0),
                    )
                    scored.append((len(keywords) > 1 and matched == len(keywords),
                                   longest_match, primary_match, len(matched_keys), matched,
                                   sum(term in title for term in keywords),
                                   entry.priority, entry))
                    preview_terms[entry.id] = list(dict.fromkeys([
                        *(alias for alias, _ in matched_aliases), *keywords,
                    ]))
            scored.sort(key=lambda item: item[:-1], reverse=True)
            candidates = [item[-1] for item in scored]
            unmatched_long_clause = (
                len(keywords) > 1
                and max((item[1] for item in scored), default=0) < 8
                and not any(item[3] for item in scored)
                and any(re.search(r"[\u3400-\u9fff]{8}", term) for term in keywords)
            )
            if len(query_text) >= 8 and (
                not candidates or not any(item[4] for item in scored) or unmatched_long_clause
            ):
                fragments = list(dict.fromkeys(
                    run[index:index + 2]
                    for run in re.findall(r"[\u3400-\u9fff]{2,}", query_text)
                    for index in range(len(run) - 1)
                ))
                if len(fragments) >= 4:
                    approximate_scored = []
                    for entry in available:
                        title = self._normalize_query_dates(
                            " ".join([entry.id, entry.comment, *entry.keys, *entry.secondary_keys]).casefold()
                        )
                        searchable = title + " " + self._normalize_query_dates(entry.content.casefold())
                        matched_positions = [index for index, fragment in enumerate(fragments)
                                             if fragment in searchable]
                        coverage_spans = sum(index == 0 or position > matched_positions[index - 1] + 1
                                             for index, position in enumerate(matched_positions))
                        if len(matched_positions) < 3 and coverage_spans < 2:
                            continue
                        primary = self._normalize_query_dates(" ".join(entry.keys).casefold())
                        primary_positions = [index for index in matched_positions
                                             if fragments[index] in primary]
                        primary_spans = sum(index == 0 or position > primary_positions[index - 1] + 1
                                            for index, position in enumerate(primary_positions))
                        canonical_keys = self._precomputed[entry.id][0]
                        canonical_boost = 2 if (coverage_spans > 1 and canonical_keys
                                                and canonical_keys[0] in query_text) else 0
                        if any(len(key) == 2 and key in query_text for key in canonical_keys):
                            canonical_boost = max(canonical_boost, 2)
                        if (canonical_keys and len(canonical_keys[0]) >= 4
                                and canonical_keys[0] in query_text
                                and len(canonical_keys[0]) * 2 >= len(query_text.rstrip("?？!！。"))):
                            canonical_boost = max(canonical_boost, len(canonical_keys[0]))
                        matched = [fragments[index] for index in matched_positions]
                        approximate_scored.append((primary_spans, coverage_spans, len(matched),
                                                   len(primary_positions), sum(fragment in title for fragment in matched),
                                                   entry.priority, canonical_boost, entry))
                        preview_terms[entry.id] = matched
                    if candidates:
                        approximate_scored.sort(
                            key=lambda item: (item[1] + item[6], item[2], item[0], item[3], item[4], item[5]),
                            reverse=True,
                        )
                        supplemental = [item[-1] for item in approximate_scored
                                        if (item[1] >= 2 and (item[2] >= 4 or (item[0] and item[2] >= 3)))
                                        or item[6] >= 4]
                        if supplemental:
                            supplemental[:0] = [item[-1] for item in scored
                                                if any(len(self._CJK_RE.findall(key)) >= 6 and key in query_text
                                                       for key in self._precomputed[item[-1].id][0])]
                            seen_ids = set()
                            combined = []
                            for entry in [*supplemental, *candidates]:
                                if entry.id not in seen_ids:
                                    combined.append(entry)
                                    seen_ids.add(entry.id)
                            candidates = combined
                            approximate = True
                    else:
                        approximate_scored.sort(key=lambda item: item[:-1], reverse=True)
                        candidates = [item[-1] for item in approximate_scored]
                        approximate = bool(candidates)
        else:
            candidates = []

        def snapshot_for(identifier=None):
            scope = [entry for entry in candidates if entry.id == identifier] if identifier is not None else candidates
            payload = [
                snapshot_scope, "entry" if identifier is not None else "query",
                identifier if identifier is not None else [query_text, entry_ids],
                [[asdict(entry), [related for related in entry.related_entries if related in available_ids]]
                 for entry in scope],
            ]
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).digest()[:16]
            return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        current_snapshot = snapshot_for(entry_id or None)
        if (((page > 0 or offset > 0) and snapshot is None)
                or (snapshot is not None and snapshot != current_snapshot)):
            restart = ({"entry_id": entry_id} if entry_id else
                       {"entry_ids": entry_ids} if entry_ids is not None else {"keyword": keyword})
            return {
                "error": "快照缺失或已失效；丢弃旧正文片段，按 restart 从头重查，再携带新 snapshot 续读。",
                "error_code": "snapshot_required" if snapshot is None else "snapshot_expired",
                "restart_required": True, "restart": restart,
            }
        total_matches = len(candidates)
        start = 0 if entry_id or entry_ids is not None else page * 5
        selected = candidates[start:start + 5]
        results = []
        for entry in selected:
            content_start = offset if entry_id else 0
            if not entry_id and entry_ids is None:
                content_lower = entry.content.casefold()
                terms = preview_terms.get(entry.id, keywords)
                matches = [
                    (len(term), position)
                    for term in terms
                    if (position := content_lower.find(term)) >= 0
                ]
                for date_match in self._DATE_RE.finditer(content_lower):
                    normalized_date = self._normalize_query_dates(date_match.group())
                    if normalized_date == date_match.group():
                        continue
                    for term in terms:
                        if normalized_date in term:
                            expanded = term.replace(normalized_date, date_match.group())
                            if (position := content_lower.find(expanded)) >= 0:
                                matches.append((len(term), position))
                if matches:
                    _, position = max(matches, key=lambda match: (match[0], -match[1]))
                    content_start = max(0, position - 80)
            content = entry.content[content_start:content_start + (2000 if entry_id or entry_ids is not None else 400)]
            content_end = content_start + len(content)
            results.append({
                "id": entry.id,
                "snapshot": snapshot_for(entry.id),
                "title": entry.comment or ", ".join(entry.keys[:2]) or entry.id,
                "content": content,
                "offset": content_start,
                "next_offset": content_end if content_end < len(entry.content) else None,
                "total_length": len(entry.content),
                "truncated": content_start > 0 or content_end < len(entry.content),
                "visibility": entry.visibility,
                "related_entries": [related for related in entry.related_entries if related in available_ids],
            })
        return {
            "entries": results, "total_matches": total_matches,
            "snapshot": current_snapshot,
            "next_page": page + 1 if not entry_id and entry_ids is None and start + 5 < total_matches else None,
            **({"approximate": True} if approximate else {}),
        }

    def partial_keyword_candidates(
        self, player_action: str, activated_ids: set[str], excluded_ids: set[str],
        max_entries: int = 3,
    ) -> list[str]:
        """Find available entries matching primary but not secondary keywords."""
        action_lower = self._normalize_query_dates(player_action.lower())
        candidates = []
        for entry in self.entries:
            if (not entry.enabled or entry.constant or entry.id in activated_ids
                    or entry.id in excluded_ids or entry.selective_logic not in ("AND_ANY", "AND_ALL")):
                continue
            primary, secondary = self._precomputed[entry.id]
            matched = [key for key in primary if key in action_lower]
            if not matched or not secondary or self._check_secondary(secondary, action_lower, entry.selective_logic):
                continue
            candidates.append((primary[0] in action_lower, len(matched),
                               max(map(len, matched)), entry.priority, entry.id))
        candidates.sort(reverse=True)
        return [entry_id for _, _, _, _, entry_id in candidates[:max_entries]]

    def scan(
        self,
        player_action: str,
        recent_messages: list[str],
        max_entries: int = 20,
        timed_state: dict | None = None,
        turn_number: int = 0,
        excluded_entry_ids: set[str] | None = None,
    ) -> tuple[list[LorebookEntry], dict]:
        """Scan player action and recent history for keyword matches.

        Returns (activated_entries, updated_timed_state).
        timed_state tracks sticky/cooldown timers: {"sticky": {id: remaining}, "cooldown": {id: remaining}}
        """
        ts = timed_state or {}
        sticky_timers: dict[str, int] = dict(ts.get("sticky", {}))
        cooldown_timers: dict[str, int] = dict(ts.get("cooldown", {}))
        entries = ([e for e in self.entries if e.id not in excluded_entry_ids]
                   if excluded_entry_ids else self.entries)
        entry_by_id = {e.id: e for e in entries}

        activated: dict[str, LorebookEntry] = {}

        # Phase 0: Activate sticky entries (still within their sticky window)
        for eid, remaining in list(sticky_timers.items()):
            if remaining > 0 and eid in entry_by_id and entry_by_id[eid].enabled:
                activated[eid] = entry_by_id[eid]

        # Phase 1: Collect constant entries
        for entry in entries:
            if entry.constant and entry.enabled:
                activated[entry.id] = entry

        # Phase 2: Scan for keyword matches, respecting per-entry scan_depth
        action_matched_ids = set(self._scan_text_with_depth(player_action, [], activated, entries))
        newly_activated = self._scan_text_with_depth(
            player_action, recent_messages, activated, entries
        )

        # Apply cooldown and probability filters to newly matched entries
        fingerprints: dict[str, str] = dict(ts.get("fingerprints", {}))
        filtered_newly = {}
        for eid, entry in newly_activated.items():
            if cooldown_timers.get(eid, 0) > 0:
                # Content changed since cooldown started -> lift cooldown
                old_fp = fingerprints.get(eid, "")
                new_fp = self._content_fingerprint(entry.content or "")
                if old_fp and old_fp != new_fp:
                    cooldown_timers[eid] = 0
                else:
                    continue
            if entry.probability < 100 and random.randint(1, 100) > entry.probability:
                continue
            filtered_newly[eid] = entry
        activated.update(filtered_newly)

        # Phase 3: Activate related entries (knowledge graph edges)
        related_newly = self._activate_related(activated, entries)
        activated.update(related_newly)

        # Phase 4: Recursive scanning (activated content may trigger more entries)
        all_newly = {**filtered_newly, **related_newly}
        for _ in range(self.max_recursion):
            if not all_newly:
                break
            extra_text = "\n".join(e.content for e in all_newly.values())
            all_newly = self._scan_text(extra_text, activated, entries)
            # Apply cooldown/probability to recursive matches too
            filtered_rec = {}
            for eid, entry in all_newly.items():
                if cooldown_timers.get(eid, 0) > 0:
                    old_fp = fingerprints.get(eid, "")
                    new_fp = self._content_fingerprint(entry.content or "")
                    if old_fp and old_fp != new_fp:
                        cooldown_timers[eid] = 0
                    else:
                        continue
                if entry.probability < 100 and random.randint(1, 100) > entry.probability:
                    continue
                filtered_rec[eid] = entry
            activated.update(filtered_rec)
            all_newly = filtered_rec

        # Update timed effects
        new_ts = self._update_timed_effects(
            sticky_timers, cooldown_timers, activated, entry_by_id
        )

        # Mutual exclusion groups: keep only one entry per group (weighted random)
        groups: dict[str, list[LorebookEntry]] = {}
        for entry in activated.values():
            if entry.group:
                groups.setdefault(entry.group, []).append(entry)
        for group_name, members in groups.items():
            if len(members) <= 1:
                continue
            weights = [max(m.group_weight, 1) for m in members]
            chosen = random.choices(members, weights=weights, k=1)[0]
            for m in members:
                if m.id != chosen.id:
                    del activated[m.id]

        # Sort by priority descending, limit count
        result = sorted(activated.values(), key=lambda entry: (
            0 if entry.constant else
            1 if entry.id in action_matched_ids and entry.id in filtered_newly else
            2 if entry.id in filtered_newly else 3,
            -entry.priority,
        ))
        # Token budget trimming
        if self.token_budget > 0:
            trimmed = []
            used = 0
            for entry in result:
                if len(trimmed) >= max_entries:
                    break
                cost = self._estimate_tokens(entry.content)
                if used + cost > self.token_budget:
                    continue
                trimmed.append(entry)
                used += cost
            result = trimmed
        else:
            result = result[:max_entries]

        if turn_number > 0:
            for entry in result:
                entry.last_activated_turn = turn_number

        return result, new_ts

    def _scan_text_with_depth(
        self,
        player_action: str,
        recent_messages: list[str],
        already_activated: dict[str, LorebookEntry],
        entries: list[LorebookEntry],
    ) -> dict[str, LorebookEntry]:
        """Scan text against entries, respecting each entry's scan_depth.

        Groups entries by scan_depth to build text_lower only once per depth.
        """
        newly = {}
        action_lower = self._normalize_query_dates(player_action.lower())

        # 按 depth 分组，避免为每个 entry 重复拼接 text_lower
        depth_groups: dict[int, list[LorebookEntry]] = {}
        for entry in entries:
            if entry.id in already_activated or entry.constant or not entry.enabled:
                continue
            depth_groups.setdefault(entry.scan_depth, []).append(entry)

        # 缓存不同 depth 对应的 text_lower
        text_cache: dict[int, str] = {}
        for depth, entries in depth_groups.items():
            if depth not in text_cache:
                if depth <= 0:
                    text_cache[depth] = action_lower
                else:
                    trimmed = recent_messages[-depth:] if depth < len(recent_messages) else recent_messages
                    text_cache[depth] = action_lower + "\n" + self._normalize_query_dates("\n".join(trimmed).lower())

            text_lower = text_cache[depth]
            for entry in entries:
                if entry.id in newly:
                    continue
                pk, sk = self._precomputed[entry.id]
                if not pk or not any(k in text_lower for k in pk):
                    continue
                if sk and not self._check_secondary(sk, text_lower, entry.selective_logic):
                    continue
                newly[entry.id] = entry

        return newly

    def _scan_text(
        self, text: str, already_activated: dict[str, LorebookEntry],
        entries: list[LorebookEntry],
    ) -> dict[str, LorebookEntry]:
        """Scan text against all non-activated entries. Returns newly matched.

        Used for recursive scanning where scan_depth is not applicable.
        """
        newly = {}
        text_lower = self._normalize_query_dates(text.lower())

        for entry in entries:
            if entry.id in already_activated or entry.id in newly or entry.constant or not entry.enabled:
                continue
            pk, sk = self._precomputed[entry.id]
            if not pk or not any(k in text_lower for k in pk):
                continue
            if sk and not self._check_secondary(sk, text_lower, entry.selective_logic):
                continue
            newly[entry.id] = entry

        return newly

    def _activate_related(
        self, activated: dict[str, LorebookEntry], entries: list[LorebookEntry],
    ) -> dict[str, LorebookEntry]:
        """Activate entries referenced by related_entries of already-activated entries."""
        newly = {}
        entry_by_id = {e.id: e for e in entries}
        for entry in activated.values():
            for rel_id in entry.related_entries:
                if rel_id in activated or rel_id in newly:
                    continue
                rel_entry = entry_by_id.get(rel_id)
                if rel_entry and rel_entry.enabled and not rel_entry.constant:
                    newly[rel_id] = rel_entry
        return newly

    def add_entries(self, new_entries: list[dict], replace_existing: bool = False):
        """Add runtime entries, optionally refreshing IDs from a restored state."""
        for data in new_entries:
            entry = self._parse_entry(data)
            index = next((index for index, existing in enumerate(self.entries)
                          if existing.id == entry.id), None)
            if index is not None and not replace_existing:
                continue
            if index is None:
                self.entries.append(entry)
            else:
                self.entries[index] = entry
            pk = self._primary_keys(entry.keys)
            sk = [self._normalize_query_dates(k.lower()) for k in entry.secondary_keys
                  if k.strip() and not self._key_too_short(k)]
            self._precomputed[entry.id] = (pk, sk)

    def update_entry(self, entry_id: str, content: str, keys: list[str] | None = None):
        """Update an existing entry's content (and optionally keys) in-place."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.content = content
                if keys is not None:
                    entry.keys = keys
                    pk = self._primary_keys(keys)
                    self._precomputed[entry_id] = (pk, self._precomputed.get(entry_id, ([], []))[1])
                return

    def update_entry_enabled(self, entry_id: str, enabled: bool):
        """Enable or disable an entry by ID (e.g. from story tree effects)."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.enabled = enabled
                return

    def remove_entry(self, entry_id: str):
        """Remove an entry by ID (e.g. invalidated speculative lore)."""
        self.entries = [e for e in self.entries if e.id != entry_id]
        self._precomputed.pop(entry_id, None)

    def activate_by_context(
        self, location_id: str = "", game_time: str = "",
        already_activated: set | None = None,
        excluded_entry_ids: set[str] | None = None,
    ) -> list[LorebookEntry]:
        """按地点/时间上下文激活条目（不依赖关键词匹配）。

        comment 中含 loc:{location_id} 的条目在该地点自动激活；
        comment 中含 time:{tag} 的条目在 game_time 包含该 tag 时激活。
        """
        extra = []
        already = already_activated or set()
        for entry in self.entries:
            if entry.id in already or entry.id in (excluded_entry_ids or ()) or not entry.enabled:
                continue
            comment = entry.comment or ""
            if location_id and f"loc:{location_id}" in comment:
                extra.append(entry)
                continue
            if game_time and "time:" in comment:
                gt_lower = game_time.lower()
                for part in comment.split():
                    if part.startswith("time:"):
                        tag = part[5:]
                        if tag and tag in gt_lower:
                            extra.append(entry)
                            break
        return extra

    @staticmethod
    def filter_by_visibility(
        entries: list[LorebookEntry],
        scope: str,
        pc_discovered: list[str] | None = None,
        npc_ids: list[str] | None = None,
    ) -> list[LorebookEntry]:
        """按 visibility 过滤条目。

        scope:
          "all"   — 全部返回（Stage 1 骨架用）
          "pc"    — public + world + 已发现的 hidden
          "npc"   — public + world + NPC 在 known_by_npcs 中的 hidden
          "pc_strict" — public + 已发现的 hidden（Stage 5 用，排除 world）
        """
        if scope == "all":
            return list(entries)

        discovered = set(pc_discovered or [])
        npc_set = set(npc_ids or [])
        result = []
        for e in entries:
            vis = e.visibility
            if vis == "public":
                result.append(e)
            elif vis == "world":
                if scope in ("pc", "npc"):
                    result.append(e)
            elif vis == "hidden":
                if scope in ("pc", "pc_strict") and e.id in discovered:
                    result.append(e)
                elif scope == "npc" and npc_set & set(e.known_by_npcs):
                    result.append(e)
        return result

    def format_for_prompt(self, activated: list[LorebookEntry], visibility_markers: bool = False) -> str:
        """Format activated entries into a prompt section, grouped by type."""
        if not activated:
            return ""

        _TYPE_ORDER = {
            "pc_identity": 0, "npc_profile": 1, "npc_relationship": 2,
            "location_desc": 3, "event_context": 4, "story_event": 4,
        }
        _TYPE_HEADERS = {
            "pc_identity": "人物档案",
            "npc_profile": "NPC档案",
            "npc_relationship": "人物关系",
            "location_desc": "地点信息",
            "event_context": "事件背景",
            "story_event": "事件背景",
        }
        _VIS_PREFIX = {
            "world": "[世界设定-仅供推理，不可让角色直接说出] ",
            "hidden_undiscovered": "[机密-未发现：不可让主角知晓此信息] ",
        }
        grouped: dict[str, list[LorebookEntry]] = {}
        for entry in activated:
            key = entry.entry_type if entry.entry_type in _TYPE_ORDER else "_other"
            grouped.setdefault(key, []).append(entry)

        lines = ["## 相关知识 (World Info)"]
        for group_key in sorted(grouped.keys(), key=lambda k: _TYPE_ORDER.get(k, 99)):
            header = _TYPE_HEADERS.get(group_key)
            if header:
                lines.append(f"\n### {header}")
            for entry in grouped[group_key]:
                if not header:
                    lines.append(f"\n### {entry.comment or entry.id}")
                prefix = ""
                if visibility_markers and entry.visibility == "world":
                    prefix = _VIS_PREFIX["world"]
                elif visibility_markers and entry.visibility == "hidden":
                    prefix = _VIS_PREFIX["hidden_undiscovered"]
                lines.append(prefix + entry.content)

        return "\n".join(lines)

    def get_entries_by_position(
        self, activated: list[LorebookEntry], position: str
    ) -> list[LorebookEntry]:
        """Filter activated entries by injection position."""
        return [e for e in activated if e.position == position]

    @staticmethod
    def inject_depth_entries(
        messages: list[dict], depth_entries: list[LorebookEntry],
    ) -> list[dict]:
        """Insert at_depth entries into the messages list at their configured depth."""
        if not depth_entries:
            return messages
        result = list(messages)
        by_depth: dict[int, list[LorebookEntry]] = {}
        for e in depth_entries:
            by_depth.setdefault(e.depth, []).append(e)
        for depth in sorted(by_depth.keys(), reverse=True):
            entries = by_depth[depth]
            content = "\n\n".join(e.content for e in entries)
            role = entries[0].role if entries[0].role in ("system", "user", "assistant") else "system"
            idx = max(0, len(result) - depth)
            result.insert(idx, {"role": role, "content": content})
        return result

    @classmethod
    def _update_timed_effects(
        cls,
        sticky_timers: dict[str, int],
        cooldown_timers: dict[str, int],
        activated: dict[str, LorebookEntry],
        entry_by_id: dict[str, LorebookEntry],
    ) -> dict:
        """Tick down timers and set new ones for freshly activated entries."""
        new_sticky = {}
        new_cooldown = {}
        new_fingerprints = {}

        # Process sticky entries: decrement existing, start new
        for eid, entry in entry_by_id.items():
            if entry.sticky <= 0:
                continue
            if eid in activated:
                # Active this turn — reset/set sticky timer
                new_sticky[eid] = entry.sticky
            elif eid in sticky_timers:
                remaining = sticky_timers[eid] - 1
                if remaining > 0:
                    new_sticky[eid] = remaining
                elif entry.cooldown > 0:
                    # Sticky expired → start cooldown
                    new_cooldown[eid] = entry.cooldown

        # Process cooldown entries: decrement existing
        for eid, remaining in cooldown_timers.items():
            if eid in new_cooldown:
                continue  # Already set from sticky expiry
            remaining -= 1
            if remaining > 0:
                new_cooldown[eid] = remaining

        # Record content fingerprints for activated entries with cooldown
        for eid, entry in activated.items():
            if entry.cooldown > 0:
                new_fingerprints[eid] = cls._content_fingerprint(entry.content or "")

        return {"sticky": new_sticky, "cooldown": new_cooldown, "fingerprints": new_fingerprints}
