# -*- coding: utf-8 -*-
"""Exercise scan-created phoneboxes through real hub widgets and Qt drops.

ADB reads a temporary imaginary fleet and configuration is also temporary.
No app processes or real phones are used.
"""
import json
import os
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ADB_PATH"] = os.path.join(HERE, "fake_adb.bat" if os.name == "nt" else "fake_adb.py")

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit
import fake_fleet
import boxes

_tmp = tempfile.mkdtemp(prefix="venoison_phonebox_ui_")
boxes.CONFIG_PATH = os.path.join(_tmp, "boxes.json")
import hub

app = QApplication.instance() or QApplication([])
fails = []
windows = []


def check(name, condition, detail=""):
    print("   %-57s %s" % (name, "ok" if condition else "FAIL " + detail))
    if not condition:
        fails.append("%s %s" % (name, detail))


def serve(devices):
    path = os.path.join(_tmp, "fleet.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(devices, fh)
    os.environ["FAKE_ADB_FLEET"] = path


def open_hub():
    window = hub.Hub()
    window.timer.stop()
    window.show()
    app.processEvents()
    windows.append(window)
    return window


def group_for(window, serials):
    expected = set(serials)
    return next(g for g in window.cfg["groups"] if set(g["serials"]) == expected)


def group_item(window, group_id):
    tree = window.phonebox_tree
    return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == group_id)


def saved_roles(window, serials):
    return {window.cfg["assign"].get(s, boxes.UNASSIGNED) for s in serials}


