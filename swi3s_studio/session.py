"""A decoded analysis session: capture -> decode -> commands, audio, register
files, and bus-grid layout. Keeps the C++ Decoder alive so the grid can be
re-queried, and rebuilds register state as-of a time cursor.
"""
from __future__ import annotations

import bisect
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import swi3score

from .analysis.link_control import LinkControlResult, decode_link_control
from .ingest import saleae_binary, transitions
from .ingest.capture import Capture
from .model import DeviceRegisterFile, Provenance, RegisterMap
from .nputil import searchsorted as _searchsorted

# ABI the Python side expects from the swi3score native module (see bindings.cpp
# `score_abi`). Bump both together when a binding's return shape changes.
_REQUIRED_SCORE_ABI = 8


def zero_based_row(bus_row, origin) -> int:
    """0-based display row: the internal bus row minus the capture's row origin (the
    leading partial row of a mid-row-start capture bumps the decoder's counter), floored
    at 0. One source of truth for the app-wide 0-based row display."""
    return max(0, int(bus_row) - int(origin))


def _require_score_abi() -> None:
    """Fail with a clear, actionable message if the loaded swi3score native module is
    older than this Python code expects. The .so is built locally (not tracked in git),
    so pulling new code without rebuilding leaves it stale — and a stale binding whose
    return shape changed otherwise surfaces as a cryptic 'not enough values to unpack'
    (e.g. registers_from_commands going from 6- to 8-tuples)."""
    abi = int(getattr(swi3score, "score_abi", 0))
    if abi < _REQUIRED_SCORE_ABI:
        raise RuntimeError(
            f"The swi3score native decode core is out of date (ABI {abi}; this app "
            f"needs {_REQUIRED_SCORE_ABI}). Rebuild it after pulling:\n"
            f"    python3 -m pip install ./native\n"
            f"(or, offline:  bash native/build_local.sh)")


