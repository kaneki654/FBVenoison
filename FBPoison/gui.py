# fbPoison/gui.py
# PyQt6 GUI for FBPoison — "BIOTOXIN" theme.
#
# The name is the design brief: this is a toxin lab, so the palette is acid
# green + hazard amber, the emblem is a biohazard skull, and the vocabulary is
# poison-flavoured (payload / strain / toxin / hosts / dispersion / doses).
#
# The 3D is real perspective projection done in QPainter (no OpenGL):
#   * a rotating icosahedral "toxin molecule" with depth-sorted edges
#   * an infinite floor grid scrolling toward the camera
#   * a spore particle field where size and alpha come from depth
#   * buttons extruded on the Z axis that compress when pressed
#
# Every handler / worker / core call is unchanged from the previous version.

import sys
import os
import math
import random
import time
import threading
import queue
import io
import subprocess
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QComboBox,
    QListWidget, QListWidgetItem, QProgressBar, QFrame, QScrollArea,
    QSizePolicy, QButtonGroup, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QCheckBox)
from PyQt6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve,
    QRect, QRectF, QPointF, QSize, QVariantAnimation, QPoint)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QFont, QFontDatabase,
    QPainterPath, QPolygonF, QPixmap, QIcon, QLinearGradient, QRadialGradient,
    QCursor)

# Import core functions from fb_core
import core as fbcore
from core import (
    resource_path, ensure_online, connect_resilient, count_available_accounts,
    REACTIONS, parse_action_mode, normalize_url, start_llama, stop_llama,
    reset_comment_pool, find_apk, install_fb, has_fb, wait_online, wake_and_unlock,
    react_all_saved_accounts, HERE, abort_devices
)

# ── Palette: biotoxin ──────────────────────────────────────────────────────
C_VOID    = "#03060A"
C_BG      = "#060B0A"
C_PANEL   = "#08100D"
C_CARD    = "#0B1712"
C_BORDER  = "#16311F"
C_EDGE    = "#26603A"
C_INPUT   = "#050C09"
C_TEXT    = "#DCF7E2"
C_MUTE    = "#7BA48B"
C_FAINT   = "#47664F"
C_TOXIC   = "#5FFF3C"      # acid green — primary
C_TOXIC2  = "#B6FF6A"
C_VENOM   = "#00FFB2"      # secondary
C_HAZARD  = "#FFD028"
C_BLOOD   = "#FF3B4E"

# legacy aliases used by the handlers below
C_ACCENT = C_TOXIC
C_GREEN  = C_TOXIC
C_RED    = C_BLOOD
C_GOLD   = C_HAZARD
C_AMBER  = C_HAZARD
C_CYAN   = C_TOXIC
C_MAG    = C_VENOM
C_LIME   = C_TOXIC

F_UI   = "Segoe UI"
F_MONO = "Consolas"

_INPUT_QSS = (
    f"QLineEdit{{background:{C_INPUT};color:{C_TEXT};border:1px solid {C_BORDER};"
    f"border-radius:0px;padding:10px 13px;font-size:13px;"
    f"font-family:'{F_UI}';selection-background-color:{C_TOXIC};"
    f"selection-color:#04240B}}"
    f"QLineEdit:hover{{border:1px solid {C_EDGE}}}"
    f"QLineEdit:focus{{border:1px solid {C_TOXIC};background:#071410}}"
)
_FIELD_LBL = (f"color:{C_MUTE};font-size:10px;font-weight:700;"
              f"letter-spacing:1.5px;font-family:'{F_UI}';background:transparent")


def _qc(c, a=None):
    col = QColor(c)
    if a is not None:
        col.setAlpha(max(0, min(255, int(a))))
    return col

def _lerp_hex(a, b, t):
    a, b = QColor(a), QColor(b)
    return "#{:02x}{:02x}{:02x}".format(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t))

def cyber_path(r, cut=13, tl=False, tr=True, br=False, bl=True):
    p = QPainterPath()
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cut = min(cut, w / 2.0, h / 2.0)
    pts = []
    pts.append((x + cut, y) if tl else (x, y))
    if tr:
        pts += [(x + w - cut, y), (x + w, y + cut)]
    else:
        pts.append((x + w, y))
    if br:
        pts += [(x + w, y + h - cut), (x + w - cut, y + h)]
    else:
        pts.append((x + w, y + h))
    if bl:
        pts += [(x + cut, y + h), (x, y + h - cut)]
    else:
        pts.append((x, y + h))
    if tl:
        pts.append((x, y + cut))
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    p.closeSubpath()
    return p

def _glow_pixmap(color, size=256, stops=((0.0, 190), (0.45, 70), (1.0, 0))):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(size / 2, size / 2, size / 2)
    for pos, alpha in stops:
        g.setColorAt(pos, _qc(color, alpha))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


# ── 3D maths ───────────────────────────────────────────────────────────────
def rot3(p, ax, ay, az=0.0):
    """Rotate a point about X, then Y, then Z."""
    x, y, z = p
    ca, sa = math.cos(ax), math.sin(ax)
    y, z = y * ca - z * sa, y * sa + z * ca
    cb, sb = math.cos(ay), math.sin(ay)
    x, z = x * cb + z * sb, -x * sb + z * cb
    if az:
        cc, sc = math.cos(az), math.sin(az)
        x, y = x * cc - y * sc, x * sc + y * cc
    return x, y, z

def project(p, focal, cam_z=3.6):
    """Perspective-divide a rotated point. Returns (sx, sy, depth)."""
    x, y, z = p
    d = cam_z + z
    if d < 0.25:
        d = 0.25
    return x * focal / d, y * focal / d, d

def build_icosahedron():
    """12 vertices / 30 edges — the 'toxin molecule'."""
    P = (1.0 + 5.0 ** 0.5) / 2.0
    vs = []
    for a in (-1.0, 1.0):
        for b in (-P, P):
            vs += [(0.0, a, b), (a, b, 0.0), (b, 0.0, a)]
    n = (1.0 + P * P) ** 0.5
    vs = [(x / n, y / n, z / n) for x, y, z in vs]
    pairs = []
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            d = math.dist(vs[i], vs[j])
            pairs.append((d, i, j))
    pairs.sort()
    emin = pairs[0][0]
    edges = [(i, j) for d, i, j in pairs if d < emin * 1.05]
    return vs, edges

ICO_V, ICO_E = build_icosahedron()


