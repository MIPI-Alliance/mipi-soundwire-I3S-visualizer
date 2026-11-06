"""The SNR helpers of tests/conftest.py the audio tests judge a decode by: `worst_snr` reads
every sample of a stream."""
import numpy as np
from conftest import worst_snr


def _tone(n):
    return np.sin(2 * np.pi * 1000.0 * np.arange(n) / 48000)


def test_worst_snr_covers_every_sample_of_a_stream_not_only_whole_windows():
    t = np.arange(9000) / 48000
    clean = _tone(9000)
    assert worst_snr(clean) > 30.0 and worst_snr(clean[:7000]) > 30.0
    for size, slip in ((7000, 5000), (9000, 8192)):    # past the first window; in the tail
        slipped = clean[:size].copy()
        slipped[slip:] = np.sin(2 * np.pi * 1000.0 * t[slip:size] + 2.0)
        assert worst_snr(slipped) < 20.0, (size, slip)


def test_worst_snr_reads_every_sample_of_a_window_longer_than_tone_snr_reads():
    clean = _tone(20000)
    garbled = clean.copy()
    garbled[5000:6000] = np.random.default_rng(3).uniform(-1.0, 1.0, 1000)
    assert worst_snr(clean, 8192) > 30.0
    assert worst_snr(garbled, 8192) < 20.0
