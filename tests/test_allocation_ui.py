# -*- coding: utf-8 -*-
"""Exercise capacity controls and allocation persistence through the real hub."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
import boxes
import hub


def fleet(count=40):
    return [{"serial": "PHONE%03d" % i, "state": "device", "usb": "",
             "model": "Test phone"} for i in range(count)]


class RunningProcess:
    pid = 12345

    def poll(self):
        return None

    def terminate(self):
        pass


class AllocationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="venoison_allocation_ui_")
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "boxes.json")
        self.config_patch = mock.patch.object(boxes, "CONFIG_PATH", self.path)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.scanner = mock.patch.object(boxes, "list_devices_verbose", return_value=fleet())
        self.scan = self.scanner.start()
        self.addCleanup(self.scanner.stop)
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.procs = {boxes.POISON: None, boxes.VENOM: None}
            window.timer.stop()
            window.atmosphere.timer.stop()
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def open_hub(self):
        window = hub.Hub()
        window.timer.stop()
        window.atmosphere.timer.stop()
        self.windows.append(window)
        return window

    def disk(self):
        with open(self.path, encoding="utf-8") as stream:
            return json.load(stream)

    def backups(self):
        return sorted(Path(self.temp.name).glob("boxes.before-realignment-*.json"))

    def counts(self, window):
        return tuple(sum(role == wanted for role in window.assign.values())
                     for wanted in (boxes.POISON, boxes.VENOM, boxes.UNASSIGNED))

    def test_rebalance_button_fills_default_twenty_each_and_saves_backup(self):
        window = self.open_hub()
        self.assertEqual(self.counts(window), (0, 0, 40))
        before = copy.deepcopy(window.cfg)
        window.balance_btn.click()
        self.assertEqual(self.counts(window), (20, 20, 0))
        self.assertEqual(window.panel[boxes.POISON].count_lbl.text(), "20 / 20 ready")
        self.assertEqual(window.panel[boxes.VENOM].count_lbl.text(), "20 / 20 ready")
        self.assertEqual(self.disk(), window.cfg)
        self.assertEqual(window.cfg["groups"], before["groups"])
        self.assertEqual(len(self.backups()), 1)
        with self.backups()[0].open(encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), before)
        self.assertIn("Targets met", window.allocation_hint.text())

    def test_target_edits_save_without_moving_until_rebalance(self):
        window = self.open_hub()
        window.balance_btn.click()
        before = dict(window.assign)
        backup_count = len(self.backups())
        window.target_spins[boxes.POISON].setValue(28)
        window.target_spins[boxes.VENOM].setValue(12)
        self.assertEqual(window.assign, before)
        self.assertEqual(self.disk()["assign"], before)
        self.assertEqual(len(self.backups()), backup_count)
        self.assertEqual(self.disk()["targets"], {boxes.POISON: 28, boxes.VENOM: 12})
        self.assertIn("Poison needs 8 more", window.allocation_hint.text())
        self.assertIn("Venom has 8 extra", window.allocation_hint.text())
        window.balance_btn.click()
        self.assertEqual(self.counts(window), (28, 12, 0))
        self.assertEqual(sum(before[s] != window.assign[s] for s in before), 8)
        reopened = self.open_hub()
        self.assertEqual(reopened.target_spins[boxes.POISON].value(), 28)
        self.assertEqual(reopened.target_spins[boxes.VENOM].value(), 12)
        self.assertEqual(reopened.assign, window.assign)

    def test_individual_move_can_exceed_target_and_survives_rescan(self):
        window = self.open_hub()
        window.balance_btn.click()
        window.select_group(boxes.POISON)
        serial = sorted(window.rows)[0]
        window.rows[serial].setSelected(True)
        self.assertTrue(window.move_btns[boxes.VENOM].isEnabled())
        window.move_btns[boxes.VENOM].click()
        self.assertEqual(self.counts(window), (19, 21, 0))
        self.assertEqual(window.target_spins[boxes.VENOM].value(), 20)
        self.assertIn("Venom has 1 extra", window.allocation_hint.text())
        self.assertEqual(self.disk()["assign"][serial], boxes.VENOM)
        window.rescan()
        self.assertEqual(self.counts(window), (19, 21, 0))
        self.assertEqual(window.assign[serial], boxes.VENOM)

    def test_running_app_blocks_rebalance_and_individual_rerouting(self):
        window = self.open_hub()
        window.balance_btn.click()
        window.target_spins[boxes.POISON].setValue(28)
        window.target_spins[boxes.VENOM].setValue(12)
        before, saved = copy.deepcopy(window.cfg), self.disk()
        backup_count = len(self.backups())
        window.procs[boxes.POISON] = RunningProcess()
        window.refresh_counts()
        self.assertFalse(window.balance_btn.isEnabled())
        self.assertFalse(window.rebalance_devices())
        window.select_group(boxes.POISON)
        serial = sorted(window.rows)[0]
        window.rows[serial].setSelected(True)
        window.move_selected(boxes.VENOM)
        self.assertEqual(window.cfg, before)
        self.assertEqual(window.assign, before["assign"])
        self.assertEqual(self.disk(), saved)
        self.assertEqual(len(self.backups()), backup_count)
        self.assertIn("Stop", window.status.text())

    def test_rebalance_save_failure_preserves_state_and_recoverable_backup(self):
        window = self.open_hub()
        window.balance_btn.click()
        window.target_spins[boxes.POISON].setValue(28)
        window.target_spins[boxes.VENOM].setValue(12)
        before, saved = copy.deepcopy(window.cfg), self.disk()
        original_assign = dict(window.assign)
        with mock.patch.object(boxes, "save", side_effect=PermissionError("read-only test config")):
            self.assertFalse(window.rebalance_devices())
        self.assertEqual(window.cfg, before)
        self.assertEqual(window.assign, original_assign)
        self.assertEqual(self.disk(), saved)
        self.assertIn("Could not save rebalance", window.status.text())
        with open(window.last_alignment_backup, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), before)

    def test_target_save_failure_rolls_back_visible_values_and_state(self):
        window = self.open_hub()
        before, saved = copy.deepcopy(window.cfg), self.disk()
        with mock.patch.object(boxes, "save", side_effect=PermissionError("read-only test config")):
            window.target_spins[boxes.POISON].setValue(28)
        self.assertEqual(window.cfg, before)
        self.assertEqual(self.disk(), saved)
        self.assertEqual(window.target_spins[boxes.POISON].value(), 20)
        self.assertEqual(window.target_spins[boxes.VENOM].value(), 20)
        self.assertIn("Could not save device targets", window.status.text())
        self.assertEqual(self.backups(), [])

    def test_unavailable_devices_do_not_satisfy_ready_targets(self):
        devices = fleet()
        devices[0]["state"] = "offline"
        devices[20]["state"] = "unauthorized"
        self.scan.return_value = devices
        config = {"assign": {d["serial"]: (boxes.POISON if i < 20 else boxes.VENOM)
                             for i, d in enumerate(devices)}}
        boxes.save(config, strict=True)
        window = self.open_hub()
        self.assertEqual(self.counts(window), (20, 20, 0))
        self.assertEqual(window.metrics["connected"].value.text(), "40")
        self.assertEqual(window.metrics["ready"].value.text(), "38")
        self.assertEqual(window.metrics["unavailable"].value.text(), "2")
        for role in (boxes.POISON, boxes.VENOM):
            self.assertEqual(window.panel[role].count_lbl.text(), "19 / 20 ready")
            self.assertIn("1 assigned but unavailable", window.panel[role].count_lbl.toolTip())
        window.balance_btn.click()
        self.assertEqual(window.assign[devices[0]["serial"]], boxes.POISON)
        self.assertEqual(window.assign[devices[20]["serial"]], boxes.VENOM)
        self.assertIn("Poison needs 1 more", window.allocation_hint.text())
        self.assertIn("Venom needs 1 more", window.allocation_hint.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
