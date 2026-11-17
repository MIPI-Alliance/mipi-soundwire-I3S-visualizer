"""Both VCD value-change readers give what the token-at-a-time reader gives, on a corpus.

The reference below (`ref_load_capture`, `ref_transition_counts_until` and the
`_value_changes` token loop they share) is the per-token reader, kept here as the
definition of the value-change grammar; the module's header readers (`read_info`,
`_tokens`, `_declaration`) and its warning text (`_report`) are shared. Every test runs
twice, once on the native reader (`swi3score.VcdScanner`) and once on the NumPy reader the
module falls back to without it (the `reader` fixture). For every VCD the reader under test
and the reference are compared on the edges, initial levels, rate, import warnings and any
error (type and message), for `load_capture` with and without an edge budget and for
`transition_counts_until` with and without an ident list and a change limit, at several
read-step sizes. The corpus is synthetic: hand-written cases for each construct, random
dumps mixing all of them, truncated copies, and a large random dump. The native reader is
also fed each of several cases in two pieces split at every byte.
"""
import random
import sys
from array import array
from typing import Dict, Iterator, Optional, Tuple

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.ingest import vcd
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.ingest.timebase import repeats, require_distinct, same_sample_note

# ---- the reference reader -----------------------------------------------------------------

_UNKNOWN_DIGITS = frozenset("xz")


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
        toks = vcd._tokens(f)
        for tok in toks:
            if in_defs:
                if tok == "$enddefinitions":
                    _skip_block(toks, notes, tok)
                    in_defs = False
                elif tok == "$var":
                    decl, state = vcd._declaration(toks)
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


