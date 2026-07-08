"""Decode-driving config CSV: a capture that begins after the setup commit can
still decode audio when a Visualizer config CSV is supplied (the decoder applies
that data-port config from row 0 instead of snooping the missing commands).

Covers the decode-driving config-CSV plumbing (shared by workspace reload and
File ▸ Apply Config CSV to Open Capture…): the config path flows into the decode,
is persisted in the workspace `source`, and reloads; and the GUI exposes the
capture-open actions and threads the path through `open_capture`.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_config_csv.py
"""
import inspect
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import swi3score
from swi3s_studio.model import Provenance
from swi3s_studio.model.bus_config import demo_config, BusConfig
from swi3s_studio.session import Session
from swi3s_studio.workspace import session_from_source


def _config_csv(d: str) -> str:
    return demo_config().to_csv_file(os.path.join(d, "cfg.csv"))


def test_bus_config_csv_roundtrip_preserves_display_and_manager():
    # to_csv writes ManagerDataport + DisplayFields rows; from_csv_text must read
    # them back, or per-DP display fields and the manager (device -1) designation
    # are silently lost on a round trip.
    cfg = demo_config()
    cfg.dataports[0].display_fields = 5      # Sample+Bit (not the default 6)
    cfg.dataports[1].display_fields = 1      # Sample only
    cfg.dataports[2].device_number = -1      # a manager data port
    cfg2 = BusConfig.from_csv_text(cfg.to_csv())
    assert cfg2.dataports[0].display_fields == 5, cfg2.dataports[0].display_fields
    assert cfg2.dataports[1].display_fields == 1, cfg2.dataports[1].display_fields
    assert cfg2.dataports[2].device_number == -1, cfg2.dataports[2].device_number


def test_channel_grouping_exceeding_channels_is_safe():
    # A ChannelGrouping larger than the enabled-channel count must not walk the C++
    # channel index past its 16-entry table (an out-of-bounds read). Clamped to the
    # channel count, a 1-channel DP with ChannelGrouping=17 still decodes to a valid
    # grid placing only channel 0.
    cfg = demo_config()
    dp = cfg.dataports[0]
    dp.enable_ch = 0b1                              # one channel
    dp.channel_grouping = 17                        # absurd (> channels, > 16)
    with tempfile.TemporaryDirectory() as d:
        p = cfg.to_csv_file(os.path.join(d, "cg.csv"))
        cells = swi3score.grid_from_csv(p, 16)      # must not crash / read OOB
    data = [c for c in cells if not c.get("is_cds")]
    assert data, "no data cells placed"
    assert all(c.get("channel", 0) == 0 for c in data)   # only channel 0 exists


def _grid_key(c: dict):
    return (c["row"], c["col"], c["dp"], c["device"], c["channel"], c["slot"],
            c["is_cds"], c["is_source"], c["sample_here"])


def test_grid_dp_needs_enabled_channel():
    """A committed dataport with geometry set but NO channel enabled (EnableCh0=0)
    places NO data cells in the bus grid; enabling ch0 then places them. Guards against
    geometry alone (HorizontalCount) lighting a dataport."""
    def dp1_cells(en_hc):
        replay = [{"is_write": True, "device_mask": 0b10, "address": a, "data": bytes([v])}
                  for a, v in [(0x2180, 0x0d), (0x2181, en_hc), (0x2183, 0x0f)]]
        replay.append({"is_commit": True, "commit_confirmed": True, "group_mask": 0xFFFF})
        cells = swi3score.grid_from_commands(replay, 8, force_columns=16)
        return sum(1 for c in cells if not c.get("is_cds") and c.get("dp") == 1)
    assert dp1_cells(0x41) == 0, "EnableCh0=0 must place no cells (0x41 = HCount set, ch0 off)"
    assert dp1_cells(0xC1) > 0, "EnableCh0=1 (0xC1) must place cells"


