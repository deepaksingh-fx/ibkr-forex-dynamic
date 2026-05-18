# forex_cpr_ibkr - LIVE launch (Windows / PowerShell)
#
# Places REAL CFD orders on the configured account.
#
# Usage:
#     .\start_live.ps1
#     .\start_live.ps1 -LotSize 0.01           # smaller size for break-in week
#     .\start_live.ps1 -ForceCleanRestart      # wipe state file (dangerous)
#
# If execution policy blocks scripts:
#     powershell -ExecutionPolicy Bypass -File .\start_live.ps1
#
# What it does:
#   - Sets LOT_SIZE + CFD_ACCOUNT env vars for this process
#   - Asks for explicit confirmation before placing real orders
#   - Preserves strategy_state.json across restarts (state-based recovery)
#   - Streams logs to terminal AND appends to live.log

param(
    [string]$LotSize = "0.3",
    [string]$Account = "U25265693",
    [switch]$ForceCleanRestart
)

$ErrorActionPreference = 'Continue'
$units = [int]([double]$LotSize * 100000)

Write-Host ''
Write-Host '========================================================' -ForegroundColor Red
Write-Host ' forex_cpr_ibkr - LIVE MODE (real orders)' -ForegroundColor Red
Write-Host '========================================================' -ForegroundColor Red
Write-Host " account      : $Account"
Write-Host " lot size     : $LotSize lot ($units units per order)"
Write-Host " port         : 4001 (live IB Gateway)"
Write-Host " state file   : strategy_state.json (preserved across restarts)"
Write-Host " log file     : live.log (appended)"
Write-Host '========================================================' -ForegroundColor Red
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
Write-Host ''

# Explicit confirmation before live trading
$confirm = Read-Host "LIVE MODE — this will place real CFD orders on $Account. Type 'YES' to proceed"
if ($confirm -ne 'YES') {
    Write-Host 'Aborted.' -ForegroundColor Yellow
    exit 1
}

# Set env vars for this Python process
$env:LOT_SIZE = $LotSize
$env:CFD_ACCOUNT = $Account

# Build argument list
$pythonArgs = @('runner.py', '--live', '--i-really-mean-it')
if ($ForceCleanRestart) {
    $pythonArgs += '--force-clean-restart'
    Write-Host 'WARNING: --force-clean-restart will WIPE strategy_state.json' -ForegroundColor Yellow
    Write-Host '         and skip IBKR-position reconciliation.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Starting bot. Watch for LIVE_TRADING=True in the startup log.' -ForegroundColor Cyan
Write-Host 'If LIVE_TRADING=False appears, kill with Ctrl+C — wrong mode.' -ForegroundColor Cyan
Write-Host ''

python @pythonArgs 2>&1 | Tee-Object -FilePath live.log -Append
