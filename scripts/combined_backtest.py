"""
Combined regime + AdaptiveSuperTrend backtest for one symbol.

Fetches `--days` (default 90) of 5-min bars from IBKR (chunked + deduped),
buckets them by FX day, then feeds chronologically to BOTH:
  - RegimeClassifier (regime.py) - uses prior FX day's pivot
  - AdaptiveSuperTrend (adaptive_supertrend.py) - 6-method auto-select

Persisted outputs (written to `--output-dir`, default `backtest_output/`):
  1. <symbol>_<days>d_regime_transitions.csv  - every regime change
  2. <symbol>_<days>d_ast_flips.csv             - every active-ST direction flip
  3. <symbol>_<days>d_summary.md                - human-readable overview

All timestamps in NY tz, with both ISO 8601 (with offset) and human-readable
columns (e.g. "Fri 2026-05-15 10:55 EDT"). DST handled automatically.

No trades. Read-only.

Usage:
    python scripts/combined_backtest.py --symbol CHFJPY --days 90
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adaptive_supertrend import (
    METHOD_NAMES,
    AdaptiveSTConfig,
    AdaptiveSuperTrend,
)
from config import IBKRConnection, StrategyConfig
from cpr import compute_cpr_from_bars
from ibkr_client import IBKRClient
from regime import Regime, RegimeClassifier, RegimeConfig
from time_utils import current_fx_day_anchor, ny_now, to_ny


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
    log = logging.getLogger("combined_backtest")
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        now = ny_now()
        start = now - timedelta(days=days + 1)
        log.info(f"Fetching {days}D of 5-min bars for {symbol} "
                 f"({iso_ny(start)} -> {iso_ny(now)})")
        bars = await ibkr.fetch_5min_bars_range(symbol, start_ny=start, end_ny=now)
        log.info(f"Got {len(bars)} bars total")
    finally:
        await ibkr.disconnect()

    if not bars:
        print("No bars returned - aborting.")
        return

    buckets = bucket_by_fx_day(bars)
    fx_days = list(buckets.keys())
    log.info(f"Bucketed into {len(fx_days)} FX day(s)")

    day_pp: dict[datetime, float] = {}
    for fd, day_bars in buckets.items():
        if not day_bars:
            continue
        cpr = compute_cpr_from_bars(
            [b.high for b in day_bars],
            [b.low for b in day_bars],
            [b.close for b in day_bars],
        )
        day_pp[fd] = cpr.pivot

    regime_clf = RegimeClassifier(regime_cfg)
    ast = AdaptiveSuperTrend(ast_cfg, bars_per_day=288)

    regime_transitions: list[dict] = []
    ast_flips: list[dict] = []

    prev_regime: Optional[Regime] = None
    regime_start_ts: Optional[datetime] = None
    prev_active_dir: int = 0
    prev_active_method_idx: Optional[int] = None
    last_regime_at_flip: str = "?"
    classified_bars = 0
    skipped_warmup = 0
    last_ts: Optional[datetime] = None

    for i, fd in enumerate(fx_days):
        if i == 0:
            skipped_warmup = len(buckets[fd])
            log.info(f"Skipping first FX day {disp_ny(fd)} ({skipped_warmup} bars) as warm-up")
            continue
        prior_fd = fx_days[i - 1]
        if prior_fd not in day_pp:
            continue
        pp = day_pp[prior_fd]

        for b in buckets[fd]:
            ts = b.date
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)
            ts_ny = to_ny(ts)
            close = float(b.close)
            high = float(b.high)
            low = float(b.low)
            open_ = float(b.open)
            last_ts = ts_ny

            # Regime classifier
            r_snap = regime_clf.update(ts_ny, close, pp, fd)
            if r_snap.regime != prev_regime:
                if prev_regime is not None and regime_start_ts is not None:
                    duration_bars = int((ts_ny - regime_start_ts).total_seconds() // 300)
                    regime_transitions.append({
                        "from_ts_iso": iso_ny(regime_start_ts),
                        "from_ts_display": disp_ny(regime_start_ts),
                        "to_ts_iso": iso_ny(ts_ny),
                        "to_ts_display": disp_ny(ts_ny),
                        "regime": prev_regime.value,
                        "duration_bars": duration_bars,
                        "duration_minutes": duration_bars * 5,
                    })
                prev_regime = r_snap.regime
                regime_start_ts = ts_ny
            current_regime_str = r_snap.regime.value

            # Adaptive SuperTrend
            a_snap = ast.update(ts_ny, open_, high, low, close)
            classified_bars += 1

            # Active SuperTrend FLIP detection (bar-over-bar change in the
            # active-method's direction, after warm-up).
            cur_dir = a_snap.active_dir
            cur_method_idx = a_snap.active_method_idx
            if cur_dir != 0 and prev_active_dir != 0 and cur_dir != prev_active_dir:
                # True iff the active method changed on this exact bar - then
                # the apparent direction flip is really a method swap, not the
                # current SuperTrend internally flipping.
                method_switched = (
                    prev_active_method_idx is not None
                    and cur_method_idx != prev_active_method_idx
                )
                ast_flips.append({
                    "timestamp_ny_iso": iso_ny(ts_ny),
                    "timestamp_ny_display": disp_ny(ts_ny),
                    "flip": "DOWN_TO_UP" if cur_dir == 1 else "UP_TO_DOWN",
                    "new_direction": "UP" if cur_dir == 1 else "DOWN",
                    "price": round(close, 6),
                    "active_st": round(a_snap.active_st, 6) if a_snap.active_st is not None else "",
                    "active_atr": round(a_snap.active_atr, 6) if a_snap.active_atr is not None else "",
                    "active_method": METHOD_NAMES[cur_method_idx],
                    "regime_at_flip": current_regime_str,
                    "is_method_switch_flip": "true" if method_switched else "false",
                })
                last_regime_at_flip = current_regime_str
            if cur_dir != 0:
                prev_active_dir = cur_dir
            prev_active_method_idx = cur_method_idx

    # Close out final regime span.
    if prev_regime is not None and regime_start_ts is not None and last_ts is not None:
        duration_bars = int((last_ts - regime_start_ts).total_seconds() // 300)
        regime_transitions.append({
            "from_ts_iso": iso_ny(regime_start_ts),
            "from_ts_display": disp_ny(regime_start_ts),
            "to_ts_iso": iso_ny(last_ts),
            "to_ts_display": disp_ny(last_ts),
            "regime": prev_regime.value,
            "duration_bars": duration_bars,
            "duration_minutes": duration_bars * 5,
        })

    # --------- PERSIST ---------
    output_dir.mkdir(parents=True, exist_ok=True)
    base = f"{symbol}_{days}d"
    regime_csv = output_dir / f"{base}_regime_transitions.csv"
    flips_csv = output_dir / f"{base}_ast_flips.csv"
    summary_md = output_dir / f"{base}_summary.md"

    write_csv(regime_csv, regime_transitions, [
        "from_ts_iso", "from_ts_display",
        "to_ts_iso", "to_ts_display",
        "regime", "duration_bars", "duration_minutes",
    ])
    write_csv(flips_csv, ast_flips, [
        "timestamp_ny_iso", "timestamp_ny_display",
        "flip", "new_direction", "price",
        "active_st", "active_atr", "active_method",
        "regime_at_flip", "is_method_switch_flip",
    ])

    regime_counts = Counter(rt["regime"] for rt in regime_transitions)
    flip_counts = Counter(fl["flip"] for fl in ast_flips)
    method_at_flip = Counter(fl["active_method"] for fl in ast_flips)
    regime_at_flip_counts = Counter(fl["regime_at_flip"] for fl in ast_flips)

    lines = []
    lines.append(f"# Combined Backtest - {symbol} ({days} days)\n")
    lines.append(f"Generated: {disp_ny(ny_now())}\n")
    lines.append(f"\n## Coverage\n")
    lines.append(f"- Bars classified: **{classified_bars:,}**  (warm-up skipped: {skipped_warmup})")
    lines.append(f"- FX days covered: **{len(fx_days) - 1}**")
    if last_ts is not None:
        lines.append(f"- Time range: {disp_ny(fx_days[1])} -> {disp_ny(last_ts)}")
    lines.append(f"\n## Regime classifier - segment counts\n")
    lines.append(f"Total transitions: **{len(regime_transitions)}**\n")
    lines.append("| Regime | Segments |")
    lines.append("|---|---:|")
    for r, n in regime_counts.most_common():
        lines.append(f"| {r} | {n} |")
    lines.append(f"\n## Adaptive SuperTrend - direction flips\n")
    lines.append(f"Total flips: **{len(ast_flips)}**\n")
    lines.append("| Flip | Count |")
    lines.append("|---|---:|")
    for f, n in flip_counts.most_common():
        lines.append(f"| {f} | {n} |")
    lines.append("\nFlips by active method at moment of flip:\n")
    lines.append("| Method | Flips |")
    lines.append("|---|---:|")
    for m, n in method_at_flip.most_common():
        lines.append(f"| {m} | {n} |")
    lines.append("\nFlips by regime at moment of flip:\n")
    lines.append("| Regime | Flips |")
    lines.append("|---|---:|")
    for r, n in regime_at_flip_counts.most_common():
        lines.append(f"| {r} | {n} |")
    lines.append(f"\n## Output files\n")
    lines.append(f"- Regime transitions: `{regime_csv.name}` ({len(regime_transitions)} rows)")
    lines.append(f"- AST flips: `{flips_csv.name}` ({len(ast_flips)} rows)")
    summary_md.write_text("\n".join(lines) + "\n")

    # --------- CONSOLE ---------
    print()
    print("=" * 100)
    print(f"COMBINED BACKTEST - {symbol}   {classified_bars:,} classified bars  "
          f"({len(fx_days) - 1} FX days)")
    print("=" * 100)
    print()
    print(f"Regime: {len(regime_transitions)} segments  "
          f"{dict(regime_counts.most_common())}")
    print(f"AST flips: {len(ast_flips)}  {dict(flip_counts.most_common())}")
    print()
    print("Output files (NY-tz timestamps in ISO + human-readable columns):")
    print(f"  {regime_csv}")
    print(f"  {flips_csv}")
    print(f"  {summary_md}")
    print()


def main():
    p = argparse.ArgumentParser(description="Combined regime + AST backtest")
    p.add_argument("--symbol", default="CHFJPY")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--output-dir", type=Path, default=Path("backtest_output"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=55)
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

    asyncio.run(run(args.symbol, args.days, args.output_dir, cfg, regime_cfg, ast_cfg))


if __name__ == "__main__":
    main()