def disk_cfg():
    with open(boxes.CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


class DragEnter(QDragEnterEvent):
    """Synthetic Qt drag with the same source as the real tree's QDrag.

    Qt supplies source() only during a native drag loop. Overriding that
    accessor allows offscreen tests to send actual drag/drop events without
    starting a platform drag loop that waits for mouse input.
    """
    def __init__(self, source, mime):
        super().__init__(QPoint(8, 8), Qt.DropAction.MoveAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self._source = source

    def source(self):
        return self._source


class Drop(QDropEvent):
    def __init__(self, source, mime):
        super().__init__(QPointF(8, 8), Qt.DropAction.MoveAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self._source = source

    def source(self):
        return self._source


def drop_group(window, group_id, role, source=None, mime_type=None):
    mime = QMimeData()
    mime.setData(mime_type or hub.PHONEBOX_MIME, group_id.encode("utf-8"))
    source = window.phonebox_tree if source is None else source
    target = window.group_rows[role]
    enter = DragEnter(source, mime)
    QApplication.sendEvent(target, enter)
    drop = Drop(source, mime)
    QApplication.sendEvent(target, drop)
    app.processEvents()
    return enter.isAccepted(), drop.isAccepted()


class RunningProcess:
    pid = 12345

    def poll(self):
        return None

    def terminate(self):
        pass


class DragProbeTree(hub.PhoneboxTree):
    """Record the native view's drag threshold without a blocking drag loop."""
    def __init__(self):
        super().__init__()
        self.drag_calls = 0
        self.drag_payload = None

    def startDrag(self, supported_actions):
        self.drag_calls += 1
        self.drag_payload = bytes(self.mimeData(self.selectedItems()).data(hub.PHONEBOX_MIME))


print("1) the first box creates one stable group, the next scan adds another")
fleet = fake_fleet.two_boxes()
first, second = fleet[:20], fleet[20:]
first_serials = [d["serial"] for d in first]
second_serials = [d["serial"] for d in second]
serve(first)
w = open_hub()
check("first 20 phones form exactly one group", len(w.cfg["groups"]) == 1)
g1 = group_for(w, first_serials)
g1_id = g1["id"]
check("new group waits for the user to choose an app",
      saved_roles(w, first_serials) == {boxes.UNASSIGNED})
check("app choices do not include a standalone unassigned list",
      not w.group_rows[boxes.UNASSIGNED].isVisible()
      and w.move_btns[boxes.UNASSIGNED].text() == "Clear app")
check("group widget displays all 20 connected members",
      group_item(w, g1_id).text(1).replace(" ", "") == "20/20")
check("whole first group moves to Poison", w.move_phonebox(g1_id, boxes.POISON))
serve(fleet)
w.rescan()
check("second scan creates exactly one additional group", len(w.cfg["groups"]) == 2)
g2 = group_for(w, second_serials)
g2_id = g2["id"]
check("first group retains its identity and members",
      group_for(w, first_serials)["id"] == g1_id)
check("first assignment is preserved while second is unassigned",
      saved_roles(w, first_serials) == {boxes.POISON}
      and saved_roles(w, second_serials) == {boxes.UNASSIGNED})
snapshot = [(g["id"], sorted(g["serials"])) for g in w.cfg["groups"]]
w.rescan()
check("repeat scan never duplicates or merges the two groups",
      [(g["id"], sorted(g["serials"])) for g in w.cfg["groups"]] == snapshot)
check("both saved groups have a matching tree item",
      w.phonebox_tree.topLevelItemCount() == 2
      and {group_item(w, g1_id).data(0, Qt.ItemDataRole.UserRole),
           group_item(w, g2_id).data(0, Qt.ItemDataRole.UserRole)} == {g1_id, g2_id})

print("\n2) rename through the editable tree, validate, then reopen")
item = group_item(w, g1_id)
check("phonebox names are editable", bool(item.flags() & Qt.ItemFlag.ItemIsEditable))
item.setText(0, "  Farmbox North  ")
app.processEvents()
check("editing trims and saves the new name",
      group_for(w, first_serials)["name"] == "Farmbox North"
      and group_item(w, g1_id).text(0) == "Farmbox North")
group_item(w, g1_id).setText(0, "   ")
app.processEvents()
check("blank edit restores the last valid name",
      group_item(w, g1_id).text(0) == "Farmbox North"
      and group_for(w, first_serials)["name"] == "Farmbox North")
check("unknown group cannot be renamed", not w.rename_phonebox("missing", "Other"))
check("name is persisted on disk",
      next(g["name"] for g in disk_cfg()["groups"] if g["id"] == g1_id)
      == "Farmbox North")
w.close()
w = open_hub()
check("reopen restores name, membership and app routing",
      group_for(w, first_serials)["id"] == g1_id
      and group_for(w, first_serials)["name"] == "Farmbox North"
      and saved_roles(w, first_serials) == {boxes.POISON})

print("\n3) actual Qt drops move complete groups to either app")
enter, accepted = drop_group(w, g2_id, boxes.VENOM)
check("Venom accepts the drag and drop", enter and accepted)
check("all second-box devices arrive in Venom",
      saved_roles(w, second_serials) == {boxes.VENOM}
      and group_item(w, g2_id).text(2) == "Venom")
enter, accepted = drop_group(w, g1_id, boxes.VENOM)
check("Poison to Venom drop moves all first-box devices",
      enter and accepted and saved_roles(w, first_serials) == {boxes.VENOM})
enter, accepted = drop_group(w, g1_id, boxes.POISON)
check("Venom to Poison drop moves all first-box devices",
      enter and accepted and saved_roles(w, first_serials) == {boxes.POISON})
check("clearing an app leaves the physical group ready for a choice",
      w.move_phonebox(g2_id, boxes.UNASSIGNED)
      and saved_roles(w, second_serials) == {boxes.UNASSIGNED}
      and group_item(w, g2_id).text(2) == "Choose app")
before = dict(w.cfg["assign"])
enter, accepted = drop_group(w, g1_id, boxes.VENOM, source=w.table)
check("a foreign widget cannot inject a group drop",
      not enter and not accepted and w.cfg["assign"] == before)
enter, accepted = drop_group(w, g1_id, boxes.VENOM, mime_type="text/plain")
check("non-phonebox MIME is refused",
      not enter and not accepted and w.cfg["assign"] == before)
enter, accepted = drop_group(w, "missing", boxes.VENOM)
check("unknown group drops cannot change assignments",
      not accepted and w.cfg["assign"] == before)
check("invalid destination cannot change assignments",
      not w.move_phonebox(g1_id, "missing") and w.cfg["assign"] == before)

print("\n4) group moves are independent of the displayed app and text filter")
w.select_group(boxes.POISON)
w.search.setText(first_serials[0])
check("search currently shows one first-box phone", set(w.rows) == {first_serials[0]})
enter, accepted = drop_group(w, g2_id, boxes.VENOM)
check("hidden second box still moves every member",
      enter and accepted and saved_roles(w, second_serials) == {boxes.VENOM})
check("unrelated first box keeps its routing",
      saved_roles(w, first_serials) == {boxes.POISON})
w.search.setText("")
w.select_phonebox(g2_id)
check("selecting a phonebox shows its members across app filters",
      set(w.rows) == set(second_serials))

print("\n5) disconnected devices keep membership and receive whole-group moves")
gone = first_serials[:3]
serve([d for d in fleet if d["serial"] not in gone])
w.rescan()
check("unplugged members remain in their saved physical group",
      set(group_for(w, first_serials)["serials"]) == set(first_serials)
      and group_item(w, g1_id).text(1).replace(" ", "") == "17/20")
check("rescan preserves disconnected devices' saved app",
      all(w.cfg["assign"].get(s) == boxes.POISON for s in gone))
w.select_phonebox(g1_id)
w.search.setText(first_serials[3])
enter, accepted = drop_group(w, g1_id, boxes.VENOM)
check("drop also saves the new app for disconnected members",
      enter and accepted and saved_roles(w, first_serials) == {boxes.VENOM})
check("disconnected routing is saved on disk",
      all(disk_cfg()["assign"].get(s) == boxes.VENOM for s in gone))
serve(fleet)
w.rescan()
w.search.setText("")
w.select_phonebox(g1_id)
check("reconnected phones return to same group and chosen app",
      len(w.cfg["groups"]) == 2 and set(w.rows) == set(first_serials)
      and all(w.assign.get(s) == boxes.VENOM for s in gone)
      and group_item(w, g1_id).text(1).replace(" ", "") == "20/20")

print("\n6) moving a group is refused while an affected app is running")
before = dict(w.cfg["assign"])
w.procs[boxes.VENOM] = RunningProcess()
check("running source app blocks whole-group moves",
      not w.move_phonebox(g1_id, boxes.POISON) and w.cfg["assign"] == before)
check("running source also blocks clearing the group's app",
      not w.move_phonebox(g1_id, boxes.UNASSIGNED) and w.cfg["assign"] == before)
w.procs[boxes.VENOM] = None
w.procs[boxes.POISON] = RunningProcess()
check("running destination app blocks whole-group moves",
      not w.move_phonebox(g1_id, boxes.POISON) and w.cfg["assign"] == before)
w.procs[boxes.POISON] = RunningProcess()
check("an unrelated running app permits unaffected moves",
      w.move_phonebox(g2_id, boxes.UNASSIGNED)
      and saved_roles(w, second_serials) == {boxes.UNASSIGNED})
check("Venom to Unassigned can proceed while Poison runs",
      w.move_phonebox(g1_id, boxes.UNASSIGNED)
      and saved_roles(w, first_serials) == {boxes.UNASSIGNED})
w.procs[boxes.POISON] = None

print("\n7) per-phone moves can mix roles; a group drop reunifies them")
w.move_phonebox(g1_id, boxes.POISON)
w.select_phonebox(g1_id)
one = first_serials[0]
w.table.clearSelection()
w.rows[one].setSelected(True)
w.move_selected(boxes.VENOM)
check("per-phone assignment marks the physical group Mixed",
      w.assign[one] == boxes.VENOM
      and saved_roles(w, first_serials) == {boxes.POISON, boxes.VENOM}
      and group_item(w, g1_id).text(2) == "Mixed")
w.select_phonebox(g1_id)
check("mixed group selection still shows every member", set(w.rows) == set(first_serials))
enter, accepted = drop_group(w, g1_id, boxes.VENOM)
check("dropping a mixed group restores one uniform app",
      enter and accepted and saved_roles(w, first_serials) == {boxes.VENOM}
      and group_item(w, g1_id).text(2) == "Venom")
check("second physical box is not changed by that move",
      saved_roles(w, second_serials) == {boxes.UNASSIGNED})

print("\n8) the first mouse drag and first double-click work on an unselected box")
original_tree = hub.PhoneboxTree
hub.PhoneboxTree = DragProbeTree
try:
    native = open_hub()
finally:
    hub.PhoneboxTree = original_tree
tree = native.phonebox_tree
viewport = tree.viewport()
native.select_group(boxes.POISON)
item_before = group_item(native, g1_id)
tree.scrollToItem(item_before)
app.processEvents()
rect = tree.visualItemRect(item_before)
press_pos = QPoint(rect.left() + 15, rect.center().y())
drag_pos = press_pos + QPoint(0, QApplication.startDragDistance() + 10)
QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, press_pos)
move_event = QMouseEvent(QEvent.Type.MouseMove, QPointF(drag_pos),
                        QPointF(viewport.mapToGlobal(drag_pos)), Qt.MouseButton.NoButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(viewport, move_event)
QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, drag_pos)
app.processEvents()
check("the first press-drag starts the native drag",
      tree.drag_calls == 1 and tree.drag_payload == g1_id.encode("utf-8"))
check("selecting by mouse preserves the source item object",
      group_item(native, g1_id) is item_before and native.phonebox == g1_id)
native.select_group(boxes.POISON)
rect = tree.visualItemRect(item_before)
name_pos = QPoint(rect.left() + 15, rect.center().y())
QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, name_pos)
QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, name_pos)
app.processEvents()
editors = [editor for editor in tree.findChildren(QLineEdit) if editor.isVisible()]
check("first click and double-click opens the name editor", len(editors) == 1)
if editors:
    editor = editors[0]
    editor.selectAll()
    QTest.keyClicks(editor, "Native Farm")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    app.processEvents()
