"""SWI3S Studio main window: command table + register map + 2D bus grid +
decoded audio + a timeline ribbon, all bound by one shared time cursor.

Navigation: a View menu shows/raises any panel (so a closed or tab-hidden dock
is one click away), and the timeline ribbon / command table / cursor stay in
two-way sync — click the timeline to seek, and the command table follows; pick a
command and the timeline + register map follow.
"""
from __future__ import annotations

import atexit
import bisect
import heapq
import inspect
import os
import sys
import tempfile
from typing import List, Optional

import numpy as np
import swi3score
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QGuiApplication,
    QIntValidator,
    QKeySequence,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollBar,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..analysis import (
    capture_measurements,
    count_errors,
    register_diff,
    register_diff_report,
)
from ..export import write_commands_csv
from ..ingest import saleae_binary
from ..model import RegisterMap, viz_engine
from ..model.bookmarks import BookmarkSet
from ..model.bus_config import BusConfig, demo_config
from ..session import Session
from ..workspace import Workspace, session_from_source
from .audio_view import AudioView
from .authoring import AuthoringPanel
from .command_table import CommandFilterProxy, CommandTableModel, CommandTableView
from .cursor import TimeCursor
from .decoded_sample_view import DecodedSampleView
from .eye_view import EyeView
from .grid_view import GridView
from .measurements_view import MeasurementsView
from .mode_controller import ANALYSIS, MODES, TIMING, VISUALIZATION, ModeManager
from .pair_measure_view import PairMeasureView
from .raw_view import RawCaptureView
from .register_view import RegisterView
from .symbol_view import SymbolView
from .theme import (
    VizTheme,
    analyzer_stylesheet,
    apply_palette,
    chrome_stylesheet,
    resolve_mode,
    save_preference,
    saved_preference,
)
from .timeline import TimelineRibbon
from .timing_view import TimingView

DEFAULT_GRID_ROWS = 64   # Bus Grid rows to draw (config layout + TX raster); 64 is a
                         # common payload repeat interval. User-settable per capture.

# TX-map persistence scans a whole config region. Below this many UIs the scan is
# sub-~0.3s, so compute it inline; above it (a long audio region — 100s of millions of
# UIs, seconds of scan) run it on a worker thread so the grid doesn't freeze. Sized so
# the demo + short captures stay synchronous and only genuinely large regions go async.
_TX_PERSIST_SYNC_MAX_UIS = 32_000_000


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


# Samples pane lazy window: hold a bounded slice of decoded samples around the cursor and
# extend it by _SAMPLE_CHUNK rows whenever the user scrolls to either end (see
# DecodedSampleView.edgeReached). QTableWidget item creation is ~11 µs/row, so the initial
# window and chunks are sized to stay responsive rather than loading the whole capture.
_SAMPLE_HALF_WINDOW = 4000     # rows loaded on each side of the cursor initially
_SAMPLE_CHUNK = 4000           # rows added per scroll-to-edge


def _demo_samples() -> int:
    """Audio samples/channel for the demo capture: 1 s @ 48 kHz (48000) by default.
    Tests set SWI3S_DEMO_SAMPLES small so building a MainWindow — which preloads the
    demo — stays fast (a 1 s demo is ~24.6M UIs, ~2.5 s to decode)."""
    try:
        return max(1, int(os.environ.get("SWI3S_DEMO_SAMPLES", "48000")))
    except ValueError:
        return 48000


def _fmt_duration(seconds: float) -> str:
    """Human 'Δ time' for a bookmark pair: pick s / ms / µs / ns by magnitude."""
    a = abs(seconds)
    if a >= 1.0:
        return f"{seconds:,.4f} s"
    if a >= 1e-3:
        return f"{seconds * 1e3:,.3f} ms"
    if a >= 1e-6:
        return f"{seconds * 1e6:,.3f} µs"
    return f"{seconds * 1e9:,.1f} ns"


def _factory_takes_decoder_ready(factory) -> bool:
    """Whether _LoadWorker can hand `factory` a `decoder_ready` kwarg without
    raising. True if it names the param OR forwards **kwargs (the open-capture
    lambdas do `lambda **kw: Session.from_x(..., **kw)`); False for the many
    zero-arg re-decode factories and for callables introspection can't read."""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return False
    if "decoder_ready" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _LoadWorker(QObject):
    """Builds a Session (decode) and its audio store off the GUI thread, so opening
    a large capture doesn't freeze the UI. The factory runs the heavy decode; the
    audio store (per-channel arrays + render pyramids) is built here too — both were
    the synchronous work that beach-balled the main thread on dense captures. The
    per-section UI stats (numpy over millions of clock edges, ~0.3s) are precomputed
    here as well so load_session doesn't recompute them on the GUI thread."""
    done = Signal(object, object, object)   # (session, audio_store, extras dict)
    failed = Signal(str)
    progress = Signal(str)                  # human phase label for the busy dialog

    def __init__(self, factory, pdm_dc_block: bool = False) -> None:
        super().__init__()
        self._factory = factory
        self._pdm_dc_block = pdm_dc_block
        self.decoder = None   # set via decoder_ready, BEFORE run() blocks on the decode —
                               # the GUI thread polls .progress_uis/.total_uis off this while
                               # the C++ decode runs here (it releases the GIL; see Session)

    def run(self) -> None:
        try:
            self.progress.emit("Decoding capture — reading transitions…")
            # Thread the decoder out via decoder_ready BEFORE the blocking decode, so the
            # GUI thread can poll .progress_uis/.total_uis while run() executes here (it
            # releases the GIL). Most factories (re-decode helpers like `apply`/`rescramble`,
            # and the `lambda: Session.from_x(...)` open-capture ones) take no arguments at
            # all, so only pass decoder_ready through when the factory declares it —
            # otherwise every other call site would need updating just to stay callable.
            if _factory_takes_decoder_ready(self._factory):
                session = self._factory(decoder_ready=self._on_decoder_ready)
            else:
                session = self._factory()
            # Phase labels so a big capture shows WHAT it's doing, not a blank spinner.
            try:
                n = int(getattr(session, "audio_count", 0) or 0)
            except Exception:  # noqa: BLE001
                n = 0
            self.progress.emit(f"Reconstructing audio — {n:,} samples…" if n
                               else "Reconstructing audio…")
            store = session.audio_store(pdm_dc_block=self._pdm_dc_block)
            self.progress.emit("Measuring bus sections…")
            extras = {"sections": session.section_ui_stats()}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via `failed`
            self.failed.emit(str(exc))
            return
        self.done.emit(session, store, extras)

    def _on_decoder_ready(self, decoder) -> None:
        self.decoder = decoder


class _TxPersistWorker(QObject):
    """Computes the TX-map persistence summary (Session.tx_persist_columns — a
    whole-config-region scan, seconds on a long audio region) off the GUI thread, so
    entering a region with persistence on doesn't freeze the grid. Emits (lit, cols,
    token); the token lets the GUI drop a result superseded by a later cursor move /
    toggle. Reads immutable capture arrays and writes the session's memo cache (numpy
    releases the GIL over the scan), so the GUI stays live while it runs."""
    done = Signal(object, int, object)   # (lit ndarray, cols, token)

    def __init__(self, session, sample, token) -> None:
        super().__init__()
        self._session = session
        self._sample = sample
        self._token = token

    def run(self) -> None:
        try:
            lit, cols = self._session.tx_persist_columns(self._sample)
        except Exception:  # noqa: BLE001 - a stale/failed scan is just dropped (token guards)
            return
        self.done.emit(lit, int(cols), self._token)


class _LocateWorker(QObject):
    """Runs Session.locate_subcapture (the FFT correlation search, up to 4 passes
    for the orientation retry) off the GUI thread — mirrors _LoadWorker above at a
    smaller scale. On a large capture (the demo is ~24M UIs) this search is
    multi-second, which would otherwise beach-ball the GUI thread with no feedback."""
    done = Signal(list, dict)     # (matches, diagnostics)
    failed = Signal(str)
    progress = Signal(int, str)   # (percent 0..100, phase label) for the progress dialog

    def __init__(self, session, sub) -> None:
        super().__init__()
        self._session = session
        self._sub = sub

    def run(self) -> None:
        try:
            diag: dict = {}
            matches = self._session.locate_subcapture(
                self._sub, diagnostics=diag,
                progress=lambda f, label: self.progress.emit(int(f * 100), label))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via `failed`
            self.failed.emit(str(exc))
            return
        self.done.emit(matches, diag)


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



class _DockTitleBar(QWidget):
    """A themed replacement for the native QDockWidget title bar.

    The native macOS style paints a pale rounded panel behind the close/float
    buttons that clashes with the dark title strip, and neither a QSS
    `::close-button` rule nor the SH_DockWidget_ButtonsHaveFrame hint reliably
    suppresses it (QStyleSheetStyle re-renders the sub-control). Owning the title
    bar sidesteps the platform style entirely: a centered title label plus a
    close button (a plain QToolButton we style directly). Dragging and
    double-click-to-float still work — QDockWidget drives them off mouse events on
    the title-bar widget, which the label/spacers let propagate."""

    _BTN = 18   # close-button box (px); the left spacer matches it so the title centers

    def __init__(self, dock: QDockWidget, title: str) -> None:
        super().__init__(dock)
        self._dock = dock
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(0)
        self._close = QToolButton(self)
        self._close.setText("✕")
        self._close.setFixedSize(self._BTN, self._BTN)
        self._close.setCursor(Qt.ArrowCursor)
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.setToolTip("Close")
        self._close.clicked.connect(dock.close)
        lay.addWidget(self._close)         # left, matching the native macOS dock button
        lay.addStretch(1)
        self._label = QLabel(title, self)
        self._label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._label)
        lay.addStretch(1)
        lay.addSpacing(self._BTN)          # balances the left button so the label centers
        self.retheme()

    def retheme(self) -> None:
        t = VizTheme
        self.setStyleSheet(f"background:{t.FRAME_BG};")
        self._label.setStyleSheet(f"background:transparent; color:{t.TEXT};")
        # The close button is ours (not the native dock button), so QSS applies cleanly:
        # no frame at rest, a subtle rounded tint on hover.
        self._close.setStyleSheet(
            f"QToolButton {{ border:none; background:transparent; color:{t.TEXT_DIM};"
            f" font-size:12px; padding:0; }}"
            f"QToolButton:hover {{ background:{t.BORDER}; color:{t.TEXT};"
            f" border-radius:{t.CORNER_RADIUS}px; }}")

    def setTitle(self, title: str) -> None:  # noqa: N802 - Qt-style name
        self._label.setText(title)


@atexit.register
def _join_main_window_threads_at_exit() -> None:
    """Join every live MainWindow's worker threads before the interpreter tears down.

    Qt calls abort() when a QThread is destroyed while still running, so any window
    that goes away without a closeEvent — a headless script that builds a MainWindow
    and simply ends — takes the whole process down with SIGABRT (exit 134) *after* its
    work has already succeeded. That is invisible to pytest (which reports before
    teardown) but fatal to any runner that judges by exit code, e.g. tests/run_all.sh.

    Registered at import so it covers every entry point without each caller
    remembering to clean up; closing a window normally makes this a no-op.
    """
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            join = getattr(w, "join_worker_threads", None)
            if callable(join):
                join()
    except Exception:  # noqa: BLE001 - interpreter teardown must never raise
        pass


