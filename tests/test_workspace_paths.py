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

pytestmark = [pytest.mark.gui, pytest.mark.platform]


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


def test_a_capture_with_no_relative_path_is_saved_absolute_and_opens(tmp_path, monkeypatch):
    """A capture that has no path relative to the workspace (another drive on Windows, where
    os.path.relpath raises ValueError) is saved as its absolute path, and opens from it after
    the workspace is moved."""
    cap = _sal(tmp_path / "captures")
    ws_path = str(tmp_path / "proj" / "w.swi3s")
    os.makedirs(tmp_path / "proj")

    def no_relative_path(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    with monkeypatch.context() as m:
        m.setattr(os.path, "relpath", no_relative_path)
        roundabout = os.path.join(os.path.dirname(cap), os.pardir, "captures", "bus.sal")
        Workspace(links=[LinkSpec(source=_sal_source(roundabout))]).save(ws_path)
    src = _saved(ws_path)["links"][0]["source"]
    assert src["path"] == os.path.abspath(cap)
    assert src["saved_at"] == {"path": os.path.abspath(cap)}
    shutil.move(ws_path, str(tmp_path / "w.swi3s"))
    ws = Workspace.load(str(tmp_path / "w.swi3s"))
    assert ws.links[0].source["path"] == os.path.abspath(cap) and ws.missing_files() == []


def test_a_capture_under_a_non_ascii_name_is_saved_relative_and_opens(tmp_path):
    """A folder and a capture named outside ASCII are saved as the same text, relative to
    the workspace, and open from there after the folder is moved."""
    cap = _sal(tmp_path / "proj" / "Aufnahmen é 録音", name="Bus ü.sal")
    ws_path = str(tmp_path / "proj" / "w.swi3s")
    Workspace(links=[LinkSpec(source=_sal_source(cap))]).save(ws_path)
    assert _saved(ws_path)["links"][0]["source"]["path"] == "Aufnahmen é 録音/Bus ü.sal"
    shutil.move(str(tmp_path / "proj"), str(tmp_path / "moved"))
    ws = Workspace.load(str(tmp_path / "moved" / "w.swi3s"))
    assert ws.links[0].source["path"] == str(tmp_path / "moved" / "Aufnahmen é 録音" / "Bus ü.sal")
    assert ws.missing_files() == []


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
    ws = Workspace.load(str(tmp_path / "b" / "w.swi3s"))          # relative "a/..." is gone
    assert sorted(k for _i, k, _p in ws.missing_files()) == ["clock", "data"]
    ws.locate(0, "clock", str(tmp_path / "b" / "clk.bin"))
    assert ws.missing_files() == []
    assert ws.links[0].source["data"] == str(tmp_path / "b" / "dat.bin")


def test_a_lost_relative_capture_is_not_found_in_the_working_directory(tmp_path, monkeypatch):
    """A relative capture found neither beside the workspace nor where it was saved is
    reported missing, even when the working directory holds a file of that relative name."""
    cap = _sal(tmp_path / "projA" / "captures")
    Workspace(links=[LinkSpec(source=_sal_source(cap))]).save(str(tmp_path / "projA" / "w.swi3s"))
    os.makedirs(tmp_path / "downloads")
    shutil.move(str(tmp_path / "projA" / "w.swi3s"), str(tmp_path / "downloads" / "w.swi3s"))
    shutil.rmtree(tmp_path / "projA")
    _sal(tmp_path / "projB" / "captures")
    monkeypatch.chdir(tmp_path / "projB")
    ws = Workspace.load(str(tmp_path / "downloads" / "w.swi3s"))
    expected = str(tmp_path / "downloads" / "captures" / "bus.sal")
    assert ws.missing_files() == [(0, "path", expected)]


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
    assert asked == [str(tmp_path / "proj" / "captures" / "bus.sal")]
    assert len(again.links) == 1 and again._session.source["path"] == new


def test_cancel_or_skip_of_the_only_link_opens_nothing(opened, tmp_path, monkeypatch):
    win, cap = opened
    ws_path = str(tmp_path / "proj" / "bench.swi3s")
    _save(win, monkeypatch, ws_path)
    os.remove(cap)
    again, asked = _reopen(monkeypatch, ws_path, "cancel")
    assert asked and len(again.links) == 0 and getattr(again, "_load_thread", None) is None
    errors = _no_errors(monkeypatch)
    again, _asked = _reopen(monkeypatch, ws_path, "skip")
    assert len(again.links) == 0 and errors == []     # its only Link was skipped, silently


def _two_link_workspace(tmp_path):
    """A workspace of two .sal Links, "Bus A" (active) and "Bus B", each its own file."""
    a = _sal(tmp_path / "proj", "a.sal")
    b = _sal(tmp_path / "proj", "b.sal")
    ws_path = str(tmp_path / "proj" / "two.swi3s")
    Workspace(links=[LinkSpec(source=_sal_source(a), name="Bus A"),
                     LinkSpec(source=_sal_source(b), name="Bus B", offset_ps=7)],
              bookmarks=[{"sample": 11, "group": "A", "index": 1, "label": "", "link": 0},
                         {"sample": 22, "group": "A", "index": 2, "label": "", "link": 1}],
              ).save(ws_path)
    return ws_path, a, b


def _no_errors(monkeypatch):
    shown = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.critical",
                        lambda *a, **k: shown.append(a[1:3]))
    return shown


