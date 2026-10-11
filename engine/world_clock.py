"""世界时钟：枚举声明边界，汇总结算时已核对的后台变化。"""

from datetime import datetime, time, timedelta

from engine.event_scheduler import parse_time


class WorldClock:
    def __init__(self):
        self.last_tick_time: str | None = None

    @staticmethod
    def schedule_boundaries(from_time: str, to_time: str, npcs, state: dict, expirations):
        """只枚举边界，不将候选日程当作已发生的事件。"""
        t_from, t_to = parse_time(from_time), parse_time(to_time)
        if not t_from or not t_to or t_to <= t_from:
            return []
        slots = [slot for npc in npcs for slot in (npc.get("schedule") or [])]
        slots.extend(slot for overrides in state.get("npc_schedule_overrides", {}).values()
                     for slot in overrides)
        boundaries = {expires for state_id, expires in expirations
                      if state_id in state.get("active_persistent_states", []) and t_from < expires < t_to}
        offsets = set()
        for slot in slots:
            expires = parse_time(slot.get("expires", ""))
            if expires and t_from < expires < t_to:
                boundaries.add(expires)
            time_range = slot.get("time_range") or slot.get("time", "")
            if "-" not in time_range:
                continue
            for clock_time in time_range.split("-", 1):
                try:
                    hour, minute = (int(part) for part in clock_time.strip().split(":"))
                except (ValueError, TypeError):
                    continue
                if 0 <= hour <= 24 and 0 <= minute < 60 and (hour != 24 or minute == 0):
                    offsets.add(timedelta(hours=hour, minutes=minute))
        day = t_from.date()
        while day <= t_to.date():
            for offset in offsets:
                boundary = datetime.combine(day, time()) + offset
                if t_from < boundary < t_to:
                    boundaries.add(boundary)
            day += timedelta(days=1)
        return sorted(boundaries)

    def advance(self, from_time: str, to_time: str, *, schedule_events=()) -> list[dict]:
        """汇总已核对的变化，不使用回合终态倒推过去。"""
        t_from, t_to = parse_time(from_time), parse_time(to_time)
        if not t_from or not t_to or t_to < t_from:
            return []
        events = list(schedule_events)
        hours_passed = (t_to - t_from).total_seconds() / 3600

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
        }
