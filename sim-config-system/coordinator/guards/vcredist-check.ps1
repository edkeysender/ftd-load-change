# PASS (exit 0) if BOTH the x64 and x86 Visual C++ 2015-2022 runtimes are installed.
$ErrorActionPreference = 'SilentlyContinue'
function RtInstalled($path) {
  $p = Get-ItemProperty $path -ErrorAction SilentlyContinue
  if ($p -and $p.Installed -eq 1) { return "$($p.Major).$($p.Minor).$($p.Bld)" }
  return $null
}
$x64 = RtInstalled 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
$x86 = RtInstalled 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86'
if (-not $x86) { $x86 = RtInstalled 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86' }  # 32-bit OS
if ($x64 -and $x86) { Write-Output "VC++ x64=$x64 x86=$x86 installed"; exit 0 }
Write-Output "missing runtime (x64=$x64 x86=$x86)"; exit 1
