"""PHY3 (DLV) demo round-trip: the differential low-voltage PHY synthesized + decoded.

PHY3 has no forwarded clock — a virtual PLL in DlvSampleSource recovers the bit clock
from the once-per-row Sync1 edges and samples mid-UI; the CDS is a plain-NRZ bit at
Column 2 (no NRZS); Col 0 = Sync1 / last col = Sync0 are framing. Everything from the
bit level up (8b/10b, commands, scrambler, audio) is the shared FBCSE core. So a PHY3
demo must decode to the SAME audio as the PHY2 demo (identical content, different PHY).

Run: PYTHONPATH=. python3 -m pytest tests/test_phy3_demo.py
"""
import numpy as np
import swi3score

from swi3s_studio.analysis.link_control import decode_link_control
from swi3s_studio.ingest import transitions
from swi3s_studio.session import Session


def test_phy3_cold_start_selects_dlv():
    """A PHY3 cold start decodes as DLV / Safe-Lock-4 (same clocked-burst selection as
    PHY2, PhyNum 0b0011)."""
    cap = transitions.demo_capture(64, cold_start=True, phy=3)
    lc = decode_link_control(cap)
    assert lc.sequence == "cold" and lc.phy_number == 3
    assert lc.phy_name == "PHY3" and lc.phy_kind == "DLV"
    assert lc.safe_lock_columns == 4
    assert lc.phystart_sample is not None and lc.audio_start_sample is not None


def test_phy3_synthesis_framing():
    """DLV synthesis: per-UI logical differential levels with Sync1 in Column 0 and
    Sync0 in the last column of every operational (16-col) row. `row_columns` gives
    each row's width (4 in Safe-Lock, 16 operational)."""
    d = swi3score.make_demo_levels_phy3(200)
    levels, row_cols = d["levels"], d["row_columns"]
    # Offset of each row into the flat, variable-width level plan.
    offs, acc = [], 0
    for c in row_cols:
        offs.append(acc); acc += int(c)
    # Pick ten operational (16-col) rows deep in the stream.
    wide = [(o, int(c)) for o, c in zip(offs, row_cols) if int(c) == 16]
    assert len(wide) > 60, len(wide)
    sample = wide[50:60]
    col0 = [levels[o] for o, _ in sample]
    col15 = [levels[o + 15] for o, _ in sample]
    assert all(col0), "Column 0 should be Sync1 (logical 1) every row"
    assert not any(col15), "last column should be Sync0 (logical 0) every row"


def test_phy3_dlv_capture_is_complementary_pair():
    """The DLV capture stores DP and DN as a complementary differential pair (same edge
    samples, opposite initial levels)."""
    cap = transitions.demo_capture(64, phy=3)             # audio only
    assert np.array_equal(cap.clock_edges, cap.data_edges)
    assert bool(cap.initial_clock) != bool(cap.initial_data)


def test_phy3_decode_convergence():
    """The DLV front-end + shared core decode CRC-valid commands, the SSCR reconfig
    (Safe-Lock-4 -> 16 columns), and all four audio streams."""
    s = Session.from_demo(400, cold_start=True, phy=3)
    assert s.link_control.phy_name == "PHY3"
    assert s.commands and all(c["crc_valid"] for c in s.commands)
    assert any(c["is_commit"] and c["commit_confirmed"] for c in s.commands)
    assert [seg.get("column_count") for seg in s.segments] == [4, 16]
    assert s.column_count == 16
    assert s.audio_store().streams() == [(0, 0), (0, 1), (0, 2), (0, 3)]


def test_phy3_audio_bit_exact_with_phy2():
    """The headline round-trip: PHY3 (DLV, recovered clock) decodes to the SAME audio
    samples as PHY2 (FBCSE, forwarded clock) — the demos carry identical content."""
    s2 = Session.from_demo(400, cold_start=True, phy=2)
    s3 = Session.from_demo(400, cold_start=True, phy=3)

    def vals(s, dev, dp):
        return [a["value"] for a in s.decoder.audio() if a["device"] == dev and a["dp"] == dp]

    for dp in (0, 1, 2, 3):
        a2, a3 = vals(s2, 0, dp), vals(s3, 0, dp)
        n = min(len(a2), len(a3))
        assert n > 0 and a2[:n] == a3[:n], f"DP{dp} audio differs between PHY2 and PHY3"
        # PCM (dp 0/1) samples/channel match exactly; PDM (dp 2/3) may differ by a
        # trailing sample or two at the capture end (edge truncation), which is fine.
        assert abs(len(a2) - len(a3)) <= 2, (dp, len(a2), len(a3))


