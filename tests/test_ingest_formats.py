"""Capture ingestion tests: .sal (ZIP) meta + version handling, and digital CSV.

Synthesises a v0 .sal, a v3 (compressed-internal) .sal, and a digital CSV from
the demo capture and checks each decodes to the same commands/columns. Also
confirms a real Logic 2 .sal (if present) decodes to a clean, strictly-monotonic
transition stream, and that a broken v3 blob raises a clear MalformedSaleaeV3.

Run: python3 -m pytest tests/test_ingest_formats.py
"""
import json
import os
import struct
import tempfile
import zipfile

import numpy as np
import pytest

from swi3s_studio import decode_capture
from swi3s_studio.ingest import digital_csv, saleae_binary, saleae_sal, transitions

_HEADER = struct.Struct("<8s i i I d d Q")


def _v0_blob(initial: bool, edges_samples, rate) -> bytes:
    times = (np.asarray(edges_samples, dtype=np.float64) / rate)
    head = _HEADER.pack(b"<SALEAE>", 0, 0, int(initial), 0.0,
                        float(times[-1]) if times.size else 0.0, times.size)
    return head + times.astype("<f8").tobytes()


def _v0_blob_begin(initial: bool, edges_samples, rate, begin_time: float) -> bytes:
    """v0 blob for a capture that started at `begin_time` s (negative = pre-trigger).
    Edges are sample indices from capture start, stored as the absolute times
    begin_time + s/rate — how Logic records a pre-trigger capture."""
    times = begin_time + np.asarray(edges_samples, dtype=np.float64) / rate
    head = _HEADER.pack(b"<SALEAE>", 0, 0, int(initial), float(begin_time),
                        float(times[-1]) if times.size else float(begin_time), times.size)
    return head + times.astype("<f8").tobytes()


def _make_sal(path, cap, clk_blob_fn, dat_blob_fn=None):
    """Write a .sal; each blob_fn is blob_fn(initial, edges, rate). The clock and
    data channels may use different encoders (e.g. v3 clock + v0 data)."""
    dat_blob_fn = dat_blob_fn or clk_blob_fn
    rate = cap.sample_rate_hz
    meta = {"data": {"legacySettings": {"sampleRate": {"digital": rate}}},
            "binData": [
                {"type": "Digital", "deviceChannel": 0, "file": "./digital-0.bin"},
                {"type": "Analog",  "deviceChannel": 0, "file": "./analog-0.bin"},
                {"type": "Digital", "deviceChannel": 1, "file": "./digital-1.bin"},
            ]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("meta.json", json.dumps(meta))
        z.writestr("digital-0.bin", clk_blob_fn(cap.initial_clock, cap.clock_edges, rate))
        z.writestr("digital-1.bin", dat_blob_fn(cap.initial_data, cap.data_edges, rate))


def test_v0_chunked_edge_conversion_matches_whole_array():
    """The v0 seconds->uint64 conversion runs in chunks to avoid multi-GiB float64
    temporaries that OOM on a big capture (300M+ edges). It must be bit-identical to
    the naive whole-array expression at every chunk boundary. Regression for the
    'Unable to allocate 2.75 GiB float64' failure on a large .sal."""
    rng = np.random.default_rng(1)
    times = np.sort(rng.uniform(0.0, 5.0, 10_000)).astype(np.float64)
    origin = float(times[0])
    rate = 500e6
    ref = np.rint((times - origin) * rate).astype(np.uint64)
    for step in (1, 2, 7, 999, 10_000, 1 << 20):          # spans single- and multi-chunk
        got = saleae_sal._times_to_edges(times, origin, rate, step=step)
        assert got.dtype == np.uint64
        np.testing.assert_array_equal(got, ref)


def test_sal_v0_roundtrip():
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "demo.sal")
        _make_sal(p, cap, _v0_blob)
        info = saleae_sal.read_info(p)
        assert info.sample_rate_hz == cap.sample_rate_hz
        assert info.channels == [0, 1]
        cap2 = saleae_sal.load_capture(p, 0, 1)
        res = decode_capture(cap2)
    assert res.column_count == ref.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_v3_array_roundtrip():
    # Pure encode->decode of the reverse-engineered v3 layout, single-block and
    # multi-chunk (chunk_size splits the run into block 0 + following chunks).
    for init in (False, True):
        for edges in ([], [3], [3, 7, 8, 200, 455], list(range(5, 5000, 7))):
            arr = np.array(edges, dtype=np.uint64)
            for cs in (0, 1, 4, 50):
                blob = saleae_binary.build_channel_v3(init, arr, chunk_size=cs)
                assert saleae_binary.peek_version(blob) == 3
                ch = saleae_binary.parse_channel_v3(blob)
                assert ch.initial_state == init
                np.testing.assert_array_equal(ch.transition_samples, arr)


