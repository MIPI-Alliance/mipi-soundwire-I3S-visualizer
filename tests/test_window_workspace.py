"""Workspaces, through the window: Save with no capture, a save and open in a new window that
restores the Links, cursors, cursor, mode, Bus Visualizer and Timing settings and the Bus
Grid's view; an unreadable file; a capture the workspace cannot find, located, skipped or
cancelled; a saved cursor whose Link did not load; an unknown mode; and the workspace's name
held for its import warnings only while it is applied. Offscreen Qt."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import open_via_dialog, pump_loads
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from swi3s_studio import import_notes

pytestmark = pytest.mark.gui


def _win(load: bool = True):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _boxes(monkeypatch) -> list:
    shown: list = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *a, k=kind, **_kw: shown.append((k, a[1], a[2]))
                            or QMessageBox.Ok)
    return shown


def _dialogs(monkeypatch, path) -> None:
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))


def test_save_with_no_capture_asks_for_one(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    _dialogs(monkeypatch, tmp_path / "w.swi3s")
    _win(load=False).save_workspace()
    assert shown == [("information", "No session", "Load a capture first.")]
    assert list(tmp_path.iterdir()) == []


def test_a_saved_workspace_opens_in_a_new_window_as_it_was(monkeypatch, tmp_path):
    from swi3s_studio.ui.mode_controller import ANALYSIS
    from swi3s_studio.workspace import WORKSPACE_SUFFIX
    path = tmp_path / f"w{WORKSPACE_SUFFIX}"
    _dialogs(monkeypatch, path)
    win = _win()
    win._mode_mgr.switch_to(ANALYSIS)
    sess = win._session
    for i in (1, 4):
        win.cursor.set_sample(int(sess.commands[i]["start_sample"]))
        win.toggle_bookmark()
    win.cursor.set_sample(int(sess.commands[6]["start_sample"]))
    keep = win.cursor.sample
    win._links[0].name = "Amp bus"
    cfg = win._authoring.config()
    cfg.rows_to_draw = 40
    win._authoring.set_config(cfg)
    win._grid_rows_edit.setText("16")
    win._on_grid_rows_edit()
    timing = win._timing_page.to_dict()
    win.save_workspace()
    assert win._status.text() == f"Workspace saved: {path}"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cursor"] == keep and saved["mode"] == ANALYSIS

    win2 = _win(load=False)
    win2.open_workspace()
    pump_loads(win2)
    assert [b.display() for b in win2._bookmarks] == ["A1", "A2"]
    assert [b.sample for b in win2._bookmarks] == [b.sample for b in win._bookmarks]
    assert win2.cursor.sample == keep
    assert win2._links[0].name == "Amp bus"
    assert win2._mode_mgr.current() == ANALYSIS
    assert win2._authoring.config().rows_to_draw == 40
    assert win2._grid_rows == 16 and win2._grid_rows_edit.text() == "16"
    assert win2._timing_page.to_dict() == timing
    assert win2.import_warning_dialogs() == []


def test_a_file_that_is_not_a_workspace_is_reported(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    bad = tmp_path / "w.swi3s"
    bad.write_text("not json", encoding="utf-8")
    _dialogs(monkeypatch, bad)
    win = _win(load=False)
    win.open_workspace()
    assert [s[:2] for s in shown] == [("critical", "Open failed")]
    assert win._session is None


@pytest.fixture
def moved_capture(monkeypatch, tmp_path):
    """A workspace saved over a .sal capture, and the capture then moved away: (workspace
    path, the capture's new path)."""
    from swi3s_studio.ingest.sal_export import export_sal
    demo = _win()
    cap = tmp_path / "cap.sal"
    export_sal(demo._session.capture, str(cap))
    win = _win(load=False)
    open_via_dialog(monkeypatch, [str(cap)])
    win.open_capture()
    pump_loads(win)
    assert win._session is not None
    ws = tmp_path / "w.swi3s"
    _dialogs(monkeypatch, ws)
    win.save_workspace()
    moved = tmp_path / "elsewhere" / "cap.sal"
    moved.parent.mkdir()
    cap.rename(moved)
    return ws, moved


@pytest.mark.parametrize("answer", ["cancel", "skip"])
def test_a_capture_the_workspace_cannot_find_skipped_or_cancelled_opens_nothing(
        monkeypatch, moved_capture, answer):
    ws, _moved = moved_capture
    _dialogs(monkeypatch, ws)
    win = _win(load=False)
    asked: list = []
    monkeypatch.setattr(win, "_ask_missing_capture",
                        lambda name, saved, others: asked.append((name, os.path.basename(saved),
                                                                  others)) or answer)
    win.open_workspace()
    pump_loads(win)
    assert asked == [("Link 1", "cap.sal", 0)]
    assert win._session is None
    if answer == "skip":
        assert win._status.text() == "Open workspace: every Link was skipped; nothing opened."


def test_a_capture_the_workspace_cannot_find_can_be_located(monkeypatch, moved_capture):
    ws, moved = moved_capture
    win = _win(load=False)
    monkeypatch.setattr(win, "_ask_missing_capture", lambda *a: "locate")
    answers = iter([str(ws), str(moved)])
    titles: list = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda *a, **k: titles.append(a[1]) or (next(answers), ""))
    win.open_workspace()
    pump_loads(win)
    assert titles == ["Open workspace", "Locate cap.sal"]
    assert win._session is not None and win._session.source["path"] == str(moved)


