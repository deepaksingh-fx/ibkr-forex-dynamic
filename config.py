"""
Strategy configuration. Frozen at startup.

The bot is selection-only: it logs the daily-narrowest CPR pair on every
17:00 NY rollover. No trading inputs (entry bands, loss caps, EMA, etc.)
are needed.

Broker rules retained for when trade placement is reattached:
  - LIVE_TRADING gate + read_only coupling (SPEC sec7)
  - LOT_SIZE (IDEALPRO minimum, unused at runtime)
  - MIN_ACCOUNT_BALANCE_USD ($1000 filter on FA sub-accounts)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# --- Hardcoded broker constants (NOT strategy inputs) ---------------------
LOT_SIZE: float = 0.25                  # 25k IDEALPRO base ccy; reserved for future trading
MIN_ACCOUNT_BALANCE_USD: float = 1000   # FA sub-accounts below this are inactive
TRADING_ZONE_POLL_SECONDS: int = 60     # outside-zone retry cadence


# --- Default symbol universe (13 pairs, user-chosen) -----------------------
# Order matters: narrowest_pair() uses first-appearance for tie-breaking.
DEFAULT_SYMBOLS: List[str] = [
    "USDJPY", "EURUSD", "EURJPY", "GBPUSD", "GBPJPY",
    "USDCAD", "CADJPY", "USDCHF", "CHFJPY",
    "AUDUSD", "AUDJPY", "NZDUSD", "NZDJPY",
]


@dataclass(frozen=True)
class IBKRConnection:
    """Connection target. Defaults to live Gateway (no paper available)."""
    host: str = "127.0.0.1"
    port: int = 4001                # IB Gateway live; 4002 paper (not used)
    client_id: int = 17
    read_only: bool = True          # belt-and-suspenders; auto-disabled for live
    connect_timeout: float = 10.0
    request_timeout: float = 30.0


@dataclass(frozen=True)
class StrategyConfig:
    """User-provided inputs. Validated in __post_init__."""
    # Only input: the universe of pairs to consider for daily narrowest-selection.
    symbols_list: tuple[str, ...] = tuple(DEFAULT_SYMBOLS)

    # Live-trading switch - Shadow mode (False) logs decisions only; Live
    # mode (True) places real orders via IBKRClient.place_market_order.
    LIVE_TRADING: bool = False

    # CFD trading account. Per the whatIf diagnostics only U25265693 has
    # CFD permission across this user's 4 FA sub-accounts. All orders route
    # here. (Set to a different account if your setup differs.)
    cfd_account: str = "U25265693"

    # Historical bars to pre-fetch per pair for indicator warmup at startup
    # and on each pair-change. 60 days covers the AST 60-day rolling window
    # for auto-selection scoring + all indicator warmup periods.
    warmup_days: int = 60

    # Lot size for CFD orders (in base-currency units). 1000 = 0.01 lot.
    cfd_units: int = 1000

    # Paths
    balance_file_path: str = "account_balances.json"

    # Connection
    ibkr: IBKRConnection = field(default_factory=IBKRConnection)

    def __post_init__(self) -> None:
        if not self.symbols_list:
            raise ValueError("symbols_list must be non-empty")
        # Live-trading + read_only is contradictory; refuse to start.
        if self.LIVE_TRADING and self.ibkr.read_only:
            raise ValueError(
                "LIVE_TRADING=True requires ibkr.read_only=False. "
                "Refusing to start with conflicting flags - fix your config."
            )
