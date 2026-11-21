"""The imported FBVenom files must remain an exact upstream snapshot."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class UpstreamSnapshotTests(unittest.TestCase):
    def test_upstream_files_match_recorded_sha256(self):
        record = json.loads((ROOT / "docs/FBVenom-import.json").read_text())
        self.assertFalse(record["upstream_code_modified"])
        self.assertEqual(record["source"], "https://github.com/kaneki654/FBVenom.git")
        self.assertTrue(record["files"])
        checked = 0
        for item in record["files"]:
            path = ROOT / "FBVenom" / item["path"]
            if item.get("optional_runtime_artifact") and not path.exists():
                continue
            with self.subTest(path=item["path"]):
                self.assertTrue(path.is_file(), "Upstream file is missing")
                if path.is_file():
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"],
                                     "Upstream source was edited; adapt it outside FBVenom instead")
                    checked += 1
        self.assertGreater(checked, 20)

    def test_integration_lives_outside_upstream_and_updater_is_unmodified(self):
        self.assertTrue((ROOT / "venom_bridge.py").is_file())
        self.assertTrue((ROOT / "integration/venom_runtime_hook.py").is_file())
        self.assertFalse((ROOT / "FBVenom/suite_bridge.py").exists())
        updater = (ROOT / "FBVenom/updater.py").read_text()
        self.assertIn("def install(staged_path, restart=True)", updater)
        self.assertIn("def downgrade(restart=True)", updater)
        self.assertNotIn("managed by FBVenoison", updater)
        entry = (ROOT / "FBVenom/fbvenom.py").read_text()
        self.assertNotIn("venom_bridge", entry)
        self.assertNotIn("suite_bridge", entry)


if __name__ == "__main__":
    unittest.main()
