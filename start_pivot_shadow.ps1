# forex_cpr_ibkr - PIVOT strategy SHADOW launch (Windows / PowerShell)
#
# Runs the NEW pivot/SR + quality-SuperTrend strategy against live bars but
# does NOT place any orders. Every decision is logged to shadow CSVs under
# backtest_output/pivot_shadow/.
#
# This is the new strategy (runner_pivot.py). The old CPR strategy is
# unaffected - use start_shadow.ps1 for that one.
#
# Usage:
#     .\start_pivot_shadow.ps1
#     .\start_pivot_shadow.ps1 -LotSize 0.01
#     .\start_pivot_shadow.ps1 -ForceCleanRestart   # start fresh (wipe state)

param(
    [string]$LotSize = "0.3",
    [string]$Account = "U25265693",
    [switch]$ForceCleanRestart
)

$ErrorActionPreference = 'Continue'
$units = [int]([double]$LotSize * 100000)

Write-Host ''
Write-Host '========================================================' -ForegroundColor Green
Write-Host ' forex_cpr_ibkr - PIVOT strategy SHADOW MODE (no orders)' -ForegroundColor Green
Write-Host '========================================================' -ForegroundColor Green
Write-Host " account      : $Account  (informational only - no orders)"
Write-Host " lot size     : $LotSize lot ($units units, used in shadow log only)"
Write-Host " port         : 4001"
Write-Host " state file   : pivot_strategy_state.json"
Write-Host " shadow logs  : backtest_output\pivot_shadow\pivot_events_*.csv"
Write-Host "                backtest_output\pivot_shadow\pivot_trades_*.csv"
Write-Host " log file     : pivot_shadow.log (appended)"
Write-Host '========================================================' -ForegroundColor Green
Write-Host ''

# Pre-flight: confirm IB Gateway is reachable
$tcp = New-Object Net.Sockets.TcpClient
try {
    $tcp.Connect('127.0.0.1', 4001)
    if (-not $tcp.Connected) { throw "not connected" }
    Write-Host 'IB Gateway is reachable on port 4001.' -ForegroundColor Green
} catch {
    Write-Host 'ERROR: cannot reach IB Gateway on 127.0.0.1:4001.' -ForegroundColor Red
    Write-Host 'Start IB Gateway and log in first, then re-run this script.'
    exit 1
} finally {
    $tcp.Close()
}

# Set env vars for this Python process
$env:LOT_SIZE = $LotSize
$env:CFD_ACCOUNT = $Account

# Build argument list
$pythonArgs = @('runner_pivot.py')
if ($ForceCleanRestart) {
    $pythonArgs += '--force-clean-restart'
    Write-Host 'NOTE: --force-clean-restart will WIPE pivot_strategy_state.json' -ForegroundColor Yellow
}

Write-Host 'Starting bot. Watch for "SHADOW mode" in the startup log.' -ForegroundColor Cyan
Write-Host ''

python @pythonArgs 2>&1 | Tee-Object -FilePath pivot_shadow.log -Append
