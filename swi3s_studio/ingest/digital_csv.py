"""Reader for a Saleae Logic 2 **digital CSV** export.

Logic 2 (Export → CSV, digital) writes a header row ``Time [s], Channel 0,
Channel 1, …`` then one row per sample-or-transition: an absolute time in seconds
and a 0/1 per channel. We turn two chosen channels (clock, data) into edge
(transition) arrays and infer the sample rate from the grid the timestamps sit on
(`timebase.rate_from_times`).
"""
from __future__ import annotations

import csv
from operator import itemgetter
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .. import import_notes
from .capture import Capture
from .timebase import rate_from_times


def _trim_header(header: List[str]) -> List[str]:
    """`header` without its trailing empty cells (a row ending in a comma)."""
    n = len(header)
    while n > 1 and not header[n - 1].strip():
        n -= 1
    return header[:n]


def read_header(path: str) -> List[str]:
    """The header row, `Time` then one name per channel column; trailing empty cells
    (a header row ending in a comma) name no column and are dropped."""
    with open(path, newline="", encoding="utf-8") as f:
        return _trim_header(next(csv.reader(f)))


# Rows per batch of `scan`: smaller than the loader's, since a probe keeps nothing per
# row and its peak is the batch (every cell a Python string).
_SCAN_CHUNK = 50_000

# Data rows whose timestamps give the sample rate: the first this many. `scan` and
# `load_capture` read the same rows, so the probe reports the rate the loader uses.
_RATE_ROWS = 1 << 16


def scan(path: str, max_rows: int = 2_000_000) -> Tuple[List[int], int]:
    """(transitions per channel column, inferred rate) from ONE pass over up to `max_rows`
    data rows, read in batches, so a pre-decode look at a large export costs one bounded
    read and a few MB. Counts are per channel column (index 0 == CSV
    column 1), the forwarded clock toggling every UI; the rate is
    `timebase.rate_from_times` over the first `_RATE_ROWS` timestamps, or 0. The columns
    are the header's (`read_header`), and a row is read when it has a cell for each."""
    from itertools import islice
    _TRUE = ("1", "1.0", "True")
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header or len(_trim_header(header)) < 2:
            return [], 0
        header = _trim_header(header)
        ncols = len(header) - 1                     # channel columns after Time
        counts = [0] * ncols
        last: List[Optional[int]] = [None] * ncols  # each column's level ending the last batch
        rate_times: List[np.ndarray] = []
        rate_left = _RATE_ROWS
        left = max_rows
        while left > 0:
            raw = list(islice(r, min(_SCAN_CHUNK, left)))
            if not raw:
                break
            left -= len(raw)
            rows = [row for row in raw if len(row) > ncols]     # skip short rows
            del raw
            if not rows:
                continue
            for c in range(ncols):
                lvl = np.fromiter((row[c + 1].strip() in _TRUE for row in rows),
                                  dtype=np.int8, count=len(rows))
                counts[c] += int(np.count_nonzero(np.diff(lvl)))
                if last[c] is not None and int(lvl[0]) != last[c]:
                    counts[c] += 1                   # a transition across the batch seam
                last[c] = int(lvl[-1])
            times = [row[0] for row in rows]
            try:
                t = np.asarray(times, dtype=np.float64)          # fast path: all numeric
            except ValueError:                                   # a stray non-numeric cell
                t = np.full(len(times), np.nan)
                for n, v in enumerate(times):
                    try:
                        t[n] = float(v)
                    except ValueError:
                        pass
                t = t[~np.isnan(t)]
            if rate_left > 0 and t.size:
                rate_times.append(t[:rate_left])
                rate_left -= rate_times[-1].size
    rate = rate_from_times(np.concatenate(rate_times)) if rate_times else 0
    return counts, rate


def channel_transition_counts(path: str, max_rows: int = 2_000_000) -> List[int]:
    """Level transitions per channel column (column 0 is Time) over up to `max_rows`
    data rows (`scan`). Used to auto-pick clock vs data: the forwarded clock toggles
    every UI, so it has far more transitions than the NRZS data line. One count per
    channel column (index 0 == CSV column 1)."""
    return scan(path, max_rows)[0]


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
    """Infer the capture rate from the grid the first `_RATE_ROWS` timestamps sit on
    (`scan`, `timebase.rate_from_times`). Returns 0 if it can't be inferred."""
    return scan(path, max_rows)[1]



