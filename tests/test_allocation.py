# -*- coding: utf-8 -*-
"""Requested app capacities, with no phones, subprocesses or live config writes."""
import copy
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boxes


def fleet(count):
    return [{"serial": "PHONE%03d" % i, "state": "device", "usb": ""}
            for i in range(count)]


class AllocationTests(unittest.TestCase):
    def test_default_forty_is_twenty_each_and_preview_is_pure(self):
        devices, cfg = fleet(40), {"assign": {}, "other": {"preserve": True}}
        before_devices, before_cfg = copy.deepcopy(devices), copy.deepcopy(cfg)
        planned = boxes.plan_allocation(devices, cfg)
        self.assertEqual(sum(role == boxes.POISON for role in planned.values()), 20)
        self.assertEqual(sum(role == boxes.VENOM for role in planned.values()), 20)
        self.assertEqual(devices, before_devices)
        self.assertEqual(cfg, before_cfg)
        self.assertEqual(boxes.plan_allocation(list(reversed(devices)), cfg), planned)

    def test_realign_moves_only_the_needed_devices_and_keeps_groups(self):
        devices, cfg = fleet(40), {"assign": {}}
        boxes.discover_groups(devices, cfg)
        cfg["assign"] = boxes.plan_allocation(devices, cfg)
        old = copy.deepcopy(cfg)
        targets = {boxes.POISON: 28, boxes.VENOM: 12}
        planned = boxes.plan_allocation(devices, cfg, targets)
        changed = [serial for serial in planned if planned[serial] != cfg["assign"][serial]]
        self.assertEqual(len(changed), 8)
        self.assertTrue(all(planned[serial] == boxes.POISON for serial in changed))
        self.assertEqual(cfg, old)
        status = boxes.allocation_status(devices, planned, targets)
        self.assertEqual(status[boxes.POISON]["ready"], 28)
        self.assertEqual(status[boxes.VENOM]["ready"], 12)
        self.assertEqual(status["idle"], 0)

    def test_short_fleet_is_shared_proportionally_and_reports_shortage(self):
        for count, targets, expected in (
                (30, {boxes.POISON: 20, boxes.VENOM: 20}, (15, 15)),
                (13, {boxes.POISON: 20, boxes.VENOM: 20}, (7, 6)),
                (10, {boxes.POISON: 30, boxes.VENOM: 10}, (8, 2)),
                (0, {boxes.POISON: 20, boxes.VENOM: 20}, (0, 0))):
            with self.subTest(count=count, targets=targets):
                devices = fleet(count)
                result = boxes.plan_allocation(devices, {}, targets)
                status = boxes.allocation_status(devices, result, targets)
                self.assertEqual(tuple(status[role]["ready"] for role in (boxes.POISON, boxes.VENOM)), expected)
                self.assertEqual(sum(status[role]["shortage"] for role in (boxes.POISON, boxes.VENOM)),
                                 sum(targets.values()) - count)

    def test_unavailable_and_disconnected_assignments_survive(self):
        devices = fleet(44)
        devices[40]["state"] = "offline"
        devices[41]["state"] = "unauthorized"
        devices[42]["state"] = "recovery"
        devices[43]["state"] = ""
        cfg = {"assign": {"PHONE040": boxes.POISON, "PHONE041": boxes.VENOM,
                          "PHONE042": boxes.VENOM, "PHONE043": boxes.POISON,
                          "UNPLUGGED": boxes.POISON}}
        planned = boxes.plan_allocation(devices, cfg)
        for serial, role in cfg["assign"].items():
            self.assertEqual(planned[serial], role)
        status = boxes.allocation_status(devices, planned)
        self.assertEqual(status["ready"], 40)
        self.assertEqual(status["unavailable"], 4)
        self.assertEqual(status[boxes.POISON]["ready"], 20)
        self.assertEqual(status[boxes.VENOM]["ready"], 20)

    def test_surplus_is_idle_and_zero_capacity_is_supported(self):
        devices = fleet(45)
        result = boxes.plan_allocation(devices, {})
        self.assertEqual(boxes.allocation_status(devices, result)["idle"], 5)
        targets = {boxes.POISON: 0, boxes.VENOM: 0}
        result = boxes.plan_allocation(devices, {"assign": result}, targets)
        self.assertEqual(set(result.values()), {boxes.UNASSIGNED})
        self.assertEqual(boxes.allocation_status(devices, result, targets)["idle"], 45)

    def test_duplicate_or_bad_scan_rows_cannot_fill_capacity_twice(self):
        devices = fleet(4)
        conflict = dict(devices[0], state="offline")
        rows = devices + devices + [conflict, None, {}, {"serial": "", "state": "device"}]
        cfg = {"assign": {devices[0]["serial"]: boxes.VENOM}}
        planned = boxes.plan_allocation(rows, cfg)
        self.assertEqual(planned[devices[0]["serial"]], boxes.VENOM)
        status = boxes.allocation_status(rows, planned)
        self.assertEqual(status["ready"], 3)
        self.assertEqual(status["unavailable"], 1)

    def test_manual_destinations_are_kept_when_both_targets_already_match(self):
        devices = fleet(40)
        cfg = {"assign": {d["serial"]: (boxes.POISON if i % 2 else boxes.VENOM)
                          for i, d in enumerate(devices)}}
        self.assertEqual(boxes.plan_allocation(devices, cfg), cfg["assign"])

    def test_target_migration_sanitizes_and_persists_without_losing_fields(self):
        self.assertEqual(boxes.app_targets({}), {boxes.POISON: 20, boxes.VENOM: 20})
        cases = [({"poison": -5, "venom": 5000}, (0, 1000)),
                 ({"poison": " 27 ", "venom": "3"}, (27, 3)),
                 ({"poison": True, "venom": "bad"}, (20, 20)),
                 ({"poison": None, "venom": 2.5}, (20, 20)),
                 ({"poison": "9" * 5000}, (20, 20))]
        for values, expected in cases:
            with self.subTest(values=str(values)[:70]):
                self.assertEqual(tuple(boxes.app_targets({"targets": values}).values()), expected)
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(boxes, "CONFIG_PATH", os.path.join(temp, "boxes.json")):
                cfg = boxes.load()
                cfg["targets"] = {boxes.POISON: 12, boxes.VENOM: 28}
                cfg["unrelated"] = {"keep": [1, 2]}
                boxes.save(cfg, strict=True)
                self.assertEqual(boxes.load(), cfg)

    def test_deterministic_exclusive_capacity_invariants_across_fleets(self):
        rng = random.Random(19)
        for count in (0, 1, 7, 19, 20, 39, 40, 41, 70):
            for poison, venom in ((0, 0), (0, 20), (20, 0), (3, 9), (20, 20), (37, 11)):
                devices = fleet(count)
                rng.shuffle(devices)
                cfg = {"assign": {d["serial"]: rng.choice((boxes.POISON, boxes.VENOM, boxes.UNASSIGNED))
                                  for d in devices}}
                targets = {boxes.POISON: poison, boxes.VENOM: venom}
                result = boxes.plan_allocation(devices, cfg, targets)
                status = boxes.allocation_status(devices, result, targets)
                self.assertEqual(set(result), {d["serial"] for d in devices})
                self.assertLessEqual(status[boxes.POISON]["ready"], poison)
                self.assertLessEqual(status[boxes.VENOM]["ready"], venom)
                self.assertEqual(status[boxes.POISON]["ready"] + status[boxes.VENOM]["ready"],
                                 min(count, poison + venom))
                self.assertEqual(boxes.plan_allocation(list(reversed(devices)), cfg, targets), result)
                self.assertEqual(boxes.plan_allocation(devices, {"assign": result}, targets), result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
