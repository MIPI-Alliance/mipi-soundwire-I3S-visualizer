"""The demo generator's PCM samples, regenerated: native/swi3score/Demo.cpp's sineSample and
sineAt, with their TPDF dither (mix64, ditherTpdf, ditheredSample). Each value is the
unsigned (sample_size + 1)-bit field the wire carries; `signed` sign-extends it.

The arithmetic is the C++'s step for step: unsigned 64-bit wraparound for the hash, IEEE
doubles for the sine and the dither, and rounding half away from zero (std::lround).
"""
import math

_M64 = (1 << 64) - 1


def mix64(x: int) -> int:
    """Demo.cpp mix64: SplitMix64's finaliser."""
    x = (x + 0x9E3779B97F4A7C15) & _M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def dither_tpdf(key: int, index: int) -> float:
    """Demo.cpp ditherTpdf: triangular on (-1, 1) LSB."""
    h1 = mix64(((key * 0xD1B54A32D192ED03) & _M64) ^ index)
    h2 = mix64(h1)
    return (h1 >> 11) * 2.0 ** -53 - (h2 >> 11) * 2.0 ** -53


def _lround(x: float) -> int:
    """std::lround: to the nearest integer, half away from zero."""
    a = abs(x)
    n = math.floor(a)
    if a - n >= 0.5:
        n += 1
    return int(n) if x >= 0 else -int(n)


def _dithered(amp: float, w: float, key: int, index: int, sample_size: int) -> int:
    x = amp * math.sin(w)
    if sample_size >= 1:
        x += dither_tpdf(key, index)
    return _lround(x) & ((1 << (sample_size + 1)) - 1)


def sine_sample(channel: int, index: int, sample_size: int) -> int:
    """Demo.cpp sineSample: one cycle per 64 / (channel + 1) samples, keyed by `channel`;
    `sample_size` is the SampleSize field (bits - 1)."""
    amp = (1 << sample_size) * 0.45
    w = 2.0 * math.pi * (channel + 1) * index / 64.0
    return _dithered(amp, w, channel, index, sample_size)


def sine_at(freq_hz: float, rate_hz: float, index: int, sample_size: int) -> int:
    """Demo.cpp sineAt: a tone at `freq_hz` sampled at `rate_hz`, keyed by the frequency in
    whole millihertz."""
    amp = (1 << sample_size) * 0.45
    w = 2.0 * math.pi * freq_hz * index / rate_hz
    return _dithered(amp, w, _lround(freq_hz * 1000.0), index, sample_size)


def signed(value: int, sample_size: int) -> int:
    """A (sample_size + 1)-bit field as two's complement."""
    sign = 1 << sample_size
    return value - (sign << 1) if value & sign else value
