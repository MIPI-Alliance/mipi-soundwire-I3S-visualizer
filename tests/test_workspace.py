"""Workspace save/load test: round-trip the JSON model and verify rebuilding a
session from a source descriptor, plus the full GUI save -> open cycle restores
bookmarks, the cursor, and the what-if register overlay.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_workspace.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QFileDialog
from test_analog_import import _square_wave_codes, _write_synthetic_wfm

from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.workspace import Workspace, session_from_source

pytestmark = pytest.mark.gui


def test_workspace_json_roundtrip():
    ws = Workspace.single(source={"type": "demo", "audio_samples_per_channel": 8},
                          overlay=[[0, 0, 0x2090, 0x02]], bookmarks=[10, 20], cursor=42)
    ws2 = Workspace.from_json(ws.to_json())
    assert ws2.links[0].source == ws.links[0].source
    assert ws2.links[0].overlay == [[0, 0, 0x2090, 0x02]]     # [section, device, address, value]
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
    text = ("Model,SCOPE1\nSample Interval,1e-9\n\nTIME,CH1,CH2\n" +
           "\n".join(f"{t[i]:.9e},{clock[i]:.6e},{data[i]:.6e}" for i in range(n)) + "\n")
    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "capture.csv")
        with open(csv_path, "w") as f:
            f.write(text)

        orig = Session.from_analog_csv(csv_path, clock="CH1", data="CH2")
        assert orig.source["type"] == "analog_csv"

        ws_path = os.path.join(d, "ws.json")
        Workspace.single(source=orig.source).save(ws_path)
        loaded = Workspace.load(ws_path)
        reopened = session_from_source(loaded.links[0].source)

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
        Workspace.single(source=orig.source).save(ws_path)
        loaded = Workspace.load(ws_path)
        reopened = session_from_source(loaded.links[0].source)

    assert reopened.source["type"] == "wfm"
    assert reopened.capture.clock_edges.size == orig.capture.clock_edges.size
    assert reopened.capture.data_edges.size == orig.capture.data_edges.size


def test_from_json_rejects_non_workspace():
    # A JSON file that isn't a workspace (no 'source' dict) must raise a clear
    # ValueError, not a bare KeyError.
    for text in ('{"hello": 1}', '[]', '"str"', '{"source": 42}'):
        with pytest.raises(ValueError):
            Workspace.from_json(text)


def test_gui_save_open_restores_state(monkeypatch):
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()

    # Make some state: two bookmarks (one pair A1/A2) and a cursor near the end.
    win._bookmarks.add(int(win._session.commands[1]["start_sample"]))
    win._bookmarks.add(int(win._session.commands[3]["start_sample"]))
    win.cursor.set_sample(win._session.commands[-1]["start_sample"])
    cursor = win.cursor.sample

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
        win.save_workspace()
        saved = Workspace.load(path)
        assert saved.cursor == cursor
        assert saved.bookmarks == win._bookmarks.to_json()

        win2 = MainWindow()
        win2.open_workspace()
        pump_loads(win2, seconds=60.0)

    assert win2._bookmarks.samples() == win._bookmarks.samples()
    assert [b.display() for b in win2._bookmarks] == ["A1", "A2"]
    assert win2.cursor.sample == cursor


def test_gui_save_open_restores_register_overlay(monkeypatch):
    """The what-if register overlay round-trips: save it in the workspace and the
    real open_workspace factory re-applies it (patched dialogs + event pump for the
    off-thread decode)."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # A what-if override on section 0 (device 0, DP0 EnableCh_L @ 0x2090).
    win._session.register_overrides[(0, 0, 0x2090)] = 0x02

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
        win.save_workspace()
        assert [0, 0, 0x2090, 0x02] in Workspace.load(path).links[0].overlay   # saved

        win2 = MainWindow()
        win2.open_workspace()                        # async decode on a worker thread
        pump_loads(win2, seconds=60.0)

    assert win2._session.register_overrides.get((0, 0, 0x2090)) == 0x02   # re-applied


