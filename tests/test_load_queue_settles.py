"""Every load the window is asked for SETTLES: exactly one of its `after` or `on_fail` runs.

A caller sequencing several loads waits on those callbacks. A reopened workspace counts its
Links down through them and applies its saved state when the count reaches zero, so a load
that settled neither way left the count stuck, and the Links that did load kept default
names, offsets 0 and no bookmarks. That happened when building the views raised: the path
was not one any test made fail. These make each stage fail in turn, for each kind of load,
alone and queued behind another, and require the request to settle once, the queue to
drain, and the user to be told.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QMessageBox

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

    def factory(**_kw):
        if stage == "decode":
            raise RuntimeError(f"{label}: decode failed")
        return target

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
    win._load_async(lambda **_kw: gone, after=lambda _s: calls.append(("redecode", "after")),
                    kind="redecode", session=gone,
                    on_fail=lambda: calls.append(("redecode", "on_fail")))
    win.remove_link(1, confirm=False)
    pump_loads(win)
    assert ("redecode", "after") not in calls
    assert calls.count(("redecode", "on_fail")) == 1, calls
