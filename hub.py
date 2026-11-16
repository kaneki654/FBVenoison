# -*- coding: utf-8 -*-
"""FBVenoison fleet command: two engines, flexible device allocation."""
import os
import copy
import hashlib
import json
import subprocess
import sys
import tempfile

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QFrame, QLineEdit, QTreeWidget,
    QSpinBox, QProgressBar, QScrollArea, QSplitter,
    QTreeWidgetItem, QAbstractItemView, QHeaderView, QSizePolicy)
from PyQt6.QtCore import Qt, QTimer, QSize, QMimeData, QRectF
from PyQt6.QtGui import (QPainter, QColor, QPen, QBrush, QPixmap, QFont, QIcon,
    QShortcut, QKeySequence, QDrag)

import boxes
from brand_ui import Atmosphere, FusionHero, FleetMetric, emblem
from version import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))


def child_exe(role):
    """The standalone app .exe a frozen hub launches for a role.

    Explicit overrides win. Otherwise prefer the apps shipped with this suite,
    before checking legacy home-folder installations. A one-file hub extracts
    its Python modules to a temporary directory, so installed app paths must
    be resolved relative to the actual executable as well as this source file.
    """
    if role not in (boxes.POISON, boxes.VENOM):
        return None
    env_name = "FBVENOISON_%s_EXE" % ("POISON" if role == boxes.POISON else "VENOM")
    override = os.environ.get(env_name, "").strip()
    if override and os.path.isfile(override):
        return override
    folder, filename = (("FBPoison", "fbpoison.exe") if role == boxes.POISON
                        else ("FBVenom", "FBVenom.exe"))
    roots = []
    if getattr(sys, "frozen", False):
        installed = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(installed)
        # The reproducible hub build is placed in dist/; sibling app builds
        # still live under the project folder in a local developer install.
        if os.path.basename(installed).lower() == "dist":
            roots.append(os.path.dirname(installed))
    roots.append(HERE)
    cands = []
    for root in dict.fromkeys(roots):
        cands.extend((os.path.join(root, folder, "dist", filename),
                      os.path.join(root, folder, filename),
                      os.path.join(root, filename)))
    home = os.path.expanduser("~")
    cands.extend((os.path.join(home, "FBVenoison", folder, "dist", filename),
                  os.path.join(home, folder, "dist", filename)))
    if role == boxes.POISON:
        cands.append(os.path.join(home, "FULDIR", "dist_new", filename))
    for path in cands:
        if os.path.isfile(path):
            return path
    return None


