"""数据模型：Turn dataclass 和 WorldStateManager"""

import copy
from datetime import datetime
from typing import Dict, List, Any
from dataclasses import dataclass

from engine.state_manager import StateManager
from engine.script_loader import ScriptLoader

# 尝试导入酒馆模块标志
try:
    from engine.world_tree import WorldTree
    TAVERN_MODULES_AVAILABLE = True
except ImportError:
    TAVERN_MODULES_AVAILABLE = False


TIME_HINT_HOURS = {
    "immediate": 0.17,
    "short": 1,
    "medium": 2,
    "half_day": 6,
    "full_day": 12,
    "next_day": 24,
}


@dataclass
class Turn:
    """单个推演轮次的完整记录"""
    turn_num: int
    timestamp: str
    player_action: str
    context_object: Dict[str, Any]
    npc_outputs: Dict[str, Any]
    outline_summary: Dict[str, Any]
    scene_output: Dict[str, Any]
    state_before: Dict[str, Any]
    state_after: Dict[str, Any]
    player_choices: List[Dict[str, Any]]


class WorldStateManager:
    """管理世界的所有状态：变量、关系、属性。
    内部使用 ScriptLoader.create_initial_state 生成的 state dict，
    通过 StateManager.apply_changes 执行状态变更。
    """

    def __init__(self, script_data: Dict[str, Any]):
        self.script_data = script_data
        self._state_manager = StateManager(script_data) if TAVERN_MODULES_AVAILABLE else None

        # 用 ScriptLoader 生成完整初始状态
        if TAVERN_MODULES_AVAILABLE:
            self._state = ScriptLoader.create_initial_state(script_data)
        else:
            self._state = self._fallback_init(script_data)

        # 关系扁平化：ScriptLoader 输出 3D {trust, affection, fear}，引擎用简单 int
        player_rels = self._state.get("player", {}).get("relationships", {})
        flat_rels = {}
        for npc_id, rel in player_rels.items():
            if isinstance(rel, dict):
                flat_rels[npc_id] = rel.get("trust", rel.get("value", 50))
            else:
                flat_rels[npc_id] = rel
        self._flat_relationships = flat_rels

        # 游戏变量（ScriptLoader 不处理 variables，手动初始化）
        self.variables = {}
        for var in script_data.get("variables", []):
            self.variables[var["id"]] = var.get("value", var.get("default", 0))

        # 事件引擎状态（EventEngine.tick 读写此 dict）
        self.event_state: Dict[str, Any] = {}
        # Lorebook 定时状态
        self.lorebook_timed_state: Dict[str, Any] = {}
        # 历史摘要状态
        self.summary_state: Dict[str, Any] = {}
        # 历史日志
        self.turns: List[Turn] = []
        self.current_turn = 0

        # 属性恢复规则：从脚本中提取 recovery_rate
        self._recovery_rules: Dict[str, Dict[str, float]] = {}
        pc = script_data.get("player_character", {})
        for attr_name, attr_def in pc.get("attributes", {}).items():
            if isinstance(attr_def, dict) and attr_def.get("recovery_rate"):
                self._recovery_rules[attr_name] = {
                    "rate": float(attr_def["recovery_rate"]),
                    "max": float(attr_def.get("max", 100)),
                }

        # 时间：从 _state["game_time"] 解析
        game_time = self._state.get("game_time", "")
        if game_time:
            try:
                self._current_date = datetime.fromisoformat(game_time)
            except Exception:
                self._current_date = datetime.now()
        else:
            self._current_date = datetime.now()

    @staticmethod
    def _fallback_init(script_data: dict) -> dict:
        """TAVERN_MODULES_AVAILABLE=False 时的最小化初始状态"""
        pc = script_data.get("player_character", {})
        attrs = {}
        for k, v in pc.get("attributes", {}).items():
            attrs[k] = v["value"] if isinstance(v, dict) and "value" in v else v
        locs = script_data.get("locations", [])
        return {
            "game_time": script_data.get("start_time", ""),
            "player": {
                "location": pc.get("initial_location_id") or (locs[0].get("id", "start") if locs else "start"),
                "attributes": attrs,
                "relationships": copy.deepcopy(pc.get("relationships", {})),
            },
            "world_properties": {p["id"]: p.get("value", "") for p in script_data.get("world_properties", [])},
            "display_names": {},
        }

    # --- 属性访问器（向后兼容） ---

    @property
    def player_attrs(self) -> dict:
        return self._state.get("player", {}).get("attributes", {})

    @player_attrs.setter
    def player_attrs(self, value: dict):
        self._state.setdefault("player", {})["attributes"] = value

    @property
    def relationships(self) -> dict:
        return self._flat_relationships

    @relationships.setter
    def relationships(self, value: dict):
        self._flat_relationships = value

    @property
    def world_props(self) -> dict:
        return self._state.get("world_properties", {})

    @world_props.setter
    def world_props(self, value: dict):
        self._state["world_properties"] = value

    @property
    def current_location(self) -> str:
        return self._state.get("player", {}).get("location", "start")

    @current_location.setter
    def current_location(self, value: str):
        self._state.setdefault("player", {})["location"] = value

    @property
    def current_date(self) -> datetime:
        return self._current_date

    @current_date.setter
    def current_date(self, value: datetime):
        self._current_date = value

    @property
    def display_names(self) -> dict:
        return self._state.get("display_names", {})

    def get_snapshot(self) -> Dict[str, Any]:
        """获取当前完整状态快照"""
        dn = self.display_names
        location_name = dn.get(self.current_location, self.current_location)
        relationships_display = {dn.get(k, k): v for k, v in self._flat_relationships.items()}

        return {
            "turn": self.current_turn,
            "date": self._current_date.isoformat(),
            "location": location_name,
            "location_id": self.current_location,
            "player_attrs": copy.deepcopy(self.player_attrs),
            "relationships": copy.deepcopy(self._flat_relationships),
            "relationships_display": relationships_display,
            "variables": copy.deepcopy(self.variables),
            "world_props": copy.deepcopy(self.world_props),
            "display_names": dn,
            "event_state": copy.deepcopy(self.event_state),
        }

    def apply_changes(self, changes: List[Dict[str, Any]]) -> None:
        """应用状态变更。接受 {"var": path, "op": op, "value": val} 格式。"""
        if self._state_manager:
            adapted = []
            flat_rel_updates = []
            for c in changes:
                var = c.get("var", "")
                if not var:
                    continue
                op = c.get("op", "set")
                value = c.get("value")
                parts = var.split(".")
                if len(parts) == 2 and parts[0] == "relationship":
                    flat_rel_updates.append((parts[1], op, value))
                elif len(parts) == 2 and parts[0] == "player":
                    adapted.append({"target": f"player.attributes.{parts[1]}", "op": op, "value": value})
                elif len(parts) == 2 and parts[0] == "world":
                    adapted.append({"target": f"world_properties.{parts[1]}", "op": op, "value": value})
                else:
                    adapted.append({"target": var, "op": op, "value": value})
            if adapted:
                self._state, _ = self._state_manager.apply_changes(self._state, adapted, inplace=True)
            for key, op, value in flat_rel_updates:
                if op == "add":
                    self._flat_relationships[key] = self._flat_relationships.get(key, 0) + value
                else:
                    self._flat_relationships[key] = value
        else:
            for change in changes:
                var = change.get("var", "")
                op = change.get("op", "set")
                value = change.get("value")
                if not var:
                    continue
                parts = var.split(".")
                if len(parts) == 2:
                    category, key = parts
                    target = (self.player_attrs if category == "player"
                              else self._flat_relationships if category == "relationship"
                              else self.world_props if category == "world" else None)
                    if target is not None:
                        if op == "add":
                            target[key] = target.get(key, 0) + value
                        else:
                            target[key] = value

    def query_info(self, query_type: str, query_key: str) -> Any:
        """Agent查询信息的接口"""
        if query_type == "player_attr":
            return self.player_attrs.get(query_key)
        elif query_type == "relationship":
            return self._flat_relationships.get(query_key)
        elif query_type == "variable":
            return self.variables.get(query_key)
        elif query_type == "world_prop":
            return self.world_props.get(query_key)
        elif query_type == "location":
            return self.current_location
        elif query_type == "date":
            return self._current_date.isoformat()
        return None

    def apply_natural_recovery(self, hours: float) -> List[str]:
        """根据时间流逝恢复具有 recovery_rate 的属性。返回恢复描述列表。"""
        if not self._recovery_rules or hours <= 0:
            return []
        recovered = []
        attrs = self.player_attrs
        for attr_name, rule in self._recovery_rules.items():
            current = attrs.get(attr_name, 0)
            if not isinstance(current, (int, float)):
                continue
            max_val = rule["max"]
            if current >= max_val:
                continue
            gain = rule["rate"] * hours
            new_val = min(max_val, current + gain)
            if new_val > current:
                attrs[attr_name] = round(new_val) if isinstance(current, int) else round(new_val, 1)
                recovered.append(f"{attr_name} +{round(new_val - current, 1)}")
        return recovered