class Session:
    def __init__(self, capture: Capture, *, forced_column_count: int = 0,
                 decode_audio: bool = True, config_csv: str = "",
                 scrambler_overrides=None, hub_depths=None, device_names=None,
                 register_map: Optional[RegisterMap] = None,
                 source: Optional[dict] = None, ssp_row: int = -1,
                 dlv: bool = False, decoder_ready=None,
                 forced_column_sections=None, label_fields=None):
        _require_score_abi()                          # clear error if the .so is stale
        self.capture = capture
        self.source = source or {"type": "capture"}   # workspace descriptor
        # Force the DLV (PHY3) recovered-clock path even without a cold-start bring-up
        # to name the PHY — set for a PARTIAL DLV capture (mid-stream differential pair),
        # detected up front (see analysis.dlv_detect / Session.from_digital_csv). The
        # column count arrives via forced_column_count. is_dlv / _configure_source_for_phy
        # honour it, so RSP recovery, the CDS symbol table, and the recovered clock all
        # work as they do for a cold-start DLV capture.
        self._dlv_forced = bool(dlv)
        # Per-(device, dp) scrambler override: {(device, dp): bool}.
        self.scrambler_overrides = dict(scrambler_overrides or {})
        # Per-device hub depth {device: 0..5}: a peripheral N hubs deep has its
        # response delayed 2*N frame rows; fed to the decoder so responses align.
        self.hub_depths = {int(d): int(v) for d, v in dict(hub_depths or {}).items()}
        # Per-device display names {device: str} (UI metadata; no decode effect).
        self.device_names = {int(d): str(v) for d, v in dict(device_names or {}).items()}
        # Per-(device, dp) Bus Grid label fields: 1=Sample, 2=Channel, 4=Bit, OR-ed —
        # the same DisplayField flags the Visualizer uses. Absent ports fall back to
        # _DEFAULT_LABEL_FIELDS (Channel|Bit). PURE DISPLAY: consumed by the grid
        # renderer, never by the decode, so changing it re-renders and does NOT
        # re-decode. Persisted in the source descriptor (workspace only — a new
        # capture always starts at the default).
        self.label_fields: Dict[Tuple[int, int], int] = self._load_label_fields(
            label_fields if label_fields is not None
            else self.source.get("label_fields"))
        self._store_label_fields()               # normalise into the descriptor
        self._register_map_arg = register_map
        self._source = capture.sample_source()       # keep alive for the decoder
        settings = swi3score.DecoderSettings()
        settings.forced_column_count = int(forced_column_count)
        self._forced_column_count = int(forced_column_count)
        settings.decode_audio = bool(decode_audio)
        self._decode_audio = bool(decode_audio)
        # Per-data-bit sample points (Raw Capture "Port Samples" overlay) are decoded
        # ON DEMAND per visible window (session.port_bit_marks → decoder.bit_samples_window),
        # reconstructed from transport checkpoints the initial decode records — so there
        # is no lazy full-capture collection / re-decode. Nothing to set here.
        # Accept the old human-readable visualizer CSV as well as v2.0 (the C++
        # parser only reads v2.0, so convert the old format first). Persist the
        # ORIGINAL path in the source descriptor so a workspace reopen re-applies
        # it (the normalized path may be a temp file that won't exist next launch).
        if config_csv:
            from .ingest import visualizer_csv
            self.source["config_csv"] = config_csv          # original, for persistence
            norm = visualizer_csv.normalized_path(config_csv)
            # normalized_path returns a fresh temp file for an upgraded (legacy/v2.0)
            # CSV; own it so apply_config_csv can delete it when it's replaced.
            self._config_csv_tmp = norm if norm != config_csv else None
            config_csv = norm
        else:
            self._config_csv_tmp = None
        self._config_csv = config_csv or ""
        settings.config_csv_path = self._config_csv
        # Manual Stream Sync Point (bus row) — for a post-commit capture whose SSPA/SSCR
        # was never on the wire, so interval>1 ports need the phase picked by ear. -1 =
        # off. Persisted in the source so it survives a workspace reopen.
        self._store_ssp_row(ssp_row)
        self._ssp_from_sspa = False          # set by lock_ssp_from_sspa when derived
        settings.ssp_row = self._ssp_row
        settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                        for (d, p), on in self.scrambler_overrides.items()]
        settings.hub_depth_overrides = [(d, v) for d, v in self.hub_depths.items() if v]
        # Region-scoped forced column count {section_index: column_count}: pins ONE
        # config region's bus geometry over the snooped NumColumns / blind detector
        # (see force_column_count_at). Taken from the argument, else the source
        # descriptor. Resolved BEFORE the first decode — it changes the decode, so a
        # reopened workspace has to carry it into run() rather than re-decode after.
        pins = (forced_column_sections
                if forced_column_sections is not None
                else self.source.get("forced_column_sections") or {})
        self.forced_column_sections: Dict[int, int] = {
            int(k): int(v) for k, v in dict(pins).items()}
        self._store_forced_columns()             # normalise into the descriptor
        settings.forced_column_sections = sorted(self.forced_column_sections.items())
        # PHY3 (DLV) uses a recovered-clock source instead of the forwarded-clock one:
        # decode the §5.1.2 bring-up to learn the selected PHY. When it's PHY3, build a
        # DlvSampleSource over the DP wire STARTING at audio_start (so the virtual PLL
        # locks to the DLV Row-Sync edges, not the single-ended cold-start LC pulses),
        # and tell the decoder to skip NRZS and read the CDS at Column 2. FBCSE (PHY1/2)
        # and mid-stream captures keep the forwarded-clock source.
        self._link_control = decode_link_control(capture)
        # A mid-stream DLV (PHY3) capture has no cold-start to name the PHY. Detect it
        # from the differential-pair signature (any source: .sal / .bin / CSV / reopen),
        # so re-importing an exported partial-DLV .sal decodes like the original. Skips
        # when the PHY is already known (cold start) or DLV was forced by the caller.
        self._autodetect_partial_dlv(settings)
        self._source = self._configure_source_for_phy(settings)
        self.decoder = swi3score.Decoder(self._source, settings)
        # Let the caller (e.g. the UI's decode worker thread) grab the decoder BEFORE
        # the blocking run() call, so it can poll decoder.progress / progress_uis /
        # total_uis from another thread while run() executes on this one. Optional and
        # backward-compatible: absent for every existing caller.
        if decoder_ready is not None:
            decoder_ready(self.decoder)
        self.decoder.run()

        self.commands: List[dict] = self.decoder.commands()
        # Audio is kept columnar (struct-of-arrays) — a dense mic-array capture
        # decodes tens of millions of samples, and materialising that as a Python
        # list of per-sample dicts is what froze the UI. `audio` (the dict list) is
        # still available as a lazy property for any caller that wants it.
        self._refresh_audio()
        self.column_count: int = self.decoder.column_count
        self.row_rate_khz: float = self.decoder.row_rate_khz
        self.ui_rate_hz: float = self.decoder.measured_ui_rate_hz
        self.segments: List[dict] = self.decoder.segments()

        self.register_map = register_map or RegisterMap.load()
        # Per-device peripheral (vendor) register maps, keyed by device number.
        # Imported via the register-map assistant; addresses in device-defined
        # space resolve against the matching device's map.
        self.peripheral_maps: Dict[int, object] = {}
        # Debug what-if register overrides, keyed by (section_index, device, address)
        # -> byte. Section-scoped (a capture can change config mid-stream); folded
        # into the C++ decode for the matching section so the SAME override drives
        # the audio decode, bus grid, and register map (Provenance.UI). Changing an
        # override re-decodes. Empty by default; not persisted (a debug tool).
        self.register_overrides: Dict[Tuple[int, int, int], int] = {}
        # CSV-import baseline: the register snapshot + grid replay an applied config
        # CSV encodes (Provenance.CSV). Empty unless a config CSV is applied — see
        # apply_config_csv / _reload_csv_seed.
        self._reload_csv_seed()
        # Whole-capture register state, sourced from the C++ decode authority (same
        # as register_files_at, whole-capture / no overrides).
        self.register_files: Dict[int, DeviceRegisterFile] = self._build_register_files(
            self._config_commands(), None)
        # _link_control was decoded up front (source selection); keep it (property caches).
        # For DLV, capture the virtual PLL's recovered Row Sync Points NOW, from the real
        # DlvSampleSource — a later in-place re-decode (scrambler / register what-if)
        # rebuilds `self._source` from the forwarded-clock path, which has no recovered
        # clock, so caching here keeps the periodic RSP marks available afterwards.
        self._rec_syncs_cache = None
        self._rsp_analysis_cache = None
        self._commit_rsp_cache = None      # sorted confirmed-commit RSP samples (viewport-independent)
        if self.is_dlv:
            self._recovered_row_syncs()
        # A partial capture (config imposed by CSV, no setup commit on the wire) has no
        # SSP — but an SSPA in the capture announces one. Adopt it and apply it backwards
        # so the rows BEFORE the first SSPA decode at the right phase too. No-op unless
        # all of those conditions hold; costs one extra decode when it fires.
        self.lock_ssp_from_sspa()

    # ---- audio (columnar) ----
    _AUDIO_FIELDS = ("device", "dp", "channel", "sample_size", "value",
                     "index", "start_sample", "end_sample", "flow_mode", "drq_sample")
    _AUDIO_DTYPES = {"device": np.int32, "dp": np.int32, "channel": np.int32,
                     "sample_size": np.int32, "value": np.uint32,
                     "index": np.uint64, "start_sample": np.uint64,
                     "end_sample": np.uint64, "flow_mode": np.int32,
                     "drq_sample": np.uint64}

    def _build_audio_columns(self) -> Dict[str, np.ndarray]:
        """Audio as a dict of parallel NumPy arrays. Uses the core's fast
        `audio_columns()` when present; otherwise derives the columns from the
        per-sample `audio()` list (older core — correct, just not as fast)."""
        cols_fn = getattr(self.decoder, "audio_columns", None)
        if callable(cols_fn):
            return cols_fn()
        audio = self.decoder.audio()
        n = len(audio)
        # .get with a default for every field so a dict list missing the ABI-7
        # flow_mode/drq_sample keys degrades to NORMAL (0) instead of KeyError.
        defaults = {"device": -1, "flow_mode": 0, "drq_sample": 0}
        return {f: np.fromiter((a.get(f, defaults.get(f, 0)) for a in audio),
                               self._AUDIO_DTYPES[f], n)
                for f in self._AUDIO_FIELDS}

    def _refresh_audio(self) -> None:
        self._audio_cols = self._build_audio_columns()
        self._audio_list: Optional[List[dict]] = None      # lazy dict-list cache
        self._mark_cache = None                            # lazy per-value read-mark index
        self._filtered_cache = None                         # invalidate _filtered_positions memo
        es = self._audio_cols.get("end_sample")
        self.audio_count: int = int(self._audio_cols["dp"].shape[0])
        self.audio_end_sample: int = int(es.max()) if es is not None and es.size else 0

    def audio_columns(self) -> Dict[str, np.ndarray]:
        """The decoded audio as columnar NumPy arrays (the fast path the audio
        store consumes). Keys: device, dp, channel, sample_size, value, index,
        start_sample, end_sample."""
        return self._audio_cols

    @property
    def audio(self) -> List[dict]:
        """Audio as a list of per-sample dicts (built lazily from the columns).
        Prefer `audio_columns()` / `audio_count` / `audio_end_sample` in hot paths —
        materialising this list is expensive for large captures."""
        if self._audio_list is None:
            c = self._audio_cols
            cs = {f: c[f].tolist() for f in self._AUDIO_FIELDS}
            self._audio_list = [
                {f: cs[f][i] for f in self._AUDIO_FIELDS}
                for i in range(self.audio_count)]
        return self._audio_list

    @property
    def link_control(self) -> LinkControlResult:
        """The decoded §5.1.2 Cold/Warm Start link bring-up (PHY selection etc.),
        recovered from the raw DP/DN edges — lazy + cached (the capture's edges
        don't change across re-decodes, so this survives scrambler re-runs)."""
        if self._link_control is None:
            self._link_control = decode_link_control(self.capture)
        return self._link_control

    @property
    def audio_start_sample(self) -> int:
        """Sample at which audio mode begins (after PhyStart). 0 when the capture
        has no observable bring-up, so callers can gate without special-casing."""
        return self.link_control.audio_start_sample or 0

    @property
    def is_dlv(self) -> bool:
        """True when this is a DLV (PHY3) capture — differential, recovered-clock, which
        the Raw Capture view renders as one signal + a recovered bit clock. Either a
        cold start selected PHY3, or a partial mid-stream capture was detected as a DLV
        differential pair (`dlv=True`, see analysis.dlv_detect)."""
        return self._dlv_forced or self.link_control.phy_name == "PHY3"

    def recovered_clock(self):
        """For a DLV capture, the recovered clock: (row_sync_samples ndarray,
        ui_samples float). None for FBCSE (the clock is forwarded, not recovered).

        The row syncs are the virtual PLL's recovered row-opening EDGES, used directly:
        the decoder records each Column-0 UI at its mid-UI sample point (`row_sync_
        samples()`, authoritative across resyncs), and the PLL edge that opens the row
        is exactly half a UI earlier — that edge IS the Row Sync Point (the Sync0→Sync1 /
        S0→S1 transition). The recovered clock is drawn rising on these edges and falling
        half a UI later, which is where DLV samples the bit. Where the wire has no actual
        0→1 edge under a recovered RSP, `missing_rsp_samples` flags it (a PLL lock gap)."""
        rsp, _missing, ui = self._rsp_analysis()
        if ui <= 0.0:
            return None
        return rsp.astype(np.uint64), ui

    def _rsp_analysis(self):
        """(rsp, missing, ui) for a DLV capture, cached. `rsp` = the virtual PLL's
        recovered row-opening edges (the DP-wire 0→1 edge that opens each row) — used
        directly as the Row Sync Points. `missing` = the subset of rows whose opening edge
        is absent on the wire (no DP-wire 0→1 edge just before the recovered Column-0 mid-UI
        sample — a recovered-clock lock gap). `ui` = recovered (operational) UI period.

        The decoder records each Column-0 UI at its MID-UI sample; the opening edge sits
        half a UI earlier. That half-UI differs per geometry (Safe-Lock UI is wider than the
        operational UI — the bit clock speeds up at the commit), so we don't subtract a
        single half: instead each mid-UI sample is snapped back to the nearest DP 0→1 edge
        at or just before it (within half a ROW period, which is geometry-independent since
        the row rate is constant). Empty / 0.0 for FBCSE."""
        cached = getattr(self, "_rsp_analysis_cache", None)
        if cached is not None:
            return cached
        src = self._source
        ui_fn = getattr(src, "recovered_ui_samples", None)
        rows_fn = getattr(self.decoder, "row_sync_samples", None)
        empty = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), 0.0)
        if ui_fn is None or rows_fn is None:
            self._rsp_analysis_cache = empty
            return empty
        ui = float(ui_fn())
        mid = np.asarray(rows_fn(), dtype=np.int64)          # Column-0 mid-UI samples
        half = int(round(ui * 0.5)) if ui > 0 else 0
        ce = np.asarray(self.capture.clock_edges, dtype=np.int64)
        first_rise = 1 if bool(self.capture.initial_clock) else 0
        rising = ce[first_rise::2]                            # DP-wire 0→1 edges
        if ui <= 0.0 or mid.size == 0 or rising.size == 0:
            rsp = np.maximum(mid - half, 0)
            self._rsp_analysis_cache = (rsp, np.zeros(0, dtype=np.int64), ui)
            return self._rsp_analysis_cache
        # Row period is constant (the recovered-clock reference rate holds across the
        # commit), so the opening edge of each row is the DP 0→1 edge at most half a UI
        # before its Column-0 mid-UI sample. Snap to the largest rising edge <= mid; accept
        # it as the RSP when it's within half a row period (covers any Safe-Lock UI width
        # without matching a neighbouring row's edge). Beyond that = a missing opening edge.
        row_period = int(np.median(np.diff(mid))) if mid.size > 1 else max(1, 2 * half)
        tol = max(1, row_period // 2)
        j = np.clip(np.searchsorted(rising, mid, side="right") - 1, 0, rising.size - 1)
        edge = rising[j]
        gap = mid - edge
        present = (gap >= 0) & (gap <= tol)
        rsp = np.where(present, edge, np.maximum(mid - half, 0))   # RSP = real edge, else predicted
        missing = rsp[~present]
        self._rsp_analysis_cache = (rsp, missing, ui)
        return self._rsp_analysis_cache

    def missing_rsp_samples(self, lo_sample: int, hi_sample: int,
                            max_marks: int = 4000) -> np.ndarray:
        """DLV Row Sync Points in [lo, hi] whose opening 0→1 (S0→S1) edge is MISSING on
        the wire — a recovered-clock lock gap. Returned at the expected edge position so
        the Raw view can flag each. Empty for FBCSE / when too dense to draw."""
        _on, missing, _ui = self._rsp_analysis()
        if missing.size == 0:
            return np.zeros(0, dtype=np.uint64)
        i0 = int(np.searchsorted(missing, max(0, int(lo_sample)), side="left"))
        i1 = int(np.searchsorted(missing, int(hi_sample), side="right"))
        sel = missing[i0:i1]
        if sel.size == 0 or sel.size > max_marks:
            return np.zeros(0, dtype=np.uint64)
        return np.ascontiguousarray(sel.astype(np.uint64))

    #: A DLV recovered clock with more than this many missing Row-Sync edges is reported
    #: as unlocked (a handful of gaps is tolerable jitter; a sustained run means the
    #: virtual PLL lost the row cadence).
    PLL_UNLOCK_MISSING_THRESHOLD = 5

    @property
    def pll_missing_rsp_count(self) -> int:
        """Total DLV Row Sync Points whose opening 0→1 edge is missing (0 for FBCSE)."""
        return int(self._rsp_analysis()[1].size)

    @property
    def pll_unlocked(self) -> bool:
        """True when a DLV capture has more than PLL_UNLOCK_MISSING_THRESHOLD missing
        Row-Sync edges — the recovered clock did not stay locked to the row cadence."""
        return self.is_dlv and self.pll_missing_rsp_count > self.PLL_UNLOCK_MISSING_THRESHOLD

    @property
    def has_bringup(self) -> bool:
        """True when the capture contains an observable Cold/Warm Start sequence."""
        return self.link_control.sequence != "none"

    @property
    def row_origin(self) -> int:
        """Internal bus row of the capture's FIRST complete row (segment 0's anchor).
        A capture that starts mid-row anchors at row_base > 0 (the leading partial row
        bumps the decoder's counter); subtract this to display 0-based row numbers so
        the first row reads as 0 across the UI (bus grid, command table)."""
        return int(self.segments[0]["row_base"]) if self.segments else 0

    def display_row(self, bus_row) -> int:
        """0-based display row for an internal bus row (see zero_based_row)."""
        return zero_based_row(bus_row, self.row_origin)

    def audio_streams(self):
        """Distinct (device, dp) pairs present in the decoded audio."""
        # The decoder already tracks a sample rate per (device, dp), i.e. it knows
        # the distinct audio streams. Use that — it's a handful of keys — instead of
        # a unique() over the tens of millions of audio rows, which is what made
        # opening the scrambler dialog beach-ball for ~10s on dense captures.
        rates_fn = getattr(self.decoder, "audio_sample_rates", None)
        if callable(rates_fn):
            keys = rates_fn()
            if keys:
                return sorted((int(d), int(p)) for d, p in keys)
        # Fallback (older core without audio_sample_rates): collapse the (device, dp)
        # columns to a single int64 key so unique() stays a fast 1-D C sort.
        dev, dp = self._audio_cols["device"], self._audio_cols["dp"]
        if dev.size == 0:
            return []
        key = (dev.astype(np.int64) << 32) | (dp.astype(np.int64) & 0xFFFFFFFF)
        return [(int(k >> 32), int(k & 0xFFFFFFFF)) for k in np.unique(key)]

    def set_scrambler_overrides(self, overrides) -> None:
        """Re-decode in place with a new per-(device, dp) scrambler override map
        ({(device, dp): bool}); absent entries keep the snooped/CSV value."""
        new = dict(overrides or {})
        if new == self.scrambler_overrides:
            return                               # unchanged -> skip the full re-decode
        self.scrambler_overrides = new
        self._redecode()

    def set_hub_depths(self, depths) -> None:
        """Re-decode in place with new per-device hub depths ({device: 0..5}). A
        deeper device's response is read 2*depth frame rows later."""
        new = {int(d): int(v) for d, v in dict(depths or {}).items()}
        if new == self.hub_depths:
            return                               # unchanged -> skip the full re-decode
        self.hub_depths = new
        self._redecode()

    def _autodetect_partial_dlv(self, settings) -> None:
        """Mark this a DLV capture when it's a mid-stream differential pair with no
        cold-start PHY-select, so every source (.sal / .bin / CSV / workspace reopen)
        routes through the recovered-clock path — not just the CSV importer. A differential
        swap inverts every logical level, so the pair is flipped when the inverted
        orientation is the one that yields CRC-valid commands. No-op when DLV was forced
        by the caller, the cold start already named PHY3, or the pair isn't complementary."""
        if self._dlv_forced or self.link_control.phy_name == "PHY3":
            return
        from .analysis import dlv_detect
        if not dlv_detect.is_complementary(self.capture):
            return
        det = dlv_detect.detect_columns(self.capture)
        if det is None:
            return
        cols, inverted = int(det[0]), bool(det[5])
        if inverted:                                   # DP/DN swapped: correct the pair
            cap = self.capture
            self.capture = Capture(cap.data_edges, cap.clock_edges,
                                   cap.initial_data, cap.initial_clock, cap.sample_rate_hz)
        self._dlv_forced = True
        self._forced_column_count = cols
        settings.forced_column_count = cols
        # Record for provenance / a workspace reopen (harmless if the source is re-detected).
        self.source["dlv"] = True
        self.source["dlv_columns"] = cols
        self.source["dlv_inverted"] = inverted

    def _make_dlv_source(self):
        """A fresh DlvSampleSource over the DP wire, starting at audio_start so the virtual
        PLL locks to the DLV Row-Sync edges (not the single-ended cold-start LC pulses). The
        DLL's free-running UI is seeded from the Safe-Lock geometry (row period = sample_rate
        / 3.072 MRows/s; UI = row period / cols) so it locks to the once-per-row reference
        instead of guessing from the first two rising edges. Shared by the main decode and
        the CDS symbol re-decode so their framing / UI indexing match.

        A cold start seeds `cols` from the recovered Safe-Lock count; a PARTIAL capture
        (no bring-up) has none, so it uses the blind-detected `forced_column_count` and
        starts at sample 0 (audio_start is 0)."""
        cap = self.capture
        ce = np.asarray(cap.clock_edges, dtype=np.uint64)          # DP wire
        audio_start = int(self.link_control.audio_start_sample or 0)
        keep = ce[ce >= np.uint64(audio_start)]
        n_before = int(np.searchsorted(ce, np.uint64(audio_start), side="left"))
        init_dp = bool(cap.initial_clock) ^ (n_before % 2 == 1)
        cols = int(self._forced_column_count or self.link_control.safe_lock_columns or 4)
        nominal_ui = float(cap.sample_rate_hz) / (3_072_000.0 * cols) if cols > 0 else 0.0
        return swi3score.DlvSampleSource(
            np.ascontiguousarray(keep, dtype=np.uint64), init_dp,
            int(cap.sample_rate_hz), cols, nominal_ui)

    def _symbol_source(self):
        """(source, dlv, cds_column) for the CDS symbol re-decode. DLV needs the recovered-
        clock source + Column-2 plain-NRZ CDS. Crucially it reuses the ALREADY-RUN main
        decode source (self._source), not a fresh one: only after the DLL has run does it
        hold the per-row (edge, UI) log its geometry-aware Seek needs — a fresh source would
        fall back to uniform seek math that ignores the Column-0 phase offset and mis-frames
        (empty/garbage symbols). This mirrors how bit_samples_window seeks the same source;
        both re-Seek per call, so sharing is safe (the streaming decode has finished). FBCSE
        seeks a fresh, stateless source. DLV callers must pass start_ui > 0 (the seek path);
        the segment Column-0 UI is always > 0, so symbols()/around()/before() satisfy this."""
        if self.is_dlv:
            return self._source, True, int(self._cds_horizontal_start)
        return self.capture.sample_source(), False, 0

    def _configure_source_for_phy(self, settings) -> object:
        """Build the decoder's sample source for this capture's PHY and set the matching
        DLV fields on `settings`; returns the source.

        PHY3 (DLV) has no forwarded clock: a DlvSampleSource recovers the bit clock over
        the DP wire STARTING at audio_start (so the virtual PLL locks to the DLV Row-Sync
        edges, not the single-ended cold-start LC pulses), and the decoder skips NRZS and
        reads the CDS at Column 2 (Safe-Lock-4, §12.1.10.1). FBCSE (PHY1/2) keeps the
        forwarded-clock source. Shared by the initial decode AND every in-place re-decode
        (_redecode), so a DLV session never silently reverts to FBCSE framing when a
        scrambler / hub-depth / register what-if / config-CSV change re-runs the decode."""
        self._cds_horizontal_start = 0             # DLV CDS column (0 = FBCSE, CDS at Col 0)
        cap = self.capture
        if self.is_dlv:
            cols = int(self._forced_column_count or self.link_control.safe_lock_columns or 4)
            settings.dlv = True
            settings.cds_horizontal_start = 2      # Safe-Lock-4 CDS position (§12.1.10.1)
            self._cds_horizontal_start = 2
            settings.forced_column_count = cols
            self._forced_column_count = cols
            return self._make_dlv_source()
        # FBCSE (PHY1/PHY2): when the cold-start bring-up recovered the Safe-Lock column
        # count, seed the decoder with it (unless a config CSV already pins the geometry).
        # The snoop's NumColumns commits drive the count from there. Without the seed the
        # blind column detector can mis-lock on the tightly-packed 4-column PHY1 layout
        # (PCM interleaved right after the CDS), reading every other interval.
        if self._config_csv:
            # A config CSV pins the geometry, so the Safe-Lock seed must be CLEARED, not
            # merely skipped. It is sticky (stored on self by an earlier decode), and
            # forcedColumnCount outranks the CSV in Decoder::run — so importing a CSV
            # onto an already-open cold-start capture left the stale Safe-Lock width in
            # force and decoded a 16-column config at 2 columns: the grid collapsed to
            # the furthest placed cell (7 where the Visualizer shows 16) and the commands
            # went CRC-red. Only the CSV-less path may seed it.
            self._forced_column_count = 0
            settings.forced_column_count = 0
        elif self.link_control.safe_lock_columns:
            settings.forced_column_count = int(self.link_control.safe_lock_columns)
            self._forced_column_count = int(self.link_control.safe_lock_columns)
        return cap.sample_source()

    def _redecode(self) -> None:
        """Rebuild the decoder from the current settings + overrides and re-run."""
        settings = swi3score.DecoderSettings()
        settings.forced_column_count = self._forced_column_count
        settings.decode_audio = self._decode_audio
        settings.config_csv_path = self._config_csv
        settings.ssp_row = self._ssp_row
        settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                        for (d, p), on in self.scrambler_overrides.items()]
        settings.hub_depth_overrides = [(d, v) for d, v in self.hub_depths.items() if v]
        settings.register_overrides = [(int(sect), int(d), int(a), int(v))
                                       for (sect, d, a), v in self.register_overrides.items()]
        settings.forced_column_sections = sorted(self.forced_column_sections.items())
        # Rebuild the PHY-correct source (DLV needs the recovered-clock source + settings;
        # without this a DLV session silently reverts to FBCSE framing on re-decode).
        self._source = self._configure_source_for_phy(settings)
        self.decoder = swi3score.Decoder(self._source, settings)
        self.decoder.run()
        self.commands = self.decoder.commands()
        self._rel_cmds = None                    # invalidate config-command cache
        self._eff_sorted = None                  # invalidate effective-sample sort cache
        self._reg_replay_cache = None            # invalidate register-replay sort cache
        self._tx_persist_cache = {}               # invalidate tx_persist_columns memo (segments may move)
        # Recovered-clock / RSP caches: a column-changing re-decode (config CSV or a
        # NumColumns register what-if on DLV) re-frames the rows, so the cached Row Sync
        # Points / marks would be stale. Drop them; they recompute lazily from the fresh
        # (DLV-aware) decoder. A scrambler/hub re-decode leaves them identical anyway.
        self._rsp_analysis_cache = None
        self._rec_syncs_cache = None
        self._commit_rsp_cache = None            # invalidate confirmed-commit RSP sample cache
        self._refresh_audio()
        self.column_count = self.decoder.column_count
        self.row_rate_khz = self.decoder.row_rate_khz
        self.ui_rate_hz = self.decoder.measured_ui_rate_hz
        self.segments = self.decoder.segments()
        self._reload_csv_seed()                  # config CSV may have changed (apply_config_csv)
        self.register_files = self._build_register_files(self._config_commands(), None)

    def apply_config_csv(self, path: Optional[str]) -> None:
        """Impose a Visualizer config CSV on the ALREADY-OPEN capture (no reopen): a
        capture that begins after the setup commit has no WriteA32/commit on the wire,
        so its port geometry can't be snooped. Applying the CSV re-decodes with it (so
        audio reconstructs and the CDS column count/phase are known from row 0) AND
        seeds the bus grid + register map from it (Provenance.CSV — "CSV Import"), so
        both match the CSV. `path` None/"" removes it. The original path is kept in the
        source descriptor so it survives a workspace reopen (session_from_source)."""
        if path:
            from .ingest import visualizer_csv
            self.source["config_csv"] = path                 # original, for persistence
            norm = visualizer_csv.normalized_path(path)
            self._discard_config_tmp()                        # drop the previous upgrade temp
            self._config_csv_tmp = norm if norm != path else None
            self._config_csv = norm
        else:
            self.source.pop("config_csv", None)
            self._discard_config_tmp()
            self._config_csv = ""
        # Explicit (re)apply: force the CSV seed to rebuild even if the path string is
        # unchanged. An already-current CSV normalizes to its own path, so editing the
        # file on disk and re-applying the SAME path would otherwise hit the path-keyed
        # memo in _reload_csv_seed and keep the stale grid/register baseline while the
        # C++ re-decode (which re-reads the file) moves on — a silent seed↔audio mismatch.
        self._csv_seed_key = None
        self._redecode()
        # The CSV is what gives a partial capture its port geometry, so only now can an
        # SSPA be used to anchor the phase (and applied backwards). See lock_ssp_from_sspa.
        self.lock_ssp_from_sspa()

    def _discard_config_tmp(self) -> None:
        """Remove the temp CSV normalized_path created for an upgraded config (if we
        own one), so repeated apply_config_csv calls don't leak temp files."""
        tmp = getattr(self, "_config_csv_tmp", None)
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._config_csv_tmp = None

    def sspa_ssp_row(self) -> Optional[int]:
        """Bus row of the first decoded SSPA's Stream Sync Point, or None if there is no
        SSPA to read one from.

        An SSPA announces an SSP without committing anything, and its SSP lands
        Row_Delay rows after the phase ends — the same arithmetic the decoder uses
        (Decoder's sspaReanchor path), derived here from the command's END row because
        `bus_row` is the phase START."""
        for c in self.commands:
            if (c.get("has_sync_point") and not c.get("is_commit")
                    and c.get("crc_valid") and c.get("end_sample") is not None):
                last = self.bus_row_for_sample(int(c["end_sample"]))
                return last + 1 + int(c.get("row_delay", 0))
        return None

    def _wire_supplies_an_ssp(self) -> bool:
        """Does the capture itself establish the data ports' SSP? True when a confirmed
        sync-point COMMIT is on the wire — that both configures the ports and anchors
        their phase, so nothing needs deriving."""
        return any(c.get("is_commit") and c.get("commit_confirmed")
                   and c.get("has_sync_point") and c.get("crc_valid")
                   for c in self.commands)

    def lock_ssp_from_sspa(self) -> bool:
        """A partial capture has no setup commit, so the data ports' SSP was never on the
        wire and interval > 1 ports decode at an arbitrary phase (1/N rows correct). If an
        SSPA IS present it announces a real SSP, so use it as the anchor — and apply it
        BACKWARDS as well as forwards.

        Backwards matters because the decoder's own SSPA handling only re-anchors from the
        row it fires on: everything BEFORE the first SSPA still decodes at the arbitrary
        phase. Measured on a sliced demo: the region before the SSPA read -14 dB tone SNR
        while the region after read +38 dB. Feeding the row through the manual-SSP path
        reduces it modulo the LCM of the ports' interval periods, so the phase holds from
        row 0 (see Decoder's mSspSyncRow) and the whole capture decodes cleanly.

        No-op (returns False) when an explicit SSP row is already set, when the wire
        supplies its own SSP, when there is no SSPA, or when there is no audio to re-phase.
        Returns True if it changed the anchor and re-decoded."""
        if self._ssp_row >= 0 and not self._ssp_from_sspa:
            return False                      # an explicit / persisted choice wins
        if self._wire_supplies_an_ssp():
            return False                      # a commit already anchored the ports
        if not self._config_csv:
            return False                      # no imposed geometry -> nothing to re-phase
        row = self.sspa_ssp_row()
        if row is None or row == self._ssp_row:
            return False
        self._ssp_row = int(row)               # NOT _store_ssp_row: this is DERIVED, so it
        self._ssp_from_sspa = True             # is re-derived on reopen rather than pinned
        self._redecode()
        self._ssp_from_sspa = True             # _redecode -> no _store_ssp_row, but be explicit
        return True

    @property
    def ssp_locked_from_sspa(self) -> bool:
        """True when the active SSP anchor was derived from an SSPA rather than set by
        hand — for the UI to say so instead of implying the user chose it."""
        return bool(getattr(self, "_ssp_from_sspa", False))

    def set_ssp_row(self, row: int) -> None:
        """Force the Stream Sync Point to bus `row` (row_in_interval == 0 anchors there
        for every data port) and re-decode. -1 = auto (anchor at the decode start). For
        a post-commit capture the SSPA/SSCR was never on the wire, so interval>1 ports
        decode at an arbitrary phase (only 1/N rows correct); set the row and re-decode
        until the audio is clean (the phase repeats every interval, so stepping the row
        by 1 cycles all phases). Persisted in the source; survives a workspace reopen."""
        if int(row) == self._ssp_row:
            return                               # unchanged -> skip the full re-decode
        # The manual SSP only re-phases anything when a config CSV supplied the port
        # geometry (see ssp_effective). Without one it can't take effect, so store the
        # row (for persistence) but skip the ~full-decode that would produce identical
        # output — this is the "step the row to find the phase by ear" workflow, which
        # otherwise re-decodes per keystroke for nothing.
        if not self._config_csv:
            self._store_ssp_row(row)
            return
        self._store_ssp_row(row)
        self._redecode()

    def _store_ssp_row(self, row: int) -> None:
        """Set the manual SSP row and mirror it into the source descriptor (present when
        >= 0, removed when auto). Shared by __init__ and set_ssp_row so the two can't
        drift."""
        self._ssp_row = int(row)
        self._ssp_from_sspa = False          # an explicit choice, not one we derived
        if self._ssp_row >= 0:
            self.source["ssp_row"] = self._ssp_row
        else:
            self.source.pop("ssp_row", None)

    @property
    def ssp_row(self) -> int:
        """The manual SSP bus row in effect (-1 = auto)."""
        return self._ssp_row

    @property
    def ssp_effective(self) -> bool:
        """True when a manual SSP would actually re-phase the audio. The decoder only
        arms the manual SSP when the payload engine is enabled at decode start — i.e. a
        config CSV supplied the port geometry AND audio decoded. On a snoop/no-audio
        capture the manual SSP is a no-op, so callers can message honestly."""
        return self._ssp_row >= 0 and bool(self._config_csv) and self.audio_count > 0

    @staticmethod
    def _csv_writes_replay(seed) -> List[dict]:
        """Turn a config CSV's (device, address, value) triples into a WriteA32 +
        confirmed-commit replay that reproduces the CSV config through the SAME C++
        authority the grid/register map use (registers_from_commands /
        grid_from_commands) — verified identical to grid_from_csv / registers_from_csv.
        The trailing commit promotes the staged _NEXT writes so the grid draws them
        (writes alone stage but don't take effect)."""
        replay: List[dict] = [
            {"is_write": True, "device_mask": 1 << int(d), "address": int(a),
             "data": bytes([int(v) & 0xFF])} for d, a, v in seed]
        if replay:
            replay.append({"is_commit": True, "commit_confirmed": True, "group_mask": 0xFFFF})
        return replay

    def _reload_csv_seed(self) -> None:
        """(Re)build the CSV-import baseline from the applied config CSV: the write+
        commit replay prepended to the bus-grid replay, and the register snapshot
        seeded as Provenance.CSV. Empty (a no-op baseline) when no config CSV is
        applied. Called from __init__ and every _redecode; the seed only depends on
        the config-CSV path, so it's memoized on that path — a scrambler/hub/SSP/
        override re-decode (which doesn't touch the CSV) skips the two C++ parses."""
        if self._config_csv == getattr(self, "_csv_seed_key", None):
            return
        seed = swi3score.registers_from_csv(self._config_csv) if self._config_csv else []
        self._csv_replay = self._csv_writes_replay(seed)
        self._csv_snap = (swi3score.registers_from_commands(self._csv_replay)
                          if self._csv_replay else [])
        self._csv_seed_key = self._config_csv

    # ---- factories ----
    @classmethod
    def from_demo(cls, audio_samples_per_channel: int = 32, *,
                  cold_start: bool = False, phy: int = 2, variant: str = "",
                  **kw) -> "Session":
        """Synthetic demo session. `phy` = 2 (FBCSE) or 3 (DLV); with cold_start the
        matching §5.1.2 bring-up selects that PHY on the wire (and, for PHY3, is what
        makes the session pick the recovered-clock DLV decode path). `variant`
        ="flow_control" selects the four-mode flow-control demo (PHY2 framing)."""
        return cls(transitions.demo_capture(audio_samples_per_channel,
                                            cold_start=cold_start, phy=phy,
                                            variant=variant),
                   source={"type": "demo",
                           "audio_samples_per_channel": int(audio_samples_per_channel),
                           "cold_start": bool(cold_start), "phy": int(phy),
                           "variant": str(variant)},
                   **kw)

    @classmethod
    def from_saleae_binary(cls, clock_path: str, data_path: str,
                           sample_rate_hz: int, **kw) -> "Session":
        cap = saleae_binary.load_capture(clock_path, data_path, sample_rate_hz)
        return cls(cap, source={"type": "saleae_binary", "clock": clock_path,
                                "data": data_path, "sample_rate_hz": int(sample_rate_hz)},
                   **kw)

    @classmethod
    def from_sal(cls, path: str, clock_channel: int, data_channel: int,
                 sample_rate_hz: int = 0, auto_clock: bool = False,
                 window=None, max_bytes=None, **kw) -> "Session":
        """Open a .sal. `window` is an optional (start_sample, end_sample) range —
        only that span is decoded (cheaply, by seeking the v3 block chain) and the
        resulting Session is rebased so the window starts at sample 0. `max_bytes`
        caps the predicted memory (0 disables the pre-flight guard, which otherwise
        raises saleae_sal.SalTooLargeError rather than let a huge capture exhaust
        RAM). The window is recorded in `source` so a workspace reopens the same
        slice."""
        from .ingest import saleae_sal
        cap = saleae_sal.load_capture(path, clock_channel, data_channel,
                                      sample_rate_hz or None, auto_clock=auto_clock,
                                      window=window, max_bytes=max_bytes)
        src = {"type": "sal", "path": path,
               "clock_channel": int(clock_channel),
               "data_channel": int(data_channel),
               "auto_clock": bool(auto_clock),
               "sample_rate_hz": int(cap.sample_rate_hz)}
        if window is not None:
            src["window"] = [int(window[0]), int(window[1])]
        return cls(cap, source=src, **kw)

    @classmethod
    def from_digital_csv(cls, path: str, clock_col: int, data_col: int,
                         sample_rate_hz: int = 0, auto_dlv: bool = True, **kw) -> "Session":
        """Build a Session from a Logic digital-CSV export.

        When `auto_dlv` and no rate is forced, a capture whose two channels form a
        complementary differential pair is loaded at the TRUE sample rate (from the
        timestamp quantum, not the finest edge spacing) so a partial DLV (PHY3) capture
        decodes at the right samples/UI; the constructor then detects DLV, its column
        count, and the DP/DN polarity (see _autodetect_partial_dlv / analysis.dlv_detect).
        A normal forwarded-clock (FBCSE) CSV falls through to the standard load."""
        from .ingest import digital_csv
        if auto_dlv and not sample_rate_hz \
                and digital_csv.looks_complementary(path, clock_col, data_col):
            from .analysis import dlv_detect
            rate = dlv_detect.true_sample_rate_csv(path)
            if rate:
                cap = digital_csv.load_capture(path, clock_col, data_col, sample_rate_hz=rate)
                return cls(cap, source={"type": "digital_csv", "path": path,
                                        "clock_col": int(clock_col), "data_col": int(data_col),
                                        "sample_rate_hz": int(rate)}, **kw)
        cap = digital_csv.load_capture(path, clock_col, data_col, sample_rate_hz or None)
        return cls(cap, source={"type": "digital_csv", "path": path,
                                "clock_col": int(clock_col), "data_col": int(data_col),
                                "sample_rate_hz": int(cap.sample_rate_hz)}, **kw)

    @classmethod
    def from_vcd(cls, path: str, clock_ident: str, data_ident: str,
                 auto_clock: bool = False, **kw) -> "Session":
        from .ingest import vcd
        cap = vcd.load_capture(path, clock_ident, data_ident, auto_clock=auto_clock)
        return cls(cap, source={"type": "vcd", "path": path,
                                "clock_ident": str(clock_ident),
                                "data_ident": str(data_ident),
                                "auto_clock": bool(auto_clock),
                                "sample_rate_hz": int(cap.sample_rate_hz)}, **kw)

    @classmethod
    def from_wfm(cls, clock_path: str, data_path: str, *,
                 clock_thresh=None, data_thresh=None, auto_clock: bool = True,
                 sample_rate_hz: int = 0, **kw) -> "Session":
        """Build a Session from a pair of Tektronix .wfm scope exports — one analog
        channel per file, both channels of the SAME capture (they share a time base,
        taken from the first file). `clock_thresh`/`data_thresh` are optional (hi, lo)
        Schmitt pairs, auto-detected per channel when omitted (see
        analog.auto_threshold). `auto_clock` (default) assigns the forwarded clock by
        transition count — the clock toggles every UI, so it is the busier line (the
        same rule the .bin / digital-CSV importers use), which makes the `clock_path`/
        `data_path` argument ORDER irrelevant. Pass `auto_clock=False` to honour the
        given order verbatim."""
        from .ingest import analog
        from .ingest import wfm as wfm_ingest
        clk = wfm_ingest.read_wfm(clock_path)
        dat = wfm_ingest.read_wfm(data_path)
        # The two files are supposed to be the two channels of ONE acquisition, sharing
        # a time base — but they're read independently, and capture_from_analog below
        # only takes clk["time"] (reused for both channels). If the files actually
        # disagree (different record length or sample rate — e.g. the wrong pair was
        # picked, or one channel was re-captured separately), reusing clk's time axis
        # for dat's samples silently misaligns every data-edge sample position against
        # the wrong time base. Catch that here with a clear, WFM-specific error rather
        # than let it surface downstream as bogus/impossible edge samples.
        n_clk, n_dat = clk["time"].size, dat["time"].size
        rate_clk, rate_dat = clk["sample_rate_hz"], dat["sample_rate_hz"]
        rate_tol = 1e-6 * max(rate_clk, rate_dat)
        t0_clk = float(clk["time"][0]) if n_clk else 0.0
        t0_dat = float(dat["time"][0]) if n_dat else 0.0
        dt = 1.0 / rate_clk if rate_clk else 0.0
        t0_tol = max(1e-12, 1e-3 * dt)          # a tiny fraction of one sample period
        if n_clk != n_dat or abs(rate_clk - rate_dat) > rate_tol \
                or abs(t0_clk - t0_dat) > t0_tol:
            raise ValueError(
                "from_wfm: clock/data .wfm files disagree (different record length or "
                "time base) — are they the two channels of one acquisition? "
                f"({clock_path}: {n_clk} samples @ {rate_clk:.6g} Hz, t0={t0_clk:.6g}s vs "
                f"{data_path}: {n_dat} samples @ {rate_dat:.6g} Hz, t0={t0_dat:.6g}s)")
        cap = analog.capture_from_analog(clk["time"], clk["volts"], dat["volts"],
                                         sample_rate_hz or None, clock_thresh, data_thresh,
                                         auto_clock=auto_clock)
        return cls(cap, source={"type": "wfm", "clock_path": clock_path,
                                "data_path": data_path, "auto_clock": auto_clock,
                                "clock_thresh": clock_thresh, "data_thresh": data_thresh,
                                "sample_rate_hz": int(cap.sample_rate_hz)}, **kw)

    @classmethod
    def from_analog_csv(cls, path: str, *, clock="CH1", data="CH2",
                        clock_thresh=None, data_thresh=None, auto_clock: bool = False,
                        **kw) -> "Session":
        """Build a Session from a Tektronix scope analog CSV export (one file, all
        channels). `clock`/`data` select the forwarded-clock and data channels, by
        header name (e.g. "CH1") or by integer index into the file's channel order
        (excluding TIME). `auto_clock` assigns the forwarded clock between the two
        SELECTED channels by transition count (the busier line) — the UI turns it on
        for a plain two-channel file and off when the user has explicitly picked which
        of several channels is clock vs data. `clock_thresh`/`data_thresh` are optional
        (hi, lo) Schmitt pairs; auto-detected when omitted."""
        from .ingest import analog, analog_csv
        parsed = analog_csv.read_analog_csv(path)
        channels = parsed["channels"]
        names = list(channels.keys())

        def _select(sel):
            if isinstance(sel, str) and sel in channels:
                return sel
            idx = int(sel)
            return names[idx]

        clock_name = _select(clock)
        data_name = _select(data)
        cap = analog.capture_from_analog(parsed["time"], channels[clock_name],
                                         channels[data_name],
                                         parsed["sample_rate_hz"] or None,
                                         clock_thresh, data_thresh, auto_clock=auto_clock)
        return cls(cap, source={"type": "analog_csv", "path": path,
                                "clock": clock_name, "data": data_name,
                                "auto_clock": auto_clock,
                                "clock_thresh": clock_thresh, "data_thresh": data_thresh},
                   **kw)

    # Reconstruct a Session from a persisted `source` descriptor (workspace reload,
    # in-place re-decode). One dispatch, co-located with the from_* factories above, so
    # adding a capture format is a single-file change: add its from_* factory and one
    # branch here. The workspace layer just calls this (workspace.session_from_source).
    @classmethod
    def from_source(cls, source: dict, register_map=None) -> "Session":
        """Rebuild a Session from a workspace `source` descriptor (keyed by
        ``source['type']``, the tag each from_* factory stamps)."""
        kind = source.get("type")
        # Decode inputs common to every source: an optional config-driving CSV, a
        # manual Stream Sync Point row, any region column-count pins, and the Bus Grid
        # label fields (all persisted in the source descriptor). Each factory rebuilds
        # `source` from its own args, so these must be forwarded explicitly or a reopen
        # silently drops them.
        common = dict(register_map=register_map,
                      config_csv=source.get("config_csv", ""),
                      ssp_row=int(source.get("ssp_row", -1)),
                      forced_column_sections=source.get("forced_column_sections") or {},
                      label_fields=source.get("label_fields") or {})
        if kind == "demo":
            return cls.from_demo(int(source.get("audio_samples_per_channel", 32)),
                                 cold_start=bool(source.get("cold_start", False)),
                                 phy=int(source.get("phy", 2)),
                                 variant=str(source.get("variant", "")), **common)
        if kind == "saleae_binary":
            return cls.from_saleae_binary(source["clock"], source["data"],
                                          int(source["sample_rate_hz"]), **common)
        if kind == "sal":
            # Carry the window forward: a workspace saved from a windowed open must
            # reopen the same slice, not the whole (possibly unopenable) capture.
            win = source.get("window")
            return cls.from_sal(source["path"], int(source["clock_channel"]),
                                int(source["data_channel"]),
                                int(source.get("sample_rate_hz", 0)),
                                auto_clock=bool(source.get("auto_clock", False)),
                                window=(tuple(win) if win else None), **common)
        if kind == "digital_csv":
            return cls.from_digital_csv(source["path"], int(source["clock_col"]),
                                        int(source["data_col"]),
                                        int(source.get("sample_rate_hz", 0)), **common)
        if kind == "vcd":
            return cls.from_vcd(source["path"], str(source["clock_ident"]),
                                str(source["data_ident"]),
                                auto_clock=bool(source.get("auto_clock", False)), **common)
        if kind == "wfm":
            return cls.from_wfm(source["clock_path"], source["data_path"],
                                clock_thresh=source.get("clock_thresh"),
                                data_thresh=source.get("data_thresh"),
                                auto_clock=bool(source.get("auto_clock", True)),
                                sample_rate_hz=int(source.get("sample_rate_hz", 0)), **common)
        if kind == "analog_csv":
            return cls.from_analog_csv(source["path"], clock=source.get("clock", "CH1"),
                                       data=source.get("data", "CH2"),
                                       clock_thresh=source.get("clock_thresh"),
                                       data_thresh=source.get("data_thresh"),
                                       auto_clock=bool(source.get("auto_clock", False)), **common)
        raise ValueError(f"unknown workspace source type: {kind!r}")

    # ---- sub-capture / export ----
    def locate_subcapture(self, sub_capture, **kw) -> List[dict]:
        """Find every place `sub_capture`'s signal pattern occurs in this session's
        capture — sample-rate-independent (see analysis.subcapture). Returns
        {"start_sample", "end_sample", "score"} dicts, sorted by start_sample."""
        from .analysis import subcapture
        return subcapture.locate_subcapture(self.capture, sub_capture, **kw)

    def export_sal(self, path: str, *, clock_channel: int = 0, data_channel: int = 1,
                   clock_name: str = "SW_CLK", data_name: str = "SW_DATA",
                   sample_range=None) -> None:
        """Write this session's capture out as a Logic 2 .sal project (see
        ingest.sal_export) — a portable, round-trippable shrink of a large .bin/.csv
        capture down to a single file. `sample_range` (s0, s1) exports just that
        window (rebased to 0); None exports the whole capture."""
        from .ingest import sal_export
        sal_export.export_sal(self._capture_for_range(sample_range), path,
                              clock_channel=clock_channel, data_channel=data_channel,
                              clock_name=clock_name, data_name=data_name)

    def export_bin(self, path: str, *, clock_channel: int = 0, data_channel: int = 1,
                   include_clock: bool = True, include_data: bool = True,
                   sample_range=None) -> list:
        """Write the capture's clock+data lines as version-0 <SALEAE> binary blobs
        (one per included channel; see ingest.raw_export). Returns the paths written.
        `sample_range` (s0, s1) exports just that window; None the whole capture."""
        from .ingest import raw_export
        return raw_export.export_bin(self._capture_for_range(sample_range), path,
                                     clock_channel=clock_channel, data_channel=data_channel,
                                     include_clock=include_clock, include_data=include_data)

    def export_csv(self, path: str, *, clock_channel: int = 0, data_channel: int = 1,
                   clock_name: str = "Channel 0", data_name: str = "Channel 1",
                   include_clock: bool = True, include_data: bool = True,
                   sample_range=None) -> None:
        """Write the capture as a Logic-2 digital CSV (Time [s] + one 0/1 column per
        included line, a row per transition; see ingest.raw_export). `sample_range`
        (s0, s1) exports just that window; None the whole capture."""
        from .ingest import raw_export
        raw_export.export_csv(self._capture_for_range(sample_range), path,
                              clock_channel=clock_channel, data_channel=data_channel,
                              clock_name=clock_name, data_name=data_name,
                              include_clock=include_clock, include_data=include_data)

    # ---- export range conversions (drive the Export Capture dialog) ----
    def last_sample(self) -> int:
        """Last edge sample across both channels (the export/range upper bound)."""
        ce, de = self.capture.clock_edges, self.capture.data_edges
        last = 0
        if ce.size:
            last = max(last, int(ce[-1]))
        if de.size:
            last = max(last, int(de[-1]))
        return last

    def sample_at_bus_row(self, row: int) -> int:
        """Capture sample opening bus `row` (its Row Sync Point). Clamped; the public
        inverse of bus_row_for_sample, used to turn a bus-row export range into samples."""
        return int(self._sample_at_bus_row(max(0, int(row))))

    def total_bus_rows(self) -> int:
        """Bus-row count (last row index + 1) — the upper bound for a row range."""
        return int(self.bus_row_for_sample(self.last_sample())) + 1

    def _samples_per_ui(self) -> float:
        """Samples per UI from the measured UI rate (falls back to the capture's
        first-two-edges estimate). Used to map a UI export range to samples."""
        if self.ui_rate_hz and self.sample_rate_hz:
            return float(self.sample_rate_hz) / float(self.ui_rate_hz)
        return float(self.capture.samples_per_ui())

    def sample_at_ui(self, ui: int) -> int:
        """Capture sample of UI index `ui` (linear, from the measured UI rate). Clamped
        to the capture; turns a UI export range into samples."""
        s = int(round(max(0, int(ui)) * self._samples_per_ui()))
        return max(0, min(s, self.last_sample()))

    def total_uis(self) -> int:
        """UI count over the capture — the upper bound for a UI range."""
        spu = self._samples_per_ui()
        return int(self.last_sample() / spu) + 1 if spu > 0 else 0

    def _capture_for_range(self, sample_range):
        """The capture to export: the whole capture (sample_range None) or a rebased
        sub-window (s0, s1)."""
        if sample_range is None:
            return self.capture
        s0, s1 = int(sample_range[0]), int(sample_range[1])
        return self.capture.subcapture(s0, s1)

    # ---- queries ----
    def grid_cells(self, rows: int = 32) -> List[dict]:
        if self._csv_replay:
            # A config CSV is imposed (apply_config_csv): draw the grid from the CSV
            # baseline + any wire config so it matches the CSV, rather than the
            # decoder's snoop-only grid (empty for a post-commit capture).
            replay = self._csv_replay + self._grid_replay(self._config_commands())
            return swi3score.grid_from_commands(replay, rows, force_columns=self.column_count,
                                                dlv=self.is_dlv,
                                                cds_horizontal_start=self._cds_horizontal_start)
        if self._dlv_forced and self.column_count > 0:
            # Partial DLV: no config commits on the wire, so force the operational width
            # (as grid_cells_at does) — else the frame collapses to the 2-column S1/S0
            # default with no CDS column. Replay the decoded config so any writes show.
            return swi3score.grid_from_commands(
                self._grid_replay(self._config_commands()), rows,
                force_columns=int(self.column_count), dlv=True,
                cds_horizontal_start=self._cds_horizontal_start)
        return self.decoder.grid_cells(rows)

    @staticmethod
    def _grid_replay(cmds) -> List[dict]:
        """Write/commit replay for the C++ grid/register authority, from a config-
        command list (CRC-valid WriteA32 + every Commit) — shared by grid_cells,
        grid_cells_at and the CSV-baseline prepend."""
        replay: List[dict] = []
        for c in cmds:
            if c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"):
                replay.append({"is_write": True,
                               "device_mask": int(c.get("device_mask", 0)),
                               "address": int(c.get("address", 0)),
                               "data": bytes(c.get("data", b""))})
            elif c.get("is_commit"):
                replay.append({"is_commit": True,
                               "commit_confirmed": bool(c.get("commit_confirmed")),
                               "group_mask": int(c.get("group_mask", 0))})
        return replay

    def grid_cells_at(self, sample: Optional[int], rows: int = 32) -> List[dict]:
        """Bus-grid layout as-of a time cursor. Replays the decoded write/commit
        commands up to `sample` through the real register model, so the grid only
        reflects config that has actually taken effect: a dual-ranked _NEXT write
        stages and changes the grid ONLY when a confirmed commit targeting its
        commit group arrives (matching the hardware and the streaming decode) —
        not the instant the _NEXT write is seen. `sample=None` falls back to the
        decoder's final grid.

        A confirmed *sync-point* commit (SSCR/SSPA) does not take effect when the
        command completes — it commits at the SSP, Row_Delay rows later. So its
        effects (e.g. a dataport EnableCh, the column count) are deferred to the SSP
        sample, which is the decode-segment boundary it produced; otherwise the grid
        would enable the dataport ~Row_Delay rows before the width actually changes.

        When the capture genuinely reconfigures its column count mid-stream
        (distinct segment widths, e.g. cold-start 2col -> 8col), the segment under
        the cursor forces that physical width so each region draws at the geometry
        that was actually on the wire."""
        if sample is None:
            return self.grid_cells(rows)
        # WriteA32s stage / apply and confirmed commits promote, exactly as the live
        # decoder does — replayed in EFFECT order (see _commands_in_effect) so a
        # sync-point commit's deferred promotion lands after any write that physically
        # precedes its SSP. The replay dicts are precomputed once per decode.
        replay = self._grid_replay_in_effect(sample)
        # A config CSV imposed on the capture is the baseline from row 0 (its write+
        # commit replay prepended), so a post-commit capture matches the CSV and any
        # real bus writes still layer on top (later replay entries win).
        if self._csv_replay:
            replay = self._csv_replay + replay
        # What-if overrides for the section under the cursor, folded in by the C++
        # via the SAME force-write + commit-all as the audio decode (so grid and
        # audio agree). Section-scoped: only overrides whose section index matches
        # the cursor's section apply here.
        sect = self.segment_index_for_sample(int(sample))
        overrides = [(int(d), int(a), int(v))
                     for (s, d, a), v in self.register_overrides.items() if s == sect]
        # Force the segment width ONLY for a true multi-width reconfiguration (see
        # below); otherwise defer to the decoder's authoritative column_count — a
        # cold-start capture's lone segment is often LABELLED with the slow-preamble
        # width while the bus operates wider, and forcing that would collapse the grid.
        seg_widths = {int(s.get("column_count", 0)) for s in self.segments}
        force = 0
        if len(seg_widths) > 1:
            seg = self._segment_for_sample(int(sample))
            force = int(seg["column_count"]) if seg else self.column_count
        elif self._dlv_forced and self.column_count > 0:
            # A PARTIAL DLV capture (forced, no cold-start) has no NumColumns commit on
            # the wire for the grid to build from, and its detected operational width is
            # authoritative — so without forcing it here grid_from_commands collapses to
            # the default 2-column S1/S0 framing (no CDS column, Sync0 mis-drawn at col 1
            # instead of the last column). Force the real width so the frame draws Sync1
            # at Column 0, the CDS at Column 2, and Sync0 at the last column.
            force = int(self.column_count)
        return swi3score.grid_from_commands(replay, rows, force_columns=force,
                                            register_overrides=overrides,
                                            dlv=self.is_dlv,
                                            cds_horizontal_start=self._cds_horizontal_start)

    def config_dataports_at(self, sample: Optional[int]) -> dict:
        """The decoded config in effect AS-OF `sample` — the geometry the Bus Grid is
        showing at the cursor — as visualizer-CSV fields (BusConfig attrs). So Save
        Visualizer Config exports the config you're viewing (e.g. the 8-col segment)
        rather than always the final operational config. Uses the SAME command replay +
        segment force-width as :meth:`grid_cells_at`, so grid and export agree. `sample`
        None falls back to the decoder's final config."""
        if sample is None:
            cfg = self.decoder.config_dataports()
            if self.is_dlv:
                cfg["phy3_enabled"] = True          # a DLV capture (consistent with the sampled path)
            return cfg
        replay = self._grid_replay_in_effect(int(sample))
        if self._csv_replay:
            replay = self._csv_replay + replay
        sect = self.segment_index_for_sample(int(sample))
        overrides = [(int(d), int(a), int(v))
                     for (s, d, a), v in self.register_overrides.items() if s == sect]
        seg_widths = {int(s.get("column_count", 0)) for s in self.segments}
        force = 0
        if len(seg_widths) > 1:
            seg = self._segment_for_sample(int(sample))
            force = int(seg["column_count"]) if seg else self.column_count
        elif self._dlv_forced and self.column_count > 0:
            # Partial DLV: no NumColumns commit on the wire, so force the detected width
            # (as grid_cells_at does) — else the exported/visualized config reports 1
            # column instead of the operational DLV geometry.
            force = int(self.column_count)
        cfg = swi3score.config_from_commands(replay, force_columns=force,
                                             register_overrides=overrides)
        # BuildConfig can't know the row rate (it's set from capture timing, not the
        # registers), so derive it for this segment from the measured UI rate and the
        # segment's column count — the UI rate is constant across a capture's segments,
        # so 8-col reads 3.072 MRows/s where 16-col reads 1.536.
        cols = int(cfg.get("num_columns", 0)) + 1
        if self.ui_rate_hz and cols > 0:
            cfg["row_rate_khz"] = self.ui_rate_hz / cols / 1000.0
        if self.is_dlv:
            cfg["phy3_enabled"] = True             # a DLV capture, even with no config commit
        return cfg

    def _tx_geometry(self):
        """(origin_ui, columns) for the OPERATIONAL geometry — the fallback when no
        region is under the cursor (bring-up / single-width capture). On a cold start
        segment 0 is the narrow slow-clock preamble (e.g. 2 col); pick the segment
        matching the decoder's authoritative column_count (the audio config), else the
        widest, so the raster and its scrollbar agree."""
        seg = None
        if self.segments:
            seg = next((s for s in self.segments
                        if int(s.get("column_count", 0)) == int(self.column_count)), None)
            if seg is None:
                seg = max(self.segments, key=lambda s: int(s.get("column_count", 0)))
        origin = int(seg["start_ui"]) if seg else 0
        cols = max(2, int(seg["column_count"]) if seg else self.column_count)
        return origin, cols

    def _tx_bounds(self, sample=None):
        """(origin_ui, cols, end_ui) the TX map draws — the config region UNDER `sample`
        (past the bring-up), BOUNDED to that region so a multi-width capture is inspected
        one region at a time at its true width (an 8-col region shows 8 columns), the same
        way persistence is region-scoped. A single raster can't honestly span regions of
        different widths, so scrolling stays within the region; move the cursor to inspect
        another. Without a sample (or in the bring-up): operational origin → capture end."""
        nui = int(self.capture.clock_edges.size)
        seg = None
        if sample is not None and int(sample) >= int(self.audio_start_sample):
            seg = self._segment_for_sample(int(sample))
        if seg is not None:
            seg_start = int(seg["start_ui"])
            cols = max(2, int(seg["column_count"]))
            # Anchor the raster's Column 0 at the row opening (Sync1), not the segment's
            # start_ui — for DLV start_ui is the CDS UI (Column `_cds_horizontal_start`),
            # so without this the CDS lands at raster col 0 and the frame is shifted. 0 for
            # FBCSE (unchanged). Region boundaries still use the unshifted seg_start.
            origin = max(0, seg_start - int(self._cds_horizontal_start))
            later = [int(s["start_ui"]) for s in self.segments if int(s["start_ui"]) > seg_start]
            end_ui = min(later) if later else nui
            return origin, cols, min(end_ui, nui)
        origin, cols = self._tx_geometry()
        return origin, cols, nui

    def tx_total_rows(self, sample: Optional[int] = None) -> int:
        """Total 0-based bus rows the TX map can scroll through — the region under
        `sample` (bounded to it), else the operational segment to the capture end.
        `sample` selects the region geometry so the scrollbar matches the raster."""
        ce = self.capture.clock_edges
        if ce.size < 2:
            return 0
        origin, cols, end_ui = self._tx_bounds(sample)
        # Ceil, not floor: a region that ends mid-row still has that trailing partial
        # row (tx_raster clips the missing columns via its valid mask), so it must stay
        # reachable by the scrollbar rather than being dropped.
        avail = max(0, int(end_ui) - origin)
        return max(1, (avail + cols - 1) // cols)

    def tx_row_for_sample(self, sample: int) -> int:
        """0-based TX-map row containing `sample`, in the SAME (region-scoped) geometry
        tx_raster draws at — so Show Toggles opens at (and scrolls to) the cursor's row.
        Clamped to [0, tx_total_rows-1]."""
        origin, cols, _end = self._tx_bounds(sample)
        ui = self._ui_for_sample(int(sample))
        row = (ui - origin) // cols if cols else 0
        return int(max(0, min(row, self.tx_total_rows(sample) - 1)))

    def timing_column_roles(self, sample: Optional[int] = None) -> dict:
        """Map each frame column to its driver role, for the Timing pane's filter.

        From the decoded grid as-of `sample` (or the final grid when None — used so a
        specific reconfiguration region's geometry is reflected): column 0 (CDS) is the
        differential control stream, not a data column; a column owned by a *source* data
        port is peripheral-driven (by that device); a column owned by a *sink* port, or
        with no port cell at all (command / guard band), is manager-driven. A port's
        columns are the same on every row it occupies, so a handful of rows reveals every
        active column's owner (a large interval only changes *which rows* carry cells).

        Returns {col: {"kind": "cds"|"peripheral"|"manager", "device", "dp",
        "is_source"}}; columns with no cell default to manager-driven."""
        cols = max(2, int(self.column_count_at(sample)) if sample is not None
                   else int(self.column_count))
        roles: Dict[int, dict] = {}
        try:
            cells = self.grid_cells_at(sample, rows=128) if sample is not None \
                else self.grid_cells(rows=128)
        except Exception:
            # Can't resolve the grid → report roles unknown (empty) rather than guessing.
            # The caller then offers no per-driver filter, instead of mislabelling every
            # column "manager" (a wrong-but-plausible filter with no signal).
            return {}
        for cell in cells:
            c = int(cell.get("col", -1))
            if c < 0 or c >= cols or c in roles:
                continue
            if cell.get("is_cds"):
                roles[c] = {"kind": "cds", "device": 0, "dp": 0, "is_source": False}
            else:
                src = bool(cell.get("is_source"))
                roles[c] = {"kind": "peripheral" if src else "manager",
                            "device": int(cell.get("device", 0)),
                            "dp": int(cell.get("dp", 0)), "is_source": src}
        for c in range(cols):        # command / guard columns carry no cell — manager
            roles.setdefault(c, {"kind": "manager", "device": 0, "dp": 0, "is_source": False})
        return roles

    def timing_regions(self) -> List[dict]:
        """Audio-mode regions for the Timing pane — one per decode segment from the audio
        start on. A commit can change the bus clock rate / column count mid-capture (e.g.
        2 col → 16 col), and setup/hold only makes sense within a single clock rate, so
        each region is analysed on its own. Each: {label, start_sample, end_sample,
        column_count, edges, mid_sample}. The column→driver map is NOT precomputed here
        (see timing_column_roles) — the caller resolves it lazily for the region it shows,
        keyed by mid_sample. The link-control region before audio start, and any zero-
        width region (a truncated capture whose start UI overran the edges), are skipped."""
        ce = self.capture.clock_edges
        if ce.size == 0 or not self.segments:
            return []
        rate = self.sample_rate_hz or 1
        a = int(self.audio_start_sample)
        dlv = self.is_dlv
        out: List[dict] = []
        for i, s in enumerate(self.segments):
            su = int(s["start_ui"])
            cols = max(1, int(s["column_count"]))
            end_ui = int(self.segments[i + 1]["start_ui"]) if i + 1 < len(self.segments) else ce.size
            if dlv:
                # DLV clock_edges are the SPARSE DP-wire transitions (not one edge per UI),
                # so ce[su] is meaningless. Each segment already carries its recovered-clock
                # sample bounds — use them (next segment's start, or the audio end).
                start_s = int(s["start_sample"])
                end_s = (int(self.segments[i + 1]["start_sample"]) if i + 1 < len(self.segments)
                         else int(self.audio_end_sample))
            else:
                start_s = int(ce[min(su, ce.size - 1)])
                end_s = int(ce[min(end_ui, ce.size - 1)])
            if end_s <= a:                       # entirely link-control — skip
                continue
            start_s = max(start_s, a)
            if end_s <= start_s:                 # zero/negative width (truncated) — skip
                continue
            t0, t1 = start_s / rate, end_s / rate
            out.append({
                "label": f"{cols} col  ({t0:.2f}–{t1:.2f} s)",
                "start_sample": start_s, "end_sample": end_s,
                "column_count": cols,
                # Region "size" for the default-selection (largest) pick: UI count for FBCSE;
                # for DLV the sample span (its sparse edge count isn't a UI count).
                "edges": int(end_s - start_s) if dlv else int(end_ui - su),
                "mid_sample": (start_s + end_s) // 2,
            })
        return out

    def tx_raster(self, start_row: int = 0, rows: int = 64,
                  sample: Optional[int] = None) -> dict:
        """Physical data-line TRANSITION raster for the TX map — a where-is-real-data
        inspection that needs NO config CSV.

        For `rows` capture rows starting at 0-based `start_row` (row 0 = the segment's
        first complete row), and each column of the segment's geometry, report whether
        that UI carried a data transition — a data edge within the UI's clock window.
        This is raw payload activity (NOT the CDS's NRZS bit): idle/DC columns stay
        blank, columns actually transporting toggle, and an interval/skipping port
        shows as row-periodic gaps. `start_row` lets the view scroll the whole capture
        a window at a time (see tx_total_rows).

        Column 0 is the row opening (Sync1); the CDS column is `cds_col` (Column 0 for
        FBCSE, Column 2 for DLV), flagged separately. Returns {column_count, cds_col,
        row_labels (0-based, len == nrows), tx (nrows x ncols bool ndarray)}. Column
        geometry is the auto-detected/segment count, so it works pre-CSV."""
        ce = self.capture.clock_edges
        de = self.capture.data_edges
        # Draw at the region UNDER `sample` (bounded to it), else the operational geometry
        # to the capture end — shared with tx_total_rows so the raster and scrollbar agree.
        origin, cols, end_ui = self._tx_bounds(sample)
        nrows = max(1, int(rows))
        start_row = max(0, int(start_row))
        labels = [start_row + i for i in range(nrows)]
        base = {"column_count": cols, "cds_col": int(self._cds_horizontal_start),
                "row_labels": labels}
        if ce.size < 2:
            return {**base, "tx": np.zeros((nrows, cols), dtype=bool)}
        # UI index of (window row i, col c): row 0's Column 0 is at `origin` (start_ui),
        # so capture row (start_row + i) column c is origin + (start_row + i)*cols + c.
        r = np.arange(nrows, dtype=np.int64)[:, None]
        c = np.arange(cols, dtype=np.int64)[None, :]
        ui = origin + (start_row + r) * cols + c                 # (nrows, cols)
        nui = int(ce.size)
        hi = min(nui, int(end_ui))                               # don't cross into the next region
        valid = (ui >= 1) & (ui < hi)
        uic = np.clip(ui, 1, nui - 1)
        # A UI carries a transition iff its sampled bit differs from the previous UI's —
        # i.e. a data edge in (clock_edge[ui-1], clock_edge[ui]]. This matches the
        # decoder's own convention (CColumnDetector: level[i] != level[i-1]); attributing
        # the *next* interval's edge here would shift every column one left (lighting the
        # guard columns). Index preserves ce's dtype so searchsorted stays same-dtype.
        tx = (np.searchsorted(de, ce[uic]) - np.searchsorted(de, ce[uic - 1])) > 0
        return {**base, "tx": tx & valid}

    def _tx_persist_key(self, sample: Optional[int]) -> "tuple[int, int, int]":
        """The (origin_ui, columns, end_ui) region key tx_persist_columns memoizes on —
        the config region containing `sample` (its segment's start_ui to the next
        segment's, in that segment's column count), else the operational region."""
        ce = self.capture.clock_edges
        seg = self._segment_for_sample(int(sample)) if sample is not None else None
        if seg is not None:
            origin = int(seg["start_ui"])
            cols = max(2, int(seg["column_count"]))
            nxt = [int(s["start_ui"]) for s in self.segments if int(s["start_ui"]) > origin]
            end_ui = min(nxt) if nxt else int(ce.size)
        else:
            origin, cols = self._tx_geometry()
            end_ui = int(ce.size)
        return (origin, cols, end_ui)

    def tx_persist_cached(self, sample: Optional[int] = None):
        """The memoized tx_persist_columns result for `sample`'s region if it has already
        been computed, else None — WITHOUT triggering the (whole-region) scan. Lets the UI
        render instantly on a cache hit and only go off-thread on the first visit to a
        region (see the persistence worker in main_window)."""
        cache = getattr(self, "_tx_persist_cache", None)
        return cache.get(self._tx_persist_key(sample)) if cache else None

    def tx_persist_columns(self, sample: Optional[int] = None) -> "tuple[np.ndarray, int]":
        """(per-column ever-toggled boolean, column_count) for the CONFIGURATION REGION
        containing `sample` — the TX-map persistence summary. Scoped to one config
        region (a segment), NOT the whole capture: a capture can reconfigure mid-stream
        (e.g. 2col preamble -> 16col audio) and those geometries don't overlay. Scans
        every UI in that region so a column that only transports deeper in the region
        still lights up. `sample=None` uses the operational region. Column 0 is CDS.

        Memoized on the region's (origin, cols, end_ui) — the cursor can sit anywhere
        within a region and this scans every UI in it (an np.arange + two searchsorted +
        logical_or.at over the whole region), so re-running it on every cursor move while
        TX-map persistence is on was doing the full-region scan far more often than the
        region actually changes. Invalidated on re-decode (_redecode) — a scrambler/hub/
        SSP/override change can move segment boundaries."""
        ce = self.capture.clock_edges
        de = self.capture.data_edges
        # Region bounds in UI space (covering segment → next segment / capture end).
        key = self._tx_persist_key(sample)
        origin, cols, end_ui = key
        cache = getattr(self, "_tx_persist_cache", None)
        if cache is None:
            cache = self._tx_persist_cache = {}
        hit = cache.get(key)
        if hit is not None:
            return hit
        lit = np.zeros(cols, dtype=bool)
        lo = max(1, origin)
        if ce.size < 2 or end_ui <= lo:
            result = (lit, cols)
        else:
            # Which columns EVER toggled in the region. A UI toggled iff a data edge lands
            # in its [prev_edge, edge) window (same convention as the per-UI form:
            # searchsorted(de, ce[i]) - searchsorted(de, ce[i-1]) > 0). Rather than scan
            # every UI — a region can be 100s of millions of UIs, and np.logical_or.at over
            # that is pathologically slow — walk the DATA EDGES in the region: each edge
            # falls in exactly one UI (hence one column), so this is O(#data edges in the
            # region) with a vectorised bincount. de[k] in [ce[i-1], ce[i]) ⇒ UI i =
            # searchsorted(ce, de[k], side="right").
            d0 = int(np.searchsorted(de, ce[lo - 1], side="left"))    # first edge >= window start
            d1 = int(np.searchsorted(de, ce[end_ui - 1], side="left"))  # first edge >= window end
            if d1 > d0:
                ui = np.searchsorted(ce, de[d0:d1], side="right").astype(np.int64)
                in_win = (ui >= lo) & (ui < end_ui)                   # guard the boundary edges
                col = (ui[in_win] - origin) % cols
                lit = np.bincount(col, minlength=cols).astype(bool)
            result = (lit, cols)
        cache[key] = result
        return result

    def _commands_in_effect(self, sample: int) -> List[dict]:
        """Config-relevant commands whose effect is visible as-of `sample`, ordered by
        effective sample (a confirmed sync-point commit defers to its SSP, so it sorts
        after any write that physically precedes that SSP — keeping the staged-then-
        promoted replay correct). Stable, so same-sample commands keep decode order."""
        eff, cmds, _replay, _reff = self._effective_sorted()
        return cmds[:bisect.bisect_right(eff, int(sample))]

    def _grid_replay_in_effect(self, sample: int) -> List[dict]:
        """The C++ write/commit replay for the commands in effect as-of `sample`. The
        replay dicts are precomputed once per decode (in effective order), so a cursor
        move is a bisect + list slice — no per-move dict rebuild of every command."""
        _eff, _cmds, replay, replay_eff = self._effective_sorted()
        return replay[:bisect.bisect_right(replay_eff, int(sample))]

    def _effective_sorted(self):
        """(eff_samples, commands, replay, replay_eff) — the config commands sorted by
        effective sample, with the parallel C++ write/commit replay, cached. Called on
        every cursor move (grid + register views); building it once turns each
        `_commands_in_effect` / grid replay into a bisect + slice instead of an
        O(n log n) sort + per-command `_effective_commit_sample` recompute AND a
        per-move rebuild of the replay dict list. Invalidated in _redecode.

        `replay` keeps only writes+commits (reads don't drive the grid), so it is SHORTER
        than `eff`/`commands` (which include reads). `replay_eff` is the parallel list of
        effective samples for exactly those replay entries, so `_grid_replay_in_effect`
        bisects on IT — slicing `replay` by an `eff` index (which counts reads too) would
        over-include future writes/commits (a read before the cursor would pull one extra
        replay entry from AFTER it)."""
        cache = getattr(self, "_eff_sorted", None)
        if cache is None:
            pairs = sorted(((self._effective_commit_sample(c), c)
                            for c in self._config_commands()), key=lambda p: p[0])
            eff = [p[0] for p in pairs]
            cmds = [p[1] for p in pairs]
            # Precompute the replay dicts once (they're a pure function of the config
            # commands) so grid_cells_at doesn't rebuild them every cursor move. Keep a
            # parallel effective-sample list aligned to the replay (writes+commits only).
            replay, replay_eff = [], []
            for e, c in zip(eff, cmds):
                r = self._grid_replay([c])
                if r:
                    replay.append(r[0])
                    replay_eff.append(e)
            cache = (eff, cmds, replay, replay_eff)
            self._eff_sorted = cache
        return cache

    def _effective_commit_sample(self, c: dict) -> int:
        """Sample at which command `c`'s effects become visible in the as-of-cursor
        grid. Writes and non-sync-point commits take effect at their start. A
        confirmed sync-point commit (SSCR/SSPA) commits at its SSP — Row_Delay rows
        after it completes — so align it to the decode-segment boundary it produced
        (exact, and identical to where the column-count change renders); fall back to
        a Row_Delay estimate when the commit changed no geometry (no new segment)."""
        start = int(c.get("start_sample", 0))
        if not (c.get("is_commit") and c.get("has_sync_point") and c.get("commit_confirmed")):
            return start
        # The decode core records the exact row where this commit took effect (its SSP:
        # command-end row + 1 + Row_Delay - SyncPointOffset). Use that authoritative
        # commit point directly — no re-estimating, so the marker can't drift from where
        # the decode actually committed (registers + audio). The sample-space estimate
        # below is only the fallback for a commit the streaming decode never fired
        # (effective_row < 0), e.g. one whose SSP falls past the end of the capture.
        eff_row = int(c.get("effective_row", -1))
        if eff_row >= 0:
            return self._sample_at_bus_row(eff_row)
        end = int(c.get("end_sample", start))
        row_delay = int(c.get("row_delay", 0))
        spU = (self.sample_rate_hz / self.ui_rate_hz) if self.ui_rate_hz else 0.0
        if spU <= 0:
            return start
        old_cols = max(2, self.column_count_at(end))
        est = end + int((row_delay + 1) * old_cols * spU)
        # Prefer the actual segment boundary the streaming decoder anchored at the
        # SSP (so the DP-enable edge coincides exactly with the width change). The
        # window must be sized with the POST-commit width, not the stale cold-start
        # width at `end`: a 2col->16col commit's SSP boundary is ~Row_Delay rows of
        # the NEW (wide) geometry away, so a window using old_cols=2 undershoots it
        # and the estimate lands ~a few thousand samples SHORT — inside the stale
        # 2-col segment. Everything then keyed on _segment_for_sample(eff) mis-frames
        # (the symbol pane reads a 16-col stream as 2-col → rows explode ~8x and it
        # hunts a comma across millions of UIs; the grid renders the wrong width).
        after = [int(s["start_sample"]) for s in self.segments if int(s["start_sample"]) > end]
        wide = max(old_cols, self.column_count)
        window = end + int((row_delay + 3) * wide * spU * 1.5)
        cand = [b for b in after if b <= window]
        if cand:
            return min(cand)
        # No geometry change near the SSP: a best-effort Row_Delay estimate (spU is the
        # final measured UI rate, only approximate on a rate-changing cold start), but
        # never defer past the next real geometry change — that would hide the commit
        # even longer than the bug we're fixing.
        return min([est] + [b for b in after if b > window])

    def commit_point_samples(self) -> List[int]:
        """Samples where a confirmed sync-point commit (SSCR/SSPA) actually TAKES EFFECT
        — the SSP, Row_Delay rows after the command. The timeline draws a dotted commit
        marker at each; distinct from the commit command's own tick at its start."""
        return sorted(self._effective_commit_sample(c) for c in self.commands
                      if c.get("is_commit") and c.get("has_sync_point")
                      and c.get("commit_confirmed"))

    def commit_point_rows(self) -> List[dict]:
        """Synthetic 'Commit Point' command rows — one per confirmed sync-point commit,
        placed at the SSP where it takes effect (Row_Delay rows after the command), so
        the command table can show a row for the deferred effect (opcode/devices/group
        populated). PROTOTYPE: these rows live only in the command-table list, not in the
        decoded `commands` (so timeline ticks, error counts and audio are unaffected)."""
        rows = []
        for c in self.commands:
            if not (c.get("is_commit") and c.get("has_sync_point")
                    and c.get("commit_confirmed")):
                continue
            eff = self._effective_commit_sample(c)
            rows.append({
                "command": "Commit Point", "phase": "",
                "start_sample": eff, "end_sample": eff,
                "bus_row": self.bus_row_for_sample(eff),
                "device_mask": int(c.get("device_mask", 0)),
                "group_mask": int(c.get("group_mask", 0)),
                "opcode": int(c.get("opcode", 0)),
                "is_commit": True, "is_commit_point": True,
                "has_manager_packet": False, "crc_valid": True,
                "peripheral_response": -1, "ping_status": None,
            })
        return rows

    def command_cursor_sample(self, cmd: dict) -> int:
        """Sample to place the shared cursor at when command `cmd` is selected: its Row
        Sync Point — the rising edge that opens the command's row (Column 0). The decoded
        start_sample is the Column-0 bit's CLOSING (falling) edge one UI later; the RSP is
        where the row actually begins, and for a confirmed commit the Commit
        Synchronization Point is coincident with it (spec §Commit Sync Point). Anchoring
        the cursor here lands it ON the CDS / commit RSP marker instead of the next falling
        edge. (A 'Commit Point' row's start_sample already IS its SSP/RSP, so the grid /
        register map there show the post-commit state.)"""
        row = cmd.get("bus_row")
        if row is None:
            row = self.bus_row_for_sample(int(cmd.get("start_sample", 0)))
        return self._sample_at_bus_row(int(row))

    def row_sync_sample(self, sample: int) -> int:
        """The Row Sync Point sample (the RSP rising edge that opens Column 0) of the bus
        row containing `sample`. Anchors the cursor on a row-sync when selecting a CDS
        symbol or jumping to a commit, so it lands ON the RSP marker exactly as a command
        does (see command_cursor_sample) rather than the Column-0 closing edge one UI
        later. Idempotent: an RSP maps back to its own row (side='right' convention)."""
        return self._sample_at_bus_row(self.bus_row_for_sample(int(sample)))

    def audio_store(self, mmap_dir=None, pdm_dc_block=False):
        from .store.audio_store import AudioStore
        return AudioStore.from_session(self, mmap_dir=mmap_dir, pdm_dc_block=pdm_dc_block)

    def _segment_for_sample(self, sample: int) -> Optional[dict]:
        """The decode segment (column count + row base) covering `sample`. A
        multi-config capture (e.g. 2col -> 8col) has one segment per column count;
        the symbol viewer / grid must use the right one or it mis-frames the region.

        Selected in UI space (start_ui), NOT by the closing-edge `start_sample`: a segment
        begins at its first row's Row Sync Point (the rising edge opening Column 0, one UI
        before start_sample), which is exactly where a width-changing sync-point commit
        takes effect and where the cursor is anchored. Comparing start_sample would leave
        a cursor sitting on that RSP in the OLD segment, so the grid forced the old width
        for one UI after the commit already took effect (register/grid disagreeing).

        DLV is the exception: its `clock_edges` are the sparse DP transitions, so
        `_ui_for_sample` (an edge count) is NOT the source UI and would mis-select the
        segment (it compressed the whole audio region onto the 4-col Safe-Lock segment).
        The recovered UI can't be indexed off clock_edges, so DLV compares the real
        `start_sample` instead — the ≤1-UI RSP-edge subtlety above is immaterial next to
        picking the right width for the entire region."""
        if not self.segments:
            return None
        if self.is_dlv:
            seg = self.segments[0]
            for s in self.segments:
                if int(s["start_sample"]) <= int(sample):
                    seg = s
                else:
                    break
            return seg
        ui = self._ui_for_sample(int(sample))
        seg = self.segments[0]
        for s in self.segments:
            if int(s["start_ui"]) <= ui:
                seg = s
            else:
                break
        return seg

    def segment_index_for_sample(self, sample: int) -> int:
        """Index (in segments()) of the config section covering `sample` — the key
        for section-scoped what-if register overrides. 0 when there are no segments.
        RSP-aligned in UI space (see _segment_for_sample); DLV compares start_sample
        since its clock_edges aren't per-UI."""
        key, field = ((int(sample), "start_sample") if self.is_dlv
                      else (self._ui_for_sample(int(sample)), "start_ui"))
        idx = 0
        for i, s in enumerate(self.segments or []):
            if int(s[field]) <= key:
                idx = i
            else:
                break
        return idx

    def source_label(self) -> str:
        """A short human name for the loaded capture (for the window title / UI),
        e.g. the .sal/.csv/.vcd file's basename, or 'PHY3 Demo Capture'. A two-file
        source (Saleae binary pair or .wfm pair) shows BOTH channel files (clock &
        data)."""
        s = self.source or {}
        t = s.get("type")
        if t == "demo":
            # Now that several demo captures exist (one per audio-mode PHY, plus the
            # flow-control variant), name which one is loaded instead of a generic label.
            if s.get("variant") == "flow_control":
                return "Flow Control Demo Capture"
            phy = s.get("phy")
            return f"PHY{int(phy)} Demo Capture" if phy else "Demo Capture"
        # Single-file sources carry the actual file in "path"; any clock/data here are
        # CHANNELS within that file (e.g. analog CSV "CH1"/"CH2", .sal channel indices),
        # not filenames — so the file's basename is the right label. Check this FIRST,
        # before the two-file cases, or an analog CSV would show "CH1 & CH2".
        path = s.get("path")
        if path:
            return os.path.basename(str(path))
        # Two-file sources: a .wfm pair (clock_path/data_path) or a Saleae binary pair
        # (clock/data ARE the two file paths). Show both channel files.
        clock_path, data_path = s.get("clock_path"), s.get("data_path")
        if clock_path and data_path:
            return f"{os.path.basename(str(clock_path))} & {os.path.basename(str(data_path))}"
        clock, data = s.get("clock"), s.get("data")
        if clock and data:
            return f"{os.path.basename(str(clock))} & {os.path.basename(str(data))}"
        for key in ("clock", "data", "clock_path", "data_path"):
            v = s.get(key)
            if v:
                return os.path.basename(str(v))
        return "Capture"

    def section_ui_stats(self) -> List[dict]:
        """Per config-section UI (unit-interval) period statistics, in nanoseconds:
        median / min / max of the clock-edge spacing within each section's sample
        span. UI can change between sections (a mid-stream reconfiguration), so this
        is reported per section rather than one figure. Idle gaps (> 4x the section
        median, e.g. between bursts) are excluded from min/max so they don't dwarf
        the real UI. Returns one dict per section: section, column_count, ui_ns,
        ui_min_ns, ui_max_ns (empty if there are no clock edges)."""
        clk = np.asarray(self.capture.clock_edges, dtype=np.int64)
        rate = int(self.sample_rate_hz) or 1
        to_ns = 1e9 / rate
        segs = self.segments or [{"start_sample": 0, "column_count": self.column_count}]
        starts = [int(s.get("start_sample", 0)) for s in segs]
        out: List[dict] = []
        if self.is_dlv:
            # DLV clock_edges are the SPARSE DP-wire transitions (not one per UI), so their
            # gaps are run-lengths, NOT the UI period. Derive the UI from the recovered
            # clock: the row period (recovered Row-Sync spacing) is constant, so a section's
            # UI = row_period / its column count. min/max come from the per-row jitter.
            rsp = self._recovered_row_syncs()
            for i, s in enumerate(segs):
                cols = max(1, int(s.get("column_count", 0)))
                lo = starts[i]
                hi = starts[i + 1] if i + 1 < len(starts) else int(self.audio_end_sample)
                rr = rsp[(rsp >= lo) & (rsp < hi)]
                gaps = np.diff(rr) * to_ns                     # row periods in this section
                if gaps.size == 0:
                    continue
                per = gaps / cols                              # UI = row period / columns
                med = float(np.median(per))
                band = per[(per > 0) & (per < 4 * med)] if med > 0 else per
                if band.size == 0:
                    band = per
                out.append({"section": i, "column_count": cols, "ui_ns": med,
                            "ui_min_ns": float(band.min()), "ui_max_ns": float(band.max())})
            return out
        for i, s in enumerate(segs):
            lo = starts[i]
            hi = starts[i + 1] if i + 1 < len(starts) else (int(clk[-1]) + 1 if clk.size else lo)
            # clock_edges is sorted, so bisect the segment's [lo, hi) span instead of a
            # full-array boolean mask — the mask was O(segments x total_edges), which on a
            # capture that reconfigures many times (each a segment) scanned all edges per
            # segment and made "Measuring bus sections" take tens of seconds. Slicing is
            # O(segments x log N + N).
            i0 = int(np.searchsorted(clk, lo, side="left"))
            i1 = int(np.searchsorted(clk, hi, side="left"))
            a = clk[i0:i1]
            gaps = np.diff(a) * to_ns
            if gaps.size == 0:
                continue
            med = float(np.median(gaps))
            band = gaps[(gaps > 0) & (gaps < 4 * med)] if med > 0 else gaps
            if band.size == 0:
                band = gaps
            out.append({"section": i, "column_count": int(s.get("column_count", 0)),
                        "ui_ns": med, "ui_min_ns": float(band.min()), "ui_max_ns": float(band.max())})
        return out

    def column_count_at(self, sample: Optional[int]) -> int:
        """Bus column count in effect at `sample`. A capture that reconfigures its
        geometry mid-stream (e.g. PHY2's Safe-Lock-2 2-col entry → 8-col audio) has
        one segment per width, so the grid axis must follow the segment under the
        cursor rather than always using the final `column_count`."""
        if sample is None or len(self.segments) <= 1:
            return self.column_count
        seg = self._segment_for_sample(int(sample))
        return int(seg["column_count"]) if seg else self.column_count

    def _ui_for_sample(self, sample: int) -> int:
        """Absolute source UI index owning `sample`, using the Row-Sync-Point convention:
        a sample sitting exactly on a Column-0 RSP rising edge belongs to the row that edge
        OPENS (side='right'), not the one it closes — so a commit point / command anchored
        at its RSP (see _sample_at_bus_row) maps back to its own row, and hovering a CDS/
        commit marker reports the row it starts. Interior samples are unaffected (left ==
        right off an edge). (bus_rows_for_samples keeps the closing-edge convention for
        audio data bits, whose last-column sample point lands on the NEXT row's RSP edge
        yet belongs to the row it sits in.)

        DLV: `clock_edges` are the sparse DP transitions, so searchsorting them gives an
        edge count, NOT the source UI index (it under-counts badly, which threw every
        cursor→row/segment mapping off between the Timeline, Bus Grid and commands). The
        recovered UI period is constant within a segment and rows are integer UIs, so the
        UI index is the segment's `start_ui` plus how many recovered UIs `sample` sits past
        that segment's Column-0 anchor (`start_sample`)."""
        sample = max(0, int(sample))
        if self.is_dlv:
            rc = self.recovered_clock()
            ui = rc[1] if rc else 0.0
            seg = self._segment_for_sample(sample)     # DLV: selected by start_sample
            if seg is not None and ui > 0.0:
                # The recovered bit clock changes with the geometry (row rate constant, UI =
                # row_period / columns), so use THIS segment's UI — not the final operational
                # one — or Safe-Lock UIs get over-counted and the mapping goes non-monotonic
                # across the commit. row_period = operational UI * operational columns.
                seg_ui = ui * self.column_count / max(1, int(seg["column_count"]))
                off = int(round((sample - int(seg["start_sample"])) / seg_ui))
                return int(seg["start_ui"]) + max(0, off)
        return int(_searchsorted(self.capture.clock_edges, sample, side="right"))

    def _recovered_row_syncs(self) -> np.ndarray:
        """The DLV Row Sync Points as a sorted int64 array (empty for FBCSE): the
        once-per-row Sync0→Sync1 (S0→S1) 0→1 edge that opens each row, resolved to the
        actual DP-wire edge (see `_rsp_analysis` / `recovered_clock`). Only rows whose
        opening edge is present are included; missing ones are surfaced separately by
        `missing_rsp_samples`. Cached; the recovered clock is fixed after decode."""
        cached = getattr(self, "_rec_syncs_cache", None)
        if cached is not None:
            return cached
        on_edge = self._rsp_analysis()[0] if self.is_dlv else np.zeros(0, dtype=np.int64)
        syncs = np.sort(np.asarray(on_edge, dtype=np.int64))
        self._rec_syncs_cache = syncs
        return syncs

    def cds_column_samples(self, lo_sample: int, hi_sample: int,
                           max_marks: int = 4000) -> np.ndarray:
        """Capture sample numbers of the rising clock edge that *begins* each
        Column-0 (the CDS column) — the Row Sync Point — within
        [lo_sample, hi_sample].

        Column 0 recurs every `column_count` UIs from each segment's `start_ui`.
        The decoder aligns to a rising clock edge and calls the UI *after* it
        Column 0 (see Decoder::resync / run: "the rising edge is the alignment UI;
        Column 0 is the NEXT UI"), so the segment's `start_ui` is that next UI and
        its own bounding edge `clock_edges[start_ui]` is the *falling* edge. The
        rising edge that opens the CDS UI — the Row Sync Point — is therefore the
        preceding edge `clock_edges[start_ui - 1 + k*column_count]`, which is what
        we mark. A capture that reconfigures its geometry mid-stream has one
        segment per width, so each is walked over its own UI span.

        PHY3 (DLV) has no forwarded clock, so `clock_edges` are the sparse DP-wire
        transitions (they toggle only when the level changes), NOT one edge per UI —
        the per-UI indexing above would place non-periodic marks. Instead the marks
        come straight from the virtual PLL's recovered per-row Sync1 samples, which
        are the true (periodic) Row Sync Points.

        Returns an empty array when the window would hold more than `max_marks`
        CDS columns — too dense to be legible, so the caller hides the marks and
        lets the user zoom in.
        """
        if self.is_dlv:
            syncs = self._recovered_row_syncs()
            if syncs.size == 0:
                return np.zeros(0, dtype=np.uint64)
            i0 = int(np.searchsorted(syncs, max(0, int(lo_sample)), side="left"))
            i1 = int(np.searchsorted(syncs, int(hi_sample), side="right"))
            sel = syncs[i0:i1]
            if sel.size == 0 or sel.size > max_marks:     # none / too dense to be legible
                return np.zeros(0, dtype=np.uint64)
            return np.ascontiguousarray(sel.astype(np.uint64))
        ce = self.capture.clock_edges
        if ce.size == 0:
            return np.zeros(0, dtype=np.uint64)
        segs = self.segments or [{"start_ui": 0,
                                  "column_count": max(2, self.column_count)}]
        lo_ui = int(_searchsorted(ce, max(0, int(lo_sample)), side="left"))
        hi_ui = min(int(_searchsorted(ce, int(hi_sample), side="right")), ce.size)
        mark_arrays: List[np.ndarray] = []
        total = 0
        for i, seg in enumerate(segs):
            origin = int(seg["start_ui"])
            cols = max(1, int(seg["column_count"]))
            seg_end = int(segs[i + 1]["start_ui"]) if i + 1 < len(segs) else ce.size
            end_ui = min(seg_end, hi_ui)
            if end_ui <= origin or end_ui <= lo_ui:
                continue
            first = max(origin, lo_ui)
            k0 = (first - origin + cols - 1) // cols     # first Column-0 UI >= window start
            # Column-0 UIs are an arithmetic progression (origin + k0*cols, step cols); the
            # RSP is the rising edge that OPENS each (ui - 1). Vectorize the walk (same values
            # as the old per-UI append loop) and drop a negative RSP (only ui==0).
            uis = np.arange(origin + k0 * cols, end_ui, cols, dtype=np.int64)
            rsps = uis - 1
            rsps = rsps[rsps >= 0]
            if rsps.size:
                mark_arrays.append(rsps)
                total += int(rsps.size)
                if total > max_marks:                    # too dense — hide (caller zooms in)
                    return np.zeros(0, dtype=np.uint64)
        if not mark_arrays:
            return np.zeros(0, dtype=np.uint64)
        return np.ascontiguousarray(ce[np.concatenate(mark_arrays)])

    def commit_column_samples(self, lo_sample: int, hi_sample: int) -> np.ndarray:
        """Row-Sync-Point sample positions of confirmed Commits within
        [lo_sample, hi_sample] — placed where each commit actually **takes effect**,
        not where the commit command sits.

        A sync-point commit (SSCR/SSPA) doesn't take effect when it completes: it
        commits at its SSP, Row_Delay rows later, and that is where its payload /
        dataport-enable begins and where the bus grid flips (see
        :meth:`_effective_commit_sample`). Marking the effect RSP — rather than the
        commit command's own row — makes the commit marker coincide with where the
        port sample bits start and where the grid activates, so the three agree on
        screen. Snapped to the Column-0 RSP of the row that contains the effect
        sample (same RSP edge as :meth:`cds_column_samples`)."""
        # The commit RSP samples don't depend on the viewport, so compute them ONCE
        # (cached, invalidated on re-decode) and bisect to [lo, hi] — the Raw Capture
        # pan/zoom fires this continuously, and a full config-command scan per call was
        # O(all config commands) regardless of how far it's zoomed in.
        allc = self._commit_rsp_samples_all()
        if allc.size == 0:
            return np.zeros(0, dtype=np.uint64)
        i0 = int(np.searchsorted(allc, int(lo_sample), side="left"))
        i1 = int(np.searchsorted(allc, int(hi_sample), side="right"))
        return np.ascontiguousarray(allc[i0:i1])

    def _commit_rsp_samples_all(self) -> np.ndarray:
        """All confirmed-commit effect RSP samples (sorted, unique) for the whole
        capture — the viewport-independent basis for :meth:`commit_column_samples`.
        Built lazily and cached; invalidated on re-decode."""
        cached = getattr(self, "_commit_rsp_cache", None)
        if cached is not None:
            return cached
        if self.is_dlv:
            # DLV: `clock_edges` aren't per-UI, so snap each commit's effect sample to
            # the recovered Row Sync Point that opens its row (same marks cds uses).
            syncs = self._recovered_row_syncs()
            out: List[int] = []
            if syncs.size:
                for cmd in self._config_commands():
                    if not (cmd.get("is_commit") and cmd.get("commit_confirmed")):
                        continue
                    eff = int(self._effective_commit_sample(cmd))
                    idx = int(np.searchsorted(syncs, eff, side="right")) - 1  # row start at/before
                    if idx >= 0:
                        out.append(int(syncs[idx]))
            allc = (np.asarray(sorted(set(out)), dtype=np.uint64) if out
                    else np.zeros(0, dtype=np.uint64))
            self._commit_rsp_cache = allc
            return allc
        ce = self.capture.clock_edges
        edges = []
        if ce.size:
            for cmd in self._config_commands():     # commits are in the small config list
                if not (cmd.get("is_commit") and cmd.get("commit_confirmed")):
                    continue
                eff = int(self._effective_commit_sample(cmd))    # SSP for sync-point commits
                seg = self._segment_for_sample(eff)
                if seg is None:
                    continue
                cols = max(1, int(seg["column_count"]))
                origin = int(seg["start_ui"])
                ui = self._ui_for_sample(eff)
                k = (ui - origin) // cols                 # row within the segment
                rsp = origin + k * cols - 1                # RSP edge opening that row
                if 0 <= rsp < ce.size:
                    edges.append(rsp)
        allc = (np.ascontiguousarray(ce[np.asarray(sorted(set(edges)), dtype=np.int64)])
                if edges else np.zeros(0, dtype=np.uint64))
        self._commit_rsp_cache = np.asarray(allc, dtype=np.uint64)
        return self._commit_rsp_cache

    def _port_marks_sorted(self) -> Dict[str, np.ndarray]:
        """Per-value read spans (device, dp, start_sample, end_sample) sorted by
        start_sample — the index behind :meth:`port_sample_marks`. Built lazily
        from the decoded audio columns and invalidated on re-decode."""
        if self._mark_cache is None:
            c = self._audio_cols
            st = np.asarray(c["start_sample"], dtype=np.int64)
            order = np.argsort(st, kind="stable")
            self._mark_cache = {
                "start": st[order],
                "end": np.asarray(c["end_sample"], dtype=np.int64)[order],
                "device": np.asarray(c["device"], dtype=np.int32)[order],
                "dp": np.asarray(c["dp"], dtype=np.int32)[order],
                "channel": np.asarray(c["channel"], dtype=np.int32)[order],
                "value": np.asarray(c["value"], dtype=np.uint32)[order],
                "sample_size": np.asarray(c["sample_size"], dtype=np.int32)[order],
                "index": np.asarray(c["index"], dtype=np.uint64)[order],
            }
            # Precompute the signed decimal value once (it only depends on the decoded
            # audio) — samples_around used to recompute it over the whole table on
            # every cursor move.
            self._mark_cache["signed"] = self._signed(
                self._mark_cache["value"], self._mark_cache["sample_size"])
            # Widest value span (marks are sorted by start; a value can only straddle
            # `lo` if its start is within max_span of it) — lets port_sample_marks back
            # up exactly instead of by a fixed count that a long interval could exceed.
            spans = self._mark_cache["end"] - self._mark_cache["start"]
            self._mark_cache["max_span"] = int(spans.max()) if spans.size else 0
        return self._mark_cache

    def bus_row_for_sample(self, sample: int) -> int:
        """The SWI3S bus row (Column-0 index) covering `sample`, on the same
        continuous scale as the command table / symbol viewer: row_base +
        (UI - start_ui) // column_count for the segment that covers it.

        `start_ui` is the segment's ANCHOR UI, recorded at the CDS column
        (`_cds_horizontal_start` — Column 0 for FBCSE, Column 2 for DLV), so the CDS
        offset is added back to count rows from Column 0. It's 0 for FBCSE (unchanged);
        without it a DLV row came out one low (Column 0 mapped to row_base-1)."""
        seg = self._segment_for_sample(int(sample))
        if seg is None:
            return 0
        cols = max(1, int(seg["column_count"]))
        # DLV: anchor on the ACTUAL recovered row-opening (RSP) edges — exact and jitter-
        # proof. A real analyzer samples off-multiple of the UI, so the edges land on a
        # rounded, ±1-jittered grid; uniform UI arithmetic drifts from them and, at a row
        # boundary, round()s the RSP edge down into the previous UI. The RSP that OPENS a row
        # is the last one at/before the sample, and rsp[k] IS bus row k (the decoder records
        # one RSP per row from Row 0 in mRowCounter order — the same numbering the commit's
        # effective_row / segment row_base use). So bus row = searchsorted(rsp, s) - 1 and the
        # inverse is rsp[row]; a +1 here desynced the commit marker from the geometry change.
        if self.is_dlv:
            rsp = self._recovered_row_syncs()
            if rsp.size:
                return int(max(0, int(np.searchsorted(rsp, int(sample), side="right")) - 1))
        ui = self._ui_for_sample(int(sample))
        return int(seg["row_base"]) + (ui - int(seg["start_ui"]) + self._cds_horizontal_start) // cols

    def bus_rows_for_samples(self, samples: np.ndarray) -> np.ndarray:
        """Vectorised :meth:`bus_row_for_sample` for an array of samples — used to
        row-number a whole window at once (per-row Python calls were the navigation
        hot spot). Assigns each sample to its segment, then computes rows with array
        ops."""
        s = np.asarray(samples, dtype=np.int64)
        if s.size == 0:
            return np.zeros(0, dtype=np.int64)
        segs = self.segments or [{"start_ui": 0, "row_base": 0,
                                  "column_count": max(2, self.column_count)}]
        seg_starts = np.array([int(x["start_sample"]) for x in segs], dtype=np.int64)
        origin = np.array([int(x["start_ui"]) for x in segs], dtype=np.int64)
        base = np.array([int(x["row_base"]) for x in segs], dtype=np.int64)
        cols = np.array([max(1, int(x["column_count"])) for x in segs], dtype=np.int64)
        seg_idx = np.clip(np.searchsorted(seg_starts, s, side="right") - 1, 0, len(segs) - 1)
        # DLV: row-number through the ACTUAL recovered RSP edges (exact, jitter-proof) —
        # the same anchor as the scalar bus_row_for_sample, so the audio pane (batch) and
        # the Timeline/commands (scalar) never diverge. rsp[k] IS bus row k (one RSP per row
        # from Row 0, matching mRowCounter / effective_row), so bus row = searchsorted(rsp,s)-1.
        if self.is_dlv:
            rsp = self._recovered_row_syncs()
            if rsp.size:
                return np.maximum(0, np.searchsorted(rsp, s, side="right").astype(np.int64) - 1)
        # side='left' (closing-edge convention): a data bit's sample point is its column's
        # closing edge, which for the LAST column coincides with the next row's RSP edge but
        # belongs to the row it sits in — unlike the scalar bus_row_for_sample / _ui_for_sample
        # RSP convention (side='right') used for cursor / commit / marker anchoring.
        ui = _searchsorted(self.capture.clock_edges, np.maximum(s, 0), side="left").astype(np.int64)
        return base[seg_idx] + (ui - origin[seg_idx]) // cols[seg_idx]

    def _sample_at_bus_row(self, row: int) -> int:
        """Capture sample of bus `row`'s Row Sync Point — the rising edge that OPENS its
        Column 0 (ce[ui-1]), NOT the closing/falling edge one UI later (ce[ui]). Per the
        spec the Commit Synchronization Point is coincident with this RSP, so commit points
        and command row-syncs anchor here; the inverse of bus_row_for_sample (which uses the
        matching side='right' RSP convention). Picks the segment with the greatest
        row_base <= row, then maps row -> its Column-0 UI -> the preceding (RSP) edge."""
        row = int(row)
        segs = self.segments or [{"start_ui": 0, "row_base": 0,
                                  "column_count": max(2, self.column_count)}]
        seg = segs[0]
        for sgm in segs:                       # segments are row_base-ascending
            if int(sgm["row_base"]) <= row:
                seg = sgm
            else:
                break
        cols = max(1, int(seg["column_count"]))
        # DLV: clock_edges aren't per-UI, so map row → sample through the recovered clock:
        # the row's Column-0 mid-UI sample is start_sample + rows_in*cols*ui, and the
        # row-opening (RSP) 0→1 edge is half a UI before it — the same PLL edge the RSP
        # marks use, so cursor/commit anchoring lands on the marker.
        # DLV: return the ACTUAL recovered row-opening (RSP) edge for this bus row — the
        # exact inverse of bus_row_for_sample (which indexes the same edges). rsp[k] IS bus
        # row k (one RSP per row from Row 0, matching mRowCounter / a commit's effective_row),
        # so bus row R opens at rsp[R]. Jitter-proof, and cursors/commits land exactly on the
        # RSP mark AND on where the clock/data change geometry (a +1 here desynced the commit
        # marker from the column-count change by one row).
        if self.is_dlv:
            rsp = self._recovered_row_syncs()
            if rsp.size:
                idx = min(max(0, row), int(rsp.size) - 1)
                return int(rsp[idx])
        edges = self.capture.clock_edges
        if edges is None or len(edges) == 0:
            return 0
        ui = int(seg["start_ui"]) + (row - int(seg["row_base"])) * cols
        rsp = max(0, min(ui - 1, len(edges) - 1))   # the RSP rising edge opening Column 0
        return int(edges[rsp])

    @staticmethod
    def _signed(values: np.ndarray, bits: np.ndarray) -> np.ndarray:
        """Signed (two's-complement) interpretation of `values` given per-element
        `bits`; a 1-bit value is a PDM density code mapped {0,1}->{-1,+1}."""
        v = np.asarray(values, dtype=np.int64)
        b = np.asarray(bits, dtype=np.int64)
        half = np.where(b > 0, 1 << np.maximum(b - 1, 0), 1)
        full = 1 << np.maximum(b, 1)
        signed = np.where(v >= half, v - full, v)
        return np.where(b <= 1, v * 2 - 1, signed)     # PDM: {0,1}->{-1,+1}

    def _filtered_positions(self, ports=None, channels=None, value_pred=None):
        """(m, base): the start-sorted marks dict and an int64 array of the positions
        into it that pass the filters — or base=None when no filter is active (positions
        are the full 0..N-1). Filters are vectorised over the whole capture.

        Memoized on the normalized (hashable) filter tuple: the Samples pane calls this
        (via sample_marks_total / sample_position_of / samples_by_position) up to 4x per
        cursor move with the SAME filters, and it was rebuilding the full-length boolean
        mask (np.isin over every filter dimension + flatnonzero over ALL marks) every
        time. Invalidated whenever `_mark_cache` is (re-decode; see _refresh_audio)."""
        key = self._filter_key(ports, channels, value_pred)
        cache = self._filtered_cache
        if cache is not None and cache[0] == key:
            return cache[1]
        m = self._port_marks_sorted()
        st = m["start"]
        if st.size == 0:
            result = (m, np.zeros(0, dtype=np.int64))
        elif not ports and not channels and not value_pred:
            result = (m, None)
        else:
            keep = np.ones(st.size, dtype=bool)
            if ports:
                want = {(int(d), int(p)) for d, p in ports}
                key_arr = (m["device"].astype(np.int64) << 16) | (m["dp"].astype(np.int64) & 0xFFFF)
                wantkey = np.array(sorted((d << 16) | (p & 0xFFFF) for d, p in want), dtype=np.int64)
                keep &= np.isin(key_arr, wantkey)
            if channels:
                want_ch = np.array(sorted({int(c) for c in channels}), dtype=np.int64)
                keep &= np.isin(m["channel"].astype(np.int64), want_ch)
            if value_pred:
                op, thr = value_pred
                thr = int(thr)
                sg = m["signed"]
                ops = {"<=": sg <= thr, "<": sg < thr, "==": sg == thr,
                       "!=": sg != thr, ">=": sg >= thr, ">": sg > thr}
                if op in ops:
                    keep &= ops[op]
            result = (m, np.flatnonzero(keep))
        self._filtered_cache = (key, result)
        return result

    @staticmethod
    def _filter_key(ports, channels, value_pred):
        """Normalize (ports, channels, value_pred) to a hashable, order-independent
        key for the `_filtered_positions` memo."""
        p = tuple(sorted((int(d), int(p)) for d, p in ports)) if ports else None
        c = tuple(sorted(int(x) for x in channels)) if channels else None
        v = (str(value_pred[0]), int(value_pred[1])) if value_pred else None
        return (p, c, v)

    def _marks_rows(self, m, sel) -> List[dict]:
        """Build row dicts for `sel` (positions into the start-sorted marks)."""
        sel = np.asarray(sel, dtype=np.int64)
        if sel.size == 0:
            return []
        starts = m["start"][sel]
        rows = self.bus_rows_for_samples(starts)
        signed = m["signed"][sel]
        dev, dp, ch = m["device"][sel], m["dp"][sel], m["channel"][sel]
        val, ss, ix = m["value"][sel], m["sample_size"][sel], m["index"][sel]
        # Convert each column to a Python list ONCE (.tolist() also yields native ints, so
        # no per-row int() cast) — scalar-indexing a NumPy array per row is ~2-3x slower on
        # this Decoded-Samples hot path. Output identical.
        dev, dp, ch = dev.tolist(), dp.tolist(), ch.tolist()
        val, ss, ix = val.tolist(), ss.tolist(), ix.tolist()
        starts, rows, signed = starts.tolist(), rows.tolist(), signed.tolist()
        return [{"device": dev[i], "dp": dp[i], "channel": ch[i],
                 "value": val[i], "sample_size": ss[i], "index": ix[i],
                 "start_sample": starts[i], "row": rows[i],
                 "signed": signed[i]}
                for i in range(sel.size)]

    def sample_marks_total(self, ports=None, channels=None, value_pred=None) -> int:
        """Total decoded-sample count matching the filters (the full scrollable extent)."""
        m, base = self._filtered_positions(ports, channels, value_pred)
        return int(m["start"].size if base is None else base.size)

    def sample_position_of(self, sample: int, ports=None, channels=None,
                           value_pred=None) -> int:
        """Position (row index) of `sample` within the filtered, start-sorted samples."""
        m, base = self._filtered_positions(ports, channels, value_pred)
        st = m["start"]
        if st.size == 0 or (base is not None and base.size == 0):
            return 0
        starts = st if base is None else st[base]
        return int(np.searchsorted(starts, int(sample)))

    def samples_by_position(self, lo: int, hi: int, ports=None, channels=None,
                            value_pred=None) -> List[dict]:
        """Decoded samples for filtered row positions [lo, hi) — the slice primitive the
        Samples pane uses to lazily extend its window when scrolled to either end."""
        m, base = self._filtered_positions(ports, channels, value_pred)
        n = m["start"].size if base is None else base.size
        lo = max(0, int(lo))
        hi = min(int(n), int(hi))
        if hi <= lo:
            return []
        sel = np.arange(lo, hi) if base is None else base[lo:hi]
        return self._marks_rows(m, sel)

    def samples_around(self, sample: int, half_window: int = 400,
                       ports=None, channels=None, value_pred=None) -> List[dict]:
        """Decoded audio samples near `sample` (windowed for scalability), sorted by
        their MSB position. Each dict has device, dp, channel, value, sample_size,
        index, start_sample, row (MSB bus row) and signed (signed decimal value).

        Optional filters (applied vectorised over the whole capture, so navigation
        stays cheap): `ports` = iterable of (device, dp) to keep; `channels` =
        iterable of channel numbers to keep; `value_pred` = (op, threshold) applied
        to the signed decimal value, op in {'<=','<','==','!=','>=','>'}. Clicking a
        row seeks to start_sample."""
        m, base = self._filtered_positions(ports, channels, value_pred)
        st = m["start"]
        if st.size == 0 or (base is not None and base.size == 0):
            return []
        if base is None:
            c = int(np.searchsorted(st, int(sample)))
            sel = np.arange(max(0, c - half_window), min(st.size, c + half_window))
        else:
            c = int(np.searchsorted(st[base], int(sample)))
            sel = base[max(0, c - half_window):min(base.size, c + half_window)]
        return self._marks_rows(m, sel)

    def port_sample_marks(self, lo_sample: int, hi_sample: int,
                          max_marks: int = 4000) -> Dict[str, np.ndarray]:
        """Read markers for the **active data ports** whose decoded values overlap
        [lo_sample, hi_sample].

        Unlike the per-UI clock grid, this reflects only the ports that actually
        transported audio: each decoded value carries the (device, dp, channel)
        that read it and the sample span [start_sample, end_sample] over which its
        bits were latched — start_sample being the beginning of that sample
        interval. The caller lanes/colours each by (device, dp, channel). Returns
        parallel arrays `start, end, device, dp, channel`; empty when the window
        holds more than `max_marks` values (too dense — the caller hides them and
        lets the user zoom in)."""
        empty = {"start": np.zeros(0, np.int64), "end": np.zeros(0, np.int64),
                 "device": np.zeros(0, np.int32), "dp": np.zeros(0, np.int32),
                 "channel": np.zeros(0, np.int32)}
        m = self._port_marks_sorted()
        st = m["start"]
        if st.size == 0:
            return empty
        lo, hi = int(lo_sample), int(hi_sample)
        hi_idx = int(np.searchsorted(st, hi, side="right"))     # start <= hi
        # Back up to the first mark that could still straddle `lo`: any value with
        # start < lo - max_span has already ended before lo. Exact (a fixed backup
        # count could miss a value with a very long sample interval).
        a = int(np.searchsorted(st, lo - m["max_span"], side="left"))
        end = m["end"][a:hi_idx]
        keep = np.flatnonzero(end >= lo)                        # overlaps the window
        if keep.size == 0 or keep.size > max_marks:
            return empty
        return {"start": m["start"][a:hi_idx][keep], "end": end[keep],
                "device": m["device"][a:hi_idx][keep], "dp": m["dp"][a:hi_idx][keep],
                "channel": m["channel"][a:hi_idx][keep]}

    def _port_bit_window_uis(self, lo_sample: int, hi_sample: int):
        """(start_ui, end_ui) source-UI range covering the capture-sample window
        [lo_sample, hi_sample], for the on-demand windowed bit-sample decode. Returns
        None when there are no edges or the range is empty.

        DLV: clock_edges aren't per-UI, so map through the recovered-UI helper
        (`_ui_for_sample`) — searchsorting the sparse edges gave far-too-small UIs, so
        the window landed in the bring-up region and no audio-port bits were found."""
        if self.is_dlv:
            lo_ui = self._ui_for_sample(max(0, int(lo_sample)))
            hi_ui = self._ui_for_sample(int(hi_sample))
            return (lo_ui, hi_ui) if hi_ui > lo_ui else None
        ce = self.capture.clock_edges
        if ce is None or len(ce) == 0:
            return None
        n = int(len(ce))
        lo_ui = int(_searchsorted(ce, max(0, int(lo_sample)), side="left"))
        hi_ui = min(int(_searchsorted(ce, int(hi_sample), side="right")), n - 1)
        if hi_ui <= lo_ui:
            return None
        return lo_ui, hi_ui

    def port_bit_marks(self, lo_sample: int, hi_sample: int,
                       max_marks: int = 8000) -> Dict[str, np.ndarray]:
        """Per-data-bit sample points for the active data ports within
        [lo_sample, hi_sample].

        Each record is the exact clock edge at which one data bit was latched, with
        the (device, dp, channel) that read it and `is_start` set on the MSB — the
        bit that begins that sample's interval. This is what shows *which* edges are
        bit samples (vs the value's start/end span from :meth:`port_sample_marks`).

        Decoded ON DEMAND for just this window (no full-capture collection / re-decode):
        the C++ core restores the nearest transport-phase checkpoint recorded during the
        initial decode and ticks the schedule across the window (see
        Decoder::bitSamplesInWindow) — so enabling the overlay and navigating are cheap
        and every region (however far into the capture) resolves. Returns parallel arrays
        `sample, device, dp, channel, is_start`; empty when the window holds more than
        `max_marks` bits (too dense — the caller hides them and lets the user zoom in)."""
        empty = {"sample": np.zeros(0, np.int64), "device": np.zeros(0, np.int32),
                 "dp": np.zeros(0, np.int32), "channel": np.zeros(0, np.int32),
                 "is_start": np.zeros(0, np.uint8)}
        fn = getattr(self.decoder, "bit_samples_window", None)
        if not callable(fn):
            return empty
        window = self._port_bit_window_uis(lo_sample, hi_sample)
        if window is None:
            return empty
        b = fn(window[0], window[1])
        s = np.asarray(b["sample"], dtype=np.int64)
        if s.size == 0 or s.size > max_marks:
            return empty
        # Bits come out in tick (ascending-UI) order == ascending sample; sort defensively
        # so multi-port interleaving at a shared UI stays sample-ordered for the overlay.
        order = np.argsort(s, kind="stable")
        return {"sample": s[order],
                "device": np.asarray(b["device"], dtype=np.int32)[order],
                "dp": np.asarray(b["dp"], dtype=np.int32)[order],
                "channel": np.asarray(b["channel"], dtype=np.int32)[order],
                "is_start": np.asarray(b["is_start"], dtype=np.uint8)[order]}

    def _symbol_start_ui(self, desired: int, origin: int, cols: int) -> int:
        """Snap a desired symbol-decode start UI to a Column-0 boundary. decode_symbols
        counts columns from `start_ui` (treating it as Column 0) and reads the CDS at
        `cds_column`, so start_ui MUST land on Column 0 — otherwise the column counter is
        phase-shifted and it samples the wrong column (for DLV, `origin` is the CDS column
        at Column 2, so `origin + k*cols` would sit on Column 2, making it read Sync1 as the
        CDS). Column 0 of `origin`'s row is `origin - cds`; snap down to that lattice. For
        the shared DLV source the seek path needs start_ui > 0, so bump a Column-0-of-row-0
        result to the next row (row 0 carries only Sync/CDS framing — no data lost)."""
        if cols <= 0 or origin >= (2 ** 64 - 1):
            return max(0, int(desired))
        col0 = int(origin) - int(self._cds_horizontal_start)      # Column-0 UI (== origin for FBCSE)
        s = int(desired) - ((int(desired) - col0) % cols)         # snap down to a Column-0 boundary
        if self.is_dlv and s <= 0:
            s = col0 + cols                                       # row 1's Column 0 (seek path needs > 0)
        return max(0, s)

    def symbols(self, max_symbols: int = 1024, start_ui: int = 0) -> List[dict]:
        """Re-decode the CDS into classified 8b/10b symbols (fresh source).

        With no `start_ui`, decodes from the FIRST segment using that segment's
        column count (not the final one) so the start of a multi-config capture
        frames correctly. Rows match the streaming decoder via the segment's
        Column-0 origin + cumulative row base.
        """
        seg = self.segments[0] if self.segments else None
        cols = seg["column_count"] if seg else self.column_count
        origin = seg["start_ui"] if seg else (2 ** 64 - 1)
        base = seg["row_base"] if seg else 0
        su = self._symbol_start_ui(start_ui or (seg["start_ui"] if seg else 0), origin, cols)
        src, dlv, cds = self._symbol_source()
        return swi3score.decode_symbols(src, cols, max_symbols, su, origin, base,
                                        0, dlv, cds)

    def symbols_around(self, sample: int, max_symbols: int = 1024) -> List[dict]:
        """Symbols at/just before the Column-0 row containing `sample`, framed with
        the column count of the segment that covers `sample` and numbered on the
        same continuous bus-row scale as the command table.

        The window starts a small margin BEFORE the target row: the 8b/10b decoder
        must re-acquire on a comma after the seek, and a command's SPM comma sits
        ~10 rows before the start_sample the parser reports — without the backup
        the window would skip past this command's comma to the next one's.

        The decode is capped at the NEXT segment's Column-0 UI (`end_ui`) so a seek
        into the tail of a segment can't run this segment's framing into the next
        region's different width (which would send the comma hunt racing forward and
        report runaway row numbers)."""
        seg = self._segment_for_sample(sample)
        if seg is None:
            return self.symbols(max_symbols)
        cols = max(2, seg["column_count"])
        origin = seg["start_ui"]
        target = self._ui_for_sample(sample)
        # Back up a large margin (about half the window) so selecting a command JUMPS
        # to its comma but keeps the earlier symbols in the buffer — the pane scrolls
        # the selection to the top, and the user can still scroll BACK in time rather
        # than the window filtering everything before the command away. (The full
        # capture isn't decoded; the window just centres the target instead of starting
        # a few rows before it.)
        BACKUP_ROWS = max(24, max_symbols // 2)
        k = max(0, (target - origin) // cols - BACKUP_ROWS)
        start_ui = self._symbol_start_ui(origin + k * cols, origin, cols)
        # Cap at the start of the following segment (0 = no cap for the last one).
        after = [int(s["start_ui"]) for s in self.segments if int(s["start_ui"]) > origin]
        end_ui = min(after) if after else 0
        src, dlv, cds = self._symbol_source()
        return swi3score.decode_symbols(src, cols, max_symbols, start_ui, origin,
                                        seg["row_base"], end_ui, dlv, cds)

    def symbols_before(self, sample: int, max_symbols: int = 512) -> List[dict]:
        """The nearest chunk of CDS symbols BEFORE the Column-0 row containing `sample`
        — for decode-on-scroll back-history in the symbol pane. Decoded with the framing
        of the section covering that region (on the continuous bus-row scale), so repeated
        calls walk backward chunk by chunk, crossing config-section boundaries (each with
        its own column count). Returns [] only at the capture start (nothing earlier).

        A window of ROWS ending at the anchor is walked backward until it decodes to
        symbols. Realistic captures idle the CDS between the Manager's periodic keep-alive
        Pings ({ASW2601}), so the rows immediately before a Ping can be pure idle with no
        symbols — that is NOT the start, so skip the idle gap and keep looking rather than
        dead-ending. The window spans several Ping intervals, so the previous symbols are
        normally found in one step."""
        anchor = int(sample)
        target = self._ui_for_sample(anchor)
        if target <= 1 or not self.segments:
            return []
        # Rows per window: wide enough to comfortably span an idle Ping interval so a
        # single step lands on the previous symbols in the common case (~10 rows/symbol
        # when the CDS is dense, so max_symbols*10 also yields ~max_symbols there).
        win_rows = max(4096, max_symbols * 10)
        idx = self.segments.index(self._segment_for_sample(max(0, anchor - 1))
                                  or self.segments[0])
        end_ui = target
        guard = 0
        while idx >= 0 and guard < 4096:
            guard += 1
            seg = self.segments[idx]
            cols = max(2, seg["column_count"])
            origin = seg["start_ui"]
            rows_end = (end_ui - origin) // cols        # window end, rows from origin
            if rows_end <= 0:                           # window is before this segment
                idx -= 1
                end_ui = origin
                continue
            k = max(0, rows_end - win_rows)
            start_ui = self._symbol_start_ui(origin + k * cols, origin, cols)
            src, dlv, cds = self._symbol_source()
            syms = swi3score.decode_symbols(src, cols, max_symbols + 64, start_ui, origin,
                                            seg["row_base"], end_ui, dlv, cds)   # end-cap at window end
            syms = [s for s in syms if int(s.get("start_sample", 0)) < anchor]
            if syms:
                return syms
            # Idle window: step it further back (into the previous section at the origin).
            if k > 0:
                end_ui = start_ui
            else:
                idx -= 1
                end_ui = origin
        return []

    def register_files_at(self, sample: Optional[int]) -> Dict[int, DeviceRegisterFile]:
        """Per-device register state as-of `sample` (None = whole capture).

        Register VALUES and provenance come from the SAME C++ decode authority the
        grid and audio use (swi3score.registers_from_commands), so there is one notion
        of the decoded config — not a parallel Python replay. Writes/commits, bus reads
        (READ provenance), and section-scoped what-if overrides (UI provenance) are all
        folded into that one model; reads reveal live values in the map without
        perturbing the grid/audio config."""
        if sample is None:
            return self.register_files
        # The register replay is precomputed once (in effective order) and sliced by
        # bisect per cursor move — mirroring the grid path (_grid_replay_in_effect) —
        # instead of rebuilding the whole replay from the in-effect commands every move.
        reg_replay, reg_eff = self._reg_replay_sorted()
        replay = reg_replay[:bisect.bisect_right(reg_eff, int(sample))]
        sect = self.segment_index_for_sample(int(sample))
        return self._build_register_files_from_replay(replay, sect)

    @staticmethod
    def _reg_replay_entry(c):
        """The C++ register-model replay dict for one config command — a write, a commit,
        or a read (reads reveal live values without perturbing the grid/audio config) — or
        None if the command isn't register-relevant."""
        if c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"):
            return {"is_write": True, "device_mask": int(c.get("device_mask", 0)),
                    "address": int(c.get("address", 0)), "data": bytes(c.get("data", b""))}
        if c.get("is_commit"):
            return {"is_commit": True, "commit_confirmed": bool(c.get("commit_confirmed")),
                    "group_mask": int(c.get("group_mask", 0))}
        if (c.get("has_read_data") and c.get("read_data_crc_valid") and c.get("has_address")):
            return {"is_read": True, "device_mask": int(c.get("device_mask", 0)),
                    "address": int(c.get("address", 0)),
                    "data": bytes(c.get("read_data") or b"")}
        return None

    def _reg_replay_sorted(self):
        """(reg_replay, reg_eff): the per-command register-replay dicts in effective-sample
        order, with their parallel effective samples, cached — the register-view counterpart
        of _effective_sorted's grid replay. register_files_at bisects reg_eff and slices
        reg_replay, so a cursor move is a slice not an O(prefix) rebuild. Invalidated on
        re-decode (alongside _eff_sorted)."""
        cache = getattr(self, "_reg_replay_cache", None)
        if cache is None:
            eff, cmds, _r, _re = self._effective_sorted()
            reg_replay, reg_eff = [], []
            for e, c in zip(eff, cmds):
                entry = self._reg_replay_entry(c)
                if entry is not None:
                    reg_replay.append(entry)
                    reg_eff.append(e)
            cache = (reg_replay, reg_eff)
            self._reg_replay_cache = cache
        return cache

    def _build_register_files(self, cmds, section: Optional[int]
                              ) -> Dict[int, DeviceRegisterFile]:
        """Build per-device files for an arbitrary config-command list (whole-capture
        init / re-decode). Builds the replay then delegates; the per-cursor
        register_files_at uses the cached sliced replay via _build_register_files_from_replay."""
        replay = [r for c in cmds if (r := self._reg_replay_entry(c)) is not None]
        return self._build_register_files_from_replay(replay, section)

    def _build_register_files_from_replay(self, replay, section: Optional[int]
                                          ) -> Dict[int, DeviceRegisterFile]:
        """Build per-device files from the C++ register authority for a prebuilt `replay`
        (write/commit/read dicts). Register VALUES and their provenance (written vs read)
        come from the one C++ model — writes/commits, reads, and the section's what-if
        overrides all folded in there (reads reveal live values in the map without
        perturbing the grid/audio config). `section` = the config-section index to
        scope overrides to (None = whole capture: no overrides)."""
        overrides = [(int(d), int(a), int(v)) for (s, d, a), v in self.register_overrides.items()
                     if section is not None and s == section]
        # Map each override to the (device, base) it forces and the RANK it edits, so we
        # colour ONLY that rank UI: a _NEXT edit doesn't touch _CURR and vice-versa. The
        # C++ snapshot is keyed by the _NEXT-base address; a _CURR alias maps down to it.
        ov_next, ov_curr = set(), set()
        for d, a, _ in overrides:
            res = self.register_map.resolve(a)
            if res is not None and res.register.dual_ranked and res.rank == "CURR":
                sp = res.register
                delta = (sp.curr_offset - sp.offset) if sp.curr_offset is not None else 0x40
                ov_curr.add((d, a - delta))
            elif res is not None and res.register.dual_ranked:
                ov_next.add((d, a))                       # _NEXT-base rank
            else:
                ov_curr.add((d, a))                       # single-ranked: its one bank is _CURR
        override_bases = ov_next | ov_curr
        snap = swi3score.registers_from_commands(replay, register_overrides=overrides)

        files: Dict[int, DeviceRegisterFile] = {}

        def file_for(dev: int) -> DeviceRegisterFile:
            f = files.get(dev)
            if f is None:
                f = files[dev] = DeviceRegisterFile(self.register_map, dev,
                                                    self.peripheral_maps.get(dev))
            return f

        # C++ Source tag -> display Provenance; a what-if override wins as UI.
        src_prov = {1: Provenance.WRITTEN, 2: Provenance.READ}
        # CSV-import baseline (Provenance.CSV): seed UNDERNEATH the snooped writes/reads
        # so a post-commit capture (nothing on the wire) shows the CSV config, while any
        # real bus write/read re-seeds the same address below and wins.
        for dev, addr, cur, has_cur, _cs, nxt, has_next, _ns in self._csv_snap:
            if (dev, addr) in override_bases:
                continue
            file_for(dev).seed(addr, cur, bool(has_cur), Provenance.CSV,
                               nxt, bool(has_next), Provenance.CSV)
        for dev, addr, cur, has_cur, cur_src, nxt, has_next, next_src in snap:
            cur_p = (Provenance.UI if (dev, addr) in ov_curr
                     else src_prov.get(int(cur_src), Provenance.WRITTEN))
            next_p = (Provenance.UI if (dev, addr) in ov_next
                      else src_prov.get(int(next_src), Provenance.WRITTEN))
            file_for(dev).seed(addr, cur, bool(has_cur), cur_p, nxt, bool(has_next), next_p)
        return files

    def set_register_override(self, section_index: int, device: int, address: int,
                              value: int) -> None:
        """Debug what-if: force (device, address) to `value` for config section
        `section_index`. Re-decodes so the Bus Grid, Register Map AND audio all
        reflect it (Provenance.UI in the map). Section-scoped — see register_overrides."""
        self.register_overrides[(int(section_index), int(device), int(address))] = int(value) & 0xFF
        self._redecode()

    # ---- Bus Grid label fields (pure display; no decode effect) ----
    #: Visualizer default: Channel|Bit (DisplayField.CHANNEL | DisplayField.BIT).
    DEFAULT_LABEL_FIELDS = 6

    @staticmethod
    def _load_label_fields(raw) -> Dict[Tuple[int, int], int]:
        """Parse persisted label fields. Keys round-trip through JSON as "dev.dp"
        strings (a JSON object can't hold a tuple key); unparseable entries are
        dropped rather than raising — a hand-edited or older workspace must still
        open, just without that port's preference."""
        out: Dict[Tuple[int, int], int] = {}
        for k, v in dict(raw or {}).items():
            try:
                dev, dp = str(k).split(".")
                out[(int(dev), int(dp))] = int(v) & 7
            except (ValueError, TypeError):
                continue
        return out

    def set_label_fields(self, device: int, dp: int, fields: int) -> None:
        """Set the Bus Grid label fields for one data port (1=Sample, 2=Channel,
        4=Bit, OR-ed). Display only — the caller re-renders; there is no re-decode."""
        self.label_fields[(int(device), int(dp))] = int(fields) & 7
        self._store_label_fields()

    def label_fields_for(self, device: int, dp: int) -> int:
        """Label fields for a port, falling back to the Visualizer default."""
        return int(self.label_fields.get((int(device), int(dp)),
                                         self.DEFAULT_LABEL_FIELDS))

    def dp_display_map(self) -> Dict[Tuple[int, int], dict]:
        """`dp_display` for GridView.set_cells — {(device, dp): {"display_fields": n}}.
        Only ports that differ from the default are stored, so an untouched session
        passes an empty map and the renderer keeps its own default."""
        return {k: {"display_fields": v} for k, v in self.label_fields.items()}

    def _store_label_fields(self) -> None:
        """Mirror label fields into the source descriptor (workspace persistence).
        Entries equal to the default are dropped so a workspace only records real
        choices — and so changing the default later doesn't leave ports pinned to
        the old one."""
        keep = {f"{d}.{p}": int(v) for (d, p), v in sorted(self.label_fields.items())
                if int(v) != self.DEFAULT_LABEL_FIELDS}
        if keep:
            self.source["label_fields"] = keep
        else:
            self.source.pop("label_fields", None)

    def clear_register_overrides(self) -> None:
        if not self.register_overrides:
            return
        self.register_overrides.clear()
        self._redecode()

    # ---- region-scoped forced column count ----
    def force_column_count_at(self, sample: Optional[int], columns: int) -> int:
        """Pin the bus column count for the config region containing `sample`, and
        re-decode. `columns` 0 removes the pin. Returns the section index affected.

        Why per-region: a config CSV knows a geometry the wire may never have carried
        (a post-commit capture has no NumColumns commit to snoop), and the blind column
        detector can mis-lock. But a capture that genuinely reconfigures mid-stream has
        several real widths, so forcing one over the whole capture misframes the others
        (their commands go CRC-red). Pinning just the region under the cursor fixes the
        region being looked at and leaves the rest decoding as the wire says.

        The section index is resolved from the CURRENT segments, before the re-decode:
        a pin changes framing *within* a region, but re-detected framing can also move
        which regions exist, and re-deriving the index afterwards would let the pin walk
        to a different region on each call."""
        sect = self.segment_index_for_sample(int(sample)) if sample is not None else 0
        cols = int(columns)
        if cols and not (2 <= cols <= 32):
            raise ValueError(f"column count {cols} out of range (2..32)")
        if cols:
            if self.forced_column_sections.get(sect) == cols:
                return sect                      # unchanged -> skip the full re-decode
            self.forced_column_sections[sect] = cols
        else:
            if sect not in self.forced_column_sections:
                return sect
            del self.forced_column_sections[sect]
        self._store_forced_columns()
        self._redecode()
        return sect

    def clear_forced_column_sections(self) -> None:
        """Remove every region column-count pin and re-decode."""
        if not self.forced_column_sections:
            return
        self.forced_column_sections.clear()
        self._store_forced_columns()
        self._redecode()

    def _store_forced_columns(self) -> None:
        """Mirror the pins into the source descriptor so they survive a workspace
        reopen (present when any, removed when none) — see __init__, which reads them
        back before the first decode."""
        if self.forced_column_sections:
            self.source["forced_column_sections"] = {
                str(k): int(v) for k, v in sorted(self.forced_column_sections.items())}
        else:
            self.source.pop("forced_column_sections", None)

    def forced_column_count_at(self, sample: Optional[int]) -> int:
        """The pinned column count for the region containing `sample`, or 0 if that
        region isn't pinned. Drives the timeline's 'forced' region badge."""
        if sample is None:
            return 0
        return int(self.forced_column_sections.get(
            self.segment_index_for_sample(int(sample)), 0))


    def set_register_overrides(self, overrides) -> None:
        """Bulk-replace the section-scoped what-if register overrides
        ({(section, device, address): value}) and re-decode once. Used to restore a
        saved workspace's overlay in a single decode. See set_register_override."""
        new = {(int(s), int(d), int(a)): int(v) & 0xFF
               for (s, d, a), v in dict(overrides or {}).items()}
        if new == self.register_overrides:
            return                               # unchanged -> skip the full re-decode
        self.register_overrides = new
        self._redecode()

    def set_peripheral_map(self, device: int, pmap: Optional[object], *,
                           rebuild: bool = True) -> None:
        """Bind (or clear, with pmap=None) a device's vendor register map and
        rebuild the register files so device-defined addresses resolve against it.
        Pass rebuild=False to only stash the map when a re-decode (which rebuilds the
        files itself) is about to run — avoids a redundant whole-capture build."""
        if pmap is None:
            self.peripheral_maps.pop(device, None)
        else:
            self.peripheral_maps[device] = pmap
        if rebuild:
            self.register_files = self._build_register_files(self._config_commands(), None)

    def _config_commands(self):
        """The config-relevant commands only — CRC-valid WriteA32, every Commit, and
        CRC-valid read-backs — in decode order. Cached: the cursor-driven grid and
        register views filter this small list each tick instead of scanning the
        millions of Ping/audio commands. Kept in decode order (not sorted) so the
        replay/apply order matches apply_commands exactly."""
        rel = getattr(self, "_rel_cmds", None)
        if rel is None:
            rel = [c for c in self.commands
                   if (c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"))
                   or c.get("is_commit")
                   or (c.get("has_read_data") and c.get("read_data_crc_valid") and c.get("has_address"))]
            self._rel_cmds = rel
        return rel

    @property
    def sample_rate_hz(self) -> int:
        return self.capture.sample_rate_hz

    def sample_to_seconds(self, sample: int) -> float:
        return sample / self.sample_rate_hz if self.sample_rate_hz else 0.0
