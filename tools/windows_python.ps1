# Python setup for the Windows build. Dot-source this file; it performs no work
# until Initialize-WindowsBuildPython is called.
$script:BuildPythonSeries = '3.14'

function Get-BuildPythonInfo {
    param([Parameter(Mandatory = $true)][string]$Executable, [string[]]$PrefixArguments = @())
    # Single quotes inside Python survive Windows PowerShell 5.1 argument passing.
    $Probe = "import json, platform, struct, sys, sysconfig; print(json.dumps({'executable': sys.executable, 'version': platform.python_version(), 'major': sys.version_info.major, 'minor': sys.version_info.minor, 'bits': struct.calcsize('P') * 8, 'platform': sys.platform, 'implementation': platform.python_implementation(), 'sysconfig_platform': sysconfig.get_platform(), 'free_threaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
    try {
        $Lines = @(& $Executable @PrefixArguments -I -c $Probe 2>$null)
        if ($LASTEXITCODE -ne 0 -or $Lines.Count -eq 0) { return $null }
        return ($Lines[-1] | ConvertFrom-Json -ErrorAction Stop)
    } catch { return $null }
}

function Test-SupportedBuildPython {
    param($Info)
    return ($null -ne $Info -and $Info.platform -eq 'win32' -and
        $Info.implementation -eq 'CPython' -and $Info.major -eq 3 -and
        $Info.minor -eq 14 -and $Info.bits -eq 64 -and
        $Info.sysconfig_platform -eq 'win-amd64' -and -not $Info.free_threaded)
}

function Get-WindowsBuildPythonCandidates {
    $Candidates = @()
    if ($env:LOCALAPPDATA) {
        $Candidates += Join-Path $env:LOCALAPPDATA 'Programs/Python/Python314/python.exe'
    }
    foreach ($RegistryPath in @(
        'HKCU:\Software\Python\PythonCore\3.14\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.14\InstallPath')) {
        $Key = Get-Item -LiteralPath $RegistryPath -ErrorAction SilentlyContinue
        if ($Key) {
            $Executable = $Key.GetValue('ExecutablePath')
            if ($Executable) { $Candidates += [string]$Executable }
            $Directory = $Key.GetValue('')
            if ($Directory) { $Candidates += Join-Path ([string]$Directory) 'python.exe' }
        }
    }
    foreach ($Name in @('python3.14.exe', 'python.exe', 'python')) {
        $Command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source -notmatch '[\\/]Microsoft[\\/]WindowsApps[\\/]python(?:3)?\.exe$') {
            $Candidates += $Command.Source
        }
    }
    return @($Candidates | Select-Object -Unique)
}

function Find-SupportedWindowsBuildPython {
    param([string]$ExplicitPython)
    if ($ExplicitPython) {
        $Info = Get-BuildPythonInfo -Executable $ExplicitPython
        if (-not (Test-SupportedBuildPython -Info $Info)) {
            throw "-Python must point to regular 64-bit CPython $script:BuildPythonSeries for Windows (not 32-bit, ARM64, free-threaded, or Python 3.15)."
        }
        return [string]$Info.executable
    }
    foreach ($Candidate in @(Get-WindowsBuildPythonCandidates)) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        $Info = Get-BuildPythonInfo -Executable $Candidate
        if (Test-SupportedBuildPython -Info $Info) { return [string]$Info.executable }
    }
    $Launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($Launcher) {
        $Info = Get-BuildPythonInfo -Executable $Launcher.Source -PrefixArguments @('-3.14')
        if (Test-SupportedBuildPython -Info $Info) { return [string]$Info.executable }
    }
    throw "A regular 64-bit Windows Python $script:BuildPythonSeries interpreter was not found. Install it or pass its full path with -Python."
}

