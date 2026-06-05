"""
Order-lifecycle manager for the coil strategy. Turns strategy decisions into the
CFD order sequence and owns the resting stop + breakeven state for ONE open
trade at a time. Gated entirely through IBKRClient (shadow = logs, no orders).

Lifecycle per trade:
  open(side, units, stop_distance)      -> CFD market entry (await fill) + resting CFD stop
  arm_breakeven_if_touched(spot_mid)    -> once spot PnL >= trigger, modify stop -> entry
  close(reason)                         -> cancel resting stop + CFD market close
  reverse(...)                          -> close() then open() opposite (caller drives)

Monitoring is on SPOT (the breakeven check takes a spot mid); execution is on the
CFD (entry/stop/close). Stop PRICE is anchored to the CFD entry fill; the
breakeven TRIGGER is read from spot PnL. See SPEC_COIL.md §6/§6b.

This object holds no strategy logic - the runtime calls it. It is deliberately
small and synchronous-ish so the order sequencing is auditable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ibkr_client import IBKRClient

logger = logging.getLogger(__name__)


@dataclass
class OpenTrade:
    symbol: str
    side: int                 # +1 long / -1 short
    units: int
    entry_price: float        # CFD fill price (stop anchor)
    stop_price: float
    usd_per_price_unit: float  # for spot-PnL math (units * quote->USD)
    stop_trade: object = None  # ib_async Trade for the resting stop (or dry-run dict)
    breakeven_armed: bool = False


class CoilOrderManager:
    """One open CFD trade at a time, with a resting protective stop."""

    def __init__(self, ibkr: IBKRClient, account: str, breakeven_trigger: float):
        self.ibkr = ibkr
        self.account = account
        self.breakeven_trigger = breakeven_trigger
        self.trade: Optional[OpenTrade] = None

    @property
    def is_open(self) -> bool:
        return self.trade is not None

    @staticmethod
    def _entry_side(direction: int) -> str:
        return "BUY" if direction > 0 else "SELL"

    @staticmethod
    def _close_side(direction: int) -> str:
        return "SELL" if direction > 0 else "BUY"

    async def open(self, symbol: str, direction: int, units: int, ref_price: float,
                   stop_distance: float, usd_per_price_unit: float) -> bool:
        """Enter on the CFD, then place the resting protective stop.
        `ref_price` is the signal price (bar close); the real CFD fill price is
        used to anchor the stop when live, falling back to ref_price in shadow.
        Returns True on success. Refuses if a trade is already open."""
        if self.trade is not None:
            logger.error("open() called while a trade is already open - refusing")
            return False

        res = await self.ibkr.place_cfd_market(
            symbol, self._entry_side(direction), units, self.account)
        if res["status"] not in ("filled", "dry_run"):
            logger.error(f"entry not filled ({res['status']}): {res.get('error')}")
            return False

        entry_px = res.get("fill_price") or ref_price   # live: real fill; shadow: bar close
        stop_px = entry_px - stop_distance if direction > 0 else entry_px + stop_distance
        stop_trade = await self.ibkr.place_cfd_stop(
            symbol, self._close_side(direction), units, stop_px, self.account)

        # SAFETY: never hold a position without a confirmed-live stop. If the stop
        # fails to activate, flatten the just-opened position immediately.
        if not await self.ibkr.confirm_order_active(stop_trade):
            logger.error(f"PROTECTIVE STOP failed to activate for {symbol} -> "
                         f"flattening the entry (no naked position)")
            await self.ibkr.place_cfd_market(
                symbol, self._close_side(direction), units, self.account)
            return False

        self.trade = OpenTrade(symbol, direction, units, entry_px, stop_px,
                               usd_per_price_unit, stop_trade)
        logger.info(f"OPEN {symbol} dir={direction} entry={entry_px} stop={stop_px} units={units}")
        return True

    def adopt(self, trade: OpenTrade) -> None:
        """Take over a position recovered from IBKR on restart (with its stop)."""
        if self.trade is not None:
            logger.error("adopt() called while a trade is already open - ignored")
            return
        self.trade = trade
        logger.warning(f"ADOPTED {trade.symbol} side={trade.side} entry={trade.entry_price} "
                       f"stop={trade.stop_price} armed={trade.breakeven_armed}")

    def spot_pnl(self, spot_mid: float) -> float:
        """Unrealized USD PnL of the open trade from a SPOT mid price (proxy)."""
        if self.trade is None:
            return 0.0
        t = self.trade
        return t.side * (spot_mid - t.entry_price) * t.usd_per_price_unit

    def arm_breakeven_if_touched(self, spot_mid: float) -> bool:
        """If unrealized spot PnL has touched the trigger, move the stop to the
        entry price (one-way latch). Returns True the moment it arms."""
        t = self.trade
        if t is None or t.breakeven_armed:
            return False
        if self.spot_pnl(spot_mid) >= self.breakeven_trigger:
            self.ibkr.modify_stop(t.stop_trade, t.entry_price)
            t.stop_price = t.entry_price
            t.breakeven_armed = True
            logger.info(f"BREAKEVEN armed {t.symbol}: stop -> entry {t.entry_price}")
            return True
        return False

    async def close(self, reason: str) -> Optional[float]:
        """Cancel the resting stop and market-close the position on the CFD.
        Returns the close fill price (or None in shadow). Clears the open trade."""
        t = self.trade
        if t is None:
            return None
        self.ibkr.cancel_order(t.stop_trade)            # never leave a naked stop
        res = await self.ibkr.place_cfd_market(
            t.symbol, self._close_side(t.side), t.units, self.account)
        px = res.get("fill_price")
        logger.info(f"CLOSE {t.symbol} ({reason}) fill={px}")
        self.trade = None
        return px
