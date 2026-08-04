"""Config-vs-decoded comparison test.

Run: python3 -m pytest tests/test_compare.py
"""
import glob
import os

import swi3score

from swi3s_studio.analysis import grid_diff, grid_diff_report, register_diff
from swi3s_studio.ingest import transitions

_VIZ = os.path.join(os.path.dirname(__file__), "..", "..",
                    "mipi-soundwire-I3S-visualizer", "examples", "directed_tests")


def _cell(row, col, slot, ch, cds=False):
    return {"row": row, "col": col, "slot": slot, "channel": ch, "dp": 0, "is_cds": cds}


def test_grid_diff_classes():
    decoded = [
        _cell(0, 0, 0, -1, cds=True),
        _cell(0, 1, 1, 0),            # same
        _cell(0, 2, 1, 1),            # changed (channel differs)
        _cell(0, 4, 1, 0),            # decoded_only
    ]
    expected = [
        _cell(0, 0, 0, -1, cds=True),
        _cell(0, 1, 1, 0),
        _cell(0, 2, 1, 0),            # vs decoded ch1 -> changed
        _cell(0, 3, 1, 0),            # expected_only
    ]
    cells, n_diff = grid_diff(decoded, expected)
    by = {(c["row"], c["col"]): c["diff"] for c in cells}
    assert by[(0, 0)] == "same"
    assert by[(0, 1)] == "same"
    assert by[(0, 2)] == "changed"
    assert by[(0, 3)] == "expected_only"
    assert by[(0, 4)] == "decoded_only"
    assert n_diff == 3


def test_identical_grids_have_no_diff():
    decoded = swi3score.Decoder(transitions.demo_capture(8).sample_source(),
                                swi3score.DecoderSettings())
    decoded.run()
    g = decoded.grid_cells(16)
    cells, n_diff = grid_diff(g, g)             # compare with itself
    assert n_diff == 0
    assert all(c["diff"] == "same" for c in cells)


def test_grid_diff_report_text():
    decoded = [_cell(0, 1, 1, 0), _cell(0, 2, 1, 1), _cell(0, 4, 1, 0)]
    expected = [_cell(0, 1, 1, 0), _cell(0, 2, 1, 0), _cell(0, 3, 1, 0)]
    cells, _ = grid_diff(decoded, expected)
    summary, detail = grid_diff_report(cells)
    assert "3 differing grid cell(s)" in summary
    assert "1 changed" in summary and "1 only-on-bus" in summary and "1 only-in-config" in summary
    assert "→" in detail                         # changed cell shows bus -> config
    # An all-same grid reports a clean match with no detail.
    same_cells, _ = grid_diff([_cell(0, 1, 1, 0)], [_cell(0, 1, 1, 0)])
    s2, d2 = grid_diff_report(same_cells)
    assert "matches" in s2 and d2 == ""


def test_registers_from_csv_roundtrip():
    """The expected-config register encoder is the exact inverse of the decode:
    feeding its writes through grid_from_registers reproduces grid_from_csv."""
    import tempfile

    from swi3s_studio.ingest import visualizer_csv as vc
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "old.csv")
        with open(old, "w") as f:
            f.write(_OLD_CSV)
        cfg = vc.normalized_path(old)
        writes = swi3score.registers_from_csv(cfg)
        assert writes and all(len(t) == 3 for t in writes)
        g_csv = swi3score.grid_from_csv(cfg, 32)
        g_reg = swi3score.grid_from_registers(writes, 8, 32)
        key = lambda c: (c["row"], c["col"], c["dp"], c["channel"], c["slot"])
        a = sorted(key(c) for c in g_csv if not c["is_cds"])
        b = sorted(key(c) for c in g_reg if not c["is_cds"])
        assert a == b and len(a) > 0


