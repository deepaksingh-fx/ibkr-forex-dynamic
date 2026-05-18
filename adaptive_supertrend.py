"""
Adaptive SuperTrend with auto method selection - Python port of the Pine v6
"Adaptive SuperTrend - Auto Method Selection V3.4" indicator.

Six SuperTrend variants run in parallel, each with its own ATR-multiplier
calculation:
  0. Percentile     - multiplier scales with percentile rank of ATR
  1. Regime         - switches multiplier on low/normal/high ATR regime
  2. Z-Score        - multiplier scales with z-score of ATR
  3. Dynamic Period - base multiplier but ATR period varies with vol score
  4. Rate of Change - multiplier scales with |ROC|
  5. Hybrid         - EMA-smoothed average of the other five multipliers

Each method maintains its own per-bar SuperTrend state and a rolling
trade-simulator (mark-to-flip points). Every `eval_interval_bars`, the
system scores all six methods over a rolling `perf_lookback_days` window
and selects the best by chosen criterion (Total Points, Win Rate, or
Average Per Trade). The "active" method's SuperTrend drives signals and
the live trade state machine.

Signals require the active method's direction to flip AND the optional RSI
and MACD filters to confirm. The live trade state tracks entries, trailing
stop (ATR / Percentage / Fixed Points), and three targets (ATR / RR Ratio
/ Fixed Points). Signals take precedence over TSL exits (mirrors Pine).

Per-bar API:
    ast = AdaptiveSuperTrend(config, bars_per_day=288)
    for bar in bars:
        snap = ast.update(bar.timestamp, bar.open, bar.high, bar.low, bar.close)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from indicators import (
    EMA,
    MACD,
    PercentRank,
    ROC,
    RSI,
    SMA,
    Stdev,
    TrueRange,
    WilderATR,
)


METHOD_NAMES = ["Percentile", "Regime", "Z-Score", "Dynamic Period",
                "Rate of Change", "Hybrid"]
METHOD_INDEX = {n: i for i, n in enumerate(METHOD_NAMES)}


# --- Config -------------------------------------------------------------
@dataclass(frozen=True)
class AdaptiveSTConfig:
    # Base
    base_atr: int = 10
    base_mult: float = 3.0

    # Auto-selection
    enable_auto: bool = True
    selection_criterion: str = "Average Per Trade"   # Total Points | Win Rate | Average Per Trade
    eval_interval_bars: int = 30
    min_trades: int = 5
    perf_lookback_days: int = 60
    manual_method: str = "Percentile"

    # Percentile
    pctl_lookback: int = 100
    pctl_sens: float = 0.5

    # Regime
    reg_ma: int = 50
    low_vol_mult: float = 4.0
    norm_vol_mult: float = 2.5
    high_vol_mult: float = 1.5

    # Z-Score
    zs_lookback: int = 100
    zs_sens: float = 0.3

    # Dynamic period
    min_atr: int = 7
    max_atr: int = 20
    period_sens: float = 50.0

    # Rate of Change
    roc_lookback: int = 20
    roc_sens: float = 0.4

    # Hybrid
    hyb_smooth: int = 5

    # Filters
    enable_rsi: bool = True
    rsi_len: int = 14
    rsi_buy: float = 55.0
    rsi_sell: float = 45.0
    enable_macd: bool = True
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # TSL
    enable_tsl: bool = True
    tsl_method: str = "ATR"        # ATR | Percentage | Fixed Points
    tsl_atr_mult: float = 1.5
    tsl_pct: float = 1.0
    tsl_points: float = 50.0

    # Targets
    enable_targets: bool = True
    target_method: str = "ATR"      # ATR | RR Ratio | Fixed Points
    t1_atr_mult: float = 2.0
    t2_atr_mult: float = 4.0
    t3_atr_mult: float = 6.0
    t1_rr: float = 1.5
    t2_rr: float = 3.0
    t3_rr: float = 5.0
    t1_pts: float = 50.0
    t2_pts: float = 100.0
    t3_pts: float = 150.0

    def __post_init__(self):
        if self.selection_criterion not in (
            "Total Points", "Win Rate", "Average Per Trade"
        ):
            raise ValueError(f"Bad selection_criterion: {self.selection_criterion!r}")
        if self.manual_method not in METHOD_INDEX:
            raise ValueError(f"Bad manual_method: {self.manual_method!r}")
        if self.tsl_method not in ("ATR", "Percentage", "Fixed Points"):
            raise ValueError(f"Bad tsl_method: {self.tsl_method!r}")
        if self.target_method not in ("ATR", "RR Ratio", "Fixed Points"):
            raise ValueError(f"Bad target_method: {self.target_method!r}")
        if self.min_atr < 2 or self.max_atr <= self.min_atr:
            raise ValueError("Need 2 <= min_atr < max_atr")


# --- Trade simulator ----------------------------------------------------
@dataclass
class _Trade:
    bar_idx: int
    points: float
    bars: int


class _TradeSimulator:
    """
    Per-method trade simulator: flip-to-flip points over a rolling
    `lookback_bars` window. Mirrors the Pine indicator's trade tracker.
    """

    def __init__(self, lookback_bars: int):
        self.lookback_bars = lookback_bars
        self.trades: deque[_Trade] = deque()
        self.entry_price: Optional[float] = None
        self.entry_bar: Optional[int] = None

    def on_flip(self, prev_dir: int, bar_idx: int, close: float):
        """Direction just flipped from prev_dir -> new_dir at bar_idx, close=close."""
        if self.entry_price is not None and prev_dir != 0:
            pts = (close - self.entry_price) if prev_dir == 1 else (self.entry_price - close)
            self.trades.append(_Trade(bar_idx=bar_idx, points=pts, bars=bar_idx - (self.entry_bar or bar_idx)))
        self.entry_price = close
        self.entry_bar = bar_idx

    def ensure_entry(self, bar_idx: int, close: float):
        if self.entry_price is None:
            self.entry_price = close
            self.entry_bar = bar_idx

    def prune(self, cutoff_bar: int):
        while self.trades and self.trades[0].bar_idx < cutoff_bar:
            self.trades.popleft()

    def count(self) -> int:
        return len(self.trades)

    def total_points(self) -> float:
        return sum(t.points for t in self.trades)

    def wins(self) -> int:
        return sum(1 for t in self.trades if t.points > 0)

    def avg_bars(self) -> int:
        if not self.trades:
            return 0
        return round(sum(t.bars for t in self.trades) / len(self.trades))


# --- Per-method SuperTrend state ----------------------------------------
@dataclass
class _MethodState:
    prev_upper: Optional[float] = None
    prev_lower: Optional[float] = None
    prev_dir: int = 0   # 0 = uninitialized, 1 = up, -1 = down
    dir: int = 0
    st_value: Optional[float] = None
    last_mult: Optional[float] = None
    last_atr: Optional[float] = None


# --- Output snapshot ----------------------------------------------------
@dataclass(frozen=True)
class AdaptiveSnapshot:
    timestamp: datetime
    bar_index: int
    open: float
    high: float
    low: float
    close: float

    # Active method
    active_method_idx: int
    active_method_name: str
    active_dir: int             # 1, -1, 0 (uninitialized)
    active_st: Optional[float]
    active_atr: Optional[float]
    active_mult: Optional[float]

    # Signals
    bull_signal: bool
    bear_signal: bool

    # Filters
    rsi: Optional[float]
    macd_line: Optional[float]
    macd_signal_val: Optional[float]
    rsi_bull_pass: bool
    rsi_bear_pass: bool
    macd_bull_pass: bool
    macd_bear_pass: bool

    # Live trade state
    live_dir: int               # 1, -1, 0
    live_entry: Optional[float]
    live_entry_bar: Optional[int]
    live_tsl: Optional[float]
    live_t1: Optional[float]
    live_t2: Optional[float]
    live_t3: Optional[float]
    t1_hit: bool
    t2_hit: bool
    t3_hit: bool
    tsl_exited_this_bar: bool   # True iff TSL caused an exit this bar
    targets_hit_this_bar: list[int]  # e.g. [1, 2] if T1 and T2 newly hit this bar

    # Per-method diagnostics (lists of length 6)
    method_dirs: list[int]
    method_st_values: list[Optional[float]]
    method_trade_counts: list[int]
    method_total_points: list[float]
    method_win_rates: list[float]
    method_avg_points: list[float]
    method_scores: list[float]


# --- Adaptive SuperTrend ------------------------------------------------
class AdaptiveSuperTrend:
    def __init__(self, config: AdaptiveSTConfig, bars_per_day: int = 288):
        self.config = config
        self.bars_per_day = bars_per_day
        self.perf_lookback_bars = max(1, config.perf_lookback_days * bars_per_day)

        # Base indicators
        self.atr_base = WilderATR(config.base_atr)
        self.tr = TrueRange()             # for dynamic period method
        self._dyn_atr: Optional[float] = None   # custom variable-period RMA

        # Method-specific indicator inputs
        self.regime_sma = SMA(config.reg_ma)            # SMA of base ATR
        self.zs_sma = SMA(config.zs_lookback)
        self.zs_stdev = Stdev(config.zs_lookback)
        self.pctl_rank = PercentRank(config.pctl_lookback)
        self.dyn_stdev_close = Stdev(20)
        self.dyn_volscore_rank = PercentRank(100)
        self.roc = ROC(config.roc_lookback)
        self.hyb_ema = EMA(config.hyb_smooth)

        # Filters
        self.rsi = RSI(config.rsi_len)
        self.macd = MACD(config.macd_fast, config.macd_slow, config.macd_signal)

        # Per-method state + trade simulators
        self.states: list[_MethodState] = [_MethodState() for _ in range(6)]
        self.trades: list[_TradeSimulator] = [
            _TradeSimulator(self.perf_lookback_bars) for _ in range(6)
        ]

        # Active method
        self.active_idx = METHOD_INDEX.get(config.manual_method, 0)

        # Live trade state
        self.live_dir: int = 0
        self.live_entry: Optional[float] = None
        self.live_entry_bar: Optional[int] = None
        self.live_tsl: Optional[float] = None
        self.live_t1: Optional[float] = None
        self.live_t2: Optional[float] = None
        self.live_t3: Optional[float] = None
        self.t1_hit: bool = False
        self.t2_hit: bool = False
        self.t3_hit: bool = False
        self._last_active_atr_at_entry: Optional[float] = None  # for ATR-based targets

        # Bookkeeping
        self.bar_index: int = -1
        self.prev_close: Optional[float] = None

    # ------------------------- helpers -------------------------
    def _dyn_period(self, vol_score: Optional[float]) -> Optional[float]:
        if vol_score is None:
            return None
        cfg = self.config
        raw = cfg.min_atr + (cfg.max_atr - cfg.min_atr) * vol_score * (cfg.period_sens / 50.0)
        return max(2.0, min(500.0, raw))

    def _update_dyn_atr(self, tr_val: float, dyn_period: Optional[float]) -> Optional[float]:
        """Custom variable-period RMA: dynATR_t = (dynATR_{t-1}*(N-1) + TR_t)/N."""
        if dyn_period is None:
            return None
        if self._dyn_atr is None:
            self._dyn_atr = tr_val
        else:
            self._dyn_atr = (self._dyn_atr * (dyn_period - 1.0) + tr_val) / dyn_period
        return self._dyn_atr

    def _method_atr(self, idx: int, atr_base: Optional[float], dyn_atr: Optional[float]) -> Optional[float]:
        return dyn_atr if idx == 3 else atr_base

    # ------------------------- main update -------------------------
    def update(self, timestamp: datetime, open_: float, high: float,
               low: float, close: float) -> AdaptiveSnapshot:
        self.bar_index += 1
        cfg = self.config
        bi = self.bar_index

        # 1. Base ATR + True Range for dynamic period.
        atr_base = self.atr_base.update(high, low, close)
        tr_val = self.tr.update(high, low, close)

        # 2. Dynamic-period sub-pipeline.
        sd_close = self.dyn_stdev_close.update(close)
        vol_score_pct = self.dyn_volscore_rank.update(sd_close) if sd_close is not None else None
        vol_score = (vol_score_pct / 100.0) if vol_score_pct is not None else None
        dyn_period = self._dyn_period(vol_score)
        dyn_atr = self._update_dyn_atr(tr_val, dyn_period)

        # 3. Per-method multipliers.
        method_mults: list[Optional[float]] = [None] * 6
        method_mults[3] = cfg.base_mult     # dynamic period uses base mult

        # Percentile method (0)
        pctl_rank_val = self.pctl_rank.update(atr_base) if atr_base is not None else None
        if pctl_rank_val is not None:
            pctl_rank = pctl_rank_val / 100.0
            method_mults[0] = cfg.base_mult * (1.0 + (pctl_rank - 0.5) * cfg.pctl_sens * 2.0)

        # Regime method (1)
        atr_avg = self.regime_sma.update(atr_base) if atr_base is not None else None
        regime_str = "Normal"
        if atr_avg is not None and atr_avg > 0 and atr_base is not None:
            atr_ratio = atr_base / atr_avg
            if atr_ratio < 0.7:
                regime_str = "Low"
                method_mults[1] = cfg.low_vol_mult
            elif atr_ratio > 1.3:
                regime_str = "High"
                method_mults[1] = cfg.high_vol_mult
            else:
                method_mults[1] = cfg.norm_vol_mult

        # Z-Score method (2)
        z_mean = self.zs_sma.update(atr_base) if atr_base is not None else None
        z_std = self.zs_stdev.update(atr_base) if atr_base is not None else None
        z_score = None
        if z_mean is not None and z_std is not None and z_std > 0 and atr_base is not None:
            z_score = (atr_base - z_mean) / z_std
            method_mults[2] = cfg.base_mult * (1.0 + z_score * cfg.zs_sens)
        elif z_mean is not None:
            z_score = 0.0
            method_mults[2] = cfg.base_mult

        # ROC method (4)
        roc_val = self.roc.update(close)
        if roc_val is not None:
            method_mults[4] = cfg.base_mult * (1.0 + abs(roc_val) * cfg.roc_sens / 100.0)

        # Hybrid method (5): EMA-smoothed average of the OTHER five method mults
        # (using the base_mult slot for method 3 since that's its multiplier).
        # Per the Pine: (pctl + regime + zs + base + roc) / 5
        other_mults = [method_mults[0], method_mults[1], method_mults[2],
                       cfg.base_mult, method_mults[4]]
        if all(m is not None for m in other_mults):
            hyb_raw = sum(other_mults) / 5.0
            hyb_mult = self.hyb_ema.update(hyb_raw)
            method_mults[5] = hyb_mult

        # 4. Per-method SuperTrend update.
        hl2 = (high + low) / 2.0
        prev_close = self.prev_close if self.prev_close is not None else close

        flips: list[bool] = [False] * 6
        for i in range(6):
            mult = method_mults[i]
            atr_i = self._method_atr(i, atr_base, dyn_atr)
            if mult is None or atr_i is None:
                continue
            st = self.states[i]
            st.last_mult = mult
            st.last_atr = atr_i

            upper_basic = hl2 + mult * atr_i
            lower_basic = hl2 - mult * atr_i

            prev_up = st.prev_upper if st.prev_upper is not None else upper_basic
            prev_lo = st.prev_lower if st.prev_lower is not None else lower_basic

            new_up = upper_basic if (upper_basic < prev_up or prev_close > prev_up) else prev_up
            new_lo = lower_basic if (lower_basic > prev_lo or prev_close < prev_lo) else prev_lo

            old_dir = st.dir
            # Pine default initial dir = 1 (uptrend). We use 0 for "uninitialized"
            # and seed dir = 1 on the first valid bar.
            if old_dir == 0:
                new_dir = 1
            elif old_dir == -1 and close > new_up:
                new_dir = 1
            elif old_dir == 1 and close < new_lo:
                new_dir = -1
            else:
                new_dir = old_dir

            st.prev_dir = old_dir
            st.prev_upper = new_up
            st.prev_lower = new_lo
            st.dir = new_dir
            st.st_value = new_lo if new_dir == 1 else new_up

            # Trade tracking
            if old_dir != 0 and new_dir != old_dir:
                self.trades[i].on_flip(old_dir, bi, close)
                flips[i] = True
            else:
                self.trades[i].ensure_entry(bi, close)
            self.trades[i].prune(bi - self.perf_lookback_bars)

        # 5. Auto-selection.
        method_scores = [self._method_score(i) for i in range(6)]
        if cfg.enable_auto and (bi % max(1, cfg.eval_interval_bars) == 0):
            best_idx = 0
            best_score = -float("inf")
            for k in range(6):
                if method_scores[k] > best_score:
                    best_score = method_scores[k]
                    best_idx = k
            self.active_idx = best_idx
        elif not cfg.enable_auto:
            self.active_idx = METHOD_INDEX.get(cfg.manual_method, 0)

        active_idx = self.active_idx
        active_state = self.states[active_idx]
        active_dir = active_state.dir
        active_prev_dir = active_state.prev_dir
        active_st = active_state.st_value
        active_atr = active_state.last_atr
        active_mult = active_state.last_mult

        # 6. Filters.
        rsi_val = self.rsi.update(close)
        macd_line, macd_sig, _ = self.macd.update(close)

        rsi_bull_pass = (not cfg.enable_rsi) or (rsi_val is not None and rsi_val > cfg.rsi_buy)
        rsi_bear_pass = (not cfg.enable_rsi) or (rsi_val is not None and rsi_val < cfg.rsi_sell)
        macd_bull_pass = (not cfg.enable_macd) or (macd_line is not None and macd_sig is not None and macd_line > macd_sig)
        macd_bear_pass = (not cfg.enable_macd) or (macd_line is not None and macd_sig is not None and macd_line < macd_sig)

        # 7. Signals (active method flip + filters).
        bull_signal = (active_prev_dir == -1 and active_dir == 1
                       and rsi_bull_pass and macd_bull_pass)
        bear_signal = (active_prev_dir == 1 and active_dir == -1
                       and rsi_bear_pass and macd_bear_pass)

        # 8. Live trade state machine + TSL/targets.
        tsl_exited, new_targets_hit = self._update_live_state(
            high, low, close, active_atr, bull_signal, bear_signal,
        )

        self.prev_close = close

        return AdaptiveSnapshot(
            timestamp=timestamp,
            bar_index=bi,
            open=open_,
            high=high,
            low=low,
            close=close,
            active_method_idx=active_idx,
            active_method_name=METHOD_NAMES[active_idx],
            active_dir=active_dir,
            active_st=active_st,
            active_atr=active_atr,
            active_mult=active_mult,
            bull_signal=bull_signal,
            bear_signal=bear_signal,
            rsi=rsi_val,
            macd_line=macd_line,
            macd_signal_val=macd_sig,
            rsi_bull_pass=rsi_bull_pass,
            rsi_bear_pass=rsi_bear_pass,
            macd_bull_pass=macd_bull_pass,
            macd_bear_pass=macd_bear_pass,
            live_dir=self.live_dir,
            live_entry=self.live_entry,
            live_entry_bar=self.live_entry_bar,
            live_tsl=self.live_tsl,
            live_t1=self.live_t1,
            live_t2=self.live_t2,
            live_t3=self.live_t3,
            t1_hit=self.t1_hit,
            t2_hit=self.t2_hit,
            t3_hit=self.t3_hit,
            tsl_exited_this_bar=tsl_exited,
            targets_hit_this_bar=new_targets_hit,
            method_dirs=[s.dir for s in self.states],
            method_st_values=[s.st_value for s in self.states],
            method_trade_counts=[t.count() for t in self.trades],
            method_total_points=[t.total_points() for t in self.trades],
            method_win_rates=[
                (100.0 * t.wins() / t.count()) if t.count() > 0 else 0.0
                for t in self.trades
            ],
            method_avg_points=[
                (t.total_points() / t.count()) if t.count() > 0 else 0.0
                for t in self.trades
            ],
            method_scores=method_scores,
        )

    # ------------------------- auto-selection scoring -------------------------
    def _method_score(self, idx: int) -> float:
        cnt = self.trades[idx].count()
        if cnt < self.config.min_trades:
            return -1.0e10
        crit = self.config.selection_criterion
        if crit == "Total Points":
            return self.trades[idx].total_points()
        if crit == "Win Rate":
            return 100.0 * self.trades[idx].wins() / cnt
        # Average Per Trade (default)
        return self.trades[idx].total_points() / cnt

    # ------------------------- live trade state -------------------------
    def _sl_dist_now(self, close: float, atr: Optional[float]) -> Optional[float]:
        cfg = self.config
        if cfg.tsl_method == "ATR":
            return cfg.tsl_atr_mult * atr if atr is not None else None
        if cfg.tsl_method == "Percentage":
            return close * cfg.tsl_pct / 100.0
        return cfg.tsl_points

    def _compute_target(self, mult_atr: float, rr: float, pts: float,
                        direction: int, entry: float, atr_at_entry: Optional[float],
                        sl_dist: Optional[float]) -> Optional[float]:
        cfg = self.config
        if cfg.target_method == "ATR":
            if atr_at_entry is None:
                return None
            offset = mult_atr * atr_at_entry
            return entry + offset if direction == 1 else entry - offset
        if cfg.target_method == "RR Ratio":
            if sl_dist is None:
                return None
            offset = rr * sl_dist
            return entry + offset if direction == 1 else entry - offset
        # Fixed Points
        return entry + pts if direction == 1 else entry - pts

    def _update_live_state(
        self, high: float, low: float, close: float,
        active_atr: Optional[float], bull_signal: bool, bear_signal: bool,
    ) -> tuple[bool, list[int]]:
        cfg = self.config
        sl_dist_now = self._sl_dist_now(close, active_atr)

        # TSL hit detection (close-of-bar mode).
        tsl_hit_long = (
            self.live_dir == 1 and cfg.enable_tsl
            and self.live_tsl is not None and close < self.live_tsl
        )
        tsl_hit_short = (
            self.live_dir == -1 and cfg.enable_tsl
            and self.live_tsl is not None and close > self.live_tsl
        )

        old_dir = self.live_dir
        new_dir = old_dir

        # State machine - independent ifs, in Pine order.
        # (Signal-induced flips first; TSL exits later only if no opposite signal.)
        if old_dir == 0 and bull_signal:
            new_dir = 1
        if old_dir == 0 and bear_signal:
            new_dir = -1
        if old_dir == 1 and bear_signal:
            new_dir = -1
        if old_dir == -1 and bull_signal:
            new_dir = 1
        if old_dir == 1 and tsl_hit_long and not bear_signal:
            new_dir = 0
        if old_dir == -1 and tsl_hit_short and not bull_signal:
            new_dir = 0

        state_changed = new_dir != old_dir
        tsl_exited_this_bar = state_changed and new_dir == 0

        if state_changed:
            # Reset live state.
            self.live_dir = new_dir
            if new_dir == 0:
                self._reset_live_state()
            else:
                self.live_entry = close
                self.live_entry_bar = self.bar_index
                self.live_tsl = (close - sl_dist_now) if (new_dir == 1 and sl_dist_now is not None) else \
                                (close + sl_dist_now) if (new_dir == -1 and sl_dist_now is not None) else None
                self._last_active_atr_at_entry = active_atr
                self.live_t1 = self._compute_target(cfg.t1_atr_mult, cfg.t1_rr, cfg.t1_pts, new_dir, close, active_atr, sl_dist_now) if cfg.enable_targets else None
                self.live_t2 = self._compute_target(cfg.t2_atr_mult, cfg.t2_rr, cfg.t2_pts, new_dir, close, active_atr, sl_dist_now) if cfg.enable_targets else None
                self.live_t3 = self._compute_target(cfg.t3_atr_mult, cfg.t3_rr, cfg.t3_pts, new_dir, close, active_atr, sl_dist_now) if cfg.enable_targets else None
                self.t1_hit = False
                self.t2_hit = False
                self.t3_hit = False

        new_targets_hit: list[int] = []
        if not state_changed and self.live_dir != 0:
            # Trail TSL (only upward for long, only downward for short).
            if self.live_dir == 1 and sl_dist_now is not None:
                cand = close - sl_dist_now
                if self.live_tsl is None or cand > self.live_tsl:
                    self.live_tsl = cand
                # Target hit checks use intra-bar wicks (high for long).
                if cfg.enable_targets:
                    if self.live_t1 is not None and not self.t1_hit and high >= self.live_t1:
                        self.t1_hit = True
                        new_targets_hit.append(1)
                    if self.live_t2 is not None and not self.t2_hit and high >= self.live_t2:
                        self.t2_hit = True
                        new_targets_hit.append(2)
                    if self.live_t3 is not None and not self.t3_hit and high >= self.live_t3:
                        self.t3_hit = True
                        new_targets_hit.append(3)
            elif self.live_dir == -1 and sl_dist_now is not None:
                cand = close + sl_dist_now
                if self.live_tsl is None or cand < self.live_tsl:
                    self.live_tsl = cand
                if cfg.enable_targets:
                    if self.live_t1 is not None and not self.t1_hit and low <= self.live_t1:
                        self.t1_hit = True
                        new_targets_hit.append(1)
                    if self.live_t2 is not None and not self.t2_hit and low <= self.live_t2:
                        self.t2_hit = True
                        new_targets_hit.append(2)
                    if self.live_t3 is not None and not self.t3_hit and low <= self.live_t3:
                        self.t3_hit = True
                        new_targets_hit.append(3)

        return tsl_exited_this_bar, new_targets_hit

    def _reset_live_state(self):
        self.live_entry = None
        self.live_entry_bar = None
        self.live_tsl = None
        self.live_t1 = None
        self.live_t2 = None
        self.live_t3 = None
        self.t1_hit = False
        self.t2_hit = False
        self.t3_hit = False
        self._last_active_atr_at_entry = None
