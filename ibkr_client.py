"""
ib_async wrapper. The single point of contact with IBKR.

Capabilities:
  - connect / disconnect lifecycle
  - list managed FA sub-accounts
  - fetch account balances (NetLiquidation, USD-preferred, BASE fallback)
  - qualify forex contracts on IDEALPRO (cached)
  - fetch historical 5-min bars for a window
  - subscribe to streaming 5-min bars (keepUpToDate=True)
  - place a market order - GATED by LIVE_TRADING; in dry-run, logs intent only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ib_async import IB, BarDataList, Contract, Forex, MarketOrder, OrderStatus   # type: ignore[import-untyped]  # noqa: F401

from config import StrategyConfig

logger = logging.getLogger(__name__)

# Max time we wait for an order to reach a terminal status (Filled / Cancelled / Inactive).
# Market orders on IDEALPRO normally fill in <2s; 15s gives generous headroom.
ORDER_FILL_TIMEOUT_S: float = 15.0
# Polling interval while waiting.
_FILL_POLL_INTERVAL_S: float = 0.2
# Trade-log error code we ignore (odd-lot routing notice - informational).
_HARMLESS_LOG_CODES = {399}


class IBKRClient:
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.ib = IB()
        self._contracts: Dict[str, Contract] = {}

    # ------------------------- lifecycle -------------------------
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

    # ------------------------- accounts -------------------------
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

    # ------------------------- contracts -------------------------
    async def qualify_cfd(self, symbol: str) -> Contract:
        """
        Qualify the FX CFD contract for `symbol` (e.g. 'EURUSD').
        secType='CFD', exchange='SMART'. Cached.

        Use this for CFD trading (only U25265693 has CFD permission per the
        whatIf diagnostics). The Forex IDEALPRO contract from qualify_forex
        cannot be used for CFD orders.
        """
        cache_key = f"CFD:{symbol}"
        if cache_key in self._contracts:
            return self._contracts[cache_key]
        base, quote = symbol[:3].upper(), symbol[3:].upper()
        c = Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
        result = await self.ib.qualifyContractsAsync(c)
        if not result or result[0] is None:
            raise RuntimeError(f"Could not qualify CFD contract: {symbol}")
        self._contracts[cache_key] = result[0]
        return result[0]

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

    # ------------------------- market data -------------------------
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

    async def determine_effective_close_time(
        self,
        symbol: str,
        sample_days: int = 10,
    ):
        """
        For one symbol, find the mode of the LAST 5-min bar's open-time per
        FX day across `sample_days` of recent history. That open time equals
        the close of the second-to-last bar - i.e. the moment we want to
        force-exit on (5 minutes before the asset's actual close).

        Returns:
          time(16, 55) NY for normal 24/5 forex pairs (last bar opens 16:55
          -> closes 17:00; force-exit on close of 16:50 -> 16:55 bar = 16:55).

        Returns None if no bars are available.
        """
        from collections import Counter
        from time_utils import current_fx_day_anchor, to_ny

        # Use the existing fetch (returns 5-min bars ending now).
        bars = await self.fetch_5min_bars(
            symbol,
            end_ny=__import__("time_utils").ny_now(),
            duration_str=f"{sample_days} D",
        )
        if not bars:
            return None

        # Bucket by FX day, find last bar's open time per day.
        per_day_last_open: Dict[Any, Any] = {}
        for b in bars:
            ts = b.date
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_ny = to_ny(ts)
            fx_start, _ = current_fx_day_anchor(ts_ny)
            cur = per_day_last_open.get(fx_start)
            if cur is None or ts_ny > cur:
                per_day_last_open[fx_start] = ts_ny

        if not per_day_last_open:
            return None

        # Take the mode of the time-of-day across days, ignoring partial days
        # (today's in-progress FX day will have a low last-open).
        # Drop the most recent FX day if it's the current one.
        keys_sorted = sorted(per_day_last_open.keys())
        today_start, _ = current_fx_day_anchor()
        if keys_sorted and keys_sorted[-1] == today_start:
            keys_sorted = keys_sorted[:-1]

        times = [per_day_last_open[k].time() for k in keys_sorted]
        if not times:
            return None
        counter = Counter(times)
        return counter.most_common(1)[0][0]

    async def fetch_5min_bars_range(
        self,
        symbol: str,
        start_ny: datetime,
        end_ny: datetime,
        chunk_days: int = 25,
        pace_sleep_s: float = 0.5,
        max_retries: int = 3,
        retry_backoff_s: float = 2.0,
    ) -> List[Any]:
        """
        Fetch all 5-min bars across [start_ny, end_ny] by pagination.

        IBKR caps a single 5-min request at ~30 days of bars. We slide a
        `chunk_days` window backwards from `end_ny` until we cover the start,
        then concatenate + dedupe by timestamp.

        Throttling defence: if a chunk returns empty unexpectedly (usually an
        IBKR pacing violation / error 162), retry with exponential backoff
        up to `max_retries` times before giving up.
        """
        if end_ny <= start_ny:
            raise ValueError(f"end_ny ({end_ny}) must be > start_ny ({start_ny})")
        cursor = end_ny
        all_bars: list = []
        seen_keys: set = set()
        import asyncio
        while cursor > start_ny:
            window_start = max(start_ny, cursor - timedelta(days=chunk_days))
            duration_days = max(1, (cursor - window_start).days + 1)
            duration_str = f"{duration_days} D"
            logger.info(
                f"fetch_5min_bars_range({symbol}): chunk end={cursor.isoformat()} "
                f"duration={duration_str}"
            )
            chunk = []
            for attempt in range(max_retries + 1):
                chunk = await self.fetch_5min_bars(symbol, end_ny=cursor, duration_str=duration_str)
                if chunk:
                    break
                if attempt < max_retries:
                    backoff = retry_backoff_s * (2 ** attempt)
                    logger.warning(
                        f"fetch_5min_bars_range({symbol}): chunk returned 0 bars "
                        f"(likely IBKR error 162 / pacing); retrying in {backoff:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        f"fetch_5min_bars_range({symbol}): chunk end={cursor.isoformat()} "
                        f"failed after {max_retries} retries - proceeding with what we have"
                    )
            for b in chunk:
                ts = b.date
                key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_bars.append(b)
            cursor = window_start
            if cursor > start_ny and pace_sleep_s > 0:
                await asyncio.sleep(pace_sleep_s)
        # Sort by timestamp ascending (chunks come in reverse-chronological).
        all_bars.sort(key=lambda b: b.date)
        return all_bars

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

    # ------------------------- positions (for reconciliation) -------------------------
    async def get_open_positions(self) -> List[Any]:
        """
        Return list of currently-open positions across all managed accounts.
        Each entry has: .account, .contract (with .symbol, .currency, .secType),
        .position (signed quantity), .avgCost.

        WARNING: IDEALPRO has a "virtual FX position" threshold (~20k base ccy).
        Cash forex positions BELOW that threshold do NOT appear here - they
        settle into the multi-currency cash ledger. Use fetch_cash_ledger()
        to detect those.
        """
        positions = await self.ib.reqPositionsAsync()
        return list(positions)

    async def fetch_cash_ledger(self, account: str) -> Dict[str, float]:
        """
        Per-currency cash balances for one account, used by the reconciler to
        detect sub-threshold FX positions that don't surface in
        reqPositionsAsync (IDEALPRO virtual FX position rule, ~20k base ccy).

        Returns: {currency: signed_balance} excluding the synthetic 'BASE' row.
        Account base currency (typically USD on these FA subs) is included -
        callers usually skip it because it's indistinguishable from natural
        cash, but it's available if needed.
        """
        try:
            await self.ib.accountSummaryAsync(account=account)
        except Exception:
            logger.exception(f"accountSummaryAsync({account}) failed during ledger fetch")
            return {}
        out: Dict[str, float] = {}
        for v in self.ib.accountValues(account=account):
            if v.tag != "CashBalance":
                continue
            if v.currency == "BASE":
                continue
            try:
                out[v.currency] = float(v.value)
            except (ValueError, TypeError):
                continue
        return out

    # ------------------------- orders (DRY-RUN GATED) -------------------------
    async def place_market_order(
        self,
        account: str,
        symbol: str,
        side: str,            # "BUY" or "SELL"
        lot_size: float,
        timeout_s: float = ORDER_FILL_TIMEOUT_S,
    ) -> Dict[str, Any]:
        """
        Submit a market order and WAIT for terminal resolution before returning.

        Return dict shape:
          {
            "status":      "filled" | "rejected" | "timeout" | "dry_run",
            "intent":      {account, symbol, side, lot_size, lot_units},
            "fill_price":  float | None,   # avg fill price (filled only)
            "fill_qty":    int   | None,   # filled qty   (filled only)
            "error":       str   | None,   # rejection / timeout reason
            "trade":       Trade | None,   # ib_async Trade obj (live only)
          }

        Caller MUST branch on `status`:
          - "filled" / "dry_run"  -> state may be updated
          - "rejected" / "timeout"-> state must NOT be updated; account should halt
        """
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
        empty = {"fill_price": None, "fill_qty": None, "error": None, "trade": None}

        if not self.config.LIVE_TRADING:
            logger.info(f"[DRY-RUN] Would place: {intent}")
            return {"status": "dry_run", "intent": intent, **empty}

        # Live path - guarded by config. Should never reach here in dev.
        contract = await self.qualify_forex(symbol)
        order = MarketOrder(side, lot_units)
        order.account = account
        try:
            trade = self.ib.placeOrder(contract, order)
            logger.info(f"[LIVE] submitted: {intent}")
        except Exception as e:
            logger.exception(f"placeOrder synchronous failure: {intent}")
            return {"status": "rejected", "intent": intent,
                    "fill_price": None, "fill_qty": None,
                    "error": f"placeOrder raised: {e}", "trade": None}

        final_status = await self._wait_for_terminal_status(trade, timeout_s)

        if final_status == "Filled":
            avg_px = getattr(trade.orderStatus, "avgFillPrice", None) or None
            filled = getattr(trade.orderStatus, "filled", 0) or 0
            try:
                fill_price = float(avg_px) if avg_px else None
            except (TypeError, ValueError):
                fill_price = None
            try:
                fill_qty = int(filled)
            except (TypeError, ValueError):
                fill_qty = 0
            logger.info(
                f"[LIVE] FILLED: {intent} avgPrice={fill_price} qty={fill_qty}"
            )
            return {"status": "filled", "intent": intent,
                    "fill_price": fill_price, "fill_qty": fill_qty,
                    "error": None, "trade": trade}

        # Anything else = failure. Extract reason for the log/caller.
        reason = self._extract_rejection_reason(trade)
        if final_status == "Timeout":
            last = getattr(trade.orderStatus, "status", "?")
            err = f"timeout after {timeout_s}s, last status={last}; reason={reason}"
            logger.error(f"[LIVE] TIMEOUT: {intent} ({err})")
            return {"status": "timeout", "intent": intent,
                    "fill_price": None, "fill_qty": None,
                    "error": err, "trade": trade}

        err = f"final_status={final_status}; reason={reason}"
        logger.error(f"[LIVE] REJECTED: {intent} ({err})")
        return {"status": "rejected", "intent": intent,
                "fill_price": None, "fill_qty": None,
                "error": err, "trade": trade}

    async def _wait_for_terminal_status(self, trade, timeout_s: float) -> str:
        """
        Poll trade.orderStatus.status until it reaches OrderStatus.DoneStates,
        or we hit the timeout. Returns the terminal status string, or "Timeout".
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            status = getattr(trade.orderStatus, "status", None)
            if status in OrderStatus.DoneStates:
                return status
            await asyncio.sleep(_FILL_POLL_INTERVAL_S)
        return "Timeout"

    @staticmethod
    def _extract_rejection_reason(trade) -> str:
        """Pull a human-readable rejection reason from trade.log entries."""
        for entry in reversed(getattr(trade, "log", []) or []):
            msg = (getattr(entry, "message", "") or "").strip()
            code = getattr(entry, "errorCode", 0) or 0
            if code in _HARMLESS_LOG_CODES:
                continue
            if code or msg:
                return f"code={code} msg={msg[:240]}"
        return "no error message in trade.log"
