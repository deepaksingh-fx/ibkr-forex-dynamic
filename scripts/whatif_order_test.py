"""
whatIf order test — validates the live order placement path WITHOUT executing.

IBKR's `order.whatIf = True` flag sends the order to the gateway for margin
and commission analysis, but the order is NEVER routed to the market. The
gateway returns an OrderState with margin / commission preview.

This is the closest we can get to a real-order test without spending money.

Tests:
  * BUY  25,000 EURUSD whatIf for U25265693
  * BUY  25,000 EURUSD whatIf for U25272450
  * SELL 25,000 EURUSD whatIf for U25265693
  * SELL 25,000 EURUSD whatIf for U25272450

What it proves:
  - lot units conversion (0.25 lot = 25,000)
  - FA sub-account stamping via order.account
  - Side string ("BUY" / "SELL")
  - Contract qualification at order-time
  - IBKR accepts our order shape
  - Margin headroom per account

Connection: readonly=False is REQUIRED for IBKR to process whatIf.
Safety: every order constructed in this script has order.whatIf = True
asserted before placeOrder is called. No order without that flag will pass.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ib_async import IB, Forex, MarketOrder   # type: ignore[import-untyped]


HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 88           # different from runner / report generator

SYMBOL = "EURUSD"
DEFAULT_LOT_UNITS = 25_000        # = 0.25 lot

ELIGIBLE_ACCOUNTS = ["U25265693", "U25272450"]   # from the balance check


def assert_whatif(order: "MarketOrder") -> None:
    """Triple-check that order.whatIf is True before any placeOrder call."""
    if not getattr(order, "whatIf", False):
        raise RuntimeError(
            "SAFETY: order.whatIf is not True. Refusing to placeOrder. "
            "This script must NEVER place a real order."
        )


def _has_margin_info(state) -> bool:
    """True iff the orderState carries usable whatIf analysis."""
    if state is None:
        return False
    # Commission populated → analysis complete.
    c = getattr(state, "commission", None)
    if c is not None and not (isinstance(c, float) and c > 1e300):
        return True
    # Or any margin-after field populated to a real number.
    for fname in ("initMarginAfter", "maintMarginAfter", "equityWithLoanAfter"):
        v = getattr(state, fname, None)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        # IBKR uses ~1.79e308 to mean "no value"; reject it.
        if abs(f) < 1e300:
            return True
    return False


async def whatif_one(ib: IB, contract, account: str, side: str, qty: int) -> dict:
    """
    Submit one whatIf order using ib_async's purpose-built whatIfOrderAsync.
    Returns the resulting OrderState as a dict.
    """
    order = MarketOrder(side, qty)
    order.account = account
    order.whatIf = True              # ← MUST be set before submission
    assert_whatif(order)

    try:
        # whatIfOrderAsync: the canonical API. Returns OrderState with margin info.
        state = await asyncio.wait_for(
            ib.whatIfOrderAsync(contract, order), timeout=15.0
        )
        return _state_to_dict(state, account, side, qty, trade=None)
    except asyncio.TimeoutError:
        return {
            "account": account, "side": side, "qty": qty,
            "ok": False, "error": "whatIfOrderAsync timed out (15s)",
            "log": [],
        }
    except Exception as e:
        return {
            "account": account, "side": side, "qty": qty,
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "log": [],
        }


def _state_to_dict(state, account: str, side: str, qty: int, trade=None) -> dict:
    # Extract error reasons from trade.log entries even if state is empty.
    log_msgs = []
    final_status = None
    if trade is not None:
        final_status = getattr(trade.orderStatus, "status", None) if trade.orderStatus else None
        for entry in getattr(trade, "log", []):
            msg = getattr(entry, "message", "") or ""
            ec = getattr(entry, "errorCode", 0)
            if msg or ec:
                log_msgs.append(f"[{getattr(entry, 'status', '?')}] code={ec} msg={msg}")

    if state is None:
        return {
            "account": account, "side": side, "qty": qty,
            "ok": False,
            "final_status": final_status,
            "log": log_msgs,
            "error": "no orderState",
        }
    return {
        "account": account,
        "side": side,
        "qty": qty,
        "ok": True,
        "final_status": final_status,
        "log": log_msgs,
        "status": getattr(state, "status", None),
        "commission": getattr(state, "commission", None),
        "commissionCurrency": getattr(state, "commissionCurrency", None),
        "initMarginBefore": getattr(state, "initMarginBefore", None),
        "initMarginAfter": getattr(state, "initMarginAfter", None),
        "initMarginChange": getattr(state, "initMarginChange", None),
        "maintMarginBefore": getattr(state, "maintMarginBefore", None),
        "maintMarginAfter": getattr(state, "maintMarginAfter", None),
        "maintMarginChange": getattr(state, "maintMarginChange", None),
        "equityWithLoanBefore": getattr(state, "equityWithLoanBefore", None),
        "equityWithLoanAfter": getattr(state, "equityWithLoanAfter", None),
        "warningText": getattr(state, "warningText", None),
    }


async def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--units", type=int, default=DEFAULT_LOT_UNITS,
                   help=f"Order size in base-currency units (default {DEFAULT_LOT_UNITS:,} = 0.25 lot)")
    args = p.parse_args()
    lot_units = args.units

    print("=" * 80)
    print("whatIf order test — NO orders will be placed in the market")
    print(f"  Contract: {SYMBOL} ({lot_units:,} units = {lot_units/100_000:.4f} lot)")
    print(f"  Accounts: {ELIGIBLE_ACCOUNTS}")
    print(f"  Sides:    BUY, SELL  (4 whatIf submissions total)")
    print("=" * 80)

    ib = IB()
    # NOTE: readonly=False is required for IBKR to process whatIf. Safety:
    # every Order constructed below has whatIf=True asserted before placeOrder.
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, readonly=False, timeout=15)
    if not ib.isConnected():
        print("ERROR: failed to connect")
        return 1
    print(f"\nConnected (readonly=False, clientId={CLIENT_ID})")

    try:
        accounts_visible = ib.managedAccounts()
        print(f"Managed accounts visible: {accounts_visible}")

        # Sanity: requested accounts must actually be visible.
        missing = [a for a in ELIGIBLE_ACCOUNTS if a not in accounts_visible]
        if missing:
            print(f"WARN: requested accounts not visible: {missing}")

        # Qualify contract once.
        contract = (await ib.qualifyContractsAsync(Forex(SYMBOL)))[0]
        print(f"Qualified {SYMBOL}: conId={contract.conId} exchange={contract.exchange}")

        results = []
        for acct in ELIGIBLE_ACCOUNTS:
            for side in ("BUY", "SELL"):
                print(f"\n→ whatIf {side} {lot_units:,} {SYMBOL} for {acct} ...")
                r = await whatif_one(ib, contract, acct, side, lot_units)
                results.append(r)
                print(f"   final_status={r.get('final_status')}")
                for entry in r.get("log", []):
                    print(f"     log: {entry}")
                if r.get("ok"):
                    if r.get("commission") is not None:
                        print(f"   ✓ status={r['status']} "
                              f"commission={r['commission']} {r.get('commissionCurrency','')}")
                        print(f"     initMargin {r['initMarginBefore']} → {r['initMarginAfter']} "
                              f"(Δ={r['initMarginChange']})")
                        print(f"     maintMargin {r['maintMarginBefore']} → {r['maintMarginAfter']} "
                              f"(Δ={r['maintMarginChange']})")
                        print(f"     equityWithLoan {r['equityWithLoanBefore']} → {r['equityWithLoanAfter']}")
                    if r.get("warningText"):
                        print(f"     ⚠ warning: {r['warningText']}")

        # Summary
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        ok_count = sum(1 for r in results if r.get("ok"))
        print(f"  {ok_count}/{len(results)} whatIf submissions returned an OrderState")
        print()
        print(f"{'Account':<12} {'Side':<5} {'Status':<10} {'Commission':<14} {'InitMargin Δ':<14} {'EquityAfter':<14}")
        for r in results:
            if not r.get("ok"):
                print(f"{r['account']:<12} {r['side']:<5} ✗ {r.get('error','')}")
                continue
            print(f"{r['account']:<12} {r['side']:<5} "
                  f"{str(r.get('status','-')):<10} "
                  f"{str(r.get('commission','-')):<14} "
                  f"{str(r.get('initMarginChange','-')):<14} "
                  f"{str(r.get('equityWithLoanAfter','-')):<14}")

    finally:
        ib.disconnect()
    print("\nDone. (No orders placed.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