def child_bridge_ready(executable):
    """Bind managed-mode compatibility to the exact rebuilt Venom binary."""
    try:
        marker = os.path.splitext(executable)[0] + ".suite-bridge.json"
        with open(marker, encoding="utf-8") as handle:
            capability = json.load(handle)
        if (capability.get("protocol") != 1 or
                capability.get("app") != "FBVenom"):
            return False
        digest = hashlib.sha256()
        with open(executable, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return capability.get("sha256") == digest.hexdigest()
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# ── palette ────────────────────────────────────────────────────────────────
# A deeper, calmer console. Two layers of elevation (panel, then card) give the
# rail and its blocks real depth; colour is still reserved for the two apps and
# for state, but the surfaces around them are now shaped rather than flat.
BG      = "#080C12"     # window
PANEL   = "#101824"     # rail
CARD    = "#141F2E"     # raised block (launch blocks, header chip)
HOVER   = "#171C23"
SEL     = "#1D242E"
LINE    = "#232A33"
EDGE    = "#2E3742"     # brighter hairline for focus / card edges
TEXT    = "#ECEFF3"
DIM     = "#98A1AD"
FAINT   = "#5A626D"

GREEN   = "#4DE6A4"     # Poison
PURPLE  = "#B495FF"     # Venom
RED     = "#F8655B"
AMBER   = "#E0A52E"
ACCENT  = "#4C8DFF"     # neutral interactive accent (focus rings)

FONT    = "DejaVu Sans" if sys.platform != "win32" else "Segoe UI"
MONO    = "DejaVu Sans Mono" if sys.platform != "win32" else "Consolas"


def _tint(hex_color, factor):
    """Lighten (factor>1) or darken (factor<1) a #rrggbb colour for hover and
    pressed states, so one accent yields a whole button without extra tokens."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return hex_color
    scale = lambda v: max(0, min(255, round(v * factor)))
    return "#%02X%02X%02X" % (scale(r), scale(g), scale(b))

ROLE_COLOR = {boxes.POISON: GREEN, boxes.VENOM: PURPLE, boxes.UNASSIGNED: DIM}
ROLE_NAME  = {boxes.POISON: "Poison", boxes.VENOM: "Venom",
              boxes.UNASSIGNED: "No app"}
STATE_COLOR = {"device": DIM, "offline": RED, "unauthorized": AMBER}
STATE_TEXT  = {"device": "ready", "offline": "offline",
               "unauthorized": "unauthorized"}
GROUPS = (boxes.POISON, boxes.VENOM, boxes.UNASSIGNED)
PHONEBOX_MIME = "application/x-fbvenoison-phonebox"


def backup_assignments(cfg):
    """Keep a recoverable copy before an explicit physical-box realignment."""
    directory = os.path.dirname(os.path.abspath(boxes.CONFIG_PATH))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     prefix="boxes.before-realignment-",
                                     suffix=".json", delete=False) as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
        return fh.name


def app_icon(size=256):
    """The generated serpent and poison emblem, shared with the access gate."""
    return emblem(size)


class HLine(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setStyleSheet("background:%s;border:none" % LINE)


class VLine(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(1)
        self.setStyleSheet("background:%s;border:none" % LINE)


class Btn(QPushButton):
    """solid = the one saturated thing on screen; ghost = everything else."""

    def __init__(self, text, accent=TEXT, kind="ghost", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
        if kind == "solid":
            self.setStyleSheet(
                "QPushButton{background:%s;color:#07090C;border:none;"
                "border-radius:8px;padding:0 16px;font-family:'%s';"
                "font-size:12px;font-weight:700;letter-spacing:0.2px;"
                "text-align:center}"
                "QPushButton:hover{background:%s}"
                "QPushButton:pressed{background:%s}"
                "QPushButton:disabled{background:%s;color:%s}"
                % (accent, FONT, _tint(accent, 1.12), _tint(accent, 0.88),
                   CARD, FAINT))
        elif kind == "quiet":
            self.setStyleSheet(
                "QPushButton{background:transparent;color:%s;border:none;"
                "padding:0 4px;font-family:'%s';font-size:12px;"
                "text-align:left}"
                "QPushButton:hover{color:%s}"
                "QPushButton:disabled{color:%s}" % (DIM, FONT, TEXT, FAINT))
        else:
            self.setStyleSheet(
                "QPushButton{background:%s;color:%s;"
                "border:1px solid %s;border-radius:8px;padding:0 14px;"
                "font-family:'%s';font-size:12px;font-weight:500}"
                "QPushButton:hover{background:%s;color:%s;border:1px solid %s}"
                "QPushButton:pressed{background:%s}"
                "QPushButton:disabled{background:transparent;color:%s;"
                "border:1px solid %s}"
                % (CARD, DIM, LINE, FONT, HOVER, TEXT, EDGE, SEL, FAINT, LINE))


class GroupRow(QWidget):
    """One line in the rail: the group's name, its count, selected or not."""

    def __init__(self, role, on_click, on_drop=None, valid_drop=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.selected = False
        self._on_click = on_click
        self._on_drop = on_drop
        self._valid_drop = valid_drop
        self._drop_hover = False
        self.setAcceptDrops(on_drop is not None)
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lo = QHBoxLayout(self)
        lo.setContentsMargins(14, 0, 12, 0)
        lo.setSpacing(8)
        self.name = QLabel(boxes.APPS[role]["label"].replace("POISON", "Poison")
                           .replace("VENOM", "Venom")
                           if role in boxes.APPS else ROLE_NAME[role])
        self.setToolTip("Drop a phonebox here to move all its phones")
        self.name.setStyleSheet(
            "color:%s;font-family:'%s';font-size:13px;background:transparent"
            % (ROLE_COLOR[role], FONT))
        lo.addWidget(self.name)
        lo.addStretch()
        self.count = QLabel("0")
        self.count.setStyleSheet(
            "color:%s;font-family:'%s';font-size:12px;background:transparent"
            % (DIM, FONT))
        lo.addWidget(self.count)
        for label in (self.name, self.count):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def dragEnterEvent(self, event):
        if self._valid_drop and self._valid_drop(event):
            self._drop_hover = True
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            self.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._drop_hover = False
        self.update()
        event.accept()

    def dropEvent(self, event):
        self._drop_hover = False
        if self._valid_drop and self._valid_drop(event):
            group_id = bytes(event.mimeData().data(PHONEBOX_MIME)).decode("utf-8")
            if self._on_drop(group_id, self.role):
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            else:
                event.ignore()
        else:
            event.ignore()
        self.update()

    def set_selected(self, on):
        self.selected = on
        self.update()

    def set_count(self, n):
        self.count.setText(str(n))
        self.count.setStyleSheet(
            "color:%s;font-family:'%s';font-size:12px;background:transparent"
            % (TEXT if self.selected else DIM, FONT))

    def mousePressEvent(self, _e):
        self._on_click(self.role)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(8, 3, -8, -3)
        accent = QColor(ROLE_COLOR[self.role])
        if self._drop_hover:
            p.setBrush(QColor(SEL))
            pen = QPen(accent)
            pen.setWidthF(1.2)
            p.setPen(pen)
            p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), 9, 9)
        elif self.selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(SEL))
            p.drawRoundedRect(r, 9, 9)
            bar = QRectF(r.left() + 1, r.top() + 5, 3, r.height() - 10)
            p.setBrush(accent)
            p.drawRoundedRect(bar, 1.5, 1.5)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(HOVER))
            p.drawRoundedRect(r, 9, 9)
        p.end()


class PhoneboxTree(QTreeWidget):
    """Drags a saved group ID, so filtering cannot change its membership."""

    def mimeTypes(self):
        return [PHONEBOX_MIME]

    def mimeData(self, items):
        data = QMimeData()
        if items:
            group_id = items[0].data(0, Qt.ItemDataRole.UserRole)
            data.setData(PHONEBOX_MIME, str(group_id).encode("utf-8"))
        return data

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        # Route the saved group without Qt deleting its source row after Move.
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(items))
        drag.setPixmap(self.viewport().grab(self.visualItemRect(items[0])))
        drag.exec(Qt.DropAction.MoveAction)


class LaunchBlock(QWidget):
    """An engine card with live readiness, target and process controls."""
    def __init__(self, role, on_launch, on_stop, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.role = role
        self.setObjectName("launchcard")
        accent = ROLE_COLOR[role]
        self.setStyleSheet("#launchcard{background:rgba(16,25,37,235);border:1px solid %s;border-radius:12px}" % _tint(accent, .36))
        lo = QVBoxLayout(self)
        lo.setContentsMargins(16, 13, 16, 12)
        lo.setSpacing(8)
        top = QHBoxLayout()
        name = QLabel("FB" + ROLE_NAME[role])
        name.setStyleSheet("color:%s;font-size:16px;font-weight:700;background:transparent" % accent)
        top.addWidget(name)
        top.addStretch()
        self.count_lbl = QLabel("0 / 20 ready")
        self.count_lbl.setStyleSheet("color:%s;font-size:11px;background:transparent" % TEXT)
        top.addWidget(self.count_lbl)
        lo.addLayout(top)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("QProgressBar{background:#263143;border:none;border-radius:2px}QProgressBar::chunk{background:%s;border-radius:2px}" % accent)
        lo.addWidget(self.progress)
        row = QHBoxLayout()
        self.state_lbl = QLabel("Not running")
        self.state_lbl.setStyleSheet("color:%s;font-size:10px;background:transparent" % DIM)
        row.addWidget(self.state_lbl, 1)
        self.launch_btn = Btn("Launch " + ROLE_NAME[role], accent, "solid")
        self.launch_btn.clicked.connect(lambda: on_launch(self.role))
        row.addWidget(self.launch_btn)
        self.stop_btn = Btn("Stop")
        self.stop_btn.clicked.connect(lambda: on_stop(self.role))
        self.stop_btn.setEnabled(False)
        row.addWidget(self.stop_btn)
        lo.addLayout(row)

    def set_count(self, n):
        self.setToolTip("%d assigned devices · %s" % (n, boxes.APPS[self.role]["job"]))

    def set_capacity(self, ready, target, unavailable=0):
        self.count_lbl.setText("%d / %d ready" % (ready, target))
        self.progress.setRange(0, max(1, target))
        self.progress.setValue(min(ready, max(1, target)))
        detail = "%d ready of %d needed" % (ready, target)
        if unavailable:
            detail += " · %d assigned but unavailable" % unavailable
        if ready < target:
            detail += " · %d more needed" % (target - ready)
        elif ready > target:
            detail += " · %d above target" % (ready - target)
        self.count_lbl.setToolTip(detail)

    def set_running(self, running, pid=None):
        self.state_lbl.setText("Running · pid %s" % pid if running else "Not running")
        self.state_lbl.setStyleSheet("color:%s;font-size:10px;background:transparent" % (ROLE_COLOR[self.role] if running else DIM))
        self.launch_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)


