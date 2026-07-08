"""Visualizer visual style — single source of truth for the SWI3S Studio
"Bus Visualizer" look, ported from the standalone SoundWire I3S Visualizer's
customtkinter dark theme.

Centralising the colours, backgrounds and corner radii here (rather than
scattering hex literals through the widgets) lets the authoring panel, its
sub-panes (Description / Notifications), the data-port key and the bus grid all
share one consistent palette — matching the original Visualizer.
"""
from __future__ import annotations

import os

_CHECK_SVG = os.path.join(os.path.dirname(__file__), "assets", "check.svg").replace("\\", "/")


class VizTheme:
    """Active palette. Colour attributes are (re)assigned by :func:`apply_palette`
    (default dark, set at import); consumers read ``VizTheme.X`` as before, so a
    theme switch + re-render picks up the new values. Sizes/typography below are
    theme-independent."""
    BORDER_WIDTH = 1
    CORNER_RADIUS = 6
    HEADER_CSS = "font-size:15px; font-weight:600;"   # panel-name headers
    TITLE_CSS = "font-size:18px; font-weight:600;"    # page / mode titles
    MODE = "dark"


# Each palette defines every colour key. apply_palette() copies one onto VizTheme.
# Dark is the original Visualizer palette; light is its readable counterpart
# (flipped backgrounds/text + slightly darkened data colours for white canvases).
_DARK = {
    "WINDOW_BG": "#242424", "FRAME_BG": "#2b2b2b", "ENTRY_BG": "#2b2b2b",
    "BORDER": "#565b5e", "TEXT": "#dce4ee", "TEXT_DIM": "#7a848d",
    "ACCENT": "#1f6aa5", "ACCENT_HOVER": "#144870",
    "CHECK_ON": "#1f6aa5", "CHECK_BORDER": "#949ba2", "OK_GREEN": "#388e3c",
    "GRID_LINE": "#8c9196", "GRID_TEXT": "#c8ccd0", "GRID_INK": "#0f0f12",
    "GRID_CDS_FILL": "#34373c",
    "PLOT_BG": "#1e2023", "CURSOR": "#f5f5f5", "AXIS": "#787878",
    "TRACE_PALETTE": ["#3c96c8", "#46be82", "#c85a6e", "#beaa46", "#9670c8", "#5ac8c8"],
    "RAW_DP": "#5ac8c8", "RAW_DN": "#ffaa64",
    "SEM_ERROR": "#dc505a", "SEM_ERROR_BG": "#5a2328", "SEM_SSP": "#e1b946",
    "SEM_COMMIT": "#5aaae6", "SEM_READ": "#b48ceb", "SEM_OTHER": "#5ac88c",
    "SEM_PING": "#6e7882", "SEM_BOOKMARK": "#e15ad2", "SEM_BRINGUP": "#3cc8dc",
    "PROV_DEFAULT": "#8c8c8c", "PROV_WRITTEN": "#3caa4b", "PROV_CSV": "#4682d2",
    "PROV_UI": "#e1962b",
    "SYM_INVALID": "#c8505a", "SYM_ROBUST": "#50a0dc", "SYM_DCODE": "#5abe82",
    "SYM_KCODE": "#aa78d2", "SYM_UNKNOWN": "#c8c8c8",
    "SCROLL_HANDLE": "#5a6068", "SCROLL_HANDLE_HOVER": "#6f7680",
}
_LIGHT = {
    "WINDOW_BG": "#e7e9ec", "FRAME_BG": "#fafbfc", "ENTRY_BG": "#ffffff",
    "BORDER": "#c2c6cb", "TEXT": "#1b1e22", "TEXT_DIM": "#6a7077",
    "ACCENT": "#1f6aa5", "ACCENT_HOVER": "#185888",
    "CHECK_ON": "#1f6aa5", "CHECK_BORDER": "#9aa0a6", "OK_GREEN": "#2e7d32",
    "GRID_LINE": "#9098a0", "GRID_TEXT": "#2a2e33", "GRID_INK": "#0f0f12",
    "GRID_CDS_FILL": "#e2e5e9",
    "PLOT_BG": "#ffffff", "CURSOR": "#1a1a1a", "AXIS": "#909090",
    "TRACE_PALETTE": ["#2f7db4", "#2fa86e", "#c04a5e", "#9c8520", "#8050c0", "#1f9b9b"],
    "RAW_DP": "#1f9b9b", "RAW_DN": "#cc6a14",
    "SEM_ERROR": "#c62828", "SEM_ERROR_BG": "#f6d2d6", "SEM_SSP": "#b8860b",
    "SEM_COMMIT": "#1f6aa5", "SEM_READ": "#7d3c98", "SEM_OTHER": "#2e8b57",
    "SEM_PING": "#737a82", "SEM_BOOKMARK": "#c026b8", "SEM_BRINGUP": "#1597ad",
    "PROV_DEFAULT": "#808080", "PROV_WRITTEN": "#2e8b3c", "PROV_CSV": "#2f6fc0",
    "PROV_UI": "#c47a16",
    "SYM_INVALID": "#c0303a", "SYM_ROBUST": "#2f7fc0", "SYM_DCODE": "#2ea05e",
    "SYM_KCODE": "#7d52b8", "SYM_UNKNOWN": "#888888",
    "SCROLL_HANDLE": "#b8bdc4", "SCROLL_HANDLE_HOVER": "#a0a6ad",
}
_PALETTES = {"dark": _DARK, "light": _LIGHT}


