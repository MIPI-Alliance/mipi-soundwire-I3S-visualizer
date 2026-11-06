"""Sub-capture locator tests: find a smaller capture's signal pattern inside a
larger one, sample-rate-independent.

Run: PYTHONPATH="$PWD" python3 -m pytest tests/test_subcapture.py -q
"""
from __future__ import annotations

import time

import numpy as np
import pytest
import swi3score

from swi3s_studio.analysis.subcapture import capture_to_symbols, locate_subcapture
from swi3s_studio.ingest import transitions
from swi3s_studio.ingest.capture import Capture


def test_low_entropy_sub_is_bounded_not_a_hang():
    """A low-entropy sub (constant/idle data) matches at a huge number of overlapping
    offsets in a large capture. The exact-match hit collection must be CAPPED so this
    stays fast instead of O(N * hits). Both the exact path (some matches) and the
    no-match seed path must be bounded."""
    n = 8_000_000
    clk = (np.arange(1, n + 1, dtype=np.int64) * 2).astype(np.uint64)
    # data never toggles -> every clock edge samples data level 0 (a constant pattern)
    main = Capture(clock_edges=clk, data_edges=np.zeros(0, dtype=np.uint64),
                   initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    sub = Capture(clock_edges=(np.arange(1, 601, dtype=np.int64) * 2).astype(np.uint64),
                  data_edges=np.zeros(0, dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    t0 = time.time()
    matches = locate_subcapture(main, sub)          # constant sub -> matches (capped/collapsed)
    # A sub whose sampled data alternates every clock edge (main's is constant 0) cannot
    # match, and exercises the seed path.
    sub2 = Capture(clock_edges=sub.clock_edges, data_edges=sub.clock_edges - 1,
                   initial_clock=False, initial_data=True, sample_rate_hz=1_000_000)
    assert locate_subcapture(main, sub2) == []
    # A LOOSE BOUND HERE, THE TIGHT ONE IN THE PERF LANE. Wall-clock CEILINGS belong behind
    # the `perf` marker (docs/TESTING.md); this bound only catches an O(N * hits) hang, which
    # no runner jitter reaches. The tight ceiling is
    # test_perf.py::test_low_entropy_locate_ceiling.
    assert time.time() - t0 < 120.0, "low-entropy locate must be bounded, not a hang"
    # The constant sub matches the constant main exactly, at many overlapping offsets that
    # collapse to a handful of occurrences.
    assert matches and all(m["score"] == 1.0 for m in matches)
    # STRUCTURALLY bounded too, not just fast: the cap on collected hits is what makes it
    # quick, so assert the cap held rather than inferring it from the clock.
    assert len(matches) <= 64, \
        f"the match cap did not hold: {len(matches)} matches (max_matches defaults to 64)"


def test_the_exact_search_stops_at_its_hit_limit_and_says_it_was_cut():
    from swi3s_studio.analysis.subcapture import _exact_offsets
    main = np.zeros(1000, dtype=np.uint8)
    sub = np.zeros(10, dtype=np.uint8)
    assert _exact_offsets(main, sub, limit=5) == ([0, 1, 2, 3, 4], True)
    offsets, truncated = _exact_offsets(main, sub)
    assert len(offsets) == 991 and truncated is False


def test_a_constant_sub_in_a_convolved_search_is_capped_to_its_first_hits(monkeypatch):
    """On the convolve path a constant sub clears the threshold at every offset; the hit list is
    cut at _MAX_HITS, so only the first occurrence (offsets 0..49 collapse to one) is kept."""
    from swi3s_studio.analysis import subcapture
    bits = np.zeros(3000, dtype=np.int64)
    main, sub = _cap_from_bits(bits), _cap_from_bits(bits[:100])
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 10 ** 12)         # force the convolve path
    uncapped = locate_subcapture(main, sub, max_matches=1000)
    assert len(uncapped) > 20
    monkeypatch.setattr(subcapture, "_MAX_HITS", 50)
    capped = locate_subcapture(main, sub, max_matches=1000)
    assert len(capped) == 1 and capped[0]["start_sample"] == 4 and capped[0]["score"] == 1.0


RATE = transitions.DEFAULT_SAMPLE_RATE_HZ


def _level_before(edges: np.ndarray, initial: bool, sample: int) -> bool:
    """Logic level of a line just before `sample` (number of prior edges' parity)."""
    n = int(np.searchsorted(edges, np.uint64(sample), side="left"))
    return bool(initial) ^ bool(n & 1)


def _slice_capture(main: Capture, clock_lo_idx: int, clock_hi_idx: int) -> tuple:
    """Extract a contiguous SUB capture spanning main clock edges
    [clock_lo_idx, clock_hi_idx), re-based so the first included clock edge's
    sample becomes sample 0's frame (edges shifted by the window start).

    Returns (sub_capture, origin_sample) where `origin_sample` is the MAIN
    sample number corresponding to SUB sample 0 — i.e. the expected match
    `start_sample` when this SUB is located back in `main`.
    """
    clk = main.clock_edges
    dat = main.data_edges
    origin = int(clk[clock_lo_idx])
    hi_sample = int(clk[clock_hi_idx]) if clock_hi_idx < clk.size else int(clk[-1]) + 1

    sub_clk = clk[clock_lo_idx:clock_hi_idx].astype(np.int64) - origin
    lo_mask = (dat.astype(np.int64) >= origin) & (dat.astype(np.int64) < hi_sample)
    sub_dat = dat[lo_mask].astype(np.int64) - origin

    initial_clock = _level_before(clk, main.initial_clock, origin)
    initial_data = _level_before(dat, main.initial_data, origin)

    sub = Capture(
        clock_edges=sub_clk.astype(np.uint64),
        data_edges=sub_dat.astype(np.uint64),
        initial_clock=initial_clock,
        initial_data=initial_data,
        sample_rate_hz=main.sample_rate_hz,
    )
    return sub, origin


def _rescale_capture(cap: Capture, new_rate: int) -> Capture:
    """Return a copy of `cap` re-sampled to `new_rate`: every edge sample number
    is scaled by new_rate/old_rate, so the same transition PATTERN (and hence
    the same symbol sequence) now spans a different number of physical samples
    — exactly what happens when the same signal is captured by an analyzer
    running at a different sample rate."""
    ratio = new_rate / float(cap.sample_rate_hz)
    clk = np.round(cap.clock_edges.astype(np.float64) * ratio).astype(np.uint64)
    dat = np.round(cap.data_edges.astype(np.float64) * ratio).astype(np.uint64)
    # Keep each edge list strictly ascending: edges that round to the same sample at a low
    # ratio collapse into one.
    clk = np.unique(clk)
    dat = np.unique(dat)
    return Capture(clock_edges=clk, data_edges=dat,
                  initial_clock=cap.initial_clock, initial_data=cap.initial_data,
                  sample_rate_hz=new_rate)


def _make_main() -> Capture:
    levels = swi3score.make_demo_levels(300)   # config + commit + ~300 samples/ch audio
    return transitions.build_capture_from_levels(levels)


def test_symbol_sequence_is_the_logical_waveform():
    main = _make_main()
    seq = capture_to_symbols(main)
    # One symbol (the data bit) per clock edge.
    assert len(seq) == main.clock_edges.size
    assert seq.syms.dtype == np.uint8
    assert set(np.unique(seq.syms).tolist()) <= {0, 1}     # data level 0/1
    assert seq.samples.size == seq.syms.size


def test_symbol_sequence_is_rate_independent():
    """The same logical waveform captured at a different sample rate reduces to the
    SAME symbol sequence — only the `samples` mapping scales."""
    main = _make_main()
    seq = capture_to_symbols(main)
    fast = capture_to_symbols(_rescale_capture(main, main.sample_rate_hz * 3))
    assert np.array_equal(seq.syms, fast.syms)


def test_locates_exact_slice_at_original_offset():
    main = _make_main()
    n_edges = main.clock_edges.size
    lo, hi = n_edges // 3, n_edges // 3 + 4000    # a few thousand UIs, mid-capture
    sub, origin = _slice_capture(main, lo, hi)

    matches = locate_subcapture(main, sub)
    assert matches, "expected at least one match"
    best = max(matches, key=lambda m: m["score"])
    assert abs(best["start_sample"] - origin) <= 4, best
    assert best["score"] >= 0.98, best      # an exact slice is a perfect match
    # Matches must be sorted by start_sample.
    assert [m["start_sample"] for m in matches] == sorted(m["start_sample"] for m in matches)


def test_unrelated_subcapture_does_not_match():
    main = _make_main()
    rng = np.random.default_rng(12345)
    # Random clock edges (no relation to main's pattern) at a similar density.
    n = 4000
    gaps = rng.integers(2, 8, size=n).astype(np.uint64)
    clk = np.cumsum(gaps)
    dat_positions = rng.choice(clk[:-1], size=n // 3, replace=False)
    dat = np.sort(dat_positions).astype(np.uint64)
    bogus = Capture(clock_edges=clk, data_edges=dat,
                    initial_clock=False, initial_data=bool(rng.integers(0, 2)),
                    sample_rate_hz=RATE)

    matches = locate_subcapture(main, bogus)
    assert matches == [], matches


def test_matches_across_different_sample_rates():
    main = _make_main()
    n_edges = main.clock_edges.size
    lo, hi = n_edges // 2, n_edges // 2 + 3000
    sub, origin = _slice_capture(main, lo, hi)

    # Re-capture the same SUB pattern at 3x the sample rate (as if a different
    # analyzer/session recorded it) — absolute sample counts no longer relate
    # to main's, only the transition SHAPE does.
    sub_fast = _rescale_capture(sub, main.sample_rate_hz * 3)
    assert sub_fast.sample_rate_hz != main.sample_rate_hz

    matches = locate_subcapture(main, sub_fast)
    assert matches, "expected a rate-independent match"
    best = max(matches, key=lambda m: m["score"])
    assert abs(best["start_sample"] - origin) <= 8, best
    assert best["score"] >= 0.98, best      # same pattern, different rate -> still perfect

    # And at a slower rate too (0.5x).
    sub_slow = _rescale_capture(sub, main.sample_rate_hz // 2)
    matches_slow = locate_subcapture(main, sub_slow)
    assert matches_slow, "expected a rate-independent match (slow)"
    best_slow = max(matches_slow, key=lambda m: m["score"])
    assert abs(best_slow["start_sample"] - origin) <= 8, best_slow
    assert best_slow["score"] >= 0.98, best_slow


def test_matches_with_swapped_clock_data_orientation():
    """A sub whose clock/data lines are assigned opposite to main's (e.g. a .wfm
    that auto-picked the other line as clock) must still be found — locate retries
    the swapped orientation when the as-loaded one finds nothing."""
    main = _make_main()
    n_edges = main.clock_edges.size
    sub, origin = _slice_capture(main, n_edges // 2, n_edges // 2 + 3000)
    swapped = Capture(clock_edges=sub.data_edges, data_edges=sub.clock_edges,
                      initial_clock=sub.initial_data, initial_data=sub.initial_clock,
                      sample_rate_hz=sub.sample_rate_hz)
    diag = {}
    matches = locate_subcapture(main, swapped, diagnostics=diag)
    assert matches, "swapped-orientation sub should still be located"
    assert diag["orientation"] == "clock/data swapped"
    assert max(matches, key=lambda m: m["score"])["score"] >= 0.98


def _unrelated_pair():
    main = _make_main()
    rng = np.random.default_rng(999)
    clk = np.cumsum(rng.integers(2, 8, size=3000)).astype(np.uint64)
    dat = np.sort(rng.choice(clk[:-1], size=1000, replace=False)).astype(np.uint64)
    bogus = Capture(clock_edges=clk, data_edges=dat, initial_clock=False,
                    initial_data=True, sample_rate_hz=RATE)
    return main, bogus


def test_diagnostics_on_no_match_without_a_shared_seed_has_no_best_location(monkeypatch):
    """The exact path (forced here) reports a location only where the sub's first 32 symbols
    occur; a sub that shares no such seed with main has none (None) and a zero score."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 0)
    main, bogus = _unrelated_pair()
    diag = {}
    assert locate_subcapture(main, bogus, diagnostics=diag) == []
    assert diag["orientation"] is None
    assert diag["best_score"] == 0.0
    assert diag["best_start_sample"] is None and diag["best_end_sample"] is None


def test_diagnostics_on_no_match_in_a_convolved_search_reports_the_count_peak(monkeypatch):
    """The convolve path (forced here) always has a global peak: the offset with the most
    agreeing symbols, a concrete location, scored below the threshold."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 10 ** 12)
    main, bogus = _unrelated_pair()
    diag = {}
    assert locate_subcapture(main, bogus, diagnostics=diag) == []
    assert diag["orientation"] is None
    assert 0.0 < diag["best_score"] < 0.98
    assert diag["best_start_sample"] is not None and diag["best_end_sample"] is not None


def _damaged_tail_pair():
    rng = np.random.default_rng(31)
    bits = rng.integers(0, 2, size=3000)
    sub_bits = bits[1000:1400].copy()
    sub_bits[240::5] ^= 1                               # the first 240 of 400 symbols intact
    return _cap_from_bits(bits), _cap_from_bits(sub_bits)


def test_diagnostics_locates_the_offset_with_the_most_agreeing_symbols(monkeypatch):
    """A sub whose tail is damaged reports, on the convolve path, the offset where most of its
    symbols agree (so the UI can bookmark it) with a score below the threshold."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 10 ** 12)
    main, sub = _damaged_tail_pair()
    diag = {}
    assert locate_subcapture(main, sub, diagnostics=diag) == []
    assert diag["best_score"] == pytest.approx(0.92)   # 32 of 400 symbols differ
    assert diag["best_start_sample"] == 4 * 1001        # where the slice was taken from
    assert diag["best_end_sample"] == 4 * 1401
    assert diag["orientation"] is None


def test_diagnostics_locates_the_longest_intact_head_on_the_exact_path(monkeypatch):
    """The exact path scores a near-miss by the longest matching prefix among the offsets that
    share the sub's first 32 symbols: here 240 of 400."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 0)
    main, sub = _damaged_tail_pair()
    diag = {}
    assert locate_subcapture(main, sub, diagnostics=diag) == []
    assert diag["best_score"] == pytest.approx(240 / 400)
    assert diag["best_start_sample"] == 4 * 1001
    assert diag["best_end_sample"] == 4 * 1401
    assert diag["orientation"] is None


def _make_multirate(gaps_by_region, seed=3):
    """Build a Capture whose clock runs at DIFFERENT constant gap rates across
    regions (e.g. [(10000,4),(4000,6)] = 10000 edges at gap 4 then 4000 at gap 6),
    with a distinctive pseudo-random data bit at each clock edge so a sub-slice is
    localizable. Returns (capture, clock_samples)."""
    gaps = np.concatenate([np.full(n, g) for (n, g) in gaps_by_region])
    clk = np.cumsum(gaps).astype(np.int64)
    bits = np.random.default_rng(seed).integers(0, 2, size=clk.size)
    chg = np.flatnonzero(np.diff(bits) != 0) + 1
    dat = (clk[chg] - 1).astype(np.uint64)            # data edge just before a changing bit
    cap = Capture(clock_edges=clk.astype(np.uint64), data_edges=dat,
                  initial_clock=False, initial_data=bool(bits[0]),
                  sample_rate_hz=1_000_000)
    return cap, clk


def test_locates_slice_in_a_multi_rate_main():
    """A true occurrence in a region whose clock rate differs from main's global
    median must still score ~1.0. capture_to_symbols normalizes gaps by the
    WHOLE-capture median, so a single-rate sub sliced from a non-median region
    carries a constant log-gap offset vs main's window; _shape_similarity must
    mean-center that away."""
    main, clk = _make_multirate([(10000, 4), (4000, 6)])
    k0, k1 = 11000, 11500                              # slice from the gap=6 region
    s0, s1 = int(clk[k0]), int(clk[k1])
    ce, de = main.clock_edges, main.data_edges
    sub = Capture(
        clock_edges=(ce[(ce >= s0) & (ce < s1)] - s0).astype(np.uint64),
        data_edges=(de[(de.astype(np.int64) >= s0) & (de.astype(np.int64) < s1)]
                    .astype(np.int64) - s0).astype(np.uint64),
        initial_clock=bool(main.initial_clock) ^ bool(np.count_nonzero(ce < s0) & 1),
        initial_data=bool(main.initial_data) ^ bool(np.count_nonzero(de.astype(np.int64) < s0) & 1),
        sample_rate_hz=main.sample_rate_hz)
    diag = {}
    matches = locate_subcapture(main, sub, diagnostics=diag)
    assert matches, diag
    best = max(matches, key=lambda m: m["score"])
    assert best["score"] >= 0.98, diag
    assert abs(best["start_sample"] - s0) <= 8, (best, s0)


def test_an_exact_slice_is_found():
    main = _make_main()
    n = main.clock_edges.size
    sub, origin = _slice_capture(main, n // 3, n // 3 + 2000)
    matches = locate_subcapture(main, sub)
    assert matches, "an exact slice should match"
    assert abs(max(matches, key=lambda m: m["score"])["start_sample"] - origin) <= 4


def _dlv(n, seed):
    """A PHY3 pair: the data line toggles on every clock edge."""
    gaps = np.random.default_rng(seed).integers(8, 40, n)
    e = np.cumsum(gaps).astype(np.uint64)
    return Capture(clock_edges=e, data_edges=e.copy(), initial_clock=False,
                   initial_data=True, sample_rate_hz=1_000_000)


def test_a_dlv_capture_is_refused_not_matched_everywhere():
    """On a PHY3 pair the data line is the clock's complement, so the data bit at each
    clock edge only alternates: any sub would match at every other offset with score 1.0."""
    main, sub = _dlv(20000, 1), _dlv(2000, 2)
    with pytest.raises(ValueError, match="PHY3 .DLV. differential pair"):
        locate_subcapture(main, sub)
    with pytest.raises(ValueError, match="sub capture is a PHY3"):
        locate_subcapture(_make_main(), sub)
    normal_sub, _origin = _slice_capture(_make_main(), 0, 2000)
    with pytest.raises(ValueError, match="main capture is a PHY3"):
        locate_subcapture(main, normal_sub)


def test_empty_or_oversized_sub_returns_no_matches():
    main = _make_main()
    empty = Capture(clock_edges=np.asarray([], dtype=np.uint64),
                    data_edges=np.asarray([], dtype=np.uint64),
                    initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    assert locate_subcapture(main, empty) == []

    # A "sub" bigger than main can't match anywhere.
    short, _origin = _slice_capture(main, 0, main.clock_edges.size // 10)
    assert short.clock_edges.size < main.clock_edges.size
    assert locate_subcapture(main=short, sub=main) == []
    # An empty main has nothing to match in.
    assert locate_subcapture(sub=main, main=empty) == []


# ---------------------------------------------------------------------------
# Coincident edges, collapse, ranking, thresholds and the exact-search path
# ---------------------------------------------------------------------------

def _cap_from_bits(bits, gap: int = 4) -> Capture:
    """A capture whose data bit at clock edge i is bits[i]: clock edges every `gap`
    samples from `gap`, a data edge one sample before each clock edge whose bit differs
    from the previous (the first from a low initial level)."""
    bits = np.asarray(bits, dtype=np.int64)
    clk = (np.arange(1, bits.size + 1, dtype=np.int64) * gap)
    prev = np.concatenate([[0], bits[:-1]])
    dat = clk[bits != prev] - 1
    return Capture(clock_edges=clk.astype(np.uint64), data_edges=dat.astype(np.uint64),
                   initial_clock=False, initial_data=False, sample_rate_hz=RATE)


def test_a_data_edge_on_a_clock_edge_sample_is_read_before_it_as_the_decoder_does():
    """The decoder reads the data line at the sample before a clock edge, so a data
    transition ON the clock-edge sample belongs to the next UI. At 4x the sample rate the
    same transition one fine sample ahead of the clock edge is read at that edge: the
    coarse capture, which collapses it onto the clock edge, reads it a UI later."""
    def cap(scale, data_at):
        return Capture(clock_edges=np.array([10, 20, 30, 40], dtype=np.uint64) * scale,
                       data_edges=np.array([data_at], dtype=np.uint64),
                       initial_clock=False, initial_data=False, sample_rate_hz=RATE * scale)
    assert capture_to_symbols(cap(1, 20)).syms.tolist() == [0, 0, 1, 1]
    # One sample earlier the edge is seen by the clock edge at 20 (symbol 1 at index 1).
    assert capture_to_symbols(cap(1, 19)).syms.tolist() == [0, 1, 1, 1]
    assert capture_to_symbols(cap(4, 80)).syms.tolist() == [0, 0, 1, 1]      # on the edge, 4x
    assert capture_to_symbols(cap(4, 79)).syms.tolist() == [0, 1, 1, 1]      # one fine sample ahead
    # Sample 0 has no earlier sample: it reads itself.
    first = Capture(clock_edges=np.array([0, 10], dtype=np.uint64),
                    data_edges=np.array([0], dtype=np.uint64), initial_clock=False,
                    initial_data=False, sample_rate_hz=RATE)
    assert capture_to_symbols(first).syms.tolist() == [1, 1]


def test_collapse_merges_hits_closer_than_the_sub_and_keeps_the_best():
    from swi3s_studio.analysis.subcapture import _collapse
    assert _collapse([0, 10], [1.0, 1.0], 10) == [(0, 1.0), (10, 1.0)]       # exactly m apart
    assert _collapse([0, 9], [1.0, 0.9], 10) == [(0, 1.0)]                   # earlier is better
    assert _collapse([0, 9], [0.9, 1.0], 10) == [(9, 1.0)]                   # later is better
    assert _collapse([0, 9], [0.95, 0.95], 10) == [(0, 0.95)]                # tie keeps the first
    assert _collapse([9, 0], [1.0, 0.9], 10) == [(9, 1.0)]                   # input order is free


def test_the_exact_search_reports_every_overlapping_hit_position():
    from swi3s_studio.analysis.subcapture import _exact_offsets
    main = np.array([0, 1, 0, 1, 0, 1, 0], dtype=np.uint8)
    needle = np.array([0, 1, 0], dtype=np.uint8)
    assert _exact_offsets(main, needle) == ([0, 2, 4], False)
    assert _exact_offsets(np.ones(10, dtype=np.uint8), np.ones(3, dtype=np.uint8)) == (
        list(range(8)), False)


def _aperiodic_sub(m: int = 100, seed: int = 21) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2, size=m)


def test_a_sub_as_long_as_the_main_matches_it_once():
    bits = _aperiodic_sub(120)
    main = _cap_from_bits(bits)
    matches = locate_subcapture(main, _cap_from_bits(bits))
    assert [(m["start_sample"], m["score"]) for m in matches] == [(4, 1.0)]
    assert matches[0]["end_sample"] == int(main.clock_edges[-1])


def test_back_to_back_occurrences_are_reported_separately():
    sub = _aperiodic_sub()
    main = _cap_from_bits(np.tile(sub, 3))
    matches = locate_subcapture(main, _cap_from_bits(sub))
    assert [m["start_sample"] for m in matches] == [4, 4 + 4 * 100, 4 + 4 * 200]
    # An interior match ends on the clock edge one symbol past it; the last is clamped.
    assert [m["end_sample"] for m in matches] == [4 + 4 * 100, 4 + 4 * 200, 4 * 300]


def _main_with_occurrences(damaged_flags, sub, seed=5):
    """Random filler between occurrences of `sub`; a flagged occurrence has one symbol
    flipped. Returns (capture, [start sample of each occurrence])."""
    rng = np.random.default_rng(seed)
    bits, starts = [], []
    for damaged in damaged_flags:
        bits.extend(rng.integers(0, 2, size=150).tolist())
        occ = sub.copy()
        if damaged:
            occ[50] ^= 1
        starts.append(4 * (len(bits) + 1))
        bits.extend(occ.tolist())
    bits.extend(rng.integers(0, 2, size=150).tolist())
    return _cap_from_bits(bits), starts


def test_a_capped_result_keeps_the_best_scoring_occurrences():
    sub = _aperiodic_sub()
    flags = [False, True] * 6
    main, starts = _main_with_occurrences(flags, sub)
    matches = locate_subcapture(main, _cap_from_bits(sub), max_matches=4,
                                score_threshold=0.9)
    assert len(matches) == 4
    assert all(m["score"] == 1.0 for m in matches)
    assert [m["start_sample"] for m in matches] == sorted(m["start_sample"] for m in matches)
    assert {m["start_sample"] for m in matches} <= {s for s, f in zip(starts, flags) if not f}
    # Uncapped, the damaged ones are reported at their lower score.
    everything = locate_subcapture(main, _cap_from_bits(sub), max_matches=64,
                                   score_threshold=0.9)
    assert sorted(m["score"] for m in everything) == [0.99] * 6 + [1.0] * 6


def test_a_capped_result_is_reported_in_time_order_not_score_order():
    sub = _aperiodic_sub()
    flags = [True, True, False, False, True]                  # scores .99 .99 1 1 .99
    main, starts = _main_with_occurrences(flags, sub)
    matches = locate_subcapture(main, _cap_from_bits(sub), max_matches=3, score_threshold=0.9)
    assert [m["start_sample"] for m in matches] == [starts[0], starts[2], starts[3]]
    assert [m["score"] for m in matches] == [0.99, 1.0, 1.0]


def test_the_score_threshold_is_inclusive():
    sub = _aperiodic_sub(40)
    damaged = sub.copy()
    damaged[:10] ^= 1                                  # 30 of 40 symbols agree: 0.75
    main = _cap_from_bits(np.concatenate([np.random.default_rng(8).integers(0, 2, 200),
                                          damaged,
                                          np.random.default_rng(9).integers(0, 2, 200)]))
    at = locate_subcapture(main, _cap_from_bits(sub), score_threshold=0.75)
    assert [m["score"] for m in at if m["start_sample"] == 4 * 201] == [0.75]
    above = locate_subcapture(main, _cap_from_bits(sub), score_threshold=0.76)
    assert not [m for m in above if m["start_sample"] == 4 * 201]


def test_the_exact_search_path_finds_separated_occurrences(monkeypatch):
    """A main too large for the convolution takes the byte-substring search; it reports each
    of three separated occurrences once, and matches a sub as long as the main."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 0)
    searched = []
    real = subcapture._exact_offsets
    monkeypatch.setattr(subcapture, "_exact_offsets",
                        lambda *a, **k: searched.append(1) or real(*a, **k))
    sub = _aperiodic_sub()
    main, starts = _main_with_occurrences([False, False, False], sub)
    matches = locate_subcapture(main, _cap_from_bits(sub))
    assert searched, "the exact-search path was not taken"
    assert [m["start_sample"] for m in matches] == starts
    assert all(m["score"] == 1.0 for m in matches)


def test_the_exact_search_path_collapses_overlapping_hits_to_one_match(monkeypatch):
    """A periodic main holds the sub at every period; hits closer than the sub's length are
    one occurrence and are reported once."""
    from swi3s_studio.analysis import subcapture
    monkeypatch.setattr(subcapture, "_CONVOLVE_BUDGET", 0)
    period = _aperiodic_sub(10)
    sub = np.tile(period, 5)                           # 50 symbols, repeating every 10
    main = _cap_from_bits(np.tile(period, 8))          # hits at 0, 10, 20, 30
    matches = locate_subcapture(main, _cap_from_bits(sub))
    assert [m["score"] for m in matches] == [1.0]
    assert matches[0]["start_sample"] == 4
    # A sub as long as the main matches it on this path too.
    assert len(locate_subcapture(_cap_from_bits(sub), _cap_from_bits(sub))) == 1
