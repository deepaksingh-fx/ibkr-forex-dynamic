"""
Coil-index pair selection for the session/DMI/CPR strategy. Pure, no I/O.

Per trading session, among that session's candidate pairs, select ONE pair to
trade for the session as follows:

  1. ADX filter (threshold 30): keep only pairs whose ADX -- as of the first
     closed 5-min candle of the session -- is BELOW `adx_filter_max` (30). A
     pair already trending hard is discarded for the session/day.

  2. For each survivor compute a coil index:

         width_pct    = (TC - BC) / TC * 100          # tighter CPR -> smaller
         low_adx_frac = (# last-24h 5m bars with ADX < adx_coil_max) / N_valid
         coil_index   = (1 - low_adx_frac) * width_pct

     adx_coil_max defaults to 20. Both a narrow CPR (small width_pct) and a lot
     of low-ADX / ranging bars (large low_adx_frac -> small 1-low_adx_frac)
     push the coil index DOWN -- i.e. the most "coiled" pair scores lowest.

  3. Pick the survivor with the LOWEST coil index. Ties resolve to the first
     appearance in the candidate list.

DMI is Wilder 14/14 (DI Length 14, ADX Smoothing 14) on the 5-min chart, via
the existing `quality_supertrend.DMI`. CPR is the standard daily CPR.

The pure selector (`coil_select`) takes pre-computed per-pair metrics. A helper
(`compute_pair_coil`) builds those metrics from chronological 5-min bars + a
CPR, so callers stay free of indicator bookkeeping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from cpr import CPR
from quality_supertrend import DMI
from selection import SelectionError

# --- defaults (single source of truth; flip here, not at call sites) ---
ADX_FILTER_MAX = 30.0   # step 1: keep pairs with ADX strictly below this
ADX_COIL_MAX = 20.0     # step 2: count 24h bars with ADX strictly below this
COIL_WINDOW_BARS = 288  # last 24h of 5-min bars (288 = 24h * 12)
DI_LEN = 14
ADX_LEN = 14


@dataclass(frozen=True, slots=True)
class CoilParams:
    """Tunable thresholds/periods for the coil selection. All overridable."""
    adx_filter_max: float = ADX_FILTER_MAX
    adx_coil_max: float = ADX_COIL_MAX
    coil_window_bars: int = COIL_WINDOW_BARS
    di_len: int = DI_LEN
    adx_len: int = ADX_LEN
    # No NEW entries (incl. reversal re-entries) within this many minutes of the
    # session end. Exits/stops still fire. The final 5m candle force-flats.
    no_entry_last_minutes: int = 60

    def __post_init__(self) -> None:
        if self.coil_window_bars < 1:
            raise ValueError("coil_window_bars must be >= 1")
        if self.di_len < 1 or self.adx_len < 1:
            raise ValueError("di_len / adx_len must be >= 1")
        if self.no_entry_last_minutes < 0:
            raise ValueError("no_entry_last_minutes must be >= 0")


@dataclass(frozen=True, slots=True)
class PairCoil:
    """Per-pair inputs to the coil selection, plus derived scores."""
    symbol: str
    adx_now: float          # ADX at the first session candle (step-1 filter)
    plus_di: float          # DI+ at that same bar (diagnostics)
    minus_di: float         # DI- at that same bar (diagnostics)
    cpr: CPR
    low_adx_bars: int       # 24h bars with ADX < adx_coil_max
    total_adx_bars: int     # valid (non-None ADX) bars in the 24h window
    adx_coil_max: float = ADX_COIL_MAX

    @property
    def width_pct(self) -> float:
        return self.cpr.width_pct_tc

    @property
    def low_adx_frac(self) -> float:
        if self.total_adx_bars <= 0:
            raise SelectionError(f"{self.symbol}: no valid ADX bars in window")
        return self.low_adx_bars / self.total_adx_bars

    @property
    def coil_index(self) -> float:
        return (1.0 - self.low_adx_frac) * self.width_pct


def coil_select(
    metrics: Sequence[PairCoil],
    *,
    adx_filter_max: float = ADX_FILTER_MAX,
) -> PairCoil:
    """
    Return the PairCoil with the lowest coil index among pairs that pass the
    ADX filter (adx_now < adx_filter_max). Ties -> first appearance in `metrics`.

    Raises:
      SelectionError: empty input, or no pair survives the ADX filter.
    """
    if not metrics:
        raise SelectionError("metrics is empty")

    survivors = [m for m in metrics if m.adx_now < adx_filter_max]
    if not survivors:
        raise SelectionError(
            f"no pair has ADX < {adx_filter_max} "
            f"(adx_now: {{{', '.join(f'{m.symbol}={m.adx_now:.1f}' for m in metrics)}}})"
        )

    pos = {id(m): i for i, m in enumerate(metrics)}
    return min(survivors, key=lambda m: (m.coil_index, pos[id(m)]))


def compute_pair_coil(
    symbol: str,
    bars: Sequence,
    cpr: CPR,
    *,
    di_len: int = DI_LEN,
    adx_len: int = ADX_LEN,
    adx_coil_max: float = ADX_COIL_MAX,
    coil_window_bars: int = COIL_WINDOW_BARS,
) -> PairCoil:
    """
    Build a PairCoil from chronological 5-min `bars` (warm-up history up to and
    including the first closed session candle) and the pair's daily `cpr`.

    `bars` items need `.high`, `.low`, `.close` (IBKR BarData satisfies this).
    The last bar is the first session candle: its ADX/DI feed the step-1 filter;
    the trailing `coil_window_bars` bars' ADX feed the step-2 low-ADX count.

    Raises:
      SelectionError: too few bars / ADX never seeds (warm-up insufficient).
    """
    if not bars:
        raise SelectionError(f"{symbol}: no bars supplied")

    dmi = DMI(di_len=di_len, adx_len=adx_len)
    adx_series: list[Optional[float]] = []
    last_plus = last_minus = last_adx = None
    for bar in bars:
        plus_di, minus_di, adx = dmi.update(bar.high, bar.low, bar.close)
        adx_series.append(adx)
        if adx is not None:
            last_plus, last_minus, last_adx = plus_di, minus_di, adx

    if last_adx is None:
        raise SelectionError(
            f"{symbol}: ADX never seeded over {len(bars)} bars "
            f"(need warm-up > di_len + adx_len = {di_len + adx_len})"
        )

    window = [a for a in adx_series[-coil_window_bars:] if a is not None]
    if not window:
        raise SelectionError(f"{symbol}: no valid ADX in last {coil_window_bars} bars")
    low_adx_bars = sum(1 for a in window if a < adx_coil_max)

    return PairCoil(
        symbol=symbol,
        adx_now=last_adx,
        plus_di=last_plus,
        minus_di=last_minus,
        cpr=cpr,
        low_adx_bars=low_adx_bars,
        total_adx_bars=len(window),
        adx_coil_max=adx_coil_max,
    )
