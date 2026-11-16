#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A stand-in for the adb binary, so tests exercise the REAL code path.

boxes.list_devices_verbose() shells out to adb and parses its text. Pointing
ADB_PATH at this (via fake_adb.bat) means the subprocess call, the encoding
and the parsing are all genuinely tested - not bypassed by a monkeypatch.

The fleet to serve is read from the JSON file named by FAKE_ADB_FLEET.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_fleet


def main():
    args = sys.argv[1:]
    path = os.environ.get("FAKE_ADB_FLEET", "")
    devices = []
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                devices = json.load(fh)
        except Exception as e:
            sys.stderr.write("fake_adb: bad fleet file: %s\n" % e)
            return 1

    if args and args[0] == "devices":
        if "-l" in args:
            sys.stdout.write(fake_fleet.to_adb_lines(devices))
        else:
            out = ["List of devices attached"]
            out += ["%s\t%s" % (d["serial"], d["state"]) for d in devices]
            sys.stdout.write("\n".join(out) + "\n")
        return 0

    # anything else: behave like a quiet, successful adb
    return 0


if __name__ == "__main__":
    sys.exit(main())
