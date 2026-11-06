"""MainWindow rules that are not tied to one pane: which table a position indexes, how a
worker's failure settles, which Link a diff is read from, what a limit tells the user, the
one limit Rows To Draw has in every box and the group separator it rejects, what a damaged
workspace reports, the column range Force Column Count offers, what a finished load leaves
behind, what an audio export replaces and the cursor it leaves, the one name each file
format goes by, and what a save path gets: the format's suffix, a confirmation before a
replacement, and a whole-file write. Offscreen Qt."""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog

from swi3s_studio import import_notes

pytestmark = pytest.mark.gui


def _win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    return win


def _flush_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def _pump_until(cond, seconds: float = 20.0) -> None:
    deadline = time.time() + seconds
    while not cond():
        assert time.time() < deadline, "the condition did not come true"
        QApplication.processEvents()
        time.sleep(0.005)


# ---- the CDS pane follows the Commands table, Commit Point rows included ----

def test_the_cds_pane_selects_the_command_the_table_rows_name(monkeypatch):
    win = _win()
    model = win._cmd_model
    rows = [model.command_at(r) for r in range(model.rowCount())]
    assert any(c.get("is_commit_point") for c in rows)       # the demo confirms commits
    first_commit = next(i for i, c in enumerate(rows) if c.get("is_commit_point"))
    assert first_commit < len(rows) - 1
    chosen = []
    monkeypatch.setattr(win._symbol_view, "select_bus_row", chosen.append)
    for idx in range(first_commit + 1, len(rows)):
        win._select_symbol_for_sample(win._starts[idx])
        assert chosen[-1] == int(rows[idx].get("bus_row", 0)), idx


# ---- a failed TX-persist scan ends its request and frees the slot ----

def test_a_failed_persist_scan_reports_no_result():
    from swi3s_studio.ui.main_window import _TxPersistWorker

    class Broken:
        def tx_persist_columns(self, _sample):
            raise RuntimeError("no data")

    token = object()
    got = []
    worker = _TxPersistWorker(Broken(), 0, token)
    worker.done.connect(lambda lit, cols, tok, err: got.append((lit, cols, tok, err)))
    worker.run()
    assert got == [(None, 0, token, "no data")]


def test_a_failed_persist_scan_names_its_error_in_the_status_bar():
    win = _win()
    token = object()
    win._tx_persist_token = token
    win._on_tx_persist_done(None, 0, token, "no data")
    assert "persistence could not be computed: no data" in win._status.text()
    win._status.setText("")
    win._on_tx_persist_done(None, 0, object(), "stale")          # a superseded request
    assert win._status.text() == ""


def test_a_failed_persist_scan_does_not_wedge_later_requests(monkeypatch):
    """A request made while a scan runs waits for it, and starts once that scan fails."""
    import threading
    win = _win()
    release = threading.Event()

    class Broken:
        def tx_persist_columns(self, _sample):
            assert release.wait(20.0)
            raise RuntimeError("no data")

    class Fine:
        calls = 0

        def tx_persist_columns(self, _sample):
            import numpy as np
            Fine.calls += 1
            return np.zeros(2, dtype=bool), 2

    drawn = []
    monkeypatch.setattr(win, "_render_tx_persist", lambda lit, cols, token: drawn.append(lit))
    win._start_tx_persist_worker(Broken(), 0, object())
    win._start_tx_persist_worker(Fine(), 0, object())          # arrives while the first runs
    assert win._tx_persist_pending is not None and Fine.calls == 0
    release.set()
    _pump_until(lambda: win._tx_persist_thread is None and Fine.calls == 1)
    assert len(drawn) == 1 and drawn[0] is not None
    win._start_tx_persist_worker(Fine(), 0, object())          # the slot is free again
    _pump_until(lambda: win._tx_persist_thread is None and Fine.calls == 2)


def test_the_persist_slot_is_held_until_its_thread_has_finished():
    win = _win()

    class Slow:
        def tx_persist_columns(self, _sample):
            import numpy as np
            time.sleep(0.2)
            return np.zeros(2, dtype=bool), 2

    token = object()
    win._start_tx_persist_worker(Slow(), 0, token)
    thread = win._tx_persist_thread
    win._on_tx_persist_done(None, 0, token)         # the scan's result has been handled
    assert win._tx_persist_thread is thread         # …but its thread has not stopped yet
    win.join_worker_threads()                       # a close now waits for it
    assert not thread.isRunning() and win._tx_persist_thread is None


