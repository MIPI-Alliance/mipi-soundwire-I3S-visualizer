"""Arbitrary-rate resampler tests (numpy-only DSP).

Run: python3 tests/test_resample.py
"""
import numpy as np

from swi3s_studio.dsp import resample, design_lowpass, resampled_length, pdm_to_pcm


def _tone(freq, rate, n):
    return np.sin(2.0 * np.pi * freq * np.arange(n) / rate)


def test_identity_and_length():
    x = np.arange(100.0)
    assert np.array_equal(resample(x, 48000, 48000), x)      # rates equal -> copy
    assert resample(np.array([]), 48000, 16000).size == 0
    # Output length matches the predicted length for several ratios.
    for inr, outr in [(48000, 16000), (16000, 48000), (3072000, 48000), (48000, 44100)]:
        n = 5000
        y = resample(np.zeros(n), inr, outr)
        assert y.size == resampled_length(n, inr, outr), (inr, outr, y.size)


def test_downsample_preserves_inband_tone():
    fs = 48000
    sig = _tone(1000, fs, fs)                 # 1 kHz, 1 s
    y = resample(sig, fs, 16000)              # /3; 1 kHz well below 8 kHz Nyquist
    ref = _tone(1000, 16000, y.size)
    mid = slice(2000, y.size - 2000)          # ignore filter edge transients
    assert np.max(np.abs(y[mid] - ref[mid])) < 0.05


def test_antialias_rejects_out_of_band():
    fs = 48000
    hi = _tone(7000, fs, fs)                  # 7 kHz
    y = resample(hi, fs, 12000)               # new Nyquist 6 kHz -> must be removed
    rms = np.sqrt(np.mean(y[1000:-1000] ** 2))
    assert rms < 0.1, rms                      # not aliased back as a low tone


def test_upsample_preserves_tone():
    base = 16000
    sig = _tone(500, base, base)
    y = resample(sig, base, 48000)            # x3
    ref = _tone(500, 48000, y.size)
    mid = slice(3000, y.size - 3000)
    assert np.max(np.abs(y[mid] - ref[mid])) < 0.05


def test_amplitude_preserved():
    # Unity DC gain: a constant resamples to ~the same constant (interior, away
    # from filter-edge transients).
    y = resample(np.full(16000, 0.5), 48000, 12000)   # -> 4000 samples
    assert abs(np.mean(y[300:-300]) - 0.5) < 1e-3


def test_design_lowpass_dc_gain():
    h = design_lowpass(101, 0.25, gain=3.0)
    assert h.size == 101 and abs(h.sum() - 3.0) < 1e-9
    assert np.allclose(h, h[::-1])             # symmetric (linear phase)


def test_pdm_decimation():
    # Alternating 1,0 PDM (50% density) -> ~0 DC after decimation.
    pdm = np.tile([1, 0], 48000)
    p = pdm_to_pcm(pdm, 3072000, 48000)
    assert abs(np.mean(p)) < 0.05
    # A low-frequency PDM-encoded tone (density-modulated) recovers that tone's
    # sign trend: build a slow square in density and check the decimated mean swings.
    hi = 3072000
    t = np.arange(hi // 8)                      # 1/8 s
    dens = (np.sin(2.0 * np.pi * 100 * t / hi) > 0).astype(float)   # 100 Hz square in density
    rec = pdm_to_pcm(dens, hi, 48000)
    assert rec.max() > 0.3 and rec.min() < -0.3


if __name__ == "__main__":
    test_identity_and_length(); print("ok: identity + predicted output length")
    test_downsample_preserves_inband_tone(); print("ok: downsample preserves in-band tone")
    test_antialias_rejects_out_of_band(); print("ok: anti-alias rejects out-of-band tone")
    test_upsample_preserves_tone(); print("ok: upsample preserves tone")
    test_amplitude_preserved(); print("ok: unity DC gain (amplitude preserved)")
    test_design_lowpass_dc_gain(); print("ok: low-pass DC gain + symmetry")
    test_pdm_decimation(); print("ok: PDM -> PCM decimation")
    print("ALL RESAMPLE TESTS PASSED")
