"""Zoomed in far enough, the Audio pane draws a waveform as its samples.

Once each sample has _STEM_MIN_PX pixels of the track's width, a track is drawn as a stem
from zero to each sample's value with a dot on it, at the sample's capture position, so the
samples themselves can be seen and counted; zoomed out it is the line (or min/max envelope)
through them as before. The switch follows the samples per pixel, so a pane made narrower
or wider at the same zoom switches too.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.audio_view import _STEM_MIN_PX


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    w._show_dock(w._audio_dock)
    _settle()
    return w


def _settle():
    for _ in range(4):
        QApplication.processEvents()


def _track(av):
    plot, curve, dev, dp, ch, _n = av._plots[0]
    return plot, curve, dev, dp, ch


def _zoom_to_samples(av, count):
    """Zoom every track to `count` samples of the first, from a quarter of the way in."""
    plot, _curve, dev, dp, ch = _track(av)
    sat = av._store.sample_positions(dev, dp, ch)
    first = sat.size // 4
    plot.setXRange(float(sat[first]), float(sat[first + count]), padding=0)
    _settle()
    return first


def _drawn(curve):
    """The data the track was given (getData is it clipped to the view)."""
    return np.asarray(curve.xData), np.asarray(curve.yData)


def test_zoomed_in_each_sample_is_a_stem_with_a_dot(win):
    av = win._audio_view
    plot, curve, dev, dp, ch = _track(av)
    first = _zoom_to_samples(av, 40)
    width = plot.getViewBox().width()
    assert 40 * _STEM_MIN_PX <= width, "the track is too narrow for stems: nothing to test"
    xs, ys = _drawn(curve)
    sat = av._store.sample_positions(dev, dp, ch)
    full = float(1 << (av._store.sample_bits(dev, dp, ch) - 1))
    values = np.asarray(av._store.samples(dev, dp, ch), dtype=np.float64) / full
    i0 = int(np.searchsorted(sat, xs[0]))
    shown = slice(i0, i0 + xs.size // 2)
    assert i0 <= first and xs.size // 2 >= 40
    assert np.array_equal(xs[0::2], xs[1::2]), "a stem is not vertical"
    assert np.array_equal(xs[0::2], sat[shown].astype(np.float64))
    assert np.all(ys[0::2] == 0.0), "a stem does not start at zero"
    assert np.allclose(ys[1::2], values[shown])
    dx, dy = curve.sample_dots.getData()
    assert np.array_equal(dx, xs[0::2]) and np.allclose(dy, ys[1::2])


def test_zoomed_out_it_is_the_line_through_them(win):
    av = win._audio_view
    _plot, curve, *_ = _track(av)
    _zoom_to_samples(av, 40)
    av.reset_zoom()                            # the whole capture
    _settle()
    dx, _dy = curve.sample_dots.getData()
    assert len(dx) == 0, "the dots stayed on a zoomed-out track"
    xs, ys = _drawn(curve)
    assert xs.size and not np.all(ys[0::2] == 0.0)


def test_the_switch_follows_the_pane_width_at_one_zoom(win):
    av = win._audio_view
    plot, curve, *_ = _track(av)
    _zoom_to_samples(av, 60)
    assert len(curve.sample_dots.getData()[0]), "no stems at the start: nothing to test"
    rng = tuple(plot.getViewBox().viewRange()[0])
    win.resize(400, 900)                       # 60 samples in far fewer pixels
    _settle()
    assert tuple(plot.getViewBox().viewRange()[0]) == pytest.approx(rng)
    if 60 * _STEM_MIN_PX > plot.getViewBox().width():
        assert len(curve.sample_dots.getData()[0]) == 0
    win.resize(1400, 900)
    _settle()
    assert len(curve.sample_dots.getData()[0])


def test_every_track_and_vertical_zoom(win):
    """Every waveform track draws its stems, and Vertical Zoom fits Y to the samples."""
    av = win._audio_view
    _zoom_to_samples(av, 40)
    for _plot, curve, *_ in av._plots:
        if hasattr(curve, "sample_dots"):
            assert len(curve.sample_dots.getData()[0])
    av._vzoom_btn.click()
    _settle()
    plot, curve, *_ = _track(av)
    lo, hi = plot.getViewBox().viewRange()[1]
    dy = curve.sample_dots.getData()[1]
    assert lo <= dy.min() and dy.max() <= hi and hi - lo < 2.0
