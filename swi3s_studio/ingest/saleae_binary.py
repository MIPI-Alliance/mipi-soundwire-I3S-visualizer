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
internal** format (layout below). Version 4 is the SAME format with the version
field bumped (see _V3_LIKE_VERSIONS), so it goes through the same reader. Other
versions raise :class:`UnsupportedSaleaeVersion`.

Version-3 layout (little-endian)::

    0   char[8]  "<SALEAE>"
    8   u32      version  (== 3, or 4: identical format, see _V3_LIKE_VERSIONS)
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

import os
import struct
from dataclasses import dataclass

import numpy as np

from .. import import_notes
from .capture import Capture

# The v3 delta codec prefers the native decoder (swi3score.decode_saleae_v3_deltas)
# and falls back to the vectorised-numpy _v3_decode_deltas_np when the native core is
# absent. Both match decode_v3_deltas bit for bit.
try:
    import swi3score as _score
    _native_v3 = getattr(_score, "decode_saleae_v3_deltas", None)
except Exception:                                  # pragma: no cover - native optional
    _native_v3 = None

_MAGIC = b"<SALEAE>"
_TYPE_DIGITAL = 0
# <8s identifier> <i version> <i type> <I initial_state> <d begin> <d end> <Q n>
_HEADER = struct.Struct("<8s i i I d d Q")


