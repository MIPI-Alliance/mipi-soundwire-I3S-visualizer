"""Blind detection for a **partial** DLV (PHY3) capture — one that starts mid-stream
with no §5.1.2 cold-start bring-up, so nothing on the wire names the PHY, the
Safe-Lock column count, or the audio-start point.

A DLV link is a differential pair: the two captured channels are always opposite
(``data == NOT clock``). That complementary signature is what marks a capture as
DLV without a bring-up (:func:`is_complementary`). The recovered-clock front-end
(``DlvSampleSource``) then locks to the once-per-row Sync edges regardless of the
column count, so the row structure (RSPs) comes out at the constant 3.072 MRows/s
reference rate for any seed; the **column count** is recovered by trying each
Safe-Lock width and keeping the one whose framing yields CRC-valid commands
(:func:`detect_columns`).

CSV timing: a digital-CSV *edge* export lists one row per transition, so the finest
timestamp spacing is one UI, not one sample. :func:`true_sample_rate_csv` takes the
sample period as the GCD of the transition spacings over up to ``max_rows`` rows and
snaps the rate to a standard analyzer rate (:func:`snap_to_standard_rate`), reporting
a snap that moves it. The DLV DLL needs that rate to seed its row-period and keep
enough samples per UI for the mid-UI bit sampling.
"""
from __future__ import annotations

import csv
import math

import numpy as np
import swi3score

from .. import import_notes
from ..ingest.capture import Capture
from ..ingest.digital_csv import time_unit
from ..ingest.timebase import text_csv
from ..ingest.transitions import DLV_ROW_RATE_HZ

# The analyzer's digital sample-rate menu (Hz). The GCD-derived rate is snapped to the
# nearest of these — a real capture is always at one of them, and snapping cleans up
# any residual from timestamp rounding.
_STANDARD_RATES_HZ = [500_000_000, 250_000_000, 125_000_000, 100_000_000, 50_000_000,
                      25_000_000, 20_000_000, 12_500_000, 10_000_000, 6_250_000,
                      5_000_000, 4_000_000, 2_500_000, 2_000_000, 1_000_000]

# The DLV row reference rate is constant (§ recovered-clock): 3.072 MRows/s. It is defined
# once, in ingest.transitions (DLV_ROW_RATE_HZ), and shared with the demo builder.

# The recovered clock samples each UI at its middle and needs at least this many samples
# per UI (DlvSampleSource's kMinUiSamples, swi3score.dlv_min_ui_samples); a candidate
# width with fewer is not decoded.
MIN_UI_SAMPLES = 1.0
# PHY3 CDS_HorizontalStart's reset value, the column the CDS is read at until a commit moves
# it: the core's kDlvCdsColumn (swi3score.dlv_cds_column).
DLV_CDS_COLUMN = 2
# Candidate column widths, narrowest first (2/4 = Safe-Lock, 16 = operational): on a tie in
# CRC-valid commands the narrower width wins.
_CANDIDATE_COLUMNS = (2, 4, 8, 16)


def snap_to_standard_rate(rate_hz: float) -> int:
    """Snap a rate to the nearest standard analyzer sample rate when within 2 %; else
    round to the nearest kHz (digital capture rates are kHz-aligned)."""
    best = min(_STANDARD_RATES_HZ, key=lambda s: abs(s - rate_hz))
    if abs(best - rate_hz) <= 0.02 * best:
        return int(best)
    return int(round(rate_hz / 1000.0) * 1000)


# The largest timestamp spacing, in picoseconds, the GCD reads (below 2**63).
_MAX_SPACING_PS = 2.0 ** 62


@text_csv
def true_sample_rate_csv(path: str, max_rows: int = 500_000) -> int:
    """The true analyzer sample rate of a digital-CSV *edge* export, from the
    GCD of the transition-time spacings (each is an integer number of sample periods),
    snapped to the nearest standard rate. Returns 0 if it can't be determined. Bytes that
    are not UTF-8 text, or text the csv module rejects (a cell past its field limit),
    raise ValueError naming the file. The times are in the unit the header's Time cell
    names (`digital_csv.time_unit`, seconds when it names none), as the loader reads them;
    the rows are read up to the first blank one.

    The finest spacing of an edge export is one *UI* (many samples), so the rate is the
    grid every spacing is a whole multiple of, not the finest spacing itself."""
    times = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        scale = time_unit(next(r, []))[0]                # the header names the Time unit
        for i, row in enumerate(r):
            if i >= max_rows or not row:
                break
            try:
                t = float(row[0])
            except ValueError:
                continue
            if math.isfinite(t):                         # a nan or inf time is no edge
                times.append(t)
    if len(times) < 3:
        return 0
    d = np.diff(np.asarray(times, dtype=np.float64) * scale)
    # Work in integer picoseconds so the GCD is exact (standard rates have integer-ps
    # periods: 500 MHz -> 2000 ps, 12.5 MHz -> 80000 ps). A spacing whose picosecond
    # count does not fit int64 is left out.
    d = d[(d > 0) & (d * 1e12 < _MAX_SPACING_PS)]
    if d.size == 0:
        return 0
    ps = np.rint(d * 1e12).astype(np.int64)
    ps = ps[ps > 0]
    if ps.size == 0:
        return 0
    quantum = int(np.gcd.reduce(ps))
    if quantum <= 0:
        return 0
    raw = 1e12 / quantum
    rate = snap_to_standard_rate(raw)
    if rate <= 0:
        # Under 500 Hz the kHz rounding leaves no rate: the spacing names none to load at.
        import_notes.warn(import_notes.source_name(path),
                          f"the timestamps' spacing gives a sample rate of {raw:,.3g} Hz, "
                          f"which rounds to no analyzer rate; it is not used")
        return 0
    if abs(rate - raw) > 1e-3 * rate:
        import_notes.warn(import_notes.source_name(path),
                          f"the timestamps' spacing gives a sample rate of {raw:,.0f} Hz; "
                          f"it is snapped to the standard rate of {rate:,} Hz")
    return rate