def test_phy3_windowed_bit_samples_resolve():
    """The Capture 'Show Samples' overlay works for DLV: the on-demand windowed bit-
    sample decode resolves port sample points in the audio region (it needs the DLV
    sample source to be seekable — it wasn't, so nothing drew). Marks cover every
    active dataport, are all inside the queried window, and a deep window resolves too
    (seek is O(1), not a replay from the start)."""
    s = Session.from_demo(600, cold_start=True, phy=3)
    ui = s.recovered_clock()[1]
    seg16 = s.segments[1]
    lo = int(seg16["start_sample"] + 10 * 16 * ui)
    hi = lo + int(30 * 16 * ui)
    m = s.port_bit_marks(lo, hi)
    assert m["sample"].size > 0, "no bit samples drawn in the DLV audio region"
    assert bool(((m["sample"] >= lo) & (m["sample"] <= hi)).all())
    assert set(np.unique(m["dp"]).tolist()) == {0, 1, 2, 3}      # all four demo dataports
    assert int(m["is_start"].sum()) > 0                          # MSBs flagged
    # A window far into the capture resolves via seek (empty would mean seek failed).
    deep_lo = int(0.8 * s.audio_end_sample)
    deep = s.port_bit_marks(deep_lo, deep_lo + int(30 * 16 * ui))
    assert deep["sample"].size > 0


def test_phy3_virtual_pll_recovers_clock():
    """The virtual PLL holds a CONSTANT row period across the Safe-Lock-4 -> 16 commit, so
    the recovered UI rises 4x (Safe-Lock -> operational) as the row subdivides into more
    columns. Seeded with the Safe-Lock nominal UI so it locks to the once-per-row reference;
    it ends at the operational UI = sample_rate / 49.152 MHz (non-integer at the 500 MHz
    analyzer rate: ~10.17 samples)."""
    cap = transitions.demo_capture(200, phy=3)            # audio only (locks at row 0)
    op_ui = cap.sample_rate_hz / 49_152_000.0                 # operational UI in samples
    nominal = cap.sample_rate_hz / (3_072_000.0 * 4)          # Safe-Lock-4 UI seed
    src = swi3score.DlvSampleSource(cap.clock_edges.astype(np.uint64),
                                    bool(cap.initial_clock), int(cap.sample_rate_hz), 4, nominal)
    st = swi3score.DecoderSettings()
    st.dlv = True; st.cds_horizontal_start = 2; st.forced_column_count = 4
    swi3score.Decoder(src, st).run()
    assert abs(src.recovered_ui_samples() - op_ui) < 1.0, (src.recovered_ui_samples(), op_ui)
    assert src.recovered_row_syncs().size > 100          # one per decoded row


def test_phy3_grid_layout_s1_cds_s0():
    """The bus grid frames a DLV row as S1 (Column 0) · CDS (Column 2) · … · S0 (last
    column) — not the FBCSE CDS-at-Column-0 layout. Checked on both grid paths (the
    decoder's own grid and the as-of-cursor command replay the UI actually draws),
    and confirmed the audio geometry is the full 16 columns (not the 4-col Safe-Lock
    segment the sparse DLV clock_edges used to mis-select)."""
    s = Session.from_demo(400, cold_start=True, phy=3)
    smp = int(s.audio_end_sample) - 1

    def frame(cells):
        by_col = {}
        for c in cells:
            if c.get("is_cds"):
                by_col.setdefault(int(c["col"]), "CDS")
            elif c.get("slot") in (7, 8):        # grid-only S0=7 / S1=8 system slots
                by_col.setdefault(int(c["col"]), "S0" if c["slot"] == 7 else "S1")
        return by_col

    for cells in (s.grid_cells(16), s.grid_cells_at(smp, 16)):
        fr = frame(cells)
        assert fr.get(0) == "S1", fr
        assert fr.get(2) == "CDS", fr
        assert fr.get(s.column_count - 1) == "S0", fr   # S0 at the LAST column (col 15)
    # PHY2 (FBCSE) keeps CDS at Column 0 and emits no S0/S1.
    s2 = Session.from_demo(400, cold_start=True, phy=2)
    fr2 = frame(s2.grid_cells_at(int(s2.audio_end_sample) - 1, 16))
    assert fr2.get(0) == "CDS" and 8 not in fr2.values() and "S0" not in fr2.values()


