"""
Tests for the CPR + Regime + Adaptive SuperTrend strategy state machine.

We stub out the regime and AST indicators so each test can program a
specific sequence of (regime, active_dir) outputs and exercise the state
machine deterministically.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from adaptive_supertrend import AdaptiveSnapshot, METHOD_NAMES
from cpr_st_strategy import CPRSuperTrendStrategy, Position
from regime import Regime, RegimeSnapshot


NY = ZoneInfo("America/New_York")


# --- Stubs ---------------------------------------------------------------
class _StubRegime:
    """Returns programmed regime snapshots one per .update() call."""

    def __init__(self, regimes):
        self._regimes = list(regimes)
        self._i = 0

    def update(self, ts, close, pp, fx_start):
        r = self._regimes[self._i] if self._i < len(self._regimes) else self._regimes[-1]
        self._i += 1
        return RegimeSnapshot(
            timestamp=ts, close=close, daily_pp=pp,
            er=0.5, crossings=0,
            in_directional=r in (Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_DOWN),
            regime=r, fx_day_start=fx_start,
        )


class _StubAST:
    """Returns programmed active_dir values, one per .update() call."""

    def __init__(self, dirs):
        self._dirs = list(dirs)
        self._i = 0
        self._bar = -1

    def update(self, ts, o, h, l, c):
        d = self._dirs[self._i] if self._i < len(self._dirs) else self._dirs[-1]
        self._i += 1
        self._bar += 1
        return AdaptiveSnapshot(
            timestamp=ts, bar_index=self._bar,
            open=o, high=h, low=l, close=c,
            active_method_idx=0,
            active_method_name=METHOD_NAMES[0],
            active_dir=d,
            active_st=None, active_atr=None, active_mult=None,
            bull_signal=False, bear_signal=False,
            rsi=None, macd_line=None, macd_signal_val=None,
            rsi_bull_pass=True, rsi_bear_pass=True,
            macd_bull_pass=True, macd_bear_pass=True,
            live_dir=0,
            live_entry=None, live_entry_bar=None,
            live_tsl=None, live_t1=None, live_t2=None, live_t3=None,
            t1_hit=False, t2_hit=False, t3_hit=False,
            tsl_exited_this_bar=False, targets_hit_this_bar=[],
            method_dirs=[d] * 6, method_st_values=[None] * 6,
            method_trade_counts=[0] * 6, method_total_points=[0.0] * 6,
            method_win_rates=[0.0] * 6, method_avg_points=[0.0] * 6,
            method_scores=[-1e10] * 6,
        )


def _strategy_with_stubs(regimes, active_dirs, force_exit_time=time(16, 55)):
    s = CPRSuperTrendStrategy(force_exit_close_time=force_exit_time)
    s.regime = _StubRegime(regimes)
    s.ast = _StubAST(active_dirs)
    return s


def _feed(strategy, prices, tc=100.0, bc=99.0, pp=99.5,
          start_ts=datetime(2026, 5, 19, 8, 0, tzinfo=NY),
          fx_day_start=datetime(2026, 5, 18, 17, 0, tzinfo=NY)):
    """Feed a sequence of closing prices through the strategy."""
    outcomes = []
    for i, c in enumerate(prices):
        ts = start_ts + timedelta(minutes=5 * i)
        outcomes.append(strategy.update(
            timestamp=ts, open_=c, high=c, low=c, close=c,
            daily_tc=tc, daily_bc=bc, daily_pp=pp,
            fx_day_start=fx_day_start,
        ))
    return outcomes


# --- ENTRY ---------------------------------------------------------------
class TestEntry:
    def test_three_gates_align_long(self):
        # bias LONG (close 105 > TC 100), regime directional, AST up
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_UP],
            active_dirs=[1, 1],
        )
        outs = _feed(s, [105.0, 105.0])
        # First bar: prev_active_dir is 0 -> no flip yet. Entry possible if all gates align.
        assert outs[0].position_after == 1
        assert outs[0].events[0].action == "ENTRY_LONG"
        assert outs[0].events[0].reason == "ALL_GATES_ALIGN"

    def test_three_gates_align_short(self):
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_DOWN],
            active_dirs=[-1],
        )
        outs = _feed(s, [95.0])
        assert outs[0].position_after == -1
        assert outs[0].events[0].action == "ENTRY_SHORT"

    def test_no_entry_when_bias_none(self):
        # close 99.5 is between BC 99 and TC 100 -> bias NONE
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP],
            active_dirs=[1],
        )
        outs = _feed(s, [99.5])
        assert outs[0].position_after == 0
        assert outs[0].events == []

    def test_no_entry_when_regime_not_directional(self):
        s = _strategy_with_stubs(
            regimes=[Regime.MEAN_REVERTING],
            active_dirs=[1],
        )
        outs = _feed(s, [105.0])
        assert outs[0].position_after == 0
        assert outs[0].events == []

    def test_no_entry_when_supertrend_mismatch(self):
        # bias LONG but active_dir = -1 (red)
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP],
            active_dirs=[-1],
        )
        outs = _feed(s, [105.0])
        assert outs[0].position_after == 0
        assert outs[0].events == []

    def test_entry_when_regime_directional_opposite_to_bias(self):
        # bias LONG + regime DIRECTIONAL_DOWN + AST UP -> regime is directional
        # (loose gate), so all 3 gates align -> ENTRY LONG fires.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_DOWN],
            active_dirs=[1],
        )
        outs = _feed(s, [105.0])
        assert outs[0].position_after == 1
        assert outs[0].events[0].action == "ENTRY_LONG"

    def test_entry_when_regime_up_with_short_bias(self):
        # bias SHORT + regime DIRECTIONAL_UP + AST DOWN -> regime is directional
        # (loose gate), so all 3 align -> ENTRY SHORT fires.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP],
            active_dirs=[-1],
        )
        outs = _feed(s, [95.0])
        assert outs[0].position_after == -1
        assert outs[0].events[0].action == "ENTRY_SHORT"

    def test_already_long_blocks_new_long(self):
        # Enter long on bar 1, then on bar 2 conditions still align - no new entry.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[1, 1, 1],
        )
        outs = _feed(s, [105.0, 106.0, 107.0])
        assert outs[0].events[0].action == "ENTRY_LONG"
        assert outs[1].events == []   # already long -> no further entry
        assert outs[2].events == []


# --- EXIT (SuperTrend flip) ----------------------------------------------
class TestExitOnFlip:
    def test_long_exits_on_st_flip_down(self):
        # Enter long, then AST flips to -1 on bar 2 -> exit
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[1, 1, -1],
        )
        outs = _feed(s, [105.0, 106.0, 105.5])
        assert outs[0].events[0].action == "ENTRY_LONG"
        assert outs[1].events == []
        # Bar 2: active_dir flipped 1 -> -1. Position was long, should exit.
        # Close 105.5 is still > TC 100, so bias remains LONG; but new bias is
        # LONG while AST is DOWN - mismatch, no reversal. Just exit.
        assert outs[2].position_after == 0
        assert outs[2].events[0].action == "EXIT_FLIP"
        assert outs[2].events[0].reason == "SUPERTREND_FLIP"

    def test_short_exits_on_st_flip_up(self):
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_DOWN] * 3,
            active_dirs=[-1, -1, 1],
        )
        outs = _feed(s, [95.0, 94.0, 94.5])
        assert outs[0].events[0].action == "ENTRY_SHORT"
        assert outs[2].position_after == 0
        assert outs[2].events[0].action == "EXIT_FLIP"


# --- FORCE EXIT at EOD ---------------------------------------------------
class TestForceExitEOD:
    def test_force_exit_at_close_time(self):
        # force-exit time-of-day = 16:55. So the bar whose close is 16:55 NY
        # has open time 16:50 NY. Construct bars at 16:45 (entry) and 16:50
        # (force exit).
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_UP],
            active_dirs=[1, 1],
            force_exit_time=time(16, 55),
        )
        start = datetime(2026, 5, 19, 16, 45, tzinfo=NY)
        outs = _feed(s, [105.0, 105.0], start_ts=start)
        # bar[0] opens 16:45, closes 16:50 -> not force-exit -> entry
        assert outs[0].position_after == 1
        assert outs[0].events[0].action == "ENTRY_LONG"
        # bar[1] opens 16:50, closes 16:55 -> IS force-exit bar
        assert outs[1].position_after == 0
        assert outs[1].events[0].action == "EXIT_EOD"
        assert outs[1].events[0].reason == "FORCE_EXIT_EOD"

    def test_no_new_entry_on_force_exit_bar(self):
        # If position is FLAT and the bar is force-exit, no entry even if gates align.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP],
            active_dirs=[1],
            force_exit_time=time(16, 55),
        )
        start = datetime(2026, 5, 19, 16, 50, tzinfo=NY)   # this bar closes at 16:55
        outs = _feed(s, [105.0], start_ts=start)
        assert outs[0].position_after == 0
        assert outs[0].events == []


# --- SAME-BAR REVERSAL ---------------------------------------------------
class TestSameBarReversal:
    def test_long_to_short_reversal(self):
        # bar 0: enter long (close > TC, regime dir, AST up)
        # bar 1: still long
        # bar 2: AST flips to -1, close drops below BC, regime still directional
        # -> EXIT_FLIP + REVERSE_TO_SHORT on same bar
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_DOWN],
            active_dirs=[1, 1, -1],
        )
        outs = _feed(s, [105.0, 106.0, 95.0])   # close 95 < BC 99 -> bias SHORT
        # bar 2: should have 2 events: EXIT_FLIP then REVERSE_TO_SHORT
        assert outs[2].position_after == -1
        assert len(outs[2].events) == 2
        assert outs[2].events[0].action == "EXIT_FLIP"
        assert outs[2].events[1].action == "REVERSE_TO_SHORT"

    def test_no_reversal_when_new_bias_doesnt_align(self):
        # bar 2: AST flips to -1 (exit long), but close still > TC -> bias still LONG
        # -> mismatch with new AST direction -> no reversal, just exit
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[1, 1, -1],
        )
        outs = _feed(s, [105.0, 106.0, 105.5])   # close 105.5 > TC 100
        assert outs[2].position_after == 0
        assert len(outs[2].events) == 1
        assert outs[2].events[0].action == "EXIT_FLIP"

    def test_reversal_even_when_regime_doesnt_flip_direction(self):
        # bar 2: AST flips, close drops below BC (bias SHORT), regime stays
        # DIRECTIONAL_UP. Loose regime gate: directional is enough regardless
        # of direction -> all 3 short-side gates align -> REVERSE_TO_SHORT fires.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[1, 1, -1],
        )
        outs = _feed(s, [105.0, 106.0, 95.0])
        assert outs[2].position_after == -1
        assert len(outs[2].events) == 2
        assert outs[2].events[0].action == "EXIT_FLIP"
        assert outs[2].events[1].action == "REVERSE_TO_SHORT"

    def test_no_reversal_on_force_exit_bar(self):
        # force-exit doesn't allow a new entry on the same bar, even if gates align.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_DOWN],
            active_dirs=[1, -1],
            force_exit_time=time(16, 55),
        )
        # bar[0] at 16:45: enter long
        # bar[1] at 16:50: closes at 16:55 -> force-exit bar, AST also flipped,
        #                  close 95 < BC -> would normally reverse, but force-exit blocks new entry.
        outs = _feed(s, [105.0, 95.0], start_ts=datetime(2026, 5, 19, 16, 45, tzinfo=NY))
        assert outs[1].position_after == 0
        assert len(outs[1].events) == 1
        assert outs[1].events[0].action == "EXIT_EOD"


# --- Wait for full alignment --------------------------------------------
class TestWaitForFullAlignment:
    """Two of three gates aligned -> no entry. We must keep checking each
    bar and enter on the first bar where the third gate also aligns."""

    def test_waits_for_regime_to_become_directional(self):
        # bars 0-1: bias LONG, AST UP, regime MEAN_REVERTING -> no entry
        # bar 2:    regime flips to DIRECTIONAL_UP -> ENTER
        s = _strategy_with_stubs(
            regimes=[Regime.MEAN_REVERTING, Regime.MEAN_REVERTING, Regime.DIRECTIONAL_UP],
            active_dirs=[1, 1, 1],
        )
        outs = _feed(s, [105.0, 106.0, 107.0])
        assert outs[0].events == [] and outs[0].position_after == 0
        assert outs[1].events == [] and outs[1].position_after == 0
        assert outs[2].position_after == 1
        assert outs[2].events[0].action == "ENTRY_LONG"

    def test_waits_for_supertrend_to_flip_green(self):
        # bars 0-1: bias LONG, regime DIRECTIONAL_UP, AST DOWN -> no entry
        # bar 2:    AST flips UP -> ENTER
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[-1, -1, 1],
        )
        outs = _feed(s, [105.0, 106.0, 107.0])
        assert outs[0].events == [] and outs[0].position_after == 0
        assert outs[1].events == [] and outs[1].position_after == 0
        assert outs[2].position_after == 1
        assert outs[2].events[0].action == "ENTRY_LONG"

    def test_waits_for_close_to_break_tc(self):
        # bars 0-1: regime UP, AST UP, but close between TC/BC -> bias NONE -> no entry
        # bar 2:    close breaks above TC -> ENTER
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 3,
            active_dirs=[1, 1, 1],
        )
        outs = _feed(s, [99.5, 99.5, 105.0])
        assert outs[0].events == [] and outs[0].position_after == 0
        assert outs[1].events == [] and outs[1].position_after == 0
        assert outs[2].position_after == 1
        assert outs[2].events[0].action == "ENTRY_LONG"

    def test_long_alignment_then_short_alignment_no_entry_between(self):
        # Day starts choppy. Eventually all 3 short-side gates align -> enter.
        # Sequence (loose regime gate):
        #   bar 0: bias NONE,  regime DIR_UP,    AST UP   -> no (bias NONE)
        #   bar 1: bias LONG,  regime MEAN_REV,  AST UP   -> no (regime non-dir)
        #   bar 2: bias SHORT, regime MEAN_REV,  AST -1   -> no (regime non-dir)
        #   bar 3: bias SHORT, regime DIR_DOWN,  AST UP   -> no (AST mismatch)
        #   bar 4: bias SHORT, regime DIR_DOWN,  AST DOWN -> ENTER SHORT
        s = _strategy_with_stubs(
            regimes=[
                Regime.DIRECTIONAL_UP, Regime.MEAN_REVERTING,
                Regime.MEAN_REVERTING, Regime.DIRECTIONAL_DOWN,
                Regime.DIRECTIONAL_DOWN,
            ],
            active_dirs=[1, 1, -1, 1, -1],
        )
        outs = _feed(s, [99.5, 105.0, 95.0, 95.0, 95.0])
        for i in range(4):
            assert outs[i].position_after == 0, f"bar {i} should be flat"
            assert outs[i].events == [], f"bar {i} should produce no events"
        assert outs[4].position_after == -1
        assert outs[4].events[0].action == "ENTRY_SHORT"


# --- Held positions ignore bias ------------------------------------------
class TestPositionHeld:
    def test_long_held_when_bias_drops_to_none(self):
        # Enter long with bias LONG. Then close drops between TC/BC (bias NONE).
        # SuperTrend still up. Position should be held.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 2,
            active_dirs=[1, 1],
        )
        outs = _feed(s, [105.0, 99.5])   # bias NONE on bar 1
        assert outs[0].events[0].action == "ENTRY_LONG"
        assert outs[1].position_after == 1
        assert outs[1].events == []

    def test_long_held_when_bias_flips_short_but_st_stays_up(self):
        # Enter long. Close drops below BC (bias SHORT), but AST still UP.
        # No flip -> no exit. Position held.
        s = _strategy_with_stubs(
            regimes=[Regime.DIRECTIONAL_UP] * 2,
            active_dirs=[1, 1],
        )
        outs = _feed(s, [105.0, 95.0])
        assert outs[1].position_after == 1
        assert outs[1].events == []