function Install-OfficialBuildPython {
    param([Parameter(Mandatory = $true)][string]$CacheDirectory)
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required for the per-user Python installation.' }
    Write-Host "Installing/updating Python $script:BuildPythonSeries x64 from python.org..."
    $Page = Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/downloads/windows/'
    # Only stable 3.14.x AMD64 installers from the official HTTPS host qualify.
    $Pattern = 'https://www\.python\.org/ftp/python/(3\.14\.\d+)/python-\1-amd64\.exe'
    $Matches = [regex]::Matches([string]$Page.Content, $Pattern)
    $Release = $Matches | ForEach-Object {
        [PSCustomObject]@{ Version = [version]$_.Groups[1].Value; Url = $_.Value }
    } | Sort-Object Version -Descending | Select-Object -First 1
    if (-not $Release) { throw 'python.org did not provide a stable Python 3.14 x64 installer.' }
    New-Item -ItemType Directory -Force -Path $CacheDirectory | Out-Null
    $Installer = Join-Path $CacheDirectory ("python-$($Release.Version)-amd64.exe")
    Invoke-WebRequest -UseBasicParsing -Uri $Release.Url -OutFile $Installer
    $Signature = Get-AuthenticodeSignature -LiteralPath $Installer
    if ($Signature.Status -ne 'Valid' -or
            [string]$Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'The Python installer signature could not be verified. It was not executed.'
    }
    $InstallDirectory = Join-Path $env:LOCALAPPDATA 'Programs/Python/Python314'
    $InstallerLog = Join-Path $CacheDirectory 'python-install.log'
    $Arguments = @('/quiet', '/norestart', 'InstallAllUsers=0', 'Include_launcher=0',
        'Include_test=0', 'Include_pip=1', 'PrependPath=0',
        ('TargetDir="' + $InstallDirectory + '"'), ('/log "' + $InstallerLog + '"'))
    $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru
    if ($Process.ExitCode -notin @(0, 3010)) {
        throw "Python installation failed (exit $($Process.ExitCode)). See $InstallerLog"
    }
    if ($Process.ExitCode -eq 3010) { Write-Warning 'Python was updated; Windows also requested a restart.' }
}

function Update-WindowsBuildPython {
    param([Parameter(Mandatory = $true)][string]$CacheDirectory)
    $Winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($Winget) {
        Write-Host "Installing/updating regular Python $script:BuildPythonSeries x64 with Windows Package Manager..."
        # `install` upgrades an existing matching package unless --no-upgrade is used.
        # WinGet uses a nonzero status for "already current"; inspect it here
        # even when the invoking PowerShell 7 profile treats native errors as terminating.
        $PSNativeCommandUseErrorActionPreference = $false
        try {
            & $Winget.Source install --id Python.Python.3.14 --exact --source winget --architecture x64 --scope user --silent --disable-interactivity --accept-source-agreements --accept-package-agreements | ForEach-Object { Write-Host $_ }
            $Result = $LASTEXITCODE
        } catch {
            $Result = $null
            Write-Warning "Windows Package Manager could not start. $($_.Exception.Message)"
        }
        # 0x8A15002B means the installed package already has no applicable upgrade.
        if ($Result -eq 0 -or $Result -eq -1978335189) { return }
        Write-Warning "Windows Package Manager could not update Python (exit $Result). Trying the official Python installer."
    }
    Install-OfficialBuildPython -CacheDirectory $CacheDirectory
}

function Initialize-WindowsBuildPython {
    param([string]$ExplicitPython, [switch]$SkipPythonUpdate,
          [Parameter(Mandatory = $true)][string]$CacheDirectory)
    if (-not $SkipPythonUpdate) { Update-WindowsBuildPython -CacheDirectory $CacheDirectory }
    try {
        $Selected = Find-SupportedWindowsBuildPython -ExplicitPython $ExplicitPython
    } catch {
        if ($SkipPythonUpdate -or $ExplicitPython) { throw }
        # WinGet may report no upgrade while only a different architecture is
        # registered. Install the official x64 runtime, then discover it again.
        Write-Warning 'The updated Python x64 interpreter was not found. Trying the official installer.'
        Install-OfficialBuildPython -CacheDirectory $CacheDirectory
        $Selected = Find-SupportedWindowsBuildPython
    }
    $Info = Get-BuildPythonInfo -Executable $Selected
    if (-not (Test-SupportedBuildPython -Info $Info)) { throw 'The selected Python became unavailable or incompatible before building.' }
    Write-Host "Build Python: $($Info.version), regular x64 ($Selected)" -ForegroundColor Cyan
    return $Selected
}