def test_grid_from_commands_next_staging():
    """grid_from_commands must honour dual-ranked _NEXT staging: a _NEXT dataport
    write changes the placed grid ONLY after a CONFIRMED commit of its group — not
    the instant the write is seen, and never on an unconfirmed commit. This is the
    fix for the as-of-cursor grid updating on _NEXT writes before any commit."""
    import tempfile

    from swi3s_studio.ingest import visualizer_csv as vc
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "old.csv")
        with open(old, "w") as f:
            f.write(_OLD_CSV)
        writes = swi3score.registers_from_csv(vc.normalized_path(old))
    ndata = lambda cells: sum(1 for c in cells if not c.get("is_cds"))
    wcmds = [{"is_write": True, "device_mask": 1 << dev, "address": addr,
              "data": bytes([val & 0xFF])} for dev, addr, val in writes]
    # _NEXT writes alone: staged, grid has no data cells yet.
    assert ndata(swi3score.grid_from_commands(wcmds, 32)) == 0
    # An UNCONFIRMED commit does not promote.
    unconf = wcmds + [{"is_commit": True, "commit_confirmed": False, "group_mask": 0xFF}]
    assert ndata(swi3score.grid_from_commands(unconf, 32)) == 0
    # A CONFIRMED commit promotes _NEXT -> _CURR; the data ports now place.
    conf = wcmds + [{"is_commit": True, "commit_confirmed": True, "group_mask": 0xFF}]
    assert ndata(swi3score.grid_from_commands(conf, 32)) > 0


def test_register_diff_finds_mismatch():
    from swi3s_studio.model import RegisterMap
    rmap = RegisterMap.load()
    baseline = [(0, 0x2009, 0x17)]                          # effective config baseline
    expected = [(0, 0x2009, 0x17), (0, 0x200B, 0x00)]       # 0x200B absent -> reset 0x08
    diffs = register_diff(expected, baseline, rmap)
    addrs = {d["address"] for d in diffs}
    assert 0x2009 not in addrs                              # matches baseline -> not a diff
    assert 0x200B in addrs                                  # baseline absent, reset 0x08 != 0x00
    d = next(x for x in diffs if x["address"] == 0x200B)
    assert d["expected"] == 0x00 and d["decoded"] == 0x08


def test_grid_from_csv_optional():
    csvs = sorted(glob.glob(os.path.join(_VIZ, "*.csv")))
    if not csvs:
        print("   (skipped grid_from_csv: visualizer examples not found)")
        return
    cells = swi3score.grid_from_csv(csvs[0], 16)
    assert isinstance(cells, list)
    print(f"   grid_from_csv({os.path.basename(csvs[0])}) -> {len(cells)} cells")


# Old human-readable visualizer export (v1.73). Two enabled DPs at HStart 5 / 2.
_OLD_CSV = """\
Save file using excess one,True
Columns per Row (2-32),8
Skipping Denominator (1-4096),16
Row Rate [kHz] (1-61440),768
Rows to Draw (1-10240),32
Data Port Name,Spkr-O,SpkrIV
Data Port Device Number,0,0
Data Port Channels,0,1
Data Port Sample Width,23,15
Data Port Interval Integer,15,15
Data Port Offset,0,0
Data Port Horizontal Start,5,2
Data Port Horizontal Count,1,1
Source,True,False
Data Port Enabled,True,True
"""


def test_visualizer_csv_legacy_detect_and_convert():
    from swi3s_studio.ingest import visualizer_csv as vc
    assert vc.detect_version(_OLD_CSV) == "legacy"
    cur = vc.to_current_text(_OLD_CSV)
    assert vc.detect_version(cur) == "current"               # upgraded to v3.0
    fields = {r.split(",")[0]: r.split(",")[1:] for r in cur.splitlines() if r}
    assert fields["NumColumns_REG"] == ["7"]                 # 8 columns -> reg 7
    assert float(fields["RowRate"][0]) == 768.0
    assert fields["HorizontalStart_REG"][:2] == ["5", "2"]
    assert fields["Enabled"][:2] == ["True", "True"]
    # Data Port Channels (excess-one count) -> EnableCh_REG contiguous bitmask.
    assert fields["EnableCh_REG"][:2] == ["0b1", "0b11"]     # 1 ch, 2 ch
    assert fields["SampleSize_REG"][:2] == ["23", "15"]
    # The v3.0 addition (per-DP logical number) is now present, defaulting to the
    # column index (legacy had no DP-number concept).
    assert fields["DataPortNumber"][:2] == ["0", "1"]
    # current text passes through unchanged (idempotent).
    assert vc.to_current_text(cur) == cur


