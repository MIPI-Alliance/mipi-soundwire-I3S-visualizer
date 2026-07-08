"""Reader/writer for the Saleae digital capture binary format.

This implements the documented Saleae **binary export** format (one file per
digital channel): an 8-byte ``<SALEAE>`` identifier, version/type ints, then the
initial state and the absolute transition *times in seconds*. SWI3S Studio reads
a forwarded-clock channel + a data channel and converts the transition times to
sample numbers (via a supplied sample rate) to build a :class:`Capture`.

The writer round-trips the same format, so tests can synthesise capture files.

The Logic 2 project ``.sal`` (a ZIP of ``meta.json`` + per-channel ``digital-N.bin``)
is handled by ``saleae_sal``, which reuses :func:`parse_channel` for version-0
digital blobs and :func:`parse_channel_v3` for Logic 2's **version-3 compressed
internal** format (reverse-engineered below). Other versions raise
:class:`UnsupportedSaleaeVersion`.

Version-3 layout (little-endian), reverse-engineered from Logic 2 captures and
**validated to the sample against a Logic CSV export** (see docs/saleae_sal_format.md)::

    0   char[8]  "<SALEAE>"
    8   u32      version  (== 3)
    12  u32      type     (== 100, digital)
    16  u8       initial_state (0/1)
    17  f64      sample_rate
    25  ...      variable metadata (capture timestamp, channel-name records)
    ..  block:   u64 A_start | u64 B_end | u16 pad | u64 byte_count | codec bytes
    ..  block:   ...  (repeats; blocks chain A_start == previous B_end)

Each block header gives the cumulative sample positions (from capture start) at
its first/last transition and the byte length of the following **base-128 delta
codec** run. The codec stores ``delta - 1`` as big-endian base-128 digits where
the MSB byte adds ``0x40``, interior bytes add ``0x80``, and the final byte is the
raw low digit (``< 0x80``); a single byte ``< 0x40`` is a whole small delta (see
:func:`_v3_decode_deltas`). A block's deltas sum to ``B_end - A_start`` and the
blocks tile the blob exactly (the parser's self-check). The first block starts at
sample 0; the last delta is the gap to capture end and is dropped, so transition
samples ``= cumsum(deltas)[:-1]``. Because a variable-length metadata header
precedes the first block, its offset is found by scanning for the self-consistent
chain rather than assuming a fixed position.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .capture import Capture

_MAGIC = b"<SALEAE>"
_TYPE_DIGITAL = 0
# <8s identifier> <i version> <i type> <I initial_state> <d begin> <d end> <Q n>
_HEADER = struct.Struct("<8s i i I d d Q")


class UnsupportedSaleaeVersion(Exception):
    """A <SALEAE> blob whose version we don't decode. We handle version 0 (the
    documented export) and version 3 (Logic 2's compressed internal `.sal`
    format); anything else lands here."""

    def __init__(self, version: int, label: str = ""):
        self.version = version
        super().__init__(
            f"{label or 'channel'}: unsupported <SALEAE> digital format "
            f"(version {version}). SWI3S Studio reads version 0 and 3; "
            f"in Logic 2 use Export → Raw data → Binary (or CSV) and open that.")


@dataclass
class DigitalChannel:
    initial_state: bool
    times_s: np.ndarray          # float64 absolute transition times (seconds)
    begin_time: float = 0.0
    end_time: float = 0.0


@dataclass
class DigitalChannelSamples:
    """A digital channel decoded straight to integer transition *sample* numbers
    (used by the version-3 reader, which stores sample deltas, not times)."""
    initial_state: bool
    transition_samples: np.ndarray   # uint64 absolute transition sample indices


_V3_VERSION = 3
_V3_TYPE_DIGITAL = 100
# Each transition-data block is prefixed by a 26-byte header:
#   u64 A_start | u64 B_end | u16 level | u64 byte_count
# A_start/B_end are cumulative sample positions (from capture start) at the block's
# first/last transition; `level` is the line state at the block's start (so the
# FIRST block's level is the channel's initial state); byte_count is the length of
# the delta-codec run that follows. Blocks chain (A_start == previous B_end); a
# block's decoded deltas sum to (B_end - A_start). The old fixed offsets
# (_V3_BLOCKS_OFF etc.) were wrong for Logic 2 — the data is preceded by a
# variable-length metadata header, so the first block is located by scanning for a
# self-consistent chain (see parse_channel_v3).
_V3_BLOCK_HEADER = 26
_V3_LEVEL_OFF = 16             # u16 line-level (initial state in the first block)
_V3_BYTECOUNT_OFF = 18         # u64 byte_count offset within the block header


def _v3_encode_delta(delta: int) -> bytes:
    """Encode one sample delta (>= 1) as the v3 base-128 code (inverse of the
    decode in :func:`_v3_decode_deltas`): big-endian base-128 digits of
    ``delta - 1`` where the MSB byte adds 0x40, interior bytes add 0x80, and the
    final byte is the raw low digit (< 0x80)."""
    v = int(delta) - 1
    if v < 0:
        raise ValueError("v3 delta must be >= 1")
    digits = [0] if v == 0 else []
    while v:
        digits.append(v & 0x7f)
        v >>= 7
    digits.reverse()
    if len(digits) == 1 and digits[0] < 0x40:
        return bytes(digits)                       # single-byte code
    if len(digits) == 1:                           # low digit >= 0x40: needs MSB + terminal
        digits = [0] + digits
    out = bytearray([digits[0] + 0x40])            # MSB
    out += bytes(d + 0x80 for d in digits[1:-1])   # interior digits
    out.append(digits[-1])                         # terminal (raw)
    return bytes(out)


def _v3_decode_deltas(buf: bytes, off: int, nbytes: int):
    """Decode ``nbytes`` of the v3 delta codec starting at ``off`` into a list of
    deltas (each >= 1). A byte < 0x40 is a single-byte code; otherwise the first
    byte is the MSB (digit = byte - 0x40), bytes >= 0x80 are interior digits
    (byte - 0x80), and the first byte < 0x80 terminates (raw low digit)."""
    out = []
    end = off + nbytes
    i = off
    while i < end:
        b = buf[i]; i += 1
        if b < 0x40:                               # single-byte code
            out.append(b + 1)
            continue
        val = b - 0x40                             # most-significant digit
        while i < end:
            b = buf[i]; i += 1
            if b >= 0x80:                          # interior digit
                val = val * 128 + (b - 0x80)
            else:                                  # terminal digit
                val = val * 128 + b
                break
        out.append(val + 1)
    return out



class MalformedSaleaeV3(UnsupportedSaleaeVersion):
    """A version-3 digital blob whose block/chunk structure doesn't decode (it
    doesn't tile the file, or a chunk header looks wrong). Treated like an
    unsupported format so the caller can suggest a Raw-Binary/CSV export."""

    def __init__(self, label: str, detail: str):
        self.version = _V3_VERSION
        self.detail = detail
        Exception.__init__(
            self,
            f"{label or 'channel'}: could not decode Logic 2 version-3 digital "
            f"data ({detail}). Try Logic 2 → Export → Raw data → Binary (or CSV) "
            f"and open that instead.")


def parse_channel_v3(data: bytes, label: str = "") -> DigitalChannelSamples:
    """Parse a version-3 (Logic 2 compressed internal) <SALEAE> digital blob.

    Layout (little-endian): `<SALEAE>` · u32 version(=3) · u32 type(=100) ·
    f64 sample_rate · … variable metadata (capture timestamp, channel-name
    records) … · a chain of transition-data blocks. Each block is a 26-byte header
    (u64 A_start, u64 B_end, u16 level, u64 byte_count) followed by `byte_count`
    bytes of the base-128 delta codec (see _v3_decode_deltas). Blocks chain
    (A_start == previous B_end) and a block's deltas sum to (B_end - A_start); the
    first block starts at sample 0 (capture start) and its `level` is the channel's
    initial state. The last delta is the gap to capture end and is dropped, so
    transition samples = cumsum(deltas)[:-1].

    The variable-length metadata means the first block isn't at a fixed offset, so
    it's found by scanning for the self-consistent chain that tiles the blob exactly.
    Raises UnsupportedSaleaeVersion for non-v3 blobs and MalformedSaleaeV3 when no
    such chain is found."""
    if len(data) < 16 or data[:8] != _MAGIC:
        raise ValueError(f"{label or 'data'}: not a Saleae binary capture (bad identifier)")
    version, ctype = struct.unpack_from("<II", data, 8)
    if version != _V3_VERSION:
        raise UnsupportedSaleaeVersion(version, label)
    if ctype != _V3_TYPE_DIGITAL:
        raise ValueError(f"{label or 'data'}: not a digital channel (type={ctype})")
    n = len(data)

    def walk(o0):
        """Decode the block chain starting at o0; return (initial_level, deltas) if
        it tiles the blob exactly, else None. The first block must start at sample
        0, and carries the channel's initial state in its `level` field."""
        deltas = []
        o = o0
        prev_b = None
        initial = 0
        while o + _V3_BLOCK_HEADER <= n:
            a, b = struct.unpack_from("<QQ", data, o)
            cnt = struct.unpack_from("<Q", data, o + _V3_BYTECOUNT_OFF)[0]
            body = o + _V3_BLOCK_HEADER
            if b < a or body + cnt > n:
                return None
            if prev_b is None:
                if a != 0:                          # capture starts at sample 0
                    return None
                # A LEADING EMPTY block (b == a, cnt == 0) is legitimate only for a
                # genuinely empty channel — i.e. when it IS the whole blob. Otherwise
                # it's a spurious match on a zero run in the variable-length metadata:
                # it would steal the initial state as 0 (inverting the data line) and
                # chain into the real first block. Reject so the scan advances to the
                # real block. A real non-empty block always has b > a (every delta
                # >= 1), so this never rejects a valid chain.
                if b == a and body + cnt != n:
                    return None
                initial = struct.unpack_from("<H", data, o + _V3_LEVEL_OFF)[0]
            elif a != prev_b:                       # blocks must chain
                return None
            run = _v3_decode_deltas(data, body, cnt)
            if sum(run) != (b - a):
                return None
            deltas.extend(run)
            prev_b = b
            o = body + cnt
        return (initial, deltas) if o == n else None

    # The variable-length metadata header has no documented bound, so scan the whole
    # pre-block region for the first self-consistent chain start. walk() is only
    # invoked where the u64 at o0 is 0 (a candidate first block, A_start == 0) and
    # bails on the first block that doesn't fit/sum, so a spurious zero-qword is cheap.
    for o0 in range(16, n - _V3_BLOCK_HEADER + 1):
        if struct.unpack_from("<Q", data, o0)[0] != 0:   # first block A_start == 0
            continue
        r = walk(o0)
        if r is None:
            continue
        initial, deltas = r
        if not deltas:
            samples = np.zeros(0, dtype=np.uint64)
        else:
            samples = np.cumsum(np.asarray(deltas, dtype=np.int64))[:-1].astype(np.uint64)
        return DigitalChannelSamples(bool(initial), np.ascontiguousarray(samples))
    raise MalformedSaleaeV3(label, "no self-consistent transition-block chain found "
                                   "(unrecognised version-3 layout)")



