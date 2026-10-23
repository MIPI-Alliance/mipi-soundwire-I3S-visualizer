"""A windowed v3 decode checks every block it decodes, and decodes big blocks in chunks.

The whole-file reader refuses a block holding a delta code too long for int64, or whose
deltas do not sum to its header's span. A windowed decode, in memory or streamed from
the zip, must refuse the same blob rather than return edges out of order. The streamed
decode reads an overlapping block a chunk at a time, so its output must not depend on
where the chunk boundaries fall.

Every blob here is built in the test.
"""
import io
import json
import struct
import zipfile

import numpy as np
import pytest

from swi3s_studio.ingest import saleae_binary as sb
from swi3s_studio.ingest import saleae_sal as ss

_RATE = 500_000_000


def _logic_header() -> bytes:
    """The 51 bytes a Logic-shaped v3 blob carries before its first block."""
    blob = sb.build_logic2_channel_v3(False, np.array([5], dtype=np.uint64),
                                      sample_rate_hz=_RATE, unix_ms=1, frac_ms=0.5,
                                      capture_end=10)
    return blob[:51]


def _one_block(body: bytes, b_end: int) -> bytes:
    return _logic_header() + struct.pack("<QQHQ", 0, b_end, 0, len(body)) + body


def _codes(*deltas) -> bytes:
    return b"".join(sb.encode_v3_delta(d) for d in deltas)


# 100, then a ten-digit code that wraps int64 negative (-50), then 10, 10, 1: the block
# sums to 71, so only the code-length check can refuse it.
_OVERLONG = _one_block(_codes(100) + bytes([0x41] + [0xFF] * 8 + [0x4D]) + _codes(10, 10, 1),
                       71)
# Valid codes summing to 121, under a header that says 500.
_WRONG_SUM = _one_block(_codes(100, 10, 10, 1), 500)


def _sal(tmp_path, blob):
    good = sb.build_logic2_channel_v3(False, np.arange(1, 60, 3, dtype=np.uint64),
                                      sample_rate_hz=_RATE, unix_ms=1, frac_ms=0.5,
                                      capture_end=1000)
    meta = {"data": {"legacySettings": {"sampleRate": {"digital": _RATE}}},
            "binData": [{"type": "Digital", "file": f"digital-{i}.bin", "deviceChannel": i}
                        for i in range(2)]}
    path = tmp_path / "c.sal"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta))
        z.writestr("digital-0.bin", blob)
        z.writestr("digital-1.bin", good)
    return str(path)


@pytest.mark.parametrize("blob", [_OVERLONG, _WRONG_SUM], ids=["overlong", "wrong-sum"])
def test_a_corrupt_block_is_refused_with_or_without_a_window(blob, tmp_path):
    with pytest.raises(sb.MalformedSaleaeV3):
        sb.parse_channel_v3(blob)
    with pytest.raises(sb.MalformedSaleaeV3):
        sb.parse_channel_v3_window(blob, 0, 1000)
    for trust in (True, False):
        with pytest.raises(sb.MalformedSaleaeV3):
            ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), 0, 1000, "t",
                                           trust_levels=trust)
    path = _sal(tmp_path, blob)
    with pytest.raises(sb.MalformedSaleaeV3):
        ss.load_capture(path, 0, 1, max_bytes=0)
    with pytest.raises(sb.MalformedSaleaeV3):
        ss.load_capture(path, 0, 1, window=(0, 1000), max_bytes=0)


def test_an_overlong_code_across_a_chunk_boundary_is_refused(monkeypatch):
    # The ten-digit code's interior bytes straddle the chunk seam; the carry holds the
    # start of the unfinished code, so the scan still sees all eight in a row.
    lead = _codes(*([3] * 997))
    body = lead + bytes([0x41] + [0xFF] * 8 + [0x4D]) + _codes(10)
    blob = _one_block(body, 3 * 997 - 50 + 10)
    for chunk in (len(lead) + 3, len(lead) + 5, 64):
        monkeypatch.setattr(ss, "_STREAM_CHUNK", chunk)
        with pytest.raises(sb.MalformedSaleaeV3):
            ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), 0, 10 ** 6, "t",
                                           trust_levels=False)


@pytest.mark.parametrize("step", [40, 200, 20000])
@pytest.mark.parametrize("native", [True, False])
def test_a_streamed_window_is_the_same_in_any_chunk_size(step, native, monkeypatch):
    e = np.cumsum(np.random.default_rng(step).integers(1, step, 3000)).astype(np.uint64)
    blob = sb.build_logic2_channel_v3(True, e, sample_rate_hz=_RATE, unix_ms=1, frac_ms=0.5,
                                      capture_end=int(e[-1]) + 7, block_deltas=1 << 30)
    if not native:
        monkeypatch.setattr(sb, "_native_v3", None)
    last = int(e[-1])
    windows = [(0, last + 7), (last // 3, last // 2), (int(e[100]), int(e[101]) + 1),
               (last - 5, last + 7)]
    for chunk in (1, 2, 7, 64, 4096, 1 << 18):
        monkeypatch.setattr(ss, "_STREAM_CHUNK", chunk)
        for a, b in windows:
            ref = sb.parse_channel_v3_window(blob, a, b)
            got = ss._decode_v3_window_streaming(io.BytesIO(blob), len(blob), a, b, "t")
            assert got[0] == ref.initial_state, (chunk, a, b)
            assert np.array_equal(got[1], ref.transition_samples), (chunk, a, b)


def test_an_edge_at_the_window_start_opens_the_window_at_its_level():
    # The window is the open interval (start, end): an edge AT start is folded into the
    # opening level, as in Capture.subcapture.
    e = np.array([10, 20, 30, 40], dtype=np.uint64)
    blob = sb.build_channel_v3(False, e)
    w = sb.parse_channel_v3_window(blob, 20, 40)
    assert w.transition_samples.tolist() == [30]
    assert w.initial_state is False          # low, then 10 and 20: low again at 20
    w = sb.parse_channel_v3_window(blob, 19, 40)
    assert w.transition_samples.tolist() == [20, 30] and w.initial_state is True
