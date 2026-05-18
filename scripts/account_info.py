"""
Read-only IBKR account inspector.

What it does:
  1. Connects to IB Gateway read-only.
  2. Lists every managed account.
  3. For each account: full accountSummary + accountValues dump.
  4. Lists open positions across all accounts.
  5. Probes CFD contract availability for the 15 default forex pairs.

NO ORDERS are placed. Read-only connection.

Output:
  - Console dump (everything)
  - Markdown file in --output-dir summarising the key data

Usage:
    python scripts/account_info.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, Contract, Forex  # type: ignore[import-untyped]

from config import DEFAULT_SYMBOLS
from time_utils import ny_now


# Keys we surface in the headline per-account summary.
KEY_TAGS = [
    "AccountType",
    "AccountReady",
    "AccountCode",
    "Currency",
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "AvailableFunds",
    "ExcessLiquidity",
    "GrossPositionValue",
    "MaintMarginReq",
    "InitMarginReq",
    "Leverage",
    "Cushion",
]


def configure_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def dump_account(ib: IB, account: str) -> dict:
    """Pull everything we can for one account. Returns a dict for the report."""
    summary_rows = await ib.accountSummaryAsync(account=account)
    # accountValues is a snapshot of EVERY tag/currency for the account.
    # It's populated by ib.reqAccountUpdates which the gateway runs after connect.
    all_values = ib.accountValues(account=account)

    # Build a dict of tag → list[(value, currency)] from accountValues.
    by_tag: dict[str, list] = defaultdict(list)
    for v in all_values:
        by_tag[v.tag].append((v.value, v.currency))

    # Key headline metrics: prefer USD or BASE entries when multiple currencies.
    headline = {}
    for tag in KEY_TAGS:
        rows = by_tag.get(tag, [])
        chosen = None
        for val, ccy in rows:
            if ccy in ("USD", "BASE", ""):
                chosen = (val, ccy)
                break
        if chosen is None and rows:
            chosen = rows[0]
        if chosen:
            headline[tag] = chosen

    # Cash balances per currency (CashBalance tag, excluding BASE).
    cash_balances: dict[str, float] = {}
    for val, ccy in by_tag.get("CashBalance", []):
        if ccy == "BASE":
            continue
        try:
            f = float(val)
            if abs(f) > 1e-6:
                cash_balances[ccy] = f
        except (ValueError, TypeError):
            pass

    return {
        "account": account,
        "headline": headline,
        "cash_balances": cash_balances,
        "all_values": all_values,
        "tag_count": len(by_tag),
    }


async def try_qualify_cfd(ib: IB, symbol: str) -> dict:
    """Attempt to qualify the FX CFD contract for `symbol` (e.g. EURUSD).
    Returns whatever info we can collect; never raises."""
    base, quote = symbol[:3], symbol[3:]
    contract = Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
    try:
        result = await ib.qualifyContractsAsync(contract)
        if result and result[0] is not None:
            q = result[0]
            return {
                "symbol": symbol,
                "qualified": True,
                "conId": q.conId,
                "exchange": q.exchange,
                "primary_exchange": getattr(q, "primaryExchange", ""),
                "currency": q.currency,
                "local_symbol": getattr(q, "localSymbol", ""),
                "trading_class": getattr(q, "tradingClass", ""),
                "error": None,
            }
    except Exception as e:
        return {
            "symbol": symbol,
            "qualified": False,
            "error": f"{type(e).__name__}: {e}",
        }
    return {"symbol": symbol, "qualified": False, "error": "no qualified result"}


async def run(host: str, port: int, client_id: int, output_dir: Path):
    log = logging.getLogger("account_info")
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=10.0)
    log.info(f"Connected to {host}:{port} clientId={client_id} (READ-ONLY)")

    try:
        # Let the gateway populate accountValues / positions.
        await asyncio.sleep(1.5)

        managed = list(ib.managedAccounts())
        log.info(f"Managed accounts: {managed}")

        # Per-account dump.
        per_account = []
        for acct in managed:
            log.info(f"Fetching details for {acct}...")
            info = await dump_account(ib, acct)
            per_account.append(info)

        # Open positions across all accounts.
        positions = await ib.reqPositionsAsync()
        positions = list(positions)
        log.info(f"Total open positions across all accounts: {len(positions)}")

        # CFD contract probe.
        log.info(f"Probing CFD contracts for {len(DEFAULT_SYMBOLS)} forex pairs...")
        cfd_results = []
        for sym in DEFAULT_SYMBOLS:
            r = await try_qualify_cfd(ib, sym)
            mark = "✓" if r.get("qualified") else "✗"
            log.info(f"  {sym}: {mark}  conId={r.get('conId')}  exchange={r.get('exchange')}  "
                     f"err={r.get('error')}")
            cfd_results.append(r)
    finally:
        ib.disconnect()

    # ───────── CONSOLE DUMP ─────────
    print()
    print("=" * 100)
    print(f"IBKR ACCOUNT INSPECTION — {ny_now().strftime('%a %Y-%m-%d %H:%M %Z')}")
    print(f"Host: {host}:{port}   Client ID: {client_id}   READ-ONLY")
    print("=" * 100)
    print()
    print(f"Managed accounts ({len(managed)}): {managed}")
    print()

    for info in per_account:
        a = info["account"]
        print("-" * 100)
        print(f"ACCOUNT: {a}")
        print("-" * 100)
        for k, (v, ccy) in info["headline"].items():
            print(f"  {k:<22} {v} {ccy}")
        if info["cash_balances"]:
            print(f"  Cash balances by currency:")
            for ccy, bal in sorted(info["cash_balances"].items()):
                print(f"    {ccy:<5} {bal:>14.2f}")
        print(f"  (Total tags exposed by IBKR for this account: {info['tag_count']})")
        print()

    print("-" * 100)
    print(f"OPEN POSITIONS ({len(positions)})")
    print("-" * 100)
    if not positions:
        print("  (none)")
    else:
        for p in positions:
            try:
                con = p.contract
                print(f"  {p.account}  {con.secType:<6} {con.symbol}/{con.currency:<5}"
                      f" qty={p.position:+g}  avgCost={p.avgCost}")
            except Exception:
                print(f"  (malformed: {p!r})")
    print()

    print("-" * 100)
    print(f"FX CFD CONTRACT AVAILABILITY ({len(cfd_results)} probes)")
    print("-" * 100)
    print(f"  {'Pair':<8} {'OK?':<4} {'conId':>10} {'Exchange':<10} {'Local':<12} {'Class':<10} {'Error'}")
    for r in cfd_results:
        ok = "yes" if r.get("qualified") else "no"
        print(
            f"  {r['symbol']:<8} {ok:<4} "
            f"{str(r.get('conId') or ''):>10} "
            f"{str(r.get('exchange') or ''):<10} "
            f"{str(r.get('local_symbol') or ''):<12} "
            f"{str(r.get('trading_class') or ''):<10} "
            f"{r.get('error') or ''}"
        )

    # ───────── MARKDOWN FILE ─────────
    output_dir.mkdir(parents=True, exist_ok=True)
    md = output_dir / f"account_info_{ny_now().strftime('%Y-%m-%d_%H%M')}.md"
    lines = []
    lines.append(f"# IBKR Account Inspection\n")
    lines.append(f"Generated: {ny_now().strftime('%a %Y-%m-%d %H:%M %Z')}")
    lines.append(f"\nGateway: `{host}:{port}` (client {client_id}, **READ-ONLY**)\n")
    lines.append(f"\n## Managed accounts ({len(managed)})\n")
    lines.append(", ".join(f"`{a}`" for a in managed))
    for info in per_account:
        a = info["account"]
        lines.append(f"\n### Account `{a}`\n")
        lines.append("| Field | Value | Currency |")
        lines.append("|---|---|---|")
        for k, (v, ccy) in info["headline"].items():
            lines.append(f"| {k} | {v} | {ccy} |")
        if info["cash_balances"]:
            lines.append(f"\n**Cash balances by currency:**\n")
            lines.append("| Currency | Balance |")
            lines.append("|---|---:|")
            for ccy, bal in sorted(info["cash_balances"].items()):
                lines.append(f"| {ccy} | {bal:,.2f} |")
    lines.append(f"\n## Open positions ({len(positions)})\n")
    if not positions:
        lines.append("None.\n")
    else:
        lines.append("| Account | SecType | Symbol/Currency | Qty | AvgCost |")
        lines.append("|---|---|---|---:|---:|")
        for p in positions:
            try:
                con = p.contract
                lines.append(f"| {p.account} | {con.secType} | {con.symbol}/{con.currency} | "
                             f"{p.position:+g} | {p.avgCost} |")
            except Exception:
                lines.append(f"| ? | ? | (malformed) | | |")
    lines.append(f"\n## FX CFD contract availability\n")
    lines.append("| Pair | Qualified | conId | Exchange | Local | Trading Class | Error |")
    lines.append("|---|---|---:|---|---|---|---|")
    for r in cfd_results:
        ok = "✓" if r.get("qualified") else "✗"
        lines.append(
            f"| {r['symbol']} | {ok} | "
            f"{r.get('conId') or ''} | "
            f"{r.get('exchange') or ''} | "
            f"{r.get('local_symbol') or ''} | "
            f"{r.get('trading_class') or ''} | "
            f"{r.get('error') or ''} |"
        )
    md.write_text("\n".join(lines) + "\n")
    print()
    print(f"Markdown report saved: {md}")


def main():
    p = argparse.ArgumentParser(description="Read-only IBKR account inspector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=70)
    p.add_argument("--output-dir", type=Path, default=Path("backtest_output"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)
    asyncio.run(run(args.host, args.port, args.client_id, args.output_dir))


if __name__ == "__main__":
    main()