def apply_palette(mode: str) -> str:
    """Copy the `mode` ('dark'|'light') palette onto VizTheme and set the few
    derived colours. Returns the resolved mode. Call before (re)building widgets."""
    mode = mode if mode in _PALETTES else "dark"
    for key, val in _PALETTES[mode].items():
        setattr(VizTheme, key, val)
    VizTheme.GRID_BG = VizTheme.FRAME_BG       # grid canvas = panel background
    VizTheme.SYM_COMMA = VizTheme.SEM_SSP      # comma reuses the gold SSP colour
    VizTheme.MODE = mode
    return mode


PREF_KEY = "appearance/mode"        # QSettings key
DEFAULT_PREF = "system"             # follow the OS until the user chooses


def saved_preference() -> str:
    """The persisted appearance preference ('dark'|'light'|'system'); defaults to
    'system'. Reads QSettings (org/app set by app.py before the QApplication)."""
    try:
        from PySide6.QtCore import QSettings
        pref = str(QSettings().value(PREF_KEY, DEFAULT_PREF))
        return pref if pref in ("dark", "light", "system") else DEFAULT_PREF
    except Exception:
        return DEFAULT_PREF


def save_preference(pref: str) -> None:
    """Persist the appearance preference for next launch."""
    try:
        from PySide6.QtCore import QSettings
        QSettings().setValue(PREF_KEY, pref)
    except Exception:
        pass


def resolve_mode(pref: str) -> str:
    """Map an appearance preference to a concrete mode. 'system' follows the OS
    colour scheme (Qt 6.5+ QStyleHints.colorScheme), defaulting to dark."""
    if pref in ("dark", "light"):
        return pref
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        app = QApplication.instance()
        if app is not None:
            return "light" if app.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
    except Exception:
        pass
    return "dark"


def scrollbar_qss() -> str:
    """Themed scrollbar QSS (handle colour follows the active palette)."""
    h, hh = VizTheme.SCROLL_HANDLE, VizTheme.SCROLL_HANDLE_HOVER
    return (
        "QScrollBar:vertical { background:transparent; width:12px; margin:0; border:none; }"
        f"QScrollBar::handle:vertical {{ background:{h}; min-height:32px;"
        " border-radius:5px; margin:1px; }"
        f"QScrollBar::handle:vertical:hover {{ background:{hh}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; width:0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }"
        "QScrollBar:horizontal { background:transparent; height:12px; margin:0; border:none; }"
        f"QScrollBar::handle:horizontal {{ background:{h}; min-width:32px;"
        " border-radius:5px; margin:1px; }"
        f"QScrollBar::handle:horizontal:hover {{ background:{hh}; }}"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; height:0; }"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:none; }"
    )


apply_palette("dark")   # default until app.py applies the saved preference