def test_phy3_rsp_marks_are_periodic():
    """The Raw-view RSP (Row Sync Point) marks for a DLV capture are the virtual PLL's
    recovered row-opening edges — periodic (one per row) AND landing on the actual DP
    0→1 (S0→S1) edge that opens each row, all through the audio region (not just at the
    start, where the old sample-source row counter tracked before drifting)."""
    s = Session.from_demo(3000, cold_start=True, phy=3)
    clk = np.asarray(s.capture.clock_edges, dtype=np.int64)
    first_rise = 1 if bool(s.capture.initial_clock) else 0
    rising = set(clk[first_rise::2].tolist())            # DP-wire 0→1 edges
    syncs = np.asarray(s.recovered_clock()[0], dtype=np.int64)
    assert syncs.size > 50
    # Every recovered RSP lands on a real 0→1 edge (sampled across the whole capture).
    assert all(int(x) in rising for x in syncs[::7]), "an RSP is not on a DP 0→1 edge"
    # A window deep in the audio region yields evenly spaced marks, one per row.
    lo, hi = int(syncs[-12]), int(syncs[-2])
    marks = np.asarray(s.cds_column_samples(lo, hi)).astype(np.int64)
    assert marks.size >= 8, marks.size
    assert all(int(m) in rising for m in marks)
    gaps = np.diff(marks)
    assert gaps.size and (gaps.max() - gaps.min()) <= 1, gaps.tolist()   # near-uniform (±1 jitter)


def test_phy3_pll_lock_is_healthy_on_clean_demo():
    """Every recovered RSP on the clean demo has a real 0→1 edge under it, so there are
    no missing-edge flags and the PLL is not reported unlocked."""
    s = Session.from_demo(3000, cold_start=True, phy=3)
    assert s.pll_missing_rsp_count == 0
    assert s.pll_unlocked is False
    assert np.asarray(s.missing_rsp_samples(0, int(s.audio_end_sample))).size == 0
    # FBCSE never reports DLV PLL state.
    assert Session.from_demo(400, cold_start=True, phy=2).pll_unlocked is False


def test_phy3_pll_unlock_threshold():
    """More than PLL_UNLOCK_MISSING_THRESHOLD missing RSP edges flags the PLL unlocked,
    and missing_rsp_samples reports each missing edge within the queried window."""
    s = Session.from_demo(400, cold_start=True, phy=3)
    thr = s.PLL_UNLOCK_MISSING_THRESHOLD
    rsp = np.arange(1000, 1000 + 100 * 512, 512, dtype=np.int64)
    # Inject exactly threshold missing → still locked.
    s._rsp_analysis_cache = (rsp, rsp[:thr], 32.0)
    s._rec_syncs_cache = None
    assert s.pll_missing_rsp_count == thr and s.pll_unlocked is False
    # One more → unlocked; and the window filter returns the ones in range.
    s._rsp_analysis_cache = (rsp, rsp[:thr + 1], 32.0)
    assert s.pll_unlocked is True
    win = np.asarray(s.missing_rsp_samples(int(rsp[1]), int(rsp[3])))
    assert win.tolist() == rsp[1:4].tolist()


