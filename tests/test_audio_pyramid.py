"""Audio render-pyramid / envelope test (device-keyed).

Run: python3 tests/test_audio_pyramid.py
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


if __name__ == "__main__":
    test_pyramid_built_and_decimates(); print("ok: pyramid built + decimates, extremes preserved")
    test_envelope_exact_when_zoomed_in(); print("ok: zoomed-in envelope == raw samples")
    test_memmap_backing(); print("ok: memmap-backed channel + envelope")
    print("ALL AUDIO-PYRAMID TESTS PASSED")
