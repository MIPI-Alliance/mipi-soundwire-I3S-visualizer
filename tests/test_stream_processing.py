"""Per-stream Filter & Gain (DC blocker, high-pass, gain) in the audio store: what is applied, what it reaches, and
that Revert gives back the decoded samples exactly."""
import os
import tempfile
import wave

import numpy as np
import pytest

from swi3s_studio.dsp import filters
from swi3s_studio.store.audio_store import AudioStore, StreamProcessing, full_scale

RATE = 48_000.0


def _store(signals, bits=16, mmap_dir=None, other=True, rate=RATE, capture_rate_hz=0.0,
           positions=None):
    """Device 1 DP 2 with one channel per signal (and, with `other`, a second stream that
    nothing here touches), from columns as the decoder delivers them."""
    cols = {k: [] for k in ("device", "dp", "channel", "sample_size", "value", "index",
                            "start_sample")}

    def add(dev, dp, ch, x):
        n = len(x)
        cols["device"].append(np.full(n, dev))
        cols["dp"].append(np.full(n, dp))
        cols["channel"].append(np.full(n, ch))
        cols["sample_size"].append(np.full(n, bits))
        cols["value"].append(np.asarray(x, dtype=np.int64) & ((1 << bits) - 1))
        cols["index"].append(np.arange(n))
        cols["start_sample"].append(np.arange(n) * 10 if positions is None else positions)
    for ch, x in enumerate(signals):
        add(1, 2, ch, x)
    rates = {(1, 2): rate}
    if other:
        add(3, 0, 0, signals[0])
        rates[(3, 0)] = RATE
    cols = {k: np.concatenate(v) for k, v in cols.items()}
    return AudioStore.from_audio_columns(cols, rates, mmap_dir=mmap_dir,
                                         capture_rate_hz=capture_rate_hz)


def _tone(n=48_001, amp=1000.0, dc=4000.0, f=1000.0):
    """Starts and ends on a zero crossing (48,001 samples of 1 kHz at 48 kHz), so a
    high-pass leaves no edge response and the expected values are exact."""
    t = np.arange(n)
    return np.rint(dc + amp * np.sin(2 * np.pi * f * t / RATE)).astype(np.int64)


def test_a_stream_is_not_processed_until_asked():
    store = _store([_tone()])
    assert store.processing(1, 2) is None
    np.testing.assert_array_equal(store.samples(1, 2, 0), store.raw_samples(1, 2, 0))


def test_the_high_pass_removes_the_offset_and_keeps_the_tone():
    store = _store([_tone()])
    store.set_processing(1, 2, StreamProcessing(highpass_hz=20.0))
    y = np.asarray(store.samples(1, 2, 0), dtype=np.float64)
    mid = y[2000:-2000]
    assert abs(mid.mean()) < 1.0                          # the 4000 offset is gone
    assert np.max(np.abs(mid)) == pytest.approx(1000.0, abs=2.0)


def test_the_samples_are_the_filter_then_the_gain_rounded():
    x = _tone()
    store = _store([x])
    store.set_processing(1, 2, StreamProcessing(highpass_hz=50.0, gain_db=6.0))
    want = np.rint(filters.highpass(x.astype(np.float64), 50.0, RATE) * 10 ** (6.0 / 20))
    np.testing.assert_array_equal(store.samples(1, 2, 0), want.astype(np.int32))


def test_gain_alone_needs_no_filter():
    x = _tone(dc=0.0)
    store = _store([x])
    store.set_processing(1, 2, StreamProcessing(gain_db=-6.0206))
    np.testing.assert_allclose(store.samples(1, 2, 0), np.rint(x / 2.0), atol=1)


def test_a_gain_past_full_scale_saturates_as_a_dac_would():
    store = _store([_tone(dc=0.0, amp=20_000.0)])
    store.set_processing(1, 2, StreamProcessing(gain_db=12.0, allow_clipping=True))
    y = np.asarray(store.samples(1, 2, 0))
    assert y.max() == 32767 and y.min() == -32768


def test_every_channel_of_the_stream_gets_the_same_setting_and_no_other_stream_does():
    a, b = _tone(), _tone(amp=300.0, dc=-2000.0, f=440.0)
    store = _store([a, b])
    other = np.array(store.samples(3, 0, 0))
    proc = StreamProcessing(highpass_hz=20.0, gain_db=3.0)
    store.set_processing(1, 2, proc)
    for ch, x in enumerate((a, b)):
        want = np.rint(filters.highpass(x.astype(np.float64), 20.0, RATE) * 10 ** (3 / 20))
        np.testing.assert_array_equal(store.samples(1, 2, ch), want.astype(np.int32))
    np.testing.assert_array_equal(store.samples(3, 0, 0), other)
    assert store.processing(1, 2) == proc and store.processing(3, 0) is None


