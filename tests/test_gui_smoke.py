"""Headless GUI smoke test (Qt offscreen): construct the main window, load the
demo capture, and verify the command table, register map, and 2D grid all
populate, and that the time cursor drives the register view.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import swi3score
from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.ui.command_table import _COL
from swi3s_studio.model import Provenance


def main():
    app = QApplication.instance() or QApplication([])

    win = MainWindow()
    win.load_demo()

    # Command table populated: the decoded commands PLUS synthetic 'Commit Point' rows
    # (one per confirmed sync-point commit, at its SSP).
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
    print(f"ok: command table — {model.rowCount()} rows, write -> {label!r}")

    # Response column decoded to symbolic names (not "P:0").
    responses = [model.data(model.index(r, _COL["Response"])) or "" for r in range(model.rowCount())]
    assert any("WRITE_OK" in x for x in responses), responses
    assert any("PING_ATTACHED" in x for x in responses), responses     # per-device ping
    assert not any("P:" in x for x in responses)
    print("ok: response column decoded to symbolic names")

    # Register view populated, ScramblerEn (DP0 0x200B bit3) reset = 1.
    assert win._reg_view.device_count >= 1
    dev0 = win._session.register_files[0]
    assert dev0.value(0x200B) & 0x08, "ScramblerEn reset bit"
    assert dev0.provenance(0x1081) == Provenance.WRITTEN
    print("ok: register view — device(s) present, ScramblerEn reset=1, NumColumns WRITTEN")

    # Grid: at load the cursor sits at t=0, so the bus shows NO active data ports
    # (only the always-present CDS Column 0) — config hasn't happened yet. Scrub to
    # the end to see the fully-decoded config. The grid is multi-emit (source/sink
    # half-cells, merged samples, CDS + headers), so item_count isn't rows*cols.
    assert not win._grid_view._stream_colors, "no data-port streams should be active at t=0"
    # Scrub PAST the SSP to where the config has actually committed. The last command
    # is the SSCR (a sync-point commit) — its config commits at the SSP (Row_Delay
    # rows later), not at the command itself — so scrub to the end of the audio.
    win.cursor.set_sample(win._session.audio_end_sample or win._session.commands[-1]["end_sample"])
    assert win._grid_view.item_count >= win._session.column_count
    # Visualizer-style colouring: each (device, dp) stream gets a distinct
    # data-port colour, and the legend tracks its channels.
    sc = win._grid_view._stream_colors
    assert (0, 0) in sc and (1, 1) in sc                 # 2 devices x 2 DPs
    assert len({c.name() for c in sc.values()}) == len(sc)   # all distinct
    assert win._grid_view.stream_color(0, 0).name() == sc[(0, 0)].name()
    win.cursor.set_sample(0)                              # back to start for later steps
    print(f"ok: bus grid — empty at t=0, {len(sc)} colour-keyed streams once configured")

    # Audio viewer populated (one plot per channel across all streams) + export.
    assert win._audio_view.plot_count == 8, win._audio_view.plot_count   # 2 dev x 2 dp x 2 ch
    assert win._export_action.isEnabled()
    # Curves carry the actual (non-flat) waveform, and Reset Zoom runs.
    _p, curve0, _a, _d, _k, _n = win._audio_view._plots[0]
    ys = curve0.getData()[1]
    assert ys is not None and len(ys) and (ys.max() - ys.min()) > 0, "audio curve is flat"
    win._audio_view.reset_zoom()
    # Channel show/hide: unchecking one stream drops its track; None/All work.
    first_key = next(iter(win._audio_view._checks))
    win._audio_view._checks[first_key].setChecked(False)
    assert win._audio_view.plot_count == 7
    win._audio_view._set_all_channels(False)
    assert win._audio_view.plot_count == 0
    win._audio_view._set_all_channels(True)
    assert win._audio_view.plot_count == 8
    print(f"ok: audio viewer — 8 plots, non-flat, channel toggle + reset zoom")

    # Vertical Zoom: fits each track's Y to the visible data (vs full-scale ±1).
    av = win._audio_view
    plot0, _c0, d0, p0, k0, n0 = av._plots[0]
    # The audio X axis is capture-sample space now, so view the FULL capture extent
    # (not the per-channel index count) to see the whole waveform before vzoom fits Y.
    av._plots and plot0.setXRange(0, max(1, av._capture_extent), padding=0)
    ex, elo, ehi = win._audio_store.envelope(d0, p0, k0, 0, max(1, n0), max_points=2000)
    scale = float(1 << (win._audio_store.sample_bits(d0, p0, k0) - 1))
    data_max = float(max((ehi / scale).max(), abs((elo / scale).min())))
    av._vzoom_btn.setChecked(True)
    y0, y1 = plot0.getViewBox().viewRange()[1]
    assert av._vzoom and y1 <= 1.0 + 1e-6                     # fit within full scale
    assert abs(y1 - data_max) < 0.2 or data_max > 0.9        # fits the data (unless already full-scale)
    av._vzoom_btn.setChecked(False)
    y0b, y1b = plot0.getViewBox().viewRange()[1]
    assert y1b >= 0.9                                         # back to ~full-scale ±1
    # Time axis adapts unit + comma-separates when zoomed (µs / ms / s).
    from swi3s_studio.ui.audio_view import _TimeAxis
    tax = _TimeAxis(orientation="bottom"); tax.set_rate(48000)
    assert tax.tickStrings([297600], 1, 10)[0].endswith("µs")          # fine spacing → µs
    assert "," in tax.tickStrings([297600], 1, 10)[0]                  # thousands separated
    assert tax.tickStrings([48000, 96000], 1, 48000) == ["1 s", "2 s"]  # coarse → s
    print("ok: audio viewer — vertical zoom fits Y; time axis adaptive µs/ms/s with commas")

    # Raw Capture shares the same adaptive comma-separated time axis (x is in seconds).
    from swi3s_studio.ui.raw_view import _RawTimeAxis
    rax = _RawTimeAxis(orientation="bottom")
    zt = rax.tickStrings([4.3021955], 1, 0.5e-6)[0]      # zoomed to sub-µs spacing
    assert zt.endswith("µs") and "," in zt, zt           # → '4,302,195.50 µs'
    assert rax.tickStrings([1.0, 2.0], 1, 1.0) == ["1 s", "2 s"]
    print("ok: raw capture — adaptive comma-separated time axis (µs/ms/s)")

    # Audio cursor line: one per plot, positioned at the shared cursor's CAPTURE sample
    # directly (the audio X axis is capture-sample space, shared by every track).
    assert len(win._audio_view._cursor_lines) == win._audio_view.plot_count
    late = win._session.commands[-1]["start_sample"]
    win.cursor.set_sample(late)
    _p, _c, dev, dp, ch, _n = win._audio_view._plots[0]
    assert win._audio_view._cursor_lines[0].value() == late
    # The index<->sample map still round-trips (used by playback + export).
    i0, _i1 = win._audio_store.index_range_for_samples(dev, dp, ch, late, late)
    assert win._audio_store.sample_at_index(dev, dp, ch, i0) is not None
    win.cursor.set_sample(0)                  # restore baseline for later sections
    print("ok: audio cursor line tracks the shared cursor in capture-sample space")

    # Playback controls exist and are idle; stop() is safe with nothing playing.
    assert hasattr(win._audio_view, "play") and not win._audio_view.is_playing
    win._audio_view.stop()                    # no-op, must not raise
    print("ok: audio playback controls present + idle")

    # CDS symbol viewer populated, first row is the comma.
    assert win._symbol_view.symbol_count > 0
    print(f"ok: symbol viewer — {win._symbol_view.symbol_count} symbols")

    # Symbol viewer follows the cursor via windowed (seeked) re-decode: jumping
    # to a later command shows fewer symbols than the from-start full decode.
    win._symbol_dock.show()
    full_syms = win._symbol_view.symbol_count
    win.cursor.set_sample(win._session.commands[2]["start_sample"])
    assert 0 < win._symbol_view.symbol_count <= full_syms
    win.cursor.set_sample(0)                       # back to start -> full window again
    assert win._symbol_view.symbol_count >= full_syms
    print("ok: symbol viewer follows cursor (windowed re-decode)")

    # Decode-on-scroll back-history: prepending earlier symbols grows the list above
    # (keeping the current rows), and an empty prepend marks the start.
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
    print("ok: symbol viewer decode-on-scroll (prepend earlier symbols)")

    # Synchronized navigation: selecting a row moves the cursor to that command's
    # start (command_cursor_sample). A commit COMMAND stays at the command (pre-commit
    # state); the separate 'Commit Point' row sits at the SSP (post-commit).
    last = model.rowCount() - 1
    win._cmd_view.selectRow(last)
    assert win.cursor.sample == win._session.command_cursor_sample(
        model.command_at(last))                     # row->command via the model (augmented list)
    # As-of t=0 (before any write) NumColumns is still at its DEFAULT.
    early = win._session.register_files_at(0)
    if 0 in early:
        assert early[0].provenance(0x1081) == Provenance.DEFAULT
    print(f"ok: synchronized cursor — selecting row {last} set sample {win.cursor.sample}")

    # Selecting a command selects ITS beginning CDS symbol (the SPM comma, whose
    # bus row == the command's bus_row) and scrolls it to the top of the pane.
    wcmd_row = next(r for r in range(model.rowCount())
                    if model.data(model.index(r, _COL["Opcode"])) == "WriteA32")
    win._cmd_view.selectRow(wcmd_row)
    wcmd = model.command_at(wcmd_row)               # row->command via the model
    sel = win._symbol_view.selectionModel().selectedRows()
    assert sel, "command selection did not select a CDS symbol"
    sel_sym = win._symbol_view._symbols[sel[0].row()]
    assert int(sel_sym["row"]) == int(wcmd["bus_row"]), (sel_sym["row"], wcmd["bus_row"])
    assert sel_sym["kind"] == 1, "selected CDS symbol should be the SPM comma"
    # The command's start_sample is the Row Sync Point (its SPM comma's first bit),
    # so it matches the comma symbol's start_sample exactly (not just the row).
    assert int(wcmd["start_sample"]) == int(sel_sym["start_sample"]), \
        (wcmd["start_sample"], sel_sym["start_sample"])
    print("ok: command selection -> its SPM comma symbol selected at pane top")

    # Selecting a CDS symbol row moves the shared cursor and the command table.
    sv = win._symbol_view
    if sv.rowCount() > 5:
        row = min(20, sv.rowCount() - 1)
        samp = sv.sample_at_row(row)
        sv.selectRow(row)
        assert win.cursor.sample == samp, (win.cursor.sample, samp)
        assert win._cmd_view.selectionModel().selectedRows(), "command table didn't follow symbol"
        print("ok: CDS symbol selection moves cursor + command table")

    # Timeline seek -> cursor -> nearest command selected (two-way sync).
    target = win._session.commands[2]["start_sample"]
    win._on_seek(target + 5)
    assert win.cursor.sample == target + 5
    sel = win._cmd_view.selectionModel().selectedRows()
    assert sel and sel[0].row() == 2, sel
    print("ok: timeline seek -> cursor -> nearest command (row 2)")

    # Timeline bus-config bands: segments loaded, each distinct column count gets
    # a band colour, and segment lookup works.
    assert win._timeline._segments, "timeline got no config segments"
    assert win._timeline._seg_band, "no band colours assigned"
    # Segment lookup: the last segment starting at/before a mid-capture command is the
    # operational geometry (the hover-tooltip helper was removed; look it up directly).
    smp = win._session.commands[2]["start_sample"]
    seg = next((s for s in reversed(win._timeline._segments)
                if int(s.get("start_sample", 0)) <= smp), None)
    assert seg is not None and seg["column_count"] == win._session.column_count
    print(f"ok: timeline config bands — {len(win._timeline._segments)} segment(s), "
          f"{len(win._timeline._seg_band)} geometr(y/ies)")

    # Navigation: View menu can re-show a closed dock. (Use isHidden(), since the
    # top-level window isn't shown under the offscreen platform.)
    win._grid_dock.close()
    assert win._grid_dock.isHidden()
    win._show_dock(win._grid_dock)
    assert not win._grid_dock.isHidden()
    # Re-shown dock re-joins its tab group (so the tab bar isn't lost).
    tabbed = win.tabifiedDockWidgets(win._grid_dock)
    assert win._audio_dock in tabbed or win._symbol_dock in tabbed, tabbed
    print("ok: View navigation — closed Bus Grid reopened + re-tabified")

    # Re-show robustness: a dock that was floated then closed (a macOS quirk path)
    # must come back docked in its area and re-tabified, not as a stray window.
    from PySide6.QtCore import Qt as _Qt
    ad = win._audio_dock
    ad.setFloating(True)
    ad.close()
    win._show_dock(ad)
    assert not ad.isHidden() and not ad.isFloating()
    assert win.dockWidgetArea(ad) == _Qt.BottomDockWidgetArea
    tb = win.tabifiedDockWidgets(ad)
    assert win._grid_dock in tb or win._symbol_dock in tb, tb
    print("ok: floated+closed Audio dock re-docks and re-tabifies")

    # Compare: the expected register config shows in the CSV-Import colour in the
    # register view, distinct from a bus write/read; Clear removes it.
    rv = win._reg_view
    win._cmd_view.selectRow(model.rowCount() - 1)     # cursor -> end (all writes applied)
    dev0_file = win._session.register_files.get(0)
    rv.set_expected([(0, 0x200B, 0x00)])
    assert rv.has_expected()
    _v, prov = rv._effective(0, 0x200B, dev0_file)
    assert prov == Provenance.CSV and _v == 0x00
    rv.clear_expected()
    assert not rv.has_expected()
    _v2, prov2 = rv._effective(0, 0x200B, dev0_file)
    assert prov2 != Provenance.CSV            # overlay gone -> back to decoded
    print("ok: compare — expected-config register overlay (blue), clear restores")

    # Filter: kind filter narrows rows; errors-only hides everything (demo has no
    # errors); clearing restores. The Filter menu drives the proxy.
    total_rows = win._cmd_proxy.rowCount()
    win._cmd_proxy.set_kinds({"WriteA32"})
    writes = win._cmd_proxy.rowCount()
    assert 0 < writes < total_rows, (writes, total_rows)
    win._cmd_proxy.set_kinds(set())
    # Boolean free text (and / or).
    win._cmd_proxy.set_text("writea32 or ping")
    assert win._cmd_proxy.rowCount() > writes
    win._cmd_proxy.set_text("")
    win._errors_only_act.setChecked(True)
    assert win._cmd_proxy.rowCount() == 0    # demo is clean
    win._errors_only_act.setChecked(False)
    assert win._cmd_proxy.rowCount() == total_rows
    # Filter menu: the command-table title reflects an active filter, and Clear resets.
    win._cmd_proxy.set_kinds({"WriteA32"})
    win._update_cmd_title()
    assert "of" in win._cmd_title.text() and "Cmd" in win._cmd_title.text()
    win._clear_all_filters()
    assert win._cmd_title.text() == "Command Table"
    print(f"ok: filter/search — kind WriteA32 -> {writes}/{total_rows}, errors-only -> 0")

    # Measurements panel populated.
    assert win._meas_view.metric_count > 0
    print(f"ok: measurements — {win._meas_view.metric_count} metrics")

    # Bookmarks: toggle at cursor, navigate, clear.
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
    print("ok: bookmarks — toggle/next/clear")

    # Large-capture sample numbers (> 2**31) must survive the cursor/timeline
    # signals: a 32-bit Signal(int) overflows and breaks slot dispatch. Test the
    # signals in isolation so the assertion doesn't depend on cursor-follow logic.
    from swi3s_studio.ui.cursor import TimeCursor
    from swi3s_studio.ui.timeline import TimelineRibbon
    big = 2_400_000_000
    seen = []
    tc = TimeCursor(); tc.sampleChanged.connect(lambda s: seen.append(("cursor", s)))
    tc.set_sample(big)
    tr = TimelineRibbon(); tr.seeked.connect(lambda s: seen.append(("seek", s)))
    tr.seeked.emit(big)                   # 32-bit signal would raise "Slot not found"
    assert seen == [("cursor", big), ("seek", big)], seen
    print("ok: 64-bit sample signals (> 2**31) — no overflow / slot loss")

    # Raw Capture zoom behaviour: a re-decode of the SAME capture (e.g. enabling the
    # lazy Sample Bits overlay, or a what-if override) must PRESERVE the user's zoom +
    # the Sample Bits toggle — only a new capture re-fits the view.
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
    print("ok: raw capture — zoom + Sample Bits toggle survive a same-capture re-decode")

    # Zoom-to-row: with a cursor set, "1 Row" shrinks the X span to ~one bus row.
    mid = win._session.commands[len(win._session.commands) // 2]["start_sample"]
    raw.set_cursor(int(mid))
    raw.zoom_to_row()
    rx0, rx1 = raw._plot.getViewBox().viewRange()[0]
    assert 0 < (rx1 - rx0) < full_span, (rx1 - rx0, full_span)   # zoomed to a small window
    assert rx0 <= mid / win._session.sample_rate_hz <= rx1, "cursor not inside the one-row window"
    raw.reset_zoom()
    print("ok: raw capture — zoom-to-row shrinks to a single bus row around the cursor")

    # Register map editing path: right-click context-menu and double-click share
    # _prompt_edit, which emits registerEdited(device, address, value) for an
    # editable row. The context menu fires via _RegisterTree.contextMenuEvent (NOT the
    # CustomContextMenu policy, which doesn't deliver for item-view body clicks on
    # macOS). Both dual-ranked ranks (NEXT and CURR peer) are editable, redirecting to
    # the register's NEXT-base. Test in isolation so the emit doesn't kick off the main
    # window's async re-decode.
    from PySide6.QtCore import Qt as _Qt2
    from PySide6.QtWidgets import QInputDialog
    from swi3s_studio.ui.register_view import RegisterView
    rv2 = RegisterView(win._rmap)
    rv2.set_files(win._session.register_files_at(win._session.commands[-1]["start_sample"]))
    # The tree reports body right-clicks via contextRequested (contextMenuEvent), not
    # the (unreliable-for-body) CustomContextMenu policy.
    assert rv2._tree.contextMenuPolicy() != _Qt2.CustomContextMenu
    assert hasattr(rv2._tree, "contextRequested")

    def _find_editable(item, want_label=None):
        for i in range(item.childCount()):
            c = item.child(i)
            if c.data(0, _Qt2.UserRole) and (want_label is None or want_label in c.text(0)):
                return c
            found = _find_editable(c, want_label)
            if found is not None:
                return found
        return None

    editable = _find_editable(rv2._tree.invisibleRootItem())
    assert editable is not None, "no editable register row found"
    # A dual-ranked CURR peer row is editable too, redirecting to the NEXT-base address.
    curr_row = _find_editable(rv2._tree.invisibleRootItem(), "(CURR)")
    if curr_row is not None:
        assert curr_row.data(0, _Qt2.UserRole), "CURR row should be editable (redirects to NEXT-base)"
    # Context menu: built (not shown) via _context_menu_for — 'Force value…' and (when
    # the register has fields) 'Edit fields…' enabled for an editable row, greyed for a
    # header/empty click; 'Clear all manual edits' present.
    menu = rv2._context_menu_for(editable)
    acts = {a.text(): a for a in menu.actions()}
    force = next(a for t, a in acts.items() if t.startswith("Force"))
    assert force.isEnabled()
    assert any("Clear" in t for t in acts)
    # No row → 'Force value…' greyed.
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
    print("ok: register map — context menu (built) + edit path emits registerEdited (NEXT + CURR)")

    # Field-level editor: a register with sub-byte fields exposes them; 'Edit fields…'
    # is offered, and composing a field value writes exactly that field's bits.
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
    # Field editor bounds: a partial-enum field (only a reserved top value) is edited as
    # a bounded number that EXCLUDES the reserved value, so every valid value is
    # enterable and reserved is not; a fully-enumerated field stays a dropdown.
    from swi3s_studio.ui.register_view import _field_value_max, _enum_covers
    from swi3s_studio.model.registers import FieldSpec
    bw = FieldSpec(name="BitWidth", hi=1, lo=0, reset=0, enum={3: "Reserved"})
    assert not _enum_covers(bw)
    assert _field_value_max(bw, 0) == 2       # 0..2 (3 = Reserved excluded)
    assert _field_value_max(bw, 3) == 3       # a currently-set reserved value stays allowed
    assert _enum_covers(FieldSpec(name="En", hi=0, lo=0, reset=0, enum={0: "Off", 1: "On"}))
    print("ok: register map — field editor (editable fields + compose + valid-range bounds)")

    # Command Table Devices uses ranges to save space.
    from swi3s_studio.ui.command_table import _collapse_ranges
    assert _collapse_ranges([1, 3, 4, 5]) == "1,3-5"
    assert _collapse_ranges([0, 1, 2]) == "0-2"
    assert _collapse_ranges([2]) == "2"

    # Command Table: wide-label narrow columns get two-line headers so they shrink.
    assert model.headerData(_COL["Packet Length"], _Qt2.Horizontal, _Qt2.DisplayRole) == "Packet\nLength"
    assert model.headerData(_COL["Group Mask"], _Qt2.Horizontal, _Qt2.DisplayRole) == "Group\nMask"
    assert model.headerData(_COL["Devices"], _Qt2.Horizontal, _Qt2.DisplayRole) == "Devices"
    assert win._cmd_view.verticalHeader().isHidden()      # empty row-index column gone
    print("ok: command table — wrapped narrow-column headers + no row-index column")

    # Re-decode cursor restore: _restore_cursor re-syncs the panes at the given sample
    # even when the cursor VALUE is unchanged (load_session re-inits panes at t=0 and
    # TimeCursor.set_sample no-ops on an equal value — the reported "edit moves the
    # cursor to the beginning"). After a simulated t=0 reset, restoring at `late` must
    # bring the register view back to the late state.
    late = int(win._session.command_cursor_sample(win._session.commands[-1]))
    win.cursor.set_sample(late)
    files_late = win._session.register_files_at(late)
    rdev = sorted(files_late)[0]
    v_late = files_late[rdev].value(0x1081)
    win._reg_view.set_files(win._session.register_files_at(0))   # simulate load_session reset
    win._restore_cursor(late)                                     # value already == late (a no-op set_sample)
    assert rdev in win._reg_view._files and win._reg_view._files[rdev].value(0x1081) == v_late
    win.cursor.set_sample(0)
    print("ok: re-decode cursor restore — panes re-sync at cursor (not the beginning)")

    # --- studio UI batch ---
    # Window title reminds which capture is loaded, with the app version.
    from swi3s_studio import __version__ as _ver
    assert "Demo" in win.windowTitle() and _ver in win.windowTitle(), win.windowTitle()
    # Register map: reduced indentation to save horizontal space.
    assert win._reg_view._tree.indentation() <= 12
    # Eye Diagram renamed to Timing; per-section UI loaded.
    assert win._eye_dock.windowTitle() == "Timing"
    assert win._eye_view._sections, "timing view got no per-section UI stats"
    assert all("ui_ns" in s for s in win._eye_view._sections)
    # Raw capture: zoom-to-1-row and 2-row both run; a max-zoom (1-UI) floor is set.
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
    # Register no-op edit: forcing a register to the value it already has must NOT
    # start a re-decode or add an override (the reported "unchanged → fresh decode").
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
    print("ok: studio UI batch — title, indent, Timing rename+sections, 2-row zoom, "
          "multi-select samples, no-op register edit")

    print("GUI SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
