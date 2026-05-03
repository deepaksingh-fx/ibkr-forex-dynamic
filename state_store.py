"""
Strategy state persistence + atomic writes.

State is written on every change and read on startup.
Combined with IBKR position reconciliation (in strategy.py), this enables
safe restart-mid-day.

Atomic write pattern:
  1. write to <path>.tmp in same directory
  2. fsync the file descriptor
  3. os.replace (atomic on POSIX)

Failure modes:
  - File missing on load → caller treats as first-run.
  - Malformed JSON / unsupported version → raise StateStoreError.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


CURRENT_VERSION = 1


@dataclass
class PersistedPosition:
    side: str               # "LONG" or "SHORT"
    entry_price: float
    entry_time: str         # ISO 8601 with tz
    trail_armed: bool


@dataclass
class PersistedState:
    version: int = CURRENT_VERSION
    saved_at: str = ""
    fx_day_start: str = ""
    shortlist: List[str] = field(default_factory=list)
    # pair → account → position
    positions: Dict[str, Dict[str, PersistedPosition]] = field(default_factory=dict)
    day_realized: Dict[str, float] = field(default_factory=dict)   # account → realized today
    halted: Dict[str, bool] = field(default_factory=dict)          # account → daily-breaker tripped


class StateStoreError(RuntimeError):
    pass


class StateStore:
    """JSON-backed atomic state store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def save(self, state: PersistedState) -> None:
        state.saved_at = datetime.now(timezone.utc).isoformat()
        data = _to_dict(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file → fsync → rename
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent,
            prefix=".strategy_state.", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = f.name
        os.replace(tmp_path, self.path)

    def load(self) -> PersistedState:
        if not self.path.exists():
            raise StateStoreError(f"State file not found: {self.path}")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StateStoreError(f"State file malformed: {e}") from e
        return _from_dict(data)


def _to_dict(state: PersistedState) -> dict:
    return {
        "version": state.version,
        "saved_at": state.saved_at,
        "fx_day_start": state.fx_day_start,
        "shortlist": list(state.shortlist),
        "positions": {
            pair: {acct: asdict(pos) for acct, pos in by_acct.items()}
            for pair, by_acct in state.positions.items()
        },
        "day_realized": {k: float(v) for k, v in state.day_realized.items()},
        "halted": {k: bool(v) for k, v in state.halted.items()},
    }


def _from_dict(data: dict) -> PersistedState:
    v = data.get("version")
    if v != CURRENT_VERSION:
        raise StateStoreError(f"Unsupported state version: {v!r} (expected {CURRENT_VERSION})")
    positions: Dict[str, Dict[str, PersistedPosition]] = {}
    for pair, by_acct in (data.get("positions") or {}).items():
        positions[pair] = {acct: PersistedPosition(**p) for acct, p in by_acct.items()}
    return PersistedState(
        version=v,
        saved_at=data.get("saved_at", ""),
        fx_day_start=data.get("fx_day_start", ""),
        shortlist=list(data.get("shortlist") or []),
        positions=positions,
        day_realized={k: float(v) for k, v in (data.get("day_realized") or {}).items()},
        halted={k: bool(v) for k, v in (data.get("halted") or {}).items()},
    )
