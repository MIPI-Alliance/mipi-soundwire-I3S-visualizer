"""Audio render-pyramid / envelope test (device-keyed).

Run: python3 -m pytest tests/test_audio_pyramid.py
"""
import os
import tempfile

import numpy as np

from swi3s_studio.store.audio_store import AudioStore

DEV, DP, CH = 0, 0, 0


def _ramp_store(n=200_000, mmap_dir=None):
    # One stream (dev0/dp0/ch0): a triangle so min/max envelopes are exact.
    idx = np.arange(n)
    tri = (np.abs(((idx % 4000) - 2000)) - 1000).astype(np.int64)  # in [-1000, 1000)
    audio = [{"device": DEV, "dp": DP, "channel": CH, "index": int(i),
              "value": int(v) & 0xFFFF, "sample_size": 16,
              "start_sample": int(i), "end_sample": int(i) + 1}
             for i, v in zip(idx, tri)]
    return AudioStore.from_audio(audio, {(DEV, DP): 48000.0}, mmap_dir=mmap_dir), tri


def test_pyramid_built_and_decimates():
    store, tri = _ramp_store(200_000)
    assert store.pyramid_levels(DEV, DP, CH) >= 3, "expected multiple pyramid levels"
    x, lo, hi = store.envelope(DEV, DP, CH, 0, 200_000, max_points=1000)
    assert 0 < x.size <= 4000, x.size
    assert (hi >= lo).all()
    assert lo.min() == tri.min()
    assert hi.max() == tri.max()


def test_envelope_exact_when_zoomed_in():
    store, tri = _ramp_store(200_000)
    x, lo, hi = store.envelope(DEV, DP, CH, 1000, 1100, max_points=2048)
    assert (lo == hi).all()
    assert np.array_equal(lo, tri[1000:1100])


def test_memmap_backing():
    with tempfile.TemporaryDirectory() as d:
        store, tri = _ramp_store(50_000, mmap_dir=d)
        try:
            assert os.path.exists(os.path.join(d, "dev0_dp0_ch0.i32"))
            assert isinstance(store.samples(DEV, DP, CH), np.memmap)
            x, lo, hi = store.envelope(DEV, DP, CH, 0, 50_000, max_points=500)
            assert lo.min() == tri.min() and hi.max() == tri.max()
        finally:
            store.close()   # release the memmap so Windows can delete the temp dir


def test_reconfig_index_reset_keeps_all_epochs():
    """The core numbers audio samples with a per-(dp,channel) `index` that RESETS on
    every bus reconfiguration, so the same index recurs once per config epoch. The
    store must key each channel on start_sample (globally monotonic), NOT on index —
    otherwise the old dense[index]=value (last-write-wins) kept only the FINAL epoch
    and dropped every earlier one, so the audio view showed nothing where a data port
    was first enabled (the Dev6 DP2 bug on the pdm768 capture). Two epochs reuse the
    same index range at disjoint capture times; all samples must survive, time-ordered,
    with monotonic capture anchors."""
    K = 50
    audio = []
    for i in range(K):                        # epoch 1: index 0..K-1, early samples
        audio.append({"device": 6, "dp": 2, "channel": 0, "index": i,
                      "value": i, "sample_size": 16,
                      "start_sample": 100 + i, "end_sample": 101 + i})
    for i in range(K):                        # epoch 2: index RESETS to 0..K-1, later
        audio.append({"device": 6, "dp": 2, "channel": 0, "index": i,
                      "value": 1000 + i, "sample_size": 16,
                      "start_sample": 10_000 + i, "end_sample": 10_001 + i})
    store = AudioStore.from_audio(audio, {(6, 2): 0.0})
    s = store.samples(6, 2, 0)
    assert s.size == 2 * K, f"both epochs must survive: expected {2*K}, got {s.size}"
    sat = store.sample_positions(6, 2, 0)
    assert sat.size == 2 * K and bool(np.all(np.diff(sat) >= 0)), "anchors non-decreasing"
    assert int(sat.min()) == 100 and int(sat.max()) == 10_000 + K - 1   # spans BOTH epochs
    assert s[0] == 0 and s[K - 1] == K - 1                    # epoch 1 (early), in order
    assert s[K] == 1000 and s[2 * K - 1] == 1000 + K - 1      # epoch 2 (late), in order
