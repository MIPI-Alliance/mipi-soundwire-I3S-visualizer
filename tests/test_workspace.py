"""Workspace save/load test: round-trip the JSON model and verify rebuilding a
session from a source descriptor, plus the full GUI save -> open cycle restores
bookmarks, the cursor, and the what-if register overlay.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_workspace.py
"""
import os
import struct
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.workspace import Workspace, session_from_source


def _write_synthetic_wfm(path: str, codes, dt: float = 1e-9, t0: float = 0.0,
                         v_scale: float = 1.0, v_offset: float = 0.0) -> None:
    """Minimal, valid Tektronix .wfm (version 3) file — just enough of the header
    fields wfm.read_wfm actually parses (see swi3s_studio/ingest/wfm.py's layout
    reference) to exercise Session.from_wfm without a real scope export."""
    codes = np.asarray(codes, dtype="<i2")
    bytes_per_point = 2
    curve_buffer_start = 0x346          # arbitrary, past every fixed-offset field used
    data_start = 0
    postcharge_start = codes.size * bytes_per_point
    buf = bytearray(curve_buffer_start + postcharge_start)
    struct.pack_into("<H", buf, 0x000, 0x0F0F)                  # little-endian BOM
    buf[0x002:0x002 + 8] = b":WFM#003"
    struct.pack_into("<B", buf, 0x00F, bytes_per_point)
    struct.pack_into("<i", buf, 0x010, curve_buffer_start)
    struct.pack_into("<i", buf, 0x07A, 2)                       # data_type: single-valued
    struct.pack_into("<d", buf, 0x0A8, v_scale)
    struct.pack_into("<d", buf, 0x0B0, v_offset)
    struct.pack_into("<i", buf, 0x0F0, 0)                       # v_format: int16
    struct.pack_into("<d", buf, 0x1E8, dt)
    struct.pack_into("<d", buf, 0x1F0, t0)
    struct.pack_into("<I", buf, 0x336, data_start)
    struct.pack_into("<I", buf, 0x33A, postcharge_start)
    buf[curve_buffer_start + data_start:curve_buffer_start + postcharge_start] = codes.tobytes()
    with open(path, "wb") as f:
        f.write(buf)


