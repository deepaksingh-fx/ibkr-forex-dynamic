"""
Tests for the coil strategy's bias/DMI signal core (pure, no orders).
"""
from __future__ import annotations

from coil_strategy import CoilBase, entry_dir, reverse_signal
from pivots import compute_pivots


# Pivots from a simple prior day: H=101, L=99, C=100 -> P=100, range=2.
# Levels: R4=104 R3=103 R2=102 R1=101 P=100 S1=99 S2=98 S3=97 S4=96
PIV = compute_pivots(101.0, 99.0, 100.0)


class TestEntryDir:
    def test_long_needs_bias_and_di(self):
        assert entry_dir("LONG", 25.0, 10.0) == 1
        assert entry_dir("LONG", 10.0, 25.0) == 0      # DMI disagrees
        assert entry_dir("SHORT", 10.0, 25.0) == -1
        assert entry_dir("SHORT", 25.0, 10.0) == 0     # DMI disagrees
        assert entry_dir("NONE", 25.0, 10.0) == 0

    def test_di_tie_is_no_trade(self):
        assert entry_dir("LONG", 20.0, 20.0) == 0
        assert entry_dir("SHORT", 20.0, 20.0) == 0


class TestReverseSignal:
    def test_long_reverses_only_on_full_opposite(self):
        # Long held. Bias flipped SHORT but DI+ still > DI- -> NO reverse.
        assert reverse_signal(1, "SHORT", 25.0, 10.0) is False
        # Bias SHORT and DI- > DI+ -> reverse.
        assert reverse_signal(1, "SHORT", 10.0, 25.0) is True
        # Bias still LONG -> never reverse a long.
        assert reverse_signal(1, "LONG", 10.0, 25.0) is False

    def test_short_reverses_only_on_full_opposite(self):
        assert reverse_signal(-1, "LONG", 10.0, 25.0) is False   # DMI not confirming
        assert reverse_signal(-1, "LONG", 25.0, 10.0) is True
        assert reverse_signal(-1, "SHORT", 25.0, 10.0) is False

    def test_flat_never_reverses(self):
        assert reverse_signal(0, "LONG", 25.0, 10.0) is False


class TestCoilBase:
    def test_seed_on_first_candle_picks_touched_closest(self):
        b = CoilBase()
        # First candle straddles R1(101) and P(100); close 100.2 -> closest = P.
        is_first = b.update(high=101.2, low=99.9, close=100.2, pivots=PIV)
        assert is_first is True
        assert b.name == "P" and b.level == 100.0

    def test_seed_with_no_touch_uses_closest_of_all(self):
        b = CoilBase()
        # Candle between P and R1 touching neither; close 100.4 -> closest is P.
        b.update(high=100.6, low=100.1, close=100.4, pivots=PIV)
        assert b.name == "P"

    def test_base_hops_on_later_touch(self):
        b = CoilBase()
        b.update(100.1, 99.9, 100.0, PIV)          # seed at P
        moved = b.update(102.1, 101.9, 102.05, PIV)  # touches R2(102)
        assert moved is False
        assert b.name == "R2" and b.level == 102.0

    def test_base_unchanged_when_no_touch(self):
        b = CoilBase()
        b.update(100.1, 99.9, 100.0, PIV)          # seed at P
        b.update(100.4, 100.2, 100.3, PIV)          # touches nothing
        assert b.name == "P" and b.level == 100.0

    def test_bias_from_base(self):
        b = CoilBase()
        b.update(100.1, 99.9, 100.0, PIV)          # base = P = 100
        assert b.bias(100.5) == "LONG"
        assert b.bias(99.5) == "SHORT"
        assert b.bias(100.0) == "NONE"
