"""A workspace names its captures relative to itself, so a folder holding both can be moved
or shared, and asks for a capture it cannot find."""
import json
import os
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import export_sal_links, pump_loads, two_link_captures
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio.workspace import LinkSpec, Workspace


def _sal(folder, name="bus.sal"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    export_sal_links([two_link_captures()[0]], path)
    return path


def _sal_source(path):
    return {"type": "sal", "path": path, "clock_channel": 0, "data_channel": 1}


def _saved(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- the file
def test_a_capture_is_saved_relative_to_the_workspace(tmp_path):
    cap = _sal(tmp_path / "proj" / "captures")
    ws_path = str(tmp_path / "proj" / "bench.swi3s")
    Workspace(links=[LinkSpec(source=_sal_source(cap))]).save(ws_path)
    src = _saved(ws_path)["links"][0]["source"]
    assert src["path"] == "captures/bus.sal"
    assert src["saved_at"] == {"path": os.path.abspath(cap)}


def test_a_moved_folder_still_opens(tmp_path):
    cap = _sal(tmp_path / "proj" / "captures")
    Workspace(links=[LinkSpec(source=_sal_source(cap))]).save(str(tmp_path / "proj" / "w.swi3s"))
    shutil.move(str(tmp_path / "proj"), str(tmp_path / "moved"))
    ws = Workspace.load(str(tmp_path / "moved" / "w.swi3s"))
    assert ws.links[0].source["path"] == str(tmp_path / "moved" / "captures" / "bus.sal")
    assert ws.missing_files() == [] and "saved_at" not in ws.links[0].source


def test_a_workspace_moved_alone_finds_the_capture_where_it_was(tmp_path):
    cap = _sal(tmp_path / "captures")
    Workspace(links=[LinkSpec(source=_sal_source(cap))]).save(str(tmp_path / "w.swi3s"))
    os.makedirs(tmp_path / "elsewhere")
    shutil.move(str(tmp_path / "w.swi3s"), str(tmp_path / "elsewhere" / "w.swi3s"))
    ws = Workspace.load(str(tmp_path / "elsewhere" / "w.swi3s"))
    assert ws.links[0].source["path"] == os.path.abspath(cap) and ws.missing_files() == []


def test_a_lost_capture_is_reported_and_locating_one_file_finds_its_partner(tmp_path):
    """A .bin pair names two files; once the user finds one, the other is taken from the
    same folder without asking again."""
    pair = {"type": "saleae_binary", "clock": str(tmp_path / "a" / "clk.bin"),
            "data": str(tmp_path / "a" / "dat.bin"), "sample_rate_hz": 500_000_000}
    os.makedirs(tmp_path / "a")
    for name in ("clk.bin", "dat.bin"):
        (tmp_path / "a" / name).write_bytes(b"x")
    Workspace(links=[LinkSpec(source=pair)]).save(str(tmp_path / "w.swi3s"))
    shutil.move(str(tmp_path / "a"), str(tmp_path / "b"))
    shutil.move(str(tmp_path / "w.swi3s"), str(tmp_path / "b" / "w.swi3s"))
    os.makedirs(tmp_path / "c")
    ws = Workspace.load(str(tmp_path / "b" / "w.swi3s"))          # relative "a/..." is gone
    assert sorted(k for _i, k, _p in ws.missing_files()) == ["clock", "data"]
    ws.locate(0, "clock", str(tmp_path / "b" / "clk.bin"))
    assert ws.missing_files() == []
    assert ws.links[0].source["data"] == str(tmp_path / "b" / "dat.bin")


def test_an_analog_csvs_channel_names_are_not_taken_for_files(tmp_path):
    cap = tmp_path / "scope.csv"
    cap.write_text("x")
    src = {"type": "analog_csv", "path": str(cap), "clock": "CH1", "data": "CH2"}
    Workspace(links=[LinkSpec(source=src)]).save(str(tmp_path / "w.swi3s"))
    saved = _saved(str(tmp_path / "w.swi3s"))["links"][0]["source"]
    assert (saved["path"], saved["clock"], saved["data"]) == ("scope.csv", "CH1", "CH2")


def test_a_config_csv_travels_with_the_workspace_too(tmp_path):
    cap = _sal(tmp_path)
    cfg = tmp_path / "cfg" / "bus.csv"
    os.makedirs(cfg.parent)
    cfg.write_text("x")
    src = {**_sal_source(cap), "config_csv": str(cfg)}
    Workspace(links=[LinkSpec(source=src)]).save(str(tmp_path / "w.swi3s"))
    assert _saved(str(tmp_path / "w.swi3s"))["links"][0]["source"]["config_csv"] == "cfg/bus.csv"


def test_a_workspace_saved_before_paths_were_relative_still_opens(tmp_path):
    """An older workspace has absolute paths and no saved_at."""
    cap = _sal(tmp_path)
    text = Workspace(links=[LinkSpec(source=_sal_source(cap))]).to_json()
    (tmp_path / "old.json").write_text(text)
    ws = Workspace.load(str(tmp_path / "old.json"))
    assert ws.links[0].source["path"] == cap and ws.missing_files() == []


# ---------------------------------------------------------------- the window
@pytest.fixture
def opened(tmp_path):
    """A window with a .sal capture open, as File ▸ Open Capture would leave it."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    cap = _sal(tmp_path / "proj" / "captures")
    win = MainWindow()
    win.load_session(Session.from_sal(cap, 0, 1))
    return win, cap


def _save(win, monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()


def test_save_names_the_file_swi3s(opened, tmp_path, monkeypatch):
    win, _cap = opened
    _save(win, monkeypatch, str(tmp_path / "proj" / "bench"))       # typed with no extension
    assert os.path.isfile(tmp_path / "proj" / "bench.swi3s")
    _save(win, monkeypatch, str(tmp_path / "proj" / "mine.json"))   # an extension typed is kept
    assert os.path.isfile(tmp_path / "proj" / "mine.json")


def _reopen(monkeypatch, ws_path, answer, located=None):
    from swi3s_studio.ui.main_window import MainWindow
    again = MainWindow()
    asked = []
    monkeypatch.setattr(again, "_ask_missing_capture",
                        lambda name, saved, others: (asked.append(saved), answer)[1])
    answers = iter([(ws_path, ""), (located or "", "")])
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: next(answers))
    again.open_workspace()
    pump_loads(again)
    return again, asked


def test_a_lost_capture_can_be_located(opened, tmp_path, monkeypatch):
    win, cap = opened
    ws_path = str(tmp_path / "proj" / "bench.swi3s")
    _save(win, monkeypatch, ws_path)
    os.makedirs(tmp_path / "archive")
    new = shutil.move(cap, str(tmp_path / "archive" / "bus.sal"))
    again, asked = _reopen(monkeypatch, ws_path, "locate", located=new)
    assert asked == ["captures/bus.sal"]
    assert len(again.links) == 1 and again._session.source["path"] == new


def test_cancel_opens_nothing_and_skip_opens_the_rest(opened, tmp_path, monkeypatch):
    win, cap = opened
    ws_path = str(tmp_path / "proj" / "bench.swi3s")
    _save(win, monkeypatch, ws_path)
    os.remove(cap)
    again, asked = _reopen(monkeypatch, ws_path, "cancel")
    assert asked and len(again.links) == 0 and getattr(again, "_load_thread", None) is None
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.critical", lambda *a, **k: None)
    again, _asked = _reopen(monkeypatch, ws_path, "skip")
    assert len(again.links) == 0                      # its only Link was skipped
