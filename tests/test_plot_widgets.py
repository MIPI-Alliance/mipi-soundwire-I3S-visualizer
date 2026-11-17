"""ui/plot_widgets.XWheelViewBox: a horizontal wheel or swipe pans the X axis by 15 % of
the view per notch, a vertical one zooms it about the pointer as pyqtgraph does, and an
event with no orientation is taken as vertical. The Audio and Capture views pan with it.
Real QGraphicsSceneWheelEvents on a real viewbox. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtWidgets import QApplication, QGraphicsSceneWheelEvent

pytestmark = pytest.mark.gui


def _box(x0=100.0, x1=200.0):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.plot_widgets import XWheelViewBox
    box = XWheelViewBox()
    box.resize(400, 100)
    box.setMouseEnabled(x=True, y=False)
    box.setRange(xRange=(x0, x1), yRange=(-1.0, 1.0), padding=0)
    return box


def _wheel(delta: int, orientation, x: float = 200.0) -> QGraphicsSceneWheelEvent:
    ev = QGraphicsSceneWheelEvent(QEvent.GraphicsSceneWheel)
    ev.setDelta(delta)
    ev.setOrientation(orientation)
    ev.setPos(QPointF(x, 50.0))
    ev.setAccepted(False)
    return ev


def _x(box):
    (x0, x1), _y = box.viewRange()
    return x0, x1


@pytest.mark.parametrize("delta, shift", [(120, -15.0), (-120, 15.0), (240, -30.0),
                                          (60, -7.5)])
def test_a_horizontal_wheel_pans_x_by_fifteen_percent_of_the_view_per_notch(delta, shift):
    box = _box()
    y = box.viewRange()[1]
    ev = _wheel(delta, Qt.Horizontal)
    box.wheelEvent(ev)
    x0, x1 = _x(box)
    assert x0 == pytest.approx(100.0 + shift) and x1 == pytest.approx(200.0 + shift)
    assert box.viewRange()[1] == pytest.approx(y)
    assert ev.isAccepted()


@pytest.mark.parametrize("delta, narrower", [(120, True), (-120, False)])
def test_a_vertical_wheel_zooms_x_about_the_pointer(delta, narrower):
    """A notch away zooms in and one towards zooms out, by pyqtgraph's proportional step,
    keeping the data under the pointer where it is; Y does not move."""
    box = _box()
    y = box.viewRange()[1]
    under = box.mapToView(QPointF(200.0, 50.0)).x()
    box.wheelEvent(_wheel(delta, Qt.Vertical))
    x0, x1 = _x(box)
    assert (x1 - x0 < 100.0) == narrower and x1 - x0 != pytest.approx(100.0)
    assert box.mapToView(QPointF(200.0, 50.0)).x() == pytest.approx(under)
    assert box.viewRange()[1] == pytest.approx(y)


class _NoOrientation:
    """A wheel event that has no orientation(), delegating the rest to a real one."""

    def __init__(self, ev):
        self._ev = ev

    def orientation(self):
        raise AttributeError("orientation")

    def __getattr__(self, name):
        return getattr(self._ev, name)


def test_a_wheel_event_with_no_orientation_zooms_as_a_vertical_one():
    box, ref = _box(), _box()
    box.wheelEvent(_NoOrientation(_wheel(120, Qt.Horizontal)))
    ref.wheelEvent(_wheel(120, Qt.Vertical))
    assert _x(box) == pytest.approx(_x(ref))


def test_the_audio_and_capture_views_pan_with_a_horizontal_wheel():
    """Every plot of the Audio and Capture views of a loaded capture draws in an
    XWheelViewBox, so a horizontal swipe pans it."""
    import pyqtgraph as pg

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.plot_widgets import XWheelViewBox
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    for view in (win._audio_view, win._raw_view):
        plots = view.findChildren(pg.PlotWidget)
        assert plots, type(view).__name__
        for plot in plots:
            assert isinstance(plot.getViewBox(), XWheelViewBox), type(view).__name__
    box = win._raw_view.findChildren(pg.PlotWidget)[0].getViewBox()
    lo, hi = _x(box)
    box.setXRange(lo + 0.4 * (hi - lo), lo + 0.6 * (hi - lo), padding=0)   # inside the capture
    x0, x1 = _x(box)
    box.wheelEvent(_wheel(-120, Qt.Horizontal))         # towards the later samples
    assert _x(box) == pytest.approx((x0 + 0.15 * (x1 - x0), x1 + 0.15 * (x1 - x0)))
    win.close()
