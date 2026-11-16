import sys
import os
import updater          # imported at top level so PyInstaller bundles it

if __name__ == "__main__":
    if "--cli" in sys.argv:
        from core import cli_main
        updater.bootstrap(log=lambda s: print("  [update] " + s))
        updater.start(log=lambda s: print("  [update] " + s))
        cli_main()
    else:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon
        from gui import ReactionGUI, app_icon_pixmap
        from auth import require_key

        # Start on the newest build already downloaded, before any window
        # exists - the .exe is only ever a bootstrap.
        updater.bootstrap(log=lambda s: print("  [update] " + s))

        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setWindowIcon(QIcon(app_icon_pixmap(256)))

        # Locked gate: the wrong key plays the denial sequence and never
        # returns True, so the program exits without ever building the GUI.
        if os.environ.get("FBVENOISON_AUTHED") != "1" and not require_key():
            sys.exit(0)

        gui = ReactionGUI()
        gui.show_splash()

        # Poll for new logic and graft it into this process. A run already in
        # flight is not interrupted - see updater.hot_swap().
        updater.start(log=lambda s: gui.send_cmd({"action": "toast", "text": s}))

        sys.exit(app.exec())
