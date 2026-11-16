# -*- coding: utf-8 -*-
"""Durable phonebox capture and assignment, without real ADB or config writes."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import boxes


def fleet(prefix, count=20, hub="1-1", start=0):
    return [{"serial": "%s%03d" % (prefix, i), "state": "device",
             "usb": "%s.%d" % (hub, i + 1) if hub else ""}
            for i in range(start, start + count)]


def serials(devices):
    return sorted(device["serial"] for device in devices)


class PhoneboxGroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="venoison_groups_")
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "boxes.json")
        self.config_patch = mock.patch.object(boxes, "CONFIG_PATH", self.path)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.cfg = boxes.load()

    def test_sequential_boxes_keep_membership_names_and_choices(self):
        first, second = fleet("A"), fleet("B", hub="2-2")
        created = boxes.discover_groups(list(reversed(first)), self.cfg)
        self.assertEqual(len(created), 1)
        group_id = created[0]["id"]
        self.assertEqual(created[0]["serials"], serials(first))
        self.assertTrue(boxes.rename_group(self.cfg, group_id, "  Farm One  "))
        self.assertTrue(boxes.assign_group(self.cfg, group_id, boxes.VENOM))
        saved_first = copy.deepcopy(boxes.get_group(self.cfg, group_id))

        created = boxes.discover_groups(list(reversed(second + first)), self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["id"], "box-2")
        self.assertEqual(created[0]["serials"], serials(second))
        self.assertEqual(boxes.get_group(self.cfg, group_id), saved_first)
        self.assertEqual(boxes.discover_groups(second, self.cfg), [])
        self.assertEqual(boxes.discover_groups(first + second, self.cfg), [])
        choices = boxes.resolve(first + second, self.cfg)
        self.assertTrue(all(choices[s] == boxes.VENOM for s in serials(first)))
        self.assertTrue(all(choices[s] == boxes.UNASSIGNED for s in serials(second)))

    def test_cross_hub_scan_cohort_completes_twenty(self):
        first, second = fleet("A"), fleet("B", hub="2-2")
        created = boxes.discover_groups(first[:11] + second[:9], self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["serials"], serials(first[:11] + second[:9]))
        created = boxes.discover_groups(list(reversed(first + second)), self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["serials"], serials(first[11:] + second[9:]))
        self.assertEqual(self.cfg["pending"], {})

    def test_full_hubs_take_priority_on_simultaneous_forty_scan(self):
        first, second = fleet("Z"), fleet("A", hub="2-2")
        created = boxes.discover_groups(list(reversed(first + second)), self.cfg)
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0]["serials"], serials(first))
        self.assertEqual(created[1]["serials"], serials(second))

    def test_four_internal_hubs_per_box_allow_sequential_capture(self):
        first = sum((fleet("A%d" % hub, 5, hub="1-1.%d" % hub)
                     for hub in range(4)), [])
        second = sum((fleet("B%d" % hub, 5, hub="2-2.%d" % hub)
                      for hub in range(4)), [])
        captured = boxes.discover_groups(list(reversed(first)), self.cfg)[0]
        self.assertEqual(captured["serials"], serials(first))
        created = boxes.discover_groups(second + first, self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["serials"], serials(second))
        self.assertEqual(self.cfg["groups"][0], captured)

    def test_mixed_usb_metadata_does_not_stall_capture(self):
        devices = fleet("A", 10) + fleet("B", 10, hub="")
        created = boxes.discover_groups(list(reversed(devices)), self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["serials"], serials(devices))
        self.assertEqual(self.cfg["pending"], {})

    def test_cross_hub_partial_scans_keep_global_first_seen_order(self):
        first = fleet("Z", 4) + fleet("Y", 4, hub="2-2")
        later = fleet("A", 6) + fleet("B", 6, hub="2-2")
        group = boxes.discover_groups(first, self.cfg)[0]
        group_id = group["id"]
        self.assertTrue(group["capturing"])
        boxes.save(self.cfg)
        self.cfg = boxes.load()
        created = boxes.discover_groups(later + list(reversed(first)), self.cfg)
        self.assertEqual(created, [])
        self.assertEqual(boxes.get_group(self.cfg, group_id)["serials"], serials(first) + serials(later))
        self.assertFalse(boxes.get_group(self.cfg, group_id)["capturing"])
        self.assertEqual(self.cfg["pending_order"], [])

    def test_cross_hub_fallback_names_every_visible_phone(self):
        devices = fleet("A", 17) + fleet("B", 16, hub="2-2")
        created = boxes.discover_groups(devices, self.cfg)
        self.assertEqual([len(group["serials"]) for group in created], [20, 13])
        self.assertEqual([group["capturing"] for group in created], [False, True])
        self.assertEqual(self.cfg["pending"], {})

    def test_disconnected_partial_does_not_absorb_new_visible_partial(self):
        devices = fleet("A")
        first = boxes.discover_groups(devices[:10], self.cfg)[0]
        second = boxes.discover_groups(devices[10:], self.cfg)[0]
        self.assertEqual(first["serials"], serials(devices[:10]))
        self.assertEqual(second["serials"], serials(devices[10:]))
        self.assertEqual(boxes.discover_groups([], self.cfg), [])
        self.assertEqual(boxes.discover_groups(list(reversed(devices)), self.cfg), [])
        self.assertEqual(len(self.cfg["groups"]), 2)

    def test_stale_pending_cannot_fill_a_newly_visible_box(self):
        stale, current = fleet("A", 10), fleet("B")
        boxes.discover_groups(stale, self.cfg)
        created = boxes.discover_groups(current, self.cfg)
        self.assertEqual(created[0]["serials"], serials(current))
        self.assertEqual(self.cfg["groups"][0]["serials"], serials(stale))
        more = fleet("A", 10, start=10)
        created = boxes.discover_groups(stale + more, self.cfg)
        self.assertEqual(created, [])
        self.assertEqual(self.cfg["groups"][0]["serials"], serials(stale + more))

    def test_missing_usb_uses_scan_batches_and_stable_enumeration(self):
        # The later box's serials sort before the earlier box. It must still
        # become box-2, rather than a newly sorted half of the combined fleet.
        first, second = fleet("Z", hub=""), fleet("A", hub="")
        boxes.discover_groups(list(reversed(first)), self.cfg)
        boxes.discover_groups(second + first, self.cfg)
        self.assertEqual(self.cfg["groups"][0]["serials"], serials(first))
        self.assertEqual(self.cfg["groups"][1]["serials"], serials(second))
        simultaneous = boxes.load()
        created = boxes.discover_groups(list(reversed(first + second)), simultaneous)
        self.assertEqual(created[0]["serials"], serials(second))
        self.assertEqual(created[1]["serials"], serials(first))

    def test_duplicates_and_topology_changes_never_regroup_members(self):
        devices = fleet("A")
        boxes.discover_groups(devices[:10], self.cfg)
        moved = fleet("A", hub="3-3")
        duplicate = dict(moved[0], usb="")
        created = boxes.discover_groups(moved + moved + [duplicate], self.cfg)
        self.assertEqual(created, [])
        self.assertEqual(self.cfg["groups"][0]["serials"], serials(moved))
        self.assertEqual(self.cfg["pending"], {})
        renamed = copy.deepcopy(self.cfg["groups"][0])
        self.assertEqual(boxes.discover_groups(devices + moved, self.cfg), [])
        self.assertEqual(self.cfg["groups"], [renamed])

    def test_partial_first_seen_order_is_retained(self):
        later_serials = fleet("Z", 10)
        earlier_serials = fleet("A", 10)
        boxes.discover_groups(later_serials, self.cfg)
        created = boxes.discover_groups(earlier_serials + later_serials, self.cfg)
        self.assertEqual(created, [])
        self.assertEqual(self.cfg["groups"][0]["serials"],
                         serials(later_serials) + serials(earlier_serials))

    def test_legacy_roles_are_preserved_and_group_moves_include_disconnected(self):
        devices = fleet("A")
        self.cfg["assign"] = {devices[0]["serial"]: boxes.POISON,
                              "OTHER": boxes.VENOM}
        group = boxes.discover_groups(devices, self.cfg)[0]
        self.assertEqual(self.cfg["assign"][devices[0]["serial"]], boxes.POISON)
        self.assertTrue(boxes.assign_group(self.cfg, group["id"], boxes.VENOM))
        # Resolve with half the box unplugged; assignment still updates all 20.
        self.assertEqual(len(boxes.resolve(devices[:10], self.cfg)), 10)
        self.assertTrue(all(self.cfg["assign"][s] == boxes.VENOM for s in serials(devices)))
        self.assertEqual(self.cfg["assign"]["OTHER"], boxes.VENOM)
        self.assertTrue(boxes.assign_group(self.cfg, group["id"], boxes.UNASSIGNED))
        self.assertTrue(all(r == boxes.UNASSIGNED
                            for r in boxes.resolve(devices, self.cfg).values()))

    def test_names_and_roles_reject_invalid_input_without_mutation(self):
        group = boxes.discover_groups(fleet("A"), self.cfg)[0]
        self.assertIs(boxes.get_group(self.cfg, group["id"]), group)
        before = copy.deepcopy(self.cfg)
        for name in (" ", "X" * 81, None, 42):
            self.assertFalse(boxes.rename_group(self.cfg, group["id"], name))
        self.assertFalse(boxes.rename_group(self.cfg, "missing", "Name"))
        self.assertFalse(boxes.assign_group(self.cfg, group["id"], "unknown"))
        self.assertFalse(boxes.assign_group(self.cfg, "missing", boxes.POISON))
        self.assertIsNone(boxes.get_group(self.cfg, "missing"))
        self.assertEqual(self.cfg, before)
        self.assertTrue(boxes.rename_group(self.cfg, group["id"], "X" * 80))

    def test_round_trip_keeps_groups_pending_roles_and_other_fields(self):
        group = boxes.discover_groups(fleet("A"), self.cfg)[0]
        boxes.rename_group(self.cfg, group["id"], "Farm 一")
        boxes.assign_group(self.cfg, group["id"], boxes.POISON)
        boxes.discover_groups(fleet("B", 8, hub="2-2"), self.cfg)
        self.cfg["auto"] = False
        self.cfg["display"] = {"width": 800}
        boxes.save(self.cfg)
        loaded = boxes.load()
        self.assertEqual(loaded, self.cfg)
        self.assertEqual(boxes.discover_groups(fleet("B", hub="2-2"), loaded), [])
        self.assertEqual(loaded["groups"][1]["id"], "box-2")
        self.assertFalse(loaded["groups"][1]["capturing"])
        self.assertEqual(loaded["groups"][0]["name"], "Farm 一")
        self.assertEqual(loaded["display"], {"width": 800})

    def test_sanitization_enforces_exclusive_membership_and_safe_roles(self):
        saved = {"assign": {"A": boxes.POISON, "B": "invalid", "": boxes.VENOM},
                 "auto": False, "extra": 7,
                 "groups": [None,
                            {"id": "box-5", "name": " Saved ",
                             "serials": ["A", "B", "A", None, ""]},
                            {"id": "box-5", "name": "Duplicate ID", "serials": ["C"]},
                            {"id": "custom", "name": "", "serials": ["B", "D"]}],
                 "pending": {"1-1": ["A", "E", "E", None], "2-2": ["E", "F"]}}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        loaded = boxes.load()
        self.assertEqual(loaded["groups"],
                         [{"id": "box-5", "name": "Saved", "serials": ["A", "B"], "capturing": True},
                          {"id": "custom", "name": "custom", "serials": ["D"], "capturing": True}])
        self.assertEqual(loaded["pending"], {"1-1": ["E"], "2-2": ["F"]})
        self.assertEqual(loaded["assign"], {"A": boxes.POISON, "B": boxes.UNASSIGNED,
                                             "D": boxes.UNASSIGNED, "E": boxes.UNASSIGNED,
                                             "F": boxes.UNASSIGNED})
        self.assertEqual(loaded["extra"], 7)
        new = boxes.discover_groups(fleet("Z", hub="3-3"), loaded)[0]
        self.assertEqual(new["id"], "box-6")

    def test_atomic_save_failure_preserves_previous_file(self):
        boxes.save(self.cfg)
        with open(self.path, "rb") as fh:
            original = fh.read()
        self.cfg["extra"] = "change"
        with mock.patch.object(boxes.os, "replace", side_effect=OSError("write blocked")):
            boxes.save(self.cfg)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(os.listdir(self.temp.name), ["boxes.json"])

    def test_strict_atomic_save_failure_raises_and_cleans_temp_file(self):
        boxes.save(self.cfg)
        with open(self.path, "rb") as fh:
            original = fh.read()
        self.cfg["extra"] = "unsaved change"
        with mock.patch.object(boxes.os, "replace", side_effect=OSError("write blocked")):
            with self.assertRaisesRegex(OSError, "write blocked"):
                boxes.save(self.cfg, strict=True)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(os.listdir(self.temp.name), ["boxes.json"])

    def test_partial_growth_keeps_incoming_legacy_role_in_other_app(self):
        devices = fleet("A")
        group = boxes.discover_groups(devices[:9], self.cfg)[0]
        boxes.assign_group(self.cfg, group["id"], boxes.POISON)
        legacy_venom = devices[9]["serial"]
        self.cfg["assign"][legacy_venom] = boxes.VENOM
        self.assertEqual(boxes.discover_groups(devices, self.cfg), [])
        self.assertEqual(self.cfg["assign"][legacy_venom], boxes.VENOM)
        self.assertTrue(all(self.cfg["assign"][s] == boxes.POISON
                            for s in serials(devices) if s != legacy_venom))
        self.assertEqual(boxes.serials_for(boxes.VENOM, devices, self.cfg), [legacy_venom])

    def test_discovery_is_in_memory_and_resolve_still_supports_legacy(self):
        devices = fleet("A")
        with mock.patch.object(boxes, "save") as save:
            boxes.discover_groups(devices, self.cfg)
            save.assert_not_called()
        self.assertFalse(os.path.exists(self.path))
        legacy = {"assign": {}, "auto": True}
        self.assertEqual(boxes.resolve(devices, legacy), boxes.auto_split(devices))
        groups_only = {"groups": self.cfg["groups"], "auto": True}
        self.assertTrue(all(role == boxes.UNASSIGNED
                            for role in boxes.resolve(devices, groups_only).values()))

    def test_first_nine_get_named_immediately_then_fill_to_twenty(self):
        devices = fleet("A", hub="")
        group = boxes.discover_groups(list(reversed(devices[:9])), self.cfg)[0]
        self.assertEqual(group["serials"], serials(devices[:9]))
        self.assertTrue(group["capturing"])
        boxes.rename_group(self.cfg, group["id"], "Actual box")
        self.assertEqual(boxes.discover_groups(devices, self.cfg), [])
        group = boxes.get_group(self.cfg, group["id"])
        self.assertEqual(group["name"], "Actual box")
        self.assertEqual(group["serials"], serials(devices))
        self.assertFalse(group["capturing"])
        self.assertEqual(self.cfg["pending"], {})

    def test_new_twenty_has_priority_over_growing_visible_partial_nine(self):
        partial, complete = fleet("A", 9, hub=""), fleet("B", hub="")
        first = copy.deepcopy(boxes.discover_groups(partial, self.cfg)[0])
        created = boxes.discover_groups(partial + complete, self.cfg)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["serials"], serials(complete))
        self.assertFalse(created[0]["capturing"])
        self.assertEqual(boxes.get_group(self.cfg, first["id"]), first)

    def test_partial_unplugged_then_new_twenty_stays_separate(self):
        partial, complete = fleet("A", 9), fleet("B", hub="2-2")
        first = copy.deepcopy(boxes.discover_groups(partial, self.cfg)[0])
        created = boxes.discover_groups(complete, self.cfg)
        self.assertEqual(created[0]["serials"], serials(complete))
        self.assertEqual(boxes.get_group(self.cfg, first["id"]), first)

    def test_partial_growth_inherits_only_unanimous_saved_app_role(self):
        devices = fleet("A")
        for role in (boxes.POISON, boxes.VENOM):
            cfg = boxes.load()
            group = boxes.discover_groups(devices[:9], cfg)[0]
            boxes.assign_group(cfg, group["id"], role)
            boxes.discover_groups(devices, cfg)
            self.assertTrue(all(cfg["assign"][s] == role for s in serials(devices)))
        cfg = boxes.load()
        group = boxes.discover_groups(devices[:9], cfg)[0]
        cfg["assign"][devices[0]["serial"]] = boxes.POISON
        boxes.discover_groups(devices, cfg)
        self.assertEqual(cfg["assign"][devices[0]["serial"]], boxes.POISON)
        self.assertTrue(all(cfg["assign"][s] == boxes.UNASSIGNED for s in serials(devices[9:])))
        cfg = boxes.load()
        boxes.discover_groups(devices[:9], cfg)
        cfg["assign"][devices[9]["serial"]] = boxes.VENOM
        boxes.discover_groups(devices, cfg)
        self.assertEqual(cfg["assign"][devices[9]["serial"]], boxes.VENOM)

    def test_partial_never_exceeds_capacity_or_absorbs_disconnected_pending(self):
        initial, overflow = fleet("A", 9), fleet("B", 12)
        original = copy.deepcopy(boxes.discover_groups(initial, self.cfg)[0])
        self.cfg["pending"] = {"1-1": ["STALE%02d" % i for i in range(11)]}
        self.cfg["pending_order"] = list(self.cfg["pending"]["1-1"])
        created = boxes.discover_groups(initial + overflow, self.cfg)
        self.assertEqual(created[0]["serials"], serials(overflow))
        self.assertEqual(boxes.get_group(self.cfg, original["id"]), original)
        self.assertEqual(len(self.cfg["pending"]["1-1"]), 11)

    def test_closed_twenty_reconnect_four_plus_five_does_not_regroup(self):
        old, new = fleet("A"), fleet("B", 5)
        first = copy.deepcopy(boxes.discover_groups(old, self.cfg)[0])
        created = boxes.discover_groups(old[:4] + new, self.cfg)
        self.assertEqual(created[0]["serials"], serials(new))
        self.assertTrue(created[0]["capturing"])
        self.assertEqual(boxes.get_group(self.cfg, first["id"]), first)

    def test_load_migrates_old_partial_capturing_and_preserves_closed_flag(self):
        cfg = {"groups": [{"id": "box-1", "name": "Closed20", "serials": serials(fleet("A"))},
                          {"id": "box-2", "name": "Open9", "serials": serials(fleet("B", 9))},
                          {"id": "box-3", "name": "Locked5", "serials": serials(fleet("C", 5)),
                           "capturing": False}]}
        boxes.save(cfg)
        loaded = boxes.load()
        self.assertEqual([group["capturing"] for group in loaded["groups"]], [False, True, False])

    def test_explicit_realign_repairs_actual_nine_and_next_eleven_completes(self):
        current = fleet("ACT", 9, hub="")
        historical = fleet("HIST", 16, hub="")
        stale = fleet("STALE", 11, hub="")
        saved_members = serials(current[:4] + historical)
        self.cfg["groups"] = [{"id": "box-1", "name": "Real farmbox", "serials": saved_members}]
        self.cfg["assign"] = {serial: boxes.POISON for serial in saved_members}
        self.cfg["pending"] = {"": serials(current[4:] + stale)}
        self.cfg["pending_order"] = serials(current[4:] + stale)
        before_assign = dict(self.cfg["assign"])
        with mock.patch.object(boxes, "save") as save:
            self.assertTrue(boxes.realign_group(current, self.cfg, "box-1"))
            save.assert_not_called()
        group = boxes.get_group(self.cfg, "box-1")
        self.assertEqual(group["name"], "Real farmbox")
        self.assertEqual(group["serials"], serials(current))
        self.assertTrue(group["capturing"])
        self.assertEqual(self.cfg["alignment_history"],
                         [{"id": "box-1", "name": "Real farmbox", "serials": saved_members}])
        self.assertEqual(self.cfg["pending"][""], serials(stale))
        self.assertTrue(all(self.cfg["assign"][serial] == role
                            for serial, role in before_assign.items()))
        boxes.discover_groups(current + stale, self.cfg)
        self.assertEqual(len(boxes.get_group(self.cfg, "box-1")["serials"]), 20)
        self.assertFalse(boxes.get_group(self.cfg, "box-1")["capturing"])
        self.assertEqual(self.cfg["pending"], {})
        self.assertFalse(os.path.exists(self.path))

    def test_realign_after_autoscan_absorbs_partial_and_retains_history(self):
        old, new = fleet("A"), fleet("B", 5)
        first = copy.deepcopy(boxes.discover_groups(old, self.cfg)[0])
        boxes.rename_group(self.cfg, first["id"], "Original name")
        second = copy.deepcopy(boxes.discover_groups(old[:4] + new, self.cfg)[0])
        assignments = dict(self.cfg["assign"])
        self.assertTrue(boxes.realign_group(old[:4] + new, self.cfg, first["id"]))
        self.assertEqual(len(self.cfg["groups"]), 1)
        self.assertIsNone(boxes.get_group(self.cfg, second["id"]))
        group = boxes.get_group(self.cfg, first["id"])
        self.assertEqual(group["name"], "Original name")
        self.assertEqual(group["serials"], serials(old[:4] + new))
        self.assertEqual(self.cfg["assign"], assignments)
        self.assertEqual([entry["serials"] for entry in self.cfg["alignment_history"]],
                         [first["serials"], second["serials"]])
        boxes.save(self.cfg)
        self.assertEqual(boxes.load(), self.cfg)
        new = boxes.discover_groups(fleet("C"), self.cfg)[0]
        self.assertEqual(new["id"], "box-3")

    def test_realign_absorption_keeps_other_partial_disconnected_members(self):
        first = boxes.discover_groups(fleet("A"), self.cfg)[0]
        other = boxes.discover_groups(fleet("B", 8), self.cfg)[0]
        self.assertTrue(boxes.realign_group(fleet("A", 4) + fleet("B", 5), self.cfg, first["id"]))
        self.assertEqual(boxes.get_group(self.cfg, other["id"])["serials"], serials(fleet("B", 3, start=5)))
        self.assertTrue(boxes.get_group(self.cfg, other["id"])["capturing"])

    def test_realign_rejects_closed_overlap_invalid_count_or_unknown_id(self):
        first = boxes.discover_groups(fleet("A"), self.cfg)[0]
        boxes.discover_groups(fleet("B"), self.cfg)
        before = copy.deepcopy(self.cfg)
        for devices, ident in ((fleet("A", 4) + fleet("B", 5), first["id"]),
                               (fleet("C", 21), first["id"]),
                               ([], first["id"]), (fleet("A", 4), "missing")):
            self.assertFalse(boxes.realign_group(devices, self.cfg, ident))
            self.assertEqual(self.cfg, before)

    def test_realign_deduplicates_scan_rows_and_preserves_serial_states(self):
        group = boxes.discover_groups(fleet("A"), self.cfg)[0]
        current = fleet("A", 4)
        current[0]["state"] = "offline"
        current[1]["state"] = "unauthorized"
        self.assertTrue(boxes.realign_group(current + current, self.cfg, group["id"]))
        self.assertEqual(boxes.get_group(self.cfg, group["id"])["serials"], serials(current))


if __name__ == "__main__":
    unittest.main(verbosity=2)
