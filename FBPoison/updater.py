"""Live update client - hot-swaps new logic into the RUNNING process.

Why hot-swap instead of "download, quit, relaunch": a run holds twenty phones,
each part-way through its own list of accounts and links. Restarting throws all
of that away. Python lets us do better - a function that is already executing
keeps the code object it started with until it returns, while every LATER call
resolves through the module dict we just refreshed. So work in flight finishes
on the code it began with, and the next account, link or device picks up the
fix. The run is never interrupted.

Two things make that safe, and both are load-bearing:

  * Live state is carried across. gui.py sets core.STOP_REQUESTED at run start
    (gui.py:2341); a plain importlib.reload() would reset it to None and Abort
    would silently stop working mid-run. _PRESERVE is that guard list.
  * The new source is compiled and executed into a THROWAWAY namespace first.
    A syntax error or an import that blows up never reaches the live module -
    we roll back and keep running on the old code.

gui.py is deliberately NOT hot-swapped: its widgets are already built and wired,
so new UI code cannot be grafted onto a live window. GUI updates stage to disk
and take effect on the next launch.
"""

import atexit
import hashlib
import importlib.util
import json
import os
from private_settings import get_private_setting
import subprocess
import sys
import threading
import urllib.request

from version import APP_VERSION

# Set by release.py at publish time. Overridable for testing against a preview
# deployment without rebuilding.
UPDATE_BASE = os.environ.get("FBPOISON_UPDATE_URL", "https://updatesite-phi.vercel.app").rstrip("/")
UPDATE_TOKEN = get_private_setting("update_token", "FBPOISON_UPDATE_TOKEN")

POLL_SECONDS = 300
HTTP_TIMEOUT = 30

# Modules that can be swapped into a live process, and the state that must
# survive the swap. Anything a `global` statement in core.py writes to, plus
# the hooks gui.py installs from outside.
HOT_MODULES = ("core",)
_PRESERVE = {
    "core": (
        "STOP_REQUESTED",      # gui.py:2341 - losing it breaks Abort mid-run
        "SPEED", "PACE_ACTION", "PACE_LINK", "PACE_ACCOUNT",
        "_geom_cache",         # learned per-device geometry, written in place
        "CURRENT_POST_URL",    # set per link while a run walks the list
        # A downloaded build knows nothing about phone boxes, so letting it
        # replace these would hand this app every phone on the bus again.
        "list_devices", "_box_allow", "_box_config_path", "_BOX_ROLE",
        # Local fixes the update server's builds do not carry, kept across a
        # hot-swap so an update can never silently undo them:
        #   * share-link -> real-permalink navigation (otherwise the run opens
        #     the news feed and scrolls it instead of reaching the post), and
        #   * the pinned adb path (otherwise a version clash with the phone
        #     mirror restarts the adb server and drops phones mid-run).
        "_fb_uri_variants", "_post_view_attempts", "FB_LINK_ALIASES",
        "normalize_phone_url", "_canonicalize_fb_url", "_canonical_from_html",
        "_read_until_canonical", "_accept_resolved_url",
        "_url_resolve_cache", "_url_resolve_lock", "_SHARE_UA",
        "_SHARE_HTML_LIMIT", "_SHARE_TOKEN_RE", "_CANONICAL_HREF_RE",
        "_OG_URL_RE", "_POST_ISH_RE", "_find_adb", "ADB",
    ),
}


def _state_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "FBPoison", "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _installed_path():
    return os.path.join(_state_dir(), "installed.json")


