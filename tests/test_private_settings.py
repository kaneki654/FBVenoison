"""Private credentials remain local and environment overrides stay explicit."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "tested_poison_private_settings", APP / "FBPoison/private_settings.py")
private = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private)


class PrivateSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "poison_private.json"
        self.enterContext(patch.dict(os.environ, {
            "FBPOISON_PRIVATE_CONFIG": str(self.file)}, clear=True))

    def test_local_file_and_environment_precedence(self):
        self.file.write_text(json.dumps({"groq_api_key": "local-fixture"}))
        self.assertEqual(private.get_private_setting("groq_api_key", "GROQ_API_KEY"), "local-fixture")
        os.environ["GROQ_API_KEY"] = "environment-fixture"
        self.assertEqual(private.get_private_setting("groq_api_key", "GROQ_API_KEY"), "environment-fixture")
        os.environ["GROQ_API_KEY"] = ""
        self.assertEqual(private.get_private_setting("groq_api_key", "GROQ_API_KEY"), "")

    def test_missing_malformed_and_non_string_settings_are_empty(self):
        for content in (None, "{invalid", "[]", '{"update_token": 42}'):
            if content is not None:
                self.file.write_text(content)
            self.assertEqual(private.get_private_setting("update_token", "FBPOISON_UPDATE_TOKEN"), "")

    def test_source_and_frozen_locations(self):
        os.environ.pop("FBPOISON_PRIVATE_CONFIG")
        with patch.object(private.sys, "frozen", False, create=True):
            self.assertEqual(private.settings_path(), APP / "FBPoison/poison_private.json")
        executable = Path(self.temp.name) / "fbpoison.exe"
        with patch.object(private.sys, "frozen", True, create=True), \
                patch.object(private.sys, "executable", str(executable)):
            self.assertEqual(private.settings_path(), self.file)


if __name__ == "__main__":
    unittest.main()
