"""Transport gaps and respacing on flow-controlled (TX/RX/ASYNC) ports, and the gap memo.

A flow-controlled port declines transport opportunities, so its samples arrive one or
several opportunities apart. Those runs of declined opportunities are not gaps. A dropout
(the port stops and later resumes) is, and the store respaces each run between dropouts on
its own, so no sample is drawn inside the hole and the rate is measured over the runs.
"""
import numpy as np
import pytest

from swi3s_studio.session import Session
from swi3s_studio.store import audio_store
from swi3s_studio.store.audio_store import AudioStore

_CAP_RATE = 500_000_000.0
_STEP = _CAP_RATE / 48_000.0          # capture samples per stream sample
_OPP = _STEP / 2.0                    # opportunities at twice the stream rate


def _store(sat, flow_mode, capture_rate_hz=_CAP_RATE, rate=0.0):
    sat = np.asarray(sat).astype(np.uint64)
    n = sat.size
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": np.zeros(n, np.uint32), "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.full(n, flow_mode, np.int32),
    }
    return AudioStore.from_audio_columns(cols, {(0, 0): rate}, capture_rate_hz=capture_rate_hz)


def _coin_transport(n, seed=1):
    """`n` transport positions where each opportunity carries a sample on a fair coin, so a
    run of k declined opportunities has probability 2**-k."""
    rng = np.random.default_rng(seed)
    opportunities = rng.geometric(0.5, size=n - 1)          # 1, 2, 3 ... with mean 2
    return np.concatenate([[0], np.cumsum(opportunities)]) * _OPP


@pytest.fixture(scope="module")
def flow_demo():
    return AudioStore.from_session(Session.from_demo(4000, cold_start=True,
                                                     variant="flow_control"))


def test_the_flow_control_demo_has_no_transport_gaps(flow_demo):
    for dev, dp in flow_demo.streams():
        for ch in flow_demo.channels(dev, dp):
            assert flow_demo.transport_gaps(dev, dp, ch).size == 0, (dev, dp, ch)


def test_the_flow_control_demo_envelope_is_not_split(flow_demo):
    """With no gaps the zoomed-out envelope is the pyramid's bins and nothing more, the same
    count as an evenly transported NORMAL port carrying the same number of samples."""
    for dev, dp in flow_demo.streams():
        for ch in flow_demo.channels(dev, dp):
            n = len(flow_demo.samples(dev, dp, ch))
            even = _store(np.arange(n) * _STEP, 0)
            x, _, _ = flow_demo.envelope(dev, dp, ch, 0, n, max_points=256)
            assert x.size == even.envelope(0, 0, 0, 0, n, max_points=256)[0].size


@pytest.mark.parametrize("flow_mode", [1, 2, 3])
def test_declined_opportunities_are_not_gaps_and_a_dropout_is(flow_mode):
    jitter = _coin_transport(50_000)
    assert np.diff(jitter).max() > 8 * _STEP                 # the tail is many stream steps
    assert _store(jitter, flow_mode).transport_gaps(0, 0, 0).size == 0
    resumed = jitter[-1] + 30 * _CAP_RATE + _coin_transport(50_000, seed=2)
    gaps = _store(np.concatenate([jitter, resumed]), flow_mode).transport_gaps(0, 0, 0)
    assert gaps.tolist() == [49_999]


def test_a_normal_port_keeps_the_lower_quartile_rule():
    """A NORMAL port's step four times its usual one is a gap."""
    steps = np.full(999, _STEP)
    steps[500] = 5 * _STEP
    sat = np.concatenate([[0.0], np.cumsum(steps)])
    assert _store(sat, 0).transport_gaps(0, 0, 0).tolist() == [500]


@pytest.mark.parametrize("flow_mode", [1, 2, 3])
def test_a_dropout_ends_a_run_and_is_left_out_of_the_rate(flow_mode):
    """1000 samples 500 capture samples apart, a dropout, then 1000 more: each run keeps its
    own positions and the rate is the runs' rate, 48 kHz at a 24 MHz capture."""
    sat = np.concatenate([np.arange(1000) * 500, 10_000_000 + np.arange(1000) * 500])
    st = _store(sat, flow_mode, capture_rate_hz=24e6)
    pos = st.sample_positions(0, 0, 0)
    np.testing.assert_array_equal(pos, sat)
    assert st.transport_gaps(0, 0, 0).tolist() == [999]
    assert st.rate(0, 0) == pytest.approx(48_000.0)
    assert st.native_rate(0, 0) == pytest.approx(48_000.0)


def test_each_run_is_respaced_on_its_own():
    """Jittered runs either side of a dropout are each spread evenly between their own
    first and last transports."""
    left = _coin_transport(5_000)
    right = left[-1] + 10 * _CAP_RATE + _coin_transport(5_000, seed=3)
    sat = np.round(np.concatenate([left, right])).astype(np.int64)
    pos = np.asarray(_store(sat, 1).sample_positions(0, 0, 0), dtype=np.int64)
    assert pos[0] == sat[0] and pos[4_999] == sat[4_999]
    assert pos[5_000] == sat[5_000] and pos[-1] == sat[-1]
    for run in (pos[:5_000], pos[5_000:]):
        d = np.diff(run)
        assert int(d.max() - d.min()) <= 1


def test_gaps_are_computed_once_per_channel_and_read_only(monkeypatch):
    calls = []
    real = audio_store._gap_indices
    monkeypatch.setattr(audio_store, "_gap_indices",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    sat = np.concatenate([np.arange(1000) * _STEP, 30 * _CAP_RATE + np.arange(1000) * _STEP])
    st = _store(sat, 0)
    first = st.transport_gaps(0, 0, 0)
    assert st.transport_gaps(0, 0, 0) is first and len(calls) == 1
    assert not first.flags.writeable
    st.envelope(0, 0, 0, 0, 2000, max_points=100)
    st.transport_step(0, 0, 0)
    assert len(calls) == 1
    assert st.transport_gaps(0, 0, 0, tolerance=1e9).size == 0 and len(calls) == 2


def test_the_waveforms_connect_mask_reads_the_memoised_gaps(monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from swi3s_studio.ui.audio_view import AudioView

    sat = np.concatenate([np.arange(1000) * _STEP, 30 * _CAP_RATE + np.arange(1000) * _STEP])
    st = _store(sat, 0)
    calls = []
    real = audio_store._gap_indices
    monkeypatch.setattr(audio_store, "_gap_indices",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    x_idx, _, _ = st.envelope(0, 0, 0, 0, 2000, max_points=100)
    pos = st.sample_positions(0, 0, 0)
    xi = np.asarray(x_idx, dtype=np.int64)
    view = AudioView.__new__(AudioView)
    view._store = st
    for _ in range(3):
        conn = view._connect_mask(0, 0, 0, xi, np.asarray(pos[xi], dtype=np.float64))
        assert not isinstance(conn, str)
    assert len(calls) == 1
