"""Workspace save/load test: round-trip the JSON model and verify rebuilding a
session from a source descriptor, plus the full GUI save -> open cycle restores
bookmarks, the cursor, and the what-if register overlay.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_workspace.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio.workspace import Workspace, session_from_source
from swi3s_studio.ui.main_window import MainWindow


def test_workspace_json_roundtrip():
    ws = Workspace(source={"type": "demo", "audio_samples_per_channel": 8},
                   overlay=[[0, 0, 0x2090, 0x02]], bookmarks=[10, 20], cursor=42)
    ws2 = Workspace.from_json(ws.to_json())
    assert ws2.source == ws.source
    assert ws2.overlay == [[0, 0, 0x2090, 0x02]]     # [section, device, address, value]
    assert ws2.bookmarks == [10, 20]
    assert ws2.cursor == 42


def test_session_from_source():
    sess = session_from_source({"type": "demo", "audio_samples_per_channel": 8})
    assert sess.column_count == 16
    assert sess.source["type"] == "demo"


def test_from_json_rejects_non_workspace():
    # A JSON file that isn't a workspace (no 'source' dict) must raise a clear
    # ValueError, not a bare KeyError.
    for text in ('{"hello": 1}', '[]', '"str"', '{"source": 42}'):
        try:
            Workspace.from_json(text)
            assert False, f"expected ValueError for {text!r}"
        except ValueError:
            pass


def test_gui_save_open_restores_state():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()

    # Make some state: two bookmarks and a cursor near the end.
    win._bookmarks = {win._session.commands[1]["start_sample"],
                      win._session.commands[3]["start_sample"]}
    win.cursor.set_sample(win._session.commands[-1]["start_sample"])

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        # Save directly via the Workspace model (avoids the file dialog).
        ws = Workspace(source=win._session.source,
                       bookmarks=sorted(win._bookmarks),
                       cursor=win.cursor.sample)
        ws.save(path)

        # Fresh window, open the workspace.
        win2 = MainWindow()
        loaded = Workspace.load(path)
        from swi3s_studio.workspace import session_from_source as sfs
        win2.load_session(sfs(loaded.source, register_map=win2._rmap))
        win2.apply_workspace(loaded)

    assert win2._bookmarks == set(ws.bookmarks)
    assert win2.cursor.sample == ws.cursor


def test_gui_save_open_restores_register_overlay():
    """The what-if register overlay round-trips: save it in the workspace and the
    real open_workspace factory re-applies it (patched dialogs + event pump for the
    off-thread decode)."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # A what-if override on section 0 (device 0, DP0 EnableCh_L @ 0x2090).
    win._session.register_overrides[(0, 0, 0x2090)] = 0x02

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        save_orig, open_orig = QFileDialog.getSaveFileName, QFileDialog.getOpenFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        try:
            win.save_workspace()
            assert [0, 0, 0x2090, 0x02] in Workspace.load(path).overlay   # saved

            win2 = MainWindow()
            win2.open_workspace()                        # async decode on a worker thread
            for _ in range(4000):
                if getattr(win2, "_load_thread", None) is None:
                    break
                app.processEvents()
            else:
                raise AssertionError("workspace load did not finish")
        finally:
            QFileDialog.getSaveFileName = save_orig
            QFileDialog.getOpenFileName = open_orig

    assert win2._session.register_overrides.get((0, 0, 0x2090)) == 0x02   # re-applied