def test_v3_zero_run_in_metadata_keeps_initial_state():
    # A zero run in the variable-length metadata (capture timestamp / channel names)
    # must NOT be accepted as a spurious empty first block: that would steal the
    # initial state as 0 (inverting the data line) while the real transitions still
    # decode. The block scan must skip it and find the real first block.
    for init in (True, False):
        blob = saleae_binary.build_channel_v3(init, np.array([3, 7, 20], dtype=np.uint64))
        # Inject a 26-byte all-zero run (a plausible empty-block-shaped header match)
        # right after the 16-byte <SALEAE>/version/type header.
        tampered = blob[:16] + b"\x00" * 26 + blob[16:]
        ch = saleae_binary.parse_channel_v3(tampered, "clk")
        assert ch.initial_state is init, (init, ch.initial_state)
        np.testing.assert_array_equal(ch.transition_samples, [3, 7, 20])
    # A genuinely empty channel (its only block IS empty) still parses.
    for init in (True, False):
        ch = saleae_binary.parse_channel_v3(
            saleae_binary.build_channel_v3(init, np.zeros(0, dtype=np.uint64)))
        assert ch.initial_state is init and ch.transition_samples.size == 0


def test_sal_v3_multichunk_roundtrip():
    # Encode the demo's clock as a *multi-chunk* v3 blob (the layout Logic uses
    # for long channels) and the data as v0, then decode back through the real
    # pipeline -- exercising parse_channel_v3's chunk loop on a real decode.
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)

    def v3_chunked(initial, edges, rate):
        return saleae_binary.build_channel_v3(initial, edges, chunk_size=4096)

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "demo_v3clk.sal")
        _make_sal(p, cap, clk_blob_fn=v3_chunked, dat_blob_fn=_v0_blob)
        cap2 = saleae_sal.load_capture(p, 0, 1)
        np.testing.assert_array_equal(cap2.clock_edges, cap.clock_edges)
        np.testing.assert_array_equal(cap2.data_edges, cap.data_edges)
        res = decode_capture(cap2)
    assert res.column_count == ref.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_sal_mixed_version_pretrigger_alignment():
    # A .sal mixing a v3 clock (samples from capture start) with a v0 data channel
    # (absolute seconds) MUST stay aligned when the capture has a negative pre-trigger
    # begin_time. The v0 channel is rebased on begin_time (capture start == v3 sample 0),
    # NOT on its own first-transition floor — else the two lines skew by the pre-trigger
    # amount and every decoded value garbles. Regression for the mixed-version bug.
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    begin = -0.001                        # 1 ms pre-trigger, as Logic 2 records
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "demo_pretrig.sal")
        _make_sal(p, cap,
                  clk_blob_fn=lambda i, e, r: saleae_binary.build_channel_v3(i, e),
                  dat_blob_fn=lambda i, e, r: _v0_blob_begin(i, e, r, begin))
        cap2 = saleae_sal.load_capture(p, 0, 1)
    # Both channels land on the SAME sample timeline despite the pre-trigger offset.
    np.testing.assert_array_equal(cap2.clock_edges, cap.clock_edges)
    np.testing.assert_array_equal(cap2.data_edges, cap.data_edges)
    res = decode_capture(cap2)
    assert res.column_count == ref.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_sal_v3_malformed_clear_error():
    # A v3 channel whose block chain is broken (byte_count overruns the blob): we
    # surface a clear MalformedSaleaeV3 rather than mis-decode or crash.
    blob = bytearray(saleae_binary.build_channel_v3(
        True, np.arange(5, 105, dtype=np.uint64), chunk_size=10))
    # First block header is right after the 16-byte magic/version/type; corrupt its
    # byte_count so no self-consistent chain can tile the blob.
    struct.pack_into("<Q", blob, 16 + saleae_binary._V3_BYTECOUNT_OFF, len(blob))
    try:
        saleae_binary.parse_channel_v3(bytes(blob), "clk")
        assert False, "expected MalformedSaleaeV3"
    except saleae_binary.MalformedSaleaeV3 as exc:
        assert exc.version == 3 and "Raw data" in str(exc)
        assert isinstance(exc, saleae_binary.UnsupportedSaleaeVersion)


