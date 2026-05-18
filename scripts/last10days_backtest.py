"""
Multi-pair daily-rotation backtest for the CPR + Regime + Adaptive SuperTrend
strategy.

Process:
  1. Fetch ~35 days of 5-min bars for ALL 15 default forex pairs (chunked).
  2. Identify the last N completed trading FX days (default N=10).
  3. For each backtest FX day:
       - Look at the PRIOR TRADING FX day's HLC for each pair → compute CPR
         width %; the narrowest is the "selected pair" for this FX day.
  4. Run the strategy on EACH pair end-to-end across all fetched bars (so each
     pair's regime + AST indicators stay warm). For each FX day, only the
     "selected pair"'s events are counted toward the result.
  5. Force-exit at the empirically-determined close time per pair (16:55 NY
     for normal 24/5 forex).

Output: an Excel workbook + a markdown summary in `--output-dir`.

Sheets:
  - Daily Summary       — one row per FX day (selected pair + day stats)
  - Selection Detail    — one row per (FX day, pair) showing width %, TC, BC,
                           rank, and selected flag
  - All Trades          — every trade from selected pairs
  - All Events          — every event (entry / exit / reverse) on selected days
  - Headline            — aggregate stats across the backtest window

Usage:
    python scripts/last10days_backtest.py --days 10
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from adaptive_supertrend import AdaptiveSTConfig
from config import DEFAULT_SYMBOLS, IBKRConnection, StrategyConfig
from cpr import compute_cpr_from_hlc
from cpr_st_strategy import CPRSuperTrendStrategy
from ibkr_client import IBKRClient
from regime import RegimeConfig
from time_utils import (
    current_fx_day_anchor,
    is_in_trading_zone,
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


def iso_ny(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def disp_ny(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M %Z")


def disp_date(dt: datetime) -> str:
    """Display the TRADING-DAY name for an FX-day START anchor.

    Each FX day runs [prior-calendar-day 17:00 NY, today 17:00 NY) — its
    START is on the prior calendar day. For human-readable labels we want
    the day the session is named after, which is `start + 1 day`.
    e.g. fd = Sun 2026-05-03 17:00 NY  →  "Mon 2026-05-04" (Mon's FX day).
    """
    return (dt + timedelta(days=1)).strftime("%a %Y-%m-%d")


def trading_day_iso(dt: datetime) -> str:
    return (dt + timedelta(days=1)).date().isoformat()


def bucket_by_fx_day(bars):
    buckets: OrderedDict[datetime, list] = OrderedDict()
    for b in bars:
        ts = b.date
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        ts_ny = to_ny(ts)
        fx_start, _ = current_fx_day_anchor(ts_ny)
        buckets.setdefault(fx_start, []).append(b)
    return buckets


def effective_close_time_from_buckets(buckets) -> Optional[time]:
    """Mode of last-bar-open-time across complete FX days = force-exit close time."""
    if not buckets:
        return None
    sorted_starts = sorted(buckets.keys())
    today, _ = current_fx_day_anchor()
    # Drop the in-progress FX day if present.
    if sorted_starts and sorted_starts[-1] == today:
        sorted_starts = sorted_starts[:-1]
    if not sorted_starts:
        return None
    times = []
    for fd in sorted_starts:
        bars = buckets[fd]
        if not bars:
            continue
        last_ts = bars[-1].date
        if last_ts.tzinfo is None:
            from datetime import timezone as _tz
            last_ts = last_ts.replace(tzinfo=_tz.utc)
        last_ts_ny = to_ny(last_ts)
        times.append(last_ts_ny.time())
    if not times:
        return None
    return Counter(times).most_common(1)[0][0]


def list_trading_fx_days(buckets, n: int, exclude_in_progress: bool = True):
    """Return the last `n` COMPLETED trading FX day starts (sorted ascending)."""
    sorted_starts = sorted(buckets.keys())
    if exclude_in_progress:
        today, _ = current_fx_day_anchor()
        sorted_starts = [s for s in sorted_starts if s != today]
    # Filter to "trading" FX days only: start weekday must be one of
    # {Sun=6, Mon=0, Tue=1, Wed=2, Thu=3} (excludes Fri/Sat starts).
    trading = [s for s in sorted_starts if s.weekday() in (6, 0, 1, 2, 3)]
    return trading[-n:] if n > 0 else trading


async def run(days: int, output_dir: Path,
              cfg: StrategyConfig, regime_cfg: RegimeConfig,
              ast_cfg: AdaptiveSTConfig, fetch_days: int):
    log = logging.getLogger("last10days_backtest")
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        now = ny_now()
        start = now - timedelta(days=fetch_days)
        log.info(f"Fetching {fetch_days}D of 5-min bars for {len(DEFAULT_SYMBOLS)} pairs "
                 f"(serial — IBKR's ib_async timeout kills parallel fetches)")

        # Pre-qualify all contracts in parallel (these are tiny requests).
        await ibkr.qualify_many(list(DEFAULT_SYMBOLS))

        import time as _time
        t0 = _time.time()
        pair_bars: dict[str, list] = {}
        for i, sym in enumerate(DEFAULT_SYMBOLS, 1):
            bars = await ibkr.fetch_5min_bars_range(
                sym, start_ny=start, end_ny=now, pace_sleep_s=0.2,
            )
            pair_bars[sym] = bars
            log.info(f"  [{i:>2}/{len(DEFAULT_SYMBOLS)}] {sym:<7} → {len(bars):>6} bars  "
                     f"(elapsed {_time.time() - t0:.0f}s)")
        log.info(f"All fetches done in {_time.time() - t0:.1f}s")
    finally:
        await ibkr.disconnect()

    # 1. Bucket each pair's bars by FX day + compute per-day HLC.
    pair_buckets: dict[str, "OrderedDict[datetime, list]"] = {}
    pair_day_hlc: dict[str, dict[datetime, tuple[float, float, float]]] = {}
    pair_close_time: dict[str, time] = {}

    for sym, bars in pair_bars.items():
        b = bucket_by_fx_day(bars)
        pair_buckets[sym] = b
        hlc = {}
        for fd, day_bars in b.items():
            if not day_bars:
                continue
            H = max(x.high for x in day_bars)
            L = min(x.low for x in day_bars)
            C = day_bars[-1].close
            hlc[fd] = (H, L, C)
        pair_day_hlc[sym] = hlc
        ct = effective_close_time_from_buckets(b)
        pair_close_time[sym] = ct or time(16, 55)
        log.info(f"{sym}: {len(b)} FX days, effective close = {pair_close_time[sym].strftime('%H:%M')} NY")

    # 2. Identify the backtest FX days (last N completed trading FX days).
    # Use one pair (any) to compute the universe of FX days.
    all_fx_days_any = sorted(set().union(*[set(b.keys()) for b in pair_buckets.values()]))
    today, _ = current_fx_day_anchor()
    completed_trading = [s for s in all_fx_days_any
                         if s != today and s.weekday() in (6, 0, 1, 2, 3)]
    backtest_days = completed_trading[-days:] if days > 0 else completed_trading
    log.info(f"Backtest FX days ({len(backtest_days)}): "
             + ", ".join(disp_date(d) for d in backtest_days))

    # 3. Per FX day, find the narrowest pair from prior trading FX day's HLC.
    selection_rows = []   # one per (FX day, pair)
    daily_selection: dict[datetime, tuple[str, float, float, float, float]] = {}
    for fd in backtest_days:
        # The "prior trading FX day" depends on this FX day's weekday.
        # We need a moment inside this FX day to feed prior_trading_fx_day_window.
        sample_now = fd + timedelta(hours=1)
        prior_start, prior_end = prior_trading_fx_day_window(sample_now)
        # Buckets keyed by FX-day START — prior_start = bucket key for the
        # prior trading FX day's HLC.
        pair_widths = []
        for sym in DEFAULT_SYMBOLS:
            hlc_map = pair_day_hlc.get(sym, {})
            if prior_start not in hlc_map:
                continue
            H, L, C = hlc_map[prior_start]
            try:
                cpr = compute_cpr_from_hlc(H, L, C)
            except Exception:
                continue
            pair_widths.append((sym, cpr))
        if not pair_widths:
            log.warning(f"No pairs with CPR available for FX day {disp_date(fd)} — skipping")
            continue
        # Tie-break by first appearance in DEFAULT_SYMBOLS.
        order = {s: i for i, s in enumerate(DEFAULT_SYMBOLS)}
        pair_widths.sort(key=lambda kv: (kv[1].width_pct, order[kv[0]]))
        ranked = pair_widths
        selected_sym, selected_cpr = ranked[0]
        daily_selection[fd] = (
            selected_sym, selected_cpr.width_pct,
            selected_cpr.tc, selected_cpr.bc, selected_cpr.pivot,
        )
        for rank, (sym, cpr) in enumerate(ranked, 1):
            selection_rows.append({
                "FX Day": disp_date(fd),
                "FX Day ISO": trading_day_iso(fd),
                "Pair": sym,
                "Width %": round(cpr.width_pct, 4),
                "TC": round(cpr.tc, 6),
                "BC": round(cpr.bc, 6),
                "Pivot": round(cpr.pivot, 6),
                "Rank": rank,
                "Selected": (sym == selected_sym),
            })

    log.info("Daily selections:")
    for fd, (sym, w, tc, bc, _) in daily_selection.items():
        log.info(f"  {disp_date(fd)}  →  {sym}   width={w:.4f}%   TC={tc:.5f}  BC={bc:.5f}")

    # 4. For each selected pair (unique set), run the strategy across all
    #    fetched bars, accumulating events only on the FX days that pair was
    #    selected.
    selected_pairs = set(s[0] for s in daily_selection.values())
    log.info(f"Selected pairs across the {len(backtest_days)} day window: {sorted(selected_pairs)}")

    fx_day_to_selected_pair = {fd: sel[0] for fd, sel in daily_selection.items()}

    all_events: list[dict] = []
    all_trades: list[dict] = []
    bars_processed = 0

    for sym in sorted(selected_pairs):
        strategy = CPRSuperTrendStrategy(
            regime_cfg=regime_cfg,
            ast_cfg=ast_cfg,
            force_exit_close_time=pair_close_time[sym],
            ast_bars_per_day=288,
        )
        symbol_buckets = pair_buckets[sym]
        symbol_fx_days_sorted = sorted(symbol_buckets.keys())
        open_trade: Optional[dict] = None

        for fd in symbol_fx_days_sorted:
            sample_now = fd + timedelta(hours=1)
            prior_start, prior_end = prior_trading_fx_day_window(sample_now)
            if prior_start not in pair_day_hlc[sym]:
                continue
            H, L, C = pair_day_hlc[sym][prior_start]
            try:
                cpr = compute_cpr_from_hlc(H, L, C)
            except Exception:
                continue

            is_selected_today = (fx_day_to_selected_pair.get(fd) == sym)

            for b in symbol_buckets[fd]:
                ts = b.date
                if ts.tzinfo is None:
                    from datetime import timezone as _tz
                    ts = ts.replace(tzinfo=_tz.utc)
                ts_ny = to_ny(ts)

                outcome = strategy.update(
                    timestamp=ts_ny,
                    open_=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    daily_tc=cpr.tc,
                    daily_bc=cpr.bc,
                    daily_pp=cpr.pivot,
                    fx_day_start=fd,
                )
                bars_processed += 1

                if not is_selected_today:
                    continue   # Strategy runs to keep indicators warm; don't record.

                for ev in outcome.events:
                    all_events.append({
                        "FX Day": disp_date(fd),
                        "Pair": sym,
                        "Time NY (ISO)": iso_ny(ev.timestamp),
                        "Time NY": disp_ny(ev.timestamp),
                        "Action": ev.action,
                        "Reason": ev.reason,
                        "Price": round(ev.price, 6),
                        "Bias": ev.bias,
                        "Regime": ev.regime,
                        "Regime Directional": ev.regime_directional,
                        "AST Dir": ev.active_dir,
                        "AST Method": ev.active_method,
                        "TC": round(cpr.tc, 6),
                        "BC": round(cpr.bc, 6),
                    })

                    if ev.action in ("ENTRY_LONG", "ENTRY_SHORT",
                                      "REVERSE_TO_LONG", "REVERSE_TO_SHORT"):
                        side = "LONG" if ev.action in ("ENTRY_LONG", "REVERSE_TO_LONG") else "SHORT"
                        open_trade = {
                            "fd": fd, "pair": sym,
                            "entry_ts": ev.timestamp,
                            "entry_price": ev.price,
                            "side": side,
                            "entry_regime": ev.regime,
                            "entry_ast_method": ev.active_method,
                            "was_reversal": ev.action.startswith("REVERSE_"),
                        }
                    elif ev.action in ("EXIT_FLIP", "EXIT_EOD") and open_trade is not None:
                        side = open_trade["side"]
                        entry_px = open_trade["entry_price"]
                        exit_px = ev.price
                        pts = (exit_px - entry_px) if side == "LONG" else (entry_px - exit_px)
                        bars_in_trade = int((ev.timestamp - open_trade["entry_ts"]).total_seconds() // 300)
                        all_trades.append({
                            "FX Day": disp_date(open_trade["fd"]),
                            "Pair": open_trade["pair"],
                            "Side": side,
                            "Entry Time NY": disp_ny(open_trade["entry_ts"]),
                            "Entry Time ISO": iso_ny(open_trade["entry_ts"]),
                            "Entry Price": round(entry_px, 6),
                            "Exit Time NY": disp_ny(ev.timestamp),
                            "Exit Time ISO": iso_ny(ev.timestamp),
                            "Exit Price": round(exit_px, 6),
                            "Points": round(pts, 6),
                            "Win": pts > 0,
                            "Bars in Trade": bars_in_trade,
                            "Exit Reason": ev.reason,
                            "Was Reversal Entry": open_trade["was_reversal"],
                            "Entry Regime": open_trade["entry_regime"],
                            "Exit Regime": ev.regime,
                            "Entry AST Method": open_trade["entry_ast_method"],
                            "Exit AST Method": ev.active_method,
                        })
                        open_trade = None
        # End of this pair's bar loop. open_trade should always be None
        # at this point (force-exit closes positions at EOD).

    log.info(f"Total bars processed across selected pairs: {bars_processed:,}")
    log.info(f"Total trades recorded: {len(all_trades)}")
    log.info(f"Total events recorded: {len(all_events)}")

    # 5. Build per-day summary.
    daily_summary_rows = []
    for fd in backtest_days:
        if fd not in daily_selection:
            continue
        sym, width_pct, tc, bc, pivot = daily_selection[fd]
        day_trades = [t for t in all_trades if t["FX Day"] == disp_date(fd) and t["Pair"] == sym]
        n = len(day_trades)
        wins = sum(1 for t in day_trades if t["Win"])
        total_pts = sum(t["Points"] for t in day_trades)
        best = max((t["Points"] for t in day_trades), default=0.0)
        worst = min((t["Points"] for t in day_trades), default=0.0)
        daily_summary_rows.append({
            "FX Day": disp_date(fd),
            "FX Day ISO": trading_day_iso(fd),
            "Selected Pair": sym,
            "Width %": round(width_pct, 4),
            "TC": round(tc, 6),
            "BC": round(bc, 6),
            "Pivot": round(pivot, 6),
            "Trades": n,
            "Wins": wins,
            "Losses": n - wins,
            "Win Rate %": round((100.0 * wins / n) if n else 0.0, 1),
            "Total Pts": round(total_pts, 4),
            "Avg Pts / Trade": round(total_pts / n, 4) if n else 0.0,
            "Best Trade": round(best, 4),
            "Worst Trade": round(worst, 4),
        })

    # 6. Headline aggregate.
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t["Win"])
    total_pts = sum(t["Points"] for t in all_trades)
    win_rate = (100.0 * total_wins / total_trades) if total_trades else 0.0
    avg_per_trade = (total_pts / total_trades) if total_trades else 0.0
    side_counts = Counter(t["Side"] for t in all_trades)
    exit_counts = Counter(t["Exit Reason"] for t in all_trades)
    reversal_count = sum(1 for t in all_trades if t["Was Reversal Entry"])

    headline_rows = [
        {"Metric": "Backtest FX days", "Value": len(backtest_days)},
        {"Metric": "Total trades", "Value": total_trades},
        {"Metric": "Wins", "Value": total_wins},
        {"Metric": "Losses", "Value": total_trades - total_wins},
        {"Metric": "Win rate %", "Value": round(win_rate, 1)},
        {"Metric": "Total points", "Value": round(total_pts, 4)},
        {"Metric": "Avg pts / trade", "Value": round(avg_per_trade, 4)},
        {"Metric": "Long trades", "Value": side_counts.get("LONG", 0)},
        {"Metric": "Short trades", "Value": side_counts.get("SHORT", 0)},
        {"Metric": "Exits by ST flip", "Value": exit_counts.get("SUPERTREND_FLIP", 0)},
        {"Metric": "Exits by EOD force", "Value": exit_counts.get("FORCE_EXIT_EOD", 0)},
        {"Metric": "Reversal entries", "Value": reversal_count},
    ]

    # 7. Write Excel + markdown.
    output_dir.mkdir(parents=True, exist_ok=True)
    last_label = backtest_days[-1].date().isoformat() if backtest_days else "no-data"
    xlsx_path = output_dir / f"last{days}days_strategy_{last_label}.xlsx"
    md_path = output_dir / f"last{days}days_strategy_{last_label}.md"

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame(headline_rows).to_excel(writer, sheet_name="Headline", index=False)
        pd.DataFrame(daily_summary_rows).to_excel(writer, sheet_name="Daily Summary", index=False)
        pd.DataFrame(selection_rows).to_excel(writer, sheet_name="Selection Detail", index=False)
        pd.DataFrame(all_trades).to_excel(writer, sheet_name="All Trades", index=False)
        pd.DataFrame(all_events).to_excel(writer, sheet_name="All Events", index=False)

    # Markdown summary
    lines = []
    lines.append(f"# Last {days} days strategy backtest\n")
    lines.append(f"Generated: {disp_ny(ny_now())}\n")
    lines.append(f"\n## Headline\n")
    for r in headline_rows:
        lines.append(f"- {r['Metric']}: **{r['Value']}**")
    lines.append(f"\n## Daily selections\n")
    lines.append("| Day | Selected | Width % | TC | BC | Trades | Win% | Total Pts |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in daily_summary_rows:
        lines.append(
            f"| {r['FX Day']} | {r['Selected Pair']} | "
            f"{r['Width %']} | {r['TC']} | {r['BC']} | "
            f"{r['Trades']} | {r['Win Rate %']}% | {r['Total Pts']:+.4f} |"
        )
    lines.append(f"\n## Output\n")
    lines.append(f"- Workbook: `{xlsx_path.name}`")
    lines.append(f"  - Sheets: Headline, Daily Summary, Selection Detail, All Trades, All Events")
    md_path.write_text("\n".join(lines) + "\n")

    # 8. Console
    print()
    print("=" * 110)
    print(f"LAST {days} DAYS STRATEGY BACKTEST — multi-pair daily selection")
    print("=" * 110)
    print(f"Backtest window: {disp_date(backtest_days[0])} → {disp_date(backtest_days[-1])}")
    print(f"Selected pairs:  {dict(Counter(s[0] for s in daily_selection.values()))}")
    print()
    print("Per-day result:")
    for r in daily_summary_rows:
        pl = f"{r['Total Pts']:+.4f}"
        wr = f"{r['Win Rate %']:.0f}%" if r["Trades"] else " — "
        print(f"  {r['FX Day']:<22} → {r['Selected Pair']:<6}  "
              f"width={r['Width %']:.4f}%   "
              f"trades={r['Trades']:>3}  win={wr:>5}  pts={pl}")
    print()
    print(f"TOTAL:  trades={total_trades}  win_rate={win_rate:.1f}%  total_pts={total_pts:+.4f}  avg={avg_per_trade:+.4f}")
    print()
    print(f"Excel: {xlsx_path}")
    print(f"Markdown: {md_path}")


def main():
    p = argparse.ArgumentParser(description="Multi-pair daily-rotation backtest")
    p.add_argument("--days", type=int, default=10, help="Number of completed trading FX days to backtest")
    p.add_argument("--fetch-days", type=int, default=35,
                   help="How many days of 5-min history to fetch per pair (warmup + backtest)")
    p.add_argument("--output-dir", type=Path, default=Path("backtest_output"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=57)
    p.add_argument("--regime-er-len", type=int, default=20)
    p.add_argument("--regime-er-enter", type=float, default=0.40)
    p.add_argument("--regime-er-exit", type=float, default=0.30)
    p.add_argument("--regime-cross-len", type=int, default=30)
    p.add_argument("--regime-cross-threshold", type=int, default=3)
    p.add_argument("--ast-base-atr", type=int, default=10)
    p.add_argument("--ast-base-mult", type=float, default=3.0)
    p.add_argument("--ast-eval-interval", type=int, default=30)
    p.add_argument("--ast-min-trades", type=int, default=5)
    p.add_argument("--ast-perf-lookback-days", type=int, default=60)
    p.add_argument("--ast-criterion", default="Average Per Trade",
                   choices=["Total Points", "Win Rate", "Average Per Trade"])
    p.add_argument("--ast-no-rsi", action="store_true")
    p.add_argument("--ast-no-macd", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)

    cfg = StrategyConfig(
        ibkr=IBKRConnection(
            host=args.host, port=args.port, client_id=args.client_id,
            read_only=True,
        ),
    )
    regime_cfg = RegimeConfig(
        er_len=args.regime_er_len, er_enter=args.regime_er_enter,
        er_exit=args.regime_er_exit, cross_len=args.regime_cross_len,
        cross_threshold=args.regime_cross_threshold,
    )
    ast_cfg = AdaptiveSTConfig(
        base_atr=args.ast_base_atr, base_mult=args.ast_base_mult,
        eval_interval_bars=args.ast_eval_interval,
        min_trades=args.ast_min_trades,
        perf_lookback_days=args.ast_perf_lookback_days,
        selection_criterion=args.ast_criterion,
        enable_rsi=not args.ast_no_rsi,
        enable_macd=not args.ast_no_macd,
    )

    asyncio.run(run(args.days, args.output_dir, cfg, regime_cfg, ast_cfg, args.fetch_days))


if __name__ == "__main__":
    main()
