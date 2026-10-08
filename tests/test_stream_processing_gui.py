"""High-Pass & Gain in the window: the dialog's fields, Preview / Cancel / Apply, the
stream menu, Revert, and the setting staying with its Link through a re-decode and a
saved workspace. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from conftest import pump_loads
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDialog, QMenu
from test_stream_processing import _store, _tone

from swi3s_studio.store.audio_store import StreamProcessing

_PROCESS = "High-Pass && Gain…"


def _revert(dlg):
    """The dialog's Revert button, as clicked."""
    dlg._revert_btn.click()
    return QDialog.Accepted


def _dialog(store, previews=None):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.audio_process_dialog import StreamProcessingDialog
    seen = [] if previews is None else previews

    def preview(proc):
        seen.append(proc)
        store.set_processing(1, 2, proc)
    return StreamProcessingDialog(store, 1, 2, preview=preview)


# ---------------------------------------------------------------- the dialog
def test_it_opens_on_a_20_hz_high_pass_and_the_gain_that_normalises_after_it():
    store = _store([_tone(dc=4000.0, amp=1000.0)])
    dlg = _dialog(store)
    proc = dlg.processing()
    assert proc.highpass_hz == 20.0
    assert dlg.peak_db() == pytest.approx(store.peak_db(1, 2, highpass_hz=20.0))
    assert proc.gain_db == pytest.approx(-dlg.peak_db(), abs=0.05)
    assert dlg._new_peak.value() == pytest.approx(0.0, abs=0.05)
    assert not proc.allow_clipping and dlg._apply_btn.isEnabled()


def test_amplification_and_new_peak_are_one_number():
    dlg = _dialog(_store([_tone()]))
    peak = dlg.peak_db()
    dlg._gain.setValue(3.0)
    assert dlg._new_peak.value() == pytest.approx(peak + 3.0, abs=0.05)
    dlg._new_peak.setValue(-12.0)
    assert dlg._gain.value() == pytest.approx(-12.0 - peak, abs=0.05)
    dlg._gain_slider.setValue(-60)                       # -6.0 dB
    assert dlg._gain.value() == pytest.approx(-6.0)


def test_the_peak_follows_the_corner_and_an_untouched_gain_keeps_normalising():
    store = _store([_tone(dc=4000.0, amp=1000.0)])
    dlg = _dialog(store)
    dlg._hp_on.setChecked(False)
    dlg._measure()                                       # what the debounce timer runs
    assert dlg.peak_db() == pytest.approx(store.peak_db(1, 2))          # offset included
    assert dlg._gain.value() == pytest.approx(-store.peak_db(1, 2), abs=0.05)
    dlg._gain.setValue(-3.0)                             # now the user's number
    dlg._hp_on.setChecked(True)
    dlg._measure()
    assert dlg._gain.value() == pytest.approx(-3.0)


