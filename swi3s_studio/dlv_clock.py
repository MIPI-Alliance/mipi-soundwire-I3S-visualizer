"""The PHY3 (DLV) recovered clock: the sample source a DLV decode reads, the partial-capture
detection that routes a capture to it, and the Row Sync Points one decode's virtual PLL
recovered.

`DlvClockAnalysis` is built per decode from that decode's decoder, capture, settings and
sample source, and caches what it derives; a new decode builds a new one. The module
functions configure a decode before it runs. Session holds one analysis per decode and
forwards its public names to it.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

import numpy as np
import swi3score

from .ingest.capture import Capture

# A recovered clock with more than this many missing Row-Sync edges is reported as unlocked:
# a handful of gaps is tolerable jitter, a sustained run means the virtual PLL lost the row
# cadence.
PLL_UNLOCK_MISSING_THRESHOLD = 5


class DlvClockAnalysis:
    """The Row Sync Points and missing opening edges of one decode, from its decoder's
    recorded Column-0 samples and its sample source's recovered UI. A decode that is not
    DLV (settings.dlv False) has none: empty arrays and a UI of 0.0."""

    def __init__(self, decoder, capture: Capture, settings, source) -> None:
        self.decoder = decoder
        self.capture = capture
        self.source = source
        self.dlv = bool(settings.dlv)
        # (rsp, missing, ui) and the sorted Row Sync Points, computed on first use.
        self.rsp_cache: Optional[Tuple[np.ndarray, np.ndarray, float]] = None
        self.row_syncs_cache: Optional[np.ndarray] = None

    def rsp_analysis(self) -> Tuple[np.ndarray, np.ndarray, float]:
        """(rsp, missing, ui), cached. `rsp` = the virtual PLL's recovered row-opening edges
        (the DP-wire 0→1 edge that opens each row), used directly as the Row Sync Points.
        `missing` = the subset of rows whose opening edge is absent on the wire (no DP-wire
        0→1 edge just before the recovered Column-0 mid-UI sample: a recovered-clock lock
        gap). `ui` = the recovered (operational) UI period. Empty / 0.0 for FBCSE.

        The decoder records each Column-0 UI at its MID-UI sample; the opening edge sits
        half a UI earlier. That half-UI differs per geometry (a Safe-Lock UI is wider than
        the operational UI), so each mid-UI sample is snapped back to the nearest DP 0→1
        edge at or before it, within half a ROW period, which holds across geometries since
        the row rate is constant."""
        if self.rsp_cache is not None:
            return self.rsp_cache
        ui_fn = getattr(self.source, "recovered_ui_samples", None)
        rows_fn = getattr(self.decoder, "row_sync_samples", None)
        if ui_fn is None or rows_fn is None:
            self.rsp_cache = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), 0.0)
            return self.rsp_cache
        ui = float(ui_fn())
        mid = np.asarray(rows_fn(), dtype=np.int64)          # Column-0 mid-UI samples
        half = int(round(ui * 0.5)) if ui > 0 else 0
        ce = np.asarray(self.capture.clock_edges, dtype=np.int64)
        first_rise = 1 if bool(self.capture.initial_clock) else 0
        rising = ce[first_rise::2]                            # DP-wire 0→1 edges
        if ui <= 0.0 or mid.size == 0 or rising.size == 0:
            rsp = np.maximum(mid - half, 0)
            self.rsp_cache = (rsp, np.zeros(0, dtype=np.int64), ui)
            return self.rsp_cache
        # The opening edge of each row is the largest DP 0→1 edge <= its Column-0 mid-UI
        # sample, accepted when within half a row period; beyond that the edge is missing
        # and the RSP is predicted half a UI before the mid-UI sample.
        row_period = int(np.median(np.diff(mid))) if mid.size > 1 else max(1, 2 * half)
        tol = max(1, row_period // 2)
        j = np.clip(np.searchsorted(rising, mid, side="right") - 1, 0, rising.size - 1)
        edge = rising[j]
        gap = mid - edge
        present = (gap >= 0) & (gap <= tol)
        rsp = np.where(present, edge, np.maximum(mid - half, 0))
        self.rsp_cache = (rsp, rsp[~present], ui)
        return self.rsp_cache

    def recovered_clock(self):
        """(row_sync_samples uint64 ndarray, ui_samples float), or None when there is no
        recovered clock (FBCSE: the clock is forwarded).

        The row syncs are the virtual PLL's recovered row-opening EDGES: the decoder
        records each Column-0 UI at its mid-UI sample point, and the edge that opens the
        row, half a UI earlier, is the Row Sync Point (the S0→S1 transition). The clock is
        drawn rising on these edges and falling half a UI later, where DLV samples the bit.
        Where the wire has no 0→1 edge under a recovered RSP, `missing_rsp_samples` lists
        it."""
        rsp, _missing, ui = self.rsp_analysis()
        if ui <= 0.0:
            return None
        return rsp.astype(np.uint64), ui

    def recovered_row_syncs(self) -> np.ndarray:
        """The Row Sync Points as a sorted int64 array, one per row, so entry k is bus row
        k: the opening edge when present, else its predicted position. Empty for FBCSE.
        Cached."""
        if self.row_syncs_cache is not None:
            return self.row_syncs_cache
        on_edge = self.rsp_analysis()[0] if self.dlv else np.zeros(0, dtype=np.int64)
        self.row_syncs_cache = np.sort(np.asarray(on_edge, dtype=np.int64))
        return self.row_syncs_cache

    def missing_rsp_samples(self, lo_sample: int, hi_sample: int,
                            max_marks: int = 4000) -> np.ndarray:
        """Row Sync Points in [lo, hi] whose opening 0→1 (S0→S1) edge is missing on the
        wire, at the expected edge position, as uint64. Empty for FBCSE, and when more than
        `max_marks` fall in the window."""
        _on, missing, _ui = self.rsp_analysis()
        if missing.size == 0:
            return np.zeros(0, dtype=np.uint64)
        i0 = int(np.searchsorted(missing, max(0, int(lo_sample)), side="left"))
        i1 = int(np.searchsorted(missing, int(hi_sample), side="right"))
        sel = missing[i0:i1]
        if sel.size == 0 or sel.size > max_marks:
            return np.zeros(0, dtype=np.uint64)
        return np.ascontiguousarray(sel.astype(np.uint64))

    @property
    def missing_rsp_count(self) -> int:
        """Row Sync Points whose opening 0→1 edge is missing (0 for FBCSE)."""
        return int(self.rsp_analysis()[1].size)

    @property
    def pll_unlocked(self) -> bool:
        """True when a DLV decode has more than PLL_UNLOCK_MISSING_THRESHOLD missing
        Row-Sync edges."""
        return self.dlv and self.missing_rsp_count > PLL_UNLOCK_MISSING_THRESHOLD


def make_dlv_source(capture: Capture, audio_start: int, columns: int):
    """A fresh DlvSampleSource over the DP wire (`capture.clock_edges`) from `audio_start`,
    so the virtual PLL locks to the DLV Row-Sync edges rather than the single-ended
    cold-start link-control pulses. The DLL's free-running UI is seeded from `columns`
    (row period = sample rate / DLV_ROW_RATE_HZ; UI = row period / columns), so it locks
    to the once-per-row reference."""
    from .analysis import dlv_detect
    ce = np.asarray(capture.clock_edges, dtype=np.uint64)
    keep = ce[ce >= np.uint64(audio_start)]
    n_before = int(np.searchsorted(ce, np.uint64(audio_start), side="left"))
    init_dp = bool(capture.initial_clock) ^ (n_before % 2 == 1)
    nominal_ui = (float(capture.sample_rate_hz) / (dlv_detect.DLV_ROW_RATE_HZ * columns)
                  if columns > 0 else 0.0)
    return swi3score.DlvSampleSource(
        np.ascontiguousarray(keep, dtype=np.uint64), init_dp,
        int(capture.sample_rate_hz), columns, nominal_ui)


def configure_dlv(settings, columns: int) -> int:
    """Set `settings` for a DLV decode at `columns` columns: the recovered-clock path, and
    the CDS read at CDS_HorizontalStart's PHY3 reset value. Returns that CDS column."""
    from .analysis.dlv_detect import DLV_CDS_COLUMN
    settings.dlv = True
    settings.cds_horizontal_start = DLV_CDS_COLUMN
    settings.forced_column_count = columns
    return DLV_CDS_COLUMN


