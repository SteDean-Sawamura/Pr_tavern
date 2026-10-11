"""Unified event engine — replaces StoryTree + EventScheduler + 4 ad-hoc state lists."""

from __future__ import annotations

import copy
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from engine.event_scheduler import fast_forward_fixed_frequency

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time helpers (migrated from event_scheduler.py)
# ---------------------------------------------------------------------------

def _parse_time(t: str | None) -> datetime | None:
    if not t:
        return None
    try:
        if isinstance(t, str) and t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        # 统一为 naive datetime，游戏内时间不需要时区
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _advance_time(dt: datetime, value: int, unit: str) -> datetime | None:
    if not dt or value <= 0:
        return None
    if unit == "day":
        return dt + timedelta(days=value)
    elif unit == "week":
        return dt + timedelta(weeks=value)
    elif unit == "month":
        if relativedelta:
            return dt + relativedelta(months=value)
        return dt + timedelta(days=value * 30)
    elif unit == "hour":
        return dt + timedelta(hours=value)
    elif unit == "minute":
        return dt + timedelta(minutes=value)
    return dt + timedelta(days=value)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class GameEvent:
    id: str
    name: str = ""
    description: str = ""
    source: str = "script"
    category: str = "event"

    # Trigger conditions
    requires: list[str] = field(default_factory=list)
    condition: str = ""
    trigger_time: str | None = None
    frequency: dict | None = None
    probability: float = 1.0
    delay_turns: int = 0
    activate_on: list[str] = field(default_factory=list)
    conditions: list[dict] = field(default_factory=list)

    # Lifecycle
    status: str = "pending"
    max_turns: int | None = None
    cooldown: int = 0
    repeatable: bool = False
    dormant_after: int | None = None
    duration_turns: int | None = None
    weight: int = 10
    auto_resolve: bool = False
    completion_keywords: list[str] = field(default_factory=list)

    # Effects
    on_activate: dict = field(default_factory=dict)
    on_complete: dict = field(default_factory=dict)
    on_expire: dict = field(default_factory=dict)
    on_tick: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)

    # Choices
    choices: list[dict] = field(default_factory=list)

    # Display
    visible: bool = True
    icon: str = ""
    tags: list[str] = field(default_factory=list)
    position: dict = field(default_factory=lambda: {"x": 0, "y": 0})
    on_complete_unlock: list[str] = field(default_factory=list)
    related_npcs: list[str] = field(default_factory=list)
    related_orgs: list[str] = field(default_factory=list)
    chain_events: list[dict] = field(default_factory=list)

    # Runtime
    created_turn: int = 0
    last_updated: int = 0
    turns_waited: int = 0
    metadata: dict = field(default_factory=dict)

    # Tree membership
    tree_id: str = ""