def installed():
    """What was last applied successfully, so a restart keeps the new code."""
    try:
        with open(_installed_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_installed(version, files):
    try:
        with open(_installed_path(), "w", encoding="utf-8") as fh:
            json.dump({"version": version, "files": files}, fh, indent=2)
    except Exception:
        pass


def bootstrap(log=lambda s: None):
    """Re-apply the newest downloaded build before the UI comes up.

    This is what makes an update stick across restarts without ever having to
    overwrite the .exe: the binary stays a plain bootstrap, and the newest
    core.py is loaded out of the update cache on the way in.
    """
    # DISABLED (selfcontained-hub): hot-swapping a server build replaces
    # list_devices/ensure_online with that build's own unpatched versions,
    # which bypass the preserved box filter so the app sees every phone. The
    # patched source already carries every fix; run it unchanged.
    return None
    rec = installed()
    v = rec.get("version")
    if not v or not _newer(v, APP_VERSION):
        return None
    for name, path in (rec.get("files") or {}).items():
        mod = name[:-3] if name.endswith(".py") else None
        if mod in HOT_MODULES and os.path.exists(path):
            try:
                hot_swap(mod, path)
            except Exception as e:
                log("cached build " + v + " rejected, using bundled code ("
                    + str(e) + ")")
                return None
    globals()["APP_VERSION"] = v
    import version as _v
    _v.APP_VERSION = v
    log("running build " + v)
    return v


def _get(path, binary=False):
    req = urllib.request.Request(
        UPDATE_BASE + path,
        headers={"User-Agent": "fbpoison/" + APP_VERSION,
                 "x-update-token": UPDATE_TOKEN})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
    return raw if binary else json.loads(raw.decode("utf-8"))


def _newer(remote, local):
    """True when `remote` is a later version string than `local`."""
    def parts(v):
        out = []
        for chunk in str(v).split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    a, b = parts(remote), parts(local)
    a += [0] * (len(b) - len(a))
    b += [0] * (len(a) - len(b))
    return a > b


def check():
    """Return the manifest when the service is offering something newer."""
    if not UPDATE_BASE:
        return None
    m = _get("/api/manifest")
    return m if _newer(m.get("version", "0"), APP_VERSION) else None


def fetch(manifest):
    """Download every file in the manifest, verifying each hash. Returns paths."""
    dest = os.path.join(_state_dir(), str(manifest["version"]))
    os.makedirs(dest, exist_ok=True)
    got = {}
    for f in manifest.get("files", []):
        name, want = f["name"], f["sha256"]
        path = os.path.join(dest, name)
        cached = False
        if os.path.exists(path):
            with open(path, "rb") as fh:
                cached = hashlib.sha256(fh.read()).hexdigest() == want
        if not cached:
            blob = _get("/api/file?name=" + name, binary=True)
            have = hashlib.sha256(blob).hexdigest()
            if have != want:
                raise ValueError(name + ": sha256 mismatch (got "
                                 + have[:12] + ", want " + want[:12] + ")")
            with open(path, "wb") as fh:
                fh.write(blob)
        got[name] = path
    return got


def _rebind_from_imports(before, mod):
    """Repoint `from core import x` names that other modules already captured.

    gui.py:38 pulls react_all_saved_accounts and friends directly into its own
    namespace. Refreshing core's module dict does nothing for those copies, so
    the GUI would keep calling the OLD functions forever and the update would
    look like it did nothing. Rebind by identity AND name, so an unrelated
    module that merely happens to hold the same object is left alone.
    """
    for m in list(sys.modules.values()):
        d = getattr(m, "__dict__", None)
        if m is None or m is mod or not isinstance(d, dict):
            continue
        for name, old_obj in list(d.items()):
            if name not in before or before[name] is not old_obj:
                continue
            new_obj = mod.__dict__.get(name, old_obj)
            if new_obj is not old_obj:
                try:
                    d[name] = new_obj
                except Exception:
                    pass


def hot_swap(name, path):
    """Swap `path` into the live module `name`. Returns True when applied."""
    old = sys.modules.get(name)
    if old is None:
        return False

    with open(path, "rb") as fh:
        source = fh.read()
    compile(source, path, "exec")          # syntax gate: never exec junk

    spec = importlib.util.spec_from_file_location(name, path)
    staged = importlib.util.module_from_spec(spec)
    sys.modules[name] = staged             # so self-imports resolve to the new one
    try:
        spec.loader.exec_module(staged)    # runs in a throwaway namespace
    except BaseException:
        sys.modules[name] = old            # nothing reached the live module
        raise
    finally:
        if sys.modules.get(name) is staged:
            sys.modules[name] = old

    keep = {k: getattr(old, k) for k in _PRESERVE.get(name, ()) if hasattr(old, k)}
    before = dict(old.__dict__)

    old.__dict__.update(staged.__dict__)   # publish to every `import core` holder
    for k, v in keep.items():              # ...then restore the live state
        setattr(old, k, v)
    _rebind_from_imports(before, old)
    return True


def stage_exe(files):
    """Park a new binary next to the current one and swap it in on exit.

    A running .exe cannot overwrite itself on Windows, so this is the one part
    that genuinely needs a restart. The swap is done by a detached batch file
    that waits for our PID to exit first.
    """
    exe_src = files.get("fbpoison.exe")
    if not exe_src or not getattr(sys, "frozen", False):
        return False
    # normpath matters: these strings are handed to cmd.exe, and `move` reads a
    # forward slash as the start of a switch, so a posix-style path fails
    # silently and the old binary just stays put.
    target = os.path.normpath(sys.executable)
    src = os.path.normpath(exe_src)
    pid = str(os.getpid())
    bat = os.path.join(_state_dir(), "swap.bat")
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        ":wait",
        'tasklist /fi "PID eq ' + pid + '" | find "' + pid + '" >nul',
        "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)",
        # Windows can keep the image locked for a moment after the process is
        # gone, so a single move attempt is not enough.
        "set tries=0",
        ":move",
        'move /y "' + src + '" "' + target + '" >nul 2>&1',
        "if not errorlevel 1 goto done",
        "set /a tries+=1",
        "if !tries! lss 15 (ping -n 2 127.0.0.1 >nul & goto move)",
        ":done",
        'start "" "' + target + '"',
        'del "%~f0"',
    ]
    # newline="" or Python turns each \r\n into \r\r\n, which cmd mis-parses
    with open(bat, "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")

    def _swap():
        # DETACHED_PROCESS, and - the part that actually matters - its own null
        # stdio. Letting the batch inherit our handles looks harmless and is
        # not: we are mid-exit when this fires, so tasklist/find/ping come up
        # with no usable stdio and cmd dies instantly without swapping
        # anything. Measured on this machine: inherited handles never swapped
        # once, DEVNULL swapped every time.
        subprocess.Popen(["cmd", "/c", bat],
                         creationflags=0x00000008,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)

    atexit.register(_swap)
    return True


