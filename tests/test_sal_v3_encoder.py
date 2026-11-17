"""The v3 delta encoder is byte-identical to a scalar reference encoder, and round-trips.

`_reference_code` and `_reference_blocks` below are the per-delta scalar encoder and block
writer, kept here as the definition the array encoder must reproduce byte for byte. Every
encoded run is also decoded back by the scalar decoder, the array decoder and (when built)
the native decoder. Edge values cover each code length's first and last value, the single
byte limit (0x40), the two-byte case for 0x40..0x7F, the int64 limit the decoders hold, and
the uint64 limit the encoder accepts.
"""
import struct

import numpy as np
import pytest

from swi3s_studio.ingest import saleae_binary as sb

try:
    import swi3score
    _NATIVE = getattr(swi3score, "decode_saleae_v3_deltas", None)
except Exception:                                  # pragma: no cover - native optional
    _NATIVE = None

_INT64_MAX = (1 << 63) - 1


def _reference_code(delta: int) -> bytes:
    v = int(delta) - 1
    if v < 0:
        raise ValueError("v3 delta must be >= 1")
    digits = [0] if v == 0 else []
    while v:
        digits.append(v & 0x7f)
        v >>= 7
    digits.reverse()
    if len(digits) == 1 and digits[0] < 0x40:
        return bytes(digits)
    if len(digits) == 1:
        digits = [0] + digits
    out = bytearray([digits[0] + 0x40])
    out += bytes(d + 0x80 for d in digits[1:-1])
    out.append(digits[-1])
    return bytes(out)


def _reference_blocks(init: int, deltas: np.ndarray, block_size: int,
                      per_block_level: bool) -> bytes:
    step = block_size if block_size and block_size > 0 else max(deltas.size, 1)
    starts = list(range(0, deltas.size, step)) or [0]
    out = bytearray()
    a = 0
    for i in starts:
        chunk = deltas[i:i + step]
        level = (init ^ (i & 1)) if per_block_level else (init if i == 0 else 0)
        b_end = a + int(chunk.sum())
        body = b"".join(_reference_code(int(d)) for d in chunk.tolist())
        out += struct.pack("<QQHQ", a, b_end, level, len(body))
        out += body
        a = b_end
    return bytes(out)


def _edge_values():
    vals = {1, 2, 0x40, 0x41, 0x42, 0x80, 0x81}
    for k in range(1, 10):                       # each code length's first and last value
        floor = 1 << (7 * k)
        vals.update({floor - 1, floor, floor + 1, floor + 2})
    vals.update({(1 << 14) - 1, 1 << 14, 1 << 21, 1 << 28, 1 << 35, _INT64_MAX - 1,
                 _INT64_MAX})
    return sorted(v for v in vals if 1 <= v <= _INT64_MAX)


def _every_length(n_per_length: int, seed: int, top: int = _INT64_MAX) -> np.ndarray:
    """Random deltas whose codes take every byte length 1..9 (the int64 range), or every
    length that holds a delta no larger than `top`."""
    rng = np.random.default_rng(seed)
    bounds = [(1, 0x40), (0x41, 1 << 14)] + [(1 << (7 * k), 1 << (7 * (k + 1)))
                                             for k in range(2, 9)]
    bounds[-1] = (bounds[-1][0], _INT64_MAX)
    parts = [rng.integers(lo, min(hi, top), n_per_length, dtype=np.int64, endpoint=True)
             for lo, hi in bounds if lo <= top]
    d = np.concatenate(parts)
    rng.shuffle(d)
    return d


def _decoded_by_every_decoder(body: bytes) -> list:
    """The deltas each available decoder reads from `body`; all must agree."""
    scalar = sb.decode_v3_deltas(body, 0, len(body))
    array = sb._v3_decode_deltas_np(body, 0, len(body), _chunk=1 << 10).tolist()
    assert array == scalar
    if _NATIVE is not None:
        assert _NATIVE(np.frombuffer(body, np.uint8), 0, len(body)).tolist() == scalar
    return scalar


@pytest.mark.parametrize("delta", _edge_values())
def test_an_edge_delta_encodes_as_the_reference_and_decodes_back(delta):
    assert sb.encode_v3_delta(delta) == _reference_code(delta)
    assert sb.encode_v3_deltas(np.array([delta], dtype=np.int64)) == _reference_code(delta)
    assert _decoded_by_every_decoder(sb.encode_v3_delta(delta)) == [delta]