def detect_partial_dlv(capture: Capture, label: Callable[[], str],
                       end: Optional[int] = None) -> Optional[Tuple[Capture, int, bool]]:
    """(capture, columns, inverted) when `capture` is a mid-stream DLV differential pair,
    else None. With `end`, the pair is looked for in the samples before it only: the stretch
    before a later bring-up, whose link control is single-ended. `capture` is returned whole,
    with its two channels exchanged when the inverted orientation is the one that yields
    CRC-valid commands (a differential swap inverts every level). A pair sampled too slowly
    to decode as PHY3, and a swapped pair, are reported as import warnings under `label()`."""
    from . import import_notes
    from .analysis import dlv_detect
    probe = capture if end is None or end <= 0 else capture.subcapture(0, end)
    if not dlv_detect.is_complementary(probe):
        return None
    if dlv_detect.rate_too_low(capture.sample_rate_hz):
        import_notes.warn(label(),
                          f"the two channels form a differential pair, but at "
                          f"{int(capture.sample_rate_hz):,} Hz a DLV UI spans under "
                          f"{dlv_detect.MIN_UI_SAMPLES:g} sample; it is not decoded as PHY3")
        return None
    det = dlv_detect.detect_columns(probe)
    if det is None:
        return None
    cols, inverted = int(det[0]), bool(det[5])
    if inverted:
        import_notes.warn(label(),
                          "the differential pair's two channels are swapped (DN on the "
                          "clock channel); they are read the other way round")
        capture = Capture(capture.data_edges, capture.clock_edges,
                          capture.initial_data, capture.initial_clock, capture.sample_rate_hz)
    return capture, cols, inverted