def test_sal_unknown_version_error():
    blob = _HEADER.pack(b"<SALEAE>", 7, 100, 1, 0.0, 1.0, 0) + b"\x00" * 32
    try:
        saleae_binary.parse_channel_v3(blob, "ch")
        assert False, "expected UnsupportedSaleaeVersion"
    except saleae_binary.UnsupportedSaleaeVersion as exc:
        assert exc.version == 7


def test_digital_csv_unsorted_rows_yield_ascending_edges():
    # A CSV whose rows aren't in time order must still yield ASCENDING edge arrays
    # (the C++ TransitionSampleSource requires it). Clock toggles every sample, so
    # every row is a transition; scrambling the row order used to produce
    # non-ascending edges (e.g. [3,2]).
    rate = 1_000_000
    clk = {0: 0, 1: 1, 2: 0, 3: 1}          # toggles every sample
    order = [0, 3, 1, 2]                     # deliberately out of time order
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "unsorted.csv")
        with open(p, "w") as f:
            f.write("Time [s], Clock, Data\n")
            for s in order:
                f.write(f"{s/rate:.12g},{clk[s]},0\n")
        cap = digital_csv.load_capture(p, 1, 2, sample_rate_hz=rate)
    assert cap.clock_edges.tolist() == [1, 2, 3], cap.clock_edges.tolist()
    assert cap.clock_edges.tolist() == sorted(cap.clock_edges.tolist())   # ascending
    assert cap.initial_clock is False       # sample 0 level is 0


def test_digital_csv_roundtrip():
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    rate = cap.sample_rate_hz
    # Build a per-sample digital CSV (Time, Clock, Data) from the capture edges.
    last = int(max(cap.clock_edges[-1], cap.data_edges[-1])) + 2
    clk = np.zeros(last, dtype=np.int8)
    dat = np.zeros(last, dtype=np.int8)
    cs, ds = int(cap.initial_clock), int(cap.initial_data)
    ci = di = 0
    ce, de = cap.clock_edges, cap.data_edges
    for s in range(last):
        while ci < ce.size and ce[ci] == s:
            cs ^= 1; ci += 1
        while di < de.size and de[di] == s:
            ds ^= 1; di += 1
        clk[s] = cs; dat[s] = ds
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cap.csv")
        with open(p, "w") as f:
            f.write("Time [s], Clock, Data\n")
            for s in range(last):
                f.write(f"{s/rate:.12g},{clk[s]},{dat[s]}\n")
        assert digital_csv.read_header(p)[1].strip() == "Clock"
        cap2 = digital_csv.load_capture(p, 1, 2)         # cols: 1=Clock, 2=Data
        res = decode_capture(cap2)
    assert res.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_digital_csv_negative_pretrigger():
    """Logic CSV exports timestamp from a negative pre-trigger origin; the reader
    must rebase to it (a negative time * rate would wrap through the uint64 cast).
    Build the same capture shifted to start well before t=0 and confirm it decodes
    identically with no overflow warning."""
    import warnings
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    rate = cap.sample_rate_hz
    last = int(max(cap.clock_edges[-1], cap.data_edges[-1])) + 2
    clk = np.zeros(last, dtype=np.int8); dat = np.zeros(last, dtype=np.int8)
    cs, ds = int(cap.initial_clock), int(cap.initial_data)
    ci = di = 0
    ce, de = cap.clock_edges, cap.data_edges
    for s in range(last):
        while ci < ce.size and ce[ci] == s:
            cs ^= 1; ci += 1
        while di < de.size and de[di] == s:
            ds ^= 1; di += 1
        clk[s] = cs; dat[s] = ds
    shift = 8.389700564                       # pre-trigger seconds, like a real capture
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "neg.csv")
        with open(p, "w") as f:
            f.write("Time [s], Clock, Data\n")
            for s in range(last):
                f.write(f"{s/rate - shift:.12g},{clk[s]},{dat[s]}\n")
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)   # overflow -> failure
            cap2 = digital_csv.load_capture(p, 1, 2)
        assert np.all(np.diff(cap2.clock_edges) >= 0)         # monotonic, not wrapped
        res = decode_capture(cap2)
    assert res.column_count == 16
    assert [c["command"] for c in res.commands] == [c["command"] for c in ref.commands]