check("Return commits the edited name and preserves its item",
      group_for(native, first_serials)["name"] == "Native Farm"
      and group_item(native, g1_id) is item_before)
native.rescan()
check("scan metadata refresh preserves the existing item",
      group_item(native, g1_id) is item_before)
enter, accepted = drop_group(native, g1_id, boxes.POISON)
check("routing drop preserves the dragged source item",
      enter and accepted and group_item(native, g1_id) is item_before)

print("\n9) long literal names do not force the window wider")
native.select_phonebox(g1_id)
app.processEvents()
minimum_before = native.minimumSizeHint().width()
width_before = native.width()
long_name = "<b>" + "W" * 73 + "</b>"
check("an 80-character name is accepted", native.rename_phonebox(g1_id, long_name))
enter, accepted = drop_group(native, g1_id, boxes.VENOM)
app.processEvents()
check("long name and move message keep the original minimum width",
      enter and accepted and native.minimumSizeHint().width() == minimum_before,
      "%d -> %d" % (minimum_before, native.minimumSizeHint().width()))
check("the window remains at its requested width", native.width() == width_before,
      str(native.width()))
check("markup-like names stay literal in the title and move message",
      native.detail_title.text() == long_name and long_name in native.status.text()
      and native.detail_title.textFormat() == Qt.TextFormat.PlainText
      and native.status.textFormat() == Qt.TextFormat.PlainText)