# ---- Compare is read from the Link whose Registers pane shows the overlay ----

def test_compare_reads_the_link_the_registers_pane_shows(monkeypatch):
    import swi3s_studio.ui.main_window as mw
    from swi3s_studio.session import Session
    win = _win()
    first = win._session
    second = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win.load_session(second, add_link=True)
    win.set_group_link("right", 0)                    # Registers: Link 1
    win.set_group_link("grid", 1)                     # Bus Grid, and the active Link: Link 2
    assert win.links.active_index == 1 and win._pane_link["right"] == 0
    monkeypatch.setattr(mw.swi3score, "registers_from_csv", lambda _p: [(0, 0, 0)])
    seen = []
    monkeypatch.setattr(win._reg_view, "set_expected",
                        lambda _e: seen.append(("overlay", win._session)))
    real_diff = mw.register_diff
    monkeypatch.setattr(mw, "register_diff",
                        lambda *a, **k: (seen.append(("diff", win._session)),
                                         real_diff(*a, **k))[1])
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win._run_compare("expected.csv")
    assert seen == [("overlay", first), ("diff", first)]
    assert win.links.active_index == 1                 # the active Link is put back


def test_compare_shows_its_boxes_with_the_active_link_and_cursor_restored(monkeypatch):
    import swi3s_studio.ui.main_window as mw
    from swi3s_studio.session import Session
    win = _win()
    win.load_session(Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap),
                     add_link=True)
    win.set_group_link("right", 0)                    # Registers: Link 1
    win.set_group_link("grid", 1)                     # the active Link: Link 2
    assert win.links.active_index == 1 and win._pane_link["right"] == 0
    win.cursor.set_sample(200)
    # The Links count the same instant differently, so a cursor still inside the other
    # Link would read 1200 here.
    monkeypatch.setattr(win._links, "convert",
                        lambda sample, src, dst: sample if src == dst else sample + 1000)
    seen = []

    def note(*_a, **_k):
        seen.append((win.links.active_index, win.cursor.sample))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", note)
    monkeypatch.setattr(QMessageBox, "warning", note)
    monkeypatch.setattr(mw.swi3score, "registers_from_csv", lambda _p: [(0, 0x30, 5)])
    win._run_compare("expected.csv")
    monkeypatch.setattr(mw.swi3score, "registers_from_csv", lambda _p: [])
    win._run_compare("empty.csv")
    assert seen == [(1, 200), (1, 200)]


def _compared(monkeypatch, links=1):
    """A window of `links` Links whose Registers pane shows Link 1 with a Compare overlay."""
    import swi3s_studio.ui.main_window as mw
    from swi3s_studio.session import Session
    win = _win()
    for _ in range(links - 1):
        win.load_session(Session.from_demo(300, cold_start=True, phy=2,
                                           register_map=win._rmap), add_link=True)
    if links > 1:
        win.set_group_link("right", 0)
    monkeypatch.setattr(mw.swi3score, "registers_from_csv", lambda _p: [(0, 0x30, 5)])
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    win._run_compare("expected.csv")
    assert win._reg_view.has_expected()
    return win


def test_the_compare_overlay_survives_a_re_decode_of_its_link(monkeypatch):
    win = _compared(monkeypatch)
    win.load_session(win._session)                     # a re-decode of the same Link
    assert win._reg_view.has_expected()


def test_the_compare_overlay_goes_with_the_capture_it_was_made_for(monkeypatch):
    win = _compared(monkeypatch)
    win.load_demo()                                    # another capture replaces it
    assert not win._reg_view.has_expected()


@pytest.mark.parametrize("move", ["switch", "picker", "add"])
def test_the_compare_overlay_goes_when_the_registers_pane_shows_another_link(
        monkeypatch, move):
    from swi3s_studio.session import Session
    win = _compared(monkeypatch, links=2)
    if move == "switch":
        win.switch_link(1)                             # the Links menu
    elif move == "picker":
        win.set_group_link("right", 1)                 # the Registers group's picker
    else:
        win.load_session(Session.from_demo(300, cold_start=True, phy=2,
                                           register_map=win._rmap), add_link=True)
    assert win._pane_link["right"] != 0
    assert not win._reg_view.has_expected()


