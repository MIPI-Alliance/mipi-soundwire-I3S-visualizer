"""The menu bar: Mode first, and each mode shows only what applies to it.

The bar used to hold every menu in every mode (the Analyzer's disabled, some not even
that: Devices, Bookmarks and Links stayed live in the Visualizer), and File carried all
three modes' submenus. Now File holds the current mode's items, with ⌘O / ⌘S on its open
and save; the Analyzer's menus appear only in the Analyzer; and an item that acts on a
capture waits for one. Everything that re-decodes is under Decode.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections import Counter

import pytest
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMessageBox

from swi3s_studio.ui.mode_controller import ANALYSIS, TIMING, VISUALIZATION

_BAR = {
    VISUALIZATION: ["Mode", "File", "View", "Help"],
    TIMING: ["Mode", "File", "View", "Help"],
    ANALYSIS: ["Mode", "File", "Commands", "Decode", "Devices", "Audio", "Bookmarks",
               "View", "Help"],
}
_FILE = {
    VISUALIZATION: ["Open Visualizer CSV…", "Save Visualizer CSV…", "Export Grid Image…",
                    "Export Bus Model (JSON)…", "Quit"],
    TIMING: ["Open Timing Settings…", "Save Timing Settings…", "Quit"],
    ANALYSIS: ["Open Capture…", "Open Demo Capture",
               "Open Workspace…", "Save Workspace…", "Export Capture…",
               "Locate Sub-Capture…", "Export Visualizer CSV…", "Export Bus Grid Image…",
               "View Bus Grid in Visualizer", "Quit"],
}


def _text(a):
    """An item as shown: mnemonic markers dropped, an escaped "&&" kept as one "&"."""
    return a.text().replace("&&", "\0").replace("&", "").replace("\0", "&").split("\t")[0]


def _bar(win):
    return [_text(a) for a in win.menuBar().actions() if a.isVisible()]


def _items(menu):
    return [_text(a) for a in menu.actions() if not a.isSeparator() and a.isVisible()]


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    return MainWindow()


@pytest.mark.parametrize("mode", [VISUALIZATION, TIMING, ANALYSIS])
def test_each_mode_shows_only_its_menus_and_file_items(win, mode):
    win._mode_mgr.switch_to(mode)
    assert _bar(win) == _BAR[mode]
    assert _items(win._file_menu) == _FILE[mode]
    assert win._quit_action.menuRole() == QAction.MenuRole.QuitRole


def test_switching_modes_keeps_every_file_item(win):
    """QMenu.clear() deletes what it removes even when another menu owns it: refilling
    File that way destroyed the previous mode's items on the first switch."""
    for mode in (VISUALIZATION, ANALYSIS, TIMING, VISUALIZATION, ANALYSIS):
        win._mode_mgr.switch_to(mode)
        assert _items(win._file_menu) == _FILE[mode], mode


@pytest.mark.parametrize("mode", [VISUALIZATION, TIMING, ANALYSIS])
def test_open_and_save_keys_go_to_the_current_mode(win, mode):
    win._mode_mgr.switch_to(mode)
    op, sv = win._file_keys[mode]
    assert op.shortcut() == QKeySequence(QKeySequence.StandardKey.Open)
    assert sv.shortcut() == QKeySequence(QKeySequence.StandardKey.Save)
    for m, (o, s) in win._file_keys.items():
        if m != mode:
            assert o.shortcut().isEmpty() and s.shortcut().isEmpty()


@pytest.mark.parametrize("mode", [VISUALIZATION, TIMING, ANALYSIS])
def test_no_key_names_two_live_actions(win, mode):
    win._mode_mgr.switch_to(mode)
    keys = Counter()
    for act in win.findChildren(QAction):
        if not act.isEnabled():
            continue
        for seq in act.shortcuts():
            if not seq.isEmpty():
                keys[seq.toString()] += 1
    assert not [k for k, n in keys.items() if n > 1], keys


@pytest.mark.parametrize("mode", [VISUALIZATION, TIMING])
def test_the_analyzers_menus_and_keys_are_off_elsewhere(win, mode):
    win._mode_mgr.switch_to(mode)
    for menu in win._analysis_menus:
        assert not menu.menuAction().isVisible() and not menu.isEnabled()
    assert not win._legend_action.isVisible()
    # Preferences is the waveform colours and weights: the Analyzer's, ⌘, included.
    for act in (win._colors_action, win._prefs_action):
        assert not act.isVisible() and not act.isEnabled()
    assert not any(a.isVisible() for a in win._view_pane_actions)


