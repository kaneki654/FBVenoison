"""Parse PowerShell and exercise rebuild orchestration without real Windows builds.

Set PWSH to a portable pwsh executable when it is not on PATH. Native build
fixtures run on Unix; syntax checks also work on Windows. Registry writes and
real PyInstaller, network, ADB, and application launches are never invoked.
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
PWSH = os.environ.get("PWSH") or shutil.which("pwsh") or shutil.which("powershell")

MOCK_PYTHON = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["BUILD_MOCK_LOG"], "a", encoding="utf-8") as output:
    output.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
if args[:1] == ["-c"]:
    assert len(args) == 2 and "sys.platform == 'win32'" in args[1]
    sys.exit(0)
if args[:2] != ["-m", "PyInstaller"]:
    sys.exit("Unexpected dependency install or build command")
name = {"fbpoison.spec": "fbpoison", "FBVenom.spec": "FBVenom",
        "fbvenoison.spec": "fbvenoison"}[args[-1]]
mode = os.environ.get("BUILD_MOCK_MODE", "")
if mode == "failed_build" and name == "FBVenom":
    sys.exit(7)
destination = Path(args[args.index("--distpath") + 1])
destination.mkdir(parents=True, exist_ok=True)
if mode != "missing_hub" or name != "fbvenoison":
    binary = destination / (name + ".exe")
    binary.write_bytes(b"MZ-mock-completed-build-" + name.encode())
    if name == "FBVenom":
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        marker = {"app": "FBVenom", "protocol": 1,
                  "sha256": "0" * 64 if mode == "bad_bridge" else digest}
        (destination / "FBVenom.suite-bridge.json").write_text(json.dumps(marker))
'''


@unittest.skipUnless(PWSH, "PowerShell is unavailable; set PWSH to run script validation")
class RebuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="venoison rebuild ")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project with spaces"
        self.project.mkdir()
        for name in ("rebuild_windows.ps1", "build_windows.ps1"):
            shutil.copy2(ROOT / name, self.project / name)
        for folder in ("FBVenom", "FBPoison"):
            (self.project / folder).mkdir()
        for name in ("update_config.example.json", "FBPoison/poison_private.example.json",
                     "FBVenom/venom_ai.example.json"):
            (self.project / name).write_text("{}", encoding="utf-8")
        (self.project / "FBVenom/requirements-build.txt").write_text("# fixture\n")
        self.output = self.project / "release with spaces"
        self.tools = self.project / "android tools"
        self.tools.mkdir()
        for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt"):
            (self.tools / name).write_bytes(b"MZ-offline-fixture")
        self.log = self.project / "build-arguments.jsonl"
        self.environment = dict(os.environ, OS="Windows_NT", BUILD_MOCK_LOG=str(self.log))
        for key in ("BUILD_MOCK_MODE", "FBVENOM_PLATFORM_TOOLS", "FBVENOISON_EXE",
                    "FBVENOISON_VENOM_EXE", "FBVENOISON_POISON_EXE", "VENOM", "POISON"):
            self.environment.pop(key, None)
        if os.name != "nt":
            builder = self.project / ".venv-build/Scripts/python.exe"
            builder.parent.mkdir(parents=True)
            builder.write_text("#!" + sys.executable + "\n" + MOCK_PYTHON, encoding="utf-8")
            builder.chmod(0o755)

    def powershell(self, script, *arguments, environment=None):
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                               str(script), *map(str, arguments)], cwd=self.project,
                              env=environment or self.environment, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=40)

    def build(self, mode="", *, entry="rebuild_windows.ps1", register_only=False,
              tools=None, environment=None):
        values = dict(environment or self.environment, BUILD_MOCK_MODE=mode)
        arguments = ["-SkipRegistration", "-SkipDependencyInstall", "-OutputDirectory", self.output]
        if register_only:
            arguments.append("-RegisterOnly")
        else:
            arguments += ["-PlatformTools", tools or self.tools]
        return self.powershell(self.project / entry, *arguments, environment=values)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def require_unix_native_fixture(self):
        if os.name == "nt":
            self.skipTest("Unix-only fake native executable; no actual Windows build is performed")

    def test_scripts_and_generated_elevated_command_parse(self):
        parser = self.project / "parse-test.ps1"
        parser.write_text(r'''
$ErrorActionPreference = 'Stop'
foreach ($Name in @('rebuild_windows.ps1', 'build_windows.ps1')) {
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
# Evaluate only the authored here-string, never the registration command itself.
Invoke-Expression ('$Registration = ' + $Assignment.Right.Extent.Text)
$Tokens = $null; $Errors = $null
[void][Management.Automation.Language.Parser]::ParseInput($Registration, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
Write-Output 'All three PowerShell scripts parsed.'
''', encoding="utf-8")
        result = self.powershell(parser)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("All three PowerShell scripts parsed.", result.stdout)

    def test_wrapper_builds_all_apps_and_records_exact_paths_with_spaces(self):
        self.require_unix_native_fixture()
        result = self.build(entry="build_windows.ps1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        builds = [call for call in calls if call["args"][:2] == ["-m", "PyInstaller"]]
        self.assertEqual([call["args"][-1] for call in builds],
                         ["fbpoison.spec", "FBVenom.spec", "fbvenoison.spec"])
        self.assertEqual([Path(call["cwd"]) for call in builds],
                         [self.project / "FBPoison", self.project / "FBVenom", self.project])
        paths = json.loads((self.output / "application_paths.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(paths["VENOM"], str(self.output / "FBVenom/FBVenom.exe"))
        self.assertEqual(paths["POISON"], str(self.output / "FBPoison/fbpoison.exe"))
        self.assertEqual(paths["FBVENOISON_VENOM_EXE"], paths["VENOM"])
        self.assertEqual(paths["FBVENOISON_POISON_EXE"], paths["POISON"])
        self.assertEqual(paths["FBVENOISON_EXE"], str(self.output / "fbvenoison.exe"))
        self.assertEqual((self.output / "platform-tools/adb.exe").read_bytes(),
                         (self.tools / "adb.exe").read_bytes())
        self.assertTrue(all("--clean" in call["args"] for call in builds))
        self.assertNotIn("Registered Machine environment", result.stdout)

    def test_failures_never_publish_application_path_values(self):
        self.require_unix_native_fixture()
        for mode, expected in (("failed_build", "Python command failed"),
                               ("missing_hub", "Missing rebuilt application"),
                               ("bad_bridge", "no matching suite bridge")):
            with self.subTest(mode=mode):
                shutil.rmtree(self.output, ignore_errors=True)
                result = self.build(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertFalse((self.output / "application_paths.json").exists())

    def test_register_only_skips_build_and_rejects_replaced_binary(self):
        self.require_unix_native_fixture()
        built = self.build()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        before = self.log.read_bytes()
        registered = self.build(register_only=True)
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        self.assertEqual(self.log.read_bytes(), before)
        record = (self.output / "application_paths.json").read_bytes()
        with (self.output / "FBVenom/FBVenom.exe").open("ab") as binary:
            binary.write(b"replacement")
        rejected = self.build(register_only=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("no matching suite bridge", rejected.stdout + rejected.stderr)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual((self.output / "application_paths.json").read_bytes(), record)

    def test_explicit_invalid_tools_path_does_not_use_valid_environment_fallback(self):
        self.require_unix_native_fixture()
        environment = dict(self.environment, FBVENOM_PLATFORM_TOOLS=str(self.tools))
        result = self.build(tools=self.project / "missing requested tools", environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform-tools are incomplete", result.stdout + result.stderr)
        self.assertFalse(any(call["args"][:2] == ["-m", "PyInstaller"] for call in self.calls()))
        self.assertFalse((self.output / "application_paths.json").exists())


if __name__ == "__main__":
    unittest.main()
