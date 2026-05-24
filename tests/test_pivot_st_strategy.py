"""Tests for the pivot/SR + quality-SuperTrend state machine.

The SuperTrend is stubbed so we can drive st_dir / state deterministically and
exercise the base-level + entry/exit logic in isolation.
"""
from __future__ import annotations

from datetime import datetime, time

import pytest

from pivot_st_strategy import PivotSuperTrendStrategy
from pivots import Pivots
from quality_supertrend import QSTSnapshot


# Clean level ladder: R4=104 .. P=100 .. S4=96, 1.0 apart.
LADDER = Pivots(
    high=104, low=96, close=100, pp=100,
    r1=101, r2=102, r3=103, r4=104,
    s1=99, s2=98, s3=97, s4=96,
)

DAY1 = datetime(2026, 1, 5, 17, 0)
DAY2 = datetime(2026, 1, 6, 17, 0)


class _FakeST:
    """Controllable stand-in for QualitySuperTrend."""

    def __init__(self):
        self.dir = 0
        self.state = "GREY"

    def update(self, ts, o, h, l, c):
        return QSTSnapshot(ts, c, self.dir, self.state, None, None, None)


def _make():
    s = PivotSuperTrendStrategy(force_exit_close_time=time(16, 55))
    s.st = _FakeST()
    return s


def _bar(s, *, hi, lo, close, st_dir, state, hour=10, minute=0, fx=DAY1):
    s.st.dir = st_dir
    s.st.state = state
    ts = datetime(2026, 1, 5, hour, minute)
    return s.update(ts, open_=close, high=hi, low=lo, close=close,
                    pivots=LADDER, fx_day_start=fx)


def _actions(outcome):
    return [e.action for e in outcome.events]


# --------------------------------------------------------------------------
# Base-level seeding (first candle)
# --------------------------------------------------------------------------
class TestBaseSeeding:
    def test_single_touch_sets_base_no_entry(self):
        s = _make()
        o = _bar(s, hi=99.5, lo=98.5, close=99.2, st_dir=1, state="GREEN")
        assert o.is_first_candle
        assert o.base_level_name == "S1"   # only S1 (99) is in [98.5, 99.5]
        assert o.events == []              # first candle never enters
        assert s.position == 0

    def test_two_touches_picks_closest_to_close(self):
        s = _make()
        # Touches S1(99) and P(100); close 99.8 is nearer P.
        o = _bar(s, hi=100.5, lo=98.5, close=99.8, st_dir=1, state="GREEN")
        assert o.base_level_name == "P"

    def test_zero_touch_first_candle_falls_back_to_closest(self):
        s = _make()
        # Touches nothing (gap between P=100 and R1=101); close 100.4 nearer P.
        o = _bar(s, hi=100.7, lo=100.3, close=100.4, st_dir=1, state="GREEN")
        assert o.base_level_name == "P"


# --------------------------------------------------------------------------
# Entry gates (from candle #2)
# --------------------------------------------------------------------------
class TestEntry:
    def _seed_at_s1(self, s):
        _bar(s, hi=99.5, lo=98.5, close=99.2, st_dir=1, state="GREEN")

    def test_long_entry_when_above_base_and_green(self):
        s = _make()
        self._seed_at_s1(s)
        o = _bar(s, hi=99.8, lo=99.4, close=99.7, st_dir=1, state="GREEN", minute=5)
        assert _actions(o) == ["ENTRY_LONG"]
        assert s.position == 1

    def test_no_entry_when_grey(self):
        s = _make()
        self._seed_at_s1(s)
        o = _bar(s, hi=99.8, lo=99.4, close=99.7, st_dir=1, state="GREY", minute=5)
        assert o.events == []
        assert s.position == 0

    def test_no_entry_when_color_mismatches_bias(self):
        s = _make()
        self._seed_at_s1(s)
        # close above base => LONG bias, but RED only satisfies SHORT.
        o = _bar(s, hi=99.8, lo=99.4, close=99.7, st_dir=-1, state="RED", minute=5)
        assert o.events == []
        assert s.position == 0

    def test_short_entry_when_below_base_and_red(self):
        s = _make()
        self._seed_at_s1(s)
        # Close below S1 base (99) with RED.
        o = _bar(s, hi=98.9, lo=98.6, close=98.7, st_dir=-1, state="RED", minute=5)
        assert _actions(o) == ["ENTRY_SHORT"]
        assert s.position == -1


