"""
whatIf preview - submits an order with order.whatIf = True so IBKR returns
margin/commission analysis WITHOUT routing to the market. Nothing is filled,
no money moves.

Usage examples:
    python scripts/whatif.py --lot 0.05 --side SELL
    python scripts/whatif.py --lot 0.25 --side BUY  --symbol EURUSD
    python scripts/whatif.py --lot 0.10 --side SELL --account U25265693
    python scripts/whatif.py --lot 0.05 --side SELL --port 4002    # paper

If --account is omitted, the script tests the order on EVERY funded sub-
account (skips master + accounts at $0).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB, Forex, MarketOrder   # type: ignore[import-untyped]


def _funded_accounts(ib: IB, min_balance_usd: float = 1000.0) -> List[str]:
    """Return managed accounts whose USD NetLiquidation is >= min_balance_usd."""
    out: List[str] = []
    for acct in ib.managedAccounts():
        netliq = 0.0
        for v in ib.accountValues(account=acct):
            if v.tag == "NetLiquidation" and v.currency == "USD":
                try:
                    netliq = float(v.value)
                except (ValueError, TypeError):
                    pass
                break
        if netliq >= min_balance_usd:
            out.append(acct)
    return out


async def main():
    p = argparse.ArgumentParser(description="whatIf preview - no real orders")
    p.add_argument("--lot", type=float, required=True,
                   help="Lot size, e.g. 0.05, 0.25, 1.0")
    p.add_argument("--side", choices=["BUY", "SELL"], required=True,
                   help="BUY (open LONG) or SELL (open SHORT)")
    p.add_argument("--symbol", default="EURUSD",
                   help="Forex pair, default EURUSD")
    p.add_argument("--account", default=None,
                   help="Sub-account; omit to whatIf against every funded sub")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001,
                   help="IB Gateway port (4001 live, 4002 paper)")
    p.add_argument("--client-id", type=int, default=99)
    args = p.parse_args()

    units = int(args.lot * 100_000)

    print("=" * 64)
    print(f" whatIf preview - NO real orders fire")
    print(f"   side    : {args.side}")
    print(f"   symbol  : {args.symbol}")
    print(f"   lot     : {args.lot}  ({units:,} units)")
    print(f"   gateway : {args.host}:{args.port}")
    print("=" * 64)
    print()

    ib = IB()
    # readonly=False is REQUIRED so the gateway will respond to whatIf.
    await ib.connectAsync(args.host, args.port, clientId=args.client_id,
                           readonly=False, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1

    try:
        # Account list
        if args.account:
            accounts = [args.account]
        else:
            await asyncio.sleep(1.5)   # give accountValues subscription a moment
            accounts = _funded_accounts(ib)
            if not accounts:
                print("No funded accounts found (NetLiq >= $1,000).")
                return 1
        print(f"Will preview on: {accounts}\n")

        # Qualify the contract once.
        contract = (await ib.qualifyContractsAsync(Forex(args.symbol)))[0]

        for acct in accounts:
            print(f"--- {acct} ---")
            order = MarketOrder(args.side, units)
            order.account = acct
            order.whatIf = True
            assert order.whatIf is True, "safety check"

            trade = ib.placeOrder(contract, order)
            # Wait for the broker to send back the OrderState.
            for _ in range(50):
                await asyncio.sleep(0.1)
                if trade.orderStatus.status in ("Filled", "Cancelled",
                                                 "Inactive", "ApiCancelled"):
                    break
                # whatIf returns 'PreSubmitted' or 'Inactive' with errors;
                # we also break if we have an error.
                if trade.log and any(e.errorCode for e in trade.log):
                    break

            state = trade.orderStatus
            errors = [(e.errorCode, e.message)
                      for e in (trade.log or []) if e.errorCode]

            print(f"  status     : {state.status}")
            print(f"  initMargin : {getattr(state, 'initMarginChange', 'n/a')}")
            print(f"  maintMargin: {getattr(state, 'maintMarginChange', 'n/a')}")
            print(f"  commission : {getattr(state, 'commission', 'n/a')}")
            if errors:
                print("  errors     :")
                for code, msg in errors:
                    print(f"      [{code}] {msg[:200]}")
            else:
                print("  errors     : none")
            print()

    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