def test_capture_items_wait_for_a_capture(win):
    assert win._session is None
    assert win._needs_capture and not any(a.isEnabled() for a in win._needs_capture)
    win.load_demo()
    assert all(a.isEnabled() for a in win._needs_capture)


def test_decode_holds_what_re_decodes_and_audio_is_playback(win):
    win._mode_mgr.switch_to(ANALYSIS)
    assert _items(win._decode_menu) == ["Import Visualizer CSV…",
                                        "Remove Region's Visualizer CSV", "Force Column Count…",
                                        "Hub Depths…", "Per-Dataport Scrambler…",
                                        "Manual SSP Move", "Block PDM DC Bias"]
    assert _items(win._audio_menu) == ["Playback Output Device", "Playback Bit Depth",
                                       "Playback Decimation", "High-Pass & Gain",
                                       "Export Audio as WAV…"]
    assert _items(win._devices_menu) == ["Device Names…", "Peripheral Register Maps…"]
    assert "Signal Panes: All Links" in _items(win._links_menu)


def test_help_has_the_guide_and_about(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    help_ = win._help_menu
    assert _items(help_)[0] == "SWI3S Studio User Guide"
    about = next(a for a in help_.actions() if _text(a) == "About SWI3S Studio")
    assert about.menuRole() == QAction.MenuRole.AboutRole
    shown = []
    monkeypatch.setattr(QMessageBox, "about", lambda *a: shown.append(a[2]))
    about.trigger()
    from swi3s_studio import __version__
    assert shown and __version__ in shown[0]
    win.show_user_guide()
    assert win._guide.isVisible() and "SWI3S Studio" in win._guide.toPlainText()
    win._guide.close()


# ---- the Links menu's items, where they went -------------------------------------------

def _all_menu_texts(win):
    out = []

    def walk(menu):
        for a in menu.actions():
            out.append(_text(a))
            if a.menu() is not None:
                walk(a.menu())
    for menu in (win._file_menu, win._decode_menu, win._devices_menu, win._audio_menu,
                 win._view_menu, win._help_menu, win._links_menu):
        walk(menu)
    return out


def test_there_is_no_links_menu_and_no_add_link_item(win):
    for mode in (VISUALIZATION, TIMING, ANALYSIS):
        win._mode_mgr.switch_to(mode)
        assert "Links" not in _bar(win)
    assert not any(t.startswith("Add Link") for t in _all_menu_texts(win))


def test_view_links_needs_two_links_and_keeps_the_shortcuts():
    from test_links_gui import _win, _with_added_link
    one = _win()
    one._mode_mgr.switch_to(ANALYSIS)
    assert not one._view_links_action.isVisible()
    win, _first, _second = _with_added_link()
    win._mode_mgr.switch_to(ANALYSIS)
    assert win._view_links_action.isVisible()
    keys = {_text(a): a.shortcut().toString() for a in win._links_menu.actions()
            if not a.isSeparator()}
    assert keys["Commands: All Links"] == QKeySequence("Ctrl+Alt+0").toString()
    assert keys["Signal Panes: All Links"] == QKeySequence("Ctrl+Alt+Shift+0").toString()
    assert keys["Link 1"] == QKeySequence("Ctrl+Alt+1").toString()
    win._mode_mgr.switch_to(VISUALIZATION)
    assert not win._view_links_action.isVisible()


def test_align_is_a_bookmarks_item_for_two_links():
    from test_links_gui import _win, _with_added_link
    one = _win()
    assert not one._align_links_action.isEnabled()
    win, _first, _second = _with_added_link()
    assert win._align_links_action in win._bookmarks_menu.actions()
    assert win._align_links_action.isEnabled()


@pytest.mark.parametrize("item", ["Rename Link…", "Set Offset…", "Remove Link…",
                                  "Show This Link Everywhere"])
def test_a_links_label_menu_acts_on_that_link(monkeypatch, item):
    from PySide6.QtWidgets import QMenu
    from test_links_gui import _with_added_link
    win, first, second = _with_added_link()
    win._activate_link(1)                                # the other Link is the active one
    ran = []
    monkeypatch.setattr(win, "rename_link", lambda: ran.append(("rename", win.links.active_index)))
    monkeypatch.setattr(win, "set_link_offset_dialog",
                        lambda: ran.append(("offset", win.links.active_index)))
    monkeypatch.setattr(win, "remove_link", lambda i=None: ran.append(("remove", i)))
    monkeypatch.setattr(win, "switch_link", lambda i: ran.append(("show", i)))
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: next(
        a for a in m.actions() if _text(a) == item))
    win._link_label_menu(win._ribbons[0], None)          # right-click Link 1's label
    want = {"Rename Link…": ("rename", 0), "Set Offset…": ("offset", 0),
            "Remove Link…": ("remove", 0), "Show This Link Everywhere": ("show", 0)}[item]
    assert ran == [want]


