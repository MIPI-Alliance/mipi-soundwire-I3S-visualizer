"""Bookmark GUI integration: Ctrl+B pairing, the Measurements pane, propagation to the
timeline/audio/raw panes, and drag-to-move with edge snapping. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


def _win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    return win


def test_toggle_pairs_and_populates_measurement_pane():
    win = _win()
    s1 = int(win._session.commands[1]["start_sample"])
    s2 = int(win._session.commands[3]["start_sample"])
    win.cursor.set_sample(s1); win.toggle_bookmark()
    win.cursor.set_sample(s2); win.toggle_bookmark()

    assert [b.display() for b in win._bookmarks] == ["A1", "A2"]
    # one complete pair -> three Measurements rows (A1, A2, A1-A2), propagated to every pane
    assert win._pair_view.rowCount() == 3
    assert len(win._timeline._bookmarks) == 2
    assert len(win._raw_view._bm_lines) == 2
    assert len(win._audio_view._bm_marks) == 2
    win.close()


def test_toggle_at_existing_bookmark_removes_it():
    win = _win()
    s1 = int(win._session.commands[1]["start_sample"])
    win.cursor.set_sample(s1); win.toggle_bookmark()
    assert len(win._bookmarks) == 1
    win.cursor.set_sample(s1); win.toggle_bookmark()      # toggle at same spot removes
    assert len(win._bookmarks) == 0
    assert win._pair_view.rowCount() == 0
    win.close()


def test_drag_moves_bookmark_and_snaps_to_edge():
    win = _win()
    s1 = int(win._session.commands[1]["start_sample"])
    s2 = int(win._session.commands[3]["start_sample"])
    win.cursor.set_sample(s1); win.toggle_bookmark()      # A1
    win.cursor.set_sample(s2); win.toggle_bookmark()      # A2

    # Drag A2 to a raw sample that is NOT on an edge; final=True must snap it to the
    # nearest clock edge, and the model must reflect the move.
    ce = win._session.capture.clock_edges
    target_edge = int(ce[len(ce) // 2])
    win._on_bookmark_moved("A2", target_edge + 1, True)   # 1 sample off an edge
    a2 = next(b for b in win._bookmarks if b.display() == "A2")
    assert a2.sample == target_edge                        # snapped onto the edge
    win.close()


def test_clear_bookmarks_empties_all_panes():
    win = _win()
    win.cursor.set_sample(int(win._session.commands[1]["start_sample"])); win.toggle_bookmark()
    win.clear_bookmarks()
    assert len(win._bookmarks) == 0
    assert win._pair_view.rowCount() == 0
    assert win._timeline._bookmarks == []
    win.close()
