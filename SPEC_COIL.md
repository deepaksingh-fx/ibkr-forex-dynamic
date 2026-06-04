# SPEC_COIL — Session / Coil-Index Strategy

Locked 2026-06-04. Built **alongside** the existing strategies (does not modify
`SPEC.md`, the old CPR strategy, or the SuperTrend pivot strategy). This strategy
**drops SuperTrend entirely** and uses the DMI (DI+/DI−) for entry/exit.

All session times are **IST (Asia/Kolkata)**. Indicator math is on **5-minute**
bars. Default price feed is **IDEALPRO MIDPOINT** (IBKR serves no historical bars
for FX CFDs). All risk amounts are **USD, absolute (not %)**.

---

## 1. Sessions (params — `coil_config.json`)

Four non-overlapping IST sessions, each with its own candidate pairs:

| Session | IST window | Default pairs |
|---|---|---|
| Asian | 04:30–11:30 | AUDJPY, NZDJPY, AUDUSD, NZDUSD, USDJPY |
| London | 12:30–17:30 | GBPJPY, EURJPY, CHFJPY, USDCHF |
| LDN-NY Overlap | 17:30–21:30 | EURUSD, GBPUSD, GBPJPY, USDCHF |
| New York | 21:30–02:30 (+1d) | USDCAD, USDJPY, CADJPY |

Windows, pairs, and all thresholds below are configurable.

## 2. Asset selection (per session) — **BUILT & TESTED**

At session start, wait for the **first 5-minute candle to close**, then:

1. **ADX filter** (DMI 14/14): keep only pairs with `ADX < adx_filter_max` (30).
   Trending pairs are discarded for that session/day. If **all** are filtered →
   **no trade** that session (acceptable).
2. **CPR** (standard daily, prior trading FX day H/L/C). Width:
   `width_pct = (TC − BC) / TC × 100`.
3. **Low-ADX count** over the last `coil_window_bars` (288 = 24h) 5-minute bars:
   `low_adx_frac = (# bars with ADX < adx_coil_max[20]) / N_valid`.
4. **Coil index** = `(1 − low_adx_frac) × width_pct`.
5. Select the pair with the **lowest** coil index (most coiled). Ties → first in
   the candidate list.

Implemented in `coil_selection.py` (+ `sessions.py`, `coil_config.py`).
DMI = exact Pine v6 `ta.dmi(14,14)` via `quality_supertrend.DMI`.

## 3. Levels & bias (after selection)

Trade off the **9 daily pivots** from the prior FX-day H/L/C (`pivots.py`):
`P, R1–R4, S1–S4`.

- **Dynamic base level**: seeded on the **session's first candle** (the same
  candle used for selection; NOT eligible for entry). Seed = touched level
  closest to the close; if none touched, the closest of all 9. On every later
  candle that touches a level, the base **hops** to the touched level closest to
  the close. Touch nothing → base unchanged.
- **Bias**: `close > base` → **LONG-only**; `close < base` → **SHORT-only**;
  `close == base` → none. Bias is recomputed every bar and may change mid-trade.

## 4. Entry (only when flat; from candle #2; not in the final-hour lockout)

Both gates must agree:

- **LONG**  if bias LONG  **and** `DI+ > DI−`
- **SHORT** if bias SHORT **and** `DI− > DI+`

Otherwise no trade. Enter at the bar close.

**Final-hour lockout** (`no_entry_last_minutes`, default 60): no NEW entries —
**including reversal re-entries** — within the last hour of the session window.
Exits and stops still fire normally during this window; we just don't open
anything new. The **final 5-minute candle** force-flats (auto-close, §5).

## 5. Exit — stop-and-reverse

A bias change alone **never** exits. While in a position, exit only when the
**full opposite entry signal** fires (opposite bias **AND** opposite DMI):

- Long → exit when bias SHORT **and** `DI− > DI+`.
- Short → exit when bias LONG **and** `DI+ > DI−`.

On a signal exit, **immediately open the opposite position on the same candle**
(stop-and-reverse). One position at a time; multiple reversals per session
allowed.

