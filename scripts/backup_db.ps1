<#
.SYNOPSIS
    Daily copy of the STRIDE databases (data\*.db), keeping the last 30 days.

.DESCRIPTION
    Copies every data\*.db into <Destination>\<yyyy-MM-dd>\ once a day (a second
    run on the same day does nothing) and deletes day folders older than
    -KeepDays. scripts\run_stride.ps1 calls it after the paper runs, so no job
    is writing to the databases while they are copied.

    Default destination: %USERPROFILE%\Documents\stride_backups
        powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1
        powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1 -Destination D:\stride_backups
#>
param(
    [string]$Destination = (Join-Path $env:USERPROFILE "Documents\stride_backups"),
    [int]$KeepDays = 30
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $Root "data"
$Today = Get-Date -Format "yyyy-MM-dd"
$Target = Join-Path $Destination $Today

if (-not (Test-Path $Target)) {
    $Databases = Get-ChildItem -Path $Data -Filter "*.db" -File -ErrorAction SilentlyContinue
    if ($Databases) {
        New-Item -ItemType Directory -Force -Path $Target | Out-Null
        $Databases | Copy-Item -Destination $Target
        Write-Output "backup: $($Databases.Count) databases copied to $Target"
    }
}

$Cutoff = (Get-Date).Date.AddDays(-$KeepDays)
Get-ChildItem -Path $Destination -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' } |
    Where-Object { [datetime]::ParseExact($_.Name, "yyyy-MM-dd", $null) -lt $Cutoff } |
    ForEach-Object {
        Remove-Item -Recurse -Force -Path $_.FullName
        Write-Output "backup: removed $($_.Name) (older than $KeepDays days)"
    }
