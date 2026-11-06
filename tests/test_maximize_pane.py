"""View ▸ Maximize Pane: the selected pane alone in the window, then every pane back.

The selected pane is the one holding the keyboard focus (a click in any pane gives it), or
the pane whose title-bar button or title was used. Maximizing saves the whole arrangement
(sizes, tab groups, the tab in front, floating panes) and restoring puts it back exactly,
compared here as the window's saved state, except that the maximized pane stays the tab in
front of its group and keeps the focus, so the key pressed again acts on the same pane.
Anything that shows, hides or closes a pane, leaves the Bus Analyzer or opens a workspace
puts the panes back first. The maximized pane cannot be dragged out, and making it
floating puts the panes back; a resize the layout asks for meanwhile is made to the arrangement
put back; and a workspace saved while one is maximized records the arrangement it returns to.
In the Timing Calculator the inequalities, or the term diagram, take the page and the key
restores them; in the Bus Visualizer the grid does, by its top-right button, the key, Maximize
Frame or a splitter drag. Both stay maximized across a mode switch.
"""
import contextlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

pytestmark = [pytest.mark.gui, pytest.mark.platform]


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


def test_the_maximized_pane_cannot_be_dragged_out_and_floated_it_puts_the_panes_back(win):
    from PySide6.QtWidgets import QDockWidget
    dock = win._grid_dock
    floatable = bool(dock.features() & QDockWidget.DockWidgetFloatable)
    assert floatable, "the Bus Grid pane is floatable before it is maximized"
    before = bytes(win.saveState())
    win.toggle_maximized_pane(dock)
    _settle()
    assert not dock.features() & QDockWidget.DockWidgetFloatable
    dock.setFloating(True)
    _settle()
    assert win.maximized_pane() is None and win._central_stack.isVisible()
    assert win._maximize_action.text() == "Maximize Pane"
    assert not dock.isFloating()
    assert bool(dock.features() & QDockWidget.DockWidgetFloatable) == floatable
    assert bytes(win.saveState()) == _with_in_front(win, before, dock)


def test_the_restore_after_a_floated_maximized_pane_belongs_to_the_pane(win, monkeypatch):
    """The deferred restore is a timer owned by the pane, so it never fires on a window
    that has been destroyed."""
    import swi3s_studio.ui.main_window as mw
    owners = []
    monkeypatch.setattr(mw.timers, "after", lambda ms, owner, fn: owners.append(owner))
    dock = win._grid_dock
    win.toggle_maximized_pane(dock)
    _settle()
    owners.clear()
    win._on_dock_floated(dock, True)
    assert dock in owners


def test_the_dock_layout_timers_belong_to_the_window_and_the_pane(win, monkeypatch):
    """The deferred tab sync is owned by the window and the deferred pane refresh by the
    pane, so neither fires on a deleted widget."""
    import swi3s_studio.ui.main_window as mw
    owners = []
    monkeypatch.setattr(mw.timers, "after", lambda ms, owner, fn: owners.append(owner))
    win._pane_tabs_pending = False
    win._schedule_pane_tabs()
    assert owners == [win]
    owners.clear()
    win._on_dock_floated(win._grid_dock, False)
    assert owners == [win._grid_dock]


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


def _popup_action(w, label):
    """The item `label` of the menu a right-click on the menu bar or a title bar opens."""
    menu = w.createPopupMenu()
    assert menu is not None
    try:
        return next(a for a in menu.actions() if a.text() == label)
    finally:
        menu.deleteLater()


def test_the_title_bar_menu_hides_the_maximized_pane_through_the_view_menu(win):
    """Unchecking the maximized pane in the right-click menu puts the panes back and
    leaves it hidden; the window is never left with nothing in it."""
    win.toggle_maximized_pane(win._reg_dock)
    _settle()
    act = _popup_action(win, "Registers")
    assert act is win._dock_actions[win._reg_dock] and act.isChecked()
    act.setChecked(False)
    _settle()
    assert win.maximized_pane() is None and win._central_stack.isVisible()
    assert win._reg_dock.isHidden() and win._grid_dock in _shown(win)
    assert win._maximize_action.text() == "Maximize Pane"


