"""
Compute ADX(14,14) for AUDJPY from IBKR using three price sources - MIDPOINT,
BID, ASK - and print recent bars side by side. Tests whether the bot's ADX
(MIDPOINT) differs from a TradingView chart purely because of the price source
(midpoint range compression).

Read-only, clientId 81.
    python scripts/adx_sources.py --symbol AUDJPY
"""
from __future__ import annotations
import argparse, asyncio, sys
from datetime import timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient
from quality_supertrend import DMI
from time_utils import to_ny


def ts_ny(b):
    t = b.date
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return to_ny(t)


def adx_series(bars):
    """Return {ts_ny: (adx, high, low, close)} using our canonical DMI(14,14)."""
    dmi = DMI(14, 14)
    out = {}
    for b in sorted(bars, key=ts_ny):
        _, _, a = dmi.update(float(b.high), float(b.low), float(b.close))
        out[ts_ny(b)] = (a, float(b.high), float(b.low), float(b.close))
    return out


async def main(symbol):
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=81, read_only=True, connect_timeout=30.0))
    ib = IBKRClient(cfg)
    await ib.connect()
    try:
        contract = await ib.qualify_forex(symbol)
        async def fetch(show):
            bars = await ib.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="12 D",
                barSizeSetting="5 mins", whatToShow=show, useRTH=False, formatDate=2)
            return list(bars)
        mid = await fetch("MIDPOINT")
        bid = await fetch("BID")
        ask = await fetch("ASK")
    finally:
        await ib.disconnect()

    m = adx_series(mid); b = adx_series(bid); a = adx_series(ask)
    times = sorted(m.keys())[-40:]
    print(f"\n[adx_sources] {symbol}  ADX(14,14) by price source\n")
    print("  time          MID:  H        L        C        adx   |  BID adx |  ASK adx")
    for t in times:
        ma, mh, ml, mc = m.get(t, (None,)*4)
        ba = b.get(t, (None,))[0]
        aa = a.get(t, (None,))[0]
        def f(x, w=7, p=3):
            return f"{x:{w}.{p}f}" if x is not None else " " * w
        print(f"  {t.strftime('%a %H:%M')}   {f(mh,8,5)} {f(ml,8,5)} {f(mc,8,5)} {f(ma,6,2)}  | {f(ba,6,2)}  | {f(aa,6,2)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--symbol", default="AUDJPY")
    args = ap.parse_args()
    asyncio.run(main(args.symbol))
