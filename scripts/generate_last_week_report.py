"""
Generate a Markdown backtest report for last week and save it to
reports/last_week_backtest.md.

Re-uses the replay engine from backtest_replay.py. Same logic, same numbers,
just emitted as a clean human-readable Markdown file with summary tables.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Re-use the existing engine.
from scripts.backtest_replay import (
    Bar, ReplayLog, fetch_bars, cpr_from_window, replay_pair,
    SOURCE_WEEK_START, SOURCE_WEEK_END, REPLAY_START, REPLAY_END,
    ALLOWED_CURRENCIES, ENTRY_TRIGGER_RANGE_PCT, PER_TRADE_LOSS_PCT,
    PER_DAY_LOSS_PCT, TRAIL_ARM_PCT, FROZEN_BALANCE, LOT_SIZE,
    HOST, PORT,
)
# Different client_id from runner.py to avoid lingering-slot conflicts
CLIENT_ID = 73
from ib_async import IB    # type: ignore[import-untyped]
from config import DEFAULT_SYMBOLS
from cpr import CPR
from selection import select_shortlist


REPORT_PATH = ROOT / "reports" / "last_week_backtest.md"


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%a %m-%d %H:%M")


def write_report(
    weekly_cprs: Dict[str, CPR],
    primary: str,
    expanded: bool,
    shortlist: List[str],
    replays: Dict[str, ReplayLog],
    bar_counts: Dict[str, int],
) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = []

    out.append("# forex_cpr_ibkr — last-week backtest report")
    out.append("")
    out.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_")
    out.append("")
    out.append("## Setup")
    out.append("")
    out.append("| Parameter | Value |")
    out.append("|---|---|")
    out.append(f"| Source week (weekly CPR) | {fmt_time(SOURCE_WEEK_START)} → {fmt_time(SOURCE_WEEK_END)} NY |")
    out.append(f"| Replay week | {fmt_time(REPLAY_START)} → {fmt_time(REPLAY_END)} NY |")
    out.append(f"| `allowed_currencies` | `{ALLOWED_CURRENCIES}` |")
    out.append(f"| `entry_trigger_range_pct` | {ENTRY_TRIGGER_RANGE_PCT}% |")
    out.append(f"| `per_trade_loss_pct` | {PER_TRADE_LOSS_PCT}% |")
    out.append(f"| `per_day_loss_pct` | {PER_DAY_LOSS_PCT}% |")
    out.append(f"| `trail_arm_pct` | {TRAIL_ARM_PCT}% |")
    out.append(f"| Frozen balance (assumption) | ${FROZEN_BALANCE:,.0f} |")
    out.append(f"| Lot size | {LOT_SIZE} (= {int(LOT_SIZE*100_000):,} base units) |")
    out.append("")

    # Bar counts
    out.append("## Data quality")
    out.append("")
    out.append(f"| Symbol | bars (14d) |")
    out.append("|---|---|")
    for sym in DEFAULT_SYMBOLS:
        out.append(f"| {sym} | {bar_counts.get(sym, 0)} |")
    out.append("")

    # Weekly CPR ranking
    ranked = sorted(weekly_cprs.items(), key=lambda kv: kv[1].width_pct)
    out.append("## Weekly CPR width % (narrowest first)")
    out.append("")
    out.append("| Rank | Symbol | width % | TC | BC |")
    out.append("|---|---|---|---|---|")
    for i, (sym, c) in enumerate(ranked, 1):
        out.append(f"| {i} | **{sym}** | {c.width_pct:.4f} | {c.tc:.5f} | {c.bc:.5f} |")
    out.append("")

    # Selection
    out.append("## Asset selection")
    out.append("")
    out.append(f"- **Primary**: `{primary}` (width % = {weekly_cprs[primary].width_pct:.4f})")
    out.append(f"- **Expanded**: `{expanded}`")
    out.append(f"- **Shortlist**: `{shortlist}`")
    out.append("")

    # Per-pair replay
    for sym, log in replays.items():
        out.append(f"## Replay: {sym}")
        out.append("")
        # Summary table first
        out.append("### Summary")
        out.append("")
        out.append("| Metric | Value |")
        out.append("|---|---|")
        out.append(f"| Total events | {len(log.entries)} |")
        out.append(f"| Realized trades | {log.realized_trades} |")
        out.append(f"| EOD exits | {log.eod_exits} |")
        out.append(f"| Per-trade SL hits | {log.sl_hits} |")
        out.append(f"| Trail exits | {log.trail_exits} |")
        out.append(f"| Reversals | {log.reversals} |")
        out.append(f"| Daily breaker trips | {log.breaker_trips} |")
        out.append(f"| **Approx total P&L** | **${log.total_pnl:.2f}** ({log.total_pnl/FROZEN_BALANCE*100:.2f}% of frozen balance) |")
        out.append("")

        # Event log
        out.append("### Event log")
        out.append("")
        out.append("| Time (NY) | Event | Detail |")
        out.append("|---|---|---|")
        for ts, evt, msg in log.entries:
            # Escape pipe characters in message for MD table
            safe = msg.replace("|", "\\|")
            out.append(f"| `{fmt_time(ts)}` | `{evt}` | {safe} |")
        out.append("")

    # Conclusion
    out.append("## Notes")
    out.append("")
    out.append("- This is an **approximate** backtest. P&L uses a simplified forex pip model (right magnitude, not accounting-grade).")
    out.append("- Per-trade SL is evaluated only on bar close (not tick-level). Live mode uses tick-level via `reqPnLSingle`.")
    out.append("- The replay does NOT apply the trading-zone gate — bars at boundary moments (e.g. Fri 17:00 entry) are processed. Live mode skips them.")
    out.append("- One-week sample is too small to draw conclusions about expectancy. Use multi-week runs for that.")
    out.append("")

    REPORT_PATH.write_text("\n".join(out))
    print(f"Report written to: {REPORT_PATH}")
    print(f"Size: {REPORT_PATH.stat().st_size:,} bytes")


async def main() -> int:
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    if not ib.isConnected():
        print("Failed to connect")
        return 1
    try:
        # Fetch bars
        bars_per_pair: Dict[str, List[Bar]] = {}
        bar_counts: Dict[str, int] = {}
        print(f"Fetching 14 days of 5-min bars for {len(DEFAULT_SYMBOLS)} pairs...")
        for sym in DEFAULT_SYMBOLS:
            bars = await fetch_bars(ib, sym, REPLAY_END, "14 D")
            bars_per_pair[sym] = bars
            bar_counts[sym] = len(bars)

        # Weekly CPR
        weekly_cprs: Dict[str, CPR] = {}
        for sym, bars in bars_per_pair.items():
            cpr = cpr_from_window(bars, SOURCE_WEEK_START, SOURCE_WEEK_END)
            if cpr is not None:
                weekly_cprs[sym] = cpr

        # Selection
        sel = select_shortlist(DEFAULT_SYMBOLS, ALLOWED_CURRENCIES, weekly_cprs)
        print(f"Primary: {sel.primary}, expanded: {sel.expanded}, shortlist: {sel.shortlist}")

        # Replay each shortlisted pair
        replays: Dict[str, ReplayLog] = {}
        for sym in sel.shortlist:
            print(f"Replaying {sym}...")
            log = replay_pair(sym, bars_per_pair[sym], weekly_cprs[sym],
                              REPLAY_START, REPLAY_END)
            replays[sym] = log

        write_report(weekly_cprs, sel.primary, sel.expanded, list(sel.shortlist),
                     replays, bar_counts)
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
