"""Installed suite apps must win over unrelated older home-folder builds."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
import boxes
import hub


class AppResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="venoison_paths_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.installed = self.root / "suite"
        self.home = self.root / "home"
        self.source.mkdir()
        self.installed.mkdir()
        self.home.mkdir()
        self.enterContext(patch.object(hub, "HERE", str(self.source)))
        self.enterContext(patch.object(hub.sys, "executable",
                                      str(self.installed / "fbvenoison.exe")))
        self.enterContext(patch.object(hub.os.path, "expanduser",
                                      return_value=str(self.home)))
        self.enterContext(patch.dict(os.environ, {}, clear=False))
        for name in ("FBVENOISON_POISON_EXE", "FBVENOISON_VENOM_EXE"):
            os.environ.pop(name, None)

    def create(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_source_prefers_bundled_venom(self):
        with patch.object(hub.sys, "frozen", False, create=True):
            bundled = self.create(self.source / "FBVenom/dist/FBVenom.exe")
            self.create(self.home / "FBVenom/dist/FBVenom.exe")
            self.assertEqual(hub.child_exe(boxes.VENOM), bundled)

    def test_frozen_finds_apps_beside_executable(self):
        with patch.object(hub.sys, "frozen", True, create=True):
            installed = self.create(self.installed / "FBVenom/FBVenom.exe")
            self.create(self.source / "FBVenom/dist/FBVenom.exe")
            self.create(self.home / "FBVenom/dist/FBVenom.exe")
            self.assertEqual(hub.child_exe(boxes.VENOM), installed)

    def test_frozen_dist_build_finds_project_sibling(self):
        with patch.object(hub.sys, "frozen", True, create=True), patch.object(
                hub.sys, "executable", str(self.installed / "dist/fbvenoison.exe")):
            installed = self.create(self.installed / "FBPoison/dist/fbpoison.exe")
            self.create(self.home / "FULDIR/dist_new/fbpoison.exe")
            self.assertEqual(hub.child_exe(boxes.POISON), installed)

    def test_explicit_override_wins(self):
        chosen = self.create(self.root / "custom/venom.exe")
        os.environ["FBVENOISON_VENOM_EXE"] = chosen
        self.create(self.source / "FBVenom/dist/FBVenom.exe")
        self.assertEqual(hub.child_exe(boxes.VENOM), chosen)

    def test_missing_override_falls_back_to_bundled(self):
        os.environ["FBVENOISON_VENOM_EXE"] = str(self.root / "missing.exe")
        bundled = self.create(self.source / "FBVenom/dist/FBVenom.exe")
        self.assertEqual(hub.child_exe(boxes.VENOM), bundled)

    def test_does_not_guess_versioned_builds(self):
        self.create(self.source / "FBPoison/dist/fbpoison_20260805.exe")
        self.assertIsNone(hub.child_exe(boxes.POISON))

    def test_unknown_app_is_rejected(self):
        self.assertIsNone(hub.child_exe("unknown"))


if __name__ == "__main__":
    unittest.main()
