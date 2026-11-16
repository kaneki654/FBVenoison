# -*- coding: utf-8 -*-
"""Drive the hub window itself against imaginary fleets.

The hub is where mistakes would be invisible - a phone quietly in both boxes,
a filter that hides a phone but still launches it, a launch with an empty box.
Everything here goes through the real widgets: selecting groups, selecting
rows, and pressing the same buttons a user presses.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ADB_PATH"] = os.path.join(HERE, "fake_adb.bat" if os.name == "nt" else "fake_adb.py")

from PyQt6.QtWidgets import QApplication
import fake_fleet
import boxes

_tmp = tempfile.mkdtemp(prefix="venoison_ui_")
boxes.CONFIG_PATH = os.path.join(_tmp, "boxes.json")   # never touch the real one

import hub

app = QApplication.instance() or QApplication([])
fails = []


def check(name, condition, detail=""):
    print("   %-48s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


def serve(devices):
    path = os.path.join(_tmp, "fleet.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(devices, fh)
    os.environ["FAKE_ADB_FLEET"] = path


def fresh_hub(devices):
    serve(devices)
    try:
        os.remove(boxes.CONFIG_PATH)
    except OSError:
        pass
    return hub.Hub()


def counts(w):
    """Phones per box, per the hub's own assignment."""
    n = {boxes.POISON: 0, boxes.VENOM: 0, boxes.UNASSIGNED: 0}
    for r in w.assign.values():
        n[r] = n.get(r, 0) + 1
    return n


def shown(w):
    """Serials rendered in the table right now (group + filter applied)."""
    return sorted(w.rows.keys())


def select_rows(w, serials):
    w.table.clearSelection()
    for s in serials:
        w.rows[s].setSelected(True)


# ─────────────────────────────────────────────────────────────────────────
print("1) 40 phones, two hubs")
w = fresh_hub(fake_fleet.two_boxes())
check("hub read all 40 phones", len(w.devices) == 40, "%d" % len(w.devices))
w.do_auto_split()
c = counts(w)
check("split is 20 poison / 20 venom / 0 loose",
      (c[boxes.POISON], c[boxes.UNASSIGNED], c[boxes.VENOM]) == (20, 0, 20),
      str(c))
check("rail counts agree",
      w.group_rows[boxes.POISON].count.text() == "20"
      and w.group_rows[boxes.VENOM].count.text() == "20")
check("table shows only the selected group",
      len(shown(w)) == 20 and w.group == boxes.POISON,
      "%d rows in %s" % (len(shown(w)), w.group))
check("every shown phone really is in that group",
      all(w.assign[s] == boxes.POISON for s in shown(w)))

print("\n2) switching group shows the other box")
w.select_group(boxes.VENOM)
check("venom table has the other 20", len(shown(w)) == 20,
      "%d" % len(shown(w)))
check("no phone appears in both groups",
      not (set(shown(w)) & set(s for s, r in w.assign.items()
                               if r == boxes.POISON)))
w.select_group(boxes.UNASSIGNED)
check("unassigned is empty", len(shown(w)) == 0, "%d" % len(shown(w)))
w.select_group(boxes.POISON)

print("\n3) moving selected phones to the other box")
first = shown(w)[:3]
select_rows(w, first)
check("footer reports the selection", "3 selected" in w.status.text(),
      w.status.text())
w.move_selected(boxes.VENOM)
c = counts(w)
check("three moved poison -> venom",
      (c[boxes.POISON], c[boxes.VENOM]) == (17, 23), str(c))
check("they left the poison table",
      not (set(first) & set(shown(w))))
w.select_group(boxes.VENOM)
check("and arrived in venom", all(s in shown(w) for s in first))
w.select_group(boxes.POISON)
w.table.clearSelection()
w.move_selected(boxes.VENOM)
check("moving with nothing selected is refused, not silent",
      "select phones first" in w.status.text().lower(), w.status.text())
select_rows(w, shown(w)[:1])
w.move_selected(boxes.POISON)
check("moving into the group you are already in is refused",
      "already in" in w.status.text().lower(), w.status.text())
w.table.clearSelection()

print("\n4) the assignment survives a rescan")
before = dict(w.assign)
w.rescan()
check("same split after rescan", w.assign == before)
check("table redrawn for the current group", len(shown(w)) == 17,
      "%d" % len(shown(w)))

