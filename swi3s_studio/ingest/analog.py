"""Analog → digital front-end shared by the scope ingest readers (.wfm, analog CSV).

A scope capture gives volts, not levels: each channel is thresholded with a
mid-rail Schmitt trigger (10% hysteresis) before it feeds the same edge-extraction
path as a Saleae digital export. `capture_from_analog` mirrors
`digital_csv.load_capture`'s edge extraction so both paths hand the decoder core
the same :class:`Capture` shape.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from .. import import_notes
from .capture import Capture
from .timebase import (
    MAX_RATE_HZ,
    TimeRangeError,
    rate_problem,
    repeats,
    same_sample_note,
    to_samples,
)

# Points per step of the chunked passes below (the time-order check and the Schmitt
# forward fill), which bounds their temporaries whatever the record length.
_CHUNK = 1 << 20

# Peak bytes per point of `capture_from_analog` beyond its three float64 inputs: the two
# uint8 level arrays (2), and the larger of the rate's float64 spacings (8, partitioned
# in place) and the threshold's percentile copy (8), with their masks.
FRONT_END_BYTES_PER_POINT = 12
# Bytes per point of one `_CHUNK` step of the Schmitt fill: the int8 states, two masks,
# and the int64 index with the arange it is built from (19).
_CHUNK_BYTES_PER_POINT = 19


def front_end_bytes(points: int) -> int:
    """Peak of `capture_from_analog` beyond its inputs, for `points` per channel: the
    per-point term and one chunk of the Schmitt fill. Measured with tracemalloc within
    these figures (about 11 per point, plus the chunk)."""
    points = int(points)
    return points * FRONT_END_BYTES_PER_POINT + min(points, _CHUNK) * _CHUNK_BYTES_PER_POINT


class AnalogTooLargeError(MemoryError):
    """Opening this analog capture is predicted to exhaust memory.

    Raised BEFORE the samples are read, like the digital loaders' guards. An analog
    capture is read whole, so the remedies are a shorter record or more RAM.
    """

    def __init__(self, path: str, points: int, predicted: int, budget: int):
        self.path = path
        self.points = int(points)
        self.predicted = int(predicted)
        self.budget = int(budget)
        MemoryError.__init__(
            self,
            f"{path}: about {points:,} points per channel would need roughly "
            f"{predicted / 1e9:.1f} GB, but only {budget / 1e9:.1f} GB is budgeted. "
            f"Export a shorter record, free memory, or pass max_bytes=0 to override.")


def check_budget(path: str, points: int, predicted: int, max_bytes) -> None:
    """Raise :class:`AnalogTooLargeError` when `predicted` bytes exceed the shared
    `memory_budget(max_bytes)`. `max_bytes` follows the .sal convention: None uses the
    computed budget, a positive value overrides it, and 0 opts out."""
    if max_bytes == 0:
        return
    from .saleae_sal import memory_budget  # ONE definition of the budget
    budget = memory_budget(max_bytes)
    if predicted > budget:
        raise AnalogTooLargeError(path, points, predicted, budget)


def auto_threshold(values: np.ndarray) -> Tuple[float, float]:
    """Pick a mid-rail (hi, lo) Schmitt threshold pair from the signal's own
    swing: rails are the 10th/90th percentile (robust to overshoot/ringing at the
    extremes), the threshold band is centered on their midpoint, and its total
    width is 10% of the rail-to-rail swing (the "10% hysteresis"). A degenerate
    (under `_MIN_SWING_V`) swing — a flat, dead or noise-only channel — falls back to
    (0.6, 0.4), a generic normalized-logic split.

    The degeneracy check uses the min/max range, not the percentile rails: a
    minority level under ~10% duty cycle puts both percentile rails on the
    majority level. A live channel whose percentile rails collapse uses min/max
    as its rails."""
    return _auto_threshold(values)[:2]


_FALLBACK_THRESHOLD = (0.6, 0.4)

# Smallest rail-to-rail swing, in volts, that is read as a signal; a channel that varies
# by less is noise or a dead channel and takes the fallback thresholds.
_MIN_SWING_V = 1e-6


def _auto_threshold(values: np.ndarray) -> Tuple[float, float, bool]:
    """`auto_threshold` plus whether it fell back to `_FALLBACK_THRESHOLD`. Samples that
    are NaN are left out of the rails; the fallback is for a channel with no number in
    it or no swing."""
    v = np.asarray(values, dtype=np.float64)
    owned = False
    if v.size and bool(np.isnan(np.min(v))):
        v = v[~np.isnan(v)]              # the rails are those of the samples that are numbers
        owned = True
    full_range = float(np.max(v) - np.min(v)) if v.size else 0.0
    if full_range < _MIN_SWING_V or not np.isfinite(full_range):
        return _FALLBACK_THRESHOLD[0], _FALLBACK_THRESHOLD[1], True
    # One copy of v, unless v is already a private copy the percentile may reorder.
    lo_rail, hi_rail = (float(x) for x in np.percentile(v, [10, 90], overwrite_input=owned))
    swing = hi_rail - lo_rail
    if swing <= 0 or not np.isfinite(swing):
        # Percentile rails collapsed onto one level (low-duty-cycle minority
        # level) even though the channel does toggle — fall back to the true
        # min/max as the rails so the minority pulses still cross threshold.
        lo_rail, hi_rail = float(np.min(v)), float(np.max(v))
        swing = full_range
    mid = (lo_rail + hi_rail) / 2.0
    band = 0.10 * swing
    return mid + band / 2.0, mid - band / 2.0, False


def _checked_pair(given, field: str, pre: str = "") -> Tuple[float, float]:
    """`given` as a (hi, lo) pair of finite floats with hi >= lo. ValueError naming
    `field` otherwise."""
    try:
        hi, lo = (float(x) for x in given)
    except (TypeError, ValueError):
        hi = lo = float("nan")
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi < lo:
        raise ValueError(f"{pre}{field} must be two finite numbers (high, low) with high at "
                         f"least low (got {given!r})")
    return hi, lo


def _varies(v: np.ndarray) -> bool:
    """True when the numbers of `v` are not all one value."""
    return bool(v.size) and not bool(np.all(np.isnan(v))) and \
        float(np.nanmax(v)) > float(np.nanmin(v))


def _non_numbers(v: np.ndarray) -> int:
    """NaN samples in `v`, counted `_CHUNK` at a time."""
    return sum(int(np.count_nonzero(np.isnan(v[i:i + _CHUNK])))
               for i in range(0, v.size, _CHUNK))


def schmitt_levels(values: np.ndarray, hi: float, lo: float) -> np.ndarray:
    """Threshold `values` into 0/1 levels with hysteresis: rises to 1 once v > hi,
    falls to 0 once v < lo, and holds its last state in between (the dead band
    that rejects near-threshold noise). The initial state is seeded from the
    first sample against the band's midpoint, not the hysteresis rule (there is
    no "previous state" yet).

    Vectorized as a forward-fill: samples inside the dead band are undecided (-1)
    and take on the most recent decided state via `np.maximum.accumulate` over an
    index array. Runs `_CHUNK` samples at a time, carrying the last state across
    each boundary, so the temporaries stay bounded."""
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return np.empty(0, dtype=np.uint8)
    out = np.empty(v.size, dtype=np.uint8)
    mid = (hi + lo) / 2.0
    state = 1 if v[0] > mid else 0       # used only if sample 0 is in the dead band
    for i in range(0, v.size, _CHUNK):
        part = v[i:i + _CHUNK]
        decided = np.full(part.shape, -1, dtype=np.int8)
        decided[part > hi] = 1
        decided[part < lo] = 0
        if decided[0] < 0:
            decided[0] = state           # the carried state (the seed, for sample 0)
        idx = np.where(decided >= 0, np.arange(part.size), 0)
        np.maximum.accumulate(idx, out=idx)
        out[i:i + part.size] = decided[idx]
        state = int(out[i + part.size - 1])
    return out


def _ascending(t: np.ndarray) -> bool:
    """True if `t` never decreases, checked `_CHUNK` steps at a time."""
    for i in range(0, max(0, t.size - 1), _CHUNK):
        j = min(i + _CHUNK, t.size - 1)
        if np.any(t[i + 1:j + 1] < t[i:j]):
            return False
    return True


def _median_positive_step(t: np.ndarray) -> float:
    """The median of the positive steps of `t`, or 0 if there are none.

    Equal to ``np.median(d[d > 0])`` for ``d = np.diff(t)``, found by partitioning
    the one spacing array in place rather than copying the positive steps twice. A NaN
    step (a NaN in `t`) is counted as a non-positive step, so the median is of the
    valid positive steps only."""
    d = np.diff(t)
    d[np.isnan(d)] = 0.0
    nonpos = int(np.count_nonzero(d <= 0))
    m = d.size - nonpos
    if m <= 0:
        return 0.0
    k = nonpos + m // 2                  # the positive steps sort after the rest
    if m % 2:
        d.partition(k)
        return float(d[k])
    d.partition([k - 1, k])
    return float(np.mean(d[k - 1:k + 1]))


def capture_from_analog(time: np.ndarray, clock_v: np.ndarray, data_v: np.ndarray,
                        sample_rate_hz: Optional[int] = None,
                        clock_thresh: Optional[Tuple[float, float]] = None,
                        data_thresh: Optional[Tuple[float, float]] = None,
                        auto_clock: bool = False, source: str = "") -> Capture:
    """Build a :class:`Capture` from two analog channels (clock, data) sharing a
    `time` axis (seconds). Each channel is thresholded independently (auto mid-rail
    + hysteresis if its `*_thresh` is not supplied), then edges are extracted the
    same way as `digital_csv.load_capture`: sort-by-time if needed, rebase to
    `origin = time.min()` (captures start at a negative pre-trigger time), then a
    level-change sample is `round((t[change] - origin) * rate)`. `sample_rate_hz`
    is inferred from the median timestamp spacing if not given.

    With `auto_clock`, the forwarded clock is assigned by transition count rather
    than argument order — the clock toggles every UI, so it is the busier of the
    two thresholded channels (the rule of `CaptureProbe.busiest_pair` and of the
    `auto_clock` option of the .sal and VCD readers; the two args are swapped when
    `data_v` turns out busier). If a per-channel threshold was supplied it travels with
    its channel through the swap.

    A given threshold pair that is not two finite numbers with high at least low raises
    ValueError naming the field.

    `source` names the file(s) in the import warnings this raises (import_notes): rows
    sorted into time order, a defaulted rate, a given rate outside 1 Hz to
    `timebase.MAX_RATE_HZ`, a channel whose thresholds could not be detected, a channel
    that varies but never crosses its thresholds, samples
    that are not numbers, and transitions that land on the same sample as the one before.
    When the rate is taken from the time axis, an axis that steps by less than
    `1 / MAX_RATE_HZ` s raises ValueError; a time axis that gives a rate below 1 Hz is
    warned about and 1 MHz is assumed."""
    pre = f"{source}: " if source else ""
    t = np.asarray(time, dtype=np.float64)
    cv = np.asarray(clock_v, dtype=np.float64)
    dv = np.asarray(data_v, dtype=np.float64)
    if t.size < 2:
        raise ValueError("capture_from_analog: not enough samples to form a capture")
    if not (t.size == cv.size == dv.size):
        # Session.from_wfm reads the clock/data .wfm files independently, so
        # their record lengths can differ.
        raise ValueError(
            f"capture_from_analog: time/clock/data length mismatch "
            f"({t.size} vs {cv.size} vs {dv.size})"
        )

    # A sample whose time is nan or inf has no place on the timeline; it is dropped.
    finite = np.isfinite(t)
    if not finite.all():
        gone = int(t.size - np.count_nonzero(finite))
        import_notes.warn(source, f"{gone:,} sample{'s' if gone != 1 else ''} with a time "
                                  f"that is not a finite number "
                                  f"{'were' if gone != 1 else 'was'} skipped")
        t, cv, dv = t[finite], cv[finite], dv[finite]
        if t.size < 2:
            raise ValueError(f"{pre}fewer than two samples have a finite time")

    # Sort only when the timeline is out of order (as digital_csv does); the
    # np.diff edge detection below needs ascending time.
    if not _ascending(t):
        import_notes.warn(source, "the samples are not in time order; they are sorted "
                                  "by time")
        order = np.argsort(t, kind="stable")
        t, cv, dv = t[order], cv[order], dv[order]

    rate = sample_rate_hz
    if rate:
        problem = rate_problem(rate)
        if problem:
            import_notes.warn(source, f"the sample rate {rate!r} Hz {problem}; it is "
                                      f"taken from the time axis instead")
            rate = None
    if not rate:
        med = _median_positive_step(t)
        per_s = 1.0 / med if med > 0 else 0.0
        if per_s > MAX_RATE_HZ:
            raise ValueError(f"{pre}the time axis steps by {med:g} s, a sample rate above "
                             f"{MAX_RATE_HZ:,.0f} Hz; set the sample rate in the Open "
                             f"Capture dialog")
        rate = int(round(per_s))
        if rate < 1:
            rate = 1_000_000
            if per_s > 0:
                import_notes.warn(source, f"the time axis gives a sample rate of "
                                          f"{per_s:g} Hz, below 1 Hz; 1 MHz is assumed")
            else:
                import_notes.warn(source, "the time axis does not give a sample rate; "
                                          "1 MHz is assumed")

    def thresholds(v, given, field):
        """(hi, lo, whether they could not be detected)."""
        if given is not None:
            return (*_checked_pair(given, field, pre), False)
        return _auto_threshold(v)

    c_hi, c_lo, c_flat = thresholds(cv, clock_thresh, "clock_thresh")
    d_hi, d_lo, d_flat = thresholds(dv, data_thresh, "data_thresh")
    c_levels = schmitt_levels(cv, c_hi, c_lo)
    d_levels = schmitt_levels(dv, d_hi, d_lo)
    clock_levels, data_levels = c_levels, d_levels

    # Auto-orient: the forwarded clock toggles every UI, so it is the busier of
    # the two thresholded channels. Swap the level arrays (not just the args) when
    # `data` turns out busier, so the rest of the function is orientation-agnostic.
    roles = ("clock", "data")
    if auto_clock:
        def _transitions(lv: np.ndarray) -> int:
            return int(np.count_nonzero(np.diff(lv) != 0)) if lv.size >= 2 else 0
        if _transitions(data_levels) > _transitions(clock_levels):
            clock_levels, data_levels = data_levels, clock_levels
            roles = ("data", "clock")        # the roles of the first and second argument

    # The warnings name each channel by the role it has after the orientation.
    for flat, hi, lo, role in ((c_flat, c_hi, c_lo, roles[0]), (d_flat, d_hi, d_lo, roles[1])):
        if flat:
            import_notes.warn(source, f"the {role} channel's thresholds could not be "
                                      f"detected (it is flat or holds non-numbers); "
                                      f"{hi:g} V / {lo:g} V are used")
    for v, lv, hi, lo, role in ((cv, c_levels, c_hi, c_lo, roles[0]),
                                (dv, d_levels, d_hi, d_lo, roles[1])):
        if lv.size and not np.any(lv != lv[0]) and _varies(v):
            import_notes.warn(source, f"the {role} channel never crosses its thresholds "
                                      f"{hi:g} V / {lo:g} V (its samples span "
                                      f"{float(np.nanmin(v)):g} V to {float(np.nanmax(v)):g} V); "
                                      f"it has no transitions")
    for v, role in ((cv, roles[0]), (dv, roles[1])):
        bad = _non_numbers(v)
        if bad:
            import_notes.warn(source, f"{bad:,} {role} sample{'s' if bad != 1 else ''} "
                                      f"{'are' if bad != 1 else 'is'} not a number; each "
                                      f"holds the level before it")

    # Rebase to the earliest timestamp so sample 0 == capture start and no time
    # goes negative (a negative * rate would wrap through the uint64 cast).
    origin = float(t.min())

    def edges(levels: np.ndarray) -> Tuple[np.ndarray, bool]:
        change = np.flatnonzero(np.diff(levels) != 0) + 1
        try:
            samples = to_samples(t[change], origin, rate)
        except TimeRangeError as e:
            raise ValueError(f"{pre}{e}") from None
        return samples, bool(levels[0])

    ce, ic = edges(clock_levels)
    de, id_ = edges(data_levels)
    same = repeats(ce) + repeats(de)
    if same:
        import_notes.warn(source, same_sample_note(same, rate))
    return Capture(clock_edges=ce, data_edges=de,
                   initial_clock=ic, initial_data=id_, sample_rate_hz=int(rate))
