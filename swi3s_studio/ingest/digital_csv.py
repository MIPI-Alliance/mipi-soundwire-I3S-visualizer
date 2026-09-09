"""Reader for a Saleae Logic 2 **digital CSV** export.

Logic 2 (Export → CSV, digital) writes a header row ``Time [s], Channel 0,
Channel 1, …`` then one row per sample-or-transition: an absolute time in seconds
and a 0/1 per channel. We turn two chosen channels (clock, data) into edge
(transition) arrays and infer the sample rate from the smallest time step.
"""
from __future__ import annotations

import csv
from typing import List, Optional, Tuple

import numpy as np

from .capture import Capture


def read_header(path: str) -> List[str]:
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def channel_transition_counts(path: str, max_rows: int = 2_000_000) -> List[int]:
    """Count level transitions per channel column (column 0 is Time), reading up
    to `max_rows` data rows. Used to auto-pick clock vs data: the forwarded clock
    toggles every UI, so it has far more transitions than the NRZS data line.
    Returns one count per channel column (index 0 == CSV column 1)."""
    _TRUE = ("1", "1.0", "True")
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)                          # csv.reader parses at C speed
        header = next(r, None)
        if not header:
            return []
        ncols = len(header) - 1                     # channel columns after Time
        if ncols <= 0:
            return []
        rows = []
        for i, row in enumerate(r):
            if i >= max_rows:
                break
            if len(row) > ncols:                    # skip short rows (as the old loop did)
                rows.append(row)
    if not rows:
        return [0] * ncols
    # Per column: strip + membership + count level changes with NumPy, instead of a
    # per-cell Python string test in a row loop (the old hot spot on 2M-row CSVs).
    counts = [0] * ncols
    for c in range(ncols):
        vals = np.char.strip(np.array([row[c + 1] for row in rows]))
        lvl = np.isin(vals, _TRUE).astype(np.int8)
        counts[c] = int(np.count_nonzero(np.diff(lvl)))   # nonzero diff == a transition
    return counts


def looks_complementary(path: str, clock_col: int, data_col: int,
                        max_rows: int = 100_000) -> bool:
    """True when two channels form a DLV differential pair — always opposite
    (``data == NOT clock``). That marks a partial DLV (PHY3) capture with no
    cold-start (see analysis.dlv_detect). A forwarded-clock (FBCSE) pair is
    independent (~half the rows differ), so the 0.99 threshold separates them
    cleanly. `clock_col`/`data_col` are 1-based CSV columns (0 is Time)."""
    _TRUE = ("1", "1.0", "True")
    m = max(clock_col, data_col)
    n = diff = 0
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        if not (next(r, None)):
            return False
        for i, row in enumerate(r):
            if i >= max_rows:
                break
            if len(row) <= m:
                continue
            a = row[clock_col].strip() in _TRUE
            b = row[data_col].strip() in _TRUE
            n += 1
            diff += (a != b)
    return n > 0 and diff / n >= 0.99


def infer_sample_rate(path: str, max_rows: int = 2_000_000) -> int:
    """Infer the capture rate from the finest timestamp spacing (~one sample
    period), snapped to the nearest kHz. Returns 0 if it can't be inferred."""
    times: List[str] = []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for i, row in enumerate(r):
            if i >= max_rows or not row:
                break
            times.append(row[0])
    if len(times) < 2:
        return 0
    # Bulk float-parse + finest positive spacing with NumPy (was a per-row Python loop).
    try:
        t = np.asarray(times, dtype=np.float64)     # fast path: all times numeric
    except ValueError:                              # rare stray non-numeric cell
        def _f(s):
            try:
                return float(s)
            except ValueError:
                return np.nan
        t = np.array([_f(s) for s in times], dtype=np.float64)
        t = t[~np.isnan(t)]
    if t.size < 2:
        return 0
    d = np.diff(t)
    d = d[d > 0]                                     # only forward steps (matches t > last)
    if d.size == 0:
        return 0
    rate = 1.0 / float(d.min())
    return int(round(rate / 1000.0)) * 1000         # snap to nearest kHz



def _channel_columns(header: List[str]) -> List[int]:
    # Every column after the first (time) that isn't obviously a time column.
    return [i for i in range(1, len(header))]


# Rows converted to arrays per batch. Big enough that the per-batch NumPy calls are
# amortised, small enough that the Python-object working set stays a few MB.
_ROW_CHUNK = 200_000

