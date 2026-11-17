"""The window's cursors (bookmarks), through the window: the Cursors menu, the status each
toggle reports and its hit tolerance, Next / Previous wrapping over the active Link's
cursors only, Clear, a drag from a pane (snapped on release, the playhead following), the
rows the Cursors pane lists for a lone cursor and a pair, and nothing done with no capture.
Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def _win(load: bool = True):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _menu_item(win, text: str):
    return next(a for a in win._bookmarks_menu.actions() if a.text() == text)


def _spu(win) -> float:
    return win._session.sample_rate_hz / win._session.ui_rate_hz


def _starts(win, *idx) -> list:
    return [int(win._session.commands[i]["start_sample"]) for i in idx]


def test_the_cursors_menu_holds_toggle_next_previous_and_clear_with_their_keys():
    win = _win()
    got = [(a.text(), a.shortcut().toString()) for a in win._bookmarks_menu.actions()
           if not a.isSeparator()][:4]
    assert got == [("&Toggle Cursor at Playhead", "Ctrl+B"), ("&Next Cursor", "Ctrl+]"),
                   ("&Previous Cursor", "Ctrl+["), ("&Clear Cursors", "")]
    win.close()


def test_a_toggle_with_no_capture_does_nothing():
    win = _win(load=False)
    status = win._status.text()
    win.toggle_bookmark()
    assert len(win._bookmarks) == 0 and win._status.text() == status
    win.close()


def test_each_toggle_names_the_cursor_it_drops_and_the_count_it_leaves():
    win = _win()
    s1, s2 = _starts(win, 1, 3)
    win.cursor.set_sample(s1)
    _menu_item(win, "&Toggle Cursor at Playhead").trigger()
    assert win._status.text() == f"Cursor A1 @ {s1:,}"
    win.cursor.set_sample(s2)
    win.toggle_bookmark()
    assert win._status.text() == f"Cursor A2 @ {s2:,}"
    win.cursor.set_sample(s1)
    win.toggle_bookmark()
    assert win._status.text() == "1 cursor(s)"
    assert [b.display() for b in win._bookmarks] == ["A2"]
    win.close()


def test_a_toggle_within_half_a_ui_removes_the_cursor_and_one_beyond_adds_another():
    win = _win()
    (s1,) = _starts(win, 2)
    half = max(1, int(_spu(win) // 2))
    win.cursor.set_sample(s1)
    win.toggle_bookmark()
    win.cursor.set_sample(s1 + half + 1)
    win.toggle_bookmark()
    assert len(win._bookmarks) == 2
    win.cursor.set_sample(s1 + half + 1 + half)
    win.toggle_bookmark()
    assert [b.sample for b in win._bookmarks] == [s1]
    win.close()


def test_next_and_previous_wrap_over_the_cursors_in_sample_order():
    win = _win()
    a, b, c = _starts(win, 1, 3, 5)
    for s in (b, a, c):
        win.cursor.set_sample(s)
        win.toggle_bookmark()
    win.cursor.set_sample(a)
    seen = []
    for _ in range(4):
        _menu_item(win, "&Next Cursor").trigger()
        seen.append(win.cursor.sample)
    assert seen == [b, c, a, b]
    seen = []
    for _ in range(3):
        _menu_item(win, "&Previous Cursor").trigger()
        seen.append(win.cursor.sample)
    assert seen == [a, c, b]
    win.close()


def test_next_and_previous_with_no_cursor_leave_the_playhead():
    win = _win()
    (s,) = _starts(win, 4)
    win.cursor.set_sample(s)
    win.next_bookmark()
    win.prev_bookmark()
    assert win.cursor.sample == s
    win.close()


def test_clear_empties_every_pane_of_cursors():
    win = _win()
    for s in _starts(win, 1, 3, 5):
        win.cursor.set_sample(s)
        win.toggle_bookmark()
    assert win._pair_view.rowCount() == 4                 # A1, A2, A1-A2, B1
    _menu_item(win, "&Clear Cursors").trigger()
    assert len(win._bookmarks) == 0 and win._pair_view.rowCount() == 0
    assert win._timeline._bookmarks == [] and len(win._raw_view._bm_lines) == 0
    assert win._audio_view._bm_marks == []
    win.close()


def test_the_cursors_pane_lists_a_lone_cursor_and_then_the_pair_with_its_deltas():
    """A lone cursor is one row; a pair is its two rows and their delta, whose time is the
    samples between them at the capture's rate and whose UI and row counts are the
    session's."""
    from swi3s_studio.ui.main_window import _fmt_duration
    win = _win()
    sess = win._session
    s1, s2 = _starts(win, 1, 4)
    win.cursor.set_sample(s1)
    win.toggle_bookmark()
    rows = win._bookmark_measure_rows()
    assert [r[0] for r in rows] == ["A1"]
    assert rows[0][1] == f"{sess.ui_index(s1):,}"
    assert rows[0][2] == f"{sess.display_row(sess.bus_row_for_sample(s1)):,}"
    assert rows[0][3] == _fmt_duration(s1 / sess.sample_rate_hz)
    win.cursor.set_sample(s2)
    win.toggle_bookmark()
    rows = win._bookmark_measure_rows()
    assert [r[0] for r in rows] == ["A1", "A2", "A1-A2"]
    delta = rows[-1]
    assert delta[1] == f"{abs(sess.ui_index(s2) - sess.ui_index(s1)):,}"
    assert delta[2] == f"{abs(sess.bus_row_for_sample(s2) - sess.bus_row_for_sample(s1)):,}"
    assert delta[3] == _fmt_duration((s2 - s1) / sess.sample_rate_hz)
    assert delta[4] == win._bm_ps(win._bookmarks.items[0])
    assert win._pair_view.rowCount() == 3
    win.close()


def test_a_drag_moves_the_cursor_live_and_snaps_it_to_a_clock_edge_on_release():
    import numpy as np
    win = _win()
    (s1,) = _starts(win, 2)
    win.cursor.set_sample(s1)
    win.toggle_bookmark()
    edges = win._session.capture.clock_edges
    i = int(np.searchsorted(edges, s1)) + 3
    off_edge = int(edges[i]) + 1
    win._on_bookmark_moved("A1", off_edge, final=False)
    assert win._bookmarks.items[0].sample == off_edge
    assert win._timeline._bookmarks == [(off_edge, "A1")]
    playhead = win.cursor.sample
    assert playhead == s1                                 # a live drag leaves the playhead
    win._on_bookmark_moved("A1", off_edge, final=True)
    assert win._bookmarks.items[0].sample == int(edges[i])
    assert win.cursor.sample == int(edges[i])
    win._on_bookmark_moved("Z9", off_edge, final=True)    # no such cursor: nothing moves
    assert win._bookmarks.items[0].sample == int(edges[i])
    win.close()


def test_a_drag_in_the_capture_pane_moves_the_cursor():
    win = _win()
    (s1,) = _starts(win, 2)
    win.cursor.set_sample(s1)
    win.toggle_bookmark()
    win._raw_view.bookmarkMoved.emit("A1", s1 + 5, False)
    assert win._bookmarks.items[0].sample == s1 + 5
    win.close()
