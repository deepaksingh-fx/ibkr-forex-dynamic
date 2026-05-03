"""
Currency-hierarchy-aware pair construction.

The forex pair-naming convention orders currencies by a hierarchy:
    EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY

Higher rank becomes the BASE; lower rank becomes the QUOTE.
e.g. EUR + USD → EURUSD, USD + JPY → USDJPY, USD + CHF → USDCHF.

This matches IBKR / IDEALPRO conventions for the 15-pair default universe.
"""
from __future__ import annotations

from typing import List, Tuple


HIERARCHY: List[str] = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]
_RANK = {ccy: i for i, ccy in enumerate(HIERARCHY)}


class UnknownCurrency(ValueError):
    pass


def split_pair(symbol: str) -> Tuple[str, str]:
    """'EURUSD' → ('EUR', 'USD'). 6-char alpha required."""
    s = symbol.upper().strip()
    if len(s) != 6 or not s.isalpha():
        raise ValueError(f"Not a forex pair symbol: {symbol!r}")
    return s[:3], s[3:]


def construct_pair(c1: str, c2: str) -> str:
    """
    Combine two currencies into the canonical pair name per the hierarchy.
    Order-agnostic: construct_pair('USD', 'EUR') == construct_pair('EUR', 'USD') == 'EURUSD'.
    Raises UnknownCurrency if either currency isn't in HIERARCHY.
    Raises ValueError if c1 == c2.
    """
    a, b = c1.upper().strip(), c2.upper().strip()
    if a == b:
        raise ValueError(f"Cannot construct pair from same currency twice: {a}")
    if a not in _RANK:
        raise UnknownCurrency(a)
    if b not in _RANK:
        raise UnknownCurrency(b)
    base, quote = (a, b) if _RANK[a] < _RANK[b] else (b, a)
    return base + quote


def pair_contains_any(symbol: str, currencies: tuple[str, ...] | list[str]) -> bool:
    """True iff either currency in `symbol` is in `currencies`."""
    base, quote = split_pair(symbol)
    upper = {c.upper() for c in currencies}
    return base in upper or quote in upper