def test_real_sal_if_present():
    """Exercise any real Logic 2 .sal files in ~/Downloads (best effort). The
    clock channel is typically multi-chunk; assert it decodes to a clean,
    strictly-monotonic transition stream.

    A big capture is expected to be REFUSED rather than loaded whole — est_peak_bytes
    carries the decode transient and the budget is capped absolutely, so a multi-GB file
    raises SalTooLargeError on any machine. That is the guard working, not a failure, so
    the assertion moves to a windowed load of the same file: it still proves the file
    decodes, and it covers the streaming window path on real Logic 2 data at the same time.
    """
    import glob
    paths = sorted(glob.glob(os.path.expanduser("~/Downloads/*.sal")))
    if not paths:
        print("   (skipped real .sal: none present in ~/Downloads)")
        return
    for p in paths:
        info = saleae_sal.read_info(p)
        assert info.sample_rate_hz > 0 and len(info.channels) >= 1
        name = os.path.basename(p)
        if len(info.channels) < 2:
            print(f"   {name}: {len(info.channels)} digital ch — skipped (need 2)")
            continue
        clk_ch, dat_ch = info.channels[0], info.channels[1]
        try:
            try:
                cap = saleae_sal.load_capture(p, clk_ch, dat_ch)
                how = "whole"
            except saleae_sal.SalTooLargeError as exc:
                # Too big to open whole: the guard refused before allocating, which is the
                # documented behaviour. Prove it decodes through a window instead.
                span = saleae_sal.capture_span_samples(p, channels=[clk_ch, dat_ch])
                assert span > 0, f"{name}: refused ({exc}) and no block index to window by"
                hi = min(span, int(0.2 * info.sample_rate_hz) or span)
                cap = saleae_sal.load_capture(p, clk_ch, dat_ch, window=(0, hi))
                how = f"window 0-{hi/info.sample_rate_hz:.3f}s (refused whole)"
            assert np.all(np.diff(cap.clock_edges) > 0)      # strictly increasing
            assert np.all(np.diff(cap.data_edges) > 0)
            print(f"   {name}: decoded ({cap.clock_edges.size} clk / "
                  f"{cap.data_edges.size} dat edges, {cap.sample_rate_hz/1e6:g} MHz, {how})")
        except saleae_binary.UnsupportedSaleaeVersion as exc:
            print(f"   {name}: clean error — {type(exc).__name__}")


def test_digital_csv_auto_clock_and_rate():
    """The forwarded clock toggles every UI, so it has far more transitions than
    the data line — channel_transition_counts must rank it first (this drives the
    auto clock/data pick on CSV load). infer_sample_rate must recover the rate
    from the finest timestamp spacing."""
    cap = transitions.demo_capture(16)
    rate = cap.sample_rate_hz
    last = int(max(cap.clock_edges[-1], cap.data_edges[-1])) + 2
    clk = np.zeros(last, dtype=np.int8); dat = np.zeros(last, dtype=np.int8)
    cs, ds = int(cap.initial_clock), int(cap.initial_data)
    ci = di = 0
    ce, de = cap.clock_edges, cap.data_edges
    for s in range(last):
        while ci < ce.size and ce[ci] == s:
            cs ^= 1; ci += 1
        while di < de.size and de[di] == s:
            ds ^= 1; di += 1
        clk[s] = cs; dat[s] = ds
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "auto.csv")
        with open(p, "w") as f:
            # Put data in column 1 and clock in column 2 — auto-detect must NOT
            # rely on column order, only on transition counts.
            f.write("Time [s], Data, Clock\n")
            # %.15g so the per-sample timestamp spacing (1/rate ~ 1.3 ns at 786 MHz)
            # survives to kHz precision — a real analyzer CSV carries full/integer
            # timestamps; %.12g rounding alone would drift infer_sample_rate a few ppm.
            for s in range(last):
                f.write(f"{s/rate:.15g},{dat[s]},{clk[s]}\n")
        counts = digital_csv.channel_transition_counts(p)
        assert len(counts) == 2, counts
        # Clock (column index 1 here) has the most edges.
        assert counts[1] > counts[0], counts
        got = digital_csv.infer_sample_rate(p)
        assert got == rate, (got, rate)