def _square_wave_codes(n: int, period: int, hi: int = 1000, lo: int = -1000):
    codes = np.full(n, lo, dtype="<i2")
    codes[(np.arange(n) // (period // 2)) % 2 == 1] = hi
    return codes


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


def test_session_from_source_analog_csv_roundtrip():
    """A workspace saved after opening an analog CSV must be reopenable (Open
    Workspace) — session_from_source needs an "analog_csv" branch."""
    n = 4000
    t = (np.arange(n, dtype=np.float64) - 100) * 1e-9
    clock = _square_wave_codes(n, period=20).astype(np.float64) / 1000.0   # +-1V square
    data = _square_wave_codes(n, period=200).astype(np.float64) / 1000.0
    text = ("Model,MSO58\nSample Interval,1e-9\n\nTIME,CH1,CH2\n" +
           "\n".join(f"{t[i]:.9e},{clock[i]:.6e},{data[i]:.6e}" for i in range(n)) + "\n")
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "capture.csv")
        with open(csv_path, "w") as f:
            f.write(text)

        orig = Session.from_analog_csv(csv_path, clock="CH1", data="CH2")
        assert orig.source["type"] == "analog_csv"

        ws_path = os.path.join(d, "ws.json")
        Workspace(source=orig.source).save(ws_path)
        loaded = Workspace.load(ws_path)
        reopened = session_from_source(loaded.source)

    assert reopened.source["type"] == "analog_csv"
    assert reopened.capture.clock_edges.size == orig.capture.clock_edges.size
    assert reopened.capture.data_edges.size == orig.capture.data_edges.size


def test_session_from_source_wfm_roundtrip_synthetic():
    """A workspace saved after opening a .wfm pair must be reopenable —
    session_from_source needs a "wfm" branch. Uses synthetic .wfm files (no real
    scope export required) so this runs everywhere."""
    n = 4000
    clk_codes = _square_wave_codes(n, period=20)     # busier -> auto-picked as clock
    dat_codes = _square_wave_codes(n, period=200)
    with tempfile.TemporaryDirectory() as d:
        clock_path = os.path.join(d, "ch1.wfm")
        data_path = os.path.join(d, "ch2.wfm")
        _write_synthetic_wfm(clock_path, clk_codes, dt=1e-9, v_scale=1.0 / 1000.0)
        _write_synthetic_wfm(data_path, dat_codes, dt=1e-9, v_scale=1.0 / 1000.0)

        orig = Session.from_wfm(clock_path, data_path)
        assert orig.source["type"] == "wfm"

        ws_path = os.path.join(d, "ws.json")
        Workspace(source=orig.source).save(ws_path)
        loaded = Workspace.load(ws_path)
        reopened = session_from_source(loaded.source)

    assert reopened.source["type"] == "wfm"
    assert reopened.capture.clock_edges.size == orig.capture.clock_edges.size
    assert reopened.capture.data_edges.size == orig.capture.data_edges.size


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
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()

    # Make some state: two bookmarks (one pair A1/A2) and a cursor near the end.
    win._bookmarks.add(int(win._session.commands[1]["start_sample"]))
    win._bookmarks.add(int(win._session.commands[3]["start_sample"]))
    win.cursor.set_sample(win._session.commands[-1]["start_sample"])

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        # Save directly via the Workspace model (avoids the file dialog).
        ws = Workspace(source=win._session.source,
                       bookmarks=win._bookmarks.to_json(),
                       cursor=win.cursor.sample)
        ws.save(path)

        # Fresh window, open the workspace.
        win2 = MainWindow()
        loaded = Workspace.load(path)
        from swi3s_studio.workspace import session_from_source as sfs
        win2.load_session(sfs(loaded.source, register_map=win2._rmap))
        win2.apply_workspace(loaded)

    assert win2._bookmarks.samples() == win._bookmarks.samples()
    assert [b.display() for b in win2._bookmarks] == ["A1", "A2"]
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
            # Wall-clock wait, not a fixed iteration count: processEvents() returns
            # instantly when the queue is empty, so `for _ in range(N)` can elapse before
            # a slow runner's decode worker finishes (flaked on the Windows VM). Poll on
            # real time with a generous ceiling instead.
            deadline = time.time() + 60.0
            while getattr(win2, "_load_thread", None) is not None:
                if time.time() > deadline:
                    raise AssertionError("workspace load did not finish")
                app.processEvents()
                time.sleep(0.005)
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
            # Wall-clock wait, not a fixed iteration count: processEvents() returns
            # instantly when the queue is empty, so `for _ in range(N)` can elapse before
            # a slow runner's decode worker finishes (flaked on the Windows VM). Poll on
            # real time with a generous ceiling instead.
            deadline = time.time() + 60.0
            while getattr(win2, "_load_thread", None) is not None:
                if time.time() > deadline:
                    raise AssertionError("workspace load did not finish")
                app.processEvents()
                time.sleep(0.005)
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
            # Wall-clock wait, not a fixed iteration count: processEvents() returns
            # instantly when the queue is empty, so `for _ in range(N)` can elapse before
            # a slow runner's decode worker finishes (flaked on the Windows VM). Poll on
            # real time with a generous ceiling instead.
            deadline = time.time() + 60.0
            while getattr(win2, "_load_thread", None) is not None:
                if time.time() > deadline:
                    raise AssertionError("workspace load did not finish")
                app.processEvents()
                time.sleep(0.005)
        finally:
            QFileDialog.getSaveFileName = save_orig
            QFileDialog.getOpenFileName = open_orig

    assert win2._tx_map and win2._tx_persist and win2._grid_rows == 32
    assert win2._raw_view.show_clock is False


def test_view_prefs_no_stale_state_in_reused_window():
    """open_workspace REUSES the current window. Applying a workspace with TX map off
    (or a pre-v2 empty view) must clear any TX-map/persist/clock state left from a
    previously-viewed capture — not leak it onto the new one."""
    _app = QApplication.instance() or QApplication([])
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
