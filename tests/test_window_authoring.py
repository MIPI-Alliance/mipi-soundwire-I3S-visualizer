"""The window's Bus Visualizer authoring, through the window: a burst of edits redraws the
authored grid once after the debounce, an activated notification and Reset redraw at once,
the init config and its fallback, Open / Save Visualizer CSV and their messages, the panel's
requests reaching the window's file dialogs, and Compare with Visualizer. Offscreen Qt."""
import os
import pathlib
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

pytestmark = pytest.mark.gui


def _win(load: bool = False):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _draws(monkeypatch, win) -> list:
    """A list that gains an entry each time the authored grid is drawn."""
    drawn: list = []
    real = win._viz_grid.set_bus_model
    monkeypatch.setattr(win._viz_grid, "set_bus_model",
                        lambda *a, **k: drawn.append(a[2:4]) or real(*a, **k))
    return drawn


def _file_status(monkeypatch, win) -> list:
    said: list = []
    real = win._authoring.set_file_status
    monkeypatch.setattr(win._authoring, "set_file_status",
                        lambda t: said.append(t) or real(t))
    return said


def _boxes(monkeypatch) -> list:
    shown: list = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *a, k=kind, **_kw: shown.append((k, a[1], a[2]))
                            or QMessageBox.Ok)
    return shown


def _pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def _csv(tmp_path, name="cfg.csv", rows=8):
    from swi3s_studio.model.bus_config import demo_config
    cfg = demo_config()
    cfg.rows_to_draw = rows
    return cfg.to_csv_file(str(tmp_path / name)), cfg


def test_a_burst_of_edits_redraws_the_authored_grid_once_after_the_debounce(monkeypatch):
    win = _win()
    drawn = _draws(monkeypatch, win)
    for _ in range(3):
        win._authoring.configChanged.emit()
    assert drawn == []
    _pump(0.5)
    assert len(drawn) == 1
    win.close()


def test_an_activated_notification_redraws_at_once(monkeypatch):
    win = _win()
    drawn = _draws(monkeypatch, win)
    win._authoring.issueActivated.emit([])
    assert len(drawn) == 1
    win.close()


def test_reset_puts_the_default_config_in_the_panel(monkeypatch, tmp_path):
    from swi3s_studio.model.bus_config import BusConfig
    from swi3s_studio.ui.authoring import AuthoringPanel
    win = _win()
    fresh = AuthoringPanel()
    fresh.set_config(BusConfig())
    path, _cfg = _csv(tmp_path, rows=8)
    win.open_visualizer_csv_path(path)
    assert win._authoring.config().to_dict() != fresh.config().to_dict()
    win._authoring.resetRequested.emit()
    assert win._authoring.config().to_dict() == fresh.config().to_dict()
    win.close()


def test_the_init_config_loads_from_the_file_the_environment_names(monkeypatch, tmp_path):
    path, cfg = _csv(tmp_path, "init.csv", rows=12)
    monkeypatch.setenv("SWI3S_INIT_CSV", path)
    win = _win()
    said = _file_status(monkeypatch, win)
    win._authoring.loadInitRequested.emit()
    assert win._authoring.config().rows_to_draw == 12
    assert win._status.text() == f"Loaded init config: {path}"
    assert said == ["Loaded: init.csv"]
    win.close()


def test_an_unreadable_init_config_falls_back_to_the_demo_config(monkeypatch, tmp_path):
    from swi3s_studio.model.bus_config import demo_config
    bad = tmp_path / "init.csv"
    bad.mkdir()                                       # there, and not a file to read
    monkeypatch.setenv("SWI3S_INIT_CSV", str(bad))
    win = _win()
    said = _file_status(monkeypatch, win)
    win.load_init_csv()
    assert win._authoring.config().to_dict() == demo_config().to_dict()
    assert said == ["Loaded: built-in demo config"]
    assert win._status.text().startswith("init.csv unavailable (")
    assert win._status.text().endswith("); loaded demo config")
    win.close()


def test_open_visualizer_csv_switches_to_the_visualizer_and_loads_the_file(monkeypatch,
                                                                         tmp_path):
    from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION
    path, _cfg = _csv(tmp_path, rows=24)
    asked: list = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda *a, **k: asked.append(a[1]) or (path, ""))
    win = _win()
    win._mode_mgr.switch_to(ANALYSIS)
    said = _file_status(monkeypatch, win)
    win.open_visualizer_csv()
    assert asked == ["Open Visualizer CSV"]
    assert win._mode_mgr.current() == VISUALIZATION
    assert win._authoring.config().rows_to_draw == 24
    assert win._status.text() == f"Loaded config: {path}"
    assert said == ["Loaded: cfg.csv"]
    win.close()


def test_a_csv_that_cannot_be_read_is_reported_and_the_panel_kept(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    win = _win()
    before = win._authoring.config().to_dict()
    win.open_visualizer_csv_path(str(tmp_path / "missing.csv"))
    assert [(k, t) for k, t, _x in shown] == [("warning", "Load config")]
    assert shown[0][2].startswith("Couldn't read that CSV: ")
    assert win._authoring.config().to_dict() == before
    win.close()


def test_save_visualizer_csv_writes_the_panels_config(monkeypatch, tmp_path):
    from swi3s_studio.model.bus_config import BusConfig
    src, _cfg = _csv(tmp_path, "src.csv", rows=20)
    out = tmp_path / "saved.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win = _win()
    win.open_visualizer_csv_path(src)
    said = _file_status(monkeypatch, win)
    win._authoring.saveRequested.emit()
    assert BusConfig.from_csv(str(out)).to_dict() == win._authoring.config().to_dict()
    assert win._status.text() == f"Config saved: {out}"
    assert said == ["Saved: saved.csv"]
    win.close()


@pytest.mark.parametrize("signal, title", [
    ("loadRequested", "Load bus config (visualizer CSV)"),
    ("saveRequested", "Save bus config"),
    ("exportJsonRequested", "Export bus model JSON"),
])
def test_the_panels_requests_open_the_windows_file_dialogs(monkeypatch, signal, title):
    asked: list = []
    for name in ("getOpenFileName", "getSaveFileName"):
        monkeypatch.setattr(QFileDialog, name, lambda *a, **k: asked.append(a[1]) or ("", ""))
    win = _win()
    status = win._status.text()
    getattr(win._authoring, signal).emit()
    assert asked == [title] and win._status.text() == status
    win.close()


def test_compare_with_visualizer_with_no_capture_asks_for_one(monkeypatch):
    shown = _boxes(monkeypatch)
    win = _win()
    win.use_authoring_as_expected()
    assert [(k, t) for k, t, _x in shown] == [("information", "Compare with Visualizer")]
    win.close()


def test_compare_with_visualizer_compares_the_panels_config_in_the_analyzer(monkeypatch,
                                                                          tmp_path):
    """The config compared is the panel's, written as a Visualizer CSV, and the compare
    runs in the Bus Analyzer."""
    from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION
    win = _win(load=True)
    win._mode_mgr.switch_to(VISUALIZATION)
    compared: list = []
    monkeypatch.setattr(win, "_run_compare", lambda path: compared.append(
        (win._mode_mgr.current(), pathlib.Path(path).read_bytes())))
    win.use_authoring_as_expected()
    want = pathlib.Path(win._authoring.config().to_csv_file(str(tmp_path / "want.csv")))
    assert compared == [(ANALYSIS, want.read_bytes())]
    win.close()
