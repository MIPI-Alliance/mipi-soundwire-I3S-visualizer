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
from pathlib import Path

import numpy as np
import pytest

from swi3s_studio import import_notes
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


# 400 edges ten samples apart in blocks of 50 deltas: block k covers edges 50k .. 50k+49.
_TAIL_EDGES = np.arange(10, 4010, 10, dtype=np.uint64)


def _block_offsets(blob: bytes):
    """``[(header offset, A_start, B_end, byte count)]`` of a Logic-shaped blob's chain."""
    out, pos = [], 51
    while pos + 26 <= len(blob):
        a, b, _lvl, cnt = struct.unpack_from("<QQHQ", blob, pos)
        out.append((pos, a, b, cnt))
        pos += 26 + cnt
    return out


def _blob_with_bad_block(k: int, at: int = 0) -> bytes:
    """The `_TAIL_EDGES` blob with byte `at` of block `k`'s run changed from a 10-sample
    delta to an 11-sample one, so that block no longer sums to its header's span."""
    blob = bytearray(sb.build_logic2_channel_v3(False, _TAIL_EDGES, sample_rate_hz=_RATE,
                                                unix_ms=1, frac_ms=0.5, capture_end=4100,
                                                block_deltas=50))
    off = _block_offsets(bytes(blob))[k][0] + 26 + at
    assert blob[off] == sb.encode_v3_delta(10)[0]
    blob[off] = sb.encode_v3_delta(11)[0]
    return bytes(blob)


def test_a_bad_block_after_the_window_is_an_import_warning(tmp_path):
    path = _sal(tmp_path, _blob_with_bad_block(6))
    with pytest.raises(sb.MalformedSaleaeV3):
        ss.load_capture(path, 0, 1, max_bytes=0)
    window = (0, int(_TAIL_EDGES[120]))                    # ends inside block 2
    with import_notes.collect() as notes:
        cap = ss.load_capture(path, 0, 1, window=window, max_bytes=0)
    assert cap.clock_edges.tolist() == _TAIL_EDGES[:120].tolist()
    texts = [n.message for n in notes]
    assert len(texts) == 1, texts
    assert "digital ch0" in texts[0] and "after the window" in texts[0], texts
    assert f"{int(_TAIL_EDGES[299]):,}" in texts[0], texts     # block 6 starts at edge 299


def test_a_good_capture_past_the_window_raises_no_warning(tmp_path):
    blob = sb.build_logic2_channel_v3(False, _TAIL_EDGES, sample_rate_hz=_RATE, unix_ms=1,
                                      frac_ms=0.5, capture_end=4100, block_deltas=50)
    with import_notes.collect() as notes:
        ss.load_capture(_sal(tmp_path, blob), 0, 1, window=(0, 1200), max_bytes=0)
    assert list(notes) == []


def test_the_block_holding_the_window_end_is_checked_whole(tmp_path, monkeypatch):
    # The bad byte is in block 2 past the window's end, and the chunk is small enough
    # that the window's edges end chunks before the block does.
    monkeypatch.setattr(ss, "_STREAM_CHUNK", 4)
    path = _sal(tmp_path, _blob_with_bad_block(2, at=40))
    with pytest.raises(sb.MalformedSaleaeV3):
        ss.load_capture(path, 0, 1, window=(0, int(_TAIL_EDGES[110])), max_bytes=0)



# 60,000 edges, random enough that deflate keeps the blob big.
_DAMAGED_EDGES = np.cumsum(np.random.default_rng(1).integers(1, 5000, 60000)).astype(np.uint64)


def _damaged_blob(block_deltas: int = 500) -> bytes:
    return sb.build_logic2_channel_v3(False, _DAMAGED_EDGES, sample_rate_hz=_RATE, unix_ms=1,
                                      frac_ms=0.5, capture_end=int(_DAMAGED_EDGES[-1]) + 10,
                                      block_deltas=block_deltas)


def _member_data(raw: bytes, name: str):
    """``(offset, size)`` of member `name`'s compressed data in the zip bytes `raw`."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        zi = z.getinfo(name)
    n, m = struct.unpack_from("<HH", raw, zi.header_offset + 26)
    return zi.header_offset + 30 + n + m, zi.compress_size


def _with_bad_crc(path: str) -> None:
    """Change the CRC-32 the central directory records for digital-0.bin."""
    raw = bytearray(Path(path).read_bytes())
    i = raw.find(b"PK\x01\x02")
    while i >= 0:
        n = struct.unpack_from("<H", raw, i + 28)[0]
        if raw[i + 46:i + 46 + n] == b"digital-0.bin":
            raw[i + 16] ^= 0xFF
        i = raw.find(b"PK\x01\x02", i + 4)
    Path(path).write_bytes(bytes(raw))


def _with_bad_deflate(path: str, at: float) -> None:
    """Overwrite 40 bytes of digital-0.bin's deflate stream, `at` of the way through it."""
    raw = bytearray(Path(path).read_bytes())
    off, size = _member_data(bytes(raw), "digital-0.bin")
    start = off + int(size * at)
    raw[start:start + 40] = b"\xff" * 40
    Path(path).write_bytes(bytes(raw))


@pytest.mark.parametrize("damage", ["crc", "deflate"])
def test_a_member_that_cannot_be_read_past_the_window_is_an_import_warning(damage, tmp_path):
    path = _sal(tmp_path, _damaged_blob())
    if damage == "crc":
        _with_bad_crc(path)
    else:
        _with_bad_deflate(path, 0.9)
    with pytest.raises(ValueError, match="cannot read"):
        ss.load_capture(path, 0, 1, max_bytes=0)
    window = (0, int(_DAMAGED_EDGES[100]))
    with import_notes.collect() as notes:
        cap = ss.load_capture(path, 0, 1, window=window, max_bytes=0)
    assert cap.clock_edges.tolist() == _DAMAGED_EDGES[:100].tolist()
    texts = [n.message for n in notes]
    assert any("digital ch0: the capture cannot be read past sample" in t
               and "after the window" in t and "Bad CRC-32" in t for t in texts), texts


def test_a_member_that_cannot_be_read_inside_the_window_is_refused(tmp_path):
    path = _sal(tmp_path, _damaged_blob())
    _with_bad_deflate(path, 0.5)
    with pytest.raises(ValueError, match=r"digital ch0 cannot be read past sample .*inside "
                                         r"the window"):
        ss.load_capture(path, 0, 1, window=(0, int(_DAMAGED_EDGES[-1])), max_bytes=0)


def test_a_bad_crc_met_only_after_the_window_is_an_import_warning(tmp_path):
    # Two blocks, the second big: the header walk reads its header without reaching
    # the member's end, so the check of the blocks after the window meets the bad
    # CRC.
    path = _sal(tmp_path, _damaged_blob(block_deltas=50000))
    _with_bad_crc(path)
    info = ss.read_info(path)
    assert ss._v3_chains(path, info, [0])[0][5] is None
    with import_notes.collect() as notes:
        cap = ss.load_capture(path, 0, 1, window=(0, int(_DAMAGED_EDGES[100])), max_bytes=0)
    assert cap.clock_edges.tolist() == _DAMAGED_EDGES[:100].tolist()
    texts = [n.message for n in notes]
    assert len(texts) == 1 and "after the window" in texts[0] \
        and "Bad CRC-32" in texts[0], texts