def test_clear_compare_removes_the_overlay_with_no_capture_loaded(monkeypatch):
    win = _compared(monkeypatch)
    win._session = None
    win.clear_comparison()
    assert not win._reg_view.has_expected()


# ---- Open Capture hands a config CSV to every loader ----

def test_a_wfm_capture_is_opened_with_the_supplied_config(monkeypatch):
    from swi3s_studio.session import Session
    win = _win()
    asked = []
    monkeypatch.setattr(win, "_load_async", lambda factory, *a, **k: asked.append(factory))
    got = {}
    monkeypatch.setattr(Session, "from_wfm", classmethod(lambda cls, *a, **k: got.update(k)))
    link = SimpleNamespace(clock=0, data=1, clock_thresh=None, data_thresh=None,
                           auto_clock=True, name="Link 1")
    pr = SimpleNamespace(kind="wfm", paths=["clk.wfm", "dat.wfm"], sample_rate_hz=0,
                         name="x")
    req = SimpleNamespace(links=[link], sample_rate_hz=0, window_s=None, add=False,
                          offset_s=0.0)
    win._open_request(pr, req, config_csv="config.csv")
    asked[0]()
    assert got["config_csv"] == "config.csv"


# ---- a limit on Rows To Draw is told to the user ----

def test_rows_over_the_authored_limit_are_named_in_the_notifications(monkeypatch):
    from swi3s_studio.ui.main_window import _AUTHORED_ROWS_MAX, _authored_rows
    assert _authored_rows(50) == (50, None)
    assert _authored_rows(_AUTHORED_ROWS_MAX) == (_AUTHORED_ROWS_MAX, None)
    rows, note = _authored_rows(5000)
    assert rows == _AUTHORED_ROWS_MAX and note is not None
    assert "5000" in note.message and str(_AUTHORED_ROWS_MAX) in note.message

    win = _win()
    cfg = win._authoring.config()
    cfg.rows_to_draw = 5000
    monkeypatch.setattr(win._authoring, "config", lambda: cfg)
    shown = []
    monkeypatch.setattr(win._authoring, "set_issues", shown.append)
    win._refresh_authored_grid()
    assert any(i.source == "Rows To Draw" for i in shown[-1])
    cfg.rows_to_draw = 40
    win._refresh_authored_grid()
    assert not any(i.source == "Rows To Draw" for i in shown[-1])


@pytest.mark.parametrize("locale", ["en_US", "de_DE"])
def test_the_rows_to_draw_box_rejects_a_group_separator(locale):
    from PySide6.QtCore import QLocale
    from PySide6.QtGui import QValidator
    saved = QLocale()
    QLocale.setDefault(QLocale(locale))
    try:
        win = _win()
    finally:
        QLocale.setDefault(saved)
    box = win._grid_rows_edit
    for text in ("1,024", "1.024"):
        assert box.validator().validate(text, 0)[0] == QValidator.Invalid
    assert box.validator().validate("1024", 0)[0] == QValidator.Acceptable


# ---- a config's column count is bounded before the Bus Grid draws it ----

@pytest.mark.parametrize("value, used", [(10 ** 12, 31), (300_000, 31), (0, 1), (-5, 1),
                                         ("lots", 15), (31, 31), (32, 31), (1, 1)])
def test_a_workspace_num_columns_outside_the_register_range_is_clamped_with_a_note(
        value, used):
    from swi3s_studio.model.bus_config import demo_config
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    from swi3s_studio.workspace import Workspace
    win = _win()
    authoring = {**demo_config().to_dict(), "num_columns": value}
    ws = Workspace.single(source=win._session.source, authoring=authoring, mode=VISUALIZATION)
    with import_notes.collect() as notes:
        win.apply_workspace(ws, source="ws.swi3s")
    assert win._authoring.config().num_columns == used
    said = [n.message for n in notes
            if "NumColumns_REG" in n.message or "num_columns" in n.message]
    assert len(said) == (0 if value == used else 1), said


