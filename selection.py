"""
Daily-narrowest pair selection (SPEC sec5). Pure, no I/O.

Pick the symbol in `symbols_list` with the lowest CPR width %. Ties resolve
to the first appearance in `symbols_list`.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from cpr import CPR


class SelectionError(RuntimeError):
    """Raised when selection cannot complete (missing CPRs, empty input)."""


def narrowest_pair(
    symbols_list: Sequence[str],
    cprs: Mapping[str, CPR],
) -> str:
    """
    Return the symbol with the lowest CPR width %.

    Args:
      symbols_list: candidate pairs. Order matters for tie-breaking.
      cprs:         CPR per symbol. Must cover every symbol in symbols_list.

    Raises:
      SelectionError: empty input or missing CPRs.
    """
    if not symbols_list:
        raise SelectionError("symbols_list is empty")
    missing = [s for s in symbols_list if s not in cprs]
    if missing:
        raise SelectionError(f"cprs missing entries for: {missing}")

    pos = {s: i for i, s in enumerate(symbols_list)}
    return min(symbols_list, key=lambda s: (cprs[s].width_pct, pos[s]))
