"""Audio view: a mixed playback places each data port at its own start, and the channel
menu's explanation for a disabled timing choice is shown.

Pinned here:
- a mix of data ports starts a port whose first sample after the cursor comes later, later
  by that gap, zero-filling before it, and the mix runs to the end of the latest-ending port;
- the cursor sweep waits for the swept stream's first sample;
- a lead is converted from capture samples to frames at the play rate;
- a Play names the streams of unknown rate it plays at the assumed rate;
- the clip's end and Stop return the playhead to where Play started (Stop there goes to
  the audio's start), and a Play resuming a Pause keeps that origin;
- the channel menu disables Evenly Spaced with its explanation for a stream that already
  arrives evenly, and offers it with no tooltip for one that does not.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_audio_view_mixed_playback.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import swi3score
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMenu

from swi3s_studio.ingest import transitions
from swi3s_studio.store.audio_store import AudioStore
from swi3s_studio.ui.audio_view import AudioView

pytestmark = pytest.mark.gui

_app = QApplication.instance() or QApplication([])

_PLAY_RATE = 48000
_CAPTURE_RATE = 96000.0      # twice the play rate: a lead of n capture samples is n/2 frames
_GAP = 4800                  # capture samples
_GAP_FRAMES = 2400           # _GAP at the play rate


def _store(n=300):
    s = swi3score.DecoderSettings()
    d = swi3score.Decoder(transitions.demo_capture(n).sample_source(), s)
    d.run()
    return AudioStore.from_audio(d.audio(), d.audio_sample_rates())


_made: list = []


@pytest.fixture(autouse=True)
def _delete_the_views():
    """Delete, after each test, the parentless views it built."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    yield
    for v in _made:
        if isValid(v):
            v.deleteLater()
    _made.clear()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _view(store, cursor=0):
    av = AudioView()
    _made.append(av)
    av._store = store
    av._capture_rate = _CAPTURE_RATE
    av._cursor_sample = cursor
    return av


def _mono(av, dp):
    pcm, *_ = av._mix_selected([(0, dp, 0)], [(0, dp)])
    return np.frombuffer(pcm, dtype="<i2").astype(np.int32)


def test_a_port_that_starts_after_the_cursor_is_mixed_in_at_its_start():
    base = _store()
    early_alone = _mono(_view(base), 2)
    late_alone = _mono(_view(base), 3)
    shifted = _store()
    shifted._sample_at[(0, 3, 0)] = shifted._sample_at[(0, 3, 0)] + _GAP
    av = _view(shifted)
    pcm, rate, nch, _q = av._mix_selected([(0, 2, 0), (0, 3, 0)], [(0, 2), (0, 3)])
    mix = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    expect = np.zeros(max(early_alone.size, _GAP_FRAMES + late_alone.size))
    expect[:early_alone.size] += early_alone
    expect[_GAP_FRAMES:_GAP_FRAMES + late_alone.size] += late_alone
    expect = np.rint(expect / 2.0)
    assert (rate, nch) == (_PLAY_RATE, 1) and mix.size == expect.size
    assert np.abs(mix - expect).max() <= 2
    at_zero = np.zeros(expect.size)
    at_zero[:early_alone.size] += early_alone
    at_zero[:late_alone.size] += late_alone
    assert np.abs(expect - np.rint(at_zero / 2.0)).max() > 100, "the shift is audible"


def test_a_port_already_running_at_the_cursor_is_not_delayed():
    store = _store()
    av = _view(store, cursor=int(store.sample_at_index(0, 3, 0, 100)))
    assert av._lead_frames(0, 3, 0, 100, 48000) == 0
    assert av._lead_frames(0, 3, 0, 0, 48000) == 0


