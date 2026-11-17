"""A re-decode runs on a fork of the Link's Session (Session.fork) and is adopted on the GUI
thread when it is done (Session.adopt), so the GUI only ever reads a whole Session.

Each test pauses the load worker at a fixed point inside the re-decode, on an Event, and
reads the live Session from the GUI thread while it waits: the interleaving is fixed, not
left to timing.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui

_WAIT_S = 60.0

# What a re-decode replaces and the GUI reads: the decoder and its source, and the results.
_DECODE_STATE = ("decoder", "_source", "commands", "segments", "register_files")
# Caches over the decode that the re-decode resets and a cursor move fills.
_CACHES = ("_rel_cmds", "_eff_sorted", "_reg_replay_cache", "_reg_checkpoints",
           "_commit_rsp_cache")


def _pump_until(pred, what: str) -> None:
    app = QApplication.instance()
    deadline = time.time() + _WAIT_S
    while not pred():
        assert time.time() < deadline, f"timed out waiting for {what}"
        app.processEvents()
        time.sleep(0.005)


def _loaded_window(monkeypatch):
    """A window with a demo capture loaded, and the list each load_session call adds its
    session to."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    base = Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)
    win.load_session(base)
    loads = []
    load = win.load_session

    def counted(session, *a, **k):
        loads.append(session)
        return load(session, *a, **k)
    monkeypatch.setattr(win, "load_session", counted)
    return win, base, loads


