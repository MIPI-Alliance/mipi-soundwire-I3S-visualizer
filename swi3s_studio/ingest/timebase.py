"""The sample rate of a capture that stores transition TIMES but no rate.

A digital CSV and a version-0 ``.bin`` hold absolute times in seconds. Every time is a
whole number of sample periods from capture start, so the rate is the coarsest grid
that every timestamp sits on. The finest spacing alone is not the rate: an export with
one row per transition has no two rows closer than the shortest gap between edges,
which on a forwarded-clock capture is many samples.

`rate_from_times` finds that grid. With ``d`` the finest spacing, the period is
``d / k`` for some whole ``k``; candidates are tried from ``k = 1`` up. Each candidate
is fitted over doubling runs of the timestamps around the finest spacing: a run assigns
each timestamp its nearest whole sample from the run's anchor, the first timestamp of the
finest spacing (each within `_ASSIGN_TOLERANCE`), and the period is then re-estimated
from the farthest of them, so a starting estimate taken from timestamps printed to a
coarser precision than the period, or a finest spacing measured far from the first
timestamp (an idle lead-in), still converges. The first fitted
candidate whose grid holds every timestamp to `_GRID_TOLERANCE` of a sample wins. The
result is rounded to the kHz, then the Hz, taking the first rounding the grid holds.

`to_samples` is the one conversion of times to sample indices: a time that is not a
sample index at the rate raises `TimeRangeError`, and `drop_nonfinite` and
`require_finite` are the rules for a time or a header start time that is not a number.
`require_distinct` is the one refusal of a clock and a data selection that name the same
line. `rate_problem` and `check_rate` are the one range rule for a sample rate: from 1 Hz to
`MAX_RATE_HZ`. `MAX_LINE_CHARS` is the one limit on a CSV line the digital reader, the
probe's first-line and head reads and the analog-CSV detection read as a row (the probe's
analog data-row scan, the analog reader and the DLV rate scan read rows through `csv`
unbounded), and `quoted` the one form of a file's cell in an import warning. `text_csv` is
the one wrapper that turns bytes that are not text into a ValueError naming the file.
"""
from __future__ import annotations

import csv
import functools
from typing import Tuple

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


def _on_grid(off: np.ndarray, rate: float, tol: float = _GRID_TOLERANCE,
             origin: float = 0.0) -> bool:
    """True if every offset in `off` (seconds) is within `tol` samples of a whole number of
    samples from `origin`. Two temporaries the size of `off`, whatever its length."""
    pos = off * rate
    if origin:
        pos -= origin * rate
    whole = np.rint(pos)
    pos -= whole
    del whole
    np.abs(pos, out=pos)
    return bool(pos.max() <= tol)


def _fit(off: np.ndarray, rate: float, at: int = 0) -> float:
    """The fitted rate if `off` (ascending) sits on a grid near `rate`, else 0.

    Checks runs of offsets around `off[at]` (the first of the finest spacing), measured
    from it, to `_ASSIGN_TOLERANCE`: first the finest pair, then each stage every offset
    within twice the run's reach, or the nearest one beyond it when none is. A stage that
    fails at the estimate is tried at the estimate rounded to the kHz, then the Hz. After
    each passing stage the rate is re-estimated as (whole samples to the farthest offset of
    the run) / (that distance), so an error in the starting estimate does not accumulate
    across a long capture or an idle lead-in. The caller checks the fitted rate to
    `_GRID_TOLERANCE`.
    """
    n = off.size
    anchor = float(off[at])
    lo, hi = at, min(n, at + _FIRST_STAGE)
    while True:
        part = off[lo:hi]
        for r in (rate, float(round(rate / 1000.0) * 1000), float(round(rate))):
            if r > 0 and _on_grid(part, r, _ASSIGN_TOLERANCE, anchor):
                rate = r
                break
        else:
            return 0.0
        far = max(float(off[hi - 1]) - anchor, anchor - float(off[lo]))
        if far > 0:
            rate = float(np.rint(far * rate)) / far
        if lo == 0 and hi == n:
            return rate
        left = anchor - float(off[lo - 1]) if lo else np.inf
        right = float(off[hi]) - anchor if hi < n else np.inf
        reach = max(2.0 * far, min(left, right))
        # Each stage takes at least the nearest offset outside the run, whatever the
        # rounding of anchor -/+ reach.
        new_lo = int(np.searchsorted(off, anchor - reach, side="left"))
        new_hi = int(np.searchsorted(off, anchor + reach, side="right"))
        lo = min(new_lo, lo - 1) if left <= reach else min(new_lo, lo)
        hi = max(new_hi, hi + 1) if right <= reach else max(new_hi, hi)