def test_skipping_the_first_link_opens_the_others(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    ws_path, a, b = _two_link_workspace(tmp_path)
    os.remove(a)
    errors = _no_errors(monkeypatch)
    again, asked = _reopen(monkeypatch, ws_path, "skip")
    assert asked == [os.path.abspath(a)]
    assert [(e.name, e.offset_ps) for e in again.links] == [("Bus B", 7)]
    assert again._session.source["path"] == b
    assert sorted((m.sample, m.link) for m in again._bookmarks) == [(22, 0)]
    assert errors == []                               # a skipped file is not an error


def test_skipping_every_link_opens_nothing_and_reports_no_error(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    ws_path, a, b = _two_link_workspace(tmp_path)
    os.remove(a)
    os.remove(b)
    errors = _no_errors(monkeypatch)
    again, asked = _reopen(monkeypatch, ws_path, "skip")
    assert asked == [os.path.abspath(a), os.path.abspath(b)]
    assert len(again.links) == 0 and getattr(again, "_load_thread", None) is None
    assert errors == []


def test_a_windows_pair_on_another_drive_is_named_and_located_by_its_file(tmp_path):
    """A pair saved on Windows on another drive than its workspace keeps its absolute
    `D:\\` paths; once the user finds one file, the other is taken from the same folder, and
    the Link and an import note name each by its file."""
    from swi3s_studio import import_notes
    from swi3s_studio.session import Session
    pair = {"type": "saleae_binary", "clock": "D:\\caps\\clk.bin", "data": "D:\\caps\\dat.bin",
            "sample_rate_hz": 500_000_000,
            "saved_at": {"clock": "D:\\caps\\clk.bin", "data": "D:\\caps\\dat.bin"}}
    ws_path = tmp_path / "w.swi3s"
    ws_path.write_text(json.dumps({"links": [{"source": pair}]}), encoding="utf-8")
    found = tmp_path / "found"
    os.makedirs(found)
    for name in ("clk.bin", "dat.bin"):
        (found / name).write_bytes(b"x")
    ws = Workspace.load(str(ws_path))
    assert [p for _i, _k, p in ws.missing_files()] == ["D:\\caps\\clk.bin", "D:\\caps\\dat.bin"]
    assert Session.label_of(ws.links[0].source) == "clk.bin & dat.bin"
    assert import_notes.source_name("D:\\caps\\clk.bin") == "clk.bin"
    ws.locate(0, "clock", str(found / "clk.bin"))
    assert ws.missing_files() == []
    assert ws.links[0].source["data"] == str(found / "dat.bin")


@pytest.mark.skipif(os.sep == "\\", reason="a backslash cannot be in a Windows file name")
def test_a_posix_file_name_holding_a_backslash_is_named_and_located_by_it(tmp_path):
    from swi3s_studio import import_notes
    from swi3s_studio.session import Session
    saved = tmp_path / "gone"
    os.makedirs(saved)
    pair = {"type": "saleae_binary", "clock": str(saved / "run\\A_clk.bin"),
            "data": str(saved / "run\\A_dat.bin"), "sample_rate_hz": 1_000_000}
    ws_path = tmp_path / "w.swi3s"
    ws_path.write_text(json.dumps({"links": [{"source": pair}]}), encoding="utf-8")
    found = tmp_path / "found"
    os.makedirs(found)
    for name in ("run\\A_clk.bin", "run\\A_dat.bin"):
        (found / name).write_bytes(b"x")
    ws = Workspace.load(str(ws_path))
    assert Session.label_of(ws.links[0].source) == "run\\A_clk.bin & run\\A_dat.bin"
    assert import_notes.source_name(str(saved / "run\\A_clk.bin")) == "run\\A_clk.bin"
    assert import_notes.source_name("run\\A_clk.bin") == "A_clk.bin"
    ws.locate(0, "clock", str(found / "run\\A_clk.bin"))
    assert ws.missing_files() == []
    assert ws.links[0].source["data"] == str(found / "run\\A_dat.bin")