def peek_version(data: bytes) -> int:
    """Return the <SALEAE> format version of a digital blob (raises on bad magic)."""
    if len(data) < 12 or data[:8] != _MAGIC:
        raise ValueError("not a Saleae binary capture (bad identifier)")
    return int(struct.unpack_from("<I", data, 8)[0])


def build_channel_v3(initial_state: bool, transition_samples: np.ndarray,
                     chunk_size: int = 0) -> bytes:
    """Serialise a version-3 digital blob (inverse of :func:`parse_channel_v3`).

    Writes a minimal header (`<SALEAE>` · version · type) followed by the
    transition-block chain that `parse_channel_v3` reconstructs: each block is
    `u64 A_start, u64 B_end, u16 level, u64 byte_count` then the base-128 delta
    codec bytes. The first block's `level` carries the initial state. Deltas are
    `[s0] + diff(s) + [1]` (the trailing 1-sample gap to capture end that the
    reader drops). `chunk_size > 0` splits the deltas across multiple blocks
    (exercising the chain walk); 0 uses a single block. Any delta magnitude is
    supported (variable-length codec)."""
    s = np.ascontiguousarray(transition_samples, dtype=np.uint64).astype(np.int64)
    out = bytearray(_MAGIC)
    out += struct.pack("<II", _V3_VERSION, _V3_TYPE_DIGITAL)

    if s.size == 0:                               # no transitions: one empty block
        out += struct.pack("<QQHQ", 0, 0, int(bool(initial_state)), 0)
        return bytes(out)
    if np.any(np.diff(s) <= 0) or s[0] < 0:
        raise ValueError("build_channel_v3: transition samples must be strictly increasing and >= 0")
    deltas = np.empty(s.size + 1, dtype=np.int64)
    deltas[0] = s[0]                              # gap from capture start to first edge
    deltas[1:s.size] = np.diff(s)
    deltas[s.size] = 1                            # trailing gap to capture end (dropped on read)

    n = deltas.size
    step = chunk_size if chunk_size and chunk_size > 0 else n
    a = 0
    i = 0
    while i < n:
        chunk = deltas[i:i + step]
        body = b"".join(_v3_encode_delta(int(d)) for d in chunk.tolist())
        b_end = a + int(chunk.sum())
        level = int(bool(initial_state)) if i == 0 else 0   # initial state in first block
        out += struct.pack("<QQHQ", a, b_end, level, len(body))
        out += body
        a = b_end
        i += step
    return bytes(out)