print("\n10) nine scanned phones immediately appear as a named physical box")
boxes.CONFIG_PATH = os.path.join(_tmp, "partial_boxes.json")
no_usb_first = [dict(device, usb="") for device in first]
no_usb_second = [dict(device, usb="") for device in second]
nine_serials = first_serials[:9]
serve(no_usb_first[:9])
partial = open_hub()
check("first nine phones create one named group immediately",
      len(partial.cfg["groups"]) == 1
      and partial.cfg["groups"][0]["name"] == "Phonebox 1")
partial_id = group_for(partial, nine_serials)["id"]
check("the partial group displays nine of its twenty-phone capacity",
      group_item(partial, partial_id).text(1).replace(" ", "") == "9/20"
      and group_item(partial, partial_id).text(2) == "Choose app")
check("first scan selects the new box and shows all nine phones",
      partial.phonebox == partial_id and set(partial.rows) == set(nine_serials)
      and not partial.group_rows[boxes.UNASSIGNED].isVisible())
check("partial group can be routed as a whole",
      partial.move_phonebox(partial_id, boxes.POISON))
serve(no_usb_first)
partial.rescan()
check("the next eleven complete the existing group without changing identity",
      len(partial.cfg["groups"]) == 1
      and group_for(partial, first_serials)["id"] == partial_id
      and group_item(partial, partial_id).text(1).replace(" ", "") == "20/20")
check("later members inherit the partial group's chosen app",
      saved_roles(partial, first_serials) == {boxes.POISON})
