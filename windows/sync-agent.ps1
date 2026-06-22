# FTD Mode Switcher - Windows Sync Agent
#
# Polls the RPi for the active mode and the selected version of each software
# component, assembles those versions into a staging dir, and mirrors the result
# into D:\ftd\products\Load_testing.
#
# Run manually:    powershell.exe -ExecutionPolicy Bypass -File sync-agent.ps1
# Install as task: powershell.exe -ExecutionPolicy Bypass -File install-task.ps1

param(
    [string]$RpiHost         = "RPI_HOSTNAME_OR_IP",   # <-- EDIT THIS
    [string]$RepoUrl         = "ftd@RPI_HOSTNAME_OR_IP:/home/ftd/repos/ftd.git",  # <-- EDIT THIS
    [string]$RepoPath        = "C:\git\ftd",
    [string]$StagePath       = "C:\git\ftd-stage",
    [string]$TargetPath      = "D:\ftd\products\Load_testing",
    [string]$StateCacheFile  = "C:\git\last_deploy.txt",
    [string]$LogFile         = "C:\git\ftd-sync.log",
    [int]$PollInterval       = 15
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

function Get-RemoteState {
    try {
        return Invoke-RestMethod -Uri "http://${RpiHost}:8089/api/state" -TimeoutSec 5
    } catch {
        Write-Log "Failed to reach RPi: $($_.Exception.Message)" "WARN"
        return $null
    }
}

function Get-StateSignature {
    param($State)
    if (-not $State) { return "" }
    $mode = [string]$State.current_mode
    $pairs = @()
    if ($State.versions) {
        foreach ($p in ($State.versions.PSObject.Properties | Sort-Object Name)) {
            $pairs += "$($p.Name)=$($p.Value)"
        }
    }
    return "$mode|" + ($pairs -join ";")
}

function Get-CachedSignature {
    if (Test-Path $StateCacheFile) {
        return (Get-Content $StateCacheFile -Raw).Trim()
    }
    return ""
}

function Set-CachedSignature {
    param([string]$Sig)
    $null = New-Item -ItemType Directory -Force -Path (Split-Path $StateCacheFile -Parent)
    Set-Content -Path $StateCacheFile -Value $Sig
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

function Deploy-State {
    param($State)

    $mode = [string]$State.current_mode
    Write-Log "===== Deploying mode '$mode' ====="

    if (-not (Initialize-Repo)) { return $false }

    Push-Location $RepoPath
    try {
        Write-Log "Fetching..."
        git fetch origin 2>&1 | ForEach-Object { Write-Log $_ }

        Write-Log "Checking out $mode..."
        git checkout $mode 2>&1 | ForEach-Object { Write-Log $_ }
        git reset --hard "origin/$mode" 2>&1 | ForEach-Object { Write-Log $_ }
        git clean -fdx 2>&1 | ForEach-Object { Write-Log $_ }

        # Rebuild a clean staging dir holding the union of selected versions.
        if (Test-Path $StagePath) { Remove-Item $StagePath -Recurse -Force }
        $null = New-Item -ItemType Directory -Force -Path $StagePath

        $deployed = 0
        $missing  = @()
        if ($State.versions) {
            foreach ($p in $State.versions.PSObject.Properties) {
                $component = $p.Name
                $version   = [string]$p.Value
                $src = Join-Path (Join-Path $RepoPath $component) $version
                if (Test-Path $src) {
                    Write-Log "Staging $component $version  ($src)"
                    Copy-Item -Path (Join-Path $src '*') -Destination $StagePath -Recurse -Force
                    $deployed++
                } else {
                    Write-Log "Selected $component $version not found at $src" "WARN"
                    $missing += "$component=$version"
                }
            }
        }

        if ($deployed -eq 0) {
            $why = if ($missing.Count -gt 0) { "all selections missing: " + ($missing -join ", ") }
                   else { "no versions selected" }
            Write-Log "Skipping mirror to avoid wiping $TargetPath ($why)." "ERROR"
            return $false
        }

        $null = New-Item -ItemType Directory -Force -Path $TargetPath
        Write-Log "Mirroring $deployed component(s) -> $TargetPath..."
        $null = robocopy $StagePath $TargetPath /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP
        $exit = $LASTEXITCODE
        if ($exit -ge 8) {
            Write-Log "Robocopy failed with exit code $exit" "ERROR"
            return $false
        }

        Write-Log "Deploy complete (robocopy exit: $exit)."
        return $true
    } finally {
        Pop-Location
    }
}

# =============================== Main loop ===============================
Write-Log "FTD Sync Agent starting. RPi=$RpiHost, Poll=${PollInterval}s, Target=$TargetPath"

$cachedSig = Get-CachedSignature
$state     = Get-RemoteState

if ($state) {
    $sig = Get-StateSignature $state
    if ($sig -ne $cachedSig) {
        Write-Log "Boot: state '$sig' differs from cached '$cachedSig'. Deploying."
        if (Deploy-State $state) { Set-CachedSignature $sig }
    } else {
        Write-Log "Boot: state '$sig' matches cache. No deploy needed."
    }
} elseif ($cachedSig) {
    Write-Log "Boot: RPi unreachable. Keeping last deploy ('$cachedSig')." "WARN"
} else {
    Write-Log "Boot: RPi unreachable and no cache. Waiting for RPi..." "WARN"
}

while ($true) {
    Start-Sleep -Seconds $PollInterval

    $state = Get-RemoteState
    if (-not $state) { continue }

    $sig    = Get-StateSignature $state
    $cached = Get-CachedSignature
    if ($sig -ne $cached) {
        Write-Log "Change detected: '$cached' -> '$sig'"
        if (Deploy-State $state) { Set-CachedSignature $sig }
    }
}
