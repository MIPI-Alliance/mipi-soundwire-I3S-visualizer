"""Audio waveform must not draw across regions where no samples were decoded.

Reported against spkr48k_pdm3072.sal: two active dataport regions separated by a
stretch with no decoded samples (~103 s to ~133 s) were joined by a straight line
in the Audio tab, implying audio values that were never on the bus.

Audio sample INDICES are contiguous across such a hole (index n+1 follows index n
even if it arrived 30 s later), so the gap is only visible in the capture positions
(AudioStore._sample_at). The renderer maps index -> capture sample for X, so the
two regions land far apart on the time axis and the single polyline bridges them.
"""
import numpy as np
import pytest

from swi3s_studio.store.audio_store import AudioStore

_CAP_RATE = 500_000_000.0      # 500 MHz capture (matches the demo/real captures)
_AUDIO_RATE = 48000.0
_GAP_START_S, _GAP_END_S = 103.0, 133.0


def _store_with_gap(n_per_region: int = 2000, gap: bool = True) -> AudioStore:
    """Two active regions of a single 16-bit channel, optionally separated by the
    reported ~103->133 s dead stretch. With gap=False the same samples are laid
    down contiguously (the control case: nothing should be broken).

    The first region is placed so it ENDS at _GAP_START_S; the second begins at
    _GAP_END_S, so the hole between them is exactly the reported ~30 s."""
    spu = _CAP_RATE / _AUDIO_RATE                     # capture samples per audio sample
    first_start = _GAP_START_S * _CAP_RATE - n_per_region * spu
    left = first_start + np.arange(n_per_region) * spu
    if gap:
        right = _GAP_END_S * _CAP_RATE + np.arange(n_per_region) * spu
    else:
        right = left[-1] + spu + np.arange(n_per_region) * spu
    sat = np.concatenate([left, right]).astype(np.uint64)
    n = 2 * n_per_region
    value = (np.sin(np.arange(n) / 20.0) * 10000).astype(np.int64).astype(np.uint32)
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": value, "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.zeros(n, np.int32),
    }
    return AudioStore.from_audio_columns(cols, {(0, 0): _AUDIO_RATE},
                                         capture_rate_hz=_CAP_RATE)


def test_transport_gaps_finds_the_dead_region():
    """The store reports the single index where transport stopped and resumed."""
    st = _store_with_gap()
    gaps = st.transport_gaps(0, 0, 0)
    assert gaps.size == 1, f"expected exactly one gap, got {gaps.size}"
    # The break is at the last index of the first region.
    assert int(gaps[0]) == 1999, gaps
    sat = st.sample_positions(0, 0, 0)
    width_s = (int(sat[2000]) - int(sat[1999])) / _CAP_RATE
    assert 29.0 < width_s < 31.0, f"gap should span ~30 s, got {width_s:.1f} s"


def test_transport_gaps_empty_when_continuous():
    """A contiguous stream must report NO gaps — guards against a tolerance so
    tight that ordinary sample-to-sample jitter reads as a dropout."""
    assert _store_with_gap(gap=False).transport_gaps(0, 0, 0).size == 0


def test_transport_gaps_tolerates_flow_control_jitter():
    """A flow-controlled port legitimately skips transport opportunities, giving
    occasional 2x steps. Those must NOT be reported as gaps."""
    spu = _CAP_RATE / _AUDIO_RATE
    steps = np.full(4000, spu)
    steps[::7] *= 2.0                                  # jitter: every 7th sample waits
    sat = np.concatenate([[0.0], np.cumsum(steps)[:-1]]).astype(np.uint64)
    n = sat.size
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": np.zeros(n, np.uint32), "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.full(n, 1, np.int32),
    }
    st = AudioStore.from_audio_columns(cols, {(0, 0): _AUDIO_RATE},
                                       capture_rate_hz=_CAP_RATE)
    assert st.transport_gaps(0, 0, 0).size == 0, "flow-control jitter misread as a dropout"


def test_rendered_curve_is_broken_across_the_gap():
    """End-to-end through the widget's own render path: the connect mask handed to
    pyqtgraph must cut the polyline exactly where the samples stop.

    This is the regression the user saw — without the mask the curve is one
    unbroken line across ~30 s of undecoded time."""
    pytest.importorskip("PySide6.QtWidgets")
    from swi3s_studio.ui.audio_view import AudioView

    st = _store_with_gap()
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, 4000, max_points=2000)
    sat = st.sample_positions(0, 0, 0)
    xi = np.clip(np.asarray(x_idx, dtype=np.int64), 0, sat.size - 1)
    x = np.asarray(sat[xi], dtype=np.float64)

    view = AudioView.__new__(AudioView)               # no Qt construction needed
    view._store = st
    conn = view._connect_mask(0, 0, 0, xi, x)

    assert conn is not None, "continuous mask returned for a capture WITH a gap"
    assert conn.size == x.size * 2, "mask must be per-vertex over the min/max ladder"
    # Exactly one hi->lo link is cut, and it is the bin pair straddling the hole.
    cuts = np.flatnonzero(conn[1:-1:2] == 0)
    assert cuts.size == 1, f"expected one break, got {cuts.size}"
    jump_s = (x[cuts[0] + 1] - x[cuts[0]]) / _CAP_RATE
    assert jump_s > 25.0, f"break placed at a {jump_s:.1f} s step — wrong bin"
    # The lo->hi pair WITHIN each bin must stay connected (it is the extent bar).
    assert (conn[0:-1:2] == 1).all(), "min/max extent bar was broken"


