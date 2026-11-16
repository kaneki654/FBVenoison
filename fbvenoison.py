# -*- coding: utf-8 -*-
"""FBVenoison — VENom + pOISON.

Launcher for the pair. FBPoison drives one phone box (reactions), FBVenom the
other (comments), each in its own window, each seeing only its own phones.
Neither app's code is touched; the split is applied in the child process.

    python fbvenoison.py            # the hub window
    python fbvenoison.py --boxes    # print the split and exit
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _pin_adb():
    """Make the whole suite speak to devices through ONE adb.

    The hub, FBPoison and FBVenom otherwise each resolve their own adb, and a
    version mismatch makes them - and the phone mirror (touping) - kill and
    restart each other's server on port 5037, which drops phones mid-run.
    ADB_PATH is honoured by boxes.py and by both apps' _find_adb(), and children
    inherit it, so setting it once here keeps everyone on a single server. An
    ADB_PATH already in the environment wins; otherwise match the phone mirror's
    bundled adb so the whole machine agrees on one version.
    """
    if os.environ.get("ADB_PATH", "").strip():
        return
    for cand in (r"C:\Program Files (x86)\panda_android\tools\adb.exe",
                 r"C:\Program Files\panda_android\tools\adb.exe"):
        if os.path.isfile(cand):
            os.environ["ADB_PATH"] = cand
            print("[adb] suite pinned to %s" % cand)
            return
    if sys.platform == "win32":
        root = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
        bundled = os.path.join(root, "platform-tools", "adb.exe")
        if os.path.isfile(bundled):
            os.environ["ADB_PATH"] = bundled


def main():
    if "--demo" in sys.argv:
        from demo import run
        return run()

    import boxes

    _pin_adb()

    if "--boxes" in sys.argv:
        devices = boxes.list_devices_verbose()
        cfg = boxes.load()
        # Preview the same capture rules as the hub without changing the file.
        boxes.discover_groups(devices, cfg)
        assign = boxes.resolve(devices, cfg)
        print("%d device(s) connected" % len(devices))
        connected = {d["serial"] for d in devices}
        for group in cfg["groups"]:
            roles = {cfg["assign"].get(s, boxes.UNASSIGNED) for s in group["serials"]}
            role = next(iter(roles)) if len(roles) == 1 else "mixed"
            label = "choose app" if role == boxes.UNASSIGNED else role
            print("  %s: %d/%d connected, %d captured, %s" % (group["name"],
                  len(connected.intersection(group["serials"])), boxes.PHONEBOX_SIZE,
                  len(group["serials"]), label))
        for role in (boxes.POISON, boxes.VENOM):
            mine = sorted(s for s, r in assign.items() if r == role)
            meta = boxes.APPS[role]
            print("\n%s (%s) - %d phone(s)"
                  % (meta["label"], meta["job"], len(mine)))
            for s in mine:
                print("   ", s)
        loose = [d["serial"] for d in devices
                 if assign.get(d["serial"], boxes.UNASSIGNED)
                 == boxes.UNASSIGNED]
        if loose:
            print("\nawaiting app selection (%d) - these will sit idle:" % len(loose))
            for s in loose:
                print("   ", s)
        return 0

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    import hub

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(hub.app_icon(256)))

    # One gate for the whole suite. The children are told it already passed,
    # so they do not each ask again; started on their own they still do.
    # Venoison's own panel - same key and same layered check as FBPoison's,
    # but it does not import that app's auth/gui/core just to ask a question.
    from login_panel import require_key
    if not require_key():
        return 0

    w = hub.Hub()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
