"""Locate a SUB capture's logical (data, clock) pattern inside a larger MAIN capture.

Use case: the user has a short reference waveform (a sub-capture — a snippet from
another session, or a re-triggered capture of "the same thing") and wants every place
it recurs in a big capture, so the caller can drop a bookmark at each occurrence.

**Logical data pattern — rate-independent.** Each capture is reduced to its per-UI
DATA bits: the data-line level (0/1) sampled at each clock edge, in order — the same
bit stream the decoder reads off the wire (the clock defines the sampling points, so
the (data, clock) waveform is captured with the clock implicit). Because it is the
ORDERED sequence of sampled bits — not absolute sample counts — it carries no
sample-rate information, so the same signal captured at a different rate reduces to the
SAME sequence.

**Matching.** Finding the sub is a direct search for its bit SEQUENCE inside the main's.
For normal-size captures the per-offset match count is computed with a direct sliding
convolution (:func:`numpy.convolve` — no FFT). For a very large main (tens of millions of
edges), where a direct O(N·M) convolution would take too long, exact occurrences are
found instead by an O(N) byte-substring search (the bits packed one per byte) — the same
pattern match, just the scalable form.

Public API:
    capture_to_symbols(capture)              -> SymbolSequence
    locate_subcapture(main, sub, ...)         -> list[dict]
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

from ..nputil import searchsorted as _ss

# Opt-in timing: set SWI3S_LOCATE_DEBUG=1 to print how long each locate stage takes
# (to stderr, visible in run.sh's terminal), for diagnosing a slow/hung search.
_DEBUG = bool(os.environ.get("SWI3S_LOCATE_DEBUG"))


def _timed(label, fn, *args, **kw):
    if not _DEBUG:
        return fn(*args, **kw)
    t = time.perf_counter()
    r = fn(*args, **kw)
    n = None
    if args and hasattr(args[0], "__len__"):
        try:
            n = len(args[0])
        except Exception:  # noqa: BLE001
            n = None
    print(f"[locate] {label}: {time.perf_counter() - t:.2f}s"
          + (f" (n={n:,})" if n else ""), file=sys.stderr, flush=True)
    return r

# A match must agree on at least this FRACTION of the sub's transition symbols. We look
# for near-exact occurrences: a genuine recurrence reproduces the logical (data, clock)
# waveform exactly (score 1.0) even across sample rates, while a coincidental overlap of
# unrelated traffic scores far lower. 0.98 admits only a stray flipped symbol.
DEFAULT_SCORE_THRESHOLD = 0.98

# Above this many (offsets × sub-symbols) multiply-adds, the direct numpy.convolve
# match-count is too slow, so fall back to the O(N) exact byte-substring search. ~4e8
# keeps the convolve path well under a second; a 100M-edge capture lands in the exact path.
_CONVOLVE_BUDGET = 400_000_000

# Cap on raw exact-match hits collected before collapsing. A low-entropy sub (a constant
# or idle data run) matches at a huge number of overlapping offsets; without a cap the
# hit-collection loop is O(N * hits) and hangs. The cap's worth of adjacent hits collapses
# to a handful of reported occurrences, so a distinctive sub is unaffected.
_MAX_HITS = 8192


@dataclass
class SymbolSequence:
    """A capture reduced to its rate-independent logical DATA pattern.

    ``syms``    one uint8 per clock edge: the data-line level (0/1) sampled at that
                clock edge — the per-UI data bit the decoder reads off the wire. The
                clock defines the sampling points, so this sequence is the logical
                (data, clock) waveform with the clock implicit; it carries no absolute
                sample-rate information, so the same signal at a different rate reduces
                to the SAME sequence.
    ``samples`` the MAIN-capture sample number of each clock edge, so a match index
                maps back to a sample number for bookmarking.
    """
    syms: np.ndarray
    samples: np.ndarray

    def __len__(self) -> int:
        return int(self.syms.size)


def _levels_at(edges: np.ndarray, initial: bool, points: np.ndarray) -> np.ndarray:
    """Vectorised logic level of a line at each of `points`: `initial` XORed with the
    parity of the number of transitions at or before the point (each edge toggles the
    level). One searchsorted call."""
    n = _ss(edges, np.asarray(points, dtype=np.int64), side="right")
    return np.bool_(initial) ^ (n.astype(np.int64) & 1).astype(bool)


def capture_to_symbols(capture) -> SymbolSequence:
    """Reduce `capture` to its :class:`SymbolSequence`: the data-line level (0/1) at
    each clock edge, in order — the per-UI data bit the clock samples. One searchsorted,
    O(N), no sort (a whole-transition union of both lines would need an O(N log N) sort
    over ~N_clock+N_data points, far too slow on a 100M-edge capture). Rate-independent —
    see module docstring."""
    clk = np.asarray(capture.clock_edges, dtype=np.int64)
    dat = np.asarray(capture.data_edges, dtype=np.int64)
    if clk.size == 0:
        return SymbolSequence(syms=np.zeros(0, dtype=np.uint8),
                              samples=np.zeros(0, dtype=np.uint64))
    dlevel = _levels_at(dat, capture.initial_data, clk)     # data bit at each clock edge
    return SymbolSequence(syms=dlevel.astype(np.uint8),
                          samples=clk.astype(np.uint64))


def _swapped(capture):
    """A view of `capture` with clock and data lines exchanged. Different acquisitions
    may disagree on which physical line is 'clock' (a .wfm auto-picks the busier line; a
    digital import may fix it to a channel index). Locating tries this swapped view as a
    fallback. capture_to_symbols only reads these four attributes."""
    return SimpleNamespace(clock_edges=capture.data_edges,
                           data_edges=capture.clock_edges,
                           initial_clock=capture.initial_data,
                           initial_data=capture.initial_clock,
                           sample_rate_hz=capture.sample_rate_hz)


def _match_counts(main_syms: np.ndarray, sub_syms: np.ndarray) -> np.ndarray:
    """Per-offset count of matching symbols between `sub_syms` and every equal-length
    window of `main_syms`, via a direct sliding convolution (numpy.convolve, one pass
    per symbol value present in the sub — no FFT). Length N - M + 1. Only called on the
    convolve path (bounded problem size); the caller gates on `_CONVOLVE_BUDGET`."""
    m = sub_syms.size
    n = main_syms.size
    out_len = n - m + 1
    counts = np.zeros(out_len, dtype=np.float64)
    rev = sub_syms[::-1]
    # Only symbol values actually in the sub can contribute a match — iterate those
    # (the capture alphabet is binary 0/1, so this is 1-2 passes, not a fixed 4). Skips
    # building a full-length mask for absent values (the old (0,1,2,3) loop wasted two
    # N-sized float arrays per call).
    for v in np.unique(rev):
        a = (main_syms == v).astype(np.float64)
        k = (rev == v).astype(np.float64)
        counts += np.convolve(a, k, mode="valid")
    return counts


def _exact_offsets(main_syms: np.ndarray, sub_syms: np.ndarray,
                   limit: int = 0) -> tuple:
    """Start indices where `sub_syms` occurs EXACTLY in `main_syms`, via a C-level
    byte-substring search (one byte per symbol) — O(N), tiny memory, no FFT.

    Returns (offsets, truncated). A LOW-ENTROPY sub (a constant/idle data run) matches
    at a huge number of overlapping offsets in a large capture, which would make this
    an O(N * hits) blowup (a real hang). `limit` caps how many hits are collected: once
    reached, the search stops and `truncated=True` — such a sub isn't a distinctive
    reference, and the collected hits still collapse to a few reported matches."""
    m = sub_syms.size
    if m == 0 or main_syms.size < m:
        return [], False
    hay = np.ascontiguousarray(main_syms, dtype=np.uint8).tobytes()
    needle = np.ascontiguousarray(sub_syms, dtype=np.uint8).tobytes()
    out: list[int] = []
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return out, False
        out.append(i)
        if limit and len(out) >= limit:
            return out, True
        start = i + 1


def _best_partial(main_syms: np.ndarray, sub_syms: np.ndarray,
                  seed_len: int = 32) -> tuple:
    """Closest near-miss when there is no exact occurrence (for the "best candidate"
    bookmark): among offsets that share the sub's first `seed_len` symbols, the one with
    the longest matching prefix. Returns (score_fraction, start_index) or (0.0, None).
    Cheap — a short-seed byte search (capped, so a low-entropy seed can't blow up) plus a
    prefix compare at each hit."""
    m = sub_syms.size
    if m == 0 or main_syms.size < m:
        return 0.0, None
    s = min(seed_len, m)
    # Cap the seed hits low (this path only needs the SINGLE best near-miss): a
    # low-entropy seed can still match widely, and each hit costs an O(m) prefix
    # compare below, so an uncapped seed set would be O(hits * m) — another hang.
    seeds, _ = _exact_offsets(main_syms, sub_syms[:s], limit=256)
    best_run, best_i = 0, None
    for i in seeds:
        w = main_syms[i:i + m]
        eq = (w == sub_syms[:w.size])
        run = int(w.size) if eq.all() else int(np.argmax(~eq))
        if run > best_run:
            best_run, best_i = run, i
    return (best_run / m, best_i) if best_i is not None else (0.0, None)


def _collapse(offsets: list, scores: list, m: int) -> list:
    """Collapse overlapping hits (offsets within `m` of each other — the same
    occurrence) to the single best-scoring one; return [(offset, score)] by offset."""
    order = sorted(range(len(offsets)), key=lambda k: offsets[k])
    kept: list[tuple] = []
    for k in order:
        o, sc = offsets[k], scores[k]
        if kept and o - kept[-1][0] < m:
            if sc > kept[-1][1]:
                kept[-1] = (o, sc)
        else:
            kept.append((o, sc))
    return kept


def _search_one(main_seq, sub_seq, score_threshold: float, max_matches: int):
    """Find `sub_seq`'s symbol sequence in `main_seq`. Returns (matches, best): matches
    is the above-threshold, overlap-collapsed, capped list of
    ``{"start_sample","end_sample","score"}``; best describes the single closest
    candidate (score/location) regardless of threshold, for the no-match bookmark."""
    _NONE = {"score": 0.0, "start_sample": None, "end_sample": None}
    m = len(sub_seq)
    n = len(main_seq)
    if m == 0 or n < m:
        return [], dict(_NONE)

    def _rec(off, score):
        end = min(off + m, n - 1)
        return {"start_sample": int(main_seq.samples[off]),
                "end_sample": int(main_seq.samples[end]),
                "score": float(score)}

    use_convolve = 4 * (n - m + 1) * m <= _CONVOLVE_BUDGET
    if use_convolve:
        counts = _match_counts(main_seq.syms, sub_seq.syms)
        if counts.size == 0:
            return [], dict(_NONE)
        peak = int(np.argmax(counts))
        best = _rec(peak, counts[peak] / m)              # global peak (for the no-match bookmark)
        hit = np.flatnonzero(counts >= score_threshold * m)
        if hit.size == 0:
            return [], best
        # Cap the hit list like the exact-search path (_MAX_HITS): a low-entropy sub
        # (idle/constant run) clears the threshold at a huge number of overlapping offsets,
        # and _collapse over millions of Python-list hits is an O(hits) stall. flatnonzero
        # is ascending, so the kept prefix's adjacent hits still collapse to their
        # occurrences; beyond the cap the sub simply isn't distinctive.
        if hit.size > _MAX_HITS:
            hit = hit[:_MAX_HITS]
        collapsed = _collapse(list(hit), [counts[i] / m for i in hit], m)
        matches = [_rec(o, sc) for o, sc in collapsed]
    else:
        # Huge main: exact byte-substring search (O(N)); a genuine occurrence is exact.
        # Capped: a low-entropy sub (idle/constant run) matches at millions of offsets,
        # which without a cap is an O(N * hits) hang. Adjacent hits from a constant run
        # then collapse to one occurrence; beyond the cap the sub simply isn't distinctive.
        hits, _trunc = _exact_offsets(main_seq.syms, sub_seq.syms, limit=_MAX_HITS)
        if not hits:
            frac, bi = _best_partial(main_seq.syms, sub_seq.syms)
            return [], (_rec(bi, frac) if bi is not None else dict(_NONE))
        collapsed = _collapse(hits, [1.0] * len(hits), m)   # merge overlapping edge-index hits
        matches = [_rec(o, sc) for o, sc in collapsed]      # exact => score 1.0
        best = matches[0]

    matches.sort(key=lambda d: d["start_sample"])
    if len(matches) > max_matches:
        matches.sort(key=lambda d: d["score"], reverse=True)
        matches = matches[:max_matches]
        matches.sort(key=lambda d: d["start_sample"])
    return matches, best


def locate_subcapture(main, sub, *, max_matches: int = 64,
                      tolerance: float = 0.05,
                      score_threshold: float = DEFAULT_SCORE_THRESHOLD,
                      progress=None,
                      diagnostics: dict = None) -> list:
    """Find every place `sub`'s logical (data, clock) pattern occurs in `main`.

    Both are :class:`~swi3s_studio.ingest.capture.Capture` and may have different
    ``sample_rate_hz`` — matching is on the ORDERED sequence of logical
    ``(data<<1)|clock`` states at each transition (see module docstring), not absolute
    sample counts, so it is rate-independent. A match needs BOTH lines to agree at every
    transition. `tolerance` is accepted for backward compatibility and ignored (the old
    gap-shape term is gone). `score_threshold` is the minimum fraction of matching
    symbols (default near-exact).

    Orientation: `sub` is searched as-loaded first; if that finds nothing it is retried
    with clock/data SWAPPED, so a sub whose lines were assigned opposite to `main`'s
    still matches. Overlapping hits from one occurrence are collapsed to their best.

    If `diagnostics` (a dict) is given, it is populated with ``best_score`` (the closest
    candidate's matching fraction, even below threshold), ``best_start_sample`` /
    ``best_end_sample`` (that candidate's span, for a no-match bookmark; may be None), and
    ``orientation`` ('as-loaded', 'clock/data swapped', or None).

    Returns up to `max_matches` dicts sorted by ``start_sample`` ascending:
        {"start_sample": int, "end_sample": int, "score": float}
    Empty list if nothing meets `score_threshold`.
    """
    _p = progress or (lambda *_a: None)      # progress(fraction 0..1, phase label)
    _p(0.02, "Decoding capture…")
    main_seq = _timed("reduce main", capture_to_symbols, main)
    _p(0.55, "Decoding sub-capture…")
    sub_seq = _timed("reduce sub", capture_to_symbols, sub)

    _p(0.65, "Searching (as-loaded)…")
    matches, best = _timed("search as-loaded", _search_one,
                           main_seq, sub_seq, score_threshold, max_matches)
    orientation = "as-loaded" if matches else None

    if not matches:
        _p(0.80, "Searching (clock/data swapped)…")
        swapped_seq = _timed("reduce sub (swapped)", capture_to_symbols, _swapped(sub))
        swap_matches, swap_best = _timed("search swapped", _search_one, main_seq,
                                         swapped_seq, score_threshold, max_matches)
        if swap_best["score"] > best["score"]:
            best = swap_best
        if swap_matches:
            matches = swap_matches
            orientation = "clock/data swapped"

    _p(1.0, "Done")
    if diagnostics is not None:
        diagnostics["best_score"] = best["score"]
        diagnostics["best_start_sample"] = best["start_sample"]
        diagnostics["best_end_sample"] = best["end_sample"]
        diagnostics["orientation"] = orientation
    return matches