def authoring_stylesheet() -> str:
    """Qt stylesheet for the authoring panel and its children — rounded,
    light-bordered text entries / text edits, uniform background, light text,
    centred checkbox indicators, and themed scrollbars. Parameter/label/entry text
    is a touch smaller; the panel-name headers keep their own (larger) inline font."""
    t = VizTheme
    return f"""
    QWidget {{ background: {t.FRAME_BG}; color: {t.TEXT}; }}
    QLabel {{ background: transparent; color: {t.TEXT}; font-size: 12px; }}
    QLabel:disabled {{ color: {t.TEXT_DIM}; }}
    QLineEdit, QPlainTextEdit {{
        background: {t.ENTRY_BG};
        color: {t.TEXT};
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px;
        padding: 1px 4px;
        font-size: 12px;
        selection-background-color: {t.ACCENT};
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled {{ color: {t.TEXT_DIM}; }}
    QGroupBox {{ border: none; }}
    /* Centre the (text-less) checkbox indicator: spacing:0 drops the trailing
       label gap, and subcontrol-position:center centres the indicator within the
       checkbox regardless of any residual widget slack, so the row's stretches
       then place it dead-centre in the column. */
    QCheckBox {{ background: transparent; spacing: 0px; }}
    QCheckBox::indicator {{
        subcontrol-position: center;
        width: 16px; height: 16px;
        border: {t.BORDER_WIDTH}px solid {t.CHECK_BORDER};
        border-radius: 4px;
        background: {t.ENTRY_BG};
    }}
    QCheckBox::indicator:checked {{
        background: {t.CHECK_ON};
        border-color: {t.CHECK_ON};
        image: url({_CHECK_SVG});
    }}
    QCheckBox::indicator:disabled {{ border-color: {t.TEXT_DIM}; }}
    {scrollbar_qss()}
    """


def analyzer_stylesheet() -> str:
    """Qt stylesheet for the Analyzer's views — gives the item-views (command
    table, register tree, symbol / measurements tables) and their chrome the same
    flat, dark, 1px-bordered, rounded look as the Visualizer, instead of the
    platform-default light controls. Apply per analyzer view (like
    authoring_stylesheet) — the Visualizer keeps its own sheet."""
    t = VizTheme
    return f"""
    QWidget {{ background: {t.FRAME_BG}; color: {t.TEXT}; }}
    QLabel {{ background: transparent; color: {t.TEXT}; font-size: 12px; }}
    QLabel:disabled {{ color: {t.TEXT_DIM}; }}
    QAbstractScrollArea {{ background: {t.FRAME_BG}; }}
    /* One background shade everywhere (matches the Visualizer): no alternating-row
       banding, headers and corner share the panel background — only borders separate. */
    QTableView, QTreeView, QTreeWidget, QTableWidget {{
        background: {t.FRAME_BG};
        alternate-background-color: {t.FRAME_BG};
        color: {t.TEXT};
        gridline-color: {t.BORDER};
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px;
        selection-background-color: {t.ACCENT};
        selection-color: white;
        font-size: 12px;
        outline: none;
    }}
    QTableView::item, QTreeView::item {{ padding: 1px 4px; }}
    QHeaderView {{ background: {t.FRAME_BG}; }}
    QHeaderView::section {{
        background: {t.FRAME_BG};
        color: {t.TEXT};
        border: none;
        border-right: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-bottom: {t.BORDER_WIDTH}px solid {t.BORDER};
        padding: 2px 6px;
        font-size: 12px;
    }}
    QTableCornerButton::section {{ background: {t.FRAME_BG}; border: none; }}
    /* Action buttons + the device selector reuse the Visualizer recipe. */
    QPushButton {{
        background: {t.ACCENT}; color: white; border: none;
        border-radius: {t.CORNER_RADIUS}px; padding: 4px 8px;
    }}
    QPushButton:hover {{ background: {t.ACCENT_HOVER}; }}
    /* Checkable toggle buttons (Show Toggles, Hide CDS/Clock, …): the unchecked
       ("deselected") state is a neutral themed chip with readable text — not the
       accent fill — so it's legible and clearly distinct from the checked state in
       both light and dark. Checked stays accent-on-white. */
    QPushButton:checkable:!checked {{
        background: {t.ENTRY_BG}; color: {t.TEXT};
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
    }}
    QPushButton:checkable:!checked:hover {{ border-color: {t.ACCENT}; }}
    QPushButton:disabled {{ background: {t.ENTRY_BG}; color: {t.TEXT_DIM};
        border: {t.BORDER_WIDTH}px solid {t.BORDER}; }}
    QComboBox {{
        background: {t.ENTRY_BG}; color: {t.TEXT};
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px; padding: 1px 6px;
    }}
    /* Merge the drop-down button into the field: no separate border/background box
       beside the arrow (the "weirdness" at the picker's right edge). */
    QComboBox::drop-down {{ border: none; background: transparent; width: 18px; }}
    /* Tool button (the multi-select filter) reuses the entry recipe; its pop-up menu is
       themed too so checkable items stay readable (native menus are OS-styled). */
    QToolButton {{
        background: {t.ENTRY_BG}; color: {t.TEXT}; font-size: 12px;
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px; padding: 2px 8px;
    }}
    QToolButton:hover {{ border-color: {t.ACCENT}; }}
    QToolButton::menu-indicator {{ image: none; }}
    QMenu {{ background: {t.FRAME_BG}; color: {t.TEXT};
             border: {t.BORDER_WIDTH}px solid {t.BORDER}; }}
    QMenu::item {{ padding: 3px 20px 3px 8px; }}
    QMenu::item:selected {{ background: {t.ACCENT}; color: white; }}
    QMenu::separator {{ height: 1px; background: {t.BORDER}; margin: 3px 0; }}
    QComboBox QAbstractItemView {{
        background: {t.FRAME_BG}; color: {t.TEXT};
        selection-background-color: {t.ACCENT}; selection-color: white;
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
    }}
    QCheckBox {{ background: transparent; spacing: 4px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: {t.BORDER_WIDTH}px solid {t.CHECK_BORDER};
        border-radius: 4px;
        background: {t.ENTRY_BG};
    }}
    QCheckBox::indicator:checked {{
        background: {t.CHECK_ON};
        border-color: {t.CHECK_ON};
        image: url({_CHECK_SVG});
    }}
    QCheckBox::indicator:disabled {{ border-color: {t.TEXT_DIM}; }}
    {scrollbar_qss()}
    """


