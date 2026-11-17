"""Play-head sweep tests for multi-data-port playback where the streams differ in
length (no audio device needed).

  1. `_mix_selected` zero-fills to the LONGEST selected stream (like pcm_play /
     export_wav), so a data port that ends early does not truncate the mix.
  2. `_sweep_reference` anchors the cursor sweep on the LONGEST stream, so the play
     head reaches the end of the capture.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import swi3score
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import transitions
from swi3s_studio.store.audio_store import AudioStore
from swi3s_studio.ui.audio_view import AudioView

pytestmark = pytest.mark.gui

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


class _Click:
    def __init__(self, pos):
        self._p = pos

    def button(self):
        from PySide6.QtCore import Qt
        return Qt.LeftButton

    def scenePos(self):
        return self._p

    def accept(self):
        pass


def test_a_click_on_the_amplitude_axis_seeks_nowhere_and_one_in_the_data_area_stays_in_the_capture():
    """Only the data area seeks, and never to a sample before 0 or past the capture."""
    from PySide6.QtCore import QPointF
    store = _store()
    av = AudioView()
    av.resize(900, 500)
    av.set_capture_context(48000.0, 1000)
    av.set_store(store)
    av.show()
    _app.processEvents()
    plot = av._plots[0][0]
    got = []
    av.seeked.connect(got.append)
    box = plot.vb.sceneBoundingRect()
    mid = box.center().y()
    av._on_plot_click(_Click(QPointF(box.left() - 8, mid)), plot, 0, 0, 0)     # the axis
    assert got == []
    plot.vb.setXRange(-500, 5000, padding=0)
    _app.processEvents()
    av._on_plot_click(_Click(QPointF(box.left() + 2, mid)), plot, 0, 0, 0)
    av._on_plot_click(_Click(QPointF(box.right() - 2, mid)), plot, 0, 0, 0)
    assert len(got) == 2 and all(0 <= s <= 1000 for s in got)


def test_a_click_past_the_capture_in_a_widened_view_seeks_to_its_first_and_last_sample():
    """All Links lets a lane's view run past its own capture (`set_x_bounds`); a click there
    seeks to sample 0 or the capture's last sample, not outside it."""
    from PySide6.QtCore import QPointF
    store = _store()
    av = AudioView()
    av.resize(900, 500)
    av.set_capture_context(48000.0, 1000)
    av.set_store(store)
    av.set_x_bounds((-0.01, 0.1))                 # about -480 .. 4800 samples at 48 kHz
    av.show()
    _app.processEvents()
    plot = av._plots[0][0]
    plot.vb.setXRange(-400, 4000, padding=0)
    _app.processEvents()
    got = []
    av.seeked.connect(got.append)
    box = plot.vb.sceneBoundingRect()
    left, right = QPointF(box.left() + 2, box.center().y()), QPointF(box.right() - 2, box.center().y())
    assert plot.vb.mapSceneToView(left).x() < 0
    assert plot.vb.mapSceneToView(right).x() > 1000
    av._on_plot_click(_Click(left), plot, 0, 0, 0)
    av._on_plot_click(_Click(right), plot, 0, 0, 0)
    assert got == [0, 1000]