def apply_update(manifest, log=lambda s: None):
    """Fetch and apply one update. Returns a short human-readable outcome."""
    files = fetch(manifest)
    swapped, staged = [], []
    for name, path in files.items():
        mod = name[:-3] if name.endswith(".py") else None
        if mod in HOT_MODULES:
            try:
                if hot_swap(mod, path):
                    swapped.append(name)
            except Exception as e:
                log("update: " + name + " rejected, staying on "
                    + APP_VERSION + " (" + str(e) + ")")
                return "rejected:" + name
        else:
            staged.append(name)
    if stage_exe(files):
        staged.append("fbpoison.exe")

    # Record what is now live so the next launch starts on it instead of
    # falling back to whatever the .exe was built with.
    _write_installed(manifest["version"],
                     {n: p for n, p in files.items() if n.endswith(".py")})

    import version as _v                    # keep the reported build in step
    _v.APP_VERSION = manifest["version"]
    globals()["APP_VERSION"] = manifest["version"]

    if swapped and staged:
        return "live: " + ", ".join(swapped) + " | next launch: " + ", ".join(staged)
    if swapped:
        return "live: " + ", ".join(swapped)
    return "next launch: " + ", ".join(staged) if staged else "nothing to do"


class Updater(threading.Thread):
    """Background poller. `log` receives one-line status strings for the UI."""

    def __init__(self, log=lambda s: None, interval=POLL_SECONDS):
        super().__init__(daemon=True)
        self.log = log
        self.interval = interval
        self._stop = threading.Event()
        self.last_error = None

    def stop(self):
        self._stop.set()

    def run(self):
        if not UPDATE_BASE:
            return
        while not self._stop.is_set():
            try:
                m = check()
                if m:
                    self.log("update " + m["version"] + " found - applying live")
                    self.log("update " + m["version"] + ": "
                             + apply_update(m, self.log))
                self.last_error = None
            except Exception as e:
                if str(e) != str(self.last_error):
                    self.log("update check failed: " + str(e))
                self.last_error = e
            self._stop.wait(self.interval)


def start(log=lambda s: None, interval=POLL_SECONDS):
    return None  # DISABLED (selfcontained-hub): no polling / hot-swap
    u = Updater(log=log, interval=interval)
    u.start()
    return u
