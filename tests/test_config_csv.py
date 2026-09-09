"""Decode-driving config CSV: a capture that begins after the setup commit can
still decode audio when a Visualizer config CSV is supplied (the decoder applies
that data-port config from row 0 instead of snooping the missing commands).

Covers the decode-driving config-CSV plumbing (shared by workspace reload and
File ▸ Apply Config CSV to Open Capture…): the config path flows into the decode,
is persisted in the workspace `source`, and reloads; and the GUI exposes the
capture-open actions and threads the path through `open_capture`.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_config_csv.py
"""
import inspect
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import swi3score

from swi3s_studio.model import Provenance
from swi3s_studio.model.bus_config import BusConfig, demo_config
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


def test_cds_per_source_roundtrip_and_legacy_migration():
    """Per-source CDS guard/tail must survive a to_csv -> from_csv_text round trip, and a
    legacy (pre-per-source) scalar-only CSV must migrate its global guard/tail onto ALL 13
    sources (Manager + 12 devices share the CDS). Guards the config-CSV upgrade pipeline —
    exactly the fields that could silently regress when the format is next extended."""
    from swi3s_studio.model.bus_config import (
        CDS_GUARD_G0,
        CDS_GUARD_G1,
        CDS_GUARD_OFF,
        CDS_NUM_SOURCES,
    )
    # Divergent per-source config -> exact round trip through the split guard/tail rows.
    cfg = demo_config()
    cfg.cds_guard = [CDS_GUARD_G0, CDS_GUARD_G1] + [CDS_GUARD_OFF] * (CDS_NUM_SOURCES - 2)
    cfg.cds_tail = [2, 0, 1] + [0] * (CDS_NUM_SOURCES - 3)
    cfg2 = BusConfig.from_csv_text(cfg.to_csv())
    assert cfg2.cds_guard == cfg.cds_guard, (cfg2.cds_guard, cfg.cds_guard)
    assert cfg2.cds_tail == cfg.cds_tail, (cfg2.cds_tail, cfg.cds_tail)

    # Legacy scalar-only CSV (no per-source rows) -> broadcast the global guard/tail to
    # all 13 sources (a uniform per-source config the engine collapses back to one symbol).
    legacy = ("AppVersion,2.0.0\n"
              "NumColumns_REG,7\n"
              "CDS_GuardEnabled_REG,1\n"
              "CDS_GuardPolarity_REG,1\n"
              "CDS_TailWidth_REG,2\n")
    cfg3 = BusConfig.from_csv_text(legacy)
    assert cfg3.cds_guard == [CDS_GUARD_G1] * CDS_NUM_SOURCES, cfg3.cds_guard
    assert cfg3.cds_tail == [2] * CDS_NUM_SOURCES, cfg3.cds_tail


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


