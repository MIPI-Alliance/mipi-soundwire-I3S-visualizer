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

# $timescale magnitudes -> seconds. VCD allows values 1/10/100 of each unit.
_TIMESCALE_UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}
_TIMESCALE_RE = re.compile(r"^\s*(\d+)\s*(s|ms|us|ns|ps|fs)\s*$")


@dataclass
class VcdSignal:
    ident: str                   # VCD identifier code (e.g. "!", "#", "aB")
    name: str                    # scope-qualified reference name
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


def _parse_timescale(text: str) -> float:
    """'1ns' / '10 ps' / '1 us' -> seconds. Raises ValueError if unparseable."""
    m = _TIMESCALE_RE.match(text.strip())
    if not m:
        raise ValueError(f"unrecognised $timescale {text!r}")
    return int(m.group(1)) * _TIMESCALE_UNITS[m.group(2)]


def _tokens(f) -> Iterator[str]:
    """Whitespace-delimited token stream over a file (VCD is token-oriented; a
    declaration or a vector value can span or share lines)."""
    for line in f:
        for tok in line.split():
            yield tok


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
                parts = []
                for x in toks:
                    if x == "$end":
                        break
                    parts.append(x)
                timescale_s = _parse_timescale("".join(parts))
                timescale_text = " ".join(parts)
            elif tok == "$var":
                # $var <type> <width> <ident> <reference> [<bit-select>] $end
                decl = []
                for x in toks:
                    if x == "$end":
                        break
                    decl.append(x)
                if len(decl) >= 4:
                    # A non-decimal width -> 0 so it's NOT offered as a 1-bit clock/
                    # data candidate (a real scalar always declares width "1").
                    width = int(decl[1]) if decl[1].isdigit() else 0
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
    return VcdInfo(timescale_s=timescale_s, sample_rate_hz=rate, signals=signals,
                   timescale_text=timescale_text)


class _StreamNotes:
    """What `_value_changes` passed over for the idents in `watch`: x/z values after a
    signal's first 0/1 one and outside a $dumpoff block (`xz`: ident -> [count, first
    time]), time stamps earlier than the one before (`back`: [count, first time]), and
    bare "#" tokens with no time (`bare`)."""

    def __init__(self, watch) -> None:
        self.watch = set(watch)
        self.seen: set = set()
        self.xz: Dict[str, list] = {}
        self.back = [0, 0]
        self.bare = 0


def _value_changes(path: str, notes: Optional[_StreamNotes] = None) -> Iterator[tuple]:
    """Yield (time, ident, level) for every scalar 0/1 value change in a VCD's
    value-change section. The single source of truth for the value stream, shared by
    load_capture and transition_counts. It skips the header, x/z (undumped/unknown)
    values, vector/real changes, and $comment blocks; the $dumpvars / $dumpon /
    $dumpoff / $dumpall markers pass through and their contained real 0/1 changes are
    yielded normally (the x-fills a $dumpoff emits for every signal are dropped, so
    they don't inject phantom edges). `notes` (a `_StreamNotes`) counts what was passed
    over for its watched idents."""
    t = 0
    in_defs = True
    in_dumpoff = False
    with open(path, encoding="utf-8", errors="replace") as f:
        toks = _tokens(f)
        for tok in toks:
            if in_defs:
                if tok == "$enddefinitions":
                    for x in toks:
                        if x == "$end":
                            break
                    in_defs = False
                elif tok.startswith("$") and tok != "$end":
                    for x in toks:            # skip header blocks (incl. $comment) to $end
                        if x == "$end":
                            break
                continue
            c = tok[0]
            if c == "#":
                if len(tok) > 1:              # ignore a stray bare "#" (truncated dump)
                    t_new = int(tok[1:])
                    if notes is not None and t_new < t:
                        if not notes.back[0]:
                            notes.back[1] = t_new
                        notes.back[0] += 1
                    t = t_new
                elif notes is not None:
                    notes.bare += 1
                continue
            if c in "bBrR":                   # vector/real change: value token then ident token
                next(toks, None)
                continue
            if c == "$":
                if tok == "$comment":         # skip free text (may look like value changes)
                    for x in toks:
                        if x == "$end":
                            break
                elif tok == "$dumpoff":
                    in_dumpoff = True
                elif tok == "$end":
                    in_dumpoff = False
                continue                      # $dumpvars/$dumpon/$dumpoff/$dumpall/$end: markers
            if c not in "01":                 # x / z / other: not a binary level change
                if notes is not None and not in_dumpoff:
                    ident = tok[1:]
                    if ident in notes.seen:
                        rec = notes.xz.setdefault(ident, [0, t])
                        rec[0] += 1
                continue
            if notes is not None and tok[1:] in notes.watch:
                notes.seen.add(tok[1:])
            yield t, tok[1:], 1 if c == "1" else 0


def transition_counts(path: str, idents=None) -> Dict[str, int]:
    """Count scalar transitions per identifier (a signal's first value is its initial
    level, not a transition). Used to auto-pick the forwarded clock (the busiest
    line). If `idents` is given, only those are counted."""
    return transition_counts_bounded(path, idents)[0]


def transition_counts_bounded(path: str, idents=None, max_changes: Optional[int] = None
                              ) -> Tuple[Dict[str, int], bool]:
    """transition_counts, reading at most `max_changes` value changes: (counts, truncated).
    The channel picker only needs the RANKING (a forwarded clock toggles every UI), which a
    bounded prefix gives without parsing a whole large dump on the GUI thread."""
    counts, truncated, _t = transition_counts_until(path, idents, max_changes)
    return counts, truncated


def transition_counts_until(path: str, idents=None, max_changes: Optional[int] = None
                            ) -> Tuple[Dict[str, int], bool, int]:
    """transition_counts_bounded plus the simulation time reached: ``(counts,
    truncated, last time read)``. A truncated count covers ``[0, last time]``, which
    is what lets a caller scale it to the whole dump."""
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
    for ident, missing in ((clock_ident, clock_missing), (data_ident, data_missing)):
        if missing:
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
    if notes.bare:
        import_notes.warn(src, f"{notes.bare:,} \"#\" with no time "
                               f"{'were' if notes.bare != 1 else 'was'} ignored (the dump "
                               f"may be truncated)")


def load_capture(path: str, clock_ident: str, data_ident: str,
                 auto_clock: bool = False, max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a VCD's clock + data scalar signals.

    Each value change at ``#t`` becomes a transition at sample ``t`` (the
    $timescale is the sample period), and a signal's first observed value is its
    initial level. With ``auto_clock`` the busier of the two signals is taken as
    the forwarded clock (it toggles every UI), so the caller needn't know which is
    which.

    Edges are collected as uint64 in array('Q') buffers (`BYTES_PER_EDGE`), and
    every `_CHECK_EVERY` edges the bytes held are compared with the shared
    `memory_budget()`; past it, :class:`VcdTooLargeError` is raised. `max_bytes`
    follows the .sal convention: None uses the computed budget, a positive value
    overrides it, and 0 opts out."""
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
    init_clk = False
    init_dat = False
    next_check = _CHECK_EVERY
    notes = _StreamNotes((clock_ident, data_ident))
    for t, ident, level in _value_changes(path, notes):
        if ident == clock_ident:
            if clk_last is None:
                init_clk, clk_last = bool(level), level
            elif level != clk_last:
                clk_edges.append(t)
                clk_last = level
        elif ident == data_ident:
            if dat_last is None:
                init_dat, dat_last = bool(level), level
            elif level != dat_last:
                dat_edges.append(t)
                dat_last = level
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
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=init_clk, initial_data=init_dat, sample_rate_hz=rate)
