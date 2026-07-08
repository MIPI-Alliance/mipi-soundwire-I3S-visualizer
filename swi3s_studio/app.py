"""SWI3S Studio application entry point.

    python3 -m swi3s_studio.app                 # empty window (File > Load Demo)
    python3 -m swi3s_studio.app --demo          # open the synthetic demo capture
    python3 -m swi3s_studio.app --demo-bringup  # demo with a §5.1.2 Cold Start
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow
from .ui.theme import apply_palette, resolve_mode, saved_preference

_APP_NAME = "SWI3S Studio"
_MIN_PY = (3, 11)


def _require_python() -> None:
    """Fail early (before building anything) if the interpreter is too old. A clear
    message beats a later, cryptic failure from a 3.11+ feature."""
    if sys.version_info < _MIN_PY:
        have = ".".join(map(str, sys.version_info[:3]))
        need = ".".join(map(str, _MIN_PY))
        sys.stderr.write(f"SWI3S Studio requires Python {need}+ (running {have}).\n")
        raise SystemExit(1)


def main(argv=None) -> int:
    _require_python()
    argv = list(sys.argv if argv is None else argv)
    # Name the app before QApplication so the macOS application menu reads
    # "SWI3S Studio" instead of "Python".
    QApplication.setApplicationName(_APP_NAME)
    QApplication.setApplicationDisplayName(_APP_NAME)
    QApplication.setOrganizationName("MIPI SWI3S")
    _name_macos_app_menu()
    _set_regular_activation_policy()   # become a normal GUI app BEFORE QApplication
    app = QApplication(argv)
    app.setApplicationName(_APP_NAME)
    # Apply the saved appearance preference before building the window so every
    # widget is constructed with the right palette ('system' follows the OS).
    apply_palette(resolve_mode(saved_preference()))
    win = MainWindow()
    if "--demo-bringup" in argv:
        win.load_demo_bringup()
    elif "--demo" in argv:
        win.load_demo()
    win.show()
    _activate(win)
    # Activate again once the event loop is live, and once more after it settles —
    # a single pre-exec() call is ignored for an unbundled process (the app stays
    # in the background with its menu bar hidden until clicked).
    QTimer.singleShot(0, lambda: _activate(win))
    QTimer.singleShot(200, lambda: _activate(win))
    return app.exec()


def _set_regular_activation_policy() -> None:
    """Promote the process to a Regular (Dock + menu bar) app *before* QApplication.
    An unbundled `python -m …` process starts as a background/accessory process, so
    the OS doesn't bring it forward on launch and its menu bar never takes over the
    top of the screen. Setting the policy this early lets macOS treat it as a normal
    GUI-app launch (it then activates it itself), which matters on recent macOS where
    an app can't reliably force itself frontmost afterwards. No-op off macOS / no PyObjC."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
    except Exception:  # noqa: BLE001 - PyObjC absent
        pass


def _activate(win) -> None:
    """Bring the window to the front and make this process the active application.
    `raise_`/`activateWindow` focus the window on every platform; on macOS we also
    (re)assert the Regular policy and activate via the modern NSApplication.activate()
    (macOS 14+), falling back to the deprecated activateIgnoringOtherApps_ and a
    NSRunningApplication activate. No-op off macOS / when PyObjC is absent."""
    win.raise_()
    win.activateWindow()
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (NSApp, NSApplicationActivationPolicyRegular,
                            NSApplicationActivateIgnoringOtherApps, NSRunningApplication)
        ns = NSApp()
        if ns is not None:
            ns.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            if hasattr(ns, "activate"):
                ns.activate()                          # macOS 14+ preferred API
            else:
                ns.activateIgnoringOtherApps_(True)
        cur = NSRunningApplication.currentApplication()
        if cur is not None:
            cur.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:  # noqa: BLE001 - PyObjC absent / non-macOS; window focus above suffices
        pass


def _name_macos_app_menu() -> None:
    """Make the macOS application menu (the bold item next to the Apple menu) read
    "SWI3S Studio" instead of "Python". For an unbundled Python app that title comes
    from the main bundle's CFBundleName, which Qt's setApplicationName can't change —
    so patch the bundle's info dictionary directly (PyObjC), before QApplication."""
    try:
        from Foundation import NSBundle, NSProcessInfo
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = _APP_NAME
        NSProcessInfo.processInfo().setProcessName_(_APP_NAME)
    except Exception:                      # noqa: BLE001 - PyObjC absent / not macOS
        pass


if __name__ == "__main__":
    raise SystemExit(main())