def test_the_title_bar_menu_shows_a_hidden_pane_after_putting_the_panes_back(win):
    win._dock_actions[win._grid_dock].setChecked(False)
    _settle()
    win.toggle_maximized_pane(win._eye_dock)
    _settle()
    _popup_action(win, "Bus Grid").setChecked(True)
    _settle()
    assert win.maximized_pane() is None and win._central_stack.isVisible()
    assert win._grid_dock in _shown(win)


def test_the_title_bar_menu_lists_only_the_view_menus_pane_items(win):
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    menu = win.createPopupMenu()
    try:
        assert set(menu.actions()) == set(win._dock_actions.values())
        assert not {d.toggleViewAction() for d in win._dock_area} & set(menu.actions())
    finally:
        menu.deleteLater()
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    assert win.createPopupMenu() is None


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


def test_outside_the_bus_analyzer_no_analyzer_pane_is_maximized(win):
    """In the Bus Visualizer the item maximizes the grid (see below); an Analyzer pane asked
    for there is not maximized."""
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    assert win._maximize_action.isEnabled() and win._maximize_action.isVisible()
    win.toggle_maximized_pane(win._sample_dock)
    assert win.maximized_pane() is None and win._viz_split.sizes()[0] > 0


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


def test_the_key_puts_a_maximized_term_diagram_back(win):
    """While the term diagram has the Timing page, View ▸ Maximize Pane reads Restore Panes
    and puts the diagram back, leaving the inequalities as they were; the diagram's own ⤢
    keeps the menu in step."""
    from swi3s_studio.ui.mode_controller import TIMING
    win._mode_mgr.switch_to(TIMING)
    _settle()
    page = win._timing_page
    page._hl = {"Man_t_DD"}
    page._draw_term("Man_t_DD", "MP_setup")
    page._diagram_max_btn.click()
    _settle()
    assert page.is_diagram_maximized() and not page._topbar.isVisible()
    assert win._maximize_action.text() == "Restore Panes"

    win._maximize_action.trigger()                   # the key's action
    _settle()
    assert not page.is_diagram_maximized() and not page.is_maximized()
    assert page._ineq_scroll.isVisible() and page._topbar.isVisible()
    assert not page._diagram_max_btn.isChecked()
    assert win._maximize_action.text() == "Maximize Pane"

    win._maximize_action.trigger()                   # the inequalities maximized
    _settle()
    assert page.is_maximized() and not page._topbar.isVisible()
    page._diagram_max_btn.click()
    _settle()
    assert page.is_diagram_maximized() and win._maximize_action.text() == "Restore Panes"
    page._diagram.doubleClicked.emit()               # the diagram back by its own control
    _settle()
    assert page.is_maximized() and not page._topbar.isVisible()
    assert win._maximize_action.text() == "Restore Panes" and win._timing_max_btn.isChecked()
    win._maximize_action.trigger()
    _settle()
    assert not page.is_maximized() and page._topbar.isVisible()


