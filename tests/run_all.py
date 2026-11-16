# -*- coding: utf-8 -*-
"""Run every FBVenoison test. Each in its own process, so one crash cannot
take the rest with it.

    python tests/run_all.py
"""
import os
import subprocess
import sys
import time

# This console is cp1252; child output carries box-drawing and accented text,
# and printing it raised UnicodeEncodeError - hiding the very failure we were
# trying to show. Same trap as core.py's emoji-in-account-names bug.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ("PowerShell rebuild and registration workflow", "test_rebuild_script.py"),
    ("rebuilt app launch integration", "test_rebuild_launch.py"),
    ("local credential settings", "test_private_settings.py"),
    ("isolated demo entry and controls", "test_demo.py"),
    ("requested device capacities", "test_allocation.py"),
    ("device target controls and persistence", "test_allocation_ui.py"),
    ("bundled application resolution", "test_app_resolution.py"),
    ("managed FBVenom executable isolation", "test_managed_venom.py"),
    ("imported FBVenom suite bridge", "test_venom_suite_bridge.py"),
    ("verified suite updater", "test_updater.py"),
    ("Facebook share-video URL normalization", "test_url_normalization.py"),
    ("box splitting on realistic fleets", "test_boxes_diverse.py"),
    ("durable phonebox groups", "test_phonebox_groups.py"),
    ("each app confined to its box", "test_split_isolation.py"),
    ("the hub window", "test_hub_ui.py"),
    ("scanning and dragging phonebox groups", "test_phonebox_groups_ui.py"),
    ("launching two real windows", "test_launch.py"),
    ("the login panel", "test_login_panel.py"),
]


def main():
    print("FBVenoison test suite\n" + "=" * 58)
    passed, failed = 0, []
    for title, name in TESTS:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            failed.append((name, "missing"))
            print("  MISSING  %-42s" % title)
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, path], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=HERE, timeout=900)
        dt = time.time() - t0
        if p.returncode == 0:
            passed += 1
            print("  PASS     %-42s %5.1fs" % (title, dt))
        else:
            failed.append((name, (p.stdout or "") + (p.stderr or "")))
            print("  FAIL     %-42s %5.1fs" % (title, dt))
    print("=" * 58)
    print("%d passed, %d failed" % (passed, len(failed)))
    for name, out in failed:
        print("\n--- %s ---" % name)
        print("\n".join(out.strip().splitlines()[-18:]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
