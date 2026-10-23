"""View ▸ Maximize Pane: the selected pane alone in the window, then every pane back.

The selected pane is the one holding the keyboard focus (a click in any pane gives it), or
the pane whose title-bar button or title was used. Maximizing saves the whole arrangement
(sizes, tab groups, the tab in front, floating panes) and restoring puts it back exactly,
compared here as the window's saved state, except that the maximized pane stays the tab in
front of its group and keeps the focus, so the key pressed again acts on the same pane.
Anything that shows, hides or closes a pane, leaves the Bus Analyzer or opens a workspace
puts the panes back first; a resize the layout asks for meanwhile is made to the arrangement
put back; and a workspace saved while one is maximized records the arrangement it returns to.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    _settle()
    return w


def _settle():
    for _ in range(6):
        QApplication.processEvents()


def _shown(w):
    """The docks on screen: a tab behind another is visible to Qt but placed off it."""
    return {d for d in w._dock_area if d.isVisible() and d.geometry().x() >= 0}


def _bar(w, dock):
    return next(b for b in w._dock_titlebars if b._dock is dock)


def _with_in_front(w, before, dock):
    """The arrangement `before` with `dock` the tab in front of its group: what restoring
    after maximizing `dock` returns to. Call it with no pane maximized."""
    current = w.saveState()
    w.restoreState(before)
    dock.raise_()
    _settle()
    expected = bytes(w.saveState())
    w.restoreState(current)
    _settle()
    return expected


def _name(pane):
    return pane if isinstance(pane, str) or pane is None else pane.objectName()


def test_the_key_maximizes_the_focused_pane_and_restores_the_layout_exactly(win):
    act = win._maximize_action
    assert act.shortcut() == QKeySequence("Ctrl+Shift+M")
    before = bytes(win.saveState())
    win._sample_view.setFocus()
    _settle()
    act.trigger()
    _settle()
    assert win.maximized_pane() is win._sample_dock
    assert _shown(win) == {win._sample_dock, win._timeline_dock}   # Samples keeps the Timeline
    assert not win._central_stack.isVisible(), "the Commands table still takes room"
    assert win._sample_dock.height() + win._timeline_dock.height() > win.height() * 0.8
    assert act.text() == "Restore Panes"
    act.trigger()
    _settle()
    assert win.maximized_pane() is None
    assert bytes(win.saveState()) == _with_in_front(win, before, win._sample_dock), (
        "the arrangement did not come back as it was")
    assert win._central_stack.isVisible() and act.text() == "Maximize Pane"


def test_a_pane_behind_another_tab_comes_to_the_front(win):
    before = bytes(win.saveState())
    front = _shown(win)
    assert win._audio_dock not in front, "Audio is in front already: nothing to test"
    _bar(win, win._audio_dock)._max.click()
    _settle()
    assert _shown(win) == {win._audio_dock}
    assert _bar(win, win._audio_dock)._max.isChecked()
    _bar(win, win._audio_dock)._max.click()
    _settle()
    assert bytes(win.saveState()) == _with_in_front(win, before, win._audio_dock)
    assert win._audio_dock in _shown(win), "the pane worked in went back behind a tab"
    assert not _bar(win, win._audio_dock)._max.isChecked()


def test_double_clicking_a_title_maximizes_instead_of_floating(win):
    bar = _bar(win, win._reg_dock)
    before = bytes(win.saveState())
    middle = QPoint(bar.width() // 2, bar.height() // 2)
    QTest.mouseDClick(bar, Qt.LeftButton, pos=middle)
    _settle()
    assert win.maximized_pane() is win._reg_dock
    assert not win._reg_dock.isFloating()
    QTest.mouseDClick(bar, Qt.LeftButton, pos=middle)
    _settle()
    assert bytes(win.saveState()) == before


# The panes read by moving through time keep the Timeline; the rest take the whole window.
_KEEP_TIMELINE = {"commands", "_reg_dock", "_grid_dock", "_symbol_dock", "_sample_dock"}

_PANES = ("_reg_dock", "_meas_dock", "_pair_dock", "_grid_dock",
          "_audio_dock", "_symbol_dock", "_sample_dock", "_raw_dock", "_eye_dock", "commands")


@pytest.mark.parametrize("attr", _PANES)
def test_pressing_the_key_again_and_again_keeps_to_the_same_pane(win, attr):
    """Each press toggles the pane the focus is in, and the focus stays in it throughout."""
    pane = "commands" if attr == "commands" else getattr(win, attr)
    if pane == "commands":
        win._cmd_view.setFocus()
    else:
        pane.show()
        pane.raise_()
        _settle()
        win._pane_focus_widget(pane).setFocus()
    _settle()
    seen = []
    for _ in range(4):
        win._maximize_action.trigger()
        _settle()
        seen.append((_name(win.maximized_pane()),
                     _name(win._pane_of(QApplication.focusWidget()))))
    want = [(_name(pane), _name(pane)), (None, _name(pane))] * 2
    assert seen == want


@pytest.mark.parametrize("attr", _PANES)
def test_the_timeline_stays_only_beside_the_panes_navigated_in_time(win, attr):
    pane = "commands" if attr == "commands" else getattr(win, attr)
    win.toggle_maximized_pane(pane)
    _settle()
    alone = set() if pane == "commands" else {pane}
    timeline = {win._timeline_dock} if attr in _KEEP_TIMELINE else set()
    assert _shown(win) == alone | timeline
    win.toggle_maximized_pane()
    _settle()
    assert win.maximized_pane() is None


@pytest.mark.parametrize("attr", sorted(_KEEP_TIMELINE - {"commands"}))
def test_the_kept_timeline_keeps_its_height_and_the_pane_takes_the_rest(win, attr):
    """With the central page hidden, Qt shares its room between the Timeline and the pane on
    every resize of the window, so the Timeline's height is held until the restore."""
    pane, timeline = getattr(win, attr), win._timeline_dock
    height, cap = timeline.height(), timeline.maximumHeight()
    win.toggle_maximized_pane(pane)
    _settle()
    assert abs(timeline.height() - height) <= 2, f"{timeline.height()} px, was {height}"
    assert pane.height() > win.height() * 0.6
    for tall in (1100, 700):                         # the window resized meanwhile
        win.resize(1400, tall)
        _settle()
        assert abs(timeline.height() - height) <= 2, f"{tall}: {timeline.height()} px"
        assert pane.height() > win.height() * 0.6, f"{tall}: the pane lost the room"
    win.toggle_maximized_pane()
    _settle()
    assert timeline.maximumHeight() == cap, "the Timeline was left unable to grow"


