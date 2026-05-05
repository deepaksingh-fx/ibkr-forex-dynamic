"""
Unit tests for IBKRClient.place_market_order's new fill/rejection contract.

We mock self.ib.placeOrder() to return a fake Trade whose orderStatus we
mutate to simulate IBKR's async lifecycle. The class-level helpers
_wait_for_terminal_status and _extract_rejection_reason are also tested
directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient


# ─── Fakes for ib_async Trade / OrderStatus / log entries ────────────────
@dataclass
class _FakeStatus:
    status: str = "PendingSubmit"
    filled: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class _FakeLogEntry:
    status: str = ""
    message: str = ""
    errorCode: int = 0


@dataclass
class _FakeTrade:
    orderStatus: _FakeStatus = field(default_factory=_FakeStatus)
    log: List[_FakeLogEntry] = field(default_factory=list)


def _config(live: bool = True) -> StrategyConfig:
    return StrategyConfig(
        allowed_currencies=("USD",),
        LIVE_TRADING=live,
        ibkr=IBKRConnection(read_only=not live),
    )


# ─── _wait_for_terminal_status ───────────────────────────────────────────
class TestWaitForTerminalStatus:
    def test_returns_immediately_when_already_filled(self):
        client = IBKRClient(_config(live=True))
        trade = _FakeTrade(orderStatus=_FakeStatus(status="Filled"))
        result = asyncio.run(client._wait_for_terminal_status(trade, timeout_s=2.0))
        assert result == "Filled"

    def test_returns_immediately_when_already_cancelled(self):
        client = IBKRClient(_config(live=True))
        trade = _FakeTrade(orderStatus=_FakeStatus(status="Cancelled"))
        result = asyncio.run(client._wait_for_terminal_status(trade, timeout_s=2.0))
        assert result == "Cancelled"

    def test_returns_when_status_transitions_to_terminal(self):
        client = IBKRClient(_config(live=True))
        trade = _FakeTrade(orderStatus=_FakeStatus(status="PendingSubmit"))

        async def driver():
            # After a brief delay, flip the status to Filled.
            async def flip():
                await asyncio.sleep(0.3)
                trade.orderStatus.status = "Filled"
            asyncio.create_task(flip())
            return await client._wait_for_terminal_status(trade, timeout_s=2.0)

        result = asyncio.run(driver())
        assert result == "Filled"

    def test_returns_timeout_if_no_terminal_state(self):
        client = IBKRClient(_config(live=True))
        trade = _FakeTrade(orderStatus=_FakeStatus(status="PendingSubmit"))
        result = asyncio.run(client._wait_for_terminal_status(trade, timeout_s=0.4))
        assert result == "Timeout"

    def test_inactive_is_terminal_failure(self):
        """Yesterday's verification-token rejection ended up at 'Inactive'."""
        client = IBKRClient(_config(live=True))
        trade = _FakeTrade(orderStatus=_FakeStatus(status="Inactive"))
        result = asyncio.run(client._wait_for_terminal_status(trade, timeout_s=2.0))
        assert result == "Inactive"


# ─── _extract_rejection_reason ───────────────────────────────────────────
class TestExtractRejectionReason:
    def test_returns_default_when_log_empty(self):
        trade = _FakeTrade()
        assert "no error message" in IBKRClient._extract_rejection_reason(trade)

    def test_returns_real_error_when_present(self):
        trade = _FakeTrade(log=[
            _FakeLogEntry(status="Cancelled", errorCode=201,
                          message="Order rejected — leverage"),
        ])
        reason = IBKRClient._extract_rejection_reason(trade)
        assert "201" in reason
        assert "leverage" in reason

    def test_skips_harmless_oddlot_warning(self):
        """Code 399 (odd-lot routing) is not a real error and must be skipped."""
        trade = _FakeTrade(log=[
            _FakeLogEntry(status="ValidationError", errorCode=399,
                          message="Order routed as odd lot"),
        ])
        reason = IBKRClient._extract_rejection_reason(trade)
        assert "no error message" in reason

    def test_returns_most_recent_real_error_when_mixed(self):
        """If both odd-lot warning and real error are in the log, real error wins."""
        trade = _FakeTrade(log=[
            _FakeLogEntry(status="ValidationError", errorCode=399,
                          message="Order routed as odd lot"),
            _FakeLogEntry(status="Inactive", errorCode=201,
                          message="Order increases leveraged FX position"),
        ])
        reason = IBKRClient._extract_rejection_reason(trade)
        assert "201" in reason
        assert "leveraged" in reason