@pytest.mark.parametrize("typed", [1, 33])
def test_force_column_count_offers_and_accepts_the_cores_column_range(monkeypatch, typed):
    import swi3score
    from PySide6.QtWidgets import QInputDialog
    win = _win()
    lo, hi = swi3score.min_column_count, swi3score.max_column_count
    asked, told = [], []
    monkeypatch.setattr(QInputDialog, "getInt",
                        lambda *a, **k: asked.append(a[3:6]) or (typed, True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: told.append(a[2]))
    monkeypatch.setattr(win, "_load_async",
                        lambda *a, **k: pytest.fail("an illegal count was decoded"))
    win.force_column_count()
    assert asked and asked[0][1:] == (0, hi)
    assert told == [f"{typed} is not a legal column count ({lo}–{hi})."]


@pytest.mark.parametrize("typed", ["lo", "hi", 0])
def test_force_column_count_accepts_a_legal_count_and_0_to_remove_the_pin(monkeypatch, typed):
    import swi3score
    from PySide6.QtWidgets import QInputDialog
    win = _win()
    count = {"lo": swi3score.min_column_count, "hi": swi3score.max_column_count}.get(typed, 0)
    told, decoded = [], []
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (count, True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: told.append(a[2]))
    monkeypatch.setattr(win, "_load_async", lambda apply, **k: decoded.append(apply))
    win.force_column_count()
    assert told == [] and len(decoded) == 1
    sect = win._session.segment_index_for_sample(int(win.cursor.sample))
    decoded[0]()
    assert int(win._session.forced_column_sections.get(sect, 0)) == count


def test_the_register_column_range_is_the_cores_less_one():
    import swi3score

    import swi3s_studio.ui.main_window as mw
    from swi3s_studio.swviz.config.constants import InterfaceRanges
    assert (mw._MIN_COLUMNS, mw._MAX_COLUMNS) == (swi3score.min_column_count,
                                                   swi3score.max_column_count)
    assert mw._NUM_COLUMNS_REG == (InterfaceRanges.MIN_COLUMNS_PER_ROW,
                                   InterfaceRanges.MAX_COLUMNS_PER_ROW)


def test_the_authored_grid_fallback_draws_at_most_the_registers_columns(monkeypatch):
    win = _win()
    cfg = win._authoring.config()
    cfg.num_columns = 10 ** 12                 # the engine refuses it: the fallback draws
    monkeypatch.setattr(win._authoring, "config", lambda: cfg)
    drawn, shown = [], []
    monkeypatch.setattr(win._viz_grid, "set_bus_model", lambda *a, **k: drawn.append(a[2]))
    monkeypatch.setattr(win._authoring, "set_issues", shown.append)
    win._refresh_authored_grid()
    assert drawn == [32]
    assert [i.source for i in shown[-1]] == ["Engine"]


@pytest.mark.parametrize("value", [2147483647, 32])
def test_a_settings_csv_num_columns_outside_the_register_range_is_clamped_with_a_note(
        tmp_path, value):
    from swi3s_studio.model.bus_config import demo_config
    win = _win()
    path = tmp_path / "wide.csv"
    demo_config().to_csv_file(str(path))
    text = path.read_text(encoding="utf-8")
    lines = [(f"NumColumns_REG,{value}" if line.startswith("NumColumns_REG,") else line)
             for line in text.splitlines()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with import_notes.collect() as notes:
        cfg = win._bus_config_from_csv(str(path))
    assert cfg.num_columns == 31
    assert [n.message for n in notes if "NumColumns_REG" in n.message] == [
        f"NumColumns_REG {value} is outside 1..31; it is read as 31."]


@pytest.mark.parametrize("wanted, rows, limited", [(5000, 1024, True), (40, 40, False)])
def test_the_bus_model_export_writes_at_most_the_authored_row_limit_and_says_so(
        monkeypatch, tmp_path, wanted, rows, limited):
    import json

    from swi3s_studio.model.viz_engine import MAX_AUTHORED_ROWS
    assert MAX_AUTHORED_ROWS == 1024
    win = _win()
    win._authoring._iface["rows_to_draw"][1].setText(str(wanted))   # Save flushes the entry
    path = tmp_path / "out.json"
    offered = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: offered.append(a[2]) or (str(path), "")))
    status = []
    monkeypatch.setattr(win._authoring, "set_file_status", status.append)
    win.export_bus_model_json()
    assert os.path.basename(offered[0]) == "bus_model.json"
    assert json.loads(path.read_text(encoding="utf-8"))["num_rows"] == rows
    note = f" (limited to {rows} rows)" if limited else ""
    assert win._status.text() == f"Bus model exported: {path}{note}"
    assert status == [f"Exported: out.json{note}"]


