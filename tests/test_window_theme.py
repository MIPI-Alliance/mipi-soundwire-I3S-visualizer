"""The window's appearance, through the window: View ▸ Appearance's three exclusive items
and the one checked at start, what a switch re-applies (the palette, the saved preference,
the chrome's stylesheets, the dock title bars, every colour-caching view and both grids),
an OS scheme change followed only under Follow System, and Preferences re-styling only on
OK. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def _win(pref: str = "dark", load: bool = False):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.theme import apply_palette, save_preference
    save_preference(pref)
    apply_palette("light" if pref == "light" else "dark")
    win = MainWindow()
    if load:
        win.load_demo()
    return win


def _appearance(win) -> list:
    return list(win._appearance_group.actions())


@pytest.mark.parametrize("pref", ["system", "light", "dark"])
def test_view_appearance_offers_three_exclusive_items_with_the_saved_one_checked(pref):
    win = _win(pref)
    acts = _appearance(win)
    assert [a.text() for a in acts] == ["Follow &System", "&Light", "&Dark"]
    assert win._appearance_group.isExclusive() and all(a.isCheckable() for a in acts)
    assert [a.isChecked() for a in acts] == [p == pref for p in ("system", "light", "dark")]
    assert win._appearance_pref == pref
    win.close()


def test_a_switch_saves_the_choice_and_re_applies_the_chrome():
    from swi3s_studio.ui.theme import (
        VizTheme,
        analyzer_stylesheet,
        chrome_stylesheet,
        saved_preference,
    )
    win = _win("dark")
    _appearance(win)[1].trigger()                           # Light
    assert VizTheme.MODE == "light" and saved_preference() == "light"
    assert win._appearance_pref == "light"
    assert win.styleSheet() == chrome_stylesheet()
    assert win._analysis_page.styleSheet() == analyzer_stylesheet()
    assert win._grid_pane.styleSheet() == analyzer_stylesheet()
    assert VizTheme.FRAME_BG in win._central_stack.styleSheet()
    assert VizTheme.FRAME_BG in win._viz_split.styleSheet()
    assert VizTheme.FRAME_BG in win._cmd_title.styleSheet()
    _appearance(win)[2].trigger()                           # Dark
    assert VizTheme.MODE == "dark" and saved_preference() == "dark"
    assert win.styleSheet() == chrome_stylesheet()
    assert VizTheme.FRAME_BG in win._central_stack.styleSheet()
    win.close()


def test_a_switch_rethemes_every_colour_caching_view_and_redraws_both_grids(monkeypatch):
    win = _win("dark", load=True)
    calls: list = []
    for name in ("_authoring", "_viz_grid", "_grid_view", "_reg_view", "_cmd_view",
                 "_meas_view", "_timing_page", "_pair_view"):
        view = getattr(win, name)
        real = view.retheme
        monkeypatch.setattr(view, "retheme", lambda r=real, n=name: calls.append(n) or r())
    bars = []
    for bar in win._dock_titlebars:
        real = bar.retheme
        monkeypatch.setattr(bar, "retheme", lambda r=real, b=bar: bars.append(b) or r())
    drawn: list = []
    real_model = win._viz_grid.set_bus_model
    monkeypatch.setattr(win._viz_grid, "set_bus_model",
                        lambda *a, **k: drawn.append("authored") or real_model(*a, **k))
    real_grid = win._refresh_grid
    monkeypatch.setattr(win, "_refresh_grid", lambda: drawn.append("decoded") or real_grid())
    keep = win.cursor.sample
    win.apply_theme("light")
    assert sorted(calls) == sorted(["_authoring", "_viz_grid", "_grid_view", "_reg_view",
                                    "_cmd_view", "_meas_view", "_timing_page", "_pair_view"])
    assert len(bars) == len(win._dock_titlebars) > 0
    assert drawn == ["authored", "decoded"]
    assert win.cursor.sample == keep and win._session is not None
    win.close()


@pytest.mark.parametrize("pref, follows", [("system", True), ("light", False),
                                           ("dark", False)])
def test_an_os_scheme_change_is_followed_only_under_follow_system(monkeypatch, pref, follows):
    win = _win(pref)
    seen: list = []
    real = win._timing_page.retheme
    monkeypatch.setattr(win._timing_page, "retheme", lambda: seen.append(1) or real())
    hints = QApplication.instance().styleHints()
    hints.colorSchemeChanged.emit(hints.colorScheme())
    assert bool(seen) == follows
    assert win._appearance_pref == pref
    win.close()


@pytest.mark.parametrize("accepted", [True, False])
def test_preferences_re_styles_the_window_only_on_ok(monkeypatch, accepted):
    from swi3s_studio.ui import line_style
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    win = _win("dark")
    stored: list = []
    monkeypatch.setattr(line_style, "set_preferences", stored.append)
    monkeypatch.setattr(PreferencesDialog, "exec", lambda dlg: 1 if accepted else 0)
    seen: list = []
    real = win._timing_page.retheme
    monkeypatch.setattr(win._timing_page, "retheme", lambda: seen.append(1) or real())
    titles: list = []
    real_init = PreferencesDialog.__init__

    def init(dlg, prefs, parent=None, mode=None):
        titles.append((parent is win, mode))
        real_init(dlg, prefs, parent, mode=mode)

    monkeypatch.setattr(PreferencesDialog, "__init__", init)
    win.show_preferences()
    assert titles == [(True, "dark")]
    assert len(stored) == (1 if accepted else 0) and bool(seen) == accepted
    win.close()
