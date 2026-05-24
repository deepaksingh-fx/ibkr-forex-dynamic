"""
Shadow-live event log for the pivot/SR + quality-SuperTrend strategy.

Mirrors `shadow_log.py` but records the new strategy's telemetry: the dynamic
base level (name + price), the close-vs-base bias, and the SuperTrend state
(GREEN/RED/GREY) instead of regime/AST fields.

One row per PivotEvent (events CSV); entry/exit pairs roll up into a trades CSV
with points + pips. Both written incrementally so the files stay current even
if the bot is killed.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from pivot_st_strategy import PivotEvent


logger = logging.getLogger(__name__)


def _pip_factor(symbol: str) -> int:
    """1 pip = 0.01 for JPY-quote pairs, 0.0001 otherwise."""
    return 100 if symbol[3:].upper() == "JPY" else 10000


class PivotShadowLog:
    EVENT_FIELDS = [
        "timestamp_ny_iso", "timestamp_ny_display",
        "pair", "action", "reason", "price", "new_position",
        "bias", "base_level_name", "base_level",
        "st_state", "st_dir",
    ]
    TRADE_FIELDS = [
        "entry_ts_iso", "entry_ts_display",
        "exit_ts_iso", "exit_ts_display",
        "pair", "side", "entry_price", "exit_price",
        "points", "pips", "bars_in_trade",
        "exit_reason", "was_reversal",
        "entry_base", "exit_base",
    ]

    def __init__(self, output_dir: Path, session_start_ny: datetime):
        self.output_dir = output_dir
        self.session_start = session_start_ny
        self.output_dir.mkdir(parents=True, exist_ok=True)
        tag = session_start_ny.strftime("%Y-%m-%d_%H%M")
        self.events_path = output_dir / f"pivot_events_{tag}.csv"
        self.trades_path = output_dir / f"pivot_trades_{tag}.csv"

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

        self._open: Optional[dict] = None

        logger.info(f"Pivot shadow log: events -> {self.events_path}")
        logger.info(f"Pivot shadow log: trades -> {self.trades_path}")

    def record_event(self, pair: str, event: PivotEvent):
        ts = event.timestamp
        self._events_w.writerow({
            "timestamp_ny_iso": ts.isoformat(timespec="seconds"),
            "timestamp_ny_display": ts.strftime("%a %Y-%m-%d %H:%M %Z"),
            "pair": pair,
            "action": event.action,
            "reason": event.reason,
            "price": f"{event.price:.6f}",
            "new_position": event.new_position,
            "bias": event.bias,
            "base_level_name": event.base_level_name,
            "base_level": f"{event.base_level:.6f}",
            "st_state": event.st_state,
            "st_dir": event.st_dir,
        })
        self._events_fp.flush()

        if event.action in ("ENTRY_LONG", "ENTRY_SHORT",
                             "REVERSE_TO_LONG", "REVERSE_TO_SHORT"):
            side = "LONG" if event.action in ("ENTRY_LONG", "REVERSE_TO_LONG") else "SHORT"
            self._open = {
                "entry_ts": ts,
                "entry_price": event.price,
                "side": side,
                "entry_base": event.base_level_name,
                "was_reversal": event.action.startswith("REVERSE_"),
            }
        elif event.action in ("EXIT_FLIP", "EXIT_EOD") and self._open is not None:
            o = self._open
            entry_px, exit_px = o["entry_price"], event.price
            pts = (exit_px - entry_px) if o["side"] == "LONG" else (entry_px - exit_px)
            bars = int((ts - o["entry_ts"]).total_seconds() // 300)
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
                "pips": f"{pts * _pip_factor(pair):.2f}",
                "bars_in_trade": bars,
                "exit_reason": event.reason,
                "was_reversal": o["was_reversal"],
                "entry_base": o["entry_base"],
                "exit_base": event.base_level_name,
            })
            self._trades_fp.flush()
            self._open = None

    def close(self):
        try:
            self._events_fp.close()
            self._trades_fp.close()
        except Exception:
            pass
