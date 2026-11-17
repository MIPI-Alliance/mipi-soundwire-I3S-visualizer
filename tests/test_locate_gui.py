"""Sub-capture locate progress/teardown GUI tests.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_locate_gui.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def test_locate_progress_survives_reentrant_teardown():
    """A modal QProgressDialog.setValue() re-enters the event loop, which can dispatch the
    queued 'done' signal and tear _locate_dlg down MID-CALL. _on_locate_progress must work
    off a local ref (and do setValue last), and _teardown_locate_thread must be idempotent,
    so the progress tick never dereferences a None dialog."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()

    class _FakeDlg:
        def __init__(self):
            self.label = None
            self.value = None

        def maximum(self):
            return 100

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


def test_a_dialog_torn_down_under_nested_progress_ticks_outlives_every_set_value():
    """setValue() re-enters the event loop, so a second progress tick can run inside the
    first and the search's end inside that. The dialog the end tears down is deleted only
    once the outermost setValue() has returned, never while one is still running on it."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    ctl = win._locate_ctl
    events = []

    class _Stub:
        def __init__(self, name):
            self.name = name

        def quit(self):
            pass

        def wait(self):
            pass

        def deleteLater(self):
            events.append(f"delete {self.name}")

    class _NestingDlg(_Stub):
        depth = 0

        def maximum(self):
            return 100

        def setLabelText(self, text):
            pass

        def close(self):
            pass

        def setValue(self, v):
            _NestingDlg.depth += 1
            events.append(f"enter {_NestingDlg.depth}")
            if _NestingDlg.depth == 1:
                ctl._on_progress(60, "")        # a second tick dispatched by this setValue
            else:
                ctl.teardown()                  # the search's end dispatched by the second
            events.append(f"leave {_NestingDlg.depth}")
            _NestingDlg.depth -= 1

    ctl.dlg = _NestingDlg("dlg")
    ctl.search_thread = _Stub("thread")
    ctl.worker = _Stub("worker")
    ctl._on_progress(50, "")
    assert ctl.dlg is None
    assert events.index("delete dlg") > events.index("leave 1"), events
    assert events.count("delete dlg") == 1


def _locate_with(monkeypatch, tmp_path, reference):
    """File ▸ Locate Sub-Capture answered with a real .sal, the reference read by
    `reference(pr, req)`; the message boxes shown are returned as (kind, text)."""
    import time

    from conftest import open_via_dialog
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.ingest.sal_export import export_sal
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    main = win._session.capture
    ref = str(tmp_path / "ref.sal")
    export_sal(main.subcapture(0, main.sample_rate_hz // 2000), ref)
    open_via_dialog(monkeypatch, [ref])
    monkeypatch.setattr(MainWindow, "_reference_capture", staticmethod(reference))
    boxes = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: boxes.append(("box", box.text())) or 0)
    monkeypatch.setattr(QMessageBox, "critical",
                        lambda parent, title, text, *a, **k: boxes.append((title, text)))
    win.locate_subcapture()
    started = win._locate_thread is not None
    deadline = time.time() + 60
    while getattr(win, "_locate_thread", None) is not None:
        assert time.time() < deadline, "the search never finished"
        QApplication.instance().processEvents()
        time.sleep(0.01)
    return win, started, boxes


def test_locate_reads_the_reference_off_the_gui_thread(monkeypatch, tmp_path):
    import threading

    from swi3s_studio import import_notes
    from swi3s_studio.ui.main_window import MainWindow
    where = []
    real = MainWindow._reference_capture

    def reference(pr, req):
        where.append(threading.current_thread() is threading.main_thread())
        import_notes.warn("ref.sal", "a signal was clamped")
        return real(pr, req)
    win, started, _boxes = _locate_with(monkeypatch, tmp_path, reference)
    assert started, "the dialog and its worker start before the reference is read"
    assert where == [False]
    dialogs = win.import_warning_dialogs()
    assert len(dialogs) == 1 and "clamped" in dialogs[0].warnings[0].message


def test_locate_reports_a_reference_that_cannot_be_read(monkeypatch, tmp_path):
    def reference(pr, req):
        raise ValueError("ref.sal: not a capture")
    win, started, boxes = _locate_with(monkeypatch, tmp_path, reference)
    assert started
    assert boxes == [("Locate failed", "ref.sal: not a capture")]
    assert win._locate_dlg is None and win._locate_worker is None
    assert win._locate_session is None                  # the failed search holds no Session
    assert len(win._bookmarks) == 0