serve(no_usb_first[:9] + no_usb_second)
partial.rescan()
check("twenty newly scanned phones create a separate second box",
      len(partial.cfg["groups"]) == 2
      and group_for(partial, first_serials)["id"] == partial_id
      and len(group_for(partial, second_serials)["serials"]) == 20
      and saved_roles(partial, second_serials) == {boxes.UNASSIGNED})
check("a completed first box keeps its offline members during ordinary scans",
      group_item(partial, partial_id).text(1).replace(" ", "") == "9/20")

print("\n11) a new full second box does not fill the incomplete first box")
boxes.CONFIG_PATH = os.path.join(_tmp, "separate_partial_boxes.json")
serve(no_usb_first[:9])
separate = open_hub()
separate_first_id = group_for(separate, nine_serials)["id"]
serve(no_usb_first[:9] + no_usb_second)
separate.rescan()
check("nine first-box members remain together while the second twenty arrive",
      len(separate.cfg["groups"]) == 2
      and group_for(separate, nine_serials)["id"] == separate_first_id
      and len(group_for(separate, second_serials)["serials"]) == 20
      and group_item(separate, separate_first_id).text(1).replace(" ", "") == "9/20")

print("\n12) re-align the screenshot's stale twenty-member box to its nine phones")
boxes.CONFIG_PATH = os.path.join(_tmp, "stale_boxes.json")
new_five = [fake_fleet.phone("CURRENT_NEW_%02d" % i, None) for i in range(5)]
new_five_serials = [device["serial"] for device in new_five]
stale_pending = ["PENDING_OFFLINE_%02d" % i for i in range(11)]
current_nine = no_usb_first[:4] + new_five
current_nine_serials = [device["serial"] for device in current_nine]
legacy_routes = {serial: boxes.POISON for serial in first_serials}
seed = {"auto": False, "assign": legacy_routes,
        "groups": [{"id": "box-1", "name": "My first farmbox",
                    "serials": first_serials, "capturing": False}],
        "pending": {"": new_five_serials + stale_pending},
        "pending_order": new_five_serials + stale_pending,
        "user_note": "preserve this configuration field"}