def test_a_malformed_column_count_cannot_reach_the_decoder_as_a_modulus():
    """NumColumns_REG is not validated on the way in, and columnCount() is used as a MODULUS.

    `LoadCsv` takes the field straight from parseInt, and `Decoder::run` computes
    `mColumn = (mColumnCount - phaseOffset % mColumnCount) % mColumnCount`. A CSV saying
    `NumColumns_REG,-1` therefore made that a division by zero — undefined behaviour that
    SPLITS BY PLATFORM in the worst direction: ARM64's sdiv quietly yields 0 and the capture
    mis-decodes, while x86_64's idiv raises SIGFPE and kills the process. The maintainer's Mac
    and both test guests are ARM64; the CI runners are x86_64, so the crash lands where nobody
    is watching. A more negative value (-100 -> -99) skipped the crash and poisoned every
    row/column calculation instead.

    Clamped in columnCount() itself, so the live decode AND the audio engine (which hands the
    same value to CDataPort/CFlowControlPort::Configure) both get a legal count. The grid paths
    already applied this floor to their own local copy, which is why only the decode crashed.
    A legal count must still pass through untouched — that is the other half of this test.
    """
    import numpy as np

    clock = np.arange(0, 4000, 10, dtype=np.uint64)
    data = np.arange(5, 4000, 40, dtype=np.uint64)
    # (NumColumns_REG in the CSV, the column count the decoder must end up using)
    cases = ((7, 8), (31, 32),           # legal: unchanged
             (0, 2), (-1, 2), (-100, 2),  # below the protocol minimum -> kMinColumnCount
             (32, 32), (5000, 32))        # above the maximum -> kMaxColumnCount
    with tempfile.TemporaryDirectory() as d:
        for num_columns, expected in cases:
            path = os.path.join(d, f"nc{num_columns}.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"AppVersion,2.0.0\nNumColumns_REG,{num_columns}\n")
            src = swi3score.TransitionSampleSource(clock, data, False, False, 100_000_000)
            settings = swi3score.DecoderSettings()
            settings.config_csv_path = path
            settings.decode_audio = False
            dec = swi3score.Decoder(src, settings)
            dec.run()                    # must not divide by zero
            assert dec.column_count == expected, \
                f"NumColumns_REG={num_columns} gave column_count={dec.column_count}, " \
                f"expected {expected}"
            assert 2 <= dec.column_count <= 32, \
                f"column_count {dec.column_count} is outside the protocol's own limits"


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
    assert hasattr(win, "open_visualizer_config")     # File ▸ Analyzer ▸ Import Visualizer CSV
    # The Analyzer file submenu (held on the window, so its actions survive multi-window
    # GC in the same process — see the shiboken caveat).
    texts = [a.text() for a in win._file_analyzer_menu.actions()]
    assert any("Open &Capture" in t for t in texts), texts
    assert any("Import Visualizer CSV" in t for t in texts), texts
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


def test_apply_config_csv_reapply_same_path_rebuilds_after_edit():
    """Re-applying the SAME (already-current) config-CSV path after the file on disk was
    edited must rebuild the CSV grid/register baseline. An already-current CSV normalizes
    to its own path, so _reload_csv_seed's path-keyed memo would otherwise serve the stale
    seed while the C++ re-decode re-reads the file — a silent baseline↔audio mismatch.
    Regression for apply_config_csv invalidating the memo on every explicit apply."""
    from swi3s_studio.model.bus_config import demo_config
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cfg.csv")
        demo_config().to_csv_file(path)
        sess = Session.from_demo(64)
        sess.apply_config_csv(path)
        snap_before = list(sess._csv_snap)
        assert snap_before, "config CSV encoded no registers"

        cfg2 = demo_config()
        cfg2.dataports[0].interval = 3           # 4-row interval (was 8) -> Interval_REG changes
        cfg2.to_csv_file(path)                    # overwrite the SAME path
        sess.apply_config_csv(path)               # re-apply the identical path string
        assert list(sess._csv_snap) != snap_before, (
            "re-apply must rebuild the CSV baseline from the edited file, not the memo")


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
    win.load_demo()                                # the demo is no longer preloaded in __init__
    # The GUI Apply Config CSV wrapper folded into open_visualizer_config; the session
    # method it drives still exists (exercised by the tests above).
    assert hasattr(win, "open_visualizer_config")
    assert hasattr(win._session, "apply_config_csv")
    labels = [a.text() for a in win._file_analyzer_menu.actions()]
    assert any("Import Visualizer CSV" in t for t in labels), labels


def test_open_visualizer_config_routes_update_vs_compare():
    """Open Visualizer Config routes to the right action per the dialog result:
    file+compare → _run_compare, file+update → _apply_config_csv_path, authoring →
    use_authoring_as_expected (compare only). Spies avoid the modal + async re-decode."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui import open_config_dialog
    from swi3s_studio.ui.main_window import MainWindow
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
    from swi3s_studio.ui.main_window import ANALYSIS, VISUALIZATION, MainWindow
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


def test_visualizer_file_dir_memory_independent_of_analyzer():
    """The Visualizer's config-CSV load/save dialogs browse from (and update) their own
    last-directory memory ("visualizer"), independent of the Analyzer's ("analyzer").
    Regression: the authoring Load/Save buttons opened at "" / cwd and never remembered,
    so the two modes shared no scoping. Verifies both the dialog START dir and the
    remember-on-use, without touching the real filesystem picker."""
    from PySide6.QtWidgets import QApplication, QFileDialog
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    with tempfile.TemporaryDirectory() as vdir, tempfile.TemporaryDirectory() as adir, \
            tempfile.TemporaryDirectory() as newdir:
        win._remember_capture_dir(os.path.join(vdir, "seed.csv"), "visualizer")
        win._remember_capture_dir(os.path.join(adir, "seed.sal"), "analyzer")

        seen = {}
        orig_open, orig_save = QFileDialog.getOpenFileName, QFileDialog.getSaveFileName
        QFileDialog.getOpenFileName = staticmethod(
            lambda *a, **k: (seen.__setitem__("load_start", a[2]), ("", ""))[1])
        save_path = os.path.join(newdir, "out.csv")
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (seen.__setitem__("save_start", a[2]), (save_path, ""))[1])
        try:
            win.load_authoring_csv()      # cancels (returns "") -> only records start dir
            win.save_authoring_csv()      # returns a real path -> saves + remembers
        finally:
            QFileDialog.getOpenFileName = orig_open
            QFileDialog.getSaveFileName = orig_save

        # Both dialogs opened at the Visualizer dir (not "", cwd, or the Analyzer dir).
        assert seen["load_start"] == vdir, seen
        assert os.path.dirname(seen["save_start"]) == vdir, seen
        # Saving into newdir updated the Visualizer memory; the Analyzer's is untouched.
        assert win._last_capture_dir("visualizer") == newdir
        assert win._last_capture_dir("analyzer") == adir
        assert os.path.exists(save_path)   # the save actually wrote the CSV


def test_imposing_a_wider_config_warns_instead_of_silently_dropping_ports(monkeypatch):
    """A config whose ports need columns beyond the capture's decoded width has those ports
    simply NOT PLACED. That used to be silent: the grid came back narrower with fewer ports,
    which reads as "the config didn't load" rather than "this config is wider than this
    capture". Now it is one prompt, and declining aborts.

    Asserted on the MESSAGE, not just the prompt count, so a future refactor that keeps
    prompting but stops naming the offending ports still fails. Both answers are exercised:
    an abort-only test cannot tell "declining works" from "the spy never fired at all"."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    cap_cols = int(win._session.column_count)
    assert cap_cols > 0, "demo decoded no columns — test premise gone"

    cfg = demo_config()
    dp = cfg.dataports[1]
    dp.enabled = True
    dp.enable_ch = 0b11
    dp.name = "TooWide"
    dp.horizontal_start = cap_cols - 1        # ... so start+count runs past the capture
    dp.horizontal_count = 3                   # excess-1: owns 4 columns

    seen, started = {}, []
    # _load_async is the real gate: it is what launches the re-decode. Spying on a name
    # that does not exist would make the abort assertion vacuous.
    assert hasattr(win, "_load_async")
    monkeypatch.setattr(win, "_load_async", lambda *a, **k: started.append(True))
    answer = [QMessageBox.No]

    def fake_warning(_parent, title, text, *a, **k):
        seen["title"], seen["text"] = title, text
        return answer[0]

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))

    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "wide.csv"))
        win._apply_config_csv_path(path)                     # declined
        assert seen, "no warning shown — the drop is still silent"
        assert "beyond the capture's decoded width" in seen["text"], seen["text"]
        assert str(cap_cols) in seen["text"], seen["text"]
        assert "TooWide" in seen["text"], "the offending port is not named: " + seen["text"]
        assert not started, "declining the prompt must abort before the re-decode"

        answer[0] = QMessageBox.Yes
        win._apply_config_csv_path(path)                     # accepted
        assert started, "accepting the prompt must proceed to the re-decode"


