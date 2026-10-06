"""Timeline zoom, pan, seek and bookmark drag, driven by real Qt input events.

The ribbon's wheel and mouse handlers had never run under a test (every test before this set
the view directly), and a stack of Links made them load-bearing: zooming
one Link's band has to show the same INSTANTS on every other band, whatever its offset.
Events go through QApplication.sendEvent so the dispatch is the real one.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.timeline import TimelineRibbon

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
