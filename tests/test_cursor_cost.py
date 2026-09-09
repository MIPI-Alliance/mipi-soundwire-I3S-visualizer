"""What a cursor move is ALLOWED TO DO — asserted as counts, not as wall-clock times.

Every user-visible stall this project has shipped was a per-cursor-move cost that grew
with the capture, and the perf suite kept missing them because it times FUNCTIONS in
isolation. Two things follow from that, and this file is both of them:

  * A ceiling on a function cannot express "and not on the GUI thread". A 1.5 s bound is
    right for a worker and ruinous for a cursor move; `test_tx_persist_columns_ceiling`
    allowed 30 M UIs at 1.5 s and stayed green while a 4.7 s capture froze for ~550 ms per
    move, because the defect was WHICH THREAD the call landed on, not how fast it was.
  * Wall clocks have to be generous enough to survive a loaded runner, so they catch
    cliffs and never drift. A 2-3x regression passes every ceiling in test_perf.

So the assertions here are COUNTS: rows built, calls made, thread used. They are
deterministic, they are the same on a busy CI runner as on a workstation, and they fail on
drift rather than only on a cliff. `tests/test_perf.py` keeps the wall-clock budget for the
whole cascade; this file pins the shape that budget depends on.

The model is test_gui_smoke.test_cursor_cascade_coalesces_when_event_loop_live, which
counts heavy rebuilds rather than timing them.
"""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import _SAMPLE_HALF_WINDOW, _TX_PERSIST_SYNC_MAX_UIS, MainWindow

_app = QApplication.instance() or QApplication([])


def _win(rows=600):
    w = MainWindow()
    w.load_session(Session.from_demo(rows, cold_start=True, register_map=w._rmap))
    return w


# --------------------------------------------------------------- the Samples pane