def _channel_columns(header: List[str]) -> List[int]:
    # Every column after the first (time) that isn't obviously a time column.
    return [i for i in range(1, len(header))]


# Rows converted to arrays per batch. Big enough that the per-batch NumPy calls are
# amortised; the batch's Python-object working set (`_batch_bytes`, about 35 MB on a
# three-column export) is a constant whatever the file's length.
_ROW_CHUNK = 200_000

# Peak bytes per row of `load_capture` on a time-ascending file, an upper bound: a row
# adds at most one uint64 edge to each of the two channels (16 while reading), and
# joining a channel's per-batch edge arrays into one copies that channel's edges once
# (8 more, after the last batch is released).
_READ_BYTES_PER_ROW = 16
_BYTES_PER_ROW = 24
# Peak bytes per row on a file whose rows are not in time order, an upper bound. It is
# read whole into arrays (float64 time and two int8 levels, 10) and sorted, which also
# holds the int64 sort order (8) and one sorted copy of the time (8).
_UNSORTED_BYTES_PER_ROW = 26
# Python-object bytes per row of a batch, beyond the row's text: the row list with its
# GC header, the time cell's str header, and the batch's pointer lists. Measured with
# tracemalloc at about 165 on a three-column export. A one-character level cell is a
# cached str, so a cell costs its list pointer.
_ROW_OBJECT_BYTES = 200
_CELL_POINTER_BYTES = 8
# Bytes per row of the first `_RATE_ROWS` rows while the rate is inferred from them:
# the rows held as arrays (10), their times joined (8), and `rate_from_times` (measured
# with tracemalloc at 42).
_RATE_BYTES_PER_ROW = 64


def _batch_bytes(ncols: int, row_text_bytes: float) -> int:
    """Peak working set of one batch of `_ROW_CHUNK` rows of `ncols` cells each."""
    per_row = _ROW_OBJECT_BYTES + ncols * _CELL_POINTER_BYTES + row_text_bytes
    return int(_ROW_CHUNK * per_row)


def _row_text_bytes(sample_bytes: bytes) -> float:
    """Mean length of the data rows in `sample_bytes` (the header dropped), or 0."""
    lines = [ln for ln in sample_bytes.split(b"\n")[1:] if ln]
    return sum(len(ln) for ln in lines) / len(lines) if lines else 0.0


def predict_peak_bytes(rows: int, ncols: int, sample_bytes: bytes,
                       ascending: bool = True) -> int:
    """Predicted peak of `load_capture` on a file of `rows` data rows of `ncols` cells,
    `sample_bytes` being the head of the file: the larger of the peak while reading
    (a batch, the rows the rate is inferred from, and the edges so far) and the peak
    while joining the edge arrays.
    `ascending=False` prices a file whose rows are not in time order."""
    batch = (_batch_bytes(ncols, _row_text_bytes(sample_bytes))
             + min(int(rows), _RATE_ROWS) * _RATE_BYTES_PER_ROW)
    rows = int(rows)
    if not ascending:
        return rows * _UNSORTED_BYTES_PER_ROW + batch
    return max(rows * _READ_BYTES_PER_ROW + batch, rows * _BYTES_PER_ROW)


class CsvTooLargeError(MemoryError):
    """Opening this digital CSV is predicted to exhaust memory.

    Raised BEFORE the file is read, like the .sal guard. There is no windowed CSV
    path, so the remedies are a smaller export or more RAM.
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

    Divides the file size by the mean length of the first few data rows. Row width
    in a Logic export is near-constant (fixed-precision timestamps and single-digit
    levels), so this is accurate to a few percent.
    """
    import os
    size = os.path.getsize(path)
    lines = [ln for ln in sample_bytes.split(b"\n")[1:] if ln]      # drop the header
    if not lines:
        return 0
    mean = sum(len(ln) + 1 for ln in lines) / len(lines)
    return int(size / mean) if mean > 0 else 0


_TRUE = ("1", "1.0", "True")
# Level cells read as written: anything else is read as 0 and reported.
_LEVELS = frozenset(_TRUE + ("0", "0.0", "False"))


