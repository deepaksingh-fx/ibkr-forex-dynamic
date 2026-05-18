"""
forex_cpr_ibkr — entry point.

    python runner.py                          # SHADOW mode (no orders)
    python runner.py --live --i-really-mean-it  # LIVE mode (real CFD orders)

What it does:
  - Connects to IBKR.
  - At each 17:00 NY rollover, picks the daily-narrowest CPR pair.
  - Pre-fetches `warmup_days` (60) of 5-min bars to warm indicators.
  - Streams 5-min bars and runs the CPR + Regime + AdaptiveSuperTrend
    state machine bar-by-bar (entry: 3-gate alignment; exit: ST flip or
    16:55 force-EOD; same-bar reversal allowed).

Shadow mode (default): no orders placed. Every strategy decision is logged
to backtest_output/shadow/shadow_events_<session>.csv. Trades are rolled
up into shadow_trades_<session>.csv with points + pips.

Live mode: real CFD market orders are placed on cfd_account (default
U25265693 — the only account with CFD permission per diagnostics).
Requires BOTH --live AND --i-really-mean-it flags.
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
from state_store import StateStore
from strategy import Strategy


def make_config(args) -> StrategyConfig:
    """Build the StrategyConfig.

    Env-editable overrides:
      LOT_SIZE      decimal lots, default 0.3 (= 30,000 base-ccy units)
      CFD_ACCOUNT   IBKR account ID, default U25265693
    """
    import os

    live = bool(args.live and args.i_really_mean_it)

    # LOT_SIZE in decimal lots (0.3 = 30k units). 1 lot = 100,000 units.
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
    p = argparse.ArgumentParser(
        description="forex_cpr_ibkr — CPR/Regime/AST strategy with shadow + live modes"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=42)
    p.add_argument("--balance-file", type=Path, default=Path("account_balances.json"))
    p.add_argument("--state-file", type=Path, default=Path("strategy_state.json"),
                   help="Persisted strategy state for restart recovery.")
    p.add_argument("--force-clean-restart", action="store_true",
                   help="Wipe state file at startup; skip IBKR position "
                        "reconciliation. DANGEROUS: ignores any positions that "
                        "exist in IBKR.")
    p.add_argument("--verbose", "-v", action="store_true")
    # Live flags retained as plumbing for future trading rules.
    p.add_argument("--live", action="store_true",
                   help="LIVE mode — places real CFD orders. Default is SHADOW (no orders).")
    p.add_argument("--i-really-mean-it", action="store_true",
                   help="Required with --live to confirm intent to place REAL orders.")
    args = p.parse_args()

    configure_logging(args.verbose)
    log = logging.getLogger("runner")

    cfg = make_config(args)

    if cfg.LIVE_TRADING:
        log.warning("=" * 60)
        log.warning("LIVE_TRADING enabled — socket is NOT read-only.")
        log.warning("(This build does not place orders regardless.)")
        log.warning("=" * 60)
    else:
        log.info("Dry-run mode (read-only socket; no orders placed)")

    log.info(
        f"Config: symbols={len(cfg.symbols_list)} pairs "
        f"port={cfg.ibkr.port} clientId={cfg.ibkr.client_id} "
        f"cfd_account={cfg.cfd_account} cfd_units={cfg.cfd_units} "
        f"(= {cfg.cfd_units / 100_000:.2f} lot)"
    )

    ibkr = IBKRClient(cfg)
    balances = BalanceStore(cfg.balance_file_path)
    state_store = StateStore(args.state_file)
    strategy = Strategy(
        cfg, ibkr, balances,
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
