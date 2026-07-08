"""Bus-Visualizer engine adapter.

Bus-Visualizer mode is driven by the **SWI3S Visualizer engine** — the first-party,
non-UI placement / clash / validation engine in `swi3s_studio.swviz` — rather than a
re-implementation, so placement, clash detection, and validation match the historical
Visualizer bit-for-bit. This module is the single seam: it builds a `BusModel` from a
v2.0 CSV exactly the way the engine's headless batch path does (`num_rows =
VizConfig.rows_to_draw`; `BusModelBuilder(interface, num_rows, viz).build()`).

The Qt renderer + notifications read the returned `BusModel.bits` and `.warnings`;
the test suite compares the serialized model to the golden JSONs.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

# Quiet the engine's console logging (clash / CSV-field chatter); all its loggers are
# children of 'swi3s_visualizer'.
logging.getLogger("swi3s_visualizer").setLevel(logging.ERROR)


def build_bus_model(csv_path: str, num_rows: int | None = None):
    """Load a v2.0 CSV through the Visualizer's CSV handler and build the merged
    BusModel. Returns (bus_model, interface, viz_config). `num_rows` defaults to the
    config's RowsToDraw, matching the engine's headless build."""
    from ..swviz.models.interface import Interface
    from ..swviz.io.csv_handler import CSVHandler
    from ..swviz.viz import VizConfig
    from ..swviz.core.engine import BusModelBuilder

    iface = Interface()
    viz = VizConfig()
    CSVHandler.load_csv(csv_path, iface, viz)
    rows = int(viz.rows_to_draw if num_rows is None else num_rows)
    bus_model = BusModelBuilder(iface, rows, viz).build()
    return bus_model, iface, viz


def model_json(csv_path: str, num_rows: int | None = None) -> dict:
    """Serialize the built BusModel the same way the Visualizer's batch path does
    (`JSONHandler.save_bus_model`) and return the parsed dict — for golden tests."""
    from ..swviz.io.json_handler import JSONHandler

    bus_model, _iface, _viz = build_bus_model(csv_path, num_rows)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        JSONHandler.save_bus_model(tmp, bus_model, batch_mode=True)
        with open(tmp, encoding="utf-8") as f:
            return json.load(f)
    finally:
        os.remove(tmp)


# Visualizer SlotType (enums.py) -> the GridView's slot ints / kinds.
#   DATA 0, GUARD_0 1, TAIL 2, HANDOVER 3, CDS 4, S0 5, S1 6, GUARD_1 7,
#   CLASH 8, TX_PRESENT 9, DRQ 10
_SLOT_TO_GRID = {0: 1, 1: 3, 2: 5, 3: 9, 5: 7, 6: 8, 7: 4, 9: 2, 10: 6}  # CDS(4) -> is_cds


def render_payload(csv_path: str, num_rows: int | None = None):
    """Build the merged BusModel and adapt it for the Qt grid + notifications.
    Returns (cells, clashes, issues, num_columns, num_rows):
      - cells: GridView cell dicts (row/col/dp/device/channel/sample/bit/is_source/
        is_cds/slot/scrambler/port_mode/display_fields) for every placed bit;
      - clashes: {(row, col): 'bus'|'device'|'read'} from the model's clash lists;
      - issues: Studio Issues mirroring the Visualizer's notifications.
    """
    bm, _iface, _viz = build_bus_model(csv_path, num_rows)
    ncols = bm.num_columns
    cells = []
    for b in bm.bits:
        sv = b.slot.value
        is_cds = (sv == 4)
        df = b.display_fields
        df_int = df.value if hasattr(df, "value") else (6 if df is None else int(df))
        cells.append({
            "row": b.row, "col": b.column,
            "dp": -1 if b.dp is None else b.dp,
            "device": b.device,
            "channel": -1 if b.channel is None else b.channel,
            "sample": 0 if b.sample is None else b.sample,
            "bit": 0 if b.bit is None else b.bit,
            "is_source": (b.direction.value == 0),
            "is_cds": is_cds,
            "slot": 0 if is_cds else _SLOT_TO_GRID.get(sv, 0),
            "scrambler": bool(b.scrambler_enabled),
            "port_mode": int(b.port_mode),
            "display_fields": df_int,
        })
    clashes = {}
    for idx in bm.read_overlaps:
        clashes[(idx // ncols, idx % ncols)] = "read"
    for idx in bm.device_clashes:
        clashes[(idx // ncols, idx % ncols)] = "device"
    for idx in bm.bus_clashes:
        clashes[(idx // ncols, idx % ncols)] = "bus"   # bus clash wins the colour
    return cells, clashes, issues_from_model(bm), ncols, bm.num_rows


def issues_from_model(bm):
    """Studio Issues mirroring the Visualizer's ErrorPanel categories/severities."""
    from ..analysis.issues import Issue, ERROR, WARNING, INFO, sort_issues
    ncols = bm.num_columns

    def rc(idx):
        return (idx // ncols, idx % ncols)

    out = []
    if bm.bus_clashes:
        out.append(Issue(ERROR, "Bus Clash",
                         f"physical bus collision on {len(bm.bus_clashes)} slot(s) "
                         f"(different devices drive the same slot)",
                         [rc(i) for i in bm.bus_clashes]))
    if bm.device_clashes:
        out.append(Issue(WARNING, "Device Clash",
                         f"same device drives {len(bm.device_clashes)} slot(s) from "
                         f"two data ports", [rc(i) for i in bm.device_clashes]))
    if bm.read_overlaps:
        out.append(Issue(INFO, "Read Overlap",
                         f"two sinks read the same {len(bm.read_overlaps)} slot(s)",
                         [rc(i) for i in bm.read_overlaps]))
    w = bm.warnings
    for name, result in w.validation_issues:
        for err in result.errors:
            out.append(Issue(ERROR, name, err.message))
    for dp_name, need, avail in w.interval_overflow_warnings:
        out.append(Issue(ERROR, dp_name,
                         f"interval overflow: needs {need} bits, only {avail} fit"))
    if w.scrambler_mismatches:
        out.append(Issue(WARNING, "Scrambler",
                         f"source/sink ScramblerEn differ on "
                         f"{len(w.scrambler_mismatches)} slot(s)",
                         [rc(i) for i, _s, _k in w.scrambler_mismatches]))
    if w.test_mode_mismatches:
        out.append(Issue(WARNING, "Test Mode",
                         f"port test-mode differs on {len(w.test_mode_mismatches)} slot(s)",
                         [rc(m[0]) for m in w.test_mode_mismatches]))
    if w.sample_bit_mismatches:
        out.append(Issue(WARNING, "Sample/Bit Mismatch",
                         f"source/sink sample or bit differ on "
                         f"{len(w.sample_bit_mismatches)} slot(s)",
                         [rc(m[0]) for m in w.sample_bit_mismatches]))
    for label, items in (("TxP source without sink", w.txp_mismatches),
                         ("TxP sink without source", w.txp_orphan_sinks),
                         ("DRQ source without sink", w.drq_mismatches),
                         ("DRQ sink without source", w.drq_orphan_sinks)):
        if items:
            out.append(Issue(WARNING, "Flow Control",
                             f"{label}: {len(items)} slot(s)", [rc(i) for i in items]))
    for dp_name, _n in w.sink_handover_warnings:
        out.append(Issue(INFO, dp_name, "sink data ports don't need handovers"))
    for dp_name, _n in w.enabled_no_channels_warnings:
        out.append(Issue(INFO, dp_name, "enabled but no channel selected"))
    for dp_name, interval, shown in w.display_truncation_warnings:
        out.append(Issue(INFO, dp_name,
                         f"interval is {interval} rows, only {shown} displayed"))
    return sort_issues(out)

