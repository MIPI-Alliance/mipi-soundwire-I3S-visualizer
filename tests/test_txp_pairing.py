"""TxPresent pairing in the Visualizer engine: a flow-controlled source port's TxPresent bit
is matched to its sink's on the bit's last UI, where the sink reads it. A wide bit
(BitWidth_REG > 0) holds the TxPresent slot for BitWidth_REG + 1 UIs; only the last is
paired, and every UI still occupies its column.

Every input is a hand-built Interface: a source and a sink port on two devices at the same
slots, Tx Controlled.

Run: PYTHONPATH=. python3 -m pytest tests/test_txp_pairing.py
"""
import logging

import pytest

from swi3s_studio.swviz.core.engine import BusModelBuilder
from swi3s_studio.swviz.models.enums import FlowMode, SlotType
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

logging.disable(logging.CRITICAL)

_ROWS = 8


def _build(bit_width, with_sink=True):
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = 15
    ports = ((0, 3, False), (1, 5, True)) if with_sink else ((0, 3, False),)
    for slot, device, sink in ports:
        dp = iface.data_ports[slot].config
        dp.PortDirection_REG = sink
        dp.FlowMode_REG = FlowMode.TX_CONTROLLED
        dp.EnableCh_REG = 0b1
        dp.SampleSize_REG = 3
        dp.BitWidth_REG = bit_width
        dp.HorizontalStart_REG = 2
        dp.HorizontalCount_REG = 11
        dp.Interval_REG = 3
        viz.data_ports[slot].enabled = True
        iface.set_dp_device(slot, device)
    return BusModelBuilder(iface, _ROWS, viz).build()


def _txp_bits(bm):
    return [b for b in bm.bits if b.slot == SlotType.TX_PRESENT and b.dp == 0]


@pytest.mark.parametrize("bit_width", [0, 1, 2])
def test_a_paired_tx_controlled_port_has_no_txp_mismatch(bit_width):
    bm = _build(bit_width)
    assert len(_txp_bits(bm)) == 2 * (bit_width + 1)      # one TxPresent per interval
    assert bm.warnings.txp_mismatches == []


@pytest.mark.parametrize("bit_width", [0, 1, 2])
def test_an_unpaired_wide_txp_is_reported_once_per_bit_on_its_last_ui(bit_width):
    bm = _build(bit_width, with_sink=False)
    last = [b.bit_index for b in _txp_bits(bm)][bit_width::bit_width + 1]
    assert bm.warnings.txp_mismatches == last
