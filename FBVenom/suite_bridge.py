"""Adapt the imported FBVenom build to the suite's device hand-off.

Standalone FBVenom keeps its normal device discovery and authentication. The
hub passes an explicit device snapshot and completed key gate to its child;
this bridge applies those only when their environment variables are present.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

PROTOCOL = 1
METADATA_NAME = "FBVenom.suite-bridge.json"


def managed_by_suite():
    return ("FBVENOISON_DEVICES" in os.environ or
            os.environ.get("FBVENOISON_AUTHED") == "1")


def apply():
    """Apply the launch snapshot before either GUI or CLI imports its helpers."""
    if not managed_by_suite():
        return False

    import core

    # ADB_PATH pins the hub, mirror and child apps to the same ADB server.
    shared_adb = os.environ.get("ADB_PATH", "").strip()
    if shared_adb:
        core.ADB = shared_adb

    if "FBVENOISON_DEVICES" in os.environ:
        allowed = frozenset(serial.strip() for serial in
                            os.environ["FBVENOISON_DEVICES"].split(",")
                            if serial.strip())
        # Keep the first function, which may already be the source bootstrap's
        # filter. Reapplying the bridge must not stack stale launch snapshots.
        original = getattr(core, "_fbvenoison_original_list_devices", None)
        if original is None:
            original = core.list_devices
            core._fbvenoison_original_list_devices = original

        def list_devices():
            return {serial: state for serial, state in original().items()
                    if serial in allowed}

        list_devices.__wrapped__ = original
        core.list_devices = list_devices
        core._fbvenoison_suite_protocol = PROTOCOL

    if os.environ.get("FBVENOISON_AUTHED") == "1":
        import auth
        auth.require_key = lambda *args, **kwargs: True
    return True


def write_build_metadata(executable):
    """Bind a completed bridge-aware binary to its capability sidecar.

    Called by the Windows build spec only after PyInstaller finishes EXE.
    Never infer capability merely from a Python bridge beside an old binary.
    """
    executable = Path(executable).resolve()
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    metadata = {"app": "FBVenom", "protocol": PROTOCOL,
                "sha256": digest.hexdigest()}
    target = executable.parent / METADATA_NAME
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=target.parent,
                                         prefix=".suite-bridge-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(metadata, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
