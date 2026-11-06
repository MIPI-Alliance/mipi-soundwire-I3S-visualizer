"""A decode in flight is cancelled through the native API when the window closes or a newer
capture is opened, and the window says so.

The decoders here are stand-ins whose `run` is a wait that only `cancel()` ends, so a load
that is not cancelled fails these by timing out, not by finishing.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui

_WAIT_S = 20.0


class _BlockingDecoder:
    """Reads nothing until cancel() is called, as a decoder blocked in run()."""

    def __init__(self) -> None:
        self.cancel_calls = 0
        self._gate = threading.Event()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._gate.set()

    def run(self) -> bool:
        """True if the wait ended by cancel(), False on timeout."""
        return self._gate.wait(_WAIT_S)


def _pump_until(pred, what: str) -> None:
    app = QApplication.instance()
    deadline = time.time() + _WAIT_S
    while not pred():
        assert time.time() < deadline, f"timed out waiting for {what}"
        app.processEvents()
        time.sleep(0.005)


def _blocking_factory(made: list, outcome: list):
    """A factory that starts a blocking decoder through decoder_ready and runs it."""
    def factory(decoder_ready=None):
        dec = _BlockingDecoder()
        made.append(dec)
        decoder_ready(dec)
        outcome.append(dec.run())
        raise RuntimeError("the cancelled decode left partial state")
    return factory


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    return MainWindow()


def test_the_worker_cancels_its_decoder_and_reports_cancelled_not_failed():
    from swi3s_studio.ui.main_window import _LoadWorker
    QApplication.instance() or QApplication([])
    made, outcome, seen = [], [], []
    worker = _LoadWorker(_blocking_factory(made, outcome))
    for name in ("done", "failed", "cancelled"):
        getattr(worker, name).connect(lambda *a, n=name: seen.append(n))
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    try:
        _pump_until(lambda: worker.decoder is not None, "the decoder")
        worker.cancel()
        _pump_until(lambda: bool(seen), "the worker to settle")
    finally:
        thread.quit()
        thread.wait()
    assert seen == ["cancelled"]
    assert outcome == [True] and made[0].cancel_calls >= 1


def test_a_decode_that_fails_before_a_cancel_reports_failed_alone():
    from swi3s_studio.ui.main_window import _LoadWorker
    QApplication.instance() or QApplication([])
    seen = []

    def factory():
        raise RuntimeError("bad capture")
    worker = _LoadWorker(factory)
    for name in ("done", "failed", "cancelled"):
        getattr(worker, name).connect(lambda *a, n=name: seen.append(n))
    run_once = worker._run

    def run_then_cancel():
        out = run_once()
        worker.cancel()                     # the cancel lands after the failure was seen
        return out
    worker._run = run_then_cancel
    worker.run()
    assert seen == ["failed"]


def test_a_decoder_started_after_the_request_is_cancelled_before_it_reads():
    from swi3s_studio.ui.main_window import _LoadWorker
    worker = _LoadWorker(lambda **kw: None)
    worker.cancel()                         # before any decoder exists
    dec = _BlockingDecoder()
    worker._on_decoder_ready(dec)
    assert dec.cancel_calls == 1 and dec.run() is True


def test_closing_the_window_cancels_the_decode_and_shows_the_notice():
    from swi3s_studio.ui.main_window import CLOSING_NOTICE
    win = _window()
    made, outcome, after = [], [], []
    win._load_async(_blocking_factory(made, outcome), after=lambda s: after.append(s),
                    on_fail=lambda: after.append("failed"))
    _pump_until(lambda: win._load_worker.decoder is not None, "the decoder")
    dlg = win._load_dlg
    start = time.time()
    win.close()                                        # closeEvent joins the worker threads
    assert time.time() - start < _WAIT_S / 2          # returned on cancel, not on the timeout
    assert outcome == [True] and made[0].cancel_calls >= 1
    assert CLOSING_NOTICE in win._status.text()
    assert win._load_thread is None and win._load_dlg is None
    assert not dlg.isVisible()
    QApplication.instance().processEvents()
    assert after == []                                 # a cancelled request settles neither way


def test_closing_with_no_decode_running_shows_no_notice():
    from swi3s_studio.ui.main_window import CLOSING_NOTICE
    win = _window()
    win.close()
    assert CLOSING_NOTICE not in win._status.text()
    win.join_worker_threads()                          # idempotent after the close


@pytest.mark.parametrize("running", ["open", "add"])
def test_opening_a_capture_cancels_the_decode_it_replaces(running):
    from conftest import pump_loads

    from swi3s_studio.session import Session
    win = _window()
    made, outcome, calls = [], [], []
    win._load_async(_blocking_factory(made, outcome), kind=running,
                    after=lambda s: calls.append("first"), on_fail=lambda: calls.append("first!"))
    _pump_until(lambda: win._load_worker.decoder is not None, "the decoder")
    newer = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win._load_async(lambda **kw: newer, kind="open", after=lambda s: calls.append("newer"))
    pump_loads(win, seconds=_WAIT_S)
    assert outcome == [True] and made[0].cancel_calls >= 1
    assert calls == ["newer"]
    assert win.links.index_of(newer) >= 0
    win.join_worker_threads()


def test_opening_a_capture_does_not_cancel_a_re_decode():
    from conftest import pump_loads

    from swi3s_studio.session import Session
    win = _window()
    base = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win.load_session(base)
    gate = threading.Event()
    calls = []

    def redecode(**_kw):
        assert gate.wait(_WAIT_S)
        return base
    win._load_async(redecode, kind="redecode", session=base,
                    after=lambda s: calls.append("redecode"))
    _pump_until(lambda: win._load_worker is not None, "the worker")
    worker = win._load_worker
    newer = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win._load_async(lambda **kw: newer, kind="open", after=lambda s: calls.append("open"))
    assert not worker._cancel.is_set()
    gate.set()
    pump_loads(win, seconds=_WAIT_S)
    assert calls == ["redecode", "open"]
    win.join_worker_threads()


def test_a_real_decode_stops_promptly_when_the_window_closes():
    """The native decoder, not a stand-in: a demo that takes seconds to decode is cancelled
    from the close once it is reading, the window shows the closing notice, and the request
    settles neither as done nor as failed."""
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import CLOSING_NOTICE
    win = _window()
    after = []
    win._load_async(lambda **kw: Session.from_demo(Session.DEMO_SAMPLES_MAX, cold_start=True,
                                                   phy=2, register_map=win._rmap, **kw),
                    after=lambda s: after.append(s), on_fail=lambda: after.append("failed"))
    def reading():
        worker = win._load_worker
        return (worker is not None and worker.decoder is not None
                and worker.decoder.progress_uis > 0)
    _pump_until(reading, "the decode to start reading")
    dec = win._load_worker.decoder
    start = time.time()
    win.close()                                        # closeEvent joins the worker threads
    assert dec.cancelled
    # Stopped before the end of the capture: a full run reads every UI but the last.
    assert 0 < dec.progress_uis < dec.total_uis - 1, (dec.progress_uis, dec.total_uis)
    assert time.time() - start < _WAIT_S / 2
    assert CLOSING_NOTICE in win._status.text()
    assert win._load_thread is None
    QApplication.instance().processEvents()
    assert after == []


@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_a_locate_result_delivered_after_the_close_opens_nothing(outcome, monkeypatch):
    """A Locate Sub-Capture search's done or failed can arrive after the close has joined its
    thread: it is dropped, with no message box, no import warnings and no cursors placed."""
    from types import SimpleNamespace

    from PySide6.QtWidgets import QMessageBox
    win = _window()
    win.load_demo()
    win._locate_session = win._session
    win._locate_path = "reference.sal"
    win._locate_worker = SimpleNamespace(notes=["a note the search made"])
    win.close()                                        # closeEvent joins the worker threads
    shown = []
    monkeypatch.setattr(win, "show_import_warnings", lambda *a, **k: shown.append("notes"))
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, n=name, **k: shown.append(n))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append("exec"))
    marks = len(win._bookmarks)
    if outcome == "done":
        win._on_locate_done([], {"best_score": 0.5, "best_start_sample": 100})
    else:
        win._on_locate_failed("the reference could not be read")
    assert shown == [] and len(win._bookmarks) == marks
    assert win._locate_session is None
