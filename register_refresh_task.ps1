<#
    register_refresh_task.ps1 — schedule refresh_and_push.ps1 daily (boss's machine).

    Register:   powershell -ExecutionPolicy Bypass -File .\register_refresh_task.ps1
    Custom time:powershell -ExecutionPolicy Bypass -File .\register_refresh_task.ps1 -At 18:30
    Remove:     powershell -ExecutionPolicy Bypass -File .\register_refresh_task.ps1 -Unregister
    Run now:    schtasks /run /tn "Data\RefreshAndPush"

    Default 18:00 (after US releases/close). This does NOT touch the existing
    CitiVelocity task; refresh_and_push also invokes the Citi daily bat itself, so
    if you keep both, pass -SkipCiti here (edit the action below) to avoid double runs.
#>
param(
    [string]$At = "18:00",
    [string]$Python = "python",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$TaskName = "Data\RefreshAndPush"
$Script = Join-Path $PSScriptRoot "refresh_and_push.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed '$TaskName'."; return
}
if (-not (Test-Path $Script)) { throw "refresh_and_push.ps1 not found next to this script." }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$Script`" -Python `"$Python`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily refresh of all Data feeds + commit/push" -Force | Out-Null
Write-Output "Registered '$TaskName' to run refresh_and_push.ps1 daily at $At."
Write-Output "Test now:  schtasks /run /tn `"$TaskName`""
Write-Output "Log:       $(Join-Path $PSScriptRoot 'logs')"
