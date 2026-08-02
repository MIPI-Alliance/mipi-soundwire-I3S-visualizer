"""Reader for a Tektronix scope **analog CSV** export (this project's MSO58).

The file is a small key/value preamble (Model, per-channel Waveform Type,
Sample Interval, Record Length, Vertical Units, …), a blank line, then a
`TIME, CH1, CH2, …` header row and one float row per sample in scientific
notation. Multi-channel exports repeat the key/value columns per channel
side by side (``Model,MSO58,,Channel,CH1,,Channel,CH2`` — note the empty
column between channel blocks), so the preamble is parsed generically as
whatever key/value pairs appear before the TIME row rather than assuming a
fixed column layout.

Time starts negative (pre-trigger, matching the .wfm/digital-CSV convention
this project already reads) and runs to several hundred thousand rows, so the
data rows are bulk-parsed as one NumPy float64 block rather than per-cell.
"""
from __future__ import annotations

import csv
from typing import Dict, List, Optional

import numpy as np


def is_analog_csv(path: str, max_scan_rows: int = 10_000) -> bool:
    """True if `path` is a Tektronix analog CSV (volts) rather than a Logic 2
    digital CSV (0/1 levels), decided by the DATA VALUES, not the header — a
    digital export's channel columns are strictly 0 or 1, an analog export's are
    arbitrary floating-point volts.

    A data row is one whose channel columns (everything after the time column)
    ALL parse as numbers — this skips the digital header row and the Tek
    key/value preamble + TIME header automatically without special-casing them.
    Returns True as soon as any channel value is not exactly 0 or 1; returns
    False (digital) if the scanned data rows are all 0/1.

    Every data row up to `max_scan_rows` is checked in full; beyond that the
    scan keeps going for the rest of the file but only inspects one row in
    every `max_scan_rows` (i.e. total inspected rows stays roughly bounded at
    ~2x `max_scan_rows` regardless of file length). This matters for a capture
    with a long railed/saturated lead-in — thousands of rows that are all
    exactly 0/1 before the real analog data begins — which the old hard cap at
    `max_scan_rows` DATA rows would misclassify as digital. A genuinely all-0/1
    file (legitimately digital) still costs only ~2x `max_scan_rows` row
    parses no matter how many million rows it has; a real analog capture trips
    on its first non-binary sample almost immediately, scanned or thinned."""
    seen = 0
    thinned = False
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                vals = []
                for cell in row[1:]:                 # skip the time column
                    c = cell.strip()
                    if not c:
                        vals = None                  # blank cell -> not a data row
                        break
                    try:
                        vals.append(float(c))
                    except ValueError:
                        vals = None                  # header/preamble text -> skip
                        break
                if not vals:
                    continue
                seen += 1
                if thinned and seen % max_scan_rows != 0:
                    continue                          # thinned out past max_scan_rows
                for v in vals:
                    if v != 0.0 and v != 1.0:
                        return True                  # a non-binary value -> analog
                if seen >= max_scan_rows:
                    # Don't stop scanning here (a long railed lead-in of exact
                    # 0/1 values would misclassify as digital) — instead thin
                    # the remaining rows so total work stays bounded.
                    thinned = True
    except OSError:
        return False
    return False                                     # only 0/1 seen -> digital


def _find_time_header(rows: List[List[str]]) -> int:
    """Return the index of the header row (first field case-insensitively
    "TIME") within a small prefix of pre-read preamble rows."""
    for i, row in enumerate(rows):
        if row and row[0].strip().upper() == "TIME":
            return i
    raise ValueError("analog CSV: no TIME header row found in preamble")


def _parse_preamble(rows: List[List[str]]) -> Dict[str, List[str]]:
    """Collect the preamble's key/value pairs. Each preamble row is one or more
    ``key,value`` pairs (a multi-channel export repeats the pair per channel,
    separated by an empty column) — e.g. ``Sample Interval,1.6e-10,,Sample
    Interval,1.6e-10``. Returns each key mapped to the list of values seen
    (one per channel block, in file order); a blank/short row is skipped."""
    out: Dict[str, List[str]] = {}
    for row in rows:
        cells = [c for c in row]
        i = 0
        while i + 1 < len(cells) + 1 and i < len(cells):
            key = cells[i].strip() if i < len(cells) else ""
            val = cells[i + 1].strip() if i + 1 < len(cells) else ""
            if key:
                out.setdefault(key, []).append(val)
            i += 2
    return out


