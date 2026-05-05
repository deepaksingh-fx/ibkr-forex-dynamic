"""
Tests for the cash-ledger fallback in Strategy._reconcile_with_ibkr.

IDEALPRO has a virtual-FX-position threshold (~20k base ccy). Sub-threshold
forex positions don't surface in reqPositionsAsync; they settle into the
multi-currency cash ledger. The reconciler must fall back to the ledger to
match expected positions in that regime — otherwise it would raise
StateMismatchError on every restart while LOT_SIZE < 0.20.

Test surface:
  - position visible via reqPositions      → matched normally (no ledger probe)
  - position invisible (sub-threshold)
       and ledger sign matches             → matched via ledger
       and ledger sign opposes              → mismatch
       and ledger empty                     → mismatch
  - mix of LONG and SHORT across pairs/accounts
  - account base currency (USD) skipped
  - cross-currency pair (EURJPY) — non-USD legs probed
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from balance_store import BalanceStore
from config import IBKRConnection, StrategyConfig
from pnl_tracker import SimulatedPnLTracker
from state_store import (
    PersistedPosition,
    PersistedState,
    StateStore,
)
from strategy import Strategy, StateMismatchError


# ─── ib_async stand-ins ───────────────────────────────────────────────────
class _FakeContract:
    def __init__(self, symbol: str, currency: str, secType: str = "CASH"):
        self.symbol = symbol
        self.currency = currency
        self.secType = secType


class _FakeIbPos:
    """Shape produced by reqPositionsAsync."""
    def __init__(self, account: str, symbol: str, currency: str,
                 position: float, avgCost: float = 0.0):
        self.account = account
        self.contract = _FakeContract(symbol, currency)
        self.position = position
        self.avgCost = avgCost


class _FakeIBKRClient:
    """Minimal IBKRClient stub: just the methods the reconciler calls."""

    def __init__(self,
                 positions: Optional[List[_FakeIbPos]] = None,
                 ledger: Optional[Dict[str, Dict[str, float]]] = None):
        self._positions = positions or []
        self._ledger = ledger or {}
        self.ledger_calls: List[str] = []

    async def get_open_positions(self):
        return list(self._positions)

    async def fetch_cash_ledger(self, account: str):
        self.ledger_calls.append(account)
        return dict(self._ledger.get(account, {}))


# ─── helpers ──────────────────────────────────────────────────────────────
def _make_strategy(client: _FakeIBKRClient,
                   state_store: StateStore,
                   active: Dict[str, float]) -> Strategy:
    cfg = StrategyConfig(
        allowed_currencies=("USD",),
        LIVE_TRADING=False,
        ibkr=IBKRConnection(read_only=True),
    )
    s = Strategy(cfg, client, SimulatedPnLTracker(),  # type: ignore[arg-type]
                 BalanceStore("/tmp/_unused.json"),
                 state_store=state_store)
    s.active_accounts = dict(active)
    return s


def _persist(state_store: StateStore, **positions_per_pair) -> None:
    """positions_per_pair: pair → {account: ('LONG'|'SHORT', entry_price)}"""
    pos: Dict[str, Dict[str, PersistedPosition]] = {}
    for pair, by_acct in positions_per_pair.items():
        pos[pair] = {}
        for acct, (side, px) in by_acct.items():
            pos[pair][acct] = PersistedPosition(
                side=side, entry_price=px,
                entry_time=datetime(2026, 5, 5, tzinfo=timezone.utc).isoformat(),
                trail_armed=False,
            )
    state_store.save(PersistedState(
        fx_day_start=datetime(2026, 5, 5, tzinfo=timezone.utc).isoformat(),
        shortlist=list(positions_per_pair.keys()),
        positions=pos,
    ))


@pytest.fixture
def store(tmp_path: Path):
    return StateStore(tmp_path / "state.json")


# ─── normal path: position is above threshold and visible ────────────────
class TestVisiblePosition:
    def test_above_threshold_long_matches_via_reqpositions(self, store):
        """Position ≥20k EUR shows in reqPositions normally — ledger NOT consulted."""
        _persist(store, EURUSD={"U25265693": ("LONG", 1.17)})
        client = _FakeIBKRClient(
            positions=[_FakeIbPos("U25265693", "EUR", "USD", position=25000)],
            ledger={"U25265693": {"EUR": 25000.0, "USD": -29250.0}},
        )
        s = _make_strategy(client, store, active={"U25265693": 100_000.0})
        asyncio.run(s._reconcile_with_ibkr())
        # Did NOT consult ledger — reqPositions had it.
        assert client.ledger_calls == []

    def test_above_threshold_short_matches(self, store):
        _persist(store, EURUSD={"U25265693": ("SHORT", 1.17)})
        client = _FakeIBKRClient(
            positions=[_FakeIbPos("U25265693", "EUR", "USD", position=-25000)],
        )
        s = _make_strategy(client, store, active={"U25265693": 100_000.0})
        asyncio.run(s._reconcile_with_ibkr())
        assert client.ledger_calls == []


# ─── sub-threshold: ledger fallback resolves mismatch ────────────────────
class TestLedgerFallbackMatches:
    def test_long_eurusd_matches_via_positive_eur_balance(self, store):
        """0.05 lot LONG EURUSD → +5,000 EUR cash; reqPositions shows nothing."""
        _persist(store, EURUSD={"U25265693": ("LONG", 1.17)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"EUR": 5000.0, "USD": -5852.50}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        # Should NOT raise.
        asyncio.run(s._reconcile_with_ibkr())
        assert client.ledger_calls == ["U25265693"]

    def test_short_eurusd_matches_via_negative_eur_balance(self, store):
        _persist(store, EURUSD={"U25265693": ("SHORT", 1.17)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"EUR": -5000.0, "USD": 5852.50}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        asyncio.run(s._reconcile_with_ibkr())

    def test_long_usdjpy_matches_via_negative_jpy_balance(self, store):
        """LONG USDJPY = bought USD with JPY → JPY cash goes negative.
        Probe order is base, then quote — base USD is skipped, quote JPY wins."""
        _persist(store, USDJPY={"U25265693": ("LONG", 150.0)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"USD": 5000.0, "JPY": -750000.0}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        asyncio.run(s._reconcile_with_ibkr())

    def test_short_usdjpy_matches_via_positive_jpy_balance(self, store):
        _persist(store, USDJPY={"U25265693": ("SHORT", 150.0)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"USD": -5000.0, "JPY": 750000.0}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        asyncio.run(s._reconcile_with_ibkr())

    def test_cross_pair_eurjpy_matches_via_eur_leg(self, store):
        """EURJPY has no USD leg — base EUR is probed first."""
        _persist(store, EURJPY={"U25265693": ("LONG", 175.0)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"EUR": 5000.0, "JPY": -875000.0}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        asyncio.run(s._reconcile_with_ibkr())


# ─── sub-threshold: ledger inconsistent → raises ─────────────────────────
class TestLedgerFallbackMismatch:
    def test_state_long_but_ledger_short_raises(self, store):
        """State says LONG but EUR balance is negative → SHORT → mismatch."""
        _persist(store, EURUSD={"U25265693": ("LONG", 1.17)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"EUR": -5000.0, "USD": 5852.50}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        with pytest.raises(StateMismatchError):
            asyncio.run(s._reconcile_with_ibkr())

    def test_state_position_but_ledger_empty_raises(self, store):
        """No reqPositions match AND no non-USD cash → state has stray entry."""
        _persist(store, EURUSD={"U25265693": ("LONG", 1.17)})
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"USD": 14996.0}},   # only USD natural cash
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        with pytest.raises(StateMismatchError):
            asyncio.run(s._reconcile_with_ibkr())


# ─── ledger query is cached per-account within one reconciliation ────────
class TestLedgerCaching:
    def test_ledger_fetched_once_per_account(self, store):
        """Two pairs (EURUSD + EURJPY) on the same account → one ledger call."""
        _persist(
            store,
            EURUSD={"U25265693": ("LONG", 1.17)},
            EURJPY={"U25265693": ("LONG", 175.0)},
        )
        client = _FakeIBKRClient(
            positions=[],
            ledger={"U25265693": {"EUR": 10000.0, "USD": -5852.50, "JPY": -875000.0}},
        )
        s = _make_strategy(client, store, active={"U25265693": 15_000.0})
        asyncio.run(s._reconcile_with_ibkr())
        # Only one ledger fetch despite two expected positions on this account.
        assert client.ledger_calls == ["U25265693"]


# ─── mixed: one position visible, one sub-threshold ──────────────────────
class TestMixed:
    def test_visible_and_subthreshold_both_match(self, store):
        _persist(
            store,
            EURUSD={"U25265693": ("LONG", 1.17), "U25272450": ("SHORT", 1.17)},
        )
        client = _FakeIBKRClient(
            # Only first account's position is visible (above threshold).
            positions=[_FakeIbPos("U25265693", "EUR", "USD", position=25000)],
            ledger={
                "U25265693": {"EUR": 25000.0, "USD": -29250.0},
                "U25272450": {"EUR": -5000.0, "USD": 5852.50},
            },
        )
        s = _make_strategy(
            client, store,
            active={"U25265693": 15_000.0, "U25272450": 10_000.0},
        )
        asyncio.run(s._reconcile_with_ibkr())
        # Ledger is only consulted for the missing one (U25272450).
        assert client.ledger_calls == ["U25272450"]
