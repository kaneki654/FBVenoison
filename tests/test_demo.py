"""Exercise the demo entry and controls without real devices or saved changes."""
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
import boxes
import fbvenoison
import hub
import login_panel


def digest(path):
    file = Path(path)
    return hashlib.sha256(file.read_bytes()).digest() if file.is_file() else None


class DemoTests(unittest.TestCase):
    def test_demo_entry_and_controls_are_isolated(self):
        app = QApplication.instance() or QApplication([])
        protected = boxes.CONFIG_PATH
        before = digest(protected)
        config_paths = []
        errors = []
        real_save = boxes.save
        real_exec = QApplication.exec

        def guarded_save(*args, **kwargs):
            self.assertNotEqual(boxes.CONFIG_PATH, protected)
            config_paths.append(boxes.CONFIG_PATH)
            return real_save(*args, **kwargs)

        def exercise():
            try:
                windows = [window for window in app.topLevelWidgets()
                           if isinstance(window, hub.Hub)]
                self.assertEqual(len(windows), 1)
                window = windows[0]
                self.assertEqual(len(window.devices), 40)
                self.assertTrue(all(d["serial"].startswith("DEMO-")
                                    for d in window.devices))
                self.assertEqual(len(window.serials_for(boxes.POISON)), 20)
                self.assertEqual(len(window.serials_for(boxes.VENOM)), 20)
                window.rescan_btn.click()
                window.panel[boxes.POISON].launch_btn.click()
                window.panel[boxes.VENOM].launch_btn.click()
                window.both_btn.click()
                window.update_btn.click()
                self.assertTrue(all(proc is None for proc in window.procs.values()))
                window.select_group(boxes.POISON)
                window.rows[window.serials_for(boxes.POISON)[0]].setSelected(True)
                window.move_selected(boxes.VENOM)
                self.assertEqual(len(window.serials_for(boxes.VENOM)), 21)
                window.target_spins[boxes.POISON].setValue(18)
                window.target_spins[boxes.VENOM].setValue(22)
                window.balance_btn.click()
                self.assertEqual(len(window.serials_for(boxes.POISON)), 18)
                self.assertEqual(len(window.serials_for(boxes.VENOM)), 22)
                window.motion_btn.click()
                self.assertTrue(Path(boxes.CONFIG_PATH).is_file())
            except BaseException as error:
                errors.append(error)
            finally:
                for window in app.topLevelWidgets():
                    if isinstance(window, hub.Hub):
                        window.close()
                app.quit()

        def finite_exec():
            QTimer.singleShot(0, exercise)
            QTimer.singleShot(3000, app.quit)
            return real_exec()

        forbidden = AssertionError("Demo attempted a live operation")
        with patch.object(sys, "argv", ["fbvenoison.py", "--demo"]), \
                patch.object(fbvenoison, "_pin_adb", side_effect=forbidden) as pin, \
                patch.object(boxes, "list_devices_verbose", side_effect=forbidden) as scan, \
                patch.object(login_panel, "require_key", side_effect=forbidden) as auth, \
                patch("subprocess.run", side_effect=forbidden) as process, \
                patch("subprocess.Popen", side_effect=forbidden) as launch, \
                patch("urllib.request.urlopen", side_effect=forbidden) as network, \
                patch.object(boxes, "save", side_effect=guarded_save), \
                patch.object(QApplication, "exec", side_effect=finite_exec):
            self.assertEqual(fbvenoison.main(), 0)
            self.assertIs(boxes.list_devices_verbose, scan)
            for operation in (pin, scan, auth, process, launch, network):
                operation.assert_not_called()

        if errors:
            raise errors[0]
        self.assertEqual(boxes.CONFIG_PATH, protected)
        self.assertEqual(digest(protected), before)
        self.assertTrue(config_paths)
        self.assertTrue(all(not Path(path).exists() for path in config_paths))


if __name__ == "__main__":
    unittest.main()