def test_the_lead_is_the_gap_to_the_first_sample_at_the_play_rate():
    store = _store()
    store._sample_at[(0, 3, 0)] = store._sample_at[(0, 3, 0)] + _GAP
    av = _view(store)
    first = int(store.sample_at_index(0, 3, 0, 0))
    assert first > _GAP
    assert av._lead_frames(0, 3, 0, 0, 48000) == round(first * 48000 / _CAPTURE_RATE)
    assert av._lead_frames(0, 3, 0, 0, 24000) == round(first * 24000 / _CAPTURE_RATE)
    av._capture_rate = 0.0
    assert av._lead_frames(0, 3, 0, 0, 48000) == 0


class _Sink:
    """A sink that has output `frames` frames at 48 kHz and is still running."""

    def __init__(self, *_a):
        self.frames = 0

    def state(self):
        from PySide6.QtMultimedia import QAudio
        return QAudio.State.ActiveState

    def processedUSecs(self):
        return -(-self.frames * 1_000_000 // 48000)   # rounded up: reads back as `frames`

    def start(self, _source):
        pass

    def stop(self):
        pass


def _delayed_mix():
    """A view mixing data ports 2 and 3 from cursor 0, port 3 starting _GAP samples later."""
    store = _store()
    store._sample_at[(0, 3, 0)] = store._sample_at[(0, 3, 0)] + _GAP
    av = _view(store)
    av.set_channel_selection({(0, 2, 0), (0, 3, 0)})
    return av, store


def test_play_sweeps_against_the_later_port_from_its_own_first_sample(monkeypatch):
    """The sweep reference is the port reaching furthest, here the delayed one, and its
    lead is the gap to its first sample, at the play rate."""
    from swi3s_studio.ui import audio_view
    if not audio_view._HAVE_AUDIO:
        pytest.skip("no QtMultimedia")
    av, store = _delayed_mix()
    monkeypatch.setattr(audio_view, "QAudioSink", _Sink)
    monkeypatch.setattr(av, "_selected_device", lambda: type("D", (), {"isNull": lambda s: False})())
    av.play()
    try:
        assert av._sink is not None, "play() did not start"
        assert (av._play_dev, av._play_dp) == (0, 3)
        first = int(store.sample_at_index(0, 3, 0, 0))
        assert av._play_lead_frames == round(first * _PLAY_RATE / _CAPTURE_RATE) > 0
    finally:
        av.hard_stop()


def test_the_cursor_waits_for_the_swept_streams_first_sample(monkeypatch):
    from swi3s_studio.ui import audio_view
    if not audio_view._HAVE_AUDIO:
        pytest.skip("no QtMultimedia")
    av, store = _delayed_mix()
    monkeypatch.setattr(audio_view, "QAudioSink", _Sink)
    monkeypatch.setattr(av, "_selected_device", lambda: type("D", (), {"isNull": lambda s: False})())
    av.play()
    moved = []
    av.playCursorMoved.connect(moved.append)
    try:
        lead = av._play_lead_frames
        for frames in (0, lead // 2, lead - 1):
            av._sink.frames = frames
            av._on_play_tick()
        assert moved == [], "the cursor swept before the swept stream had a sample"
        av._sink.frames = lead + 10
        av._on_play_tick()
        step = int(10 * av._play_idx_per_frame)
        assert moved == [int(store.sample_at_index(0, 3, 0, av._play_start_idx + step))]
    finally:
        av.hard_stop()


def _evenly_spaced_entry(monkeypatch, av, irregular):
    """(tooltips shown, Evenly Spaced enabled, its tooltip) of data port 3's channel menu,
    the stream's timing irregular or not."""
    shown = {}

    def grab(menu, *_a):
        shown["on"] = menu.toolTipsVisible()
        act = next(a for a in menu.actions() if a.text() == "Evenly Spaced")
        shown["entry"] = (act.isEnabled(), act.toolTip())
        return None

    monkeypatch.setattr(QMenu, "exec", grab)
    monkeypatch.setattr(av._store, "timing_is_irregular", lambda *_a: irregular)
    av._stream_menu(av, QPoint(0, 0), 0, 3)
    return (shown["on"], *shown["entry"])


def test_the_channel_menu_shows_the_disabled_choices_tooltip(monkeypatch):
    av = _view(_store())
    assert _evenly_spaced_entry(monkeypatch, av, irregular=False) == (
        True, False, "This stream's samples already arrive evenly spaced")


def test_the_channel_menu_offers_evenly_spaced_for_irregular_timing_with_no_tooltip(
        monkeypatch):
    av = _view(_store())
    on, enabled, tip = _evenly_spaced_entry(monkeypatch, av, irregular=True)
    assert enabled and tip == "Evenly Spaced"   # Qt's tooltip for an action with none set


def _playing(monkeypatch, offset=2000):
    """(a view playing data port 2 into a sink that reads back its frames, the cursor
    sample it started from, `offset` capture samples into the audio)."""
    from swi3s_studio.ui import audio_view
    if not audio_view._HAVE_AUDIO:
        pytest.skip("no QtMultimedia")
    av = _view(_store())
    monkeypatch.setattr(av, "_selected", lambda: [(0, 2, 0)])
    av._cursor_sample = av._audio_start_sample() + offset
    monkeypatch.setattr(audio_view, "QAudioSink", _Sink)
    monkeypatch.setattr(av, "_selected_device", lambda: type("D", (), {"isNull": lambda s: False})())
    av.play()
    assert av._sink is not None, "play() did not start"
    return av, av._cursor_sample


def _sweep(av, frames):
    av._sink.frames = frames
    av._on_play_tick()


def test_the_end_of_the_clip_returns_the_playhead_to_where_play_started(monkeypatch):
    av, origin = _playing(monkeypatch)
    moved = []
    av.playCursorMoved.connect(moved.append)
    _sweep(av, av._play_nframes // 2)
    assert moved and moved[-1] > origin
    _sweep(av, av._play_nframes)
    assert av._sink is None and moved[-1] == origin == av._cursor_sample


def test_stop_returns_to_where_play_started_then_to_the_start_of_the_audio(monkeypatch):
    av, origin = _playing(monkeypatch)
    moved = []
    av.playCursorMoved.connect(moved.append)
    _sweep(av, av._play_nframes // 2)
    av.stop_and_rewind()
    _sweep(av, av._play_nframes + av._play_fade_frames)     # the stop fade plays out
    assert av._sink is None and moved[-1] == origin
    av.stop_and_rewind()                             # already there: the audio's start
    assert moved[-1] == av._audio_start_sample() < origin


def test_a_resumed_pause_keeps_the_origin_and_a_moved_playhead_starts_a_new_one(monkeypatch):
    av, origin = _playing(monkeypatch)
    _sweep(av, av._play_nframes // 4)
    paused = av._cursor_sample
    av._toggle_play()                                # pause
    _sweep(av, av._play_nframes + av._play_fade_frames)
    assert av._sink is None and av._cursor_sample == paused
    av.play()                                        # resume from where it paused
    assert av._play_origin == origin
    av._toggle_play()                                # pause the resumed playback
    _sweep(av, av._play_nframes + av._play_fade_frames)
    assert av._sink is None and av._paused_at is not None
    av.set_cursor(origin + 2000)                     # moved while paused: a new Play
    av.play()
    assert av._play_origin == origin + 2000
    av.hard_stop()


def test_play_names_the_streams_of_unknown_rate_it_plays_at_the_assumed_rate(monkeypatch):
    from swi3s_studio.ui import audio_view
    if not audio_view._HAVE_AUDIO:
        pytest.skip("no QtMultimedia")
    av, store = _delayed_mix()
    store._rates[(0, 3)] = 0.0
    monkeypatch.setattr(audio_view, "QAudioSink", _Sink)
    monkeypatch.setattr(av, "_selected_device", lambda: type("D", (), {"isNull": lambda s: False})())
    av.play()
    try:
        assert av.assumed_rate_streams == [(0, 3)]
    finally:
        av.hard_stop()
    store._rates[(0, 3)] = 48000.0
    av.play()
    try:
        assert av.assumed_rate_streams == []
    finally:
        av.hard_stop()
