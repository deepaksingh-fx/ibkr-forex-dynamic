"""
forex_cpr_ibkr — main strategy loop.

Orchestrates everything:
  1. Trading-zone gate (poll every 60s outside).
  2. On entry to zone: load/init balances, filter ≥$1000, weekly CPR, selection.
  3. Pre-warm 50-EMA per shortlisted pair (fetch ≥250 historical 5-min bars).
  4. For Tue–Fri: also compute today's daily CPR from prior FX day.
     For Mon: weekly CPR == daily CPR.
  5. Subscribe to streaming 5-min bars per shortlisted pair.
  6. On each new closed bar:
        update EMA → exit checks (EOD/breaker/SL/trail/reversal) → entry trigger
  7. On 17:00 NY rollover detected via bar timestamps:
        - Tue→Fri close-of-day: recompute next day's daily CPR
        - Sun rollover: recompute weekly CPR + re-run selection
  8. On Fri 17:00 NY: cancel streams, drop into trading-zone gate.

NO orders unless LIVE_TRADING=True (config). Dry-run logs full intent.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from config import (
    EMA_PERIOD,
    EMA_PREWARM_BARS,
    LOT_SIZE,
    StrategyConfig,
    TRADING_ZONE_POLL_SECONDS,
    TRAIL_ARM_PCT,
)
from cpr import CPR, compute_cpr_from_bars
from ibkr_client import IBKRClient
from indicators import EMA
from balance_store import BalanceStore
from pnl_tracker import PnLTracker
from selection import select_shortlist, SelectionError
from state_store import (
    PersistedPosition,
    PersistedState,
    StateStore,
)
from time_utils import (
    NY,
    current_fx_day_anchor,
    is_eod_candle_close,
    is_in_trading_zone,
    ny_now,
    prior_fx_day_window,
    prior_week_window,
    to_ny,
)

logger = logging.getLogger(__name__)

LOT_UNITS = LOT_SIZE * 100_000   # 10,000 base ccy at 0.10 lot
EOD_TIME = time(16, 55)


class StateMismatchError(RuntimeError):
    """Raised at startup when persisted state ≠ IBKR positions."""


# ─── Per-pair state (across all accounts) ─────────────────────────────────
@dataclass
class _Position:
    side: str                     # "LONG" or "SHORT"
    entry_price: float
    entry_time: datetime
    trail_armed: bool = False


@dataclass
class _PairState:
    symbol: str
    accounts: List[str]
    weekly_cpr: CPR
    daily_cpr: CPR                                    # = weekly_cpr on Mon, prior FX day Tue–Fri
    ema: EMA = field(default_factory=lambda: EMA(EMA_PERIOD))
    # account -> Position | None
    positions: Dict[str, Optional[_Position]] = field(default_factory=dict)
    halted: Dict[str, bool] = field(default_factory=dict)
    last_processed_open_ny: Optional[datetime] = None
    last_fx_day_start: Optional[datetime] = None
    bars_handle: object = None     # BarDataList we subscribed to


# ─── Strategy ─────────────────────────────────────────────────────────────
class Strategy:
    def __init__(
        self,
        config: StrategyConfig,
        ibkr: IBKRClient,
        pnl: PnLTracker,
        balances: BalanceStore,
        state_store: Optional[StateStore] = None,
        force_clean_restart: bool = False,
    ):
        self.config = config
        self.ibkr = ibkr
        self.pnl = pnl
        self.balances = balances
        self.state_store = state_store
        self.force_clean_restart = force_clean_restart
        self.active_accounts: Dict[str, float] = {}   # account -> frozen balance
        self.pair_states: Dict[str, _PairState] = {}
        self._stop = asyncio.Event()
        self._bar_evt = asyncio.Event()    # set whenever any pair's bar list updates

    # ───────────────────────── entry point ─────────────────────────
    async def run(self) -> None:
        await self.ibkr.connect()
        try:
            while not self._stop.is_set():
                # Trading-zone gate.
                while not is_in_trading_zone(ny_now()):
                    logger.info(
                        "Outside trading zone (Sun 17:00 → Fri 17:00 NY); "
                        f"sleeping {TRADING_ZONE_POLL_SECONDS}s..."
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=TRADING_ZONE_POLL_SECONDS)
                        return  # _stop set
                    except asyncio.TimeoutError:
                        continue

                # In zone — bootstrap and run a session.
                await self._run_session()
        finally:
            await self.ibkr.disconnect()

    def stop(self) -> None:
        self._stop.set()

    # ───────────────────────── one session = one trading-week segment ─────────────────────────
    async def _run_session(self) -> None:
        """Run from 'now' (inside zone) until we exit the zone (Fri 17:00 NY)."""
        await self._bootstrap_accounts()
        # Reconcile against IBKR before any subscription — fail loud on mismatch.
        await self._reconcile_with_ibkr()
        await self._bootstrap_selection_and_subscribe()
        self._restore_positions_from_persisted()
        # Save fresh state after bootstrap completes.
        self._save_state()

        # Active loop: wait for bar updates and react. Periodically wake to
        # check the zone gate. Streams handle the rest via callbacks.
        while is_in_trading_zone(ny_now()) and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._bar_evt.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
            self._bar_evt.clear()

        # Exited the zone — clean up streams.
        await self._teardown_streams()

    # ───────────────────────── account bootstrap ─────────────────────────
    async def _bootstrap_accounts(self) -> None:
        if self.balances.has_file():
            self.balances.load()
            logger.info(f"Loaded frozen balances from {self.balances.path}")
        else:
            logger.info("No balance file found — fetching from IBKR (one-time snapshot)")
            fresh = await self.ibkr.fetch_account_balances_usd()
            self.balances.init_from(fresh)
            logger.info(f"Wrote frozen balances → {self.balances.path}: {fresh}")

        active = self.balances.active_accounts()
        if not active:
            raise RuntimeError(
                "No active accounts (none have balance ≥ $1000). "
                "Check account_balances.json or fund an account."
            )
        self.active_accounts = active
        logger.info(
            f"Active accounts ({len(active)}): "
            + ", ".join(f"{a}=${b:,.2f}" for a, b in active.items())
        )

    # ───────────────────────── persistence helpers ─────────────────────────
    def _build_persisted_state(self) -> PersistedState:
        cur_fx_start, _ = current_fx_day_anchor(ny_now())
        positions: Dict[str, Dict[str, PersistedPosition]] = {}
        day_realized: Dict[str, float] = {}
        halted: Dict[str, bool] = {}
        for sym, ps in self.pair_states.items():
            positions[sym] = {}
            for acct, pos in ps.positions.items():
                if pos is None:
                    continue
                positions[sym][acct] = PersistedPosition(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    entry_time=pos.entry_time.isoformat(),
                    trail_armed=pos.trail_armed,
                )
            for acct, h in ps.halted.items():
                # take the OR across pairs (per-account flag)
                halted[acct] = halted.get(acct, False) or h
        for acct in self.active_accounts:
            day_realized[acct] = self.pnl.day_pnl(acct)
        return PersistedState(
            fx_day_start=cur_fx_start.isoformat(),
            shortlist=list(self.pair_states.keys()),
            positions=positions,
            day_realized=day_realized,
            halted=halted,
        )

    def _save_state(self) -> None:
        """Best-effort persist; never crashes the strategy on persistence failure."""
        if self.state_store is None:
            return
        try:
            self.state_store.save(self._build_persisted_state())
        except Exception:
            logger.exception("State persist failed (continuing — strategy is unaffected)")

    async def _reconcile_with_ibkr(self) -> None:
        """
        On startup, compare any persisted state against IBKR's actual positions.
        - No state file + IBKR clean       → fresh start, OK
        - No state file + IBKR has positions for active accounts → halt unless force_clean_restart
        - State file + matching IBKR       → resume (restore positions on first bar after subscribe)
        - State file + mismatching IBKR    → halt unless force_clean_restart
        """
        if self.state_store is None:
            return

        # Force-clean: nuke state file and skip reconciliation.
        if self.force_clean_restart:
            if self.state_store.exists():
                logger.warning(f"--force-clean-restart: deleting state file {self.state_store.path}")
                self.state_store.delete()
            return

        ibkr_positions = []
        try:
            ibkr_positions = await self.ibkr.get_open_positions()
        except Exception:
            logger.exception("Failed to fetch IBKR positions for reconciliation")

        # Filter to forex positions in our active accounts.
        relevant: Dict[tuple, dict] = {}   # (pair, account) -> {"qty": signed_qty, "avg_cost": ...}
        for ib_pos in ibkr_positions:
            try:
                if ib_pos.contract.secType != "CASH":
                    continue
                if ib_pos.account not in self.active_accounts:
                    continue
                if abs(ib_pos.position) < 1e-9:
                    continue   # closed
                pair = ib_pos.contract.symbol + ib_pos.contract.currency
                relevant[(pair, ib_pos.account)] = {
                    "qty": float(ib_pos.position),
                    "avg_cost": float(getattr(ib_pos, "avgCost", 0.0)),
                }
            except Exception:
                logger.exception("Skipping malformed IBKR position entry")

        # Load persisted state if any.
        persisted: Optional[PersistedState] = None
        if self.state_store.exists():
            try:
                persisted = self.state_store.load()
            except Exception:
                logger.exception("State file unreadable — refusing to start. "
                                 "Use --force-clean-restart to overwrite.")
                raise StateMismatchError("State file unreadable")

        # Build set of expected (pair, account) → side from state.
        expected: Dict[tuple, str] = {}
        if persisted:
            for pair, by_acct in persisted.positions.items():
                for acct, pos in by_acct.items():
                    expected[(pair, acct)] = pos.side

        issues: List[str] = []

        # State expects but IBKR doesn't have:
        for key, side in expected.items():
            if key not in relevant:
                issues.append(f"State expects {key[0]} {side} for {key[1]} but IBKR has no position")

        # IBKR has but state doesn't expect:
        for key, info in relevant.items():
            if key not in expected:
                issues.append(f"IBKR has {key[0]} qty={info['qty']:+g} for {key[1]} "
                              f"but state has no record")
                continue
            # Both have it — check direction/qty.
            ib_side = "LONG" if info["qty"] > 0 else "SHORT"
            if ib_side != expected[key]:
                issues.append(f"{key[0]} {key[1]}: state={expected[key]} but IBKR={ib_side}")

        if issues:
            for i in issues:
                logger.error(f"RECONCILIATION ISSUE: {i}")
            raise StateMismatchError(
                f"{len(issues)} reconciliation issue(s) detected. "
                f"Resolve manually (close stray positions in TWS or fix state file), "
                f"then re-run. To wipe state and start fresh, use --force-clean-restart."
            )

        if persisted and not relevant:
            logger.info("Reconciliation OK: no state positions, no IBKR positions.")
        elif persisted:
            logger.info(f"Reconciliation OK: {len(relevant)} matched position(s) "
                        f"between state file and IBKR — strategy will resume.")
        else:
            logger.info("Reconciliation OK: no state file (first run), no IBKR positions.")
        # Stash for later restoration after subscriptions are wired up.
        self._persisted_to_restore = persisted

    def _restore_positions_from_persisted(self) -> None:
        """After pair_states are built, restore positions/trail flags from persisted."""
        persisted: Optional[PersistedState] = getattr(self, "_persisted_to_restore", None)
        if persisted is None:
            return
        for sym, by_acct in persisted.positions.items():
            ps = self.pair_states.get(sym)
            if ps is None:
                continue   # pair no longer in shortlist
            for acct, pos in by_acct.items():
                if acct not in ps.positions:
                    continue
                try:
                    et = datetime.fromisoformat(pos.entry_time)
                except Exception:
                    et = ny_now()
                ps.positions[acct] = _Position(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    entry_time=et,
                    trail_armed=pos.trail_armed,
                )
                # Re-prime PnL tracker so per-trade SL math has the entry.
                self.pnl.on_entry(
                    account=acct, symbol=sym, side=pos.side,
                    entry_price=pos.entry_price, lot_units=LOT_UNITS,
                )
                logger.info(f"[RESTORE] {acct} {sym} {pos.side} @{pos.entry_price:.5f} "
                            f"(trail_armed={pos.trail_armed})")
        # Restore halted flags + day_realized
        for acct, h in persisted.halted.items():
            for ps in self.pair_states.values():
                if acct in ps.halted:
                    ps.halted[acct] = h
        # Day-realized PnL is replayed by re-priming PnLTracker exits — but in the
        # SimulatedPnLTracker we don't have a direct "set realized" hook. The day
        # number will be approximate after restart; if you need exact, restart at
        # 17:00 NY rollover when day_pnl resets anyway.

    # ───────────────────────── selection + subscribe ─────────────────────────
    async def _bootstrap_selection_and_subscribe(self) -> None:
        now = ny_now()
        ws, we = prior_week_window(now)
        logger.info(f"Computing weekly CPR for {len(self.config.symbols_list)} symbols, "
                    f"window {ws.isoformat()} → {we.isoformat()}")

        # Fetch + compute weekly CPR for every symbol in the input list.
        weekly_cprs: Dict[str, CPR] = {}
        for sym in self.config.symbols_list:
            bars = await self.ibkr.fetch_5min_bars(sym, end_ny=we, duration_str="1 W")
            in_window = self._filter_window(bars, ws, we)
            if not in_window:
                logger.warning(f"{sym}: no bars in weekly window — skipping")
                continue
            cpr = compute_cpr_from_bars(
                [b.high for b in in_window],
                [b.low for b in in_window],
                [b.close for b in in_window],
            )
            weekly_cprs[sym] = cpr

        # Run selection.
        try:
            sel = select_shortlist(
                list(self.config.symbols_list),
                list(self.config.allowed_currencies),
                weekly_cprs,
            )
        except SelectionError as e:
            raise RuntimeError(f"Asset selection failed: {e}") from e

        logger.info(f"Selection: primary={sel.primary} expanded={sel.expanded} "
                    f"shortlist={list(sel.shortlist)}")

        # Pre-warm + (Tue–Fri) compute daily CPR + subscribe.
        cur_fx_start, _ = current_fx_day_anchor(now)
        is_monday_fx_day = cur_fx_start.weekday() == 6      # Mon FX day starts on Sunday → weekday 6

        for sym in sel.shortlist:
            weekly = weekly_cprs[sym]
            if is_monday_fx_day:
                daily = weekly
            else:
                pds, pde = prior_fx_day_window(now)
                pday_bars = await self.ibkr.fetch_5min_bars(sym, end_ny=pde, duration_str="2 D")
                pday_in_window = self._filter_window(pday_bars, pds, pde)
                if not pday_in_window:
                    logger.warning(f"{sym}: no prior-FX-day bars; falling back to weekly CPR")
                    daily = weekly
                else:
                    daily = compute_cpr_from_bars(
                        [b.high for b in pday_in_window],
                        [b.low for b in pday_in_window],
                        [b.close for b in pday_in_window],
                    )

            # Pre-warm EMA with last EMA_PREWARM_BARS bars before now.
            warm_bars = await self.ibkr.fetch_5min_bars(
                sym, end_ny=now, duration_str="3 D"
            )
            warm_closes = [b.close for b in warm_bars[-EMA_PREWARM_BARS:]]
            ps = _PairState(
                symbol=sym,
                accounts=list(self.active_accounts.keys()),
                weekly_cpr=weekly,
                daily_cpr=daily,
            )
            ps.ema.warmup(warm_closes)
            for acct in self.active_accounts.keys():
                ps.positions[acct] = None
                ps.halted[acct] = False
            ps.last_fx_day_start = cur_fx_start
            self.pair_states[sym] = ps
            ema_str = f"{ps.ema.value:.5f}" if ps.ema.value is not None else "None"
            logger.info(
                f"{sym}: weekly_TC={weekly.tc:.5f} weekly_BC={weekly.bc:.5f} "
                f"daily_TC={daily.tc:.5f} daily_BC={daily.bc:.5f} "
                f"EMA={ema_str} (prewarm={len(warm_closes)} bars)"
            )

        # Subscribe to streaming 5-min bars per shortlisted pair.
        for sym in sel.shortlist:
            ps = self.pair_states[sym]
            ps.bars_handle = await self.ibkr.stream_5min_bars(
                sym, on_update=self._make_bar_handler(sym)
            )
            logger.info(f"{sym}: subscribed to streaming 5-min bars")

    # ───────────────────────── teardown ─────────────────────────
    async def _teardown_streams(self) -> None:
        for sym, ps in self.pair_states.items():
            if ps.bars_handle is not None:
                self.ibkr.cancel_stream(ps.bars_handle)
                ps.bars_handle = None
        # Also force-exit any open positions before leaving the zone.
        for sym, ps in self.pair_states.items():
            for acct, pos in ps.positions.items():
                if pos is not None:
                    await self._exit_position(ps, acct, reason="ZONE_EXIT", current_price=pos.entry_price)
        self.pair_states.clear()

    # ───────────────────────── bar handler factory ─────────────────────────
    def _make_bar_handler(self, sym: str):
        def handler(bars, hasNewBar):
            # Wake the main loop so it can react.
            self._bar_evt.set()
            if not hasNewBar or len(bars) < 2:
                return
            # The just-closed bar is the second-to-last (the last is still updating).
            closed = bars[-2]
            # Coerce timestamp to NY tz-aware
            ts = closed.date
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)
            ts_ny = to_ny(ts)
            asyncio.create_task(self._on_closed_bar(sym, ts_ny, closed))
        return handler

    # ───────────────────────── per-bar logic ─────────────────────────
    async def _on_closed_bar(self, sym: str, open_ny: datetime, bar) -> None:
        ps = self.pair_states.get(sym)
        if ps is None:
            return
        if ps.last_processed_open_ny == open_ny:
            return                              # de-dupe
        ps.last_processed_open_ny = open_ny
        close = float(bar.close)
        bar_close_ny = open_ny + timedelta(minutes=5)

        # Update EMA on every closed bar.
        ema_val = ps.ema.update(close)

        # Detect FX-day rollover.
        bar_fx_start, _ = current_fx_day_anchor(open_ny)
        if ps.last_fx_day_start is None:
            ps.last_fx_day_start = bar_fx_start
        elif bar_fx_start != ps.last_fx_day_start:
            # Rolled over.
            await self._on_fx_day_rollover(ps, bar_fx_start)
            ps.last_fx_day_start = bar_fx_start

        # Update PnL with new price.
        self.pnl.update_price(sym, close)

        # Per-account: arm trail, run exit precedence, then entry trigger.
        for acct in ps.accounts:
            await self._eval_account_on_bar(ps, acct, bar_close_ny, close, ema_val)

    async def _eval_account_on_bar(
        self, ps: _PairState, acct: str, bar_close_ny: datetime,
        close: float, ema_val: Optional[float]
    ) -> None:
        if ps.halted[acct] and ps.positions[acct] is None:
            return

        balance = self.active_accounts[acct]
        arm_threshold = balance * TRAIL_ARM_PCT / 100.0
        trade_loss_cap = balance * self.config.per_trade_loss_pct / 100.0
        day_loss_cap = balance * self.config.per_day_loss_pct / 100.0

        pos = ps.positions[acct]
        if pos is not None:
            unreal = self.pnl.trade_pnl(acct, ps.symbol)
            if not pos.trail_armed and unreal >= arm_threshold:
                pos.trail_armed = True
                logger.info(
                    f"[{acct}] {ps.symbol} TRAIL_ARMED  side={pos.side} "
                    f"entry={pos.entry_price:.5f} now={close:.5f} unreal=${unreal:.2f}"
                )
                self._save_state()
        else:
            unreal = 0.0

        # 1. EOD 16:55
        if pos is not None and bar_close_ny.time() == EOD_TIME:
            await self._exit_position(ps, acct, "EOD_EXIT", close)
            return

        # 2. Daily breaker
        running_day_pnl = self.pnl.day_pnl(acct)
        if not ps.halted[acct] and running_day_pnl <= -day_loss_cap:
            if pos is not None:
                logger.warning(f"[{acct}] DAILY_BREAKER tripped (day_pnl=${running_day_pnl:.2f})")
                await self._exit_position(ps, acct, "DAILY_BREAKER", close)
            ps.halted[acct] = True
            self._save_state()
            return

        # 3. Per-trade SL
        if pos is not None and unreal <= -trade_loss_cap:
            logger.warning(f"[{acct}] {ps.symbol} SL_HIT unreal=${unreal:.2f}")
            await self._exit_position(ps, acct, "SL_HIT", close)
            pos = None
            unreal = 0.0
            # fall through to potentially re-enter on this same bar

        # 4. Trail
        pos = ps.positions[acct]
        if pos is not None and pos.trail_armed and ema_val is not None:
            crossed = (
                (pos.side == "LONG" and close < ema_val)
                or (pos.side == "SHORT" and close > ema_val)
            )
            if crossed:
                logger.info(f"[{acct}] {ps.symbol} TRAIL_EXIT close={close:.5f} ema={ema_val:.5f}")
                await self._exit_position(ps, acct, "TRAIL_EXIT", close)

        # 5. Entry trigger (or reversal)
        if ps.halted[acct]:
            return
        await self._maybe_enter(ps, acct, bar_close_ny, close)

    # ───────────────────────── entry trigger ─────────────────────────
    async def _maybe_enter(self, ps: _PairState, acct: str,
                            bar_close_ny: datetime, close: float) -> None:
        cpr = ps.daily_cpr
        pct = self.config.entry_trigger_range_pct / 100.0
        upper = cpr.tc + cpr.tc * pct
        lower = cpr.bc - cpr.bc * pct
        in_long = cpr.tc < close <= upper
        in_short = lower <= close < cpr.bc
        if not (in_long or in_short):
            return

        pos = ps.positions[acct]
        if in_long:
            if pos is None:
                await self._open_position(ps, acct, "LONG", close, bar_close_ny)
            elif pos.side == "SHORT":
                await self._exit_position(ps, acct, "REVERSE", close)
                await self._open_position(ps, acct, "LONG", close, bar_close_ny)
            # already long → ignore
        elif in_short:
            if pos is None:
                await self._open_position(ps, acct, "SHORT", close, bar_close_ny)
            elif pos.side == "LONG":
                await self._exit_position(ps, acct, "REVERSE", close)
                await self._open_position(ps, acct, "SHORT", close, bar_close_ny)

    async def _open_position(self, ps: _PairState, acct: str,
                              side: str, price: float, t: datetime) -> None:
        ibkr_side = "BUY" if side == "LONG" else "SELL"
        await self.ibkr.place_market_order(acct, ps.symbol, ibkr_side, LOT_SIZE)
        ps.positions[acct] = _Position(side=side, entry_price=price, entry_time=t)
        self.pnl.on_entry(acct, ps.symbol, side, price, LOT_UNITS)
        logger.info(f"[{acct}] {ps.symbol} OPEN_{side} @{price:.5f} (CPR TC={ps.daily_cpr.tc:.5f} BC={ps.daily_cpr.bc:.5f})")
        self._save_state()

    async def _exit_position(self, ps: _PairState, acct: str,
                              reason: str, current_price: float) -> None:
        pos = ps.positions.get(acct)
        if pos is None:
            return
        ibkr_side = "SELL" if pos.side == "LONG" else "BUY"
        await self.ibkr.place_market_order(acct, ps.symbol, ibkr_side, LOT_SIZE)
        realized = self.pnl.on_exit(acct, ps.symbol)
        logger.info(
            f"[{acct}] {ps.symbol} {reason}  side={pos.side} "
            f"entry={pos.entry_price:.5f} → {current_price:.5f} realized≈${realized:.2f}"
        )
        ps.positions[acct] = None
        self._save_state()

    # ───────────────────────── FX day rollover ─────────────────────────
    async def _on_fx_day_rollover(self, ps: _PairState, new_fx_start: datetime) -> None:
        # Recompute daily CPR from prior FX day.
        prior_start = new_fx_start - timedelta(days=1)
        prior_bars = await self.ibkr.fetch_5min_bars(
            ps.symbol, end_ny=new_fx_start, duration_str="2 D"
        )
        in_window = self._filter_window(prior_bars, prior_start, new_fx_start)
        if in_window:
            ps.daily_cpr = compute_cpr_from_bars(
                [b.high for b in in_window],
                [b.low for b in in_window],
                [b.close for b in in_window],
            )
            logger.info(
                f"{ps.symbol} ROLLOVER → new FX day {new_fx_start.strftime('%a %m-%d')}: "
                f"daily CPR TC={ps.daily_cpr.tc:.5f} BC={ps.daily_cpr.bc:.5f}"
            )
        # Reset per-day state for all accounts.
        for acct in ps.accounts:
            self.pnl.reset_day(acct)
            ps.halted[acct] = False
        self._save_state()

        # If we crossed a Sunday boundary, re-run weekly selection too. (Skipped in v1
        # for simplicity — the next session bootstrap handles this on next zone re-entry.)

    # ───────────────────────── helpers ─────────────────────────
    @staticmethod
    def _filter_window(bars, start_ny: datetime, end_ny: datetime):
        out = []
        for b in bars:
            ts = b.date
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)
            ts_ny = to_ny(ts)
            if start_ny <= ts_ny < end_ny:
                out.append(b)
        return out
