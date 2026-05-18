"""
Full CFD whatIf diagnostics for account U25265693.

Gathers RAW output for every diagnostic the user requested:
  1. Contract object: print() + vars()
  2. Order object: print() + vars()
  3. ib.qualifyContractsAsync(contract) raw output
  4. All API callbacks/events captured live (openOrder, orderStatus, error,
     execDetails, commissionReport)
  5. Show contract constructor code
  6. Confirm secType / exchange
  7. (manual — IBKR TWS UI test, user-side)
  8. Multiple sizes + multiple pairs
  9. Margin/whatIf state dump

Connection is non-read-only because whatIf requires it, but every order has
whatIf=True. No real orders are placed.
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


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ───────────────────── event capture ─────────────────────
EVENTS: list[dict] = []


def _log_event(name: str, payload: dict):
    EVENTS.append({"t": _time.time(), "event": name, **payload})


def _attach_handlers(ib: IB):
    def on_error(reqId, errorCode, errorString, contract):
        _log_event("errorEvent", {
            "reqId": reqId,
            "errorCode": errorCode,
            "errorString": errorString,
            "contract_repr": repr(contract),
        })

    def on_open_order(trade):
        _log_event("openOrderEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "trade_repr": repr(trade),
        })

    def on_order_status(trade):
        _log_event("orderStatusEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "filled": getattr(trade.orderStatus, "filled", None),
            "remaining": getattr(trade.orderStatus, "remaining", None),
            "permId": getattr(trade.orderStatus, "permId", None),
            "trade_repr": repr(trade),
        })

    def on_exec_details(trade, fill):
        _log_event("execDetailsEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "fill_repr": repr(fill),
            "trade_repr": repr(trade),
        })

    def on_commission_report(trade, fill, commission_report):
        _log_event("commissionReportEvent", {
            "orderId": getattr(trade.order, "orderId", None),
            "fill_repr": repr(fill),
            "commission_repr": repr(commission_report),
        })

    ib.errorEvent += on_error
    ib.openOrderEvent += on_open_order
    ib.orderStatusEvent += on_order_status
    ib.execDetailsEvent += on_exec_details
    ib.commissionReportEvent += on_commission_report


def _dump_events_since(t0: float):
    """Print every event captured since t0 (raw)."""
    rows = [e for e in EVENTS if e["t"] >= t0]
    if not rows:
        print("    (no events captured)")
        return
    for e in rows:
        rel = e["t"] - t0
        print(f"\n    [t+{rel:6.2f}s] {e['event']}")
        for k, v in e.items():
            if k in ("t", "event"):
                continue
            print(f"      {k}: {v}")


# ───────────────────── helpers ─────────────────────
def make_cfd_contract(symbol: str) -> Contract:
    """The exact constructor we use everywhere."""
    base, quote = symbol[:3], symbol[3:]
    # Constructor code shown explicitly to satisfy item 5:
    contract = Contract(
        secType="CFD",
        symbol=base,
        currency=quote,
        exchange="SMART",
    )
    return contract


async def run_one_test(ib: IB, account: str, symbol: str, units: int,
                       wait_seconds: float = 15.0) -> None:
    """Run one whatIf test and dump everything raw."""
    print("\n" + "=" * 100)
    print(f"TEST — account={account}  symbol={symbol}  units={units}  whatIf=True")
    print("=" * 100)

    # ─── Item 5: Constructor code being used ───
    print("\n--- (5) CONTRACT CONSTRUCTOR CODE ---")
    print('    contract = Contract(secType="CFD", symbol=<base>, currency=<quote>, exchange="SMART")')

    # ─── Item 1: Raw contract object before qualification ───
    contract = make_cfd_contract(symbol)
    print("\n--- (1a) CONTRACT BEFORE qualifyContracts ---")
    print(f"    print(contract):       {contract!r}")
    print(f"    print(vars(contract)): {vars(contract)!r}")

    # ─── Item 3: qualifyContracts (we use the async version under asyncio) ───
    print("\n--- (3) qualifyContractsAsync(contract) RAW OUTPUT ---")
    qualified_list = await ib.qualifyContractsAsync(contract)
    print(f"    raw return type: {type(qualified_list)}")
    print(f"    raw return list: {qualified_list!r}")
    if not qualified_list or qualified_list[0] is None:
        print("    *** qualifyContracts failed — aborting this test ***")
        return
    contract = qualified_list[0]

    # ─── Item 1b: Contract after qualification ───
    print("\n--- (1b) CONTRACT AFTER qualifyContracts (this is what we send) ---")
    print(f"    print(contract):       {contract!r}")
    print(f"    print(vars(contract)): {vars(contract)!r}")

    # ─── Item 6: Confirm instrument routing ───
    print("\n--- (6) INSTRUMENT ROUTING CONFIRMATION ---")
    print(f"    secType  = {contract.secType}   (expected: CFD)")
    print(f"    exchange = {contract.exchange}   (expected: SMART)")
    print(f"    is CFD?       {contract.secType == 'CFD'}")
    print(f"    is CASH?      {contract.secType == 'CASH'}")
    print(f"    is IDEALPRO?  {contract.exchange == 'IDEALPRO'}")
    print(f"    is SMART?     {contract.exchange == 'SMART'}")

    # ─── Item 2: Order object ───
    order = MarketOrder("BUY", units)
    order.account = account
    order.whatIf = True   # ★ preview only — no real placement
    order.tif = "DAY"
    order.outsideRth = True

    print("\n--- (2) ORDER OBJECT ---")
    print(f"    print(order):       {order!r}")
    print(f"    print(vars(order)): {vars(order)!r}")
    print(f"    order.whatIf = {order.whatIf}   (★ must be True; we do not place real orders)")

    # ─── Place the whatIf order; capture every event in real-time ───
    t0 = _time.time()
    print(f"\n--- (4) EVENT CAPTURE — placing whatIf order at t=0 ---")
    print(f"    waiting up to {wait_seconds:.1f}s for response...")

    trade = ib.placeOrder(contract, order)
    # Initial trade snapshot
    print(f"\n    placeOrder() returned Trade(orderId={trade.order.orderId}, "
          f"status={trade.orderStatus.status}):")
    print(f"      trade_repr_initial: {trade!r}")

    # Wait, polling for terminal state or margin response.
    deadline = _time.time() + wait_seconds
    while _time.time() < deadline:
        status = getattr(trade.orderStatus, "status", "")
        if status in ("Inactive", "Rejected", "Cancelled", "ApiCancelled", "Filled"):
            break
        # Also break on margin info present.
        os = getattr(trade, "orderState", None)
        if os is not None:
            init_after = getattr(os, "initMarginAfter", None)
            if init_after not in (None, "", "0", "0.00", "1.7976931348623157E308"):
                break
        await asyncio.sleep(0.25)

    elapsed = _time.time() - t0
    print(f"\n    waiting done after {elapsed:.2f}s; orderStatus.status = "
          f"{getattr(trade.orderStatus, 'status', None)}")

    # Dump captured events for THIS test only (since t0).
    print("\n--- Captured events from API during this test (raw): ---")
    _dump_events_since(t0)

    # ─── Item 9: whatIf state / margin response ───
    print("\n--- (9) whatIf / margin response — raw ---")
    os = getattr(trade, "orderState", None)
    print(f"    trade.orderState type: {type(os)}")
    print(f"    trade.orderState repr: {os!r}")
    if os is not None:
        for field in (
            "status", "commission", "commissionCurrency", "warningText",
            "initMarginBefore", "initMarginChange", "initMarginAfter",
            "maintMarginBefore", "maintMarginChange", "maintMarginAfter",
            "equityWithLoanBefore", "equityWithLoanChange", "equityWithLoanAfter",
        ):
            v = getattr(os, field, "<missing>")
            print(f"      orderState.{field:<25} = {v!r}")

    # Final trade snapshot (after waiting).
    print("\n--- Final Trade object state (raw) ---")
    print(f"    {trade!r}")
    print(f"    trade.orderStatus repr: {trade.orderStatus!r}")
    print(f"    trade.log entries:")
    for entry in trade.log:
        print(f"      {entry!r}")


async def run(host: str, port: int, client_id: int):
    log = logging.getLogger("cfd_diagnostics")
    ib = IB()

    # Attach handlers BEFORE connecting so we catch every callback.
    _attach_handlers(ib)

    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id} (readonly=False — whatIf only)")
    await asyncio.sleep(1.0)
    log.info(f"Managed accounts: {list(ib.managedAccounts())}")

    target = "U25265693"
    try:
        # Item 8 — multiple sizes and pairs.
        await run_one_test(ib, target, "EURUSD", 25000)
        await asyncio.sleep(1.0)
        await run_one_test(ib, target, "EURUSD", 1000)
        await asyncio.sleep(1.0)
        await run_one_test(ib, target, "USDJPY", 25000)
        await asyncio.sleep(1.0)
        await run_one_test(ib, target, "GBPUSD", 25000)
    finally:
        ib.disconnect()
        log.info("Disconnected")

    print("\n" + "=" * 100)
    print(f"TOTAL EVENTS CAPTURED ACROSS ALL TESTS: {len(EVENTS)}")
    print("=" * 100)


def main():
    p = argparse.ArgumentParser(description="CFD whatIf full diagnostics")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=80)
    args = p.parse_args()

    configure_logging()
    asyncio.run(run(args.host, args.port, args.client_id))


if __name__ == "__main__":
    main()
