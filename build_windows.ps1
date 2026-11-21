# Compatibility entry point. Rebuild and register all three applications.
[CmdletBinding()]
param(
    [string]$Python,
    [string]$OutputDirectory,
    [string]$PlatformTools,
    [ValidateSet('Machine', 'User')][string]$Scope = 'Machine',
    [switch]$SkipDependencyInstall,
    [switch]$SkipPythonUpdate,
    [switch]$SkipRegistration,
    [switch]$RegisterOnly,
    [switch]$Launch
)
& (Join-Path $PSScriptRoot 'rebuild_windows.ps1') @PSBoundParameters
