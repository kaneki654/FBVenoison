"""A rebuilt suite shares ADB and reuses the hub's unlock in native children."""
import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fbvenoison


class SharedAdbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'fbvenoison.exe'
        self.adb = self.root / 'platform-tools' / 'adb.exe'
        self.adb.parent.mkdir()
        self.adb.touch()
        real_isfile = os.path.isfile
        self.enterContext(patch.object(fbvenoison.os.path, 'isfile',
            side_effect=lambda candidate: Path(candidate) == self.adb and real_isfile(candidate)))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(fbvenoison.sys, 'platform', 'win32'))
        self.enterContext(patch.object(fbvenoison.sys, 'frozen', True, create=True))
        self.enterContext(patch.object(fbvenoison.sys, 'executable', str(self.exe)))

    def test_frozen_hub_pins_shared_tools_beside_executable(self):
        fbvenoison._pin_adb()
        self.assertEqual(os.environ['ADB_PATH'], str(self.adb))

    def test_existing_adb_override_remains_authoritative(self):
        os.environ['ADB_PATH'] = 'already-selected-adb'
        fbvenoison._pin_adb()
        self.assertEqual(os.environ['ADB_PATH'], 'already-selected-adb')

    def test_missing_shared_tools_leaves_normal_discovery_available(self):
        self.adb.unlink()
        fbvenoison._pin_adb()
        self.assertNotIn('ADB_PATH', os.environ)


class NativePoisonUnlockTests(unittest.TestCase):
    def run_entry(self, authed):
        updater = SimpleNamespace(bootstrap=Mock(), start=Mock())
        application = Mock()
        application.exec.return_value = 0
        widgets = ModuleType('PyQt6.QtWidgets')
        widgets.QApplication = Mock(return_value=application)
        qtgui = ModuleType('PyQt6.QtGui')
        qtgui.QIcon = Mock()
        gui = SimpleNamespace(ReactionGUI=Mock(), app_icon_pixmap=Mock())
        auth = SimpleNamespace(require_key=Mock(return_value=False))
        modules = {'updater': updater, 'gui': gui, 'auth': auth,
                   'PyQt6.QtWidgets': widgets, 'PyQt6.QtGui': qtgui}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {
                'FBVENOISON_AUTHED': authed}, clear=True), patch.object(
                sys, 'argv', ['fbpoison.py']):
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_path(str(ROOT / 'FBPoison/fbpoison.py'), run_name='__main__')
        self.assertEqual(exit_result.exception.code, 0)
        return auth, gui

    def test_managed_native_child_uses_existing_unlock(self):
        auth, gui = self.run_entry('1')
        auth.require_key.assert_not_called()
        gui.ReactionGUI.assert_called_once()
        gui.ReactionGUI.return_value.show_splash.assert_called_once()

    def test_standalone_child_still_requires_its_key(self):
        auth, gui = self.run_entry('0')
        auth.require_key.assert_called_once()
        gui.ReactionGUI.assert_not_called()


if __name__ == '__main__':
    unittest.main()