# ─── place_market_order — dry-run path ───────────────────────────────────
class TestPlaceMarketOrderDryRun:
    def test_dry_run_returns_dry_run_status(self):
        client = IBKRClient(_config(live=False))
        result = asyncio.run(client.place_market_order(
            "U25265693", "EURUSD", "BUY", 0.05,
        ))
        assert result["status"] == "dry_run"
        assert result["fill_price"] is None
        assert result["fill_qty"] is None
        assert result["error"] is None
        assert result["intent"]["lot_units"] == 5000


# ─── place_market_order — live path with mocked ib.placeOrder ────────────
class _FakeIB:
    """Minimal stand-in for ib_async.IB used inside IBKRClient for this test."""

    def __init__(self, trade: _FakeTrade):
        self._trade = trade
        self.place_order_calls: List[tuple] = []

    def isConnected(self):
        return True

    def placeOrder(self, contract, order):
        self.place_order_calls.append((contract, order))
        return self._trade


class TestPlaceMarketOrderLive:
    @pytest.fixture
    def patched_client(self, monkeypatch):
        """Build a client whose ib.placeOrder is faked and whose qualify_forex
        returns a stub contract without a network call."""
        client = IBKRClient(_config(live=True))

        async def fake_qualify(symbol):
            class _C:
                conId = 12345
                symbol = "EUR"
                currency = "USD"
            return _C()
        monkeypatch.setattr(client, "qualify_forex", fake_qualify)
        return client

    def test_filled_returns_fill_price_and_qty(self, patched_client):
        trade = _FakeTrade(orderStatus=_FakeStatus(
            status="Filled", filled=5000.0, avgFillPrice=1.17260,
        ))
        patched_client.ib = _FakeIB(trade)

        result = asyncio.run(patched_client.place_market_order(
            "U25265693", "EURUSD", "BUY", 0.05, timeout_s=1.0,
        ))
        assert result["status"] == "filled"
        assert result["fill_price"] == pytest.approx(1.17260)
        assert result["fill_qty"] == 5000
        assert result["error"] is None

    def test_inactive_with_error_returns_rejected(self, patched_client):
        trade = _FakeTrade(
            orderStatus=_FakeStatus(status="Inactive"),
            log=[_FakeLogEntry(status="Inactive", errorCode=201,
                                message="verification required")],
        )
        patched_client.ib = _FakeIB(trade)

        result = asyncio.run(patched_client.place_market_order(
            "U25265693", "EURUSD", "BUY", 0.05, timeout_s=1.0,
        ))
        assert result["status"] == "rejected"
        assert result["fill_price"] is None
        assert "201" in (result["error"] or "")

    def test_no_terminal_state_returns_timeout(self, patched_client):
        trade = _FakeTrade(orderStatus=_FakeStatus(status="PendingSubmit"))
        patched_client.ib = _FakeIB(trade)

        result = asyncio.run(patched_client.place_market_order(
            "U25265693", "EURUSD", "BUY", 0.05, timeout_s=0.4,
        ))
        assert result["status"] == "timeout"
        assert result["fill_price"] is None
        assert "timeout" in (result["error"] or "").lower()

    def test_synchronous_place_order_exception_returns_rejected(self, patched_client):
        class _RaisingIB:
            def placeOrder(self, c, o):
                raise RuntimeError("socket dead")
        patched_client.ib = _RaisingIB()

        result = asyncio.run(patched_client.place_market_order(
            "U25265693", "EURUSD", "BUY", 0.05, timeout_s=1.0,
        ))
        assert result["status"] == "rejected"
        assert "socket dead" in (result["error"] or "")

    def test_invalid_side_raises(self, patched_client):
        with pytest.raises(ValueError):
            asyncio.run(patched_client.place_market_order(
                "U25265693", "EURUSD", "HOLD", 0.05,
            ))
