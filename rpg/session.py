"""TavernRPGSession：主会话类，协调整个推演流程"""

import os
import re
import json
import copy
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import asdict

from db import database
from config import SCRIPTS_DIR

from .llm_utils import llm_call, _parse_json_from_llm, get_llm_state, _semantic_attr_value, _semantic_relationship
from .tool_system import _build_tool_schemas_and_executors
from .models import Turn, WorldStateManager, TIME_HINT_HOURS, TAVERN_MODULES_AVAILABLE
from .agents import (
    DirectorAgent, NPCAgent, OutlineAgent, ConflictDetector, SceneAgent, ContinuityValidator,
)
from .script_builder import ScriptBuilder

# 酒馆引擎模块导入
try:
    from engine.world_tree import WorldTree
    from engine.event_engine import EventEngine, GameEvent, EventResult
    from engine.lorebook import Lorebook, LorebookEntry
    from engine.history_summarizer import HistorySummarizer
    from engine.state_manager import StateManager
    from engine.script_loader import ScriptLoader
    from engine.dice import DiceRoller, DiceResult
    from engine.script_variables import ScriptVariables
    from engine.triggers import TriggerEngine
    from engine.meta_events import MetaEventBus, MetaEvent, MetaEventTrigger
    try:
        from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE
    except ImportError:
        VectorMemory = None
        _VECTOR_AVAILABLE = False
    try:
        from engine.class_system import ClassRegistry
    except ImportError:
        ClassRegistry = None
except ImportError:
    WorldTree = None
    EventEngine = None
    Lorebook = None
    HistorySummarizer = None
    DiceRoller = None
    ScriptVariables = None
    TriggerEngine = None
    MetaEventBus = None
    VectorMemory = None
    _VECTOR_AVAILABLE = False
    ClassRegistry = None

# fallback DiceRoller
if DiceRoller is None:
    from engine.dice import DiceRoller

# fallback MetaEventBus
if MetaEventBus is None:
    from engine.meta_events import MetaEventBus

