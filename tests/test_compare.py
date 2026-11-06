"""Config-vs-decoded comparison: the expected-config register encoder and its inverse
(`registers_from_csv` / `grid_from_registers`), `_NEXT` staging in `grid_from_commands`, the
register diff and its report text (`register_diff`, `register_diff_report`), the grid placed
from a directed-test CSV, and the legacy and v2.0 config CSV upgrade to the current format.

Run: python3 -m pytest tests/test_compare.py
"""
import os

import swi3score

from swi3s_studio.analysis import register_diff
from swi3s_studio.analysis.compare import register_diff_report

_VIZ = os.path.join(os.path.dirname(__file__), "..", "visualizer_examples", "directed_tests")


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
    the instant the write is seen, and never on an unconfirmed commit."""
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
    devices = 0
    for dev, _a, _v in writes:
        devices |= 1 << dev
    unconf = wcmds + [{"is_commit": True, "commit_confirmed": False, "group_mask": 0xFF,
                       "device_mask": devices}]
    assert ndata(swi3score.grid_from_commands(unconf, 32)) == 0
    # A CONFIRMED commit promotes _NEXT -> _CURR; the data ports now place.
    conf = wcmds + [{"is_commit": True, "commit_confirmed": True, "group_mask": 0xFF,
                     "device_mask": devices}]
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


def _diffs(*rows):
    return [{"device": d, "address": a, "label": f"R{a:X}", "decoded": dec, "expected": exp}
            for d, a, dec, exp in rows]


def test_register_diff_report_says_a_match_and_groups_a_mismatch_by_device():
    summary, detail = register_diff_report([], 7)
    assert summary == "Config matches the decoded registers — 0 of 7 register(s) differ."
    assert detail == ""
    summary, detail = register_diff_report(
        _diffs((1, 0x2009, 0x17, 0x18), (0, 0x200B, 0x08, 0x00), (0, 0x2001, 0x01, 0x02)), 9)
    assert summary == ("3 of 9 register(s) differ between the expected config and the "
                       "decoded bus.")
    assert detail.splitlines() == [
        "Device 0:",
        "  0x2001 R2001: decoded 0x01  →  expected 0x02",
        "  0x200B R200B: decoded 0x08  →  expected 0x00",
        "",
        "Device 1:",
        "  0x2009 R2009: decoded 0x17  →  expected 0x18"]


def test_register_diff_report_counts_only_the_registers_past_the_cap():
    four = _diffs(*[(0, 0x2000 + i, 0, 1) for i in range(4)])
    # The header and three registers fill four lines: one register is left over.
    _s, detail = register_diff_report(four, 4, max_lines=4)
    assert detail.splitlines()[-1] == "  … (+1 more)"
    assert sum(1 for ln in detail.splitlines() if ln.startswith("  0x")) == 3
    _s, detail = register_diff_report(four, 4, max_lines=3)
    assert detail.splitlines()[-1] == "  … (+2 more)"
    # The last register landing exactly on the cap leaves nothing to count.
    _s, detail = register_diff_report(four, 4, max_lines=5)
    assert "more" not in detail and detail.count("  0x") == 4
    _s, detail = register_diff_report(four[:2], 2, max_lines=3)
    assert "more" not in detail and detail.count("  0x") == 2
    # Across devices the cap counts every line, headers included.
    two_dev = _diffs((0, 0x2000, 0, 1), (1, 0x2000, 0, 1))
    _s, detail = register_diff_report(two_dev, 2, max_lines=3)
    assert detail.splitlines() == ["Device 0:", "  0x2000 R2000: decoded 0x00  →  expected 0x01",
                                   "  … (+1 more)"]


def test_register_diff_report_never_ends_on_a_device_header_with_nothing_under_it():
    three_dev = _diffs(*[(d, 0x2000 + i, 0, 1) for d in range(3) for i in range(2)])
    for cap in range(1, 12):
        _s, detail = register_diff_report(three_dev, 6, max_lines=cap)
        lines = detail.splitlines()
        listing = [ln for ln in lines if "more)" not in ln]
        assert len(listing) <= cap, (cap, lines)
        for i, ln in enumerate(lines):
            if ln.startswith("Device "):
                assert lines[i + 1].startswith("  0x"), (cap, lines)
        shown = sum(1 for ln in lines if ln.startswith("  0x"))
        left = 6 - shown
        assert (f"  … (+{left} more)" in lines) == (left > 0)
    _s, detail = register_diff_report(three_dev, 6, max_lines=5)
    assert detail.splitlines() == [
        "Device 0:", "  0x2000 R2000: decoded 0x00  →  expected 0x01",
        "  0x2001 R2001: decoded 0x00  →  expected 0x01", "  … (+4 more)"]


def test_grid_from_csv_places_data_ports_for_a_directed_example():
    """The bundled directed-test CSV exists and places at least one non-CDS cell."""
    path = os.path.join(_VIZ, "Contrived_PDM_sample_grouping_with_channel_grouping.csv")
    assert os.path.isfile(path), path
    cells = swi3score.grid_from_csv(path, 16)
    assert isinstance(cells, list) and sum(1 for c in cells if not c.get("is_cds")) > 0


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
    # The per-DP logical number is present, defaulting to the column index (legacy had
    # no DP-number concept).
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
