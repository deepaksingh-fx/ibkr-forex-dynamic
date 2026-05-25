"""
Read-only diagnostic: reproduce the pivot strategy's state for one symbol over
the current FX day and dump a per-bar trace, to compare against the live bot.

Connects on a separate clientId (read-only), fetches forex bars (same source
the live decisions use), recomputes the 9 pivot levels, and replays the strategy
exactly like pivot_runtime does - printing base / ST state / bias / position per
bar so we can verify the live entry.

    python scripts/pivot_diag.py --symbol AUDJPY
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import time as dtime
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient
from pivot_st_strategy import PivotSuperTrendStrategy
from pivots import compute_pivots
from quality_supertrend import QSTConfig, QualitySuperTrend
from time_utils import (
    current_fx_day_anchor,
    ny_now,
    prior_trading_fx_day_window,
    to_ny,
)


def ts_ny(b):
    t = b.date
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return to_ny(t)


async def main(symbol: str, warmup_days: int):
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=79, read_only=True))
    ib = IBKRClient(cfg)
    await ib.connect()
    try:
        now = ny_now()
        start = now - timedelta(days=warmup_days)
        bars = await ib.fetch_5min_bars_range(symbol, start_ny=start, end_ny=now)  # forex/IDEALPRO
    finally:
        await ib.disconnect()

    print(f"\n[diag] {symbol}: fetched {len(bars)} bars up to {now.isoformat()}")

    # Build per-FX-day HLC cache (same as pivot_runtime).
    hlc = {}
    for b in bars:
        fxs, _ = current_fx_day_anchor(ts_ny(b))
        H, L, C = float(b.high), float(b.low), float(b.close)
        if fxs in hlc:
            h, l, _ = hlc[fxs]
            hlc[fxs] = (max(h, H), min(l, L), C)
        else:
            hlc[fxs] = (H, L, C)

    def pivots_for(t):
        fxs, _ = current_fx_day_anchor(t)
        ps, _ = prior_trading_fx_day_window(fxs + timedelta(hours=1))
        if ps not in hlc:
            return None
        return compute_pivots(*hlc[ps])

    cur_fxs, _ = current_fx_day_anchor(now)
    prior_start, _ = prior_trading_fx_day_window(cur_fxs + timedelta(hours=1))
    print(f"[diag] current FX day: {cur_fxs.isoformat()}")
    print(f"[diag] prior trading FX day start: {prior_start.isoformat()}  HLC={hlc.get(prior_start)}")
    piv = pivots_for(cur_fxs + timedelta(hours=1))
    if piv:
        print("[diag] levels for current FX day:")
        for n, v in piv.as_levels():
            print(f"        {n:>2} = {v:.5f}")

    # Replay through the strategy + a parallel ST probe for raw indicator values.
    strat = PivotSuperTrendStrategy(qst_cfg=QSTConfig(), force_exit_close_time=dtime(16, 55))
    probe = QualitySuperTrend(QSTConfig())
    bars.sort(key=ts_ny)
    print(f"\n[diag] per-bar trace for current FX day ({cur_fxs.date()} session):")
    print("  time         close      base         bias   st     dir  ADX    EMA50      STline    pos  events")
    for b in bars:
        t = ts_ny(b)
        p = pivots_for(t)
        if p is None:
            continue
        fxs, _ = current_fx_day_anchor(t)
        snap = probe.update(t, float(b.open), float(b.high), float(b.low), float(b.close))
        out = strat.update(timestamp=t, open_=float(b.open), high=float(b.high),
                           low=float(b.low), close=float(b.close), pivots=p, fx_day_start=fxs)
        if t >= cur_fxs:
            acts = [e.action for e in out.events]
            adx = f"{snap.adx:5.1f}" if snap.adx is not None else "  -  "
            ema = f"{snap.ema_fast:.5f}" if snap.ema_fast is not None else "   -   "
            stl = f"{snap.supertrend:.5f}" if snap.supertrend is not None else "   -   "
            base = f"{out.base_level_name}@{out.base_level:.5f}" if out.base_level is not None else "none"
            print(f"  {t.strftime('%a %H:%M')}  {b.close:9.5f}  {base:<12} {out.bias:<5}  "
                  f"{out.st_state:<5} {out.st_dir:+d}  {adx}  {ema}  {stl}  "
                  f"{out.position_before:+d}->{out.position_after:+d}  {acts}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="AUDJPY")
    ap.add_argument("--warmup-days", type=int, default=60)
    a = ap.parse_args()
    asyncio.run(main(a.symbol, a.warmup_days))
