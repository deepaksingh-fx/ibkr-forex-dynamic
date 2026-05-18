# forex_cpr_ibkr - SHADOW launch (Windows / PowerShell)
#
# Runs the full strategy against live bars but does NOT place any orders.
# Every decision is logged to shadow CSVs under backtest_output/shadow/.
#
# Use this for a few days before flipping to live, to compare hypothetical
# trades against real market evolution.
#
# Usage:
#     .\start_shadow.ps1
#     .\start_shadow.ps1 -LotSize 0.01

param(
    [string]$LotSize = "0.3",
    [string]$Account = "U25265693"
)

$ErrorActionPreference = 'Continue'
$units = [int]([double]$LotSize * 100000)

Write-Host ''
Write-Host '========================================================' -ForegroundColor Green
Write-Host ' forex_cpr_ibkr - SHADOW MODE (no real orders)' -ForegroundColor Green
Write-Host '========================================================' -ForegroundColor Green
Write-Host " account      : $Account  (informational only - no orders)"
Write-Host " lot size     : $LotSize lot ($units units, used in shadow log only)"
Write-Host " port         : 4001"
Write-Host " state file   : strategy_state.json"
Write-Host " shadow logs  : backtest_output\shadow\shadow_events_*.csv"
Write-Host "                backtest_output\shadow\shadow_trades_*.csv"
Write-Host " log file     : shadow.log (appended)"
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

Write-Host 'Starting bot. Watch for "Dry-run mode" in startup log.' -ForegroundColor Cyan
Write-Host ''

python runner.py 2>&1 | Tee-Object -FilePath shadow.log -Append
