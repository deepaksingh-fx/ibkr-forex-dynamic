"""
Tests for the CFD order-lifecycle manager (no Gateway). A fake IBKR records
every call so we can assert the exact order sequence and the breakeven latch.
"""
from __future__ import annotations

import pytest

from coil_orders import CoilOrderManager


class FakeIBKR:
    def __init__(self, stop_active=True, market_fills=True):
        self.calls = []
        self.stop_active = stop_active
        self.market_fills = market_fills

    async def place_cfd_market(self, symbol, side, units, account):
        self.calls.append(("market", symbol, side, units))
        if not self.market_fills:
            return {"status": "timeout", "fill_price": None, "fill_qty": None, "trade": None}
        return {"status": "dry_run", "fill_price": None, "fill_qty": None, "trade": None}

    async def place_cfd_stop(self, symbol, side, units, stop_price, account):
        self.calls.append(("stop", symbol, side, units, round(stop_price, 5)))
        return {"status": "dry_run"}

    async def confirm_order_active(self, trade, timeout_s=3.0):
        self.calls.append(("confirm",))
        return self.stop_active

    def modify_stop(self, trade, price):
        self.calls.append(("modify", round(price, 5)))

    def cancel_order(self, trade):
        self.calls.append(("cancel",))


async def test_open_long_places_market_then_stop():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    ok = await om.open("NZDJPY", +1, 30000, ref_price=100.0, stop_distance=0.2,
                       usd_per_price_unit=200.0)
    assert ok and om.is_open
    assert om.trade.entry_price == 100.0
    assert om.trade.stop_price == pytest.approx(99.8)      # 100 - 0.2 (long)
    assert ib.calls[0] == ("market", "NZDJPY", "BUY", 30000)
    assert ib.calls[1] == ("stop", "NZDJPY", "SELL", 30000, 99.8)  # protective SELL stop below


async def test_open_retries_stop_then_flattens_as_last_resort():
    # Stop never goes live -> retry MAX_STOP_TRIES times, THEN flatten (last resort).
    ib = FakeIBKR(stop_active=False)
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    ok = await om.open("NZDJPY", +1, 30000, ref_price=100.0, stop_distance=0.2,
                       usd_per_price_unit=200.0)
    assert ok is False and om.is_open is False
    kinds = [c[0] for c in ib.calls]
    assert kinds.count("stop") == om.MAX_STOP_TRIES        # retried, didn't abandon on first miss
    assert kinds.count("confirm") == om.MAX_STOP_TRIES
    assert ib.calls[0] == ("market", "NZDJPY", "BUY", 30000)    # entry placed
    assert ib.calls[-1] == ("market", "NZDJPY", "SELL", 30000)  # flatten only as last resort


async def test_open_keeps_trade_if_stop_activates_on_retry():
    # Stop fails once then succeeds -> trade is kept (entry NOT deleted).
    ib = FakeIBKR(stop_active=False)
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)

    calls = {"n": 0}
    base_confirm = ib.confirm_order_active

    async def confirm(trade, timeout_s=3.0):
        calls["n"] += 1
        ib.calls.append(("confirm",))
        return calls["n"] >= 2          # first attempt fails, second succeeds
    ib.confirm_order_active = confirm

    ok = await om.open("NZDJPY", +1, 30000, 100.0, 0.2, 200.0)
    assert ok is True and om.is_open is True
    assert [c[0] for c in ib.calls].count("stop") == 2     # retried once, then kept the trade
    assert not any(c == ("market", "NZDJPY", "SELL", 30000) for c in ib.calls)  # NOT flattened


async def test_open_short_stop_is_above():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("USDCAD", -1, 30000, ref_price=1.4000, stop_distance=0.001,
                  usd_per_price_unit=21000.0)
    assert om.trade.stop_price == pytest.approx(1.401)     # 1.40 + 0.001 (short)
    assert ib.calls[1] == ("stop", "USDCAD", "BUY", 30000, 1.401)   # protective BUY stop above


async def test_refuse_double_open():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("NZDJPY", +1, 30000, 100.0, 0.2, 200.0)
    n = len(ib.calls)
    ok = await om.open("NZDJPY", -1, 30000, 100.0, 0.2, 200.0)
    assert ok is False and len(ib.calls) == n             # nothing placed


async def test_breakeven_arms_and_latches():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("NZDJPY", +1, 30000, 100.0, 0.2, 200.0)   # $200 per 1.0 move
    # spot 100.1 -> PnL = 0.1*200 = $20 < 50 -> no arm
    assert om.arm_breakeven_if_touched(100.1) is False
    assert om.trade.breakeven_armed is False
    # spot 100.3 -> PnL = $60 >= 50 -> arm, stop moves to entry
    assert om.arm_breakeven_if_touched(100.3) is True
    assert om.trade.breakeven_armed is True
    assert om.trade.stop_price == 100.0
    assert ("modify", 100.0) in ib.calls
    # second touch does not re-arm
    assert om.arm_breakeven_if_touched(100.5) is False


async def test_close_market_closes_then_cancels_stop():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("NZDJPY", +1, 30000, 100.0, 0.2, 200.0)
    ib.calls.clear()
    ok = await om.close("session_end")
    assert ok is True and om.is_open is False
    assert ib.calls[0] == ("market", "NZDJPY", "SELL", 30000)   # close FIRST (confirm fill)
    assert ("cancel",) in ib.calls                              # stop cancelled only AFTER fill


async def test_close_that_never_fills_keeps_position_and_stop():
    # The desync bug: a close that doesn't fill must NOT mark flat or cancel the stop.
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("NZDJPY", +1, 30000, 100.0, 0.2, 200.0)
    ib.calls.clear()
    ib.market_fills = False                                # CFD won't fill the close
    ok = await om.close("opp_signal")
    assert ok is False                                    # reported NOT closed
    assert om.is_open is True                             # still holding the position
    assert om.trade.side == 1
    assert ("cancel",) not in ib.calls                    # stop was NOT cancelled (still protected)
    assert [c[0] for c in ib.calls].count("market") == om.MAX_CLOSE_TRIES  # retried


async def test_spot_pnl_sign():
    ib = FakeIBKR()
    om = CoilOrderManager(ib, "ACC", breakeven_trigger=50.0)
    await om.open("NZDJPY", -1, 30000, 100.0, 0.2, 200.0)   # short
    assert om.spot_pnl(99.5) == pytest.approx(0.5 * 200.0)  # price down -> short profit
    assert om.spot_pnl(100.5) == pytest.approx(-0.5 * 200.0)
