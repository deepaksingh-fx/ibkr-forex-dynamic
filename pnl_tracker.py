"""
PnL tracking for SL / daily-breaker / trail-arm decisions (SPEC §11.2, §11.4, §16).

In **live mode**, IBKR's reqPnL / reqPnLSingle would be the source of truth.
In **dry-run mode**, we simulate from price updates we feed in.

For v1 we ship the simulated tracker only. Strategy code talks to PnLTracker
via the abstract interface so a live tracker can drop in later.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def approx_pnl_usd(symbol: str, entry_price: float, current_price: float,
                   side: str, lot_units: float) -> float:
    """
    Rough USD PnL for a forex position (approximate; magnitude correct).

    USD-quote pairs (e.g. EURUSD): exact.
    JPY/CHF/CAD/GBP-quote: divides by an approximate quote-to-USD rate. Right magnitude.
    """
    direction = 1.0 if side == "LONG" else -1.0
    diff = (current_price - entry_price) * direction
    quote = symbol[3:].upper()
    if quote == "USD":
        return diff * lot_units
    if quote == "JPY":
        return diff * lot_units / current_price
    if quote == "CHF":
        return diff * lot_units / 0.78
    if quote == "CAD":
        return diff * lot_units / 1.37
    if quote == "GBP":
        return diff * lot_units * 1.27
    return diff * lot_units   # fallback


@dataclass
class _OpenTrade:
    account: str
    symbol: str
    side: str
    entry_price: float
    lot_units: float
    last_price: float = field(default=0.0)

    @property
    def unrealized(self) -> float:
        if self.last_price <= 0:
            return 0.0
        return approx_pnl_usd(self.symbol, self.entry_price, self.last_price, self.side, self.lot_units)


class PnLTracker(ABC):
    @abstractmethod
    def on_entry(self, account: str, symbol: str, side: str, entry_price: float,
                 lot_units: float, conId: Optional[int] = None) -> None: ...

    @abstractmethod
    def on_exit(self, account: str, symbol: str) -> float: ...
    """Closes the trade, accrues realized PnL into day_pnl, returns the realized."""

    @abstractmethod
    def trade_pnl(self, account: str, symbol: str) -> float: ...

    @abstractmethod
    def day_pnl(self, account: str) -> float: ...

    @abstractmethod
    def update_price(self, symbol: str, price: float) -> None: ...

    @abstractmethod
    def reset_day(self, account: str) -> None: ...


class SimulatedPnLTracker(PnLTracker):
    """Dry-run PnL: math from prices we feed in (e.g. on each 5-min candle close)."""

    def __init__(self):
        self._open: Dict[tuple, _OpenTrade] = {}        # (account, symbol) -> trade
        self._realized_today: Dict[str, float] = {}     # account -> realized today

    def on_entry(self, account, symbol, side, entry_price, lot_units, conId=None):
        key = (account, symbol)
        if key in self._open:
            logger.warning(f"on_entry: {key} already open; replacing")
        self._open[key] = _OpenTrade(account, symbol, side, entry_price, lot_units, last_price=entry_price)

    def on_exit(self, account, symbol):
        key = (account, symbol)
        if key not in self._open:
            return 0.0
        trade = self._open.pop(key)
        realized = trade.unrealized
        self._realized_today.setdefault(account, 0.0)
        self._realized_today[account] += realized
        return realized

    def trade_pnl(self, account, symbol):
        key = (account, symbol)
        return self._open[key].unrealized if key in self._open else 0.0

    def day_pnl(self, account):
        realized = self._realized_today.get(account, 0.0)
        unrealized = sum(t.unrealized for (a, _), t in self._open.items() if a == account)
        return realized + unrealized

    def update_price(self, symbol, price):
        for (a, s), t in self._open.items():
            if s == symbol:
                t.last_price = price

    def reset_day(self, account):
        self._realized_today[account] = 0.0

    def open_positions(self, account: str) -> Dict[str, _OpenTrade]:
        return {s: t for (a, s), t in self._open.items() if a == account}