print("\n5) filtering hides rows but must NOT change the boxes")
poison_before = len(w.serials_for(boxes.POISON))
# filter on one phone's own serial: every group here shares a prefix, so a
# prefix filter would narrow nothing and prove nothing
one = shown(w)[0]
w.search.setText(one)
check("rows are filtered to the match",
      shown(w) == [one], "%d shown" % len(shown(w)))
check("assignment untouched while filtered", w.assign == before)
check("launch still uses the whole box, not just what is visible",
      len(w.serials_for(boxes.POISON)) == poison_before,
      "%d" % len(w.serials_for(boxes.POISON)))
w.search.setText("")
check("clearing the filter restores the rows", len(shown(w)) == 17)

print("\n6) bulk assign follows group + filter, and nothing else")
w6 = fresh_hub(fake_fleet.two_boxes())
w6.assign = {d["serial"]: boxes.UNASSIGNED for d in w6.devices}
w6.select_group(boxes.UNASSIGNED)
w6.search.setText("BOXA")
n_shown = len(shown(w6))
w6.assign_shown(boxes.POISON)
c = counts(w6)
check("only the shown phones were assigned",
      c[boxes.POISON] == n_shown and c[boxes.VENOM] == 0, str(c))
check("the hidden ones stayed unassigned",
      c[boxes.UNASSIGNED] == 40 - n_shown, str(c))
w6.search.setText("BOXB")
w6.assign_shown(boxes.VENOM)
c = counts(w6)
check("the other half went to venom",
      c[boxes.POISON] + c[boxes.VENOM] == 40 and c[boxes.UNASSIGNED] == 0,
      str(c))
w6.search.setText("nothing-matches-this")
w6.assign_shown(boxes.POISON)
check("bulk assign with nothing shown is refused, not silent",
      "nothing to assign" in w6.status.text().lower(), w6.status.text())
w6.search.setText("")

print("\n7) unassigned phones are surfaced, not hidden")
w7 = fresh_hub(fake_fleet.two_boxes())
w7.assign = {d["serial"]: boxes.UNASSIGNED for d in w7.devices}
w7.rebuild()
check("all 40 are unassigned", counts(w7)[boxes.UNASSIGNED] == 40,
      str(counts(w7)))
check("the footer shows phones awaiting an app", "awaiting app" in w7.status.text().lower(),
      w7.status.text())
w7.launch(boxes.POISON)
check("launching an empty box is refused", not w7.is_running(boxes.POISON))
check("and it says why", "no phones" in w7.status.text().lower(),
      w7.status.text())
w7.launch_both()
check("launch both with nothing assigned is refused",
      not w7.is_running(boxes.POISON) and not w7.is_running(boxes.VENOM))

print("\n8) sick phones are flagged, not silently used")
w8 = fresh_hub(fake_fleet.messy_fleet())
w8.do_auto_split()
total = sum(1 for _ in w8.devices)
seen = 0
for role in (boxes.POISON, boxes.VENOM, boxes.UNASSIGNED):
    w8.select_group(role)
    seen += len(shown(w8))
check("every phone appears under some group", seen == total,
      "%d of %d" % (seen, total))
check("footer warns that assigned phones are not ready",
      "not ready" in w8.status.text().lower(), w8.status.text())

print("\n9) no phones connected at all")
w9 = fresh_hub([])
check("hub opens with an empty fleet", len(w9.devices) == 0)
check("no rows", len(shown(w9)) == 0)
w9.do_auto_split()
check("auto-split on nothing does not crash", True)
w9.launch_both()
check("launch with no phones is refused",
      not w9.is_running(boxes.POISON) and not w9.is_running(boxes.VENOM))

print("\n10) a phone unplugged between scans")
fleet = fake_fleet.two_boxes()
w10 = fresh_hub(fleet)
w10.do_auto_split()
gone = fleet[0]["serial"]
role_before = w10.assign[gone]
serve(fleet[1:])
w10.rescan()
check("the unplugged phone left the list", gone not in shown(w10))
check("the rest kept their boxes", len(w10.devices) == 39,
      "%d" % len(w10.devices))
serve(fleet)
w10.rescan()
check("replugged, it returns to the same box",
      w10.assign.get(gone) == role_before, str(w10.assign.get(gone)))

print("\n" + ("PASS: the hub holds up on realistic fleets"
              if not fails else "FAIL (%d):\n  " % len(fails)
              + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