def test_grid_effect_slice_ignores_interleaved_reads():
    """Regression for the bus-grid staged-write leak: the grid replay (writes+commits
    only) is index-aligned to its OWN effective-sample list, so a READ before the cursor
    can't pull a FUTURE write into the slice. With write@100, read@200, write@300 to one
    register, the grid as-of 250 must contain ONLY write@100 — never the future write@300
    (which would enable a dataport before its command)."""
    sess = Session.from_demo(8)
    sess._rel_cmds = [
        {"command": "WriteA32", "crc_valid": True, "has_address": True, "device_mask": 0b1,
         "address": 0x2081, "data": bytes([0x81]), "start_sample": 100},
        {"has_read_data": True, "read_data_crc_valid": True, "has_address": True,
         "device_mask": 0b1, "address": 0x2081, "read_data": bytes([0x00]), "start_sample": 200},
        {"command": "WriteA32", "crc_valid": True, "has_address": True, "device_mask": 0b1,
         "address": 0x2081, "data": bytes([0xC1]), "start_sample": 300},
    ]
    sess._eff_sorted = None                      # rebuild the effective order from the crafted list
    writes = [r for r in sess._grid_replay_in_effect(250) if r.get("is_write")]
    assert len(writes) == 1 and writes[0]["data"] == bytes([0x81]), \
        f"future write leaked into the grid slice: {writes}"


def test_config_csv_drives_decode_and_persists():
    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        sess = Session.from_demo(64, cold_start=True, config_csv=csv)
        # Reached the decoder, and the ORIGINAL path is persisted in the source
        # descriptor (not the normalized temp path).
        assert sess._config_csv, "config path not applied to the decode"
        assert sess.source.get("config_csv") == csv, sess.source
        # Audio still reconstructs with the config applied from row 0.
        assert {(a["device"], a["dp"], a["channel"]) for a in (sess.audio or [])}


def test_config_csv_survives_workspace_reload():
    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        sess = Session.from_demo(64, cold_start=True, config_csv=csv)
        reloaded = session_from_source(sess.source)
        assert reloaded.source.get("config_csv") == csv
        assert reloaded._config_csv


def test_gui_capture_open_actions():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    # open_capture still threads a config_csv through (workspace-reload path uses it).
    assert "config_csv" in inspect.signature(win.open_capture).parameters
    # The old "Open Capture with Config CSV" menu wrapper is gone.
    assert not hasattr(win, "open_capture_with_config")
    assert hasattr(win, "open_visualizer_csv")        # File ▸ Visualizer ▸ Open Settings
    assert hasattr(win, "open_visualizer_config")     # File ▸ Analyzer ▸ Open Visualizer Config
    # The Analyzer file submenu (held on the window, so its actions survive multi-window
    # GC in the same process — see the shiboken caveat).
    texts = [a.text() for a in win._file_analyzer_menu.actions()]
    assert any("Open &Capture" in t for t in texts), texts
    assert any("Open &Visualizer Config" in t for t in texts), texts
    assert any("Open &Workspace" in t for t in texts), texts
    assert any("Save &Workspace" in t for t in texts), texts
    # The old flat gating lists are gone (File actions guard at call time instead).
    assert not hasattr(win, "_file_gated") and not hasattr(win, "_file_always")


def test_apply_config_csv_imposes_grid_and_registers():
    """Applying a config CSV to an ALREADY-OPEN capture (no reopen) seeds the bus
    grid and register map from it (Provenance.CSV) so both match the CSV from row 0,
    and removing it reverts to the snoop-only decode."""
    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        sess = Session.from_demo(64)                          # opened WITHOUT a config CSV
        assert not sess._csv_replay
        grid_before = sorted(map(_grid_key, sess.grid_cells_at(0, 8)))

        sess.apply_config_csv(csv)                            # <-- in-place import
        assert sess._config_csv and sess.source.get("config_csv") == csv

        # Bus grid at row 0 matches the CSV (no wire commands are in effect yet, so the
        # CSV baseline is all there is).
        want_grid = sorted(map(_grid_key, swi3score.grid_from_csv(csv, 8)))
        assert sorted(map(_grid_key, sess.grid_cells_at(0, 8))) == want_grid

        # Register map at row 0 carries the CSV values, tagged "CSV Import".
        want = {(dev, a): (v & 0xFF) for dev, a, v in swi3score.registers_from_csv(csv)}
        files = sess.register_files_at(0)
        assert want, "config CSV encoded no registers"
        for (dev, a), v in want.items():
            f = files.get(dev)
            assert f is not None and (f.value(a) & 0xFF) == v, (dev, a, v)
            assert f.get(a).provenance == Provenance.CSV, (dev, a)

        # Removing it reverts to the original snoop-only grid.
        sess.apply_config_csv(None)
        assert not sess._csv_replay
        assert "config_csv" not in sess.source
        assert sorted(map(_grid_key, sess.grid_cells_at(0, 8))) == grid_before


