# forex_cpr_ibkr — Strategy Specification

Living document. Updated as the user defines more rules. **No code is written until this spec is complete and confirmed.**

---

## 1. Project goals

- Single-strategy, single-broker (Interactive Brokers) standalone Python project.
- Run a forex CPR strategy on **5-minute candles**.
- Connect to IBKR via `ib_async` (TWS / IB Gateway socket).
- IBKR account is a **Financial Advisor (FA) live account** — multiple sub-accounts under one login. **No paper account is available.**
- During development: **DRY-RUN ONLY**. Real orders must require an explicit, deliberate flag flip. See §14 (Safety).

### 1.1 Strategy inputs (configurable at startup)

| Input | Type | Default | Used in |
|---|---|---|---|
| `symbols_list` | list[str] | the 15 pairs in §2 | §9 primary selection |
| `allowed_currencies` | list[str] | (must provide, ≥1) | §9 cross-pair expansion |
| `entry_trigger_range_pct` | float (percent) | (e.g. 0.05) | §10 entry trigger |
| `per_trade_loss_pct` | float (percent) | (user-set) | §11.2 per-trade SL |
| `per_day_loss_pct` | float (percent) | (user-set) | §11.2 daily circuit breaker |

Lot size is hardcoded at 0.25 (see §12), not configurable.

---

## 2. Symbols (15 forex pairs)

EURUSD, USDJPY, GBPUSD, USDCHF, USDCAD, EURJPY, EURGBP, EURCHF, EURCAD, GBPJPY, GBPCHF, GBPCAD, CHFJPY, CADCHF, CADJPY

All routed via IBKR `IDEALPRO`.

---

## 3. Timeframe

- Strategy operates on **5-minute candles**.
- All time math done in `America/New_York` zoneinfo (DST handled automatically — "17:00 NY" stays 17:00 year-round; UTC offset shifts itself between -5 winter and -4 summer).

---

## 4. FX day boundaries (NY 17:00 anchor)

**Each FX day = 5-min bars whose open time falls in `[17:00 NY of prior calendar day, 17:00 NY of that calendar day)`.**

| FX day | Bar opens in |
|---|---|
| Monday | Sun 17:00 → Mon 17:00 (last bar opens 16:55 Mon) |
| Tuesday | Mon 17:00 → Tue 17:00 |
| Wednesday | Tue 17:00 → Wed 17:00 |
| Thursday | Wed 17:00 → Thu 17:00 |
| Friday | Thu 17:00 → Fri 17:00 |

**Weekend:** Market closed Fri 17:00 → Sun 17:00. No Saturday FX day, no Sunday FX day. The block from Sun 17:00 onward is the start of Monday's FX day.

**Trading window for the bot:** Sun 17:00 NY → Fri 17:00 NY. (Confirmed by user.)

---

## 5. CPR formula (same on every day; what changes is the source window)

From the source window's 5-min candles:

```
H = max(high)  across the source window
L = min(low)   across the source window
C = close of the last 5-min bar of the source window
    (the bar that opens 16:55 of the window's final day, closes 17:00)

Pivot  = (H + L + C) / 3
BC     = (H + L) / 2
TC_raw = 2 · Pivot − BC

# Force ordering — TC must always be the higher line:
TC = max(TC_raw, BC)
BC = min(TC_raw, BC)
```

Only **BC** and **TC** are used by the strategy. Pivot, S/R levels are not used.

---

## 6. Two CPRs run simultaneously

There are **two** CPR objects per asset, with different purposes:

### 6a. Weekly CPR — for asset selection

- **Source window:** Last week. Sun 17:00 NY (8 days before today) → Fri 17:00 NY (last Friday). Covers all 5 last-week FX days.
- **Computed once per trading week**, at the Sun 17:00 NY rollover.
- **Static for the entire trading week** (Sun 17:00 → Fri 17:00).
- **Used as input to asset selection** (see §9). Specifically the **CPR width %**.
- **Computed for ALL 15 assets**, regardless of which assets are eventually traded.

### 6b. Daily CPR — for trading levels (entry rules)

- **Source window:** the prior FX day.
- **Computed at each 17:00 NY rollover**, fresh for the new FX day.

