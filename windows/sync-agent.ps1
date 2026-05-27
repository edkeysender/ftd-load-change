# FTD Mode Switcher - Windows Sync Agent
# Polls the RPi for the current mode and deploys files to D:\ftd\products\Load_testing
# when the mode changes.
#
# Run manually:    powershell.exe -ExecutionPolicy Bypass -File sync-agent.ps1
# Install as task: powershell.exe -ExecutionPolicy Bypass -File install-task.ps1

param(
    [string]$RpiHost      = "RPI_HOSTNAME_OR_IP",   # <-- EDIT THIS
    [string]$RepoUrl      = "ftd@RPI_HOSTNAME_OR_IP:/home/ftd/repos/ftd.git",  # <-- EDIT THIS
    [string]$RepoPath     = "C:\git\ftd",
    [string]$TargetPath   = "D:\ftd\products\Load_testing",
    [string]$LastModeFile = "C:\git\last_mode.txt",
    [string]$LogFile      = "C:\git\ftd-sync.log",
    [int]$PollInterval    = 15
)

# Ensure parent dirs exist
$null = New-Item -ItemType Directory -Force -Path (Split-Path $RepoPath -Parent)
$null = New-Item -ItemType Directory -Force -Path (Split-Path $TargetPath -Parent)

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
}

function Get-RemoteMode {
    try {
        $resp = Invoke-RestMethod -Uri "http://${RpiHost}:8080/api/state" -TimeoutSec 5
        return $resp.current_mode
    } catch {
        Write-Log "Failed to reach RPi: $($_.Exception.Message)" "WARN"
        return $null
    }
}

function Get-LastMode {
    if (Test-Path $LastModeFile) {
        return (Get-Content $LastModeFile -Raw).Trim()
    }
    return ""
}

function Set-LastMode {
    param([string]$Mode)
    $null = New-Item -ItemType Directory -Force -Path (Split-Path $LastModeFile -Parent)
    Set-Content -Path $LastModeFile -Value $Mode
}

function Initialize-Repo {
    if (-not (Test-Path "$RepoPath\.git")) {
        Write-Log "Cloning repo from $RepoUrl..."
        git clone $RepoUrl $RepoPath 2>&1 | ForEach-Object { Write-Log $_ }
        if (-not (Test-Path "$RepoPath\.git")) {
            Write-Log "Clone failed!" "ERROR"
            return $false
        }
    }
    return $true
}

function Deploy-Mode {
    param([string]$Mode)

    Write-Log "===== Deploying mode: $Mode ====="

    if (-not (Initialize-Repo)) { return $false }

    Push-Location $RepoPath
    try {
        # Force-sync the working repo to remote branch
        Write-Log "Fetching..."
        git fetch origin 2>&1 | ForEach-Object { Write-Log $_ }

        Write-Log "Checking out $Mode..."
        git checkout $Mode 2>&1 | ForEach-Object { Write-Log $_ }
        git reset --hard "origin/$Mode" 2>&1 | ForEach-Object { Write-Log $_ }
        git clean -fdx 2>&1 | ForEach-Object { Write-Log $_ }

        # Ensure target exists
        $null = New-Item -ItemType Directory -Force -Path $TargetPath

        # Mirror to target: /MIR makes target an exact 1:1 copy
        # /XD .git excludes the .git folder
        Write-Log "Mirroring to $TargetPath..."
        $robo = robocopy $RepoPath $TargetPath /MIR /XD .git /R:2 /W:2 /NFL /NDL /NJH /NJS /NP
        $exit = $LASTEXITCODE

        if ($exit -ge 8) {
            Write-Log "Robocopy failed with exit code $exit" "ERROR"
            return $false
        }

        Write-Log "Deploy complete (robocopy exit: $exit)"
        Set-LastMode $Mode
        return $true
    } finally {
        Pop-Location
    }
}

# ============ Main loop ============
Write-Log "FTD Sync Agent starting. RPi=$RpiHost, Poll=${PollInterval}s, Target=$TargetPath"

# Initial deploy on startup if we have a cached mode (handles boot case)
$lastMode = Get-LastMode
$bootMode = Get-RemoteMode

if ($bootMode) {
    # RPi reachable: deploy current remote mode if it differs from last
    if ($bootMode -ne $lastMode) {
        Write-Log "Boot: remote mode '$bootMode' differs from cached '$lastMode'. Deploying."
        Deploy-Mode $bootMode | Out-Null
    } else {
        Write-Log "Boot: mode '$bootMode' matches cache. No deploy needed."
    }
} elseif ($lastMode) {
    Write-Log "Boot: RPi unreachable. Last known mode was '$lastMode'. Skipping deploy (files already in place)." "WARN"
} else {
    Write-Log "Boot: RPi unreachable and no cached mode. Waiting for RPi..." "WARN"
}

# Poll loop
while ($true) {
    Start-Sleep -Seconds $PollInterval

    $remoteMode = Get-RemoteMode
    if (-not $remoteMode) { continue }  # Skip if RPi unreachable

    $currentMode = Get-LastMode
    if ($remoteMode -ne $currentMode) {
        Write-Log "Mode change detected: '$currentMode' -> '$remoteMode'"
        Deploy-Mode $remoteMode | Out-Null
    }
}
