"""Story director: background plot progression and dynamic content injection."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StoryDirectorMixin:
    """Mixin for the background story director system — plan, tree, events, goals, mechanics."""

    async def _run_story_director(self):
        """统一后台剧情系统：plan先行，然后tree/events/goals并发。"""
        logger = logging.getLogger(__name__)
        ctx = self._gather_director_context()
        if not ctx.get("goals_text"):
            self._start_script_blueprint(ctx)
            return

        # Phase 1: plan 先执行，更新 blueprint 供后续子任务读取
        plan_result = await self._director_task_plan(ctx)
        if isinstance(plan_result, Exception):
            logger.warning("story_director.plan 失败: %s", plan_result)
            plan_result = None
        elif plan_result and isinstance(plan_result, dict) and plan_result.get("plot_threads"):
            self.current_state["plot_blueprint"] = plan_result
            self.current_state["plot_blueprint"]["_last_run_turn"] = self.turn_number
            ctx["current_bp"] = plan_result

        # Phase 2: tree/events/goals 并发（lorebook 由 lorebook_evolution 统一管理）
        tasks = [
            self._director_task_tree(ctx),
            self._director_task_events(ctx),
            self._director_task_goals(ctx),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        labels = ["tree", "events", "goals"]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("story_director.%s 失败: %s", labels[i], r)

        all_results = {"plan": plan_result, **dict(zip(labels, results))}
        self._apply_director_results(all_results)

        # Phase 3: game_mechanics（接收 events 结果作为上下文）
        events_result = all_results.get("events")
        mech_result = await self._director_task_mechanics(
            ctx, events_result if isinstance(events_result, dict) else None,
        )
        if isinstance(mech_result, Exception):
            logger.warning("story_director.mechanics 失败: %s", mech_result)
            mech_result = None
        if mech_result and isinstance(mech_result, dict):
            self._apply_mechanics_results(mech_result)

        # Phase 4: 如果 tree/events 产出了新内容，触发 lorebook_evolution
        tree_result = all_results.get("tree")
        events_result = all_results.get("events")
        new_nodes = tree_result.get("new_nodes", []) if isinstance(tree_result, dict) else []
        new_events = []
        if isinstance(events_result, dict):
            new_events = events_result.get("one_time", []) + events_result.get("cyclic", [])
        if new_nodes or new_events:
            self.current_state["_pending_lore_evolution"] = True
            self.current_state["_pending_lore_context"] = {
                "new_nodes": [n.get("description", "") for n in new_nodes if isinstance(n, dict)],
                "new_events": [e.get("description", "") for e in new_events if isinstance(e, dict)],
            }

        logger.info("StoryDirector 完成 (turn %d)", self.turn_number)

    def _script_anchor_nodes(self) -> list[dict]:
        if not self.story_tree_engine:
            return []
        story_state = self.current_state.get("story_tree_state", {})
        script_order = tuple(self.story_tree_engine._nodes)
        active_ids = set(story_state.get("active", []))
        completed_ids = set(story_state.get("completed", []))
        active = [node_id for node_id in script_order if node_id in active_ids][:3]
        completed = [node_id for node_id in reversed(script_order) if node_id in completed_ids][:2]
        anchors = []
        for node_id in active + completed:
            tree_id = self.story_tree_engine._tree_for_node.get(node_id, "")
            node = self.story_tree_engine._nodes.get(node_id)
            if not node or tree_id.startswith(("__", "_dyn_")) or any(
                item["id"] == node_id for item in anchors
            ):
                continue
            anchors.append({
                "id": node_id,
                "status": "active" if node_id in active else "completed",
            })
        return anchors

    def _script_anchor_briefs(self) -> list[dict]:
        briefs = []
        for index, anchor in enumerate(self._script_anchor_nodes(), 1):
            node = self.story_tree_engine._nodes[anchor["id"]]
            briefs.append({
                "alias": f"A{index}",
                "status": anchor["status"],
                "name": str(node.get("name", ""))[:100],
                "description": str(node.get("description", ""))[:500],
            })
        return briefs

    def _script_blueprint_source(self):
        anchors = self._script_anchor_nodes()
        visible_history = "\n".join(
            node.get("ai_response", "")[-600:]
            for node in self.world_tree.get_recent_history(3)
            if node.get("ai_response")
        )[-1500:]
        return (self.world_tree.active_node_id, self.turn_number,
                self.current_state.get("game_time", ""),
                tuple((item["id"], item["status"]) for item in anchors), visible_history)

    def _discard_stale_script_blueprint(self) -> bool:
        current = self._script_blueprint_source()
        task = self._deferred_story_task
        if task is not None and self._deferred_story_source != current:
            task.cancel()
            self._deferred_story_task = None
            self._deferred_story_source = None
        blueprint = self.current_state.get("plot_blueprint", {})
        if blueprint.get("_source") != "script_anchored":
            return False
        source = blueprint.get("_script_context")
        expected = [current[0], current[1], current[2],
                    [list(item) for item in current[3]], current[4]]
        if source != expected:
            self.current_state.pop("plot_blueprint", None)
            return True
        return False

    def _start_script_blueprint(self, ctx: dict):
        self._discard_stale_script_blueprint()
        if not self.ai_provider or self._deferred_story_task is not None:
            return
        source = self._script_blueprint_source()
        anchors = self._script_anchor_nodes()
        if not anchors or not source[4]:
            return
        existing = self.current_state.get("plot_blueprint", {})
        if existing.get("plot_threads") and existing.get("_source") != "script_anchored":
            return
        context = {
            "game_time": ctx["game_time"],
            "visible_history": source[4],
            "anchors": self._script_anchor_briefs(),
        }
        self._deferred_story_source = source
        self._deferred_story_task = self._schedule_deferred_task(
            self._director_task_script_plan(context)
        )

    async def _director_task_script_plan(self, context: dict) -> dict | None:
        anchors = json.dumps(context["anchors"], ensure_ascii=False)
        prompt = (
            f"当前时间: {context['game_time']}\n玩家已经看到的叙事:\n{context['visible_history']}\n"
            f"幕后剧本节点（仅供选方向，不代表事件已经发生或玩家知情）:\n{anchors}\n\n"
            "结合剧本底稿与玩家已见叙事，选择0-3个仍有张力的节点及推进方式。"
            "不得把幕后节点内容写入输出；不要预言结局或把尚未触发的事件当成事实。"
            "approach 只能是 observe、investigate、interact；无需扩展则返回空对象。\n"
            '返回JSON: {"plot_threads":[{"source_node_id":"A1","approach":"investigate"}]}'
        )
        return await asyncio.wait_for(
            self._retry_ai_call(
                [{"role": "user", "content": prompt}],
                "你是幕后剧本规划器。剧本底稿是可能性，不是已发生事实；只返回代号和方式的紧凑JSON。",
                self._parse_simple_json, label="script_blueprint", stage="knowledge_graph",
            ), timeout=60,
        )

    def _consume_script_blueprint(self) -> bool:
        self._discard_stale_script_blueprint()
        task = self._deferred_story_task
        if task is None or not task.done():
            return False
        self._deferred_story_task = None
        source = self._deferred_story_source
        self._deferred_story_source = None
        if task.cancelled() or source is None:
            return False
        try:
            proposal = task.result()
        except Exception:
            logger.exception("异步剧本规划失败")
            return False
        if source != self._script_blueprint_source():
            return False
        anchors = self._script_anchor_nodes()
        if not isinstance(proposal, dict):
            return False
        threads = proposal.get("plot_threads")
        if not isinstance(threads, list):
            return False
        approaches = {
            "observe": "观察当前局势与已知线索的变化",
            "investigate": "核查玩家已掌握的线索",
            "interact": "寻找可交谈的人并核实已知局势",
        }
        validated = []
        for item in threads[:3]:
            if not isinstance(item, dict):
                continue
            alias = item.get("source_node_id")
            approach = item.get("approach", "observe")
            if (not isinstance(alias, str) or alias not in {f"A{index}" for index in range(1, len(anchors) + 1)}
                    or not isinstance(approach, str) or approach not in approaches):
                continue
            anchor_id = anchors[int(alias[1:]) - 1]["id"]
            if any(thread["source_node_id"] == anchor_id for thread in validated):
                continue
            validated.append({
                "id": f"_script_{anchor_id}",
                "name": f"局势线索{alias[1:]}",
                "source_node_id": anchor_id,
                "stages": [{"id": f"_script_{anchor_id}_current",
                            "description": approaches[approach], "status": "active"}],
            })
        if not validated:
            return False
        self.current_state["plot_blueprint"] = {
            "plot_threads": validated,
            "_source": "script_anchored",
            "_last_run_turn": self.turn_number,
            "_script_context": [source[0], source[1], source[2],
                                [list(item) for item in source[3]], source[4]],
        }
        return True

    def _gather_director_context(self) -> dict:
        """收集所有子任务共享的上下文。"""
        world_bg = self.script.get("world_background", "")[:400]
        pc = self.current_state.get("player", {})
        key_events = self.current_state.get("key_events", [])[-8:]
        recent_events_text = "；".join(
            e.get("event", "")[:40] for e in key_events
        )
        world_pulse = self._extract_world_pulse_hints()
        current_bp = self.current_state.get("plot_blueprint", {})
        game_time = self.current_state.get("game_time", "")

        # 近期叙事摘要（最近3回合的 plot_decision 摘要）
        recent_narrative = ""
        recent_nodes = self.world_tree.get_recent_history(3)
        narr_lines = []
        for nd in recent_nodes:
            pd = nd.get("plot_decision", "")
            if pd:
                narr_lines.append(pd[:80])
        if narr_lines:
            recent_narrative = "；".join(narr_lines)

        npc_goals = []
        for npc in self.script.get("npcs", []):
            goals = npc.get("goals", [])
            if goals:
                descs = [g.get("description", g.get("id", "")) for g in goals]
                npc_goals.append(f"- {npc.get('name', npc['id'])}: {'; '.join(descs)}")

        org_goals = []
        for org in self.script.get("organizations", []):
            goals = org.get("goals", [])
            if goals:
                descs = [g.get("description", g.get("id", "")) for g in goals]
                org_goals.append(f"- {org.get('name', org['id'])}: {'; '.join(descs)}")

        goals_text = "\n".join(npc_goals + org_goals)

        existing_event_ids = []
        dsc = self.current_state.get("dynamic_story_content", {})
        for e in dsc.get("one_time_events", []):
            existing_event_ids.append(e.get("id", ""))
        for e in dsc.get("cyclic_events", []):
            existing_event_ids.append(e.get("id", ""))

        dynamic_node_ids = []
        script_tree_ids = []
        if self.story_tree_engine:
            for nid in self.story_tree_engine._nodes:
                if nid.startswith("_dyn_"):
                    dynamic_node_ids.append(nid)
                else:
                    script_tree_ids.append(nid)

        script_lore_summary = "; ".join(
            f"{e.id}({e.comment or ','.join(e.keys[:2])})"
            for e in self.prompt_builder.lorebook.entries
            if e.enabled and not e.id.startswith("_kg_") and not e.id.startswith("_dyn_")
        )[:800]

        script_event_ids = [e.get("id", "") for e in self.script.get("one_time_events", [])]
        script_event_ids += [e.get("id", "") for e in self.script.get("cyclic_events", [])]

        active_states = self.current_state.get("active_persistent_states", [])

        random_items = []
        for ri in self.script.get("random_items", []):
            condition = ri.get("trigger_condition")
            if condition and not isinstance(condition, str):
                condition = json.dumps(condition, ensure_ascii=False, separators=(",", ":"))
            random_items.append(
                f"{ri['id']}({ri.get('trigger_type', '?')}"
                f"{', cond=' + condition[:50] if condition else ''})"
            )
        ri_summary = "; ".join(random_items)[:500]

        sv = self.current_state.get("script_variables", {})
        var_summary = "; ".join(f"{k}={v}" for k, v in sv.items())[:300]

        trigger_summary = "; ".join(
            f"{t['id']}({t.get('condition', '')})"
            for t in self.trigger_engine.triggers
        )[:300]

        return {
            "world_bg": world_bg,
            "pc_summary": f"位置:{pc.get('location','?')}, 属性:{pc.get('attributes',{})}",
            "recent_events": recent_events_text,
            "recent_narrative": recent_narrative,
            "world_pulse": world_pulse,
            "current_bp": current_bp,
            "goals_text": goals_text,
            "game_time": game_time,
            "existing_event_ids": ", ".join(existing_event_ids)[:300],
            "dynamic_node_ids": dynamic_node_ids,
            "key_events_raw": key_events,
            "script_lore_summary": script_lore_summary,
            "script_event_ids": ", ".join(script_event_ids)[:300],
            "script_tree_ids": script_tree_ids,
            "active_states": active_states,
            "ri_summary": ri_summary,
            "var_summary": var_summary,
            "trigger_summary": trigger_summary,
        }

    async def _director_task_plan(self, ctx: dict) -> dict | None:
        """长期规划：更新 plot_threads，标记 stage status。"""
        current_bp = ctx["current_bp"]
        bp_text = ""
        if current_bp.get("plot_threads"):
            lines = []
            for t in current_bp["plot_threads"]:
                stages_desc = ", ".join(
                    f"{s['id']}({s.get('status','pending')})" for s in t.get("stages", [])
                )
                lines.append(f"- {t.get('name', t['id'])}: [{stages_desc}]")
            bp_text = "\n".join(lines)

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"各势力目标:\n{ctx['goals_text']}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
            f"近期叙事走向: {ctx['recent_narrative'][:200]}\n"
        )
        if ctx["world_pulse"]:
            prompt += f"世界脉搏: {ctx['world_pulse']}\n"
        if bp_text:
            prompt += f"当前规划:\n{bp_text}\n\n"
        else:
            prompt += "尚无规划\n\n"
        prompt += (
            "任务：基于以上信息，输出更新后的全局剧情规划。\n"
            "- 如果近期事件使某stage落地，标记status为completed\n"
            "- 如果某stage的前置已完成，标记为active\n"
            "- 可以新增/删除/调整threads和stages\n"
            "- 每条thread 2-4个stages，每stage一句话描述\n"
            '返回JSON: {"plot_threads":[{"id":"","name":"","driver":"npc/org_id",'
            '"stages":[{"id":"","description":"","status":"pending|active|completed"}]}]}'
        )
        system = "你是剧情规划器。维护全局剧情线方向，供每回合骨架决策参考。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_plan", stage="knowledge_graph",
        )

    async def _director_task_tree(self, ctx: dict) -> dict | None:
        """story_tree 节点管理：生成新节点 + 审查 pending_review 节点完成。"""
        current_bp = ctx["current_bp"]
        active_stages = []
        if current_bp.get("plot_threads"):
            for t in current_bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        pending_review_nodes = []
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            active_set = set(sts.get("active", []))
            for nid in ctx["dynamic_node_ids"]:
                node = self.story_tree_engine._nodes.get(nid)
                if node and node.get("type") == "pending_review" and nid in active_set:
                    pending_review_nodes.append(
                        f"- {nid}: {node.get('description', node.get('name', ''))}"
                    )

        if not active_stages and not pending_review_nodes:
            return None

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
            f"活跃剧情方向:\n" + ("\n".join(active_stages) if active_stages else "无") + "\n"
            f"已有动态节点ID: {', '.join(ctx['dynamic_node_ids'][:20])}\n"
            f"剧本已有节点ID: {', '.join(ctx['script_tree_ids'][:30])}\n"
        )
        if pending_review_nodes:
            prompt += (
                f"\n待审查节点（判断是否已在叙事中落地）:\n" +
                "\n".join(pending_review_nodes) + "\n"
            )
        prompt += (
            "\n任务：\n"
            "1. 根据活跃剧情方向，生成0-3个新 story_tree 节点\n"
            "2. 对待审查节点，判断是否可标记完成\n\n"
            "节点type可选:\n"
            "- timed: 定时自动完成（附duration_turns:回合数）\n"
            "- auto: 条件满足时自动完成（附condition表达式）\n"
            "- pending_review: 无法给出明确条件，由下次审查判断完成\n\n"
            "condition 语法:\n"
            "- player.属性名 op 值 (op: >=, <=, >, <, ==, !=)\n"
            "- event_fired.事件ID == true\n"
            "- node_completed.节点ID == true\n"
            "- milestone.里程碑ID == true\n"
            "- 多条件: cond1 AND cond2, cond1 OR cond2\n\n"
            '返回JSON: {"new_nodes":[{"id":"_dyn_xxx","name":"","description":"",'
            '"type":"timed|auto|pending_review","duration_turns":5,"condition":"",'
            '"requires":["已有节点ID"],"related_npcs":[],"related_orgs":[]}],'
            '"complete_nodes":["要标记完成的节点ID"]}'
        )
        system = "你是剧情树管理器。生成可追踪的叙事节点，审查节点是否已落地。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_tree", stage="knowledge_graph",
        )

    async def _director_task_events(self, ctx: dict) -> dict | None:
        """事件生成：生成新的 one_time/cyclic 事件。"""
        active_stages = []
        bp = ctx["current_bp"]
        if bp.get("plot_threads"):
            for t in bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"玩家状态: {ctx['pc_summary']}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
        )
        if ctx["world_pulse"]:
            prompt += f"世界脉搏暗示: {ctx['world_pulse']}\n"
        if active_stages:
            prompt += f"活跃剧情方向:\n" + "\n".join(f"- {s}" for s in active_stages) + "\n"
        if ctx["active_states"]:
            prompt += f"活跃持续状态: {', '.join(ctx['active_states'])}\n"
        prompt += (
            f"已有动态事件ID: {ctx['existing_event_ids']}\n"
            f"剧本已有事件ID: {ctx['script_event_ids']}\n\n"
            "任务：生成0-2个新事件来丰富世界。事件应该是玩家可能遭遇的具体情境。\n"
            "不要与已有事件重复或冲突。\n"
            '返回JSON: {"one_time":[{"id":"_dyn_evt_xxx","name":"","description":"",'
            '"trigger_time":"ISO时间或空","condition":"可选条件表达式"}],'
            '"cyclic":[{"id":"_dyn_cyc_xxx","name":"","description":"",'
            '"frequency_value":1,"frequency_unit":"day","condition":"可选"}]}\n'
            "不需要扩展则返回空: {}"
        )
        system = "你是事件生成器。生成与世界观一致的动态事件。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_events", stage="knowledge_graph",
        )

    async def _director_task_goals(self, ctx: dict) -> dict | None:
        """NPC/org 目标更新。"""
        if not ctx["recent_events"]:
            return None
        prompt = (
            f"各势力当前目标:\n{ctx['goals_text']}\n"
            f"近期关键事件: {ctx['recent_events']}\n\n"
            "任务：根据剧情进展，判断是否需要新增/调整目标。\n"
            '返回JSON: {"npc_goals":[{"npc_id":"","goals":[{"id":"","description":"","type":"short_term|long_term","priority":"low|medium|high"}]}],'
            '"org_goals":[{"org_id":"","goals":[{"id":"","description":"","priority":"medium"}]}]}\n'
            "不需要变更则返回空: {}"
        )
        system = "你是目标管理器。根据剧情进展更新NPC和组织目标。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_goals", stage="knowledge_graph",
        )

    async def _director_task_mechanics(self, ctx: dict, events_result: dict | None) -> dict | None:
        """游戏机制维护：根据剧情变化更新 random_items / variables / triggers。"""
        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
        )
        if ctx["active_states"]:
            prompt += f"活跃持续状态: {', '.join(ctx['active_states'])}\n"
        prompt += (
            f"\n当前随机项ID: {', '.join(item['id'] for item in self.script.get('random_items', []))}\n"
            f"当前随机项: {ctx['ri_summary']}\n"
            f"当前脚本变量: {ctx['var_summary']}\n"
            f"当前触发器: {ctx['trigger_summary']}\n"
        )
        if events_result:
            new_evts = []
            for e in events_result.get("one_time", []):
                new_evts.append(e.get("name", e.get("id", "")))
            for e in events_result.get("cyclic", []):
                new_evts.append(e.get("name", e.get("id", "")))
            if new_evts:
                prompt += f"本轮新增事件: {', '.join(new_evts)}\n"
        prompt += (
            "\n任务：检查上述游戏机制是否需要随世界变化更新。\n"
            "- 随机项(random_items)：新地点/势力出现时更新trigger_condition，或新增/移除随机项\n"
            "- conditional 的 trigger_condition 用条件对象：location_ids 是剧本地点ID列表，action_keywords 是行动关键词列表，night 和 destination_changed 是布尔值；对象内同时满足，any 列表中的对象满足其一即可。不要把自然语言写成条件\n"
            "- 变量(variables)：如需跟踪新的世界状态量\n"
            "- 触发器(triggers)：如需在特定事件触发时自动改变持续状态\n"
            "不需要变更则返回空: {}\n\n"
            "返回JSON:\n"
            '{"random_items":{"add":[{"id":"_dyn_xxx","description":"","trigger":"","trigger_type":"conditional","trigger_condition":{"location_ids":["已有地点ID"],"action_keywords":["行动关键词"]},'
            '"duration_turns":6,"cooldown_turns":2,"dice":{"count":1,"faces":100,"modifier":0},'
            '"ranges":[{"min":1,"max":50,"label":"","description":"","state_changes":[]},{"min":51,"max":100,"label":"","description":"","state_changes":[]}]}],'
            '"update":[{"id":"已有id","trigger_type":"conditional","trigger_condition":{"night":true}}],"remove":["过时id"]},'
            '"variables":{"add":[{"id":"xxx","type":"number","default":0,"min":0,"max":100}],"update":[{"id":"xxx","value":0}]},'
            '"triggers":{"add":[{"id":"_dyn_xxx","condition":"event.xxx.fired","actions":[{"type":"activate_persistent_state","target":"xxx"}]}],'
            '"update":[{"id":"xxx","enabled":false}],"remove":["过时id"]}}'
        )
        system = "你是游戏机制维护器。根据世界变化更新随机项、变量和触发器。只返回紧凑JSON，不需要变更返回{}。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_mechanics", stage="knowledge_graph",
        )

    def _apply_mechanics_results(self, results: dict):
        """应用 game_mechanics 子任务的结果。"""
        logger = logging.getLogger(__name__)
        dsc = self.current_state.setdefault("dynamic_story_content", {
            "trees": [], "one_time_events": [], "cyclic_events": [],
            "lorebook": [], "generation_log": [],
        })
        ri = results.get("random_items")
        if ri and isinstance(ri, dict):
            self._apply_random_item_updates(ri)
            dsc.setdefault("random_item_updates", []).append({"turn": self.turn_number, "updates": ri})
            logger.info("StoryDirector mechanics: random_items updated (add=%d, update=%d, remove=%d)",
                        len(ri.get("add", [])), len(ri.get("update", [])), len(ri.get("remove", [])))
        var = results.get("variables")
        if var and isinstance(var, dict):
            self._apply_variable_updates(var)
            dsc.setdefault("variable_updates", []).append({"turn": self.turn_number, "updates": var})
            logger.info("StoryDirector mechanics: variables updated (add=%d, update=%d)",
                        len(var.get("add", [])), len(var.get("update", [])))
        trg = results.get("triggers")
        if trg and isinstance(trg, dict):
            self._apply_trigger_updates(trg)
            dsc.setdefault("trigger_updates", []).append({"turn": self.turn_number, "updates": trg})
            logger.info("StoryDirector mechanics: triggers updated (add=%d, update=%d, remove=%d)",
                        len(trg.get("add", [])), len(trg.get("update", [])), len(trg.get("remove", [])))

    def _apply_director_results(self, results: dict):
        """应用所有子任务的结果到游戏状态。"""
        logger = logging.getLogger(__name__)
        dsc = self.current_state.setdefault("dynamic_story_content", {
            "trees": [], "one_time_events": [], "cyclic_events": [],
            "lorebook": [], "generation_log": [],
        })

        # Plan 已在 Phase 1 应用，此处只记录日志用
        plan = results.get("plan")

        # Tree
        tree_result = results.get("tree")
        if tree_result and isinstance(tree_result, dict):
            new_nodes = tree_result.get("new_nodes", [])
            if new_nodes:
                tree = {"id": f"_dyn_director_{self.turn_number}", "name": "动态剧情",
                        "nodes": new_nodes}
                dsc["trees"].append(tree)
                self._inject_dynamic_tree(tree)

            completed_nodes = []
            for nid in tree_result.get("complete_nodes", []):
                if self.story_tree_engine and nid in self.story_tree_engine._nodes:
                    node = self.story_tree_engine._nodes[nid]
                    sts = self.current_state.setdefault("story_tree_state", {
                        "completed": [], "active": [], "unlocked": [],
                        "timed_progress": {}, "choices_made": {},
                    })
                    if nid not in sts["completed"]:
                        sts["completed"].append(nid)
                        if nid in sts["active"]:
                            sts["active"].remove(nid)
                        completed_nodes.append(node)
                        logger.info("StoryDirector 标记节点完成: %s", nid)
            if completed_nodes:
                self._sync_node_lorebook(completed_nodes, [])

        # Events
        events_result = results.get("events")
        if events_result and isinstance(events_result, dict):
            for evt in events_result.get("one_time", []):
                if evt.get("id"):
                    dsc["one_time_events"].append(evt)
                    self._inject_dynamic_event(evt, "one_time")
            for evt in events_result.get("cyclic", []):
                if evt.get("id"):
                    dsc["cyclic_events"].append(evt)
                    self._inject_dynamic_event(evt, "cyclic")

        # Goals
        goals_result = results.get("goals")
        if goals_result and isinstance(goals_result, dict):
            self._apply_goal_updates(goals_result)

        # Log
        dsc["generation_log"].append({
            "turn": self.turn_number,
            "summary": f"plan={'ok' if plan else 'skip'}, tree={len(tree_result.get('new_nodes',[])) if isinstance(tree_result, dict) else 0} nodes",
        })
        dsc["_last_expand_turn"] = self.turn_number

        # Capacity limits
        if len(dsc["generation_log"]) > 20:
            dsc["generation_log"] = dsc["generation_log"][-20:]
        if len(dsc["lorebook"]) > 50:
            dsc["lorebook"] = dsc["lorebook"][-50:]
        if len(dsc["one_time_events"]) > 30:
            dsc["one_time_events"] = dsc["one_time_events"][-30:]
        if len(dsc["cyclic_events"]) > 15:
            dsc["cyclic_events"] = dsc["cyclic_events"][-15:]
        if len(dsc["trees"]) > 20:
            dsc["trees"] = dsc["trees"][-20:]
        if len(dsc.get("random_item_updates", [])) > 20:
            dsc["random_item_updates"] = dsc["random_item_updates"][-20:]
        if len(dsc.get("variable_updates", [])) > 20:
            dsc["variable_updates"] = dsc["variable_updates"][-20:]
        if len(dsc.get("trigger_updates", [])) > 20:
            dsc["trigger_updates"] = dsc["trigger_updates"][-20:]

    def _extract_world_pulse_hints(self) -> str:
        """Extract [世界脉搏] lines from recent plot decisions."""
        recent = self.world_tree.get_recent_history(5)
        hints = []
        pulse_re = re.compile(r'\[世界脉搏\]\s*(.*?)(?:\n\[|$)', re.DOTALL)
        for node in recent:
            pd = node.get("plot_decision", "")
            if not pd:
                continue
            m = pulse_re.search(pd)
            if m:
                text = m.group(1).strip()
                if text:
                    hints.append(text[:100])
        return "；".join(hints[-3:]) if hints else ""

    async def _search_for_expansion(self, world_bg: str, recent_events: str, pc: dict) -> str:
        """Use AI to extract a search keyword, then web-search for reference material."""
        settings = self.script.get("settings", {})
        if not settings.get("web_search_expansion", True):
            return ""
        if not self.ai_provider:
            return ""

        location = pc.get("location", "")
        context = f"世界背景: {world_bg[:100]}\n位置: {location}\n近期事件: {recent_events}"

        # Let routing model produce a concise search keyword
        try:
            keyword_raw = await self._retry_ai_call(
                [{"role": "user", "content": context}],
                "根据以下游戏上下文，输出一个最适合网络搜索的关键词短语（用于查找相关历史/地理/文化资料）。"
                "只输出关键词本身，不超过15字，不要解释。如果上下文信息充分不需要搜索，输出空字符串。",
                lambda x: x.strip() if isinstance(x, str) else "",
                label="search_keyword", stage="knowledge_graph",
            )
        except Exception:
            keyword_raw = ""

        if not keyword_raw or len(keyword_raw) > 40:
            return ""

        try:
            from search.search_engine import SearchEngine
            engine = SearchEngine()
            results = await engine.search(keyword_raw, sources=["web"], max_results=3)
            if not results:
                return ""
            lines = ["## 外部参考资料（仅供参考，须适配世界观）"]
            for r in results:
                title = r.get("title", "")
                content = r.get("content", "")[:200]
                if title or content:
                    lines.append(f"- {title}: {content}")
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception as e:
            logging.getLogger(__name__).debug("扩展搜索失败(非致命): %s", e)
            return ""

    def _inject_dynamic_tree(self, tree: dict):
        """注入一棵动态剧情树到 story_tree_engine 和 event_engine。"""
        if not self.story_tree_engine:
            from engine.story_tree import StoryTreeEngine
            self.story_tree_engine = StoryTreeEngine({"trees": []})
        existing_ids = set(self.story_tree_engine._nodes.keys())
        new_node_ids = {n["id"] for n in tree.get("nodes", []) if n.get("id")}
        valid_ids = existing_ids | new_node_ids
        tree_copy = {
            "id": tree["id"],
            "name": tree.get("name", ""),
            "icon": tree.get("icon", "scroll"),
            "nodes": [],
        }
        for node in tree.get("nodes", []):
            if node.get("id") in existing_ids:
                continue
            # 动态生成的 choice 节点降级为 auto（叙事不会自动展示选项）
            if node.get("type") == "choice":
                node["type"] = "auto"
                node.pop("choices", None)
            # 动态节点不直接暴露给叙事层，通过lorebook间接影响
            effects = node.get("effects", {})
            effects.pop("narrative_callback", None)
            effects.pop("notify", None)
            # 清理引用不存在节点的 requires
            requires = node.get("requires", [])
            if requires:
                node["requires"] = [r for r in requires if isinstance(r, str) and r in valid_ids]
                if not node["requires"] and node.get("type") == "auto":
                    continue
            tree_copy["nodes"].append(node)
            existing_ids.add(node["id"])
        if tree_copy["nodes"]:
            self.story_tree_engine.trees.append(tree_copy)
            for node in tree_copy["nodes"]:
                self.story_tree_engine._nodes[node["id"]] = node
                self.story_tree_engine._tree_for_node[node["id"]] = tree_copy["id"]
        self.event_engine.inject_dynamic_tree(tree)

    def _inject_dynamic_event(self, evt: dict, kind: str):
        """注入动态事件到 event_scheduler 和 event_engine。"""
        collection_key = "one_time_events" if kind == "one_time" else "cyclic_events"
        collection = self.event_scheduler.script.setdefault(collection_key, [])
        if any(e.get("id") == evt["id"] for e in collection):
            return
        collection.append(evt)
        self._event_def_by_id[evt["id"]] = evt
        self._event_desc_by_id[evt["id"]] = evt.get("description", evt["id"])
        if kind == "cyclic":
            self.event_scheduler._cyclic_by_id[evt["id"]] = evt
            trackers = self.current_state.setdefault("cyclic_event_trackers", {})
            if evt["id"] not in trackers:
                trackers[evt["id"]] = {"next_fire": evt.get("first_trigger", "")}
        self.event_engine.inject_dynamic_event(evt, kind)
        self.event_engine.inject_dynamic_event_tracker(self.current_state, evt, kind)

    def _apply_goal_updates(self, updates: dict):
        """更新 NPC/组织的 goals。"""
        for npc_update in updates.get("npc_goals", []):
            npc_id = npc_update.get("npc_id")
            new_goals = npc_update.get("goals", [])
            npc_def = self._npc_by_id.get(npc_id)
            if npc_def and new_goals:
                existing = {g.get("id") for g in npc_def.get("goals", [])}
                for g in new_goals:
                    if g.get("id") not in existing:
                        npc_def.setdefault("goals", []).append(g)
        for org_update in updates.get("org_goals", []):
            org_id = org_update.get("org_id")
            new_goals = org_update.get("goals", [])
            for org in self.script.get("organizations", []):
                if org.get("id") == org_id:
                    existing = {g.get("id") for g in org.get("goals", [])}
                    for g in new_goals:
                        if g.get("id") not in existing:
                            org.setdefault("goals", []).append(g)
                    break

    def _apply_lorebook_updates(self, updates: dict):
        """更新已有 lorebook 条目的 content。"""
        for lore_upd in updates.get("lorebook", []):
            lid = lore_upd.get("id")
            new_content = lore_upd.get("content")
            if lid and new_content:
                self.prompt_builder.lorebook.update_entry(lid, content=new_content)

    def _mechanics_definitions(self):
        return {
            "random_items": self.script.get("random_items", []),
            "variables": list(self.script_variables.definitions.values()),
            "triggers": self.trigger_engine.triggers,
        }

    def _persist_mechanics_runtime(self):
        overrides = {}
        for section, entries in self._mechanics_definitions().items():
            initial = {entry["id"]: entry for entry in self._initial_mechanics[section] if entry.get("id")}
            current = {entry["id"]: entry for entry in entries if entry.get("id")}
            overrides[section] = {
                entry_id: copy.deepcopy(current.get(entry_id))
                for entry_id in dict.fromkeys([*current, *initial])
                if initial.get(entry_id) != current.get(entry_id)
            }
        self.current_state["mechanics_overrides"] = overrides

    def _set_mechanics_definitions(self, definitions):
        self.script["random_items"] = definitions["random_items"]
        self._random_item_by_id = {entry["id"]: entry for entry in definitions["random_items"] if entry.get("id")}
        self.script_variables.definitions = {entry["id"]: entry for entry in definitions["variables"] if entry.get("id")}
        self.trigger_engine.triggers = definitions["triggers"]

    def _hydrate_mechanics_runtime(self, state):
        previous_triggers = self.trigger_engine.triggers
        self._set_mechanics_definitions(copy.deepcopy(self._initial_mechanics))
        overrides = state.get("mechanics_overrides", {})
        history = state.get("dynamic_story_content", {})
        for section, key, apply_updates in (
            ("random_items", "random_item_updates", self._apply_random_item_updates),
            ("variables", "variable_updates", self._apply_variable_updates),
            ("triggers", "trigger_updates", self._apply_trigger_updates),
        ):
            if section not in overrides:
                for record in history.get(key, []):
                    apply_updates(record.get("updates", {}), persist=False)
        definitions = self._mechanics_definitions()
        for section, entries in definitions.items():
            changes = overrides.get(section, {})
            existing = {entry.get("id") for entry in entries}
            definitions[section] = [
                copy.deepcopy(changes.get(entry.get("id"), entry)) for entry in entries
                if changes.get(entry.get("id"), entry) is not None
            ]
            definitions[section].extend(
                copy.deepcopy(entry) for entry_id, entry in changes.items()
                if entry_id not in existing and entry is not None
            )
        self._set_mechanics_definitions(definitions)
        if previous_triggers != self.trigger_engine.triggers:
            self._sync_mechanics_triggers()

    def _sync_mechanics_triggers(self):
        from engine.story_tree import StoryTreeEngine

        tree_def = {}
        self._merge_legacy_events_into_tree(tree_def, {"triggers": self.trigger_engine.triggers})
        trees = tree_def.get("trees", [])
        self.script["triggers"] = copy.deepcopy(self.trigger_engine.triggers)
        if self.story_tree_engine:
            engine = self.story_tree_engine
            for node_id, tree_id in list(engine._tree_for_node.items()):
                if tree_id == "__system_triggers":
                    engine._tree_for_node.pop(node_id)
                    engine._nodes.pop(node_id, None)
            engine.trees[:] = [tree for tree in engine.trees if tree["id"] != "__system_triggers"]
            engine.trees.extend(trees)
            for tree in trees:
                for node in tree["nodes"]:
                    engine._nodes[node["id"]] = node
                    engine._tree_for_node[node["id"]] = tree["id"]
        elif trees:
            self.story_tree_engine = StoryTreeEngine(tree_def)
        if self.event_engine:
            engine = self.event_engine
            for event_id, tree_id in list(engine._tree_for_event.items()):
                if tree_id == "__system_triggers":
                    engine._tree_for_event.pop(event_id)
                    engine.events.pop(event_id, None)
            engine._trees[:] = [tree for tree in engine._trees if tree["id"] != "__system_triggers"]
            engine._load_story_trees({"story_tree": tree_def})
            engine.shadow_story_nodes = self.story_tree_engine is not None

    def _apply_random_item_updates(self, updates: dict, *, persist=True):
        """应用 random_items 的增删改。"""
        updates = copy.deepcopy(updates)
        random_items = self.script.setdefault("random_items", [])
        for rid in updates.get("remove", []):
            random_items[:] = [r for r in random_items if r.get("id") != rid]
            self._random_item_by_id.pop(rid, None)
        for upd in updates.get("update", []):
            item = self._random_item_by_id.get(upd.get("id"))
            if item:
                for field in ("trigger_type", "trigger_condition", "description", "trigger"):
                    if field in upd:
                        item[field] = upd[field]
        for new_item in updates.get("add", []):
            nid = new_item.get("id", "")
            if nid and nid not in self._random_item_by_id:
                if not nid.startswith("_dyn_"):
                    new_item["id"] = f"_dyn_{nid}"
                    nid = new_item["id"]
                if nid in self._random_item_by_id:
                    continue
                random_items.append(new_item)
                self._random_item_by_id[nid] = new_item
        if persist:
            self._persist_mechanics_runtime()

    def _apply_variable_updates(self, updates: dict, *, persist=True):
        """应用 script_variables 的新增和值更新。"""
        for var_def in updates.get("add", []):
            vid = var_def.get("id")
            if vid and vid not in self.script_variables.definitions:
                self.script_variables.definitions[vid] = copy.deepcopy(var_def)
                if persist:
                    self.script_variables.set(self.current_state, vid, var_def.get("default", 0))
        for upd in updates.get("update", []):
            vid = upd.get("id")
            if persist and vid and "value" in upd:
                self.script_variables.set(self.current_state, vid, upd["value"])
        if persist:
            self._persist_mechanics_runtime()

    def _apply_trigger_updates(self, updates: dict, *, persist=True):
        """应用 triggers 的增删改。"""
        updates = copy.deepcopy(updates)
        triggers = self.trigger_engine.triggers
        for tid in updates.get("remove", []):
            triggers[:] = [t for t in triggers if t.get("id") != tid]
        for upd in updates.get("update", []):
            for t in triggers:
                if t.get("id") == upd.get("id"):
                    for field in ("condition", "enabled", "description", "actions", "event", "action", "params"):
                        if field in upd:
                            t[field] = upd[field]
                    break
        for new_t in updates.get("add", []):
            nid = new_t.get("id", "")
            if nid and not any(t.get("id") == nid for t in triggers):
                if not nid.startswith("_dyn_"):
                    new_t["id"] = f"_dyn_{nid}"
                if any(trigger.get("id") == new_t["id"] for trigger in triggers):
                    continue
                triggers.append(new_t)
        if persist:
            self._persist_mechanics_runtime()
            self._sync_mechanics_triggers()

    def _restore_dynamic_story(self):
        """从 current_state 恢复动态剧情内容（会话恢复时调用）。"""
        dsc = self.current_state.get("dynamic_story_content")
        if not dsc:
            return
        for tree in dsc.get("trees", []):
            self._inject_dynamic_tree(tree)
        for evt in dsc.get("one_time_events", []):
            self._inject_dynamic_event(evt, "one_time")
        for evt in dsc.get("cyclic_events", []):
            self._inject_dynamic_event(evt, "cyclic")
        for entry in dsc.get("lorebook", []):
            self.prompt_builder.lorebook.add_entries([entry])
        self._hydrate_mechanics_runtime(self.current_state)

    def _extract_name_keywords(self, text: str) -> list[str]:
        """Extract NPC/org/location names from text as lorebook keywords."""
        keywords = []
        for npc in self.script.get("npcs", []):
            name = npc.get("name", "")
            if name and name in text:
                keywords.append(name)
        for dyn_id, dyn_st in self.current_state.get("npcs", {}).items():
            if dyn_id in self._npc_by_id or not isinstance(dyn_st, dict):
                continue
            name = dyn_st.get("name", "")
            if name and name in text:
                keywords.append(name)
        for org in self.script.get("organizations", []):
            name = org.get("name", "")
            if name and name in text:
                keywords.append(name)
        for loc in self.script.get("locations", []):
            name = loc.get("name", "")
            if name and name in text:
                keywords.append(name)
        return keywords

    def _get_npc_or_org_name(self, id_str: str) -> str:
        """Resolve an NPC or org ID to its display name."""
        npc = self._npc_by_id.get(id_str, {})
        if npc:
            return npc.get("name", id_str)
        dyn = self.current_state.get("npcs", {}).get(id_str)
        if isinstance(dyn, dict) and dyn.get("name"):
            return dyn["name"]
        for org in self.script.get("organizations", []):
            if org.get("id") == id_str:
                return org.get("name", id_str)
        return id_str
