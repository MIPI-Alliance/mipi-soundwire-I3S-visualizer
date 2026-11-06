"""The cursor tick refreshes a dock only while it is on screen, and raising a tab behind
another refreshes it at once.

A tab behind the raised one reports isVisible() and is not hidden, so only its visible
region says whether it is showing.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def _settled_window():
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_session(Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap))
    win.show()
    for _ in range(3):
        app.processEvents()
    return win, app


def _raise(win, app, dock) -> None:
    win._show_dock(dock)
    dock.raise_()
    for _ in range(3):
        app.processEvents()


def test_a_tab_behind_another_is_not_showing_until_it_is_raised():
    win, app = _settled_window()
    _raise(win, app, win._raw_dock)
    assert win._dock_showing(win._raw_dock)
    for behind in (win._symbol_dock, win._sample_dock, win._grid_dock):
        assert not behind.isHidden() and not win._dock_showing(behind)
    _raise(win, app, win._symbol_dock)
    assert win._dock_showing(win._symbol_dock) and not win._dock_showing(win._raw_dock)
    win.join_worker_threads()


def test_a_hidden_dock_is_not_showing_and_an_unshown_window_shows_what_is_not_hidden():
    win, app = _settled_window()
    win._sample_dock.hide()
    assert not win._dock_showing(win._sample_dock)
    win.close()
    from swi3s_studio.ui.main_window import MainWindow
    fresh = MainWindow()
    assert not fresh.isVisible()
    for dock in (fresh._grid_dock, fresh._reg_dock, fresh._symbol_dock, fresh._sample_dock):
        assert fresh._dock_showing(dock) == (not dock.isHidden())
    fresh._show_dock(fresh._grid_dock)
    assert not fresh.isVisible() and fresh._dock_showing(fresh._grid_dock)


def test_the_cursor_tick_skips_the_cds_behind_another_tab_and_raising_it_refreshes_it(monkeypatch):
    win, app = _settled_window()
    seen = []
    monkeypatch.setattr(win, "_refresh_symbols_around", lambda sample: seen.append(sample))
    sample = int(win._session.capture.clock_edges[len(win._session.capture.clock_edges) // 2])

    def tick() -> None:
        win._pending_cursor = (sample, False, False, False, -1)
        win._apply_cursor_heavy()

    _raise(win, app, win._symbol_dock)
    seen.clear()
    tick()
    assert seen == [sample], "the tick did not refresh the CDS tab that is showing"
    _raise(win, app, win._raw_dock)
    seen.clear()
    tick()
    assert seen == [], "a CDS tab behind another was refreshed by the cursor tick"
    _raise(win, app, win._symbol_dock)
    assert seen, "raising the CDS tab did not refresh it"
    win.join_worker_threads()


def test_raising_the_registers_tab_shows_the_phy_block_in_force_at_the_cursor(monkeypatch):
    win, app = _settled_window()
    _raise(win, app, win._meas_dock)
    assert not win._dock_showing(win._reg_dock)
    last = int(win._session.last_sample())
    phy_seen = []
    monkeypatch.setattr(win._session, "phy_at", lambda sample: 1 if sample >= last // 2 else 2)
    monkeypatch.setattr(win._reg_view, "set_phy", phy_seen.append)
    win.cursor.set_sample(last)
    win._apply_cursor_heavy()
    assert phy_seen == [], "the Registers tab behind another was refreshed by the cursor tick"
    _raise(win, app, win._reg_dock)
    assert phy_seen and phy_seen[-1] == 1
    win.join_worker_threads()


@pytest.mark.parametrize("which", ["cds", "samples"])
def test_floating_a_tab_behind_another_refreshes_it_at_the_cursor(which):
    """A pane floated out of its tab group is on screen without being raised: it is
    windowed at the cursor once the layout settles."""
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_session(Session.from_demo(1000, cold_start=True, phy=2, register_map=win._rmap))
    win.show()
    for _ in range(3):
        app.processEvents()
    _raise(win, app, win._raw_dock)
    sess = win._session
    sample = int(sess.audio_start_sample + (sess.last_sample() - sess.audio_start_sample) * 0.7)
    win.cursor.set_sample(sample)
    dock = win._symbol_dock if which == "cds" else win._sample_dock
    assert not win._dock_showing(dock)
    dock.setFloating(True)
    for _ in range(3):
        app.processEvents()
    if which == "cds":
        assert win._symbol_view._symbols == sess.symbols_around(win.cursor.sample)
    else:
        span = win._sample_view.shown_span()
        assert span is not None and span[0] <= win.cursor.sample <= span[1]
    dock.setFloating(False)
    for _ in range(3):
        app.processEvents()
    win.join_worker_threads()
