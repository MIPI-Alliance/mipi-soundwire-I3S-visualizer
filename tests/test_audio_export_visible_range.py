"""Exporting the Audio pane's visible range writes the same stretch of the capture for
every stream.

The visible window is a capture-sample span. Each (device, dataport) maps it to its own
sample indices through its own sample positions, since streams differ in rate and start
position: the same index range would cover a different stretch of each stream.
"""
import os
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.store.audio_store import AudioStore

# (dp, first capture sample, capture samples per audio sample, count, rate Hz)
_STREAMS = [(0, 1_000, 100, 400, 44_100.0), (1, 5_000, 92, 400, 48_000.0)]


def _two_rate_store():
    """Device 0 with two mono 16-bit streams at different rates and start positions.
    Each sample's value is its own index, so a WAV frame names the sample it came from."""
    audio = []
    for dp, start, step, n, _rate in _STREAMS:
        for i in range(n):
            audio.append({"device": 0, "dp": dp, "channel": 0, "sample_size": 16,
                          "value": i, "index": i, "start_sample": start + i * step})
    return AudioStore.from_audio(audio, {(0, dp): rate for dp, _s, _st, _n, rate in _STREAMS})


def _frames(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int64)


def _dialog(store, **kw):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    dlg = AudioExportDialog(store, **kw)
    dlg._range.setCurrentIndex(1)                       # "Visible range (audio view)"
    return dlg


def test_store_maps_a_capture_window_to_each_streams_own_indices():
    store = _two_rate_store()
    s0, s1 = 20_000, 30_000
    for dp, start, step, n, _rate in _STREAMS:
        pos = start + np.arange(n) * step
        want = (int(np.searchsorted(pos, s0, "left")), int(np.searchsorted(pos, s1, "right")))
        assert store.stream_index_range(0, dp, s0, s1) == want
    assert store.stream_index_range(7, 7, s0, s1) == (0, 0)      # no such stream


def test_visible_range_export_covers_the_same_capture_span_for_every_stream(tmp_path):
    store = _two_rate_store()
    s0, s1 = 20_000, 30_000
    dlg = _dialog(store, visible_sample_range=(s0, s1))
    assert dlg.sample_range() == (s0, s1)
    ranges = {dp: dlg.index_range(0, dp) for dp, *_ in _STREAMS}
    assert ranges[0] != ranges[1]                       # the streams' indices differ
    for dp, start, step, _n, _rate in _STREAMS:
        path = store.export_wav(0, dp, str(tmp_path / f"dp{dp}.wav"), index_range=ranges[dp])
        idx = _frames(path)                             # each frame's value is its index
        pos = start + idx * step
        assert idx.size > 0
        assert np.array_equal(idx, np.arange(idx[0], idx[-1] + 1))
        assert pos[0] >= s0 and pos[0] - step < s0, dp  # starts at the window's start
        assert pos[-1] <= s1 and pos[-1] + step > s1, dp  # and ends at its end


def test_a_capture_window_needs_a_stream_to_become_indices():
    dlg = _dialog(_two_rate_store(), visible_sample_range=(20_000, 30_000))
    with pytest.raises(ValueError):
        dlg.index_range()
    dlg._range.setCurrentIndex(0)                       # whole capture: no stream needed
    assert dlg.index_range() is None and dlg.index_range(0, 1) is None
    assert dlg.sample_range() is None


def test_an_index_window_is_still_used_as_given():
    dlg = _dialog(_two_rate_store(), visible_index_range=(10, 20))
    assert dlg.index_range() == (10, 20) and dlg.index_range(0, 1) == (10, 20)
    assert dlg.sample_range() is None


def test_the_audio_view_reports_its_window_in_capture_samples():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    win.load_demo()
    QApplication.processEvents()
    av = win._audio_view
    t0, t1 = av.x_range_seconds()
    av.set_x_range_seconds(t0 + (t1 - t0) * 0.40, t0 + (t1 - t0) * 0.45)
    QApplication.processEvents()
    x0, x1 = av._plots[0][0].viewRange()[0]
    s0, s1 = av.visible_sample_range()
    assert (s0, s1) == (int(max(0, x0)), int(max(0, x1)))
    _plot, _c, dev, dp, ch, _n = av._plots[0]
    assert win._audio_store.stream_index_range(dev, dp, s0, s1) == av.visible_index_range()
