"""Analog → digital front-end shared by the scope ingest readers (.wfm, analog CSV).

A scope capture gives volts, not levels: each channel needs a threshold before it
can feed the same edge-extraction path as a Saleae digital export. We use a
mid-rail threshold with 10% hysteresis (a Schmitt trigger) rather than a single
compare level, so a channel that lingers near the threshold (slow edges, ringing)
doesn't chatter into spurious transitions. `capture_from_analog` then mirrors
`digital_csv.load_capture`'s edge extraction so both paths hand the decoder core
the same :class:`Capture` shape.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from .capture import Capture


def auto_threshold(values: np.ndarray) -> Tuple[float, float]:
    """Pick a mid-rail (hi, lo) Schmitt threshold pair from the signal's own
    swing: rails are the 10th/90th percentile (robust to overshoot/ringing at the
    extremes), the threshold band is centered on their midpoint, and its total
    width is 10% of the rail-to-rail swing (the "10% hysteresis"). A degenerate
    (near-zero) swing — a flat or dead channel — falls back to (0.6, 0.4), a
    generic normalized-logic split that at least avoids div-by-zero chatter.

    The degeneracy check itself uses min/max, not the 10th/90th percentile: a
    minority logic level occupying less than ~10% duty cycle (a brief pulse on
    an otherwise-idle line) pushes BOTH percentile rails onto the majority
    level, making `swing` look zero for a line that is genuinely toggling. Only
    the true full-range swing is allowed to declare the channel dead; once it's
    confirmed live, the percentile rails are still used for the actual
    thresholds so a normal signal's overshoot/ringing is ignored as before."""
    v = np.asarray(values, dtype=np.float64)
    full_range = float(np.max(v) - np.min(v)) if v.size else 0.0
    if full_range <= 0 or not np.isfinite(full_range):
        return 0.6, 0.4
    lo_rail = float(np.percentile(v, 10))
    hi_rail = float(np.percentile(v, 90))
    swing = hi_rail - lo_rail
    if swing <= 0 or not np.isfinite(swing):
        # Percentile rails collapsed onto one level (low-duty-cycle minority
        # level) even though the channel does toggle — fall back to the true
        # min/max as the rails so the minority pulses still cross threshold.
        lo_rail, hi_rail = float(np.min(v)), float(np.max(v))
        swing = full_range
    mid = (lo_rail + hi_rail) / 2.0
    band = 0.10 * swing
    return mid + band / 2.0, mid - band / 2.0


def schmitt_levels(values: np.ndarray, hi: float, lo: float) -> np.ndarray:
    """Threshold `values` into 0/1 levels with hysteresis: rises to 1 once v > hi,
    falls to 0 once v < lo, and holds its last state in between (the dead band
    that rejects near-threshold noise). The initial state is seeded from the
    first sample against the band's midpoint, not the hysteresis rule (there is
    no "previous state" yet).

    Vectorized as a forward-fill rather than a Python per-sample loop (scope
    exports run to hundreds of thousands of samples): samples inside the dead
    band are undecided (-1) and take on the most recent decided state via
    `np.maximum.accumulate` over an index array — the same trick as a "last
    valid value" forward-fill."""
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return np.empty(0, dtype=np.uint8)
    decided = np.full(v.shape, -1, dtype=np.int8)
    decided[v > hi] = 1
    decided[v < lo] = 0
    if decided[0] < 0:
        mid = (hi + lo) / 2.0
        decided[0] = 1 if v[0] > mid else 0
    idx = np.where(decided >= 0, np.arange(v.size), 0)
    np.maximum.accumulate(idx, out=idx)
    return decided[idx].astype(np.uint8)


def channel_activity(values: np.ndarray, hi: float, lo: float) -> int:
    """Number of level transitions after Schmitt-thresholding `values`. Used to
    auto-pick the forwarded clock among two analog channels: the clock toggles
    every UI, so it is always the busier of the two (mirrors
    `digital_csv.channel_transition_counts`'s role for digital exports)."""
    levels = schmitt_levels(values, hi, lo)
    if levels.size < 2:
        return 0
    return int(np.count_nonzero(np.diff(levels) != 0))


