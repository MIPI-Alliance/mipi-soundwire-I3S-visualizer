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
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

import numpy as np

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
    return VcdInfo(timescale_s=timescale_s, sample_rate_hz=rate, signals=signals)


def _value_changes(path: str) -> Iterator[tuple]:
    """Yield (time, ident, level) for every scalar 0/1 value change in a VCD's
    value-change section. The single source of truth for the value stream, shared by
    load_capture and transition_counts. It skips the header, x/z (undumped/unknown)
    values, vector/real changes, and $comment blocks; the $dumpvars / $dumpon /
    $dumpoff / $dumpall markers pass through and their contained real 0/1 changes are
    yielded normally (the x-fills a $dumpoff emits for every signal are dropped, so
    they don't inject phantom edges)."""
    t = 0
    in_defs = True
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
                    t = int(tok[1:])
                continue
            if c in "bBrR":                   # vector/real change: value token then ident token
                next(toks, None)
                continue
            if c == "$":
                if tok == "$comment":         # skip free text (may look like value changes)
                    for x in toks:
                        if x == "$end":
                            break
                continue                      # $dumpvars/$dumpon/$dumpoff/$dumpall/$end: markers
            if c not in "01":                 # x / z / other: not a binary level change
                continue
            yield t, tok[1:], 1 if c == "1" else 0


def transition_counts(path: str, idents=None) -> Dict[str, int]:
    """Count scalar transitions per identifier (a signal's first value is its initial
    level, not a transition). Used to auto-pick the forwarded clock (the busiest
    line). If `idents` is given, only those are counted."""
    want = set(idents) if idents is not None else None
    counts: Dict[str, int] = {}
    last: Dict[str, int] = {}
    for _t, ident, level in _value_changes(path):
        if want is not None and ident not in want:
            continue
        if ident not in last:
            last[ident] = level                # initial value, not a transition
        elif level != last[ident]:
            last[ident] = level
            counts[ident] = counts.get(ident, 0) + 1
    return counts


def load_capture(path: str, clock_ident: str, data_ident: str,
                 auto_clock: bool = False) -> Capture:
    """Build a Capture from a VCD's clock + data scalar signals.

    Each value change at ``#t`` becomes a transition at sample ``t`` (the
    $timescale is the sample period), and a signal's first observed value is its
    initial level. With ``auto_clock`` the busier of the two signals is taken as
    the forwarded clock (it toggles every UI), so the caller needn't know which is
    which."""
    rate = read_info(path).sample_rate_hz
    clk_edges: List[int] = []
    dat_edges: List[int] = []
    clk_last: Optional[int] = None
    dat_last: Optional[int] = None
    init_clk = False
    init_dat = False
    for t, ident, level in _value_changes(path):
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

    clk = np.asarray(clk_edges, dtype=np.uint64)
    dat = np.asarray(dat_edges, dtype=np.uint64)
    # The forwarded clock toggles every UI -> the most edges. When the caller can't
    # say which is which, take the busier signal as the clock (same as .sal/.csv).
    if auto_clock and dat.size > clk.size:
        clk, dat = dat, clk
        init_clk, init_dat = init_dat, init_clk
    # The decoder assumes ascending edges; VCD is time-ordered, but sort defensively
    # so a non-monotonic dump can't feed the decoder out-of-order samples.
    clk.sort()
    dat.sort()
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=init_clk, initial_data=init_dat, sample_rate_hz=rate)
