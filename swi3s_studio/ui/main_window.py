"""SWI3S Studio main window: command table + register map + 2D bus grid +
decoded audio + a timeline ribbon, all bound by one shared time cursor.

Navigation: a View menu shows/raises any panel (so a closed or tab-hidden dock
is one click away), and the timeline ribbon / command table / cursor stay in
two-way sync — click the timeline to seek, and the command table follows; pick a
command and the timeline + register map follow.
"""
from __future__ import annotations

import bisect
import os
import sys
import tempfile
from typing import List, Optional

import numpy as np

from PySide6.QtCore import Qt, QTimer, QEvent, QThread, QObject, QSettings, Signal
from PySide6.QtGui import QAction, QActionGroup, QIntValidator, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDockWidget, QFileDialog,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QScrollBar, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

import swi3score

from .. import __version__
from ..session import Session
from ..model import RegisterMap
from ..model.bus_config import BusConfig, demo_config
from ..model import viz_engine
from ..analysis import (count_errors, is_error, capture_measurements,
                        register_diff, register_diff_report)
from ..workspace import Workspace, session_from_source
from ..ingest import saleae_binary
from ..export import write_commands_csv
from .cursor import TimeCursor
from .command_table import CommandTableModel, CommandTableView, CommandFilterProxy
from .register_view import RegisterView
from .grid_view import GridView
from .audio_view import AudioView
from .symbol_view import SymbolView
from .decoded_sample_view import DecodedSampleView
from .raw_view import RawCaptureView
from .eye_view import EyeView
from .measurements_view import MeasurementsView
from .timeline import TimelineRibbon
from .mode_controller import (ModeManager, VISUALIZATION, TIMING, ANALYSIS, MODES)
from .authoring import AuthoringPanel
from .timing_view import TimingView
from .theme import (VizTheme, analyzer_stylesheet, chrome_stylesheet, apply_palette,
                    resolve_mode, saved_preference, save_preference)

DEFAULT_GRID_ROWS = 64   # Bus Grid rows to draw (config layout + TX raster); 64 is a
                         # common payload repeat interval. User-settable per capture.


def _bundled_data_path(filename: str) -> str:
    """Locate a file under data/ across dev, installed, and PyInstaller layouts
    (mirrors model.registers._default_registers_json)."""
    here = os.path.dirname(os.path.dirname(__file__))            # swi3s_studio/
    candidates = [
        os.path.join(os.path.dirname(here), "data", filename),   # repo layout
        os.path.join(here, "data", filename),                    # packaged in pkg
        os.path.join(sys.prefix, "share", "swi3s-studio", filename),  # pip data-files
    ]
    base = getattr(sys, "_MEIPASS", None)                        # PyInstaller bundle
    if base:
        candidates.insert(0, os.path.join(base, "data", filename))
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def _app_home() -> str:
    """The obvious, user-editable directory where init.csv lives: next to the
    executable for a frozen build, else the repo root for a source checkout. This
    is the spot a user drops their own init.csv to replace the default."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # swi3s_studio/ui/main_window.py -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _init_csv_path() -> str:
    """Resolve a user-supplied init.csv (it's a local file, not shipped with the
    app). Order: $SWI3S_INIT_CSV, then init.csv in the app home (repo root / next to
    the executable), then the working directory. Returns the app-home path when none
    exists, so load_init_csv's open fails cleanly and falls back to the demo config."""
    env = os.environ.get("SWI3S_INIT_CSV")
    if env and os.path.exists(env):
        return env
    for d in (_app_home(), os.getcwd()):
        c = os.path.join(d, "init.csv")
        if os.path.exists(c):
            return c
    return os.path.join(_app_home(), "init.csv")


class _LoadWorker(QObject):
    """Builds a Session (decode) and its audio store off the GUI thread, so opening
    a large capture doesn't freeze the UI. The factory runs the heavy decode; the
    audio store (per-channel arrays + render pyramids) is built here too — both were
    the synchronous work that beach-balled the main thread on dense captures. The
    per-section UI stats (numpy over millions of clock edges, ~0.3s) are precomputed
    here as well so load_session doesn't recompute them on the GUI thread."""
    done = Signal(object, object, object)   # (session, audio_store, extras dict)
    failed = Signal(str)

    def __init__(self, factory, pdm_dc_block: bool = False) -> None:
        super().__init__()
        self._factory = factory
        self._pdm_dc_block = pdm_dc_block

    def run(self) -> None:
        try:
            session = self._factory()
            store = session.audio_store(pdm_dc_block=self._pdm_dc_block)
            extras = {"sections": session.section_ui_stats()}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via `failed`
            self.failed.emit(str(exc))
            return
        self.done.emit(session, store, extras)


class _CheckableMenu(QMenu):
    """A menu that stays open while toggling its checkable items, so several filter
    values can be picked in one go; non-checkable items (e.g. 'All') close it."""

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 (Qt signature)
        act = self.activeAction()
        if act is not None and act.isCheckable() and act.isEnabled():
            act.trigger()                    # toggle + emit; keep the menu open
            return
        super().mouseReleaseEvent(e)


class _CurrentPageStack(QStackedWidget):
    """A QStackedWidget that sizes to the CURRENT page, not the max of all pages.

    The default QStackedWidget reports its minimum/size hint as the maximum over
    every page. Here the Visualization page's authoring panel is tall (~636 px
    min), so in Analysis mode the central area still demanded that height and
    squeezed the bottom docks flat — the Audio/Grid separator would catch but not
    move. Reporting only the visible page's hints frees that space."""

    def sizeHint(self):  # noqa: N802 - Qt override
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self):  # noqa: N802 - Qt override
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()



