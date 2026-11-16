# -*- coding: utf-8 -*-
"""Imaginary phones that behave like real ones.

Real fleets are not 40 identical healthy devices. These generators reproduce
what actually shows up in `adb devices -l`: two powered hubs, phones that went
offline, one that lost its authorisation, a wifi/TCP device with a colon in its
serial, an emulator, mixed models, and hubs nested several levels deep.
"""
import random

MODELS = ["CPH2631", "CPH2565", "SM_A155F", "Infinix_X6831",
          "TECNO_KI5q", "RMX3830", "V2352", "M2101K6G"]


def phone(serial, hub=None, port=1, state="device", model=None, depth=2):
    """One line of `adb devices -l`. `hub` None means no usb field at all."""
    usb = ""
    if hub:
        tail = ".".join(str(port) for _ in range(max(1, depth - 1)))
        usb = f"{hub}.{tail}" if tail else str(hub)
    return {"serial": serial, "state": state, "usb": usb,
            "model": model or random.choice(MODELS)}


def two_boxes(n_a=20, n_b=20, hub_a="1-1", hub_b="2-4", seed=7):
    """The user's actual setup: two boxes of 20 on two powered hubs."""
    rnd = random.Random(seed)
    out = []
    for i in range(n_a):
        out.append(phone("BOXA%04X" % rnd.randrange(0xFFFF), hub_a, i + 1,
                         model=rnd.choice(MODELS)))
    for i in range(n_b):
        out.append(phone("BOXB%04X" % rnd.randrange(0xFFFF), hub_b, i + 1,
                         model=rnd.choice(MODELS)))
    # serials must be unique even if the RNG collides
    seen, uniq = set(), []
    for idx, d in enumerate(out):
        s = d["serial"]
        while s in seen:
            s = "%s%d" % (d["serial"], idx)
        seen.add(s)
        d["serial"] = s
        uniq.append(d)
    return uniq


def messy_fleet():
    """Everything that goes wrong at once, in one fleet."""
    out = two_boxes(9, 9)
    out[0]["state"] = "offline"                 # phone crashed
    out[1]["state"] = "unauthorized"            # RSA prompt not accepted
    out.append(phone("192.168.1.55:5555", None, state="device"))   # wifi adb
    out.append(phone("emulator-5554", None, state="device",
                     model="sdk_gphone64"))
    out.append(phone("DEEPHUB01", "1-1", 3, depth=4))  # 1-1.3.3.3
    out.append(phone("NOUSB0001", None))        # no usb field at all
    return out


def one_box(n=20, hub="1-1"):
    return [phone("SOLO%03d" % i, hub, i + 1) for i in range(n)]


def three_hubs(per=8):
    out = []
    for h, name in enumerate(("1-1", "2-4", "3-2")):
        out += [phone("H%d_%03d" % (h, i), name, i + 1) for i in range(per)]
    return out


def flat_no_usb(n=40):
    return [phone("FLAT%04d" % i, None) for i in range(n)]


def to_adb_lines(devices):
    """Render exactly like `adb devices -l` does."""
    lines = ["List of devices attached"]
    for d in devices:
        parts = [d["serial"], d["state"]]
        if d.get("usb"):
            parts.append("usb:" + d["usb"])
        if d.get("model"):
            parts.append("product:" + d["model"].lower())
            parts.append("model:" + d["model"])
            parts.append("device:" + d["model"].lower())
        parts.append("transport_id:%d" % (hash(d["serial"]) % 900 + 10))
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"
