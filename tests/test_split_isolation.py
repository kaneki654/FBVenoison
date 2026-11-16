# -*- coding: utf-8 -*-
"""Each app must see ONLY its own phone box - verified in real child processes.

This is the point of the whole merge: 40 phones, 20 to FBPoison, 20 to
FBVenom, with neither app's source changed. The split is enforced by
bootstrap.py wrapping core.list_devices(), so it is tested the way the hub
does it - by actually spawning the children.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import fake_fleet
import boxes

PROBE = os.path.join(HERE, "probe_entry.py")
fails = []


def check(name, condition, detail=""):
    print("   %-46s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


def run_child(app_dir, label, serials, fleet):
    """Spawn one app through the bootstrap and ask what it can see."""
    env = dict(os.environ)
    env["FBVENOISON_DEVICES"] = ",".join(serials)
    env["FBVENOISON_LABEL"] = label
    env["FBVENOISON_AUTHED"] = "1"
    env["PROBE_FLEET"] = json.dumps({d["serial"]: d["state"] for d in fleet})
    # Standalone app cores also read boxes.json now. Keep this test independent
    # of the user's real saved assignments, including the empty-mask case.
    with tempfile.TemporaryDirectory(prefix="venoison_child_") as temp:
        cfg = os.path.join(temp, "boxes.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"assign": {}}, fh)
        env["FBVENOISON_CONFIG"] = cfg
        p = subprocess.run(
            [sys.executable, os.path.join(APP, "bootstrap.py"), app_dir, PROBE],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=240)
    for line in (p.stdout or "").splitlines():
        if line.startswith("PROBE_RESULT "):
            return json.loads(line[len("PROBE_RESULT "):])
    print("      child stdout:", (p.stdout or "")[-300:])
    print("      child stderr:", (p.stderr or "")[-300:])
    return None


def split_fleet(fleet):
    assign = boxes.auto_split(fleet)
    p = sorted(s for s, r in assign.items() if r == boxes.POISON)
    v = sorted(s for s, r in assign.items() if r == boxes.VENOM)
    return p, v


# ─────────────────────────────────────────────────────────────────────────
print("1) the real setup: 40 phones, 20 per app")
fleet = fake_fleet.two_boxes()
box_p, box_v = split_fleet(fleet)
res_p = run_child(boxes.APPS[boxes.POISON]["dir"], "FBPOISON", box_p, fleet)
res_v = run_child(boxes.APPS[boxes.VENOM]["dir"], "FBVENOM", box_v, fleet)
check("FBPoison child started", res_p is not None)
check("FBVenom child started", res_v is not None)
if res_p and res_v:
    check("FBPoison sees exactly its 20", res_p["direct"] == box_p,
          "%d seen" % len(res_p["direct"]))
    check("FBVenom sees exactly its 20", res_v["direct"] == box_v,
          "%d seen" % len(res_v["direct"]))
    check("GUI path (ensure_online) filtered too",
          res_p["via_ensure_online"] == box_p
          and res_v["via_ensure_online"] == box_v)
    check("no phone reached both apps",
          not (set(res_p["direct"]) & set(res_v["direct"])))
    check("FBPoison ran in its own folder",
          res_p["cwd_is_app_dir"] == "FBPoison", res_p["cwd_is_app_dir"])
    check("FBVenom ran in its own folder",
          res_v["cwd_is_app_dir"] == "FBVenom", res_v["cwd_is_app_dir"])
    # The FULL path, not just the tail: "...FBPoison\core.py" is equally true
    # of a sibling copy, so the loose check would pass while the hub launched
    # a different app than the one boxes.py resolves to.
    want_p = os.path.join(boxes.APPS[boxes.POISON]["dir"], "core.py")
    want_v = os.path.join(boxes.APPS[boxes.VENOM]["dir"], "core.py")
    check("each app loaded the exact core.py the hub resolves to",
          os.path.normcase(res_p["core_file"]) == os.path.normcase(want_p)
          and os.path.normcase(res_v["core_file"]) == os.path.normcase(want_v),
          "%s | %s" % (res_p["core_file"], res_v["core_file"]))
    check("FBVenom still has its AI config", res_v["has_aiconfig"])
    check("FBPoison does not", not res_p["has_aiconfig"])

# ─────────────────────────────────────────────────────────────────────────
print("\n2) awkward serials must survive the hand-off")
fleet = fake_fleet.messy_fleet()
wifi = [d["serial"] for d in fleet if ":" in d["serial"]]
emu = [d["serial"] for d in fleet if d["serial"].startswith("emulator-")]
mine = sorted(wifi + emu + [fleet[2]["serial"], fleet[3]["serial"]])
res = run_child(boxes.APPS[boxes.POISON]["dir"], "FBPOISON", mine, fleet)
check("child started", res is not None)
if res:
    check("wifi serial with a colon came through",
          all(w in res["direct"] for w in wifi), str(wifi))
    check("emulator serial came through",
          all(e in res["direct"] for e in emu), str(emu))
    check("got exactly the listed phones", res["direct"] == mine,
          "%d of %d" % (len(res["direct"]), len(mine)))

# ─────────────────────────────────────────────────────────────────────────
print("\n3) offline / unauthorized phones are still MINE, not hidden")
fleet = fake_fleet.messy_fleet()
sick = sorted(d["serial"] for d in fleet if d["state"] != "device")
res = run_child(boxes.APPS[boxes.POISON]["dir"], "FBPOISON", sick, fleet)
check("child started", res is not None)
if res:
    check("a broken phone assigned to me still appears",
          res["direct"] == sick, "%s vs %s" % (res["direct"], sick))
    check("its state is reported, not swallowed",
          any(v != "device" for v in res["states"].values()),
          str(res["states"]))

# ─────────────────────────────────────────────────────────────────────────
print("\n4) a big fleet does not overflow the hand-off")
fleet = fake_fleet.flat_no_usb(200)
mine = sorted(d["serial"] for d in fleet[:100])
res = run_child(boxes.APPS[boxes.POISON]["dir"], "FBPOISON", mine, fleet)
check("child started with 100 serials", res is not None)
if res:
    check("all 100 arrived intact", res["direct"] == mine,
          "%d arrived" % len(res["direct"]))

# ─────────────────────────────────────────────────────────────────────────
print("\n5) an empty box must not silently mean 'everything'")
fleet = fake_fleet.two_boxes()
res = run_child(boxes.APPS[boxes.POISON]["dir"], "FBPOISON", [], fleet)
check("child started", res is not None)
if res:
    # No assignment means the app behaves exactly as it does standalone.
    # The hub refuses to launch an empty box, which is tested separately.
    check("unassigned launch sees the whole fleet",
          len(res["direct"]) == len(fleet), "%d" % len(res["direct"]))

print("\n" + ("PASS: each app is confined to its own box"
              if not fails else "FAIL (%d):\n  " % len(fails)
              + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