class TavernRPGSession:
    """主会话类：协调整个推演流程"""

    def __init__(self, script_id: str = "", script_data: Optional[Dict[str, Any]] = None):
        """初始化会话，加载剧本 + 复杂结构"""
        # 加载剧本
        if script_data is None:
            try:
                with open(os.path.join(SCRIPTS_DIR, script_id + ".json"), "r", encoding="utf-8") as f:
                    script_data = json.load(f)
            except FileNotFoundError:
                print(f"[WARN] 找不到{script_id}.json，使用最小化剧本")
                script_data = {"script_id": script_id, "npcs": []}
        self.script_data = script_data
        self.opening_text = self.script_data.get("opening", {}).get("text", "")

        # 初始化世界状态
        self.world_state = WorldStateManager(self.script_data)

        # 初始化NPC Agents
        self.npc_agents: Dict[str, NPCAgent] = {}
        for npc in self.script_data.get("npcs", []):
            npc_id = npc.get("id")
            self.npc_agents[npc_id] = NPCAgent(npc_id, npc, self.world_state)

        # 推演 pending 状态
        self._pending_turn = None  # 存储待确认的推演结果
        self._last_validation = {}  # 最近一次校验结果

        # === 引入酒馆的复杂结构 ===
        if TAVERN_MODULES_AVAILABLE:
            self.world_tree = WorldTree(script_id=script_id)
            self.event_engine = EventEngine(self.script_data)
            self.lorebook = Lorebook(self.script_data.get("lorebook", []))
            self.history_summarizer = HistorySummarizer()
            # 新增模块
            self.state_manager = self.world_state._state_manager
            self.dice_roller = DiceRoller()
            self.script_variables = ScriptVariables(self.script_data.get('variables', []))
            self.script_variables.init_state(self.world_state.event_state)
            self.trigger_engine = TriggerEngine(
                self.script_data.get('triggers', []), self.script_variables)
            self.meta_event_bus = MetaEventBus()
            self.vector_memory = VectorMemory(script_id) if VectorMemory and _VECTOR_AVAILABLE else None
            self.class_system = ClassRegistry(self.script_data.get('system')) if ClassRegistry else None
            print(f"[OK] WorldTree, EventEngine, Lorebook 已加载")
        else:
            self.world_tree = None
            self.event_engine = None
            self.lorebook = None
            self.history_summarizer = None
            self.state_manager = None
            self.dice_roller = DiceRoller()
            self.script_variables = None
            self.trigger_engine = None
            self.meta_event_bus = MetaEventBus()
            self.vector_memory = None
            self.class_system = None
            print(f"[WARN] 酒馆模块不可用，某些功能受限")

        # Pacing 系统初始化
        self.tension = 0  # 0-100, 越高越紧张
        self._tension_afterglow = 0  # 余韵期剩余轮数
        self._tension_peak = 0  # 最近的 tension 峰值
        self._pending_tension_adjustment = 0  # OutlineAgent 的 tension 手动调节
        self.npc_chat_history: Dict[str, list] = {}  # NPC 对话历史
        self.npc_intervention_cooldowns: Dict[str, int] = {}  # NPC 干预冷却
        self.npc_memories: Dict[str, list] = {}  # NPC 跨回合记忆
        self.npc_plans: Dict[str, Dict[str, Any]] = {}  # NPC 长期计划
        self.npc_emotion_arcs: Dict[str, list] = {}  # NPC 情感弧线

        # NPC 间关系网络
        self.npc_relationships_global: Dict[str, dict] = {}
        self.npc_relationships_known: Dict[str, dict] = {}
        self._REL_NET_MAX = 80

        # 信息不对称系统
        self.information_network: list = []

        # 阵营声望系统
        self.faction_reputation: Dict[str, dict] = {}

        # 工具执行器映射表
        self._tool_handlers = {
            "modify_relationship": self._exec_modify_relationship,
            "apply_state_change": self._exec_apply_state_change,
            "move_player": self._exec_move_player,
            "set_world_prop": self._exec_set_world_prop,
            "give_item": self._exec_give_item,
            "take_item": self._exec_take_item,
            "spawn_npc": self._exec_spawn_npc,
            "remove_npc": self._exec_remove_npc,
            "update_npc_relationship": self._exec_update_npc_relationship,
            "share_information": self._exec_share_information,
            "update_plan": self._exec_update_plan,
            "change_faction_reputation": self._exec_change_faction_reputation,
            "trigger_event": self._exec_trigger_event,
        }

        print(f"[OK] 酒馆RPG系统初始化完成")
        print(f"  剧本: {self.script_data.get('script_name', 'Unknown')}")
        print(f"  NPC数: {len(self.npc_agents)}")
        print(f"  变量数: {len(self.world_state.variables)}")


    async def process_turn(self, player_input: str, director_plan: dict = None) -> Dict[str, Any]:
        """处理单个推演轮次（纯编排）"""
        dp = director_plan or {}
        resolved_action = dp.get("resolved_action", player_input)
        use_llm = get_llm_state().get("client") is not None
        hints = dp.get("pipeline_hints", {})

        # 阶段0: 时间推进 + 事件 + 恢复
        hours = self._advance_time(dp, player_input)
        event_result = self._tick_events(resolved_action)
        self.world_state.apply_natural_recovery(hours)

        # 阶段1: 上下文组装
        print('\n[阶段1] 上下文组装...')
        lore_text, event_inject = self._scan_lorebook_and_events(player_input, event_result)
        context = self._build_context(player_input, dp, event_result, lore_text, event_inject)
        print('[OK] 收集完成')

        # 阶段2: NPC 推理
        print(f'\n[阶段2] NPC有限视角推理... ({"LLM" if use_llm else "规则"})')
        npc_outputs = await self._run_npc_phase(context, use_llm)

        # 被动技能检定
        passive_checks = self._check_passive_skills(npc_outputs)
        if passive_checks:
            context["passive_checks"] = passive_checks
            print(f'[被动检定] {len(passive_checks)} 次: {", ".join(c["trigger_npc"] + "→" + c["result"]["outcome"] for c in passive_checks)}')

        # 阶段2.5: Pacing + 干预 + 计划分解
        tension = self._compute_tension(resolved_action, npc_outputs,
                                         {"conflicts": [], "event_result_summary": context.get("shared", {}).get("event_result_summary", {})})
        context["tension"] = tension
        context["pacing_hint"] = self._pacing_hint(tension)
        if tension >= 50:
            print(f'[节奏] tension={tension}/100 → {context["pacing_hint"][:20]}...')
        self._check_interventions_and_plans(context, resolved_action)

        # 阶段3: Outline + Conflict（按 pipeline_hints 条件执行）
        do_outline = not hints.get("skip_outline", False)
        do_conflict = not hints.get("skip_conflict", False)

        if do_outline or do_conflict:
            print(f'\n[阶段3] {"大纲" if do_outline else ""}{"+" if do_outline and do_conflict else ""}{"冲突分析" if do_conflict else ""}... ({"LLM" if use_llm else "规则"})')

        if do_outline and use_llm:
            if do_conflict:
                outline_task = OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                conflict_task = ConflictDetector.detect(npc_outputs)
                outline, conflicts = await asyncio.gather(outline_task, conflict_task)
            else:
                outline = await OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                conflicts = []
        elif do_outline:
            outline = OutlineAgent.synthesize_rules(context, npc_outputs, self.world_state)
            conflicts = ConflictDetector.detect_rules(npc_outputs) if do_conflict else []
        else:
            outline = {"summary": "", "next_phase": "", "tool_calls": []}
            conflicts = ConflictDetector.detect_rules(npc_outputs) if do_conflict else []

        if do_outline or do_conflict:
            print(f'[OK] 大纲完成: {len(outline.get("tool_calls", []))} 个工具提议, 冲突: {len(conflicts)}')

        # 质量反馈环
        re_reason = outline.get("re_reason_requests", [])
        if re_reason and use_llm:
            req = re_reason[0]
            rr_agent = self.npc_agents.get(req.get("npc_id", ""))
            if rr_agent:
                print(f'[反馈] OutlineAgent 请求 {rr_agent.name} 重新推理: {req.get("reason", "")}')
                try:
                    npc_outputs[req["npc_id"]] = await rr_agent._reason_llm(context, session=self)
                    outline = await OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                    print(f'[反馈] 重新综合完成')
                except Exception as e:
                    print(f'[反馈] 重新推理失败: {e}')

        # 阶段3.5: NPC 对话轮
        npc_dialogues = await self._maybe_npc_dialogue(conflicts, npc_outputs, context, use_llm)
        if npc_dialogues:
            context["npc_dialogues"] = npc_dialogues
        outline["conflicts"] = conflicts

        # 阶段4: 场景生成
        scene_budget = hints.get("scene_budget", "normal")
        print(f'[阶段4] 场景生成... ({"LLM" if use_llm else "规则"}, budget={scene_budget})')
        if use_llm:
            scene = await SceneAgent.generate_llm(context, npc_outputs, self.world_state, outline,
                                                   session=self, scene_budget=scene_budget)
        else:
            scene = SceneAgent.generate_rules(context, npc_outputs, self.world_state, outline)
        scene["tension"] = tension
        print(f'[OK] 场景生成完成')

        dice_results = []
        if self.dice_roller:
            for dice_cfg in self.script_data.get('always_active_dice', []):
                try:
                    dr = self.dice_roller.roll_and_resolve(
                        dice_cfg.get('dice', {'count': 1, 'faces': 100}),
                        dice_cfg.get('ranges', []),
                    )
                    dice_results.append({'id': dice_cfg.get('id', ''), 'formula': dr.formula,
                        'total': dr.total, 'label': dr.range_label,
                        'state_changes': dr.range_state_changes})
                except Exception as e:
                    print(f'[WARN] dice failed: {e}')
            if dice_results:
                print(f'[骰子] {len(dice_results)} 次检定')
        scene['dice_results'] = dice_results

        # 阶段5: 构建 Turn 记录（同步，不等校验）
        turn = self._build_and_record_turn(player_input, context, npc_outputs, outline, scene)

        scene["npc_interventions"] = context.get("npc_interventions", [])
        if context.get("has_plan_declaration"):
            scene["has_plan_declaration"] = True

        # 阶段4.5+后处理：异步执行，不阻塞返回
        async def _deferred_post_scene():
            try:
                validation = {}
                post_outline = outline.copy()
                if not hints.get("skip_validator", False):
                    if use_llm:
                        validation = await ContinuityValidator.validate(
                            scene.get("scene_text", ""), self.world_state, self.script_data)
                    else:
                        validation = ContinuityValidator.validate_rules(
                            scene.get("scene_text", ""), self.script_data)
                    if validation.get("inconsistencies"):
                        print(f'[校验] 发现 {len(validation["inconsistencies"])} 处不一致')
                    if validation.get("needs_expansion"):
                        post_outline["needs_expansion"] = True
                        print(f'[校验] 需要世界扩展: {validation.get("new_names", [])}')
                self._last_validation = validation
                await self._post_turn_processing(turn, scene, post_outline, player_input, use_llm)
                print(f'[OK] 后台校验与后处理完成')
            except Exception as e:
                print(f'[WARN] 后台校验/后处理失败: {e}')
                self._last_validation = {"error": str(e)}

        self._last_validation = {"pending": True}
        asyncio.create_task(_deferred_post_scene())

        return {"turn": turn, "scene": scene, "choices": scene.get("choices", [])}

    def _advance_time(self, dp: dict, player_input: str) -> float:
        """推进游戏时间，返回推进的小时数"""
        old_date = self.world_state.current_date.isoformat()
        self.world_state.current_turn += 1
        time_hint = dp.get("time_hint", "medium")
        hours = TIME_HINT_HOURS.get(time_hint, 2)
        self.world_state.current_date += timedelta(hours=hours)

        resolved_action = dp.get("resolved_action", player_input)
        print(f'\n{"="*60}')
        print(f'推演轮次 #{self.world_state.current_turn}')
        print(f'玩家输入: {player_input}')
        if resolved_action != player_input:
            print(f'Director解析: {resolved_action}')
        skip_info = dp.get("skip_npcs", [])
        if skip_info:
            print(f'跳过NPC: {", ".join(skip_info)}')
        print(f'时间步长: {time_hint} (+{hours}h)')
        print(f'{"="*60}')
        return hours

    def _tick_events(self, resolved_action: str):
        """EventEngine tick，返回 event_result"""
        if not self.event_engine:
            return None
        try:
            event_result = self.event_engine.tick(
                state=self.world_state.event_state,
                turn_number=self.world_state.current_turn,
                game_time=self.world_state.current_date.isoformat(),
                old_time=(self.world_state.current_date - timedelta(hours=2)).isoformat(),
                condition_eval=self._evaluate_condition,
                player_action=resolved_action,
                state_manager=self.state_manager,
            )
            self._apply_event_result(event_result)
            ac = len(event_result.newly_active)
            cc = len(event_result.newly_completed)
            if ac or cc:
                print(f'[事件] 新激活: {ac}, 完成: {cc}')
            if event_result.notifications:
                for n in event_result.notifications:
                    print(f'  [通知] {n}')
            return event_result
        except Exception as e:
            print(f'[WARN] EventEngine.tick 失败: {e}')
            return None

    def _scan_lorebook_and_events(self, player_input: str, event_result) -> tuple:
        """扫描 Lorebook + 构建事件注入文本，返回 (lore_text, event_inject)"""
        lore_text = ""
        if self.lorebook:
            try:
                recent_msgs = [t.player_action for t in self.world_state.turns[-5:]]
                activated_entries, self.world_state.lorebook_timed_state = self.lorebook.scan(
                    player_action=player_input,
                    recent_messages=recent_msgs,
                    timed_state=self.world_state.lorebook_timed_state,
                    turn_number=self.world_state.current_turn,
                )
                if activated_entries:
                    lore_text = self.lorebook.format_for_prompt(activated_entries)
                    print(f'[知识库] 激活 {len(activated_entries)} 条知识')
            except Exception as e:
                print(f'[WARN] Lorebook.scan 失败: {e}')

        event_inject = ""
        if event_result:
            parts = []
            parts.extend(event_result.inject_prompts)
            for cb in event_result.narrative_callbacks:
                text = cb.get("text", "") if isinstance(cb, dict) else str(cb)
                if text:
                    parts.append(text)
            for cons in event_result.triggered_consequences:
                desc = cons.get("description", "")
                if desc:
                    parts.append(f"[后果触发] {desc}")
            for w in event_result.imminent_warnings:
                parts.append(f"[即将发生] {w}")
            for se in event_result.scheduled_events:
                desc = se.get("description", "")
                if desc:
                    parts.append(f"[定时事件] {desc}")
            if parts:
                event_inject = '\n'.join(parts)
                print(f'[事件] {len(parts)} 条事件注入提示')
        return lore_text, event_inject

    async def _run_npc_phase(self, context: dict, use_llm: bool) -> dict:
        """阶段2：NPC 推理"""
        if use_llm:
            npc_outputs = await self._run_npc_agents_llm(context)
        else:
            npc_outputs = self._run_npc_agents_parallel(context)
        print(f'[OK] {len(npc_outputs)} 个NPC推理完成')
        for npc_id, output in npc_outputs.items():
            print(f'  - {output.get("name")}: {output.get("action_type")}')
        return npc_outputs

    def _check_interventions_and_plans(self, context: dict, resolved_action: str):
        """检查 NPC 干预和计划声明"""
        interventions = self._check_npc_interventions()
        if interventions:
            scene_hijack = None
            for iv in interventions:
                if iv.get("urgency") == "high":
                    scene_hijack = iv
                    break
            context["npc_interventions"] = interventions
            if scene_hijack:
                context["scene_hijack"] = scene_hijack
                hijack_npc = scene_hijack["npc_id"]
                if hijack_npc not in [o.get("npc_id") for o in context.get("npc_outputs", {}).values()]:
                    context["scene_type"] = "npc_hijack"
                print(f'[劫持] {scene_hijack["npc_name"]} 主动干预：{scene_hijack["reason"]}')

        if self._detect_plan_declaration(resolved_action):
            context["has_plan_declaration"] = True
            print(f'[计划] 检测到计划声明，将分解为步骤')

    async def _maybe_npc_dialogue(self, conflicts: list, npc_outputs: dict,
                                   context: dict, use_llm: bool) -> list:
        """高冲突时触发 NPC 间对话轮"""
        if not conflicts or not use_llm:
            return []
        high = [c for c in conflicts if c.get("severity") == "high"]
        if not high:
            return []
        results = []
        for conflict in high[:1]:
            between = conflict.get("between", [])
            if len(between) < 2:
                continue
            a_id, b_id = between[0], between[1]
            a_out = npc_outputs.get(a_id, {})
            b_out = npc_outputs.get(b_id, {})
            if (a_out.get("skipped") or b_out.get("skipped") or
                a_out.get("action_type") in ("观望", "无关") or
                b_out.get("action_type") in ("观望", "无关")):
                continue
            dialogue = await self._npc_dialogue_round(a_id, b_id, conflict.get("topic", ""), context)
            if dialogue:
                results.append(dialogue)
                print(f'[NPC对话] {dialogue["npc_a_name"]} vs {dialogue["npc_b_name"]}: {conflict.get("topic", "")}')
        return results

    def _build_and_record_turn(self, player_input: str, context: dict,
                                npc_outputs: dict, outline: dict, scene: dict) -> "Turn":
        """构建 Turn 对象并记录到 WorldTree"""
        state_before = self.world_state.get_snapshot()
        state_after = copy.deepcopy(state_before)
        turn = Turn(
            turn_num=self.world_state.current_turn,
            timestamp=datetime.now().isoformat(),
            player_action=player_input,
            context_object=context,
            npc_outputs=npc_outputs,
            outline_summary=outline,
            scene_output=scene,
            state_before=state_before,
            state_after=state_after,
            player_choices=scene.get("choices", [])
        )
        if self.world_tree:
            try:
                parent_node_id = self.world_tree.active_node_id
                node_id = self.world_tree.add_node(
                    parent_id=parent_node_id,
                    game_time=self.world_state.current_date.isoformat(),
                    turn_number=self.world_state.current_turn,
                    player_action={"text": player_input},
                    ai_response=scene.get('scene_text', ''),
                    choices_presented=scene.get('choices', []),
                    state_snapshot=self.world_state.get_snapshot(),
                )
                branch_count = sum(1 for n in self.world_tree.nodes.values() if not n.get("children_ids"))
                print(f'\n[树] 节点 {node_id} 已添加 (分支数: {branch_count})')
            except Exception as e:
                print(f'[WARN] 添加到WorldTree失败: {e}')
        return turn

    async def _post_turn_processing(self, turn: "Turn", scene: dict, outline: dict,
                                     player_input: str, use_llm: bool):
        """后处理：摘要、骰子、触发器、元事件、向量存储、世界扩展"""
        # HistorySummarizer
        if self.history_summarizer and self.world_tree and use_llm:
            try:
                recent_word_count = sum(
                    len(t.player_action) + len(t.scene_output.get("scene_text", ""))
                    for t in self.world_state.turns[-10:]
                )
                if self.history_summarizer.needs_summary(
                    self.world_state.current_turn, self.world_state.summary_state,
                    recent_word_count=recent_word_count,
                ):
                    branch = self.world_tree.get_active_branch()
                    keep = self.history_summarizer.keep_recent
                    older_nodes = branch[:-keep] if len(branch) > keep else []
                    if older_nodes:
                        existing_summary = self.world_state.summary_state.get("history_summary", "")
                        sys_prompt, user_prompt = self.history_summarizer.build_summary_prompt(
                            older_nodes, existing_summary
                        )
                        summary_text = await llm_call(sys_prompt, user_prompt, max_tokens=1500)
                        if summary_text:
                            self.world_state.summary_state = self.history_summarizer.update_state_with_summary(
                                self.world_state.summary_state, summary_text, self.world_state.current_turn,
                            )
                            print(f'[摘要] 历史已压缩至轮次 {self.world_state.current_turn}')
            except Exception as e:
                print(f'[WARN] HistorySummarizer 失败: {e}')

        # TriggerEngine
        if self.trigger_engine:
            try:
                trigger_actions = self.trigger_engine.fire('on_turn_end', self.world_state.event_state)
                if trigger_actions:
                    effects = self.trigger_engine.execute_actions(trigger_actions, self.world_state.event_state)
                    if effects.get('notifications'):
                        for msg in effects['notifications']:
                            print(f'  [触发器] {msg}')
                    if effects.get('lore_activations') and self.lorebook:
                        for eid in effects['lore_activations']:
                            self.lorebook.update_entry_enabled(eid, True)
                    if effects.get('lore_deactivations') and self.lorebook:
                        for eid in effects['lore_deactivations']:
                            self.lorebook.update_entry_enabled(eid, False)
                    print(f'[触发器] {len(trigger_actions)} 个动作执行')
            except Exception as e:
                print(f'[WARN] TriggerEngine failed: {e}')

        # MetaEventBus
        if self.meta_event_bus:
            try:
                fired_meta = self.meta_event_bus.evaluate(
                    self.world_state.current_turn, self.world_state.event_state,
                    condition_eval=self._evaluate_condition,
                    ai_available=use_llm, vector_available=self.vector_memory is not None,
                )
                for me in fired_meta:
                    MetaEventBus.mark_fired(self.world_state.event_state, me.id, self.world_state.current_turn)
                if fired_meta:
                    print(f'[元事件] {len(fired_meta)} 个触发')
            except Exception as e:
                print(f'[WARN] MetaEventBus failed: {e}')

        # VectorMemory 存储
        if self.vector_memory:
            try:
                scene_text = scene.get('scene_text', '')
                if scene_text and len(scene_text) >= 20:
                    vm_node_id = f'turn_{self.world_state.current_turn}'
                    self.vector_memory.add(
                        node_id=vm_node_id,
                        text=f'{player_input}\n{scene_text}',
                        metadata={'turn_number': self.world_state.current_turn,
                                  'game_time': self.world_state.current_date.isoformat()},
                    )
            except Exception as e:
                print(f'[WARN] VectorMemory.add failed: {e}')

        # 世界扩展
        if use_llm and outline.get("needs_expansion"):
            try:
                expansions = await ScriptBuilder.expand_world(
                    scene.get('scene_text', ''), outline, self.script_data
                )
                new_npcs = expansions.get('new_npcs', [])
                new_locs = expansions.get('new_locations', [])
                if new_npcs or new_locs:
                    await self._apply_world_expansion(new_npcs, new_locs)
                    print(f'[扩展] +{len(new_npcs)} NPC, +{len(new_locs)} 地点')
            except Exception as e:
                print(f'[WARN] WorldExpander failed: {e}')

    async def _apply_world_expansion(self, new_npcs: list, new_locations: list):
        """添加新的 NPC/地点，并为它们生成 visual_profile"""
        for npc in new_npcs:
            npc_id = npc.get("id", f"npc_{len(self.script_data.get('npcs', []))}")
            npc["id"] = npc_id
            # Generate visual profile for new NPC
            if not npc.get("visual_profile") and get_llm_state().get("client"):
                try:
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
                    response = await llm_call(sys_prompt, user_prompt, max_tokens=300, raise_on_error=False)
                    profile = _parse_json_from_llm(response) or {}
                    npc["visual_profile"] = profile
                except Exception:
                    npc["visual_profile"] = {}

            self.script_data.setdefault("npcs", []).append(npc)
            self.npc_agents[npc_id] = NPCAgent(npc_id, npc, self.world_state)
            self.world_state.relationships[npc_id] = npc.get("attitude_toward_player", 50)

        for loc in new_locations:
            loc_id = loc.get("id", f"loc_{len(self.script_data.get('locations', []))}")
            loc["id"] = loc_id
            # Generate visual profile for new location
            if not loc.get("visual_profile") and get_llm_state().get("client"):
                try:
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
                    response = await llm_call(sys_prompt, user_prompt, max_tokens=300, raise_on_error=False)
                    profile = _parse_json_from_llm(response) or {}
                    loc["visual_profile"] = profile
                except Exception:
                    loc["visual_profile"] = {}

            self.script_data.setdefault("locations", []).append(loc)

        if new_npcs or new_locations:
            sid = self.script_data.get("script_id", "unknown")
            path = os.path.join(SCRIPTS_DIR, f"{sid}.json")
            if os.path.exists(path):
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(self.script_data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

    def _apply_event_result(self, result) -> None:
        """应用 EventResult 到世界状态"""
        if not result:
            return
        for effect in result.effects:
            action = effect.get("action", "")
            params = effect.get("params", {})
            if action == "set_var":
                target = params.get("target", "")
                op = params.get("op", "set")
                value = params.get("value")
                if target and value is not None:
                    if self.state_manager:
                        self.state_manager.apply_changes(
                            self.world_state.event_state,
                            [{"target": target, "op": op, "value": value}], inplace=True)
                    else:
                        self.world_state.apply_changes([{"var": target, "op": op, "value": value}])
        if self.lorebook:
            for eid in result.lore_activations:
                self.lorebook.update_entry_enabled(eid, True)
            for eid in result.lore_deactivations:
                self.lorebook.update_entry_enabled(eid, False)
            if result.lore_additions:
                self.lorebook.add_entries(result.lore_additions)
            for upd in result.lore_updates:
                self.lorebook.update_entry(upd.get("id", ""), upd.get("content", ""), upd.get("keys"))
            for rid in result.lore_removals:
                self.lorebook.remove_entry(rid)
        active_states = self.world_state.event_state.setdefault("active_persistent_states", [])
        for sid in result.state_activations:
            if sid not in active_states:
                active_states.append(sid)
        for sid in result.state_deactivations:
            if sid in active_states:
                active_states.remove(sid)

    @staticmethod
    def _summarize_event_result(result) -> Dict[str, Any]:
        """将 EventResult 转为可序列化的摘要"""
        if not result:
            return {}
        return {
            "newly_active": [e.get("name", e.get("id", "")) for e in result.newly_active],
            "newly_completed": [e.get("name", e.get("id", "")) for e in result.newly_completed],
            "notifications": result.notifications,
            "inject_prompts": result.inject_prompts,
            "triggered_consequences": [c.get("description", "") for c in result.triggered_consequences],
            "imminent_warnings": result.imminent_warnings,
        }

    def _evaluate_condition(self, condition: str) -> bool:
        """条件表达式求值（供 EventEngine/TriggerEngine 使用）"""
        if not condition:
            return True
        import re as _re
        import operator as _op
        _OPS = {
            ">=": _op.ge, "<=": _op.le, ">": _op.gt, "<": _op.lt,
            "==": _op.eq, "!=": _op.ne,
        }
        m = _re.match(r"([\w.]+)\s*(>=|<=|>|<|==|!=)\s*(.+)", condition.strip())
        if not m:
            return False
        path, op_str, raw_val = m.group(1), m.group(2), m.group(3).strip()
        if self.state_manager:
            actual = self.state_manager._get_value(self.world_state.event_state, path)
        else:
            actual = self.world_state.query_info("variable", path)
        if actual is None:
            return False
        try:
            target = int(raw_val) if raw_val.lstrip("-").isdigit() else raw_val
            if isinstance(actual, (int, float)) and isinstance(target, str):
                target = float(target)
            return _OPS.get(op_str, lambda a, b: False)(actual, target)
        except (ValueError, TypeError):
            return False

    def _build_xml_section(self, tag: str, content: str) -> str:
        """构建 XML 结构化提示段"""
        if not content or not content.strip():
            return ""
        return f"<{tag}>\n{content}\n</{tag}>"

    def _build_structured_context_prompt(self, context: Dict[str, Any]) -> str:
        """将 context 组装为结构化 XML 提示词"""
        sections = []
        # world section
        world_info = []
        world_info.append(f"轮次: {self.world_state.current_turn}")
        world_info.append(f"日期: {self.world_state.current_date.isoformat()}")
        loc_id = self.world_state.current_location
        loc_name = loc_id
        for loc in self.script_data.get("locations", []):
            if loc.get("id") == loc_id:
                loc_name = loc.get("name", loc_id)
                break
        world_info.append(f"地点: {loc_name}")
        sections.append(self._build_xml_section("world", "\n".join(world_info)))
        # lorebook section
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            sections.append(self._build_xml_section("lorebook", lore))
        # events section
        event_inject = context.get("shared", {}).get("event_inject", "")
        if event_inject:
            sections.append(self._build_xml_section("events", event_inject))
        # history summary section
        summary = self.world_state.summary_state.get("history_summary", "")
        if summary:
            sections.append(self._build_xml_section("story_context", summary))
        return "\n\n".join(s for s in sections if s)

    def vector_query(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """语义检索历史（VectorMemory 可用时）"""
        if not self.vector_memory:
            return []
        try:
            return self.vector_memory.query(query, top_k=top_k)
        except Exception:
            return []

    def _build_context(self, player_input: str, dp: dict,
                       event_result, lore_text: str, event_inject: str) -> Dict[str, Any]:
        """阶段1：一次性组装分层 context dict"""
        resolved = dp.get("resolved_action", player_input)

        # 1. 世界层
        world = {
            "turn": self.world_state.current_turn,
            "date": self.world_state.current_date.isoformat(),
            "location": self.world_state.current_location,
            "environment": {k: v for k, v in self.world_state.world_props.items()
                            if k in ("weather", "lighting", "atmosphere", "season",
                                     "time_of_day", "天气", "光照", "氛围", "季节")},
        }

        # 2. 历史层
        recent_turns = self.world_state.turns[-5:]
        relationship_trends = {}
        if len(self.world_state.turns) >= 2:
            prev_s = self.world_state.turns[-2].state_after
            curr_s = self.world_state.turns[-1].state_after
            for nid in prev_s.get("relationships", {}):
                pv = prev_s["relationships"].get(nid, 0)
                cv = curr_s["relationships"].get(nid, 0)
                if pv != cv:
                    relationship_trends[nid] = f"{pv} → {cv}"
        history = {
            "recent_actions": [t.player_action for t in recent_turns],
            "relationship_trends": relationship_trends,
        }

        # 3. NPC 独立上下文
        agendas = self._check_npc_agendas()
        agenda_map = {a["npc_id"]: a["action"] for a in agendas}
        npc_contexts = {}
        for npc_id in self.npc_agents:
            nc = {}
            if self.npc_memories.get(npc_id):
                nc["memories"] = self.npc_memories[npc_id]
            if agenda_map.get(npc_id):
                nc["agenda"] = agenda_map[npc_id]
            if self.npc_plans.get(npc_id):
                nc["plan"] = self.npc_plans[npc_id]
            arc = self.npc_emotion_arcs.get(npc_id, [])
            if len(arc) >= 3:
                nc["emotion_arc"] = arc[-5:]
            rel = self._build_npc_relationship_context(npc_id)
            if rel:
                nc["relationships"] = rel
            per_npc_know = [info["fact"][:120] for info in self.information_network
                            if npc_id in info.get("known_by", [])][:5]
            if per_npc_know:
                nc["knowledge"] = per_npc_know
            if nc:
                npc_contexts[npc_id] = nc

        if agendas:
            print(f'[日程] {len(agendas)} 个NPC有议程: {", ".join(a["npc_name"] for a in agendas)}')

        # 4. 共享层
        shared = {}
        if lore_text:
            shared["lorebook"] = lore_text
        if event_inject:
            shared["event_inject"] = event_inject
        if self.faction_reputation:
            shared["faction_reputation"] = self.faction_reputation
        esummary = self._summarize_event_result(event_result) if event_result else {}
        if esummary:
            shared["event_result_summary"] = esummary

        # 5. 语义检索
        semantic = []
        if self.vector_memory:
            try:
                semantic = self.vector_memory.query(player_input, 3)
            except Exception:
                pass

        return {
            "user_action": resolved,
            "raw_input": player_input,
            "director": dp,
            "world": world,
            "history": history,
            "npc_contexts": npc_contexts,
            "shared": shared,
            "semantic_history": semantic,
        }

    def _run_npc_agents_parallel(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """阶段2：NPC并行推理（规则模式）"""
        npc_outputs = {}
        for npc_id, agent in self.npc_agents.items():
            try:
                npc_outputs[npc_id] = agent._reason_rules(context)
            except Exception as e:
                print(f"  警告：{npc_id} 推理失败: {e}")
        return npc_outputs

    async def _run_npc_agents_llm(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """阶段2：NPC 两轮推理（含 Director 自适应路由策略）"""
        skip = set(context.get("director", {}).get("skip_npcs") or [])
        strategies = context.get("director", {}).get("npc_strategies", {})

        # 合并 skip_npcs 和 minimal 策略
        for npc_id, strat in strategies.items():
            if strat == "minimal":
                skip.add(npc_id)

        supportive_ids = {npc_id for npc_id, strat in strategies.items() if strat == "supportive"}

        active_agents = {npc_id: agent for npc_id, agent in self.npc_agents.items() if npc_id not in skip}
        npc_outputs = {}
        for npc_id in skip:
            agent = self.npc_agents.get(npc_id)
            if agent:
                npc_outputs[npc_id] = {
                    "name": agent.npc_data.get("name", npc_id),
                    "action_type": "无关",
                    "emotion": "平静",
                    "dialogue": None,
                    "skipped": True,
                }

        if not active_agents:
            return npc_outputs

        # --- 第一轮：并行收集意图 ---
        async def _get_intent(npc_id, agent):
            try:
                return await agent._reason_intent(context)
            except Exception as e:
                print(f"  警告：{npc_id} 意图推理失败: {e}")
                return {"npc_id": npc_id, "name": agent.name, "action_type": "观望", "stance": ""}

        intent_tasks = [_get_intent(npc_id, agent) for npc_id, agent in active_agents.items()]
        intent_results = await asyncio.gather(*intent_tasks)
        intent_context = list(intent_results)

        active_intents = [i for i in intent_context if i["action_type"] != "观望"]
        if active_intents:
            names = ", ".join(i["name"] for i in active_intents)
            print(f'  [意图] {len(active_intents)} 个NPC有行动意图: {names}')

        # --- 动态策略提升：supportive NPC 显示强烈情绪时升级为 aggressive ---
        _ESCALATE_TYPES = {"冲突", "警告", "主动联系", "主动谈判"}
        promoted = set()
        for intent in intent_context:
            npc_id = intent.get("npc_id", "")
            if npc_id in supportive_ids and intent.get("action_type") in _ESCALATE_TYPES:
                promoted.add(npc_id)
        if promoted:
            supportive_ids -= promoted
            print(f'  [策略提升] {len(promoted)} 个NPC从supportive升级为aggressive: {", ".join(promoted)}')

        # --- 第二轮：aggressive 走完整推理，supportive 用意图结果直接构建精简输出 ---
        aggressive_agents = {npc_id: agent for npc_id, agent in active_agents.items() if npc_id not in supportive_ids}
        supportive_agents = {npc_id: agent for npc_id, agent in active_agents.items() if npc_id in supportive_ids}

        # supportive NPC 直接用第一轮意图结果构建输出（跳过第二轮 LLM）
        for npc_id, agent in supportive_agents.items():
            intent = next((i for i in intent_context if i.get("npc_id") == npc_id), None)
            npc_outputs[npc_id] = {
                "agent_id": npc_id,
                "name": agent.name,
                "action_type": intent.get("action_type", "观望") if intent else "观望",
                "emotion": "平静",
                "dialogue": None,
                "thought": intent.get("stance", "") if intent else "",
                "tool_calls": [],
                "strategy": "supportive",
            }

        if supportive_agents:
            print(f'  [路由] {len(supportive_agents)} 个NPC使用精简推理: {", ".join(a.name for a in supportive_agents.values())}')

        # aggressive NPC 完整第二轮推理
        async def _reason_full(npc_id, agent):
            try:
                return npc_id, await agent._reason_llm(context, session=self,
                                                       intent_context=intent_context, strategy="aggressive")
            except Exception as e:
                print(f"  警告：{npc_id} LLM推理失败: {e}")
                return npc_id, agent._reason_rules(context)

        if aggressive_agents:
            full_tasks = [_reason_full(npc_id, agent) for npc_id, agent in aggressive_agents.items()]
            full_results = await asyncio.gather(*full_tasks)
            for npc_id, output in full_results:
                npc_outputs[npc_id] = output
        return npc_outputs

    # ===== Pacing 系统 =====

    def _compute_tension(self, action: str, npc_outputs: Dict[str, Any], outline: Dict[str, Any]) -> int:
        """计算当前 tension（0-100），指导叙事节奏"""
        tension = self.tension

        conflicts = outline.get("conflicts", [])
        for c in conflicts:
            sev = c.get("severity", "low")
            tension += {"high": 20, "medium": 10, "low": 5}.get(sev, 5)

        active_actions = sum(1 for o in npc_outputs.values() if o.get("action_type") not in ("观望", "无关"))
        tension += active_actions * 3

        if any(kw in action for kw in ("攻击", "战斗", "逃跑", "危险", "追赶", "偷")):
            tension += 15
        elif any(kw in action for kw in ("休息", "睡觉", "闲聊", "散步")):
            tension = max(0, tension - 15)

        # 事件驱动 tension
        event_summary = outline.get("event_result_summary") or {}
        if event_summary.get("newly_active"):
            tension += 10
        if event_summary.get("triggered_consequences"):
            tension += 5 * len(event_summary["triggered_consequences"])

        # OutlineAgent 手动调节
        tension += self._pending_tension_adjustment
        self._pending_tension_adjustment = 0

        tension = max(0, min(100, tension))

        # Tension lock
        has_active_events = bool(
            self.world_state.event_state.get("active_persistent_states")
            or event_summary.get("newly_active")
        )
        if not has_active_events:
            if tension < self._tension_peak - 20 and self._tension_peak >= 50:
                self._tension_afterglow = 3
                self._tension_peak = 0
            if self._tension_afterglow > 0:
                tension_decay = max(1, int(tension * 0.05))
                self._tension_afterglow -= 1
            else:
                tension_decay = max(1, int(tension * 0.1))
            self.tension = max(0, tension - tension_decay)
        else:
            self.tension = tension
        self._tension_peak = max(self._tension_peak, tension)
        return tension

    def _pacing_hint(self, tension: int) -> str:
        """生成 pacing 提示"""
        if tension >= 80:
            return "节奏：高度紧张。叙事应短促有力，使用快节奏描写，增加紧迫感和悬念。"
        elif tension >= 50:
            return "节奏：中度紧张。叙事应在紧张和舒缓之间平衡，适当铺垫但不拖沓。"
        elif tension >= 20:
            return "节奏：舒缓。叙事可以更加细腻，关注角色情感和环境描写。"
        else:
            return "节奏：宁静。叙事应轻松自然，可以展现日常场景和角色的日常面。"

    def _resolve_skill_check(self, attr_name: str, difficulty: str = "medium") -> Dict[str, Any]:
        """执行技能检定"""
        import random
        dc = {"easy": 8, "medium": 12, "hard": 16, "extreme": 20}.get(difficulty, 12)
        player = self.script_data.get("player_character", {})
        attrs = player.get("attributes", {})
        attr_val = 50
        matched_name = attr_name
        for k, v in attrs.items():
            if attr_name in k or k in attr_name:
                attr_val = v if isinstance(v, (int, float)) else (v.get("value", 50) if isinstance(v, dict) else 50)
                matched_name = k
                break
        bonus = (attr_val - 10) // 2 if isinstance(attr_val, (int, float)) else 0
        roll = random.randint(1, 20)
        total = roll + bonus
        success = total >= dc
        outcome = "critical_success" if roll == 20 else "critical_failure" if roll == 1 else "success" if success else "failure"
        return {
            "attribute": matched_name,
            "difficulty": difficulty,
            "dc": dc,
            "roll": roll,
            "bonus": bonus,
            "total": total,
            "outcome": outcome,
            "description": f"「{matched_name}」检定: d20({roll})+{bonus}={total} vs DC{dc} → {'成功' if success else '失败'}",
        }

    _HOSTILE_ACTIONS = {"欺骗", "偷窃", "暗算", "背叛", "陷阱", "威胁", "施法", "攻击"}
    _PASSIVE_ATTR_MAP = {
        "欺骗": "洞察", "偷窃": "感知", "暗算": "感知", "背叛": "洞察",
        "陷阱": "感知", "威胁": "意志", "施法": "感知", "攻击": "敏捷",
    }

    def _check_passive_skills(self, npc_outputs: Dict[str, Any]) -> list:
        """NPC 对抗性行动时自动触发玩家被动检定"""
        checks = []
        for npc_id, output in npc_outputs.items():
            action_type = output.get("action_type", "")
            if action_type not in self._HOSTILE_ACTIONS:
                continue
            attr = self._PASSIVE_ATTR_MAP.get(action_type, "感知")
            result = self._resolve_skill_check(attr, "medium")
            checks.append({
                "trigger_npc": output.get("name", npc_id),
                "action_type": action_type,
                "attribute": attr,
                "result": result,
            })
        return checks

    async def _npc_dialogue_round(self, npc_a_id: str, npc_b_id: str, topic: str, context: Dict[str, Any]) -> Optional[Dict]:
        """高冲突场景下两个NPC之间的一轮对话"""
        agent_a = self.npc_agents.get(npc_a_id)
        agent_b = self.npc_agents.get(npc_b_id)
        if not agent_a or not agent_b:
            return None
        name_a = agent_a.npc_data.get("name", npc_a_id)
        name_b = agent_b.npc_data.get("name", npc_b_id)
        system_prompt = f"你在模拟两个NPC之间的紧张对话。话题：{topic}。输出格式：\n{name_a}: (对白)\n{name_b}: (对白)\n用2-3轮对话表现冲突。保持简洁，总计不超过150字。"
        user_prompt = f"{name_a}（{agent_a.npc_data.get('personality', '')}）和{name_b}（{agent_b.npc_data.get('personality', '')}）就「{topic}」发生了争执。"
        try:
            dialogue_text = await llm_call(system_prompt, user_prompt, max_tokens=600)
            if dialogue_text:
                return {"npc_a": npc_a_id, "npc_b": npc_b_id, "npc_a_name": name_a, "npc_b_name": name_b, "topic": topic, "dialogue": dialogue_text}
        except Exception:
            pass
        return None

    def _check_npc_agendas(self) -> list:
        """检查 NPC 日程"""
        triggered = []
        turn = self.world_state.current_turn
        hour = self.world_state.current_date.hour

        for npc_def in self.script_data.get("npcs", []):
            npc_id = npc_def.get("id", "")
            for agenda in npc_def.get("agenda", []):
                trigger = agenda.get("trigger", "")
                condition = agenda.get("condition", "")
                fired = False
                if trigger.startswith("turn"):
                    try:
                        op = ">=" if ">=" in trigger else "==" if "==" in trigger else ">"
                        val = int(trigger.split(op)[-1].strip())
                        fired = (op == ">=" and turn >= val) or (op == "==" and turn == val) or (op == ">" and turn > val)
                    except (ValueError, IndexError):
                        pass
                elif trigger.startswith("hour"):
                    try:
                        op = ">=" if ">=" in trigger else "==" if "==" in trigger else ">"
                        val = int(trigger.split(op)[-1].strip())
                        fired = (op == ">=" and hour >= val) or (op == "==" and hour == val) or (op == ">" and hour > val)
                    except (ValueError, IndexError):
                        pass
                elif trigger.startswith("every_") and trigger.endswith("_turns"):
                    try:
                        interval = int(trigger[6:-6])
                        fired = interval > 0 and turn % interval == 0
                    except (ValueError, IndexError):
                        pass
                elif trigger == "always":
                    fired = True
                else:
                    fired = self._evaluate_condition(trigger)

                if fired and condition:
                    fired = self._evaluate_condition(condition)

                if fired and not agenda.get("_done"):
                    triggered.append({
                        "npc_id": npc_id,
                        "npc_name": npc_def.get("name", npc_id),
                        "action": agenda.get("action", ""),
                        "priority": agenda.get("priority", "low"),
                    })
                    if not agenda.get("repeatable"):
                        agenda["_done"] = True
        return triggered

    # ===== NPC 间关系网络 =====

    @staticmethod
    def _normalize_rel_key(a: str, b: str) -> str:
        return f"{min(a, b)}_{max(a, b)}"

    def _apply_npc_relationship_updates(self, updates: list):
        for upd in updates:
            a = upd.get("from_npc") or upd.get("a", "")
            b = upd.get("to_npc") or upd.get("b", "")
            if not a or not b:
                continue
            key = self._normalize_rel_key(a, b)
            existing = self.npc_relationships_global.get(key)
            entry = {
                "a": a, "b": b,
                "type": upd.get("rel_type", existing.get("type", "中立") if existing else "中立"),
                "description": upd.get("description", existing.get("description", "") if existing else ""),
                "_turn": self.world_state.current_turn,
            }
            self.npc_relationships_global[key] = entry
            self.npc_relationships_known[key] = entry
        # 淘汰
        for net in (self.npc_relationships_global, self.npc_relationships_known):
            if len(net) <= self._REL_NET_MAX:
                continue
            items = sorted(net.items(), key=lambda x: x[1].get("_turn", 0))
            to_remove = len(net) - self._REL_NET_MAX
            for k, _ in items[:to_remove]:
                net.pop(k, None)

    def _build_npc_relationship_context(self, npc_id: str) -> str:
        """为指定 NPC 构建其与其他 NPC 的关系上下文"""
        lines = []
        dn = {}
        for n in self.script_data.get("npcs", []):
            dn[n.get("id", "")] = n.get("name", n.get("id", ""))
        for _key, rel in self.npc_relationships_known.items():
            a, b = rel.get("a", ""), rel.get("b", "")
            if npc_id not in (a, b):
                continue
            other = b if a == npc_id else a
            other_name = dn.get(other, other)
            desc = rel.get("description", "")
            lines.append(f"- {other_name}：{rel.get('type', '中立')}" + (f"（{desc[:30]}）" if desc else ""))
        if not lines:
            return ""
        return "## 你与其他角色的关系\n\n" + "\n".join(lines[:6])

    # ===== 信息不对称系统 =====

    def _propagate_information(self):
        network = self.information_network
        if not network:
            return
        import random
        for info in network:
            known = set(info.get("known_by", []))
            if len(known) >= info.get("max_spread", 5):
                continue
            new_knowers = set()
            for knower in list(known):
                for _rk, rel in self.npc_relationships_global.items():
                    a, b = rel.get("a", ""), rel.get("b", "")
                    partner = ""
                    if a == knower:
                        partner = b
                    elif b == knower:
                        partner = a
                    if partner and partner not in known and partner not in new_knowers:
                        if random.random() < info.get("spread_chance", 0.3):
                            new_knowers.add(partner)
                if len(known) + len(new_knowers) >= info.get("max_spread", 5):
                    break
            for nk in new_knowers:
                info["known_by"].append(nk)
                if random.random() < 0.3:
                    info["distortion"] = min(3, info.get("distortion", 0) + 1)

    def _build_npc_knowledge_context(self, present_npc_ids: list) -> str:
        network = self.information_network
        if not network or not present_npc_ids:
            return ""
        _DISTORTION_LABELS = {0: "", 1: "（略有偏差）", 2: "（严重失真）", 3: "（面目全非）"}
        present_set = set(present_npc_ids)
        npc_facts: dict = {}
        for info in network:
            known_by = set(info.get("known_by", []))
            overlapping = present_set & known_by
            if not overlapping:
                continue
            fact = info.get("fact", "")
            if not fact:
                continue
            distortion = min(info.get("distortion", 0), 3)
            label = _DISTORTION_LABELS.get(distortion, "")
            for npc_id in overlapping:
                npc_facts.setdefault(npc_id, []).append(f"{fact[:60]}{label}")
        if not npc_facts:
            return ""
        dn = {n.get("id", ""): n.get("name", n.get("id", "")) for n in self.script_data.get("npcs", [])}
        lines = ["## NPC 认知差异（各NPC只知道各自的信息，对话/反应须反映认知差异）"]
        for npc_id, facts in npc_facts.items():
            name = dn.get(npc_id, npc_id)
            lines.append(f"- {name}知道: {'; '.join(facts[:3])}")
        return "\n".join(lines)

    # ===== 声望系统 =====

    @staticmethod
    def _reputation_title(value: int) -> str:
        if value >= 90: return "崇拜"
        if value >= 70: return "友好"
        if value >= 50: return "中立"
        if value >= 30: return "冷淡"
        if value >= 10: return "敌对"
        return "通缉"

    def _check_npc_interventions(self) -> list:
        """检查 NPC 是否主动干预"""
        interventions = []
        cooldowns = self.npc_intervention_cooldowns
        player_loc = self.world_state.current_location
        turn = self.world_state.current_turn

        for npc_def in self.script_data.get("npcs", []):
            if len(interventions) >= 2:
                break
            npc_id = npc_def.get("id", "")
            if not npc_id or turn < cooldowns.get(npc_id, 0):
                continue

            npc_loc = npc_def.get("default_location", "")
            is_present = npc_loc == player_loc
            if not is_present:
                connections = []
                for loc in self.script_data.get("locations", []):
                    if loc.get("id") == player_loc:
                        connections = loc.get("connections", [])
                        break
                is_nearby = npc_loc in connections
            else:
                is_nearby = False

            if not is_present and not is_nearby:
                continue

            attitude = self.world_state.relationships.get(npc_id, 50)
            capabilities = npc_def.get("capabilities", "")

            if attitude < 25 and any(k in capabilities for k in ("战斗", "攻击", "武力", "守卫", "combat")):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_def.get("name", npc_id),
                    "type": "confrontation",
                    "urgency": "high",
                    "reason": f"对你怀有敌意（好感{attitude}）",
                    "suggested_action": f"{npc_def.get('name', npc_id)}拦住你的去路",
                })
                cooldowns[npc_id] = turn + 5
                continue

            if attitude > 80 and is_present:
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_def.get("name", npc_id),
                    "type": "aid",
                    "urgency": "medium",
                    "reason": "关系亲密，主动提供帮助",
                    "suggested_action": f"{npc_def.get('name', npc_id)}主动上前和你打招呼",
                })
                cooldowns[npc_id] = turn + 5
                continue

        return interventions

    @staticmethod
    def _detect_plan_declaration(action_text: str) -> bool:
        _PLAN_KEYWORDS = ("计划", "打算", "准备", "预谋", "我的计划是", "策划", "筹划")
        return any(kw in action_text for kw in _PLAN_KEYWORDS)

    # ===== NPC 独立对话流程 =====

    async def talk_to_npc(self, npc_id: str, message: str) -> Dict[str, Any]:
        """与 NPC 进行独立对话（不推进主线）"""
        npc_data = next((n for n in self.script_data.get("npcs", []) if n.get("id") == npc_id), None)
        if not npc_data:
            return {"error": f"NPC not found: {npc_id}"}

        npc_name = npc_data.get("name", npc_id)
        rel_value = self.world_state.relationships.get(npc_id, 50)

        history = self.npc_chat_history.get(npc_id, [])

        system_prompt = f"""你现在扮演 {npc_name}（{npc_data.get('title', '')}），与玩家进行自然对话。

## 角色信息
- 性格：{npc_data.get('personality', '')}
- 背景：{npc_data.get('bio', '')}
- 与玩家关系值：{rel_value}/100
- 当前游戏时间：{self.world_state.current_date.isoformat()}
- 当前地点：{self.world_state.current_location}

## 对话规则
1. 严格以 {npc_name} 的口吻和性格回应
2. 对话长度 50-150 字
3. 可以自然推进关系，但单轮关系变化不超过 ±5
4. 如果包含关系变化，在回复末尾添加 ```npc_talk {{"npc_attitude_changes": [{{"npc_id": "{npc_id}", "dimension": "value", "change": 数值, "reason": "原因"}}]}} ```
5. 使用中文弯引号包裹对话"""

        messages = []
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-6:]:
            if isinstance(h, dict) and h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h.get("player", "")})
            messages.append({"role": "assistant", "content": h.get("npc", "")})
        messages.append({"role": "user", "content": message})

        raw = await llm_call(system_prompt, message if not messages[:-1] else "")
        if messages[:-1]:
            client = get_llm_state().get("client")
            model = get_llm_state().get("model", "")
            if client:
                try:
                    all_msgs = [{"role": "system", "content": system_prompt}] + messages
                    resp = await client.chat.completions.create(
                        model=model, messages=all_msgs, max_tokens=1000, temperature=0.85
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                except Exception:
                    pass

        att_changes = []
        match = re.search(r'```npc_talk\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                att_changes = parsed.get("npc_attitude_changes", [])
            except (json.JSONDecodeError, KeyError):
                pass

        clean_response = re.sub(r'```npc_talk\s*\{.*?\}\s*```', '', raw, flags=re.DOTALL).strip()

        rel_before = self.world_state.relationships.get(npc_id, 50)
        for ac in att_changes:
            delta = ac.get("change", 0)
            delta = max(-5, min(5, delta))
            target_npc = ac.get("npc_id", npc_id)
            self.world_state.relationships[target_npc] = self.world_state.relationships.get(target_npc, 50) + delta
        rel_after = self.world_state.relationships.get(npc_id, 50)

        crossings = []
        for th in (20, 40, 60, 80):
            if rel_before < th <= rel_after:
                crossings.append({"threshold": th, "direction": "up", "from": rel_before, "to": rel_after})
            elif rel_before >= th > rel_after:
                crossings.append({"threshold": th, "direction": "down", "from": rel_before, "to": rel_after})

        history.append({"player": message, "npc": clean_response})
        if len(history) > 20:
            overflow = history[:-6]
            kept = history[-6:]
            summary_parts = [f"玩家:{h.get('player', '')[:30]}→NPC:{h.get('npc', '')[:50]}" for h in overflow[-5:] if isinstance(h, dict) and not h.get("_summary")]
            existing_summary = history[0].get("_summary", "") if history and isinstance(history[0], dict) and history[0].get("_summary") else ""
            new_summary = existing_summary + "; ".join(summary_parts)
            if len(new_summary) > 500:
                new_summary = new_summary[-500:]
            history = [{"_summary": new_summary}] + kept
        self.npc_chat_history[npc_id] = history

        if self.world_tree:
            try:
                self.world_tree.add_node(
                    parent_id=self.world_tree.active_node_id,
                    game_time=self.world_state.current_date.isoformat(),
                    turn_number=self.world_state.current_turn,
                    player_action={"type": "npc_talk", "npc_id": npc_id, "text": message},
                    ai_response=clean_response,
                    state_snapshot=self.world_state.get_snapshot(),
                )
            except Exception:
                pass

        self.world_state.current_date += timedelta(minutes=5)

        return {
            "npc_id": npc_id,
            "npc_name": npc_name,
            "response": clean_response,
            "attitude_changes": att_changes,
            "relationship": self.world_state.relationships.get(npc_id, 50),
            "dialogue_count": len([h for h in history if isinstance(h, dict) and not h.get("_summary")]),
            "relationship_crossings": crossings,
        }

    def get_history_summary(self, num_turns: int = 5) -> str:
        """获取历史摘要"""
        turns = self.world_state.turns[-num_turns:]
        summary = f"最近{len(turns)}轮推演:\n"
        for turn in turns:
            summary += f"  T{turn.turn_num}: {turn.player_action}\n"
        return summary

    def save_checkpoint(self, name: str = None) -> int:
        """保存检查点到数据库，返回存档 id"""
        if not name:
            name = f"轮次{self.world_state.current_turn} 自动存档"

        checkpoint = {
            "world_state": self.world_state.get_snapshot(),
            "opening_text": self.opening_text,
            "turns": [asdict(turn) for turn in self.world_state.turns],
            "npc_memories": self.npc_memories,
            "npc_relationships_global": self.npc_relationships_global,
            "npc_relationships_known": self.npc_relationships_known,
            "information_network": self.information_network,
            "faction_reputation": self.faction_reputation,
            "event_state": self.world_state.event_state,
            "lorebook_timed_state": self.world_state.lorebook_timed_state,
            "summary_state": getattr(self.world_state, 'summary_state', {}),
            "npc_chat_history": getattr(self, 'npc_chat_history', {}),
            "npc_plans": getattr(self, 'npc_plans', {}),
            "npc_emotion_arcs": getattr(self, 'npc_emotion_arcs', {}),
            "pending_turn": asdict(self._pending_turn["turn"]) if self._pending_turn else None,
            "pacing": {
                "tension": self.tension,
                "peak": self._tension_peak,
                "afterglow": self._tension_afterglow,
                "pending_adjustment": self._pending_tension_adjustment,
            },
            "npc_intervention_cooldowns": self.npc_intervention_cooldowns,
        }

        script_id = self.script_data.get("script_id", "unknown")
        turn = self.world_state.current_turn
        game_time = self.world_state.current_date.isoformat()
        data_json = json.dumps(checkpoint, ensure_ascii=False)

        conn = sqlite3.connect(database.DB_PATH)
        cur = conn.execute(
            "INSERT INTO rpg_saves (script_id, name, turn, game_time, data) VALUES (?, ?, ?, ?, ?)",
            (script_id, name, turn, game_time, data_json),
        )
        save_id = cur.lastrowid
        conn.commit()
        conn.close()

        print(f"[OK] 存档已保存: #{save_id} ({name})")
        return save_id

    def restore_checkpoint(self, checkpoint: dict) -> None:
        self.opening_text = checkpoint.get("opening_text", self.opening_text)
        ws = checkpoint.get("world_state", {})
        self.world_state.current_turn = ws.get("turn", 0)
        self.world_state.current_location = ws.get("location_id", ws.get("location", ""))
        if ws.get("date"):
            try:
                self.world_state.current_date = datetime.fromisoformat(ws["date"])
            except Exception:
                pass
        self.world_state.player_attrs = ws.get("player_attrs", {})
        self.world_state.relationships = ws.get("relationships", {})
        self.world_state.variables = ws.get("variables", {})
        self.world_state.world_props = ws.get("world_props", {})
        self.npc_memories = checkpoint.get("npc_memories", {})
        self.npc_relationships_global = checkpoint.get("npc_relationships_global", {})
        self.npc_relationships_known = checkpoint.get("npc_relationships_known", {})
        self.information_network = checkpoint.get("information_network", [])
        self.faction_reputation = checkpoint.get("faction_reputation", {})

        # 恢复 turns 列表
        turns_data = checkpoint.get("turns", [])
        self.world_state.turns = []
        for td in turns_data:
            try:
                self.world_state.turns.append(Turn(**td))
            except Exception:
                pass

        # 恢复补充字段
        self.world_state.event_state = ws.get("event_state", checkpoint.get("event_state", {}))
        self.world_state.lorebook_timed_state = checkpoint.get("lorebook_timed_state", {})
        if hasattr(self.world_state, 'summary_state'):
            self.world_state.summary_state = checkpoint.get("summary_state", {})
        self.npc_chat_history = checkpoint.get("npc_chat_history", {})
        self.npc_plans = checkpoint.get("npc_plans", {})
        self.npc_emotion_arcs = checkpoint.get("npc_emotion_arcs", {})
        pacing = checkpoint.get("pacing", {})
        self.tension = pacing.get("tension", 0)
        self._tension_peak = pacing.get("peak", 0)
        self._tension_afterglow = pacing.get("afterglow", 0)
        self._pending_tension_adjustment = pacing.get("pending_adjustment", 0)
        self.npc_intervention_cooldowns = checkpoint.get("npc_intervention_cooldowns", {})
        pending_data = checkpoint.get("pending_turn")
        self._pending_turn = None
        if isinstance(pending_data, dict):
            try:
                pending_turn = Turn(**pending_data)
                self._pending_turn = {
                    "turn": pending_turn,
                    "scene": pending_turn.scene_output,
                    "choices": pending_turn.scene_output.get("choices", []),
                }
            except (TypeError, AttributeError):
                pass

    def load_checkpoint(self, save_id: int) -> bool:
        """从数据库加载检查点"""
        conn = sqlite3.connect(database.DB_PATH)
        row = conn.execute("SELECT data FROM rpg_saves WHERE id = ?", (save_id,)).fetchone()
        conn.close()
        if not row:
            print(f"[FAIL] 存档不存在: #{save_id}")
            return False

        self.restore_checkpoint(json.loads(row[0]))

        print(f"[OK] 存档已加载: #{save_id}")
        return True

    # ===== pending 模式方法 =====

    async def process_turn_preview(self, player_input: str, director_plan: dict = None) -> Dict[str, Any]:
        """推演但不应用状态变更，返回预览"""
        result = await self.process_turn(player_input, director_plan=director_plan)
        self._pending_turn = result
        return self.get_pending_preview()

    def get_pending_preview(self) -> Optional[Dict[str, Any]]:
        if not self._pending_turn:
            return None
        turn = self._pending_turn["turn"]
        return {
            "pending": True,
            "turn_num": turn.turn_num,
            "player_action": turn.player_action,
            "scene": self._pending_turn["scene"],
            "npc_outputs": turn.npc_outputs,
            "outline": turn.outline_summary,
            "state_changes": turn.state_after,
        }

    async def retry_npc(self, npc_id: str, user_hint: str = "") -> Dict[str, Any]:
        """重推某个 NPC"""
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果")

        context = self._pending_turn["turn"].context_object
        agent = self.npc_agents.get(npc_id)
        if not agent:
            raise ValueError(f"NPC不存在: {npc_id}")

        modified_context = dict(context)
        modified_context["user_hint"] = user_hint

        use_llm = get_llm_state().get("client") is not None
        if use_llm:
            new_output = await agent._reason_llm(modified_context)
        else:
            new_output = agent._reason_rules(modified_context)
        self._pending_turn["turn"].npc_outputs[npc_id] = new_output

        if use_llm:
            outline = await OutlineAgent.synthesize_llm(context, self._pending_turn["turn"].npc_outputs, self.world_state, session=self)
            scene = await SceneAgent.generate_llm(context, self._pending_turn["turn"].npc_outputs, self.world_state, outline, session=self)
        else:
            outline = OutlineAgent.synthesize_rules(context, self._pending_turn["turn"].npc_outputs, self.world_state)
            scene = SceneAgent.generate_rules(context, self._pending_turn["turn"].npc_outputs, self.world_state, outline)

        self._pending_turn["turn"].outline_summary = outline
        self._pending_turn["turn"].scene_output = scene

        return {
            "pending": True,
            "retried_npc": npc_id,
            "npc_output": new_output,
            "outline": outline,
            "scene": scene,
        }

    # ===== 工具执行器（表驱动） =====

    def _exec_modify_relationship(self, result: dict, source_npc: str):
        npc_id = result.get("npc_id")
        delta = result.get("delta", 0)
        if npc_id:
            self.world_state.relationships[npc_id] = self.world_state.relationships.get(npc_id, 0) + delta

    def _exec_apply_state_change(self, result: dict, source_npc: str):
        self.world_state.apply_changes([{
            "var": result.get("var"), "op": result.get("op", "set"), "value": result.get("value"),
        }])

    def _exec_move_player(self, result: dict, source_npc: str):
        loc = result.get("location_id")
        if loc:
            self.world_state.current_location = loc

    def _exec_set_world_prop(self, result: dict, source_npc: str):
        key = result.get("key")
        if key:
            self.world_state.world_props[key] = result.get("value")

    def _exec_give_item(self, result: dict, source_npc: str):
        if "inventory" not in self.world_state.variables:
            self.world_state.variables["inventory"] = []
        self.world_state.variables["inventory"].append({
            "name": result.get("item_name"), "desc": result.get("description", ""),
        })

    def _exec_take_item(self, result: dict, source_npc: str):
        if "inventory" in self.world_state.variables:
            self.world_state.variables["inventory"] = [
                i for i in self.world_state.variables["inventory"]
                if i.get("name") != result.get("item_name")
            ]

    def _exec_spawn_npc(self, result: dict, source_npc: str):
        npc_id = f"dynamic_npc_{len(self.script_data.get('npcs', []))}"
        new_npc = {
            "id": npc_id, "name": result.get("name"), "title": result.get("title"),
            "personality": result.get("personality"),
            "default_location": result.get("location_id"), "attitude_toward_player": 50,
        }
        self.script_data.setdefault("npcs", []).append(new_npc)
        self.npc_agents[npc_id] = NPCAgent(npc_id, new_npc, self.world_state)
        self.world_state.relationships[npc_id] = 50

    def _exec_remove_npc(self, result: dict, source_npc: str):
        npc_id = result.get("npc_id")
        if npc_id:
            self.script_data["npcs"] = [n for n in self.script_data.get("npcs", []) if n.get("id") != npc_id]
            self.npc_agents.pop(npc_id, None)
            self.world_state.relationships.pop(npc_id, None)

    def _exec_update_npc_relationship(self, result: dict, source_npc: str):
        self._apply_npc_relationship_updates([result])

    def _exec_share_information(self, result: dict, source_npc: str):
        fact = result.get("fact", "")
        known_by = result.get("known_by", "")
        if fact:
            self.information_network.append({
                "origin_turn": self.world_state.current_turn,
                "fact": fact, "known_by": [known_by] if known_by else [],
                "spread_chance": 0.3, "distortion": 0, "max_spread": 5,
            })
            if len(self.information_network) > 30:
                self.information_network = self.information_network[-30:]

    def _exec_update_plan(self, result: dict, source_npc: str):
        goal = result.get("goal", "")
        if goal and source_npc:
            self.npc_plans[source_npc] = {
                "goal": goal, "next_step": result.get("next_step", ""),
                "progress": result.get("progress", ""),
                "updated_turn": self.world_state.current_turn,
            }

    def _exec_change_faction_reputation(self, result: dict, source_npc: str):
        fid = result.get("faction_id", "")
        if fid:
            delta = result.get("delta", 0)
            entry = self.faction_reputation.setdefault(fid, {"value": 50})
            entry["value"] = max(0, min(100, entry.get("value", 50) + delta))
            entry["title"] = self._reputation_title(entry["value"])
            if result.get("reason"):
                entry["last_reason"] = result["reason"]

    def _exec_trigger_event(self, result: dict, source_npc: str):
        name = result.get("event_name", "")
        desc = result.get("description", "")
        if name and self.event_engine:
            evt = {"id": f"dynamic_{name}_{self.world_state.current_turn}",
                   "name": name, "description": desc}
            self.event_engine.inject_dynamic_event(evt, "one_time")
            print(f'  [事件] Director 触发: {name}')

    def confirm_turn(self) -> Dict[str, Any]:
        """确认待推演结果，应用所有状态变更"""
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果")

        turn = self._pending_turn["turn"]

        # 收集所有 agent tool_calls
        all_tool_calls = []
        dp = turn.context_object.get("director", {})
        for tc in dp.get("tool_calls", []):
            all_tool_calls.append(tc)
        for npc_id, npc_out in turn.npc_outputs.items():
            for tc in npc_out.get("tool_calls", []):
                tc["_source_npc"] = npc_id
                all_tool_calls.append(tc)
        for tc in turn.outline_summary.get("tool_calls", []):
            all_tool_calls.append(tc)

        # 表驱动执行
        for tc in all_tool_calls:
            result = tc.get("result", {})
            tc_type = result.get("type", "")
            source_npc = tc.get("_source_npc", "")
            handler = self._tool_handlers.get(tc_type)
            if handler:
                handler(result, source_npc)

        for dice_result in turn.scene_output.get('dice_results', []):
            self.world_state.apply_changes([
                {'var': change['target'], 'op': change.get('op', 'add'), 'value': change.get('value', 0)}
                for change in dice_result.get('state_changes', []) if change.get('target')
            ])

        if all_tool_calls:
            print(f'[Tool] 执行了 {len(all_tool_calls)} 个工具提议')

        turn.state_after = self.world_state.get_snapshot()
        self.world_state.turns.append(turn)

        # 更新 NPC 跨回合记忆
        NPC_MEMORY_MAX = 5
        for npc_id, npc_out in turn.npc_outputs.items():
            if npc_out.get("skipped") or npc_out.get("action_type") in ("观望", "无关"):
                continue
            entry = f"T{turn.turn_num}: {npc_out.get('action_type', '')}。"
            if npc_out.get("dialogue"):
                d = npc_out["dialogue"]
                entry += f"说：「{d[:30]}{'...' if len(d) > 30 else ''}」"
            elif npc_out.get("thought"):
                entry += npc_out["thought"][:30]
            mem = self.npc_memories.setdefault(npc_id, [])
            mem.append(entry)
            if len(mem) > NPC_MEMORY_MAX:
                self.npc_memories[npc_id] = mem[-NPC_MEMORY_MAX:]

        # 更新 NPC 情感弧线
        EMOTION_ARC_MAX = 8
        for npc_id, npc_out in turn.npc_outputs.items():
            emotion = npc_out.get("emotion")
            if emotion and not npc_out.get("skipped"):
                arc = self.npc_emotion_arcs.setdefault(npc_id, [])
                arc.append(emotion)
                if len(arc) > EMOTION_ARC_MAX:
                    self.npc_emotion_arcs[npc_id] = arc[-EMOTION_ARC_MAX:]

        # 信息传播
        self._propagate_information()

        result = self._pending_turn
        self._pending_turn = None

        return {
            "confirmed": True,
            "turn_num": turn.turn_num,
            "state_after": turn.state_after,
            "choices": turn.player_choices,
        }

    async def regenerate_scene(self, hint: str = "") -> Dict[str, Any]:
        """重新生成当前轮的场景描写（Swipe 机制）"""
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果可以重生成")

        turn = self._pending_turn["turn"]
        context = turn.context_object
        npc_outputs = turn.npc_outputs

        if hint:
            context = dict(context)
            context["regenerate_hint"] = hint

        use_llm = get_llm_state().get("client") is not None
        if not use_llm:
            return {"error": "重生成需要 AI 连接"}

        outline = turn.outline_summary
        scene = await SceneAgent.generate_llm(context, npc_outputs, self.world_state, outline, session=self)
        scene["tension"] = context.get("tension", 0)
        scene["npc_interventions"] = context.get("npc_interventions", [])

        if self.world_tree:
            node = self.world_tree.get_node(self.world_tree.active_node_id)
            if node:
                swipes = node.setdefault("swipes", [])
                if not swipes:
                    swipes.append({
                        "narrative": turn.scene_output.get("scene_text", ""),
                        "choices": turn.scene_output.get("choices", []),
                    })
                swipes.append({
                    "narrative": scene.get("scene_text", ""),
                    "choices": scene.get("choices", []),
                })
                node["active_swipe_index"] = len(swipes) - 1

        turn.scene_output = scene
        self._pending_turn["scene"] = scene

        return {
            "scene": scene,
            "swipe_index": len(self.world_tree.get_node(self.world_tree.active_node_id).get("swipes", [])) - 1 if self.world_tree else 0,
            "total_swipes": len(self.world_tree.get_node(self.world_tree.active_node_id).get("swipes", [])) if self.world_tree else 1,
        }

    def swipe_to(self, direction: str) -> Optional[Dict[str, Any]]:
        """切换到当前节点的不同 swipe 版本"""
        if not self.world_tree or not self._pending_turn:
            return None
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return None
        swipes = node.get("swipes", [])
        if len(swipes) <= 1:
            return None

        idx = node.get("active_swipe_index", 0)
        if direction == "left":
            idx = max(0, idx - 1)
        else:
            idx = min(len(swipes) - 1, idx + 1)

        node["active_swipe_index"] = idx
        swipe = swipes[idx]

        turn = self._pending_turn["turn"]
        turn.scene_output["scene_text"] = swipe["narrative"]
        turn.scene_output["choices"] = swipe.get("choices", [])
        self._pending_turn["scene"] = turn.scene_output

        return {
            "scene_text": swipe["narrative"],
            "choices": swipe.get("choices", []),
            "swipe_index": idx,
            "total_swipes": len(swipes),
        }

    async def load_branch(self, node_id: str) -> Dict[str, Any]:
        """加载历史分支（支持多线剧情）"""
        if not self.world_tree:
            return {"error": "WorldTree not available"}
        node = self.world_tree.get_node(node_id)
        if not node:
            return {"error": f"Node {node_id} not found"}
        self.world_tree.set_active_node(node_id)
        state_snap = node.get("state_snapshot", {})
        if state_snap:
            self.world_state.current_turn = state_snap.get("turn", 0)
            self.world_state.current_location = state_snap.get("location_id", "")
            if state_snap.get("date"):
                try:
                    self.world_state.current_date = datetime.fromisoformat(state_snap["date"])
                except:
                    pass
            self.world_state.relationships = state_snap.get("relationships", {})
            self.world_state.player_attrs = state_snap.get("player_attrs", {})
            self.world_state.variables = state_snap.get("variables", {})
        return {
            "node_id": node_id,
            "turn": node.get("turn_number"),
            "action": node.get("player_action", {}).get("text", ""),
            "narrative": node.get("ai_response", ""),
            "state": self.world_state.get_snapshot(),
            "choices": node.get("choices_presented", [])
        }