def test_a_bus_model_export_that_fails_leaves_the_previous_file(monkeypatch, tmp_path):
    import swi3s_studio.ui.main_window as mw
    win = _win()
    path = tmp_path / "bus_model.json"
    path.write_text('{"kept": true}', encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    monkeypatch.setattr(mw.viz_engine, "model_json", lambda *_a: {"bits": [object()]})
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    win.export_bus_model_json()
    assert shown and "not JSON serializable" in shown[0]
    assert path.read_text(encoding="utf-8") == '{"kept": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["bus_model.json"]


# ---- a damaged workspace reports what it drops ----

def test_an_unreadable_saved_window_layout_is_reported():
    win = _win()
    win._ws_source = "w.json"
    try:
        with import_notes.collect() as reported:
            win._restore_window({"geometry": "!!!", "docks": "!!!"})
        with import_notes.collect() as quiet:
            win._restore_window({"geometry": "!!!", "docks": "!!!"}, report=False)
    finally:
        win._ws_source = None
    assert [n.message for n in reported] == [
        "the saved window layout could not be read; the default layout is shown"]
    assert quiet == []


@pytest.mark.parametrize("saved", ["abc", [], 7])
def test_a_saved_window_entry_that_is_not_a_mapping_is_reported(saved):
    win = _win()
    win._ws_source = "w.json"
    try:
        with import_notes.collect() as notes:
            win._apply_window_view_state({"window": saved})
        with import_notes.collect() as absent:
            win._apply_window_view_state({})
    finally:
        win._ws_source = None
    assert [n.message for n in notes] == [
        "the saved window layout is not a mapping; the default layout is shown"]
    assert absent == []


def test_a_locate_that_fails_after_its_read_still_shows_the_reads_warnings(monkeypatch):
    from swi3s_studio.ui.main_window import _LocateWorker

    def load():
        import_notes.warn("ref.bin", "clamped a value")
        raise ValueError("unreadable")
    worker = _LocateWorker(None, load=load)
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == ["unreadable"]
    assert [n.message for n in worker.notes] == ["clamped a value"]

    win = _win()
    shown = []
    monkeypatch.setattr(win, "show_import_warnings", shown.append)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    win._locate_worker = worker
    win._on_locate_failed("unreadable")
    assert [[n.message for n in notes] for notes in shown] == [["clamped a value"]]


@pytest.mark.parametrize("exc, text", [(KeyError("CH3"), "KeyError: 'CH3'"),
                                       (IndexError(), "IndexError: "),
                                       (ValueError("unreadable"), "unreadable")])
def test_a_locate_failure_names_an_exception_that_has_no_text_of_its_own(exc, text):
    from swi3s_studio.ui.main_window import _LocateWorker

    def load():
        raise exc
    worker = _LocateWorker(None, load=load)
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed == [text]


# ---- a new capture starts at its first sample ----

def test_opening_a_capture_puts_the_cursor_and_the_playhead_at_its_first_sample():
    from swi3s_studio.session import Session
    win = _win()
    win.cursor.set_sample(int(win._session.last_sample()))
    assert win.cursor.sample > 0
    win.load_session(Session.from_demo(300, cold_start=True, phy=1, register_map=win._rmap))
    assert win.cursor.sample == 0 and win._timeline._cursor == 0


# ---- failures say what failed ----

def _critical_into(monkeypatch):
    said = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: said.append(a[1:3]))
    return said


def test_a_failed_re_decode_is_titled_as_one_and_names_the_error(monkeypatch):
    win = _win()
    said = _critical_into(monkeypatch)

    def broken():
        raise KeyError("missing")
    win._load_async(broken, kind="redecode")
    pump_loads(win)
    assert said == [("Re-decode failed", "KeyError: 'missing'")]


def test_a_failed_open_keeps_its_title(monkeypatch):
    win = _win()
    said = _critical_into(monkeypatch)

    def broken():
        raise ValueError("not a capture")
    win._load_async(broken)
    pump_loads(win)
    assert said == [("Open failed", "not a capture")]


def test_a_failing_after_step_is_not_reported_as_the_views_failing(monkeypatch):
    win = _win()
    said = _critical_into(monkeypatch)

    def after(_session):
        raise RuntimeError("step failed")
    session = win._session
    win._load_async(lambda: session, after=after, kind="redecode")
    pump_loads(win)
    assert len(said) == 1 and "views failed" not in said[0][1]
    assert "a step after it failed" in said[0][1]


