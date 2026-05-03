"""
forex_cpr_ibkr — entry point.

One command:
    python runner.py

Defaults to DRY-RUN (no real orders). Override with explicit flags only.

Configure the strategy by editing the `make_config()` factory below.
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
from pnl_tracker import SimulatedPnLTracker
from state_store import StateStore
from strategy import Strategy


def make_config(args) -> StrategyConfig:
    """Build the StrategyConfig. Edit defaults here as needed."""
    # Default = DRY-RUN. Live trading ONLY if --i-really-mean-it explicit flag.
    live = bool(args.live and args.i_really_mean_it)
    return StrategyConfig(
        # Asset universe — defaults to all 15 pairs.
        # symbols_list=("EURUSD", "USDJPY", ...),

        # Required: the allowed currencies that gate cross-pair expansion.
        allowed_currencies=tuple(args.allowed.split(",")) if args.allowed else ("USD",),

        # Trigger / risk
        entry_trigger_range_pct=args.trigger_pct,
        per_trade_loss_pct=args.trade_loss_pct,
        per_day_loss_pct=args.day_loss_pct,

        # Live switch — defaults to False (dry-run).
        LIVE_TRADING=live,
        balance_file_path=str(args.balance_file),
        ibkr=IBKRConnection(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            read_only=not live,                 # belt-and-suspenders
        ),
    )


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    p = argparse.ArgumentParser(description="forex_cpr_ibkr — one command, automatic dry-run")
    p.add_argument("--allowed", default="USD",
                   help="Comma-separated allowed currencies, e.g. USD,JPY (default: USD)")
    p.add_argument("--trigger-pct", type=float, default=0.05,
                   help="Entry trigger band %% (default: 0.05)")
    p.add_argument("--trade-loss-pct", type=float, default=1.0,
                   help="Per-trade SL %% of frozen balance (default: 1.0)")
    p.add_argument("--day-loss-pct", type=float, default=2.0,
                   help="Per-day breaker %% of frozen balance (default: 2.0)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=42)
    p.add_argument("--balance-file", type=Path, default=Path("account_balances.json"))
    p.add_argument("--state-file", type=Path, default=Path("strategy_state.json"),
                   help="Persisted strategy state for restart recovery.")
    p.add_argument("--force-clean-restart", action="store_true",
                   help="Wipe state file at startup; skip IBKR position reconciliation. "
                        "DANGER: ignores any positions that exist in IBKR.")
    p.add_argument("--verbose", "-v", action="store_true")
    # DANGEROUS — live mode requires both flags.
    p.add_argument("--live", action="store_true",
                   help="Enable live trading (requires --i-really-mean-it).")
    p.add_argument("--i-really-mean-it", action="store_true",
                   help="Confirm intent to place REAL orders. Required with --live.")
    args = p.parse_args()

    configure_logging(args.verbose)
    log = logging.getLogger("runner")

    cfg = make_config(args)

    if cfg.LIVE_TRADING:
        log.warning("=" * 60)
        log.warning("LIVE TRADING IS ENABLED — REAL ORDERS WILL BE PLACED.")
        log.warning("=" * 60)
    else:
        log.info("Dry-run mode (no orders will be placed)")

    log.info(f"Config: allowed={list(cfg.allowed_currencies)} "
             f"entry_pct={cfg.entry_trigger_range_pct} "
             f"trade_loss={cfg.per_trade_loss_pct}% "
             f"day_loss={cfg.per_day_loss_pct}% "
             f"port={cfg.ibkr.port} "
             f"clientId={cfg.ibkr.client_id}")

    ibkr = IBKRClient(cfg)
    pnl = SimulatedPnLTracker()
    balances = BalanceStore(cfg.balance_file_path)
    state_store = StateStore(args.state_file)
    strategy = Strategy(
        cfg, ibkr, pnl, balances,
        state_store=state_store,
        force_clean_restart=args.force_clean_restart,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(signum, frame):
        log.info(f"Signal {signum} received — stopping...")
        strategy.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(strategy.run())
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception:
        log.exception("Fatal error in strategy")
        return 1
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
