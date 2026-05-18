"""
Quick balance check across all FA sub-accounts.

Lists every managed account, fetches NetLiquidation, applies the
$1000+ filter from SPEC sec13.0, and reports which accounts would
actually trade.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB   # type: ignore[import-untyped]

from config import MIN_ACCOUNT_BALANCE_USD


HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 42


async def main() -> int:
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1

    try:
        accounts = ib.managedAccounts()
        print(f"Managed accounts: {accounts}\n")

        rows = []
        for acct in accounts:
            try:
                values = await ib.accountSummaryAsync(account=acct)
            except Exception as e:
                rows.append((acct, None, None, f"ERROR fetching: {e}"))
                continue

            netliq_usd = None
            for v in values:
                if v.tag == "NetLiquidation" and v.account == acct and v.currency == "USD":
                    try:
                        netliq_usd = float(v.value)
                    except (TypeError, ValueError):
                        pass
                    break
            # Some FA accounts only report in BASE; fall back.
            if netliq_usd is None:
                for v in values:
                    if v.tag == "NetLiquidation" and v.account == acct:
                        try:
                            netliq_usd = float(v.value)
                        except (TypeError, ValueError):
                            pass
                        break
            rows.append((acct, netliq_usd, "USD" if netliq_usd is not None else "?", ""))

        # Print table
        print(f"{'Account':<14} {'Balance':>14} {'Currency':<10} {'Status':<22}")
        print("-" * 64)
        eligible = []
        for acct, bal, ccy, err in rows:
            if err:
                status = err
            elif bal is None:
                status = "no NetLiquidation found"
            elif bal >= MIN_ACCOUNT_BALANCE_USD:
                status = f"[OK] ELIGIBLE (>= ${MIN_ACCOUNT_BALANCE_USD:.0f})"
                eligible.append((acct, bal))
            else:
                status = f"[NO] below ${MIN_ACCOUNT_BALANCE_USD:.0f}"
            bal_str = f"${bal:,.2f}" if bal is not None else "-"
            print(f"{acct:<14} {bal_str:>14} {ccy:<10} {status:<22}")

        print()
        print(f"Eligible accounts (>= ${MIN_ACCOUNT_BALANCE_USD:.0f}): {len(eligible)} of {len(rows)}")
        if eligible:
            total = sum(b for _, b in eligible)
            print(f"Total tradable balance: ${total:,.2f}")
            for acct, bal in sorted(eligible, key=lambda x: -x[1]):
                print(f"  - {acct}: ${bal:,.2f}")

    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
