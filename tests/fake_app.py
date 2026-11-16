# -*- coding: utf-8 -*-
"""Stands in for a launched app: reports what it sees, then stays open.

Used to prove the hub really starts two separate, long-lived processes - one
per box - the way it starts the real windows.
"""
import json
import os
import sys
import time

import core

FLEET = json.loads(os.environ.get("PROBE_FLEET", "{}"))


def fake_run(args, serial=None, timeout=60):
    if args and args[0] == "devices":
        body = "".join("%s\t%s\n" % (s, st) for s, st in FLEET.items())
        return 0, "List of devices attached\n" + body, ""
    return 0, "", ""


core.run = fake_run

out = os.environ.get("FAKE_APP_REPORT")
report = {
    "pid": os.getpid(),
    "label": os.environ.get("FBVENOISON_LABEL", ""),
    "cwd": os.path.basename(os.getcwd()),
    "core_file": os.path.abspath(core.__file__),
    "devices": sorted(core.list_devices().keys()),
}
if out:
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
print("FAKE_APP_READY " + json.dumps(report))
sys.stdout.flush()

# stay alive like a real window would, until the hub terminates us
while True:
    time.sleep(0.2)
