"""世界时钟：NPC 按游戏时间独立行动，不需要玩家推动。纯规则，不调 LLM。"""

import logging
from datetime import datetime, time, timedelta

logger = logging.getLogger(__name__)


class WorldClock:
    def __init__(self):
        self.last_tick_time: str | None = None
        self.pending_events: list[dict] = []  # NPC 日程事件

    def register_npc_schedule(self, npc_id: str, npc_name: str, schedule: list[dict]):
        """注册 NPC 日程。schedule 为 [{time_range, location, activity}, ...]"""
        for slot in schedule:
            time_range = slot.get("time_range", "")
            if "-" not in time_range:
                continue
            self.pending_events.append({
                "npc_id": npc_id,
                "npc_name": npc_name,
                "time_range": time_range,
                "location": slot.get("location", ""),
                "activity": slot.get("activity", ""),
            })

    def advance(self, from_time: str, to_time: str) -> list[dict]:
        """推进世界时间，返回这段时间内发生的后台事件。"""
        events: list[dict] = []
        try:
            t_from = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
            t_to = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
        except Exception:
            return events

        if t_to <= t_from:
            return events

        hours_passed = (t_to - t_from).total_seconds() / 3600

        # 检查 NPC 日程变化
        scheduled = []
        day = t_from.date()
        while day <= t_to.date():
            for evt in self.pending_events:
                try:
                    start_str = evt["time_range"].split("-", 1)[0].strip()
                    start_at = datetime.combine(day, time.fromisoformat(start_str), tzinfo=t_from.tzinfo)
                except (KeyError, ValueError):
                    continue
                if t_from < start_at <= t_to:
                    scheduled.append((start_at, {
                        "type": "npc_schedule_start",
                        "npc_id": evt["npc_id"],
                        "npc_name": evt["npc_name"],
                        "activity": evt.get("activity", ""),
                        "location": evt.get("location", ""),
                        "time": start_at.isoformat(),
                    }))
            day += timedelta(days=1)
        events.extend(event for _, event in sorted(scheduled, key=lambda item: item[0]))

        # 生成世界动态摘要
        if hours_passed > 1:
            events.append({
                "type": "world_passage",
                "hours": round(hours_passed, 1),
                "summary": f"{round(hours_passed, 1)}小时过去了",
            })

        self.last_tick_time = to_time
        return events

    def snapshot(self) -> dict:
        return {
            "last_tick_time": self.last_tick_time,
            "pending_events_count": len(self.pending_events),
        }
