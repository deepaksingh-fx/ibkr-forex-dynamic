"""
Strategy state persistence + atomic write.

Persists the minimum needed to safely resume across restarts:
  - The CFD account we're trading on
  - The currently-selected pair (so we can detect cross-pair mismatches)
  - Strategy position (1/-1/0), entry price, entry timestamp
  - Last processed bar timestamp (for stream dedupe across restarts)

The actual indicator state (Regime + Adaptive SuperTrend internals) is NOT
persisted - it's rebuilt deterministically by replaying the 60-day warmup
window on every startup. The warmup gives identical state regardless of
when you start, assuming the same input data.

Atomic write pattern: write to .tmp -> fsync -> os.replace (POSIX-atomic).
Reads tolerate a missing file but raise on corruption or version mismatch.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CURRENT_VERSION = 1


@dataclass
class PersistedState:
    version: int = CURRENT_VERSION
    saved_at: str = ""                              # ISO 8601, set by save()
    cfd_account: str = ""
    selected_pair: Optional[str] = None
    position: int = 0                               # 1 = long, -1 = short, 0 = flat
    entry_price: Optional[float] = None
    entry_timestamp: Optional[str] = None           # ISO 8601 with tz
    last_processed_open_ny: Optional[str] = None    # ISO 8601 with tz


class StateStoreError(RuntimeError):
    pass


class StateStore:
    """JSON-backed atomic strategy state store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def save(self, state: PersistedState) -> None:
        state.saved_at = datetime.now(timezone.utc).isoformat()
        state.version = CURRENT_VERSION
        data = asdict(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        v = data.get("version")
        if v != CURRENT_VERSION:
            raise StateStoreError(
                f"Unsupported state version: {v!r} (expected {CURRENT_VERSION}). "
                f"Delete the state file and use --force-clean-restart."
            )
        return PersistedState(
            version=v,
            saved_at=data.get("saved_at", ""),
            cfd_account=data.get("cfd_account", ""),
            selected_pair=data.get("selected_pair"),
            position=int(data.get("position", 0)),
            entry_price=data.get("entry_price"),
            entry_timestamp=data.get("entry_timestamp"),
            last_processed_open_ny=data.get("last_processed_open_ny"),
        )
