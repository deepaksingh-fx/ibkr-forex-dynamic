"""
Validate our DMI ADX against talib + pandas_ta on identical IBKR MIDPOINT bars.
If all three agree, our formula is canonical and any gap vs TradingView is data.
Read-only, clientId 82.
    python scripts/adx_validate.py --symbol AUDJPY
"""
from __future__ import annotations
import argparse, asyncio, sys
from datetime import timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import talib
import pandas_ta as pta

from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient
from quality_supertrend import DMI
from time_utils import to_ny


def ts_ny(b):
    t = b.date
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return to_ny(t)


async def main(symbol):
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=82, read_only=True, connect_timeout=30.0))
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

    H = np.array([float(b.high) for b in bars])
    L = np.array([float(b.low) for b in bars])
    C = np.array([float(b.close) for b in bars])

    # ours
    dmi = DMI(14, 14)
    ours = []
    for h, l, c in zip(H, L, C):
        _, _, a = dmi.update(float(h), float(l), float(c))
        ours.append(a)

    # talib
    tl = talib.ADX(H, L, C, timeperiod=14)

    # pandas_ta
    df = pd.DataFrame({"high": H, "low": L, "close": C})
    adf = df.ta.adx(length=14)
    pt = adf["ADX_14"].to_numpy()

    print(f"\n[adx_validate] {symbol}  ADX(14) on identical IBKR MIDPOINT bars\n")
    print("  time         ours        talib       pandas_ta")
    for i, b in enumerate(bars):
        t = ts_ny(b)
        if t.strftime("%H:%M") in ("22:10","22:15","22:20","22:25","22:30","22:35"):
            o = f"{ours[i]:.5f}" if ours[i] is not None else "  -  "
            tv = f"{tl[i]:.5f}" if not np.isnan(tl[i]) else "  -  "
            pv = f"{pt[i]:.5f}" if not np.isnan(pt[i]) else "  -  "
            print(f"  {t.strftime('%a %H:%M')}   {o}   {tv}   {pv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--symbol", default="AUDJPY")
    args = ap.parse_args()
    asyncio.run(main(args.symbol))
