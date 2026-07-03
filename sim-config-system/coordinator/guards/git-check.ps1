# PASS (exit 0) if git is available anywhere the agent can use it.
$ErrorActionPreference = 'SilentlyContinue'
$paths = @(
  'C:\sim-agent\git\cmd\git.exe',
  'C:\Program Files\Git\cmd\git.exe',
  'C:\Program Files\Git\bin\git.exe'
)
foreach ($p in $paths) { if (Test-Path $p) { Write-Output "git at $p"; exit 0 } }
if (Get-Command git -ErrorAction SilentlyContinue) { Write-Output 'git on PATH'; exit 0 }
Write-Output 'git not found'; exit 1