@pytest.mark.parametrize("delta", [1 << 63, (1 << 63) + 1, (1 << 64) - 1])
def test_a_delta_past_int64_encodes_as_the_reference(delta):
    # Ten digits: the decoders refuse such a code; the encoder still writes it exactly.
    assert sb.encode_v3_delta(delta) == _reference_code(delta)
    assert sb.encode_v3_deltas(np.array([delta], dtype=np.uint64)) == _reference_code(delta)


def test_the_edge_values_in_one_run_are_the_reference_run():
    d = np.array(_edge_values(), dtype=np.int64)
    body = sb.encode_v3_deltas(d)
    assert body == b"".join(_reference_code(x) for x in d.tolist())
    assert _decoded_by_every_decoder(body) == d.tolist()


def test_an_empty_array_encodes_to_nothing():
    assert sb.encode_v3_deltas(np.zeros(0, dtype=np.int64)) == b""
    assert sb.encode_v3_deltas(np.zeros(0, dtype=np.uint64)) == b""


@pytest.mark.parametrize("bad", [0, -1, -(1 << 62)])
def test_a_delta_below_one_is_refused(bad):
    with pytest.raises(ValueError, match=">= 1"):
        sb.encode_v3_delta(bad)
    with pytest.raises(ValueError, match=">= 1"):
        sb.encode_v3_deltas(np.array([5, bad, 7], dtype=np.int64))


def test_a_non_integer_array_is_refused():
    with pytest.raises(TypeError):
        sb.encode_v3_deltas(np.array([1.0, 2.0]))


def test_random_deltas_of_every_code_length_are_the_reference_run():
    d = _every_length(2_000, seed=3)
    body = sb.encode_v3_deltas(d)
    assert body == b"".join(_reference_code(x) for x in d.tolist())
    assert _decoded_by_every_decoder(body) == d.tolist()


def test_a_run_longer_than_one_encode_pass_is_the_reference_run(monkeypatch):
    monkeypatch.setattr(sb, "_V3_ENCODE_CHUNK", 1_000)
    d = _every_length(500, seed=4)
    assert sb.encode_v3_deltas(d) == b"".join(_reference_code(x) for x in d.tolist())


@pytest.mark.parametrize("block_size", [0, 1, 3, 7, 4096])
@pytest.mark.parametrize("per_block_level", [False, True])
@pytest.mark.parametrize("init", [0, 1])
def test_the_block_chain_is_the_reference_chain(block_size, per_block_level, init,
                                                monkeypatch):
    # A small encode pass, so groups of blocks split the deltas at block boundaries.
    monkeypatch.setattr(sb, "_V3_ENCODE_CHUNK", 50)
    d = _every_length(40, seed=5, top=1 << 50)     # block sums stay inside a u64
    for deltas in (d, d[:1], np.zeros(0, dtype=np.int64)):
        assert (sb._emit_v3_blocks(init, deltas, block_size, per_block_level)
                == _reference_blocks(init, deltas, block_size, per_block_level))


@pytest.mark.parametrize("chunk_size", [0, 1, 2, 1000])
def test_a_blob_with_one_transition_round_trips(chunk_size):
    for s in (1, 63, 64, 65, 127, 128, 1 << 40):
        blob = sb.build_channel_v3(True, np.array([s], dtype=np.uint64), chunk_size)
        got = sb.parse_channel_v3(blob)
        assert got.initial_state is True
        assert got.transition_samples.tolist() == [s]
    with pytest.raises(ValueError, match=">= 1"):     # no gap from capture start
        sb.build_channel_v3(True, np.array([0], dtype=np.uint64), chunk_size)


def test_a_large_capture_round_trips_through_both_writers():
    rng = np.random.default_rng(6)
    gaps = np.concatenate([rng.integers(1, 64, 300_000), rng.integers(64, 1 << 14, 100_000),
                           rng.integers(1 << 14, 1 << 30, 20_000)])
    rng.shuffle(gaps)
    s = np.cumsum(gaps).astype(np.uint64)
    blob = sb.build_channel_v3(False, s, chunk_size=100_000)
    assert np.array_equal(sb.parse_channel_v3(blob).transition_samples, s)
    end = int(s[-1]) + 17
    blob = sb.build_logic2_channel_v3(True, s, sample_rate_hz=500e6, unix_ms=1, frac_ms=0.5,
                                      capture_end=end)
    got = sb.parse_channel_v3(blob)
    assert got.initial_state is True and np.array_equal(got.transition_samples, s)
    deltas = np.diff(np.concatenate(([0], s.astype(np.int64), [end])))
    assert blob[51:] == _reference_blocks(1, deltas, sb.V3_LOGIC_BLOCK_DELTAS, True)
