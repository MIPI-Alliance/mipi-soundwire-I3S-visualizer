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


def load_capture(path: str, clock_col: int, data_col: int,
                 sample_rate_hz: Optional[int] = None) -> Capture:
    """Build a Capture from a digital CSV using the chosen clock/data columns
    (column indices into the CSV; column 0 is Time)."""
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, None) or []         # validate the chosen columns up front
        for label, col in (("clock", clock_col), ("data", data_col)):
            if col < 1 or col >= len(header):
                raise ValueError(f"{path}: {label} column {col} is out of range "
                                 f"(file has {len(header)} columns; column 0 is Time)")
        m = max(clock_col, data_col)
        rows = [row for row in r if len(row) > m]   # csv.reader parses at C speed
    if len(rows) < 2:
        raise ValueError(f"{path}: not enough rows to form a capture")

    # Bulk-convert the three columns of interest with NumPy instead of per-cell
    # Python string tests in a row loop (the old hot spot on multi-million-row CSVs).
    _TRUE = ("1", "1.0", "True")
    cv = np.isin(np.char.strip(np.array([row[clock_col] for row in rows])), _TRUE).astype(np.int8)
    dv = np.isin(np.char.strip(np.array([row[data_col] for row in rows])), _TRUE).astype(np.int8)
    tcol = [row[0] for row in rows]
    try:
        t = np.asarray(tcol, dtype=np.float64)      # fast path: all times numeric
    except ValueError:
        # Rare: a stray non-numeric time cell — parse per-row and drop the bad ones
        # (matching the old loop's try/except-per-row robustness).
        t = np.full(len(tcol), np.nan, dtype=np.float64)
        for i, s in enumerate(tcol):
            try:
                t[i] = float(s)
            except ValueError:
                pass
        ok = ~np.isnan(t)
        t, cv, dv = t[ok], cv[ok], dv[ok]
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
