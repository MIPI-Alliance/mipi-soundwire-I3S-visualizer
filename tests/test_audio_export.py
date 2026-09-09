"""Audio store + WAV export test (device-keyed):

    decode demo -> AudioStore -> export per-(device,dataport) WAV -> read back
    with `wave` -> compare to the generated sine. The demo has one device with four
    data ports: dp0/dp1 = mono 16-bit PCM (48 kHz), dp2/dp3 = 1-bit PDM.

Run: python3 -m pytest tests/test_audio_export.py
"""
import math
import os
import tempfile
import wave

import numpy as np
import swi3score

from swi3s_studio.ingest import transitions
from swi3s_studio.store.audio_store import AudioStore, container_bits, sign_extend


def _expected_sine_signed(tone, index, field_bits):
    # field_bits = SampleSize register value = sample_bits - 1. `tone` is the sine
    # selector the demo uses: for a data port `dp`, tone == dp*2 on its single channel.
    amp = (1 << field_bits) * 0.45
    v = int(round(amp * math.sin(2.0 * math.pi * (tone + 1) * index / 64.0)))
    unsigned = v & ((1 << (field_bits + 1)) - 1)
    return int(sign_extend(np.array([unsigned]), field_bits + 1)[0])


def _decode_store(n=32):
    s = swi3score.DecoderSettings()
    d = swi3score.Decoder(transitions.demo_capture(n).sample_source(), s)
    d.run()
    return AudioStore.from_audio(d.audio(), d.audio_sample_rates())


def test_store_shapes_and_rate():
    store = _decode_store()
    # One device: dp0/dp1 mono 16-bit PCM, dp2/dp3 PDM.
    assert store.streams() == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert store.channels(0, 0) == [0]
    assert store.channels(0, 1) == [0]
    assert store.rate(0, 0) > 0
    assert store.samples(0, 0, 0).size >= 16
    assert store.sample_bits(0, 0, 0) == 16
    assert container_bits(16) == 16


def test_wav_roundtrip_matches_sine():
    store = _decode_store()
    field_bits = store.sample_bits(0, 0, 0) - 1          # 15 (16-bit PCM)
    with tempfile.TemporaryDirectory() as d:
        paths = store.export_all(d, prefix="swi3s")
        assert len(paths) == 4              # four data ports
        dev0dp0 = os.path.join(d, "swi3s_dev0_dp0.wav")
        assert dev0dp0 in paths
        with wave.open(dev0dp0, "rb") as w:
            assert w.getnchannels() == 1                 # mono (ch0 only)
            assert w.getsampwidth() == 2
            assert w.getframerate() == int(round(store.rate(0, 0)))
            n = w.getnframes()
            raw = w.readframes(n)
    frames = np.frombuffer(raw, dtype="<i2")
    # dp0 carries tone == dp*2 == 0 on its single channel.
    for i in range(min(frames.size, 32)):
        exp = _expected_sine_signed(0, i, field_bits)
        assert int(frames[i]) == exp, f"[{i}] wav={frames[i]} exp={exp}"

def test_playback_pads_unequal_channels_to_max():
    # pcm_play / pcm_int16 must zero-fill shorter channels to the LONGEST (like
    # export_wav), not truncate to the shortest — otherwise playback drops tail
    # audio the exported WAV keeps.
    store = AudioStore()
    store._channels[(0, 0, 0)] = np.array([100, 200, 300, 400], dtype=np.int64)  # 4 samples
    store._channels[(0, 0, 1)] = np.array([500, 600], dtype=np.int64)            # 2 samples
    store._sample_bits[(0, 0, 0)] = 16
    store._sample_bits[(0, 0, 1)] = 16
    store._rates[(0, 0)] = 48000.0

    inter, rate, nch = store.pcm_int16(0, 0)
    assert nch == 2 and rate == 48000
    assert inter.size == 4 * 2, "should pad to the longest channel (4), not the shortest (2)"
    frames = inter.reshape(-1, 2)
    assert list(frames[:, 0]) == [100, 200, 300, 400]     # longer channel intact
    assert list(frames[:, 1]) == [500, 600, 0, 0]         # shorter channel zero-filled

    raw, prate, pch, qfmt = store.pcm_play(0, 0, depth=16)
    assert pch == 2 and prate == 48000 and qfmt == "Int16"
    assert len(raw) == 4 * 2 * 2               # frames × channels × 2 bytes (int16)


def test_export_subset_and_range():
    store = _decode_store()
    with tempfile.TemporaryDirectory() as d:
        mono = store.export_wav(0, 1, os.path.join(d, "mono.wav"), channels=[0])
        with wave.open(mono, "rb") as w:
            assert w.getnchannels() == 1
        ranged = store.export_wav(0, 0, os.path.join(d, "range.wav"), index_range=(4, 14))
        with wave.open(ranged, "rb") as w:
            assert w.getnchannels() == 1                 # mono
            assert w.getnframes() == 10
    i0, i1 = store.index_range_for_samples(0, 0, 0, 0, 10**12)
    assert i0 == 0 and i1 == store.samples(0, 0, 0).size


