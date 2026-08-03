"""Cross-engine placement guardrail — the two LIVE placement engines must agree.

Analysis mode renders the bus grid from the C++ core (`swi3score.grid_from_csv`);
Visualization-mode authoring renders it from the Python `swviz` engine (via
`model/viz_engine.py::render_payload`). Their §14.2.5 placement is held equal
transitively through the golden suite, but this asserts it DIRECTLY: for each data
port (solo-placed to avoid clash-interaction differences), the two engines put the
same DP-owned cells at the same (row, col, slot).

This is the explicit guard the 3.0.8 review asked for against the duplicated-engine
risk; if one engine's cascade is changed without the other, this fails. The
`test_cross_engine_all_configs_*` sweep over EVERY example config (all flow modes) is
what would have caught the {ASW5203} TX_PRESENT-in-RX_CONTROLLED bug landing in only
one engine — the older demo-only check ran a NORMAL config and never exercised it.

Scope note: the sweep compares DATA + TX_PRESENT, which are DataPort-exclusive slots.
The Flow Control Port's cells (DRQ, and the FCP's own guard/tail) are rendered by
swviz but NOT by the C++ `grid_from_csv` (which lays out data ports only), a known
structural difference — so they're out of scope here and their DataPort-guard/tail
counterparts stay covered by test_visualizer_placement's golden.

Run: PYTHONPATH=. python3 -m pytest tests/test_grid_cross_engine.py
"""
import glob
import os
import tempfile

import swi3score

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig, demo_config
from swi3s_studio.model.grid_slots import GridSlot

# DP-owned kinds both engines emit as bit cells (GridSlot 1–6 == C++ SwI3sSlot 1–6).
# S0/S1/HANDOVER (viz-only) and CDS (is_cds) are system slots handled separately.
_DATA_KINDS = {int(GridSlot.DATA), int(GridSlot.TX_PRESENT), int(GridSlot.GUARD_0),
               int(GridSlot.GUARD_1), int(GridSlot.TAIL), int(GridSlot.DRQ)}
# DataPort-EXCLUSIVE slots — the FCP never emits DATA/TX_PRESENT, so these compare
# cleanly across all flow modes (see the module scope note).
_DP_ONLY_KINDS = {int(GridSlot.DATA), int(GridSlot.TX_PRESENT)}
_TXP = int(GridSlot.TX_PRESENT)
_CDS_COL = 0     # Studio reserves column 0 for the control stream

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")


def _owned(cells, kinds=_DATA_KINDS):
    """DP-owned cells as (dp,row,col,slot), excluding the CDS column."""
    return {(c["dp"], c["row"], c["col"], c["slot"]) for c in cells
            if c["dp"] >= 0 and not c.get("is_cds") and c["slot"] in kinds
            and c["col"] != _CDS_COL}


def _solo_paths(cfg, td):
    """Yield (dp_index, csv_path) for each enabled data port, solo-enabled."""
    for i, dp in enumerate(cfg.dataports):
        if not dp.enabled:
            continue
        solo = BusConfig.from_dict(cfg.to_dict())
        for j, other in enumerate(solo.dataports):
            other.enabled = (j == i)
        yield i, solo.to_csv_file(os.path.join(td, f"solo{i}.csv"))


def test_cross_engine_solo_placement_matches():
    cfg = demo_config()
    rows = max(1, min(int(cfg.rows_to_draw), 64))
    with tempfile.TemporaryDirectory() as d:
        placed = False
        for i, path in _solo_paths(cfg, d):
            placed = True
            cpp = _owned(swi3score.grid_from_csv(path, rows))
            viz = _owned(viz_engine.render_payload(path, rows)[0])
            assert cpp == viz, (f"DP{i} placement differs between C++ and swviz engines: "
                                f"C++-only={sorted(cpp - viz)[:5]} "
                                f"swviz-only={sorted(viz - cpp)[:5]}")
        assert placed, "demo config has no enabled data ports"


def _all_example_csvs():
    return sorted(glob.glob(os.path.join(_EXAMPLES, "**", "*.csv"), recursive=True))


def test_cross_engine_all_configs_data_placement():
    """Every example config, every flow mode: the C++ core and swviz place the same
    DATA + TX_PRESENT cells for each solo data port. This is the sweep that catches a
    placement rule (e.g. {ASW5203} TX_PRESENT) landing in one engine but not the other."""
    csvs = _all_example_csvs()
    assert len(csvs) >= 89, f"expected the vendored example corpus, found {len(csvs)}"
    fails = []
    for csv in csvs:
        cfg = BusConfig.from_csv(csv)
        rows = max(1, min(int(cfg.rows_to_draw), 64))
        with tempfile.TemporaryDirectory() as d:
            for i, path in _solo_paths(cfg, d):
                cpp = _owned(swi3score.grid_from_csv(path, rows), _DP_ONLY_KINDS)
                viz = _owned(viz_engine.render_payload(path, rows)[0], _DP_ONLY_KINDS)
                if cpp != viz:
                    rel = os.path.relpath(csv, _EXAMPLES)
                    fails.append(f"{rel} DP{i}: C++-only={sorted(cpp - viz)[:3]} "
                                 f"swviz-only={sorted(viz - cpp)[:3]}")
    assert not fails, "cross-engine DATA/TX_PRESENT mismatches:\n  " + "\n  ".join(fails)


def test_txpresent_present_iff_flow_controlled():
    """{ASW5203}: TX_PRESENT is placed iff the DP is flow-controlled (TX_CONTROLLED,
    RX_CONTROLLED, or ASYNC) and never in NORMAL — asserted for BOTH engines. This is a
    SPEC-conformance check, not an equivalence one: it catches a bug present identically
    in both engines (as the RX_CONTROLLED omission originally was), which a cross-engine
    equivalence test cannot see."""
    from swi3s_studio.swviz.models.enums import FlowMode
    base = BusConfig.from_csv(
        os.path.join(_EXAMPLES, "directed_tests", "rx_synchronous_flow_control.csv"))
    cases = [(int(FlowMode.NORMAL), False), (int(FlowMode.TX_CONTROLLED), True),
             (int(FlowMode.RX_CONTROLLED), True), (int(FlowMode.ASYNC), True)]
    with tempfile.TemporaryDirectory() as d:
        for fm, expect_txp in cases:
            cfg = BusConfig.from_dict(base.to_dict())
            for dp in cfg.dataports:
                if dp.enabled:
                    dp.flow_mode = fm
            rows = max(1, min(int(cfg.rows_to_draw), 64))
            path = cfg.to_csv_file(os.path.join(d, f"fm{fm}.csv"))
            cpp_txp = any(c["slot"] == _TXP and not c.get("is_cds")
                          for c in swi3score.grid_from_csv(path, rows))
            viz_txp = any(c["slot"] == _TXP and not c.get("is_cds")
                          for c in viz_engine.render_payload(path, rows)[0])
            assert cpp_txp == expect_txp, (
                f"C++ FlowMode={fm}: TX_PRESENT present={cpp_txp}, expected {expect_txp}")
            assert viz_txp == expect_txp, (
                f"swviz FlowMode={fm}: TX_PRESENT present={viz_txp}, expected {expect_txp}")
