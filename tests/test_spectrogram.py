"""The Audio pane's spectrogram lane: the spectrum it draws (dsp/spectrogram.py) and the
per-channel choice between it and the waveform. That choice is also part of a Link's Audio
view, so tests/test_workspace_view.py carries it through a Link switch, a re-decode and a
saved workspace."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from swi3s_studio.dsp.spectrogram import FLOOR_DB, FRAME_SIZES, spectrogram

_RATE = 48_000


def _tone(hz, amplitude, seconds=2.0, full=32768):
    t = np.arange(int(_RATE * seconds)) / _RATE
    return np.round(np.sin(2 * np.pi * hz * t) * amplitude * (full - 1)).astype(np.int64)


_BIN = _RATE / 1024


@pytest.mark.parametrize("hz,amp", [(22 * _BIN, 1.0), (64 * _BIN, 0.5), (320 * _BIN, 0.1)])
def test_a_tone_peaks_at_its_frequency_and_level(hz, amp):
    """A full-scale sine on a bin centre reads 0 dB, and a lower one its level below."""
    s = spectrogram(_tone(hz, amp), 0, _RATE * 2, 200, 32768)
    assert np.all(s.db.argmax(axis=1) * _BIN == hz)
    assert s.db.max(axis=1) == pytest.approx(np.full(200, 20 * np.log10(amp)), abs=0.1)


def test_a_tone_between_bins_reads_within_the_windows_scalloping():
    """Off a bin centre a Hann window loses up to 1.42 dB, which is the price of its low
    leakage; 1 kHz sits a third of a bin off."""
    s = spectrogram(_tone(1000.0, 1.0), 0, _RATE * 2, 50, 32768)
    assert np.all(np.abs(s.db.argmax(axis=1) * _BIN - 1000.0) <= _BIN)
    assert np.all((s.db.max(axis=1) > -1.42) & (s.db.max(axis=1) <= 0.0))


def test_one_column_per_frame_centred_in_the_stretch_asked_for():
    x = _tone(1000.0, 0.5)
    s = spectrogram(x, 20_000, 30_000, 100, 32768)
    assert s.db.shape == (100, s.nfft // 2 + 1)
    assert s.centres[0] == 20_000 and s.centres[-1] == 29_999


def test_a_frame_at_an_end_is_moved_inward_not_padded():
    """A frame centred on the first sample would read half silence; it is all signal."""
    x = _tone(22 * _BIN, 1.0)
    s = spectrogram(x, 0, 10, 2, 32768)
    assert s.db.max(axis=1) == pytest.approx([0.0, 0.0], abs=0.1)


def test_short_and_empty_streams():
    s = spectrogram(_tone(1000.0, 1.0, seconds=200 / _RATE), 0, 200, 50, 32768)
    assert s.nfft == 128 and s.db.shape[1] == 65          # the largest power of two in 200
    assert spectrogram(np.zeros(5, dtype=np.int64), 0, 5, 10, 1).db.size == 0
    assert spectrogram(_tone(1000.0, 1.0), 10, 10, 10, 1).db.size == 0


@pytest.mark.parametrize("nfft", FRAME_SIZES)
def test_a_glitch_shows_from_half_a_frame_before_it_and_no_earlier(nfft):
    """The frames are centred on their columns, so a click shows in every column whose
    frame holds it: from nfft/2 samples before the click to nfft/2 after, and nowhere
    else. That is why a click appears before it happens, and why the frame size is the
    user's choice."""
    x = np.zeros(48_000, dtype=np.int64)
    x[24_000] = 30_000                                      # one sample, in silence
    s = spectrogram(x, 0, x.size, x.size, 32768, nfft=nfft)  # a column per sample
    lit = s.centres[(s.db > FLOOR_DB + 1).any(axis=1)]
    assert lit.min() - 24_000 >= -nfft // 2 and lit.max() - 24_000 <= nfft // 2
    assert lit.min() - 24_000 <= -nfft // 2 + 2, "the frame reaches less far than it says"


def test_silence_reads_the_floor_not_minus_infinity():
    s = spectrogram(np.zeros(4096, dtype=np.int64), 0, 4096, 4, 32768)
    assert np.all(s.db == FLOOR_DB)


# ---------------------------------------------------------------------- the pane
@pytest.fixture
def win():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    QApplication.processEvents()
    return w


def _lanes(av):
    import pyqtgraph as pg
    return {(d, p, c): isinstance(curve, pg.ImageItem) for _pl, curve, d, p, c, _n in av._plots}


def test_each_channel_chooses_waveform_or_spectrogram(win):
    av = win._audio_view
    keys = sorted(av._checks)
    av.set_spectrogram(keys[0], True)
    assert _lanes(av) == {k: k == keys[0] for k in keys}
    plot, img = av._plots[0][0], av._plots[0][1]
    assert img.image is not None and img.image.shape[0] > 1 and img.image.shape[1] > 1
    assert "spectrogram" in plot.titleLabel.text
    av.set_spectrogram(keys[0], False)
    assert not any(_lanes(av).values())