@pytest.mark.parametrize("revert", [None, StreamProcessing()], ids=["None", "identity"])
def test_revert_gives_back_the_decoded_samples_exactly(revert):
    x = _tone()
    store = _store([x])
    decoded = np.array(store.samples(1, 2, 0))
    store.set_processing(1, 2, StreamProcessing(highpass_hz=1.0, gain_db=20.0,
                                                allow_clipping=True))
    store.set_processing(1, 2, revert)
    assert store.processing(1, 2) is None
    np.testing.assert_array_equal(store.samples(1, 2, 0), decoded)


def test_a_new_setting_starts_from_the_decoded_samples_not_the_last_result():
    x = _tone()
    store = _store([x])
    store.set_processing(1, 2, StreamProcessing(gain_db=-20.0))
    store.set_processing(1, 2, StreamProcessing(gain_db=6.0))
    want = np.rint(x * 10 ** (6 / 20)).astype(np.int32)
    np.testing.assert_array_equal(store.samples(1, 2, 0), want)


def test_the_waveform_follows_the_processing():
    """The render pyramid and the envelope memo are both derived from the samples."""
    store = _store([_tone()])
    before = store.envelope(1, 2, 0, 0, 48_000, max_points=64)
    store.set_processing(1, 2, StreamProcessing(highpass_hz=20.0))
    after = store.envelope(1, 2, 0, 0, 48_000, max_points=64)
    assert after[2].max() < before[2].max() - 3000       # the offset left the envelope
    store.set_processing(1, 2, None)
    again = store.envelope(1, 2, 0, 0, 48_000, max_points=64)
    for b, a in zip(before, again):
        np.testing.assert_array_equal(b, a)


def test_playback_and_export_follow_the_processing(tmp_path):
    store = _store([_tone(dc=0.0, amp=1000.0)])
    store.set_processing(1, 2, StreamProcessing(gain_db=20.0))
    pcm, _rate, nch, _fmt = store.pcm_play(1, 2)
    assert nch == 1 and np.max(np.abs(np.frombuffer(pcm, dtype=np.int16))) == 10_000
    path = store.export_wav(1, 2, str(tmp_path / "x.wav"))
    with wave.open(path) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert np.max(np.abs(data)) == 10_000


def test_the_peak_is_measured_after_the_high_pass_and_before_the_gain():
    x = _tone(dc=4000.0, amp=1000.0)
    store = _store([x])
    assert store.peak_db(1, 2) == pytest.approx(20 * np.log10(5000 / 32768), abs=0.01)
    assert store.peak_db(1, 2, highpass_hz=20.0) == pytest.approx(
        20 * np.log10(1000 / 32768), abs=0.05)
    store.set_processing(1, 2, StreamProcessing(gain_db=12.0))
    assert store.peak_db(1, 2) == pytest.approx(20 * np.log10(5000 / 32768), abs=0.01)


def test_the_normalising_gain_lands_the_peak_on_full_scale():
    x = _tone(dc=0.0, amp=1234.0)
    store = _store([x])
    gain = -store.peak_db(1, 2)
    store.set_processing(1, 2, StreamProcessing(gain_db=gain))
    assert np.max(np.abs(store.samples(1, 2, 0))) == 32768 - 1 or \
        np.max(np.abs(store.samples(1, 2, 0))) == 32768


def test_a_silent_stream_has_no_peak():
    assert _store([np.zeros(1000, dtype=np.int64)]).peak_db(1, 2) is None


def test_full_scale_follows_the_sample_width():
    assert full_scale(16) == 32768 and full_scale(24) == 1 << 23
    store = _store([_tone(dc=0.0, amp=3_000_000.0)], bits=24)
    store.set_processing(1, 2, StreamProcessing(gain_db=12.0))
    assert np.max(store.samples(1, 2, 0)) == (1 << 23) - 1


def test_a_memory_mapped_store_reverts_and_closes():
    with tempfile.TemporaryDirectory() as d:
        store = _store([_tone()], mmap_dir=d)
        decoded = np.array(store.samples(1, 2, 0))
        store.set_processing(1, 2, StreamProcessing(highpass_hz=20.0))
        store.set_processing(1, 2, None)
        np.testing.assert_array_equal(store.samples(1, 2, 0), decoded)
        store.set_processing(1, 2, StreamProcessing(gain_db=1.0))
        store.close()
        for name in os.listdir(d):
            os.remove(os.path.join(d, name))             # every handle was released


def test_the_setting_round_trips_and_a_bad_one_is_refused():
    p = StreamProcessing(highpass_hz=20.0, gain_db=-3.5, allow_clipping=True)
    assert StreamProcessing.from_json(p.to_json()) == p
    assert StreamProcessing.from_json({"gain_db": 2}) == StreamProcessing(gain_db=2.0)
    for bad in ({"highpass_hz": "x"}, {"highpass_hz": -5}, None, {"gain_db": [1]}):
        assert StreamProcessing.from_json(bad) is None
    assert p.describe() == "HPF 20 Hz, -3.5 dB"
    assert StreamProcessing(gain_db=0.0).is_identity()


