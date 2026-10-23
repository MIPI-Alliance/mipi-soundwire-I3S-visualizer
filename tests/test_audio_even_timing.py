"""Audio ▸ right-click a channel ▸ Evenly Spaced: a skipping stream drawn at its average rate.

Payload Interval Skipping carries 44.1 kHz over 48 kHz rows as 147 samples in every 160
intervals, so the samples arrive in steps and skips. Drawn at their transport times a sine is
lumpy; drawn evenly spaced it is the waveform a receiver plays. The demo's DP0 is such a stream.
The choice is the Link's: it survives a re-decode and is saved in the workspace.
"""
import numpy as np
import pytest
from conftest import pump_loads
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMenu

from swi3s_studio.session import Session
from swi3s_studio.store.audio_store import AudioStore, _even_runs

_SKIPPING = (0, 0)          # the demo's 44.1 kHz port


@pytest.fixture(scope="module")
def demo():
    return Session.from_demo(48000)


def _steps(store, dev, dp):
    ch = store.channels(dev, dp)[0]
    return np.diff(np.asarray(store.sample_positions(dev, dp, ch), dtype=np.int64))


def test_only_an_unevenly_transported_stream_is_offered_it(demo):
    st = AudioStore.from_session(demo)
    assert st.timing_is_irregular(*_SKIPPING)
    assert not any(st.timing_is_irregular(d, p) for d, p in st.streams() if (d, p) != _SKIPPING)
    assert not st.set_evenly_spaced(0, 1, True), "an even stream has nothing to respace"


def test_evenly_spaced_is_the_average_rate_and_as_transported_comes_back(demo):
    st = AudioStore.from_session(demo)
    before = _steps(st, *_SKIPPING)
    assert before.max() > 1.5 * before.min()                  # a skip doubles the step
    assert st.set_evenly_spaced(*_SKIPPING, True) and st.evenly_spaced(*_SKIPPING)
    after = _steps(st, *_SKIPPING)
    assert after.max() - after.min() <= 1                    # whole samples, no more
    rate = demo.sample_rate_hz / after.mean()
    assert abs(rate - 44100) < 5, rate
    assert st.set_evenly_spaced(*_SKIPPING, False)
    np.testing.assert_array_equal(_steps(st, *_SKIPPING), before)


def test_a_real_transport_gap_still_breaks_the_run():
    """Respacing is per run between gaps: a dropout is not smoothed into the audio."""
    sat = np.array([0, 10, 21, 30, 40, 1000, 1010, 1021, 1030], dtype=np.int64)
    out = _even_runs(sat, np.array([4]))
    assert list(out[:5]) == [0, 10, 20, 30, 40] and list(out[5:]) == [1000, 1010, 1020, 1030]


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.load_demo()
    return w


def test_the_channel_menu_offers_it_only_where_it_changes_something(win, monkeypatch):
    seen = {}
    def grab(menu, *_a):
        seen.update({a.text(): a.isEnabled() for a in menu.actions()})
        return None
    monkeypatch.setattr(QMenu, "exec", grab)
    cb = next(iter(win._audio_view._checks.values()))
    win._audio_view._stream_menu(cb, QPoint(0, 0), *_SKIPPING)
    assert seen.get("Evenly Spaced") and seen.get("As Transported")
    seen.clear()
    win._audio_view._stream_menu(cb, QPoint(0, 0), 0, 1)
    assert seen.get("Evenly Spaced") is False


def test_it_is_the_links_choice_and_survives_a_re_decode(win):
    win.set_stream_even_timing(*_SKIPPING, True)
    assert win._links[0].view.even_timing == {_SKIPPING}
    old = win._audio_store
    win.load_session(win._session)                      # what an SSP step or an override does
    assert win._audio_store is not old and win._audio_store.evenly_spaced(*_SKIPPING)
    win.set_stream_even_timing(*_SKIPPING, False)
    assert win._links[0].view.even_timing == set()
    assert not win._audio_store.evenly_spaced(*_SKIPPING)


def test_a_saved_workspace_reopens_with_it(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    win.set_stream_even_timing(*_SKIPPING, True)
    path = str(tmp_path / "ws.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    again = MainWindow()
    again.open_workspace()
    pump_loads(again)
    assert again.links[0].view.even_timing == {_SKIPPING}
    assert again._audio_store.evenly_spaced(*_SKIPPING)


def _spurs(spec):
    """Mean level of each column's strongest bin outside ±4 bins of its peak, dB."""
    out = []
    for row in spec.db:
        k = int(row.argmax())
        out.append(np.delete(row, range(max(0, k - 4), k + 5)).max())
    return float(np.mean(out))


def test_held_spectrogram_shows_the_skip_pattern_and_the_even_spectrum_does_not(demo):
    from swi3s_studio.dsp.spectrogram import held_spectrogram, spectrogram
    st = AudioStore.from_session(demo)
    ch = st.channels(*_SKIPPING)[0]
    x, sat = st.samples(*_SKIPPING, ch), st.sample_positions(*_SKIPPING, ch)
    full = float(1 << (st.sample_bits(*_SKIPPING, ch) - 1))
    step = st.transport_step(*_SKIPPING, ch)
    assert abs(demo.sample_rate_hz / step - 44100) < 5
    held = held_spectrogram(x, sat, int(sat[0]), int(sat[-1]), step, 32, full)
    even = spectrogram(x, 0, x.size, 32, full)
    assert np.allclose(held.db.max(axis=1), even.db.max(axis=1), atol=1.0)  # same tone
    assert _spurs(held) > _spurs(even) + 30
    assert held.centres[0] >= sat[0] and held.centres[-1] <= sat[-1]


def test_a_spectrogram_lane_follows_the_streams_timing(win):
    av = win._audio_view
    key = next(k for k in sorted(av._checks) if k[:2] == _SKIPPING)
    av.set_spectrogram(key, True)
    def image():
        img = next(c for _p, c, d, p, ch, _n in av._plots if (d, p, ch) == key)
        return np.array(img.image)
    transported = image()
    win.set_stream_even_timing(*_SKIPPING, True)
    even = image()
    assert np.median(transported) > np.median(even) + 20
    win.set_stream_even_timing(*_SKIPPING, False)
    np.testing.assert_allclose(image(), transported)
