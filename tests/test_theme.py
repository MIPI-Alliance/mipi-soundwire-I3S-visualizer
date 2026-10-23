"""Theme switcher tests (Qt offscreen): the swappable dark/light palette, the
preference resolve + persistence, and a live MainWindow theme switch that
re-themes the views without losing the loaded capture.

Run: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_theme.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# conftest keeps every QSettings of the run in a throwaway ini, so save_preference() (here
# and inside MainWindow.apply_theme) cannot touch the user's real appearance preference.
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.theme import (
    _DARK,
    _LIGHT,
    PREF_KEY,
    VizTheme,
    apply_palette,
    resolve_mode,
    save_preference,
    saved_preference,
)


def test_apply_palette():
    """apply_palette copies a whole palette onto VizTheme + sets the derived keys."""
    apply_palette("dark")
    assert VizTheme.MODE == "dark"
    assert VizTheme.FRAME_BG == _DARK["FRAME_BG"]
    # Derived: grid canvas = frame bg; comma = gold SSP.
    assert VizTheme.GRID_BG == VizTheme.FRAME_BG
    assert VizTheme.SYM_COMMA == VizTheme.SEM_SSP

    apply_palette("light")
    assert VizTheme.MODE == "light"
    assert VizTheme.FRAME_BG == _LIGHT["FRAME_BG"] != _DARK["FRAME_BG"]
    assert VizTheme.PLOT_BG == _LIGHT["PLOT_BG"]
    # Every colour key is defined in BOTH palettes (a missing key would leave a
    # stale value from the other theme).
    assert set(_DARK) == set(_LIGHT)

    # Unknown mode falls back to dark (never leaves the palette half-applied).
    apply_palette("nonsense")
    assert VizTheme.MODE == "dark"
    print("ok: apply_palette dark/light + derived keys + fallback")


def test_resolve_mode():
    assert resolve_mode("dark") == "dark"
    assert resolve_mode("light") == "light"
    # 'system' resolves to a concrete mode (depends on the offscreen platform's
    # reported scheme; must be one of the two, never 'system').
    assert resolve_mode("system") in ("dark", "light")
    print("ok: resolve_mode maps system -> concrete dark/light")


def test_preference_roundtrip():
    """save_preference / saved_preference round-trip through QSettings (isolated to a
    temp store — see the module header)."""
    QApplication.setOrganizationName("MIPI SWI3S")
    QApplication.setApplicationName("SWI3S Studio")
    for pref in ("light", "dark", "system"):
        save_preference(pref)
        assert saved_preference() == pref
    # An invalid stored value falls back to the default ('system').
    QSettings().setValue(PREF_KEY, "bogus")
    assert saved_preference() == "system"
    print("ok: preference save/load round-trip (+ invalid -> system)")


def test_live_switch_rethemes_window():
    """A live MainWindow theme switch re-applies the palette, re-themes the bus-grid
    canvas, and survives with a loaded capture (cursor/data intact)."""
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("dark")
    win = MainWindow()
    win.load_demo()
    win.cursor.set_sample(win._session.commands[2]["start_sample"])
    keep = win.cursor.sample

    win.apply_theme("light")
    assert VizTheme.MODE == "light"
    # The grid canvas background tracks the (light) frame background.
    assert win._grid_view.backgroundBrush().color().name() == VizTheme.GRID_BG.lower()
    # The switch must not disturb the cursor or drop the capture.
    assert win.cursor.sample == keep and win._session is not None
    assert win._cmd_model.rowCount() > 0

    win.apply_theme("dark")
    assert VizTheme.MODE == "dark"
    assert win._grid_view.backgroundBrush().color().name() == VizTheme.GRID_BG.lower()

    # Every colour-caching view exposes retheme() (so apply_theme reaches them all).
    for view in (win._authoring, win._viz_grid, win._grid_view, win._audio_view,
                 win._timeline, win._reg_view, win._cmd_view, win._symbol_view,
                 win._raw_view, win._eye_view, win._meas_view, win._timing_page):
        assert callable(getattr(view, "retheme", None)), type(view).__name__

    # The persisted preference reflects the last switch.
    assert win._appearance_pref == "dark" and saved_preference() == "dark"
    print("ok: live MainWindow switch re-themes grid + keeps capture/cursor")


def test_the_timing_page_follows_a_live_switch():
    """The Timing Calculator follows View ▸ Appearance: each way, every style it builds
    from the palette, and the red of a failing margin, are the new palette's."""
    _app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("dark")
    win = MainWindow()
    page = win._timing_page
    for mode in ("light", "dark"):
        win.apply_theme(mode)
        assert VizTheme.ENTRY_BG in page.styleSheet(), mode
        assert f"color:{VizTheme.TEXT_DIM};" in page._subtitle.styleSheet(), mode
        assert f"color:{VizTheme.TEXT_DIM};" in page._trace_hint.styleSheet(), mode
        assert VizTheme.FRAME_BG in page._tree.header().styleSheet(), mode
        reds = set()
        it = QTreeWidgetItemIterator(page._tree)
        while it.value():
            if it.value().text(1).startswith("-"):
                reds.add(it.value().foreground(1).color().name().lower())
            it += 1
        assert reds == {VizTheme.SEM_ERROR.lower()}, (mode, reds)


