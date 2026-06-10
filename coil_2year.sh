#!/bin/bash
# One-shot 2-year backtest: warm the bar cache (resumable, gentle pacing), then
# run the backtest offline and build the per-session report.
# Needs IB Gateway up. Re-running is safe - it resumes from the cache.
#
#   bash coil_2year.sh
#
PY=/Users/deepak/miniconda3/envs/trading-engine/bin/python
cd "$(dirname "$0")"

WARM_START=2024-05-28; WARM_END=2026-06-11      # download range (buffer for warmup/CPR)
BT_START=2024-06-03;   BT_END=2026-06-05        # backtest range
N_SERIES=33                                     # 11 pairs x MID/BID/ASK

echo "[1/3] Warming bar cache (gentle 11s pacing; auto-retries through drops)..."
for attempt in $(seq 1 40); do
  n=$(ls barcache/*.json 2>/dev/null | wc -l | tr -d ' ')
  echo "  pass $attempt: $n/$N_SERIES series cached"
  [ "$n" -ge "$N_SERIES" ] && break
  $PY warm_cache.py "$WARM_START" "$WARM_END" || true
  sleep 30      # let any IBKR pacing penalty clear between passes
done
n=$(ls barcache/*.json 2>/dev/null | wc -l | tr -d ' ')
echo "  cache: $n/$N_SERIES series"

echo "[2/3] Backtesting offline (1 lot, bid/ask, \$2/order, continuous cap)..."
$PY -c "
from datetime import date, timedelta
d=date.fromisoformat('$BT_START'); e=date.fromisoformat('$BT_END')
while d<=e:
    if d.weekday()<5: print(d.isoformat())
    d+=timedelta(days=1)" > /tmp/dates_2y.txt
while read -r dd; do
  [ -z "$dd" ] && continue
  $PY coil_backtest.py --date "$dd" --units 100000 --risk 165 --breakeven 165 \
      --daily-limit 500 --commission 2 --offline > /dev/null 2>&1
done < /tmp/dates_2y.txt

echo "[3/3] Building report..."
$PY coil_report.py "$BT_START" "$BT_END" coil_2year_report.xlsx
echo "DONE -> coil_2year_report.xlsx  (sheets: Summary, Daily, Trades, + one per session)"
