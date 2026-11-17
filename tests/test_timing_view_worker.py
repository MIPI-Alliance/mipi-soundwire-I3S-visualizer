"""The Timing page's recompute runs off the UI thread (`ui/latest_job.py`).

`LatestJob` applies only the latest request's result: a request superseded while another
runs never starts, a result collected after a newer submit is dropped, a worker's exception
is raised on the UI thread, and `stop` joins the worker and applies nothing. On the page the
corner search runs on the worker while the event loop keeps running, a stale result never
overwrites a newer one, and closing or deleting the page during a recompute joins or outlives
the worker cleanly, as does an interpreter exiting with one in flight.

From the start of a recompute until its result is shown the page holds no results: every
substitution number and margin and every Max Frequency shows the placeholder, the term diagram draws
"Computing…" at its size, and the line beside the title says so; a superseded request leaves
the page blank until the latest result; a read of the results waits for it."""
import gc
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]

_ROOT = Path(__file__).resolve().parent.parent


def _app():
    return QApplication.instance() or QApplication([])


def _workers():
    from swi3s_studio.ui.latest_job import THREAD_NAME
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


@pytest.fixture(autouse=True)
def _no_worker_left():
    """No test leaves a worker running into the next."""
    yield
    for t in _workers():
        t.join(60)
    assert not _workers()


def _job():
    """A LatestJob on a fresh parent, and the list of results it applied."""
    from swi3s_studio.ui.latest_job import LatestJob
    _app()
    parent = QObject()
    applied = []
    return parent, LatestJob(parent, applied.append), applied


# ---- LatestJob -------------------------------------------------------------------------


def test_a_request_superseded_before_it_starts_never_runs():
    parent, job, applied = _job()
    gate, ran = threading.Event(), []

    def blocking(_cancelled):
        ran.append("A")
        assert gate.wait(30)
        return "A"

    first = job.submit(blocking)
    job.submit(lambda _c: ran.append("B") or "B")
    last = job.submit(lambda _c: ran.append("C") or "C")
    assert last == first + 2 == job.generation and job.busy
    gate.set()
    job.wait()
    assert ran == ["A", "C"] and applied == ["C"] and not job.busy


def test_a_result_collected_after_a_newer_submit_is_dropped():
    parent, job, applied = _job()
    job.submit(lambda _c: "old")
    job._running.thread.join()          # finished, not yet collected
    job.submit(lambda _c: "new")
    job.wait()
    assert applied == ["new"]


def test_a_worker_s_exception_is_raised_on_the_ui_thread():
    parent, job, applied = _job()

    def failing(_cancelled):
        raise ZeroDivisionError("in the worker")

    job.submit(failing)
    with pytest.raises(ZeroDivisionError, match="in the worker"):
        job.wait()
    assert applied == [] and not job.busy


def test_a_worker_s_exception_is_handed_to_fail_before_it_is_raised():
    from swi3s_studio.ui.latest_job import LatestJob
    _app()
    parent, failed = QObject(), []
    job = LatestJob(parent, failed.append, lambda exc: failed.append(("fail", str(exc))))

    def failing(_cancelled):
        raise ZeroDivisionError("in the worker")

    job.submit(failing)
    with pytest.raises(ZeroDivisionError):
        job.wait()
    assert failed == [("fail", "in the worker")]


def test_a_failed_recompute_shows_no_margins_and_names_the_error(monkeypatch):
    """A recompute that raises leaves no blank page: the tree is empty, the diagram is not
    pending, the advisory line names the error, and the exception still propagates."""
    from swi3s_studio.ui import timing_view
    tv = _tv()

    def broken(base, rows):
        raise RuntimeError("broken search")     # not a refusal (_UNEVALUABLE)
    monkeypatch.setattr(timing_view, "_worst_at", broken)
    tv._fclk.setValue(10.0)              # inputs no earlier search saw
    tv._recompute()
    assert tv._status.text() == timing_view._COMPUTING
    with pytest.raises(RuntimeError, match="broken search"):
        tv._job.wait()
    assert tv._status.text() == "" and not tv._diagram.pending
    assert tv._tree.topLevelItemCount() == 0
    assert "The margin search failed (RuntimeError: broken search)." in (
        tv._ceiling_label.text())


