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

    @classmethod
    def from_session(cls, session, mmap_dir: Optional[str] = None,
                     pdm_dc_block: bool = False) -> "AudioStore":
        return cls.from_audio_columns(session.audio_columns(),
                                      session.decoder.audio_sample_rates(),
                                      mmap_dir=mmap_dir, pdm_dc_block=pdm_dc_block)

    @classmethod
    def from_audio(cls, audio: List[dict], rates: Dict[Tuple[int, int], float],
                   mmap_dir: Optional[str] = None,
                   pdm_dc_block: bool = False) -> "AudioStore":
        """Build from the list-of-dicts form (decoder.audio()). Kept for callers
        and tests that have the dict list; it just packs the columns and delegates
        to :meth:`from_audio_columns`, so the grouping is the same vectorised path."""
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
        }
        return cls.from_audio_columns(cols, rates, mmap_dir=mmap_dir,
                                      pdm_dc_block=pdm_dc_block)

    @classmethod
    def from_audio_columns(cls, cols: Dict[str, np.ndarray],
                           rates: Dict[Tuple[int, int], float],
                           mmap_dir: Optional[str] = None,
                           pdm_dc_block: bool = False) -> "AudioStore":
        """Build from columnar (struct-of-arrays) audio — the fast path for large
        captures. `cols` carries parallel arrays: device, dp, channel, sample_size,
        value, index, start_sample (from ``Decoder.audio_columns()``). Grouping into
        per-(device, dp, channel) dense arrays is done with NumPy (one lexsort +
        slicing), not per-sample Python dicts, so 50M+ samples stay manageable."""
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
        index = np.asarray(cols["index"]).astype(np.int64, copy=False)
        start_sample = np.asarray(cols["start_sample"]).astype(np.int64, copy=False)
        total = device.shape[0]
        if total == 0:
            return store
        # One lexical sort groups all samples by (device, dp, channel); a run of
        # equal keys is then one contiguous slice. Robust to any index range (no
        # bit-packing assumptions).
        order = np.lexsort((channel, dp, device))
        sdev, sdp, sch = device[order], dp[order], channel[order]
        changed = (np.diff(sdev) != 0) | (np.diff(sdp) != 0) | (np.diff(sch) != 0)
        cuts = np.flatnonzero(changed) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [total]))
        for s, e in zip(starts.tolist(), ends.tolist()):
            rows = order[s:e]
            r0 = rows[0]
            key = (int(device[r0]), int(dp[r0]), int(channel[r0]))
            bits = int(sample_size[r0])
            idx = index[rows]
            n = int(idx.max()) + 1 if idx.size else 0
            # Dense per-channel arrays. Gaps (missing indices) zero-fill, last write
            # wins on dup idx. PDM streams are contiguous, so the zero-fill is never
            # reached for them (see decode_pdm on why that matters).
            dense_val = np.zeros(n, dtype=np.int64)
            dense_val[idx] = value[rows].astype(np.int64, copy=False)
            dense_sat = np.zeros(n, dtype=np.int64)
            dense_sat[idx] = start_sample[rows]
            # Carry capture-sample anchors across any index gap so the array stays
            # non-decreasing — index_range_for_samples does searchsorted on it, and a
            # raw zero in a gap would break monotonicity and return garbage slices.
            if n:
                dense_sat = np.maximum.accumulate(dense_sat)

            if bits == 1:
                # 1-bit PDM density stream: decode to PCM here so the rest of the
                # app (display/cursor/playback/export) sees a uniform signed-PCM
                # channel. Decimation collapses the bit stream toward ~48 kHz and
                # rebases the per-sample capture anchors onto the decimated grid.
                devp = (int(device[r0]), int(dp[r0]))
                signed, out_rate, M = decode_pdm(dense_val, native_rates.get(devp, 0.0),
                                                 dc_block=pdm_dc_block)
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
    def streams(self) -> List[Tuple[int, int]]:
        """Sorted (device, dp) pairs that carry audio."""
        return sorted({(dev, dp) for dev, dp, _ in self._channels})

    def channels(self, device: int, dp: int) -> List[int]:
        return sorted(ch for d, p, ch in self._channels if d == device and p == dp)

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
        `max_points` columns using the render pyramid — O(points), not O(samples)."""
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
        return x, lo, hi

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

        frames = np.zeros((length, len(chans)), dtype=np.int64)
        for col, ch in enumerate(chans):
            s = slices[ch]
            frames[: s.size, col] = s
        interleaved = frames.reshape(-1)

        if cbits == 16:
            raw = interleaved.astype("<i2").tobytes()
        elif cbits == 32:
            raw = interleaved.astype("<i4").tobytes()
        else:  # 24-bit: low 3 little-endian bytes of the int32 two's complement
            b = interleaved.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
            raw = np.ascontiguousarray(b).tobytes()

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
