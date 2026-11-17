"""Timeline zoom, pan, seek and bookmark drag, driven by real Qt input events.

In a stack of Links, zooming one Link's band has to show the same INSTANTS on every other
band, whatever its offset. Events go through QApplication.sendEvent so the dispatch is the
real one.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.timeline import TimelineRibbon

pytestmark = pytest.mark.gui

_TOTAL = 1_000_000


def _app():
    return QApplication.instance() or QApplication([])


def _wheel(widget, x, dy=0, dx=0, modifiers=Qt.NoModifier):
    pos = QPointF(x, widget.height() / 2)
    ev = QWheelEvent(pos, widget.mapToGlobal(pos), QPoint(0, 0), QPoint(dx, dy),
                     Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


def _mouse(widget, kind, x, button=Qt.LeftButton, buttons=None):
    pos = QPointF(x, widget.height() / 2)
    held = button if buttons is None else buttons
    if kind == QEvent.MouseButtonRelease and buttons is None:
        held = Qt.NoButton
    ev = QMouseEvent(kind, pos, widget.mapToGlobal(pos), button, held, Qt.NoModifier,
                     QPointingDevice.primaryPointingDevice())
    QApplication.sendEvent(widget, ev)


def _press(w, x, button=Qt.LeftButton):
    _mouse(w, QEvent.MouseButtonPress, x, button)


def _move(w, x, buttons):
    _mouse(w, QEvent.MouseMove, x, Qt.NoButton, buttons)


def _release(w, x, button=Qt.LeftButton):
    _mouse(w, QEvent.MouseButtonRelease, x, button)


@pytest.fixture
def ribbon():
    _app()
    r = TimelineRibbon()
    r.resize(1000, TimelineRibbon.BAND_HEIGHT)
    r.set_events([], _TOTAL)
    return r


def _view(r):
    return r._view_lo, r._view_hi


def test_a_wheel_zooms_in_about_the_pointer(ribbon):
    x = 700.0
    under = ribbon._sample_for(x)
    _wheel(ribbon, x, dy=240)                       # two notches in
    lo, hi = _view(ribbon)
    assert hi - lo == pytest.approx(_TOTAL * 0.9985 ** 240, rel=1e-6)   # proportional
    assert abs(ribbon._sample_for(x) - under) <= 1   # the sample under the pointer stays


def test_zooming_out_past_the_whole_capture_stops_at_it(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    for _ in range(10):
        _wheel(ribbon, 500.0, dy=-480)
    assert _view(ribbon) == (0.0, float(_TOTAL))


def test_zoom_in_stops_at_sixteen_samples(ribbon):
    for _ in range(60):
        _wheel(ribbon, 500.0, dy=1200)
    lo, hi = _view(ribbon)
    assert hi - lo == pytest.approx(16.0)


def test_a_horizontal_gesture_pans_by_a_fraction_of_the_view(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    lo, hi = _view(ribbon)
    _wheel(ribbon, 500.0, dx=-120)                   # one notch: ~15% of the span, later
    lo2, hi2 = _view(ribbon)
    assert hi2 - lo2 == pytest.approx(hi - lo)
    assert lo2 - lo == pytest.approx(0.15 * (hi - lo))


def test_shift_wheel_pans_like_a_horizontal_gesture(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    lo, hi = _view(ribbon)
    _wheel(ribbon, 500.0, dy=-120, modifiers=Qt.ShiftModifier)
    assert _view(ribbon)[0] - lo == pytest.approx(0.15 * (hi - lo))


def test_panning_into_an_edge_keeps_the_span(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    span = ribbon._view_span()
    for _ in range(50):
        _wheel(ribbon, 500.0, dx=-120)
    assert _view(ribbon)[1] == float(_TOTAL)
    assert ribbon._view_span() == pytest.approx(span)


def test_a_middle_drag_keeps_the_grabbed_sample_under_the_pointer(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    _wheel(ribbon, 500.0, dx=-240)                    # away from the edges, so none clamps
    grabbed = ribbon._sample_for(400.0)
    seeks = []
    ribbon.seeked.connect(seeks.append)
    _press(ribbon, 400.0, Qt.MiddleButton)
    for x in (420.0, 470.0, 530.0):
        _move(ribbon, x, Qt.MiddleButton)
    _release(ribbon, 530.0, Qt.MiddleButton)
    assert abs(ribbon._sample_for(530.0) - grabbed) <= 2
    assert seeks == []                                # a pan is not a seek


def test_a_click_seeks_to_the_sample_under_it(ribbon):
    seeks = []
    ribbon.seeked.connect(seeks.append)
    _press(ribbon, 300.0)
    _release(ribbon, 300.0)
    assert seeks == [ribbon._sample_for(300.0)]


def test_a_left_drag_coalesces_its_seeks_and_ends_on_the_release(ribbon):
    """Every seek drives the whole cursor cascade, so a drag must not emit per pixel —
    but it must end exactly where the pointer stopped."""
    seeks = []
    ribbon.seeked.connect(seeks.append)
    _press(ribbon, 100.0)
    for x in range(101, 400, 3):
        _move(ribbon, float(x), Qt.LeftButton)
    _release(ribbon, 399.0)
    assert len(seeks) <= 3                             # the press, maybe one tick, the end
    assert seeks[-1] == ribbon._sample_for(399.0)


def test_dragging_a_bookmark_moves_it_and_does_not_seek(ribbon):
    ribbon.set_bookmarks([(250_000, "A1")])
    x0 = ribbon._x_for(250_000)
    seeks, moves = [], []
    ribbon.seeked.connect(seeks.append)
    ribbon.bookmarkMoved.connect(lambda lbl, s, final: moves.append((lbl, s, final)))
    _press(ribbon, x0 + 3)                             # the line, not only the diamond
    _move(ribbon, x0 + 100, Qt.LeftButton)
    _release(ribbon, x0 + 150)
    assert seeks == []
    assert [m[2] for m in moves] == [False, True]      # live, then final
    assert moves[-1][:2] == ("A1", ribbon._sample_for(x0 + 150))


def test_a_double_click_shows_the_whole_capture(ribbon):
    _wheel(ribbon, 500.0, dy=480)
    _mouse(ribbon, QEvent.MouseButtonDblClick, 500.0)
    assert _view(ribbon) == (0.0, float(_TOTAL))


def test_a_user_zoom_announces_the_view_and_a_told_view_does_not(ribbon):
    """The stack listens to viewChanged and answers with set_view, so set_view must stay
    silent or two ribbons would ping-pong forever."""
    seen = []
    ribbon.viewChanged.connect(lambda lo, hi: seen.append((lo, hi)))
    _wheel(ribbon, 500.0, dy=240)
    assert seen == [_view(ribbon)]
    ribbon.set_view(1000.0, 5000.0)
    assert len(seen) == 1 and _view(ribbon) == (1000.0, 5000.0)


def test_a_cursor_outside_the_zoomed_view_brings_the_view_to_it(ribbon):
    ribbon.set_view(0.0, 10_000.0)
    ribbon.set_cursor(700_000)
    lo, hi = _view(ribbon)
    assert lo < 700_000 < hi and hi - lo == pytest.approx(10_000.0)


# ---- a stack of Links: one window, in each Link's own samples ----

def _two_link_window(offset_ps=0):
    from swi3s_studio.ui.main_window import MainWindow
    _app()
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    win.load_demo_links()
    win.links[1].offset_ps = offset_ps
    win._update_timeline_domain()
    QApplication.processEvents()
    return win


def _instant(win, i, sample):
    return win.links[i].to_ps(round(sample))


@pytest.mark.parametrize("offset_ps", [0, 3_000_000_000])       # 0 and +3 ms
def test_zooming_one_link_shows_the_same_instants_on_the_other(offset_ps):
    win = _two_link_window(offset_ps)
    r0, r1 = win._ribbons[:2]
    x = r0.width() * 0.4
    _wheel(r0, x, dy=720)
    period = 1e12 / win.links[0].session.sample_rate_hz
    for a, b in ((r0._view_lo, r1._view_lo), (r0._view_hi, r1._view_hi)):
        assert abs(_instant(win, 0, a) - _instant(win, 1, b)) <= period
    assert r1._view_hi - r1._view_lo < 0.5 * (r1._bounds()[1] - r1._bounds()[0])


def test_panning_the_second_link_moves_the_first():
    win = _two_link_window(1_000_000_000)
    r0, r1 = win._ribbons[:2]
    _wheel(r1, r1.width() / 2, dy=720)
    before = r0._view_lo
    _press(r1, 300.0, Qt.MiddleButton)
    _move(r1, 200.0, Qt.MiddleButton)
    _release(r1, 200.0, Qt.MiddleButton)
    assert r0._view_lo > before                        # dragged left: later instants
    period = 1e12 / win.links[0].session.sample_rate_hz
    assert abs(_instant(win, 0, r0._view_lo) - _instant(win, 1, r1._view_lo)) <= period


def test_a_zoom_moves_no_cursor_and_rebuilds_no_pane(monkeypatch):
    """Zoom and pan are view-only: counted, so a drift to 'refresh the panes on a zoom'
    fails here deterministically rather than as a few ms a wall clock cannot see."""
    win = _two_link_window()
    calls = []
    monkeypatch.setattr(win, "_on_cursor", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(win, "_on_seek", lambda *a, **k: calls.append(a))
    cursor = win.cursor.sample
    r0 = win._ribbons[0]
    _wheel(r0, 500.0, dy=480)
    _wheel(r0, 500.0, dx=-120)
    _press(r0, 300.0, Qt.MiddleButton)
    _move(r0, 250.0, Qt.MiddleButton)
    _release(r0, 250.0, Qt.MiddleButton)
    QApplication.processEvents()
    assert calls == [] and win.cursor.sample == cursor


def test_a_click_on_the_other_link_s_band_makes_it_active_at_that_sample():
    win = _two_link_window(2_000_000_000)                # Link 2 at +2 ms
    win.switch_link(0)
    r1 = win._ribbons[1]
    # An instant inside BOTH Links (Link 2 starts 2 ms in, so its band's left part is
    # before it and the right part may be after Link 1 ends): the middle of the overlap.
    first = win.links[1].to_ps(0)
    last = win.links[0].to_ps(win._session.last_sample())
    target = win.links[1].to_sample((first + last) // 2)
    x = r1._x_for(target)
    target = r1._sample_for(x)
    _press(r1, x)
    _release(r1, x)
    QApplication.processEvents()
    assert win.links.active_index == 1                   # the band clicked is now active
    assert abs(win.cursor.sample - target) <= 1          # at most a snap to the edge grid
    assert win._pane_link == {"commands": 0, "right": 0, "grid": 0, "bottom": 0}   # groups unmoved


def _commands():
    return [
        {"command": "Ping", "start_sample": 200_000, "bus_row": 4},
        {"command": "Ping", "start_sample": 400_000, "bus_row": 8, "has_manager_packet": True,
         "crc_valid": False},
        {"command": "ReadA32", "start_sample": 600_000, "bus_row": 12},
    ]


def test_hover_names_only_the_commands_whose_ticks_are_drawn(ribbon):
    """A command the draw filter hides has no tick, so the pointer beside it names nothing."""
    ribbon.set_events(_commands(), _TOTAL)
    ribbon.set_row_origin(0)
    ping_x = ribbon._x_for(200_000)
    assert ribbon._hover_text(ping_x).startswith("Ping")
    ribbon.set_draw_filter({400_000})                 # Errors Only: the CRC error stays
    assert ribbon._nearest(ping_x) is None
    assert ribbon._hover_text(ping_x) == ""
    assert ribbon._hover_text(ribbon._x_for(400_000)).startswith("Ping (CRC error)")
    ribbon.set_draw_filter(None)
    assert ribbon._hover_text(ping_x).startswith("Ping")


def _key(widget, key, modifiers=Qt.NoModifier):
    from PySide6.QtTest import QTest
    QTest.keyClick(widget, key, modifiers)
    if modifiers:
        QTest.keyRelease(widget, Qt.Key_Control)       # QTest keeps the modifier held


@pytest.fixture
def stepper(ribbon):
    ribbon.set_events(_commands(), _TOTAL)
    seen = []
    ribbon.seeked.connect(seen.append)
    return ribbon, seen


def test_arrow_keys_step_to_the_previous_and_next_command(stepper):
    r, seen = stepper
    r.set_cursor(0)
    _key(r, Qt.Key_Right)
    assert seen == [200_000]
    r.set_cursor(200_000)                              # a tie: the command under the cursor
    _key(r, Qt.Key_Right)                              # is passed, not re-selected
    r.set_cursor(400_000)
    _key(r, Qt.Key_Left)
    assert seen == [200_000, 400_000, 200_000]
    r.set_cursor(200_000)
    _key(r, Qt.Key_Left)                               # before the first command: stays
    r.set_cursor(600_000)
    _key(r, Qt.Key_Right)                              # after the last command: stays
    assert seen == [200_000, 400_000, 200_000]


def test_arrow_keys_follow_the_command_filter_when_one_is_set(stepper):
    r, seen = stepper
    r.set_nav_starts([200_000, 600_000])               # the table shows these two
    r.set_cursor(200_000)
    _key(r, Qt.Key_Right)
    assert seen == [600_000]
    r.set_nav_starts([])                               # a filter that shows nothing
    _key(r, Qt.Key_Left)
    assert seen == [600_000]
    r.set_nav_starts(None)                             # no filter: every command
    r.set_cursor(600_000)
    _key(r, Qt.Key_Left)
    assert seen == [600_000, 400_000]


def test_control_arrow_jumps_between_the_commit_commands(stepper):
    r, seen = stepper
    r.set_commit_starts([300_000, 500_000])
    r.set_cursor(0)
    _key(r, Qt.Key_Right, Qt.ControlModifier)
    r.set_cursor(300_000)
    _key(r, Qt.Key_Right, Qt.ControlModifier)
    r.set_cursor(500_000)
    _key(r, Qt.Key_Left, Qt.ControlModifier)
    assert seen == [300_000, 500_000, 300_000]
    _key(r, Qt.Key_Up, Qt.ControlModifier)             # not an arrow it handles
    r.set_commit_starts([])
    _key(r, Qt.Key_Right, Qt.ControlModifier)          # no commits: nothing to jump to
    assert seen == [300_000, 500_000, 300_000]


def test_an_arrow_step_recentres_a_zoomed_view_on_the_command(stepper):
    r, seen = stepper
    for _ in range(20):
        _wheel(r, 100.0, dy=240)                       # zoom in at the left edge
    r.set_cursor(0)
    _key(r, Qt.Key_Right)
    lo, hi = _view(r)
    assert seen == [200_000] and (lo + hi) / 2 == pytest.approx(200_000)


def test_hover_describes_a_commit_point_then_a_config_band(ribbon):
    ribbon.set_events(_commands(), _TOTAL)
    ribbon.set_sample_rate(1_000_000)
    ribbon.set_row_label(lambda s: int(s) // 1000)
    ribbon.set_segments([{"start_sample": 0, "column_count": 4, "row_base": 0}], _TOTAL)
    ribbon.set_commit_points([800_000])
    cmd = ribbon._hover_text(ribbon._x_for(600_000))
    assert cmd.startswith("ReadA32 · row ") and cmd.endswith("600,000.0 µs")
    assert ribbon._hover_text(ribbon._x_for(800_000)) == "Commit Sync Point · row 800 · 800,000.0 µs"
    assert ribbon._hover_text(ribbon._x_for(900_000)).startswith("Bus config: ")
    ribbon.set_segments([], _TOTAL)
    assert ribbon._hover_text(ribbon._x_for(900_000)) == ""
