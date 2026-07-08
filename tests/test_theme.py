"""Theme switcher tests (Qt offscreen): the swappable dark/light palette, the
preference resolve + persistence, and a live MainWindow theme switch that
re-themes the views without losing the loaded capture.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_theme.py
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

# Isolate ALL QSettings in this process to a throwaway ini, so save_preference()
# (here and inside MainWindow.apply_theme) can't overwrite the user's real
# appearance preference. setDefaultFormat makes the no-arg QSettings() use it.
_SETTINGS_DIR = tempfile.mkdtemp(prefix="swi3s_test_settings_")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _SETTINGS_DIR)

from swi3s_studio.ui.theme import (VizTheme, apply_palette, resolve_mode,
                                    saved_preference, save_preference,
                                    PREF_KEY, _DARK, _LIGHT)


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
    app = QApplication.instance() or QApplication([])
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
                 win._raw_view, win._eye_view, win._meas_view):
        assert callable(getattr(view, "retheme", None)), type(view).__name__

    # The persisted preference reflects the last switch.
    assert win._appearance_pref == "dark" and saved_preference() == "dark"
    print("ok: live MainWindow switch re-themes grid + keeps capture/cursor")


if __name__ == "__main__":
    test_apply_palette()
    test_resolve_mode()
    test_preference_roundtrip()
    test_live_switch_rethemes_window()
    print("ALL THEME TESTS PASSED")