@dataclass
class EventResult:
    newly_completed: list[dict] = field(default_factory=list)
    newly_active: list[dict] = field(default_factory=list)
    newly_expired: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    notification_sources: list[str] = field(default_factory=list)
    inject_prompts: list[str] = field(default_factory=list)
    lore_activations: list[str] = field(default_factory=list)
    lore_deactivations: list[str] = field(default_factory=list)
    lore_additions: list[dict] = field(default_factory=list)
    lore_updates: list[dict] = field(default_factory=list)
    lore_removals: list[str] = field(default_factory=list)
    lore_reveals: list[str] = field(default_factory=list)
    game_events: list[str] = field(default_factory=list)
    narrative_callbacks: list[dict] = field(default_factory=list)
    state_activations: list[str] = field(default_factory=list)
    state_deactivations: list[str] = field(default_factory=list)
    triggered_consequences: list[dict] = field(default_factory=list)
    expired_consequences: list[dict] = field(default_factory=list)
    deadline_results: list[dict] = field(default_factory=list)
    lorebook_sync: list[dict] = field(default_factory=list)
    scheduled_events: list[dict] = field(default_factory=list)
    imminent_warnings: list[str] = field(default_factory=list)

    def merge(self, other: EventResult):
        for f in (
            "newly_completed", "newly_active", "newly_expired", "effects",
            "notifications", "notification_sources", "inject_prompts", "lore_activations",
            "lore_deactivations", "lore_additions", "lore_updates",
            "lore_removals", "lore_reveals", "game_events", "narrative_callbacks",
            "state_activations", "state_deactivations",
            "triggered_consequences", "expired_consequences",
            "deadline_results", "lorebook_sync", "scheduled_events",
            "imminent_warnings",
        ):
            getattr(self, f).extend(getattr(other, f))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class EventEngine:

    def __init__(self, script: dict, *, shadow_story_nodes: bool = False):
        self.events: dict[str, GameEvent] = {}
        self._trees: list[dict] = []
        self._tree_for_event: dict[str, str] = {}
        self.shadow_story_nodes = shadow_story_nodes
        self._load_story_trees(script)
        self._load_scheduled_events(script)
        self._load_dynamic_events(script)

    # ---- Script loaders ---------------------------------------------------

    def _load_story_trees(self, script: dict):
        st = script.get("story_tree", {})
        for tree in st.get("trees", []):
            self._trees.append({"id": tree["id"], "name": tree.get("name", ""), "description": tree.get("description", ""), "icon": tree.get("icon", "")})
            for node in tree.get("nodes", []):
                ntype = node.get("type", "auto")
                cat_map = {"auto": "story", "timed": "story", "choice": "choice", "quest": "quest", "trigger": "trigger", "periodic": "periodic"}
                cat = cat_map.get(ntype, "story")
                ev = GameEvent(
                    id=node["id"],
                    name=node.get("name", node["id"]),
                    description=node.get("description", ""),
                    source="script",
                    category=cat,
                    requires=[r for r in node.get("requires", []) if isinstance(r, str)],
                    condition=node.get("condition", ""),
                    activate_on=node.get("activate_events", []),
                    duration_turns=node.get("duration_turns"),
                    cooldown=node.get("cooldown", 0),
                    repeatable=node.get("repeatable", True) if ntype == "periodic" else False,
                    weight=node.get("weight", 10),
                    auto_resolve=node.get("auto_resolve", False),
                    completion_keywords=node.get("completion_keywords", []),
                    effects=node.get("effects", {}),
                    on_complete=node.get("on_complete", {}),
                    choices=node.get("choices", []),
                    visible=True,
                    position=node.get("position", {"x": 0, "y": 0}),
                    on_complete_unlock=node.get("on_complete_unlock", []),
                    related_npcs=node.get("related_npcs", []),
                    related_orgs=node.get("related_orgs", []),
                    tree_id=tree["id"],
                )
                if ntype == "trigger":
                    ev.metadata["event"] = node.get("event", [])
                    if isinstance(ev.metadata["event"], str):
                        ev.metadata["event"] = [ev.metadata["event"]]
                self.events[ev.id] = ev
                self._tree_for_event[ev.id] = tree["id"]

    def _load_scheduled_events(self, script: dict):
        for ev_def in script.get("cyclic_events", []):
            eid = ev_def.get("id", "")
            if not eid or eid in self.events:
                continue
            freq_val = ev_def.get("frequency_value", 1)
            freq_unit = ev_def.get("frequency_unit", "day")
            ev = GameEvent(
                id=eid,
                name=ev_def.get("name", eid),
                description=ev_def.get("description", ""),
                source="script",
                category="scheduled",
                condition=ev_def.get("condition", ""),
                frequency={"value": freq_val, "unit": freq_unit},
                trigger_time=ev_def.get("first_trigger"),
                repeatable=True,
                effects=self._convert_scheduled_effects(ev_def),
                metadata={
                    "expires_at": ev_def.get("expires_at"),
                    "fire_events": ev_def.get("fire_events", []),
                    "activate_events": ev_def.get("activate_events", []),
                    "original_def": ev_def,
                },
            )
            self.events[eid] = ev
        for ev_def in script.get("one_time_events", []):
            eid = ev_def.get("id", "")
            if not eid or eid in self.events:
                continue
            ev = GameEvent(
                id=eid,
                name=ev_def.get("name", eid),
                description=ev_def.get("description", ""),
                source="script",
                category="scheduled",
                condition=ev_def.get("condition", ""),
                trigger_time=ev_def.get("trigger_time"),
                repeatable=False,
                effects=self._convert_scheduled_effects(ev_def),
                metadata={
                    "fire_events": ev_def.get("fire_events", []),
                    "activate_events": ev_def.get("activate_events", []),
                    "original_def": ev_def,
                },
            )
            self.events[eid] = ev

    def _load_dynamic_events(self, script: dict):
        for ev_def in script.get("dynamic_events", []):
            eid = ev_def.get("id", "")
            if not eid or eid in self.events:
                continue
            ev = GameEvent(
                id=eid,
                name=ev_def.get("name", eid),
                description=ev_def.get("description", ""),
                source="script",
                category="dynamic",
                conditions=ev_def.get("conditions", []),
                cooldown=ev_def.get("cooldown", 5),
                weight=ev_def.get("weight", 10),
                effects=self._convert_dynamic_effects(ev_def),
                chain_events=ev_def.get("chain_events", []),
                repeatable=True,
            )
            self.events[eid] = ev

    @staticmethod
    def _convert_scheduled_effects(ev_def: dict) -> dict:
        effects = {}
        raw = ev_def.get("effects", [])
        if isinstance(raw, list):
            sv, al, dl, adl, ul, rl, nc, ast, dst, so = [], [], [], [], [], [], [], [], [], []
            for eff in raw:
                etype = eff.get("type", "")
                if etype == "state_change":
                    sv.append({"target": eff["target"], "op": eff.get("op", "set"), "value": eff.get("value")})
                elif etype == "activate_state":
                    ast.append(eff.get("id", ""))
                elif etype == "deactivate_state":
                    dst.append(eff.get("id", ""))
                elif etype == "narrative_callback":
                    nc.append({"text": eff.get("text", ""), "priority": eff.get("priority", "medium")})
                elif etype == "activate_lore":
                    al.append(eff.get("id", ""))
                elif etype == "deactivate_lore":
                    dl.append(eff.get("id", ""))
                elif etype == "add_lore":
                    adl.append(eff.get("entry", {}))
                elif etype == "update_lore":
                    ul.append({"id": eff.get("id", ""), "content": eff.get("content", ""), "keys": eff.get("keys")})
                elif etype == "remove_lore":
                    rl.append(eff.get("id", ""))
                elif etype == "schedule_override":
                    params = {
                        "npc_id": eff.get("npc_id", ""),
                        "time_range": eff.get("time_range", ""),
                        "location": eff.get("location", ""),
                        "activity": eff.get("activity", ""),
                        "priority": eff.get("priority", 100),
                        "expires": eff.get("expires", ""),
                    }
                    if "condition" in eff:
                        params["condition"] = eff["condition"]
                    so.append({"action": "schedule_override", "params": params})
            if sv:
                effects["set_var"] = sv
            if al:
                effects["activate_lore"] = al
            if dl:
                effects["deactivate_lore"] = dl
            if adl:
                effects["add_lore"] = adl
            if ul:
                effects["update_lore"] = ul
            if rl:
                effects["remove_lore"] = rl
            if nc:
                effects["narrative_callback"] = nc
            if ast:
                effects["activate_state"] = ast
            if dst:
                effects["deactivate_state"] = dst
            if so:
                effects["schedule_override"] = so
        elif isinstance(raw, dict):
            effects = raw
        return effects

    _convert_dynamic_effects = _convert_scheduled_effects

    # ---- State management -------------------------------------------------

    def _init_state(self, state: dict) -> dict:
        es = state.setdefault("events", {})
        return es

    def sync_story_tree_state(self, state: dict):
        if not self.shadow_story_nodes:
            return
        story_state = state.get("story_tree_state", {})
        statuses = [(status, set(story_state.get(status, [])))
                    for status in ("completed", "active", "unlocked")]
        events = self._init_state(state)
        for node_id in self._tree_for_event:
            if self.events[node_id].category == "trigger":
                continue
            entry = events.setdefault(node_id, {})
            entry["status"] = next((status for status, node_ids in statuses if node_id in node_ids), "locked")
            for story_key, event_key in (("choices_made", "choices_made"),
                                         ("timed_progress", "progress"),
                                         ("periodic_cooldowns", "cooldown_until")):
                values = story_state.get(story_key, {})
                if node_id in values:
                    entry[event_key] = copy.deepcopy(values[node_id])
                else:
                    entry.pop(event_key, None)

    def _migrate_state(self, state: dict):
        if "events" in state and state["events"]:
            return
        events = state.setdefault("events", {})
        # Story tree state
        sts = state.get("story_tree_state", {})
        for eid in sts.get("completed", []):
            events.setdefault(eid, {})["status"] = "completed"
        for eid in sts.get("active", []):
            events.setdefault(eid, {})["status"] = "active"
        for eid in sts.get("unlocked", []):
            if eid not in events:
                events[eid] = {"status": "unlocked"}
        choices_made = sts.get("choices_made", {})
        for eid, cid in choices_made.items():
            events.setdefault(eid, {})["choices_made"] = cid
        for eid, tp in sts.get("timed_progress", {}).items():
            events.setdefault(eid, {})["progress"] = dict(tp)
        for eid, cd in sts.get("periodic_cooldowns", {}).items():
            events.setdefault(eid, {})["cooldown_until"] = cd
        # Cyclic event trackers
        for eid, tracker in state.get("cyclic_event_trackers", {}).items():
            es = events.setdefault(eid, {})
            es["next_fire"] = tracker.get("next_fire")
            es["last_fired"] = tracker.get("last_fired")
        # Fired one-time events
        for eid in state.get("fired_one_time_events", []):
            events.setdefault(eid, {})["status"] = "completed"
        # Pending consequences
        for cons in state.get("pending_consequences", []):
            desc = cons.get("description", "")
            cid = cons.get("id", f"_cons_{hash(desc) % 100000}")
            events[cid] = {
                "status": "pending",
                "category": "consequence",
                "turns_waited": cons.get("turns_waited", 0),
                "metadata": cons,
            }
        # Active deadlines
        for dl in state.get("active_deadlines", []):
            dlid = dl.get("id", "")
            if dlid:
                events[dlid] = {
                    "status": "active",
                    "category": "deadline",
                    "metadata": dl,
                }
        # Narrative threads
        for t in state.get("narrative_threads", []):
            tid = t.get("id", "")
            if tid:
                events[tid] = {
                    "status": t.get("status", "active"),
                    "category": "thread",
                    "last_updated": t.get("last_updated", 0),
                    "metadata": t,
                }
        # Clue board
        for c in state.get("clue_board", []):
            cid = c.get("id", "")
            if cid:
                events[cid] = {
                    "status": "completed",
                    "category": "clue",
                    "metadata": c,
                }
        # Dynamic events
        for eid, cd in state.get("dynamic_event_cooldowns", {}).items():
            events.setdefault(eid, {})["cooldown_until"] = cd
        for p in state.get("pending_dynamic_events", []):
            eid = p.get("event_id", "")
            if eid:
                events.setdefault(eid, {})["due_turn"] = p.get("due_turn", 0)

    # ---- Core tick --------------------------------------------------------

    def tick(
        self,
        state: dict,
        turn_number: int,
        game_time: str = "",
        old_time: str = "",
        condition_eval: Callable[[str], bool] | None = None,
        player_action: str = "",
        active_lifecycle_events: list[str] | None = None,
        state_manager=None,
        advance_timed: bool = True,
    ) -> EventResult:
        self._migrate_state(state)
        es = self._init_state(state)
        result = EventResult()

        # Phase 1: Trigger events (responding to fire_events from previous tick)
        pending_fires = state.pop("_pending_fire_events", [])
        if pending_fires:
            for ev_name in pending_fires:
                sub = self._fire_event_internal(ev_name, state, es, turn_number, condition_eval)
                result.merge(sub)

        # Phase 2: Scheduled events (game-time based)
        sub = self._tick_scheduled(state, es, old_time, game_time, condition_eval)
        result.merge(sub)

        # Phase 3: Consequences (probability + delay + escalation)
        sub = self._tick_consequences(state, es, turn_number, player_action)
        result.merge(sub)

        # Phase 4: Deadlines (turn countdown)
        sub = self._tick_deadlines(state, es, turn_number, condition_eval)
        result.merge(sub)

        # Phase 5: Story/quest/choice/periodic (dependency chains + conditions)
        sub = self._tick_story_nodes(state, es, turn_number, condition_eval, advance_timed)
        result.merge(sub)

        # Phase 6: Dynamic events (condition-based with weighted random)
        sub = self._tick_dynamic(state, es, turn_number, condition_eval, state_manager)
        result.merge(sub)

        # Phase 7: Thread dormant check
        self._tick_thread_dormancy(state, es, turn_number)

        # Phase 8: Process cascading fire_events
        if result.game_events:
            state["_pending_fire_events"] = list(result.game_events)

        return result

    # ---- Phase 2: Scheduled -----------------------------------------------

    def _tick_scheduled(
        self, state: dict, es: dict,
        old_time: str, new_time: str,
        condition_eval: Callable | None,
        *, condition_eval_at_time: Callable[[str, str], bool] | None = None,
    ) -> EventResult:
        result = EventResult()
        t_old = _parse_time(old_time)
        t_new = _parse_time(new_time)
        if not t_old or not t_new or t_new <= t_old:
            return result

        def matches_at(condition, fire_time):
            if condition_eval_at_time is not None:
                return condition_eval_at_time(fire_time.isoformat(), condition)
            return condition_eval is None or condition_eval(condition)

        for eid, ev in list(self.events.items()):
            if ev.category != "scheduled":
                continue
            evs = es.get(eid, {})

            if ev.frequency:
                # Cyclic event
                next_fire = _parse_time(evs.get("next_fire") or ev.trigger_time)
                if not next_fire:
                    continue
                expires_at = _parse_time(ev.metadata.get("expires_at"))
                effective_end = t_new
                if expires_at:
                    if expires_at < t_old:
                        continue
                    effective_end = min(t_new, expires_at)

                freq_val = ev.frequency.get("value", 1)
                freq_unit = ev.frequency.get("unit", "day")
                if freq_val <= 0:
                    continue
                next_fire = fast_forward_fixed_frequency(next_fire, t_old, freq_val, freq_unit)
                iterations = 0
                _last_fired_time = None
                while next_fire and next_fire <= effective_end and iterations < 1000:
                    iterations += 1
                    if next_fire >= t_old:
                        if not matches_at(ev.condition, next_fire):
                            prev = next_fire
                            next_fire = _advance_time(next_fire, freq_val, freq_unit)
                            if next_fire and next_fire <= prev:
                                break
                            continue
                        result.scheduled_events.append({
                            "type": "cyclic", "event_id": eid,
                            "description": ev.description,
                            "fire_time": next_fire.isoformat(),
                        })
                        self._apply_effects(ev.effects, result, eid)
                        for fe in ev.metadata.get("fire_events", []):
                            result.game_events.append(fe)
                        _last_fired_time = next_fire
                    prev = next_fire
                    next_fire = _advance_time(next_fire, freq_val, freq_unit)
                    if next_fire and next_fire <= prev:
                        break
                evs_entry = es.setdefault(eid, {})
                if _last_fired_time:
                    evs_entry["last_fired"] = _last_fired_time.isoformat()
                evs_entry["next_fire"] = next_fire.isoformat() if next_fire else None
            else:
                # One-time event
                if evs.get("status") == "completed":
                    continue
                trigger = _parse_time(ev.trigger_time)
                if trigger and t_old <= trigger <= t_new:
                    if not matches_at(ev.condition, trigger):
                        continue
                    result.scheduled_events.append({
                        "type": "one_time", "event_id": eid,
                        "description": ev.description,
                        "fire_time": trigger.isoformat(),
                    })
                    self._apply_effects(ev.effects, result, eid)
                    for fe in ev.metadata.get("fire_events", []):
                        result.game_events.append(fe)
                    es.setdefault(eid, {})["status"] = "completed"

        return result

    # ---- Phase 3: Consequences --------------------------------------------

    def _tick_consequences(
        self, state: dict, es: dict,
        turn_number: int, player_action: str,
    ) -> EventResult:
        result = EventResult()
        action_lower = player_action.lower() if player_action else ""
        triggered = []
        remaining_ids = []
        expired = []

        cons_events = [(eid, ev) for eid, ev in self.events.items() if ev.category == "consequence"]
        for eid, ev in cons_events:
            evs = es.get(eid, {})
            status = evs.get("status", ev.status)
            if status != "pending":
                continue
            turns_waited = evs.get("turns_waited", ev.turns_waited) + 1
            evs_entry = es.setdefault(eid, {})
            evs_entry["turns_waited"] = turns_waited

            importance = ev.metadata.get("importance", "normal")
            max_turns = ev.max_turns or (8 if importance == "high" else 5)

            if turns_waited < ev.delay_turns:
                remaining_ids.append(eid)
                continue

            base_chance = ev.probability
            escalation = 0.08 if importance == "high" else 0.05
            chance = base_chance + escalation * max(0, turns_waited - ev.delay_turns)
            desc = ev.description
            if action_lower and desc:
                words = [w for w in re.split(r'[\s，。、；：！？""''（）\[\]{},.;:!?\'"()\-/]+', desc) if len(w) >= 2]
                if any(w in action_lower for w in words):
                    chance += 0.2
            chance = min(1.0, chance)

            if turns_waited >= max_turns and importance == "high":
                triggered.append((eid, ev))
            elif random.random() < chance:
                triggered.append((eid, ev))
            elif turns_waited >= max_turns:
                expired.append((eid, ev))
            else:
                remaining_ids.append(eid)
                # Imminent warning
                if importance == "high" and turns_waited >= max_turns - 2:
                    result.imminent_warnings.append(desc[:30] if desc else "未知威胁")

        # Cap at 2 consequences per turn
        if len(triggered) > 2:
            overflow = triggered[2:]
            triggered = triggered[:2]
            for eid, ev in overflow:
                evs = es.setdefault(eid, {})
                evs["status"] = "pending"
                remaining_ids.append(eid)

        for eid, ev in triggered:
            es.setdefault(eid, {})["status"] = "completed"
            result.triggered_consequences.append({"id": eid, "description": ev.description, "metadata": ev.metadata})
            result.lorebook_sync.append({"action": "remove", "entry_id": f"_cons_{hash(ev.description) % 100000}"})

        for eid, ev in expired:
            es.setdefault(eid, {})["status"] = "expired"
            result.expired_consequences.append({"id": eid, "description": ev.description})
            result.lorebook_sync.append({"action": "remove", "entry_id": f"_cons_{hash(ev.description) % 100000}"})

        return result

    # ---- Phase 4: Deadlines -----------------------------------------------

    def _tick_deadlines(
        self, state: dict, es: dict,
        turn_number: int,
        condition_eval: Callable | None,
    ) -> EventResult:
        result = EventResult()

        dl_events = [(eid, ev) for eid, ev in self.events.items() if ev.category == "deadline"]
        removed_ids = []
        for eid, ev in dl_events:
            evs = es.get(eid, {})
            status = evs.get("status", ev.status)
            if status != "active":
                continue

            turns_remaining = evs.get("turns_remaining", ev.metadata.get("turns_remaining", 0)) - 1
            evs_entry = es.setdefault(eid, {})
            evs_entry["turns_remaining"] = turns_remaining
            for dl_item in state.get("active_deadlines", []):
                if dl_item.get("id") == eid:
                    dl_item["turns_remaining"] = turns_remaining
                    break

            on_complete = ev.metadata.get("on_complete", {})
            if on_complete and on_complete.get("condition"):
                if condition_eval and condition_eval(on_complete["condition"]):
                    result.deadline_results.append({
                        "id": eid,
                        "description": on_complete.get("description", ev.description),
                        "outcome": "completed",
                    })
                    evs_entry["status"] = "completed"
                    removed_ids.append(eid)
                    continue

            if turns_remaining <= 0:
                on_expire = ev.on_expire or ev.metadata.get("on_expire", {})
                result.deadline_results.append({
                    "id": eid,
                    "description": on_expire.get("description", ev.description),
                    "outcome": "expired",
                })
                for sc in on_expire.get("state_changes", []):
                    result.effects.append({"action": "set_var", "params": sc})
                evs_entry["status"] = "expired"
                removed_ids.append(eid)

        if removed_ids:
            ad = state.get("active_deadlines", [])
            state["active_deadlines"] = [d for d in ad if d.get("id") not in removed_ids]

        return result

    # ---- Phase 5: Story nodes ---------------------------------------------

    def _tick_story_nodes(
        self, state: dict, es: dict,
        turn_number: int,
        condition_eval: Callable | None,
        advance_timed: bool,
    ) -> EventResult:
        result = EventResult()
        completed = set()
        active = set()
        unlocked = set()

        # Build status sets from es
        for eid, evs in es.items():
            st = evs.get("status", "")
            if st == "completed":
                completed.add(eid)
            elif st == "active":
                active.add(eid)
            elif st == "unlocked":
                unlocked.add(eid)

        periodic_eligible: list[GameEvent] = []

        for tree_meta in self._trees:
            tree_id = tree_meta["id"]
            tree_events = [ev for ev in self.events.values() if ev.tree_id == tree_id]
            for ev in tree_events:
                eid = ev.id
                cat = ev.category

                # Trigger nodes handled in fire_event
                if cat == "trigger":
                    continue

                if eid in completed:
                    if cat == "periodic" and ev.repeatable:
                        cd_until = es.get(eid, {}).get("cooldown_until", 0)
                        if turn_number >= cd_until:
                            completed.discard(eid)
                            es.setdefault(eid, {})["status"] = "pending"
                        else:
                            continue
                    else:
                        continue

                if not all(r in completed for r in ev.requires):
                    continue

                if condition_eval is not None and not condition_eval(ev.condition):
                    continue

                if eid not in unlocked and eid not in active:
                    if ev.activate_on:
                        continue
                    unlocked.add(eid)
                    es.setdefault(eid, {})["status"] = "unlocked"

                if cat == "story":
                    if ev.duration_turns is not None:
                        # Timed node
                        evs_entry = es.setdefault(eid, {})
                        progress = evs_entry.get("progress")
                        if not progress:
                            evs_entry["progress"] = {
                                "started_turn": turn_number,
                                "remaining": ev.duration_turns,
                            }
                            if eid not in active:
                                active.add(eid)
                                evs_entry["status"] = "active"
                                result.newly_active.append(self._event_to_dict(ev))
                        elif (advance_timed
                              and turn_number > progress["started_turn"]
                              and turn_number > progress.get("last_advanced_turn", progress["started_turn"])):
                            progress["last_advanced_turn"] = turn_number
                            progress["remaining"] -= 1
                            if progress["remaining"] <= 0:
                                del evs_entry["progress"]
                                self._complete_event(ev, es, result, completed, unlocked)
                                active.discard(eid)
                    else:
                        # Auto node
                        self._complete_event(ev, es, result, completed, unlocked)

                elif cat in ("choice", "quest"):
                    if eid not in active:
                        active.add(eid)
                        es.setdefault(eid, {})["status"] = "active"
                        result.newly_active.append(self._event_to_dict(ev))
                        if cat == "choice" and not ev.auto_resolve and not self.shadow_story_nodes:
                            result.notifications.append(f"【剧情分支】{ev.name} — 请在剧情树中做出选择")
                            result.notification_sources.append(eid)
                    if cat == "choice" and ev.auto_resolve:
                        chosen_c = self._auto_resolve_choice(ev, condition_eval)
                        if chosen_c:
                            es[eid]["choices_made"] = chosen_c["id"]
                            self._complete_event(ev, es, result, completed, unlocked)
                            self._apply_effects(chosen_c.get("effects", {}), result, eid)
                            for uid in chosen_c.get("unlock", []):
                                unlocked.add(uid)
                                es.setdefault(uid, {})["status"] = "unlocked"
                            active.discard(eid)

                elif cat == "periodic":
                    cd_until = es.get(eid, {}).get("cooldown_until", 0)
                    if turn_number < cd_until:
                        continue
                    periodic_eligible.append(ev)

        # Periodic weighted random
        if periodic_eligible:
            weights = [ev.weight for ev in periodic_eligible]
            chosen = random.choices(periodic_eligible, weights=weights, k=1)[0]
            self._apply_effects(chosen.effects, result, chosen.id)
            es.setdefault(chosen.id, {})["cooldown_until"] = turn_number + chosen.cooldown
            if not chosen.repeatable:
                self._complete_event(chosen, es, result, completed, unlocked)
            else:
                completed.add(chosen.id)
                es.setdefault(chosen.id, {})["status"] = "completed"
                result.newly_completed.append(self._event_to_dict(chosen))

        return result

    # ---- Phase 6: Dynamic events ------------------------------------------

    def _tick_dynamic(
        self, state: dict, es: dict,
        turn_number: int,
        condition_eval: Callable | None,
        state_manager=None,
    ) -> EventResult:
        result = EventResult()
        fired: list[GameEvent] = []

        # Process pending chain events
        pending_chains = state.get("_pending_chain_events", [])
        due = [p for p in pending_chains if p.get("due_turn", 0) <= turn_number]
        for p in due:
            eid = p.get("event_id", "")
            ev = self.events.get(eid)
            if ev and ev.category == "dynamic":
                fired.append(ev)
            pending_chains.remove(p)
        if not pending_chains:
            state.pop("_pending_chain_events", None)

        # Evaluate fresh events
        eligible = []
        for eid, ev in self.events.items():
            if ev.category != "dynamic":
                continue
            evs = es.get(eid, {})
            cd_until = evs.get("cooldown_until", 0)
            if turn_number < cd_until:
                continue
            all_met = True
            for cond in ev.conditions:
                path = cond.get("path", "")
                if state_manager:
                    actual = state_manager._get_value(state, path)
                else:
                    actual = None
                if actual is None:
                    all_met = False
                    break
                if not self._compare_values(actual, cond.get("op", "=="), cond.get("value")):
                    all_met = False
                    break
            if all_met:
                eligible.append(ev)

        if eligible and not fired:
            weights = [ev.weight for ev in eligible]
            chosen = random.choices(eligible, weights=weights, k=1)[0]
            fired.append(chosen)

        for ev in fired:
            evs_entry = es.setdefault(ev.id, {})
            evs_entry["cooldown_until"] = turn_number + ev.cooldown
            evs_entry.setdefault("triggered", []).append(turn_number)
            self._apply_effects(ev.effects, result, ev.id)

            for chain in ev.chain_events:
                state.setdefault("_pending_chain_events", []).append({
                    "event_id": chain.get("event_id", ""),
                    "due_turn": turn_number + chain.get("delay_turns", 3),
                })

        if fired:
            state["_dynamic_events_this_turn"] = [
                {"id": ev.id, "effects": ev.effects} for ev in fired
            ]

        return result

    # ---- Phase 7: Thread dormancy -----------------------------------------

    def _tick_thread_dormancy(self, state: dict, es: dict, turn_number: int):
        dormant_threshold = 5
        for eid, ev in self.events.items():
            if ev.category != "thread":
                continue
            evs = es.get(eid, {})
            if evs.get("status") != "active":
                continue
            last = evs.get("last_updated", ev.last_updated)
            if turn_number - last >= dormant_threshold:
                evs["status"] = "dormant"

    # ---- Fire event (external trigger) ------------------------------------

    def fire_event(
        self, event_name: str, state: dict,
        condition_eval: Callable[[str], bool] | None = None,
        *, skip_story_triggers: bool = False,
    ) -> EventResult:
        self._migrate_state(state)
        es = self._init_state(state)
        return self._fire_event_internal(
            event_name, state, es, 0, condition_eval,
            skip_story_triggers=skip_story_triggers,
        )

    def _fire_event_internal(
        self, event_name: str, state: dict, es: dict,
        turn_number: int, condition_eval: Callable | None,
        *, skip_story_triggers: bool = False,
    ) -> EventResult:
        result = EventResult()
        completed = {eid for eid, evs in es.items() if evs.get("status") == "completed"}
        unlocked = {eid for eid, evs in es.items() if evs.get("status") == "unlocked"}

        for eid, ev in self.events.items():
            if ev.category == "trigger":
                node_events = ev.metadata.get("event", [])
                if event_name not in node_events:
                    continue
                if skip_story_triggers and eid in self._tree_for_event:
                    continue
                if condition_eval is not None and not condition_eval(ev.condition):
                    continue
                self._apply_effects(ev.effects, result, eid)
                continue

            if eid in completed or eid in unlocked or es.get(eid, {}).get("status") == "active":
                continue
            if event_name in ev.activate_on:
                if condition_eval is not None and not condition_eval(ev.condition):
                    continue
                unlocked.add(eid)
                es.setdefault(eid, {})["status"] = "unlocked"

        return result

    # ---- Player actions ---------------------------------------------------

    def make_choice(
        self, state: dict, event_id: str, choice_id: str,
        condition_eval: Callable[[str], bool] | None = None,
    ) -> EventResult:
        self._migrate_state(state)
        es = self._init_state(state)
        ev = self.events.get(event_id)
        if not ev:
            return EventResult()

        evs = es.get(event_id, {})
        if evs.get("status") != "active":
            return EventResult()

        chosen = None
        for c in ev.choices:
            if c["id"] == choice_id:
                chosen = c
                break
        if not chosen:
            return EventResult()

        choice_cond = chosen.get("condition", "")
        if condition_eval is not None and not condition_eval(choice_cond):
            return EventResult()

        es.setdefault(event_id, {})["choices_made"] = choice_id
        completed = {eid for eid, evs in es.items() if evs.get("status") == "completed"}
        unlocked = {eid for eid, evs in es.items() if evs.get("status") == "unlocked"}
        result = EventResult()

        self._complete_event(ev, es, result, completed, unlocked)
        self._apply_effects(chosen.get("effects", {}), result, event_id)

        for uid in chosen.get("unlock", []):
            unlocked.add(uid)
            es.setdefault(uid, {})["status"] = "unlocked"

        evs_entry = es.setdefault(event_id, {})
        if evs_entry.get("status") == "active":
            evs_entry["status"] = "completed"

        return result

    def complete_quest(self, state: dict, event_id: str) -> EventResult:
        self._migrate_state(state)
        es = self._init_state(state)
        ev = self.events.get(event_id)
        if not ev or ev.category != "quest":
            return EventResult()
        evs = es.get(event_id, {})
        if evs.get("status") != "active":
            return EventResult()

        completed = {eid for eid, evs2 in es.items() if evs2.get("status") == "completed"}
        unlocked = {eid for eid, evs2 in es.items() if evs2.get("status") == "unlocked"}
        result = EventResult()
        self._complete_event(ev, es, result, completed, unlocked)
        evs_entry = es.setdefault(event_id, {})
        if evs_entry.get("status") == "active":
            evs_entry["status"] = "completed"
        return result

    # ---- Add event at runtime ---------------------------------------------

    def add_event(self, event: GameEvent):
        self.events[event.id] = event

    def add_consequence(self, state: dict, cons: dict):
        desc = cons.get("description", "")
        if not desc:
            return
        # Dedup
        for eid, ev in self.events.items():
            if ev.category == "consequence" and ev.description:
                if self._text_similar(desc, ev.description):
                    evs = state.get("events", {}).get(eid, {})
                    if evs.get("status") == "pending":
                        if cons.get("importance") == "high" and ev.metadata.get("importance") != "high":
                            ev.metadata["importance"] = "high"
                        return

        cid = cons.get("id", f"_cons_{hash(desc) % 100000}")
        ev = GameEvent(
            id=cid,
            description=desc,
            source="ai",
            category="consequence",
            probability=cons.get("trigger_chance", 0.3),
            delay_turns=cons.get("turns_delay", 1),
            max_turns=cons.get("max_turns", 5),
            metadata=cons,
        )
        ev.metadata.setdefault("importance", "normal")
        self.events[cid] = ev
        es = state.setdefault("events", {})
        es[cid] = {"status": "pending", "turns_waited": 0}

    def add_deadline(self, state: dict, dl: dict, turn_number: int):
        dlid = dl.get("id", "")
        if not dlid:
            return
        if dlid in self.events:
            return
        ev = GameEvent(
            id=dlid,
            description=dl.get("description", dlid),
            source="ai",
            category="deadline",
            on_expire=dl.get("on_expire", {}),
            metadata=dl,
        )
        ev.metadata["turns_remaining"] = dl.get("turns_remaining", 5)
        ev.metadata["on_complete"] = dl.get("on_complete", {})
        ev.metadata["created_turn"] = turn_number
        ev.metadata["visible"] = dl.get("visible", True)
        self.events[dlid] = ev
        es = state.setdefault("events", {})
        es[dlid] = {"status": "active", "turns_remaining": dl.get("turns_remaining", 5)}

    def add_thread(self, state: dict, tu: dict, turn_number: int):
        tid = tu.get("id", "")
        if not tid:
            return
        if tid in self.events:
            ev = self.events[tid]
            evs = state.setdefault("events", {}).setdefault(tid, {})
            if tu.get("status"):
                evs["status"] = tu["status"]
            if tu.get("description"):
                ev.description = tu["description"]
            evs["last_updated"] = turn_number
            ev.last_updated = turn_number
            ev.metadata.update(tu)
            return
        ev = GameEvent(
            id=tid,
            name=tu.get("name", tid),
            description=tu.get("description", ""),
            source="ai",
            category="thread",
            dormant_after=5,
            last_updated=turn_number,
            created_turn=turn_number,
            metadata=tu,
        )
        self.events[tid] = ev
        es = state.setdefault("events", {})
        es[tid] = {"status": tu.get("status", "active"), "last_updated": turn_number}

    def add_clue(self, state: dict, clue: dict, turn_number: int):
        cid = clue.get("id", "")
        if not cid:
            return
        if cid in self.events:
            return
        ev = GameEvent(
            id=cid,
            description=clue.get("text", ""),
            source="ai",
            category="clue",
            created_turn=turn_number,
            metadata=clue,
        )
        self.events[cid] = ev
        es = state.setdefault("events", {})
        es[cid] = {"status": "completed"}

    # ---- Inject dynamic content (from AI Stage 6) -------------------------

    def inject_dynamic_tree(self, tree: dict):
        existing_ids = set(self.events.keys())
        tree_meta = {
            "id": tree["id"],
            "name": tree.get("name", ""),
            "description": tree.get("description", ""),
            "icon": tree.get("icon", "scroll"),
        }
        has_new = False
        for node in tree.get("nodes", []):
            nid = node.get("id", "")
            if nid in existing_ids:
                continue
            has_new = True
            ntype = node.get("type", "auto")
            cat_map = {"auto": "story", "timed": "story", "choice": "choice", "quest": "quest", "trigger": "trigger", "periodic": "periodic"}
            ev = GameEvent(
                id=nid,
                name=node.get("name", nid),
                description=node.get("description", ""),
                source="dynamic",
                category=cat_map.get(ntype, "story"),
                requires=[r for r in node.get("requires", []) if isinstance(r, str)],
                condition=node.get("condition", ""),
                activate_on=node.get("activate_events", []),
                duration_turns=node.get("duration_turns"),
                cooldown=node.get("cooldown", 0),
                repeatable=node.get("repeatable", True) if ntype == "periodic" else False,
                weight=node.get("weight", 10),
                auto_resolve=node.get("auto_resolve", False),
                completion_keywords=node.get("completion_keywords", []),
                effects=node.get("effects", {}),
                choices=node.get("choices", []),
                position=node.get("position", {"x": 0, "y": 0}),
                on_complete_unlock=node.get("on_complete_unlock", []),
                related_npcs=node.get("related_npcs", []),
                related_orgs=node.get("related_orgs", []),
                tree_id=tree["id"],
            )
            if ntype == "trigger":
                ev.metadata["event"] = node.get("event", [])
                if isinstance(ev.metadata["event"], str):
                    ev.metadata["event"] = [ev.metadata["event"]]
            self.events[nid] = ev
            self._tree_for_event[nid] = tree["id"]
        if has_new:
            if not any(t["id"] == tree["id"] for t in self._trees):
                self._trees.append(tree_meta)

    def inject_dynamic_event(self, evt: dict, kind: str):
        eid = evt.get("id", "")
        if not eid or eid in self.events:
            return
        if kind == "one_time":
            ev = GameEvent(
                id=eid,
                name=evt.get("name", eid),
                description=evt.get("description", ""),
                source="dynamic",
                category="scheduled",
                condition=evt.get("condition", ""),
                trigger_time=evt.get("trigger_time"),
                repeatable=False,
                effects=self._convert_scheduled_effects(evt),
                metadata={
                    "fire_events": evt.get("fire_events", []),
                    "activate_events": evt.get("activate_events", []),
                    "original_def": evt,
                },
            )
        else:
            freq_val = evt.get("frequency_value", 1)
            freq_unit = evt.get("frequency_unit", "day")
            ev = GameEvent(
                id=eid,
                name=evt.get("name", eid),
                description=evt.get("description", ""),
                source="dynamic",
                category="scheduled",
                condition=evt.get("condition", ""),
                frequency={"value": freq_val, "unit": freq_unit},
                trigger_time=evt.get("first_trigger"),
                repeatable=True,
                effects=self._convert_scheduled_effects(evt),
                metadata={
                    "fire_events": evt.get("fire_events", []),
                    "activate_events": evt.get("activate_events", []),
                    "original_def": evt,
                },
            )
        self.events[eid] = ev

    def inject_dynamic_event_tracker(self, state: dict, evt: dict, kind: str):
        eid = evt.get("id", "")
        if not eid:
            return
        es = state.setdefault("events", {})
        if kind == "cyclic" and eid not in es:
            es[eid] = {"next_fire": evt.get("first_trigger", "")}

    # ---- Query ------------------------------------------------------------

    def get_visible_trees(self, state: dict, turn_number: int = 0) -> list[dict]:
        self._migrate_state(state)
        es = self._init_state(state)
        visible = []
        for tree_meta in self._trees:
            tree_id = tree_meta["id"]
            tree_events = sorted(
                [ev for ev in self.events.values() if ev.tree_id == tree_id],
                key=lambda e: e.id,
            )
            tree_nodes = []
            for ev in tree_events:
                eid = ev.id
                evs = es.get(eid, {})
                raw_status = evs.get("status", "")

                if raw_status == "completed":
                    status = "completed"
                elif raw_status == "active":
                    status = "active"
                elif raw_status == "unlocked":
                    status = "available"
                else:
                    status = "locked"

                node_view = {
                    "id": eid,
                    "name": ev.name,
                    "description": ev.description,
                    "type": self._category_to_node_type(ev.category),
                    "status": status,
                    "position": ev.position,
                    "requires": ev.requires,
                    "on_complete_unlock": ev.on_complete_unlock,
                    "activate_events": ev.activate_on,
                    "effects": {"fire_events": ev.effects.get("fire_events", [])},
                    "related_npcs": ev.related_npcs,
                    "related_orgs": ev.related_orgs,
                }

                if ev.category == "choice":
                    chosen = evs.get("choices_made")
                    view_choices = []
                    for c in ev.choices:
                        view_choices.append({
                            "id": c["id"],
                            "label": c.get("label", c["id"]),
                            "description": c.get("description", ""),
                            "chosen": c["id"] == chosen,
                            "unlock": c.get("unlock", []),
                        })
                    node_view["choices"] = view_choices

                if ev.duration_turns is not None and evs.get("progress"):
                    total = ev.duration_turns
                    remaining = evs["progress"].get("remaining", 0)
                    node_view["progress"] = {
                        "total": total,
                        "remaining": remaining,
                        "percent": max(0, min(100, int((total - remaining) / total * 100))) if total > 0 else 100,
                    }

                if ev.category == "periodic":
                    if raw_status == "completed" and ev.repeatable:
                        cd = evs.get("cooldown_until", 0)
                        node_view["status"] = "available" if turn_number >= cd else "completed"
                    cd_remain = evs.get("cooldown_until", 0) - turn_number
                    if cd_remain > 0:
                        node_view["cooldown_remaining"] = cd_remain

                tree_nodes.append(node_view)

            visible.append({
                "id": tree_id,
                "name": tree_meta.get("name", tree_id),
                "description": tree_meta.get("description", ""),
                "icon": tree_meta.get("icon", ""),
                "nodes": tree_nodes,
            })
        return visible

    def get_upcoming_nodes(self, state: dict, limit: int = 3) -> list[dict]:
        self._migrate_state(state)
        es = self._init_state(state)
        completed = {eid for eid, evs in es.items() if evs.get("status") == "completed"}
        unlocked = {eid for eid, evs in es.items() if evs.get("status") == "unlocked"}
        active = {eid for eid, evs in es.items() if evs.get("status") == "active"}
        upcoming = []
        for ev in self.events.values():
            if not ev.tree_id:
                continue
            if ev.id in completed or ev.id in unlocked or ev.id in active:
                continue
            if ev.category == "trigger":
                continue
            if ev.requires and all(r in completed for r in ev.requires):
                upcoming.append(self._event_to_dict(ev))
        return upcoming[:limit]

    def get_events_by_category(self, state: dict, category: str) -> list[dict]:
        self._migrate_state(state)
        es = self._init_state(state)
        results = []
        for eid, ev in self.events.items():
            if ev.category != category:
                continue
            evs = es.get(eid, {})
            results.append({
                "id": eid,
                "name": ev.name,
                "description": ev.description,
                "status": evs.get("status", ev.status),
                "metadata": ev.metadata,
                **evs,
            })
        return results

    def get_events_for_prompt(self, state: dict) -> dict:
        """按 category 分组返回事件摘要，供 prompt_builder 统一消费。"""
        self._migrate_state(state)
        es = self._init_state(state)
        result: dict[str, list[dict]] = {
            "consequences": [], "deadlines": [], "threads": [],
            "clues": [], "story_nodes": [],
        }
        for eid, ev in self.events.items():
            evs = es.get(eid, {})
            status = evs.get("status", ev.status)
            if ev.category == "consequence" and status == "pending":
                result["consequences"].append({
                    "id": eid, "description": ev.description,
                    "turns_waited": evs.get("turns_waited", 0),
                    "importance": ev.metadata.get("importance", "normal"),
                })
            elif ev.category == "deadline" and status == "active":
                result["deadlines"].append({
                    "id": eid, "description": ev.description,
                    "turns_remaining": evs.get("turns_remaining", 0),
                })
            elif ev.category == "thread" and status in ("active", "dormant"):
                result["threads"].append({
                    "id": eid, "name": ev.name,
                    "description": ev.description, "status": status,
                })
            elif ev.category == "clue" and status == "completed":
                result["clues"].append({
                    "id": eid, "text": ev.description,
                    "category": ev.metadata.get("category", "?"),
                    "source": ev.metadata.get("source", ""),
                    "linked_to": ev.metadata.get("linked_to", []),
                })
            elif ev.category in ("story", "quest") and status == "active":
                result["story_nodes"].append({
                    "id": eid, "name": ev.name, "description": ev.description,
                })
        return result

    def apply_event_changes(
        self, state: dict, changes: list[dict], turn_number: int,
    ) -> EventResult:
        """统一 CRUD 入口，处理 AI 输出的 event_changes 列表。"""
        result = EventResult()
        for change in changes:
            action = change.get("action", "create")
            eid = change.get("id", "")
            if not eid:
                continue
            if action == "create":
                category = change.get("category", "event")
                fields = change.get("fields", {})
                desc = change.get("description", "")
                if category == "consequence":
                    fields.setdefault("trigger_chance", 0.3)
                    fields.setdefault("turns_delay", 1)
                    fields.setdefault("max_turns", 5)
                    fields.setdefault("importance", "normal")
                    self.add_consequence(state, {"id": eid, "description": desc, **fields})
                elif category == "deadline":
                    fields.setdefault("turns_remaining", 5)
                    fields.setdefault("visible", True)
                    self.add_deadline(state, {"id": eid, "description": desc, **fields}, turn_number)
                elif category == "thread":
                    self.add_thread(state, {
                        "id": eid, "name": change.get("name", eid),
                        "description": desc,
                        "status": fields.get("status", "active"), **fields,
                    }, turn_number)
                elif category == "clue":
                    self.add_clue(state, {"id": eid, "text": desc, **fields}, turn_number)
                    # 前端兼容：同步写入 clue_board
                    clue_board = state.setdefault("clue_board", [])
                    if not any(c.get("id") == eid for c in clue_board):
                        clue_board.append({
                            "id": eid, "text": desc,
                            "category": fields.get("category", "事件"),
                            "source": fields.get("source", ""),
                            "turn_discovered": turn_number,
                            "linked_to": [],
                        })
            elif action == "update":
                fields = change.get("fields", {})
                if eid in self.events:
                    ev = self.events[eid]
                    evs = state.setdefault("events", {}).setdefault(eid, {})
                    if "status" in fields:
                        evs["status"] = fields["status"]
                        if fields["status"] in ("completed", "expired") and ev.category == "deadline":
                            ad = state.get("active_deadlines", [])
                            state["active_deadlines"] = [d for d in ad if d.get("id") != eid]
                    if "description" in fields:
                        ev.description = fields["description"]
                    if "name" in fields:
                        ev.name = fields["name"]
                    ev.last_updated = turn_number
                    evs["last_updated"] = turn_number
                    ev.metadata.update(fields)
            elif action == "delete":
                if eid in self.events:
                    state.setdefault("events", {}).pop(eid, None)
                    del self.events[eid]
        return result

    def summarize_tree_progress(self, state: dict) -> str:
        self._migrate_state(state)
        es = self._init_state(state)
        lines = []
        for tree_meta in self._trees:
            tree_id = tree_meta["id"]
            tree_events = [ev for ev in self.events.values() if ev.tree_id == tree_id]
            if not tree_events:
                continue
            total = len(tree_events)
            done = sum(1 for ev in tree_events if es.get(ev.id, {}).get("status") == "completed")
            act = sum(1 for ev in tree_events if es.get(ev.id, {}).get("status") == "active")
            name = tree_meta.get("name", tree_id)
            lines.append(f"[{name}] {done}/{total}完成, {act}进行中")
        return "; ".join(lines) if lines else ""

    # ---- Helpers ----------------------------------------------------------

    def _complete_event(
        self, ev: GameEvent, es: dict,
        result: EventResult, completed: set, unlocked: set,
    ):
        completed.add(ev.id)
        es.setdefault(ev.id, {})["status"] = "completed"
        result.newly_completed.append(self._event_to_dict(ev))
        self._apply_effects(ev.effects, result, ev.id)
        for uid in ev.on_complete_unlock:
            unlocked.add(uid)
            es.setdefault(uid, {})["status"] = "unlocked"

    def _apply_effects(self, effects: dict, result: EventResult, source_id: str):
        if not effects or (self.shadow_story_nodes and source_id in self._tree_for_event
                           and self.events[source_id].category != "trigger"):
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
        for rid in effects.get("reveal_lore", []):
            result.lore_reveals.append(rid)
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
        for ev_id in effects.get("fire_events", []):
            result.game_events.append(ev_id)
        for uid in effects.get("unlock_events", []) + effects.get("unlock_nodes", []):
            result.effects.append({"action": "unlock_node", "params": {"node_id": uid}})
        cb = effects.get("narrative_callback")
        if cb:
            if isinstance(cb, list):
                for c in cb:
                    result.narrative_callbacks.append(
                        c if isinstance(c, dict) else {"text": str(c), "priority": "medium"}
                    )
            elif isinstance(cb, dict):
                result.narrative_callbacks.append(cb)
            else:
                result.narrative_callbacks.append({"text": str(cb), "priority": "medium"})
        for sid in effects.get("activate_state", []):
            result.state_activations.append(sid)
        for sid in effects.get("deactivate_state", []):
            result.state_deactivations.append(sid)
        for sc in effects.get("state_change", []):
            result.effects.append({"action": "set_var", "params": sc})
        for so in effects.get("schedule_override", []):
            result.effects.append(so)

    @staticmethod
    def _auto_resolve_choice(ev: GameEvent, condition_eval: Callable | None) -> dict | None:
        choices = ev.choices
        fallback = None
        for choice in choices:
            condition = choice.get("condition", "")
            if isinstance(condition, str) and not condition.strip():
                if fallback is None:
                    fallback = choice
            elif condition_eval is not None and condition_eval(condition):
                return choice
        return fallback

    @staticmethod
    def _event_to_dict(ev: GameEvent) -> dict:
        return {
            "id": ev.id,
            "name": ev.name,
            "description": ev.description,
            "type": ev.category,
            "effects": ev.effects,
            "choices": ev.choices,
            "on_complete_unlock": ev.on_complete_unlock,
        }

    @staticmethod
    def _category_to_node_type(cat: str) -> str:
        mapping = {"story": "auto", "quest": "quest", "choice": "choice", "trigger": "trigger", "periodic": "periodic"}
        return mapping.get(cat, "auto")

    @staticmethod
    def _text_similar(a: str, b: str, threshold: float = 0.6) -> bool:
        if not a or not b:
            return False
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if shorter in longer:
            return True
        sa = {a[i:i+2] for i in range(len(a) - 1)}
        sb = {b[i:i+2] for i in range(len(b) - 1)}
        if not sa or not sb:
            return a == b
        overlap = len(sa & sb)
        union = len(sa | sb)
        return overlap / union >= threshold

    @staticmethod
    def _compare_values(actual, op: str, expected):
        try:
            if isinstance(expected, (int, float)):
                actual = float(actual)
            ops = {
                "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
                ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
                "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
                "contains": lambda a, b: str(b) in str(a),
            }
            fn = ops.get(op)
            return fn(actual, expected) if fn else False
        except (ValueError, TypeError):
            return False
