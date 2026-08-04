"""Demo bus-config expectations: the handover-friendly PHY2 layout, the PHY3 operational
row rate, and cursor-aware config export.

These lock in three fixes:
- The Bus Grid and "Export Visualizer CSV" disagreed for a reconfiguring capture
  (grid showed the cursor's 8-col segment, export always wrote the final 16-col config).
  Export is now as-of the cursor (`config_dataports_at`).
- The PHY2 demo now keeps the column right after the CDS (col 1) and the last column
  (col 7 at 8-col) FREE for the CDS / S1 handovers, matching real configs — the two PCM
  ports time-interleave in col 2 at 8-col to fit.
- The PHY3 demo holds its 16-col operational row rate at 3.072 MRows/s (the recovered-
  clock PLL's stable reference), using one capture sample rate throughout.
"""

from swi3s_studio.session import Session


def _seg(s, cols):
    """The demo segment with `cols` columns."""
    return next(x for x in s.segments if int(x["column_count"]) == cols)


def _mid(s, seg):
    """A sample well inside `seg` (midway to the next segment / capture end)."""
    starts = sorted(int(x["start_sample"]) for x in s.segments)
    start = int(seg["start_sample"])
    later = [x for x in starts if x > start]
    end = later[0] if later else int(s.audio_end_sample)
    return (start + end) // 2


def _cols_used(cells):
    """Set of grid columns occupied by a data port (dp >= 0), and the CDS column."""
    data = {int(c["col"]) for c in cells if not c.get("is_cds") and int(c.get("dp", -1)) >= 0}
    cds = {int(c["col"]) for c in cells if c.get("is_cds")}
    return data, cds


def test_phy2_8col_keeps_handover_columns_free():
    """PHY2 8-col: CDS at col 0, data in cols 2-6, with col 1 (after CDS) and col 7 (row
    end) FREE for handovers. The two PCM ports share col 2 (time-interleaved) to fit."""
    s = Session.from_demo(400, cold_start=True, phy=2)
    seg8 = _seg(s, 8)
    cells = s.grid_cells_at(_mid(s, seg8), 64)          # enough rows to catch the interleave
    data, cds = _cols_used(cells)
    assert cds == {0}
    assert 1 not in data and 7 not in data, sorted(data)   # handover columns free
    assert data == {2, 3, 4, 5, 6}, sorted(data)
    # DP0 and DP1 both land in col 2 (interleaved).
    dp_at_2 = {int(c["dp"]) for c in cells if int(c.get("col", -1)) == 2 and int(c.get("dp", -1)) >= 0}
    assert dp_at_2 == {0, 1}, dp_at_2


def test_phy2_16col_layout_frees_handover_column():
    """PHY2 16-col: CDS col 0, DP0-3 at cols 2,3,4/5,6/7 — col 1 free for the handover."""
    s = Session.from_demo(400, cold_start=True, phy=2)
    cells = s.grid_cells_at(_mid(s, _seg(s, 16)), 64)
    data, cds = _cols_used(cells)
    assert cds == {0}
    assert 1 not in data, sorted(data)
    assert data == {2, 3, 4, 5, 6, 7}, sorted(data)


def test_phy2_export_config_follows_cursor_segment():
    """Export Visualizer CSV exports the config as-of the cursor: the 8-col segment
    exports an 8-col config, the 16-col segment a 16-col config — matching the grid."""
    s = Session.from_demo(400, cold_start=True, phy=2)
    c8 = s.config_dataports_at(_mid(s, _seg(s, 8)))
    c16 = s.config_dataports_at(_mid(s, _seg(s, 16)))
    assert c8["num_columns"] + 1 == 8
    assert c16["num_columns"] + 1 == 16
    # Row rate follows the segment (constant UI rate / columns): 3.072 vs 1.536 MRows/s.
    assert abs(c8["row_rate_khz"] - 3072.0) < 1.0, c8["row_rate_khz"]
    assert abs(c16["row_rate_khz"] - 1536.0) < 1.0, c16["row_rate_khz"]
    # 8-col interleave (reference 8col_bus_config): PCM ports share col 2, Offset 0/16.
    hs8 = [dp["horizontal_start"] for dp in c8["dataports"][:4]]
    off8 = [dp["offset"] for dp in c8["dataports"][:4]]
    assert hs8 == [2, 2, 3, 5] and off8 == [0, 16, 0, 0], (hs8, off8)
    # 16-col (reference 16col_bus_config): one column per PCM port.
    hs16 = [dp["horizontal_start"] for dp in c16["dataports"][:4]]
    assert hs16 == [2, 3, 4, 6], hs16


def test_phy3_operational_row_rate_is_3072():
    """PHY3 holds its 16-col operational row rate at 3.072 MRows/s (UI 49.152 MHz), with
    one capture sample rate for the whole capture and 48 kHz / 3.072 MHz audio intact."""
    s = Session.from_demo(400, cold_start=True, phy=3)
    assert s.column_count == 16
    assert abs(s.row_rate_khz - 3072.0) < 1.0, s.row_rate_khz
    assert abs(s.ui_rate_hz - 49_152_000) < 1e5, s.ui_rate_hz
    rates = s.decoder.audio_sample_rates()
    assert abs(rates[(0, 0)] - 48_000) < 1.0                # PCM 48 kHz
    assert abs(rates[(0, 2)] - 3_072_000) < 100.0           # PDM 3.072 MHz


def test_open_grid_in_visualizer_menu():
    """Analyzer ▸ View Bus Grid in Visualizer loads the decoded config (as-of the cursor)
    straight into the authoring panel and switches to Visualization — no Save/Open CSV
    round trip."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import ANALYSIS, VISUALIZATION, MainWindow

    win = MainWindow()
    win.load_demo(phy=2)
    win._mode_mgr.switch_to(ANALYSIS)
    assert win._mode_mgr.current() == ANALYSIS
    # Put the cursor in the operational (16-col) audio region so a full config is in effect.
    seg16 = next(x for x in win._session.segments if int(x["column_count"]) == 16)
    win.cursor.set_sample(int(seg16["start_sample"]) + 10_000)
    win.open_grid_in_visualizer()
    assert win._mode_mgr.current() == VISUALIZATION
    cfg = win._authoring.config()
    assert cfg.num_columns + 1 == 16
    assert sum(1 for d in cfg.dataports if d.enabled) == 4      # the demo's four ports