# ---- a finished load leaves nothing behind ----

def test_loads_and_menus_do_not_accumulate_children():
    win = _win()
    _flush_deletes()
    dialogs = len(win.findChildren(QProgressDialog))
    link_actions = len(win._links_menu.findChildren(QAction))
    for _ in range(3):
        session = win._session
        win._load_async(lambda s=session: s, kind="redecode")
        pump_loads(win)
        win._update_link_switcher()
    _flush_deletes()
    assert len(win.findChildren(QProgressDialog)) == dialogs
    assert len(win._links_menu.findChildren(QAction)) == link_actions


# ---- closing ----

def test_closing_takes_down_a_search_dialog_that_is_still_up():
    win = _win()
    dlg = QProgressDialog("Searching…", None, 0, 100, win)
    dlg.show()
    win._locate_dlg = dlg
    win.join_worker_threads()
    assert win._locate_dlg is None and not dlg.isVisible()


def test_locate_waits_for_a_decode_that_is_running(monkeypatch):
    win = _win()
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[1]))

    def no_picker(*a, **k):
        raise AssertionError("the file picker opened during a decode")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", no_picker)
    win._load_thread = SimpleNamespace(quit=lambda: None, wait=lambda: None)
    try:
        win.locate_subcapture()
    finally:
        win._load_thread = None
    assert told == ["Decode running"]


def test_a_maximized_window_is_not_resized_to_fit_the_screen(monkeypatch):
    win = _win()
    win.resize(5000, 4000)
    monkeypatch.setattr(win, "isMaximized", lambda: True)
    win._fit_to_available_screen()
    assert (win.width(), win.height()) == (5000, 4000)


# ---- the SSP step keeps the audio selection only on the lane it read it from ----

def test_the_ssp_step_does_not_touch_a_lane_that_is_gone(monkeypatch):
    win = _win()
    kept = []
    lane = win._audio_lane()
    monkeypatch.setattr(lane, "set_channel_selection", kept.append)
    after_hooks = []
    monkeypatch.setattr(win, "_redecode_preserving",
                        lambda apply, status="", *, after=None: after_hooks.append(after))
    win.set_ssp_row_async(3)
    after_hooks[0]()                                              # the same lane: restored
    assert len(kept) == 1
    kept.clear()
    monkeypatch.setattr(win, "_audio_lane", lambda: object())     # the lane was replaced
    after_hooks[0]()
    assert kept == []


# ---- exporting audio does not replace files unasked ----

def _export_one_stream(win, directory, monkeypatch):
    """Answer Export Audio's dialog and folder picker with the first stream and
    `directory`; the stream exported, as (device, data port)."""
    import swi3s_studio.ui.audio_export_dialog as aed
    keep = list(win._audio_store.streams())[0]

    class Chosen:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 1

        def selected_streams(self):
            return [keep]

        def target_rate(self, *_k):
            return None

        def index_range(self, *_k):
            return None
    monkeypatch.setattr(aed, "AudioExportDialog", Chosen)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(directory))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    return keep


def test_export_audio_asks_before_replacing_a_file(tmp_path, monkeypatch):
    """A file the export would write that exists is named in a question whose default is
    No; No writes nothing, Yes replaces it."""
    win = _win()
    keep = _export_one_stream(win, tmp_path, monkeypatch)
    target = tmp_path / f"swi3s_dev{keep[0]}_dp{keep[1]}.wav"
    target.write_bytes(b"old")
    asked, written = [], []
    real = win._audio_store.export_wav
    monkeypatch.setattr(win._audio_store, "export_wav",
                        lambda *a, **k: (written.append(a[2]), real(*a, **k))[1])
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: (asked.append(a), QMessageBox.No)[1])
    win.export_audio()
    assert len(asked) == 1 and target.name in asked[0][2]
    assert asked[0][4] == QMessageBox.No                 # the default answer
    assert target.read_bytes() == b"old" and written == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.export_audio()
    assert target.read_bytes() != b"old" and written == [str(target)]
    assert QApplication.overrideCursor() is None


