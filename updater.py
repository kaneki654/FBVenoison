"""Verified, asynchronous release downloads for the whole FBVenoison suite.

A release service is deliberately not assumed. Configure an HTTPS manifest URL
in the dialog or set FBVENOISON_UPDATE_URL. The manifest is a JSON object with
version, url, sha256, and optional notes. Packages are staged, never executed
or extracted into a running application. Back up local configuration and stop
the child apps before replacing an installation with the downloaded release.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request

from PyQt6.QtCore import QThread, QUrl, pyqtSignal, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QPlainTextEdit, QProgressBar)

from version import VERSION

HTTP_TIMEOUT = 30
MANIFEST_LIMIT = 1024 * 1024
PACKAGE_LIMIT = 2 * 1024 * 1024 * 1024
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SUFFIXES = (".tar.gz", ".tar.xz", ".zip", ".tgz", ".exe", ".msi", ".appimage", ".deb", ".rpm")


def version_key(value):
    """Comparable numeric release and SemVer prerelease key; reject bad input."""
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Release version must be a version string.")
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Release version must use numbers such as 3.1.0 or 3.1.0-rc.1.")
    release = tuple(int(n) for n in match[1].split("."))
    release += (0,) * (4 - len(release))
    prerelease = match[2]
    identifiers = ()
    if prerelease is not None:
        if any(not item for item in prerelease.split(".")):
            raise ValueError("Invalid prerelease version.")
        identifiers = tuple((0, int(item)) if item.isdigit() else (1, item)
                            for item in prerelease.split("."))
    return release, prerelease is None, identifiers


def is_newer(remote, local=VERSION):
    return version_key(remote) > version_key(local)


def https_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Enter a valid HTTPS URL.")
    value = value.strip()
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme.lower() != "https" or not parts.hostname or
            parts.username or parts.password or parts.fragment or
            any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("Updates require an HTTPS URL without credentials or fragments.")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("Invalid HTTPS port.") from exc
    return value


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url):
    req = urllib.request.Request(https_url(url), headers={
        "User-Agent": "FBVenoison/" + VERSION,
        "Accept": "application/json, application/octet-stream;q=0.9"})
    response = urllib.request.build_opener(_HTTPSRedirect()).open(req, timeout=HTTP_TIMEOUT)
    try:
        https_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def validate_manifest(data):
    if not isinstance(data, dict):
        raise ValueError("The release manifest must be a JSON object.")
    release = data.get("version")
    version_key(release)
    url = https_url(data.get("url"))
    digest = data.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("The release manifest needs a SHA-256 checksum (64 hex characters).")
    notes = data.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 64000:
        raise ValueError("Release notes must be text of at most 64,000 characters.")
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    suffix = next((ext for ext in _SUFFIXES if path.endswith(ext)), None)
    if suffix is None:
        raise ValueError("Release URL must name a ZIP archive, tar archive, or installer package.")
    return {"version": release.strip(), "url": url, "sha256": digest.lower(),
            "notes": notes, "suffix": suffix}


def check_release(url, current=VERSION):
    with _open_url(url) as response:
        raw = response.read(MANIFEST_LIMIT + 1)
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("Release manifest is too large.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The update URL did not return a UTF-8 JSON manifest.") from exc
    manifest = validate_manifest(data)
    return manifest if is_newer(manifest["version"], current) else None


def settings_directory():
    override = os.environ.get("FBVENOISON_UPDATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # The same persisted installation location used for device assignments.
    import boxes
    return Path(boxes.CONFIG_PATH).resolve().parent


def load_settings():
    try:
        saved = json.loads((settings_directory() / "update_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    url = os.environ.get("FBVENOISON_UPDATE_URL", "").strip()
    if not url and isinstance(saved, dict):
        value = saved.get("manifest_url", "")
        url = value if isinstance(value, str) else ""
    return {"manifest_url": url}


def save_settings(url):
    url = https_url(url) if url.strip() else ""
    directory = settings_directory()
    directory.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".update-settings-", delete=False) as handle:
            temp = handle.name
            json.dump({"manifest_url": url}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, directory / "update_config.json")
        temp = None
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)


def download_release(manifest, directory=None, progress=lambda percent: None):
    """Stage one hash-verified package atomically; clean partial downloads."""
    manifest = validate_manifest(manifest)
    if not is_newer(manifest["version"]):
        raise ValueError("This release is not newer than the installed launcher.")
    directory = Path(directory) if directory is not None else settings_directory() / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("FBVenoison-" + manifest["version"] + manifest["suffix"])
    temp = None
    try:
        digest = hashlib.sha256()
        received = 0
        with _open_url(manifest["url"]) as response:
            length = response.headers.get("Content-Length", "")
            try:
                expected = int(length) if length else None
            except (TypeError, ValueError):
                expected = None
            if expected is not None and (expected < 0 or expected > PACKAGE_LIMIT):
                raise ValueError("Release package is too large (maximum 2 GiB).")
            with tempfile.NamedTemporaryFile(mode="wb", dir=directory,
                                             prefix=".download-", suffix=".part", delete=False) as handle:
                temp = handle.name
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > PACKAGE_LIMIT:
                        raise ValueError("Release package exceeded the 2 GiB limit.")
                    digest.update(chunk)
                    handle.write(chunk)
                    progress(min(99, int(received * 100 / expected)) if expected else -1)
                handle.flush()
                os.fsync(handle.fileno())
        if expected is not None and received != expected:
            raise ValueError("Download was incomplete. Please retry.")
        if digest.hexdigest() != manifest["sha256"]:
            raise ValueError("SHA-256 verification failed. The package was discarded; please retry.")
        os.replace(temp, target)
        temp = None
        progress(100)
        return str(target.resolve())
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)


class _UpdateWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            self.result.emit(self.task(self.progress.emit))
        except Exception as exc:
            self.error.emit(str(exc))


class UpdateDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FBVenoison · Release center")
        self.setMinimumSize(540, 470)
        self.resize(620, 530)
        self.manifest = None
        self.package_path = None
        self.worker = None
        self.setStyleSheet("""
            QDialog { background:#0c111b; color:#e9eff7; }
            QLabel { color:#dbe5f2; background:transparent; }
            QLineEdit, QPlainTextEdit { background:#151c2a; color:#dbe5f2;
                border:1px solid #293449; border-radius:8px; padding:9px; }
            QPushButton { background:#1e2a3d; color:#e9eff7; border:1px solid #34445f;
                border-radius:8px; padding:10px 15px; }
            QPushButton:hover { background:#2c3b52; }
            QPushButton:disabled { color:#65738b; background:#121a27; }
            QProgressBar { background:#151c2a; border:0; border-radius:4px; color:#dbe5f2; }
            QProgressBar::chunk { background:#7de1ae; border-radius:4px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        heading = QLabel("Release center")
        heading.setStyleSheet("font-size:24px;font-weight:700;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Installed launcher  " + VERSION + "   ·   Suite release packages"))
        hint = QLabel("Connect your release feed, check for a newer build, and download a verified package.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("HTTPS manifest URL"))
        self.url_edit = QLineEdit(load_settings()["manifest_url"])
        self.url_edit.setPlaceholderText("Enter your publisher's HTTPS manifest URL")
        layout.addWidget(self.url_edit)
        buttons = QHBoxLayout()
        self.check_btn = QPushButton("Save & check")
        self.check_btn.clicked.connect(self.check)
        buttons.addWidget(self.check_btn)
        self.download_btn = QPushButton("Download release")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.download)
        buttons.addWidget(self.download_btn)
        layout.addLayout(buttons)
        self.status = QLabel("No update feed configured." if not self.url_edit.text() else "Ready to check for updates.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes appear here after a check.")
        layout.addWidget(self.notes, 1)
        footer = QHBoxLayout()
        self.open_btn = QPushButton("Open download folder")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_folder)
        footer.addWidget(self.open_btn)
        footer.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)
        instructions = QLabel("Downloaded packages are staged for installation. Stop both apps, back up settings, then replace the installation and restart.")
        instructions.setWordWrap(True)
        instructions.setStyleSheet("color:#8795ab;font-size:11px;")
        layout.addWidget(instructions)

    def _busy(self, active):
        self.url_edit.setEnabled(not active)
        self.check_btn.setEnabled(not active)
        self.download_btn.setEnabled(not active and self.manifest is not None)
        self.close_btn.setEnabled(not active)

    def _start(self, task, success):
        self._busy(True)
        self.worker = _UpdateWorker(task, self)
        self.worker.result.connect(success)
        self.worker.error.connect(self._error)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        worker, self.worker = self.worker, None
        self._busy(False)
        if worker is not None:
            worker.deleteLater()

    def _error(self, message):
        self.status.setText("Update failed: " + message)
        self.progress_bar.hide()

    def _progress(self, percent):
        self.progress_bar.setRange(0, 0 if percent < 0 else 100)
        if percent >= 0:
            self.progress_bar.setValue(percent)

    def check(self):
        if self.worker is not None and self.worker.isRunning():
            return
        url = self.url_edit.text().strip()
        self.manifest = None
        self.download_btn.setEnabled(False)
        self.notes.clear()
        self.progress_bar.hide()
        try:
            url = https_url(url)
            save_settings(url)
        except Exception as exc:
            self._error(str(exc))
            return
        self.status.setText("Checking your release feed…")
        self._start(lambda progress: check_release(url), self._checked)

    def _checked(self, manifest):
        self.manifest = manifest
        if manifest:
            self.status.setText("Release " + manifest["version"] + " is available. SHA-256 will be verified before saving.")
            self.notes.setPlainText(manifest["notes"] or "No release notes supplied.")
        else:
            self.status.setText("You are up to date with this release feed (" + VERSION + ").")

    def download(self):
        if self.manifest is None or (self.worker is not None and self.worker.isRunning()):
            return
        manifest = dict(self.manifest)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.status.setText("Downloading release " + manifest["version"] + "…")
        self._start(lambda progress: download_release(manifest, progress=progress), self._downloaded)

    def _downloaded(self, path):
        self.package_path = path
        self.open_btn.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status.setText("Verified and saved: " + path + "\nStop both apps before installing this release.")

    def open_folder(self):
        if self.package_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.package_path).parent)))

    def reject(self):
        # Escape uses QDialog.reject() directly, bypassing closeEvent().
        if self.worker is None or not self.worker.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


def open_updater(parent=None):
    """Show one retained, nonmodal updater per parent window."""
    dialog = getattr(parent, "_suite_update_dialog", None) if parent is not None else None
    if dialog is None:
        dialog = UpdateDialog(parent)
        if parent is not None:
            parent._suite_update_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
