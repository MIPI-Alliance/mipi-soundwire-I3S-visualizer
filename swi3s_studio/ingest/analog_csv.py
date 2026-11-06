"""Reader for a scope **analog CSV** export.

The file is a small key/value preamble (Model, per-channel Waveform Type,
Sample Interval, Record Length, Vertical Units, …), a blank line, then a
`TIME, CH1, CH2, …` header row and one float row per sample in scientific
notation. Multi-channel exports repeat the key/value columns per channel
side by side (``Model,SCOPE1,,Channel,CH1,,Channel,CH2`` — note the empty
column between channel blocks), so the preamble is parsed generically as
whatever key/value pairs appear before the TIME row.

Time starts negative (pre-trigger, as in .wfm and digital CSV). The data rows
are parsed in batches of `_BATCH_ROWS`, each bulk-converted to a NumPy float64
block and split into per-column arrays.
"""
from __future__ import annotations

import csv
import math
import os
from itertools import islice
from typing import Dict, List, Optional

import numpy as np

from .. import import_notes
from . import analog
from .timebase import MAX_LINE_CHARS, MAX_RATE_HZ, quoted, text_csv


def _trimmed(row: List[str]) -> List[str]:
    """`row` without its trailing empty cells, keeping at least the first cell."""
    n = len(row)
    while n > 1 and not row[n - 1].strip():
        n -= 1
    return row[:n]


def _numeric_channels(row: List[str]) -> Optional[List[float]]:
    """The channel values of a data row (every cell after the time column a number),
    or None for a header, preamble, blank or short row. Trailing empty cells (a row
    ending in a comma) are not cells."""
    row = _trimmed(row)
    if len(row) < 2:
        return None
    vals: List[float] = []
    for cell in row[1:]:                     # skip the time column
        c = cell.strip()
        if not c:
            return None                      # blank cell -> not a data row
        try:
            vals.append(float(c))
        except ValueError:
            return None                      # header/preamble text -> skip
    return vals


def _non_binary(vals: List[float]) -> bool:
    return any(v != 0.0 and v != 1.0 for v in vals)


def _row_of_line(line: bytes) -> List[str]:
    """The cells of one CSV line read as bytes; a leading byte-order mark is not part of
    the first cell."""
    return next(csv.reader([line.decode("utf-8-sig", errors="replace")]), [])


