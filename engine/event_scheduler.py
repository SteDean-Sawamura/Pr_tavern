"""Event scheduler for cyclic and one-time events."""

import copy
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

def is_public_event(script: dict, event_id: str) -> bool:
    default = script.get("visibility_policy", {}).get("default_event_visibility", "public")
    for section in ("cyclic_events", "one_time_events"):
        for event in script.get(section, []):
            if event.get("id") != event_id:
                continue
            visibility = event.get("visibility")
            return (visibility if visibility is not None else default) == "public"
    return default == "public"


def is_public_story_node(script: dict, node_id: str) -> bool:
    default = script.get("visibility_policy", {}).get("default_story_visibility", "public")
    for tree in script.get("story_tree", {}).get("trees", []):
        for node in tree.get("nodes", []):
            if node.get("id") != node_id:
                continue
            visibility = node.get("visibility")
            return (visibility if visibility is not None else default) == "public"
    return default == "public"


def public_event_notifications(script: dict, result) -> list[str]:
    sources = result.notification_sources
    notifications = result.notifications
    require_source = script.get("visibility_policy", {}).get("require_notification_source", False)
    if len(sources) != len(notifications):
        return [] if require_source else notifications
    story_ids = {
        node["id"] for tree in script.get("story_tree", {}).get("trees", [])
        for node in tree.get("nodes", [])
    }
    scheduled_ids = {
        event["id"] for section in ("cyclic_events", "one_time_events")
        for event in script.get(section, []) if event.get("id")
    }
    return [message for message, source in zip(notifications, sources)
            if (is_public_story_node(script, source) if source in story_ids else
                is_public_event(script, source) if source in scheduled_ids else
                bool(source) or not require_source)]