def test_the_poll_timer_applies_a_finished_result_and_stops():
    parent, job, applied = _job()
    job.submit(lambda _c: threading.current_thread())
    deadline = time.monotonic() + 30
    while not applied and time.monotonic() < deadline:
        QTest.qWait(10)
    assert applied and applied[0] is not threading.main_thread()
    assert not job._timer.isActive() and not job.busy


def test_stop_cancels_and_joins_the_worker_and_applies_nothing():
    parent, job, applied = _job()
    started = threading.Event()

    def until_cancelled(cancelled):
        started.set()
        while not cancelled():
            time.sleep(0.001)
        return "cancelled"

    job.submit(until_cancelled)
    assert started.wait(30)
    assert job.stop() is True
    assert not _workers() and not job.busy
    QTest.qWait(3 * 15)
    job.wait()
    assert applied == []
    assert job.stop() is False, "nothing left to drop"


def test_no_garbage_collection_runs_on_a_worker():
    """A worker that allocates cycles enough for many collections: none runs on the worker,
    collection is off while it runs and back on once it is collected."""
    from swi3s_studio.ui.latest_job import THREAD_NAME
    parent, job, applied = _job()
    assert gc.isenabled()
    collected_on = []

    def note(phase, _info):
        if phase == "start":
            collected_on.append(threading.current_thread().name)

    def cycles(_cancelled):
        for _ in range(200_000):
            a = []
            a.append(a)
        return "done"

    gc.callbacks.append(note)
    try:
        job.submit(cycles)
        assert not gc.isenabled()
        job.wait()
    finally:
        gc.callbacks.remove(note)
    assert applied == ["done"] and gc.isenabled()
    assert THREAD_NAME not in collected_on


# ---- the Timing page -------------------------------------------------------------------


def _tv():
    _app()
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.flush_recompute()
    return tv


def _rates(tv):
    """The F_CLK target of every result the page shows."""
    return {r.inputs.F_CLK_target_MHz for side in tv._worst.values() for r in side.values()}


def _gate_columns(mp, block_first: float, search: bool = True):
    """Patch the page's per-column corner search: record each call's F_CLK and thread. The
    first call is held for up to `block_first` s or until the returned event is set, and
    returns a placeholder no page can show, so the recompute goes on to its next column
    unless it was cancelled. Later calls run the real search, or with `search` False refuse
    their input, which costs no search."""
    from swi3s_studio.ui import timing_view
    real = timing_view._worst_at
    gate, started, seen = threading.Event(), threading.Event(), []

    def gated(base, rows):
        seen.append((base.F_CLK_target_MHz, threading.current_thread()))
        first = len(seen) == 1
        if first:
            started.set()
            gate.wait(block_first)
            return {}, {}
        if not search:
            raise ZeroDivisionError("refused")
        return real(base, rows)

    mp.setattr(timing_view, "_worst_at", gated)
    return gate, started, seen


def test_the_corner_search_runs_off_the_ui_thread_and_the_event_loop_keeps_running(monkeypatch):
    """A real recompute (the shared memo off): the first column's search is held on the worker
    until the UI thread has run its event loop through several timer ticks, so `_recompute`
    returns with the search not yet done and the loop turns while it is. Nothing depends on
    how long the search or the machine takes."""
    from swi3s_studio.ui import timing_view
    tv = _tv()
    tv.show()
    QTest.qWait(50)                  # the page's first layout, before the recompute starts
    threads, held = [], []
    ticks, enough = [], threading.Event()
    real = timing_view._worst_at

    def search(base, rows):
        threads.append(threading.current_thread())
        if len(threads) == 1:        # a search run on the UI thread waits here out of turns
            held.append(enough.wait(15))
        return real(base, rows)

    def tick():
        ticks.append(time.perf_counter())
        if len(ticks) >= 5:
            enough.set()

    monkeypatch.setattr(timing_view, "_worst_at", search)
    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(tick)
    ticker.start()
    tv._fclk.setValue(10.0)
    tv._recompute()
    assert not enough.is_set() and tv._job.busy, "the recompute was done when it returned"
    deadline = time.monotonic() + 120
    while tv._job.busy and time.monotonic() < deadline:
        QTest.qWait(10)
    ticker.stop()
    assert not tv._job.busy and _rates(tv) == {10.0}
    assert held == [True], "the search was not held until the event loop had ticked"
    assert len(threads) == 2 and threading.main_thread() not in threads
    tv.close()