def test_a_failed_audio_export_puts_the_cursor_back(tmp_path, monkeypatch):
    """The wait cursor the export shows is taken down when a write fails, and the failure
    is reported."""
    win = _win()
    _export_one_stream(win, tmp_path, monkeypatch)

    def fail(*_a, **_k):
        raise OSError("the disk is full")
    monkeypatch.setattr(win._audio_store, "export_wav", fail)
    said = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: said.append(a[1:3]))
    win.export_audio()
    assert said == [("Export failed", "the disk is full")]
    assert QApplication.overrideCursor() is None


def test_an_interrupted_audio_export_puts_the_cursor_back(tmp_path, monkeypatch):
    """An interrupt (not an Exception) raised while writing still takes the wait cursor
    down, and propagates."""
    win = _win()
    _export_one_stream(win, tmp_path, monkeypatch)

    def interrupt(*_a, **_k):
        assert QApplication.overrideCursor() is not None
        raise KeyboardInterrupt
    monkeypatch.setattr(win._audio_store, "export_wav", interrupt)
    with pytest.raises(KeyboardInterrupt):
        win.export_audio()
    assert QApplication.overrideCursor() is None


# ---- one name per file format ----

def test_open_capture_names_each_format_as_export_capture_does():
    """Every format Export Capture writes is offered by Open Capture's file filter under the
    same name and pattern."""
    from swi3s_studio.ui.capture_export_dialog import _FORMATS
    from swi3s_studio.ui.main_window import _CAPTURE_FILTER
    offered = _CAPTURE_FILTER.split(";;")
    for _label, _key, _ext, pattern in _FORMATS:
        assert pattern in offered, pattern


# ---- a save path: its suffix and its replacement ----

@pytest.mark.parametrize("typed, chosen, written", [
    ("grid", "SVG (*.svg)", "grid.svg"),
    ("grid", "PNG (*.png)", "grid.png"),
    ("grid.png", "SVG (*.svg)", "grid.png"),
    ("grid.svg", "PNG (*.png)", "grid.svg"),
    ("GRID.SVG", "PNG (*.png)", "GRID.SVG"),
    ("bus grid 1.5", "SVG (*.svg)", "bus grid 1.5.svg"),
    ("bus grid 1.5", "PNG (*.png)", "bus grid 1.5.png"),
    ("grid.v2", "SVG (*.svg)", "grid.v2.svg"),
])
def test_export_grid_gives_a_name_typed_without_a_suffix_the_chosen_filters(
        tmp_path, monkeypatch, typed, chosen, written):
    win = _win()
    grid = win._grid_view
    assert grid.item_count > 0
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / typed), chosen))
    saved = []
    monkeypatch.setattr(grid, "export_image", saved.append)
    win.export_grid(grid)
    assert saved == [str(tmp_path / written)]


@pytest.mark.parametrize("answer, written", [
    (QMessageBox.No, []),
    (QMessageBox.Yes, ["grid.svg"]),
])
def test_export_grid_asks_before_an_added_suffix_replaces_an_existing_file(
        tmp_path, monkeypatch, answer, written):
    win = _win()
    grid = win._grid_view
    (tmp_path / "grid.svg").write_text("earlier export")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "grid"), "SVG (*.svg)"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or answer)
    saved = []
    monkeypatch.setattr(grid, "export_image", saved.append)
    win.export_grid(grid)
    assert len(asked) == 1 and "grid.svg" in asked[0]
    assert saved == [str(tmp_path / name) for name in written]


def test_save_workspace_asks_before_an_added_suffix_replaces_an_existing_file(
        tmp_path, monkeypatch):
    from swi3s_studio.ui.main_window import WORKSPACE_SUFFIX
    win = _win()
    existing = tmp_path / f"ws{WORKSPACE_SUFFIX}"
    existing.write_text("earlier workspace")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "ws"), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or QMessageBox.No)
    win.save_workspace()
    assert len(asked) == 1
    assert existing.read_text() == "earlier workspace"


def test_save_workspace_adds_its_suffix_to_a_dotted_name_and_keeps_a_json_one(
        tmp_path, monkeypatch):
    from swi3s_studio.ui.main_window import WORKSPACE_SUFFIX
    win = _win()
    for typed, written in (("run 1.5", f"run 1.5{WORKSPACE_SUFFIX}"),
                           ("run.json", "run.json"),
                           (f"run{WORKSPACE_SUFFIX}", f"run{WORKSPACE_SUFFIX}")):
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            lambda *a, _t=typed, **k: (str(tmp_path / _t), ""))
        win.save_workspace()
        assert (tmp_path / written).is_file(), written
    assert not (tmp_path / "run 1.5").exists()


