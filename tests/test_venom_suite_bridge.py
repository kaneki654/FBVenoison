"""Route an unchanged upstream FBVenom through the external suite adapter."""
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import textwrap
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
VENOM = ROOT / "FBVenom"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_file("_tested_venom_bridge", ROOT / "venom_bridge.py")


class SuiteBridgeTests(unittest.TestCase):
    def setUp(self):
        self.core = ModuleType("core")
        self.core.ADB = "bundled-adb"
        self.devices = {"mine": "device", "theirs": "device",
                        "192.168.2.15:5555": "offline"}
        self.core.list_devices = lambda: dict(self.devices)
        self.original_list = self.core.list_devices
        self.auth = ModuleType("auth")
        self.auth.require_key = lambda: False
        self.original_gate = self.auth.require_key
        self.usb = ModuleType("usb_internet")

        class Manager:
            def _command(self, arguments, timeout=8):
                return 0, "", ""
        self.usb.USBInternetManager = Manager
        self.original_command = Manager._command
        self.updater = ModuleType("updater")
        self.updater.install = Mock()
        self.updater.downgrade = Mock()
        self.modules = self.enterContext(patch.dict(sys.modules, {
            "core": self.core, "auth": self.auth, "usb_internet": self.usb,
            "updater": self.updater, "venom_bridge": bridge}))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_standalone_keeps_discovery_key_gate_and_usb_functions(self):
        self.assertFalse(bridge.apply())
        self.assertIs(self.core.list_devices, self.original_list)
        self.assertIs(self.auth.require_key, self.original_gate)
        self.assertIs(self.usb.USBInternetManager._command, self.original_command)
        self.assertEqual(self.core.list_devices(), self.devices)

    def test_snapshot_preserves_states_and_awkward_serials(self):
        os.environ["FBVENOISON_DEVICES"] = "mine,192.168.2.15:5555"
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {
            "mine": "device", "192.168.2.15:5555": "offline"})
        self.assertIs(self.auth.require_key, self.original_gate)

    def test_explicit_empty_snapshot_denies_all(self):
        os.environ["FBVENOISON_DEVICES"] = ""
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {})

    def test_reapply_updates_snapshot_without_stacking_filter_layers(self):
        os.environ["FBVENOISON_DEVICES"] = "mine"
        bridge.apply()
        os.environ["FBVENOISON_DEVICES"] = "theirs"
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {"theirs": "device"})
        self.assertIs(self.core.list_devices.__wrapped__, self.original_list)
        self.assertIs(self.usb.USBInternetManager._command.__wrapped__, self.original_command)

    def test_hub_auth_shared_adb_and_unchanged_updater(self):
        install, downgrade = self.updater.install, self.updater.downgrade
        os.environ.update(FBVENOISON_DEVICES="mine", FBVENOISON_AUTHED="1",
                          ADB_PATH="/suite/shared-adb")
        bridge.apply()
        self.assertTrue(self.auth.require_key())
        self.assertEqual(self.core.ADB, "/suite/shared-adb")
        self.assertIs(self.updater.install, install)
        self.assertIs(self.updater.downgrade, downgrade)

    def test_external_runtime_hook_applies_before_upstream_entry(self):
        os.environ.update(FBVENOISON_DEVICES="mine", FBVENOISON_AUTHED="1")
        runpy.run_path(str(ROOT / "integration/venom_runtime_hook.py"), run_name="_hook_probe")
        self.assertEqual(self.core.list_devices(), {"mine": "device"})
        self.assertTrue(self.auth.require_key())

    def test_frozen_auth_is_deferred_until_normal_module_import_and_finder_is_removed(self):
        calls = []

        class Loader:
            resource_marker = "delegated-loader"

            def create_module(self, spec):
                calls.append("create")
                return None

            def exec_module(self, module):
                calls.append("execute-upstream-auth")
                module.require_key = lambda: False

        class Finder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "auth":
                    return importlib.machinery.ModuleSpec(fullname, Loader())
                return None

        os.environ["FBVENOISON_AUTHED"] = "1"
        gui_before = sys.modules.get("gui")
        with patch.object(sys, "meta_path", [Finder(), *sys.meta_path]), patch.dict(sys.modules):
            sys.modules.pop("auth", None)
            bridge.apply(defer_auth=True)
            bridge.apply(defer_auth=True)
            pending = [finder for finder in sys.meta_path
                       if getattr(finder, "_fbvenoison_auth_gate", False)]
            self.assertEqual(len(pending), 1)
            self.assertNotIn("auth", sys.modules)
            self.assertIs(sys.modules.get("gui"), gui_before)
            self.assertEqual(calls, [])
            self.assertIsNone(pending[0].find_spec("unrelated_module"))
            self.assertIn(pending[0], sys.meta_path)
            auth = importlib.import_module("auth")
            self.assertTrue(auth.require_key())
            self.assertEqual(calls, ["create", "execute-upstream-auth"])
            self.assertEqual(auth.__loader__.resource_marker, "delegated-loader")
            self.assertNotIn(pending[0], sys.meta_path)

    def test_deferred_gate_does_not_unlock_if_launch_authorization_is_revoked(self):
        class Loader:
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                module.require_key = lambda: False

        class Finder:
            def find_spec(self, fullname, path=None, target=None):
                return importlib.machinery.ModuleSpec(fullname, Loader()) if fullname == "auth" else None

        os.environ["FBVENOISON_AUTHED"] = "1"
        with patch.object(sys, "meta_path", [Finder(), *sys.meta_path]), patch.dict(sys.modules):
            sys.modules.pop("auth", None)
            bridge.apply(defer_auth=True)
            os.environ["FBVENOISON_AUTHED"] = "0"
            auth = importlib.import_module("auth")
            self.assertFalse(auth.require_key())
            self.assertFalse(any(getattr(finder, "_fbvenoison_auth_gate", False)
                                 for finder in sys.meta_path))

    def test_real_upstream_usb_discovery_and_start_receive_only_assigned_phones(self):
        usb = load_file("_tested_upstream_usb", VENOM / "usb_internet.py")
        assigned = ["R58M%04d" % n for n in range(20)]
        full_fleet = ["R58M%04d" % n for n in range(40)]
        calls = []

        def run(arguments, **kwargs):
            calls.append(list(arguments))
            if arguments[1:3] == ["devices", "-l"]:
                return 0, "List of devices attached\n" + "".join(
                    serial + "\tdevice usb:1-1.1 model:Pixel\n" for serial in full_fleet), ""
            return 0, "allowed-result", ""

        os.environ["FBVENOISON_DEVICES"] = ",".join(assigned)
        with patch.dict(sys.modules, {"usb_internet": usb}), tempfile.TemporaryDirectory() as folder:
            bridge.apply()
            with patch.object(usb, "_read_adb_proto", return_value=[]):
                manager = usb.USBInternetManager(
                    "/fake/adb", "/fake/gnirehtet", "/fake/gnirehtet.apk",
                    log_dir=folder, run=run,
                    network_check=lambda: {"ok": True, "kind": "wifi"})
                try:
                    self.assertEqual([row["serial"] for row in manager.discover_usb()], assigned)
                    with patch.object(manager, "_verify_helpers"), \
                            patch.object(manager, "_start_relay"), \
                            patch.object(manager, "_start_device") as start:
                        manager.start_all()
                    self.assertEqual(sorted(call.args[0]["serial"] for call in start.call_args_list), assigned)
                    self.assertEqual(sorted(row["serial"] for row in manager.snapshot()["devices"]), assigned)
                    for program, arguments in (
                        (manager.adb_path, ["-s", full_fleet[-1], "shell", "getprop"]),
                        (manager.relay_path, ["start", full_fleet[-1]]),
                        (manager.relay_path, ["stop", full_fleet[-1]]),
                        (manager.relay_path, ["install", full_fleet[-1]]),
                        (manager.relay_path, ["tunnel", full_fleet[-1]]),
                    ):
                        before = len(calls)
                        code, _, error = manager._command([program, *arguments])
                        self.assertEqual(code, 1)
                        self.assertIn("not assigned", error)
                        self.assertEqual(len(calls), before)
                    self.assertEqual(manager._command([manager.adb_path, "-s", assigned[0], "get-state"]),
                                     (0, "allowed-result", ""))
                    self.assertEqual(manager._command([manager.relay_path, "start", assigned[0]]),
                                     (0, "allowed-result", ""))
                finally:
                    manager.close()

    def test_real_source_entry_hands_gui_only_its_twenty_devices(self):
        probe = textwrap.dedent("""
            import json, os, runpy, sys
            from unittest.mock import patch
            sys.path.insert(0, sys.argv[1])
            sys.path.insert(1, sys.argv[2])
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtGui import QPixmap
            import core
            phones = {'A%02d' % n: 'device' for n in range(40)}
            core.run = lambda *a, **k: (0, 'List of devices attached\\n' +
                ''.join(s + '\\t' + state + '\\n' for s, state in phones.items()), '')
            from venom_bridge import apply
            apply()
            class Window:
                def __init__(self):
                    import auth, version
                    print('ENTRY_PROBE ' + json.dumps({
                        'direct': sorted(core.list_devices()),
                        'gui': sorted(core.ensure_online()), 'version': version.APP_VERSION,
                        'authenticated': auth.require_key(), 'adb': core.ADB,
                        'core_file': core.__file__}))
                def show_splash(self):
                    pass
            import gui
            gui.ReactionGUI = Window
            gui.app_icon_pixmap = lambda size: QPixmap(size, size)
            entry = os.path.join(sys.argv[1], 'fbvenom.py')
            sys.argv = [entry]
            with patch.object(QApplication, 'exec', return_value=0), \\
                    patch('subprocess.Popen', side_effect=AssertionError('live process')), \\
                    patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                runpy.run_path(entry, run_name='__main__')
        """)
        assigned = ["A%02d" % n for n in range(20)]
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                           FBVENOISON_DEVICES=",".join(assigned),
                           FBVENOISON_AUTHED="1", ADB_PATH="/suite/shared-adb")
        result = subprocess.run([sys.executable, "-c", probe, str(VENOM), str(ROOT)],
                                capture_output=True, text=True,
                                env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith("ENTRY_PROBE "))
        report = json.loads(line[len("ENTRY_PROBE "):])
        self.assertEqual(report["direct"], assigned)
        self.assertEqual(report["gui"], assigned)
        self.assertEqual(report["version"], "1.1.12")
        self.assertEqual(Path(report["core_file"]).resolve(), (VENOM / "core.py").resolve())
        self.assertTrue(report["authenticated"])
        self.assertEqual(report["adb"], "/suite/shared-adb")

    def test_real_upstream_update_and_downgrade_functions_remain_functional(self):
        version = ModuleType("version")
        version.APP_VERSION = "1.1.12"
        with patch.dict(sys.modules, {"version": version}):
            updater = load_file("_tested_upstream_updater", VENOM / "updater.py")
        os.environ["FBVENOISON_DEVICES"] = "mine"
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "FBVenom.exe"
            previous = Path(folder) / "FBVenom.prev.exe"
            staged = Path(folder) / "download.exe"
            for file in (current, previous, staged):
                file.write_bytes(b"fixture executable")
            with patch.object(updater, "APP_DIR", folder), \
                    patch.object(updater, "APP_PATH", str(current)), \
                    patch.object(updater, "SETTINGS_PATH", str(Path(folder) / "venom_update.json")), \
                    patch.object(updater, "_launch_detached") as launch:
                script = updater.install(str(staged), restart=False)
                self.assertTrue(Path(script).is_file())
                self.assertIn(str(current), Path(script).read_text())
                self.assertTrue(updater.can_downgrade())
                self.assertTrue(Path(updater.downgrade(restart=False)).is_file())
                launch.assert_not_called()

    def test_build_marker_is_bound_to_exact_completed_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "FBVenom.exe"
            marker = executable.parent / bridge.METADATA_NAME
            self.assertFalse(marker.exists())
            with self.assertRaises(FileNotFoundError):
                bridge.write_build_metadata(executable)
            self.assertFalse(marker.exists())
            executable.write_bytes(b"MZ-completed-test-build")
            self.assertEqual(bridge.write_build_metadata(executable), marker)
            self.assertEqual(json.loads(marker.read_text()), {
                "app": "FBVenom", "protocol": 1,
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()})


if __name__ == "__main__":
    unittest.main()
