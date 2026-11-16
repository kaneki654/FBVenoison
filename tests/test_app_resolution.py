"""Installed suite apps must win over unrelated older home-folder builds."""
import os
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
        for name in ("FBVENOISON_POISON_EXE", "FBVENOISON_VENOM_EXE", "POISON", "VENOM"):
            os.environ.pop(name, None)
        self.registry = self.enterContext(patch.object(
            hub, "_persistent_executable_values", return_value=[]))

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

    def test_registered_aliases_win_over_bundled_apps(self):
        for role, alias in ((boxes.POISON, "POISON"), (boxes.VENOM, "VENOM")):
            with self.subTest(role=role):
                registered = self.create(self.root / "registered" / (alias + ".exe"))
                os.environ[alias] = registered
                self.assertEqual(hub.child_exe(role), registered)
        self.registry.assert_not_called()

    def test_explicit_override_wins_over_alias_and_registry(self):
        chosen = self.create(self.root / "explicit/venom.exe")
        os.environ["FBVENOISON_VENOM_EXE"] = chosen
        os.environ["VENOM"] = self.create(self.root / "alias/venom.exe")
        self.registry.return_value = [self.create(self.root / "persistent/venom.exe")]
        self.assertEqual(hub.child_exe(boxes.VENOM), chosen)
        self.registry.assert_not_called()

    def test_invalid_explicit_override_can_use_alias(self):
        os.environ["FBVENOISON_POISON_EXE"] = str(self.root / "removed.exe")
        chosen = self.create(self.root / "alias/poison.exe")
        os.environ["POISON"] = chosen
        self.assertEqual(hub.child_exe(boxes.POISON), chosen)

    def test_quoted_environment_paths_are_expanded(self):
        chosen = self.create(self.root / "folder with spaces/venom.exe")
        os.environ["TASK_BUILD_DIRECTORY"] = str(Path(chosen).parent)
        os.environ["VENOM"] = '  "$TASK_BUILD_DIRECTORY' + os.sep + 'venom.exe"  '
        self.assertEqual(hub.child_exe(boxes.VENOM), chosen)

    def test_persisted_paths_cover_missing_and_stale_process_environment(self):
        chosen = self.create(self.root / "persistent/venom.exe")
        self.registry.return_value = [str(self.root / "stale-user.exe"), chosen]
        os.environ["FBVENOISON_VENOM_EXE"] = str(self.root / "old-process.exe")
        self.assertEqual(hub.child_exe(boxes.VENOM), chosen)
        self.registry.assert_called_once_with(("FBVENOISON_VENOM_EXE", "VENOM"))

    def test_invalid_persisted_values_fall_back_to_bundled(self):
        self.registry.return_value = [None, 123, "", '"missing.exe"']
        chosen = self.create(self.source / "FBVenom/dist/FBVenom.exe")
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
        self.registry.assert_not_called()


class WindowsRegistryTests(unittest.TestCase):
    def test_user_values_precede_machine_values(self):
        names = ("FBVENOISON_VENOM_EXE", "VENOM")
        stored = {("user", names[0]): ("user-override.exe", 1),
                  ("user", names[1]): ("user-alias.exe", 2),
                  ("machine", names[0]): ("machine-override.exe", 1),
                  ("machine", names[1]): ("machine-alias.exe", 1)}
        registry = SimpleNamespace(
            HKEY_CURRENT_USER="user", HKEY_LOCAL_MACHINE="machine",
            KEY_READ=1, REG_SZ=1, REG_EXPAND_SZ=2,
            OpenKey=Mock(side_effect=lambda root, *_: nullcontext(root)),
            QueryValueEx=Mock(side_effect=lambda key, name: stored[(key, name)]))
        with patch.object(hub.sys, "platform", "win32"), patch.dict(
                sys.modules, {"winreg": registry}):
            self.assertEqual(hub._persistent_executable_values(names), [
                "user-override.exe", "user-alias.exe",
                "machine-override.exe", "machine-alias.exe"])

    def test_unreadable_user_key_does_not_hide_machine_value(self):
        def open_key(root, *_):
            if root == "user":
                raise PermissionError("fixture")
            return nullcontext(root)

        def query(key, name):
            if name == "VENOM":
                return "machine.exe", 1
            raise FileNotFoundError(name)

        registry = SimpleNamespace(
            HKEY_CURRENT_USER="user", HKEY_LOCAL_MACHINE="machine",
            KEY_READ=1, REG_SZ=1, REG_EXPAND_SZ=2,
            OpenKey=Mock(side_effect=open_key), QueryValueEx=Mock(side_effect=query))
        with patch.object(hub.sys, "platform", "win32"), patch.dict(
                sys.modules, {"winreg": registry}):
            self.assertEqual(hub._persistent_executable_values(
                ("FBVENOISON_VENOM_EXE", "VENOM")), ["machine.exe"])

    def test_non_windows_does_not_read_registry(self):
        registry = SimpleNamespace(OpenKey=Mock())
        with patch.object(hub.sys, "platform", "linux"), patch.dict(
                sys.modules, {"winreg": registry}):
            self.assertEqual(hub._persistent_executable_values(("VENOM",)), [])
        registry.OpenKey.assert_not_called()


if __name__ == "__main__":
    unittest.main()