def parse_channel(data: bytes, label: str = "") -> DigitalChannel:
    """Parse a documented (version 0) <SALEAE> digital blob from memory."""
    if len(data) < _HEADER.size or data[:8] != _MAGIC:
        raise ValueError(f"{label or 'data'}: not a Saleae binary capture (bad identifier)")
    magic, version, ctype, initial, begin, end, n = _HEADER.unpack_from(data, 0)
    if version != 0:
        raise UnsupportedSaleaeVersion(version, label)
    if ctype != _TYPE_DIGITAL:
        raise ValueError(f"{label or 'data'}: not a digital channel (type={ctype})")
    times = np.frombuffer(data, dtype="<f8", count=n, offset=_HEADER.size)
    if times.size != n:
        raise ValueError(f"{label or 'data'}: truncated (expected {n} transitions)")
    return DigitalChannel(bool(initial), np.ascontiguousarray(times), begin, end)


def read_channel(path: str) -> DigitalChannel:
    with open(path, "rb") as f:
        return parse_channel(f.read(), path)


def transition_count(path: str) -> int:
    """Read just the version-0 <SALEAE> header and return its transition count
    (cheap: no need to load the whole edge array). Used to tell the forwarded
    clock (toggles every UI -> most transitions) from the data line. Returns -1
    if the file isn't a readable version-0 digital blob."""
    try:
        with open(path, "rb") as f:
            head = f.read(_HEADER.size)
        if len(head) < _HEADER.size or head[:8] != _MAGIC:
            return -1
        _, version, ctype, _, _, _, n = _HEADER.unpack(head)
        if version != 0 or ctype != _TYPE_DIGITAL:
            return -1
        return int(n)
    except OSError:
        return -1


