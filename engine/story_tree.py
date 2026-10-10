"""Story Tree: branching narrative control system (focus tree / national focus tree)."""

from __future__ import annotations

import re
import operator
from dataclasses import dataclass, field
from typing import Callable

_OPS = {
    ">=": operator.ge, "<=": operator.le,
    ">": operator.gt, "<": operator.lt,
    "==": operator.eq, "!=": operator.ne,
}
_COND_RE = re.compile(r'([\w.]+)\s*(>=|<=|>|<|==|!=)\s*(.+)')


@dataclass
class StoryTreeResult:
    newly_completed: list[dict] = field(default_factory=list)
    newly_active: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    notification_sources: list[str] = field(default_factory=list)
    inject_prompts: list[str] = field(default_factory=list)
    lore_activations: list[str] = field(default_factory=list)
    lore_deactivations: list[str] = field(default_factory=list)
    lore_additions: list[dict] = field(default_factory=list)
    lore_updates: list[dict] = field(default_factory=list)
    lore_removals: list[str] = field(default_factory=list)
    game_events: list[str] = field(default_factory=list)
    narrative_callbacks: list[dict] = field(default_factory=list)
    state_activations: list[str] = field(default_factory=list)


class StoryTreeEngine:

    def __init__(self, story_tree_def: dict):
        self.trees: list[dict] = story_tree_def.get("trees", [])
        self._nodes: dict[str, dict] = {}
        self._tree_for_node: dict[str, str] = {}
        for tree in self.trees:
            for node in tree.get("nodes", []):
                self._nodes[node["id"]] = node
                self._tree_for_node[node["id"]] = tree["id"]

    def _init_state(self, state: dict) -> dict:
        sts = state.setdefault("story_tree_state", {})
        sts.setdefault("completed", [])
        sts.setdefault("active", [])
        sts.setdefault("choices_made", {})
        sts.setdefault("timed_progress", {})
        sts.setdefault("unlocked", [])
        return sts

    def evaluate(
        self,
        state: dict,
        turn_number: int,
        condition_eval: Callable[[str], bool] | None = None,
        active_events: list[str] | None = None,
        advance_timed: bool = True,
    ) -> StoryTreeResult:
        sts = self._init_state(state)
        completed = set(sts["completed"])
        active = set(sts["active"])
        unlocked = set(sts["unlocked"])
        result = StoryTreeResult()
        periodic_eligible: list[dict] = []

        for tree in self.trees:
            for node in tree.get("nodes", []):
                nid = node["id"]
                ntype = node.get("type", "auto")

                # trigger nodes: fire on matching lifecycle events, skip otherwise
                if ntype == "trigger":
                    node_events = node.get("event", [])
                    if isinstance(node_events, str):
                        node_events = [node_events]
                    if not any(e in (active_events or []) for e in node_events):
                        continue
                    condition = node.get("condition", "")
                    if condition and condition_eval and not condition_eval(condition):
                        continue
                    self._apply_effects(node.get("effects", {}), result, nid)
                    continue

                if nid in completed:
                    # periodic repeatable: reset after cooldown expires
                    if ntype == "periodic" and node.get("repeatable", True):
                        cd_until = sts.get("periodic_cooldowns", {}).get(nid, 0)
                        if turn_number >= cd_until:
                            completed.discard(nid)
                            sts["completed"] = list(completed)
                        else:
                            continue
                    else:
                        continue

                requires = [r for r in node.get("requires", []) if isinstance(r, str)]
                if requires and not all(r in completed for r in requires):
                    continue

                condition = node.get("condition", "")
                if condition and condition_eval and not condition_eval(condition):
                    continue

                if nid not in unlocked:
                    if requires or node.get("activate_events"):
                        continue
                    unlocked.add(nid)
                    sts["unlocked"] = list(unlocked)

                if ntype == "auto":
                    self._complete_node(node, sts, result, completed, unlocked)

                elif ntype == "timed":
                    tp = sts["timed_progress"]
                    if nid not in tp:
                        tp[nid] = {
                            "started_turn": turn_number,
                            "remaining": node.get("duration_turns", 3),
                        }
                        if nid not in active:
                            active.add(nid)
                            sts["active"] = list(active)
                            result.newly_active.append(node)
                    elif (advance_timed
                          and turn_number > tp[nid]["started_turn"]
                          and turn_number > tp[nid].get("last_advanced_turn", tp[nid]["started_turn"])):
                        tp[nid]["last_advanced_turn"] = turn_number
                        tp[nid]["remaining"] -= 1
                        if tp[nid]["remaining"] <= 0:
                            del tp[nid]
                            self._complete_node(node, sts, result, completed, unlocked)
                            active.discard(nid)
                            sts["active"] = list(active)

                elif ntype in ("choice", "quest"):
                    if nid not in active:
                        active.add(nid)
                        sts["active"] = list(active)
                        result.newly_active.append(node)
                        if ntype == "choice" and node.get("auto_resolve"):
                            chosen_c = self._auto_resolve_choice(node, condition_eval)
                            if chosen_c:
                                sts["choices_made"][nid] = chosen_c["id"]
                                self._complete_node(node, sts, result, completed, unlocked)
                                self._apply_effects(chosen_c.get("effects", {}), result, nid)
                                for uid in chosen_c.get("unlock", []):
                                    unlocked.add(uid)
                                sts["unlocked"] = list(unlocked)
                                active.discard(nid)
                                sts["active"] = list(active)
                        elif ntype == "choice":
                            notify = f"【剧情分支】{node.get('name', nid)} — 请在剧情树中做出选择"
                            result.notifications.append(notify)
                            result.notification_sources.append(nid)

                elif ntype == "periodic":
                    cd_until = sts.get("periodic_cooldowns", {}).get(nid, 0)
                    if turn_number < cd_until:
                        continue
                    periodic_eligible.append(node)

                elif ntype == "pending_review":
                    if nid not in active:
                        active.add(nid)
                        sts["active"] = list(active)
                        result.newly_active.append(node)

        # Periodic: weighted random selection
        if periodic_eligible:
            import random
            weights = [n.get("weight", 10) for n in periodic_eligible]
            chosen = random.choices(periodic_eligible, weights=weights, k=1)[0]
            self._apply_effects(chosen.get("effects", {}), result, chosen["id"])
            cd = chosen.get("cooldown", 5)
            sts.setdefault("periodic_cooldowns", {})[chosen["id"]] = turn_number + cd
            if not chosen.get("repeatable", True):
                self._complete_node(chosen, sts, result, completed, unlocked)
            else:
                completed.add(chosen["id"])
                sts["completed"] = list(completed)
                result.newly_completed.append(chosen)

        return result

    def make_choice(
        self,
        state: dict,
        node_id: str,
        choice_id: str,
        condition_eval: Callable[[str], bool] | None = None,
    ) -> StoryTreeResult:
        sts = self._init_state(state)
        node = self._nodes.get(node_id)
        if not node:
            return StoryTreeResult()

        if node_id not in sts["active"]:
            return StoryTreeResult()

        choices = node.get("choices", [])
        chosen = None
        for c in choices:
            if c["id"] == choice_id:
                chosen = c
                break
        if not chosen:
            return StoryTreeResult()

        choice_cond = chosen.get("condition", "")
        if choice_cond and condition_eval and not condition_eval(choice_cond):
            return StoryTreeResult()

        sts["choices_made"][node_id] = choice_id

        completed = set(sts["completed"])
        unlocked = set(sts["unlocked"])
        active = set(sts["active"])
        result = StoryTreeResult()

        self._complete_node(node, sts, result, completed, unlocked)
        self._apply_effects(chosen.get("effects", {}), result, node_id)

        for uid in chosen.get("unlock", []):
            unlocked.add(uid)
        sts["unlocked"] = list(unlocked)

        active.discard(node_id)
        sts["active"] = list(active)

        return result

    def complete_quest(self, state: dict, node_id: str) -> StoryTreeResult:
        sts = self._init_state(state)
        node = self._nodes.get(node_id)
        if not node or node.get("type") != "quest":
            return StoryTreeResult()
        if node_id not in sts["active"]:
            return StoryTreeResult()

        completed = set(sts["completed"])
        unlocked = set(sts["unlocked"])
        active = set(sts["active"])
        result = StoryTreeResult()

        self._complete_node(node, sts, result, completed, unlocked)
        active.discard(node_id)
        sts["active"] = list(active)

        return result

    def fire_event(
        self,
        event: str,
        state: dict,
        condition_eval: Callable[[str], bool] | None = None,
    ) -> StoryTreeResult:
        """Unlock nodes whose activate_events contains the given event name."""
        sts = self._init_state(state)
        completed = set(sts["completed"])
        unlocked = set(sts["unlocked"])
        result = StoryTreeResult()
        changed = False

        for tree in self.trees:
            for node in tree.get("nodes", []):
                nid = node["id"]
                if nid in completed or nid in unlocked:
                    continue
                events = node.get("activate_events", [])
                if event in events:
                    condition = node.get("condition", "")
                    if condition and condition_eval and not condition_eval(condition):
                        continue
                    unlocked.add(nid)
                    changed = True

        if changed:
            sts["unlocked"] = list(unlocked)
        return result

    @staticmethod
    def _auto_resolve_choice(
        node: dict,
        condition_eval: Callable[[str], bool] | None,
    ) -> dict | None:
        """Pick the first choice whose condition is met, or the last choice as fallback."""
        choices = node.get("choices", [])
        if not choices:
            return None
        for c in choices:
            cond = c.get("condition", "")
            if not cond:
                continue
            if condition_eval and condition_eval(cond):
                return c
        # Fallback: last choice (should be the unconditional default)
        return choices[-1]

    def _complete_node(
        self,
        node: dict,
        sts: dict,
        result: StoryTreeResult,
        completed: set,
        unlocked: set,
    ):
        nid = node["id"]
        completed.add(nid)
        sts["completed"] = list(completed)
        result.newly_completed.append(node)

        self._apply_effects(node.get("effects", {}), result, nid)

        for uid in node.get("on_complete_unlock", []):
            unlocked.add(uid)
        sts["unlocked"] = list(unlocked)

    @staticmethod
    def _apply_effects(effects: dict, result: StoryTreeResult, source_id: str):
        if not effects:
            return

        for sv in effects.get("set_var", []):
            result.effects.append({"action": "set_var", "params": sv})

        for eid in effects.get("activate_lore", []):
            result.lore_activations.append(eid)

        for eid in effects.get("deactivate_lore", []):
            result.lore_deactivations.append(eid)

        for entry in effects.get("add_lore", []):
            result.lore_additions.append(entry)

        for upd in effects.get("update_lore", []):
            result.lore_updates.append(upd)

        for rid in effects.get("remove_lore", []):
            result.lore_removals.append(rid)

        prompt = effects.get("inject_prompt", "")
        if prompt:
            result.inject_prompts.append(prompt)

        notify = effects.get("notify", "")
        if notify:
            result.notifications.append(notify)
            result.notification_sources.append(source_id)

        for loc in effects.get("unlock_locations", []):
            result.effects.append({"action": "unlock_location", "params": {"location_id": loc}})

        for npc in effects.get("reveal_npcs", []):
            result.effects.append({"action": "reveal_npc", "params": {"npc_id": npc}})

        for rep in effects.get("set_reputation", []):
            result.effects.append({"action": "set_reputation", "params": rep})

        for ev in effects.get("fire_events", []):
            result.game_events.append(ev)

        for uid in effects.get("unlock_nodes", []):
            result.effects.append({"action": "unlock_node", "params": {"node_id": uid}})

        cb = effects.get("narrative_callback")
        if cb:
            result.narrative_callbacks.append(
                cb if isinstance(cb, dict) else {"text": str(cb), "priority": "medium"}
            )

        for sid in effects.get("activate_state", []):
            result.state_activations.append(sid)

        for sc in effects.get("state_change", []):
            result.effects.append({"action": "set_var", "params": sc})

    def get_upcoming_nodes(self, state: dict, limit: int = 3) -> list[dict]:
        """返回即将可解锁的节点（前置条件已满足，但尚未 unlock/complete/active）。"""
        sts = self._init_state(state)
        completed = set(sts["completed"])
        unlocked = set(sts["unlocked"])
        active = set(sts["active"])
        upcoming = []
        for tree in self.trees:
            for node in tree.get("nodes", []):
                nid = node["id"]
                if nid in completed or nid in unlocked or nid in active:
                    continue
                if node.get("type") == "trigger":
                    continue
                requires = [r for r in node.get("requires", []) if isinstance(r, str)]
                if requires and all(r in completed for r in requires):
                    upcoming.append(node)
        return upcoming[:limit]

    def get_visible_trees(self, state: dict, turn_number: int = 0) -> list[dict]:
        sts = self._init_state(state)
        completed = set(sts["completed"])
        active = set(sts["active"])
        unlocked = set(sts["unlocked"])
        choices_made = sts.get("choices_made", {})
        timed = sts.get("timed_progress", {})
        periodic_cds = sts.get("periodic_cooldowns", {})

        visible = []
        for tree in self.trees:
            tree_nodes = []
            for node in tree.get("nodes", []):
                nid = node["id"]
                if nid in completed:
                    status = "completed"
                elif nid in active:
                    status = "active"
                elif nid in unlocked:
                    status = "available"
                else:
                    status = "locked"

                node_view = {
                    "id": nid,
                    "name": node.get("name", nid),
                    "description": node.get("description", ""),
                    "type": node.get("type", "auto"),
                    "status": status,
                    "position": node.get("position", {"x": 0, "y": 0}),
                    "requires": node.get("requires", []),
                    "on_complete_unlock": node.get("on_complete_unlock", []),
                    "activate_events": node.get("activate_events", []),
                    "effects": {"fire_events": node.get("effects", {}).get("fire_events", [])},
                    "related_npcs": node.get("related_npcs", []),
                    "related_orgs": node.get("related_orgs", []),
                }

                if node.get("type") == "choice":
                    chosen = choices_made.get(nid)
                    view_choices = []
                    for c in node.get("choices", []):
                        view_choices.append({
                            "id": c["id"],
                            "label": c.get("label", c["id"]),
                            "description": c.get("description", ""),
                            "chosen": c["id"] == chosen,
                            "unlock": c.get("unlock", []),
                        })
                    node_view["choices"] = view_choices

                if node.get("type") == "timed" and nid in timed:
                    total = node.get("duration_turns", 3)
                    remaining = timed[nid].get("remaining", 0)
                    node_view["progress"] = {
                        "total": total,
                        "remaining": remaining,
                        "percent": max(0, min(100, int((total - remaining) / total * 100))) if total > 0 else 100,
                    }

                if node.get("type") == "periodic":
                    if nid in completed and node.get("repeatable", True):
                        cd = periodic_cds.get(nid, 0)
                        status = "available" if turn_number >= cd else "completed"
                        node_view["status"] = status
                    cd_remain = periodic_cds.get(nid, 0) - turn_number
                    if cd_remain > 0:
                        node_view["cooldown_remaining"] = cd_remain

                tree_nodes.append(node_view)

            visible.append({
                "id": tree["id"],
                "name": tree.get("name", tree["id"]),
                "description": tree.get("description", ""),
                "icon": tree.get("icon", ""),
                "nodes": tree_nodes,
            })

        return visible
