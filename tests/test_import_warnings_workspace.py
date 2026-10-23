"""Import warnings from a workspace: a key this version does not read, a value of the wrong
type, a row that does not read and a saved setting that no longer applies are left out and
reported, and a reopened workspace lists them in one Import warnings window titled with the
workspace file."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.workspace import LinkSpec, Workspace


def _messages(notes):
    return [n.message for n in notes]


def test_a_saved_workspace_reads_back_without_warnings():
    ws = Workspace(links=[LinkSpec(source={"type": "demo"}, overlay=[[0, 1, 0x2090, 2]],
                                   device_names={"1": "Amp"}, device_hub_depths={"2": 1},
                                   device_scramblers=[[1, 2, True]], even_timing=[[1, 2]])],
                   bookmarks=[{"sample": 5, "group": "A", "index": 1, "label": "", "link": 0}],
                   view={"grid_rows": 32})
    with import_notes.collect() as notes:
        back = Workspace.from_json(ws.to_json(), "ok.swi3s")
    assert notes == []
    assert back.links[0].overlay == [[0, 1, 0x2090, 2]]


def test_a_v3_workspace_reads_without_warnings():
    v3 = {"version": 3, "source": {"type": "demo"}, "overlay": [[0, 0, 0x2090, 2]],
          "bookmarks": [10, 20], "cursor": 4, "mode": "Analysis", "device_names": {},
          "device_hub_depths": {}, "device_regmaps": {}, "device_scramblers": [], "view": {}}
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps(v3), "old.json")
    assert notes == []


def test_what_a_workspace_cannot_read_is_dropped_and_reported():
    d = {"version": 9, "future_key": 1, "active_link": 0, "cursor": 0, "mode": "Analysis",
         "view": [], "timing": "fast", "bookmarks": [{"sample": "x"}, {"sample": 3}],
         "links": [{"source": {"type": "demo"}, "name": "Bus", "colour": "red",
                    "overlay": [[0, 0x2090, 2], [0, 1, 0x2090, 2]],
                    "device_scramblers": [[1, 2]], "even_timing": "all",
                    "device_names": {"1": "Amp", "40": "Nowhere", "2": 7},
                    "device_hub_depths": {"1": 9, "3": 2},
                    "device_regmaps": {"x": {}}, "view": 5}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "bad.swi3s")
    link = ws.links[0]
    assert link.overlay == [[0, 1, 0x2090, 2]]
    assert link.device_scramblers == [] and link.even_timing == []
    assert link.device_names == {"1": "Amp"} and link.device_hub_depths == {"3": 2}
    assert link.device_regmaps == {} and link.view == {}
    assert ws.view == {} and ws.timing is None and len(ws.bookmarks) == 1
    assert {n.source for n in notes} == {"bad.swi3s"}
    assert _messages(notes) == [
        "Link 1 (Bus): 1 saved setting this version does not read, ignored: colour",
        "Link 1 (Bus): 'even_timing' is not a list; not read",
        "Link 1 (Bus): 1 register override not read (not [region, device, address, value] "
        "numbers)",
        "Link 1 (Bus): 1 scrambler override not read (not [device, data port, on])",
        "Link 1 (Bus): 2 device names not read: the device is not 0..11 or the value is "
        "not text",
        "Link 1 (Bus): 1 hub depth not read: the device is not 0..11 or the value is not "
        "0..5",
        "Link 1 (Bus): 1 register map not read: the device is not 0..11 or the value is not "
        "a map",
        "Link 1 (Bus): its saved view is not a mapping; not read",
        "1 saved setting this version does not read, ignored: future_key",
        "saved in workspace format 9, newer than the 4 this version reads: what that format "
        "adds is not restored",
        "the saved window view is not a mapping; not read",
        "the saved 'timing' settings are not a mapping; not read",
        "1 cursor with no readable sample not read",
    ]


def _reopen(monkeypatch, d, tmp_path, name="odd.swi3s"):
    from swi3s_studio.ui.main_window import MainWindow
    path = tmp_path / name
    path.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    win = MainWindow()
    win.open_workspace()
    pump_loads(win)
    return win


def _demo_source():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.load_demo()
    return dict(w._session.source)


def test_reopening_a_workspace_with_invalid_values_opens_one_warnings_window(
        tmp_path, monkeypatch):
    src = _demo_source()
    d = {"version": 4, "active_link": 0, "cursor": 0, "mode": "Sketching",
         "view": {"grid_rows": "lots", "commands_header": {"hidden": [], "sort": ["Nope", 0]},
                  "registers": {"device": 99}},
         "links": [{"source": src, "name": "Link 1", "overlay": [[0, 1]],
                    "stream_colors": [[0, 1, "dark", "not-a-colour"]]}]}
    win = _reopen(monkeypatch, d, tmp_path)
    assert len(win.links) == 1
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1 and not dlgs[0].isModal()
    assert dlgs[0].windowTitle() == "Import warnings — odd.swi3s"
    lines = dlgs[0].lines()
    assert all(line.startswith("odd.swi3s: ") for line in lines)
    for part in ("Link 1 (Link 1): 1 register override not read",
                 "Link 1 (Link 1): 1 saved stream colour could not be read and was not "
                 "restored",
                 "the saved mode 'Sketching' is not one of",
                 "the saved 'grid_rows' 'lots' is not a number; 64 is used",
                 "the saved Commands sort 'Nope' is not a sortable column of the table",
                 "the saved Registers view has entries that do not read, left as the pane has "
                 "them: device 99"):
        assert any(part in line for line in lines), (part, lines)
    assert "import warning" in win._status.text()


def test_reopening_a_clean_workspace_opens_no_window(tmp_path, monkeypatch):
    src = _demo_source()
    d = json.loads(Workspace.single(source=src).to_json())
    win = _reopen(monkeypatch, d, tmp_path, "clean.swi3s")
    assert len(win.links) == 1
    assert win.import_warning_dialogs() == []


def test_a_saved_window_reapplied_from_a_round_trip_reports_nothing(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    path = str(tmp_path / "round.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    again = MainWindow()
    again.open_workspace()
    pump_loads(again)
    assert again.import_warning_dialogs() == []