def capture_from_analog(time: np.ndarray, clock_v: np.ndarray, data_v: np.ndarray,
                        sample_rate_hz: Optional[int] = None,
                        clock_thresh: Optional[Tuple[float, float]] = None,
                        data_thresh: Optional[Tuple[float, float]] = None,
                        auto_clock: bool = False) -> Capture:
    """Build a :class:`Capture` from two analog channels (clock, data) sharing a
    `time` axis (seconds). Each channel is thresholded independently (auto mid-rail
    + hysteresis if its `*_thresh` is not supplied), then edges are extracted the
    same way as `digital_csv.load_capture`: sort-by-time if needed, rebase to
    `origin = time.min()` (captures start at a negative pre-trigger time), then a
    level-change sample is `round((t[change] - origin) * rate)`. `sample_rate_hz`
    is inferred from the median timestamp spacing if not given (more robust to a
    single noisy sample than the min-spacing digital captures use, since analog
    scope exports don't have a mix of transition- and sample-driven timestamps).

    With `auto_clock`, the forwarded clock is assigned by transition count rather
    than argument order — the clock toggles every UI, so it is the busier of the
    two thresholded channels (the same rule `_open_bin`/digital-CSV use; the two
    args are swapped when `data_v` turns out busier). If a per-channel threshold
    was supplied it travels with its channel through the swap."""
    t = np.asarray(time, dtype=np.float64)
    cv = np.asarray(clock_v, dtype=np.float64)
    dv = np.asarray(data_v, dtype=np.float64)
    if t.size < 2:
        raise ValueError("capture_from_analog: not enough samples to form a capture")
    if not (t.size == cv.size == dv.size):
        # Session.from_wfm reads the clock/data .wfm files independently, so a
        # record-length mismatch is a real (if rare) input error, not a bug in
        # this function — catch it here with a clear message rather than let a
        # short array silently misalign timestamps or a long one raise a
        # cryptic IndexError deep in the `edges` closure below.
        raise ValueError(
            f"capture_from_analog: time/clock/data length mismatch "
            f"({t.size} vs {cv.size} vs {dv.size})"
        )

    # Analog exports are recorded in acquisition order, but guard the sort exactly
    # like digital_csv so an already-ascending capture (the common case) pays
    # nothing and np.diff-based edge detection below sees an ordered timeline.
    if np.any(np.diff(t) < 0):
        order = np.argsort(t, kind="stable")
        t, cv, dv = t[order], cv[order], dv[order]

    rate = sample_rate_hz
    if not rate:
        dt = np.diff(t)
        dt = dt[dt > 0]
        rate = int(round(1.0 / float(np.median(dt)))) if dt.size else 1_000_000

    c_hi, c_lo = clock_thresh if clock_thresh is not None else auto_threshold(cv)
    d_hi, d_lo = data_thresh if data_thresh is not None else auto_threshold(dv)
    clock_levels = schmitt_levels(cv, c_hi, c_lo)
    data_levels = schmitt_levels(dv, d_hi, d_lo)

    # Auto-orient: the forwarded clock toggles every UI, so it is the busier of
    # the two thresholded channels. Swap the level arrays (not just the args) when
    # `data` turns out busier, so the rest of the function is orientation-agnostic.
    if auto_clock:
        def _transitions(lv: np.ndarray) -> int:
            return int(np.count_nonzero(np.diff(lv) != 0)) if lv.size >= 2 else 0
        if _transitions(data_levels) > _transitions(clock_levels):
            clock_levels, data_levels = data_levels, clock_levels

    # Rebase to the earliest timestamp so sample 0 == capture start and no time
    # goes negative (a negative * rate would wrap through the uint64 cast).
    origin = float(t.min())

    def edges(levels: np.ndarray) -> Tuple[np.ndarray, bool]:
        change = np.flatnonzero(np.diff(levels) != 0) + 1
        samples = np.rint((t[change] - origin) * rate).astype(np.uint64)
        return samples, bool(levels[0])

    ce, ic = edges(clock_levels)
    de, id_ = edges(data_levels)
    return Capture(clock_edges=ce, data_edges=de,
                   initial_clock=ic, initial_data=id_, sample_rate_hz=int(rate))
