"""
Replay the strategy across THIS week so far (Mon FX day → now) and write a
result file under reports/.

  - Source week (weekly CPR window):  Sun Apr 26 17:00 NY → Fri May 1 17:00 NY
  - Replay window:                    Sun May 3 17:00 NY → now (Tue May 5)
  - Read-only IBKR session, paper gateway (4002).

Approximate P&L only (forex pip-math is simplified). For rule-shape sanity,
not for accounting accuracy.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB, Forex   # type: ignore[import-untyped]

from config import DEFAULT_SYMBOLS
from cpr import CPR, compute_cpr_from_bars
from indicators import EMA
from selection import select_shortlist
from time_utils import NY, current_fx_day_anchor, ny_now, to_ny


# ─── Config (mirrors strategy + scripts/backtest_replay.py) ──────────────
ALLOWED_CURRENCIES = ["USD"]
ENTRY_TRIGGER_RANGE_PCT = 0.05
PER_TRADE_LOSS_PCT = 0.33
PER_DAY_LOSS_PCT = 1.0
TRAIL_ARM_PCT = 0.5
EMA_PERIOD = 50
LOT_SIZE = 0.20                      # 20k units — at/above IDEALPRO threshold
LOT_UNITS = LOT_SIZE * 100_000       # 20,000 base ccy units
FROZEN_BALANCE = 10_000.0            # smaller of the two real subs (U25272450)

# Windows
SOURCE_WEEK_START = datetime(2026, 4, 26, 17, 0, tzinfo=NY)   # Sun Apr 26 17:00 NY
SOURCE_WEEK_END   = datetime(2026, 5, 1, 17, 0, tzinfo=NY)    # Fri May 1 17:00 NY
REPLAY_START      = datetime(2026, 5, 3, 17, 0, tzinfo=NY)    # Sun May 3 17:00 NY
REPLAY_END        = ny_now()                                  # truncate to now
# Snap to the most recent 5-min boundary just before now, so we don't read
# a half-formed bar.
_minute = (REPLAY_END.minute // 5) * 5
REPLAY_END = REPLAY_END.replace(minute=_minute, second=0, microsecond=0)

# IBKR (live, read-only — backtest never places orders)
HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 60

OUT_DIR = ROOT / "reports"


# ─── P&L helper (approximate) ────────────────────────────────────────────
def approx_pnl_usd(symbol: str, entry_price: float, current_price: float,
                   side: str, lot_units: float = LOT_UNITS) -> float:
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
    return diff * lot_units


# ─── Data classes ────────────────────────────────────────────────────────
@dataclass
class Position:
    side: str
    entry_price: float
    entry_time: datetime
    trail_armed: bool = False


@dataclass
class Bar:
    open_ny: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def close_ny(self) -> datetime:
        return self.open_ny + timedelta(minutes=5)


@dataclass
class ReplayLog:
    pair: str
    entries: List[tuple] = field(default_factory=list)
    total_pnl: float = 0.0
    realized_trades: int = 0
    eod_exits: int = 0
    sl_hits: int = 0
    trail_exits: int = 0
    reversals: int = 0
    breaker_trips: int = 0


# ─── Bar fetching ────────────────────────────────────────────────────────
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


# ─── Replay engine ───────────────────────────────────────────────────────
def replay_pair(symbol: str, bars: List[Bar], weekly_cpr: CPR,
                replay_start: datetime, replay_end: datetime) -> ReplayLog:
    log = ReplayLog(pair=symbol)

    # Pre-warm 50-EMA with bars before replay_start.
    ema = EMA(EMA_PERIOD)
    pre_bars = [b for b in bars if b.open_ny < replay_start]
    for b in pre_bars[-300:]:
        ema.update(b.close)
    ema_str = f"{ema.value:.5f}" if ema.value is not None else "None"
    log.entries.append((
        replay_start, "PRE_WARM",
        f"EMA pre-warm with {min(300, len(pre_bars))} bars → ema={ema_str}",
    ))

    pct = ENTRY_TRIGGER_RANGE_PCT / 100
    arm_threshold = FROZEN_BALANCE * TRAIL_ARM_PCT / 100
    trade_loss_cap = FROZEN_BALANCE * PER_TRADE_LOSS_PCT / 100
    day_loss_cap = FROZEN_BALANCE * PER_DAY_LOSS_PCT / 100

    pos: Optional[Position] = None
    daily_cpr = weekly_cpr
    daily_pnl = 0.0
    halted_for_day = False
    last_fx_day_start: Optional[datetime] = None

    EOD = time(16, 55)
    replay_bars = [b for b in bars if replay_start <= b.open_ny < replay_end]

    for b in replay_bars:
        ema_val = ema.update(b.close)
        bar_fx_start, _ = current_fx_day_anchor(b.open_ny)

        if last_fx_day_start is None:
            last_fx_day_start = bar_fx_start
        elif bar_fx_start != last_fx_day_start:
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

        if halted_for_day and pos is None:
            continue

        unreal = 0.0
        if pos is not None:
            unreal = approx_pnl_usd(symbol, pos.entry_price, b.close, pos.side)
            if not pos.trail_armed and unreal >= arm_threshold:
                pos.trail_armed = True
                log.entries.append((
                    b.close_ny, "TRAIL_ARMED",
                    f"{pos.side} entry={pos.entry_price:.5f} now={b.close:.5f} "
                    f"unreal=${unreal:.2f}",
                ))

        # 1. EOD exit
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
            continue

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

        # 4. Trail exit (EMA cross after armed)
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

        if halted_for_day:
            continue

        # 5. Entry trigger / reversal
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

    if pos is not None:
        unreal = approx_pnl_usd(symbol, pos.entry_price, replay_bars[-1].close, pos.side)
        log.entries.append((
            replay_end, "STILL_OPEN",
            f"{pos.side} entry={pos.entry_price:.5f} mark-to-market unreal=${unreal:.2f} "
            f"(would close at next 16:55)",
        ))

    return log


# ─── Output ──────────────────────────────────────────────────────────────
def write_report(out: TextIO, ranked: List, shortlist: List[str],
                 weekly_cprs: Dict[str, CPR], logs: List[ReplayLog]) -> None:
    p = lambda *a, **k: print(*a, **k, file=out)
    bar = "=" * 80

    p(bar)
    p("forex_cpr_ibkr — THIS WEEK BACKTEST (replay)")
    p(bar)
    p(f"Generated:        {datetime.now(timezone.utc).isoformat()}Z")
    p(f"Source week:      {SOURCE_WEEK_START.strftime('%a %Y-%m-%d %H:%M')} NY → "
      f"{SOURCE_WEEK_END.strftime('%a %Y-%m-%d %H:%M')} NY")
    p(f"Replay window:    {REPLAY_START.strftime('%a %Y-%m-%d %H:%M')} NY → "
      f"{REPLAY_END.strftime('%a %Y-%m-%d %H:%M')} NY  "
      f"({(REPLAY_END - REPLAY_START).total_seconds() / 3600:.1f} hours of market time)")
    p(f"Allowed ccy:      {ALLOWED_CURRENCIES}")
    p(f"Trigger band:     ±{ENTRY_TRIGGER_RANGE_PCT}% of TC/BC")
    p(f"Per-trade SL:     {PER_TRADE_LOSS_PCT}% of frozen balance")
    p(f"Per-day breaker:  {PER_DAY_LOSS_PCT}% of frozen balance")
    p(f"Trail arm:        {TRAIL_ARM_PCT}% of frozen balance")
    p(f"EMA period:       {EMA_PERIOD}")
    p(f"Lot size:         {LOT_SIZE} ({LOT_UNITS:.0f} units)")
    p(f"Frozen balance:   ${FROZEN_BALANCE:,.0f}  (per-account)")
    p()

    p("─── Weekly CPR (narrowest 5) ───")
    for sym, c in ranked[:5]:
        p(f"  {sym:<7} width%={c.width_pct:.4f}  TC={c.tc:.5f}  BC={c.bc:.5f}")
    p(f"  ... ({len(ranked) - 5} more)")
    p()
    p(f"Selection shortlist: {shortlist}")
    p()

    grand_pnl = 0.0
    grand_trades = 0
    for log in logs:
        grand_pnl += log.total_pnl
        grand_trades += log.realized_trades

    p(bar)
    p(f"OVERALL — across {len(logs)} pair(s)")
    p(bar)
    p(f"  Realized trades : {grand_trades}")
    p(f"  Total approx P&L: ${grand_pnl:+.2f}  "
      f"({grand_pnl / FROZEN_BALANCE * 100:+.2f}% of frozen balance)")
    p()

    for log in logs:
        p(bar)
        p(f"PAIR {log.pair}")
        p(bar)
        p(f"  Mon trading CPR: TC={weekly_cprs[log.pair].tc:.5f} "
          f"BC={weekly_cprs[log.pair].bc:.5f} "
          f"width%={weekly_cprs[log.pair].width_pct:.4f}")
        p()
        p(f"  ─── Events ({len(log.entries)}) ───")
        for ts, evt, msg in log.entries:
            p(f"  {ts.strftime('%a %m-%d %H:%M'):<16} {evt:<18} {msg}")
        p()
        p(f"  ─── Summary ───")
        p(f"    Realized trades    : {log.realized_trades}")
        p(f"    EOD exits          : {log.eod_exits}")
        p(f"    Per-trade SL hits  : {log.sl_hits}")
        p(f"    Trail exits        : {log.trail_exits}")
        p(f"    Reversals          : {log.reversals}")
        p(f"    Daily breaker trips: {log.breaker_trips}")
        p(f"    Approx total P&L   : ${log.total_pnl:+.2f}  "
          f"({log.total_pnl / FROZEN_BALANCE * 100:+.2f}% of frozen balance)")
        p()


# ─── Main ────────────────────────────────────────────────────────────────
async def main() -> int:
    print(f"Connecting to paper gateway {HOST}:{PORT} ...")
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1

    try:
        # Need bars from BEFORE source week through replay_end.
        # 14 days from REPLAY_END covers Apr 21 → May 5. That includes the
        # source window (Apr 26→May 1) plus EMA pre-warm room.
        print(f"Fetching 14 days of 5-min bars for {len(DEFAULT_SYMBOLS)} pairs...")
        bars_per_pair: Dict[str, List[Bar]] = {}
        for sym in DEFAULT_SYMBOLS:
            bars_per_pair[sym] = await fetch_bars(ib, sym, REPLAY_END, "14 D")
            print(f"  {sym}: {len(bars_per_pair[sym])} bars")

        print(f"\nComputing weekly CPR (source: {SOURCE_WEEK_START.date()} → {SOURCE_WEEK_END.date()})...")
        weekly_cprs: Dict[str, CPR] = {}
        for sym, bars in bars_per_pair.items():
            cpr = cpr_from_window(bars, SOURCE_WEEK_START, SOURCE_WEEK_END)
            if cpr is not None:
                weekly_cprs[sym] = cpr

        ranked = sorted(weekly_cprs.items(), key=lambda kv: kv[1].width_pct)
        result = select_shortlist(DEFAULT_SYMBOLS, ALLOWED_CURRENCIES, weekly_cprs)
        print(f"Selection: primary={result.primary} expanded={result.expanded} "
              f"shortlist={list(result.shortlist)}")

        logs: List[ReplayLog] = []
        for sym in result.shortlist:
            print(f"  replaying {sym} ...")
            log = replay_pair(sym, bars_per_pair[sym], weekly_cprs[sym],
                              REPLAY_START, REPLAY_END)
            logs.append(log)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / (
            f"backtest_this_week_lot{LOT_SIZE}_"
            f"sl{PER_TRADE_LOSS_PCT}_"
            f"day{PER_DAY_LOSS_PCT}_"
            f"{REPLAY_END.strftime('%Y-%m-%d_%H%M')}NY.txt"
        )
        with out_path.open("w", encoding="utf-8") as f:
            write_report(f, ranked, list(result.shortlist), weekly_cprs, logs)
        print(f"\nReport written → {out_path}")

        # Echo summary to stdout
        grand_pnl = sum(l.total_pnl for l in logs)
        grand_trades = sum(l.realized_trades for l in logs)
        print(f"  Pairs replayed: {len(logs)}")
        print(f"  Total trades:   {grand_trades}")
        print(f"  Total P&L:      ${grand_pnl:+.2f} "
              f"({grand_pnl / FROZEN_BALANCE * 100:+.2f}% of $10k)")

        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