def read_analog_csv(path: str) -> Dict[str, object]:
    """Parse a Tektronix analog CSV export into ``{"time", "channels",
    "sample_rate_hz"}``: `time` is the shared TIME column (seconds, may start
    negative); `channels` maps each header name (e.g. "CH1") after TIME to its
    float64 volts array; `sample_rate_hz` is derived from the TIME column's
    median spacing (preferred over the preamble's "Sample Interval" — the
    column is ground truth for what was actually exported, and matches how
    `analog.capture_from_analog`/`digital_csv` infer rate elsewhere)."""
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        all_rows = list(r)

    header_idx = _find_time_header(all_rows)
    preamble_rows = all_rows[:header_idx]
    header = [c.strip() for c in all_rows[header_idx]]
    data_rows = all_rows[header_idx + 1:]
    # A trailing blank line (or short row) is common at EOF; drop rows that
    # can't possibly hold every column.
    ncols = len(header)
    data_rows = [row for row in data_rows if len(row) >= ncols and row[0].strip()]
    # A row with MORE columns than the header (a stray trailing comma from an
    # Excel re-save is the usual culprit) would otherwise make `data_rows`
    # ragged and crash the np.asarray call below with a cryptic "inhomogeneous
    # shape" error — truncate to the header's column count before that point.
    data_rows = [row[:ncols] for row in data_rows]
    if len(data_rows) < 2:
        raise ValueError(f"{path}: not enough data rows after the TIME header")

    # Bulk-convert with NumPy rather than per-cell float() in a Python loop —
    # this file runs to ~625k rows (digital_csv's load_capture hits the same
    # hot spot and takes the same approach).
    block = np.asarray(data_rows, dtype=np.float64)
    time = block[:, 0]
    channels = {header[c]: block[:, c] for c in range(1, ncols)}

    preamble = _parse_preamble(preamble_rows)
    sample_rate_hz = _infer_sample_rate(time, preamble)

    return {"time": time, "channels": channels, "sample_rate_hz": sample_rate_hz}


def _infer_sample_rate(time: np.ndarray, preamble: Dict[str, List[str]]) -> float:
    """Prefer the TIME column's own median spacing over the preamble's "Sample
    Interval" field: the column reflects what was actually exported (e.g. after
    any decimation), while the preamble records the scope's acquisition
    interval. Falls back to the preamble value only if the column has fewer
    than 2 rows to diff."""
    if time.size >= 2:
        dt = np.diff(time)
        dt = dt[dt > 0]
        if dt.size:
            return 1.0 / float(np.median(dt))
    for key in ("Sample Interval",):
        vals = preamble.get(key)
        if vals:
            try:
                interval = float(vals[0])
                if interval > 0:
                    return 1.0 / interval
            except ValueError:
                pass
    return 0.0


def sample_interval(preamble_or_path: object) -> Optional[float]:
    """Recover the scope's "Sample Interval" preamble field, given either an
    already-parsed preamble dict (as produced internally) or a file path.
    Returns None if the field isn't present. Exposed for callers that want the
    raw acquisition interval rather than `read_analog_csv`'s preferred (TIME
    column-derived) rate."""
    if isinstance(preamble_or_path, dict):
        preamble = preamble_or_path
    else:
        with open(preamble_or_path, newline="", encoding="utf-8") as f:
            rows = []
            for row in csv.reader(f):
                if row and row[0].strip().upper() == "TIME":
                    break
                rows.append(row)
        preamble = _parse_preamble(rows)
    vals = preamble.get("Sample Interval")
    if not vals:
        return None
    try:
        return float(vals[0])
    except ValueError:
        return None


def channel_names(path: str) -> List[str]:
    """Return the channel names (the header fields after TIME) without a full
    parse — reads only the preamble + TIME header row, not the bulk data rows,
    so the UI can enumerate channels (e.g. to populate a clock/data picker)
    without paying for hundreds of thousands of data rows it doesn't need yet.
    Reuses `_find_time_header` on a bounded prefix, the same trick
    `sample_interval` uses to read just the preamble by path."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.reader(f):
            rows.append(row)
            if row and row[0].strip().upper() == "TIME":
                break
    header_idx = _find_time_header(rows)
    header = [c.strip() for c in rows[header_idx]]
    return header[1:]