# Retained bytes per row once converted: time float64 (8) + two int8 level columns.
# The transient per-batch Python objects are bounded by _ROW_CHUNK and do not scale
# with the file, so they are not part of the prediction.
_BYTES_PER_ROW = 10


class CsvTooLargeError(MemoryError):
    """Opening this digital CSV is predicted to exhaust memory.

    Raised BEFORE the file is read, like the .sal guard, so the caller gets a number
    instead of a swapping machine. There is no windowed CSV path to offer, so the
    remedies are a smaller export or more RAM.
    """

    def __init__(self, path: str, rows: int, predicted: int, budget: int):
        self.path = path
        self.rows = int(rows)
        self.predicted = int(predicted)
        self.budget = int(budget)
        MemoryError.__init__(
            self,
            f"{path}: about {rows:,} rows would need roughly "
            f"{predicted / 1e9:.1f} GB of arrays, but only {budget / 1e9:.1f} GB is "
            f"budgeted. Export a shorter capture (or fewer columns), free memory, or "
            f"pass max_bytes=0 to override.")


def _predict_rows(path: str, header_len: int, sample_bytes: bytes) -> int:
    """Rows in the file, estimated from its size and the width of a sampled row.

    Reading the whole file to count them would defeat the purpose of a pre-flight
    check, so this divides the file size by the mean length of the first few data
    rows. Row width in a Logic export is near-constant (fixed-precision timestamps
    and single-digit levels), so this is accurate to a few percent — and the guard's
    threshold is an order-of-magnitude decision, not a precise one.
    """
    import os
    size = os.path.getsize(path)
    lines = [ln for ln in sample_bytes.split(b"\n")[1:] if ln]      # drop the header
    if not lines:
        return 0
    mean = sum(len(ln) + 1 for ln in lines) / len(lines)
    return int(size / mean) if mean > 0 else 0