| Trading day | Daily CPR source window |
|---|---|
| Monday | *(Same as weekly CPR — see §6a. No separate daily CPR exists on Monday.)* |
| Tuesday | Mon FX day = Sun 17:00 → Mon 17:00 |
| Wednesday | Tue FX day = Mon 17:00 → Tue 17:00 |
| Thursday | Wed FX day = Tue 17:00 → Wed 17:00 |
| Friday | Thu FX day = Wed 17:00 → Thu 17:00 |

**Special case Monday:** weekly CPR == daily CPR (same source window). Only one CPR object on Monday.

Levels are **static** within their applicable window — *unless* a future "dynamic CPR" rule overrides this. See §7.

### Worked example (week of Mon Nov 17 – Fri Nov 21, 2025)

| Day | CPR levels computed from |
|---|---|
| Mon Nov 17 | Sun **Nov 9** 17:00 → Fri **Nov 14** 17:00 (full last week) |
| Tue Nov 18 | Sun Nov 16 17:00 → Mon Nov 17 17:00 |
| Wed Nov 19 | Mon Nov 17 17:00 → Tue Nov 18 17:00 |
| Thu Nov 20 | Tue Nov 18 17:00 → Wed Nov 19 17:00 |
| Fri Nov 21 | Wed Nov 19 17:00 → Thu Nov 20 17:00 |

---

## 7. Dynamic CPR — defined

"Dynamic CPR" is the **weekly→daily shifting behavior already described in §6**. There is no separate dynamic-CPR concept beyond that.

