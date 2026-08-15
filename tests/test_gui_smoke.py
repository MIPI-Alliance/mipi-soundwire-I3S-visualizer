"""Headless GUI smoke test (Qt offscreen): construct the main window, load the demo
capture, and verify the command table, register map, 2D grid, audio/symbol panes,
navigation, filters, statistics, raw capture and register editing all behave.

Split into pytest test_* functions sharing ONE module-scoped MainWindow (building it
is ~1s; the walkthrough mutates that shared window in order, like a real session).
Runs under CI (`pytest`) so it catches drift from the demo it asserts against — e.g.
the 3.0.6 demo change to a single device with four data ports (DP0/DP2 PCM/PDM
unscrambled, DP1/DP3 scrambled), 4 mono channels.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_gui_smoke.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog

from swi3s_studio.model import Provenance
from swi3s_studio.ui.command_table import _COL, _collapse_ranges
from swi3s_studio.ui.main_window import MainWindow

# The demo loads a single device with four data ports, all mono: DP0 (PCM) and DP2
# (PDM) run UNSCRAMBLED, DP1/DP3 scrambled. See native/swi3score/Demo.cpp.
_STREAMS = [(0, 0), (0, 1), (0, 2), (0, 3)]
_PLOTS = 4                                   # 1 device x 4 DPs x 1 channel

_app = QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win():
    """One MainWindow with the demo loaded, shared across the walkthrough (ordered)."""
    w = MainWindow()
    w.load_demo()
    return w


def test_command_table(win):
    # The decoded commands PLUS synthetic 'Commit Point' rows (one per confirmed
    # sync-point commit, at its SSP).
    model = win._cmd_model
    n_cp = len(win._session.commit_point_rows())
    assert model.rowCount() == len(win._session.commands) + n_cp > 0
    cmds = [model.data(model.index(r, _COL["Opcode"])) for r in range(model.rowCount())]
    assert "WriteA32" in cmds and "Ping" in cmds, cmds
    if n_cp:
        assert "Commit Point" in cmds, "commit-point rows should appear in the table"
    # A WriteA32 resolves its address to a register label.
    wrow = next(r for r in range(model.rowCount())
                if model.data(model.index(r, _COL["Opcode"])) == "WriteA32")
    label = model.data(model.index(wrow, _COL["Register"]))
    assert label and ("[NEXT]" in label or "DP" in label or "." in label), label

    # Response column decoded to symbolic names (not "P:0").
    responses = [model.data(model.index(r, _COL["Response"])) or "" for r in range(model.rowCount())]
    assert any("WRITE_OK" in x for x in responses), responses
    assert any("PING_ATTACHED" in x for x in responses), responses     # per-device ping
    assert not any("P:" in x for x in responses)


def test_register_view_scrambler(win):
    # The demo runs DP0/DP2 (PCM/PDM) UNSCRAMBLED — PortControl.ScramblerEn=0, explicitly
    # written — and leaves DP1/DP3 at the reset default (scrambling on). So DP0's
    # ScramblerEn (0x200B bit3) reads WRITTEN-0 and DP1's (0x210B bit3) reads DEFAULT-1.
    assert win._reg_view.device_count >= 1
    dev0 = win._session.register_files[0]
    assert not (dev0.value(0x200B) & 0x08) and dev0.provenance(0x200B) == Provenance.WRITTEN, \
        "DP0 should be written unscrambled (ScramblerEn=0)"
    assert (dev0.value(0x210B) & 0x08) and dev0.provenance(0x210B) == Provenance.DEFAULT, \
        "DP1 should keep the reset default (ScramblerEn=1)"
    assert dev0.provenance(0x1081) == Provenance.WRITTEN


def test_bus_grid(win):
    # At load the cursor sits at t=0, so the bus shows NO active data ports (only the
    # always-present CDS Column 0) — config hasn't happened yet.
    win.cursor.set_sample(0)
    assert not win._grid_view._stream_colors, "no data-port streams should be active at t=0"
    # Scrub PAST the SSP (the last command is the SSCR sync-point commit; its config
    # commits Row_Delay rows later) to where the config has actually committed.
    win.cursor.set_sample(win._session.audio_end_sample or win._session.commands[-1]["end_sample"])
    assert win._grid_view.item_count >= win._session.column_count
    # Visualizer-style colouring: each (device, dp) stream gets a distinct colour.
    sc = win._grid_view._stream_colors
    for key in _STREAMS:
        assert key in sc, (key, sorted(sc))
    assert len({c.name() for c in sc.values()}) == len(sc)   # all distinct
    assert win._grid_view.stream_color(0, 0).name() == sc[(0, 0)].name()
    win.cursor.set_sample(0)                              # back to start for later steps


def test_audio_viewer_channels(win):
    # One plot per channel across all streams; the demo has _PLOTS mono channels.
    assert win._audio_view.plot_count == _PLOTS, win._audio_view.plot_count
    assert win._export_action.isEnabled()
    # Curves carry the actual (non-flat) waveform, and Reset Zoom runs.
    _p, curve0, _a, _d, _k, _n = win._audio_view._plots[0]
    ys = curve0.getData()[1]
    assert ys is not None and len(ys) and (ys.max() - ys.min()) > 0, "audio curve is flat"
    win._audio_view.reset_zoom()
    # Channel show/hide: unchecking one stream drops its track; None/All work.
    first_key = next(iter(win._audio_view._checks))
    win._audio_view._checks[first_key].setChecked(False)
    assert win._audio_view.plot_count == _PLOTS - 1
    win._audio_view._set_all_channels(False)
    assert win._audio_view.plot_count == 0
    win._audio_view._set_all_channels(True)
    assert win._audio_view.plot_count == _PLOTS


def test_audio_vzoom_and_time_axis(win):
    # Vertical Zoom: fits each track's Y to the visible data (vs full-scale ±1).
    av = win._audio_view
    plot0, _c0, d0, p0, k0, n0 = av._plots[0]
    # The audio X axis is capture-sample space, so view the FULL capture extent.
    av._plots and plot0.setXRange(0, max(1, av._capture_extent), padding=0)
    ex, elo, ehi = win._audio_store.envelope(d0, p0, k0, 0, max(1, n0), max_points=2000)
    scale = float(1 << (win._audio_store.sample_bits(d0, p0, k0) - 1))
    data_max = float(max((ehi / scale).max(), abs((elo / scale).min())))
    av._vzoom_btn.setChecked(True)
    y0, y1 = plot0.getViewBox().viewRange()[1]
    assert av._vzoom and y1 <= 1.0 + 1e-6                     # fit within full scale
    assert abs(y1 - data_max) < 0.2 or data_max > 0.9        # fits the data (unless full-scale)
    av._vzoom_btn.setChecked(False)
    _y0b, y1b = plot0.getViewBox().viewRange()[1]
    assert y1b >= 0.9                                         # back to ~full-scale ±1
    # Time axis adapts unit + comma-separates when zoomed (µs / ms / s).
    from swi3s_studio.ui.audio_view import _TimeAxis
    tax = _TimeAxis(orientation="bottom"); tax.set_rate(48000)
    assert tax.tickStrings([297600], 1, 10)[0].endswith("µs")          # fine spacing → µs
    assert "," in tax.tickStrings([297600], 1, 10)[0]                  # thousands separated
    assert tax.tickStrings([48000, 96000], 1, 48000) == ["1 s", "2 s"]  # coarse → s


def test_raw_time_axis():
    # Raw Capture shares the same adaptive comma-separated time axis (x is in seconds).
    from swi3s_studio.ui.raw_view import _RawTimeAxis
    rax = _RawTimeAxis(orientation="bottom")
    zt = rax.tickStrings([4.3021955], 1, 0.5e-6)[0]      # zoomed to sub-µs spacing
    assert zt.endswith("µs") and "," in zt, zt           # → '4,302,195.50 µs'
    assert rax.tickStrings([1.0, 2.0], 1, 1.0) == ["1 s", "2 s"]


def test_audio_cursor_line(win):
    # Audio cursor line: one per plot, at the shared cursor's CAPTURE sample directly.
    assert len(win._audio_view._cursor_lines) == win._audio_view.plot_count
    late = win._session.commands[-1]["start_sample"]
    win.cursor.set_sample(late)
    _p, _c, dev, dp, ch, _n = win._audio_view._plots[0]
    assert win._audio_view._cursor_lines[0].value() == late
    i0, _i1 = win._audio_store.index_range_for_samples(dev, dp, ch, late, late)
    assert win._audio_store.sample_at_index(dev, dp, ch, i0) is not None
    win.cursor.set_sample(0)
    # Playback controls exist and are idle; stop() is safe with nothing playing.
    assert hasattr(win._audio_view, "play") and not win._audio_view.is_playing
    win._audio_view.stop()                    # no-op, must not raise


def test_symbol_viewer(win):
    assert win._symbol_view.symbol_count > 0
    # Follows the cursor via windowed (seeked) re-decode: jumping to a later command
    # shows fewer symbols than the from-start full decode.
    win._symbol_dock.show()
    full_syms = win._symbol_view.symbol_count
    win.cursor.set_sample(win._session.commands[2]["start_sample"])
    assert 0 < win._symbol_view.symbol_count <= full_syms
    win.cursor.set_sample(0)                       # back to start -> full window again
    assert win._symbol_view.symbol_count >= full_syms

    # Decode-on-scroll back-history: prepending earlier symbols grows the list above.
    sv = win._symbol_view
    win.cursor.set_sample(win._session.commands[-1]["start_sample"])
    n0 = sv.symbol_count
    if n0 and sv._symbols:
        earlier = win._session.symbols_before(int(sv._symbols[0]["start_sample"]))
        sv.prepend_symbols(earlier)
        assert sv.symbol_count == n0 + len(earlier), (sv.symbol_count, n0, len(earlier))
        sv.prepend_symbols([])                 # nothing more above
        assert sv._at_start
    win.cursor.set_sample(0)


def test_synchronized_navigation(win):
    model = win._cmd_model
    # Selecting a row moves the cursor to that command's start (command_cursor_sample).
    last = model.rowCount() - 1
    win._cmd_view.selectRow(last)
    assert win.cursor.sample == win._session.command_cursor_sample(model.command_at(last))
    # As-of t=0 (before any write) NumColumns is still at its DEFAULT.
    early = win._session.register_files_at(0)
    if 0 in early:
        assert early[0].provenance(0x1081) == Provenance.DEFAULT

    # Selecting a command selects ITS beginning CDS symbol (the SPM comma).
    wcmd_row = next(r for r in range(model.rowCount())
                    if model.data(model.index(r, _COL["Opcode"])) == "WriteA32")
    win._cmd_view.selectRow(wcmd_row)
    wcmd = model.command_at(wcmd_row)
    sel = win._symbol_view.selectionModel().selectedRows()
    assert sel, "command selection did not select a CDS symbol"
    sel_sym = win._symbol_view._symbols[sel[0].row()]
    assert int(sel_sym["row"]) == int(wcmd["bus_row"]), (sel_sym["row"], wcmd["bus_row"])
    assert sel_sym["kind"] == 1, "selected CDS symbol should be the SPM comma"
    assert int(wcmd["start_sample"]) == int(sel_sym["start_sample"]), \
        (wcmd["start_sample"], sel_sym["start_sample"])

    # Selecting a CDS symbol row moves the shared cursor and the command table. The
    # cursor anchors on the symbol's row Row-Sync-Point (rising edge opening the row),
    # ~1 UI before the symbol's raw start_sample — the CDS/commit convention.
    sv = win._symbol_view
    if sv.rowCount() > 5:
        row = min(20, sv.rowCount() - 1)
        samp = sv.sample_at_row(row)
        sv.selectRow(row)
        assert win.cursor.sample == win._session.row_sync_sample(samp), \
            (win.cursor.sample, samp, win._session.row_sync_sample(samp))
        assert win._cmd_view.selectionModel().selectedRows(), "command table didn't follow symbol"

    # Timeline seek -> cursor -> nearest command selected (two-way sync).
    target = win._session.commands[2]["start_sample"]
    win._on_seek(target + 5)
    assert win.cursor.sample == target + 5
    sel = win._cmd_view.selectionModel().selectedRows()
    assert sel and sel[0].row() == 2, sel


def test_timeline_config_bands(win):
    assert win._timeline._segments, "timeline got no config segments"
    assert win._timeline._seg_band, "no band colours assigned"
    # Segment lookup: the last segment starting at/before a sample is that sample's
    # geometry. The demo reconfigures (2col → 8 → 16), so check against the geometry
    # AT that sample, not the final operational column count.
    smp = win._session.commands[2]["start_sample"]
    seg = next((s for s in reversed(win._timeline._segments)
                if int(s.get("start_sample", 0)) <= smp), None)
    assert seg is not None and seg["column_count"] == win._session.column_count_at(smp)


def test_dock_navigation(win):
    # View menu can re-show a closed dock (isHidden(), since the window isn't shown
    # under the offscreen platform).
    win._grid_dock.close()
    assert win._grid_dock.isHidden()
    win._show_dock(win._grid_dock)
    assert not win._grid_dock.isHidden()
    tabbed = win.tabifiedDockWidgets(win._grid_dock)
    assert win._audio_dock in tabbed or win._symbol_dock in tabbed, tabbed
    # A dock floated then closed (a macOS quirk path) must come back docked + re-tabified.
    ad = win._audio_dock
    ad.setFloating(True)
    ad.close()
    win._show_dock(ad)
    assert not ad.isHidden() and not ad.isFloating()
    assert win.dockWidgetArea(ad) == Qt.BottomDockWidgetArea
    tb = win.tabifiedDockWidgets(ad)
    assert win._grid_dock in tb or win._symbol_dock in tb, tb


def test_compare_overlay(win):
    # The expected register config shows in the CSV-Import colour, distinct from a bus
    # write/read; Clear removes it.
    rv = win._reg_view
    win._cmd_view.selectRow(win._cmd_model.rowCount() - 1)     # cursor -> end
    dev0_file = win._session.register_files.get(0)
    rv.set_expected([(0, 0x200B, 0x00)])
    assert rv.has_expected()
    _v, prov = rv._effective(0, 0x200B, dev0_file)
    assert prov == Provenance.CSV and _v == 0x00
    rv.clear_expected()
    assert not rv.has_expected()
    _v2, prov2 = rv._effective(0, 0x200B, dev0_file)
    assert prov2 != Provenance.CSV            # overlay gone -> back to decoded


def test_command_filters(win):
    # Kind filter narrows rows; errors-only hides everything (demo clean); clear restores.
    total_rows = win._cmd_proxy.rowCount()
    win._cmd_proxy.set_kinds({"WriteA32"})
    writes = win._cmd_proxy.rowCount()
    assert 0 < writes < total_rows, (writes, total_rows)
    win._cmd_proxy.set_kinds(set())
    win._cmd_proxy.set_text("writea32 or ping")          # boolean free text
    assert win._cmd_proxy.rowCount() > writes
    win._cmd_proxy.set_text("")
    win._errors_only_act.setChecked(True)
    assert win._cmd_proxy.rowCount() == 0                # demo is clean
    win._errors_only_act.setChecked(False)
    assert win._cmd_proxy.rowCount() == total_rows
    win._cmd_proxy.set_kinds({"WriteA32"})
    win._update_cmd_title()
    assert "of" in win._cmd_title.text() and "Cmd" in win._cmd_title.text()
    win._clear_all_filters()
    assert win._cmd_title.text() == "Commands"


def test_statistics_collapsible_sections(win):
    # Statistics populated, grouped into collapsible sections (header click folds rows).
    # Statistics builds lazily on first show (it's tabbed behind Registers at load), so
    # raise it the way the dock-visibility signal would.
    win._meas_dock.raise_()
    win._on_meas_dock_visible(True)
    assert win._meas_view.metric_count > 0
    mv = win._meas_view
    assert mv._sections, "Statistics has no collapsible section headers"
    hr = next(iter(mv._sections))
    kids = mv._sections[hr]
    if kids:
        assert not mv.isRowHidden(kids[0])
        mv._on_cell_clicked(hr, 0)
        assert mv.isRowHidden(kids[0]), "section did not collapse on header click"
        mv._on_cell_clicked(hr, 0)
        assert not mv.isRowHidden(kids[0]), "section did not re-expand"


def test_bookmarks(win):
    win.cursor.set_sample(win._session.commands[1]["start_sample"])
    win.toggle_bookmark()
    win.cursor.set_sample(win._session.commands[4]["start_sample"])
    win.toggle_bookmark()
    assert len(win._bookmarks) == 2
    win.cursor.set_sample(0)
    win.next_bookmark()
    assert win.cursor.sample == win._session.commands[1]["start_sample"]
    win.clear_bookmarks()
    assert not win._bookmarks


def test_64bit_sample_signals():
    # Large-capture sample numbers (> 2**31) must survive the cursor/timeline signals: a
    # 32-bit Signal(int) overflows and breaks slot dispatch.
    from swi3s_studio.ui.cursor import TimeCursor
    from swi3s_studio.ui.timeline import TimelineRibbon
    big = 2_400_000_000
    seen = []
    tc = TimeCursor(); tc.sampleChanged.connect(lambda s: seen.append(("cursor", s)))
    tc.set_sample(big)
    tr = TimelineRibbon(); tr.seeked.connect(lambda s: seen.append(("seek", s)))
    tr.seeked.emit(big)                   # 32-bit signal would raise "Slot not found"
    assert seen == [("cursor", big), ("seek", big)], seen


def test_raw_capture_zoom(win):
    # A re-decode of the SAME capture must PRESERVE the user's zoom + Sample Bits toggle.
    raw = win._raw_view
    raw.reset_zoom()
    fx0, fx1 = raw._plot.getViewBox().viewRange()[0]
    full_span = fx1 - fx0
    raw._plot.setXRange(fx0, fx0 + full_span * 0.05, padding=0)      # zoom way in
    zx0, zx1 = raw._plot.getViewBox().viewRange()[0]
    raw._samp_btn.blockSignals(True)                                 # simulate "Hide Samples" on
    raw._samp_btn.setChecked(True); raw._samp_btn.setText("Hide Samples")
    raw._show_ports = True; raw._samp_btn.blockSignals(False)
    raw.set_capture(win._session.capture,                            # same capture (a re-decode)
                    dp_is_data_line=win._session.link_control.lc_on_data_line)
    ax0, ax1 = raw._plot.getViewBox().viewRange()[0]
    assert abs(ax0 - zx0) < full_span * 1e-6 and abs(ax1 - zx1) < full_span * 1e-6, \
        ((zx0, zx1), (ax0, ax1))            # zoom preserved across the same-capture re-decode
    assert raw._samp_btn.isChecked() and raw._show_ports, "Sample Bits toggle reset by re-decode"

    # Zoom-to-row: with a cursor set, "1 Row" shrinks the X span to ~one bus row.
    mid = win._session.commands[len(win._session.commands) // 2]["start_sample"]
    raw.set_cursor(int(mid))
    raw.zoom_to_row()
    rx0, rx1 = raw._plot.getViewBox().viewRange()[0]
    assert 0 < (rx1 - rx0) < full_span, (rx1 - rx0, full_span)   # zoomed to a small window
    assert rx0 <= mid / win._session.sample_rate_hz <= rx1, "cursor not inside the one-row window"
    raw.reset_zoom()


def test_register_editing(win):
    # Right-click context-menu and double-click share _prompt_edit, which emits
    # registerEdited(device, address, value) for an editable row. Test in isolation so
    # the emit doesn't kick off the main window's async re-decode.
    from swi3s_studio.model.registers import FieldSpec
    from swi3s_studio.ui.register_view import RegisterView, _enum_covers, _field_value_max
    rv2 = RegisterView(win._rmap)
    rv2.set_files(win._session.register_files_at(win._session.commands[-1]["start_sample"]))
    assert rv2._tree.contextMenuPolicy() != Qt.CustomContextMenu
    assert hasattr(rv2._tree, "contextRequested")

    def _find_editable(item, want_label=None):
        for i in range(item.childCount()):
            c = item.child(i)
            if c.data(0, Qt.UserRole) and (want_label is None or want_label in c.text(0)):
                return c
            found = _find_editable(c, want_label)
            if found is not None:
                return found
        return None

    editable = _find_editable(rv2._tree.invisibleRootItem())
    assert editable is not None, "no editable register row found"
    curr_row = _find_editable(rv2._tree.invisibleRootItem(), "(CURR)")
    if curr_row is not None:
        assert curr_row.data(0, Qt.UserRole), "CURR row should be editable (redirects to NEXT-base)"
    menu = rv2._context_menu_for(editable)
    acts = {a.text(): a for a in menu.actions()}
    force = next(a for t, a in acts.items() if t.startswith("Force"))
    assert force.isEnabled()
    assert any("Clear" in t for t in acts)
    none_force = next(a for a in rv2._context_menu_for(None).actions() if a.text().startswith("Force"))
    assert not none_force.isEnabled()
    rv2._prompt_edit(None)                    # non-editable → safe no-op (no emit)
    emitted = []
    rv2.registerEdited.connect(lambda d, a, v: emitted.append((d, a, v)))
    _orig_text = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("0x00", True))
    try:
        force.trigger()                       # runs the raw-byte edit path (getText patched)
    finally:
        QInputDialog.getText = _orig_text
    assert emitted and emitted[0][2] == 0x00, emitted

    # Field-level editor: a register with sub-byte fields exposes them.
    def _find_fieldy(item):
        for i in range(item.childCount()):
            c = item.child(i)
            if rv2._editable_fields(c):
                return c
            found = _find_fieldy(c)
            if found is not None:
                return found
        return None
    frow = _find_fieldy(rv2._tree.invisibleRootItem())
    assert frow is not None, "no register with editable fields found"
    flds = rv2._editable_fields(frow)
    fmenu = {a.text(): a for a in rv2._context_menu_for(frow).actions()}
    edit_fields = next(a for t, a in fmenu.items() if t.startswith("Edit fields"))
    assert edit_fields.isEnabled()
    f0 = flds[0]
    assert RegisterView._compose_fields(0, [(f0, 1)]) == ((1 << f0.lo) & f0.mask)
    bw = FieldSpec(name="BitWidth", hi=1, lo=0, reset=0, enum={3: "Reserved"})
    assert not _enum_covers(bw)
    assert _field_value_max(bw, 0) == 2       # 0..2 (3 = Reserved excluded)
    assert _field_value_max(bw, 3) == 3       # a currently-set reserved value stays allowed
    assert _enum_covers(FieldSpec(name="En", hi=0, lo=0, reset=0, enum={0: "Off", 1: "On"}))


def test_command_table_headers(win):
    model = win._cmd_model
    assert _collapse_ranges([1, 3, 4, 5]) == "1,3-5"
    assert _collapse_ranges([0, 1, 2]) == "0-2"
    assert _collapse_ranges([2]) == "2"
    # Wide-label narrow columns get two-line headers so they shrink.
    assert model.headerData(_COL["Packet Length"], Qt.Horizontal, Qt.DisplayRole) == "Packet\nLength"
    assert model.headerData(_COL["Group Mask"], Qt.Horizontal, Qt.DisplayRole) == "Group\nMask"
    assert model.headerData(_COL["Devices"], Qt.Horizontal, Qt.DisplayRole) == "Devices"
    assert win._cmd_view.verticalHeader().isHidden()      # empty row-index column gone


def test_redecode_cursor_restore(win):
    # _restore_cursor re-syncs the panes at the given sample even when the cursor VALUE
    # is unchanged (load_session re-inits at t=0 and TimeCursor.set_sample no-ops equal).
    late = int(win._session.command_cursor_sample(win._session.commands[-1]))
    win.cursor.set_sample(late)
    files_late = win._session.register_files_at(late)
    rdev = sorted(files_late)[0]
    v_late = files_late[rdev].value(0x1081)
    win._reg_view.set_files(win._session.register_files_at(0))   # simulate load_session reset
    win._restore_cursor(late)                                     # value already == late
    assert rdev in win._reg_view._files and win._reg_view._files[rdev].value(0x1081) == v_late
    win.cursor.set_sample(0)


def test_studio_ui_batch(win):
    from swi3s_studio import __version__ as _ver
    assert "Demo" in win.windowTitle() and _ver in win.windowTitle(), win.windowTitle()
    assert win._reg_view._tree.indentation() <= 12
    # Eye Diagram renamed to Timing; per-section UI loaded.
    assert win._eye_dock.windowTitle() == "Timing"
    assert win._eye_view._sections, "timing view got no per-section UI stats"
    assert all("ui_ns" in s for s in win._eye_view._sections)
    # Raw capture: zoom-to-2-rows runs; a max-zoom (1-UI) floor is set.
    win.cursor.set_sample(win._session.commands[2]["start_sample"])
    win._raw_view.zoom_to_rows(2)
    rx0, rx1 = win._raw_view._plot.getViewBox().viewRange()[0]
    assert rx1 > rx0
    assert win._raw_view._ui_samples > 0
    # Decoded Samples: DP + channel filters are multi-select and return lists.
    from swi3s_studio.ui.decoded_sample_view import _MultiSelect
    sv = win._sample_view
    assert isinstance(sv._port_cb, _MultiSelect) and isinstance(sv._chan_cb, _MultiSelect)
    acts = sv._port_cb._menu.actions()
    if len(acts) >= 2:
        acts[0].setChecked(True); acts[1].setChecked(True)
        ports, _ch, _pred = sv.filters()
        assert ports is not None and len(ports) == 2, ports     # multi-select → list
    # Register no-op edit: forcing a register to the value it already has must NOT start
    # a re-decode or add an override.
    late = win._session.command_cursor_sample(win._session.commands[-1])
    win.cursor.set_sample(int(late))
    files_late = win._session.register_files_at(int(late))
    dev0 = sorted(files_late)[0]
    cur_v = files_late[dev0].value(0x2090)
    before = dict(win._session.register_overrides)
    win._on_register_edited(dev0, 0x2090, cur_v)
    assert win._session.register_overrides == before, "no-op edit changed overrides"
    assert getattr(win, "_load_thread", None) is None, "no-op edit started a re-decode"
    win.cursor.set_sample(0)


def test_the_demo_arrives_on_first_analyzer_entry_not_at_startup():
    """Constructing the window must decode NOTHING. The demo's decode was ~2.3 GB of the
    ~2.6 GB the app sat at on launch, and every session paid it — including the default
    Bus Visualizer ones that never open the Analyzer, and every File ▸ Open that replaced
    it seconds later. It now arrives on the first switch to Bus Analyzer, exactly once."""
    from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION

    w = MainWindow()
    assert w._session is None, "MainWindow.__init__ decoded a capture"
    assert w._demo_preload_pending
    w._mode_mgr.switch_to(ANALYSIS)
    first = w._session
    assert first is not None, "entering Bus Analyzer did not preload the demo"
    assert not w._demo_preload_pending
    # Leaving and coming back must NOT decode again — a new Session object would mean it did.
    w._mode_mgr.switch_to(VISUALIZATION)
    w._mode_mgr.switch_to(ANALYSIS)
    assert w._session is first, "re-entering Bus Analyzer re-decoded the demo"
    w.close()


def test_a_loaded_capture_is_never_replaced_by_the_deferred_demo():
    """The deferred preload must not fire over a capture the user opened. Opening from the
    Visualizer switches into the Analyzer, which is the same code path the preload hangs
    off — so if the pending flag outlived the load, opening a file would show the demo."""
    from swi3s_studio.session import Session
    from swi3s_studio.ui.mode_controller import ANALYSIS

    w = MainWindow()
    assert w._demo_preload_pending
    sess = Session.from_demo(8)                   # stands in for an opened capture
    w.load_session(sess)                          # switches to Analyzer itself
    assert w._session is sess
    assert not w._demo_preload_pending
    w._mode_mgr.switch_to(ANALYSIS)               # explicit re-entry, for good measure
    assert w._session is sess, "the deferred demo replaced the loaded capture"
    w.close()


def test_load_demo_async_when_event_loop_live():
    """When the event loop is running, load_demo goes through the async worker + progress
    dialog (same as an external capture), instead of blocking the GUI thread — the 1 s demo
    is seconds of synthesis + decode. Before the loop starts / in headless tests it stays
    synchronous, so a headless load_demo() returns with the session ready. Pumps events to
    completion. (conftest sets SWI3S_DEMO_SAMPLES=300, so the pumped decode is quick.)"""
    import time
    w = MainWindow()                                    # constructor loads nothing now
    assert w._session is None and getattr(w, "_load_thread", None) is None
    w.load_demo()                                       # loop not live yet => synchronous
    assert w._session is not None and getattr(w, "_load_thread", None) is None
    MainWindow._event_loop_live = True
    try:
        w.load_demo(phy=3)
        assert getattr(w, "_load_thread", None) is not None, "load_demo didn't start the async worker"
        assert getattr(w, "_load_dlg", None) is not None, "no progress dialog shown"
        # Wall-clock wait, not a fixed iteration count: processEvents() returns instantly
        # on an empty queue, so `for _ in range(N)` measures CPU spin, not elapsed time.
        # The worker needs ~7k iterations here but the old range(2000) gave up after
        # ~50 ms — it passed under pytest (warm caches, short conftest demo) and failed
        # as a standalone script, which is what run_all.sh does. Same fix as d0873fd
        # applied to test_ssp.py / test_workspace.py; this sibling was missed.
        deadline = time.time() + 60.0
        while getattr(w, "_load_thread", None) is not None:
            if time.time() > deadline:
                break
            _app.processEvents()
            time.sleep(0.005)
        assert getattr(w, "_load_thread", None) is None, "async demo load never finished"
        assert w._session.link_control.phy_name == "PHY3"
    finally:
        MainWindow._event_loop_live = False


def test_cursor_cascade_coalesces_when_event_loop_live():
    """Clicking around the timeline must not stack a full heavy pane rebuild (register
    replay + symbol/sample table + grid) per click on the main thread. When the event
    loop is live, the heavy half of the cursor cascade is deferred to a single-shot timer
    that coalesces rapid cursor changes into ONE rebuild at the resting cursor; the cheap
    cursor LINE still moves on every change. Headless / tests keep it synchronous."""
    import time
    w = MainWindow()
    w.load_demo()                                    # no longer preloaded in __init__
    s = w._session
    assert s is not None
    N = int(s.capture.clock_edges[-1])
    heavy = [0]
    orig = w._apply_cursor_heavy

    def counted():
        heavy[0] += 1
        orig()
    w._apply_cursor_heavy = counted
    w._cursor_heavy_timer.timeout.disconnect()
    w._cursor_heavy_timer.timeout.connect(counted)

    MainWindow._event_loop_live = True
    try:
        for i in range(15):                              # rapid, no idle between (fast clicking)
            w.cursor.set_sample(int(N * (0.2 + 0.04 * i)))
        assert heavy[0] == 0, "heavy cascade ran synchronously — not deferred/coalesced"
        assert w._timeline._cursor == int(N * (0.2 + 0.04 * 14)), "cursor line didn't track"
        for _ in range(200):                             # let the coalescing timer fire once
            if heavy[0]:
                break
            time.sleep(0.005)
            _app.processEvents()
        assert heavy[0] == 1, f"expected 1 coalesced rebuild, got {heavy[0]}"
    finally:
        MainWindow._event_loop_live = False


def test_teardown_is_idempotent_after_join_worker_threads():
    """The worker-completion handlers must survive join_worker_threads() clearing
    their state out from under them.

    QThread.wait() returns when the worker's run() returns — it does NOT mean the
    worker's queued done/failed signal has been DELIVERED. So closing the window (or
    the atexit hook) mid-load nulls _load_thread, the queued signal then lands, and
    _finish_load re-enters with everything already cleared. It used to raise
    AttributeError inside a Qt slot: PySide6 prints and swallows that, so the visible
    symptom was a stray traceback plus a progress dialog that never closed and a
    worker never deleteLater'd. Both teardowns now bail cleanly.
    """
    w = MainWindow()
    for attrs, fn in (
        (("_load_thread", "_load_dlg", "_load_worker"), w._finish_load),
        (("_locate_thread", "_locate_dlg", "_locate_worker"), w._teardown_locate_thread),
    ):
        for a in attrs:
            setattr(w, a, None)
        fn()                      # must not raise
        for a in attrs:
            assert getattr(w, a) is None, f"{a} resurrected by a no-op teardown"
    w.join_worker_threads()
    w.join_worker_threads()       # idempotent


def test_no_worker_respawns_after_join_worker_threads():
    """Nothing may start a NEW worker thread once the window is closing.

    A worker's queued done/failed signal can still be delivered after
    join_worker_threads() has waited on it; its handler then ran the next queued
    request, spawning a fresh QThread AFTER everything was believed joined — the
    exact destroyed-while-running SIGABRT the join exists to prevent. Both respawn
    paths (_run_pending_load and _start_tx_persist_worker) now honour _closing.
    """
    w = MainWindow()
    w._load_pending = (lambda **kw: w._session, None)
    w._tx_persist_pending = (w._session, 0, object())
    w.join_worker_threads()
    assert w._load_pending is None and w._tx_persist_pending is None, \
        "join left pending work queued"
    w._run_pending_load()
    assert getattr(w, "_load_thread", None) is None, \
        "a load thread was spawned after join_worker_threads()"
    w._start_tx_persist_worker(w._session, 0, object())
    assert getattr(w, "_tx_persist_thread", None) is None, \
        "a TX-persist thread was spawned after join_worker_threads()"