def is_complementary(capture: Capture, tol: float = 0.005) -> bool:
    """True when the capture's two channels form a DLV differential pair: they toggle
    together (same edge samples) and start at opposite levels, so ``data == NOT clock``
    throughout. Edges are compared by position in the two lists, so only edges that fall on
    the same sample count as agreeing; `tol` is the fraction of edges (and of the edge
    count difference) allowed to disagree."""
    ce = capture.clock_edges                         # already contiguous uint64 (Capture.__init__)
    de = capture.data_edges
    # Cheap rejections first (initial levels, edge counts): they need only sizes/flags,
    # so a non-pair, the common FBCSE case, is rejected before the edge arrays are read.
    if ce.size == 0 or de.size == 0 or bool(capture.initial_clock) == bool(capture.initial_data):
        return False
    if abs(ce.size - de.size) > tol * max(ce.size, de.size):
        return False
    n = min(ce.size, de.size)
    # Element-wise compare of the shared prefix; a uint64 != is exact, so no int64 copy.
    return bool(np.count_nonzero(ce[:n] != de[:n]) <= tol * n)


def rate_too_low(sample_rate_hz, candidates=_CANDIDATE_COLUMNS) -> bool:
    """True when no candidate width has :data:`MIN_UI_SAMPLES` samples per UI at
    `sample_rate_hz`, so :func:`detect_columns` tries none."""
    row = float(sample_rate_hz) / DLV_ROW_RATE_HZ
    return not any(c > 0 and row / c >= MIN_UI_SAMPLES for c in candidates)


def detect_columns(capture: Capture, *, candidates=_CANDIDATE_COLUMNS,
                   max_edges: int = 2_000_000, min_valid: int = 4,
                   min_valid_ratio: float = 0.5):
    """Blind-detect the DLV column count AND wire polarity by decoding a prefix of the
    capture under each candidate width, in each DP/DN orientation, and keeping the
    combination that yields the most CRC-valid commands (ties: the higher valid ratio, then
    the earlier candidate — narrowest first by default). Every candidate is decoded: a
    decode forced to the width the capture starts at follows its later NumColumns commits and
    so reads every command, but a wider width the capture commits to later also decodes
    cleanly, over that stretch only. Returns
    ``(columns, valid, total, valid_ratio, row_rate_khz, inverted)`` or None when nothing
    decodes convincingly (not a DLV capture, or too short). `inverted` is True when the
    captured pair is DP/DN-swapped — a differential swap inverts every logical level, so
    the CDS bits and 8b/10b fail unless the initial DP level is flipped.

    The virtual PLL locks to the once-per-row reference for any seed/polarity, so
    `row_rate_khz` lands at ~3.072 MHz regardless; the count and polarity are
    disambiguated purely by which framing produces valid CRCs. A width whose UI at the
    capture's rate is under :data:`MIN_UI_SAMPLES` samples is not tried."""
    ce = np.ascontiguousarray(capture.clock_edges, dtype=np.uint64)
    if ce.size < 64:
        return None
    edges = np.ascontiguousarray(ce[:max_edges], dtype=np.uint64)
    rate = int(capture.sample_rate_hz)
    init_dp = bool(capture.initial_clock)
    best = None
    for cols in candidates:
        nominal_ui = rate / (DLV_ROW_RATE_HZ * cols) if cols > 0 else 0.0
        if not nominal_ui >= MIN_UI_SAMPLES:
            continue
        for inverted in (False, True):
            src = swi3score.DlvSampleSource(edges, (not init_dp) if inverted else init_dp,
                                            rate, int(cols), nominal_ui)
            settings = swi3score.DecoderSettings()
            settings.dlv = True
            settings.cds_horizontal_start = DLV_CDS_COLUMN
            settings.forced_column_count = int(cols)
            dec = swi3score.Decoder(src, settings)
            dec.run()
            cmds = dec.commands()
            total = len(cmds)
            valid = sum(1 for c in cmds if c.get("crc_valid"))
            ratio = (valid / total) if total else 0.0
            cand = (int(cols), valid, total, ratio, float(dec.row_rate_khz), inverted)
            if best is None or (valid, ratio) > (best[1], best[3]):   # most CRC-valid wins
                best = cand
    if best is not None and best[1] >= min_valid and best[3] >= min_valid_ratio:
        return best
    return None
