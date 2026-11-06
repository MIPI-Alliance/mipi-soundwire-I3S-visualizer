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

import re
from array import array
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .. import import_notes
from .capture import Capture
from .timebase import repeats, require_distinct, same_sample_note

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

# The digits of a vector value that is all unknown or undriven (an x/z fill, not a change).
_UNKNOWN_DIGITS = frozenset("xz")


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
    """What `_value_changes` passed over for the idents in `watch`: x/z values after a
    signal's first 0/1 one and outside a $dumpoff block (`xz`: ident -> [count, first
    time]), time stamps earlier than the one before (`back`: [count, first time]), and
    bare "#" tokens with no time (`bare`), the `$` block that reached the end of the file
    without its `$end` (`open_block`: its keyword, or None), and, per watched ident, the
    value changes that land on the time of an earlier change of the same signal (`same`:
    ident -> [count, first time]), and the value changes of a watched signal that are a
    vector or real value that is not a one-bit 0/1 (`unread`: ident -> [count, first
    time])."""

    def __init__(self, watch) -> None:
        self.watch = set(watch)
        self.seen: set = set()
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


def _unknown(notes: Optional[_StreamNotes], ident: str, t: int, in_dumpoff: bool) -> None:
    """Count an x/z value of a watched signal that has had a 0/1 value, outside $dumpoff."""
    if notes is not None and not in_dumpoff and ident in notes.seen:
        rec = notes.xz.setdefault(ident, [0, t])
        rec[0] += 1


def _value_changes(path: str, notes: Optional[_StreamNotes] = None) -> Iterator[tuple]:
    """Yield (time, ident, level) for every scalar 0/1 value change in a VCD's
    value-change section. The single source of truth for the value stream, shared by
    load_capture and transition_counts. A signal declared one bit wide is read in both
    forms a dump writes it: ``1!`` and the one-bit vector ``b1 !`` (``b0``/``b1``, any
    case). It skips the header, x/z (undumped/unknown) values, other vector/real
    changes, and $comment blocks; the $dumpvars / $dumpon /
    $dumpoff / $dumpall markers pass through and their contained real 0/1 changes are
    yielded normally (the x-fills a $dumpoff emits for every signal are dropped, so
    they don't inject phantom edges). `notes` (a `_StreamNotes`) counts what was passed
    over for its watched idents. A malformed time stamp raises ValueError."""
    t = 0
    one_bit: set = set()                  # idents declared one bit wide
    declared: set = set()                 # the first declaration of an ident decides
    in_defs = True
    in_dumpoff = False
    with open(path, encoding="utf-8", errors="replace") as f:
        toks = _tokens(f)
        for tok in toks:
            if in_defs:
                if tok == "$enddefinitions":
                    _skip_block(toks, notes, tok)
                    in_defs = False
                elif tok == "$var":
                    decl, state = _declaration(toks)
                    if state == "long":
                        _skip_block(toks, notes, tok)    # not a declaration
                    elif len(decl) >= 4 and decl[2] not in declared:
                        declared.add(decl[2])
                        if decl[1] == "1":
                            one_bit.add(decl[2])
                elif tok.startswith("$") and tok != "$end":
                    _skip_block(toks, notes, tok)   # header blocks (incl. $comment)
                continue
            c = tok[0]
            if c == "#":
                if len(tok) > 1:              # ignore a stray bare "#" (truncated dump)
                    t_new = _time_stamp(path, tok)
                    if notes is not None and t_new < t:
                        if not notes.back[0]:
                            notes.back[1] = t_new
                        notes.back[0] += 1
                    t = t_new
                elif notes is not None:
                    notes.bare += 1
                continue
            if c in "bBrR":                   # vector/real change: value token then ident token
                ident = next(toks, None)
                if ident is None:
                    continue
                v = tok[1:].lower()
                if c in "bB" and ident in one_bit:
                    if v in ("0", "1"):
                        if notes is not None and ident in notes.watch:
                            notes.seen.add(ident)
                        yield t, ident, int(v)
                        continue
                    if v in ("x", "z"):
                        _unknown(notes, ident, t, in_dumpoff)
                        continue
                if (notes is not None and ident in notes.watch and not in_dumpoff
                        and not set(v) <= _UNKNOWN_DIGITS):
                    rec = notes.unread.setdefault(ident, [0, t])
                    rec[0] += 1
                continue
            if c == "$":
                if tok == "$comment":         # skip free text (may look like value changes)
                    _skip_block(toks, notes, tok)
                elif tok == "$dumpoff":
                    in_dumpoff = True
                elif tok == "$end":
                    in_dumpoff = False
                continue                      # $dumpvars/$dumpon/$dumpoff/$dumpall/$end: markers
            if c not in "01":                 # x / z / other: not a binary level change
                _unknown(notes, tok[1:], t, in_dumpoff)
                continue
            if notes is not None and tok[1:] in notes.watch:
                notes.seen.add(tok[1:])
            yield t, tok[1:], 1 if c == "1" else 0
    if in_defs and notes is not None:
        notes.header_open = True


