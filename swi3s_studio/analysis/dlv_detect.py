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

CSV timing: a Logic *edge* export lists one row per transition, so the finest
timestamp spacing is one UI, not one sample — ``digital_csv.infer_sample_rate``
(min spacing) under-reports the rate by the UI/sample ratio. The true sample
period is the GCD of the transition spacings (:func:`true_sample_rate_csv`), which
the DLV DLL needs to seed its row-period and keep enough samples per UI for the
mid-UI bit sampling.
"""
from __future__ import annotations

import csv

import numpy as np
import swi3score

from ..ingest.capture import Capture

# Logic's digital sample-rate menu (Hz). The GCD-derived rate is snapped to the
# nearest of these — a real capture is always at one of them, and snapping cleans up
# any residual from timestamp rounding.
_STANDARD_RATES_HZ = [500_000_000, 250_000_000, 125_000_000, 100_000_000, 50_000_000,
                      25_000_000, 20_000_000, 12_500_000, 10_000_000, 6_250_000,
                      5_000_000, 4_000_000, 2_500_000, 2_000_000, 1_000_000]

# The DLV row reference rate is constant (§ recovered-clock): 3.072 MRows/s.
_DLV_ROW_RATE_HZ = 3_072_000.0
# Candidate Safe-Lock column widths, widest first (16 = operational, 2/4 = Safe-Lock).
_CANDIDATE_COLUMNS = (16, 8, 4, 2)


def snap_to_standard_rate(rate_hz: float) -> int:
    """Snap a rate to the nearest standard Logic sample rate when within 2 %; else
    round to the nearest kHz (digital capture rates are kHz-aligned)."""
    best = min(_STANDARD_RATES_HZ, key=lambda s: abs(s - rate_hz))
    if abs(best - rate_hz) <= 0.02 * best:
        return int(best)
    return int(round(rate_hz / 1000.0) * 1000)


def true_sample_rate_csv(path: str, max_rows: int = 500_000) -> int:
    """The true analyzer sample rate of a Logic digital-CSV *edge* export, from the
    GCD of the transition-time spacings (each is an integer number of sample periods),
    snapped to the nearest standard rate. Returns 0 if it can't be determined.

    Unlike ``digital_csv.infer_sample_rate`` (finest spacing == one *sample*), this is
    correct for an edge export where the finest spacing is one *UI* (many samples)."""
    times = []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)                                    # header
        for i, row in enumerate(r):
            if i >= max_rows or not row:
                break
            try:
                times.append(float(row[0]))
            except ValueError:
                pass
    if len(times) < 3:
        return 0
    d = np.diff(np.asarray(times, dtype=np.float64))
    d = d[d > 0]
    if d.size == 0:
        return 0
    # Work in integer picoseconds so the GCD is exact (standard rates have integer-ps
    # periods: 500 MHz -> 2000 ps, 12.5 MHz -> 80000 ps).
    ps = np.rint(d * 1e12).astype(np.int64)
    ps = ps[ps > 0]
    if ps.size == 0:
        return 0
    quantum = int(np.gcd.reduce(ps))
    if quantum <= 0:
        return 0
    return snap_to_standard_rate(1e12 / quantum)


def is_complementary(capture: Capture, tol: float = 0.005) -> bool:
    """True when the capture's two channels form a DLV differential pair: they toggle
    together (same edge samples) and start at opposite levels, so ``data == NOT clock``
    throughout. `tol` allows a small fraction of edges to differ (sampling skew)."""
    ce = capture.clock_edges                         # already contiguous uint64 (Capture.__init__)
    de = capture.data_edges
    # Cheap rejections FIRST — a non-pair (opposite initial levels, or very different
    # edge counts) is the common FBCSE case, and these tests need only sizes/flags. Do
    # them before touching the (100s-of-millions-of-element) edge arrays: converting
    # both to int64 up front copied gigabytes just to reject on size a line later.
    if ce.size == 0 or de.size == 0 or bool(capture.initial_clock) == bool(capture.initial_data):
        return False
    if abs(ce.size - de.size) > tol * max(ce.size, de.size):
        return False
    n = min(ce.size, de.size)
    # Element-wise compare of the shared prefix; a uint64 != is exact, so no int64 copy.
    return bool(np.count_nonzero(ce[:n] != de[:n]) <= tol * n)


def detect_columns(capture: Capture, *, candidates=_CANDIDATE_COLUMNS,
                   max_edges: int = 2_000_000, min_valid: int = 4,
                   min_valid_ratio: float = 0.5):
    """Blind-detect the DLV column count AND wire polarity by decoding a prefix of the
    capture under each candidate Safe-Lock width, in each DP/DN orientation, and keeping
    the combination that yields the most CRC-valid commands. Returns
    ``(columns, valid, total, valid_ratio, row_rate_khz, inverted)`` or None when nothing
    decodes convincingly (not a DLV capture, or too short). `inverted` is True when the
    captured pair is DP/DN-swapped — a differential swap inverts every logical level, so
    the CDS bits and 8b/10b fail unless the initial DP level is flipped.

    The virtual PLL locks to the once-per-row reference for any seed/polarity, so
    `row_rate_khz` lands at ~3.072 MHz regardless; the count and polarity are
    disambiguated purely by which framing produces valid CRCs."""
    ce = np.ascontiguousarray(capture.clock_edges, dtype=np.uint64)
    if ce.size < 64:
        return None
    edges = np.ascontiguousarray(ce[:max_edges], dtype=np.uint64)
    rate = int(capture.sample_rate_hz)
    init_dp = bool(capture.initial_clock)
    best = None
    for cols in candidates:
        nominal_ui = rate / (_DLV_ROW_RATE_HZ * cols) if cols > 0 else 0.0
        for inverted in (False, True):
            src = swi3score.DlvSampleSource(edges, (not init_dp) if inverted else init_dp,
                                            rate, int(cols), nominal_ui)
            settings = swi3score.DecoderSettings()
            settings.dlv = True
            settings.cds_horizontal_start = 2            # Safe-Lock CDS position (§12.1.10.1)
            settings.forced_column_count = int(cols)
            dec = swi3score.Decoder(src, settings)
            dec.run()
            cmds = dec.commands()
            total = len(cmds)
            valid = sum(1 for c in cmds if c.get("crc_valid"))
            ratio = (valid / total) if total else 0.0
            cand = (int(cols), valid, total, ratio, float(dec.row_rate_khz), inverted)
            if best is None or valid > best[1]:         # most CRC-valid commands wins
                best = cand
            if ratio >= 0.99 and valid >= min_valid:     # a clean decode — no need to try more
                return cand
    if best is not None and best[1] >= min_valid and best[3] >= min_valid_ratio:
        return best
    return None
