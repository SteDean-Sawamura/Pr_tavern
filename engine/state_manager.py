"""State manager for applying and validating world state changes."""

import copy
import logging

from engine.event_scheduler import parse_time


class StateManager:
    def __init__(self, script: dict):
        self.script = script
        self._world_aliases = {
            prop["name"]: prop["id"] for prop in script.get("world_properties", [])
            if prop.get("name") and prop.get("id")
        }
        self._attr_rules = {}
        pc = script.get("player_character", {})
        for attr_name, attr in pc.get("attributes", {}).items():
            if isinstance(attr, dict):
                rule = {
                    "min": attr.get("min", 0),
                    "max": attr.get("max", 100),
                }
                self._attr_rules[f"player.{attr_name}"] = rule
                self._attr_rules[f"player.attributes.{attr_name}"] = rule
        for rel_name, rel in pc.get("relationships", {}).items():
            if isinstance(rel, dict):
                rel_rule = {
                    "min": rel.get("min", 0),
                    "max": rel.get("max", 100),
                }
                self._attr_rules[f"player.relationships.{rel_name}"] = rel_rule
                # Also register 3D sub-paths
                for dim in ("trust", "affection", "fear"):
                    self._attr_rules[f"player.relationships.{rel_name}.{dim}"] = rel_rule
        # NPC attitude clamp [0, 100]
        att_rule = {"min": 0, "max": 100}
        for npc in script.get("npcs", []):
            npc_id = npc.get("id", "")
            if npc_id:
                self._attr_rules[f"npcs.{npc_id}.attitude_toward_player"] = att_rule
        # Skill value clamp [0, 100]
        skill_rule = {"min": 0, "max": 100}
        for sid in pc.get("skill_proficiencies", []):
            self._attr_rules[f"player.skills.{sid}.value"] = skill_rule
        # 预解析 persistent_states 的 expires_at，避免每回合重复解析
        self._ps_expires: list[tuple[str, object]] = []
        for ps in script.get("persistent_states", []):
            expires_at = ps.get("expires_at")
            if expires_at:
                t = parse_time(expires_at)
                if t:
                    self._ps_expires.append((ps["id"], t))

    def apply_changes(self, state: dict, changes: list[dict], *, inplace: bool = False) -> tuple[dict, list[dict]]:
        """Apply state changes, return (new_state, change_log_with_old_new).

        If inplace=True, modify state directly instead of deepcopy (caller must manage copies).
        """
        new_state = state if inplace else copy.deepcopy(state)
        log = []

        for change in changes:
            target = change.get("target", "")
            op = change.get("op", "add")
            value = change.get("value", 0)
            reason = change.get("reason", "")

            old_value = self._get_value(new_state, target)
            new_value = self._compute(old_value, op, value)

            # Clamp to rules if they exist
            rule = self._attr_rules.get(target)
            if rule and isinstance(new_value, (int, float)):
                new_value = max(rule["min"], min(rule["max"], new_value))

            self._set_value(new_state, target, new_value)
            log.append({
                "target": target,
                "op": op,
                "value": value,
                "old": old_value,
                "new": new_value,
                "reason": reason,
            })

        return new_state, log

    def check_expirations(self, state: dict, current_time: str, *, inplace: bool = False) -> tuple[dict, list[str]]:
        """Remove expired persistent states.

        If inplace=True, modify state directly.
        """
        new_state = state if inplace else copy.deepcopy(state)
        expired = []

        if not self._ps_expires:
            return new_state, expired

        t_current = parse_time(current_time)
        if not t_current:
            return new_state, expired

        for ps_id, t_expires in self._ps_expires:
            if t_expires <= t_current:
                if ps_id in new_state.get("active_persistent_states", []):
                    new_state["active_persistent_states"].remove(ps_id)
                    expired.append(ps_id)

        return new_state, expired

    def reveal_location(self, state: dict, location_id: str, *, inplace: bool = False) -> dict:
        new_state = state if inplace else copy.deepcopy(state)
        visible = new_state.get("visible_locations", [])
        if location_id not in visible:
            visible.append(location_id)
            new_state["visible_locations"] = visible
        return new_state

    def _canonical_path(self, state: dict, path: str) -> str:
        if path.startswith("world.") and "world" not in state and "world_properties" in state:
            path = "world_properties." + path[6:]
        if path.startswith("world_properties."):
            name = path[len("world_properties."):]
            prop_id = self._world_aliases.get(name)
            if prop_id and prop_id in state.get("world_properties", {}):
                return "world_properties." + prop_id
        return path

    def _get_value(self, state: dict, path: str):
        """Navigate a dotted path like 'player.attributes.health'."""
        path = self._canonical_path(state, path)
        parts = path.split(".")
        obj = state
        for part in parts:
            if isinstance(obj, dict):
                # Try nested lookups: player.mood -> player.attributes.mood
                if part not in obj and "attributes" in obj and part in obj["attributes"]:
                    obj = obj["attributes"][part]
                elif part not in obj and "relationships" in obj and part in obj["relationships"]:
                    obj = obj["relationships"][part]
                else:
                    obj = obj.get(part)
            else:
                return None
        return obj

    def _set_value(self, state: dict, path: str, value):
        """Set a value at a dotted path."""
        path = self._canonical_path(state, path)
        parts = path.split(".")
        obj = state
        for part in parts[:-1]:
            if isinstance(obj, dict):
                if part not in obj and "attributes" in obj and part in obj.get("attributes", {}):
                    obj = obj["attributes"]
                    continue
                if part not in obj and "relationships" in obj and part in obj.get("relationships", {}):
                    obj = obj["relationships"]
                    continue
                if part not in obj:
                    logging.getLogger(__name__).warning(
                        "state_manager: 路径 %r 中的 %r 不存在，自动创建空字典（可能是拼写错误）",
                        path, part,
                    )
                obj = obj.setdefault(part, {})
        final_key = parts[-1]
        if isinstance(obj, dict):
            # Smart lookup for short paths like player.mood
            if final_key not in obj:
                if "attributes" in obj and final_key in obj["attributes"]:
                    obj["attributes"][final_key] = value
                    return
                if "relationships" in obj and final_key in obj["relationships"]:
                    obj["relationships"][final_key] = value
                    return
            obj[final_key] = value

    @staticmethod
    def _compute(old_value, op: str, value):
        if op == "set":
            return value
        # List/string operations
        if op == "append":
            if isinstance(old_value, list):
                return old_value + [value]
            if isinstance(old_value, str):
                return old_value + str(value)
            # 老值为空/None -> 新建列表（P1-7: 数值 0 是有效值，不应视为空）
            if old_value is None or old_value == "":
                return [value]
            return [old_value, value]
        if op == "remove":
            if isinstance(old_value, list):
                return [x for x in old_value if x != value]
            if isinstance(old_value, str) and isinstance(value, str):
                return old_value.replace(value, "")
            return old_value
        if op == "toggle":
            # 布尔翻转；非布尔则按真值翻转为布尔
            return not bool(old_value)
        # For arithmetic ops, ensure both operands are numeric
        if isinstance(old_value, str):
            try:
                old_value = float(old_value)
                if old_value.is_integer():
                    old_value = int(old_value)
            except ValueError:
                old_value = 0
        if not isinstance(old_value, (int, float)):
            old_value = 0
        if not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (ValueError, TypeError):
                return old_value
        if op == "add":
            return old_value + value
        elif op == "multiply":
            return old_value * value
        elif op == "subtract":
            return old_value - value
        elif op == "divide":
            if value == 0:
                return old_value
            result = old_value / value
            return int(result) if isinstance(old_value, int) else result
        return old_value
