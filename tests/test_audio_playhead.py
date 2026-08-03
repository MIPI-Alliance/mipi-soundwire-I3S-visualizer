"""Play-head sweep regression tests (no audio device needed).

Covers two fixes for multi-data-port playback where the streams differ in length:
  1. `_mix_selected` zero-fills to the LONGEST selected stream (like pcm_play /
     export_wav), so a data port that ends early doesn't truncate the whole mix.
  2. `_sweep_reference` anchors the cursor sweep on the LONGEST stream, so the play
     head reaches the true end instead of sticking at a shorter DP's last sample
     (which made replay re-snap to a fixed mid-capture location).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import swi3score
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import transitions
from swi3s_studio.store.audio_store import AudioStore
from swi3s_studio.ui.audio_view import AudioView

_app = QApplication.instance() or QApplication([])


def _store(n=300):
    s = swi3score.DecoderSettings()
    d = swi3score.Decoder(transitions.demo_capture(n).sample_source(), s)
    d.run()
    return AudioStore.from_audio(d.audio(), d.audio_sample_rates())


def _truncate(store, key, keep):
    store._channels[key] = store._channels[key][:keep].copy()
    store._sample_at[key] = store._sample_at[key][:keep].copy()
    store._pyramids.pop(key, None)


def test_mix_pads_to_longest_not_shortest():
    store = _store()
    full = store.samples(0, 3, 0).size
    _truncate(store, (0, 0, 0), full // 4)          # first DP now 1/4 length
    av = AudioView()
    av._store = store
    av._cursor_sample = 0
    sel = [(0, 0, 0), (0, 1, 0), (0, 2, 0), (0, 3, 0)]
    dps = [(0, 0), (0, 1), (0, 2), (0, 3)]
    pcm, rate, nch, qfmt = av._mix_selected(sel, dps)
    frames = (len(pcm) // 2) // max(1, nch)
    assert nch == 1 and rate == 48000
    assert frames == full, f"mix truncated to {frames}, expected longest {full}"


def test_sweep_reference_is_longest_stream():
    store = _store()
    full = store.samples(0, 3, 0).size
    _truncate(store, (0, 0, 0), full // 4)          # first selected DP is the SHORTEST
    av = AudioView()
    av._store = store
    av._cursor_sample = 0
    sel = [(0, 0, 0), (0, 1, 0), (0, 2, 0), (0, 3, 0)]
    dps = [(0, 0), (0, 1), (0, 2), (0, 3)]
    ref = av._sweep_reference(dps, sel, 48000)
    assert ref is not None
    dev, dp, ch, i0, native = ref
    assert (dev, dp) != (0, 0), "sweep still anchored on the short first DP"
    # The chosen reference must reach the end of the capture, not the short DP's 1/4 mark.
    remaining = store.samples(dev, dp, ch)[i0:].size
    assert remaining == full


def test_sweep_reference_single_dp_is_that_dp():
    store = _store()
    av = AudioView()
    av._store = store
    av._cursor_sample = 0
    ref = av._sweep_reference([(0, 2)], [(0, 2, 0)], 48000)
    assert ref is not None and ref[0] == 0 and ref[1] == 2
