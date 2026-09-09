"""Large-.sal guards: cost estimation, the refuse-before-OOM check, and windowed load.

A .sal is a zip of delta-coded transitions, so it expands enormously: a reported capture
was 291 MB on disk, 2.25 GB uncompressed, and ~2.25 bn transitions = ~18 GB of uint64
edge arrays. Opening it whole exhausted a 24 GB machine and left it unresponsive with no
error, because every stage (zip inflate -> blob decode -> edge array) was whole-file.

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


def _write_sal(tmp_path, ch0: bytes, ch1: bytes, rate: int = _RATE, *,
               compress: bool = False, name: str = "cap.sal"):
    """A minimal two-channel .sal (meta.json + two digital blobs).

    `compress` writes DEFLATE members instead of the default STORED. It matters: the span
    walk seeks with ZipExtFile.seek(), which is a real seek only on a STORED member and
    read-and-discard on a compressed one. A STORED-only fixture therefore never exercises
    the path a real Logic 2 capture takes.
    """
    p = tmp_path / name
    meta = {
        "data": {"legacySettings": {"sampleRate": {"digital": rate}}},
        "binData": [
            {"type": "Digital", "file": "digital-0.bin", "deviceChannel": 0},
            {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 1},
        ],
    }
    import json
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(p, "w", mode) as z:
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
    exactly with the in-memory one.

    Pinned to the PARITY path (`trust_levels=False`), because build_channel_v3 stamps 0
    for every non-first block and the header-level fast path correctly refuses such a
    blob — see test_unstamped_block_levels_are_refused_rather_than_guessed.
    """
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    last = int(e[-1])
    for a, b in [(0, last + 1), (5000, 20000), (20000, 60000)]:
        ref = sb.parse_channel_v3_window(blob, a, b)
        got = ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), a, b, "t",
                                             trust_levels=False)
        assert got is not None, "streaming decoder bailed on a valid v3 blob"
        assert np.array_equal(got[1], ref.transition_samples)
        assert got[0] == ref.initial_state


def _logic2_blob(e, initial=False, block_deltas=250):
    """A Logic-2-shaped v3 blob: full metadata header AND per-block `level` stamps."""
    return sb.build_logic2_channel_v3(initial, e, sample_rate_hz=_RATE, unix_ms=0,
                                     frac_ms=0.0, capture_end=int(e[-1]) + 1000,
                                     block_deltas=block_deltas)


@pytest.mark.parametrize("step", [40, 200, 5000])
def test_streaming_window_reads_the_level_from_the_block_header(step, monkeypatch):
    """On a Logic-2 blob the window's opening level comes from a block HEADER, and no
    block before the window is decoded at all.

    All a skipped block ever contributed was the line level at the window's start — one
    bit — and deriving it by counting transitions is O(START OFFSET). On the reported
    291 MB / 176 s capture that made the same 0.5 s window cost 0.85 s at the start and
    28.07 s at 175.5 s, per channel. Each block header already carries `level`, the state
    at that block's start, and the block containing s0 is decoded ANYWAY because it
    overlaps the window — so its own transitions give the level at s0 and nothing earlier
    matters. Verified on the real capture: header level agreed with the decoded parity on
    400/400 blocks of both channels, and the fast path reproduced the parity path's
    initial state AND samples byte-for-byte on every window tried.

    `_count_and_discard` is booby-trapped here: reaching it means a skipped block is being
    decoded again, which is the regression this exists to catch. The step sweep keeps
    multi-byte delta codes in play (see _edges).
    """
    e = _edges(step=step)
    blob = _logic2_blob(e)
    last = int(e[-1])

    def boom(*a, **k):
        raise AssertionError("a skipped block was decoded — the header level was not used")

    monkeypatch.setattr(ss, "_count_and_discard", boom)
    for a, b in [(0, last + 1), (5000, 20000), (20000, 60000), (last // 2, last)]:
        ref = sb.parse_channel_v3_window(blob, a, b)
        got = ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), a, b, "t")
        assert got is not None, "streaming decoder bailed on a Logic-2 v3 blob"
        assert got[0] == ref.initial_state, f"window ({a},{b}) opened at the wrong level"
        assert np.array_equal(got[1], ref.transition_samples), f"window ({a},{b}) samples"


def test_unstamped_block_levels_are_refused_rather_than_guessed(tmp_path):
    """A blob whose per-block levels aren't stamped must be DETECTED, not trusted.

    build_channel_v3 writes `level` only on the first block and 0 on the rest, so reading
    a later block's header would open the window at the wrong level — samples right,
    polarity wrong, silently, which is the exact failure mode the parity walk was written
    to avoid. The fast path therefore requires evidence of stamping (a nonzero level on
    some skipped non-first block) and raises otherwise; load_capture retries by parity, so
    the answer stays correct and only the speed is given up.
    """
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    a, b = 20000, 60000
    with pytest.raises(ss._UnstampedBlockLevels):
        ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), a, b, "t")

    # ...and the loader still gets it right, via the retry.
    path = _write_sal(tmp_path, blob, sb.build_channel_v3(False, e + 1, chunk_size=250))
    cap = ss.load_capture(path, 0, 1, window=(a, b))
    ref = sb.parse_channel_v3_window(blob, a, b)
    assert np.array_equal(cap.clock_edges.astype(np.int64),
                          ref.transition_samples.astype(np.int64) - a)
    assert bool(cap.initial_clock) == bool(ref.initial_state), (
        "the retry lost the line level the parity walk exists to get right")


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


