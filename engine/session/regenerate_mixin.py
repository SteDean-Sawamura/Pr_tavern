"""GameSession Mixin: 重生成与 Swipe 系统"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from engine.prompt_builder import PromptBuilder
from engine.dice import DiceResult
from engine.event_scheduler import parse_time
from engine.session.agentic_mixin import NarrativeSettlementError
from ai.base import strip_think_tags

if TYPE_CHECKING:
    pass  # 避免循环导入

# 玩家风格分析频率（每 N 回合）
PLAY_STYLE_INTERVAL = 5


class RegenerateMixin:
    """重生成（regenerate）、Swipe 切换、续写、历史摘要、风格分析相关方法"""

    _REGEN_CONTEXT_KEYS = ("_generation_baseline", "turn_instructions", "stage_directives", "_turn_player_inputs",
                           "check_result", "triggered_consequences", "achieved_milestones")

    def _capture_generation_baseline(self, ctx: dict) -> dict:
        return {
            "version": 1,
            "state": copy.deepcopy(self.current_state),
            "state_changes": copy.deepcopy(ctx.get("all_state_changes", [])),
            "dice": {
                "items": copy.deepcopy(ctx.get("candidate_conditional_items", {})),
                "results": {item_id: asdict(result) for item_id, result in
                            ctx.get("candidate_conditional_dice_results", {}).items()},
                "states": copy.deepcopy(ctx.get("candidate_conditional_dice_state", {})),
                "event_results": {item_id: asdict(result) for item_id, result in
                                  ctx.get("candidate_event_dice_results", {}).items()},
                "event_states": copy.deepcopy(ctx.get("candidate_event_dice_state", {})),
                "new_time": ctx["new_time"],
                "settled": copy.deepcopy([
                    dice for dice in ctx.get("dice_dicts", [])
                    if not dice.get("candidate_event_id") and not dice.get("candidate_destination")
                ]),
            },
        }

    @staticmethod
    def _generation_dice(baseline: dict) -> dict:
        dice = copy.deepcopy(baseline["dice"])
        for key in ("results", "event_results"):
            dice[key] = {item_id: DiceResult(**result) for item_id, result in dice[key].items()}
        return dice

    @staticmethod
    def _remember_generation_dice(baseline: dict, dice: dict) -> None:
        baseline["dice"]["event_states"] = copy.deepcopy(dice["event_states"])
        baseline["dice"]["event_results"] = {
            item_id: asdict(result) for item_id, result in dice["event_results"].items()
        }

    @staticmethod
    def _require_generation_baseline(node: dict) -> dict:
        baseline = node.get("_generation_baseline")
        if not isinstance(baseline, dict) or baseline.get("version") != 1:
            raise ValueError("此节点缺少生成前检查点，请先完整重生成，再重算状态。")
        return baseline

    async def _prepare_full_regeneration(self, node: dict, parent_state: dict) -> tuple[dict, dict]:
        previous_state = self.current_state
        previous_node = self.world_tree.active_node_id
        previous_turn = self.turn_number
        previous_models = {name: getattr(self, name) for name in
                           ("player_model", "npc_autonomy", "narrative_graph", "director_notes")}
        try:
            for name, model in previous_models.items():
                setattr(self, name, copy.deepcopy(model))
            self.current_state = copy.deepcopy(parent_state)
            self._hydrate_branch_runtime(self.current_state)
            self._restore_session_models_from_state()
            self.world_tree.active_node_id = node["parent_id"]
            self.world_tree._branch_dirty = True
            self.turn_number = max(0, previous_turn - 1)
            action = copy.deepcopy(node.get("player_action") or {"type": "freeform", "text": ""})
            ctx = self._prepare_turn(action)
            ctx["_turn_player_inputs"] = copy.deepcopy(node.get("_turn_player_inputs") or [])
            ctx["turn_instructions"] = list(dict.fromkeys([
                *node.get("turn_instructions", []), *ctx.get("turn_instructions", []),
            ]))
            route = await self._enrich_and_route(action, ctx)
            ctx["generation_baseline"]["state"].update(self._snapshot_session_models())
            return ctx, route
        finally:
            self.current_state = previous_state
            self.world_tree.active_node_id = previous_node
            self.world_tree._branch_dirty = True
            self.turn_number = previous_turn
            for name, model in previous_models.items():
                setattr(self, name, model)
            self._hydrate_branch_runtime(previous_state)

    def _hydrate_branch_runtime(self, state: dict):
        self._hydrate_mechanics_runtime(state)
        self._hydrate_dynamic_story_runtime(state)
        trackers = state.get("cyclic_event_trackers", {})
        for event in self.script.get("cyclic_events", []):
            event_id = event.get("id")
            if not event_id or event_id in trackers:
                continue
            event_state = state.get("events", {}).get(event_id, {})
            next_fire = event_state.get("next_fire", event.get("first_trigger") or self.script.get("start_time", ""))
            if "next_fire" not in event_state:
                scheduled = parse_time(next_fire)
                game_time = parse_time(state.get("game_time"))
                frequency = event.get("frequency_value", 1)
                unit = event.get("frequency_unit", "day")
                if scheduled and game_time and scheduled < game_time and isinstance(frequency, int) and frequency > 0:
                    if unit in ("minute", "hour", "day", "week"):
                        interval = timedelta(**{f"{unit}s": frequency})
                        scheduled += interval * ((game_time - scheduled) // interval)
                        if scheduled < game_time:
                            scheduled += interval
                    else:
                        while scheduled < game_time:
                            following = self.event_scheduler._advance_time(scheduled, frequency, unit)
                            if not following or following <= scheduled:
                                break
                            scheduled = following
                if scheduled:
                    next_fire = scheduled.isoformat()
            trackers[event_id] = {"last_fired": event_state.get("last_fired"), "next_fire": next_fire}
            state["cyclic_event_trackers"] = trackers
        overrides = state.get("lorebook_entry_overrides", {})
        override_ids = self.prompt_builder._lorebook_override_ids | overrides.keys()
        for entry_id in override_ids:
            self.prompt_builder.lorebook.remove_entry(entry_id)
            initial = self.prompt_builder._initial_lore_entries.get(entry_id)
            if initial is not None:
                self.prompt_builder.lorebook.add_entries([copy.deepcopy(initial)])
        self._index_dynamic_npcs_from_state(state)
        self._hydrate_actor_goals(state)
        active_lore_ids = {entry.id for entry in self.prompt_builder.lorebook.entries
                           if entry.entry_type == "story_active" and entry.id.startswith("_st_active_")}
        for entry_id in active_lore_ids:
            self.prompt_builder.lorebook.remove_entry(entry_id)
        story_nodes = self.story_tree_engine._nodes if self.story_tree_engine else {}
        active_nodes = [story_nodes[node_id] for node_id in state.get("story_tree_state", {}).get("active", [])
                        if node_id in story_nodes]
        shadow_nodes = story_nodes if self.event_engine.shadow_story_nodes else {}
        active_nodes.extend(
            self.event_engine._event_to_dict(event) for event_id, event in self.event_engine.events.items()
            if event_id not in shadow_nodes and state.get("events", {}).get(event_id, {}).get("status") == "active"
            and event.category in ("story", "quest", "choice")
        )
        original_state = self.current_state
        try:
            self.current_state = state
            self._sync_node_lorebook([], active_nodes, target=state)
        finally:
            self.current_state = original_state
        current_active_lore_ids = {entry.id for entry in self.prompt_builder.lorebook.entries
                                   if entry.entry_type == "story_active" and entry.id.startswith("_st_active_")}
        active_lore_ids.update(current_active_lore_ids)
        self.prompt_builder.track_dynamic_lore(state)
        for entry_id, enabled in self.prompt_builder._initial_lore_enabled.items():
            self.prompt_builder.lorebook.update_entry_enabled(entry_id, enabled)
        dynamic_lore = [entry for entry in state.get("dynamic_lorebook", [])
                        if entry.get("id") not in self.prompt_builder._static_lore_ids]
        dynamic_lore.extend(
            entry for entry in state.get("dynamic_story_content", {}).get("lorebook", [])
            if entry.get("id") in override_ids
            and entry.get("id") not in self.prompt_builder._static_lore_ids
        )
        if dynamic_lore:
            self.prompt_builder.lorebook.add_entries(dynamic_lore, replace_existing=True)
        for entry_id, entry in overrides.items():
            if entry is None or (entry.get("entry_type") == "story_active"
                                 and entry_id.startswith("_st_active_") and entry_id not in current_active_lore_ids):
                self.prompt_builder.lorebook.remove_entry(entry_id)
            else:
                self.prompt_builder.lorebook.add_entries([copy.deepcopy(entry)], replace_existing=True)
        for entry_id, enabled in state.get("lorebook_enabled_overrides", {}).items():
            self.prompt_builder.lorebook.update_entry_enabled(entry_id, enabled)
        self.prompt_builder.sync_shelved_pc_knowledge(state)
        self.prompt_builder._lorebook_override_ids.update(overrides)
        self._sync_lorebook_vectors(override_ids | active_lore_ids)

    def _replay_pending_event_cascades(self) -> list[str]:
        if not self.current_state.get("_pending_fire_events"):
            return []
        result = self.event_engine.dispatch_pending(
            self.current_state, self.turn_number, condition_eval=self._evaluate_condition,
        )
        instructions = list(result.inject_prompts)
        self._apply_event_result(result, inject_prompts=instructions)
        return instructions

    def _settle_regen_state(self, parsed: dict, baseline: dict,
                            present_npc_ids: list[str], *, action_text: str = "",
                             destination_dice: dict | None = None,
                             initial_location: str | None = None,
                             default_end_time: str | None = None,
                             is_system_opening: bool = False,
                             map_travel_destination: str | None = None) -> tuple[dict, list[dict], list[str]]:
        state = copy.deepcopy(baseline)
        for key in self._SWIPE_STRIP_KEYS:
            if key in self.current_state and key not in state:
                state[key] = copy.deepcopy(self.current_state[key])

        previous_location = initial_location or baseline.get("player", {}).get("location")
        state, changes = self._apply_common_parsed_changes(
            state, ({**parsed, "lore_reveals": []} if parsed.get("lore_reveals") else parsed),
            inplace=True, present_npc_ids=present_npc_ids,
        )
        settled_location = state.get("player", {}).get("location")
        actual_location_change = (
            settled_location if parsed.get("location_change") and settled_location != previous_location else None
        )
        base_time = state.get("game_time", baseline.get("game_time", ""))
        ai_end_time = parsed.get("end_time")
        ai_time_advance = parsed.get("time_advance") if not ai_end_time else None
        travel_minutes = (self._configured_travel_minutes(
            previous_location, actual_location_change, map_travel_destination, baseline,
        ) if actual_location_change else 0)
        accepted_end_time = False
        if ai_end_time:
            try:
                end_time = datetime.fromisoformat(ai_end_time.replace("Z", "+00:00"))
                start_time = datetime.fromisoformat(base_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
            else:
                minutes = (end_time - start_time).total_seconds() / 60
                if travel_minutes and minutes < travel_minutes:
                    raise ValueError(
                        f"到达 {actual_location_change} 至少需要 {travel_minutes} 分钟旅途，"
                        f"end_time 仅推进 {minutes:.0f} 分钟；重生成未保存"
                    )
                if minutes > 0:
                    state["game_time"] = (
                        self._advance_game_time(base_time, timedelta(hours=48))
                        if minutes > 2880 else ai_end_time
                    )
                    accepted_end_time = True
        elif ai_time_advance:
            minutes = self._parse_duration_minutes(ai_time_advance)
            if minutes is not None and minutes < 5:
                state["game_time"] = self._advance_game_time(base_time, timedelta(minutes=5))
            elif minutes is not None and minutes > 2880:
                state["game_time"] = self._advance_game_time(base_time, timedelta(hours=48))
            else:
                state["game_time"] = self._apply_iso_duration(base_time, ai_time_advance)
        elif default_end_time:
            state["game_time"] = default_end_time
        if travel_minutes and not accepted_end_time:
            state["game_time"] = self._advance_game_time(
                state.get("game_time", base_time), timedelta(minutes=travel_minutes),
            )

        final_time = state.get("game_time", base_time)
        if is_system_opening and final_time != baseline.get("game_time"):
            raise ValueError(f"系统开场不推进时间，end_time 必须为 {baseline.get('game_time', '')}")
        future_reveals = self._future_lore_reveal_claims(parsed, final_time)
        if future_reveals:
            raise ValueError(f"词条原话发生在结算时间 {final_time} 之后: {', '.join(future_reveals)}")
        self._apply_npc_lore_reveals(
            state, parsed, present_npc_ids,
            turn_start_time=base_time, turn_end_time=final_time,
        )
        previous_state = self.current_state
        try:
            self.current_state = state
            self._hydrate_branch_runtime(state)
            triggered_events = self.event_scheduler.check_events(
                state, base_time, final_time, condition_eval=self._evaluate_condition,
            )
            settled_event_items = set()
            event_dice_context = {
                "candidate_event_dice_state": destination_dice["event_states"],
                "candidate_event_dice_results": destination_dice["event_results"],
            } if destination_dice else {}

            def settle_event_dice(event_id):
                if not destination_dice:
                    return
                results, event_dice_changes = self._settle_event_linked_dice(
                    event_id, event_dice_context, settled_event_items, stage_unstaged=True,
                )
                destination_dice["settled"].extend(self._dice_result_to_dict(result) for result in results)
                changes.extend(event_dice_changes)

            world_events = []
            event_changes, _, scheduled_result = self._settle_timed_events(
                base_time, final_time, triggered_events, on_event=settle_event_dice,
                world_events=world_events,
            )
            changes.extend(event_changes)
            world_events = copy.copy(self.world_clock).advance(base_time, final_time, schedule_events=world_events)
            if world_events:
                state.setdefault("_pending_world_events", []).extend(world_events)
            if destination_dice:
                for item_id, item in destination_dice["items"].items():
                    if not self._random_item_condition_matches(
                        item.get("trigger_condition"), action_text, base_time,
                        initial_location=previous_location,
                        settled_location=actual_location_change,
                    ):
                        continue
                    staged_state = destination_dice["states"][item_id]
                    if staged_state is None:
                        state.setdefault("random_item_state", {}).pop(item_id, None)
                    else:
                        state.setdefault("random_item_state", {})[item_id] = copy.deepcopy(staged_state)
                    result = destination_dice["results"].get(item_id)
                    if result is not None:
                        destination_dice["settled"].append(self._dice_result_to_dict(result))
                        if result.range_state_changes:
                            state, log = self.state_manager.apply_changes(
                                state, result.range_state_changes, inplace=True,
                            )
                            changes.extend(log)
            if scheduled_result.game_events:
                state.setdefault("_pending_fire_events", []).extend(scheduled_result.game_events)
            state["game_time"] = final_time
            state = self.event_scheduler.update_trackers(
                state, triggered_events, final_time, inplace=True,
            )
            self._apply_npc_event_reports(
                state, parsed, present_npc_ids,
                turn_start_time=base_time, turn_end_time=final_time,
            )
            self._compute_time_atmosphere()
            self._refresh_npc_locations()
            self._ensure_rooms_initialized(state)
            self._apply_npc_contact_changes(state, parsed.get("npc_contact_changes", []))
            changes.extend(self._settle_actor_goals())
            state = self.current_state
        finally:
            self.current_state = previous_state
            self._hydrate_branch_runtime(previous_state)

        return state, changes, [event["event_id"] for event in triggered_events]

    def _record_regen_swipe(self, node: dict, narrative: str, choices: list,
                            state: dict, changes: list[dict], agent_trace: dict | None = None,
                            triggered_events: list[str] | None = None,
                            dice_rolls: list[dict] | None = None,
                            generation_context: dict | None = None,
                            outgoing_state: dict | None = None) -> None:
        self._enrich_choice_previews(choices)
        self.prompt_builder.track_dynamic_lore(self.current_state)
        swipes = node.setdefault("swipes", [])
        if not swipes:
            original = (outgoing_state if outgoing_state is not None else
                        node.get("state_snapshot") or copy.deepcopy(self.current_state))
            swipes.append({
                "narrative": node.get("ai_response", ""),
                "choices": node.get("choices_presented", []),
                "state_changes": node.get("state_changes", []),
                "state_snapshot": self._slim_snapshot(original),
                "agent_trace": copy.deepcopy(node.get("agent_trace")),
                "game_time": node.get("game_time", original.get("game_time", "")),
                "triggered_events": copy.deepcopy(node.get("triggered_events", [])),
                "dice_rolls": copy.deepcopy(node.get("dice_rolls", [])),
                **{key: copy.deepcopy(node[key]) for key in self._REGEN_CONTEXT_KEYS if key in node},
            })
            node["_shared_strip_keys"] = {
                key: copy.deepcopy(original[key])
                for key in self._SWIPE_STRIP_KEYS if key in original
            }
        else:
            active_index = node.get("active_swipe_index", 0)
            if 0 <= active_index < len(swipes):
                if outgoing_state is not None:
                    swipes[active_index]["state_snapshot"] = copy.deepcopy(self._slim_snapshot(outgoing_state))
                swipes[active_index]["agent_trace"] = copy.deepcopy(node.get("agent_trace"))
                for key in self._REGEN_CONTEXT_KEYS:
                    if key in node:
                        swipes[active_index][key] = copy.deepcopy(node[key])
        if triggered_events is None:
            triggered_events = node.get("triggered_events", [])
        if dice_rolls is None:
            dice_rolls = node.get("dice_rolls", [])
        swipes.append({
            "narrative": narrative,
            "choices": choices,
            "state_changes": changes,
            "state_snapshot": self._slim_snapshot(state),
            "agent_trace": copy.deepcopy(agent_trace),
            "game_time": state.get("game_time", ""),
            "triggered_events": copy.deepcopy(triggered_events),
            "dice_rolls": copy.deepcopy(dice_rolls),
            **{key: copy.deepcopy(node[key]) for key in self._REGEN_CONTEXT_KEYS if key in node},
            **copy.deepcopy(generation_context or {}),
        })
        if generation_context:
            node.update(copy.deepcopy(generation_context))
        node["active_swipe_index"] = len(swipes) - 1
        node["ai_response"] = narrative
        node["choices_presented"] = choices
        node["state_changes"] = changes
        node["state_snapshot"] = state
        node["game_time"] = state.get("game_time", "")
        node["triggered_events"] = copy.deepcopy(triggered_events)
        node["dice_rolls"] = copy.deepcopy(dice_rolls)
        if agent_trace:
            node["agent_trace"] = agent_trace
        else:
            node.pop("agent_trace", None)
        self.current_state = state
        self._hydrate_branch_runtime(state)

    async def regenerate(self, stage: str = "all", hint: str = "") -> dict:
        """Regenerate the AI response for the current node (Swipe system).

        stage: "all" = full regenerate (narrative+choices+state)
               "choices" = keep narrative, regenerate choices only
               "state" = keep narrative+choices, regenerate state only
        hint: optional narrative direction hint injected into the prompt
        """
        await self._drain_background_tasks()  # P0-3
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return {"error": "No active node"}

        self.current_state.update(self._snapshot_session_models())
        self._restore_session_models_from_state()
        action = node.get("player_action") or {"type": "freeform", "text": ""}
        is_system_opening = isinstance(action, dict) and action.get("type") == "system"
        game_time = node.get("game_time", "")

        # B16: 排除当前节点（正在被重新生成），只取之前的历史
        all_recent = self.world_tree.get_recent_history(6)
        recent = [n for n in all_recent if n["id"] != self.world_tree.active_node_id][-5:]
        recent_messages = [n.get("ai_response", "") for n in recent]
        action_text = action.get("text", "") if isinstance(action, dict) else str(action)
        player_inputs = self.prompt_builder.format_turn_player_inputs(node.get("_turn_player_inputs"))

        check_result = node.get("check_result")

        current_narrative = node.get("ai_response", "")
        current_choices = node.get("choices_presented", [])
        regen_agent_trace = None
        regen_working_state = None

        # 计算在场NPC和历史上下文（regenerate也需要）
        parent_id = node.get("parent_id")
        parent_snapshot = None
        if parent_id:
            parent_node = self.world_tree.get_node(parent_id)
            if parent_node:
                parent_snapshot = parent_node.get("state_snapshot")
                if not parent_snapshot:
                    parent_snapshot = await self._load_snapshot_from_db(parent_id)
                    if parent_snapshot:
                        parent_node["state_snapshot"] = parent_snapshot
        generation_baseline = None
        if stage == "all":
            if not parent_snapshot:
                return {"error": "Previous state snapshot not found"}
            regen_ctx, regen_route = await self._prepare_full_regeneration(node, parent_snapshot)
            generation_baseline = regen_ctx["generation_baseline"]
        elif stage == "state":
            try:
                generation_baseline = copy.deepcopy(self._require_generation_baseline(node))
            except ValueError as exc:
                return {"error": str(exc), "stage": stage}
        regen_baseline = copy.deepcopy(
            self.current_state if stage == "choices" else
            generation_baseline["state"] if generation_baseline else
            (parent_snapshot if parent_snapshot else self.current_state)
        )
        previous_state = self.current_state
        try:
            self.current_state = regen_baseline
            self._hydrate_branch_runtime(regen_baseline)
            regen_present_npc_ids, _ = self._compute_present_npcs(regen_baseline)
            if stage != "all":
                activated_lore, _ = self.prompt_builder.scan_lorebook(
                    action_text, recent_messages,
                    timed_state=regen_baseline.get("lorebook_timed_state"),
                    turn_number=self.turn_number,
                    state=regen_baseline,
                )
        finally:
            self.current_state = previous_state
            self._hydrate_branch_runtime(previous_state)

        destination_dice = self._generation_dice(generation_baseline) if generation_baseline else None

        if stage == "all":
            regen_ctx["candidate_event_dice_state"] = destination_dice["event_states"]
            regen_ctx["candidate_event_dice_results"] = destination_dice["event_results"]
            # hint injection
            regen_action = dict(action)
            if hint:
                regen_action["text"] = action_text + f"\n[重生成倾向] 请让本次叙事偏向以下方向：{hint}"

            regen_plot_reasoning = ""
            self.current_state = copy.deepcopy(regen_baseline)
            try:
                self._hydrate_branch_runtime(self.current_state)
                self._restore_session_models_from_state()
                if self._agentic_unified_enabled():
                    async for item in self._execute_unified_agent(
                        regen_ctx, regen_route, regen_action, streaming=False,
                    ):
                        if item["type"] == "pipeline_result":
                            raw_narrative = item["narrative"]
                            parsed = item["parsed"]
                            regen_agent_trace = item.get("agent_trace")
                            parsed["narrative"] = raw_narrative
                            regen_plot_reasoning = item.get("plot_reasoning", "")
                            plot_decision = item.get("plot_decision", "")
                            regen_working_state = self.current_state
                            break
                else:
                    async for item in self._execute_pipeline(
                        regen_ctx, regen_route, regen_action,
                        state_baseline=regen_baseline,
                    ):
                        if item["type"] == "pipeline_result":
                            raw_narrative = item["narrative"]
                            parsed = item["parsed"]
                            parsed["narrative"] = raw_narrative
                            regen_plot_reasoning = item.get("plot_reasoning", "")
                            plot_decision = item.get("plot_decision", "")
                            break
            finally:
                self.current_state = previous_state
                self._hydrate_branch_runtime(previous_state)

        elif stage == "choices":
            event_sections = PromptBuilder._format_events_for_prompt(
                self.event_engine.get_events_for_prompt(regen_baseline))
            choices_msgs, choices_sys = self.prompt_builder.build_choices_prompt(
                current_narrative, action_text, regen_baseline,
                turn_number=self.turn_number, activated_lore=activated_lore,
                event_sections=event_sections)
            if player_inputs:
                choices_msgs[-1]["content"] += "\n\n" + player_inputs
            raw_choices = await self.ai_provider.generate(
                choices_msgs, system=choices_sys, max_tokens=8192, **self._stage_kwargs("choices"))
            raw_choices = strip_think_tags(raw_choices)

            choices = self._filter_player_visible_choices(
                self.response_parser.parse_choices(raw_choices), current_narrative,
                state=regen_baseline, ctx={"event_sections": event_sections},
            )
            parsed = {"narrative": current_narrative,
                      "choices": choices or self._generate_context_choices(regen_baseline)}

        elif stage == "state":
            npc_msgs, npc_sys = self.prompt_builder.build_npc_reaction_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
                present_npc_ids=regen_present_npc_ids,
            )
            res_msgs, res_sys = self.prompt_builder.build_world_state_resource_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            spa_msgs, spa_sys = self.prompt_builder.build_world_state_spatial_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            tmp_msgs, tmp_sys = self.prompt_builder.build_world_state_temporal_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            wld_msgs, wld_sys = self.prompt_builder.build_world_state_world_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            ext_msgs, ext_sys = self.prompt_builder.build_world_state_ext_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
                event_sections=PromptBuilder._format_events_for_prompt(self.event_engine.get_events_for_prompt(regen_baseline)),
            )
            for messages, stages in ((npc_msgs, ("npc",)), (res_msgs, ()), (spa_msgs, ()),
                                     (tmp_msgs, ()), (wld_msgs, ("world",)), (ext_msgs, ())):
                instructions = self._format_agent_instructions(node, stages, state=regen_baseline)
                instructions = "\n\n".join(part for part in (instructions, player_inputs) if part)
                if instructions:
                    messages[-1]["content"] += "\n\n" + instructions
            _regen_raw = await asyncio.gather(
                self.ai_provider.generate(npc_msgs, system=npc_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(res_msgs, system=res_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(spa_msgs, system=spa_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(tmp_msgs, system=tmp_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(wld_msgs, system=wld_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(ext_msgs, system=ext_sys, max_tokens=4096, **self._stage_kwargs("state")),
                return_exceptions=True,
            )
            [raw_npc, raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext], _ = self._sanitize_gather_results(
                _regen_raw, [("NPC关系推演", False), ("资源状态推演", False),
                             ("空间状态推演", False), ("时间状态推演", False),
                             ("世界属性推演", False), ("扩展状态推演", False)],
            )

            parsed = self.response_parser.parse_split_v3(current_narrative, raw_npc, raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext)
            parsed["choices"] = current_choices

        else:
            return {"error": f"Unknown stage: {stage}"}

        if stage == "all" and not regen_ctx.get("_unified_narrative_output"):
            parsed["narrative"] = self._apply_narrative_output_rules(parsed.get("narrative", ""))

        if stage in ("all", "state"):
            try:
                swipe_state, swipe_state_changes, regen_event_ids = self._settle_regen_state(
                    parsed, regen_working_state or regen_baseline, regen_present_npc_ids,
                     action_text=action_text, destination_dice=destination_dice,
                     initial_location=regen_baseline.get("player", {}).get("location"),
                     default_end_time=destination_dice["new_time"] if stage == "all" else None,
                     is_system_opening=is_system_opening,
                     map_travel_destination=(self._map_travel_destination(action, state=regen_baseline)
                                             if isinstance(action, dict) else None),
                 )
            except ValueError as exc:
                return {"error": str(exc), "stage": stage}
        else:
            swipe_state = copy.deepcopy(self.current_state)
            swipe_state_changes = copy.deepcopy(node.get("state_changes", []))
            regen_event_ids = None

        # 检查 narrative 解析是否有效，避免静默复刻旧叙事产生"假成功"swipe
        new_narrative = parsed.get("narrative", "").strip()
        if stage == "all" and not new_narrative:
            return {"error": "叙事生成为空，重生成失败", "stage": stage}
        if stage == "all":
            feedback = self._timed_event_revision_feedback(
                regen_ctx, new_narrative, settled_end_time=swipe_state.get("game_time"),
                settled_state=swipe_state,
            )
            if feedback:
                logging.getLogger(__name__).warning("重生成未通过事件结算校验：%s", feedback)
                return {"error": str(NarrativeSettlementError(feedback)), "stage": stage}

        if stage in ("all", "state"):
            if stage == "all" and "_workflow_choices" in regen_ctx:
                try:
                    self.current_state = swipe_state
                    self._hydrate_branch_runtime(swipe_state)
                    parsed["choices"] = await self._generate_settled_workflow_choices(new_narrative, regen_ctx)
                finally:
                    self.current_state = previous_state
                    self._hydrate_branch_runtime(previous_state)
            instructions = regen_ctx.get("turn_instructions", []) if stage == "all" else node.get("turn_instructions", [])
            self._consume_choice_instructions(swipe_state, instructions)
            swipe_state_changes = copy.deepcopy(generation_baseline["state_changes"]) + swipe_state_changes
        generation_context = None
        if generation_baseline:
            self._remember_generation_dice(generation_baseline, destination_dice)
            generation_context = {"_generation_baseline": generation_baseline}
        if stage == "all":
            generation_context = {
                "_generation_baseline": generation_baseline,
                **{key: regen_ctx.get(key) for key in self._REGEN_CONTEXT_KEYS if key != "_generation_baseline"},
            }
        swipe_state.update(self._snapshot_session_models())
        self._record_regen_swipe(
            node, new_narrative if stage == "all" else parsed.get("narrative", current_narrative),
            parsed.get("choices", []), swipe_state, swipe_state_changes, regen_agent_trace,
            regen_event_ids,
            dice_rolls=destination_dice["settled"] if destination_dice else None,
            generation_context=generation_context,
            outgoing_state=previous_state,
        )
        if stage == "all" and regen_plot_reasoning:
            node["plot_reasoning"] = regen_plot_reasoning
            node["plot_decision"] = plot_decision

        # Regenerate scene image if provider is available and stage includes narrative
        scene_image = None
        if stage == "all" and getattr(self, "_image_provider", None) and new_narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = swipe_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                tod = swipe_state.get("time_of_day", "day")
                weather = swipe_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states_regen = swipe_state.get("npcs", {})
                for nid, ns in list(npc_states_regen.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    new_narrative, loc_name, "atmospheric", tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_image = await self._image_provider.generate_image(img_prompt)
                scene_image["_prompt"] = img_prompt
            except Exception as e:
                logging.getLogger(__name__).warning("Regenerate scene image failed: %s", e)

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": node["ai_response"],
            "choices": node["choices_presented"],
            "state": self.current_state,
            "state_changes": swipe_state_changes,
            "dice_rolls": node.get("dice_rolls", []),
            "triggered_events": node.get("triggered_events", []),
            "swipe_index": node["active_swipe_index"],
            "total_swipes": len(node["swipes"]),
            "stage": stage,
            "scene_image": scene_image,
            **({"agent_trace": regen_agent_trace} if regen_agent_trace else {}),
        }

    async def attempt_deduction(self, clue_ids: list[str]) -> dict:
        """Player attempts to link clues and form a deduction."""
        clue_board = self.current_state.get("clue_board", [])
        selected = [c for c in clue_board if c["id"] in clue_ids]
        selected_ids = {clue["id"] for clue in selected}
        if len(selected_ids) < 2:
            return {"success": False, "message": "至少需要两条线索才能推理"}
        if set(clue_ids) != selected_ids:
            return {"success": False, "message": "包含尚未发现的线索"}

        # Deterministic cache key: sorted clue IDs
        cache_key = "|".join(sorted(clue_ids))
        deduction_cache = self.current_state.setdefault("deduction_cache", {})
        if cache_key in deduction_cache:
            return deduction_cache[cache_key]

        clue_texts = "\n".join(f"- [{c['category']}] {c['text']}（来源：{c['source']}）" for c in selected)

        # Check script-defined deduction templates first
        templates = self.script.get("deduction_templates", [])
        completed_template = None
        for tpl in templates:
            req_ids = set(tpl.get("required_clues", []))
            if req_ids and req_ids.issubset(selected_ids):
                completed = self.current_state.setdefault("completed_deductions", [])
                if tpl["id"] in completed:
                    if completed_template is None:
                        completed_template = tpl
                else:
                    completed.append(tpl["id"])
                    for effect in tpl.get("effects", []):
                        if effect.get("type") == "reveal_location":
                            vis = self.current_state.setdefault("visible_locations", [])
                            if effect["id"] not in vis:
                                vis.append(effect["id"])
                        elif effect.get("type") == "unlock_secret":
                            us = self.current_state.setdefault("npc_unlocked_secrets", {})
                            nid = effect.get("npc_id", "")
                            sid = effect.get("secret_id", "")
                            if nid and sid:
                                us.setdefault(nid, [])
                                if sid not in us[nid]:
                                    us[nid].append(sid)
                        elif effect.get("type") == "milestone":
                            ms = self.current_state.setdefault("achieved_milestones", [])
                            if effect["id"] not in ms:
                                ms.append(effect["id"])
                    for cid in clue_ids:
                        for c in clue_board:
                            if c["id"] == cid:
                                c.setdefault("linked_to", []).extend(
                                    [x for x in clue_ids if x != cid and x not in c.get("linked_to", [])]
                                )
                    result = {
                        "success": True,
                        "message": tpl.get("conclusion", "你的推理揭示了真相。"),
                        "effects": tpl.get("effects", []),
                    }
                    deduction_cache[cache_key] = result
                    return result
        if completed_template is not None:
            result = {
                "success": True,
                "message": completed_template.get("conclusion", "你的推理揭示了真相。"),
                "effects": [],
                "already_completed": True,
            }
            deduction_cache[cache_key] = result
            return result

        # AI-based open-ended deduction (via tool call for robustness)
        prompt = "玩家尝试将以下线索关联推理：\n" + clue_texts + "\n\n判断这些线索之间是否存在合理关联。如果存在，给出推理结论。调用 judge_deduction 工具提交结果。"
        try:
            deduction_tool = [{"type": "function", "function": {
                "name": "judge_deduction",
                "parameters": {"type": "object", "properties": {
                    "valid": {"type": "boolean", "description": "线索之间是否存在合理关联"},
                    "conclusion": {"type": "string", "description": "推理结论（1-2句话）"},
                    "insight": {"type": "string", "description": "玩家由此获得的新认知（可选）"},
                }, "required": ["valid", "conclusion"]},
            }}]
            resp = await self.ai_provider.generate_with_tools(
                [{"role": "user", "content": prompt}],
                system="你是推理验证引擎。严格基于线索内容判断关联性，不编造线索中不存在的信息。",
                tools=deduction_tool,
                max_tokens=512,
            )
            tc_list = resp.get("tool_calls") or []
            if tc_list:
                parsed = tc_list[0].get("arguments") or {}
            else:
                # 降级：尝试从文本解析
                clean = (resp.get("content") or "").strip()
                if clean.startswith("```"):
                    clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                parsed = json.loads(clean) if clean else {"valid": False, "conclusion": "无法判断"}
            if parsed.get("valid"):
                for cid in clue_ids:
                    for c in clue_board:
                        if c["id"] == cid:
                            c.setdefault("linked_to", []).extend(
                                [x for x in clue_ids if x != cid and x not in c.get("linked_to", [])]
                            )
                conclusion = parsed.get("conclusion", "推理成立。")
                insight = parsed.get("insight", "")
                # 将推理结论作为新线索加入线索栏
                import hashlib
                deduction_id = "ded_" + hashlib.md5(cache_key.encode()).hexdigest()[:8]
                if not any(c["id"] == deduction_id for c in clue_board):
                    source_names = "、".join(c.get("text", c["id"])[:10] for c in selected)
                    clue_board.append({
                        "id": deduction_id,
                        "category": "deduction",
                        "text": conclusion,
                        "source": f"推理（{source_names}）",
                        "turn": self.turn_number,
                        "linked_to": list(clue_ids),
                    })
                if insight:
                    self._record_narrative_callback(
                        insight, ["deduction"], priority="high",
                    )
                result = {"success": True, "message": conclusion, "new_clue_id": deduction_id}
                deduction_cache[cache_key] = result
                return result
            result = {"success": False, "message": parsed.get("conclusion", "这些线索之间似乎没有直接关联。")}
            deduction_cache[cache_key] = result
            return result
        except Exception as e:
            logging.getLogger(__name__).error("attempt_deduction AI调用失败: %s", e)
            return {"success": False, "message": "推理失败，请尝试不同的线索组合。"}

    async def continue_narrative(self) -> dict:
        """Extend the current narrative without creating a new turn."""
        await self._drain_background_tasks()
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return {"error": "No active node"}

        current_narrative = node.get("ai_response", "")
        if not current_narrative:
            return {"error": "No narrative to continue"}

        msgs, sys_prompt = self.prompt_builder.build_continue_prompt(
            current_narrative, self.current_state,
        )
        raw = await self.ai_provider.generate(
            msgs, system=sys_prompt, max_tokens=8192, **self._stage_kwargs("narrative")
        )
        continuation = strip_think_tags(raw).strip() if raw else ""
        if not continuation:
            return {"error": "续写生成为空"}

        continuation = self.regex_engine.apply(continuation, "ai_output")
        extended = current_narrative.rstrip() + "\n\n" + continuation
        node["ai_response"] = extended

        swipes = node.get("swipes")
        if swipes:
            idx = node.get("active_swipe_index", 0)
            if 0 <= idx < len(swipes):
                swipes[idx]["narrative"] = extended

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": extended,
            "continuation": continuation,
            "choices": node.get("choices_presented", []),
            "state": self.current_state,
        }

    def swipe_to(self, direction: str) -> dict | None:
        """Switch to a different swipe on the current node.
        direction: 'left' or 'right'
        """
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
        return self.swipe_to_index(idx)

    def get_all_swipes(self) -> list[dict]:
        """Return summary of all swipes on the active node."""
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return []
        swipes = node.get("swipes", [])
        if not swipes:
            return [{
                "index": 0,
                "narrative_preview": (node.get("ai_response", "") or "")[:300],
                "choices": node.get("choices_presented", []),
                "state_changes": node.get("state_changes", []),
                "active": True,
            }]
        active_idx = node.get("active_swipe_index", 0)
        result = []
        for i, s in enumerate(swipes):
            result.append({
                "index": i,
                "narrative_preview": (s.get("narrative", "") or "")[:300],
                "choices": s.get("choices", []),
                "state_changes": s.get("state_changes", []),
                "active": i == active_idx,
            })
        return result

    def swipe_to_index(self, index: int) -> dict | None:
        """Jump directly to a specific swipe index."""
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return None
        swipes = node.get("swipes", [])
        if index < 0 or index >= len(swipes):
            return None

        current_index = node.get("active_swipe_index", 0)
        if 0 <= current_index < len(swipes):
            for name in ("player_model", "npc_autonomy", "narrative_graph", "director_notes"):
                self.current_state["_" + name] = copy.deepcopy(getattr(self, name).snapshot())
            self.prompt_builder.track_dynamic_lore(self.current_state)
            swipes[current_index]["state_snapshot"] = copy.deepcopy(
                self._slim_snapshot(self.current_state)
            )
            swipes[current_index]["agent_trace"] = copy.deepcopy(node.get("agent_trace"))
            swipes[current_index]["game_time"] = node.get("game_time", "")
            swipes[current_index]["triggered_events"] = copy.deepcopy(node.get("triggered_events", []))
            swipes[current_index]["dice_rolls"] = copy.deepcopy(node.get("dice_rolls", []))
            for key in self._REGEN_CONTEXT_KEYS:
                if key in node:
                    swipes[current_index][key] = copy.deepcopy(node[key])

        node["active_swipe_index"] = index
        swipe = swipes[index]
        node["ai_response"] = swipe["narrative"]
        node["choices_presented"] = swipe.get("choices", [])
        node["state_changes"] = swipe.get("state_changes", [])
        if "triggered_events" in swipe:
            node["triggered_events"] = copy.deepcopy(swipe["triggered_events"])
        if "dice_rolls" in swipe:
            node["dice_rolls"] = copy.deepcopy(swipe["dice_rolls"])
        for key in self._REGEN_CONTEXT_KEYS:
            if key in swipe:
                node[key] = copy.deepcopy(swipe[key])
            else:
                node.pop(key, None)
        if swipe.get("agent_trace"):
            node["agent_trace"] = swipe["agent_trace"]
        else:
            node.pop("agent_trace", None)

        if swipe.get("state_snapshot") is not None:
            restored = copy.deepcopy(swipe["state_snapshot"])
            shared = node.get("_shared_strip_keys", {})
            for key in self._SWIPE_STRIP_KEYS:
                if key not in restored:
                    src = shared.get(key) or self.current_state.get(key)
                    if src is not None:
                        restored[key] = copy.deepcopy(src)
            node["state_snapshot"] = restored
            node["game_time"] = swipe.get("game_time", restored.get("game_time", ""))
            self.current_state = restored
            self._hydrate_branch_runtime(restored)
            self._restore_session_models_from_state()

        self._discard_stale_script_blueprint()

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": swipe["narrative"],
            "choices": swipe.get("choices", []),
            "state": self.current_state,
            "state_changes": swipe.get("state_changes", []),
            "dice_rolls": node.get("dice_rolls", []),
            "triggered_events": node.get("triggered_events", []),
            "swipe_index": index,
            "total_swipes": len(swipes),
            "agent_trace": node.get("agent_trace"),
        }

    @staticmethod
    def _apply_post_processing(text: str, rules: list[dict]) -> str:
        """Apply regex post-processing rules to AI output."""
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            try:
                text = re.sub(rule["find"], rule["replace"], text)
            except re.error:
                pass
        return text

    async def _maybe_summarize_history(self):
        """Check if history needs summarization and do it if so."""
        if self.current_state.get("summary_frozen", False):
            return

        # Calculate word count since last summarization for word-aware trigger
        branch = self.world_tree.get_active_branch()
        last_st = self.current_state.get("last_summarized_turn", 0)
        recent_word_count = sum(
            len(n.get("ai_response", "")) + len(
                n.get("player_action", {}).get("text", "")
                if isinstance(n.get("player_action"), dict) else ""
            )
            for n in branch if n.get("turn_number", 0) > last_st
        )

        if not self.history_summarizer.needs_summary(
            self.turn_number, self.current_state, recent_word_count
        ):
            return

        # 连续失败超过 3 次，跳过本批并推进 last_summarized_turn
        if self._summary_fail_count >= 3:
            logging.getLogger(__name__).warning(
                "摘要连续失败%d次，跳过本批次 turn=%d", self._summary_fail_count, self.turn_number
            )
            self.current_state["last_summarized_turn"] = self.turn_number
            self._summary_fail_count = 0
            return

        # Only fetch the nodes we actually need: older ones beyond keep_recent
        keep = self.history_summarizer.keep_recent
        branch = self.world_tree.get_active_branch()
        if len(branch) <= keep:
            return

        older_nodes = branch[:-keep] if keep > 0 else branch
        # Only summarize nodes since last summarization to avoid re-processing
        last_summarized = self.current_state.get("last_summarized_turn", 0)
        older_nodes = [n for n in older_nodes if n.get("turn_number", 0) > last_summarized]
        if not older_nodes:
            return
        existing_summary = self.current_state.get("history_summary", "")

        system_prompt, user_prompt = self.history_summarizer.build_summary_prompt(
            older_nodes, existing_summary
        )

        try:
            summary = await self.ai_provider.generate(
                [{"role": "user", "content": user_prompt}],
                system=system_prompt, **self._stage_kwargs("summary"),
            )
            async with self._state_lock:
                self.current_state = self.history_summarizer.update_state_with_summary(
                    self.current_state, summary.strip(), self.turn_number
                )
            self._summary_fail_count = 0
            self._sync_key_events_to_lorebook()
        except Exception:
            self._summary_fail_count += 1
            logging.getLogger(__name__).warning(
                "历史摘要生成失败（连续第%d次）", self._summary_fail_count
            )

    async def _maybe_analyze_play_style(self):
        """Periodically analyze player actions and generate a play style summary."""
        interval = PLAY_STYLE_INTERVAL
        if self.turn_number < interval:
            return
        last_analyzed = self.current_state.get("play_style_last_turn", 0)
        if (self.turn_number - last_analyzed) < interval:
            return

        # Gather recent player actions
        branch = self.world_tree.get_active_branch()
        recent = branch[-interval:] if len(branch) >= interval else branch
        actions_text = []
        for node in recent:
            action = node.get("player_action")
            if action:
                text = action.get("text", "") if isinstance(action, dict) else str(action)
                if text:
                    actions_text.append(f"第{node.get('turn_number', '?')}回合: {text}")

        if not actions_text:
            return

        system = (
            "你是一个角色扮演风格分析助手。根据玩家最近的行动，用一个简短标签（2-4字）和一句话描述（20字以内）概括玩家的角色扮演风格。\n"
            "风格标签示例：谨慎型、冒险型、外交型、战斗型、探索型、社交型、阴谋型、善良型、混乱型等。\n"
            "仅输出标签和描述，格式：标签|描述\n"
            "例：谨慎型|总是先观察再行动，避免直接冲突"
        )
        user = "玩家最近的行动：\n" + "\n".join(actions_text)

        try:
            result = await self.ai_provider.generate(
                [{"role": "user", "content": user}],
                system=system, **self._stage_kwargs("summary"),
            )
            result = result.strip()
            # Strip thinking tags if present
            result = strip_think_tags(result)
            async with self._state_lock:
                if "|" in result:
                    tag, desc = result.split("|", 1)
                    self.current_state["play_style_summary"] = {
                        "tag": tag.strip(),
                        "description": desc.strip(),
                        "turn": self.turn_number,
                    }
                else:
                    self.current_state["play_style_summary"] = {
                        "tag": result[:10].strip(),
                        "description": "",
                        "turn": self.turn_number,
                    }
                self.current_state["play_style_last_turn"] = self.turn_number
        except Exception:
            pass  # Non-critical
