# forex_cpr_ibkr - live launch (Windows / PowerShell)
#
# Usage:
#   1. Stop any currently-running bot (Ctrl+C in the other window)
#   2. From the project root, run:
#         .\start_live.ps1
#      OR (if execution-policy blocks scripts):
#         powershell -ExecutionPolicy Bypass -File .\start_live.ps1
#
# What it does:
#   - Wipes stale account_balances.json (forces fresh balance fetch)
#   - Wipes strategy_state.json (clean reconcile from broker)
#   - Launches runner.py with: USD,JPY allowed; entry 0.05%; per-trade SL 0.33%;
#     per-day cap 1%; LIVE TRADING ON; --force-clean-restart
#   - Streams logs to terminal AND appends to bot.log

$ErrorActionPreference = 'Stop'

Write-Host ''
Write-Host '========================================================' -ForegroundColor Cyan
Write-Host ' forex_cpr_ibkr - LIVE LAUNCH' -ForegroundColor Cyan
Write-Host '========================================================' -ForegroundColor Cyan
Write-Host ' allowed       : USD,JPY'
Write-Host ' port          : 4001 (live gateway)'
Write-Host ' trigger       : 0.05 percent'
Write-Host ' per-trade SL  : 0.33 percent'
Write-Host ' per-day cap   : 1.0 percent'
Write-Host ' live trading  : YES'
Write-Host '========================================================' -ForegroundColor Cyan
Write-Host ''

# Wipe stale state - fresh balance fetch + clean reconcile.
Remove-Item -Force account_balances.json -ErrorAction SilentlyContinue
Remove-Item -Force strategy_state.json   -ErrorAction SilentlyContinue
Write-Host 'Cleared: account_balances.json, strategy_state.json'
Write-Host ''
Write-Host 'Look for LIVE_TRADING=True in the first few log lines.'
Write-Host 'If you see LIVE_TRADING=False, kill it (Ctrl+C) - dry-run mode.'
Write-Host ''

python runner.py `
    --allowed USD,JPY `
    --port 4001 `
    --trigger-pct 0.05 `
    --trade-loss-pct 0.33 `
    --day-loss-pct 1.0 `
    --live `
    --i-really-mean-it `
    --force-clean-restart 2>&1 | Tee-Object -FilePath bot.log -Append
