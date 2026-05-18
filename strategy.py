"""
forex_cpr_ibkr — integrated live/shadow strategy loop.

What it does (per session within the Sun 17:00 → Fri 17:00 NY zone):
  1. Bootstrap accounts (balances + the CFD trading account check).
  2. Run daily-narrowest selection (15-pair CPR-width comparison).
  3. For the selected pair:
       a. Fetch `warmup_days` of 5-min bars (chunked, dedup).
       b. Build the per-FX-day HLC cache → CPR (TC/BC/Pivot) per day.
       c. Instantiate `CPRSuperTrendStrategy` and replay every warmup bar
          chronologically to warm the regime classifier + AdaptiveSuperTrend.
       d. Subscribe to live streaming 5-min bars.
  4. On each newly-closed live bar: update the fx_day_hlc cache, look up
     today's CPR (from prior trading FX day's HLC), feed to the strategy,
     log every event to the shadow CSV, and (only if LIVE_TRADING=True)
     place a real CFD market order on `cfd_account`.
  5. At each 17:00 NY rollover, re-run selection. If the narrowest pair
     changed, tear down the current pair's stream + strategy and rebuild
     for the new pair (force-exit at 16:55 has already flattened position).
  6. At Fri 17:00 NY, tear down and drop into the weekend gate.

Shadow mode (LIVE_TRADING=False, default):
  No real orders are placed. Every strategy decision is logged to
  `backtest_output/shadow/shadow_events_<session>.csv` and rolled up into
  trades in `shadow_trades_<session>.csv`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, Optional

from adaptive_supertrend import AdaptiveSTConfig
from balance_store import BalanceStore
from config import StrategyConfig, TRADING_ZONE_POLL_SECONDS
from cpr import compute_cpr_from_bars, compute_cpr_from_hlc
from cpr_st_strategy import CPRSuperTrendStrategy
from ibkr_client import IBKRClient
from regime import RegimeConfig
from selection import SelectionError, narrowest_pair
from shadow_log import ShadowLog
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
    """Raised when persisted state doesn't match IBKR positions and
    force_clean_restart wasn't set. Bot refuses to start to prevent
    orphaning or double-trading a position."""


RECONNECT_BACKOFF_INITIAL_S = 5.0
RECONNECT_BACKOFF_MAX_S = 60.0


def _bar_ts_ny(bar) -> datetime:
    ts = bar.date
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    return to_ny(ts)


class Strategy:
    """Integrated live/shadow strategy."""

    def __init__(
        self,
        config: StrategyConfig,
        ibkr: IBKRClient,
        balances: BalanceStore,
        shadow_dir: Path = Path("backtest_output/shadow"),
        state_store: Optional[StateStore] = None,
        force_clean_restart: bool = False,
    ):
        self.config = config
        self.ibkr = ibkr
        self.balances = balances
        self.shadow_dir = shadow_dir
        self.state_store = state_store
        self.force_clean_restart = force_clean_restart

        # Per-pair state — torn down and rebuilt on each pair change.
        self.current_pair: Optional[str] = None
        self.current_strategy: Optional[CPRSuperTrendStrategy] = None
        self.current_bars_handle = None
        self.current_force_exit_time: Optional[time] = None
        # Per-FX-day HLC for the CURRENT pair only. Rebuilt on pair change.
        self.fx_day_hlc: Dict[datetime, tuple[float, float, float]] = {}
        # Last bar timestamp processed by the strategy (for streaming dedupe).
        self.last_processed_open_ny: Optional[datetime] = None
        # Track which FX day the strategy is in to detect rollover.
        self.current_fx_day_start: Optional[datetime] = None

        self.active_accounts: Dict[str, float] = {}
        self.shadow_log: Optional[ShadowLog] = None
        self._stop = asyncio.Event()
        self._bar_evt = asyncio.Event()

    # ─────────────────── entry point ───────────────────
    async def run(self) -> None:
        backoff_s = RECONNECT_BACKOFF_INITIAL_S
        try:
            while not self._stop.is_set():
                try:
                    if not self.ibkr.is_connected:
                        logger.info("Connecting to IBKR...")
                        await self.ibkr.connect()
                        backoff_s = RECONNECT_BACKOFF_INITIAL_S

                    while (not is_in_trading_zone(ny_now())
                            and not self._stop.is_set()):
                        if not self.ibkr.is_connected:
                            raise ConnectionLostError("disconnected while waiting for zone")
                        logger.info(
                            f"Outside trading zone; sleeping {TRADING_ZONE_POLL_SECONDS}s..."
                        )
                        await self._interruptible_sleep(TRADING_ZONE_POLL_SECONDS)
                    if self._stop.is_set():
                        break

                    await self._run_session()
                except StateMismatchError:
                    # Don't loop on this — the user needs to intervene.
                    raise
                except ConnectionLostError as e:
                    logger.warning(f"Connection lost ({e}); reconnecting in {backoff_s:.0f}s")
                    await self._teardown_pair()
                    await self._safe_disconnect()
                    await self._interruptible_sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, RECONNECT_BACKOFF_MAX_S)
                except Exception:
                    logger.exception(
                        f"Unexpected session error; reconnecting in {backoff_s:.0f}s"
                    )
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

    # ─────────────────── session = inside-zone segment ───────────────────
    async def _run_session(self) -> None:
        await self._bootstrap_accounts()
        await self._verify_cfd_account_present()

        if self.shadow_log is None:
            self.shadow_log = ShadowLog(self.shadow_dir, ny_now())

        # Initial selection + strategy install.
        await self._select_and_install_strategy()

        # Main loop: wait for bar events. Periodically check for FX-day
        # rollover (which may require pair reselection).
        last_seen_fx_day = self.current_fx_day_start
        while is_in_trading_zone(ny_now()) and not self._stop.is_set():
            if not self.ibkr.is_connected:
                raise ConnectionLostError("ib socket dropped during session")
            try:
                await asyncio.wait_for(self._bar_evt.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
            self._bar_evt.clear()
            # FX-day rollover detection.
            cur_fx_day, _ = current_fx_day_anchor(ny_now())
            if last_seen_fx_day is not None and cur_fx_day != last_seen_fx_day:
                logger.info(f"FX-day rollover detected → {cur_fx_day.isoformat()}")
                last_seen_fx_day = cur_fx_day
                await self._select_and_install_strategy()
            else:
                last_seen_fx_day = cur_fx_day

        await self._teardown_pair()
        logger.info("Exited trading zone — dropping back to gate")

    # ─────────────────── bootstrap ───────────────────
    async def _bootstrap_accounts(self) -> None:
        if self.balances.has_file():
            self.balances.load()
            logger.info(f"Loaded frozen balances from {self.balances.path}")
        else:
            logger.info("No balance file — fetching from IBKR (one-time snapshot)")
            fresh = await self.ibkr.fetch_account_balances_usd()
            self.balances.init_from(fresh)
            logger.info(f"Wrote frozen balances → {self.balances.path}: {fresh}")
        active = self.balances.active_accounts()
        if not active:
            logger.warning("No active sub-accounts (none ≥ $1000)")
        else:
            logger.info(f"Active accounts ({len(active)}): "
                        + ", ".join(f"{a}=${b:,.2f}" for a, b in active.items()))
        self.active_accounts = active

    async def _verify_cfd_account_present(self) -> None:
        managed = self.ibkr.managed_accounts()
        if self.config.cfd_account not in managed:
            logger.warning(
                f"cfd_account={self.config.cfd_account} NOT in managed accounts "
                f"{managed} — shadow logging will still work, but if you flip "
                f"LIVE_TRADING=True orders will fail."
            )
        else:
            logger.info(f"CFD trading account: {self.config.cfd_account}  "
                        f"(LIVE_TRADING={self.config.LIVE_TRADING})")

    # ─────────────────── selection + install strategy ───────────────────
    async def _select_and_install_strategy(self) -> None:
        """Pick the daily-narrowest pair and (re)build the strategy if it changed."""
        sel = await self._compute_daily_selection()
        if sel is None:
            return
        winner, cpr = sel
        if winner == self.current_pair:
            logger.info(f"Selected pair unchanged: {winner}  (no rebuild needed)")
            return

        # Pair changed — tear down old + build new.
        if self.current_pair is not None:
            logger.info(f"Pair change {self.current_pair} → {winner}; tearing down")
            await self._teardown_pair()

        await self._install_strategy_for_pair(winner)

    async def _compute_daily_selection(self):
        now = ny_now()
        ws, we = prior_trading_fx_day_window(now)
        logger.info(f"Computing daily CPR for {len(self.config.symbols_list)} symbols, "
                    f"prior trading FX day {ws.isoformat()} → {we.isoformat()}")
        cprs = {}
        for sym in self.config.symbols_list:
            try:
                bars = await self.ibkr.fetch_5min_bars(sym, end_ny=we, duration_str="2 D")
            except Exception:
                logger.exception(f"{sym}: fetch failed; skipping")
                continue
            in_window = self._filter_window(bars, ws, we)
            if not in_window:
                logger.warning(f"{sym}: no bars in prior-FX-day window — skipping")
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
            logger.error("No symbols produced a CPR — skipping selection")
            return None
        candidates = [s for s in self.config.symbols_list if s in cprs]
        try:
            winner = narrowest_pair(candidates, cprs)
        except SelectionError as e:
            logger.error(f"Selection failed: {e}")
            return None
        wcpr = cprs[winner]
        logger.info(
            f"SELECTED {winner}  width_pct={wcpr.width_pct:.4f}%  "
            f"TC={wcpr.tc:.5f}  BC={wcpr.bc:.5f}"
        )
        return winner, wcpr

    # ─────────────────── state persistence + reconciliation ───────────────────
    async def _reconcile_and_restore(self) -> None:
        """
        After install: compare persisted state + IBKR positions and either
        restore the strategy's position or halt on mismatch.

        Decision matrix:
          (A) no state file + IBKR flat                → fresh start, OK
          (B) no state file + IBKR has position        → halt (unaccounted)
          (C) state file pos=0 + IBKR flat             → OK
          (D) state file pos=N + IBKR has matching N   → restore strategy.position
          (E) state file pos=N + IBKR mismatch         → halt
        """
        if self.state_store is None or self.current_strategy is None:
            return

        if self.force_clean_restart:
            if self.state_store.exists():
                logger.warning(
                    f"--force-clean-restart: deleting state file {self.state_store.path}"
                )
                self.state_store.delete()
            return

        # Fetch IBKR positions for our CFD account.
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

        # Any CFD position on a pair OTHER than the current selection is a
        # stranded open trade — halt for safety.
        for p in cfd_positions:
            pair = (p.contract.symbol or "") + (p.contract.currency or "")
            if pair != self.current_pair:
                raise StateMismatchError(
                    f"Stranded IBKR CFD position on {pair} (qty={p.position:+g}) "
                    f"in {self.config.cfd_account}, but today's selection is "
                    f"{self.current_pair}. Close manually in TWS, then re-run "
                    f"with --force-clean-restart."
                )

        # Determine IBKR's position on the current pair.
        cur_pair_pos = next(
            (p for p in cfd_positions
             if (p.contract.symbol + p.contract.currency) == self.current_pair),
            None,
        )
        ibkr_qty = float(cur_pair_pos.position) if cur_pair_pos else 0.0
        ibkr_position = 1 if ibkr_qty > 0 else -1 if ibkr_qty < 0 else 0

        # Load persisted state.
        persisted: Optional[PersistedState] = None
        if self.state_store.exists():
            try:
                persisted = self.state_store.load()
            except StateStoreError as e:
                raise StateMismatchError(
                    f"State file unreadable: {e}. Fix or delete it and re-run "
                    f"with --force-clean-restart."
                )

        persisted_pos = persisted.position if persisted else 0

        # Reconcile.
        if persisted_pos != ibkr_position:
            raise StateMismatchError(
                f"State/IBKR mismatch: state file says position={persisted_pos} "
                f"on {persisted.selected_pair if persisted else '(none)'} but "
                f"IBKR has position={ibkr_position} (qty={ibkr_qty:+g}) on "
                f"{self.current_pair}. Resolve manually, then re-run with "
                f"--force-clean-restart."
            )

        if ibkr_position == 0:
            logger.info("Reconciliation OK: both state and IBKR are flat.")
            return

        # Restore strategy state from persisted.
        s = self.current_strategy
        s.position = ibkr_position
        s.entry_price = persisted.entry_price if persisted else None
        if persisted and persisted.entry_timestamp:
            try:
                s.entry_timestamp = datetime.fromisoformat(persisted.entry_timestamp)
            except ValueError:
                logger.warning(
                    f"Could not parse persisted entry_timestamp "
                    f"{persisted.entry_timestamp!r}; leaving None"
                )
        # Set prev_active_dir to match position direction so the NEXT opposite
        # AST flip triggers an exit (otherwise a flip that happened during
        # downtime would be silently missed).
        s._prev_active_dir = ibkr_position
        logger.info(
            f"[RESTORE] {self.current_pair} position={ibkr_position} "
            f"entry={s.entry_price} restored from state file + IBKR"
        )

    def _save_state(self) -> None:
        """Best-effort save after each event; never crashes the strategy."""
        if self.state_store is None or self.current_strategy is None:
            return
        s = self.current_strategy
        state = PersistedState(
            cfd_account=self.config.cfd_account,
            selected_pair=self.current_pair,
            position=s.position,
            entry_price=s.entry_price,
            entry_timestamp=(
                s.entry_timestamp.isoformat() if s.entry_timestamp else None
            ),
            last_processed_open_ny=(
                self.last_processed_open_ny.isoformat()
                if self.last_processed_open_ny else None
            ),
        )
        try:
            self.state_store.save(state)
        except Exception:
            logger.exception("State save failed (continuing — strategy unaffected)")

    async def _install_strategy_for_pair(self, symbol: str) -> None:
        """Pre-fetch warmup, build strategy, replay, start streaming."""
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

        # Build FX-day HLC cache.
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

        # Build the strategy with default Regime + AST configs.
        strategy = CPRSuperTrendStrategy(
            regime_cfg=RegimeConfig(),
            ast_cfg=AdaptiveSTConfig(),
            force_exit_close_time=close_t,
            ast_bars_per_day=288,
        )

        # Replay bars chronologically, calling strategy.update on each.
        # Use per-bar CPR from the FX day's prior trading day's HLC.
        bars_sorted = sorted(bars, key=lambda b: _bar_ts_ny(b))
        replayed = 0
        for b in bars_sorted:
            ts_ny = _bar_ts_ny(b)
            cpr = self._cpr_for_ts(ts_ny)
            if cpr is None:
                continue   # no prior CPR available; skip warmup bars before first usable day
            fxs, _ = current_fx_day_anchor(ts_ny)
            outcome = strategy.update(
                timestamp=ts_ny,
                open_=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                daily_tc=cpr.tc,
                daily_bc=cpr.bc,
                daily_pp=cpr.pivot,
                fx_day_start=fxs,
            )
            replayed += 1
            # During warmup we DO NOT log events — these are historical
            # decisions that have already passed.
            self.last_processed_open_ny = ts_ny
            self.current_fx_day_start = fxs

        logger.info(f"[install] Replayed {replayed} warmup bars; strategy is warm")

        # Install the live state.
        self.current_pair = symbol
        self.current_strategy = strategy
        self.current_force_exit_time = close_t

        # Reconcile against IBKR + persisted state BEFORE we start streaming.
        # If there's a mismatch, raise StateMismatchError — the user must
        # intervene before the bot can run.
        await self._reconcile_and_restore()

        # Save a snapshot of the post-reconciliation state.
        self._save_state()

        # Subscribe to streaming bars.
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

    # ─────────────────── per-bar processing ───────────────────
    def _cpr_for_ts(self, ts_ny: datetime):
        """Return CPR for the FX day containing ts_ny, or None if its prior
        trading FX day's HLC is not yet in our cache."""
        fxs, _ = current_fx_day_anchor(ts_ny)
        sample = fxs + timedelta(hours=1)
        prior_start, _ = prior_trading_fx_day_window(sample)
        hlc = self.fx_day_hlc.get(prior_start)
        if hlc is None:
            return None
        H, L, C = hlc
        try:
            return compute_cpr_from_hlc(H, L, C)
        except Exception:
            return None

    def _make_bar_handler(self, symbol: str):
        def handler(bars, hasNewBar):
            # Wake the main loop.
            self._bar_evt.set()
            if not hasNewBar or len(bars) < 2:
                return
            # The just-closed bar is bars[-2] (the last is still updating).
            closed = bars[-2]
            asyncio.create_task(self._on_closed_bar(symbol, closed))
        return handler

    async def _on_closed_bar(self, symbol: str, bar) -> None:
        """Top-level try/except so a crash here doesn't silently kill the
        asyncio task and leave the bot running deaf to bar updates."""
        try:
            await self._on_closed_bar_impl(symbol, bar)
        except Exception:
            logger.exception(
                f"[{symbol}] _on_closed_bar raised on bar {getattr(bar, 'date', '?')}"
            )

    async def _on_closed_bar_impl(self, symbol: str, bar) -> None:
        # Only handle bars from the currently-installed pair.
        if symbol != self.current_pair or self.current_strategy is None:
            return
        ts_ny = _bar_ts_ny(bar)
        if (self.last_processed_open_ny is not None
                and ts_ny <= self.last_processed_open_ny):
            return   # dedupe — already processed during warmup or earlier event

        # Update HLC cache for this bar's FX day.
        fxs, _ = current_fx_day_anchor(ts_ny)
        cur = self.fx_day_hlc.get(fxs)
        if cur is None:
            self.fx_day_hlc[fxs] = (float(bar.high), float(bar.low), float(bar.close))
        else:
            H, L, _ = cur
            self.fx_day_hlc[fxs] = (max(H, float(bar.high)),
                                    min(L, float(bar.low)),
                                    float(bar.close))

        cpr = self._cpr_for_ts(ts_ny)
        if cpr is None:
            logger.warning(f"[{symbol}] {ts_ny.isoformat()} no CPR available — skipping bar")
            self.last_processed_open_ny = ts_ny
            return

        try:
            outcome = self.current_strategy.update(
                timestamp=ts_ny,
                open_=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                daily_tc=cpr.tc,
                daily_bc=cpr.bc,
                daily_pp=cpr.pivot,
                fx_day_start=fxs,
            )
        except Exception:
            logger.exception(f"[{symbol}] strategy.update raised on bar {ts_ny.isoformat()}")
            self.last_processed_open_ny = ts_ny
            return

        self.last_processed_open_ny = ts_ny
        self.current_fx_day_start = fxs

        # Per-bar heartbeat: always logs the three entry gates + trade flags.
        # Greppable format: `[BAR] <ts> <asset> close=X | bias=Y regime=Z st=W |
        # new_trade=... closed=... pos=A→B`.
        opened_actions = {"ENTRY_LONG", "ENTRY_SHORT",
                          "REVERSE_TO_LONG", "REVERSE_TO_SHORT"}
        closed_actions = {"EXIT_FLIP", "EXIT_EOD"}
        opens = [e for e in outcome.events if e.action in opened_actions]
        closes = [e for e in outcome.events if e.action in closed_actions]
        new_trade_str = (
            f"YES({opens[0].action})" if opens else "no"
        )
        closed_str = (
            f"YES({closes[0].action})" if closes else "no"
        )
        st_str = (
            "+1 GREEN" if outcome.active_dir == 1
            else "-1 RED" if outcome.active_dir == -1
            else " 0  --"
        )
        logger.info(
            f"[BAR] {ts_ny.strftime('%a %m-%d %H:%M')}  asset={symbol}  "
            f"close={outcome.close:.5f}  |  "
            f"bias={outcome.bias:<5}  regime={outcome.regime:<18}  st={st_str} "
            f"({outcome.active_method})  |  "
            f"new_trade={new_trade_str}  closed={closed_str}  "
            f"pos={outcome.position_before:+d}→{outcome.position_after:+d}"
        )

        # Surface events. Use position-delta to pick the order side — this
        # handles all 4 transitions (open long/short, close long/short,
        # reversal legs) without depending on active_dir, which is wrong
        # for EXIT_EOD (force-exit doesn't follow an AST flip).
        running_pos = outcome.position_before
        for ev in outcome.events:
            tag = "[LIVE]" if self.config.LIVE_TRADING else "[SHADOW]"
            logger.info(
                f"{tag} {symbol}  {ev.action}  @{ev.price:.5f}  "
                f"bias={ev.bias}  regime={ev.regime}  "
                f"ast_dir={ev.active_dir} ({ev.active_method})  "
                f"pos: {running_pos} → {ev.new_position}"
            )
            if self.shadow_log is not None:
                self.shadow_log.record_event(symbol, ev, cpr.tc, cpr.bc, cpr.pivot)

            if self.config.LIVE_TRADING:
                delta = ev.new_position - running_pos
                if delta == 1:
                    side = "BUY"
                elif delta == -1:
                    side = "SELL"
                else:
                    logger.error(
                        f"[LIVE] unexpected position delta {delta} on {ev.action} "
                        f"(prev={running_pos}, new={ev.new_position}) — skipping order"
                    )
                    running_pos = ev.new_position
                    continue
                await self._fire_live_order(symbol, ev, side)

            running_pos = ev.new_position
            # Persist after every event so a crash mid-trade leaves a
            # consistent file the reconciler can pick up.
            self._save_state()

    async def _fire_live_order(self, symbol: str, ev, side: str) -> None:
        """
        Live-mode order placement. Side is computed by the caller from the
        position-delta (BUY for +1, SELL for -1). NO whatIf — real orders.
        """
        contract = await self.ibkr.qualify_cfd(symbol)
        from ib_async import MarketOrder
        order = MarketOrder(side, self.config.cfd_units)
        order.account = self.config.cfd_account
        order.whatIf = False
        order.tif = "DAY"
        try:
            trade = self.ibkr.ib.placeOrder(contract, order)
            logger.warning(
                f"[LIVE] submitted: {ev.action} {side} {self.config.cfd_units} "
                f"{symbol} CFD → account {self.config.cfd_account} "
                f"orderId={trade.order.orderId}"
            )
        except Exception:
            logger.exception(f"[LIVE] placeOrder raised for {ev.action} {symbol}")

    # ─────────────────── helpers ───────────────────
    @staticmethod
    def _filter_window(bars, start_ny: datetime, end_ny: datetime):
        out = []
        for b in bars:
            ts_ny = _bar_ts_ny(b)
            if start_ny <= ts_ny < end_ny:
                out.append(b)
        return out
