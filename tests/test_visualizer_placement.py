"""SWI3S Visualizer placement regression — ported from the visualizer's own test
suite (`mipi-soundwire-I3S-visualizer/test/testsuite.py`).

The visualizer's tests run each example CSV through its placement model and check
the result against a golden output. Studio reuses that placement as the verified
C++ core, so the equivalent Studio test asserts that **Studio's per-data-port
placement matches the visualizer's** for every example config.

Fixtures:
- `visualizer_examples/<category>/<name>.csv` (repo root) — the visualizer's
  example configs (directed tests, spec figures, use cases), the source of inputs.
- `tests/visualizer/golden/<category>/<name>.txt` — the per-data-port placement for
  that config: one `dp row col SLOT` line per owned slot, with a `# rows N` header.
  Authored for Studio (not part of the upstream visualizer suite, which uses the JSON
  goldens); the placements were dumped from the vendored swviz engine, so this test is
  a change-detector for the C++ core against that engine, NOT an independent oracle —
  the spec + reviewed CSVs are the authority when a golden changes.

Comparison: for each config we solo-place every enabled data port through the C++
cascade (`swi3score.grid_from_csv` with only that DP enabled — the same trick the
clash detector uses) and compare the `(dp, row, col, slot)` set to the golden.
Column 0 (the CDS column, which Studio reserves) is excluded: the visualizer's
*raw* model can place a misconfigured DP there, but Studio gives the CDS column to
the control stream (the visualizer's engine flags it as a clash too).

Run: PYTHONPATH=. python3 -m pytest tests/test_visualizer_placement.py
"""
import glob
import os
import tempfile

import swi3score

from swi3s_studio.model.bus_config import BusConfig

_HERE = os.path.dirname(os.path.abspath(__file__))
_VIZ = os.path.join(_HERE, "visualizer")
# Example configs are the repo-root visualizer_examples/ folder (copied verbatim
# from the original visualizer); the golden placements stay vendored under
# tests/visualizer/golden.
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")

# Studio SLOT_NAMES -> the visualizer's slot strings.
_S2V = {"Data": "DATA", "TxPresent": "TX_PRESENT", "Guard0": "GUARD_0",
        "Guard1": "GUARD_1", "Tail": "TAIL", "Drq": "DRQ"}
_SLOT = list(swi3score.SLOT_NAMES)
_CDS_COL = 0


def _read_golden(path):
    rows = 0
    slots = set()
    with open(path) as f:
        for line in f:
            if line.startswith("# rows"):
                rows = int(line.split()[2])
                continue
            p = line.split()
            if len(p) == 4 and int(p[2]) != _CDS_COL:
                slots.add((int(p[0]), int(p[1]), int(p[2]), p[3]))
    return rows, slots


def _studio_placement(cfg: BusConfig, rows: int):
    """Per-DP placement: solo-place each enabled data port through the C++
    cascade and collect its (dp, row, col, slot) slots (excluding the CDS column)."""
    out = set()
    with tempfile.TemporaryDirectory() as td:
        for i, dp in enumerate(cfg.dataports):
            if not dp.enabled:
                continue
            solo = BusConfig.from_dict(cfg.to_dict())
            for j, other in enumerate(solo.dataports):
                if j != i:
                    other.enabled = False
            path = solo.to_csv_file(os.path.join(td, f"solo{i}.csv"))
            for c in swi3score.grid_from_csv(path, rows):
                if c["is_cds"] or c["dp"] < 0 or c["col"] == _CDS_COL:
                    continue
                name = _SLOT[c["slot"]]
                if name in _S2V:
                    out.add((i, c["row"], c["col"], _S2V[name]))
    return out


def _cases():
    for gold in sorted(glob.glob(os.path.join(_VIZ, "golden", "**", "*.txt"), recursive=True)):
        base = os.path.relpath(gold, os.path.join(_VIZ, "golden"))[:-4]
        csv = os.path.join(_EXAMPLES, base + ".csv")
        if os.path.exists(csv):
            yield base, csv, gold


def test_visualizer_examples_present():
    cases = list(_cases())
    # The visualizer ships 58 directed + 30 spec-figure + 1 use-case configs.
    assert len(cases) == 90, f"expected 90 vendored configs, found {len(cases)}"


def test_placement_matches_visualizer():
    fails = []
    n = 0
    for base, csv, gold in _cases():
        rows, want = _read_golden(gold)
        cfg = BusConfig.from_csv(csv)
        got = _studio_placement(cfg, rows)
        if got != want:
            fails.append(f"{base}: missing={len(want - got)} extra={len(got - want)}")
        n += 1
    assert n == 90, n
    assert not fails, "placement mismatches:\n  " + "\n  ".join(fails)