# Divisors j of a found rate tried by `coarser_grid_note`, and the share of timestamps that
# may sit off rate/j for it to be named (at least one).
_COARSER_MAX_DIVISOR = 64
_COARSER_MAX_SHARE = 1e-3


def coarser_grid_note(times: np.ndarray, rate: int) -> str:
    """The import warning for `times` (seconds) whose `rate` grid is j times a coarser one
    (j from 2 to `_COARSER_MAX_DIVISOR`) that all but a few of them sit on: at most
    `_COARSER_MAX_SHARE` of them, and at least one. A few timestamps printed off the
    analyzer's grid make `rate_from_times` find this finer grid. "" when there is none;
    the largest such j is named."""
    t = np.asarray(times, dtype=np.float64)
    t = t[np.isfinite(t)]
    if t.size < 2 or rate <= 0:
        return ""
    pos = np.rint((t - t.min()) * float(rate)).astype(np.int64)
    limit = max(1, int(t.size * _COARSER_MAX_SHARE))
    for j in range(_COARSER_MAX_DIVISOR, 1, -1):
        off = int(np.count_nonzero(pos % j))
        if 0 < off <= limit:
            coarse = f"{rate // j:,}" if rate % j == 0 else f"{rate / j:,.3f}"
            return (f"{off:,} of the {t.size:,} timestamps read for the rate "
                    f"{'is' if off == 1 else 'are'} off the {coarse} Hz grid the rest "
                    f"sit on; the capture is read at {rate:,} Hz, the grid that holds them "
                    f"all")
    return ""


def rate_from_times(times: np.ndarray) -> int:
    """The sample rate (Hz) of transition `times` (seconds, any order), or 0.

    Returns 0 when fewer than two distinct finite times are given. When no grid of
    up to `_MAX_SAMPLES_PER_GAP` samples per finest spacing holds every time, returns
    the reciprocal of the finest spacing, rounded to 4 significant figures (at least 1 Hz).
    A candidate grid whose sample index at the last time is not a finite float ends the
    search. A finest spacing whose reciprocal is not a finite float (under about 5.6e-309
    s) gives `_RATE_CEILING`, above `MAX_RATE_HZ`, which every reader refuses.
    """
    t = np.asarray(times, dtype=np.float64)
    off = np.unique(t[np.isfinite(t)])
    if off.size < 2:
        return 0
    off -= off[0]                     # in place: `off` is unique's own sorted copy
    gaps = np.diff(off)
    at = int(np.argmin(gaps))
    finest = float(gaps[at])
    del gaps
    if finest <= 0:
        return 0
    last = float(off[-1])
    for k in range(1, _MAX_SAMPLES_PER_GAP + 1):
        if not np.isfinite(last * (k / finest)):
            break
        rate = _fit(off, k / finest, at)
        if rate <= 0 or not _on_grid(off, rate):
            continue
        for rounded in (round(rate / 1000.0) * 1000, round(rate)):
            if rounded > 0 and _on_grid(off, float(rounded)):
                return int(rounded)
        return int(round(rate))
    per_s = 1.0 / finest
    if not np.isfinite(per_s):
        return _RATE_CEILING
    return max(1, int(round(float(f"{per_s:.4g}"))))


_MAX_SAMPLE = float(2 ** 63)       # a sample index must stay below this

# Largest sample rate (Hz) any reader accepts: a femtosecond sample, the finest time unit
# a capture format names. A larger rate does not fit the native sample sources.
MAX_RATE_HZ = 1e15

# What `rate_from_times` returns for a spacing finer than float64 can invert.
_RATE_CEILING = 10 ** 30

# The longest line, in characters, the digital CSV reader, the probe's first-line and head
# reads and the analog-CSV detection read as one row, its line ending not counted.
MAX_LINE_CHARS = 1 << 16
_CHUNK = 1 << 20

# The most characters of a file's cell an import warning quotes, as the native loader does.
QUOTED_CHARS = 60


def clipped(text: str) -> str:
    """`text` as an import warning shows it: whole when it has at most `QUOTED_CHARS`
    characters, else its first `QUOTED_CHARS` - 1 characters and an ellipsis."""
    return text if len(text) <= QUOTED_CHARS else text[:QUOTED_CHARS - 1] + "\u2026"


def quoted(text: str) -> str:
    """`text` as an import warning quotes it: the repr of `clipped(text)`."""
    return repr(clipped(text))


