"""
Entry point for the COIL/session strategy (selection -> bias/DMI -> stop-and-
reverse -> CFD stop + breakeven). Monitor on spot, execute on CFD.

    python runner_coil.py                              # SHADOW (no orders)
    python runner_coil.py --live --i-really-mean-it    # LIVE (real CFD orders)

SHADOW is the default and places nothing. LIVE requires BOTH flags AND sets
read_only=False (the existing gate). SMOKE-TEST in shadow first.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from config import IBKRConnection, StrategyConfig
from coil_config import load_coil_config
from coil_runtime import CoilRuntime
from ibkr_client import IBKRClient


def main() -> int:
    p = argparse.ArgumentParser(description="Coil/session strategy runtime (shadow + live)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=53)
    p.add_argument("--config", help="JSON config (sessions + params)")
    p.add_argument("--units", type=int, default=30000, help="CFD units per trade")
    p.add_argument("--risk", type=float, default=50.0, help="USD risked per trade")
    p.add_argument("--breakeven", type=float, default=50.0, help="USD profit that arms breakeven")
    p.add_argument("--daily-limit", type=float, default=150.0, help="USD daily loss cap")
    p.add_argument("--live", action="store_true", help="LIVE mode - real CFD orders")
    p.add_argument("--i-really-mean-it", action="store_true", help="Required with --live")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("runner_coil")

    live = bool(args.live and args.i_really_mean_it)
    cfg = StrategyConfig(
        LIVE_TRADING=live,
        cfd_units=args.units,
        ibkr=IBKRConnection(host=args.host, port=args.port,
                            client_id=args.client_id, read_only=not live),
    )
    sessions, params, _feed = load_coil_config(args.config)

    if live:
        log.warning("=" * 60)
        log.warning("LIVE TRADING ENABLED - REAL CFD ORDERS WILL BE PLACED")
        log.warning(f"account={cfg.cfd_account} units={cfg.cfd_units} "
                    f"risk=${args.risk} BE=${args.breakeven} dailyLimit=${args.daily_limit}")
        log.warning("=" * 60)
    else:
        log.info("SHADOW mode - no orders; logging intents only. Smoke-test before going live.")

    ibkr = IBKRClient(cfg)
    runtime = CoilRuntime(cfg, ibkr, params=params, sessions=sessions,
                          risk_per_trade=args.risk, breakeven_trigger=args.breakeven,
                          daily_loss_limit=args.daily_limit)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    signal.signal(signal.SIGINT, lambda *_: runtime.stop())
    signal.signal(signal.SIGTERM, lambda *_: runtime.stop())
    try:
        loop.run_until_complete(runtime.run())
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        log.exception("fatal error in coil runtime")
        return 1
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