# --------------------------------------------------------------------------
# Exit logic
# --------------------------------------------------------------------------
class TestExit:
    def _enter_long(self, s):
        _bar(s, hi=99.5, lo=98.5, close=99.2, st_dir=1, state="GREEN")          # seed S1
        _bar(s, hi=99.8, lo=99.4, close=99.7, st_dir=1, state="GREEN", minute=5)  # ENTRY_LONG
        assert s.position == 1

    def test_flip_into_grey_still_exits(self):
        s = _make()
        self._enter_long(s)
        o = _bar(s, hi=99.6, lo=99.2, close=99.3, st_dir=-1, state="GREY", minute=10)
        assert _actions(o) == ["EXIT_FLIP"]
        assert s.position == 0

    def test_same_side_grey_does_not_exit(self):
        s = _make()
        self._enter_long(s)
        # Direction stays up (no flip), colour drops to grey -> hold.
        o = _bar(s, hi=99.9, lo=99.5, close=99.8, st_dir=1, state="GREY", minute=10)
        assert o.events == []
        assert s.position == 1

    def test_same_bar_reversal(self):
        s = _make()
        self._enter_long(s)
        # Flip to down + RED + close below base -> exit then reverse short.
        o = _bar(s, hi=98.9, lo=98.6, close=98.7, st_dir=-1, state="RED", minute=10)
        assert _actions(o) == ["EXIT_FLIP", "REVERSE_TO_SHORT"]
        assert s.position == -1

    def test_force_exit_eod_and_no_entry(self):
        s = _make()
        self._enter_long(s)
        # Bar closes at 16:55; even with a valid long setup, no new entry.
        o = _bar(s, hi=99.9, lo=99.5, close=99.8, st_dir=1, state="GREEN",
                 hour=16, minute=50)
        assert _actions(o) == ["EXIT_EOD"]
        assert s.position == 0


# --------------------------------------------------------------------------
# Dynamic base + bias change while holding
# --------------------------------------------------------------------------
class TestDynamicBase:
    def test_bias_change_does_not_close_position(self):
        s = _make()
        # Seed base at P (touch P only).
        _bar(s, hi=100.4, lo=99.6, close=100.2, st_dir=1, state="GREEN")
        # Enter long above P.
        _bar(s, hi=100.5, lo=100.1, close=100.3, st_dir=1, state="GREEN", minute=5)
        assert s.position == 1
        # New candle touches S1 and closes below it -> base moves to S1, bias
        # flips SHORT, but no ST flip -> position stays open.
        o = _bar(s, hi=99.2, lo=98.8, close=98.9, st_dir=1, state="GREEN", minute=10)
        assert o.base_level_name == "S1"
        assert o.bias == "SHORT"
        assert o.events == []
        assert s.position == 1

    def test_zero_touch_keeps_existing_base(self):
        s = _make()
        _bar(s, hi=99.5, lo=98.5, close=99.2, st_dir=1, state="GREEN")  # seed S1
        o = _bar(s, hi=99.8, lo=99.4, close=99.6, st_dir=1, state="GREY", minute=5)
        assert o.base_level_name == "S1"   # unchanged - nothing touched

    def test_fx_day_rollover_reseeds_base(self):
        s = _make()
        _bar(s, hi=99.5, lo=98.5, close=99.2, st_dir=1, state="GREEN")  # day1 seed
        # New FX day -> next candle is a fresh first candle.
        o = _bar(s, hi=100.4, lo=99.6, close=100.2, st_dir=1, state="GREEN",
                 minute=5, fx=DAY2)
        assert o.is_first_candle
        assert o.base_level_name == "P"
        assert o.events == []
