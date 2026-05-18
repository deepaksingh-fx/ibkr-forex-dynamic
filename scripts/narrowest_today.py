"""
Compute today's daily-narrowest CPR pair, standalone.

Same logic as Strategy._select_and_log, but runs once and prints the result.
Uses `prior_trading_fx_day_window` so Monday correctly falls back to last
Friday's FX day.

Usage:
    python scripts/narrowest_today.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_SYMBOLS, IBKRConnection, StrategyConfig
from cpr import compute_cpr_from_bars
from ibkr_client import IBKRClient
from selection import narrowest_pair
from time_utils import (
    current_fx_day_anchor,
    ny_now,
    prior_trading_fx_day_window,
    to_ny,
)


def configure_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def filter_window(bars, start_ny, end_ny):
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


async def run(symbols, cfg):
    log = logging.getLogger("narrowest_today")
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        now = ny_now()
        cur_start, _ = current_fx_day_anchor(now)
        ws, we = prior_trading_fx_day_window(now)
        log.info(f"Current FX day start: {cur_start.isoformat()}")
        log.info(f"Source window (prior trading FX day): {ws.isoformat()} -> {we.isoformat()}")

        cprs = {}
        for sym in symbols:
            try:
                bars = await ibkr.fetch_5min_bars(sym, end_ny=we, duration_str="2 D")
            except Exception as e:
                log.warning(f"{sym}: fetch failed: {e}")
                continue
            in_win = filter_window(bars, ws, we)
            if not in_win:
                log.warning(f"{sym}: no bars in window - skipping")
                continue
            cprs[sym] = compute_cpr_from_bars(
                [b.high for b in in_win],
                [b.low for b in in_win],
                [b.close for b in in_win],
            )
    finally:
        await ibkr.disconnect()

    if not cprs:
        print("No CPRs computed - nothing to rank.")
        return

    candidates = [s for s in symbols if s in cprs]
    winner = narrowest_pair(candidates, cprs)
    ranked = sorted(cprs.items(), key=lambda kv: kv[1].width_pct)

    print()
    print("=" * 90)
    print(f"DAILY-NARROWEST CPR PAIR for FX day starting {cur_start.strftime('%a %Y-%m-%d %H:%M %Z')}")
    print(f"Source window: {ws.strftime('%a %Y-%m-%d %H:%M %Z')} -> {we.strftime('%a %Y-%m-%d %H:%M %Z')}")
    print("=" * 90)
    wcpr = cprs[winner]
    print(f"  WINNER: {winner}   width_pct={wcpr.width_pct:.4f}%   "
          f"TC={wcpr.tc:.5f}   BC={wcpr.bc:.5f}   Pivot={wcpr.pivot:.5f}")
    print()
    print("Full ranking (ascending width %):")
    print(f"  {'PAIR':<8} {'WIDTH%':>10} {'TC':>12} {'BC':>12} {'PIVOT':>12}")
    for sym, c in ranked:
        marker = " *" if sym == winner else "  "
        print(f"  {marker}{sym:<6} {c.width_pct:>10.4f} {c.tc:>12.5f} {c.bc:>12.5f} {c.pivot:>12.5f}")
    print()


def main():
    p = argparse.ArgumentParser(description="Today's daily-narrowest CPR pair")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=54)
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help=f"Comma-separated pair list (default: {len(DEFAULT_SYMBOLS)} pairs)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)
    cfg = StrategyConfig(
        ibkr=IBKRConnection(
            host=args.host, port=args.port, client_id=args.client_id,
            read_only=True,
        ),
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(run(symbols, cfg))


if __name__ == "__main__":
    main()