def test_gui_save_open_restores_scrambler_overrides(monkeypatch):
    """Per-(device, dp) scrambler overrides are a decode input (they change the
    reconstructed audio), so they must round-trip like hub depths — save in the
    workspace and re-apply via the real open_workspace factory."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    win._session.set_scrambler_overrides({(0, 2): False, (1, 3): True})

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
        win.save_workspace()
        saved = Workspace.load(path).links[0].device_scramblers
        assert [0, 2, False] in saved and [1, 3, True] in saved, saved

        win2 = MainWindow()
        win2.open_workspace()                        # async decode on a worker thread
        pump_loads(win2, seconds=60.0)

    assert win2._session.scrambler_overrides == {(0, 2): False, (1, 3): True}   # re-applied


def test_gui_save_open_restores_view_prefs(monkeypatch):
    """A saved workspace round-trips the TX-map, persistence, Rows-To-Draw and Show-Clock view state."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # Set a distinctive view state: TX map on + persistence, 32 rows, clock hidden.
    win._grid_rows_edit.setText("32"); win._on_grid_rows_edit()
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)
    win._raw_view.set_show_clock(False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ws.json")
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
        win.save_workspace()
        saved = Workspace.load(path).view
        assert saved["tx_map"] and saved["tx_persist"] and saved["grid_rows"] == 32
        assert saved["show_clock"] is False

        win2 = MainWindow()
        win2.open_workspace()
        pump_loads(win2, seconds=60.0)

    assert win2._tx_map and win2._tx_persist and win2._grid_rows == 32
    assert win2._raw_view.show_clock is False


def test_view_prefs_no_stale_state_in_reused_window():
    """open_workspace REUSES the current window. Applying a workspace with TX map off
    (or an empty view) must clear any TX-map/persist/clock state left from a
    previously-viewed capture — not leak it onto the new one."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    # Dirty the window: TX map on + persistence + clock hidden.
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)
    win._raw_view.set_show_clock(False)
    assert win._tx_map and win._tx_persist

    # Apply a workspace whose view is all-default (TX off), and one with an empty view.
    from swi3s_studio.workspace import Workspace
    for view in ({"tx_map": False, "tx_persist": False, "show_clock": True}, {}):
        win.apply_workspace(Workspace.single(source=win._session.source, view=view))
        assert not win._tx_map, f"TX map leaked (view={view})"
        assert not win._tx_persist, f"persist leaked (view={view})"
        assert not win._persist_btn.isChecked()
        assert win._raw_view.show_clock is True, f"clock state leaked (view={view})"

    # A corrupt grid_rows must not raise (the mode switch after it still runs): it is
    # reported, and the default is used, not the value the window had.
    from swi3s_studio import import_notes
    from swi3s_studio.ui.main_window import DEFAULT_GRID_ROWS
    from swi3s_studio.ui.mode_controller import ANALYSIS, TIMING
    win.apply_workspace(Workspace.single(source=win._session.source, view={"grid_rows": 32},
                                         mode=TIMING))
    assert win._grid_rows == 32 != DEFAULT_GRID_ROWS and win._mode_mgr.current() == TIMING
    with import_notes.collect() as notes:
        win.apply_workspace(Workspace.single(source=win._session.source,
                                             view={"grid_rows": "oops"}, mode="Analysis"),
                            source="ws.swi3s")
    assert [n.message for n in notes] == [
        f"the saved 'grid_rows' 'oops' is not a number; {DEFAULT_GRID_ROWS} is used"]
    assert win._grid_rows == DEFAULT_GRID_ROWS
    assert win._mode_mgr.current() == ANALYSIS


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


_V3 = {
    "version": 3, "source": {"type": "demo"}, "overlay": [[0, 0, 0x2090, 0x02]],
    "bookmarks": [{"sample": 10, "group": "A", "index": 1, "label": ""}], "cursor": 42,
    "mode": "Analysis", "device_names": {"0": "Codec"}, "device_hub_depths": {"2": 1},
    "device_regmaps": {}, "device_scramblers": [[0, 1, True]], "view": {"tx_map": True},
}


def test_a_v3_workspace_loads_as_one_link_at_offset_0():
    import json
    ws = Workspace.from_json(json.dumps(_V3))
    (link,) = ws.links
    assert link.source == {"type": "demo"} and link.name == "Link 1" and link.offset_ps == 0
    assert link.overlay == [[0, 0, 0x2090, 0x02]]
    assert link.device_names == {"0": "Codec"} and link.device_hub_depths == {"2": 1}
    assert link.device_scramblers == [[0, 1, True]]
    assert ws.active_link == 0 and ws.cursor == 42 and ws.view == {"tx_map": True}
    # Re-saving writes the CURRENT layout, not a v3 label on a v4 body.
    again = json.loads(ws.to_json())
    assert again["version"] == 4 and "source" not in again
    assert again["links"][0]["overlay"] == [[0, 0, 0x2090, 0x02]]


def test_a_v4_workspace_round_trips_every_link():
    from swi3s_studio.workspace import LinkSpec
    ws = Workspace(links=[LinkSpec(source={"type": "demo"}),
                          LinkSpec(source={"type": "demo", "x": 1}, name="Amp bus",
                                   offset_ps=-1_250_000, device_names={"3": "Amp"})],
                   active_link=1, bookmarks=[{"sample": 7, "group": "A", "index": 1,
                                              "label": "", "link": 1}])
    back = Workspace.from_json(ws.to_json())
    assert back == ws


def test_from_json_rejects_a_broken_link_list():
    import json
    for bad in ({"links": []}, {"links": [{"name": "no source"}]},
                {"links": [{"source": {"type": "demo"}}], "active_link": 1}):
        with pytest.raises(ValueError):
            Workspace.from_json(json.dumps(bad))


def test_gui_save_open_restores_every_links_device_register_maps(tmp_path, monkeypatch):
    """A device's vendor register map is saved in the workspace with its Link, and reopened
    every Link has it: in its session (the register files), in its Commands table (the
    vendor register names, which the table copied when the Link loaded, before the
    workspace's maps were applied) and in the Registers pane's Peripheral block."""
    import pathlib

    from swi3s_studio.model.regmap_import import PeripheralRegisterMap
    app = QApplication.instance() or QApplication([])
    pm = PeripheralRegisterMap.from_json(
        pathlib.Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "regmaps", "example_amp.json")).read_text("utf-8"))
    win = MainWindow()
    win.load_demo_links()
    pump_loads(win, seconds=60.0)
    for link in win._links:
        link.session.set_peripheral_map(0, pm)

    path = str(tmp_path / "ws.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    win.save_workspace()
    assert [sorted(spec.device_regmaps) for spec in Workspace.load(path).links] == [
        ["0"], ["0"]]
    win2 = MainWindow()
    win2.open_workspace()
    pump_loads(win2, seconds=60.0)

    assert len(win2._links) == 2
    for i, link in enumerate(win2._links):
        assert len(link.session.peripheral_maps[0].registers) == len(pm.registers), i
        assert 0 in link.view.cmd_model._peripheral_maps, (
            f"Link {i}'s Commands table has no map: vendor register names unresolved")
        win2.switch_link(i)
        app.processEvents()
        root = win2._reg_view._tree.invisibleRootItem()
        titles = [root.child(k).text(0) for k in range(root.childCount())]
        assert any(t.startswith("Peripheral Registers") for t in titles), (i, titles)


def test_load_names_the_file_it_cannot_read_as_a_workspace(tmp_path):
    """A file that is not UTF-8, not JSON or nested too deeply to parse raises ValueError
    naming the path, as does a missing one."""
    bad = {"not_utf8.swi3s": b"\xff\xfe", "not_json.swi3s": b"{bad",
           "too_deep.swi3s": b"[" * 100_000}
    for name, data in bad.items():
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(ValueError, match="not a readable workspace") as e:
            Workspace.load(str(path))
        assert str(path) in str(e.value)
    missing = tmp_path / "missing.swi3s"
    with pytest.raises(ValueError, match="not a readable workspace") as e:
        Workspace.load(str(missing))
    assert str(missing) in str(e.value)


def test_load_reads_no_more_than_the_workspace_limit(tmp_path, monkeypatch):
    """A file over MAX_WORKSPACE_CHARS raises ValueError naming it; one at the limit loads."""
    from swi3s_studio import workspace
    monkeypatch.setattr(workspace, "MAX_WORKSPACE_CHARS", 64)
    body = '{"links": [{"source": {"type": "demo"}}]}'
    at_limit = tmp_path / "at_limit.swi3s"
    at_limit.write_text(body + " " * (64 - len(body)), encoding="utf-8")
    assert len(Workspace.load(str(at_limit)).links) == 1
    over = tmp_path / "over.swi3s"
    over.write_text(body + " " * (65 - len(body)), encoding="utf-8")
    with pytest.raises(ValueError, match="too large for a workspace") as e:
        Workspace.load(str(over))
    assert str(over) in str(e.value)


def test_load_reads_an_endless_stream_no_further_than_the_limit(monkeypatch):
    """A stream that never ends is read once, for at most the limit plus one character."""
    from swi3s_studio import workspace
    monkeypatch.setattr(workspace, "MAX_WORKSPACE_CHARS", 64)
    asked = []

    class Endless:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, n=-1):
            assert n >= 0, "an unbounded read of an endless stream"
            asked.append(n)
            return "x" * n
    monkeypatch.setattr(workspace, "open", lambda *a, **k: Endless(), raising=False)
    with pytest.raises(ValueError, match="too large for a workspace"):
        Workspace.load("endless")
    assert asked == [65]


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="no /dev/zero")
def test_load_of_a_device_that_never_ends_raises(monkeypatch):
    from swi3s_studio import workspace
    monkeypatch.setattr(workspace, "MAX_WORKSPACE_CHARS", 64)
    with pytest.raises(ValueError, match="too large for a workspace"):
        Workspace.load("/dev/zero")
