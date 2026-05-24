"""
forex_cpr_ibkr - entry point for the PIVOT/SR + Quality-SuperTrend strategy.

    python runner_pivot.py                          # SHADOW mode (no orders)
    python runner_pivot.py --live --i-really-mean-it  # LIVE mode (real CFD orders)

This runs the NEW strategy (pivot/SR anchors + quality-filtered SuperTrend).
The old CPR strategy is unaffected - use `runner.py` for that one.

What it does:
  - Connects to IBKR.
  - At each 17:00 NY rollover, picks the daily-narrowest CPR pair (unchanged).
  - Pre-fetches `warmup_days` (60) of 5-min bars to warm the SuperTrend.
  - Streams 5-min bars and runs the pivot state machine bar-by-bar:
      base level seeded on the day's first candle (no entry there), dynamic
      base updates on touch, 2-gate entry (close vs base + GREEN/RED), exit on
      SuperTrend flip or 16:55 force-EOD, same-bar reversal allowed.

Shadow mode (default): no orders; decisions logged to
backtest_output/pivot_shadow/pivot_events_<session>.csv (+ trades CSV).

Live mode: real CFD market orders on cfd_account. Requires BOTH --live AND
--i-really-mean-it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from balance_store import BalanceStore
from config import IBKRConnection, StrategyConfig
from ibkr_client import IBKRClient
from pivot_runtime import PivotRuntime
from state_store import StateStore


def make_config(args) -> StrategyConfig:
    import os

    live = bool(args.live and args.i_really_mean_it)
    lot_size_lots = float(os.environ.get("LOT_SIZE", "0.3"))
    cfd_units = int(round(lot_size_lots * 100_000))
    cfd_account = os.environ.get("CFD_ACCOUNT", "U25265693")

    return StrategyConfig(
        LIVE_TRADING=live,
        balance_file_path=str(args.balance_file),
        cfd_account=cfd_account,
        cfd_units=cfd_units,
        ibkr=IBKRConnection(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            read_only=not live,
        ),
    )


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="forex_cpr_ibkr - pivot/SR + quality-SuperTrend strategy (shadow + live)"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=43)
    p.add_argument("--balance-file", type=Path, default=Path("account_balances.json"))
    p.add_argument("--state-file", type=Path, default=Path("pivot_strategy_state.json"),
                   help="Persisted strategy state for restart recovery.")
    p.add_argument("--force-clean-restart", action="store_true",
                   help="Wipe state file at startup; skip IBKR reconciliation. "
                        "DANGEROUS: ignores positions that exist in IBKR.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="LIVE mode - places real CFD orders. Default is SHADOW.")
    p.add_argument("--i-really-mean-it", action="store_true",
                   help="Required with --live to confirm intent to place REAL orders.")
    args = p.parse_args()

    configure_logging(args.verbose)
    log = logging.getLogger("runner_pivot")

    cfg = make_config(args)

    if cfg.LIVE_TRADING:
        log.warning("=" * 60)
        log.warning("LIVE TRADING IS ENABLED - REAL CFD ORDERS WILL BE PLACED.")
        log.warning(f"Account: {cfg.cfd_account}  Units per order: {cfg.cfd_units}")
        log.warning("=" * 60)
    else:
        log.info("SHADOW mode (read-only socket; no orders placed - decisions logged only)")

    log.info(
        f"[PIVOT] Config: symbols={len(cfg.symbols_list)} pairs port={cfg.ibkr.port} "
        f"clientId={cfg.ibkr.client_id} cfd_account={cfg.cfd_account} "
        f"cfd_units={cfg.cfd_units} (= {cfg.cfd_units / 100_000:.2f} lot)"
    )

    ibkr = IBKRClient(cfg)
    balances = BalanceStore(cfg.balance_file_path)
    state_store = StateStore(args.state_file)
    runtime = PivotRuntime(
        cfg, ibkr, balances,
        state_store=state_store,
        force_clean_restart=args.force_clean_restart,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(signum, frame):
        log.info(f"Signal {signum} received - stopping...")
        runtime.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(runtime.run())
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception:
        log.exception("Fatal error in pivot runtime")
        return 1
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