def test_the_visualizer_grid_has_a_maximize_button_at_its_top_right(win):
    """In the Bus Visualizer the grid's ⤢, pinned to its top right, the key (View ▸ Maximize
    Pane) and the authoring panel's Maximize Frame each give the grid the whole page, the
    parameters collapsed, and the same toggle puts them back; the ⤢ reads Restore while the
    grid has the page."""
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    btn, grid = win._viz_max_btn, win._viz_grid
    assert btn.isVisible() and btn.parent() is grid and btn.text() == "⤢"
    vp = grid.viewport().geometry()
    assert vp.contains(btn.geometry()) and btn.geometry().right() > vp.right() - 2 * btn.width()
    assert btn.geometry().top() < vp.top() + btn.height()
    assert win._maximize_action.isVisible() and win._maximize_action.isEnabled()

    btn.click()
    _settle()
    assert win._viz_split.sizes()[0] == 0 and btn.isChecked() and btn.isVisible()
    assert "Restore" in btn.toolTip() and win._maximize_action.text() == "Restore Panes"
    assert grid.viewport().geometry().contains(btn.geometry())

    win._maximize_action.trigger()                   # the key's action
    _settle()
    assert win._viz_split.sizes()[0] > 0 and not btn.isChecked()
    assert win._maximize_action.text() == "Maximize Pane"

    win._authoring.maximizeRequested.emit()          # Maximize Frame
    _settle()
    assert win._viz_split.sizes()[0] == 0 and btn.isChecked()
    btn.click()
    _settle()
    assert win._viz_split.sizes()[0] > 0 and not btn.isChecked()


def _viz_agrees(win, maximized):
    """The split, the grid's ⤢ (checked and tooltip), View ▸ Maximize Pane and the DP key all
    say the same thing about the Visualizer's grid."""
    btn = win._viz_max_btn
    assert (win._viz_split.sizes()[0] == 0) is maximized
    assert btn.isChecked() is maximized
    assert ("Restore" in btn.toolTip()) is maximized
    assert win._maximize_action.text() == ("Restore Panes" if maximized else "Maximize Pane")
    assert win._viz_grid.show_key is maximized


def test_the_visualizer_grid_stays_maximized_across_a_mode_switch(win):
    """A maximized grid is still maximized on coming back to the Bus Visualizer, every
    indicator with it, and the next toggle restores the parameters."""
    from swi3s_studio.ui.mode_controller import TIMING, VISUALIZATION
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    win._viz_max_btn.click()
    _settle()
    _viz_agrees(win, True)
    win._mode_mgr.switch_to(TIMING)
    _settle()
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    _viz_agrees(win, True)
    win._maximize_action.trigger()
    _settle()
    _viz_agrees(win, False)


def test_the_timing_inequalities_stay_maximized_across_a_mode_switch(win):
    """Maximized Timing inequalities are still maximized on coming back to the Timing
    Calculator, and the next toggle restores the top bar."""
    from swi3s_studio.ui.mode_controller import ANALYSIS, TIMING
    win._mode_mgr.switch_to(TIMING)
    _settle()
    page = win._timing_page
    win._maximize_action.trigger()
    _settle()
    assert page.is_maximized() and not page._topbar.isVisible()
    win._mode_mgr.switch_to(ANALYSIS)
    _settle()
    win._mode_mgr.switch_to(TIMING)
    _settle()
    assert page.is_maximized() and not page._topbar.isVisible()
    assert win._maximize_action.text() == "Restore Panes"
    win._maximize_action.trigger()
    _settle()
    assert not page.is_maximized() and page._topbar.isVisible()