def test_a_stale_result_never_overwrites_a_newer_one():
    """A recompute superseded while its first column is searched stops there, and only the
    newer one's result is shown."""
    tv = _tv()
    applied = []
    real_apply = tv._job._apply
    tv._job._apply = lambda result: (applied.append(result), real_apply(result))[1]
    with pytest.MonkeyPatch.context() as mp:
        gate, started, seen = _gate_columns(mp, 30)
        tv._fclk.setValue(10.0)
        tv._recompute()
        assert started.wait(30)
        older = tv._job.generation
        tv._fclk.setValue(11.0)
        tv._recompute()
        assert tv._job.generation == older + 1
        gate.set()
        tv.flush_recompute()
    assert len(applied) == 1 and _rates(tv) == {11.0}
    assert [f for f, _t in seen] == [10.0, 11.0, 11.0], "the stale recompute ran on"


def test_closing_the_page_during_a_recompute_joins_the_worker_and_shows_nothing_stale():
    """Close joins the worker, which stops after the column it is on; nothing is applied to
    the closed page, and showing it again recomputes."""
    tv = _tv()
    tv.show()
    before = _rates(tv)
    with pytest.MonkeyPatch.context() as mp:
        gate, started, seen = _gate_columns(mp, 0.2)
        tv._fclk.setValue(10.0)
        tv._recompute()
        assert started.wait(30)
        tv.close()
        assert not _workers() and not tv._job.busy
        assert len(seen) == 1, "the cancelled recompute searched its second column"
    QTest.qWait(100)
    assert before != {10.0} and _rates(tv) == set(), "the closed page holds no results"
    tv.show()
    assert tv._job.busy
    tv.flush_recompute()
    assert _rates(tv) == {10.0}
    tv.close()


def test_deleting_the_page_during_a_recompute_is_clean():
    """A page deleted without a close, as a parent's deletion does: the poll timer goes with it,
    so nothing is delivered, and the worker, holding no Qt object, stops after the column it
    is on."""
    from PySide6.QtCore import QEvent
    from shiboken6 import Shiboken
    tv = _tv()
    with pytest.MonkeyPatch.context() as mp:
        gate, started, seen = _gate_columns(mp, 0.2, search=False)
        tv._fclk.setValue(10.0)
        tv._recompute()
        assert started.wait(30)
        poll = tv._job._timer
        tv.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not Shiboken.isValid(poll)
        for t in _workers():
            t.join(60)
    assert len(seen) == 1, "the recompute of a deleted page searched its second column"
    QTest.qWait(200)
    assert gc.isenabled(), "collection stays off after the deleted page's worker ended"


class _Held:
    """The page's per-column corner search, each call held until the test releases a permit,
    then run for real; `started` holds each call's F_CLK target."""

    def __init__(self, mp):
        from swi3s_studio.ui import timing_view
        self._real = timing_view._worst_at
        self.permits = threading.Semaphore(0)
        self.started = []
        self._cv = threading.Condition()
        mp.setattr(timing_view, "_worst_at", self._search)

    def _search(self, base, rows):
        with self._cv:
            self.started.append(base.F_CLK_target_MHz)
            self._cv.notify_all()
        assert self.permits.acquire(timeout=60)
        return self._real(base, rows)

    def wait_started(self, n):
        """Until `n` calls have started, turning the event loop (which starts a waiting
        request)."""
        deadline = time.monotonic() + 30
        while len(self.started) < n:
            assert time.monotonic() < deadline, self.started
            QTest.qWait(5)


def _until_idle(tv):
    deadline = time.monotonic() + 120
    while tv._job.busy:
        assert time.monotonic() < deadline
        QTest.qWait(5)
    QTest.qWait(3 * 15)              # the poll that collects the result


def _cells(tv):
    """Every tree row's (text with tags stripped, the F_max a substitution row shows, the
    column label of a substitution row or None)."""
    import re

    from swi3s_studio.ui.timing_view import _FMAX_ROLE, _SUBST_LABEL_ROLE
    out, stack = [], [tv._tree.topLevelItem(i) for i in range(tv._tree.topLevelItemCount())]
    while stack:
        item = stack.pop(0)
        stack[:0] = [item.child(i) for i in range(item.childCount())]
        out.append((re.sub(r"<[^>]+>", "", item.text(0)), item.data(0, _FMAX_ROLE) or "",
                    item.data(0, _SUBST_LABEL_ROLE)))
    return out


