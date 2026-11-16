# -*- coding: utf-8 -*-
"""Which phone box each device belongs to, and therefore which app drives it.

Each physical box of 20 phones can run FBPoison (reactions) or FBVenom
(comments). Nothing here touches either app's code: the split is
enforced in the child process by filtering core.list_devices(), so each app
simply never sees the other box's phones.

Discovery names a phonebox immediately, then finishes capturing it at 20
phones. Complete USB hubs are captured first; remaining phones use their
first-seen scan order, sorting new serials within a scan. Group names,
membership, and app choices survive unplugging or rescanning the boxes.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)


def _find_config():
    """The one boxes.json shared by the hub and both child apps.

    Frozen into an .exe, __file__ points at a temp extraction dir, so the plain
    HERE/boxes.json would be a throwaway copy that never persists. Walk up from
    the exe instead - checking boxes.json and FBVenoison/boxes.json at each level,
    exactly how the child apps resolve it - so the hub reads and writes the same
    file. FBVENOISON_CONFIG overrides everything.
    """
    override = os.environ.get("FBVENOISON_CONFIG", "").strip()
    if override:
        return override
    roots = [os.path.dirname(os.path.abspath(sys.argv[0] or ".")),
             os.path.dirname(os.path.abspath(sys.executable)), HERE]
    for root in roots:
        directory = os.path.abspath(root)
        for _ in range(6):
            for parts in (("boxes.json",), ("FBVenoison", "boxes.json")):
                path = os.path.join(directory, *parts)
                if os.path.isfile(path):
                    return path
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
    return os.path.join(base, "boxes.json")


CONFIG_PATH = _find_config()

POISON = "poison"
VENOM = "venom"
UNASSIGNED = "none"
PHONEBOX_SIZE = 20
MAX_APP_TARGET = 1000
_ROLES = (POISON, VENOM, UNASSIGNED)


def app_targets(cfg):
    """Return each app's requested ready-phone count, defaulting to 20/20.

    App capacity is independent of physical phonebox membership. Invalid old
    configuration values use the default; numeric counts are bounded to the
    same 0..1000 range exposed by the hub's capacity controls.
    """
    raw = cfg.get("targets") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    targets = {}
    for role in (POISON, VENOM):
        value = raw.get(role, PHONEBOX_SIZE)
        if isinstance(value, bool):
            value = PHONEBOX_SIZE
        elif isinstance(value, str):
            try:
                value = int(value.strip()) if re.fullmatch(r"[+-]?\d+", value.strip()) else PHONEBOX_SIZE
            except ValueError:
                value = PHONEBOX_SIZE
        elif not isinstance(value, int):
            value = PHONEBOX_SIZE
        targets[role] = max(0, min(MAX_APP_TARGET, value))
    return targets

def _app_dir(name, entry):
    """Where an app lives.

    Bundled inside FBVenoison wins, so the suite is self-contained and can be
    copied or shipped as one folder. A sibling copy in tools/ is the fallback,
    which is how it used to be laid out. A folder only counts if it actually
    has the entry point - an empty or half-copied directory must not silently
    win over a working one.
    """
    for base in (HERE, TOOLS):
        cand = os.path.join(base, name)
        if os.path.isfile(os.path.join(cand, entry)):
            return cand
    return os.path.join(HERE, name)          # report the bundled path in errors


APPS = {
    POISON: {
        "label": "FBPOISON",
        "job": "reactions",
        "dir": _app_dir("FBPoison", "fbpoison.py"),
        "entry": "fbpoison.py",
    },
    VENOM: {
        "label": "FBVENOM",
        "job": "comments",
        "dir": _app_dir("FBVenom", "fbvenom.py"),
        "entry": "fbvenom.py",
    },
}

# Resolved per call, not at import: ADB_PATH set after this module loads (or
# changed while running) must still be honoured. Assigning boxes.ADB directly
# still works and wins.
ADB = None


def adb_path():
    return ADB or os.environ.get("ADB_PATH") or "adb"


def _no_window():
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def list_devices_verbose():
    """[{serial, state, usb, model}] straight from `adb devices -l`."""
    try:
        p = subprocess.run([adb_path(), "devices", "-l"], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=40,
                           **_no_window())
        out = p.stdout or ""
    except Exception as e:
        print(f"[boxes] adb failed: {e}")
        return []
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        usb = model = ""
        for token in parts[2:]:
            if token.startswith("usb:"):
                usb = token[4:]
            elif token.startswith("model:"):
                model = token[6:]
        devices.append({"serial": serial, "state": state,
                        "usb": usb, "model": model})
    return devices


def hub_of(usb):
    """The physical hub a device hangs off: '1-1.4.3' -> '1-1.4'.

    One phone box is one powered hub, so its phones share this prefix.
    """
    if not isinstance(usb, str) or not usb:
        return ""
    base = usb.split(":")[0]
    bits = base.split(".")
    return ".".join(bits[:-1]) if len(bits) > 1 else base


def _serials(value, seen=None):
    """Keep valid serials once, in their saved order."""
    seen = seen if seen is not None else set()
    out = []
    if isinstance(value, list):
        for serial in value:
            if isinstance(serial, str) and serial.strip() and serial not in seen:
                seen.add(serial)
                out.append(serial)
    return out


def _normalise(cfg):
    """Sanitize persisted fields while preserving unrelated config fields.

    Membership is exclusive: the first valid group owns a serial, then the
    first pending bucket. Old groups shorter than 20 remain open for capture;
    complete groups keep their membership until explicit realignment.
    """
    out = dict(cfg) if isinstance(cfg, dict) else {}
    raw_assign = out.get("assign")
    out["assign"] = {
        serial: role for serial, role in raw_assign.items()
        if isinstance(serial, str) and serial.strip() and role in _ROLES
    } if isinstance(raw_assign, dict) else {}
    out["auto"] = bool(out.get("auto", True))
    out["targets"] = app_targets(out)
    seen, ids, groups = set(), set(), []
    raw_groups = out.get("groups")
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            ident = group.get("id")
            if not isinstance(ident, str) or not ident.strip() or ident in ids:
                continue
            members = _serials(group.get("serials"), seen)
            if not members:
                continue
            name = group.get("name")
            # Invalid names get a stable ID label, rather than a scan-derived
            # number. Valid saved names are never regenerated.
            name = name.strip()[:80] if isinstance(name, str) else ""
            groups.append({"id": ident, "name": name or ident,
                           "serials": members,
                           "capturing": bool(group.get("capturing", len(members) < PHONEBOX_SIZE))
                           and len(members) < PHONEBOX_SIZE})
            ids.add(ident)
    out["groups"] = groups
    pending = {}
    raw_pending = out.get("pending")
    if isinstance(raw_pending, dict):
        for hub, values in raw_pending.items():
            if isinstance(hub, str):
                members = _serials(values, seen)
                if members:
                    pending[hub] = members
    out["pending"] = pending
    pending_members = {serial for members in pending.values() for serial in members}
    order = [serial for serial in _serials(out.get("pending_order"))
             if serial in pending_members]
    ordered = set(order)
    # Older configs have only per-hub queues. Their saved insertion order is
    # the deterministic starting order for a cross-hub capture queue.
    for members in pending.values():
        for serial in members:
            if serial not in ordered:
                order.append(serial)
                ordered.add(serial)
    out["pending_order"] = order
    for serial in seen:
        out["assign"].setdefault(serial, UNASSIGNED)
    return out


def load():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            cfg = saved
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[boxes] {CONFIG_PATH} unreadable ({e}); starting fresh")
    return _normalise(cfg)


def save(cfg, strict=False):
    """Replace config atomically; strict callers receive write failures.

    The default keeps the historical log-and-return behavior. Strict mode is
    useful when a caller must commit its in-memory change only after saving.
    """
    temp_path = None
    try:
        directory = os.path.dirname(os.path.abspath(CONFIG_PATH))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=directory, prefix=".boxes-",
                                         suffix=".tmp", delete=False) as fh:
            temp_path = fh.name
            json.dump(_normalise(cfg), fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, CONFIG_PATH)
        temp_path = None
    except Exception as e:
        if strict:
            raise
        print(f"[boxes] could not write {CONFIG_PATH}: {e}")
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return cfg


def discover_groups(devices, cfg):
    """Capture complete phoneboxes, mutating cfg but never writing to disk.

    Per-hub queues and pending_order keep first-seen order across scans. Full
    new cohorts of 20 take priority over growing an existing partial box.
    Remaining newcomers grow an open box only when an existing member is
    visible and the entire cohort fits. Otherwise they get a new named box
    immediately. A box closes at 20; disconnecting never reopens it.

    Growth inherits an app role only when all existing members share it.
    Disconnected historical pending phones stay saved but are never appended.
    Internal hubs and missing USB are supported; scan one box at a time to
    disambiguate partial cohorts. No disk writes or child-process changes occur.
    """
    cfg.update(_normalise(cfg))
    grouped = {serial for group in cfg["groups"] for serial in group["serials"]}
    visible = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        serial = device.get("serial")
        if not isinstance(serial, str) or not serial.strip():
            continue
        hub = hub_of(device.get("usb", ""))
        # Duplicate ADB rows cannot count twice. Prefer real USB topology,
        # choosing deterministically if conflicting paths are reported.
        if serial not in visible or (not bool(hub), hub) < (
                not bool(visible[serial]), visible[serial]):
            visible[serial] = hub
        cfg["assign"].setdefault(serial, UNASSIGNED)

    pending = cfg["pending"]
    owner = {serial: hub for hub, serials in pending.items() for serial in serials}
    for serial in sorted(visible):
        if serial in grouped:
            continue
        hub = visible[serial]
        if serial in owner:
            if owner[serial] == hub:
                continue
            pending[owner[serial]].remove(serial)
        else:
            cfg["pending_order"].append(serial)
        pending.setdefault(hub, []).append(serial)
        owner[serial] = hub

    history = cfg.get("alignment_history")
    previous = cfg["groups"] + (history if isinstance(history, list) else [])
    highest = max((int(match.group(1)) for group in previous
                   if isinstance(group, dict) and isinstance(group.get("id"), str)
                   for match in [re.fullmatch(r"box-(\d+)", group["id"])]
                   if match), default=0)
    created = []
    consumed = set()

    def capture(members):
        nonlocal highest
        highest += 1
        group = {"id": "box-%d" % highest,
                 "name": "Phonebox %d" % highest,
                 "serials": members,
                 "capturing": len(members) < PHONEBOX_SIZE}
        cfg["groups"].append(group)
        created.append(group)
        consumed.update(members)

    for hub in sorted(pending):
        candidates = [serial for serial in pending[hub]
                      if serial in visible and visible[serial] == hub]
        for offset in range(0, len(candidates) - PHONEBOX_SIZE + 1, PHONEBOX_SIZE):
            capture(candidates[offset:offset + PHONEBOX_SIZE])
    candidates = [serial for serial in cfg["pending_order"]
                  if serial in visible and serial not in consumed]
    for offset in range(0, len(candidates) - PHONEBOX_SIZE + 1, PHONEBOX_SIZE):
        capture(candidates[offset:offset + PHONEBOX_SIZE])
    remaining = [serial for serial in candidates if serial not in consumed]
    if remaining:
        for group in cfg["groups"]:
            members = group["serials"]
            if (not group["capturing"] or not any(serial in visible for serial in members)
                    or len(members) + len(remaining) > PHONEBOX_SIZE):
                continue
            roles = {cfg["assign"].get(serial, UNASSIGNED) for serial in members}
            if len(roles) == 1 and next(iter(roles)) in (POISON, VENOM):
                role = next(iter(roles))
                for serial in remaining:
                    if cfg["assign"].get(serial, UNASSIGNED) == UNASSIGNED:
                        cfg["assign"][serial] = role
            members.extend(remaining)
            group["capturing"] = len(members) < PHONEBOX_SIZE
            consumed.update(remaining)
            break
        else:
            capture(remaining)
    for hub in pending:
        pending[hub] = [serial for serial in pending[hub] if serial not in consumed]
    cfg["pending"] = {hub: serials for hub, serials in pending.items() if serials}
    cfg["pending_order"] = [serial for serial in cfg["pending_order"]
                            if serial not in consumed]
    return created


def get_group(cfg, group_id):
    """Return the saved mutable group, or None if its ID is unknown."""
    for group in cfg.get("groups", []):
        if isinstance(group, dict) and group.get("id") == group_id:
            return group
    return None


def rename_group(cfg, group_id, name):
    """Rename a group with a nonblank name of at most 80 characters."""
    group = get_group(cfg, group_id)
    if group is None or not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) > 80:
        return False
    group["name"] = name
    return True


def assign_group(cfg, group_id, role):
    """Move every member, including disconnected phones, to one app role."""
    group = get_group(cfg, group_id)
    if group is None or role not in _ROLES:
        return False
    if not isinstance(cfg.get("assign"), dict):
        cfg["assign"] = {}
    for serial in group["serials"]:
        cfg["assign"][serial] = role
    return True


def realign_group(devices, cfg, group_id):
    """Explicitly align one selected box to the 1..20 visible serials.

    Automatic discovery never repairs closed membership. This user-triggered
    operation preserves ID, name, and saved assignments; records the original
    membership in alignment_history. Overlapping provisional groups can be
    absorbed under this explicit action, with their before-membership saved
    too. Overlap with any other closed group is refused. Disconnected members
    of an absorbed provisional group remain in that group. This only changes
    memory, so the caller controls backup, persistence, and running-app checks.
    """
    visible = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        serial = device.get("serial")
        if isinstance(serial, str) and serial.strip():
            visible.add(serial)
    if not 1 <= len(visible) <= PHONEBOX_SIZE:
        return False
    normalized = _normalise(cfg)
    group = get_group(normalized, group_id)
    if group is None:
        return False
    overlapping = [other for other in normalized["groups"]
                   if other["id"] != group_id and visible.intersection(other["serials"])]
    if any(not other["capturing"] for other in overlapping):
        return False
    history = normalized.get("alignment_history")
    if not isinstance(history, list):
        history = []
    else:
        history = list(history)
    history.append({"id": group["id"], "name": group["name"],
                    "serials": list(group["serials"])})
    for other in overlapping:
        history.append({"id": other["id"], "name": other["name"],
                        "serials": list(other["serials"])})
        other["serials"] = [serial for serial in other["serials"] if serial not in visible]
    normalized["groups"] = [saved for saved in normalized["groups"] if saved["serials"]]
    normalized["alignment_history"] = history
    group["serials"] = sorted(visible)
    group["capturing"] = len(visible) < PHONEBOX_SIZE
    for serial in visible:
        normalized["assign"].setdefault(serial, UNASSIGNED)
    normalized["pending"] = {
        hub: members for hub, serials in normalized["pending"].items()
        if (members := [serial for serial in serials if serial not in visible])}
    normalized["pending_order"] = [serial for serial in normalized["pending_order"]
                                    if serial not in visible]
    cfg.update(normalized)
    return True


def _allocation_devices(devices):
    """Deduplicate valid scan serials, treating conflicting states as unavailable."""
    states = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        serial = device.get("serial")
        if not isinstance(serial, str) or not serial.strip():
            continue
        ready = device.get("state") == "device"
        states[serial] = states.get(serial, True) and ready
    return states


def plan_allocation(devices, cfg, targets=None):
    """Propose a full saved assignment map for the requested ready capacity.

    This is a pure preview: neither config, physical phonebox membership nor
    disk is changed. Only connected, authorized phones (ADB state ``device``)
    can move. Offline, unauthorized and disconnected phones retain their saved
    app so unplugging does not erase their destination.

    Existing ready assignments are retained up to each capacity. Other ready
    phones fill vacancies deterministically by serial; excess phones become
    unassigned. If the fleet is smaller than the total request, ready capacity
    is shared in proportion to the targets (30 phones at 20/20 yield 15/15).
    The hub must check running apps and persist this returned map explicitly.
    """
    normalized = _normalise(cfg)
    requested = app_targets({"targets": targets}) if targets is not None else app_targets(normalized)
    states = _allocation_devices(devices)
    ready = sorted(serial for serial, is_ready in states.items() if is_ready)
    result = dict(normalized["assign"])
    for serial in states:
        result.setdefault(serial, UNASSIGNED)

    quotas = dict(requested)
    total = sum(requested.values())
    if total > len(ready):
        quotas = {role: len(ready) * requested[role] // total
                  for role in (POISON, VENOM)}
        # Largest remainders distribute odd spare phones without exceeding
        # either requested count. Stable role order breaks exact ties.
        remainder_order = sorted((POISON, VENOM),
                                 key=lambda role: -(len(ready) * requested[role] % total))
        for role in remainder_order[:len(ready) - sum(quotas.values())]:
            quotas[role] += 1

    retained, counts = set(), {POISON: 0, VENOM: 0}
    for serial in ready:
        role = result.get(serial, UNASSIGNED)
        if role in counts and counts[role] < quotas[role]:
            retained.add(serial)
            counts[role] += 1
        else:
            result[serial] = UNASSIGNED
    available = iter(serial for serial in ready if serial not in retained)
    for role in (POISON, VENOM):
        for _ in range(quotas[role] - counts[role]):
            result[next(available)] = role
    return result


def allocation_status(devices, assign, targets=None):
    """Summarize ready capacity; unavailable phones never satisfy a target.

    Returns ``poison`` and ``venom`` dictionaries with ``ready``, ``target``,
    ``shortage`` and ``excess``; plus integer ``ready``, ``idle`` and
    ``unavailable`` totals. ``idle`` counts ready phones assigned to neither
    app. Disconnected historical assignments are excluded from all totals.
    """
    requested = app_targets({"targets": targets})
    states = _allocation_devices(devices)
    assign = assign if isinstance(assign, dict) else {}
    ready_roles = [assign.get(serial, UNASSIGNED)
                   for serial, ready in states.items() if ready]
    status = {}
    for role in (POISON, VENOM):
        count = ready_roles.count(role)
        target = requested[role]
        status[role] = {"ready": count, "target": target,
                        "shortage": max(0, target - count),
                        "excess": max(0, count - target)}
    status["ready"] = len(ready_roles)
    status["idle"] = sum(role not in (POISON, VENOM) for role in ready_roles)
    status["unavailable"] = len(states) - len(ready_roles)
    return status


def auto_split(devices):
    """Propose an assignment: two hubs -> two boxes, else split down the middle.

    Returns {serial: POISON|VENOM}.
    """
    if not devices:
        return {}
    groups = {}
    for d in devices:
        groups.setdefault(hub_of(d["usb"]), []).append(d["serial"])
    real = {k: v for k, v in groups.items() if k}
    # Exactly two hubs is the expected two-box setup: bigger hub first so the
    # mapping is stable between runs rather than depending on enumeration order.
    if len(real) == 2:
        order = sorted(real.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        out = {}
        for role, (_hub, serials) in zip((POISON, VENOM), order):
            for s in serials:
                out[s] = role
        return out
    # Otherwise fall back to a straight half-and-half by serial, which at least
    # gives a deterministic 20/20 on a 40-phone fleet.
    serials = sorted(d["serial"] for d in devices)
    half = (len(serials) + 1) // 2
    return {s: (POISON if i < half else VENOM) for i, s in enumerate(serials)}


def resolve(devices, cfg):
    """Final assignment: saved choices win, anything new gets the auto guess."""
    assign = dict(cfg.get("assign") or {})
    # Even a caller with group metadata but no assignment map must never
    # silently split an already captured box between the two applications.
    for group in cfg.get("groups", []):
        for serial in group.get("serials", []):
            assign.setdefault(serial, UNASSIGNED)
    known = {d["serial"] for d in devices}
    fresh = [d for d in devices if d["serial"] not in assign]
    if fresh and cfg.get("auto", True):
        assign.update(auto_split(fresh) if len(fresh) != len(devices)
                      else auto_split(devices))
    return {s: r for s, r in assign.items() if s in known}


def serials_for(role, devices, cfg):
    res = resolve(devices, cfg)
    return sorted(s for s, r in res.items() if r == role)
