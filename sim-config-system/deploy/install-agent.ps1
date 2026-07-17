# Install simagent.exe to always start ELEVATED at logon.
#
# Windows can't make a bare .exe self-elevate without a UAC prompt every time. The clean
# way is a scheduled task that runs at logon with "highest privileges" - it launches the
# agent with a full admin token and no prompt. The agent still runs AS the logged-in user
# (not SYSTEM) on purpose: the notifications / recycle-bin / wallpaper guards write the
# console user's own registry hive, which a SYSTEM process would miss.
#
# Run this ONCE per PC, in an ELEVATED PowerShell (right-click > Run as administrator):
#   powershell -ExecutionPolicy Bypass -File install-agent.ps1
# Optionally point it at the exe:  -ExePath D:\somewhere\simagent.exe
[CmdletBinding()]
param(
  [string]$ExePath,
  [string]$InstallDir = 'C:\sim-agent',
  [string]$TaskName = 'sim-agent'
)
$ErrorActionPreference = 'Stop'

# Must be elevated to register a highest-privileges task and control services later.
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Error 'Run this in an elevated PowerShell (Run as administrator).'; exit 1 }

# Find the exe: explicit path, next to this script, or already installed.
if (-not $ExePath) {
  foreach ($c in @((Join-Path $PSScriptRoot 'simagent.exe'), (Join-Path $InstallDir 'simagent.exe'))) {
    if (Test-Path $c) { $ExePath = $c; break }
  }
}
if (-not $ExePath -or -not (Test-Path $ExePath)) {
  Write-Error "simagent.exe not found. Copy it next to this script or pass -ExePath."; exit 1
}

# Install into a stable folder (also where the agent keeps its state + sensor DLLs).
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$target = Join-Path $InstallDir 'simagent.exe'
if ((Resolve-Path $ExePath).Path -ne (Join-Path $InstallDir 'simagent.exe')) {
  Copy-Item $ExePath $target -Force
  Write-Host "Copied agent to $target"
}

# The task runs as the person who will be signed in at the sim - use the current user, who
# MUST be a local administrator for the elevated token to actually carry admin rights.
$me = "$env:USERDOMAIN\$env:USERNAME"
$isLocalAdmin = (Get-LocalGroupMember -Group 'Administrators' -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -eq $me }) -ne $null
if (-not $isLocalAdmin) {
  Write-Warning "$me is not a local Administrator - the task will run but WITHOUT admin rights."
  Write-Warning "Add the account to Administrators (or run the task as one), then re-run this."
}

# Stop any hand-started agent so the task owns the one running instance.
Stop-Process -Name simagent -Force -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Force `
  -Action    (New-ScheduledTaskAction -Execute $target -WorkingDirectory $InstallDir) `
  -Trigger   (New-ScheduledTaskTrigger -AtLogOn -User $me) `
  -Principal (New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest) `
  -Settings  (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0) | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$p = Get-CimInstance Win32_Process -Filter "Name='simagent.exe'" | Select-Object -First 1
if ($p) {
  $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner
  Write-Host "Agent running as $($o.Domain)\$($o.User) (pid $($p.ProcessId)), elevated at logon via task '$TaskName'."
} else {
  Write-Warning "Task registered, but simagent.exe is not running yet - check Task Scheduler > $TaskName."
}
Write-Host "Done. It now starts automatically, elevated, every time this user signs in."
