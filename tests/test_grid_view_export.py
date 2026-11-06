"""Bus Grid image export: a scene too large for a PNG raster is refused with a message
that names SVG, and exports as SVG; a failed write leaves an existing file as it was."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session

pytestmark = pytest.mark.gui


def test_a_grid_too_large_for_a_png_says_so_and_still_exports_as_svg(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    import swi3s_studio.ui.grid_view as gv
    s = Session.from_demo(64)
    grid = gv.GridView()
    grid.set_cells(s.grid_cells(), s.column_count)
    monkeypatch.setattr(gv, "_MAX_PNG_BYTES", 1000)
    with pytest.raises(OSError, match="too large for a PNG.*SVG"):
        grid.export_image(str(tmp_path / "g.png"))
    assert not (tmp_path / "g.png").exists()
    assert grid.export_image(str(tmp_path / "g.svg")).endswith("g.svg")


@pytest.mark.parametrize("name, magic", [("g.png", b"\x89PNG"), ("g.svg", b"<?xml")])
def test_a_grid_image_that_fails_to_write_leaves_the_previous_file(tmp_path, monkeypatch, name,
                                                                   magic):
    QApplication.instance() or QApplication([])
    import swi3s_studio.ui.grid_view as gv
    s = Session.from_demo(64)
    grid = gv.GridView()
    grid.set_cells(s.grid_cells(), s.column_count)
    path = tmp_path / name
    path.write_bytes(b"earlier export")

    def fail(*_a, **_k):
        raise OSError("the volume is full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="volume is full"):
        grid.export_image(str(path))
    assert path.read_bytes() == b"earlier export"
    assert [p.name for p in tmp_path.iterdir()] == [name]
    monkeypatch.undo()
    grid.export_image(str(path))
    assert path.read_bytes().startswith(magic)