def chrome_stylesheet() -> str:
    """Window-chrome QSS baked with the active palette, applied to the QMainWindow so
    it cascades to the dock tab bars and modal dialogs (which otherwise render with the
    OS-native style — unreadable in light mode). Covers: the window gutters, the tabbed-
    dock QTabBar (deselected tab text stays dark/readable), and the decode QProgressDialog
    (dark-on-dark in light mode without this)."""
    t = VizTheme
    return f"""
    QMainWindow {{ background: {t.FRAME_BG}; }}
    QMainWindow::separator {{ background: {t.FRAME_BG}; width: 4px; height: 4px; }}
    /* The in-window menu bar (Windows/Linux) needs explicit colours or dark mode
       renders dark text on a dark bar. macOS uses the native system bar and ignores
       QSS, so this is harmless there. */
    QMenuBar {{ background: {t.FRAME_BG}; color: {t.TEXT}; }}
    QMenuBar::item {{ background: transparent; color: {t.TEXT}; padding: 3px 8px; }}
    QMenuBar::item:selected {{ background: {t.ACCENT}; color: white; }}
    QMenuBar::item:pressed {{ background: {t.ACCENT}; color: white; }}
    QMenuBar::item:disabled {{ color: {t.TEXT_DIM}; }}
    /* Dock title bars share the panel background (no two-tone strip beside the tab
       bar) and carry no divider line. */
    QDockWidget {{ background: {t.FRAME_BG}; color: {t.TEXT}; titlebar-normal-icon: none; }}
    QDockWidget::title {{
        background: {t.FRAME_BG}; color: {t.TEXT};
        padding: 3px 6px; border: none; text-align: center;
    }}
    /* Dock tabs sit at the BOTTOM of each pane (South) and are flush against the
       window edge, so a tab rounded only on one side gets its other corners clipped by
       that edge. Draw each tab as a fully-rounded chip lifted a few px off both edges
       (top/bottom margin) so none of its corners are cut; deselected reads via dimmer
       text, selected via full text + an accent outline. */
    QTabBar {{ background: {t.FRAME_BG}; border: none; }}
    QTabBar::tab {{
        background: {t.FRAME_BG}; color: {t.TEXT_DIM};
        padding: 4px 12px; margin: 3px 3px 4px 0;
        border: {t.BORDER_WIDTH}px solid {t.BORDER};
        border-radius: {t.CORNER_RADIUS}px;
    }}
    QTabBar::tab:selected {{ color: {t.TEXT}; border-color: {t.ACCENT}; }}
    QTabBar::tab:!selected:hover {{ color: {t.TEXT}; }}
    QProgressDialog {{ background: {t.FRAME_BG}; color: {t.TEXT}; }}
    QProgressDialog QLabel {{ color: {t.TEXT}; background: transparent; }}
    QProgressBar {{
        background: {t.ENTRY_BG}; color: {t.TEXT};
        border: {t.BORDER_WIDTH}px solid {t.BORDER}; border-radius: {t.CORNER_RADIUS}px;
        text-align: center;
    }}
    QProgressBar::chunk {{ background: {t.ACCENT}; border-radius: {t.CORNER_RADIUS}px; }}
    """


# Reusable themed scrollbar style lives in scrollbar_qss() (palette-aware) above.