def ref_transition_counts_until(path: str, idents=None, max_changes: Optional[int] = None
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

def _same(notes: _StreamNotes, ident: str, t: int) -> None:
    """Count one value change that lands on the time of the signal's previous change."""
    rec = notes.same.setdefault(ident, [0, t])
    rec[0] += 1


def ref_load_capture(path: str, clock_ident: str, data_ident: str,
                 auto_clock: bool = False, max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a VCD's clock + data scalar signals.

    Each value change at ``#t`` becomes a transition at sample ``t`` (the
    $timescale is the sample period), and a signal's first observed value is its
    initial level. Several changes of one signal at one ``#t`` make one transition
    (or none) to the level the step ends on, and are reported. With ``auto_clock`` the
    busier of the two signals is taken as the forwarded clock (it toggles every UI), so
    the caller needn't know which is which. A clock signal that is also the data signal
    raises ValueError naming the file.

    Edges are collected as uint64 in array('Q') buffers (`vcd.BYTES_PER_EDGE`), and
    every `vcd._CHECK_EVERY` edges the bytes held are compared with the shared
    `memory_budget()`; past it, :class:`VcdTooLargeError` is raised. `max_bytes`
    follows the .sal convention: None uses the computed budget, a positive value
    overrides it, and 0 opts out."""
    require_distinct(path, clock_ident, data_ident, "signal")
    info = vcd.read_info(path)
    rate = info.sample_rate_hz
    budget = 0
    if max_bytes != 0:
        from swi3s_studio.ingest.saleae_sal import memory_budget  # ONE definition of the budget
        budget = memory_budget(max_bytes)
    clk_edges = array("Q")
    dat_edges = array("Q")
    clk_last: Optional[int] = None
    dat_last: Optional[int] = None
    clk_t = dat_t = -1                         # time of each signal's latest change
    init_clk = False
    init_dat = False
    next_check = vcd._CHECK_EVERY
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
            next_check += vcd._CHECK_EVERY
            edges = len(clk_edges) + len(dat_edges)
            held = edges * vcd.BYTES_PER_EDGE
            if held > budget:
                raise vcd.VcdTooLargeError(path, edges, held, budget, t)

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
    vcd._report(path, info, notes, clock_ident, data_ident, clk_last is None, dat_last is None)
    same = repeats(clk) + repeats(dat)          # edges the sort put on one sample
    if same:
        import_notes.warn(import_notes.source_name(path), same_sample_note(same, rate))
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=init_clk, initial_data=init_dat, sample_rate_hz=rate)


# ---- comparing a reader with the reference ------------------------------------------------

_NATIVE = vcd._NATIVE


@pytest.fixture(autouse=True, params=["native", "array"])
def reader(request, monkeypatch):
    """The value-change reader under test: the native scanner, or the NumPy fallback."""
    if request.param == "native":
        assert _NATIVE is not None, "the swi3score module has no VcdScanner; rebuild it"
    else:
        monkeypatch.setattr(vcd, "_NATIVE", None)
    return request.param


def _outcome(fn):
    """What a reader returns or raises, with the import warnings it gave."""
    with import_notes.collect() as notes:
        try:
            got = fn()
        except Exception as e:                           # noqa: BLE001 - compared below
            extra = tuple(getattr(e, k, None) for k in ("edges", "held", "budget",
                                                        "last_time"))
            return ("raised", type(e).__name__, str(e), extra), [str(n) for n in notes]
    if isinstance(got, Capture):
        got = ("capture", got.clock_edges.dtype.str, got.clock_edges.tolist(),
               got.data_edges.dtype.str, got.data_edges.tolist(), got.initial_clock,
               got.initial_data, got.sample_rate_hz)
    else:
        counts, cut, t = got
        got = ("counts", list(counts.items()), cut, t)
    return got, [str(n) for n in notes]


def _same_outcome(new, ref):
    a, b = _outcome(new), _outcome(ref)
    assert a == b


_STEPS = (1, 2, 3, 7, 64, 1000)


def _compare(path, pairs, monkeypatch, steps=_STEPS, limits=(None, 0, 1, 2, 5, 40),
             budgets=((0, None), (None, None), (30, 3), (200, 7)), autos=(False, True)):
    """Compare both readers on `path` for each (clock, data) pair, step size, change limit
    and (max_bytes, _CHECK_EVERY) budget."""
    info_idents = None
    try:
        info_idents = [s.ident for s in vcd.read_info(path).scalar_signals]
    except ValueError:
        pass
    for step in steps:
        monkeypatch.setattr(vcd, "_READ_BYTES", step)
        monkeypatch.setattr(vcd, "_NATIVE_READ_BYTES", step)
        for clock, data in pairs:
            for max_bytes, every in budgets:
                if every is not None:
                    monkeypatch.setattr(vcd, "_CHECK_EVERY", every)
                for auto in autos:
                    _same_outcome(
                        lambda: vcd.load_capture(path, clock, data, auto_clock=auto,
                                                 max_bytes=max_bytes),
                        lambda: ref_load_capture(path, clock, data, auto_clock=auto,
                                                 max_bytes=max_bytes))
                monkeypatch.setattr(vcd, "_CHECK_EVERY", 1 << 16)
        for idents in (None, info_idents, [p[0] for p in pairs]):
            for limit in limits:
                _same_outcome(lambda: vcd.transition_counts_until(path, idents, limit),
                              lambda: ref_transition_counts_until(path, idents, limit))


_HEAD = ("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n"
         "$var wire 1 % dat $end\n$upscope $end\n$enddefinitions $end\n")

# Each case is a whole file (str, written as UTF-8, or bytes) and the (clock, data) pairs.
_CASES = {
    "scalar": (_HEAD + "#0\n0!\n1%\n#5\n1!\n#7\n0%\n#9\n0!\n1%\n", [("!", "%")]),
    "dumpvars": (_HEAD + "#0\n$dumpvars\n0!\n0%\n$end\n#3\n1!\n$dumpall 1! 0% $end\n#4\n0!\n",
                 [("!", "%")]),
    "dumpoff": (_HEAD + "#0\n1!\n1%\n#10\n0!\n#20\n$dumpoff\nx!\nx%\n$end\n#30\n$dumpon\n0!"
                "\n1%\n$end\n#40\n1!\nx!\n#41\nz%\nX!\nZ%\n#50\n0!\n", [("!", "%")]),
    "dumpoff_unclosed": (_HEAD + "#0\n1!\n1%\n#10\n$dumpoff\nx!\n#20\nx%\n0!\n",
                         [("!", "%")]),
    "xz_before_first": (_HEAD + "#0\nx!\nz%\n#1\n0!\n#2\nx!\nx%\n#3\n1%\nX%\n", [("!", "%")]),
    "vectors": ("$timescale 10 ps $end\n$var reg 1 ! clk [0:0] $end\n$var wire 8 v bus [7:0]"
                " $end\n$var wire 1 % dat $end\n$var real 64 r volts $end\n$enddefinitions"
                " $end\n#0\nb0 !\nb10101010 v\n0%\nr1.5 r\n#5\nb1 !\nB0 %\nbx !\nbZ !\n"
                "#6\nB1 !\nb0x1 !\nbxx !\nb !\nr2 !\nRx !\n#7\nb1 v\nb11 %\nr0.0 %\n#8\nb0 !\n",
                [("!", "%"), ("v", "%"), ("r", "!")]),
    "comments": (_HEAD + "#0\n0!\n0%\n$comment 1! #99 b1 ! $dumpoff $end\n#5\n1!\n"
                 "$comment\n$comment $end 0!\n#6 $comment $end $end\n1%\n", [("!", "%")]),
    "comment_unclosed": (_HEAD + "#0\n0!\n0%\n$comment two\n#10\n1!\n", [("!", "%")]),
    "value_eats_keywords": (_HEAD + "#0\n0!\n0%\nb1 $comment\n#3\n1!\nb0 $end\nb1 #9\n"
                            "#4\nb1 b0 !\nb1 b0 b1 %\n0%\nr1 $dumpoff\nx!\n#5\nx!\n",
                            [("!", "%")]),
    "value_at_end": (_HEAD + "#0\n0!\n0%\n#2\n1!\nb1", [("!", "%")]),
    "times": (_HEAD + "#0\n0!\n0%\n#0005\n1!\n#5\n0!\n#3\n1!\n#3\n0!\n#\n#10\n1%\n#\n"
              f"#{(1 << 63) - 1}\n0%\n", [("!", "%")]),
    "same_step": (_HEAD + "#0\n0!\n0%\n#5\n1!\n0!\n1!\n#7\n0!\n1!\n#9\n1%\n0%\n1%\n0%\n",
                  [("!", "%")]),
    "back_onto_edge": (_HEAD + "#0\n0!\n0%\n#5\n1!\n#7\n0!\n#5\n1!\n0!\n1!\n#9\n0!\n#7\n1!"
                       "\n0!\n1!\n0!\n", [("!", "%")]),
    "back_then_steps": (_HEAD + "#0\n0!\n#7\n1!\n#9\n0!\n#9\n1!\n#7\n0!\n#7\n1!\n0!\n1!\n"
                        "0!\n#20\n1!\n", [("!", "%")]),
    "bad_time_fraction": (_HEAD + "#0\n0!\n0%\n#5\n1!\n#2.5\n0!\n", [("!", "%")]),
    "bad_time_negative": (_HEAD + "#0\n0!\n#-5\n1!\n", [("!", "%")]),
    "bad_time_letters": (_HEAD + "#0\n0!\n#1\n1!\n#abc\n0!\n", [("!", "%")]),
    "bad_time_2_63": (_HEAD + f"#0\n0!\n#{1 << 63}\n1!\n", [("!", "%")]),
    "bad_time_digits": (_HEAD + "#0\n0!\n#" + "9" * 30 + "\n1!\n", [("!", "%")]),
    "bad_time_arabic": (_HEAD + "#0\n0!\n#٣\n1!\n", [("!", "%")]),
    "zeros_time": (_HEAD + "#0\n0!\n#" + "0" * 3000 + "7\n1!\n", [("!", "%")]),
    "consumed_bad_time": (_HEAD + "#0\n0!\nb1 #x\n#1\n1!\n", [("!", "%")]),
    "timescale_fraction": ("$timescale 3 ns $end\n$var wire 1 ! a $end\n$var wire 1 \" b $end"
                           "\n$enddefinitions $end\n#0\n0!\n0\"\n#4\n1!\n", [("!", '"')]),
    "timescale_us": ("$timescale\n  100 us\n$end\n$var wire 1 ! a $end\n$var wire 1 # b $end"
                     "\n$enddefinitions $end\n#0\n0!\n0#\n#4\n1!\n1#\n", [("!", "#")]),
    "scopes": ("$date today $end\n$version gen 1 $end\n$timescale 1 fs $end\n$scope module top"
               " $end\n$scope module u1 $end\n$var wire 1 ! clk $end\n$upscope $end\n$scope "
               "task t $end\n$var wire 1 % d $end\n$var wire 1 ! clk_alias $end\n$upscope $end"
               "\n$upscope $end\n$comment $var wire 1 Z fake $end\n$enddefinitions $end\n"
               "#0\n0!\n0%\n0Z\n#1\n1Z\n1!\n#2\n1%\n", [("!", "%"), ("Z", "!")]),
    "odd_idents": ("$timescale 1ns $end\n$var wire 1 # h $end\n$var wire 1 $ d $end\n"
                   "$var wire 1 0 z $end\n$var wire 1 b bb $end\n$var wire 1 ~{}| t $end\n"
                   "$var wire 1 longident_9 l $end\n$var wire 1 é e $end\n"
                   "$var wire 1 λλλλ x $end\n$var wire 01 q odd $end\n"
                   "$enddefinitions $end\n#0\n0#\n0$\n00\n0b\n0~{}|\n0longident_9\n0é\n"
                   "0λλλλ\n0q\n#1\n1#\n1$\nb1 0\nb1 b\n1~{}|\n"
                   "1longident_9\nb1 é\nb1 λλλλ\nb1 q\n#2\nxé\n"
                   "é1\nxλλλλ\n0\n1\n#3\n0é\n",
                   [("#", "$"), ("0", "b"), ("~{}|", "longident_9"), ("é", "q"),
                    ("λλλλ", "longident_9"), ("", "#")]),
    "whitespace": ("$timescale\t1ns\x0b$end\r\n$var wire 1 ! a $end\x1c$var wire 1 % b $end"
                   " $enddefinitions $end　#0\x0c0!\x1d0%\x1e#5\x1f1! #6"
                   "\x851% #7\r0!\r\n\r\n#8 \t 1!", [("!", "%")]),
    "no_enddefinitions": ("$timescale 1ns $end\n$var wire 1 ! a $end\n#0\n0!\n#1\n1!\n",
                          [("!", "%")]),
    "enddefinitions_open": ("$timescale 1ns $end\n$var wire 1 ! a $end\n$enddefinitions\n#0"
                            "\n0!\n", [("!", "%")]),
    "header_block_open": ("$timescale 1ns $end\n$var wire 1 ! a $end\n$comment never", [("!", "%")]),
    "undeclared": (_HEAD + "#0\n0!\n0%\n0?\n#1\n1?\n1!\nb1 ?\n", [("!", "?"), ("?", "%")]),
    "missing": (_HEAD + "#0\n0!\n#1\n1!\n", [("!", "%"), ("!", "nope")]),
    "invalid_utf8": (_HEAD.encode() + b"#0\n0!\n0\xff\n0%\n#1\n1\xff\n1!\n\xc3\n#2\n0\xc3\xa9"
                     b"\n1%\n#3\n0\xe2\x82", [("!", "%"), ("�", "!")]),
    "empty": ("", [("!", "%")]),
    "only_timescale": ("$timescale 1ns $end", [("!", "%")]),
    "long_token_value": (_HEAD + "#0\n0!\n#1\n1!\n" + "a" * (70_000) + "\n#2\n0!\n",
                         [("!", "%")]),
    "long_token_in_comment": (_HEAD + "#0\n0!\n$comment " + "é" * 66_000 + " $end\n#2\n"
                              "1!\n", [("!", "%")]),
    "token_at_limit": (_HEAD + "#0\n0!\n" + "z" * (1 << 16) + "\n#2\n1!\n", [("!", "%")]),
    "long_tail": (_HEAD + "#0\n0!\n#1\n1!\n" + "q" * 70_000, [("!", "%")]),
}


def _write(tmp_path, name, text) -> str:
    p = tmp_path / f"{name}.vcd"
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return str(p)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_hand_written_case_reads_as_the_reference_reads_it(tmp_path, monkeypatch, name):
    text, pairs = _CASES[name]
    big = len(text) > 10_000
    _compare(_write(tmp_path, name, text), pairs, monkeypatch,
             steps=(997, 1 << 15) if big else _STEPS)


@pytest.mark.parametrize("name", ["scalar", "vectors", "comments", "odd_idents", "whitespace",
                                  "invalid_utf8", "value_eats_keywords", "dumpoff"])
def test_every_cut_of_a_case_reads_as_the_reference_reads_it(tmp_path, monkeypatch, name):
    text, pairs = _CASES[name]
    raw = text if isinstance(text, bytes) else text.encode("utf-8")
    head = raw.index(b"$enddefinitions") if b"$enddefinitions" in raw else 0
    for cut in range(max(head - 4, 0), len(raw) + 1):
        path = _write(tmp_path, f"{name}_cut", raw[:cut])
        _compare(path, pairs[:1], monkeypatch, steps=(3,), limits=(None, 3),
                 budgets=((0, None),), autos=(False,))



def test_the_wide_space_table_is_every_non_ascii_space_str_split_splits_on():
    every = {c for c in range(0x80, sys.maxunicode + 1) if chr(c).isspace()}
    assert set(vcd._WIDE_SPACE) == every
    assert set(np.flatnonzero(vcd._WS_LUT).tolist()) == {c for c in range(256)
                                                          if c < 0x80 and chr(c).isspace()}

# ---- random dumps --------------------------------------------------------------------------

_IDENTS = ["!", "%", '"', "#", "0", "b", "x", "aB", "~{}", "longer_than_7", "é",
           "λλ", "?", "v", "r"]
_SEPS = [" ", "\n", "\n", "\n", "\t", "\r\n", "  \n ", "\x0b", "\x0c", "\x1c", " ",
         " ", "\x85"]


def _random_vcd(rng: random.Random, n: int) -> tuple:
    """A random dump of about `n` value-section tokens, and two idents to watch."""
    ts = rng.choice(["1ns", "1 ns", "10 ps", "100 us", "3 ns", "1 fs", "1 s"])
    out = []
    if rng.random() < 0.5:
        out += ["$date", "today", "$end", "$version", "v", "$end"]
    out += ["$timescale", *ts.split(), "$end", "$scope", "module", "top", "$end"]
    widths = {}
    for ident in _IDENTS:
        w = rng.choice(["1", "1", "1", "8", "01", "64"])
        kind = "real" if w == "64" else "wire"
        widths[ident] = w
        out += ["$var", kind, w, ident, "n_" + str(len(widths)), "$end"]
        if rng.random() < 0.2:
            out += ["$scope", "module", "inner", "$end", "$upscope", "$end"]
    if rng.random() < 0.3:
        out += ["$comment", "$var", "wire", "1", "Q", "fake", "$end"]
    out += ["$upscope", "$end"]
    if rng.random() < 0.97:
        out += ["$enddefinitions", "$end"]
    t = 0
    for _ in range(n):
        r = rng.random()
        ident = rng.choice(_IDENTS)
        if r < 0.15:
            step = rng.choice([0, 1, 1, 2, 5, 100])
            if rng.random() < 0.03:
                step = -rng.randint(1, 50)
            t = max(0, t + step)
            out.append("#" + "0" * rng.choice([0, 0, 0, 1, 25]) + str(t))
        elif r < 0.5:
            out.append(rng.choice("0011xzXZu-") + ident)
        elif r < 0.7:
            out += [rng.choice("bbbB") + rng.choice(["0", "1", "x", "z", "X", "Z", "1010", "xx",
                                                     "0x1", ""]), ident]
        elif r < 0.75:
            out += [rng.choice("rR") + rng.choice(["1.5", "0", "x", "-2e3"]), ident]
        elif r < 0.8:
            out.append(rng.choice(["$dumpvars", "$dumpon", "$dumpoff", "$dumpall", "$end",
                                   "$end", "$scope", "$var", "$enddefinitions"]))
        elif r < 0.82:
            out += ["$comment", rng.choice(["1!", "#9", "b1", "x"]), "$end"]
        elif r < 0.83:
            out.append("#")
        elif r < 0.86:
            out += [rng.choice(["b1", "B0", "r1"]), rng.choice(["$comment", "$end", "#3", "b1",
                                                                "$dumpoff", "0!"])]
        elif r < 0.87 and rng.random() < 0.3:
            out.append(rng.choice(["#1.5", "#-1", "#a"]))
        else:
            out.append(rng.choice(["0", "1", "foo", "é!", "1é", "é"]))
    text = "".join(tok + rng.choice(_SEPS) for tok in out)
    one_bit = [i for i in _IDENTS if widths[i] == "1"] or _IDENTS
    clock = rng.choice(one_bit)
    data = rng.choice([i for i in _IDENTS if i != clock])
    return text, [(clock, data)]


@pytest.mark.parametrize("seed", range(40))
def test_a_random_dump_reads_as_the_reference_reads_it(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    text, pairs = _random_vcd(rng, rng.choice([5, 40, 300]))
    raw = text.encode("utf-8")
    if seed % 3 == 0:                                  # cut, maybe inside a character
        raw = raw[:rng.randint(len(raw) // 2, len(raw))]
    if seed % 5 == 0:                                  # bytes that are not UTF-8
        k = rng.randint(len(raw) // 2, len(raw))
        raw = raw[:k] + rng.choice([b"\xff", b"\xc3", b"\xe2\x82", b"\x80"]) + raw[k:]
    _compare(_write(tmp_path, f"r{seed}", raw), pairs, monkeypatch, steps=(1, 64, 1 << 15),
             limits=(None, 1, 7), budgets=((0, None), (30, 3)))


def test_a_large_random_dump_reads_as_the_reference_reads_it(tmp_path, monkeypatch):
    rng = random.Random(1234)
    text, pairs = _random_vcd(rng, 150_000)
    path = _write(tmp_path, "large", text.replace("#1.5", "#15").replace("#-1", "#1")
                  .replace("#a", "#2"))
    _compare(path, pairs, monkeypatch, steps=(4093, 1 << 15), limits=(None, 1000),
             budgets=((0, None), (5_000, 50)))


def test_a_large_clock_and_data_dump_reads_as_the_reference_reads_it(tmp_path, monkeypatch):
    rng = np.random.default_rng(5)
    ce = np.cumsum(rng.integers(1, 4, 200_000))
    de = np.sort(rng.choice(ce, 50_000, replace=False))
    events = sorted([(int(t), "!") for t in ce] + [(int(t), "%") for t in de])
    lines = [_HEAD, "#0\n$dumpvars\n0!\n1%\n$end\n"]
    lvl = {"!": 0, "%": 1}
    cur = None
    for t, ident in events:
        if t != cur:
            lines.append(f"#{t}\n")
            cur = t
        lvl[ident] ^= 1
        lines.append(f"{lvl[ident]}{ident}\n")
    path = _write(tmp_path, "clkdat", "".join(lines))
    _compare(path, [("!", "%"), ("%", "!")], monkeypatch, steps=(1 << 15,),
             limits=(None, 100_000), budgets=((0, None), (1_000_000, 1 << 16)))


# ---- non-ASCII spaces and idents -----------------------------------------------------------

_WIDE_SPACES = sorted(c for c in range(0x80, sys.maxunicode + 1) if chr(c).isspace())
# Characters next to those, which are not spaces.
_NOT_SPACES = [0x84, 0x86, 0x9F, 0xA1, 0x167F, 0x1681, 0x180E, 0x1FFF, 0x200B, 0x2027, 0x202A,
               0x202E, 0x2030, 0x205E, 0x2060, 0x2FFF, 0x3001, 0xFEFF]


def test_every_non_ascii_space_separates_tokens_as_the_reference_reads_it(tmp_path, monkeypatch):
    for c in _WIDE_SPACES + _NOT_SPACES:
        sep = chr(c)
        text = _HEAD + sep.join(["#0", "0!", "0%", "#3", "1!", "1%", "#5", "0!"]) + sep
        _compare(_write(tmp_path, f"sep{c:x}", text), [("!", "%")], monkeypatch,
                 steps=(1, 2, 64), limits=(None, 2), budgets=((0, None),), autos=(False,))


def test_an_ident_with_a_lone_surrogate_matches_nothing_as_in_the_reference(tmp_path,
                                                                             monkeypatch):
    path = _write(tmp_path, "surrogate", _CASES["scalar"][0])
    _compare(path, [("\ud800", "%"), ("!", "\udcff")], monkeypatch, steps=(64,),
             limits=(None,), budgets=((0, None),), autos=(False,))


# ---- the native reader's read steps --------------------------------------------------------

class _Pieces:
    """A binary file whose reads return `pieces` in turn, then b""."""

    def __init__(self, pieces):
        self.pieces = [p for p in pieces if p]

    def read(self, _n=-1):
        return self.pieces.pop(0) if self.pieces else b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _read_in_pieces(monkeypatch, path, raw, cuts):
    """Make the module read `path` in binary as the pieces of `raw` cut at `cuts`."""
    real_open = open

    def fake_open(p, mode="r", *args, **kwargs):
        if p == path and mode == "rb":
            bounds = [0, *cuts, len(raw)]
            return _Pieces([raw[a:b] for a, b in zip(bounds, bounds[1:])])
        return real_open(p, mode, *args, **kwargs)

    monkeypatch.setattr(vcd, "open", fake_open, raising=False)


def _split_cases():
    """(case, cut lists): a cut at every byte of the small cases; around the long token and
    inside its characters for the long ones."""
    for name in ("scalar", "vectors", "comments", "odd_idents", "whitespace", "invalid_utf8",
                 "times", "value_eats_keywords", "dumpoff", "bad_time_arabic",
                 "comment_unclosed", "value_at_end", "no_enddefinitions"):
        yield name, None
    for name, mark in (("token_at_limit", b"z"), ("long_token_in_comment", "é".encode()),
                       ("long_token_value", b"a"), ("long_tail", b"q")):
        yield name, mark


@pytest.mark.parametrize("name, mark", list(_split_cases()))
def test_the_native_reader_fed_two_pieces_split_anywhere_reads_as_the_reference(
        tmp_path, monkeypatch, reader, name, mark):
    if reader != "native":
        pytest.skip("the native reader's read steps")
    text, pairs = _CASES[name]
    raw = text if isinstance(text, bytes) else text.encode("utf-8")
    path = _write(tmp_path, name, raw)
    clock, data = pairs[0]
    monkeypatch.setattr(vcd, "_CHECK_EVERY", 3)

    def calls(load, counts):
        return [lambda: load(path, clock, data, max_bytes=0),
                lambda: load(path, clock, data, max_bytes=30),
                lambda: counts(path, None, None),
                lambda: counts(path, None, 3)]

    want = [_outcome(fn) for fn in calls(ref_load_capture, ref_transition_counts_until)]
    if mark is None:
        cuts = [[k] for k in range(len(raw) + 1)]
    else:                                   # the long token's ends, and a cut inside it
        s = raw.index(mark * 1000)
        e = raw.rindex(mark) + len(mark)
        near = [s - 1, s, s + 1, s + 2, s + 3, (s + e) // 2, (s + e) // 2 + 1, e - 1, e, e + 1,
                s + (1 << 16) - 1, s + (1 << 16), s + (1 << 16) + 1]
        cuts = [[k] for k in near if 0 <= k <= len(raw)] + [[s + 1, e - 1], [s + 3, s + 5]]
    for cut in cuts:
        _read_in_pieces(monkeypatch, path, raw, cut)
        got = [_outcome(fn) for fn in calls(vcd.load_capture, vcd.transition_counts_until)]
        assert got == want, cut
