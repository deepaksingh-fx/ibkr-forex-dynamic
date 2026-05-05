"""
Strategy configuration. Frozen at startup.

The user provides a `StrategyConfig`. All other tunables are constants below.
The `LIVE_TRADING` flag is the single switch that decides whether real orders
go to IBKR. Dry-run is the default. See SPEC.md §14 for safety rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# ─── Hardcoded tunables (NOT strategy inputs) ─────────────────────────────
LOT_SIZE: float = 0.20                  # 20k units; at IDEALPRO threshold — surfaces in
                                         # reqPositionsAsync, allows trail-arm to reach
                                         # the 0.5%-of-balance threshold on $10k accounts.
                                         # Margin ≈ $170/position; 1 pip ≈ $2.00.
MIN_ACCOUNT_BALANCE_USD: float = 1000   # accounts below this are excluded
TRAIL_ARM_PCT: float = 0.5              # arm trail at 0.5% of frozen balance
EMA_PERIOD: int = 50                    # 50-period EMA on 5-min closes
EMA_PREWARM_BARS: int = 250             # 5x EMA period for clean convergence
TRADING_ZONE_POLL_SECONDS: int = 60     # outside-zone retry cadence


# ─── Default symbol universe ──────────────────────────────────────────────
DEFAULT_SYMBOLS: List[str] = [
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "USDCAD",
    "EURJPY", "EURGBP", "EURCHF", "EURCAD",
    "GBPJPY", "GBPCHF", "GBPCAD",
    "CHFJPY", "CADCHF", "CADJPY",
]


@dataclass(frozen=True)
class IBKRConnection:
    """Connection target. Defaults to live Gateway (no paper available)."""
    host: str = "127.0.0.1"
    port: int = 4001                # IB Gateway live; 4002 paper (not used)
    client_id: int = 17
    read_only: bool = True          # belt-and-suspenders during dev
    connect_timeout: float = 10.0
    request_timeout: float = 30.0


@dataclass(frozen=True)
class StrategyConfig:
    """
    User-provided strategy inputs. Validated in __post_init__.

    See SPEC.md §1.1 for definitions.
    """
    # User inputs (see §1.1)
    symbols_list: tuple[str, ...] = tuple(DEFAULT_SYMBOLS)
    allowed_currencies: tuple[str, ...] = ()              # MUST be ≥1
    entry_trigger_range_pct: float = 0.05
    per_trade_loss_pct: float = 1.0
    per_day_loss_pct: float = 2.0

    # Live-trading switch — DEFAULT FALSE. Real orders only fire when True.
    LIVE_TRADING: bool = False

    # Paths
    balance_file_path: str = "account_balances.json"

    # Connection
    ibkr: IBKRConnection = field(default_factory=IBKRConnection)

    def __post_init__(self) -> None:
        if not self.symbols_list:
            raise ValueError("symbols_list must be non-empty")
        if not self.allowed_currencies:
            raise ValueError("allowed_currencies must contain at least one currency")
        if self.entry_trigger_range_pct <= 0:
            raise ValueError("entry_trigger_range_pct must be > 0")
        if self.per_trade_loss_pct <= 0:
            raise ValueError("per_trade_loss_pct must be > 0")
        if self.per_day_loss_pct <= 0:
            raise ValueError("per_day_loss_pct must be > 0")

        # Normalize allowed_currencies to upper-case (frozen tuple swap).
        upper = tuple(c.upper().strip() for c in self.allowed_currencies)
        object.__setattr__(self, "allowed_currencies", upper)

        # Live-trading + read_only is contradictory; force read_only=False if going live.
        if self.LIVE_TRADING and self.ibkr.read_only:
            raise ValueError(
                "LIVE_TRADING=True requires ibkr.read_only=False. "
                "Refusing to start with conflicting flags — fix your config."
            )
