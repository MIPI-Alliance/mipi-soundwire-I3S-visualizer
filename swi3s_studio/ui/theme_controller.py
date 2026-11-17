"""The window's appearance: View ▸ Appearance (dark, light or following the OS), the
window chrome's stylesheets, Preferences (the waveforms, and the Timing Diagrams with a live
preview), and the re-theme of every colour-caching view on a switch.

`ThemeController` holds the appearance preference. It is a QObject child of the window, so
its connection to the OS scheme signal goes with the window.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from . import diagram_style, line_style
from .theme import (
    VizTheme,
    analyzer_stylesheet,
    apply_palette,
    chrome_stylesheet,
    combo_qss,
    resolve_mode,
    save_preference,
    saved_preference,
)

if TYPE_CHECKING:
    from .main_window import MainWindow


def title_button_style() -> str:
    """A title bar's ✕ / ⤢ buttons: no frame at rest, a subtle rounded tint on hover, and
    on a maximize button while its pane is the maximized one. The docks' title bars and the
    Commands pane's header share it."""
    t = VizTheme
    return (f"QToolButton {{ border:none; background:transparent; color:{t.TEXT_DIM};"
            f" font-size:12px; padding:0; }}"
            f"QToolButton:hover, QToolButton:checked {{ background:{t.BORDER};"
            f" color:{t.TEXT}; border-radius:{t.CORNER_RADIUS}px; }}")


class ThemeController(QObject):
    """The appearance of `win`, a MainWindow: `pref` is 'dark', 'light' or 'system', read
    from the saved preference."""

    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self._win = win
        self.pref = saved_preference()

    def apply_chrome(self) -> None:
        """(Re)apply the window-chrome stylesheets that bake VizTheme colours: the
        Analysis command-table page, its faux title bar, the Visualization splitter
        gutter + floating restore button, the central-stack background and the main
        window gutters. Run at build and again on every theme switch."""
        t = VizTheme
        self._win._analysis_page.setStyleSheet(analyzer_stylesheet())
        if self._win._grid_pane is not None:
            # The Bus Grid's toolbar buttons.
            self._win._grid_pane.setStyleSheet(analyzer_stylesheet())
        self._win._cmd_header.setStyleSheet(
            f"QWidget#cmdHeader {{ background:{t.FRAME_BG}; }}")
        self._win._cmd_title.setStyleSheet(f"padding:3px 6px; background:{t.FRAME_BG};")
        self._win._cmd_max_btn.setStyleSheet(title_button_style())
        self._win._timing_max_btn.setStyleSheet(title_button_style())
        self._win._viz_max_btn.setStyleSheet(title_button_style())
        self._win._viz_split.setStyleSheet(f"QSplitter {{ background:{t.FRAME_BG}; }} "
                                           f"QSplitter::handle {{ background:{t.FRAME_BG}; }}")
        self._win._central_stack.setStyleSheet(
            f"#centralStack {{ background:{t.FRAME_BG}; }}")
        for bar in self._win._dock_titlebars:    # custom dock title bars
            bar.retheme()
        self._win._style_dock_tabbars()          # tab-bar base tracks the palette
        # Cascades to the dock tab bars (deselected tab text readable in light mode) and
        # to the decode QProgressDialog (which is otherwise OS-native/dark in light mode).
        self._win.setStyleSheet(chrome_stylesheet())

    def apply(self, pref: str) -> None:
        """Switch appearance live (pref: 'dark' | 'light' | 'system'). Persist the
        choice, re-resolve + apply the palette, re-theme the window chrome and
        broadcast retheme() to every colour-caching view so the plots and the bus
        grid switch instantly — keeping the cursor, filters and bookmarks. 'system'
        tracks the OS scheme (see follow_os_scheme)."""
        self.pref = pref
        save_preference(pref)
        apply_palette(resolve_mode(pref))
        self.restyle()

    def show_preferences(self, diagrams: bool = False) -> None:
        """Preferences, on its Timing Diagrams tab with `diagrams`. While it is open the
        Timing page's diagram draws the Timing Diagrams tab's settings as they are edited.
        On OK, store both sets and redraw every pane; on Cancel, the diagram draws the
        stored settings again."""
        from .preferences_dialog import PreferencesDialog
        page = self._win._timing_page
        dlg = PreferencesDialog(line_style.preferences(), self._win, mode=VizTheme.MODE)
        dlg.set_diagram_units(*page.diagram_units())
        if diagrams:
            dlg.show_diagram_tab()

        def preview(prefs: dict) -> None:
            diagram_style.set_preview(prefs)
            page.restyle_diagram()

        dlg.diagramChanged.connect(preview)
        try:
            ok = dlg.exec()
        finally:
            diagram_style.set_preview(None)
        if ok:
            line_style.set_preferences(dlg.preferences())
            diagram_style.set_preferences(dlg.diagram_preferences())
            self.restyle()
        page.restyle_diagram()

    def show_diagram_preferences(self) -> None:
        """Preferences on its Timing Diagrams tab (View ▸ Diagram Colors, Lines and
        Fonts…)."""
        self.show_preferences(diagrams=True)

    def restyle(self) -> None:
        """Re-theme the chrome and every colour-caching view from the active palette and
        the waveform preferences, keeping the cursor, filters and bookmarks."""
        self.apply_chrome()
        for pickers in self._win._pane_pickers.values():
            for picker in pickers:
                picker.setStyleSheet(combo_qss())
        # Views that cache pens / brushes / backgrounds re-read the palette.
        w = self._win
        lanes = (w._audio_lanes, w._raw_lanes, w._symbol_lanes, w._sample_lanes, w._eye_lanes)
        for view in (w._authoring, w._viz_grid, w._grid_view,
                     *(v for lane in lanes for v in lane.views), *lanes,
                     *w._ribbons, *w._ribbon_labels, w._reg_view, w._cmd_view,
                     w._meas_view, w._timing_page, w._pair_view, *w._empty_hints,
                     w._expr_dialog, *w._history_dialogs(), *w.import_warning_dialogs()):
            rt = getattr(view, "retheme", None)
            if callable(rt):
                rt()
        # The grids only recolour their canvas on retheme(); re-render their data so
        # every cell picks up the new palette (authored grid always; decoded grid
        # when a capture is loaded).
        self._win._authoring_ctl.refresh_grid()
        self._win._refresh_grid()

    def follow_os_scheme(self) -> None:
        """Re-apply the appearance whenever the OS light/dark scheme changes, while the
        preference is 'system'."""
        app = QApplication.instance()
        if app is None:
            return
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_os_scheme_changed)
        except (AttributeError, RuntimeError):       # older Qt without the signal
            pass

    def _on_os_scheme_changed(self, *_a) -> None:
        """OS light/dark switched — re-apply only when following the system."""
        if self.pref == "system":
            self.apply("system")