def test_rendered_curve_is_continuous_without_a_gap():
    """The control case: a gap-free channel returns the 'all' fast path.

    It must be the STRING 'all', never None — see
    test_connect_mask_is_always_accepted_by_pyqtgraph."""
    pytest.importorskip("PySide6.QtWidgets")
    from swi3s_studio.ui.audio_view import AudioView

    st = _store_with_gap(gap=False)
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, 4000, max_points=2000)
    sat = st.sample_positions(0, 0, 0)
    xi = np.clip(np.asarray(x_idx, dtype=np.int64), 0, sat.size - 1)
    x = np.asarray(sat[xi], dtype=np.float64)

    view = AudioView.__new__(AudioView)
    view._store = st
    assert view._connect_mask(0, 0, 0, xi, x) == 'all'


@pytest.mark.parametrize("gap", [True, False])
def test_connect_mask_is_always_accepted_by_pyqtgraph(gap):
    """Whatever _connect_mask returns must be a value setData() accepts.

    Regression: it returned None for a continuous trace, and pyqtgraph rejects that
    with `ValueError: connect argument must be "all", "pairs", "finite", or array`.
    _render is wired to sigXRangeChanged, so the raise landed inside the zoom
    handler — axes repainted, the curve never updated, and the waveform vanished on
    zoom. On a gap-free capture (mask always the continuous case) it never drew at
    all. Asserting the mask's SHAPE is not enough; this feeds it to the real
    setData, which is what actually validates it."""
    pytest.importorskip("PySide6.QtWidgets")
    pg = pytest.importorskip("pyqtgraph")
    from pyqtgraph.Qt import QtWidgets

    from swi3s_studio.ui.audio_view import AudioView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    st = _store_with_gap(gap=gap)
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, 4000, max_points=2000)
    sat = st.sample_positions(0, 0, 0)
    xi = np.clip(np.asarray(x_idx, dtype=np.int64), 0, sat.size - 1)
    x = np.asarray(sat[xi], dtype=np.float64)

    view = AudioView.__new__(AudioView)
    view._store = st
    conn = view._connect_mask(0, 0, 0, xi, x)

    xs = np.repeat(x, 2)
    ys = np.empty(x.size * 2, dtype=np.float64)
    ys[0::2] = np.asarray(lo, dtype=np.float64)
    ys[1::2] = np.asarray(hi, dtype=np.float64)

    plot = pg.PlotWidget()
    plot.setClipToView(True)               # as the real tracks are configured
    curve = plot.plot(pen='w')
    curve.setData(xs, ys, connect=conn)    # raises if `conn` is not a legal value
    # And it must still render after a zoom — the path the user hit.
    plot.setXRange(float(x[len(x) // 3]), float(x[2 * len(x) // 3]), padding=0)
    app.processEvents()
    assert curve.curve.getPath().elementCount() > 0, "curve drew nothing after zoom"


def _silent_then_loud(n1: int = 1990, n2: int = 2000, quiet: int = 0, loud: int = 9000):
    """Region 1 at `quiet`, a ~30 s hole, then region 2 at `loud`.

    n1 is deliberately NOT a multiple of any pyramid factor, so a decimated bin
    lands astride the boundary — the configuration that produced the artifact.
    """
    spu = _CAP_RATE / _AUDIO_RATE
    left = _GAP_START_S * _CAP_RATE - n1 * spu + np.arange(n1) * spu
    right = _GAP_END_S * _CAP_RATE + np.arange(n2) * spu
    sat = np.concatenate([left, right]).astype(np.uint64)
    n = n1 + n2
    val = np.full(n, quiet, np.int64)
    val[n1:] = loud
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": val.astype(np.uint32), "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.zeros(n, np.int32),
    }
    return AudioStore.from_audio_columns(cols, {(0, 0): _AUDIO_RATE},
                                         capture_rate_hz=_CAP_RATE), n1


@pytest.mark.parametrize("max_points", [50, 100, 250, 1000])
def test_no_envelope_bin_spans_a_gap(max_points):
    """No rendered bin may mix samples from both sides of a transport gap.

    Reported on dev6 dp2 at 103,667,003.52 us: the waveform showed a jump from zero
    at the last sample of the region, while the Samples tab showed that sample as 0.
    The render pyramid is built over the contiguous index array and knows nothing
    about capture positions, so one decimated bin aggregated the silence before the
    dropout with the loud audio after it; its [min,max] bar drew at the pre-gap
    position as a jump that no decoded sample contains. Only visible zoomed out,
    where factor > 1 — at full zoom each bin is one sample and cannot straddle.
    """
    st, n1 = _silent_then_loud()
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, n1 + 2000, max_points=max_points)
    assert x_idx.size, "empty envelope"
    # Every bin must sit wholly on one side of the boundary: a quiet-region bin is
    # flat at 0, a loud-region bin flat at 9000. A bin holding both is the bug.
    for j in range(x_idx.size):
        assert not (lo[j] == 0 and hi[j] == 9000), (
            f"bin {j} (x={x_idx[j]}) spans the gap: [{lo[j]}, {hi[j]}]")


@pytest.mark.parametrize("max_points", [50, 100, 250, 1000])
def test_last_pre_gap_bin_matches_the_decoded_sample(max_points):
    """The bin drawn at the end of the pre-gap region must equal the last decoded
    sample there — that is the value the Samples tab shows (0), so the waveform
    must not rise above it."""
    st, n1 = _silent_then_loud()
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, n1 + 2000, max_points=max_points)
    pre = np.flatnonzero(np.asarray(x_idx) < n1)
    assert pre.size, "no bins before the gap"
    j = int(pre[-1])
    assert lo[j] == 0 and hi[j] == 0, (
        f"last pre-gap bin is [{lo[j]}, {hi[j]}], but the last decoded sample is 0")


