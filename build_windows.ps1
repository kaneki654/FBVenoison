# Compatibility entry point. Rebuild and register all three applications.
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
& (Join-Path $PSScriptRoot 'rebuild_windows.ps1') @PSBoundParameters