class _RowNotes:
    """What one read of a digital CSV passed over, for its import warnings: rows too
    short for the chosen columns, rows whose time is not a number, and level cells
    that are not 0 or 1, each as [count, first line, first text]."""

    def __init__(self) -> None:
        self.short: List[int] = [0, 0]
        self.bad_time: List[Any] = [0, 0, ""]
        self.bad_level: Dict[str, List[Any]] = {}
        self.rate_defaulted = False
        self.unsorted = False

    def report(self, path: str) -> None:
        src = import_notes.source_name(path)
        n, first = self.short
        if n:
            import_notes.warn(src, f"{n:,} row{'s' if n != 1 else ''} too short for the "
                                   f"chosen columns {'were' if n != 1 else 'was'} skipped "
                                   f"(first at line {first:,})")
        n, first, text = self.bad_time
        if n:
            import_notes.warn(src, f"{n:,} row{'s' if n != 1 else ''} with a time that is "
                                   f"not a number {'were' if n != 1 else 'was'} skipped "
                                   f"(first at line {first:,}: {text!r})")
        for role, (n, first, text) in self.bad_level.items():
            import_notes.warn(src, f"{n:,} {role} cell{'s' if n != 1 else ''} not 0 or 1 "
                                   f"{'were' if n != 1 else 'was'} read as 0 (first at "
                                   f"line {first:,}: {text!r})")
        if self.unsorted:
            import_notes.warn(src, "the rows are not in time order; they are sorted by time")
        if self.rate_defaulted:
            import_notes.warn(src, "the timestamps do not give a sample rate; 1 MHz is "
                                   "assumed")


def _check_levels(batch: List[list], col: int, role: str, base: int,
                  lines: Optional[List[int]], notes: _RowNotes) -> None:
    """Count the cells of column `col` in `batch` that are not a 0/1 level."""
    vals = set(map(itemgetter(col), batch))
    if vals <= _LEVELS:
        return
    bad = {v for v in vals if v.strip() not in _LEVELS}
    if not bad:
        return
    rec = notes.bad_level.setdefault(role, [0, 0, ""])
    for k, row in enumerate(batch):
        if row[col] in bad:
            if not rec[0]:
                rec[1] = lines[k] if lines is not None else base + k
                rec[2] = row[col]
            rec[0] += 1


def _convert(batch: List[list], clock_col: int, data_col: int,
             notes: Optional[_RowNotes] = None, base: int = 0,
             lines: Optional[List[int]] = None
             ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(time float64, clock int8, data int8) of one batch of rows, dropping any row
    whose time cell is not a number.

    Levels come from `np.fromiter` over a membership test on the stripped cell. With
    `notes`, the batch's unreadable cells are counted there; a row's line number is
    `lines[k]`, or `base + k` when `lines` is None.
    """
    if notes is not None:
        _check_levels(batch, clock_col, "clock", base, lines, notes)
        _check_levels(batch, data_col, "data", base, lines, notes)
    n = len(batch)
    cv = np.fromiter((row[clock_col].strip() in _TRUE for row in batch),
                     dtype=np.int8, count=n)
    dv = np.fromiter((row[data_col].strip() in _TRUE for row in batch),
                     dtype=np.int8, count=n)
    tcol = [row[0] for row in batch]
    try:
        tt = np.asarray(tcol, dtype=np.float64)        # fast path: all numeric
    except ValueError:
        # Rare: a stray non-numeric time cell. Parse per row and drop the bad ones.
        tt = np.full(len(tcol), np.nan, dtype=np.float64)
        for i, s in enumerate(tcol):
            try:
                tt[i] = float(s)
            except ValueError:
                pass
        ok = ~np.isnan(tt)
        if notes is not None:
            bad = np.flatnonzero(~ok)
            if bad.size:
                k = int(bad[0])
                if not notes.bad_time[0]:
                    notes.bad_time[1] = lines[k] if lines is not None else base + k
                    notes.bad_time[2] = tcol[k]
                notes.bad_time[0] += int(bad.size)
        tt, cv, dv = tt[ok], cv[ok], dv[ok]
    return tt, cv, dv


def _batches(r, m: int, clock_col: int, data_col: int, notes: Optional[_RowNotes] = None):
    """Yield `_convert`ed batches of the rows left in reader `r` that have a cell at
    column `m`; yields an empty batch for a batch with none, so the caller can tell
    a file with no usable rows from one with no numeric times. With `notes`, the
    skipped rows are counted there (line numbers count the header as line 1)."""
    # PULL WHOLE BATCHES WITH islice, and keep the row filter a comprehension; an
    # explicit `for row in r: ... append` loop is much slower.
    from itertools import islice
    line = 2                           # the first data row's line
    while True:
        raw = list(islice(r, _ROW_CHUNK))
        if not raw:
            return
        base = line
        line += len(raw)
        batch = [row for row in raw if len(row) > m]
        lines = None
        if notes is not None and len(batch) != len(raw):
            lines = [base + k for k, row in enumerate(raw) if len(row) > m]
            short = [base + k for k, row in enumerate(raw)
                     if len(row) <= m and any(c.strip() for c in row)]
            if short:
                if not notes.short[0]:
                    notes.short[1] = short[0]
                notes.short[0] += len(short)
        del raw
        if batch:
            out = _convert(batch, clock_col, data_col, notes, base, lines)
            del batch                  # released before the next batch is read
            yield out


class _Line:
    """One channel's edges, built a batch at a time."""

    def __init__(self) -> None:
        self.parts: List[np.ndarray] = []
        self.last: Optional[int] = None      # level of the last row added
        self.initial = False

    def add(self, t: np.ndarray, levels: np.ndarray, origin: float, rate: int) -> None:
        """Append the edges of one batch: the rows whose level differs from the row
        before, the first row of the file excepted, at round((t - origin) * rate)."""
        if self.last is None:
            self.initial = bool(levels[0])
            change = np.flatnonzero(np.diff(levels)) + 1
        else:
            change = np.flatnonzero(np.diff(levels, prepend=np.int8(self.last)))
        if change.size:
            self.parts.append(np.rint((t[change] - origin) * rate).astype(np.uint64))
        self.last = int(levels[-1])

    def edges(self) -> np.ndarray:
        """The edges added so far as one array; the per-batch arrays are released."""
        parts, self.parts = self.parts, []
        if not parts:
            return np.zeros(0, dtype=np.uint64)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)


