"""Export tests: command CSV (headless) and bus-grid SVG/PNG (offscreen Qt).

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_export.py
"""
import csv
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio import decode_capture
from swi3s_studio.export import write_commands_csv
from swi3s_studio.ingest import transitions
from swi3s_studio.model import RegisterMap


def test_commands_csv():
    res = decode_capture(transitions.demo_capture(8))
    rmap = RegisterMap.load()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cmds.csv")
        write_commands_csv(res.commands, path, rmap)
        with open(path) as f:
            rows = list(csv.DictReader(f))
    assert len(rows) == len(res.commands)
    # First WriteA32 resolves its register label, and the demo is error-free.
    w = next(r for r in rows if r["command"] == "WriteA32")
    assert "NumColumns" in w["register"] or "DP" in w["register"]
    assert all(r["error"] == "" for r in rows)


def test_grid_export_png_svg():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    with tempfile.TemporaryDirectory() as d:
        png = win._grid_view.export_image(os.path.join(d, "g.png"))
        svg = win._grid_view.export_image(os.path.join(d, "g.svg"))
        assert os.path.getsize(png) > 0
        assert os.path.getsize(svg) > 0
        with open(svg, "rb") as f:
            assert f.read(64).lstrip().startswith(b"<?xml") or b"svg" in open(svg, "rb").read(256)