def app_icon_pixmap(size=256):
    for name in ("icon.png", "icon.ico"):
        pm = QPixmap(resource_path(name))
        if not pm.isNull():
            return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx = cy = size / 2.0
    rad = size * 0.44

    hexa = QPolygonF()
    for i in range(6):
        a = math.radians(60 * i - 90)
        hexa.append(QPointF(cx + rad * math.cos(a), cy + rad * math.sin(a)))
    body = QLinearGradient(0, 0, 0, size)
    body.setColorAt(0, QColor("#0D2416"))
    body.setColorAt(1, QColor("#040C08"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(body))
    p.drawPolygon(hexa)

    stroke = QLinearGradient(0, 0, size, size)
    stroke.setColorAt(0, QColor(C_TOXIC))
    stroke.setColorAt(1, QColor(C_VENOM))
    p.setPen(QPen(QBrush(stroke), max(2.0, size * 0.03)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(hexa)

    inner = QPolygonF()
    for i in range(6):
        a = math.radians(60 * i - 90)
        inner.append(QPointF(cx + rad * 0.74 * math.cos(a),
                             cy + rad * 0.74 * math.sin(a)))
    p.setPen(QPen(_qc(C_TOXIC, 70), max(1.0, size * 0.012)))
    p.drawPolygon(inner)

    p.setPen(QColor("#EAFFE9"))
    p.setFont(QFont(F_UI, int(size * 0.40), QFont.Weight.Black))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "☠")
    p.end()
    return pm


def hazard_stripes(p, rect, t=0.0, alpha=42, band=13):
    """Diagonal hazard tape — the one motif that says 'toxic' instantly."""
    p.save()
    p.setClipRect(rect)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc(C_HAZARD, alpha))
    off = (t * 22.0) % (band * 2)
    x = rect.x() - rect.height() - band * 2 + off
    while x < rect.x() + rect.width() + band * 2:
        poly = QPolygonF([
            QPointF(x, rect.y() + rect.height()),
            QPointF(x + band, rect.y() + rect.height()),
            QPointF(x + band + rect.height(), rect.y()),
            QPointF(x + rect.height(), rect.y()),
        ])
        p.drawPolygon(poly)
        x += band * 2
    p.restore()


# ── 3D backdrop ────────────────────────────────────────────────────────────
class RootCanvas(QWidget):
    """Perspective floor + depth-sorted spore field. Owns frameless resize."""
    EDGE = 7

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.t = 0.0
        self.setMouseTracking(True)
        self._glow = _glow_pixmap(C_TOXIC, 320)
        self._glow2 = _glow_pixmap(C_VENOM, 320)
        self.spores = [self._spore(random.uniform(0.5, 9.0)) for _ in range(70)]
        self._resize_edge = None
        self._start_geo = None
        self._start_mouse = None

    def _spore(self, z=None):
        return {"x": random.uniform(-3.4, 3.4),
                "y": random.uniform(-2.4, 2.4),
                "z": 9.0 if z is None else z,
                "sp": random.uniform(0.28, 0.95),
                "ph": random.uniform(0, 6.28)}

    def advance(self, dt):
        self.t += dt
        for s in self.spores:
            s["z"] -= dt * s["sp"]
            if s["z"] < 0.45:
                s.update(self._spore())
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0

        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor("#040806"))
        base.setColorAt(0.55, QColor(C_VOID))
        base.setColorAt(1.0, QColor("#020503"))
        p.fillRect(0, 0, w, h, QBrush(base))

        # ── 3D floor grid receding to a horizon ──
        focal = 300.0
        horizon = h * 0.60
        scroll = (self.t * 0.55) % 1.0
        for i in range(1, 30):
            z = i - scroll
            if z < 0.55:
                continue
            sy = horizon + focal / z
            if sy > h + 4:
                continue
            a = int(40 * max(0.0, 1.0 - z / 27.0))
            if a <= 1:
                continue
            p.setPen(QPen(_qc(C_TOXIC, a), 1))
            p.drawLine(QPointF(0, sy), QPointF(w, sy))
        near, far = 0.62, 27.0
        for gx in range(-12, 13):
            x1 = cx + focal * gx * 0.55 / near
            y1 = horizon + focal / near
            x2 = cx + focal * gx * 0.55 / far
            y2 = horizon + focal / far
            a = int(30 * max(0.0, 1.0 - abs(gx) / 13.0))
            if a <= 1:
                continue
            p.setPen(QPen(_qc(C_TOXIC, a), 1))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # ambient toxin glow — kept low so panel text stays readable
        gx_ = w * (0.20 + 0.05 * math.sin(self.t * 0.10))
        gy_ = h * (0.20 + 0.05 * math.cos(self.t * 0.08))
        p.setOpacity(0.26)
        p.drawPixmap(int(gx_ - 320), int(gy_ - 320), 640, 640, self._glow)
        p.setOpacity(0.16)
        p.drawPixmap(int(w * 0.84 - 300), int(h * 0.78 - 300), 600, 600, self._glow2)
        p.setOpacity(1.0)

        # ── depth-sorted spores ──
        p.setPen(Qt.PenStyle.NoPen)
        for s in sorted(self.spores, key=lambda q: -q["z"]):
            sx, sy, d = project((s["x"], s["y"], 0.0), 300.0, s["z"])
            X, Y = cx + sx, cy + sy
            if X < -20 or X > w + 20 or Y < -20 or Y > h + 20:
                continue
            r = max(0.7, 3.0 / d * 2.2)
            a = int(130 * max(0.0, min(1.0, (9.5 - d) / 9.0)))
            twinkle = 0.75 + 0.25 * math.sin(self.t * 2.0 + s["ph"])
            p.setBrush(_qc(C_TOXIC, a * twinkle * 0.55))
            p.drawEllipse(QPointF(X, Y), r * 2.4, r * 2.4)
            p.setBrush(_qc(C_TOXIC2, a * twinkle))
            p.drawEllipse(QPointF(X, Y), r, r)

        vg = QRadialGradient(cx, cy, max(w, h) * 0.72)
        vg.setColorAt(0.5, _qc("#000000", 0))
        vg.setColorAt(1.0, _qc("#000000", 165))
        p.fillRect(0, 0, w, h, QBrush(vg))

        # drawRect/drawPath FILL with the current brush — the spore loop above
        # leaves a green brush set, which would flood the whole window.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_qc(C_EDGE, 190), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.setPen(QPen(_qc(C_TOXIC, 150), 2))
        L = 22
        for (x, y, dx, dy) in ((1, 1, 1, 1), (w - 2, 1, -1, 1),
                               (1, h - 2, 1, -1), (w - 2, h - 2, -1, -1)):
            p.drawLine(x, y, x + L * dx, y)
            p.drawLine(x, y, x, y + L * dy)
        p.end()

    # ── frameless edge resize ──
    def _edge_at(self, pos):
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        e = self.EDGE
        left, right = x <= e, x >= w - e
        top, bot = y <= e, y >= h - e
        if top and left:    return "tl"
        if top and right:   return "tr"
        if bot and left:    return "bl"
        if bot and right:   return "br"
        if left:            return "l"
        if right:           return "r"
        if top:             return "t"
        if bot:             return "b"
        return None

    _CURSORS = {
        "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
        "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
        "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    }

    def mouseMoveEvent(self, ev):
        if self._resize_edge and self._start_geo is not None:
            g = QRect(self._start_geo)
            dlt = ev.globalPosition().toPoint() - self._start_mouse
            e = self._resize_edge
            if "l" in e: g.setLeft(g.left() + dlt.x())
            if "r" in e: g.setRight(g.right() + dlt.x())
            if "t" in e: g.setTop(g.top() + dlt.y())
            if "b" in e: g.setBottom(g.bottom() + dlt.y())
            mn = self.win.minimumSize()
            if g.width() >= mn.width() and g.height() >= mn.height():
                self.win.setGeometry(g)
            return
        edge = self._edge_at(ev.position().toPoint())
        self.setCursor(QCursor(self._CURSORS.get(edge, Qt.CursorShape.ArrowCursor)))

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or self.win.isMaximized():
            return
        edge = self._edge_at(ev.position().toPoint())
        if edge:
            self._resize_edge = edge
            self._start_geo = self.win.geometry()
            self._start_mouse = ev.globalPosition().toPoint()

    def mouseReleaseEvent(self, _ev):
        self._resize_edge = None
        self._start_geo = None


# ── The rotating 3D toxin molecule ─────────────────────────────────────────
class Molecule3D(QWidget):
    def __init__(self, size=74, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.t = 0.0
        self.rate = 1.0
        self.color = C_TOXIC
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def advance(self, dt):
        self.t += dt * self.rate
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        focal = w * 1.35
        ax, ay = self.t * 0.62, self.t * 0.93

        pts = []
        for v in ICO_V:
            r = rot3(v, ax, ay)
            sx, sy, d = project(r, focal)
            pts.append((cx + sx, cy + sy, d))

        # far edges first so nearer ones overlay them
        edges = sorted(ICO_E, key=lambda e: -(pts[e[0]][2] + pts[e[1]][2]))
        for i, j in edges:
            x1, y1, d1 = pts[i]
            x2, y2, d2 = pts[j]
            dd = (d1 + d2) / 2.0
            t = max(0.0, min(1.0, (4.7 - dd) / 2.0))
            p.setPen(QPen(_qc(self.color, 40 + 165 * t), 0.7 + 1.5 * t))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        p.setPen(Qt.PenStyle.NoPen)
        for x, y, d in sorted(pts, key=lambda q: -q[2]):
            t = max(0.0, min(1.0, (4.7 - d) / 2.0))
            r = 1.3 + 2.5 * t
            p.setBrush(_qc(self.color, 30 + 60 * t))
            p.drawEllipse(QPointF(x, y), r * 2.6, r * 2.6)
            p.setBrush(_qc(C_TOXIC2, 110 + 145 * t))
            p.drawEllipse(QPointF(x, y), r, r)
        p.end()


# ── Chrome ─────────────────────────────────────────────────────────────────
class WinButton(QPushButton):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(40, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._anim = QVariantAnimation(self, duration=140,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_hot)

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Forward)
        self._anim.start(); super().enterEvent(e)

    def leaveEvent(self, e):
        self._anim.setDirection(QVariantAnimation.Direction.Backward)
        self._anim.start(); super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        hot = self._hot
        tint = C_BLOOD if self.kind == "close" else C_TOXIC
        if hot > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(tint, 30 * hot))
            p.drawRect(0, 0, w, h)
        col = QColor(_lerp_hex(C_MUTE, tint, hot))
        p.setPen(QPen(col, 1.4))
        cx, cy = w / 2, h / 2
        if self.kind == "min":
            p.drawLine(QPointF(cx - 5, cy + 3), QPointF(cx + 5, cy + 3))
        elif self.kind == "max":
            p.drawRect(QRectF(cx - 5, cy - 5, 10, 10))
        else:
            p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
        p.end()


class TitleBar(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setFixedHeight(48)
        self.t = 0.0
        self._drag = None
        lo = QHBoxLayout(self)
        lo.setContentsMargins(14, 0, 8, 0)
        lo.setSpacing(11)

        self.mark = Molecule3D(30)
        lo.addWidget(self.mark)

        name = QLabel("FBPOISON")
        name.setStyleSheet(
            f"color:{C_TEXT};font-size:14px;font-weight:800;letter-spacing:4px;"
            f"font-family:'{F_UI}';background:transparent")
        lo.addWidget(name)

        tag = QLabel("☠ engagement toxin · multi-host dispersion")
        tag.setStyleSheet(
            f"color:{C_FAINT};font-size:10px;letter-spacing:0.6px;"
            f"font-family:'{F_MONO}';background:transparent")
        lo.addWidget(tag)
        lo.addStretch()

        self.b_min = WinButton("min");   self.b_min.clicked.connect(win.showMinimized)
        self.b_max = WinButton("max");   self.b_max.clicked.connect(self._toggle_max)
        self.b_cls = WinButton("close"); self.b_cls.clicked.connect(win.close)
        for b in (self.b_min, self.b_max, self.b_cls):
            lo.addWidget(b)

    def advance(self, dt):
        self.t += dt
        self.mark.advance(dt)

    def _toggle_max(self):
        self.win.showNormal() if self.win.isMaximized() else self.win.showMaximized()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, _qc("#0B1811", 240))
        g.setColorAt(1, _qc("#050C09", 170))
        p.fillRect(0, 0, w, h, QBrush(g))
        line = QLinearGradient(0, 0, w, 0)
        line.setColorAt(0.0, _qc(C_TOXIC, 200))
        line.setColorAt(0.45, _qc(C_VENOM, 110))
        line.setColorAt(1.0, _qc(C_TOXIC, 0))
        p.fillRect(0, h - 1, w, 1, QBrush(line))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.win.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            if self.win.isMaximized():
                self.win.showNormal()
                self._drag = QPoint(self.win.width() // 2, 20)
            self.win.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def mouseDoubleClickEvent(self, _ev):
        self._toggle_max()


# ── Panels & controls ──────────────────────────────────────────────────────
class Panel(QFrame):
    def __init__(self, title="", accent=C_TOXIC, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.title = title
        self.body = QVBoxLayout(self)
        top = 42 if title else 16
        self.body.setContentsMargins(17, top, 17, 16)
        self.body.setSpacing(10)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = cyber_path(r, 15, tr=True, bl=True)

        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0.0, _qc("#0A1610", 253))
        g.setColorAt(1.0, _qc("#050B08", 253))
        p.fillPath(path, QBrush(g))
        # extruded top-left light edge = fake depth
        p.setPen(QPen(_qc(C_TOXIC, 30), 1))
        p.drawLine(QPointF(2, 2), QPointF(r.width() - 16, 2))
        p.setPen(QPen(_qc(C_BORDER, 255), 1))
        p.drawPath(path)

        p.setPen(QPen(_qc(self.accent, 175), 2))
        p.drawLine(QPointF(1, 1), QPointF(19, 1))
        p.drawLine(QPointF(1, 1), QPointF(1, 19))
        p.drawLine(QPointF(r.width() - 1, r.height() - 1),
                   QPointF(r.width() - 19, r.height() - 1))
        p.drawLine(QPointF(r.width() - 1, r.height() - 1),
                   QPointF(r.width() - 1, r.height() - 19))

        if self.title:
            p.setPen(QColor(C_MUTE))
            f = QFont(F_UI, 8, QFont.Weight.Black)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.4)
            p.setFont(f)
            p.drawText(QRectF(17, 14, r.width() - 34, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self.title.upper())
            fm = p.fontMetrics().horizontalAdvance(self.title.upper())
            x0 = 17 + fm + 12
            rule = QLinearGradient(x0, 0, r.width() - 17, 0)
            rule.setColorAt(0.0, _qc(self.accent, 120))
            rule.setColorAt(1.0, _qc(self.accent, 0))
            p.fillRect(QRectF(x0, 21, max(0, r.width() - 17 - x0), 1), QBrush(rule))
        p.end()


class HazardButton(QPushButton):
    """Extruded along Z — the face physically drops onto its base when pressed."""

    def __init__(self, text, accent=C_TOXIC, primary=True, depth=5, parent=None):
        super().__init__(text, parent)
        self.accent = accent
        self.primary = primary
        self.depth = depth
        self.t = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._press = 0.0
        self._a = QVariantAnimation(self, duration=170,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        self._pa = QVariantAnimation(self, duration=90,
                                     startValue=0.0, endValue=1.0)
        self._pa.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pa.valueChanged.connect(self._set_press)

    def advance(self, dt):
        self.t += dt
        if self.isEnabled() and self.primary:
            self.update()

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def _set_press(self, v):
        self._press = float(v); self.update()

    def enterEvent(self, e):
        if self.isEnabled():
            self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self._pa.setDirection(QVariantAnimation.Direction.Forward); self._pa.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._pa.setDirection(QVariantAnimation.Direction.Backward); self._pa.start()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isEnabled()
        hot = self._hot if on else 0.0
        dz = self.depth * (1.0 - self._press)
        acc = QColor(self.accent)
        w, h = self.width(), self.height()

        base = QRectF(2.5, 2.5 + dz, w - 5, h - 5 - dz)
        face = QRectF(2.5, 2.5, w - 5, h - 5 - dz)

        # extruded side wall
        if dz > 0.4:
            side = cyber_path(base, 12, tr=True, bl=True)
            p.fillPath(side, _qc(_lerp_hex(acc.name(), "#04120A", 0.72)
                                 if on and self.primary else "#060E0A", 235))

        path = cyber_path(face, 12, tr=True, bl=True)
        if not on:
            p.fillPath(path, _qc("#08120C", 210))
            p.setPen(QPen(_qc(C_BORDER, 210), 1)); p.drawPath(path)
            p.setPen(QColor(C_FAINT))
        elif self.primary:
            g = QLinearGradient(0, face.top(), 0, face.bottom())
            g.setColorAt(0.0, _qc(C_TOXIC2, 240))
            g.setColorAt(0.55, _qc(acc.name(), 240))
            g.setColorAt(1.0, QColor(_lerp_hex(acc.name(), "#0C6B22", 0.6)))
            p.fillPath(path, QBrush(g))
            hazard_stripes(p, face.toRect(), self.t, alpha=int(26 + 22 * hot))
            if hot > 0.01:
                p.fillPath(path, _qc("#FFFFFF", 26 * hot))
            p.setPen(QPen(_qc("#E9FFDA", 130 + 110 * hot), 1))
            p.drawPath(path)
            p.setPen(QColor("#05270C"))
        else:
            p.fillPath(path, _qc("#0A140E", 200 + 40 * hot))
            p.setPen(QPen(QColor(_lerp_hex(C_BORDER, acc.name(), hot)),
                          1 + 0.4 * hot))
            p.drawPath(path)
            p.setPen(QColor(_lerp_hex(C_TEXT, acc.name(), hot)))

        f = QFont(F_UI, 10, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        p.setFont(f)
        p.drawText(face, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class GhostButton(QPushButton):
    def __init__(self, text, accent=C_TOXIC, parent=None):
        super().__init__(text, parent)
        self.accent = accent
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self._hot = 0.0
        self._a = QVariantAnimation(self, duration=150,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(text) + 26)

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def setText(self, t):
        super().setText(t)
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance(t) + 26)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hot = self._hot
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = cyber_path(r, 8, tr=True, bl=True)
        p.fillPath(path, _qc("#09130D", 180 + 50 * hot))
        p.setPen(QPen(QColor(_lerp_hex(C_BORDER, self.accent, hot)), 1))
        p.drawPath(path)
        p.setPen(QColor(_lerp_hex(C_MUTE, self.accent, hot)))
        f = QFont(F_UI, 8, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.9)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class Segmented(QWidget):
    def __init__(self, values, emoji=None, on_change=None, accent=C_TOXIC,
                 compact=False, emoji_only=False, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent")
        self.values = list(values)
        self.emoji = emoji or {}
        self.emoji_only = emoji_only
        self.accent = accent
        self._value = values[0]
        self._on_change = on_change
        self._hot_i = -1
        self._pos = 0.0
        self.setMouseTracking(True)
        self.setFixedHeight(34 if compact else 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QVariantAnimation(self, duration=280)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._anim.valueChanged.connect(self._set_pos)

    def _set_pos(self, v):
        self._pos = float(v); self.update()

    def _seg_w(self):
        return self.width() / max(1, len(self.values))

    def _label(self, v):
        if self.emoji_only and v in self.emoji:
            return self.emoji[v]
        return (f"{self.emoji[v]} " if v in self.emoji else "") + v.title()

    def mouseMoveEvent(self, ev):
        i = int(ev.position().x() // self._seg_w())
        i = max(0, min(len(self.values) - 1, i))
        if i != self._hot_i:
            self._hot_i = i; self.update()

    def leaveEvent(self, _e):
        self._hot_i = -1; self.update()

    def mousePressEvent(self, ev):
        i = int(ev.position().x() // self._seg_w())
        if 0 <= i < len(self.values):
            self._select(self.values[i])

    def _select(self, v):
        if v == self._value:
            return
        self._value = v
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(float(self.values.index(v)))
        self._anim.start()
        if self._on_change:
            self._on_change(v)

    def value(self):
        return self._value

    def setValue(self, v):
        if v in self.values:
            self._value = v
            self._anim.stop()
            self._pos = float(self.values.index(v))
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        sw = self._seg_w()

        track = cyber_path(QRectF(0.5, 0.5, w - 1, h - 1), 10, tr=True, bl=True)
        p.fillPath(track, _qc(C_INPUT, 225))
        p.setPen(QPen(_qc(C_BORDER, 255), 1))
        p.drawPath(track)

        ix = self._pos * sw
        # raised (3D) selector: shadow plate then face
        shadow = cyber_path(QRectF(ix + 3.0, 4.0, sw - 6, h - 5), 9, tr=True, bl=True)
        p.fillPath(shadow, _qc("#000000", 110))
        sel = cyber_path(QRectF(ix + 2.5, 2.5, sw - 5, h - 6), 9, tr=True, bl=True)
        g = QLinearGradient(ix, 0, ix, h)
        g.setColorAt(0.0, _qc(self.accent, 78))
        g.setColorAt(1.0, _qc(self.accent, 26))
        p.fillPath(sel, QBrush(g))
        p.setPen(QPen(_qc(self.accent, 215), 1.3))
        p.drawPath(sel)
        p.setPen(QPen(_qc(self.accent, 240), 2))
        p.drawLine(QPointF(ix + 4, h - 4), QPointF(ix + sw - 6, h - 4))

        for i, v in enumerate(self.values):
            x = i * sw
            active = abs(self._pos - i) < 0.5
            if active:
                col = QColor("#EFFFE6")
            elif i == self._hot_i:
                col = QColor(_lerp_hex(C_MUTE, self.accent, 0.7))
            else:
                col = QColor(C_MUTE)
            p.setPen(col)
            f = QFont(F_UI, 8 if len(self.values) > 4 else 9,
                      QFont.Weight.Black if active else QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.7)
            p.setFont(f)
            p.drawText(QRectF(x, 0, sw, h), Qt.AlignmentFlag.AlignCenter,
                       self._label(v))
        p.end()


class NeonCheck(QCheckBox):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(24)
        self.setMinimumWidth(self._text_w(text))
        self._t = 0.0
        self._a = QVariantAnimation(self, duration=190,
                                    startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_t)
        self.toggled.connect(self._on_toggle)

    def _set_t(self, v):
        self._t = float(v); self.update()

    def _on_toggle(self, on):
        self._a.setDirection(QVariantAnimation.Direction.Forward if on
                             else QVariantAnimation.Direction.Backward)
        self._a.start()

    def _text_w(self, text):
        from PyQt6.QtGui import QFontMetrics
        return 27 + QFontMetrics(QFont(F_UI, 9)).horizontalAdvance(text) + 10

    def sizeHint(self):
        return QSize(self._text_w(self.text()), 24)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        box = QRectF(1.5, (h - 17) / 2, 17, 17)
        path = cyber_path(box, 5, tr=True, bl=True)
        p.fillPath(path, _qc(C_INPUT, 235))
        p.setPen(QPen(QColor(_lerp_hex(C_BORDER, C_TOXIC, self._t)), 1.2))
        p.drawPath(path)
        if self._t > 0.02:
            p.setOpacity(self._t)
            p.fillPath(path, _qc(C_TOXIC, 46))
            p.setPen(QPen(_qc(C_TOXIC, 255), 2.1))
            cx, cy = box.center().x(), box.center().y()
            p.drawLine(QPointF(cx - 4.2, cy), QPointF(cx - 1.2, cy + 3.4))
            p.drawLine(QPointF(cx - 1.2, cy + 3.4), QPointF(cx + 4.4, cy - 3.4))
            p.setOpacity(1.0)
        p.setPen(QColor(C_TEXT))
        p.setFont(QFont(F_UI, 9))
        p.drawText(QRectF(27, 0, self.width() - 27, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.text())
        p.end()


class ToxicProgress(QWidget):
    """A dosing tube: liquid fill, rising bubbles, meniscus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self._target = 0
        self._value = 0.0
        self.t = 0.0
        self.bubbles = [{"x": random.random(), "y": random.random(),
                         "r": random.uniform(1.0, 2.4),
                         "sp": random.uniform(0.25, 0.7)} for _ in range(16)]
        self._a = QVariantAnimation(self, duration=460)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_v)

    def _set_v(self, v):
        self._value = float(v); self.update()

    def setRange(self, *_a): pass
    def setTextVisible(self, *_a): pass

    def setValue(self, v):
        v = max(0, min(100, int(v)))
        self._target = v
        self._a.stop()
        self._a.setStartValue(self._value)
        self._a.setEndValue(float(v))
        self._a.start()

    def value(self):
        return self._target

    def advance(self, dt):
        self.t += dt
        for b in self.bubbles:
            b["y"] -= dt * b["sp"]
            if b["y"] < 0:
                b["y"] = 1.0
                b["x"] = random.random()
        if self._value > 0:
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        tube = cyber_path(QRectF(0.5, 0.5, w - 1, h - 1), 7, tr=True, bl=True)
        p.fillPath(tube, _qc(C_INPUT, 240))
        p.setPen(QPen(_qc(C_BORDER, 230), 1))
        p.drawPath(tube)

        fw = (w - 4) * self._value / 100.0
        if fw > 2:
            p.save()
            fill = cyber_path(QRectF(2, 2, fw, h - 4), 6, tr=True, bl=True)
            p.setClipPath(fill)
            g = QLinearGradient(0, 2, 0, h - 2)
            g.setColorAt(0.0, _qc(C_TOXIC2, 235))
            g.setColorAt(0.5, _qc(C_TOXIC, 245))
            g.setColorAt(1.0, _qc("#1B8C2A", 240))
            p.fillRect(QRectF(2, 2, fw, h - 4), QBrush(g))
            # bubbles rising through the toxin
            p.setPen(Qt.PenStyle.NoPen)
            for b in self.bubbles:
                bx = 2 + b["x"] * fw
                by = 2 + b["y"] * (h - 4)
                p.setBrush(_qc("#EAFFE0", 120))
                p.drawEllipse(QPointF(bx, by), b["r"], b["r"])
            # meniscus highlight
            p.setPen(QPen(_qc("#F2FFE8", 210), 1.6))
            p.drawLine(QPointF(2 + fw - 1, 3), QPointF(2 + fw - 1, h - 3))
            p.restore()
        p.end()


class StatTile(QFrame):
    def __init__(self, caption, color=C_TOXIC, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.color = color
        self.setMinimumHeight(66)
        self._text = "0"
        self._shown = 0.0
        self._numeric = True
        self._flash = 0.0
        self._count = QVariantAnimation(self, duration=560)
        self._count.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._count.valueChanged.connect(self._set_shown)
        self._fl = QVariantAnimation(self, duration=560,
                                     startValue=1.0, endValue=0.0)
        self._fl.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fl.valueChanged.connect(self._set_flash)

    def _set_shown(self, v):
        self._shown = float(v); self.update()

    def _set_flash(self, v):
        self._flash = float(v); self.update()

    def setText(self, text):
        text = str(text)
        if text == self._text:
            return
        old = self._text
        self._text = text
        try:
            target = float(text)
            self._numeric = True
            self._count.stop()
            try:
                self._count.setStartValue(float(old))
            except ValueError:
                self._count.setStartValue(0.0)
            self._count.setEndValue(target)
            self._count.start()
        except ValueError:
            self._numeric = False
        self._fl.stop(); self._fl.start()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 4)
        # extruded base for depth
        base = cyber_path(QRectF(0.5, 4.5, self.width() - 1, self.height() - 5),
                          11, tr=True, bl=True)
        p.fillPath(base, _qc("#040B07", 220))
        path = cyber_path(r, 11, tr=True, bl=True)
        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0.0, _qc("#0C1911", 240))
        g.setColorAt(1.0, _qc("#081108", 240))
        p.fillPath(path, QBrush(g))
        p.setPen(QPen(QColor(_lerp_hex(C_BORDER, self.color, 0.32 * self._flash)), 1))
        p.drawPath(path)
        p.setPen(QPen(_qc(self.color, 70 + 150 * self._flash), 2))
        p.drawLine(QPointF(1, r.height() - 1), QPointF(1, r.height() - 15))

        shown = str(int(round(self._shown))) if self._numeric else self._text
        p.setPen(QColor(self.color))
        p.setFont(QFont(F_UI, 19, QFont.Weight.Black))
        p.drawText(QRectF(14, 6, r.width() - 24, 30),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
        p.setPen(QColor(C_FAINT))
        f = QFont(F_UI, 7, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.9)
        p.setFont(f)
        p.drawText(QRectF(15, 38, r.width() - 24, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.caption.upper())
        p.end()


class LinkTally(QWidget):
    """all devices -> link_1 -> 169 reacts · 12 comments"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.setVisible(False)

    def set_rows(self, rows):
        """rows = [(link_no, url, reacted, commented, failed), ...]"""
        self.rows = rows
        self.setVisible(bool(rows))
        self.setMinimumHeight(20 + len(rows) * 22 + 4 if rows else 0)
        self.update()

    def paintEvent(self, _e):
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        p.setPen(QColor(C_FAINT))
        f = QFont(F_UI, 7, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.9)
        p.setFont(f)
        p.drawText(QRectF(0, 0, w, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "ALL DEVICES  ·  PER LINK")
        p.setPen(QPen(_qc(C_BORDER, 200), 1))
        p.drawLine(QPointF(148, 7), QPointF(w, 7))

        y = 20
        for no, url, reacted, commented, failed in self.rows:
            p.setFont(QFont(F_MONO, 9, QFont.Weight.Bold))
            p.setPen(QColor(C_TOXIC))
            p.drawText(QRectF(0, y, 70, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"link_{no}")
            short = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] if url else ""
            p.setFont(QFont(F_MONO, 8))
            p.setPen(QColor(C_FAINT))
            fm = p.fontMetrics()
            p.drawText(QRectF(70, y, 150, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(short, Qt.TextElideMode.ElideRight, 146))
            parts = [(f"{reacted} reacts", C_TOXIC2)]
            if commented:
                parts.append((f"{commented} comments", C_VENOM))
            if failed:
                parts.append((f"{failed} failed", C_BLOOD))
            x = w
            for txt, col in reversed(parts):
                p.setFont(QFont(F_MONO, 9, QFont.Weight.Bold))
                tw = p.fontMetrics().horizontalAdvance(txt) + 14
                x -= tw
                p.setPen(QColor(col))
                p.drawText(QRectF(x, y, tw, 18),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           txt)
            p.setPen(QPen(_qc(C_BORDER, 80), 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(0, y + 19), QPointF(w, y + 19))
            y += 22
        p.end()


class Briefing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.setMinimumHeight(126)

    def set_rows(self, rows):
        self.rows = rows
        self.setMinimumHeight(22 + len(rows) * 21 + 4)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        p.setPen(QColor(C_FAINT))
        f = QFont(F_UI, 7, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.9)
        p.setFont(f)
        p.drawText(QRectF(0, 0, w, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "FORMULA")
        p.setPen(QPen(_qc(C_BORDER, 200), 1))
        p.drawLine(QPointF(58, 7), QPointF(w, 7))

        y = 22
        for label, value, color in self.rows:
            p.setFont(QFont(F_MONO, 8))
            p.setPen(QColor(C_FAINT))
            p.drawText(QRectF(0, y, 120, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       label)
            p.setFont(QFont(F_MONO, 8, QFont.Weight.Bold))
            fm = p.fontMetrics()
            val = fm.elidedText(str(value), Qt.TextElideMode.ElideRight,
                                max(60, int(w - 128)))
            p.setPen(QColor(color))
            p.drawText(QRectF(120, y, w - 120, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       val)
            p.setPen(QPen(_qc(C_BORDER, 90), 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(0, y + 18), QPointF(w, y + 18))
            y += 21
        p.end()


class DeviceCard(QFrame):
    STATUS_COLORS = {'pending': C_FAINT, 'processing': C_TOXIC,
                     'done': C_TOXIC, 'failed': C_BLOOD, 'suspended': C_HAZARD}

    def __init__(self, serial, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.status = 'pending'
        self.detail = ""
        self.setFixedHeight(44)
        self.t = 0.0
        self._in = 0.0
        self._anim = QVariantAnimation(self, duration=380,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_in)

    def _set_in(self, v):
        self._in = float(v); self.update()

    def play_in(self, delay=0):
        QTimer.singleShot(delay, self._anim.start)

    def advance(self, dt):
        if self.status == 'processing':
            self.t += dt
            self.update()

    def set_status(self, status, detail=""):
        self.status = status
        self.detail = detail or status
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        prog = max(0.0, min(1.0, self._in))
        p.setOpacity(prog)
        # slide in along Z: scale up from 0.94 as it approaches
        w, h = self.width(), self.height()
        sc = 0.94 + 0.06 * prog
        p.translate(w * (1 - sc) / 2.0, h * (1 - sc) / 2.0 + (1 - prog) * 10)
        p.scale(sc, sc)
        c = QColor(self.STATUS_COLORS.get(self.status, C_FAINT))

        r = QRectF(0.5, 0.5, w - 1, h - 1)
        path = cyber_path(r, 9, tr=True, bl=True)
        p.fillPath(path, _qc("#0A140E", 232))
        p.setPen(QPen(_qc(C_BORDER, 235), 1))
        p.drawPath(path)
        p.fillRect(QRectF(1, 1, 2.6, h - 2), _qc(c.name(), 215))

        if self.status == 'processing':
            pulse = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self.t * 3.0))
        else:
            pulse = 1.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(c.name(), 62 * pulse))
        p.drawEllipse(QPointF(21, h / 2), 7.6, 7.6)
        p.setBrush(_qc(c.name(), 255 * pulse))
        p.drawEllipse(QPointF(21, h / 2), 3.4, 3.4)

        p.setPen(QColor(C_TEXT))
        p.setFont(QFont(F_MONO, 9))
        p.drawText(QRectF(36, 0, w * 0.5, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.serial)
        p.setPen(c)
        p.setFont(QFont(F_MONO, 8))
        fm = p.fontMetrics()
        avail = int(w - 36 - w * 0.42 - 14)
        txt = fm.elidedText(self.detail or self.status,
                            Qt.TextElideMode.ElideRight, max(40, avail))
        p.drawText(QRectF(w * 0.42, 0, w - w * 0.42 - 14, h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
        p.end()


class StatusChip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 32)
        self.text = "DORMANT"
        self.color = C_FAINT
        self.active = False
        self.t = 0.0

    def set_state(self, text, color, active):
        self.text, self.color, self.active = text.upper(), color, active
        self.update()

    def advance(self, dt):
        if self.active:
            self.t += dt
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = QRectF(0.5, 0.5, w - 1, h - 1)
        path = cyber_path(r, 9, tr=True, bl=True)
        p.fillPath(path, _qc("#0A140E", 225))
        p.setPen(QPen(_qc(C_BORDER, 235), 1))
        p.drawPath(path)
        a = 1.0
        if self.active:
            a = 0.40 + 0.60 * (0.5 + 0.5 * math.sin(self.t * 2.6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(self.color, 58 * a))
        p.drawEllipse(QPointF(17, h / 2), 7.4, 7.4)
        p.setBrush(_qc(self.color, 255 * a))
        p.drawEllipse(QPointF(17, h / 2), 3.3, 3.3)
        p.setPen(QColor(C_TEXT))
        f = QFont(F_UI, 8, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.7)
        p.setFont(f)
        p.drawText(QRectF(31, 0, w - 38, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.text)
        p.end()


# ── Splash ─────────────────────────────────────────────────────────────────
class SplashScreen(QWidget):
    BOOT = [
        "synthesising toxin core",
        "arming adb vector bridge",
        "enumerating reachable hosts",
        "binding language endpoint",
        "payload ready",
    ]

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(540, 350)
        self.center()
        self.glow = _glow_pixmap(C_TOXIC, 256)
        self.t = 0.0
        self.tick_count = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(16)
        self.opacity_effect = QGraphicsOpacityEffect()
        self.opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity_effect)
        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(430)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self.fade_anim.finished.connect(self.on_fade_done)
        self.fade_anim.setStartValue(1.0)
        self.fade_anim.setEndValue(0.0)

    def on_tick(self):
        self.tick_count += 1
        self.t += 0.016
        self.update()

    def on_fade_done(self):
        self.timer.stop()

    def center(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self.move((geo.width()-self.width())//2, (geo.height()-self.height())//2)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()

        card = cyber_path(QRectF(1, 1, w - 2, h - 2), 26, tr=True, bl=True)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor("#0A1810"))
        g.setColorAt(1.0, QColor("#040906"))
        p.fillPath(card, QBrush(g))
        p.setClipPath(card)

        hazard_stripes(p, QRectF(0, 0, w, 6).toRect(), self.t, alpha=70, band=11)
        hazard_stripes(p, QRectF(0, h - 6, w, 6).toRect(), -self.t, alpha=70, band=11)

        cx, cy = w / 2.0, 118.0
        p.setOpacity(0.5)
        p.drawPixmap(int(cx - 145), int(cy - 145), 290, 290, self.glow)
        p.setOpacity(1.0)

        # the same 3D molecule, larger
        focal = 190.0
        ax, ay = self.t * 0.62, self.t * 0.93
        pts = []
        for v in ICO_V:
            sx, sy, d = project(rot3(v, ax, ay), focal)
            pts.append((cx + sx, cy + sy, d))
        for i, j in sorted(ICO_E, key=lambda e: -(pts[e[0]][2] + pts[e[1]][2])):
            x1, y1, d1 = pts[i]; x2, y2, d2 = pts[j]
            t = max(0.0, min(1.0, (4.7 - (d1 + d2) / 2.0) / 2.0))
            p.setPen(QPen(_qc(C_TOXIC, 45 + 175 * t), 0.9 + 1.8 * t))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, d in sorted(pts, key=lambda q: -q[2]):
            t = max(0.0, min(1.0, (4.7 - d) / 2.0))
            r = 1.8 + 3.2 * t
            p.setBrush(_qc(C_TOXIC, 30 + 60 * t))
            p.drawEllipse(QPointF(x, y), r * 2.6, r * 2.6)
            p.setBrush(_qc(C_TOXIC2, 120 + 135 * t))
            p.drawEllipse(QPointF(x, y), r, r)

        p.setPen(QColor(C_TEXT))
        f = QFont(F_UI, 20, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 7.0)
        p.setFont(f)
        p.drawText(QRectF(0, 196, w, 32), Qt.AlignmentFlag.AlignCenter, "FBPOISON")

        p.setPen(QColor(C_FAINT))
        p.setFont(QFont(F_MONO, 8))
        p.drawText(QRectF(0, 226, w, 18), Qt.AlignmentFlag.AlignCenter,
                   "☠ engagement toxin · multi-host dispersion")

        idx = max(0, min(len(self.BOOT) - 1, int(self.t / 0.38)))
        line = self.BOOT[idx]
        fm_f = QFont(F_MONO, 8)
        p.setFont(fm_f)
        fm = p.fontMetrics()
        mark_w = fm.horizontalAdvance("> ")
        x0 = (w - (mark_w + fm.horizontalAdvance(line))) / 2.0
        done = idx >= len(self.BOOT) - 1
        p.setPen(QColor(C_TOXIC if done else C_HAZARD))
        p.drawText(QRectF(x0, 256, mark_w + 2, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ">")
        p.setPen(QColor(C_TEXT if done else C_MUTE))
        p.drawText(QRectF(x0 + mark_w, 256, w, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)

        prog = max(0.0, min(1.0, self.t / (0.38 * len(self.BOOT))))
        tx, tw, ty, th = 66, w - 132, h - 46, 6
        p.fillRect(QRectF(tx, ty, tw, th), _qc("#08130C", 255))
        gg = QLinearGradient(tx, 0, tx + tw, 0)
        gg.setColorAt(0.0, _qc(C_TOXIC2, 240))
        gg.setColorAt(1.0, _qc(C_TOXIC, 240))
        p.fillRect(QRectF(tx, ty, tw * prog, th), QBrush(gg))
        p.setPen(QColor(C_FAINT))
        p.setFont(QFont(F_MONO, 7))
        p.drawText(QRectF(tx, ty - 18, tw, 14), Qt.AlignmentFlag.AlignRight,
                   f"{int(prog * 100):3d}%")

        p.setClipping(False)
        # the molecule vertex loop left a solid brush — clear it or drawPath
        # would fill the entire card instead of just stroking its outline.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_qc(C_EDGE, 220), 1))
        p.drawPath(card)
        p.setPen(QPen(_qc(C_TOXIC, 175), 2))
        L = 26
        p.drawLine(2, 2, 2 + L, 2);            p.drawLine(2, 2, 2, 2 + L)
        p.drawLine(w - 3, h - 3, w - 3 - L, h - 3)
        p.drawLine(w - 3, h - 3, w - 3, h - 3 - L)
        p.end()


# ── Main window ────────────────────────────────────────────────────────────
class ReactionGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FBPoison")
        self.setWindowIcon(QIcon(app_icon_pixmap(256)))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1020, 710)
        self.resize(1140, 800)
        self.running        = False
        self.stop_flag      = False
        self.reaction_count = 0
        self.device_count   = 0
        self.total_devices  = 0
        self.start_time     = 0
        self.summary_mode   = False
        self.device_cards   = {}
        self.output_queue   = queue.Queue()
        self.tick           = 0
        self.active_devices = []
        self.device_comment = {}      # serial -> posts comments?
        self._device_locks = {}       # serial -> lock (ADB access)
        self._locks_guard = threading.Lock()
        self.tally_acc = {}           # link_no -> totals across devices
        self.device_pills = {}

        central = RootCanvas(self)
        self.canvas = central
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.titlebar = TitleBar(self)
        outer.addWidget(self.titlebar)

        root = QVBoxLayout()
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(13)
        outer.addLayout(root, 1)

        # ── HUD: molecule + status + potency ──
        hud = QHBoxLayout()
        hud.setSpacing(13)
        self.hud_mol = Molecule3D(56)
        hud.addWidget(self.hud_mol)
        self.status_chip = StatusChip()
        hud.addWidget(self.status_chip)
        self.hud_msg = QLabel("load a payload and inject")
        self.hud_msg.setStyleSheet(
            f"color:{C_MUTE};font-size:11px;font-family:'{F_MONO}';"
            f"background:transparent")
        hud.addWidget(self.hud_msg)
        hud.addStretch()
        self.stat_time = QLabel("00:00")
        self.stat_time.setStyleSheet(
            f"color:{C_TOXIC};font-size:15px;font-weight:700;"
            f"font-family:'{F_MONO}';background:transparent")
        hud.addWidget(self.stat_time)
        el = QLabel("EXPOSURE")
        el.setStyleSheet(
            f"color:{C_MUTE};font-size:9px;font-weight:800;letter-spacing:1.5px;"
            f"font-family:'{F_UI}';background:transparent")
        hud.addWidget(el)
        root.addLayout(hud)

        # ── Main grid ──
        middle = QHBoxLayout()
        middle.setSpacing(13)

        cfg = Panel("payload", C_TOXIC)
        self.cfg_panel = cfg
        cfg_lo = cfg.body

        cfg_lo.addWidget(self._field_label("STRAIN"))
        self.action_seg = Segmented(
            ["React", "Comment", "React + Comment"],
            on_change=lambda _v: self.update_action_fields())
        cfg_lo.addWidget(self.action_seg)

        cfg_lo.addSpacing(4)
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self.links_lbl = self._field_label("TARGET VECTOR  ·  LINK")
        hdr.addWidget(self.links_lbl)
        hdr.addStretch()
        self.add_link_btn = GhostButton("+  ADD LINK")
        self.add_link_btn.clicked.connect(lambda: self.add_link_row(animate=True))
        hdr.addWidget(self.add_link_btn)
        cfg_lo.addLayout(hdr)

        self.links_scroll = QScrollArea()
        self.links_scroll.setWidgetResizable(True)
        self.links_scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent}"
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:0}}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px}}"
            f"QScrollBar::add-line,QScrollBar::sub-line{{height:0}}"
            f"QScrollBar::add-page,QScrollBar::sub-page{{background:transparent}}")
        self.links_host = QWidget()
        self.links_host.setStyleSheet("background:transparent")
        self.links_layout = QVBoxLayout(self.links_host)
        self.links_layout.setContentsMargins(0, 0, 4, 0)
        self.links_layout.setSpacing(6)
        self.links_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.links_scroll.setWidget(self.links_host)
        cfg_lo.addWidget(self.links_scroll, 1)
        self.link_rows = []

        # kept so the rest of the app (briefing, action fields) still has them
        self.reaction_lbl = self._field_label("")
        self.reaction_lbl.setVisible(False)
        self.comment_row = QWidget()
        self.comment_row.setVisible(False)
        self.briefing = Briefing()
        cfg_lo.addWidget(self.briefing)
        cfg_lo.addSpacing(2)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {C_BORDER},stop:1 transparent);border:none")
        cfg_lo.addWidget(sep)

        shot_row = QHBoxLayout()
        shot_row.setContentsMargins(0, 2, 0, 0)
        shot_row.setSpacing(8)
        self.screenshot_check = NeonCheck("Capture evidence after each dose")
        self.screenshot_check.setChecked(False)
        shot_row.addWidget(self.screenshot_check)
        shot_row.addStretch()
        self.open_shots_btn = GhostButton("OPEN VAULT")
        self.open_shots_btn.clicked.connect(self.open_screenshots_folder)
        shot_row.addWidget(self.open_shots_btn)
        cfg_lo.addLayout(shot_row)
        middle.addWidget(cfg, 5)

        right = QVBoxLayout()
        right.setSpacing(13)

        dev = Panel("hosts", C_VENOM)
        dev_lo = dev.body
        dhead = QHBoxLayout()
        dhead.setSpacing(7)
        self.dev_count_lbl = QLabel("0 reachable")
        self.dev_count_lbl.setStyleSheet(
            f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        dhead.addWidget(self.dev_count_lbl)
        dhead.addStretch()
        self.all_btn = GhostButton("SELECT ALL", C_VENOM)
        self.all_btn.clicked.connect(self.toggle_select_all)
        dhead.addWidget(self.all_btn)
        self.refresh_btn = GhostButton("RESCAN", C_VENOM)
        self.refresh_btn.clicked.connect(self.refresh_devices)
        dhead.addWidget(self.refresh_btn)
        dev_lo.addLayout(dhead)

        self.dev_list = QListWidget()
        self.dev_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.dev_list.setStyleSheet(
            f"QListWidget{{background:{C_INPUT};border:1px solid {C_BORDER};"
            f"border-radius:0px;padding:4px;outline:none}}"
            f"QListWidget::item{{border:none;margin:2px}}"
            f"QListWidget::item:selected{{background:rgba(95,255,60,26);"
            f"border-left:2px solid {C_TOXIC}}}"
            f"QListWidget::item:hover{{background:rgba(95,255,60,12)}}"
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:0}}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px}}"
            f"QScrollBar::add-line,QScrollBar::sub-line{{height:0}}"
            f"QScrollBar::add-page,QScrollBar::sub-page{{background:transparent}}")
        self.dev_list.setMouseTracking(True)
        self.dev_list.viewport().setMouseTracking(True)
        self.dev_list.itemEntered.connect(self.on_device_hover)
        self.account_counts = {}
        self.account_scan_inflight = set()
        self.dev_items = {}
        dev_lo.addWidget(self.dev_list, 1)
        dev.setMaximumHeight(186)
        right.addWidget(dev, 0)

        ops = Panel("dispersion", C_TOXIC)
        run_lo = ops.body
        srow = QHBoxLayout()
        self.status_label = QLabel("DORMANT")
        self.status_label.setStyleSheet(
            f"color:{C_TEXT};font-size:15px;font-weight:800;letter-spacing:1.8px;"
            f"font-family:'{F_UI}';background:transparent")
        srow.addWidget(self.status_label)
        srow.addStretch()
        run_lo.addLayout(srow)

        self.msg_label = QLabel("Load a payload, pick hosts, then inject.")
        self.msg_label.setStyleSheet(
            f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        run_lo.addWidget(self.msg_label)

        self.progress_bar = ToxicProgress()
        run_lo.addWidget(self.progress_bar)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.stat_devices = StatTile("hosts", C_TEXT)
        self.stat_reactions = StatTile("doses", C_TOXIC)
        tiles.addWidget(self.stat_devices)
        tiles.addWidget(self.stat_reactions)
        run_lo.addLayout(tiles)

        self.link_tally = LinkTally()
        run_lo.addWidget(self.link_tally)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent}"
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:0}}"
            f"QScrollBar::handle:vertical{{background:{C_EDGE};min-height:24px}}"
            f"QScrollBar::add-line,QScrollBar::sub-line{{height:0}}"
            f"QScrollBar::add-page,QScrollBar::sub-page{{background:transparent}}")
        self.device_container = QWidget()
        self.device_container.setStyleSheet("background:transparent")
        self.dev_card_layout = QVBoxLayout(self.device_container)
        self.dev_card_layout.setContentsMargins(0, 0, 4, 0)
        self.dev_card_layout.setSpacing(6)
        self.dev_card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self.device_container)
        run_lo.addWidget(scroll, 1)
        right.addWidget(ops, 1)
        middle.addLayout(right, 4)
        self.middle_layout = middle
        root.addLayout(middle, 1)

        # ── Command bar ──
        self.cmdbar = _CommandBar()
        cb = QHBoxLayout(self.cmdbar)
        cb.setContentsMargins(16, 8, 12, 8)
        cb.setSpacing(12)
        hint = QLabel("☠  every selected host is dosed in parallel")
        hint.setStyleSheet(
            f"color:{C_FAINT};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        cb.addWidget(hint)
        cb.addStretch()
        rate_cap = QLabel("DOSE RATE")
        rate_cap.setStyleSheet(
            f"color:{C_FAINT};font-size:9px;font-family:'{F_UI}';"
            f"letter-spacing:1px;background:transparent")
        cb.addWidget(rate_cap)
        self.dose_rate = DoseRate(on_change=self._set_speed)
        cb.addWidget(self.dose_rate)
        self.stop_btn = HazardButton("■  PURGE", C_BLOOD, primary=False, depth=4)
        self.stop_btn.setFixedSize(158, 46)
        self.stop_btn.clicked.connect(self.stop_reaction)
        self.stop_btn.setEnabled(False)
        cb.addWidget(self.stop_btn)
        self.start_btn = HazardButton("☠  INJECT", C_TOXIC, primary=True, depth=6)
        self.start_btn.setFixedSize(210, 46)
        self.start_btn.clicked.connect(self.start_reaction)
        cb.addWidget(self.start_btn)
        root.addWidget(self.cmdbar)

        central.setStyleSheet(f"QWidget{{color:{C_TEXT};font-family:'{F_UI}'}}")

        # decorative injection FX, on top of everything, click-through
        self.inject_fx = InjectOverlay(central)
        self.inject_fx.setGeometry(0, 0, self.width(), self.height())
        self.layout_fx = PanelFlipFX(central)
        self.layout_fx.setGeometry(0, 0, self.width(), self.height())

        # per-row fields wire themselves up in LinkRow(on_change=...)
        self.screenshot_check.toggled.connect(lambda _b: self.update_briefing())
        self.dev_list.itemSelectionChanged.connect(self.update_briefing)

        fbcore.set_speed(self.dose_rate.profile())

        self.add_link_row()          # always start with one target
        self.update_action_fields()
        self.refresh_devices()

        self.timer = QTimer()
        self.timer.timeout.connect(self.process_queue)
        self.timer.start(100)
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.animate_status)
        self.anim_timer.start(33)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        fx = getattr(self, "inject_fx", None)
        if fx is not None and self.centralWidget() is not None:
            fx.setGeometry(0, 0, self.centralWidget().width(),
                           self.centralWidget().height())

    # ── splash ──
    def show_splash(self):
        self.splash = SplashScreen()
        self.splash.show()
        QTimer.singleShot(2600, self.close_splash)

    def close_splash(self):
        self.splash.fade_anim.finished.connect(self._on_splash_faded)
        self.splash.fade_anim.start()

    def _on_splash_faded(self):
        sp, self.splash = self.splash, None
        if sp is not None:
            sp.close()
            sp.deleteLater()
        self.showNormal()
        self.refresh_devices()
        self._fade_in()
        self.update_timer()

    def _fade_in(self):
        eff = QGraphicsOpacityEffect(self)
        self.centralWidget().setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(340)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self.centralWidget().setGraphicsEffect(None))
        anim.start()
        self._fade_anim = anim

    def animate_status(self):
        self.tick += 1
        dt = 0.033
        self.canvas.advance(dt)
        self.inject_fx.advance(dt)
        self.layout_fx.advance(dt)
        self.titlebar.advance(dt)
        self.hud_mol.advance(dt)
        self.progress_bar.advance(dt)
        self.status_chip.advance(dt)
        self.start_btn.advance(dt)
        self.cmdbar.advance(dt)
        for c in self.device_cards.values():
            c.advance(dt)
        if self.running and self.stop_flag:
            self.status_chip.set_state("PURGING", C_HAZARD, True)
            self.hud_mol.rate = 3.0
            self.hud_mol.color = C_HAZARD
        elif self.summary_mode:
            aborted = self.stop_flag
            self.status_chip.set_state("PURGED" if aborted else "SATURATED",
                                       C_HAZARD if aborted else C_TOXIC, False)
            self.hud_mol.rate = 0.35
            self.hud_mol.color = C_HAZARD if aborted else C_TOXIC
        elif self.running:
            self.status_chip.set_state("SPREADING", C_TOXIC, True)
            self.hud_mol.rate = 2.2
            self.hud_mol.color = C_TOXIC
        else:
            self.status_chip.set_state("DORMANT", C_FAINT, False)
            self.hud_mol.rate = 0.55
            self.hud_mol.color = C_TOXIC

    def update_timer(self):
        if self.running and self.start_time > 0:
            elapsed = int(time.time() - self.start_time)
            self.stat_time.setText(f"{elapsed//60:02d}:{elapsed%60:02d}")
        QTimer.singleShot(1000, self.update_timer)

    def _field_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_FIELD_LBL)
        return lbl

    def toggle_select_all(self):
        rows = [self.dev_list.item(k) for k in range(self.dev_list.count())]
        rows = [it for it in rows if it and it.data(Qt.ItemDataRole.UserRole)]
        if not rows:
            return
        select = not all(it.isSelected() for it in rows)
        for it in rows:
            it.setSelected(select)
        self.all_btn.setText("CLEAR ALL" if select else "SELECT ALL")

    def _phone_pix(self):
        pix = QPixmap(22, 26)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(C_TOXIC), 1.3))
        p.setBrush(_qc("#0A1710", 220))
        p.drawRoundedRect(QRectF(4, 1, 14, 23), 2.5, 2.5)
        p.setPen(QPen(_qc(C_TOXIC, 120), 1))
        p.drawLine(QPointF(9, 4.5), QPointF(13, 4.5))
        p.setPen(QPen(_qc(C_VENOM, 190), 1.4))
        p.drawLine(QPointF(8, 20.5), QPointF(14, 20.5))
        p.end()
        return pix

    def refresh_devices(self):
        self.dev_list.clear()
        self.dev_items = {}
        self.device_pills = {}
        try:
            devs = ensure_online()
            for serial, state in devs.items():
                w  = QWidget()
                w.setStyleSheet("background:transparent")
                lo = QHBoxLayout(w)
                lo.setContentsMargins(7, 4, 7, 4)
                lo.setSpacing(9)
                icon_lbl = QLabel()
                icon_lbl.setPixmap(self._phone_pix())
                icon_lbl.setStyleSheet("background:transparent")
                lo.addWidget(icon_lbl)
                vl = QVBoxLayout()
                vl.setContentsMargins(0, 0, 0, 0)
                vl.setSpacing(1)
                s_lbl = QLabel(serial)
                s_lbl.setStyleSheet(
                    f"color:{C_TEXT};font-size:11px;font-weight:600;"
                    f"font-family:'{F_MONO}';background:transparent")
                vl.addWidget(s_lbl)
                ok = state == "device"
                st_lbl = QLabel("READY" if ok else state.upper())
                st_lbl.setStyleSheet(
                    f"color:{C_TOXIC if ok else C_HAZARD};font-size:9px;"
                    f"font-weight:700;letter-spacing:1px;"
                    f"font-family:'{F_MONO}';background:transparent")
                vl.addWidget(st_lbl)
                lo.addLayout(vl)
                lo.addStretch()
                # per-device: does this phone comment, or only react?
                pill = CommentPill(
                    on=self.device_comment.get(serial, True),
                    on_toggle=lambda checked, s=serial: self._set_device_comment(s, checked))
                pill.setToolTip("Click: this device posts comments / only reacts")
                self.device_pills[serial] = pill
                lo.addWidget(pill)
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, serial)
                item.setSizeHint(w.sizeHint())
                cached = self.account_counts.get(serial)
                item.setToolTip(f"{cached} accounts available" if cached is not None
                                else "Hover to count accounts")
                self.dev_list.addItem(item)
                self.dev_list.setItemWidget(item, w)
                item.setSelected(True)
                self.dev_items[serial] = item
            self.dev_count_lbl.setText(f"{len(devs)} reachable")
            if hasattr(self, "all_btn"):
                self.all_btn.setText("CLEAR ALL" if devs else "SELECT ALL")
        except Exception as e:
            self.dev_count_lbl.setText("error")
            item = QListWidgetItem(f"Error: {e}")
            self.dev_list.addItem(item)

    def on_device_hover(self, item):
        serial = item.data(Qt.ItemDataRole.UserRole)
        if not serial:
            return
        cached = self.account_counts.get(serial)
        if cached is not None:
            item.setToolTip(f"{cached} accounts available")
            return
        if serial in self.account_scan_inflight:
            return
        if getattr(self, "running", False):
            item.setToolTip("Counting paused during a run")
            return
        self.account_scan_inflight.add(serial)
        item.setToolTip("Counting accounts…")
        threading.Thread(target=self.scan_one, args=(serial,), daemon=True).start()

    def device_lock(self, serial):
        """One lock per phone. The hover account-count probe and the run both
        drive the same device over ADB, and the probe force-stops Facebook and
        walks to the menu — landing that mid-run yanked the post away and the
        run just re-opened and scrolled. Only one may hold a phone at a time."""
        with self._locks_guard:
            return self._device_locks.setdefault(serial, threading.Lock())

    def scan_one(self, serial):
        n = -1
        lock = self.device_lock(serial)
        if not lock.acquire(timeout=0.1):
            self.account_scan_inflight.discard(serial)
            return                      # phone is busy with a run: skip quietly
        try:
            if not getattr(self, "running", False):
                d = connect_resilient(serial)
                if d is not None:
                    n = count_available_accounts(serial, d)
        except Exception:
            n = -1
        finally:
            lock.release()
            self.account_scan_inflight.discard(serial)
        self.send_cmd({"action": "device_accounts", "serial": serial, "count": n})

    def get_selected_devices(self):
        result = []
        for item in self.dev_list.selectedItems():
            serial = item.data(Qt.ItemDataRole.UserRole)
            if serial:
                result.append(serial)
        return result

    def send_cmd(self, cmd):
        self.output_queue.put(cmd)

    def process_queue(self):
        while not self.output_queue.empty():
            try:
                msg = self.output_queue.get_nowait()
                if isinstance(msg, dict):
                    self.handle_cmd(msg)
            except queue.Empty:
                break

    def handle_cmd(self, cmd):
        a = cmd.get("action")
        if a == "status":
            self.status_label.setText(str(cmd.get("text", "")).upper())
            self.status_label.setStyleSheet(
                f"color:{cmd.get('color', C_TEXT)};font-size:15px;"
                f"font-weight:800;letter-spacing:1.8px;font-family:'{F_UI}';"
                f"background:transparent")
        elif a == "msg":
            self.msg_label.setText(cmd.get("text"))
            self.msg_label.setStyleSheet(
                f"color:{cmd.get('color', C_MUTE)};font-size:10px;"
                f"font-family:'{F_MONO}';background:transparent")
            self.hud_msg.setText(str(cmd.get("text", "")).lower())
            self.hud_msg.setStyleSheet(
                f"color:{cmd.get('color', C_MUTE)};font-size:11px;"
                f"font-family:'{F_MONO}';background:transparent")
        elif a == "progress":
            self.progress_bar.setValue(cmd.get("pct", 0))
        elif a == "stats":
            self.stat_devices.setText(
                f"{cmd.get('devices',0)} / {cmd.get('total',0)}")
            self.stat_reactions.setText(f"{cmd.get('reactions',0)}")
            self.refresh_link_tally()
        elif a == "toast":
            self.msg_label.setText(cmd.get("text"))
            self.msg_label.setStyleSheet(
                f"color:{C_HAZARD};font-size:10px;font-family:'{F_MONO}';"
                f"background:transparent")
            self.hud_msg.setText(str(cmd.get("text", "")).lower())
            self.hud_msg.setStyleSheet(
                f"color:{C_HAZARD};font-size:11px;font-family:'{F_MONO}';"
                f"background:transparent")
            QTimer.singleShot(2200, lambda: (
                self.msg_label.setText("Ready"),
                self.msg_label.setStyleSheet(
                    f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
                    f"background:transparent"),
                self.hud_msg.setText("load a payload and inject"),
                self.hud_msg.setStyleSheet(
                    f"color:{C_MUTE};font-size:11px;font-family:'{F_MONO}';"
                    f"background:transparent")))
        elif a == "device_status":
            s  = cmd.get("serial")
            st = cmd.get("status")
            if s in self.device_cards:
                self.device_cards[s].set_status(st)
        elif a == "device_accounts":
            s = cmd.get("serial")
            n = cmd.get("count", -1)
            if n is not None and n >= 0:
                self.account_counts[s] = n
                tip = f"{n} accounts available"
            else:
                tip = "Couldn't count accounts (hover again to retry)"
            item = self.dev_items.get(s)
            if item is not None:
                item.setToolTip(tip)
        elif a == "devices_init":
            self.clear_device_cards()
            for i, s in enumerate(cmd.get("devices", [])):
                self.add_device_card(s, delay=i * 55)
        elif a == "reset_dash":
            self.clear_device_cards()
            self.status_label.setText("DORMANT")
            self.status_label.setStyleSheet(
                f"color:{C_TEXT};font-size:15px;font-weight:800;"
                f"letter-spacing:1.8px;font-family:'{F_UI}';background:transparent")
            self.msg_label.setText("Priming…")
            self.msg_label.setStyleSheet(
                f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
                f"background:transparent")
            self.progress_bar.setValue(0)
            self.stat_devices.setText("0 / 0")
            self.stat_reactions.setText("0")
            self.link_tally.set_rows([])
        elif a == "show_summary":
            self.show_summary(cmd.get("results", {}), cmd.get("devices", []))

    def _set_speed(self, profile):
        """Dose rate changed: retune the pacing in core and warn about turbo."""
        fbcore.set_speed(profile)
        note = {"safe": ("human timing — lowest block risk", C_MUTE),
                "fast": ("~2x quicker — fine for aged accounts", C_VENOM),
                "turbo": ("minimal waiting — expect action blocks", C_BLOOD)}
        text, col = note.get(profile, note["safe"])
        self.msg_label.setText(f"dose rate: {profile.upper()} — {text}")
        self.msg_label.setStyleSheet(
            f"color:{col};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")

    def refresh_link_tally(self):
        """all devices -> link_N -> X reacts"""
        acc = getattr(self, "tally_acc", {})
        rows = [(no, acc[no].get("url", ""), acc[no].get("reacted", 0),
                 acc[no].get("commented", 0), acc[no].get("failed", 0))
                for no in sorted(acc)]
        self.link_tally.set_rows(rows)

    def add_device_card(self, serial, delay=0):
        card = DeviceCard(serial)
        self.device_cards[serial] = card
        self.dev_card_layout.addWidget(card)
        card.play_in(delay)

    def clear_device_cards(self):
        while self.dev_card_layout.count():
            item = self.dev_card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.device_cards = {}

    def show_summary(self, results, devices):
        self.summary_mode = True
        self.clear_device_cards()
        aborted = self.stop_flag
        self.status_label.setText("PURGED" if aborted else "SATURATED")
        self.status_label.setStyleSheet(
            f"color:{C_HAZARD if aborted else C_TOXIC};font-size:15px;"
            f"font-weight:800;letter-spacing:1.8px;font-family:'{F_UI}';"
            f"background:transparent")
        self.msg_label.setText("Purged by operator — Facebook Lite closed."
                               if aborted else
                               "All hosts dosed — Facebook Lite closed.")
        self.msg_label.setStyleSheet(
            f"color:{C_MUTE};font-size:10px;font-family:'{F_MONO}';"
            f"background:transparent")
        self.progress_bar.setValue(100)
        total_reacted = sum(
            1 for s in results.values()
            for st in (s if isinstance(s, list) else [s])
            if str(st).startswith("reacted:") or str(st).startswith("commented:"))
        for i, serial in enumerate(devices):
            st = results.get(serial, [])
            if isinstance(st, list):
                if any(("reacted" in str(s) or "commented" in str(s)) for s in st):
                    status = "done"
                elif any("suspend" in str(s).lower() for s in st):
                    status = "suspended"
                else:
                    status = "failed"
            else:
                status = "failed"
            detail = ", ".join(str(s) for s in (st if isinstance(st, list) else [st]))
            card = DeviceCard(serial)
            card.set_status(status, detail)
            self.device_cards[serial] = card
            self.dev_card_layout.addWidget(card)
            card.play_in(i * 45)
        self.stat_devices.setText(f"{len(devices)} / {len(devices)}")
        self.stat_reactions.setText(f"{total_reacted}")
        self.refresh_link_tally()

    def _set_device_comment(self, serial, enabled):
        self.device_comment[serial] = bool(enabled)
        self.update_briefing()

    def device_comments(self, serial):
        """Does this phone post comments? Defaults to yes."""
        return self.device_comment.get(serial, True)

    # ── mass links ──
    def add_link_row(self, animate=False):
        row = LinkRow(len(self.link_rows) + 1,
                      on_remove=self.remove_link_row,
                      on_change=self.update_briefing)
        self.link_rows.append(row)
        self.links_layout.addWidget(row)
        do_react, do_comment = parse_action_mode(self.action_seg.value())
        row.set_columns(do_react, do_comment)
        if animate:
            row.play_in(0)
        else:
            row._in = 1.0
        self._relayout_for_links(animate=animate)
        self.update_briefing()
        return row

    def remove_link_row(self, row):
        if len(self.link_rows) <= 1:
            self.send_cmd({"action": "toast", "text": "At least one link is required"})
            return
        self.link_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        for i, r in enumerate(self.link_rows, start=1):
            r.set_index(i)
        self._relayout_for_links(animate=True)
        self.update_briefing()

    def _relayout_for_links(self, animate=False):
        """One link = compact form. Two or more = the panel grows into a mass
        table and the whole layout rebalances, with a 3D fold over the change."""
        n = len(self.link_rows)
        mass = n >= 2
        # the count label must track EVERY add/remove, not just the mode flip
        self.links_lbl.setText(
            f"TARGET VECTORS  ·  {n} LINKS" if mass else "TARGET VECTOR  ·  LINK")
        if mass == getattr(self, "_mass_mode", None):
            return                                  # layout already correct
        was = getattr(self, "_mass_mode", None)
        self._mass_mode = mass
        if animate and was is not None and hasattr(self, "layout_fx"):
            self.layout_fx.play(self.cfg_panel)     # 3D flip over the old look
        # give the payload panel more of the window once it is a table
        self.middle_layout.setStretch(0, 8 if mass else 5)
        self.middle_layout.setStretch(1, 5 if mass else 4)
        self.links_scroll.setMinimumHeight(210 if mass else 60)
        if mass and not self.isMaximized():
            want = 1340 if self.width() < 1340 else self.width()
            self.resize(want, max(self.height(), 820))

    def link_targets(self):
        do_react, do_comment = parse_action_mode(self.action_seg.value())
        out = []
        for r in self.link_rows:
            t = r.target()
            if not t["url"]:
                continue
            out.append(fbcore.make_target(t["url"], t["reaction"], t["comment"],
                                          do_react, do_comment))
        return out

    def update_action_fields(self):
        mode = self.action_seg.value()
        do_react, do_comment = parse_action_mode(mode)
        for r in getattr(self, "link_rows", []):
            r.set_columns(do_react, do_comment)
        self.update_briefing()

    def update_briefing(self):
        if not hasattr(self, "briefing") or not hasattr(self, "link_rows"):
            return
        mode = self.action_seg.value()
        do_react, do_comment = parse_action_mode(mode)
        tg = self.link_targets()
        n_sel = len(self.get_selected_devices())
        n_links = len(tg)
        rows = [("links", f"{n_links} ready" if n_links else "none set",
                 C_TOXIC if n_links else C_HAZARD),
                ("strain", mode.lower(), C_TOXIC)]
        if n_links == 1:
            short = tg[0]["url"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            rows.append(("vector", short, C_TEXT))
            if do_react:
                rows.append(("toxin", tg[0]["reaction"].upper(), C_VENOM))
        elif n_links > 1:
            mix = ", ".join(sorted({t["reaction"] for t in tg}))
            if do_react:
                rows.append(("toxins", mix.upper(), C_VENOM))
        if do_comment:
            missing = sum(1 for t in tg if not t["comment"])
            rows.append(("samples",
                         "all set" if not missing else f"{missing} missing",
                         C_TOXIC if not missing else C_HAZARD))
        rows.append(("hosts", f"{n_sel} selected",
                     C_TOXIC if n_sel else C_HAZARD))
        if do_comment and n_sel:
            sel = self.get_selected_devices()
            talk = sum(1 for s in sel if self.device_comments(s))
            rows.append(("commenting",
                         f"{talk} of {n_sel} hosts" if talk else "none (react only)",
                         C_VENOM if talk else C_HAZARD))
        if n_links and n_sel:
            rows.append(("doses", f"{n_links} x accounts x {n_sel}", C_MUTE))
        rows.append(("evidence", "on" if self.screenshot_check.isChecked() else "off",
                     C_TOXIC if self.screenshot_check.isChecked() else C_FAINT))
        self.briefing.set_rows(rows)

    def start_reaction(self):
        mode = self.action_seg.value()
        do_react, do_comment = parse_action_mode(mode)
        targets = self.link_targets()
        if not targets:
            self.send_cmd({"action": "toast", "text": "Enter at least one post URL"})
            return
        # normalise back into the rows so the user sees what will be used
        by_url = {}
        for row in self.link_rows:
            raw = row.url.text().strip()
            if raw:
                fixed = normalize_url(raw)
                if fixed:
                    row.url.setText(fixed)
                    by_url[fixed] = True
        targets = self.link_targets()
        if do_react and any(t["reaction"] not in REACTIONS for t in targets):
            self.send_cmd({"action": "toast", "text": "Select a valid reaction"})
            return
        if do_comment:
            missing = [i + 1 for i, t in enumerate(targets) if not t["comment"]]
            if missing:
                self.send_cmd({"action": "toast",
                               "text": f"Link {missing[0]} needs an example comment"})
                return
        reaction = targets[0]["reaction"]
        comment_example = targets[0]["comment"]
        selected = self.get_selected_devices()
        if not selected:
            self.send_cmd({"action": "toast", "text": "Select at least one host"})
            return

        self.reaction_count = 0
        self.device_count   = 0
        self.total_devices  = len(selected)
        self.active_devices = list(selected)
        self.tally_acc = {}                    # fresh per-link counts per run
        self.summary_mode   = False
        self.running        = True
        self.stop_flag      = False
        self.start_time     = time.time()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # fire the toxin surge from the needle (the INJECT button itself)
        try:
            btn = self.start_btn.geometry()
            origin = self.start_btn.parentWidget().mapTo(
                self.centralWidget(),
                QPoint(btn.x() + btn.width() // 2, btn.y() + btn.height() // 2))
            self.inject_fx.setGeometry(0, 0,
                                       self.centralWidget().width(),
                                       self.centralWidget().height())
            self.inject_fx.fire(QPointF(origin))
        except Exception:
            pass

        self.send_cmd({"action": "reset_dash",    "devices": selected})
        self.send_cmd({"action": "status",        "text": "Spreading…", "color": C_TOXIC})
        self.send_cmd({"action": "msg",           "text": "Dispersing payload…", "color": C_MUTE})
        self.send_cmd({"action": "devices_init",  "devices": selected})
        self.output_queue = queue.Queue()

        take_screenshots = self.screenshot_check.isChecked()

        t = threading.Thread(
            target=self.run_reactions,
            args=(targets, reaction, selected, comment_example, do_react,
                  do_comment, take_screenshots),
            daemon=True)
        t.start()

    def run_reactions(self, targets, reaction, devices,
                      comment_example="", do_react=True, do_comment=False,
                      take_screenshots=False):
        old_out    = sys.stdout
        sys.stdout = io.StringIO()
        # Must set the flag on the CORE module itself. Rebinding a name imported
        # into this module would only change gui.STOP_REQUESTED, leaving
        # core.stop_requested() permanently False — i.e. Abort would be ignored
        # by every check inside core, and the workers would keep going.
        fbcore.STOP_REQUESTED = lambda: self.stop_flag
        try:
            # Resolve opaque share/watch links once before the device workers
            # fan out. This keeps FBPoison aligned with FBVenom: every phone
            # receives the same canonical target, while an unresolved /share/v/
            # link remains available as the safe opener fallback.
            targets = fbcore.normalize_targets_for_phones(targets)
            if not targets:
                self.send_cmd({"action": "msg",
                               "text": "No valid Facebook post URLs",
                               "color": C_BLOOD})
                return
            if do_comment:
                self.send_cmd({"action": "msg",
                               "text": "Binding language endpoint…",
                               "color": C_MUTE})
                if not start_llama():
                    self.send_cmd({"action": "msg",
                                   "text": "AI endpoint unreachable — comments disabled",
                                   "color": C_BLOOD})
                    do_comment = False
                    if not do_react:
                        return
            apk          = find_apk()
            reset_comment_pool()
            results      = {}
            results_lock = threading.Lock()
            completed    = [0]
            total        = len(devices)

            def mark_done():
                with results_lock:
                    completed[0] += 1
                    done_n  = completed[0]
                    react_n = self.reaction_count
                self.device_count = done_n
                self.send_cmd({"action": "progress",
                               "pct": int(done_n / total * 100)})
                self.send_cmd({"action": "stats",
                               "devices":   done_n,
                               "total":     total,
                               "reactions": react_n})

            def pre_wake(serial):
                try:
                    wake_and_unlock(serial)
                except Exception:
                    pass
            wakers = [threading.Thread(target=pre_wake, args=(s,), daemon=True)
                      for s in devices]
            for w in wakers:
                w.start()
            for w in wakers:
                w.join()

            def worker(serial):
                self.send_cmd({"action": "device_status",
                               "serial": serial, "status": "processing"})

                try:
                    if not self.stop_flag:
                        wait_online(serial)
                    if not self.stop_flag and not has_fb(serial):
                        if apk and install_fb(serial, apk):
                            pass
                        elif apk:
                            with results_lock:
                                results[serial] = "install-failed"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "failed"})
                            mark_done()
                            return
                        else:
                            with results_lock:
                                results[serial] = "missing-app-no-apk"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "failed"})
                            mark_done()
                            return
                    # Per-device comment switch: a phone set to REACT ONLY gets
                    # its own copy of the targets with commenting turned off.
                    dev_targets = targets
                    if do_comment and not self.device_comments(serial):
                        dev_targets = [dict(t, do_comment=False) for t in targets]
                        if not do_react:
                            # comment-only run + this device may not comment
                            with results_lock:
                                results[serial] = "skipped-react-only-device"
                            self.send_cmd({"action": "device_status",
                                           "serial": serial, "status": "pending"})
                            mark_done()
                            return
                    # mass links: one call, every link handled per account
                    res = react_all_saved_accounts(
                        serial, dev_targets, reaction, comment_example,
                        do_react, do_comment, take_screenshots)
                except Exception as e:
                    res = [f"device-error:{e}"]

                lst = res if isinstance(res, list) else [res]
                rc  = sum(1 for s in lst if str(s).startswith("reacted:"))
                cc  = sum(1 for s in lst if str(s).startswith("commented:"))
                with results_lock:
                    results[serial]      = res
                    self.reaction_count += rc + cc
                    # roll this phone's per-link counts into the run total
                    for no, row in getattr(res, "link_tally", {}).items():
                        acc = self.tally_acc.setdefault(
                            no, {"reacted": 0, "commented": 0, "failed": 0,
                                 "url": row.get("url", "")})
                        acc["url"] = acc["url"] or row.get("url", "")
                        for k in ("reacted", "commented", "failed"):
                            acc[k] += row.get(k, 0)
                if any(("reacted" in str(s) or "commented" in str(s)) for s in lst):
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "done"})
                elif "suspended" in str(res):
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "suspended"})
                else:
                    self.send_cmd({"action": "device_status",
                                   "serial": serial, "status": "failed"})
                mark_done()

            _worker_body = worker
            def worker(serial):
                # Hold the phone for the whole run: a hover-triggered account
                # probe force-stops Facebook and walks to the menu, and landing
                # that mid-run yanked the post away, so the run just re-opened
                # and scrolled instead of reaching the link.
                lock = self.device_lock(serial)
                lock.acquire()
                try:
                    _worker_body(serial)
                finally:
                    lock.release()

            workers = [threading.Thread(target=worker, args=(s,), daemon=True)
                       for s in devices]
            for wk in workers:
                wk.start()
            for wk in workers:
                wk.join()

            self.send_cmd({"action": "progress", "pct": 100})
            self.send_cmd({"action": "stats",
                           "devices":   total,
                           "total":     total,
                           "reactions": self.reaction_count})
            self.send_cmd({"action": "show_summary",
                           "results": results, "devices": devices})
        except Exception as e:
            self.send_cmd({"action": "msg", "text": f"Error: {e}", "color": C_BLOOD})
        finally:
            stop_llama()
            sys.stdout = old_out
            QTimer.singleShot(0, self.reaction_finished)

    def open_screenshots_folder(self):
        folder = os.path.join(HERE, "screenshots")
        try:
            os.makedirs(folder, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self.send_cmd({"action": "toast",
                           "text": f"Couldn't open evidence vault: {e}"})

    def reaction_finished(self):
        self.running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.summary_mode = True

    def stop_reaction(self):
        # Abort is immediate: raise the flag (every wait in core polls it within
        # ~0.1s) and force-stop Facebook Lite on every device right now, without
        # waiting for the workers to unwind.
        self.stop_flag = True
        self.stop_btn.setEnabled(False)
        self.send_cmd({"action": "status", "text": "Purging…", "color": C_HAZARD})
        self.send_cmd({"action": "msg",
                       "text": "Purge — halting and closing Facebook Lite…",
                       "color": C_HAZARD})
        devices = list(getattr(self, "active_devices", [])) or self.get_selected_devices()

        def kill():
            try:
                abort_devices(devices)
            except Exception as e:
                self.send_cmd({"action": "msg",
                               "text": f"Purge: couldn't close Facebook Lite: {e}",
                               "color": C_BLOOD})
            else:
                self.send_cmd({"action": "msg",
                               "text": "Purged — Facebook Lite closed.",
                               "color": C_HAZARD})
        threading.Thread(target=kill, daemon=True).start()


class DoseRate(QWidget):
    """Segmented control for the pacing profile: SAFE / FAST / TURBO.

    Faster profiles wait less between actions, links and accounts. That is the
    whole speed-up, and it is also the whole risk: the waiting is what keeps
    Facebook from action-blocking the accounts.
    """

    STEPS = [("SAFE", C_TOXIC, "safe"),
             ("FAST", C_VENOM, "fast"),
             ("TURBO", C_BLOOD, "turbo")]

    def __init__(self, on_change=None, parent=None):
        super().__init__(parent)
        self.setFixedSize(186, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._i = 0
        self._on_change = on_change
        self._glide = 0.0
        self._a = QVariantAnimation(self, duration=260,
                                    startValue=0.0, endValue=0.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutBack)
        self._a.valueChanged.connect(self._set_glide)
        self.setToolTip("dose rate — how hard the run is paced\n"
                        "SAFE: human timing, lowest block risk\n"
                        "FAST: ~2x quicker, fine for aged accounts\n"
                        "TURBO: minimal waiting, expect action blocks")

    def _set_glide(self, v):
        self._glide = float(v); self.update()

    def profile(self):
        return self.STEPS[self._i][2]

    def set_index(self, i):
        i = max(0, min(len(self.STEPS) - 1, int(i)))
        if i == self._i:
            return
        self._a.stop()
        self._a.setStartValue(self._glide)
        self._a.setEndValue(float(i))
        self._a.start()
        self._i = i
        if self._on_change:
            self._on_change(self.profile())

    def mousePressEvent(self, e):
        seg = self.width() / len(self.STEPS)
        self.set_index(int(e.position().x() // seg))
        super().mousePressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        seg = w / len(self.STEPS)
        p.fillPath(cyber_path(QRectF(0.5, 0.5, w - 1, h - 1), 7, tr=True, bl=True),
                   _qc("#08110C", 215))
        # sliding knob rides between segments
        accent = self.STEPS[self._i][1]
        kx = self._glide * seg
        knob = QRectF(kx + 2, 2.5, seg - 4, h - 5)
        p.fillPath(cyber_path(knob, 6, tr=True, bl=True), _qc(accent, 46))
        p.setPen(QPen(_qc(accent, 210), 1))
        p.drawPath(cyber_path(knob, 6, tr=True, bl=True))
        p.setPen(QPen(QColor(C_BORDER), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(cyber_path(QRectF(0.5, 0.5, w - 1, h - 1), 7, tr=True, bl=True))
        f = QFont(F_UI, 7, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        p.setFont(f)
        for i, (label, col, _key) in enumerate(self.STEPS):
            live = (i == self._i)
            p.setPen(QColor(col if live else C_FAINT))
            p.drawText(QRectF(i * seg, 0, seg, h),
                       Qt.AlignmentFlag.AlignCenter, label)
        p.end()


class CommentPill(QPushButton):
    """Per-device switch: does THIS phone post comments, or only react?"""

    def __init__(self, on=True, on_toggle=None, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(104, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hot = 0.0
        self._a = QVariantAnimation(self, duration=150,
                                   startValue=0.0, endValue=1.0)
        self._a.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._a.valueChanged.connect(self._set_hot)
        if on_toggle:
            self.toggled.connect(on_toggle)
        self.toggled.connect(lambda _b: self.update())

    def _set_hot(self, v):
        self._hot = float(v); self.update()

    def enterEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Forward); self._a.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._a.setDirection(QVariantAnimation.Direction.Backward); self._a.start()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        w, h = self.width(), self.height()
        r = QRectF(0.5, 0.5, w - 1, h - 1)
        path = cyber_path(r, 7, tr=True, bl=True)
        accent = C_VENOM if on else C_FAINT
        p.fillPath(path, _qc("#0A140E", 200 + 40 * self._hot))
        p.setPen(QPen(QColor(_lerp_hex(C_BORDER, accent, 0.55 + 0.45 * self._hot)), 1))
        p.drawPath(path)
        # little speech bubble
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc(accent, 235 if on else 120))
        p.drawEllipse(QRectF(7, h / 2 - 5, 11, 9))
        p.setPen(QColor(_lerp_hex(C_MUTE, accent, 1.0 if on else 0.25)))
        f = QFont(F_UI, 7, QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(f)
        p.drawText(QRectF(23, 0, w - 27, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "COMMENTS" if on else "REACT ONLY")
        p.end()


class LinkRow(QFrame):
    """One target: <link> <reaction> <example comment>."""

    REACTION_EMOJI = {"like": "\U0001F44D", "love": "❤️", "care": "\U0001F970",
                      "haha": "\U0001F606", "wow": "\U0001F62E",
                      "sad": "\U0001F622", "angry": "\U0001F621"}

    def __init__(self, index, on_remove=None, on_change=None, parent=None):
        super().__init__(parent)
        self.index = index
        self._on_remove = on_remove
        self.setFixedHeight(46)
        self._in = 0.0
        self._anim = QVariantAnimation(self, duration=380,
                                       startValue=0.0, endValue=1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_in)

        lo = QHBoxLayout(self)
        lo.setContentsMargins(12, 6, 8, 6)
        lo.setSpacing(8)

        self.num = QLabel(str(index))
        self.num.setFixedWidth(16)
        self.num.setStyleSheet(
            f"color:{C_TOXIC};font-size:11px;font-weight:800;"
            f"font-family:'{F_MONO}';background:transparent")
        lo.addWidget(self.num)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://facebook.com/…")
        self.url.setStyleSheet(_INPUT_QSS.replace("padding:10px 13px",
                                                  "padding:6px 9px"))
        lo.addWidget(self.url, 5)

        self.reaction = QComboBox()
        for r in ("like", "love", "care", "haha", "wow", "sad", "angry"):
            self.reaction.addItem(f"{self.REACTION_EMOJI[r]}  {r.title()}", r)
        self.reaction.setFixedWidth(118)
        self.reaction.setStyleSheet(
            f"QComboBox{{background:{C_INPUT};color:{C_TEXT};"
            f"border:1px solid {C_BORDER};padding:6px 8px;font-size:12px}}"
            f"QComboBox:hover{{border:1px solid {C_TOXIC}}}"
            f"QComboBox::drop-down{{border:none;width:16px}}"
            f"QComboBox QAbstractItemView{{background:{C_CARD};color:{C_TEXT};"
            f"border:1px solid {C_BORDER};selection-background-color:{C_TOXIC};"
            f"selection-color:#04240B;outline:none}}")
        lo.addWidget(self.reaction)

        self.comment = QLineEdit()
        self.comment.setPlaceholderText("example comment…")
        self.comment.setStyleSheet(_INPUT_QSS.replace("padding:10px 13px",
                                                      "padding:6px 9px"))
        lo.addWidget(self.comment, 4)

        self.kill = GhostButton("✕", C_BLOOD)
        self.kill.setFixedWidth(30)
        self.kill.clicked.connect(lambda: self._on_remove and self._on_remove(self))
        lo.addWidget(self.kill)

        if on_change:
            self.url.textChanged.connect(lambda _t: on_change())
            self.comment.textChanged.connect(lambda _t: on_change())
            self.reaction.currentIndexChanged.connect(lambda _i: on_change())

    def _set_in(self, v):
        self._in = float(v); self.update()

    def play_in(self, delay=0):
        QTimer.singleShot(delay, self._anim.start)

    def set_index(self, i):
        self.index = i
        self.num.setText(str(i))

    def set_columns(self, show_reaction, show_comment):
        self.reaction.setVisible(show_reaction)
        self.comment.setVisible(show_comment)

    def target(self):
        return {"url": self.url.text().strip(),
                "reaction": self.reaction.currentData(),
                "comment": self.comment.text().strip()}

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        prog = max(0.0, min(1.0, self._in)) if self._anim.state() or self._in else 1.0
        p.setOpacity(prog)
        w, h = self.width(), self.height()
        p.translate((1.0 - prog) * 26, 0)
        r = QRectF(0.5, 0.5, w - 1, h - 1)
        path = cyber_path(r, 9, tr=True, bl=True)
        p.fillPath(path, _qc("#0A140E", 225))
        p.setPen(QPen(_qc(C_BORDER, 230), 1))
        p.drawPath(path)
        p.fillRect(QRectF(1, 1, 2.4, h - 2), _qc(C_TOXIC, 190))
        p.end()


class PanelFlipFX(QWidget):
    """3D fold: the panel is grabbed and swung about its Y axis with real
    perspective (quadToQuad) while the layout changes underneath it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        self.shot = None
        self.rect_ = QRectF()
        self.t = 0.0
        self.dur = 0.55
        self.active = False

    def play(self, widget):
        try:
            self.shot = widget.grab()
            tl = widget.mapTo(self.parentWidget(), QPoint(0, 0))
            self.rect_ = QRectF(tl.x(), tl.y(), widget.width(), widget.height())
        except Exception:
            return
        self.setGeometry(self.parentWidget().rect())
        self.t = 0.0
        self.active = True
        self.show()
        self.raise_()
        self.update()

    def advance(self, dt):
        if not self.active:
            return
        self.t += dt
        if self.t >= self.dur:
            self.active = False
            self.hide()
            return
        self.update()

    def paintEvent(self, _e):
        if not self.active or self.shot is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        u = self.t / self.dur
        # swing out and back: a half turn about Y with perspective foreshortening
        ang = math.sin(u * math.pi) * 0.55
        r = self.rect_
        cx, cy = r.center().x(), r.center().y()
        hw, hh = r.width() / 2.0, r.height() / 2.0
        focal = 900.0
        pts = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y, z = sx * hw, sy * hh, 0.0
            xr = x * math.cos(ang) + z * math.sin(ang)
            zr = -x * math.sin(ang) + z * math.cos(ang)
            k = focal / (focal + zr)
            pts.append(QPointF(cx + xr * k, cy + y * k))
        src = QPolygonF([QPointF(0, 0), QPointF(self.shot.width(), 0),
                         QPointF(self.shot.width(), self.shot.height()),
                         QPointF(0, self.shot.height())])
        dst = QPolygonF(pts)
        from PyQt6.QtGui import QTransform
        tf = QTransform()
        if QTransform.quadToQuad(src, dst, tf):
            p.setOpacity(1.0 - u * 0.35)
            p.setTransform(tf)
            p.drawPixmap(0, 0, self.shot)
        p.end()


def _smooth(a, b, x):
    """Smoothstep between a and b."""
    if b <= a:
        return 0.0 if x < a else 1.0
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3.0 - 2.0 * t)


class InjectOverlay(QWidget):
    """The toxin surge played when INJECT is pressed.

    Genuinely 3D: the fluid is a world-space heightfield (summed travelling
    sine waves) drawn as depth-sorted strips through the same perspective
    projection as the rest of the app, lit by a fake normal-dot-light term.
    Droplets are ballistic in 3D and composited additively so overlapping
    blobs merge like metaballs. Purely decorative — it never blocks the run.
    """

    DURATION = 2.15

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()
        self.t = 0.0
        self.active = False
        self.origin = QPointF(0, 0)
        self.drops = []
        self.rings = []
        self.bubbles = []
        # pre-rendered droplet blob: building a QRadialGradient per droplet per
        # frame was the hot spot, and blitting composites identically under
        # CompositionMode_Plus.
        self._blob = _glow_pixmap(C_TOXIC2, 96,
                                  stops=((0.0, 235), (0.40, 120), (1.0, 0)))

    # ── control ──
    def fire(self, origin: QPointF):
        self.origin = QPointF(origin)
        self.t = 0.0
        self.active = True
        self.drops = []
        self.rings = []
        self.bubbles = [{"x": random.uniform(-4.4, 4.4),
                         "z": random.uniform(1.2, 8.0),
                         "p": random.uniform(0, 6.28),
                         "r": random.uniform(1.6, 4.2)} for _ in range(46)]
        # ballistic droplets thrown out of the needle — spawned at the world
        # position that projects back onto the INJECT button, so the spray
        # actually erupts from the button rather than from screen centre
        W, H = max(1, self.width()), max(1, self.height())
        ocx, ocy = W / 2.0, H * 0.52
        ofocal = H * 0.62
        for _ in range(46):
            ang = random.uniform(-math.pi, math.pi)
            spd = random.uniform(1.6, 4.6)
            z0 = random.uniform(1.6, 4.6)
            # aim the burst back across the window instead of straight up, so
            # the spray travels the full width rather than staying in the corner
            ox_w = (self.origin.x() - ocx) * z0 / ofocal
            self.drops.append({
                "x": ox_w + random.uniform(-0.16, 0.16),
                "y": (self.origin.y() - ocy) * z0 / ofocal + random.uniform(-0.10, 0.06),
                "z": z0,
                "vx": -abs(math.cos(ang)) * spd * random.uniform(0.8, 2.1)
                      * (1.0 if ox_w > 0 else -1.0),
                "vy": -random.uniform(2.4, 5.2),
                "vz": math.sin(ang) * spd * 0.30,
                "r": random.uniform(0.045, 0.135),
                "t0": 0.0,
                "trail": [],
                "dead": False,
            })
        # splash-back: the rising pool throws droplets up across the whole
        # width, so the effect reads as the entire UI being flooded
        for i in range(40):
            x0 = -5.0 + 10.0 * (i + random.uniform(0.1, 0.9)) / 40.0
            self.drops.append({
                "x": x0,
                "y": 1.2,
                "z": random.uniform(1.3, 7.5),
                "vx": random.uniform(-0.5, 0.5),
                "vy": -random.uniform(1.6, 3.6),
                "vz": random.uniform(-0.2, 0.2),
                "r": random.uniform(0.035, 0.105),
                "t0": 0.22 + random.uniform(0.0, 0.34),
                "trail": [],
                "dead": False,
            })
        self.show()
        self._sync_to_parent()   # full-size + on top before the first frame
        self.raise_()
        self.update()

    def _sync_to_parent(self):
        """Keep the overlay exactly covering its parent, always on top.

        QMainWindow.resizeEvent fires BEFORE the central widget is re-laid out,
        so sizing from it there leaves a stale (smaller) rect after a
        maximise/resize — which clips the effect to part of the window. The
        parent's rect is the authority, so re-assert it every frame.
        """
        par = self.parentWidget()
        if par is None:
            return
        if self.geometry() != par.rect():
            self.setGeometry(par.rect())
        kids = [c for c in par.children() if c.isWidgetType()]
        if kids and kids[-1] is not self:
            self.raise_()

    def advance(self, dt):
        if not self.active:
            return
        self._sync_to_parent()
        self.t += dt
        u = self.t / self.DURATION
        if u >= 1.0:
            self.active = False
            self.hide()
            return
        lvl = self._level(u)
        for d in self.drops:
            if d["dead"] or self.t < d.get("t0", 0.0):
                continue
            d["vy"] += 7.4 * dt                      # gravity
            d["x"] += d["vx"] * dt
            d["y"] += d["vy"] * dt
            d["z"] += d["vz"] * dt
            d["trail"].append((d["x"], d["y"], d["z"]))
            if len(d["trail"]) > 7:
                d["trail"].pop(0)
            if d["y"] >= lvl and d["vy"] > 0:        # hit the fluid
                d["dead"] = True
                self.rings.append({"x": d["x"], "z": max(0.9, d["z"]),
                                   "age": 0.0, "r0": d["r"] * 1.4})
        for r in self.rings:
            r["age"] += dt
        self.rings = [r for r in self.rings if r["age"] < 0.85]
        self.update()

    # ── shape of the pour ──
    def _level(self, u):
        """World-space Y of the fluid surface (smaller = higher on screen)."""
        rise = _smooth(0.10, 0.52, u)
        fall = _smooth(0.74, 1.0, u)
        k = max(0.0, rise - fall)
        return 2.55 - 3.05 * k

    def _wave(self, x, z, u):
        t = self.t
        return (0.085 * math.sin(x * 1.5 + t * 4.6)
                + 0.055 * math.sin(z * 1.9 - t * 3.4)
                + 0.035 * math.sin((x + z) * 2.7 + t * 6.1))

    def paintEvent(self, _e):
        if not self.active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        u = self.t / self.DURATION
        cx, cy = w / 2.0, h * 0.52
        focal = h * 0.62
        lvl = self._level(u)

        # ── 0. impact flash ──
        flash = max(0.0, 1.0 - abs(u - 0.13) / 0.13)
        if flash > 0.01:
            p.fillRect(0, 0, w, h, _qc(C_TOXIC, 42 * flash))

        # ── 0b. shockwave sweeping the whole window from the needle ──
        sw = _smooth(0.06, 0.62, u)
        if 0.0 < sw < 1.0:
            reach = math.hypot(w, h) * 1.15
            rr = reach * sw
            a = int(170 * (1.0 - sw) ** 1.4)
            if a > 2:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_qc(C_TOXIC2, a), 3.0 * (1.0 - sw) + 0.8))
                p.drawEllipse(self.origin, rr, rr * 0.72)
                p.setPen(QPen(_qc(C_TOXIC, int(a * 0.5)), 1.4))
                p.drawEllipse(self.origin, rr * 0.82, rr * 0.82 * 0.72)

        # ── 1. charge ring collapsing into the needle ──
        if u < 0.20:
            k = 1.0 - u / 0.20
            rr = 26 + 210 * k
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(C_TOXIC2, int(220 * (1 - k))), 2.4))
            p.drawEllipse(self.origin, rr, rr * 0.42)

        # ── 2. surge column bursting from the needle ──
        col = _smooth(0.05, 0.30, u) * (1.0 - _smooth(0.42, 0.72, u))
        if col > 0.01:
            ox, oy = self.origin.x(), self.origin.y()
            cw = 26 + 54 * col
            top = oy - (h * 0.78) * col
            grad = QLinearGradient(0, top, 0, oy)
            grad.setColorAt(0.0, _qc(C_TOXIC2, 0))
            grad.setColorAt(0.35, _qc(C_TOXIC2, int(150 * col)))
            grad.setColorAt(1.0, _qc(C_TOXIC, int(215 * col)))
            path = QPainterPath()
            path.moveTo(ox - cw * 0.5, oy)
            steps = 16
            for i in range(steps + 1):
                f = i / steps
                yy = oy + (top - oy) * f
                bulge = math.sin(f * math.pi) * 16 * col
                wob = math.sin(f * 9.0 - self.t * 15.0) * 5.0 * col
                path.lineTo(ox - cw * 0.5 - bulge + wob, yy)
            for i in range(steps, -1, -1):
                f = i / steps
                yy = oy + (top - oy) * f
                bulge = math.sin(f * math.pi) * 16 * col
                wob = math.sin(f * 9.0 - self.t * 15.0) * 5.0 * col
                path.lineTo(ox + cw * 0.5 + bulge + wob, yy)
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.fillPath(path, QBrush(grad))

        # ── 3. the 3D fluid surface ──
        if lvl < 2.5:
            GX, GZ = 24, 13
            zs = [0.95 + (9.6 - 0.95) * (i / GZ) ** 1.35 for i in range(GZ + 1)]
            rows = []
            for z in zs:
                pts = []
                for i in range(GX + 1):
                    x = -5.2 + 10.4 * i / GX
                    y = lvl + self._wave(x, z, u)
                    sx, sy, d = project((x, y, z), focal)
                    pts.append((cx + sx, cy + sy, y))
                rows.append((z, pts))

            # body of the pool: everything below the near surface edge, so it
            # reads as a filling volume of toxin instead of a floating ribbon
            _zn0, near0 = rows[0]
            body = QPainterPath()
            body.moveTo(near0[0][0], near0[0][1])
            for x_, y_, _wy in near0[1:]:
                body.lineTo(x_, y_)
            body.lineTo(w + 40, h + 40)
            body.lineTo(-40, h + 40)
            body.closeSubpath()
            bg = QLinearGradient(0, near0[0][1] - 30, 0, h)
            bg.setColorAt(0.0, _qc(C_TOXIC, 205))
            bg.setColorAt(0.35, _qc("#1E9A2C", 195))
            bg.setColorAt(1.0, _qc("#04240B", 225))
            p.setPen(Qt.PenStyle.NoPen)
            p.fillPath(body, QBrush(bg))

            for k in range(len(rows) - 1, 0, -1):     # far -> near
                zf, far = rows[k]
                zn, near = rows[k - 1]
                depth_t = max(0.0, min(1.0, 1.0 - (zf - 0.95) / 8.8))
                poly = QPolygonF()
                for x_, y_, _wy in far:
                    poly.append(QPointF(x_, y_))
                for x_, y_, _wy in reversed(near):
                    poly.append(QPointF(x_, y_))
                # fake lighting from the surface slope
                slope = (near[GX // 2][2] - far[GX // 2][2])
                lit = max(0.0, min(1.0, 0.5 + slope * 2.6))
                base = QColor(_lerp_hex("#0C5B1E", C_TOXIC, 0.25 + 0.55 * depth_t))
                col_ = QColor(_lerp_hex(base.name(), C_TOXIC2, lit * 0.45))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_qc(col_.name(), int(150 + 90 * depth_t)))
                p.drawPolygon(poly)

            # crest highlight along the nearest edge
            zn, near = rows[0]
            crest = QPainterPath()
            crest.moveTo(near[0][0], near[0][1])
            for x_, y_, _wy in near[1:]:
                crest.lineTo(x_, y_)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc("#EAFFDA", 120), 1.3))
            p.drawPath(crest)

            # bubbles suspended in the toxin
            p.setPen(Qt.PenStyle.NoPen)
            for b in self.bubbles:
                by = lvl + 0.30 + 0.16 * math.sin(self.t * 2.4 + b["p"])
                sx, sy, d = project((b["x"], by, b["z"]), focal)
                rr = max(1.0, b["r"] / d * 2.4)
                p.setBrush(_qc("#DFFFC8", 70))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rr, rr * 0.7)

            # impact ripples riding the surface
            p.setBrush(Qt.BrushStyle.NoBrush)
            for r in self.rings[-14:]:
                a = 1.0 - r["age"] / 0.85
                rad = r["r0"] + r["age"] * 0.85     # world units, not screen
                sx, sy, d = project((r["x"], lvl, r["z"]), focal)
                rw = max(2.0, rad * focal / d)
                p.setPen(QPen(_qc("#EAFFDA", int(95 * a)), 1.2))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rw, rw * 0.28)

        # ── 4. metaball droplets (additive so they merge) ──
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.setPen(Qt.PenStyle.NoPen)
        for d in self.drops:
            if d["dead"] or self.t < d.get("t0", 0.0):
                continue
            for ti, (tx, ty, tz) in enumerate(d["trail"][::2]):
                sx, sy, dd = project((tx, ty, tz), focal)
                rr = max(0.8, d["r"] * focal / dd) * (0.30 + 0.55 * ti / 4.0)
                p.setBrush(_qc(C_TOXIC, 26))
                p.drawEllipse(QPointF(cx + sx, cy + sy), rr, rr)
            sx, sy, dd = project((d["x"], d["y"], d["z"]), focal)
            rr = max(1.1, d["r"] * focal / dd) * 2.3
            p.drawPixmap(QRectF(cx + sx - rr, cy + sy - rr, rr * 2, rr * 2),
                         self._blob, QRectF(0, 0, 96, 96))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # ── 5. drain drips on the way out ──
        drain = _smooth(0.80, 1.0, u)
        if drain > 0.01:
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(16):
                fx = (i + 0.5) / 16.0
                ph = math.sin(i * 12.9898) * 43758.5453
                ph -= math.floor(ph)
                dl = (drain - ph * 0.35)
                if dl <= 0:
                    continue
                dy = h * (0.55 + dl * 0.9)
                ww = 3.0 + 4.0 * (1.0 - drain)
                p.setBrush(_qc(C_TOXIC, int(160 * (1.0 - drain))))
                p.drawEllipse(QPointF(w * fx, dy), ww, ww * 2.6)
        p.end()


class _CommandBar(QFrame):
    """Bottom bar with live hazard tape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.t = 0.0
        self.setFixedHeight(66)

    def advance(self, dt):
        self.t += dt
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = cyber_path(r, 14, tr=True, bl=True)
        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0.0, _qc("#0A1610", 235))
        g.setColorAt(1.0, _qc("#060E09", 235))
        p.fillPath(path, QBrush(g))
        p.setPen(QPen(_qc(C_BORDER, 240), 1))
        p.drawPath(path)
        strip = QRectF(1, 1, r.width() - 2, 5).toRect()
        hazard_stripes(p, strip, self.t, alpha=48, band=11)
        p.end()
