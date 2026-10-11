"""NPC 自主行为引擎：NPC 按日程和目标在后台独立行动"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NPCAutonomy:
    def __init__(self, script_npcs, script_locations):
        self.npcs = {n["id"]: n for n in script_npcs}
        self.locations = {l["id"]: l for l in script_locations}
        self.npc_states = {}  # npc_id → {location, activity, mood, last_updated}

    def tick(self, game_time: str, current_state: dict, *, resolve_npc) -> list[dict]:
        """每轮推进 NPC 的自主行为。返回行为事件列表。"""
        events = []
        try:
            datetime.fromisoformat(game_time.replace("Z", "+00:00"))
        except Exception:
            return events

        query_state = {**current_state, "game_time": game_time}
        for npc_id, npc_data in self.npcs.items():
            resolved = resolve_npc(npc_id, query_state)
            current_location = resolved.get("location", "")
            if not current_location:
                self.npc_states.pop(npc_id, None)
                continue
            current_activity = resolved.get("activity", "")
            old_state = self.npc_states.get(npc_id, {})
            new_state = {
                "location": current_location,
                "activity": current_activity or "空闲",
                "mood": old_state.get("mood", "neutral"),
                "last_updated": game_time,
            }

            # 检测变化
            if old_state.get("location") and old_state["location"] != current_location:
                events.append({
                    "type": "npc_move",
                    "npc_id": npc_id,
                    "npc_name": npc_data.get("name", npc_id),
                    "from": old_state.get("location", ""),
                    "to": current_location,
                    "activity": current_activity,
                })

            self.npc_states[npc_id] = new_state

        return events

    def get_npc_status(self, npc_id: str) -> dict:
        return self.npc_states.get(npc_id, {})

    def get_npcs_at_location(self, location_id: str) -> list[str]:
        return [nid for nid, s in self.npc_states.items() if s.get("location") == location_id]

    def snapshot(self):
        return {"npc_states": self.npc_states}

    @classmethod
    def from_snapshot(cls, script_npcs, script_locations, data):
        a = cls(script_npcs, script_locations)
        a.npc_states = data.get("npc_states", {})
        return a
