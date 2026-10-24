<#
.SYNOPSIS
    Registers the ONE Windows scheduled task that runs STRIDE paper trading.

.DESCRIPTION
    Task "STRIDE paper": every 15 minutes, every day, it runs scripts\run_stride.ps1,
    which runs `python -m stride.run_daily --phase auto` for config.paper_b.yaml and
    config.paper_dip.yaml. The job picks the phase due in New York time, so the
    task needs no change when the US switches to or from daylight-saving time
    (Costa Rica stays at UTC-6 all year).

    Settings: run whether the user is logged on or not (S4U logon: Windows stores
    no password, and none is ever written to the repository), wake the computer
    to run, run as soon as possible after a missed start, stop after 30 minutes,
    and never start a second copy while one is running.

    Run it once from an elevated PowerShell (Run as administrator):
        powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
    Running it again replaces the task.
#>
param(
    [string]$TaskName = "STRIDE paper"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\run_stride.ps1"
if (-not (Test-Path $Script)) { throw "missing $Script" }

$Admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $Admin) { throw "run this script from PowerShell opened with 'Run as administrator'" }

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $Root

# Every day at 00:00, repeated every 15 minutes for the whole day: the ticks land
# on :00, :15, :30 and :45, so the 09:15-09:30 morning window always gets one.
$Trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
$Repeat = New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 59)
$Trigger.Repetition = $Repeat.Repetition

$Settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$User = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal `
    -Description "STRIDE paper trading (B and DIP), every 15 minutes. Paper only." -Force |
    Out-Null

Write-Output "Registered scheduled task '$TaskName' for $User (every 15 minutes)."
Write-Output "Check it with: Get-ScheduledTask -TaskName '$TaskName'"