def test_phy3_cursor_row_column_consistent_across_views():
    """A cursor sample in the 16-col audio region must map to the SAME geometry
    everywhere: the column count (Bus Grid), the bus row (Timeline / commands), and the
    inverse row→sample all agree. Regression: DLV `_ui_for_sample` used to index the
    sparse clock_edges, so bus_row_for_sample landed below segment 1's row_base and the
    Timeline read 4-col while the grid read 16-col for the same cursor."""
    s = Session.from_demo(3000, cold_start=True, phy=3)
    assert [seg["column_count"] for seg in s.segments] == [4, 16]
    seg16 = s.segments[1]
    # A sample a few rows into the 16-col audio segment.
    ui = s.recovered_clock()[1]
    smp = int(seg16["start_sample"] + 8 * 16 * ui)
    assert s.column_count_at(smp) == 16                        # Bus Grid
    assert s._segment_for_sample(smp)["column_count"] == 16
    row = s.bus_row_for_sample(smp)                            # Timeline / commands
    assert row >= int(seg16["row_base"]), (row, seg16["row_base"])   # in the 16-col segment
    assert int(s.bus_rows_for_samples(np.array([smp]))[0]) == row    # batch == scalar
    # Inverse maps the row back to a sample within that same row (its RSP anchor).
    back = s._sample_at_bus_row(row)
    assert 0 <= smp - back < int(round(16 * ui)), (smp, back)


def test_phy3_demo_source_label():
    """The window/source label names which PHY demo is loaded."""
    assert Session.from_demo(64, cold_start=True, phy=3).source_label() == "PHY3 Demo Capture"
    assert Session.from_demo(64, cold_start=True, phy=2).source_label() == "PHY2 Demo Capture"


def test_phy3_top_trace_is_dp_polarity_so_rsp_is_a_rising_edge():
    """The Raw view's top 'V(DP−DN)' trace must show DP polarity (high = Sync1 / logical
    1), so a Row Sync Point (the Sync0→Sync1 0→1 edge the RSP marks) lands on a RISING
    edge of the shown signal — not a falling one (the bug: the top band was fed the
    complementary DN wire, inverting it)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.raw_view import RawCaptureView

    s = Session.from_demo(400, cold_start=True, phy=3)
    rv = RawCaptureView()
    rv.set_capture(s.capture, dlv=True, audio_start=int(s.audio_start_sample),
                   recovered_clock=s.recovered_clock())

    def level_at(edges, init, x):        # DP/DN level at sample x
        n = int(np.searchsorted(np.asarray(edges, dtype=np.int64), x, side="right"))
        return bool(init) ^ (n % 2 == 1)

    syncs = np.asarray(s.recovered_clock()[0], dtype=np.int64)
    x = int(syncs[syncs > s.audio_start_sample + 5000][10])   # a Sync1 deep in audio
    # Top band (_dp_edges) carries the DP wire, so the differential is HIGH at the RSP.
    assert level_at(rv._dp_edges, rv._dp_init, x) is True, "top trace inverted at the RSP"
    assert np.array_equal(np.asarray(rv._dp_edges, dtype=np.int64),
                          np.asarray(s.capture.clock_edges, dtype=np.int64))  # DP = clock_edges


def test_phy3_ui_load_demo_and_raw_view():
    """The 'Open Demo Capture ▸ PHY3' menu path loads a DLV session, and the Raw Capture
    view renders it as a differential signal + recovered bit clock (FBCSE labels + no
    recovered clock on a PHY2 reload)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    win = MainWindow()
    win.load_demo(phy=3)
    assert win._session.is_dlv and win._session.link_control.phy_name == "PHY3"
    rv = win._raw_view
    assert rv._dlv and rv._rec_ui > 0.0
    assert rv._top_name == "V(DP−DN)" and rv._bot_name == "Rec Clk"
    # The recovered bit clock draws in the DLV audio region.
    a = win._session.audio_start_sample
    rv._render_range(a + 5000, a + 5000 + 18 * 32)
    xc, _yc = rv._rec_clk_curve.getData()
    assert xc is not None and len(xc) > 0, "recovered clock not drawn in the audio region"
    # PHY2 reload restores the FBCSE two-line view (no recovered clock).
    win.load_demo(phy=2)
    assert not win._raw_view._dlv
    win._raw_view._render_range(0, 100000)
    xc2, _ = win._raw_view._rec_clk_curve.getData()
    assert xc2 is None or len(xc2) == 0


