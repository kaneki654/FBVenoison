# -*- coding: utf-8 -*-
"""The new gate must be a new PANEL, not a new lock.

The verification has to stay byte-for-byte what FBPoison already uses - same
salt, pepper, mask, iteration count and sealed digest - or the existing key
would silently stop working, or worse, a weaker check would let something else
through. That equivalence is asserted against FBPoison's own module.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
TOOLS = os.path.dirname(APP)
sys.path.insert(0, APP)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
import login_panel
import boxes

# The app the hub really launches - which is the bundled copy, not whatever
# sibling folder happens to sit next to the suite. Comparing against the wrong
# copy would "pass" while the shipped app used a different key.
POISON_DIR = boxes.APPS[boxes.POISON]["dir"]

app = QApplication.instance() or QApplication([])
fails = []


def check(name, condition, detail=""):
    print("   %-52s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


# ─────────────────────────────────────────────────────────────────────────
print("1) the lock is unchanged  (vs %s)" % POISON_DIR)
probe = (
    "import sys; sys.path.insert(0, r'%s')\n"
    "import auth\n"
    "print(auth._SALT)\nprint(auth._PEPPER)\nprint(auth._MASK)\n"
    "print(auth._ITERS)\nprint(auth._SEALED)\n"
    % POISON_DIR)
r = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                   text=True, encoding="utf-8", errors="replace",
                   cwd=POISON_DIR, timeout=180)
lines = (r.stdout or "").strip().splitlines()
if len(lines) != 5:
    fails.append("could not read FBPoison's auth constants: %s"
                 % ((r.stderr or "")[-200:]))
else:
    salt, pepper, mask, iters, sealed = lines
    check("same salt", str(login_panel._SALT) == salt)
    check("same pepper", str(login_panel._PEPPER) == pepper)
    check("same XOR mask", str(login_panel._MASK) == mask)
    check("same PBKDF2 rounds", str(login_panel._ITERS) == iters,
          "%s vs %s" % (login_panel._ITERS, iters))
    check("same sealed digest", login_panel._SEALED == sealed)

print("\n2) the key itself")
KEY = "!@#!@#!@#"
check("the real key is accepted", login_panel.verify_key(KEY))
# KEY.upper() is NOT a near-miss here - the key is symbols only, so it is
# the same string. Using it would assert that the right key is rejected.
NEAR_MISSES = ("", " ", "wrong", KEY + " ", " " + KEY, KEY[:-1], KEY + "#",
               "!@#!@#!@", "!@#!@#", "#@!#@!#@!", None)
for bad in NEAR_MISSES:
    if login_panel.verify_key(bad):
        fails.append("accepted a wrong key: %r" % bad)
check("%d near-misses all rejected" % len(NEAR_MISSES), True)
check("None does not raise", login_panel.verify_key(None) is False)

print("\n3) the digest is one-way and masked")
d = login_panel._digest(KEY)
check("digest is 32 bytes", len(d) == 32, str(len(d)))
check("the key is not recoverable from the file",
      KEY not in open(login_panel.__file__, encoding="utf-8").read())
check("the sealed blob is not the raw digest",
      login_panel._SEALED.encode() != d)
check("mask/unmask round-trips",
      login_panel._unmask(login_panel._mask(d)) == d)

print("\n4) the panel")
p = login_panel.LoginPanel()
check("starts locked", p.ok is False)
check("input is masked",
      p.field.echoMode() == p.field.EchoMode.Password)
check("no key is pre-filled", p.field.text() == "")

p.field.setText("wrong")
p.attempt()
check("a wrong key is refused", p.ok is False)
check("and it says so", "wrong key" in p.msg.text().lower(), p.msg.text())
check("the field is cleared after a miss", p.field.text() == "")
check("attempts are counted", p.tries == 1, str(p.tries))

p.field.setText("")
p.attempt()
check("an empty key is not counted as an attempt", p.tries == 1,
      str(p.tries))

print("\n5) lockout")
p2 = login_panel.LoginPanel()
for _ in range(login_panel.MAX_TRIES):
    p2.field.setText("nope")
    p2.attempt()
check("locks after %d tries" % login_panel.MAX_TRIES,
      not p2.field.isEnabled() and not p2.unlock_btn.isEnabled())
check("says it is locked", "locked" in p2.msg.text().lower(), p2.msg.text())
check("still not unlocked", p2.ok is False)

print("\n6) the right key opens it")
p3 = login_panel.LoginPanel()
p3.field.setText(KEY)
p3.attempt()
check("unlocked", p3.ok is True)
check("and says so", "unlock" in p3.msg.text().lower(), p3.msg.text())

print("\n7) it actually paints")
# Not theatre: drawRoundedRect with four bare floats has no matching overload,
# and the TypeError raised inside paintEvent ABORTS the process rather than
# propagating - the window died the first time it drew itself, while every
# other test here still passed, because none of them painted.
p4 = login_panel.LoginPanel()
p4.show()
app.processEvents()
pix = p4.grab()
check("the panel renders without dying",
      pix.width() == 400 and pix.height() == 268,
      "%dx%d" % (pix.width(), pix.height()))
p4.field.setText("nope")
p4.attempt()
app.processEvents()
pix2 = p4.grab()
check("and still renders after a failed attempt",
      pix2.width() == 400 and pix2.height() == 268)

print("\n8) it does not drag the other app in with it")
src = open(login_panel.__file__, encoding="utf-8").read()
for banned in ("import auth", "from auth", "import gui", "from gui",
               "import core", "from core"):
    check("does not %s" % banned, banned not in src)

print("\n" + ("PASS: new panel, same lock"
              if not fails else "FAIL (%d):\n  " % len(fails)
              + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
