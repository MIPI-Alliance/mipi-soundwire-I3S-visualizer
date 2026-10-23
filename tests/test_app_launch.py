"""App launch helper: focusing/activating the window on startup.

`app._activate` raises and focuses the window and, on macOS, activates an unbundled
`python -m swi3s_studio.app` process so it owns the native menu bar. This checks the
helper is import-safe and does not raise, on a real window and on a minimal stub
(PyObjC / macOS may be absent).

Run: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_app_launch.py
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