class UnsupportedSaleaeVersion(Exception):
    """A <SALEAE> blob whose version we don't decode. We handle version 0 (the
    documented export) and versions 3 and 4 (Logic 2's compressed internal `.sal`
    format, which are the same format under two version numbers); anything else
    lands here."""

    def __init__(self, version: int, label: str = ""):
        self.version = version
        super().__init__(
            f"{label or 'channel'}: unsupported <SALEAE> digital format "
            f"(version {version}). SWI3S Studio reads versions 0, 3 and 4; "
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
_V4_VERSION = 4
# Logic 2's version-4 digital blob is the version-3 format with only the version number
# changed: the 26-byte block header, the block chain and the base-128 delta codec are
# identical, and per-block `level` is stamped correctly, so the windowed fast path in
# saleae_sal applies. The readers accept this SET; any other version raises
# UnsupportedSaleaeVersion rather than being decoded.
_V3_LIKE_VERSIONS = frozenset({_V3_VERSION, _V4_VERSION})
_V3_TYPE_DIGITAL = 100
# Public aliases for the v3 codec surface, so exporters (sal_export, saleae_sal)
# don't reach into these privates. The codec + block-chain layout lives here (one
# owner); callers pick the header (minimal round-trip vs full Logic-2).
SALEAE_MAGIC = _MAGIC
V3_VERSION = _V3_VERSION
V4_VERSION = _V4_VERSION
V3_LIKE_VERSIONS = _V3_LIKE_VERSIONS
V3_TYPE_DIGITAL = _V3_TYPE_DIGITAL
# Each transition-data block is prefixed by a 26-byte header:
#   u64 A_start | u64 B_end | u16 level | u64 byte_count
# A_start/B_end are cumulative sample positions (from capture start) at the block's
# first/last transition; `level` is the line state at the block's start (so the
# FIRST block's level is the channel's initial state); byte_count is the length of
# the delta-codec run that follows. Blocks chain (A_start == previous B_end); a
# block's decoded deltas sum to (B_end - A_start). The data is preceded by a
# variable-length metadata header, so the first block is located by scanning for a
# self-consistent chain (see parse_channel_v3).
_V3_BLOCK_HEADER = 26
_V3_LEVEL_OFF = 16             # u16 line-level (initial state in the first block)
_V3_BYTECOUNT_OFF = 18         # u64 byte_count offset within the block header
# Bytes from the blob's start in which the first block header must begin. Every reader
# of the chain (parse_channel_v3, _find_v3_chain_start, saleae_sal._v3_span_streaming)
# tries candidate offsets only inside this prefix, so all of them accept the same blobs.
# The Logic 2 metadata header before the chain is 51 bytes (build_logic2_channel_v3).
V3_CHAIN_START_SCAN = 1 << 16


def encode_v3_delta(delta: int) -> bytes:
    """Encode one sample delta (>= 1) as the v3 base-128 code (inverse of the
    decode in :func:`decode_v3_deltas`): big-endian base-128 digits of
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


_v3_encode_delta = encode_v3_delta      # internal alias (pre-public name)


def decode_v3_deltas(buf: bytes, off: int, nbytes: int):
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


_v3_decode_deltas = decode_v3_deltas    # internal alias (pre-public name)

# A v3 code is one MSB byte (>= 0x40), zero or more INTERIOR bytes (>= 0x80), then a terminal
# byte (< 0x80), so its digit count is (interiors + 2). 128**9 is exactly 2**63: 10 base-128
# digits overflow the int64 arithmetic both fast paths use, and 11 wrap right around, so a
# crafted 11-digit code decodes to 1 and satisfies the chain's `sum == B_end - A_start`
# check against a crafted B_end of 1. Every v3 decode path (parse_channel_v3,
# parse_channel_v3_window and the zip-streaming decoder in saleae_sal) refuses a block
# holding a longer code before decoding it. Nine digits (seven interiors) is the most int64
# can hold.
_V3_MAX_INTERIOR_RUN = 7


def _v3_has_overlong_code(bb: np.ndarray) -> bool:
    """True if `bb` contains a delta code too long for int64 to hold.

    Counts the longest run of bytes >= 0x80 instead of re-deriving code boundaries, which
    makes it independent of where the slice starts — the caller may be mid-chunk — and lets it
    guard the NATIVE decoder too, which shares the same int64 arithmetic and cannot be checked
    from Python any other way. No real capture comes close: the smallest delta that needs ten
    digits is 2**63 samples, which is 585 years at 500 MS/s.
    """
    run = _V3_MAX_INTERIOR_RUN + 1                 # too long: this many high bytes in a row
    if bb.size < run:
        return False
    high = bb >= 0x80
    if not high.any():
        return False
    # AND the mask with itself shifted 1..run-1 places: what survives starts a run that long.
    # Two bytes per payload byte.
    n = bb.size - run + 1
    hit = high[:n].copy()
    for k in range(1, run):
        hit &= high[k:k + n]
    return bool(hit.any())


def _v3_decode_block_np(bb: np.ndarray, require_all: bool):
    """Decode a code-aligned slice `bb` (uint8) of the v3 delta codec into an int64
    delta array, returning (deltas, bytes_consumed). Vectorised equivalent of the
    byte loop in :func:`decode_v3_deltas`. `bb` must START at a code boundary; when
    ``require_all`` is False it stops at the last COMPLETE code and reports how many
    bytes it consumed (the trailing partial code is the caller's carry), so a long
    body can be streamed in chunks without cross-chunk state.

    The codec is a small state machine (single byte < 0x40 is a whole code; otherwise
    an MSB byte >= 0x40, zero or more interior bytes >= 0x80, then a terminal byte
    < 0x80). "Am I mid-code?" is a scan where a byte >= 0x80 forces mid=True, a byte
    < 0x40 forces mid=False, and 0x40..0x7F toggles it — computed here as
    force_value(last forcing byte) XOR parity(distance), so it vectorises."""
    n = bb.size
    if n == 0:
        return np.zeros(0, dtype=np.int64), 0
    H = bb >= 0x80                       # interior / high MSB: always continues the code
    L = bb < 0x40                        # single code / low terminal: ends the code
    idx = np.arange(n, dtype=np.int64)
    force = H | L                        # bytes that set mid absolutely (H->True, L->False)
    lf = np.maximum.accumulate(np.where(force, idx, np.int64(-1)))   # last forcing byte, or -1
    base = H[np.clip(lf, 0, None)] & (lf >= 0)                       # its value (virtual -1 => False)
    mid_after = base ^ (((idx - lf) & 1) != 0)                       # toggles run from that byte
    mid_before = np.empty(n, dtype=bool)
    mid_before[0] = False                # each chunk starts at a code boundary
    mid_before[1:] = mid_after[:-1]
    cs = ~mid_before                     # code-start bytes
    M = (~H) & (~L)                      # 0x40..0x7F
    if not require_all:
        ends = L | (M & mid_before)      # last byte of a code (single, or terminal after MSB)
        ep = np.flatnonzero(ends)
        if ep.size == 0:
            return np.zeros(0, dtype=np.int64), 0   # no complete code yet: carry it all
        m = int(ep[-1]) + 1
    else:
        m = n
    d = bb[:m].astype(np.int64)
    csm = cs[:m]
    d[csm & ~L[:m]] -= 0x40              # MSB digit
    d[(~csm) & H[:m]] -= 0x80            # interior digit (single/terminal keep the raw byte)
    seg = np.flatnonzero(csm)            # code start indices -> reduceat segments
    code_id = np.cumsum(csm) - 1
    si = np.maximum.accumulate(np.where(csm, idx[:m], np.int64(0)))
    pos = idx[:m] - si                   # byte position within its code
    e = np.bincount(code_id)[code_id] - 1 - pos                     # base-128 exponent (MSB..LSB)
    pow128 = np.int64(128) ** np.arange(int(e.max()) + 1 if e.size else 1, dtype=np.int64)
    return np.add.reduceat(d * pow128[e], seg) + 1, m


def _v3_decode_deltas_np(buf, off: int, nbytes: int, _chunk: int = 1 << 23) -> np.ndarray:
    """Vectorised, memory-bounded equivalent of :func:`decode_v3_deltas` returning an
    int64 array. Streams the body in code-aligned chunks (each begins at a code
    boundary, carrying any partial trailing code forward) so peak memory stays a few
    ×`_chunk`, not the whole body. Bit-identical to the scalar decoder."""
    if nbytes == 0:
        return np.zeros(0, dtype=np.int64)
    mv = memoryview(buf)[off:off + nbytes]
    parts = []
    carry = b""
    i = 0
    while i < nbytes or carry:
        chunk = carry + bytes(mv[i:i + _chunk])
        i += _chunk
        vals, consumed = _v3_decode_block_np(np.frombuffer(chunk, np.uint8), i >= nbytes)
        if vals.size:
            parts.append(vals)
        carry = chunk[consumed:]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)


def _emit_v3_blocks(init: int, deltas: np.ndarray, block_size: int,
                    per_block_level: bool) -> bytes:
    """Emit the v3 transition-block chain body — the part shared by the minimal
    (:func:`build_channel_v3`) and full-Logic (:func:`build_logic2_channel_v3`)
    blobs. Each block is ``u64 A_start, u64 B_end, u16 level, u64 byte_count`` then
    the base-128 delta codec run; blocks chain (``A_start == prev B_end``).

    ``init`` is the line level at the first delta. ``per_block_level=True`` stamps
    each block's true level (``init ^ (start_index & 1)``, what Logic renders from);
    ``False`` stamps 0 for non-first blocks (only block 0's level is read back).
    ``block_size <= 0`` emits a single block. An empty ``deltas`` still emits one
    (empty) block carrying just the initial level."""
    step = block_size if block_size and block_size > 0 else max(deltas.size, 1)
    starts = list(range(0, deltas.size, step)) or [0]
    out = bytearray()
    a = 0
    for i in starts:
        chunk = deltas[i:i + step]
        level = (init ^ (i & 1)) if per_block_level else (init if i == 0 else 0)
        b_end = a + int(chunk.sum())
        body = b"".join(encode_v3_delta(int(d)) for d in chunk.tolist())
        out += struct.pack("<QQHQ", a, b_end, level, len(body))
        out += body
        a = b_end
    return bytes(out)



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
    it's found by scanning the first `V3_CHAIN_START_SCAN` bytes for the start of a
    self-consistent chain that tiles the blob exactly.
    Raises UnsupportedSaleaeVersion for non-v3 blobs and MalformedSaleaeV3 when no
    such chain is found."""
    if len(data) < 16 or data[:8] != _MAGIC:
        raise ValueError(f"{label or 'data'}: not a Saleae binary capture (bad identifier)")
    version, ctype = struct.unpack_from("<II", data, 8)
    if version not in _V3_LIKE_VERSIONS:          # 3 and 4 are the same format (see the set)
        raise UnsupportedSaleaeVersion(version, label)
    if ctype != _V3_TYPE_DIGITAL:
        raise ValueError(f"{label or 'data'}: not a digital channel (type={ctype})")
    n = len(data)
    # One uint8 view of the whole blob (zero-copy) for the native per-block decode.
    buf8 = np.frombuffer(data, np.uint8) if _native_v3 is not None else None

    def decode_run(body, cnt, checked=False):
        """Decode one block's delta run to an int64 array — native if available.

        REJECTS AN OVER-LONG CODE FIRST, for both paths: they share the int64 arithmetic that
        an 11-digit code wraps to 1, and the chain check below compares that 1 against a
        B_end from the same (crafted) header, so it cannot catch it. Returning None here
        makes walk() treat the block as "not a chain", which is the right outcome either way
        — a spurious zero-qword in the metadata keeps scanning, and a genuinely corrupt
        payload ends with MalformedSaleaeV3 rather than an empty channel that looks decoded.
        `checked` skips that scan for a block walk() has already passed: it costs about two
        int64s per payload byte, as much as the output again on a multi-byte-delta block.
        """
        bb = (buf8[body:body + cnt] if buf8 is not None
              else np.frombuffer(data, np.uint8, count=cnt, offset=body))
        if not checked and _v3_has_overlong_code(bb):
            return None
        if _native_v3 is not None:
            return _native_v3(buf8, body, cnt)
        return _v3_decode_deltas_np(data, body, cnt)

    def walk(o0):
        """Validate the block chain starting at o0; return (initial_level, blocks) if it
        tiles the blob exactly, else None, where `blocks` is one (body, cnt, A_start,
        delta count) per block. The first block must start at sample 0, and carries the
        channel's initial state in its `level` field.

        Each run is decoded to check it and then DROPPED, so validating costs one block,
        and the fill below decodes it again straight into the output."""
        blocks = []
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
            run = decode_run(body, cnt)                   # int64 array (native or numpy)
            if run is None:                               # over-long code: not a valid chain
                return None
            if int(run.sum()) != (b - a):
                return None
            blocks.append((body, cnt, a, int(run.size)))
            prev_b = b
            o = body + cnt
        return (initial, blocks) if o == n else None

    # The first self-consistent chain start inside the first V3_CHAIN_START_SCAN bytes.
    # walk() is only invoked where the u64 at o0 is 0 (a candidate first block,
    # A_start == 0) and bails on the first block that doesn't fit/sum, so a spurious
    # zero-qword is cheap.
    for o0 in range(16, min(n, V3_CHAIN_START_SCAN) - _V3_BLOCK_HEADER + 1):
        if struct.unpack_from("<Q", data, o0)[0] != 0:   # first block A_start == 0
            continue
        r = walk(o0)
        if r is None:
            continue
        initial, blocks = r
        total = sum(k for _b, _c, _a, k in blocks)
        if total == 0:
            samples = np.zeros(0, dtype=np.uint64)
        else:
            # Each block's cumsum, offset by its A_start, written straight into one uint64
            # output: the chain check guarantees A_start is the sum of every earlier delta,
            # so this is the cumsum of the whole run. Drop the last (gap to capture end);
            # a[:-1] of a contiguous array stays contiguous.
            out = np.empty(total, dtype=np.uint64)
            pos = 0
            for body, cnt, a, k in blocks:
                if k:
                    dst = out[pos:pos + k]
                    # A uint64 VIEW, not dtype=: cumsum casting int64 makes a full copy.
                    # Every delta is >= 1 (walk() checked the sums), so the bits agree.
                    run = decode_run(body, cnt, checked=True)
                    np.cumsum(run.view(np.uint64), out=dst)
                    del run
                    dst += np.uint64(a)
                    pos += k
            samples = out[:-1]
        return DigitalChannelSamples(bool(initial), np.ascontiguousarray(samples))
    raise MalformedSaleaeV3(label, "no self-consistent transition-block chain found "
                                   "(unrecognised version-3 layout)")



def peek_version(data: bytes) -> int:
    """Return the <SALEAE> format version of a digital blob (raises on bad magic)."""
    if len(data) < 12 or data[:8] != _MAGIC:
        raise ValueError("not a Saleae binary capture (bad identifier)")
    return int(struct.unpack_from("<I", data, 8)[0])


def _find_v3_chain_start(data: bytes, label: str = "") -> tuple:
    """``(byte offset of the first transition block, chain end sample)`` for a v3 blob.

    The metadata header is variable-length, so the chain is located by the same
    self-consistency scan :func:`parse_channel_v3` uses, over the same candidate
    offsets (inside the first `V3_CHAIN_START_SCAN` bytes) — but this only VALIDATES
    the chain by walking block headers (checking they tile the blob exactly and
    chain A_start == prev B_end); it never decodes a delta run. That makes it
    cheap enough to call before deciding what to decode.

    The chain end is the last block's ``B_end`` — the position of the trailing
    delta that the whole-blob reader drops (the gap to capture end), which a
    windowed read must drop too."""
    n = len(data)
    for o0 in range(16, min(n, V3_CHAIN_START_SCAN) - _V3_BLOCK_HEADER + 1):
        if struct.unpack_from("<Q", data, o0)[0] != 0:      # first block A_start == 0
            continue
        o, prev_b, ok = o0, None, True
        while o + _V3_BLOCK_HEADER <= n:
            a, b = struct.unpack_from("<QQ", data, o)
            cnt = struct.unpack_from("<Q", data, o + _V3_BYTECOUNT_OFF)[0]
            body = o + _V3_BLOCK_HEADER
            if b < a or body + cnt > n or (prev_b is None and a != 0) or \
               (prev_b is not None and a != prev_b):
                ok = False
                break
            if prev_b is None and b == a and body + cnt != n:
                ok = False                                  # spurious empty-block match
                break
            prev_b, o = b, body + cnt
        if ok and o == n:
            return o0, int(prev_b if prev_b is not None else 0)
    raise MalformedSaleaeV3(label, "no self-consistent transition-block chain found "
                                   "(unrecognised version-3 layout)")


def parse_channel_v3_window(data: bytes, start_sample: int, end_sample: int,
                            label: str = "") -> DigitalChannelSamples:
    """Decode only the transitions strictly inside ``(start_sample, end_sample)`` of a
    v3 blob. An edge AT `start_sample` is folded into `initial_state`, as in
    :meth:`Capture.subcapture`.

    Each v3 block header carries absolute ``A_start``/``B_end`` sample bounds, so the
    walk stops at the first block that starts at or past the window's end, and a block
    that ends at or before the window's start contributes only its delta count.

    Transition samples are returned on the ORIGINAL capture timeline (not rebased);
    ``initial_state`` is the line level at ``start_sample``, from the parity of the
    transitions at or before it. A block before the window is fully decoded to count
    its deltas (counting bytes below 0x80 over-counts, see the loop). Every decoded
    block is checked as :func:`parse_channel_v3` checks it: no delta code too long for
    int64, and deltas summing to ``B_end - A_start``; a block that fails raises
    :class:`MalformedSaleaeV3`.
    """
    if len(data) < 16 or data[:8] != _MAGIC:
        raise ValueError(f"{label or 'data'}: not a Saleae binary capture (bad identifier)")
    version, ctype = struct.unpack_from("<II", data, 8)
    if version not in _V3_LIKE_VERSIONS:          # 3 and 4 are the same format (see the set)
        raise UnsupportedSaleaeVersion(version, label)
    if ctype != _V3_TYPE_DIGITAL:
        raise ValueError(f"{label or 'data'}: not a digital channel (type={ctype})")
    s0, s1 = int(start_sample), int(end_sample)
    if s1 <= s0:
        raise ValueError(f"parse_channel_v3_window: end ({s1}) must be past start ({s0})")

    n = len(data)
    o, chain_end = _find_v3_chain_start(data, label)
    initial = struct.unpack_from("<H", data, o + _V3_LEVEL_OFF)[0] & 1
    # The final delta is the gap from the last real transition to capture end; the
    # whole-blob reader drops it (cumsum(...)[:-1]) and a window must agree, or a
    # window covering the capture end grows a phantom edge at chain_end.
    limit = min(int(s1), chain_end)
    buf8 = np.frombuffer(data, np.uint8)
    parity = 0                       # transitions at or before the window start, mod 2
    kept: list = []

    def _checked_run(a: int, b: int, body: int, cnt: int) -> np.ndarray:
        """One block's int64 deltas, refused unless they decode within int64 and sum
        to the block header's span."""
        if _v3_has_overlong_code(buf8[body:body + cnt]):
            raise MalformedSaleaeV3(label, f"block at byte {body - _V3_BLOCK_HEADER} holds "
                                           "a delta code too long for int64")
        run = (_native_v3(buf8, body, cnt) if _native_v3 is not None
               else _v3_decode_deltas_np(data, body, cnt))
        if int(run.sum()) != b - a:
            raise MalformedSaleaeV3(label, f"block at byte {body - _V3_BLOCK_HEADER} "
                                           f"decodes to {int(run.sum())} samples, its "
                                           f"header says {b - a}")
        return run

    while o + _V3_BLOCK_HEADER <= n:
        a, b = struct.unpack_from("<QQ", data, o)
        cnt = struct.unpack_from("<Q", data, o + _V3_BYTECOUNT_OFF)[0]
        body = o + _V3_BLOCK_HEADER
        if b < a or body + cnt > n:
            break
        if b <= s0:
            # Entirely before the window: we only need how many transitions it holds,
            # to track the line level's parity.
            #
            # Do NOT count bytes < 0x80 as terminators. A multi-byte code whose MSB
            # digit is itself < 0x40 encodes as [digit+0x40, ..., terminal] — e.g.
            # encode_v3_delta(65) == b"\x40\x40" — so BOTH bytes are < 0x80 and the
            # naive count over-reports; an odd over-count inverts the window's opening
            # level. Decode the run properly instead; it is the same
            # vectorised path the overlap branch uses, and a skipped block's deltas are
            # discarded immediately rather than cumsummed.
            if cnt:
                run = _checked_run(a, b, body, cnt)
                ntr = int(run.size)
                if b >= chain_end:
                    ntr -= 1        # last block's final delta is the phantom end gap
                parity ^= ntr & 1
        elif a >= limit:
            break                                            # past the window: done
        else:
            # Overlaps: decode this block and keep the in-window transitions.
            run = _checked_run(a, b, body, cnt)
            abs_s = np.cumsum(run, dtype=np.int64) + int(a)
            if b >= chain_end and abs_s.size:
                abs_s = abs_s[:-1]                           # drop the phantom end gap
            before = int(np.count_nonzero(abs_s <= s0))      # left of window -> level
            parity ^= before & 1
            sel = abs_s[(abs_s > s0) & (abs_s < limit)]
            if sel.size:
                kept.append(sel)
        o = body + cnt

    samples = (np.concatenate(kept) if len(kept) > 1
               else (kept[0] if kept else np.zeros(0, dtype=np.int64)))
    return DigitalChannelSamples(bool(initial ^ parity),
                                 np.ascontiguousarray(samples, dtype=np.uint64))


def build_channel_v3(initial_state: bool, transition_samples: np.ndarray,
                     chunk_size: int = 0, version: int = _V3_VERSION) -> bytes:
    """Serialise a version-3 digital blob (inverse of :func:`parse_channel_v3`).

    Writes a minimal header (`<SALEAE>` · version · type) followed by the
    transition-block chain that `parse_channel_v3` reconstructs. The first block's
    `level` carries the initial state. Deltas are `[s0] + diff(s) + [1]` (the
    trailing 1-sample gap to capture end that the reader drops). `chunk_size > 0`
    splits the deltas across multiple blocks (exercising the chain walk); 0 uses a
    single block. Any delta magnitude is supported (variable-length codec).

    For a Logic-2-openable blob (full metadata header + block-count preamble) use
    :func:`build_logic2_channel_v3` instead."""
    s = np.ascontiguousarray(transition_samples, dtype=np.uint64).astype(np.int64)
    out = bytearray(_MAGIC)
    out += struct.pack("<II", int(version), _V3_TYPE_DIGITAL)

    if s.size == 0:                               # no transitions: one empty block
        out += struct.pack("<QQHQ", 0, 0, int(bool(initial_state)), 0)
        return bytes(out)
    if np.any(np.diff(s) <= 0) or s[0] < 0:
        raise ValueError("build_channel_v3: transition samples must be strictly increasing and >= 0")
    deltas = np.empty(s.size + 1, dtype=np.int64)
    deltas[0] = s[0]                              # gap from capture start to first edge
    deltas[1:s.size] = np.diff(s)
    deltas[s.size] = 1                            # trailing gap to capture end (dropped on read)
    out += _emit_v3_blocks(int(bool(initial_state)), deltas, chunk_size,
                           per_block_level=False)
    return bytes(out)


# Transitions per Logic-2 version-3 block. Logic itself chunks the transition stream
# by time (per capture-buffer flush); this writer chunks at a fixed count. The only
# invariant Logic needs is that the preamble's block-count field matches the number of
# blocks written.
V3_LOGIC_BLOCK_DELTAS = 4096


def build_logic2_channel_v3(initial_state: bool, transition_samples: np.ndarray, *,
                            sample_rate_hz: float, unix_ms: int, frac_ms: float,
                            capture_end: int,
                            block_deltas: int = V3_LOGIC_BLOCK_DELTAS,
                            version: int = _V3_VERSION) -> bytes:
    """Build one Logic-2-openable version-3 <SALEAE> digital blob (full metadata
    header + block chain). Unlike :func:`build_channel_v3` (minimal header, internal
    round-trip), this writes the header Logic requires:

      `<SALEAE>` · version · type · u8 flag=1 · f64 sample_rate · u64 unix_ms ·
      f64 frac_ms · 10-byte preamble `[u8 flag=0, u64 block_count×256, u8=0]`

    then the block chain (first block at offset 51). Logic reads block_count =
    u64 // 256; a 0/one-giant-block count stalls it at "Preparing session".

    Transitions are the channel's absolute edge samples (strictly increasing). The
    gap stream `[s0] + diff(s) + [capture_end - s_last]` (trailing gap to the shared
    capture end, dropped on read) is chunked into blocks of `block_deltas`. Each
    block's `level` is the true line state at its first delta (`init ^ (start & 1)`);
    the reader only reads block 0's level, but Logic renders from each. An edge at
    sample 0 (no >=1 gap from capture start) is folded into the initial level. An
    empty channel gets one empty block carrying just the initial state."""
    s = np.ascontiguousarray(transition_samples, dtype=np.uint64).astype(np.int64)
    init = int(bool(initial_state))
    # An edge at sample 0 has no gap from capture start (a v3 delta must be >= 1), so
    # fold it into the initial level: start at the post-edge level with no edge at 0.
    if s.size and s[0] == 0:
        init ^= 1
        s = s[1:]

    if s.size == 0:                                   # empty channel: one empty block
        deltas = np.zeros(0, dtype=np.int64)
    else:
        if np.any(np.diff(s) <= 0) or s[0] < 0:
            raise ValueError("build_logic2_channel_v3: transition samples must be strictly increasing and >= 0")
        if capture_end <= int(s[-1]):
            raise ValueError("build_logic2_channel_v3: capture_end must be past the last transition")
        deltas = np.empty(s.size + 1, dtype=np.int64)
        deltas[0] = s[0]                              # gap from capture start to first edge
        deltas[1:s.size] = np.diff(s)
        deltas[s.size] = int(capture_end) - int(s[-1])   # trailing gap to shared capture end

    nblocks = len(list(range(0, deltas.size, block_deltas))) or 1

    out = bytearray(_MAGIC)
    out += struct.pack("<II", int(version), _V3_TYPE_DIGITAL)
    # Fixed metadata: constant flag, sample rate, capture wall-clock time.
    out += struct.pack("<B", 1)
    out += struct.pack("<d", float(sample_rate_hz))
    out += struct.pack("<Q", int(unix_ms))
    out += struct.pack("<d", float(frac_ms))
    # 10-byte preamble [u8 flag=0, u64 block-count×256, u8=0], then the first block.
    out += struct.pack("<BQB", 0, nblocks * 256, 0)
    out += _emit_v3_blocks(init, deltas, block_deltas, per_block_level=True)
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
    held = (len(data) - _HEADER.size) // 8
    if n > held:
        raise ValueError(f"{label or 'data'}: truncated (the header counts {n:,} "
                         f"transitions, the file holds {held:,})")
    times = np.frombuffer(data, dtype="<f8", count=n, offset=_HEADER.size)
    return DigitalChannel(bool(initial), np.ascontiguousarray(times), begin, end)


def read_channel(path: str) -> DigitalChannel:
    with open(path, "rb") as f:
        return parse_channel(f.read(), path)


def transition_count(path: str) -> int:
    """Read just the version-0 <SALEAE> header and return its transition count
    (cheap: no need to load the whole edge array). Used to tell the forwarded
    clock (toggles every UI -> most transitions) from the data line. Returns -1
    if the file isn't a readable version-0 digital blob.

    The count is capped at the f64 times the file's size can hold, so a truncated
    file or a damaged count field is counted, and priced, by what is there;
    :func:`parse_channel` then refuses it as truncated."""
    try:
        with open(path, "rb") as f:
            head = f.read(_HEADER.size)
            size = os.fstat(f.fileno()).st_size
        if len(head) < _HEADER.size or head[:8] != _MAGIC:
            return -1
        _, version, ctype, _, _, _, n = _HEADER.unpack(head)
        if version != 0 or ctype != _TYPE_DIGITAL:
            return -1
        return min(int(n), (size - _HEADER.size) // 8)
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
        raw = f.read(k * 8)
        return np.frombuffer(raw, dtype="<f8", count=len(raw) // 8)


def infer_sample_rate(paths, sample: int = 1 << 18) -> int:
    """Infer the capture sample rate from version-0 binaries, which store
    transition times in seconds but no rate. Every time is a whole number of sample
    periods, so the rate is the grid the first `sample` times of each channel sit on
    (`timebase.rate_from_times`). Returns 0 if it can't be inferred."""
    from .timebase import rate_from_times
    merged = []
    for p in paths:
        t = _read_times_sample(p, sample)
        if t.size:
            merged.append(t)
    if not merged:
        return 0
    return rate_from_times(np.concatenate(merged))


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


_TIMES_CHUNK = 1 << 20        # edges per conversion block (bounds transient float64 memory)


def _times_to_samples(times_s: np.ndarray, sample_rate_hz: int,
                      origin_s: float = 0.0) -> np.ndarray:
    """Convert absolute transition times (seconds) to sample indices, measured
    from `origin_s`. Logic captures start at a negative (pre-trigger) time, so the
    origin must be subtracted or the cast to uint64 wraps negatives to garbage.

    Converts `_TIMES_CHUNK` edges at a time so the float64 temporaries stay bounded
    (the same chunked scheme as saleae_sal._times_to_edges). Per-element arithmetic
    matches ``np.rint((times - origin) * rate).astype(uint64)``; origin_s is <= every
    time, so no value is negative before the cast."""
    out = np.empty(times_s.size, dtype=np.uint64)
    for i in range(0, times_s.size, _TIMES_CHUNK):
        b = times_s[i:i + _TIMES_CHUNK].astype(np.float64)   # writable copy of this chunk
        b -= origin_s
        b *= sample_rate_hz
        np.rint(b, out=b)
        out[i:i + _TIMES_CHUNK] = b.astype(np.uint64)
    return out


# Peak bytes per transition of `load_capture`: the file's f64 times, read whole and kept
# until the pair is built (8), and the uint64 edge array built from them (8).
_PAIR_BYTES_PER_EDGE = 16
# Bytes per transition of one `_TIMES_CHUNK` step of `_times_to_samples`: the float64
# copy of the chunk and its uint64 cast. Measured with tracemalloc within these figures:
# 16.0 per edge plus 16 MB on a 3M + 1M transition pair, 26 per edge on 300k + 100k.
_CHUNK_BYTES_PER_EDGE = 16
# The two headers, the Capture, the small per-call arrays and the interpreter's own
# allocations during a load, which differ by platform (about 130 KB on Windows).
_PAIR_FIXED_BYTES = 1 << 20


def predict_peak_bytes(clock_path: str, data_path: str) -> int:
    """Predicted peak of :func:`load_capture` on the pair, from the two headers alone.
    Each count is :func:`transition_count`'s, capped at what the file holds. `read_channel`
    reads a file whole, so the bytes past what its count accounts for (a low or zero count)
    are charged too, and a file whose header is not a version-0 digital one is charged its
    size, which `read_channel` reads before refusing it."""
    counts = []
    cost = _PAIR_FIXED_BYTES
    for p in (clock_path, data_path):
        n = transition_count(p)
        size = os.path.getsize(p)
        if n < 0:
            cost += size
            n = 0
        else:
            cost += max(0, size - _HEADER.size - 8 * n)
        counts.append(n)
    return (cost + sum(counts) * _PAIR_BYTES_PER_EDGE
            + min(max(counts), _TIMES_CHUNK) * _CHUNK_BYTES_PER_EDGE)


class BinTooLargeError(MemoryError):
    """Opening this .bin pair is predicted to exhaust memory.

    Raised BEFORE either file is read, like the .sal guard. A .bin pair is read
    whole, so the remedies are a shorter export or more RAM."""

    def __init__(self, paths, predicted: int, budget: int):
        self.paths = tuple(paths)
        self.predicted = int(predicted)
        self.budget = int(budget)
        MemoryError.__init__(
            self,
            f"{' + '.join(self.paths)}: the pair would need roughly "
            f"{predicted / 1e9:.1f} GB, but only {budget / 1e9:.1f} GB is budgeted. "
            f"Export a shorter capture, free memory, or pass max_bytes=0 to override.")


# Transition times checked against the sample grid (`_grid_notes`).
_GRID_CHECK = 1 << 16
# Farthest a transition time may sit from the sample grid, in samples, before the
# rounding is reported.
_GRID_OFF_SAMPLES = 0.25


def _descending_steps(times: np.ndarray) -> int:
    """Places where `times` steps back, checked `_TIMES_CHUNK` steps at a time."""
    n = 0
    for i in range(0, max(0, times.size - 1), _TIMES_CHUNK):
        j = min(i + _TIMES_CHUNK, times.size - 1)
        n += int(np.count_nonzero(times[i + 1:j + 1] < times[i:j]))
    return n


def _repeats(edges: np.ndarray) -> int:
    """Edges on the same sample as the edge before, `_TIMES_CHUNK` at a time."""
    n = 0
    for i in range(0, max(0, edges.size - 1), _TIMES_CHUNK):
        j = min(i + _TIMES_CHUNK, edges.size - 1)
        n += int(np.count_nonzero(edges[i + 1:j + 1] == edges[i:j]))
    return n


def _pair_notes(clock_path: str, data_path: str, clk: DigitalChannel, dat: DigitalChannel,
                rate: int, origin: float) -> list:
    """The import warnings of one `load_capture` that need the times: [(source, message)],
    the source a file's base name or the pair's. A channel whose times step back is
    sorted in place of its `times_s`."""
    out = []
    for path, ch in ((clock_path, clk), (data_path, dat)):
        src = import_notes.source_name(path)
        extra = os.path.getsize(path) - _HEADER.size - 8 * ch.times_s.size
        if extra > 0:
            out.append((src, f"{extra:,} byte{'s' if extra != 1 else ''} past the "
                             f"{ch.times_s.size:,} transitions the header counts "
                             f"{'are' if extra != 1 else 'is'} ignored"))
        back = _descending_steps(ch.times_s)
        if back:
            ch.times_s = np.sort(ch.times_s, kind="stable")
            out.append((src, f"the transition times step back at {back:,} "
                             f"place{'s' if back != 1 else ''}; they are sorted into "
                             f"time order"))
        head = ch.times_s[:_GRID_CHECK]
        if head.size and rate > 0:
            pos = (head.astype(np.float64) - origin) * rate
            off = float(np.max(np.abs(pos - np.rint(pos))))
            if off > _GRID_OFF_SAMPLES:
                out.append((src, f"the transition times do not sit on the {rate:,} Hz "
                                 f"sample grid (up to {off:.2f} of a sample off in the "
                                 f"first {head.size:,}); each is rounded to the nearest "
                                 f"sample"))
    period = 1.0 / rate if rate > 0 else 0.0
    if abs(clk.begin_time - dat.begin_time) > period:
        out.append((f"{import_notes.source_name(clock_path)} + "
                    f"{import_notes.source_name(data_path)}",
                    f"the two files start at different times ({clk.begin_time:.9g} s and "
                    f"{dat.begin_time:.9g} s); both are read on one timeline from the "
                    f"earlier start"))
    return out


def load_capture(clock_path: str, data_path: str, sample_rate_hz: int,
                 max_bytes: int | None = None) -> Capture:
    """Read a clock + data channel pair into a :class:`Capture`.

    `max_bytes` follows the .sal convention: None uses the shared `memory_budget()`, a
    positive value overrides it, and 0 opts out. Before either file is read,
    :func:`predict_peak_bytes` is compared with that budget and
    :class:`BinTooLargeError` is raised when it is over.

    Import warnings (import_notes) report bytes past the transitions a header counts,
    times out of order (sorted), times off the sample grid, transitions that land on
    one sample, and two files that start at different times."""
    if max_bytes != 0:
        from .saleae_sal import memory_budget  # ONE definition of the budget
        predicted = predict_peak_bytes(clock_path, data_path)
        budget = memory_budget(max_bytes)
        if predicted > budget:
            raise BinTooLargeError((clock_path, data_path), predicted, budget)
    clk = read_channel(clock_path)
    dat = read_channel(data_path)
    # Both channels share one timeline; pick the earliest point (capture start,
    # which Logic records as a negative pre-trigger time) as sample 0.
    origin = min(clk.begin_time, dat.begin_time,
                 float(np.min(clk.times_s)) if clk.times_s.size else 0.0,
                 float(np.min(dat.times_s)) if dat.times_s.size else 0.0)
    notes = _pair_notes(clock_path, data_path, clk, dat, int(sample_rate_hz), origin)
    cap = Capture(
        clock_edges=_times_to_samples(clk.times_s, sample_rate_hz, origin),
        data_edges=_times_to_samples(dat.times_s, sample_rate_hz, origin),
        initial_clock=clk.initial_state,
        initial_data=dat.initial_state,
        sample_rate_hz=sample_rate_hz,
    )
    for path, edges in ((clock_path, cap.clock_edges), (data_path, cap.data_edges)):
        same = _repeats(edges)
        if same:
            notes.append((import_notes.source_name(path),
                          f"{same:,} transition{'s' if same != 1 else ''} land on the "
                          f"same sample as the one before at {int(sample_rate_hz):,} "
                          f"Hz (the rate is coarser than the transitions)"))
    for source, message in notes:
        import_notes.warn(source, message)
    return cap


def write_capture(capture: Capture, clock_path: str, data_path: str) -> None:
    """Write a :class:`Capture` out as a clock + data channel pair."""
    rate = capture.sample_rate_hz
    write_channel(clock_path, capture.initial_clock,
                  capture.clock_edges.astype("<f8") / rate)
    write_channel(data_path, capture.initial_data,
                  capture.data_edges.astype("<f8") / rate)
