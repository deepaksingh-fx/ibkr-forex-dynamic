"""
Test whether rounding IBKR JPY bars to 3 decimals (as TradingView does) explains
the ~0.05 ADX gap. Computes ADX on full-precision vs 3dp-rounded MIDPOINT bars.
Read-only, clientId 83.
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


def adx_series(bars, ndigits=None):
    dmi = DMI(14, 14)
    out = {}
    for b in bars:
        h, l, c = float(b.high), float(b.low), float(b.close)
        if ndigits is not None:
            h, l, c = round(h, ndigits), round(l, ndigits), round(c, ndigits)
        _, _, a = dmi.update(h, l, c)
        out[ts_ny(b)] = a
    return out


async def main(symbol):
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=83, read_only=True, connect_timeout=30.0))
    ib = IBKRClient(cfg)
    await ib.connect()
    try:
        contract = await ib.qualify_forex(symbol)
        bars = await ib.ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr="30 D",
            barSizeSetting="5 mins", whatToShow="MIDPOINT", useRTH=False, formatDate=2)
        bars = sorted(list(bars), key=ts_ny)
    finally:
        await ib.disconnect()

    full = adx_series(bars, None)
    r3 = adx_series(bars, 3)
    tv = {"22:15": 17.558, "22:20": 18.828, "22:25": 20.006}

    print(f"\n[adx_round] {symbol}  full-precision vs 3dp-rounded ADX vs TradingView\n")
    print("  time         full(5dp)   rounded(3dp)   TV")
    for t in sorted(full.keys()):
        hm = t.strftime("%H:%M")
        if hm in ("22:10","22:15","22:20","22:25","22:30","22:35") and t.strftime("%a") == "Sun":
            f = f"{full[t]:.5f}" if full[t] is not None else "  -  "
            r = f"{r3[t]:.5f}" if r3[t] is not None else "  -  "
            v = f"{tv[hm]:.3f}" if hm in tv else "  -  "
            print(f"  {t.strftime('%a %H:%M')}   {f}   {r}      {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--symbol", default="AUDJPY")
    args = ap.parse_args()
    asyncio.run(main(args.symbol))
