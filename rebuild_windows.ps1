<#
.SYNOPSIS
Rebuild FBVenoison, FBVenom and FBPoison, then register their executable paths.
.EXAMPLE
.\rebuild_windows.ps1 -Launch
.EXAMPLE
.\rebuild_windows.ps1 -Scope User -PlatformTools C:\Android\platform-tools
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$OutputDirectory,
    [string]$PlatformTools,
    [ValidateSet('Machine', 'User')][string]$Scope = 'Machine',
    [switch]$SkipDependencyInstall,
    [switch]$SkipRegistration,
    [switch]$RegisterOnly,
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows with 64-bit Python installed.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $ProjectRoot 'release/FBVenoison' }
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $ProjectRoot $OutputDirectory
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$BuildCache = Join-Path $ProjectRoot '.build-tools'

function Invoke-BuildPython {
    param([string[]]$PythonArguments)
    & $script:BuildPython @PythonArguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($PythonArguments -join ' ')" }
}

function Find-PlatformTools {
    param([string]$Requested)
    if ($Requested) {
        $Candidates = @($Requested)
    } else {
        $Candidates = @($env:FBVENOM_PLATFORM_TOOLS,
            (Join-Path $ProjectRoot 'FBVenom/platform-tools'),
            (Join-Path $ProjectRoot 'platform-tools'),
            (Join-Path $BuildCache 'android/platform-tools'))
        if ($env:LOCALAPPDATA) { $Candidates += Join-Path $env:LOCALAPPDATA 'Android/platform-tools' }
        if ($env:ADB_PATH) { $Candidates += Split-Path $env:ADB_PATH -Parent }
        $AdbCommand = Get-Command adb.exe -ErrorAction SilentlyContinue
        if ($AdbCommand) { $Candidates += Split-Path $AdbCommand.Source -Parent }
    }
    foreach ($Candidate in $Candidates) {
        if (-not $Candidate) { continue }
        $Complete = $true
        foreach ($Name in @('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll')) {
            if (-not (Test-Path -LiteralPath (Join-Path $Candidate $Name) -PathType Leaf)) { $Complete = $false }
        }
        if ($Complete) { return [IO.Path]::GetFullPath($Candidate) }
    }
    if ($Requested) { throw "Windows platform-tools are incomplete at $Requested" }
    Write-Host 'Downloading Windows Android platform-tools from Google...'
    $AndroidCache = Join-Path $BuildCache 'android'
    New-Item -ItemType Directory -Force -Path $AndroidCache | Out-Null
    $Archive = Join-Path $AndroidCache 'platform-tools.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $Archive
    Expand-Archive -LiteralPath $Archive -DestinationPath $AndroidCache -Force
    return Find-PlatformTools -Requested (Join-Path $AndroidCache 'platform-tools')
}

function Invoke-AppBuild {
    param([string]$Folder, [string]$Spec, [string]$Destination, [string]$Name)
    Write-Host "`nRebuilding $Name..." -ForegroundColor Cyan
    Push-Location $Folder
    try {
        Invoke-BuildPython -PythonArguments @('-m', 'PyInstaller', '--clean', '--noconfirm',
            '--distpath', $Destination, '--workpath', (Join-Path $BuildCache "work/$Name"), $Spec)
    } finally { Pop-Location }
}

