"""
Demo backtest for the NEW pivot/SR + quality-SuperTrend strategy.

Faithfully emulates the live runtime (`pivot_runtime.py`) over a recent window:

  - Each FX day: pick the daily-narrowest CPR pair (same selection as live).
  - On a pair change, rebuild a fresh PivotSuperTrendStrategy and warm it by
    replaying `--warmup-days` of that pair's bars (no trades recorded).
  - Replay the FX day's bars through the strategy, recording every event.
  - Base level seeded on the day's first candle (no entry there), dynamic base
    updates on touch, 2-gate entry (close vs base + GREEN/RED), exit on
    SuperTrend flip or 16:55 force-EOD, same-bar reversal.

CFDs only: bars are fetched from the SMART CFD contracts (use_cfd=True) - the
instrument we actually trade - not IDEALPRO spot. MIDPOINT bars.

Outputs (under --output-dir):
  pivot_demo_selections.csv   one row per FX day (winner + width %)
  pivot_demo_events.csv       every strategy event
  pivot_demo_trades.csv       closed trades with points + pips
  pivot_demo_report.md        human-readable summary

Usage:
    python scripts/pivot_backtest.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from collections import OrderedDict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_SYMBOLS, IBKRConnection, StrategyConfig
from cpr import compute_cpr_from_hlc
from ibkr_client import IBKRClient
from pivot_st_strategy import PivotSuperTrendStrategy
from pivots import compute_pivots
from quality_supertrend import QSTConfig
from selection import SelectionError, narrowest_pair
from time_utils import (
    current_fx_day_anchor,
    ny_now,
    prior_trading_fx_day_window,
    to_ny,
)

log = logging.getLogger("pivot_backtest")
FORCE_EXIT = time(16, 55)   # normal 24/5 forex; matches determine_effective_close_time


def _ts_ny(bar) -> datetime:
    ts = bar.date
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return to_ny(ts)


def _pip_factor(symbol: str) -> int:
    return 100 if symbol[3:].upper() == "JPY" else 10000


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _disp(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M")


def _write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _bucket_by_fx_day(bars) -> "OrderedDict[datetime, list]":
    buckets: OrderedDict[datetime, list] = OrderedDict()
    for b in bars:
        fxs, _ = current_fx_day_anchor(_ts_ny(b))
        buckets.setdefault(fxs, []).append(b)
    for k in buckets:
        buckets[k].sort(key=_ts_ny)
    return buckets


def _hlc_by_fx_day(buckets) -> Dict[datetime, tuple]:
    out: Dict[datetime, tuple] = {}
    for fxs, day_bars in buckets.items():
        if not day_bars:
            continue
        H = max(b.high for b in day_bars)
        L = min(b.low for b in day_bars)
        C = day_bars[-1].close
        out[fxs] = (H, L, C)
    return out


def _prior_hlc(hlc_by_day: Dict[datetime, tuple], fx_day: datetime):
    prior_start, _ = prior_trading_fx_day_window(fx_day + timedelta(hours=1))
    return hlc_by_day.get(prior_start)


async def fetch_all(ibkr: IBKRClient, symbols, start_ny, end_ny, use_cfd):
    bars_by_sym: Dict[str, list] = {}
    for sym in symbols:
        try:
            bars = await ibkr.fetch_5min_bars_range(
                sym, start_ny=start_ny, end_ny=end_ny, use_cfd=use_cfd)
            bars_by_sym[sym] = bars
            log.info(f"{sym}: {len(bars)} bars")
        except Exception:
            log.exception(f"{sym}: fetch failed; skipping")
            bars_by_sym[sym] = []
    return bars_by_sym


def run_emulation(symbols, buckets_by_sym, hlc_by_sym, demo_fx_days, warmup_days):
    """Emulate live: daily selection + pivot strategy replay. Returns
    (selections, events, trades)."""
    selections: List[dict] = []
    events: List[dict] = []
    trades: List[dict] = []

    current_pair: Optional[str] = None
    strategy: Optional[PivotSuperTrendStrategy] = None
    open_trade: Optional[dict] = None

    def pivots_for(sym: str, ts_ny: datetime):
        fxs, _ = current_fx_day_anchor(ts_ny)
        hlc = _prior_hlc(hlc_by_sym[sym], fxs)
        if hlc is None:
            return None
        return compute_pivots(*hlc)

    for fd in demo_fx_days:
        # 1. Selection across all symbols using prior-trading-day CPR width.
        cprs = {}
        for sym in symbols:
            hlc = _prior_hlc(hlc_by_sym[sym], fd)
            if hlc is None:
                continue
            try:
                cprs[sym] = compute_cpr_from_hlc(*hlc)
            except Exception:
                continue
        candidates = [s for s in symbols if s in cprs]
        if not candidates:
            log.warning(f"{fd.date()}: no CPRs - skipping day")
            continue
        try:
            winner = narrowest_pair(candidates, cprs)
        except SelectionError:
            continue
        wcpr = cprs[winner]
        selections.append({
            "fx_day": fd.date().isoformat(),
            "winner": winner,
            "width_pct": round(wcpr.width_pct, 5),
            "tc": round(wcpr.tc, 6),
            "bc": round(wcpr.bc, 6),
        })

        # 2. (Re)build + warm the strategy on a pair change.
        if winner != current_pair:
            strategy = PivotSuperTrendStrategy(
                qst_cfg=QSTConfig(), force_exit_close_time=FORCE_EXIT)
            warm_start = fd - timedelta(days=warmup_days)
            warm_bars = []
            for f2, day_bars in buckets_by_sym[winner].items():
                if warm_start <= f2 < fd:
                    warm_bars.extend(day_bars)
            warm_bars.sort(key=_ts_ny)
            for b in warm_bars:
                ts_ny = _ts_ny(b)
                piv = pivots_for(winner, ts_ny)
                if piv is None:
                    continue
                fxs, _ = current_fx_day_anchor(ts_ny)
                strategy.update(
                    timestamp=ts_ny, open_=float(b.open), high=float(b.high),
                    low=float(b.low), close=float(b.close), pivots=piv,
                    fx_day_start=fxs)
            # Mirror the live reconciler: indicators are now fully warm, but
            # start the recorded window FLAT. (The daily 16:55 force-exit already
            # guarantees the strategy is flat entering each FX day; this is
            # belt-and-suspenders.) Keep _prev_st_dir from warmup so flip
            # detection stays continuous - exactly as live reconciliation does.
            strategy.position = 0
            strategy.entry_price = None
            strategy.entry_timestamp = None
            current_pair = winner
            log.info(f"{fd.date()}: selected {winner} (rebuilt, warmed {len(warm_bars)} bars)")
        else:
            log.info(f"{fd.date()}: selected {winner} (unchanged)")

        # 3. Replay the FX day's bars, recording events.
        for b in buckets_by_sym[winner].get(fd, []):
            ts_ny = _ts_ny(b)
            piv = pivots_for(winner, ts_ny)
            if piv is None:
                continue
            outcome = strategy.update(
                timestamp=ts_ny, open_=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close), pivots=piv,
                fx_day_start=fd)
            for ev in outcome.events:
                events.append({
                    "timestamp_ny": _iso(ev.timestamp),
                    "pair": winner,
                    "action": ev.action,
                    "reason": ev.reason,
                    "price": round(ev.price, 6),
                    "bias": ev.bias,
                    "base_level_name": ev.base_level_name,
                    "base_level": round(ev.base_level, 6),
                    "st_state": ev.st_state,
                    "st_dir": ev.st_dir,
                    "new_position": ev.new_position,
                })
                if ev.action in ("ENTRY_LONG", "ENTRY_SHORT",
                                 "REVERSE_TO_LONG", "REVERSE_TO_SHORT"):
                    side = "LONG" if ev.action in ("ENTRY_LONG", "REVERSE_TO_LONG") else "SHORT"
                    open_trade = {
                        "pair": winner, "side": side,
                        "entry_ts": ev.timestamp, "entry_price": ev.price,
                        "entry_base": ev.base_level_name,
                        "was_reversal": ev.action.startswith("REVERSE_"),
                    }
                elif ev.action in ("EXIT_FLIP", "EXIT_EOD") and open_trade is not None:
                    o = open_trade
                    pts = ((ev.price - o["entry_price"]) if o["side"] == "LONG"
                           else (o["entry_price"] - ev.price))
                    bars_in = int((ev.timestamp - o["entry_ts"]).total_seconds() // 300)
                    trades.append({
                        "pair": winner, "side": o["side"],
                        "entry_ts": _iso(o["entry_ts"]), "exit_ts": _iso(ev.timestamp),
                        "entry_price": round(o["entry_price"], 6),
                        "exit_price": round(ev.price, 6),
                        "points": round(pts, 6),
                        "pips": round(pts * _pip_factor(winner), 2),
                        "bars_in_trade": bars_in,
                        "exit_reason": ev.reason,
                        "was_reversal": o["was_reversal"],
                        "entry_base": o["entry_base"],
                        "exit_base": ev.base_level_name,
                    })
                    open_trade = None
    return selections, events, trades


def build_report(selections, trades, args, win_start, win_end, bar_source) -> str:
    n = len(trades)
    wins = [t for t in trades if t["pips"] > 0]
    losses = [t for t in trades if t["pips"] < 0]
    total_pips = round(sum(t["pips"] for t in trades), 1)
    win_rate = round(100 * len(wins) / n, 1) if n else 0.0
    avg_win = round(sum(t["pips"] for t in wins) / len(wins), 1) if wins else 0.0
    avg_loss = round(sum(t["pips"] for t in losses) / len(losses), 1) if losses else 0.0

    lines = [
        "# Pivot/SR + Quality-SuperTrend - demo backtest",
        "",
        f"- Window (NY): **{win_start.date()} -> {win_end.date()}**  "
        f"({args.days} day(s) demo, {args.warmup_days}d warmup)",
        f"- Bar source: **{bar_source}**  (MIDPOINT, CFDs only)",
        f"- FX days traded: **{len(selections)}**",
        "",
        "## Summary",
        "",
        f"| Trades | Win rate | Total pips | Avg win | Avg loss |",
        f"|---|---|---|---|---|",
        f"| {n} | {win_rate}% | {total_pips} | {avg_win} | {avg_loss} |",
        "",
        "## Daily selection",
        "",
        "| FX day | Winner | CPR width % |",
        "|---|---|---|",
    ]
    for s in selections:
        lines.append(f"| {s['fx_day']} | {s['winner']} | {s['width_pct']} |")
    lines += ["", "## Trades", "",
              "| Pair | Side | Entry | Exit | Pips | Bars | Exit reason | Rev |",
              "|---|---|---|---|---|---|---|---|"]
    for t in trades:
        lines.append(
            f"| {t['pair']} | {t['side']} | {t['entry_ts']} | {t['exit_ts']} | "
            f"{t['pips']} | {t['bars_in_trade']} | {t['exit_reason']} | "
            f"{'Y' if t['was_reversal'] else ''} |")
    if not trades:
        lines.append("| _(no trades in window)_ |||||||| ")
    lines.append("")
    return "\n".join(lines)


async def main_async(args) -> int:
    cfg = StrategyConfig(
        ibkr=IBKRConnection(host=args.host, port=args.port,
                            client_id=args.client_id, read_only=True),
    )
    ibkr = IBKRClient(cfg)
    use_cfd = args.bar_source == "cfd"
    now = ny_now()
    # Fetch demo window + warmup + a small lead so the first warmup day has a prior.
    fetch_start = now - timedelta(days=args.days + args.warmup_days + 5)

    await ibkr.connect()
    try:
        log.info(f"Fetching {args.bar_source.upper()} 5-min bars for "
                 f"{len(DEFAULT_SYMBOLS)} symbols, {_iso(fetch_start)} -> {_iso(now)}")
        bars_by_sym = await fetch_all(ibkr, DEFAULT_SYMBOLS, fetch_start, now, use_cfd)
    finally:
        await ibkr.disconnect()

    buckets_by_sym = {s: _bucket_by_fx_day(b) for s, b in bars_by_sym.items()}
    hlc_by_sym = {s: _hlc_by_fx_day(buckets_by_sym[s]) for s in bars_by_sym}

    # Demo FX days = those whose start is within [now - days, now] and that have
    # a prior trading day for at least one symbol.
    window_start = now - timedelta(days=args.days)
    all_fx_days = sorted({fd for s in buckets_by_sym for fd in buckets_by_sym[s]})
    demo_fx_days = [fd for fd in all_fx_days if fd >= window_start]
    win_start = demo_fx_days[0] if demo_fx_days else window_start
    win_end = now
    log.info(f"Demo FX days: {len(demo_fx_days)}")

    selections, events, trades = run_emulation(
        DEFAULT_SYMBOLS, buckets_by_sym, hlc_by_sym, demo_fx_days, args.warmup_days)

    out = args.output_dir
    _write_csv(out / "pivot_demo_selections.csv", selections,
               ["fx_day", "winner", "width_pct", "tc", "bc"])
    _write_csv(out / "pivot_demo_events.csv", events,
               ["timestamp_ny", "pair", "action", "reason", "price", "bias",
                "base_level_name", "base_level", "st_state", "st_dir", "new_position"])
    _write_csv(out / "pivot_demo_trades.csv", trades,
               ["pair", "side", "entry_ts", "exit_ts", "entry_price", "exit_price",
                "points", "pips", "bars_in_trade", "exit_reason", "was_reversal",
                "entry_base", "exit_base"])
    report = build_report(selections, trades, args, win_start, win_end, args.bar_source.upper())
    (out / "pivot_demo_report.md").write_text(report)

    print("\n" + report)
    print(f"\nArtifacts written to {out}/")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Demo backtest for the pivot/SR strategy")
    p.add_argument("--days", type=int, default=7, help="Demo window length (FX days back).")
    p.add_argument("--warmup-days", type=int, default=10,
                   help="Indicator warmup per pair rebuild (10d is ample for ST/ADX/EMA).")
    p.add_argument("--bar-source", choices=["cfd", "forex"], default="cfd",
                   help="Fetch bars from the CFD contract (default) or IDEALPRO spot.")
    p.add_argument("--output-dir", type=Path, default=Path("backtest_output/pivot_demo"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=77)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