def test_capture_span_is_the_same_on_a_compressed_archive(tmp_path):
    """The span walk must give the same answer on a DEFLATE archive as on a STORED one.

    Every other fixture here is written STORED, where ZipExtFile.seek() is a real seek. On a
    compressed member it is read-and-discard, and the walk also re-seeks to zero for each
    candidate start offset — so the compressed path has different performance AND a different
    code path through zipfile, and was untested. A real Logic 2 capture is compressed."""
    e = _edges()
    blob = sb.build_channel_v3(False, e, chunk_size=250)
    stored = _write_sal(tmp_path, blob, blob, name="stored.sal")
    packed = _write_sal(tmp_path, blob, blob, compress=True, name="packed.sal")
    import zipfile as _z
    with _z.ZipFile(packed) as z:                     # the fixture really is compressed
        assert z.getinfo("digital-0.bin").compress_type == _z.ZIP_DEFLATED
    assert ss.capture_span_samples(packed) == ss.capture_span_samples(stored) >= int(e[-1])


def test_capture_span_channels_filter_restricts_the_walk(tmp_path):
    """`channels=` must limit which blobs are walked — that is the fix for the pre-flight
    span read blocking the GUI, and a filter that is silently ignored would look identical
    from the outside. Asserted by giving the two channels DIFFERENT lengths: filtering to the
    shorter one must return the shorter span, not the max over both."""
    short = _edges(n=200)
    long_ = _edges(n=2000)
    b_short = sb.build_channel_v3(False, short, chunk_size=250)
    b_long = sb.build_channel_v3(False, long_, chunk_size=250)
    path = _write_sal(tmp_path, b_short, b_long)
    both = ss.capture_span_samples(path)
    only0 = ss.capture_span_samples(path, channels=[0])
    only1 = ss.capture_span_samples(path, channels=[1])
    assert only1 > only0, (only0, only1)              # the fixture's premise
    assert both == only1, "unfiltered span should be the max over channels"
    assert only0 == pytest.approx(int(short[-1]), rel=0.05) or only0 >= int(short[-1])


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


def test_the_peak_estimate_carries_the_decode_transient():
    """est_peak_bytes must include the edge-build transient, and only on the edge term.

    load_capture applied _STREAM_TRANSIENT to the WINDOWED branch only -- the path that
    allocates least -- while the whole-file branch, which allocates most, used the bare
    sum. It therefore under-predicted the real peak by 1.9x-3.6x on measured captures:
    0.314 GB predicted / 0.598 actual, 0.596 / 2.125, 0.275 / 0.921. On a 103 GB machine
    that let a capture predicted at 20.29 GB reach ~60 GB resident with no prompt, and
    macOS memory compression then made every navigation pay for it -- the minute-long
    stall that no per-move Python tuning reaches.

    The transient belongs on the edge arrays alone: building them holds the delta run, its
    cumsum, the mask and the concatenate at once, while the inflated blob is one flat
    allocation. Modelled that way the same three captures predict 0.733 / 2.134 / 0.948
    against 0.598 / 2.125 / 0.921 actual -- within a few percent, erring high.
    """
    cost = ss.SalCost(file_bytes=1_000, uncompressed_bytes=2_000_000_000,
                      est_transitions=1_000_000, est_edge_bytes=8_000_000,
                      sample_rate_hz=_RATE)
    assert cost.est_peak_bytes == int(2_000_000_000 + 8_000_000 * ss._STREAM_TRANSIENT)
    assert ss._STREAM_TRANSIENT > 1.0, "a transient of 1 would restore the under-prediction"
    # The blob term must NOT be multiplied: it is one allocation, not a pipeline.
    assert cost.est_peak_bytes < int(
        (2_000_000_000 + 8_000_000) * ss._STREAM_TRANSIENT), "transient applied to the blob too"


def test_the_budget_is_capped_absolutely_not_just_by_free_ram(monkeypatch):
    """A budget of 0.6 x free memory makes the failure mode scale WITH the hardware.

    "Will it fit" and "will it stay interactive" are different questions and only the
    first grows with RAM, so the better the machine the more the app loaded without ever
    asking. That is how a 103 GB box loaded a capture whole and sat in continuous memory
    compression while 16-32 GB machines were offered a time window for the same file and
    never got into trouble -- the reported complaint being that the FASTER machine was
    slower. An explicit max_bytes still overrides the cap, because a caller naming a
    number has already made the decision.
    """
    for free_gb in (4, 16, 64, 103, 1024):
        monkeypatch.setattr(ss, "available_memory_bytes", lambda g=free_gb: int(g * 1e9))
        budget = ss.memory_budget()
        assert budget <= ss._MAX_AUTO_BUDGET, (
            f"{free_gb} GB free produced a {budget/1e9:.1f} GB budget — the cap is not applied")
        assert budget == min(int(free_gb * 1e9 * ss._MEM_HEADROOM), ss._MAX_AUTO_BUDGET), (
            f"{free_gb} GB free: the budget is neither the fraction nor the cap")
    # The fraction still governs below the cap, and the cap still governs above it -- a
    # constant would pass the equality above while breaking one of these.
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: int(4e9))
    assert ss.memory_budget() == int(4e9 * ss._MEM_HEADROOM) < ss._MAX_AUTO_BUDGET
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: int(103e9))
    assert ss.memory_budget() == ss._MAX_AUTO_BUDGET
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: int(1024e9))
    assert ss.memory_budget(max_bytes=64 << 30) == 64 << 30, "explicit max_bytes must win"
