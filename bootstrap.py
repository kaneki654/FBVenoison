# -*- coding: utf-8 -*-
"""Start FBPoison or FBVenom seeing ONLY its own phone box.

Run as:  python bootstrap.py <app_dir> <entry.py>

Neither app's source is modified. Both route every device lookup through
core.list_devices() - the GUI list goes through ensure_online(), which calls
it, and the engine calls it directly - so wrapping that one function in the
child process is enough to hand each app its own 20 phones.

The apps run as separate PROCESSES on purpose:
  * they both define modules named `core` and `gui`; one interpreter cannot
    hold two different versions of those names without rewriting imports,
  * and two processes means two GILs, so 40 phones scale the way 20 did.
"""
import os
import runpy
import sys


def main():
    if len(sys.argv) < 3:
        print("usage: bootstrap.py <app_dir> <entry.py>")
        return 2
    app_dir = os.path.abspath(sys.argv[1])
    entry = os.path.join(app_dir, sys.argv[2])
    if not os.path.isfile(entry):
        print(f"[bootstrap] no entry point at {entry}")
        return 2

    # Run exactly as if launched from its own folder: relative paths for
    # screenshots/, venom_ai.json and the rest must keep working.
    sys.path.insert(0, app_dir)
    os.chdir(app_dir)

    allow = {s.strip() for s in os.environ.get("FBVENOISON_DEVICES", "").split(",")
             if s.strip()}
    label = os.environ.get("FBVENOISON_LABEL", os.path.basename(app_dir))

    import core

    if allow:
        real_list = core.list_devices

        def list_devices():
            # Hide the other box entirely. Filtering here rather than in the
            # app keeps both codebases untouched, and covers the GUI and the
            # engine at once because both go through this function.
            return {s: st for s, st in real_list().items() if s in allow}

        core.list_devices = list_devices
        print(f"[{label}] phone box: {len(allow)} device(s) assigned")
    else:
        print(f"[{label}] no box assigned - this app sees EVERY device")

    # The hub already asked for the key; do not ask a second time for a child
    # it spawned itself. Launched directly (no hub), the gate runs as normal.
    if os.environ.get("FBVENOISON_AUTHED") == "1":
        try:
            import auth
            auth.require_key = lambda *a, **k: True
        except Exception as e:
            print(f"[{label}] could not reuse the hub's unlock ({e})")

    runpy.run_path(entry, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
