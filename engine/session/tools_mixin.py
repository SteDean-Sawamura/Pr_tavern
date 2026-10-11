"""Tools mixin – native tool call execution for GameSession."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import TYPE_CHECKING

from engine.event_scheduler import is_public_event

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)

_TOOL_CALL_RE = re.compile(r'\[TOOL_CALL:\s*(\w+)\(([^)]*)\)\]')

class ToolsMixin:
    """Native tool call execution for game sessions."""

    async def _run_tool_native_async(self, name: str, args: dict) -> str:
        if name == "recall_history" and self.vector_memory is not None:
            return await asyncio.to_thread(self._run_tool_native, name, args)
        return self._run_tool_native(name, args)

    async def _stage1_with_native_tools(self, plot_msgs: list[dict], plot_sys: str,
                                         tools_schema: list[dict], **kwargs) -> tuple[str, list[dict]]:
        """Execute Stage 1 with native API tool calling loop.

        Returns (plot_decision_text, tool_results).
        """
        tool_results = []
        messages = list(plot_msgs)
        max_rounds = 5

        for _ in range(max_rounds):
            resp = await self.ai_provider.generate_with_tools(
                messages, system=plot_sys, tools=tools_schema, **kwargs
            )
            tc_list = resp.get("tool_calls")
            if not tc_list:
                content = resp.get("content", "")
                if tool_results:
                    tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
                    content = content + "\n" + tool_context
                return content, tool_results

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            # Build tool_calls for the assistant message (OpenAI format)
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tc_list
            ]
            messages.append(assistant_msg)

            for tc in tc_list:
                result = await self._run_tool_native_async(tc["name"], tc["arguments"])
                tool_results.append({"tool": tc["name"], "args": tc["arguments"], "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # Max rounds reached — get final content
        resp = await self.ai_provider.generate_with_tools(
            messages, system=plot_sys, **kwargs
        )
        content = resp.get("content", "")
        if tool_results:
            tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
            content = content + "\n" + tool_context
        return content, tool_results

    # ── Stage 4a/5 工具执行器（工具调用模式）──

    _MAX_ATTITUDE_DELTA = 20
    _VALID_ATTITUDE_DIMENSIONS = ("trust", "affection", "fear", "overall")

    def _run_npc_reaction_tool(self, tool_name: str, args: dict) -> str:
        """Validate an NPC reaction tool call. Does not mutate state; merging is separate.

        Returns a short result string fed back to the model.
        """
        if tool_name == "register_npc":
            if not isinstance(args, dict) or not args.get("id") or not args.get("name"):
                return "错误: NPC缺少ID或姓名，已忽略"
            self._new_npc_tool_calls.append(args.copy())
            return f"已记录新NPC {args['name']}"
        if tool_name != "update_npc_attitude":
            return f"未知NPC工具: {tool_name}"

        npc_id = args.get("npc_id", "")
        if not npc_id:
            return "错误: npc_id 为空，已忽略"
        known = set(self._npc_by_id.keys()) | set(self.current_state.get("npcs", {}).keys())
        if npc_id not in known:
            return f"错误: 未知NPC ID '{npc_id}'，已忽略"

        dimension = args.get("dimension", "")
        if dimension not in self._VALID_ATTITUDE_DIMENSIONS:
            return f"错误: 无效维度 '{dimension}'，已忽略"

        change = args.get("change", 0)
        if not isinstance(change, int):
            return f"错误: change 必须为整数，已忽略"
        clamped = max(-self._MAX_ATTITUDE_DELTA, min(self._MAX_ATTITUDE_DELTA, change))
        note = "" if clamped == change else f"（原值 {change} 已钳制到 {clamped}）"

        self._npc_reaction_tool_calls.append({
            "npc_id": npc_id, "dimension": dimension,
            "change": clamped, "reason": str(args.get("reason", "")),
        })
        return f"已记录 {npc_id} 的 {dimension} 变化: {clamped:+d}{note}"

    def _run_choices_tool(self, tool_name: str, args: dict) -> str:
        """Validate an add_choice tool call. Does not mutate state; merging is separate."""
        if tool_name != "add_choice":
            return f"未知选项工具: {tool_name}"

        text = str(args.get("text", "")).strip()
        if not text:
            return "错误: text 为空，已忽略"

        risk = args.get("risk", "")
        if risk and risk not in ("safe", "moderate", "risky"):
            risk = "moderate"

        choice = {"id": str(args.get("id", "")).strip() or f"c{len(self._choices_tool_calls) + 1}", "text": text}
        if args.get("hint"):
            choice["hint"] = str(args["hint"])
        if args.get("time_hint"):
            choice["time_hint"] = str(args["time_hint"])
        if risk:
            choice["risk"] = risk

        self._choices_tool_calls.append(choice)
        return f"已添加选项 {choice['id']}: {text[:40]}"

    def _reset_stage45_tool_buffers(self):
        """Clear per-turn buffers used by Stage 4a/5 tool mode."""
        self._npc_reaction_tool_calls = []
        self._new_npc_tool_calls = []
        self._choices_tool_calls = []

    async def _run_stage45_tools(self, msgs: list[dict], sys_prompt: str,
                                 tools_schema: list[dict], *, max_tokens: int,
                                 stage_key: str, label: str) -> dict:
        """Single-round Stage 4a/5 call; retain text for models that skip tools.

        Tool calls take priority; the loop does not feed results back.
        """
        async def _call():
            resp = await self.ai_provider.generate_with_tools(
                msgs, system=sys_prompt or None, tools=tools_schema,
                max_tokens=max_tokens, **self._stage_kwargs(stage_key),
            )
            return {"tool_calls": resp.get("tool_calls") or [], "content": resp.get("content") or ""}
        from engine.session.pipeline_mixin import _retry_on_failure
        return await _retry_on_failure(_call, max_retries=1, label=label, raise_on_failure=True)

    def _is_narrated_new_npc(self, npc: dict, narrative: str) -> bool:
        if not isinstance(npc, dict):
            return False
        npc_id, name = npc.get("id"), npc.get("name")
        if not isinstance(npc_id, str) or not isinstance(name, str) or not npc_id or not name:
            return False
        player = self.current_state.get("player", {})
        if name not in narrative or npc_id == player.get("id") or name == player.get("name"):
            return False
        known = list(self._npc_by_id.values()) + list(self.current_state.get("npcs", {}).values())
        return npc_id not in self._npc_by_id and npc_id not in self.current_state.get("npcs", {}) and not any(
            isinstance(entry, dict) and entry.get("name") == name for entry in known
        )

    def _merge_npc_reaction_tool_results(self, parsed: dict, narrative: str = "") -> None:
        """Merge buffered NPC reactions and narrated introductions into parsed."""
        new_npcs = parsed.setdefault("new_npcs", [])
        seen_ids = {npc.get("id") for npc in new_npcs if isinstance(npc, dict)}
        seen_names = {npc.get("name") for npc in new_npcs if isinstance(npc, dict)}
        for npc in self._new_npc_tool_calls:
            if (self._is_narrated_new_npc(npc, narrative)
                    and npc["id"] not in seen_ids and npc["name"] not in seen_names):
                new_npcs.append(npc)
                seen_ids.add(npc["id"])
                seen_names.add(npc["name"])
        if not self._npc_reaction_tool_calls:
            return
        changes = parsed.setdefault("npc_attitude_changes", [])
        seen = {(c.get("npc_id"), c.get("dimension")) for c in changes}
        for call in self._npc_reaction_tool_calls:
            key = (call["npc_id"], call["dimension"])
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "npc_id": call["npc_id"],
                "change": call["change"],
                "reason": call.get("reason", ""),
            }
            # "overall" 不属于三维关系维度，交由下游按默认维度处理
            if call["dimension"] != "overall":
                entry["dimension"] = call["dimension"]
            changes.append(entry)

    def _merge_choices_tool_results(self, parsed: dict) -> None:
        """Merge buffered add_choice calls into parsed["choices"]."""
        if not self._choices_tool_calls:
            return
        by_id = {}
        for c in self._choices_tool_calls:
            by_id[c["id"]] = c
        parsed["choices"] = list(by_id.values())

    def _execute_tool_calls(self, text: str) -> tuple[str, list[dict]]:
        """Parse and execute [TOOL_CALL: ...] patterns in AI output.

        Returns (cleaned_text, tool_results).
        """
        results = []
        for m in _TOOL_CALL_RE.finditer(text):
            name = m.group(1)
            args = [a.strip() for a in m.group(2).split(",") if a.strip()]
            result = self._run_tool(name, args)
            if result is not None:
                results.append({"tool": name, "args": args, "result": result})
        cleaned = _TOOL_CALL_RE.sub("", text).strip()
        return cleaned, results

    def _run_tool(self, name: str, args: list[str]) -> str | None:
        if name == "roll_dice" and args:
            skill = args[0]
            dc = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
            roll = random.randint(1, 20)
            bonus = self._get_skill_bonus(skill)
            total = roll + bonus
            success = total >= dc
            return f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"
        if name == "check_inventory" and args:
            item = args[0]
            inv = self.current_state.get("inventory", [])
            found = any(item in (i.get("item", i.get("name", "")) if isinstance(i, dict) else str(i)) for i in inv)
            return f"{'持有' if found else '未持有'}{item}"
        if name == "get_npc_attitude" and args:
            npc_id = args[0]
            npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
            att = npc_data.get("attitude_toward_player", "未知") if isinstance(npc_data, dict) else "未知"
            return f"{npc_id}的态度: {att}"
        if name == "get_time":
            return self.current_state.get("game_time", "未知")
        return None

    def _run_tool_native(self, name: str, args: dict) -> str:
        """Execute a tool call from native API tool_calls (dict arguments)."""
        if name == "roll_dice":
            skill = args.get("skill", "通用")
            dc = args.get("dc", 10)
            roll = random.randint(1, 20)
            bonus = self._get_skill_bonus(skill)
            total = roll + bonus
            success = total >= dc
            return f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"
        if name == "check_inventory":
            item = args.get("item_name", "")
            inv = self.current_state.get("inventory", [])
            found = any(item in (i.get("item", i.get("name", "")) if isinstance(i, dict) else str(i)) for i in inv)
            return f"{'持有' if found else '未持有'}{item}"
        if name == "get_npc_attitude":
            npc_id = args.get("npc_id", "")
            npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
            att = npc_data.get("attitude_toward_player", "未知") if isinstance(npc_data, dict) else "未知"
            return f"{npc_id}的态度: {att}"
        if name == "get_time":
            return self.current_state.get("game_time", "未知")
        if name == "modify_stat":
            stat = args.get("stat", "")
            delta = args.get("delta", 0)
            player = self.current_state.get("player", {})
            attrs = player.get("attributes", {})
            if stat in attrs and isinstance(attrs[stat], dict):
                old = attrs[stat].get("value", 0)
                attrs[stat]["value"] = old + delta
                return f"{stat}: {old} → {old + delta}"
            old = player.get(stat, 0)
            if isinstance(old, (int, float)):
                player[stat] = old + delta
                return f"{stat}: {old} → {old + delta}"
            return f"未找到属性: {stat}"
        if name == "query_lore":
            keyword = args.get("keyword", "")
            kw_lower = keyword.lower()
            excluded_ids = self.prompt_builder._excluded_lore_ids(self.current_state)
            matches = [
                e for e in self.prompt_builder.lorebook.entries
                if e.enabled and e.id not in excluded_ids and (
                    kw_lower in (e.comment or "").lower()
                    or kw_lower in e.content[:200].lower()
                    or any(kw_lower in k.lower() for k in e.keys)
                )
            ][:3]
            if matches:
                return "\n".join(f"- {e.comment or e.id}: {e.content[:200]}" for e in matches)
            return f"未找到与'{keyword}'相关的知识条目"
        if name == "change_location":
            loc_id = args.get("location_id", "")
            if loc_id in self._location_by_id:
                loc = self._location_by_id[loc_id]
                self.current_state.setdefault("player", {})["location"] = loc_id
                return f"已移动到: {loc.get('name', loc_id)}"
            return f"未知地点: {loc_id}"
        if name == "set_variable":
            var_name = args.get("name", "")
            var_value = args.get("value", "")
            if hasattr(self, 'script_variables'):
                self.script_variables.set(var_name, var_value)
                return f"变量 {var_name} = {var_value}"
            return f"变量系统不可用"
        # --- P1 上下文 Pull 工具 ---
        if name == "recall_history":
            query = args.get("query", "").strip()
            if not query:
                return json.dumps({"results": []}, ensure_ascii=False)
            max_results = args.get("max_results", 5)
            results = []
            _SCORE_THRESHOLD = 0.3
            _SUMMARY_LIMIT = 200
            _TOTAL_CHAR_LIMIT = 800
            logger.info("recall_history: query=%r, vector_memory=%s, world_tree=%s, _narrative_buffer=%d",
                        query[:50],
                        'yes' if self.vector_memory is not None else 'no',
                        'yes' if self.world_tree is not None else 'no',
                        len(getattr(self, '_narrative_buffer', [])))
            # 优先使用向量记忆检索
            if self.vector_memory is not None:
                try:
                    branch_node_ids = {node["id"] for node in self.world_tree.get_active_branch()}
                    hits = self.vector_memory.query(
                        query, max_results, None, "turn", branch_node_ids,
                    )
                    for h in hits:
                        score = h.get("score", 0)
                        if score < _SCORE_THRESHOLD:
                            continue
                        results.append({
                            "turn": h.get("turn", "?"),
                            "summary": h.get("text", "")[:_SUMMARY_LIMIT],
                            "relevance": round(score, 2),
                        })
                except Exception as exc:
                    logger.warning("recall_history 向量检索失败: %s", exc)
            if results:
                logger.info("recall_history: vector_memory 返回 %d 条", len(results))
            # 回退到 world_tree 节点文本搜索
            query_lower = query.lower()
            exact_results = []
            if self.world_tree is not None and (
                not results or not any(query_lower in result["summary"].lower() for result in results)
            ):
                for node in reversed(self.world_tree.get_active_branch()):
                    action = node.get("player_action")
                    action_text = action.get("text", "") if isinstance(action, dict) else str(action or "")
                    narrative = node.get("ai_response", "")
                    node_text = (f"[玩家行动] {action_text}\n" if action_text else "") + narrative
                    match_at = node_text.lower().find(query_lower)
                    if match_at >= 0:
                        excerpt_start = max(0, match_at - 60)
                        exact_results.append({
                            "turn": node.get("turn_number", "?"),
                            "summary": node_text[excerpt_start:excerpt_start + _SUMMARY_LIMIT],
                            "relevance": 0.5,
                        })
                    if len(exact_results) >= max_results:
                        break
            if exact_results:
                exact_turns = {result["turn"] for result in exact_results}
                results = (exact_results + [result for result in results
                                            if result["turn"] not in exact_turns])[:max_results]
                logger.info("recall_history: world_tree 返回 %d 条", len(exact_results))
            # Bug 5 fix: 最终 fallback — 搜索 adventure_log
            if not results:
                adventure_log = self.current_state.get("adventure_log", [])
                if isinstance(adventure_log, list):
                    private_descriptions = {
                        event.get("description") for section in ("cyclic_events", "one_time_events")
                        for event in self.script.get(section, [])
                        if not is_public_event(self.script, event.get("id", "")) and event.get("description")
                    }
                    query_lower_al = query.lower()
                    for entry in reversed(adventure_log[-20:]):
                        if not isinstance(entry, dict):
                            continue
                        events = entry.get("events", [])
                        for ev in events:
                            ev_text = ev.get("text", "") if isinstance(ev, dict) else str(ev)
                            if isinstance(ev, dict) and ev.get("type") == "event" and (
                                (ev.get("event_id") and not is_public_event(self.script, ev["event_id"]))
                                or ev_text in private_descriptions
                            ):
                                continue
                            if isinstance(ev, dict) and ev.get("type") == "chapter_summary" and any(
                                description in ev_text for description in private_descriptions
                            ):
                                continue
                            if query_lower_al in ev_text.lower():
                                results.append({
                                    "turn": entry.get("turn", "?"),
                                    "summary": ev_text[:_SUMMARY_LIMIT],
                                    "relevance": 0.3,
                                })
                                break
                        if len(results) >= max_results:
                            break
            # 最终 fallback — 搜索 _narrative_buffer（agentic 模式同轮缓存）
            if not results and hasattr(self, '_narrative_buffer'):
                query_lower_nb = query.lower()
                for buf in reversed(self._narrative_buffer):
                    if query_lower_nb in buf.get("text", "").lower():
                        results.append({
                            "turn": buf.get("turn", "?"),
                            "summary": buf["text"][:_SUMMARY_LIMIT],
                            "relevance": 0.4,
                        })
                    if len(results) >= max_results:
                        break
            logger.info("recall_history: 最终结果 %d 条 (fallback路径: vm=%s wt=%s al=%s nb=%s)",
                        len(results),
                        'yes' if self.vector_memory is not None else 'no',
                        'yes' if self.world_tree is not None else 'no',
                        'yes' if self.current_state.get("adventure_log") else 'no',
                        'yes' if getattr(self, '_narrative_buffer', []) else 'no')
            # 总字符数截断
            truncated = []
            total_chars = 0
            for r in results:
                s = r["summary"]
                if total_chars + len(s) > _TOTAL_CHAR_LIMIT:
                    remaining = _TOTAL_CHAR_LIMIT - total_chars
                    if remaining > 50:
                        r["summary"] = s[:remaining]
                        truncated.append(r)
                    break
                total_chars += len(s)
                truncated.append(r)
            return json.dumps({"results": truncated}, ensure_ascii=False)
        if name == "query_lorebook":
            lb = getattr(self.prompt_builder, "lorebook", None)
            if lb is None:
                return json.dumps({"entries": [], "total_matches": 0, "next_page": None})
            result = lb.query(
                keyword=args.get("keyword", ""), entry_id=args.get("entry_id", ""),
                page=args.get("page", 0), offset=args.get("offset", 0),
                excluded_ids=self.prompt_builder._excluded_lore_ids(self.current_state),
                entry_ids=args.get("entry_ids"),
                snapshot=args.get("snapshot"),
                snapshot_scope=(self.world_tree.tree_id, self.world_tree.active_node_id,
                                self.current_state.get("game_time"), "committed"),
            )
            return json.dumps(result, ensure_ascii=False)
        if name == "query_npc_history":
            npc_name = args.get("npc_name", "").strip()
            max_turns = args.get("max_turns", 5)
            interactions = []
            if self.world_tree is not None and npc_name:
                npc_lower = npc_name.lower()
                for node in reversed(self.world_tree.get_active_branch()):
                    narrative = node.get("ai_response") or ""
                    action_raw = node.get("player_action")
                    action_text = (
                        action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")
                    )
                    node_text = (f"[玩家行动] {action_text}\n" if action_text else "") + narrative
                    match_at = node_text.lower().find(npc_lower)
                    if match_at >= 0:
                        excerpt_start = max(0, match_at - 60)
                        interactions.append({
                            "turn": node.get("turn_number", "?"),
                            "summary": node_text[excerpt_start:excerpt_start + 200],
                        })
                    if len(interactions) >= max_turns:
                        break
            return json.dumps({"npc": npc_name, "interactions": interactions}, ensure_ascii=False)
        return f"未知工具: {name}"
