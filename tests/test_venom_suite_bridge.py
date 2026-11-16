"""The imported FBVenom keeps suite isolation in source and rebuilt EXEs."""
import hashlib
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
from unittest.mock import patch

VENOM = Path(__file__).resolve().parent.parent / "FBVenom"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, VENOM / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_module("_tested_suite_bridge", "suite_bridge.py")


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
        self.modules = patch.dict(sys.modules, {"core": self.core, "auth": self.auth})
        self.modules.start()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.env.stop)

    def test_standalone_keeps_discovery_and_key_gate(self):
        self.assertFalse(bridge.apply())
        self.assertIs(self.core.list_devices, self.original_list)
        self.assertIs(self.auth.require_key, self.original_gate)
        self.assertEqual(self.core.list_devices(), self.devices)

    def test_snapshot_preserves_states_and_awkward_serials(self):
        os.environ["FBVENOISON_DEVICES"] = "mine,192.168.2.15:5555"
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {
            "mine": "device", "192.168.2.15:5555": "offline"})
        self.assertNotIn("theirs", self.core.list_devices())
        self.assertIs(self.auth.require_key, self.original_gate)

    def test_explicit_empty_snapshot_denies_all(self):
        os.environ["FBVENOISON_DEVICES"] = ""
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {})

    def test_reapply_uses_original_without_stale_filter_layers(self):
        os.environ["FBVENOISON_DEVICES"] = "mine"
        bridge.apply()
        os.environ["FBVENOISON_DEVICES"] = "theirs"
        bridge.apply()
        self.assertEqual(self.core.list_devices(), {"theirs": "device"})
        self.assertIs(self.core.list_devices.__wrapped__, self.original_list)

    def test_hub_auth_and_shared_adb_are_applied(self):
        os.environ.update(FBVENOISON_DEVICES="mine", FBVENOISON_AUTHED="1",
                          ADB_PATH="/suite/shared-adb")
        bridge.apply()
        self.assertTrue(self.auth.require_key())
        self.assertEqual(self.core.ADB, "/suite/shared-adb")

    def test_entry_applies_bridge_before_gui_or_cli(self):
        os.environ.update(FBVENOISON_DEVICES="mine", FBVENOISON_AUTHED="1")
        with patch.dict(sys.modules, {"suite_bridge": bridge}):
            runpy.run_path(str(VENOM / "fbvenom.py"), run_name="_bridge_probe")
        self.assertEqual(self.core.list_devices(), {"mine": "device"})
        self.assertTrue(self.auth.require_key())

    def test_real_source_entry_hands_gui_only_its_twenty_devices(self):
        # A separate interpreter uses the imported app's actual core and entry,
        # with only the window replaced. No bootstrap wrapper is involved.
        probe = textwrap.dedent("""
            import json, os, runpy, sys
            from unittest.mock import patch
            sys.path.insert(0, sys.argv[1])
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtGui import QPixmap
            import core
            phones = {'A%02d' % n: 'device' for n in range(40)}
            core.run = lambda *a, **k: (0, 'List of devices attached\\n' +
                ''.join(s + '\\t' + state + '\\n' for s, state in phones.items()), '')
            class Window:
                def __init__(self):
                    import auth
                    print('ENTRY_PROBE ' + json.dumps({
                        'direct': sorted(core.list_devices()),
                        'gui': sorted(core.ensure_online()),
                        'authenticated': auth.require_key(), 'adb': core.ADB}))
                def show_splash(self):
                    pass
            # Auth imports the theme's helpers from gui, so keep the actual
            # module and replace only window construction and icon loading.
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
        result = subprocess.run([sys.executable, "-c", probe, str(VENOM)],
                                capture_output=True, text=True,
                                env=environment, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines()
                    if line.startswith("ENTRY_PROBE "))
        report = json.loads(line[len("ENTRY_PROBE "):])
        self.assertEqual(report["direct"], assigned)
        self.assertEqual(report["gui"], assigned)
        self.assertTrue(report["authenticated"])
        self.assertEqual(report["adb"], "/suite/shared-adb")

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

    def test_managed_updater_cannot_replace_bridge_with_upstream_build(self):
        version = ModuleType("version")
        version.APP_VERSION = "1.1.1"
        with patch.dict(sys.modules, {"version": version}):
            updater = load_module("_tested_venom_updater", "updater.py")
        os.environ["FBVENOISON_DEVICES"] = "mine"
        with self.assertRaisesRegex(updater.UpdateError, "managed by FBVenoison"):
            updater.install("not-even-read.exe", restart=False)
        with self.assertRaisesRegex(updater.UpdateError, "managed by FBVenoison"):
            updater.downgrade(restart=False)


if __name__ == "__main__":
    unittest.main()
