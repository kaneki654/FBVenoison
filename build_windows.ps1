# Rebuild the hub and both bundled apps from their current source on Windows.
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$ReleaseRoot = Join-Path $ProjectRoot "release/FBVenoison"

function Invoke-AppBuild([string]$Folder, [string]$Spec, [string]$Destination) {
    Push-Location $Folder
    try {
        & $Python -m PyInstaller --clean --noconfirm --distpath $Destination $Spec
        if ($LASTEXITCODE -ne 0) { throw "Build failed: $Spec" }
    } finally {
        Pop-Location
    }
}

if ($env:OS -ne "Windows_NT") { throw "Run this release build on Windows." }
if (-not $env:FBVENOM_PLATFORM_TOOLS -and $env:LOCALAPPDATA) {
    $InstalledTools = Join-Path $env:LOCALAPPDATA "Android/platform-tools"
    if (Test-Path (Join-Path $InstalledTools "adb.exe")) {
        $env:FBVENOM_PLATFORM_TOOLS = $InstalledTools
    }
}
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
Invoke-AppBuild (Join-Path $ProjectRoot "FBPoison") "fbpoison.spec" (Join-Path $ReleaseRoot "FBPoison")
Invoke-AppBuild (Join-Path $ProjectRoot "FBVenom") "FBVenom.spec" (Join-Path $ReleaseRoot "FBVenom")
Invoke-AppBuild $ProjectRoot "fbvenoison.spec" $ReleaseRoot
Copy-Item (Join-Path $ProjectRoot "update_config.example.json") $ReleaseRoot
@'
FBVenoison 3.0

Run fbvenoison.exe to open the live hub, or use --demo to explore a sample fleet.
Keep FBPoison/fbpoison.exe and FBVenom/FBVenom.exe beside the hub in their folders.
Keep FBVenom/FBVenom.suite-bridge.json with its matching executable.
Android platform-tools must be installed. Set ADB_PATH to your shared adb.exe.

Device targets default to 20 per app. Change targets and click Rebalance to
apply them, or select devices and move them individually using the footer.

Open Updates and configure your publisher's HTTPS manifest URL. Downloads are
verified and staged for manual installation. Stop both apps, preserve your local
configuration and AI settings, replace the executables, and restart the hub.

Configure private AI settings on the target installation; none are distributed.
'@ | Set-Content -Encoding UTF8 (Join-Path $ReleaseRoot "START-HERE.txt")
Write-Host "Built the current hub and both apps in $ReleaseRoot"
Write-Host "Preserve local settings when upgrading an existing installation."
