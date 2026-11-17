"""Reader for the IEEE-1364 Value Change Dump (``.vcd``) format.

A VCD is what digital simulators (Verilog/VHDL, and most SPICE-adjacent digital
flows) emit: a ``$timescale`` header, ``$var`` declarations mapping short
identifier codes to signal names, then a time-ordered stream of value changes
(``#<t>`` advances simulation time; ``0!`` / ``1!`` toggle scalar signal ``!``).

SWI3S Studio imports a VCD as a capture by picking the forwarded-clock signal and
the data signal (the same two-wire model as a .sal / digital CSV) and turning
their transitions into the edge arrays the decode core walks. The ``$timescale``
is the sample clock: one timescale tick = one sample, so a value change at ``#t``
lands exactly on sample ``t`` and the sample rate is ``1 / timescale``.

Only 1-bit (scalar) signals are offered as clock/data candidates — the SWI3S PHY
is a two-wire clock+data link, so vector/real signals are never clock or data.
"""
from __future__ import annotations

import codecs
import re
from array import array
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .. import import_notes
from .capture import Capture
from .timebase import repeats, require_distinct, same_sample_note

# The value-change section is read by the native core's VcdScanner, or by the NumPy reader
# below when the loaded module has no VcdScanner. Both give the same edges, notes and errors.
try:
    import swi3score as _score
    _NATIVE = getattr(_score, "VcdScanner", None)
except Exception:                                  # pragma: no cover - native optional
    _NATIVE = None

# $timescale magnitudes -> seconds. VCD allows values 1/10/100 of each unit.
_TIMESCALE_UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}
_TIMESCALE_RE = re.compile(r"^\s*([0-9]{1,9})\s*(s|ms|us|ns|ps|fs)\s*$")


@dataclass
class VcdSignal:
    ident: str                   # VCD identifier code (e.g. "!", "#", "aB")
    name: str                    # reference name and bit-select; the scope is not part of it
    width: int                   # bits (1 = scalar; only scalars are clock/data)


@dataclass
class VcdInfo:
    timescale_s: float
    sample_rate_hz: int
    signals: List[VcdSignal] = field(default_factory=list)
    timescale_text: str = ""     # the $timescale as written ("1 ns")

    @property
    def scalar_signals(self) -> List[VcdSignal]:
        """The 1-bit signals — the only clock/data candidates."""
        return [s for s in self.signals if s.width == 1]


# A $timescale is at most two words and a $var at most six (type, width, ident, reference
# and a bit-select), so a declaration is read up to this many words; a longer one is not one.
_MAX_DECL_TOKENS = 16
# The longest text of a bad $timescale shown in an error.
_SHOWN_CHARS = 60


def _parse_timescale(text: str) -> float:
    """'1ns' / '10 ps' / '1 us' -> seconds. Raises ValueError if unparseable."""
    m = _TIMESCALE_RE.match(text.strip())
    if not m:
        shown = text if len(text) <= _SHOWN_CHARS else text[:_SHOWN_CHARS] + "…"
        raise ValueError(f"unrecognised $timescale {shown!r}")
    return int(m.group(1)) * _TIMESCALE_UNITS[m.group(2)]


# Characters read from the file per step of `_tokens`, and the longest token it yields. A
# dump written on one line is read a step at a time, so the memory held is the step plus
# one token whatever the line length; a longer token is not a VCD.
_READ_CHARS = 1 << 14
_MAX_TOKEN_CHARS = 1 << 16


def _tokens(f) -> Iterator[str]:
    """Whitespace-delimited token stream over a text file (VCD is token-oriented; a
    declaration or a vector value can span or share lines), read `_READ_CHARS` at a
    time. A token longer than `_MAX_TOKEN_CHARS` raises ValueError naming the file."""
    carry = ""                                  # the token the last step ended inside
    while True:
        chunk = f.read(_READ_CHARS)
        if not chunk:
            break
        chunk = carry + chunk
        parts = chunk.split()
        carry = "" if (not parts or chunk[-1].isspace()) else parts.pop()
        longest = max(map(len, parts), default=0)
        if max(longest, len(carry)) > _MAX_TOKEN_CHARS:
            raise ValueError(f"{getattr(f, 'name', 'VCD')}: not a VCD (a token is longer "
                             f"than {_MAX_TOKEN_CHARS:,} characters)")
        yield from parts
    if carry:
        yield carry


def _declaration(toks) -> Tuple[List[str], str]:
    """``(words, state)`` of the declaration after a `$timescale` / `$var` keyword, read
    through its `$end`, at most `_MAX_DECL_TOKENS` words of it. `state` is "closed" (the
    `$end` was read), "open" (the file ended first) or "long" (more than that many words
    came first; the rest of the block is left unread)."""
    words: List[str] = []
    for x in toks:
        if x == "$end":
            return words, "closed"
        if len(words) >= _MAX_DECL_TOKENS:
            return words, "long"
        words.append(x)
    return words, "open"


def read_info(path: str) -> VcdInfo:
    """Parse a VCD's header ($timescale + $var declarations). Stops at
    $enddefinitions, so this is cheap even on a multi-GB dump."""
    timescale_s: Optional[float] = None
    timescale_text = ""
    signals: List[VcdSignal] = []
    seen: set = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        toks = _tokens(f)
        for tok in toks:
            if tok == "$timescale":
                parts, state = _declaration(toks)
                if state == "open":
                    raise ValueError(f"{path}: the $timescale block has no $end")
                try:
                    if state == "long":
                        raise ValueError(f"unrecognised $timescale (more than "
                                         f"{_MAX_DECL_TOKENS} words)")
                    timescale_s = _parse_timescale("".join(parts))
                except ValueError as e:
                    raise ValueError(f"{path}: {e}") from None
                timescale_text = " ".join(parts)
            elif tok == "$var":
                # $var <type> <width> <ident> <reference> [<bit-select>] $end
                decl, state = _declaration(toks)
                if state == "long":
                    for x in toks:               # not a declaration: read past its $end
                        if x == "$end":
                            break
                elif len(decl) >= 4:
                    # A non-decimal width -> 0 so it's NOT offered as a 1-bit clock/
                    # data candidate (a real scalar always declares width "1").
                    width = (int(decl[1]) if decl[1].isascii() and decl[1].isdigit()
                             and len(decl[1]) <= 9 else 0)
                    ident = decl[2]
                    name = decl[3] + ("".join(decl[4:]) if len(decl) > 4 else "")
                    if ident not in seen:          # first name wins for aliased idents
                        seen.add(ident)
                        signals.append(VcdSignal(ident, name, width))
            elif tok == "$enddefinitions":
                break
            elif tok.startswith("$") and tok != "$end":
                # $comment / $date / $version / $scope / $upscope: skip the whole
                # block to its $end so free text (which can contain the literal
                # tokens $var / $timescale / $enddefinitions) can't corrupt parsing.
                for x in toks:
                    if x == "$end":
                        break
    if timescale_s is None or timescale_s <= 0:
        raise ValueError(f"{path}: missing or invalid $timescale")
    rate = int(round(1.0 / timescale_s))
    if rate < 1:                             # the rate rounds to under 1 Hz
        raise ValueError(f"{path}: the $timescale of {timescale_text} gives a sample rate "
                         f"below 1 Hz")
    return VcdInfo(timescale_s=timescale_s, sample_rate_hz=rate, signals=signals,
                   timescale_text=timescale_text)