def text_csv(fn):
    """Decorate a reader of a CSV (digital or analog) so that bytes that are not UTF-8 text, or text
    the csv module rejects, raise ValueError naming the file (the first argument)."""
    @functools.wraps(fn)
    def wrapper(path, *args, **kwargs):
        try:
            return fn(path, *args, **kwargs)
        except (UnicodeDecodeError, csv.Error) as e:
            raise ValueError(f"{path}: not a text CSV ({e})") from None
    return wrapper


class TimeRangeError(ValueError):
    """A time too far from the capture start to be a sample index at the rate."""


def to_samples(t: np.ndarray, origin: float, rate: float) -> np.ndarray:
    """round((t - origin) * rate) as uint64 sample indices. Raises `TimeRangeError` for
    a value outside [0, 2**63) (a negative, huge or non-finite one), which the cast would
    wrap or saturate. `t` is not modified."""
    with np.errstate(over="ignore", invalid="ignore"):   # an overflow is the range error below
        x = np.asarray(t, dtype=np.float64) - origin
        x *= rate
        np.rint(x, out=x)
        ok = (x >= 0) & (x < _MAX_SAMPLE)
    if not bool(ok.all()):
        far = float(np.asarray(t)[int(np.flatnonzero(~ok)[0])])
        raise TimeRangeError(f"the time {far:g} s is too far from the capture start "
                             f"({origin:g} s) to be a sample at {rate:,.0f} Hz")
    return x.astype(np.uint64)


def rate_problem(rate) -> str:
    """Why `rate` (Hz) is not a usable sample rate: "is not a number", "is below 1 Hz" or
    "is above 1e+15 Hz"; "" when it is one."""
    try:
        r = float(rate)
    except OverflowError:                    # an integer beyond float64
        return f"is above {MAX_RATE_HZ:g} Hz"
    except (TypeError, ValueError):
        return "is not a number"
    if r != r:
        return "is not a number"
    if r < 1:
        return "is below 1 Hz"
    if r > MAX_RATE_HZ:
        return f"is above {MAX_RATE_HZ:g} Hz"
    return ""


def require_distinct(source: str, clock, data, what: str) -> None:
    """Raise ValueError naming `source` when the clock and the data selection are the same
    `what` ("column", "channel", "signal" or "file"): one line cannot be both."""
    if clock == data:
        raise ValueError(f"{source}: the clock and the data are the same {what} "
                         f"({clock!r}); choose two different ones")


def check_rate(rate, source: str) -> None:
    """Raise ValueError naming `source` unless `rate` (Hz) is from 1 Hz to `MAX_RATE_HZ`."""
    if rate_problem(rate):
        raise ValueError(f"{source}: the sample rate must be at least 1 Hz and at most "
                         f"{MAX_RATE_HZ:,.0f} Hz (got {rate!r}); set it in the Open "
                         f"Capture dialog")


def repeats(edges: np.ndarray) -> int:
    """How many edges of ascending `edges` sit on the same sample as the edge before,
    counted `_CHUNK` at a time."""
    n = 0
    for i in range(0, max(0, edges.size - 1), _CHUNK):
        j = min(i + _CHUNK, edges.size - 1)
        n += int(np.count_nonzero(edges[i + 1:j + 1] == edges[i:j]))
    return n


def same_sample_note(count: int, rate) -> str:
    """The import warning for `count` transitions that `repeats` found at `rate` Hz."""
    one = count == 1
    return (f"{count:,} transition{'' if one else 's'} land{'s' if one else ''} on the same "
            f"sample as the one before at {int(rate):,} Hz (the rate is coarser than the "
            f"transitions)")


def drop_nonfinite(times: np.ndarray) -> Tuple[np.ndarray, int, int]:
    """(`times` without its nan and +-inf entries, how many were dropped, the position
    of the first one or -1). The array is returned as given when every entry is finite;
    the check runs `_CHUNK` entries at a time."""
    for i in range(0, times.size, _CHUNK):
        if not bool(np.isfinite(times[i:i + _CHUNK]).all()):
            ok = np.isfinite(times)
            return times[ok], int(times.size - np.count_nonzero(ok)), int(np.flatnonzero(~ok)[0])
    return times, 0, -1


def require_finite(value: float, source: str, field: str) -> float:
    """`value` as a float; ValueError naming `source` and `field` if it is nan or inf."""
    v = float(value)
    if not np.isfinite(v):
        raise ValueError(f"{source}: {field} is {v:g}, not a time")
    return v
