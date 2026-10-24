<#
.SYNOPSIS
    Runs the STRIDE daily job once for each paper instance (B, then DIP).

.DESCRIPTION
    Called every 15 minutes by the "STRIDE paper" scheduled task
    (scripts/install_task.ps1). Each run is `python -m stride.run_daily --phase auto`:
    the job itself decides which phase is due in New York time and does nothing
    outside the windows, on NYSE holidays, or when the phase already ran today.
    After both runs it makes the daily database backup (scripts/backup_db.ps1,
    once a day). All output is appended to data\logs\scheduler.log.

    Exit code: 0 when both runs succeeded, 1 if either failed.
    Paper trading only: the configs use mode: paper and paper keys from .env.
#>
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "scheduler.log"

function Write-Log([string]$Line) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $Log -Value "$stamp $Line" -Encoding UTF8
}

$Activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $Activate)) {
    Write-Log "error: no virtual environment at .venv (run: python -m venv .venv)"
    exit 1
}
. $Activate

$Failed = 0
foreach ($Config in @("config.paper_b.yaml", "config.paper_dip.yaml")) {
    Write-Log "start $Config"
    & python -m stride.run_daily --config $Config --phase auto 2>&1 |
        ForEach-Object { "$_" } | Add-Content -Path $Log -Encoding UTF8
    $Code = $LASTEXITCODE
    Write-Log "end $Config exit $Code"
    if ($Code -ne 0) { $Failed = 1 }
}

& (Join-Path $PSScriptRoot "backup_db.ps1") 2>&1 |
    ForEach-Object { "$_" } | Add-Content -Path $Log -Encoding UTF8

exit $Failed
