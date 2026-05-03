"""
JSON balance store with $1000+ filter (SPEC §12.2 + §13.0).

First run: fetch from IBKR, write file.
Subsequent runs: read only, never overwrite.
File corruption → raise, manual fix required.

Active accounts = balances ≥ MIN_ACCOUNT_BALANCE_USD ($1000).
Inactive accounts get NO orders, NO PnL subscriptions, NO loss caps.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from config import MIN_ACCOUNT_BALANCE_USD


class BalanceStoreError(RuntimeError):
    pass


class BalanceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._balances: Dict[str, float] | None = None

    def has_file(self) -> bool:
        return self.path.exists()

    def load(self) -> None:
        if not self.path.exists():
            raise BalanceStoreError(f"No balance file at {self.path}; call init_from() first")
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            raise BalanceStoreError(
                f"Balance file {self.path} is corrupted: {e}. "
                f"Delete it manually to rebuild from IBKR, or fix the JSON."
            ) from e
        if not isinstance(data, dict):
            raise BalanceStoreError(f"Balance file {self.path} must be a JSON object")
        try:
            self._balances = {str(k): float(v) for k, v in data.items()}
        except (ValueError, TypeError) as e:
            raise BalanceStoreError(f"Balance file {self.path} has non-numeric values: {e}") from e

    def init_from(self, balances: Dict[str, float]) -> None:
        """Write first-time snapshot. Refuses to overwrite an existing file."""
        if self.path.exists():
            raise BalanceStoreError(
                f"Balance file {self.path} already exists; refusing to overwrite. "
                f"Frozen balances are immutable per spec."
            )
        self._balances = {k: float(v) for k, v in balances.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._balances, indent=2))

    def all_balances(self) -> Dict[str, float]:
        if self._balances is None:
            raise BalanceStoreError("BalanceStore not loaded — call load() or init_from()")
        return dict(self._balances)

    def active_accounts(self) -> Dict[str, float]:
        """Accounts with frozen balance >= MIN_ACCOUNT_BALANCE_USD."""
        return {a: b for a, b in self.all_balances().items() if b >= MIN_ACCOUNT_BALANCE_USD}

    def balance_of(self, account: str) -> float:
        return self.all_balances().get(account, 0.0)
