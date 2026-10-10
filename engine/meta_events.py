"""MetaEventBus — turn-based post-turn hook dispatcher.

Evaluates registered meta-events after each turn and dispatches async
handlers as background tasks. Separate from EventScheduler which is
time-based and fires during turn preparation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MetaEventTrigger:
    turn_interval: int | None = None
    min_turn: int | None = None
    cooldown_turns: int | None = None
    condition: str | None = None
    state_flag: str | None = None
    consume_flag: bool = False
    requires_ai: bool = True
    requires_vector_memory: bool = False


@dataclass
class MetaEvent:
    id: str
    handler: str
    trigger: MetaEventTrigger
    enabled: bool = True
    priority: int = 100


class MetaEventBus:
    """Post-turn event bus: evaluates triggers and returns events to fire."""

    def __init__(self):
        self._events: dict[str, MetaEvent] = {}

    def register(self, event: MetaEvent) -> None:
        self._events[event.id] = event

    def unregister(self, event_id: str) -> None:
        self._events.pop(event_id, None)

    def evaluate(
        self,
        turn_number: int,
        state: dict,
        condition_eval: Callable[[str], bool],
        *,
        ai_available: bool = True,
        vector_available: bool = False,
    ) -> list[MetaEvent]:
        """Return meta-events that should fire this turn, sorted by priority desc."""
        trackers = state.get("meta_event_trackers", {})
        result: list[MetaEvent] = []

        for evt in self._events.values():
            if not evt.enabled:
                continue
            t = evt.trigger
            if t.requires_ai and not ai_available:
                continue
            if t.requires_vector_memory and not vector_available:
                continue
            if t.min_turn is not None and turn_number < t.min_turn:
                continue
            if t.turn_interval is not None and turn_number % t.turn_interval != 0:
                continue
            if t.cooldown_turns is not None:
                last = trackers.get(evt.id, {}).get("last_fired_turn", 0)
                if (turn_number - last) < t.cooldown_turns:
                    continue
            if t.state_flag and not state.get(t.state_flag):
                continue
            if t.condition and not condition_eval(t.condition):
                continue
            result.append(evt)

        result.sort(key=lambda e: e.priority, reverse=True)
        return result

    @staticmethod
    def mark_fired(state: dict, event_id: str, turn_number: int) -> None:
        trackers = state.setdefault("meta_event_trackers", {})
        trackers[event_id] = {"last_fired_turn": turn_number}
