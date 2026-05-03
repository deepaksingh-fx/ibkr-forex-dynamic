"""
ib_async wrapper. The single point of contact with IBKR.

Capabilities:
  - connect / disconnect lifecycle
  - list managed FA sub-accounts
  - fetch account balances (NetLiquidation, USD-preferred, BASE fallback)
  - qualify forex contracts on IDEALPRO (cached)
  - fetch historical 5-min bars for a window
  - subscribe to streaming 5-min bars (keepUpToDate=True)
  - place a market order — GATED by LIVE_TRADING; in dry-run, logs intent only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ib_async import IB, BarDataList, Contract, Forex, MarketOrder   # type: ignore[import-untyped]

from config import StrategyConfig

logger = logging.getLogger(__name__)


class IBKRClient:
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.ib = IB()
        self._contracts: Dict[str, Contract] = {}

    # ───────────────────────── lifecycle ─────────────────────────
    async def connect(self) -> None:
        c = self.config.ibkr
        await self.ib.connectAsync(
            c.host, c.port, clientId=c.client_id,
            readonly=c.read_only,
            timeout=c.connect_timeout,
        )
        if not self.ib.isConnected():
            raise RuntimeError(f"Failed to connect to {c.host}:{c.port}")
        logger.info(
            f"IBKR connected host={c.host}:{c.port} clientId={c.client_id} "
            f"readonly={c.read_only} (LIVE_TRADING={self.config.LIVE_TRADING})"
        )

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("IBKR disconnected")

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    # ───────────────────────── accounts ─────────────────────────
    def managed_accounts(self) -> List[str]:
        return list(self.ib.managedAccounts())

    async def fetch_account_balances_usd(self) -> Dict[str, float]:
        """For each managed account, return USD-equivalent NetLiquidation."""
        out: Dict[str, float] = {}
        for acct in self.managed_accounts():
            try:
                values = await self.ib.accountSummaryAsync(account=acct)
            except Exception as e:
                logger.warning(f"accountSummaryAsync failed for {acct}: {e}")
                out[acct] = 0.0
                continue

            # Prefer USD; fall back to BASE.
            netliq = None
            for v in values:
                if v.tag == "NetLiquidation" and v.account == acct and v.currency == "USD":
                    try:
                        netliq = float(v.value)
                    except (ValueError, TypeError):
                        pass
                    break
            if netliq is None:
                for v in values:
                    if v.tag == "NetLiquidation" and v.account == acct:
                        try:
                            netliq = float(v.value)
                        except (ValueError, TypeError):
                            pass
                        break
            out[acct] = netliq if netliq is not None else 0.0
        return out

    # ───────────────────────── contracts ─────────────────────────
    async def qualify_forex(self, symbol: str) -> Contract:
        if symbol in self._contracts:
            return self._contracts[symbol]
        result = await self.ib.qualifyContractsAsync(Forex(symbol))
        if not result or result[0] is None:
            raise RuntimeError(f"Could not qualify forex contract: {symbol}")
        self._contracts[symbol] = result[0]
        return result[0]

    async def qualify_many(self, symbols: List[str]) -> Dict[str, Contract]:
        to_qualify = [s for s in symbols if s not in self._contracts]
        if to_qualify:
            qualified = await self.ib.qualifyContractsAsync(*[Forex(s) for s in to_qualify])
            for q, sym in zip(qualified, to_qualify):
                if q is not None:
                    self._contracts[sym] = q
        return {s: self._contracts[s] for s in symbols if s in self._contracts}

    # ───────────────────────── market data ─────────────────────────
    async def fetch_5min_bars(
        self,
        symbol: str,
        end_ny: datetime,
        duration_str: str = "1 D",
    ) -> List[Any]:
        contract = await self.qualify_forex(symbol)
        end_utc = end_ny.astimezone(timezone.utc)
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_utc,
            durationStr=duration_str,
            barSizeSetting="5 mins",
            whatToShow="MIDPOINT",
            useRTH=False,
            formatDate=2,
        )
        return list(bars)

    async def stream_5min_bars(
        self,
        symbol: str,
        on_update: Callable[[BarDataList, bool], None],
    ) -> BarDataList:
        """
        Subscribe to streaming 5-min bars. on_update(bars, hasNewBar) fires on each
        update; hasNewBar is True when a new candle has just closed.
        """
        contract = await self.qualify_forex(symbol)
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",                     # = now
            durationStr="2 D",
            barSizeSetting="5 mins",
            whatToShow="MIDPOINT",
            useRTH=False,
            formatDate=2,
            keepUpToDate=True,                  # streams updates
        )
        bars.updateEvent += on_update
        return bars

    def cancel_stream(self, bars: BarDataList) -> None:
        try:
            self.ib.cancelHistoricalData(bars)
        except Exception:
            logger.exception("cancelHistoricalData failed")

    # ───────────────────────── positions (for reconciliation) ─────────────────────────
    async def get_open_positions(self) -> List[Any]:
        """
        Return list of currently-open positions across all managed accounts.
        Each entry has: .account, .contract (with .symbol, .currency, .secType),
        .position (signed quantity), .avgCost.
        """
        positions = await self.ib.reqPositionsAsync()
        return list(positions)

    # ───────────────────────── orders (DRY-RUN GATED) ─────────────────────────
    async def place_market_order(
        self,
        account: str,
        symbol: str,
        side: str,            # "BUY" or "SELL"
        lot_size: float,
    ) -> Dict[str, Any]:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY|SELL, got {side!r}")

        lot_units = int(lot_size * 100_000)
        intent = {
            "account": account,
            "symbol": symbol,
            "side": side,
            "lot_size": lot_size,
            "lot_units": lot_units,
        }

        if not self.config.LIVE_TRADING:
            logger.info(f"[DRY-RUN] Would place: {intent}")
            return {"status": "dry_run", "intent": intent}

        # Live path — guarded by config. Should never reach here in dev.
        contract = await self.qualify_forex(symbol)
        order = MarketOrder(side, lot_units)
        order.account = account
        try:
            trade = self.ib.placeOrder(contract, order)
            logger.warning(f"[LIVE] placed: {intent}")
            return {"status": "submitted", "intent": intent, "trade": trade}
        except Exception as e:
            logger.exception(f"placeOrder failed: {intent}")
            return {"status": "error", "intent": intent, "error": str(e)}