@pytest.mark.parametrize("method, suffix", [
    ("save_authoring_csv", ".csv"),
    ("export_bus_model_json", ".json"),
    ("save_timing_settings", ".json"),
    ("export_commands", ".csv"),
    ("export_grid_csv", ".csv"),
])
@pytest.mark.parametrize("typed", ["out", "out 1.5"])
def test_every_save_dialog_adds_its_formats_suffix_to_a_name_typed_without_one(
        tmp_path, monkeypatch, method, suffix, typed):
    win = _win()
    win.cursor.set_sample(win._session.last_sample())     # past the config writes
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / typed), ""))
    getattr(win, method)()
    assert [p.name for p in tmp_path.iterdir()] == [typed + suffix]


@pytest.mark.parametrize("method, name", [
    ("save_authoring_csv", "out.csv"),
    ("export_bus_model_json", "out.json"),
    ("save_timing_settings", "out.json"),
    ("export_commands", "out.csv"),
    ("export_grid_csv", "out.csv"),
])
def test_every_save_dialog_asks_before_an_added_suffix_replaces_an_existing_file(
        tmp_path, monkeypatch, method, name):
    win = _win()
    win.cursor.set_sample(win._session.last_sample())     # past the config writes
    (tmp_path / name).write_text("earlier")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "out"), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or QMessageBox.No)
    getattr(win, method)()
    assert len(asked) == 1 and name in asked[0]
    assert [p.name for p in tmp_path.iterdir()] == [name]
    assert (tmp_path / name).read_text() == "earlier"


def test_save_timing_settings_that_fails_leaves_the_previous_file(monkeypatch, tmp_path):
    win = _win()
    path = tmp_path / "timing_settings.json"
    path.write_text('{"kept": true}', encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(path), ""))
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))

    def fail(*_a, **_k):
        raise OSError("the volume is full")
    monkeypatch.setattr(os, "replace", fail)
    win.save_timing_settings()
    assert shown and "the volume is full" in shown[0]
    assert path.read_text(encoding="utf-8") == '{"kept": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["timing_settings.json"]


# ---- Rows To Draw has one limit ----

def test_the_authoring_box_the_bus_grid_box_and_the_config_share_one_rows_limit():
    from swi3s_studio.swviz.config.constants import InterfaceRanges
    from swi3s_studio.ui.authoring.authoring_panel import _IFACE_ROWS
    from swi3s_studio.ui.main_window import MAX_GRID_ROWS
    win = _win()
    label, _attr, _kind, lo, hi, _phy3 = next(r for r in _IFACE_ROWS if r[1] == "rows_to_draw")
    assert (lo, hi) == (InterfaceRanges.MIN_ROWS, InterfaceRanges.MAX_ROWS)
    assert label == f"Rows To Draw ({lo}-{hi})"
    assert MAX_GRID_ROWS == InterfaceRanges.MAX_ROWS
    assert win._grid_rows_edit.validator().top() == InterfaceRanges.MAX_ROWS
    assert win._authoring._iface["rows_to_draw"][1].validator().top() == InterfaceRanges.MAX_ROWS


@pytest.mark.parametrize("typed, written", [("cap", "cap.sal"), ("cap 1.5", "cap 1.5.sal"),
                                            ("cap.sal", "cap.sal"), ("CAP.SAL", "CAP.SAL")])
def test_the_export_capture_browse_button_gives_a_name_the_formats_suffix(
        tmp_path, monkeypatch, typed, written):
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    win = _win()
    dlg = CaptureExportDialog(win._session, default_dir=str(tmp_path))
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / typed), ""))
    dlg._browse()
    assert dlg.output_path() == str(tmp_path / written)
    dlg.deleteLater()


def test_the_export_capture_browse_button_asks_before_an_added_suffix_replaces_a_file(
        tmp_path, monkeypatch):
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    win = _win()
    dlg = CaptureExportDialog(win._session, default_dir=str(tmp_path))
    before = dlg.output_path()
    (tmp_path / "cap.sal").write_text("earlier")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "cap"), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or QMessageBox.No)
    dlg._browse()
    assert len(asked) == 1 and "cap.sal" in asked[0]
    assert dlg.output_path() == before
    dlg.deleteLater()
