"""Dice rolling system for the tavern game."""

import random
from dataclasses import dataclass, field


@dataclass
class DiceResult:
    raw_rolls: list[int]
    total_raw: int
    modifier: int
    total: int
    formula: str
    range_label: str | None = None
    range_state_changes: list[dict] = field(default_factory=list)
    random_item_id: str = ""
    _sustained: bool = False


class DiceRoller:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()

    def roll(self, dice_config: dict) -> DiceResult:
        count = dice_config.get("count", 1)
        faces = dice_config.get("faces", 100)
        modifier = dice_config.get("modifier", 0)
        keep_highest = dice_config.get("keep_highest", 0)
        keep_lowest = dice_config.get("keep_lowest", 0)

        raw_rolls = [self._rng.randint(1, faces) for _ in range(count)]

        # G13: keep highest/lowest 支持
        if keep_highest and 0 < keep_highest < count:
            kept = sorted(raw_rolls, reverse=True)[:keep_highest]
        elif keep_lowest and 0 < keep_lowest < count:
            kept = sorted(raw_rolls)[:keep_lowest]
        else:
            kept = raw_rolls

        total_raw = sum(kept)
        total = total_raw + modifier

        formula = f"{count}d{faces}"
        if keep_highest and 0 < keep_highest < count:
            formula += f"kh{keep_highest}"
        elif keep_lowest and 0 < keep_lowest < count:
            formula += f"kl{keep_lowest}"
        if modifier > 0:
            formula += f"+{modifier}"
        elif modifier < 0:
            formula += str(modifier)

        return DiceResult(
            raw_rolls=raw_rolls,
            total_raw=total_raw,
            modifier=modifier,
            total=total,
            formula=formula,
        )

    def resolve_ranges(self, total: int, ranges: list[dict]) -> tuple[str | None, list[dict]]:
        """Match a dice total against result ranges. Returns (label, state_changes)."""
        for r in ranges:
            r_min = r.get("min")
            r_max = r.get("max")
            if r_min is None or r_max is None:
                continue
            if r_min <= total <= r_max:
                return r.get("label"), r.get("state_changes", [])
        return None, []

    def roll_and_resolve(self, dice_config: dict, ranges: list[dict]) -> DiceResult:
        result = self.roll(dice_config)
        label, changes = self.resolve_ranges(result.total, ranges)
        result.range_label = label
        result.range_state_changes = changes
        return result

    def check_d100(self, threshold: int, comparison: str = "gte") -> tuple[DiceResult, bool]:
        """Roll a d100 and check against a threshold."""
        result = self.roll({"count": 1, "faces": 100, "modifier": 0})
        ops = {
            "gt": result.total > threshold,
            "gte": result.total >= threshold,
            "lt": result.total < threshold,
            "lte": result.total <= threshold,
            "eq": result.total == threshold,
        }
        return result, ops.get(comparison, False)
