"""Apply the hub's device hand-off around an unchanged FBVenom checkout.

Nothing in this module changes files under FBVenom. A standalone upstream
launch does not import this module, and apply() is a no-op without a suite
launch environment. Upstream automation, UI and updater functions are retained.
"""
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import sys
import tempfile

PROTOCOL = 1
METADATA_NAME = "FBVenom.suite-bridge.json"


def managed_by_suite():
    return ("FBVENOISON_DEVICES" in os.environ or
            os.environ.get("FBVENOISON_AUTHED") == "1")


def _same_program(left, right):
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right)))


def _scoped_usb_command(original, allowed):
    """Constrain USB internet discovery and targeted commands to this app."""
    def command(manager, arguments, *args, **kwargs):
        arguments = list(arguments)
        if not arguments:
            return original(manager, arguments, *args, **kwargs)
        adb = _same_program(arguments[0], manager.adb_path)
        relay = _same_program(arguments[0], manager.relay_path)
        if adb and "-s" in arguments:
            index = arguments.index("-s") + 1
            if index >= len(arguments) or arguments[index] not in allowed:
                return 1, "", "Device is not assigned to this FBVenoison session"
        serial_operations = {"install", "uninstall", "start", "stop", "restart", "tunnel", "tunnels", "run"}
        if relay and len(arguments) > 1 and arguments[1] in serial_operations:
            if len(arguments) < 3 or arguments[2] not in allowed:
                return 1, "", "Device is not assigned to this FBVenoison session"
        code, output, error = original(manager, arguments, *args, **kwargs)
        if adb and len(arguments) > 1 and arguments[1] == "devices":
            # Keep ADB's header/status lines while excluding every other phone
            # before upstream _discover_usb() can inspect or operate on it.
            lines = []
            for line in output.splitlines(keepends=True):
                fields = line.split()
                if (not fields or line.startswith("List of devices attached") or
                        line.startswith("*") or fields[0] in allowed):
                    lines.append(line)
            output = "".join(lines)
        return code, output, error
    command.__wrapped__ = original
    return command


class _AuthGateLoader:
    """Keep the upstream loader and reuse the hub gate after auth initializes."""
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module):
        self.original.exec_module(module)
        if os.environ.get("FBVENOISON_AUTHED") == "1":
            module.require_key = lambda *args, **kwargs: True

    def __getattr__(self, name):
        return getattr(self.original, name)


class _AuthGateFinder(importlib.abc.MetaPathFinder):
    """One-use auth hook that delegates to normal and frozen module finders."""
    _fbvenoison_auth_gate = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "auth":
            return None
        # Remove before delegating, so another finder can resolve the unchanged
        # module without recursively asking this hook to resolve it again.
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for finder in tuple(sys.meta_path):
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is not None:
                if spec.loader is not None:
                    spec.loader = _AuthGateLoader(spec.loader)
                return spec
        return None


def _reuse_hub_gate(defer):
    existing = sys.modules.get("auth")
    if existing is not None:
        existing.require_key = lambda *args, **kwargs: True
    elif not defer:
        import auth
        auth.require_key = lambda *args, **kwargs: True
    elif not any(getattr(finder, "_fbvenoison_auth_gate", False)
                 for finder in sys.meta_path):
        # Custom PyInstaller hooks run before package hooks. Do not import
        # auth -> gui -> PyQt6 until Qt's normal runtime setup has completed.
        sys.meta_path.insert(0, _AuthGateFinder())


def apply(defer_auth=False):
    """Apply launch-only device routing before the upstream GUI imports core."""
    if not managed_by_suite():
        return False
    import core

    shared_adb = os.environ.get("ADB_PATH", "").strip()
    if shared_adb:
        core.ADB = shared_adb

    if "FBVENOISON_DEVICES" in os.environ:
        allowed = frozenset(serial.strip() for serial in
                            os.environ["FBVENOISON_DEVICES"].split(",")
                            if serial.strip())
        original = getattr(core, "_fbvenoison_original_list_devices", None)
        if original is None:
            original = core.list_devices
            core._fbvenoison_original_list_devices = original

        def list_devices():
            if not allowed:
                return {}
            return {serial: state for serial, state in original().items()
                    if serial in allowed}

        list_devices.__wrapped__ = original
        core.list_devices = list_devices
        core._fbvenoison_suite_protocol = PROTOCOL

        # USB internet has its own ADB discovery, separate from core's list.
        # Keep its upstream implementation and constrain only its input/output.
        import usb_internet
        manager = usb_internet.USBInternetManager
        original_command = getattr(manager, "_fbvenoison_original_command", None)
        if original_command is None:
            original_command = manager._command
            manager._fbvenoison_original_command = original_command
        manager._command = _scoped_usb_command(original_command, allowed)

    if os.environ.get("FBVENOISON_AUTHED") == "1":
        _reuse_hub_gate(defer_auth)
    return True


def write_build_metadata(executable):
    """Mark only a completed externally managed build, bound to its bytes."""
    executable = Path(executable).resolve()
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    target = executable.parent / METADATA_NAME
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=target.parent,
                                         prefix=".suite-bridge-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"app": "FBVenom", "protocol": PROTOCOL,
                       "sha256": digest.hexdigest()}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