Specifically:
- **Mon:** trading levels = weekly CPR (last week's source window).
- **Tue–Fri:** trading levels = daily CPR (prior FX day's source window).
- The CPR being used for entries thus "shifts" from weekly (Mon) to daily (Tue→Fri), recomputing at each 17:00 NY rollover. That shift IS the dynamic behavior.

Asset-selection CPR is always the weekly one (recomputed once per week at Sun 17:00 NY).

---

## 8. System startup behavior

The strategy is given a **list of symbols** as input (configurable; default = the 15 pairs in §2).

### 8a. Trading-zone gate

On startup (and continuously while running):

1. Read system clock, convert to `America/New_York`.
2. **Check trading zone:** are we inside `[Sun 17:00 NY, Fri 17:00 NY)`?
   - **If NO**: log "outside trading window," sleep 60 seconds, recheck. Loop until inside the trading zone. Do nothing else during this wait.
   - **If YES**: proceed to §8b.
3. While the bot is running, the gate is also re-checked at each FX-day rollover. Once we cross Fri 17:00 NY, we go back to step 2 and poll every minute until Sun 17:00 NY.

### 8b. First time entering the trading zone

Once we confirm we're in the trading zone:

1. NY-anchored clock → determine current FX day.
2. **For every symbol in the input list, compute the weekly CPR (§6a):**
   - Fetch IBKR 5-min bars covering Sun 17:00 (8 days ago) → Fri 17:00 (last Fri).
   - Aggregate **OHLC** of that window.
   - Compute Pivot, BC, TC; force TC ≥ BC.
   - Compute CPR width % (§9).
3. **Run asset selection (§9)** → pick the single asset with the **lowest CPR width %**. That's the asset we'll trade this week.
4. **If today is Tue–Fri:** for the selected asset, also fetch prior FX day's 5-min bars and compute daily CPR (§6b).
   **If today is Monday:** weekly CPR == daily CPR (same source window) — no separate daily CPR object.
5. Begin processing live 5-min candles for the selected asset.

### 8c. Ongoing

- At each 17:00 NY rollover Mon→Thu (i.e., end of Mon FX day, end of Tue FX day, etc.): recompute daily CPR for the new FX day on the *currently selected* asset.
- At each Sun 17:00 NY rollover (start of new trading week): recompute weekly CPR for all symbols in the input list + re-run asset selection. The selected asset may change week-to-week.
- At Fri 17:00 NY: market closes. Drop back into the trading-zone gate (§8a, step 2) and poll every minute until Sun 17:00 NY.

**Implementation note:** IBKR forex daily bars typically anchor to 17:00 NY, so fetching daily bars *might* be enough instead of aggregating 5-min ourselves. To be verified during build. Default plan: fetch 5-min and aggregate — known-correct, slightly more data.

**Verified during build (2026-05-04):** end-to-end smoke against live IBKR confirmed the prior-week window aligns to the minute on the FRIDAY end (last bar opens 16:55 NY, as expected). On the SUNDAY end, IBKR returns no bars from 17:00 to ~17:15 NY — this is the actual FX re-open lag (Sydney/Tokyo trickling in), not a bug. All 15 pairs return ~1425 bars per week (vs theoretical 1440); the missing 15 bars are concentrated in the Sunday open lull. H/L/C are unaffected for any practical purpose.

---

## 9. Asset selection

Selection runs once per trading week (at Sun 17:00 NY rollover, and at startup if mid-week) and produces **the trading shortlist for the week** — either 1 pair (no expansion) or 2 pairs (cross-pair expansion).

### 9.1 Strategy inputs

The strategy accepts two configurable inputs:

| Input | Type | Default | Description |
|---|---|---|---|
| `symbols_list` | list[str] | the 15 pairs in §2 | All pairs to consider for primary selection. |
| `allowed_currencies` | list[str] | (must be provided, ≥1) | Currency codes that gate whether expansion happens. e.g. `["USD","JPY"]`. |

`allowed_currencies` is normalized to upper-case at load time.

### 9.2 CPR width % (the selection metric)

```
CPR_width     = TC − BC                        (always ≥ 0, since TC ≥ BC is enforced)
Pivot         = (H + L + C) / 3                (of the weekly source window)
CPR_width_pct = (CPR_width / Pivot) × 100
```

### 9.3 Selection algorithm

```
1. Compute weekly CPR + width % for every pair in symbols_list.
2. PRIMARY = pair with lowest width % (tie-break: first in symbols_list).
3. Decompose PRIMARY into its two currencies (base, quote).
4. If at least one of {base, quote} is in allowed_currencies:
       → trade ONLY PRIMARY. Shortlist = [PRIMARY].
   else:
       expansion = []
       for currency C in {base, quote}:
           candidates_C = [pair formed by combining C with each X in allowed_currencies]
                          (filtered: only candidates that exist in symbols_list — see §9.5 Edge 1)
           winner_C = candidate with lowest width %
                      (same tie-break: first in symbols_list)
           if winner_C exists:
               expansion.append(winner_C)
       Shortlist = expansion.
       (If both sides produced no candidates → see §9.5 Edge 2.)
```

So the shortlist is **either 1 pair** (no expansion) or **at most 2 pairs** (one per side of PRIMARY).

### 9.4 Pair-construction convention

When combining two currencies into a pair name, follow the standard forex hierarchy:

```
EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY
```

The currency that ranks higher becomes the base. So:
- EUR + USD → EURUSD
- USD + JPY → USDJPY
- USD + CHF → USDCHF
- USD + CAD → USDCAD
- GBP + JPY → GBPJPY
- CAD + JPY → CADJPY
- (etc.)

This matches IBKR / IDEALPRO conventions and matches the orderings of the 15 default pairs.

### 9.5 Edge cases

**Edge 1 — candidate not in symbols_list:**
*Locked behavior:* during expansion, candidates that aren't present in `symbols_list` are silently dropped. The user must include all expansion targets in `symbols_list` if they want them tradeable.

**Edge 2 — side produces zero valid candidates after Edge-1 filtering:**
*Locked behavior:* raise an error at startup. Forces the user to fix their inputs.

**Edge 3 — `allowed_currencies` is empty:** rejected at startup (must be ≥1).

**Edge 4 — same pair appears on both sides of expansion:** structurally impossible. The two sides are derived from `base` and `quote` of PRIMARY, which are different currencies, so the two `winner_C`s cannot be the same pair.

### 9.6 Worked examples

**Example 1 (cross-pair expansion):**
allowed=`["USD","JPY"]`, narrowest=EURGBP
→ EUR side: narrowest of {EURUSD, EURJPY}; GBP side: narrowest of {GBPUSD, GBPJPY}
→ shortlist = 2 pairs

**Example 2 (no expansion via base):**
allowed=`["EUR"]`, narrowest=EURGBP → EUR matches → shortlist = `[EURGBP]`

**Example 3 (no expansion via quote):**
allowed=`["JPY"]`, narrowest=USDJPY → JPY matches → shortlist = `[USDJPY]`

**Example 4 (single allowed currency, expansion):**
allowed=`["USD"]`, narrowest=EURGBP → EUR side: EURUSD; GBP side: GBPUSD → shortlist = `[EURUSD, GBPUSD]`

**Example 5 (three allowed, expansion):**
allowed=`["USD","JPY","CHF"]`, narrowest=EURGBP
→ EUR side: narrowest of {EURUSD, EURJPY, EURCHF}; GBP side: narrowest of {GBPUSD, GBPJPY, GBPCHF}
→ shortlist = 2 pairs

---

## 10. Entry rules

### 10.1 Strategy input — entry trigger range

The strategy accepts a third configurable input:

| Input | Type | Example |
|---|---|---|
| `entry_trigger_range_pct` | float (percent) | `0.05` |

### 10.2 Trigger formula (per shortlisted pair, on every 5-min candle close)

Let `pct = entry_trigger_range_pct / 100`.

```
upper_band = TC + TC × pct      # band ABOVE TC
lower_band = BC − BC × pct      # band BELOW BC

LONG  triggers when:  TC <  close ≤ upper_band
SHORT triggers when:  lower_band ≤ close <  BC
```

The TC/BC used here are the **trading-level CPR** (per §6b — daily CPR Tue–Fri, weekly CPR on Mon).

### 10.3 Strict-or-inclusive convention (LOCKED)

- Strictly above TC for long (close = TC alone does **not** trigger).
- Strictly below BC for short.
- Inclusive at the *outer* edge of the band (close exactly at upper_band fires; close exactly at lower_band fires).

### 10.4 Worked examples

(See conversation log; multi-pair, multi-pct examples documented. Summary below.)

EURUSD with TC=1.1800, BC=1.1750, pct=0.05:
- LONG band: (1.18000, 1.18059]
- SHORT band: [1.17441, 1.17500)

USDJPY with TC=156.50, BC=155.80, pct=0.05:
- LONG band: (156.500, 156.578]
- SHORT band: [155.722, 155.800)

### 10.5 Position-state rules (LOCKED)

| Situation | Behavior |
|---|---|
| Trigger fires, no current position | Open new position. |
| **LONG trigger fires while already LONG** (same pair) | **Ignore.** Only one trade at a time per pair. |
| **SHORT trigger fires while already SHORT** (same pair) | **Ignore.** |
| **LONG trigger fires while currently SHORT** (same pair) | **Reverse** — close existing short, open new long. |
| **SHORT trigger fires while currently LONG** (same pair) | **Reverse** — close existing long, open new short. |

### 10.6 Multi-pair independence (LOCKED)

If §9 produced a 2-pair shortlist (cross-pair expansion), each pair tracks its own position and triggers independently. A long on EURUSD does not block a short on GBPUSD — they are entirely independent state machines.

### 10.7 Time-of-day window (LOCKED)

Triggers can fire at **any 5-min close** during the FX day. There is no time-of-day gate on the entry trigger itself.

**However:** position sizing depends on *when* in the day the trigger fires. See §12 (TBD).

### 10.8 Evaluation timing

The trigger is evaluated on the **close of each 5-min candle** during the FX day. Specifically:
- The bar that opens at e.g. 09:30 closes at 09:35 → at 09:35 NY we evaluate the close against the bands.
- Order is fired immediately on that close.
- The first candle of an FX day that's eligible for entry is the bar opening 17:00 (closing 17:05) — see §11 for why the 16:55→17:00 bar is excluded.

---

## 11. Exit rules

### 11.1 End-of-day forced exit (LOCKED)

At the close of the **second-last 5-min candle** of every trading FX day — i.e., the bar that opens 16:50 NY and closes **16:55 NY** — we **exit all open positions** regardless of P&L, regardless of pair, regardless of any other rule.

**Why second-last and not last:** the last 5-min bar (opens 16:55, closes 17:00) is the FX-day rollover boundary. Liquidity often thins approaching 17:00, and exiting at 16:55 gives a 5-min cushion before the rollover.

**Schedule:**

| Day | Forced-exit candle close (NY time) | What happens after |
|---|---|---|
| Monday | 16:55 Mon | No trading 16:55→17:00; new FX day begins 17:00; trading resumes on the bar that closes 17:05. |
| Tuesday | 16:55 Tue | Same as above. |
| Wednesday | 16:55 Wed | Same as above. |
| Thursday | 16:55 Thu | Same as above. |
| **Friday** | 16:55 Fri | Market shuts 17:00 Fri → Sun 17:00. Bot drops back into the trading-zone gate (§8a). |

### 11.2 Loss-based exits (LOCKED)

Two new strategy inputs:

| Input | Type | Description |
|---|---|---|
| `per_trade_loss_pct` | float (percent) | SL cap per individual trade as % of frozen account balance |
| `per_day_loss_pct` | float (percent) | Daily P&L cap per account as % of frozen account balance |

**PnL polling:** subscribe to ticks for the active pair(s). On every tick, recompute unrealized PnL of open positions per account.

**Per-trade loss SL (per open position, per account):**
```
trade_loss_cap = frozen_balance(account) × (per_trade_loss_pct / 100)

if unrealized_pnl(position) ≤ -trade_loss_cap:
    place market-order close for that position immediately
```

**Per-day loss circuit breaker (per account):**
```
day_loss_cap = frozen_balance(account) × (per_day_loss_pct / 100)
day_pnl      = realized_pnl_since_FX_day_start + sum(unrealized_pnl of open positions)

if day_pnl ≤ -day_loss_cap:
    1. Force-close all open positions for that account.
    2. Halt new entries for that account for the remainder of this FX day.
    3. Resume normally at next FX day rollover (17:00 NY).
```

The circuit breaker is **per-account**, not portfolio-wide. If account A trips at 14:00 NY, account A halts but account B continues normally.

### 11.3 Reversal on opposite trigger

Already covered in §10.5. Treated as an exit + new entry in one operation: close existing position, open new position in opposite direction. Original quantity for the close, fresh sizing for the new entry (flat 0.25 lot per §12).

### 11.4 Trailing EMA exit (LOCKED)

A trailing exit based on a 50-period EMA on the pair's 5-min candle stream. Operates per-trade, per-account.

**Inputs (hardcoded for now, not strategy inputs):**
- `trail_arm_pct = 0.5` — % of frozen balance at which the trail arms
- `trail_ema_period = 50` — EMA period on 5-min candles

**Arming (per trade, per account):**

```
arm_threshold(account) = frozen_balance(account) × (trail_arm_pct / 100)

if not trade.trail_armed and unrealized_pnl(trade) ≥ arm_threshold(account):
    trade.trail_armed = True
```

- Armed-state is **sticky** for the life of the trade. Once armed, stays armed even if profit pulls back below the threshold.
- Arm threshold differs per sub-account because frozen balances differ. e.g., A=$40k → arms at $200; B=$10k → arms at $50.
- Arm check runs on the same tick-level PnL stream as §11.2 (no new subscription needed).

**Exit (only if armed):** evaluated on every 5-min candle close.

```
ema = EMA50 of pair's 5-min closes (continuous, never resets)

if trade.side == LONG  and trade.trail_armed and candle.close < ema:
    market-close the trade
if trade.side == SHORT and trade.trail_armed and candle.close > ema:
    market-close the trade
```

Strict inequality at the EMA — close exactly on the EMA does **not** exit.

**EMA computation:**
- Standard exponential moving average: `EMA_t = α × close_t + (1−α) × EMA_(t−1)`, `α = 2/(N+1) = 2/51`.
- Seeded with SMA of first 50 closes; switch to EMA recursion from bar 51 onward.
- EMA is a **continuous indicator on the 5-min stream** — does **not** reset at 17:00 NY rollover. CPR resets, EMA does not.
- At startup, pre-warm by fetching ≥ 250 5-min bars per shortlisted pair (5× the EMA window) so the EMA is well-converged before any trade can arm.

**Per-account trail state:** each FA sub-account tracks its own `trail_armed` flag per open trade. Account A's trade may not yet be armed while account B's already is (different frozen balances).

### 11.5 Exit precedence

For a single open position on a single 5-min candle close, exits are checked in this order. First match wins; remaining checks are skipped.

1. **EOD 16:55 NY** (§11.1) — forced close, no other condition matters.
2. **Daily-loss circuit breaker** (§11.2) — close all for that account.
3. **Per-trade SL** (§11.2) — close that trade.
4. **Trailing EMA exit** (§11.4) — close that trade if armed and EMA crossed.
5. **Reversal entry trigger** (§10.5, §11.3) — close the existing position, open new opposite.

In practice, 3 and 4 are mutually exclusive (SL implies deep loss; trail-armed implies prior profit). But the precedence above resolves any edge case.

---

## 12. Position sizing (LOCKED — flat)

```
lot_size = 0.25   # constant for every order, every account, every pair, every hour
```

This is the IBKR IDEALPRO minimum order size (= 25,000 base-currency units). Using it everywhere guarantees no orders are rejected for being below the minimum.

**No ratios, no multi-asset division, no first-hour halving.** All previously discussed scaling rules are explicitly dropped in favor of this flat sizing.

### 12.1 First-hour definition (locked, even though unused for sizing)

A candle is "first hour" iff its **close time is strictly before 18:00 NY**.

| Bar opens | Bar closes | First hour? |
|---|---|---|
| 17:00 | 17:05 | ✅ |
| 17:50 | 17:55 | ✅ |
| 17:55 | 18:00 | ❌ (boundary excluded) |
| 18:00 | 18:05 | ❌ |

First-hour bars: 11 per FX day (closes 17:05, 17:10, …, 17:55). Definition retained in case future rules need it.

### 12.2 Balance file (used by §11.2, not by sizing)

- Path: `forex_cpr_ibkr/account_balances.json`
- Format: `{ "DU1234567": 40000.0, "DU7654321": 10000.0 }` (account_code → balance in account currency)
- **First run:** query each FA sub-account balance via IBKR → write file.
- **Subsequent runs:** read file. **Never overwrite, never update.**
- If file is missing → create with current balances.
- If file exists but is corrupted (malformed JSON, missing accounts) → raise error at startup; user must manually delete or fix.
- The balances are frozen reference values used only for `per_trade_loss_pct` and `per_day_loss_pct` calculations.

---

## 13. FA allocation (LOCKED — Option 3)

**Option 3 — N separate orders, one per FA sub-account.**

Each order has:
- `order.account = sub_account_code`
- Same quantity (0.25 lot) per §12

Sub-accounts are listed via `IB.managedAccounts()` at startup. The same set is used for the balance file (§12.2) and order placement.

Order parallelism: orders for all sub-accounts placed via `asyncio.gather()` so an N-account FA sees N orders go out concurrently rather than sequentially.

### 13.0 Minimum balance filter ($1000+) — LOCKED

**Only sub-accounts with frozen balance ≥ $1000 are eligible to trade.**

- Applied at startup, against the frozen balance from `account_balances.json` (§12.2).
- Sub-accounts with balance < $1000 are excluded from `active_accounts` for the entire run.
- They get no orders, no PnL subscriptions, no per-trade SL, no daily breaker, no trail.
- If the user wants a previously-excluded account re-included after it grows above $1000, they delete `account_balances.json` and let it rebuild on next startup.

### 13.1 Per-account state isolation

Each sub-account maintains its own:
- Open position state per shortlisted pair
- Realized + unrealized PnL for the FX day
- Per-trade SL trigger
- Per-day loss circuit breaker (one trips → only that account halts)

A circuit-breaker trip on account A does not affect account B. New entry signals route to all *non-halted* accounts.

---

## 14. Safety rules (LOCKED — do not relax without explicit user override)

1. **Default mode: DRY-RUN.** `place_order()` logs the full intent (symbol, side, qty, account, group, sl/tp) but never calls `ib.placeOrder()`.
2. **Live mode requires an explicit config change** — not a CLI flag. Cannot be flipped accidentally.
3. **Belt-and-suspenders during dev**: connect with `IBKRConfig.read_only=True` so even a bug in dry-run can't reach the wire.
4. **`whatIf=True`** available for margin/order-shape testing without execution.
5. Connection target: IB Gateway live, port **4001** (live) — NOT 4002 (paper, which doesn't exist for this account).

---

## 15. Open questions for the user — ALL CLOSED

- [x] §7 — Dynamic CPR = the weekly→daily shifting in §6.
- [x] §9 — Asset selection (primary + cross-pair expansion).
- [x] §10 — Entry rules.
- [x] §11.1 — EOD forced exit at 16:55 NY.
- [x] §11.2 — Per-trade and per-day loss circuit breakers.
- [x] §11.4 — Trailing EMA exit (50-period EMA, arms at 0.5% × frozen balance, **strict cross**, **sticky armed**).
- [x] §12 — Position sizing flat 0.25 lot.
- [x] §13 — FA Option 3 (per-sub-account orders, parallel).
- [x] §13.0 — $1000+ frozen balance filter for active accounts.
- [x] §16 — PnL tracking (IBKR live, simulated dry-run).

Spec frozen.

---

## 16. PnL tracking — implementation note

This section pins down how `per_trade_loss_pct` (§11.2) and `per_day_loss_pct` (§11.2) are actually monitored at runtime. It's an implementation note, not a user-facing rule.

### 16.1 Live mode — use IBKR's PnL streaming

| IBKR API | Returns | Used for |
|---|---|---|
| `ib.reqPnL(account)` | Account-level `dailyPnL`, `unrealizedPnL`, `realizedPnL`. Auto-updates as positions move. | Daily circuit breaker (§11.2). One subscription per FA sub-account, lives the whole session. |
| `ib.reqPnLSingle(account, "", conId)` | Per-position `dailyPnL`, `unrealizedPnL`, `realizedPnL`. | Per-trade SL (§11.2). One subscription per `(account, conId)`, opened on entry fill, cancelled on exit. |

Both stream callbacks via `updateEvent`. Cadence ≈ every few seconds whenever the underlying value moves.

**Why IBKR not us:** their numbers include commissions, financing charges, FX conversion to account currency, correct lot multipliers per pair (especially the JPY-pip nuance). Re-implementing that is fiddly and easy to drift on.

### 16.2 Daily-reset boundary — verify against IBKR

The spec defines `per_day_loss_pct` as % of frozen balance accumulated from the **17:00 NY FX-day rollover**. IBKR's `dailyPnL` resets on their broker convention — for forex, this is typically the 17:00 NY rollover, but **must be verified during build** before relying on it.

**If IBKR's reset matches our 17:00 NY:** trust their `dailyPnL` directly.

**If it doesn't:** snapshot `realizedPnL` from `reqPnL` at each 17:00 NY rollover, then compute:
```
our_day_pnl = (current_realizedPnL − snapshot_at_last_1700_NY)
            + (current_unrealizedPnL across open positions)
```

Either way the consumer interface (§16.4) is unchanged.

### 16.3 Subscription lifecycle (per-trade)

```
On entry fill (account, conId):
    sub = ib.reqPnLSingle(account, "", conId)
    sub.updateEvent += check_trade_sl
    save sub in tracker keyed by (account, conId)

On any exit path (SL hit, EOD 16:55, reversal, daily breaker):
    ib.cancelPnLSingle(reqId)
    remove from tracker
```

Critical: cancellation on exit is mandatory. IBKR caps concurrent market-data subscriptions per session.

### 16.4 Dry-run mode — simulate

In dry-run we never place real orders, so there's nothing to subscribe to. Fake the math:

- On "entry": store entry price, qty, side, account, pair locally.
- Subscribe to bid/ask ticks for the pair (free; tick subscription is needed anyway for §11.2 polling).
- On every tick:
  ```
  unrealized = (mid - entry_price) × qty × direction × pip_value × fx_to_account_ccy
  ```
- Sum realized over the FX day from simulated fills.

Dry-run PnL doesn't need to be exact — it's for shape verification, not accounting.

### 16.5 Single tracker interface

Whether live or dry-run, the rest of the strategy talks to a single `PnLTracker` object:

```python
class PnLTracker:
    def on_entry(self, account: str, conId: int, fill: Fill) -> None: ...
    def on_exit (self, account: str, conId: int) -> None: ...
    def trade_pnl(self, account: str, conId: int) -> float: ...
    def day_pnl (self, account: str) -> float: ...
```

Two implementations behind it:
- `IBKRPnLTracker` (live) — wraps `reqPnL` / `reqPnLSingle`.
- `SimulatedPnLTracker` (dry-run) — local math from ticks + simulated fills.

Strategy code is mode-agnostic. The `LIVE_TRADING` flag (§14) decides which tracker is wired in at startup.

---

*Last updated: 2026-05-03*
