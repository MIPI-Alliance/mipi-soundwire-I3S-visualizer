"""PDM decode/decimation tests.

A 1-bit PDM stream is a bipolar density code, NOT a 1-bit two's-complement sample:
{0,1} must map to {-1,+1}, and the audio is recovered by band-limited decimation.
These tests encode a known tone with a 1st-order sigma-delta modulator, then check
the decode recovers the right pitch with ~0 DC (mapping {0,1}->{0,-1} instead would
halve the swing and add a -0.5 DC offset).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from swi3s_studio.dsp import pdm_to_pcm
from swi3s_studio.store.audio_store import AudioStore, decode_pdm, sign_extend


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
    # riding on top. With DC-block ENABLED (the store's pdm_dc_block; the app uses
    # Filter & Gain ▸ DC blocker), the bias is removed so the tone survives
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

    assert settled(np.ones(300_000)) == 32767            # all-ones -> +1 DC
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


def test_a_pdm_stream_at_full_scale_is_kept_unclipped(monkeypatch):
    """The decode rounds but does not clip: a biased stream whose signal reaches full scale
    keeps both sides, so Filter & Gain's DC blocker can still centre it."""
    import swi3s_studio.dsp as dsp
    from swi3s_studio.store.audio_store import decode_pdm
    loud = np.array([0.45 + 0.6, 0.45 - 0.6, 0.45])            # a bias plus a loud tone
    monkeypatch.setattr(dsp, "pdm_to_pcm", lambda *a, **k: loud.copy())
    pcm, _rate, _m = decode_pdm(np.zeros(64, dtype=np.int64), 3_072_000.0)
    assert pcm.max() > 32767 and pcm.tolist() == [int(round(v * 32767.0)) for v in loud]


def test_wav_export_clips_rather_than_wraps(tmp_path):
    """A sample past the container's range clips at full scale: assigned into int16 it
    would wrap round to the other end."""
    import wav_reader

    from swi3s_studio.store.audio_store import AudioStore
    vals = np.array([40000, -40000, 1000, 0], dtype=np.int64)
    cols = {"device": np.zeros(4, dtype=np.int64), "dp": np.ones(4, dtype=np.int64),
            "channel": np.zeros(4, dtype=np.int64), "sample_size": np.full(4, 16),
            "value": vals & 0xFFFF, "index": np.arange(4), "start_sample": np.arange(4) * 10}
    store = AudioStore.from_audio_columns(cols, {(0, 1): 48000.0})
    store._channels[(0, 1, 0)] = vals            # past 16 bits, as a full-scale PDM can be
    path = str(tmp_path / "a.wav")
    store.export_wav(0, 1, path)
    with wav_reader.open(path) as w:
        got = np.frombuffer(w.readframes(4), dtype="<i2")
    assert got.tolist() == [32767, -32768, 1000, 0]


def _audio_rows(sizes, values, starts):
    return [dict(device=0, dp=1, channel=0, sample_size=int(sz), value=int(v), index=i,
                 start_sample=int(at))
            for i, (sz, v, at) in enumerate(zip(sizes, values, starts))]


def test_sample_size_change_sign_extends_each_run_by_its_own_size():
    """A 16-bit run followed by a 24-bit run: each run is two's complement at its own
    width, and the 16-bit run is MSB-aligned to the channel's 24-bit full scale."""
    rows = _audio_rows([16, 16, 24, 24], [0x8000, 0x7FFF, 0x7FFFFF, 0x008000],
                       [0, 100, 200, 300])
    st = AudioStore.from_audio(rows, {(0, 1): 48000.0})
    assert st.samples(0, 1, 0).tolist() == [-0x800000, 0x7FFF00, 0x7FFFFF, 0x008000]
    assert st.sample_bits(0, 1, 0) == 24
    assert st.native_sample_bits(0, 1, 0) == 24
    assert st.sample_positions(0, 1, 0).tolist() == [0, 100, 200, 300]