class MainWindow(QMainWindow):
    def __init__(self, register_map: Optional[RegisterMap] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"SWI3S Studio {__version__}")
        self.resize(1320, 860)
        self.setDockNestingEnabled(True)

        self._rmap = register_map or RegisterMap.load()
        self._appearance_pref = saved_preference()   # 'dark' | 'light' | 'system'
        # PDM DC-block preference (default OFF): with it off the analyzer shows the
        # true density (all-ones → +1); on, it removes a real mic's density bias.
        self._pdm_dc_block = QSettings().value("audio/pdm_dc_block", False, type=bool)
        self._session: Optional[Session] = None
        self._audio_store = None
        self._starts: List[int] = []          # command start samples (ascending)
        self._syncing = False                  # guards two-way cursor/selection sync
        self._from_symbol = False              # cursor change originated in the CDS symbol pane
        self._from_sample = False              # cursor change originated in the Decoded Samples pane
        self._tx_map = False                   # Bus Grid shows the TX-map raster (data transitions)
        self._tx_persist = False               # TX map: persist a column if it EVER toggled in view
        self._tx_start_row = 0                 # TX map: 0-based top row of the scrolled window
        self._grid_rows = DEFAULT_GRID_ROWS    # rows to draw in the Bus Grid (config layout + raster)

        self.cursor = TimeCursor()
        self.cursor.sampleChanged.connect(self._on_cursor)

        # Command table (central, always visible) with a filter/search bar.
        self._cmd_model = CommandTableModel([], self._rmap, 1)
        self._cmd_proxy = CommandFilterProxy()
        self._cmd_proxy.setSourceModel(self._cmd_model)
        self._cmd_view = CommandTableView()
        self._cmd_view.setModel(self._cmd_proxy)
        self._cmd_view.selectionModel().selectionChanged.connect(self._on_command_selected)
        # Filtering lives in the Filter menu (built in _build_menu) to save the
        # vertical space an inline filter bar would cost; the command-table title
        # doubles as the live "N of M (active filters)" indicator.
        self._expr_dialog = None
        self._expr_edit = None
        self._cmd_proxy.rowsInserted.connect(self._update_cmd_title)
        self._cmd_proxy.rowsRemoved.connect(self._update_cmd_title)
        self._cmd_proxy.modelReset.connect(self._update_cmd_title)

        analysis_page = QWidget()
        col = QVBoxLayout(analysis_page)
        col.setContentsMargins(0, 0, 0, 0)
        self._cmd_title = QLabel("Command Table")  # the central pane has no dock title
        self._cmd_title.setAlignment(Qt.AlignCenter)   # match the dock title bars
        col.addWidget(self._cmd_title)
        col.addWidget(self._cmd_view)

        # Central area is a stack of per-mode pages (Visualization | Timing |
        # Analysis). Analysis is the command-table page above; Visualization is the
        # authoring editor; Timing is a placeholder until that phase lands.
        self._central_stack = _CurrentPageStack()
        self._authoring = AuthoringPanel()
        self._authoring.configChanged.connect(self._refresh_authored_grid)
        self._authoring.issueActivated.connect(self._on_issue_activated)
        self._authoring.loadInitRequested.connect(self.load_init_csv)
        self._authoring.resetRequested.connect(
            lambda: self._authoring.set_config(BusConfig()))
        self._authoring.loadRequested.connect(self.load_authoring_csv)
        self._authoring.saveRequested.connect(self.save_authoring_csv)
        self._authoring.saveSvgRequested.connect(self.export_grid)
        self._authoring.exportJsonRequested.connect(self.export_frame_json)
        self._authoring.maximizeRequested.connect(self._toggle_viz_maximize)
        # Visualization page mirrors the old visualizer's layout: the parameter
        # panels on top, the placed bus grid as the large canvas below. The grid
        # is its own GridView here (Analysis uses the shared bottom Bus Grid dock).
        self._viz_grid = GridView()
        self._viz_grid.set_show_key(False)     # DP key only when maximized / exported
        # Floating "Show Parameters" button, visible only while the frame is
        # maximized (the authoring panel — and its Maximize button — is collapsed
        # then). Pinned top-right, near where "Maximize Frame" was clicked.
        self._viz_restore_btn = QPushButton("⤢  Show Parameters", self._viz_grid)
        self._viz_restore_btn.clicked.connect(self._toggle_viz_maximize)
        self._viz_restore_btn.hide()
        self._viz_grid.installEventFilter(self)   # keep the button pinned top-right
        viz = QSplitter(Qt.Vertical)
        viz.addWidget(self._authoring)
        viz.addWidget(self._viz_grid)
        viz.setStretchFactor(0, 2)
        viz.setStretchFactor(1, 3)
        # Dark splitter gutter so the gap between the parameters and the grid matches
        # the panels (the default handle is a lighter system gray).
        viz.setHandleWidth(3)
        self._viz_split = viz
        self._viz_page = viz
        self._timing_page = TimingView()
        self._analysis_page = analysis_page
        self._central_stack.addWidget(self._viz_page)       # index 0 (Visualization)
        self._central_stack.addWidget(self._timing_page)    # index 1 (Timing)
        self._central_stack.addWidget(self._analysis_page)  # index 2 (Analysis)
        self.setCentralWidget(self._central_stack)
        # Uniform dark background for the central area and the window's own gutters
        # so the gaps between panes match the panels (id-scoped so it doesn't bleed
        # into the docked widgets' own styling).
        self._central_stack.setObjectName("centralStack")
        self._apply_chrome_styles()

        # Timeline ribbon (top dock).
        self._timeline = TimelineRibbon()
        self._timeline.seeked.connect(self._on_seek)
        self._timeline_dock = self._add_dock("Timeline", self._timeline, Qt.TopDockWidgetArea)

        # Register map (right dock).
        self._reg_view = RegisterView(self._rmap)
        self._reg_view.registerEdited.connect(self._on_register_edited)
        self._reg_view.overridesCleared.connect(self._on_register_overrides_cleared)
        self._reg_dock = self._add_dock("Register Map", self._reg_view, Qt.RightDockWidgetArea)

        # 2D bus grid + audio + CDS symbols (bottom docks, tabbed).
        self._grid_view = GridView()
        self._grid_dock = self._add_dock("Bus Grid", self._build_grid_pane(),
                                         Qt.BottomDockWidgetArea)
        self._audio_view = AudioView()
        self._audio_view.seeked.connect(self._on_seek)   # click a waveform to seek
        self._audio_view.playCursorMoved.connect(self._on_seek)  # play head sweeps the cursor
        self._audio_view.stopped.connect(self._on_playback_stopped)  # full refresh on stop
        self._audio_dock = self._add_dock("Audio", self._audio_view, Qt.BottomDockWidgetArea)
        self._symbol_view = SymbolView()
        self._symbol_view.sampleSelected.connect(self._on_symbol_selected)
        self._symbol_view.moreAboveRequested.connect(self._extend_symbols_above)
        self._symbol_dock = self._add_dock("CDS Symbols", self._symbol_view, Qt.BottomDockWidgetArea)
        self._sample_view = DecodedSampleView()
        self._sample_view.sampleSelected.connect(self._on_sample_selected)
        self._sample_view.filtersChanged.connect(self._on_sample_filters_changed)
        self._sample_dock = self._add_dock("Decoded Samples", self._sample_view, Qt.BottomDockWidgetArea)
        # Populate the (windowed) sample table only when its tab is actually shown.
        self._sample_dock.visibilityChanged.connect(self._on_sample_dock_visible)
        self._raw_view = RawCaptureView()
        self._raw_view.sampleSelected.connect(self._on_seek)
        self._raw_view.portSamplesRequested.connect(self._ensure_bit_samples)
        self._raw_dock = self._add_dock("Raw Capture", self._raw_view, Qt.BottomDockWidgetArea)
        self._eye_view = EyeView()
        self._eye_view.sampleSelected.connect(self._on_timing_jump)
        self._eye_dock = self._add_dock("Timing", self._eye_view, Qt.BottomDockWidgetArea)
        self._meas_view = MeasurementsView()
        self._meas_dock = self._add_dock("Statistics", self._meas_view, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(self._grid_dock, self._audio_dock)
        self.tabifyDockWidget(self._audio_dock, self._symbol_dock)
        self.tabifyDockWidget(self._symbol_dock, self._sample_dock)
        self.tabifyDockWidget(self._sample_dock, self._raw_dock)
        self.tabifyDockWidget(self._raw_dock, self._eye_dock)
        self.tabifyDockWidget(self._reg_dock, self._meas_dock)
        self._grid_dock.raise_()
        self._reg_dock.raise_()

        # Match the central pane's faux title bar to the real dock title bars: a
        # plain QLabel inherits the (larger) default widget font, while QDockWidget
        # draws its title with the dock's own font — copy it so they line up.
        self._cmd_title.setFont(self._grid_dock.font())

        # Docks that should stay tabbed together — used to re-tabify on re-show
        # (Qt drops a re-shown dock out of its tab group otherwise).
        self._tab_groups = [
            [self._grid_dock, self._audio_dock, self._symbol_dock, self._sample_dock,
             self._raw_dock, self._eye_dock],
            [self._reg_dock, self._meas_dock],
        ]

        self._bookmarks: set = set()

        # No status-bar strip — it wasted vertical space and only showed the mode /
        # a hint. Keep a hidden label so the existing status-message API
        # (self._status.setText(...)) still works without rendering anything.
        self._status = QLabel("Bus Visualizer — demo capture ready in Bus Analyzer")
        self._build_menu()

        # Three-mode shell: a top segmented switcher swaps the central page and the
        # visible dock set per mode. Analysis owns all the current docks; the other
        # modes start lean and grow in later phases. Build last so every dock exists.
        self._mode_mgr = ModeManager(
            self,
            set_central_index=self._set_central_index,
            mode_docks={
                VISUALIZATION: [],
                TIMING: [],
                ANALYSIS: [self._timeline_dock, self._reg_dock, self._meas_dock,
                           self._grid_dock, self._audio_dock, self._symbol_dock,
                           self._sample_dock, self._raw_dock, self._eye_dock],
            },
            on_change=self._on_mode_changed,
        )
        self._mode_mgr.switch_to(VISUALIZATION)   # Bus Visualizer is the default mode on open

        # Follow the OS light/dark scheme live while the preference is 'system'.
        app = QApplication.instance()
        if app is not None:
            try:
                app.styleHints().colorSchemeChanged.connect(self._on_os_color_scheme_changed)
            except (AttributeError, RuntimeError):       # older Qt without the signal
                pass

        # Preload the demo capture into the Analyzer session in the background (it's
        # near-instant), staying in the Bus Visualizer default — so switching to Bus
        # Analyzer shows the decoded demo immediately, with the title reading its name.
        try:
            self.load_session(
                Session.from_demo(256, cold_start=True, register_map=self._rmap),
                switch_mode=False)
        except Exception:  # noqa: BLE001 - never let a demo-decode hiccup block startup
            pass

        # The colour-caching views (notably the bus grid, whose palette constants are
        # captured at import — before app startup applies the saved palette) must be
        # rethemed once now, or they render with stale/default colours until the first
        # manual theme switch. apply_theme rethemes + re-renders every view.
        self.apply_theme(self._appearance_pref)

    def _set_central_index(self, index: int) -> None:
        self._central_stack.setCurrentIndex(index)

    def _on_mode_changed(self, mode: str) -> None:
        # Menus that only make sense while analysing a capture are disabled outside
        # Analysis mode; the View dock toggles only apply to Analysis's docks.
        analysing = (mode == ANALYSIS)
        for menu in getattr(self, "_analysis_menus", []):
            menu.setEnabled(analysing)
        # The View menu's pane toggles only apply to the Analyzer's docks; disable
        # them outside Analysis (Full Screen stays enabled — it's not in this list).
        for act in getattr(self, "_view_pane_actions", []):
            act.setEnabled(analysing)
        # The bus grid is shared: show the authored grid in Visualization, the
        # decoded grid in Analysis.
        if mode == VISUALIZATION:
            self._refresh_authored_grid()
            self._apply_viz_split_sizes()
        elif analysing and self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)
        # First time into Analysis, lay out the default arrangement (Bus Grid active,
        # bottom row ~25% tall, Register Map ~33% wide). Deferred so the window has a
        # real geometry; later visits restore the user's own arrangement.
        if analysing and not getattr(self, "_analysis_laid_out", False):
            QTimer.singleShot(0, self._apply_analysis_layout)
        self._status.setText(f"{mode} mode")
        self._update_title()

    def _update_title(self) -> None:
        """Window title: the loaded capture's name is shown ONLY in Analyzer mode (the
        capture is what you're analysing there); Visualizer/Timing just show the app
        name. A long capture label (e.g. two .bin channel files) is middle-elided."""
        base = f"SWI3S Studio {__version__}"
        sess = self._session
        if getattr(self, "_mode_mgr", None) is not None \
                and self._mode_mgr.current() == ANALYSIS and sess is not None:
            self.setWindowTitle(f"{base} — {self._elide(sess.source_label())}")
        else:
            self.setWindowTitle(base)

    @staticmethod
    def _elide(text: str, limit: int = 60) -> str:
        """Middle-elide `text` to `limit` chars with '…' so a long two-file title
        (clock & data basenames) still fits the title bar."""
        if len(text) <= limit:
            return text
        keep = limit - 1
        head = (keep + 1) // 2
        return text[:head] + "…" + text[len(text) - (keep - head):]

    def _apply_analysis_layout(self) -> None:
        """One-time default Analysis arrangement: Bus Grid is the active bottom tab,
        the bottom dock row is ~25% of the window height, and the Register Map is
        ~33% of the width. No-op until the window is shown (so the proportions use a
        real geometry); marks itself done only once it actually applies."""
        if getattr(self, "_analysis_laid_out", False) or not self.isVisible():
            return
        self._analysis_laid_out = True
        self._grid_dock.raise_()             # Bus Grid, not Audio, on top
        self._reg_dock.raise_()
        h, w = max(1, self.height()), max(1, self.width())
        self.resizeDocks([self._grid_dock], [int(h * 0.25)], Qt.Vertical)
        self.resizeDocks([self._reg_dock], [int(w * 0.33)], Qt.Horizontal)

    def _apply_viz_split_sizes(self) -> None:
        """Size the Visualization splitter so the parameters pane is its content
        height (no unused space below) and the grid takes the rest. The splitter
        clamps to the panel's minimum, so this is the same result whether it runs on
        first show or when returning to the mode."""
        if self._mode_mgr.current() != VISUALIZATION:
            return
        h = self._viz_split.height()
        if h <= 0:
            return
        params = max(1, min(self._authoring.minimumSizeHint().height(), h - 80))
        self._viz_split.setSizes([params, h - params])

    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        # The splitter has no height until the window is shown, so the first-load
        # sizing (run in __init__) was a no-op and left unused space. Re-apply once
        # the geometry exists so first load matches the return-to-mode appearance.
        if not getattr(self, "_sized_viz_once", False):
            self._sized_viz_once = True
            QTimer.singleShot(0, self._apply_viz_split_sizes)
        if not getattr(self, "_stripped_fs_once", False):
            self._stripped_fs_once = True
            # AppKit inserts "Enter Full Screen" into the View menu lazily (after show,
            # and again each time the menu opens); strip it now, on a delay, AND right
            # before the View menu opens so full-screen is left to the OS window
            # title-bar button. Best-effort — no-op off macOS.
            self._strip_macos_fullscreen_menu_item()
            QTimer.singleShot(0, self._strip_macos_fullscreen_menu_item)
            QTimer.singleShot(500, self._strip_macos_fullscreen_menu_item)
            vm = getattr(self, "_view_menu", None)
            if vm is not None:
                vm.aboutToShow.connect(self._strip_macos_fullscreen_menu_item)

    def _strip_macos_fullscreen_menu_item(self) -> None:
        """Remove AppKit's auto-inserted full-screen item from the (native) menu bar.
        The green title-bar button still toggles full screen; only the menu entry goes.
        No-op unless we're on macOS with PyObjC and the item is present."""
        import sys
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
            main_menu = NSApp().mainMenu()
            if main_menu is None:
                return
            for i in range(main_menu.numberOfItems()):
                sub = main_menu.itemAtIndex_(i).submenu()
                if sub is None:
                    continue
                for j in range(sub.numberOfItems() - 1, -1, -1):
                    it = sub.itemAtIndex_(j)
                    title = (it.title() or "")
                    # "Enter/Exit Full Screen"; the localised action selector is
                    # toggleFullScreen: — match either so we catch it regardless of title.
                    if "Full Screen" in title or (
                            it.action() is not None and "toggleFullScreen" in str(it.action())):
                        sub.removeItemAtIndex_(j)
        except Exception:  # noqa: BLE001 - PyObjC absent / AppKit shape differs
            pass

    # ---- visualization-mode authoring ----
    def _refresh_authored_grid(self, highlight=None) -> None:
        """Render Bus-Visualizer from the first-party Visualizer engine (swi3s_studio.swviz,
        exact parity) — the merged bus model gives the grid bits + clashes + the notifications."""
        cfg = self._authoring.config()
        rows = max(1, min(int(cfg.rows_to_draw), 1024))
        try:
            with tempfile.TemporaryDirectory() as d:
                path = cfg.to_csv_file(os.path.join(d, "authored.csv"))
                cells, clashes, issues, ncols, nrows = viz_engine.render_payload(path, rows)
        except Exception as exc:  # noqa: BLE001 — surface engine errors as an issue
            from ..analysis.issues import Issue, ERROR
            self._viz_grid.set_bus_model([], {}, cfg.column_count(), 1)
            self._authoring.set_issues([Issue(ERROR, "Engine", f"could not build: {exc}")])
            return
        self._viz_grid.set_bus_model(cells, clashes, ncols, nrows)
        self._authoring.set_issues(issues)

    def _on_issue_activated(self, cells: list) -> None:
        self._refresh_authored_grid()

    def load_init_csv(self) -> None:
        """Load a user-supplied init.csv (from the app home / repo root) into the
        authoring panel, showing "Loaded: init.csv" like Load Settings. Falls back to
        the built-in demo config when no init.csv is present or it can't be read."""
        path = _init_csv_path()
        try:
            from ..ingest import visualizer_csv
            cfg = BusConfig.from_csv(visualizer_csv.normalized_path(path))
        except Exception as exc:  # noqa: BLE001 — missing/unreadable init.csv
            self._authoring.set_config(demo_config())
            self._authoring.set_file_status("Loaded: built-in demo config")
            self._status.setText(f"init.csv unavailable ({exc}); loaded demo config")
            return
        self._authoring.set_config(cfg)
        self._status.setText(f"Loaded init config: {path}")
        self._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")

    def load_authoring_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load bus config (visualizer CSV)", "", "CSV (*.csv);;All files (*)")
        if path:
            self._load_authoring_csv(path)

    def open_visualizer_csv(self) -> None:
        """File ▸ Open Visualizer CSV: load a Visualizer config CSV into the authoring
        panel, switching to Visualizer mode if we're elsewhere. Legacy / v2.0 CSVs are
        upgraded to the current format on the way in (visualizer_csv.normalized_path)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Visualizer CSV", self._last_capture_dir(),
            "Visualizer CSV (*.csv);;All files (*)")
        if not path:
            return
        if self._mode_mgr.current() != VISUALIZATION:
            self._mode_mgr.switch_to(VISUALIZATION)
        self._remember_capture_dir(path)
        self._load_authoring_csv(path)

    def _load_authoring_csv(self, path: str) -> None:
        """Read a Visualizer CSV into the authoring panel (upgrading legacy/v2.0 CSVs)."""
        try:
            from ..ingest import visualizer_csv
            cfg_path = visualizer_csv.normalized_path(path)   # upgrade any legacy/v2.0 CSV
            self._authoring.set_config(BusConfig.from_csv(cfg_path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load config", f"Couldn't read that CSV: {exc}")
            return
        self._status.setText(f"Loaded config: {path}")
        self._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")


    def save_authoring_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save bus config", "bus_config.csv", "CSV (*.csv)")
        if not path:
            return
        self._authoring.commit_edits()     # flush a name/field still being edited
        try:
            self._authoring.config().to_csv_file(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save config", str(exc))
            return
        self._status.setText(f"Config saved: {path}")
        self._authoring.set_file_status(f"Saved: {os.path.basename(path)}")

    def export_frame_json(self) -> None:
        """Write the authored bus model (the Visualizer engine's serialized model —
        bits + clashes + warnings) to JSON."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export frame model JSON", "frame_model.json", "JSON (*.json)")
        if not path:
            return
        self._authoring.commit_edits()
        cfg = self._authoring.config()
        rows = max(1, min(int(cfg.rows_to_draw), 1024))
        try:
            import json
            with tempfile.TemporaryDirectory() as d:
                cpath = cfg.to_csv_file(os.path.join(d, "authored.csv"))
                model = viz_engine.model_json(cpath, rows)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(model, f, indent=2)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export JSON", str(exc))
            return
        self._status.setText(f"Frame model exported: {path}")
        self._authoring.set_file_status(f"Exported: {os.path.basename(path)}")

    # ---- timing calculator settings (JSON) ----
    def save_timing_settings(self) -> None:
        """Save the Timing Calculator settings (TimingView.to_dict) to JSON."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save timing settings", "timing_settings.json", "JSON (*.json)")
        if not path:
            return
        try:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._timing_page.to_dict(), f, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "Save timing settings", str(exc))
            return
        self._status.setText(f"Timing settings saved: {path}")

    def open_timing_settings(self) -> None:
        """Load Timing Calculator settings from JSON, switching to Timing mode."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open timing settings", "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a timing-settings object")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open timing settings", f"Couldn't read that file: {exc}")
            return
        if self._mode_mgr.current() != TIMING:
            self._mode_mgr.switch_to(TIMING)
        self._timing_page.set_from_dict(data)
        self._status.setText(f"Timing settings loaded: {path}")

    def use_authoring_as_expected(self) -> None:
        """Compare ▸ Compare with Visualizer: push the Visualizer-authored config
        into Analysis ▸ Compare as the expected config (register-map diff)."""
        if self._session is None:
            QMessageBox.information(self, "Compare with Visualizer",
                                    "Load a capture in Analysis mode first, then compare "
                                    "the Visualizer config against it.")
            return
        self._authoring.commit_edits()
        cfg = self._authoring.config()
        with tempfile.TemporaryDirectory() as d:
            path = cfg.to_csv_file(os.path.join(d, "expected.csv"))
            self._mode_mgr.switch_to(ANALYSIS)
            self._run_compare(path)

    def _apply_chrome_styles(self) -> None:
        """(Re)apply the window-chrome stylesheets that bake VizTheme colours: the
        Analysis command-table page, its faux title bar, the Visualization splitter
        gutter + floating restore button, the central-stack background and the main
        window gutters. Run at build and again on every theme switch."""
        t = VizTheme
        self._analysis_page.setStyleSheet(analyzer_stylesheet())
        if hasattr(self, "_grid_pane"):
            self._grid_pane.setStyleSheet(analyzer_stylesheet())   # Bus Grid toolbar buttons
        self._cmd_title.setStyleSheet(f"padding:3px 6px; background:{t.FRAME_BG};")
        self._viz_restore_btn.setStyleSheet(
            f"QPushButton {{ background:{t.ACCENT}; color:white; border:none; "
            f"border-radius:{t.CORNER_RADIUS}px; padding:6px 10px; }} "
            f"QPushButton:hover {{ background:{t.ACCENT_HOVER}; }}")
        self._viz_split.setStyleSheet(f"QSplitter {{ background:{t.FRAME_BG}; }} "
                                      f"QSplitter::handle {{ background:{t.FRAME_BG}; }}")
        self._central_stack.setStyleSheet(f"#centralStack {{ background:{t.FRAME_BG}; }}")
        # Cascades to the dock tab bars (deselected tab text readable in light mode) and
        # to the decode QProgressDialog (which is otherwise OS-native/dark in light mode).
        self.setStyleSheet(chrome_stylesheet())

    def apply_theme(self, pref: str) -> None:
        """Switch appearance live (pref: 'dark' | 'light' | 'system'). Persist the
        choice, re-resolve + apply the palette, re-theme the window chrome and
        broadcast retheme() to every colour-caching view so the plots and the bus
        grid switch instantly — keeping the cursor, filters and bookmarks. 'system'
        tracks the OS scheme (see _on_os_color_scheme_changed)."""
        self._appearance_pref = pref
        save_preference(pref)
        apply_palette(resolve_mode(pref))
        self._apply_chrome_styles()
        # Views that cache pens / brushes / backgrounds re-read the palette.
        for view in (self._authoring, self._viz_grid, self._grid_view, self._audio_view,
                     self._timeline, self._reg_view, self._cmd_view, self._symbol_view,
                     self._sample_view, self._raw_view, self._eye_view, self._meas_view):
            rt = getattr(view, "retheme", None)
            if callable(rt):
                rt()
        # The grids only recolour their canvas on retheme(); re-render their data so
        # every cell picks up the new palette (authored grid always; decoded grid
        # when a capture is loaded).
        self._refresh_authored_grid()
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)

    def _on_os_color_scheme_changed(self, *_a) -> None:
        """OS light/dark switched — re-apply only when following the system."""
        if getattr(self, "_appearance_pref", "system") == "system":
            self.apply_theme("system")

    def _build_grid_pane(self) -> QWidget:
        """Wrap the Analysis Bus Grid (self._grid_view) with a toolbar: Show/Hide
        Toggles (the TX-map raster), Persistence (fill a column if it EVER toggled in
        the viewed rows), and a Rows-To-Draw box (raster + config-grid height)."""
        host = QWidget()
        host.setStyleSheet(analyzer_stylesheet())   # theme the toolbar buttons (readable
        #                                             checked/unchecked in light + dark)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        self._toggles_btn = QPushButton("Show Toggles")
        self._toggles_btn.setCheckable(True)
        self._toggles_btn.setToolTip(
            "Replace the config layout with the TX map: mark every UI that carried a "
            "physical data-line transition. Needs no decoded config / CSV.")
        self._toggles_btn.toggled.connect(self._toggle_tx_map)

        self._persist_btn = QPushButton("Persistence On")
        self._persist_btn.setCheckable(True)
        self._persist_btn.setEnabled(False)     # only meaningful while toggles are shown
        self._persist_btn.setToolTip(
            "Persist toggles: fill a column for all viewed rows if it EVER toggled "
            "within them — transporting columns become solid bars. With Rows To Draw "
            "= 1 this collapses to 'did this column ever toggle'.")
        self._persist_btn.toggled.connect(self._toggle_tx_persist)

        rows_lbl = QLabel("Rows To Draw")
        self._grid_rows_edit = QLineEdit(str(self._grid_rows))
        self._grid_rows_edit.setValidator(QIntValidator(1, 10240, self))
        self._grid_rows_edit.setFixedWidth(64)
        self._grid_rows_edit.setToolTip("Number of bus rows to draw in the Bus Grid (1–10240)")
        self._grid_rows_edit.editingFinished.connect(self._on_grid_rows_edit)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.addWidget(self._toggles_btn)
        bar.addWidget(self._persist_btn)
        bar.addStretch(1)
        bar.addWidget(rows_lbl)
        bar.addWidget(self._grid_rows_edit)
        lay.addLayout(bar)
        # The TX map is a whole-capture raster; a mid-row-start capture has ~millions
        # of rows, so render only the Rows-To-Draw window and scroll it with a
        # dedicated bar (re-render per position). Hidden unless the TX map is shown.
        self._tx_scroll = QScrollBar(Qt.Vertical)
        self._tx_scroll.setVisible(False)
        self._tx_scroll.valueChanged.connect(self._on_tx_scroll)
        grid_row = QHBoxLayout()
        grid_row.setContentsMargins(0, 0, 0, 0)
        grid_row.setSpacing(0)
        grid_row.addWidget(self._grid_view, 1)
        grid_row.addWidget(self._tx_scroll)
        lay.addLayout(grid_row, 1)
        # Wheel over the grid scrolls the TX window (when shown).
        self._grid_view.viewport().installEventFilter(self)
        self._grid_pane = host          # re-styled on theme switch (_apply_chrome_styles)
        return host

    def _add_dock(self, title: str, widget: QWidget, area) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(title.replace(" ", ""))
        dock.setWidget(widget)
        self.addDockWidget(area, dock)
        if not hasattr(self, "_dock_area"):
            self._dock_area = {}
        self._dock_area[dock] = area          # home area, for robust re-show
        return dock

    def _build_menu(self) -> None:
        f = self.menuBar().addMenu("&File")
        # File actions are grouped by the mode they act on. Opens work from any mode
        # (each switches to the mode it needs) and the handlers guard when there's
        # nothing to act on, so nothing in File is mode-gated.
        analyzer = f.addMenu("&Analyzer")
        analyzer.addAction("Open &Capture…", self.open_capture)
        analyzer.addAction("Open &Visualizer Config…", self.open_visualizer_config)
        analyzer.addAction("&Save Visualizer Config…", self.export_grid_csv)
        analyzer.addAction("&Clear Comparison", self.clear_comparison)
        analyzer.addSeparator()
        analyzer.addAction("Open &Workspace…", self.open_workspace)
        analyzer.addAction("Save &Workspace…", self.save_workspace)
        analyzer.addSeparator()
        analyzer.addAction("Save Bus &Image…", lambda: self.export_grid(self._grid_view))
        self._file_analyzer_menu = analyzer

        visualizer = f.addMenu("&Visualizer")
        visualizer.addAction("&Open Settings…", self.open_visualizer_csv)
        visualizer.addAction("&Save Settings…", self.save_authoring_csv)
        visualizer.addAction("Save &Image…", lambda: self.export_grid(self._viz_grid))
        visualizer.addAction("Save Bus &Model…", self.export_frame_json)

        timing = f.addMenu("&Timing")
        timing.addAction("&Open Settings…", self.open_timing_settings)
        timing.addAction("&Save Settings…", self.save_timing_settings)

        f.addSeparator()
        f.addAction("&Quit", self.close)

        # Commands: filter the command table (submenu) + export it as CSV, and jump
        # the cursor between the sync-point commits (SSCR/DSCR — where the config takes
        # effect). ⌘> / ⌘< (Ctrl on non-mac) mirror the +/- SSP move accelerators.
        cmds = self.menuBar().addMenu("&Commands")
        flt = self._build_filter_menu(cmds)
        cmds.addSeparator()
        nxt = cmds.addAction("Next SSCR/DSCR Commit\tCtrl+>", self.next_sscr)
        # '>' is Shift+'.', so the OS delivers Ctrl+Shift+. — bind BOTH spellings or the
        # shortcut silently never matches (it read as ⌘> in the menu either way).
        nxt.setShortcuts([QKeySequence("Ctrl+>"), QKeySequence("Ctrl+Shift+.")])
        prv = cmds.addAction("Previous SSCR/DSCR Commit\tCtrl+<", self.prev_sscr)
        prv.setShortcuts([QKeySequence("Ctrl+<"), QKeySequence("Ctrl+Shift+,")])
        cmds.addSeparator()
        cmds.addAction("&Export as CSV…", self.export_commands)

        rm = self.menuBar().addMenu("&Devices")
        rm.addAction("&Names…", self.manage_device_names)
        rm.addAction("&Register Maps…", self.manage_register_maps)
        rm.addAction("&Hub Depth…", self.manage_hub_depths)

        a = self.menuBar().addMenu("&Audio")
        a.addAction("Per-Dataport &Scrambler…", self.scrambler_overrides_dialog)
        # Manual Stream Sync Point: for a post-commit capture the SSPA/SSCR was never on
        # the wire, so interval>1 ports decode at an arbitrary phase — pick the row and
        # step it (the phase repeats every interval) until the audio is clean.
        ssp = a.addMenu("Manual &SSP Move")
        ssp.addAction("&Set SSP at Cursor Row", self.set_ssp_at_cursor)
        ssp.addAction("Move SSP &+1 Row\tCtrl+.", lambda: self.step_ssp_row(+1)).setShortcut("Ctrl+.")
        # -1 Row: bind ⌘, and show the accelerator in the menu. macOS reserves ⌘, for
        # the standard Preferences item, so Qt's text-heuristic would give this action a
        # Preferences menu-role and pull it out of the Audio menu (hiding the shortcut) —
        # force NoRole so it stays put with its accelerator shown.
        minus = ssp.addAction("Move SSP &−1 Row\tCtrl+,", lambda: self.step_ssp_row(-1))
        minus.setMenuRole(QAction.MenuRole.NoRole)
        minus.setShortcut(QKeySequence("Ctrl+,"))
        ssp.addAction("&Clear Manual SSP", lambda: self.set_ssp_row_async(-1))
        # PDM DC-block toggle (default OFF): off shows the true density on the wire
        # (all-ones → +1 DC); on removes a real mic's density bias for listening.
        self._pdm_dc_action = a.addAction("Block &PDM DC Bias")
        self._pdm_dc_action.setCheckable(True)
        self._pdm_dc_action.setChecked(self._pdm_dc_block)
        self._pdm_dc_action.setToolTip(
            "Off: decode the true PDM density (a constant all-ones stream reads full-"
            "scale +1). On: subtract the mean so a real mic's density bias doesn't "
            "swamp the audio (a constant/DC pattern then reads ~0).")
        self._pdm_dc_action.toggled.connect(self._on_pdm_dc_toggled)
        self._export_action = a.addAction("&Export Audio as WAV…", self.export_audio)
        self._export_action.setEnabled(False)
        a.addSeparator()
        self._audio_menu = a
        self._play_device_menu = a.addMenu("Playback &Output Device")
        self._play_depth_menu = a.addMenu("Playback &Bit Depth")
        self._play_rate_menu = a.addMenu("Playback &Decimation")
        self._build_audio_playback_menu()

        b = self.menuBar().addMenu("&Bookmarks")
        b.addAction("&Toggle Bookmark at Cursor\tCtrl+B", self.toggle_bookmark).setShortcut("Ctrl+B")
        b.addAction("&Next Bookmark\tCtrl+]", self.next_bookmark).setShortcut("Ctrl+]")
        b.addAction("&Previous Bookmark\tCtrl+[", self.prev_bookmark).setShortcut("Ctrl+[")
        b.addAction("&Clear Bookmarks", self.clear_bookmarks)

        # Menus that only apply while analysing a capture — disabled in other modes.
        self._analysis_menus = [cmds, a, b]

        # View menu: show/raise any panel (reopens a closed or tab-hidden dock).
        # The pane items only apply to the Analyzer's docks, so they're disabled
        # outside Analysis mode (see _on_mode_changed). Full-screen is left to the OS
        # (the window title-bar button); we don't add a menu item, and on macOS we
        # strip AppKit's auto-inserted "Enter Full Screen" (see showEvent).
        v = self.menuBar().addMenu("&View")
        self._view_menu = v
        self._view_pane_actions = []   # View items disabled outside Analysis
        # (The Command Table is the always-visible central pane, so it needs no
        # show/hide item here; the View menu is just the show/hide-panes menu.)
        # Checkable per-dock toggles that route SHOW through _show_dock (so a
        # reopened dock re-joins its tab group instead of losing the tab bar).
        self._dock_actions = {}        # dock -> its checkable View-menu action
        for label, dock in (("Timeline", self._timeline_dock),
                            ("Register Map", self._reg_dock),
                            ("Statistics", self._meas_dock),
                            ("Bus Grid", self._grid_dock),
                            ("Audio", self._audio_dock),
                            ("CDS Symbols", self._symbol_dock),
                            ("Decoded Samples", self._sample_dock),
                            ("Raw Capture", self._raw_dock),
                            ("Timing", self._eye_dock)):
            act = v.addAction(label)
            act.setCheckable(True)
            act.setChecked(not dock.isHidden())
            act.toggled.connect(lambda on, d=dock: self._show_dock(d) if on else d.hide())
            self._dock_actions[dock] = act
            self._view_pane_actions.append(act)
            # Keep the menu check in sync with the dock's own close button.
            dock.visibilityChanged.connect(lambda _vis, d=dock: self._sync_dock_action(d))

        # TX map moved to buttons in the Bus Grid pane itself (Show Toggles /
        # Persistence — see _build_grid_pane), keeping the View menu to show/hide panes.

        # Appearance (dark / light / follow-system), persisted across launches and
        # applied live — see apply_theme. Not gated to Analysis mode (it themes the
        # whole app, including Visualization).
        v.addSeparator()
        appearance = v.addMenu("&Appearance")
        self._appearance_group = QActionGroup(self)
        self._appearance_group.setExclusive(True)
        for label, pref in (("Follow &System", "system"), ("&Light", "light"),
                            ("&Dark", "dark")):
            act = appearance.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._appearance_pref == pref)
            act.triggered.connect(lambda _c=False, p=pref: self.apply_theme(p))
            self._appearance_group.addAction(act)

        h = self.menuBar().addMenu("&Help")
        h.addAction("&Keyboard Shortcuts…", self.show_keyboard_shortcuts)
        h.addAction("&Timeline Marks Legend…", self.show_timeline_legend)

    # ---- Filter submenu (under Commands; replaces the inline filter bar) ----
    def _build_filter_menu(self, parent):
        fm = parent.addMenu("&Filter")
        fm.addAction("Filter &Expression…", self._filter_expression_dialog)
        fm.addSeparator()
        self._kind_menu = self._make_filter_submenu(
            fm, "&Commands", [], self._cmd_proxy.set_kinds, exclude="Ping")
        self._dev_menu = self._make_filter_submenu(
            fm, "&Devices", [(d, f"Device {d}") for d in range(12)],
            self._cmd_proxy.set_devices)
        self._group_menu = self._make_filter_submenu(
            fm, "&Groups", [(g, f"Group {g}") for g in range(4)],
            self._cmd_proxy.set_groups)
        fm.addSeparator()
        self._errors_only_act = fm.addAction("&Errors Only")
        self._errors_only_act.setCheckable(True)
        self._errors_only_act.toggled.connect(
            lambda on: (self._cmd_proxy.set_errors_only(on), self._on_command_filter_changed()))
        fm.addSeparator()
        fm.addAction("Clear &All Filters", self._clear_all_filters)
        return fm

    def _make_filter_submenu(self, parent, title, items, setter, exclude=None):
        """A stay-open checkable submenu (+ an 'All' reset). `setter(set)` applies
        the selection (empty = all). `sub._fill(items)` repopulates it. `exclude` (a
        value) adds an 'All excl. <value>' quick action that checks every entry but
        that one (used for 'All excl. Ping' on the command-kind filter)."""
        sub = _CheckableMenu(title, self)
        parent.addMenu(sub)
        sub._actions = []

        def apply():
            setter({a.data() for a in sub._actions if a.isChecked()})
            self._on_command_filter_changed()

        def clear():
            for a in sub._actions:
                a.blockSignals(True)
                a.setChecked(False)
                a.blockSignals(False)
            apply()

        def select_all_except(value):
            for a in sub._actions:
                a.blockSignals(True)
                a.setChecked(a.data() != value)
                a.blockSignals(False)
            apply()

        def fill(its):
            sub.clear()
            sub._actions = []
            sub.addAction("All").triggered.connect(clear)
            if exclude is not None:
                sub.addAction(f"All excl. {exclude}").triggered.connect(
                    lambda: select_all_except(exclude))
            sub.addSeparator()
            for value, label in its:
                a = sub.addAction(str(label))
                a.setCheckable(True)
                a.setData(value)
                a.toggled.connect(lambda _on: apply())
                sub._actions.append(a)
            apply()                              # sync the proxy to the (empty) selection

        sub._fill = fill
        sub._clear = clear
        fill(items)
        return sub

    def _filter_expression_dialog(self) -> None:
        """Non-modal dialog with the free-text boolean expression (filters live as
        you type) — the one filter that can't be a menu item."""
        if self._expr_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Filter Expression")
            lay = QVBoxLayout(dlg)
            lay.addWidget(QLabel("Filter commands (substring; and / or / parentheses):"))
            self._expr_edit = QLineEdit()
            self._expr_edit.setMinimumWidth(360)
            self._expr_edit.textChanged.connect(
                lambda t: (self._cmd_proxy.set_text(t), self._on_command_filter_changed()))
            lay.addWidget(self._expr_edit)
            row = QHBoxLayout()
            row.addStretch(1)
            clr = QPushButton("Clear")
            clr.clicked.connect(self._expr_edit.clear)
            row.addWidget(clr)
            close = QPushButton("Close")
            close.clicked.connect(dlg.hide)
            row.addWidget(close)
            lay.addLayout(row)
            self._expr_dialog = dlg
        self._expr_dialog.show()
        self._expr_dialog.raise_()
        self._expr_edit.setFocus()

    def _clear_all_filters(self) -> None:
        self._kind_menu._clear()
        self._dev_menu._clear()
        self._group_menu._clear()
        self._errors_only_act.setChecked(False)        # toggled → set_errors_only(False)
        if self._expr_edit is not None:
            self._expr_edit.clear()                    # textChanged → set_text("")
        else:
            self._cmd_proxy.set_text("")
        self._on_command_filter_changed()

    def _update_cmd_title(self) -> None:
        """The command-table title doubles as the live filter indicator:
        'Command Table — N of M  (active filters)'."""
        title = getattr(self, "_cmd_title", None)
        if title is None:
            return
        base = "Command Table"
        if self._session is None or self._cmd_model is None:
            title.setText(base)
            return
        total = self._cmd_model.rowCount()
        shown = self._cmd_proxy.rowCount()
        bits = self._cmd_proxy.describe_active()
        if shown != total or bits:
            suffix = f" — {shown:,} of {total:,}"
            if bits:
                suffix += f"   ({' · '.join(bits)})"
            title.setText(base + suffix)
        else:
            title.setText(base)

    def _on_command_filter_changed(self) -> None:
        """A command filter changed: refresh the title indicator AND re-pop the
        nearest still-visible command to the top of the table, so the selection tracks
        the cursor against the new filter (the previously-selected command may now be
        hidden, or a nearer one revealed)."""
        self._update_cmd_title()
        if self._session is not None:
            self._select_command_for_sample(int(self.cursor.sample))
        self._update_nav_starts()

    def _update_nav_starts(self) -> None:
        """Push the command table's currently-VISIBLE command starts to the timeline so
        Left/Right arrow navigation skips commands hidden by the active filter."""
        if self._session is None or self._cmd_model is None:
            self._timeline.set_nav_starts(None)
            return
        proxy = self._cmd_proxy
        starts = []
        for pr in range(proxy.rowCount()):
            sr = proxy.mapToSource(proxy.index(pr, 0)).row()
            if 0 <= sr < len(self._starts):
                starts.append(int(self._starts[sr]))
        self._timeline.set_nav_starts(starts)

    def _refresh_audio_devices(self) -> None:
        """Audio ▸ Refresh Devices: force a re-scan (the cached list otherwise keeps
        the snapshot from when the menu was first built), then rebuild the submenu."""
        self._audio_view.refresh_devices()
        self._build_audio_playback_menu()

    def _build_audio_playback_menu(self) -> None:
        """Populate the Audio ▸ playback submenus (output device, bit depth,
        decimation) — moved off the Audio pane's toolbar to keep it uncluttered.
        Each is a checkable, mutually-exclusive group that sets the AudioView's
        playback state. Rebuilt on demand so a hot-plugged device shows up."""
        av = self._audio_view
        # --- output device ---
        m = self._play_device_menu
        m.clear()
        self._play_device_group = QActionGroup(self)
        self._play_device_group.setExclusive(True)
        names = av.device_names()
        if not names:
            act = m.addAction("(no audio output devices)")
            act.setEnabled(False)
        for i, name in enumerate(names):
            act = m.addAction(name)
            act.setCheckable(True)
            act.setChecked(i == av.device_index)
            act.triggered.connect(lambda _c=False, idx=i: av.set_device_index(idx))
            self._play_device_group.addAction(act)
        m.addSeparator()
        m.addAction("Refresh Devices", self._refresh_audio_devices)

        # --- bit depth ---
        m = self._play_depth_menu
        m.clear()
        self._play_depth_group = QActionGroup(self)
        self._play_depth_group.setExclusive(True)
        for bits in (16, 24):
            act = m.addAction(f"{bits}-bit")
            act.setCheckable(True)
            act.setChecked(av.play_depth == bits)
            act.triggered.connect(lambda _c=False, b=bits: av.set_play_depth(b))
            self._play_depth_group.addAction(act)
        m.setToolTip("24-bit preserves fidelity for 24-bit sources; most output "
                     "devices play both identically by ear.")

        # --- decimation (resample for playback) — per (device, dataport) ---
        self._build_decimation_menu()

    def _build_decimation_menu(self) -> None:
        """(Re)build the Audio ▸ Playback Decimation submenu. Decimation is set per
        (device, dataport): one submenu per stream, each an exclusive rate group.
        Rebuilt on capture load (streams change); shows a placeholder before then."""
        av = self._audio_view
        m = self._play_rate_menu
        m.clear()
        self._play_rate_groups = []          # keep the QActionGroups alive
        rates = [("Native (no resample)", None), ("8 kHz", 8000),
                 ("16 kHz", 16000), ("32 kHz", 32000), ("44.1 kHz", 44100),
                 ("48 kHz", 48000), ("96 kHz", 96000)]
        streams = av.streams()
        if not streams:
            act = m.addAction("(load a capture with audio)")
            act.setEnabled(False)
            return
        for dev, dp in streams:
            sub = m.addMenu(f"Device {dev} · DP{dp}")
            group = QActionGroup(self)
            group.setExclusive(True)
            current = av.play_rate_target_for(dev, dp)
            for label, hz in rates:
                act = sub.addAction(label)
                act.setCheckable(True)
                act.setChecked(current == hz)
                act.triggered.connect(
                    lambda _c=False, d=dev, p=dp, r=hz: av.set_play_rate_target(d, p, r))
                group.addAction(act)
            self._play_rate_groups.append(group)

    def show_keyboard_shortcuts(self) -> None:
        """Help ▸ Keyboard Shortcuts: the full keymap, grouped. Keep this in sync when
        a shortcut is added (the accelerators live in _build_menu / mode_controller /
        nav.install_jog_shortcuts). Ctrl is ⌘ on macOS."""
        import sys
        from html import escape
        mod = "⌘" if sys.platform == "darwin" else "Ctrl"
        groups = [
            ("Modes", [
                (f"{mod}1", "Bus Visualizer"),
                (f"{mod}2", "Timing Calculator"),
                (f"{mod}3", "Bus Analyzer"),
            ]),
            ("Cursor navigation (Analyzer)", [
                ("← / →", "Previous / next command (respects the command filter)"),
                (f"{mod}← / {mod}→", "Previous / next SSCR/DSCR commit"),
                (f"{mod}> / {mod}<", "Previous / next SSCR/DSCR commit (menu)"),
                (f"{mod}]", "Next bookmark"),
                (f"{mod}[", "Previous bookmark"),
                (f"{mod}B", "Toggle bookmark at cursor"),
            ]),
            ("Waveform panes (Audio / Raw Capture, when focused)", [
                (">", "Page the view one width right"),
                ("<", "Page the view one width left"),
            ]),
            ("Manual Stream Sync Point (Audio)", [
                (f"{mod}.", "Move SSP +1 row"),
                (f"{mod},", "Move SSP −1 row"),
            ]),
            ("Application", [
                (f"{mod}Q", "Quit"),
            ]),
        ]
        html = []
        for title, rows in groups:
            html.append(f"<p style='margin-bottom:2px;'><b>{title}</b></p>"
                        "<table cellpadding='2' style='margin-left:8px;'>")
            for keys, desc in rows:
                html.append(f"<tr><td><code>&nbsp;{escape(keys)}&nbsp;</code></td>"
                            f"<td>&nbsp;— {escape(desc)}</td></tr>")
            html.append("</table>")
        self._info_box("Keyboard Shortcuts", "".join(html))

    def _info_box(self, title: str, html: str) -> None:
        """A plain informational dialog with NO icon (the stock info/‘!’ glyph adds
        nothing to these reference popups). Read-only rich text."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.RichText)
        box.setText(html)
        box.exec()

    def show_timeline_legend(self) -> None:
        """Explain the timeline ribbon's colours/shapes (also surfaced on hover)."""
        from .timeline import MARK_LEGEND, _LC_SECTION_PALETTE_IDX, _lc_section_color
        rows = "".join(
            f"<tr><td><span style='color:{c.name()}; font-size:18px;'>&#9632;</span>"
            f"</td><td><b>&nbsp;{name}</b></td><td>&nbsp;— {meaning}</td></tr>"
            for name, c, meaning in MARK_LEGEND)
        # Link-control sub-phase colours (drawn as bands within the §5.1.2 bring-up).
        lc_meaning = {
            "Idle": "bus idle (DP low) before the sequence",
            "Bus Reset": "Cold-Start bus reset (LC:00 then LC:10)",
            "Cold Start": "reset recovery + PHY-number clocking",
            "Warm Start": "warm-start pulse (PHY reused)",
            "LC_Request": "peripheral-driven link-control request",
            "PhyStart": "hand-off to the selected audio PHY",
        }
        lc_rows = "".join(
            f"<tr><td><span style='color:{_lc_section_color(name).name()}; font-size:18px;'>"
            f"&#9632;</span></td><td><b>&nbsp;{name}</b></td><td>&nbsp;— {meaning}</td></tr>"
            for name, meaning in lc_meaning.items() if name in _LC_SECTION_PALETTE_IDX)
        self._info_box(
            "Timeline Marks",
            "<p>Each command is a tick on the timeline, coloured by kind. "
            "Tall ticks flag the notable events (CRC errors and SSP commits).</p>"
            f"<table cellpadding='2'>{rows}</table>"
            "<p>The translucent background <b>bands</b> mark bus-config segments, "
            "coloured by column count — so a reconfiguration (e.g. cold-start "
            "2col → 8col) is visible at a glance.</p>"
            "<p>The <b>§5.1.2 link bring-up</b> is drawn as distinct colour bands, "
            "one per sub-phase (the Cold-Start safe-lock band is labelled by the "
            "selected PHY, e.g. <i>Cold Start PHY2</i>):</p>"
            f"<table cellpadding='2'>{lc_rows}</table>")

    def _sync_dock_action(self, dock) -> None:
        act = self._dock_actions.get(dock)
        if act is None:
            return
        try:                                  # may fire during teardown
            act.blockSignals(True)
            act.setChecked(not dock.isHidden())
            act.blockSignals(False)
        except RuntimeError:
            pass

    def _show_dock(self, dock: QDockWidget) -> None:
        # Re-show a closed/tab-hidden dock and force it back to docked. A dock closed
        # via its X button (especially on macOS) can come back floating or detached,
        # so we ALWAYS drop floating and re-add it to its home dock area, then re-join
        # its tab group beside a still-docked groupmate so the group's tab bar isn't
        # lost. Re-adding an already-docked dock is harmless.
        dock.setFloating(False)
        home = self._dock_area.get(dock, Qt.BottomDockWidgetArea)
        self.addDockWidget(home, dock)
        dock.show()
        for group in self._tab_groups:
            if dock in group:
                anchor = next((d for d in group
                               if d is not dock and not d.isHidden()
                               and not d.isFloating()
                               and self.dockWidgetArea(d) != Qt.NoDockWidgetArea), None)
                if anchor is not None:
                    self.tabifyDockWidget(anchor, dock)
                break
        dock.raise_()

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        """Release audio playback deliberately on quit (stop the sink/source + sweep
        timer) instead of leaving it to GC ordering during teardown."""
        try:
            self._audio_view._hard_stop()
        except Exception:  # noqa: BLE001 - never block window close on teardown
            pass
        super().closeEvent(event)

    # ---- loading ----
    def load_demo(self) -> None:
        """The demo capture, with a §5.1.2 Cold Start spliced in front so the link
        comes up from Bus Reset → PHY-select → audio on the wire (the grid shows
        'No PHY Selected' until the PHY-select sequence is decoded). ~4 sine cycles
        per channel (the demo sine period is 64 samples)."""
        self.load_session(Session.from_demo(256, cold_start=True, register_map=self._rmap))

    def load_demo_bringup(self) -> None:
        """Alias for load_demo (kept for the app's --demo-bringup flag)."""
        self.load_demo()

    def _last_capture_dir(self) -> str:
        """The directory of the last-opened capture (persisted across launches), so
        the open dialog reopens where the user was working. Empty on first run."""
        return QSettings().value("capture/last_dir", "", type=str)

    def _remember_capture_dir(self, path: str) -> None:
        if path:
            QSettings().setValue("capture/last_dir", os.path.dirname(os.path.abspath(path)))

    def open_visualizer_config(self) -> None:
        """Analyzer ▸ Open Visualizer Config: pick a config (a CSV file or the current
        Visualizer authoring) and either impose it on the open capture (grid + registers
        from row 0) or compare it against the decode (Register Map overlay). Folds in the
        old Apply Config CSV, Compare ▸ CSV Config and Compare ▸ Visualizer."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        from .open_config_dialog import OpenVisualizerConfigDialog
        dlg = OpenVisualizerConfigDialog(self, has_authoring=True,
                                         start_dir=self._last_capture_dir())
        if not dlg.exec():
            return
        if dlg.use_authoring:
            # Authoring is in-memory and transient → compare only (read immediately).
            self.use_authoring_as_expected()
            return
        # File source: normalise (upgrade legacy/v2.0), then update and/or compare.
        from ..ingest import visualizer_csv
        try:
            cfg_path = visualizer_csv.normalized_path(dlg.path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Open Visualizer Config", f"Couldn't read that CSV: {exc}")
            return
        self._remember_capture_dir(dlg.path)
        if dlg.do_compare:
            self._run_compare(cfg_path)
        if dlg.do_update:
            self._apply_config_csv_path(dlg.path)

    def _apply_config_csv_path(self, csv: str) -> None:
        """Impose a Visualizer config CSV on the ALREADY-OPEN capture (no reopen): re-decode
        with it AND seed the bus grid + register map from it (Provenance.CSV — "CSV Import"),
        so a post-commit capture matches the CSV. Runs the re-decode on the worker thread
        (same path as the Scrambler override); the cursor and bookmarks are kept."""
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = self._session, self.cursor.sample, set(self._bookmarks)

        def apply(s=sess, p=csv):
            s.apply_config_csv(p)
            return s

        def done(_s, cur=keep_cursor, bms=keep_bms, p=csv):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText(f"Imposed {os.path.basename(p)} on the capture "
                                 f"(grid + register map from CSV; re-decoded).")

        self._load_async(apply, after=done)

    def open_capture(self, config_csv: str = "") -> None:
        """Open a capture: a Logic 2 `.sal` project, one digital `.csv`, a simulation
        `.vcd`, or BOTH per-channel `.bin` files together (multi-select) to skip the
        second-file prompt. `config_csv` (optional) supplies the data-port config to
        decode with from row 0 — normally empty from the menu; use Apply Config CSV to
        impose a config after opening. Opening switches to Analysis (via load_session)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open capture (.sal, .csv, .vcd, or both .bin channel files)",
            self._last_capture_dir(),
            filter="Captures (*.sal *.csv *.bin *.vcd);;Saleae project (*.sal);;"
                   "Digital CSV (*.csv);;Saleae binary (*.bin);;VCD (*.vcd);;All files (*)")
        if not paths:
            return
        self._remember_capture_dir(paths[0])
        try:
            bins = [p for p in paths if p.lower().endswith(".bin")]
            if len(bins) >= 2:
                self._open_bin(bins[0], bins[1], config_csv=config_csv)
                return
            path = paths[0]
            low = path.lower()
            if low.endswith(".sal"):
                self._open_sal(path, config_csv=config_csv)
            elif low.endswith(".csv"):
                self._open_csv(path, config_csv=config_csv)
            elif low.endswith(".vcd"):
                self._open_vcd(path, config_csv=config_csv)
            else:
                self._open_bin(path, config_csv=config_csv)
        except saleae_binary.UnsupportedSaleaeVersion as exc:
            QMessageBox.warning(self, "Unsupported format", str(exc))
        except Exception as exc:  # noqa: BLE001 - surface ingest/decode errors
            QMessageBox.critical(self, "Open failed", str(exc))

    def _pick_two_channels(self, title, options, labels):
        """Ask the user which entries are the clock and the data channel.
        `options` are the values; `labels` the display strings. Labels aren't
        guaranteed unique (VCD signal names can collide across scopes), so
        disambiguate the display strings and resolve the choice back by INDEX —
        never by label lookup, which would pick the first of a duplicate."""
        if len(options) < 2:
            raise ValueError("capture needs at least two digital channels")
        counts: dict = {}
        for lb in labels:
            counts[lb] = counts.get(lb, 0) + 1
        disp, nth = [], {}
        for lb in labels:
            if counts[lb] > 1:
                nth[lb] = nth.get(lb, 0) + 1
                disp.append(f"{lb}  #{nth[lb]}")
            else:
                disp.append(lb)
        clk, ok = QInputDialog.getItem(self, title, "Clock channel:", disp, 0, False)
        if not ok:
            return None
        ci = disp.index(clk)
        di = 1 if ci == 0 else 0
        dat, ok = QInputDialog.getItem(self, title, "Data channel:", disp, di, False)
        if not ok:
            return None
        return options[ci], options[disp.index(dat)]

    def _open_sal(self, path: str, config_csv: str = "") -> None:
        from ..ingest import saleae_sal
        info = saleae_sal.read_info(path)
        if len(info.channels) < 2:
            raise ValueError(f"{path}: fewer than two digital channels")
        labels = [f"Channel {c}" for c in info.channels]
        if len(info.channels) == 2:
            # Auto-pick the forwarded clock (the busier channel) — same as .bin/.csv.
            clk, dat, auto = info.channels[0], info.channels[1], True
        else:
            picked = self._pick_two_channels("Open .sal", info.channels, labels)
            if picked is None:
                return
            clk, dat, auto = picked[0], picked[1], False
        def after(sess, p=path):
            src = sess.source
            self._status.setText(
                f"Opened {p}  (clock=ch{src['clock_channel']}, data=ch{src['data_channel']}"
                f"{' auto' if src.get('auto_clock') else ''}, "
                f"{sess.sample_rate_hz/1e6:.3f} MHz)")
        self._load_async(
            lambda: Session.from_sal(path, clk, dat, auto_clock=auto,
                                     register_map=self._rmap, config_csv=config_csv),
            after=after)

    def _open_vcd(self, path: str, config_csv: str = "") -> None:
        """Import a simulation VCD: pick the clock + data scalar signals (auto when
        there are exactly two 1-bit signals), mapping $timescale ticks to samples."""
        from ..ingest import vcd
        info = vcd.read_info(path)
        sigs = info.scalar_signals
        if len(sigs) < 2:
            raise ValueError(f"{path}: fewer than two 1-bit signals to use as clock/data")
        idents = [s.ident for s in sigs]
        labels = [s.name for s in sigs]
        name_of = {s.ident: s.name for s in sigs}
        if len(sigs) == 2:
            # Auto-pick the forwarded clock (the busier signal) — same as .sal/.csv.
            clk, dat, auto = idents[0], idents[1], True
        else:
            picked = self._pick_two_channels("Open .vcd", idents, labels)
            if picked is None:
                return
            clk, dat, auto = picked[0], picked[1], False
        def after(sess, p=path, cn=name_of.get(clk, clk), dn=name_of.get(dat, dat), a=auto):
            self._status.setText(
                f"Opened {p}  (clock={cn}, data={dn}{' auto' if a else ''}, "
                f"{sess.sample_rate_hz / 1e6:.3f} MHz)")
        self._load_async(
            lambda: Session.from_vcd(path, clk, dat, auto_clock=auto,
                                     register_map=self._rmap, config_csv=config_csv),
            after=after)

    def _open_csv(self, path: str, config_csv: str = "") -> None:
        from ..ingest import digital_csv
        header = digital_csv.read_header(path)
        cols = list(range(1, len(header)))      # skip Time column 0
        if len(cols) < 2:
            raise ValueError(f"{path}: need >= 2 channel columns")
        labels = [header[i] for i in cols]
        # Auto-assign clock vs data by transition count (the forwarded clock
        # toggles every UI, so it has the most edges) — same heuristic as .bin.
        # Only fall back to the manual picker when the counts can't decide.
        counts = digital_csv.channel_transition_counts(path)
        clk = dat = None
        if len(counts) == len(cols) and counts:
            order = sorted(range(len(cols)), key=lambda i: counts[i], reverse=True)
            if counts[order[0]] > counts[order[1]]:           # a clear winner
                clk, dat = cols[order[0]], cols[order[1]]
        if clk is None:
            picked = self._pick_two_channels("Open digital CSV", cols, labels)
            if picked is None:
                return
            clk, dat = picked
        ci, di = cols.index(clk), cols.index(dat)
        note = (f"clock={labels[ci]} ({counts[ci]:,} edges), "
                f"data={labels[di]} ({counts[di]:,} edges)"
                if len(counts) == len(cols) else f"clock={labels[ci]}, data={labels[di]}")
        def after(sess, p=path, nt=note):
            self._status.setText(f"Opened {p}  ({nt}, {sess.sample_rate_hz/1e6:.3f} MHz)")
        self._load_async(
            lambda: Session.from_digital_csv(path, clk, dat, register_map=self._rmap,
                                             config_csv=config_csv),
            after=after)

    def _open_bin(self, path: str, data: str = "", config_csv: str = "") -> None:
        # Documented per-channel <SALEAE> export needs both channel files + a rate.
        # `data` may be supplied (both files chosen at once); else prompt for it.
        from ..ingest import saleae_binary
        if not data:
            data, _ = QFileDialog.getOpenFileName(
                self, "Second channel (Saleae binary)",
                filter="Saleae binary (*.bin);;All files (*)")
            if not data:
                return
        # The <SALEAE> binary stores transition times in seconds but no sample
        # rate; infer it from the finest timestamp spacing (~one sample period).
        # When inference is confident, use it directly (no modal — the rate only
        # ever needs changing if the decoded row/audio rates come out wrong);
        # only prompt when inference fails.
        guess = saleae_binary.infer_sample_rate([path, data])
        if guess:
            rate = guess
        else:
            rate, ok = QInputDialog.getInt(
                self, "Sample rate",
                "Capture sample rate (Hz):\n(couldn't auto-detect from the "
                "file's timestamps — please enter it)",
                98_304_000, 1, 2_000_000_000, 1_000_000)
            if not ok:
                return
        # Assign clock vs data by transition count. In FBCSE the forwarded clock
        # moves to DN for audio mode, so over a multi-second capture the audio clock
        # (DN) carries the most edges — which is the orientation the CDS/audio
        # decoder needs. (The §5.1.2 link-control clock is the OTHER line, DP; the
        # link-control decoder is self-orienting and finds the bring-up there.)
        na, nb = saleae_binary.transition_count(path), saleae_binary.transition_count(data)
        clock, dat = path, data
        if na >= 0 and nb >= 0 and nb > na:
            clock, dat = data, path
        def after(_session, c=clock, d=dat):
            self._status.setText(f"Clock = {os.path.basename(c)}, "
                                 f"Data = {os.path.basename(d)}")
        self._load_async(
            lambda: Session.from_saleae_binary(clock, dat, rate, register_map=self._rmap,
                                               config_csv=config_csv),
            after=after)

    # ---- workspace save / load ----
    def save_workspace(self) -> None:
        if self._session is None:
            QMessageBox.information(self, "No session", "Load a capture first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save workspace",
                                              "workspace.swi3s.json", "Workspace (*.json)")
        if not path:
            return
        ws = Workspace(
            source=self._session.source,
            overlay=[[int(s), int(d), int(a), int(v)]
                     for (s, d, a), v in self._session.register_overrides.items()],
            bookmarks=sorted(self._bookmarks),
            cursor=self.cursor.sample,
            mode=self._mode_mgr.current() or ANALYSIS,
            authoring=self._authoring.config().to_dict(),
            timing=self._timing_page.to_dict(),
            device_regmaps={str(d): pm.to_json_dict()
                            for d, pm in self._session.peripheral_maps.items()},
            device_names={str(d): n for d, n in self._session.device_names.items()},
            device_hub_depths={str(d): v for d, v in self._session.hub_depths.items() if v},
            device_scramblers=[[int(d), int(p), bool(on)]
                               for (d, p), on in self._session.scrambler_overrides.items()],
            view={"tx_map": self._tx_map, "tx_persist": self._tx_persist,
                  "grid_rows": self._grid_rows, "tx_start_row": self._tx_start_row,
                  "show_clock": self._raw_view.show_clock},
        )
        try:
            ws.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._status.setText(f"Workspace saved: {path}")

    def open_workspace(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open workspace", "", "Workspace (*.json)")
        if not path:
            return
        try:
            ws = Workspace.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        # Apply saved hub depths AND what-if register overrides inside the worker-
        # thread factory (both change the decode), in a single off-GUI-thread decode.
        # Doing it in apply_workspace (GUI thread) would re-decode again and freeze the UI.
        def factory(w=ws):
            sess = session_from_source(w.source, register_map=self._rmap)
            depths = ({int(d): int(v) for d, v in w.device_hub_depths.items()}
                      if w.device_hub_depths else None)
            scram = {}
            for row in (w.device_scramblers or []):
                try:                                  # [device, dp, on]
                    d, p, on = int(row[0]), int(row[1]), bool(row[2])
                except (IndexError, TypeError, ValueError):
                    continue
                scram[(d, p)] = on
            overs = {}
            for row in (w.overlay or []):
                try:                                  # [section, device, address, value]
                    sect, d, a, v = int(row[0]), int(row[1]), int(row[2]), int(row[3])
                except (IndexError, TypeError, ValueError):
                    continue
                overs[(sect, d, a)] = v & 0xFF
            # Stage every decode input (hub depths + scrambler overrides), then do a
            # SINGLE re-decode via the last setter, so reopening doesn't fan out into
            # several decodes. Register overrides re-decode last (they also stage).
            if depths is not None:
                sess.hub_depths = depths
            if scram:
                sess.scrambler_overrides = scram
            if overs:
                sess.set_register_overrides(overs)    # sets overrides + one re-decode
            elif scram:
                sess.set_scrambler_overrides(scram)   # one re-decode (picks up staged depths)
            elif depths is not None:
                sess.set_hub_depths(depths)
            return sess
        self._load_async(factory, after=lambda _session, w=ws: self.apply_workspace(w))

    def apply_workspace(self, ws: Workspace) -> None:
        """Apply view state (bookmarks, cursor) onto the loaded session."""
        self._bookmarks = set(ws.bookmarks)
        self._timeline.set_bookmarks(sorted(self._bookmarks))
        self.cursor.set_sample(ws.cursor)
        if self._session is not None:
            # Device names are UI metadata (cheap). Hub depths were already applied
            # in open_workspace's worker-thread factory and reflected by
            # load_session, so there's no re-decode here.
            if ws.device_names:
                self._session.device_names = {int(d): n for d, n in ws.device_names.items()}
                self._reg_view.set_device_names(self._session.device_names)
        if ws.device_regmaps and self._session is not None:
            from ..model.regmap_import import PeripheralRegisterMap
            for dev_str, doc in ws.device_regmaps.items():
                self._session.set_peripheral_map(int(dev_str),
                                                 PeripheralRegisterMap.from_json_dict(doc))
            self._reg_view.set_peripheral_maps(self._session.peripheral_maps)
            self._reg_view.set_files(self._session.register_files_at(self.cursor.sample))
        if ws.authoring:
            self._authoring.set_config(BusConfig.from_dict(ws.authoring))
        if ws.timing:
            self._timing_page.set_from_dict(ws.timing)
        self._apply_view_prefs(ws.view)
        if ws.mode in MODES:
            self._mode_mgr.switch_to(ws.mode)

    def _apply_view_prefs(self, view: dict) -> None:
        """Restore the transient Bus-Grid / Raw-Capture pane state from a workspace.
        Always establishes a DEFINITE state (defaults for missing keys) — open_workspace
        reuses the current window, so an early return would leak the previous capture's
        TX-map/clock state; a pre-v2 workspace (empty view) restores defaults. Drives the
        widgets so their handlers run, then forces one render so the result is correct
        regardless of which setters happened to change (robust to the reused-window
        case where a setChecked to the same value fires no signal)."""
        view = view or {}

        def _int(key, default):
            try:
                return int(view.get(key, default))
            except (TypeError, ValueError):     # tolerate a hand-edited/corrupt workspace
                return default

        self._grid_rows_edit.setText(str(_int("grid_rows", DEFAULT_GRID_ROWS)))
        self._on_grid_rows_edit()               # clamps 1..10240
        self._raw_view.set_show_clock(bool(view.get("show_clock", True)))
        tx_on = bool(view.get("tx_map", False))
        self._toggles_btn.setChecked(tx_on)
        # Persistence only matters while toggles are shown; force it off otherwise so a
        # disabled-but-checked button / stale _tx_persist can't be restored or re-saved.
        self._persist_btn.setChecked(tx_on and bool(view.get("tx_persist", False)))
        if tx_on:
            self._tx_scroll.setValue(_int("tx_start_row", 0))
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)

    def _load_async(self, factory, after=None) -> None:
        """Build a Session via `factory()` (the heavy decode + audio store) on a
        worker thread, showing an indeterminate busy dialog so the UI stays
        responsive. On success runs load_session + optional `after(session)` on the
        GUI thread; on failure shows the error. If a decode is already running, the
        LATEST request is queued and run when it finishes (so a fast re-decode — an
        SSP step, a toggle — isn't silently dropped)."""
        if getattr(self, "_load_thread", None) is not None:
            self._load_pending = (factory, after)   # latest wins; run on completion
            return
        self._load_pending = None
        self._load_after = after
        dlg = QProgressDialog("Decoding capture…", None, 0, 0, self)
        dlg.setWindowTitle("SWI3S Studio")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setCancelButton(None)            # the C++ decode can't be interrupted mid-run
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._load_dlg = dlg
        self._load_thread = QThread(self)
        self._load_worker = _LoadWorker(factory, pdm_dc_block=self._pdm_dc_block)
        self._load_worker.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._load_worker.run)
        # Cross-thread → queued delivery onto the GUI thread (self lives there).
        self._load_worker.done.connect(self._on_load_done)
        self._load_worker.failed.connect(self._on_load_failed)
        self._load_thread.start()
        dlg.show()

    def _finish_load(self) -> None:
        self._load_dlg.close()
        self._load_thread.quit()
        self._load_thread.wait()
        self._load_worker.deleteLater()
        self._load_dlg = None
        self._load_thread = None
        self._load_worker = None

    def _run_pending_load(self) -> None:
        """Start the most recent request that arrived while a decode was in flight."""
        pending = getattr(self, "_load_pending", None)
        if pending is not None:
            self._load_pending = None
            self._load_async(*pending)

    def _on_load_done(self, session, store, extras) -> None:
        after = self._load_after
        self._finish_load()
        # load_session (+ the optional after) runs on the GUI thread now that the
        # worker is joined. Guard it: an exception here would otherwise escape the
        # queued slot as an unhandled GUI-thread crash, and the busy dialog is
        # already closed. Surface it like the decode-failure path does.
        try:
            self.load_session(session, store, extras=extras)
            if after is not None:
                after(session)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.critical(self, "Load failed",
                                 f"The capture decoded but the views failed to load: {exc}")
        self._run_pending_load()

    def _on_load_failed(self, message: str) -> None:
        self._finish_load()
        QMessageBox.critical(self, "Open failed", message)
        self._run_pending_load()

    def load_session(self, session: Session, audio_store=None, extras=None,
                     switch_mode: bool = True) -> None:
        # `extras` (from the load worker) carries values precomputed off the GUI thread
        # so this method doesn't recompute them here and freeze the UI. None → compute
        # lazily (the synchronous demo path).
        extras = extras or {}
        self._session = session
        # Command table shows the decoded commands PLUS synthetic 'Commit Point' rows at
        # each confirmed sync-point commit's SSP (where it takes effect) — sorted in with
        # the commands. PROTOTYPE: these extra rows are table-only (not in session.commands),
        # so the timeline ticks, error counts and audio are unchanged. _starts/_kinds are
        # built from this augmented list so cursor-selection + filtering stay aligned.
        table_cmds = sorted(list(session.commands) + session.commit_point_rows(),
                            key=lambda c: int(c.get("start_sample", 0)))
        self._cmd_model = CommandTableModel(table_cmds, self._rmap,
                                            session.sample_rate_hz,
                                            peripheral_maps=session.peripheral_maps,
                                            row_origin=session.row_origin)
        self._cmd_proxy.setSourceModel(self._cmd_model)
        self._cmd_view.fit_columns()
        self._starts = [c.get("start_sample", 0) for c in table_cmds]

        kinds = sorted({c.get("command", "") for c in table_cmds} - {""})
        self._kind_menu._fill([(k, k) for k in kinds])   # repopulate for this capture
        self._clear_all_filters()                        # don't carry a prior capture's filters
        self._update_cmd_title()

        # Initialize the register view and bus grid AS-OF the cursor's start (t=0),
        # not the whole-capture end state — so the far-left cursor shows the bus
        # before any configuration (defaults / empty grid), and, when a §5.1.2
        # bring-up is present, "No PHY Selected" until the PHY-select is decoded.
        # Both views then fill in as the cursor advances past the config commands.
        self._reg_view.set_files(session.register_files_at(0))
        self._reg_view.set_peripheral_maps(session.peripheral_maps)
        self._reg_view.set_device_names(session.device_names)
        self._reg_view.set_phy(session.link_control.phy_name)
        # Keep the TX-map scroll position across a re-decode (SSP step, override, CSV):
        # _update_tx_scrollbar clamps it into the new capture's range rather than
        # snapping back to row 0 on every re-decode. (A genuinely new/shorter capture
        # just clamps to a valid row.)
        self._update_tx_scrollbar()
        self._apply_grid_for_sample(0)
        self._audio_store = (audio_store if audio_store is not None
                             else session.audio_store(pdm_dc_block=self._pdm_dc_block))
        # Audio waveforms are plotted in CAPTURE-sample space so they sit at their true
        # offset and line up under the timeline — give the view the capture rate + full
        # extent (commands + audio) BEFORE set_store (which rebuilds the plots).
        capture_total = max([c.get("end_sample", 0) for c in session.commands]
                            + [session.audio_end_sample, 1])
        self._audio_view.set_capture_context(session.sample_rate_hz, capture_total)
        self._audio_view.set_store(self._audio_store)
        self._export_action.setEnabled(not self._audio_store.is_empty())
        self._build_decimation_menu()            # per-dataport decimation for this capture's streams
        self._symbol_view.set_sample_rate(session.sample_rate_hz)
        self._symbol_view.set_row_origin(session.row_origin)
        self._symbol_view.set_symbols(session.symbols())
        self._raw_view.set_capture(session.capture,
                                   dp_is_data_line=session.link_control.lc_on_data_line)
        # Nominal samples per bus row (row rate → samples), for the zoom-to-row button.
        _row_hz = (session.row_rate_khz or 0.0) * 1000.0
        self._raw_view.set_row_samples(session.sample_rate_hz / _row_hz if _row_hz else 0.0)
        # Samples per UI (max-zoom floor: don't let the user zoom in past ~1 UI).
        self._raw_view.set_ui_samples(
            session.sample_rate_hz / session.ui_rate_hz if session.ui_rate_hz else 0.0)
        self._raw_view.set_cds_provider(session.cds_column_samples)
        self._raw_view.set_commit_provider(session.commit_column_samples)
        _store = self._audio_store
        _lanes = [(d, p, ch) for (d, p) in _store.streams() for ch in _store.channels(d, p)]
        self._raw_view.set_port_marks(session.port_bit_marks, _lanes)
        self._sample_view.set_sample_rate(session.sample_rate_hz)
        self._sample_view.set_row_origin(session.row_origin)
        self._sample_view.set_lanes(_lanes)
        self._sample_win = None            # (first,last) start_sample shown; None = needs (re)build
        self._eye_view.set_analysis_context(session.segments, session.timing_regions(),
                                            session.timing_column_roles)
        self._eye_view.set_capture(session.capture)
        self._eye_view.set_sections(extras.get("sections")
                                    if extras.get("sections") is not None
                                    else session.section_ui_stats())   # per-section min/max UI
        self._timeline.set_sample_rate(session.sample_rate_hz)
        self._timeline.set_row_origin(session.row_origin)
        self._meas_view.set_rows(
            list(capture_measurements(session, store=self._audio_store))
            + self._meas_view.link_timing_rows(session.link_control))
        self._bookmarks.clear()
        self._timeline.set_bookmarks([])
        # Remind the user which capture is loaded — but only in Analyzer mode (see
        # _update_title): the capture is irrelevant to the Visualizer/Timing views.
        self._update_title()

        total = max([c.get("end_sample", 0) for c in session.commands]
                    + [session.audio_end_sample, 1])
        self._timeline.set_events(session.commands, total)
        self._timeline.set_segments(session.segments, total)
        self._timeline.set_bringup(session.link_control)
        self._timeline.set_cursor(0)
        self._update_nav_starts()               # arrow-nav starts (all visible at load)
        self._timeline.set_commit_starts(self._sscr_samples())   # ⌘+arrow commit jump
        self._timeline.set_commit_points(session.commit_point_samples())   # dotted SSP markers

        n_err = count_errors(session.commands)
        lc = session.link_control
        phy = f" · {lc.label()}" if session.has_bringup else ""
        self._status.setText(
            f"{len(session.commands)} commands ({n_err} errors) · "
            f"{session.audio_count} audio samples · {session.column_count} columns · "
            f"clock {session.ui_rate_hz/2e6:.3f} MHz · row rate {session.row_rate_khz:.1f} kHz"
            f"{phy}"
        )

        # A capture is opened to be analysed, so land in Analysis mode where the
        # decoded, cursor-gated bus grid lives (Visualization shows the authored config,
        # which is unrelated to the capture). Opening a capture from any mode switches
        # here — but the startup demo preload passes switch_mode=False so the app still
        # opens in the Bus Visualizer default (the demo is just ready in the background).
        if switch_mode and self._mode_mgr.current() != ANALYSIS:
            self._mode_mgr.switch_to(ANALYSIS)

    def export_audio(self) -> None:
        if self._session is None or self._audio_store is None or self._audio_store.is_empty():
            return
        from .audio_export_dialog import AudioExportDialog
        dlg = AudioExportDialog(self._audio_store,
                                visible_index_range=self._audio_view.visible_index_range(),
                                default_rates=self._audio_view.play_rate_targets,
                                parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        dps = dlg.selected_streams()
        if not dps:
            QMessageBox.information(self, "Export audio", "No streams selected.")
            return
        directory = QFileDialog.getExistingDirectory(self, "Export audio WAV files to…")
        if not directory:
            return
        rng = dlg.index_range()
        try:
            paths = []
            notes = []
            for dev, dp in dps:
                rate = dlg.target_rate(dev, dp)
                path = os.path.join(directory, f"swi3s_dev{dev}_dp{dp}.wav")
                paths.append(self._audio_store.export_wav(dev, dp, path, index_range=rng,
                                                          target_rate=rate))
                if rate:
                    notes.append(f"dev{dev}/dp{dp} → {rate/1000:g} kHz")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        note = ("\n\nResampled: " + ", ".join(notes)) if notes else ""
        QMessageBox.information(self, "Audio exported", "Wrote:\n" + "\n".join(paths) + note)

    def export_commands(self) -> None:
        if self._session is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export commands CSV",
                                              "commands.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            write_commands_csv(self._session.commands, path, self._rmap)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._status.setText(f"Commands exported: {path}")

    def _toggle_viz_maximize(self) -> None:
        """Collapse the authoring controls to give the grid the whole page, or
        restore the previous split (the Visualizer's 'Maximize Frame'). When
        maximized, show the DP colour key (room for it) + the floating restore
        button; when restored, hide both."""
        sizes = self._viz_split.sizes()
        if sizes and sizes[0] > 0:
            self._viz_prev_sizes = sizes
            self._viz_split.setSizes([0, sum(sizes)])
            maximized = True
        else:
            self._viz_split.setSizes(getattr(self, "_viz_prev_sizes", None)
                                     or [self._viz_split.height() // 2] * 2)
            maximized = False
        self._viz_grid.set_show_key(maximized)
        self._viz_restore_btn.setVisible(maximized)
        if maximized:
            self._position_restore_btn()
            self._viz_restore_btn.raise_()
        self._refresh_authored_grid()

    def _position_restore_btn(self) -> None:
        """Pin the floating Show-Parameters button to the grid's top-right corner."""
        b = self._viz_restore_btn
        b.adjustSize()
        b.move(max(8, self._viz_grid.width() - b.width() - 12), 10)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        if obj is self._viz_grid and event.type() == QEvent.Resize \
                and self._viz_restore_btn.isVisible():
            self._position_restore_btn()
        # Wheel over the Bus Grid scrolls the TX-map window (row-at-a-time nav across
        # the whole capture) instead of the tiny native scene — only in the scrollable
        # mode (TX on, persistence off; a persistence slice is fixed).
        if self._tx_map and not self._tx_persist and obj is self._grid_view.viewport() \
                and event.type() == QEvent.Wheel:
            # macOS trackpads report pixelDelta (angleDelta is 0), mouse wheels report
            # angleDelta in 1/8-degree steps — honour whichever is non-zero.
            dy = event.angleDelta().y() or event.pixelDelta().y()
            if dy:
                rows = max(1, abs(dy) // 40)          # a few rows per notch/gesture
                self._tx_scroll.setValue(self._tx_scroll.value() + (-rows if dy > 0 else rows))
                return True
        return super().eventFilter(obj, event)

    def _active_grid(self):
        """The grid for the current mode: the embedded authoring grid in
        Visualization, the shared decoded grid otherwise."""
        return self._viz_grid if self._mode_mgr.current() == VISUALIZATION else self._grid_view

    def export_grid(self, grid=None) -> None:
        # Works for the decoded grid (Analysis) and the authored grid (Visualization).
        # `grid` picks one explicitly (the File submenus target their mode's grid
        # regardless of the current mode); None = whichever grid is active.
        if grid is None or isinstance(grid, bool):        # bool = QAction 'triggered' arg
            grid = self._active_grid()
        if grid.item_count == 0:
            QMessageBox.information(self, "Export grid",
                                    "Nothing to export — load a capture or author a config.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export bus grid",
                                              "bus_grid.svg", "SVG (*.svg);;PNG (*.png)")
        if not path:
            return
        # Always include the DP colour key in the exported image, even when it's
        # hidden on screen (Visualizer grid). Force it on, re-render, then restore.
        restore = None
        if grid is self._viz_grid and not grid.show_key:
            grid.set_show_key(True)
            self._refresh_authored_grid()
            restore = False
        try:
            grid.export_image(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        finally:
            if restore is not None:
                grid.set_show_key(restore)
                self._refresh_authored_grid()
        self._status.setText(f"Bus grid exported: {path}")

    def export_grid_csv(self) -> None:
        """Export the analyzer's decoded bus grid as a Visualizer settings CSV, so a
        captured configuration can be re-opened/edited in Visualization mode (or the
        standalone Visualizer)."""
        if self._session is None:
            QMessageBox.information(self, "Export settings", "Load a capture first.")
            return
        cfg, n_enabled = BusConfig.from_decoder_config(self._session.decoder.config_dataports())
        if n_enabled == 0:
            QMessageBox.information(self, "Export settings",
                                    "No data ports were decoded in this capture.")
            return
        src = self._session.source.get("path") or self._session.source.get("type", "capture")
        cfg.description = f"Exported from analyzer bus grid ({os.path.basename(str(src))})"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bus grid as Visualizer settings", "bus_grid_settings.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            cfg.to_csv_file(path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        note = f"Exported {min(n_enabled, 12)} data port(s) to {path}"
        if n_enabled > 12:
            note += f" (capped at 12 of {n_enabled} visualizer columns)"
        self._status.setText(note)

    # ---- synchronized navigation ----
    def _on_register_edited(self, device: int, address: int, value: int) -> None:
        """Debug what-if: force a register value for the config section under the
        cursor, then re-decode so the Bus Grid, Register Map AND audio all reflect
        it. The re-decode runs on the worker thread (like the Scrambler override),
        since audio must be re-derived."""
        if self._session is None:
            return
        sect = self._session.segment_index_for_sample(self.cursor.sample)
        d, a, v = int(device), int(address), int(value) & 0xFF
        # No-op guard: if the forced value already equals what the register shows
        # as-of the cursor (and isn't changing an existing override), don't kick off a
        # full re-decode — the user cancelled with the same value or re-entered it.
        files = self._session.register_files_at(self.cursor.sample)
        f = files.get(d)
        if f is not None and f.value(a) == v and \
                self._session.register_overrides.get((sect, d, a)) in (None, v):
            self._status.setText(f"Dev{d} 0x{a:04X} already 0x{v:02X} — no change.")
            return
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = self._session, self.cursor.sample, set(self._bookmarks)

        def apply(s=sess):
            s.set_register_override(sect, d, a, v)
            return s

        def done(_s, cur=keep_cursor, bms=keep_bms):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText(
                f"Manual edit: section {sect} · Dev{d} 0x{a:04X} = 0x{v:02X} "
                f"(what-if — grid/registers/audio; re-decoded)")

        self._load_async(apply, after=done)

    def _restore_cursor(self, sample: int) -> None:
        """Restore the cursor to `sample` after a re-decode and force every pane to
        re-sync there. load_session re-inits the panes at t=0, and TimeCursor.set_sample
        no-ops when the value is unchanged (it usually is, since a what-if edit doesn't
        move the cursor) — so without an explicit refresh the panes would be left
        showing the capture start even though the cursor never moved."""
        self.cursor.set_sample(int(sample))
        self._on_cursor(int(sample))

    def _on_timing_jump(self, sample: int) -> None:
        """Timing pane asked to visit a tight-margin transition: move the shared cursor
        there and surface it in the Raw Capture pane, zoomed to a couple of bus rows and
        centered on the event so its clock edge sits in the middle of the pane."""
        self._on_seek(int(sample))
        self._raw_dock.show()
        self._raw_dock.raise_()
        self._raw_view.center_on_sample(int(sample), 2)

    def _on_register_overrides_cleared(self) -> None:
        if self._session is None or not self._session.register_overrides:
            self._status.setText("No manual register edits.")
            return
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = self._session, self.cursor.sample, set(self._bookmarks)

        def clear(s=sess):
            s.clear_register_overrides()
            return s

        def done(_s, cur=keep_cursor, bms=keep_bms):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText("Manual register edits cleared; re-decoded.")

        self._load_async(clear, after=done)

    def _on_command_selected(self, *_args) -> None:
        if self._syncing or self._session is None:
            return
        rows = self._cmd_view.selectionModel().selectedRows()
        if not rows:
            return
        src = self._cmd_proxy.mapToSource(rows[0])
        cmd = self._cmd_model.command_at(src.row())
        if cmd:
            self._syncing = True
            # A commit jumps the cursor to where it takes effect (its SSP) so the
            # Bus Grid / Register Map show the post-commit state; other commands use
            # their start.
            try:
                self.cursor.set_sample(self._session.command_cursor_sample(cmd))
            finally:
                self._syncing = False          # never leave sync wedged on an error

    # ---- bookmarks ----
    def toggle_bookmark(self) -> None:
        if self._session is None:
            return
        s = self.cursor.sample
        if s in self._bookmarks:
            self._bookmarks.discard(s)
        else:
            self._bookmarks.add(s)
        self._timeline.set_bookmarks(sorted(self._bookmarks))
        self._status.setText(f"{len(self._bookmarks)} bookmark(s)")

    def next_bookmark(self) -> None:
        if not self._bookmarks:
            return
        bms = sorted(self._bookmarks)
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in bms if s > cur), bms[0]))

    def prev_bookmark(self) -> None:
        if not self._bookmarks:
            return
        bms = sorted(self._bookmarks, reverse=True)
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in bms if s < cur), bms[0]))

    def clear_bookmarks(self) -> None:
        self._bookmarks.clear()
        self._timeline.set_bookmarks([])

    def _sscr_samples(self) -> list:
        """Start samples of the sync-point commit commands (SSCR / DSCR), ascending —
        the config commits at these, so they're the natural jump targets."""
        if self._session is None:
            return []
        return sorted(int(c.get("start_sample", 0)) for c in self._session.commands
                      if c.get("command") in ("SSCR", "DSCR"))

    def next_sscr(self) -> None:
        pts = self._sscr_samples()
        if not pts:
            self._status.setText("No SSCR/DSCR commit in this capture")
            return
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in pts if s > cur), pts[-1]))

    def prev_sscr(self) -> None:
        pts = self._sscr_samples()
        if not pts:
            self._status.setText("No SSCR/DSCR commit in this capture")
            return
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in reversed(pts) if s < cur), pts[0]))

    def _on_seek(self, sample: int) -> None:
        self.cursor.set_sample(sample)

    def _on_playback_stopped(self) -> None:
        """Playback stopped: the heavy panes (registers, grid, symbols) were throttled
        while playing, so refresh them once at the resting cursor. is_playing is now
        False, so _on_cursor takes its full (un-throttled) path."""
        if self._session is not None:
            self._on_cursor(self.cursor.sample)

    def _on_cursor(self, sample: int) -> None:
        if self._session is None:
            return
        # Cheap, every tick: move the playhead LINES so the cursor stays smooth.
        # These just reposition existing lines (no re-render), so they're safe to
        # run at the playback tick rate.
        self._timeline.set_cursor(sample)
        self._audio_view.set_cursor(sample)      # move the cursor line on the waveforms
        # During playback do NO heavy main-thread work. Re-rendering the raw-capture
        # envelope (auto-pan when zoomed), replaying the register files, the windowed
        # symbol re-decode and the grid rebuild are all heavy Python that holds the
        # GIL and starves the (Python) audio pull-source on its thread → glitchy audio
        # (and the raw view scrolling mid-play). Defer them all to one refresh at the
        # resting cursor on stop / clip end (_on_playback_stopped → _on_cursor).
        if self._audio_view.is_playing:
            return
        self._raw_view.set_cursor(sample)
        self._reg_view.set_files(self._session.register_files_at(sample))
        if not self._syncing:
            self._select_command_for_sample(sample)
        # Symbol viewer follows the cursor via windowed (seeked) re-decode, so it
        # works on big captures without scanning from the start. Skip if hidden, and
        # skip when the cursor change *came from* the symbol pane (a click / arrow
        # key) — re-windowing there would snap the selection back to the command's
        # comma, trapping navigation on the first symbols.
        if not self._symbol_dock.isHidden() and not self._from_symbol:
            self._refresh_symbols_around(sample)
        if self._sample_dock.isVisible() and not self._from_sample:
            self._refresh_samples_around(sample)
        # If what-if edits are active, keep the projected grid; else show the grid
        # as-of the cursor — it fills in as registers are written, and for a true
        # multi-width reconfiguration (cold-start 2col -> 8col) the per-segment
        # geometry follows the cursor. set_cells reads each cell's own column, so
        # the decoder's final column_count is the right axis width to pass.
        self._apply_grid_for_sample(sample)

    def _apply_grid_for_sample(self, sample: int) -> None:
        """Render the bus grid for the time cursor. Only a COLD start has the §5.1.2
        PHY-select on the wire, so while the cursor sits in that bring-up (before audio
        mode) there is no bus geometry yet — show 'No PHY Selected' until it's decoded.
        A warm start reused the PHY off-capture (nothing to wait for) and a mid-stream
        capture has no bring-up, so both show the grid from row 0. Past the bring-up the
        grid fills in as registers are written (per-segment geometry follows a
        multi-width reconfiguration)."""
        s = self._session
        if self._tx_map:                       # TX map replaces the layout with a raster
            if self._tx_persist:
                # Persistence: "did this column EVER toggle" within the CONFIGURATION
                # REGION under the cursor, shown as a fixed N-row summary (every row
                # identical). Scans the whole region (not just the first Rows-To-Draw
                # window) so a column that only transports deeper still lights up;
                # scoped to one region so distinct geometries (e.g. 2col -> 16col)
                # don't overlay.
                lit, cols = s.tx_persist_columns(sample)
                n = max(1, self._grid_rows)
                raster = {"column_count": cols, "cds_col": 0,
                          "row_labels": list(range(n)),
                          "tx": np.broadcast_to(lit, (n, cols))}
            else:
                # Whole-capture, scrollable where-is-data view: render the Rows-To-Draw
                # window at the scrolled top row (0-based), independent of the cursor.
                raster = s.tx_raster(self._tx_start_row, self._grid_rows)
            self._grid_view.set_tx_raster(raster)
            return
        # Only gate on a COLD start (PHY-select actually decoded mid-capture). A warm
        # start / mid-stream capture is already configured from row 0 → show the grid.
        if s.link_control.sequence == "cold" and sample < s.audio_start_sample:
            lc = s.link_control
            self._grid_view.show_message(
                "No PHY Selected",
                f"Link bring-up in progress — {lc.label()} "
                f"(PHY-select decoded at the Cold Start, §5.1.2)")
            return
        self._grid_view.set_cells(s.grid_cells_at(sample, self._grid_rows),
                                  s.column_count_at(sample))

    def _toggle_tx_map(self, on: bool) -> None:
        """Switch the Bus Grid between the config layout and the TX-map raster (the
        'Show/Hide Toggles' button), then re-render. Persistence is only meaningful
        while toggles are shown; the scrollbar navigates the whole capture in TX mode.
        See GridView.set_tx_raster / Session.tx_raster."""
        self._tx_map = bool(on)
        self._toggles_btn.setText("Hide Toggles" if on else "Show Toggles")
        self._persist_btn.setEnabled(on)
        # Open the TX map at the CURSOR's row (not always row 0) so it starts where the
        # user is looking — and, since the row is in tx_raster's own geometry, at the
        # segment under the cursor. _update_tx_scrollbar syncs the bar to this.
        if on and self._session is not None:
            self._tx_start_row = self._session.tx_row_for_sample(self.cursor.sample)
        # Persistence is a sub-mode of the TX map; clear it when toggles turn off so the
        # button can't linger disabled-but-checked and a stale _tx_persist can't be saved.
        if not on and self._persist_btn.isChecked():
            self._persist_btn.setChecked(False)   # fires _toggle_tx_persist(False)
        self._update_tx_scrollbar()               # shows/hides the bar for the mode
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)

    def _toggle_tx_persist(self, on: bool) -> None:
        """Toggle TX-map persistence (the 'Persistence On/Off' button). Persistence is a
        fixed single-slice summary, so it hides the outer scrollbar (no scrolling)."""
        self._tx_persist = bool(on)
        self._persist_btn.setText("Persistence Off" if on else "Persistence On")
        self._update_tx_scrollbar()            # shows/hides the bar for the new mode
        if self._session is not None and self._tx_map:
            self._apply_grid_for_sample(self.cursor.sample)

    def _update_tx_scrollbar(self) -> None:
        """Size the TX-map scrollbar to the capture: range [0, total_rows - window],
        one page = the Rows-To-Draw window. Clamps the current top row into range. The
        bar is shown only in the scrollable mode — TX map ON and persistence OFF (a
        persistence slice is fixed, and the config layout doesn't use it)."""
        self._tx_scroll.setVisible(self._tx_map and not self._tx_persist)
        total = self._session.tx_total_rows() if self._session is not None else 0
        span = max(0, total - self._grid_rows)
        self._tx_scroll.blockSignals(True)
        self._tx_scroll.setRange(0, span)
        self._tx_scroll.setPageStep(max(1, self._grid_rows))
        self._tx_scroll.setSingleStep(1)
        self._tx_start_row = min(self._tx_start_row, span)
        self._tx_scroll.setValue(self._tx_start_row)
        self._tx_scroll.blockSignals(False)

    def _on_tx_scroll(self, value: int) -> None:
        """Scroll the TX-map window to top row `value` (0-based) and re-render."""
        self._tx_start_row = max(0, int(value))
        if self._session is not None and self._tx_map:
            self._apply_grid_for_sample(self.cursor.sample)

    def _on_grid_rows_edit(self) -> None:
        """Apply the Rows-To-Draw box (Bus Grid raster + config-grid height)."""
        try:
            rows = int(self._grid_rows_edit.text())
        except (TypeError, ValueError):
            self._grid_rows_edit.setText(str(self._grid_rows))
            return
        rows = max(1, min(10240, rows))
        self._grid_rows_edit.setText(str(rows))
        if rows == self._grid_rows:
            return
        self._grid_rows = rows
        self._update_tx_scrollbar()          # window (page) size changed
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)

    # ---- manual Stream Sync Point (SSP) ----
    def set_ssp_at_cursor(self) -> None:
        """Set the manual SSP to the bus row under the time cursor, then re-decode."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Load a capture first.")
            return
        self.set_ssp_row_async(self._session.bus_row_for_sample(self.cursor.sample))

    def step_ssp_row(self, delta: int) -> None:
        """Nudge the manual SSP row by `delta` (cycle phases by ear). If none is set
        yet, start from the cursor's bus row."""
        if self._session is None:
            return
        base = self._session.ssp_row
        if base < 0:
            base = self._session.bus_row_for_sample(self.cursor.sample)
        self.set_ssp_row_async(max(0, base + int(delta)))

    def set_ssp_row_async(self, row: int) -> None:
        """Re-decode with the manual SSP at bus `row` (-1 = auto) on the worker thread,
        keeping the cursor + bookmarks — same path as the Scrambler override."""
        if self._session is None:
            return
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = self._session, self.cursor.sample, set(self._bookmarks)
        # Hold the audio view across the re-decode: the streams are unchanged (same
        # bits, re-phased), so keep the shown tracks and the zoom fixed on the same
        # spot to compare sync as the phase steps — set_store would otherwise re-show
        # every dataport and refit to full-scale.
        keep_sel = self._audio_view.channel_selection()
        keep_zoom = self._audio_view.visible_index_range()

        def apply(s=sess, r=int(row)):
            s.set_ssp_row(r)
            return s

        def done(_s, cur=keep_cursor, bms=keep_bms, sel=keep_sel, zoom=keep_zoom, r=int(row)):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._audio_view.set_channel_selection(sel)   # keep hidden tracks hidden
            self._audio_view.set_visible_index_range(zoom)  # then hold the zoom
            # Honest status: the decoder only arms the manual SSP when the payload
            # engine is enabled at decode start (a config CSV supplied the geometry and
            # audio decoded). On a snoop/no-audio capture it's a no-op — say so rather
            # than claiming a clean re-decode. Row shown 0-based (decoder keeps internal).
            sess = self._session
            if r < 0:
                msg = "Manual SSP cleared (auto anchor); re-decoded."
            elif sess is not None and not sess.ssp_effective:
                msg = (f"SSP set to row {sess.display_row(r)}, but it only re-phases audio "
                       f"when a config CSV is applied (File ▸ Apply Config CSV) — "
                       f"no change to this capture.")
            else:
                disp = sess.display_row(r) if sess is not None else r
                msg = f"SSP anchored at bus row {disp}; re-decoded — listen for a clean decode."
            self._status.setText(msg)

        self._load_async(apply, after=done)

    def _refresh_symbols_around(self, sample: int) -> None:
        if self._session is None:
            return
        syms = self._session.symbols_around(sample)
        if not syms:                                  # keep the prior view if empty
            return
        self._symbol_view.set_symbols(syms)
        # Select the command's beginning CDS symbol (its SPM comma) and bring it to
        # the TOP of the pane — the comma's bus row equals the command's bus_row.
        target_row = None
        if self._starts:
            idx = max(0, bisect.bisect_right(self._starts, sample) - 1)
            if idx < len(self._session.commands):
                target_row = int(self._session.commands[idx].get("bus_row", 0))
        self._symbol_view.select_bus_row(target_row)

    def _extend_symbols_above(self, earliest_sample: int) -> None:
        """Decode-on-scroll: the symbol pane scrolled to its top, so decode the chunk
        of CDS symbols just before its earliest row and prepend them — letting the
        user scroll back through history (across config sections) without re-decoding
        the whole capture."""
        if self._session is None:
            self._symbol_view.prepend_symbols([])
            return
        self._symbol_view.prepend_symbols(self._session.symbols_before(int(earliest_sample)))

    def _on_symbol_selected(self, sample: int) -> None:
        """A CDS symbol row was clicked: move the shared cursor there and select
        the nearest command. Guarded so the cursor/symbol/command updates don't
        bounce back through each other, and so the symbol pane isn't re-windowed
        (which would snap the selection back to the command's comma)."""
        if self._syncing or self._session is None:
            return
        self._syncing = True
        self._from_symbol = True
        try:
            self.cursor.set_sample(int(sample))   # updates timeline / registers / grid
            self._select_command_for_sample(int(sample))
        finally:
            self._syncing = False
            self._from_symbol = False

    def _on_sample_selected(self, sample: int) -> None:
        """A Decoded-Samples row was clicked: move the shared cursor to that
        sample's MSB and select the nearest command. Guarded against re-entrancy
        and against re-windowing the pane out from under the click."""
        if self._syncing or self._session is None:
            return
        self._syncing = True
        self._from_sample = True
        try:
            self.cursor.set_sample(int(sample))
            self._select_command_for_sample(int(sample))
        finally:
            self._syncing = False
            self._from_sample = False

    def _refresh_samples_around(self, sample: int, force: bool = False) -> None:
        """Window the Decoded-Samples table around the cursor and select the sample
        at/just before it. To keep navigation cheap the heavy table rebuild is
        skipped while the cursor stays inside the currently shown window (with a
        margin) — then only the row selection moves. `force` rebuilds regardless
        (used on first show and on filter changes)."""
        if self._session is None:
            return
        span = self._sample_view.shown_span()
        win = getattr(self, "_sample_win", None)
        if (not force and win is not None and span is not None
                and win[0] <= int(sample) <= win[1]):
            self._sample_view.select_sample(int(sample))   # cheap: just move selection
            return
        ports, chans, pred = self._sample_view.filters()
        rows = self._session.samples_around(int(sample), ports=ports,
                                            channels=chans, value_pred=pred)
        if not rows:
            self._sample_view.set_samples([])
            self._sample_win = None
            return
        self._sample_view.set_samples(rows)
        self._sample_view.select_sample(int(sample))
        # Only skip a rebuild in the inner half of the shown window, so we refresh
        # well before the cursor reaches the (edge) rows the window can't extend past.
        first, last = int(rows[0]["start_sample"]), int(rows[-1]["start_sample"])
        margin = (last - first) // 4
        self._sample_win = (first + margin, last - margin)

    def _on_sample_filters_changed(self) -> None:
        """Port / value filter changed: rebuild the window at the current cursor."""
        if self._session is not None and self._sample_dock.isVisible():
            self._refresh_samples_around(self.cursor.sample, force=True)

    def _on_sample_dock_visible(self, visible: bool) -> None:
        """Populate the (windowed) sample table when its tab is first shown/raised."""
        if visible and self._session is not None:
            self._refresh_samples_around(self.cursor.sample, force=True)

    def _ensure_bit_samples(self) -> None:
        """Lazily collect per-data-bit sample points (for the Raw Capture "Port
        Samples" overlay) the first time the user enables it — a one-time re-decode
        on the worker thread, so a normal capture load isn't burdened with it."""
        if self._session is None or self._session.has_bit_samples:
            return
        self._audio_view.stop()
        sess, cur = self._session, self.cursor.sample
        bms = set(self._bookmarks)

        def collect(s=sess):
            s.set_collect_bit_samples(True)
            return s

        def done(_s, c=cur, b=bms):
            self._bookmarks = set(b)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._raw_view.set_port_marks(self._session.port_bit_marks,
                                          [(d, p, ch) for (d, p) in self._audio_store.streams()
                                           for ch in self._audio_store.channels(d, p)])
            self._restore_cursor(c)
            self._status.setText("Per-bit sample points collected.")

        self._load_async(collect, after=done)

    # ---- per-dataport scrambler override ----
    def scrambler_overrides_dialog(self) -> None:
        if self._session is None:
            QMessageBox.information(self, "No session", "Load a capture first.")
            return
        streams = self._session.audio_streams()
        if not streams:
            QMessageBox.information(self, "Scrambler",
                                    "No decoded audio dataports in this capture.")
            return
        cur = self._session.scrambler_overrides
        dlg = QDialog(self)
        dlg.setWindowTitle("Per-Dataport Scrambler")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Override the descrambler per (device, dataport).\n"
                             "Auto = use the value snooped from the bus."))
        combos = {}
        for dev, dp in streams:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"Device {dev}  ·  DP {dp}"))
            cb = QComboBox()
            cb.addItems(["Auto", "On (descramble)", "Off (raw)"])
            if (dev, dp) in cur:
                cb.setCurrentIndex(1 if cur[(dev, dp)] else 2)
            row.addStretch(1)
            row.addWidget(cb)
            lay.addLayout(row)
            combos[(dev, dp)] = cb
        btns = QHBoxLayout()
        ok = QPushButton("Apply"); cancel = QPushButton("Cancel")
        ok.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        btns.addStretch(1); btns.addWidget(cancel); btns.addWidget(ok)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        overrides = {}
        for key, cb in combos.items():
            i = cb.currentIndex()
            if i == 1:
                overrides[key] = True
            elif i == 2:
                overrides[key] = False
        # Re-decode + rebuild the audio store on the worker thread (same path as
        # opening a capture) — doing it inline froze the GUI for seconds. Stop
        # playback first: the worker mutates the live session, and a running play
        # timer would keep reading it (registers/grid) cross-thread. Keep the cursor
        # and bookmarks; a re-decode isn't a new capture and shouldn't drop them.
        self._audio_view.stop()
        sess, keep_cursor, n = self._session, self.cursor.sample, len(overrides)
        keep_bookmarks = set(self._bookmarks)

        def rescramble(s=sess, ov=overrides):
            s.set_scrambler_overrides(ov)
            return s

        def done(_s, cur=keep_cursor, count=n, bms=keep_bookmarks):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText(f"Scrambler override applied to {count} dataport(s); re-decoded.")

        self._load_async(rescramble, after=done)

    # ---- config-vs-decoded comparison ----
    def _run_compare(self, cfg_path: str) -> None:
        """Compare an expected config (CSV path) against the decoded capture as a
        REGISTER-MAP diff: overlay the expected registers in blue in the Register
        Map (alongside the green values snooped from the bus) and report which
        registers differ. Reached from Analyzer ▸ Open Visualizer Config (a CSV file or
        the current Visualizer authoring, when its Compare action is chosen).

        A register comparison (rather than a grid-cell comparison) is robust to the
        visualizer and hardware numbering data ports differently and reads directly
        in spec terms (address + field) instead of placement geometry."""
        if self._session is None:
            return
        expected = swi3score.registers_from_csv(cfg_path)
        if not expected:
            QMessageBox.warning(self, "Compare", "No registers found in that config.")
            return

        # Expected (CSV) config in the register view (blue), beside the green values
        # snooped from the bus — the visual side of the register-map comparison.
        self._reg_view.set_expected(expected)
        self._reg_dock.show()
        self._reg_dock.raise_()

        # Baseline = the decoder's effective register config (snooped from the bus);
        # absent addresses fall back to their spec reset value inside register_diff.
        baseline = [(dev, addr, val)
                    for dev, f in self._session.register_files.items()
                    for addr, val in f.items()]
        diffs = register_diff(expected, baseline, self._rmap)
        summary, detail = register_diff_report(diffs, n_expected=len(expected))

        box = QMessageBox(self)
        box.setWindowTitle("Compare with expected config")
        box.setIcon(QMessageBox.Information)
        box.setText(summary)
        box.setInformativeText(
            "The expected register config is shown in blue in the Register Map "
            "(green = snooped from the bus). Compare ▸ Clear to reset.")
        if detail:
            box.setDetailedText(detail)
        box.exec()
        self._status.setText(f"Config vs decoded: {len(diffs)} register(s) differ "
                             "(expected config shown in blue)")

    def clear_comparison(self) -> None:
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)
            self._reg_view.clear_expected()
            self._status.setText("Comparison cleared")

    # ---- devices (name / register map / hub depth) ----
    def manage_device_names(self) -> None:
        self._open_devices(("names",))

    def manage_register_maps(self) -> None:
        self._open_devices(("maps",))

    def manage_hub_depths(self) -> None:
        self._open_devices(("depths",))

    def _open_devices(self, sections=("names", "maps", "depths")) -> None:
        """Open the Devices dialog focused on `sections` (a subset of names/maps/depths)
        and apply the result. The apply is section-agnostic: it diffs the dialog's
        names/hub_depths/regmaps against the session, so a focused dialog that only
        edited one aspect leaves the others unchanged."""
        if self._session is None:
            QMessageBox.information(self, "No session", "Load a capture first.")
            return
        from .devices_dialog import DevicesDialog
        sess = self._session
        dlg = DevicesDialog(self, range(12), sess.device_names, sess.hub_depths,
                            dict(sess.peripheral_maps), sections=sections)
        if not dlg.exec():
            return
        # Names (UI metadata).
        sess.device_names = dict(dlg.names)
        # A hub-depth change re-decodes below, which rebuilds the register files
        # itself — so skip the per-map rebuild in that case (avoids N+1 whole-capture
        # builds); otherwise let each set_peripheral_map rebuild so the change shows.
        new_depths = {d: v for d, v in dlg.hub_depths.items() if v}
        will_redecode = new_depths != {d: v for d, v in sess.hub_depths.items() if v}
        for dev in range(12):
            new_map = dlg.regmaps.get(dev)
            if new_map is not sess.peripheral_maps.get(dev):
                sess.set_peripheral_map(dev, new_map, rebuild=not will_redecode)
        # Refresh the register view (names + maps + as-of-cursor files) + command
        # table now; a hub-depth re-decode (below) rebuilds these again via
        # load_session, but names/maps must show even when depths didn't change.
        self._reg_view.set_device_names(sess.device_names)
        self._reg_view.set_peripheral_maps(sess.peripheral_maps)
        self._reg_view.set_files(sess.register_files_at(self.cursor.sample))
        self._cmd_model.set_peripheral_maps(sess.peripheral_maps)
        # Hub depths change the decode, so re-decode — but OFF the GUI thread (with
        # the busy dialog), like the scrambler-override path. A synchronous re-decode
        # here froze the UI for seconds on a large capture.
        if will_redecode:
            self._apply_hub_depths_async(new_depths)
        else:
            self._status.setText("Devices updated")

    def _apply_hub_depths_async(self, new_depths: dict) -> None:
        """Re-decode with new per-device hub depths on the worker thread (busy
        dialog, responsive UI), then restore the cursor + bookmarks that load_session
        clears. Mirrors scrambler_overrides_dialog. Playback is stopped first: the
        worker mutates the live session that a running play timer would read."""
        self._audio_view.stop()
        keep_cursor = self.cursor.sample
        keep_bookmarks = set(self._bookmarks)
        sess = self._session

        def rehub(s=sess, d=dict(new_depths)):
            s.set_hub_depths(d)
            return s

        def done(_session, cur=keep_cursor, bms=keep_bookmarks):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText("Hub depths applied; re-decoded.")

        self._load_async(rehub, after=done)

    def _on_pdm_dc_toggled(self, on: bool) -> None:
        """Audio ▸ Block PDM DC Bias: persist the choice and rebuild the audio store
        with the new setting. The bit stream is unchanged (no C++ re-decode) — only
        the PDM→PCM decimation differs — but that re-decimation is O(samples), so run
        it off the GUI thread via the load path (identity factory: same session, new
        store built with the flag). Keeps cursor + bookmarks."""
        self._pdm_dc_block = bool(on)
        QSettings().setValue("audio/pdm_dc_block", self._pdm_dc_block)
        if self._session is None:
            return
        self._audio_view.stop()
        keep_cursor = self.cursor.sample
        keep_bookmarks = set(self._bookmarks)

        def rebuild(s=self._session):
            return s                              # same decode; load_session rebuilds
        #                                           the store with self._pdm_dc_block

        def done(_session, cur=keep_cursor, bms=keep_bookmarks):
            self._bookmarks = set(bms)
            self._timeline.set_bookmarks(sorted(self._bookmarks))
            self._restore_cursor(cur)
            self._status.setText(
                f"PDM DC bias block {'on' if self._pdm_dc_block else 'off'}; audio re-decoded.")

        self._load_async(rebuild, after=done)

    def _select_command_for_sample(self, sample: int) -> None:
        if not self._starts:
            return
        idx = bisect.bisect_right(self._starts, sample) - 1
        if idx < 0:
            idx = 0
        # The exact nearest command may be filtered out of the table. Fall back to the
        # nearest VISIBLE command so the cursor still surfaces a row: prefer the closest
        # accepted row at/before the cursor (the "in effect" command), else the next
        # accepted row after it. Without this, moving the cursor while a filter is
        # active selects nothing.
        n = len(self._starts)

        def _proxy_of(src_row: int):
            # A filtered-out source row maps to an invalid proxy index.
            pi = self._cmd_proxy.mapFromSource(self._cmd_model.index(src_row, 0))
            return pi if pi.isValid() else None

        proxy_idx = _proxy_of(idx)
        if proxy_idx is None:
            for j in range(idx - 1, -1, -1):        # search backward (earlier commands)
                proxy_idx = _proxy_of(j)
                if proxy_idx is not None:
                    break
        if proxy_idx is None:
            for j in range(idx + 1, n):             # then forward (later commands)
                proxy_idx = _proxy_of(j)
                if proxy_idx is not None:
                    break
        if proxy_idx is None:
            return                                  # nothing visible under the filter
        self._syncing = True
        try:
            self._cmd_view.selectRow(proxy_idx.row())
            # Pin the nearest command to the TOP of the viewport. The default
            # EnsureVisible hint scrolls the minimum needed, so the selected row lands
            # at the top when scrolling down but the bottom when scrolling up —
            # inconsistent as the cursor moves. PositionAtTop is deterministic.
            self._cmd_view.scrollTo(proxy_idx, QAbstractItemView.ScrollHint.PositionAtTop)
        finally:
            self._syncing = False