def test_the_spectrogram_follows_the_shared_zoom(win):
    av = win._audio_view
    key = sorted(av._checks)[0]
    av.set_spectrogram(key, True)
    img = av._plots[0][1]
    full = img.mapRectToParent(img.boundingRect())          # where this channel has audio
    rate = av._capture_rate
    a, b = full.left() + full.width() * 0.4, full.left() + full.width() * 0.6
    av.set_x_range_seconds(a / rate, b / rate)
    shown = img.mapRectToParent(img.boundingRect())
    assert shown.left() == pytest.approx(a, abs=full.width() * 0.02)
    assert shown.right() == pytest.approx(b, abs=full.width() * 0.02)


def test_the_channels_menu_offers_the_choice(win, monkeypatch):
    """Right-click a channel ▸ Spectrogram / Waveform. QMenu.exec is patched to pick an
    item by name (conftest routes an instance's exec through the class attribute)."""
    from PySide6.QtWidgets import QMenu
    av = win._audio_view
    key = sorted(av._checks)[2]
    want = ["Spectrogram"]
    monkeypatch.setattr(QMenu, "exec", lambda self, *_a: next(
        a for a in self.actions() if a.text() == want[0]))
    av._stream_menu(av._checks[key], av._checks[key].rect().center(), *key)
    assert av.spectrogram_channels() == {key}
    want[0] = "Waveform"
    av._stream_menu(av._checks[key], av._checks[key].rect().center(), *key)
    assert av.spectrogram_channels() == set()


def test_each_channel_has_its_own_frame_size(monkeypatch):
    """Right-click ▸ a frame size shows that channel as a spectrogram at it; the item
    names the half-frame smear and the bin width at the channel's rate. A longer demo than
    the suite's: a stream shorter than a frame gets a frame of its own length."""
    from PySide6.QtWidgets import QApplication, QMenu
    monkeypatch.setenv("SWI3S_DEMO_SAMPLES", "3000")
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    av = win._audio_view
    keys = sorted(av._checks)
    labels = []

    def pick(self, *_a):
        labels.extend(a.text() for a in self.actions())
        return next(a for a in self.actions() if "256-point" in a.text())

    monkeypatch.setattr(QMenu, "exec", pick)
    av._stream_menu(av._checks[keys[1]], av._checks[keys[1]].rect().center(), *keys[1])
    rate = av._store.rate(*keys[1][:2])
    assert any(f"±{128 / rate * 1000:.1f} ms · {rate / 256:.0f} Hz bins" in t for t in labels)
    assert av.spectrogram_channels() == {keys[1]} and av.spectrogram_frame(keys[1]) == 256
    av.set_spectrogram(keys[0], True)
    shapes = {(d, p, c): curve.image.shape[1] for _pl, curve, d, p, c, _n in av._plots
              if (d, p, c) in av.spectrogram_channels()}
    assert shapes[keys[1]] == 129 and shapes[keys[0]] > 129   # 256 vs the default
    titles = [pl.titleLabel.text for pl, _c, d, p, c, _n in av._plots if (d, p, c) == keys[1]]
    assert "256-point" in titles[0]
    with pytest.raises(ValueError):
        av.set_spectrogram_frame(keys[1], 300)


def test_vertical_zoom_leaves_a_spectrogram_alone(win):
    av = win._audio_view
    key = sorted(av._checks)[0]
    av.set_spectrogram(key, True)
    plot = av._plots[0][0]
    before = plot.getViewBox().viewRange()[1]
    av._vzoom_btn.setChecked(True)
    assert plot.getViewBox().viewRange()[1] == pytest.approx(before)
    assert before[0] == pytest.approx(0.0) and before[1] == pytest.approx(
        av._store.rate(*key[:2]) / 2, rel=1e-6)


def test_each_bin_is_drawn_centred_on_its_frequency(monkeypatch):
    """Bin k of an n-point frame is k * rate / n; the image is placed so its pixel row is
    centred there, not spread evenly from 0 to Nyquist (which put 3000 Hz at ~3017 Hz)."""
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("SWI3S_DEMO_SAMPLES", "3000")
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    av = win._audio_view
    key = sorted(av._checks)[0]
    av.set_spectrogram(key, True)
    img = av._plots[0][1]
    rate = av._store.rate(*key[:2])
    bins = img.image.shape[1]
    rect = img.mapRectToParent(img.boundingRect())
    row_hz = rect.height() / bins
    assert row_hz == pytest.approx((rate / 2) / (bins - 1), rel=1e-6)
    assert rect.top() == pytest.approx(-row_hz / 2, rel=1e-6)        # bin 0 centred on 0 Hz
    assert rect.bottom() == pytest.approx(rate / 2 + row_hz / 2, rel=1e-6)