# ---- keys, the guide window, Export Audio's Link ----------------------------------

def test_quit_and_preferences_have_the_keys_the_shortcuts_help_lists(win):
    w = win
    assert QKeySequence("Ctrl+Q") in w._quit_action.shortcuts()
    assert QKeySequence("Ctrl+,") in w._prefs_action.shortcuts()     # Settings key alone off mac


def test_the_shortcuts_help_spells_modifiers_for_the_platform(win, monkeypatch):
    import sys
    w = win
    w._mode_mgr.switch_to(ANALYSIS)                       # where the Links keys are listed
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()) or 0)
    for platform, want, never in (("darwin", "⌘⌥⇧0", "Ctrl+"), ("win32", "Ctrl+Alt+Shift+0", "⌥")):
        monkeypatch.setattr(sys, "platform", platform)
        w.show_keyboard_shortcuts()
        assert want in shown[-1] and never not in shown[-1]


def test_the_user_guide_closes_with_the_window(win):
    w = win
    w.show_user_guide()
    guide = w._guide
    assert guide.isVisible() and guide.parent() is w and guide.isWindow()
    w.close()
    assert not guide.isVisible()


def test_export_audio_follows_the_link_it_would_export(monkeypatch):
    # In All Links, Export Audio exports the active Link; binding lane by lane must not
    # leave it enabled for whichever lane was bound last.
    from test_links_gui import _with_added_link
    win, _first, _second = _with_added_link()
    win._export_action.setEnabled(False)                  # stale, as a lane bind left it
    win.set_bottom_scope_all(True)
    assert win._export_action.isEnabled()                 # set for the active Link on entry

    class _Silent:
        def is_empty(self):
            return True
    monkeypatch.setattr(win.links[0].view, "audio_store", _Silent())
    win._activate_link(1)
    assert win._export_action.isEnabled()
    win._activate_link(0)
    assert not win._export_action.isEnabled()
    win._bind_lane_of(1)                                  # the last lane bound has audio
    assert not win._export_action.isEnabled()


# ---- Commands ▸ Filter ▸ Commands: quick picks on ⌘4–⌘7 -----------------------------------

def _kinds_shown(w):
    proxy = w._cmd_proxy
    return {proxy.sourceModel().command_at(proxy.mapToSource(proxy.index(r, 0)).row())
            .get("command") for r in range(proxy.rowCount())}


def _press(w, key):
    from PySide6.QtTest import QTest
    w.show()
    QApplication.processEvents()
    from PySide6.QtCore import Qt
    seq = QKeySequence(key)
    QTest.keySequence(w, seq)
    # QTest leaves Ctrl "held" after a sequence (its modifier state outlives the window),
    # and a later test's row click then reads as a Ctrl-click: let go of it.
    QTest.keyRelease(w, Qt.Key_Control)
    QApplication.processEvents()


@pytest.fixture
def analyzer(win):
    win.load_demo()                       # the Analyzer opens empty: these need commands
    win._mode_mgr.switch_to(ANALYSIS)
    win._clear_all_filters()
    return win


def test_the_quick_command_filters_have_their_keys(analyzer):
    keys = {_text(a): a.shortcut().toString() for a in analyzer._kind_menu.actions()
            if not a.isSeparator() and not a.isCheckable()}
    assert keys == {"All": "Ctrl+4", "None": "Ctrl+5", "All excl. Ping": "Ctrl+6",
                    "Commit": "Ctrl+7"}


