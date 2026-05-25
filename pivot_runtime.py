"""
Runtime orchestrator for the pivot/SR + quality-SuperTrend strategy.

Standalone twin of `strategy.py` (the old CPR strategy's orchestrator), kept
separate so the old strategy is never touched. It reuses the same broker
plumbing (selection, warmup, streaming, reconciliation, live-order gate) but:

  - feeds `pivots.Pivots` (9 SR anchors) to the strategy instead of TC/BC/PP;
  - drives `PivotSuperTrendStrategy` (no regime, no Adaptive SuperTrend);
  - logs to `PivotShadowLog`.

Asset selection, the FX-day window, the 16:55 force-exit and reconnection
backoff behave exactly as in the old orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, Optional

from balance_store import BalanceStore
from config import StrategyConfig, TRADING_ZONE_POLL_SECONDS
from cpr import compute_cpr_from_bars
from ibkr_client import IBKRClient
from pivot_shadow_log import PivotShadowLog
from pivot_st_strategy import PivotSuperTrendStrategy
from pivots import Pivots, compute_pivots
from quality_supertrend import QSTConfig
from selection import SelectionError, narrowest_pair
from state_store import PersistedState, StateStore, StateStoreError
from time_utils import (
    current_fx_day_anchor,
    is_in_trading_zone,
    ny_now,
    prior_trading_fx_day_window,
    to_ny,
)

logger = logging.getLogger(__name__)


class ConnectionLostError(RuntimeError):
    """Raised when the IBKR socket drops mid-session."""


class StateMismatchError(RuntimeError):
    """Persisted state doesn't match IBKR positions and force_clean_restart
    wasn't set. Bot refuses to start to avoid orphaning/double-trading."""


RECONNECT_BACKOFF_INITIAL_S = 5.0
RECONNECT_BACKOFF_MAX_S = 60.0

# After a 5-min bar closes on the stream, wait this long then re-fetch it from
# historical data so the decision uses the SETTLED OHLC rather than the
# provisional real-time bar (whose intrabar high/low can differ in thin
# liquidity and flip threshold-sensitive filters like ADX at its border).
SETTLE_REFETCH_DELAY_S = 3.0


def _bar_ts_ny(bar) -> datetime:
    ts = bar.date
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    return to_ny(ts)


