# One-time setup of the GitHub Actions paper run. Run it yourself, once, from the
# repository root:   powershell -ExecutionPolicy Bypass -File scripts\setup_actions.ps1
#
# It (1) creates the PRIVATE repo JAbarca74/stride-state, (2) makes a deploy key
# that can write only to that repo, (3) stores the key and the paper/data keys from
# your .env as Actions secrets of JAbarca74/stride. No value is printed.
# IBKR and any live-trading setting are never copied. Needs `gh` logged in.

$ErrorActionPreference = "Stop"
$Repo = "JAbarca74/stride"
$State = "JAbarca74/stride-state"

if (-not (Test-Path .env)) { throw "run this from the repository root (no .env here)" }

gh repo create $State --private --description "Private state mailbox for the STRIDE paper run"

$Dir = Join-Path $env:TEMP ("stride-key-" + [guid]::NewGuid())
New-Item -ItemType Directory $Dir | Out-Null
try {
    ssh-keygen -q -t ed25519 -N '""' -C stride-state-deploy -f "$Dir\key"
    gh repo deploy-key add "$Dir\key.pub" -R $State --allow-write --title actions-state
    Get-Content "$Dir\key" -Raw | gh secret set STATE_DEPLOY_KEY -R $Repo
}
finally {
    Remove-Item $Dir -Recurse -Force
}

$Wanted = "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DATA_API_KEY", "ALERT_TELEGRAM_TOKEN", "ALERT_TELEGRAM_CHAT_ID"
foreach ($Line in Get-Content .env) {
    if ($Line -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$' -and $Wanted -contains $Matches[1]) {
        $Matches[2] | gh secret set $Matches[1] -R $Repo
    }
}
gh secret list -R $Repo
