"""State-apply mixin -- response application and state mutation for GameSession."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timedelta
from itertools import chain

from typing import TYPE_CHECKING

from engine.event_engine import EventResult
from engine.event_scheduler import is_public_event, is_public_story_node, parse_time, public_event_notifications

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)


class StateApplyMixin:
    """State application: parsed response handling, inventory, weather, emotions."""

    async def _apply_parsed_response(
        self: GameSession, parsed: dict, raw_response: str, player_action: dict, ctx: dict,
    ) -> dict:
        """Apply parsed AI response to game state and finalize the turn.

        Returns the full result dict.
        """
        # FLOW-3: Replace generic fallback choices with context-aware ones
        _GENERIC_TEXTS = {"继续探索", "与周围的人交谈", "做其他事情", "环顾四周", "离开这里"}
        choices = parsed.get("choices", [])
        if choices:
            generic_indices = [i for i, c in enumerate(choices) if c.get("text") in _GENERIC_TEXTS]
            if generic_indices:
                if len(generic_indices) == len(choices):
                    # 全部泛化：整体替换
                    parsed["choices"] = self._generate_context_choices()
                else:
                    # 部分泛化：逐条替换
                    context_choices = self._generate_context_choices()
                    for idx in generic_indices:
                        # 从 context_choices 中取一个未出现在当前列表中的替代
                        replacement = None
                        for cc in context_choices:
                            if not any(c.get("text") == cc["text"] for c in choices):
                                replacement = cc
                                break
                        if replacement:
                            replacement["id"] = choices[idx].get("id", f"c{idx+1}")
                            choices[idx] = replacement

        new_time = ctx["new_time"]
        triggered_events = ctx["triggered_events"]
        all_state_changes = ctx["all_state_changes"]

        # Post-processing rules
        rules = self.script.get("post_processing_rules", [])
        if rules:
            parsed["narrative"] = self._apply_post_processing(
                parsed.get("narrative", raw_response), rules
            )

        # Regex scripts on AI output
        if parsed.get("narrative"):
            parsed["narrative"] = self.regex_engine.apply(parsed["narrative"], "ai_output")

        # O-1: 复用共享方法处理通用 state 变更（state_changes / npc_attitude /
        # persistent_states / world_properties / inventory / consequences /
        # offscreen NPC / dynamic NPCs / npc_relationships / reveal_locations /
        # location_change + travel_time）
        old_attitudes = {
            nid: nd.get("attitude_toward_player", 50)
            for nid, nd in self.current_state.get("npcs", {}).items()
            if isinstance(nd, dict)
        }
        old_faction_reps = {
            fid: fd.get("value", 50)
            for fid, fd in self.current_state.get("faction_reputation", {}).items()
            if isinstance(fd, dict)
        }
        previous_location = self.current_state.get("player", {}).get("location")
        ctx["_travel_origin"] = previous_location
        map_destination = self._map_travel_destination(player_action)
        if map_destination:
            ctx["_map_travel_destination"] = map_destination
        self.current_state, common_changes = self._apply_common_parsed_changes(
            self.current_state, ({**parsed, "lore_reveals": []}
                                 if parsed.get("lore_reveals") else parsed), inplace=True,
            present_npc_ids=ctx.get("present_npc_ids"),
        )
        settled_location = self.current_state.get("player", {}).get("location")
        actual_location_change = (
            settled_location if parsed.get("location_change") and settled_location != previous_location else None
        )
        travel_minutes = (self._travel_minutes_for_updates(ctx, actual_location_change, None)[0]
                          if actual_location_change else 0)
        all_state_changes.extend(common_changes)
        npc_attitude_notifications = self._check_attitude_thresholds(
            old_attitudes, parsed.get("npc_attitude_changes", [])
        )
        self._sync_world_changes_to_lorebook(old_attitudes)
        self._check_faction_reputation_events(old_faction_reps)

        # 记录场景中有态度变化的 NPC 到 interaction_log
        npc_att_changes = parsed.get("npc_attitude_changes", [])
        if npc_att_changes:
            action_text = player_action.get("text", "")
            interaction_log = self.current_state.setdefault("npc_interaction_log", {})
            for ac in npc_att_changes:
                npc_id = ac.get("npc_id", "")
                if not npc_id:
                    continue
                npc_ilog = interaction_log.setdefault(npc_id, [])
                npc_ilog.append({
                    "turn": self.turn_number,
                    "context": action_text[:60],
                    "attitude_delta": ac.get("change", 0),
                })
                if len(npc_ilog) > 15:
                    interaction_log[npc_id] = npc_ilog[-10:]

        # 自动标记已出场NPC为 known=true，避免AI反复"首次出场"
        narrative_text = parsed.get("narrative", raw_response)
        self._auto_mark_npcs_known(narrative_text)

        # Time advance from AI (Stage 4b is the sole decision-maker)
        # end_time: AI 直接输出叙事结束时的绝对时间戳
        ai_end_time = parsed.get("end_time")
        # 向后兼容：旧格式 time_advance (ISO 8601 duration)
        ai_time_advance = parsed.get("time_advance") if not ai_end_time else None
        pre_ai_time = new_time
        accepted_end_time = False
        time_error = ""
        if ai_end_time:
            time_error = self._validate_end_time(ai_end_time, ctx, actual_location_change)
            if time_error:
                logger.warning("AI end_time=%r 未采用: %s", ai_end_time, time_error)
            else:
                new_time = ctx["old_time"] if ctx.get("is_system_opening") else ai_end_time
                accepted_end_time = True
        elif ai_time_advance and not ctx.get("is_system_opening"):
            ai_new_time = self._apply_iso_duration(ctx["old_time"], ai_time_advance)
            ai_minutes = self._parse_duration_minutes(ai_time_advance)
            if ai_minutes is not None:
                if ai_minutes < 5:
                    ai_new_time = self._advance_game_time(ctx["old_time"], timedelta(minutes=5))
                elif ai_minutes > 2880:
                    ai_new_time = self._advance_game_time(ctx["old_time"], timedelta(hours=48))
            new_time = ai_new_time

        # Explicit end_time already marks the scene's end, including travel.
        if actual_location_change and not accepted_end_time and not ctx.get("is_system_opening"):
            if travel_minutes:
                new_time = self._advance_game_time(new_time, timedelta(minutes=travel_minutes))

        # Re-check events if AI changed the initial estimate
        if new_time != pre_ai_time:
            extra_events = self.event_scheduler.check_events(
                self.current_state, pre_ai_time, new_time,
                condition_eval=self._evaluate_condition,
            )
            existing_keys = {(e["event_id"], e["fire_time"]) for e in triggered_events}
            for e in extra_events:
                if (e["event_id"], e["fire_time"]) not in existing_keys:
                    triggered_events.append(e)

        future_reveals = self._future_lore_reveal_claims(parsed, new_time)
        feedback = (json.dumps({"error": "词条原话发生在结算时间之后，请修改叙事或结束时间",
                                "end_time": new_time, "entry_ids": future_reveals}, ensure_ascii=False)
                    if future_reveals else self._timed_event_revision_feedback(
                        ctx, parsed.get("narrative", raw_response), settled_end_time=new_time,
                    ))
        if feedback:
            from engine.session.agentic_mixin import NarrativeSettlementError
            logger.warning("叙事未通过事件结算校验：%s", feedback)
            raise NarrativeSettlementError(feedback)

        # Filter out events whose fire_time exceeds final time (estimated was too large)
        final_time = new_time
        if final_time and ctx["old_time"]:
            valid_events = []
            for evt in triggered_events:
                ft = evt.get("fire_time", "")
                if not ft or ft <= final_time:
                    valid_events.append(evt)
                else:
                    logger.info("事件 %s fire_time=%s 超出最终时间 %s，跳过",
                                evt.get("event_id"), ft, final_time)
            triggered_events[:] = valid_events

        candidate_dice_results = ctx.get("candidate_event_dice_results", {})
        candidate_dice_state = ctx.get("candidate_event_dice_state", {})
        actual_dice_results = []
        settled_item_ids = set()

        def settle_event_dice(event_id):
            if not self.current_state.get("dice_check_enabled", True):
                return
            for item in self.script.get("random_items", []):
                if (item.get("trigger_type") != "event_linked"
                        or item.get("linked_event_id") != event_id
                        or item["id"] in settled_item_ids):
                    continue
                item_id = item["id"]
                settled_item_ids.add(item_id)
                if item_id in candidate_dice_state:
                    staged_state = candidate_dice_state[item_id]
                    if staged_state is None:
                        self.current_state.setdefault("random_item_state", {}).pop(item_id, None)
                    else:
                        self.current_state.setdefault("random_item_state", {})[item_id] = staged_state
                    dice_result = candidate_dice_results.get(item_id)
                else:
                    dice_result = self._roll_or_sustain(item)
                if dice_result is not None:
                    actual_dice_results.append(dice_result)
                    if dice_result.range_state_changes:
                        self.current_state, log = self.state_manager.apply_changes(
                            self.current_state, dice_result.range_state_changes, inplace=True,
                        )
                        all_state_changes.extend(log)

        event_changes, ctx["expired"], scheduled_result = self._settle_timed_events(
            ctx["old_time"], new_time, triggered_events, on_event=settle_event_dice,
        )
        if self.current_state.get("dice_check_enabled", True):
            for item_id, item in ctx.get("candidate_conditional_items", {}).items():
                if not self._random_item_condition_matches(
                    item.get("trigger_condition"), player_action.get("text", ""), ctx["old_time"],
                    initial_location=previous_location,
                    settled_location=(actual_location_change if self.current_state.get("player", {}).get("location")
                                      == actual_location_change else None),
                ):
                    continue
                staged_state = ctx["candidate_conditional_dice_state"][item_id]
                if staged_state is None:
                    self.current_state.setdefault("random_item_state", {}).pop(item_id, None)
                else:
                    self.current_state.setdefault("random_item_state", {})[item_id] = staged_state
                dice_result = ctx["candidate_conditional_dice_results"].get(item_id)
                if dice_result is not None:
                    actual_dice_results.append(dice_result)
                    if dice_result.range_state_changes:
                        self.current_state, log = self.state_manager.apply_changes(
                            self.current_state, dice_result.range_state_changes, inplace=True,
                        )
                        all_state_changes.extend(log)
        ctx["dice_dicts"] = [dice for dice in ctx["dice_dicts"]
                             if not dice.get("candidate_event_id") and not dice.get("candidate_destination")]
        ctx["dice_dicts"].extend(self._dice_result_to_dict(result) for result in actual_dice_results)
        all_state_changes.extend(event_changes)
        if scheduled_result.game_events:
            self.current_state.setdefault("_pending_fire_events", []).extend(scheduled_result.game_events)
        if scheduled_result.notifications:
            ctx.setdefault("trigger_notifications", []).extend(
                public_event_notifications(self.script, scheduled_result))

        # B8: 确保 reveal_locations 中的动态地点有 display_name
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_id = loc_entry.get("id", "")
                loc_name = loc_entry.get("name", loc_id)
            else:
                loc_id = loc_entry
                loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id)
            if not loc_id:
                continue
            dn = self.current_state.setdefault("display_names", {})
            if loc_id not in dn:
                dn[loc_id] = loc_name
        self._apply_weather_effects()
        threshold_events = self._check_attribute_thresholds(all_state_changes)

        # Finalize time and trackers
        self._apply_npc_lore_reveals(
            self.current_state, parsed, ctx.get("present_npc_ids"),
            turn_start_time=ctx.get("old_time"), turn_end_time=new_time,
        )
        self._apply_npc_event_reports(
            self.current_state, parsed, ctx.get("present_npc_ids"),
            turn_start_time=ctx.get("old_time"), turn_end_time=new_time,
        )
        self.current_state["game_time"] = new_time
        if hasattr(self, 'npc_autonomy'):
            self.npc_autonomy.tick(new_time, self.current_state, resolve_npc=self._get_npc_autonomy_info)

        # G3: 世界时钟推进 — NPC 后台日程事件
        if ctx.get("old_time") and new_time and hasattr(self, 'world_clock'):
            world_events = self.world_clock.advance(ctx["old_time"], new_time)
            if world_events:
                self.current_state.setdefault("_pending_world_events", []).extend(world_events)

        self._compute_time_atmosphere()
        self.current_state = self.event_scheduler.update_trackers(
            self.current_state, triggered_events, new_time, inplace=True
        )

        # Adventure log
        event_text = self._update_adventure_log(
            player_action, parsed, triggered_events,
            ctx["triggered_consequences"], ctx["achieved_milestones"],
            threshold_events, all_state_changes=all_state_changes,
        )
        self._mark_used_callbacks(parsed.get("narrative", ""))
        self._mark_used_interactables(player_action)
        self._collect_interactables()
        self._update_pacing_state(ctx)
        self._track_world_pulse(ctx)
        self._record_information(parsed, ctx)
        self._propagate_information()
        self._consolidate_information_memory()
        self._check_memory_echoes(ctx)
        self._trace_choice_ripples(ctx)
        self._compute_compose_feedback(
            parsed.get("narrative", raw_response), ctx,
        )

        game_over = parsed.get("game_over")

        # Build game statistics when game ends
        game_statistics = None
        if game_over:
            game_statistics = {
                "total_turns": self.turn_number,
                "play_time_seconds": self.current_state.get("play_time_seconds", 0),
                "locations_visited": len(self.current_state.get("visible_locations", [])),
                "npcs_met": sum(
                    1 for n in self.current_state.get("npcs", {}).values()
                    if isinstance(n, dict) and n.get("met", n.get("known", False))
                ),
                "milestones_achieved": len(self.current_state.get("achieved_milestones", [])),
                "items_collected": len(self.current_state.get("inventory", [])),
                "ending_type": game_over.get("ending_type", "") if isinstance(game_over, dict) else "",
            }

        self._refresh_npc_locations()
        self._ensure_rooms_initialized()
        self._apply_npc_contact_changes(self.current_state, parsed.get("npc_contact_changes", []))
        self._record_npc_sightings()

        # Persist narrative graph & player model into state for snapshot
        self.current_state["_narrative_graph"] = self.narrative_graph.snapshot()
        self.current_state["_player_model"] = self.player_model.snapshot()
        if hasattr(self, 'npc_autonomy'):
            self.current_state["_npc_autonomy"] = self.npc_autonomy.snapshot()
        if hasattr(self, 'director_notes'):
            self.current_state["_director_notes"] = self.director_notes.snapshot()

        # World tree node
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id
            if self.turn_number > 1
            else self.world_tree.root_node_id,
            game_time=new_time,
            turn_number=self.turn_number,
            player_action=player_action,
            ai_response=parsed.get("narrative", raw_response),
            choices_presented=parsed.get("choices", []),
            dice_rolls=ctx["dice_dicts"],
            state_changes=all_state_changes,
            triggered_events=[e["event_id"] for e in triggered_events],
            state_snapshot=self.current_state,
        )
        # Bug-1: 将 regenerate 需要的上下文写入节点，避免重新生成时丢失
        node = self.world_tree.get_node(node_id)
        if node:
            node["check_result"] = ctx["check_result"]
            node["triggered_consequences"] = ctx["triggered_consequences"]
            node["achieved_milestones"] = ctx["achieved_milestones"]
            if ctx.get("agent_trace"):
                node["agent_trace"] = ctx["agent_trace"]
            if ctx.get("turn_summary_override"):
                node["turn_summary"] = ctx["turn_summary_override"]
            if ctx.get("plot_reasoning"):
                node["plot_reasoning"] = ctx["plot_reasoning"]
            if ctx.get("plot_decision"):
                node["plot_decision"] = ctx["plot_decision"]
            node["_pipeline_ctx"] = {
                "check_result": ctx["check_result"],
                "dice_dicts": ctx["dice_dicts"],
                "triggered_events": ctx["triggered_events"],
                "triggered_consequences": ctx["triggered_consequences"],
                "achieved_milestones": ctx["achieved_milestones"],
                "present_npc_ids": ctx["present_npc_ids"],
                "nearby_npc_ids": ctx["nearby_npc_ids"],
                "activated_lore": ctx["activated_lore"],
                "stage_directives": ctx["stage_directives"],
                "turn_instructions": ctx.get("turn_instructions", []),
                "event_sections": ctx["event_sections"],
                "old_time": ctx["old_time"],
                "estimated_minutes": ctx["estimated_minutes"],
                "is_system_opening": ctx.get("is_system_opening", False),
                "recent_nodes": ctx["recent_nodes"],
                "prev_plot_decision": ctx["prev_plot_decision"],
                "base_history_context": ctx["base_history_context"],
                "context_memory": ctx.get("context_memory", ""),
                "recent_reasoning": ctx.get("recent_reasoning", []),
                "story_lore_ids": ctx.get("story_lore_ids"),
                "history_context": ctx.get("history_context", ""),
                "lore_ids_from_rag": ctx.get("lore_ids_from_rag"),
                "databank_hits": ctx.get("databank_hits", []),
            }
            node["turn_instructions"] = ctx.get("turn_instructions", [])
            node["stage_directives"] = ctx["stage_directives"]
            if ctx.get("route"):
                node["_pipeline_route"] = ctx["route"]

        # Urgent lorebook evolution on significant changes
        if parsed.get("location_change") or parsed.get("new_npcs"):
            self.current_state["_pending_lore_evolution"] = True

        # Feature #1: Consume scene hijack and extend cooldown
        hijack = self.current_state.pop("_scene_hijack", None)
        if hijack:
            cooldowns = self.current_state.setdefault("npc_intervention_cooldowns", {})
            cooldowns[hijack.get("npc_id", "")] = self.turn_number + 10

        # Feature #3: Initialize plan from plot decision if declared
        route = ctx.get("route", {})
        if route.get("has_plan_declaration") and self.current_state.get("pending_plan", {}).get("status") != "active":
            plan_steps = self._extract_plan_steps(ctx.get("plot_decision", ""))
            if plan_steps:
                self.current_state["pending_plan"] = {
                    "id": f"plan_{self.turn_number}",
                    "declared_turn": self.turn_number,
                    "goal": player_action.get("text", "")[:80],
                    "steps_remaining": plan_steps[:5],
                    "steps_completed": [],
                    "status": "active",
                    "risk_level": self._extract_plan_risk(ctx.get("plot_decision", "")),
                }

        # Feature #5: Check for flashback-triggering lore reveals
        seen_flashbacks = self.current_state.get("_seen_flashback_lore", [])
        for lore_entry in ctx.get("activated_lore", []):
            lore_id = lore_entry.id if hasattr(lore_entry, "id") else ""
            if not lore_id or lore_id in seen_flashbacks:
                continue
            has_flashback = (
                (hasattr(lore_entry, "discoverable") and lore_entry.discoverable.get("flashback"))
                or (hasattr(lore_entry, "visibility") and lore_entry.visibility == "hidden")
            )
            if has_flashback:
                content = lore_entry.content if hasattr(lore_entry, "content") else ""
                self.current_state["_pending_flashback"] = {
                    "lore_id": lore_id,
                    "content": content[:500],
                }
                self.current_state.setdefault("_seen_flashback_lore", []).append(lore_id)
                # Cap seen list
                if len(self.current_state["_seen_flashback_lore"]) > 50:
                    self.current_state["_seen_flashback_lore"] = self.current_state["_seen_flashback_lore"][-50:]
                break

        # Unified post-turn dispatch via MetaEventBus
        self._dispatch_meta_events(ctx, parsed, player_action, raw_response)

        # Vector memory: store this turn's content for future semantic retrieval
        if self.vector_memory:
            narrative = parsed.get("narrative", raw_response)
            vm_text = f"{player_action.get('text', '')} → {narrative}"
            lore_ids_str = ",".join(
                e.id for e in ctx.get("activated_lore", [])[:20]
            ) if ctx.get("activated_lore") else ""
            vm_meta = {
                "turn_number": self.turn_number,
                "game_time": self.current_state.get("game_time", ""),
                "location": self.current_state.get("player", {}).get("location", ""),
                "active_lore_ids": lore_ids_str,
            }
            self._schedule_index_task(self._async_vector_store(node_id, vm_text, vm_meta))

        # 提取 NPC 表情标签和主动发言（Stage 4a 已返回）
        npc_expressions = []
        npc_interjections = parsed.get("npc_interjections", [])
        sd = self.current_state.get("scene_details")
        if sd and isinstance(sd, dict):
            npc_expressions = sd.get("npc_expressions", [])

        self._enrich_choice_previews(parsed.get("choices", []))

        result = {
            "node_id": node_id,
            "narrative": parsed.get("narrative", raw_response),
            "choices": parsed.get("choices", []),
            "state": self.current_state,
            "dice_rolls": ctx["dice_dicts"],
            "state_changes": all_state_changes,
            "triggered_events": triggered_events,
            "expired_states": ctx["expired"],
            "check_result": ctx["check_result"],
            "triggered_consequences": ctx["triggered_consequences"],
            "achieved_milestones": ctx["achieved_milestones"],
            "milestone_progress": ctx.get("milestone_progress", []),
            "threshold_events": threshold_events,
            "npc_attitude_notifications": npc_attitude_notifications,
            "game_over": game_over,
            "game_statistics": game_statistics,
            "npc_expressions": npc_expressions,
            "npc_interjections": npc_interjections,
            "activated_lore": [
                {"id": e.id, "comment": e.comment, "content": e.content}
                for e in (ctx["activated_lore"] or []) if e.comment or e.content
            ],
            "databank_hits": [
                {"text": h["text"][:200], "filename": h.get("filename", "")}
                for h in ctx.get("databank_hits", [])
            ],
        }
        if parsed.get("_state_parse_failed"):
            result["warnings"] = ["状态推演解析失败，本回合属性/物品/NPC态度未更新。"]
        if time_error:
            fallback = "已按回合预估时间结算"
            if travel_minutes:
                fallback += "并计入旅途耗时"
            result.setdefault("warnings", []).append(f"end_time 未采用：{time_error}；{fallback}。")

        # Fire after_ai triggers (via unified story tree + event engine)
        _, aa_notifications = self._fire_lifecycle_event("after_ai")
        if aa_notifications:
            result.setdefault("trigger_notifications", []).extend(aa_notifications)
        milestones = getattr(self, "_pending_vector_milestones", [])
        if self.vector_memory:
            if event_text:
                self._schedule_index_task(self._async_vector_store(
                    f"event_{node_id}", event_text,
                    {"turn_number": self.turn_number, "doc_type": "event",
                     "parent_node": node_id, "game_time": new_time,
                     "location": self.current_state.get("player", {}).get("location", "")},
                ))
            seen_milestones = set()
            for milestone in milestones:
                milestone_id = milestone.get("id", "")
                if milestone_id in seen_milestones:
                    continue
                seen_milestones.add(milestone_id)
                self._schedule_index_task(self._async_vector_store(
                    f"ms_{node_id}_{milestone_id}",
                    f"[剧情里程碑·第{self.turn_number}回合] {milestone.get('name', milestone_id)}: {milestone.get('description', '')}",
                    {"turn_number": self.turn_number, "doc_type": "milestone",
                     "node_id": milestone_id, "parent_node": node_id, "game_time": new_time},
                ))
        if hasattr(self, "_pending_vector_milestones"):
            del self._pending_vector_milestones

        # Add before_generation notifications from ctx
        if ctx.get("trigger_notifications"):
            result.setdefault("trigger_notifications", []).extend(ctx["trigger_notifications"])

        # Story tree updates for frontend
        st_result = ctx.get("story_tree_updates")
        if st_result:
            result["story_tree_updates"] = {
                "newly_completed": [{"id": n["id"], "name": n.get("name", n["id"])} for n in st_result.newly_completed
                                    if is_public_story_node(self.script, n["id"])],
                "newly_active": [{"id": n["id"], "name": n.get("name", n["id"]), "type": n.get("type")} for n in st_result.newly_active
                                 if is_public_story_node(self.script, n["id"]) or n.get("type") == "choice"],
            }

        # Generate news from completed story nodes + triggered one-time events
        # Only use events from the initial time window (_prepare_turn), not from
        # AI time-jump re-checks, to prevent future events leaking into current news.
        if not ctx.get("_agentic_post_processed"):
            try:
                news_sources = []
                if st_result and st_result.newly_completed:
                    for n in st_result.newly_completed:
                        if not is_public_story_node(self.script, n["id"]):
                            continue
                        news_sources.append({"type": "story_node", "name": n.get("name", n.get("id", "")), "description": n.get("description", "")})
                ot_ids = {e["id"] for e in self.script.get("one_time_events", []) if isinstance(e, dict) and e.get("id")}
                initial_count = ctx.get("initial_event_count", len(ctx.get("triggered_events") or []))
                initial_events = (ctx.get("triggered_events") or [])[:initial_count]
                for evt in initial_events:
                    eid = evt.get("event_id", "")
                    if eid in ot_ids and is_public_event(self.script, eid):
                        evt_def = self._event_def_by_id.get(eid)
                        desc = evt.get("description", "")
                        if not desc and isinstance(evt_def, dict):
                            desc = evt_def.get("description", "")
                        news_sources.append({"type": "one_time_event", "name": (evt_def or {}).get("name", eid), "description": desc})
                if news_sources and self.ai_provider:
                    news = await self._generate_event_news(news_sources)
                    if news:
                        feed = self.current_state.setdefault("news_feed", [])
                        feed.append(news)
                        if len(feed) > 50:
                            self.current_state["news_feed"] = feed[-50:]
                        result["news"] = [news]
            except Exception:
                pass

        # Emotion classification (non-blocking: if it fails, no emotion label)
        narrative_text = result.get("narrative", "")
        if not ctx.get("_agentic_post_processed") and narrative_text and self.ai_provider:
            try:
                emotion = await self._classify_emotion(narrative_text)
                if emotion:
                    result["emotion"] = emotion
            except Exception:
                pass

        if ctx.get("agent_trace"):
            result["agent_trace"] = ctx["agent_trace"]

        if ctx.get("_agentic_post_processed"):
            self._record_narrative_facts(result.get("narrative", ""), ctx)

        result["state"] = self.current_state
        if node:
            node["state_snapshot"] = copy.deepcopy(self.current_state)
            node["_state_hash"] = self.world_tree.state_hash(node["state_snapshot"])

        return result

        """Classify narrative emotion using a quick AI call."""
        prompt = (
            "从以下标签中选择最匹配的情绪：joy, sadness, anger, fear, surprise, "
            "love, tension, calm, excitement, melancholy。只输出一个英文词。\n\n"
            + narrative[-300:]
        )
        result = await self.ai_provider.generate(
            [{"role": "user", "content": prompt}], max_tokens=10
        )
        if not result:
            return ""
        parts = result.strip().lower().split()
        if not parts:
            return ""
        word = parts[0]
        valid = {"joy", "sadness", "anger", "fear", "surprise", "love", "tension", "calm", "excitement", "melancholy"}
        return word if word in valid else ""

    def _settle_timed_events(self, old_time: str, new_time: str,
                             triggered_events: list[dict], on_event=None):
        event_times = self.event_scheduler.iter_event_times(
            self.current_state, old_time, new_time,
        )
        scheduled_result = EventResult()
        event_changes = []
        fired_events = []
        expired = []
        cursor = old_time
        for fire_time, is_event_time in chain(
            ((event_time, True) for event_time in event_times), [(new_time, False)],
        ):
            self.current_state, newly_expired = self.state_manager.check_expirations(
                self.current_state, fire_time, inplace=True,
            )
            expired.extend(state_id for state_id in newly_expired if state_id not in expired)
            boundary = parse_time(fire_time)
            segment_start = cursor
            if is_event_time and boundary and boundary == parse_time(cursor):
                segment_start = (boundary - timedelta(microseconds=1)).isoformat()
            while True:
                segment = self.event_engine._tick_scheduled(
                    self.current_state, self.current_state.setdefault("events", {}),
                    segment_start, fire_time, self._evaluate_condition,
                )
                scheduled_result.merge(segment)
                if not segment.scheduled_events:
                    break
                for evt in segment.scheduled_events:
                    event_id = evt["event_id"]
                    if on_event:
                        on_event(event_id)
                    event_def = self._event_def_by_id.get(event_id)
                    if isinstance(event_def, dict):
                        effects = self._apply_event_def_effects(event_def, event_id)
                        if not is_public_event(self.script, event_id):
                            effects = [{**change, "_source_event_id": event_id} for change in effects]
                        event_changes.extend(effects)
                        witnesses = [npc_id for npc_id in event_def.get("known_by_npcs", [])
                                     if npc_id in self._npc_by_id
                                     and self.current_state.get("npcs", {}).get(npc_id, {}).get("alive") is not False]
                        if witnesses and evt["type"] == "one_time":
                            network = self.current_state.setdefault("information_network", [])
                            network.append({
                                "id": f"event_{event_id}",
                                "source_event_id": event_id,
                                "origin_turn": self.turn_number,
                                "fact": str(event_def.get("description") or "")[:200],
                                "known_by": witnesses,
                                "known_at": {npc_id: fire_time for npc_id in witnesses},
                                "spread_chance": 0.3,
                                "distortion": 0,
                                "max_spread": 5,
                                "tags": ["faction_event"],
                            })
                            if len(network) > 30:
                                self.current_state["information_network"] = network[-30:]
                    trigger_actions = self.trigger_engine.fire(
                        event_id, self.current_state, condition_eval=self._evaluate_condition,
                    )
                    if trigger_actions:
                        trigger_effects = self.trigger_engine.execute_actions(
                            trigger_actions, self.current_state,
                        )
                        self._apply_trigger_effects(trigger_effects)
                    self.event_scheduler.update_trackers(
                        self.current_state, [evt], fire_time, inplace=True,
                    )
                    if isinstance(event_def, dict) and self.story_tree_engine:
                        self.story_tree_engine.fire_event(
                            f"event.{event_id}", self.current_state,
                            condition_eval=self._evaluate_condition,
                        )
                        for fire_event in event_def.get("fire_events", []):
                            self.story_tree_engine.fire_event(
                                fire_event, self.current_state,
                                condition_eval=self._evaluate_condition,
                            )
                fired_events.extend(segment.scheduled_events)
                segment_start = (boundary - timedelta(microseconds=1)).isoformat()
            cursor = fire_time
        triggered_events[:] = fired_events
        return event_changes, expired, scheduled_result

    async def _generate_event_news(self: GameSession, sources: list[dict]) -> dict | None:
        """Generate a news article from triggered events using AI."""
        if not self.ai_provider or not sources:
            return None
        try:
            bg = self.script.get("background", "")[:300]
            game_time = self.current_state.get("game_time", "")
            event_desc = "\n".join(f"- [{s['type']}] {s['name']}: {s.get('description', '')}" for s in sources)
            prompt = (
                f"根据以下游戏事件，写一条符合游戏世界观的新闻快讯。\n"
                f"世界背景: {bg}\n当前时间: {game_time}\n\n事件:\n{event_desc}\n\n"
                f"请返回JSON: {{\"title\": \"新闻标题(10字内)\", \"content\": \"新闻正文(50-150字)\"}}"
            )
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是游戏世界中的新闻记者，用符合世界观的风格写新闻。只返回紧凑JSON。",
                max_tokens=1024,
            )
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            data = json.loads(m.group()) if m else {"title": "快讯", "content": raw[:200]}
            return {
                "id": f"news_{self.turn_number}",
                "turn": self.turn_number,
                "time": game_time,
                "title": data.get("title", "快讯"),
                "content": data.get("content", ""),
                "source_events": [s["name"] for s in sources],
            }
        except Exception:
            return None

    async def generate_newspaper(self: GameSession, force: bool = False) -> dict:
        """Generate a newspaper summary using AI, with per-turn caching."""
        turn_key = str(self.turn_number)
        papers = self.current_state.get("newspapers", {})
        if not force and turn_key in papers:
            cached = dict(papers[turn_key])
            cached["cached"] = True
            return cached

        game_time = self.current_state.get("game_time", "")
        if not self.ai_provider:
            return {"title": "报纸", "date": game_time, "headline": "无AI服务", "sections": [{"title": "提示", "content": "需要配置AI服务才能生成报纸。"}]}
        bg = self.script.get("background", "")[:300]
        log = self.current_state.get("adventure_log", [])
        news_feed = self.current_state.get("news_feed", [])

        log_text = ""
        recent = log[-20:] if isinstance(log, list) else []
        for entry in recent:
            if isinstance(entry, dict):
                events = entry.get("events", [])
                for ev in events:
                    if isinstance(ev, dict):
                        log_text += f"- [回合{entry.get('turn', '?')}] {ev.get('text', '')}\n"

        news_text = ""
        for n in (news_feed[-10:] if isinstance(news_feed, list) else []):
            if isinstance(n, dict):
                news_text += f"- {n.get('title', '')}: {n.get('content', '')}\n"

        material = (log_text + "\n" + news_text).strip()
        if not material:
            result = {"title": "报纸", "date": game_time, "headline": "风平浪静", "sections": [{"title": "本期无重大新闻", "content": "一切平安。"}]}
            result["cached"] = False
            self.current_state.setdefault("newspapers", {})[turn_key] = result
            return result

        prompt = (
            f"你是游戏世界中的报社总编。根据以下素材，编写一份报纸。\n"
            f"世界背景: {bg}\n当前时间: {game_time}\n\n素材:\n{material[:2000]}\n\n"
            f"请返回JSON: {{\"title\": \"报纸名(2-4字)\", \"date\": \"刊载日期\", "
            f"\"headline\": \"头条标题\", \"sections\": [{{\"title\": \"栏目标题\", \"content\": \"栏目内容\"}}]}}\n"
            f"生成3-5个栏目，内容要符合世界观，有趣生动。只返回JSON。"
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是游戏世界报社总编，用符合世界观的风格编写报纸。只返回JSON。",
                max_tokens=1200,
            )
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
            else:
                result = {"title": "报纸", "date": game_time, "content": raw[:500]}
        except Exception as e:
            return {"title": "报纸", "date": game_time, "headline": "生成失败", "sections": [{"title": "错误", "content": str(e)}]}
        result["cached"] = False
        self.current_state.setdefault("newspapers", {})[turn_key] = result
        return result

    async def switch_pov(self: GameSession, preset_id: str | None = None, custom_character: dict | None = None) -> dict | None:
        """Switch player POV: shelve current PC as NPC, init new PC in same world timeline."""
        state = self.current_state
        pov_history = state.setdefault("pov_history", [])

        # Limit switches
        if len(pov_history) >= 5:
            return None

        # Validate target
        if preset_id:
            shelved = state.get("shelved_pc_data", {})
            presets = self.script.get("player_presets", [])
            preset = next((p for p in presets if p.get("id") == preset_id), None)
            is_shelved = preset_id in shelved
            if not preset and not is_shelved:
                return None
            # Cannot switch to a preset that's already an active NPC (not shelved)
            if not is_shelved and preset_id in state.get("npcs", {}):
                return None
        elif not custom_character:
            return None

        # 1) Shelve current PC
        self._shelve_current_pc()

        # 2) Init new PC
        self._init_new_pc(preset_id, custom_character)

        # 3) Mark POV switch in world tree
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id,
            game_time=state.get("game_time", ""),
            turn_number=self.turn_number,
            player_action={"type": "pov_switch", "text": f"视角切换至 {state['player']['name']}"},
            ai_response="",
            state_snapshot=state,
        )

        # 4) Generate POV switch narrative
        narrative = await self._generate_pov_narrative()

        # Update the node with the narrative
        node = self.world_tree.get_node(node_id)
        if node:
            node["ai_response"] = narrative

        # 5) Generate choices for new PC
        choices = self._generate_context_choices()
        if node:
            node["choices_presented"] = choices

        # POV 切换后旧的冻结前缀不再有效（player_name 等已变）
        self._stable_prefix = None

        return {
            "node_id": node_id,
            "narrative": narrative,
            "player": state["player"],
            "choices": choices,
            "state": state,
        }

    def _apply_resource_state_changes(
        self, state: dict, changes: list[dict], *, inplace: bool = False,
    ) -> tuple[dict, list[dict]]:
        state = state if inplace else copy.deepcopy(state)
        all_changes = []
        max_delta = 30
        for change in changes:
            operation = change.get("op", "add")
            value = change.get("value", 0)
            if operation in ("add", "subtract") and not change.get("reason"):
                logger.warning("丢弃无reason属性变更: %s", change)
                continue
            if isinstance(value, (int, float)):
                if operation == "set":
                    old_value = self.state_manager._get_value(state, change.get("target", ""))
                    if isinstance(old_value, (int, float)) and abs(value - old_value) > max_delta:
                        clamped = old_value + (max_delta if value > old_value else -max_delta)
                        logger.warning(
                            "属性变化幅度过大 %s: %s→%s, 限制为→%s",
                            change.get("target"), old_value, value, clamped,
                        )
                        change = {**change, "value": clamped}
                elif operation in ("add", "subtract") and abs(value) > max_delta:
                    clamped = max_delta if value > 0 else -max_delta
                    logger.warning(
                        "属性变化幅度过大 %s: op=%s val=%s, 限制为%s",
                        change.get("target"), operation, value, clamped,
                    )
                    change = {**change, "value": clamped}
            state, log = self.state_manager.apply_changes(state, [change], inplace=True)
            all_changes.extend(log)
        return state, all_changes

    @staticmethod
    def _clear_derived_npc_positions(state: dict, changes: list[dict]) -> None:
        for change in changes:
            parts = change.get("target", "").split(".", 2)
            if len(parts) != 3 or parts[0] != "npcs":
                continue
            npc_state = state.get("npcs", {}).get(parts[1])
            if not isinstance(npc_state, dict):
                continue
            if parts[2] == "current_location":
                npc_state.pop("_derived_location", None)
                if npc_state.get("current_room") == npc_state.get("_derived_room"):
                    npc_state.pop("current_room", None)
                npc_state.pop("_derived_room", None)
            elif parts[2] == "current_room":
                npc_state.pop("_derived_room", None)

    def _apply_player_location_change(self, state: dict, location_change) -> None:
        if isinstance(location_change, dict):
            location_change = location_change.get("id", location_change.get("location_id", ""))
        if not location_change:
            return
        if not self._can_use_location_id(state, location_change):
            logger.warning("AI location_change=%r 尚未发现，跳过", location_change)
            return
        location = self._location_by_id.get(location_change, {})
        access_condition = location.get("access_condition", "")
        if access_condition and not self._evaluate_condition(access_condition, state):
            state["location_access_blocked"] = {
                "location_id": location_change,
                "reason": location.get("access_blocked_reason", "声望不足，无法进入此区域"),
            }
            return
        player = state.setdefault("player", {})
        if location_change != player.get("location"):
            player["current_room"] = location.get("default_room", "")
        player["location"] = location_change
        state.pop("location_access_blocked", None)

    def _apply_entity_spatial_changes(self, state: dict, parsed: dict) -> None:
        for change in parsed.get("npc_location_changes", []):
            npc_id = change.get("npc_id", "")
            new_loc = change.get("new_location", "")
            if not npc_id or not new_loc:
                continue
            if new_loc not in self._location_by_id and new_loc not in state.get("visible_locations", []):
                logger.warning("AI npc_location_changes: new_location=%r 不在合法地点列表中，跳过", new_loc)
                continue
            npc_st = state.get("npcs", {}).get(npc_id)
            if isinstance(npc_st, dict):
                npc_st.pop("_derived_location", None)
                if npc_st.get("current_room") == npc_st.get("_derived_room"):
                    npc_st.pop("current_room", None)
                npc_st.pop("_derived_room", None)
                npc_st["current_location"] = new_loc

        for change in parsed.get("room_changes", []):
            target_id = change.get("id", "")
            new_room = change.get("new_room", "")
            if not target_id or not new_room:
                continue
            if target_id == "player":
                state.setdefault("player", {})["current_room"] = new_room
            else:
                npc_st = state.get("npcs", {}).get(target_id)
                if isinstance(npc_st, dict):
                    npc_st.pop("_derived_room", None)
                    npc_st["current_room"] = new_room

    def _apply_common_parsed_changes(
        self: GameSession, state: dict, parsed: dict, *, inplace: bool = False,
        present_npc_ids: list[str] | None = None,
    ) -> tuple[dict, list[dict]]:
        """P3: 共享方法 -- 把 parsed AI 响应中的通用 state 变更应用到给定 state。

        被 _apply_parsed_response（主回合，inplace=True）和 regenerate（swipe 副本，
        inplace=False）共用，避免重复逻辑漂移。

        包含：state_changes / npc_attitude_changes / activate|deactivate_states /
        world_property_changes / location_change / reveal_locations / inventory_changes /
        offscreen_npc_updates / new_npcs / npc_relationship_updates。
        注: 事件CRUD(consequences/deadlines/clues/threads)已移至异步事件阶段(event_stage)处理。
        不含 time_advance（两边时间基准不同）和仅主回合需要的副作用
        （旅行时间 / 事件再检查 / 天气 / 阈值 / adventure log）。
        """
        all_changes: list[dict] = []
        dirty_npc_ids: set[str] = set()

        # Harness 级校验：关键字段类型异常时重置
        if not isinstance(parsed.get("state_changes"), (list, type(None))):
            parsed["state_changes"] = []
            logger.warning('[校验] state_changes 格式异常，已重置为空列表')
        if not isinstance(parsed.get("npc_attitude_changes"), (list, type(None))):
            parsed["npc_attitude_changes"] = []
            logger.warning('[校验] npc_attitude_changes 格式异常，已重置为空列表')
        if not isinstance(parsed.get("inventory_changes"), (list, type(None))):
            parsed["inventory_changes"] = []
            logger.warning('[校验] inventory_changes 格式异常，已重置为空列表')

        if parsed.get("state_changes"):
            # Handle variable ops before passing to state_manager
            var_ops = [sc for sc in parsed["state_changes"] if sc.get("type") == "variable"]
            regular_changes = [sc for sc in parsed["state_changes"] if sc.get("type") != "variable"]
            for vop in var_ops:
                self.script_variables.apply_op(
                    state, vop.get("id", ""), vop.get("op", "set"), vop.get("value")
                )
            if regular_changes:
                state, log = self._apply_resource_state_changes(
                    state, regular_changes, inplace=inplace
                )
                all_changes.extend(log)
                self._apply_org_changes_to_script(log)
                self._clear_derived_npc_positions(state, log)
                for sc in log:
                    t = sc.get("target", "")
                    if t == "player.relationships":
                        dirty_npc_ids.update(state.get("player", {}).get("relationships", {}))
                    elif t.startswith("player.relationships."):
                        dirty_npc_ids.add(t.split(".")[2])
                    if t.startswith("npcs."):
                        parts = t.split(".", 2)
                        if len(parts) >= 2:
                            dirty_npc_ids.add(parts[1])

        npc_att = parsed.get("npc_attitude_changes", [])
        if npc_att:
            att_as_state = self._npc_attitude_to_state_changes(npc_att, state=state)
            MAX_ATTITUDE_DELTA = 20
            for sc in att_as_state:
                val = sc.get("value", 0)
                if sc.get("op") in ("add", "subtract") and isinstance(val, (int, float)):
                    if abs(val) > MAX_ATTITUDE_DELTA:
                        sc["value"] = MAX_ATTITUDE_DELTA if val > 0 else -MAX_ATTITUDE_DELTA
            state, log = self.state_manager.apply_changes(
                state, att_as_state, inplace=inplace
            )
            all_changes.extend(log)
            for sc in att_as_state:
                t = sc.get("target", "")
                if "relationships" in t or t.startswith("npcs."):
                    parts = t.split(".")
                    if "relationships" in t and len(parts) >= 3:
                        dirty_npc_ids.add(parts[2])
                    elif t.startswith("npcs.") and len(parts) >= 2:
                        dirty_npc_ids.add(parts[1])
            # Sync significant attitude changes to lorebook
            self._sync_attitude_to_lorebook(npc_att)

        state_operations = parsed.get("_persistent_state_operations")
        if state_operations is None:
            state_operations = [("activate", entry) for entry in parsed.get("activate_states", [])]
            state_operations.extend(("deactivate", state_id) for state_id in parsed.get("deactivate_states", []))
        for operation, entry in state_operations:
            if operation == "deactivate":
                active = state.get("active_persistent_states", [])
                if entry in active:
                    active.remove(entry)
                continue
            if self._is_undeclared_environment_state(entry):
                continue
            if isinstance(entry, dict):
                sid = entry.get("id", "")
                s_name = entry.get("name", "")
                s_desc = entry.get("description", "")
                # name 等于 ID 或纯英文时视为缺失，从描述生成中文名
                if not s_name or s_name == sid or (s_name.isascii() and "_" in s_name):
                    if s_desc:
                        s_name = s_desc[:10].rstrip("，。、,.")
                    else:
                        s_name = sid.replace("_", " ")
            else:
                sid = entry
                ps_def = self._ps_by_id.get(sid, {})
                s_name = ps_def.get("name") or ps_def.get("display_name") or ""
                s_desc = ps_def.get("description") or ""
            if not sid:
                continue
            active = state.setdefault("active_persistent_states", [])
            if sid not in active:
                active.append(sid)
            # 为新状态注册 display_name 和 description
            dn = state.setdefault("display_names", {})
            existing_name = dn.get(sid, "")
            # 覆盖条件：之前没有、或之前存的是英文ID
            if s_name and (not existing_name or (existing_name.isascii() and "_" in existing_name)):
                dn[sid] = s_name
            psd = state.setdefault("persistent_state_descriptions", {})
            if s_desc and sid not in psd:
                psd[sid] = s_desc

        # Invalidate speculative lorebook entries (butterfly effect)
        for lore_id in parsed.get("invalidate_lore", []):
            self.prompt_builder.lorebook.remove_entry(lore_id)
            if self.vector_memory:
                self._schedule_index_task(asyncio.to_thread(self.vector_memory.remove_lorebook, lore_id))
            dl = state.get("dynamic_lorebook", [])
            state["dynamic_lorebook"] = [e for e in dl if e.get("id") != lore_id]

        for wp in parsed.get("world_property_changes", []):
            wp_id = wp.get("id")
            if not wp_id or self._is_undeclared_object_property(wp_id):
                continue
            state.setdefault("world_properties", {})[wp_id] = wp.get("value")
            wp_name = wp.get("name")
            if wp_name:
                state.setdefault("display_names", {})[wp_id] = wp_name
        if parsed.get("world_property_changes"):
            self._update_dynamic_connections(state)

        # reveal_locations 先于 location_change 处理，确保同回合揭示+移动可行
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_id = loc_entry.get("id", "")
                loc_name = loc_entry.get("name", loc_id)
                loc_desc = loc_entry.get("description", "")
            else:
                loc_id = loc_entry
                loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id)
                loc_desc = ""
            if not loc_id:
                continue
            if not self._is_duplicate_location(state, loc_id):
                state = self.state_manager.reveal_location(state, loc_id, inplace=inplace)
                state.setdefault("display_names", {})[loc_id] = loc_name
                if loc_id not in self._location_by_id and loc_desc:
                    self.prompt_builder.add_location_kg_entry(loc_id, loc_name, loc_desc)
                    self.prompt_builder._loc_name_map[loc_id] = loc_name
                    if self.vector_memory:
                        kg_entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_loc_{loc_id}"), None)
                        if kg_entry and kg_entry.content:
                            self._schedule_index_task(asyncio.to_thread(
                                self.vector_memory.add_lorebook, kg_entry.id, kg_entry.content,
                                {"entry_type": kg_entry.entry_type, "comment": kg_entry.comment},
                            ))

        self._apply_player_location_change(state, parsed.get("location_change"))

        for inv in parsed.get("inventory_changes", []):
            item_name = inv.get("item", "")
            if not item_name:
                continue
            inv_action = inv.get("action", "add")
            quantity = inv.get("quantity", 1)
            description = inv.get("description", "")
            inventory = state.setdefault("inventory", [])
            if inv_action == "add":
                found = False
                for entry in inventory:
                    if entry.get("item") == item_name:
                        entry["quantity"] = entry.get("quantity", 1) + quantity
                        if description:
                            entry["description"] = description
                        found = True
                        break
                if not found:
                    new_entry = {"item": item_name, "quantity": quantity}
                    if description:
                        new_entry["description"] = description
                    inventory.append(new_entry)
            elif inv_action == "remove":
                found_entry = next((e for e in inventory if e.get("item") == item_name), None)
                if not found_entry:
                    logger.warning("丢弃移除不存在物品: %s", item_name)
                    continue
                found_entry["quantity"] = found_entry.get("quantity", 1) - quantity
                if found_entry["quantity"] <= 0:
                    inventory.remove(found_entry)

        # Offscreen NPC updates
        _offscreen_seen_ids: set = set()
        for update in parsed.get("offscreen_npc_updates", []):
            # 优先用 name 匹配，兼容旧格式 npc_id
            raw_name = update.get("name", "")
            raw_id = update.get("npc_id", "")
            npc_id = ""
            if raw_name:
                npc_id = self._npc_name_to_id.get(raw_name, "")
                if not npc_id:
                    # 尝试从动态 NPC state 中按名字查找
                    for _sid, _sn in state.get("npcs", {}).items():
                        if isinstance(_sn, dict) and _sn.get("name") == raw_name:
                            npc_id = _sid
                            break
            if not npc_id and raw_id:
                if raw_id in self._npc_by_id or raw_id in state.get("npcs", {}):
                    npc_id = raw_id
            if not npc_id:
                continue
            if npc_id in _offscreen_seen_ids:
                continue
            _offscreen_seen_ids.add(npc_id)
            log = state.setdefault("npc_offscreen_log", {})
            npc_log = log.setdefault(npc_id, [])
            npc_log.append({
                "turn": self.turn_number,
                "time": state.get("game_time", ""),
                "action": update.get("action", ""),
                "location": update.get("location", ""),
                "mood": update.get("mood", ""),
            })
            if len(npc_log) > 20:
                log[npc_id] = npc_log[-15:]
            new_loc = update.get("location", "")
            if new_loc:
                npc_st = state.get("npcs", {}).get(npc_id)
                if isinstance(npc_st, dict):
                    npc_st.pop("_derived_location", None)
                    if npc_st.get("current_room") == npc_st.get("_derived_room"):
                        npc_st.pop("current_room", None)
                    npc_st.pop("_derived_room", None)
                    npc_st["current_location"] = new_loc
            self._sync_offscreen_to_lorebook(npc_id, log.get(npc_id, []))

        self._apply_entity_spatial_changes(state, parsed)

        # Dynamic new NPCs
        script_npc_ids = {n["id"] for n in self.script.get("npcs", []) if n.get("id")}
        script_npc_names = {n["name"]: n["id"] for n in self.script.get("npcs", [])
                            if n.get("id") and n.get("name")}
        current_pc_id = state.get("player", {}).get("id", "player")
        current_pc_name = state.get("player", {}).get("name", "")
        for new_npc in parsed.get("new_npcs", []):
            npc_id = new_npc.get("id", "")
            if not npc_id:
                continue
            # Skip if this is the current PC
            if npc_id == current_pc_id:
                continue
            npcs_dict = state.setdefault("npcs", {})
            if npc_id in npcs_dict:
                continue
            if npc_id in self._initial_npc_ids:
                continue
            # 按 name 去重：AI 可能给同一 NPC 生成不同 id
            new_name = new_npc.get("name", npc_id)
            existing_names = {
                nd.get("name", "") for nd in npcs_dict.values() if isinstance(nd, dict)
            }
            if new_name in existing_names or (new_name in script_npc_names
                                              and script_npc_names[new_name] != npc_id):
                continue
            # Skip if name matches current PC
            if new_name == current_pc_name:
                continue
            npcs_dict[npc_id] = {
                "name": new_npc.get("name", npc_id),
                "attitude_toward_player": new_npc.get("attitude_toward_player", 50),
                "known": new_npc.get("known", True),
                "met": new_npc.get("met", new_npc.get("known", True)),
                "default_location": new_npc.get("location", ""),
                "current_location": new_npc.get("location", ""),
                "bio": new_npc.get("bio", ""),
                "personality": new_npc.get("personality", ""),
                "capabilities": new_npc.get("capabilities", ""),
                "title": new_npc.get("title", ""),
                "organizations": new_npc.get("organizations") or self._migrate_npc_org_fields(new_npc),
                "superior": new_npc.get("superior", ""),
            }
            state.setdefault("display_names", {})[npc_id] = new_npc.get("name", npc_id)
            rels = state.get("player", {}).setdefault("relationships", {})
            if npc_id not in rels:
                att = new_npc.get("attitude_toward_player", 50)
                # Use AI-provided 3D values if present, else fall back to personality heuristic
                ai_trust = new_npc.get("trust")
                ai_affection = new_npc.get("affection")
                ai_fear = new_npc.get("fear")
                if ai_trust is not None or ai_affection is not None or ai_fear is not None:
                    trust = max(0, min(100, int(ai_trust or att)))
                    affection = max(0, min(100, int(ai_affection or att)))
                    fear = max(0, min(100, int(ai_fear or 0)))
                else:
                    personality = (new_npc.get("personality", "") or "").lower()
                    trust = att
                    affection = att
                    fear = 0
                    if any(w in personality for w in ("冷", "警惕", "多疑", "严厉", "冷酷")):
                        trust = max(0, att - 15)
                        affection = max(0, att - 10)
                    elif any(w in personality for w in ("热情", "友善", "善良", "温和", "开朗")):
                        affection = min(100, att + 10)
                    if any(w in personality for w in ("威严", "强势", "暴力", "危险", "凶")):
                        fear = min(40, max(10, 60 - att))
                rels[npc_id] = {"trust": trust, "affection": affection, "fear": fear}
            # 同步 script 和 prompt_builder 缓存
            script_npcs = self.script.setdefault("npcs", [])
            if not any(n.get("id") == npc_id for n in script_npcs):
                script_npcs.append(new_npc)
            self._npc_by_id[npc_id] = new_npc
            self._npc_name_to_id[new_name] = npc_id
            self.prompt_builder._npc_name_map[npc_id] = new_npc.get("name", npc_id)
            self.prompt_builder._npc_by_id[npc_id] = new_npc
            dirty_npc_ids.add(npc_id)

        # Generate KG lorebook entries for newly created NPCs
        new_npc_ids = [n.get("id", "") for n in parsed.get("new_npcs", []) if n.get("id")]
        new_npc_ids = [nid for nid in new_npc_ids if nid in state.get("npcs", {})]
        if new_npc_ids and self.prompt_builder._has_kg:
            self.prompt_builder.refresh_kg_entries(new_npc_ids, [])
            if self.vector_memory:
                self._sync_kg_entries_to_vector(new_npc_ids, [])

        # NPC met changes (认识→熟识)
        for mc in parsed.get("npc_met_changes", []):
            npc_id = mc.get("npc_id", "")
            if not npc_id:
                continue
            npc_st = state.get("npcs", {}).get(npc_id)
            if isinstance(npc_st, dict):
                if mc.get("met") is not None:
                    npc_st["met"] = bool(mc["met"])
                if mc.get("known") is not None:
                    npc_st["known"] = bool(mc["known"])

        # NPC-NPC relationship updates
        npc_rel_updates = parsed.get("npc_relationship_updates", [])
        if npc_rel_updates:
            self._apply_npc_relationship_updates(npc_rel_updates, state=state)

        scene_details = parsed.get("scene_details")
        if scene_details and isinstance(scene_details, dict):
            state["scene_details"] = scene_details
            loc_id = state.get("player", {}).get("location", "")
            if loc_id and self.prompt_builder.lorebook:
                self.prompt_builder.update_location_scene(loc_id, scene_details)

        for frc in parsed.get("faction_reputation_changes", []):
            fid = frc.get("faction_id", "")
            if not fid:
                continue
            change = frc.get("change", 0)
            rep = state.setdefault("faction_reputation", {})
            entry = rep.setdefault(fid, {"value": 50})
            entry["value"] = max(0, min(100, entry.get("value", 50) + change))
            entry["title"] = self._reputation_title(entry["value"])
            if frc.get("reason"):
                entry["last_reason"] = frc["reason"]

        # Moral alignment changes
        for mac in parsed.get("moral_alignment_changes", []):
            axis = mac.get("axis", "")
            change = mac.get("change", 0)
            if not axis or not change:
                continue
            ma = state.setdefault("moral_alignment", {
                "mercy_vs_cruelty": 0,
                "honesty_vs_deception": 0,
                "order_vs_chaos": 0,
            })
            if axis in ma:
                ma[axis] = max(-100, min(100, ma[axis] + change))

        self._sync_npc_fields_to_script(state, dirty_npc_ids=dirty_npc_ids)

        # Companion changes from AI
        for recruit in parsed.get("recruit_companions", []):
            cid = recruit if isinstance(recruit, str) else recruit.get("npc_id", "")
            if not cid:
                continue
            companions = state.setdefault("companions", [])
            if cid in companions:
                continue
            npc_st = state.get("npcs", {}).get(cid)
            if not isinstance(npc_st, dict):
                continue
            companions.append(cid)
            loyalty = state.setdefault("companion_loyalty", {})
            loyalty.setdefault(cid, {"value": 50})
            npc_name = npc_st.get("name", cid)
            state.setdefault("_companion_events", []).append({
                "npc_id": cid, "name": npc_name, "event": "join",
            })
        for dismiss in parsed.get("dismiss_companions", []):
            cid = dismiss if isinstance(dismiss, str) else dismiss.get("npc_id", "")
            companions = state.get("companions", [])
            if cid in companions:
                companions.remove(cid)
                npc_st = state.get("npcs", {}).get(cid, {})
                npc_name = npc_st.get("name", cid) if isinstance(npc_st, dict) else cid
                state.setdefault("_companion_events", []).append({
                    "npc_id": cid, "name": npc_name, "event": "leave",
                })

        # Sync attitude for all dirty NPCs (state_changes or attitude_changes touched relationships)
        self._sync_dirty_npc_attitudes(state, dirty_npc_ids)
        self._apply_npc_lore_reveals(state, parsed, present_npc_ids)

        return state, all_changes

    def _apply_npc_lore_reveals(self, state, parsed, present_npc_ids,
                                *, turn_start_time=None, turn_end_time=None):
        for claim in parsed.get("lore_reveals", []):
            entry = self._validated_npc_lore_reveal(
                state, claim, parsed.get("narrative", ""), present_npc_ids,
                turn_start_time=turn_start_time, turn_end_time=turn_end_time,
            )
            if entry is None:
                continue
            discovered = state.setdefault("pc_discovered_lore", [])
            if entry.id in discovered:
                continue
            clues = state.setdefault("pc_lore_clues", {}).setdefault(entry.id, [])
            evidence = claim["evidence"]
            if evidence not in clues:
                clues.append(evidence)
            reveal_facts = entry.discoverable.get("reveal_facts")
            if (isinstance(reveal_facts, list) and reveal_facts
                    and all(isinstance(fact, str) and fact.strip()
                            and any(fact in clue for clue in clues) for fact in reveal_facts)):
                discovered.append(entry.id)
                state["pc_lore_clues"].pop(entry.id)
                logger.info("Lore discovered: %s (source=npc_reveal:%s)",
                            entry.id, entry.discoverable["npc_source"])

    def _apply_npc_event_reports(self, state, parsed, present_npc_ids,
                                 *, turn_start_time=None, turn_end_time=None):
        claims = parsed.get("event_reports")
        if not isinstance(claims, list):
            return
        start = parse_time(turn_start_time or state.get("game_time"))
        end = parse_time(turn_end_time)
        if not start or not end:
            return
        fired = set(state.get("fired_one_time_events", []))
        events = {event["id"]: event for event in self.script.get("one_time_events", [])
                  if isinstance(event, dict) and event.get("id")}
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            event_id = claim.get("event_id")
            if not isinstance(event_id, str):
                continue
            event = events.get(event_id)
            if not event or event_id not in fired:
                continue
            fire_time = parse_time(event.get("trigger_time"))
            claimed_time = claim.get("occurred_at")
            if claimed_time is not None and not isinstance(claimed_time, str):
                continue
            heard_at = parse_time(claimed_time)
            if claimed_time and not heard_at:
                continue
            if not fire_time or fire_time > end:
                continue
            anchor = ""
            if fire_time >= start:
                if not heard_at or not fire_time < heard_at <= end:
                    continue
                anchor = heard_at.strftime("%Y年%m月%d日%H:%M")
            elif heard_at and not start <= heard_at <= end:
                continue
            npc_id = claim.get("npc_id")
            evidence = claim.get("evidence")
            if (not isinstance(npc_id, str) or npc_id not in (present_npc_ids or [])
                    or not isinstance(evidence, str) or len(evidence) < 12
                    or not evidence.startswith("“") or not evidence.endswith("”")
                    or not self._npc_quote_in_narrative(
                        state, npc_id, evidence, parsed.get("narrative", ""), anchor)):
                continue
            if not any(
                isinstance(info, dict) and info.get("source_event_id") == event_id
                and npc_id in info.get("known_by", [])
                and isinstance(info.get("known_at"), dict)
                and (known_at := parse_time(info["known_at"].get(npc_id)))
                and fire_time <= known_at <= (heard_at or end)
                for info in state.get("information_network", [])
            ):
                continue
            event_text = " ".join([str(event.get("description") or ""),
                                   *(str(item) for item in event.get("narrative_claims", []))])
            segments = re.findall(r"[\u3400-\u9fff]{4,}", evidence)
            if not any(segment[index:index + 4] in event_text
                       for segment in segments for index in range(len(segment) - 3)):
                continue
            reports = state.setdefault("pc_event_reports", [])
            if any(report.get("event_id") == event_id and report.get("npc_id") == npc_id
                   and report.get("evidence") == evidence for report in reports):
                continue
            npc = state.get("npcs", {}).get(npc_id, {})
            reports.append({
                "event_id": event_id,
                "npc_id": npc_id,
                "npc_name": npc.get("name") or self._npc_by_id.get(npc_id, {}).get("name", npc_id),
                "evidence": evidence,
                "heard_at": claim.get("occurred_at") or turn_end_time,
            })
            if len(reports) > 30:
                state["pc_event_reports"] = reports[-30:]

    @staticmethod
    def _future_lore_reveal_claims(parsed, end_time):
        finish = parse_time(end_time)
        if not finish:
            return []
        return [str(claim.get("entry_id", "?")) for claim in parsed.get("lore_reveals", [])
                if isinstance(claim, dict) and claim.get("occurred_at")
                and (occurred_at := parse_time(claim["occurred_at"])) and occurred_at > finish]

    def _validated_npc_lore_reveal(self, state, claim, narrative, present_npc_ids,
                                    *, require_attitude=True, turn_start_time=None,
                                    turn_end_time=None, lorebook=None):
        lorebook = self.prompt_builder.lorebook if lorebook is None else lorebook
        if not isinstance(claim, dict) or not lorebook:
            return None
        entry = next((entry for entry in lorebook.entries
                      if entry.id == claim.get("entry_id")), None)
        if not entry or not entry.enabled or entry.visibility != "hidden":
            return None
        if entry.discoverable.get("method") != "npc_reveal":
            return None
        occurrence_anchor = ""
        current_time = parse_time(turn_start_time or state.get("game_time"))
        if entry.available_from:
            available_from = parse_time(entry.available_from)
            if current_time is None or available_from is None:
                return None
            if current_time < available_from:
                occurred_at = parse_time(claim.get("occurred_at"))
                end_time = parse_time(turn_end_time)
                if not occurred_at or not end_time or not available_from <= occurred_at <= end_time:
                    return None
                occurrence_anchor = occurred_at.strftime("%Y年%m月%d日%H:%M")
        npc_id = entry.discoverable.get("npc_source", "")
        if not npc_id or npc_id not in (present_npc_ids or []):
            return None
        npc_state = state.get("npcs", {}).get(npc_id, {})
        if not isinstance(npc_state, dict):
            return None
        if require_attitude and npc_state.get("attitude_toward_player", 50) < entry.discoverable.get("attitude_threshold", 60):
            return None
        evidence = claim.get("evidence", "")
        if (not isinstance(evidence, str) or len(evidence) < 12
                or not evidence.startswith("“") or not evidence.endswith("”")
                or not isinstance(narrative, str)):
            return None
        npc_name = npc_state.get("name") or self._npc_by_id.get(npc_id, {}).get("name", npc_id)
        if not self._npc_quote_in_narrative(state, npc_id, evidence, narrative, occurrence_anchor):
            return None
        keywords = (*entry.keys, *entry.secondary_keys)
        matched_key = any(keyword != npc_name and len(keyword) >= 2
                          and keyword in evidence and keyword in entry.content for keyword in keywords)
        authored_content = entry.content.replace(npc_name, "")
        evidence_segments = re.findall(r"[\u3400-\u9fff]{4,}", evidence.replace(npc_name, ""))
        grounded_phrases = {
            segment[index:index + 4]
            for segment in evidence_segments
            for index in range(len(segment) - 3)
            if segment[index:index + 4] in authored_content
        }
        if not matched_key and len(grounded_phrases) < 2:
            return None
        return entry

    def _npc_quote_in_narrative(self, state, npc_id, evidence, narrative, occurrence_anchor=""):
        if not isinstance(narrative, str):
            return False
        speakers = []
        for candidate_id, candidate_state in state.get("npcs", {}).items():
            candidate_name = (candidate_state.get("name") if isinstance(candidate_state, dict) else "") or self._npc_by_id.get(candidate_id, {}).get("name", "")
            if candidate_name:
                speakers.append((candidate_id, candidate_name))
        for quote_match in re.finditer(re.escape(evidence), narrative):
            preceding = narrative[max(0, quote_match.start() - 120):quote_match.start()]
            sentence = re.split(r"[。！？\n]", preceding)[-1]
            if occurrence_anchor and occurrence_anchor not in sentence:
                continue
            clause = re.split(r"[，,；;]", sentence)[-1]
            if re.search(r"(?:对|向|跟|冲着)[^，,；;：:]{1,30}(?:说|道|问|喊|答|告诉)\s*[：:]\s*$", clause):
                clause = re.split(r"(?:对|向|跟|冲着)", clause, maxsplit=1)[0]
            speaker_context = clause if any(name in clause for _, name in speakers) else sentence
            last_speaker = max(((speaker_context.rfind(name), speaker_id)
                                for speaker_id, name in speakers if name in speaker_context),
                               default=(-1, ""))[1]
            if last_speaker == npc_id:
                return True
        return False

    def _apply_inventory_change(self: GameSession, inv: dict):
        """Apply an inventory change (add/remove item)."""
        item_name = inv.get("item", "")
        if not item_name:
            return
        action = inv.get("action", "add")
        quantity = inv.get("quantity", 1)
        description = inv.get("description", "")
        inventory = self.current_state.setdefault("inventory", [])

        if action == "add":
            # Check if item already exists (fuzzy: also match substring)
            exact = None
            fuzzy = None
            for entry in inventory:
                ename = entry.get("item", "")
                if ename == item_name:
                    exact = entry
                    break
                if not fuzzy and (item_name in ename or ename in item_name):
                    fuzzy = entry
            target = exact or fuzzy
            if target:
                target["quantity"] = target.get("quantity", 1) + quantity
                if description:
                    target["description"] = description
                return
            new_entry = {"item": item_name, "quantity": quantity}
            if description:
                new_entry["description"] = description
            inventory.append(new_entry)
        elif action == "remove":
            exact = None
            fuzzy = None
            for entry in inventory:
                ename = entry.get("item", "")
                if ename == item_name:
                    exact = entry
                    break
                if not fuzzy and (item_name in ename or ename in item_name):
                    fuzzy = entry
            target = exact or fuzzy
            if target:
                target["quantity"] = target.get("quantity", 1) - quantity
                if target["quantity"] <= 0:
                    inventory.remove(target)
                return

    def _apply_weather(self: GameSession, dice_results: list):
        for dr in dice_results:
            item_id = dr.random_item_id
            if item_id == "weather" and dr.range_label:
                self.current_state["current_weather"] = dr.range_label

    def _get_event_description(self: GameSession, event_id: str) -> str:
        """Look up event description from script by ID."""
        return self._event_desc_by_id.get(event_id, event_id)

    def _dice_result_to_dict(self: GameSession, dr) -> dict:
        item_id = dr.random_item_id
        # Look up description and trigger_type from script
        duration_info = ""
        item = self._random_item_by_id.get(item_id)
        description = item.get("description", item_id) if item else item_id
        trigger_type = item.get("trigger_type", "") if item else ""
        # Generate source label for prompt clarity
        source_labels = {
            "always": "环境骰子",
            "conditional": "条件骰子",
            "event_linked": "事件骰子",
        }
        source_label = source_labels.get(trigger_type, description)
        # Add sustained info
        sustained = dr._sustained
        ri_state = self.current_state.get("random_item_state", {}).get(item_id, {})
        remaining = ri_state.get("remaining_turns", 0)
        if sustained:
            duration_info = f"(持续中，剩余{remaining}回合)"
        return {
            "random_item_id": item_id,
            "description": description,
            "source_label": source_label,
            "formula": dr.formula,
            "raw_rolls": dr.raw_rolls,
            "total": dr.total,
            "range_label": dr.range_label,
            "sustained": sustained,
            "remaining_turns": remaining,
            "duration_info": duration_info,
        }

    def _apply_weather_effects(self: GameSession):
        """Apply gameplay effects based on current weather."""
        weather = self.current_state.get("current_weather", "")

        # Remove old weather states
        active = self.current_state.get("active_persistent_states", [])
        weather_state_ids = {"weather_extreme", "weather_storm", "weather_rain"}.difference(self._ps_by_id)
        self.current_state["active_persistent_states"] = [
            s for s in active if s not in weather_state_ids
        ]

        if not weather:
            return

        # Keyword-based matching instead of exact string match
        if any(k in weather for k in ("极端", "灾")):
            weather_state = "weather_extreme"
        elif any(k in weather for k in ("暴雨", "大雪", "暴风", "台风", "冰雹")):
            weather_state = "weather_storm"
        elif any(k in weather for k in ("阴雨", "小雨", "雨", "雪", "雾")):
            weather_state = "weather_rain"
        else:
            return
        if weather_state in weather_state_ids:
            self.current_state["active_persistent_states"].append(weather_state)

    def _check_attribute_thresholds(self: GameSession, state_changes: list[dict]) -> list[dict]:
        """Check if any attribute crossed a threshold after state changes."""
        events = []
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})

        for change in state_changes:
            target = change.get("target", "")
            # Extract attribute name from target like "player.attributes.health" or "player.health"
            attr_name = target.split(".")[-1]

            rule = attr_rules.get(attr_name, {})
            if not isinstance(rule, dict):
                continue

            thresholds = rule.get("thresholds", [])
            old_val = change.get("old", 0)
            new_val = change.get("new", 0)
            if not isinstance(old_val, (int, float)):
                old_val = 0
            if not isinstance(new_val, (int, float)):
                new_val = 0

            for th in thresholds:
                th_val = th.get("value", 0)
                th_dir = th.get("direction", "below")

                # Check if the threshold was crossed
                if th_dir == "below" and old_val > th_val and new_val <= th_val:
                    events.append({
                        "attribute": attr_name,
                        "direction": "below",
                        "threshold": th_val,
                        "value": new_val,
                        "description": th.get("description", f"{attr_name}降至危险水平"),
                        "activate_state": th.get("activate_state"),
                    })
                    if th.get("activate_state"):
                        active = self.current_state.setdefault("active_persistent_states", [])
                        if th["activate_state"] not in active:
                            active.append(th["activate_state"])

                elif th_dir == "below" and old_val <= th_val and new_val > th_val:
                    if th.get("activate_state"):
                        active = self.current_state.get("active_persistent_states", [])
                        if th["activate_state"] in active:
                            active.remove(th["activate_state"])
                            events.append({
                                "attribute": attr_name,
                                "direction": "above",
                                "threshold": th_val,
                                "value": new_val,
                                "description": th.get("description", f"{attr_name}恢复正常"),
                                "deactivate_state": th["activate_state"],
                            })

                elif th_dir == "above" and old_val < th_val and new_val >= th_val:
                    events.append({
                        "attribute": attr_name,
                        "direction": "above",
                        "threshold": th_val,
                        "value": new_val,
                        "description": th.get("description", f"{attr_name}达到高水平"),
                        "activate_state": th.get("activate_state"),
                    })
                    if th.get("activate_state"):
                        active = self.current_state.setdefault("active_persistent_states", [])
                        if th["activate_state"] not in active:
                            active.append(th["activate_state"])

                elif th_dir == "above" and old_val >= th_val and new_val < th_val:
                    if th.get("activate_state"):
                        active = self.current_state.get("active_persistent_states", [])
                        if th["activate_state"] in active:
                            active.remove(th["activate_state"])
                            events.append({
                                "attribute": attr_name,
                                "direction": "below",
                                "threshold": th_val,
                                "value": new_val,
                                "description": th.get("description", f"{attr_name}不再达标"),
                                "deactivate_state": th["activate_state"],
                            })

        return events

    def _evaluate_conditional_result(self: GameSession, result: dict) -> dict:
        """Evaluate a conditional result by rolling dice.

        Conditional result format:
          {"type": "conditional", "dice": {...}, "success": {...}, "failure": {...},
           "threshold": 50, "description": "..."}
        """
        dice_config = result.get("dice", {"formula": "1d100"})
        threshold = result.get("threshold", 50)

        roll_result = self.dice.roll_and_resolve(dice_config, [])
        total = roll_result.total

        if total >= threshold:
            outcome = result.get("success", {})
        else:
            outcome = result.get("failure", {})

        # Merge the outcome with the base result info
        evaluated = {
            "description": outcome.get("description", result.get("description", "")),
            "state_changes": outcome.get("state_changes", []),
            "conditional_roll": total,
            "conditional_threshold": threshold,
            "conditional_success": total >= threshold,
        }
        return evaluated