def test_a_v3_delta_code_too_long_for_int64_is_refused():
    """The v3 fast paths do int64 arithmetic, and 128**9 is exactly 2**63.

    So a code with 10 base-128 digits overflows and 11 wraps right around to 1 — at which
    point the block chain's own `sum == B_end - A_start` check is satisfied by a crafted
    B_end of 1, and `parse_channel_v3` returns a channel with NO transitions and no error.
    A silently empty channel is the worst possible outcome for an analyzer: the capture looks
    decoded.

    Not reachable from a real capture — the smallest delta needing ten digits is 2**63
    samples, 585 years at 500 MS/s — so this is about a corrupt or crafted file failing
    loudly. Both fast paths are covered: the check counts interior bytes on the raw block, so
    it guards the native decoder too, which shares the same overflow and cannot be checked
    from Python any other way.
    """
    import struct

    import numpy as np

    from swi3s_studio.ingest import saleae_binary as sb

    def code(digits):
        """One v3 code on the wire: MSB byte | 0x40, interiors | 0x80, raw terminal."""
        return bytes([0x40 | digits[0]] + [0x80 | d for d in digits[1:-1]] + [digits[-1]])

    def blob(a, b, payload):
        head = sb.SALEAE_MAGIC + struct.pack("<II", sb.V3_VERSION, sb.V3_TYPE_DIGITAL)
        return head + struct.pack("<QQHQ", a, b, 0, len(payload)) + payload

    # The detector's boundary: nine digits is the most int64 holds (seven interior bytes).
    for digits, overlong in ((2, False), (5, False), (9, False), (10, True), (11, True)):
        wire = np.frombuffer(code([1] + [0] * (digits - 1)), dtype=np.uint8)
        assert sb._v3_has_overlong_code(wire) is overlong, f"{digits} digits"

    # The crafted file that used to decode to an empty channel with no error.
    crafted = blob(0, 1, code([1] + [0] * 10))          # 11 digits -> wraps to 1
    try:
        channel = sb.parse_channel_v3(crafted, "crafted")
    except sb.MalformedSaleaeV3:
        pass                                            # the required outcome
    else:
        raise AssertionError(
            "an 11-digit delta code was accepted, yielding "
            f"{len(channel.transition_samples)} transitions — a silently empty channel")

    # A legal nine-digit delta must still decode: the guard is about the arithmetic's limit,
    # not about long codes being suspicious.
    digits = [1] + [0] * 8
    value = 0
    for d in digits:
        value = value * 128 + d
    value += 1
    ok = sb.parse_channel_v3(blob(0, value, code(digits)), "legal")
    assert ok.transition_samples.size == 0, \
        "one delta is the gap to capture end, which is dropped — so no transitions"


