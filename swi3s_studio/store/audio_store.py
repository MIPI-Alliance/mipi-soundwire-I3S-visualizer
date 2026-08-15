"""Audio results store + WAV export.

Organizes decoded audio samples into per-(dataport, channel) signed arrays and
writes one interleaved multichannel WAV per dataport (channels ascending, gaps
zero-filled). Multi-bit sample values come off the wire as unsigned bit patterns
of `sample_size` bits and are sign-extended (two's-complement) for playback. A
1-bit `sample_size` is a PDM density stream, NOT a 1-bit two's-complement sample;
it is decoded to PCM at build time (map {0,1}->±1 and band-limit + decimate, see
decode_pdm). The per-dataport sample rate comes from the C++ core's verified
SampleRateHz (and is replaced by the decimated rate for PDM streams).

(The min/max render pyramid for very large captures plugs in here; for now the
viewer relies on pyqtgraph downsampling.)
"""
from __future__ import annotations

import os
import wave
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..nputil import searchsorted as _ss


def sign_extend(values: np.ndarray, bits: int) -> np.ndarray:
    """Interpret an array of unsigned `bits`-wide values as two's complement.

    A 1-bit `bits` is a PDM density code, not a 1-bit two's-complement sample (that
    would map {0,1}->{0,-1}); it maps bipolar to {-1,+1}. PDM streams are normally
    decoded via decode_pdm; this keeps a direct caller correct for bits<=1 too."""
    v = np.asarray(values, dtype=np.int64)
    if bits <= 1:
        return v * 2 - 1                      # {0,1} -> {-1,+1}
    half = 1 << (bits - 1)
    return np.where(v >= half, v - (1 << bits), v)


def container_bits(sample_bits: int) -> int:
    """Smallest standard signed-PCM container that holds `sample_bits`."""
    if sample_bits <= 16:
        return 16
    if sample_bits <= 24:
        return 24
    return 32


_PDM_PCM_RATE = 48000   # 1-bit PDM streams are decimated to this PCM rate at build


def decode_pdm(bits: np.ndarray, native_rate: float,
               target_rate: int = _PDM_PCM_RATE, dc_block: bool = False):
    """Decode a 1-bit PDM density stream to signed PCM (int16 full-scale range).

    PDM is a bipolar *density* code, not a two's-complement sample: {0,1}->{-1,+1},
    and the audio is the local average, recovered by band-limited decimation
    (low-pass below the output Nyquist, then downsample — see dsp.pdm_to_pcm).

    `dc_block` (default False) subtracts the mean so a real mic's large density bias
    (a density well off 50%) doesn't swamp a quiet tone — what a hardware PDM decoder
    does for listening. It is OFF by default so the analyzer shows the TRUE density
    on the wire: an all-ones stream reads full-scale +1 (DC), all-zeros −1, 50% ~0.
    Turn it on (Audio ▸ Block PDM DC Bias) to centre a real mic capture. Note a
    constant/DC pattern decodes to ~0 when blocked (its mean is the whole signal).

    Returns (pcm: int64 in int16 range, out_rate_hz, M) where M is the integer
    decimation factor (input samples per output sample). When the native rate is
    unknown or already <= target, decimation isn't possible and the ±1 mapping is
    returned at unit amplitude (M = 1) so it stays sign-correct and quiet, not a
    full-scale square-wave blast."""
    native = float(native_rate)
    if native <= target_rate:
        return np.asarray(bits, dtype=np.int64) * 2 - 1, native, 1
    from ..dsp import pdm_to_pcm
    M = max(2, int(round(native / target_rate)))
    out_rate = native / M
    pcm = pdm_to_pcm(bits, native, out_rate)              # float ~[-1, 1], DC = density bias
    if dc_block and pcm.size:
        pcm = pcm - pcm.mean()                            # optional: remove the mic density bias
    signed = np.clip(np.rint(pcm * 32767.0), -32768, 32767).astype(np.int64)
    return signed, out_rate, M


_RATE_RECOVERY_SUBSAMPLE = 4096   # samples per probe window (bounds memory)