def _assert_blank(tv, before):
    """Every value `before` showed is the placeholder now, with the rows kept: each
    substitution row's numbers, its margin and the F_max where it showed one."""
    from swi3s_studio.ui.timing_view import _COMPUTING, _PENDING
    now = _cells(tv)
    assert len(now) == len(before)
    for (t0, f0, lab0), (t1, f1, lab1) in zip(before, now):
        assert (f1, lab1) == (f0 and _PENDING, lab0), (t0, t1)
        tail = f" (FMAX {_PENDING})" if f0 else ""
        assert t1 == (f"{lab0}:{_PENDING} = {_PENDING}{tail}" if lab0 else t0), (t0, t1)
    assert any(f0 for _t, f0, _lab in before), "some substitution row shows an F_max"
    assert sum(1 for row in now if row[2]) > 30, "the tree has substitution rows"
    assert tv._worst == {} and tv._rendered is None
    assert tv._status.text() == _COMPUTING
    assert "~" not in tv._launch_combo.toolTip()


def _clocked_view_with_a_term_drawn():
    from swi3s_studio.timing.spec_source import LaunchMode
    tv = _tv()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK4.value))
    tv.resize(1800, 1100)
    tv.show()
    tv.flush_recompute()
    QApplication.processEvents()
    leg = "MP_setup"
    key = "Per_t_IS"
    tv._on_term_clicked(key, leg)
    assert tv._diagram_term == key and tv._diagram.panels
    assert "viable to ~" in tv._launch_combo.toolTip()
    return tv


def _fresh_cells(tv):
    """The tree a page computing `tv`'s inputs afresh shows."""
    from swi3s_studio.ui.timing_view import TimingView
    other = TimingView()
    other.set_from_dict(tv.to_dict())
    other.flush_recompute()
    cells = _cells(other)
    other.close()
    return cells


def test_the_page_is_blank_while_a_recompute_runs_and_filled_when_it_is_in():
    """A discrete change starts a recompute: until its result is in, every substitution number
    and margin and every Max Frequency is the placeholder, the tree and the diagram keep their
    size, the diagram draws no panel, and the status says it is computing. Its result fills
    them with the new inputs' values; nothing of the earlier result shows at any point."""
    tv = _clocked_view_with_a_term_drawn()
    before = _cells(tv)
    size, diagram_h = tv._tree.size(), tv._diagram.minimumHeight()
    panels = tv._diagram.panels
    painted = []
    real_paint = tv._diagram._paint_columns
    tv._diagram._paint_columns = lambda *a: (painted.append(1), real_paint(*a))[1]
    with pytest.MonkeyPatch.context() as mp:
        held = _Held(mp)
        tv._ho_ui_check.toggle()
        held.wait_started(1)
        _assert_blank(tv, before)
        assert tv._tree.size() == size
        assert tv._diagram.pending and tv._diagram.panels == panels
        assert tv._diagram.minimumHeight() == diagram_h and tv._diagram_pane.isVisible()
        tv._diagram.repaint()
        assert painted == [], "the diagram drew a panel from the replaced corner"
        QTest.qWait(100)
        _assert_blank(tv, before)
        held.permits.release(2)
        _until_idle(tv)
    after = _cells(tv)
    assert after != before and after == _fresh_cells(tv)
    assert all("…" not in c for row in after for c in row[:2])
    assert tv._status.text() == "" and not tv._diagram.pending and tv._worst
    assert "viable to ~" in tv._launch_combo.toolTip()
    tv._diagram.repaint()
    assert painted
    tv.close()


def test_a_superseded_recompute_leaves_the_page_blank_until_the_latest_result():
    tv = _clocked_view_with_a_term_drawn()
    before = _cells(tv)
    with pytest.MonkeyPatch.context() as mp:
        held = _Held(mp)
        tv._fclk.setValue(10.0)
        tv._recompute()
        held.wait_started(1)
        tv._fclk.setValue(11.0)
        tv._recompute()
        held.permits.release(1)          # the first runs its column, then stops: superseded
        held.wait_started(2)             # its result is collected and dropped; the latest runs
        assert held.started == [10.0, 11.0]
        QTest.qWait(100)
        _assert_blank(tv, before)
        held.permits.release(2)
        _until_idle(tv)
    assert _rates(tv) == {11.0} and _cells(tv) == _fresh_cells(tv)
    tv.close()


