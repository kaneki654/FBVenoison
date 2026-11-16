# -*- coding: utf-8 -*-
"""The hub must really open two separate, independent windows.

Not "a function returned without raising" - two live processes, each holding
its own box, each in its own app folder, both cleaned up when the hub closes.
The real entry points are swapped for a stand-in that reports and then stays
open, because a GUI cannot be asserted on from a test runner.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ADB_PATH"] = os.path.join(HERE, "fake_adb.bat" if os.name == "nt" else "fake_adb.py")

from PyQt6.QtWidgets import QApplication
import fake_fleet
import boxes

_tmp = tempfile.mkdtemp(prefix="venoison_launch_")
boxes.CONFIG_PATH = os.path.join(_tmp, "boxes.json")

# Keep the real app folders (bootstrap must import each app's own core.py),
# but swap the entry point for the stand-in. bootstrap joins dir+entry, and an
# absolute entry wins, so this reaches the stand-in without touching the apps.
FAKE_ENTRY = os.path.join(HERE, "fake_app.py")
for role in (boxes.POISON, boxes.VENOM):
    boxes.APPS[role]["entry"] = FAKE_ENTRY

import hub

app = QApplication.instance() or QApplication([])
fails = []


def check(name, condition, detail=""):
    print("   %-48s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


fleet = fake_fleet.two_boxes()
path = os.path.join(_tmp, "fleet.json")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(fleet, fh)
os.environ["FAKE_ADB_FLEET"] = path
os.environ["PROBE_FLEET"] = json.dumps({d["serial"]: d["state"] for d in fleet})

w = hub.Hub()
group_p, group_v = w.cfg["groups"]
w.move_phonebox(group_p["id"], boxes.POISON)
w.move_phonebox(group_v["id"], boxes.VENOM)
box_p = w.serials_for(boxes.POISON)
box_v = w.serials_for(boxes.VENOM)
print("1) launching both boxes")
check("board split 20 / 20", (len(box_p), len(box_v)) == (20, 20),
      "%d/%d" % (len(box_p), len(box_v)))

# A group member vanishes at the hub scan, then reappears when the child asks
# ADB (the fake child fleet still contains it). Its launch mask must keep it.
gone = box_p[0]
with open(path, "w", encoding="utf-8") as fh:
    json.dump([d for d in fleet if d["serial"] != gone], fh)
w.rescan()
check("one group member disconnected before launch",
      len(w.serials_for(boxes.POISON)) == 19 and gone not in w.assign)
check("disconnected group member retains its app",
      w.cfg["assign"].get(gone) == boxes.POISON)

reports = {}
for role in (boxes.POISON, boxes.VENOM):
    os.environ["FAKE_APP_REPORT"] = os.path.join(_tmp, "%s.json" % role)
    reports[role] = os.environ["FAKE_APP_REPORT"]
    w.launch(role)

deadline = time.time() + 90
while time.time() < deadline:
    if all(os.path.isfile(p) for p in reports.values()):
        break
    time.sleep(0.3)

got = {}
for role, p in reports.items():
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            got[role] = json.load(fh)

check("FBPoison process started", boxes.POISON in got)
check("FBVenom process started", boxes.VENOM in got)

if len(got) == 2:
    p_rep, v_rep = got[boxes.POISON], got[boxes.VENOM]
    check("two DIFFERENT processes", p_rep["pid"] != v_rep["pid"],
          "%s vs %s" % (p_rep["pid"], v_rep["pid"]))
    check("FBPoison got exactly its box", p_rep["devices"] == box_p,
          "%d devices" % len(p_rep["devices"]))
    check("FBVenom got exactly its box", v_rep["devices"] == box_v,
          "%d devices" % len(v_rep["devices"]))
    check("neither saw the other's phones",
          not (set(p_rep["devices"]) & set(v_rep["devices"])))
    check("reconnected group member is in the child launch mask",
          gone in w.launched_serials[boxes.POISON] and gone in p_rep["devices"])
    check("each ran in its own app folder",
          p_rep["cwd"] == "FBPoison" and v_rep["cwd"] == "FBVenom",
          "%s / %s" % (p_rep["cwd"], v_rep["cwd"]))
    check("each loaded its OWN core.py",
          "FBPoison" in p_rep["core_file"] and "FBVenom" in v_rep["core_file"],
          "%s | %s" % (p_rep["core_file"][-30:], v_rep["core_file"][-30:]))

print("\n2) the hub tracks them while they run")
w.poll_children()
check("poison shows RUNNING", w.is_running(boxes.POISON))
check("venom shows RUNNING", w.is_running(boxes.VENOM))
check("card says running with a pid",
      "Running" in w.panel[boxes.POISON].state_lbl.text()
      and "pid" in w.panel[boxes.POISON].state_lbl.text(),
      w.panel[boxes.POISON].state_lbl.text())
check("LAUNCH disabled while open",
      not w.panel[boxes.POISON].launch_btn.isEnabled())
check("STOP enabled while open",
      w.panel[boxes.POISON].stop_btn.isEnabled())
before = dict(w.cfg["assign"])
check("running app protects even its disconnected group member",
      not w.move_phonebox(group_p["id"], boxes.VENOM)
      and w.cfg["assign"] == before)

print("\n3) relaunching an already-open app is refused")
w.launch(boxes.POISON)
check("refused, with a reason", "already open" in w.status.text().lower(),
      w.status.text())

print("\n4) stopping one leaves the other alone")
w.stop(boxes.POISON)
deadline = time.time() + 40
while time.time() < deadline and w.is_running(boxes.POISON):
    time.sleep(0.2)
w.poll_children()
check("poison closed", not w.is_running(boxes.POISON))
check("venom still open", w.is_running(boxes.VENOM))
check("poison card back to not-running",
      w.panel[boxes.POISON].state_lbl.text() == "Not running",
      w.panel[boxes.POISON].state_lbl.text())
check("LAUNCH re-enabled", w.panel[boxes.POISON].launch_btn.isEnabled())

print("\n5) closing the hub takes the children with it")


class _Ev:
    def accept(self):
        pass


w.closeEvent(_Ev())
deadline = time.time() + 40
while time.time() < deadline and w.is_running(boxes.VENOM):
    time.sleep(0.2)
check("no orphan windows left behind", not w.is_running(boxes.VENOM))

print("\n" + ("PASS: two real windows, one box each, cleaned up properly"
              if not fails else "FAIL (%d):\n  " % len(fails)
              + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
