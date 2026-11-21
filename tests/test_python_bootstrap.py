"""Check Python-first setup without changing Windows installations or registry."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell')
HELPER = ROOT / 'tools/windows_python.ps1'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(PWSH, 'PowerShell unavailable; set PWSH for bootstrap tests')
class PythonBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='venoison python setup ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_ps(self, body, expected_code=0):
        script = self.directory / 'bootstrap-check.ps1'
        script.write_text("$ErrorActionPreference = 'Stop'\n$WarningPreference = 'SilentlyContinue'\n. "
                          + literal(HELPER) + '\n' + body, encoding='utf-8')
        result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive',
                                 '-File', str(script)], cwd=self.directory,
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=30)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        values = [line.removeprefix('RESULT=') for line in result.stdout.splitlines()
                  if line.startswith('RESULT=')]
        return json.loads(values[-1]) if values else None

    def test_helper_parses_and_probes_a_real_interpreter(self):
        result = self.run_ps(r'''
$Tokens = $null; $Errors = $null
[void][Management.Automation.Language.Parser]::ParseFile(''' + literal(HELPER) + r''', [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
$Info = Get-BuildPythonInfo -Executable ''' + literal(sys.executable) + r'''
Write-Output ('RESULT=' + ($Info | ConvertTo-Json -Compress))
''')
        self.assertEqual(result['major'], sys.version_info.major)
        self.assertEqual(result['minor'], sys.version_info.minor)
        self.assertEqual(result['bits'], struct.calcsize('P') * 8)
        self.assertEqual(result['platform'], sys.platform)
        self.assertTrue(Path(result['executable']).is_file())

    def test_supported_guard_rejects_wrong_version_architecture_and_runtime(self):
        result = self.run_ps(r'''
$Good = @{ platform='win32'; implementation='CPython'; major=3; minor=14; bits=64;
           sysconfig_platform='win-amd64'; free_threaded=$false }
$Results = @((Test-SupportedBuildPython -Info ([PSCustomObject]$Good)))
foreach ($Case in @(@{minor=15}, @{bits=32}, @{sysconfig_platform='win-arm64'},
                    @{free_threaded=$true}, @{platform='linux'}, @{implementation='PyPy'})) {
    $Changed = $Good.Clone()
    foreach ($Key in $Case.Keys) { $Changed[$Key] = $Case[$Key] }
    $Results += Test-SupportedBuildPython -Info ([PSCustomObject]$Changed)
}
$Results += Test-SupportedBuildPython -Info $null
Write-Output ('RESULT=' + (ConvertTo-Json -InputObject $Results -Compress))
''')
        self.assertEqual(result, [True] + [False] * 7)

    def test_update_happens_before_discovery_and_skip_is_explicit(self):
        result = self.run_ps(r'''
$script:Events = [Collections.Generic.List[string]]::new()
function Update-WindowsBuildPython { param($CacheDirectory) $script:Events.Add('update') }
function Find-SupportedWindowsBuildPython { param($ExplicitPython) $script:Events.Add('find:' + $ExplicitPython); return 'selected.exe' }
function Get-BuildPythonInfo { param($Executable)
    $script:Events.Add('probe:' + $Executable)
    return [PSCustomObject]@{ platform='win32'; implementation='CPython'; major=3; minor=14; bits=64;
        sysconfig_platform='win-amd64'; free_threaded=$false; version='3.14.10'; executable=$Executable }
}
$Selected = Initialize-WindowsBuildPython -ExplicitPython 'C:\Python With Spaces\python.exe' -CacheDirectory 'cache'
$First = @($script:Events.ToArray()); $script:Events.Clear()
$Skipped = Initialize-WindowsBuildPython -SkipPythonUpdate -CacheDirectory 'cache'
Write-Output ('RESULT=' + (@{ first=$First; skipped=@($script:Events.ToArray()); selected=$Selected; skip_selected=$Skipped } | ConvertTo-Json -Compress))
''')
        self.assertEqual(result['first'], ['update', 'find:C:\\Python With Spaces\\python.exe',
                                          'probe:selected.exe'])
        self.assertEqual(result['skipped'], ['find:', 'probe:selected.exe'])
        self.assertEqual(result['selected'], 'selected.exe')
        self.assertEqual(result['skip_selected'], 'selected.exe')

    def test_missing_updated_runtime_uses_official_fallback_except_explicit_or_skip(self):
        result = self.run_ps(r'''
$script:Events = @(); $script:Attempts = 0
function Update-WindowsBuildPython { param($CacheDirectory) $script:Events += 'update' }
function Install-OfficialBuildPython { param($CacheDirectory) $script:Events += 'official' }
function Find-SupportedWindowsBuildPython { param($ExplicitPython)
    $script:Events += 'find'; $script:Attempts += 1
    if ($script:Attempts -eq 1) { throw 'fixture missing interpreter' }
    return 'selected.exe'
}
function Get-BuildPythonInfo { param($Executable)
    $script:Events += 'probe'
    return [PSCustomObject]@{ platform='win32'; implementation='CPython'; major=3; minor=14; bits=64;
        sysconfig_platform='win-amd64'; free_threaded=$false; version='3.14.10'; executable=$Executable }
}
$Results = @()
foreach ($Case in @(@{}, @{SkipPythonUpdate=$true}, @{ExplicitPython='chosen.exe'})) {
    $script:Events = @(); $script:Attempts = 0; $Failed = $false
    try { $Selected = Initialize-WindowsBuildPython -CacheDirectory 'cache' @Case }
    catch { $Failed = $true }
    $Results += @{ failed=$Failed; events=$script:Events }
}
Write-Output ('RESULT=' + (ConvertTo-Json -InputObject $Results -Depth 5 -Compress))
''')
        self.assertEqual(result[0], {'failed': False, 'events': [
            'update', 'find', 'official', 'find', 'probe']})
        self.assertEqual(result[1], {'failed': True, 'events': ['find']})
        self.assertEqual(result[2], {'failed': True, 'events': ['update', 'find']})

    def test_explicit_incompatible_python_never_falls_back_silently(self):
        result = self.run_ps(r'''
function Get-BuildPythonInfo { param($Executable)
    return [PSCustomObject]@{ platform='win32'; implementation='CPython'; major=3; minor=15; bits=64;
        sysconfig_platform='win-amd64'; free_threaded=$false }
}
function Get-WindowsBuildPythonCandidates { throw 'A fallback must not be attempted.' }
try { Find-SupportedWindowsBuildPython -ExplicitPython 'chosen.exe'; throw 'Unsupported interpreter was accepted.' }
catch { $Message = $_.Exception.Message }
Write-Output ('RESULT=' + (@{ message=$Message } | ConvertTo-Json -Compress))
''')
        self.assertIn('regular 64-bit CPython 3.14', result['message'])
        self.assertNotIn('fallback must not', result['message'])

    def test_winget_installs_upgrades_and_falls_back_only_on_failure(self):
        result = self.run_ps(r'''
$script:Fallbacks = 0
$script:Calls = @()
function Get-Command { param($Name, $CommandType, $ErrorAction)
    return [PSCustomObject]@{Source='Invoke-FixtureWinget'}
}
function Invoke-FixtureWinget { $script:Calls += ,@($args); $global:LASTEXITCODE = $script:FixtureExit; 'fixture winget output' }
function Install-OfficialBuildPython { param($CacheDirectory) $script:Fallbacks += 1 }
foreach ($Code in @(0, -1978335189, 7)) {
    $script:FixtureExit = $Code
    Update-WindowsBuildPython -CacheDirectory 'fixture-cache'
}
Write-Output ('RESULT=' + (@{ calls=$script:Calls; fallbacks=$script:Fallbacks } | ConvertTo-Json -Depth 5 -Compress))
''')
        self.assertEqual(result['fallbacks'], 1)
        self.assertEqual(len(result['calls']), 3)
        for args in result['calls']:
            self.assertEqual(args[:3], ['install', '--id', 'Python.Python.3.14'])
            self.assertIn('--architecture', args)
            self.assertEqual(args[args.index('--architecture') + 1], 'x64')
            self.assertEqual(args[args.index('--scope') + 1], 'user')
            self.assertNotIn('--ignore-security-hash', args)

    def test_official_fallback_selects_latest_314_and_checks_signature_first(self):
        result = self.run_ps(r'''
$env:LOCALAPPDATA = ''' + literal(self.directory / 'local app data') + r'''
$script:Events = @()
function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile)
    if ($OutFile) { $script:Events += ('download:' + $Uri); [IO.File]::WriteAllText($OutFile, 'fixture'); return }
    return [PSCustomObject]@{Content='https://www.python.org/ftp/python/3.14.9/python-3.14.9-amd64.exe https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe https://www.python.org/ftp/python/3.15.2/python-3.15.2-amd64.exe'}
}
function Get-AuthenticodeSignature { param($LiteralPath)
    $script:Events += 'signature'
    return [PSCustomObject]@{Status='Valid'; SignerCertificate=[PSCustomObject]@{Subject='CN=Python Software Foundation'}}
}
function Start-Process { param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    $script:Events += 'install'; $script:InstallerArgs = $ArgumentList
    return [PSCustomObject]@{ExitCode=0}
}
Install-OfficialBuildPython -CacheDirectory ''' + literal(self.directory / 'cache with spaces') + r'''
Write-Output ('RESULT=' + (@{events=$script:Events; args=$script:InstallerArgs} | ConvertTo-Json -Depth 4 -Compress))
''')
        self.assertEqual(result['events'], [
            'download:https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe',
            'signature', 'install'])
        self.assertIn('InstallAllUsers=0', result['args'])
        self.assertIn('PrependPath=0', result['args'])
        self.assertTrue(any(arg.startswith('TargetDir="') and 'local app data' in arg
                            for arg in result['args']))

    def test_unverified_python_installer_is_never_started(self):
        result = self.run_ps(r'''
$env:LOCALAPPDATA = ''' + literal(self.directory / 'local app data') + r'''
$script:Installed = $false
function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile)
    if ($OutFile) { [IO.File]::WriteAllText($OutFile, 'fixture'); return }
    return [PSCustomObject]@{Content='https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe'}
}
function Get-AuthenticodeSignature { param($LiteralPath)
    return [PSCustomObject]@{Status='NotSigned'; SignerCertificate=$null}
}
function Start-Process { $script:Installed = $true }
try { Install-OfficialBuildPython -CacheDirectory ''' + literal(self.directory / 'cache') + r'''; throw 'Installer was incorrectly accepted.' }
catch { $Message = $_.Exception.Message }
Write-Output ('RESULT=' + (@{installed=$script:Installed; message=$Message} | ConvertTo-Json -Compress))
''')
        self.assertFalse(result['installed'])
        self.assertIn('signature could not be verified', result['message'])


if __name__ == '__main__':
    unittest.main()
