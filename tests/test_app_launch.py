"""App launch helper: focusing/activating the window on startup.

`app._activate` fixes the macOS unbundled "no menu bar" case — an unbundled
`python -m swi3s_studio.app` process isn't a foreground GUI app by default, so the
native (top-of-screen) menu bar stays owned by the launching terminal until the app
is activated. This checks the helper is import-safe and doesn't raise, on a real
window and on a minimal stub (PyObjC / macOS may be absent).

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_app_launch.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow

from swi3s_studio import app as _app


def test_activate_is_import_safe():
    QApplication.instance() or QApplication([])
    # Real window: raise_/activateWindow (+ the guarded macOS AppKit path) must not raise.
    w = QMainWindow()
    w.show()
    _app._activate(w)

    # Minimal stub exercises the cross-platform focus path with no Qt window state.
    class _Stub:
        raised = activated = False
        def raise_(self):
            self.raised = True
        def activateWindow(self):
            self.activated = True
    s = _Stub()
    _app._activate(s)
    assert s.raised and s.activated, "activate must raise + focus the window"


if __name__ == "__main__":
    test_activate_is_import_safe()
    print("ok: app._activate focuses the window and is import-safe")
    print("ALL APP-LAUNCH TESTS PASSED")