def _rate_of(times: List[np.ndarray], notes: Optional[_RowNotes] = None) -> int:
    """`rate_from_times` over the first `_RATE_ROWS` of `times`, or 1 MHz if it has
    too few distinct times to give one (noted in `notes`)."""
    t = np.concatenate(times)[:_RATE_ROWS] if times else np.zeros(0)
    rate = rate_from_times(t)
    if not rate and notes is not None:
        notes.rate_defaulted = True
    return rate or 1_000_000


def _read_ascending(r, m: int, clock_col: int, data_col: int,
                    rate: Optional[int], notes: Optional[_RowNotes] = None):
    """Edges of a time-ascending file, one batch at a time: ``(clock _Line, data
    _Line, rate, numeric rows, any rows)``, or None at the first row earlier than the
    row before it.

    A batch's edges are kept and its rows released, so what grows with the file is
    the edge arrays. Until the rate is known (`_RATE_ROWS` rows read, or the end of
    the file) the batches are held as arrays.
    """
    clk, dat = _Line(), _Line()
    pending: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    pending_rows = 0
    origin = 0.0
    t_last = -np.inf
    rows = 0
    any_rows = False
    for t, cv, dv in _batches(r, m, clock_col, data_col, notes):
        any_rows = True
        if not t.size:
            continue
        if t[0] < t_last or (t.size > 1 and bool(np.any(t[1:] < t[:-1]))):
            return None
        if not rows:
            origin = float(t[0])    # sample 0: the earliest time of an ascending file
        t_last = float(t[-1])
        rows += t.size
        pending.append((t, cv, dv))
        pending_rows += t.size
        if not rate:
            if pending_rows < _RATE_ROWS:
                continue
            rate = _rate_of([p[0] for p in pending], notes)
        for pt, pc, pd in pending:
            clk.add(pt, pc, origin, rate)
            dat.add(pt, pd, origin, rate)
        pending = []
    if pending:
        rate = rate or _rate_of([p[0] for p in pending], notes)
        for pt, pc, pd in pending:
            clk.add(pt, pc, origin, rate)
            dat.add(pt, pd, origin, rate)
    return clk, dat, int(rate or 1_000_000), rows, any_rows