def test_pdm_run_then_pcm_run_decodes_each_by_its_own_size():
    """A 1-bit PDM run followed by a 24-bit PCM run: only the PDM run goes through the
    PDM decoder; the PCM run is sign-extended at 24 bits."""
    cap = 12_288_000.0
    pdm_rate = 3_072_000.0
    step = int(cap / pdm_rate)
    t = np.arange(int(pdm_rate * 0.05)) / pdm_rate
    bits = _sigma_delta_1bit(0.5 * np.sin(2 * np.pi * 1000.0 * t))
    pcm = [0xC00000] * 16                          # -0x400000 at 24 bits
    pdm_end = bits.size * step
    starts = list(range(0, pdm_end, step)) + [pdm_end + 256 * k for k in range(16)]
    rows = _audio_rows([1] * bits.size + [24] * 16, list(bits) + pcm, starts)
    st = AudioStore.from_audio(rows, {}, capture_rate_hz=cap)
    out = st.samples(0, 1, 0)
    assert st.sample_bits(0, 1, 0) == 24
    assert out[-16:].tolist() == [-0x400000] * 16
    head = out[:-16].astype(np.float64) / (1 << 23)
    assert head.size == bits.size // 64            # decimated 3.072 MHz -> 48 kHz
    assert abs(_dominant_hz(head[200:], 48000.0) - 1000.0) < 50.0
    assert 0.35 < np.abs(head[200:]).max() < 0.65
    pos = st.sample_positions(0, 1, 0)
    assert np.all(np.diff(pos) >= 0)
    assert pos[-16:].tolist() == starts[-16:]


def test_sign_extend_2bit_is_twos_complement_not_a_density_code():
    assert list(sign_extend(np.array([0, 1, 2, 3]), 2)) == [0, 1, -2, -1]


def test_a_native_rate_just_above_the_target_decimates_by_at_least_two():
    """60 kHz to 48 kHz rounds to a factor of 1; the decimation is never below 2."""
    bits = (np.arange(6000) % 3 == 0).astype(np.uint8)
    signed, out_rate, M = decode_pdm(bits, 60_000.0)
    assert M == 2 and out_rate == 30_000.0
    assert abs(signed.size - bits.size // 2) <= 1


def test_a_native_rate_equal_to_the_target_is_not_decimated():
    bits = np.array([1, 0, 0, 1, 1, 0], dtype=np.uint8)
    signed, out_rate, M = decode_pdm(bits, 48_000.0)
    assert (M, out_rate) == (1, 48_000.0)
    assert signed.tolist() == [1, -1, -1, 1, 1, -1]


def test_playback_scales_a_2bit_channel_from_its_own_width():
    store = AudioStore()
    store._channels[(0, 0, 0)] = np.array([-2, -1, 0, 1], dtype=np.int64)
    store._sample_bits[(0, 0, 0)] = 2
    store._rates[(0, 0)] = 48000.0
    inter, _rate, nch = store.pcm_int16(0, 0)
    assert nch == 1 and inter.tolist() == [-32768, -16384, 0, 16384]
    raw, _rate, nch, fmt = store.pcm_play(0, 0, depth=16)
    assert fmt == "Int16" and np.frombuffer(raw, dtype=np.int16).tolist() == inter.tolist()


def test_a_pcm_channel_with_a_pdm_run_plays_at_the_decimated_rate():
    """The PDM run's decimated rate is the port's rate, with the bit rate kept as the
    native rate, so playback pitch and the WAV header are not the bit rate."""
    cap = 12_288_000.0
    step = 4
    t = np.arange(int(3_072_000.0 * 0.02)) / 3_072_000.0
    bits = _sigma_delta_1bit(0.5 * np.sin(2 * np.pi * 1000.0 * t))
    pdm_end = bits.size * step
    starts = list(range(0, pdm_end, step)) + [pdm_end + 256 * k for k in range(16)]
    rows = _audio_rows([1] * bits.size + [24] * 16, list(bits) + [0xC00000] * 16, starts)
    for known in ({(0, 1): 3_072_000.0}, {}):
        st = AudioStore.from_audio(rows, known, capture_rate_hz=cap)
        assert st.rate(0, 1) == pytest.approx(48000.0)
        assert st.native_rate(0, 1) == pytest.approx(3_072_000.0, rel=0.01)