def _mid_sample(session) -> int:
    seg = session.segments[len(session.segments) // 2]
    return int(seg["start_sample"]) + 1000


def _pause(monkeypatch, owner, name):
    """Make owner.name wait, on the worker, until the returned `release` is set; `reached`
    is set when it starts waiting and `args` holds what it was called with."""
    real = getattr(owner, name)
    reached, release, args = threading.Event(), threading.Event(), []

    def paused(*a):
        args.append(a)
        reached.set()
        assert release.wait(_WAIT_S)
        return real(*a)
    monkeypatch.setattr(owner, name, paused)
    return reached, release, args


def test_the_gui_never_sees_the_decoder_of_a_running_re_decode(monkeypatch):
    """While the re-decode's decoder is about to run, the window's Session still holds the
    decoder, source and results it had, and a cursor move reads them; the decode is adopted
    into that same Session afterwards and the views are rebuilt once."""
    from swi3s_studio import session as session_mod
    win, base, loads = _loaded_window(monkeypatch)
    before = {k: base.__dict__[k] for k in _DECODE_STATE}
    depths = dict(base.hub_depths)
    reached, release, args = _pause(monkeypatch, session_mod, "run_watched")
    win._apply_hub_depths_async({1: 1})
    try:
        _pump_until(reached.is_set, "the re-decode's decoder")
        running = args[0][0]
        live = win._session
        assert live is base
        assert live.decoder is not running
        for k, v in before.items():
            assert live.__dict__[k] is v, k
        assert live.hub_depths == depths
        win._on_seek(_mid_sample(live))            # every pane re-reads the session
        live.port_bit_marks(0, _mid_sample(live))  # the Capture pane's native read
        live.register_files_at(_mid_sample(live))
    finally:
        release.set()
    pump_loads(win, seconds=_WAIT_S)
    assert win._session is base
    assert base.decoder is running
    assert base.hub_depths == {1: 1}
    assert loads == [base]
    win.join_worker_threads()


def test_caches_a_cursor_move_fills_during_a_re_decode_are_dropped_with_the_old_decode(
        monkeypatch):
    """A cursor move while the re-decode copies its audio out reads the old decode, whole;
    the caches it fills belong to that decode and are not kept over the adopted one."""
    from swi3s_studio.session import Session
    win, base, loads = _loaded_window(monkeypatch)
    commands = base.commands
    reached, release, _ = _pause(monkeypatch, Session, "_refresh_audio")
    win._apply_hub_depths_async({1: 1})
    try:
        _pump_until(reached.is_set, "the re-decode's audio copy")
        live = win._session
        assert live.commands is commands
        win._on_seek(_mid_sample(live))
        live.register_files_at(_mid_sample(live))
        filled = {k: live.__dict__.get(k) for k in _CACHES}
        assert any(v is not None for v in filled.values())
    finally:
        release.set()
    pump_loads(win, seconds=_WAIT_S)
    assert base.commands is not commands
    for k, v in filled.items():
        if v is not None:
            assert base.__dict__.get(k) is not v, k
    assert loads == [base]
    win.join_worker_threads()


def test_a_name_set_during_a_re_decode_is_kept_and_the_decode_is_adopted(monkeypatch):
    from swi3s_studio import session as session_mod
    win, base, loads = _loaded_window(monkeypatch)
    reached, release, _ = _pause(monkeypatch, session_mod, "run_watched")
    win._apply_hub_depths_async({1: 1})
    try:
        _pump_until(reached.is_set, "the re-decode's decoder")
        base.set_dp_name(1, 1, "Mic L")
        base.set_region_name(0, "Boot")
    finally:
        release.set()
    pump_loads(win, seconds=_WAIT_S)
    assert base.hub_depths == {1: 1}
    assert base.dp_names[(1, 1)] == "Mic L" and base.region_names[0] == "Boot"
    assert base.source["dp_names"] == {"1.1": "Mic L"}
    assert base.source["region_names"] == {"0": "Boot"}
    win.join_worker_threads()


def test_a_re_decode_queued_behind_another_forks_what_the_first_left(monkeypatch):
    win, base, loads = _loaded_window(monkeypatch)
    win._apply_hub_depths_async({1: 1})

    def rescramble(s):
        s.set_scrambler_overrides({(1, 1): True})
        return s
    win._redecode_preserving(rescramble)
    pump_loads(win, seconds=_WAIT_S)
    assert base.hub_depths == {1: 1}
    assert base.scrambler_overrides == {(1, 1): True}
    assert loads == [base, base]
    win.join_worker_threads()


def test_a_re_decode_that_returns_another_session_fails_and_leaves_the_link(monkeypatch):
    from swi3s_studio.ui import main_window
    win, base, loads = _loaded_window(monkeypatch)
    decoder = base.decoder
    shown, failed = [], []
    monkeypatch.setattr(main_window.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    win._load_async(lambda fork: base, kind="redecode", on_fail=lambda: failed.append(1))
    pump_loads(win, seconds=_WAIT_S)
    assert failed == [1] and len(shown) == 1 and "fork" in shown[0]
    assert loads == [] and base.decoder is decoder
    win.join_worker_threads()


def _results_seen(session, store):
    """What a reader of `session` sees of its on-disk results: the directory and its files,
    the command table and the audio samples."""
    import numpy as np
    root = session.results_dir
    files = sorted(os.path.relpath(os.path.join(d, f), root)
                   for d, _dirs, names in os.walk(root) for f in names)
    channels = {k: np.asarray(v).copy() for k, v in store._channels.items()}
    return root, session.commands_path, files, session.commands_table(), channels


def _assert_results_unchanged(session, store, before):
    import numpy as np
    root, path, files, table, channels = before
    now = _results_seen(session, store)
    assert now[:3] == (root, path, files)
    assert now[3].equals(table)
    assert now[4].keys() == channels.keys()
    assert all(np.array_equal(now[4][k], channels[k]) for k in channels)


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
def test_a_re_decode_writes_its_own_results_and_one_not_adopted_leaves_the_live_ones(
        monkeypatch, ending):
    """A large capture's re-decode writes its command table and audio files in a results
    directory of its own: the GUI reads the live Session's results, unchanged, while it runs,
    and a re-decode that is cancelled or fails removes its directory and leaves the live
    Session's as they were."""
    from swi3s_studio.session import Session, _SessionFork
    from swi3s_studio.ui import main_window
    monkeypatch.setattr(Session, "LARGE_CAPTURE_AUDIO_BYTES", -1)
    win, base, loads = _loaded_window(monkeypatch)
    store = win._links[0].view.audio_store
    before = _results_seen(base, store)
    decoder = base.decoder
    reached, release, forks = threading.Event(), threading.Event(), []
    real = Session.audio_store

    def paused(self, *a, **k):
        out = real(self, *a, **k)
        if isinstance(self, _SessionFork):
            forks.append(self.results_dir)
            reached.set()
            assert release.wait(_WAIT_S)
        return out
    monkeypatch.setattr(Session, "audio_store", paused)
    shown = []
    monkeypatch.setattr(main_window.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))

    def redecode(fork):
        fork._redecode()
        if ending == "failed":
            fork.audio_store()
            raise RuntimeError("a step after the decode failed")
        return fork
    win._redecode_preserving(redecode)
    try:
        _pump_until(reached.is_set, "the re-decode's results")
        assert forks[0] not in (None, before[0]) and os.listdir(forks[0])
        _assert_results_unchanged(base, store, before)
        win._on_seek(_mid_sample(base))            # every pane re-reads the live session
        if ending == "cancelled":
            win._load_worker.cancel()
    finally:
        release.set()
    pump_loads(win, seconds=_WAIT_S)
    assert not os.path.exists(forks[0])
    assert base.decoder is decoder and loads == []
    assert len(shown) == (0 if ending == "cancelled" else 1)
    _assert_results_unchanged(base, store, before)
    win.join_worker_threads()