class _StreamNotes:
    """What the value-change scan passed over for the idents in `watch`: x/z values after a
    signal's first 0/1 one and outside a $dumpoff block (`xz`: ident -> [count, first
    time]), time stamps earlier than the one before (`back`: [count, first time]), and
    bare "#" tokens with no time (`bare`), the `$` block that reached the end of the file
    without its `$end` (`open_block`: its keyword, or None), and, per watched ident, the
    value changes that land on the time of an earlier change of the same signal (`same`:
    ident -> [count, first time]), and the value changes of a watched signal that are a
    vector or real value that is not a one-bit 0/1 (`unread`: ident -> [count, first
    time]). Each per-ident dict holds its idents in the order of their first record."""

    def __init__(self, watch) -> None:
        self.watch = set(watch)
        self.xz: Dict[str, list] = {}
        self.unread: Dict[str, list] = {}
        self.back = [0, 0]
        self.bare = 0
        self.open_block: Optional[str] = None
        self.header_open = False
        self.same: Dict[str, list] = {}


def _skip_block(toks, notes: Optional[_StreamNotes], keyword: str) -> None:
    """Consume tokens through the next `$end`. A block that reaches the end of the file
    first is recorded in `notes.open_block` (it consumed the rest of the file, so no later
    block can be recorded)."""
    for x in toks:
        if x == "$end":
            return
    if notes is not None:
        notes.open_block = keyword


def _time_stamp(path: str, tok: str) -> int:
    """The tick count of a `#t` token: a whole number in 0 .. 2**63 - 1 (a tick is a
    sample, and a sample index stays below 2**63), else ValueError naming the file and
    the token."""
    text = tok[1:]
    digits = text.lstrip("0")
    if text.isascii() and text.isdigit() and len(digits) <= 20:
        t = int(digits or "0")
        if t < (1 << 63):
            return t
    shown = tok if len(tok) <= 40 else tok[:40] + "…"
    raise ValueError(f"{path}: {shown!r} is not a time stamp (a '#' followed by a whole, "
                     f"non-negative number of ticks below 2**63)")


# ---- the NumPy reader of the value-change section ----------------------------------------
#
# Without the native scanner, the value changes are tokenised and classified with NumPy a
# step of `_READ_BYTES` at a time. The bytes are those of the text `_tokens` reads: UTF-8
# decoded with replacement, and any non-ASCII whitespace turned into a space, so the tokens
# are the UTF-8 encodings of the same str tokens. The header before `$enddefinitions` is
# read a token at a time.

# Bytes read from the file per step of `_ByteTokens`. A step's arrays hold a few dozen
# bytes per token, so the step bounds what the scan holds besides the edges.
_READ_BYTES = 1 << 15

# The ASCII characters str.split() splits on (str.isspace()), as a byte lookup table.
_WS_LUT = np.zeros(256, dtype=bool)
_WS_LUT[[c for c in range(128) if chr(c).isspace()]] = True
# The non-ASCII characters str.isspace() accepts (none lies above U+3000), mapped to a space.
_WIDE_SPACE = {c: 0x20 for c in range(0x80, 0x3001) if chr(c).isspace()}

# Token classes, by first byte.
# _NONE marks a token that is not read for itself (a value token or its ident).
_NONE, _OTHER, _HASH, _DOLLAR, _LVL0, _LVL1, _VEC_B, _VEC_R = range(8)
_CLASS_LUT = np.full(256, _OTHER, dtype=np.uint8)
_CLASS_LUT[ord("#")] = _HASH
_CLASS_LUT[ord("$")] = _DOLLAR
_CLASS_LUT[ord("0")] = _LVL0
_CLASS_LUT[ord("1")] = _LVL1
_CLASS_LUT[[ord("b"), ord("B")]] = _VEC_B
_CLASS_LUT[[ord("r"), ord("R")]] = _VEC_R
_XZ_LUT = np.zeros(256, dtype=bool)
_XZ_LUT[[ord(c) for c in "xXzZ"]] = True
# Bytes in a UTF-8 lead byte's character, by lead byte (a continuation byte counts 1).
_CHAR_BYTES_LUT = np.ones(256, dtype=np.int64)
_CHAR_BYTES_LUT[0xC0:0xE0] = 2
_CHAR_BYTES_LUT[0xE0:0xF0] = 3
_CHAR_BYTES_LUT[0xF0:0x100] = 4

# A `#t` token whose digits are at most this many is read by the array path (10**18 fits
# an int64); a longer one goes through `_time_stamp`.
_FAST_TIME_DIGITS = 18
# An ident of up to 7 bytes is keyed by its bytes and length packed into a uint64; a longer
# one by a number from `_IdentKeys`, at or above 2**63.
_PACKED_IDENT_BYTES = 7
_LONG_KEY_BASE = 1 << 63


# Bytes handed to the native scanner per step; the step is the only part of the file held.
_NATIVE_READ_BYTES = 1 << 18


def _ident_bytes(ident: str) -> bytes:
    """The UTF-8 of an ident as a token holds it. A lone surrogate, which no token holds,
    is kept as its own bytes so the ident matches nothing."""
    return ident.encode("utf-8", "surrogatepass")


