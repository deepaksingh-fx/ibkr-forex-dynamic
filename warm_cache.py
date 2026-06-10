"""
One-time bar-cache warm: download every session pair (MIDPOINT/BID/ASK) for the
backtest window into the local cache. After this, `coil_backtest.py --offline`
runs in seconds with no IBKR.

    python warm_cache.py [start_ny end_ny]   # defaults cover Apr-Jun 2026
"""
import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from bar_cache import warm
from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient

NY = ZoneInfo("America/New_York")
# all unique pairs across Asian + London + Overlap (conversion pairs already in here)
SYMBOLS = ["AUDJPY", "NZDJPY", "AUDUSD", "NZDUSD", "USDJPY",
           "GBPJPY", "EURJPY", "CHFJPY", "USDCHF", "EURUSD", "GBPUSD"]
WHATS = ["MIDPOINT", "BID", "ASK"]


async def main():
    start = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=NY) if len(sys.argv) > 2 \
        else datetime(2026, 4, 3, tzinfo=NY)
    end = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=NY) if len(sys.argv) > 2 \
        else datetime(2026, 6, 6, tzinfo=NY)
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=70, read_only=True))
    ib = IBKRClient(cfg)
    await ib.connect()
    try:
        print(f"warming {len(SYMBOLS)} pairs x {len(WHATS)} feeds, {start.date()} -> {end.date()}")
        await warm(ib, SYMBOLS, WHATS, start, end)
        print("WARM COMPLETE")
    finally:
        await ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
