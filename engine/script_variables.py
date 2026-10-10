"""Script variable system: defines, tracks, and expands variables in prompts."""

import re

_VAR_MACRO_RE = re.compile(r'\{\{var::(\w+)\}\}')


class ScriptVariables:
    """Manages script-defined variables stored in game state."""

    def __init__(self, definitions: list[dict]):
        self.definitions = {d["id"]: d for d in definitions if d.get("id")}

    def init_state(self, state: dict) -> dict:
        """Initialize variables in state from definitions."""
        vars_dict = state.setdefault("script_variables", {})
        for vid, defn in self.definitions.items():
            if vid not in vars_dict:
                vars_dict[vid] = defn.get("default", 0)
        return state

    def get(self, state: dict, var_id: str):
        return state.get("script_variables", {}).get(var_id)

    def set(self, state: dict, var_id: str, value):
        defn = self.definitions.get(var_id, {})
        if defn.get("type") == "number":
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = 0
            value = max(defn.get("min", -9999), min(defn.get("max", 9999), value))
        elif defn.get("type") == "bool":
            value = bool(value)
        state.setdefault("script_variables", {})[var_id] = value

    def inc(self, state: dict, var_id: str, amount: int = 1):
        cur = self.get(state, var_id) or 0
        self.set(state, var_id, cur + amount)

    def dec(self, state: dict, var_id: str, amount: int = 1):
        cur = self.get(state, var_id) or 0
        self.set(state, var_id, cur - amount)

    def apply_op(self, state: dict, var_id: str, op: str, value=None):
        """Apply an operation: set, inc, dec, toggle."""
        if op == "set":
            self.set(state, var_id, value)
        elif op == "inc":
            self.inc(state, var_id, int(value) if value else 1)
        elif op == "dec":
            self.dec(state, var_id, int(value) if value else 1)
        elif op == "toggle":
            cur = self.get(state, var_id)
            self.set(state, var_id, not cur)

    def expand_macros(self, text: str, state: dict) -> str:
        """Replace {{var::name}} with variable values."""
        if "{{var::" not in text:
            return text

        def replacer(m):
            val = self.get(state, m.group(1))
            return str(val) if val is not None else ""

        return _VAR_MACRO_RE.sub(replacer, text)
