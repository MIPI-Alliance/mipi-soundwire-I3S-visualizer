"""Transport gaps on a NORMAL port whose samples are grouped onto rows (SampleGrouping).

A grouped port carries several samples a few capture samples apart, then nothing until
its next row, so its steps are short in-row steps and one long row step per group. The
row step is the stream's cadence, not a gap; a dropout is still a gap.
"""
import numpy as np
import pytest

from swi3s_studio.store.audio_store import AudioStore

_CAP_RATE = 24_000_000.0


def _grouped(n, per_row, in_row, row):
    """`n` positions, `per_row` samples per row `in_row` apart, rows `row` apart."""
    i = np.arange(n)
    return (i // per_row) * row + (i % per_row) * in_row


def _store(sat, rate=0.0, capture_rate_hz=_CAP_RATE):
    sat = np.asarray(sat).astype(np.uint64)
    n = sat.size
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": np.zeros(n, np.uint32), "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.zeros(n, np.int32),
    }
    return AudioStore.from_audio_columns(cols, {(0, 0): rate}, capture_rate_hz=capture_rate_hz)


@pytest.mark.parametrize("verified", [False, True])
def test_two_samples_per_row_have_no_gaps(verified):
    """Steps of 4 then 60: the row step is 15x the in-row step and 1.9x the average."""
    sat = _grouped(4000, 2, 4, 64)
    st = _store(sat, rate=_CAP_RATE / 32.0 if verified else 0.0)
    assert st.transport_gaps(0, 0, 0).size == 0
    assert st.timing_is_irregular(0, 0)
    assert st.transport_step(0, 0, 0) == pytest.approx(32.0, rel=1e-3)


def test_eight_samples_per_row_have_no_gaps():
    """Eight samples 17 apart, rows 256 apart: in-row steps over half the average step."""
    sat = _grouped(8000, 8, 17, 256)
    st = _store(sat, rate=_CAP_RATE / 32.0)
    assert st.transport_gaps(0, 0, 0).size == 0
    assert st.transport_step(0, 0, 0) == pytest.approx(32.0, rel=1e-3)


def test_a_grouped_port_with_long_intervals_has_no_gaps():
    """Two samples 128 apart, then the next pair 2083 on: rows several intervals apart."""
    i = np.arange(4000)
    sat = (i // 2) * 2083 + (i % 2) * 128
    st = _store(sat)
    assert st.transport_gaps(0, 0, 0).size == 0
    assert st.transport_step(0, 0, 0) == pytest.approx((sat[-1] - sat[0]) / 3999, rel=1e-9)


def test_a_dropout_on_a_grouped_port_is_a_gap():
    left = _grouped(2000, 2, 4, 64)
    right = left[-1] + 10_000_000 + _grouped(2000, 2, 4, 64)
    st = _store(np.concatenate([left, right]))
    assert st.transport_gaps(0, 0, 0).tolist() == [1999]


def test_a_reported_rate_keeps_a_mostly_idle_port_gapped():
    """Pairs of samples one stream step apart, 5000 steps between pairs, at a reported rate
    of one sample per step: the long steps are gaps, not the row step of a grouping."""
    step = 500.0
    steps = np.empty(200)
    steps[0::2] = step
    steps[1::2] = step * 5000
    sat = np.concatenate([[0.0], np.cumsum(steps)])
    st = _store(sat, rate=_CAP_RATE / step)
    assert st.transport_gaps(0, 0, 0).size == 100
