"""SWI3S Studio application entry point.

    python3 -m swi3s_studio.app                          # empty window (File > Load Demo)
    python3 -m swi3s_studio.app --demo                   # open the synthetic demo capture
    python3 -m swi3s_studio.app --demo-bringup           # demo with a §5.1.2 Cold Start
    python3 -m swi3s_studio.app -c cfg.csv               # open a Visualizer config CSV
    python3 -m swi3s_studio.app -c cfg.csv -o model.json # headless: write the bus model

Argument handling lives in `cli.py`, which is Qt-free, and `-o` is dispatched there BEFORE
anything Qt is constructed — see `main`. Run with `--help` for the full list.

NOTHING QT IS IMPORTED AT MODULE LEVEL, and that is what makes "-o implies headless" true rather
than nearly true: a batch model dump neither imports PySide6 nor touches the UI package, so it
cannot be broken by a display problem, a Qt version, or anything in the window's construction.
`tests/test_cli.py` pins it by checking sys.modules in a subprocess. The cost is the two
deferred-import helpers below; the alternative was a module-level QProxyStyle subclass, which
pulls in QtWidgets just to be defined.
"""
from __future__ import annotations

import sys

from . import cli

_APP_NAME = "SWI3S Studio"
_MIN_PY = (3, 11)


def _centered_tab_style():
    """Build the tab-centering proxy style. A FUNCTION, not a module-level class, because
    subclassing QProxyStyle requires QtWidgets at import time and the headless path must not
    import Qt at all — see the module docstring.

    Center tab-bar tabs so tabbed docks match macOS. The Windows/Fusion styles left-align a
    QTabBar's tabs; SH_TabBar_Alignment is the single style hint that controls this, and it
    governs QMainWindow's dock tab bars (the bottom pane group + the
    Registers/Statistics/Bookmarks group) too. macOS already returns AlignCenter, so this proxy
    is only installed off-darwin (leaving the native Mac style intact).

    (The dock close/float button chrome is handled by a custom title-bar widget in main_window,
    not a style hint — the native style paints a button frame the QSS can't suppress.)
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class _CenteredTabStyle(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
            if hint == QStyle.StyleHint.SH_TabBar_Alignment:
                return int(Qt.AlignmentFlag.AlignCenter)
            return super().styleHint(hint, option, widget, returnData)

    return _CenteredTabStyle()



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
    args, qt_argv = cli.parse(argv)
    # -o IMPLIES HEADLESS: return before any QApplication, window or event loop exists, so a
    # batch run needs no display and cannot be affected by a saved appearance preference or a
    # restored workspace. cli is Qt-free; this is the only place that decides between the two.
    if args.output:
        return cli.run_headless(args.config, args.output)
    # Qt and the UI package are imported HERE, past the headless return, so a model dump does
    # not pay for them and cannot fail on them.
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import apply_palette, resolve_mode, saved_preference

    # Name the app before QApplication so the macOS application menu reads
    # "SWI3S Studio" instead of "Python".
    QApplication.setApplicationName(_APP_NAME)
    QApplication.setApplicationDisplayName(_APP_NAME)
    QApplication.setOrganizationName("MIPI SWI3S")
    _name_macos_app_menu()
    _set_regular_activation_policy()   # become a normal GUI app BEFORE QApplication
    app = QApplication(qt_argv)
    app.setApplicationName(_APP_NAME)
    # Center tabbed-dock tab bars off macOS (Windows/Fusion left-align them; macOS's
    # native style already centers and is left untouched).
    if sys.platform != "darwin":
        app.setStyle(_centered_tab_style())
    # Apply the saved appearance preference before building the window so every
    # widget is constructed with the right palette ('system' follows the OS).
    apply_palette(resolve_mode(saved_preference()))
    win = MainWindow()
    if args.demo_bringup:
        win.load_demo_bringup()
    elif args.demo:
        win.load_demo()
    elif args.config:
        win.open_visualizer_csv_path(args.config)
    win.show()
    # Qt-level focus is safe before the event loop; the macOS Cocoa activation is NOT —
    # calling NSApplication.activate() before app.exec() spins AppKit's activation while
    # no run loop is live, which prints "modalSession has been exited prematurely" on macOS
    # (and is a no-op for an unbundled process anyway, per _activate). Defer the Cocoa part
    # to the singleShot calls below, which fire once the loop is running.
    _activate(win, cocoa=False)
    # Once the event loop is live, demo loads may use the async worker+progress path (before
    # this, the __init__ preload ran synchronously). Flag it on the first loop iteration.
    def _mark_loop_live() -> None:
        MainWindow._event_loop_live = True
    QTimer.singleShot(0, _mark_loop_live)
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


def _activate(win, cocoa: bool = True) -> None:
    """Bring the window to the front and make this process the active application.
    `raise_`/`activateWindow` focus the window on every platform; on macOS we also
    (re)assert the Regular policy and activate via the modern NSApplication.activate()
    (macOS 14+), falling back to the deprecated activateIgnoringOtherApps_ and a
    NSRunningApplication activate. No-op off macOS / when PyObjC is absent.

    `cocoa=False` does ONLY the cross-platform Qt focus and skips the Cocoa activation —
    used for the pre-exec() call, since activating via AppKit before the event loop is
    running triggers a spurious "modalSession has been exited prematurely" warning (and is
    ineffective for an unbundled process regardless; the post-loop calls do the real work)."""
    win.raise_()
    win.activateWindow()
    if sys.platform != "darwin" or not cocoa:
        return
    try:
        from AppKit import (
            NSApp,
            NSApplicationActivateIgnoringOtherApps,
            NSApplicationActivationPolicyRegular,
            NSRunningApplication,
        )
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
