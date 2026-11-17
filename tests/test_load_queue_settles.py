"""Every load the window is asked for that is neither superseded nor cancelled SETTLES:
exactly one of its `after` or `on_fail` runs. A superseded or cancelled request settles with
neither.

A caller sequencing several loads waits on those callbacks. A reopened workspace counts its
Links down through them and applies its saved state when the count reaches zero, so a load
that settles neither way leaves the count stuck. These make each stage fail in turn, for
each kind of load, alone and queued behind another, and require the request to settle once,
the queue to drain, and the user to be told.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QMessageBox

pytestmark = pytest.mark.gui

# Where a load can fail: the decode on the worker thread; building the views, before or
# after the session became a Link; and the caller's own follow-on.
_STAGES = (None, "decode", "views", "views_after_link", "after")
_KINDS = ("open", "add", "redecode")


def _session(win):
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_session(_session(win))
    return win


def _expected(kind, stage):
    """`after` when the session ended up a Link, else `on_fail`. A re-decode's session is a
    Link already, so even its views failing leaves it one."""
    if stage == "decode" or (stage == "views" and kind != "redecode"):
        return "on_fail"
    return "after"


def _request(win, kind, stage, calls, label):
    target = win._session if kind == "redecode" else _session(win)

    def factory(*fork, **_kw):
        # A re-decode is handed the fork of its session (Session.fork) and returns it.
        if stage == "decode":
            raise RuntimeError(f"{label}: decode failed")
        return fork[0] if fork else target

    def after(_s):
        calls.append((label, "after"))
        if stage == "after":
            raise RuntimeError(f"{label}: after failed")
    win._load_async(factory, after=after, kind=kind,
                    on_fail=lambda: calls.append((label, "on_fail")))
    return target


def _failing_views(win, monkeypatch, failing):
    """load_session raising for the targets in `failing`: {session: stage}."""
    build = win.load_session

    def load_session(session, *a, **k):
        stage = failing.get(id(session))
        if stage == "views":
            raise RuntimeError("views failed")
        result = build(session, *a, **k)
        if stage == "views_after_link":
            raise RuntimeError("views failed after the Link was made")
        return result
    monkeypatch.setattr(win, "load_session", load_session)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("stage", _STAGES)
def test_a_load_settles_exactly_once_whichever_stage_fails(kind, stage, monkeypatch):
    win = _window()
    said = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: said.append(a[2]))
    failing = {}
    _failing_views(win, monkeypatch, failing)
    calls = []
    target = _request(win, kind, stage, calls, "the load")
    if stage in ("views", "views_after_link"):
        failing[id(target)] = stage
    pump_loads(win)
    assert calls == [("the load", _expected(kind, stage))], calls
    assert getattr(win, "_load_thread", None) is None and not win._load_pending
    assert bool(said) == (stage is not None), said      # a failure is reported, once
    assert len(said) <= 1, said


@pytest.mark.parametrize("stage", _STAGES[1:])
def test_a_failed_load_does_not_stall_the_ones_queued_behind_it(stage, monkeypatch):
    """Three Links added back to back: the first decodes while the other two queue. The
    middle one fails at `stage`; every one still settles, in order, and the last loads."""
    win = _window()
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    failing = {}
    _failing_views(win, monkeypatch, failing)
    calls = []
    _request(win, "add", None, calls, "first")
    middle = _request(win, "add", stage, calls, "middle")
    if stage in ("views", "views_after_link"):
        failing[id(middle)] = stage
    last = _request(win, "add", None, calls, "last")
    pump_loads(win)
    assert calls == [("first", "after"), ("middle", _expected("add", stage)), ("last", "after")]
    assert win.links.index_of(last) >= 0


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_a_re_decode_of_a_removed_link_settles_as_failed(monkeypatch, running):
    """A re-decode for a Link that is removed, before it runs or while it runs, is
    discarded, since loading it would make it a new capture. Discarded is not loaded: it
    settles as failed."""
    win = _window()
    win.load_session(_session(win), add_link=True)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    calls = []
    if not running:
        _request(win, "add", None, calls, "busy")          # occupies the worker
    gone = win.links[1].session
    win._load_async(lambda fork, **_kw: fork, after=lambda _s: calls.append(("redecode", "after")),
                    kind="redecode", session=gone,
                    on_fail=lambda: calls.append(("redecode", "on_fail")))
    win.remove_link(1, confirm=False)
    pump_loads(win)
    assert ("redecode", "after") not in calls
    assert calls.count(("redecode", "on_fail")) == 1, calls


def test_queued_re_decodes_that_set_different_things_all_land():
    """Re-decodes of one Link queued behind a running decode: a register edit and two SSP
    steps leave the edit and the later SSP."""
    win = _window()
    s = win._session
    dev = 1
    win._load_thread = object()                       # a decode is running
    try:
        win.set_ssp_row_async(5)
        win._on_register_edited(dev, 0x48, 0x5A)
        win.set_ssp_row_async(7)                      # replaces the queued SSP only
        assert len(win._load_pending) == 2
    finally:
        win._load_thread = None
    win._run_pending_load()
    pump_loads(win)
    assert s.ssp_row == 7
    sect = s.segment_index_for_sample(win.cursor.sample)
    assert s.register_overrides.get((sect, dev, 0x48)) == 0x5A


@pytest.mark.parametrize("mode", ("timing", "visualization"))
def test_a_re_decode_landing_in_another_mode_leaves_the_mode(mode):
    """A re-decode keeps the mode the user moved to while it ran; an added Link lands in
    Analysis."""
    from swi3s_studio.ui.mode_controller import ANALYSIS, TIMING, VISUALIZATION
    win = _window()
    target = {"timing": TIMING, "visualization": VISUALIZATION}[mode]
    win.set_ssp_row_async(4)
    win._mode_mgr.switch_to(target)                  # while the re-decode runs
    pump_loads(win)
    assert win._session.ssp_row == 4
    assert win._mode_mgr.current() == target
    win._load_async(lambda **_kw: _session(win), kind="add")
    pump_loads(win)
    assert len(win.links) == 2 and win._mode_mgr.current() == ANALYSIS


def test_a_re_decode_queued_behind_an_open_is_dropped_when_the_open_replaces_its_link():
    """An open running, then a re-decode of the Link it will replace: the open lands, the
    re-decode is dropped and settles as failed, and no decode of the replaced Link runs."""
    win = _window()
    old = win._session
    new = _session(win)
    calls, forked = [], []
    win._load_async(lambda **_kw: new)                # an open, running

    def redecode(fork):
        forked.append(fork)
        return fork
    win._load_async(redecode, after=lambda _s: calls.append("after"), kind="redecode",
                    session=old, on_fail=lambda: calls.append("on_fail"))
    assert win._load_thread is not None and len(win._load_pending) == 1
    pump_loads(win)
    assert win._session is new and len(win.links) == 1
    assert calls == ["on_fail"] and not forked


def test_an_open_settles_the_running_and_the_queued_loads_it_replaces_with_neither(monkeypatch):
    """An open cancels the running decode of a capture and drops the Links queued behind it:
    those loads call neither `after` nor `on_fail`, the open settles once, and the queue
    drains."""
    win = _window()
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    calls = []
    _request(win, "add", None, calls, "running")
    _request(win, "add", None, calls, "queued")
    _request(win, "open", None, calls, "opener")
    pump_loads(win)
    assert calls == [("opener", "after")], calls
    assert getattr(win, "_load_thread", None) is None and not win._load_pending


def test_an_open_discards_an_added_link_whose_decode_finished_before_the_open(monkeypatch):
    """An open asked for after an added Link's decode finished, but before its result is
    delivered, cancels it all the same: the added Link settles with neither callback and
    its session is closed unloaded."""
    import threading

    import swi3s_studio.ui.main_window as mw
    win = _window()
    from PySide6.QtCore import Qt
    finished = threading.Event()
    init = mw._LoadWorker.__init__

    def init_and_mark(self, factory):
        init(self, factory)
        self.done.connect(lambda *_a: finished.set(), Qt.DirectConnection)
    monkeypatch.setattr(mw._LoadWorker, "__init__", init_and_mark)
    calls = []
    added = _request(win, "add", None, calls, "added")
    closed = []
    monkeypatch.setattr(added, "close", lambda: closed.append(True))
    assert finished.wait(30)
    _request(win, "open", None, calls, "opener")
    pump_loads(win)
    assert calls == [("opener", "after")], calls
    assert closed == [True] and len(win.links) == 1
    assert win.links.index_of(added) < 0
