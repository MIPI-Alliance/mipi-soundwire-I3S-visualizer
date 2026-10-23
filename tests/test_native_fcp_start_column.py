"""The Flow Control Port shares its data port's Column-0 phase.

A config-CSV decode of a capture that starts partway into a row seeds every data port
with the capture's start column. The FCP beside an RX_CONTROLLED or ASYNC port places its
DRQ bit at the absolute bus column FCP_HorizontalStart, so it is seeded with the same
start column. These tests decode the flow-control demo once from the wire (the reference)
and then from slices that start at every column of a row, with the demo's config CSV, and
check that each DRQ keeps its distance from the data it gates.
"""
import collections
import os

import pytest
import swi3score

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_CSV = os.path.join(_FIXTURES, "flow_control_demo.csv")
_SAMPLES_PER_UI = 4
_COLUMNS = 16
_DRQ_DEVICES = (2, 3)            # RX_CONTROLLED, ASYNC


def _decode(levels, csv=None):
    st = swi3score.DecoderSettings()
    if csv:
        st.config_csv_path = csv
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI), st)
    dec.run()
    return dec


def _drq_to_sample_end(dec):
    """Bus-column distance from each DRQ-gated sample's DRQ bit to the sample's last bit."""
    return collections.Counter(
        ((a["end_sample"] - a["drq_sample"]) // _SAMPLES_PER_UI) % _COLUMNS
        for a in dec.audio() if a["device"] in _DRQ_DEVICES and a["drq_sample"])


@pytest.fixture(scope="module")
def flow_levels():
    levels = swi3score.make_demo_levels_flow_control(120)
    ref = _decode(levels)
    assert ref.segments()[-1]["column_count"] == _COLUMNS
    assert ref.flow_control_stats()["fails"] == 0
    return levels, ref


@pytest.mark.parametrize("offset", range(_COLUMNS))
def test_csv_decode_from_mid_row_keeps_drq_column(flow_levels, offset):
    levels, ref = flow_levels
    want = set(_drq_to_sample_end(ref))
    assert want, "the reference decode carries no DRQ-gated samples"
    start = ref.segments()[-1]["start_ui"] + 64 * _COLUMNS + offset
    got = _drq_to_sample_end(_decode(levels[start:], _CSV))
    assert got, f"offset {offset}: no DRQ-gated samples decoded"
    assert set(got) <= want, (
        f"offset {offset}: DRQ-to-sample distances {dict(got)}, wire decode {sorted(want)}")