def test_gap_split_preserves_values_either_side():
    """Splitting must not lose or invent data: the loud region's own bins still
    report the loud value, and the quiet region's still report the quiet one."""
    st, n1 = _silent_then_loud()
    x_idx, lo, hi = st.envelope(0, 0, 0, 0, n1 + 2000, max_points=100)
    x_idx = np.asarray(x_idx)
    assert (hi[x_idx >= n1] == 9000).all(), "post-gap audio lost its amplitude"
    assert (hi[x_idx < n1] == 0).all(), "pre-gap silence gained amplitude"


@pytest.mark.parametrize("flow_mode", [0, 1, 2, 3])   # NORMAL / TX / RX / ASYNC
def test_gaps_are_found_on_flow_controlled_ports_too(flow_mode):
    """A dropout must be detected on EVERY flow mode, not just NORMAL.

    Regression: a flow-controlled channel's `_sample_at` is replaced with a uniform
    de-jitter ramp (mirroring a receiver FIFO clocking out at the average rate), which
    is right for rendering but makes the positions evenly spaced BY CONSTRUCTION — so
    a 30 s dropout left no oversized step for transport_gaps() to find, and the
    waveform drew straight through it on exactly the ports most likely to have one.
    Gap detection now reads the true transport positions stashed before the ramp.
    """
    spu = _CAP_RATE / _AUDIO_RATE
    n = 3000
    left = _GAP_START_S * _CAP_RATE - n * spu + np.arange(n) * spu
    right = _GAP_END_S * _CAP_RATE + np.arange(n) * spu
    sat = np.concatenate([left, right]).astype(np.uint64)
    N = 2 * n
    cols = {
        "device": np.zeros(N, np.int32), "dp": np.zeros(N, np.int32),
        "channel": np.zeros(N, np.int32), "sample_size": np.full(N, 15, np.int32),
        "value": np.zeros(N, np.uint32), "index": np.arange(N, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.full(N, flow_mode, np.int32),
    }
    st = AudioStore.from_audio_columns(cols, {(0, 0): _AUDIO_RATE},
                                       capture_rate_hz=_CAP_RATE)
    gaps = st.transport_gaps(0, 0, 0)
    assert gaps.size == 1, (
        f"flow_mode={flow_mode}: the 30 s dropout was not detected (gaps={gaps.size})")
    assert int(gaps[0]) == n - 1, gaps


def test_gaps_survive_a_mostly_gapped_channel():
    """The nominal step must track the ACTIVE cadence even when gaps outnumber it.

    A median inverts once gaps are ~half the steps — it then reports the GAP width as
    'nominal' and detects nothing, which is the opposite of what a bursty, mostly-idle
    port needs. The lower quartile keeps tracking the real cadence.
    """
    spu = _CAP_RATE / _AUDIO_RATE
    steps = np.empty(200)
    steps[0::2] = spu                 # active step
    steps[1::2] = spu * 5000          # dropout, every other step
    sat = np.concatenate([[0.0], np.cumsum(steps)]).astype(np.uint64)
    n = sat.size
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.full(n, 15, np.int32),
        "value": np.zeros(n, np.uint32), "index": np.arange(n, dtype=np.uint64),
        "start_sample": sat, "flow_mode": np.zeros(n, np.int32),
    }
    st = AudioStore.from_audio_columns(cols, {(0, 0): _AUDIO_RATE},
                                       capture_rate_hz=_CAP_RATE)
    assert st.transport_gaps(0, 0, 0).size == 100, (
        "a channel that is ~half dropouts reported none — the nominal step inverted")
