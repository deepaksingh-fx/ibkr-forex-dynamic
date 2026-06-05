"""
Print the CFD minTick (and implied decimal places) for the coil strategy's pairs.
Run on the machine with the Gateway up:

    python scripts/show_ticks.py
"""
from __future__ import annotations
import asyncio, sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ib_async import IB, Contract   # noqa: E402

PAIRS = ["AUDJPY", "NZDJPY", "USDJPY", "GBPJPY", "EURJPY", "CHFJPY",
         "AUDUSD", "NZDUSD", "USDCHF"]


async def main():
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4001, clientId=120, readonly=True, timeout=10)
    print(f"\n{'pair':8} {'minTick':>12} {'decimals':>9}   example price")
    for p in PAIRS:
        c = Contract(secType="CFD", symbol=p[:3], currency=p[3:], exchange="SMART")
        try:
            (c,) = await ib.qualifyContractsAsync(c)
            cds = await ib.reqContractDetailsAsync(c)
            tick = float(cds[0].minTick) if cds else None
            dec = abs(Decimal(str(tick)).as_tuple().exponent) if tick else "?"
            ex = (190 if p.endswith("JPY") else 1)
            example = round(round(ex / tick) * tick, 10) if tick else "?"
            print(f"{p:8} {tick!s:>12} {dec!s:>9}   {example}")
        except Exception as e:
            print(f"{p:8} ERROR: {e}")
    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
