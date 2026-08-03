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
    symmetric FIR's group delay is (len(h)-1)/2, removed here so output aligns.
    Whole-array (unbounded) form; used only for small inputs. `resample` uses the
    output-blocked `_resample_core` so a heavy decimation never allocates an
    O(N)-sized FFT."""
    n = x.size + h.size - 1
    nfft = 1 << int(np.ceil(np.log2(max(1, n))))
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(h, nfft), nfft)[:n]
    start = (h.size - 1) // 2
    return y[start:start + x.size]


# Output samples computed per block. Peak memory is ~one block's zero-stuffed input
# window + its FFT, NOT the whole N*L upsampled signal / convolution (which OOM'd on a
# minutes-long PDM stream at 3.6-5.8 GB).
_OUT_BLOCK = 1 << 15


def _resample_core(x: np.ndarray, L: int, M: int, h: np.ndarray) -> np.ndarray:
    """Compute out[k] = (linconv(upsample(x, L), h) 'same')[k*M] for k in [0, ceil(NL/M)),
    i.e. exactly what ``_fftconvolve_same(up, h)[::M]`` produced — but only for the KEPT
    (every-M-th) samples, in bounded output blocks, so it never materializes the full
    upsampled signal or convolution. This is a low-pass FIR followed by dropping samples
    (polyphase decimation); for L=1 the input window is just a slice of x."""
    N = x.size
    NL = N * L
    T = h.size
    d = (T - 1) // 2                       # symmetric-FIR group delay ('same' offset)
    n_out = -(-NL // M)                     # ceil(NL / M); == resampled_length
    out = np.empty(n_out, dtype=np.float64)

    # A full block spans this many input positions; fix its FFT size so h transforms once.
    seg_common = (_OUT_BLOCK - 1) * M + (T - 1) + 1
    fft_common = 1 << int(np.ceil(np.log2(max(1, seg_common + T - 1))))
    Hf_common = np.fft.rfft(h, fft_common)

    for k0 in range(0, n_out, _OUT_BLOCK):
        k1 = min(k0 + _OUT_BLOCK, n_out)
        # y-indices this block needs: k*M for k in [k0, k1). y[i] = sum_j h[j]*up[i+d-j],
        # so the up-window is [k0*M + d - (T-1), (k1-1)*M + d].
        up_lo = k0 * M + d - (T - 1)
        up_hi = (k1 - 1) * M + d
        seg_len = up_hi - up_lo + 1
        up_seg = np.zeros(seg_len, dtype=np.float64)
        # Scatter x into the zero-stuffed window: up[idx] = x[idx//L] at multiples of L
        # within the window ∩ [0, NL). (L == 1 -> a plain contiguous slice of x.)
        lo = max(0, up_lo)
        lo += (L - lo % L) % L                       # round up to a multiple of L
        hi = min(NL - 1, up_hi)
        if lo <= hi:
            idxs = np.arange(lo, hi + 1, L)
            up_seg[idxs - up_lo] = x[idxs // L]
        if seg_len == seg_common:
            fftsize, Hf = fft_common, Hf_common
        else:                                        # last (short) block
            fftsize = 1 << int(np.ceil(np.log2(max(1, seg_len + T - 1))))
            Hf = np.fft.rfft(h, fftsize)
        # c[p] = sum_q up_seg[q]*h[p-q]; y[i] = c[i + d - up_lo].
        c = np.fft.irfft(np.fft.rfft(up_seg, fftsize) * Hf, fftsize)
        ps = np.arange(k0, k1) * M + d - up_lo
        out[k0:k1] = c[ps]
    return out


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

    # Low-pass at the lower of the two Nyquists (normalised to the upsampled Nyquist),
    # gain L to compensate the zero-stuff, then keep every M-th filtered sample. The core
    # computes only those kept samples in blocks (memory-bounded — see _resample_core).
    cutoff = min(1.0 / L, 1.0 / M)
    taps = 2 * half_width * max(L, M) + 1
    h = design_lowpass(taps, cutoff, gain=float(L))
    return _resample_core(x, L, M, h)


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
