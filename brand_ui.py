"""Generated branding and a lightweight, resize-aware Qt atmosphere."""
import math
import os
import time

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import QColor, QPainter, QPixmap, QRadialGradient, QLinearGradient
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "branding")


def emblem(size):
    source = QPixmap(os.path.join(ASSETS, "venoison-emblem.png"))
    return source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)


class Atmosphere(QWidget):
    """Animate cached artwork, light and droplets; pause when hidden/minimized."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = QPixmap(os.path.join(ASSETS, "venoison-atmosphere.png"))
        self.texture = QPixmap()
        self.motion = True
        self.phase = 0.0
        self.clock = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_motion(self, enabled):
        self.motion = bool(enabled)
        if self.motion and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _tick(self):
        now = time.monotonic()
        if self.isVisible() and not self.window().isMinimized():
            self.phase += min(now - self.clock, .1)
            self.update()
        self.clock = now

    def showEvent(self, event):
        super().showEvent(event)
        self.clock = time.monotonic()
        if self.motion:
            self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.source.isNull():
            self.texture = self.source.scaled(self.width() + 48, self.height() + 48,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#080c12"))
        t = self.phase
        if not self.texture.isNull():
            dx = (self.width() - self.texture.width()) / 2 + math.sin(t * .12) * 15
            dy = (self.height() - self.texture.height()) / 2 + math.cos(t * .09) * 12
            p.setOpacity(.75)
            p.drawPixmap(QPointF(dx, dy), self.texture)
            p.setOpacity(1)
        # Light travels slowly between the two strains, scaled to any window.
        for x, color, offset in ((.12, "#37ed9b", 0), (.88, "#a77bff", 2)):
            center = QPointF(self.width() * x, self.height() * (.24 + .08 * math.sin(t * .17 + offset)))
            glow = QRadialGradient(center, max(self.width(), self.height()) * .48)
            tint = QColor(color)
            tint.setAlpha(22)
            glow.setColorAt(0, tint)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(self.rect(), glow)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(22):
            x = ((i * .6180339) % 1) * self.width() + math.sin(t * .15 + i) * 12
            y = (self.height() * ((i * .381966) % 1) - t * (3 + i % 4)) % max(1, self.height())
            c = QColor("#64e9b0" if i % 2 else "#b99aff")
            c.setAlpha(45)
            p.setBrush(c)
            radius = 1 + (i % 3) * .5
            p.drawEllipse(QPointF(x, y), radius, radius)


class FusionHero(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMaximumHeight(142)
        row = QHBoxLayout(self)
        row.setContentsMargins(26, 10, 28, 10)
        text = QVBoxLayout()
        text.setSpacing(5)
        for value, style in (
            ("TWO STRAINS. ONE COMMAND CENTER.", "color:#71e7b0;font-size:9px;font-weight:700;letter-spacing:2px"),
            ("Unite your fleet.", "color:#f2f6ff;font-size:30px;font-weight:700;letter-spacing:-1px"),
            ("Poison reacts. Venom comments. You control the balance.", "color:#a4b2c5;font-size:12px"),
        ):
            label = QLabel(value)
            label.setStyleSheet(style + ";background:transparent")
            text.addWidget(label)
        row.addLayout(text, 1)
        self.art = QLabel()
        self.art.setPixmap(emblem(128))
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setFixedWidth(150)
        row.addWidget(self.art)


class FleetMetric(QWidget):
    def __init__(self, title, color, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("fleetmetric")
        self.setStyleSheet("#fleetmetric{background:rgba(15,22,33,205);border:1px solid #253346;border-radius:11px}")
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 9, 16, 9)
        self.value = QLabel("0")
        self.value.setStyleSheet("color:%s;font-size:23px;font-weight:700;background:transparent" % color)
        row.addWidget(self.value)
        self.caption = QLabel(title)
        self.caption.setStyleSheet("color:#9cacc2;font-size:10px;background:transparent")
        row.addWidget(self.caption)
        row.addStretch()