def test_apply_config_csv_persists_across_reload():
    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        sess = Session.from_demo(64)
        sess.apply_config_csv(csv)
        reloaded = session_from_source(sess.source)
        assert reloaded.source.get("config_csv") == csv
        assert reloaded._csv_replay, "CSV baseline not rebuilt on reload"


def test_gui_exposes_open_visualizer_config():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    # The GUI Apply Config CSV wrapper folded into open_visualizer_config; the session
    # method it drives still exists (exercised by the tests above).
    assert hasattr(win, "open_visualizer_config")
    assert hasattr(win._session, "apply_config_csv")
    labels = [a.text() for a in win._file_analyzer_menu.actions()]
    assert any("Open &Visualizer Config" in t for t in labels), labels


def test_open_visualizer_config_routes_update_vs_compare():
    """Open Visualizer Config routes to the right action per the dialog result:
    file+compare → _run_compare, file+update → _apply_config_csv_path, authoring →
    use_authoring_as_expected (compare only). Spies avoid the modal + async re-decode."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui import open_config_dialog
    win = MainWindow()
    win.load_demo()                                    # a decoded capture in Analysis
    calls = []
    win._run_compare = lambda p: calls.append(("compare", p))
    win._apply_config_csv_path = lambda p: calls.append(("update", p))
    win.use_authoring_as_expected = lambda: calls.append(("authoring_compare", None))

    def fake_dialog(use_authoring, path, do_update, do_compare):
        class _Dlg:
            def __init__(self, *a, **k):
                self.use_authoring = use_authoring; self.path = path
                self.do_update = do_update; self.do_compare = do_compare
            def exec(self): return 1
        return _Dlg

    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        open_config_dialog.OpenVisualizerConfigDialog = fake_dialog(False, csv, False, True)
        win.open_visualizer_config()
        open_config_dialog.OpenVisualizerConfigDialog = fake_dialog(False, csv, True, False)
        win.open_visualizer_config()
        open_config_dialog.OpenVisualizerConfigDialog = fake_dialog(True, "", False, True)
        win.open_visualizer_config()
    assert [c[0] for c in calls] == ["compare", "update", "authoring_compare"], calls


def test_open_visualizer_csv_switches_mode_and_loads():
    """File ▸ Open Visualizer CSV loads the CSV into the authoring panel and switches
    to Visualizer mode even when we start in Analysis (opening a capture lands there)."""
    from PySide6.QtWidgets import QApplication, QFileDialog
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow, ANALYSIS, VISUALIZATION
    win = MainWindow()
    win.load_demo()                                    # opening a capture lands in Analysis
    assert win._mode_mgr.current() == ANALYSIS
    with tempfile.TemporaryDirectory() as d:
        csv = _config_csv(d)
        orig = QFileDialog.getOpenFileName
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (csv, ""))
        try:
            win.open_visualizer_csv()
        finally:
            QFileDialog.getOpenFileName = orig
    assert win._mode_mgr.current() == VISUALIZATION     # switched to Visualizer
    assert len(win._authoring.config().dataports) == 12


if __name__ == "__main__":
    test_bus_config_csv_roundtrip_preserves_display_and_manager(); print("ok: BusConfig CSV round-trip keeps display fields + manager")
    test_channel_grouping_exceeding_channels_is_safe(); print("ok: ChannelGrouping > channels decodes safely (no OOB)")
    test_grid_dp_needs_enabled_channel(); print("ok: bus grid needs an enabled channel (geometry alone places nothing)")
    test_grid_effect_slice_ignores_interleaved_reads(); print("ok: grid effect-slice ignores interleaved reads (no future-write leak)")
    test_config_csv_drives_decode_and_persists(); print("ok: config CSV drives decode + persists")
    test_config_csv_survives_workspace_reload(); print("ok: config CSV survives workspace reload")
    test_gui_capture_open_actions(); print("ok: File ▸ Analyzer exposes Open Capture / Visualizer Config / Workspace")
    test_apply_config_csv_imposes_grid_and_registers(); print("ok: apply imposes grid + registers (CSV Import)")
    test_apply_config_csv_persists_across_reload(); print("ok: applied config CSV persists across reload")
    test_gui_exposes_open_visualizer_config(); print("ok: GUI exposes Open Visualizer Config")
    test_open_visualizer_config_routes_update_vs_compare(); print("ok: Open Visualizer Config routes update vs compare")
    test_open_visualizer_csv_switches_mode_and_loads(); print("ok: Open Visualizer CSV (Visualizer ▸ Open Settings) switches + loads")
    print("ALL CONFIG-CSV TESTS PASSED")

