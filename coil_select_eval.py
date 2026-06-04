"""
One-off evaluator for the coil-index session strategy. For a given IST date it
runs the pair selection for each session and prints the chosen pair, exactly as
the live runtime would at each session's first-candle close.

    python coil_select_eval.py                       # yesterday (IST), default config
    python coil_select_eval.py --date 2026-06-03
    python coil_select_eval.py --session Asian       # one session only
    python coil_select_eval.py --config coil_config.json
    python coil_select_eval.py --adx-filter-max 25 --adx-coil-max 18

Sessions, pairs, and ADX thresholds/periods are all params: edit coil_config.json
(--config) or override the thresholds on the CLI. Read-only -- connects to IB
Gateway, fetches history, places NO orders. Requires a running Gateway
(default 127.0.0.1:4001).

Per session pair: fetch 5-min MIDPOINT bars up to the first-candle close, compute
the daily CPR from the prior completed FX day, build coil metrics (DMI di/adx len,
ADX<adx_coil_max over the last coil_window_bars), apply the ADX<adx_filter_max
filter, and select the lowest coil index.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from coil_config import load_coil_config
from coil_selection import CoilParams, PairCoil, coil_select, compute_pair_coil
from config import IBKRConnection, StrategyConfig
from cpr import CPR, compute_cpr_from_bars
from ibkr_client import IBKRClient
from selection import SelectionError
from sessions import IST, Session, first_candle_close
from time_utils import NY, prior_trading_fx_day_window, to_ny

logger = logging.getLogger("coil_eval")

# Extra days of 5-min history fetched before the first-candle close, on top of
# the coil window, to seed DMI and cover the prior FX day used for CPR.
FETCH_PAD_DAYS = 3


def _bar_open_ny(bar) -> datetime:
    ts = bar.date
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    return to_ny(ts)


def _cpr_from_prior_fx_day(bars, anchor_ny: datetime) -> CPR:
    """Daily CPR from the most-recent completed trading FX day before the anchor."""
    win_start, win_end = prior_trading_fx_day_window(anchor_ny)
    highs, lows, closes = [], [], []
    for b in bars:
        t = _bar_open_ny(b)
        if win_start <= t < win_end:
            highs.append(b.high)
            lows.append(b.low)
            closes.append(b.close)
    if not highs:
        raise SelectionError(
            f"no bars in prior FX day window [{win_start:%Y-%m-%d %H:%M}..{win_end:%H:%M} NY]"
        )
    return compute_cpr_from_bars(highs, lows, closes)


async def _metrics_for_session(
    ibkr: IBKRClient, session: Session, session_date: date,
    params: CoilParams, use_cfd: bool,
) -> List[PairCoil]:
    close_ist = first_candle_close(session, session_date)
    anchor_ny = close_ist.astimezone(NY)
    fetch_days = params.coil_window_bars / 288.0 + FETCH_PAD_DAYS
    fetch_start_ny = anchor_ny - timedelta(days=fetch_days)

    metrics: List[PairCoil] = []
    for symbol in session.pairs:
        try:
            bars = await ibkr.fetch_5min_bars_range(
                symbol, start_ny=fetch_start_ny, end_ny=anchor_ny, use_cfd=use_cfd
            )
            bars = [b for b in bars if _bar_open_ny(b) < anchor_ny]
            if not bars:
                logger.warning("  %-7s: no bars returned", symbol)
                continue
            cpr = _cpr_from_prior_fx_day(bars, anchor_ny)
            m = compute_pair_coil(
                symbol, bars, cpr,
                di_len=params.di_len, adx_len=params.adx_len,
                adx_coil_max=params.adx_coil_max,
                coil_window_bars=params.coil_window_bars,
            )
            metrics.append(m)
            flag = "" if m.adx_now < params.adx_filter_max else f"  [FILTERED ADX>={params.adx_filter_max:g}]"
            logger.info(
                "  %-7s adx=%5.1f (DI+%.1f/DI-%.1f) width%%=%.4f low/total=%d/%d coil=%.5f%s",
                symbol, m.adx_now, m.plus_di, m.minus_di, m.width_pct,
                m.low_adx_bars, m.total_adx_bars, m.coil_index, flag,
            )
        except Exception as e:  # noqa: BLE001 - report & continue to next pair
            logger.warning("  %-7s: skipped (%s)", symbol, e)
    return metrics


async def _evaluate(ibkr: IBKRClient, sessions: Tuple[Session, ...],
                    params: CoilParams, feed: str, date_ist: date,
                    only: Optional[str]) -> None:
    use_cfd = feed == "cfd"
    print(f"\n=== Coil selection for IST date {date_ist}  [feed={feed}] "
          f"(adx_filter<{params.adx_filter_max:g}, adx_coil<{params.adx_coil_max:g}, "
          f"window={params.coil_window_bars}) ===\n")
    for session in sessions:
        if only and session.name.lower() != only.lower():
            continue
        logger.info(
            "[%s] %s-%s IST  candidates: %s",
            session.name, session.start.strftime("%H:%M"),
            session.end.strftime("%H:%M"), ", ".join(session.pairs),
        )
        metrics = await _metrics_for_session(ibkr, session, date_ist, params, use_cfd)
        try:
            winner = coil_select(metrics, adx_filter_max=params.adx_filter_max)
            print(f"[{session.name:<14}] SELECTED -> {winner.symbol}"
                  f"  (coil={winner.coil_index:.5f}, adx_now={winner.adx_now:.1f})")
        except SelectionError as e:
            print(f"[{session.name:<14}] NO SELECTION ({e})")
        print()


async def run(args) -> None:
    sessions, params, feed = load_coil_config(args.config)
    if args.feed:
        feed = args.feed
    # CLI overrides win over the config file.
    overrides = {}
    if args.adx_filter_max is not None:
        overrides["adx_filter_max"] = args.adx_filter_max
    if args.adx_coil_max is not None:
        overrides["adx_coil_max"] = args.adx_coil_max
    if args.coil_window_bars is not None:
        overrides["coil_window_bars"] = args.coil_window_bars
    if overrides:
        params = replace(params, **overrides)

    if args.date:
        date_ist = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        date_ist = (datetime.now(IST) - timedelta(days=1)).date()

    cfg = StrategyConfig(ibkr=IBKRConnection(
        host=args.host, port=args.port, client_id=args.client_id, read_only=True,
    ))
    ibkr = IBKRClient(cfg)
    await ibkr.connect()
    try:
        await _evaluate(ibkr, sessions, params, feed, date_ist, args.session)
    finally:
        await ibkr.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description="Coil-index session selection evaluator (read-only)")
    p.add_argument("--date", help="IST session date YYYY-MM-DD (default: yesterday IST)")
    p.add_argument("--session", help="Only evaluate this session by name")
    p.add_argument("--config", help="Path to JSON config (sessions + params)")
    p.add_argument("--feed", choices=("cfd", "midpoint"),
                   help="Price feed for ADX/CPR (default: cfd / from config)")
    p.add_argument("--adx-filter-max", type=float, help="Override: keep pairs with ADX < this (default 30)")
    p.add_argument("--adx-coil-max", type=float, help="Override: count 24h bars with ADX < this (default 20)")
    p.add_argument("--coil-window-bars", type=int, help="Override: bars in the low-ADX window (default 288)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=51)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
