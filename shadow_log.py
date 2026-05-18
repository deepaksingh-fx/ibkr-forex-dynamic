"""
Shadow-live event log.

Records every strategy decision (entry / exit / reversal / force-exit) to
a CSV file in real-time, so the user can compare hypothetical trades
against actual market evolution after a week of running.

One row per StrategyEvent. NY-tz timestamps in both ISO and human-readable.

Also rolls up trades (entry/exit pairs) into a separate trades CSV, with
points + pips computed.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cpr_st_strategy import StrategyEvent


logger = logging.getLogger(__name__)


def _pip_factor(symbol: str) -> int:
    """1 pip = 0.01 for JPY-quote pairs, 0.0001 otherwise."""
    return 100 if symbol[3:].upper() == "JPY" else 10000


class ShadowLog:
    """
    Per-session CSV writers for events and trades.

    Files (in `output_dir`):
      shadow_events_<session_start>.csv     - every strategy event
      shadow_trades_<session_start>.csv     - closed trades (entry/exit pairs)

    Both files are written incrementally (one row per event), so the CSVs
    are always up-to-date even if the bot is killed.
    """

    EVENT_FIELDS = [
        "timestamp_ny_iso", "timestamp_ny_display",
        "pair", "action", "reason", "price",
        "new_position",
        "bias", "regime", "regime_directional",
        "active_dir", "active_method",
        "daily_tc", "daily_bc", "daily_pivot",
    ]
    TRADE_FIELDS = [
        "entry_ts_iso", "entry_ts_display",
        "exit_ts_iso", "exit_ts_display",
        "pair", "side", "entry_price", "exit_price",
        "points", "pips", "bars_in_trade",
        "exit_reason", "was_reversal",
        "entry_regime", "exit_regime",
        "entry_active_method", "exit_active_method",
    ]

    def __init__(self, output_dir: Path, session_start_ny: datetime):
        self.output_dir = output_dir
        self.session_start = session_start_ny
        self.output_dir.mkdir(parents=True, exist_ok=True)
        tag = session_start_ny.strftime("%Y-%m-%d_%H%M")
        self.events_path = output_dir / f"shadow_events_{tag}.csv"
        self.trades_path = output_dir / f"shadow_trades_{tag}.csv"

        # Open files in append mode; write header if new.
        self._events_fp = self.events_path.open("a", newline="")
        self._events_w = csv.DictWriter(self._events_fp, fieldnames=self.EVENT_FIELDS)
        if self.events_path.stat().st_size == 0:
            self._events_w.writeheader()
            self._events_fp.flush()

        self._trades_fp = self.trades_path.open("a", newline="")
        self._trades_w = csv.DictWriter(self._trades_fp, fieldnames=self.TRADE_FIELDS)
        if self.trades_path.stat().st_size == 0:
            self._trades_w.writeheader()
            self._trades_fp.flush()

        # Assemble trades from entry/exit event pairs.
        self._open: Optional[dict] = None

        logger.info(f"Shadow log: events -> {self.events_path}")
        logger.info(f"Shadow log: trades -> {self.trades_path}")

    def record_event(self, pair: str, event: StrategyEvent,
                     daily_tc: float, daily_bc: float, daily_pivot: float):
        """Write one event row + maybe close out a trade."""
        ts = event.timestamp
        row = {
            "timestamp_ny_iso": ts.isoformat(timespec="seconds"),
            "timestamp_ny_display": ts.strftime("%a %Y-%m-%d %H:%M %Z"),
            "pair": pair,
            "action": event.action,
            "reason": event.reason,
            "price": f"{event.price:.6f}",
            "new_position": event.new_position,
            "bias": event.bias,
            "regime": event.regime,
            "regime_directional": event.regime_directional,
            "active_dir": event.active_dir,
            "active_method": event.active_method,
            "daily_tc": f"{daily_tc:.6f}",
            "daily_bc": f"{daily_bc:.6f}",
            "daily_pivot": f"{daily_pivot:.6f}",
        }
        self._events_w.writerow(row)
        self._events_fp.flush()

        # Trade assembly.
        if event.action in ("ENTRY_LONG", "ENTRY_SHORT",
                            "REVERSE_TO_LONG", "REVERSE_TO_SHORT"):
            side = "LONG" if event.action in ("ENTRY_LONG", "REVERSE_TO_LONG") else "SHORT"
            self._open = {
                "entry_ts": ts,
                "entry_price": event.price,
                "side": side,
                "pair": pair,
                "entry_regime": event.regime,
                "entry_active_method": event.active_method,
                "was_reversal": event.action.startswith("REVERSE_"),
            }
        elif event.action in ("EXIT_FLIP", "EXIT_EOD") and self._open is not None:
            o = self._open
            entry_px = o["entry_price"]
            exit_px = event.price
            pts = (exit_px - entry_px) if o["side"] == "LONG" else (entry_px - exit_px)
            bars = int((ts - o["entry_ts"]).total_seconds() // 300)
            pips = pts * _pip_factor(pair)
            self._trades_w.writerow({
                "entry_ts_iso": o["entry_ts"].isoformat(timespec="seconds"),
                "entry_ts_display": o["entry_ts"].strftime("%a %Y-%m-%d %H:%M %Z"),
                "exit_ts_iso": ts.isoformat(timespec="seconds"),
                "exit_ts_display": ts.strftime("%a %Y-%m-%d %H:%M %Z"),
                "pair": pair,
                "side": o["side"],
                "entry_price": f"{entry_px:.6f}",
                "exit_price": f"{exit_px:.6f}",
                "points": f"{pts:.6f}",
                "pips": f"{pips:.2f}",
                "bars_in_trade": bars,
                "exit_reason": event.reason,
                "was_reversal": o["was_reversal"],
                "entry_regime": o["entry_regime"],
                "exit_regime": event.regime,
                "entry_active_method": o["entry_active_method"],
                "exit_active_method": event.active_method,
            })
            self._trades_fp.flush()
            self._open = None

    def close(self):
        try:
            self._events_fp.close()
            self._trades_fp.close()
        except Exception:
            pass
