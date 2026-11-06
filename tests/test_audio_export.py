"""Audio store + WAV export test (device-keyed):

    decode demo -> AudioStore -> export per-(device,dataport) WAV -> read back
    with tests/wav_reader.py -> compare to the generated sine. The demo has one device
    with four data ports: dp0 = 16-bit PCM at 44.1 kHz (payload skipping on 48 kHz
    opportunities), dp1 = 16-bit PCM at 48 kHz, dp2/dp3 = PDM decimated to 48 kHz.

Run: python3 -m pytest tests/test_audio_export.py
"""
import math
import os
import tempfile

import numpy as np
import pytest
import swi3score
import wav_reader

from swi3s_studio.ingest import transitions
from swi3s_studio.store.audio_store import AudioStore, container_bits, sign_extend, wav_header


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
        with wav_reader.open(dev0dp0, "rb") as w:
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
        with wav_reader.open(mono, "rb") as w:
            assert w.getnchannels() == 1
        ranged = store.export_wav(0, 0, os.path.join(d, "range.wav"), index_range=(4, 14))
        with wav_reader.open(ranged, "rb") as w:
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
    scale = 2.0 ** (16 - bits)
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
        with wav_reader.open(path, "rb") as w:
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
    scale = 2.0 ** (16 - bits)
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
    into NumPy columns (56 B/sample) at load, so the Session releases the native vector.

    Everything the app reads survives that: the columns, the dict-list `audio` property
    built from them, the audio store, and WAV export (all fed by the columns; the per-DP
    rates come from the snooped config, not the samples). Reading the decoder's audio
    afterwards RAISES, so it cannot read as a capture that carried no audio."""
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
        with wav_reader.open(path, "rb") as w:
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


def _sized_store(size, values, channels=1):
    """Device 0, dp 0 with `channels` channels of `size`-bit samples at 48 kHz."""
    rows = [{"device": 0, "dp": 0, "channel": ch, "sample_size": size,
             "value": int(v) & ((1 << size) - 1), "index": i, "start_sample": i * 10}
            for ch in range(channels) for i, v in enumerate(values)]
    return AudioStore.from_audio(rows, {(0, 0): 48000.0})


# (sample size, container bits, valid bits) for the three containers and a size inside one
_WAV_SIZES = [(8, 16, 8), (12, 16, 12), (16, 16, 16), (20, 24, 20), (24, 24, 24), (32, 32, 32)]


@pytest.mark.parametrize("size,container,valid", _WAV_SIZES)
def test_the_wav_is_extensible_pcm_with_the_ports_sample_size(tmp_path, size, container, valid):
    top = (1 << (size - 1)) - 1
    store = _sized_store(size, [0, 1, -1, top, -top - 1], channels=2)
    path = store.export_wav(0, 0, str(tmp_path / "x.wav"))
    w = wav_reader.open(path)
    assert w.format_tag == 0xFFFE and w.cb_size == 22
    assert w.subformat == wav_reader.SUBTYPE_PCM          # integer PCM
    assert w.nchannels == 2 and w.framerate == 48000
    assert w.container_bits == container and w.valid_bits == valid
    assert w.block_align == 2 * container // 8 and w.byte_rate == 48000 * w.block_align
    assert w.getnframes() == 5


@pytest.mark.parametrize("size,container,valid", _WAV_SIZES)
def test_the_samples_are_left_justified_in_the_container(tmp_path, size, container, valid):
    top = (1 << (size - 1)) - 1
    values = [0, 1, -1, 5, top, -top - 1]
    store = _sized_store(size, values)
    got = wav_reader.open(store.export_wav(0, 0, str(tmp_path / "x.wav"))).samples()[:, 0]
    shift = container - size
    assert got.tolist() == [v << shift for v in values]
    assert got.tolist()[-2:] == [(1 << (container - 1)) - (1 << shift), -(1 << (container - 1))]
    assert all(g % (1 << shift) == 0 for g in got.tolist())   # the low bits are zero


def test_a_narrower_channel_is_left_justified_by_its_own_size_in_a_mixed_export(tmp_path):
    """A 16-bit channel beside a 24-bit one lands in the 24-bit container at its own full
    scale, the level in-app playback gives it; a value past its size clips to it."""
    rows = [{"device": 0, "dp": 0, "channel": ch, "sample_size": size,
             "value": int(v) & ((1 << size) - 1), "index": i, "start_sample": i * 10}
            for ch, size, vals in ((0, 24, [0x400000, -0x400000, 0x100]),
                                   (1, 16, [0x4000, -0x4000, 1]))
            for i, v in enumerate(vals)]
    store = AudioStore.from_audio(rows, {(0, 0): 48000.0})
    assert (store.sample_bits(0, 0, 0), store.sample_bits(0, 0, 1)) == (24, 16)
    w = wav_reader.open(store.export_wav(0, 0, str(tmp_path / "x.wav")))
    assert w.container_bits == 24 and w.valid_bits == 24
    got = w.samples()
    assert got[:, 0].tolist() == [0x400000, -0x400000, 0x100]     # the 24-bit channel as is
    assert got[:, 1].tolist() == got[:, 0].tolist()                  # the same levels
    play = store.pcm_int16(0, 0)[0].reshape(-1, 2)
    assert play[:, 0].tolist() == play[:, 1].tolist()               # and in playback
    store._channels[(0, 0, 1)] = np.array([40000, -40000, 0], dtype=np.int64)
    over = wav_reader.open(store.export_wav(0, 0, str(tmp_path / "y.wav"))).samples()[:, 1]
    assert over.tolist() == [32767 << 8, -32768 << 8, 0]


def test_a_wav_of_odd_data_length_is_padded_to_a_word(tmp_path):
    store = _sized_store(24, [1, 2, 3])                   # 9 data bytes
    path = store.export_wav(0, 0, str(tmp_path / "x.wav"))
    w = wav_reader.open(path)                             # checks the RIFF size too
    assert len(w.data) == 9 and w.getnframes() == 3
    assert os.path.getsize(path) % 2 == 0


def test_a_value_past_the_sample_size_clips_to_it(tmp_path):
    store = _sized_store(12, [0, 0, 0])
    store._channels[(0, 0, 0)] = np.array([5000, -5000, 100], dtype=np.int64)
    got = wav_reader.open(store.export_wav(0, 0, str(tmp_path / "x.wav"))).samples()[:, 0]
    assert got.tolist() == [2047 << 4, -2048 << 4, 100 << 4]


def test_a_heavy_decimation_keeps_its_ratio_in_bounded_memory():
    """24.576 MHz -> 48 kHz is exactly 1/512; the input window is capped, so the peak stays
    under 200 MB however long the capture."""
    import tracemalloc

    from swi3s_studio.dsp import resample, resampled_length
    n = 4_000_000
    x = np.random.default_rng(1).standard_normal(n)
    tracemalloc.start()
    try:
        y = resample(x, 24_576_000, 48_000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert y.size == resampled_length(n, 24_576_000, 48_000) == -(-n // 512)
    assert peak < 200e6, peak / 1e6


def test_resample_blocking_does_not_change_the_samples(monkeypatch):
    import importlib
    core = importlib.import_module("swi3s_studio.dsp.resample")
    x = np.random.default_rng(2).standard_normal(60_000)
    whole = core.resample(x, 3_072_000, 48_000)
    monkeypatch.setattr(core, "_INPUT_WINDOW", 4096)
    assert np.allclose(core.resample(x, 3_072_000, 48_000), whole, atol=1e-9)


def test_the_wav_header_names_no_speaker_positions(tmp_path):
    path = tmp_path / "h.wav"
    path.write_bytes(wav_header(2, 48000, 16, 12, 8) + bytes(8))
    with wav_reader.open(str(path), "rb") as w:
        assert w.channel_mask == 0 and w.valid_bits == 12 and w.getnchannels() == 2


def test_a_wav_at_the_size_limit_is_written_and_one_byte_over_is_refused(tmp_path, monkeypatch):
    from swi3s_studio.store import audio_store
    store = AudioStore()
    store._channels[(0, 0, 0)] = np.arange(32, dtype=np.int64)
    store._sample_bits[(0, 0, 0)] = 16
    store._rates[(0, 0)] = 48000.0
    with monkeypatch.context() as patch:
        patch.setattr(audio_store, "_WAV_MAX_DATA_BYTES", 64)             # 32 samples x 2 bytes
        out = store.export_wav(0, 0, str(tmp_path / "ok.wav"))
        with wav_reader.open(out, "rb") as w:
            assert w.getnframes() == 32
        patch.setattr(audio_store, "_WAV_MAX_DATA_BYTES", 63)
        with pytest.raises(ValueError, match="4 GiB"):
            store.export_wav(0, 0, str(tmp_path / "over.wav"))


def _store_of(sizes):
    n = len(sizes)
    cols = {"device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
            "channel": np.zeros(n, np.int32), "sample_size": np.array(sizes, np.int32),
            "value": np.random.default_rng(0).integers(0, 2, n).astype(np.uint32),
            "index": np.arange(n, dtype=np.uint64),
            "start_sample": (np.arange(n) * 2000).astype(np.uint64)}
    return AudioStore.from_audio_columns(cols, {}, capture_rate_hz=96_000_000.0)


def test_a_pcm_channel_with_no_core_rate_measures_it_and_a_mixed_one_does_not():
    """16-bit samples 2000 capture samples apart at 96 MHz are 48 kHz. A channel that also
    carries 1-bit runs has no single sample spacing, so its rate stays unknown."""
    assert _store_of([16] * 1000).rate(0, 0) == pytest.approx(48000.0)
    assert _store_of([1] * 600 + [16] * 400).rate(0, 0) == 0.0


def _rated_store(rate, n_channels=1, size=16, device=3, dp=2):
    rows = [{"device": device, "dp": dp, "channel": ch, "sample_size": size, "value": i,
             "index": i, "start_sample": i * 10} for ch in range(n_channels) for i in range(4)]
    return AudioStore.from_audio(rows, {(device, dp): rate})


@pytest.mark.parametrize("rate, n_channels, size", [
    (2.0 ** 32, 1, 16), (2.0 ** 33, 1, 16),                  # the rate field overflows
    (2.0 ** 31, 1, 16), (2.0 ** 30, 2, 16),                  # only the byte rate overflows
    (2.0 ** 30, 1, 32), (2.0 ** 30, 2, 24)])
def test_a_wav_format_a_header_cannot_hold_is_refused_naming_the_stream(
        tmp_path, rate, n_channels, size):
    store = _rated_store(rate, n_channels, size)
    with pytest.raises(ValueError, match=r"device 3 dp 2: a WAV file cannot carry"):
        store.export_wav(3, 2, str(tmp_path / "x.wav"))
    assert not (tmp_path / "x.wav").exists()


def test_the_largest_wav_byte_rate_is_written(tmp_path):
    store = _rated_store(2.0 ** 31 - 1)                   # byte rate 2**32 - 2
    with wav_reader.open(store.export_wav(3, 2, str(tmp_path / "x.wav")), "rb") as w:
        assert w.getframerate() == 2 ** 31 - 1 and w.getnframes() == 4
    with pytest.raises(ValueError, match="byte rate"):
        wav_header(1, 2 ** 32 - 1, 16, 16, 0)
    with pytest.raises(ValueError, match="rate must be 1"):
        wav_header(1, 0, 16, 16, 0)


@pytest.mark.platform           # os.replace and the temporary file behave per OS
@pytest.mark.parametrize("fails_at", ["before_header", "after_header", "at_replace"])
def test_a_failed_export_leaves_an_existing_wav_untouched_and_no_temporary_file(
        tmp_path, monkeypatch, fails_at):
    """The WAV is complete before it replaces the destination: a write that fails before the
    header, after the header with the samples unwritten, or at the final replace keeps the
    previous file byte for byte and leaves nothing beside it."""
    from swi3s_studio.store import audio_store
    store = _rated_store(48000.0)
    dest = tmp_path / "x.wav"
    dest.write_bytes(b"previous contents")

    class Flaky:
        """A file whose second write (the samples, after the header) fails."""
        def __init__(self, f):
            self.f, self.writes = f, 0

        def write(self, data):
            self.writes += 1
            if self.writes == 2:
                raise OSError("disk full")
            return self.f.write(data)

    real_write_whole = audio_store.write_whole
    with monkeypatch.context() as patch:
        if fails_at == "before_header":
            patch.setattr(audio_store, "wav_header",
                          lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        elif fails_at == "after_header":
            patch.setattr(audio_store, "write_whole",
                          lambda path, write, *a, **k: real_write_whole(
                              path, lambda f: write(Flaky(f)), *a, **k))
        else:
            patch.setattr(os, "replace",
                          lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            store.export_wav(3, 2, str(dest))
    assert dest.read_bytes() == b"previous contents"
    assert [p.name for p in tmp_path.iterdir()] == ["x.wav"]
    store.export_wav(3, 2, str(dest))
    assert dest.read_bytes()[:4] == b"RIFF"
    assert [p.name for p in tmp_path.iterdir()] == ["x.wav"]


@pytest.mark.platform           # file modes behave per OS
def test_an_export_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    dest = tmp_path / "x.wav"
    dest.write_bytes(b"old")
    os.chmod(dest, 0o640)
    _rated_store(48000.0).export_wav(3, 2, str(dest))
    assert (os.stat(dest).st_mode & 0o777) == (0o640 if os.name != "nt" else 0o666)


@pytest.mark.platform           # os.replace and the temporary file behave per OS
def test_write_whole_writes_text_or_bytes_and_falls_back_when_the_folder_refuses(
        tmp_path, monkeypatch):
    from swi3s_studio.fsutil import write_whole
    text = tmp_path / "t.txt"
    write_whole(str(text), lambda f: f.write("a\nb\n"), mode="w", encoding="utf-8",
                newline="", fsync=True)
    assert text.read_bytes() == b"a\nb\n"
    binary = tmp_path / "b.bin"
    write_whole(str(binary), lambda f: f.write(b"\x00\x01"))
    assert binary.read_bytes() == b"\x00\x01"
    real_open = os.open

    def refuse(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise PermissionError("no second file here")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", refuse)
    write_whole(str(binary), lambda f: f.write(b"direct"))
    assert binary.read_bytes() == b"direct"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.bin", "t.txt"]


@pytest.mark.platform           # file-name limits are per file system
def test_write_whole_writes_a_name_too_long_for_its_temporary_file(tmp_path):
    from swi3s_studio.fsutil import write_whole
    name = None
    for length in range(255, 200, -1):               # the longest name the file system takes
        candidate = tmp_path / ("a" * (length - 4) + ".csv")
        try:
            candidate.write_text("x")
        except OSError:
            continue
        name = candidate
        break
    if name is None or len(name.name) + 14 <= 255:
        pytest.skip("this file system takes names longer than the temporary file's")
    write_whole(str(name), lambda f: f.write("new"), mode="w", encoding="utf-8")
    assert name.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == [name.name]


@pytest.mark.platform           # the null device is per OS
def test_write_whole_writes_the_null_device_directly(tmp_path):
    """A target that is not a regular file, os.devnull ('nul' on Windows) included, is
    written directly: no temporary file is made beside it and no permission check refuses
    it."""
    from swi3s_studio.fsutil import write_whole
    write_whole(os.devnull, lambda f: f.write("discarded"), mode="w", encoding="utf-8")
    write_whole(os.devnull, lambda f: f.write(b"discarded"))


@pytest.mark.platform           # write permission is per OS and account
def test_write_whole_refuses_a_target_the_user_cannot_write_and_leaves_it_alone(tmp_path):
    from swi3s_studio.fsutil import write_whole
    target = tmp_path / "protected.txt"
    target.write_text("old")
    os.chmod(target, 0o444)
    if os.access(target, os.W_OK):
        pytest.skip("this account writes read-only files (superuser)")
    try:
        with pytest.raises(PermissionError):
            write_whole(str(target), lambda f: f.write("new"), mode="w", encoding="utf-8")
        assert target.read_text() == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["protected.txt"]
    finally:
        os.chmod(target, 0o644)


def test_playback_keeps_a_sixteen_bit_stream_bit_exact_and_left_justifies_the_rest():
    """A 16-bit stream plays unchanged, full scale included; a 24-bit stream plays as its top
    16 bits; pcm_play at 24-bit depth carries a 24-bit stream in the top 24 bits of int32."""
    rows = [{"device": 0, "dp": 0, "channel": 0, "sample_size": 16, "value": v & 0xFFFF,
             "index": i, "start_sample": i * 10}
            for i, v in enumerate([32767, -32768, 1000, -1])]
    store = AudioStore.from_audio(rows, {(0, 0): 48000.0})
    assert store.pcm_int16(0, 0)[0].tolist() == [32767, -32768, 1000, -1]
    wide = np.frombuffer(store.pcm_play(0, 0, depth=24)[0], dtype="<i4")
    assert wide.tolist() == [32767 << 16, -32768 << 16, 1000 << 16, -1 << 16]
    rows = [{"device": 0, "dp": 0, "channel": 0, "sample_size": 24, "value": v & 0xFFFFFF,
             "index": i, "start_sample": i * 10}
            for i, v in enumerate([8388607, -8388608, 256, 255])]
    store = AudioStore.from_audio(rows, {(0, 0): 48000.0})
    assert store.pcm_int16(0, 0)[0].tolist() == [32767, -32768, 1, 1]
    wide = np.frombuffer(store.pcm_play(0, 0, depth=24)[0], dtype="<i4")
    assert wide.tolist() == [8388607 << 8, -8388608 << 8, 256 << 8, 255 << 8]


def test_the_wav_reader_checks_the_data_chunk_against_what_the_file_holds(tmp_path):
    """The reader that checks exported headers refuses a data size that disagrees with the
    bytes written, even when the RIFF size matches the file length."""
    import struct
    good = wav_header(1, 48000, 16, 16, 8) + bytes(8)
    path = tmp_path / "x.wav"
    path.write_bytes(good)
    assert wav_reader.open(str(path)).getnframes() == 4
    data_at = good.index(b"data") + 4
    for size, why in ((12, "chunk runs past the end"), (7, "whole number of frames"),
                      (6, "chunk header runs past")):
        bad = good[:data_at] + struct.pack("<I", size) + good[data_at + 4:]
        path.write_bytes(bad)
        with pytest.raises(AssertionError, match=why):
            wav_reader.open(str(path))
    path.write_bytes(good + b"\0\0\0\0")                   # trailing bytes after the data
    with pytest.raises(AssertionError, match="RIFF size is wrong"):
        wav_reader.open(str(path))


def test_pcm_play_interleaves_each_channel_by_value_and_zero_fills_the_tail():
    store = AudioStore()
    store._channels[(0, 0, 0)] = np.array([100, -200, 300, 400], dtype=np.int64)
    store._channels[(0, 0, 3)] = np.array([500, 600], dtype=np.int64)
    store._channels[(0, 0, 1)] = np.array([-7, 8, 9], dtype=np.int64)
    for ch in (0, 1, 3):
        store._sample_bits[(0, 0, ch)] = 16
    store._rates[(0, 0)] = 48000.0
    raw, _rate, nch, fmt = store.pcm_play(0, 0)
    assert (nch, fmt) == (3, "Int16")
    assert np.frombuffer(raw, dtype="<i2").reshape(-1, 3).tolist() == [
        [100, -7, 500], [-200, 8, 600], [300, 9, 0], [400, 0, 0]]
    raw, _rate, nch, fmt = store.pcm_play(0, 0, channels=[3, 0], depth=24)
    assert (nch, fmt) == (2, "Int32")
    assert np.frombuffer(raw, dtype="<i4").reshape(-1, 2).tolist() == [
        [500 << 16, 100 << 16], [600 << 16, -200 << 16], [0, 300 << 16], [0, 400 << 16]]


def test_a_pdm_decimation_factor_is_the_rounded_ratio_of_the_bit_rate_to_the_pcm_rate():
    from swi3s_studio.store.audio_store import decode_pdm
    bits = np.random.default_rng(5).integers(0, 2, 59 * 40)
    pcm, out_rate, m = decode_pdm(bits, 2_822_400.0)         # 58.8 input bits per output
    assert m == 59 and out_rate == pytest.approx(2_822_400.0 / 59)
    assert pcm.size == pytest.approx(bits.size / 59, abs=1)
    _pcm, out_rate, m = decode_pdm(bits, 2_822_400.0 * 58.4 / 58.8)   # 58.4 rounds down
    assert m == 58


def test_evenly_spaced_runs_round_positions_to_the_nearest_capture_sample():
    from swi3s_studio.store.audio_store import _even_runs
    out = _even_runs(np.array([0, 1, 2, 10]), np.zeros(0, dtype=np.int64))
    assert out.tolist() == [0, 3, 7, 10]                       # 10/3 and 20/3 round up or down
    out = _even_runs(np.array([5, 6, 7, 8, 100, 101, 102, 109]), np.array([3]))
    assert out[:4].tolist() == [5, 6, 7, 8] and out[4:].tolist() == [100, 103, 106, 109]


def _stepped(step, jump, at, n=80):
    """Positions `step` apart with the step after index `at` replaced by `jump`."""
    steps = np.full(n, step, dtype=np.int64)
    steps[at] = jump
    return np.concatenate(([0], np.cumsum(steps)))


@pytest.mark.parametrize("flow, tolerance", [(False, 4.0), (True, 32.0)])
def test_a_transport_step_is_a_gap_only_past_the_tolerance_times_the_nominal_step(
        flow, tolerance):
    from swi3s_studio.store.audio_store import _gap_indices
    at = 40
    exactly = _stepped(10, int(10 * tolerance), at)
    assert _gap_indices(exactly, flow=flow).tolist() == []
    past = _stepped(10, int(10 * tolerance) + 1, at)
    assert _gap_indices(past, flow=flow).tolist() == [at]
    assert _gap_indices(exactly, flow=flow, tolerance=tolerance - 1).tolist() == [at]


def test_a_recovered_rate_is_the_median_of_three_probes_not_their_mean():
    from swi3s_studio.store.audio_store import _RATE_RECOVERY_SUBSAMPLE, _recover_rate
    n = 3 * _RATE_RECOVERY_SUBSAMPLE
    at = np.arange(n, dtype=np.int64) * 10
    at[100:] += 40_000                                       # a reconfig gap inside the head probe
    assert _recover_rate(at, 1000.0) == pytest.approx(100.0)  # the mid and tail probes, 10 apart
    clean = np.arange(n, dtype=np.int64) * 10
    assert _recover_rate(clean, 1000.0) == pytest.approx(100.0)


def test_a_pdm_overshoot_in_a_wide_mixed_channel_is_held_at_full_scale_in_a_memmap(tmp_path):
    """A PDM run is decimated without clipping, so its overshoot, left-justified into a 32-bit
    channel, passes the int32 range: a memmap-backed channel clips it as the in-memory one's
    export does, rather than wrapping its sign."""
    zeros, ones, pcm32 = 59 * 100, 59 * 300, 50
    n = zeros + ones + pcm32
    sizes = np.r_[np.ones(zeros + ones), np.full(pcm32, 32)].astype(np.int32)
    value = np.r_[np.zeros(zeros), np.ones(ones), np.arange(pcm32)].astype(np.uint32)
    cols = {"device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
            "channel": np.zeros(n, np.int32), "sample_size": sizes, "value": value,
            "index": np.arange(n, dtype=np.uint64),
            "start_sample": (np.arange(n) * 8).astype(np.uint64)}
    plain = AudioStore.from_audio_columns(cols, {(0, 0): 2_822_400.0},
                                          capture_rate_hz=8 * 2_822_400.0)
    mapped = AudioStore.from_audio_columns(cols, {(0, 0): 2_822_400.0},
                                           capture_rate_hz=8 * 2_822_400.0,
                                           mmap_dir=str(tmp_path))
    try:
        want = plain.samples(0, 0, 0)
        assert want.max() > 2 ** 31 - 1                        # the overshoot is real
        got = np.asarray(mapped.samples(0, 0, 0))
        assert got.tolist() == np.clip(want, -2 ** 31, 2 ** 31 - 1).tolist()
        assert got.max() == 2 ** 31 - 1 and got.min() < 0 and np.argmax(got) == np.argmax(want)
    finally:
        mapped.close()
