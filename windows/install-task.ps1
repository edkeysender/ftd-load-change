# FTD Mode Switcher - Install sync agent as scheduled task
# Run as Administrator: powershell.exe -ExecutionPolicy Bypass -File install-task.ps1

$ErrorActionPreference = "Stop"

$TaskName    = "FTD-Mode-Sync"
$ScriptPath  = "C:\git\sync-agent.ps1"
$ScriptDir   = Split-Path $ScriptPath -Parent

Write-Host "=== Installing FTD Mode Sync as scheduled task ===" -ForegroundColor Cyan

# Ensure target dir exists
if (-not (Test-Path $ScriptDir)) {
    New-Item -ItemType Directory -Force -Path $ScriptDir | Out-Null
}

# Copy sync-agent.ps1 next to this installer if not already there
$Source = Join-Path $PSScriptRoot "sync-agent.ps1"
if ((Test-Path $Source) -and ((Resolve-Path $Source).Path -ne (Resolve-Path -ErrorAction SilentlyContinue $ScriptPath).Path)) {
    Copy-Item -Path $Source -Destination $ScriptPath -Force
    Write-Host "Copied sync-agent.ps1 to $ScriptPath" -ForegroundColor Green
}

if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: $ScriptPath not found. Place sync-agent.ps1 next to this installer." -ForegroundColor Red
    exit 1
}

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the task
$action    = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

$trigger   = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings  = New-ScheduledTaskSettingsSet `
             -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries `
             -RestartCount 999 `
             -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Polls RPi for FTD mode and deploys to D:\ftd\products\Load_testing" | Out-Null

Write-Host ""
Write-Host "Task installed: $TaskName" -ForegroundColor Green
Write-Host ""
Write-Host "Starting task now..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Format-List TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns

Write-Host "Log file: C:\git\ftd-sync.log" -ForegroundColor Cyan
Write-Host "Done." -ForegroundColor Green
