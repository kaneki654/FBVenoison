"""Exercise the authored PowerShell build without installers, registry, or ADB.

The native shim is a Unix test fixture. Real Windows wheel resolution is tested
separately; these tests verify ordering, failure handling, and command arguments.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell')

MOCK_PYTHON = r'''
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
role = 'base' if Path(__file__).name == 'base-python.exe' else 'venv'
mode = os.environ.get('BUILD_MOCK_MODE', '')
with open(os.environ['BUILD_MOCK_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps({'args': args, 'cwd': os.getcwd(), 'role': role}) + '\n')
if '-c' in args:
    code = args[args.index('-c') + 1]
    if 'json.dumps' in code:
        version = '3.14.10'
        version_file = Path(__file__).parent.parent / 'fixture-version.json'
        if role == 'venv' and version_file.exists():
            version = json.loads(version_file.read_text())['version']
        major, minor, *_ = map(int, version.split('.'))
        info = {'executable': str(Path(__file__).resolve()), 'version': version,
                'major': major, 'minor': minor, 'bits': 64, 'platform': 'win32',
                'implementation': 'CPython', 'sysconfig_platform': 'win-amd64',
                'free_threaded': False}
        if role == 'base':
            if mode == 'base_315': info.update(version='3.15.2', minor=15)
            if mode == 'base_32': info['bits'] = 32
            if mode == 'base_arm': info['sysconfig_platform'] = 'win-arm64'
            if mode == 'base_free_threaded': info['free_threaded'] = True
        print(json.dumps(info))
    else:
        assert 'import PyInstaller' in code, 'Unexpected Python preflight'
    sys.exit(0)
if '-m' in args:
    module = args[args.index('-m') + 1]
    if module == 'venv':
        assert role == 'base'
        root = Path(args[-1]); executable = root / 'Scripts/python.exe'
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(__file__, executable); executable.chmod(0o755)
        (root / 'fixture-version.json').write_text(json.dumps({'version': '3.14.10'}))
        sys.exit(0)
    if module == 'pip':
        action = next(value for value in args if value in ('install', 'download', 'check'))
        stage = 'tools' if action == 'install' and 'pip' in args[args.index('install') + 1:] else action
        if mode == 'pip_' + stage + '_failure': sys.exit(9)
        if action == 'download':
            Path(args[args.index('--dest') + 1]).mkdir(parents=True, exist_ok=True)
        sys.exit(0)
    if module == 'PyInstaller':
        name = {'fbpoison.spec': 'fbpoison', 'FBVenom-suite.spec': 'FBVenom',
                'fbvenoison.spec': 'fbvenoison'}[Path(args[-1]).name]
        if mode == 'failed_build' and name == 'FBVenom': sys.exit(7)
        destination = Path(args[args.index('--distpath') + 1])
        destination.mkdir(parents=True, exist_ok=True)
        if mode != 'missing_hub' or name != 'fbvenoison':
            binary = destination / (name + '.exe')
            binary.write_bytes(b'MZ-mock-completed-build-' + name.encode())
            if name == 'FBVenom':
                digest = hashlib.sha256(binary.read_bytes()).hexdigest()
                (destination / 'FBVenom.suite-bridge.json').write_text(json.dumps({
                    'app': 'FBVenom', 'protocol': 1,
                    'sha256': '0' * 64 if mode == 'bad_bridge' else digest}))
        sys.exit(0)
if any(Path(value).name == 'tools_prepare_usb_internet.py' for value in args):
    output = Path(args[args.index('--out') + 1]); output.mkdir(parents=True, exist_ok=True)
    (output / 'gnirehtet.exe').write_bytes(b'MZ-fixture')
    (output / 'gnirehtet.apk').write_bytes(b'fixture')
    sys.exit(0)
sys.exit('Unexpected build command: ' + repr(args))
'''


@unittest.skipUnless(PWSH, 'PowerShell unavailable; set PWSH for script validation')
class RebuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='venoison rebuild ')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project with spaces'
        self.project.mkdir()
        for name in ('rebuild_windows.ps1', 'build_windows.ps1', 'tools/windows_python.ps1'):
            destination = self.project / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        for folder in ('FBVenom', 'FBPoison'):
            (self.project / folder).mkdir()
        for name in ('update_config.example.json', 'FBPoison/poison_private.example.json',
                     'FBVenom/venom_ai.example.json'):
            (self.project / name).write_text('{}')
        (self.project / 'FBVenom/requirements-build.txt').write_text('# fixture\n')
        self.output = self.project / 'release with spaces'
        self.tools = self.project / 'android tools'
        self.tools.mkdir()
        for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt'):
            (self.tools / name).write_bytes(b'MZ-offline-fixture')
        self.log = self.project / 'build-arguments.jsonl'
        self.environment = dict(os.environ, OS='Windows_NT', BUILD_MOCK_LOG=str(self.log))
        for key in ('BUILD_MOCK_MODE', 'FBVENOM_PLATFORM_TOOLS', 'FBVENOISON_EXE',
                    'FBVENOISON_VENOM_EXE', 'FBVENOISON_POISON_EXE', 'VENOM', 'POISON'):
            self.environment.pop(key, None)
        self.base = self.project / 'base-python.exe'
        self.builder = self.project / '.venv-build/Scripts/python.exe'
        if os.name != 'nt':
            for executable in (self.base, self.builder):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text('#!' + sys.executable + '\n' + MOCK_PYTHON)
                executable.chmod(0o755)

    def powershell(self, script, *arguments, environment=None):
        return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
                               str(script), *map(str, arguments)], cwd=self.project,
                              env=environment or self.environment, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=40)

    def build(self, mode='', *, entry='rebuild_windows.ps1', register_only=False,
              tools=None, environment=None, skip_dependencies=True):
        values = dict(environment or self.environment, BUILD_MOCK_MODE=mode)
        arguments = ['-SkipRegistration', '-SkipPythonUpdate', '-Python', self.base,
                     '-OutputDirectory', self.output]
        if skip_dependencies:
            arguments.append('-SkipDependencyInstall')
        if register_only:
            arguments.append('-RegisterOnly')
        else:
            arguments += ['-PlatformTools', tools or self.tools]
        return self.powershell(self.project / entry, *arguments, environment=values)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @staticmethod
    def module(call):
        args = call['args']
        return args[args.index('-m') + 1] if '-m' in args else None

    def native_fixture(self):
        if os.name == 'nt':
            self.skipTest('Unix-only native shim; real Windows dependency tests run separately')

    def test_scripts_and_generated_elevated_command_parse(self):
        parser = self.project / 'parse-test.ps1'
        parser.write_text(r'''
$ErrorActionPreference = 'Stop'
foreach ($Name in @('rebuild_windows.ps1', 'build_windows.ps1', 'tools/windows_python.ps1')) {
    $Tokens = $null; $Errors = $null
    $Ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $Name), [ref]$Tokens, [ref]$Errors)
    if ($Errors.Count) { throw ($Errors | Out-String) }
    if ($Name -eq 'rebuild_windows.ps1') { $RebuildAst = $Ast }
}
$Definition = $RebuildAst.Find({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq 'Set-FBVenoisonEnvironment'
}, $true)
$Body = $Definition.Body.Extent.Text
$FunctionText = $Body.Substring(1, $Body.Length - 2)
$Payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"VENOM":"C:\\Program Files\\Venom.exe"}'))
$Assignment = $RebuildAst.Find({ param($Node)
    $Node -is [Management.Automation.Language.AssignmentStatementAst] -and $Node.Left.Extent.Text -eq '$Registration'
}, $true)
Invoke-Expression ('$Registration = ' + $Assignment.Right.Extent.Text)
$Tokens = $null; $Errors = $null
[void][Management.Automation.Language.Parser]::ParseInput($Registration, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
Write-Output 'All PowerShell scripts parsed.'
''')
        result = self.powershell(parser)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('All PowerShell scripts parsed.', result.stdout)

    def test_wrapper_builds_all_apps_with_exact_paths_and_external_venom_spec(self):
        self.native_fixture()
        result = self.build(entry='build_windows.ps1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        builds = [call for call in self.calls() if self.module(call) == 'PyInstaller']
        self.assertEqual([Path(call['args'][-1]).name for call in builds],
                         ['fbpoison.spec', 'FBVenom-suite.spec', 'fbvenoison.spec'])
        self.assertEqual(Path(builds[1]['args'][-1]), self.project / 'FBVenom-suite.spec')
        self.assertEqual([Path(call['cwd']) for call in builds],
                         [self.project / 'FBPoison', self.project / 'FBVenom', self.project])
        paths = json.loads((self.output / 'application_paths.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(paths['VENOM'], str(self.output / 'FBVenom/FBVenom.exe'))
        self.assertEqual(paths['POISON'], str(self.output / 'FBPoison/fbpoison.exe'))
        self.assertEqual(paths['FBVENOISON_VENOM_EXE'], paths['VENOM'])
        self.assertEqual(paths['FBVENOISON_POISON_EXE'], paths['POISON'])
        self.assertEqual(paths['FBVENOISON_EXE'], str(self.output / 'fbvenoison.exe'))
        self.assertEqual((self.output / 'platform-tools/adb.exe').read_bytes(),
                         (self.tools / 'adb.exe').read_bytes())
        self.assertTrue(all('--clean' in call['args'] for call in builds))
        self.assertNotIn('Registered Machine environment', result.stdout)
        pip_calls = [call for call in self.calls() if self.module(call) == 'pip']
        self.assertEqual(len(pip_calls), 1)
        self.assertIn('check', pip_calls[0]['args'])
        usb = next(call for call in self.calls() if any(Path(value).name == 'tools_prepare_usb_internet.py' for value in call['args']))
        self.assertIn('--platform', usb['args'])
        self.assertEqual(usb['args'][usb['args'].index('--platform') + 1], 'windows')

    def test_failures_never_publish_application_path_values(self):
        self.native_fixture()
        for mode, expected in (('failed_build', 'Python command failed'),
                               ('missing_hub', 'Missing rebuilt application'),
                               ('bad_bridge', 'no matching suite bridge')):
            with self.subTest(mode=mode):
                shutil.rmtree(self.output, ignore_errors=True)
                result = self.build(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertFalse((self.output / 'application_paths.json').exists())

    def test_register_only_skips_every_python_step_and_rejects_replaced_binary(self):
        self.native_fixture()
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        before = self.log.read_bytes()
        registered = self.build(register_only=True)
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        self.assertEqual(self.log.read_bytes(), before)
        record = (self.output / 'application_paths.json').read_bytes()
        with (self.output / 'FBVenom/FBVenom.exe').open('ab') as binary:
            binary.write(b'replacement')
        rejected = self.build(register_only=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('no matching suite bridge', rejected.stdout + rejected.stderr)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual((self.output / 'application_paths.json').read_bytes(), record)

    def test_explicit_invalid_tools_path_does_not_fall_back_to_environment(self):
        self.native_fixture()
        environment = dict(self.environment, FBVENOM_PLATFORM_TOOLS=str(self.tools))
        result = self.build(tools=self.project / 'missing requested tools', environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('platform-tools are incomplete', result.stdout + result.stderr)
        self.assertFalse(any(self.module(call) == 'PyInstaller' for call in self.calls()))
        self.assertFalse((self.output / 'application_paths.json').exists())

    def test_dependencies_upgrade_tools_download_wheels_install_offline_then_check(self):
        self.native_fixture()
        result = self.build(skip_dependencies=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        pip_calls = [call for call in calls if self.module(call) == 'pip']
        actions = [next(value for value in call['args'] if value in ('install', 'download', 'check'))
                   for call in pip_calls]
        self.assertEqual(actions, ['install', 'download', 'install', 'check'])
        self.assertIn('--upgrade', pip_calls[0]['args'])
        self.assertEqual(pip_calls[0]['args'][-3:], ['pip', 'setuptools', 'wheel'])
        for call in pip_calls[:3]:
            self.assertIn('--only-binary=:all:', call['args'])
            self.assertIn('--isolated', call['args'])
            self.assertEqual(call['args'][0], '-I')
        self.assertIn('--no-index', pip_calls[2]['args'])
        self.assertIn('--find-links', pip_calls[2]['args'])
        first_pip = calls.index(pip_calls[0])
        self.assertTrue(any(call['role'] == 'base' and '-c' in call['args'] for call in calls[:first_pip]))
        self.assertTrue(any(call['role'] == 'venv' and '-c' in call['args'] for call in calls[:first_pip]))
        self.assertLess(calls.index(pip_calls[-1]), next(i for i, call in enumerate(calls) if self.module(call) == 'PyInstaller'))

    def test_pip_failure_stops_before_helpers_builds_and_registration(self):
        self.native_fixture()
        for mode in ('pip_tools_failure', 'pip_download_failure', 'pip_install_failure', 'pip_check_failure'):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                result = self.build(mode, skip_dependencies=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Python command failed', result.stdout + result.stderr)
                self.assertIn('installation logs', result.stdout + result.stderr)
                self.assertFalse(any(self.module(call) == 'PyInstaller' for call in self.calls()))
                self.assertFalse(any(any(Path(value).name == 'tools_prepare_usb_internet.py'
                                         for value in call['args']) for call in self.calls()))
                self.assertFalse((self.output / 'application_paths.json').exists())

    def test_stale_python_315_venv_is_preserved_and_recreated_before_pip(self):
        self.native_fixture()
        environment = self.builder.parent.parent
        (environment / 'fixture-version.json').write_text(json.dumps({'version': '3.15.2'}))
        (environment / 'keep-me.txt').write_text('preserve old environment')
        skipped = self.build()
        self.assertNotEqual(skipped.returncode, 0)
        self.assertIn('needs repair', skipped.stdout + skipped.stderr)
        self.assertFalse(any(self.module(call) == 'pip' for call in self.calls()))
        self.log.unlink()
        repaired = self.build(skip_dependencies=False)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        backups = list((self.project / '.build-tools').glob('old-venv-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'keep-me.txt').read_text(), 'preserve old environment')
        self.assertEqual(json.loads((environment / 'fixture-version.json').read_text())['version'], '3.14.10')
        calls = self.calls()
        created = next(i for i, call in enumerate(calls) if self.module(call) == 'venv')
        first_pip = next(i for i, call in enumerate(calls) if self.module(call) == 'pip')
        self.assertLess(created, first_pip)
        self.assertEqual(calls[created]['role'], 'base')

    def test_incompatible_explicit_base_is_rejected_before_any_pip_or_venv_work(self):
        self.native_fixture()
        for mode in ('base_315', 'base_32', 'base_arm', 'base_free_threaded'):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                result = self.build(mode, skip_dependencies=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('regular 64-bit CPython 3.14', result.stdout + result.stderr)
                self.assertTrue(all('-c' in call['args'] and call['role'] == 'base' for call in self.calls()))
                self.assertFalse((self.output / 'application_paths.json').exists())


if __name__ == '__main__':
    unittest.main()
