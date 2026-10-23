"""Registers ▸ History…: the window's filter, the row it maps a double-click to, where the
cursor lands, which Link it lands on, and the palette it draws in. Offscreen Qt.

The dialog cases build it from synthetic RegisterEvents; the window cases open it on demo
Links, one of them on a Link other than the active one.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.analysis.register_history import RegisterEvent


def _app():
    return QApplication.instance() or QApplication([])


def _event(i, sample, status="OK"):
    return RegisterEvent(command_index=i, start_sample=sample, bus_row=i, access="Write",
                         value=i, applied=status == "OK", status=status, response="")


def _dialog(events):
    from swi3s_studio.ui.register_history_dialog import RegisterHistoryDialog
    _app()
    return RegisterHistoryDialog("reg", events, 1e6, lambda r: r)


EVENTS = [_event(0, 100), _event(1, 200, "WRITE_FAILED"), _event(2, 300),
          _event(3, 400, "no response")]


def test_failed_attempts_only_shows_the_failed_rows_and_maps_a_double_click_to_them():
    dlg = _dialog(EVENTS)
    try:
        assert dlg._failed_only.isEnabled()
        assert dlg._table.rowCount() == 4
        dlg._failed_only.setChecked(True)
        assert dlg._table.rowCount() == 2
        assert [dlg._table.item(r, 5).text() for r in range(2)] == ["WRITE_FAILED",
                                                                     "no response"]
        assert dlg.event_at(0) is EVENTS[1] and dlg.event_at(1) is EVENTS[3]
        assert dlg.event_at(2) is None
        got = []
        dlg.jump.connect(got.append)
        dlg._on_double_click(1, 0)
        assert got == [400]
        dlg._failed_only.setChecked(False)
        assert dlg._table.rowCount() == 4 and dlg.event_at(1) is EVENTS[1]
    finally:
        dlg.close()


def test_with_no_failed_attempt_the_filter_is_disabled():
    dlg = _dialog([_event(0, 100), _event(1, 200)])
    try:
        assert not dlg._failed_only.isEnabled()
    finally:
        dlg.close()


# ------------------------------------------------------------------------- in the window
def _session(win):
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)


def _first_write(sess):
    w = next(c for c in sess.commands if c.get("command") == "WriteA32" and c.get("crc_valid"))
    dev = (int(w["device_mask"]) & -int(w["device_mask"])).bit_length() - 1
    return dev, int(w["address"]), w


@pytest.fixture
def three():
    """Links A, B and C; C 5 ms later on the shared timeline."""
    _app()
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_session(_session(win))
    win.load_session(_session(win), add_link=True)
    win.load_session(_session(win), add_link=True)
    win.set_link_offset(2, 5_000_000_000)
    win.switch_link(0)
    QApplication.processEvents()
    yield win
    for dlg in win._history_dialogs():
        dlg.close()


def test_a_jump_lands_on_the_commands_row_sync_point(three):
    sess = three._session
    dev, addr, w = _first_write(sess)
    dlg = three.show_register_history(dev, addr, "label")
    three.cursor.set_sample(0)
    dlg._on_double_click(0, 0)
    assert three.cursor.sample == sess.command_cursor_sample(w)
    assert three.cursor.sample < int(w["start_sample"])         # the RSP, before Column 0 closes


def test_a_jump_follows_its_link_when_a_removal_renumbers_the_links(three):
    win = three
    c = win.links[2].session
    win.set_group_link("right", 2)
    dev, addr, w = _first_write(c)
    dlg = win.show_register_history(dev, addr, "label")
    win.remove_link(0, confirm=False)                 # C is now index 1
    win.switch_link(0)                                # B
    assert win.links.index_of(c) == 1
    dlg._on_double_click(0, 0)
    assert win.links.active_index == 1                # C, not B
    assert win.cursor.sample == c.command_cursor_sample(w)


def test_a_history_window_closes_with_its_link_and_on_a_new_capture(three):
    win = three
    win.set_group_link("right", 2)
    dev, addr, _w = _first_write(win.links[2].session)
    on_c = win.show_register_history(dev, addr, "C")
    win.set_group_link("right", 0)
    on_a = win.show_register_history(dev, addr, "A")
    assert on_c.isVisible() and on_a.isVisible()
    win.remove_link(2, confirm=False)
    assert not on_c.isVisible() and on_a.isVisible()
    win.load_session(_session(win))                   # an open replaces the analysis
    assert not on_a.isVisible()


def test_a_jump_from_a_link_that_is_gone_does_nothing(three):
    win = three
    win.set_group_link("right", 2)
    dev, addr, _w = _first_write(win.links[2].session)
    dlg = win.show_register_history(dev, addr, "C")
    win.remove_link(2, confirm=False)
    win.cursor.set_sample(123)
    before = win.links.active_index
    dlg._on_double_click(0, 0)
    assert win.cursor.sample == 123 and win.links.active_index == before


def test_an_open_history_window_follows_a_theme_switch(three, monkeypatch):
    from swi3s_studio.ui.theme import VizTheme
    win = three
    monkeypatch.setattr("swi3s_studio.ui.main_window.save_preference", lambda _p: None)
    win.apply_theme("dark")
    dlg = win.show_register_history(*_first_write(win._session)[:2], "label")
    dlg._events.append(_event(99, 10 ** 9, "WRITE_FAILED"))
    dlg._fill()
    row = dlg._table.rowCount() - 1
    dark_tint, dark_sheet = VizTheme.SEM_ERROR_BG, dlg.styleSheet()
    try:
        win.apply_theme("light")
        assert VizTheme.SEM_ERROR_BG != dark_tint
        assert dlg.styleSheet() != dark_sheet
        assert dlg._table.item(row, 0).background().color().name().lower() == \
            VizTheme.SEM_ERROR_BG.lower()
    finally:
        win.apply_theme("dark")


def test_an_open_history_window_re_reads_its_link_after_a_re_decode(three):
    """A re-decode changes the Session's commands in place: the window shows the accesses
    of the new decode, and a double-click lands on one of them."""
    win = three
    sess = win._session
    dev, addr, w = _first_write(sess)
    dlg = win.show_register_history(dev, addr, "label")
    before = dlg._table.rowCount()
    assert before >= 1 and dlg.event_at(0).start_sample == int(w["start_sample"])
    sess.commands = [c for c in sess.commands if c is not w]    # the new decode lacks it
    win.load_session(sess)                                      # the re-decode path
    assert dlg.isVisible()
    assert dlg._table.rowCount() == before - 1
    assert all(dlg.event_at(r).start_sample != int(w["start_sample"])
               for r in range(dlg._table.rowCount()))
    if dlg._table.rowCount():
        dlg._on_double_click(0, 0)
        assert win.cursor.sample == sess.row_sync_sample(int(dlg.event_at(0).start_sample))


def test_a_re_decode_of_another_link_leaves_the_window_as_it_is(three):
    win = three
    a, b = win.links[0].session, win.links[1].session
    dev, addr, w = _first_write(a)
    dlg = win.show_register_history(dev, addr, "A")
    before = dlg._table.rowCount()
    b.commands = [c for c in b.commands if c.get("command") != "WriteA32"]
    win.load_session(b)
    assert dlg._table.rowCount() == before


def test_set_events_updates_the_summary_and_the_filter():
    dlg = _dialog(EVENTS)
    try:
        dlg._failed_only.setChecked(True)
        dlg.set_events([_event(0, 100)])
        assert not dlg._failed_only.isEnabled() and not dlg._failed_only.isChecked()
        assert dlg._table.rowCount() == 1 and "1 access(es)" in dlg._summary.text()
        dlg.set_events([])
        assert dlg._table.rowCount() == 0 and "not written or read" in dlg._summary.text()
    finally:
        dlg.close()
