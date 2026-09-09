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
    from ..swviz.core.engine import BusModelBuilder
    from ..swviz.io.csv_handler import CSVHandler
    from ..swviz.models.interface import Interface
    from ..swviz.viz import VizConfig

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


# Visualizer authoring SlotType (swviz.models.enums) -> the shared GridSlot ints the
# GridView renders. Built from named members on both sides (not magic numbers) so the
# mapping is self-documenting and can't silently skew if either vocabulary changes.
# CDS is handled separately (is_cds); EMPTY/CLASH are never emitted as data cells.
def _build_slot_to_grid():
    from ..swviz.models.enums import SlotType
    from .grid_slots import GridSlot
    pairs = [
        (SlotType.DATA, GridSlot.DATA),
        (SlotType.GUARD_0, GridSlot.GUARD_0),
        (SlotType.TAIL, GridSlot.TAIL),
        (SlotType.HANDOVER, GridSlot.HANDOVER),
        (SlotType.S0, GridSlot.S0),
        (SlotType.S1, GridSlot.S1),
        (SlotType.GUARD_1, GridSlot.GUARD_1),
        (SlotType.TX_PRESENT, GridSlot.TX_PRESENT),
        (SlotType.DRQ, GridSlot.DRQ),
    ]
    return {st.value: int(gs) for st, gs in pairs}


def _cds_slot_value() -> int:
    from ..swviz.models.enums import SlotType
    return SlotType.CDS.value


_SLOT_TO_GRID = _build_slot_to_grid()   # CDS(SlotType.CDS) -> is_cds, handled below
_CDS_SLOT_VALUE = _cds_slot_value()


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
    # The CDS is per-source (Manager + 12 devices). When they DIVERGE (any source's
    # guard/tail differs), the grid splits the CDS cells into Manager/Peripheral
    # bands; when uniform they collapse to one full-height symbol. A divergent config
    # where only the Manager drives a guard emits a single Manager cell (devices are
    # off/bare), indistinguishable from the uniform case by cells alone — so pass the
    # divergence explicitly for the grid to honour (see GridView._draw_row).
    cds_split = (len(set(_iface.CDS_Guard_PerSource)) > 1
                 or len(set(_iface.CDS_Tail_PerSource)) > 1)

    # A cell's `dp` is the LOGICAL DataPortNumber, not swviz's slot index. The two diverge
    # whenever a config numbers a port differently from its position in the file — four
    # peripherals each using their own DP0, for instance — and the C++ core already reports
    # the logical number (Decoder.cpp: `dpNumber >= 0 ? dpNumber : slot index`). Without this
    # map the same key meant two different things depending on which engine produced the
    # cells, so the grid labelled device 1's DP0 as "DP5" and the cross-engine test compared
    # a number against an index.
    #
    # Mapped HERE, at the single boundary both engines' cells pass through, because the index
    # is a real index inside swviz: _detect_truncated_drq groups bits by it and looks them up
    # by enumerate() position, so redefining it in the engine would silently stop that
    # detection for precisely the configs this fixes. Every example config in the corpus uses
    # the identity mapping, which is why 89 of them never caught this.
    _logical_dp = [dpv.dp_number if getattr(dpv, "dp_number", -1) >= 0 else i
                   for i, dpv in enumerate(_viz.data_ports)]

    def _dp_of(slot_index):
        if slot_index is None or slot_index < 0:
            return -1
        if slot_index >= len(_logical_dp):      # not expected; report rather than mislabel
            return slot_index
        return _logical_dp[slot_index]

    # The SLOT index travels alongside the logical number, because they answer different
    # questions and the grid needs both. `dp` identifies the port to a human (and to the
    # C++ core, for cross-engine comparison); `dp_index` identifies the config ROW, which
    # is what the colour key must key on. Several slots may legally share one
    # DataPortNumber — "a device may not reuse a data-port number; the same number across
    # different devices is fine" (BusConfig.duplicate_dp_numbers) — so a smart-amp config
    # giving each of four devices its own DP0/DP1 has unique (device, dp) pairs but only
    # two distinct numbers. Colouring by the number merged those eight ports into two
    # colours; the slot index is 1:1 with a port and stays put as others enable/disable.
    def _slot_of(slot_index):
        return -1 if slot_index is None or slot_index < 0 else int(slot_index)

    cells = []
    for b in bm.bits:
        sv = b.slot.value
        is_cds = (sv == _CDS_SLOT_VALUE)
        df = b.display_fields
        df_int = df.value if hasattr(df, "value") else (6 if df is None else int(df))
        cells.append({
            "row": b.row, "col": b.column,
            "dp": _dp_of(b.dp),
            "dp_index": _slot_of(b.dp),
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
            "cds_split": cds_split,
        })
    clashes = {}
    for idx in bm.read_overlaps:
        clashes[(idx // ncols, idx % ncols)] = "read"
    for idx in bm.device_clashes:
        clashes[(idx // ncols, idx % ncols)] = "device"
    for idx in bm.bus_clashes:
        clashes[(idx // ncols, idx % ncols)] = "bus"   # bus clash wins the colour
    # The engine labels per-DP notifications "DP{index}" (the Visualizer JSON format
    # recovers the index from that by regex — see BusModelJSONEncoder). For DISPLAY,
    # relabel each with the port's device + LOGICAL DataPortNumber + user-assigned name,
    # e.g. "Dev0 DP3 (LeftMic)", so a notification clearly names its data port (and follows
    # a rename). Model_json/goldens use the encoder directly, not this path.
    #
    # The number must come through _logical_dp for the same reason the cells do: on a config
    # where DataPortNumber differs from the slot index, labelling by index named a port that
    # does not exist. Fixing the cells alone left this path saying "Dev1 DP5" for device 1's
    # DP0 — the identical defect one boundary over, which is exactly the trap the debt
    # register's "label doubling as a serialization key" note warns about. The DICT KEY stays
    # the engine's raw "DP{index}", because that is the string the engine actually emitted.
    issues = issues_from_model(bm)
    dp_labels = {}
    for i, dpv in enumerate(_viz.data_ports):
        try:
            dev = _iface.get_dp_device(i)
        except Exception:
            dev = None
        dev_str = "Mgr" if dev == -1 else (f"Dev{dev}" if dev is not None else "")
        label = f"{dev_str} DP{_dp_of(i)}".strip()
        name = getattr(dpv, "name", None)
        if name and name != f"DP{i}":            # a real rename, not the default label
            label += f" ({name})"
        dp_labels[f"DP{i}"] = label
    for it in issues:
        it.source = dp_labels.get(it.source, it.source)
    return cells, clashes, issues, ncols, bm.num_rows


def issues_from_model(bm):
    """Studio Issues mirroring the Visualizer's ErrorPanel categories/severities."""
    from ..analysis.issues import ERROR, INFO, WARNING, Issue, sort_issues
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
        out.append(Issue(INFO, dp_name, "drawn but no channel enabled"))
    for dp_name, interval, shown in w.display_truncation_warnings:
        out.append(Issue(INFO, dp_name,
                         f"interval is {interval} rows, only {shown} displayed"))
    return sort_issues(out)