def test_pcm_int16_for_playback():
    store = _decode_store()
    pcm, rate, nch = store.pcm_int16(0, 0)          # mono
    assert nch == 1 and rate == int(round(store.rate(0, 0)))
    assert pcm.dtype == np.int16
    # Mono: samples aren't interleaved — check the single channel tracks the source
    # samples scaled to full-scale int16.
    bits = store.sample_bits(0, 0, 0)
    scale = 32767.0 / float(1 << (bits - 1))
    src0 = store.samples(0, 0, 0)
    exp0 = np.clip(np.round(src0[:pcm.size].astype(float) * scale), -32768, 32767)
    assert np.array_equal(pcm.astype(float), exp0)
    # A windowed start drops the leading samples.
    pcm2, _r, nch2 = store.pcm_int16(0, 0, channels=[0], start=4)
    assert nch2 == 1 and pcm2.size == max(0, store.samples(0, 0, 0).size - 4)
    # Full-scale never clips past the int16 range.
    assert pcm.max() <= 32767 and pcm.min() >= -32768


def test_export_resampled():
    """export_wav(target_rate=…) writes at the requested rate with a resampled
    frame count (band-limited; see dsp.resample)."""
    from swi3s_studio.dsp import resampled_length
    store = _decode_store(64)
    native = int(round(store.rate(0, 0)))
    target = native // 2
    with tempfile.TemporaryDirectory() as d:
        path = store.export_wav(0, 0, os.path.join(d, "half.wav"), target_rate=target)
        with wave.open(path, "rb") as w:
            assert w.getframerate() == target
            assert w.getnchannels() == 1                 # mono
            exp = resampled_length(store.samples(0, 0, 0).size, native, target)
            assert w.getnframes() == exp, (w.getnframes(), exp)


def test_pcm_play_depths():
    """pcm_play returns 16-bit (Int16) or full-scale-32 (Int32 container for
    24-bit) interleaved bytes for playback; 24-bit doubles the byte width and
    keeps the Qt format name the play() path feeds QAudioFormat."""
    store = _decode_store()
    b16, r16, n16, f16 = store.pcm_play(0, 0, depth=16)   # mono
    b24, r24, n24, f24 = store.pcm_play(0, 0, depth=24)
    assert n16 == 1 and n24 == 1 and r16 == r24
    assert f16 == "Int16" and f24 == "Int32"
    # 24-bit uses a 4-byte int32 container vs 2-byte int16 -> exactly 2x bytes.
    assert len(b24) == 2 * len(b16)
    # 16-bit channel 0 matches the int16 playback scaling (mono: no de-interleave).
    pcm16 = np.frombuffer(b16, dtype="<i2")
    bits = store.sample_bits(0, 0, 0)
    scale = 32767.0 / float(1 << (bits - 1))
    src0 = store.samples(0, 0, 0)
    exp0 = np.clip(np.round(src0[: pcm16.size].astype(float) * scale),
                   -32768, 32767)
    assert np.array_equal(pcm16.astype(float), exp0)


def test_pcm_play_decimation():
    """pcm_play(target_rate=) band-limit-resamples for playback: the returned
    rate is the target and the frame count scales by target/native."""
    from swi3s_studio.dsp import resampled_length
    store = _decode_store(64)
    native = int(round(store.rate(0, 0)))
    target = native // 2
    b_nat, r_nat, n, f = store.pcm_play(0, 0, depth=16)
    b_dec, r_dec, n2, f2 = store.pcm_play(0, 0, depth=16, target_rate=target)
    assert r_nat == native and r_dec == target and n == n2
    nat_frames = len(b_nat) // (n * 2)
    dec_frames = len(b_dec) // (n2 * 2)
    exp = resampled_length(store.samples(0, 0, 0).size, native, target)
    assert dec_frames == exp, (dec_frames, exp)
    assert dec_frames < nat_frames               # decimated -> fewer frames


def test_the_session_releases_the_native_audio_once_it_has_copied_it():
    """The decode's own AudioSample vector is 72 B/sample and the Session copies all of it
    into NumPy columns (56 B/sample) at load — 449 MB against 349 MB on the 6.24M-sample
    demo, with no second reader for the native side. So the Session hands it back.

    Everything the app actually reads must survive that: the columns, the dict-list `audio`
    property built from them, the audio store, and WAV export (all fed by the columns; the
    per-DP rates come from the snooped config, not the samples). And reading the decoder's
    audio afterwards must RAISE — an empty list there would be indistinguishable from a
    capture that carried no audio, which is a wrong answer that looks like data."""
    import pytest

    from swi3s_studio.session import Session

    sess = Session.from_demo(32)
    assert sess.decoder.audio_released(), "the Session kept the native audio vector"
    with pytest.raises(RuntimeError, match="released"):
        sess.decoder.audio()
    with pytest.raises(RuntimeError, match="released"):
        sess.decoder.audio_columns()

    # The copy is intact, and every consumer downstream of it still works.
    assert sess.audio_count > 0
    assert sess.audio_columns()["dp"].shape[0] == sess.audio_count
    assert len(sess.audio) == sess.audio_count            # dict list, from the columns
    store = sess.audio_store()
    assert store.streams() == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert store.rate(0, 0) > 0                           # from audio_sample_rates (config)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "dev0_dp0.wav")
        store.export_wav(0, 0, path)
        with wave.open(path, "rb") as w:
            assert w.getnframes() > 0


def test_a_redecode_releases_the_fresh_vector_too():
    """Every decode copies and releases: a re-decode (scrambler/register what-if, SSP step)
    builds a NEW Decoder, so the release has to happen per decode, not once per Session."""
    from swi3s_studio.session import Session

    sess = Session.from_demo(32)
    before = sess.audio_count
    sess.set_scrambler_overrides({(0, 0): True})          # forces a re-decode
    assert sess.decoder.audio_released(), "the re-decode's vector was left behind"
    assert sess.audio_count == before
