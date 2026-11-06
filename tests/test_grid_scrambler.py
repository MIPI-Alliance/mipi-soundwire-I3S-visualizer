"""The Bus Visualizer's scrambler indicator (`GridView._half_cell`).

Pinned here: every scrambled DATA bit column carries one black square flush in its own
top-left corner, and it renders black throughout, for single-bit cells side by
side and for a wide bit drawn as one merged cell.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_grid_scrambler.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math
import tempfile

import pytest

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.model.grid_slots import GridSlot

pytestmark = pytest.mark.gui


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _cfg(wide_bit: bool, sink: bool = False) -> BusConfig:
    """One scrambled port (a source, or a sink with `sink`), two channels of one bit each,
    side by side in every row; with `wide_bit`, one channel whose bit spans two columns."""
    cfg = BusConfig(num_columns=6, row_rate_khz=3072.0)
    dp = cfg.dataports[0]
    dp.enabled = True
    dp.device_number = 0
    dp.port_direction = sink             # False: source
    dp.scrambler_en = True
    dp.enable_ch = 0b1 if wide_bit else 0b11
    dp.bit_width = 1 if wide_bit else 0
    dp.sample_size = 0
    dp.interval = 1
    dp.offset = 0
    dp.horizontal_start = 2
    dp.horizontal_count = 2
    return cfg


def _squares(gv) -> list:
    from PySide6.QtCore import Qt
    return [it for it in gv._scene.items()
            if hasattr(it, "rect") and it.rect().width() == 6 and it.rect().height() == 6
            and it.brush().style() != Qt.NoBrush and it.brush().color().name() == "#000000"]


@pytest.mark.parametrize("wide_bit, sink", [(False, False), (True, False), (False, True)],
                         ids=("source", "source-wide-bit", "sink"))
def test_each_scrambled_bit_column_has_its_square_in_its_own_corner(qapp, wide_bit, sink):
    from swi3s_studio.ui.grid_view import _HALF, GridView
    cfg = _cfg(wide_bit, sink)
    with tempfile.TemporaryDirectory() as d:
        cells = viz_engine.render_payload(cfg.to_csv_file(os.path.join(d, "c.csv")), 4)[0]
    data = [c for c in cells if c["slot"] == int(GridSlot.DATA) and c.get("scrambler")]
    assert len({c["col"] for c in data}) == 2, "the fixture places two scrambled columns"
    assert {c["is_source"] for c in data} == {not sink}
    gv = GridView()
    gv.resize(900, 400)
    gv.set_bus_model(cells, {}, cfg.column_count(), 4)
    squares = _squares(gv)
    want = sorted((gv._cx(c["col"]), gv._cy(c["row"]) + (0 if c["is_source"] else _HALF))
                  for c in data)
    assert sorted((sq.rect().left(), sq.rect().top()) for sq in squares) == want
    # Rendered, each square is black throughout: nothing drawn after it covers it.
    from PySide6.QtGui import QImage, QPainter
    scale, box = 3, gv._scene.itemsBoundingRect()
    img = QImage(math.ceil(box.width() * scale), math.ceil(box.height() * scale),
                 QImage.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    painter.scale(scale, scale)                  # exactly `scale` pixels per scene unit
    painter.translate(-box.topLeft())
    gv._scene.render(painter, box, box)
    painter.end()
    side = 6 * scale
    for sq in squares:
        x0 = (sq.rect().left() - box.left()) * scale          # device pixels, exact
        y0 = (sq.rect().top() - box.top()) * scale
        # The pixels wholly inside the square, past the cell border line drawn over its
        # edge (one scene unit wide) and one pixel of rounding.
        edge = scale + 1
        xs = range(math.ceil(x0) + edge, math.floor(x0 + side) - edge)
        ys = range(math.ceil(y0) + edge, math.floor(y0 + side) - edge)
        assert xs and ys
        assert all(img.pixelColor(x, y).lightness() < 30 for x in xs for y in ys), sq.rect()