def test_phy3_cds_symbols_populate():
    """The CDS symbol table (SymbolView) is POPULATED for DLV — it reads the plain-NRZ CDS
    at Column 2 via the recovered-clock source, not NRZS at Column 0 (which read the Sync1
    framing and left the table empty/all-NO_RESPONSE). Same symbol mix as PHY2, and clicking
    a command (symbols_around) resolves its 8b/10b symbols."""
    s3 = Session.from_demo(400, cold_start=True, phy=3)
    syms = s3.symbols(200)
    # kinds: 1=comma, 2=token, 3=data (real 8b/10b) — must dominate, not NO_RESPONSE (5).
    real = sum(1 for x in syms if x["kind"] in (1, 2, 3))
    assert real > 150, f"CDS symbols not decoding (only {real} real of {len(syms)})"
    assert any(x["kind"] == 1 for x in syms), "no comma decoded on the DLV CDS"
    # Clicking any command resolves its symbols (was empty — start_ui landed off Column 0).
    for c in [c for c in s3.commands if c["command"] == "WriteA32"][:3]:
        around = s3.symbols_around(int(c["start_sample"]), 30)
        assert around and any(x["kind"] in (1, 2, 3) for x in around), "symbols_around empty for DLV"


def test_phy3_recovered_clock_speeds_up_at_commit():
    """The recovered bit clock the Raw view draws changes rate at the commit: it packs one
    cycle per column per row, and the row RATE is constant, so the Safe-Lock-4 region draws
    4 cycles/row and the 16-column operational region 16 — a 4x-faster clock after the commit
    (not the single fixed period the old constant-UI drawing showed everywhere)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.raw_view import RawCaptureView

    s = Session.from_demo(600, cold_start=True, phy=3)
    rv = RawCaptureView()
    rv.set_capture(s.capture, dlv=True, audio_start=int(s.audio_start_sample),
                   recovered_clock=s.recovered_clock(), segments=s.segments)
    rate = s.sample_rate_hz
    row = rate / (s.row_rate_khz * 1000.0)

    def cycles_per_row(seg):
        lo = int(seg["start_sample"]) + int(20 * row)
        xs, _ = rv._rec_clock_xy(lo, lo + int(2 * row), 0.0, 1.0)
        starts = (np.asarray(xs) * rate)[0::5] if len(xs) else np.array([])
        return int(((starts >= lo) & (starts < lo + 2 * row)).sum()) / 2.0

    by_cols = {int(x["column_count"]): x for x in s.segments}
    assert abs(cycles_per_row(by_cols[4]) - 4) <= 1, "Safe-Lock clock not ~4 cycles/row"
    assert abs(cycles_per_row(by_cols[16]) - 16) <= 1, "operational clock not ~16 cycles/row"
    # The FIRST operational row (the one the commit opens) must already draw the NEW column
    # count. A DLV segment's start_sample is its Column-2 (CDS) anchor, a few UIs after the
    # row's opening edge; if the per-row lookup keys on that instead of the row start, this
    # boundary row is drawn with the old (Safe-Lock) cycle count and its data transitions
    # fall between the recovered clock edges.
    rsp = rv._rec_syncs
    first_op = int(np.searchsorted(rsp, int(by_cols[16]["start_sample"]), side="right") - 1)
    lo, hi = int(rsp[first_op]), int(rsp[first_op + 1])
    xs, _ = rv._rec_clock_xy(lo, hi, 0.0, 1.0)
    starts = (np.asarray(xs) * rate)[0::5] if len(xs) else np.array([])
    n_first = int(((starts >= lo) & (starts < hi)).sum())
    assert n_first == 16, f"first operational row drew {n_first} cycles, not 16 (boundary misclassified)"


def test_phy3_commit_marker_coincides_with_geometry_change():
    """The confirmed-commit marker (Raw view / Timeline) must land on the SAME row where the
    clock and data change to 16 columns — the commit IS the geometry change. The commit's
    effect sample (from effective_row) must resolve to the operational segment's first row's
    Row Sync, i.e. the row that opens the 16-col region, not one row later. Regression: the
    DLV bus-row<->RSP map was off by one (rsp[R+1]), so _sample_at_bus_row(effective_row)
    landed a row past the geometry change and the marker desynced from the clock/data."""
    s = Session.from_demo(600, cold_start=True, phy=3)
    seg16 = next(x for x in s.segments if int(x["column_count"]) == 16)
    rsp = np.asarray(s._recovered_row_syncs(), dtype=np.int64)
    # The row that opens the 16-col region: the RSP at/before the segment's Column-0 anchor.
    geom_change = int(rsp[np.searchsorted(rsp, int(seg16["start_sample"]), side="right") - 1])
    # Where the commit marker is placed (as commit_column_samples snaps it).
    c = next(c for c in s.commands if c.get("is_commit") and c.get("commit_confirmed"))
    eff = int(s._effective_commit_sample(c))
    marker = int(rsp[np.searchsorted(rsp, eff, side="right") - 1])
    assert marker == geom_change, (marker, geom_change)
    # effective_row round-trips to that same opening edge (marker == clock == command row).
    assert s._sample_at_bus_row(int(c["effective_row"])) == geom_change
    assert s.bus_row_for_sample(geom_change) == int(c["effective_row"])


def test_phy3_dlv_timing_polarity_and_per_region_ui():
    """DLV timing details (release review follow-ups): (a) tr_data_rising is the DP wire's
    level AFTER each transition — the ((k+1) odd) parity, not (k odd) which is inverted and
    swapped the eye's rising/falling colours; (b) ui_ns is the MEASURED region's UI (row
    period / its columns), not the global operational UI — so a Safe-Lock region reports its
    own ~4x-wider UI, keeping the 'setup/hold ≈ half a UI' invariant per region."""
    from swi3s_studio.analysis.bus_timing import measure_bus_timing
    s = Session.from_demo(600, cold_start=True, phy=3)
    seg4 = next(x for x in s.segments if int(x["column_count"]) == 4)
    seg16 = next(x for x in s.segments if int(x["column_count"]) == 16)
    t16 = measure_bus_timing(s.capture, segments=s.segments, start_sample=int(seg16["start_sample"]),
                             end_sample=int(s.audio_end_sample), recovered_clock=s.recovered_clock())
    ce = np.asarray(s.capture.clock_edges, dtype=np.int64)
    init = bool(s.capture.initial_clock)
    samp = np.asarray(t16.tr_sample, dtype=np.int64)
    truth = init ^ ((np.searchsorted(ce, samp, side="right") & 1) == 1)   # DP level after edge
    assert bool((np.asarray(t16.tr_data_rising) == truth).all()), "tr_data_rising inverted"
    # Per-region UI: the 4-col region's UI is ~4x the 16-col region's (row rate held constant).
    t4 = measure_bus_timing(s.capture, segments=s.segments, start_sample=int(seg4["start_sample"]),
                            end_sample=int(seg16["start_sample"]), recovered_clock=s.recovered_clock())
    assert abs(t4.ui_ns / t16.ui_ns - 4.0) < 0.2, (t4.ui_ns, t16.ui_ns)
    assert t16.ui_ns * 0.35 < np.median(t4.tr_setup_ns) < t4.ui_ns, np.median(t4.tr_setup_ns)


def test_phy3_recovered_clock_draw_is_bounded():
    """The Raw-view recovered-clock draw must be BOUNDED regardless of how many rows the
    window spans (its per-row work was O(rows) and stalled the GUI thread on a wide zoom-out
    of a long capture; re-run on every pan/zoom). A full-window draw returns at most
    ~5 * _MAX_EDGES points (5 per drawn cycle), decimating whole rows when zoomed out."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.raw_view import RawCaptureView

    s = Session.from_demo(3000, cold_start=True, phy=3)
    rv = RawCaptureView()
    rv.set_capture(s.capture, dlv=True, audio_start=int(s.audio_start_sample),
                   recovered_clock=s.recovered_clock(), segments=s.segments)
    xs, ys = rv._rec_clock_xy(int(s.audio_start_sample), int(s.audio_end_sample), 0.1, 0.9)
    assert len(xs) == len(ys) and len(xs) > 0
    assert len(xs) <= 5 * rv._MAX_EDGES + 5, len(xs)   # bounded — not one cycle per row


