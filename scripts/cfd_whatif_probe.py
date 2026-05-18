"""
Probe per-account CFD trading permissions using whatIf orders.

A whatIf order is sent to IBKR for margin/commission analysis ONLY - it is
NOT placed in the market. If the account lacks CFD permissions, IBKR
returns an error that we surface.

We connect with read_only=False because IBKR rejects whatIf orders in
read-only mode. The whatIf flag itself is the safety: every order has
`whatIf=True` set explicitly. NO order is ever placed live.

Usage:
    python scripts/cfd_whatif_probe.py
    python scripts/cfd_whatif_probe.py --symbol EURUSD --units 25000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, Contract, LimitOrder, MarketOrder, Order  # type: ignore[import-untyped]

from time_utils import ny_now


def configure_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _extract_trade_error(trade) -> str:
    """Pick the most useful error message from a Trade's log entries."""
    log = getattr(trade, "log", []) or []
    msgs = []
    for entry in log:
        msg = (getattr(entry, "message", "") or "").strip()
        code = getattr(entry, "errorCode", 0) or 0
        if msg or code:
            msgs.append(f"code={code} msg={msg[:200]}")
    return " | ".join(msgs[-3:]) if msgs else ""


async def cfd_whatif_for_account(ib: IB, contract: Contract, account: str,
                                  side: str, units: int,
                                  wait_seconds: float = 12.0,
                                  order_type: str = "MKT",
                                  limit_price: float | None = None) -> dict:
    """Run a whatIf order against one account; return outcome dict."""
    if order_type == "LMT":
        if limit_price is None:
            raise ValueError("limit_price required for LMT order")
        order = LimitOrder(side, units, limit_price)
    else:
        order = MarketOrder(side, units)
    order.account = account
    order.whatIf = True                      # * critical: preview only
    order.tif = "DAY"
    order.outsideRth = True   # so the price is acceptable any hour

    try:
        trade = ib.placeOrder(contract, order)
    except Exception as e:
        return {
            "account": account,
            "ok": False,
            "error": f"placeOrder raised: {type(e).__name__}: {e}",
            "state": None,
        }

    # Wait briefly for IBKR to either accept (populate orderState) or reject.
    state = None
    deadline = asyncio.get_event_loop().time() + wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        os = getattr(trade, "orderStatus", None)
        oc = getattr(trade, "orderState", None) or getattr(trade, "state", None)
        # Rejection?
        if os and getattr(os, "status", "") in ("Inactive", "Rejected", "Cancelled", "ApiCancelled"):
            break
        # Margin populated -> success.
        if oc is not None:
            init_after = getattr(oc, "initMarginAfter", None)
            if init_after not in (None, "", "0", "0.00", "1.7976931348623157E308"):
                state = oc
                break
        await asyncio.sleep(0.2)
    else:
        # Loop completed without break -> try one final read.
        state = getattr(trade, "orderState", None) or getattr(trade, "state", None)

    rejected = (
        getattr(trade.orderStatus, "status", "") in ("Inactive", "Rejected", "Cancelled", "ApiCancelled")
    )
    err_text = _extract_trade_error(trade)

    if rejected or state is None:
        # Diagnostic dump - for the ambiguous case (no error AND no margin),
        # show the actual trade object so we know where to look next.
        trade_dump = {
            "status": getattr(trade.orderStatus, "status", None),
            "orderState_repr": repr(getattr(trade, "orderState", None)),
            "log": [
                {"code": getattr(e, "errorCode", 0),
                 "status": getattr(e, "status", ""),
                 "msg": getattr(e, "message", "")[:200]}
                for e in (trade.log or [])
            ],
        }
        return {
            "account": account,
            "ok": False,
            "error": err_text or "no margin info returned (see diagnostic)",
            "state": None,
            "raw": {"trade_log": err_text, "diagnostic": trade_dump},
        }

    raw = {
        "status": getattr(state, "status", ""),
        "commission": getattr(state, "commission", None),
        "commissionCurrency": getattr(state, "commissionCurrency", ""),
        "warningText": getattr(state, "warningText", ""),
        "initMarginBefore": getattr(state, "initMarginBefore", ""),
        "initMarginChange": getattr(state, "initMarginChange", ""),
        "initMarginAfter": getattr(state, "initMarginAfter", ""),
        "maintMarginBefore": getattr(state, "maintMarginBefore", ""),
        "maintMarginChange": getattr(state, "maintMarginChange", ""),
        "maintMarginAfter": getattr(state, "maintMarginAfter", ""),
        "equityWithLoanBefore": getattr(state, "equityWithLoanBefore", ""),
        "equityWithLoanChange": getattr(state, "equityWithLoanChange", ""),
        "equityWithLoanAfter": getattr(state, "equityWithLoanAfter", ""),
    }
    return {
        "account": account,
        "ok": True,
        "error": None,
        "state": state,
        "raw": raw,
    }