def transition_counts_until(path: str, idents=None, max_changes: Optional[int] = None
                            ) -> Tuple[Dict[str, int], bool, int]:
    """Scalar transitions per identifier (a signal's first value is its initial level, not
    a transition), reading at most `max_changes` value changes: ``(counts, truncated,
    last time read)``. With `idents`, only those are counted. A bounded prefix gives the
    RANKING of the signals (a forwarded clock toggles every UI) without parsing a whole
    large dump on the GUI thread; a truncated count covers ``[0, last time]``, which is
    what lets a caller scale it to the whole dump."""
    want = set(idents) if idents is not None else None
    counts: Dict[str, int] = {}
    last: Dict[str, int] = {}
    seen = 0
    t_seen = 0
    for t, ident, level in _value_changes(path):
        if max_changes is not None and seen >= max_changes:
            return counts, True, t_seen
        seen += 1
        t_seen = t
        if want is not None and ident not in want:
            continue
        if ident not in last:
            last[ident] = level                # initial value, not a transition
        elif level != last[ident]:
            last[ident] = level
            counts[ident] = counts.get(ident, 0) + 1
    return counts, False, t_seen


# Peak bytes per edge of `load_capture`: one uint64 in a growing array('Q'), whose
# over-allocation is at most an eighth. The arrays become the Capture's without a copy.
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


def _same(notes: _StreamNotes, ident: str, t: int) -> None:
    """Count one value change that lands on the time of the signal's previous change."""
    rec = notes.same.setdefault(ident, [0, t])
    rec[0] += 1


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

    Edges are collected as uint64 in array('Q') buffers (`BYTES_PER_EDGE`), and
    every `_CHECK_EVERY` edges the bytes held are compared with the shared
    `memory_budget()`; past it, :class:`VcdTooLargeError` is raised. `max_bytes`
    follows the .sal convention: None uses the computed budget, a positive value
    overrides it, and 0 opts out."""
    require_distinct(path, clock_ident, data_ident, "signal")
    info = read_info(path)
    rate = info.sample_rate_hz
    budget = 0
    if max_bytes != 0:
        from .saleae_sal import memory_budget  # ONE definition of the budget
        budget = memory_budget(max_bytes)
    clk_edges = array("Q")
    dat_edges = array("Q")
    clk_last: Optional[int] = None
    dat_last: Optional[int] = None
    clk_t = dat_t = -1                         # time of each signal's latest change
    init_clk = False
    init_dat = False
    next_check = _CHECK_EVERY
    notes = _StreamNotes((clock_ident, data_ident))
    for t, ident, level in _value_changes(path, notes):
        if ident == clock_ident:
            if clk_last is None:
                init_clk, clk_last = bool(level), level
            elif level != clk_last:
                if t == clk_t:                 # a further change within one time step:
                    _same(notes, ident, t)     # the step's net change is what counts
                if t == clk_t and clk_edges and clk_edges[-1] == t:
                    clk_edges.pop()
                else:
                    clk_edges.append(t)
                clk_last, clk_t = level, t
        elif ident == data_ident:
            if dat_last is None:
                init_dat, dat_last = bool(level), level
            elif level != dat_last:
                if t == dat_t:
                    _same(notes, ident, t)
                if t == dat_t and dat_edges and dat_edges[-1] == t:
                    dat_edges.pop()
                else:
                    dat_edges.append(t)
                dat_last, dat_t = level, t
        else:
            continue
        if budget and len(clk_edges) + len(dat_edges) >= next_check:
            next_check += _CHECK_EVERY
            edges = len(clk_edges) + len(dat_edges)
            held = edges * BYTES_PER_EDGE
            if held > budget:
                raise VcdTooLargeError(path, edges, held, budget, t)

    # Zero-copy views of the buffers (the arrays stay alive through the views).
    clk = np.frombuffer(clk_edges, dtype=np.uint64) if len(clk_edges) else \
        np.zeros(0, dtype=np.uint64)
    dat = np.frombuffer(dat_edges, dtype=np.uint64) if len(dat_edges) else \
        np.zeros(0, dtype=np.uint64)
    # The forwarded clock toggles every UI -> the most edges. When the caller can't
    # say which is which, take the busier signal as the clock (same as .sal/.csv).
    if auto_clock and dat.size > clk.size:
        clk, dat = dat, clk
        init_clk, init_dat = init_dat, init_clk
    # The decoder assumes ascending edges; VCD is time-ordered, but sort defensively
    # so a non-monotonic dump can't feed the decoder out-of-order samples.
    clk.sort()
    dat.sort()
    _report(path, info, notes, clock_ident, data_ident, clk_last is None, dat_last is None)
    same = repeats(clk) + repeats(dat)          # edges the sort put on one sample
    if same:
        import_notes.warn(import_notes.source_name(path), same_sample_note(same, rate))
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=init_clk, initial_data=init_dat, sample_rate_hz=rate)
