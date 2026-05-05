"""
Tests for the rejection-handling contract on _open_position / _exit_position.

Surface area covered:
  - filled                 → state recorded with actual fill price
  - dry_run                → state recorded with bar-close price
  - rejected (entry)       → no state recorded, account halted
  - timeout (entry)        → no state recorded, account halted
  - rejected (exit)        → state PRESERVED (IBKR still holds it), account halted
  - timeout (exit)         → state PRESERVED, account halted
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from cpr import CPR
from strategy import Strategy, _PairState, _Position
from config import StrategyConfig, IBKRConnection


# ─── Mocks ────────────────────────────────────────────────────────────────
class _MockIBKR:
    """Minimal IBKRClient stub. Each call to place_market_order pops from the
    queued results list, so each test queues whatever it wants returned."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.results: List[Dict[str, Any]] = []   # FIFO queue of return values

    def queue(self, status: str, *,
              fill_price: Optional[float] = None,
              fill_qty: Optional[int] = None,
              error: Optional[str] = None) -> None:
        self.results.append({
            "status": status, "fill_price": fill_price,
            "fill_qty": fill_qty, "error": error, "intent": None, "trade": None,
        })

    async def place_market_order(self, account, symbol, side, lot_size,
                                 timeout_s: float = 15.0):
        self.calls.append({"account": account, "symbol": symbol,
                           "side": side, "lot_size": lot_size})
        if not self.results:
            raise AssertionError("no queued result for place_market_order call")
        return self.results.pop(0)


class _MockPnL:
    def __init__(self) -> None:
        self.entries: List[tuple] = []
        self.exits: List[tuple] = []

    def on_entry(self, account, symbol, side, entry_price, lot_units, conId=None):
        self.entries.append((account, symbol, side, entry_price, lot_units))

    def on_exit(self, account, symbol):
        self.exits.append((account, symbol))
        return 0.0   # realized

    def trade_pnl(self, account, symbol):
        return 0.0

    def day_pnl(self, account):
        return 0.0

    def update_price(self, symbol, price):
        pass


class _MockBalances:
    pass


def _make_strategy(live: bool = True) -> Strategy:
    cfg = StrategyConfig(
        allowed_currencies=("USD",),
        LIVE_TRADING=live,
        ibkr=IBKRConnection(read_only=not live),
    )
    return Strategy(cfg, _MockIBKR(), _MockPnL(), _MockBalances(), state_store=None)


def _make_pair_state(symbol: str = "EURUSD",
                     accounts: Optional[List[str]] = None) -> _PairState:
    accts = accounts or ["U25265693"]
    cpr = CPR(high=1.17204, low=1.17200, close=1.17202,
              pivot=1.17202, bc=1.17202, tc=1.17203)
    ps = _PairState(symbol=symbol, accounts=accts,
                     weekly_cpr=cpr, daily_cpr=cpr)
    for a in accts:
        ps.positions[a] = None
        ps.halted[a] = False
    return ps


# ─── _open_position ──────────────────────────────────────────────────────
class TestOpenPosition:
    def test_filled_records_state_with_actual_fill_price(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        bar_close = 1.17258
        actual_fill = 1.17260                          # slight slippage from bar close
        s.ibkr.queue("filled", fill_price=actual_fill, fill_qty=10000)

        asyncio.run(s._open_position(
            ps, acct, "LONG", bar_close,
            datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ))

        pos = ps.positions[acct]
        assert pos is not None
        assert pos.side == "LONG"
        assert pos.entry_price == pytest.approx(actual_fill)
        assert ps.halted[acct] is False
        assert s.pnl.entries == [(acct, "EURUSD", "LONG", actual_fill, 20000)]
        assert s.ibkr.calls[0]["side"] == "BUY"

    def test_dry_run_records_state_with_bar_close_price(self):
        s = _make_strategy(live=False)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        bar_close = 1.17258
        s.ibkr.queue("dry_run")

        asyncio.run(s._open_position(
            ps, acct, "LONG", bar_close,
            datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ))

        pos = ps.positions[acct]
        assert pos is not None
        assert pos.entry_price == pytest.approx(bar_close)
        assert ps.halted[acct] is False

    def test_rejected_does_not_record_state_and_halts(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        s.ibkr.queue("rejected", error="Error 201: verification token required")

        asyncio.run(s._open_position(
            ps, acct, "LONG", 1.17258,
            datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ))

        assert ps.positions[acct] is None
        assert ps.halted[acct] is True
        assert s.pnl.entries == []

    def test_timeout_does_not_record_state_and_halts(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        s.ibkr.queue("timeout", error="timeout after 15s")

        asyncio.run(s._open_position(
            ps, acct, "SHORT", 1.17143,
            datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ))

        assert ps.positions[acct] is None
        assert ps.halted[acct] is True


# ─── _exit_position ──────────────────────────────────────────────────────
class TestExitPosition:
    def test_filled_clears_state(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        ps.positions[acct] = _Position(
            side="LONG", entry_price=1.17258,
            entry_time=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        )
        s.ibkr.queue("filled", fill_price=1.17320, fill_qty=10000)

        asyncio.run(s._exit_position(ps, acct, "EOD_EXIT", 1.17320))

        assert ps.positions[acct] is None
        assert ps.halted[acct] is False
        assert s.pnl.exits == [(acct, "EURUSD")]
        assert s.ibkr.calls[0]["side"] == "SELL"   # offset for LONG

    def test_dry_run_clears_state(self):
        s = _make_strategy(live=False)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        ps.positions[acct] = _Position(
            side="SHORT", entry_price=1.17143,
            entry_time=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        )
        s.ibkr.queue("dry_run")

        asyncio.run(s._exit_position(ps, acct, "TRAIL_EXIT", 1.17240))

        assert ps.positions[acct] is None
        assert ps.halted[acct] is False

    def test_rejected_PRESERVES_state_and_halts(self):
        """Critical: if exit is rejected, IBKR still has the position.
        We must NOT clear our state, otherwise we'd lose track of a real position."""
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        original_pos = _Position(
            side="LONG", entry_price=1.17258,
            entry_time=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        )
        ps.positions[acct] = original_pos
        s.ibkr.queue("rejected", error="connection blip")

        asyncio.run(s._exit_position(ps, acct, "SL_HIT", 1.17100))

        assert ps.positions[acct] is original_pos     # state preserved exactly
        assert ps.halted[acct] is True                # account halted
        assert s.pnl.exits == []                      # pnl NOT closed out

    def test_timeout_PRESERVES_state_and_halts(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        original_pos = _Position(
            side="SHORT", entry_price=1.17143,
            entry_time=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        )
        ps.positions[acct] = original_pos
        s.ibkr.queue("timeout", error="timeout after 15s")

        asyncio.run(s._exit_position(ps, acct, "EOD_EXIT", 1.17240))

        assert ps.positions[acct] is original_pos
        assert ps.halted[acct] is True
        assert s.pnl.exits == []

    def test_no_op_when_no_position(self):
        s = _make_strategy(live=True)
        ps = _make_pair_state()
        acct = ps.accounts[0]
        # ps.positions[acct] is already None
        # Don't queue any result - if place_market_order is called, mock raises.

        asyncio.run(s._exit_position(ps, acct, "EOD_EXIT", 1.17240))

        assert ps.positions[acct] is None
        assert s.ibkr.calls == []     # no order should have been placed
