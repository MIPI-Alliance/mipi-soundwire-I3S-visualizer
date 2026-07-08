"""A decoded analysis session: capture -> decode -> commands, audio, register
files, and bus-grid layout. Keeps the C++ Decoder alive so the grid can be
re-queried, and rebuilds register state as-of a time cursor.
"""
from __future__ import annotations

import bisect
import os

from typing import Dict, List, Optional, Tuple

import numpy as np

from .nputil import searchsorted as _searchsorted
import swi3score

from .ingest.capture import Capture
from .ingest import transitions, saleae_binary
from .model import RegisterMap, DeviceRegisterFile, Provenance
from .analysis.link_control import decode_link_control, LinkControlResult


# ABI the Python side expects from the swi3score native module (see bindings.cpp
# `score_abi`). Bump both together when a binding's return shape changes.
_REQUIRED_SCORE_ABI = 2


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
                 source: Optional[dict] = None, ssp_row: int = -1):
        _require_score_abi()                          # clear error if the .so is stale
        self.capture = capture
        self.source = source or {"type": "capture"}   # workspace descriptor
        # Per-(device, dp) scrambler override: {(device, dp): bool}.
        self.scrambler_overrides = dict(scrambler_overrides or {})
        # Per-device hub depth {device: 0..5}: a peripheral N hubs deep has its
        # response delayed 2*N frame rows; fed to the decoder so responses align.
        self.hub_depths = {int(d): int(v) for d, v in dict(hub_depths or {}).items()}
        # Per-device display names {device: str} (UI metadata; no decode effect).
        self.device_names = {int(d): str(v) for d, v in dict(device_names or {}).items()}
        self._register_map_arg = register_map
        self._source = capture.sample_source()       # keep alive for the decoder
        settings = swi3score.DecoderSettings()
        settings.forced_column_count = int(forced_column_count)
        self._forced_column_count = int(forced_column_count)
        settings.decode_audio = bool(decode_audio)
        self._decode_audio = bool(decode_audio)
        # Per-data-bit sample points (Raw Capture "Port Samples" overlay) are heavy
        # to collect + index on a dense capture, so they are OFF by default and
        # collected lazily on first use via set_collect_bit_samples() (a re-decode).
        self._collect_bits = False
        settings.collect_bit_samples = self._collect_bits
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
        settings.ssp_row = self._ssp_row
        settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                        for (d, p), on in self.scrambler_overrides.items()]
        settings.hub_depth_overrides = [(d, v) for d, v in self.hub_depths.items() if v]
        self.decoder = swi3score.Decoder(self._source, settings)
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
        self._link_control: Optional[LinkControlResult] = None

    # ---- audio (columnar) ----
    _AUDIO_FIELDS = ("device", "dp", "channel", "sample_size", "value",
                     "index", "start_sample", "end_sample")
    _AUDIO_DTYPES = {"device": np.int32, "dp": np.int32, "channel": np.int32,
                     "sample_size": np.int32, "value": np.uint32,
                     "index": np.uint64, "start_sample": np.uint64,
                     "end_sample": np.uint64}

    def _build_audio_columns(self) -> Dict[str, np.ndarray]:
        """Audio as a dict of parallel NumPy arrays. Uses the core's fast
        `audio_columns()` when present; otherwise derives the columns from the
        per-sample `audio()` list (older core — correct, just not as fast)."""
        cols_fn = getattr(self.decoder, "audio_columns", None)
        if callable(cols_fn):
            return cols_fn()
        audio = self.decoder.audio()
        n = len(audio)
        get = {"device": lambda a: a.get("device", -1)}
        return {f: np.fromiter((get.get(f, lambda a, k=f: a[k])(a) for a in audio),
                               self._AUDIO_DTYPES[f], n)
                for f in self._AUDIO_FIELDS}

    def _refresh_audio(self) -> None:
        self._audio_cols = self._build_audio_columns()
        self._audio_list: Optional[List[dict]] = None      # lazy dict-list cache
        self._mark_cache = None                            # lazy per-value read-mark index
        self._bit_cache = None                             # lazy per-bit sample-point index
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

    @property
    def has_bit_samples(self) -> bool:
        """True once per-data-bit sample points have been collected (they're lazy —
        the Raw Capture "Port Samples" overlay needs them; nothing else does)."""
        return self._collect_bits

    def set_collect_bit_samples(self, on: bool = True) -> None:
        """Enable (or disable) per-data-bit sample-point collection and re-decode.
        Off by default; the Raw Capture overlay calls this on first use so a normal
        load isn't burdened with indexing tens of millions of bit points."""
        on = bool(on)
        if on == self._collect_bits:
            return
        self._collect_bits = on
        self._redecode()

    def _redecode(self) -> None:
        """Rebuild the decoder from the current settings + overrides and re-run."""
        self._source = self.capture.sample_source()
        settings = swi3score.DecoderSettings()
        settings.forced_column_count = self._forced_column_count
        settings.decode_audio = self._decode_audio
        settings.collect_bit_samples = self._collect_bits
        settings.config_csv_path = self._config_csv
        settings.ssp_row = self._ssp_row
        settings.scrambler_overrides = [(int(d), int(p), 1 if on else 0)
                                        for (d, p), on in self.scrambler_overrides.items()]
        settings.hub_depth_overrides = [(d, v) for d, v in self.hub_depths.items() if v]
        settings.register_overrides = [(int(sect), int(d), int(a), int(v))
                                       for (sect, d, a), v in self.register_overrides.items()]
        self.decoder = swi3score.Decoder(self._source, settings)
        self.decoder.run()
        self.commands = self.decoder.commands()
        self._rel_cmds = None                    # invalidate config-command cache
        self._eff_sorted = None                  # invalidate effective-sample sort cache
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
        self._redecode()

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
                  cold_start: bool = False, **kw) -> "Session":
        return cls(transitions.demo_capture(audio_samples_per_channel,
                                            cold_start=cold_start),
                   source={"type": "demo",
                           "audio_samples_per_channel": int(audio_samples_per_channel),
                           "cold_start": bool(cold_start)},
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
                 sample_rate_hz: int = 0, auto_clock: bool = False, **kw) -> "Session":
        from .ingest import saleae_sal
        cap = saleae_sal.load_capture(path, clock_channel, data_channel,
                                      sample_rate_hz or None, auto_clock=auto_clock)
        return cls(cap, source={"type": "sal", "path": path,
                                "clock_channel": int(clock_channel),
                                "data_channel": int(data_channel),
                                "auto_clock": bool(auto_clock),
                                "sample_rate_hz": int(cap.sample_rate_hz)}, **kw)

    @classmethod
    def from_digital_csv(cls, path: str, clock_col: int, data_col: int,
                         sample_rate_hz: int = 0, **kw) -> "Session":
        from .ingest import digital_csv
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

    # ---- queries ----
    def grid_cells(self, rows: int = 32) -> List[dict]:
        if self._csv_replay:
            # A config CSV is imposed (apply_config_csv): draw the grid from the CSV
            # baseline + any wire config so it matches the CSV, rather than the
            # decoder's snoop-only grid (empty for a post-commit capture).
            replay = self._csv_replay + self._grid_replay(self._config_commands())
            return swi3score.grid_from_commands(replay, rows, force_columns=self.column_count)
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
        return swi3score.grid_from_commands(replay, rows, force_columns=force,
                                            register_overrides=overrides)

    def _tx_geometry(self):
        """(origin_ui, columns) the TX map draws at — the OPERATIONAL geometry, not
        segments[0]. On a cold start segment 0 is the narrow slow-clock preamble (e.g.
        2 col); pick the segment matching the decoder's authoritative column_count (the
        audio config), else the widest, so the raster and its scrollbar agree. Shared by
        tx_raster and tx_total_rows."""
        seg = None
        if self.segments:
            seg = next((s for s in self.segments
                        if int(s.get("column_count", 0)) == int(self.column_count)), None)
            if seg is None:
                seg = max(self.segments, key=lambda s: int(s.get("column_count", 0)))
        origin = int(seg["start_ui"]) if seg else 0
        cols = max(2, int(seg["column_count"]) if seg else self.column_count)
        return origin, cols

    def tx_total_rows(self) -> int:
        """Total 0-based bus rows the TX map can scroll through (the operational
        segment's first row to the end of the capture). Used to size the TX-map
        scrollbar."""
        ce = self.capture.clock_edges
        if ce.size < 2:
            return 0
        origin, cols = self._tx_geometry()
        # Ceil, not floor: a capture that ends mid-row still has that trailing partial
        # row (tx_raster clips the missing columns via its valid mask), so it must stay
        # reachable by the scrollbar rather than being dropped.
        avail = max(0, int(ce.size) - origin)
        return max(1, (avail + cols - 1) // cols)

    def tx_row_for_sample(self, sample: int) -> int:
        """0-based TX-map row containing `sample`, in the SAME geometry tx_raster
        draws at — so the Bus Grid's Show Toggles can open at (and scroll to) the
        cursor's row. Clamped to [0, tx_total_rows-1]."""
        origin, cols = self._tx_geometry()
        ui = self._ui_for_sample(int(sample))
        row = (ui - origin) // cols if cols else 0
        return int(max(0, min(row, self.tx_total_rows() - 1)))

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
        out: List[dict] = []
        for i, s in enumerate(self.segments):
            su = int(s["start_ui"])
            cols = max(1, int(s["column_count"]))
            end_ui = int(self.segments[i + 1]["start_ui"]) if i + 1 < len(self.segments) else ce.size
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
                "column_count": cols, "edges": int(end_ui - su),
                "mid_sample": (start_s + end_s) // 2,
            })
        return out

    def tx_raster(self, start_row: int = 0, rows: int = 64) -> dict:
        """Physical data-line TRANSITION raster for the TX map — a where-is-real-data
        inspection that needs NO config CSV.

        For `rows` capture rows starting at 0-based `start_row` (row 0 = the segment's
        first complete row), and each column of the segment's geometry, report whether
        that UI carried a data transition — a data edge within the UI's clock window.
        This is raw payload activity (NOT the CDS's NRZS bit): idle/DC columns stay
        blank, columns actually transporting toggle, and an interval/skipping port
        shows as row-periodic gaps. `start_row` lets the view scroll the whole capture
        a window at a time (see tx_total_rows).

        Column 0 is the CDS column (flagged separately). Returns {column_count, cds_col,
        row_labels (0-based, len == nrows), tx (nrows x ncols bool ndarray)}. Column
        geometry is the auto-detected/segment count, so it works pre-CSV."""
        ce = self.capture.clock_edges
        de = self.capture.data_edges
        # Draw at the OPERATIONAL geometry (not segments[0], which on a cold start is
        # the narrow slow-clock preamble) — shared with tx_total_rows so the raster and
        # its scrollbar agree.
        origin, cols = self._tx_geometry()
        nrows = max(1, int(rows))
        start_row = max(0, int(start_row))
        labels = [start_row + i for i in range(nrows)]
        base = {"column_count": cols, "cds_col": 0, "row_labels": labels}
        if ce.size < 2:
            return {**base, "tx": np.zeros((nrows, cols), dtype=bool)}
        # UI index of (window row i, col c): row 0's Column 0 is at `origin` (start_ui),
        # so capture row (start_row + i) column c is origin + (start_row + i)*cols + c.
        r = np.arange(nrows, dtype=np.int64)[:, None]
        c = np.arange(cols, dtype=np.int64)[None, :]
        ui = origin + (start_row + r) * cols + c                 # (nrows, cols)
        nui = ce.size
        valid = (ui >= 1) & (ui < nui)
        uic = np.clip(ui, 1, nui - 1)
        # A UI carries a transition iff its sampled bit differs from the previous UI's —
        # i.e. a data edge in (clock_edge[ui-1], clock_edge[ui]]. This matches the
        # decoder's own convention (CColumnDetector: level[i] != level[i-1]); attributing
        # the *next* interval's edge here would shift every column one left (lighting the
        # guard columns). Index preserves ce's dtype so searchsorted stays same-dtype.
        tx = (np.searchsorted(de, ce[uic]) - np.searchsorted(de, ce[uic - 1])) > 0
        return {**base, "tx": tx & valid}

    def tx_persist_columns(self, sample: Optional[int] = None) -> "tuple[np.ndarray, int]":
        """(per-column ever-toggled boolean, column_count) for the CONFIGURATION REGION
        containing `sample` — the TX-map persistence summary. Scoped to one config
        region (a segment), NOT the whole capture: a capture can reconfigure mid-stream
        (e.g. 2col preamble -> 16col audio) and those geometries don't overlay. Scans
        every UI in that region so a column that only transports deeper in the region
        still lights up. `sample=None` uses the operational region. Column 0 is CDS."""
        ce = self.capture.clock_edges
        de = self.capture.data_edges
        # Region bounds in UI space: the covering segment's start_ui to the next
        # segment's start_ui (or end of capture), in that segment's own column count.
        seg = self._segment_for_sample(int(sample)) if sample is not None else None
        if seg is not None:
            origin = int(seg["start_ui"])
            cols = max(2, int(seg["column_count"]))
            nxt = [int(s["start_ui"]) for s in self.segments if int(s["start_ui"]) > origin]
            end_ui = min(nxt) if nxt else int(ce.size)
        else:
            origin, cols = self._tx_geometry()
            end_ui = int(ce.size)
        lit = np.zeros(cols, dtype=bool)
        lo = max(1, origin)
        if ce.size < 2 or end_ui <= lo:
            return lit, cols
        ui = np.arange(lo, end_ui, dtype=np.int64)
        # A UI toggled iff its sampled bit differs from the previous UI (same convention
        # as tx_raster). OR each column across the whole region.
        toggled = (np.searchsorted(de, ce[ui]) - np.searchsorted(de, ce[ui - 1])) > 0
        col = (ui - origin) % cols
        np.logical_or.at(lit, col[toggled], True)
        return lit, cols

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
        """Sample to place the shared cursor at when command `cmd` is selected. Every
        row uses its own start sample — including a commit COMMAND, which sits at the
        command (so the grid / register map show the PRE-commit state there). The
        deferred effect is a separate 'Commit Point' row placed at the SSP (its
        start_sample already IS the effective sample), so selecting THAT shows the
        post-commit state. (Previously a commit command jumped the cursor to its SSP,
        which made the commit look like it took effect at the command.)"""
        return int(cmd.get("start_sample", 0))

    def audio_store(self, mmap_dir=None, pdm_dc_block=False):
        from .store.audio_store import AudioStore
        return AudioStore.from_session(self, mmap_dir=mmap_dir, pdm_dc_block=pdm_dc_block)

    def _segment_for_sample(self, sample: int) -> Optional[dict]:
        """The decode segment (column count + row base) covering `sample`. A
        multi-config capture (e.g. 2col -> 8col) has one segment per column count;
        the symbol viewer must use the right one or it mis-frames the region."""
        if not self.segments:
            return None
        seg = self.segments[0]
        for s in self.segments:
            if s["start_sample"] <= sample:
                seg = s
            else:
                break
        return seg

    def segment_index_for_sample(self, sample: int) -> int:
        """Index (in segments()) of the config section covering `sample` — the key
        for section-scoped what-if register overrides. 0 when there are no segments."""
        idx = 0
        for i, s in enumerate(self.segments or []):
            if s["start_sample"] <= sample:
                idx = i
            else:
                break
        return idx

    def source_label(self) -> str:
        """A short human name for the loaded capture (for the window title / UI),
        e.g. the .sal/.csv/.bin file's basename, or 'Demo capture'. A two-file Saleae
        binary shows BOTH channel files (clock & data)."""
        s = self.source or {}
        t = s.get("type")
        if t == "demo":
            return "Demo capture"
        clock, data = s.get("clock"), s.get("data")
        if clock and data:
            return f"{os.path.basename(str(clock))} & {os.path.basename(str(data))}"
        for key in ("path", "clock", "data"):
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
        for i, s in enumerate(segs):
            lo = starts[i]
            hi = starts[i + 1] if i + 1 < len(starts) else (int(clk[-1]) + 1 if clk.size else lo)
            a = clk[(clk >= lo) & (clk < hi)]
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
        """Absolute source UI index for `sample` — exact via the clock-edge list
        (UI index == clock-edge index), so it's immune to the per-UI period
        changing or a long idle before the clock starts."""
        return int(_searchsorted(self.capture.clock_edges, max(0, int(sample))))

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

        Returns an empty array when the window would hold more than `max_marks`
        CDS columns — too dense to be legible, so the caller hides the marks and
        lets the user zoom in.
        """
        ce = self.capture.clock_edges
        if ce.size == 0:
            return np.zeros(0, dtype=np.uint64)
        segs = self.segments or [{"start_ui": 0,
                                  "column_count": max(2, self.column_count)}]
        lo_ui = int(_searchsorted(ce, max(0, int(lo_sample)), side="left"))
        hi_ui = min(int(_searchsorted(ce, int(hi_sample), side="right")), ce.size)
        marks: List[int] = []
        for i, seg in enumerate(segs):
            origin = int(seg["start_ui"])
            cols = max(1, int(seg["column_count"]))
            seg_end = int(segs[i + 1]["start_ui"]) if i + 1 < len(segs) else ce.size
            end_ui = min(seg_end, hi_ui)
            if end_ui <= origin or end_ui <= lo_ui:
                continue
            first = max(origin, lo_ui)
            k0 = (first - origin + cols - 1) // cols     # first Column-0 UI >= window start
            ui = origin + k0 * cols
            while ui < end_ui:
                rsp = ui - 1                             # RSP = rising edge that opens the CDS UI
                if rsp >= 0:
                    marks.append(rsp)
                    if len(marks) > max_marks:           # too dense — hide (caller zooms in)
                        return np.zeros(0, dtype=np.uint64)
                ui += cols
        if not marks:
            return np.zeros(0, dtype=np.uint64)
        return np.ascontiguousarray(ce[np.asarray(marks, dtype=np.int64)])

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
        ce = self.capture.clock_edges
        if ce.size == 0:
            return np.zeros(0, dtype=np.uint64)
        lo, hi = int(lo_sample), int(hi_sample)
        edges = []
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
                s = int(ce[rsp])
                if lo <= s <= hi:
                    edges.append(rsp)
        if not edges:
            return np.zeros(0, dtype=np.uint64)
        return np.ascontiguousarray(ce[np.asarray(sorted(set(edges)), dtype=np.int64)])

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
        (UI - start_ui) // column_count for the segment that covers it."""
        seg = self._segment_for_sample(int(sample))
        if seg is None:
            return 0
        cols = max(1, int(seg["column_count"]))
        ui = self._ui_for_sample(int(sample))
        return int(seg["row_base"]) + (ui - int(seg["start_ui"])) // cols

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
        ui = _searchsorted(self.capture.clock_edges, np.maximum(s, 0)).astype(np.int64)
        return base[seg_idx] + (ui - origin[seg_idx]) // cols[seg_idx]

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
        m = self._port_marks_sorted()
        st = m["start"]
        if st.size == 0:
            return []
        signed_all = m["signed"]                    # precomputed (see _port_marks_sorted)
        if not ports and not channels and not value_pred:
            # No filter — the common case on every cursor move. Window directly around
            # the cursor with two bisects; skip the O(N) full-table mask + flatnonzero.
            c = int(np.searchsorted(st, int(sample)))
            sel = np.arange(max(0, c - half_window), min(st.size, c + half_window))
        else:
            keep = np.ones(st.size, dtype=bool)
            if ports:
                want = {(int(d), int(p)) for d, p in ports}
                key = (m["device"].astype(np.int64) << 16) | (m["dp"].astype(np.int64) & 0xFFFF)
                wantkey = np.array(sorted((d << 16) | (p & 0xFFFF) for d, p in want), dtype=np.int64)
                keep &= np.isin(key, wantkey)
            if channels:
                want_ch = np.array(sorted({int(c) for c in channels}), dtype=np.int64)
                keep &= np.isin(m["channel"].astype(np.int64), want_ch)
            if value_pred:
                op, thr = value_pred
                thr = int(thr)
                ops = {"<=": signed_all <= thr, "<": signed_all < thr, "==": signed_all == thr,
                       "!=": signed_all != thr, ">=": signed_all >= thr, ">": signed_all > thr}
                if op in ops:
                    keep &= ops[op]
            idx = np.flatnonzero(keep)
            if idx.size == 0:
                return []
            # Window the kept samples around the cursor (in the kept-index space).
            c = int(np.searchsorted(st[idx], int(sample)))
            sel = idx[max(0, c - half_window):min(idx.size, c + half_window)]
        if sel.size == 0:
            return []
        starts = st[sel]
        rows = self.bus_rows_for_samples(starts)
        signed = signed_all[sel]
        dev, dp, ch = m["device"][sel], m["dp"][sel], m["channel"][sel]
        val, ss, ix = m["value"][sel], m["sample_size"][sel], m["index"][sel]
        return [{"device": int(dev[i]), "dp": int(dp[i]), "channel": int(ch[i]),
                 "value": int(val[i]), "sample_size": int(ss[i]), "index": int(ix[i]),
                 "start_sample": int(starts[i]), "row": int(rows[i]),
                 "signed": int(signed[i])}
                for i in range(sel.size)]

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

    def _port_bits_sorted(self) -> Dict[str, np.ndarray]:
        """Per-data-bit sample points (sample, device, dp, channel, is_start) sorted
        by sample — the index behind :meth:`port_bit_marks`. Built lazily from the
        decoder's bit_samples() (collected because collect_bit_samples is set) and
        invalidated on re-decode."""
        if self._bit_cache is None:
            b = self.decoder.bit_samples()
            s = np.asarray(b["sample"], dtype=np.int64)
            order = np.argsort(s, kind="stable")
            self._bit_cache = {
                "sample": s[order],
                "device": np.asarray(b["device"], dtype=np.int32)[order],
                "dp": np.asarray(b["dp"], dtype=np.int32)[order],
                "channel": np.asarray(b["channel"], dtype=np.int32)[order],
                "is_start": np.asarray(b["is_start"], dtype=np.uint8)[order],
            }
        return self._bit_cache

    def port_bit_marks(self, lo_sample: int, hi_sample: int,
                       max_marks: int = 8000) -> Dict[str, np.ndarray]:
        """Per-data-bit sample points for the active data ports within
        [lo_sample, hi_sample].

        Each record is the exact clock edge at which one data bit was latched, with
        the (device, dp, channel) that read it and `is_start` set on the MSB — the
        bit that begins that sample's interval. This is what shows *which* edges are
        bit samples (vs the value's start/end span from :meth:`port_sample_marks`).
        Returns parallel arrays `sample, device, dp, channel, is_start`; empty when
        the window holds more than `max_marks` bits (too dense — zoom in)."""
        empty = {"sample": np.zeros(0, np.int64), "device": np.zeros(0, np.int32),
                 "dp": np.zeros(0, np.int32), "channel": np.zeros(0, np.int32),
                 "is_start": np.zeros(0, np.uint8)}
        m = self._port_bits_sorted()
        s = m["sample"]
        if s.size == 0:
            return empty
        i0 = int(np.searchsorted(s, int(lo_sample), side="left"))
        i1 = int(np.searchsorted(s, int(hi_sample), side="right"))
        if i1 - i0 <= 0 or i1 - i0 > max_marks:
            return empty
        sl = slice(i0, i1)
        return {k: v[sl] for k, v in m.items()}

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
        su = start_ui or (seg["start_ui"] if seg else 0)
        return swi3score.decode_symbols(self.capture.sample_source(), cols,
                                        max_symbols, su, origin, base)

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
        start_ui = origin + k * cols
        # Cap at the start of the following segment (0 = no cap for the last one).
        after = [int(s["start_ui"]) for s in self.segments if int(s["start_ui"]) > origin]
        end_ui = min(after) if after else 0
        return swi3score.decode_symbols(self.capture.sample_source(), cols,
                                        max_symbols, start_ui, origin, seg["row_base"],
                                        end_ui)

    def symbols_before(self, sample: int, max_symbols: int = 512) -> List[dict]:
        """The chunk of CDS symbols immediately BEFORE the Column-0 row containing
        `sample` — for decode-on-scroll back-history in the symbol pane. Decoded with
        the framing of the section that covers the region just before `sample` (on the
        continuous bus-row scale), so repeated calls walk backward chunk by chunk,
        crossing config-section boundaries (each with its own column count). Returns []
        at the capture start (nothing earlier to show)."""
        target = self._ui_for_sample(int(sample))
        if target <= 1 or not self.segments:
            return []
        seg = self._segment_for_sample(max(0, int(sample) - 1)) or self.segments[0]
        cols = max(2, seg["column_count"])
        origin = seg["start_ui"]
        rows_before = (target - origin) // cols
        if rows_before <= 0:
            # `sample` sits at/before this section's Column 0 — step into the previous
            # section and show its tail, so back-scroll crosses the boundary.
            idx = self.segments.index(seg)
            if idx <= 0:
                return []
            seg = self.segments[idx - 1]
            cols = max(2, seg["column_count"])
            origin = seg["start_ui"]
            rows_before = (target - origin) // cols
            if rows_before <= 0:
                return []
        k = max(0, rows_before - max_symbols)
        start_ui = origin + k * cols
        syms = swi3score.decode_symbols(self.capture.sample_source(), cols,
                                        max_symbols + 64, start_ui, origin,
                                        seg["row_base"], target)     # end-cap at the anchor
        # Keep only symbols strictly before the anchor sample (the end-cap already
        # stops near it; this trims any that land at/after).
        return [s for s in syms if int(s.get("start_sample", 0)) < int(sample)]

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
        cmds = self._commands_in_effect(sample)   # config commands, EFFECT order, SSP-aware
        sect = self.segment_index_for_sample(int(sample))
        return self._build_register_files(cmds, sect)

    def _build_register_files(self, cmds, section: Optional[int]
                              ) -> Dict[int, DeviceRegisterFile]:
        """Build per-device files from the C++ register authority for `cmds` (a
        config-command list). Register VALUES and their provenance (written vs read)
        come from the one C++ model — writes/commits, reads, and the section's what-if
        overrides all folded in there (reads reveal live values in the map without
        perturbing the grid/audio config). `section` = the config-section index to
        scope overrides to (None = whole capture: no overrides). Reused by
        register_files_at and the whole-capture register_files."""
        replay = []
        for c in cmds:
            if c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"):
                replay.append({"is_write": True, "device_mask": int(c.get("device_mask", 0)),
                               "address": int(c.get("address", 0)), "data": bytes(c.get("data", b""))})
            elif c.get("is_commit"):
                replay.append({"is_commit": True,
                               "commit_confirmed": bool(c.get("commit_confirmed")),
                               "group_mask": int(c.get("group_mask", 0))})
            elif (c.get("has_read_data") and c.get("read_data_crc_valid")
                  and c.get("has_address")):
                replay.append({"is_read": True, "device_mask": int(c.get("device_mask", 0)),
                               "address": int(c.get("address", 0)),
                               "data": bytes(c.get("read_data") or b"")})
        overrides = [(int(d), int(a), int(v)) for (s, d, a), v in self.register_overrides.items()
                     if section is not None and s == section]
        override_addrs = {(d, a) for d, a, _ in overrides}
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
            if (dev, addr) in override_addrs:
                continue
            file_for(dev).seed(addr, cur, bool(has_cur), Provenance.CSV,
                               nxt, bool(has_next), Provenance.CSV)
        for dev, addr, cur, has_cur, cur_src, nxt, has_next, next_src in snap:
            if (dev, addr) in override_addrs:
                cur_p = next_p = Provenance.UI
            else:
                cur_p = src_prov.get(int(cur_src), Provenance.WRITTEN)
                next_p = src_prov.get(int(next_src), Provenance.WRITTEN)
            file_for(dev).seed(addr, cur, bool(has_cur), cur_p, nxt, bool(has_next), next_p)
        return files

    def set_register_override(self, section_index: int, device: int, address: int,
                              value: int) -> None:
        """Debug what-if: force (device, address) to `value` for config section
        `section_index`. Re-decodes so the Bus Grid, Register Map AND audio all
        reflect it (Provenance.UI in the map). Section-scoped — see register_overrides."""
        self.register_overrides[(int(section_index), int(device), int(address))] = int(value) & 0xFF
        self._redecode()

    def clear_register_overrides(self) -> None:
        if not self.register_overrides:
            return
        self.register_overrides.clear()
        self._redecode()

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