with open(boxes.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump(seed, fh)
serve(current_nine)
stale = open_hub()
stale.select_phonebox("box-1")
check("old saved membership reproduces four visible of twenty",
      group_item(stale, "box-1").text(1).replace(" ", "") == "4/20"
      and len(stale.rows) == 4)
check("automatic discovery gives the other five a provisional group",
      len(stale.cfg["groups"]) == 2
      and len(group_for(stale, new_five_serials)["serials"]) == 5)
before_alignment = json.loads(json.dumps(stale.cfg))
stale.procs[boxes.VENOM] = RunningProcess()
check("re-alignment waits until both apps have stopped",
      not stale.realign_phonebox("box-1") and stale.cfg == before_alignment)
stale.procs[boxes.VENOM] = None
check("unknown box re-alignment is refused without mutation",
      not stale.realign_phonebox("missing") and stale.cfg == before_alignment)
serve([])
check("re-alignment checks the fresh scan rather than a stale table",
      not stale.realign_phonebox("box-1") and stale.cfg == before_alignment)
serve(current_nine)
disk_before_failed_alignment = disk_cfg()
with mock.patch.object(boxes.os, "replace", side_effect=OSError("write denied by test")):
    failed_alignment = stale.realign_phonebox("box-1")
check("failed alignment save leaves memory and disk membership unchanged",
      not failed_alignment and stale.cfg == before_alignment
      and disk_cfg() == disk_before_failed_alignment
      and "write denied by test" in stale.status.text())
check("explicit re-alignment joins all nine under the selected box",
      stale.realign_phonebox("box-1"))
check("aligned box retains its name and shows every current phone",
      len(stale.cfg["groups"]) == 1
      and group_for(stale, current_nine_serials)["id"] == "box-1"
      and group_for(stale, current_nine_serials)["name"] == "My first farmbox"
      and group_item(stale, "box-1").text(1).replace(" ", "") == "9/20"
      and set(stale.rows) == set(current_nine_serials))
check("saved routes and unrelated configuration survive re-alignment",
      all(stale.cfg["assign"].get(serial) == role for serial, role in legacy_routes.items())
      and stale.cfg["user_note"] == seed["user_note"])
history = stale.cfg.get("alignment_history", [])
check("displaced old and provisional membership remain recoverable",
      any(entry["id"] == "box-1" and set(entry["serials"]) == set(first_serials)
          for entry in history)
      and any(set(entry["serials"]) == set(new_five_serials) for entry in history))
check("current aligned members are removed from all pending queues",
      not set(current_nine_serials).intersection(
          serial for members in stale.cfg["pending"].values() for serial in members)
      and not set(current_nine_serials).intersection(stale.cfg["pending_order"]))
with open(stale.last_alignment_backup, "r", encoding="utf-8") as fh:
    alignment_backup = json.load(fh)
check("a backup preserves the exact configuration before repair",
      alignment_backup == before_alignment
      and os.path.dirname(os.path.abspath(stale.last_alignment_backup)) == _tmp)
stale.rescan()
check("another scan preserves the aligned nine-member group",
      len(stale.cfg["groups"]) == 1
      and group_for(stale, current_nine_serials)["id"] == "box-1"
      and set(stale.rows) == set(current_nine_serials))
stale.close()
aligned_reload = open_hub()
aligned_reload.select_phonebox("box-1")
check("reopen restores the alignment and historical membership backups",
      len(aligned_reload.cfg["groups"]) == 1
      and set(aligned_reload.rows) == set(current_nine_serials)
      and aligned_reload.cfg.get("alignment_history") == history)

print("\n13) newly scanned phones keep their saved app while a partial box grows")
boxes.CONFIG_PATH = os.path.join(_tmp, "legacy_partial_routes.json")
incoming_legacy_serial = first_serials[9]
with open(boxes.CONFIG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"auto": False, "assign": {incoming_legacy_serial: boxes.VENOM}}, fh)
serve(no_usb_first[:9])
legacy_partial = open_hub()
legacy_partial_id = group_for(legacy_partial, nine_serials)["id"]
legacy_partial.move_phonebox(legacy_partial_id, boxes.POISON)
legacy_partial.procs[boxes.VENOM] = RunningProcess()
legacy_partial.launched_serials[boxes.VENOM] = {incoming_legacy_serial}
serve(no_usb_first)
legacy_partial.rescan()
check("a newly scanned member retains its saved Venom route",
      legacy_partial.cfg["assign"][incoming_legacy_serial] == boxes.VENOM
      and legacy_partial.assign[incoming_legacy_serial] == boxes.VENOM)
check("the other ten incoming phones inherit Poison and mark the group Mixed",
      all(legacy_partial.assign[serial] == boxes.POISON
          for serial in first_serials if serial != incoming_legacy_serial)
      and group_item(legacy_partial, legacy_partial_id).text(2) == "Mixed")
check("the two app assignments stay disjoint while Venom runs",
      not set(legacy_partial.serials_for(boxes.POISON)).intersection(
          legacy_partial.serials_for(boxes.VENOM))
      and legacy_partial.launched_serials[boxes.VENOM] == {incoming_legacy_serial})
legacy_partial.procs[boxes.VENOM] = None

print("\n14) an app launched with nine phones requests a restart for the next eleven")
boxes.CONFIG_PATH = os.path.join(_tmp, "running_partial_box.json")
serve(no_usb_first[:9])
running_partial = open_hub()
running_partial_id = group_for(running_partial, nine_serials)["id"]
running_partial.move_phonebox(running_partial_id, boxes.POISON)
running_partial.procs[boxes.POISON] = RunningProcess()
running_partial.launched_serials[boxes.POISON] = set(nine_serials)
serve(no_usb_first)
running_partial.rescan()
running_partial.poll_children()
check("the running app reports exactly eleven newly scanned phones",
      "Restart for 11 newly scanned phones" in
      running_partial.panel[boxes.POISON].state_lbl.text())
check("rescan keeps the running process's original launch mask unchanged",
      running_partial.launched_serials[boxes.POISON] == set(nine_serials)
      and set(running_partial.serials_for(boxes.POISON)) == set(first_serials)
      and not running_partial.serials_for(boxes.VENOM))

for window in windows:
    window.procs = {boxes.POISON: None, boxes.VENOM: None}
    window.close()
print("\n" + ("PASS: physical phoneboxes can be scanned, renamed and dragged"
              if not fails else "FAIL (%d):\n  " % len(fails) + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
