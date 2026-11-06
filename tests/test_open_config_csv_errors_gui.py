"""A config CSV the reader cannot parse is reported to the user, naming the file, by every
window path that reads one; none of them lets the error escape the Qt slot."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import open_via_dialog, pump_loads
from PySide6.QtWidgets import QApplication, QMessageBox

pytestmark = pytest.mark.gui


def _huge_cell_csv(tmp_path) -> str:
    path = tmp_path / "huge_cell.csv"
    path.write_text("AppVersion,3\nNumColumns_REG," + "9" * 200_000 + "\n")
    return str(path)


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    return win


def _capture(tmp_path, win) -> str:
    path = str(tmp_path / "cap.sal")
    win._session.export_sal(path)
    return path


def test_open_capture_names_the_config_csv_it_could_not_read(monkeypatch, tmp_path):
    win = _window()
    shown = []
    monkeypatch.setattr(QMessageBox, "critical",
                        lambda parent, title, text, *a, **k: shown.append((title, text)))
    open_via_dialog(monkeypatch, [_capture(tmp_path, win)])
    win.open_capture(config_csv=_huge_cell_csv(tmp_path))
    pump_loads(win)
    assert len(shown) == 1 and shown[0][0] == "Open failed"
    assert "huge_cell.csv" in shown[0][1] and "field" in shown[0][1], shown


def test_the_authoring_panel_names_the_config_csv_it_could_not_read(monkeypatch, tmp_path):
    win = _window()
    shown = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *a, **k: shown.append((title, text)))
    win._load_authoring_csv(_huge_cell_csv(tmp_path))
    assert len(shown) == 1 and "huge_cell.csv" in shown[0][1], shown
