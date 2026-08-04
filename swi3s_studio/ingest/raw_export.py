"""Workaround data-out paths for a :class:`~swi3s_studio.ingest.capture.Capture`:
per-channel Saleae **binary** blobs and a Logic-2-style **digital CSV**.

These sit alongside the ``.sal`` writer (``sal_export``) as portable, tool-agnostic
ways to get a capture's clock+data lines out of SWI3S Studio:

- :func:`export_bin` writes one documented version-0 ``<SALEAE>`` blob per channel
  (the "Binary export" layout: initial state + absolute transition times in
  seconds). This is the stable, documented Saleae format; other Saleae tooling and
  our own ``saleae_binary``/``.sal`` reader consume it directly.
- :func:`export_csv` writes a Logic-2 digital CSV: a ``Time [s],<name>,…`` header
  then one row per transition (on any channel), each row giving every channel's
  level at that instant. Round-trips through ``digital_csv.load_capture``.

Both round-trip through this package; the CSV is also readable by any spreadsheet /
NumPy / pandas, which is the point of the workaround.
"""
from __future__ import annotations

import csv
import os

import numpy as np

from . import saleae_binary
from .capture import Capture


def export_bin(capture: Capture, path: str, *,
               clock_channel: int = 0, data_channel: int = 1,
               include_clock: bool = True, include_data: bool = True) -> list[str]:
    """Write `capture`'s clock and data lines as version-0 ``<SALEAE>`` binary blobs
    (one file per included channel) and return the paths written.

    `path` is a base name (typically what a Save dialog returns, e.g.
    ``…/capture.bin``); the per-channel files are its stem plus
    ``-digital-<channel>.bin`` (Saleae exports one file per channel — a single
    ``.bin`` can't hold two). Transition times are ``edge_sample / sample_rate``
    (seconds), `begin_time = 0` (synthetic captures have no pre-trigger region).
    `include_clock`/`include_data` select which signals to write (both by default)."""
    if include_clock and include_data and clock_channel == data_channel:
        raise ValueError(f"clock_channel and data_channel must differ (both {clock_channel})")
    rate = int(capture.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"export_bin: capture.sample_rate_hz must be > 0 (got {rate})")

    signals = []
    if include_clock:
        signals.append((clock_channel, capture.clock_edges, capture.initial_clock))
    if include_data:
        signals.append((data_channel, capture.data_edges, capture.initial_data))
    if not signals:
        raise ValueError("export_bin: no signals selected")
    stem = path[:-4] if path.lower().endswith(".bin") else path
    written: list[str] = []
    for ch, edges, init in signals:
        p = f"{stem}-digital-{int(ch)}.bin"
        times = np.ascontiguousarray(edges, dtype=np.float64) / rate
        saleae_binary.write_channel(p, bool(init), times, begin_time=0.0)
        written.append(p)
    return written


def _levels_at(samples: np.ndarray, edges: np.ndarray, initial: bool) -> np.ndarray:
    """Level of a line at each query sample: `initial` XOR (number of the line's
    transitions at or before that sample, mod 2). `side='right'` so the flip takes
    effect *at* the transition sample (the row for an edge shows the post-edge
    level), matching how `digital_csv` recovers edges from consecutive-row diffs."""
    edges = np.ascontiguousarray(edges, dtype=np.uint64)
    counts = np.searchsorted(edges, samples, side="right")
    return (int(bool(initial)) ^ (counts & 1)).astype(np.int8)


def export_csv(capture: Capture, path: str, *,
               clock_channel: int = 0, data_channel: int = 1,
               clock_name: str = "Channel 0", data_name: str = "Channel 1",
               include_clock: bool = True, include_data: bool = True) -> None:
    """Write `capture` as a Logic-2 digital CSV (`Time [s],<clock>,<data>`).

    Emits one row per transition on any included line — the merged, sorted set of
    edge samples — each row giving every included line's level at that sample,
    preceded by a capture-start row (sample 0) carrying the initial states when the
    first edge is past 0. `include_clock`/`include_data` select which signals become
    columns (both by default). Times are `sample / sample_rate`, written
    full-precision so `round(time * rate)` recovers the exact sample. With both
    signals it round-trips through
    `digital_csv.load_capture(path, clock_col=1, data_col=2, sample_rate_hz=rate)`."""
    rate = int(capture.sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"export_csv: capture.sample_rate_hz must be > 0 (got {rate})")

    cols = []                                        # (name, edges, initial)
    if include_clock:
        cols.append((clock_name, np.ascontiguousarray(capture.clock_edges, dtype=np.uint64),
                     capture.initial_clock))
    if include_data:
        cols.append((data_name, np.ascontiguousarray(capture.data_edges, dtype=np.uint64),
                     capture.initial_data))
    if not cols:
        raise ValueError("export_csv: no signals selected")

    all_edges = [e for _, e, _ in cols if e.size]
    samples = np.unique(np.concatenate(all_edges)) if all_edges else np.zeros(0, dtype=np.uint64)
    # A capture-start row (sample 0) fixes the initial state when no edge sits at 0.
    if samples.size == 0 or samples[0] != 0:
        samples = np.concatenate([np.zeros(1, dtype=np.uint64), samples])

    levels = [_levels_at(samples, e, init) for _, e, init in cols]
    times = samples.astype(np.float64) / rate

    names = [name for name, _, _ in cols]
    level_lists = [lv.tolist() for lv in levels]     # native ints once, not per-row int()
    tvals = times.tolist()
    n = len(tvals)
    ncol = len(level_lists)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Time [s]", *names])             # header via csv (handles any quoting)
        # Data rows are plain numbers (no CSV quoting needed): build + write them in blocks
        # joined with csv's \r\n terminator instead of a per-row writerow() call (~2x on a
        # multi-million-row export). Chunked so peak memory stays bounded.
        CHUNK = 1 << 16
        for base in range(0, n, CHUNK):
            end = min(base + CHUNK, n)
            if ncol == 1:
                l0 = level_lists[0]
                rows = [f"{tvals[i]:.15g},{l0[i]}" for i in range(base, end)]
            elif ncol == 2:
                l0, l1 = level_lists
                rows = [f"{tvals[i]:.15g},{l0[i]},{l1[i]}" for i in range(base, end)]
            else:
                rows = [",".join([f"{tvals[i]:.15g}", *[str(ll[i]) for ll in level_lists]])
                        for i in range(base, end)]
            f.write("\r\n".join(rows))
            f.write("\r\n")
