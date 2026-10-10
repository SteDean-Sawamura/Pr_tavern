"""Tests for engine.dice module."""

import random
import pytest
from engine.dice import DiceRoller, DiceResult


class TestDiceRoller:
    def setup_method(self):
        self.rng = random.Random(42)
        self.roller = DiceRoller(rng=self.rng)

    def test_roll_basic(self):
        result = self.roller.roll({"count": 1, "faces": 6})
        assert 1 <= result.total <= 6
        assert len(result.raw_rolls) == 1
        assert result.modifier == 0
        assert result.formula == "1d6"

    def test_roll_multiple_dice(self):
        result = self.roller.roll({"count": 3, "faces": 6})
        assert len(result.raw_rolls) == 3
        assert result.total_raw == sum(result.raw_rolls)
        assert result.total == result.total_raw
        assert result.formula == "3d6"

    def test_roll_with_positive_modifier(self):
        result = self.roller.roll({"count": 1, "faces": 20, "modifier": 5})
        assert result.modifier == 5
        assert result.total == result.total_raw + 5
        assert result.formula == "1d20+5"

    def test_roll_with_negative_modifier(self):
        result = self.roller.roll({"count": 1, "faces": 20, "modifier": -3})
        assert result.modifier == -3
        assert result.total == result.total_raw - 3
        assert result.formula == "1d20-3"

    def test_roll_defaults(self):
        result = self.roller.roll({})
        assert len(result.raw_rolls) == 1
        assert 1 <= result.raw_rolls[0] <= 100
        assert result.formula == "1d100"

    def test_resolve_ranges_match(self):
        ranges = [
            {"min": 1, "max": 30, "label": "低", "state_changes": [{"target": "x", "op": "set", "value": 1}]},
            {"min": 31, "max": 70, "label": "中"},
            {"min": 71, "max": 100, "label": "高"},
        ]
        label, changes = self.roller.resolve_ranges(15, ranges)
        assert label == "低"
        assert len(changes) == 1

        label, changes = self.roller.resolve_ranges(50, ranges)
        assert label == "中"
        assert changes == []

        label, changes = self.roller.resolve_ranges(85, ranges)
        assert label == "高"

    def test_resolve_ranges_no_match(self):
        ranges = [{"min": 1, "max": 50, "label": "A"}]
        label, changes = self.roller.resolve_ranges(60, ranges)
        assert label is None
        assert changes == []

    def test_roll_and_resolve(self):
        ranges = [
            {"min": 1, "max": 50, "label": "低"},
            {"min": 51, "max": 100, "label": "高"},
        ]
        result = self.roller.roll_and_resolve({"count": 1, "faces": 100}, ranges)
        assert isinstance(result, DiceResult)
        assert result.range_label in ("低", "高")

    def test_check_d100_gte(self):
        result, passed = self.roller.check_d100(50, "gte")
        assert isinstance(passed, bool)
        assert result.formula == "1d100"

    def test_check_d100_lt(self):
        result, passed = self.roller.check_d100(50, "lt")
        assert passed == (result.total < 50)

    def test_deterministic_with_seed(self):
        r1 = DiceRoller(rng=random.Random(123))
        r2 = DiceRoller(rng=random.Random(123))
        res1 = r1.roll({"count": 5, "faces": 20})
        res2 = r2.roll({"count": 5, "faces": 20})
        assert res1.raw_rolls == res2.raw_rolls
