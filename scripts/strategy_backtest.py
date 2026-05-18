"""
Single-pair backtest of the CPR + Regime + Adaptive SuperTrend strategy.

Three-gate entry (CPR bias + directional regime + SuperTrend alignment),
SuperTrend-flip exit, force-exit at the per-pair effective close time,
same-bar reversal allowed (except on force-exit bar).

Setup per FX day:
  - Compute daily CPR (TC, BC, Pivot) from the PRIOR TRADING FX day's HLC
    (so Monday uses last Friday's session — `prior_trading_fx_day_window`).
  - Use those values for every 5-min bar in this FX day.

Force-exit time is determined empirically per symbol from recent history
(IBKRClient.determine_effective_close_time). For normal 24/5 forex this
is 16:55 NY.

Outputs (under `--output-dir`):
  - <symbol>_<days>d_strategy_trades.csv
  - <symbol>_<days>d_strategy_events.csv
  - <symbol>_<days>d_strategy_summary.md

Usage:
    python scripts/strategy_backtest.py --symbol CHFJPY --days 90
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from collections import Counter, OrderedDict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adaptive_supertrend import AdaptiveSTConfig
from config import IBKRConnection, StrategyConfig
from cpr import compute_cpr_from_bars
from cpr_st_strategy import CPRSuperTrendStrategy
from ibkr_client import IBKRClient
from regime import RegimeConfig
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


def iso_ny(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def disp_ny(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M %Z")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


async def run(symbol: str, days: int, output_dir: Path,
              cfg: StrategyConfig, regime_cfg: RegimeConfig,
              ast_cfg: AdaptiveSTConfig):
    log = logging.getLogger("strategy_backtest")
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        # 1. Effective close time for the symbol.
        close_time = await ibkr.determine_effective_close_time(symbol, sample_days=10)
        if close_time is None:
            log.warning(f"Could not determine effective close time for {symbol} — using 16:55 NY")
            close_time = time(16, 55)
        log.info(f"Force-exit close time for {symbol}: {close_time.strftime('%H:%M')} NY")

        # 2. Fetch full bar window.
        now = ny_now()
        start = now - timedelta(days=days + 5)   # +5 so we have at least one prior day for the first FX day's CPR
        log.info(f"Fetching {days + 5}D of 5-min bars for {symbol} "
                 f"({iso_ny(start)} → {iso_ny(now)})")
        bars = await ibkr.fetch_5min_bars_range(symbol, start_ny=start, end_ny=now)
        log.info(f"Got {len(bars)} bars total")
    finally:
        await ibkr.disconnect()

    if not bars:
        print("No bars returned — aborting.")
        return

    buckets = bucket_by_fx_day(bars)
    fx_days_sorted = sorted(buckets.keys())
    log.info(f"Bucketed into {len(fx_days_sorted)} FX day(s)")

    # 3. Per-FX-day HLC (used to compute the daily CPR of the FOLLOWING trading FX day).
    fx_day_hlc: dict[datetime, tuple[float, float, float]] = {}
    for fd in fx_days_sorted:
        day_bars = buckets[fd]
        if not day_bars:
            continue
        H = max(b.high for b in day_bars)
        L = min(b.low for b in day_bars)
        C = day_bars[-1].close
        fx_day_hlc[fd] = (H, L, C)

    # 4. Build the strategy and feed bars chronologically. For each bar we
    #    need the CPR (TC, BC, Pivot) of its FX day, which is computed from
    #    the PRIOR TRADING FX day's HLC.
    strategy = CPRSuperTrendStrategy(
        regime_cfg=regime_cfg,
        ast_cfg=ast_cfg,
        force_exit_close_time=close_time,
        ast_bars_per_day=288,
    )

    trades: list[dict] = []
    events: list[dict] = []
    # State for assembling closed trades from event pairs.
    open_trade: Optional[dict] = None
    skipped_days = 0
    used_days = 0

    for fd in fx_days_sorted:
        # Find this FX day's "prior trading FX day" using the helper:
        # we pass a "now" near the start of this FX day (just after fd) so
        # current_fx_day_anchor(now) returns fd.
        sample_now = fd + timedelta(hours=1)   # any moment within fd
        prior_start, prior_end = prior_trading_fx_day_window(sample_now)
        # Buckets are keyed by FX-day START. prior_start = start of the prior
        # trading FX day = the bucket key we want.
        if prior_start not in fx_day_hlc:
            skipped_days += 1
            continue

        H, L, C = fx_day_hlc[prior_start]
        from cpr import compute_cpr_from_hlc
        cpr = compute_cpr_from_hlc(H, L, C)
        used_days += 1

        for b in buckets[fd]:
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

            for ev in outcome.events:
                events.append({
                    "timestamp_ny_iso": iso_ny(ev.timestamp),
                    "timestamp_ny_display": disp_ny(ev.timestamp),
                    "action": ev.action,
                    "reason": ev.reason,
                    "price": round(ev.price, 6),
                    "bias": ev.bias,
                    "regime": ev.regime,
                    "regime_directional": ev.regime_directional,
                    "active_dir": ev.active_dir,
                    "active_method": ev.active_method,
                    "new_position": ev.new_position,
                    "daily_tc": round(cpr.tc, 6),
                    "daily_bc": round(cpr.bc, 6),
                    "daily_pivot": round(cpr.pivot, 6),
                })

                # Assemble closed trades from entry/exit pairs.
                if ev.action in ("ENTRY_LONG", "ENTRY_SHORT",
                                 "REVERSE_TO_LONG", "REVERSE_TO_SHORT"):
                    if open_trade is not None:
                        # Defensive: should not happen, but log.
                        log.warning(f"Unclosed trade displaced by {ev.action} at {ev.timestamp}")
                    side = "LONG" if ev.action in ("ENTRY_LONG", "REVERSE_TO_LONG") else "SHORT"
                    open_trade = {
                        "entry_ts_iso": iso_ny(ev.timestamp),
                        "entry_ts_display": disp_ny(ev.timestamp),
                        "side": side,
                        "entry_price": round(ev.price, 6),
                        "entry_bias": ev.bias,
                        "entry_regime": ev.regime,
                        "entry_active_method": ev.active_method,
                        "was_reversal": ev.action.startswith("REVERSE_"),
                        "_entry_ts": ev.timestamp,
                    }
                elif ev.action in ("EXIT_FLIP", "EXIT_EOD"):
                    if open_trade is None:
                        continue
                    side = open_trade["side"]
                    entry_px = open_trade["entry_price"]
                    exit_px = round(ev.price, 6)
                    pts = (exit_px - entry_px) if side == "LONG" else (entry_px - exit_px)
                    bars_in_trade = int((ev.timestamp - open_trade["_entry_ts"]).total_seconds() // 300)
                    trades.append({
                        **{k: v for k, v in open_trade.items() if not k.startswith("_")},
                        "exit_ts_iso": iso_ny(ev.timestamp),
                        "exit_ts_display": disp_ny(ev.timestamp),
                        "exit_price": exit_px,
                        "exit_reason": ev.reason,
                        "exit_regime": ev.regime,
                        "exit_active_method": ev.active_method,
                        "points": round(pts, 6),
                        "bars_in_trade": bars_in_trade,
                        "win": pts > 0,
                    })
                    open_trade = None

    # ───────── PERSIST ─────────
    output_dir.mkdir(parents=True, exist_ok=True)
    base = f"{symbol}_{days}d_strategy"
    trades_csv = output_dir / f"{base}_trades.csv"
    events_csv = output_dir / f"{base}_events.csv"
    summary_md = output_dir / f"{base}_summary.md"

    write_csv(trades_csv, trades, [
        "entry_ts_iso", "entry_ts_display", "exit_ts_iso", "exit_ts_display",
        "side", "entry_price", "exit_price", "points", "win",
        "bars_in_trade", "exit_reason", "was_reversal",
        "entry_bias", "entry_regime", "exit_regime",
        "entry_active_method", "exit_active_method",
    ])
    write_csv(events_csv, events, [
        "timestamp_ny_iso", "timestamp_ny_display", "action", "reason",
        "price", "bias", "regime", "regime_directional",
        "active_dir", "active_method", "new_position",
        "daily_tc", "daily_bc", "daily_pivot",
    ])

    # Stats
    n_trades = len(trades)
    wins = sum(1 for t in trades if t["win"])
    losses = sum(1 for t in trades if not t["win"])
    win_rate = (100.0 * wins / n_trades) if n_trades else 0.0
    total_pts = sum(t["points"] for t in trades)
    avg_pts = (total_pts / n_trades) if n_trades else 0.0
    best = max((t["points"] for t in trades), default=0.0)
    worst = min((t["points"] for t in trades), default=0.0)
    avg_bars = (sum(t["bars_in_trade"] for t in trades) / n_trades) if n_trades else 0.0
    exit_breakdown = Counter(t["exit_reason"] for t in trades)
    side_breakdown = Counter(t["side"] for t in trades)
    reversal_count = sum(1 for t in trades if t["was_reversal"])

    lines = []
    lines.append(f"# Strategy Backtest — {symbol} ({days} days)\n")
    lines.append(f"Generated: {disp_ny(ny_now())}\n")
    lines.append(f"Force-exit close time: **{close_time.strftime('%H:%M')} NY**")
    lines.append(f"\n## Coverage\n")
    lines.append(f"- FX days fetched: {len(fx_days_sorted)}")
    lines.append(f"- FX days classified: **{used_days}** (skipped {skipped_days} as warm-up / missing prior CPR)")
    lines.append(f"\n## Trade summary\n")
    lines.append(f"- Total trades: **{n_trades}**")
    lines.append(f"- Wins: **{wins}**  /  Losses: **{losses}**")
    lines.append(f"- Win rate: **{win_rate:.1f}%**")
    lines.append(f"- Total points: **{total_pts:+.4f}**")
    lines.append(f"- Average / trade: **{avg_pts:+.4f}**")
    lines.append(f"- Best trade: **{best:+.4f}**")
    lines.append(f"- Worst trade: **{worst:+.4f}**")
    lines.append(f"- Avg bars in trade: **{avg_bars:.1f}**")
    lines.append(f"- Reversal entries: **{reversal_count}**")
    lines.append(f"\n### By side\n")
    lines.append("| Side | Count |")
    lines.append("|---|---:|")
    for s, n in side_breakdown.most_common():
        lines.append(f"| {s} | {n} |")
    lines.append(f"\n### By exit reason\n")
    lines.append("| Reason | Count |")
    lines.append("|---|---:|")
    for r, n in exit_breakdown.most_common():
        lines.append(f"| {r} | {n} |")
    lines.append(f"\n## Output files\n")
    lines.append(f"- Trades: `{trades_csv.name}` ({n_trades} rows)")
    lines.append(f"- Events: `{events_csv.name}` ({len(events)} rows)")
    summary_md.write_text("\n".join(lines) + "\n")

    # ───────── CONSOLE ─────────
    print()
    print("=" * 100)
    print(f"STRATEGY BACKTEST — {symbol}  ({used_days} FX days, force-exit at {close_time.strftime('%H:%M')} NY)")
    print("=" * 100)
    print(f"Trades:        {n_trades}    (wins {wins} / losses {losses}, win rate {win_rate:.1f}%)")
    print(f"Total pts:     {total_pts:+.4f}")
    print(f"Avg / trade:   {avg_pts:+.4f}")
    print(f"Best / Worst:  {best:+.4f} / {worst:+.4f}")
    print(f"Avg bars:      {avg_bars:.1f}")
    print(f"Sides:         {dict(side_breakdown)}")
    print(f"Exits:         {dict(exit_breakdown)}")
    print(f"Reversals:     {reversal_count}")
    print()
    print("Output files:")
    print(f"  {trades_csv}")
    print(f"  {events_csv}")
    print(f"  {summary_md}")
    print()


def main():
    p = argparse.ArgumentParser(description="Single-pair CPR-ST strategy backtest")
    p.add_argument("--symbol", default="CHFJPY")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--output-dir", type=Path, default=Path("backtest_output"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=56)
    # Regime
    p.add_argument("--regime-er-len", type=int, default=20)
    p.add_argument("--regime-er-enter", type=float, default=0.40)
    p.add_argument("--regime-er-exit", type=float, default=0.30)
    p.add_argument("--regime-cross-len", type=int, default=30)
    p.add_argument("--regime-cross-threshold", type=int, default=3)
    # AST
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

    asyncio.run(run(args.symbol, args.days, args.output_dir, cfg, regime_cfg, ast_cfg))


if __name__ == "__main__":
    main()