def _read_unsorted(r, m: int, clock_col: int, data_col: int, rate: Optional[int],
                   notes: Optional[_RowNotes] = None):
    """Edges of a file whose rows are not in time order: every row is read into
    arrays and sorted by time (stable), then the edges are taken as for an ascending
    file. Same return shape as `_read_ascending`."""
    t_parts: List[np.ndarray] = []
    c_parts: List[np.ndarray] = []
    d_parts: List[np.ndarray] = []
    any_rows = False
    for t, cv, dv in _batches(r, m, clock_col, data_col, notes):
        any_rows = True
        t_parts.append(t)
        c_parts.append(cv)
        d_parts.append(dv)
    if not any_rows:
        return _Line(), _Line(), int(rate or 1_000_000), 0, False
    if not rate:
        rate = _rate_of(t_parts, notes)  # the first rows in FILE order, as `scan` reads
    t = np.concatenate(t_parts)
    t_parts.clear()
    cv = np.concatenate(c_parts)
    c_parts.clear()
    dv = np.concatenate(d_parts)
    d_parts.clear()
    if t.size < 2:
        return _Line(), _Line(), int(rate), int(t.size), True
    order = np.argsort(t, kind="stable")
    t = t[order]
    cv = cv[order]
    dv = dv[order]
    del order
    # Rebase to the earliest timestamp so sample 0 = capture start and no time goes
    # negative (a negative * rate would wrap through the uint64 cast to garbage).
    origin = float(t[0])
    clk, dat = _Line(), _Line()
    for i in range(0, t.size, _ROW_CHUNK):          # bounds the per-edge temporaries
        clk.add(t[i:i + _ROW_CHUNK], cv[i:i + _ROW_CHUNK], origin, rate)
        dat.add(t[i:i + _ROW_CHUNK], dv[i:i + _ROW_CHUNK], origin, rate)
    return clk, dat, int(rate), int(t.size), True


def load_capture(path: str, clock_col: int, data_col: int,
                 sample_rate_hz: Optional[int] = None,
                 max_bytes: Optional[int] = None) -> Capture:
    """Build a Capture from a digital CSV using the chosen clock/data columns
    (column indices into the CSV; column 0 is Time).

    Rows are read in batches of `_ROW_CHUNK`; each batch's edges are kept and its
    rows released, so the Python-object working set is bounded by `_ROW_CHUNK` and
    what grows with the file is the edge arrays. Logic captures start at a negative
    (pre-trigger) time, so sample 0 is the earliest timestamp. With no
    `sample_rate_hz` the rate is `timebase.rate_from_times` over the first
    `_RATE_ROWS` timestamps.

    A file whose rows are not in time order is read a second time, whole, and sorted
    (the C++ TransitionSampleSource requires ascending edges); its peak is priced at
    `_UNSORTED_BYTES_PER_ROW` and checked again before that read.

    `max_bytes` follows the .sal convention: None/omitted uses the shared
    `memory_budget()`, a positive value overrides it, and 0 opts out of the check.
    The check compares `predict_peak_bytes` with the budget before the file is read.
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

        # PRE-FLIGHT, BEFORE READING. max_bytes == 0 is the documented opt-out, checked
        # here because memory_budget() has no value for "unlimited".
        rows_est = 0
        ncols = len(_trim_header(header))      # as the probe (read_header) counts them
        if max_bytes != 0:
            rows_est = _predict_rows(path, ncols, sample)
            predicted = predict_peak_bytes(rows_est, ncols, sample)
            budget = memory_budget(max_bytes)
            if predicted > budget:
                raise CsvTooLargeError(path, rows_est, predicted, budget)
        notes = _RowNotes()
        got = _read_ascending(r, m, clock_col, data_col, sample_rate_hz, notes)

    if got is None:
        if max_bytes != 0:
            predicted = predict_peak_bytes(rows_est, ncols, sample, ascending=False)
            budget = memory_budget(max_bytes)
            if predicted > budget:
                raise CsvTooLargeError(path, rows_est, predicted, budget)
        with open(path, newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            next(r, None)
            notes = _RowNotes()          # the second read covers every row
            notes.unsorted = True
            got = _read_unsorted(r, m, clock_col, data_col, sample_rate_hz, notes)

    clk, dat, rate, rows, any_rows = got
    if not any_rows:
        raise ValueError(f"{path}: not enough rows to form a capture")
    if rows < 2:
        raise ValueError(f"{path}: not enough numeric rows to form a capture")
    notes.report(path)
    ce = clk.edges()
    de = dat.edges()
    return Capture(clock_edges=ce, data_edges=de,
                   initial_clock=clk.initial, initial_data=dat.initial,
                   sample_rate_hz=int(rate))
