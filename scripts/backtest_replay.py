"""
Backtest replay against live IBKR data.

Replays Mon Apr 27 → Fri May 1 2026 5-min candles through the entire rule set:
  * Asset selection (using weekly CPR from Apr 19–24 as the source window)
  * Mon = weekly CPR; Tue–Fri = daily CPR from prior FX day
  * Entry trigger band per §10
  * Position state: one trade per pair; reversal on opposite trigger (§10.5)
  * Trail arm at 0.5% × frozen balance, exit on 50-EMA cross (§11.4)
  * Per-trade SL at -per_trade_loss_pct% × frozen balance (§11.2)
  * Per-day circuit breaker at -per_day_loss_pct% × frozen balance (§11.2)
  * EOD forced exit on the 16:50→16:55 candle (§11.1)

NO ORDERS PLACED. Read-only IBKR session. Approximate P&L (forex pip math is
simplified; this backtest is for *rule-shape sanity*, not for accurate P&L).
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB, Forex   # type: ignore[import-untyped]

from config import DEFAULT_SYMBOLS
from cpr import CPR, compute_cpr_from_bars
from indicators import EMA
from selection import select_shortlist
from time_utils import NY, current_fx_day_anchor, to_ny


# ─── Backtest config ────────────────────────────────────────────────────
ALLOWED_CURRENCIES = ["USD"]
ENTRY_TRIGGER_RANGE_PCT = 0.05    # SPEC default
PER_TRADE_LOSS_PCT = 1.0          # 1% — illustrative
PER_DAY_LOSS_PCT = 2.0            # 2% — illustrative
TRAIL_ARM_PCT = 0.5               # SPEC §11.4 hardcoded
EMA_PERIOD = 50
LOT_SIZE = 0.25                   # SPEC §12 fixed
LOT_UNITS = LOT_SIZE * 100_000    # 25,000 base-currency units

# Approx using user's actual sub-account balance ($10,002 from live test)
FROZEN_BALANCE = 10_000.0

# Replay windows
SOURCE_WEEK_START = datetime(2026, 4, 19, 17, 0, tzinfo=NY)   # Sun Apr 19 17:00 NY
SOURCE_WEEK_END = datetime(2026, 4, 24, 17, 0, tzinfo=NY)     # Fri Apr 24 17:00 NY
REPLAY_START = datetime(2026, 4, 26, 17, 0, tzinfo=NY)        # Sun Apr 26 17:00 (= Mon FX day)
REPLAY_END = datetime(2026, 5, 1, 17, 0, tzinfo=NY)           # Fri May 1 17:00 (= end of Fri FX day)

# IBKR
HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 42


# ─── Approximate P&L helper ─────────────────────────────────────────────
def approx_pnl_usd(symbol: str, entry_price: float, current_price: float,
                   side: str, lot_units: float = LOT_UNITS) -> float:
    """
    Rough USD P&L for a 0.25 lot forex position.

    For pairs ending in USD: exact (price diff × units).
    For pairs ending in JPY/CHF/CAD: divides by an approximate quote-to-USD
    rate. Magnitude is right; not exact for accounting.
    """
    direction = 1.0 if side == "LONG" else -1.0
    diff = (current_price - entry_price) * direction
    quote = symbol[3:].upper()
    if quote == "USD":
        return diff * lot_units
    if quote == "JPY":
        # Profit in JPY → USD: ÷ JPY-per-USD rate (~150)
        return diff * lot_units / current_price
    if quote == "CHF":
        return diff * lot_units / 0.78   # CHF-per-USD ~0.78
    if quote == "CAD":
        return diff * lot_units / 1.37   # CAD-per-USD ~1.37
    if quote == "GBP":
        return diff * lot_units * 1.27   # 1 GBP = 1.27 USD
    return diff * lot_units              # fallback


# ─── Position dataclass ─────────────────────────────────────────────────
@dataclass
class Position:
    side: str          # "LONG" or "SHORT"
    entry_price: float
    entry_time: datetime
    trail_armed: bool = False


# ─── Bar fetching ───────────────────────────────────────────────────────
@dataclass
class Bar:
    open_ny: datetime   # bar OPEN time (5-min boundary)
    open: float
    high: float
    low: float
    close: float

    @property
    def close_ny(self) -> datetime:
        return self.open_ny + timedelta(minutes=5)


async def fetch_bars(ib: IB, symbol: str, end_ny: datetime,
                     duration_str: str = "14 D") -> List[Bar]:
    contract = (await ib.qualifyContractsAsync(Forex(symbol)))[0]
    end_utc = end_ny.astimezone(timezone.utc)
    raw = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime=end_utc,
        durationStr=duration_str,
        barSizeSetting="5 mins",
        whatToShow="MIDPOINT",
        useRTH=False,
        formatDate=2,
    )
    out = []
    for b in raw:
        ts = b.date if b.date.tzinfo else b.date.replace(tzinfo=timezone.utc)
        out.append(Bar(
            open_ny=to_ny(ts),
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
        ))
    return out


# ─── CPR helpers ────────────────────────────────────────────────────────
def cpr_from_window(bars: List[Bar], start_ny: datetime,
                    end_ny: datetime) -> Optional[CPR]:
    in_window = [b for b in bars if start_ny <= b.open_ny < end_ny]
    if not in_window:
        return None
    return compute_cpr_from_bars(
        [b.high for b in in_window],
        [b.low for b in in_window],
        [b.close for b in in_window],
    )


# ─── Replay engine ──────────────────────────────────────────────────────
@dataclass
class ReplayLog:
    entries: List[tuple]                # (ts, event, message)
    pair: str
    total_pnl: float = 0.0
    realized_trades: int = 0
    eod_exits: int = 0
    sl_hits: int = 0
    trail_exits: int = 0
    reversals: int = 0
    breaker_trips: int = 0


def replay_pair(symbol: str, bars: List[Bar], weekly_cpr: CPR,
                replay_start: datetime, replay_end: datetime) -> ReplayLog:
    """
    Replay one pair through the full strategy rule set.
    Mon (in replay window): uses weekly_cpr as the trading CPR.
    Tue-Fri: recomputes daily CPR at each 17:00 NY rollover.
    """
    log = ReplayLog(entries=[], pair=symbol)

    # Pre-warm 50-EMA with bars BEFORE replay_start (use last 250+ closes).
    ema = EMA(EMA_PERIOD)
    pre_bars = [b for b in bars if b.open_ny < replay_start]
    for b in pre_bars[-300:]:
        ema.update(b.close)
    ema_str = f"{ema.value:.5f}" if ema.value is not None else "None"
    log.entries.append((
        replay_start, "PRE_WARM",
        f"{symbol}: EMA pre-warm with {min(300, len(pre_bars))} bars → ema={ema_str}",
    ))

    pct = ENTRY_TRIGGER_RANGE_PCT / 100
    arm_threshold = FROZEN_BALANCE * TRAIL_ARM_PCT / 100
    trade_loss_cap = FROZEN_BALANCE * PER_TRADE_LOSS_PCT / 100
    day_loss_cap = FROZEN_BALANCE * PER_DAY_LOSS_PCT / 100

    pos: Optional[Position] = None
    daily_cpr = weekly_cpr   # Monday's trading CPR is the weekly CPR.
    daily_pnl = 0.0
    halted_for_day = False
    last_fx_day_start: Optional[datetime] = None

    EOD = time(16, 55)

    replay_bars = [b for b in bars if replay_start <= b.open_ny < replay_end]

    for b in replay_bars:
        # Update EMA every bar (continuous indicator).
        ema_val = ema.update(b.close)

        # Detect FX-day rollover. Each bar belongs to FX day = current_fx_day_anchor(open_time).
        bar_fx_start, _ = current_fx_day_anchor(b.open_ny)

        if last_fx_day_start is None:
            last_fx_day_start = bar_fx_start
        elif bar_fx_start != last_fx_day_start:
            # Crossed into a new FX day → recompute daily CPR from prior FX day.
            prior_start = bar_fx_start - timedelta(days=1)
            new_cpr = cpr_from_window(bars, prior_start, bar_fx_start)
            if new_cpr is not None:
                daily_cpr = new_cpr
                log.entries.append((
                    b.open_ny, "ROLLOVER",
                    f"new FX day {bar_fx_start.strftime('%a %m-%d')}: "
                    f"daily CPR TC={new_cpr.tc:.5f} BC={new_cpr.bc:.5f} "
                    f"width%={new_cpr.width_pct:.4f}",
                ))
            last_fx_day_start = bar_fx_start
            daily_pnl = 0.0
            halted_for_day = False

        # If halted for the day → skip everything except the EOD check.
        # Actually: if halted, we don't take new trades. Existing positions
        # were already closed on the breaker trip.
        if halted_for_day and pos is None:
            continue

        # ── Compute current unrealized for arm/SL/breaker decisions ──
        unreal = 0.0
        if pos is not None:
            unreal = approx_pnl_usd(symbol, pos.entry_price, b.close, pos.side)
            # Arm trail when threshold crossed.
            if not pos.trail_armed and unreal >= arm_threshold:
                pos.trail_armed = True
                log.entries.append((
                    b.close_ny, "TRAIL_ARMED",
                    f"{pos.side} entry={pos.entry_price:.5f} now={b.close:.5f} "
                    f"unreal=${unreal:.2f}",
                ))

        # ── EXIT PRECEDENCE (§11.5) ──
        # 1. EOD 16:55 NY
        if pos is not None and b.close_ny.time() == EOD:
            daily_pnl += unreal
            log.total_pnl += unreal
            log.entries.append((
                b.close_ny, "EOD_EXIT",
                f"{pos.side} {pos.entry_price:.5f} → {b.close:.5f} pnl=${unreal:.2f}",
            ))
            log.eod_exits += 1
            log.realized_trades += 1
            pos = None
            continue   # last bar of FX day; nothing else to do

        # 2. Daily breaker
        running_day_pnl = daily_pnl + unreal
        if not halted_for_day and running_day_pnl <= -day_loss_cap:
            if pos is not None:
                daily_pnl += unreal
                log.total_pnl += unreal
                log.entries.append((
                    b.close_ny, "DAILY_BREAKER",
                    f"day pnl=${running_day_pnl:.2f} ≤ -${day_loss_cap:.2f}; "
                    f"closing {pos.side} pnl=${unreal:.2f}",
                ))
                log.breaker_trips += 1
                log.realized_trades += 1
                pos = None
            halted_for_day = True
            continue

        # 3. Per-trade SL
        if pos is not None and unreal <= -trade_loss_cap:
            daily_pnl += unreal
            log.total_pnl += unreal
            log.entries.append((
                b.close_ny, "SL_HIT",
                f"{pos.side} unreal=${unreal:.2f} ≤ -${trade_loss_cap:.2f}",
            ))
            log.sl_hits += 1
            log.realized_trades += 1
            pos = None
            continue

        # 4. Trail (armed + EMA crossed strictly)
        if pos is not None and pos.trail_armed and ema_val is not None:
            crossed = (
                (pos.side == "LONG" and b.close < ema_val)
                or (pos.side == "SHORT" and b.close > ema_val)
            )
            if crossed:
                daily_pnl += unreal
                log.total_pnl += unreal
                log.entries.append((
                    b.close_ny, "TRAIL_EXIT",
                    f"{pos.side} close={b.close:.5f} crossed EMA={ema_val:.5f} "
                    f"pnl=${unreal:.2f}",
                ))
                log.trail_exits += 1
                log.realized_trades += 1
                pos = None
                # fall through to entry check (could re-enter immediately)

        # 5. Entry trigger / reversal
        if halted_for_day:
            continue

        upper_band = daily_cpr.tc + daily_cpr.tc * pct
        lower_band = daily_cpr.bc - daily_cpr.bc * pct
        in_long = daily_cpr.tc < b.close <= upper_band
        in_short = lower_band <= b.close < daily_cpr.bc

        if in_long:
            if pos is None:
                pos = Position("LONG", b.close, b.close_ny)
                log.entries.append((
                    b.close_ny, "OPEN_LONG",
                    f"close={b.close:.5f} TC={daily_cpr.tc:.5f} (band ≤ {upper_band:.5f})",
                ))
            elif pos.side == "SHORT":
                # Reversal: close + open
                exit_pnl = approx_pnl_usd(symbol, pos.entry_price, b.close, "SHORT")
                daily_pnl += exit_pnl
                log.total_pnl += exit_pnl
                log.entries.append((
                    b.close_ny, "REVERSE",
                    f"close SHORT {pos.entry_price:.5f}→{b.close:.5f} pnl=${exit_pnl:.2f}; "
                    f"open LONG @{b.close:.5f}",
                ))
                log.reversals += 1
                log.realized_trades += 1
                pos = Position("LONG", b.close, b.close_ny)
            # else: already long → ignore
        elif in_short:
            if pos is None:
                pos = Position("SHORT", b.close, b.close_ny)
                log.entries.append((
                    b.close_ny, "OPEN_SHORT",
                    f"close={b.close:.5f} BC={daily_cpr.bc:.5f} (band ≥ {lower_band:.5f})",
                ))
            elif pos.side == "LONG":
                exit_pnl = approx_pnl_usd(symbol, pos.entry_price, b.close, "LONG")
                daily_pnl += exit_pnl
                log.total_pnl += exit_pnl
                log.entries.append((
                    b.close_ny, "REVERSE",
                    f"close LONG {pos.entry_price:.5f}→{b.close:.5f} pnl=${exit_pnl:.2f}; "
                    f"open SHORT @{b.close:.5f}",
                ))
                log.reversals += 1
                log.realized_trades += 1
                pos = Position("SHORT", b.close, b.close_ny)

    # End of replay window — if still open, mark.
    if pos is not None:
        log.entries.append((
            replay_end, "STILL_OPEN_AT_END",
            f"{pos.side} entry={pos.entry_price:.5f} (would have closed at next 16:55)",
        ))

    return log


# ─── Main ───────────────────────────────────────────────────────────────
async def main() -> int:
    print("=" * 80)
    print("forex_cpr_ibkr — backtest replay")
    print(f"  Source week (weekly CPR): {SOURCE_WEEK_START.strftime('%a %Y-%m-%d')} 17:00 NY → {SOURCE_WEEK_END.strftime('%a %Y-%m-%d')} 17:00 NY")
    print(f"  Replay week:              {REPLAY_START.strftime('%a %Y-%m-%d')} 17:00 NY → {REPLAY_END.strftime('%a %Y-%m-%d')} 17:00 NY")
    print(f"  allowed_currencies={ALLOWED_CURRENCIES} entry_pct={ENTRY_TRIGGER_RANGE_PCT}% "
          f"trade_loss={PER_TRADE_LOSS_PCT}% day_loss={PER_DAY_LOSS_PCT}% "
          f"trail_arm={TRAIL_ARM_PCT}% balance=${FROZEN_BALANCE:.0f} lot={LOT_SIZE}")
    print("=" * 80)

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1

    try:
        # Fetch 14 days of bars for all 15 pairs
        print(f"\nFetching 14 days of 5-min bars for {len(DEFAULT_SYMBOLS)} pairs...")
        bars_per_pair: Dict[str, List[Bar]] = {}
        for sym in DEFAULT_SYMBOLS:
            bars_per_pair[sym] = await fetch_bars(ib, sym, REPLAY_END, "14 D")
            print(f"  {sym}: {len(bars_per_pair[sym])} bars")

        # Compute weekly CPR from source window
        print(f"\nComputing weekly CPR (source: {SOURCE_WEEK_START.date()} → {SOURCE_WEEK_END.date()})...")
        weekly_cprs: Dict[str, CPR] = {}
        for sym, bars in bars_per_pair.items():
            cpr = cpr_from_window(bars, SOURCE_WEEK_START, SOURCE_WEEK_END)
            if cpr is not None:
                weekly_cprs[sym] = cpr

        # Rank by width %
        ranked = sorted(weekly_cprs.items(), key=lambda kv: kv[1].width_pct)
        print("\nWeekly CPR width % (narrowest first):")
        for sym, c in ranked[:5]:
            print(f"  {sym}: width%={c.width_pct:.4f}  TC={c.tc:.5f}  BC={c.bc:.5f}")
        print(f"  ... ({len(ranked) - 5} more)")

        # Run selection
        result = select_shortlist(DEFAULT_SYMBOLS, ALLOWED_CURRENCIES, weekly_cprs)
        print(f"\nSelection: primary={result.primary} expanded={result.expanded} "
              f"shortlist={list(result.shortlist)}")

        # Replay each shortlisted pair
        for sym in result.shortlist:
            print("\n" + "=" * 80)
            print(f"REPLAY: {sym}")
            print(f"  Mon's trading CPR (= weekly CPR): TC={weekly_cprs[sym].tc:.5f} "
                  f"BC={weekly_cprs[sym].bc:.5f} width%={weekly_cprs[sym].width_pct:.4f}")
            print("=" * 80)

            log = replay_pair(sym, bars_per_pair[sym], weekly_cprs[sym],
                              REPLAY_START, REPLAY_END)

            # Print events
            for ts, evt, msg in log.entries:
                print(f"  {ts.strftime('%a %m-%d %H:%M'):<16} {evt:<18} {msg}")

            # Summary
            print()
            print(f"  ─── SUMMARY {sym} ───")
            print(f"    Total events       : {len(log.entries)}")
            print(f"    Realized trades    : {log.realized_trades}")
            print(f"    EOD exits          : {log.eod_exits}")
            print(f"    Per-trade SL hits  : {log.sl_hits}")
            print(f"    Trail exits        : {log.trail_exits}")
            print(f"    Reversals          : {log.reversals}")
            print(f"    Daily breaker trips: {log.breaker_trips}")
            print(f"    Approx total P&L   : ${log.total_pnl:.2f}  "
                  f"({log.total_pnl / FROZEN_BALANCE * 100:.2f}% of frozen balance)")

    finally:
        ib.disconnect()
    print("\nBacktest complete. (Disconnected.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
