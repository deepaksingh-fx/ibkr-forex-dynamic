"""
Place a 0.01 lot (1000 units) EURUSD CFD MarketOrder BUY on EVERY managed
account, with try/except per account. Capture every callback. Do NOT cancel.

This is a REAL ORDER (whatIf=False). Successful accounts will have a real
~1000 EUR notional long position left open afterwards.

Output: per-account outcome (accepted / rejected / hung) + the live
position snapshot after all attempts.

Safety:
  - 0.01 lot = 1000 units = ~$1,100 notional per success
  - Per the user's instruction, NO orders are cancelled
  - Each account is in its own try/except so one failure doesn't block others
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, Contract, MarketOrder  # type: ignore[import-untyped]


# ----- event capture (keyed by orderId so each account's events are isolated) -----
EVENTS_BY_ORDER: dict[int, list[dict]] = {}
ERRORS_BY_REQ: dict[int, list[dict]] = {}


def _log_event_for_order(order_id: int, name: str, payload: dict):
    EVENTS_BY_ORDER.setdefault(order_id, []).append({"t": _time.time(), "event": name, **payload})


def _log_error_for_req(req_id: int, payload: dict):
    ERRORS_BY_REQ.setdefault(req_id, []).append({"t": _time.time(), **payload})


def _attach(ib: IB):
    def on_error(reqId, errorCode, errorString, contract):
        _log_error_for_req(reqId, {
            "errorCode": errorCode,
            "errorString": errorString,
            "contract_repr": repr(contract),
        })

    def on_open_order(trade):
        oid = trade.order.orderId
        _log_event_for_order(oid, "openOrderEvent", {
            "permId": getattr(trade.order, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
        })

    def on_order_status(trade):
        oid = trade.order.orderId
        _log_event_for_order(oid, "orderStatusEvent", {
            "permId": getattr(trade.orderStatus, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "filled": getattr(trade.orderStatus, "filled", None),
            "remaining": getattr(trade.orderStatus, "remaining", None),
            "avgFillPrice": getattr(trade.orderStatus, "avgFillPrice", None),
            "whyHeld": getattr(trade.orderStatus, "whyHeld", None),
        })

    def on_exec_details(trade, fill):
        oid = trade.order.orderId
        _log_event_for_order(oid, "execDetailsEvent", {"fill_repr": repr(fill)})

    def on_commission_report(trade, fill, cr):
        oid = trade.order.orderId
        _log_event_for_order(oid, "commissionReportEvent", {"cr_repr": repr(cr)})

    ib.errorEvent += on_error
    ib.openOrderEvent += on_open_order
    ib.orderStatusEvent += on_order_status
    ib.execDetailsEvent += on_exec_details
    ib.commissionReportEvent += on_commission_report


async def place_for_account(ib: IB, contract: Contract, account: str,
                             units: int, wait_seconds: float = 8.0) -> dict:
    """One try/except per account."""
    result: dict = {
        "account": account, "ok": False, "exception": None,
        "orderId": None, "permId": None, "final_status": None,
        "filled": 0.0, "remaining": float(units), "avg_fill_price": 0.0,
        "errors": [], "events": [], "trade_repr": None,
    }
    try:
        order = MarketOrder("BUY", units)
        order.account = account
        order.whatIf = False              # * REAL order
        order.tif = "DAY"
        order.outsideRth = False
        order.transmit = True

        trade = ib.placeOrder(contract, order)
        result["orderId"] = trade.order.orderId
        result["trade_repr_initial"] = repr(trade)

        # Watch for response.
        deadline = _time.time() + wait_seconds
        while _time.time() < deadline:
            status = getattr(trade.orderStatus, "status", "")
            if status in ("Filled", "Cancelled", "ApiCancelled", "Rejected", "Inactive"):
                break
            await asyncio.sleep(0.2)

        result["permId"] = getattr(trade.orderStatus, "permId", 0)
        result["final_status"] = getattr(trade.orderStatus, "status", None)
        result["filled"] = getattr(trade.orderStatus, "filled", 0.0)
        result["remaining"] = getattr(trade.orderStatus, "remaining", 0.0)
        result["avg_fill_price"] = getattr(trade.orderStatus, "avgFillPrice", 0.0)
        result["trade_repr"] = repr(trade)
        result["fills"] = [repr(f) for f in trade.fills]
        result["log_entries"] = [repr(e) for e in trade.log]
        result["events"] = EVENTS_BY_ORDER.get(result["orderId"], [])
        result["errors"] = ERRORS_BY_REQ.get(result["orderId"], [])
        result["ok"] = result["final_status"] in ("Filled", "Submitted", "PreSubmitted")
    except Exception as e:
        result["exception"] = f"{type(e).__name__}: {e}"
    return result


async def run(host: str, port: int, client_id: int, units: int, symbol: str):
    log = logging.getLogger("cfd_market_micro_all")
    ib = IB()
    _attach(ib)
    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id} (readonly=False, real orders)")
    await asyncio.sleep(1.0)

    accounts = list(ib.managedAccounts())
    log.info(f"Managed accounts: {accounts}")

    try:
        # Qualify CFD contract.
        base, quote = symbol[:3], symbol[3:]
        contract = Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
        qualified = await ib.qualifyContractsAsync(contract)
        contract = qualified[0]
        log.info(f"Contract: {contract!r}")

        # Per-account: try / except / continue.
        results = []
        for acct in accounts:
            log.info(f"--- Placing MarketOrder BUY {units} {symbol} CFD for {acct} ---")
            r = await place_for_account(ib, contract, acct, units)
            results.append(r)
            log.info(f"  {acct}: status={r['final_status']} filled={r['filled']} "
                     f"exception={r['exception']}")
            await asyncio.sleep(1.0)   # gentle pacing

        # Snapshot current positions after all orders fired.
        log.info("Fetching position snapshot...")
        positions = list(await ib.reqPositionsAsync())

        # Also pull open orders.
        open_trades = ib.openTrades()
    finally:
        ib.disconnect()
        log.info("Disconnected")

    # --- REPORTS ---
    print("\n" + "=" * 110)
    print(f"PER-ACCOUNT OUTCOME - {symbol} CFD MarketOrder BUY {units} units (0.01 lot)")
    print("=" * 110)
    print(f"{'Account':<12} {'orderId':>8} {'permId':>14} {'status':<14} "
          f"{'filled':>7} {'avg_fill':>10}  errors")
    print("-" * 110)
    for r in results:
        errs = ", ".join(f"{e.get('errorCode')}:{(e.get('errorString') or '')[:40]}" for e in r["errors"])
        print(f"{r['account']:<12} {str(r['orderId'] or ''):>8} {str(r['permId'] or ''):>14} "
              f"{str(r['final_status'] or '-'):<14} "
              f"{r['filled']:>7.1f} {r['avg_fill_price']:>10.5f}  "
              f"{errs or '-'}")

    print()
    print("=" * 110)
    print("FULL PER-ACCOUNT DETAIL (raw)")
    print("=" * 110)
    for r in results:
        print(f"\n------ {r['account']} ------")
        if r["exception"]:
            print(f"  PYTHON EXCEPTION: {r['exception']}")
            continue
        print(f"  orderId       : {r['orderId']}")
        print(f"  permId        : {r['permId']}")
        print(f"  final status  : {r['final_status']}")
        print(f"  filled        : {r['filled']}")
        print(f"  remaining     : {r['remaining']}")
        print(f"  avg fill px   : {r['avg_fill_price']}")
        print(f"  fills:")
        for f in r.get("fills", []):
            print(f"    {f}")
        print(f"  log:")
        for e in r.get("log_entries", []):
            print(f"    {e}")
        print(f"  callbacks captured:")
        for ev in r["events"]:
            rel = ev["t"] - r["events"][0]["t"] if r["events"] else 0
            print(f"    [t+{rel:5.2f}s] {ev['event']}: "
                  + ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("t", "event")))
        print(f"  errors:")
        for e in r["errors"]:
            print(f"    errorCode={e.get('errorCode')} msg={e.get('errorString')}")

    print()
    print("=" * 110)
    print(f"LIVE POSITION SNAPSHOT (after orders, before disconnect)")
    print("=" * 110)
    if not positions:
        print("  (no open positions)")
    else:
        for p in positions:
            try:
                con = p.contract
                print(f"  {p.account:<12} {con.secType:<5} {con.symbol}/{con.currency:<5} "
                      f"qty={p.position:+g}  avgCost={p.avgCost}  conId={con.conId}")
            except Exception:
                print(f"  (malformed: {p!r})")

    print()
    print(f"Open trades remaining at API: {len(open_trades)}")
    for t in open_trades:
        print(f"  {t!r}")


def main():
    p = argparse.ArgumentParser(description="0.01 lot CFD market BUY across all accounts")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=82)
    p.add_argument("--units", type=int, default=1000, help="0.01 lot = 1000 units")
    p.add_argument("--symbol", default="EURUSD")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run(args.host, args.port, args.client_id, args.units, args.symbol))


if __name__ == "__main__":
    main()
