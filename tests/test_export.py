"""Export tests: command CSV (headless) and bus-grid SVG/PNG (offscreen Qt).

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_export.py
"""
import csv
import os
import pathlib
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
        head = pathlib.Path(svg).read_bytes()[:256]
        assert head.lstrip().startswith(b"<?xml") or b"svg" in head


def test_the_links_export_merges_every_link_in_global_time(tmp_path):
    import csv

    from swi3s_studio.export import write_links_commands_csv
    a = [{"start_sample": 10, "end_sample": 11, "command": "Ping"},
         {"start_sample": 30, "end_sample": 31, "command": "Write"}]
    b = [{"start_sample": 5, "end_sample": 6, "command": "Read"}]
    path = str(tmp_path / "all.csv")
    write_links_commands_csv([("Link 1", a, lambda s: s * 1000),
                              ("Amp", b, lambda s: s * 1000 + 15_000)], path)
    rows = list(csv.reader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    assert rows[0][:3] == ["link", "start_ps", "start_sample"]
    # Amp's sample 5 is at 20 ns: after Link 1's 10 ns, before its 30 ns.
    assert [(r[0], r[1], r[4]) for r in rows[1:]] == [
        ("Link 1", "10000", "Ping"), ("Amp", "20000", "Read"), ("Link 1", "30000", "Write")]