def is_analog_csv(path: str, max_scan_rows: int = 10_000) -> bool:
    """True if `path` is a scope analog CSV (volts) rather than a digital CSV (0/1
    levels), decided by the DATA VALUES, not the header — a
    digital export's channel columns are strictly 0 or 1, an analog export's are
    arbitrary floating-point volts.

    A data row is one whose channel columns (everything after the time column)
    ALL parse as numbers — this skips the digital header row and the scope
    key/value preamble + TIME header automatically without special-casing them.
    Returns True as soon as any channel value is not exactly 0 or 1; returns
    False (digital) if the scanned data rows are all 0/1.

    The first `max_scan_rows` data rows are checked in full, reading at most
    `max_scan_rows` + `_PREAMBLE_MAX_ROWS` lines (a row that is not a data row counts
    against that budget too). Past them, the file is sampled at `max_scan_rows` byte
    offsets spread evenly through the rest of it, checking the first whole line after
    each, so at most about 2x `max_scan_rows` + `_PREAMBLE_MAX_ROWS` lines are parsed
    whatever the file's length, and a long railed/saturated lead-in of exact 0/1 rows
    does not classify an analog capture as digital. A line over
    `MAX_LINE_CHARS` among the first rows (counted in bytes of the raw line, so a line of
    multi-byte characters reaches it sooner) means the file is not an analog CSV (False);
    one among the sampled lines is passed over."""
    seen = 0
    lines = 0
    try:
        with open(path, "rb") as f:
            while seen < max_scan_rows and lines < max_scan_rows + _PREAMBLE_MAX_ROWS:
                line = f.readline(MAX_LINE_CHARS + 1)
                if not line:
                    return False                 # whole file read: only 0/1 seen
                lines += 1
                if len(line) > MAX_LINE_CHARS:
                    return False                 # not a scope export's row
                row = _row_of_line(line)
                vals = _numeric_channels(row)
                if not vals:
                    continue
                seen += 1
                if _non_binary(vals):
                    return True                  # a non-binary value -> analog
            start = f.tell()
            size = os.fstat(f.fileno()).st_size
            step = (size - start) / float(max_scan_rows)
            for k in range(max_scan_rows):
                f.seek(int(start + k * step))
                if k:
                    f.readline(MAX_LINE_CHARS)  # finish the line the offset fell in
                line = f.readline(MAX_LINE_CHARS + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_CHARS:
                    continue
                row = _row_of_line(line)
                vals = _numeric_channels(row)
                if vals and _non_binary(vals):
                    return True
    except (OSError, csv.Error):
        return False
    return False                                 # only 0/1 seen -> digital


# How far a preamble may run before the TIME header row: a scope's key/value preamble
# is a few dozen rows and a few KB, so a file without the header is refused within these
# bounds instead of being read to its end.
_PREAMBLE_MAX_ROWS = 200
_PREAMBLE_MAX_BYTES = 1 << 20


def _no_header_error(path: str) -> ValueError:
    return ValueError(
        f"{path}: no TIME header row found in the preamble (the rows before the first "
        f"data row, at most {_PREAMBLE_MAX_ROWS}); a scope CSV export's header row "
        f"starts with TIME")


def _is_data_row(row: List[str]) -> bool:
    """True for a row whose time cell and every channel cell are numbers."""
    if not row:
        return False
    try:
        float(row[0])
    except ValueError:
        return False
    return _numeric_channels(row) is not None


def _split_header(rows, path: str) -> tuple:
    """``(preamble rows, TIME header row)`` from an iterator of CSV rows, consumed up to
    and including the header row (first field case-insensitively "TIME"). Raises
    ValueError naming `path` at the first data row, or after `_PREAMBLE_MAX_ROWS` rows,
    with no header before it."""
    pre: List[List[str]] = []
    for row in rows:
        if row and row[0].strip().upper() == "TIME":
            return pre, row
        if len(pre) >= _PREAMBLE_MAX_ROWS or _is_data_row(row):
            break
        pre.append(row)
    raise _no_header_error(path)


def _rows_of(f, path: str):
    """The CSV rows of binary file `f` from its position, one line at a time, stopping
    once `_PREAMBLE_MAX_BYTES` have been read. A line the csv module cannot read is no
    header row: the no-header error naming `path`."""
    used = 0
    while used < _PREAMBLE_MAX_BYTES:
        line = f.readline(_PREAMBLE_MAX_BYTES - used)
        if not line:
            return
        used += len(line)
        try:
            yield _row_of_line(line)
        except csv.Error:                    # a cell over the csv field limit
            raise _no_header_error(path) from None


def _parse_preamble(rows: List[List[str]]) -> Dict[str, List[str]]:
    """Collect the preamble's key/value pairs. Each preamble row is one or more
    ``key,value`` pairs (a multi-channel export repeats the pair per channel,
    separated by an empty column) — e.g. ``Sample Interval,1.6e-10,,Sample
    Interval,1.6e-10``. Returns each key mapped to the list of values seen
    (one per channel block, in file order); a blank/short row is skipped.

    A row is read left to right: an empty cell where a key is expected is a block
    separator and is skipped, any other cell is a key and the cell after it its
    value (empty when the row ends)."""
    out: Dict[str, List[str]] = {}
    for row in rows:
        i = 0
        while i < len(row):
            key = row[i].strip()
            if not key:
                i += 1
                continue
            val = row[i + 1].strip() if i + 1 < len(row) else ""
            out.setdefault(key, []).append(val)
            i += 2
    return out


# Data rows per parsed batch. Each row is a list of Python strings until its batch is
# converted, so this bounds that working set.
_BATCH_ROWS = 50_000
# Python-object bytes per row of a batch beyond its text: the row list with its GC
# header and item pointers, and one str header per cell. CPython: a list is 56 bytes
# plus 8 per item, an ASCII str 41 bytes plus its text (a number cell is not cached).
_ROW_OBJECT_BYTES = 100
_CELL_OBJECT_BYTES = 60


def _named_header(row: List[str]):
    """``(header, blank, repeated)`` of a TIME header `row`: the stripped cells without
    trailing empties, with every channel name unique. A blank channel cell reads
    ``Column N`` (N the 1-based file column) and a repeat of a name reads ``NAME (2)``,
    ``NAME (3)`` and so on, skipping any name the header already holds. `blank` lists
    the file columns renamed for being blank, `repeated` the names that occurred more
    than once, in order."""
    cells = [c.strip() for c in _trimmed(row)]
    taken = set(cells[1:])
    seen = set()
    blank: List[int] = []
    repeated: List[str] = []
    out = cells[:1]
    for c, name in enumerate(cells[1:], start=1):
        if not name:
            blank.append(c + 1)
            name = f"Column {c + 1}"
        if name in seen:
            if name not in repeated:
                repeated.append(name)
            k = 2
            while f"{name} ({k})" in taken or f"{name} ({k})" in seen:
                k += 1
            name = f"{name} ({k})"
        seen.add(name)
        taken.add(name)
        out.append(name)
    return out, blank, repeated


def scan_header(path: str):
    """``(preamble rows, header, byte offset of the first data row, sampled data-row
    text)`` of an analog CSV, reading only the preamble (within `_PREAMBLE_MAX_ROWS` and
    `_PREAMBLE_MAX_BYTES`) and 64 KB past the TIME header row. The header holds TIME and
    then one unique name per channel column (`_named_header`). Raises ValueError when
    there is no TIME header within them."""
    with open(path, "rb") as f:
        pre, row = _split_header(_rows_of(f, path), path)
        header = _named_header(row)[0]
        offset = f.tell()
        sample = f.read(64 << 10)
    return pre, header, offset, sample


def predict_peak_bytes(path: str) -> int:
    """Predicted peak of reading `path` with `read_analog_csv` and thresholding two
    of its channels (analog.capture_from_analog), before any data row is read.

    Rows are estimated from the file size and the width of the sampled rows. Every
    column is kept as float64 (8 per column per row); on top of that comes the larger
    of joining a column's batches into one (8 per row, one column at a time) and the
    analog front end (`analog.front_end_bytes`); and one batch of `_BATCH_ROWS` rows is
    live as Python objects while it is parsed."""
    return _predict(path)[1]


def _predict(path: str):
    """``(estimated data rows, predict_peak_bytes)`` of `path`."""
    _pre, header, offset, sample = scan_header(path)
    rows, row_text = _estimate_rows(path, offset, sample)
    ncols = max(1, len(header))
    batch = _BATCH_ROWS * (_ROW_OBJECT_BYTES + ncols * _CELL_OBJECT_BYTES + row_text)
    return rows, int(rows * 8 * ncols + max(8 * rows, analog.front_end_bytes(rows)) + batch)


def _estimate_rows(path: str, offset: int, sample: bytes):
    """``(data rows, mean row text bytes)`` from the file size past `offset` and the
    mean width of the whole rows in `sample`."""
    lines = [ln for ln in sample.split(b"\n")[:-1] if ln.strip()]
    if not lines:
        return 0, 0.0
    mean = sum(len(ln) + 1 for ln in lines) / len(lines)
    size = os.path.getsize(path) - offset
    return int(size / mean) if mean > 0 else 0, mean - 1


def _bad_row_error(path: str, number: int, text: str) -> ValueError:
    return ValueError(f"{path}: data row {number:,} after the TIME header is not all "
                      f"numbers: {text!r}")


def _to_block(path: str, rows: List[List[str]], numbers: List[int]):
    """``(float64 block, cut)`` of a batch of rectangular data rows of `path`, whose
    numbers after the TIME header (counting every row, kept or not) are `numbers`. When
    a cell does not parse and the batch's last row is the only such row, that row is
    dropped and `cut` is ``(its row number, its text)``, else None: it is the file's
    partly written last row only if no further data row follows, which the caller
    checks. Any other unreadable row raises ValueError naming it."""
    try:
        return np.asarray(rows, dtype=np.float64), None
    except ValueError:
        pass
    last = len(rows) - 1
    for k, row in enumerate(rows):
        try:
            np.asarray(row, dtype=np.float64)
        except ValueError:
            if k != last:
                raise _bad_row_error(path, numbers[k], ",".join(row)) from None
            block = (np.asarray(rows[:last], dtype=np.float64) if last
                     else np.zeros((0, len(row)), dtype=np.float64))
            return block, (numbers[k], ",".join(row))
    return np.asarray(rows, dtype=np.float64), None


def _mhz(rate_hz: float) -> str:
    return f"{rate_hz / 1e6:.6g} MHz"


@text_csv
def read_analog_csv(path: str, max_bytes: Optional[int] = None) -> Dict[str, object]:
    """Parse a scope analog CSV export into ``{"time", "channels",
    "sample_rate_hz"}``: `time` is the shared TIME column (seconds, may start
    negative); `channels` maps each header name (e.g. "CH1") after TIME to its
    float64 volts array, a repeated name numbered and a blank one called ``Column N``
    so every column is kept; `sample_rate_hz` is derived from the TIME column's
    median spacing (preferred over the preamble's "Sample Interval"; see
    `_infer_rate`).

    Data rows are parsed `_BATCH_ROWS` at a time. Before any is read the memory guard
    compares `predict_peak_bytes` with the shared `memory_budget()`: `max_bytes`
    follows the .sal convention (None uses the computed budget, a positive value
    overrides it, 0 opts out) and :class:`analog.AnalogTooLargeError` is raised over
    budget.

    The file's last data row is dropped when a cell of it does not parse as a number
    (a partly written file); any other such row raises ValueError naming the file, the
    row's number after the TIME header and its text.

    Import warnings (import_notes) report the renamed header cells, the dropped last row,
    rows too short for the header that were skipped, rows with no TIME value that were
    skipped, cells past the header's columns that were ignored, a preamble Sample Interval
    that is not a positive time giving a usable rate (it is not used), and a TIME column
    whose spacing disagrees with the preamble's Sample Interval. A file that is not UTF-8
    text, or text the csv module rejects, raises ValueError naming it."""
    if max_bytes != 0:
        rows, predicted = _predict(path)
        analog.check_budget(path, rows, predicted, max_bytes)

    with open(path, newline="", encoding="utf-8-sig") as f:      # a BOM is not a cell
        r = csv.reader(f)
        preamble_rows, header_row = _split_header(r, path)
        header, blank, repeated = _named_header(header_row)
        ncols = len(header)
        parts: List[List[np.ndarray]] = [[] for _ in range(ncols)]
        cut = None             # (data row number, text) of a dropped unreadable last row
        line = 1               # rows read after the TIME header, the next one's number
        short = [0, 0]         # non-blank rows too short for the header: [count, first]
        no_time = [0, 0]       # non-blank rows of full width with no TIME cell: [count, first]
        extra = [0, 0]         # rows with non-blank cells past the header's: [count, first]
        while True:
            raw = list(islice(r, _BATCH_ROWS))
            if not raw:
                break
            base = line
            line += len(raw)
            # A trailing blank line (or short row) is common at EOF; drop rows that
            # can't possibly hold every column, and truncate rows with more columns
            # than the header (e.g. a stray trailing comma) so each block is
            # rectangular.
            keep = [k for k, row in enumerate(raw) if len(row) >= ncols and row[0].strip()]
            rows = [raw[k][:ncols] for k in keep]
            lens = set(map(len, raw))
            if len(rows) != len(raw):
                for k, row in enumerate(raw):
                    if len(row) >= ncols and row[0].strip() or not any(
                            c.strip() for c in row):
                        continue
                    bucket = short if len(row) < ncols else no_time
                    if not bucket[0]:
                        bucket[1] = base + k
                    bucket[0] += 1
            if max(lens) > ncols:
                for k, row in enumerate(raw):
                    if len(row) > ncols and any(c.strip() for c in row[ncols:]):
                        if not extra[0]:
                            extra[1] = base + k
                        extra[0] += 1
            del raw
            if not rows:
                continue
            if cut is not None:
                raise _bad_row_error(path, *cut)
            block, cut = _to_block(path, rows, [base + k for k in keep])
            del rows
            for c in range(ncols):
                parts[c].append(np.ascontiguousarray(block[:, c]))
            del block

    if sum(p.size for p in parts[0]) < 2:
        raise ValueError(f"{path}: not enough data rows after the TIME header")
    columns = []
    for c in range(ncols):                   # one column's batches joined at a time
        col = parts[c][0] if len(parts[c]) == 1 else np.concatenate(parts[c])
        parts[c] = []
        columns.append(col)
    time = columns[0]
    channels = {header[c]: columns[c] for c in range(1, ncols)}

    preamble = _parse_preamble(preamble_rows)
    sample_rate_hz, column_rate, from_preamble = _infer_rate(time, preamble)

    src = import_notes.source_name(path)
    if blank:
        import_notes.warn(src, f"the TIME header has no name for column"
                               f"{'s' if len(blank) != 1 else ''} "
                               f"{', '.join(map(str, blank))}; read as "
                               f"{', '.join(repr(header[c - 1]) for c in blank)}")
    if repeated:
        import_notes.warn(src, f"the TIME header repeats the name"
                               f"{'s' if len(repeated) != 1 else ''} "
                               f"{', '.join(map(repr, repeated))}; every column is kept, "
                               f"the later ones numbered ('{repeated[0]} (2)')")
    if cut is not None:
        import_notes.warn(src, f"the last data row ({cut[0]:,} after the TIME header) is not "
                               f"all numbers and was dropped (the file may be partly "
                               f"written): {quoted(cut[1])}")
    if short[0]:
        n = short[0]
        import_notes.warn(src, f"{n:,} row{'s' if n != 1 else ''} with fewer cells than the "
                               f"{ncols} columns of the TIME header "
                               f"{'were' if n != 1 else 'was'} skipped (first: row "
                               f"{short[1]:,} after the TIME header)")
    if no_time[0]:
        n = no_time[0]
        import_notes.warn(src, f"{n:,} row{'s' if n != 1 else ''} with no TIME value "
                               f"{'were' if n != 1 else 'was'} skipped (first: row "
                               f"{no_time[1]:,} after the TIME header)")
    if extra[0]:
        n = extra[0]
        import_notes.warn(src, f"{n:,} row{'s' if n != 1 else ''} with cells past the "
                               f"{ncols} columns of the TIME header: those cells are "
                               f"ignored (first: row {extra[1]:,} after the TIME header)")
    unusable = _interval_problem(preamble)
    if unusable:
        import_notes.warn(src, f"the preamble's Sample Interval {quoted(unusable)} is not a "
                               f"positive time that gives a rate from 1 Hz to "
                               f"{MAX_RATE_HZ:,.0f} Hz; it is not used")
    interval = _usable_interval(preamble)
    if interval and column_rate > 0 \
            and abs(column_rate * interval - 1.0) > 1e-3:
        import_notes.warn(src, f"the TIME column's spacing gives {_mhz(column_rate)} and "
                               f"the preamble's Sample Interval {_mhz(1.0 / interval)}; "
                               + ("the TIME values are too coarse to resolve that interval, "
                                  "so the preamble's is used" if from_preamble
                                  else "the TIME column's is used"))

    return {"time": time, "channels": channels, "sample_rate_hz": sample_rate_hz}


def _whole_hz(rate_hz: float) -> float:
    """`rate_hz` rounded to a whole Hz (a rate below 1 Hz is kept), so a timebase whose
    spacing carries rounding error in its last digit does not read a hertz short."""
    return float(round(rate_hz)) if 1.0 <= rate_hz < math.inf else rate_hz


def _time_resolves(time: np.ndarray, interval: float) -> bool:
    """True if the float64 TIME values can tell samples `interval` seconds apart: the
    gap between adjacent floats at the largest |time| is under 1 % of the interval."""
    finite = time[np.isfinite(time)]
    if finite.size == 0:
        return True
    top = max(abs(float(finite.min())), abs(float(finite.max())))
    return float(np.spacing(top)) < 0.01 * interval


def _interval_problem(preamble: Dict[str, List[str]]) -> str:
    """The text of the preamble's Sample Interval when the field is present and is not a
    positive time giving a rate from 1 Hz to `MAX_RATE_HZ`, else ""."""
    vals = preamble.get("Sample Interval")
    text = vals[0].strip() if vals else ""
    try:
        interval = float(text)
    except ValueError:
        return text
    if interval > 0 and 1.0 <= 1.0 / interval <= MAX_RATE_HZ:
        return ""
    return text


def _usable_interval(preamble: Dict[str, List[str]]) -> float:
    """The preamble's Sample Interval in seconds, or 0.0 when it is absent or
    `_interval_problem` rejects it."""
    if _interval_problem(preamble):
        return 0.0
    vals = preamble.get("Sample Interval")
    return float(vals[0]) if vals and vals[0].strip() else 0.0


def _infer_rate(time: np.ndarray, preamble: Dict[str, List[str]]):
    """``(sample rate, the TIME column's own rate or 0, whether the preamble's Sample
    Interval gave the rate)``. The TIME column's median spacing is preferred over the
    preamble's "Sample Interval" field: the column reflects what was actually exported
    (e.g. after any decimation), while the preamble records the scope's acquisition
    interval. The preamble's interval is used instead when the two disagree by more than
    0.1 % and the column's float64 values are too coarse to resolve that interval
    (`_time_resolves`: a long absolute time axis), and when the column has fewer than 2
    rows to diff."""
    interval = _usable_interval(preamble)
    column = 0.0
    if time.size >= 2:
        med = analog._median_positive_step(time)
        if med > 0:
            column = _whole_hz(1.0 / med)
    if column > 0:
        if interval and abs(column * interval - 1.0) > 1e-3 \
                and not _time_resolves(time, interval):
            return _whole_hz(1.0 / interval), column, True
        return column, column, False
    if interval:
        return _whole_hz(1.0 / interval), 0.0, True
    return 0.0, 0.0, False


def sample_interval(preamble_or_path: object) -> Optional[float]:
    """Recover the scope's "Sample Interval" preamble field, given either an
    already-parsed preamble dict (as produced internally) or a file path.
    Returns None if the field isn't present. Exposed for callers that want the
    raw acquisition interval rather than `read_analog_csv`'s preferred (TIME
    column-derived) rate."""
    if isinstance(preamble_or_path, dict):
        preamble = preamble_or_path
    else:
        try:
            rows = scan_header(str(preamble_or_path))[0]
        except ValueError:
            return None                      # no TIME header: not a scope CSV export
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
    parse — reads only the preamble + TIME header row (`scan_header`), not the data
    rows, so the UI can enumerate channels (e.g. to populate a clock/data picker).
    Raises ValueError when there is no TIME header within the preamble's bounds."""
    return scan_header(path)[1][1:]
