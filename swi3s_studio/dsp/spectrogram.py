"""A short-time spectrum of one stretch of a channel, for the Audio pane's spectrogram lane.

`spectrogram(x, i0, i1, columns, full_scale)` returns one column per output pixel: a
Hann-windowed FFT of `nfft` samples centred on each column's position in [i0, i1), as dB
relative to a full-scale sine. It is computed for the visible stretch on every zoom or pan,
like the waveform's envelope, so its cost is the columns (about 8 ms for 1400 at 1024
points on a laptop), not the stream's length.

Zoomed out past `columns x nfft` samples the frames no longer overlap and the samples
between them are not looked at: a click shorter than the gap between two frames can fall
between columns until you zoom in. Zoomed in to fewer than `nfft` samples per column, the
frames overlap and neighbouring columns repeat. Both are the usual trade of a spectrogram drawn
at screen resolution.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np

# Frame length in samples: 47 Hz bins at 48 kHz. A power of two, for the FFT.
NFFT = 1024
# The frame lengths offered. A frame reaches half its length either side of its column, so
# a glitch shows from that far before it: 2.7 ms at 256 and 48 kHz, 21 ms at 2048, with
# bins of 188 Hz and 23 Hz. Time against frequency resolution is the trade.
FRAME_SIZES = (256, 512, 1024, 2048)
# The smallest frame worth drawing; a shorter stream gets one frame of its own length.
_MIN_NFFT = 16
# dB below full scale treated as silence, so log10 never sees zero.
FLOOR_DB = -140.0


class Spectrum(NamedTuple):
    db: np.ndarray        # (columns, nfft // 2 + 1) dB re full-scale sine, column-major
    centres: np.ndarray   # the sample index each column's frame is centred on
    nfft: int


def spectrogram(x: np.ndarray, i0: int, i1: int, columns: int, full_scale: float,
                nfft: int = NFFT) -> Spectrum:
    """The spectrum of `x[i0:i1]` at `columns` evenly spaced frame centres.

    `full_scale` is the amplitude of a full-scale sample (2**(bits-1) for signed PCM), so a
    full-scale sine reads 0 dB. A frame near either end of `x` is moved inward rather than
    padded, so every frame is all signal."""
    n = int(x.size)
    i0, i1 = max(0, int(i0)), min(n, int(i1))
    columns = max(1, int(columns))
    if n < _MIN_NFFT or i1 <= i0:
        return Spectrum(np.zeros((0, 0)), np.zeros(0, dtype=np.int64), 0)
    nfft = int(min(nfft, 1 << (n.bit_length() - 1)))     # largest power of two that fits
    columns = min(columns, i1 - i0)
    centres = np.linspace(i0, i1 - 1, columns).round().astype(np.int64)
    starts = np.clip(centres - nfft // 2, 0, n - nfft)
    frames = np.asarray(x)[starts[:, None] + np.arange(nfft)].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)          # DC would swamp the bottom bin
    window = np.hanning(nfft)
    mag = np.abs(np.fft.rfft(frames * window, axis=1))
    # A sine of amplitude A peaks at A * sum(window) / 2 in its bin.
    ref = float(full_scale or 1.0) * window.sum() / 2.0
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(mag / ref)
    return Spectrum(np.maximum(db, FLOOR_DB), centres, nfft)
