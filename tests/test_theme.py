"""Theme switcher tests (Qt offscreen): the swappable dark/light palette, the
preference resolve + persistence, and a live MainWindow theme switch that
re-themes the views without losing the loaded capture.

Run: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_theme.py
"""
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# conftest keeps every QSettings of the run in a throwaway ini, so save_preference() (here
# and inside MainWindow.apply_theme) cannot touch the user's real appearance preference.
import pytest
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

pytestmark = pytest.mark.gui


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


def test_resolve_mode():
    assert resolve_mode("dark") == "dark"
    assert resolve_mode("light") == "light"
    # 'system' resolves to a concrete mode (depends on the offscreen platform's
    # reported scheme; must be one of the two, never 'system').
    assert resolve_mode("system") in ("dark", "light")


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


def test_the_timing_page_follows_a_live_switch():
    """The Timing Calculator follows View ▸ Appearance: each way, every style it builds
    from the palette, and the colours of the margins, are the new palette's."""
    _app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("dark")
    win = MainWindow()
    page = win._timing_page
    page.flush_recompute()
    for mode in ("light", "dark"):
        win.apply_theme(mode)
        assert VizTheme.ENTRY_BG in page.styleSheet(), mode
        assert f"color:{VizTheme.TEXT_DIM};" in page._status.styleSheet(), mode
        assert f"color:{VizTheme.TEXT_DIM};" in page._trace_hint.styleSheet(), mode
        assert VizTheme.FRAME_BG in page._tree.header().styleSheet(), mode
        colours = {"-": set(), "+": set()}
        it = QTreeWidgetItemIterator(page._tree)
        while it.value():
            found = re.search(r'= <span style="color:(#[0-9a-fA-F]{6})">([+-])',
                              it.value().text(0))
            if found:
                colours[found.group(2)].add(found.group(1).lower())
            it += 1
        assert colours == {"-": {VizTheme.SEM_ERROR.lower()},
                           "+": {VizTheme.DIAG_OK.lower()}}, (mode, colours)


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

def test_the_filter_expression_dialog_follows_a_live_switch():
    """Commands ▸ Filter ▸ Filter Expression is drawn on the frame background of the theme
    it opens in and of each theme switched to while it exists."""
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow

    apply_palette("dark")
    win = MainWindow()
    dlg = win._filter_expression_box()
    for mode in ("dark", "light", "dark"):
        if mode != VizTheme.MODE:
            win.apply_theme(mode)
        img = dlg.grab().toImage()
        assert img.pixelColor(2, 2).name() == VizTheme.FRAME_BG.lower(), mode
        assert img.pixelColor(img.width() - 3, img.height() - 3).name() == \
            VizTheme.FRAME_BG.lower(), mode
    win.close()

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


def _hls(color):
    import colorsys

    from PySide6.QtGui import QColor
    q = QColor(color)
    return colorsys.rgb_to_hls(q.redF(), q.greenF(), q.blueF())


def _data_colours():
    """Every palette colour drawn as a trace, data-port line or cursor pair, of both themes:
    a stream colour chosen in one theme may be shown on the other's background."""
    from swi3s_studio.ui import theme as t
    out = []
    for pal in (t._DARK, t._LIGHT):
        out += list(pal["DP_LINE_PALETTE"]) + list(pal["TRACE_PALETTE"])
        out += [pal["RAW_DP"], pal["RAW_DN"]]
    return out


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("bg_key", ["FRAME_BG", "WINDOW_BG", "ENTRY_BG", "PLOT_BG"])
def test_text_in_a_data_colour_reads_on_each_background_in_the_same_hue(mode, bg_key):
    """legible_text_color gives every data colour at least TEXT_ON_DATA_CONTRAST:1 on each
    background of each theme: a colour that has it is returned as it is; one short of it
    keeps its hue (within 3 degrees) and its lightness moves darker on a light background
    and lighter on a dark one, no further than the floor needs."""
    from swi3s_studio.ui import theme as t
    from swi3s_studio.ui.theme import TEXT_ON_DATA_CONTRAST, contrast, legible_text_color
    bg = (t._LIGHT if mode == "light" else t._DARK)[bg_key]
    assert TEXT_ON_DATA_CONTRAST >= 3.0
    for colour in _data_colours():
        out = legible_text_color(colour, bg)
        assert contrast(out, bg) >= TEXT_ON_DATA_CONTRAST, (colour, bg, out)
        if contrast(colour, bg) >= TEXT_ON_DATA_CONTRAST:
            assert out == colour.lower(), (colour, bg, out)
            continue
        (h0, l0, s0), (h1, l1, _s1) = _hls(colour), _hls(out)
        if s0 > 0.05:
            turn = abs(h0 - h1) * 360
            assert min(turn, 360 - turn) <= 3.0, (colour, bg, out)
        assert (l1 < l0) if mode == "light" else (l1 > l0), (colour, bg, out)
        assert contrast(out, bg) < TEXT_ON_DATA_CONTRAST + 0.1, (colour, bg, out)


def test_a_colour_with_enough_contrast_is_returned_unchanged():
    from swi3s_studio.ui.theme import contrast, legible_text_color
    for pal in (_LIGHT, _DARK):
        bg = pal["FRAME_BG"]
        assert legible_text_color(pal["TEXT"], bg) == pal["TEXT"].lower()
        for colour in pal["DP_LINE_PALETTE"]:
            if contrast(colour, bg) >= 3.5:
                assert legible_text_color(colour, bg) == colour.lower()
    assert legible_text_color("#ff50a7", _LIGHT["FRAME_BG"]) != "#ff50a7"   # 2.9:1 there
