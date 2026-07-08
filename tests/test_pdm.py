"""PDM decode/decimation tests.

A 1-bit PDM stream is a bipolar density code, NOT a 1-bit two's-complement sample:
{0,1} must map to {-1,+1}, and the audio is recovered by band-limited decimation.
These tests encode a known tone with a 1st-order sigma-delta modulator, then check
the decode recovers the right pitch with ~0 DC (the bug was {0,1}->{0,-1}, which
both halves the swing and adds a -0.5 DC offset, decoding tones as garble).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from swi3s_studio.dsp import pdm_to_pcm
from swi3s_studio.store.audio_store import sign_extend, decode_pdm, AudioStore


def _sigma_delta_1bit(x: np.ndarray) -> np.ndarray:
    """1st-order sigma-delta modulator: float x in [-1,1] -> PDM bits {0,1} whose
    local density tracks x. (Recursive — must be a Python loop.)"""
    acc = 0.0
    out = np.empty(x.size, dtype=np.uint8)
    for i in range(x.size):
        acc += float(x[i])
        b = 1.0 if acc >= 0.0 else -1.0
        out[i] = 1 if b > 0 else 0
        acc -= b
    return out


def _dominant_hz(sig: np.ndarray, rate: float) -> float:
    sp = np.abs(np.fft.rfft(sig * np.hanning(sig.size)))
    fr = np.fft.rfftfreq(sig.size, 1.0 / rate)
    return float(fr[np.argmax(sp[1:]) + 1])             # skip DC bin


def test_sign_extend_1bit_is_bipolar():
    assert list(sign_extend(np.array([0, 1, 0, 1]), 1)) == [-1, 1, -1, 1]
    # multi-bit stays two's complement
    assert list(sign_extend(np.array([0, 1, 127, 128, 255]), 8)) == [0, 1, 127, -128, -1]


def test_pdm_to_pcm_recovers_tone_without_dc():
    fs, f0, out = 2_048_000.0, 997.0, 48000
    t = np.arange(int(fs * 0.2)) / fs
    bits = _sigma_delta_1bit(0.5 * np.sin(2 * np.pi * f0 * t))
    pcm = pdm_to_pcm(bits, fs, out)
    assert abs(float(pcm.mean())) < 0.02, pcm.mean()        # no DC offset
    assert abs(_dominant_hz(pcm, out) - f0) < 10.0, _dominant_hz(pcm, out)


def test_decode_pdm_scales_and_decimates():
    fs, f0 = 2_048_000.0, 440.0
    t = np.arange(int(fs * 0.2)) / fs
    bits = _sigma_delta_1bit(0.5 * np.sin(2 * np.pi * f0 * t))
    signed, out_rate, M = decode_pdm(bits, fs)
    assert M > 1 and 40000 < out_rate < 50000, (M, out_rate)
    assert signed.size == bits.size // M or abs(signed.size - bits.size // M) <= 1
    # full-scale-ish (0.5 amplitude -> well above the int16 noise floor) and centered
    assert abs(float(signed.mean())) < 0.05 * 32767
    assert signed.astype(np.float64).std() > 0.05 * 32767
    assert abs(_dominant_hz(signed.astype(np.float64), out_rate) - f0) < 10.0


def test_store_decodes_pdm_channel_as_pcm():
    fs, f0 = 2_048_000.0, 997.0
    t = np.arange(int(fs * 0.2)) / fs
    bits = _sigma_delta_1bit(0.5 * np.sin(2 * np.pi * f0 * t))
    n = bits.size
    cols = dict(
        device=np.zeros(n, np.int32), dp=np.ones(n, np.int32),
        channel=np.zeros(n, np.int32), sample_size=np.ones(n, np.int32),
        value=bits.astype(np.uint32), index=np.arange(n, dtype=np.uint64),
        start_sample=np.arange(n, dtype=np.uint64),
    )
    store = AudioStore.from_audio_columns(cols, {(0, 1): fs})
    assert store.sample_bits(0, 1, 0) == 16                 # decoded to PCM
    rate = store.rate(0, 1)
    assert 40000 < rate < 50000, rate                       # rate replaced by decimated
    # The on-bus rate + sample size are preserved for display / bandwidth, distinct
    # from the decimated playback rate above (so the UI shows the true DP rate).
    assert store.native_rate(0, 1) == fs
    assert store.native_sample_bits(0, 1, 0) == 1
    s = store.samples(0, 1, 0).astype(np.float64)
    assert abs(s.mean()) < 0.05 * 32767                     # no DC
    assert abs(_dominant_hz(s, rate) - f0) < 10.0           # right pitch
    # capture-sample anchors rebased onto the decimated grid: one per output sample,
    # monotonic non-decreasing (needed for the cursor's searchsorted mapping)
    sat = store._sample_at[(0, 1, 0)]
    assert sat.size == s.size
    assert np.all(np.diff(sat) >= 0)


def test_decode_pdm_blocks_dc_bias():
    # Real PDM mics sit at a density well off 50% (a big DC bias) with the audio
    # riding on top — exactly digital_0/1's mics (~0.31/0.36 density). With DC-block
    # ENABLED (Audio ▸ Block PDM DC Bias), the bias is removed so the tone survives
    # instead of the dominant "frequency" being 0.
    fs, f0, bias = 2_048_000.0, 997.0, 0.45        # 0.45 DC -> density ~0.7
    t = np.arange(int(fs * 0.2)) / fs
    bits = _sigma_delta_1bit(np.clip(bias + 0.1 * np.sin(2 * np.pi * f0 * t), -1, 1))
    signed, out_rate, M = decode_pdm(bits, fs, dc_block=True)
    s = signed.astype(np.float64)
    assert abs(s.mean()) < 0.02 * 32767, s.mean()              # DC removed despite 0.7 density
    assert abs(_dominant_hz(s, out_rate) - f0) < 10.0          # tone survives, not DC


def test_decode_pdm_preserves_dc_by_default():
    # Default (dc_block=False, the analyzer default): decode the TRUE density so a
    # constant test pattern reads its real DC level instead of 0. All-ones is
    # full-scale +1, all-zeros −1; a 50% pattern is ~0. (Skip the FIR edge
    # transients by taking the median of the settled middle.)
    fs = 3_072_000.0

    def settled(bits):
        s, _out, _M = decode_pdm(np.asarray(bits, dtype=np.int64), fs)
        return int(np.median(s[s.size // 4: 3 * s.size // 4]))

    assert settled(np.ones(300_000)) == 32767            # all-ones -> +1 DC (the reported bug)
    assert settled(np.zeros(300_000)) == -32767          # all-zeros -> -1 DC
    assert abs(settled(np.tile([1, 0], 150_000))) < 0.02 * 32767   # 50% -> ~0
    # DC-block ON instead drives the constant all-ones to ~0.
    s_on, _r, _m = decode_pdm(np.ones(300_000, dtype=np.int64), fs, dc_block=True)
    assert abs(int(np.median(s_on[s_on.size // 4: 3 * s_on.size // 4]))) < 0.02 * 32767


def test_store_pdm_dc_block_flag_threads_through():
    # The AudioStore build honours pdm_dc_block: default OFF keeps a constant
    # all-ones channel at full-scale +1; ON removes it.
    fs, n = 3_072_000.0, 300_000
    cols = dict(
        device=np.zeros(n, np.int32), dp=np.ones(n, np.int32),
        channel=np.zeros(n, np.int32), sample_size=np.ones(n, np.int32),
        value=np.ones(n, np.uint32), index=np.arange(n, dtype=np.uint64),
        start_sample=np.arange(n, dtype=np.uint64),
    )

    def median_mid(store):
        s = store.samples(0, 1, 0)
        return int(np.median(s[s.size // 4: 3 * s.size // 4]))

    off = AudioStore.from_audio_columns(cols, {(0, 1): fs})                  # default: no block
    assert median_mid(off) == 32767
    on = AudioStore.from_audio_columns(cols, {(0, 1): fs}, pdm_dc_block=True)
    assert abs(median_mid(on)) < 0.02 * 32767


if __name__ == "__main__":
    test_sign_extend_1bit_is_bipolar()
    test_pdm_to_pcm_recovers_tone_without_dc()
    test_decode_pdm_scales_and_decimates()
    test_decode_pdm_blocks_dc_bias()
    test_decode_pdm_preserves_dc_by_default()
    test_store_decodes_pdm_channel_as_pcm()
    test_store_pdm_dc_block_flag_threads_through()
    print("PDM TESTS PASSED")
