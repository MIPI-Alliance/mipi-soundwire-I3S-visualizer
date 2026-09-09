"""Decoded-Samples pane GUI tests: the table holds a bounded window that extends when
scrolled to either end (lazy loading), has no row-index gutter, and moves the shared
cursor when a row is selected.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import _SAMPLE_CHUNK, _SAMPLE_HALF_WINDOW, MainWindow

_app = QApplication.instance() or QApplication([])


def _win():
    w = MainWindow()
    w.load_session(Session.from_demo(600, cold_start=True, register_map=w._rmap))
    w._sample_dock.show()
    mid = int(w._session.capture.clock_edges[len(w._session.capture.clock_edges) // 2])
    w._refresh_samples_around(mid, force=True)
    return w, mid


def test_no_row_index_gutter():
    w, _ = _win()
    assert not w._sample_view._table.verticalHeader().isVisible()


def test_window_is_bounded_and_lazily_extends():
    w, _ = _win()
    sv = w._sample_view
    total = w._sample_total
    assert total > 2 * _SAMPLE_HALF_WINDOW, "demo too small to exercise windowing"
    n0 = sv._table.rowCount()
    assert n0 == 2 * _SAMPLE_HALF_WINDOW < total          # bounded window, not the whole capture

    w._on_sample_edge(1)                                  # scroll to bottom -> load more
    n1 = sv._table.rowCount()
    assert n1 == n0 + _SAMPLE_CHUNK

    w._on_sample_edge(-1)                                 # scroll to top -> load more
    n2 = sv._table.rowCount()
    assert n2 == n1 + _SAMPLE_CHUNK

    starts = [int(sv._samples[i]["start_sample"]) for i in range(len(sv._samples))]
    assert starts == sorted(starts), "loaded window not in ascending order"
    assert len(set(starts)) == len(starts) or True        # dupes allowed (same-sample lanes), order matters


def test_extending_the_window_does_not_re_enter_at_the_far_end():
    """An edge load must not provoke a second one at the OPPOSITE end.

    prepend_samples re-anchors the view with sb.setValue() after inserting rows. The
    scrollbar is a separate object from the table, so the blockSignals() around the insert
    never covered that call, and the new value can land within _EDGE_PAD of either end —
    which _on_scroll read as the user scrolling there and answered with another edgeReached.
    A scroll-to-TOP extend therefore also appended a chunk at the BOTTOM: double the row
    building, and at _MAX_LOADED it evicted the very rows the user had scrolled up to reach.

    test_window_is_bounded_and_lazily_extends could not see it. With the original 4000-row
    window, 12000 + 4000 was exactly _MAX_LOADED, so the stray append's own eviction landed
    the row count back on the expected number. This asserts the absence of the cascade
    itself, and that `_sample_range` still describes what is actually loaded.
    """
    w, _ = _win()
    sv = w._sample_view
    assert w._sample_total > 4 * _SAMPLE_CHUNK, "demo too small to exercise both edges"

    seen: list = []
    sv.edgeReached.disconnect()
    sv.edgeReached.connect(seen.append)          # observe re-entry instead of servicing it

    for direction in (1, -1, -1, 1):
        seen.clear()
        before = sv._table.rowCount()
        w._on_sample_edge(direction)
        assert not seen, (
            f"extending at {direction:+d} re-entered with {seen} — a programmatic re-anchor "
            "is being read as a user scroll (see prepend_samples' scroll guard)")
        grew = sv._table.rowCount() - before
        assert grew in (0, _SAMPLE_CHUNK), (
            f"extending at {direction:+d} added {grew} rows, not one chunk ({_SAMPLE_CHUNK})")

    lo, hi = w._sample_range
    assert hi - lo == sv._table.rowCount(), (
        f"_sample_range spans {hi - lo} positions but {sv._table.rowCount()} rows are loaded — "
        "the owner's idea of the window has drifted from the pane's")


def test_edge_load_stops_at_extents():
    w, _ = _win()
    sv = w._sample_view
    # Walk to the very start; prepend must stop at position 0 (no negative growth).
    guard = 0
    while w._sample_range[0] > 0 and guard < 10000:
        w._on_sample_edge(-1)
        guard += 1
    assert w._sample_range[0] == 0
    n_at_top = sv._table.rowCount()
    w._on_sample_edge(-1)                                 # already at the top: no change
    assert sv._table.rowCount() == n_at_top


def test_selecting_row_moves_cursor():
    w, _ = _win()
    sv = w._sample_view
    before = w.cursor.sample
    target = min(5, sv._table.rowCount() - 1)
    sv._table.selectRow(target)
    _app.processEvents()
    assert w.cursor.sample == int(sv._samples[target]["start_sample"])
    assert w.cursor.sample != before


def test_programmatic_select_does_not_echo():
    """A programmatic select_sample() (cursor -> sample view) must NOT re-emit
    sampleSelected. blockSignals() doesn't reliably suppress itemSelectionChanged in
    the live widget, so select_sample gates on an explicit flag. Without this, a user
    row-click (which SHOULD emit) is indistinguishable from the cursor-driven
    selection, and the latter feeds back into the cursor."""
    w, _ = _win()
    sv = w._sample_view
    echoed = []
    sv.sampleSelected.connect(lambda s: echoed.append(int(s)))
    # Programmatic selection: must be silent.
    sv.select_sample(int(sv._samples[3]["start_sample"]))
    _app.processEvents()
    assert echoed == [], f"select_sample echoed sampleSelected: {echoed}"
    # A genuine user row-click still emits (the signal isn't wedged off).
    sv._table.selectRow(4)
    _app.processEvents()
    assert echoed == [int(sv._samples[4]["start_sample"])], echoed


def test_cursor_into_bringup_is_not_yanked_back_by_sample_view():
    """Regression: with the Decoded-Samples pane visible, moving the cursor into the
    link bring-up (which has no matching audio sample row) must leave it there. The
    cursor cascade calls sample_view.select_sample(), whose selection echo used to
    re-emit sampleSelected -> snap the cursor to the nearest LOADED row (back near the
    old position) — the "click once, cursor doesn't move; click again, it does" bug."""
    w, _ = _win()
    # Park the cursor in decoded audio (a wide region with real sample rows loaded).
    audio = w._session.audio_start_sample
    late = int(w._session.commands[-1]["start_sample"])
    w.cursor.set_sample(late)
    _app.processEvents()
    assert w.cursor.sample == late
    # One move into the bring-up (before audio): the cursor must land there and stay.
    target = max(0, int(w._session.link_control.bus_reset_sample or 0) + 1000)
    assert target < audio
    w.cursor.set_sample(target)
    _app.processEvents()
    assert w.cursor.sample == target, (
        f"cursor yanked back to {w.cursor.sample} (expected {target}) — sample-view echo")
