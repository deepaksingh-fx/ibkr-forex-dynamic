# forex_cpr_ibkr — Strategy Specification

Slim spec. The bot computes daily CPR width % for a list of forex pairs on each FX-day rollover, and **logs the narrowest**. No orders, no positions, no PnL. Broker plumbing (IBKR connection, FA accounts, dry-run safety) is kept intact so trading rules can be reattached later.

---

## 1. Project goals

- Single-broker (Interactive Brokers) standalone Python project.
- Connect to IBKR via `ib_async` (TWS / IB Gateway socket).
- IBKR account is a **Financial Advisor (FA) live account** — multiple sub-accounts under one login. **No paper account is available.**
- Bot does **not place orders**. It computes and logs the daily-narrowest CPR pair.

### 1.1 Strategy inputs

| Input | Type | Default | Used in |
|---|---|---|---|
| `symbols_list` | list[str] | the 15 pairs in §2 | §5 selection universe |

That's it. Risk inputs, entry bands, EMA, allowed-currencies expansion — all removed.

---

## 2. Symbols (13 forex pairs)

USDJPY, EURUSD, EURJPY, GBPUSD, GBPJPY, USDCAD, CADJPY, USDCHF, CHFJPY, AUDUSD, AUDJPY, NZDUSD, NZDJPY

Live trading uses IBKR CFD contracts (`secType='CFD'`, `exchange='SMART'`).
The selection-stage CPR is fetched from these same CFD contracts; bar
prices on IBKR are nearly identical between IDEALPRO spot and CFD anyway.

---

## 3. Timeframe and FX day

- Strategy operates on **5-minute candles**.
- All time math done in `America/New_York` zoneinfo (DST handled automatically).
- **FX day = `[prior calendar day 17:00 NY, today 17:00 NY)`** for bar open times.
- **Trading window:** Sun 17:00 NY → Fri 17:00 NY. Outside this window the bot sleeps and polls the gate every 60 seconds.

---

## 4. CPR formula

From the source window's 5-min candles:

```
H = max(high)
L = min(low)
C = close of the last 5-min bar of the source window

Pivot  = (H + L + C) / 3
BC     = (H + L) / 2
TC_raw = 2 · Pivot − BC

# Force ordering — TC must always be the higher line:
TC = max(TC_raw, BC)
BC = min(TC_raw, BC)
```

**Width %** (the selection metric):
```
CPR_width     = TC − BC                  (always ≥ 0)
CPR_width_pct = (CPR_width / Pivot) × 100
```

---

## 5. Daily selection

- **Source window:** most-recent COMPLETED trading FX day strictly before the current FX day.
  - Tue–Fri FX days: yesterday's FX day = `[yesterday 17:00 NY, today 17:00 NY)`.
  - **Monday FX day: last Friday's FX day** = `[Thu 17:00 NY, Fri 17:00 NY)`. The mechanical "yesterday" would be the weekend, which has no bars, so we walk back to the most recent trading session.
- **When:** at startup (once we're inside the trading zone), and again at each 17:00 NY FX-day rollover.
- **Algorithm:** for every symbol in `symbols_list`, fetch prior-trading-FX-day 5-min bars, compute CPR + width %, pick the symbol with the **lowest width %**. Tie-break: first appearance in `symbols_list`.
- **Output:** one log line per selection — `selected_pair`, `width_pct`, `TC`, `BC`, plus a sorted table of all pairs' width %.

No cross-pair expansion. No allowed-currencies input. Single narrowest pair per FX day.

---

## 6. Runtime loop

1. Connect to IBKR (reconnect with 5→60s backoff on socket drop).
2. **Trading-zone gate:** outside `[Sun 17:00 NY, Fri 17:00 NY)` → sleep 60s, recheck.
3. **Bootstrap accounts (broker rule):** snapshot FA sub-account NetLiquidation to `account_balances.json` on first run (never overwrite); load on subsequent runs; filter to balances ≥ $1000. Log active accounts.
4. **Initial selection:** compute + log.
5. **Loop:** sleep until next 17:00 NY rollover (or zone exit). On rollover, recompute + log. On zone exit, drop back to the gate.

---

## 7. Safety rules (LOCKED — even though we're not trading)

These are broker rules retained from the previous spec so that re-adding trade placement later doesn't reopen them:

1. **Default mode: DRY-RUN.** `IBKRClient.place_market_order()` logs intent and returns `{"status": "dry_run"}` unless `cfg.LIVE_TRADING=True`. Currently unreachable from the strategy loop (no order calls are made), but the gate is intact.
2. **Live mode requires `--live` AND `--i-really-mean-it`** at the CLI. Either alone keeps you in dry-run.
3. **`IBKRConnection.read_only=True`** by default; auto-forced `False` only when `LIVE_TRADING=True`. Contradictory combos refuse to start.
4. **Lot size constant:** `LOT_SIZE = 0.25` (= 25k IDEALPRO base ccy). Kept in config for when trading returns; unused by the selection loop.
5. **$1000+ filter:** sub-accounts below `MIN_ACCOUNT_BALANCE_USD = $1000` are excluded from `active_accounts`. Currently only affects logging.
6. Connection target: IB Gateway live, port **4001**. No paper account on this login.

---

## 8. What was removed (for reference)

The previous spec defined a full trading strategy: weekly→daily dynamic CPR shift, cross-pair expansion via `allowed_currencies`, TC/BC entry trigger bands, per-trade SL / daily-loss breaker / 50-EMA trail / EOD 16:55 exit, position-state machine, FA per-account order placement, PnL tracking. All of that has been **deleted** in this revision. The current bot is a selection logger only. Re-adding trading rules is a future task.

---

*Last updated: 2026-05-18*
