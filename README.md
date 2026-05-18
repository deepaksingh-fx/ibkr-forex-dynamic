# forex_cpr_ibkr

Daily-narrowest CPR pair logger for Interactive Brokers FA accounts.

On each 17:00 NY FX-day rollover, the bot computes the prior FX day's CPR (and width %) for every symbol in `--symbols`, then logs the pair with the narrowest CPR width. No orders are placed. See `SPEC.md`.

> Broker plumbing (connection, FA account discovery, $1000+ filter, dry-run safety gates, `whatIf` order test) is intact so trading rules can be reattached later.

---

## Prerequisites

- **Python 3.12** (via conda or pyenv)
- **IB Gateway** (or TWS) running on port `4001` (live). This account has no paper login.
- IBKR API access enabled in Gateway/TWS: Configure → Settings → API → Settings → enable "ActiveX and Socket Clients."

---

## Setup

```bash
git clone git@github.com:deepaksingh-fx/ibkr-forex-dynamic.git
cd ibkr-forex-dynamic

# conda
conda create -n forex_cpr python=3.12 -y
conda activate forex_cpr
pip install -r requirements.txt

# OR venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Run

```bash
python runner.py
```

Connects to IB Gateway (read-only socket), gates on the Sun 17:00 NY → Fri 17:00 NY window, snapshots FA sub-account balances on first run, and logs the daily-narrowest CPR pair on each 17:00 NY rollover.

### Flags

| Flag | Default | Description |
|---|---|---|
| `--host 127.0.0.1` | local | IBKR Gateway host |
| `--port 4001` | `4001` | IBKR Gateway port |
| `--client-id 42` | `42` | IBKR client ID (must be unique per concurrent connection) |
| `--balance-file PATH` | `account_balances.json` | Frozen-balance JSON (auto-created on first run) |
| `-v / --verbose` | off | Debug logging |
| `--live` + `--i-really-mean-it` | off | Disables the read-only socket. **No orders are placed** in this build — flag is plumbing for future trading rules. |

### Stop

`Ctrl-C` once.

---

## What happens on first run

1. Connect to IBKR Gateway (read-only socket).
2. Fetch FA sub-account NetLiquidation → write `account_balances.json` (one-time snapshot, never updated).
3. Log active accounts (those with balance ≥ $1000).
4. Enter trading-zone gate: outside the window, sleep 60s and recheck.
5. Inside the zone: compute daily CPR for every symbol from the prior FX day's 5-min bars. Log the narrowest + a ranking table.
6. Sleep until next 17:00 NY rollover. Re-select. Repeat.
7. At Fri 17:00 NY: drop back to the weekend gate.

---

## Pre-flight checks

### Account balances + $1000 filter
```bash
python scripts/check_balances.py
```

### Verify order plumbing (whatIf — no real orders)
```bash
python scripts/whatif_order_test.py            # 25,000 units (0.25 lot)
python scripts/whatif_order_test.py --units 10000   # 0.10 lot
```

---

## Tests

```bash
# Offline tests (fast)
pytest tests/ --ignore=tests/test_ibkr_live.py -q

# Live IBKR integration tests (requires running gateway)
RUN_IBKR_LIVE=1 pytest tests/test_ibkr_live.py -v
```

---

## Project layout

```
forex_cpr_ibkr/
├── runner.py        # CLI entry point
├── strategy.py      # gate + daily selection loop
├── selection.py     # narrowest_pair() — pure
├── cpr.py           # CPR formula + width %
├── time_utils.py    # NY clock, FX day, trading zone, prior-FX-day window
├── ibkr_client.py   # ib_async wrapper (connection, bars, accounts, dormant order path)
├── balance_store.py # JSON frozen-balance store with $1000 filter
├── config.py        # StrategyConfig + IBKRConnection
├── tests/
├── scripts/         # check_balances, whatif_order_test, whatif
└── SPEC.md
```

---

## License

Proprietary — internal use only.
