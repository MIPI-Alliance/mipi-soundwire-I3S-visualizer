"""The window closes a Session it drops (Session.close), so a large capture's on-disk
results go as its Link does: when another capture or a workspace replaces the analysis, when
its Link is removed, when a re-decode is adopted (the results it replaced) and when the
window closes. No pane reads an audio store closed with them."""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session

pytestmark = pytest.mark.gui


@pytest.fixture
def win(monkeypatch):
    """A window with the two-Link demo, every decode large. `w.shown_when_closed` lists each
    audio store a pane or a Link still held when it was closed."""
    from swi3s_studio.store.audio_store import AudioStore
    from swi3s_studio.ui.audio_view import AudioView
    monkeypatch.setattr(Session, "LARGE_CAPTURE_AUDIO_BYTES", -1)   # every decode is large
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.shown_when_closed = []
    close = AudioStore.close

    def watched(store):
        held = [v for v in w.findChildren(AudioView) if getattr(v, "_store", None) is store]
        held += [link for link in w.links if link.view.audio_store is store]
        if held and not store.closed:
            w.shown_when_closed.append(store)
        close(store)
    monkeypatch.setattr(AudioStore, "close", watched)
    w.load_demo_links()
    return w


@pytest.fixture
def no_gc():
    """Garbage collection off, so what is removed is removed by the window."""
    gc.disable()
    yield
    gc.enable()


_HELD: list = []


def _roots(win):
    """Each Link's results directory. Its Session is held here too, so the directory is
    removed only by the window closing the Session, not by its last reference going."""
    _HELD[:] = [link.session for link in win.links]
    return [link.session.results_dir for link in win.links]


def _assert_no_pane_reads_a_closed_store(win):
    from swi3s_studio.ui.audio_view import AudioView
    assert win.shown_when_closed == []
    for link in win.links:
        assert not link.view.audio_store.closed, link.name
    for view in win.findChildren(AudioView):
        store = getattr(view, "_store", None)
        assert store is None or not store.closed
    win.grab()                                         # every pane paints
    s = win._session
    win._on_seek(int(s.segments[len(s.segments) // 2]["start_sample"]))


def test_a_replaced_capture_removes_its_results_at_once(win, no_gc):
    roots = _roots(win)
    assert all(os.listdir(r) for r in roots)
    win.load_session(Session.from_demo(32))
    assert not any(os.path.exists(r) for r in roots)
    _assert_no_pane_reads_a_closed_store(win)


def test_a_capture_opened_over_the_analysis_removes_its_results(win, no_gc):
    from conftest import pump_loads
    roots = _roots(win)
    win._load_async(lambda **kw: Session.from_demo(32, **kw), kind="open")
    pump_loads(win)
    assert not any(os.path.exists(r) for r in roots)
    assert os.listdir(win._session.results_dir)
    _assert_no_pane_reads_a_closed_store(win)


def test_a_workspace_load_removes_the_results_it_replaces(win, no_gc, tmp_path, monkeypatch):
    from conftest import pump_loads
    from PySide6.QtWidgets import QFileDialog
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    roots = _roots(win)
    win.open_workspace()
    pump_loads(win)
    assert len(win.links) == 2
    assert not any(os.path.exists(r) for r in roots)
    assert all(os.listdir(r) for r in _roots(win))
    _assert_no_pane_reads_a_closed_store(win)


def test_an_adopted_re_decode_removes_the_results_it_replaced(win, no_gc):
    from conftest import pump_loads
    session = win._session
    old = session.results_dir

    def redecode(fork):
        fork._redecode()
        return fork
    win._redecode_preserving(redecode)
    pump_loads(win)
    assert win._session is session
    assert not os.path.exists(old) and os.listdir(session.results_dir)
    _assert_no_pane_reads_a_closed_store(win)


def test_a_removed_link_removes_its_results_and_keeps_the_others(win, no_gc):
    first, second = _roots(win)
    win.remove_link(1, confirm=False)
    assert not os.path.exists(second) and os.listdir(first)
    _assert_no_pane_reads_a_closed_store(win)


def test_a_link_removed_while_another_re_decodes_removes_its_results_at_once(win, no_gc):
    import threading

    from conftest import pump_loads
    first, second = _roots(win)
    win.switch_link(0)
    gate = threading.Event()

    def redecode(fork):
        assert gate.wait(60)
        fork._redecode()
        return fork
    win._redecode_preserving(redecode)                 # of the active Link, the first
    try:
        win.remove_link(1, confirm=False)
        assert not os.path.exists(second)
    finally:
        gate.set()
    pump_loads(win)
    assert not os.path.exists(first) and os.listdir(win._session.results_dir)
    _assert_no_pane_reads_a_closed_store(win)


def test_closing_the_window_removes_every_links_results(win, no_gc):
    roots = _roots(win)
    win.close()
    assert not any(os.path.exists(r) for r in roots)


def test_a_capture_opened_asynchronously_removes_the_replaced_results_at_once(win):
    from conftest import pump_loads
    roots = [link.session.results_dir for link in win.links]
    assert all(os.listdir(r) for r in roots)
    gc.disable()
    try:
        win._load_async(lambda: Session.from_demo(32))
        pump_loads(win)
        assert not any(os.path.exists(r) for r in roots)
        assert win._dropped_sessions == []
    finally:
        gc.enable()


def test_a_link_removed_while_a_search_runs_is_closed_when_it_ends(win):
    first, second = (link.session.results_dir for link in win.links)
    gc.disable()
    try:
        win._locate_ctl.search_thread = object()          # a sub-capture search in flight
        win.remove_link(1, confirm=False)
        assert os.path.exists(second)                      # the worker may still read it
        win._locate_ctl.search_thread = None
        win._close_dropped_sessions()
        assert not os.path.exists(second) and os.listdir(first)
    finally:
        win._locate_ctl.search_thread = None
        gc.enable()
