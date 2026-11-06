"""timebase.rate_from_times for times on no grid (the reciprocal of the finest spacing to
4 significant figures, so the relative error is the same at every rate) and on a grid
(a fit that stays on the nearest sample), timebase.repeats across its chunks, and the
sample-rate range rule, and the direct behaviour of `to_samples`, `drop_nonfinite`,
`require_finite`, `same_sample_note` and `require_distinct`.

Run: python3 -m pytest tests/test_timebase.py
"""
import re

import numpy as np
import pytest

from swi3s_studio.ingest import timebase


@pytest.mark.parametrize("spacing, want", [
    (1.9e-3, 526), (2.1e-3, 476), (1.2e-3, 833), (0.67e-3, 1493), (0.4e-3, 2500),
    (3e-3, 333), (1e-6, 1_000_000), (3.0, 1),
    (7.3e-6, 137_000), (8.1e-6, 123_500), (1.7e-7, 5_882_000)])
def test_a_spacing_on_no_grid_gives_its_rate_to_four_figures(monkeypatch, spacing, want):
    monkeypatch.setattr(timebase, "_MAX_SAMPLES_PER_GAP", 1)
    times = np.array([0.0, spacing, spacing + spacing * np.pi])
    assert timebase.rate_from_times(times) == want


def test_times_on_a_grid_give_that_grid_rate():
    """A candidate whose timestamps sit up to 0.45 of a sample off its grid is not a fit:
    these four times are on a 5728 Hz grid, and a 367 Hz candidate holds them to 0.4."""
    assert timebase.rate_from_times(np.array([0, 218, 234, 421]) / 5728.0) == 5728


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 1 << 20])
def test_a_repeat_is_counted_at_every_pair_whatever_the_chunk(monkeypatch, chunk):
    monkeypatch.setattr(timebase, "_CHUNK", chunk)
    n = 10
    for pair in range(n - 1):                       # the repeat sits on this pair only
        edges = np.arange(n, dtype=np.uint64)
        edges[pair + 1:] -= np.uint64(1)
        assert timebase.repeats(edges) == 1, (chunk, pair)
    assert timebase.repeats(np.arange(n, dtype=np.uint64)) == 0
    assert timebase.repeats(np.zeros(n, dtype=np.uint64)) == n - 1
    assert timebase.repeats(np.zeros(1, dtype=np.uint64)) == 0
    assert timebase.repeats(np.zeros(0, dtype=np.uint64)) == 0


@pytest.mark.parametrize("rate, problem", [
    (1, ""), (1.0, ""), (1e15, ""), (500_000_000, ""),
    (0.99, "is below 1 Hz"), (0, "is below 1 Hz"), (-5, "is below 1 Hz"),
    (1e15 * 1.0000001, "is above 1e+15 Hz"), (2 ** 64, "is above 1e+15 Hz"),
    (float("inf"), "is above 1e+15 Hz"), (float("nan"), "is not a number"),
    ("fast", "is not a number"), (None, "is not a number"), (10 ** 400, "is above 1e+15 Hz")])
def test_the_rate_range_rule(rate, problem):
    assert timebase.rate_problem(rate) == problem
    if problem:
        want = (r"x\.csv: the sample rate must be at least 1 Hz and at most "
                r"1,000,000,000,000,000 Hz \(got " + re.escape(repr(rate)) + r"\)")
        with pytest.raises(ValueError, match=want):
            timebase.check_rate(rate, "x.csv")
    else:
        timebase.check_rate(rate, "x.csv")


def test_a_spacing_finer_than_float64_can_invert_gives_a_rate_every_reader_refuses():
    rate = timebase.rate_from_times(np.array([0.0, 5e-324, 1e-323]))
    assert rate > timebase.MAX_RATE_HZ
    assert timebase.rate_problem(rate) == "is above 1e+15 Hz"


def test_one_huge_time_neither_warns_nor_hides_the_grid_of_the_others(recwarn):
    times = np.concatenate([np.arange(6) * 1e-6, [1e308], np.arange(6, 3000) * 1e-6])
    with np.errstate(all="raise"):
        assert timebase.rate_from_times(times) == 1_000_000
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


@pytest.mark.parametrize("t, want", [(0.0, 0), (0.4, 0), (0.5, 0), (0.6, 1), (2.5, 2),
                                     (float(2 ** 63 - 1024), 2 ** 63 - 1024)])
def test_to_samples_rounds_to_the_nearest_sample_inside_the_range(t, want):
    assert timebase.to_samples(np.array([t]), 0.0, 1.0).tolist() == [want]


@pytest.mark.parametrize("t", [-0.6, float(2 ** 63), 1e300, float("nan"), float("inf"),
                               float("-inf")])
def test_to_samples_refuses_a_time_outside_the_sample_range(t):
    with pytest.raises(timebase.TimeRangeError, match="too far from the capture start"):
        timebase.to_samples(np.array([0.0, t]), 0.0, 1.0)


def test_to_samples_counts_from_the_origin_and_leaves_its_input_alone():
    t = np.array([-2e-6, -1e-6, 0.0])
    got = timebase.to_samples(t, -2e-6, 1_000_000)
    assert got.dtype == np.uint64 and got.tolist() == [0, 1, 2]
    assert t.tolist() == [-2e-6, -1e-6, 0.0]


@pytest.mark.parametrize("chunk", [1, 3, 4, 100])
@pytest.mark.parametrize("bad_at", [0, 2, 3, 4, 8, 9])
def test_drop_nonfinite_reports_the_first_bad_position_across_chunk_edges(
        monkeypatch, chunk, bad_at):
    monkeypatch.setattr(timebase, "_CHUNK", chunk)
    times = np.arange(10, dtype=np.float64)
    inf_at = 9 if bad_at != 9 else 8
    times[bad_at] = np.nan
    times[inf_at] = np.inf
    kept, gone, first = timebase.drop_nonfinite(times)
    assert (gone, first) == (2, min(bad_at, inf_at))
    assert np.isfinite(kept).all() and kept.size == 8


def test_drop_nonfinite_returns_a_finite_array_as_given():
    times = np.arange(5, dtype=np.float64)
    kept, gone, first = timebase.drop_nonfinite(times)
    assert kept is times and (gone, first) == (0, -1)


def test_require_finite_names_the_source_and_field():
    assert timebase.require_finite("2.5", "f.bin", "the begin time") == 2.5
    with pytest.raises(ValueError, match=r"f\.bin: the begin time is nan, not a time"):
        timebase.require_finite(float("nan"), "f.bin", "the begin time")


def test_the_same_sample_note_agrees_with_its_count():
    assert timebase.same_sample_note(1, 1000) == (
        "1 transition lands on the same sample as the one before at 1,000 Hz (the rate is "
        "coarser than the transitions)")
    assert timebase.same_sample_note(2, 1000) == (
        "2 transitions land on the same sample as the one before at 1,000 Hz (the rate is "
        "coarser than the transitions)")


def test_require_distinct_refuses_only_a_repeated_selection():
    timebase.require_distinct("f.csv", 1, 2, "column")
    with pytest.raises(ValueError, match=r"f\.csv: the clock and the data are the same "
                                         r"column \(1\); choose two different ones"):
        timebase.require_distinct("f.csv", 1, 1, "column")
