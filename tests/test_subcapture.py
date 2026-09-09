"""Sub-capture locator tests: find a smaller capture's signal pattern inside a
larger one, sample-rate-independent.

Run: PYTHONPATH="$PWD" python3 -m pytest tests/test_subcapture.py -q
"""
from __future__ import annotations

import time

import numpy as np
import swi3score

from swi3s_studio.analysis.subcapture import capture_to_symbols, locate_subcapture
from swi3s_studio.ingest import transitions
from swi3s_studio.ingest.capture import Capture


def test_low_entropy_sub_is_bounded_not_a_hang():
    """A low-entropy sub (constant/idle data) matches at a huge number of overlapping
    offsets in a large capture. The exact-match hit collection must be CAPPED so this
    stays fast instead of the O(N * hits) hang (which stalled the UI for minutes).
    Both the exact path (some matches) and the no-match seed path must be bounded."""
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
    # A no-match constant sub (data all 1 vs main all 0) exercises the seed path.
    sub2 = Capture(clock_edges=sub.clock_edges, data_edges=sub.clock_edges,  # data toggles every edge
                   initial_clock=False, initial_data=True, sample_rate_hz=1_000_000)
    _ = locate_subcapture(main, sub2)
    # A LOOSE BOUND HERE, THE TIGHT ONE IN THE PERF LANE. This file runs in the default suite
    # on every CI config, and docs/TESTING.md's convention is that wall-clock CEILINGS belong
    # behind the `perf` marker with generous margins, so runner jitter cannot redden an
    # ordinary run. But the defect this pins was a HANG (minutes, O(N * hits)), and a hang is
    # worth catching everywhere — so the bound that stays here is one no amount of jitter can
    # reach while still failing a regression to that behaviour. The real ceiling
    # (test_perf.py::test_low_entropy_locate_ceiling) is 15 s.
    assert time.time() - t0 < 120.0, "low-entropy locate must be bounded, not a hang"
    assert isinstance(matches, list)
    # STRUCTURALLY bounded too, not just fast: the cap on collected hits is what makes it
    # quick, so assert the cap held rather than inferring it from the clock.
    assert len(matches) <= 64, \
        f"the match cap did not hold: {len(matches)} matches (max_matches defaults to 64)"


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
    # Guarantee strictly ascending / no duplicate collapses at low ratios by
    # nudging any non-increasing step (only matters at extreme downscale, not
    # exercised by the ratios used in these tests, but keeps this robust).
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


def test_diagnostics_reports_best_score_on_no_match():
    """When nothing clears the threshold, `diagnostics` reports how close the best
    candidate got AND where it is (so the UI can bookmark it), and orientation is
    None."""
    main = _make_main()
    rng = np.random.default_rng(999)
    clk = np.cumsum(rng.integers(2, 8, size=3000)).astype(np.uint64)
    dat = np.sort(rng.choice(clk[:-1], size=1000, replace=False)).astype(np.uint64)
    bogus = Capture(clock_edges=clk, data_edges=dat, initial_clock=False,
                    initial_data=True, sample_rate_hz=RATE)
    diag = {}
    matches = locate_subcapture(main, bogus, diagnostics=diag)
    assert matches == []
    assert diag["orientation"] is None
    assert 0.0 <= diag["best_score"] < 0.98
    # best candidate is either a concrete near-miss location or None (a truly
    # unrelated sub shares no seed with main, so there is no meaningful "best").
    assert diag["best_start_sample"] is None or isinstance(diag["best_start_sample"], int)


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
    mean-center that away (else the true match scored ~0 and unrelated windows
    won). Regression for the global-vs-local median normalization bug."""
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


def test_tolerance_zero_is_exact_and_does_not_crash():
    """tolerance=0 ('exact shape only') must not divide-by-zero into NaN scores;
    an exact slice must still be found."""
    main = _make_main()
    n = main.clock_edges.size
    sub, origin = _slice_capture(main, n // 3, n // 3 + 2000)
    matches = locate_subcapture(main, sub, tolerance=0.0)
    assert matches, "exact slice should still match at tolerance=0"
    assert abs(max(matches, key=lambda m: m["score"])["start_sample"] - origin) <= 4


def test_empty_or_oversized_sub_returns_no_matches():
    main = _make_main()
    empty = Capture(clock_edges=np.asarray([], dtype=np.uint64),
                    data_edges=np.asarray([], dtype=np.uint64),
                    initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    assert locate_subcapture(main, empty) == []

    # A "sub" bigger than main can't match anywhere.
    assert locate_subcapture(sub=main, main=empty) == []