def test_a_port_the_core_gave_no_rate_is_measured_and_can_be_filtered():
    """A real capture can leave a port's rate unverified (0). The samples' capture positions
    and the capture rate still give it: 10 capture samples apart at 480 kHz is 48 kHz."""
    store = _store([_tone()], rate=0.0, capture_rate_hz=480_000.0)
    assert store.rate(1, 2) == pytest.approx(RATE)
    assert store.native_rate(1, 2) == pytest.approx(RATE)
    store.set_processing(1, 2, StreamProcessing(highpass_hz=20.0))
    assert abs(float(np.mean(store.samples(1, 2, 0)))) < 1.0


def test_a_measured_rate_is_not_fooled_by_samples_grouped_onto_rows():
    """Two samples per row, 4 capture samples apart, rows 16 apart: 8 per sample on average,
    while the most common gap is 4. 480 kHz / 8 = 60 kHz, not 120 kHz."""
    n = 20_000
    pos = (np.arange(n) // 2) * 16 + (np.arange(n) % 2) * 4
    store = _store([_tone(n=n)], rate=0.0, capture_rate_hz=480_000.0, positions=pos)
    assert store.rate(1, 2) == pytest.approx(60_000.0, rel=1e-3)


def test_a_reported_rate_is_kept():
    store = _store([_tone()], rate=44_100.0, capture_rate_hz=480_000.0)
    assert store.rate(1, 2) == 44_100.0



# ---------------------------------------------------------------- the DC blocker
def test_the_dc_blocker_is_a_1_hz_zero_phase_filter_ahead_of_filter_and_gain():
    """It was Decode ▸ Block PDM DC Bias, which subtracted the mean of every PDM port in the
    window; now it is one stream's, for PCM as well, and a filter, so a bias that drifts
    goes too. Zero phase, so the tone is neither shifted nor, a thousand times above the
    corner, attenuated."""
    t = np.arange(48_001)
    tone = 1000.0 * np.sin(2 * np.pi * 1000.0 * t / RATE)
    drift = 4000.0 + 1500.0 * t / t.size                   # a bias that moves
    x = np.rint(drift + tone).astype(np.int64)
    store = _store([x])
    proc = StreamProcessing(dc_block=True)
    assert not proc.is_identity() and proc.describe() == "DC blocked"
    store.set_processing(1, 2, proc)
    got = np.asarray(store.samples(1, 2, 0), dtype=np.float64)
    mid = slice(4_800, -4_800)                             # away from the ends
    assert abs(float(np.mean(got[mid]))) < 5.0
    assert np.max(np.abs(got[mid] - tone[mid])) < 6.0      # the tone, unshifted
    assert got == pytest.approx(np.clip(np.rint(filters.dc_block(x, RATE)), -32768, 32767))
    both = StreamProcessing(dc_block=True, highpass_hz=20.0, gain_db=3.0)
    assert both.describe() == "DC blocked, HPF 20 Hz, +3.0 dB"


def test_the_dc_blocker_corner_is_1_hz():
    """-6 dB at the corner: a 2nd-order Butterworth there is -3 dB, run forward and back."""
    for hz, want_db in ((1.0, -6.02), (10.0, -0.0),):
        t = np.arange(int(RATE * 20)) / RATE
        x = np.sin(2 * np.pi * hz * t)
        y = filters.dc_block(x, RATE)
        mid = slice(int(RATE * 5), int(RATE * 15))
        got = 20 * np.log10(np.max(np.abs(y[mid])) / np.max(np.abs(x[mid])))
        assert got == pytest.approx(want_db, abs=0.05), hz


def test_the_peak_is_measured_after_the_dc_blocker():
    x = _tone(dc=4000.0, amp=1000.0)
    store = _store([x])
    assert store.peak_db(1, 2) == pytest.approx(20 * np.log10(5000 / 32768), abs=0.01)
    assert store.peak_db(1, 2, dc_block=True) == pytest.approx(
        20 * np.log10(1000 / 32768), abs=0.1)


def test_a_stream_with_no_rate_is_not_dc_blocked():
    x = _tone(dc=4000.0, amp=1000.0)
    store = _store([x], rate=0.0)
    store.set_processing(1, 2, StreamProcessing(dc_block=True))
    assert np.array_equal(store.samples(1, 2, 0), x)


def test_the_dc_blocker_round_trips_and_an_old_setting_reads_it_off():
    proc = StreamProcessing(highpass_hz=20.0, dc_block=True)
    assert StreamProcessing.from_json(proc.to_json()) == proc
    old = {"highpass_hz": 20.0, "gain_db": 0.0, "allow_clipping": False}   # before 3.0.20
    assert StreamProcessing.from_json(old) == StreamProcessing(highpass_hz=20.0)