def parse_time(t: str | None) -> datetime | None:
    if not t:
        return None
    try:
        if isinstance(t, str) and t.endswith('Z'):
            t = t[:-1] + '+00:00'
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def fast_forward_fixed_frequency(next_fire: datetime, start: datetime, value: int, unit: str) -> datetime:
    fixed_units = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}
    if next_fire >= start or unit not in fixed_units:
        return next_fire
    interval = timedelta(**{fixed_units[unit]: value})
    return next_fire + ((start - next_fire) // interval) * interval if interval else next_fire


class EventScheduler:
    def __init__(self, script: dict):
        self.script = script
        # P2: 预构建事件字典，避免每次查找 O(n)
        self._cyclic_by_id = {
            e["id"]: e for e in script.get("cyclic_events", [])
            if isinstance(e, dict) and e.get("id")
        }

    def check_events(
        self, state: dict, old_time: str, new_time: str,
        condition_eval=None,
    ) -> list[dict]:
        """Check all events that should fire between old_time and new_time."""
        triggered = []
        t_old = parse_time(old_time)
        t_new = parse_time(new_time)
        if not t_old or not t_new or t_new <= t_old:
            return triggered

        # Check cyclic events
        trackers = state.get("cyclic_event_trackers", {})
        for event in self.script.get("cyclic_events", []):
            eid = event["id"]
            tracker = trackers.get(eid, {})
            next_fire = parse_time(tracker.get("next_fire"))
            expires_at = parse_time(event.get("expires_at"))

            if not next_fire:
                continue
            # B12: 过期事件应限制触发到过期时间，而非跳过整个事件
            effective_end = t_new
            if expires_at:
                if expires_at < t_old:
                    continue  # 已过期，跳过
                effective_end = min(t_new, expires_at)

            freq_val = event.get("frequency_value", 1)
            if freq_val <= 0:
                continue  # Skip invalid frequency to prevent infinite loop

            freq_unit = event.get("frequency_unit", "day")
            next_fire = fast_forward_fixed_frequency(next_fire, t_old, freq_val, freq_unit)

            # Fire all occurrences in the window (with safety limit)
            max_iterations = 1000
            iterations = 0
            while next_fire and next_fire <= effective_end and iterations < max_iterations:
                iterations += 1
                if next_fire >= t_old:
                    cond = event.get("condition", "")
                    if cond and condition_eval and not condition_eval(cond):
                        prev_fire = next_fire
                        next_fire = self._advance_time(
                            next_fire,
                            freq_val,
                            event.get("frequency_unit", "day"),
                        )
                        if next_fire and next_fire <= prev_fire:
                            break
                        continue
                    triggered.append({
                        "type": "cyclic",
                        "event_id": eid,
                        "description": event.get("description", ""),
                        "fire_time": next_fire.isoformat(),
                    })
                prev_fire = next_fire
                next_fire = self._advance_time(
                    next_fire,
                    freq_val,
                    event.get("frequency_unit", "day"),
                )
                if next_fire and next_fire <= prev_fire:
                    break  # Safety: time didn't advance

        # Check one-time events
        fired = set(state.get("fired_one_time_events", []))
        for event in self.script.get("one_time_events", []):
            eid = event["id"]
            if eid in fired:
                continue
            trigger_time = parse_time(event.get("trigger_time"))
            if trigger_time and t_old <= trigger_time <= t_new:
                cond = event.get("condition", "")
                if cond and condition_eval and not condition_eval(cond):
                    continue
                triggered.append({
                    "type": "one_time",
                    "event_id": eid,
                    "description": event.get("description", ""),
                    "fire_time": trigger_time.isoformat(),
                })

        # Sort by fire time
        triggered.sort(key=lambda e: e["fire_time"])
        return triggered

    def iter_event_pages(self, state: dict, old_time: str, new_time: str):
        """Yield complete time-ordered pages across capped scheduler windows."""
        cursor = parse_time(old_time)
        end = parse_time(new_time)
        if not cursor or not end or end <= cursor:
            return
        while cursor <= end:
            events = [
                event for event in self.check_events(state, cursor.isoformat(), new_time)
                if cursor <= parse_time(event["fire_time"]) <= end
            ]
            times = sorted({event["fire_time"] for event in events}, key=parse_time)
            if not times:
                return
            page_end = parse_time(times[min(1000, len(times)) - 1])
            page_events = [event for event in events if parse_time(event["fire_time"]) <= page_end]
            yield cursor.isoformat(), page_end.isoformat(), page_events
            cursor = page_end + timedelta(microseconds=1)

    def iter_event_times(self, state: dict, old_time: str, new_time: str):
        """Yield chronological event times across capped scheduler windows."""
        for _, _, events in self.iter_event_pages(state, old_time, new_time):
            yield from sorted({event["fire_time"] for event in events}, key=parse_time)

    def update_trackers(
        self, state: dict, triggered_events: list[dict], new_time: str,
        *, inplace: bool = False,
    ) -> dict:
        """Update event trackers after events fire."""
        new_state = state if inplace else copy.deepcopy(state)
        trackers = new_state.setdefault("cyclic_event_trackers", {})
        fired = new_state.setdefault("fired_one_time_events", [])
        t_new = parse_time(new_time)

        for event_info in triggered_events:
            eid = event_info["event_id"]
            if event_info["type"] == "cyclic":
                # Advance next_fire past new_time
                script_event = self._find_cyclic_event(eid)
                if script_event and eid in trackers:
                    freq_val = script_event.get("frequency_value", 1)
                    if freq_val <= 0:
                        continue
                    fire_time = parse_time(event_info["fire_time"])
                    freq_unit = script_event.get("frequency_unit", "day")
                    next_fire = self._advance_time(
                        fire_time, freq_val, freq_unit,
                    )
                    if next_fire and t_new:
                        next_fire = fast_forward_fixed_frequency(next_fire, t_new, freq_val, freq_unit)
                    max_iter = 1000
                    i = 0
                    while next_fire and t_new and next_fire <= t_new and i < max_iter:
                        i += 1
                        prev = next_fire
                        next_fire = self._advance_time(
                            next_fire, freq_val, freq_unit,
                        )
                        if next_fire and next_fire <= prev:
                            break
                    trackers[eid] = {
                        "last_fired": event_info["fire_time"],
                        "next_fire": next_fire.isoformat() if next_fire else None,
                    }
            elif event_info["type"] == "one_time":
                if eid not in fired:
                    fired.append(eid)

        return new_state

    def _find_cyclic_event(self, event_id: str) -> dict | None:
        return self._cyclic_by_id.get(event_id)

    @staticmethod
    def _advance_time(
        dt: datetime, value: int, unit: str
    ) -> datetime | None:
        if not dt:
            return None
        if unit == "day":
            return dt + timedelta(days=value)
        elif unit == "week":
            return dt + timedelta(weeks=value)
        elif unit == "month":
            return dt + relativedelta(months=value)
        elif unit == "hour":
            return dt + timedelta(hours=value)
        elif unit == "minute":
            return dt + timedelta(minutes=value)
        # B13: 未知单位记日志而非静默回退
        import logging
        logging.getLogger(__name__).warning(
            "未知的频率单位 %r，回退按 day 处理", unit
        )
        return dt + timedelta(days=value)
