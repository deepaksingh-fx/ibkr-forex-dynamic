"""
Verify our streaming ADX has no bar-lag vs an independent reference Wilder ADX.

Fetches bars, runs our DMI (quality_supertrend.DMI) bar-by-bar, computes a
separate canonical Wilder ADX, then tests which shift (0/1/2 bars) best aligns
them. Shift 0 with ~0 error => no lag.

    python scripts/adx_check.py --symbol AUDJPY
"""
from __future__ import annotations
import argparse, asyncio, sys
from datetime import timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient
from quality_supertrend import DMI
from time_utils import ny_now, to_ny


def ts_ny(b):
    t = b.date
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return to_ny(t)


def wilder_rma(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(vals)):
        prev = (prev * (n - 1) + vals[i]) / n
        out[i] = prev
    return out


def ref_adx(H, L, C, n=14):
    N = len(H)
    tr = [0.0] * N; pdm = [0.0] * N; mdm = [0.0] * N
    for i in range(N):
        if i == 0:
            tr[i] = H[i] - L[i]
        else:
            tr[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
            up = H[i] - H[i - 1]; dn = L[i - 1] - L[i]
            pdm[i] = up if (up > dn and up > 0) else 0.0
            mdm[i] = dn if (dn > up and dn > 0) else 0.0
    atr = wilder_rma(tr, n); sp = wilder_rma(pdm, n); sm = wilder_rma(mdm, n)
    dx = [None] * N
    for i in range(N):
        if atr[i] and atr[i] != 0 and sp[i] is not None and sm[i] is not None:
            pdi = 100 * sp[i] / atr[i]; mdi = 100 * sm[i] / atr[i]
            s = pdi + mdi
            dx[i] = 100 * abs(pdi - mdi) / (s if s else 1.0)
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx = [None] * N
    if first is not None:
        r = wilder_rma([v for v in dx[first:]], n)
        for k, v in enumerate(r):
            adx[first + k] = v
    return adx


async def main(symbol):
    cfg = StrategyConfig(ibkr=IBKRConnection(client_id=79, read_only=True))
    ib = IBKRClient(cfg)
    await ib.connect()
    try:
        now = ny_now()
        bars = await ib.fetch_5min_bars_range(symbol, start_ny=now - timedelta(days=12), end_ny=now)
    finally:
        await ib.disconnect()
    bars.sort(key=ts_ny)
    H = [float(b.high) for b in bars]; L = [float(b.low) for b in bars]; C = [float(b.close) for b in bars]

    # Our streaming DMI.
    dmi = DMI(14, 14)
    ours = []
    for h, l, c in zip(H, L, C):
        _, _, a = dmi.update(h, l, c)
        ours.append(a)
    ref = ref_adx(H, L, C, 14)

    print(f"\n[adx_check] {symbol}: {len(bars)} bars")
    print("\nlast 12 bars:  time           ours      ref       diff")
    for b, o, r in list(zip(bars, ours, ref))[-12:]:
        os_ = f"{o:7.3f}" if o is not None else "   -   "
        rs = f"{r:7.3f}" if r is not None else "   -   "
        ds = f"{abs(o-r):7.4f}" if (o is not None and r is not None) else "   -   "
        print(f"               {ts_ny(b).strftime('%a %H:%M')}  {os_}  {rs}  {ds}")

    # Lag test: mean abs error of ours[i] vs ref[i-shift] over settled tail.
    print("\nlag test (mean |ours[i] - ref[i-shift]| over last 300 settled bars):")
    tail = range(max(0, len(bars) - 300), len(bars))
    for shift in (0, 1, 2):
        errs = [abs(ours[i] - ref[i - shift])
                for i in tail
                if i - shift >= 0 and ours[i] is not None and ref[i - shift] is not None]
        if errs:
            print(f"  shift={shift}:  mean|err|={sum(errs)/len(errs):.6f}   max|err|={max(errs):.6f}   n={len(errs)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--symbol", default="AUDJPY")
    a = ap.parse_args()
    asyncio.run(main(a.symbol))
