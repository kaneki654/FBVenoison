# -*- coding: utf-8 -*-
"""Venoison access gate.

The key check is byte-for-byte the one FBPoison already uses - same salt,
pepper, XOR mask, 120,000 PBKDF2 rounds and sealed digest - so the existing
key keeps working and nothing about the verification got weaker. The panel shares the generated fusion branding with the hub.

Deliberately self-contained. Importing FBPoison's auth module would drag in
its gui module and its core module, which is exactly what the split between
these apps exists to avoid.

    python login_panel.py           show the gate, print the result
    python login_panel.py --seal    change the key
"""
import base64
import hashlib
import hmac
import sys

from PyQt6.QtWidgets import (QApplication, QDialog, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton)
from PyQt6.QtCore import (Qt, QTimer, QVariantAnimation, QEasingCurve,
    QPoint, QRectF)
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
from brand_ui import emblem

# ── layered key check (identical to FBPoison's) ────────────────────────────
_SALT   = b"fbpoison::biotoxin::dispersion::v1"
_PEPPER = b"G0Ju.VBS::engagement-toxin"
_MASK   = bytes((0x5B, 0xA7, 0x31, 0xE9, 0x0C, 0x74, 0xD2, 0x8F,
                 0x16, 0xC3, 0x4A, 0x9D, 0x2E, 0xB8, 0x60, 0xF5))
_ITERS  = 120_000

# XOR-masked + base64 digest of the authorised key. Not a recognisable hash;
# to change the key, run:  python login_panel.py --seal
_SEALED = "Av57aGQRqDhqb8qR9THLaDrqnA9Z1Ri4IVtTbLb6A7o="

MAX_TRIES = 3


def _mask(raw: bytes) -> str:
    out = bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw))
    return base64.b64encode(out).decode("ascii")


def _unmask(blob: str) -> bytes:
    raw = base64.b64decode(blob.encode("ascii"))
    return bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(raw))


def _digest(password: str) -> bytes:
    """Three chained one-way layers."""
    pw = password.encode("utf-8")
    l1 = hashlib.sha256(_SALT + pw).digest()
    l2 = hmac.new(_PEPPER, l1, hashlib.sha512).digest()
    return hashlib.pbkdf2_hmac("sha256", l2, _SALT, _ITERS)


def verify_key(password: str) -> bool:
    """Constant-time comparison against the sealed digest."""
    try:
        return hmac.compare_digest(_digest(password or ""), _unmask(_SEALED))
    except Exception:
        return False


# ── look ───────────────────────────────────────────────────────────────────
BG     = "#0E1013"
PANEL  = "#14171B"
LINE   = "#23282F"
FIELD  = "#101317"
TEXT   = "#E8EAED"
DIM    = "#9AA0A8"
FAINT  = "#61676F"
GREEN  = "#56D364"
PURPLE = "#A371F7"
RED    = "#F85149"
FONT   = "DejaVu Sans" if sys.platform != "win32" else "Segoe UI"


