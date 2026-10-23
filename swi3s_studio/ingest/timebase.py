"""The sample rate of a capture that stores transition TIMES but no rate.

A digital CSV and a version-0 ``.bin`` hold absolute times in seconds. Every time is a
whole number of sample periods from capture start, so the rate is the coarsest grid
that every timestamp sits on. The finest spacing alone is not the rate: an export with
one row per transition has no two rows closer than the shortest gap between edges,
which on a forwarded-clock capture is many samples.

`rate_from_times` finds that grid. With ``d`` the finest spacing, the period is
``d / k`` for some whole ``k``; candidates are tried from ``k = 1`` up. Each candidate
is fitted over doubling prefixes of the timestamps: a prefix assigns each timestamp
its nearest whole sample (each within `_ASSIGN_TOLERANCE`), and the period is then
re-estimated from the farthest of them, so a starting estimate taken from timestamps
printed to a coarser precision than the period still converges. The first fitted
candidate whose grid holds every timestamp to `_GRID_TOLERANCE` of a sample wins. The
result is rounded to the kHz, then the Hz, taking the first rounding the grid holds.
"""
from __future__ import annotations

import numpy as np

# Largest distance, in samples, a timestamp may sit from the accepted grid. Timestamps
# printed to 1 ns at a period that is not a whole number of ns are off by up to 0.5 ns,
# 0.012 of a sample at 24 MHz. A grid k times too coarse puts timestamps a multiple of
# 1/k of a sample off.
_GRID_TOLERANCE = 0.02

# Largest k (finest spacing / period) tried. When the shortest gap between timestamps
# is more samples than this, the rate falls back to the finest spacing.
_MAX_SAMPLES_PER_GAP = 1 << 14

# Largest distance, in samples, a timestamp may sit from the grid while a candidate's
# period is still being fitted. Below 0.5, so every timestamp keeps its nearest sample.
_ASSIGN_TOLERANCE = 0.25

# Timestamps checked at the first stage; each later stage checks twice as many.
_FIRST_STAGE = 2


def _on_grid(off: np.ndarray, rate: float, tol: float = _GRID_TOLERANCE) -> bool:
    """True if every offset in `off` (seconds) is within `tol` samples of a whole one.
    Two temporaries the size of `off`, whatever its length."""
    pos = off * rate
    whole = np.rint(pos)
    pos -= whole
    del whole
    np.abs(pos, out=pos)
    return bool(pos.max() <= tol)


def _fit(off: np.ndarray, rate: float) -> float:
    """The fitted rate if `off` sits on a grid near `rate`, else 0.

    Checks prefixes of 2, 4, 8, ... offsets to `_ASSIGN_TOLERANCE`. After each passing
    stage the rate is re-estimated as (whole samples at the farthest offset) / (that
    offset), so an error in the starting estimate does not accumulate across a long
    capture. The caller checks the fitted rate to `_GRID_TOLERANCE`.
    """
    m = _FIRST_STAGE
    n = off.size
    while True:
        part = off[:min(m, n)]
        if not _on_grid(part, rate, _ASSIGN_TOLERANCE):
            return 0.0
        far = float(part[-1])
        if far > 0:
            rate = float(np.rint(far * rate)) / far
        if m >= n:
            return rate
        m *= 2


def rate_from_times(times: np.ndarray) -> int:
    """The sample rate (Hz) of transition `times` (seconds, any order), or 0.

    Returns 0 when fewer than two distinct finite times are given. When no grid of
    up to `_MAX_SAMPLES_PER_GAP` samples per finest spacing holds every time, returns
    the reciprocal of the finest spacing, rounded to the kHz.
    """
    t = np.asarray(times, dtype=np.float64)
    off = np.unique(t[np.isfinite(t)])
    if off.size < 2:
        return 0
    off -= off[0]                     # in place: `off` is unique's own sorted copy
    finest = float(np.min(np.diff(off)))
    if finest <= 0:
        return 0
    for k in range(1, _MAX_SAMPLES_PER_GAP + 1):
        rate = _fit(off, k / finest)
        if rate <= 0 or not _on_grid(off, rate):
            continue
        for rounded in (round(rate / 1000.0) * 1000, round(rate)):
            if rounded > 0 and _on_grid(off, float(rounded)):
                return int(rounded)
        return int(round(rate))
    return int(round(1.0 / finest / 1000.0)) * 1000
