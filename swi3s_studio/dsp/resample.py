"""Arbitrary-rate, band-limited resampling for decoded audio (numpy-only).

`resample(x, in_rate, out_rate)` converts a sample stream from any input rate to
any output rate via rational polyphase resampling: approximate out/in as L/M
(bounded denominator), upsample by L, low-pass at min(in,out)/2, downsample by M.
The low-pass is a Blackman-windowed sinc; convolution is done by FFT so even a
heavy PDM decimation (e.g. 3.072 MHz → 48 kHz, M = 64) stays O(N log N).

`pdm_to_pcm` is a thin wrapper for 1-bit (PDM) dataport streams: map {0,1} → {−1,
+1} and resample — the low-pass integrates the pulse density into PCM.

Pure functions over numpy arrays; no SciPy, no Qt. The amplitude scale is
preserved (the filter has unity DC gain), so resampled integer audio keeps the
same full-scale range as its input.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np


def design_lowpass(num_taps: int, cutoff: float, gain: float = 1.0) -> np.ndarray:
    """Blackman-windowed sinc low-pass. `cutoff` is normalised to Nyquist (0..1);
    the result is scaled so its DC gain equals `gain`. `num_taps` is forced odd
    so the filter is symmetric (linear phase)."""
    num_taps = int(num_taps) | 1
    n = np.arange(num_taps) - (num_taps - 1) / 2.0
    h = np.sinc(cutoff * n) * np.blackman(num_taps)
    s = h.sum()
    if s != 0.0:
        h = h * (gain / s)
    return h


def _fftconvolve_same(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Linear convolution via FFT, cropped to the 'same' (centred) region — the
    symmetric FIR's group delay is (len(h)-1)/2, removed here so output aligns."""
    n = x.size + h.size - 1
    nfft = 1 << int(np.ceil(np.log2(max(1, n))))
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(h, nfft), nfft)[:n]
    start = (h.size - 1) // 2
    return y[start:start + x.size]


def resample(x, in_rate: float, out_rate: float, half_width: int = 24,
             max_denominator: int = 256) -> np.ndarray:
    """Resample `x` from `in_rate` to `out_rate` (Hz). Returns float64 of length
    ≈ len(x) * out_rate / in_rate. Identity (rates equal / empty) returns a copy.

    `half_width` sets the filter quality (taps per polyphase branch ≈ 2*half_width);
    `max_denominator` bounds the rational approximation of the ratio (a tiny rate
    error in exchange for a bounded filter / upsample factor)."""
    x = np.asarray(x, dtype=np.float64).ravel()
    ir = int(round(in_rate))
    orate = int(round(out_rate))
    if x.size == 0 or ir <= 0 or orate <= 0 or ir == orate:
        return x.copy()

    frac = Fraction(orate, ir).limit_denominator(max_denominator)
    L, M = frac.numerator, frac.denominator
    if L < 1 or M < 1:
        return x.copy()

    # Upsample by L (zero-stuff), low-pass, decimate by M. Cutoff is the lower of
    # the two Nyquists, normalised to the upsampled Nyquist (= min(1/L, 1/M)).
    up = np.zeros(x.size * L, dtype=np.float64)
    up[::L] = x
    cutoff = min(1.0 / L, 1.0 / M)
    taps = 2 * half_width * max(L, M) + 1
    h = design_lowpass(taps, cutoff, gain=float(L))  # gain L compensates zero-stuff
    y = _fftconvolve_same(up, h)
    return y[::M]


def resampled_length(n: int, in_rate: float, out_rate: float,
                     max_denominator: int = 256) -> int:
    """Output length `resample` will produce for an input of `n` samples — handy
    for sizing/validation without running the filter."""
    ir, orate = int(round(in_rate)), int(round(out_rate))
    if n == 0 or ir <= 0 or orate <= 0 or ir == orate:
        return n
    frac = Fraction(orate, ir).limit_denominator(max_denominator)
    return -(-(n * frac.numerator) // frac.denominator)   # ceil(n*L/M), matches up[::M]


def pdm_to_pcm(bits, in_rate: float, out_rate: float, **kw) -> np.ndarray:
    """Decimate a 1-bit PDM stream to PCM. `bits` is 0/1 (or already ±1); it is
    mapped to ±1 then band-limited + downsampled by `resample`. Returns float64 in
    roughly [-1, 1] (full-scale before any integer quantisation)."""
    b = np.asarray(bits, dtype=np.float64).ravel()
    if b.size and b.min() >= 0.0:        # {0,1} -> {-1,+1}; pass ±1 through unchanged
        b = b * 2.0 - 1.0
    return resample(b, in_rate, out_rate, **kw)
