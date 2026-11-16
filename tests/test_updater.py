"""Release verification and responsive UI tests; no external service required."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget


PACKAGE = b"a release package fixture, never executed"


def manifest(**changes):
    data = {"version": "3.1.0", "url": "https://releases.example.test/FBVenoison.zip",
            "sha256": hashlib.sha256(PACKAGE).hexdigest(), "notes": "New interface."}
    data.update(changes)
    return data


class Response(io.BytesIO):
    def __init__(self, data, size=None):
        super().__init__(data)
        self.headers = {} if size is None else {"Content-Length": str(size)}

    def geturl(self):
        return "https://releases.example.test/FBVenoison.zip"


class VerificationTests(unittest.TestCase):
    def test_release_ordering(self):
        self.assertTrue(updater.is_newer("3.10.0", "3.9.9"))
        self.assertFalse(updater.is_newer("v3.0", "3.0.0"))
        self.assertTrue(updater.is_newer("3.1.0", "3.1.0-rc.9"))
        self.assertTrue(updater.is_newer("3.1.0-rc.10", "3.1.0-rc.9"))
        self.assertFalse(updater.is_newer("3.1.0+build.2", "3.1.0+build.1"))
        for value in (None, "../evil", "latest", "3.0.0-rc..1"):
            with self.assertRaises(ValueError):
                updater.version_key(value)

    def test_manifest_rejects_unsafe_or_incomplete_releases(self):
        invalid = [dict(url="http://releases.example.test/app.zip"),
                   dict(url="https://name:password@example.test/app.zip"),
                   dict(url="file:///tmp/app.zip"), dict(url="https://example.test/core.py"),
                   dict(url="https://example.test/app.zip#fragment"),
                   dict(sha256="wrong"), dict(notes={}), dict(version="../../outside")]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                updater.validate_manifest(manifest(**changes))
        valid = updater.validate_manifest(manifest(sha256=hashlib.sha256(PACKAGE).hexdigest().upper()))
        self.assertEqual(valid["sha256"], hashlib.sha256(PACKAGE).hexdigest())

    def test_downgrade_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            updater._HTTPSRedirect().redirect_request(None, None, 302, "Found", {},
                                                     "http://example.test/app.zip")

    def test_check_returns_only_newer_release(self):
        for release, expected in (("3.1.0", True), ("3.0.0", False), ("2.9.0", False)):
            response = Response(json.dumps(manifest(version=release)).encode())
            with patch.object(updater, "_open_url", return_value=response):
                result = updater.check_release("https://releases.example.test/manifest.json")
            self.assertEqual(result is not None, expected)
        with patch.object(updater, "_open_url", return_value=Response(b"<html>bad feed</html>")):
            with self.assertRaisesRegex(ValueError, "JSON manifest"):
                updater.check_release("https://releases.example.test/manifest.json")
        with patch.object(updater, "MANIFEST_LIMIT", 10), patch.object(updater, "_open_url", return_value=Response(b"x" * 11)):
            with self.assertRaisesRegex(ValueError, "too large"):
                updater.check_release("https://releases.example.test/manifest.json")

    def test_verified_download_is_atomic_and_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = []
            with patch.object(updater, "_open_url", return_value=Response(PACKAGE, len(PACKAGE))):
                output = Path(updater.download_release(manifest(), directory, progress.append))
            self.assertEqual(output.read_bytes(), PACKAGE)
            self.assertEqual(output.parent, Path(directory))
            self.assertEqual(progress[-1], 100)
            self.assertFalse(list(Path(directory).glob("*.part")))
            # A failed replacement cannot overwrite an already verified file.
            with patch.object(updater, "_open_url", return_value=Response(b"corrupt")):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    updater.download_release(manifest(), directory)
            self.assertEqual(output.read_bytes(), PACKAGE)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_incomplete_and_oversized_downloads_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            for response, limit in ((Response(PACKAGE, len(PACKAGE) + 1), len(PACKAGE) + 2),
                                    (Response(PACKAGE), len(PACKAGE) - 1)):
                with patch.object(updater, "_open_url", return_value=response), patch.object(updater, "PACKAGE_LIMIT", limit):
                    with self.assertRaises(ValueError):
                        updater.download_release(manifest(), directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_settings_persist_without_touching_device_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FBVENOISON_UPDATE_DIR": directory}, clear=False):
            with patch.dict(os.environ, {"FBVENOISON_UPDATE_URL": ""}, clear=False):
                updater.save_settings("https://example.test/feed.json")
                self.assertEqual(updater.load_settings()["manifest_url"], "https://example.test/feed.json")
                self.assertFalse((Path(directory) / "boxes.json").exists())
                with self.assertRaises(ValueError):
                    updater.save_settings("http://example.test/feed.json")
                self.assertEqual(updater.load_settings()["manifest_url"], "https://example.test/feed.json")


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_worker(self, dialog):
        deadline = time.monotonic() + 3
        while dialog.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(dialog.worker, "Update worker did not finish")

    def test_asynchronous_check_download_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FBVENOISON_UPDATE_DIR": directory, "FBVENOISON_UPDATE_URL": ""}, clear=False):
            parent = QWidget()
            dialog = updater.open_updater(parent)
            dialog.url_edit.setText("https://example.test/feed.json")
            beats = []
            timer = QTimer()
            timer.timeout.connect(lambda: beats.append(True))
            timer.start(10)
            def delayed_check(url):
                time.sleep(0.12)
                return updater.validate_manifest(manifest())
            with patch.object(updater, "check_release", side_effect=delayed_check):
                dialog.check()
                self.assertFalse(dialog.check_btn.isEnabled())
                self.wait_for_worker(dialog)
            self.assertGreater(len(beats), 1, "UI event loop blocked during check")
            self.assertTrue(dialog.download_btn.isEnabled())
            self.assertIn("3.1.0", dialog.status.text())
            with patch.object(updater, "_open_url", return_value=Response(PACKAGE)):
                dialog.download()
                self.wait_for_worker(dialog)
            self.assertTrue(dialog.open_btn.isEnabled())
            self.assertEqual(Path(dialog.package_path).read_bytes(), PACKAGE)
            self.assertEqual(dialog.progress_bar.value(), 100)
            dialog.close()
            self.assertIs(updater.open_updater(parent), dialog)
            timer.stop()
            dialog.close()
            parent.close()

    def test_worker_error_is_visible_and_retry_enabled(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FBVENOISON_UPDATE_DIR": directory, "FBVENOISON_UPDATE_URL": ""}, clear=False):
            dialog = updater.UpdateDialog()
            dialog.url_edit.setText("https://example.test/feed.json")
            with patch.object(updater, "check_release", side_effect=OSError("Network unavailable")):
                dialog.check()
                self.wait_for_worker(dialog)
            self.assertIn("Network unavailable", dialog.status.text())
            self.assertTrue(dialog.check_btn.isEnabled())
            self.assertFalse(dialog.download_btn.isEnabled())
            dialog.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