def _recover_pdm_bit_rate(sample_at: np.ndarray, capture_rate_hz: float) -> float:
    """Recover a PDM port's native bit rate from its own per-bit capture-sample
    anchors, for use when the core reported no verified rate for the port.

    `sample_at` is non-decreasing and, under standard SampleGrouping/Spacing, its
    consecutive diffs are BIMODAL: small gaps between samples grouped onto the same
    row and one large gap to the next active row (see CDpConfig::computeSampleRate
    — rate = (SampleGrouping+1) * rowRate / (Interval+1)). np.median(np.diff(...))
    picks the small intra-row gap and overestimates the bit rate by several times,
    which drives the wrong decimation factor M in decode_pdm (wrong pitch/speed).

    The AVERAGE spacing over a whole run of rows — (last - first) / (count - 1) —
    is exact regardless of how the samples clump within a row, because it just
    divides the true elapsed capture-sample span by the number of steps; unlike a
    per-diff statistic, it isn't fooled by which gap size is more numerous. A single
    probe is still vulnerable to a reconfig gap (the port going silent mid-capture
    stretches one span disproportionately), so take a few short probes — head, mid,
    tail — and use their MEDIAN so one distorted probe can't dominate.

    Each probe only touches a small, strided slice (bounded by
    `_RATE_RECOVERY_SUBSAMPLE`), so this stays O(1)-ish memory even on a
    100M-sample channel — no full-array np.diff / np.median."""
    n = sample_at.size
    if n < 2 or capture_rate_hz <= 0.0:
        return 0.0
    span = min(_RATE_RECOVERY_SUBSAMPLE, n)
    starts = sorted({0, max(0, (n - span) // 2), max(0, n - span)})
    steps = []
    for s0 in starts:
        probe = sample_at[s0:s0 + span]
        if probe.size < 2:
            continue
        elapsed = float(probe[-1]) - float(probe[0])
        if elapsed > 0.0:
            steps.append(elapsed / (probe.size - 1))
    if not steps:
        return 0.0
    return capture_rate_hz / float(np.median(steps))


# --- min/max render pyramid -------------------------------------------------
# Multi-resolution min/max so pan/zoom renders from pre-binned envelopes and
# never scans the full per-channel array — the technique pro waveform viewers use
# to stay smooth over very long captures.
_PYRAMID_FACTOR = 8
_PYRAMID_MIN_LEN = 1024


def _build_pyramid(raw: np.ndarray) -> List[Tuple[int, np.ndarray, np.ndarray]]:
    levels: List[Tuple[int, np.ndarray, np.ndarray]] = [(1, raw, raw)]
    mn = np.asarray(raw)
    mx = mn
    factor = 1
    while len(mn) > _PYRAMID_MIN_LEN:
        factor *= _PYRAMID_FACTOR

        def reduce(a: np.ndarray, op: str) -> np.ndarray:
            pad = (-len(a)) % _PYRAMID_FACTOR
            if pad:
                a = np.concatenate([a, np.full(pad, a[-1] if len(a) else 0, dtype=a.dtype)])
            return getattr(a.reshape(-1, _PYRAMID_FACTOR), op)(axis=1)

        mn = reduce(mn, "min")
        mx = reduce(mx, "max")
        levels.append((factor, mn, mx))
    return levels



@dataclass
class AudioStore:
    # Keyed by (device, dp, channel). Two devices may each expose a DP0, so the
    # device is part of the key everywhere.
    _channels: Dict[Tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    _sample_bits: Dict[Tuple[int, int, int], int] = field(default_factory=dict)
    # On-bus sample size in bits (before PDM decode promotes 1-bit → 16-bit PCM).
    # Used for the true DP bandwidth; _sample_bits holds the stored/PCM width.
    _native_bits: Dict[Tuple[int, int, int], int] = field(default_factory=dict)
    _rates: Dict[Tuple[int, int], float] = field(default_factory=dict)   # (device, dp) -> Hz
    # Native (pre-decimation) per-DP sample rate. `_rates` is overwritten with the
    # decimated PCM rate for 1-bit/PDM streams (for playback/render); this keeps the
    # true on-bus rate for display + bandwidth, which must not show the 48 kHz
    # playback decimation.
    _native_rates: Dict[Tuple[int, int], float] = field(default_factory=dict)
    _pyramids: Dict[Tuple[int, int, int], list] = field(default_factory=dict)
    _sample_at: Dict[Tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    # Memoised transport_gaps() per channel — see _gaps_cached().
    _gap_cache: Dict[Tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    # TRUE per-index transport positions for flow-controlled channels, captured before
    # the de-jitter ramp overwrites _sample_at. Only populated for flow_mode != 0;
    # transport_gaps() falls back to _sample_at for everything else. See from_audio_columns.
    _transport_at: Dict[Tuple[int, int, int], np.ndarray] = field(default_factory=dict)

    @classmethod
    def from_session(cls, session, mmap_dir: Optional[str] = None,
                     pdm_dc_block: bool = False) -> "AudioStore":
        return cls.from_audio_columns(session.audio_columns(),
                                      session.decoder.audio_sample_rates(),
                                      mmap_dir=mmap_dir, pdm_dc_block=pdm_dc_block,
                                      capture_rate_hz=float(getattr(session, "sample_rate_hz", 0.0)))

    @classmethod
    def from_audio(cls, audio: List[dict], rates: Dict[Tuple[int, int], float],
                   mmap_dir: Optional[str] = None,
                   pdm_dc_block: bool = False,
                   capture_rate_hz: float = 0.0) -> "AudioStore":
        """Build from the list-of-dicts form (decoder.audio()). Kept for callers
        and tests that have the dict list; it just packs the columns and delegates
        to :meth:`from_audio_columns`, so the grouping is the same vectorised path.
        Pass `capture_rate_hz` to get the same flow-control de-jitter rate as the
        columnar path (else positions de-jitter but the reported rate is unchanged)."""
        n = len(audio)
        if n == 0:
            return cls(_rates=dict(rates), _native_rates=dict(rates))
        cols = {
            "device": np.fromiter((a.get("device", -1) for a in audio), np.int32, n),
            "dp": np.fromiter((a["dp"] for a in audio), np.int32, n),
            "channel": np.fromiter((a["channel"] for a in audio), np.int32, n),
            "sample_size": np.fromiter((a["sample_size"] for a in audio), np.int32, n),
            "value": np.fromiter((a["value"] for a in audio), np.uint32, n),
            "index": np.fromiter((a["index"] for a in audio), np.uint64, n),
            "start_sample": np.fromiter((a["start_sample"] for a in audio), np.uint64, n),
            # flow_mode (ABI 7) drives the de-jitter in from_audio_columns; default 0
            # (NORMAL) if a pre-ABI-7 dict list lacks it, so this path matches the
            # columnar one instead of silently skipping de-jitter for flow-controlled ports.
            "flow_mode": np.fromiter((a.get("flow_mode", 0) for a in audio), np.int32, n),
        }
        return cls.from_audio_columns(cols, rates, mmap_dir=mmap_dir,
                                      pdm_dc_block=pdm_dc_block,
                                      capture_rate_hz=capture_rate_hz)

    @classmethod
    def from_audio_columns(cls, cols: Dict[str, np.ndarray],
                           rates: Dict[Tuple[int, int], float],
                           mmap_dir: Optional[str] = None,
                           pdm_dc_block: bool = False,
                           capture_rate_hz: float = 0.0) -> "AudioStore":
        """Build from columnar (struct-of-arrays) audio — the fast path for large
        captures. `cols` carries parallel arrays: device, dp, channel, sample_size,
        value, index, start_sample (from ``Decoder.audio_columns()``). Grouping into
        per-(device, dp, channel) dense arrays is done with NumPy (one lexsort +
        slicing), not per-sample Python dicts, so 50M+ samples stay manageable.
        `capture_rate_hz` lets a PDM stream recover its bit rate from its own sample
        spacing when the core reports no verified rate (else it can't be decimated)."""
        store = cls(_rates=dict(rates), _native_rates=dict(rates))
        native_rates = dict(rates)   # immutable snapshot: store._rates is overwritten
        #                              with the decimated rate for PDM dataports below,
        #                              but every channel must decimate from the SAME
        #                              original bit rate (not a sibling's decimated one).
        device = np.asarray(cols["device"])
        dp = np.asarray(cols["dp"])
        channel = np.asarray(cols["channel"])
        value = np.asarray(cols["value"])
        sample_size = np.asarray(cols["sample_size"])
        start_sample = np.asarray(cols["start_sample"]).astype(np.int64, copy=False)
        # Per-sample flow mode (0 NORMAL, 1 TX / 2 RX / 3 ASYNC). Flow-controlled ports
        # transport on a jittered schedule, so their samples arrive at irregular
        # capture times; the display de-jitters them to the measured average rate (see
        # the loop below). Absent on pre-ABI-7 cores -> treat everything as NORMAL.
        flow_mode = cols.get("flow_mode")
        flow_mode = np.asarray(flow_mode) if flow_mode is not None else None
        total = device.shape[0]
        if total == 0:
            return store
        # One lexical sort groups all samples by (device, dp, channel) AND orders each
        # group by capture sample in a single pass: start_sample is the primary
        # (first-listed) lexsort key, so a run of equal (device, dp, channel) is one
        # contiguous, already-time-sorted slice — no separate per-channel argsort.
        # (Ordering by start_sample, not the core's per-(dp,channel) `index`, is
        # deliberate: that counter RESETS to 0 on every bus reconfiguration, so equal
        # index values recur once per config epoch; a dense[index]=value placement kept
        # only the LAST epoch and dropped earlier ones. start_sample is globally
        # monotonic, so this stitches all epochs into one correct, time-ordered channel.
        # No index gap zero-fill: the waveform is drawn in capture-sample space, so real
        # gaps read as gaps, not silence.)
        order = np.lexsort((start_sample, channel, dp, device))
        sdev, sdp, sch = device[order], dp[order], channel[order]
        changed = (np.diff(sdev) != 0) | (np.diff(sdp) != 0) | (np.diff(sch) != 0)
        cuts = np.flatnonzero(changed) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [total]))
        for s, e in zip(starts.tolist(), ends.tolist()):
            rows = order[s:e]                     # already ordered by start_sample
            r0 = rows[0]
            key = (int(device[r0]), int(dp[r0]), int(channel[r0]))
            devp = (int(device[r0]), int(dp[r0]))
            bits = int(sample_size[r0])
            dense_val = value[rows].astype(np.int64, copy=False)
            dense_sat = start_sample[rows].astype(np.int64, copy=False)   # already non-decreasing
            n = dense_val.size

            if bits == 1:
                # 1-bit PDM density stream: decode to PCM here so the rest of the
                # app (display/cursor/playback/export) sees a uniform signed-PCM
                # channel. Decimation collapses the bit stream toward ~48 kHz and
                # rebases the per-sample capture anchors onto the decimated grid.
                devp = (int(device[r0]), int(dp[r0]))
                native = native_rates.get(devp, 0.0)
                if native <= 0.0 and capture_rate_hz > 0.0 and n > 1:
                    # The core reported no verified rate for this port (e.g. a capture
                    # with errors, or no config). Recover the PDM bit rate from the bits'
                    # own capture-sample spacing so it can still be decimated to PCM
                    # instead of collapsing to a ±1 (near-silent) stream — see
                    # _recover_pdm_bit_rate for why a plain median-of-diffs is wrong
                    # under SampleGrouping/Spacing.
                    native = _recover_pdm_bit_rate(dense_sat, capture_rate_hz)
                    if native > 0.0:
                        # Feeds display/bandwidth (native_rate()) with the recovered
                        # on-bus rate instead of the original 0; matches how a
                        # core-verified rate is exposed for every other port.
                        store._native_rates[devp] = native
                signed, out_rate, M = decode_pdm(dense_val, native, dc_block=pdm_dc_block)
                sbits = 16
                if M > 1 and signed.size:
                    ratio = n / signed.size
                    sat_idx = np.clip(np.rint(np.arange(signed.size) * ratio),
                                      0, n - 1).astype(np.int64)
                    dense_sat = dense_sat[sat_idx]
                    store._rates[devp] = float(out_rate)
            else:
                signed = sign_extend(dense_val, bits)
                sbits = bits

            # Flow-controlled ports (TX/RX/ASYNC) transport on a jittered schedule, so
            # dense_sat lands the samples at irregular capture times and a bit-exact sine
            # would render visibly distorted. A real receiver de-jitters via its FIFO,
            # clocking the samples out at the long-term AVERAGE input rate. Mirror that:
            # replace the jittered anchors with a uniform ramp across the same delivered
            # span (first->last transport), and report that measured average as the rate.
            # The intended source rate is unknown/arbitrary in general, so it is measured,
            # not assumed. NORMAL (flow_mode 0) is already uniform and left untouched; the
            # per-interval transport jitter remains visible in the Samples/grid view.
            fm = int(flow_mode[r0]) if flow_mode is not None else 0
            if fm != 0 and dense_sat.size > 1:
                # Gap detection must read the TRUE transport positions, so stash them
                # before the ramp replaces them. The de-jitter below makes the anchors
                # perfectly uniform by construction, which erases the very step a
                # dropout consists of — transport_gaps() would then find nothing on any
                # flow-controlled port, exactly where the waveform most needs breaking.
                store._transport_at[key] = np.ascontiguousarray(dense_sat)
                # Use the CURRENT anchor count: a PDM branch above already decimated
                # dense_sat to signed.size, so `n` (the pre-decimation bit count) would
                # mismatch the value array and corrupt _sample_at. m == signed.size here.
                m = dense_sat.size
                span = int(dense_sat[-1]) - int(dense_sat[0])
                if span > 0:
                    dense_sat = (int(dense_sat[0])
                                 + np.rint(np.arange(m) * (span / (m - 1)))).astype(np.int64)
                    if capture_rate_hz > 0.0:
                        avg = (m - 1) / span * capture_rate_hz
                        # Update BOTH the display rate and the native (pre-decimation)
                        # rate: native_rate() feeds the plot title + Statistics bandwidth,
                        # so leaving it at the 96 kHz opportunity rate would keep showing
                        # the inflated rate the de-jitter is meant to remove.
                        store._rates[devp] = avg
                        store._native_rates[devp] = avg

            store._sample_at[key] = dense_sat
            if mmap_dir is not None:
                dev, p, ch = key
                path = os.path.join(mmap_dir, f"dev{dev}_dp{p}_ch{ch}.i32")
                mm = np.memmap(path, dtype=np.int32, mode="w+", shape=(max(signed.size, 1),))
                mm[:signed.size] = signed.astype(np.int32)
                mm.flush()
                arr = mm
            else:
                arr = signed
            store._channels[key] = arr
            store._sample_bits[key] = sbits
            store._native_bits[key] = bits          # on-bus size (1 for PDM), for bandwidth
            # Render pyramid is built LAZILY on first envelope() — most channels are
            # never viewed, so building all of them at load is wasted work + memory on
            # wide (mic-array) captures.
        return store

    # ---- queries ----
    def _stream_index(self) -> Dict[Tuple[int, int], List[int]]:
        """(device, dp) -> sorted channel list, built once from the (immutable-after-build)
        channel keys and cached. streams()/channels() are called in nested loops at several
        sites; scanning every channel key per call was O(streams x keys) — quadratic on wide
        (mic-array) captures."""
        idx = getattr(self, "_stream_idx", None)
        if idx is None:
            idx = {}
            for (dev, dp, ch) in self._channels:
                idx.setdefault((dev, dp), []).append(ch)
            for chans in idx.values():
                chans.sort()
            self._stream_idx = idx
        return idx

    def streams(self) -> List[Tuple[int, int]]:
        """Sorted (device, dp) pairs that carry audio."""
        return sorted(self._stream_index().keys())

    def channels(self, device: int, dp: int) -> List[int]:
        return list(self._stream_index().get((device, dp), ()))     # already sorted

    def samples(self, device: int, dp: int, channel: int) -> np.ndarray:
        return self._channels.get((device, dp, channel), np.zeros(0, dtype=np.int64))

    def sample_bits(self, device: int, dp: int, channel: int) -> int:
        return self._sample_bits.get((device, dp, channel), 16)

    def native_sample_bits(self, device: int, dp: int, channel: int) -> int:
        """On-bus sample size in bits (1 for a PDM stream), for the DP bandwidth.
        Falls back to the stored width when no native size was recorded."""
        key = (device, dp, channel)
        return self._native_bits.get(key) or self._sample_bits.get(key, 16)

    def rate(self, device: int, dp: int) -> float:
        return self._rates.get((device, dp), 0.0)

    def native_rate(self, device: int, dp: int) -> float:
        """The on-bus sample rate (Hz) before any playback decimation — what to
        show as the stream's sample rate and use for its bandwidth. Falls back to
        rate() for streams that were never decimated."""
        return self._native_rates.get((device, dp)) or self._rates.get((device, dp), 0.0)

    def is_empty(self) -> bool:
        return not self._channels

    def close(self) -> None:
        """Release any memmap file handles so the backing files can be deleted.
        On Windows an open memmap locks its file (the dir can't be removed until
        every handle is closed); POSIX unlinks an open file happily, so this is a
        no-op there in practice. Only the mmap_dir-backed path holds handles — the
        in-memory path stores plain ndarrays. Safe to call repeatedly; the store's
        sample arrays must not be used afterward. Also usable as a context manager."""
        self._pyramids.clear()                # drop derived views before closing the mmaps
        for arr in self._channels.values():
            mm = getattr(arr, "_mmap", None)  # np.memmap exposes the mmap; plain ndarray -> None
            if mm is not None:
                mm.close()
        self._channels.clear()

    def __enter__(self) -> "AudioStore":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def _pyramid(self, key):
        """The render pyramid for a channel, built + memoised on first use (lazy)."""
        lv = self._pyramids.get(key)
        if lv is None:
            arr = self._channels.get(key)
            lv = _build_pyramid(np.asarray(arr)) if arr is not None and len(arr) else []
            self._pyramids[key] = lv
        return lv

    def envelope(self, device: int, dp: int, channel: int, start: int, stop: int,
                 max_points: int = 2048):
        """Min/max envelope of [start, stop) for a channel, decimated to about
        `max_points` columns using the render pyramid — O(points), not O(samples).

        Bins that straddle a transport gap are SPLIT. The pyramid is built over the
        contiguous sample-index array, which knows nothing about capture positions,
        so one decimated bin can aggregate the silence before a dropout together
        with the loud audio after it — and the resulting [min,max] bar renders at
        the pre-gap position as a jump from zero that no decoded sample contains
        (reported on dev6 dp2 at 103,667,003.52 us, where the last decoded sample
        of the region is 0). Splitting recomputes the two sides from the raw samples
        so each is exact, and anchors them at the region's true last/first index.
        There is at most one straddling bin per gap, so the extra work is negligible.
        """
        key = (device, dp, channel)
        levels = self._pyramid(key)
        if not levels:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        n = len(self._channels[key])
        start = max(0, min(start, n))
        stop = max(start, min(stop, n))
        span = stop - start
        if span <= 0:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        target = max(1, span // max(1, max_points))
        factor, lo_arr, hi_arr = levels[0]
        for f, mn, mx in levels:
            if f <= target:
                factor, lo_arr, hi_arr = f, mn, mx
            else:
                break
        i0 = start // factor
        i1 = (stop + factor - 1) // factor
        lo = np.asarray(lo_arr[i0:i1], dtype=np.int64)
        hi = np.asarray(hi_arr[i0:i1], dtype=np.int64)
        x = (np.arange(len(lo)) + i0) * factor + factor // 2
        if factor > 1:
            x, lo, hi = self._split_gap_bins(key, x, lo, hi, i0, factor, n)
        return x, lo, hi

    def _split_gap_bins(self, key, x, lo, hi, i0: int, factor: int, n: int):
        """Split any bin that spans a transport gap into its two true halves.

        `factor == 1` bins hold a single sample and can never straddle, so the
        caller skips this entirely at full zoom — which is also why the artifact
        only appears when zoomed out far enough to decimate.
        """
        gaps = self._gaps_cached(key)
        if gaps.size == 0:
            return x, lo, hi
        samples = self._channels.get(key)
        if samples is None or len(samples) == 0:
            return x, lo, hi
        bin_start = (np.arange(len(lo)) + i0) * factor
        bin_stop = np.minimum(bin_start + factor, n)
        # A gap at index g means "nothing decoded between g and g+1", so a bin
        # straddles it when it holds both g and g+1.
        pos = np.searchsorted(bin_start, gaps, side="right") - 1
        out_x, out_lo, out_hi = [], [], []
        cut: Dict[int, list] = {}
        for g, j in zip(gaps.tolist(), pos.tolist()):
            if 0 <= j < len(lo) and bin_start[j] <= g < bin_stop[j] - 1:
                cut.setdefault(j, []).append(g)
        if not cut:
            return x, lo, hi
        for j in range(len(lo)):
            gs = cut.get(j)
            if not gs:
                out_x.append(int(x[j])); out_lo.append(int(lo[j])); out_hi.append(int(hi[j]))
                continue
            # Walk the sub-ranges between consecutive gaps inside this bin.
            edges = [int(bin_start[j])] + [g + 1 for g in gs] + [int(bin_stop[j])]
            for a, b in zip(edges[:-1], edges[1:]):
                if b <= a:
                    continue
                seg = np.asarray(samples[a:b], dtype=np.int64)
                if seg.size == 0:
                    continue
                # Anchor each side at the index the eye should see it end/begin on:
                # the last real sample before the gap, the first one after it.
                anchor = (b - 1) if (b - 1) in gs else a
                out_x.append(int(anchor))
                out_lo.append(int(seg.min()))
                out_hi.append(int(seg.max()))
        return (np.asarray(out_x, dtype=x.dtype),
                np.asarray(out_lo, dtype=np.int64),
                np.asarray(out_hi, dtype=np.int64))

    def _gaps_cached(self, key):
        """Memoised transport_gaps: envelope() runs on every pan/zoom tick, and the
        underlying diff+median over a multi-million-sample position array is far too
        expensive to repeat per frame. Gaps are a property of the decoded data, so
        the answer never changes for a given channel."""
        g = self._gap_cache.get(key)
        if g is None:
            g = self.transport_gaps(key[0], key[1], key[2])
            self._gap_cache[key] = g
        return g


    def pyramid_levels(self, device: int, dp: int, channel: int) -> int:
        return len(self._pyramid((device, dp, channel)))

    def index_range_for_samples(self, device: int, dp: int, channel: int,
                                start_sample: int, stop_sample: int) -> Tuple[int, int]:
        """Map a capture-sample window to the [i0, i1) sample-index slice."""
        sat = self._sample_at.get((device, dp, channel))
        if sat is None or sat.size == 0:
            return 0, 0
        i0 = int(_ss(sat, start_sample, side="left"))
        i1 = int(_ss(sat, stop_sample, side="right"))
        return i0, i1

    def sample_at_index(self, device: int, dp: int, channel: int,
                        index: int) -> Optional[int]:
        """Capture sample where audio sample `index` of this channel began (the
        inverse of index_range_for_samples), clamped to range. None if empty."""
        sat = self._sample_at.get((device, dp, channel))
        if sat is None or sat.size == 0:
            return None
        i = max(0, min(int(index), sat.size - 1))
        return int(sat[i])

    def sample_positions(self, device: int, dp: int, channel: int):
        """The per-audio-index → capture-sample map for a channel (the array behind
        sample_at_index), or None. Lets a caller plot in capture-sample space by
        indexing this directly instead of calling sample_at_index() per point."""
        return self._sample_at.get((device, dp, channel))

    def transport_gaps(self, device: int, dp: int, channel: int,
                       tolerance: float = 4.0) -> np.ndarray:
        """Audio-sample indices `i` where NO samples were decoded between index `i`
        and `i+1` — i.e. the port stopped transporting and later resumed.

        Audio indices are contiguous across such a hole (index n+1 follows index n
        even if it arrived 30 s later), so only the CAPTURE positions reveal it: a
        gap is a step far larger than the stream's own cadence. A renderer must break
        its polyline at these indices, otherwise the two active regions are joined by
        a straight line implying audio that was never decoded.

        Reads `_transport_at` when present — the TRUE transport positions of a
        flow-controlled channel, stashed before the de-jitter ramp replaced
        `_sample_at` with a uniform one. Reading the de-jittered array would find
        nothing on any flow-controlled port: the ramp is uniform BY CONSTRUCTION, so
        it erases the very step a dropout consists of.

        `tolerance` is the multiple of the channel's nominal per-sample step that
        still counts as continuous. The nominal step is the 25th PERCENTILE diff, not
        the median: the median is only robust while gaps are a minority, and inverts
        once they are ~half the steps (an intermittent, bursty port), at which point
        it reports the GAP width as "nominal" and detects nothing. The lower quartile
        keeps tracking the active cadence well past that point. The default 4x clears
        ordinary flow-control jitter (a TX/RX/ASYNC port legitimately skips transport
        opportunities, giving 2x steps) while still catching a real dropout, which is
        orders of magnitude larger — not a marginal call.

        Returns the indices as an int64 array (empty when the channel is absent,
        shorter than 2 samples, or gap-free)."""
        key = (device, dp, channel)
        sat = self._transport_at.get(key)
        if sat is None:
            sat = self._sample_at.get(key)
        if sat is None or sat.size < 2:
            return np.zeros(0, dtype=np.int64)
        d = np.diff(np.asarray(sat, dtype=np.int64))
        nominal = float(np.percentile(d, 25))
        if not np.isfinite(nominal) or nominal <= 0.0:
            return np.zeros(0, dtype=np.int64)
        return np.flatnonzero(d > nominal * float(tolerance)).astype(np.int64)

    def pcm_play(self, device: int, dp: int, channels: Optional[List[int]] = None,
                 start: int = 0, stop: Optional[int] = None, depth: int = 16,
                 target_rate: Optional[int] = None):
        """Interleaved PCM for in-app playback at the requested bit `depth`
        (16 or 24). Returns (bytes, sample_rate_hz, n_channels, qt_format) where
        qt_format is the QAudioFormat.SampleFormat name to use ("Int16" or
        "Int32"). 16-bit packs to int16; 24-bit scales to a full-scale int32
        container (Qt has no Int24 format) so 24-bit sources keep their fidelity.
        Each channel is scaled from its own bit width so all play at comparable
        loudness; `start`/`stop` window by sample index. `target_rate` (Hz)
        band-limited-resamples before packing (e.g. play a PDM/odd-rate stream
        decimated to 48 kHz); the returned rate is the actual playback rate."""
        chans = [c for c in (channels if channels is not None
                             else self.channels(device, dp))
                 if (device, dp, c) in self._channels]
        native = int(round(self.rate(device, dp))) or 48000
        if not chans:
            return b"", native, 0, "Int16"
        wide = (depth >= 24)
        full = float((1 << 31) - 1) if wide else 32767.0
        dt = np.int32 if wide else np.int16
        lo, hi = (-(1 << 31), (1 << 31) - 1) if wide else (-32768, 32767)
        out_rate = native
        do_resample = bool(target_rate) and int(target_rate) != native
        if do_resample:
            from ..dsp import resample
            out_rate = int(target_rate)
        cols = []
        for ch in chans:
            s = self.samples(device, dp, ch)[start:stop].astype(np.float64)
            bits = self.sample_bits(device, dp, ch)
            scale = full / float(1 << (bits - 1)) if bits and bits > 1 else 1.0
            s = s * scale
            if do_resample:
                s = resample(s, native, out_rate)
            cols.append(np.clip(np.round(s), lo, hi).astype(dt))
        # Pad shorter channels with silence to the LONGEST (zero-fill), matching
        # export_wav — truncating to the shortest dropped tail audio on the others.
        n = max((c.size for c in cols), default=0)
        nch = len(cols)
        out = np.zeros(n * nch, dtype=dt)
        for i, c in enumerate(cols):
            out[i::nch][:c.size] = c                # interleave channel-major, zero-fill tail
        return out.tobytes(), out_rate, len(cols), ("Int32" if wide else "Int16")

    # ---- WAV export ----
    def pcm_int16(self, device: int, dp: int, channels: Optional[List[int]] = None,
                  start: int = 0, stop: Optional[int] = None):
        """Interleaved 16-bit PCM for a (device, dataport), normalised to full
        scale, for in-app playback. Returns (np.int16 interleaved, sample_rate_hz,
        n_channels). Each channel is scaled from its own bit width so all play at
        comparable loudness; `start`/`stop` window by sample index."""
        chans = [c for c in (channels if channels is not None
                             else self.channels(device, dp))
                 if (device, dp, c) in self._channels]
        if not chans:
            return np.zeros(0, dtype=np.int16), int(round(self.rate(device, dp))) or 48000, 0
        cols = []
        for ch in chans:
            s = self.samples(device, dp, ch)[start:stop]
            bits = self.sample_bits(device, dp, ch)
            scale = 32767.0 / float(1 << (bits - 1)) if bits and bits > 1 else 1.0
            cols.append(np.clip(np.round(s.astype(np.float64) * scale),
                                -32768, 32767).astype(np.int16))
        # Zero-fill shorter channels to the longest (matches export_wav / pcm_play).
        n = max((c.size for c in cols), default=0)
        nch = len(cols)
        out = np.zeros(n * nch, dtype=np.int16)
        for i, c in enumerate(cols):
            out[i::nch][:c.size] = c                # interleave channel-major, zero-fill tail
        rate = int(round(self.rate(device, dp))) or 48000
        return out, rate, len(cols)

    def export_wav(self, device: int, dp: int, path: str,
                   channels: Optional[List[int]] = None,
                   index_range: Optional[Tuple[int, int]] = None,
                   target_rate: Optional[float] = None) -> str:
        """Write one interleaved multichannel WAV for a (device, dataport).

        `target_rate` (Hz) resamples every channel to that rate before writing
        (band-limited; see dsp.resample) — e.g. decimate a PDM/odd-rate stream to
        48 kHz. None keeps the native rate."""
        chans = channels if channels is not None else self.channels(device, dp)
        chans = [c for c in chans if (device, dp, c) in self._channels]
        if not chans:
            raise ValueError(f"device {device} dp {dp} has no channels to export")
        slices = {ch: self.samples(device, dp, ch) for ch in chans}
        if index_range is not None:
            i0, i1 = index_range
            slices = {ch: s[i0:i1] for ch, s in slices.items()}

        native = int(round(self.rate(device, dp))) or 48000
        out_rate = native
        if target_rate and int(round(target_rate)) != native:
            from ..dsp import resample
            out_rate = int(round(target_rate))
            slices = {ch: np.rint(resample(s, native, out_rate)).astype(np.int64)
                      for ch, s in slices.items()}

        length = max((s.size for s in slices.values()), default=0)
        bits = max(self.sample_bits(device, dp, ch) for ch in chans)
        cbits = container_bits(bits)

        # Build the interleave buffer at the CONTAINER width (little-endian) directly, not
        # int64 then downcast — samples are already in-range PCM, so this is byte-identical
        # while using 2-4x less memory. 24-bit builds in int32 and packs the low 3 bytes.
        dt = np.dtype("<i2") if cbits == 16 else np.dtype("<i4")
        frames = np.zeros((length, len(chans)), dtype=dt)
        for col, ch in enumerate(chans):
            s = slices[ch]
            frames[: s.size, col] = s
        interleaved = frames.reshape(-1)

        if cbits == 24:  # low 3 little-endian bytes of the int32 two's complement
            b = interleaved.view(np.uint8).reshape(-1, 4)[:, :3]
            raw = np.ascontiguousarray(b).tobytes()
        else:
            raw = interleaved.tobytes()

        with wave.open(path, "wb") as w:
            w.setnchannels(len(chans))
            w.setsampwidth(cbits // 8)
            w.setframerate(out_rate)
            w.writeframes(raw)
        return path

    def export_all(self, directory: str, prefix: str = "swi3s",
                   streams: Optional[List[Tuple[int, int]]] = None,
                   sample_range: Optional[Tuple[int, int]] = None,
                   target_rate: Optional[float] = None) -> List[str]:
        """Export selected (device, dp) streams (default all), optionally
        resampled to `target_rate` Hz."""
        out = []
        for dev, dp in (streams if streams is not None else self.streams()):
            rng = None
            if sample_range is not None:
                chs = self.channels(dev, dp)
                if chs:
                    rng = self.index_range_for_samples(dev, dp, chs[0], *sample_range)
            path = os.path.join(directory, f"{prefix}_dev{dev}_dp{dp}.wav")
            out.append(self.export_wav(dev, dp, path, index_range=rng,
                                       target_rate=target_rate))
        return out