def test_phy3_timing_eye_is_balanced_not_zero_setup():
    """DLV setup/hold is measured against the recovered MID-UI sample point, giving a
    balanced ~half-UI eye — not the setup≈0 / hold≈full-UI garbage the forwarded-clock model
    produced (DP and DN of the differential pair toggle at the SAME samples, so treating DP
    as a forwarded clock made every data edge coincide with a 'clock' edge)."""
    from swi3s_studio.analysis.bus_timing import measure_bus_timing
    s = Session.from_demo(600, cold_start=True, phy=3)
    seg16 = next(x for x in s.segments if int(x["column_count"]) == 16)
    lo = int(seg16["start_sample"])
    t = measure_bus_timing(s.capture, segments=s.segments, start_sample=lo,
                           end_sample=int(s.audio_end_sample), recovered_clock=s.recovered_clock())
    assert t is not None and t.n_data_edges > 100
    setup, hold = np.asarray(t.tr_setup_ns), np.asarray(t.tr_hold_ns)
    # Balanced eye ~ half a UI each (NOT setup≈0), and setup + hold ~ one UI.
    assert t.ui_ns * 0.35 < np.median(setup) < t.ui_ns * 0.65, (np.median(setup), t.ui_ns)
    assert t.ui_ns * 0.35 < np.median(hold) < t.ui_ns * 0.65, (np.median(hold), t.ui_ns)
    assert bool((setup > 0).all()), "some setup times are <= 0 (forwarded-clock artefact)"
    # Nothing exceeds ~one UI: the partial boundary rows (sub-divided at the adjacent
    # region's column count) are excluded, so no bogus multi-UI setup/hold stretches the eye.
    assert setup.max() < t.ui_ns * 1.1 and hold.max() < t.ui_ns * 1.1, (setup.max(), hold.max(), t.ui_ns)


