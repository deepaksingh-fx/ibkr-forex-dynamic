"""
Historical regime backtest for a single pair.

Fetches `--days` (default 8) of 5-min bars from IBKR, buckets them by FX day
(17:00 NY rollover), computes each FX day's daily PP from its own HLC, then
feeds chronologically to the regime classifier using the PRIOR FX day's PP
for each bar (matching the Pine indicator semantics).

Prints two reports:
  1. Zone-change events - every transition between regimes
  2. Per-FX-day summary - bar counts per regime

No trades. No orders. Read-only.

Usage:
    python scripts/regime_backtest.py --symbol CHFJPY --days 8
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

# Allow running as `python scripts/regime_backtest.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    """Group bars by FX-day-start anchor. Preserves input order within each bucket."""
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


def format_ts(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M %Z")


async def run(symbol: str, days: int, cfg: StrategyConfig, regime_cfg: RegimeConfig):
    log = logging.getLogger("regime_backtest")
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        now = ny_now()
        log.info(f"Fetching {days}D of 5-min bars for {symbol} (end={now.isoformat()})")
        bars = await ibkr.fetch_5min_bars(symbol, end_ny=now, duration_str=f"{days} D")
        log.info(f"Got {len(bars)} bars")
    finally:
        await ibkr.disconnect()

    if not bars:
        print("No bars returned from IBKR - nothing to backtest.")
        return

    buckets = bucket_by_fx_day(bars)
    fx_days = list(buckets.keys())
    log.info(f"Bucketed into {len(fx_days)} FX day(s):")
    for fd in fx_days:
        log.info(f"  {format_ts(fd)}  bars={len(buckets[fd])}")

    # Compute daily HLC + PP per FX day from its OWN bars.
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

    # Feed bars chronologically; use the PRIOR FX day's PP for each bar.
    # The first FX day in our window has no prior PP -> skip those bars (treated
    # as additional warm-up).
    classifier = RegimeClassifier(regime_cfg)
    transitions: list = []
    per_day_counts: dict[datetime, Counter] = {}

    prev_regime: Regime | None = None
    transition_start_ts: datetime | None = None
    transition_start_snap = None

    skipped_first_day_bars = 0
    classified_bars = 0

    for i, fd in enumerate(fx_days):
        if i == 0:
            skipped_first_day_bars = len(buckets[fd])
            log.info(
                f"Skipping first FX day {format_ts(fd)} ({skipped_first_day_bars} bars) "
                f"- need a prior day for PP."
            )
            continue
        prior_fd = fx_days[i - 1]
        if prior_fd not in day_pp:
            log.warning(f"No PP available for prior FX day {format_ts(prior_fd)} - skipping {format_ts(fd)}")
            continue
        pp = day_pp[prior_fd]

        for b in buckets[fd]:
            ts = b.date
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)
            ts_ny = to_ny(ts)
            snap = classifier.update(ts_ny, float(b.close), pp, fd)
            classified_bars += 1
            per_day_counts.setdefault(fd, Counter())[snap.regime.value] += 1

            if snap.regime != prev_regime:
                # Close out the previous transition span.
                if prev_regime is not None and transition_start_snap is not None:
                    transitions.append({
                        "from_ts": transition_start_ts,
                        "to_ts": ts_ny,            # exclusive: new regime takes over here
                        "regime": prev_regime,
                        "bars": classified_bars - transition_start_snap["bar_idx"],
                        "er_at_end": transition_start_snap["last_er"],
                        "crossings_at_end": transition_start_snap["last_cross"],
                    })
                prev_regime = snap.regime
                transition_start_ts = ts_ny
                transition_start_snap = {"bar_idx": classified_bars - 1, "last_er": snap.er, "last_cross": snap.crossings}
            else:
                # Same regime -> update "last seen" metrics so end-of-span shows latest.
                if transition_start_snap is not None:
                    transition_start_snap["last_er"] = snap.er
                    transition_start_snap["last_cross"] = snap.crossings

    # Close out the final transition.
    if prev_regime is not None and transition_start_snap is not None and classified_bars > 0:
        last_ts = transition_start_ts  # fallback
        # Find true last bar timestamp.
        for fd in reversed(fx_days):
            if fd in buckets and buckets[fd]:
                lb = buckets[fd][-1]
                lts = lb.date
                if lts.tzinfo is None:
                    from datetime import timezone as _tz
                    lts = lts.replace(tzinfo=_tz.utc)
                last_ts = to_ny(lts)
                break
        transitions.append({
            "from_ts": transition_start_ts,
            "to_ts": last_ts,
            "regime": prev_regime,
            "bars": classified_bars - transition_start_snap["bar_idx"],
            "er_at_end": transition_start_snap["last_er"],
            "crossings_at_end": transition_start_snap["last_cross"],
        })

    # --------- Reports ---------
    print()
    print("=" * 100)
    print(f"REGIME BACKTEST - {symbol}   ({classified_bars} classified bars, "
          f"skipped {skipped_first_day_bars} warm-up bars from first FX day)")
    print(f"Config: ER len={regime_cfg.er_len} enter={regime_cfg.er_enter} exit={regime_cfg.er_exit} "
          f"| Crossings len={regime_cfg.cross_len} threshold={regime_cfg.cross_threshold}")
    print("=" * 100)

    print()
    print("ZONE-CHANGE EVENTS")
    print("-" * 100)
    print(f"{'FROM':<30} {'TO':<30} {'REGIME':<18} {'BARS':>5}  {'ER@end':>8}  {'PPx@end':>8}")
    print("-" * 100)
    for t in transitions:
        er_str = f"{t['er_at_end']:.3f}" if t['er_at_end'] is not None else "  n/a "
        print(
            f"{format_ts(t['from_ts']):<30} "
            f"{format_ts(t['to_ts']):<30} "
            f"{t['regime'].value:<18} "
            f"{t['bars']:>5}  "
            f"{er_str:>8}  "
            f"{t['crossings_at_end']:>8}"
        )
    print()

    print()
    print("PER-FX-DAY REGIME DISTRIBUTION (bar counts)")
    print("-" * 100)
    all_regimes = [
        Regime.DIRECTIONAL_UP, Regime.DIRECTIONAL_DOWN,
        Regime.MEAN_REVERTING, Regime.NON_DIRECTIONAL, Regime.WARMING_UP,
    ]
    header = "FX DAY".ljust(30) + "".join(f"{r.value:>18}" for r in all_regimes) + f"{'TOTAL':>8}"
    print(header)
    print("-" * 100)
    for fd, ctr in per_day_counts.items():
        line = f"{format_ts(fd):<30}"
        for r in all_regimes:
            line += f"{ctr.get(r.value, 0):>18}"
        line += f"{sum(ctr.values()):>8}"
        print(line)
    print()


def main():
    p = argparse.ArgumentParser(description="Historical regime backtest for one symbol")
    p.add_argument("--symbol", default="CHFJPY")
    p.add_argument("--days", type=int, default=8,
                   help="Days of 5-min history to fetch (default 8 - gives 6-7 classifiable FX days)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=53)
    # ER / crossings tunables.
    p.add_argument("--er-len", type=int, default=20)
    p.add_argument("--er-enter", type=float, default=0.40)
    p.add_argument("--er-exit", type=float, default=0.30)
    p.add_argument("--cross-len", type=int, default=30)
    p.add_argument("--cross-threshold", type=int, default=3)
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
        er_len=args.er_len, er_enter=args.er_enter, er_exit=args.er_exit,
        cross_len=args.cross_len, cross_threshold=args.cross_threshold,
    )

    asyncio.run(run(args.symbol, args.days, cfg, regime_cfg))


if __name__ == "__main__":
    main()