def test_the_collision_check_sees_what_the_csv_will_encode():
    """`duplicate_dp_numbers` and the CSV writer must agree about the device number.

    The writer collapses anything outside {-1} u [0, 11] onto device 0, because the register
    cannot hold the -1 Manager sentinel and has nowhere else to put an out-of-range value.
    The checker used to key on the RAW attribute, so two ports at -2 and -9 sharing a
    dp_number compared as different, passed the check, and were then merged by the export —
    a real collision the user only saw after reloading. Both now go through
    `csv_device_identity`.

    The other half matters just as much: the Manager and a genuine device 0 sharing a
    dp_number is LEGITIMATE (the flag distinguishes them on the wire and on reload), so
    keying on the collapsed number alone would invent a collision.
    """
    from swi3s_studio.model.bus_config import csv_device_identity

    # The encoding itself: the pair the CSV carries.
    assert csv_device_identity(-1) == (0, True), "Manager encodes as device 0 + flag"
    assert csv_device_identity(0) == (0, False)
    assert csv_device_identity(7) == (7, False)
    assert csv_device_identity(-2) == (0, False), "out of range has nowhere but device 0"
    assert csv_device_identity(-9) == (0, False)

    # 1. Out-of-range ports that the export would merge are reported BEFORE the export.
    merged = demo_config()
    merged.dataports[0].device_number = -2
    merged.dataports[0].dp_number = 3
    merged.dataports[1].device_number = -9
    merged.dataports[1].dp_number = 3
    dups = merged.duplicate_dp_numbers()
    assert 0 in dups and 1 in dups, \
        f"two ports the export merges onto device 0 must collide, got {dups}"

    # 2. Manager + a real device 0 on the same dp_number is not a collision.
    ok = demo_config()
    ok.dataports[5].dp_number = 99            # move the slot that owns dp 5 by default
    ok.dataports[0].device_number = -1
    ok.dataports[0].dp_number = 5
    ok.dataports[1].device_number = 0
    ok.dataports[1].dp_number = 5
    assert ok.duplicate_dp_numbers() == [], \
        "the Manager and device 0 are distinct on the wire; this is not a clash"

    # 3. And that pair still round-trips with the Manager intact.
    with tempfile.TemporaryDirectory() as d:
        back = BusConfig.from_csv(ok.to_csv_file(os.path.join(d, "mgr.csv")))
    assert back.dataports[0].device_number == -1, "the Manager flag did not survive"
    assert back.dataports[1].device_number == 0

    # 4. An ordinary same-device collision is still caught.
    same = demo_config()
    same.dataports[0].device_number = 4
    same.dataports[0].dp_number = 2
    same.dataports[1].device_number = 4
    same.dataports[1].dp_number = 2
    assert {0, 1} <= set(same.duplicate_dp_numbers())