def _native_read(path: str, scanner) -> Optional[tuple]:
    """Feed the file to a native `VcdScanner` a step of `_NATIVE_READ_BYTES` at a time,
    up to its end or the scanner's stop. A malformed time stamp, an over-long token and an
    allocation failure raise as the NumPy reader raises them; the budget error
    ``("too_large", edges, time)`` is returned for the caller to raise, else None."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_NATIVE_READ_BYTES)
            if not chunk:
                scanner.finish()
                break
            if scanner.feed(chunk):
                break
    err = scanner.error()
    if err is None or err[0] == "too_large":
        return err
    if err[0] == "stamp":
        _time_stamp(path, err[1].decode("utf-8"))
        raise AssertionError(f"{path}: the native reader refused the time stamp "
                             f"{err[1]!r}")                     # pragma: no cover
    if err[0] == "too_long":
        raise _too_long(path)
    raise MemoryError(f"{path}: out of memory reading the value changes")


def _too_long(path: str) -> ValueError:
    return ValueError(f"{path}: not a VCD (a token is longer than {_MAX_TOKEN_CHARS:,} "
                      f"characters)")


class _ByteTokens:
    """The tokens of a VCD opened in binary mode, a step of `_READ_BYTES` at a time.

    Each step holds `data` (bytes) and the token bounds `starts`/`ends` of the complete
    tokens in it; `i` is the first token not yet read. Iterating yields the tokens from `i`
    on as str, reading further steps as needed. A token longer than `_MAX_TOKEN_CHARS`
    characters ends a step's tokens (`too_long`), and reaching it raises ValueError naming
    the file. `hold(token)` hands a token back to be the first of the next step."""

    def __init__(self, f, path: str) -> None:
        self.f = f
        self.path = path
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.carry = b""                    # the token the last step ended inside
        self.held = b""
        self.done = False                   # the file has been read to its end
        self.data = b""
        self.starts = np.zeros(0, dtype=np.int64)
        self.ends = np.zeros(0, dtype=np.int64)
        self.i = 0
        self.n = 0
        self.too_long = False
        self.base = 0                       # tokens of earlier steps (stream position of 0)

    def hold(self, token: bytes) -> None:
        self.held = token

    def step(self) -> bool:
        """Read the next step; False once the file has no more tokens."""
        if self.done:
            return False
        self.base += self.n
        raw = self.f.read(_READ_BYTES)
        if not raw:
            self.done = True
        if raw and raw.isascii() and not self.dec.getstate()[0]:
            text = raw
        else:
            s = self.dec.decode(raw, final=not raw)
            if not s.isascii():
                s = s.translate(_WIDE_SPACE)
            text = s.encode("utf-8")
        data = (self.held + b" " if self.held else b"") + self.carry + text
        self.held = b""
        a = np.frombuffer(data, dtype=np.uint8)
        bounds = np.flatnonzero(np.diff(~_WS_LUT[a], prepend=False, append=False))
        starts, ends = bounds[0::2], bounds[1::2]
        del bounds
        self.carry = b""
        if not self.done and ends.size and ends[-1] == len(data):
            self.carry = data[int(starts[-1]):]
            starts, ends = starts[:-1], ends[:-1]
        self.too_long = False
        over = np.flatnonzero(ends - starts > _MAX_TOKEN_CHARS)
        for k in over.tolist():             # bytes over the limit: count the characters
            if len(data[int(starts[k]):int(ends[k])].decode("utf-8")) > _MAX_TOKEN_CHARS:
                starts, ends = starts[:k], ends[:k]
                self.too_long = True
                break
        if (not self.too_long and len(self.carry) > _MAX_TOKEN_CHARS
                and len(self.carry.decode("utf-8")) > _MAX_TOKEN_CHARS):
            self.too_long = True
        self.data, self.starts, self.ends = data, starts, ends
        self.i, self.n = 0, int(starts.size)
        return bool(self.n or self.too_long or not self.done)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        while self.i >= self.n:
            if self.too_long:
                raise _too_long(self.path)
            if not self.step():
                raise StopIteration
        k = self.i
        self.i += 1
        return self.data[int(self.starts[k]):int(self.ends[k])].decode("utf-8")


class _IdentKeys:
    """uint64 keys of ident byte strings: up to `_PACKED_IDENT_BYTES` bytes packed with the
    length in the top byte, a longer ident numbered from `_LONG_KEY_BASE` as first met."""

    def __init__(self) -> None:
        self.long: Dict[bytes, int] = {}
        self.names: Dict[int, bytes] = {}

    def of(self, ident: bytes) -> int:
        if len(ident) <= _PACKED_IDENT_BYTES:
            return (len(ident) << 56) | int.from_bytes(ident, "little")
        key = self.long.get(ident)
        if key is None:
            key = _LONG_KEY_BASE + len(self.long)
            self.long[ident] = key
            self.names[key] = ident
        return key

    def ident(self, key: int) -> str:
        if key >= _LONG_KEY_BASE:
            return self.names[key].decode("utf-8")
        return int(key & ((1 << 56) - 1)).to_bytes(key >> 56, "little").decode("utf-8")

    def keys(self, data: bytes, a: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """The keys of the idents ``data[lo:hi]``, elementwise."""
        n = hi - lo
        key = n.astype(np.uint64) << np.uint64(56)
        last = max(a.size - 1, 0)
        for k in range(_PACKED_IDENT_BYTES):
            m = n > k
            if not m.any():
                break
            byte = a[np.minimum(lo + k, last)].astype(np.uint64) << np.uint64(8 * k)
            key |= np.where(m, byte, np.uint64(0))
        for j in np.flatnonzero(n > _PACKED_IDENT_BYTES).tolist():
            key[j] = self.of(data[int(lo[j]):int(hi[j])])
        return key


def _equals(a: np.ndarray, starts: np.ndarray, ends: np.ndarray, word: bytes) -> np.ndarray:
    """Indices of the tokens that are exactly `word`."""
    idx = np.flatnonzero(ends - starts == len(word))
    for k, ch in enumerate(word):
        idx = idx[a[starts[idx] + k] == ch]
    return idx


class _Targets:
    """The idents a load watches: an ident's id is its index in `idents`, any other is -1."""

    def __init__(self, idents: List[str], one_bit: set) -> None:
        self.words = [_ident_bytes(x) for x in idents]
        self.one = np.array([x in one_bit for x in idents] + [False], dtype=bool)

    def ids(self, data: bytes, a: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        out = np.full(lo.size, -1, dtype=np.int64)
        n = hi - lo
        for j, w in enumerate(self.words):
            idx = np.flatnonzero(n == len(w))
            for k, ch in enumerate(w):
                idx = idx[a[lo[idx] + k] == ch]
            out[idx] = j
        return out

    def one_bit(self, ids: np.ndarray) -> np.ndarray:
        return self.one[ids]                               # id -1 reads the False at the end

    @staticmethod
    def keep(ids: np.ndarray) -> np.ndarray:
        return ids >= 0


class _AllIdents:
    """Every ident, by its `_IdentKeys` key."""

    def __init__(self, keys: _IdentKeys, one_bit: set) -> None:
        self.keys = keys
        self.one = np.unique(np.array([keys.of(x.encode("utf-8")) for x in one_bit],
                                      dtype=np.uint64))

    def ids(self, data: bytes, a: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        return self.keys.keys(data, a, lo, hi)

    def one_bit(self, ids: np.ndarray) -> np.ndarray:
        return _member(ids, self.one)

    @staticmethod
    def keep(ids: np.ndarray) -> np.ndarray:
        return np.ones(ids.size, dtype=bool)


class _Segment:
    """The events of one run of tokens the scan reads as value changes, in token order.

    `ch_*` are the scalar 0/1 value changes (stream position, ident id, level); with notes,
    `uk_*` are the x/z values outside $dumpoff, and `ur_*` the vector or real changes outside
    $dumpoff whose value is not all x/z. `times(idx)` is the time at token indices `idx`
    (the `*_idx` arrays)."""

    def __init__(self, scan: "_ValueScan", n: int, stamps: np.ndarray, tvals: np.ndarray,
                 pos0: int) -> None:
        self._t0 = scan.t
        self._n = n
        self._stamps = stamps
        self._tvals = tvals
        self.pos0 = pos0
        self._filled: Optional[np.ndarray] = None
        empty_i = np.zeros(0, dtype=np.int64)
        self.ch_idx = self.uk_idx = self.ur_idx = empty_i
        self.ch_id = self.uk_id = self.ur_id = empty_i
        self.ch_lvl = np.zeros(0, dtype=np.int8)

    def times(self, idx: np.ndarray) -> np.ndarray:
        if self._tvals.size == 0:
            return np.full(idx.size, self._t0, dtype=np.int64)
        if idx.size * 8 < self._n:
            j = np.searchsorted(self._stamps, idx) - 1
            return np.where(j >= 0, self._tvals[np.maximum(j, 0)], np.int64(self._t0))
        if self._filled is None:
            at = np.full(self._n, -1, dtype=np.int64)
            at[self._stamps] = np.arange(self._stamps.size)
            np.maximum.accumulate(at, out=at)
            self._filled = np.where(at >= 0, self._tvals[np.maximum(at, 0)],
                                    np.int64(self._t0))
        return self._filled[idx]


class _ValueScan:
    """The value-change section of a VCD, read step by step into `_Segment`s.

    The rules are the VCD's token grammar as this reader applies it, in stream order:
    ``#t`` sets the time (a bare ``#`` is counted and ignored, a malformed one raises);
    a token starting ``b``/``B``/``r``/``R`` is a value whose ident is the NEXT token,
    whatever it is; ``0x``/``1x`` is a scalar change of ident ``x``; ``$comment`` skips to
    the next ``$end``; ``$dumpoff`` opens a $dumpoff block and ``$end`` closes it; any other
    ``$`` token is a marker; any other token is an x/z (or unknown) value of the ident after
    its first character. A one-bit ``b0``/``b1`` (any case) of an ident declared one bit
    wide is a scalar change, and ``bx``/``bz`` of one an x/z value.

    `idents` (`_Targets` or `_AllIdents`) gives each event's ident id. `sink(segment)`
    receives each run of events and returns True to stop the scan."""

    def __init__(self, path: str, toks: _ByteTokens, idents,
                 notes: Optional[_StreamNotes]) -> None:
        self.path = path
        self.toks = toks
        self.idents = idents
        self.notes = notes
        self.t = 0
        self.in_dumpoff = False
        self.in_comment = False

    def run(self, sink) -> None:
        tk = self.toks
        while True:
            if tk.i < tk.n and self._step(sink):
                return
            if tk.too_long:
                raise _too_long(self.path)
            if not tk.step():
                break
        if self.in_comment and self.notes is not None:
            self.notes.open_block = "$comment"

    def _step(self, sink) -> bool:
        """Read the tokens of the current step from `toks.i` on; True if `sink` stopped."""
        tk = self.toks
        data, a = tk.data, np.frombuffer(tk.data, dtype=np.uint8)
        S, E = tk.starts[tk.i:tk.n], tk.ends[tk.i:tk.n]
        pos0 = tk.base + tk.i
        tk.i = tk.n
        m = int(S.size)
        cls = _CLASS_LUT[a[S]]
        vec = cls >= _VEC_B
        # The start of each run of value tokens: within a run, the 1st, 3rd, ... are values
        # and each takes the token after it as its ident.
        run0 = np.arange(m)
        run0[1:][vec[:-1]] = 0
        np.maximum.accumulate(run0, out=run0)
        dollar = np.flatnonzero(cls == _DOLLAR)
        comments = dollar[_equals(a, S[dollar], E[dollar], b"$comment")]
        block_ends = dollar[_equals(a, S[dollar], E[dollar], b"$end")]
        k = 0
        while k < m:
            if self.in_comment:
                j = int(np.searchsorted(block_ends, k))
                if j == block_ends.size:
                    return False
                k = int(block_ends[j]) + 1
                self.in_comment = False
                continue
            # The next $comment that is not the ident of a value token.
            c = m
            for cc in comments[np.searchsorted(comments, k):].tolist():
                p = cc - 1
                if not (p >= k and vec[p] and ((p - max(int(run0[p]), k)) & 1) == 0):
                    c = cc
                    break
            if self._segment(sink, data, a, S, E, cls, vec, run0, k, c, pos0):
                return True
            if c == m:
                return False
            self.in_comment = True
            k = c + 1
        return False

    def _segment(self, sink, data, a, S, E, cls, vec, run0, k0, k1, pos0) -> bool:
        """Read tokens ``[k0, k1)`` of the step, none of them inside a $comment."""
        n = k1 - k0
        if n <= 0:
            return False
        valtok = vec[k0:k1].copy()
        if k0:
            off = np.arange(k0, k1) - np.maximum(run0[k0:k1], k0)
        else:
            off = np.arange(n) - run0[:k1]
        valtok &= (off & 1) == 0
        del off
        tk = self.toks
        if valtok[-1]:
            # A value token whose ident is not in this step: read it with the next one.
            if not tk.done and not tk.too_long:
                tk.hold(data[int(S[k1 - 1]):int(E[k1 - 1])])
            valtok[-1] = False
        # A token read for itself: neither a value token nor the ident after one.
        c = cls[k0:k1].copy()
        c[valtok] = _NONE
        c[1:][valtok[:-1]] = _NONE
        s, e = S[k0:k1], E[k0:k1]

        # Time stamps.
        hashes = np.flatnonzero(c == _HASH)
        hl = e[hashes] - s[hashes]
        bare = hashes[hl == 1]
        stamps = hashes[hl > 1]
        tvals, bad, err = self._stamps(data, a, s[stamps], e[stamps])
        del hashes, hl
        if bad is not None:                          # the scan stops at a malformed one
            n = int(stamps[bad])
            stamps, tvals = stamps[:bad], tvals[:bad]
            bare = bare[bare < n]
            valtok, c, s, e = valtok[:n], c[:n], s[:n], e[:n]
        notes = self.notes
        if notes is not None:
            notes.bare += int(bare.size)
            if tvals.size:
                prev = np.empty(tvals.size, dtype=np.int64)
                prev[0] = self.t
                prev[1:] = tvals[:-1]
                back = np.flatnonzero(tvals < prev)
                if back.size:
                    if not notes.back[0]:
                        notes.back[1] = int(tvals[back[0]])
                    notes.back[0] += int(back.size)
        seg = _Segment(self, n, stamps, tvals, pos0 + k0)

        idents = self.idents
        # A scalar change: the ident follows the level.
        sc = np.flatnonzero((c == _LVL0) | (c == _LVL1))
        sc_id = idents.ids(data, a, s[sc] + 1, e[sc])
        # A value token: its ident is the next token; a one-bit b0/b1/bx/bz of a one-bit
        # ident is read as a scalar 0/1 or an x/z.
        vals = np.flatnonzero(valtok)
        v_id = idents.ids(data, a, s[vals + 1], e[vals + 1])
        vs, ve = s[vals], e[vals]
        second = a[np.minimum(vs + 1, a.size - 1)]
        short_b = (ve - vs == 2) & (cls[k0:k1][vals] == _VEC_B)
        one = idents.one_bit(v_id) & short_b
        v01 = one & ((second == ord("0")) | (second == ord("1")))
        vxz = one & _XZ_LUT[second]
        keep_sc = idents.keep(sc_id)
        keep_v = idents.keep(v_id) & v01
        ch = np.concatenate((sc[keep_sc], vals[keep_v]))
        ch_id = np.concatenate((sc_id[keep_sc], v_id[keep_v]))
        ch_lvl = np.concatenate((c[sc[keep_sc]] == _LVL1, second[keep_v] == ord("1")))
        if vals.size and ch.size:
            order = np.argsort(ch, kind="stable")
            ch, ch_id, ch_lvl = ch[order], ch_id[order], ch_lvl[order]
        seg.ch_idx, seg.ch_id, seg.ch_lvl = ch, ch_id, ch_lvl.astype(np.int8)

        if notes is not None:
            self._notes(seg, data, a, c, s, e, vals, v_id, v01 | vxz, vxz)
        if tvals.size:
            self.t = int(tvals[-1])
        if sink(seg):
            return True
        if err is not None:
            raise err
        return False

    def _notes(self, seg, data, a, c, s, e, vals, v_id, read, vxz) -> None:
        """The x/z values and the unread vector/real values of the watched idents."""
        idents = self.idents
        markers = np.flatnonzero(c == _DOLLAR)
        offs = markers[_equals(a, s[markers], e[markers], b"$dumpoff")]
        ons = markers[_equals(a, s[markers], e[markers], b"$end")]
        marks = np.concatenate((offs, ons))
        mark_off = np.concatenate((np.ones(offs.size, dtype=bool),
                                   np.zeros(ons.size, dtype=bool)))
        order = np.argsort(marks)
        marks, mark_off = marks[order], mark_off[order]

        def outside(idx):
            if not marks.size:
                return np.full(idx.size, not self.in_dumpoff, dtype=bool)
            j = np.searchsorted(marks, idx) - 1
            return ~np.where(j >= 0, mark_off[np.maximum(j, 0)], self.in_dumpoff)

        # An x/z or other token: the ident follows its first character.
        other = np.flatnonzero(c == _OTHER)
        o_id = idents.ids(data, a, s[other] + _CHAR_BYTES_LUT[a[s[other]]], e[other])
        uk = np.concatenate((other, vals[vxz]))
        uk_id = np.concatenate((o_id, v_id[vxz]))
        keep = idents.keep(uk_id)
        uk, uk_id = uk[keep], uk_id[keep]
        order = np.argsort(uk, kind="stable")
        uk, uk_id = uk[order], uk_id[order]
        keep = outside(uk)
        seg.uk_idx, seg.uk_id = uk[keep], uk_id[keep]

        ur, ur_id = vals[~read], v_id[~read]
        keep = idents.keep(ur_id)
        ur, ur_id = ur[keep], ur_id[keep]
        keep = outside(ur)
        ur, ur_id = ur[keep], ur_id[keep]
        keep = np.array([not _all_xz(data, int(s[i]) + 1, int(e[i])) for i in ur.tolist()],
                        dtype=bool)
        seg.ur_idx, seg.ur_id = ur[keep], ur_id[keep]
        if marks.size:
            self.in_dumpoff = bool(mark_off[-1])

    def _stamps(self, data: bytes, a: np.ndarray, s: np.ndarray, e: np.ndarray):
        """``(times, first malformed index or None, its error)`` of the `#t` tokens
        ``data[s:e]``. Up to `_FAST_TIME_DIGITS` digits are read as a digit matrix, right
        aligned; a longer token goes through `_time_stamp`."""
        tv = np.zeros(s.size, dtype=np.int64)
        ok = np.ones(s.size, dtype=bool)
        if s.size == 0:
            return tv, None, None
        ln = e - s
        fast = ln <= _FAST_TIME_DIGITS + 1
        fi = np.flatnonzero(fast)
        if fi.size:
            fs, fe = s[fi], e[fi]
            w = int((fe - fs).max()) - 1
            pos = fe[:, None] + np.arange(-w, 0)[None, :]
            inside = pos > fs[:, None]
            d = a[np.where(inside, pos, 0)].astype(np.int64) - ord("0")
            d[~inside] = 0
            ok[fi] = ((d >= 0) & (d <= 9)).all(axis=1)
            tv[fi] = d @ (np.int64(10) ** np.arange(w - 1, -1, -1, dtype=np.int64))
        for i in np.flatnonzero(~fast).tolist():
            try:
                tv[i] = _time_stamp(self.path, data[int(s[i]):int(e[i])].decode("utf-8"))
            except ValueError:
                ok[i] = False
        bad = np.flatnonzero(~ok)
        if bad.size == 0:
            return tv, None, None
        i = int(bad[0])
        try:
            _time_stamp(self.path, data[int(s[i]):int(e[i])].decode("utf-8"))
        except ValueError as err:
            return tv, i, err
        raise AssertionError("unreachable")              # pragma: no cover


def _member(keys: np.ndarray, sorted_keys: np.ndarray) -> np.ndarray:
    """Whether each of `keys` is in the sorted array `sorted_keys`."""
    if sorted_keys.size == 0 or keys.size == 0:
        return np.zeros(keys.size, dtype=bool)
    j = np.minimum(np.searchsorted(sorted_keys, keys), sorted_keys.size - 1)
    return sorted_keys[j] == keys


def _all_xz(data: bytes, lo: int, hi: int) -> bool:
    """Whether ``data[lo:hi]`` is all x/z digits (any case); an empty value is."""
    return not data[lo:hi].translate(None, b"xXzZ")


def _scan(path: str, notes: Optional[_StreamNotes], make_idents, sink) -> None:
    """Read a VCD's header, then hand its value changes to `sink` (see `_ValueScan`).

    The header is read a token at a time up to `$enddefinitions`: `$var` declares an
    ident (the first declaration of an ident decides; width "1" makes it one bit wide),
    and every other `$` block is skipped to its `$end`. `make_idents(one_bit)` gives the
    scan's ident ids from the set of one-bit idents. A header that never ends sets
    `notes.header_open`."""
    one_bit: set = set()
    declared: set = set()
    with open(path, "rb") as f:
        toks = _ByteTokens(f, path)
        in_defs = True
        for tok in toks:
            if tok == "$enddefinitions":
                _skip_block(toks, notes, tok)
                in_defs = False
                break
            if tok == "$var":
                decl, state = _declaration(toks)
                if state == "long":
                    _skip_block(toks, notes, tok)    # not a declaration
                elif len(decl) >= 4 and decl[2] not in declared:
                    declared.add(decl[2])
                    if decl[1] == "1":
                        one_bit.add(decl[2])
            elif tok.startswith("$") and tok != "$end":
                _skip_block(toks, notes, tok)       # header blocks (incl. $comment)
        if in_defs:
            if notes is not None:
                notes.header_open = True
            return
        _ValueScan(path, toks, make_idents(one_bit), notes).run(sink)


def transition_counts_until(path: str, idents=None, max_changes: Optional[int] = None
                            ) -> Tuple[Dict[str, int], bool, int]:
    """Scalar transitions per identifier (a signal's first value is its initial level, not
    a transition), reading at most `max_changes` value changes: ``(counts, truncated,
    last time read)``. With `idents`, only those are counted. A bounded prefix gives the
    RANKING of the signals (a forwarded clock toggles every UI) without parsing a whole
    large dump on the GUI thread; a truncated count covers ``[0, last time]``, which is
    what lets a caller scale it to the whole dump. `counts` holds the idents in the order
    of their first transition."""
    if _NATIVE is not None:
        scanner = _NATIVE.counts(-1 if max_changes is None else
                                 min(max(int(max_changes), 0), (1 << 63) - 1))
        _native_read(path, scanner)
        pairs, cut, t_last = scanner.counts_result()
        want = None if idents is None else {x for x in idents if isinstance(x, str)}
        found = ((ident.decode("utf-8"), n) for ident, n in pairs)
        return ({k: n for k, n in found if want is None or k in want}, bool(cut),
                int(t_last))
    keys = _IdentKeys()
    want = (None if idents is None else
            np.unique(np.array([keys.of(_ident_bytes(x)) for x in idents
                                if isinstance(x, str)], dtype=np.uint64)))
    counts: Dict[int, int] = {}
    last: Dict[int, int] = {}
    seen = 0
    t_seen = 0
    cut = False

    def sink(seg: _Segment) -> bool:
        nonlocal seen, t_seen, cut
        key, lvl, idx = seg.ch_id, seg.ch_lvl, seg.ch_idx
        if max_changes is not None and seen + key.size > max_changes:
            take = max(int(max_changes) - seen, 0)
            key, lvl, idx = key[:take], lvl[:take], idx[:take]
            cut = True
        seen += int(key.size)
        if idx.size:
            t_seen = int(seg.times(idx[-1:])[0])
        if want is not None:
            keep = _member(key, want)
            key, lvl = key[keep], lvl[keep]
        if key.size:
            _count_transitions(key, lvl, last, counts)
        return cut

    _scan(path, None, lambda one_bit: _AllIdents(keys, one_bit), sink)
    return {keys.ident(k): v for k, v in counts.items()}, cut, t_seen


def _count_transitions(key: np.ndarray, lvl: np.ndarray, last: Dict[int, int],
                       counts: Dict[int, int]) -> None:
    """Add the transitions of the changes `key`/`lvl` (stream order) to `counts`, with
    `last` the level each key last changed to; a key's first change is its initial level.
    A key enters `counts` at its first transition."""
    order = np.argsort(key, kind="stable")
    k_s, l_s = key[order], lvl[order]
    head = np.ones(k_s.size, dtype=bool)
    head[1:] = k_s[1:] != k_s[:-1]
    heads = np.flatnonzero(head)
    prev = np.zeros(l_s.size, dtype=np.int8)
    prev[1:] = l_s[:-1]
    known = ~head
    hkeys = k_s[heads].tolist()
    for h, kk in zip(heads.tolist(), hkeys):
        if kk in last:
            prev[h], known[h] = last[kk], True
    moved = known & (l_s != prev)
    tails = np.append(heads[1:], k_s.size) - 1
    n_moved = np.add.reduceat(moved.astype(np.int64), heads).tolist()
    first_moved = np.minimum.reduceat(np.where(moved, order, np.iinfo(np.int64).max),
                                      heads).tolist()
    fresh = []
    for kk, nm, fm, lv in zip(hkeys, n_moved, first_moved, l_s[tails].tolist()):
        last[kk] = lv
        if nm:
            if kk in counts:
                counts[kk] += nm
            else:
                fresh.append((fm, kk, nm))
    for _fm, kk, nm in sorted(fresh):
        counts[kk] = nm


# Peak bytes per edge of `load_capture`: one uint64 in a growing buffer (the native
# scanner's, or an array('Q')), whose over-allocation is at most an eighth. The buffers become
# the Capture's arrays without a copy. The step read from the file (`_NATIVE_READ_BYTES`, or
# the NumPy reader's arrays for `_READ_BYTES`) is a fixed amount beside them.
BYTES_PER_EDGE = 9
# Edges read between checks of the running total against the budget.
_CHECK_EVERY = 1 << 16


class VcdTooLargeError(MemoryError):
    """Loading this VCD's two signals passed the memory budget.

    A VCD has no index, so its edge count is not known until it is read: the loader
    stops as soon as the edges held pass the budget, before memory is exhausted.
    """

    def __init__(self, path: str, edges: int, held: int, budget: int, last_time: int):
        self.path = path
        self.edges = int(edges)
        self.held = int(held)
        self.budget = int(budget)
        self.last_time = int(last_time)
        MemoryError.__init__(
            self,
            f"{path}: the two signals reached {edges:,} edges ({held / 1e9:.1f} GB) by "
            f"#{last_time}, past the {budget / 1e9:.1f} GB budgeted for one load. Dump a "
            f"shorter run, free memory, or pass max_bytes=0 to override.")


def _report(path: str, info: VcdInfo, notes: _StreamNotes, clock_ident: str,
            data_ident: str, clock_missing: bool, data_missing: bool) -> None:
    """Raise the import warnings of one `load_capture` (import_notes)."""
    src = import_notes.source_name(path)
    exact = 1.0 / info.timescale_s
    if abs(exact - info.sample_rate_hz) > max(1e-3, 1e-12 * exact):
        import_notes.warn(src, f"the $timescale of {info.timescale_text} is not a whole "
                               f"number of hertz; the sample rate is rounded to "
                               f"{info.sample_rate_hz:,} Hz")
    names = {s.ident: s.name for s in info.signals}
    if notes.header_open:
        import_notes.warn(src, "the header has no $enddefinitions (the file may be "
                               "truncated); no value change was read, so the signals are "
                               "read as a constant 0")
    for ident, missing in ((clock_ident, clock_missing), (data_ident, data_missing)):
        if missing and not notes.header_open:
            import_notes.warn(src, f"signal {names.get(ident, ident)!r} has no 0/1 value "
                                   f"change; it is read as a constant 0")
    for ident, (count, first) in notes.xz.items():
        import_notes.warn(src, f"{count:,} x/z value{'s' if count != 1 else ''} on signal "
                               f"{names.get(ident, ident)!r} (first at #{first}) "
                               f"{'were' if count != 1 else 'was'} read as the level "
                               f"before {'them' if count != 1 else 'it'}")
    if notes.back[0]:
        n = notes.back[0]
        import_notes.warn(src, f"time goes backwards at {n:,} place{'s' if n != 1 else ''} "
                               f"(first to #{notes.back[1]}); the edges are sorted into "
                               f"time order")
    for ident, (count, first) in notes.unread.items():
        import_notes.warn(src, f"{count:,} vector or real value change"
                               f"{'s' if count != 1 else ''} on signal "
                               f"{names.get(ident, ident)!r} (first at #{first}) "
                               f"{'were' if count != 1 else 'was'} not read")
    for ident, (count, first) in notes.same.items():
        import_notes.warn(src, f"{count:,} value change{'s' if count != 1 else ''} on signal "
                               f"{names.get(ident, ident)!r} landed on the time of the "
                               f"change before (first at #{first}); each time step is "
                               f"read as the level it ends on")
    if notes.open_block:
        import_notes.warn(src, f"a {notes.open_block} block has no $end; the rest of the "
                               f"file was not read")
    if notes.bare:
        import_notes.warn(src, f"{notes.bare:,} \"#\" with no time "
                               f"{'were' if notes.bare != 1 else 'was'} ignored (the dump "
                               f"may be truncated)")



class _Line:
    """One watched signal's state across the scan: its level, its edges (array('Q')),
    and its notes ([count, first time, first stream position] each)."""

    def __init__(self, ident: str) -> None:
        self.ident = ident
        self.last: Optional[int] = None
        self.init = False
        self.first_pos: Optional[int] = None      # stream position of its first 0/1 value
        self.prev_t = -1                          # time of its latest transition
        self.run = 0                              # transitions so far at `prev_t`
        self.exact = False                        # its transitions once went back in time
        self.edges = array("Q")
        self.xz: Optional[list] = None
        self.unread: Optional[list] = None
        self.same: Optional[list] = None

    def edge_ops(self, tau: np.ndarray) -> np.ndarray:
        """Apply transitions at times `tau` (stream order) to `edges`, returning +1 for each
        that added an edge and -1 for each that removed one.

        A transition at the time of the transition before it removes the edge that one
        added if it is the last edge held, and otherwise adds one; any other transition
        adds an edge. While the transitions never go back in time, the transitions of one
        time step alternate add/remove, so the step keeps its edge when it holds an odd
        number of them; that is applied in bulk. After a step back in time the rule is
        applied one transition at a time."""
        m = int(tau.size)
        prev = np.empty(m, dtype=np.int64)
        prev[0] = self.prev_t
        prev[1:] = tau[:-1]
        if not self.exact and bool((tau < prev).any()):
            self.exact = True
        if self.exact:
            ops = np.empty(m, dtype=np.int8)
            e, pt = self.edges, self.prev_t
            for k, x in enumerate(tau.tolist()):
                if x == pt and e and e[-1] == x:
                    e.pop()
                    ops[k] = -1
                else:
                    e.append(x)
                    ops[k] = 1
                pt = x
            self.prev_t = pt
            return ops
        start = tau != prev
        ar = np.arange(m)
        head = np.maximum.accumulate(np.where(start, ar, -1))
        idx = np.where(head >= 0, ar - head, ar + self.run)     # place within its time step
        del ar, head
        ops = np.where(idx & 1, -1, 1).astype(np.int8)
        tail = np.empty(m, dtype=bool)
        tail[:-1] = start[1:]
        tail[-1] = True
        keeps = (idx[tail] & 1) == 0
        step_t = tau[tail]
        if not start[0]:                          # continues the step of the last call
            before = bool(self.run & 1)
            if before and not keeps[0]:
                self.edges.pop()
            elif keeps[0] and not before:
                self.edges.append(int(step_t[0]))
            keeps, step_t = keeps[1:], step_t[1:]
        self.edges.frombytes(np.ascontiguousarray(step_t[keeps], dtype=np.uint64).view(np.uint8))
        self.prev_t = int(tau[-1])
        self.run = int(idx[-1]) + 1
        return ops


def _note(rec: Optional[list], count: int, first_t: int, first_pos: int) -> list:
    """`rec` ([count, first time, first position]) with `count` more records."""
    if rec is None:
        return [count, first_t, first_pos]
    rec[0] += count
    return rec


def _ordered(lines, attr: str) -> Dict[str, list]:
    """A notes dict of the lines' `attr` records, in the order of their first record."""
    recs = [(getattr(ln, attr), ln.ident) for ln in lines if getattr(ln, attr) is not None]
    recs.sort(key=lambda r: r[0][2])
    return {ident: [rec[0], rec[1]] for rec, ident in recs}


def _native_capture(path: str, clock_ident: str, data_ident: str, budget: int,
                    notes: _StreamNotes) -> tuple:
    """The two signals read by the native scanner, ``((edges, initial level, has no 0/1
    value, edges ascending), ...)`` for the clock then the data, with `notes` filled in.
    Past `budget` (0: none), raises VcdTooLargeError."""
    assert _NATIVE is not None                        # load_capture reads with it only then
    scanner = _NATIVE.capture(_ident_bytes(clock_ident), _ident_bytes(data_ident),
                              max(min(budget, (1 << 63) - 1), -(1 << 63)), _CHECK_EVERY,
                              BYTES_PER_EDGE)
    err = _native_read(path, scanner)
    if err is not None:
        edges, t = int(err[1]), int(err[2])
        raise VcdTooLargeError(path, edges, edges * BYTES_PER_EDGE, budget, t)
    r = scanner.capture_result()
    idents = (clock_ident, data_ident)
    notes.xz, notes.unread, notes.same = (
        {idents[k]: [int(count), int(first)] for k, count, first in r[kind]}
        for kind in ("xz", "unread", "same"))
    notes.back = [int(x) for x in r["back"]]
    notes.bare = int(r["bare"])
    notes.open_block = None if r["open_block"] is None else r["open_block"].decode("utf-8")
    notes.header_open = bool(r["header_open"])
    return tuple((r[edges], bool(r["initial"][k]), bool(r["missing"][k]),
                  bool(r["ascending"][k]))
                 for k, edges in enumerate(("clock_edges", "data_edges")))


def _array_capture(path: str, clock_ident: str, data_ident: str, budget: int,
                   notes: _StreamNotes) -> tuple:
    """What `_native_capture` returns, read by the NumPy reader."""
    lines = (_Line(clock_ident), _Line(data_ident))
    next_check = _CHECK_EVERY

    def sink(seg: _Segment) -> bool:
        nonlocal next_check
        held_before = len(lines[0].edges) + len(lines[1].edges)
        op_pos, op_val, op_t = [], [], []
        for j, ln in enumerate(lines):
            u = seg.uk_id == j
            if u.any() and (ln.first_pos is not None or (seg.ch_id == j).any()):
                first = ln.first_pos
                if first is None:
                    first = int(seg.ch_idx[np.argmax(seg.ch_id == j)]) + seg.pos0
                upos = seg.uk_idx[u] + seg.pos0
                after = np.flatnonzero(upos > first)
                if after.size:
                    ln.xz = _note(ln.xz, int(after.size),
                                  int(seg.times(seg.uk_idx[u][after[:1]])[0]),
                                  int(upos[after[0]]))
            r = np.flatnonzero(seg.ur_id == j)
            if r.size:
                ln.unread = _note(ln.unread, int(r.size),
                                  int(seg.times(seg.ur_idx[r[:1]])[0]),
                                  int(seg.ur_idx[r[0]]) + seg.pos0)
            m = seg.ch_id == j
            if not m.any():
                continue
            idx, lvl = seg.ch_idx[m], seg.ch_lvl[m]
            if ln.first_pos is None:
                ln.first_pos = int(idx[0]) + seg.pos0
            if ln.last is None:
                ln.init, ln.last = bool(lvl[0]), int(lvl[0])
            prev = np.empty(lvl.size, dtype=np.int8)
            prev[0] = ln.last
            prev[1:] = lvl[:-1]
            moved = lvl != prev
            ln.last = int(lvl[-1])
            tidx = idx[moved]
            if not tidx.size:
                continue
            tau = seg.times(tidx)
            before = np.empty(tau.size, dtype=np.int64)
            before[0] = ln.prev_t
            before[1:] = tau[:-1]
            same = np.flatnonzero(tau == before)
            if same.size:
                ln.same = _note(ln.same, int(same.size), int(tau[same[0]]),
                                int(tidx[same[0]]) + seg.pos0)
            ops = ln.edge_ops(tau)
            if budget:
                op_pos.append(tidx)
                op_val.append(ops)
                op_t.append(tau)
        if budget and op_pos:
            order = np.argsort(np.concatenate(op_pos), kind="stable")
            held = held_before + np.cumsum(np.concatenate(op_val)[order], dtype=np.int64)
            times = np.concatenate(op_t)[order]
            k = 0
            while True:
                hit = np.flatnonzero(held[k:] >= next_check)
                if not hit.size:
                    break
                k += int(hit[0])
                edges = int(held[k])
                if edges * BYTES_PER_EDGE > budget:
                    raise VcdTooLargeError(path, edges, edges * BYTES_PER_EDGE, budget,
                                           int(times[k]))
                next_check += _CHECK_EVERY
        return False

    _scan(path, notes, lambda one_bit: _Targets([clock_ident, data_ident], one_bit), sink)
    notes.xz = _ordered(lines, "xz")
    notes.unread = _ordered(lines, "unread")
    notes.same = _ordered(lines, "same")
    clk_line, dat_line = lines
    # Zero-copy views of the buffers (the arrays stay alive through the views).
    clk = np.frombuffer(clk_line.edges, dtype=np.uint64) if len(clk_line.edges) else \
        np.zeros(0, dtype=np.uint64)
    dat = np.frombuffer(dat_line.edges, dtype=np.uint64) if len(dat_line.edges) else \
        np.zeros(0, dtype=np.uint64)
    # A line whose transitions never went back in time holds ascending edges.
    return ((clk, clk_line.init, clk_line.last is None, not clk_line.exact),
            (dat, dat_line.init, dat_line.last is None, not dat_line.exact))


def load_capture(path: str, clock_ident: str, data_ident: str,
                 auto_clock: bool = False, max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a VCD's clock + data scalar signals.

    Each value change at ``#t`` becomes a transition at sample ``t`` (the
    $timescale is the sample period), and a signal's first observed value is its
    initial level. Several changes of one signal at one ``#t`` make one transition
    (or none) to the level the step ends on, and are reported. With ``auto_clock`` the
    busier of the two signals is taken as the forwarded clock (it toggles every UI), so
    the caller needn't know which is which. A clock signal that is also the data signal
    raises ValueError naming the file.

    Edges are collected as uint64 in growing buffers (`BYTES_PER_EDGE`), and
    each time the edges held reach a multiple of `_CHECK_EVERY` the bytes held are
    compared with the shared `memory_budget()`; past it, :class:`VcdTooLargeError` is
    raised. `max_bytes` follows the .sal convention: None uses the computed budget, a
    positive value overrides it, and 0 opts out."""
    require_distinct(path, clock_ident, data_ident, "signal")
    info = read_info(path)
    rate = info.sample_rate_hz
    budget = 0
    if max_bytes != 0:
        from .saleae_sal import memory_budget  # ONE definition of the budget
        budget = memory_budget(max_bytes)
    notes = _StreamNotes((clock_ident, data_ident))
    read = _native_capture if _NATIVE is not None else _array_capture
    (clk, init_clk, clk_missing, clk_up), (dat, init_dat, dat_missing, dat_up) = read(
        path, clock_ident, data_ident, budget, notes)
    _report(path, info, notes, clock_ident, data_ident, clk_missing, dat_missing)
    # The forwarded clock toggles every UI -> the most edges. When the caller can't
    # say which is which, take the busier signal as the clock (same as .sal/.csv).
    if auto_clock and dat.size > clk.size:
        clk, dat = dat, clk
        init_clk, init_dat = init_dat, init_clk
        clk_up, dat_up = dat_up, clk_up
    # The decoder needs ascending edges: a dump whose time goes back is sorted.
    if not clk_up:
        clk.sort()
    if not dat_up:
        dat.sort()
    same = repeats(clk) + repeats(dat)          # edges the sort put on one sample
    if same:
        import_notes.warn(import_notes.source_name(path), same_sample_note(same, rate))
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=init_clk, initial_data=init_dat, sample_rate_hz=rate)
