"""Large-.sal guards: cost estimation, the refuse-before-OOM check, and windowed load.

A .sal is a zip of delta-coded transitions, so it expands enormously: the reported
spkr48k_pdm3072.sal is 291 MB on disk, 2.25 GB uncompressed, and ~2.25 bn transitions
= ~18 GB of uint64 edge arrays. Opening it whole exhausted a 24 GB machine and left it
unresponsive with no error, because every stage (zip inflate -> blob decode -> edge
array) was whole-file.

Two defences, both covered here:
  1. estimate_cost() predicts the peak from the zip DIRECTORY only (no inflation), so
     load_capture can raise SalTooLargeError before allocating anything.
  2. A (start, end) window decodes only the overlapping v3 blocks — streamed straight
     from the zip, never holding the inflated blob — so cost scales with the window.
"""
import io
import zipfile

import numpy as np
import pytest

from swi3s_studio.ingest import saleae_binary as sb
from swi3s_studio.ingest import saleae_sal as ss

_RATE = 500_000_000


def _edges(n=5000, seed=3, step=40):
    """Transition samples with deltas in [1, step).

    `step` matters more than it looks: the v3 varint is single-BYTE only for deltas
    <= 64, so a small step exercises none of the multi-byte encodings. That gap let a
    real bug ship — the skipped-block parity counted bytes < 0x80 as one-per-delta,
    but encode_v3_delta(65) == b"\x40\x40" has TWO such bytes, so an odd over-count
    inverted the window's initial line level (samples right, polarity wrong,
    silently). Tests that sweep windows should use several steps; see
    test_windowed_v3_matches_full_decode.
    """
    return np.cumsum(np.random.default_rng(seed).integers(1, step, size=n)).astype(np.uint64)


def _write_sal(tmp_path, ch0: bytes, ch1: bytes, rate: int = _RATE):
    """A minimal two-channel .sal (meta.json + two digital blobs)."""
    p = tmp_path / "cap.sal"
    meta = {
        "data": {"legacySettings": {"sampleRate": {"digital": rate}}},
        "binData": [
            {"type": "Digital", "file": "digital-0.bin", "deviceChannel": 0},
            {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 1},
        ],
    }
    import json
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("meta.json", json.dumps(meta))
        z.writestr("digital-0.bin", ch0)
        z.writestr("digital-1.bin", ch1)
    return str(p)


# --------------------------------------------------------------------------- window

@pytest.mark.parametrize("step", [40, 200, 20000])
@pytest.mark.parametrize("chunk", [0, 250, 1000])
def test_windowed_v3_matches_full_decode(chunk, step):
    """A windowed decode must equal slicing a full decode — samples AND the line
    level at the window start (which depends on the parity of everything skipped).

    Parametrized over `step` so multi-byte varint deltas are covered: at step=40 every
    delta encodes to one byte and the skipped-block parity bug that inverted the line
    level was invisible. See _edges."""
    e = _edges(step=step)
    last = int(e[-1])
    for init in (False, True):
        blob = sb.build_channel_v3(init, e, chunk_size=chunk)
        for a, b in [(0, last + 1), (5000, 20000), (20000, 60000), (1, 2),
                     (0, 1), (last - 10, last + 5), (last, last + 1)]:
            w = sb.parse_channel_v3_window(blob, a, b)
            exp = e[(e > a) & (e < b)]
            exp_init = bool(init) ^ (int(np.count_nonzero(e <= a)) & 1)
            assert np.array_equal(w.transition_samples, exp), f"[{a},{b}) samples"
            assert w.initial_state == exp_init, f"[{a},{b}) initial level"


def test_windowed_decode_drops_the_phantom_end_delta():
    """The v3 chain's last delta is the gap to capture end, not a transition. The
    whole-blob reader drops it; a window covering the end must agree, or the capture
    grows an edge that was never on the wire."""
    e = _edges(n=50)
    blob = sb.build_channel_v3(False, e, chunk_size=10)
    w = sb.parse_channel_v3_window(blob, 0, int(e[-1]) + 10_000)
    assert np.array_equal(w.transition_samples, e)


def test_streaming_window_matches_in_memory_window():
    """The zip-streaming decoder (which never materialises the blob) must agree
    exactly with the in-memory one."""
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    last = int(e[-1])
    for a, b in [(0, last + 1), (5000, 20000), (20000, 60000)]:
        ref = sb.parse_channel_v3_window(blob, a, b)
        got = ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), a, b, "t")
        assert got is not None, "streaming decoder bailed on a valid v3 blob"
        assert np.array_equal(got[1], ref.transition_samples)
        assert got[0] == ref.initial_state


def test_load_capture_window_is_rebased(tmp_path):
    """A windowed load rebases to sample 0, matching Capture.subcapture, so every
    downstream sample number is window-relative."""
    e = _edges()
    path = _write_sal(tmp_path,
                      sb.build_channel_v3(False, e, chunk_size=250),
                      sb.build_channel_v3(False, e + 1, chunk_size=250))
    s0, s1 = 20000, 60000
    cap = ss.load_capture(path, 0, 1, window=(s0, s1))
    exp = e[(e > s0) & (e < s1)].astype(np.int64) - s0
    assert np.array_equal(cap.clock_edges.astype(np.int64), exp)
    assert cap.clock_edges.size and int(cap.clock_edges[0]) >= 0