def test_visualizer_csv_v2_upgrades_to_current():
    """A v2.0 register CSV (no DataPortNumber row) is upgraded to current: the
    numbers are added (defaulting to the column index) and the geometry survives."""
    from swi3s_studio.ingest import visualizer_csv as vc
    cur = vc.to_current_text(_OLD_CSV)                        # legacy -> current
    # Strip the DataPortNumber row to synthesise a v2.0 file.
    v2 = "\n".join(l for l in cur.splitlines() if not l.startswith("DataPortNumber")) + "\n"
    assert vc.detect_version(v2) == "register_old"
    upgraded = vc.to_current_text(v2)
    assert vc.detect_version(upgraded) == "current"
    f = {r.split(",")[0]: r.split(",")[1:] for r in upgraded.splitlines() if r}
    assert f["DataPortNumber"][:2] == ["0", "1"]             # numbers re-added
    assert f["HorizontalStart_REG"][:2] == ["5", "2"]        # geometry preserved



def test_visualizer_csv_legacy_grid_placement(tmp=None):
    """The upgraded legacy CSV places dataports at the expected columns."""
    import tempfile

    from swi3s_studio.ingest import visualizer_csv as vc
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "old.csv")
        with open(old, "w") as f:
            f.write(_OLD_CSV)
        curpath = vc.normalized_path(old)
        assert curpath != old                                 # converted
        grid = swi3score.grid_from_csv(curpath, 8)
        cols = {}
        for c in grid:
            if not c.get("is_cds") and c.get("dp", -1) >= 0:
                cols.setdefault(c["dp"], set()).add(c["col"])
        placed = {frozenset(v) for v in cols.values()}
        # HStart 5 -> cols {5,6}; HStart 2 -> cols {2,3} (HCount excess-one = 2 wide).
        assert frozenset({5, 6}) in placed and frozenset({2, 3}) in placed
    # An already-current file is returned unchanged by normalized_path.
    with tempfile.TemporaryDirectory() as d:
        cur = os.path.join(d, "cur.csv")
        with open(cur, "w") as f:
            f.write(vc.to_current_text(_OLD_CSV))
        assert vc.normalized_path(cur) == cur


def test_grid_from_registers_force_columns():
    """force_columns overrides the register-derived column width — the lever the
    cursor-driven grid uses to draw an earlier cold-start segment at its physical
    width even though the (final) registers say more columns. The _OLD_CSV config
    places data ports at columns 2,3,5,6; forcing the grid to 2 columns clamps the
    layout so only columns 0..1 exist (the off-grid data ports drop out)."""
    import tempfile

    from swi3s_studio.ingest import visualizer_csv as vc
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "old.csv")
        with open(old, "w") as f:
            f.write(_OLD_CSV)                       # an 8-column config
        writes = swi3score.registers_from_csv(vc.normalized_path(old))
    g_auto = swi3score.grid_from_registers(writes, 8, 16)
    g2 = swi3score.grid_from_registers(writes, 8, 16, force_columns=2)
    cols = lambda cells: set(c["col"] for c in cells)
    assert max(cols(g_auto)) == 6                   # data ports reach col 6
    assert cols(g2) == {0}                          # forced to 2 cols: only CDS fits
    assert len(g2) < len(g_auto)                    # forced grid is strictly smaller
    assert all(c["col"] <= 1 for c in g2)