def test_a_cursor_move_inside_the_loaded_window_builds_no_rows():
    """Moving the cursor within the Samples pane's loaded window must be a SELECTION
    change and nothing more.

    The pane is a QTableWidget refilled wholesale, so a rebuild costs
    2 * _SAMPLE_HALF_WINDOW * len(_COLS) fresh QTableWidgetItems -- 48,000 at the original
    half-window of 4000, profiled at ~99 ms of a 142 ms cursor move. The whole design
    depends on the in-window move being free (_refresh_samples_around's cheap path), and
    nothing asserted that it was. Counting _fill_row is exact: it is the only place the
    pane builds a row, and it builds len(_COLS) items each time.
    """
    w = _win()
    sv = w._sample_view
    w._sample_dock.show()
    mid = int(w._session.capture.clock_edges[len(w._session.capture.clock_edges) // 2])
    w._refresh_samples_around(mid, force=True)

    span = sv.shown_span()
    assert span is not None and span[1] > span[0], "pane loaded nothing — nothing to prove"

    built = [0]
    orig = sv._fill_row

    def counted(r, s):
        built[0] += 1
        orig(r, s)

    sv._fill_row = counted
    # Several moves that all land INSIDE the loaded window.
    lo, hi = span
    for f in (0.25, 0.5, 0.75, 0.4):
        w._refresh_samples_around(int(lo + (hi - lo) * f))
    assert built[0] == 0, (
        f"{built[0]} rows rebuilt for cursor moves inside the loaded window — the cheap "
        "path in _refresh_samples_around is not being taken, so every move now pays a "
        "full wholesale refill")


def test_a_cursor_move_outside_the_window_builds_at_most_one_window():
    """Leaving the window costs ONE re-centre, bounded by the window size.

    This is the count that the pane's cost reduces to, and it is what makes the
    _SAMPLE_HALF_WINDOW constant load-bearing: the rebuild is O(window), so the bound has
    to be stated in terms of the window rather than the capture. A regression that rebuilt
    per pane, or that loaded the whole capture, shows up here as a multiple.
    """
    w = _win()
    sv = w._sample_view
    w._sample_dock.show()
    ce = w._session.capture.clock_edges
    w._refresh_samples_around(int(ce[len(ce) // 2]), force=True)

    built = [0]
    orig = sv._fill_row

    def counted(r, s):
        built[0] += 1
        orig(r, s)

    sv._fill_row = counted
    w._refresh_samples_around(int(ce[1]))          # far outside: forces a re-centre
    assert built[0] > 0, "no rows built at all — the move did not leave the window"
    assert built[0] <= 2 * _SAMPLE_HALF_WINDOW, (
        f"{built[0]} rows built for ONE cursor move, but the window holds at most "
        f"{2 * _SAMPLE_HALF_WINDOW} — the pane is rebuilding more than once, or the "
        "window is no longer bounded")


# ------------------------------------------------- what runs on the GUI thread

def _big_region_session(w, ui_count):
    """A demo session re-pointed at a capture with ONE config region of `ui_count` UIs.

    Same swap the perf suite uses (tx_persist_columns reads only `capture` and `segments`),
    which is what makes it possible to test the region-size routing without decoding a
    multi-second capture.

    `_link_control` is reset because it is cached from the ORIGINAL capture: left in place,
    the demo's cold-start bring-up keeps _apply_grid_for_sample on its "No PHY Selected"
    branch and the TX-map code below never runs, so the routing assertions would pass
    without testing anything. The synthetic capture has no bring-up, which is what a
    mid-stream capture looks like.
    """
    s = w._session
    ce = (np.arange(1, ui_count + 1, dtype=np.int64) * 4).astype(np.uint64)
    de = (np.arange(0, ui_count, 500, dtype=np.int64) * 4 + 2).astype(np.uint64)
    s.capture = type(s.capture)(clock_edges=ce, data_edges=de, initial_clock=False,
                                initial_data=False, sample_rate_hz=500_000_000)
    s.segments = [{"start_ui": 1, "column_count": 8, "start_sample": int(ce[2]),
                   "end_sample": int(ce[-1]), "row_base": 0}]
    s._tx_persist_cache = {}
    s._link_control = None
    assert s.link_control.sequence != "cold" or s.audio_start_sample == 0, (
        "the swapped capture still looks like a cold-start bring-up, so the TX-map branch "
        "is unreachable and these tests would be hollow")
    return s


def _record_tx_routing(w, s, monkeypatch):
    """(inline_calls, worker_calls) recorders for the persistence routing decision."""
    inline: list = []
    worker: list = []
    monkeypatch.setattr(s, "tx_persist_columns",
                        lambda sample=None: inline.append(threading.current_thread())
                        or (np.zeros(8, dtype=bool), 8))
    monkeypatch.setattr(w, "_start_tx_persist_worker",
                        lambda *a, **k: worker.append(True))
    return inline, worker


def test_a_big_region_persistence_scan_never_runs_on_the_gui_thread(monkeypatch):
    """A config region above _TX_PERSIST_SYNC_MAX_UIS must be routed to the worker.

    This is the invariant no wall-clock ceiling can express. The scan runs at ~32 ns/UI, so
    the constant IS the freeze a cursor move may cause -- and it was set to 32 M UIs under a
    comment claiming "sub-~0.3s", which admitted ~1.0 s. The cruel part was that it bit
    SHORT captures: a 4.7 s capture's single 18.9 M-UI region sat UNDER the threshold and
    therefore ran inline, while the long captures the constant was written for were routed
    away correctly all along. Measured 530 ms median per cursor move against 28 ms with
    persistence off.

    Asserting on the ROUTING rather than on elapsed time makes this deterministic: no
    threads have to be scheduled and no clock has to be read.
    """
    w = _win()
    w._tx_map = True
    w._tx_persist = True
    s = _big_region_session(w, _TX_PERSIST_SYNC_MAX_UIS + 100_000)
    inline, worker = _record_tx_routing(w, s, monkeypatch)

    w._apply_grid_for_sample(int(s.capture.clock_edges[100]))

    assert not inline, (
        f"a region of {_TX_PERSIST_SYNC_MAX_UIS + 100_000:,} UIs was scanned INLINE on the "
        f"GUI thread ({inline}) — at ~32 ns/UI that is the cursor-move freeze users report. "
        "It belongs on _start_tx_persist_worker.")
    assert worker, "the big region went to neither the inline path nor the worker"


def test_a_small_region_persistence_scan_still_runs_inline(monkeypatch):
    """The other half of the routing rule, so the fix can't be "send everything async".

    The inline path exists deliberately: for a small region the thread hand-off and the
    async round-trip cost more than the scan, and keeping it synchronous is what makes the
    demo and short captures respond immediately. A test that only pinned the async side
    would be satisfied by deleting the inline branch.
    """
    w = _win()
    w._tx_map = True
    w._tx_persist = True
    s = _big_region_session(w, max(1000, _TX_PERSIST_SYNC_MAX_UIS // 10))
    inline, worker = _record_tx_routing(w, s, monkeypatch)

    w._apply_grid_for_sample(int(s.capture.clock_edges[100]))

    assert inline, "a small region was pushed to a worker thread — the inline path is gone"
    assert not worker, f"a small region also started a worker ({worker})"
    assert inline[0] is threading.main_thread(), (
        "the inline path ran off the main thread, so 'inline' no longer means what the "
        "routing decision assumes")


def test_the_inline_threshold_is_stated_as_a_gui_budget(monkeypatch):
    """The routing constant must stay small enough to BE an inline budget.

    Paired with test_perf.test_tx_persist_inline_threshold_stays_inline, which measures the
    scan at exactly this size. This half needs no clock: it fails if the constant is raised
    past what a cursor move can absorb, which is the change that caused the freeze. 4 M UIs
    is ~130 ms at the measured rate -- already the outer edge of tolerable.
    """
    assert _TX_PERSIST_SYNC_MAX_UIS <= 4_000_000, (
        f"_TX_PERSIST_SYNC_MAX_UIS is {_TX_PERSIST_SYNC_MAX_UIS:,}. The scan measures "
        "~32 ns/UI, so this is a GUI-thread freeze of "
        f"~{_TX_PERSIST_SYNC_MAX_UIS * 32e-9 * 1000:.0f} ms on every first cursor move into "
        "a region. Route bigger regions to _start_tx_persist_worker instead of raising it.")


def test_the_cursor_cascade_skips_hidden_docks(monkeypatch):
    """A hidden pane must cost NOTHING on a cursor move.

    _apply_cursor_heavy's per-dock guards are the reason hiding Decoded Samples took a
    reported cursor move from 142 ms to 43 ms, which is how the pane was identified in the
    first place. That makes the guards a performance contract, and it was untested: a pane
    that stopped checking its own dock would reintroduce the cost invisibly for anyone who
    had closed the tab.
    """
    w = _win()
    s = w._session
    ce = s.capture.clock_edges
    for dock in (w._sample_dock, w._symbol_dock, w._reg_dock):
        dock.hide()
    _app.processEvents()

    touched: list = []
    monkeypatch.setattr(w, "_refresh_samples_around",
                        lambda *a, **k: touched.append("samples"))
    monkeypatch.setattr(w, "_refresh_symbols_around",
                        lambda *a, **k: touched.append("symbols"))
    monkeypatch.setattr(s, "register_files_at",
                        lambda *a, **k: touched.append("registers") or [])

    w._pending_cursor = (int(ce[len(ce) // 2]), False, False, False)
    w._apply_cursor_heavy()

    assert not touched, (
        f"a cursor move rebuilt {touched} while those docks were hidden — the per-dock "
        "guards in _apply_cursor_heavy are the reason closing a tab restores "
        "responsiveness")
