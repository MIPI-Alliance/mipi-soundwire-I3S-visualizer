"""A short-time spectrum of one stretch of a channel, for the Audio pane's spectrogram lane.

`spectrogram(x, i0, i1, columns, full_scale)` returns one column per output pixel: a
Blackman-Harris-windowed FFT of `nfft` samples centred on each column's position in [i0, i1), as dB
relative to a full-scale sine. It is computed for the visible stretch on every zoom or pan,
like the waveform's envelope, so its cost scales with the columns, not the stream's length.

Zoomed out past `columns x nfft` samples the frames no longer overlap and the samples
between them are not looked at: a click shorter than the gap between two frames can fall
between columns until you zoom in. Zoomed in to fewer than `nfft` samples per column, the
frames overlap and neighbouring columns repeat.

`held_spectrogram(x, positions, t0, t1, step, ...)` is the same spectrum of the signal as
transported: each sample held from its capture position until the next, read on a uniform
grid of `step` capture samples. A stream carried on an uneven schedule shows the schedule's
spurs there and not in `spectrogram`, which takes the samples as evenly spaced.
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
# 4-term Blackman-Harris coefficients: sidelobes at -92 dB. A full-scale tone's leakage is
# under -90 dB beyond 5 bins and under -100 dB beyond 10; it stays above the Audio pane's
# -120 dB colour floor for about 70 bins either side. Scalloping loss is at most 0.83 dB.
_BH4 = (0.35875, 0.48829, 0.14128, 0.01168)


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
    frames = np.asarray(x)[starts[:, None] + np.arange(nfft)]
    return Spectrum(_db(frames, full_scale), centres, nfft)


def held_spectrogram(x: np.ndarray, positions: np.ndarray, t0: float, t1: float,
                     step: float, columns: int, full_scale: float,
                     nfft: int = NFFT) -> Spectrum:
    """The spectrum of `x` held at its `positions` (ascending capture samples), over the
    capture window [t0, t1), read every `step` capture samples. `centres` are the frames'
    centres as capture positions (float), not sample indices."""
    pos = np.asarray(positions)
    n_x = int(min(np.asarray(x).size, pos.size))
    if n_x < _MIN_NFFT or not step > 0:
        return Spectrum(np.zeros((0, 0)), np.zeros(0), 0)
    p0 = float(pos[0])
    n = int((float(pos[n_x - 1]) - p0) // step) + 1        # grid points over the stream
    k0 = max(0, int(np.floor((float(t0) - p0) / step)))
    k1 = min(n, int(np.ceil((float(t1) - p0) / step)) + 1)
    columns = max(1, int(columns))
    if n < _MIN_NFFT or k1 <= k0:
        return Spectrum(np.zeros((0, 0)), np.zeros(0), 0)
    nfft = int(min(nfft, 1 << (n.bit_length() - 1)))
    columns = min(columns, k1 - k0)
    centres = np.linspace(k0, k1 - 1, columns).round().astype(np.int64)
    starts = np.clip(centres - nfft // 2, 0, n - nfft)
    grid = p0 + (starts[:, None] + np.arange(nfft)) * float(step)
    idx = np.clip(np.searchsorted(pos[:n_x], grid, side="right") - 1, 0, n_x - 1)
    return Spectrum(_db(np.asarray(x)[idx], full_scale), p0 + centres * float(step), nfft)


def _db(frames: np.ndarray, full_scale: float) -> np.ndarray:
    """Blackman-Harris-windowed magnitude of each row of `frames`, dB re a full-scale sine."""
    frames = frames.astype(np.float64)
    window = _blackman_harris(frames.shape[1])
    # Remove the window-weighted mean, which zeroes the DC bin exactly; a plain mean leaves
    # a DC term in the bottom bins whenever the frame holds a fraction of a cycle.
    frames -= (frames @ window / window.sum())[:, None]
    mag = np.abs(np.fft.rfft(frames * window, axis=1))
    # A sine of amplitude A peaks at A * sum(window) / 2 in its bin.
    ref = float(full_scale or 1.0) * window.sum() / 2.0
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(mag / ref)
    return np.maximum(db, FLOOR_DB)


def _blackman_harris(n: int) -> np.ndarray:
    """The periodic (DFT-even) 4-term Blackman-Harris window of length `n`."""
    a0, a1, a2, a3 = _BH4
    w = 2.0 * np.pi * np.arange(n) / n
    return a0 - a1 * np.cos(w) + a2 * np.cos(2 * w) - a3 * np.cos(3 * w)
