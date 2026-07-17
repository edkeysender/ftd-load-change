# PASS (exit 0) if Windows is activated (licensed). Check-only: activating needs a
# product key or KMS server, which varies per site and must not be automated blindly -
# so a failure is fixed by hand.
$ErrorActionPreference = 'SilentlyContinue'

# ApplicationID of the Windows OS licence (constant); PartialProductKey filters out the
# many unlicensed slots so we look at the real installed edition.
$app = '55c92734-d682-4d71-983e-d6ec3f16059f'
$p = Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='$app' AND PartialProductKey IS NOT NULL" |
     Select-Object -First 1
if (-not $p) { Write-Output 'Windows is not activated (no licensed product installed)'; exit 1 }

# LicenseStatus: 0 Unlicensed, 1 Licensed, 2 OOB grace, 3 OOT grace, 4 non-genuine,
# 5 notification, 6 extended grace. Only 1 is a clean, activated state.
$status = [int]$p.LicenseStatus
$names = @{0='unlicensed';1='licensed';2='out-of-box grace';3='out-of-tolerance grace';
           4='non-genuine grace';5='notification (unactivated)';6='extended grace'}
if ($status -eq 1) { Write-Output "Windows activated - $($p.Name)"; exit 0 }
Write-Output "Windows NOT activated - status $status ($($names[$status])) - activate it by hand (Settings > System > Activation)"
exit 1
