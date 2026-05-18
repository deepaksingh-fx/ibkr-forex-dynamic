"""
Decisive CFD order routing test.

Places a REAL LimitOrder (whatIf=False) for 1000 EURUSD CFD @ limit 0.5 —
absurdly far below market (~1.16), so it CANNOT fill unless EURUSD collapses
~57% in 25 seconds. After the observation window, the order is CANCELLED.

Isolates whether:
  - Only the whatIf preview path is broken → real LMT order will get a
    permId, status updates, openOrder callbacks, normal lifecycle.
  - OR all CFD routing is broken on U25265693 → real LMT order will ALSO
    hang in PendingSubmit / never get a permId.

Safety:
  - limit price 0.5 << current market (~1.16) so it can't fill
  - whatIf=False (real submission), but outsideRth=False
  - tif=DAY so it auto-expires if not cancelled
  - Explicit CANCEL after the observation window
  - Final sweep to verify no open orders remain
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, Contract, LimitOrder  # type: ignore[import-untyped]


EVENTS: list[dict] = []


def _log(name: str, payload: dict):
    EVENTS.append({"t": _time.time(), "event": name, **payload})


def _attach(ib: IB):
    def on_error(reqId, errorCode, errorString, contract):
        _log("errorEvent", {
            "reqId": reqId, "errorCode": errorCode,
            "errorString": errorString,
            "contract_repr": repr(contract),
        })

    def on_open_order(trade):
        _log("openOrderEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "permId": getattr(trade.order, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "trade_repr": repr(trade),
        })

    def on_order_status(trade):
        _log("orderStatusEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "permId": getattr(trade.orderStatus, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "filled": getattr(trade.orderStatus, "filled", None),
            "remaining": getattr(trade.orderStatus, "remaining", None),
            "whyHeld": getattr(trade.orderStatus, "whyHeld", None),
            "trade_repr": repr(trade),
        })

    def on_exec_details(trade, fill):
        _log("execDetailsEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "fill_repr": repr(fill),
        })

    def on_commission_report(trade, fill, cr):
        _log("commissionReportEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "cr_repr": repr(cr),
        })

    def on_cancel_order(trade):
        _log("cancelOrderEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "trade_repr": repr(trade),
        })

    ib.errorEvent += on_error
    ib.openOrderEvent += on_open_order
    ib.orderStatusEvent += on_order_status
    ib.execDetailsEvent += on_exec_details
    ib.commissionReportEvent += on_commission_report
    ib.cancelOrderEvent += on_cancel_order


def _dump_events_since(t0: float):
    rows = [e for e in EVENTS if e["t"] >= t0]
    if not rows:
        print("    (no events)")
        return
    for e in rows:
        rel = e["t"] - t0
        print(f"\n    [t+{rel:6.2f}s] {e['event']}")
        for k, v in e.items():
            if k in ("t", "event"):
                continue
            print(f"      {k}: {v}")


async def run(host: str, port: int, client_id: int, account: str):
    log = logging.getLogger("cfd_real_lmt_test")
    ib = IB()
    _attach(ib)
    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id} (readonly=False)")
    await asyncio.sleep(1.0)
    log.info(f"Managed accounts: {list(ib.managedAccounts())}")

    trade = None
    try:
        # Qualify the CFD contract.
        contract = Contract(secType="CFD", symbol="EUR", currency="USD", exchange="SMART")
        qualified = await ib.qualifyContractsAsync(contract)
        contract = qualified[0]
        print("\n--- CONTRACT (post-qualify) ---")
        print(f"    {contract!r}")
        print(f"    vars: {vars(contract)}")

        # Build the safe LimitOrder. 0.5 << 1.16 market = no fill risk.
        order = LimitOrder("BUY", 1000, 0.5)
        order.account = account
        order.whatIf = False        # ★ REAL submission this time
        order.outsideRth = False
        order.tif = "DAY"
        # Belt and suspenders: also set a transmit guard so the order doesn't
        # get transmitted unless we want — but ib_async needs transmit=True
        # for the order to actually be sent. Keeping it True; the safety is
        # the absurdly-low price.
        order.transmit = True

        print("\n--- ORDER ---")
        print(f"    {order!r}")
        print(f"    vars: {vars(order)}")
        print(f"    safety: limit=0.5, market is ~1.16 → 57% out-of-money, CANNOT FILL")

        t0 = _time.time()
        print(f"\n--- PLACING REAL LIMIT ORDER (whatIf=False) at t=0 ---")
        trade = ib.placeOrder(contract, order)
        print(f"    placeOrder() returned Trade(orderId={trade.order.orderId})")
        print(f"    initial trade_repr: {trade!r}")

        # Watch for 25 seconds; print all events as they arrive.
        wait_seconds = 25.0
        print(f"\n--- WATCHING FOR {wait_seconds:.0f}s ---")
        await asyncio.sleep(wait_seconds)

        print("\n--- ALL EVENTS CAPTURED DURING WATCH WINDOW ---")
        _dump_events_since(t0)

        # ─── CANCEL ───
        print("\n--- CANCELLING ORDER (safety cleanup) ---")
        cancel_t0 = _time.time()
        ib.cancelOrder(trade.order)
        await asyncio.sleep(5.0)
        print("\n--- EVENTS AFTER CANCEL ---")
        _dump_events_since(cancel_t0)

        # ─── Final state inspection ───
        print("\n--- FINAL TRADE STATE ---")
        print(f"    trade_repr: {trade!r}")
        print(f"    orderStatus: {trade.orderStatus!r}")
        print(f"    log entries:")
        for entry in trade.log:
            print(f"      {entry!r}")

        # Sanity sweep: any leftover open orders?
        print("\n--- OPEN-ORDERS SWEEP ---")
        await asyncio.sleep(1.0)
        open_trades = ib.openTrades()
        print(f"    ib.openTrades() count: {len(open_trades)}")
        for t in open_trades:
            print(f"      LEFTOVER: {t!r}")

    finally:
        # Last-ditch safety: cancel anything still open.
        try:
            for t in ib.openTrades():
                if t.order.account == account and t.order.orderId == (trade.order.orderId if trade else -1):
                    print(f"!! Final safety cancel for orderId={t.order.orderId}")
                    ib.cancelOrder(t.order)
            await asyncio.sleep(1.0)
        except Exception as e:
            print(f"final safety cancel raised: {e}")
        ib.disconnect()
        print("\n--- DISCONNECTED ---")


def main():
    p = argparse.ArgumentParser(description="Decisive CFD real-LMT order test (with cancel)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=81)
    p.add_argument("--account", default="U25265693")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run(args.host, args.port, args.client_id, args.account))


if __name__ == "__main__":
    main()
