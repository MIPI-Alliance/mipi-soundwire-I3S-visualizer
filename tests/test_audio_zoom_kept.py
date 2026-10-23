"""Redrawing the Audio pane's tracks leaves its zoom where the user put it.

A change that rebuilds the tracks (a channel drawn as a spectrogram or a waveform, a frame
size, a channel shown or hidden, colours, names, Filter & Gain) restores the zoom and only
redraws the cursor line; it does not follow a cursor outside the window. Following is for a
cursor that moves.

A track does not grow past its pane for a long title (pyqtgraph gives a title a minimum
width of its text), so the X-linked tracks all show the same stretch of the capture.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    QApplication.processEvents()
    return w


def _zoom_away_from_the_cursor(av):
    """Zoom to 40-45% of the capture, with the cursor at its start, outside the window."""
    t0, t1 = av.x_range_seconds()
    av.set_x_range_seconds(t0 + (t1 - t0) * 0.40, t0 + (t1 - t0) * 0.45)
    QApplication.processEvents()
    x0, x1 = av._link.getViewBox().viewRange()[0]
    assert not x0 <= av._cursor_sample <= x1, "the cursor is in the window: nothing to test"
    return av.x_range_seconds()


def _ranges(av):
    return [tuple(plot.getViewBox().viewRange()[0]) for plot, *_rest in av._plots]


_REBUILDS = {
    "spectrogram on": lambda av, k: av.set_spectrogram(k, True),
    "spectrogram frame size": lambda av, k: av.set_spectrogram_frame(k, 2048),
    "spectrogram channels restored": lambda av, k: av.set_spectrogram_channels({k}),
    "channel hidden": lambda av, k: av._checks[k].setChecked(False),
    "every channel shown": lambda av, k: av._set_all_channels(True),
    "colours": lambda av, k: av._recolor_streams(),
    "data-port names": lambda av, k: av.set_stream_names({k[:2]: "Mic L"}),
    "Filter & Gain": lambda av, k: av.refresh_processing(),
}


@pytest.mark.parametrize("change", sorted(_REBUILDS))
def test_a_rebuild_leaves_the_zoom_where_it_was(win, change):
    av = win._audio_view
    key = sorted(av._checks)[0]
    before = _zoom_away_from_the_cursor(av)
    _REBUILDS[change](av, key)
    QApplication.processEvents()
    assert av.x_range_seconds() == pytest.approx(before, rel=1e-9), change


def test_turning_a_spectrogram_off_again_leaves_the_zoom_too(win):
    av = win._audio_view
    key = sorted(av._checks)[0]
    av.set_spectrogram(key, True)
    before = _zoom_away_from_the_cursor(av)
    av.set_spectrogram(key, False)
    QApplication.processEvents()
    assert av.x_range_seconds() == pytest.approx(before, rel=1e-9)


def test_a_cursor_that_moves_out_of_the_window_is_still_followed(win):
    av = win._audio_view
    t0, t1 = _zoom_away_from_the_cursor(av)
    span = t1 - t0
    x0, x1 = av._link.getViewBox().viewRange()[0]
    late = int(x1 + (x1 - x0) * 3)                   # well past the window's right edge
    av.set_cursor(late)
    x0, x1 = av._link.getViewBox().viewRange()[0]
    assert x0 <= late <= x1, "the moved cursor was left outside the view"
    n0, n1 = av.x_range_seconds()
    assert n1 - n0 == pytest.approx(span, rel=1e-6), "following the cursor changed the zoom"


def test_a_long_title_does_not_widen_its_track_in_a_narrow_pane(win):
    av = win._audio_view
    av.setFixedWidth(300)                            # narrower than a spectrogram's title
    QApplication.processEvents()
    av.set_spectrogram(sorted(av._checks)[1], True)
    _zoom_away_from_the_cursor(av)
    QApplication.processEvents()
    for plot, *_rest in av._plots:
        assert plot.geometry().width() <= plot.getViewWidget().width() + 0.5, (
            f"a track is {plot.geometry().width():.0f} px in a "
            f"{plot.getViewWidget().width()} px pane: {plot.titleLabel.text!r}")
    first, *rest = _ranges(av)
    for rng in rest:
        assert rng == pytest.approx(first, rel=1e-9), "the tracks show different stretches"