def test_phy3_timing_pane_has_points_in_every_region():
    """The Timing pane resolves setup/hold points in BOTH DLV regions (4-col and 16-col),
    and they SURVIVE the column filter. Two DLV-only bugs made it empty: timing_regions used
    the sparse DP `clock_edges` per-UI (ce[start_ui]) for the region bounds — meaningless for
    DLV, giving a zero-width range and no timing; and the per-transition column was -1, so the
    (enabled) column filter's np.isin excluded every point. Exercised through the EyeView the
    UI uses, per region, with the default 'All' filter."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.eye_view import EyeView

    s = Session.from_demo(600, cold_start=True, phy=3)
    regions = s.timing_regions()
    assert {int(r["column_count"]) for r in regions} >= {4, 16}
    for r in regions:                                   # bounds are real samples, not ~0-width
        assert int(r["end_sample"]) - int(r["start_sample"]) > 1000, r["label"]
    ev = EyeView()
    ev.set_analysis_context(s.segments, regions, s.timing_column_roles)
    ev.set_capture(s.capture, recovered_clock=s.recovered_clock())
    for i in range(len(regions)):
        ev._region.setCurrentIndex(i)
        ev._render()                                    # measure (view not visible in a test)
        t = ev._timing
        assert t is not None and t.n_data_edges > 100, regions[i]["label"]
        assert set(np.unique(t.tr_column).tolist()) != {-1}, "column is -1 (filter would drop all)"
        assert int(ev._filter_mask(t).sum()) > 100, f"filter dropped all points in {regions[i]['label']}"


def test_phy3_statistics_rates_make_sense():
    """The Statistics rates are right for DLV: per-region UI = row_period / columns (from the
    recovered clock, NOT the sparse DP-edge gaps), the row rate is CONSTANT across the commit
    (~3.072 MHz), and the recovered clock runs at the full bit/UI rate (4x faster in the
    16-col region than the 4-col), not the FBCSE DDR UI/2."""
    s = Session.from_demo(600, cold_start=True, phy=3)
    st = {int(x["column_count"]): x["ui_ns"] for x in s.section_ui_stats()}
    assert set(st) == {4, 16}
    # UI = 1 / (row_rate * cols): 4-col ~81 ns, 16-col ~20 ns (a clean 4x, not sparse-gap noise).
    assert abs(st[4] - 1e9 / (3_072_000 * 4)) < 4.0, st[4]
    assert abs(st[16] - 1e9 / (3_072_000 * 16)) < 2.0, st[16]
    assert abs(st[4] / st[16] - 4.0) < 0.2, (st[4], st[16])   # bit clock rises 4x at the commit
    # Whole-capture headline rate: operational UI ~49.152 MHz, row ~3.072 MHz.
    assert abs(s.ui_rate_hz - 49_152_000) < 3e5, s.ui_rate_hz
    assert abs(s.row_rate_khz - 3072.0) < 5.0, s.row_rate_khz