class Mark(QWidget):
    """The generated fusion emblem shared with the hub."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 44)

    def paintEvent(self, _e):
        p = QPainter(self)
        image = emblem(42)
        p.drawPixmap((self.width() - image.width()) // 2,
                     (self.height() - image.height()) // 2, image)
        p.end()


class LoginPanel(QDialog):
    """Frameless key prompt. Returns True only on the correct key."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setFixedSize(400, 268)
        self.ok = False
        self.tries = 0
        self._drag = None

        root = QVBoxLayout(self)
        root.setContentsMargins(34, 30, 34, 26)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(Mark(), 0, Qt.AlignmentFlag.AlignVCenter)
        name = QVBoxLayout()
        name.setSpacing(1)
        t = QLabel("FBVenoison")
        t.setStyleSheet(
            "color:%s;font-family:'%s';font-size:16px;font-weight:600;"
            "background:transparent" % (TEXT, FONT))
        name.addWidget(t)
        sub = QLabel("VENOM + POISON / FLEET COMMAND")
        sub.setStyleSheet(
            "color:%s;font-family:'%s';font-size:11px;background:transparent"
            % (FAINT, FONT))
        name.addWidget(sub)
        head.addLayout(name)
        head.addStretch()
        root.addLayout(head)
        root.addSpacing(17)

        cap = QLabel("Access key")
        cap.setStyleSheet(
            "color:%s;font-family:'%s';font-size:11px;background:transparent"
            % (DIM, FONT))
        root.addWidget(cap)
        root.addSpacing(7)

        # wrapped so the shake moves the field without disturbing the layout
        self.field_wrap = QWidget()
        self.field_wrap.setStyleSheet("background:transparent")
        fw = QVBoxLayout(self.field_wrap)
        fw.setContentsMargins(0, 0, 0, 0)
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setFixedHeight(38)
        self.field.setStyleSheet(
            "QLineEdit{background:%s;color:%s;border:1px solid %s;"
            "border-radius:7px;padding:0 12px;font-family:'%s';font-size:14px;"
            "letter-spacing:2px}"
            "QLineEdit:focus{border:1px solid %s}"
            % (FIELD, TEXT, LINE, FONT, FAINT))
        self.field.returnPressed.connect(self.attempt)
        self.field.textEdited.connect(self._typing)
        fw.addWidget(self.field)
        root.addWidget(self.field_wrap)
        root.addSpacing(8)

        self.msg = QLabel(" ")
        self.msg.setStyleSheet(
            "color:%s;font-family:'%s';font-size:11px;background:transparent"
            % (FAINT, FONT))
        root.addWidget(self.msg)
        root.addStretch()

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setFixedHeight(34)
        self.cancel_btn.setStyleSheet(
            "QPushButton{background:transparent;color:%s;border:1px solid %s;"
            "border-radius:7px;padding:0 16px;font-family:'%s';font-size:12px}"
            "QPushButton:hover{color:%s}" % (DIM, LINE, FONT, TEXT))
        self.cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self.cancel_btn)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unlock_btn.setFixedHeight(34)
        self.unlock_btn.setStyleSheet(
            "QPushButton{background:%s;color:#0B0E11;border:none;"
            "border-radius:7px;padding:0 20px;font-family:'%s';font-size:12px;"
            "font-weight:600}"
            "QPushButton:hover{background:#FFFFFF}"
            "QPushButton:disabled{background:%s;color:%s}"
            % (TEXT, FONT, PANEL, FAINT))
        self.unlock_btn.clicked.connect(self.attempt)
        btns.addWidget(self.unlock_btn)
        root.addLayout(btns)

        self.field.setFocus()

    # ── frameless: drag by the panel itself ──
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(LINE), 1))
        p.setBrush(QColor(BG))
        # QRectF, not four bare floats: there is no float x,y overload,
        # and the TypeError inside paintEvent aborts the process rather
        # than raising - the window simply dies on first paint.
        p.drawRoundedRect(
            QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12, 12)
        p.end()

    # ── behaviour ──
    def _typing(self, _t):
        if self.msg.text().strip():
            self.say(" ", FAINT)

    def say(self, text, color):
        self.msg.setText(text)
        self.msg.setStyleSheet(
            "color:%s;font-family:'%s';font-size:11px;background:transparent"
            % (color, FONT))

    def shake(self):
        """A short nudge - enough to register as 'no', not a performance."""
        start = self.field_wrap.pos()
        anim = QVariantAnimation(self, duration=260, startValue=0.0,
                                 endValue=1.0)
        anim.setEasingCurve(QEasingCurve.Type.Linear)

        def step(v):
            import math
            dx = int(math.sin(v * math.pi * 4) * 7 * (1.0 - v))
            self.field_wrap.move(start + QPoint(dx, 0))

        anim.valueChanged.connect(step)
        anim.finished.connect(lambda: self.field_wrap.move(start))
        anim.start()
        self._anim = anim        # keep a reference or it is collected mid-run

    def attempt(self):
        key = self.field.text()
        if not key:
            self.say("Enter the access key", FAINT)
            return
        # PBKDF2 at 120k rounds takes a moment; say so rather than freeze mute
        self.unlock_btn.setEnabled(False)
        self.say("Checking…", FAINT)
        QApplication.processEvents()

        good = verify_key(key)
        self.unlock_btn.setEnabled(True)
        if good:
            self.ok = True
            self.say("Unlocked", GREEN)
            QTimer.singleShot(140, self.accept)
            return

        self.tries += 1
        self.field.clear()
        self.field.setFocus()
        self.shake()
        left = MAX_TRIES - self.tries
        if left <= 0:
            self.say("Wrong key. Locked.", RED)
            self.field.setEnabled(False)
            self.unlock_btn.setEnabled(False)
            QTimer.singleShot(900, self.reject)
        else:
            self.say("Wrong key · %d attempt%s left"
                     % (left, "" if left == 1 else "s"), RED)

    def run(self):
        self.exec()
        return self.ok


def require_key(parent=None) -> bool:
    """Show the gate. True only if the correct key was entered."""
    return LoginPanel(parent).run()


if __name__ == "__main__":
    if "--seal" in sys.argv:
        pw = input("new access key: ")
        print("\nreplace _SEALED in login_panel.py with:\n")
        print('    _SEALED = "%s"' % _mask(_digest(pw)))
    else:
        app = QApplication(sys.argv)
        print("access granted" if require_key() else "access refused")
