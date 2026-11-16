# -*- coding: utf-8 -*-
"""Box splitting against fleets that look like real ones.

Everything here goes through boxes.list_devices_verbose(), which shells out to
adb and parses its text - ADB_PATH points at fake_adb.bat, so the subprocess
call and the parser are genuinely exercised rather than stubbed.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import fake_fleet
import boxes

fails = []
_tmp = tempfile.mkdtemp(prefix="venoison_")
os.environ["ADB_PATH"] = os.path.join(HERE, "fake_adb.bat" if os.name == "nt" else "fake_adb.py")


def serve(devices):
    """Make `adb devices -l` return this fleet, then read it back the real way."""
    path = os.path.join(_tmp, "fleet.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(devices, fh)
    os.environ["FAKE_ADB_FLEET"] = path
    return boxes.list_devices_verbose()


def check(name, condition, detail=""):
    print("   %-46s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


def split_counts(assign):
    p = sum(1 for r in assign.values() if r == boxes.POISON)
    v = sum(1 for r in assign.values() if r == boxes.VENOM)
    return p, v


# ─────────────────────────────────────────────────────────────────────────
print("1) the real setup: two boxes of 20 on two hubs")
devs = serve(fake_fleet.two_boxes())
check("adb parsing returned 40 phones", len(devs) == 40, "got %d" % len(devs))
check("usb paths survived parsing", all(d["usb"] for d in devs))
check("models survived parsing", all(d["model"] for d in devs))
assign = boxes.auto_split(devs)
p, v = split_counts(assign)
check("split is 20 / 20", (p, v) == (20, 20), "got %d/%d" % (p, v))
by_hub = {}
for d in devs:
    by_hub.setdefault(boxes.hub_of(d["usb"]), set()).add(assign[d["serial"]])
check("no hub straddles both apps", all(len(x) == 1 for x in by_hub.values()),
      str({k: sorted(x) for k, x in by_hub.items()}))

# ─────────────────────────────────────────────────────────────────────────
print("\n2) uneven boxes (18 + 22) - still one hub per app")
devs = serve(fake_fleet.two_boxes(18, 22))
assign = boxes.auto_split(devs)
p, v = split_counts(assign)
check("all 40 assigned", p + v == 40, "got %d" % (p + v))
check("bigger hub went to poison", p == 22, "poison=%d venom=%d" % (p, v))

# ─────────────────────────────────────────────────────────────────────────
print("\n3) messy fleet: offline, unauthorized, wifi, emulator, deep hub")
devs = serve(fake_fleet.messy_fleet())
check("every line parsed", len(devs) == 22, "got %d" % len(devs))
states = {d["state"] for d in devs}
check("offline state preserved", "offline" in states, str(states))
check("unauthorized state preserved", "unauthorized" in states, str(states))
wifi = [d for d in devs if ":" in d["serial"]]
check("wifi serial with a colon survived", len(wifi) == 1, str(wifi))
emu = [d for d in devs if d["serial"].startswith("emulator-")]
check("emulator listed", len(emu) == 1)
deep = [d for d in devs if d["serial"] == "DEEPHUB01"]
check("deep hub path parsed", bool(deep) and deep[0]["usb"].count(".") >= 2,
      str(deep))
check("deep hub groups with its own hub",
      boxes.hub_of("1-1.3.3.3") == "1-1.3.3", boxes.hub_of("1-1.3.3.3"))
assign = boxes.auto_split(devs)
check("every phone got a box", len(assign) == len(devs),
      "%d of %d" % (len(assign), len(devs)))
check("no phone landed in both",
      not (set(s for s, r in assign.items() if r == boxes.POISON)
           & set(s for s, r in assign.items() if r == boxes.VENOM)))

# ─────────────────────────────────────────────────────────────────────────
print("\n4) one box only (20 phones, one hub)")
devs = serve(fake_fleet.one_box())
assign = boxes.auto_split(devs)
p, v = split_counts(assign)
check("all 20 assigned", p + v == 20, "got %d" % (p + v))
check("a single hub is halved, not dumped on one app", p > 0 and v > 0,
      "poison=%d venom=%d" % (p, v))

# ─────────────────────────────────────────────────────────────────────────
print("\n5) three hubs - must not silently drop one")
devs = serve(fake_fleet.three_hubs())
assign = boxes.auto_split(devs)
p, v = split_counts(assign)
check("all 24 assigned", p + v == 24, "got %d" % (p + v))

# ─────────────────────────────────────────────────────────────────────────
print("\n6) no usb info at all (hubless / older adb)")
devs = serve(fake_fleet.flat_no_usb(40))
check("40 parsed without usb fields", len(devs) == 40)
assign = boxes.auto_split(devs)
p, v = split_counts(assign)
check("still a clean 20 / 20", (p, v) == (20, 20), "got %d/%d" % (p, v))

# ─────────────────────────────────────────────────────────────────────────
print("\n7) degenerate fleets")
for n in (0, 1, 2, 3, 39, 41, 100):
    devs = serve(fake_fleet.flat_no_usb(n))
    assign = boxes.auto_split(devs)
    check("%3d phones -> all assigned, none lost" % n, len(assign) == n,
          "%d assigned" % len(assign))

# ─────────────────────────────────────────────────────────────────────────
print("\n8) adb itself is unhappy")
os.environ["FAKE_ADB_FLEET"] = os.path.join(_tmp, "does-not-exist.json")
devs = boxes.list_devices_verbose()
check("missing fleet file -> empty list, no crash", devs == [], str(devs[:2]))
old = os.environ["ADB_PATH"]
os.environ["ADB_PATH"] = os.path.join(HERE, "no_such_adb_binary.bat")
devs = boxes.list_devices_verbose()
check("missing adb binary -> empty list, no crash", devs == [], str(devs[:2]))
os.environ["ADB_PATH"] = old

# ─────────────────────────────────────────────────────────────────────────
print("\n9) saved assignment survives a rescan, and tracks unplugging")
devs = serve(fake_fleet.two_boxes())
assign = boxes.auto_split(devs)
cfg = {"assign": dict(assign), "auto": False}
again = boxes.resolve(devs, cfg)
check("unchanged across a rescan", again == assign)
moved = devs[0]["serial"]
cfg["assign"][moved] = (boxes.VENOM if assign[moved] == boxes.POISON
                        else boxes.POISON)
after = boxes.resolve(devs, cfg)
check("manual move is respected", after[moved] != assign[moved])
fewer = devs[:30]
shrunk = boxes.resolve(fewer, cfg)
check("unplugged phones drop out", len(shrunk) == 30, "%d" % len(shrunk))
check("remaining phones keep their box",
      all(shrunk[d["serial"]] == cfg["assign"][d["serial"]] for d in fewer))
back = boxes.resolve(devs, cfg)
check("replugged phones come back to the same box", back == after)

print("\n" + ("PASS: box splitting holds up on realistic fleets"
              if not fails else "FAIL (%d):\n  " % len(fails)
              + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