class Hub(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FBVenoison · Fleet command")
        self.setWindowIcon(QIcon(app_icon()))
        self.resize(1280, 920)
        self.setMinimumSize(960, 720)
        self.cfg = boxes.load()
        self.devices = []
        self.assign = {}
        self.group = boxes.POISON
        self.phonebox = None
        self.phonebox_items = {}
        self.filter_text = ""
        self._note = None
        self.procs = {boxes.POISON: None, boxes.VENOM: None}
        self.launched_serials = {boxes.POISON: set(), boxes.VENOM: set()}
        self.setStyleSheet("QWidget{font-family:'%s';color:%s}QToolTip{background:#172436;color:#eef3fa;border:1px solid #354962;padding:8px}QScrollBar:vertical{background:transparent;width:8px}QScrollBar::handle:vertical{background:#34445b;border-radius:4px;min-height:24px}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}" % (FONT, TEXT))
        self.atmosphere = Atmosphere()
        self.setCentralWidget(self.atmosphere)
        outer = QVBoxLayout(self.atmosphere)
        outer.setContentsMargins(18, 0, 18, 0)
        outer.setSpacing(12)

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(5, 10, 5, 0)
        hl.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(app_icon(46))
        mark.setFixedSize(46, 46)
        hl.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        title = QLabel("FBVenoison")
        title.setStyleSheet("font-size:19px;font-weight:700;letter-spacing:.4px")
        titles.addWidget(title)
        subtitle = QLabel("FLEET COMMAND  /  v" + VERSION)
        subtitle.setStyleSheet("color:%s;font-size:8px;letter-spacing:1.6px" % DIM)
        titles.addWidget(subtitle)
        hl.addLayout(titles)
        hl.addStretch()
        self.motion_btn = Btn("Motion on", kind="quiet")
        self.motion_btn.setCheckable(True)
        self.motion_btn.setChecked(bool(self.cfg.get("motion", True)))
        self.motion_btn.clicked.connect(self.toggle_motion)
        self.atmosphere.set_motion(self.motion_btn.isChecked())
        self.motion_btn.setText("Motion on" if self.motion_btn.isChecked() else "Motion off")
        self.motion_btn.setToolTip("Pause or resume the animated atmosphere")
        hl.addWidget(self.motion_btn)
        self.update_btn = Btn("Updates ↗")
        self.update_btn.clicked.connect(self.show_updater)
        hl.addWidget(self.update_btn)
        outer.addWidget(head)
        outer.addWidget(FusionHero())

        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self.metrics = {}
        for key, title, color in (("connected", "DETECTED", TEXT), ("ready", "READY", GREEN), ("idle", "AWAITING APP", PURPLE), ("unavailable", "UNAVAILABLE", AMBER)):
            metric = FleetMetric(title, color)
            self.metrics[key] = metric
            metrics.addWidget(metric, 1)
        outer.addLayout(metrics)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(12)
        body.setStyleSheet("QSplitter::handle{background:transparent}")
        rail = QWidget()
        rail.setObjectName("rail")
        rail.setStyleSheet("#rail{background:rgba(12,20,31,235);border:1px solid #253346;border-radius:12px}")
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(6, 16, 6, 12)
        rl.setSpacing(6)
        apps_label = QLabel("  WORK SECTIONS")
        apps_label.setStyleSheet("color:%s;font-size:9px;font-weight:700;letter-spacing:1.6px;padding:0 9px 5px" % DIM)
        rl.addWidget(apps_label)
        self.group_rows = {}
        for role in GROUPS:
            row = GroupRow(role, self.select_group, self.move_phonebox, self.valid_phonebox_drop)
            self.group_rows[role] = row
            rl.addWidget(row)
            if role == boxes.UNASSIGNED:
                row.hide()
        rl.addSpacing(12)
        heading = QHBoxLayout()
        label = QLabel("  PHYSICAL BOXES")
        label.setStyleSheet("color:%s;font-size:9px;letter-spacing:1.4px;padding-left:9px" % DIM)
        heading.addWidget(label, 1)
        self.rename_btn = Btn("Rename", kind="quiet")
        self.rename_btn.setEnabled(False)
        self.rename_btn.clicked.connect(self.edit_phonebox_name)
        heading.addWidget(self.rename_btn)
        rl.addLayout(heading)
        self.phonebox_tree = PhoneboxTree()
        self.phonebox_tree.setColumnCount(3)
        self.phonebox_tree.setHeaderLabels(["NAME", "PHONES", "APP"])
        self.phonebox_tree.setRootIsDecorated(False)
        self.phonebox_tree.setMinimumHeight(95)
        self.phonebox_tree.setUniformRowHeights(True)
        self.phonebox_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.phonebox_tree.setDragEnabled(True)
        self.phonebox_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.phonebox_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.phonebox_tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.phonebox_tree.setStyleSheet("QTreeWidget{background:transparent;border:none;color:%s;font-size:10px;outline:none}QTreeWidget::item{height:34px;padding-left:5px}QTreeWidget::item:selected{background:%s;color:%s}QHeaderView::section{background:#101b29;color:%s;border:none;padding:7px 4px;font-size:8px}QLineEdit{background:%s;color:%s;border:1px solid %s}" % (TEXT, SEL, TEXT, DIM, BG, TEXT, DIM))
        bh = self.phonebox_tree.header()
        bh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        bh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        bh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.phonebox_tree.setColumnWidth(1, 50)
        self.phonebox_tree.setColumnWidth(2, 70)
        self.phonebox_tree.itemSelectionChanged.connect(self.on_phonebox_select)
        self.phonebox_tree.itemChanged.connect(self.on_phonebox_edit)
        rl.addWidget(self.phonebox_tree, 1)
        self.capture_hint = QLabel("Scan one physical box at a time.\nDrag a box onto a work section.")
        self.capture_hint.setWordWrap(True)
        self.capture_hint.setStyleSheet("color:%s;font-size:10px;padding:8px 12px" % DIM)
        rl.addWidget(self.capture_hint)
        self.pending_lbl = QLabel("")
        self.pending_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.pending_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.pending_lbl.setWordWrap(True)
        self.pending_lbl.setStyleSheet("color:%s;font-size:10px;padding:0 12px" % DIM)
        rl.addWidget(self.pending_lbl)
        self.realign_btn = Btn("Re-align this box", kind="quiet")
        self.realign_btn.setEnabled(False)
        self.realign_btn.setToolTip("Keep only this physical box powered on. Save a backup, then replace its captured membership with the current scan.")
        self.realign_btn.clicked.connect(lambda: self.realign_phonebox(self.phonebox))
        rl.addWidget(self.realign_btn)
        self.auto_btn = Btn("Split by physical box", kind="quiet")
        self.auto_btn.clicked.connect(self.do_auto_split)
        rl.addWidget(self.auto_btn)
        rail_scroll = QScrollArea()
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        rail_scroll.setStyleSheet("QScrollArea{background:transparent;border:none}")
        rail_scroll.setMinimumWidth(255)
        rail_scroll.setWidget(rail)
        body.addWidget(rail_scroll)

        right = QWidget()
        rr = QVBoxLayout(right)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(10)
        engines = QHBoxLayout()
        engines.setSpacing(10)
        self.panel = {}
        for role in (boxes.POISON, boxes.VENOM):
            block = LaunchBlock(role, self.launch, self.stop)
            self.panel[role] = block
            engines.addWidget(block, 1)
        rr.addLayout(engines)

        allocation = QFrame()
        allocation.setObjectName("allocation")
        allocation.setStyleSheet("#allocation{background:rgba(14,23,35,235);border:1px solid #253346;border-radius:11px}")
        al = QVBoxLayout(allocation)
        al.setContentsMargins(14, 10, 14, 10)
        al.setSpacing(6)
        controls = QHBoxLayout()
        cap = QLabel("DEVICE TARGETS")
        cap.setStyleSheet("font-size:9px;font-weight:700;letter-spacing:1px;color:%s" % DIM)
        controls.addWidget(cap)
        self.target_spins = {}
        targets = boxes.app_targets(self.cfg)
        for role in (boxes.POISON, boxes.VENOM):
            label = QLabel(ROLE_NAME[role])
            label.setStyleSheet("color:%s;font-size:10px" % ROLE_COLOR[role])
            controls.addWidget(label)
            spin = QSpinBox()
            spin.setRange(0, boxes.MAX_APP_TARGET)
            spin.setValue(targets[role])
            spin.setFixedWidth(69)
            spin.setFixedHeight(29)
            spin.setAccessibleName(ROLE_NAME[role] + " device target")
            spin.setStyleSheet("QSpinBox{background:#0c1420;color:#eef3fa;border:1px solid #34465d;border-radius:5px;padding:3px 4px;font-size:11px}QSpinBox::up-button,QSpinBox::down-button{width:15px}")
            spin.valueChanged.connect(self.targets_changed)
            self.target_spins[role] = spin
            controls.addWidget(spin)
        controls.addStretch()
        self.balance_btn = Btn("Rebalance", GREEN, "solid")
        self.balance_btn.setToolTip("Keep existing ready assignments where possible; move available devices to these targets. A short fleet is shared proportionally. Offline choices and physical boxes stay saved.")
        self.balance_btn.clicked.connect(self.rebalance_devices)
        controls.addWidget(self.balance_btn)
        self.both_btn = Btn("Launch both")
        self.both_btn.clicked.connect(self.launch_both)
        controls.addWidget(self.both_btn)
        al.addLayout(controls)
        self.allocation_hint = QLabel("")
        self.allocation_hint.setWordWrap(True)
        self.allocation_hint.setStyleSheet("color:%s;font-size:10px;background:transparent" % DIM)
        al.addWidget(self.allocation_hint)
        rr.addWidget(allocation)

        table_card = QFrame()
        table_card.setObjectName("devicecard")
        table_card.setStyleSheet("#devicecard{background:rgba(10,18,29,236);border:1px solid #253346;border-radius:12px}")
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(10, 7, 10, 8)
        tl.setSpacing(5)
        tools = QHBoxLayout()
        self.detail_title = QLabel("")
        self.detail_title.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.detail_title.setStyleSheet("font-size:13px;font-weight:700;padding:5px")
        tools.addWidget(self.detail_title, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search serial, model, hub…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(32)
        self.search.setMaximumWidth(240)
        self.search.setStyleSheet("QLineEdit{background:#0c1420;color:#edf3fa;border:1px solid #2c3c52;border-radius:7px;padding:0 9px;font-size:10px}QLineEdit:focus{border-color:#89a7ff}")
        self.search.textChanged.connect(self.on_filter)
        tools.addWidget(self.search)
        self.rescan_btn = Btn("Rescan · F5")
        self.rescan_btn.clicked.connect(self.rescan)
        tools.addWidget(self.rescan_btn)
        tl.addLayout(tools)
        self.table = QTreeWidget()
        self.table.setColumnCount(5)
        self.table.setHeaderLabels(["DEVICE SERIAL", "MODEL", "USB HUB", "STATUS", "SECTION"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSortingEnabled(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setStyleSheet("QTreeWidget{background:transparent;border:none;outline:none;font-size:11px;color:%s}QTreeWidget::item{height:37px;border:none;border-bottom:1px solid #1b2839;padding-left:9px}QTreeWidget::item:hover{background:#182a3b}QTreeWidget::item:selected{background:#233e55;color:#f4f8ff}QHeaderView::section{background:#111f2f;color:#91a3bb;border:none;padding:10px 9px;font-size:8px;font-weight:700;letter-spacing:1px}" % TEXT)
        hdr = self.table.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 132), (2, 74), (3, 94), (4, 76)):
            hdr.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self.on_select)
        tl.addWidget(self.table, 1)
        self.empty_label = QLabel("No devices in this section. Rescan or choose another work section.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color:%s;font-size:11px;padding:24px" % DIM)
        tl.addWidget(self.empty_label)
        tip = QLabel("Select devices, then move them below. Use Ctrl or Shift to select several.")
        tip.setStyleSheet("color:%s;font-size:9px;padding:4px" % DIM)
        tl.addWidget(tip)
        rr.addWidget(table_card, 1)
        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([276, 950])
        outer.addWidget(body, 1)

        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(4, 0, 4, 10)
        fl.setSpacing(8)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fl.addWidget(self.status, 1)
        self.move_btns = {}
        for role in GROUPS:
            button = Btn("Clear app" if role == boxes.UNASSIGNED else "→ " + ROLE_NAME[role], ROLE_COLOR[role])
            button.clicked.connect(lambda _c=False, r=role: self.move_selected(r))
            self.move_btns[role] = button
            fl.addWidget(button)
        outer.addWidget(foot)
        QShortcut(QKeySequence("F5"), self, self.rescan)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_children)
        self.timer.start(1200)
        self.select_group(boxes.POISON)
        self.rescan()

    def toggle_motion(self, enabled):
        self.atmosphere.set_motion(enabled)
        self.motion_btn.setText("Motion on" if enabled else "Motion off")
        self.cfg["motion"] = bool(enabled)
        boxes.save(self.cfg)

    def show_updater(self):
        from updater import open_updater
        open_updater(self)

    def targets_changed(self, *_args):
        updated = copy.deepcopy(self.cfg)
        updated["targets"] = {role: spin.value() for role, spin in self.target_spins.items()}
        try:
            boxes.save(updated, strict=True)
        except (OSError, ValueError, TypeError) as error:
            saved = boxes.app_targets(self.cfg)
            for role, spin in self.target_spins.items():
                was_blocked = spin.blockSignals(True)
                spin.setValue(saved[role])
                spin.blockSignals(was_blocked)
            self.say("Could not save device targets: %s" % error, RED)
            return
        self.cfg = updated
        self.refresh_counts()

    def rebalance_devices(self):
        if any(self.is_running(role) for role in (boxes.POISON, boxes.VENOM)):
            self.say("Stop both apps before rebalancing devices", AMBER)
            return False
        updated = copy.deepcopy(self.cfg)
        updated["assign"] = boxes.plan_allocation(self.devices, updated)
        changed = sum(self.assign.get(d["serial"], boxes.UNASSIGNED) != updated["assign"].get(d["serial"], boxes.UNASSIGNED) for d in self.devices)
        try:
            self.last_alignment_backup = backup_assignments(self.cfg)
            boxes.save(updated, strict=True)
        except (OSError, ValueError, TypeError) as error:
            self.say("Could not save rebalance: %s" % error, RED)
            return False
        self.cfg = updated
        self.sync_connected_assignments()
        self._note = ("Rebalanced · %d devices moved" % changed, GREEN)
        self.rebuild()
        return True

    # ── data ──
    def rescan(self):
        self.devices = boxes.list_devices_verbose()
        created = boxes.discover_groups(self.devices, self.cfg)
        self.sync_connected_assignments()
        boxes.save(self.cfg)
        if created:
            self._note = ("Created " + ", ".join(g["name"] for g in created), TEXT)
        # New boxes are visible immediately, before an app has been chosen.
        if self.phonebox is None and self.assign and all(r == boxes.UNASSIGNED for r in self.assign.values()):
            visible = set(self.assign)
            first = next((g for g in self.cfg.get("groups", []) if visible.intersection(g["serials"])), None)
            if first:
                self.phonebox = first["id"]
                for row in self.group_rows.values():
                    row.set_selected(False)
        self.rebuild()

    def sync_connected_assignments(self):
        saved = self.cfg.get("assign", {})
        self.assign = {d["serial"]: saved.get(d["serial"], boxes.UNASSIGNED)
                       for d in self.devices}

    def persist_assignments(self):
        # Keep the routes of unplugged phones for their next scan and launch.
        self.cfg.setdefault("assign", {}).update(self.assign)
        self.cfg["auto"] = False
        boxes.save(self.cfg)

    def on_filter(self, text):
        self.filter_text = (text or "").strip().lower()
        self.rebuild()

    def _visible(self, d):
        if not self.filter_text:
            return True
        hay = "%s %s %s" % (d["serial"], d.get("model") or "",
                            d.get("usb") or "")
        return self.filter_text in hay.lower()

    def shown_devices(self):
        """Phones in the selected group that also pass the filter."""
        phonebox = boxes.get_group(self.cfg, self.phonebox)
        members = set(phonebox["serials"]) if phonebox else None
        return [d for d in sorted(self.devices, key=lambda x: x["serial"])
                if (d["serial"] in members if members is not None else
                    self.assign.get(d["serial"], boxes.UNASSIGNED) == self.group)
                and self._visible(d)]

    def select_group(self, role):
        self.group = role
        self.phonebox = None
        if getattr(self, "phonebox_tree", None) is not None:
            self.phonebox_tree.blockSignals(True)
            self.phonebox_tree.clearSelection()
            self.phonebox_tree.setCurrentItem(None)
            self.phonebox_tree.blockSignals(False)
            self.rename_btn.setEnabled(False)
        for r, row in self.group_rows.items():
            row.set_selected(r == role)
        if getattr(self, "table", None) is not None:
            self.rebuild()

    def select_phonebox(self, group_id):
        if boxes.get_group(self.cfg, group_id) is None:
            return
        self.phonebox = group_id
        for row in self.group_rows.values():
            row.set_selected(False)
        self.rebuild()

    def on_phonebox_select(self):
        item = self.phonebox_tree.currentItem()
        if item and item.isSelected():
            self.select_phonebox(item.data(0, Qt.ItemDataRole.UserRole))

    def on_phonebox_edit(self, item, column):
        group_id = item.data(0, Qt.ItemDataRole.UserRole)
        if column == 0:
            self.rename_phonebox(group_id, item.text(0))
        else:
            self.rebuild_phoneboxes()

    def edit_phonebox_name(self):
        item = self.phonebox_items.get(self.phonebox)
        if item:
            self.phonebox_tree.editItem(item, 0)

    def rename_phonebox(self, group_id, name):
        if not boxes.rename_group(self.cfg, group_id, name):
            self._note = ("Enter a phonebox name (1–80 characters)", AMBER)
            self.rebuild_phoneboxes()
            self.refresh_counts()
            return False
        boxes.save(self.cfg)
        self.rebuild_phoneboxes()
        self.update_detail_title()
        return True

    def realign_phonebox(self, group_id):
        if any(self.is_running(role) for role in (boxes.POISON, boxes.VENOM)):
            self.say("Stop both apps before re-aligning a phonebox", AMBER)
            return False
        repaired = copy.deepcopy(self.cfg)
        scanned = boxes.list_devices_verbose()
        if not boxes.realign_group(scanned, repaired, group_id):
            self.say("Keep only this phonebox connected (1–20 phones) before re-aligning", AMBER)
            return False
        try:
            self.last_alignment_backup = backup_assignments(self.cfg)
        except OSError as error:
            self.say("Could not back up phoneboxes: %s" % error, RED)
            return False
        try:
            boxes.save(repaired, strict=True)
        except (OSError, ValueError, TypeError) as error:
            self.say("Could not save aligned phonebox: %s" % error, RED)
            return False
        self.cfg = repaired
        self.devices = scanned
        self.sync_connected_assignments()
        self.phonebox = group_id
        for row in self.group_rows.values():
            row.set_selected(False)
        phonebox = boxes.get_group(self.cfg, group_id)
        self._note = ("Aligned %s · %d/20 phones" % (phonebox["name"], len(phonebox["serials"])), TEXT)
        self.rebuild()
        return True

    def valid_phonebox_drop(self, event):
        if event.source() is not getattr(self, "phonebox_tree", None):
            return False
        data = event.mimeData()
        if not data.hasFormat(PHONEBOX_MIME):
            return False
        try:
            raw = bytes(data.data(PHONEBOX_MIME))
            return len(raw) <= 200 and boxes.get_group(self.cfg, raw.decode("utf-8")) is not None
        except UnicodeError:
            return False

    def routing_allowed(self, serials, role):
        changed = {s for s in serials
                   if self.cfg.get("assign", {}).get(s, boxes.UNASSIGNED) != role}
        if not changed:
            return True
        affected = {role} | {self.cfg.get("assign", {}).get(s, boxes.UNASSIGNED)
                             for s in changed}
        for app_role in (boxes.POISON, boxes.VENOM):
            if self.is_running(app_role) and (app_role in affected or
                    changed & self.launched_serials[app_role]):
                self._note = ("Stop %s before moving these phones" % boxes.APPS[app_role]["label"], AMBER)
                self.refresh_counts()
                return False
        return True

    def move_phonebox(self, group_id, role):
        phonebox = boxes.get_group(self.cfg, group_id)
        if phonebox is None or role not in GROUPS:
            return False
        if not self.routing_allowed(phonebox["serials"], role):
            return False
        boxes.assign_group(self.cfg, group_id, role)
        self.cfg["auto"] = False
        boxes.save(self.cfg)
        self.sync_connected_assignments()
        self._note = ("%s · %d phones → %s" %
                      (phonebox["name"], len(phonebox["serials"]), ROLE_NAME[role]), ROLE_COLOR[role])
        self.rebuild()
        return True

    def phonebox_role(self, phonebox):
        roles = {self.cfg.get("assign", {}).get(s, boxes.UNASSIGNED)
                 for s in phonebox["serials"]}
        return next(iter(roles)) if len(roles) == 1 else None

    def rebuild_phoneboxes(self):
        connected = {d["serial"] for d in self.devices}
        self.phonebox_tree.blockSignals(True)
        saved_ids = {g["id"] for g in self.cfg.get("groups", [])}
        for group_id in list(self.phonebox_items):
            if group_id not in saved_ids:
                item = self.phonebox_items.pop(group_id)
                self.phonebox_tree.takeTopLevelItem(self.phonebox_tree.indexOfTopLevelItem(item))
        for phonebox in self.cfg.get("groups", []):
            role = self.phonebox_role(phonebox)
            count = len(connected.intersection(phonebox["serials"]))
            item = self.phonebox_items.get(phonebox["id"])
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, phonebox["id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled)
                self.phonebox_tree.addTopLevelItem(item)
                self.phonebox_items[phonebox["id"]] = item
            values = [phonebox["name"], "%d/%d" % (count, boxes.PHONEBOX_SIZE),
                      "Choose app" if role == boxes.UNASSIGNED else (ROLE_NAME[role] if role is not None else "Mixed")]
            for column, value in enumerate(values):
                if item.text(column) != value:
                    item.setText(column, value)
            item.setForeground(2, QColor(ROLE_COLOR.get(role, AMBER)))
            tip = "%s · %d/%d phones captured · %d connected · %s\nDrag onto an app above. Double-click the name to rename." % (
                phonebox["name"], len(phonebox["serials"]), boxes.PHONEBOX_SIZE, count, item.text(2))
            for column in range(3):
                item.setToolTip(column, tip)
            if phonebox["id"] == self.phonebox:
                if self.phonebox_tree.currentItem() is not item:
                    self.phonebox_tree.setCurrentItem(item)
                if not item.isSelected():
                    item.setSelected(True)
        self.phonebox_tree.blockSignals(False)
        self.rename_btn.setEnabled(self.phonebox in self.phonebox_items)
        self.realign_btn.setEnabled(self.phonebox in self.phonebox_items)
        capturing = [g for g in self.cfg.get("groups", []) if g.get("capturing", False)]
        self.pending_lbl.setText(" · ".join("%s: %d/20 captured" % (g["name"], len(g["serials"])) for g in capturing)
                                 if capturing else ("No phoneboxes scanned yet" if not self.phonebox_items else ""))
        self.pending_lbl.setToolTip(self.pending_lbl.text())

    def update_detail_title(self):
        phonebox = boxes.get_group(self.cfg, self.phonebox)
        self.detail_title.setText(phonebox["name"] if phonebox else
                                 self.group_rows[self.group].name.text())
        self.detail_title.setToolTip(self.detail_title.text())

    def rebuild(self):
        if getattr(self, "table", None) is None:
            return
        self.rebuild_phoneboxes()
        self.update_detail_title()
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.rows = {}
        for d in self.shown_devices():
            state = d.get("state", "device")
            item = QTreeWidgetItem([
                d["serial"],
                d.get("model") or "unknown",
                boxes.hub_of(d.get("usb", "")) or "—",
                STATE_TEXT.get(state, state),
                ROLE_NAME.get(self.assign.get(d["serial"], boxes.UNASSIGNED), "No app"),
            ])
            item.setFont(0, QFont(MONO, 9))
            item.setForeground(1, QColor(DIM))
            item.setForeground(2, QColor(FAINT))
            item.setForeground(3, QColor(STATE_COLOR.get(state, DIM)))
            item.setForeground(4, QColor(ROLE_COLOR.get(self.assign.get(d["serial"], boxes.UNASSIGNED), DIM)))
            item.setData(0, Qt.ItemDataRole.UserRole, d["serial"])
            self.table.addTopLevelItem(item)
            self.rows[d["serial"]] = item
        self.table.setSortingEnabled(True)
        self.empty_label.setVisible(not self.rows)
        self.refresh_counts()

    def selected_serials(self):
        return [i.data(0, Qt.ItemDataRole.UserRole)
                for i in self.table.selectedItems()]

    def on_select(self):
        self.refresh_counts()

    # ── assignment ──
    def set_role(self, serial, role):
        if role not in GROUPS or not self.routing_allowed([serial], role):
            return
        self.assign[serial] = role
        self.persist_assignments()
        self.rebuild()

    def move_selected(self, role):
        picked = self.selected_serials()
        if not picked and self.phonebox:
            return self.move_phonebox(self.phonebox, role)
        if not picked:
            self._note = ("select phones first", AMBER)
            self.refresh_counts()
            return
        if role == self.group and not self.phonebox:
            self._note = ("they are already in %s" % ROLE_NAME[role], AMBER)
            self.refresh_counts()
            return
        if not self.routing_allowed(picked, role):
            return
        for s in picked:
            self.assign[s] = role
        self.persist_assignments()
        self._note = ("%d phone%s → %s" % (len(picked),
                                           "" if len(picked) == 1 else "s",
                                           ROLE_NAME[role]), ROLE_COLOR[role])
        self.rebuild()

    def assign_shown(self, role):
        """Everything the current group+filter is showing."""
        shown = [d["serial"] for d in self.shown_devices()]
        if not shown:
            self._note = ("nothing to assign - no phones shown", AMBER)
            self.refresh_counts()
            return
        if not self.routing_allowed(shown, role):
            return
        for s in shown:
            self.assign[s] = role
        self.persist_assignments()
        self._note = ("%d phone%s → %s" % (len(shown),
                                           "" if len(shown) == 1 else "s",
                                           ROLE_NAME[role]), ROLE_COLOR[role])
        self.rebuild()

    def do_auto_split(self):
        if any(self.is_running(role) for role in (boxes.POISON, boxes.VENOM)):
            self.say("Stop both apps before auto-splitting phones", AMBER)
            return
        self.cfg["auto"] = True
        self.assign = boxes.auto_split(self.devices)
        self.cfg.setdefault("assign", {}).update(self.assign)
        # A completed phonebox is routed as a whole, even with only one box.
        for index, phonebox in enumerate(self.cfg.get("groups", [])):
            boxes.assign_group(self.cfg, phonebox["id"], (boxes.POISON, boxes.VENOM)[index % 2])
        self.sync_connected_assignments()
        boxes.save(self.cfg)
        self._note = ("Phoneboxes assigned to Poison / Venom", DIM)
        self.rebuild()

    def refresh_counts(self):
        n = {boxes.POISON: 0, boxes.VENOM: 0, boxes.UNASSIGNED: 0}
        bad = 0
        state_by = {d["serial"]: d.get("state", "device") for d in self.devices}
        for s, r in self.assign.items():
            n[r] = n.get(r, 0) + 1
            if r != boxes.UNASSIGNED and state_by.get(s, "device") != "device":
                bad += 1
        for role in GROUPS:
            self.group_rows[role].set_count(n[role])
        for role in (boxes.POISON, boxes.VENOM):
            self.panel[role].set_count(n[role])

        targets = boxes.app_targets(self.cfg)
        capacity = boxes.allocation_status(self.devices, self.assign, targets)
        self.metrics["connected"].value.setText(str(len(self.devices)))
        for key in ("ready", "idle", "unavailable"):
            self.metrics[key].value.setText(str(capacity[key]))
        hints = []
        for role in (boxes.POISON, boxes.VENOM):
            info = capacity[role]
            self.panel[role].set_capacity(info["ready"], info["target"], n[role] - info["ready"])
            if info["shortage"]:
                hints.append("%s needs %d more" % (ROLE_NAME[role], info["shortage"]))
            elif info["excess"]:
                hints.append("%s has %d extra" % (ROLE_NAME[role], info["excess"]))
        self.allocation_hint.setText(" · ".join(hints) + (" · Rebalance to apply targets." if hints else "Targets met. Move any selected device between sections below."))
        self.balance_btn.setEnabled(not any(self.is_running(role) for role in (boxes.POISON, boxes.VENOM)))

        picked = len(self.selected_serials())
        phonebox = boxes.get_group(self.cfg, self.phonebox)
        for role, btn in self.move_btns.items():
            if phonebox and not picked:
                btn.setText("Clear app" if role == boxes.UNASSIGNED else "Box → %s" % ROLE_NAME[role])
                btn.setEnabled(self.phonebox_role(phonebox) != role)
            else:
                btn.setText("Clear app" if role == boxes.UNASSIGNED else "→ %s" % ROLE_NAME[role])
                btn.setEnabled(bool(picked) and (self.phonebox is not None or role != self.group))

        if picked:
            msg = "%d selected" % picked
            col = TEXT
        else:
            hubs = sorted({boxes.hub_of(d.get("usb", ""))
                           for d in self.shown_devices()} - {""})
            bits = []
            if hubs:
                bits.append("hub " + ", ".join(hubs[:3]))
            bits.append("%d phone%s" % (len(self.rows),
                                        "" if len(self.rows) == 1 else "s"))
            msg = " · ".join(bits)
            col = DIM
        # Standing warnings always follow, and a one-off note goes in FRONT of
        # them - never over them, or "3 phones are offline" would vanish the
        # moment a button is pressed.
        warn = []
        if n[boxes.UNASSIGNED]:
            warn.append("%d awaiting app selection" % n[boxes.UNASSIGNED])
        if bad:
            warn.append("%d not ready" % bad)
        if warn:
            msg += "   ·   " + " · ".join(warn)
            col = AMBER
        if self._note:
            note, note_col = self._note
            msg = "%s   ·   %s" % (note, msg)
            if col == DIM:
                col = note_col
            self._note = None
        self.say(msg, col)

    def say(self, text, col=DIM):
        self.status.setText(text)
        self.status.setToolTip(text)
        self.status.setStyleSheet(
            "color:%s;font-family:'%s';font-size:11px;background:transparent"
            % (col, FONT))

    # ── children ──
    def serials_for(self, role):
        return sorted(s for s, r in self.assign.items() if r == role)

    def is_running(self, role):
        p = self.procs.get(role)
        return bool(p and p.poll() is None)

    def launch(self, role):
        meta = boxes.APPS[role]
        if self.is_running(role):
            self.say("%s is already open in its own window" % meta["label"],
                     AMBER)
            return
        serials = self.serials_for(role)
        if not serials:
            self.say("no phones assigned to %s - assign some first"
                     % meta["label"], RED)
            return
        # Include saved unplugged members so a reconnect reaches this same app.
        serials = sorted(s for s, r in self.cfg.get("assign", {}).items() if r == role)
        env = dict(os.environ)
        env["FBVENOISON_DEVICES"] = ",".join(serials)
        env["FBVENOISON_LABEL"] = meta["label"]
        env["FBVENOISON_AUTHED"] = "1"
        try:
            if getattr(sys, "frozen", False):
                # Frozen hub: sys.executable is the hub exe and bootstrap.py is
                # bundled, not on disk - so launch the app's own .exe directly.
                # It reads FBVENOISON_DEVICES to see only its box and
                # FBVENOISON_AUTHED to skip its own key gate.
                exe = child_exe(role)
                if not exe:
                    self.say("cannot find the %s app (.exe) - set %s to its path"
                             % (meta["label"],
                                "FBVENOISON_%s_EXE" % ROLE_NAME[role].upper()), RED)
                    return
                if role == boxes.VENOM and not child_bridge_ready(exe):
                    self.say("Rebuild FBVenom with build_windows.ps1 to enable its assigned-device bridge", AMBER)
                    return
                proc = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env)
            else:
                entry = os.path.join(meta["dir"], meta["entry"])
                if not os.path.isfile(entry):
                    self.say("cannot find %s" % entry, RED)
                    return
                proc = subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "bootstrap.py"),
                     meta["dir"], meta["entry"]],
                    cwd=meta["dir"], env=env)
        except Exception as e:
            self.say("could not start %s: %s" % (meta["label"], e), RED)
            return
        self.procs[role] = proc
        self.launched_serials[role] = set(serials)
        self.panel[role].set_running(True, proc.pid)
        self.say("%s opened in its own window with %d phone%s"
                 % (meta["label"], len(serials),
                    "" if len(serials) == 1 else "s"), ROLE_COLOR[role])

    def launch_both(self):
        started = []
        for role in (boxes.POISON, boxes.VENOM):
            if not self.is_running(role) and self.serials_for(role):
                self.launch(role)
                started.append(boxes.APPS[role]["label"])
        if len(started) == 2:
            self.say("Both windows open · %s" % " + ".join(started), TEXT)
        elif not started:
            self.say("nothing to launch - assign phones to a box first", AMBER)

    def stop(self, role):
        if not self.is_running(role):
            return
        self.procs[role].terminate()
        self.say("%s asked to close" % boxes.APPS[role]["label"], AMBER)

    def poll_children(self):
        self.balance_btn.setEnabled(not any(self.is_running(role) for role in (boxes.POISON, boxes.VENOM)))
        for role in (boxes.POISON, boxes.VENOM):
            running = self.is_running(role)
            self.panel[role].set_running(
                running, self.procs[role].pid if running else None)
            if running:
                added = set(self.serials_for(role)) - self.launched_serials[role]
                if added:
                    self.panel[role].state_lbl.setText("Running · pid %s\nRestart for %d newly scanned phones" %
                                                      (self.procs[role].pid, len(added)))

    def closeEvent(self, ev):
        dialog = getattr(self, "_suite_update_dialog", None)
        worker = getattr(dialog, "worker", None)
        if worker is not None and worker.isRunning():
            self.say("Wait for the update operation to finish before closing", AMBER)
            dialog.show()
            dialog.raise_()
            ev.ignore()
            return
        self.atmosphere.timer.stop()
        # Do not leave orphan windows behind when the hub closes.
        for proc in self.procs.values():
            if proc and proc.poll() is None:
                proc.terminate()
        ev.accept()