def test_gui_save_open_restores_scrambler_overrides():
    """Per-(device, dp) scrambler overrides are a decode input (they change the
    reconstructed audio), so they must round-trip like hub depths — save in the
    workspace and re-apply via the real open_workspace factory."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    win._session.set_scrambler_overrides({(0, 2): False, (1, 3): True})

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        save_orig, open_orig = QFileDialog.getSaveFileName, QFileDialog.getOpenFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        try:
            win.save_workspace()
            saved = Workspace.load(path).device_scramblers
            assert [0, 2, False] in saved and [1, 3, True] in saved, saved

            win2 = MainWindow()
            win2.open_workspace()                        # async decode on a worker thread
            for _ in range(4000):
                if getattr(win2, "_load_thread", None) is None:
                    break
                app.processEvents()
            else:
                raise AssertionError("workspace load did not finish")
        finally:
            QFileDialog.getSaveFileName = save_orig
            QFileDialog.getOpenFileName = open_orig

    assert win2._session.scrambler_overrides == {(0, 2): False, (1, 3): True}   # re-applied


def test_gui_save_open_restores_view_prefs():
    """v2 workspace round-trips the transient TX-map + Hide-Clock pane state."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # Set a distinctive view state: TX map on + persistence, 32 rows, clock hidden.
    win._grid_rows_edit.setText("32"); win._on_grid_rows_edit()
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)
    win._raw_view.set_show_clock(False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        save_orig, open_orig = QFileDialog.getSaveFileName, QFileDialog.getOpenFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        try:
            win.save_workspace()
            saved = Workspace.load(path).view
            assert saved["tx_map"] and saved["tx_persist"] and saved["grid_rows"] == 32
            assert saved["show_clock"] is False

            win2 = MainWindow()
            win2.open_workspace()
            for _ in range(4000):
                if getattr(win2, "_load_thread", None) is None:
                    break
                app.processEvents()
            else:
                raise AssertionError("workspace load did not finish")
        finally:
            QFileDialog.getSaveFileName = save_orig
            QFileDialog.getOpenFileName = open_orig

    assert win2._tx_map and win2._tx_persist and win2._grid_rows == 32
    assert win2._raw_view.show_clock is False


def test_view_prefs_no_stale_state_in_reused_window():
    """open_workspace REUSES the current window. Applying a workspace with TX map off
    (or a pre-v2 empty view) must clear any TX-map/persist/clock state left from a
    previously-viewed capture — not leak it onto the new one."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # Dirty the window: TX map on + persistence + clock hidden.
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)
    win._raw_view.set_show_clock(False)
    assert win._tx_map and win._tx_persist

    # Apply a v2 workspace whose view is all-default (TX off), and a v1 (empty view).
    from swi3s_studio.workspace import Workspace
    for view in ({"tx_map": False, "tx_persist": False, "show_clock": True}, {}):
        win.apply_workspace(Workspace(source=win._session.source, view=view))
        assert not win._tx_map, f"TX map leaked (view={view})"
        assert not win._tx_persist, f"persist leaked (view={view})"
        assert not win._persist_btn.isChecked()
        assert win._raw_view.show_clock is True, f"clock state leaked (view={view})"

    # A corrupt grid_rows must not raise (mode switch below must still run).
    win.apply_workspace(Workspace(source=win._session.source,
                                  view={"grid_rows": "oops"}, mode="Analysis"))
    assert 1 <= win._grid_rows <= 10240


def test_toggle_off_clears_persist():
    """Turning the TX map off clears persistence so {tx_map:False, tx_persist:True} is
    never a live/saved state."""
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)
    assert win._tx_persist
    win._toggles_btn.setChecked(False)
    assert not win._tx_persist and not win._persist_btn.isChecked()


if __name__ == "__main__":
    test_workspace_json_roundtrip(); print("ok: workspace JSON round-trip")
    test_session_from_source(); print("ok: session rebuilt from source descriptor")
    test_from_json_rejects_non_workspace(); print("ok: from_json rejects a non-workspace JSON")
    test_gui_save_open_restores_state(); print("ok: GUI save -> open restores bookmarks/cursor")
    test_gui_save_open_restores_register_overlay(); print("ok: GUI save -> open restores register overlay")
    test_gui_save_open_restores_scrambler_overrides(); print("ok: GUI save -> open restores scrambler overrides")
    test_gui_save_open_restores_view_prefs(); print("ok: GUI save -> open restores view prefs")
    test_view_prefs_no_stale_state_in_reused_window(); print("ok: no stale view state in reused window")
    test_toggle_off_clears_persist(); print("ok: toggle off clears persist")
    print("ALL WORKSPACE TESTS PASSED")
