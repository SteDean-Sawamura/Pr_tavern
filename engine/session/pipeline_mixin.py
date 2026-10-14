"""Pipeline mixin -- core execution pipeline and narrative review for GameSession."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from typing import TYPE_CHECKING

from ai.base import stream_split_think, strip_think_tags, _THINK_EXTRACT_RE
from engine.lorebook import Lorebook
from engine.session.agentic_mixin import PendingConsequenceDisclosureError
from engine.tool_schemas import (
    GAME_TOOLS, GAME_TOOLS_SCHEMA, NARRATIVE_TOOLS_SCHEMA,
    NPC_REACTION_TOOLS, CHOICES_TOOLS,
)

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)


async def _retry_on_failure(coro_factory, max_retries=1, label="", raise_on_failure=False):
    """重试异步调用。coro_factory 是返回协程的无参函数。"""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            result = await coro_factory()
            if attempt > 0:
                logger.info('[重试] %s 第%d次重试成功', label, attempt)
            return result
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning('[重试] %s 失败 (%s)，重试中...', label, e)
            else:
                logger.warning('[失败] %s 重试%d次后仍失败: %s', label, max_retries, e)
    if raise_on_failure and last_error is not None:
        raise last_error
    return None


def _extract_reasoning(raw: str) -> str:
    """Extract content inside <think>...</think> tags. Returns empty string if none."""
    if not raw:
        return ""
    m = _THINK_EXTRACT_RE.search(raw)
    return m.group(1).strip() if m else ""


class PipelineMixin:
    """Core pipeline: multi-stage execution, NPC dialogue extraction, narrative review."""

    def _extract_npc_dialogues(
        self, narrative: str, present_npcs: list[str]
    ) -> dict[str, list[str]]:
        """从叙事中提取归属到各NPC的对话文本。"""
        npc_states = self.current_state.get("npcs", {})
        dn = self.current_state.get("display_names", {})
        name_to_id = {}
        for npc_id in present_npcs:
            ns = npc_states.get(npc_id, {})
            name = ns.get("name", dn.get(npc_id, npc_id))
            name_to_id[name] = npc_id

        result: dict[str, list[str]] = {npc_id: [] for npc_id in present_npcs}

        # 匹配模式: NPC名 + 说/道/笑道 等 + "对话"  或  "对话" 前一行提到NPC名
        for name, npc_id in name_to_id.items():
            pattern = re.compile(
                rf'{re.escape(name)}[^"“\n]{{0,20}}["“]([^"”]+)["”]'
            )
            for m in pattern.finditer(narrative):
                result[npc_id].append(m.group(1))

        return result

    def _check_npc_voice_consistency(
        self, narrative: str, present_npcs: list[str]
    ) -> bool:
        """返回True=通过，False=NPC对话风格雷同需修正。"""
        if len(present_npcs) < 2:
            return True

        npc_dialogues = self._extract_npc_dialogues(narrative, present_npcs)
        # 只分析有对话的NPC
        active = {k: v for k, v in npc_dialogues.items() if v}
        if len(active) < 2:
            return True

        features = {}
        for npc_id, texts in active.items():
            combined = "".join(texts)
            if not combined:
                continue
            clen = len(combined)
            features[npc_id] = {
                "avg_len": sum(len(t) for t in texts) / len(texts),
                "excl": combined.count("！") / clen,
                "ques": combined.count("？") / clen,
                "ellip": combined.count("…") / clen,
                "modal": sum(combined.count(p) for p in self._MODAL_PARTICLES) / clen,
            }

        npcs = list(features.keys())
        for i in range(len(npcs)):
            for j in range(i + 1, len(npcs)):
                fi, fj = features[npcs[i]], features[npcs[j]]
                if (
                    abs(fi["avg_len"] - fj["avg_len"]) < 3
                    and abs(fi["excl"] - fj["excl"]) + abs(fi["ques"] - fj["ques"]) < 0.02
                    and abs(fi["modal"] - fj["modal"]) < 0.02
                ):
                    return False
        return True

    async def _maybe_fix_npc_voices(self, narrative: str, state: dict, present_npc_ids: list[str] | None = None) -> str:
        """如果NPC对话风格雷同，用角色行为器重写对话部分。"""
        if not present_npc_ids:
            return narrative
        # Extract dialogue for lorebook regardless of consistency check
        self._accumulate_npc_dialogue_style(narrative, present_npc_ids)
        if self._check_npc_voice_consistency(narrative, present_npc_ids):
            return narrative

        logger.info("NPC声音校验未通过，触发对话修正")
        # 用角色行为器的prompt生成修正后的对话
        voice_table = self.prompt_builder._build_npc_voice_table(state, present_npc_ids or [])
        system = (
            "你是NPC对话修正师。下方叙事中的NPC对话风格过于雷同，请根据声纹速查表重写对话部分。\n\n"
            "规则：\n"
            "- 只修改引号内的对话内容和说话动作描写\n"
            "- 不要修改环境描写和剧情事实\n"
            "- 每个NPC的说话方式必须明显不同\n"
            "- 保持对话的语义不变，只改风格\n"
            "- 输出完整的修正后叙事"
        )
        content = f"原始叙事:\n{narrative}\n\n{voice_table}"
        messages = [{"role": "user", "content": content}]

        try:
            raw = await self.ai_provider.generate(messages, system=system, max_tokens=8192, **self._stage_kwargs("narrative"))
            fixed = strip_think_tags(raw).strip()
            if len(fixed) > len(narrative) * 0.5:
                return fixed
        except Exception as e:
            logger.warning("NPC声音修正调用失败: %s", e)

        return narrative

    def _narrative_review_evidence(self, ctx: dict) -> tuple[list[dict], str]:
        active_script = ctx.get("_review_active_script")
        if active_script is None:
            active_script = [
                {"name": brief["name"], "description": brief["description"]}
                for brief in self._script_anchor_briefs()
                if brief["status"] == "active" and brief["description"]
            ]
        if not active_script:
            return [], ""
        recent = ctx.get("recent_nodes") or []
        known = " ".join(
            str(node.get("ai_response") or node.get("narrative") or "")[-500:]
            for node in recent[-3:] if isinstance(node, dict)
        )[-1500:]
        return active_script, known

    async def _review_narrative(
        self, narrative: str, action_text: str, ctx: dict,
        *, state: dict | None = None,
    ) -> dict | None:
        """Stage 3.5: AI 叙事质量评审。返回 {pass, violations} 或 None。"""
        if not self.ai_provider or not narrative:
            return None
        _st = state if state is not None else self.current_state
        npc_states = _st.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        present_npcs = []
        for nid in (ctx.get("present_npc_ids") or [])[:6]:
            ns = npc_states.get(nid, {})
            nd = npc_defs.get(nid, {})
            if not isinstance(ns, dict):
                continue
            present_npcs.append({
                "id": nid,
                "name": ns.get("name", nid),
                "title": ns.get("title") or nd.get("title") or nd.get("role") or nd.get("occupation", ""),
            })
        private_script, known_context = self._narrative_review_evidence(ctx)
        msgs, sys_prompt = self.prompt_builder.build_narrative_review_prompt(
            narrative, action_text, present_npcs,
            pc_name=_st.get("player", {}).get("name", ""),
            private_script=private_script, known_context=known_context,
            plan_reply_context=ctx.get("_review_plan_reply"),
            turn_player_inputs=ctx.get("_turn_player_inputs"),
        )
        for max_tokens in (getattr(self.ai_provider, "review_max_tokens", 500),
                           getattr(self.ai_provider, "review_retry_max_tokens", 1500)):
            try:
                ctx["_semantic_review_model_calls"] = ctx.get("_semantic_review_model_calls", 0) + 1
                raw = await self.ai_provider.generate(
                    msgs, system=sys_prompt, max_tokens=max_tokens,
                    **(self._stage_kwargs("review") or self._stage_kwargs("state")),
                )
                raw = strip_think_tags(raw or "")
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if not match:
                    logger.warning("叙事质量评审未返回 JSON（响应长度=%d，预算=%d）", len(raw), max_tokens)
                    continue
                result = json.loads(match.group())
                if isinstance(result, dict) and type(result.get("pass")) is bool:
                    return result
                logger.warning("叙事质量评审返回无效的 pass 字段（预算=%d）", max_tokens)
            except json.JSONDecodeError:
                logger.warning("叙事质量评审返回无效 JSON（预算=%d）", max_tokens)
            except Exception as e:
                logger.warning("叙事质量评审失败: %s", e)
                break
        return None

    @staticmethod
    def _trim_plot_sections(plot_decision: str, known_objects: set[str] | None = None) -> str:
        """Parse Stage 1 output into sections and enforce per-section item limits.

        Handles repeated section headers (AI sometimes outputs [关键事件] multiple
        times instead of a list) by merging them, then deduplicates and caps.
        """
        if not plot_decision:
            return plot_decision

        # Section header regex: [SectionName]
        section_re = re.compile(r'^\[(.+?)\]\s*', re.MULTILINE)
        sections: dict[str, list[str]] = {}
        order: list[str] = []
        last_key = None
        last_start = 0

        for m in section_re.finditer(plot_decision):
            if last_key is not None:
                chunk = plot_decision[last_start:m.start()].strip()
                if chunk:
                    sections.setdefault(last_key, []).append(chunk)
                if last_key not in order:
                    order.append(last_key)
            last_key = m.group(1)
            last_start = m.end()
        if last_key is not None:
            chunk = plot_decision[last_start:].strip()
            if chunk:
                sections.setdefault(last_key, []).append(chunk)
            if last_key not in order:
                order.append(last_key)

        if not sections:
            return plot_decision

        # Per-section item limits
        limits = {"关键事件": 3, "世界脉搏": 2, "NPC决策": 3, "场景约束": 3}
        item_split_re = re.compile(r'[;；\n]+')

        assembled: dict[str, str] = {}
        for key in order:
            chunks = sections.get(key, [])
            raw_text = "\n".join(chunks)
            items = [s.strip() for s in item_split_re.split(raw_text) if s.strip()]
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for item in items:
                if item not in seen:
                    seen.add(item)
                    unique.append(item)
            limit = limits.get(key)
            if limit and len(unique) > limit:
                unique = unique[:limit]
            assembled[key] = "；".join(unique) if len(unique) > 1 else (unique[0] if unique else "")

        # Validate [场景约束]: filter out items mentioning unknown objects
        if known_objects and "场景约束" in assembled:
            scene_text = assembled["场景约束"]
            obj_match = re.search(r'物件\s*[=＝]\s*([^;；\n]+)', scene_text)
            if obj_match:
                raw_objects = [o.strip() for o in obj_match.group(1).split(",") if o.strip()]
                # Keep only objects that match known sources (substring match)
                valid = [o for o in raw_objects if any(k in o or o in k for k in known_objects)]
                if len(valid) < len(raw_objects):
                    if valid:
                        new_obj_str = ", ".join(valid)
                    else:
                        new_obj_str = "无"
                    assembled["场景约束"] = scene_text[:obj_match.start(1)] + new_obj_str + scene_text[obj_match.end(1):]

        # Reassemble
        parts = []
        for key in order:
            if key in assembled and assembled[key]:
                parts.append(f"[{key}] {assembled[key]}")
        return "\n".join(parts)

    def _format_stage_directives(self, ctx: dict, stage: str, *, state: dict) -> str:
        directives = ctx.get("stage_directives", {}).get(stage) or []
        return self.script_variables.expand_macros("\n".join(directives), state)

    async def _execute_pipeline(
        self, ctx: dict, route: dict, player_action: dict, *,
        streaming: bool = False,
        state_baseline: dict | None = None,
    ):
        """Generate narrative, state and NPC candidates; defer choices until settlement.

        state_baseline: if provided (regenerate), use this instead of self.current_state
                        for prompt building. None means use self.current_state.
        """
        _state = state_baseline if state_baseline is not None else self.current_state
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        _event_sections = ctx.get("event_sections") or {}
        _guard_narrative = bool(_state.get("pending_consequences") or
                                (isinstance(_event_sections, dict) and _event_sections.get("consequences")))

        # --- Shared pre-Stage-1 setup ---
        _recent_for_tail = ctx.get("recent_nodes", [])
        _recent_narratives = []
        _prev_tail = ""
        if _recent_for_tail:
            _rn = _recent_for_tail[-1]
            _rn_action = _rn.get("player_action")
            _rn_action_text = ""
            if _rn_action:
                _rn_action_text = _rn_action.get("text", "") if isinstance(_rn_action, dict) else str(_rn_action)
            _rn_narrative = _rn.get("ai_response", "")
            if _rn_narrative:
                _recent_narratives.append({
                    "turn": _rn.get("turn_number", "?"),
                    "action": _rn_action_text,
                    "narrative": _rn_narrative,
                })
                _prev_tail = _rn_narrative[-500:] if len(_rn_narrative) > 500 else _rn_narrative

        _prev_plot = ctx.get("prev_plot_decision", "")

        _tools = GAME_TOOLS if self.script.get("settings", {}).get("ai_tools_enabled") else None
        _use_native_tools = _tools and hasattr(self.ai_provider, 'generate_with_tools')
        _focus_npcs = route.get("focus_npcs") or ctx.get("present_npc_ids")
        _sd_plot = self._format_stage_directives(ctx, "plot", state=_state)
        _plot_hctx = "## 当前剧情线指令\n" + _sd_plot if _sd_plot else ""
        _pacing = _state.get("pacing_state", {})
        _pacing_tension = _pacing.get("tension", 50)
        _pacing_rec = _pacing.get("recommendation", "")
        if _pacing_tension < 40:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "低紧张期：[关键事件]以日常因果为主。[世界脉搏]最多一条暗示性观察（看到/听到），"
                "不引发即时冲突，不揭示结论。整体氛围应是'日常中偶有不对劲'而非'步步惊心'。"
            )
        elif _pacing_tension < 70:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "中紧张期：可以有一个信息推进或异常发现，但必须与已有线索/NPC关联。"
                "不要同时堆叠多个新悬疑元素。"
            )
        else:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "高紧张期：可以有明确冲突、多条信息同时涌来、或NPC态度急转。"
            )
        if _pacing_rec:
            _pacing_hint += f"\n{_pacing_rec}"
        _plot_hctx = (_plot_hctx + "\n\n" + _pacing_hint).strip() if _plot_hctx else _pacing_hint
        _cfb = _state.get("_compose_feedback", "")
        if _cfb:
            _plot_hctx = (_plot_hctx + f"\n\n## 上轮叙事问题（本轮骨架需规避）\n{_cfb}").strip()
        _dissolves = _state.get("_pulse_dissolves")
        if _dissolves:
            _dissolve_text = "；".join(d if isinstance(d, str) else d.get("text", "") for d in _dissolves[:3])
            _plot_hctx = (_plot_hctx + f"\n\n## 可消解的旧暗示（非强制，有自然机会时收束）\n"
                          f"以下前几轮的世界脉搏暗示未转化为正式事件，如果本轮场景有合理契机，"
                          f"可以在[世界脉搏]中一笔带过给出日常解释（如'原来只是例行巡逻'）。"
                          f"如无契机则忽略，不要强行插入：\n"
                          f"{_dissolve_text}").strip()
        _lore_summary = self._build_lore_summary_for_plot(ctx.get("activated_lore", []))
        if _lore_summary:
            _plot_hctx = (_plot_hctx + f"\n\n{_lore_summary}").strip() if _plot_hctx else _lore_summary
        _profile_hint = self._build_player_profile_hint(_state)
        if _profile_hint:
            _plot_hctx = (_plot_hctx + f"\n\n{_profile_hint}").strip() if _plot_hctx else _profile_hint

        if route.get("scene_type") == "opening":
            _opening_base = self.script_variables.expand_macros(ctx.get("base_history_context", ""), _state)
            if _opening_base:
                _plot_hctx = (_opening_base + "\n\n" + _plot_hctx).strip() if _plot_hctx else _opening_base
        elif ctx.get("turn_instructions"):
            _turn_instructions = self.script_variables.expand_macros("\n".join(ctx["turn_instructions"]), _state)
            _plot_hctx = (f"## 本轮声明指令\n{_turn_instructions}\n\n" + _plot_hctx).strip()

        # Feature #1: NPC scene hijack directive
        if route.get("scene_type") == "npc_hijack":
            _hijack = _state.get("_scene_hijack") or {}
            _hijack_hint = (
                f"## 场景劫持\n本回合由NPC主导场景。{_hijack.get('npc_name', '')}因「{_hijack.get('reason', '')}」"
                f"打断玩家行动。玩家的行动被中断，叙事焦点转移到NPC的主动行为上。\n"
                f"建议场景: {_hijack.get('suggested_action', '')}"
            )
            _plot_hctx = (_hijack_hint + "\n\n" + _plot_hctx).strip()

        # Feature #3: Plan decomposition directive
        if route.get("has_plan_declaration"):
            _plot_hctx = (
                _plot_hctx + "\n\n## 计划分解\n"
                "玩家声明了一个多步骤计划。在[行动结果]之后额外输出:\n"
                "plan_steps: [\"步骤1\", \"步骤2\", ...]\n"
                "plan_risk: \"low|medium|high\"\n"
                "将计划分解为3-5个具体可执行步骤，评估整体风险。"
            ).strip()

        # Feature #4: Active war context
        faction_wars = _state.get("faction_wars", [])
        player_loc = _state.get("player", {}).get("location", "")
        for war in faction_wars:
            if war.get("status") in ("skirmish", "open_war"):
                territories = war.get("territories", {})
                if player_loc in territories:
                    controller = territories[player_loc]
                    _war_hint = (
                        f"## 战争氛围\n玩家所在地被{self._get_org_name(controller)}控制，"
                        f"{'全面战争' if war['status'] == 'open_war' else '武装冲突'}进行中。"
                        f"天平偏向: {'进攻方' if war.get('balance', 0) > 0 else '防守方'}({abs(war.get('balance', 0))}%)"
                    )
                    _plot_hctx = (_plot_hctx + "\n\n" + _war_hint).strip()
                    break

        # --- Stage 1: 剧情决策 ---
        plot_msgs, plot_sys = self.prompt_builder.build_plot_decision_prompt(
            action_text,
            _state,
            check_result=ctx.get("check_result"),
            dice_results=ctx.get("dice_dicts"),
            triggered_events=ctx.get("triggered_events"),
            triggered_consequences=ctx.get("triggered_consequences"),
            achieved_milestones=ctx.get("achieved_milestones"),
            present_npc_ids=_focus_npcs,
            history_context=_plot_hctx,
            recent_reasoning=None,
            game_tools=None if _use_native_tools else _tools,
            prev_narrative_tail=_prev_tail,
            prev_plot_decision=_prev_plot,
            recent_narratives=_recent_narratives or None,
            event_sections=ctx.get("event_sections"),
            recall_hints=route.get("recall_hints") or None,
        )

        if _use_native_tools:
            _stage1_result = await _retry_on_failure(
                lambda: self._stage1_with_native_tools(
                    plot_msgs, plot_sys, GAME_TOOLS_SCHEMA,
                    max_tokens=8192, **self._stage_kwargs("narrative")
                ),
                max_retries=1, label="Stage1工具调用",
            )
            if _stage1_result is not None:
                plot_decision, tool_results = _stage1_result
            else:
                plot_decision, tool_results = "", []
            plot_reasoning = ""
        else:
            raw_plot = await self.ai_provider.generate(
                plot_msgs, system=plot_sys, max_tokens=8192, **self._stage_kwargs("narrative")
            )
            plot_reasoning = _extract_reasoning(raw_plot)
            plot_decision = strip_think_tags(raw_plot)
            plot_decision, tool_results = self._execute_tool_calls(plot_decision)
            if tool_results:
                tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
                plot_decision = plot_decision + "\n" + tool_context

        if plot_decision and plot_decision.lstrip().startswith(("```json", "```\n[", "[{")):
            _marker = "[行动结果]"
            _pos = plot_decision.find(_marker)
            if _pos > 0:
                logger.info("Stage 1 输出包含前缀 JSON，已截取骨架部分")
                plot_decision = plot_decision[_pos:]

        ctx["plot_reasoning"] = plot_reasoning
        # Build known objects set for scene constraint validation
        _known_objects = set()
        for it in _state.get("inventory", []):
            _item_name = it.get("item", "")
            if _item_name:
                _known_objects.add(_item_name)
        _player_loc = _state.get("player", {}).get("location", "")
        if _player_loc:
            _loc_def = self.prompt_builder._location_by_id.get(_player_loc, {})
            _loc_desc = _loc_def.get("description", "")
            if _loc_desc:
                _known_objects.add(_loc_desc)
        plot_decision = self._trim_plot_sections(plot_decision, _known_objects or None)
        ctx["plot_decision"] = plot_decision
        logger.info("=== Stage 1 骨架 ===\n%s", plot_decision)

        # Start compose context build in parallel with Stage 2
        _scope = route.get("scope", "moderate")
        _skip_state = route.get("skip_state_settlement", False)
        _skip_npc_reaction = route.get("skip_npc_reaction", False)
        _skip_choices = route.get("skip_choices", False)
        _compose_ctx_task = asyncio.create_task(
            self._build_compose_context(plot_decision, ctx, scope=_scope, state=_state)
        )

        # Shared pre-computation for Stage 2/3
        recent_openings = []
        for node in ctx.get("recent_nodes", [])[-3:]:
            resp = node.get("ai_response", "")
            if resp:
                recent_openings.append(resp[:20])

        _prev_ending_type = ""
        if _prev_tail:
            _last_100 = _prev_tail[-100:]
            if '"' in _last_100 or '“' in _last_100 or '”' in _last_100:
                _prev_ending_type = "对话未完"
            elif any(w in _last_100 for w in ("走", "转身", "站起", "推开", "拿起", "迈")):
                _prev_ending_type = "动作收束"
            elif any(w in _last_100 for w in ("也许", "或许", "不知道", "？", "……")):
                _prev_ending_type = "悬念留白"
            else:
                _prev_ending_type = "画面定格"

        _nearby_hint = self.prompt_builder.build_nearby_npc_hint(ctx.get("nearby_npc_ids", []), _state)
        _use_merged_narrative = _use_native_tools  # P2: 合并 Stage 2+3 当工具调用可用时

        if _use_merged_narrative:
            # --- Stage 2+3 合并：单次叙事生成（工具调用模式）---
            compose_history = await _compose_ctx_task

            narrative_msgs, narrative_sys = self.prompt_builder.build_narrative_prompt(
                ctx, plot_decision, route, _state,
                recent_openings=recent_openings,
                history_context=compose_history,
                prev_narrative_tail=_prev_tail,
                prev_ending_type=_prev_ending_type,
                authors_note=self.authors_note,
                action_text=action_text,
                negative_prompt=self.negative_prompt,
                logit_bias_hint=self._build_logit_bias_hint(),
                estimated_minutes=ctx.get("estimated_minutes", 30),
                event_sections=ctx.get("event_sections"),
            )
            # Inject lorebook and nearby NPC hints
            _pc_disc = _state.get("pc_discovered_lore", [])
            _vis_lore = Lorebook.filter_by_visibility(ctx["activated_lore"], "pc", _pc_disc)
            narrative_msgs = self.prompt_builder.inject_depth_lore(narrative_msgs, _vis_lore)
            if _nearby_hint and narrative_msgs:
                narrative_msgs[-1]["content"] += _nearby_hint
            # Inject stage directives
            _sd_env = self._format_stage_directives(ctx, "env", state=_state)
            _sd_char = self._format_stage_directives(ctx, "char", state=_state)
            _extra_directives = []
            if _sd_env:
                _extra_directives.append("## 环境剧情线指令\n" + _sd_env)
            if _sd_char:
                _extra_directives.append("## 角色剧情线指令\n" + _sd_char)
            if _extra_directives and narrative_msgs:
                narrative_msgs[-1]["content"] += "\n\n" + "\n\n".join(_extra_directives)

            env_text = ""
            char_text = ""

            # Variables needed by post-narrative code (Stage 4b-temporal, review, state settlement)
            _use_state_tools = True  # merged path always uses state tools
            _active_sys = route.get("systems") or None
            _check_res = ctx.get("check_result")
            _old_time = ctx.get("old_time", "")
            compose_msgs = narrative_msgs  # for review retry reuse
            compose_sys = narrative_sys

            async def _4b_gen(msgs, sys_prompt):
                async with self._4b_semaphore:
                    return await self.ai_provider.generate(msgs, system=sys_prompt, max_tokens=4096, **self._stage_kwargs("state"))

            # 合并叙事生成（工具调用模式不流式，等完整响应）
            _narrative_reasoning = ""
            _4b_tasks = []
            try:
                resp = await self.ai_provider.generate_with_tools(
                    narrative_msgs, system=narrative_sys,
                    tools=NARRATIVE_TOOLS_SCHEMA,
                    max_tokens=8192, **self._stage_kwargs("narrative")
                )
                raw_narrative = resp.get("content", "")
                _narrative_reasoning = _extract_reasoning(raw_narrative)
                narrative = strip_think_tags(raw_narrative)

                # 处理工具调用结果
                for tc in (resp.get("tool_calls") or []):
                    tc_name = tc.get("name", "")
                    tc_args = tc.get("arguments", {})
                    if tc_name == "set_atmosphere":
                        ctx["atmosphere"] = tc_args
                        logger.info("set_atmosphere: %s", tc_args)
                    elif tc_name == "set_scene_image":
                        ctx["scene_image_prompt"] = tc_args
                        logger.info("set_scene_image: %s", tc_args)
            except BaseException:
                for t in _4b_tasks:
                    t.cancel()
                raise

        else:
            # --- fallback: 原 Stage 2 + Stage 3 分离逻辑 ---
            # --- Stage 2: 环境渲染 ‖ 角色行为（自适应）---
            _scene_type = route.get("scene_type", "")
            _skip_env = _scene_type in ("social", "rest") or route.get("scope") == "minor"
            if route.get("scope") == "minor":
                env_text = ""
                char_text = ""
            else:
                if _skip_env:
                    env_text = ""
                else:
                    env_msgs, env_sys = self.prompt_builder.build_env_render_prompt(
                        plot_decision, _state
                    )
                    _sd_env = self._format_stage_directives(ctx, "env", state=_state)
                    if _sd_env and env_msgs:
                        env_msgs[-1]["content"] += "\n\n## 环境剧情线指令\n" + _sd_env
                    try:
                        raw_env = await self.ai_provider.generate(env_msgs, system=env_sys, max_tokens=8192, **self._stage_kwargs("narrative"))
                    except Exception as _env_err:
                        logger.warning("环境渲染失败: %s", _env_err)
                        raw_env = ""
                    env_text = strip_think_tags(raw_env) if raw_env else ""
                char_msgs, char_sys = self.prompt_builder.build_character_action_prompt(
                    plot_decision, _state,
                    present_npc_ids=ctx.get("present_npc_ids"),
                    dice_results=ctx.get("dice_dicts"),
                    check_result=ctx.get("check_result"),
                    triggered_events=ctx.get("triggered_events"),
                    triggered_consequences=ctx.get("triggered_consequences"),
                )
                _sd_char = self._format_stage_directives(ctx, "char", state=_state)
                if _sd_char and char_msgs:
                    char_msgs[-1]["content"] += "\n\n## 角色剧情线指令\n" + _sd_char
                if env_text and char_msgs:
                    _env_brief = env_text[:150]
                    char_msgs[-1]["content"] += f"\n\n== 已确定的环境描写（角色行为须与之一致）==\n{_env_brief}"
                try:
                    raw_char = await self.ai_provider.generate(char_msgs, system=char_sys, max_tokens=8192, **self._stage_kwargs("narrative"))
                except Exception as _char_err:
                    logger.warning("角色行为失败: %s", _char_err)
                    raw_char = ""
                char_text = strip_think_tags(raw_char) if raw_char else ""

            # --- Stage 3: 叙事润色整合 ---
            compose_history = await _compose_ctx_task
            for _stage, _label, _missing in (
                ("env", "环境", not env_text),
                ("char", "角色", not char_text),
            ):
                _directive = self._format_stage_directives(ctx, _stage, state=_state)
                if _missing and _directive:
                    compose_history += f"\n\n## {_label}剧情线指令\n" + _directive

            compose_msgs, compose_sys = self.prompt_builder.build_narrative_compose_prompt(
                plot_decision, env_text, char_text, _state, recent_openings,
                missing_env=not env_text, missing_char=not char_text,
                history_context=compose_history,
                prev_narrative_tail=_prev_tail,
                scene_type=route.get("scene_type", ""),
                prev_ending_type=_prev_ending_type,
                authors_note=self.authors_note,
                action_text=action_text,
                negative_prompt=self.negative_prompt,
                logit_bias_hint=self._build_logit_bias_hint(),
                scope=route.get("scope", "moderate"),
                estimated_minutes=ctx.get("estimated_minutes", 30),
                event_sections=ctx.get("event_sections"),
            )
            _pc_disc = _state.get("pc_discovered_lore", [])
            _vis_lore = Lorebook.filter_by_visibility(ctx["activated_lore"], "pc", _pc_disc)
            compose_msgs = self.prompt_builder.inject_depth_lore(compose_msgs, _vis_lore)
            if _nearby_hint and compose_msgs:
                compose_msgs[-1]["content"] += _nearby_hint

            # --- Stage 3 ‖ 4b parallel launch ---
            _active_sys = route.get("systems") or None
            _check_res = ctx.get("check_result")
            _old_time = ctx.get("old_time", "")
            _use_state_tools = _use_native_tools and hasattr(self.ai_provider, 'generate_with_tools')

            # 4b parallel prompts/tasks only needed in fallback (non-tool) path
            _4b_tasks = []
            if not _use_state_tools and not _skip_state:
                res_msgs, res_sys = self.prompt_builder.build_world_state_resource_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                spa_msgs, spa_sys = self.prompt_builder.build_world_state_spatial_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                wld_msgs, wld_sys = self.prompt_builder.build_world_state_world_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                ext_msgs, ext_sys = self.prompt_builder.build_world_state_ext_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                    event_sections=ctx.get("event_sections"),
                )

                # stage directives → resource
                _sd_world = self._format_stage_directives(ctx, "world", state=_state)
                if _sd_world and wld_msgs:
                    wld_msgs[-1]["content"] += "\n\n## 当前剧情线指令\n" + _sd_world
                # lorebook context → resource
                _core_lore = self._build_lore_context_for_core(ctx.get("activated_lore", []))
                if _core_lore and res_msgs:
                    res_msgs[-1]["content"] += _core_lore
                # nearby NPC hint → spatial (reuse cached _nearby_hint from Stage 3)
                if _nearby_hint and spa_msgs:
                    spa_msgs[-1]["content"] += _nearby_hint
                # story context → ext
                _story_ctx = self.prompt_builder._build_story_context_section(_state)
                if _story_ctx and ext_msgs:
                    ext_msgs[-1]["content"] += f"\n\n{_story_ctx}"

                # Launch 4b tasks (concurrency limited by _4b_semaphore)
                async def _4b_gen(msgs, sys_prompt):
                    async with self._4b_semaphore:
                        return await self.ai_provider.generate(msgs, system=sys_prompt, max_tokens=4096, **self._stage_kwargs("state"))

                _4b_res_task = asyncio.create_task(_4b_gen(res_msgs, res_sys))
                _4b_spa_task = asyncio.create_task(_4b_gen(spa_msgs, spa_sys))
                _4b_wld_task = asyncio.create_task(_4b_gen(wld_msgs, wld_sys))
                _4b_ext_task = asyncio.create_task(_4b_gen(ext_msgs, ext_sys))
                _4b_tasks = [_4b_res_task, _4b_spa_task, _4b_wld_task, _4b_ext_task]

            # Stage 3: narrative generation (streaming or non-streaming)
            _narrative_reasoning = ""
            if streaming and _guard_narrative:
                yield {"type": "preflight", "stage": "narrative_review"}
            try:
                if streaming:
                    full_narrative = ""
                    _think_parts = []
                    raw_stream = self.ai_provider.generate_stream(
                        compose_msgs, system=compose_sys, raw=True, **self._stage_kwargs("narrative")
                    )
                    async for msg_type, chunk in stream_split_think(raw_stream):
                        if msg_type == "think":
                            _think_parts.append(chunk)
                            yield {"type": "thinking", "content": chunk}
                        else:
                            full_narrative += chunk
                            if not _guard_narrative:
                                yield {"type": "text", "content": chunk}
                    narrative = strip_think_tags(full_narrative)
                    if _think_parts:
                        _narrative_reasoning = "".join(_think_parts)
                else:
                    raw_narrative_result = await self.ai_provider.generate(
                        compose_msgs, system=compose_sys, raw=True, **self._stage_kwargs("narrative")
                    )
                    _narrative_reasoning = _extract_reasoning(raw_narrative_result)
                    narrative = strip_think_tags(raw_narrative_result)
            except BaseException:
                for t in _4b_tasks:
                    t.cancel()
                raise

            # 素材复用率监控
            if char_text and narrative:
                _n = 6
                _src_ngrams = set(char_text[i:i+_n] for i in range(max(0, len(char_text) - _n + 1)))
                _out_ngrams = [narrative[i:i+_n] for i in range(max(0, len(narrative) - _n + 1))]
                if _out_ngrams and _src_ngrams:
                    _reuse = sum(1 for ng in _out_ngrams if ng in _src_ngrams) / len(_out_ngrams)
                    if _reuse > 0.6:
                        logger.warning("Stage 3 素材复用率 %.1f%%（高于60%%阈值）", _reuse * 100)

        if not narrative.strip():
            logger.warning("Stage 3 叙事正文为空，重试一次")
            try:
                retry_msgs = [dict(message) for message in compose_msgs]
                retry_msgs[-1]["content"] += "\n\n请输出本回合的叙事正文，不要只输出思考或工具调用。"
                retry_raw = await self.ai_provider.generate(
                    retry_msgs, system=compose_sys, raw=True,
                    max_tokens=8192, **self._stage_kwargs("narrative"),
                )
                narrative = strip_think_tags(retry_raw).strip()
                if not narrative:
                    raise RuntimeError("叙事正文为空，不能结算本回合")
            except BaseException:
                for task in _4b_tasks:
                    task.cancel()
                raise
            if streaming and not _guard_narrative:
                yield {"type": "narrative_revised", "content": narrative}

        # NPC 声音校验
        pre_fix_narrative = narrative
        narrative = await self._maybe_fix_npc_voices(narrative, _state, ctx.get("present_npc_ids"))
        if streaming and narrative != pre_fix_narrative and not _guard_narrative:
            yield {"type": "narrative_revised", "content": narrative}

        # Stage 4b-temporal: 延迟到叙事完成后（仅 fallback 路径）
        if not _use_state_tools and not _skip_state:
            tmp_msgs, tmp_sys = self.prompt_builder.build_world_state_temporal_prompt(
                narrative, action_text, _state,
                check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
            )
            if tmp_msgs:
                tmp_msgs[-1]["content"] += (
                    f"\n\n## end_time 决策指引\n"
                    f"当前游戏时间: {_old_time}\n"
                    "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                    "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                    "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                    "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                    "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间（如23:30），若叙事写到醒来才给次日早晨\n"
                    f"格式示例: {_old_time[:10] or '1970-01-01'}T10:00:00"
                )
            _4b_tmp_task = asyncio.create_task(_4b_gen(tmp_msgs, tmp_sys))
            _4b_tasks.append(_4b_tmp_task)

        # ★ Stage 3.5 review ‖ NPC RAG 并行 ★
        async def _do_review():
            return await self._review_narrative(narrative, action_text, ctx, state=_state)

        async def _do_npc_rag():
            if not (self.vector_memory and ctx.get("present_npc_ids")):
                return ""
            _npc_st = _state.get("npcs", {})
            npc_names = [_npc_st.get(nid, {}).get("name", nid)
                         for nid in ctx["present_npc_ids"][:3]
                         if isinstance(_npc_st.get(nid), dict)]
            if not npc_names:
                return ""
            npc_hits = await self._stage_rag_query(
                " ".join(npc_names), top_k=3, doc_type="turn",
                exclude_turns=[self.turn_number],
            )
            if npc_hits:
                return "\n".join(f"第{r['turn']}回合: {r['text'][:200]}" for r in npc_hits)
            return ""

        review, npc_rag_context = await asyncio.gather(
            _do_review(), _do_npc_rag(),
        )

        if review and not review.get("pass"):
            violations = review.get("violations", [])
            logger.info("叙事质量校验未通过: %s", violations)
            violation_text = "\n".join(
                f"- {v.get('type', '?')}: {v.get('detail', '')}" for v in violations if isinstance(v, dict)
            )
            if violation_text:
                retry_msgs = [dict(m) for m in compose_msgs]
                retry_msgs[-1]["content"] += (
                    f"\n\n## 上次生成被审核拒绝，请修正以下问题后重写：\n{violation_text}"
                )
                try:
                    retry_raw = await self.ai_provider.generate(
                        retry_msgs, system=compose_sys, **self._stage_kwargs("narrative")
                    )
                    revised_narrative = strip_think_tags(retry_raw).strip()
                    if revised_narrative:
                        narrative = revised_narrative
                        if streaming and not _guard_narrative:
                            yield {"type": "narrative_revised", "content": narrative}
                    else:
                        logger.warning("叙事重试正文为空，保留原叙事")
                except Exception as e:
                    logger.warning("叙事重试失败: %s", e)

        if self._narrative_reveals_pending_consequence(ctx, narrative, state=_state):
            retry_msgs = [dict(message) for message in compose_msgs]
            retry_msgs[-1]["content"] += "\n\n上一版叙事提前揭露尚未触发的后果，请只写玩家此刻可见的事实并重写。"
            try:
                retry_raw = await self.ai_provider.generate(
                    retry_msgs, system=compose_sys, raw=True,
                    max_tokens=8192, **self._stage_kwargs("narrative"),
                )
                revised_narrative = strip_think_tags(retry_raw).strip()
                if not revised_narrative or self._narrative_reveals_pending_consequence(
                        ctx, revised_narrative, state=_state):
                    raise PendingConsequenceDisclosureError("叙事包含未触发后果，本回合未结算，请重试")
            except BaseException:
                for task in _4b_tasks:
                    task.cancel()
                raise
            narrative = revised_narrative
            if streaming and not _guard_narrative:
                yield {"type": "narrative_revised", "content": narrative}
        if streaming and _guard_narrative:
            yield {"type": "text", "content": narrative}

        # 汇合 Stage 4b 结果
        if _skip_state:
            print("[Route skip] skip_state_settlement=True → 跳过 Stage 4 状态推演")
            logger.info("[Route skip] skip_state_settlement=True → 跳过 Stage 4 状态推演")
            parsed = self.response_parser._empty_result()
            parsed["narrative"] = narrative.strip()
        elif _use_state_tools:
            # 工具调用路径：单次调用替代 5 路并行（带重试）
            parsed = await _retry_on_failure(
                lambda: self._execute_state_settlement(
                    ctx, narrative, plot_decision, action_text, _state,
                    old_time=_old_time,
                ),
                max_retries=1, label="状态推演",
            )
            if parsed is None:
                parsed = self.response_parser._empty_result()
                parsed["narrative"] = narrative.strip()
                _warnings.append("状态推演工具调用失败，本回合属性/物品变化可能未正确记录")
        else:
            # fallback: 原 5 路并行 + parse_split_v3
            _4b_raw = await asyncio.gather(
                _4b_res_task, _4b_spa_task, _4b_tmp_task, _4b_wld_task, _4b_ext_task,
                return_exceptions=True,
            )
            [raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext], _4b_warns = self._sanitize_gather_results(
                _4b_raw, [("资源状态推演", True), ("空间状态推演", False),
                           ("时间状态推演", False), ("世界属性推演", False), ("扩展状态推演", False)],
            )
            _warnings.extend(_4b_warns)

            # partial parse（不含NPC）
            parsed = self.response_parser.parse_split_v3(narrative, "", raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext)

            if parsed.get("_state_parse_failed"):
                _warnings.append("状态推演部分失败，本回合属性/物品变化可能未正确记录")

        new_scene = parsed.get("scene_details")
        if new_scene and isinstance(new_scene, dict):
            _state["scene_details"] = new_scene

        _run_npc = not _skip_npc_reaction or (_skip_state and (
            route.get("scene_type") == "social" or "new_npc" in (route.get("systems") or [])
        ))

        if not _run_npc:
            print("[Route skip] skip_npc_reaction=True → 跳过 Stage 4a NPC反应")
            logger.info("[Route skip] skip_npc_reaction=True → 跳过 Stage 4a NPC反应")
        if _skip_choices:
            print("[Route skip] skip_choices=True → 跳过 Stage 5 选项生成")
            logger.info("[Route skip] skip_choices=True → 跳过 Stage 5 选项生成")

        npc_lore_ids = set()
        _present_npc_ids = ctx.get("present_npc_ids", [])
        for nid in _present_npc_ids:
            npc_def = self._npc_by_id.get(nid, {})
            npc_lore_ids.update(npc_def.get("related_lore", []))
        _npc_base_lore = [
            e for e in ctx.get("activated_lore", [])
            if e.constant or e.id in npc_lore_ids
        ] if npc_lore_ids else ctx.get("activated_lore", [])
        npc_filtered_lore = Lorebook.filter_by_visibility(
            _npc_base_lore, "npc", npc_ids=_present_npc_ids,
        ) if _npc_base_lore else None

        _sd_npc = self._format_stage_directives(ctx, "npc", state=_state)
        if _sd_npc:
            _npc_dir = "## 当前剧情线NPC指令\n" + _sd_npc
            npc_rag_context = (npc_rag_context + "\n" + _npc_dir).strip() if npc_rag_context else _npc_dir

        raw_npc = ""
        if _run_npc:
            npc_msgs, npc_sys = self.prompt_builder.build_npc_reaction_prompt(
                narrative, action_text, _state,
                check_result=ctx.get("check_result"),
                present_npc_ids=ctx.get("present_npc_ids"),
                npc_history=npc_rag_context,
                npc_lore=npc_filtered_lore,
            )
            try:
                if _use_native_tools:
                    self._reset_stage45_tool_buffers()
                    response = await self._run_stage45_tools(
                        npc_msgs, npc_sys, NPC_REACTION_TOOLS,
                        max_tokens=4096, stage_key="state", label="NPC关系推演",
                    )
                    if not response["tool_calls"]:
                        raw_npc = response["content"]
                    for call in response["tool_calls"]:
                        self._run_npc_reaction_tool(call.get("name", ""), call.get("arguments") or {})
                    self._merge_npc_reaction_tool_results(parsed, narrative)
                else:
                    raw_npc = await _retry_on_failure(
                        lambda: self.ai_provider.generate(npc_msgs, system=npc_sys, max_tokens=4096, **self._stage_kwargs("state")),
                        max_retries=1, label="NPC反应", raise_on_failure=True,
                    )
            except Exception as exc:
                logger.warning("NPC关系推演失败: %s", exc)
                _warnings.append("NPC关系推演失败，本回合该部分变化可能未记录")
        # merge NPC results into parsed
        if raw_npc:
            npc_parsed = self.response_parser.parse_npc_reaction(strip_think_tags(raw_npc))
            for k, v in npc_parsed.items():
                if k == "scene_details" and parsed.get("scene_details"):
                    sd = parsed["scene_details"]
                    if isinstance(v, dict):
                        if v.get("npc_expressions"):
                            sd.setdefault("npc_expressions", v["npc_expressions"])
                        if v.get("pending_tension"):
                            sd.setdefault("pending_tension", v["pending_tension"])
                elif k == "new_npcs":
                    existing = parsed.setdefault("new_npcs", [])
                    known_ids = {npc.get("id") for npc in existing if isinstance(npc, dict)}
                    known_names = {npc.get("name") for npc in existing if isinstance(npc, dict)}
                    for npc in v if isinstance(v, list) else []:
                        if (self._is_narrated_new_npc(npc, narrative)
                                and npc["id"] not in known_ids and npc["name"] not in known_names):
                            existing.append(npc)
                            known_ids.add(npc["id"])
                            known_names.add(npc["name"])
                else:
                    parsed[k] = v

        parsed["choices"] = []
        ctx["_workflow_choices"] = {
            "skip": _skip_choices, "native": bool(_use_native_tools), "action_text": action_text,
        }

        yield {
            "type": "pipeline_result",
            "narrative": narrative,
            "parsed": parsed,
            "warnings": _warnings,
            "plot_decision": plot_decision,
            "plot_reasoning": plot_reasoning,
            "narrative_reasoning": _narrative_reasoning,
            "compose_msgs": compose_msgs,
            "compose_sys": compose_sys,
        }

    async def _generate_settled_workflow_choices(self, narrative: str, ctx: dict) -> list[dict]:
        state = self.current_state
        options = ctx["_workflow_choices"]
        if options["skip"]:
            return self._enrich_choice_previews(self._generate_context_choices(state))
        activated_lore, _ = self.prompt_builder.scan_lorebook(
            options["action_text"], [node.get("ai_response", "") for node in ctx.get("recent_nodes", [])] + [narrative],
            timed_state=state.get("lorebook_timed_state"), turn_number=self.turn_number, state=state,
        )
        event_sections = self.prompt_builder._format_events_for_prompt(self.event_engine.get_events_for_prompt(state))
        messages, system = self.prompt_builder.build_choices_prompt(
            narrative, options["action_text"], state, turn_number=self.turn_number,
            activated_lore=activated_lore, event_sections=event_sections,
            pc_discovered_lore=state.get("pc_discovered_lore", []),
        )
        parsed = {}
        raw_choices = ""
        try:
            if options["native"]:
                self._choices_tool_calls = []
                response = await self._run_stage45_tools(
                    messages, system, CHOICES_TOOLS, max_tokens=8192, stage_key="choices", label="选项生成",
                )
                if not response["tool_calls"]:
                    raw_choices = response["content"]
                for call in response["tool_calls"]:
                    self._run_choices_tool(call.get("name", ""), call.get("arguments") or {})
                self._merge_choices_tool_results(parsed)
            else:
                raw_choices = await _retry_on_failure(
                    lambda: self.ai_provider.generate(messages, system=system, max_tokens=8192, **self._stage_kwargs("choices")),
                    max_retries=1, label="选项生成", raise_on_failure=True,
                )
        except Exception as exc:
            logger.warning("选项生成失败，使用结算后上下文选项: %s", exc)
        if raw_choices:
            parsed["choices"] = self.response_parser.parse_choices(strip_think_tags(raw_choices))
        choices = self._filter_player_visible_choices(
            parsed.get("choices") or [], narrative, state=state, ctx={**ctx, "event_sections": event_sections},
        )
        return self._enrich_choice_previews(self._replace_generic_choices(choices) or self._generate_context_choices(state))

    def _run_turn_computations(self, story_tree_result, check_result, achieved_milestones):
        """Two-phase turn computations with explicit dependency ordering."""
        # Phase 1: independent computations
        self._evaluate_tone(story_tree_result)
        goal_changes = self._settle_actor_goals()
        self._check_npc_goal_conflicts()
        self._update_companion_loyalty()
        self._sync_companions()
        self._apply_reputation_attitude_modifier()
        self._check_quest_templates()
        self._compute_xp_and_level({"check_result": check_result, "achieved_milestones": achieved_milestones})
        self._collect_interactables()
        self._compute_time_atmosphere()
        self._compute_difficulty_awareness()
        self._compute_npc_relationship_depth()
        self._compute_discovery_hints()
        # Phase 2: depends on phase 1 (attitude -> secrets/interventions, time_atmo -> effects_summary)
        self._check_npc_secrets()
        self._check_npc_interventions()
        self._compute_active_effects_summary()
        return goal_changes

    async def _build_compose_context(self, plot_decision: str, ctx: dict, scope: str = "moderate", *, state: dict | None = None) -> str:
        """Build compose_history for Stage 3, can run in parallel with Stage 2."""
        _st = state if state is not None else self.current_state
        compose_history = self.script_variables.expand_macros(ctx.get("base_history_context", ""), _st)
        cm = ctx.get("context_memory", "")
        if cm:
            compose_history = (compose_history + "\n\n" + cm).strip() if compose_history else cm
        if scope != "minor":
            _compose_reasoning = ctx.get("recent_reasoning", [])
            if _compose_reasoning:
                reasoning_lines = ["## 前轮决策思路（供参考，保持连贯）"]
                for r in _compose_reasoning:
                    reasoning_lines.append(f"第{r['turn']}回合: {r['reasoning']}")
                compose_history = (compose_history + "\n\n" + "\n".join(reasoning_lines)).strip()
        if scope != "minor" and self.vector_memory and plot_decision:
            fh_hits = await self._stage_rag_query(
                plot_decision[:200], top_k=2, doc_type="turn",
                exclude_turns=[self.turn_number],
                boost_lore_ids=ctx.get("story_lore_ids") or None,
            )
            if fh_hits:
                fh_lines = ["## 相关历史伏笔（确保叙事连贯）"]
                for r in fh_hits:
                    fh_lines.append(f"第{r['turn']}回合: {r['text'][:80]}")
                compose_history = (compose_history + "\n\n" + "\n".join(fh_lines)).strip()
        _sd_compose = self._format_stage_directives(ctx, "compose", state=_st)
        if _sd_compose:
            compose_history = (compose_history + "\n\n## 当前剧情线指令\n" + _sd_compose).strip()
        if self.story_tree_engine:
            _upcoming = self.story_tree_engine.get_upcoming_nodes(_st, limit=2)
            if _upcoming:
                _hint_lines = ["## 叙事伏笔暗示（自然编入叙事，不要直说）"]
                for _un in _upcoming:
                    _hint_lines.append(f"- {_un.get('name', '')}: {_un.get('description', '')[:80]}")
                compose_history = (compose_history + "\n\n" + "\n".join(_hint_lines)).strip()
        if scope != "minor":
            npc_knowledge = self._build_npc_knowledge_context(ctx, _st)
            if npc_knowledge:
                compose_history = (compose_history + "\n\n" + npc_knowledge).strip()
            causal_ctx = self._build_causal_context(_st)
            if causal_ctx:
                compose_history = (compose_history + "\n\n" + causal_ctx).strip()
        return compose_history
