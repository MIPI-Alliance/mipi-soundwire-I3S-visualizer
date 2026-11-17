"""Locate Sub-Capture, through the window: a slice of the open capture is found and each
match marked with a labelled cursor on the Link searched, with the count in the status bar
and a box with no icon; no match marks the best candidate, or nothing when there is none;
results for a capture no longer open are discarded; what is refused before the file picker
and before the search; a result delivered while the window closes places nothing.
Offscreen Qt."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import open_via_dialog
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

pytestmark = pytest.mark.gui


def _win(load: bool = True):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _boxes(monkeypatch) -> list:
    """(kind, title, text) of every static message box, and ("exec", text, icon) of every
    box exec'd."""
    shown: list = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *a, k=kind, **_kw: shown.append((k, a[1], a[2]))
                            or QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(
        ("exec", box.text(), box.icon())) or 0)
    return shown


def _wait_for_search(win, seconds: float = 60.0) -> None:
    deadline = time.time() + seconds
    while win._locate_thread is not None:
        assert time.time() < deadline, "the search never finished"
        QApplication.processEvents()
        time.sleep(0.01)


def _reference(tmp_path, win):
    """A .sal holding the first 0.5 ms of the open capture."""
    from swi3s_studio.ingest.sal_export import export_sal
    main = win._session.capture
    path = str(tmp_path / "ref.sal")
    export_sal(main.subcapture(0, main.sample_rate_hz // 2000), path)
    return path


def test_a_slice_of_the_open_capture_is_found_and_each_match_marked(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QProgressDialog
    shown = _boxes(monkeypatch)
    win = _win()
    ref = _reference(tmp_path, win)
    open_via_dialog(monkeypatch, [ref])
    dialogs: list = []
    real_show = QProgressDialog.show
    monkeypatch.setattr(QProgressDialog, "show",
                        lambda dlg: dialogs.append(dlg.windowTitle()) or real_show(dlg))
    win.locate_subcapture()
    assert win._locate_thread is not None and win._locate_session is win._session
    _wait_for_search(win)
    assert dialogs == ["Locate Sub-Capture"]
    marks = list(win._bookmarks)
    assert marks, shown
    n = len(marks)
    assert all(b.link == 0 for b in marks)
    assert [b.label.split("  ")[0] for b in marks] == [f"Sub {i}/{n}" for i in range(1, n + 1)]
    assert all(b.label.split("  ")[1].startswith("score=") for b in marks)
    # The slice recurs far more often than the 64 the list holds, and the box says so.
    assert n == 64
    status = win._status.text()
    assert status.startswith("Locate Sub-Capture: ") and status.endswith(
        f" matches, of which {n} marked with cursors from ref.sal"), status
    ((kind, text, icon),) = shown
    assert (kind, icon) == ("exec", QMessageBox.Icon.NoIcon)
    assert text.startswith("Found ") and f" matches, of which {n} are marked;" in text
    assert text.endswith("More matches exist than the list holds."), text
    assert (win._locate_thread, win._locate_worker, win._locate_dlg,
            win._locate_session) == (None, None, None, None)
    assert win._timeline._bookmarks == [(int(b.sample), b.display()) for b in marks]


def test_a_result_cut_by_the_match_limit_says_so_in_the_box_and_the_status(monkeypatch):
    shown = _boxes(monkeypatch)
    win = _win()
    win._locate_session = win._session
    matches = [{"start_sample": 10 * i, "score": 1.0} for i in range(1, 5)]
    win._finish_locate_subcapture("ref.sal", matches, {"total_matches": 500, "truncated": True,
                                                       "hits_capped": False})
    assert shown == [("exec", "Found 500 matches, of which 4 are marked; cursors placed at each "
                              "marked start sample. More matches exist than the list holds.",
                      QMessageBox.Icon.NoIcon)]
    assert win._status.text() == ("Locate Sub-Capture: 500 matches, of which 4 marked with "
                                  "cursors from ref.sal")
    shown.clear()
    win._locate_session = win._session
    win._finish_locate_subcapture("ref.sal", matches, {"total_matches": 4, "truncated": True,
                                                       "hits_capped": True})
    assert shown[0][1].startswith("Found at least 4 matches, of which 4 are marked;")


def test_no_match_marks_the_best_candidate_and_says_how_close_it_was(monkeypatch):
    from swi3s_studio.analysis.subcapture import DEFAULT_SCORE_THRESHOLD
    shown = _boxes(monkeypatch)
    win = _win()
    win._locate_session = win._session
    win._finish_locate_subcapture("/x/ref.sal", [], {"best_score": 0.42,
                                                     "best_start_sample": 100})
    (bm,) = list(win._bookmarks)
    assert (bm.sample, bm.label) == (100, "Best (no match)  score=0.42")
    (box,) = shown
    assert box[:2] == ("information", "Locate Sub-Capture")
    assert box[2].startswith("No occurrences of ref.sal found.\n\nBest candidate scored "
                             f"42.0% (needs ≥ {DEFAULT_SCORE_THRESHOLD * 100:.0f}%")
    assert box[2].endswith("A cursor was placed at the best candidate so you can inspect it.")
    assert win._status.text() == "Locate Sub-Capture: no matches (best 42.0%)"
    assert win._locate_session is None


def test_no_match_and_no_candidate_places_no_cursor(monkeypatch):
    shown = _boxes(monkeypatch)
    win = _win()
    win._locate_session = win._session
    win._finish_locate_subcapture("ref.sal", [], {})
    assert len(win._bookmarks) == 0
    assert "A cursor was placed" not in shown[0][2]
    assert win._status.text() == "Locate Sub-Capture: no matches (best 0.0%)"


def test_results_for_a_capture_no_longer_open_are_discarded(monkeypatch):
    from swi3s_studio.session import Session
    shown = _boxes(monkeypatch)
    win = _win()
    win._locate_session = Session.from_demo(300, cold_start=True, phy=2,
                                            register_map=win._rmap)
    win._finish_locate_subcapture("/x/ref.sal", [{"start_sample": 5, "score": 1.0}], {})
    assert len(win._bookmarks) == 0 and shown == []
    assert win._status.text() == ("Locate Sub-Capture: results for ref.sal discarded; the "
                                  "capture searched is no longer open")


def test_a_result_already_handled_is_not_handled_again(monkeypatch):
    shown = _boxes(monkeypatch)
    win = _win()
    status = win._status.text()
    win._finish_locate_subcapture("ref.sal", [{"start_sample": 5, "score": 1.0}], {})
    assert len(win._bookmarks) == 0 and shown == [] and win._status.text() == status


def test_with_no_capture_open_locate_asks_for_one(monkeypatch):
    shown = _boxes(monkeypatch)
    asked: list = []
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        lambda *a, **k: asked.append(1) or ([], ""))
    win = _win(load=False)
    win.locate_subcapture()
    assert shown == [("information", "No capture", "Open a capture first.")]
    assert asked == []


def test_a_cancelled_file_picker_starts_no_search(monkeypatch):
    shown = _boxes(monkeypatch)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([], ""))
    win = _win()
    win.locate_subcapture()
    assert win._locate_thread is None and shown == []


def test_a_cancelled_reference_dialog_starts_no_search(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    win = _win()
    open_via_dialog(monkeypatch, [_reference(tmp_path, win)], edit=lambda dlg: False)
    win.locate_subcapture()
    assert win._locate_thread is None and shown == []


def test_a_file_that_is_not_a_capture_is_reported(monkeypatch, tmp_path):
    shown = _boxes(monkeypatch)
    bad = tmp_path / "ref.sal"
    bad.write_bytes(b"not a capture")
    win = _win()
    open_via_dialog(monkeypatch, [str(bad)])
    win.locate_subcapture()
    assert win._locate_thread is None
    assert [s[:2] for s in shown] == [("warning", "Locate Sub-Capture")]


def test_a_result_delivered_while_the_window_closes_places_nothing(monkeypatch):
    shown = _boxes(monkeypatch)
    win = _win()
    win._locate_session = win._session
    win._closing = True
    win._on_locate_done([{"start_sample": 5, "score": 1.0}], {})
    assert len(win._bookmarks) == 0 and shown == [] and win._locate_session is None
    win._locate_session = win._session
    win._on_locate_failed("gone")
    assert shown == [] and win._locate_session is None
    win._closing = False
