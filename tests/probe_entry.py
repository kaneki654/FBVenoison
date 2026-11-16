# -*- coding: utf-8 -*-
"""Stands in for fbpoison.py / fbvenom.py inside the bootstrap.

By the time this runs, bootstrap has already wrapped core.list_devices(). We
fake the `adb devices` output so a fleet exists, then report what this child
can actually see - directly, and through ensure_online(), which is the path
the GUI's device list uses.
"""
import json
import os
import sys

import core

# {serial: state} exactly as the fleet defines it - the child must report the
# real state, not a guess, or "this phone is offline" never reaches the UI.
STATES = json.loads(os.environ.get("PROBE_FLEET", "{}"))
FLEET = list(STATES.keys())


def fake_run(args, serial=None, timeout=60):
    if args and args[0] == "devices":
        body = "".join("%s\t%s\n" % (s, STATES.get(s, "device")) for s in FLEET)
        return 0, "List of devices attached\n" + body, ""
    return 0, "", ""


core.run = fake_run

direct = core.list_devices()
gui = core.ensure_online()
print("PROBE_RESULT " + json.dumps({
    "label": os.environ.get("FBVENOISON_LABEL", ""),
    "cwd_is_app_dir": os.path.basename(os.getcwd()),
    "core_file": os.path.abspath(core.__file__),
    "has_aiconfig": hasattr(core, "ai_cfg"),
    "direct": sorted(direct.keys()),
    "states": direct,
    "via_ensure_online": sorted(gui.keys()),
}))