function Set-FBVenoisonEnvironment {
    param([hashtable]$Values, [string]$Target)
    $Location = [Enum]::Parse([EnvironmentVariableTarget], $Target)
    $Previous = @{}
    foreach ($Key in $Values.Keys) { $Previous[$Key] = [Environment]::GetEnvironmentVariable($Key, $Location) }
    try {
        foreach ($Key in $Values.Keys) { [Environment]::SetEnvironmentVariable($Key, [string]$Values[$Key], $Location) }
    } catch {
        foreach ($Key in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Key, $Previous[$Key], $Location) }
        throw
    }
    # Notification failure must not undo or misreport successfully saved values.
    try {
        if (-not ('FBVenoison.EnvironmentNotification' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace FBVenoison {
    public static class EnvironmentNotification {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr window, uint message,
            IntPtr wParam, string lParam, uint flags, uint timeout, out IntPtr result);
    }
}
'@
        }
        $Result = [IntPtr]::Zero
        [void][FBVenoison.EnvironmentNotification]::SendMessageTimeout(
            [IntPtr]0xffff, 0x1a, [IntPtr]::Zero, 'Environment', 2, 2000, [ref]$Result)
    } catch {
        Write-Warning "Application paths were saved, but Windows could not be notified. Restart the launching application or sign in again. $($_.Exception.Message)"
    }
}

if (-not $RegisterOnly) {
    # Keep build dependencies separate from the user's global Python installation.
    $BuildEnvironment = Join-Path $ProjectRoot '.venv-build'
    $script:BuildPython = Join-Path $BuildEnvironment 'Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
        Write-Host 'Creating the local Python build environment...'
        & $Python -m venv $BuildEnvironment
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment. Check -Python points to 64-bit Windows Python.' }
    }
    if (-not $SkipDependencyInstall) {
        Invoke-BuildPython -PythonArguments @('-m', 'pip', 'install', '-r', (Join-Path $ProjectRoot 'FBVenom/requirements-build.txt'))
    }
    # Python single quotes survive Windows PowerShell 5.1 native argument passing.
    Invoke-BuildPython -PythonArguments @('-c', "import sys, struct, PyInstaller, PyQt6, uiautomator2, adbutils, PIL; assert sys.platform == 'win32' and struct.calcsize('P') == 8, '64-bit Windows Python is required'")
    $Tools = Find-PlatformTools -Requested $PlatformTools
    # A supplied tools folder is checked for genuine Windows PE files by Venom's spec.
    $env:FBVENOM_PLATFORM_TOOLS = $Tools
    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    Invoke-AppBuild -Folder (Join-Path $ProjectRoot 'FBPoison') -Spec 'fbpoison.spec' -Destination (Join-Path $OutputDirectory 'FBPoison') -Name 'FBPoison'
    Invoke-AppBuild -Folder (Join-Path $ProjectRoot 'FBVenom') -Spec 'FBVenom.spec' -Destination (Join-Path $OutputDirectory 'FBVenom') -Name 'FBVenom'
    Invoke-AppBuild -Folder $ProjectRoot -Spec 'fbvenoison.spec' -Destination $OutputDirectory -Name 'FBVenoison'
    $SharedTools = Join-Path $OutputDirectory 'platform-tools'
    New-Item -ItemType Directory -Force -Path $SharedTools | Out-Null
    foreach ($Name in @('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt')) {
        $ToolFile = Join-Path $Tools $Name
        if (Test-Path -LiteralPath $ToolFile -PathType Leaf) {
            $Destination = Join-Path $SharedTools $Name
            if ([IO.Path]::GetFullPath($ToolFile) -ne [IO.Path]::GetFullPath($Destination)) {
                Copy-Item -LiteralPath $ToolFile -Destination $Destination -Force
            }
        }
    }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'update_config.example.json') -Destination $OutputDirectory -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'FBPoison/poison_private.example.json') -Destination (Join-Path $OutputDirectory 'FBPoison') -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'FBVenom/venom_ai.example.json') -Destination (Join-Path $OutputDirectory 'FBVenom') -Force
}