def test_the_corner_slider_is_logarithmic_from_1_hz_to_the_maximum():
    dlg = _dialog(_store([_tone()]))
    dlg._corner_slider.setValue(0)
    assert dlg._corner.value() == pytest.approx(1.0)
    dlg._corner_slider.setValue(dlg._corner_slider.maximum())
    assert dlg._corner.value() == pytest.approx(20_000.0)
    dlg._corner_slider.setValue(dlg._corner_slider.maximum() // 2)
    assert dlg._corner.value() == pytest.approx(np.sqrt(20_000.0), rel=0.01)


def test_without_allow_clipping_a_gain_past_full_scale_cannot_be_applied():
    dlg = _dialog(_store([_tone()]))
    dlg._new_peak.setValue(3.0)
    assert dlg.clips() and not dlg._apply_btn.isEnabled() and not dlg._preview_btn.isEnabled()
    dlg._clip.setChecked(True)
    assert dlg._apply_btn.isEnabled() and dlg.processing().allow_clipping


def test_preview_shows_it_and_cancel_puts_back_what_was_there():
    store = _store([_tone()])
    decoded = np.array(store.samples(1, 2, 0))
    seen = []
    dlg = _dialog(store, seen)
    dlg.preview()
    assert store.processing(1, 2) == dlg.processing()
    dlg.reject()
    assert seen[-1] is None and store.processing(1, 2) is None
    np.testing.assert_array_equal(store.samples(1, 2, 0), decoded)


def test_cancel_returns_to_an_earlier_setting_not_to_none():
    store = _store([_tone()])
    before = StreamProcessing(highpass_hz=100.0, gain_db=-2.0)
    store.set_processing(1, 2, before)
    dlg = _dialog(store)
    assert dlg.processing() == before                   # it opens on the current setting
    dlg._gain.setValue(4.0)
    dlg.preview()
    dlg.reject()
    assert store.processing(1, 2) == before


def test_a_silent_stream_has_no_peak_to_normalise_to():
    dlg = _dialog(_store([np.zeros(2000, dtype=np.int64)]))
    assert dlg.peak_db() is None and not dlg._new_peak.isEnabled()
    assert dlg.processing().gain_db == 0.0 and dlg._apply_btn.isEnabled()


def test_a_stream_without_a_rate_offers_gain_only():
    store = _store([_tone()])
    store._rates[(1, 2)] = 0.0
    dlg = _dialog(store)
    assert not dlg._hp_on.isEnabled() and dlg.processing().highpass_hz is None


# ---------------------------------------------------------------- the window
@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.load_demo()
    return w


def _stream(win):
    return win._audio_store.streams()[0]


def _labels(win, dev, dp):
    """(checkbox text, tooltip) of each of the stream's channels."""
    return [(cb.text(), cb.toolTip()) for (d, p, _c), cb in win._audio_view._checks.items()
            if (d, p) == (dev, dp)]


def _marked(win, dev, dp, setting):
    """Every channel of the stream marked processed, with `setting` in its tooltip, and
    spelled out in its track's title."""
    titles = [p[0].titleLabel.text for p in win._audio_view._plots if p[2:4] == (dev, dp)]
    return (bool(titles) and all(setting in t for t in titles)
            and all(t.endswith(" *") and tip.startswith(setting)
                    for t, tip in _labels(win, dev, dp)))


def _menu(win, monkeypatch, which, dev, dp, dialog=None):
    cb = next(c for (d, p, _ch), c in win._audio_view._checks.items() if (d, p) == (dev, dp))
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: next(
        a for a in m.actions() if a.text() == which))
    if dialog is not None:
        from swi3s_studio.ui import audio_process_dialog as apd
        monkeypatch.setattr(apd.StreamProcessingDialog, "exec", dialog)
    win._audio_view._stream_menu(cb, QPoint(0, 0), dev, dp)


def _accept_with(**fields):
    def run(dlg):
        if "highpass" in fields:
            dlg._hp_on.setChecked(fields["highpass"] is not None)
            if fields["highpass"] is not None:
                dlg._corner.setValue(fields["highpass"])
        if "gain" in fields:
            dlg._gain.setValue(fields["gain"])
        dlg._clip.setChecked(True)
        return QDialog.Accepted
    return run


def test_the_stream_menu_applies_it_and_says_so_and_revert_undoes_it(win, monkeypatch):
    dev, dp = _stream(win)
    store = win._audio_store
    decoded = np.array(store.samples(dev, dp, store.channels(dev, dp)[0]))
    _menu(win, monkeypatch, _PROCESS, dev, dp, _accept_with(highpass=50.0, gain=6.0))
    proc = StreamProcessing(highpass_hz=50.0, gain_db=6.0, allow_clipping=True)
    assert win._links[0].view.stream_processing == {(dev, dp): proc}
    assert store.processing(dev, dp) == proc
    assert _marked(win, dev, dp, "HPF 50 Hz, +6.0 dB")
    assert "HPF 50 Hz, +6.0 dB" in win._status.text()

    _menu(win, monkeypatch, _PROCESS, dev, dp, _revert)
    assert win._links[0].view.stream_processing == {} and store.processing(dev, dp) is None
    assert all(not t.endswith("*") and not tip for t, tip in _labels(win, dev, dp))
    np.testing.assert_array_equal(store.samples(dev, dp, store.channels(dev, dp)[0]), decoded)


def test_revert_is_in_the_dialog_and_only_for_a_processed_stream(win, monkeypatch):
    """One place to undo it: the dialog. The right-click menu only opens the dialog."""
    dev, dp = _stream(win)
    seen = []
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: seen.extend(a.text() for a in m.actions()))
    cb = next(iter(win._audio_view._checks.values()))
    win._audio_view._stream_menu(cb, QPoint(0, 0), dev, dp)
    assert _PROCESS in seen and not any("Revert" in t for t in seen)
    store = win._audio_store
    assert not _dialog_for(store, dev, dp)._revert_btn.isEnabled()
    win.set_stream_processing(dev, dp, StreamProcessing(gain_db=1.0))
    assert _dialog_for(store, dev, dp)._revert_btn.isEnabled()


