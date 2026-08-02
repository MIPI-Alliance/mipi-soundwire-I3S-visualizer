"""Sub-capture locate progress/teardown GUI regressions.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_locate_gui.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


def test_locate_progress_survives_reentrant_teardown():
    """A modal QProgressDialog.setValue() re-enters the event loop, which can dispatch the
    queued 'done' signal and tear _locate_dlg down MID-CALL. _on_locate_progress must work
    off a local ref (and do setValue last), and _teardown_locate_thread must be idempotent —
    otherwise the progress tick dereferences a None dialog (the AttributeError crash seen
    during a real sub-capture locate)."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()

    class _FakeDlg:
        def __init__(self):
            self.label = None
            self.value = None

        def setLabelText(self, text):
            self.label = text

        def setValue(self, v):
            self.value = v
            win._locate_dlg = None          # re-entrant teardown fires during setValue

        def close(self):
            pass

    win._locate_dlg = _FakeDlg()
    win._on_locate_progress(50, "phase")    # must NOT raise despite the mid-call teardown
    assert win._locate_dlg is None

    # Idempotent teardown: a second (re-entrant) done/failed must not crash on the None dlg.
    win._teardown_locate_thread()           # already torn down -> early return, no AttributeError

    # And a progress tick with no active dialog is a no-op, not a crash.
    win._on_locate_progress(75, "later")
