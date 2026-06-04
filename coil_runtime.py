"""
Live/shadow runtime for the coil/session strategy.

SHADOW by default (LIVE_TRADING=False): logs every order intent, places nothing.
LIVE behind --live --i-really-mean-it (read_only=False), per the existing gate.

Monitor on SPOT, execute on CFD (SPEC_COIL.md §6b). One pair, one position at a
time. Per session: wait first 5m candle -> select -> trade the bias/DMI state
machine -> protective stop + breakeven (spot tick watcher) -> stop-and-reverse
-> force-flat on the final candle. Daily-loss limit via account PnL.

THIS IS V1 - must be smoke-tested in shadow (connect, select, stream a bar,
emit order intents) before the live gate is ever flipped.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from coil_orders import CoilOrderManager
from coil_selection import CoilParams, compute_pair_coil, coil_select
from coil_strategy import CoilBase, entry_dir, reverse_signal
from config import StrategyConfig
from cpr import compute_cpr_from_bars
from ibkr_client import IBKRClient
from pivots import compute_pivots
from quality_supertrend import DMI
from selection import SelectionError
from sessions import IST, Session, SESSIONS, first_candle_close, session_window
from time_utils import NY, ny_now, prior_trading_fx_day_window, to_ny

logger = logging.getLogger(__name__)

WARMUP_DAYS = 4
_CONV = {"JPY": "USDJPY", "CHF": "USDCHF", "CAD": "USDCAD"}
TICK_POLL_S = 1.0          # breakeven watcher cadence while in a trade


def _bar_open_ny(bar) -> datetime:
    ts = bar.date
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    return to_ny(ts)


class CoilRuntime:
    def __init__(self, config: StrategyConfig, ibkr: IBKRClient,
                 params: Optional[CoilParams] = None,
                 sessions: Tuple[Session, ...] = SESSIONS,
                 risk_per_trade: float = 50.0, breakeven_trigger: float = 50.0,
                 daily_loss_limit: float = 150.0):
        self.config = config
        self.ibkr = ibkr
        self.params = params or CoilParams()
        self.sessions = sessions
        self.risk = risk_per_trade
        self.breakeven = breakeven_trigger
        self.daily_limit = daily_loss_limit
        self.om = CoilOrderManager(ibkr, config.cfd_account, breakeven_trigger)
        self._stop = asyncio.Event()
        self._done: set[Tuple[str, str]] = set()    # (session_name, date) handled
        self._halt_fx_day: Optional[str] = None      # FX-day key halted by daily limit
        self._pnl = None

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------- main loop -----------------------------
    async def run(self) -> None:
        await self.ibkr.connect()
        self._pnl = self.ibkr.subscribe_account_pnl(self.config.cfd_account)
        mode = "LIVE" if self.config.LIVE_TRADING else "SHADOW"
        logger.info(f"[coil] {mode} runtime up. units={self.config.cfd_units} "
                    f"risk=${self.risk} BE=${self.breakeven} dailyLimit=${self.daily_limit}")
        try:
            while not self._stop.is_set():
                now_ist = ny_now().astimezone(IST)
                active = self._active(now_ist)
                if active is None:
                    await self._sleep(30)
                    continue
                session, sdate = active
                key = (session.name, sdate.isoformat())
                if key in self._done or not session.pairs:
                    await self._sleep(30)
                    continue
                fcc = first_candle_close(session, sdate)
                if now_ist < fcc:
                    await self._sleep(min(30, (fcc - now_ist).total_seconds()))
                    continue
                self._done.add(key)
                try:
                    await self._trade_session(session, sdate)
                except Exception:
                    logger.exception(f"[coil] session {session.name} {sdate} crashed")
        finally:
            await self._teardown_open("shutdown")
            await self.ibkr.disconnect()

    def _active(self, now_ist: datetime):
        from sessions import active_session
        return active_session(now_ist, self.sessions)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, secs))
        except asyncio.TimeoutError:
            pass

    def _daily_limit_hit(self) -> bool:
        try:
            daily = float(getattr(self._pnl, "dailyPnL", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        return daily <= -self.daily_limit

    # --------------------------- one session ---------------------------
    async def _trade_session(self, session: Session, sdate) -> None:
        if self._halt_fx_day and self._fx_day_key() == self._halt_fx_day:
            logger.info(f"[coil] {session.name}: halted for the day (daily limit)")
            return
        winner = await self._select(session, sdate)
        if winner is None:
            return
        logger.info(f"[coil] {session.name} selected {winner}")

        start_ist, end_ist = session_window(session, sdate)
        end_ny = end_ist.astimezone(NY)
        anchor_ny = first_candle_close(session, sdate).astimezone(NY)
        noentry_ny = end_ny - timedelta(minutes=self.params.no_entry_last_minutes)

        # warm-up + pivots + sizing
        bars = await self.ibkr.fetch_5min_bars_range(
            winner, start_ny=anchor_ny - timedelta(days=WARMUP_DAYS), end_ny=anchor_ny)
        cpr = self._cpr(bars, anchor_ny)
        piv = compute_pivots(cpr.high, cpr.low, cpr.close)
        upu = await self._usd_per_price_unit(winner, anchor_ny)
        stop_dist = self.risk / upu

        dmi = DMI(self.params.di_len, self.params.adx_len)
        for b in bars:
            dmi.update(float(b.high), float(b.low), float(b.close))
        base = CoilBase()
        # seed base from the just-closed first candle
        fb = bars[-1]
        base.update(float(fb.high), float(fb.low), float(fb.close), piv)

        quote = await self.ibkr.subscribe_spot_quote(winner)
        watcher = asyncio.create_task(self._breakeven_watcher(quote))
        try:
            await self._session_bar_loop(winner, dmi, base, piv, stop_dist, upu,
                                         end_ny, noentry_ny, quote)
        finally:
            watcher.cancel()
            await self._teardown_open("session_end")
            await self.ibkr.cancel_spot_quote(winner)

    async def _session_bar_loop(self, symbol, dmi, base, piv, stop_dist, upu,
                                end_ny, noentry_ny, quote) -> None:
        evt = asyncio.Event()
        latest = {"bar": None}

        def on_update(bars, has_new):
            if has_new and len(bars) >= 2:
                latest["bar"] = bars[-2]      # the just-CLOSED bar
                evt.set()

        stream = await self.ibkr.stream_5min_bars(symbol, on_update)
        try:
            while not self._stop.is_set():
                now_ny = ny_now().astimezone(NY)
                if now_ny >= end_ny:
                    break
                try:
                    await asyncio.wait_for(evt.wait(), timeout=10)
                except asyncio.TimeoutError:
                    continue
                evt.clear()
                bar = latest["bar"]
                if bar is None:
                    continue
                await self._on_closed_bar(symbol, bar, dmi, base, piv, stop_dist, upu,
                                          end_ny, noentry_ny, quote)
                if self._daily_limit_hit():
                    logger.warning("[coil] DAILY LIMIT hit -> flatten + halt day")
                    await self._teardown_open("daily_limit")
                    self._halt_fx_day = self._fx_day_key()
                    break
        finally:
            self.ibkr.cancel_stream(stream)

    async def _on_closed_bar(self, symbol, bar, dmi, base, piv, stop_dist, upu,
                             end_ny, noentry_ny, quote) -> None:
        h, l, c = float(bar.high), float(bar.low), float(bar.close)
        plus, minus, _ = dmi.update(h, l, c)
        base.update(h, l, c, piv)
        if plus is None:
            return
        open_ny = _bar_open_ny(bar)
        is_end = open_ny.astimezone(NY) >= end_ny - timedelta(minutes=5)
        no_new = is_end or open_ny >= noentry_ny
        bias = base.bias(c)

        # exit / reverse
        if self.om.is_open:
            pos = self.om.trade.side
            if reverse_signal(pos, bias, plus, minus):
                await self.om.close("opp_signal")
                if not no_new:
                    await self.om.open(symbol, -pos, self.config.cfd_units, c, stop_dist, upu)
        # entry when flat
        if not self.om.is_open and not no_new:
            d = entry_dir(bias, plus, minus)
            if d:
                await self.om.open(symbol, d, self.config.cfd_units, c, stop_dist, upu)
        # session-end force-flat
        if is_end and self.om.is_open:
            await self.om.close("session_end")

    async def _breakeven_watcher(self, quote) -> None:
        """Poll the spot quote ~1/s; arm breakeven the instant PnL touches it."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(TICK_POLL_S)
                if not self.om.is_open:
                    continue
                mid = quote.midpoint()
                if mid is None or mid != mid:      # NaN guard
                    continue
                self.om.arm_breakeven_if_touched(mid)
        except asyncio.CancelledError:
            pass

    # ----------------------------- helpers -----------------------------
    async def _select(self, session: Session, sdate) -> Optional[str]:
        anchor_ny = first_candle_close(session, sdate).astimezone(NY)
        start = anchor_ny - timedelta(days=self.params.coil_window_bars / 288.0 + 3)
        metrics = []
        for sym in session.pairs:
            try:
                bars = await self.ibkr.fetch_5min_bars_range(sym, start_ny=start, end_ny=anchor_ny)
                bars = [b for b in bars if _bar_open_ny(b) < anchor_ny]
                if not bars:
                    continue
                cpr = self._cpr(bars, anchor_ny)
                metrics.append(compute_pair_coil(
                    sym, bars, cpr, di_len=self.params.di_len, adx_len=self.params.adx_len,
                    adx_coil_max=self.params.adx_coil_max,
                    coil_window_bars=self.params.coil_window_bars))
            except Exception as e:
                logger.warning(f"[coil] {sym} skipped in selection: {e}")
        try:
            return coil_select(metrics, adx_filter_max=self.params.adx_filter_max).symbol
        except SelectionError as e:
            logger.info(f"[coil] {session.name}: no selection ({e})")
            return None

    def _cpr(self, bars, anchor_ny):
        ws, we = prior_trading_fx_day_window(anchor_ny)
        H, L, C = [], [], []
        for b in bars:
            t = _bar_open_ny(b)
            if ws <= t < we:
                H.append(b.high); L.append(b.low); C.append(b.close)
        if not H:
            raise SelectionError("no prior-FX-day bars for CPR")
        return compute_cpr_from_bars(H, L, C)

    async def _usd_per_price_unit(self, symbol, at_ny) -> float:
        quote = symbol[3:].upper()
        units = self.config.cfd_units
        if quote == "USD":
            return float(units)
        conv = _CONV.get(quote)
        if conv is None:
            raise SelectionError(f"no USD conversion for {quote}")
        b = await self.ibkr.fetch_5min_bars(conv, end_ny=at_ny, duration_str="1 D")
        return float(units) / float(b[-1].close)

    def _fx_day_key(self) -> str:
        from time_utils import current_fx_day_anchor
        return current_fx_day_anchor()[0].isoformat()

    async def _teardown_open(self, reason: str) -> None:
        if self.om.is_open:
            try:
                await self.om.close(reason)
            except Exception:
                logger.exception(f"[coil] teardown close failed ({reason})")