def _read_times_sample(path: str, count: int) -> np.ndarray:
    """Read up to `count` version-0 transition times (seconds) from the file head."""
    with open(path, "rb") as f:
        head = f.read(_HEADER.size)
        if len(head) < _HEADER.size or head[:8] != _MAGIC:
            return np.zeros(0)
        _, version, ctype, _, _, _, n = _HEADER.unpack(head)
        if version != 0 or ctype != _TYPE_DIGITAL:
            return np.zeros(0)
        k = min(int(count), int(n))
        return np.frombuffer(f.read(k * 8), dtype="<f8")


def infer_sample_rate(paths, sample: int = 2_000_000) -> int:
    """Infer the capture sample rate from version-0 binaries, which store
    transition times in seconds but no rate. Timestamps are integer multiples of
    one sample period, so the finest spacing between transitions (across the given
    channels) is ~one period: rate ~= 1 / min_delta. Snapped to the nearest kHz
    (digital capture rates are kHz-aligned). Returns 0 if it can't be inferred."""
    merged = []
    for p in paths:
        t = _read_times_sample(p, sample)
        if t.size:
            merged.append(t)
    if not merged:
        return 0
    allt = np.unique(np.concatenate(merged))
    if allt.size < 2:
        return 0
    d = np.diff(allt)
    d = d[d > 0]
    if d.size == 0:
        return 0
    rate = 1.0 / float(d.min())
    return int(round(rate / 1000.0)) * 1000        # snap to nearest kHz