def test_a_retheme_while_blank_keeps_the_page_blank():
    from swi3s_studio.ui.theme import VizTheme, apply_palette
    from swi3s_studio.ui.timing_view import _PENDING
    tv = _clocked_view_with_a_term_drawn()
    before = _cells(tv)
    mode = VizTheme.MODE
    try:
        with pytest.MonkeyPatch.context() as mp:
            held = _Held(mp)
            tv._ho_ui_check.toggle()
            held.wait_started(1)
            apply_palette("light" if mode == "dark" else "dark")
            tv.retheme()
            _assert_blank(tv, before)
            from swi3s_studio.ui.timing_view import _FMAX_ROLE
            item = next(it for it in (tv._tree.topLevelItem(0).child(c)
                                      for c in range(tv._tree.topLevelItem(0).childCount()))
                        if it.data(0, _FMAX_ROLE))
            assert (f'<span style="color:{VizTheme.TEXT_DIM}">{_PENDING} = {_PENDING} '
                    f'(F<sub>MAX</sub> {_PENDING})</span>') in item.text(0), item.text(0)
            held.permits.release(2)
            _until_idle(tv)
    finally:
        apply_palette(mode)
    assert _cells(tv) == _fresh_cells(tv)
    tv.close()


@pytest.mark.parametrize("reader", ["summary", "term click"])
def test_a_read_during_a_recompute_waits_for_its_result(reader):
    """`summary_text` and a click on a term wait for the running recompute and read its
    result, never the placeholder."""
    tv = _clocked_view_with_a_term_drawn()
    with pytest.MonkeyPatch.context() as mp:
        held = _Held(mp)
        tv._ho_ui_check.toggle()
        held.wait_started(1)
        release = threading.Timer(0.3, held.permits.release, args=(2,))
        release.start()
        if reader == "summary":
            text = tv.summary_text()
            assert "…" not in text
        else:
            tv._on_term_clicked("Man_t_DD", "MP_setup")
            assert tv._diagram_term == "Man_t_DD" and not tv._diagram.pending
        release.join()
    assert not tv._job.busy and tv._status.text() == ""
    assert _cells(tv) == _fresh_cells(tv)
    if reader == "summary":
        fresh = _tv()
        fresh.set_from_dict(tv.to_dict())
        assert text == fresh.summary_text()
        fresh.close()
    tv.close()


@pytest.mark.platform
def test_an_interpreter_exiting_with_a_recompute_in_flight_exits_cleanly():
    """One page closed while its recompute runs, one never shown and still computing when the
    event loop quits and the interpreter exits: status 0, no abort. The corner search is replaced by a sleep
    (0.3 s for the first page, 3 s for the second) that then refuses the input."""
    script = textwrap.dedent("""
        import os, sys, threading, time
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        from swi3s_studio.ui import timing_view
        calls, started = [], threading.Event()
        def held(base, rows):
            calls.append(None)
            first = len(calls) == 1
            started.set()
            time.sleep(0.3 if first else 3.0)
            raise ZeroDivisionError
        timing_view._worst_at = held
        closed = timing_view.TimingView()
        assert started.wait(30)
        left = timing_view.TimingView()
        closed.show()                     # quitting closes a shown window, so `left` is not
        QTimer.singleShot(20, closed.close)
        QTimer.singleShot(40, app.quit)
        rc = app.exec()
        assert not closed._job.busy and left._job.busy
        assert left._job._running.cancel.is_set(), "quitting drops the page's requests"
        assert any(t.name == "latest-job" for t in threading.enumerate())
        print("EXITING", flush=True)
        sys.exit(rc)
    """)
    path = os.pathsep.join(p for p in (str(_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    proc = subprocess.run([sys.executable, "-c", script], cwd=_ROOT, capture_output=True,
                          text=True, timeout=300, env=dict(os.environ, PYTHONPATH=path))
    assert "EXITING" in proc.stdout, proc.stderr[-3000:]
    assert proc.returncode == 0, proc.stderr[-3000:]
