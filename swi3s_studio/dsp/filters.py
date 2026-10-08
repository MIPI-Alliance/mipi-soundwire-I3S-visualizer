"""Zero-phase Butterworth high-pass filtering for decoded audio.

`butter_highpass_sos(cutoff_hz, rate_hz)` designs the filter as second-order sections and
`sosfiltfilt(sos, x)` runs it forward and then backward, so the result has the squared
magnitude response and no phase shift: an edge or a click stays where it happened instead
of being smeared later in time, which is the point when looking for glitches.

Both follow scipy.signal step for step (`butter(order, fc, "highpass", fs=fs,
output="sos")`, `sosfilt_zi`, and `sosfiltfilt` with its odd padding), and the tests compare
them with outputs SciPy produced. `highpass` pads by at least three periods of the corner
(`padlen`): SciPy's default extension is 15 samples, far shorter than a low corner rings,
and it left a transient of up to half the signal's offset over the first and last 40 ms of
a stream at 20 Hz, a false glitch at both ends of every capture. SciPy is not a dependency: the design is a few
lines of numpy, and the one part numpy cannot vectorise, the recursion itself, runs in the
native core (`swi3score.sosfilt`).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

ORDER = 4                   # the high-pass offered in the UI: 4th-order Butterworth
MIN_CUTOFF_HZ = 1.0
MAX_CUTOFF_HZ = 20_000.0


def max_cutoff_hz(rate_hz: float) -> float:
    """The highest corner a stream at `rate_hz` can take: 20 kHz, or just under Nyquist."""
    return float(min(MAX_CUTOFF_HZ, 0.499 * float(rate_hz)))


def butter_highpass_sos(cutoff_hz: float, rate_hz: float, order: int = ORDER) -> np.ndarray:
    """A Butterworth high-pass of even `order` as (order // 2, 6) second-order sections,
    [b0, b1, b2, 1, a1, a2] per row, with the gain in the first section and the pole pair
    nearest the unit circle in the last, as scipy.signal.butter(..., output="sos")."""
    if order < 2 or order % 2:
        raise ValueError("only even orders are supported")
    fs = float(rate_hz)
    if not 0.0 < cutoff_hz < fs / 2.0:
        raise ValueError(f"cutoff {cutoff_hz} Hz must lie between 0 and Nyquist ({fs / 2} Hz)")
    # Analog prototype poles on the unit circle's left half, then the high-pass transform
    # at the corner pre-warped for the bilinear transform (scipy's internal fs = 2).
    m = np.arange(-order + 1, order, 2)
    proto = -np.exp(1j * np.pi * m / (2 * order))
    warped = 4.0 * np.tan(np.pi * (2.0 * cutoff_hz / fs) / 2.0)
    p_hp = warped / proto                       # every zero of the high-pass is at s = 0
    k_hp = np.real(1.0 / np.prod(-proto))
    # Bilinear transform: s = 0 maps to z = 1, so every digital zero is at z = 1.
    p = (4.0 + p_hp) / (4.0 - p_hp)
    k = k_hp * np.real(1.0 / np.prod(4.0 - p_hp)) * 4.0 ** order
    # One section per conjugate pair, farthest from the unit circle first.
    upper = p[np.imag(p) > 0]
    upper = upper[np.argsort(np.abs(upper))]
    sos = np.zeros((order // 2, 6))
    for i, pole in enumerate(upper):
        sos[i] = [1.0, -2.0, 1.0, 1.0, -2.0 * pole.real, abs(pole) ** 2]
    sos[0, :3] *= k
    return sos


def sosfilt_zi(sos: np.ndarray) -> np.ndarray:
    """Initial state per section for a step response's steady state (scipy's sosfilt_zi):
    scaled by the first input sample, the filter starts as if the signal had always been
    at that level, so it does not ring at the start."""
    zi = np.empty((sos.shape[0], 2))
    scale = 1.0
    for s, row in enumerate(sos):
        b, a = row[:3], row[3:]
        i_minus_a = np.array([[1.0 + a[1], -1.0], [a[2], 1.0]])
        zi[s] = scale * np.linalg.solve(i_minus_a, b[1:] - a[1:] * b[0])
        scale *= b.sum() / a.sum()
    return zi


def sosfilt(sos: np.ndarray, x: np.ndarray, zi: np.ndarray):
    """(y, zf): the cascade run once over `x` from state `zi`, in the native core."""
    import swi3score
    return swi3score.sosfilt(np.ascontiguousarray(sos, dtype=np.float64),
                             np.ascontiguousarray(x, dtype=np.float64),
                             np.ascontiguousarray(zi, dtype=np.float64))


def _padlen(sos: np.ndarray) -> int:
    ntaps = 2 * sos.shape[0] + 1
    ntaps -= min(int((sos[:, 2] == 0).sum()), int((sos[:, 5] == 0).sum()))
    return 3 * ntaps


def sosfiltfilt(sos: np.ndarray, x: np.ndarray, padlen: Optional[int] = None) -> np.ndarray:
    """Forward-backward filtering with scipy.sosfiltfilt's edge handling: the signal is
    extended at each end by its odd reflection (`padlen` samples, by default `3 * ntaps`),
    each pass starts from the steady state of its first sample, and the extension is cut
    off again.

    SciPy refuses a signal no longer than the extension; here the extension shrinks to fit
    instead, so a very short stream is still filtered, and one of fewer than two samples
    comes back unchanged."""
    x = np.asarray(x, dtype=np.float64)
    n = x.shape[0]
    if n < 2:
        return x.copy()
    edge = min(_padlen(sos) if padlen is None else int(padlen), n - 1)
    ext = np.concatenate((2.0 * x[0] - x[edge:0:-1], x,
                          2.0 * x[-1] - x[-2:-(edge + 2):-1]))
    zi = sosfilt_zi(sos)
    y, _ = sosfilt(sos, ext, zi * ext[0])
    y, _ = sosfilt(sos, y[::-1], zi * y[-1])
    return y[::-1][edge:-edge] if edge else y[::-1]


def highpass_padlen(sos: np.ndarray, cutoff_hz: float, rate_hz: float) -> int:
    """The edge extension `highpass` uses: three periods of the corner, or SciPy's default
    if that is longer. Measured on an offset tone with a drift, it takes the edge error
    at a 20 Hz corner from 394 to 0, and at 1 Hz from 783 to 7."""
    return max(_padlen(sos), int(np.ceil(3.0 * float(rate_hz) / float(cutoff_hz))))


def highpass(x: np.ndarray, cutoff_hz: float, rate_hz: float) -> np.ndarray:
    """`x` through the 4th-order zero-phase Butterworth high-pass at `cutoff_hz`."""
    sos = butter_highpass_sos(cutoff_hz, rate_hz)
    return sosfiltfilt(sos, x, padlen=highpass_padlen(sos, cutoff_hz, rate_hz))
