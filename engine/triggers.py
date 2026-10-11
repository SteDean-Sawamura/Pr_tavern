"""Trigger engine: lifecycle hooks that fire actions on game events."""

import re
import operator

from engine.conditions import evaluate_condition
from engine.script_variables import ScriptVariables

_OPS = {
    ">=": operator.ge, "<=": operator.le,
    ">": operator.gt, "<": operator.lt,
    "==": operator.eq, "!=": operator.ne,
}
_COND_RE = re.compile(r'(?:script_variables\.)?(\w+)\s*(>=|<=|>|<|==|!=)\s*(.+)')


class TriggerEngine:
    """Fires scripted actions in response to game lifecycle events."""

    def __init__(self, triggers: list[dict], script_variables: ScriptVariables):
        self.triggers = triggers
        self.variables = script_variables

    def fire(self, event: str, state: dict,
             include_event_triggers: bool = True, condition_eval=None) -> list[dict]:
        """Fire triggers matching event. Returns list of action dicts to execute."""
        results = []
        for t in self.triggers:
            if not t.get("enabled", True):
                continue
            # Schema A: {"event": "xxx", "action": "xxx", "params": {...}}
            if t.get("event") == event:
                if include_event_triggers and self._check_condition(t, state, condition_eval):
                    results.append({"action": t["action"], "params": t.get("params", {})})
                continue
            # Schema B (剧本格式): {"condition": "event.xxx.fired", "actions": [{type, target}]}
            cond = t.get("condition", "")
            if isinstance(cond, str) and cond.startswith("event.") and cond.endswith(".fired"):
                cond_event_id = cond[6:-6]
                if cond_event_id == event:
                    for act in t.get("actions", []):
                        results.append({"action": act.get("type", ""), "params": act})
        return results

    def _check_condition(self, trigger: dict, state: dict, condition_eval=None) -> bool:
        cond = trigger.get("condition", "")
        if condition_eval is not None:
            return condition_eval(cond, state)
        return evaluate_condition(cond, lambda term: self._compare_condition(term, state))

    def _compare_condition(self, condition: str, state: dict) -> bool:
        m = _COND_RE.fullmatch(condition.strip())
        if not m:
            return False
        var_id, op_str, raw_val = m.group(1), m.group(2), m.group(3).strip().strip('"').strip("'")
        current = self.variables.get(state, var_id)
        if current is None:
            return False
        boolean_target = {"true": True, "false": False, "1": True, "0": False,
                          "yes": True, "no": False}.get(raw_val.casefold())
        if isinstance(current, bool) and boolean_target is not None:
            raw_val = str(int(boolean_target))
        try:
            target = int(raw_val) if raw_val.lstrip('-').isdigit() else raw_val
        except (ValueError, TypeError):
            target = raw_val
        if isinstance(current, bool) and not isinstance(target, int):
            return False
        op_fn = _OPS.get(op_str)
        if not op_fn:
            return True
        try:
            return op_fn(current, target)
        except TypeError:
            return False

    def execute_actions(self, actions: list[dict], state: dict) -> dict:
        """Execute action list and return side-effects.

        Returns dict with optional keys:
        - inject_prompts: list[str] — extra prompts to inject before generation
        - notifications: list[str] — messages to show to player
        - lore_activations: list[str] — entry IDs to force-activate
        - lore_deactivations: list[str] — entry IDs to force-deactivate
        """
        effects = {
            "inject_prompts": [],
            "notifications": [],
            "lore_activations": [],
            "lore_deactivations": [],
            "lore_reveals": [],
        }
        for act in actions:
            action_type = act["action"]
            params = act.get("params", {})
            if action_type == "set_var":
                self.variables.apply_op(
                    state,
                    params.get("var_id", ""),
                    params.get("op", "set"),
                    params.get("value"),
                )
            elif action_type == "inject_prompt":
                text = params.get("text", "")
                if text:
                    effects["inject_prompts"].append(text)
            elif action_type == "activate_lore":
                entry_id = params.get("entry_id", "")
                if entry_id:
                    effects["lore_activations"].append(entry_id)
            elif action_type == "deactivate_lore":
                entry_id = params.get("entry_id", "")
                if entry_id:
                    effects["lore_deactivations"].append(entry_id)
            elif action_type == "reveal_lore":
                entry_id = params.get("entry_id", "")
                if entry_id:
                    effects["lore_reveals"].append(entry_id)
            elif action_type == "notify":
                msg = params.get("message", "")
                if msg:
                    effects["notifications"].append(msg)
            elif action_type == "schedule_override":
                npc_id = params.get("npc_id", "")
                if npc_id and params.get("time_range") and params.get("location"):
                    overrides = state.setdefault("npc_schedule_overrides", {})
                    npc_ov = overrides.setdefault(npc_id, [])
                    entry = {
                        "time_range": params["time_range"],
                        "location": params["location"],
                        "activity": params.get("activity", ""),
                        "priority": params.get("priority", 100),
                    }
                    if params.get("expires"):
                        entry["expires"] = params["expires"]
                    npc_ov.append(entry)
            elif action_type == "activate_persistent_state":
                target = params.get("target", "")
                if target:
                    aps = state.setdefault("active_persistent_states", [])
                    if target not in aps:
                        aps.append(target)
            elif action_type == "deactivate_persistent_state":
                target = params.get("target", "")
                if target:
                    aps = state.get("active_persistent_states", [])
                    if target in aps:
                        aps.remove(target)
        return effects