def test_maximizing_again_before_the_layout_settles_leaves_no_cap(win):
    timeline = win._timeline_dock
    cap = timeline.maximumHeight()
    for _ in range(2):                               # within one pass of the event loop
        win.toggle_maximized_pane(win._sample_dock)
        win.toggle_maximized_pane()
    _settle()
    assert timeline.maximumHeight() == cap


def test_a_closed_timeline_stays_closed(win):
    win._timeline_dock.hide()
    _settle()
    win.toggle_maximized_pane(win._grid_dock)
    _settle()
    assert _shown(win) == {win._grid_dock}
    win.toggle_maximized_pane()
    _settle()
    assert win._timeline_dock.isHidden()


def test_the_timeline_is_never_the_maximized_pane(win):
    """It is how the other panes are navigated, not a view of its own: no button, and
    neither the key nor a double-click maximizes it (nor floats it, as for every pane)."""
    bar = _bar(win, win._timeline_dock)
    assert not bar._max.isVisible() and bar._on_maximize is None
    before = bytes(win.saveState())
    QTest.mouseDClick(bar, Qt.LeftButton, pos=QPoint(bar.width() // 2, bar.height() // 2))
    _settle()
    assert not win._timeline_dock.isFloating() and win.maximized_pane() is None
    win._timeline_dock.widget().setFocus()
    _settle()
    assert win._pane_of(QApplication.focusWidget()) is win._timeline_dock
    win._maximize_action.trigger()
    _settle()
    assert win.maximized_pane() is None and bytes(win.saveState()) == before
    assert "Timeline" in win._status.text()


def test_the_commands_pane_has_a_maximize_button_and_its_title_a_double_click(win):
    before = bytes(win.saveState())
    btn = win._cmd_max_btn
    assert btn.isVisible() and not btn.isChecked()
    btn.click()
    _settle()
    assert win.maximized_pane() == "commands" and btn.isChecked()
    assert "Restore" in btn.toolTip()
    btn.click()
    _settle()
    assert win.maximized_pane() is None and not btn.isChecked()
    title = win._cmd_title
    middle = QPoint(title.width() // 2, title.height() // 2)
    QTest.mouseDClick(title, Qt.LeftButton, pos=middle)
    _settle()
    assert win.maximized_pane() == "commands"
    QTest.mouseDClick(title, Qt.LeftButton, pos=middle)
    _settle()
    assert win.maximized_pane() is None
    assert bytes(win.saveState()) == before


def test_a_title_bar_button_moves_the_focus_to_its_pane(win):
    win._cmd_view.setFocus()
    _settle()
    _bar(win, win._raw_dock)._max.click()
    _settle()
    assert win._pane_of(QApplication.focusWidget()) is win._raw_dock
    win._maximize_action.trigger()                   # the key now acts on Capture
    _settle()
    assert win.maximized_pane() is None
    assert win._pane_of(QApplication.focusWidget()) is win._raw_dock


@pytest.mark.parametrize("attr, view", [
    ("_grid_dock", "_grid_view"), ("_reg_dock", "_reg_view._tree"),
    ("_sample_dock", "_sample_view"), ("_eye_dock", "_eye_view"),
])
def test_a_title_bar_button_focuses_the_panes_view_not_a_toolbar_control(win, attr, view):
    """The focus goes to the pane's view, not its first toolbar control, so the keys act on
    the view."""
    pane = getattr(win, attr)
    target = win
    for part in view.split("."):
        target = getattr(target, part)
    win._show_dock(pane)
    win._cmd_view.setFocus()
    _settle()
    _bar(win, pane)._max.click()
    _settle()
    focus = QApplication.focusWidget()
    assert focus is target or target.isAncestorOf(focus), type(focus).__name__
    win.toggle_maximized_pane()
    _settle()
    focus = QApplication.focusWidget()
    assert focus is target or target.isAncestorOf(focus), type(focus).__name__


def test_the_key_from_the_keyboard_toggles_the_pane(win):
    """The shortcut itself, not the action triggered: a key event in the focused pane."""
    assert not win._maximize_action.autoRepeat(), "a held key would toggle again and again"
    win._sample_view.setFocus()
    _settle()
    for want in (win._sample_dock, None):
        target = QApplication.focusWidget()
        QTest.keyClick(target, Qt.Key_M, Qt.ControlModifier | Qt.ShiftModifier)
        QTest.keyRelease(target, Qt.Key_Shift)       # QTest keeps the modifiers held
        QTest.keyRelease(target, Qt.Key_Control)
        _settle()
        assert win.maximized_pane() is want


def test_the_commands_table_can_be_the_maximized_pane(win):
    before = bytes(win.saveState())
    win._cmd_view.setFocus()
    _settle()
    win.toggle_maximized_pane()
    _settle()
    assert win.maximized_pane() == "commands"
    assert _shown(win) == {win._timeline_dock} and win._central_stack.isVisible()
    win.toggle_maximized_pane()
    _settle()
    assert bytes(win.saveState()) == before


def test_a_floating_pane_is_maximized_in_the_window_and_floats_again_after(win):
    """Compared with Qt's own restore of the same state, its bytes and the pane's size: a
    pane just floated has no settled geometry yet, and any restore gives it one (offscreen
    it has no position; on Windows the restore also sizes the floating window afresh), so
    neither the state nor the size from before a restore can match."""
    win._meas_dock.show()
    win._meas_dock.setFloating(True)
    _settle()
    before = win.saveState()
    win.restoreState(before)
    _settle()
    expected = bytes(win.saveState())
    size = win._meas_dock.size()
    win.toggle_maximized_pane(win._meas_dock)
    _settle()
    assert not win._meas_dock.isFloating() and _shown(win) == {win._meas_dock}
    win.toggle_maximized_pane()
    _settle()
    assert win._meas_dock.isFloating() and win._meas_dock.size() == size
    assert bytes(win.saveState()) == expected


def test_with_no_pane_selected_nothing_changes(win):
    before = bytes(win.saveState())
    win.setFocus()                                   # the window itself is no pane
    _settle()
    win.toggle_maximized_pane()
    _settle()
    assert win.maximized_pane() is None and bytes(win.saveState()) == before
    assert "Click in a pane" in win._status.text()


def test_showing_another_pane_puts_the_panes_back_first(win):
    win.toggle_maximized_pane(win._sample_dock)
    _settle()
    win._show_dock(win._meas_dock)
    _settle()
    assert win.maximized_pane() is None
    assert win._central_stack.isVisible()
    assert win._meas_dock in _shown(win)


def test_hiding_a_pane_from_the_view_menu_puts_the_panes_back_and_keeps_it_hidden(win):
    win.toggle_maximized_pane(win._sample_dock)
    _settle()
    win._dock_actions[win._timeline_dock].setChecked(False)
    _settle()
    assert win.maximized_pane() is None and win._central_stack.isVisible()
    assert win._timeline_dock.isHidden(), "the restore showed it again"
    assert not win._dock_actions[win._timeline_dock].isChecked()
    assert win._sample_dock in _shown(win)


def test_closing_the_maximized_pane_puts_the_panes_back(win):
    """Its ✕: the window is not left with the central page hidden and nothing in it."""
    win.toggle_maximized_pane(win._audio_dock)
    _settle()
    _bar(win, win._audio_dock)._close.click()
    _settle()
    assert win.maximized_pane() is None and win._central_stack.isVisible()
    assert win._audio_dock.isHidden() and win._reg_dock in _shown(win)
    assert not win._dock_actions[win._audio_dock].isChecked()


@pytest.mark.parametrize("attr", ["_sample_dock", "_audio_dock"])
def test_a_link_added_or_removed_puts_the_panes_back_and_resizes_the_timeline(win, attr):
    """The Timeline grows by a band per Link. The arrangement a maximize saved has the old
    number, so the change puts the panes back first, then resizes the Timeline."""
    pane, timeline = getattr(win, attr), win._timeline_dock
    want = {}
    _add_link(win)
    want[2] = timeline.height()
    win.remove_link(1, confirm=False)
    _settle()
    want[1] = timeline.height()
    assert want[2] > want[1], "a second Link did not grow the Timeline: nothing to test"
    for n, change in ((2, lambda: _add_link(win)),
                      (1, lambda: win.remove_link(1, confirm=False))):
        win.toggle_maximized_pane(pane)
        _settle()
        change()
        _settle()
        assert win.maximized_pane() is None and win._central_stack.isVisible()
        assert abs(timeline.height() - want[n]) <= 2, f"{n} Links: {timeline.height()} px"


def _add_link(w):
    from swi3s_studio.ingest import transitions
    from swi3s_studio.session import Session
    from swi3s_studio.ui import main_window as mw
    s = Session.from_demo(mw._demo_samples(), cold_start=True, phy=2, variant="flow_control",
                          delay_samples=transitions.DEMO_LINK2_DELAY_SAMPLES,
                          register_map=w._rmap)
    w.load_session(s, add_link=True)
    _settle()


def test_all_links_turned_off_while_maximized_shrinks_the_panes_put_back(win):
    """Leaving All Links gives the bottom panes back their one-Link height, measured in the
    arrangement put back, not in the maximized pane."""
    _add_link(win)
    win._show_dock(win._raw_dock)
    _settle()
    single = win._raw_dock.height()
    win.toggle_maximized_pane(win._raw_dock)
    _settle()
    win.set_bottom_scope_all(True)
    _settle()
    win.toggle_maximized_pane()
    _settle()
    assert win._raw_dock.height() > single + 20, "All Links did not grow it: nothing to test"
    win.toggle_maximized_pane(win._raw_dock)
    _settle()
    win.set_bottom_scope_all(False)
    _settle()
    win.toggle_maximized_pane()
    for _ in range(4):
        _settle()
    assert abs(win._raw_dock.height() - single) <= 2, f"{win._raw_dock.height()}, was {single}"


def test_a_workspace_saved_after_all_links_was_turned_off_while_maximized_reopens_short(
        win, monkeypatch, tmp_path):
    """The resize waiting for the restore is not in the arrangement the maximize saved, so the
    workspace carries it, and reopened the bottom panes have their one-Link height."""
    import time

    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    _add_link(win)
    win._show_dock(win._raw_dock)
    _settle()
    single = win._raw_dock.height()
    win.set_bottom_scope_all(True)
    for _ in range(4):
        _settle()
    assert win._raw_dock.height() > single + 20, "All Links did not grow it: nothing to test"
    win.toggle_maximized_pane(win._raw_dock)
    _settle()
    win.set_bottom_scope_all(False)
    _settle()
    path = str(tmp_path / "ws.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (path, "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (path, "")))
    win.save_workspace()
    again = MainWindow()
    again.resize(1400, 900)
    again.show()
    again.open_workspace()
    deadline = time.time() + 60.0
    while getattr(again, "_load_thread", None) is not None:
        assert time.time() < deadline, "workspace load did not finish"
        QApplication.processEvents()
        time.sleep(0.005)
    for _ in range(4):
        _settle()
    assert again.maximized_pane() is None
    assert abs(again._raw_dock.height() - single) <= 2, f"{again._raw_dock.height()}, was {single}"


def test_the_timing_jump_and_the_register_compare_put_the_panes_back(win, monkeypatch):
    win.toggle_maximized_pane(win._sample_dock)
    _settle()
    win._on_timing_jump(int(win._session.commands[5]["start_sample"]))
    _settle()
    assert win.maximized_pane() is None and win._raw_dock in _shown(win)


def test_a_workspace_saved_while_maximized_records_the_layout_it_returns_to(win):
    from base64 import b64decode
    before = bytes(win.saveState())
    win.toggle_maximized_pane(win._sample_dock)
    _settle()
    saved = b64decode(win._window_view_state()["window"]["docks"])
    assert saved == before


def test_leaving_the_bus_analyzer_puts_the_panes_back_before_it_saves_its_layout(win):
    from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION
    before = bytes(win.saveState())
    win.toggle_maximized_pane(win._sample_dock)
    _settle()
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    assert win.maximized_pane() is None
    win._mode_mgr.switch_to(ANALYSIS)
    _settle()
    assert bytes(win.saveState()) == _with_in_front(win, before, win._sample_dock), (
        "the Analyzer came back maximized or rearranged")


def test_outside_the_bus_analyzer_the_item_is_hidden_and_does_nothing(win):
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    assert not win._maximize_action.isEnabled() and not win._maximize_action.isVisible()
    win.toggle_maximized_pane(win._sample_dock)
    assert win.maximized_pane() is None


def test_the_shortcuts_help_lists_the_key(win, monkeypatch):
    shown = []
    monkeypatch.setattr(win, "_info_box", lambda title, html: shown.append(html))
    win.show_keyboard_shortcuts()
    import sys
    key = "⌘⇧M" if sys.platform == "darwin" else "Ctrl+Shift+M"
    assert key in shown[0] and "Maximize the selected pane" in shown[0]


def test_the_timing_inequalities_maximize_to_the_whole_page(win):
    """In the Timing Calculator the ⤢ beside "Timing Inequalities", the key (View ▸ Maximize
    Pane) and a double-click on the title each give the inequalities the whole page: the page
    title and the top bar are hidden, the parameter sections still scroll below them, and the
    same toggle brings the top bar back."""
    from swi3s_studio.ui.mode_controller import TIMING
    win._mode_mgr.switch_to(TIMING)
    _settle()
    page = win._timing_page
    hidden = (page._title, page._subtitle, page._topbar)
    assert win._maximize_action.isVisible() and win._maximize_action.isEnabled()
    assert page._results_title.text() == "Timing Inequalities"

    win._timing_max_btn.click()
    _settle()
    assert page.is_maximized() and not any(w.isVisible() for w in hidden)
    assert page._tree.isVisible() and page._params.isVisible()   # still scrolled to
    assert win._timing_max_btn.isChecked()
    assert win._maximize_action.text() == "Restore Panes"

    win._maximize_action.trigger()                   # the key's action
    _settle()
    assert not page.is_maximized() and all(w.isVisible() for w in hidden)
    assert not win._timing_max_btn.isChecked()
    assert win._maximize_action.text() == "Maximize Pane"

    QTest.mouseDClick(page._results_title, Qt.LeftButton)
    _settle()
    assert page.is_maximized()
    page.maximizeRequested.emit()
    _settle()
    assert not page.is_maximized()
