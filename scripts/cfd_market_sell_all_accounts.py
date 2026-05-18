"""
Place a 0.01 lot (1000 units) EURUSD CFD MarketOrder SELL on EVERY managed
account, with try/except per account. This:
  - CLOSES the existing long position on U25265693
  - Tests whether the other accounts allow a SELL (similar diagnostic to BUY)

Also: at start, cancel any leftover open orders (e.g. F25172115 PendingSubmit
from the previous test).

Real orders (whatIf=False).
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


EVENTS_BY_ORDER: dict[int, list[dict]] = {}
ERRORS_BY_REQ: dict[int, list[dict]] = {}


def _log_event_for_order(order_id: int, name: str, payload: dict):
    EVENTS_BY_ORDER.setdefault(order_id, []).append({"t": _time.time(), "event": name, **payload})


def _log_error_for_req(req_id: int, payload: dict):
    ERRORS_BY_REQ.setdefault(req_id, []).append({"t": _time.time(), **payload})


def _attach(ib: IB):
    def on_error(reqId, errorCode, errorString, contract):
        _log_error_for_req(reqId, {"errorCode": errorCode, "errorString": errorString})

    def on_open_order(trade):
        _log_event_for_order(trade.order.orderId, "openOrderEvent", {
            "permId": getattr(trade.order, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
        })

    def on_order_status(trade):
        _log_event_for_order(trade.order.orderId, "orderStatusEvent", {
            "permId": getattr(trade.orderStatus, "permId", None),
            "status": getattr(trade.orderStatus, "status", None),
            "filled": getattr(trade.orderStatus, "filled", None),
            "remaining": getattr(trade.orderStatus, "remaining", None),
            "avgFillPrice": getattr(trade.orderStatus, "avgFillPrice", None),
        })

    def on_exec_details(trade, fill):
        _log_event_for_order(trade.order.orderId, "execDetailsEvent", {"fill_repr": repr(fill)})

    def on_commission_report(trade, fill, cr):
        _log_event_for_order(trade.order.orderId, "commissionReportEvent", {"cr_repr": repr(cr)})

    ib.errorEvent += on_error
    ib.openOrderEvent += on_open_order
    ib.orderStatusEvent += on_order_status
    ib.execDetailsEvent += on_exec_details
    ib.commissionReportEvent += on_commission_report


async def cancel_leftover_orders(ib: IB) -> int:
    """Cancel any open orders left from previous tests. Returns count cancelled."""
    open_trades = ib.openTrades()
    print(f"\n--- LEFTOVER OPEN TRADES AT API (before SELL test) ---")
    print(f"    count: {len(open_trades)}")
    for t in open_trades:
        print(f"      {t!r}")
    cancelled = 0
    for t in list(open_trades):
        try:
            print(f"    Cancelling orderId={t.order.orderId} account={t.order.account} ...")
            ib.cancelOrder(t.order)
            cancelled += 1
        except Exception as e:
            print(f"    cancel raised: {e}")
    if cancelled:
        await asyncio.sleep(3.0)   # give IBKR time to ack the cancels
    return cancelled


async def place_sell_for_account(ib: IB, contract: Contract, account: str,
                                  units: int, wait_seconds: float = 8.0) -> dict:
    result: dict = {
        "account": account, "ok": False, "exception": None,
        "orderId": None, "permId": None, "final_status": None,
        "filled": 0.0, "remaining": float(units), "avg_fill_price": 0.0,
        "errors": [], "events": [], "trade_repr": None,
    }
    try:
        order = MarketOrder("SELL", units)
        order.account = account
        order.whatIf = False
        order.tif = "DAY"
        order.outsideRth = False
        order.transmit = True

        trade = ib.placeOrder(contract, order)
        result["orderId"] = trade.order.orderId

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
        result["fills"] = [repr(f) for f in trade.fills]
        result["log_entries"] = [repr(e) for e in trade.log]
        result["events"] = EVENTS_BY_ORDER.get(result["orderId"], [])
        result["errors"] = ERRORS_BY_REQ.get(result["orderId"], [])
        result["ok"] = result["final_status"] in ("Filled", "Submitted", "PreSubmitted")
    except Exception as e:
        result["exception"] = f"{type(e).__name__}: {e}"
    return result


async def run(host: str, port: int, client_id: int, units: int, symbol: str):
    log = logging.getLogger("cfd_market_sell_all")
    ib = IB()
    _attach(ib)
    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id}")
    await asyncio.sleep(1.0)
    accounts = list(ib.managedAccounts())
    log.info(f"Managed accounts: {accounts}")

    try:
        # Position snapshot BEFORE.
        positions_before = list(await ib.reqPositionsAsync())
        print("\n--- POSITIONS BEFORE SELL ---")
        if not positions_before:
            print("  (none)")
        for p in positions_before:
            print(f"  {p.account:<12} {p.contract.secType:<5} "
                  f"{p.contract.symbol}/{p.contract.currency:<5} "
                  f"qty={p.position:+g}  avgCost={p.avgCost}")

        # Cancel any leftover orders (e.g. F25172115 PendingSubmit).
        n_cancelled = await cancel_leftover_orders(ib)
        log.info(f"Cancelled {n_cancelled} leftover order(s)")

        # Qualify contract.
        base, quote = symbol[:3], symbol[3:]
        contract = Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
        qualified = await ib.qualifyContractsAsync(contract)
        contract = qualified[0]
        log.info(f"Contract: {contract!r}")

        # SELL on every account.
        results = []
        for acct in accounts:
            log.info(f"--- SELL {units} {symbol} CFD on {acct} ---")
            r = await place_sell_for_account(ib, contract, acct, units)
            results.append(r)
            log.info(f"  {acct}: status={r['final_status']} filled={r['filled']} "
                     f"errors={[(e.get('errorCode'), (e.get('errorString') or '')[:60]) for e in r['errors']]}")
            await asyncio.sleep(1.0)

        # Position snapshot AFTER.
        await asyncio.sleep(1.5)
        positions_after = list(await ib.reqPositionsAsync())

        # Any leftover orders now?
        open_trades_after = ib.openTrades()
    finally:
        ib.disconnect()
        log.info("Disconnected")

    # ─── REPORT ───
    print("\n" + "=" * 110)
    print(f"PER-ACCOUNT OUTCOME — SELL 1000 EURUSD CFD (0.01 lot)")
    print("=" * 110)
    print(f"{'Account':<12} {'orderId':>8} {'permId':>14} {'status':<14} "
          f"{'filled':>7} {'avg_fill':>10}  errors")
    print("-" * 110)
    for r in results:
        errs = ", ".join(f"{e.get('errorCode')}:{(e.get('errorString') or '')[:40]}" for e in r["errors"])
        print(f"{r['account']:<12} {str(r['orderId'] or ''):>8} {str(r['permId'] or ''):>14} "
              f"{str(r['final_status'] or '—'):<14} "
              f"{r['filled']:>7.1f} {r['avg_fill_price']:>10.5f}  "
              f"{errs or '—'}")

    print()
    print("=" * 110)
    print("FULL PER-ACCOUNT DETAIL (raw)")
    print("=" * 110)
    for r in results:
        print(f"\n────── {r['account']} ──────")
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
        print(f"  callbacks:")
        for ev in r["events"]:
            print(f"    {ev['event']}: "
                  + ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("t", "event")))
        print(f"  errors:")
        for e in r["errors"]:
            print(f"    errorCode={e.get('errorCode')} msg={e.get('errorString')}")

    print()
    print("=" * 110)
    print("POSITION SNAPSHOT AFTER SELL")
    print("=" * 110)
    if not positions_after:
        print("  (no open positions — all positions are flat)")
    for p in positions_after:
        print(f"  {p.account:<12} {p.contract.secType:<5} "
              f"{p.contract.symbol}/{p.contract.currency:<5} "
              f"qty={p.position:+g}  avgCost={p.avgCost}")

    print()
    print(f"Open trades remaining at API: {len(open_trades_after)}")
    for t in open_trades_after:
        print(f"  {t!r}")


def main():
    p = argparse.ArgumentParser(description="0.01 lot CFD SELL across all accounts")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=83)
    p.add_argument("--units", type=int, default=1000)
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