def write_channel(path: str, initial_state: bool, times_s: np.ndarray,
                  begin_time: float | None = None, end_time: float | None = None) -> None:
    times = np.ascontiguousarray(times_s, dtype="<f8")
    if begin_time is None:
        begin_time = 0.0
    if end_time is None:
        end_time = float(times[-1]) if times.size else 0.0
    with open(path, "wb") as f:
        f.write(_HEADER.pack(_MAGIC, 0, _TYPE_DIGITAL, int(bool(initial_state)),
                             float(begin_time), float(end_time), int(times.size)))
        f.write(times.tobytes())


def _times_to_samples(times_s: np.ndarray, sample_rate_hz: int,
                      origin_s: float = 0.0) -> np.ndarray:
    """Convert absolute transition times (seconds) to sample indices, measured
    from `origin_s`. Logic captures start at a negative (pre-trigger) time, so the
    origin must be subtracted or the cast to uint64 wraps negatives to garbage."""
    return np.rint((times_s - origin_s) * sample_rate_hz).astype(np.uint64)


def load_capture(clock_path: str, data_path: str, sample_rate_hz: int) -> Capture:
    """Read a clock + data channel pair into a :class:`Capture`."""
    clk = read_channel(clock_path)
    dat = read_channel(data_path)
    # Both channels share one timeline; pick the earliest point (capture start,
    # which Logic records as a negative pre-trigger time) as sample 0.
    origin = min(clk.begin_time, dat.begin_time,
                 float(clk.times_s[0]) if clk.times_s.size else 0.0,
                 float(dat.times_s[0]) if dat.times_s.size else 0.0)
    return Capture(
        clock_edges=_times_to_samples(clk.times_s, sample_rate_hz, origin),
        data_edges=_times_to_samples(dat.times_s, sample_rate_hz, origin),
        initial_clock=clk.initial_state,
        initial_data=dat.initial_state,
        sample_rate_hz=sample_rate_hz,
    )


def write_capture(capture: Capture, clock_path: str, data_path: str) -> None:
    """Write a :class:`Capture` out as a clock + data channel pair."""
    rate = capture.sample_rate_hz
    write_channel(clock_path, capture.initial_clock,
                  capture.clock_edges.astype("<f8") / rate)
    write_channel(data_path, capture.initial_data,
                  capture.data_edges.astype("<f8") / rate)
