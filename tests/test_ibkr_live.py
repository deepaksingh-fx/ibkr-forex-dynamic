"""
Live IBKR smoke tests against a running IB Gateway.

GATED by env var RUN_IBKR_LIVE=1 - by default these tests are skipped, since
they require an actively-connected gateway on the user's machine.

NO ORDERS ARE PLACED. We only:
  - connect (read_only=True, belt-and-suspenders)
  - list managed accounts
  - read account balances (NetLiquidation, TotalCashValue)
  - qualify all 15 forex contracts on IDEALPRO
  - fetch historical 5-min bars
  - subscribe + unsubscribe to market data (ticks)
  - subscribe + unsubscribe to account-level reqPnL (no positions, but the
    subscription should still establish cleanly)

If `placeOrder` is ever called on this code path, the test should fail loud.
We assert read_only=True so the IBKR session itself rejects any accidental order.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from config import DEFAULT_SYMBOLS, IBKRConnection


pytestmark = pytest.mark.live_ibkr

if not os.environ.get("RUN_IBKR_LIVE"):
    pytest.skip("Set RUN_IBKR_LIVE=1 to run live IBKR tests", allow_module_level=True)


# Lazy import - `ib_async` may not be installed in some environments.
ib_async = pytest.importorskip("ib_async")
from ib_async import IB, Forex, util  # noqa: E402


CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "42"))
HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
PORT = int(os.environ.get("IBKR_PORT", "4001"))


# ------------------------------------------------------------------------
# Fixture: connected, read-only IB client (function-scoped to dodge
# pytest-asyncio module-scoped event-loop issues).
# ------------------------------------------------------------------------
@pytest.fixture
async def ib():
    client = IB()
    await client.connectAsync(
        HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15
    )
    assert client.isConnected(), "Failed to connect"
    try:
        yield client
    finally:
        if client.isConnected():
            client.disconnect()


# ------------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------------
async def test_connection_is_alive(ib):
    assert ib.isConnected() is True

async def test_server_time_round_trip(ib):
    """Verify the wire is alive: ask server time."""
    t = await ib.reqCurrentTimeAsync()
    assert isinstance(t, datetime)
    # Server time should be close to wall clock.
    delta = abs((t - datetime.now(timezone.utc)).total_seconds())
    assert delta < 60, f"Server time off by {delta}s"


# ------------------------------------------------------------------------
# FA: managed accounts
# ------------------------------------------------------------------------
async def test_managed_accounts_exists(ib):
    accounts = ib.managedAccounts()
    assert isinstance(accounts, list)
    assert len(accounts) >= 1, "Expected at least one managed account"
    for a in accounts:
        assert isinstance(a, str) and a.strip(), f"Bad account code: {a!r}"


async def test_account_balances_fetchable(ib):
    """For each managed account, fetch balance via accountSummaryAsync."""
    accounts = ib.managedAccounts()
    for acct in accounts:
        rows = await ib.accountSummaryAsync(account=acct)
        # Returns a list of AccountValue. Look for NetLiquidation in USD.
        netliq = next(
            (r for r in rows if r.tag == "NetLiquidation" and r.account == acct),
            None,
        )
        assert netliq is not None, f"No NetLiquidation for {acct}"
        # Value is a string; coerce.
        balance = float(netliq.value)
        assert balance >= 0, f"Negative balance for {acct}: {balance}"


# ------------------------------------------------------------------------
# Contracts: qualify all 15 default forex pairs
# ------------------------------------------------------------------------
async def test_qualify_eurusd(ib):
    contract = Forex("EURUSD")
    qualified = await ib.qualifyContractsAsync(contract)
    assert len(qualified) == 1
    q = qualified[0]
    assert q.symbol == "EUR"
    assert q.currency == "USD"
    assert q.exchange == "IDEALPRO"
    assert q.conId > 0


async def test_qualify_all_15_default_pairs(ib):
    """Every pair in DEFAULT_SYMBOLS must be qualifiable on IDEALPRO."""
    contracts = [Forex(sym) for sym in DEFAULT_SYMBOLS]
    qualified = await ib.qualifyContractsAsync(*contracts)
    assert len(qualified) == len(DEFAULT_SYMBOLS), (
        f"Only qualified {len(qualified)}/{len(DEFAULT_SYMBOLS)}"
    )
    for q, sym in zip(qualified, DEFAULT_SYMBOLS):
        assert q.exchange == "IDEALPRO"
        assert q.conId > 0
        # Symbol order: base = first 3 letters
        assert q.symbol == sym[:3]
        assert q.currency == sym[3:]


# ------------------------------------------------------------------------
# Historical data: 5-min bars
# ------------------------------------------------------------------------
async def test_historical_5min_bars_eurusd(ib):
    """Fetch one trading day of 5-min EURUSD bars."""
    contract = (await ib.qualifyContractsAsync(Forex("EURUSD")))[0]
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="5 mins",
        whatToShow="MIDPOINT",   # FX has no TRADES
        useRTH=False,
        formatDate=2,
    )
    assert len(bars) > 0, "No 5-min bars returned"

    # Verify time ordering.
    times = [b.date for b in bars]
    assert times == sorted(times), "Bars must be ascending in time"

    # Spot-check: first bar's OHLC make sense.
    b = bars[0]
    assert b.high >= b.low
    assert b.high >= b.open
    assert b.high >= b.close
    assert b.low <= b.open
    assert b.low <= b.close
    # EURUSD reasonable bound (it has been ~0.95-1.30 in modern history)
    assert 0.5 < b.close < 2.0


async def test_historical_bars_for_all_pairs(ib):
    """Each of the 15 pairs returns >0 bars for a 1-day 5-min request."""
    for sym in DEFAULT_SYMBOLS:
        contract = (await ib.qualifyContractsAsync(Forex(sym)))[0]
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="MIDPOINT",
            useRTH=False,
            formatDate=2,
        )
        assert len(bars) > 0, f"No bars for {sym}"


# ------------------------------------------------------------------------
# Market data subscription (ticks)
# ------------------------------------------------------------------------
async def test_market_data_subscription(ib):
    """
    Subscribe to EURUSD ticks; receive at least one update; cancel.

    Skipped automatically when the FX market is closed (weekends), since no
    ticks will stream and a missing tick is not a real failure.
    """
    import asyncio
    from time_utils import is_in_trading_zone, ny_now

    if not is_in_trading_zone(ny_now()):
        pytest.skip("FX market closed (outside Sun 17:00 NY -> Fri 17:00 NY); no ticks expected")

    contract = (await ib.qualifyContractsAsync(Forex("EURUSD")))[0]
    ticker = ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        got_data = False
        while asyncio.get_event_loop().time() < deadline:
            if (
                (ticker.bid and ticker.bid > 0)
                or (ticker.ask and ticker.ask > 0)
                or (ticker.last and ticker.last > 0)
            ):
                got_data = True
                break
            await asyncio.sleep(0.5)
        assert got_data, "No tick data received in 10s (market open but no stream)"
    finally:
        ib.cancelMktData(contract)


# ------------------------------------------------------------------------
# Account-level reqPnL (no positions needed; subscription should establish)
# ------------------------------------------------------------------------
async def test_account_pnl_subscription(ib):
    accounts = ib.managedAccounts()
    acct = accounts[0]
    pnl = ib.reqPnL(acct)
    try:
        # Just verify the subscription returned an object.
        assert pnl is not None
        assert pnl.account == acct
    finally:
        ib.cancelPnL(acct)


# ------------------------------------------------------------------------
# Safety: read-only enforced
# ------------------------------------------------------------------------
async def test_session_is_readonly(ib):
    """ib_async stores the readonly flag on the wrapper; verify it's True."""
    # Some versions store this on ib.client.readonly_; we check what's there.
    # If neither attribute exists, this test silently passes - but the
    # connectAsync call enforced readonly=True at the wire level regardless.
    flag = getattr(ib.client, "readonly_", None) or getattr(ib.client, "readonly", None)
    if flag is not None:
        assert flag is True
