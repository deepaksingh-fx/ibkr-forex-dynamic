# forex_cpr_ibkr

Single-strategy CPR-based forex bot for Interactive Brokers FA accounts.

Trades a configurable list of forex pairs on 5-minute candles using the **dynamic CPR** rule (weekly CPR on Monday, daily CPR Tue–Fri, both anchored to 17:00 NY local time so DST handles itself). See `SPEC.md` for the full specification.

**Default mode is dry-run (no real orders).** Real trading requires two explicit flags.

---

## Prerequisites

- **Python 3.12** (via conda or pyenv)
- **IB Gateway** (or TWS) running on port `4001` (live) or `4002` (paper)
- IBKR account with **API access enabled**:
  - In Gateway/TWS: Configure → Settings → API → Settings → enable "ActiveX and Socket Clients"
  - Note the socket port (default 4001 live / 4002 paper)
  - For FA accounts trading at the spec'd 0.10 lot, ensure sub-accounts have **Forex Trading** permission enabled in Account Management. (Leveraged FX is required to trade the IDEALPRO 0.25-lot minimum; 0.10 lot routes as odd-lot and works without leverage permission on most account types — confirm via the `whatIf` script below.)

---

## Setup

```bash
git clone git@github.com:deepaksingh-fx/ibkr-forex-dynamic.git
cd ibkr-forex-dynamic

# Option A — conda
conda create -n forex_cpr python=3.12 -y
conda activate forex_cpr
pip install -r requirements.txt

# Option B — venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Run

### Default — DRY-RUN (no orders)

```bash
python runner.py
```

This connects to IB Gateway, gates on the trading-zone window (Sun 17:00 NY → Fri 17:00 NY), runs the full strategy logic against live market data, and **logs** every trade intent without placing real orders.

### LIVE — places real orders

Requires **both** flags:

```bash
python runner.py --live --i-really-mean-it
```

Either one alone keeps you in dry-run.

### Useful flags

| Flag | Default | Description |
|---|---|---|
| `--allowed USD,JPY` | `USD` | Comma-separated allowed currencies for cross-pair expansion |
| `--trigger-pct 0.05` | `0.05` | Entry trigger band % above TC / below BC |
| `--trade-loss-pct 1.0` | `1.0` | Per-trade SL as % of frozen account balance |
| `--day-loss-pct 2.0` | `2.0` | Daily breaker as % of frozen account balance |
| `--host 127.0.0.1` | local | IBKR Gateway host |
| `--port 4001` | `4001` | IBKR Gateway port (4001=live, 4002=paper) |
| `--client-id 42` | `42` | IBKR client ID (must be unique per concurrent connection) |
| `--balance-file PATH` | `account_balances.json` | Frozen-balance JSON (auto-created on first run) |
| `--state-file PATH` | `strategy_state.json` | Persisted strategy state for restart recovery |
| `--force-clean-restart` | off | Wipe state file at startup, skip IBKR position reconciliation. **Dangerous.** |
| `-v / --verbose` | off | Debug logging |

### Stop the bot

`Ctrl-C` once. The runner catches SIGINT and stops cleanly.

---

## What happens on first run

1. Connects to IBKR Gateway (read-only by default; live mode disables read-only).
2. Fetches balances for every managed FA sub-account → writes `account_balances.json` (a one-time snapshot, never updated thereafter).
3. Filters accounts with balance ≥ $1000 (`MIN_ACCOUNT_BALANCE_USD` in `config.py`).
4. Enters trading-zone gate: outside the window, polls every 60 seconds.
5. Once inside the trading zone:
   - Reconciles state file vs IBKR open positions; refuses to start on mismatch
   - Computes weekly CPR for every symbol in `--symbols` (default 15 pairs)
   - Runs asset selection → produces shortlist (1 pair, or 2 via cross-pair expansion)
   - Pre-warms 50-EMA per shortlisted pair
   - Computes daily CPR (skipped on Mondays, where weekly = daily)
   - Subscribes to streaming 5-min bars
6. On each new closed 5-min bar: evaluate exit precedence (EOD / daily breaker / per-trade SL / trail-EMA / reversal) → entry trigger.
7. At each 17:00 NY rollover: recompute daily CPR for the new FX day.
8. At Fri 17:00 NY: closes positions, drops back to weekend gate.

---

## Pre-flight checks

### Check account balances + $1000 filter

```bash
python scripts/check_balances.py
```

### Verify order plumbing without placing real orders (whatIf)

```bash
python scripts/whatif_order_test.py            # 25,000 units (0.25 lot)
python scripts/whatif_order_test.py --units 10000   # 0.10 lot
```

`whatIf` orders are sent to IBKR for margin/commission analysis only — they never enter the market.

### Generate a backtest report for last week

```bash
python scripts/generate_last_week_report.py
# → reports/last_week_backtest.md
```

---

## Tests

```bash
# Pure-module tests (offline, fast)
pytest tests/ --ignore=tests/test_ibkr_live.py -q

# Live IBKR integration tests (gated, requires running gateway)
RUN_IBKR_LIVE=1 pytest tests/test_ibkr_live.py -v
```

186 unit tests + 11 live IBKR tests (1 weekend-skipped).

---

## Project layout

```
forex_cpr_ibkr/
├── runner.py              # CLI entry point — single command to start
├── strategy.py            # main loop: gates, CPR, selection, entry/exit precedence
├── ibkr_client.py         # ib_async wrapper (connection, contracts, bars, orders)
├── pnl_tracker.py         # SimulatedPnLTracker (live IBKRPnLTracker is TBD)
├── balance_store.py       # JSON balance file with $1000 filter
├── state_store.py         # Atomic strategy-state persistence (restart recovery)
├── config.py              # StrategyConfig dataclass + hardcoded tunables
├── cpr.py                 # CPR formula + width %
├── selection.py           # Asset selection (primary + cross-pair expansion)
├── indicators.py          # Streaming 50-EMA
├── pair_utils.py          # Currency hierarchy + pair construction
├── time_utils.py          # NY clock, FX day, trading zone, first hour, EOD
├── tests/                 # 186 unit + 11 live IBKR tests
├── scripts/               # one-off scripts (balances, whatIf, backtest, report)
└── SPEC.md                # full strategy specification
```

---

## Known limitations (read before going live)

1. **Per-trade SL is bar-close-level**, not tick-level. Spec says tick-level. With 5-min bars, you have up to 5 minutes of unhedged exposure if a wick goes past your cap and recovers by close.
2. **No reconnect handling.** If the IBKR socket drops, the script dies. Open positions remain in IBKR; restart the runner to reconcile.
3. **`SimulatedPnLTracker` is approximate.** USD-quote pairs (EURUSD) are exact; non-USD-quote pairs use rough conversion factors. Loss caps may fire ±5–10% off the configured threshold for cross-pairs.
4. **Sunday open lag.** Forex doesn't actually start trading until ~17:15 NY on Sunday; weekly CPR computation accounts for this (uses available bars).

See SPEC.md §11 and §16 for full caveats.

---

## License

Proprietary — internal use only.