def test_none_then_all_by_key(analyzer):
    every = _kinds_shown(analyzer)
    assert {"Ping", "SSCR", "WriteA32"} <= every
    _press(analyzer, "Ctrl+5")
    assert analyzer._cmd_proxy.rowCount() == 0 and "Cmd: none" in analyzer._cmd_title.text()
    _press(analyzer, "Ctrl+4")
    assert _kinds_shown(analyzer) == every


def test_all_but_ping_and_commit_by_key(analyzer):
    every = _kinds_shown(analyzer)
    _press(analyzer, "Ctrl+6")
    assert _kinds_shown(analyzer) == every - {"Ping"}
    _press(analyzer, "Ctrl+7")
    # the demo's sync-point commits and the Commit Point rows where they take effect
    assert _kinds_shown(analyzer) == {"SSCR", "Commit Point"}


def test_ticking_a_kind_after_none_shows_that_kind(analyzer):
    analyzer._kind_menu._select_none()
    ping = next(a for a in analyzer._kind_menu._actions if a.data() == "Ping")
    ping.setChecked(True)
    assert _kinds_shown(analyzer) == {"Ping"}
    saved = analyzer._filter_snapshot()                    # as switching Links keeps it
    analyzer._clear_all_filters()
    analyzer._restore_filters(saved)
    assert _kinds_shown(analyzer) == {"Ping"}


def test_none_is_kept_per_link_when_switching(analyzer):
    snap = analyzer._filter_snapshot
    analyzer._kind_menu._select_none()
    saved = snap()
    analyzer._clear_all_filters()
    analyzer._restore_filters(saved)
    assert analyzer._cmd_proxy.rowCount() == 0 and analyzer._kind_menu._none


def test_the_filter_keys_do_nothing_outside_the_analyzer(win):
    win._mode_mgr.switch_to(VISUALIZATION)
    before = win._cmd_proxy.rowCount()
    _press(win, "Ctrl+5")
    assert win._cmd_proxy.rowCount() == before


@pytest.mark.parametrize("platform, items", [
    ("darwin", ["Waveform Colors and Line Weight…", "Preferences…"]),
    ("win32", ["Waveform Colors and Line Weight…"]),
    ("linux", ["Waveform Colors and Line Weight…"]),
])
def test_view_offers_preferences_once_where_it_stays_in_view(monkeypatch, platform, items):
    import sys
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr(sys, "platform", platform)
    w = MainWindow()
    w._mode_mgr.switch_to(ANALYSIS)
    from PySide6.QtWidgets import QMenu
    # (by title: QAction.menu() answers a deleted wrapper on PySide 6.11)
    view = next(m for m in w.findChildren(QMenu) if m.title() == "&View")
    shown = [_text(a) for a in view.actions() if a.text().replace("&", "") in
             ("Waveform Colors and Line Weight…", "Preferences…")]
    assert shown == items
    assert QKeySequence("Ctrl+,") in w._prefs_action.shortcuts()


def test_the_shortcuts_help_lists_the_analyzers_keys_only_in_the_analyzer(win, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self.text()) or 0)
    win._mode_mgr.switch_to(VISUALIZATION)
    win.show_keyboard_shortcuts()
    elsewhere = shown[-1]
    assert "Modes" in elsewhere and "Cursor navigation" not in elsewhere
    assert "listed in the Bus Analyzer" in elsewhere
    assert "Preferences" not in elsewhere                 # ⌘, is the Analyzer's (it is off here)
    win._mode_mgr.switch_to(ANALYSIS)
    win.show_keyboard_shortcuts()
    here = shown[-1]
    for title in ("Links", "Command filter", "Cursor navigation", "Waveform panes",
                  "Manual Stream Sync Point", "Waveform settings"):
        assert f"<b>{title}" in here, title               # every Analyzer group, by name
    assert "listed in the Bus Analyzer" not in here


def test_the_status_bar_shows_what_the_window_just_did(win):
    # Every self._status.setText(...) used to go to a hidden label.
    win.load_demo()
    win.show()
    win._kind_menu._fill([("Ping", "Ping")])              # a capture with no commit
    commit = next(a for a in win._kind_menu.actions() if _text(a) == "Commit")
    commit.trigger()
    QApplication.processEvents()
    bar = win.statusBar()
    assert bar.isVisible() and win._status.isVisible() and bar.isAncestorOf(win._status)
    assert win._status.text() == "No commit in this capture"