def test_the_visualizer_dialog_button_follows_a_live_switch():
    """The Bus Visualizer's CDS Settings "Open…" button sets its own style; each way, it is
    the new palette's entry look."""
    _app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QPushButton

    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("light")
    win = MainWindow()
    (button,) = [b for b in win._authoring.findChildren(QPushButton) if b.text() == "Open…"]
    for mode in ("dark", "light"):
        win.apply_theme(mode)
        assert f"background:{VizTheme.ENTRY_BG};" in button.styleSheet(), mode
        assert f"color:{VizTheme.TEXT};" in button.styleSheet(), mode


def test_the_link_names_beside_the_timeline_follow_a_live_switch():
    """The Link name beside each timeline band is drawn in the theme's text colour, each
    way, so it never stays black on the dark palette."""
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("light")
    win = MainWindow()
    win.load_demo_links()
    assert len(win._ribbon_labels) >= 2
    for mode in ("dark", "light", "dark"):
        win.apply_theme(mode)
        for label in win._ribbon_labels:
            assert f"color: {VizTheme.TEXT};" in label.styleSheet(), mode

# --- VizTheme's palette keys must stay DECLARED for a type checker ----------------------
#
# apply_palette() setattr()s every palette key onto VizTheme, so a type checker sees none
# of them unless they are declared as bare annotations. These tests pin the declaration
# against the palettes in both directions, so adding a colour without declaring it (or
# declaring one that no palette defines) fails the build.

def _annotations():
    from swi3s_studio.ui.theme import VizTheme
    return set(getattr(VizTheme, "__annotations__", {}))   # getattr: PEP 649 on 3.14+


_DERIVED = {"GRID_BG", "SYM_COMMA"}      # set by apply_palette, not present in a palette


def test_viztheme_declares_every_palette_key():
    from swi3s_studio.ui import theme as t
    missing = (set(t._DARK) | _DERIVED) - _annotations()
    assert not missing, (
        f"these are assigned onto VizTheme but not declared, so a type checker cannot see "
        f"them: {sorted(missing)} — add `NAME: str` to the class")


def test_viztheme_declares_nothing_that_no_palette_defines():
    from swi3s_studio.ui import theme as t
    stray = _annotations() - set(t._DARK) - _DERIVED
    assert not stray, (
        f"declared on VizTheme but no palette defines them (dead or renamed): {sorted(stray)}")


def test_both_palettes_define_exactly_the_same_keys():
    """apply_palette() copies whichever palette is selected; a key present in only one
    means a theme switch leaves a stale colour from the other mode."""
    from swi3s_studio.ui import theme as t
    assert set(t._DARK) == set(t._LIGHT), (
        f"dark-only={sorted(set(t._DARK) - set(t._LIGHT))} "
        f"light-only={sorted(set(t._LIGHT) - set(t._DARK))}")