def test_capture_span_samples_streams_without_inflating(tmp_path):
    """The capture length comes from the block chain's last B_end, read by walking
    headers — it must be available without decoding (that is what lets the app ask
    'how long is this?' about a file too big to open)."""
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    path = _write_sal(tmp_path, blob, blob)
    span = ss.capture_span_samples(path)
    assert span >= int(e[-1]), f"span {span} < last edge {int(e[-1])}"


# ----------------------------------------------------------------------- estimation

def test_estimate_cost_reads_only_the_directory(tmp_path):
    """The estimate must be derivable from the zip directory: it is the thing that
    runs BEFORE we are willing to allocate."""
    e = _edges(n=20000)
    blob = sb.build_channel_v3(False, e, chunk_size=1000)
    path = _write_sal(tmp_path, blob, blob)
    c = ss.estimate_cost(path)
    assert c.uncompressed_bytes >= 2 * len(blob) * 0.9
    assert c.est_transitions > 0
    assert c.est_edge_bytes == c.est_transitions * 8
    assert c.sample_rate_hz == _RATE
    assert "GB" in c.summary() or "MB" in c.summary()


def test_estimate_is_an_upper_bound_on_transitions(tmp_path):
    """Counting payload bytes over-counts (multi-byte varints), never under-counts —
    the safe direction for a guard that must not wave through an OOM."""
    e = _edges(n=20000, step=5000)          # large deltas -> multi-byte varints
    blob = sb.build_channel_v3(False, e, chunk_size=1000)
    path = _write_sal(tmp_path, blob, blob)
    c = ss.estimate_cost(path, channels=[0])
    assert c.est_transitions >= e.size, "estimate under-counted — guard could pass an OOM"


def test_load_refuses_when_over_budget(tmp_path):
    """Over budget => SalTooLargeError, raised before any inflation, carrying the
    numbers the UI needs to offer a window."""
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    path = _write_sal(tmp_path, blob, blob)
    with pytest.raises(ss.SalTooLargeError) as ei:
        ss.load_capture(path, 0, 1, max_bytes=1)
    assert ei.value.cost.est_transitions > 0
    assert "available" in str(ei.value).lower()


def test_guard_disabled_with_zero(tmp_path):
    """max_bytes=0 opts out entirely (scripts/tests that know what they're doing)."""
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    path = _write_sal(tmp_path, blob, blob)
    cap = ss.load_capture(path, 0, 1, max_bytes=0)
    assert cap.clock_edges.size == e.size


def test_window_passes_a_budget_the_full_load_would_fail(tmp_path):
    """The whole point: a window makes an otherwise-refused capture openable. The
    same budget must reject the full load and accept a small window."""
    e = _edges(n=20000)
    blob = sb.build_channel_v3(False, e, chunk_size=500)
    path = _write_sal(tmp_path, blob, blob)
    full = ss.estimate_cost(path)
    budget = full.est_peak_bytes // 4          # too small for the whole file
    with pytest.raises(ss.SalTooLargeError):
        ss.load_capture(path, 0, 1, max_bytes=budget)
    span = ss.capture_span_samples(path)
    cap = ss.load_capture(path, 0, 1, window=(0, span // 100), max_bytes=budget)
    assert cap.clock_edges.size < e.size, "window did not actually narrow the load"


def test_available_memory_is_sane():
    """0 (unknown) or a positive byte count — never negative, which would make the
    guard refuse every capture."""
    assert ss.available_memory_bytes() >= 0


def test_multibyte_varint_skip_block_keeps_the_line_level():
    """A block skipped entirely before the window must contribute the RIGHT parity.

    Regression: the skip path counted bytes < 0x80 as one-per-delta, but a multi-byte
    code whose MSB digit is < 0x40 has two such bytes (encode_v3_delta(65) ==
    b"\\x40\\x40"). An odd over-count flipped the parity, so the window opened with the
    line level INVERTED — transitions correct, polarity wrong, no error raised. Every
    delta here is 100 (two bytes, both < 0x80) with an odd count per block, which is
    the shape that flips it.
    """
    e = np.cumsum(np.full(30, 100, dtype=np.int64)).astype(np.uint64)
    for init in (False, True):
        blob = sb.build_channel_v3(init, e, chunk_size=5)   # 5 deltas/block -> odd
        for a, b in [(700, 900), (1200, 1400), (1700, 1900), (2200, 2400)]:
            w = sb.parse_channel_v3_window(blob, a, b)
            exp = e[(e > a) & (e < b)]
            exp_init = bool(init) ^ (int(np.count_nonzero(e <= a)) & 1)
            assert np.array_equal(w.transition_samples, exp), f"[{a},{b}) samples"
            assert w.initial_state == exp_init, (
                f"init={init} [{a},{b}): line level inverted "
                f"(got {w.initial_state}, want {exp_init})")


def test_guard_fails_closed_when_memory_is_unknown(tmp_path, monkeypatch):
    """available_memory_bytes() == 0 means UNKNOWN, not unlimited.

    psutil is the accurate path but may be absent, and stock macOS has no
    SC_AVPHYS_PAGES — so the budget was 0 and `if budget > 0` silently skipped the
    check, disabling the guard on exactly the machines it was written for. It must
    fall back to a finite budget instead."""
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: 0)
    assert ss._FALLBACK_BUDGET > 0
    e = _edges(n=20000, step=200)
    blob = sb.build_channel_v3(False, e, chunk_size=500)
    path = _write_sal(tmp_path, blob, blob)
    # Force the estimate over the fallback budget: the guard must still raise.
    monkeypatch.setattr(ss, "_FALLBACK_BUDGET", 1)
    with pytest.raises(ss.SalTooLargeError):
        ss.load_capture(path, 0, 1)
