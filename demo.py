"""A local sample fleet. All settings are temporary; no ADB or child apps run."""
import os
import sys
import tempfile


def run():
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication
    import boxes
    from hub import Hub, app_icon, GREEN

    fleet = [{
        "serial": "DEMO-%s-%02d" % ("A" if n < 20 else "B", n % 20 + 1),
        "model": ("SM_A125F", "Redmi_Note_11", "SM_A032F", "CPH2269")[n % 4],
        "state": "device", "usb": "1-%d.%d" % (1 if n < 20 else 2, n % 20 + 1),
    } for n in range(40)]

    class DemoHub(Hub):
        def launch(self, role):
            self.say("Demo fleet · launches are available in the live hub", GREEN)

        def launch_both(self):
            self.launch(boxes.POISON)

        def show_updater(self):
            self.say("Open Updates from the live hub to configure your release feed", GREEN)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(app_icon()))
    previous_path, previous_scan = boxes.CONFIG_PATH, boxes.list_devices_verbose
    try:
        with tempfile.TemporaryDirectory(prefix="fbvenoison-demo-") as directory:
            boxes.CONFIG_PATH = os.path.join(directory, "boxes.json")
            boxes.list_devices_verbose = lambda: [dict(d) for d in fleet]
            cfg = boxes.load()
            boxes.discover_groups(fleet, cfg)
            for index, group in enumerate(cfg["groups"]):
                group["name"] = ("Alpha · Poison", "Beta · Venom")[index]
                boxes.assign_group(cfg, group["id"], (boxes.POISON, boxes.VENOM)[index])
            boxes.save(cfg, strict=True)
            window = DemoHub()
            window.setWindowTitle("FBVenoison · Demo fleet · no real devices")
            window.show()
            window.say("Demo fleet · 40 sample devices · settings are temporary", GREEN)
            return app.exec()
    finally:
        boxes.CONFIG_PATH, boxes.list_devices_verbose = previous_path, previous_scan
