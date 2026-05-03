"""
End-to-end pre-trade smoke against live IBKR.

Chains every module we've built so far against real market data:

  prior_week_window()    →  IBKR 5-min bars for all 15 forex pairs
                         →  filter to exact NY-anchored window
                         →  compute_cpr_from_bars() per pair
                         →  width % per pair (sorted)
                         →  select_shortlist() under several allowed_currencies presets

What it proves:
  * Our NY-anchored prior_week_window agrees with IBKR's returned bar timestamps.
  * All 15 pairs return a sensible bar count for the prior week.
  * Weekly CPR width % is computable for every pair against real data.
  * The asset-selection algorithm picks a sane primary + (when needed) expansion.

NO ORDERS PLACED. Read-only session.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make local modules importable when running from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB, Forex   # type: ignore[import-untyped]

from config import DEFAULT_SYMBOLS
from cpr import compute_cpr_from_bars
from selection import select_shortlist, SelectionError
from time_utils import NY, ny_now, prior_week_window, to_ny


HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 42


async def fetch_window_bars(
    ib: IB,
    contract,
    window_start_ny: datetime,
    window_end_ny: datetime,
):
    """
    Fetch 5-min MIDPOINT bars covering [window_start_ny, window_end_ny) by
    bar OPEN time. We request slightly more than needed and filter in code.
    """
    end_utc = window_end_ny.astimezone(timezone.utc)
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime=end_utc,
        durationStr="1 W",
        barSizeSetting="5 mins",
        whatToShow="MIDPOINT",
        useRTH=False,
        formatDate=2,           # ISO8601 UTC datetime objects
    )
    # Filter to exactly the window by bar OPEN (`bar.date`).
    in_window = []
    for b in bars:
        ts = b.date
        # Coerce to tz-aware: ib_async with formatDate=2 returns aware in UTC,
        # but be defensive in case some entries are naive.
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ny = to_ny(ts)
        if window_start_ny <= ts_ny < window_end_ny:
            in_window.append(b)
    return in_window


async def main() -> int:
    print("=" * 72)
    print("forex_cpr_ibkr — end-to-end pre-trade smoke")
    print("=" * 72)

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1

    try:
        accounts = ib.managedAccounts()
        print(f"\nConnected. Managed accounts: {accounts}")

        # Prior week window per our time math
        now = ny_now()
        ws, we = prior_week_window(now)
        print(f"\nNY now           : {now.isoformat()}")
        print(f"Prior week start : {ws.isoformat()}  (Sun 17:00 NY, 8 days ago)")
        print(f"Prior week end   : {we.isoformat()}  (Fri 17:00 NY, last Fri)")
        print(f"Window length    : {we - ws}")

        # Qualify all 15 forex pairs
        contracts = await ib.qualifyContractsAsync(*[Forex(s) for s in DEFAULT_SYMBOLS])
        print(f"\nQualified {len(contracts)}/15 forex contracts on IDEALPRO.")

        # Fetch + compute weekly CPR per pair (serial, to be polite to IBKR pacing)
        weekly_cprs = {}
        bar_counts = {}
        print("\nFetching last week's 5-min bars and computing weekly CPR...")
        print(f"{'pair':<8} {'bars':>6} {'first_open (NY)':<22} {'last_open (NY)':<22} "
              f"{'H':>10} {'L':>10} {'C':>10} {'TC':>10} {'BC':>10} {'width%':>9}")
        for sym, contract in zip(DEFAULT_SYMBOLS, contracts):
            bars = await fetch_window_bars(ib, contract, ws, we)
            bar_counts[sym] = len(bars)
            if not bars:
                print(f"{sym:<8} {'0':>6}   <no bars in window>")
                continue
            first_ny = to_ny(bars[0].date if bars[0].date.tzinfo else bars[0].date.replace(tzinfo=timezone.utc))
            last_ny = to_ny(bars[-1].date if bars[-1].date.tzinfo else bars[-1].date.replace(tzinfo=timezone.utc))
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            closes = [b.close for b in bars]
            c = compute_cpr_from_bars(highs, lows, closes)
            weekly_cprs[sym] = c
            print(
                f"{sym:<8} {len(bars):>6} "
                f"{first_ny.strftime('%a %m-%d %H:%M'):<22} "
                f"{last_ny.strftime('%a %m-%d %H:%M'):<22} "
                f"{c.high:>10.5f} {c.low:>10.5f} {c.close:>10.5f} "
                f"{c.tc:>10.5f} {c.bc:>10.5f} {c.width_pct:>9.4f}"
            )

        if not weekly_cprs:
            print("\nNo weekly CPRs computed — bailing.")
            return 1

        # Ranked by width % (narrowest first — what selection cares about)
        ranked = sorted(weekly_cprs.items(), key=lambda kv: kv[1].width_pct)
        print("\n" + "=" * 72)
        print("Pairs ranked by weekly CPR width % (narrowest first):")
        print("=" * 72)
        for i, (sym, c) in enumerate(ranked, 1):
            marker = "  ← narrowest (PRIMARY candidate)" if i == 1 else ""
            print(f"  {i:>2}. {sym}: width%={c.width_pct:.4f}{marker}")

        # Sanity: bar counts. ~5 trading days × 12 bars/hr × 24 hr ≈ 1440.
        print("\n" + "=" * 72)
        print("Bar-count sanity check (expected ~1440 for a full prior-week window):")
        print("=" * 72)
        avg_count = sum(bar_counts.values()) / len(bar_counts)
        for sym in DEFAULT_SYMBOLS:
            n = bar_counts.get(sym, 0)
            warn = "  ⚠ low" if n < avg_count * 0.7 else ""
            print(f"  {sym}: {n} bars{warn}")
        print(f"  avg: {avg_count:.0f}")

        # Run selection under several presets
        print("\n" + "=" * 72)
        print("Asset selection under various `allowed_currencies` presets:")
        print("=" * 72)
        presets = [
            ["USD"],
            ["USD", "JPY"],
            ["EUR"],
            ["EUR", "USD", "JPY"],
            ["CHF"],
            ["CAD"],
        ]
        for allowed in presets:
            try:
                r = select_shortlist(DEFAULT_SYMBOLS, allowed, weekly_cprs)
                tag = "expanded" if r.expanded else "no-exp"
                print(f"  allowed={allowed}: primary={r.primary} ({tag}) → trade {list(r.shortlist)}")
            except SelectionError as e:
                print(f"  allowed={allowed}: SelectionError: {e}")

        # Verify our window math against actual returned timestamps
        print("\n" + "=" * 72)
        print("Window-alignment check (does IBKR data start/end where we expect?):")
        print("=" * 72)
        sample_sym = next(iter(weekly_cprs.keys()))
        sample_bars = await fetch_window_bars(ib, contracts[DEFAULT_SYMBOLS.index(sample_sym)], ws, we)
        first_open = to_ny(sample_bars[0].date if sample_bars[0].date.tzinfo else sample_bars[0].date.replace(tzinfo=timezone.utc))
        last_open = to_ny(sample_bars[-1].date if sample_bars[-1].date.tzinfo else sample_bars[-1].date.replace(tzinfo=timezone.utc))
        print(f"  Using {sample_sym} as a sample.")
        print(f"  Expected first open (Sun 17:00 NY): {ws.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"  Actual   first open               : {first_open.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"  Match: {first_open == ws}  (or within 5min tolerance: {abs((first_open - ws).total_seconds()) <= 300})")
        print(f"  Expected last open  (16:55 last Fri): {(we - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"  Actual   last open                  : {last_open.strftime('%Y-%m-%d %H:%M %Z')}")

    finally:
        ib.disconnect()
    print("\nDone. (Disconnected cleanly.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