async def run(host: str, port: int, client_id: int, symbol: str,
              units: int, side: str):
    log = logging.getLogger("cfd_whatif_probe")
    # MUST connect non-read-only for whatIf orders. The whatIf flag on each
    # order is what protects us from real placement.
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id} (readonly=False, whatIf only)")

    try:
        # Brief wait for account population.
        await asyncio.sleep(1.0)
        accounts = list(ib.managedAccounts())
        log.info(f"Managed accounts: {accounts}")

        # Qualify the CFD contract.
        base, quote = symbol[:3], symbol[3:]
        contract = Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified or qualified[0] is None:
            raise RuntimeError(f"Failed to qualify CFD contract for {symbol}")
        contract = qualified[0]
        log.info(f"CFD contract: {symbol} -> secType={contract.secType} "
                 f"conId={contract.conId} exchange={contract.exchange} "
                 f"local={contract.localSymbol}")

        # Probe each account. For accounts where the MARKET-order whatIf hangs
        # (likely off-hours pricing), follow up with a LIMIT-order whatIf which
        # IBKR can price without market data.
        results = []
        for acct in accounts:
            log.info(f"Submitting whatIf MARKET {side} {units:,} {symbol} for {acct}...")
            r = await cfd_whatif_for_account(ib, contract, acct, side, units)
            r["order_type"] = "MKT"
            if not r["ok"] and "no margin info returned" in (r.get("error") or ""):
                # Retry with a LIMIT order far from any plausible market price.
                limit_px = 1.05 if side == "BUY" else 1.30   # EURUSD bracket
                log.info(f"  MKT hung - retrying with LIMIT @ {limit_px} for {acct}...")
                r_lmt = await cfd_whatif_for_account(
                    ib, contract, acct, side, units,
                    order_type="LMT", limit_price=limit_px,
                )
                r_lmt["order_type"] = "LMT"
                r_lmt["note"] = f"MKT hung in PendingSubmit; LIMIT retry @ {limit_px}"
                results.append(r_lmt)
            else:
                results.append(r)
    finally:
        ib.disconnect()
        log.info("Disconnected")

    # --- Console report ---
    print()
    print("=" * 110)
    print(f"CFD whatIf PROBE  -  {ny_now().strftime('%a %Y-%m-%d %H:%M %Z')}")
    print(f"Contract: {symbol} CFD ({contract.localSymbol}, conId={contract.conId}, exchange={contract.exchange})")
    print(f"Order: {side} {units:,} units  (whatIf=True - no real order placed)")
    print("=" * 110)
    print()
    print(f"{'Account':<12} {'CFD?':<6} {'InitMargin delta':>15} {'MaintMargin delta':>15} "
          f"{'EquityLoan delta':>15} {'Commission':>14} {'Warning / Error'}")
    print("-" * 110)
    for r in results:
        if r["ok"] and r["state"]:
            raw = r["raw"]
            im = raw["initMarginChange"]
            mm = raw["maintMarginChange"]
            el = raw["equityWithLoanChange"]
            comm = raw["commission"]
            comm_str = f"{comm} {raw['commissionCurrency']}" if comm not in (None, "", 0) else "-"
            warn = raw["warningText"] or ""
            print(f"{r['account']:<12} {'YES':<6} {str(im):>15} {str(mm):>15} "
                  f"{str(el):>15} {comm_str:>14} {warn}")
        else:
            print(f"{r['account']:<12} {'NO':<6} {'-':>15} {'-':>15} "
                  f"{'-':>15} {'-':>14} {r.get('error') or 'unknown'}")
    print()
    print("Full per-account detail:")
    print("-" * 110)
    for r in results:
        print(f"\n  Account: {r['account']}")
        if r["state"]:
            raw = r["raw"]
            for k in ("status", "commission", "commissionCurrency", "warningText",
                     "initMarginBefore", "initMarginChange", "initMarginAfter",
                     "maintMarginBefore", "maintMarginChange", "maintMarginAfter",
                     "equityWithLoanBefore", "equityWithLoanChange", "equityWithLoanAfter"):
                v = raw.get(k)
                if v in (None, "", "0", "0.0"):
                    continue
                print(f"    {k:<25} {v}")
        else:
            print(f"    (no state - error: {r.get('error')})")
            diag = (r.get("raw") or {}).get("diagnostic")
            if diag:
                print(f"    diagnostic:")
                print(f"      orderStatus.status = {diag['status']}")
                print(f"      orderState         = {diag['orderState_repr']}")
                if diag["log"]:
                    print(f"      trade.log entries:")
                    for entry in diag["log"]:
                        print(f"        code={entry['code']} status={entry['status']} msg={entry['msg']}")
    print()


def main():
    p = argparse.ArgumentParser(description="CFD whatIf permission probe")
    p.add_argument("--symbol", default="EURUSD", help="Forex pair, e.g. EURUSD")
    p.add_argument("--units", type=int, default=25000, help="Order size in base currency units")
    p.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=72)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)
    asyncio.run(run(args.host, args.port, args.client_id,
                    args.symbol, args.units, args.side))


if __name__ == "__main__":
    main()
