"""
Hands-off 2-year backtest: warm the bar cache (resumable, gentle pacing),
backtest offline, build the per-session report. Cross-platform (mac/Windows).

    python coil_2year.py

Progress (with timestamps + elapsed) is printed AND written to coil_2year.log,
so you can watch it live:   tail -f coil_2year.log   (or just open the file)

Needs IB Gateway up. Re-running is safe - it resumes from whatever's cached.
"""
import asyncio
import glob
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
NY = ZoneInfo("America/New_York")
LOG = os.path.join(HERE, "coil_2year.log")

SYMBOLS = ["AUDJPY", "NZDJPY", "AUDUSD", "NZDUSD", "USDJPY",
           "GBPJPY", "EURJPY", "CHFJPY", "USDCHF", "EURUSD", "GBPUSD"]
WHATS = ["MIDPOINT", "BID", "ASK"]
N_SERIES = len(SYMBOLS) * len(WHATS)              # 33
WARM_START = datetime(2024, 5, 28, tzinfo=NY)
WARM_END = datetime(2026, 6, 11, tzinfo=NY)
BT_START, BT_END = date(2024, 6, 3), date(2026, 6, 5)

_T0 = time.time()


def _el():
    s = int(time.time() - _T0)
    return f"{s//3600}h{(s % 3600)//60:02d}m{s % 60:02d}s"


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')} | +{_el()}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _cached():
    from bar_cache import CACHE_DIR
    return len(glob.glob(os.path.join(CACHE_DIR, "*.json")))


async def warm_phase():
    from bar_cache import warm
    from config import IBKRConnection, StrategyConfig
    from ibkr_client import IBKRClient
    for attempt in range(1, 41):
        n = _cached()
        log(f"[1/3 warm] pass {attempt}: {n}/{N_SERIES} series cached")
        if n >= N_SERIES:
            break
        try:
            ib = IBKRClient(StrategyConfig(ibkr=IBKRConnection(client_id=74, read_only=True)))
            await ib.connect()
            await warm(ib, SYMBOLS, WHATS, WARM_START, WARM_END, logfn=log)
            await ib.disconnect()
        except Exception as e:
            log(f"[1/3 warm] pass {attempt} connection error: {e} (retrying after pause)")
        if _cached() < N_SERIES:
            await asyncio.sleep(30)      # let any pacing penalty clear between passes
    log(f"[1/3 warm] DONE: {_cached()}/{N_SERIES} series cached")


def backtest_phase():
    dates = []
    d = BT_START
    while d <= BT_END:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    log(f"[2/3 backtest] {len(dates)} weekdays, offline (1 lot, bid/ask, $2/order)...")
    for i, dd in enumerate(dates, 1):
        subprocess.run([sys.executable, "coil_backtest.py", "--date", dd,
                        "--units", "100000", "--risk", "165", "--breakeven", "165",
                        "--daily-limit", "500", "--commission", "2", "--offline"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if i % 50 == 0:
            log(f"[2/3 backtest] {i}/{len(dates)} days done")
    log(f"[2/3 backtest] DONE: {len(dates)} days")


def report_phase():
    log("[3/3 report] building coil_2year_report.xlsx...")
    subprocess.run([sys.executable, "coil_report.py", BT_START.isoformat(),
                    BT_END.isoformat(), "coil_2year_report.xlsx"])
    log(f"DONE -> coil_2year_report.xlsx  (Summary/Daily/Trades + per-session)  total {_el()}")


def main():
    open(LOG, "w").close()
    log(f"START 2-year run: warm {WARM_START.date()}..{WARM_END.date()}, "
        f"backtest {BT_START}..{BT_END}")
    asyncio.run(warm_phase())
    if _cached() < N_SERIES:
        log(f"WARNING: only {_cached()}/{N_SERIES} series cached - proceeding with partial data")
    backtest_phase()
    report_phase()


if __name__ == "__main__":
    main()