class MainWindow(QMainWindow):
    # True once the Qt event loop is running (set by app.main via a singleShot). Demo loads
    # go through the async worker+progress path only when it's live; before that (the __init__
    # preload) and in headless tests (which never start exec()) they run synchronously, so a
    # freshly built MainWindow has its session ready without pumping events.
    _event_loop_live = False

    def __init__(self, register_map: Optional[RegisterMap] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"SWI3S Studio {__version__}")
        # Clamp the initial size to the available screen: the default 860 px height can
        # exceed a shorter display (or one with a tall taskbar), pushing the bottom dock
        # tab bar off-screen. Fit within the work area, leaving a small margin.
        _w, _h = 1320, 860
        _scr = QGuiApplication.primaryScreen()
        if _scr is not None:
            _avail = _scr.availableGeometry()
            _w = min(_w, _avail.width() - 40)
            _h = min(_h, _avail.height() - 40)
        self.resize(max(_w, 800), max(_h, 500))
        self.setDockNestingEnabled(True)

        self._rmap = register_map or RegisterMap.load()
        self._appearance_pref = saved_preference()   # 'dark' | 'light' | 'system'
        # PDM DC-block preference (default OFF): with it off the analyzer shows the
        # true density (all-ones → +1); on, it removes a real mic's density bias.
        self._pdm_dc_block = QSettings().value("audio/pdm_dc_block", False, type=bool)
        self._session: Optional[Session] = None
        self._audio_store = None
        self._meas_dirty = False               # Statistics needs a (re)build (lazy, on show)
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
        # Coalesce the HEAVY half of the cursor cascade (register replay + symbol/sample
        # table rebuilds + grid render — ~150 ms on a big capture). The cursor LINE moves
        # instantly on every change; the heavy panes refresh once at the resting cursor
        # via this single-shot timer, so clicking around the timeline collapses to one
        # rebuild instead of stacking one per click on the main thread (same idea as the
        # timeline's drag coalescing and the playback throttle). Stays synchronous in
        # headless / tests (see _on_cursor, gated on _event_loop_live).
        self._pending_cursor: Optional[tuple] = None
        self._cursor_heavy_timer = QTimer(self)
        self._cursor_heavy_timer.setSingleShot(True)
        self._cursor_heavy_timer.setInterval(20)
        self._cursor_heavy_timer.timeout.connect(self._apply_cursor_heavy)

        # Command table (central, always visible) with a filter/search bar.
        self._cmd_model = CommandTableModel([], self._rmap, 1)
        self._cmd_proxy = CommandFilterProxy()
        self._cmd_proxy.setSourceModel(self._cmd_model)
        self._cmd_view = CommandTableView()
        self._cmd_view.setModel(self._cmd_proxy)
        # The shared cursor follows the CURRENT cell (currentChanged), not the row
        # selection — so per-cell selection/copy works without breaking cursor sync.
        self._cmd_view.selectionModel().currentChanged.connect(self._on_command_selected)
        # Filtering lives in the Filter menu (built in _build_menu) to save the
        # vertical space an inline filter bar would cost; the command-table title
        # doubles as the live "N of M (active filters)" indicator.
        self._expr_dialog = None
        self._expr_edit = None
        self._cmd_proxy.rowsInserted.connect(self._update_cmd_title)
        self._cmd_proxy.rowsRemoved.connect(self._update_cmd_title)
        self._cmd_proxy.modelReset.connect(self._update_cmd_title)
        # Sorted list of filter-accepted SOURCE rows, so _select_command_for_sample can
        # bisect to the nearest visible command instead of walking mapFromSource per row
        # across a big filtered gap (errors-only on a long clean capture: up to ~1.2 s/tick).
        # Rebuilt lazily and dropped whenever the filter / model changes the visible set.
        self._accepted_src_cache = None
        for _sig in (self._cmd_proxy.rowsInserted, self._cmd_proxy.rowsRemoved,
                     self._cmd_proxy.modelReset, self._cmd_proxy.layoutChanged):
            _sig.connect(lambda *a: setattr(self, "_accepted_src_cache", None))

        analysis_page = QWidget()
        col = QVBoxLayout(analysis_page)
        col.setContentsMargins(0, 0, 0, 0)
        self._cmd_title = QLabel("Commands")  # the central pane has no dock title
        self._cmd_title.setAlignment(Qt.AlignCenter)   # match the dock title bars
        col.addWidget(self._cmd_title)
        col.addWidget(self._cmd_view)

        # Central area is a stack of per-mode pages (Visualization | Timing |
        # Analysis). Analysis is the command-table page above; Visualization is the
        # authoring editor; Timing is a placeholder until that phase lands.
        self._central_stack = _CurrentPageStack()
        self._authoring = AuthoringPanel()
        # Debounce authoring edits: a full engine rebuild (O(rows×columns×DPs)) per field
        # blur makes the panel unresponsive at large row counts. Coalesce rapid edits
        # through a short timer; direct callers still rebuild immediately.
        self._authored_refresh_timer = QTimer(self)
        self._authored_refresh_timer.setSingleShot(True)
        self._authored_refresh_timer.setInterval(150)
        self._authored_refresh_timer.timeout.connect(self._refresh_authored_grid)
        self._authoring.configChanged.connect(self._schedule_authored_refresh)
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
        self._reg_view.comparisonCleared.connect(self.clear_comparison)   # "Clear Compare" button
        self._reg_dock = self._add_dock("Registers", self._reg_view, Qt.RightDockWidgetArea)

        # 2D bus grid + audio + CDS symbols (bottom docks, tabbed).
        self._grid_view = GridView()
        self._grid_view.dpKeyClicked.connect(self._on_dp_key_clicked)
        self._grid_dock = self._add_dock("Bus Grid", self._build_grid_pane(),
                                         Qt.BottomDockWidgetArea)
        self._audio_view = AudioView()
        self._audio_view.seeked.connect(self._on_seek)   # click a waveform to seek
        self._audio_view.playCursorMoved.connect(self._on_seek)  # play head sweeps the cursor
        self._audio_view.stopped.connect(self._on_playback_stopped)  # full refresh on stop
        self._audio_view.bookmarkMoved.connect(self._on_bookmark_moved)
        self._audio_dock = self._add_dock("Audio", self._audio_view, Qt.BottomDockWidgetArea)
        self._symbol_view = SymbolView()
        self._symbol_view.sampleSelected.connect(self._on_symbol_selected)
        self._symbol_view.moreAboveRequested.connect(self._extend_symbols_above)
        self._symbol_dock = self._add_dock("CDS", self._symbol_view, Qt.BottomDockWidgetArea)
        self._sample_view = DecodedSampleView()
        self._sample_view.sampleSelected.connect(self._on_sample_selected)
        self._sample_view.filtersChanged.connect(self._on_sample_filters_changed)
        self._sample_view.edgeReached.connect(self._on_sample_edge)   # lazy load on scroll-to-edge
        self._sample_dock = self._add_dock("Samples", self._sample_view, Qt.BottomDockWidgetArea)
        # Populate the (windowed) sample table only when its tab is actually shown.
        self._sample_dock.visibilityChanged.connect(self._on_sample_dock_visible)
        # The register tree + bus grid are rebuilt on every cursor tick, so _on_cursor
        # skips them while their tab is hidden; refresh once when the tab is re-shown.
        self._reg_dock.visibilityChanged.connect(self._on_reg_dock_visible)
        self._grid_dock.visibilityChanged.connect(self._on_grid_dock_visible)
        self._raw_view = RawCaptureView()
        self._raw_view.sampleSelected.connect(self._on_seek)
        self._raw_view.bookmarkMoved.connect(self._on_bookmark_moved)
        self._raw_dock = self._add_dock("Capture", self._raw_view, Qt.BottomDockWidgetArea)
        self._eye_view = EyeView()
        self._eye_view.sampleSelected.connect(self._on_timing_jump)
        self._eye_dock = self._add_dock("Timing", self._eye_view, Qt.BottomDockWidgetArea)
        self._meas_view = MeasurementsView()
        self._meas_dock = self._add_dock("Statistics", self._meas_view, Qt.RightDockWidgetArea)
        # Statistics is tabbed behind Registers (hidden at load), but capture_measurements
        # iterates the whole audio store — so compute it lazily on first show / re-decode,
        # not eagerly for a hidden tab. Same deferral as the register/grid docks.
        self._meas_dock.visibilityChanged.connect(self._on_meas_dock_visible)
        self._pair_view = PairMeasureView()
        self._pair_view.sampleSelected.connect(self._on_seek)     # click a pair -> seek to it
        self._pair_dock = self._add_dock("Bookmarks", self._pair_view, Qt.RightDockWidgetArea)
        # Bottom docks tabbed left→right: Bus Grid, Capture, CDS, Audio, Samples, Timing.
        self.tabifyDockWidget(self._grid_dock, self._raw_dock)
        self.tabifyDockWidget(self._raw_dock, self._symbol_dock)
        self.tabifyDockWidget(self._symbol_dock, self._audio_dock)
        self.tabifyDockWidget(self._audio_dock, self._sample_dock)
        self.tabifyDockWidget(self._sample_dock, self._eye_dock)
        self.tabifyDockWidget(self._reg_dock, self._meas_dock)
        self.tabifyDockWidget(self._meas_dock, self._pair_dock)
        self._grid_dock.raise_()
        self._reg_dock.raise_()
        self._style_dock_tabbars()      # drop the native (lighter) tab-bar base strip

        # Match the central pane's faux title bar to the real dock title bars: a
        # plain QLabel inherits the (larger) default widget font, while QDockWidget
        # draws its title with the dock's own font — copy it so they line up.
        self._cmd_title.setFont(self._grid_dock.font())

        # Docks that should stay tabbed together — used to re-tabify on re-show
        # (Qt drops a re-shown dock out of its tab group otherwise).
        self._tab_groups = [
            [self._grid_dock, self._raw_dock, self._symbol_dock, self._audio_dock,
             self._sample_dock, self._eye_dock],
            [self._reg_dock, self._meas_dock, self._pair_dock],
        ]

        self._bookmarks = BookmarkSet()
        self._timeline.bookmarkMoved.connect(self._on_bookmark_moved)

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
                           self._pair_dock, self._grid_dock, self._audio_dock,
                           self._symbol_dock, self._sample_dock, self._raw_dock,
                           self._eye_dock],
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
                Session.from_demo(_demo_samples(), cold_start=True, register_map=self._rmap),
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
        # Switching to Visualization makes the (tall) authoring panel the central widget's
        # size hint; QMainWindow grows the window to meet its minimum and never shrinks it
        # back on the next switch, so the window can creep past the screen (bottom off-
        # screen in Viz, dock tab bar clipped back in Analysis). Re-fit to the work area
        # after the switch settles so the window never stays larger than the screen.
        QTimer.singleShot(0, self._fit_to_available_screen)

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
        # Fit the whole window FRAME inside the screen work area, once. resize() in
        # __init__ sets the client size, but the title bar + borders (~30-40 px on
        # Windows) aren't known until the frame exists after show — without this the
        # frame's bottom fell below the taskbar and the bottom dock tab bar was hidden
        # until a full-screen toggle forced a relayout.
        if not getattr(self, "_fit_screen_once", False):
            self._fit_screen_once = True
            QTimer.singleShot(0, self._fit_to_available_screen)
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

    def _fit_to_available_screen(self) -> None:
        """Shrink + nudge the window so its whole frame fits the screen work area.
        Accounts for the window-manager frame overhead (title bar + borders), which is
        only known once the window is shown. Runs once from showEvent."""
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is None:
            return
        avail = scr.availableGeometry()
        frame = self.frameGeometry()
        overhead_w = max(0, frame.width() - self.width())
        overhead_h = max(0, frame.height() - self.height())
        new_w = min(self.width(), avail.width() - overhead_w)
        new_h = min(self.height(), avail.height() - overhead_h)
        if new_w != self.width() or new_h != self.height():
            self.resize(max(new_w, 800), max(new_h, 500))
        # Slide the (possibly resized) frame back inside the work area, clamped so it
        # never runs off the top/left either.
        frame = self.frameGeometry()
        dx = min(0, avail.right() - frame.right())
        dy = min(0, avail.bottom() - frame.bottom())
        if frame.left() + dx < avail.left():
            dx = avail.left() - frame.left()
        if frame.top() + dy < avail.top():
            dy = avail.top() - frame.top()
        if dx or dy:
            self.move(self.x() + dx, self.y() + dy)

    def _strip_macos_fullscreen_menu_item(self) -> None:
        """Remove AppKit's auto-inserted full-screen item from the (native) menu bar.
        The green title-bar button still toggles full screen; only the menu entry goes.
        No-op unless we're on macOS with PyObjC and the item is present."""
        import sys
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp, NSUserDefaults
            # Stop AppKit from re-inserting "Enter Full Screen" every time a menu opens
            # (this is why it "came back" after a one-off removal); then drop any it has
            # already added. The green title-bar button still toggles full screen.
            NSUserDefaults.standardUserDefaults().setBool_forKey_(
                False, "NSFullScreenMenuItemEverywhere")
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
    def _schedule_authored_refresh(self) -> None:
        """Coalesce a burst of authoring edits into one rebuild (see the debounce timer)."""
        self._authored_refresh_timer.start()

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
            from ..analysis.issues import ERROR, Issue
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
            cfg = self._bus_config_from_csv(path)
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
            self, "Load bus config (visualizer CSV)",
            self._last_capture_dir("visualizer"), "CSV (*.csv);;All files (*)")
        if path:
            self._remember_capture_dir(path, "visualizer")
            self._load_authoring_csv(path)

    def open_visualizer_csv(self) -> None:
        """File ▸ Open Visualizer CSV: load a Visualizer config CSV into the authoring
        panel, switching to Visualizer mode if we're elsewhere. Legacy / v2.0 CSVs are
        upgraded to the current format on the way in (visualizer_csv.normalized_path)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Visualizer CSV", self._last_capture_dir("visualizer"),
            "Visualizer CSV (*.csv);;All files (*)")
        if not path:
            return
        if self._mode_mgr.current() != VISUALIZATION:
            self._mode_mgr.switch_to(VISUALIZATION)
        self._remember_capture_dir(path, "visualizer")
        self._load_authoring_csv(path)

    def _load_authoring_csv(self, path: str) -> None:
        """Read a Visualizer CSV into the authoring panel (upgrading legacy/v2.0 CSVs)."""
        try:
            self._authoring.set_config(self._bus_config_from_csv(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load config", f"Couldn't read that CSV: {exc}")
            return
        self._status.setText(f"Loaded config: {path}")
        self._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")

    def _bus_config_from_csv(self, path: str) -> "BusConfig":
        """Read a BusConfig from a Visualizer CSV, upgrading any legacy/v2.0 format and
        removing the temp file normalized_path may create, so repeated loads of an old
        config don't leak temps into the OS temp dir."""
        from ..ingest import visualizer_csv
        cfg_path = visualizer_csv.normalized_path(path)   # upgrade any legacy/v2.0 CSV
        try:
            return BusConfig.from_csv(cfg_path)
        finally:
            if cfg_path != path:
                try:
                    os.remove(cfg_path)
                except OSError:
                    pass


    def save_authoring_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save bus config",
            os.path.join(self._last_capture_dir("visualizer"), "bus_config.csv"),
            "CSV (*.csv)")
        if not path:
            return
        self._remember_capture_dir(path, "visualizer")
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
            self, "Export frame model JSON",
            os.path.join(self._last_capture_dir("visualizer"), "frame_model.json"),
            "JSON (*.json)")
        if not path:
            return
        self._remember_capture_dir(path, "visualizer")
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
            self, "Save timing settings",
            os.path.join(self._last_capture_dir("timing"), "timing_settings.json"),
            "JSON (*.json)")
        if not path:
            return
        self._remember_capture_dir(path, "timing")
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
            self, "Open timing settings", self._last_capture_dir("timing"),
            "JSON (*.json);;All files (*)")
        if not path:
            return
        self._remember_capture_dir(path, "timing")
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
        for bar in getattr(self, "_dock_titlebars", []):    # custom dock title bars
            bar.retheme()
        self._style_dock_tabbars()          # tab-bar base tracks the palette
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
                     self._sample_view, self._raw_view, self._eye_view, self._meas_view,
                     self._pair_view):
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
        # Own the title bar so the close X matches the theme (see _DockTitleBar).
        bar = _DockTitleBar(dock, title)
        dock.setTitleBarWidget(bar)
        if not hasattr(self, "_dock_titlebars"):
            self._dock_titlebars = []
        self._dock_titlebars.append(bar)
        self.addDockWidget(area, dock)
        if not hasattr(self, "_dock_area"):
            self._dock_area = {}
        self._dock_area[dock] = area          # home area, for robust re-show
        return dock

    def _style_dock_tabbars(self) -> None:
        """Flatten the tabified-dock QTabBars to the panel background. The native
        (macOS) style paints a lighter 'tab base' strip behind/around the tab chips
        that a QSS `QTabBar { background }` doesn't override — it reads as a pale block
        around the Registers/Statistics and Bus Grid/Capture/… tabs. setDrawBase(False)
        drops that strip, and an autoFill with the panel colour makes the bar sit flush
        on the surrounding pane. Re-run on theme switch (the palette colour changes)."""
        t = VizTheme
        for tb in self.findChildren(QTabBar):        # the app has no QTabWidgets — all dock bars
            tb.setDrawBase(False)
            tb.setAutoFillBackground(True)
            pal = tb.palette()
            pal.setColor(QPalette.Window, QColor(t.FRAME_BG))
            tb.setPalette(pal)

    def _build_menu(self) -> None:
        f = self.menuBar().addMenu("&File")
        # File actions are grouped by the mode they act on. Opens work from any mode
        # (each switches to the mode it needs) and the handlers guard when there's
        # nothing to act on, so nothing in File is mode-gated.
        analyzer = f.addMenu("&Analyzer")
        analyzer.addAction("Open &Capture…", self.open_capture)
        demo_menu = analyzer.addMenu("Open &Demo Capture")
        demo_menu.addAction("PHY1 (FBCSE)", lambda: self.load_demo(phy=1))
        demo_menu.addAction("PHY2 (FBCSE)", lambda: self.load_demo(phy=2))
        demo_menu.addAction("PHY2 (Flow Control)",
                            lambda: self.load_demo(phy=2, variant="flow_control"))
        demo_menu.addAction("PHY3 (DLV)", lambda: self.load_demo(phy=3))
        self._export_capture_action = analyzer.addAction("Export &Capture…", self.export_capture)
        self._export_capture_action.setEnabled(False)     # needs a loaded capture
        analyzer.addAction("&Locate Sub-Capture…", self.locate_subcapture)
        analyzer.addSeparator()
        analyzer.addAction("&Import Visualizer CSV…", self.open_visualizer_config)
        analyzer.addAction("&Export Visualizer CSV…", self.export_grid_csv)
        analyzer.addAction("&Force Column Count…", self.force_column_count)
        analyzer.addSeparator()
        analyzer.addAction("View Bus Grid in Visuali&zer", self.open_grid_in_visualizer)
        analyzer.addAction("Save Bus Grid &Image…", lambda: self.export_grid(self._grid_view))
        analyzer.addSeparator()
        analyzer.addAction("Open &Workspace…", self.open_workspace)
        analyzer.addAction("Save &Workspace…", self.save_workspace)
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
        # effect), with ⌘> / ⌘< (Ctrl on non-mac).
        cmds = self.menuBar().addMenu("&Commands")
        self._build_filter_menu(cmds)
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
        # Nudge the SSP by ±1 row with ⌘K / ⌘L (K left of L → −1 / +1). Both display and
        # aren't macOS-reserved — unlike ⌘, (Preferences), which macOS strips the glyph
        # from on any non-Preferences item.
        ssp.addAction("Move SSP &−1 Row\tCtrl+K", lambda: self.step_ssp_row(-1)).setShortcut("Ctrl+K")
        ssp.addAction("Move SSP &+1 Row\tCtrl+L", lambda: self.step_ssp_row(+1)).setShortcut("Ctrl+L")
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
                            ("Registers", self._reg_dock),
                            ("Statistics", self._meas_dock),
                            ("Bookmarks", self._pair_dock),
                            ("Bus Grid", self._grid_dock),
                            ("Capture", self._raw_dock),
                            ("CDS", self._symbol_dock),
                            ("Audio", self._audio_dock),
                            ("Samples", self._sample_dock),
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
        base = "Commands"
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
        """Push the command table's currently-VISIBLE commands to the timeline: their RSP
        cursor samples for Left/Right arrow nav, AND their raw start_samples for the drawn-
        tick filter (so hidden commands' ticks disappear). The two differ — nav parks on the
        RSP, but ticks are drawn at the raw start_sample — so they're sent separately."""
        if self._session is None or self._cmd_model is None:
            self._timeline.set_nav_starts(None)
            self._timeline.set_draw_filter(None)
            return
        proxy = self._cmd_proxy
        nav, draw = [], []
        for pr in range(proxy.rowCount()):
            sr = proxy.mapToSource(proxy.index(pr, 0)).row()
            if 0 <= sr < len(self._starts):
                nav.append(int(self._starts[sr]))
            cmd = self._cmd_model.command_at(sr)
            if cmd is not None:
                draw.append(int(cmd.get("start_sample", 0)))
        self._timeline.set_nav_starts(nav)
        self._timeline.set_draw_filter(draw)

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
            ("Waveform panes (Audio / Capture, when focused)", [
                (">", "Page the view one width right"),
                ("<", "Page the view one width left"),
            ]),
            ("Manual Stream Sync Point (Audio)", [
                (f"{mod}L", "Move SSP +1 row"),
                (f"{mod}K", "Move SSP −1 row"),
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
        from .timeline import _LC_SECTION_PALETTE_IDX, MARK_LEGEND, _lc_section_color
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
        timer) instead of leaving it to GC ordering during teardown, then join EVERY
        worker thread so no QThread is destroyed while still running (a noisy warning
        at best, a SIGABRT at worst).

        All three workers must be joined, not just the TX-persistence one: quitting
        while a capture load or a sub-capture search is in flight would otherwise
        destroy a running QThread. Qt aborts the process on that, so it surfaced as an
        exit-134 with the tests themselves all passing."""
        try:
            self._audio_view._hard_stop()
        except Exception:  # noqa: BLE001 - never block window close on teardown
            pass
        self.join_worker_threads()
        super().closeEvent(event)

    def join_worker_threads(self) -> None:
        """Quit + wait on every worker QThread, clearing each slot.

        Safe to call more than once and at any point (a slot that is None is skipped).
        Exposed separately from closeEvent so a headless caller — a test that builds a
        MainWindow without ever showing or closing it — can release the threads
        deterministically instead of leaving them to interpreter teardown.

        Sets `_closing` FIRST and drops the pending-work queues: a worker's queued
        done/failed signal can still be delivered after wait() returns, and its
        handler would otherwise start the next queued request — spawning a brand-new
        QThread after we believed everything was joined, which is exactly the
        destroyed-while-running SIGABRT this method exists to prevent.
        """
        self._closing = True
        self._load_pending = None
        self._tx_persist_pending = None
        for attr in ("_load_thread", "_locate_thread", "_tx_persist_thread"):
            th = getattr(self, attr, None)
            if th is None:
                continue
            try:
                th.quit()
                th.wait()
            except RuntimeError:
                pass          # already destroyed by Qt (deleteLater on finished)
            setattr(self, attr, None)

    # ---- loading ----
    def load_demo(self, phy: int = 2, variant: str = "") -> None:
        """The demo capture for the selected audio-mode PHY, with a §5.1.2 Cold Start
        spliced in front so the link comes up from Bus Reset → PHY-select → audio on the
        wire (the grid shows 'No PHY Selected' until the PHY-select is decoded). PHY1 =
        slow FBCSE (4-column bus whose ports are repositioned mid-capture); PHY2 = FBCSE;
        PHY3 = DLV (differential pair, recovered clock, mid-UI sampling). ~4 sine cycles
        per channel (period 64). ``variant="flow_control"`` loads the PHY2-framed four-mode
        flow-control demo (TX_PRESENT-gated jittered transport + validated DRQ handshake).

        Loaded through the async path (worker thread + progress dialog), same as an external
        capture, once the event loop is live: the 1 s demo synthesises + decodes ~25M UIs
        (seconds), which would otherwise beach-ball the GUI thread with no feedback. Before
        the loop starts (and in headless tests) it loads synchronously so the session is
        ready immediately."""
        n, p, v = _demo_samples(), int(phy), str(variant)
        factory = lambda **kw: Session.from_demo(n, cold_start=True, phy=p,   # noqa: E731
                                                 variant=v,
                                                 register_map=self._rmap, **kw)
        if MainWindow._event_loop_live:
            self._load_async(factory)
        else:
            self.load_session(factory())

    def load_demo_bringup(self) -> None:
        """Alias for load_demo (PHY2) — kept for the app's --demo-bringup flag."""
        self.load_demo(phy=2)

    # Per-mode last-opened-file directory: the Analyzer (captures, .sal export, sub-
    # capture) and the Visualizer (config CSVs) browse independently, so opening one
    # doesn't jump the other's dialog to an unrelated folder. Keys are QSettings
    # "capture/last_dir_<mode>"; the pre-existing unscoped "capture/last_dir" (from
    # before per-mode dirs existed) seeds the analyzer's the first time so upgraders
    # keep their remembered folder instead of resetting to empty.
    def _last_capture_dir(self, mode: str = "analyzer") -> str:
        """The directory of the last-opened file for `mode` ("analyzer" |
        "visualizer" | "timing"), persisted across launches. Visualizer defaults to
        ./visualizer_examples (relative to the cwd) when nothing is stored yet;
        Analyzer and Timing default to empty (Analyzer falls back to the legacy shared
        key, then empty)."""
        settings = QSettings()
        key = f"capture/last_dir_{mode}"
        stored = settings.value(key, "", type=str)
        if stored:
            return stored
        if mode == "analyzer":
            return settings.value("capture/last_dir", "", type=str)   # legacy shared key
        if mode == "visualizer":
            return os.path.join(os.getcwd(), "visualizer_examples")
        return ""

    def _remember_capture_dir(self, path: str, mode: str = "analyzer") -> None:
        if path:
            # A directory picker (audio export) hands back the dir itself; a file
            # picker hands back a file whose parent is the dir to remember.
            d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
            QSettings().setValue(f"capture/last_dir_{mode}", d)

    def open_visualizer_config(self) -> None:
        """Analyzer ▸ Import Visualizer CSV: pick a config (a CSV file or the current
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
        # A digital capture CSV (Time, ch0, ch1) also ends in .csv but is NOT a Visualizer
        # config — imposing it would silently seed an empty config (grid disappears, no
        # error). A real config CSV carries AppVersion / *_REG / NumColumns rows (the same
        # markers _open_csv uses to reject a config as a capture). Reject the inverse here.
        try:
            with open(dlg.path, encoding="utf-8", errors="replace") as f:
                head = [next(f, "") for _ in range(40)]
        except OSError as exc:
            QMessageBox.warning(self, "Import Visualizer CSV", f"Couldn't read that file: {exc}")
            return
        if not any(ln.startswith("AppVersion") or "_REG," in ln or ln.startswith("NumColumns")
                   for ln in head):
            QMessageBox.warning(
                self, "Import Visualizer CSV",
                "That CSV isn't a Visualizer config (no AppVersion / *_REG / NumColumns "
                "rows) — it looks like a capture. Open a capture via File ▸ Open Capture, "
                "or export one first with Export Visualizer CSV.")
            return
        try:
            cfg_path = visualizer_csv.normalized_path(dlg.path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Import Visualizer CSV", f"Couldn't read that CSV: {exc}")
            return
        self._remember_capture_dir(dlg.path)
        # `cfg_path` is only for the (synchronous) compare read; the update path re-reads
        # the ORIGINAL dlg.path (Session.apply_config_csv re-normalizes and owns its own
        # temp, for persistence). So if normalized_path upgraded a legacy/v2.0 CSV into a
        # temp here, remove it afterwards rather than leaking one per open.
        try:
            if dlg.do_compare:
                self._run_compare(cfg_path)
            if dlg.do_update:
                self._apply_config_csv_path(dlg.path)
        finally:
            if cfg_path != dlg.path:
                try:
                    os.remove(cfg_path)
                except OSError:
                    pass

    def _apply_config_csv_path(self, csv: str) -> None:
        """Impose a Visualizer config CSV on the ALREADY-OPEN capture (no reopen): re-decode
        with it AND seed the bus grid + register map from it (Provenance.CSV — "CSV Import"),
        so a post-commit capture matches the CSV. Runs the re-decode on the worker thread
        (same path as the Scrambler override); the cursor and bookmarks are kept."""
        # A config CSV is imposed as ONE config from row 0. A capture that reconfigures
        # mid-stream (e.g. a DLV cold start: Safe-Lock-4 -> 16-col) has several — so the
        # single config can only match one region and will misframe the others (their
        # commands go CRC-red). Warn before imposing.
        if self._session is not None:
            widths = sorted({int(s.get("column_count", 0)) for s in self._session.segments})
            if len(widths) > 1:
                resp = QMessageBox.warning(
                    self, "Import Visualizer CSV",
                    f"This capture reconfigures mid-stream ({len(self._session.segments)} "
                    f"regions, column widths {', '.join(map(str, widths))}). A config CSV is "
                    "imposed from row 0, so it can match only one region and will misframe "
                    "the others (their commands turn red).\n\nImpose it anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if resp != QMessageBox.Yes:
                    return
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = self._session, self.cursor.sample, self._bookmarks.copy()

        def apply(s=sess, p=csv):
            s.apply_config_csv(p)
            return s

        def done(_s, cur=keep_cursor, bms=keep_bms, p=csv):
            self._bookmarks = bms
            self._push_bookmarks()
            self._restore_cursor(cur)
            self._status.setText(f"Imposed {os.path.basename(p)} on the capture "
                                 f"(grid + register map from CSV; re-decoded).")

        self._load_async(apply, after=done)

    def _on_dp_key_clicked(self, device: int, dp: int) -> None:
        """A data-port swatch in the Bus Grid colour key was clicked: choose which
        fields that port's cell labels show (Sample / Channel / Bit).

        The Analyzer was hard-wired to Channel|Bit, which hides the sample index —
        so a multi-sample port drew every cell as the same "C0B0". This is the
        Visualizer's per-port display-fields choice, applied to the decoded grid.
        Display only: it re-renders, never re-decodes.
        """
        s = self._session
        if s is None:
            return
        from .authoring.dialogs import GridLabelFieldsDialog

        # Preview against this port's first real cell, so the dialog shows the label
        # the grid will actually draw rather than a generic S0C0B0.
        smp = ch = bit = 0
        for c in s.grid_cells_at(self.cursor.sample, self._grid_rows):
            if c.get("device") == device and c.get("dp") == dp and not c["is_cds"]:
                smp, ch, bit = int(c.get("sample", 0)), int(c.get("channel", 0)), int(c.get("bit", 0))
                break
        names = getattr(s, "device_names", {}) or {}
        who = names.get(device) or f"D{device}·DP{dp}"
        dlg = GridLabelFieldsDialog(self, f"{who} Cell Labels",
                                    s.label_fields_for(device, dp),
                                    sample=smp, channel=ch, bit=bit, apply_all=True)
        if not dlg.exec():
            return
        if dlg.apply_to_all:
            # EVERY port configured anywhere in the capture — not just the ones whose cells
            # land in the grid window at the cursor. That window is `self._grid_rows` rows
            # as-of the cursor, so a port first configured in a later region kept the
            # default labels, and with the cursor in the FIRST region (before any commit
            # has taken effect) it can yield no ports at all — "apply to every data port"
            # then silently applied to just the one clicked. Read the CONFIG per region
            # instead of the rendered cells, and include the final config so a port
            # disabled before the last region is still covered.
            ports = {(device, dp)}
            for smp in [None] + [int(sg.get("start_sample", 0)) for sg in (s.segments or [])]:
                for d in (s.config_dataports_at(smp).get("dataports") or []):
                    if d.get("enabled") and int(d.get("dp_number", -1)) >= 0:
                        ports.add((int(d.get("device_number", 0)), int(d["dp_number"])))
            for d, p in ports:
                s.set_label_fields(d, p, dlg.fields)
        else:
            s.set_label_fields(device, dp, dlg.fields)
        self._update_grid_for_cursor(self.cursor.sample)

    def force_column_count(self) -> None:
        """Analyzer ▸ Force Column Count: pin the bus column count for the config
        region under the cursor, and re-decode.

        Region-scoped on purpose. An imported config CSV can know a geometry the wire
        never carried (a post-commit capture has no NumColumns commit to snoop), and the
        blind column detector can mis-lock — but a capture that genuinely reconfigures
        mid-stream has several real widths, so forcing one across the whole capture
        misframes the others. Pinning only the cursor's region fixes what you're looking
        at and leaves the rest decoding as the wire says. The pinned region is labelled
        '(forced)' in the timeline and persists in the workspace."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        s = self._session
        sample = self.cursor.sample
        sect = s.segment_index_for_sample(int(sample))
        current = int(s.forced_column_sections.get(sect, 0))
        wire = int(s.column_count_at(int(sample)))
        n_regions = max(1, len(s.segments))
        # Offer the imported config CSV's width as the default when there is one — that
        # is the case this exists for, and it saves reading the number off the CSV.
        csv_cols = 0
        try:
            if s._config_csv:
                csv_cols = int(s.config_dataports_at(None).get("num_columns", 0)) + 1
        except Exception:  # noqa: BLE001 - a bad/partial CSV must not block the dialog
            csv_cols = 0
        default = current or csv_cols or wire
        val, ok = QInputDialog.getInt(
            self, "Force Column Count",
            f"Column count for config region {sect + 1} of {n_regions} "
            f"(currently decoding at {wire}"
            + (f"; imported CSV says {csv_cols}" if csv_cols and csv_cols != wire else "")
            + ").\n\nThis pins THIS region only — other regions keep the width read off "
              "the wire.\nSet 0 to remove the pin and decode this region normally.",
            default, 0, 32)
        if not ok:
            return
        if val and not 2 <= val <= 32:
            QMessageBox.warning(self, "Force Column Count",
                                f"{val} is not a legal column count (2–32).")
            return
        self._audio_view.stop()
        sess, keep_cursor, keep_bms = s, sample, self._bookmarks.copy()

        def apply(_s=sess, smp=sample, v=val):
            _s.force_column_count_at(int(smp), int(v))
            return _s

        def done(_s, cur=keep_cursor, bms=keep_bms, v=val, i=sect):
            self._bookmarks = bms
            self._push_bookmarks()
            self._restore_cursor(cur)
            self._status.setText(
                f"Region {i + 1} pinned to {v} columns (re-decoded)." if v
                else f"Region {i + 1} pin removed (re-decoded).")

        self._load_async(apply, after=done)

    def open_capture(self, config_csv: str = "") -> None:
        """Open a capture: a Logic 2 `.sal` project, one digital `.csv`, a simulation
        `.vcd`, or BOTH per-channel files together (multi-select) for the two-file
        formats — `.bin` (Saleae binary) and `.wfm` (Tektronix scope) — to skip the
        second-file prompt. `config_csv` (optional) supplies the data-port config to
        decode with from row 0 — normally empty from the menu; use Apply Config CSV to
        impose a config after opening. Opening switches to Analysis (via load_session)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open capture (.sal, .csv, .vcd, or both .bin/.wfm channel files)",
            self._last_capture_dir(),
            filter="Captures (*.sal *.csv *.bin *.vcd *.wfm);;Saleae project (*.sal);;"
                   "Digital CSV (*.csv);;Saleae binary (*.bin);;VCD (*.vcd);;"
                   "Tektronix waveform (*.wfm);;All files (*)")
        if not paths:
            return
        self._remember_capture_dir(paths[0])
        try:
            bins = [p for p in paths if p.lower().endswith(".bin")]
            if len(bins) >= 2:
                self._open_bin(bins[0], bins[1], config_csv=config_csv)
                return
            wfms = [p for p in paths if p.lower().endswith(".wfm")]
            if len(wfms) >= 2:
                self._open_wfm_pair(wfms[0], wfms[1])
                return
            path = paths[0]
            low = path.lower()
            if low.endswith(".sal"):
                self._open_sal(path, config_csv=config_csv)
            elif low.endswith(".csv"):
                self._open_csv(path, config_csv=config_csv)
            elif low.endswith(".vcd"):
                self._open_vcd(path, config_csv=config_csv)
            elif low.endswith(".wfm"):
                QMessageBox.warning(self, "Open WFM",
                                    "A .wfm capture is two files (one channel each) — "
                                    "select BOTH .wfm files together.")
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

    def _sal_window_prompt(self, path: str, clk: int, dat: int):
        """Pre-flight a .sal open and, when it won't fit in memory, offer a window.

        A .sal is a zip of delta-coded transitions: a ~280 MB file can hold 2.2 GB
        of payload and decode to ~18 GB of uint64 edge arrays, which exhausts RAM
        and leaves the machine unresponsive with no error. The estimate below reads
        only the zip directory, so it costs nothing and happens BEFORE any
        allocation.

        Returns the (start_sample, end_sample) window to load, None for the whole
        capture, or the string "cancel" if the user backed out.
        """
        from ..ingest import saleae_sal
        try:
            cost = saleae_sal.estimate_cost(path, channels=[clk, dat])
            budget = saleae_sal.memory_budget()   # shared with the loader's guard
        except Exception:
            return None                       # can't estimate — let the load proceed
        if budget <= 0 or cost.fits_in(budget):
            return None
        span = saleae_sal.capture_span_samples(path)
        rate = cost.sample_rate_hz or 1
        total_s = span / rate if span else 0.0
        # Suggest the largest window predicted to fit, rounded down to a whole second.
        frac = budget / float(max(1, cost.est_edge_bytes * 2))
        suggest_s = max(1.0, min(total_s or 10.0, (total_s or 10.0) * frac))
        msg = (f"This capture is too large to open in full.\n\n"
               f"{cost.summary()}\nAvailable: {budget / 1e9:.1f} GB\n\n")
        if not span:
            QMessageBox.warning(self, "Capture too large", msg +
                                "A time window can't be offered (no block index in "
                                "this file). Free memory, or export a shorter range "
                                "from Logic 2.")
            return "cancel"
        msg += (f"The capture is {total_s:.1f} s long. Open a time window instead?\n"
                f"About {suggest_s:.0f} s is predicted to fit.")
        if QMessageBox.question(self, "Capture too large", msg,
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return "cancel"
        start, ok = QInputDialog.getDouble(self, "Open window", "Start (seconds):",
                                           0.0, 0.0, max(0.0, total_s), 3)
        if not ok:
            return "cancel"
        length, ok = QInputDialog.getDouble(self, "Open window", "Length (seconds):",
                                            round(suggest_s, 3), 0.001,
                                            max(0.001, total_s - start), 3)
        if not ok:
            return "cancel"
        return (int(start * rate), int((start + length) * rate))

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
        window = self._sal_window_prompt(path, clk, dat)
        if window == "cancel":
            return

        def after(sess, p=path):
            src = sess.source
            win = src.get("window")
            rate = sess.sample_rate_hz or 1
            span = (f", window {win[0]/rate:.3f}-{win[1]/rate:.3f} s" if win else "")
            self._status.setText(
                f"Opened {p}  (clock=ch{src['clock_channel']}, data=ch{src['data_channel']}"
                f"{' auto' if src.get('auto_clock') else ''}, "
                f"{sess.sample_rate_hz/1e6:.3f} MHz{span})")
        self._load_async(
            lambda **kw: Session.from_sal(path, clk, dat, auto_clock=auto,
                                     window=window, register_map=self._rmap,
                                     config_csv=config_csv, **kw),
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
            lambda **kw: Session.from_vcd(path, clk, dat, auto_clock=auto,
                                     register_map=self._rmap, config_csv=config_csv, **kw),
            after=after)

    def _open_csv(self, path: str, config_csv: str = "") -> None:
        from ..ingest import analog_csv, digital_csv
        # A Visualizer *config* CSV (register-style: AppVersion / *_REG / NumColumns
        # rows) is not a capture — catch it up front so the user gets an actionable
        # message instead of a cryptic "analog CSV: no TIME header" from deep in the
        # parser (is_analog_csv can false-positive on it).
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = [next(f, "") for _ in range(40)]
        except OSError as exc:
            raise ValueError(f"Couldn't read {path}: {exc}") from exc
        if any(ln.startswith("AppVersion") or "_REG," in ln or ln.startswith("NumColumns")
               for ln in head):
            raise ValueError(
                "This looks like a Visualizer config CSV, not a capture.\n\n"
                "Open it via File ▸ Visualizer ▸ Open Settings.")
        # A .csv is either a Logic 2 DIGITAL export (header row of 0/1 channels) or
        # a Tektronix ANALOG scope export (volts, needs thresholding). Route by the
        # file's own shape so a single Open Capture handles both.
        if analog_csv.is_analog_csv(path):
            self._open_analog_csv(path, config_csv=config_csv)
            return
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
            # A clear winner (forwarded clock has the most edges), OR exactly two
            # channels — then assign by count order and let the loader sort out the
            # rest. A DLV differential pair ties on edge count (both rails toggle
            # together), so a 2-channel tie must NOT drop to the manual picker; the
            # complementary pair is detected downstream (Session.from_digital_csv).
            if counts[order[0]] > counts[order[1]] or len(cols) == 2:
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
            lambda **kw: Session.from_digital_csv(path, clk, dat, register_map=self._rmap,
                                             config_csv=config_csv, **kw),
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
            lambda **kw: Session.from_saleae_binary(clock, dat, rate, register_map=self._rmap,
                                               config_csv=config_csv, **kw),
            after=after)

    def open_wfm(self) -> None:
        """Kept for programmatic/scripted use: pick both .wfm channel files and
        open them as a pair. (No menu entry — .wfm is opened via Open Capture by
        multi-selecting both files, same as .bin.)"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open WFM pair (select BOTH channel files)", self._last_capture_dir(),
            "Tektronix waveform (*.wfm);;All files (*)")
        if len(paths) == 2:
            self._remember_capture_dir(paths[0])
            self._open_wfm_pair(paths[0], paths[1])

    def _open_wfm_pair(self, clock_path: str, data_path: str) -> None:
        """Load a WFM pair (order-independent — from_wfm auto-orients the clock)."""
        def after(sess, c=clock_path, d=data_path):
            self._status.setText(
                f"Opened {os.path.basename(c)} / {os.path.basename(d)}  "
                f"({sess.sample_rate_hz/1e6:.3f} MHz)")
        try:
            self._load_async(
                lambda **kw: Session.from_wfm(clock_path, data_path,
                                         register_map=self._rmap, **kw),
                after=after)
        except Exception as exc:  # noqa: BLE001 - surface ingest/decode errors
            QMessageBox.critical(self, "Open failed", str(exc))

    def _open_analog_csv(self, path: str, config_csv: str = "") -> None:
        """Load a Tektronix analog CSV export (all channels in one file), reached
        via Open Capture when the .csv is detected as analog. Same front-end as the
        .wfm importer: auto mid-rail + 10% hysteresis thresholding, and the clock
        auto-assigned by transition count when there are exactly two channels;
        with more than two the user picks which channel is clock and which is
        data (no busier-channel guess across unrelated channels)."""
        try:
            from ..ingest import analog_csv
            # Header-only read (preamble + TIME row) to enumerate channel names — the
            # bulk data is parsed once, asynchronously, inside Session.from_analog_csv
            # below; a full read_analog_csv here would parse the whole file twice.
            names = analog_csv.channel_names(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        if len(names) < 2:
            QMessageBox.warning(self, "Open Analog CSV", f"{path}: fewer than two channels")
            return
        if len(names) == 2:
            clock, data = names[0], names[1]      # order irrelevant — auto_clock decides
            clock_thresh = data_thresh = None
            auto_clock = True
        else:
            from .open_analog_csv_dialog import OpenAnalogCsvDialog
            dlg = OpenAnalogCsvDialog(self, channel_names=names)
            if not dlg.exec():
                return
            clock, data = dlg.clock, dlg.data
            clock_thresh, data_thresh = dlg.clock_thresh, dlg.data_thresh
            auto_clock = False                     # user chose which is which
        def after(sess, p=path, c=clock, d=data, auto=auto_clock):
            if auto:
                self._status.setText(
                    f"Opened {p}  (analog, clock auto-detected, "
                    f"{sess.sample_rate_hz/1e6:.3f} MHz)")
            else:
                self._status.setText(
                    f"Opened {p}  (analog, clock={c}, data={d}, "
                    f"{sess.sample_rate_hz/1e6:.3f} MHz)")
        try:
            self._load_async(
                lambda **kw: Session.from_analog_csv(path, clock=clock, data=data,
                                                clock_thresh=clock_thresh,
                                                data_thresh=data_thresh,
                                                auto_clock=auto_clock,
                                                register_map=self._rmap,
                                                config_csv=config_csv, **kw),
                after=after)
        except Exception as exc:  # noqa: BLE001 - surface ingest/decode errors
            QMessageBox.critical(self, "Open failed", str(exc))

    def locate_subcapture(self) -> None:
        """Analyzer ▸ Locate Sub-Capture: open a SECOND capture (any format the Open
        Capture dialog accepts — select BOTH files together for the two-file .bin /
        .wfm formats) and find every place its signal pattern occurs in the
        currently-open capture (Session.locate_subcapture — sample-rate independent).
        Drops a bookmark at each match's start_sample, labeled with its rank + score,
        and reports the count.

        The search itself (up to 4 FFT correlation passes — the orientation retry —
        over a possibly multi-million-UI capture) runs on a worker thread (see
        _LocateWorker below), with an indeterminate busy dialog, so a large capture
        (the demo is ~24M UIs) doesn't beach-ball the GUI thread for several seconds."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Locate sub-capture (.sal, .csv, .vcd, or both .bin/.wfm files)",
            self._last_capture_dir(),
            filter="Captures (*.sal *.csv *.bin *.vcd *.wfm);;Saleae project (*.sal);;"
                   "Digital CSV (*.csv);;Saleae binary (*.bin);;VCD (*.vcd);;"
                   "Tektronix waveform (*.wfm);;All files (*)")
        if not paths:
            return
        self._remember_capture_dir(paths[0], "analyzer")
        # Pair formats: when both channel files are selected together, use them as the
        # pair (no second-file prompt); otherwise fall back to a single primary path.
        path, second = paths[0], None
        for ext in (".wfm", ".bin"):
            same = [p for p in paths if p.lower().endswith(ext)]
            if len(same) >= 2:
                path, second = same[0], same[1]
                break
        try:
            import os as _os
            import sys as _sys
            import time as _t
            _t0 = _t.perf_counter()
            sub = self._load_bare_capture(path, second)
            if _os.environ.get("SWI3S_LOCATE_DEBUG"):
                _n = sub.clock_edges.size if sub is not None else 0
                _mn = self._session.capture.clock_edges.size if self._session else 0
                print(f"[locate] load sub ({os.path.basename(path)}): "
                      f"{_t.perf_counter() - _t0:.2f}s (sub clk={_n:,}, main clk={_mn:,})",
                      file=_sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - surface ingest errors
            QMessageBox.critical(self, "Locate failed", str(exc))
            return
        if sub is None:
            return    # user cancelled a channel-pick / second-file dialog — silent, no error

        dlg = QProgressDialog("Searching for sub-capture…", None, 0, 100, self)
        dlg.setWindowTitle("SWI3S Studio")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setCancelButton(None)         # the FFT search can't be interrupted mid-run
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        self._locate_dlg = dlg
        self._locate_thread = QThread(self)
        self._locate_worker = _LocateWorker(self._session, sub)
        self._locate_worker.moveToThread(self._locate_thread)
        self._locate_thread.started.connect(self._locate_worker.run)
        # Connect a BOUND METHOD (not a lambda): PySide can't resolve a bare lambda's
        # thread affinity, so it wires it as a DirectConnection — the slot would then run
        # on the WORKER thread, and _teardown_locate_thread's self._locate_thread.wait()
        # would be a thread waiting for ITSELF → permanent deadlock (beach-ball, spinner
        # never closes). A bound method of this QMainWindow gets a QueuedConnection onto
        # the GUI thread, like _LoadWorker.done. Path is stashed for the slot to read.
        self._locate_path = path
        self._locate_worker.progress.connect(self._on_locate_progress)
        self._locate_worker.done.connect(self._on_locate_done)
        self._locate_worker.failed.connect(self._on_locate_failed)
        self._locate_thread.start()
        dlg.show()

    def _on_locate_progress(self, percent: int, label: str) -> None:
        dlg = getattr(self, "_locate_dlg", None)
        if dlg is None:
            return
        if label:
            dlg.setLabelText(f"Searching for sub-capture — {label}")
        # A modal QProgressDialog.setValue() re-enters the event loop (processEvents),
        # which can dispatch the queued 'done' signal and tear the dialog down mid-call.
        # Operate on the LOCAL ref and do setValue LAST, so nothing dereferences
        # self._locate_dlg after it may have been set to None (the crash we saw).
        dlg.setValue(percent)

    def _on_locate_done(self, matches: list, diag: dict) -> None:
        self._finish_locate_subcapture(self._locate_path, matches, diag)

    def _finish_locate_subcapture(self, path: str, matches: list, diag: dict) -> None:
        self._teardown_locate_thread()
        if not matches:
            from ..analysis.subcapture import DEFAULT_SCORE_THRESHOLD
            best = float(diag.get("best_score", 0.0))
            best_start = diag.get("best_start_sample")
            placed = ""
            if best_start is not None:
                bm = self._bookmarks.add(int(best_start))
                bm.label = f"Best (no match)  score={best:.3g}"
                self._push_bookmarks()
                placed = ("\n\nA bookmark was placed at the best candidate so you "
                          "can inspect it.")
            QMessageBox.information(
                self, "Locate Sub-Capture",
                f"No occurrences of {os.path.basename(path)} found.\n\n"
                f"Best candidate scored {best * 100:.1f}% (needs "
                f"≥ {DEFAULT_SCORE_THRESHOLD * 100:.0f}% — only near-exact matches "
                f"count). A low best score means the pattern doesn't recur here; a "
                f"high one that still misses suggests a different acquisition "
                f"(sample rate / analog threshold) rather than a true occurrence."
                f"{placed}")
            self._status.setText(
                f"Locate Sub-Capture: no matches (best {best * 100:.1f}%)")
            return
        for i, m in enumerate(matches, start=1):
            bm = self._bookmarks.add(int(m["start_sample"]))
            bm.label = f"Sub {i}/{len(matches)}  score={m.get('score', 0):.3g}"
        self._locate_dbg("added %d bookmarks" % len(matches))
        self._push_bookmarks()
        self._locate_dbg("push_bookmarks")
        self._status.setText(
            f"Locate Sub-Capture: {len(matches)} match(es) bookmarked "
            f"from {os.path.basename(path)}")
        # No icon on the matches announcement (plain informational text).
        box = QMessageBox(self)
        box.setWindowTitle("Locate Sub-Capture")
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setText(f"Found {len(matches)} match(es); bookmarks placed at each "
                    f"start sample.")
        box.exec()

    def _locate_dbg(self, label: str) -> None:
        """SWI3S_LOCATE_DEBUG stage timing for the post-search (GUI-thread) result
        handling — pinpoints a hang that happens AFTER the search returns."""
        import os as _os
        if not _os.environ.get("SWI3S_LOCATE_DEBUG"):
            return
        import sys as _sys
        import time as _t
        now = _t.perf_counter()
        last = getattr(self, "_locate_dbg_t", None)
        self._locate_dbg_t = now
        if last is not None:
            print(f"[locate] {label}: {now - last:.2f}s", file=_sys.stderr, flush=True)

    def _on_locate_failed(self, message: str) -> None:
        self._teardown_locate_thread()
        QMessageBox.critical(self, "Locate failed", message)

    def _teardown_locate_thread(self) -> None:
        # Idempotent: the modal progress dialog's setValue() re-enters the event loop, so
        # 'done'/'failed' can fire more than once (or during a progress tick). Bail if the
        # dialog is already gone rather than dereferencing None.
        #
        # Guard the THREAD too, not just the dialog: join_worker_threads() (window close
        # / atexit) clears _locate_thread while a queued done/failed may still be in
        # flight, and wait() returning doesn't mean that signal was delivered.
        if getattr(self, "_locate_dlg", None) is None or \
           getattr(self, "_locate_thread", None) is None:
            self._locate_dlg = None
            self._locate_worker = None
            return
        self._locate_dlg.close()
        self._locate_thread.quit()
        self._locate_thread.wait()
        self._locate_worker.deleteLater()
        self._locate_dlg = None
        self._locate_thread = None
        self._locate_worker = None


    def _load_bare_capture(self, path: str, second: str = None):
        """Build a bare Capture (no Session/decode) from any of the formats Open
        Capture accepts, for Locate Sub-Capture's second file. Reuses the same
        per-format loaders as open_capture, picking clock/data the same way (auto
        for exactly two channels, else prompt) — but returns the Capture directly
        instead of wrapping it in a Session. `second` is the already-chosen second
        channel file for the two-file formats (.bin, .wfm); when omitted those
        formats prompt for it, so a lone pair file still works.

        Returns None if the user cancels a channel-pick / second-file dialog (the
        caller treats that as a silent no-op, matching _open_sal/_open_vcd/_open_csv);
        genuine ingest errors (bad file, wrong shape, …) still raise."""
        low = path.lower()
        if low.endswith(".sal"):
            from ..ingest import saleae_sal
            info = saleae_sal.read_info(path)
            if len(info.channels) < 2:
                raise ValueError(f"{path}: fewer than two digital channels")
            if len(info.channels) == 2:
                clk, dat = info.channels[0], info.channels[1]
            else:
                labels = [f"Channel {c}" for c in info.channels]
                picked = self._pick_two_channels("Sub-capture: .sal", info.channels, labels)
                if picked is None:
                    return None
                clk, dat = picked
            return saleae_sal.load_capture(path, clk, dat, auto_clock=(len(info.channels) == 2))
        if low.endswith(".vcd"):
            from ..ingest import vcd
            info = vcd.read_info(path)
            sigs = info.scalar_signals
            if len(sigs) < 2:
                raise ValueError(f"{path}: fewer than two 1-bit signals")
            idents = [s.ident for s in sigs]
            if len(sigs) == 2:
                clk, dat = idents[0], idents[1]
            else:
                labels = [s.name for s in sigs]
                picked = self._pick_two_channels("Sub-capture: .vcd", idents, labels)
                if picked is None:
                    return None
                clk, dat = picked
            return vcd.load_capture(path, clk, dat, auto_clock=(len(sigs) == 2))
        if low.endswith(".csv"):
            from ..ingest import analog_csv, digital_csv
            if analog_csv.is_analog_csv(path):
                # Analog Tek CSV: same front-end as an analog-CSV open — threshold
                # each channel (auto mid-rail + 10%) and auto-orient the clock.
                parsed = analog_csv.read_analog_csv(path)
                names = list(parsed["channels"].keys())
                if len(names) < 2:
                    raise ValueError(f"{path}: fewer than two channels")
                if len(names) == 2:
                    clk_name, dat_name = names[0], names[1]
                else:
                    picked = self._pick_two_channels(
                        "Sub-capture: analog CSV", names, names)
                    if picked is None:
                        return None
                    clk_name, dat_name = picked
                from ..ingest import analog
                return analog.capture_from_analog(
                    parsed["time"], parsed["channels"][clk_name],
                    parsed["channels"][dat_name], parsed["sample_rate_hz"] or None,
                    auto_clock=(len(names) == 2))
            header = digital_csv.read_header(path)
            cols = list(range(1, len(header)))
            if len(cols) < 2:
                raise ValueError(f"{path}: need >= 2 channel columns")
            labels = [header[i] for i in cols]
            counts = digital_csv.channel_transition_counts(path)
            clk = dat = None
            if len(counts) == len(cols) and counts:
                order = sorted(range(len(cols)), key=lambda i: counts[i], reverse=True)
                if counts[order[0]] > counts[order[1]]:
                    clk, dat = cols[order[0]], cols[order[1]]
            if clk is None:
                picked = self._pick_two_channels("Sub-capture: digital CSV", cols, labels)
                if picked is None:
                    return None
                clk, dat = picked
            return digital_csv.load_capture(path, clk, dat)
        if low.endswith(".bin"):
            data = second
            if not data:
                data, _ = QFileDialog.getOpenFileName(
                    self, "Sub-capture: second channel (Saleae binary)",
                    filter="Saleae binary (*.bin);;All files (*)")
            if not data:
                return None
            guess = saleae_binary.infer_sample_rate([path, data])
            if guess:
                rate = guess
            else:
                rate, ok = QInputDialog.getInt(
                    self, "Sample rate", "Sub-capture sample rate (Hz):",
                    98_304_000, 1, 2_000_000_000, 1_000_000)
                if not ok:
                    return None
            return saleae_binary.load_capture(path, data, rate)
        if low.endswith(".wfm"):
            # A WFM sub-capture is a pair (one analog channel per file), like .bin:
            # use the already-selected second file, else prompt. Auto-orient the clock.
            data = second
            if not data:
                data, _ = QFileDialog.getOpenFileName(
                    self, "Sub-capture: second channel (Tektronix .wfm)",
                    os.path.dirname(path),
                    filter="Tektronix waveform (*.wfm);;All files (*)")
            if not data:
                return None
            from ..ingest import analog
            from ..ingest import wfm as wfm_ingest
            a = wfm_ingest.read_wfm(path)
            b = wfm_ingest.read_wfm(data)
            return analog.capture_from_analog(a["time"], a["volts"], b["volts"],
                                              auto_clock=True)
        raise ValueError(f"{path}: unsupported sub-capture format")

    def export_capture(self) -> None:
        """File ▸ Analyzer ▸ Export Capture…: one dialog to pick the format (.sal /
        .bin / .csv), which signals + their names, the range (whole capture, or a
        time / bus-row / UI window), and the output file — then dispatch to the
        matching Session.export_* writer."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        from .capture_export_dialog import CaptureExportDialog
        clk_name, dat_name = ("DP", "DN") if self._session.is_dlv else ("SW_CLK", "SW_DATA")
        dlg = CaptureExportDialog(self._session, default_dir=self._last_capture_dir("analyzer"),
                                  clock_name=clk_name, data_name=dat_name, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        path = dlg.output_path()
        fmt = dlg.fmt()
        clk_name, dat_name = dlg.signal_names()
        clk_on, dat_on = dlg.include()
        rng = dlg.sample_range()
        self._remember_capture_dir(path, "analyzer")
        QApplication.setOverrideCursor(Qt.WaitCursor)     # deflate/write of a big capture is ~0.5s
        try:
            if fmt == "sal":
                self._session.export_sal(path, clock_channel=0, data_channel=1,
                                         clock_name=clk_name, data_name=dat_name, sample_range=rng)
                wrote = path
            elif fmt == "bin":
                written = self._session.export_bin(path, clock_channel=0, data_channel=1,
                                                   include_clock=clk_on, include_data=dat_on,
                                                   sample_range=rng)
                wrote = ", ".join(os.path.basename(p) for p in written)
            else:                                          # csv
                self._session.export_csv(path, clock_channel=0, data_channel=1,
                                         clock_name=clk_name, data_name=dat_name,
                                         include_clock=clk_on, include_data=dat_on, sample_range=rng)
                wrote = path
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        span = "" if rng is None else f"  (samples {rng[0]:,}–{rng[1]:,})"
        self._status.setText(f"Capture exported ({fmt}): {wrote}{span}")

    # ---- workspace save / load ----
    def save_workspace(self) -> None:
        if self._session is None:
            QMessageBox.information(self, "No session", "Load a capture first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save workspace",
            os.path.join(self._last_capture_dir("analyzer"), "workspace.swi3s.json"),
            "Workspace (*.json)")
        if not path:
            return
        self._remember_capture_dir(path, "analyzer")
        ws = Workspace(
            source=self._session.source,
            overlay=[[int(s), int(d), int(a), int(v)]
                     for (s, d, a), v in self._session.register_overrides.items()],
            bookmarks=self._bookmarks.to_json(),
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
        path, _ = QFileDialog.getOpenFileName(
            self, "Open workspace", self._last_capture_dir("analyzer"),
            "Workspace (*.json)")
        if not path:
            return
        self._remember_capture_dir(path, "analyzer")
        try:
            ws = Workspace.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        # Apply saved hub depths AND what-if register overrides inside the worker-
        # thread factory (both change the decode), in a single off-GUI-thread decode.
        # Doing it in apply_workspace (GUI thread) would re-decode again and freeze the UI.
        def factory(w=ws, **kw):
            # **kw absorbs decoder_ready (see _factory_takes_decoder_ready /
            # _LoadWorker.run) so the n/N decode-progress bar appears on workspace
            # reopen too, not just fresh opens. session_from_source itself doesn't
            # forward it through to Session.from_x yet (that's workspace.py, owned
            # elsewhere) — this just carries it as far as this factory can.
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
        self._bookmarks = BookmarkSet.from_json(ws.bookmarks)
        self._push_bookmarks()
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
        # NON-modal, deliberately: an ApplicationModal QProgressDialog spins a modal
        # event loop inside setValue(), during which the queued cross-thread `done`
        # signal is delivered and closes the dialog — ending the modal session from
        # within its own nested run loop ("modalSession has been exited prematurely —
        # reentrant call to endModalSession" on macOS). Non-modal defers `done` to the
        # normal event loop, so close() runs at the top level. Concurrent re-triggers are
        # already coalesced (_load_pending), and the GUI is busy/frozen during the decode
        # + view build regardless, so blocking input isn't what keeps the app coherent.
        dlg.setWindowModality(Qt.NonModal)
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
        self._load_worker.progress.connect(self._on_load_phase)   # live phase text
        # Poll decoder.progress_uis/.total_uis from the GUI thread while the C++ decode
        # runs on the worker thread — Decoder.run() releases the GIL (see bindings.cpp),
        # so this is safe concurrent read access, not a race on the interpreter. Starts
        # indeterminate (dlg was built with min==max==0) since the worker hasn't reached
        # decoder_ready yet; switches to an n/N bar once a decoder with a known total_uis
        # shows up, and falls back to indeterminate again if total_uis is 0 (unknown —
        # e.g. a streaming source that can't report a count up front).
        self._load_decode_done = False    # set once the "Reconstructing audio…" phase
                                          # arrives, so the n/N poll stops overwriting it
        self._load_decode_started = False
        self._load_progress_timer = QTimer(self)
        self._load_progress_timer.timeout.connect(self._poll_decode_progress)
        self._load_progress_timer.start(100)
        self._load_thread.start()
        dlg.show()

    def _on_load_phase(self, text: str) -> None:
        """_LoadWorker.progress: a phase label from the worker thread. The FIRST one
        ("reading transitions…") is emitted before the decode, while the n/N poll still
        owns the label; every later one means the decode itself finished (now building
        the audio store / measuring sections), so stop the poll from clobbering it."""
        if self._load_dlg is None:
            return
        if getattr(self, "_load_decode_started", False):
            self._load_decode_done = True   # any phase after the first ends the n/N poll
            # Decode is done, so the n/N bar is frozen at ~full. The post-decode phases
            # (reconstructing audio, measuring bus sections) have no progress metric and
            # can take tens of seconds on a large capture, so switch to an INDETERMINATE
            # (self-animating) busy bar — otherwise the full, motionless bar reads as a
            # hang. The heavy work runs on the worker thread over GIL-releasing numpy, so
            # the GUI stays free to animate.
            self._load_dlg.setRange(0, 0)
        self._load_decode_started = True
        self._load_dlg.setLabelText(text)

    def _poll_decode_progress(self) -> None:
        worker = getattr(self, "_load_worker", None)
        dlg = getattr(self, "_load_dlg", None)
        if worker is None or dlg is None or getattr(self, "_load_decode_done", False):
            return
        decoder = getattr(worker, "decoder", None)
        if decoder is None:
            return                                    # not yet past decoder_ready
        try:  # never let a progress poll break the load
            total = int(getattr(decoder, "total_uis", 0) or 0)
            done = int(getattr(decoder, "progress_uis", 0) or 0)
            if total > 0:
                # decoder.total_uis/progress_uis are 64-bit (a multi-minute capture can
                # exceed 2**31-1 UIs); QProgressDialog.setRange/setValue take a 32-bit
                # Qt int, so passing the raw counts through overflows on every poll tick
                # once total_uis > INT32_MAX. Drive the bar off a fixed 0..1000 permille
                # scale instead — always in int32 range regardless of capture size. The
                # progress is shown by the BAR only (no n/N/percent text — the label
                # keeps its phase string), so there's no redundant number to read.
                if dlg.maximum() != 1000:
                    dlg.setRange(0, 1000)
                dlg.setValue(min(1000, int(1000 * done / total)))
            elif dlg.maximum() != 0:
                dlg.setRange(0, 0)                      # unknown total => indeterminate
        except Exception:  # noqa: BLE001 - never let a progress poll break the load
            return

    def _finish_load(self) -> None:
        # Idempotent, and safe after join_worker_threads() has already torn the load
        # down: QThread.wait() returns when the worker's run() returns, which does NOT
        # mean its queued done/failed signal has been DELIVERED. Closing the window
        # mid-load nulls _load_thread here, then the queued signal lands and re-enters
        # this method — dereferencing the cleared attributes raised AttributeError
        # inside a Qt slot, so the dialog was never closed and the worker never
        # deleteLater'd. Bail when the state is already gone.
        timer = getattr(self, "_load_progress_timer", None)
        if timer is not None:
            timer.stop()
            self._load_progress_timer = None
        if getattr(self, "_load_thread", None) is None:
            self._load_dlg = None
            self._load_worker = None
            return
        if getattr(self, "_load_dlg", None) is not None:
            self._load_dlg.close()
        self._load_thread.quit()
        self._load_thread.wait()
        if getattr(self, "_load_worker", None) is not None:
            self._load_worker.deleteLater()
        self._load_dlg = None
        self._load_thread = None
        self._load_worker = None

    def _run_pending_load(self) -> None:
        """Start the most recent request that arrived while a decode was in flight."""
        if getattr(self, "_closing", False):
            self._load_pending = None     # window is going away; don't spawn a thread
            return
        pending = getattr(self, "_load_pending", None)
        if pending is not None:
            self._load_pending = None
            self._load_async(*pending)

    def _on_load_done(self, session, store, extras) -> None:
        after = self._load_after
        # Stop the decode-progress poll BEFORE any dialog work, then keep the busy dialog
        # up through load_session — building the views (command table, timeline, grid,
        # statistics) is heavy GUI-thread work that freezes the event loop. If the dialog
        # were closed first (as it used to be), close() only POSTS the hide, so the window
        # stays painted on screen showing the LAST decode phase ("Measuring bus sections…")
        # frozen for the whole build — looking hung on the wrong step. Instead relabel it
        # "Building views…" and force a synchronous repaint so the honest phase shows, then
        # build, then close.
        timer = getattr(self, "_load_progress_timer", None)
        if timer is not None:
            timer.stop()
            self._load_progress_timer = None
        dlg = getattr(self, "_load_dlg", None)
        if dlg is not None:
            dlg.setRange(0, 0)                 # indeterminate: the build has no metric
            dlg.setLabelText("Building views…")
            dlg.repaint()                      # paint NOW; the event loop is about to block
        # load_session (+ the optional after) runs on the GUI thread now that the
        # worker is joined. Guard it: an exception here would otherwise escape the
        # queued slot as an unhandled GUI-thread crash. Surface it like the
        # decode-failure path does.
        try:
            self.load_session(session, store, extras=extras)
            if after is not None:
                after(session)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.critical(self, "Load failed",
                                 f"The capture decoded but the views failed to load: {exc}")
        finally:
            self._finish_load()
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
        # A re-decode (SSP step, register/scrambler override, Port-Samples collect) mutates
        # the SAME session in place and reloads it, so `session is` the previous one — keep
        # the command filter across it. A genuinely NEW capture clears filters below.
        re_decode = (session is self._session) and self._session is not None
        self._session = session
        # Command table shows the decoded commands PLUS synthetic 'Commit Point' rows at
        # each confirmed sync-point commit's SSP (where it takes effect) — sorted in with
        # the commands. PROTOTYPE: these extra rows are table-only (not in session.commands),
        # so the timeline ticks, error counts and audio are unchanged. _starts/_kinds are
        # built from this augmented list so cursor-selection + filtering stay aligned.
        # session.commands is chronological (decoder emits in ascending start_sample) and
        # commit_point_rows() is a small, sorted set of table-only rows; merge the two sorted
        # streams (O(N+M)) instead of concatenating + re-sorting the whole list (O(N log N) +
        # a full copy). heapq.merge breaks ties by iterable order, so commands still precede a
        # commit row at the same start_sample — identical to the old stable sorted().
        table_cmds = list(heapq.merge(session.commands, session.commit_point_rows(),
                                      key=lambda c: int(c.get("start_sample", 0))))
        self._cmd_model = CommandTableModel(table_cmds, self._rmap,
                                            session.sample_rate_hz,
                                            peripheral_maps=session.peripheral_maps,
                                            row_origin=session.row_origin)
        self._cmd_proxy.setSourceModel(self._cmd_model)
        self._cmd_view.fit_columns()
        # Cursor-selection / arrow-nav anchor samples: each command's Row Sync Point (its
        # cursor sample), NOT the raw Column-0 closing edge — so a cursor parked on a
        # command's RSP resolves to THAT command (bisect in RSP space), matching where
        # command_cursor_sample / symbol / commit navigation place the cursor.
        self._starts = [session.command_cursor_sample(c) for c in table_cmds]

        kinds = sorted({c.get("command", "") for c in table_cmds} - {""})
        if not re_decode:
            self._kind_menu._fill([(k, k) for k in kinds])   # repopulate for this capture
            self._clear_all_filters()                        # don't carry a prior capture's filters
        # else: same capture re-decoded — keep the command filter (menu + proxy) intact.
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
        self._audio_view.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        self._audio_view.set_store(self._audio_store)
        self._export_action.setEnabled(not self._audio_store.is_empty())
        self._export_capture_action.setEnabled(True)     # a capture is now loaded
        self._build_decimation_menu()            # per-dataport decimation for this capture's streams
        self._symbol_view.set_sample_rate(session.sample_rate_hz)
        self._symbol_view.set_row_origin(session.row_origin)
        self._symbol_view.set_symbols(session.symbols())
        # DLV (PHY3): the Raw view collapses the complementary DP/DN pair to one
        # differential trace in the audio region and draws the recovered bit clock
        # beneath it; FBCSE passes no DLV info and keeps the two forwarded-clock traces.
        _dlv = session.is_dlv
        _rec = session.recovered_clock() if _dlv else None
        self._raw_view.set_capture(session.capture,
                                   dp_is_data_line=session.link_control.lc_on_data_line,
                                   dlv=_dlv, audio_start=session.audio_start_sample,
                                   recovered_clock=_rec,
                                   segments=session.segments if _dlv else None)
        # Nominal samples per bus row (row rate → samples), for the zoom-to-row button.
        _row_hz = (session.row_rate_khz or 0.0) * 1000.0
        self._raw_view.set_row_samples(session.sample_rate_hz / _row_hz if _row_hz else 0.0)
        # Samples per UI (max-zoom floor: don't let the user zoom in past ~1 UI).
        self._raw_view.set_ui_samples(
            session.sample_rate_hz / session.ui_rate_hz if session.ui_rate_hz else 0.0)
        self._raw_view.set_cds_provider(session.cds_column_samples)
        self._raw_view.set_commit_provider(session.commit_column_samples)
        # DLV: flag recovered Row Sync Points with no 0→1 edge on the wire (PLL lock gaps)
        # and raise the unlock banner when there are more than the session's threshold.
        self._raw_view.set_missing_provider(session.missing_rsp_samples)
        self._raw_view.set_pll_unlocked(session.pll_unlocked)
        self._raw_view.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        _store = self._audio_store
        _lanes = [(d, p, ch) for (d, p) in _store.streams() for ch in _store.channels(d, p)]
        self._raw_view.set_port_marks(session.port_bit_marks, _lanes)
        self._sample_view.set_sample_rate(session.sample_rate_hz)
        self._sample_view.set_row_origin(session.row_origin)
        self._sample_view.set_lanes(_lanes)
        self._sample_range = None          # (lo,hi) loaded row positions; None = needs (re)build
        self._sample_total = 0             # total filtered samples (full scrollable extent)
        self._sample_filters = None        # filters the loaded window was built with
        self._symbol_range = None          # (first,last) start_sample of the loaded CDS window
        self._eye_view.set_analysis_context(session.segments, session.timing_regions(),
                                            session.timing_column_roles)
        self._eye_view.set_capture(session.capture, recovered_clock=_rec)
        self._eye_view.set_sections(extras.get("sections")
                                    if extras.get("sections") is not None
                                    else session.section_ui_stats())   # per-section min/max UI
        self._timeline.set_sample_rate(session.sample_rate_hz)
        self._timeline.set_row_origin(session.row_origin)
        self._timeline.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        # capture_measurements iterates the whole audio store; Statistics is hidden at
        # load (Grid/Registers are raised), so defer the build to first show. It re-runs
        # on the next Statistics show after any re-decode (see _refresh_measurements).
        self._meas_dirty = True
        if self._meas_dock.isVisible():
            self._refresh_measurements()
        self._bookmarks.clear()
        self._push_bookmarks()
        # Remind the user which capture is loaded — but only in Analyzer mode (see
        # _update_title): the capture is irrelevant to the Visualizer/Timing views.
        self._update_title()

        total = max([c.get("end_sample", 0) for c in session.commands]
                    + [session.audio_end_sample, 1])
        self._timeline.set_events(session.commands, total)
        # Anchor each config-band boundary at its row's Row Sync Point (the same edge the
        # commit-point marker uses), so a region transition is COINCIDENT with the commit
        # that caused it instead of ~1 UI later (the segment's raw start_sample is the
        # Column-0 closing edge; row_base == the commit's effective_row).
        band_segs = []
        for i, seg in enumerate(session.segments):
            s2 = dict(seg)
            try:
                s2["start_sample"] = session._sample_at_bus_row(int(seg["row_base"]))
            except Exception:  # noqa: BLE001 - fall back to the raw start_sample
                pass
            # Mark regions whose width the user pinned, so the band reads "16col
            # (forced)" and is outlined rather than looking snooped.
            if i in getattr(session, "forced_column_sections", {}):
                s2["forced"] = True
            band_segs.append(s2)
        self._timeline.set_segments(band_segs, total)
        self._timeline.set_bringup(session.link_control)
        self._timeline.set_cursor(0)
        self._update_nav_starts()               # arrow-nav starts (all visible at load)
        self._timeline.set_commit_starts(self._sscr_samples())   # ⌘+arrow commit jump
        self._timeline.set_commit_points(session.commit_point_samples())   # dotted SSP markers

        n_err = count_errors(session.commands)
        lc = session.link_control
        phy = f" · {lc.label()}" if session.has_bringup else ""
        # FBCSE clock is DDR (UI rate / 2); DLV's recovered clock runs at the full bit rate.
        _clk_mhz = session.ui_rate_hz / (1e6 if session.is_dlv else 2e6)
        _clk_lbl = "rec clock" if session.is_dlv else "clock"
        self._status.setText(
            f"{len(session.commands)} commands ({n_err} errors) · "
            f"{session.audio_count} audio samples · {session.column_count} columns · "
            f"{_clk_lbl} {_clk_mhz:.3f} MHz · row rate {session.row_rate_khz:.1f} kHz"
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
        directory = QFileDialog.getExistingDirectory(
            self, "Export audio WAV files to…", self._last_capture_dir("analyzer"))
        if not directory:
            return
        self._remember_capture_dir(directory, "analyzer")
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
        path, _ = QFileDialog.getSaveFileName(
            self, "Export commands CSV",
            os.path.join(self._last_capture_dir("analyzer"), "commands.csv"), "CSV (*.csv)")
        if not path:
            return
        self._remember_capture_dir(path, "analyzer")
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
        grid_mode = "visualizer" if grid is self._viz_grid else "analyzer"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bus grid",
            os.path.join(self._last_capture_dir(grid_mode), "bus_grid.svg"),
            "SVG (*.svg);;PNG (*.png)")
        if not path:
            return
        self._remember_capture_dir(path, grid_mode)
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
        standalone Visualizer). Exports the config AS-OF the cursor — the segment the
        Bus Grid is currently showing — so a reconfiguring capture saves what you see
        (e.g. its 8-col phase) rather than always the final operational config."""
        if self._session is None:
            QMessageBox.information(self, "Export settings", "Load a capture first.")
            return
        cfg, n_enabled = BusConfig.from_decoder_config(
            self._session.config_dataports_at(self.cursor.sample))
        if n_enabled == 0 and not self._session.is_dlv:
            QMessageBox.information(self, "Export settings",
                                    "No data ports were decoded in this capture.")
            return
        src = self._session.source.get("path") or self._session.source.get("type", "capture")
        cfg.description = f"Exported from analyzer bus grid ({os.path.basename(str(src))})"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bus grid as Visualizer settings",
            os.path.join(self._last_capture_dir("visualizer"), "bus_grid_settings.csv"),
            "CSV (*.csv)")
        if not path:
            return
        self._remember_capture_dir(path, "visualizer")
        try:
            cfg.to_csv_file(path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        note = f"Exported {min(n_enabled, 12)} data port(s) to {path}"
        if n_enabled > 12:
            note += f" (capped at 12 of {n_enabled} visualizer columns)"
        self._status.setText(note)

    def open_grid_in_visualizer(self) -> None:
        """Open the analyzer's decoded bus grid (config as-of the cursor) DIRECTLY in the
        Visualizer — no Save-CSV / Open-CSV round trip. Loads the config into the
        authoring panel and switches to Visualization mode. Exports the segment the grid
        is currently showing (same cursor-aware config as Export Visualizer CSV)."""
        if self._session is None:
            QMessageBox.information(self, "View Bus Grid in Visualizer", "Load a capture first.")
            return
        cfg, n_enabled = BusConfig.from_decoder_config(
            self._session.config_dataports_at(self.cursor.sample))
        # A DLV capture still has a meaningful frame (its operational column geometry) even
        # with no config commit on the wire, so open it to show the frame; only a capture
        # with neither ports NOR geometry (e.g. a config-less FBCSE window) is a dead end.
        if n_enabled == 0 and not self._session.is_dlv:
            QMessageBox.information(self, "View Bus Grid in Visualizer",
                                    "No data ports were decoded in this capture.")
            return
        src = self._session.source.get("path") or self._session.source.get("type", "capture")
        cfg.description = f"From analyzer bus grid ({os.path.basename(str(src))})"
        self._authoring.set_config(cfg)
        self._authoring.set_file_status("From analyzer bus grid")
        if self._mode_mgr.current() != VISUALIZATION:
            self._mode_mgr.switch_to(VISUALIZATION)
        if n_enabled == 0:
            note = f"Opened DLV bus frame ({cfg.column_count()} columns, no data ports on the wire)"
        else:
            note = f"Opened bus grid ({min(n_enabled, 12)} data port(s)) in the Visualizer"
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

        def apply(s=self._session):
            s.set_register_override(sect, d, a, v)
            return s

        self._redecode_preserving(
            apply,
            f"Manual edit: section {sect} · Dev{d} 0x{a:04X} = 0x{v:02X} "
            f"(what-if — grid/registers/audio; re-decoded)")

    def _redecode_preserving(self, build_fn, status: str = "", *, after=None) -> None:
        """Re-decode off the GUI thread, preserving the cursor + bookmarks across the
        rebuild (a re-decode isn't a new capture — it shouldn't drop them), then set an
        optional status message. `build_fn(session)->session` mutates/returns the session
        to (re)load; `after()` runs extra per-caller restoration (e.g. audio zoom) before
        the status. Playback is stopped first: the worker mutates the live session that a
        running play timer would read cross-thread. Shared by the register-edit/clear,
        scrambler, hub-depth, and PDM re-decodes (the SSP nudge keeps its own variant that
        also holds the audio selection/zoom)."""
        self._audio_view.stop()
        keep_cursor = self.cursor.sample
        keep_bookmarks = self._bookmarks.copy()

        def done(_s, cur=keep_cursor, bms=keep_bookmarks):
            self._bookmarks = bms
            self._push_bookmarks()
            self._restore_cursor(cur)
            if after is not None:
                after()
            if status:
                self._status.setText(status)

        self._load_async(build_fn, after=done)

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
        def clear(s=self._session):
            s.clear_register_overrides()
            return s

        self._redecode_preserving(clear, "Manual register edits cleared; re-decoded.")

    def _on_command_selected(self, *_args) -> None:
        if self._syncing or self._session is None:
            return
        idx = self._cmd_view.currentIndex()      # cursor follows the CURRENT cell's row
        if not idx.isValid():
            return
        src = self._cmd_proxy.mapToSource(idx)
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
        """Ctrl+B: if a bookmark sits at the cursor, remove it; otherwise drop the next
        one in the A1, A2, B1, B2, … sequence (each pair measures a region)."""
        if self._session is None:
            return
        s = int(self.cursor.sample)
        tol = self._bookmark_tol()
        if not self._bookmarks.remove_at(s, tol=tol):
            bm = self._bookmarks.add(s)
            self._status.setText(f"Bookmark {bm.display()} @ {s:,}")
        else:
            self._status.setText(f"{len(self._bookmarks)} bookmark(s)")
        self._push_bookmarks()

    def _bookmark_tol(self) -> int:
        """Hit tolerance (samples) for removing/snapping a bookmark: ~half a UI so a
        Ctrl+B on an existing marker toggles it rather than stacking a second one."""
        try:
            spu = self._session.sample_rate_hz / self._session.ui_rate_hz
            return max(1, int(spu // 2))
        except Exception:                       # noqa: BLE001 — never break the toggle
            return 1

    def next_bookmark(self) -> None:
        bms = sorted(self._bookmarks.samples())
        if not bms:
            return
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in bms if s > cur), bms[0]))

    def prev_bookmark(self) -> None:
        bms = sorted(self._bookmarks.samples(), reverse=True)
        if not bms:
            return
        cur = self.cursor.sample
        self.cursor.set_sample(next((s for s in bms if s < cur), bms[0]))

    def clear_bookmarks(self) -> None:
        self._bookmarks.clear()
        self._push_bookmarks()

    def _push_bookmarks(self) -> None:
        """Refresh every bookmark consumer from the current set: the timeline, audio and
        raw panes (markers) and the Measurements pane (pair deltas)."""
        marks = [(b.sample, b.display()) for b in self._bookmarks]
        self._timeline.set_bookmarks(marks)
        for view in (self._audio_view, self._raw_view):
            if hasattr(view, "set_bookmarks"):
                view.set_bookmarks(marks)
        self._pair_view.set_rows(self._bookmark_measure_rows())

    def _bookmark_measure_rows(self) -> list:
        """Bookmark rows for the pane: per group, an A1 row, an A2 row (if it exists), and
        an A1-A2 delta row. A lone bookmark shows just its A1 row (no waiting for a pair).
        Each row is (label, UI, Row, Time, seek_sample)."""
        rows = []
        sess = self._session
        rate = float(getattr(sess, "sample_rate_hz", 0) or 0) if sess else 0.0
        ui_rate = float(getattr(sess, "ui_rate_hz", 0) or 0) if sess else 0.0
        spu = (rate / ui_rate) if (rate and ui_rate) else 0.0   # samples per UI

        def ui_of(s):
            return f"{s / spu:,.0f}" if spu else "—"

        def row_of(s):
            if sess is None:
                return "—"
            try:
                return f"{sess.display_row(sess.bus_row_for_sample(int(s))):,}"
            except Exception:                       # noqa: BLE001
                return "—"

        def time_of(s):
            return _fmt_duration(s / rate) if rate else f"{int(s):,} smp"

        # Group members by their stable group letter (items() is creation-ordered A1,A2,B1…).
        groups = {}
        for bm in self._bookmarks.items:            # `items` is a property (all bookmarks)
            groups.setdefault(bm.group, {})[bm.index] = bm
        for g in sorted(groups):
            a1 = groups[g].get(1)
            a2 = groups[g].get(2)
            if a1 is not None:
                rows.append((a1.display(), ui_of(a1.sample), row_of(a1.sample),
                             time_of(a1.sample), int(a1.sample)))
            if a2 is not None:
                rows.append((a2.display(), ui_of(a2.sample), row_of(a2.sample),
                             time_of(a2.sample), int(a2.sample)))
            if a1 is not None and a2 is not None:
                dsamp = a2.sample - a1.sample
                dt = _fmt_duration(dsamp / rate) if rate else f"{dsamp:,} smp"
                try:
                    drows = abs(sess.bus_row_for_sample(a2.sample)
                                - sess.bus_row_for_sample(a1.sample))
                    drows_s = f"{drows:,}"
                except Exception:                   # noqa: BLE001
                    drows_s = "—"
                dui = f"{dsamp / spu:,.0f}" if spu else "—"
                rows.append((f"{a1.display()}-{a2.display()}", dui, drows_s, dt,
                             int(a1.sample)))
        return rows

    def _on_bookmark_moved(self, label: str, sample: int, final: bool) -> None:
        """A bookmark was dragged in a pane. Move it (snapping to the nearest edge on
        release) and refresh all consumers so the pair measurement updates live."""
        target = next((b for b in self._bookmarks if b.display() == label), None)
        if target is None:
            return
        s = int(sample)
        if final and self._session is not None:
            s = self._snap_to_edge(s)
        self._bookmarks.move(target, s)
        self._push_bookmarks()
        if final:
            self.cursor.set_sample(s)

    def _snap_to_edge(self, sample: int) -> int:
        """Snap a dragged sample to the nearest clock edge (so a bookmark lands on a real
        UI boundary). Falls back to the raw sample if there are no edges."""
        try:
            ce = self._session.capture.clock_edges
            if len(ce) == 0:
                return int(sample)
            i = int(np.searchsorted(ce, sample))
            cands = [c for c in (i - 1, i) if 0 <= c < len(ce)]
            return int(min((int(ce[c]) for c in cands), key=lambda e: abs(e - sample)))
        except Exception:                       # noqa: BLE001 — snap is best-effort
            return int(sample)


    def _sscr_samples(self) -> list:
        """Cursor samples of the sync-point commit commands (SSCR / DSCR), ascending —
        the config commits at these, so they're the natural jump targets. Each is the
        command's Row Sync Point (command_cursor_sample), so a jump lands on the RSP
        marker like selecting the command in the table does, not one UI later."""
        if self._session is None:
            return []
        return sorted(self._session.command_cursor_sample(c) for c in self._session.commands
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
        # Defer the heavy cascade so clicking around the timeline doesn't stack a full
        # rebuild per click on the main thread. Capture the guard flags NOW: the deferred
        # handler must see the sync / from-pane state as it was at THIS cursor change, not
        # whatever it is 20 ms later (from_symbol/from_sample are reset synchronously right
        # after set_sample returns). Rapid changes overwrite the pending entry, so only the
        # resting cursor triggers a rebuild.
        self._pending_cursor = (int(sample), self._syncing, self._from_symbol,
                                self._from_sample)
        if MainWindow._event_loop_live:
            self._cursor_heavy_timer.start()      # coalesce; fires when clicking settles
        else:
            self._apply_cursor_heavy()            # synchronous in headless / tests

    def _apply_cursor_heavy(self) -> None:
        """The heavy half of the cursor cascade, coalesced by ``_cursor_heavy_timer``:
        re-render the raw view, replay the register files, re-window the CDS-symbol and
        Decoded-Samples tables, and rebuild the bus grid at the (resting) cursor. Skips
        any pane whose dock is hidden. Uses the guard flags captured when the cursor moved
        (see _on_cursor) so a cursor change that ORIGINATED in the symbol/sample pane
        doesn't re-window that pane out from under the click."""
        if self._session is None or self._pending_cursor is None:
            return
        sample, syncing, from_symbol, from_sample = self._pending_cursor
        self._raw_view.set_cursor(sample)
        # Rebuilding the register tree replays command history (C++) — skip it when the
        # Registers tab is hidden; _on_reg_dock_visible refreshes it when re-shown.
        if not self._reg_dock.isHidden():
            self._reg_view.set_files(self._session.register_files_at(sample))
        if not syncing:
            self._select_command_for_sample(sample)
        # Symbol viewer follows the cursor via windowed (seeked) re-decode, so it
        # works on big captures without scanning from the start. Skip if hidden, and
        # skip when the cursor change *came from* the symbol pane (a click / arrow
        # key) — re-windowing there would snap the selection back to the command's
        # comma, trapping navigation on the first symbols.
        if not self._symbol_dock.isHidden() and not from_symbol:
            self._refresh_symbols_around(sample)
        if self._sample_dock.isVisible() and not from_sample:
            self._refresh_samples_around(sample)
        # If what-if edits are active, keep the projected grid; else show the grid
        # as-of the cursor — it fills in as registers are written, and for a true
        # multi-width reconfiguration (cold-start 2col -> 8col) the per-segment
        # geometry follows the cursor. set_cells reads each cell's own column, so
        # the decoder's final column_count is the right axis width to pass.
        # In the scrollable TX map, geometry is scoped to the cursor's config region, so
        # a cursor move (to a different-width region) must re-anchor the window on the
        # cursor's row and resize the scrollbar to the new region. The grid rebuild is
        # heavy (full scene) — skip it when the Bus Grid tab is hidden; _on_grid_dock_visible
        # re-renders it when re-shown.
        if not self._grid_dock.isHidden():
            self._update_grid_for_cursor(sample)

    def _update_grid_for_cursor(self, sample: int) -> None:
        """The grid portion of the cursor cascade — TX-scrollbar re-anchor + grid render.
        Factored out so _on_grid_dock_visible can re-run it when the Bus Grid tab is
        raised (the per-tick call in _on_cursor is skipped while the tab is hidden)."""
        if self._tx_map and not self._tx_persist:
            self._tx_start_row = self._session.tx_row_for_sample(sample)
            self._update_tx_scrollbar()
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
        # Pre-audio COLD-start bring-up: there is no bus geometry AND no audio-mode column
        # structure on the wire yet (it's single-ended §5.1.2 LC signaling), so NEITHER the
        # config layout NOR the TX map is meaningful — rastering the raw bring-up
        # transitions into a column grid just shows a bogus 2/4-col bus. Gate both modes on
        # it, ABOVE the TX-map branch. (A warm start / mid-stream capture is configured from
        # row 0, so it falls through and shows the grid / TX map as normal.)
        lc = s.link_control
        if lc.sequence == "cold" and sample < s.audio_start_sample:
            # The selected PHY is only KNOWN once its number has been fully clocked — at
            # PhyStart (the final DP falling edge → audio-mode PHY). Before that point
            # nothing on the wire names a PHY, so don't claim one (the decoded result knows
            # the answer, but at THIS cursor it can't be known yet). After it — but before
            # audio starts — the PHY is fixed, just with no geometry yet.
            phy_known_at = lc.phystart_sample or lc.phy_select_sample or s.audio_start_sample
            if sample < int(phy_known_at):
                # Name the current bring-up sub-phase (Bus Reset / Cold Start / PhyStart)
                # for context — never the PHY, which isn't decoded yet at this cursor.
                phase = next((sec["name"] for sec in lc.sections
                              if int(sec.get("start", 0)) <= sample < int(sec.get("end", 0))),
                             "")
                self._grid_view.show_message(
                    "No PHY Selected",
                    f"Link bring-up in progress — {phase} (§5.1.2)" if phase
                    else "Link bring-up in progress (§5.1.2)")
            else:
                sl = f" — Safe-Lock-{lc.safe_lock_columns}" if lc.safe_lock_columns else ""
                self._grid_view.show_message(
                    f"{lc.phy_name or 'PHY'} Selected",
                    f"Audio not started yet{sl} (§5.1.2)")
            return
        if self._tx_map:                       # TX map replaces the layout with a raster
            if self._tx_persist:
                # Persistence: "did this column EVER toggle" within the CONFIGURATION
                # REGION under the cursor, shown as a fixed N-row summary (every row
                # identical). tx_persist_columns scans the WHOLE region (seconds on a long
                # audio region). Paint the Rows-To-Draw structure (right column count, row
                # labels, nothing lit) FIRST for an immediate response; if the region is
                # already memoised, fill it in synchronously; otherwise compute it on a
                # worker thread and fill in when it lands (see _TxPersistWorker). A stale
                # request (toggle off / cursor move / re-decode) is dropped via the token.
                n = max(1, self._grid_rows)
                cols = s.column_count_at(sample) or 1
                cds_col = int(getattr(s, "_cds_horizontal_start", 0))   # 2 for DLV, 0 for FBCSE
                placeholder = {"column_count": cols, "cds_col": cds_col,
                               "row_labels": list(range(n)),
                               "tx": np.zeros((n, cols), dtype=bool)}
                self._grid_view.set_tx_raster(placeholder)
                token = object()
                self._tx_persist_token = token
                cached = s.tx_persist_cached(sample)
                if cached is not None:
                    self._render_tx_persist(cached[0], int(cached[1]), token)   # instant
                else:
                    origin, _c, end_ui = s._tx_persist_key(sample)
                    if (end_ui - origin) <= _TX_PERSIST_SYNC_MAX_UIS:
                        # Small region (~<0.3s): compute inline — the thread overhead +
                        # async round-trip isn't worth it, and it keeps the common case
                        # (demo, short captures) synchronous.
                        lit, ccols = s.tx_persist_columns(sample)
                        self._render_tx_persist(lit, int(ccols), token)
                    else:
                        self._start_tx_persist_worker(s, sample, token)         # off-thread
            else:
                # Whole-capture, scrollable where-is-data view: render the Rows-To-Draw
                # window at the scrolled top row (0-based). Geometry follows the cursor's
                # region so an 8-col region shows 8 columns (matching persistence).
                raster = s.tx_raster(self._tx_start_row, self._grid_rows, sample)
                self._grid_view.set_tx_raster(raster)
            return
        self._grid_view.set_cells(s.grid_cells_at(sample, self._grid_rows),
                                  s.column_count_at(sample),
                                  dp_display=s.dp_display_map())

    def _render_tx_persist(self, lit, cols: int, token) -> None:
        """Paint the persistence raster (every drawn row = the collapsed lit pattern),
        unless a later cursor move / toggle / re-decode superseded this request."""
        s = self._session
        if (s is None or not self._tx_map or not self._tx_persist
                or getattr(self, "_tx_persist_token", None) is not token):
            return
        n = max(1, self._grid_rows)
        cds_col = int(getattr(s, "_cds_horizontal_start", 0))
        self._grid_view.set_tx_raster({"column_count": int(cols), "cds_col": cds_col,
                                       "row_labels": list(range(n)),
                                       "tx": np.broadcast_to(lit, (n, int(cols)))})

    def _start_tx_persist_worker(self, session, sample, token) -> None:
        """Compute the (uncached) persistence summary on a worker thread so the grid
        stays responsive. At most one worker runs; a request that arrives while one is in
        flight is remembered and started when it finishes (latest wins), so a fresh region
        still gets computed after a superseding cursor move.

        Never starts once the window is closing: join_worker_threads() has already
        waited on the workers, so a thread spawned after it would be destroyed while
        still running (Qt aborts on that)."""
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_tx_persist_thread", None) is not None:
            self._tx_persist_pending = (session, sample, token)     # latest wins
            return
        self._tx_persist_pending = None
        thread = QThread(self)
        worker = _TxPersistWorker(session, sample, token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_tx_persist_done)               # queued (cross-thread)
        worker.done.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._tx_persist_thread = thread
        self._tx_persist_worker = worker
        thread.start()

    def _on_tx_persist_done(self, lit, cols: int, token) -> None:
        # Worker finished (thread quits via its own done→quit connection). Clear the slot
        # so the next request can start, render if still current, then run any pending.
        self._tx_persist_thread = None
        self._tx_persist_worker = None
        self._render_tx_persist(lit, cols, token)
        pending = getattr(self, "_tx_persist_pending", None)
        if pending is not None:
            self._tx_persist_pending = None
            self._start_tx_persist_worker(*pending)

    def _toggle_tx_map(self, on: bool) -> None:
        """Switch the Bus Grid between the config layout and the TX-map raster (the
        'Show/Hide Toggles' button), then re-render. Persistence is only meaningful
        while toggles are shown; the scrollbar navigates the cursor's config region in
        TX mode (region-scoped so each region shows at its true width).
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
        total = (self._session.tx_total_rows(self.cursor.sample)
                 if self._session is not None else 0)
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
        keeping the cursor + bookmarks — same path as the Scrambler override, plus it
        holds the audio view's selection + zoom (the streams are unchanged, just
        re-phased) so you can compare sync as the phase steps."""
        if self._session is None:
            return
        # Captured before the re-decode; set_store would otherwise re-show every
        # dataport and refit to full-scale.
        keep_sel = self._audio_view.channel_selection()
        keep_zoom = self._audio_view.visible_index_range()
        r = int(row)

        def apply(s=self._session, r=r):
            s.set_ssp_row(r)
            return s

        def after(sel=keep_sel, zoom=keep_zoom, r=r):
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

        self._redecode_preserving(apply, after=after)

    def _refresh_symbols_around(self, sample: int) -> None:
        """Window the CDS symbol table around the cursor and highlight the nearest
        command's comma. Mirrors the Decoded-Samples fast path (_refresh_samples_around):
        moving the cursor within the already-decoded window just moves the highlight —
        skipping the C++ symbols_around re-decode AND the QTableWidget rebuild, which
        otherwise ran on EVERY cursor move even when nothing left the visible window."""
        if self._session is None:
            return
        rng = getattr(self, "_symbol_range", None)
        if rng is not None and rng[0] <= int(sample) <= rng[1]:
            self._select_symbol_for_sample(sample)   # cheap: just move the highlight
            return
        syms = self._session.symbols_around(sample)
        if not syms:                                  # keep the prior view if empty
            return
        self._symbol_view.set_symbols(syms)
        self._symbol_range = (int(syms[0]["start_sample"]), int(syms[-1]["start_sample"]))
        self._select_symbol_for_sample(sample)

    def _select_symbol_for_sample(self, sample: int) -> None:
        """Select the command's beginning CDS symbol (its SPM comma) and bring it to
        the TOP of the pane — the comma's bus row equals the command's bus_row. Shared
        by the full re-decode and the in-window fast path in _refresh_symbols_around."""
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
        earlier = self._session.symbols_before(int(earliest_sample))
        self._symbol_view.prepend_symbols(earlier)
        if earlier:
            rng = getattr(self, "_symbol_range", None)
            lo = int(earlier[0]["start_sample"])
            self._symbol_range = (lo, rng[1]) if rng is not None else (lo, lo)

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
            # A CDS symbol sits at Column 0, so anchor the cursor on its row's RSP (the
            # rising edge opening the row) — coincident with the CDS marker — not the
            # Column-0 closing edge one UI later (same fix as command / commit selection).
            rsp = self._session.row_sync_sample(int(sample))
            self.cursor.set_sample(rsp)           # updates timeline / registers / grid
            self._select_command_for_sample(rsp)
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
        at/just before it. The table holds a bounded window (see _SAMPLE_HALF_WINDOW);
        moving the cursor within the loaded window just moves the selection, moving
        outside it re-centres the window, and scrolling to either edge extends it
        (_on_sample_edge). `force` rebuilds regardless (first show / filter change)."""
        if self._session is None:
            return
        ports, chans, pred = self._sample_view.filters()
        filt = (tuple(map(tuple, ports or ())), tuple(chans or ()), pred)
        # Cheap path: cursor still inside the loaded window and filters unchanged.
        if (not force and getattr(self, "_sample_range", None) is not None
                and filt == getattr(self, "_sample_filters", None)):
            span = self._sample_view.shown_span()
            if span is not None and span[0] <= int(sample) <= span[1]:
                self._sample_view.select_sample(int(sample))   # cheap: just move selection
                return
        total = self._session.sample_marks_total(ports=ports, channels=chans, value_pred=pred)
        if total == 0:
            self._sample_view.set_samples([])
            self._sample_range = None
            self._sample_total = 0
            self._sample_filters = filt
            return
        pos = self._session.sample_position_of(int(sample), ports=ports,
                                               channels=chans, value_pred=pred)
        lo = max(0, pos - _SAMPLE_HALF_WINDOW)
        hi = min(total, pos + _SAMPLE_HALF_WINDOW)
        rows = self._session.samples_by_position(lo, hi, ports=ports,
                                                 channels=chans, value_pred=pred)
        self._sample_view.set_samples(rows)
        self._sample_view.select_sample(int(sample))
        self._sample_range = (lo, hi)
        self._sample_total = total
        self._sample_filters = filt

    def _on_sample_edge(self, direction: int) -> None:
        """User scrolled the Samples table to an edge: extend the loaded window by a chunk
        in that direction (+1 = later/bottom, -1 = earlier/top), if more samples exist.
        The view caps how many rows it keeps loaded (DecodedSampleView._MAX_LOADED),
        evicting an equal-size chunk from the opposite end when the extension would grow
        past that — `_sample_range` is shrunk on that side to match, so it stays the
        source of truth for what's ACTUALLY loaded (not just what was ever requested)."""
        if self._session is None or getattr(self, "_sample_range", None) is None:
            return
        lo, hi = self._sample_range
        total = getattr(self, "_sample_total", 0)
        ports, chans, pred = getattr(self, "_sample_filters", ((), (), None))
        ports = [tuple(p) for p in ports] or None
        chans = list(chans) or None
        if direction > 0 and hi < total:
            new_hi = min(total, hi + _SAMPLE_CHUNK)
            rows = self._session.samples_by_position(hi, new_hi, ports=ports,
                                                     channels=chans, value_pred=pred)
            evicted = self._sample_view.append_samples(rows)
            self._sample_range = (lo + evicted, new_hi)
        elif direction < 0 and lo > 0:
            new_lo = max(0, lo - _SAMPLE_CHUNK)
            rows = self._session.samples_by_position(new_lo, lo, ports=ports,
                                                     channels=chans, value_pred=pred)
            evicted = self._sample_view.prepend_samples(rows)
            self._sample_range = (new_lo, hi - evicted)

    def _on_sample_filters_changed(self) -> None:
        """Port / value filter changed: rebuild the window at the current cursor."""
        if self._session is not None and self._sample_dock.isVisible():
            self._refresh_samples_around(self.cursor.sample, force=True)

    def _on_sample_dock_visible(self, visible: bool) -> None:
        """Populate the (windowed) sample table when its tab is first shown/raised. NOT
        forced: _refresh_samples_around already rebuilds when the window is empty (first
        show) or the cursor moved outside it, and otherwise just re-selects — so
        re-selecting the tab doesn't repopulate ~8k rows every time."""
        if visible and self._session is not None:
            self._refresh_samples_around(self.cursor.sample)

    def _on_reg_dock_visible(self, visible: bool) -> None:
        """Refresh the register tree when its tab is shown/raised (its per-tick rebuild is
        skipped in _on_cursor while hidden)."""
        if visible and self._session is not None:
            self._reg_view.set_files(self._session.register_files_at(self.cursor.sample))

    def _on_grid_dock_visible(self, visible: bool) -> None:
        """Render the bus grid when its tab is shown/raised (its per-tick render is skipped
        in _on_cursor while hidden). Not during playback — that path defers heavy work."""
        if visible and self._session is not None and not self._audio_view.is_playing:
            self._update_grid_for_cursor(self.cursor.sample)

    def _refresh_measurements(self) -> None:
        """(Re)compute the Statistics rows from the whole capture, once, when marked dirty.
        capture_measurements walks the full audio store, so this is deferred until the
        Statistics tab is actually shown (and re-armed on every re-decode)."""
        if self._session is None or not self._meas_dirty:
            return
        # capture_measurements emits the Link Control section (timing + PM actions) first,
        # then Regions, Commands, Data Ports — each a collapsible header.
        self._meas_view.set_rows(capture_measurements(self._session, store=self._audio_store))
        self._meas_dirty = False

    def _on_meas_dock_visible(self, visible: bool) -> None:
        """Build the Statistics table on first show / after a re-decode (deferred from load
        so a hidden Statistics tab doesn't pay for a full-capture measurement pass)."""
        if visible:
            self._refresh_measurements()

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
        # opening a capture) — doing it inline froze the GUI for seconds. Keeps the
        # cursor + bookmarks; a re-decode isn't a new capture and shouldn't drop them.
        n = len(overrides)

        def rescramble(s=self._session, ov=overrides):
            s.set_scrambler_overrides(ov)
            return s

        self._redecode_preserving(
            rescramble, f"Scrambler override applied to {n} dataport(s); re-decoded.")

    # ---- config-vs-decoded comparison ----
    def _run_compare(self, cfg_path: str) -> None:
        """Compare an expected config (CSV path) against the decoded capture as a
        REGISTER-MAP diff: overlay the expected registers in blue in the Register
        Map (alongside the green values snooped from the bus) and report which
        registers differ. Reached from Analyzer ▸ Import Visualizer CSV (a CSV file or
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
        def rehub(s=self._session, d=dict(new_depths)):
            s.set_hub_depths(d)
            return s

        self._redecode_preserving(rehub, "Hub depths applied; re-decoded.")

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

        def rebuild(s=self._session):
            return s                              # same decode; load_session rebuilds
        #                                           the store with self._pdm_dc_block

        self._redecode_preserving(
            rebuild,
            f"PDM DC bias block {'on' if self._pdm_dc_block else 'off'}; audio re-decoded.")

    def _accepted_src_rows(self) -> list:
        """Sorted SOURCE rows currently accepted by the command filter, cached (dropped
        on any filter/model change). Lets _select_command_for_sample bisect to the nearest
        visible command instead of probing mapFromSource across a large filtered gap."""
        if self._accepted_src_cache is None:
            p = self._cmd_proxy
            rows = [p.mapToSource(p.index(r, 0)).row() for r in range(p.rowCount())]
            rows.sort()
            self._accepted_src_cache = rows
        return self._accepted_src_cache

    def _select_command_for_sample(self, sample: int) -> None:
        if not self._starts:
            return
        idx = bisect.bisect_right(self._starts, sample) - 1
        if idx < 0:
            idx = 0
        # The exact nearest command may be filtered out of the table. Fall back to the
        # nearest VISIBLE command: the closest accepted row at/before the cursor (the
        # "in effect" command), else the first accepted row after it. Bisect the cached
        # sorted accepted-source-row list rather than walk mapFromSource per row.
        accepted = self._accepted_src_rows()
        if not accepted:
            return                                  # nothing visible under the filter
        pos = bisect.bisect_right(accepted, idx) - 1
        src_row = accepted[pos] if pos >= 0 else accepted[0]
        proxy_idx = self._cmd_proxy.mapFromSource(self._cmd_model.index(src_row, 0))
        if not proxy_idx.isValid():
            return
        self._syncing = True
        try:
            # Current cell first (it selects just that cell), THEN select the whole row
            # so the row highlight isn't clobbered — the row reads as selected and the
            # current cell sits on it for keyboard nav.
            self._cmd_view.setCurrentIndex(proxy_idx)
            self._cmd_view.selectRow(proxy_idx.row())
            # Pin the nearest command to the TOP of the viewport. The default
            # EnsureVisible hint scrolls the minimum needed, so the selected row lands
            # at the top when scrolling down but the bottom when scrolling up —
            # inconsistent as the cursor moves. PositionAtTop is deterministic.
            self._cmd_view.scrollTo(proxy_idx, QAbstractItemView.ScrollHint.PositionAtTop)
        finally:
            self._syncing = False
