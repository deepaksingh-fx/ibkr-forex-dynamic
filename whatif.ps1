# whatif.ps1 — Windows wrapper for scripts/whatif.py
#
# Usage examples:
#   .\whatif.ps1 -Lot 0.05 -Side SELL
#   .\whatif.ps1 -Lot 0.25 -Side BUY
#   .\whatif.ps1 -Lot 0.10 -Side SELL -Account U25265693
#   .\whatif.ps1 -Lot 0.05 -Side SELL -Symbol EURUSD
#   .\whatif.ps1 -Lot 0.05 -Side SELL -Port 4002         # paper gateway
#
# What it does:
#   Submits the order with whatIf=True so IBKR returns margin/commission
#   analysis. NOTHING IS FILLED, NO MONEY MOVES. Reveals leverage / margin
#   rejections (Error 201) and other broker-side issues without spending.

param(
    [Parameter(Mandatory=$true)] [double] $Lot,
    [Parameter(Mandatory=$true)] [ValidateSet('BUY','SELL')] [string] $Side,
    [string] $Symbol = 'EURUSD',
    [string] $Account = '',
    [int]    $Port = 4001,
    [string] $Host_ = '127.0.0.1'
)

$ErrorActionPreference = 'Continue'

$pyArgs = @(
    'scripts/whatif.py',
    '--lot',    $Lot,
    '--side',   $Side,
    '--symbol', $Symbol,
    '--port',   $Port,
    '--host',   $Host_
)
if ($Account -ne '') {
    $pyArgs += '--account'
    $pyArgs += $Account
}

python @pyArgs