class PivotRuntime:
    """Integrated live/shadow runtime for the pivot strategy."""

    def __init__(
        self,
        config: StrategyConfig,
        ibkr: IBKRClient,
        balances: BalanceStore,
        shadow_dir: Path = Path("backtest_output/pivot_shadow"),
        state_store: Optional[StateStore] = None,
        force_clean_restart: bool = False,
        qst_cfg: Optional[QSTConfig] = None,
    ):
        self.config = config
        self.ibkr = ibkr
        self.balances = balances
        self.shadow_dir = shadow_dir
        self.state_store = state_store
        self.force_clean_restart = force_clean_restart
        self.qst_cfg = qst_cfg or QSTConfig()

        self.current_pair: Optional[str] = None
        self.current_strategy: Optional[PivotSuperTrendStrategy] = None
        self.current_bars_handle = None
        self.current_force_exit_time: Optional[time] = None
        self.fx_day_hlc: Dict[datetime, tuple[float, float, float]] = {}
        self.last_processed_open_ny: Optional[datetime] = None
        self.current_fx_day_start: Optional[datetime] = None

        self.active_accounts: Dict[str, float] = {}
        self.shadow_log: Optional[PivotShadowLog] = None
        self._stop = asyncio.Event()
        self._bar_evt = asyncio.Event()

        # --- data-health gating ---
        # Names of IBKR market/historical data farms currently reported broken.
        # While non-empty (or connectivity is lost), bar decisions are paused;
        # on recovery the strategy is re-warmed so corrupted bars never seed the
        # recursive indicators (ADX/EMA/SuperTrend).
        self._broken_farms: set[str] = set()
        self._rewarm_needed: bool = False
        self._error_handler_attached: bool = False

    # ------------------- entry point -------------------
    async def run(self) -> None:
        backoff_s = RECONNECT_BACKOFF_INITIAL_S
        try:
            while not self._stop.is_set():
                try:
                    if not self.ibkr.is_connected:
                        logger.info("Connecting to IBKR...")
                        await self.ibkr.connect()
                        self._attach_error_handler()
                        backoff_s = RECONNECT_BACKOFF_INITIAL_S

                    while (not is_in_trading_zone(ny_now())
                            and not self._stop.is_set()):
                        if not self.ibkr.is_connected:
                            raise ConnectionLostError("disconnected while waiting for zone")
                        logger.info(f"Outside trading zone; sleeping {TRADING_ZONE_POLL_SECONDS}s...")
                        await self._interruptible_sleep(TRADING_ZONE_POLL_SECONDS)
                    if self._stop.is_set():
                        break

                    await self._run_session()
                except StateMismatchError:
                    raise
                except ConnectionLostError as e:
                    logger.warning(f"Connection lost ({e}); reconnecting in {backoff_s:.0f}s")
                    await self._teardown_pair()
                    await self._safe_disconnect()
                    await self._interruptible_sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, RECONNECT_BACKOFF_MAX_S)
                except Exception:
                    logger.exception(f"Unexpected session error; reconnecting in {backoff_s:.0f}s")
                    await self._teardown_pair()
                    await self._safe_disconnect()
                    await self._interruptible_sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, RECONNECT_BACKOFF_MAX_S)
        finally:
            await self._teardown_pair()
            await self._safe_disconnect()
            if self.shadow_log is not None:
                self.shadow_log.close()

    def stop(self) -> None:
        self._stop.set()
        self._bar_evt.set()

    async def _safe_disconnect(self) -> None:
        try:
            await self.ibkr.disconnect()
        except Exception:
            logger.exception("disconnect failed (continuing)")

    async def _interruptible_sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    # ------------------- session -------------------
    async def _run_session(self) -> None:
        await self._bootstrap_accounts()
        await self._verify_cfd_account_present()

        if self.shadow_log is None:
            self.shadow_log = PivotShadowLog(self.shadow_dir, ny_now())

        await self._select_and_install_strategy()

        last_seen_fx_day = self.current_fx_day_start
        while is_in_trading_zone(ny_now()) and not self._stop.is_set():
            if not self.ibkr.is_connected:
                raise ConnectionLostError("ib socket dropped during session")
            try:
                await asyncio.wait_for(self._bar_evt.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
            self._bar_evt.clear()
            # Re-warm if a data-farm outage happened and data is now healthy.
            await self._maybe_rewarm()
            cur_fx_day, _ = current_fx_day_anchor(ny_now())
            if last_seen_fx_day is not None and cur_fx_day != last_seen_fx_day:
                logger.info(f"FX-day rollover detected -> {cur_fx_day.isoformat()}")
                last_seen_fx_day = cur_fx_day
                await self._select_and_install_strategy()
            else:
                last_seen_fx_day = cur_fx_day

        await self._teardown_pair()
        logger.info("Exited trading zone - dropping back to gate")

    # ------------------- bootstrap -------------------
    async def _bootstrap_accounts(self) -> None:
        if self.balances.has_file():
            self.balances.load()
            logger.info(f"Loaded frozen balances from {self.balances.path}")
        else:
            logger.info("No balance file - fetching from IBKR (one-time snapshot)")
            fresh = await self.ibkr.fetch_account_balances_usd()
            self.balances.init_from(fresh)
            logger.info(f"Wrote frozen balances -> {self.balances.path}: {fresh}")
        active = self.balances.active_accounts()
        if not active:
            logger.warning("No active sub-accounts (none >= $1000)")
        else:
            logger.info(f"Active accounts ({len(active)}): "
                        + ", ".join(f"{a}=${b:,.2f}" for a, b in active.items()))
        self.active_accounts = active

    async def _verify_cfd_account_present(self) -> None:
        managed = self.ibkr.managed_accounts()
        if self.config.cfd_account not in managed:
            logger.warning(
                f"cfd_account={self.config.cfd_account} NOT in managed accounts "
                f"{managed} - shadow logging still works, but LIVE orders would fail."
            )
        else:
            logger.info(f"CFD trading account: {self.config.cfd_account}  "
                        f"(LIVE_TRADING={self.config.LIVE_TRADING})")

    # ------------------- selection + install -------------------
    async def _select_and_install_strategy(self) -> None:
        sel = await self._compute_daily_selection()
        if sel is None:
            return
        winner, _ = sel
        if winner == self.current_pair:
            logger.info(f"Selected pair unchanged: {winner}  (no rebuild needed)")
            return
        if self.current_pair is not None:
            logger.info(f"Pair change {self.current_pair} -> {winner}; tearing down")
            await self._teardown_pair()
        await self._install_strategy_for_pair(winner)

    async def _compute_daily_selection(self):
        now = ny_now()
        ws, we = prior_trading_fx_day_window(now)
        logger.info(f"Computing daily CPR for {len(self.config.symbols_list)} symbols, "
                    f"prior trading FX day {ws.isoformat()} -> {we.isoformat()}")
        cprs = {}
        for sym in self.config.symbols_list:
            try:
                bars = await self.ibkr.fetch_5min_bars(sym, end_ny=we, duration_str="2 D")
            except Exception:
                logger.exception(f"{sym}: fetch failed; skipping")
                continue
            in_window = self._filter_window(bars, ws, we)
            if not in_window:
                logger.warning(f"{sym}: no bars in prior-FX-day window - skipping")
                continue
            try:
                cprs[sym] = compute_cpr_from_bars(
                    [b.high for b in in_window],
                    [b.low for b in in_window],
                    [b.close for b in in_window],
                )
            except Exception:
                logger.exception(f"{sym}: CPR compute failed; skipping")
        if not cprs:
            logger.error("No symbols produced a CPR - skipping selection")
            return None
        candidates = [s for s in self.config.symbols_list if s in cprs]
        try:
            winner = narrowest_pair(candidates, cprs)
        except SelectionError as e:
            logger.error(f"Selection failed: {e}")
            return None
        wcpr = cprs[winner]
        logger.info(f"SELECTED {winner}  width_pct={wcpr.width_pct:.4f}%  "
                    f"TC={wcpr.tc:.5f}  BC={wcpr.bc:.5f}")
        return winner, wcpr

    # ------------------- state persistence + reconciliation -------------------
    async def _reconcile_and_restore(self) -> None:
        """Compare persisted state + IBKR positions; restore or halt.

        IBKR's real position is the only source of truth - the warmup replay's
        residual virtual position is always overwritten.
        """
        if self.state_store is None or self.current_strategy is None:
            return

        if self.force_clean_restart:
            if self.state_store.exists():
                logger.warning(f"--force-clean-restart: deleting state file {self.state_store.path}")
                self.state_store.delete()
            s = self.current_strategy
            s.position = 0
            s.entry_price = None
            s.entry_timestamp = None
            logger.warning("--force-clean-restart: strategy reset to flat. "
                           "Ensure IBKR positions are flat before this fires!")
            return

        try:
            all_positions = await self.ibkr.ib.reqPositionsAsync()
        except Exception:
            logger.exception("Failed to fetch IBKR positions during reconciliation")
            all_positions = []
        cfd_positions = []
        for p in all_positions:
            try:
                if p.contract.secType != "CFD":
                    continue
                if p.account != self.config.cfd_account:
                    continue
                if abs(p.position) < 1e-9:
                    continue
                cfd_positions.append(p)
            except Exception:
                logger.exception("Skipping malformed IBKR position row")

        for p in cfd_positions:
            pair = (p.contract.symbol or "") + (p.contract.currency or "")
            if pair != self.current_pair:
                raise StateMismatchError(
                    f"Stranded IBKR CFD position on {pair} (qty={p.position:+g}) in "
                    f"{self.config.cfd_account}, but today's selection is "
                    f"{self.current_pair}. Close manually in TWS, then re-run with "
                    f"--force-clean-restart."
                )

        cur_pair_pos = next(
            (p for p in cfd_positions
             if (p.contract.symbol + p.contract.currency) == self.current_pair),
            None,
        )
        ibkr_qty = float(cur_pair_pos.position) if cur_pair_pos else 0.0
        ibkr_position = 1 if ibkr_qty > 0 else -1 if ibkr_qty < 0 else 0

        persisted: Optional[PersistedState] = None
        if self.state_store.exists():
            try:
                persisted = self.state_store.load()
            except StateStoreError as e:
                raise StateMismatchError(
                    f"State file unreadable: {e}. Fix or delete it and re-run with "
                    f"--force-clean-restart."
                )
        persisted_pos = persisted.position if persisted else 0

        if persisted_pos != ibkr_position:
            raise StateMismatchError(
                f"State/IBKR mismatch: state file says position={persisted_pos} on "
                f"{persisted.selected_pair if persisted else '(none)'} but IBKR has "
                f"position={ibkr_position} (qty={ibkr_qty:+g}) on {self.current_pair}. "
                f"Resolve manually, then re-run with --force-clean-restart."
            )

        s = self.current_strategy
        s.position = ibkr_position
        if ibkr_position == 0:
            s.entry_price = None
            s.entry_timestamp = None
            logger.info("Reconciliation OK: both state and IBKR are flat. "
                        "Resetting any warmup-residual virtual position to 0.")
            return

        s.entry_price = persisted.entry_price if persisted else None
        if persisted and persisted.entry_timestamp:
            try:
                s.entry_timestamp = datetime.fromisoformat(persisted.entry_timestamp)
            except ValueError:
                logger.warning(f"Could not parse persisted entry_timestamp "
                               f"{persisted.entry_timestamp!r}; leaving None")
        # Set prev ST direction to match the position so the NEXT opposite flip exits.
        s._prev_st_dir = ibkr_position
        logger.info(f"[RESTORE] {self.current_pair} position={ibkr_position} "
                    f"entry={s.entry_price} restored from state file + IBKR")

    def _save_state(self) -> None:
        if self.state_store is None or self.current_strategy is None:
            return
        s = self.current_strategy
        state = PersistedState(
            cfd_account=self.config.cfd_account,
            selected_pair=self.current_pair,
            position=s.position,
            entry_price=s.entry_price,
            entry_timestamp=(s.entry_timestamp.isoformat() if s.entry_timestamp else None),
            last_processed_open_ny=(
                self.last_processed_open_ny.isoformat()
                if self.last_processed_open_ny else None),
        )
        try:
            self.state_store.save(state)
        except Exception:
            logger.exception("State save failed (continuing - strategy unaffected)")

    async def _install_strategy_for_pair(self, symbol: str) -> None:
        logger.info(f"[install] Determining force-exit close time for {symbol}...")
        close_t = await self.ibkr.determine_effective_close_time(symbol, sample_days=10)
        if close_t is None:
            close_t = time(16, 55)
        logger.info(f"[install] force-exit close time: {close_t.strftime('%H:%M')} NY")

        now = ny_now()
        warmup_start = now - timedelta(days=self.config.warmup_days)
        logger.info(f"[install] Fetching {self.config.warmup_days}D warmup bars for {symbol}...")
        bars = await self.ibkr.fetch_5min_bars_range(symbol, start_ny=warmup_start, end_ny=now)
        logger.info(f"[install] Got {len(bars)} bars; computing per-FX-day HLC cache")

        self.fx_day_hlc.clear()
        for b in bars:
            ts_ny = _bar_ts_ny(b)
            fxs, _ = current_fx_day_anchor(ts_ny)
            cur = self.fx_day_hlc.get(fxs)
            if cur is None:
                self.fx_day_hlc[fxs] = (float(b.high), float(b.low), float(b.close))
            else:
                H, L, _ = cur
                self.fx_day_hlc[fxs] = (max(H, float(b.high)),
                                        min(L, float(b.low)),
                                        float(b.close))
        logger.info(f"[install] HLC cache covers {len(self.fx_day_hlc)} FX days")

        strategy = PivotSuperTrendStrategy(
            qst_cfg=self.qst_cfg,
            force_exit_close_time=close_t,
        )

        bars_sorted = sorted(bars, key=lambda b: _bar_ts_ny(b))
        replayed = 0
        for b in bars_sorted:
            ts_ny = _bar_ts_ny(b)
            piv = self._pivots_for_ts(ts_ny)
            if piv is None:
                continue
            fxs, _ = current_fx_day_anchor(ts_ny)
            strategy.update(
                timestamp=ts_ny,
                open_=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close),
                pivots=piv, fx_day_start=fxs,
            )
            replayed += 1
            self.last_processed_open_ny = ts_ny
            self.current_fx_day_start = fxs
        logger.info(f"[install] Replayed {replayed} warmup bars; strategy is warm")

        self.current_pair = symbol
        self.current_strategy = strategy
        self.current_force_exit_time = close_t

        await self._reconcile_and_restore()
        self._save_state()

        bars_handle = await self.ibkr.stream_5min_bars(
            symbol, on_update=self._make_bar_handler(symbol),
        )
        self.current_bars_handle = bars_handle
        logger.info(f"[install] Streaming live 5-min bars for {symbol}")

    async def _teardown_pair(self) -> None:
        if self.current_bars_handle is not None:
            try:
                self.ibkr.cancel_stream(self.current_bars_handle)
            except Exception:
                logger.exception("cancel_stream raised")
            self.current_bars_handle = None
        self.current_pair = None
        self.current_strategy = None
        self.current_force_exit_time = None
        self.fx_day_hlc.clear()
        self.last_processed_open_ny = None
        self.current_fx_day_start = None

    # ------------------- per-bar processing -------------------
    def _pivots_for_ts(self, ts_ny: datetime) -> Optional[Pivots]:
        """9 SR levels for the FX day containing ts_ny, from the PRIOR trading
        FX day's HLC. None if that HLC isn't cached yet."""
        fxs, _ = current_fx_day_anchor(ts_ny)
        sample = fxs + timedelta(hours=1)
        prior_start, _ = prior_trading_fx_day_window(sample)
        hlc = self.fx_day_hlc.get(prior_start)
        if hlc is None:
            return None
        H, L, C = hlc
        try:
            return compute_pivots(H, L, C)
        except Exception:
            return None

    def _make_bar_handler(self, symbol: str):
        def handler(bars, hasNewBar):
            self._bar_evt.set()
            if not hasNewBar or len(bars) < 2:
                return
            closed = bars[-2]
            asyncio.create_task(self._on_closed_bar(symbol, closed))
        return handler

    # ------------------- data-health gating -------------------
    # IBKR error/status codes (delivered via errorEvent) we react to.
    _FARM_BROKEN_CODES = {2103, 2105}        # market-data / historical-data farm broken
    _FARM_OK_CODES = {2104, 2106}            # corresponding farm restored
    _CONN_LOST_CODES = {1100}                # full TWS<->IBKR connectivity lost
    _CONN_RESTORED_LOST_DATA = {1101}        # restored, data lost -> must re-warm
    _CONN_RESTORED_OK = {1102}               # restored, data maintained

    _CONN_SENTINEL = "__connectivity__"

    def _attach_error_handler(self) -> None:
        if self._error_handler_attached:
            return
        try:
            self.ibkr.ib.errorEvent += self._on_ib_error
            self._error_handler_attached = True
        except Exception:
            logger.exception("Failed to attach errorEvent handler (data-health gating disabled)")

    @staticmethod
    def _farm_name(msg: str) -> str:
        # Messages look like "...connection is broken:usfarm" / "...is OK:cashhmds".
        return msg.rsplit(":", 1)[-1].strip() if ":" in msg else msg.strip()

    def _on_ib_error(self, reqId=None, errorCode=None, errorString="", contract=None, *extra) -> None:
        try:
            errorString = errorString or ""
            was_healthy = self.data_healthy
            if errorCode in self._FARM_BROKEN_CODES:
                self._broken_farms.add(self._farm_name(errorString))
            elif errorCode in self._FARM_OK_CODES:
                self._broken_farms.discard(self._farm_name(errorString))
            elif errorCode in self._CONN_LOST_CODES:
                self._broken_farms.add(self._CONN_SENTINEL)
            elif errorCode in self._CONN_RESTORED_LOST_DATA:
                self._broken_farms.discard(self._CONN_SENTINEL)
                self._rewarm_needed = True        # data was lost; rebuild for sure
            elif errorCode in self._CONN_RESTORED_OK:
                self._broken_farms.discard(self._CONN_SENTINEL)
            else:
                return  # not a health-relevant code

            if was_healthy and not self.data_healthy:
                logger.warning(f"[DATA] unhealthy - pausing decisions (broken: {self._broken_farms})")
            elif not was_healthy and self.data_healthy:
                # Recovered from an outage during the session -> re-warm on clean data.
                self._rewarm_needed = True
                self._bar_evt.set()               # wake the session loop promptly
                logger.warning("[DATA] restored - will re-warm strategy on clean data")
        except Exception:
            logger.exception("error in _on_ib_error handler")

    @property
    def data_healthy(self) -> bool:
        return not self._broken_farms

    async def _maybe_rewarm(self) -> None:
        """If a data outage occurred during the session, rebuild the strategy on
        fresh historical bars once data is healthy again."""
        if not self._rewarm_needed or not self.data_healthy:
            return
        self._rewarm_needed = False
        if self.current_pair is None:
            return
        logger.warning("[DATA] re-warming after data outage (rebuilding indicators on clean data)")
        await self._teardown_pair()       # clears current_pair -> forces a rebuild
        await self._select_and_install_strategy()

    async def _settled_bar(self, symbol: str, ts_ny: datetime):
        """Re-fetch the just-closed 5-min bar from historical data so the
        decision uses settled OHLC instead of the provisional streamed bar.
        Returns the matching settled bar, or None if it can't be found."""
        try:
            await asyncio.sleep(SETTLE_REFETCH_DELAY_S)
            bars = await self.ibkr.fetch_5min_bars(
                symbol, end_ny=ny_now(), duration_str="1 D")
        except Exception:
            logger.exception(f"[{symbol}] settled-bar re-fetch failed")
            return None
        for b in bars:
            if _bar_ts_ny(b) == ts_ny:
                return b
        return None

    async def _on_closed_bar(self, symbol: str, bar) -> None:
        try:
            await self._on_closed_bar_impl(symbol, bar)
        except Exception:
            logger.exception(f"[{symbol}] _on_closed_bar raised on bar {getattr(bar, 'date', '?')}")

    async def _on_closed_bar_impl(self, symbol: str, bar) -> None:
        if symbol != self.current_pair or self.current_strategy is None:
            return
        ts_ny = _bar_ts_ny(bar)
        if (self.last_processed_open_ny is not None
                and ts_ny <= self.last_processed_open_ny):
            return

        # Data-health gate: never decide/trade on a bar while an IBKR data farm
        # is broken - the OHLC may be incomplete/provisional. We skip without
        # advancing last_processed; the post-recovery re-warm rebuilds cleanly.
        if not self.data_healthy:
            logger.warning(
                f"[{symbol}] {ts_ny.isoformat()} data unhealthy "
                f"(broken: {self._broken_farms}); skipping bar - no decision/order"
            )
            return

        # Decide on the SETTLED bar, not the provisional streamed one.
        settled = await self._settled_bar(symbol, ts_ny)
        if settled is not None:
            if (abs(float(settled.high) - float(bar.high)) > 1e-9
                    or abs(float(settled.low) - float(bar.low)) > 1e-9
                    or abs(float(settled.close) - float(bar.close)) > 1e-9):
                logger.info(
                    f"[{symbol}] {ts_ny.isoformat()} settled bar differs from stream: "
                    f"H/L/C stream={float(bar.high):.5f}/{float(bar.low):.5f}/{float(bar.close):.5f} "
                    f"-> settled={float(settled.high):.5f}/{float(settled.low):.5f}/{float(settled.close):.5f} "
                    f"(deciding on settled)"
                )
            bar = settled
        else:
            logger.warning(
                f"[{symbol}] {ts_ny.isoformat()} settled bar unavailable - "
                f"deciding on streamed bar (may be provisional)"
            )

        fxs, _ = current_fx_day_anchor(ts_ny)
        cur = self.fx_day_hlc.get(fxs)
        if cur is None:
            self.fx_day_hlc[fxs] = (float(bar.high), float(bar.low), float(bar.close))
        else:
            H, L, _ = cur
            self.fx_day_hlc[fxs] = (max(H, float(bar.high)),
                                    min(L, float(bar.low)),
                                    float(bar.close))

        piv = self._pivots_for_ts(ts_ny)
        if piv is None:
            logger.warning(f"[{symbol}] {ts_ny.isoformat()} no pivots available - skipping bar")
            self.last_processed_open_ny = ts_ny
            return

        try:
            outcome = self.current_strategy.update(
                timestamp=ts_ny,
                open_=float(bar.open), high=float(bar.high),
                low=float(bar.low), close=float(bar.close),
                pivots=piv, fx_day_start=fxs,
            )
        except Exception:
            logger.exception(f"[{symbol}] strategy.update raised on bar {ts_ny.isoformat()}")
            self.last_processed_open_ny = ts_ny
            return

        self.last_processed_open_ny = ts_ny
        self.current_fx_day_start = fxs

        opened_actions = {"ENTRY_LONG", "ENTRY_SHORT", "REVERSE_TO_LONG", "REVERSE_TO_SHORT"}
        closed_actions = {"EXIT_FLIP", "EXIT_EOD"}
        opens = [e for e in outcome.events if e.action in opened_actions]
        closes = [e for e in outcome.events if e.action in closed_actions]
        base_str = (f"{outcome.base_level_name}@{outcome.base_level:.5f}"
                    if outcome.base_level is not None else "none")
        adx_str = f"{outcome.adx:.1f}" if outcome.adx is not None else "-"
        ema_str = f"{outcome.ema_fast:.5f}" if outcome.ema_fast is not None else "-"
        logger.info(
            f"[BAR] {ts_ny.strftime('%a %m-%d %H:%M')}  asset={symbol}  "
            f"H/L/C={float(bar.high):.5f}/{float(bar.low):.5f}/{outcome.close:.5f}  |  "
            f"base={base_str}  bias={outcome.bias:<5} "
            f"st={outcome.st_state:<5}(dir={outcome.st_dir:+d})  adx={adx_str}  ema50={ema_str}  |  "
            f"new_trade={'YES(' + opens[0].action + ')' if opens else 'no'}  "
            f"closed={'YES(' + closes[0].action + ')' if closes else 'no'}  "
            f"pos={outcome.position_before:+d}->{outcome.position_after:+d}"
        )

        running_pos = outcome.position_before
        for ev in outcome.events:
            tag = "[LIVE]" if self.config.LIVE_TRADING else "[SHADOW]"
            logger.info(
                f"{tag} {symbol}  {ev.action}  @{ev.price:.5f}  bias={ev.bias}  "
                f"base={ev.base_level_name}@{ev.base_level:.5f}  st={ev.st_state}  "
                f"pos: {running_pos} -> {ev.new_position}"
            )
            if self.shadow_log is not None:
                self.shadow_log.record_event(symbol, ev)

            if self.config.LIVE_TRADING:
                delta = ev.new_position - running_pos
                if delta == 1:
                    side = "BUY"
                elif delta == -1:
                    side = "SELL"
                else:
                    logger.error(f"[LIVE] unexpected position delta {delta} on {ev.action} "
                                 f"(prev={running_pos}, new={ev.new_position}) - skipping order")
                    running_pos = ev.new_position
                    continue
                await self._fire_live_order(symbol, ev, side)

            running_pos = ev.new_position
            self._save_state()

    async def _fire_live_order(self, symbol: str, ev, side: str) -> None:
        contract = await self.ibkr.qualify_cfd(symbol)
        from ib_async import MarketOrder
        order = MarketOrder(side, self.config.cfd_units)
        order.account = self.config.cfd_account
        order.whatIf = False
        order.tif = "DAY"
        try:
            trade = self.ibkr.ib.placeOrder(contract, order)
            logger.warning(
                f"[LIVE] submitted: {ev.action} {side} {self.config.cfd_units} {symbol} "
                f"CFD -> account {self.config.cfd_account} orderId={trade.order.orderId}"
            )
        except Exception:
            logger.exception(f"[LIVE] placeOrder raised for {ev.action} {symbol}")

    # ------------------- helpers -------------------
    @staticmethod
    def _filter_window(bars, start_ny: datetime, end_ny: datetime):
        out = []
        for b in bars:
            ts_ny = _bar_ts_ny(b)
            if start_ny <= ts_ny < end_ny:
                out.append(b)
        return out