$Applications = @{
    FBVENOISON_EXE = Join-Path $OutputDirectory 'fbvenoison.exe'
    VENOM = Join-Path $OutputDirectory 'FBVenom/FBVenom.exe'
    POISON = Join-Path $OutputDirectory 'FBPoison/fbpoison.exe'
}
foreach ($Key in @('FBVENOISON_EXE', 'VENOM', 'POISON')) {
    if (-not (Test-Path -LiteralPath $Applications[$Key] -PathType Leaf)) {
        throw "Missing rebuilt application: $($Applications[$Key]). Existing system values were not changed."
    }
}
$MarkerPath = Join-Path $OutputDirectory 'FBVenom/FBVenom.suite-bridge.json'
$Marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
$VenomHash = (Get-FileHash -LiteralPath $Applications.VENOM -Algorithm SHA256).Hash
if ($Marker.app -ne 'FBVenom' -or $Marker.protocol -ne 1 -or $Marker.sha256 -ne $VenomHash) {
    throw 'FBVenom has no matching suite bridge. Rebuild it before registering application paths.'
}
$Applications['FBVENOISON_VENOM_EXE'] = $Applications.VENOM
$Applications['FBVENOISON_POISON_EXE'] = $Applications.POISON
$Applications | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $OutputDirectory 'application_paths.json')

if (-not $SkipRegistration) {
    $IsAdministrator = $false
    if ($Scope -eq 'Machine') {
        $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
        $IsAdministrator = $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    if ($Scope -eq 'Machine' -and -not $IsAdministrator) {
        Write-Host 'Windows administrator approval is needed to save system environment values.'
        # Elevate only registration; Python and build tools run without elevation.
        $FunctionText = ${function:Set-FBVenoisonEnvironment}.ToString()
        $Payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Applications | ConvertTo-Json -Compress)))
        $Registration = @"
`$ErrorActionPreference = 'Stop'
try {
    function Set-FBVenoisonEnvironment {
$FunctionText
    }
    `$Saved = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$Payload')) | ConvertFrom-Json
    `$Values = @{}
    foreach (`$Property in `$Saved.PSObject.Properties) { `$Values[`$Property.Name] = `$Property.Value }
    Set-FBVenoisonEnvironment -Values `$Values -Target 'Machine'
    exit 0
} catch { Write-Error `$_; exit 1 }
"@
        $EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Registration))
        $HostExecutable = (Get-Process -Id $PID).Path
        try {
            $Elevated = Start-Process -FilePath $HostExecutable -Verb RunAs -ArgumentList @('-NoProfile', '-EncodedCommand', $EncodedCommand) -Wait -PassThru
        } catch {
            throw "The builds completed, but administrator approval was not completed. Retry with -RegisterOnly, or use -Scope User. $($_.Exception.Message)"
        }
        if ($Elevated.ExitCode -ne 0) {
            throw 'The builds completed, but system registration failed. Retry with -RegisterOnly, or use -Scope User.'
        }
    } else {
        Set-FBVenoisonEnvironment -Values $Applications -Target $Scope
    }
    if ($Scope -eq 'Machine') {
        # User values override system values in fresh processes. Synchronize the
        # invoking user outside the elevated process, whose identity may differ.
        Set-FBVenoisonEnvironment -Values $Applications -Target 'User'
    }
    foreach ($Key in $Applications.Keys) {
        [Environment]::SetEnvironmentVariable($Key, [string]$Applications[$Key], [EnvironmentVariableTarget]::Process)
    }
    Write-Host "`nRegistered $Scope environment values:" -ForegroundColor Green
    $Applications.GetEnumerator() | Sort-Object Name | Format-Table Name, Value -AutoSize
}
Write-Host "`nReady: $($Applications.FBVENOISON_EXE)" -ForegroundColor Green
Write-Host 'Restart any already-open FBVenoison window to load the rebuilt apps.'
if ($Launch) {
    # Refresh the launch environment even when persistent registration was skipped.
    foreach ($Key in $Applications.Keys) {
        [Environment]::SetEnvironmentVariable($Key, [string]$Applications[$Key], [EnvironmentVariableTarget]::Process)
    }
    Start-Process -FilePath $Applications.FBVENOISON_EXE -WorkingDirectory $OutputDirectory
}
