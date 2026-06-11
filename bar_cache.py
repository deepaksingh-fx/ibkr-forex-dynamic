"""
Local raw-bar cache for fast OFFLINE backtesting.

Fetch each (symbol, whatToShow) full date range from IBKR ONCE, persist to disk,
then serve sliced ranges instantly. `CachedBars` is a drop-in for IBKRClient's
fetch_5min_bars_range / fetch_5min_bars (+ no-op connect/disconnect), so the
backtest runs unchanged but never re-hits the broker.

    # warm once (hits IBKR):  await warm(ibkr, symbols, ["MIDPOINT","BID","ASK"], start, end)
    # then run offline:       cb = CachedBars(); pass it where IBKRClient was used
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

CACHE_DIR = os.path.join(os.path.dirname(__file__), "barcache")


class _Bar:
    __slots__ = ("date", "open", "high", "low", "close")

    def __init__(self, date, o, h, l, c):
        self.date = date; self.open = o; self.high = h; self.low = l; self.close = c


def _path(symbol: str, what: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_{what}.json")


def _to_utc(dt) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def warm(ibkr, symbols, whats, start_ny, end_ny, force: bool = False,
               pace_sleep_s: float = 11.0, logfn=print) -> None:
    """Download each (symbol, what) full range from the real IBKR and cache to disk.

    pace_sleep_s defaults to 11s between chunks to stay under IBKR's ~60-requests/
    10-min historical limit (going faster trips a pacing PENALTY that throttles
    the whole connection for minutes). `logfn` receives per-series progress."""
    import time as _t
    os.makedirs(CACHE_DIR, exist_ok=True)
    total = len(symbols) * len(whats)
    idx = 0
    t0 = _t.time()
    for sym in symbols:
        for what in whats:
            idx += 1
            p = _path(sym, what)
            if os.path.exists(p) and not force:
                logfn(f"[{idx}/{total}] already cached: {sym} {what}")
                continue
            try:
                bars = await ibkr.fetch_5min_bars_range(
                    sym, start_ny=start_ny, end_ny=end_ny, use_cfd=False, what=what,
                    pace_sleep_s=pace_sleep_s)
                data = [{"t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date),
                         "o": float(b.open), "h": float(b.high), "l": float(b.low), "c": float(b.close)}
                        for b in bars]
                el = (_t.time() - t0) / 60
                # only persist a non-empty series (so a drop mid-fetch leaves no
                # partial file -> a re-run cleanly retries it)
                if data:
                    with open(p, "w") as f:
                        json.dump(data, f)
                    span = f"{data[0]['t'][:10]}..{data[-1]['t'][:10]}"
                    logfn(f"[{idx}/{total}] cached {sym} {what}: {len(data)} bars ({span})  "
                          f"elapsed {el:.0f}m")
                else:
                    logfn(f"[{idx}/{total}] EMPTY {sym} {what} (dropped/unavailable) - will retry")
            except Exception as e:
                logfn(f"[{idx}/{total}] FAILED {sym} {what}: {e} - will retry")


class CachedBars:
    """Offline stand-in for IBKRClient (read-only, from the disk cache)."""

    def __init__(self):
        self._mem: dict = {}

    def _load(self, symbol: str, what: str):
        key = (symbol, what)
        if key in self._mem:
            return self._mem[key]
        bars = []
        p = _path(symbol, what)
        if os.path.exists(p):
            for r in json.load(open(p)):
                bars.append(_Bar(datetime.fromisoformat(r["t"]), r["o"], r["h"], r["l"], r["c"]))
        bars.sort(key=lambda b: b.date)
        self._mem[key] = bars
        return bars

    async def connect(self): pass
    async def disconnect(self): pass

    async def fetch_5min_bars_range(self, symbol, start_ny, end_ny, use_cfd=False,
                                    what="MIDPOINT", **kw):
        bars = self._load(symbol, what)
        s, e = _to_utc(start_ny), _to_utc(end_ny)
        return [b for b in bars if s <= _to_utc(b.date) <= e]

    async def fetch_5min_bars(self, symbol, end_ny, duration_str="1 D", use_cfd=False,
                              what="MIDPOINT", **kw):
        # conversion-rate use: the last bar at/before end_ny
        bars = self._load(symbol, what)
        e = _to_utc(end_ny)
        sub = [b for b in bars if _to_utc(b.date) <= e]
        return sub[-1:] if sub else []