def load_capture(path: str, clock_col: int, data_col: int,
                 sample_rate_hz: Optional[int] = None,
                 max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a digital CSV using the chosen clock/data columns
    (column indices into the CSV; column 0 is Time).

    READ IN BATCHES, NOT ALL AT ONCE. This used to materialise `[row for row in r]` —
    every row as a Python list of strings — and then three more full-length Python
    lists for the time and two level columns. Measured peak was ~12x the file size
    (415 MB for a 34 MB / 2M-row export), which for a format the architecture doc
    describes as multi-GB is the wrong shape entirely, and unlike the .sal path there
    was no guard to refuse the ones that would not fit. Converting each batch to
    arrays and keeping only the arrays makes the retained cost ~10 bytes/row with a
    working set bounded by `_ROW_CHUNK`.

    THE TRADE, MEASURED (best of three, same 34 MB / 2M-row file, identical edges out):
    4.1 s / 415 MB before, 5.7 s / 86 MB after — 4.8x less memory for 1.4x the wall
    clock. Deliberate: the ceiling that matters here is the one that makes a large
    export impossible rather than slow, and a 1 GB CSV now needs ~2.5 GB instead of
    ~12 GB. The residual cost is one extra list per batch (islice materialises, the
    comprehension filters); closing it needs a counting iterator to tell "end of file"
    from "every row filtered out", which is more machinery than the fraction of a
    second is worth.

    `max_bytes` follows the .sal convention: None/omitted uses the shared
    `memory_budget()`, a positive value overrides it, and 0 opts out of the check.
    """
    from .saleae_sal import memory_budget  # ONE definition of the budget

    with open(path, "rb") as fb:               # a raw sample, for the row-width estimate
        sample = fb.read(64 << 10)

    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, None) or []         # validate the chosen columns up front
        for label, col in (("clock", clock_col), ("data", data_col)):
            if col < 1 or col >= len(header):
                raise ValueError(f"{path}: {label} column {col} is out of range "
                                 f"(file has {len(header)} columns; column 0 is Time)")
        m = max(clock_col, data_col)

        # PRE-FLIGHT, BEFORE READING. max_bytes == 0 is the documented opt-out, and is
        # checked here rather than inside memory_budget() for the reason its docstring
        # gives: a budget function cannot express "unlimited" without returning a
        # number that silently disables the check.
        if max_bytes != 0:
            rows_est = _predict_rows(path, len(header), sample)
            predicted = rows_est * _BYTES_PER_ROW
            budget = memory_budget(max_bytes)
            if predicted > budget:
                raise CsvTooLargeError(path, rows_est, predicted, budget)

        _TRUE = ("1", "1.0", "True")
        t_parts: List[np.ndarray] = []
        c_parts: List[np.ndarray] = []
        d_parts: List[np.ndarray] = []

        def _flush(batch: List[list]) -> None:
            """Convert one batch of rows to arrays and drop the Python objects.

            `np.fromiter` over a membership test rather than
            `np.isin(np.char.strip(np.array(...)))`: building a NumPy unicode array and
            running char.strip over it costs ~50% more than testing the strings in place,
            and the intermediate array is pure waste since only the boolean survives.
            Both forms were checked to agree, whitespace included.
            """
            if not batch:
                return
            n = len(batch)
            cv = np.fromiter((row[clock_col].strip() in _TRUE for row in batch),
                             dtype=np.int8, count=n)
            dv = np.fromiter((row[data_col].strip() in _TRUE for row in batch),
                             dtype=np.int8, count=n)
            tcol = [row[0] for row in batch]
            try:
                tt = np.asarray(tcol, dtype=np.float64)    # fast path: all numeric
            except ValueError:
                # Rare: a stray non-numeric time cell — parse per-row and drop the bad
                # ones (matching the original loop's per-row robustness).
                tt = np.full(len(tcol), np.nan, dtype=np.float64)
                for i, s in enumerate(tcol):
                    try:
                        tt[i] = float(s)
                    except ValueError:
                        pass
                ok = ~np.isnan(tt)
                tt, cv, dv = tt[ok], cv[ok], dv[ok]
            t_parts.append(tt)
            c_parts.append(cv)
            d_parts.append(dv)

        # PULL WHOLE BATCHES WITH islice, and keep the row filter a comprehension. An
        # explicit `for row in r: ... append` loop costs about 80% more wall clock on a
        # 2M-row file than the comprehension it replaced — the batching is supposed to
        # bound memory, not to move the parse into slower Python.
        from itertools import islice
        while True:
            raw = list(islice(r, _ROW_CHUNK))
            if not raw:
                break
            _flush([row for row in raw if len(row) > m])
            del raw

    if not t_parts:
        raise ValueError(f"{path}: not enough rows to form a capture")
    t = t_parts[0] if len(t_parts) == 1 else np.concatenate(t_parts)
    cv = c_parts[0] if len(c_parts) == 1 else np.concatenate(c_parts)
    dv = d_parts[0] if len(d_parts) == 1 else np.concatenate(d_parts)
    # Release the per-batch arrays now that the concatenated copies own the data —
    # holding both is the peak. `clear()` rather than `del`: _flush closes over these
    # names, and deleting the binding leaves that closure referring to nothing.
    t_parts.clear()
    c_parts.clear()
    d_parts.clear()
    if t.size < 2:
        raise ValueError(f"{path}: not enough numeric rows to form a capture")

    # The export isn't guaranteed strictly time-ascending; sort by time so both the
    # level-change detection (np.diff below) and the resulting edge samples come out
    # ordered — the C++ TransitionSampleSource requires ascending edge arrays. Guard
    # the sort so an already-ascending capture (the common case) pays nothing.
    if t.size and np.any(np.diff(t) < 0):
        order = np.argsort(t, kind="stable")
        t, cv, dv = t[order], cv[order], dv[order]

    rate = sample_rate_hz
    if not rate:
        dt = np.diff(t)
        dt = dt[dt > 0]
        rate = int(round(1.0 / dt.min())) if dt.size else 1_000_000

    # Logic captures start at a negative (pre-trigger) time; rebase to the
    # earliest timestamp so sample 0 = capture start and no time goes negative
    # (a negative * rate would wrap through the uint64 cast to garbage). Use the
    # min, not row 0, in case the export isn't strictly time-ascending.
    origin = float(t.min())

    def edges(levels: np.ndarray) -> Tuple[np.ndarray, bool]:
        change = np.flatnonzero(np.diff(levels) != 0) + 1
        samples = np.rint((t[change] - origin) * rate).astype(np.uint64)
        return samples, bool(levels[0])

    ce, ic = edges(cv)
    de, id_ = edges(dv)
    return Capture(clock_edges=ce, data_edges=de,
                   initial_clock=ic, initial_data=id_, sample_rate_hz=int(rate))
