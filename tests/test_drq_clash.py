"""DRQ clash classification — the one slot type whose direction is the INVERSE of its port's.

A DRQ's direction is not the parent data port's: `Device._drq_direction()` defines it as
the opposite ("PortDirection_REG: True = SINK DP -> DRQ SOURCE"), because a Sink DP's flow
control port DRIVES its DRQ to ask for data, while a Source DP samples it. Every other slot
type takes its direction from `PortDirection_REG` directly, so for those the two forms of the
question agree and nothing distinguishes them.

The engine's clash check read `PortDirection_REG` anyway, which inverted the verdict for DRQ
bits in both directions: two devices genuinely DRIVING one DRQ column — a physical two-driver
collision — was recorded as a benign read overlap (and never registered as a write at all),
while several devices legally SAMPLING one DRQ column was reported as a bus clash.

WHY THIS NEEDED A HAND-BUILT CONFIG: the 96 vendored goldens carry no DRQ collision, so
`test_visualizer_engine.py` (bits + bus_clashes + read_overlaps + warnings, the authoritative
parity test) passes identically with the check inverted or correct. Fixing the inversion moves
no golden. That is precisely why the bug survived, and why the case is pinned here instead.

Run: PYTHONPATH=. python3 -m pytest tests/test_drq_clash.py
"""
import logging

from swi3s_studio.swviz.core.engine import BusModelBuilder
from swi3s_studio.swviz.models.enums import FlowMode
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

logging.disable(logging.CRITICAL)      # the engine logs each clash at INFO

_FCP_COLUMN = 12                       # both ports' DRQ lands here, on purpose
_NUM_COLUMNS = 16                      # NumColumns_REG 15 -> 16 columns
_ROWS = 8


def _two_ports_sharing_a_drq_column(port_direction: bool):
    """Two data ports on DIFFERENT devices whose FCPs use the same DRQ column.

    `port_direction` is PortDirection_REG: True = Sink DP (so both DRIVE the DRQ, a
    collision), False = Source DP (so both SAMPLE it, which is legal). The data windows are
    kept apart from the DRQ column so the DRQ verdict is not confused with a data clash.
    """
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = _NUM_COLUMNS - 1
    for slot, device in ((0, 3), (1, 5)):
        dp = iface.data_ports[slot].config
        dp.PortDirection_REG = port_direction
        dp.FlowMode_REG = FlowMode.RX_CONTROLLED      # RX_CONTROLLED places a DRQ
        dp.EnableCh_REG = 0b1
        dp.SampleSize_REG = 7
        dp.HorizontalStart_REG = 2 + slot * 4
        dp.HorizontalCount_REG = 3
        dp.Interval_REG = 3
        fcp = iface.flow_control_ports[slot].config
        fcp.FCP_HorizontalStart_REG = _FCP_COLUMN
        fcp.FCP_BitWidth_REG = 0
        fcp.FCP_TailWidth_REG = 0
        fcp.FCP_Offset_REG = 0
        fcp.FCP_GuardEnable_REG = False
        fcp.FCP_GuardPolarity_REG = False
        viz.data_ports[slot].enabled = True
        iface.set_dp_device(slot, device)
    return BusModelBuilder(iface, _ROWS, viz).build()


def test_two_driven_drqs_in_one_column_are_a_bus_clash():
    """Two Sink DPs on different devices both drive their DRQ at one column: two drivers on
    the bus at the same instant, which is a bus clash and not a read overlap."""
    bm = _two_ports_sharing_a_drq_column(port_direction=True)
    assert _FCP_COLUMN in bm.bus_clashes, (
        f"two DRIVEN DRQs in column {_FCP_COLUMN} must be a bus clash; "
        f"got bus_clashes={bm.bus_clashes} read_overlaps={bm.read_overlaps}")
    assert _FCP_COLUMN not in bm.read_overlaps, (
        "a two-driver collision is not a read overlap: "
        f"read_overlaps={bm.read_overlaps}")


def test_two_sampled_drqs_in_one_column_are_a_read_overlap():
    """Two Source DPs sample the same DRQ column. Multiple readers of one bit are legal, so
    this is a read overlap — the direction the same inverted check got wrong the other way."""
    bm = _two_ports_sharing_a_drq_column(port_direction=False)
    assert _FCP_COLUMN in bm.read_overlaps, (
        f"two SAMPLED DRQs in column {_FCP_COLUMN} must be a read overlap; "
        f"got read_overlaps={bm.read_overlaps} bus_clashes={bm.bus_clashes}")
    assert _FCP_COLUMN not in bm.bus_clashes, (
        "several devices reading one DRQ column is not a physical clash: "
        f"bus_clashes={bm.bus_clashes}")


def test_a_driven_drq_is_registered_as_a_write():
    """The write must reach the clash detector, not just be classified. The inverted branch
    also skipped `add_write` for a driven DRQ, so the column read as unoccupied to whatever
    was placed next — a clash the model would then never see at all."""
    bm = _two_ports_sharing_a_drq_column(port_direction=True)
    drq_bits = [b for b in bm.bits if bm.bit_index(b.row, b.column) == _FCP_COLUMN]
    assert drq_bits, f"expected a bit placed at column {_FCP_COLUMN}"
    # Two devices drove it, so the detector saw two writes at one index: that is the clash
    # asserted above. Its presence in bus_clashes is the observable proof add_write ran.
    assert _FCP_COLUMN in bm.bus_clashes
