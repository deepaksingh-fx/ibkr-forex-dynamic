"""
Asset selection (SPEC §9). Pure, no I/O.

Two-step:
  1. PRIMARY = pair in symbols_list with lowest weekly CPR width %.
     Tie-break: first appearance in symbols_list.
  2. If PRIMARY contains any allowed_currency → trade PRIMARY (1 pair).
     Else → cross-pair expansion: for each currency in PRIMARY, build
     candidates with each allowed_currency, take narrowest per side.
     Trade up to 2 pairs (one per PRIMARY-currency-side).

Edge 1: candidates not in symbols_list are silently dropped.
Edge 2: if a side has zero valid candidates after filtering → raise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from cpr import CPR
from pair_utils import construct_pair, pair_contains_any, split_pair


class SelectionError(RuntimeError):
    """Raised when selection cannot complete (e.g., empty side after expansion)."""


@dataclass(frozen=True)
class SelectionResult:
    primary: str
    shortlist: tuple[str, ...]   # 1 or 2 pairs
    expanded: bool               # True iff cross-pair expansion was triggered


def _narrowest(symbols: Sequence[str], cprs: Mapping[str, CPR], order: Sequence[str]) -> str | None:
    """
    Pick the narrowest of `symbols` by width_pct. Tie-break: first in `order`.
    Returns None if `symbols` is empty.
    """
    if not symbols:
        return None
    # Build (width_pct, position-in-order) so ties resolve to the earliest index.
    pos = {s: i for i, s in enumerate(order)}
    return min(symbols, key=lambda s: (cprs[s].width_pct, pos.get(s, 1 << 30)))


def select_shortlist(
    symbols_list: Sequence[str],
    allowed_currencies: Sequence[str],
    weekly_cprs: Mapping[str, CPR],
) -> SelectionResult:
    """
    Run asset selection.

    Args:
      symbols_list:       user-provided pair list (order matters for tie-breaking)
      allowed_currencies: user-provided allowed currencies (≥1)
      weekly_cprs:        precomputed weekly CPR per symbol (must cover symbols_list)

    Returns SelectionResult with `shortlist` of 1 or 2 pairs.
    Raises SelectionError if expansion produces an empty side.
    """
    if not symbols_list:
        raise SelectionError("symbols_list is empty")
    if not allowed_currencies:
        raise SelectionError("allowed_currencies is empty")
    missing = [s for s in symbols_list if s not in weekly_cprs]
    if missing:
        raise SelectionError(f"weekly_cprs missing entries for: {missing}")

    # Step 1: primary
    primary = _narrowest(list(symbols_list), weekly_cprs, symbols_list)
    assert primary is not None  # symbols_list is non-empty
    base, quote = split_pair(primary)

    # Step 2: does PRIMARY already qualify?
    if pair_contains_any(primary, allowed_currencies):
        return SelectionResult(primary=primary, shortlist=(primary,), expanded=False)

    # Expansion. For each currency in PRIMARY × each allowed currency.
    symbol_set = set(symbols_list)
    winners: list[str] = []
    for currency in (base, quote):
        candidates: list[str] = []
        for allowed in allowed_currencies:
            try:
                candidate = construct_pair(currency, allowed)
            except ValueError:
                continue  # same currency or unknown — skip
            # Edge 1: drop silently if not in user's symbols_list.
            if candidate in symbol_set and candidate in weekly_cprs:
                candidates.append(candidate)
        winner = _narrowest(candidates, weekly_cprs, symbols_list)
        if winner is None:
            # Edge 2: this side has no valid candidates.
            raise SelectionError(
                f"Cross-pair expansion: no valid candidates for currency {currency!r} "
                f"(primary={primary}, allowed={list(allowed_currencies)}). "
                f"Add the missing pair(s) to symbols_list."
            )
        winners.append(winner)

    return SelectionResult(primary=primary, shortlist=tuple(winners), expanded=True)
