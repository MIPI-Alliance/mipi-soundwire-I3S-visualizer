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
**validated to the sample against a Logic CSV export** (see the maintainers' .sal format notes)::

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

# The v3 delta codec is the hot loop on 100s-of-millions-of-edges .sal captures.
# Prefer the native decoder (swi3score.decode_saleae_v3_deltas); fall back to the
# vectorised-numpy _v3_decode_deltas_np when the native core is absent (it stays a
# self-contained, bit-identical pure-Python+numpy reader). Both match decode_v3_deltas.
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
# Public aliases for the v3 codec surface, so exporters (sal_export, saleae_sal)
# don't reach into these privates. The codec + block-chain layout lives here (one
# owner); callers pick the header (minimal round-trip vs full Logic-2).
SALEAE_MAGIC = _MAGIC
V3_VERSION = _V3_VERSION
V3_TYPE_DIGITAL = _V3_TYPE_DIGITAL
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
    ×`_chunk`, not the whole 100s-of-millions-of-edges body. Bit-identical to the
    scalar decoder (verified by round-trip)."""
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
    # One uint8 view of the whole blob (zero-copy) for the native per-block decode.
    buf8 = np.frombuffer(data, np.uint8) if _native_v3 is not None else None

    def decode_run(body, cnt):
        """Decode one block's delta run to an int64 array — native if available."""
        if _native_v3 is not None:
            return _native_v3(buf8, body, cnt)
        return _v3_decode_deltas_np(data, body, cnt)

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
            run = decode_run(body, cnt)                   # int64 array (native or numpy)
            if int(run.sum()) != (b - a):
                return None
            deltas.append(run)
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
        if not deltas or all(d.size == 0 for d in deltas):
            samples = np.zeros(0, dtype=np.uint64)
        else:
            # cumsum the concatenated per-block deltas; drop the last (gap to capture
            # end). cumsum straight into uint64 (deltas are positive) avoids a second
            # full-size int64→uint64 copy; a[:-1] of a contiguous array stays contiguous.
            alld = deltas[0] if len(deltas) == 1 else np.concatenate(deltas)
            samples = np.cumsum(alld, dtype=np.uint64)[:-1]
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
    self-consistency scan :func:`parse_channel_v3` uses — but this only VALIDATES
    the chain by walking block headers (checking they tile the blob exactly and
    chain A_start == prev B_end); it never decodes a delta run. That makes it
    cheap enough to call before deciding what to decode.

    The chain end is the last block's ``B_end`` — the position of the trailing
    delta that the whole-blob reader drops (the gap to capture end), which a
    windowed read must drop too."""
    n = len(data)
    for o0 in range(16, n - _V3_BLOCK_HEADER + 1):
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
    """Decode only the transitions in ``[start_sample, end_sample)`` of a v3 blob.

    The whole-blob :func:`parse_channel_v3` cumsums every delta in the file — on a
    1.5 bn-edge channel that is a 12 GB uint64 array, which is what exhausts RAM on
    a long capture. Each v3 block header carries absolute ``A_start``/``B_end``
    sample bounds, so a block that does not overlap the window can be SKIPPED by
    byte arithmetic without decoding its delta run at all. Only overlapping blocks
    are decoded, so cost scales with the window, not the capture.

    Transition samples are returned on the ORIGINAL capture timeline (not rebased);
    ``initial_state`` is the line level at ``start_sample``, derived from the parity
    of the transitions preceding it — for a skipped block that is just the parity of
    its delta COUNT, which the codec gives us without cumsum (one varint terminator
    per delta), so the level stays exact across skipped regions.
    """
    if len(data) < 16 or data[:8] != _MAGIC:
        raise ValueError(f"{label or 'data'}: not a Saleae binary capture (bad identifier)")
    version, ctype = struct.unpack_from("<II", data, 8)
    if version != _V3_VERSION:
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
    buf8 = np.frombuffer(data, np.uint8) if _native_v3 is not None else None
    parity = 0                       # transitions strictly before the window, mod 2
    kept: list = []

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
            # naive count over-reports. When the over-count is odd the parity flips and
            # the window opens with the line level INVERTED: samples correct, polarity
            # wrong, no error raised. Decode the run properly instead; it is the same
            # vectorised path the overlap branch uses, and a skipped block's deltas are
            # discarded immediately rather than cumsummed.
            if cnt:
                run = (_native_v3(buf8, body, cnt) if _native_v3 is not None
                       else _v3_decode_deltas_np(data, body, cnt))
                ntr = int(run.size)
                if b >= chain_end:
                    ntr -= 1        # last block's final delta is the phantom end gap
                parity ^= ntr & 1
        elif a >= limit:
            break                                            # past the window: done
        else:
            # Overlaps: decode this block and keep the in-window transitions.
            run = (_native_v3(buf8, body, cnt) if _native_v3 is not None
                   else _v3_decode_deltas_np(data, body, cnt))
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
                     chunk_size: int = 0) -> bytes:
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
    out += _emit_v3_blocks(int(bool(initial_state)), deltas, chunk_size,
                           per_block_level=False)
    return bytes(out)


# Transitions per Logic-2 version-3 block. Real Logic captures chunk the transition
# stream into many blocks (a busy channel had 818 / 412 blocks, never one giant
# block) — the boundaries are time-based (per capture-buffer flush), so the per-block
# count varies. We chunk at a fixed count instead; the only invariant Logic needs is
# that the preamble's block-count field matches the number of blocks written.
V3_LOGIC_BLOCK_DELTAS = 4096


def build_logic2_channel_v3(initial_state: bool, transition_samples: np.ndarray, *,
                            sample_rate_hz: float, unix_ms: int, frac_ms: float,
                            capture_end: int,
                            block_deltas: int = V3_LOGIC_BLOCK_DELTAS) -> bytes:
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
    out += struct.pack("<II", _V3_VERSION, _V3_TYPE_DIGITAL)
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


_TIMES_CHUNK = 1 << 20        # edges per conversion block (bounds transient float64 memory)


def _times_to_samples(times_s: np.ndarray, sample_rate_hz: int,
                      origin_s: float = 0.0) -> np.ndarray:
    """Convert absolute transition times (seconds) to sample indices, measured
    from `origin_s`. Logic captures start at a negative (pre-trigger) time, so the
    origin must be subtracted or the cast to uint64 wraps negatives to garbage.

    Converts a block at a time: the naive whole-array
    ``np.rint((times - origin) * rate).astype(uint64)`` allocates three full float64
    temporaries (~24 bytes/edge), a multi-GB OOM on a 100M+-edge .bin — the same
    chunked scheme as saleae_sal._times_to_edges. Bit-identical (per-element arithmetic
    unchanged; origin_s is <= every time, so no value is negative before the cast)."""
    out = np.empty(times_s.size, dtype=np.uint64)
    for i in range(0, times_s.size, _TIMES_CHUNK):
        b = times_s[i:i + _TIMES_CHUNK].astype(np.float64)   # writable copy of this chunk
        b -= origin_s
        b *= sample_rate_hz
        np.rint(b, out=b)
        out[i:i + _TIMES_CHUNK] = b.astype(np.uint64)
    return out


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