# --- Logic 2 version 4 -------------------------------------------------------
# v4 is v3 with the version field bumped and NOTHING else changed. Established by
# exporting one capture both ways and decoding the v4 blob with the v3 reader: the block
# chain tiles identically (306 blocks from offset 59 in both), and the transitions matched
# the v0 Binary export EXACTLY on both channels — 19,170,748 clock and 6,633,392 data
# transitions, delta arrays equal, absolute samples equal after the constant frame offset
# the metadata header carries at byte 42. See saleae_binary._V3_LIKE_VERSIONS.


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("chunk", [0, 50])
def test_v3_and_v4_blobs_decode_identically(version, chunk):
    """The reader must treat 3 and 4 as one format, whole-blob and windowed.

    Parametrised over the version so the two are asserted to produce the SAME transitions
    from the same input rather than merely each being self-consistent — a reader that
    special-cased v4 into a subtly different path would pass a v4-only test.
    """
    e = np.cumsum(np.random.default_rng(5).integers(1, 300, size=800)).astype(np.uint64)
    blob = saleae_binary.build_channel_v3(False, e, chunk_size=chunk, version=version)
    assert saleae_binary.peek_version(blob) == version, "builder ignored version="

    ch = saleae_binary.parse_channel_v3(blob, "ch")
    assert np.array_equal(ch.transition_samples, e), f"v{version} whole-blob decode"

    lo, hi = int(e[len(e) // 4]), int(e[3 * len(e) // 4])
    win = saleae_binary.parse_channel_v3_window(blob, lo, hi, "ch")
    assert np.array_equal(win.transition_samples, e[(e > lo) & (e < hi)]), \
        f"v{version} windowed decode"
    assert win.initial_state == bool(int(np.count_nonzero(e <= lo)) & 1), \
        f"v{version} windowed initial level"


@pytest.mark.parametrize("version", [3, 4])
def test_a_v4_sal_loads_and_streams_a_window(tmp_path, version):
    """End to end through load_capture, including the streaming window path.

    The window path has its own version gate (_v3_span_streaming refuses anything outside
    V3_LIKE_VERSIONS before it even looks for the chain) — which is what made a v4 .sal
    unopenable, since that function is also what capture_span_samples uses to answer "how
    long is this?". A Logic-2-shaped blob is used so the header-level fast path applies:
    v4 stamps per-block `level` correctly (306/306 blocks agreed with the decoded parity on
    the real capture, 208 nonzero), so the fast path is sound on it.
    """
    rate = 500_000_000                       # the rate the reported v4 capture was taken at
    e = np.cumsum(np.random.default_rng(6).integers(1, 300, size=2000)).astype(np.uint64)
    end = int(e[-1]) + 1000
    kw = dict(sample_rate_hz=rate, unix_ms=1787088006852, frac_ms=0.767,
              capture_end=end, block_deltas=250, version=version)
    path = str(tmp_path / f"v{version}.sal")
    meta = {"data": {"legacySettings": {"sampleRate": {"digital": rate}}},
            "binData": [{"type": "Digital", "file": "digital-0.bin", "deviceChannel": 0},
                        {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 1}]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta))
        z.writestr("digital-0.bin", saleae_binary.build_logic2_channel_v3(False, e, **kw))
        z.writestr("digital-1.bin", saleae_binary.build_logic2_channel_v3(False, e + 1, **kw))

    assert saleae_sal.capture_span_samples(path, channels=[0, 1]) == end, \
        f"v{version}: the span walk did not accept the chain"

    cap = saleae_sal.load_capture(path, 0, 1)
    assert np.array_equal(cap.clock_edges.astype(np.int64), e.astype(np.int64))

    lo, hi = int(e[500]), int(e[1500])
    wcap = saleae_sal.load_capture(path, 0, 1, window=(lo, hi))
    exp = e[(e > lo) & (e < hi)].astype(np.int64) - lo
    assert np.array_equal(wcap.clock_edges.astype(np.int64), exp), \
        f"v{version}: windowed load"


def test_the_supported_version_set_is_exactly_three_and_four():
    """Adding a version must be a deliberate act backed by evidence, not a drive-by.

    v4 was accepted only after decoding a real v4 blob and matching a v0 export of the same
    capture transition-for-transition on both channels. A future v5 might reuse the block
    chain or might not, and the cost of guessing is asymmetric: refusing a readable file is
    an inconvenience, while decoding a changed format produces a plausible WRONG bus, which
    is the failure this project can least afford. So the set is pinned, and widening it means
    coming back here and saying what was verified.
    """
    assert saleae_binary.V3_LIKE_VERSIONS == frozenset({3, 4})
    blob = _HEADER.pack(b"<SALEAE>", 5, 100, 1, 0.0, 1.0, 0) + b"\x00" * 32
    with pytest.raises(saleae_binary.UnsupportedSaleaeVersion) as ei:
        saleae_binary.parse_channel_v3(blob, "ch")
    assert ei.value.version == 5
    assert "0, 3 and 4" in str(ei.value), \
        "the error must name the versions actually supported, or it misdirects the user"