def test_a_saved_cursor_whose_link_did_not_load_is_dropped_with_a_note():
    from swi3s_studio.workspace import Workspace
    win = _win()
    ws = Workspace.single(source=win._session.source)
    ws.bookmarks = [{"sample": 10, "label": "", "group": "A", "index": 1, "link": 0},
                    {"sample": 20, "label": "", "group": "A", "index": 2, "link": 3}]
    with import_notes.collect() as notes:
        win.apply_workspace(ws, source="w.swi3s")
    assert [b.sample for b in win._bookmarks] == [10]
    assert [n.message for n in notes] == [
        "1 saved cursor on a Link that was not opened was not restored"]


def test_an_unknown_saved_mode_is_named_and_the_mode_left(monkeypatch):
    from swi3s_studio.workspace import Workspace
    win = _win()
    mode = win._mode_mgr.current()
    ws = Workspace.single(source=win._session.source)
    ws.mode = "Sculpting"
    with import_notes.collect() as notes:
        win.apply_workspace(ws, source="w.swi3s")
    assert win._mode_mgr.current() == mode
    assert any("the saved mode 'Sculpting' is not one of" in n.message for n in notes)


def test_the_workspaces_name_is_held_only_while_it_is_applied(monkeypatch):
    from swi3s_studio.workspace import Workspace
    win = _win()
    seen: list = []
    real = win._restore_window

    def restore(saved, report=True):
        seen.append(win._ws_source)
        return real(saved, report)

    monkeypatch.setattr(win, "_restore_window", restore)
    ws = Workspace.single(source=win._session.source, view={"window": {"geometry": "",
                                                                       "docks": ""}})
    assert win._ws_source is None
    with import_notes.collect():
        win.apply_workspace(ws, source="w.swi3s")
    assert seen[:1] == ["w.swi3s"] and win._ws_source is None

    def fail(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(win, "_restore_window", fail)
    with pytest.raises(RuntimeError), import_notes.collect():
        win.apply_workspace(ws, source="w.swi3s")
    assert win._ws_source is None


def test_a_cursor_placed_while_a_workspace_loads_is_kept(monkeypatch, tmp_path):
    """A cursor placed on a Link already loaded while the workspace's other Links load is
    kept with the saved cursors, in a group of its own."""
    from conftest import export_sal_links, two_link_captures

    from swi3s_studio.workspace import LinkSpec, Workspace
    paths = []
    for name in ("a.sal", "b.sal"):
        paths.append(str(tmp_path / name))
        export_sal_links([two_link_captures()[0]], paths[-1])
    links = [LinkSpec(source={"type": "sal", "path": p, "clock_channel": 0, "data_channel": 1},
                      name=n) for p, n in zip(paths, ("Bus A", "Bus B"))]
    ws = tmp_path / "two.swi3s"
    Workspace(links=links, bookmarks=[
        {"sample": 11, "group": "A", "index": 1, "label": "", "link": 0},
        {"sample": 22, "group": "A", "index": 2, "label": "", "link": 1}]).save(str(ws))
    _dialogs(monkeypatch, ws)
    win = _win(load=False)
    load = win.load_session

    def load_and_place(session, *a, **k):
        load(session, *a, **k)
        if len(win.links) == 1:                       # the first Link: place a cursor on it
            win._bookmarks_ctl.bookmarks.add(33, link=0)
    monkeypatch.setattr(win, "load_session", load_and_place)
    win.open_workspace()
    pump_loads(win)
    assert len(win.links) == 2
    assert sorted((b.sample, b.link, b.group, b.index) for b in win._bookmarks) == [
        (11, 0, "A", 1), (22, 1, "A", 2), (33, 0, "B", 1)]