def test_dragging_the_visualizer_splitter_shut_maximizes_the_grid(win):
    """Collapsing the parameters by dragging the splitter is a maximize: the ⤢, the View item
    and the DP key follow it, and the next toggle restores the parameters."""
    from swi3s_studio.ui.mode_controller import VISUALIZATION
    win._mode_mgr.switch_to(VISUALIZATION)
    _settle()
    split = win._viz_split
    split.moveSplitter(0, 1)                         # the handle dragged to the top
    _settle()
    _viz_agrees(win, True)
    win._viz_max_btn.click()
    _settle()
    _viz_agrees(win, False)
    assert split.sizes()[0] > 1
    split.moveSplitter(0, 1)
    _settle()
    split.moveSplitter(split.height() // 2, 1)       # and dragged open again
    _settle()
    _viz_agrees(win, False)


# ---- the shared cursor stays where it is ----

@contextlib.contextmanager
def _qt_messages():
    """The messages Qt prints meanwhile, less the offscreen platform's notices."""
    from PySide6.QtCore import qInstallMessageHandler
    seen: list = []
    previous = qInstallMessageHandler(
        lambda _kind, _ctx, msg: seen.append(msg)
        if "This plugin does not support" not in msg else None)
    try:
        yield seen
    finally:
        qInstallMessageHandler(previous)


def test_maximizing_and_restoring_any_pane_leaves_the_cursor(win):
    """restoreState re-shows the panes and Qt passes the focus through them; a table that
    gains the focus with no current row takes its first one, and that is not a selection."""
    cur = 2_000_000
    win.cursor.set_sample(cur)
    _settle()
    panes = ["commands"] + [d for d in win._dock_area if d is not win._timeline_dock]
    with _qt_messages() as seen:
        for pane in panes:
            win.toggle_maximized_pane(pane)
            _settle()
            assert win.maximized_pane() is not None, _name(pane)
            win.restore_panes()
            _settle()
            assert win.cursor.sample == cur, f"restoring after {_name(pane)} moved the cursor"
        win._symbol_dock.setFloating(True)
        _settle()
        assert win.cursor.sample == cur, "floating the CDS pane moved the cursor"
        win._symbol_dock.setFloating(False)
        _settle()
        assert win.cursor.sample == cur, "docking the CDS pane moved the cursor"
    assert seen == [], seen


def test_removing_a_link_while_a_pane_is_maximized_leaves_the_cursor():
    """Removing a Link puts the panes back first (its band count changes); the restore
    must not move the cursor, nor reach the Commands table of the Link being removed."""
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    win.load_demo_links()                         # Link 2 active
    _settle()
    assert win.links.active_index == 1
    cur = 1_500_000
    win.cursor.set_sample(cur)
    _settle()
    with _qt_messages() as seen:
        win.toggle_maximized_pane(win._audio_dock)
        _settle()
        win.remove_link(1, confirm=False)
        _settle()
    assert win.maximized_pane() is None
    assert win.cursor.sample == cur, "the removal moved the cursor"
    assert seen == [], seen


def test_a_focus_in_on_the_commands_table_leaves_the_shared_cursor(win):
    """Qt makes the first row current when the table gains the focus with none; the
    window does not move the cursor to that command."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent
    view = win._cmd_view
    assert win._cmd_proxy.rowCount() > 2
    target = win.cursor.sample + 12345
    win.cursor.set_sample(target)
    _settle()
    win._cmd_model.beginResetModel()             # a reset leaves the view no current row
    win._cmd_model.endResetModel()
    assert not view.currentIndex().isValid() and win.cursor.sample == target
    QApplication.sendEvent(view, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    _settle()
    assert view.currentIndex().isValid(), "Qt did not pick a current row: nothing was tested"
    assert win.cursor.sample == target


def test_a_focus_in_on_a_table_with_no_current_row_selects_nothing():
    """The rule at its source: Qt makes the first row current on a focus-in, and neither
    the CDS table nor the Commands table reports it as a selection."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent, QStandardItemModel

    from swi3s_studio.ui.command_table import CommandTableView
    from swi3s_studio.ui.symbol_view import SymbolView
    QApplication.instance() or QApplication([])
    sym = SymbolView()
    sym.set_symbols([{"kind": 3, "raw": 0x2AA, "value": 1, "start_sample": 100 * k,
                      "row": k} for k in range(1, 4)])
    seen: list = []
    sym.sampleSelected.connect(seen.append)
    QApplication.sendEvent(sym, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    assert sym.currentRow() == 0, "Qt did not pick a current row: nothing was tested"
    assert seen == [] and not sym.focus_changing
    sym.setCurrentCell(2, 0)                     # an explicit select still reports
    assert seen == [300]

    view = CommandTableView()
    model = QStandardItemModel(3, 2)
    view.setModel(model)
    during: list = []
    view.selectionModel().currentChanged.connect(lambda *_: during.append(view.focus_changing))
    QApplication.sendEvent(view, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    assert during == [True] and not view.focus_changing
