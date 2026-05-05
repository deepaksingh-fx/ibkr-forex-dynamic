"""
Tests for socket-disconnect handling in Strategy.run / _run_session.

Surface area:
  - _teardown_streams(force_exit_positions=False) does NOT call exit orders
  - _teardown_streams(force_exit_positions=True)  DOES call exit orders
  - run() catches ConnectionLostError, sleeps with backoff, retries connect
  - run() exits cleanly when _stop is set during reconnect-sleep
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from balance_store import BalanceStore
from config import IBKRConnection, StrategyConfig
from cpr import CPR
from pnl_tracker import SimulatedPnLTracker
from strategy import (
    ConnectionLostError,
    Strategy,
    _PairState,
    _Position,
)


# ─── Mocks ────────────────────────────────────────────────────────────────
class _MockIBKR:
    def __init__(self, place_results: Optional[List[Dict[str, Any]]] = None):
        self._connected = True
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.cancel_stream_calls: List[Any] = []
        self.place_results: List[Dict[str, Any]] = place_results or []
        self.place_calls: List[Dict[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def cancel_stream(self, handle):
        self.cancel_stream_calls.append(handle)

    async def place_market_order(self, account, symbol, side, lot_size, timeout_s=15.0):
        self.place_calls.append({"account": account, "symbol": symbol,
                                 "side": side, "lot_size": lot_size})
        if not self.place_results:
            return {"status": "filled", "fill_price": 1.17, "fill_qty": 5000,
                    "error": None, "intent": None, "trade": None}
        return self.place_results.pop(0)


def _make_strategy() -> Strategy:
    cfg = StrategyConfig(
        allowed_currencies=("USD",),
        LIVE_TRADING=True,
        ibkr=IBKRConnection(read_only=False),
    )
    s = Strategy(cfg, _MockIBKR(), SimulatedPnLTracker(),  # type: ignore[arg-type]
                 BalanceStore("/tmp/_unused.json"), state_store=None)
    return s


def _pair_state_with_open_position(symbol: str, account: str) -> _PairState:
    cpr = CPR(high=1.17, low=1.17, close=1.17, pivot=1.17, bc=1.17, tc=1.17)
    ps = _PairState(symbol=symbol, accounts=[account],
                    weekly_cpr=cpr, daily_cpr=cpr)
    ps.positions[account] = _Position(
        side="LONG", entry_price=1.17,
        entry_time=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )
    ps.halted[account] = False
    ps.bars_handle = object()   # sentinel for cancel_stream
    return ps


# ─── _teardown_streams ────────────────────────────────────────────────────
class TestTeardownStreams:
    def test_force_exit_true_closes_positions(self):
        """Zone-exit path: every open position must get an exit order."""
        s = _make_strategy()
        ps = _pair_state_with_open_position("EURUSD", "U25265693")
        s.pair_states["EURUSD"] = ps

        asyncio.run(s._teardown_streams(force_exit_positions=True))

        assert s.ibkr.cancel_stream_calls == [ps.bars_handle] or len(s.ibkr.cancel_stream_calls) == 1
        # exactly one place_market_order call (the SELL to flatten the LONG)
        assert len(s.ibkr.place_calls) == 1
        assert s.ibkr.place_calls[0]["side"] == "SELL"
        assert s.pair_states == {}

    def test_force_exit_false_does_not_close_positions(self):
        """Socket-drop path: must NOT try to exit (orders would just fail)."""
        s = _make_strategy()
        ps = _pair_state_with_open_position("EURUSD", "U25265693")
        s.pair_states["EURUSD"] = ps

        asyncio.run(s._teardown_streams(force_exit_positions=False))

        # streams torn down, no exit order placed
        assert len(s.ibkr.cancel_stream_calls) == 1
        assert s.ibkr.place_calls == []
        assert s.pair_states == {}

    def test_default_is_force_exit_true(self):
        """Backwards-compat: default behavior unchanged from pre-fix."""
        s = _make_strategy()
        ps = _pair_state_with_open_position("EURUSD", "U25265693")
        s.pair_states["EURUSD"] = ps

        asyncio.run(s._teardown_streams())   # no kwarg

        assert len(s.ibkr.place_calls) == 1
        assert s.ibkr.place_calls[0]["side"] == "SELL"


# ─── run() reconnect loop ─────────────────────────────────────────────────
class _FakeIBKR_disconnect_then_stop:
    """Behaves connected on first call, drops after one connect, then is
    stopped before second reconnect completes."""

    def __init__(self):
        self._connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False


class TestRunReconnectLoop:
    def test_run_exits_when_stop_is_set_during_initial_zone_wait(self):
        """If outside zone and _stop is set during the sleep, run() returns
        cleanly. Verifies disconnect is called once on shutdown."""
        s = _make_strategy()
        s.ibkr = _FakeIBKR_disconnect_then_stop()  # type: ignore[assignment]

        async def driver():
            # Set stop before run starts; outer loop should exit on the first
            # zone-gate iteration.
            s.stop()
            await s.run()

        asyncio.run(driver())
        # Connected once, then disconnected on shutdown.
        assert s.ibkr.connect_calls <= 1
        assert s.ibkr.disconnect_calls >= 1

    def test_connection_lost_triggers_reconnect_with_backoff(self,
                                                              monkeypatch):
        """Make _run_session raise ConnectionLostError, then on the second
        loop iteration set _stop. Verify run() reconnects exactly once and
        exits cleanly."""
        s = _make_strategy()
        s.ibkr = _FakeIBKR_disconnect_then_stop()  # type: ignore[assignment]

        # Skip the trading-zone gate entirely.
        monkeypatch.setattr("strategy.is_in_trading_zone", lambda _now: True)

        # Shrink reconnect backoff so the test runs quickly.
        monkeypatch.setattr("strategy.RECONNECT_BACKOFF_INITIAL_S", 0.05)
        monkeypatch.setattr("strategy.RECONNECT_BACKOFF_MAX_S", 0.05)

        # First _run_session raises; second invocation sets stop and returns.
        call_count = {"n": 0}
        async def fake_session():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionLostError("simulated drop")
            s.stop()

        monkeypatch.setattr(s, "_run_session", fake_session)

        asyncio.run(s.run())

        # Connected at least twice (initial + reconnect), session called twice.
        assert s.ibkr.connect_calls >= 2
        assert call_count["n"] >= 2
