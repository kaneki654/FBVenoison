"""A frozen Venom child must explicitly support the hub's device mask."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import boxes
import hub


class ManagedVenomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.exe = Path(self.temp.name) / 'FBVenom.exe'
        self.exe.write_bytes(b'fixture executable identity')
        self.marker = self.exe.with_suffix('.suite-bridge.json')

    def mark(self, **changes):
        data = {'protocol': 1, 'app': 'FBVenom',
                'sha256': hashlib.sha256(self.exe.read_bytes()).hexdigest()}
        data.update(changes)
        self.marker.write_text(json.dumps(data))

    def window(self):
        return SimpleNamespace(
            is_running=lambda role: False,
            serials_for=lambda role: ['VENOM-01'],
            cfg={'assign': {'VENOM-01': boxes.VENOM, 'POISON-01': boxes.POISON}},
            say=Mock(), procs={}, launched_serials={},
            panel={boxes.VENOM: SimpleNamespace(set_running=Mock())})

    def test_upstream_binary_is_not_assumed_compatible(self):
        self.assertFalse(hub.child_bridge_ready(str(self.exe)))
        self.marker.write_text('[]')
        self.assertFalse(hub.child_bridge_ready(str(self.exe)))
        self.mark(protocol=2)
        self.assertFalse(hub.child_bridge_ready(str(self.exe)))

    def test_marker_binds_exact_executable(self):
        self.mark()
        self.assertTrue(hub.child_bridge_ready(str(self.exe)))
        self.exe.write_bytes(b'upstream replacement without the bridge')
        self.assertFalse(hub.child_bridge_ready(str(self.exe)))

    def test_frozen_launch_refuses_unmanaged_binary(self):
        window = self.window()
        with patch.object(hub.sys, 'frozen', True, create=True), \
                patch.object(hub, 'child_exe', return_value=str(self.exe)), \
                patch.object(hub.subprocess, 'Popen') as launch:
            hub.Hub.launch(window, boxes.VENOM)
        launch.assert_not_called()
        self.assertIn('Rebuild FBVenom', window.say.call_args.args[0])

    def test_frozen_launch_passes_only_assigned_serials(self):
        self.mark()
        window = self.window()
        with patch.object(hub.sys, 'frozen', True, create=True), \
                patch.object(hub, 'child_exe', return_value=str(self.exe)), \
                patch.object(hub.subprocess, 'Popen') as launch:
            launch.return_value.pid = 123
            hub.Hub.launch(window, boxes.VENOM)
        launch.assert_called_once()
        env = launch.call_args.kwargs['env']
        self.assertEqual(env['FBVENOISON_DEVICES'], 'VENOM-01')
        self.assertEqual(env['FBVENOISON_AUTHED'], '1')
        self.assertEqual(window.launched_serials[boxes.VENOM], {'VENOM-01'})


if __name__ == '__main__':
    unittest.main()
