"""Lightweight ledger: structured management of items, NPCs, and locations.

Provides atomic, idempotent delta application so that game-state bookkeeping
for these three categories can be handled independently from the freeform
``state_changes`` pipeline.  Currently only defines the ``Ledger`` class;
integration with tool schemas and the agentic settlement loop is deferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LedgerEntry:
    category: str  # "item" | "npc" | "location"
    id: str
    data: dict = field(default_factory=dict)
    turn_added: int = 0
    turn_updated: int = 0


class Ledger:
    """Atomic incremental projection -- single-submission idempotent."""

    def __init__(self) -> None:
        self.items: dict[str, LedgerEntry] = {}
        self.npcs: dict[str, LedgerEntry] = {}
        self.locations: dict[str, LedgerEntry] = {}
        self._submission_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def apply_delta(
        self,
        delta: list[dict],
        turn: int,
        submission_id: str = "",
    ) -> list[str]:
        """Apply incremental changes.  Returns a human-readable changelog.

        Each *op* in *delta* is a dict with keys:
            category  -- "item" | "npc" | "location"
            action    -- "add" | "update" | "remove"
            id        -- entry identifier
            data      -- dict of fields to set / merge (for add/update)
        """
        if submission_id:
            if submission_id in self._submission_ids:
                return ["duplicate submission, skipped (idempotent)"]
            self._submission_ids.add(submission_id)

        log: list[str] = []
        for op in delta:
            category = op.get("category", "")
            action = op.get("action", "")
            entry_id = op.get("id", "")
            data = op.get("data", {})

            store = self._store_for(category)
            if store is None:
                log.append(f"unknown category: {category}")
                continue

            if action == "add":
                if entry_id in store:
                    store[entry_id].data.update(data)
                    store[entry_id].turn_updated = turn
                    log.append(f"updated {category}/{entry_id}")
                else:
                    store[entry_id] = LedgerEntry(category, entry_id, data, turn, turn)
                    log.append(f"added {category}/{entry_id}")

            elif action == "update":
                if entry_id in store:
                    store[entry_id].data.update(data)
                    store[entry_id].turn_updated = turn
                    log.append(f"updated {category}/{entry_id}")

            elif action == "remove":
                if entry_id in store:
                    del store[entry_id]
                    log.append(f"removed {category}/{entry_id}")

        return log

    def snapshot(self) -> dict:
        """Return a plain-dict view of current ledger state."""
        return {
            "items": {k: v.data for k, v in self.items.items()},
            "npcs": {k: v.data for k, v in self.npcs.items()},
            "locations": {k: v.data for k, v in self.locations.items()},
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _store_for(self, category: str) -> dict[str, LedgerEntry] | None:
        return {
            "item": self.items,
            "npc": self.npcs,
            "location": self.locations,
        }.get(category)