Additional forced exits:
- **Protective stop** hit (see §6).
- **Session end auto-close**: the **final 5-minute candle** of the session
  force-flats any open position; no entry on that bar.

## 6. Risk / execution layer (USD, absolute)

Params: `risk_per_trade`, `breakeven_trigger`, `daily_loss_limit` (all USD),
plus position size (`units`, fixed).

- **Position size is fixed**; the **stop-market** is placed at the price where
  loss = `risk_per_trade`:
  `stop_distance = risk_per_trade / (units × usd_per_price_unit)`.
  Stop sits at `entry − stop_distance` (long) / `entry + stop_distance` (short).
- **Stop order type: stop-market** (protection over precision).
- **No take-profit. Ever.**
- **Breakeven watcher (live ticks)**: track peak unrealized PnL on the open
  trade; the instant it **touches** `breakeven_trigger` (even once), modify the
  stop to the **exact entry price** (one-way latch). Requires a live price
  subscription on the open trade.
  - **Feed: IDEALPRO SPOT, not the CFD.** Verified live (2026-06-04): FX spot
    `reqMktData` / `reqTickByTickData(BidAsk)` stream fine, but the CFD contract
    returns **no real-time data** (bid/ask = NaN) — same as its missing
    historical bars. So the watcher subscribes to the pair's spot quote and uses
    it as the PnL proxy for the CFD position (tiny basis, immaterial at the
    breakeven trigger). Tick-by-tick is confirmed feasible on spot.
- **Daily loss limit**: track realized USD PnL across the FX day (resets 17:00
  NY). On `≤ −daily_loss_limit` → **flatten any open position AND halt** for the
  rest of that FX day.
- **Order hygiene**: a signal-reversal exit **cancels the resting stop** before
  reversing; a stop-out is **detected** (fill → marked flat); nothing is left
  resting in the book.

## 6b. Monitoring vs execution split (LOCKED 2026-06-04)

- **Monitoring / decisions → IDEALPRO SPOT.** 5m bars (DMI, CPR, pivots, bias),
  signal evaluation, and the live tick-by-tick breakeven watcher all read the
  pair's **spot** price (CFD serves no market data).
- **Execution / orders → CFD** (SMART, `cfd_account`). Entry market order, the
  protective stop-market, the breakeven stop-modify, signal exits and reversals
  are all placed on the CFD contract.
- **Basis nuance:** the stop *price* is anchored to the **CFD entry fill**
  (`cfd_entry − stop_distance`), while the breakeven *trigger* is detected from
  spot-derived PnL. CFD tracks spot tightly, so the small basis is immaterial at
  the trigger; on the breakeven modify we set the CFD stop to the CFD entry fill
  price (true breakeven on the instrument we hold).

## 7. State / safety

- Always know flat vs in-trade; reconcile against IBKR so an order is never
  orphaned and we never double-trade (extends the existing state store /
  reconciliation).
- Live-order placement is **gated** exactly like the rest of the system
  (`--live` + `--i-really-mean-it` + `read_only=False`). Shadow mode is default.

## 8. Build status (2026-06-04)

- **Done:** sessions, selection, config, evaluator, 25 tests.
- **This doc + a shadow backtest** (`coil_backtest.py`) follow next.
- **Not built:** live runtime loop; the order/risk layer (real stop placement,
  live-tick breakeven, reversal cancel, daily-limit flatten). No live-order code
  written.

## 9. Backtest approximations (`coil_backtest.py`)

The shadow backtest runs on 5-minute bars only (no tick stream), so:
- Protective-stop fills are checked against each bar's **adverse extreme**
  (low for long / high for short) at the stop price.
- Breakeven arming uses each bar's **favorable extreme**.
- Intrabar ordering when a bar spans both: the **stop is checked first**
  (conservative).
- USD conversion uses the relevant USD-base rate (USDJPY/USDCHF/USDCAD) sampled
  at the session, not tick-accurate.
These are simulation conveniences; the live layer uses real ticks/orders.