def _dialog_for(store, dev, dp):
    from swi3s_studio.ui.audio_process_dialog import StreamProcessingDialog
    return StreamProcessingDialog(store, dev, dp, preview=lambda _p: None)


def test_revert_wins_over_whatever_the_fields_hold():
    store = _store([_tone()])
    store.set_processing(1, 2, StreamProcessing(gain_db=4.0))
    dlg = _dialog(store)
    dlg._gain.setValue(9.0)
    dlg._revert_btn.click()
    assert dlg.result() == QDialog.Accepted and dlg.chosen() is None

def test_a_cancelled_dialog_changes_nothing(win, monkeypatch):
    dev, dp = _stream(win)
    _menu(win, monkeypatch, _PROCESS, dev, dp, lambda dlg: QDialog.Rejected)
    assert win._links[0].view.stream_processing == {}
    assert win._audio_store.processing(dev, dp) is None


def test_a_re_decode_keeps_the_streams_setting(win):
    dev, dp = _stream(win)
    proc = StreamProcessing(highpass_hz=200.0, gain_db=-3.0)
    win.set_stream_processing(dev, dp, proc)
    old = win._audio_store
    win.load_session(win._session)                     # what an SSP step or an override does
    assert win._audio_store is not old and win._audio_store.processing(dev, dp) == proc
    assert _marked(win, dev, dp, "HPF 200 Hz, -3.0 dB")


def test_it_belongs_to_its_link(win):
    from swi3s_studio.session import Session
    dev, dp = _stream(win)
    proc = StreamProcessing(gain_db=5.0)
    win.set_stream_processing(dev, dp, proc, link=0)
    win.load_session(Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap),
                     add_link=True)
    second = win.links[1].view
    assert second.stream_processing == {}
    assert all(second.audio_store.processing(d, p) is None for d, p in second.audio_store.streams())
    assert win.links[0].view.audio_store.processing(dev, dp) == proc


def test_a_saved_workspace_reopens_with_the_setting(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    dev, dp = _stream(win)
    proc = StreamProcessing(highpass_hz=20.0, gain_db=12.5, allow_clipping=True)
    win.set_stream_processing(dev, dp, proc)
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    again = MainWindow()
    again.open_workspace()
    pump_loads(again)
    assert again.links[0].view.stream_processing == {(dev, dp): proc}
    assert again._audio_store.processing(dev, dp) == proc
    assert _marked(again, dev, dp, "HPF 20 Hz, +12.5 dB")


def test_a_damaged_setting_in_a_workspace_is_skipped():
    from swi3s_studio.ui.main_window import _processing_from_json
    rows = [[1, 2, {"gain_db": 3}], ["x", 2, {}], [1, 3, {"highpass_hz": -1}], [4, 0],
            [5, 1, {"gain_db": 0}]]
    assert _processing_from_json(rows) == {(1, 2): StreamProcessing(gain_db=3.0)}


def _audio_menu_entries(win):
    win._build_processing_menu()
    return {a.text(): a for a in win._process_menu.actions()}


def test_the_audio_menu_opens_the_dialog_for_every_stream(win, monkeypatch):
    """Audio ▸ High-Pass & Gain: one entry per stream, straight to its dialog (Revert is in
    there), and an asterisk on a processed stream."""
    from swi3s_studio.ui import audio_process_dialog as apd
    streams = win._audio_store.streams()
    entries = _audio_menu_entries(win)
    assert sorted(entries) == sorted(f"Device {d} · DP{p}…" for d, p in streams)
    dev, dp = streams[0]
    monkeypatch.setattr(apd.StreamProcessingDialog, "exec", _accept_with(highpass=300.0, gain=2.0))
    entries[f"Device {dev} · DP{dp}…"].trigger()
    assert win._audio_store.processing(dev, dp) == StreamProcessing(300.0, 2.0, True)
    entries = _audio_menu_entries(win)
    assert f"Device {dev} · DP{dp}… *" in entries
    monkeypatch.setattr(apd.StreamProcessingDialog, "exec", _revert)
    entries[f"Device {dev} · DP{dp}… *"].trigger()
    assert win._audio_store.processing(dev, dp) is None


def test_the_audio_menu_says_when_there_is_no_audio():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w._build_processing_menu()
    acts = w._process_menu.actions()
    assert len(acts) == 1 and not acts[0].isEnabled()
