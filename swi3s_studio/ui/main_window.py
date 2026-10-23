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
import contextlib
import functools
import heapq
import inspect
import math
import os
import pathlib
import re
import sys
import tempfile
from typing import Any, Dict, Optional

import numpy as np
import swi3score
from PySide6.QtCore import QEvent, QEventLoop, QObject, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
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
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
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
    QStyle,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from .. import __version__, import_notes
from ..analysis import (
    capture_measurements,
    count_errors,
    register_diff,
    register_diff_report,
)
from ..export import write_commands_csv, write_links_commands_csv
from ..ingest import saleae_binary, transitions
from ..links import LinkSet
from ..model import RegisterMap, viz_engine
from ..model.bookmarks import BookmarkSet
from ..model.bus_config import BusConfig, cds_symbol, demo_config
from ..session import Session
from ..workspace import (
    DEMO_WORKSPACES,
    WORKSPACE_SUFFIX,
    LinkSpec,
    Workspace,
    demo_workspace,
    session_from_source,
)
from . import line_style, timers, view_state
from .audio_view import AudioView
from .authoring import AuthoringPanel
from .command_table import (
    COLUMN_NAMES,
    AllLinksCommandModel,
    CommandFilterProxy,
    CommandTableModel,
    CommandTableView,
)
from .cursor import TimeCursor
from .decoded_sample_view import DecodedSampleView
from .eye_view import EyeView
from .grid_view import GridView
from .link_lanes import LinkLanes
from .link_state import LinkPanelState
from .measurements_view import MeasurementsView
from .mode_controller import ANALYSIS, MODES, TIMING, VISUALIZATION, ModeManager
from .pair_measure_view import PairMeasureView
from .port_names import port_label, with_name
from .raw_view import RawCaptureView
from .register_view import RegisterView
from .symbol_view import SymbolView
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
from .timeline import TimelineRibbon
from .timing_view import TimingView

# The pane groups that each follow their OWN Link: the Commands table, the right-hand group
# (Registers, Statistics), the Bus Grid, and the bottom group (Capture, CDS, Audio, Samples,
# Timing). The Bus Grid is a group of its own because it stays one Link at a time (two
# grids do not fit across) while the bottom group can show every Link. Bookmarks and the
# Timeline span every Link, so they belong to none.
_PANE_GROUPS = ("commands", "right", "grid", "bottom")
_ALL_LINKS = "All Links"   # the Commands scope that merges every Link, in the switcher
# View ▸ Maximize Pane: the selected pane alone in the window, and back. Ctrl is ⌘ on macOS,
# where ⌘M is the system's Minimize.
_MAXIMIZE_KEY = "Ctrl+Shift+M"
_MAXIMIZE_KEY_TEXT = "⌘⇧M" if sys.platform == "darwin" else _MAXIMIZE_KEY
# The panes read by moving through time keep the Timeline when maximized, to navigate with;
# the others (Audio, Capture and Timing have their own time axes, Statistics and Bookmarks
# none to follow) take the whole window. "commands" is the Commands table; the rest name
# MainWindow's docks.
_MAXIMIZE_KEEPS_TIMELINE = ("commands", "_reg_dock", "_grid_dock", "_symbol_dock",
                            "_sample_dock")
_QWIDGETSIZE_MAX = 16777215    # Qt's "no limit" for a widget's size, which PySide6 does not export
# Two Links count as running at "the same" UI / row rate (so a cross-Link bookmark pair can
# be read in UIs and rows) within 0.1%: far above two analyzers' timebase error, far below
# the gap between distinct bus rates.
_SAME_RATE_REL_TOL = 1e-3
_MAX_OFFSET_PS = 10 ** 15   # ±1000 s: the Set Time Offset dialog's range, in every unit
DEFAULT_GRID_ROWS = 64   # Bus Grid rows to draw (config layout + TX raster); 64 is a
                         # common payload repeat interval. User-settable per capture.

# TX-map persistence scans a whole config region. Below this many UIs the scan is short
# enough to run inline; above it, run it on a worker thread so the grid doesn't freeze.
#
# The scan measures ~32 ns/UI. The bound is the INLINE BUDGET: what may run between a
# cursor move and a repaint, not what the scan can manage. Keep it small enough that a miss
# is invisible; the async path is the normal route for everything else.
# test_perf.test_tx_persist_inline_threshold_stays_inline holds the constant to a measured
# ceiling, so raising it fails the gate.
_TX_PERSIST_SYNC_MAX_UIS = 2_000_000


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
#
# Sized against the REBUILD, not the scroll. A cursor move that leaves the window re-centres
# it, discarding every QTableWidgetItem and building 2 * half * len(_COLS) fresh ones; a move
# INSIDE the window is only a select_sample and costs nothing (_refresh_samples_around). A
# screenful is ~40 rows, so several hundred absorbs ordinary arrow stepping without a
# rebuild. Do NOT raise these to buy fewer rebuilds: the rebuild is O(window) and the size
# only buys a *chance* of staying in.
# test_perf.test_decoded_samples_rebuild_ceiling holds the ceiling and the bound on `half`.
_SAMPLE_HALF_WINDOW = 500      # rows loaded on each side of the cursor initially
_SAMPLE_CHUNK = 1000           # rows added per scroll-to-edge


def _demo_samples() -> int:
    """Audio samples/channel for the demo capture: 1 s @ 48 kHz (48000) by default.
    Tests set SWI3S_DEMO_SAMPLES small so a demo load stays fast (a 1 s demo is ~24.6M
    UIs, ~2.5 s to decode)."""
    try:
        return max(1, int(os.environ.get("SWI3S_DEMO_SAMPLES", "48000")))
    except ValueError:
        return 48000


def _processing_from_json(rows) -> dict:
    """A workspace's [device, dp, {...}] stream settings as {(device, dp): StreamProcessing},
    skipping any row that does not read as one (a hand-edited or damaged workspace)."""
    from ..store.audio_store import StreamProcessing
    out = {}
    for row in rows or []:
        try:
            dev, dp, d = int(row[0]), int(row[1]), row[2]
        except (TypeError, ValueError, IndexError):
            continue
        proc = StreamProcessing.from_json(d)
        if proc is not None and not proc.is_identity():
            out[(dev, dp)] = proc
    return out


def _processing_rows_unread(rows) -> int:
    """How many of a workspace's [device, dp, {...}] Filter & Gain rows _processing_from_json
    cannot read (a row whose settings read as no processing is not counted)."""
    from ..store.audio_store import StreamProcessing
    n = 0
    for row in rows or []:
        try:
            int(row[0]), int(row[1])
            ok = StreamProcessing.from_json(row[2]) is not None
        except (TypeError, ValueError, IndexError):
            ok = False
        n += not ok
    return n


def _note_authoring_keys(d: dict, source: str) -> None:
    """Report the keys of a workspace's Visualizer config (BusConfig.to_dict) that
    BusConfig.from_dict does not read, and data ports past the twelfth."""
    from dataclasses import fields as dc_fields

    from ..model.bus_config import NUM_DATA_PORTS, DataPortConfig
    known = set(BusConfig._IFACE_ATTRS) | {"dataports", "cds_guard_enabled",
                                           "cds_guard_polarity", "cds_tail_width"}
    unknown = sorted(str(k) for k in d if k not in known)
    dps = d.get("dataports") or []
    dp_known = {f.name for f in dc_fields(DataPortConfig)}
    dp_unknown = sorted({str(k) for dd in dps[:NUM_DATA_PORTS] if isinstance(dd, dict)
                         for k in dd if k not in dp_known})
    if unknown or dp_unknown:
        import_notes.warn(source, "Visualizer config: settings this version does not read, "
                                  "ignored: " + ", ".join(unknown + [f"data port {k}"
                                                                     for k in dp_unknown]))
    if len(dps) > NUM_DATA_PORTS:
        import_notes.warn(source, f"Visualizer config: {len(dps)} data ports saved; the "
                                  f"{len(dps) - NUM_DATA_PORTS} past the {NUM_DATA_PORTS}th "
                                  f"are not read")


def _widths_by_name(widths, names) -> dict:
    """Saved Commands column widths as {column name: width}: from a dict by name, or from a
    positional list only when it has one entry per name. Entries that are not a positive
    int are dropped; anything else gives {}."""
    if isinstance(widths, dict):
        pairs = list(widths.items())
    elif isinstance(widths, list) and len(widths) == len(names):
        pairs = list(zip(names, widths))
    else:
        return {}
    return {str(k): int(w) for k, w in pairs
            if isinstance(w, int) and not isinstance(w, bool) and w > 0}


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


def _authored_slot_names(cfg) -> dict:
    """{slot index: user-assigned port name} for the Bus-Visualizer colour key. Skips the
    default "DP{i}" placeholder so the key falls back to the device-qualified number,
    which identifies the port where a bare "DP0" cannot (several devices may each own a
    DP0). Best-effort: a config shape without names just yields an empty map."""
    names = {}
    try:
        for i, dp in enumerate(cfg.dataports):
            name = (getattr(dp, "name", "") or "").strip()
            if name and name != f"DP{i}":
                names[i] = name
    except Exception:  # noqa: BLE001 — the key degrades to numbers; never break the render
        return {}
    return names


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
    audio store (per-channel arrays + render pyramids) is built here too. The
    per-section UI stats (numpy over millions of clock edges, ~0.3s) are precomputed
    here as well so load_session doesn't recompute them on the GUI thread.

    The work runs inside an import_notes collector opened on this thread; the warnings it
    gathers go back in `extras["import_warnings"]` and on `session.import_warnings`."""
    done = Signal(object, object, object)   # (session, audio_store, extras dict)
    failed = Signal(str)
    progress = Signal(str)                  # human phase label for the busy dialog

    def __init__(self, factory) -> None:
        super().__init__()
        self._factory = factory
        self.decoder = None   # set via decoder_ready, BEFORE run() blocks on the decode —
                               # the GUI thread polls .progress_uis/.total_uis off this while
                               # the C++ decode runs here (it releases the GIL; see Session)

    def run(self) -> None:
        with import_notes.collect() as notes:
            out = self._run()
        if out is None:
            return
        session, store, extras = out
        extras["import_warnings"] = list(notes)
        try:
            session.import_warnings = list(notes)
        except AttributeError:
            pass
        self.done.emit(session, store, extras)

    def _run(self):
        try:
            self.progress.emit("Decoding capture — reading transitions…")
            # Thread the decoder out via decoder_ready BEFORE the blocking decode, so the
            # GUI thread can poll .progress_uis/.total_uis while run() executes here (it
            # releases the GIL). Most factories (re-decode helpers like `apply`/`rescramble`,
            # and the `lambda: Session.from_x(...)` open-capture ones) take no arguments at
            # all, so only pass decoder_ready through when the factory declares it.
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
            store = session.audio_store()
            self.progress.emit("Measuring bus sections…")
            extras = {"sections": session.section_ui_stats()}
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via `failed`
            self.failed.emit(str(exc))
            return None
        return session, store, extras

    def _on_decoder_ready(self, decoder) -> None:
        self.decoder = decoder


class _TxPersistRequest:
    """One Bus Grid persistence request: the token a result is matched against (by
    identity), carrying the CDS column of the Link the grid showed when it was asked for."""
    __slots__ = ("cds_col",)

    def __init__(self, cds_col: int) -> None:
        self.cds_col = int(cds_col)


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
    for the orientation retry) off the GUI thread, like _LoadWorker above. On a large
    capture (the demo is ~24M UIs) this search is multi-second."""
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
    min); reporting only the visible page's hints keeps that height out of the
    Analysis layout, so the bottom docks stay resizable."""

    def sizeHint(self):  # noqa: N802 - Qt override
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self):  # noqa: N802 - Qt override
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()



def _title_button_style() -> str:
    """A title bar's ✕ / ⤢ buttons: no frame at rest, a subtle rounded tint on hover, and
    on a maximize button while its pane is the maximized one. The docks' title bars and the
    Commands pane's header share it."""
    t = VizTheme
    return (f"QToolButton {{ border:none; background:transparent; color:{t.TEXT_DIM};"
            f" font-size:12px; padding:0; }}"
            f"QToolButton:hover, QToolButton:checked {{ background:{t.BORDER};"
            f" color:{t.TEXT}; border-radius:{t.CORNER_RADIUS}px; }}")


def _maximize_button(parent: QWidget, size: int) -> QToolButton:
    btn = QToolButton(parent)
    btn.setText("⤢")
    btn.setCheckable(True)
    btn.setFixedSize(size, size)
    btn.setCursor(Qt.ArrowCursor)
    btn.setFocusPolicy(Qt.NoFocus)
    _show_maximized(btn, False)
    return btn


def _show_maximized(btn: QToolButton, on: bool) -> None:
    """Show on a maximize button whether its pane is the maximized one."""
    btn.setChecked(bool(on))
    btn.setToolTip(f"Restore Panes ({_MAXIMIZE_KEY_TEXT})" if on
                   else f"Maximize Pane ({_MAXIMIZE_KEY_TEXT})")


class _PaneTitle(QLabel):
    """The Commands pane's title, which has no dock title bar: a double-click on it
    maximizes the pane, as one on a dock's title does."""
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt override
        if ev.button() == Qt.LeftButton:
            ev.accept()
            self.doubleClicked.emit()
            return
        super().mouseDoubleClickEvent(ev)


class _DockTitleBar(QWidget):
    """A themed replacement for the native QDockWidget title bar.

    Owning the title bar bypasses the platform style (QSS cannot restyle the native
    macOS dock buttons): a centered title label plus a close button (a plain
    QToolButton styled directly). Dragging still works —
    QDockWidget drives it off mouse events on the title-bar widget, which the
    label/spacers let propagate. A double-click maximizes the pane (or puts the panes
    back), as the button on the right does, and never floats it: a pane is floated by
    dragging it out. `on_close` replaces the plain close, so the window can put the panes
    back before one goes.

    A pane tabbed with others shows the group's tabs in place of its title, so one row
    names the pane in front and picks another (set_tabs); the window hides the dock
    area's own tab bar (_sync_pane_tabs)."""

    tabPicked = Signal(str)     # the window title of the pane whose tab was clicked

    _BTN = 18   # button box (px); close on the left, maximize on the right, title centred

    def __init__(self, dock: QDockWidget, title: str, on_maximize=None,
                 on_close=None) -> None:
        super().__init__(dock)
        self._dock = dock
        self._on_maximize = on_maximize
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(0)
        self._close = QToolButton(self)
        self._close.setText("✕")
        self._close.setFixedSize(self._BTN, self._BTN)
        self._close.setCursor(Qt.ArrowCursor)
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.setToolTip("Close")
        on_close = on_close or dock.close
        self._close.clicked.connect(lambda: on_close())    # not `checked` as an argument
        lay.addWidget(self._close)         # left, matching the native macOS dock button
        self._lay = lay
        lay.addStretch(1)
        self._label = QLabel(title, self)
        self._label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._label)
        self._tabs = QTabBar(self)
        self._tabs.setObjectName("paneTabs")
        self._tabs.setDrawBase(False)
        self._tabs.setExpanding(False)
        self._tabs.setFocusPolicy(Qt.NoFocus)
        self._tabs.setUsesScrollButtons(True)
        self._tabs.tabBarClicked.connect(self._on_tab_clicked)
        self._tabs.hide()
        lay.addWidget(self._tabs)
        lay.addStretch(1)
        self._max = _maximize_button(self, self._BTN)   # balances the close button
        if on_maximize is not None:
            self._max.clicked.connect(lambda: on_maximize())
        else:                # a pane that is never maximized (Timeline)
            keep = self._max.sizePolicy()
            keep.setRetainSizeWhenHidden(True)  # still balances the close button
            self._max.setSizePolicy(keep)
            self._max.hide()
        lay.addWidget(self._max)
        self.retheme()

    def set_maximized(self, on: bool) -> None:
        """Show whether this pane is the maximized one (the button reads Restore)."""
        _show_maximized(self._max, on)

    def set_tabs(self, titles: list[str]) -> None:
        """Show `titles` as tabs, this pane's selected, in place of the title; with fewer
        than two, the title alone."""
        tabbed = len(titles) > 1
        if tabbed and self.tab_titles() != titles:
            self._tabs.blockSignals(True)
            while self._tabs.count():
                self._tabs.removeTab(0)
            for title in titles:
                self._tabs.addTab(title)
            self._tabs.blockSignals(False)
        if tabbed:
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(titles.index(self._dock.windowTitle()))
            self._tabs.blockSignals(False)
        self._tabs.setVisible(tabbed)
        self._label.setVisible(not tabbed)

    def tab_titles(self) -> list[str]:
        """The tabs shown, left to right; empty when the title is shown instead."""
        if self._tabs.isHidden():
            return []
        return [self._tabs.tabText(i) for i in range(self._tabs.count())]

    def _on_tab_clicked(self, index: int) -> None:
        if index >= 0:
            self.tabPicked.emit(self._tabs.tabText(index))

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt override
        if ev.button() == Qt.LeftButton:
            ev.accept()                        # QDockWidget would float the pane
            if self._on_maximize is not None:
                self._on_maximize()
            return
        super().mouseDoubleClickEvent(ev)

    def retheme(self) -> None:
        t = VizTheme
        self.setStyleSheet(f"background:{t.FRAME_BG};")
        self._label.setStyleSheet(f"background:transparent; color:{t.TEXT};")
        # The buttons are ours (not the native dock buttons), so QSS applies cleanly.
        self._close.setStyleSheet(_title_button_style())
        self._max.setStyleSheet(_title_button_style())

    def add_leading(self, widget: QWidget) -> None:
        """Put `widget` just right of the close button (the per-group Link picker)."""
        self._lay.insertSpacing(1, 6)
        self._lay.insertWidget(2, widget)

    def setTitle(self, title: str) -> None:  # noqa: N802 - Qt-style name
        self._label.setText(title)


@atexit.register
def _join_main_window_threads_at_exit() -> None:
    """Join every live MainWindow's worker threads before the interpreter tears down.

    Qt calls abort() when a QThread is destroyed while still running, so any window
    that goes away without a closeEvent — a headless script that builds a MainWindow
    and simply ends — would end the process with SIGABRT (exit 134).

    Registered at import so it covers every entry point; closing a window normally
    makes this a no-op.
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


def _on_grid_link(method):
    """Run a Bus Grid handler as if the grid group's Link were the active one: the grid
    code reads `self._session` and `self.cursor`, which are the active Link's, and the grid
    shows its own (see _PANE_GROUPS). Nesting is free (`_as_link` of the active Link is a
    no-op)."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self._as_link(self._pane_link["grid"]):
            return method(self, *args, **kwargs)
    return run


class _BusyDialog(QProgressDialog):
    """A busy dialog that only its owner closes. QProgressDialog closes on Escape or its
    title-bar button even with no cancel button, and the probe's dialog (_probe_files) is
    what keeps the window from being used while a nested event loop runs."""

    def __init__(self, text: str, title: str, parent) -> None:
        super().__init__(text, None, 0, 0, parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.ApplicationModal)
        self.setCancelButton(None)
        self.setMinimumDuration(0)
        self._done = False

    def finish(self) -> None:
        self._done = True
        self.close()

    def reject(self) -> None:  # Escape
        if self._done:
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._done:
            super().closeEvent(event)
        else:
            event.ignore()


class _EmptyHint(QLabel):
    """A note centred over a pane while no capture is open, so the empty Analyzer says
    what to do rather than showing blank tables and axes. It lets the mouse through and
    follows its pane's size."""

    def __init__(self, target: QWidget, text: str) -> None:
        super().__init__(text, target)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.retheme()
        target.installEventFilter(self)
        self.setGeometry(target.rect())

    def retheme(self) -> None:
        self.setStyleSheet(f"color:{VizTheme.TEXT_DIM}; background:transparent; "
                           "font-size:13px;")

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False


class _PaneTouchFilter(QObject):
    """Makes the Link of the pane group being worked in the ACTIVE Link, before the click
    or key reaches the pane — so every existing handler (seek, select, edit) and every
    menu action acts on the Link that pane shows, without each needing to know.

    ONE per application, and only while some window has two or more Links: an
    application filter costs a Python call on every event of every object. With a
    single Link nothing needs it."""

    _installed = None
    _users: set = set()                   # id()s of windows currently with >= 2 Links

    @classmethod
    def need(cls, window, on: bool) -> None:
        """Window `window` does (or no longer does) have two or more Links. A window that
        is destroyed without being closed is forgotten too, through its `destroyed`
        signal, so it cannot leave the filter installed for good."""
        key = id(window)
        if on and key not in cls._users:
            cls._users.add(key)
            window.destroyed.connect(lambda *_args, k=key: cls._forget(k))
        elif not on:
            cls._users.discard(key)
        cls._settle()

    @classmethod
    def _forget(cls, key: int) -> None:
        cls._users.discard(key)
        cls._settle()

    @classmethod
    def _settle(cls) -> None:
        """Install the filter while anyone needs it, remove it once nobody does."""
        app = QApplication.instance()
        if app is None:
            return
        if cls._users and cls._installed is None:
            cls._installed = cls(app)
            app.installEventFilter(cls._installed)
        elif not cls._users and cls._installed is not None:
            app.removeEventFilter(cls._installed)
            cls._installed.deleteLater()
            cls._installed = None

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        if event.type() in (QEvent.MouseButtonPress, QEvent.FocusIn) \
                and isinstance(obj, QWidget):
            # The PARENT chain, not window(): a floating dock is its own top-level window,
            # but it still belongs to its MainWindow and to that dock's pane group.
            w = obj
            while w is not None and not isinstance(w, MainWindow):
                w = w.parentWidget()
            if w is not None:
                w._on_pane_touched(obj)
        return False


class _LinkBandLabel(QLabel):
    """The name beside a Link's timeline band: click to show that Link, double-click to
    rename it. The Link on screen is drawn bold. Its width is set from the longest Link
    name (MainWindow._sync_timeline_rows), so short names give the bands the room."""
    clicked = Signal()
    doubleClicked = Signal()
    menuRequested = Signal(object)      # QPoint, global: the Link's own actions
    MAX_WIDTH = 160                     # a longer name is elided

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(96)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setContentsMargins(0, 0, 6, 0)
        self.setCursor(Qt.PointingHandCursor)
        self.retheme()

    def retheme(self) -> None:
        """Draw the name in the theme's text colour."""
        self.setStyleSheet(f"color: {VizTheme.TEXT};")

    def set_link(self, name: str, active: bool) -> None:
        import html
        shown = html.escape(self.fontMetrics().elidedText(name, Qt.ElideRight, self.width() - 10))
        self.setText(f"<b>{shown}</b>" if active else shown)
        self.setToolTip(f"{name}: click to show this Link, double-click to rename")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self.menuRequested.emit(event.globalPos())


class _PerLink:
    """A MainWindow attribute that belongs to the active Link. Reads and writes go to that
    Link's LinkPanelState, or to the window's idle state while no Link is loaded, so every
    existing `self._x` use keeps working unchanged as the Links behind it change."""

    def __set_name__(self, owner, name: str) -> None:
        self._field = name.lstrip("_")

    def __get__(self, win, owner=None):
        if win is None:
            return self
        return getattr(win._link_state(), self._field)

    def __set__(self, win, value) -> None:
        setattr(win._link_state(), self._field, value)


class MainWindow(QMainWindow):
    # True once the Qt event loop is running (set by app.main via a singleShot). Demo loads
    # go through the async worker+progress path only when it's live; before that (the __init__
    # preload) and in headless tests (which never start exec()) they run synchronously, so a
    # freshly built MainWindow has its session ready without pumping events.
    _event_loop_live = False
    # Emitted once the window has closed (its close accepted): app.main quits on it, so a
    # window still open elsewhere cannot keep the app running.
    closed = Signal()

    # Per-Link state (see ui/link_state.py); `_session` is the property below.
    _audio_store = _PerLink()
    _cmd_model = _PerLink()
    _starts = _PerLink()
    _meas_dirty = _PerLink()
    _sample_range = _PerLink()
    _sample_total = _PerLink()
    _sample_filters = _PerLink()
    _symbol_range = _PerLink()

    def __init__(self, register_map: Optional[RegisterMap] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"SWI3S Studio {__version__}")
        # The maximized pane and the arrangement to put back, or None (toggle_maximized_pane).
        self._max_pane: Optional[dict] = None
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
        # The analysis's Links; the per-Link attributes read the active one's state, and
        # this idle state while there is none.
        self._links = LinkSet()
        self._idle_link = LinkPanelState()
        self._cursor_instant: Optional[tuple] = None     # (Link, sample, true ps) — see _activate_link
        self._load_pending: Optional[list] = []   # queued (factory, after, kind, key) requests
        # Counts the opens asked for (queued or started): a request that queues more Links
        # once its first has loaded checks it, so a newer open is not added to (_after_open).
        self._open_gen = 0
        # The file name of the workspace apply_workspace is applying, for its import
        # warnings; None otherwise.
        self._ws_source: Optional[str] = None
        # Commands scope: one Link's commands (False) or every Link's in time order (True).
        self._cmd_all = False
        self._all_model: Optional[AllLinksCommandModel] = None
        self._all_col_widths: Optional[dict] = None   # the All Links table's, kept across rebuilds
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
        # Filtering lives in the Filter menu (built in _build_menu); the command-table
        # title doubles as the live "N of M (active filters)" indicator.
        self._expr_dialog = None
        self._expr_edit = None
        self._expr_timer: Optional[QTimer] = None   # the filter debounce; built with the dialog
        self._cmd_proxy.rowsInserted.connect(self._update_cmd_title)
        self._cmd_proxy.rowsRemoved.connect(self._update_cmd_title)
        self._cmd_proxy.modelReset.connect(self._update_cmd_title)
        # Sorted list of filter-accepted SOURCE rows, so _select_command_for_sample can
        # bisect to the nearest visible command instead of walking mapFromSource per row
        # across a big filtered gap. Rebuilt lazily and dropped whenever the filter / model
        # changes the visible set.
        self._accepted_src_cache = None
        for _sig in (self._cmd_proxy.rowsInserted, self._cmd_proxy.rowsRemoved,
                     self._cmd_proxy.modelReset, self._cmd_proxy.layoutChanged):
            _sig.connect(lambda *a: setattr(self, "_accepted_src_cache", None))

        analysis_page = QWidget()
        self._analysis_page = analysis_page    # the Commands pane group (see _group_of)
        col = QVBoxLayout(analysis_page)
        col.setContentsMargins(0, 0, 0, 0)
        self._cmd_title = _PaneTitle("Commands")   # the central pane has no dock title
        self._cmd_title.setAlignment(Qt.AlignCenter)   # match the dock title bars
        self._cmd_title.doubleClicked.connect(lambda: self.toggle_maximized_pane("commands"))
        # The Link switcher sits beside the title and is hidden until there is a second
        # Link. The header row
        # carries the title bar's background, so the maximize button at its right sits on it.
        self._cmd_header = QWidget()
        self._cmd_header.setObjectName("cmdHeader")
        header = QHBoxLayout(self._cmd_header)
        header.setContentsMargins(0, 0, 6, 0)
        header.setSpacing(0)
        header.addSpacing(_DockTitleBar._BTN + 6)      # balances the button, so the title centres
        self._link_combo = QComboBox()
        self._link_combo.setToolTip("Which Link the per-Link panes show")
        self._link_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._link_combo.hide()
        self._link_combo.activated.connect(self._on_link_combo)
        header.addWidget(self._link_combo)
        header.addWidget(self._cmd_title, 1)
        self._cmd_max_btn = _maximize_button(self._cmd_header, _DockTitleBar._BTN)
        self._cmd_max_btn.clicked.connect(lambda: self.toggle_maximized_pane("commands"))
        header.addWidget(self._cmd_max_btn)
        col.addWidget(self._cmd_header)
        col.addWidget(self._cmd_view)

        # Central area is a stack of per-mode pages (Visualization | Timing |
        # Analysis). Analysis is the command-table page above; Visualization is the
        # authoring editor; Timing is the TimingView.
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
        # Visualization page: the parameter panels on top, the placed bus grid as the
        # large canvas below. The grid
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
        # The Timing Inequalities' ⤢, the same button as a pane's: the page's top bar hidden.
        self._timing_max_btn = _maximize_button(self._timing_page, _DockTitleBar._BTN)
        self._timing_max_btn.clicked.connect(lambda: self.toggle_maximized_pane("timing"))
        self._timing_page.add_title_button(self._timing_max_btn)
        self._timing_page.maximizeRequested.connect(
            lambda: self.toggle_maximized_pane("timing"))
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

        # Timeline (top dock): one ribbon per Link, stacked on one shared time window (see
        # _sync_timeline_rows). With one Link it is a single ribbon.
        self._ribbons: list = []
        self._ribbon_labels: list = []
        self._ribbon_rows: list = []
        self._timeline_rows_shown = 1
        self._syncing_views = False
        self._timeline_stack = QWidget()
        self._timeline_col = QVBoxLayout(self._timeline_stack)
        self._timeline_col.setContentsMargins(0, 0, 0, 0)
        self._timeline_col.setSpacing(0)
        self._add_ribbon()
        self._timeline_dock = self._add_dock("Timeline", self._timeline_stack,
                                             Qt.TopDockWidgetArea, maximizable=False)

        # Register map (right dock).
        self._reg_view = RegisterView(self._rmap)
        self._reg_view.registerEdited.connect(self._on_register_edited)
        self._reg_view.overridesCleared.connect(self._on_register_overrides_cleared)
        self._reg_view.comparisonCleared.connect(self.clear_comparison)   # "Clear Compare" button
        self._reg_view.historyRequested.connect(self.show_register_history)
        self._reg_dock = self._add_dock("Registers", self._reg_view, Qt.RightDockWidgetArea)

        # 2D bus grid + audio + CDS symbols (bottom docks, tabbed).
        self._grid_view = GridView()
        self._grid_view.dpKeyClicked.connect(self._on_dp_key_clicked)
        self._grid_dock = self._add_dock("Bus Grid", self._build_grid_pane(),
                                         Qt.BottomDockWidgetArea)
        # Capture and Audio are LinkLanes: their own view, plus one lane per further Link
        # while the bottom group shows All Links (set_bottom_scope_all).
        self._bottom_all = False
        self._bottom_single_height = 0         # the signal panes' height before All Links
        self._audio_view = self._wire_audio(AudioView())
        self._audio_lanes = LinkLanes(self._audio_view, lambda: self._wire_audio(AudioView()))
        self._audio_dock = self._add_dock("Audio", self._audio_lanes, Qt.BottomDockWidgetArea)
        # CDS and Samples stack one table per Link in All Links, like Capture and Audio.
        self._origin_lane = -1                 # the table lane a selection came from
        self._symbol_view = self._wire_symbols(SymbolView())
        self._symbol_lanes = LinkLanes(self._symbol_view, lambda: self._wire_symbols(SymbolView()))
        self._symbol_dock = self._add_dock("CDS", self._symbol_lanes, Qt.BottomDockWidgetArea)
        self._sample_view = self._wire_samples(DecodedSampleView())
        self._sample_lanes = LinkLanes(self._sample_view,
                                       lambda: self._wire_samples(DecodedSampleView()))
        self._sample_dock = self._add_dock("Samples", self._sample_lanes, Qt.BottomDockWidgetArea)
        # Populate the (windowed) sample table only when its tab is actually shown.
        self._sample_dock.visibilityChanged.connect(self._on_sample_dock_visible)
        # The register tree + bus grid are rebuilt on every cursor tick, so _on_cursor
        # skips them while their tab is hidden; refresh once when the tab is re-shown.
        self._reg_dock.visibilityChanged.connect(self._on_reg_dock_visible)
        self._grid_dock.visibilityChanged.connect(self._on_grid_dock_visible)
        self._raw_view = self._wire_capture(RawCaptureView())
        self._raw_lanes = LinkLanes(self._raw_view, lambda: self._wire_capture(RawCaptureView()))
        self._raw_dock = self._add_dock("Capture", self._raw_lanes, Qt.BottomDockWidgetArea)
        self._eye_view = self._wire_timing(EyeView())
        self._eye_lanes = LinkLanes(self._eye_view, lambda: self._wire_timing(EyeView()))
        self._eye_dock = self._add_dock("Timing", self._eye_lanes, Qt.BottomDockWidgetArea)
        self._meas_view = MeasurementsView()
        self._meas_dock = self._add_dock("Statistics", self._meas_view, Qt.RightDockWidgetArea)
        # Statistics is tabbed behind Registers (hidden at load), but capture_measurements
        # iterates the whole audio store — so compute it lazily on first show / re-decode,
        # not eagerly for a hidden tab. Same deferral as the register/grid docks.
        self._meas_dock.visibilityChanged.connect(self._on_meas_dock_visible)
        self._pair_view = PairMeasureView()
        self._pair_view.sampleSelected.connect(self._on_pair_seek)   # click a pair -> seek
        self._pair_dock = self._add_dock("Cursors", self._pair_view, Qt.RightDockWidgetArea,
                                         object_name="Bookmarks")
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

        # Which Link each pane group shows (see _PANE_GROUPS), and a Link picker in the
        # title bar of every dock of the right and bottom groups, so the Link a pane shows
        # is visible whichever tab is raised and wherever the dock is moved.
        self._pane_link = dict.fromkeys(_PANE_GROUPS, 0)
        self._group_docks = {
            "right": [self._reg_dock, self._meas_dock],
            "grid": [self._grid_dock],
            "bottom": [self._raw_dock, self._symbol_dock, self._audio_dock,
                       self._sample_dock, self._eye_dock],
        }
        # Built with the second Link (see _ensure_pane_pickers): a single-Link window never
        # shows them.
        self._pane_pickers: Dict[str, list] = {"right": [], "grid": [], "bottom": []}

        # A slim status bar: the one place the window says what it just did (opened a
        # file as a Link, set an offset, found no commit, re-decoded).
        self._status = QLabel("")
        self._status.setObjectName("statusText")
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)   # copyable
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.addWidget(self._status, 1)
        self._build_menu()

        # Three-mode shell: the Mode menu swaps the central page and the visible dock
        # set per mode. Analysis owns all the docks. Build last so every dock exists.
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
            before_change=self.restore_panes,
        )
        self._timeline_keepers = {"commands" if name == "commands" else getattr(self, name)
                                  for name in _MAXIMIZE_KEEPS_TIMELINE}
        self._mode_mgr.switch_to(VISUALIZATION)   # Bus Visualizer is the default mode on open

        # Follow the OS light/dark scheme live while the preference is 'system'.
        app = QApplication.instance()
        if app is not None:
            try:
                app.styleHints().colorSchemeChanged.connect(self._on_os_color_scheme_changed)
            except (AttributeError, RuntimeError):       # older Qt without the signal
                pass

        # No demo is decoded on entering the Bus Analyzer: it opens empty, with a pointer to
        # Open Capture and the demos (_analysis_empty).

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
        # Every menu shows only what applies to the mode: the Analyzer's own menus, the
        # View menu's pane toggles and the timeline legend are hidden (and disabled, so
        # their shortcuts do not fire) elsewhere, and File holds the mode's file actions.
        for menu in getattr(self, "_analysis_menus", []):
            menu.setEnabled(analysing)
            menu.menuAction().setVisible(analysing)
        if analysing and hasattr(self, "_needs_capture"):
            # Enabling a menu enables every submenu in it (Qt passes the change down to
            # the child menus, and a submenu's item follows its menu), so what waits for a
            # capture is greyed again here.
            self._update_capture_actions()
        # Preferences holds only the waveform colours and weights, so it is the Analyzer's
        # too (and ⌘, with it), as is the View item that names it.
        for act in getattr(self, "_view_pane_actions", []) + [
                a for a in (getattr(self, "_view_pane_sep", None),
                            getattr(self, "_legend_action", None),
                            getattr(self, "_colors_action", None),
                            getattr(self, "_prefs_action", None)) if a is not None]:
            act.setEnabled(analysing)
            act.setVisible(analysing)
        # View ▸ Maximize Pane (and its key) also toggles the Timing Inequalities alone.
        if mode == TIMING and hasattr(self, "_maximize_action"):
            self._maximize_action.setEnabled(True)
            self._maximize_action.setVisible(True)
        if hasattr(self, "_maximize_action"):
            self._sync_maximize_ui()
        if hasattr(self, "_file_mode_menus"):
            self._fill_file_menu(mode)
        self._sync_view_links()
        # The bus grid is shared: show the authored grid in Visualization, the
        # decoded grid in Analysis.
        if mode == VISUALIZATION:
            self._refresh_authored_grid()
            self._apply_viz_split_sizes()
        elif analysing:
            self._refresh_grid()
        # First time into Analysis, lay out the default arrangement (Bus Grid active,
        # bottom row ~25% tall, Register Map ~33% wide). Deferred so the window has a
        # real geometry; later visits restore the user's own arrangement.
        if analysing and not getattr(self, "_analysis_laid_out", False):
            timers.after(0, self, self._apply_analysis_layout)
        self._status.setText(f"{mode} mode")
        self._update_title()
        # Switching to Visualization makes the (tall) authoring panel the central widget's
        # size hint; QMainWindow grows the window to meet its minimum and never shrinks it
        # back on the next switch. Re-fit to the work area after the switch settles so the
        # window never stays larger than the screen.
        timers.after(0, self, self._fit_to_available_screen)

    def _update_title(self) -> None:
        """Window title: the loaded capture's name is shown ONLY in Analyzer mode (the
        capture is what you're analysing there); Visualizer/Timing just show the app
        name. A long capture label (e.g. two .bin channel files) is middle-elided."""
        base = f"SWI3S Studio {__version__}"
        sess = self._session
        if getattr(self, "_mode_mgr", None) is not None \
                and self._mode_mgr.current() == ANALYSIS and sess is not None:
            label = self._elide(sess.source_label())
            if len(self._links) > 1:             # name the Link the panes are showing
                label = f"{self._links[self._links.active_index].name}: {label}"
            self.setWindowTitle(f"{base} — {label}")
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
        # Windows) aren't known until the frame exists after show.
        if not getattr(self, "_fit_screen_once", False):
            self._fit_screen_once = True
            timers.after(0, self, self._fit_to_available_screen)
        # The splitter has no height until the window is shown: apply the sizing once
        # the geometry exists, so first load matches the return-to-mode appearance.
        if not getattr(self, "_sized_viz_once", False):
            self._sized_viz_once = True
            timers.after(0, self, self._apply_viz_split_sizes)
        if not getattr(self, "_stripped_fs_once", False):
            self._stripped_fs_once = True
            # AppKit inserts "Enter Full Screen" into the View menu lazily (after show,
            # and again each time the menu opens); strip it now, on a delay, AND right
            # before the View menu opens so full-screen is left to the OS window
            # title-bar button. Best-effort — no-op off macOS.
            self._strip_macos_fullscreen_menu_item()
            timers.after(0, self, self._strip_macos_fullscreen_menu_item)
            timers.after(500, self, self._strip_macos_fullscreen_menu_item)
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
            # Stop AppKit from re-inserting "Enter Full Screen" every time a menu opens;
            # then drop any it has already added. The green title-bar button still toggles
            # full screen.
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
        self._viz_grid.set_bus_model(cells, clashes, ncols, nrows,
                                     slot_names=_authored_slot_names(cfg),
                                     cds_symbol=cds_symbol(cfg.cds_drive_type,
                                                            cfg.cds_end_drive_early))
        self._authoring.set_issues(issues)

    def _on_issue_activated(self, cells: list) -> None:
        self._refresh_authored_grid()

    def load_init_csv(self) -> None:
        """Load a user-supplied init.csv (from the app home / repo root) into the
        authoring panel, showing "Loaded: init.csv" like Load Settings. Falls back to
        the built-in demo config when no init.csv is present or it can't be read."""
        path = _init_csv_path()
        try:
            with self.reporting_import():
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
        self._remember_capture_dir(path, "visualizer")
        self.open_visualizer_csv_path(path)

    def open_visualizer_csv_path(self, path: str) -> None:
        """Open a Visualizer config CSV by PATH — the dialog-free half of
        open_visualizer_csv, so the `-c` command-line option and the menu item take the
        same route (mode switch, then load). A caller passing a path never raises a
        file dialog."""
        if self._mode_mgr.current() != VISUALIZATION:
            self._mode_mgr.switch_to(VISUALIZATION)
        self._load_authoring_csv(path)

    def _load_authoring_csv(self, path: str) -> None:
        """Read a Visualizer CSV into the authoring panel (upgrading legacy/v2.0 CSVs),
        showing its import warnings."""
        try:
            with self.reporting_import():
                cfg = self._bus_config_from_csv(path)
            self._authoring.set_config(cfg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load config", f"Couldn't read that CSV: {exc}")
            return
        self._status.setText(f"Loaded config: {path}")
        self._authoring.set_file_status(f"Loaded: {os.path.basename(path)}")

    def _bus_config_from_csv(self, path: str) -> "BusConfig":
        """Read a BusConfig from a Visualizer CSV, upgrading any legacy/v2.0 format and
        removing the temp file normalized_path may create, so repeated loads of an old
        config don't leak temps into the OS temp dir. Import warnings name `path`."""
        from ..ingest import visualizer_csv
        cfg_path = visualizer_csv.normalized_path(path)   # upgrade any legacy/v2.0 CSV
        try:
            return BusConfig.from_csv(cfg_path, source=path)
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
        name = import_notes.source_name(path)
        with self.reporting_import(name):
            self._timing_page.set_from_dict(data, source=name)
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
            self._for_group("right", lambda: self._run_compare(path))()

    def _apply_chrome_styles(self) -> None:
        """(Re)apply the window-chrome stylesheets that bake VizTheme colours: the
        Analysis command-table page, its faux title bar, the Visualization splitter
        gutter + floating restore button, the central-stack background and the main
        window gutters. Run at build and again on every theme switch."""
        t = VizTheme
        self._analysis_page.setStyleSheet(analyzer_stylesheet())
        if hasattr(self, "_grid_pane"):
            self._grid_pane.setStyleSheet(analyzer_stylesheet())   # Bus Grid toolbar buttons
        self._cmd_header.setStyleSheet(f"QWidget#cmdHeader {{ background:{t.FRAME_BG}; }}")
        self._cmd_title.setStyleSheet(f"padding:3px 6px; background:{t.FRAME_BG};")
        self._cmd_max_btn.setStyleSheet(_title_button_style())
        self._timing_max_btn.setStyleSheet(_title_button_style())
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
        self._restyle()

    def show_preferences(self) -> None:
        """Preferences ▸ Waveforms: on OK, store the settings and redraw every pane."""
        from .preferences_dialog import PreferencesDialog
        dlg = PreferencesDialog(line_style.preferences(), self, mode=VizTheme.MODE)
        if dlg.exec():
            line_style.set_preferences(dlg.preferences())
            self._restyle()

    def _restyle(self) -> None:
        """Re-theme the chrome and every colour-caching view from the active palette and
        the waveform preferences, keeping the cursor, filters and bookmarks."""
        self._apply_chrome_styles()
        for pickers in self._pane_pickers.values():
            for picker in pickers:
                picker.setStyleSheet(combo_qss())
        # Views that cache pens / brushes / backgrounds re-read the palette.
        for view in (self._authoring, self._viz_grid, self._grid_view,
                     *self._audio_lanes.views, *self._raw_lanes.views,
                     *self._symbol_lanes.views, *self._sample_lanes.views,
                     *self._eye_lanes.views,
                     self._audio_lanes, self._raw_lanes, self._symbol_lanes,
                     self._sample_lanes, self._eye_lanes,
                     *self._ribbons, *self._ribbon_labels, self._reg_view, self._cmd_view,
                     self._meas_view, self._timing_page,
                     self._pair_view, *getattr(self, "_empty_hints", ()),
                     *self._history_dialogs(), *self.import_warning_dialogs()):
            rt = getattr(view, "retheme", None)
            if callable(rt):
                rt()
        # The grids only recolour their canvas on retheme(); re-render their data so
        # every cell picks up the new palette (authored grid always; decoded grid
        # when a capture is loaded).
        self._refresh_authored_grid()
        self._refresh_grid()

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
        # WIDTH FROM THE FONT, WITH 64 PX AS A FLOOR: measure the widest string the validator
        # allows, plus frame and margins. A MINIMUM rather than a fixed width, so a wider
        # platform font is not clipped.
        _fm = self._grid_rows_edit.fontMetrics()
        _margins = self._grid_rows_edit.textMargins()
        _frame = 2 * self._grid_rows_edit.style().pixelMetric(
            QStyle.PM_DefaultFrameWidth, None, self._grid_rows_edit)
        self._grid_rows_edit.setMinimumWidth(max(
            64,                                            # the tuned macOS value, as a floor
            _fm.horizontalAdvance("10240")                 # the validator's widest value
            + _margins.left() + _margins.right() + _frame + 8))   # frame, margins, caret
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

    def _add_dock(self, title: str, widget: QWidget, area,
                  maximizable: bool = True, object_name: str = "") -> QDockWidget:
        """A dock titled `title`. Its object name, which a saved dock layout (saveState) keys
        it by, is `object_name`, or the title without spaces."""
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name or title.replace(" ", ""))
        dock.setWidget(widget)
        # Own the title bar so the close X matches the theme (see _DockTitleBar).
        bar = _DockTitleBar(dock, title, (lambda d=dock: self.toggle_maximized_pane(d))
                            if maximizable else None, lambda d=dock: self._hide_dock(d))
        dock.setTitleBarWidget(bar)
        bar.tabPicked.connect(self._raise_pane_tab)
        for changed in (dock.visibilityChanged, dock.dockLocationChanged,
                        dock.topLevelChanged):
            changed.connect(self._schedule_pane_tabs)
        if not hasattr(self, "_dock_titlebars"):
            self._dock_titlebars = []
        self._dock_titlebars.append(bar)
        self.addDockWidget(area, dock)
        if not hasattr(self, "_dock_area"):
            self._dock_area = {}
        self._dock_area[dock] = area          # home area, for robust re-show
        return dock

    def _schedule_pane_tabs(self, *_args) -> None:
        """Re-run _sync_pane_tabs once the dock layout has settled (once per pass)."""
        if not getattr(self, "_pane_tabs_pending", False):
            self._pane_tabs_pending = True
            QTimer.singleShot(0, self._sync_pane_tabs)

    def _sync_pane_tabs(self) -> None:
        """Give every docked pane that is tabbed with others its group's tabs in its title
        bar. The group is Qt's (tabifiedDockWidgets), in the order of the dock area's own
        tab bar, a direct child of the window that keeps the order and the pane in front;
        chrome_stylesheet collapses that bar to no height, so the title bar's tabs are the
        only ones drawn. Qt keeps retired tab bars, so a bar names the order only when its
        tabs are exactly the group."""
        self._pane_tabs_pending = False
        if not isValid(self):
            return
        watched = self.__dict__.setdefault("_watched_dock_tabbars", set())
        orders = []
        for tb in self.findChildren(QTabBar, options=Qt.FindDirectChildrenOnly):
            if id(tb) not in watched:
                watched.add(id(tb))
                tb.currentChanged.connect(self._schedule_pane_tabs)
                tb.tabMoved.connect(self._schedule_pane_tabs)
            orders.append([tb.tabText(i) for i in range(tb.count())])

        def docked(d: QDockWidget) -> bool:
            return not d.isHidden() and not d.isFloating()

        panes = list(self._dock_area)
        for bar in getattr(self, "_dock_titlebars", []):
            dock = bar._dock
            group = [dock, *(d for d in self.tabifiedDockWidgets(dock) if docked(d))]
            if not docked(dock) or len(group) < 2:
                bar.set_tabs([])
                continue
            names = {d.windowTitle() for d in group}
            order = next((o for o in orders if len(o) == len(names) and set(o) == names),
                         [d.windowTitle() for d in panes if d in group])
            bar.set_tabs(order)

    def _raise_pane_tab(self, title: str) -> None:
        """A title-bar tab clicked: bring the pane of that title to the front of its group."""
        dock = next((d for d in self._dock_area if d.windowTitle() == title), None)
        if dock is not None:
            dock.raise_()
            self._schedule_pane_tabs()

    def _style_dock_tabbars(self) -> None:
        """Flatten the tabified-dock QTabBars to the panel background. setDrawBase(False)
        drops the native (macOS) 'tab base' strip, which a QSS `QTabBar { background }`
        doesn't override, and an autoFill with the panel colour makes the bar sit flush
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
        self._file_menu = f
        # File holds the CURRENT mode's file actions only (_fill_file_menu, on every mode
        # change). Each mode's set is built once in a menu of its own, kept off the bar,
        # and its actions are put into File; Quit is the window's, so File can be
        # refilled without deleting it.
        analyzer = QMenu("&Analyzer", self)
        # Open Capture's dialog asks how much to decode, so a window is on offer at any
        # size, not only when the memory guard forces one.
        open_capture = analyzer.addAction("&Open Capture…", lambda: self.open_capture())
        demo_menu = analyzer.addMenu("Open &Demo Capture")
        # Each entry opens its demo with the bundled workspace (workspace.DEMO_WORKSPACES).
        for label, key in (("PHY1", "phy1"), ("PHY2", "phy2"),
                           ("PHY2 (Flow Control)", "phy2_flow_control"), ("PHY3 (DLV)", "phy3")):
            demo_menu.addAction(label, lambda k=key: self.open_demo(k))
        analyzer.addSeparator()
        analyzer.addAction("Open &Workspace…", self.open_workspace)
        save_workspace = analyzer.addAction("&Save Workspace…", self.save_workspace)
        analyzer.addSeparator()
        self._export_capture_action = analyzer.addAction("Export &Capture…",
                                                         self._for_group("bottom", self.export_capture))
        locate = analyzer.addAction("&Locate Sub-Capture…",
                                    self._for_group("bottom", self.locate_subcapture))
        analyzer.addSeparator()
        export_csv = analyzer.addAction("&Export Visualizer CSV…",
                                        self._for_group("grid", self.export_grid_csv))
        grid_image = analyzer.addAction("Export Bus Grid &Image…",
                                        self._for_group("grid", lambda: self.export_grid(self._grid_view)))
        to_viz = analyzer.addAction("View Bus Grid in Visuali&zer",
                                    self._for_group("grid", self.open_grid_in_visualizer))
        self._file_analyzer_menu = analyzer
        # Everything here that acts on a capture waits for one (see _update_capture_actions).
        self._needs_capture = [save_workspace, self._export_capture_action, locate,
                               export_csv, grid_image, to_viz]

        visualizer = QMenu("&Visualizer", self)
        open_viz = visualizer.addAction("&Open Visualizer CSV…", self.open_visualizer_csv)
        save_viz = visualizer.addAction("&Save Visualizer CSV…", self.save_authoring_csv)
        visualizer.addSeparator()
        visualizer.addAction("Export Grid &Image…", lambda: self.export_grid(self._viz_grid))
        visualizer.addAction("Export Bus &Model (JSON)…", self.export_frame_json)

        timing = QMenu("&Timing", self)
        open_timing = timing.addAction("&Open Timing Settings…", self.open_timing_settings)
        save_timing = timing.addAction("&Save Timing Settings…", self.save_timing_settings)

        # ⌘O / ⌘S open and save the CURRENT mode's file; _fill_file_menu moves the keys
        # to that mode's pair, so one key never names two actions.
        self._file_keys = {ANALYSIS: (open_capture, save_workspace),
                           VISUALIZATION: (open_viz, save_viz),
                           TIMING: (open_timing, save_timing)}

        self._file_mode_menus = {ANALYSIS: analyzer, VISUALIZATION: visualizer, TIMING: timing}
        self._quit_sep = QAction(self)
        self._quit_sep.setSeparator(True)
        self._quit_action = QAction("&Quit", self)
        self._quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        # macOS gives the Quit role ⌘Q; elsewhere the standard Quit key is often none.
        self._quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self._quit_action.triggered.connect(self.close)
        self._fill_file_menu(VISUALIZATION)          # the mode the window opens in

        # Commands: filter the command table (submenu) + export it as CSV, and jump
        # the cursor between the sync-point commits (SSCR/DSCR — where the config takes
        # effect), with ⌘> / ⌘< (Ctrl on non-mac).
        cmds = self.menuBar().addMenu("&Commands")
        self._filter_menu = self._build_filter_menu(cmds)
        cmds.addSeparator()
        # Keys go in setShortcut, never in the item's text: Qt draws a shortcut in each
        # platform's own notation (⌘ on macOS), but "\tCtrl+>" in the text is drawn as
        # written.
        nxt = cmds.addAction("Next SSCR/DSCR Commit", self._for_group("commands", self.next_sscr))
        # '>' is Shift+'.', so the OS delivers Ctrl+Shift+. — bind BOTH spellings or the
        # shortcut silently never matches (it read as ⌘> in the menu either way).
        nxt.setShortcuts([QKeySequence("Ctrl+>"), QKeySequence("Ctrl+Shift+.")])
        prv = cmds.addAction("Previous SSCR/DSCR Commit",
                             self._for_group("commands", self.prev_sscr))
        prv.setShortcuts([QKeySequence("Ctrl+<"), QKeySequence("Ctrl+Shift+,")])
        cmds.addSeparator()
        cmds.addAction("&Export as CSV…", self._for_group("commands", self.export_commands))

        # Decode: every input that changes what the capture decodes to, so each re-decodes.
        dec = self.menuBar().addMenu("&Decode")
        self._decode_menu = dec
        dec.addAction("&Import Visualizer CSV…", self._for_group("grid", self.open_visualizer_config))
        # Enabled only while the cursor's region has a CSV of its own (set as the menu opens).
        self._remove_region_csv_action = dec.addAction(
            "&Remove Region's Visualizer CSV", self._for_group("grid", self.remove_region_csv))
        dec.aboutToShow.connect(self._update_remove_region_csv)
        dec.addAction("&Force Column Count…", self._for_group("grid", self.force_column_count))
        dec.addSeparator()
        dec.addAction("Dataport &Scramblers…", self._for_group("bottom", self.scrambler_overrides_dialog))
        # Manual Stream Sync Point: for a post-commit capture the SSPA/SSCR was never on
        # the wire, so interval>1 ports decode at an arbitrary phase — pick the row and
        # step it (the phase repeats every interval) until the audio is clean.
        ssp = dec.addMenu("Manual &SSP Move")
        ssp.addAction("&Set SSP at Playhead Row", self._for_group("bottom", self.set_ssp_at_cursor))
        # Nudge the SSP by ±1 row with ⌘K / ⌘L (K left of L → −1 / +1). Both display and
        # aren't macOS-reserved — unlike ⌘, (Preferences), which macOS strips the glyph
        # from on any non-Preferences item.
        ssp.addAction("Move SSP &−1 Row",
                      self._for_group("bottom", lambda: self.step_ssp_row(-1))).setShortcut("Ctrl+K")
        ssp.addAction("Move SSP &+1 Row",
                      self._for_group("bottom", lambda: self.step_ssp_row(+1))).setShortcut("Ctrl+L")
        ssp.addAction("&Clear Manual SSP", self._for_group("bottom", lambda: self.set_ssp_row_async(-1)))

        rm = self.menuBar().addMenu("&Devices")
        self._devices_menu = rm
        rm.addAction("Device &Names…", self._for_group("right", self.manage_device_names))
        rm.addAction("&Register Maps…", self._for_group("right", self.manage_register_maps))
        rm.addAction("&Hub Depths…", self._for_group("right", self.manage_hub_depths))

        a = self.menuBar().addMenu("&Audio")
        self._play_device_menu = a.addMenu("Playback &Output Device")
        self._play_depth_menu = a.addMenu("Playback &Bit Depth")
        self._play_rate_menu = a.addMenu("Playback &Decimation")
        # Built as it opens, for the Audio view the menu acts on NOW: in All Links that is
        # the active Link's lane, which a bind (lane by lane) cannot know.
        self._play_rate_menu.aboutToShow.connect(self._build_decimation_menu)
        a.addSeparator()
        self._process_menu = a.addMenu("&Filter && Gain")
        self._process_menu.aboutToShow.connect(self._build_processing_menu)
        a.addSeparator()
        self._export_action = a.addAction("&Export…", self._for_group("bottom", self.export_audio))
        self._audio_menu = a
        self._build_audio_playback_menu()

        b = self.menuBar().addMenu("C&ursors")
        self._bookmarks_menu = b
        b.addAction("&Toggle Cursor at Playhead", self.toggle_bookmark).setShortcut("Ctrl+B")
        b.addAction("&Next Cursor", self.next_bookmark).setShortcut("Ctrl+]")
        b.addAction("&Previous Cursor", self.prev_bookmark).setShortcut("Ctrl+[")
        b.addAction("&Clear Cursors", self.clear_bookmarks)

        # No Links menu: a Link is named, placed and
        # added in Open Capture's dialog; its own Rename / Set Time Offset / Remove are on its
        # timeline label's right-click (_link_label_menu); Align reads two bookmarks, so it
        # is a Bookmarks item; showing Links is View ▸ Links (built here, placed in View).
        b.addSeparator()
        self._align_links_action = b.addAction("&Align Links on Cursor Pair…",
                                               self.align_on_bookmark_pair)
        lk = QMenu("&Links", self)
        self._all_links_action = lk.addAction("&Commands: All Links")
        self._all_links_action.setCheckable(True)
        self._all_links_action.setShortcut(QKeySequence("Ctrl+Alt+0"))
        self._all_links_action.toggled.connect(self.set_commands_scope_all)
        self._bottom_all_action = lk.addAction("&Signal Panes: All Links")
        self._bottom_all_action.setCheckable(True)
        self._bottom_all_action.setShortcut(QKeySequence("Ctrl+Alt+Shift+0"))
        self._bottom_all_action.toggled.connect(self.set_bottom_scope_all)
        lk.addSeparator()
        self._links_menu = lk
        self._link_actions: list = []          # one checkable entry per Link, rebuilt
        self._link_group = QActionGroup(self)
        self._link_group.setExclusive(True)

        # Menus that only apply while analysing a capture — disabled in other modes.
        self._analysis_menus = [cmds, dec, rm, a, b]
        # The whole of Commands, Decode, Devices and Bookmarks acts on a capture, down to
        # the items in their submenus, whose shortcuts would otherwise still fire. The
        # submenus are named here, not found through QAction.menu(), which on PySide 6.11
        # hands back a dead wrapper; their items are read each time, as some are rebuilt
        # per capture (_update_capture_actions). Align also needs two Links.
        self._capture_menus = (cmds, dec, rm, b, self._filter_menu, self._kind_menu,
                               self._dev_menu, self._group_menu, ssp)

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
                            ("Cursors", self._pair_dock),
                            ("Bus Grid", self._grid_dock),
                            ("Capture", self._raw_dock),
                            ("CDS", self._symbol_dock),
                            ("Audio", self._audio_dock),
                            ("Samples", self._sample_dock),
                            ("Timing", self._eye_dock)):
            act = v.addAction(label)
            act.setCheckable(True)
            act.setChecked(not dock.isHidden())
            act.toggled.connect(lambda on, d=dock: self._show_dock(d) if on
                                else self._hide_dock(d))
            self._dock_actions[dock] = act
            self._view_pane_actions.append(act)
            # Keep the menu check in sync with the dock's own close button.
            dock.visibilityChanged.connect(lambda _vis, d=dock: self._sync_dock_action(d))
        # The selected pane alone in the window, and back (toggle_maximized_pane).
        v.addSeparator()
        self._maximize_action = v.addAction("Maximize Pane",
                                            lambda: self.toggle_maximized_pane())
        self._maximize_action.setShortcut(QKeySequence(_MAXIMIZE_KEY))
        self._maximize_action.setAutoRepeat(False)    # a held key is one toggle, not many
        self._view_pane_actions.append(self._maximize_action)

        # Appearance (dark / light / follow-system), persisted across launches and
        # applied live — see apply_theme. Not gated to Analysis mode (it themes the
        # whole app, including Visualization).
        self._view_links_action = v.addMenu(lk)   # View ▸ Links: Analyzer, two or more Links
        self._view_pane_sep = v.addSeparator()   # hidden with the pane toggles
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
        # Preferences is the waveform colours and weights. macOS moves it to the
        # application menu (Settings…, ⌘,), so View also says what it holds there; elsewhere
        # it stays in View, where two items opening one dialog would be one too many, so
        # the named one IS Preferences.
        import sys
        self._colors_action = v.addAction("Waveform &Colors and Line Weight…",
                                          self.show_preferences)
        if sys.platform == "darwin":
            self._prefs_action = v.addAction("&Preferences…", self.show_preferences)
        else:
            self._prefs_action = self._colors_action
        self._prefs_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        # The standard Preferences key is Ctrl+, only on macOS (elsewhere just the Settings
        # key), and Keyboard Shortcuts lists Ctrl+, everywhere: bind both.
        keys = [QKeySequence("Ctrl+,")]
        keys += [k for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Preferences)
                 if k not in keys]
        self._prefs_action.setShortcuts(keys)

        h = self.menuBar().addMenu("&Help")
        self._help_menu = h            # held, like the others (shiboken: see test_config_csv)
        h.addAction("SWI3S Studio &User Guide", self.show_user_guide)
        h.addAction("&Keyboard Shortcuts…", self.show_keyboard_shortcuts)
        self._legend_action = h.addAction("&Timeline Marks Legend…", self.show_timeline_legend)
        about = h.addAction("&About SWI3S Studio", self.show_about)
        about.setMenuRole(QAction.MenuRole.AboutRole)      # macOS: the application menu
        self._update_capture_actions()

    def _update_capture_actions(self) -> None:
        """Items that act on a capture are enabled once one is loaded, submenus included,
        and greyed until then; what opens a file, sets a preference or arranges the window
        is always available. The playback device and bit depth are for playing a capture,
        so they wait for one too. Audio ▸ Export and Audio's per-stream submenus also need audio,
        in the Link they act on: the signal panes' (_for_group), or in All Links the active
        one. Not to be run inside a lane's bind (_as_lane), where the lane's Link stands in
        as the active one."""
        loaded = self._session is not None
        for act in getattr(self, "_needs_capture", []):
            act.setEnabled(loaded)

        for menu in getattr(self, "_capture_menus", ()):
            for act in menu.actions():
                if not act.isSeparator():
                    act.setEnabled(loaded)
        self._align_links_action.setEnabled(loaded and len(self._links) > 1)
        for menu in (self._play_device_menu, self._play_depth_menu):
            menu.menuAction().setEnabled(loaded)
        # Audio's per-stream submenus wait for streams, as the items above for a capture.
        streams = loaded and bool(self._audio_lane().streams())
        for menu in (self._play_rate_menu, self._process_menu):
            menu.menuAction().setEnabled(streams)
        self._sync_empty_hints(loaded)
        i = self._links.active_index if self._bottom_all else self._pane_link["bottom"]
        store = self._links[i].view.audio_store if 0 <= i < len(self._links) else None
        self._export_action.setEnabled(loaded and store is not None and not store.is_empty())

    def _sync_empty_hints(self, loaded: bool) -> None:
        """The empty-Analyzer notes: shown while no capture is open, gone once one is.
        Built the first time, over the panes that would otherwise sit blank."""
        if not hasattr(self, "_empty_hints"):
            what = ("No capture open.\n\nFile ▸ Open Capture… (⌘O) opens one; "
                    "File ▸ Open Demo Capture has a synthetic bus to explore."
                    if sys.platform == "darwin" else
                    "No capture open.\n\nFile ▸ Open Capture… (Ctrl+O) opens one; "
                    "File ▸ Open Demo Capture has a synthetic bus to explore.")
            short = "No capture open"
            self._empty_hints = [_EmptyHint(self._cmd_view.viewport(), what)] + [
                _EmptyHint(target, short) for target in (
                    self._grid_view, self._raw_view, self._audio_view,
                    self._symbol_view.viewport(), self._sample_view._table.viewport(),
                    self._eye_view)]
        for hint in self._empty_hints:
            hint.setVisible(not loaded)
            if not loaded:
                hint.raise_()

    def show_about(self) -> None:
        """Help ▸ About SWI3S Studio (macOS: the application menu)."""
        from .. import __version__
        QMessageBox.about(self, "About SWI3S Studio",
                          f"<b>SWI3S Studio</b> {__version__}<br>"
                          "MIPI SoundWire I3S bus analyzer, visualizer and timing calculator.")

    def show_user_guide(self) -> None:
        """Help ▸ User Guide: the shipped docs/USER_GUIDE.md, rendered in a window that
        can stay open beside the app."""
        import sys
        here = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parents[2]))
        path = here / "docs" / "USER_GUIDE.md"
        if not path.is_file():
            QMessageBox.information(self, "User Guide", f"The User Guide was not found at {path}.")
            return
        if getattr(self, "_guide", None) is None:
            from PySide6.QtWidgets import QTextBrowser
            # A window of the main window's, closed with it (closeEvent), so it cannot keep
            # the app running after File ▸ Quit.
            self._guide = QTextBrowser(self)
            self._guide.setWindowFlag(Qt.Window, True)
            self._guide.setWindowTitle("SWI3S Studio User Guide")
            self._guide.setSearchPaths([str(path.parent), str(path.parent.parent)])
            self._guide.setOpenExternalLinks(True)
            self._guide.resize(820, 900)
        self._guide.setMarkdown(path.read_text(encoding="utf-8"))
        self._guide.show()
        self._guide.raise_()

    def _fill_file_menu(self, mode: str) -> None:
        """File holds the given mode's actions, then Quit, and ⌘O / ⌘S go to that mode's
        open / save. Each action is removed by hand, not with clear(): QMenu.clear()
        deletes the actions it removes even when another menu owns them."""
        for m, (op, sv) in self._file_keys.items():
            op.setShortcut(QKeySequence.StandardKey.Open if m == mode else QKeySequence())
            sv.setShortcut(QKeySequence.StandardKey.Save if m == mode else QKeySequence())
        f = self._file_menu
        for act in f.actions():
            f.removeAction(act)
        for act in self._file_mode_menus[mode].actions():
            f.addAction(act)
        f.addAction(self._quit_sep)
        f.addAction(self._quit_action)

    # ---- Filter submenu (under Commands) ----
    def _build_filter_menu(self, parent):
        fm = parent.addMenu("&Filter")
        fm.addAction("Filter &Expression…", self._filter_expression_dialog)
        fm.addSeparator()
        # The Commands submenu's quick picks, each a plain ⌘ + digit (4 on: ⌘1–3 are the
        # modes; ⌘A is Select All in the tables).
        self._kind_menu = self._make_filter_submenu(
            fm, "&Commands", [], self._cmd_proxy.set_kinds, exclude="Ping",
            quick=True)
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

    # The command kinds Commands ▸ Filter ▸ Commands ▸ Commit shows: the sync-point commits
    # (the ones Next / Previous SSCR/DSCR Commit steps through, _sscr_samples) and the
    # table's Commit Point rows, where each confirmed one takes effect.
    _COMMIT_KINDS = ("SSCR", "DSCR", "Commit Point")

    def _make_filter_submenu(self, parent, title, items, setter, exclude=None,
                             quick: bool = False):
        """A stay-open checkable submenu (+ an 'All' reset). `setter(set)` applies
        the selection (empty = all). `sub._fill(items)` repopulates it. `exclude` (a
        value) adds an 'All excl. <value>' quick action that checks every entry but
        that one (used for 'All excl. Ping' on the command-kind filter). `quick` (the
        command-kind filter) adds None and Commit, and gives All, None, All excl. and
        Commit the keys ⌘4–⌘7; `setter` then also takes `none=`."""
        sub = _CheckableMenu(title, self)
        parent.addMenu(sub)
        sub._actions = []
        sub._none = False                    # None picked: nothing shown, not "empty = all"

        def apply():
            sub._none = False                # ticking anything leaves None
            setter({a.data() for a in sub._actions if a.isChecked()})
            self._on_command_filter_changed()

        def check(pred):
            for a in sub._actions:
                a.blockSignals(True)
                a.setChecked(pred(a.data()))
                a.blockSignals(False)

        def clear():
            check(lambda _v: False)
            apply()

        def select_all_except(value):
            check(lambda v: v != value)
            apply()

        def select_none():
            check(lambda _v: False)
            sub._none = True
            setter(set(), none=True)
            self._on_command_filter_changed()

        def select_commits():
            offered = {a.data() for a in sub._actions}
            if not offered & set(self._COMMIT_KINDS):
                select_none()                # no commit in this capture: none to show
                self._status.setText("No commit in this capture")
                return
            check(lambda v: v in self._COMMIT_KINDS)
            apply()

        def entry(label, slot, key=None):
            act = sub.addAction(label)
            act.triggered.connect(slot)
            if quick and key:
                act.setShortcut(QKeySequence(key))
            return act

        def fill(its):
            sub.clear()
            sub._actions = []
            sub._none = False
            entry("All", clear, "Ctrl+4")
            if quick:
                entry("None", select_none, "Ctrl+5")
            if exclude is not None:
                entry(f"All excl. {exclude}", lambda: select_all_except(exclude), "Ctrl+6")
            if quick:
                entry("Commit", select_commits, "Ctrl+7")
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
        sub._select_none = select_none
        fill(items)
        return sub

    def _filter_expression_dialog(self) -> None:
        """Non-modal dialog with the free-text boolean expression — the one filter that can't
        be a menu item. Applies on a short pause rather than per character (see the debounce
        below), or at once on Enter."""
        if self._expr_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Filter Expression")
            lay = QVBoxLayout(dlg)
            lay.addWidget(QLabel("Filter commands (substring; and / or / parentheses):"))
            self._expr_edit = QLineEdit()
            self._expr_edit.setMinimumWidth(360)
            # DEBOUNCED, not applied per character. Every set_text invalidates the proxy
            # filter, which re-tests EVERY source row; the first sweep after a model reset
            # also has to build the row-text cache, so it is the expensive one. A short
            # pause is imperceptible when typing and collapses a burst into one.
            self._expr_timer = QTimer(self)
            self._expr_timer.setSingleShot(True)
            self._expr_timer.setInterval(200)
            self._expr_timer.timeout.connect(self._apply_expr_filter)
            self._expr_edit.textChanged.connect(self._expr_timer.start)
            # Enter applies immediately, without waiting for the debounce.
            self._expr_edit.returnPressed.connect(self._apply_expr_filter)
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

    def _apply_expr_filter(self) -> None:
        """Push the Filter Expression box into the proxy — one sweep, whenever it happens.

        Called by the debounce timer, and directly by Enter and Clear All Filters. Stops the
        timer first so an immediate apply cannot be followed by a redundant queued one.
        """
        if self._expr_edit is None:
            return
        timer = getattr(self, "_expr_timer", None)
        if timer is not None:
            timer.stop()
        self._cmd_proxy.set_text(self._expr_edit.text())
        self._on_command_filter_changed()

    def _clear_all_filters(self) -> None:
        self._kind_menu._clear()
        self._dev_menu._clear()
        self._group_menu._clear()
        self._errors_only_act.setChecked(False)        # toggled → set_errors_only(False)
        if self._expr_edit is not None:
            self._expr_edit.clear()                    # textChanged → starts the debounce
            self._apply_expr_filter()                  # …but a Clear applies at once
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
        if len(self._links) > 1 and not self._cmd_all \
                and self._pane_link["commands"] != self._links.active_index:
            with self._as_link(self._pane_link["commands"]):
                return self._update_cmd_title()
        if self._session is None or self._cmd_model is None:
            title.setText(base)
            return
        total = self._table_model().rowCount()
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
            self._cursor_commands(int(self.cursor.sample))
        self._update_nav_starts()

    def _update_nav_starts(self) -> None:
        """Push the command table's currently-VISIBLE commands to the timeline: their RSP
        cursor samples for Left/Right arrow nav, AND their raw start_samples for the drawn-
        tick filter (so hidden commands' ticks disappear). The two differ — nav parks on the
        RSP, but ticks are drawn at the raw start_sample — so they're sent separately.
        They go to the band of the Link Commands shows."""
        if len(self._links) > 1 and not self._cmd_all \
                and self._pane_link["commands"] != self._links.active_index:
            with self._as_link(self._pane_link["commands"]):
                return self._update_nav_starts()
        if self._session is None or self._cmd_model is None:
            self._timeline.set_nav_starts(None)
            self._timeline.set_draw_filter(None)
            return
        proxy = self._cmd_proxy
        active = self._links.active_index
        nav, draw = [], []
        for pr in range(proxy.rowCount()):
            sr = proxy.mapToSource(proxy.index(pr, 0)).row()
            link, cmd, pos = self._row_target(sr)
            if link != active:
                continue                          # the active Link's ribbon, its own rows
            if pos is not None:
                nav.append(int(pos))
            if cmd is not None:
                draw.append(int(cmd.get("start_sample", 0)))
        self._timeline.set_nav_starts(nav)
        self._timeline.set_draw_filter(draw)

    def _refresh_audio_devices(self) -> None:
        """Audio ▸ Refresh Devices: force a re-scan (the cached list otherwise keeps
        the snapshot from when the menu was first built), then rebuild the submenu."""
        for v in self._audio_lanes.views:
            v.refresh_devices()
        self._build_audio_playback_menu()

    def _retire_action_groups(self, *groups) -> None:
        """deleteLater() exclusive-action groups that are about to be replaced.

        A QActionGroup parented to `self` OUTLIVES the menu it populated: QMenu.clear()
        destroys the QActions (and a destroyed action removes itself from its group), but the
        group is a child of the WINDOW, so rebinding the attribute just drops the last Python
        reference to a C++ object Qt still owns.

        The decimation menu is rebuilt on every load and every re-decode, one group per
        (device, dataport) audio stream.
        """
        for group in groups:
            if group is not None:
                group.deleteLater()

    def _build_audio_playback_menu(self) -> None:
        """Populate the Audio ▸ playback submenus (output device, bit depth,
        decimation). Each is a checkable, mutually-exclusive group that sets the AudioView's
        playback state. Rebuilt on demand so a hot-plugged device shows up."""
        av = self._audio_view
        # Retire the groups this rebuild is about to orphan — see _retire_action_groups.
        self._retire_action_groups(getattr(self, "_play_device_group", None),
                                  getattr(self, "_play_depth_group", None))
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
            act.triggered.connect(lambda _c=False, idx=i: [
                v.set_device_index(idx) for v in self._audio_lanes.views])
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
            act.triggered.connect(lambda _c=False, b=bits: [
                v.set_play_depth(b) for v in self._audio_lanes.views])
            self._play_depth_group.addAction(act)
        m.setToolTip("24-bit preserves fidelity for 24-bit sources; most output "
                     "devices play both identically by ear.")

        # --- decimation (resample for playback) — per (device, dataport) ---
        self._build_decimation_menu()

    def _build_decimation_menu(self) -> None:
        """(Re)build the Audio ▸ Playback Decimation submenu, for the Audio view a menu
        acts on (`_audio_lane`). Decimation is set per (device, dataport): one submenu per
        stream, each an exclusive rate group. Built as the menu opens, and on a capture
        load; shows a placeholder before one."""
        av = self._audio_lane()
        m = self._play_rate_menu
        # clear() drops the entries but leaves each stream's submenu a child of `m`; it is
        # rebuilt every time it opens, so the old ones are deleted, not piled up.
        for sub in getattr(self, "_play_rate_subs", ()):
            if isValid(sub):                # PySide may have freed one already
                sub.deleteLater()
        m.clear()
        # One group per stream; the decimation menu is rebuilt on every capture load and
        # every re-decode.
        self._retire_action_groups(*getattr(self, "_play_rate_groups", ()))
        self._play_rate_groups = []          # keep the QActionGroups alive
        self._play_rate_subs: list = []      # this build's submenus, deleted by the next
        rates = [("Native (no resample)", None), ("8 kHz", 8000),
                 ("16 kHz", 16000), ("32 kHz", 32000), ("44.1 kHz", 44100),
                 ("48 kHz", 48000), ("96 kHz", 96000)]
        streams = av.streams()
        if not streams:
            act = m.addAction("(load a capture with audio)")
            act.setEnabled(False)
            return
        names = self._lane_session_names(av)
        for dev, dp in streams:
            sub = m.addMenu(with_name(names, dev, dp, f"Device {dev} · DP{dp}"))
            self._play_rate_subs.append(sub)
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

    def _build_processing_menu(self) -> None:
        """(Re)build Audio ▸ Filter & Gain for the Audio view the menu acts on: one
        entry per stream opening its dialog (where Revert is), an asterisk on those that
        have a setting."""
        av = self._audio_lane()
        m = self._process_menu
        m.clear()
        streams = av.streams()
        if not streams:
            act = m.addAction("(load a capture with audio)")
            act.setEnabled(False)
            return
        link = self._lane_link(av)
        store = av._store
        names = self._lane_session_names(av)
        for dev, dp in streams:
            on = store is not None and store.processing(dev, dp) is not None
            m.addAction(with_name(names, dev, dp, f"Device {dev} · DP{dp}") + "…"
                        + (" *" if on else ""),
                        lambda d=dev, p=dp: self.edit_stream_processing(d, p, link=link))

    def show_keyboard_shortcuts(self) -> None:
        """Help ▸ Keyboard Shortcuts: the full keymap, grouped. Keep this in sync when
        a shortcut is added (the accelerators live in _build_menu / mode_controller /
        nav.install_jog_shortcuts). Ctrl is ⌘ on macOS."""
        import sys
        from html import escape
        mac = sys.platform == "darwin"
        mod, alt, shift = ("⌘", "⌥", "⇧") if mac else ("Ctrl+", "Alt+", "Shift+")
        groups = [
            ("Modes", [
                (f"{mod}1", "Bus Visualizer"),
                (f"{mod}2", "Timing Calculator"),
                (f"{mod}3", "Bus Analyzer"),
            ]),
            ("Files (the current mode's)", [
                (f"{mod}O", "Open (a capture / a Visualizer CSV / timing settings)"),
                (f"{mod}S", "Save (the workspace / the Visualizer CSV / timing settings)"),
            ]),
            ("Links", [
                (f"{mod}{alt}1…9", "Show Link 1…9 in every pane"),
                (f"{mod}{alt}0", "Commands: All Links"),
                (f"{mod}{alt}{shift}0", "Signal Panes: All Links"),
            ]),
            ("Command filter (Commands ▸ Filter ▸ Commands)", [
                (f"{mod}4", "All commands"),
                (f"{mod}5", "None"),
                (f"{mod}6", "All excl. Ping"),
                (f"{mod}7", "Commit (SSCR / DSCR and their Commit Points)"),
            ]),
            ("Playhead navigation", [
                ("← / →", "Previous / next command (respects the command filter)"),
                (f"{mod}← / {mod}→", "Previous / next SSCR/DSCR commit"),
                (f"{mod}> / {mod}<", "Next / previous SSCR/DSCR commit (menu)"),
                (f"{mod}]", "Next cursor"),
                (f"{mod}[", "Previous cursor"),
                (f"{mod}B", "Toggle cursor at playhead"),
            ]),
            ("Panes", [
                (f"{mod}{shift}M", "Maximize the selected pane / put the panes back "
                                   "(or double-click its title bar)"),
            ]),
            ("Waveform panes (Audio / Capture, when focused)", [
                (">", "Page the view one width right"),
                ("<", "Page the view one width left"),
            ]),
            ("Manual Stream Sync Point (Decode)", [
                (f"{mod}L", "Move SSP +1 row"),
                (f"{mod}K", "Move SSP −1 row"),
            ]),
            ("Waveform settings", [
                (f"{mod},", "Preferences (waveform colours and line weights)"),
            ]),
            ("Application", [
                (f"{mod}Q", "Quit"),
            ]),
        ]
        # The groups only the Bus Analyzer has keys for: elsewhere they are left out (its
        # menus are hidden there, so the keys do nothing), with a line saying where they are.
        analyzer_only = {"Links", "Command filter (Commands ▸ Filter ▸ Commands)",
                         "Playhead navigation", "Panes",
                         "Waveform panes (Audio / Capture, when focused)",
                         "Manual Stream Sync Point (Decode)", "Waveform settings"}
        analysing = self._mode_mgr_current() == ANALYSIS
        if not analysing:
            groups = [g for g in groups if g[0] not in analyzer_only]
        html = []
        for title, rows in groups:
            html.append(f"<p style='margin-bottom:2px;'><b>{title}</b></p>"
                        "<table cellpadding='2' style='margin-left:8px;'>")
            for keys, desc in rows:
                html.append(f"<tr><td><code>&nbsp;{escape(keys)}&nbsp;</code></td>"
                            f"<td>&nbsp;— {escape(desc)}</td></tr>")
            html.append("</table>")
        if not analysing:
            html.append("<p><i>The Bus Analyzer's keys (Links, the command filter, the playhead, "
                        "the panes, the waveform panes and their settings, the SSP) are listed "
                        "in the Bus Analyzer.</i></p>")
        self._info_box("Keyboard Shortcuts", "".join(html))

    def _info_box(self, title: str, html: str) -> None:
        """A plain informational dialog with NO icon. Read-only rich text."""
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
            "Idle": "bus idle (DP low) before the sequence; before a later bring-up, "
                    "the stretch the clock stopped for (the Link inactive)",
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
            "one per sub-phase, and so is each later one after the Link went inactive "
            "(the safe-lock band a bring-up opens is labelled by the selected PHY, "
            "e.g. <i>PHY2 Safe-Lock-2</i>):</p>"
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

    # ---- one pane maximized ----
    def _pane_of(self, widget):
        """The Bus Analyzer pane `widget` is in: its dock, or "commands" for the Commands
        table; None outside them (a menu, another mode's page, nothing)."""
        w = widget
        while w is not None:
            if isinstance(w, QDockWidget) and w in self._dock_area:
                return w
            if w is self._analysis_page:
                return "commands"
            w = w.parentWidget()
        return None

    def maximized_pane(self):
        """The pane shown alone (a dock, or "commands"), or None."""
        return self._max_pane["pane"] if self._max_pane is not None else None

    def toggle_maximized_pane(self, pane=None) -> None:
        """View ▸ Maximize Pane, its key, a pane's title-bar button or a double-click on the
        title: show one pane alone in the window, or put every pane back as it was.

        Without `pane`, the selected one is the pane holding the keyboard focus, which a
        click in any pane gives it. The arrangement is saved whole (sizes, tab groups, the
        tab in front, floating panes) and restored whole, so nothing about the layout is
        rebuilt by hand. Showing, hiding or closing a pane, adding or removing a Link,
        leaving the Bus Analyzer, or opening a workspace puts the panes back first, and a
        workspace saved meanwhile records them as they will be put back. All Links turned
        off meanwhile resizes the panes put back, not the maximized one. The panes read by
        moving through time keep the Timeline at its height (_MAXIMIZE_KEEPS_TIMELINE).

        The focus stays with the pane throughout. restoreState hides and re-shows docks,
        and Qt moves the focus off a hidden dock to the next widget in the chain, so the
        widget that had it is focused again after a restore; and the pane stays the tab in
        front of its group, since it is the one being worked in.

        In the Timing Calculator (or with `pane` "timing") it gives the Timing Inequalities
        the whole page, the top bar hidden (TimingView.set_maximized)."""
        if pane == "timing" or (pane is None and self._mode_mgr_current() == TIMING):
            self._timing_page.set_maximized(not self._timing_page.is_maximized())
            self._sync_maximize_ui()
            return
        if self._max_pane is not None:
            self.restore_panes()
            return
        if self._mode_mgr_current() != ANALYSIS:
            return
        if pane is None:
            pane = self._pane_of(QApplication.focusWidget())
        if pane is None:
            self._status.setText("Click in a pane to choose the one to maximize")
            return
        if pane is self._timeline_dock:       # navigation for the others, not a view of its own
            self._status.setText("The Timeline is not maximized: click in another pane")
            return
        focus = QApplication.focusWidget()
        if self._pane_of(focus) is not pane:      # a title-bar button: focus is elsewhere
            focus = self._pane_focus_widget(pane)
        bottom = self._raised_bottom_dock()
        self._max_pane = {"pane": pane, "state": self.saveState(), "focus": focus,
                          "shown": [d for d in self._dock_area if not d.isHidden()],
                          "bottom": (bottom, bottom.height() if bottom is not None else 0),
                          "timeline_held": False, "settle": None}
        if isinstance(pane, QDockWidget) and pane.isFloating():
            pane.setFloating(False)               # maximized means in the window
        keep = {pane}
        timeline = self._timeline_dock
        timeline_h = timeline.height()
        if pane in self._timeline_keepers:
            keep.add(timeline)                    # left as it is, open or closed
        for dock in self._dock_area:
            if dock not in keep and not dock.isHidden():
                dock.hide()
        if timeline in keep and isinstance(pane, QDockWidget) \
                and self.dockWidgetArea(pane) != Qt.BottomDockWidgetArea:
            # Beside the Timeline, a pane in a side area gets only what is left under the
            # top area's share of the window, which no resize moves. Below it, the pane
            # takes the rest. The restore puts it back.
            self.addDockWidget(Qt.BottomDockWidgetArea, pane)
        if isinstance(pane, QDockWidget):
            pane.show()
            pane.raise_()                         # a tab behind another comes to the front
            self._central_stack.hide()            # the docks then take the central space
        if timeline in keep and timeline.isVisible() and not timeline.isFloating():
            # With the central page hidden Qt shares its room between the Timeline's area
            # and the pane's, on every resize of the window, and resizeDocks cannot pin it
            # (it moves both areas). Its height is capped until the restore.
            self._max_pane["timeline_held"] = True
            timeline.setMaximumHeight(timeline_h)
        if focus is not None:
            focus.setFocus(Qt.OtherFocusReason)
        self._sync_maximize_ui()

    def restore_panes(self) -> bool:
        """Put back the arrangement a maximize saved; nothing to do if no pane is. Whether
        it did: a resizeDocks in the same pass of the event loop is overridden by the
        restored sizes, so a caller resizing after has to wait a pass."""
        saved, self._max_pane = self._max_pane, None
        if saved is None:
            return False
        timeline = self._timeline_dock
        if saved["timeline_held"]:                # first: the cap would clamp the state
            timeline.setMaximumHeight(_QWIDGETSIZE_MAX)
        self._central_stack.show()                # and this: the state's sizes include it
        if not self.restoreState(saved["state"]):
            for dock in saved["shown"]:           # the state is the window's own; still
                dock.show()
        if saved["settle"] is not None:
            self._settle_bottom_height(*saved["settle"])
        pane, focus = saved["pane"], saved["focus"]
        if isinstance(pane, QDockWidget):
            pane.raise_()
        if focus is None or not isValid(focus) or not focus.isVisible():
            focus = self._pane_focus_widget(pane)
        if focus is not None:
            focus.setFocus(Qt.OtherFocusReason)
        self._sync_maximize_ui()
        return True

    def _pane_focus_widget(self, pane):
        """Where the keyboard focus goes in `pane`: its widget if that takes focus, else its
        largest visible focusable child, the pane's view (a dock's own widget is often a plain
        container, and its first focusable child a toolbar control)."""
        host = self._cmd_view if pane == "commands" else pane.widget()
        if host is None:
            return None
        if host.focusPolicy() != Qt.NoFocus:
            return host
        return max((c for c in host.findChildren(QWidget)
                    if c.focusPolicy() != Qt.NoFocus and c.isVisibleTo(host)),
                   key=lambda c: c.width() * c.height(), default=host)

    def _hide_dock(self, dock: QDockWidget) -> None:
        """Hide a pane (its ✕, or its View-menu item unchecked). The panes are put back
        first, so the restore does not show it again or leave the window empty."""
        self.restore_panes()
        dock.hide()

    def _sync_maximize_ui(self) -> None:
        pane = self.maximized_pane()
        timing = self._timing_page.is_maximized()
        act = getattr(self, "_maximize_action", None)
        if act is not None:
            on = timing if self._mode_mgr_current() == TIMING else pane is not None
            act.setText("Restore Panes" if on else "Maximize Pane")
        _show_maximized(self._timing_max_btn, timing)
        for bar in getattr(self, "_dock_titlebars", []):
            bar.set_maximized(bar._dock is pane)
        _show_maximized(self._cmd_max_btn, pane == "commands")

    def _show_dock(self, dock: QDockWidget) -> None:
        self.restore_panes()                      # showing a pane ends a maximize
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
        worker thread (capture load, sub-capture search, TX persistence) so no QThread
        is destroyed while still running: Qt aborts the process on that."""
        try:
            for v in self._audio_lanes.views:
                v._hard_stop()
        except Exception:  # noqa: BLE001 - never block window close on teardown
            pass
        self.join_worker_threads()
        _PaneTouchFilter.need(self, False)     # a closed window touches no panes
        guide = getattr(self, "_guide", None)
        if guide is not None:
            guide.close()                      # left open, it keeps the app running
        super().closeEvent(event)
        if event.isAccepted():
            self.closed.emit()

    def join_worker_threads(self) -> None:
        """Quit + wait on every worker QThread, clearing each slot.

        Safe to call more than once and at any point (a slot that is None is skipped).
        Exposed separately from closeEvent so a headless caller — a test that builds a
        MainWindow without ever showing or closing it — can release the threads
        deterministically instead of leaving them to interpreter teardown.

        Sets `_closing` FIRST and drops the pending-work queues: a worker's queued
        done/failed signal can still be delivered after wait() returns, and its
        handler would otherwise start the next queued request on a new QThread.
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
        capture, once the event loop is live (the 1 s demo synthesises + decodes ~25M UIs,
        which takes seconds). Before the loop starts (and in headless tests) it loads
        synchronously so the session is ready immediately."""
        n, p, v = _demo_samples(), int(phy), str(variant)
        factory = lambda **kw: Session.from_demo(n, cold_start=True, phy=p,   # noqa: E731
                                                 variant=v,
                                                 register_map=self._rmap, **kw)
        if MainWindow._event_loop_live:
            self._load_async(factory)
        else:
            self.load_session(factory())

    def load_demo_links(self) -> None:
        """The two-Link demo: the PHY2 demo as Link 1 and the flow-control demo
        as Link 2, as one analyzer recording both buses, Link 2 starting
        DEMO_LINK2_DELAY_SAMPLES later. Both at offset 0, since they share the file's
        sample 0. Link 2 is queued only once Link 1 has loaded, as a multi-pair open is."""
        n = _demo_samples()

        def factory(variant: str, delay: int):
            return lambda **kw: Session.from_demo(n, cold_start=True, phy=2,  # noqa: E731
                                                  variant=variant, delay_samples=delay,
                                                  register_map=self._rmap, **kw)
        first = factory("", 0)
        second = factory("flow_control", transitions.DEMO_LINK2_DELAY_SAMPLES)
        if MainWindow._event_loop_live:
            self._load_async(first, kind="open", after=self._after_open(
                lambda _s: self._load_async(second, kind="add"), opening=True))
        else:
            self.load_session(first())
            self.load_session(second(), add_link=True)

    def open_demo(self, key: str) -> None:
        """File ▸ Open Demo Capture: the demo `key` (a workspace.DEMO_WORKSPACES key) with
        its bundled workspace. Each Link decodes from the workspace's demo source at
        _demo_samples() samples per channel, then the workspace is applied as Open Workspace
        applies one: Link, data-port and region names, each Link's view, the panes' state
        and the dock layout. "phy2" is the two-Link demo (load_demo_links)."""
        try:
            with import_notes.collect() as ws_notes:
                ws = demo_workspace(key, _demo_samples())
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Open Demo Capture failed", str(exc))
            return
        name = DEMO_WORKSPACES[key]
        if MainWindow._event_loop_live:
            self._load_workspace_links(ws, name, ws_notes, list(range(len(ws.links))))
            return
        for i, spec in enumerate(ws.links):
            self.load_session(self._link_factory(spec), add_link=i > 0)
        with import_notes.collect() as applied:
            self.apply_workspace(ws, source=name)
        self.show_import_warnings(list(ws_notes) + applied, name)

    def load_demo_bringup(self) -> None:
        """Alias for load_demo (PHY2) — kept for the app's --demo-bringup flag."""
        self.load_demo(phy=2)

    # Per-mode last-opened-file directory: the Analyzer (captures, .sal export, sub-
    # capture) and the Visualizer (config CSVs) browse independently, so opening one
    # doesn't jump the other's dialog to an unrelated folder. Keys are QSettings
    # "capture/last_dir_<mode>"; the legacy unscoped "capture/last_dir" seeds the
    # analyzer's while its own key is unset.
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
        """Decode ▸ Import Visualizer CSV: pick a config (a CSV file or the current
        Visualizer authoring) and either impose it on the open capture (grid + registers
        from row 0) or compare it against the decode (Register Map overlay)."""
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
        # markers ingest.probe uses to reject a config as a capture). Reject the inverse here.
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
        # A compare alone shows the CSV's import warnings once it finishes; an update
        # re-reads the CSV on the worker, which reports them, so they are shown once.
        try:
            with import_notes.collect() as found:
                cfg_path = visualizer_csv.normalized_path(dlg.path)
                if not dlg.do_update:
                    visualizer_csv.report_csv_warnings(cfg_path, dlg.path)
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
            else:
                self.show_import_warnings(found)
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
        (same path as the Scrambler override); the cursor and bookmarks are kept.

        A capture with SEVERAL config regions takes the CSV into the region under the
        cursor only (Session.apply_config_csv_at): a CSV describes one bus config. A
        capture with one region keeps the whole-capture import, which is what a
        post-commit capture (no config on the wire at all) needs."""
        s = self._session
        if s is None:
            return
        regions = len(s.segments or [])
        cursor = self.cursor.sample
        scoped = regions > 1
        sect = s.segment_index_for_sample(int(cursor)) if scoped else 0
        # Two ways the import can quietly not mean what the user expects, collected into
        # ONE prompt rather than two in a row:
        #  * the CSV's width is not the region's: the region will be framed at the CSV's;
        #  * a port whose columns lie beyond the width is simply NOT PLACED.
        concerns = []
        cap_cols = int((s.column_count_at(int(cursor)) if scoped
                        else getattr(s, "column_count", 0)) or 0)
        try:
            with import_notes.collect():      # the re-decode reports this CSV's warnings
                cfg = self._bus_config_from_csv(csv)
        except Exception:                     # noqa: BLE001 — unreadable CSV is
            cfg = None                        # reported by the caller, not here
        where = f"region {sect + 1} of {regions}" if scoped else "the capture"
        if cfg is not None and cap_cols > 0:
            csv_cols = int(cfg.column_count())
            if scoped and csv_cols != cap_cols:
                concerns.append(
                    f"This CSV is a {csv_cols}-column config, and {where} is {cap_cols} "
                    f"columns on the wire. The region will be decoded at {csv_cols} columns.")
                cap_cols = csv_cols
            # horizontal_count is excess-1, so the last column owned is start+count.
            over = [(i, dp) for i, dp in enumerate(cfg.dataports)
                    if dp.enabled and dp.enable_ch
                    and dp.horizontal_start + dp.horizontal_count >= cap_cols]
            if over:
                names = ", ".join(
                    f"{dp.name or f'DP{i}'} (cols "
                    f"{dp.horizontal_start}-{dp.horizontal_start + dp.horizontal_count})"
                    for i, dp in over)
                whose = "region" if scoped else "capture"
                concerns.append(
                    f"{len(over)} port(s) in this config need columns beyond the "
                    f"{whose}'s decoded width of {cap_cols}: {names}. They will not "
                    "be placed, and the grid will come back without them.")
        if concerns:
            resp = QMessageBox.warning(
                self, "Import Visualizer CSV",
                "\n\n".join(concerns) + f"\n\nImpose it on {where} anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
        self._stop_audio()
        # Bookmarks need no snapshot: a re-decode keeps its Link, and with it the bookmarks.
        sess, keep_cursor = s, cursor

        def apply(s=sess, p=csv, at=cursor):
            if scoped:
                s.apply_config_csv_at(int(at), p)
            else:
                s.apply_config_csv(p)
            return s

        def done(_s, cur=keep_cursor, p=csv, w=where):
            if _s is not self._session:
                return                  # another Link was shown meanwhile; nothing to restore
            self._restore_cursor(cur)
            rest = " The other regions decode as the wire says." if scoped else ""
            self._status.setText(f"Imposed {os.path.basename(p)} on {w} "
                                 f"(grid + register map from CSV; re-decoded).{rest}")

        self._load_async(apply, after=done, kind="redecode")

    @_on_grid_link
    def _on_dp_key_clicked(self, device: int, dp: int) -> None:
        """A data-port swatch in the Bus Grid colour key was clicked: choose which
        fields that port's cell labels show (Sample / Channel / Bit).

        This is the Visualizer's per-port display-fields choice, applied to the
        decoded grid.
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
            # land in the grid window at the cursor. Read the CONFIG per region instead of
            # the rendered cells, and include the final config so a port disabled before
            # the last region is still covered.
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

    def _update_remove_region_csv(self) -> None:
        s = self._session
        path = s.csv_section_at(self.cursor.sample) if s is not None else ""
        act = self._remove_region_csv_action
        act.setEnabled(bool(path))
        act.setText(f"&Remove {os.path.basename(path)} from This Region" if path
                    else "&Remove Region's Visualizer CSV")

    def remove_region_csv(self) -> None:
        """Decode ▸ Remove … from This Region: drop the config CSV imported into the region
        under the cursor (Session.apply_config_csv_at with no path) and re-decode, so the
        region decodes as the wire says again. Other regions' CSVs stay."""
        s = self._session
        if s is None:
            return
        sample = self.cursor.sample
        path = s.csv_section_at(sample)
        if not path:
            self._status.setText("The region under the playhead has no imported CSV.")
            return
        self._stop_audio()
        sect = s.segment_index_for_sample(int(sample))

        def apply(_s=s, smp=sample):
            _s.apply_config_csv_at(int(smp), None)
            return _s

        def done(_s, cur=sample, name=os.path.basename(path), i=sect):
            if _s is not self._session:
                return                  # another Link was shown meanwhile; nothing to restore
            self._restore_cursor(cur)
            self._status.setText(f"Removed {name} from region {i + 1} (re-decoded as the wire "
                                 "says).")

        self._load_async(apply, after=done, kind="redecode")

    def force_column_count(self) -> None:
        """Decode ▸ Force Column Count: pin the bus column count for the config
        region under the cursor, and re-decode.

        Region-scoped: only the cursor's region is pinned, and the rest decode as the
        wire says. The pinned region is labelled '(forced)' in the timeline and persists
        in the workspace."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        s = self._session
        sample = self.cursor.sample
        sect = s.segment_index_for_sample(int(sample))
        current = int(s.forced_column_sections.get(sect, 0))
        wire = int(s.column_count_at(int(sample)))
        n_regions = max(1, len(s.segments))
        # Offer the imported config CSV's width as the default when there is one.
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
        self._stop_audio()
        sess, keep_cursor = s, sample

        def apply(_s=sess, smp=sample, v=val):
            _s.force_column_count_at(int(smp), int(v))
            return _s

        def done(_s, cur=keep_cursor, v=val, i=sect):
            if _s is not self._session:
                return                  # another Link was shown meanwhile; nothing to restore
            self._restore_cursor(cur)
            self._status.setText(
                f"Region {i + 1} pinned to {v} columns (re-decoded)." if v
                else f"Region {i + 1} pin removed (re-decoded).")

        self._load_async(apply, after=done, kind="redecode")

    def open_capture(self, config_csv: str = "", caption: str = "",
                     prefer_add: bool = False) -> None:
        """File ▸ Open Capture (⌘O): pick the file(s) — one capture, or both channel files of
        a `.bin` / `.wfm` pair — then the Open Capture dialog (ui/open_capture_dialog.py)
        says what the file is and asks, in one place, for its Links (name, clock, data),
        how much of it to decode, and whether it replaces or adds to the open Links.
        `prefer_add` starts on Add. `config_csv` (optional) supplies the
        data-port config to decode with from row 0; Decode ▸ Import Visualizer CSV imposes
        one after opening."""
        if getattr(self, "_probing", False):
            return                          # a file is being read for the dialog already
        paths, _ = QFileDialog.getOpenFileNames(
            self, caption or "Open capture (.sal, .csv, .vcd, or both .bin/.wfm channel files)",
            self._last_capture_dir(),
            filter="Captures (*.sal *.csv *.bin *.vcd *.wfm);;Saleae project (*.sal);;"
                   "Digital CSV (*.csv);;Saleae binary (*.bin);;VCD (*.vcd);;"
                   "Tektronix waveform (*.wfm);;All files (*)")
        if not paths:
            return
        self._remember_capture_dir(paths[0])
        from ..ingest import saleae_sal
        from .open_capture_dialog import OpenCaptureDialog
        try:
            pr = self._probe_files(paths)
        except saleae_binary.UnsupportedSaleaeVersion as exc:
            QMessageBox.warning(self, "Unsupported format", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface a file that is not a capture
            QMessageBox.warning(self, "Open Capture", str(exc))
            return
        if pr is None:
            return                          # the window began closing while it was read
        if len(pr.channels) < 2:
            QMessageBox.warning(self, "Open Capture",
                                f"{pr.name}: fewer than two channels to use as clock and data.")
            return
        try:
            budget = saleae_sal.memory_budget()
        except Exception:  # noqa: BLE001 - no budget: the dialog shows no fit verdict
            budget = 0
        dlg = OpenCaptureDialog(pr, [link.name for link in self._links], self,
                                prefer_add=prefer_add and len(self._links) > 0,
                                budget_bytes=budget,
                                large_bytes=saleae_sal.large_load_bytes())
        if dlg.exec() != QDialog.Accepted:
            return
        self._open_request(pr, dlg.result_request(), config_csv=config_csv)

    def _probe_files(self, paths):
        """`ingest.probe.probe(paths)` off the GUI thread, behind a busy dialog, returning
        its result (or raising its error) here. A probe reads a bounded prefix, which can
        still take a second or two of a multi-GB CSV or a compressed .sal. Before the event
        loop runs (and in headless tests) it is a
        plain call, as a demo load is.

        The wait runs the event loop, so the dialog must hold the window until the read
        is done (_BusyDialog cannot be dismissed), and None comes back if the window began
        closing meanwhile: its caller then stops rather than open a dialog on a closed
        window."""
        from ..ingest import probe as probe_mod
        if not MainWindow._event_loop_live:
            return probe_mod.probe(paths)
        import threading
        box: dict = {}

        def run() -> None:
            try:
                box["pr"] = probe_mod.probe(paths)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the GUI thread
                box["exc"] = exc
        worker = threading.Thread(target=run, name="swi3s-probe", daemon=True)
        worker.start()
        worker.join(0.25)                       # a quick probe shows no dialog at all
        if worker.is_alive():
            dlg = _BusyDialog(f"Reading {os.path.basename(paths[0])}…", "Open Capture", self)
            dlg.show()
            app = QApplication.instance()
            self._probing = True
            try:
                while worker.is_alive():
                    app.processEvents(QEventLoop.AllEvents, 50)
                    worker.join(0.02)
            finally:
                self._probing = False
                dlg.finish()
        if getattr(self, "_closing", False):
            return None
        if "exc" in box:
            raise box["exc"]
        return box["pr"]

    def _open_request(self, pr, req, config_csv: str = "") -> None:
        """Load what the Open Capture dialog asked for: one Link per row, from one file, so
        on one timeline; the first with the dialog's kind (replace, or add), the rest added
        once it has LOADED, so a failure does not leave half of this file's Links on top of
        the analysis that was open. Each Link gets its name, and when adding, the offset."""
        rate = req.sample_rate_hz or int(pr.sample_rate_hz or 0)
        # The window goes to the loader in seconds: only the loaded capture knows its rate
        # (a complementary CSV pair loads at its DLV rate, not the probe's).
        window = req.window_s
        kind = "add" if req.add else "open"
        # The offset places the FILE's time zero; a window is rebased to its own start, so
        # an added window sits that much later, in line with the same file opened whole.
        start_s = req.window_s[0] if req.window_s is not None else 0.0
        offset_ps = int(round((req.offset_s + start_s) * 1e12)) if req.add else 0
        rmap = self._rmap

        def factory(link):
            k = pr.kind
            if k == "sal":
                return lambda **kw: Session.from_sal(
                    pr.paths[0], link.clock, link.data, auto_clock=link.auto_clock,
                    window_s=window, register_map=rmap, config_csv=config_csv, **kw)
            if k == "digital_csv":
                return lambda **kw: Session.from_digital_csv(
                    pr.paths[0], link.clock, link.data, window_s=window, register_map=rmap,
                    config_csv=config_csv, **kw)
            if k == "vcd":
                return lambda **kw: Session.from_vcd(
                    pr.paths[0], link.clock, link.data, auto_clock=link.auto_clock,
                    window_s=window, register_map=rmap, config_csv=config_csv, **kw)
            if k == "saleae_binary":
                return lambda **kw: Session.from_saleae_binary(
                    link.clock, link.data, rate, window_s=window, register_map=rmap,
                    config_csv=config_csv, **kw)
            if k == "wfm":
                return lambda **kw: Session.from_wfm(
                    link.clock, link.data, clock_thresh=link.clock_thresh,
                    data_thresh=link.data_thresh, auto_clock=link.auto_clock,
                    window_s=window, register_map=rmap, **kw)
            if k == "analog_csv":
                return lambda **kw: Session.from_analog_csv(
                    pr.paths[0], clock=link.clock, data=link.data,
                    clock_thresh=link.clock_thresh, data_thresh=link.data_thresh,
                    auto_clock=link.auto_clock, window_s=window, register_map=rmap,
                    config_csv=config_csv, **kw)
            raise ValueError(f"no loader for {k!r}")

        def named(link, session) -> None:
            idx = self._links.index_of(session)
            if idx < 0:
                return
            self._links.rename(idx, link.name)
            if offset_ps:
                self.set_link_offset(idx, offset_ps)
            # Everywhere the name shows, as rename_link does.
            self._update_link_switcher()
            self._update_title()
            if self._cmd_all:
                self._refresh_all_model()
            self._push_bookmarks()
            span = (f", {req.window_s[0]:.3f}–{req.window_s[1]:.3f} s"
                    if req.window_s is not None else "")
            self._status.setText(f"Opened {pr.name} as {link.name}{span}")

        first, rest = req.links[0], req.links[1:]

        def more(_session) -> None:
            for link in rest:
                self._load_async(factory(link), after=lambda s, ln=link: named(ln, s),
                                 kind="add")

        def first_loaded(session) -> None:
            named(first, session)
            more(session)

        self._load_async(factory(first), kind=kind,
                         after=self._after_open(first_loaded, opening=kind == "open"))

    def _after_open(self, then, opening: bool):
        """`then(session)`, for the `after` of a load about to be asked for, unless another
        open is asked for in between: the decode dialog is not modal, so a second Open can
        be queued while the first file's first Link decodes, and that file's other Links
        must not land on the newer one. `opening`: the load it is for is itself an open
        (which counts, once asked for)."""
        gen = self._open_gen + (1 if opening else 0)
        return lambda session: then(session) if self._open_gen == gen else None

    def locate_subcapture(self) -> None:
        """File ▸ Locate Sub-Capture (Analyzer): open a SECOND capture (any format the Open
        Capture dialog accepts — select BOTH files together for the two-file .bin /
        .wfm formats) and find every place its signal pattern occurs in the
        currently-open capture (Session.locate_subcapture — sample-rate independent).
        Drops a bookmark at each match's start_sample, labeled with its rank + score,
        and reports the count.

        The search itself (up to 4 FFT correlation passes — the orientation retry —
        over a possibly multi-million-UI capture) runs on a worker thread (see
        _LocateWorker), with a progress dialog."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        if getattr(self, "_probing", False):
            return                          # a file is being read for a dialog already
        # ONE SEARCH AT A TIME, refused BEFORE the file dialog. A second search would
        # overwrite _locate_thread / _locate_worker / _locate_dlg / _locate_path while the
        # first worker still runs, and the abandoned QThread would never be joined.
        #
        # Not queued, unlike a re-decode: the input is a file the user chose, so the user is
        # told the previous search is still running.
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_locate_thread", None) is not None:
            QMessageBox.information(
                self, "Search already running",
                "A sub-capture search is already in progress. Wait for it to finish "
                "before starting another.")
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
        # The reference's signals (and how much of it) are picked in Open Capture's dialog,
        # in its reference form: one row, no name, no Into.
        from .open_capture_dialog import OpenCaptureDialog
        try:
            pr = self._probe_files(paths)
        except Exception as exc:  # noqa: BLE001 - surface a file that is not a capture
            QMessageBox.warning(self, "Locate Sub-Capture", str(exc))
            return
        if pr is None:
            return                          # the window began closing while it was read
        if len(pr.channels) < 2:
            QMessageBox.warning(self, "Locate Sub-Capture",
                                f"{pr.name}: fewer than two channels to use as clock and data.")
            return
        pick = OpenCaptureDialog(pr, parent=self, reference=True)
        if pick.exec() != QDialog.Accepted:
            return    # cancelled — silent, no error
        path = pr.paths[0]
        try:
            import os as _os
            import sys as _sys
            import time as _t
            _t0 = _t.perf_counter()
            with self.reporting_import():
                sub = self._reference_capture(pr, pick.result_request())
            if _os.environ.get("SWI3S_LOCATE_DEBUG"):
                _n = sub.clock_edges.size if sub is not None else 0
                _mn = self._session.capture.clock_edges.size if self._session else 0
                print(f"[locate] load sub ({os.path.basename(path)}): "
                      f"{_t.perf_counter() - _t0:.2f}s (sub clk={_n:,}, main clk={_mn:,})",
                      file=_sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - surface ingest errors
            QMessageBox.critical(self, "Locate failed", str(exc))
            return
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
        # The matches are samples of THIS session: they are bookmarked on the Link holding
        # it when the search finishes, whichever Link is active then, and dropped if no
        # Link holds it any more (an open replaced the analysis while the search ran).
        self._locate_session: Optional[Session] = self._session
        self._locate_worker = _LocateWorker(self._session, sub)
        self._locate_worker.moveToThread(self._locate_thread)
        self._locate_thread.started.connect(self._locate_worker.run)
        # Connect a BOUND METHOD (not a lambda): PySide can't resolve a bare lambda's
        # thread affinity, so it wires it as a DirectConnection — the slot would then run
        # on the WORKER thread, and _teardown_locate_thread's self._locate_thread.wait()
        # would be a thread waiting for ITSELF → permanent deadlock. A bound method of this
        # QMainWindow gets a QueuedConnection onto the GUI thread, like _LoadWorker.done.
        # Path is stashed for the slot to read.
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
        # self._locate_dlg after it may have been set to None.
        dlg.setValue(percent)

    def _on_locate_done(self, matches: list, diag: dict) -> None:
        self._finish_locate_subcapture(self._locate_path, matches, diag)

    def _finish_locate_subcapture(self, path: str, matches: list, diag: dict) -> None:
        self._teardown_locate_thread()
        searched = getattr(self, "_locate_session", None)
        if searched is None:
            return                          # this search's result was already handled
        self._locate_session = None
        link = self._links.index_of(searched)
        if link < 0:
            self._status.setText(
                f"Locate Sub-Capture: results for {os.path.basename(path)} discarded; "
                f"the capture searched is no longer open")
            return
        if not matches:
            from ..analysis.subcapture import DEFAULT_SCORE_THRESHOLD
            best = float(diag.get("best_score", 0.0))
            best_start = diag.get("best_start_sample")
            placed = ""
            if best_start is not None:
                bm = self._bookmarks.add(int(best_start), link=link)
                bm.label = f"Best (no match)  score={best:.3g}"
                self._push_bookmarks()
                placed = ("\n\nA cursor was placed at the best candidate so you "
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
            bm = self._bookmarks.add(int(m["start_sample"]), link=link)
            bm.label = f"Sub {i}/{len(matches)}  score={m.get('score', 0):.3g}"
        self._locate_dbg("added %d bookmarks" % len(matches))
        self._push_bookmarks()
        self._locate_dbg("push_bookmarks")
        self._status.setText(
            f"Locate Sub-Capture: {len(matches)} match(es) marked with cursors "
            f"from {os.path.basename(path)}")
        # No icon on the matches announcement (plain informational text).
        box = QMessageBox(self)
        box.setWindowTitle("Locate Sub-Capture")
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setText(f"Found {len(matches)} match(es); cursors placed at each "
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


    @staticmethod
    def _reference_capture(pr, req):
        """A bare Capture (no Session, no decode) of what Locate's dialog picked: the
        reference is searched for, so only its edges are needed. Same readers as Open
        Capture, cut to the window when one was asked for."""
        from ..ingest import analog, analog_csv, digital_csv, saleae_sal, vcd
        from ..ingest import wfm as wfm_ingest
        link = req.links[0]
        rate = req.sample_rate_hz or int(pr.sample_rate_hz or 0)
        window = None
        if req.window_s is not None and pr.kind == "sal" and rate:
            window = Session.window_samples(req.window_s, rate)
        k = pr.kind
        if k == "sal":
            return saleae_sal.load_capture(pr.paths[0], link.clock, link.data,
                                           auto_clock=link.auto_clock, window=window)
        if k == "vcd":
            cap = vcd.load_capture(pr.paths[0], link.clock, link.data,
                                   auto_clock=link.auto_clock)
        elif k == "digital_csv":
            cap = digital_csv.load_capture(pr.paths[0], link.clock, link.data)
        elif k == "saleae_binary":
            cap = saleae_binary.load_capture(link.clock, link.data, rate)
        elif k == "analog_csv":
            parsed = analog_csv.read_analog_csv(pr.paths[0])
            cap = analog.capture_from_analog(
                parsed["time"], parsed["channels"][link.clock], parsed["channels"][link.data],
                parsed["sample_rate_hz"] or None, link.clock_thresh, link.data_thresh,
                auto_clock=link.auto_clock, source=pr.name)
        elif k == "wfm":
            a, b = wfm_ingest.read_wfm_pair(link.clock, link.data)
            cap = analog.capture_from_analog(a["time"], a["volts"], b["volts"], None,
                                             link.clock_thresh, link.data_thresh,
                                             auto_clock=link.auto_clock, source=pr.name)
        else:
            raise ValueError(f"{pr.name}: unsupported sub-capture format")
        if req.window_s is None:
            return cap
        # At the rate the reference loaded at, as Open Capture's windows are.
        return cap.subcapture(*Session.window_samples(req.window_s, cap.sample_rate_hz))

    def export_capture(self) -> None:
        """File ▸ Export Capture… (Analyzer): one dialog to pick the format (.sal /
        .bin / .csv), which signals + their names, the range (whole capture, or a
        time / bus-row / UI window), and the output file — then dispatch to the
        matching Session.export_* writer."""
        if self._session is None:
            QMessageBox.information(self, "No capture", "Open a capture first.")
            return
        from .capture_export_dialog import CaptureExportDialog
        # The session is fixed BEFORE the dialog: the range is chosen on THIS capture, and
        # closing the dialog returns focus to a pane, which can make another Link active.
        sess = self._session
        clk_name, dat_name = ("DP", "DN") if sess.is_dlv else ("SW_CLK", "SW_DATA")
        dlg = CaptureExportDialog(sess, default_dir=self._last_capture_dir("analyzer"),
                                  clock_name=clk_name, data_name=dat_name,
                                  default_name=f"capture{self._link_stem()}", parent=self)
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
                sess.export_sal(path, clock_channel=0, data_channel=1,
                                clock_name=clk_name, data_name=dat_name, sample_range=rng)
                wrote = path
            elif fmt == "bin":
                written = sess.export_bin(path, clock_channel=0, data_channel=1,
                                          include_clock=clk_on, include_data=dat_on,
                                          sample_range=rng)
                wrote = ", ".join(os.path.basename(p) for p in written)
            else:                                          # csv
                sess.export_csv(path, clock_channel=0, data_channel=1,
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
            os.path.join(self._last_capture_dir("analyzer"), f"workspace{WORKSPACE_SUFFIX}"),
            f"Workspace (*{WORKSPACE_SUFFIX})")
        if not path:
            return
        if not os.path.splitext(path)[1]:         # an extension the user typed is kept
            path += WORKSPACE_SUFFIX
        self._remember_capture_dir(path, "analyzer")
        self._keep_shown_views()                  # the panes on screen, into their Links
        ws = Workspace(
            links=[self._link_spec(link) for link in self._links],
            active_link=self._links.active_index,
            bookmarks=self._bookmarks.to_json(),
            cursor=self.cursor.sample,
            mode=self._mode_mgr.current() or ANALYSIS,
            authoring=self._authoring.config().to_dict(),
            timing=self._timing_page.to_dict(),
            view={**self._link_view_prefs(),
                  "tx_map": self._tx_map, "tx_persist": self._tx_persist,
                  "grid_rows": self._grid_rows, "tx_start_row": self._tx_start_row,
                  "show_clock": self._raw_view.show_clock,
                  **self._window_view_state()},
        )
        try:
            ws.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._status.setText(f"Workspace saved: {path}")

    def _link_view_prefs(self) -> dict:
        """The multi-Link layout, saved only when there is one: each pane group's Link,
        whether Commands shows All Links, and the Timeline dock's height."""
        if len(self._links) < 2:
            return {}
        return {"pane_links": dict(self._pane_link), "commands_all": bool(self._cmd_all),
                "bottom_all": bool(self._bottom_all),
                "timeline_height": int(self._timeline_dock.height())}

    def _apply_link_view_prefs(self, view: dict, active: int) -> None:
        if len(self._links) < 2:
            return
        panes = dict(view.get("pane_links") or {})
        # A workspace saved before 3.0.19 has no "grid" entry: the Bus Grid shows the
        # bottom group's Link.
        panes.setdefault("grid", panes.get("bottom", active))
        for group in _PANE_GROUPS:
            try:
                i = int(panes.get(group, active))
            except (TypeError, ValueError):     # tolerate a hand-edited/corrupt workspace
                self._ws_note(f"the {group} panes' saved Link {panes.get(group)!r} is not "
                              f"a number; they show the active Link")
                continue
            if 0 <= i < len(self._links) and i != self._pane_link[group]:
                self.set_group_link(group, i)
        if view.get("commands_all"):
            self.set_commands_scope_all(True)
        if view.get("bottom_all"):
            self.set_bottom_scope_all(True)
        self._activate_link(active)            # the saved active Link, not the last set
        self._update_link_switcher()
        try:
            h = int(view.get("timeline_height", 0))
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            self.resizeDocks([self._timeline_dock], [h], Qt.Vertical)

    @staticmethod
    def _link_spec(link) -> LinkSpec:
        """What a workspace records to rebuild one Link."""
        sess = link.session
        return LinkSpec(
            source=sess.source, name=link.name, offset_ps=int(link.offset_ps),
            overlay=[[int(s), int(d), int(a), int(v)]
                     for (s, d, a), v in sess.register_overrides.items()],
            device_regmaps={str(d): pm.to_json_dict() for d, pm in sess.peripheral_maps.items()},
            device_names={str(d): n for d, n in sess.device_names.items()},
            device_hub_depths={str(d): v for d, v in sess.hub_depths.items() if v},
            device_scramblers=[[int(d), int(p), bool(on)]
                               for (d, p), on in sess.scrambler_overrides.items()],
            stream_colors=line_style.overrides_to_json(
                getattr(link.view, "stream_colors", None) or {}),
            stream_processing=[[int(d), int(p), proc.to_json()] for (d, p), proc in
                               sorted((getattr(link.view, "stream_processing", None)
                                       or {}).items())],
            even_timing=[[int(d), int(p)] for d, p in
                         sorted(getattr(link.view, "even_timing", None) or ())],
            view={"commands": {"filters": view_state.filters_to_json(link.view.filters),
                               "col_widths": dict(link.view.col_widths or {})},
                  "audio": link.view.audio_view, "capture": link.view.capture_view},
        )

    def open_workspace(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open workspace", self._last_capture_dir("analyzer"),
            f"Workspace (*{WORKSPACE_SUFFIX} *.json)")
        if not path:
            return
        self._remember_capture_dir(path, "analyzer")
        name = import_notes.source_name(path)
        try:
            with import_notes.collect() as ws_notes:
                ws = Workspace.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        skipped = self._locate_missing_captures(ws, path)
        if skipped is None:
            return
        order = [i for i in range(len(ws.links)) if i not in skipped]
        if not order:
            self._status.setText("Open workspace: every Link was skipped; nothing opened.")
            self.show_import_warnings(ws_notes, name)
            return
        self._load_workspace_links(ws, name, ws_notes, order)

    def _load_workspace_links(self, ws: Workspace, name: str, ws_notes, order: list) -> None:
        """Decode the saved Links `order` names (indices into ws.links) through the load
        queue, then apply `ws` to them. `ws_notes` are the warnings reading the workspace
        gave, shown with the apply's under `name`."""
        first_i, rest = order[0], order[1:]
        # Apply saved hub depths AND what-if register overrides inside the worker-
        # thread factory (both change the decode), in a single off-GUI-thread decode per
        # Link. Doing it in apply_workspace (GUI thread) would re-decode again and freeze
        # the UI. The Links decode one after another through the load queue: the first
        # replaces the analysis, the rest are added (never dropped by the queue), and the
        # view state is applied once the LAST one has loaded.
        #
        # A Link skipped at the missing-capture prompt is left out of the load list: the
        # first Link not skipped is the open, the others are added. The Links after the
        # first are queued only once the first has LOADED: if it fails, nothing else runs.
        # Each Link that loads records which saved Link it is and its
        # Session, and the view state is applied once every Link has settled, loaded or
        # failed, to the Link that holds each Session then (self._links.index_of). A failure
        # in the middle, a Link added from Open Capture while these load, or a Link removed,
        # leaves each saved Link's names, offsets and bookmarks on its own capture.
        state: Dict[str, Any] = {"loaded": [], "waiting": 0}   # (saved Link, Session); still loading

        def finish(w=ws):
            # The workspace's own warnings, its parse's and the apply's, in one window.
            where = {spec_i: self._links.index_of(sess) for spec_i, sess in state["loaded"]}
            with import_notes.collect() as applied:
                self.apply_workspace(w, where={k: v for k, v in where.items() if v >= 0},
                                     source=name)
            self.show_import_warnings(list(ws_notes) + applied, name)

        def settle(i: int, session) -> None:
            if session is not None:
                state["loaded"].append((i, session))
            state["waiting"] -= 1
            if state["waiting"] == 0:
                finish()

        def first_loaded(session, w=ws) -> None:
            state["loaded"].append((first_i, session))
            state["waiting"] = len(rest)
            if not rest:
                finish()
            for i in rest:
                self._load_async(lambda spec=w.links[i], **kw: self._link_factory(spec, **kw),
                                 after=lambda s, i=i: settle(i, s), kind="add",
                                 on_fail=lambda i=i: settle(i, None))

        # Through _after_open, as Open Capture's: a newer open asked for while the first
        # Link decodes must not get this workspace's other Links added on top of it.
        self._load_async(lambda spec=ws.links[first_i], **kw: self._link_factory(spec, **kw),
                         after=self._after_open(first_loaded, opening=True), kind="open",
                         on_fail=lambda: self.show_import_warnings(ws_notes, name))

    def _link_factory(self, w: LinkSpec, **kw) -> Session:
        """Rebuild one saved Link's Session, decode inputs and all, on the load worker."""
        # **kw absorbs decoder_ready (see _factory_takes_decoder_ready /
        # _LoadWorker.run). session_from_source takes no decoder_ready, so it is not
        # forwarded to Session.from_x.
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

    def apply_workspace(self, ws: Workspace, loaded=None, where=None, source: str = "") -> None:
        """Apply view state (bookmarks, cursor, active Link, pane layout) and each Link's
        name, offset and device metadata onto the loaded Links.

        A saved setting that does not apply (a stream colour or Filter & Gain row that does
        not read, a register map's unreadable entries, an unknown mode, a pane's Link that
        is not open, a view value that is not a number) is reported through import_notes
        under `source`, the workspace file's name.

        `where` maps each loaded saved Link's index to the index of the Link it loaded as.
        Without it, `loaded` lists, in Link order, which of the workspace's Links each
        loaded Link is (default: all of them, in order). A saved Link that failed to load is
        simply absent, and everything that referred to it (its bookmarks, a pane group
        showing it) is dropped or falls back, instead of landing on its neighbour."""
        self._ws_source = source
        try:
            self._apply_workspace(ws, loaded, where, source)
        finally:
            self._ws_source = None

    def _apply_workspace(self, ws: Workspace, loaded, where, source: str) -> None:
        if where is None:
            loaded = list(range(len(ws.links))) if loaded is None else list(loaded)
            where = {spec_i: link_i for link_i, spec_i in enumerate(loaded)
                     if link_i < len(self._links)}
        else:
            where = {int(k): int(v) for k, v in where.items()
                     if 0 <= int(k) < len(ws.links) and 0 <= int(v) < len(self._links)}
        active = where.get(ws.active_link, 0)
        for spec_i, link_i in where.items():
            link, spec = self._links[link_i], ws.links[spec_i]
            link.name = spec.name
            link.offset_ps = int(spec.offset_ps)
            if link.view is not None:
                link.view.stream_colors = line_style.overrides_from_json(spec.stream_colors)
                link.view.stream_processing = _processing_from_json(spec.stream_processing)
                link.view.even_timing = {(int(e[0]), int(e[1])) for e in spec.even_timing or ()
                                         if isinstance(e, list) and len(e) == 2}
                lost_c = len(spec.stream_colors or ()) - len(link.view.stream_colors)
                lost_p = _processing_rows_unread(spec.stream_processing)
                for n, what in ((lost_c, "stream colour"), (lost_p, "Filter & Gain setting")):
                    if n > 0:
                        import_notes.warn(source, f"Link {spec_i + 1} ({spec.name}): {n} saved "
                                                  f"{what}{'s' if n != 1 else ''} could not "
                                                  f"be read and {'was' if n == 1 else 'were'} "
                                                  f"not restored")
                if link.view.stream_processing or link.view.even_timing:   # panes drawn first
                    self._apply_stream_processing(link.view)
                    self._refresh_processed_views(link_i)
            # Device names and register maps are UI metadata (cheap). Hub depths were
            # already applied in the worker-thread factory and reflected by load_session,
            # so there's no re-decode here.
            if spec.device_names:
                link.session.device_names = {int(d): n for d, n in spec.device_names.items()}
            if spec.device_regmaps:
                from ..model.regmap_import import PeripheralRegisterMap
                for dev_str, doc in spec.device_regmaps.items():
                    link.session.set_peripheral_map(
                        int(dev_str), PeripheralRegisterMap.from_json_dict(doc, source))
                # The Commands table took a copy of the maps when the Link loaded, before
                # these: give it them, or its vendor register names stay unresolved.
                if link.view is not None and link.view.cmd_model is not None:
                    link.view.cmd_model.set_peripheral_maps(link.session.peripheral_maps)
        if self._cmd_all:                       # it draws through the per-Link models
            self._refresh_all_model()
        # The offsets are assigned directly, and each band's domain was computed as its Link
        # was added, at offset 0: recompute it once for all of them, or every band but the
        # first draws unshifted until a zoom re-syncs it.
        self._update_timeline_domain()
        if 0 <= active < len(self._links):
            self.switch_link(active)
        self._push_stream_colors()              # switch_link is a no-op on the shown Link
        self._update_link_switcher()
        self._update_title()
        bms = BookmarkSet.from_json(ws.bookmarks)
        # A bookmark on a Link that did not load has nowhere to be shown; keep the rest,
        # renumbered to the Links as loaded.
        kept = []
        for b in bms:
            if b.link in where:
                b.link = where[b.link]
                kept.append(b)
        self._bookmarks = BookmarkSet(kept)
        self._push_bookmarks()
        self.cursor.set_sample(ws.cursor)
        shown = ws.links[ws.active_link] if ws.active_link in where else None
        if self._session is not None and shown is not None:
            if shown.device_names:
                self._reg_view.set_device_names(self._session.device_names)
            if shown.device_regmaps:
                self._reg_view.set_peripheral_maps(self._session.peripheral_maps)
                self._reg_view.set_files(self._session.register_files_at(self.cursor.sample))
        if ws.authoring:
            _note_authoring_keys(ws.authoring, source)
            self._authoring.set_config(BusConfig.from_dict(ws.authoring))
        if ws.timing:
            self._timing_page.set_from_dict(ws.timing, source=source)
        self._apply_view_prefs(ws.view)
        view = dict(ws.view or {})
        if isinstance(view.get("pane_links"), dict):
            odd = sorted(str(g) for g, i in view["pane_links"].items()
                         if not isinstance(i, int) or isinstance(i, bool))
            if odd:
                import_notes.warn(source, f"the saved Link of the {', '.join(odd)} panes is "
                                          f"not a Link number; they show the active Link")
            view["pane_links"] = {g: where[i] for g, i in view["pane_links"].items()
                                  if isinstance(i, int) and i in where}
        self._apply_link_view_prefs(view, active)
        if ws.mode in MODES:
            self._mode_mgr.switch_to(ws.mode)
        else:
            import_notes.warn(source, f"the saved mode {ws.mode!r} is not one of "
                                      f"{', '.join(MODES)}; the mode is left as it is")
        self._apply_saved_views(ws, where)
        self._apply_window_view_state(view)

    def _ws_note(self, message: str) -> None:
        """An import warning for the workspace being applied (none outside apply_workspace)."""
        source = getattr(self, "_ws_source", None)
        if source is not None:
            import_notes.warn(source, message)

    def _locate_missing_captures(self, ws: Workspace, ws_path: str) -> Optional[set]:
        """Ask about each capture file a workspace names that is neither where the workspace
        says (relative to it) nor where it was when saved: Locate it, Skip that Link (it is
        not loaded and the rest open), or Cancel the open. Returns the indices of the Links
        skipped, or None on Cancel: nothing opens."""
        skipped: set = set()
        while True:
            missing = [m for m in ws.missing_files() if m[0] not in skipped]
            if not missing:
                return skipped
            i, key, saved = missing[0]
            answer = self._ask_missing_capture(ws.links[i].name, saved, len(missing) - 1)
            if answer == "cancel":
                return None
            if answer == "skip":
                skipped.add(i)
                continue
            name = os.path.basename(saved)
            ext = os.path.splitext(name)[1]
            found, _ = QFileDialog.getOpenFileName(
                self, f"Locate {name}", os.path.dirname(os.path.abspath(ws_path)),
                f"{name} (*{ext});;All files (*)" if ext else "All files (*)")
            if found:
                ws.locate(i, key, found)

    def _ask_missing_capture(self, link_name: str, saved: str, others: int) -> str:
        """"locate", "skip" or "cancel" for one capture file a workspace cannot find."""
        box = QMessageBox(QMessageBox.Warning, "Capture not found",
                          f"{link_name}'s capture file was not found:\n{saved}", parent=self)
        more = f" {others} other file(s) are also missing." if others else ""
        box.setInformativeText("Locate it, or skip this Link and open the rest." + more)
        locate = box.addButton("Locate…", QMessageBox.AcceptRole)
        skip = box.addButton("Skip This Link", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(locate)
        box.exec()
        clicked = box.clickedButton()
        return "locate" if clicked is locate else "skip" if clicked is skip else "cancel"

    # ---- the whole view, for a workspace ----
    def _keep_shown_views(self) -> None:
        """Leave what the panes on screen show with the Links they show it for."""
        for _k, raw, audio in self._bottom_lanes():
            self._keep_lane_view(raw, audio)
        if not self._cmd_all:
            self._save_commands_view(self._pane_link["commands"])

    def _window_view_state(self) -> dict:
        """What a workspace records of the window, beyond each Link's own view."""
        from base64 import b64encode
        a = self._links.active_index
        timeline = None
        if 0 <= a < len(self._ribbons) and a < len(self._links):
            r = self._ribbons[a]
            timeline = [self._links[a].to_ps(round(r._view_lo)),
                        self._links[a].to_ps(round(r._view_hi))]
        window: Dict[str, Any] = {"geometry": b64encode(bytes(self.saveGeometry())).decode("ascii"),
                  "docks": b64encode(bytes(self._max_pane["state"] if self._max_pane
                                           else self.saveState())).decode("ascii")}
        settle = self._max_pane["settle"] if self._max_pane else None
        if settle is not None:
            # All Links turned off while a pane is maximized: the arrangement put back is
            # resized after it is restored, so the workspace carries that resize too.
            window["bottom_settle"] = [settle[0].objectName(), int(settle[1])]
        out = {
            "window": window,
            "timeline_ps": timeline,
            "commands_header": view_state.commands_header_state(self._cmd_view),
            "samples": view_state.samples_state(self._sample_view),
            "registers": view_state.registers_state(self._reg_view),
            "statistics": view_state.statistics_state(self._meas_view),
            "timing_pane": view_state.timing_state(self._eye_view),
        }
        if self._cmd_all:
            out["all_links_commands"] = {
                "filters": view_state.filters_to_json(self._filter_snapshot()),
                "col_widths": self._command_column_widths()}
        return out

    def _apply_saved_views(self, ws: Workspace, where: dict) -> None:
        """Each Link's own view from the workspace: kept with the Link, and shown on the
        panes that show it now."""
        for spec_i, link_i in where.items():
            spec, st = ws.links[spec_i], self._links[link_i].view
            v = spec.view if isinstance(spec.view, dict) else {}
            raw_cmd = v.get("commands")
            cmd: dict = raw_cmd if isinstance(raw_cmd, dict) else {}
            st.filters = view_state.filters_from_json(cmd.get("filters")) or st.filters
            widths = _widths_by_name(cmd.get("col_widths"), COLUMN_NAMES)
            if widths:
                st.col_widths = widths
            st.audio_view = v.get("audio") if isinstance(v.get("audio"), dict) else st.audio_view
            st.capture_view = (v.get("capture") if isinstance(v.get("capture"), dict)
                               else st.capture_view)
        for k, raw, audio in self._bottom_lanes():
            st = self._links[k].view
            src = getattr(self, "_ws_source", None)
            view_state.restore_audio(audio, st.audio_view, zoom=not self._bottom_all, source=src)
            view_state.restore_capture(raw, st.capture_view, zoom=not self._bottom_all,
                                       source=src)
            audio._shown_state = raw._shown_state = st
        if not self._cmd_all:
            st = self._links[self._pane_link["commands"]].view
            if st.filters is not None:
                self._restore_filters(st.filters)
            self._apply_column_widths(st.col_widths)

    def _all_kinds(self) -> list:
        """The command kinds the All Links table can show: every Link's."""
        return sorted(set().union(*(set(link.view.kinds) for link in self._links)))

    def _apply_column_widths(self, widths) -> bool:
        """Set the Commands table's column widths from `widths`: a {column name: width} as
        `_command_column_widths` returns, or a positional list as long as the table has
        columns. Columns it does not name keep their width. False when it sets none, so the
        caller fits the columns instead."""
        model = self._cmd_view.model()
        if model is None:
            return False
        names = [CommandTableView._column_name(model, c) for c in range(model.columnCount())]
        by_name = _widths_by_name(widths, names)
        if not by_name:
            return False
        for c, name in enumerate(names):
            if name in by_name:
                self._cmd_view.setColumnWidth(c, by_name[name])
        return True

    def _apply_window_view_state(self, view: dict) -> None:
        """The window-wide part of a saved view, after every Link is bound and the mode
        is set: the panes' own state, the timeline's zoom, and last the window and dock
        layout, applied again once the deferred layout passes have run."""
        allc = view.get("all_links_commands")
        if self._cmd_all and isinstance(allc, dict):
            snap = view_state.filters_from_json(allc.get("filters"))
            if snap is not None:
                self._restore_filters(snap, kinds=self._all_kinds())     # every Link's
            self._apply_column_widths(allc.get("col_widths"))
        src = getattr(self, "_ws_source", None)
        view_state.restore_commands_header(self._cmd_view, view.get("commands_header"),
                                           source=src)
        if "show_clock" in view:                   # lanes made after _apply_view_prefs ran
            for v in self._raw_lanes.views:
                v.set_show_clock(bool(view.get("show_clock", True)))
        view_state.restore_samples(self._sample_view, view.get("samples"), source=src)
        view_state.restore_registers(self._reg_view, view.get("registers"), source=src)
        view_state.restore_statistics(self._meas_view, view.get("statistics"), source=src)
        view_state.restore_timing(self._eye_view, view.get("timing_pane"), source=src)
        self._apply_timeline_zoom(view.get("timeline_ps"))
        win = view.get("window")
        if isinstance(win, dict):
            self._analysis_laid_out = True         # not the one-time default over it
            self._restore_window(win)

            def again() -> None:
                self._restore_window(win)
            timers.after(0, self, again)

    def _apply_timeline_zoom(self, rng) -> None:
        a = self._links.active_index
        try:
            lo_ps, hi_ps = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            return
        if hi_ps <= lo_ps or not 0 <= a < min(len(self._ribbons), len(self._links)):
            return
        link = self._links[a]
        self._ribbons[a]._set_view(link.to_sample(lo_ps), link.to_sample(hi_ps), emit=True)

    def _restore_window(self, win: dict) -> None:
        from base64 import b64decode
        from binascii import Error as B64Error

        from PySide6.QtCore import QByteArray
        try:
            geometry = b64decode(str(win.get("geometry", "")), validate=True)
            docks = b64decode(str(win.get("docks", "")), validate=True)
        except (B64Error, ValueError):
            return
        if geometry:
            self.restoreGeometry(QByteArray(geometry))
        if docks:
            self.restore_panes()
            self.restoreState(QByteArray(docks))
            settle = win.get("bottom_settle")
            if isinstance(settle, list) and len(settle) == 2 \
                    and isinstance(settle[1], int) and settle[1] > 0:
                dock = next((d for d in self._dock_area if d.objectName() == settle[0]), None)
                if dock is not None:
                    self._settle_bottom_height(dock, settle[1])

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
                self._ws_note(f"the saved '{key}' {view.get(key)!r} is not a number; "
                              f"{default} is used")
                return default

        self._grid_rows_edit.setText(str(_int("grid_rows", DEFAULT_GRID_ROWS)))
        self._on_grid_rows_edit()               # clamps 1..10240
        for v in self._raw_lanes.views:
            v.set_show_clock(bool(view.get("show_clock", True)))
        tx_on = bool(view.get("tx_map", False))
        self._toggles_btn.setChecked(tx_on)
        # Persistence only matters while toggles are shown; force it off otherwise so a
        # disabled-but-checked button / stale _tx_persist can't be restored or re-saved.
        self._persist_btn.setChecked(tx_on and bool(view.get("tx_persist", False)))
        if tx_on:
            self._tx_scroll.setValue(_int("tx_start_row", 0))
        self._refresh_grid()

    def _load_async(self, factory, after=None, kind=None, session=None,
                    on_fail=None, _queued: bool = False) -> None:
        """Build a Session via `factory()` (the heavy decode + audio store) on a
        worker thread, showing an indeterminate busy dialog so the UI stays
        responsive. On success runs load_session + optional `after(session)` on the
        GUI thread; on failure shows the error.

        `kind` says what the load is for: "open" (the default) replaces the analysis,
        "add" appends a Link (the Open Capture dialog's Add), and "redecode" reloads the active
        Link's session in place. If a decode is already running the request is queued, and
        a queued request is superseded only by one that makes it pointless: any open
        supersedes everything, a re-decode supersedes earlier re-decodes of the SAME
        session (latest wins, so a fast SSP step or toggle isn't silently dropped), and an
        added Link is never dropped by anything but an open. A re-decode is of the active
        Link's session unless `session` names another Link's. `on_fail()` runs if the decode
        fails, so a caller sequencing several loads can carry on or stop."""
        kind = kind or "open"
        if kind == "open" and not _queued:
            # Counted when an open is ASKED FOR, once: a queued one comes back through here
            # to start (_run_pending_load), and counting it again would make its own
            # follow-on Links look superseded (_after_open).
            self._open_gen += 1
        key = (session if session is not None else self._session) if kind == "redecode" else None
        if getattr(self, "_load_thread", None) is not None:
            pending = list(getattr(self, "_load_pending", None) or [])
            if kind == "open":
                pending = []
            elif kind == "redecode":
                pending = [r for r in pending if not (r[2] == "redecode" and r[3] is key)]
            self._load_pending = pending + [(factory, after, kind, key, on_fail)]
            return
        self._load_after = after
        self._load_kind = kind
        self._load_key = key
        self._load_on_fail = on_fail
        dlg = QProgressDialog("Decoding capture…", None, 0, 0, self)
        dlg.setWindowTitle("SWI3S Studio")
        # NON-modal, deliberately: an ApplicationModal QProgressDialog spins a modal
        # event loop inside setValue(), during which the queued cross-thread `done`
        # signal is delivered and closes the dialog — ending the modal session from
        # within its own nested run loop ("modalSession has been exited prematurely —
        # reentrant call to endModalSession" on macOS). Non-modal defers `done` to the
        # normal event loop, so close() runs at the top level. Concurrent re-triggers are
        # coalesced (_load_pending).
        dlg.setWindowModality(Qt.NonModal)
        dlg.setCancelButton(None)            # the C++ decode can't be interrupted mid-run
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._load_dlg = dlg
        self._load_thread = QThread(self)
        self._load_worker = _LoadWorker(factory)
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
            # (self-animating) busy bar. The heavy work runs on the worker thread over
            # GIL-releasing numpy, so the GUI stays free to animate.
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
        # this method. Bail when the state is already gone.
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
        pending = list(getattr(self, "_load_pending", None) or [])
        if pending:
            factory, after, kind, key, on_fail = pending.pop(0)
            self._load_pending = pending
            self._load_async(factory, after, kind, session=key, on_fail=on_fail, _queued=True)

    def _on_load_done(self, session, store, extras) -> None:
        after = self._load_after
        on_fail = getattr(self, "_load_on_fail", None)
        # Stop the decode-progress poll BEFORE any dialog work, then keep the busy dialog
        # up through load_session — building the views (command table, timeline, grid,
        # statistics) is heavy GUI-thread work that freezes the event loop, and close()
        # only POSTS the hide. Relabel it "Building views…" and force a synchronous
        # repaint, then build, then close.
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
        kind = getattr(self, "_load_kind", "open")
        try:
            if kind == "redecode" and self._links.index_of(session) < 0:
                # Its Link was removed while it decoded. Loading it would make it a NEW
                # capture and replace every surviving Link with the removed one. Discarded
                # is not loaded, so it settles as failed.
                self._status.setText("Discarded a re-decode of a Link that was removed.")
                if on_fail is not None:
                    on_fail()
            else:
                try:
                    self.load_session(session, store, extras=extras, add_link=kind == "add")
                except Exception:
                    # The request must still settle, or a workspace waiting on it never
                    # applies its saved state to the Links that did load. If the session
                    # became a Link anyway, it counts as loaded; otherwise as failed.
                    if self._links.index_of(session) >= 0:
                        if after is not None:
                            after(session)
                    elif on_fail is not None:
                        on_fail()
                    raise
                if after is not None:
                    after(session)
                self.show_import_warnings((extras or {}).get("import_warnings") or ())
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.critical(self, "Load failed",
                                 f"The capture decoded but the views failed to load: {exc}")
        finally:
            self._finish_load()
        self._run_pending_load()

    def _on_load_failed(self, message: str) -> None:
        on_fail = getattr(self, "_load_on_fail", None)
        self._finish_load()
        QMessageBox.critical(self, "Open failed", message)
        if on_fail is not None:
            on_fail()
        self._run_pending_load()

    # ---- import warnings (import_notes) ----
    def show_import_warnings(self, notes, what: str = ""):
        """Show `notes` (import_notes.ImportWarning) in a non-modal Import warnings window
        and add their count to the status bar. Returns the window, or None when `notes`
        is empty. `what` titles a window whose warnings name no file."""
        notes = list(notes or ())
        if not notes:
            return None
        from .import_warnings_dialog import ImportWarningsDialog, sources_of
        dlg = ImportWarningsDialog(notes, what, self)
        dlg.show()
        n = len(notes)
        names = ", ".join(sources_of(notes)) or what
        line = (f"{n} import warning{'s' if n != 1 else ''}"
                f"{f' in {names}' if names else ''}: see the Import warnings window.")
        prev = self._status.text().strip()
        self._status.setText(f"{prev}  {line}" if prev else line)
        return dlg

    def import_warning_dialogs(self) -> list:
        """The Import warnings windows now open, oldest first."""
        from .import_warnings_dialog import ImportWarningsDialog
        return [d for d in self.findChildren(ImportWarningsDialog) if isValid(d)]

    @contextlib.contextmanager
    def reporting_import(self, what: str = ""):
        """Collect the import warnings raised in the block (on this thread) and show them
        when it finishes without an exception. Yields the list."""
        with import_notes.collect() as notes:
            yield notes
        self.show_import_warnings(notes, what)

    @property
    def links(self) -> LinkSet:
        return self._links

    def _bottom_link_state(self, link: Optional[int] = None) -> Optional[LinkPanelState]:
        i = self._pane_link.get("bottom", -1) if link is None else link
        return self._links[i].view if 0 <= i < len(self._links) else None

    def _push_stream_colors(self) -> None:
        """Each Link's per-stream colours to the panes showing it: Audio and Capture (its
        bit overlay), lane by lane in All Links, and Samples (its Port column)."""
        for k, raw, audio in self._bottom_lanes():
            st = self._bottom_link_state(k)
            for view in (audio, raw, self._table_lane(self._sample_lanes, k)):
                view.set_stream_colors(st.stream_colors if st is not None else {})

    def _on_stream_color_chosen(self, device: int, dp: int, color: str,
                                link: Optional[int] = None) -> None:
        """A stream's colour was picked (or reset, `color` "") in the Audio pane: keep
        it as the override of the Link that lane shows, for the current theme, and
        recolour."""
        st = self._bottom_link_state(link)
        if st is None:
            return
        key = (int(device), int(dp), VizTheme.MODE)
        if color:
            st.stream_colors[key] = color
        else:
            st.stream_colors.pop(key, None)
        self._push_stream_colors()

    # ---- per-stream Filter & Gain (DC blocker, high-pass, gain) ----
    @staticmethod
    def _apply_stream_processing(st) -> None:
        """Put a Link's Filter & Gain settings onto its audio store (a stream the
        store does not carry keeps its setting for a later decode that has it)."""
        store = getattr(st, "audio_store", None)
        if store is None:
            return
        have = set(store.streams())
        for (dev, dp), proc in (st.stream_processing or {}).items():
            if (dev, dp) in have:
                store.set_processing(dev, dp, proc)
        for dev, dp in getattr(st, "even_timing", None) or ():
            if (dev, dp) in have:
                store.set_evenly_spaced(dev, dp, True)

    def _refresh_processed_views(self, link: int) -> None:
        for k, _raw, audio in self._bottom_lanes():
            if k == link:
                audio.refresh_processing()

    def _preview_stream_processing(self, dev: int, dp: int, proc, link: int) -> None:
        st = self._bottom_link_state(link)
        if st is None or st.audio_store is None:
            return
        self._stop_audio()                      # the samples a playback reads are changing
        st.audio_store.set_processing(dev, dp, proc)
        self._refresh_processed_views(link)

    def set_stream_even_timing(self, device: int, dp: int, on: bool,
                               link: Optional[int] = None) -> None:
        """Audio ▸ right-click a channel ▸ Evenly Spaced / As Transported: draw the (device,
        dp) stream of Link `link` (default: the bottom group's) evenly spaced, or at the
        times the bus carried each sample. Kept per Link, re-applied after a re-decode."""
        i = self._pane_link.get("bottom", -1) if link is None else int(link)
        st = self._bottom_link_state(i)
        if st is None or st.audio_store is None:
            return
        key = (int(device), int(dp))
        if not st.audio_store.set_evenly_spaced(key[0], key[1], on):
            return
        (st.even_timing.add if on else st.even_timing.discard)(key)
        self._refresh_processed_views(i)
        names = self._links[i].session.dp_names if 0 <= i < len(self._links) else {}
        self._status.setText(f"{port_label(names, key[0], key[1])}: "
                             + ("drawn evenly spaced" if on else "drawn as transported"))

    def set_stream_processing(self, device: int, dp: int, proc,
                              link: Optional[int] = None) -> None:
        """Keep `proc` (a StreamProcessing, or None to revert) as the Filter & Gain of
        the (device, dp) stream on Link `link` (default: the bottom group's), apply it to
        that Link's audio, and redraw the panes showing it."""
        i = self._pane_link.get("bottom", -1) if link is None else int(link)
        st = self._bottom_link_state(i)
        if st is None:
            return
        key = (int(device), int(dp))
        if proc is None or proc.is_identity():
            st.stream_processing.pop(key, None)
            proc = None
        else:
            st.stream_processing[key] = proc
        self._preview_stream_processing(key[0], key[1], proc, i)
        names = self._links[i].session.dp_names if 0 <= i < len(self._links) else {}
        name = port_label(names, key[0], key[1])
        self._status.setText(f"{name}: {proc.describe()}" if proc
                             else f"{name}: Filter & Gain reverted")

    def edit_stream_processing(self, device: int, dp: int, link: Optional[int] = None) -> None:
        """Audio ▸ a stream's right-click ▸ Filter & Gain…"""
        from .audio_process_dialog import StreamProcessingDialog
        i = self._pane_link.get("bottom", -1) if link is None else int(link)
        st = self._bottom_link_state(i)
        if st is None or st.audio_store is None:
            return
        names = self._links[i].session.dp_names if 0 <= i < len(self._links) else {}
        dlg = StreamProcessingDialog(
            st.audio_store, device, dp, parent=self, names=names,
            preview=lambda proc: self._preview_stream_processing(device, dp, proc, i))
        if dlg.exec() == QDialog.Accepted:
            self.set_stream_processing(device, dp, dlg.chosen(), link=i)

    # ---- data-port names ----
    def rename_data_port(self, device: int, dp: int, link: Optional[int] = None) -> None:
        """Audio ▸ right-click a channel ▸ Rename…: name a data port of the Link that lane
        shows (an empty name goes back to "Dev1 DP2"). The name is the Link's and is saved
        with it; the Audio, Capture and Samples panes and the Bus Grid's colour key show it."""
        i = self._links.active_index if link is None else int(link)
        if not 0 <= i < len(self._links):
            return
        sess = self._links[i].session
        current = sess.dp_names.get((int(device), int(dp)), "")
        name, ok = QInputDialog.getText(
            self, "Rename Data Port",
            f"Name for Device {device} · DP{dp} (empty for none):", text=current)
        if not ok or name.strip() == current:
            return
        sess.set_dp_name(device, dp, name)
        self._push_stream_names()
        self._refresh_grid()           # the grid group's Link, which need not be the active one

    def _push_stream_names(self) -> None:
        """Each Link's data-port names to the panes showing it, lane by lane (as
        _push_stream_colors), and to the Timing pane's driver filter."""
        for k, raw, audio in self._bottom_lanes():
            names = self._links[k].session.dp_names if 0 <= k < len(self._links) else {}
            for view in (audio, raw, self._table_lane(self._sample_lanes, k)):
                view.set_stream_names(names)
            with self._as_lane(k):
                self._eye_view.set_port_names(names)

    def _lane_session_names(self, view) -> dict:
        """The data-port names of the Link an Audio view shows."""
        k = self._lane_link(view)
        return self._links[k].session.dp_names if 0 <= k < len(self._links) else {}

    # ---- All Links in the bottom group: Capture and Audio, one lane per Link ----
    def _wire_capture(self, view):
        """Connect a Capture view (the pane's own, or a lane's) to the Link it shows."""
        view.sampleSelected.connect(lambda s, v=view: self._on_lane_seek(v, s))
        view.bookmarkMoved.connect(lambda lb, s, final, v=view: self._on_bookmark_moved(
            lb, s, final, link=self._lane_link(v)))
        return view

    def _wire_audio(self, view):
        """Connect an Audio view (the pane's own, or a lane's) to the Link it shows: its
        clicks and sweeping playhead seek, its drags move that Link's bookmarks, and its
        stream colours are that Link's."""
        view.seeked.connect(lambda s, v=view: self._on_lane_seek(v, s))
        view.playCursorMoved.connect(lambda s, v=view: self._on_lane_seek(v, s))
        view.stopped.connect(self._on_playback_stopped)    # full refresh on stop
        view.playStarted.connect(lambda v=view: self._on_lane_play(v))
        view.bookmarkMoved.connect(lambda lb, s, final, v=view: self._on_bookmark_moved(
            lb, s, final, link=self._lane_link(v)))
        view.streamColorChosen.connect(lambda d, p, c, v=view: self._on_stream_color_chosen(
            d, p, c, link=self._lane_link(v)))
        view.streamProcessingRequested.connect(
            lambda d, p, v=view: self.edit_stream_processing(d, p, link=self._lane_link(v)))
        view.streamRenameRequested.connect(
            lambda d, p, v=view: self.rename_data_port(d, p, link=self._lane_link(v)))
        view.streamTimingRequested.connect(
            lambda d, p, on, v=view: self.set_stream_even_timing(d, p, on, link=self._lane_link(v)))
        return view

    def _lane_link(self, view) -> int:
        """The Link a Capture or Audio view shows: its lane's in All Links, else the
        bottom group's."""
        if self._bottom_all:
            for lanes in (self._raw_lanes, self._audio_lanes):
                if view in lanes.views:
                    return lanes.views.index(view)
        return self._pane_link["bottom"]

    def _bottom_lanes(self):
        """(Link, Capture view, Audio view) for each lane shown: one per Link in All
        Links, else the bottom group's Link with the panes' own views."""
        if self._bottom_all:
            n = len(self._links)
            return list(zip(range(n), self._raw_lanes.views[:n], self._audio_lanes.views[:n]))
        return [(self._pane_link["bottom"], self._raw_view, self._audio_view)]

    def _audio_lane(self):
        """The Audio view a bottom-group menu acts on: in All Links the active Link's lane
        (the one last worked in), else the pane's own."""
        a = self._links.active_index
        if self._bottom_all and 0 <= a < len(self._audio_lanes.views):
            return self._audio_lanes.views[a]
        return self._audio_view

    def _capture_lane(self):
        """The Capture view for the active Link: its lane in All Links, else the pane's."""
        a = self._links.active_index
        if self._bottom_all and 0 <= a < len(self._raw_lanes.views):
            return self._raw_lanes.views[a]
        return self._raw_view

    def _wire_symbols(self, view):
        """Connect a CDS table (the pane's own, or a lane's) to the Link it shows."""
        view.sampleSelected.connect(
            lambda s, v=view: self._on_table_lane(v, self._on_symbol_selected, s))
        view.moreAboveRequested.connect(
            lambda s, v=view: self._on_table_lane(v, self._extend_symbols_above, s))
        return view

    def _wire_samples(self, view):
        """Connect a Samples table (the pane's own, or a lane's) to the Link it shows."""
        view.sampleSelected.connect(
            lambda s, v=view: self._on_table_lane(v, self._on_sample_selected, s))
        view.filtersChanged.connect(
            lambda v=view: self._on_table_lane(v, self._on_sample_filters_changed))
        view.edgeReached.connect(       # lazy load on scroll-to-edge
            lambda d, v=view: self._on_table_lane(v, self._on_sample_edge, d))
        return view

    def _wire_timing(self, view):
        """Connect a Timing pane (the pane's own, or a lane's): a worst-UI jump visits
        that lane's Link."""
        view.sampleSelected.connect(
            lambda s, v=view: self._on_table_lane(v, self._on_timing_jump, s))
        return view

    def _table_lane(self, lanes, k: int):
        """Lane k's pane in All Links, else the pane's own."""
        return lanes.views[k] if self._bottom_all and k < len(lanes.views) else lanes.views[0]

    @contextlib.contextmanager
    def _as_lane(self, k: int):
        """Run CDS / Samples / Timing code for lane k: Link k active (`_as_link`) and
        `self._symbol_view` / `self._sample_view` / `self._eye_view` pointing at its
        panes, so the single-Link handlers run as they are, lane by lane."""
        prev = self._symbol_view, self._sample_view, self._eye_view
        self._symbol_view = self._table_lane(self._symbol_lanes, k)
        self._sample_view = self._table_lane(self._sample_lanes, k)
        self._eye_view = self._table_lane(self._eye_lanes, k)
        try:
            with self._as_link(k):
                yield
        finally:
            self._symbol_view, self._sample_view, self._eye_view = prev

    def _on_table_lane(self, view, handler, *args) -> None:
        """A CDS or Samples table asked for something (a selection, the history above,
        an edge, a filter). In All Links its lane's Link becomes the active one, as a
        touch would make it, and the handler runs on that lane's tables."""
        if not self._bottom_all:
            handler(*args)
            return
        k = max(lanes.lane_of(view) for lanes in (
            self._symbol_lanes, self._sample_lanes, self._eye_lanes))
        if k < 0:
            handler(*args)
            return
        self._activate_link(k)
        self._origin_lane = k
        try:
            with self._as_lane(k):
                handler(*args)
        finally:
            self._origin_lane = -1

    def _stop_audio(self) -> None:
        for v in self._audio_lanes.views:
            v.stop()

    def _audio_playing(self) -> bool:
        return any(v.is_playing for v in self._audio_lanes.views)

    def _on_lane_play(self, view) -> None:
        """One output device: a lane starting playback stops any other."""
        for v in self._audio_lanes.views:
            if v is not view and v.is_playing:
                v.stop()

    def _on_lane_seek(self, view, sample: int) -> None:
        """A seek from Capture or Audio, counted in the Link that view shows."""
        if len(self._links) > 1:
            sample = self._clamp_to_active(self._links.convert(
                int(sample), self._lane_link(view), self._links.active_index))
        self._on_seek(sample)

    def _in_link(self, link: int, sample: int) -> int:
        """An active-Link sample counted in Link `link`."""
        if len(self._links) < 2:
            return int(sample)
        return self._links.convert(int(sample), self._links.active_index, link)

    def _sync_lanes(self) -> None:
        """The lanes from the LinkSet: one per Link, named and offset into global time, in
        All Links; else just the panes' own views."""
        span = None
        if self._bottom_all:
            names = [link.name for link in self._links]
            offs = [int(link.offset_ps) / 1e12 for link in self._links]
            ends = [o + max(1, int(link.session.last_sample())) / float(
                link.session.sample_rate_hz or 1) for o, link in zip(offs, self._links)]
            span = (min(offs), max(ends))           # global seconds every Link covers
        else:
            names, offs = [""], [0.0]
        for lanes in (self._raw_lanes, self._audio_lanes, self._symbol_lanes,
                      self._sample_lanes, self._eye_lanes):
            lanes.set_lanes(names, offs, span)

    def _bind_lane_of(self, k: int) -> None:
        """Bind lane k's Capture and Audio to Link k (All Links)."""
        self._sync_lanes()
        if 0 <= k < len(self._links):
            with self._as_lane(k):
                self._bind_lane(self._raw_lanes.views[k], self._audio_lanes.views[k])
                self._bind_tables()
                self._bind_timing()
            self._update_capture_actions()      # out of the lane: the Link it would export

    def _align_lanes(self) -> None:
        """Every lane on the range of the bottom group's Link's lane."""
        home = self._pane_link["bottom"]
        for lanes in (self._raw_lanes, self._audio_lanes):
            lanes.align_to(home if home < lanes.count() else 0)

    def _raised_bottom_dock(self):
        """The bottom-area dock on screen: the raised tab. Every tab of a tabbed group
        reports isVisible(), and those behind keep stale heights (and ignore a resize), so
        it is the one with something actually showing."""
        docks = [d for d in (self._grid_dock, *self._group_docks["bottom"])
                 if not d.isHidden() and not d.isFloating()]
        return next((d for d in docks if not d.visibleRegion().isEmpty()), None)

    def _settle_bottom_height(self, dock, height: int, passes: int = 3) -> None:
        """Size the bottom docks back to `height` once the layout has settled. A resize
        made at once is clamped to the lanes' minimum, which lingers until their layouts
        run, and the rebind that follows leaving All Links queues more layout passes that
        give the room back; so it is applied on the next pass of the event loop and again
        while it has not held, up to `passes` times."""
        def apply(n=passes):
            if self._max_pane is not None:        # made to the arrangement put back
                self._max_pane["settle"] = (dock, height, n)
                return
            if dock.isHidden() or dock.isFloating():
                return
            if dock.height() > height + 2:
                self.resizeDocks([dock], [height], Qt.Vertical)
                if n > 1:
                    timers.after(0, self, lambda: apply(n - 1))
        timers.after(0, self, apply)

    def set_bottom_scope_all(self, on: bool, rebind: bool = True) -> None:
        """Capture and Audio show every Link, one lane each on one global time axis (All
        Links), or only the Link the bottom group's picker names. The other groups keep
        the Links they show. `rebind=False` leaves the panes' binding to the caller: an
        open that replaces the Links, or a removal down to one, binds them itself."""
        on = bool(on) and len(self._links) > 1
        if on == self._bottom_all:
            self._update_link_switcher()
            return
        self._stop_audio()
        dock = self._raised_bottom_dock()
        height = dock.height() if dock is not None else 0
        if self._max_pane is not None:            # measured in the arrangement put back
            dock, height = self._max_pane["bottom"]
        if on:
            # Two or more lanes need more height, so Qt grows the docks; it never shrinks
            # them again by itself, so remember the one-Link height to go back to (none,
            # if no docked pane is on screen to measure: an old one would be wrong).
            self._bottom_single_height = height
        self._bottom_all = on
        self._sync_lanes()
        if not on and not rebind:
            # The caller binds the panes, cursor and pickers next; the cursor work is left
            # to it, since the panes are not bound yet.
            if dock is not None and self._bottom_single_height:
                self._settle_bottom_height(dock, self._bottom_single_height)
            return
        if on:
            for k in range(len(self._links)):
                with self._as_lane(k):
                    self._bind_lane(self._raw_lanes.views[k], self._audio_lanes.views[k])
                    self._bind_tables()
                    self._bind_timing()
            self._update_capture_actions()      # out of the lanes: the Link it would export
        elif rebind:
            with self._as_link(self._pane_link["bottom"]):
                self._bind_lane(self._raw_view, self._audio_view)
                self._bind_tables()
                self._bind_timing()
        self._push_bookmarks()
        self._update_link_switcher()
        if not on and dock is not None and self._bottom_single_height:
            self._settle_bottom_height(dock, self._bottom_single_height)
        if self._session is not None:
            self._restore_cursor(self.cursor.sample)
            if on:
                self._align_lanes()

    def _link_state(self) -> LinkPanelState:
        active = self._links.active
        return active.view if active is not None else self._idle_link

    @property
    def _session(self) -> Optional[Session]:
        """The active Link's Session, or None when nothing is loaded."""
        active = self._links.active
        return active.session if active is not None else None

    @_session.setter
    def _session(self, session: Optional[Session]) -> None:
        # Assigning the SAME session (a re-decode reload) keeps its Link and that Link's
        # state; a different one replaces the analysis, as opening a capture always has.
        if session is None:
            self._links.clear()
        elif self._session is not session:
            self._links.replace(session, view=LinkPanelState())
        self._close_stale_history()

    def add_link(self) -> None:
        """Open Capture with the dialog on Add (with nothing open, there is
        nothing to add to, so it is a plain open)."""
        self.open_capture(caption="Add a Link: open its capture",
                          prefer_add=self._session is not None)

    _OFFSET_UNITS = (("µs", 10 ** 6), ("ns", 10 ** 3), ("ms", 10 ** 9), ("s", 10 ** 12),
                     ("ps", 1))

    def set_link_offset(self, index: int, offset_ps: int) -> None:
        """Place Link `index`'s sample 0 at `offset_ps` in global time. Only the mapping
        moves (no re-decode): the timeline window, the All Links times, every bookmark
        (each stays on its own Link's samples, so it moves with its Link) and each pane
        group's instant are brought up to date."""
        self._links[index].offset_ps = int(offset_ps)
        # The remembered instant is the old offset's: reused, the next Link activation
        # would put the cursor back where it was before the move.
        self._cursor_instant = None
        self._update_timeline_domain()
        if self._bottom_all:
            self._sync_lanes()                  # the lanes' axes and sync read the offsets
            self._align_lanes()                 # each lane kept its own range: line them up
        if self._cmd_all:
            self._refresh_all_model()
        self._push_bookmarks()
        if self._session is not None:
            self._restore_cursor(self.cursor.sample)
        self._status.setText(f"{self._links[index].name} time offset: "
                             f"{_fmt_duration(int(offset_ps) / 1e12)}")

    def set_link_offset_dialog(self) -> None:
        """Set Time Offset… (a Link's timeline label): the active Link's offset, in a unit of
        the user's choice."""
        if len(self._links) < 2:
            return
        idx = self._links.active_index
        link = self._links[idx]
        dlg = QDialog(self)
        dlg.setWindowTitle("Set Link Time Offset")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Where {link.name}'s capture starts, in the time all Links "
                             f"share (positive = later):"))
        row = QHBoxLayout()
        value = QDoubleSpinBox()
        value.setDecimals(6)                             # µs, the first unit: 1 ps
        value.setRange(-_MAX_OFFSET_PS / 1e6, _MAX_OFFSET_PS / 1e6)
        unit = QComboBox()
        for name, _factor in self._OFFSET_UNITS:
            unit.addItem(name)
        value.setValue(link.offset_ps / self._OFFSET_UNITS[0][1])
        # Changing the unit re-expresses the SAME offset in it (5 µs -> 5000 ns), so the
        # unit is a way of reading the number, not a silent 1000x change to it.
        shown = {"unit": 0}

        def on_unit(i: int) -> None:
            ps = value.value() * self._OFFSET_UNITS[shown["unit"]][1]
            shown["unit"] = i
            # Exactly enough decimals for 1 ps in this unit (10**k ps per unit -> k).
            value.setDecimals(len(str(self._OFFSET_UNITS[i][1])) - 1)
            span = _MAX_OFFSET_PS / self._OFFSET_UNITS[i][1]   # the same ±1000 s in any unit
            value.setRange(-span, span)
            value.setValue(ps / self._OFFSET_UNITS[i][1])
        unit.currentIndexChanged.connect(on_unit)
        row.addWidget(value, 1)
        row.addWidget(unit)
        lay.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if not dlg.exec():
            return
        factor = self._OFFSET_UNITS[unit.currentIndex()][1]
        self.set_link_offset(idx, round(value.value() * factor))

    def _cross_link_pairs(self) -> list:
        """(group, member 1, member 2) for each complete bookmark pair on two Links."""
        groups: Dict[str, dict] = {}
        for bm in self._bookmarks:
            groups.setdefault(bm.group, {})[bm.index] = bm
        return [(g, m[1], m[2]) for g, m in sorted(groups.items())
                if 1 in m and 2 in m and m[1].link != m[2].link
                and max(m[1].link, m[2].link) < len(self._links)]

    def align_on_bookmark_pair(self, group: Optional[str] = None) -> None:
        """Cursors ▸ Align Links on Cursor Pair…: a pair whose members mark the SAME
        physical event on two Links. The second member's Link is moved so it lands on the first.
        Both bookmarks stay where they are in their own Link's samples, so afterwards
        they coincide, and every other bookmark on the moved Link moves with it."""
        pairs = self._cross_link_pairs()
        if not pairs:
            QMessageBox.information(
                self, "Align on Cursor Pair",
                "Place a cursor pair across two Links first: one member on each, at the "
                "same physical event (Ctrl+B on one Link, then on the other).")
            return
        if group is None:
            items = [f"{g}: {a.display()} on {self._links[a.link].name} → "
                     f"{b.display()} on {self._links[b.link].name}  (moves "
                     f"{self._links[b.link].name} by "
                     f"{_fmt_duration((self._bm_ps(a) - self._bm_ps(b)) / 1e12)})"
                     for g, a, b in pairs]
            choice, ok = QInputDialog.getItem(self, "Align on Cursor Pair",
                                              "Pair to align:", items, 0, False)
            if not ok:
                return
            group = pairs[items.index(choice)][0]
        g, a, b = next(p for p in pairs if p[0] == group)
        moved = self._links[b.link]
        self.set_link_offset(b.link, moved.offset_ps + self._bm_ps(a) - self._bm_ps(b))
        self._status.setText(f"Aligned {moved.name} on pair {g}: time offset "
                             f"{_fmt_duration(moved.offset_ps / 1e12)}")

    def _link_stem(self) -> str:
        """A filename suffix naming the active Link ("_Amp_bus"), so exports of two Links
        into one folder do not overwrite each other. Empty with one Link."""
        if len(self._links) < 2 or self._links.active is None:
            return ""
        slug = re.sub(r"[^A-Za-z0-9]+", "_", self._links.active.name).strip("_")
        return f"_{slug}" if slug else f"_link{self._links.active_index + 1}"

    def remove_link(self, index: Optional[int] = None, confirm: bool = True) -> None:
        """Remove Link… (a Link's timeline label): drop a Link (default the active one) and
        the bookmarks placed on it, after a confirmation that says how many. The last Link is not
        removed — opening a capture replaces it. Everything that refers to Links by index
        (bookmarks, each pane group's Link, the timeline bands) is renumbered, and the
        cursor keeps its instant on whichever Link becomes active."""
        if len(self._links) < 2:
            return
        idx = self._links.active_index if index is None else int(index)
        if not 0 <= idx < len(self._links):
            return
        link = self._links[idx]
        on_it = sum(1 for b in self._bookmarks if b.link == idx)
        if confirm:
            gone = f"\n\nIts {on_it} cursor(s) are removed with it." if on_it else ""
            if QMessageBox.question(self, "Remove Link", f"Remove {link.name}?{gone}",
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) != QMessageBox.Yes:
                return
        self._stop_audio()
        if self._cmd_all and len(self._links) == 2:
            self.set_commands_scope_all(False)  # one Link left: nothing to merge
        if not self._cmd_all:
            self._save_commands_view(self._pane_link["commands"])   # survives renumbering
        t_ps = self._links[self._links.active_index].to_ps(self.cursor.sample)
        keep = []
        for b in self._bookmarks:
            if b.link == idx:
                continue
            if b.link > idx:
                b.link -= 1
            keep.append(b)
        self._bookmarks = BookmarkSet(keep)
        # A queued re-decode of the removed Link must not run: it would land as a new
        # capture. (One already running is discarded when it lands — see _on_load_done.)
        # Dropped is not loaded, so each settles as failed.
        dropped = [r for r in (self._load_pending or [])
                   if r[2] == "redecode" and r[3] is link.session]
        self._load_pending = [r for r in (self._load_pending or []) if r not in dropped]
        for r in dropped:
            if r[4] is not None:
                r[4]()
        self._links.remove(idx)
        self._close_stale_history()
        new_active = self._links.active_index
        for group in _PANE_GROUPS:
            i = self._pane_link[group]
            self._pane_link[group] = new_active if i == idx else (i - 1 if i > idx else i)
        if self._bottom_all and len(self._links) < 2:
            self.set_bottom_scope_all(False, rebind=False)   # bound once, below
        new = self._links[new_active]
        self._cursor_instant = None             # its Link index no longer means the same Link
        self.cursor.rebase(min(max(0, new.to_sample(t_ps)), max(0, int(new.session.last_sample()))))
        if self._cmd_all:
            self._refresh_all_model()          # its rows name Links by index: rebuild first
        # Each band is its Link's position, so every band from the removed one on changed.
        self._sync_timeline_rows()
        for i in range(len(self._links)):
            self._bind_shared(i, feed_timeline=True)
        for i in sorted(set(self._pane_link.values())):
            self._bind_link(i, filters=self._links[i].view.filters, feed_timeline=False)
        if self._bottom_all:                    # every lane after it now shows the next Link
            for i in range(len(self._links)):
                self._bind_lane_of(i)
        self._push_bookmarks()
        self._update_link_switcher()
        self._update_title()
        self._restore_cursor(self.cursor.sample)
        self._status.setText(f"Removed {link.name}")

    def rename_link(self) -> None:
        if len(self._links) == 0:
            return
        # The Link is fixed BEFORE the dialog: closing it returns focus to a pane, which
        # can make another Link active before the answer is applied.
        idx = self._links.active_index
        link = self._links[idx]
        name, ok = QInputDialog.getText(self, "Rename Link", "Name:", text=link.name)
        if not ok:
            return
        try:
            self._links.rename(idx, name)
        except ValueError as exc:
            QMessageBox.warning(self, "Rename Link", str(exc))
            return
        # Everywhere the name is shown: the pickers and bands, the title, the All Links
        # table (its Link column and text-filter index), and the bookmark labels.
        self._update_link_switcher()
        self._update_title()
        if self._cmd_all:
            self._refresh_all_model()
        self._push_bookmarks()

    def _update_link_switcher(self) -> None:
        """Rebuild the pickers and View ▸ Links' entries from the LinkSet. The pickers
        show only with two or more Links, as does View ▸ Links."""
        if self._cmd_all and len(self._links) < 2:
            self.set_commands_scope_all(False)
        if self._bottom_all and len(self._links) < 2:
            self.set_bottom_scope_all(False)
        elif self._bottom_all:
            self._sync_lanes()                  # names (a rename), and the lane count
        combo = self._link_combo
        combo.blockSignals(True)
        combo.clear()
        for link in self._links:
            combo.addItem(link.name)
        if len(self._links) > 1:
            combo.addItem(_ALL_LINKS)
        combo.setCurrentIndex(len(self._links) if self._cmd_all
                              else max(0, self._pane_link["commands"]))
        combo.blockSignals(False)
        for act, on in ((self._all_links_action, self._cmd_all),
                        (self._bottom_all_action, self._bottom_all)):
            act.blockSignals(True)
            act.setChecked(on)
            act.setEnabled(len(self._links) > 1)
            act.blockSignals(False)
        # Sized from the longest name, not left to AdjustToContents, which elides the names
        # under the stylesheet's padding and drop-down and macOS's checkmark column in the
        # popup.
        fm = combo.fontMetrics()
        widest = max([fm.horizontalAdvance(link.name) for link in self._links]
                     + [fm.horizontalAdvance(_ALL_LINKS)])
        combo.setMinimumWidth(widest + 48)          # text + padding + drop-down arrow
        combo.view().setMinimumWidth(widest + 64)   # + the popup's checkmark column
        combo.setVisible(len(self._links) > 1)
        _PaneTouchFilter.need(self, len(self._links) > 1)
        if len(self._links) > 1:
            self._ensure_pane_pickers()
        for group, pickers in self._pane_pickers.items():
            for picker in pickers:
                picker.blockSignals(True)
                picker.clear()
                for link in self._links:
                    picker.addItem(link.name)
                if group == "bottom" and len(self._links) > 1:
                    picker.addItem(_ALL_LINKS)          # one stacked pane per Link
                picker.setCurrentIndex(len(self._links) if group == "bottom" and self._bottom_all
                                       else max(0, self._pane_link[group]))
                picker.setMinimumWidth(widest + 48)
                picker.view().setMinimumWidth(widest + 64)
                picker.blockSignals(False)
                picker.setVisible(len(self._links) > 1)
        for act in self._link_actions:
            self._link_group.removeAction(act)
            self._links_menu.removeAction(act)
        self._link_actions = []
        for i, link in enumerate(self._links):
            act = self._links_menu.addAction(link.name)
            act.setCheckable(True)
            act.setChecked(i == self._links.active_index)
            if i < 9:
                act.setShortcut(QKeySequence(f"Ctrl+Alt+{i + 1}"))
            act.triggered.connect(lambda _on=False, i=i: self.switch_link(i))
            self._link_group.addAction(act)
            self._link_actions.append(act)
        self._align_links_action.setEnabled(len(self._links) > 1)
        self._sync_view_links()
        self._sync_timeline_rows()

    def _band_segments(self, session) -> list:
        """The timeline's config bands for one Link: its segments with what the ribbon
        labels them by (pinned width, region CSV, the user's name) and their section index."""
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
            if i in getattr(session, "csv_sections", {}):
                s2["csv"] = os.path.basename(session.csv_sections[i])
            s2["index"] = i
            if i in getattr(session, "region_names", {}):
                s2["name"] = session.region_names[i]
            band_segs.append(s2)
        return band_segs

    def _feed_timeline(self, ribbon, session) -> None:
        """Give one Link's ribbon that Link's events, config bands and bring-up. Everything
        here is that Link's own; the cursor, view and bookmarks are synced separately."""
        ribbon.set_sample_rate(session.sample_rate_hz)
        ribbon.set_row_origin(session.row_origin)
        ribbon.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        total = max([c.get("end_sample", 0) for c in session.commands]
                    + [session.audio_end_sample, 1])
        ribbon.set_events(session.commands, total)
        ribbon.set_segments(self._band_segments(session), total)
        ribbon.set_bringups(session.link_controls)
        ribbon.set_commit_points(session.commit_point_samples())   # dotted SSP markers

    @property
    def _timeline(self) -> TimelineRibbon:
        """The ACTIVE Link's ribbon: every existing timeline call drives the Link on screen."""
        i = self._links.active_index
        return self._ribbons[i] if 0 <= i < len(self._ribbons) else self._ribbons[0]

    def _add_ribbon(self) -> None:
        ribbon = TimelineRibbon()
        ribbon.seeked.connect(lambda s, r=ribbon: self._on_ribbon_seek(r, s))
        ribbon.bookmarkMoved.connect(
            lambda lbl, s, final, r=ribbon: self._on_ribbon_bookmark_moved(r, lbl, s, final))
        ribbon.viewChanged.connect(lambda lo, hi, r=ribbon: self._on_ribbon_view(r, lo, hi))
        ribbon.regionMenuRequested.connect(
            lambda sect, pos, r=ribbon: self._region_band_menu(r, sect, pos))
        label = _LinkBandLabel()
        label.clicked.connect(lambda r=ribbon: self._on_ribbon_label(r, rename=False))
        label.doubleClicked.connect(lambda r=ribbon: self._on_ribbon_label(r, rename=True))
        label.menuRequested.connect(lambda pos, r=ribbon: self._link_label_menu(r, pos))
        label.hide()                            # shown only in a stack of Links
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(label)
        lay.addWidget(ribbon, 1)
        self._timeline_col.addWidget(row, 1)
        self._ribbons.append(ribbon)
        self._ribbon_labels.append(label)
        self._ribbon_rows.append(row)

    def _sync_timeline_rows(self) -> None:
        """One ribbon row per Link. With two or more, each row is labelled with its Link
        (the active one marked), heights become flexible so the dock resizes vertically,
        and the dock grows by a band per Link whenever the COUNT changes."""
        n = max(1, len(self._links))
        # A Link added or removed ends a maximize first: the arrangement it saved has the
        # old number of bands, and a workspace saved after would record it.
        restored = n != self._timeline_rows_shown and self.restore_panes()
        while len(self._ribbons) < n:
            self._add_ribbon()
        while len(self._ribbons) > n:
            row = self._ribbon_rows.pop()
            self._ribbons.pop()
            self._ribbon_labels.pop()
            self._timeline_col.removeWidget(row)
            row.deleteLater()
        multi = n > 1
        if multi:
            # As narrow as the longest name allows (bold, the active Link's look), so the
            # bands start as far left as they can; every label the same width so the
            # bands stay aligned.
            bold = QFont(self._ribbon_labels[0].font())
            bold.setBold(True)
            fm = QFontMetrics(bold)
            width = min(_LinkBandLabel.MAX_WIDTH,
                        max(fm.horizontalAdvance(link.name) for link in self._links) + 16)
            for label in self._ribbon_labels:
                label.setFixedWidth(width)
        for i, (ribbon, label) in enumerate(zip(self._ribbons, self._ribbon_labels)):
            ribbon.set_flexible_height(multi)
            label.setVisible(multi)
            if multi and i < len(self._links):
                label.set_link(self._links[i].name, i == self._links.active_index)
        if n != self._timeline_rows_shown:
            self._timeline_rows_shown = n
            def resize() -> None:
                self.resizeDocks([self._timeline_dock],
                                 [n * TimelineRibbon.BAND_HEIGHT + 24], Qt.Vertical)
            if restored:             # a resize in the pass that restored the state is lost
                timers.after(0, self, resize)
            else:
                resize()

    def _update_timeline_domain(self) -> None:
        """Every ribbon covers the UNION of the Links' spans in global time, each in its
        own samples, so the stack can share one window. One Link: its own capture."""
        if len(self._links) < 2 or len(self._ribbons) < len(self._links):
            for ribbon in self._ribbons:
                ribbon.set_domain(None)
            return
        spans = [(link.to_ps(0), link.to_ps(ribbon._total))
                 for link, ribbon in zip(self._links, self._ribbons)]
        lo_ps, hi_ps = min(a for a, _ in spans), max(b for _, b in spans)
        for link, ribbon in zip(self._links, self._ribbons):
            ribbon.set_domain(link.to_sample(lo_ps), link.to_sample(hi_ps))
        self._timeline._reset_view()            # the whole union; the others follow

    def _on_ribbon_view(self, ribbon, lo: float, hi: float) -> None:
        """A ribbon was zoomed or panned: show the same INSTANTS on every other Link."""
        if self._syncing_views or len(self._links) < 2:
            return
        i = self._ribbons.index(ribbon)
        if i >= len(self._links):
            return
        lo_ps, hi_ps = self._links[i].to_ps(round(lo)), self._links[i].to_ps(round(hi))
        self._syncing_views = True
        try:
            for j, other in enumerate(self._ribbons[:len(self._links)]):
                if j != i:
                    other.set_view(self._links[j].to_sample(lo_ps),
                                   self._links[j].to_sample(hi_ps))
        finally:
            self._syncing_views = False

    def _sync_ribbon_cursors(self, sample: int) -> None:
        """The cursor (active Link's samples) drawn at the same instant on every other
        Link's ribbon."""
        a = self._links.active_index
        for j, ribbon in enumerate(self._ribbons[:len(self._links)]):
            if j != a:
                ribbon._cursor = self._links.convert(int(sample), a, j)
                ribbon.update()

    def _on_ribbon_seek(self, ribbon, sample: int) -> None:
        """A click on a Link's band makes that Link the active one and moves the cursor to
        the clicked sample on it, so a bookmark placed next (Ctrl+B) goes on the band that
        was clicked. Which Link each pane GROUP shows is left alone: they all follow the
        same instant anyway, and each group's Link is its picker's business."""
        i = self._ribbons.index(ribbon)
        if i < len(self._links) and i != self._links.active_index:
            self._activate_link(i)
        self._on_seek(self._clamp_to_active(int(sample)))

    def _on_ribbon_bookmark_moved(self, ribbon, label: str, sample: int, final: bool) -> None:
        """A bookmark dragged on a Link's band. Letting go makes that Link active, as a
        click on the band does, so the cursor lands on the bookmark on its own bus."""
        i = self._ribbons.index(ribbon)
        link = i if i < len(self._links) else None
        if final and link is not None and link != self._links.active_index:
            self._activate_link(link)
        self._on_bookmark_moved(label, sample, final, link=link)

    def _sync_view_links(self) -> None:
        """View ▸ Links shows in the Analyzer with two or more Links (with one, it would
        offer only to show that Link, and the All Links toggles are off)."""
        act = getattr(self, "_view_links_action", None)
        if act is not None:
            on = len(self._links) > 1 and self._mode_mgr_current() == ANALYSIS
            act.setVisible(on)
            act.setEnabled(on)

    def _mode_mgr_current(self):
        mgr = getattr(self, "_mode_mgr", None)
        return mgr.current() if mgr is not None else None

    def _link_label_menu(self, ribbon, pos) -> None:
        """Right-click a Link's timeline label: that Link's own actions. Each acts on THIS
        Link: it is made the active one first."""
        i = self._ribbons.index(ribbon)
        if i >= len(self._links):
            return
        menu = QMenu(self)
        rename = menu.addAction("&Rename Link…")
        offset = menu.addAction("Set &Time Offset…")
        remove = menu.addAction("Re&move Link…")
        offset.setEnabled(len(self._links) > 1)
        remove.setEnabled(len(self._links) > 1)
        menu.addSeparator()
        show = menu.addAction("&Show This Link Everywhere")
        chosen = menu.exec(pos)
        if chosen is None:
            return
        self._activate_link(i)
        if chosen is rename:
            self.rename_link()
        elif chosen is offset:
            self.set_link_offset_dialog()
        elif chosen is remove:
            self.remove_link(i)
        elif chosen is show:
            self.switch_link(i)

    def _region_band_menu(self, ribbon, section: int, pos) -> None:
        """Right-click a config-region band on a Link's timeline: Rename… names that region
        (an empty name clears it). The name is the Link's, saved with it in the workspace."""
        i = self._ribbons.index(ribbon)
        if i >= len(self._links):
            return
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        if menu.exec(pos) is not rename:
            return
        self.rename_region(section, link=i)

    def rename_region(self, section: int, link: Optional[int] = None) -> None:
        i = self._links.active_index if link is None else int(link)
        if not 0 <= i < len(self._links):
            return
        sess = self._links[i].session
        current = sess.region_names.get(int(section), "")
        n = len(sess.segments or [])
        name, ok = QInputDialog.getText(
            self, "Rename Region",
            f"Name for config region {int(section) + 1} of {n} (empty for none):",
            text=current)
        if not ok or name.strip() == current:
            return
        sess.set_region_name(section, name)
        if i < len(self._ribbons):
            self._ribbons[i].set_segments(self._band_segments(sess))   # the bands only

    def _on_ribbon_label(self, ribbon, rename: bool) -> None:
        i = self._ribbons.index(ribbon)
        if i >= len(self._links):
            return
        self.switch_link(i)
        if rename:
            self.rename_link()

    def _table_model(self):
        """The model the Commands table is showing: the active Link's, or All Links'."""
        return self._all_model if (self._cmd_all and self._all_model is not None) \
            else self._cmd_model

    def _row_target(self, src_row: int):
        """(Link index, command, cursor sample in that Link) for a row of the table on
        screen, in either scope."""
        if self._cmd_all and self._all_model is not None:
            if not 0 <= src_row < self._all_model.rowCount():
                return -1, None, None
            li = self._all_model.link_at(src_row)
            r = self._all_model.source_row_at(src_row)
            st = self._links[li].view
            return li, st.cmd_model.command_at(r), (st.starts[r] if r < len(st.starts) else None)
        cmd = self._cmd_model.command_at(src_row) if self._cmd_model is not None else None
        pos = self._starts[src_row] if 0 <= src_row < len(self._starts) else None
        return self._links.active_index, cmd, pos

    def _build_all_model(self) -> AllLinksCommandModel:
        parts = []
        for link in self._links:
            st = link.view
            rate = float(link.session.sample_rate_hz or 1)
            pos = np.asarray(st.starts, dtype=np.float64) * (1e12 / rate) + link.offset_ps
            parts.append((link.name, st.cmd_model, pos, link.offset_ps))
        return AllLinksCommandModel(parts)

    def _refresh_all_model(self) -> None:
        """Rebuild the All Links table after any Link's commands, name or offset changed.
        The proxy keeps its filter across the swap, and the table keeps its column widths
        (including any the user dragged): fitting to content measures every cell, the
        costliest step of a bind, so it is done once, as the per-Link tables do."""
        if self._all_model is not None and self._cmd_proxy.sourceModel() is self._all_model:
            self._all_col_widths = self._command_column_widths()
        self._all_model = self._build_all_model()
        self._cmd_proxy.setSourceModel(self._all_model)
        if not self._apply_column_widths(self._all_col_widths):
            self._cmd_view.fit_columns()
            self._all_col_widths = self._command_column_widths()
        self._update_cmd_title()

    def _on_link_combo(self, index: int) -> None:
        """The Commands picker: its own Link, or All Links. Only Commands changes."""
        if index >= len(self._links):
            self.set_commands_scope_all(True)
        else:
            self.set_group_link("commands", index)

    def set_commands_scope_all(self, on: bool) -> None:
        """Commands shows every Link's commands in global time order (All Links), or only
        the Link its picker names. Entering keeps the filter as it stands, offering every
        Link's command kinds; leaving restores that Link's own filter and column widths.
        The other pane groups keep the Links they show."""
        on = bool(on) and len(self._links) > 1
        if on == self._cmd_all:
            self._update_link_switcher()
            return
        with self._as_link(self._pane_link["commands"]):   # the Link Commands shows
            st = self._link_state()
            if on:
                st.filters = self._filter_snapshot()
                st.col_widths = self._command_column_widths()
                # The proxy's sort, not the header's: -1 is capture order (see CommandTable).
                self._per_link_sort = (self._cmd_proxy.sortColumn(), self._cmd_proxy.sortOrder())
                self._cmd_all = True
                self._refresh_all_model()
                # Sort by the GLOBAL Time column: the per-Link sort column's index now names
                # the Link column, which would order by Link, not time.
                self._cmd_view.sortByColumn(AllLinksCommandModel.TIME_COLUMN, Qt.AscendingOrder)
                self._restore_filters(st.filters, kinds=self._all_kinds())
            else:
                self._all_col_widths = self._command_column_widths()   # for next time
                self._cmd_all, self._all_model = False, None
                self._cmd_proxy.setSourceModel(self._cmd_model)
                section, order = getattr(self, "_per_link_sort", (-1, Qt.AscendingOrder))
                self._cmd_view.sortByColumn(section, order)    # -1: back to capture order
                self._apply_column_widths(st.col_widths)
                if st.filters is not None:
                    self._restore_filters(st.filters)
                else:
                    self._kind_menu._fill([(k, k) for k in st.kinds])
                    self._clear_all_filters()
        self._update_cmd_title()
        self._update_link_switcher()
        if self._session is not None:
            self._cursor_commands(int(self.cursor.sample))

    def _ensure_pane_pickers(self) -> None:
        if self._pane_pickers["right"]:
            return
        for group, docks in self._group_docks.items():
            for dock in docks:
                picker = QComboBox()
                picker.setStyleSheet(combo_qss())    # the arrow, as the Commands picker has
                picker.setToolTip("Which Link this pane group shows")
                picker.setFocusPolicy(Qt.NoFocus)
                picker.hide()
                picker.activated.connect(lambda i, g=group: self._on_group_picker(g, i))
                dock.titleBarWidget().add_leading(picker)
                self._pane_pickers[group].append(picker)

    def _on_group_picker(self, group: str, index: int) -> None:
        """A pane group's picker: a Link, or (the bottom group's last entry) All Links."""
        if group == "bottom" and index == len(self._links):
            self.set_bottom_scope_all(True)
        else:
            self.set_group_link(group, index)

    def _for_group(self, group: str, action):
        """A menu action that belongs to a pane group (Audio to the bottom group, Devices
        to the right, Commands to Commands) acts on the Link THAT group shows: make it the
        active Link first, then run the action."""
        def run(*_args):
            if len(self._links) > 1 and not (group == "commands" and self._cmd_all) \
                    and not (group == "bottom" and self._bottom_all):
                self._activate_link(self._pane_link[group])
            return action()
        return run

    def _group_of(self, widget):
        """The pane group a widget belongs to, or None (menus, dialogs, the timeline)."""
        w = widget
        while w is not None:
            if w is self._analysis_page:
                return "commands"
            for group, docks in self._group_docks.items():
                if w in docks:
                    return group
            w = w.parentWidget()
        return None

    def _on_pane_touched(self, widget) -> None:
        if len(self._links) < 2:
            return
        group = self._group_of(widget)
        if group is None or (group == "commands" and self._cmd_all):
            return                  # All Links: a row picks its own Link when selected
        if group == "bottom" and self._bottom_all:
            lane = max(lanes.lane_of(widget) for lanes in (
                self._raw_lanes, self._audio_lanes, self._symbol_lanes, self._sample_lanes,
                self._eye_lanes))
            if lane >= 0:           # a lane is its Link's, like a timeline band
                self._activate_link(lane)
                return
        self._activate_link(self._pane_link[group])

    def switch_link(self, index: int) -> None:
        """Show Link `index` in EVERY pane group and make it the active Link: the Links
        menu, a timeline band's label, and a newly added Link. (A group's own picker changes
        only that group — see set_group_link.) The cursor keeps the instant it marks, and
        the Commands table being left keeps its filter for when it is shown again."""
        if not 0 <= index < len(self._links):
            return
        if index == self._links.active_index and \
                all(self._pane_link[g] == index for g in _PANE_GROUPS):
            return
        if not self._cmd_all:                   # All Links: one shared table + filter
            self._save_commands_view(self._pane_link["commands"])
        self._stop_audio()                      # one output device; it played the old Link
        self._pane_link = dict.fromkeys(_PANE_GROUPS, index)
        self._activate_link(index)
        self._bind_link(index, filters=self._links[index].view.filters, feed_timeline=False)
        self._push_bookmarks()
        self._update_link_switcher()
        self._restore_cursor(self.cursor.sample)

    def set_group_link(self, group: str, index: int) -> None:
        """Show Link `index` in ONE pane group (its picker), leaving the others where they
        are, and make it the active Link, since that is the one just asked for."""
        if not 0 <= index < len(self._links):
            return
        if group == "commands" and self._cmd_all:
            self.set_commands_scope_all(False)  # back to its own Link's table first
        if group == "bottom" and self._bottom_all:
            self._pane_link["bottom"] = index   # the Link the pane's own views come back on
            self.set_bottom_scope_all(False)
        old = self._pane_link[group]
        if old == index:
            self._activate_link(index)
            self._update_link_switcher()
            return
        if group == "commands":
            self._save_commands_view(old)
        self._pane_link[group] = index
        self._activate_link(index)
        if group == "commands":
            self._bind_commands(self._links[index].view.filters)
            self._cursor_commands(self.cursor.sample)
        elif group == "right":
            self._bind_right()
            self._cursor_right(self.cursor.sample)
        elif group == "grid":
            with self._as_link(index):
                self._bind_grid()
                self._cursor_grid(self.cursor.sample)
        else:
            self._bind_bottom()
            self._cursor_bottom(self.cursor.sample)
            self._audio_view.set_cursor(self.cursor.sample)
        self._update_nav_starts()
        self._push_bookmarks()
        self._update_link_switcher()

    def _save_commands_view(self, index: int) -> None:
        """Keep Link `index`'s Commands filter and column widths while it is not shown."""
        if 0 <= index < len(self._links):
            self._links[index].view.filters = self._filter_snapshot()
            self._links[index].view.col_widths = self._command_column_widths()

    def _activate_link(self, index: int) -> None:
        """Make Link `index` the active one (what menus act on, what the cursor counts
        in, the bold band) WITHOUT redrawing any pane: the cursor is re-counted in its
        samples at the same instant, clamped into its capture.

        The clamp must not LOSE the instant: activating a Link whose capture does not
        cover the cursor, then another that does, has to come back to where the cursor
        was. So the true instant is remembered with the clamped number, and reused while
        the cursor has not moved since."""
        a = self._links.active_index
        if index == a or not 0 <= index < len(self._links):
            return
        new = self._links[index]
        if a >= 0:
            memo = self._cursor_instant
            if memo is not None and memo[:2] == (a, self.cursor.sample):
                t_ps = memo[2]                  # the unclamped instant this number stands for
            else:
                t_ps = self._links[a].to_ps(self.cursor.sample)
            last = max(0, int(new.session.last_sample()))
            self._links.set_active(index)
            clamped = min(max(0, new.to_sample(t_ps)), last)
            self.cursor.rebase(clamped)
            self._cursor_instant = (index, clamped, t_ps)
        else:
            self._links.set_active(index)
        self._sync_timeline_rows()
        self._update_title()
        for i, act in enumerate(self._link_actions):
            act.setChecked(i == index)
        if self._bottom_all:
            self._update_capture_actions()      # Audio ▸ Export exports the active Link

    def _command_column_widths(self) -> dict:
        """The Commands table's column widths by column name, for the shown columns only: a
        hidden column reads 0 wide, and a 0 set on it is the width it gets when shown."""
        view, model = self._cmd_view, self._cmd_view.model()
        out = {}
        for c in range(model.columnCount() if model is not None else 0):
            w = view.columnWidth(c)
            if not view.isColumnHidden(c) and w > 0:
                out[CommandTableView._column_name(model, c)] = int(w)
        return out

    def _filter_snapshot(self) -> dict:
        """The Commands filter as it stands, to be restored when this Link is shown again."""
        def checked(menu):
            return {a.data() for a in menu._actions if a.isChecked()}
        return {"kinds": checked(self._kind_menu), "kinds_none": self._kind_menu._none,
                "devices": checked(self._dev_menu),
                "groups": checked(self._group_menu),
                "errors_only": self._errors_only_act.isChecked(),
                "text": (self._expr_edit.text() if self._expr_edit is not None
                         else self._cmd_proxy.filter_text())}

    def _restore_filters(self, snap: dict, kinds=None) -> None:
        """Re-impose a `_filter_snapshot` on the menus and the proxy, for the active Link's
        command kinds (or `kinds`). A kind that is not offered is simply dropped."""
        self._kind_menu._fill([(k, k) for k in (kinds if kinds is not None
                                                  else self._link_state().kinds)])
        for menu, key in ((self._kind_menu, "kinds"), (self._dev_menu, "devices"),
                          (self._group_menu, "groups")):
            for a in menu._actions:
                a.blockSignals(True)
                a.setChecked(a.data() in snap[key])
                a.blockSignals(False)
        self._kind_menu._none = bool(snap.get("kinds_none"))
        self._cmd_proxy.set_kinds({a.data() for a in self._kind_menu._actions if a.isChecked()},
                                  none=self._kind_menu._none)
        self._cmd_proxy.set_devices(set(snap["devices"]))
        self._cmd_proxy.set_groups(set(snap["groups"]))
        self._errors_only_act.blockSignals(True)
        self._errors_only_act.setChecked(bool(snap["errors_only"]))
        self._errors_only_act.blockSignals(False)
        self._cmd_proxy.set_errors_only(bool(snap["errors_only"]))
        if self._expr_edit is not None:
            self._expr_edit.blockSignals(True)
            self._expr_edit.setText(snap["text"])
            self._expr_edit.blockSignals(False)
        self._cmd_proxy.set_text(snap["text"])
        self._on_command_filter_changed()

    def load_session(self, session: Session, audio_store=None, extras=None,
                     switch_mode: bool = True, add_link: bool = False) -> None:
        # `extras` (from the load worker) carries values precomputed off the GUI thread
        # so this method doesn't recompute them here and freeze the UI. None → compute
        # lazily (the synchronous demo path).
        extras = extras or {}
        # A re-decode (SSP step, register/scrambler override, Port-Samples collect) mutates
        # the SAME session in place and reloads it, so it is found among the Links: keep that
        # Link, its bookmarks and its command filter. `add_link` appends a Link and switches
        # to it; anything else is a new capture, which replaces the analysis.
        idx = self._links.index_of(session)
        re_decode = idx >= 0
        if re_decode:
            link = self._links[idx]
        elif add_link:
            link = self._links.add(session, view=LinkPanelState())
        else:
            link = self._links.replace(session, view=LinkPanelState())
            self._close_stale_history()
            self._bookmarks.clear()          # a new analysis; a re-decode or an added Link keeps them
            self._cmd_all, self._all_model = False, None     # one Link: nothing to merge
        self._build_link_state(link, audio_store, extras)
        if re_decode:
            self._refresh_history(session)
        if add_link and not re_decode:
            new_idx = self._links.index_of(session)
            self._bind_shared(new_idx, feed_timeline=True)   # its band, fed before it shows
            self.switch_link(new_idx)
            if self._cmd_all:
                self._refresh_all_model()                    # Commands: All Links gains it
        else:
            if not re_decode:
                self._pane_link = dict.fromkeys(_PANE_GROUPS, 0)   # one Link, everywhere
                idx = 0
                if self._bottom_all:          # one Link: its panes are bound once, below
                    self.set_bottom_scope_all(False, rebind=False)
            # A re-decode refreshes the groups showing that Link (possibly none, if they
            # were switched away) and its timeline band; the other groups are untouched.
            self._bind_link(idx, filters="keep" if re_decode else None)
        self._update_link_switcher()
        if link is not self._links.active:
            return
        self._push_bookmarks()
        # A capture is opened to be analysed, so land in Analysis mode where the
        # decoded, cursor-gated bus grid lives (Visualization shows the authored config,
        # which is unrelated to the capture). Opening a capture from any mode switches
        # here; `switch_mode=False` loads without leaving the current mode.
        if switch_mode and self._mode_mgr.current() != ANALYSIS:
            self._mode_mgr.switch_to(ANALYSIS)

    def _build_link_state(self, link, audio_store=None, extras=None) -> None:
        """Everything the panes need from one Link that is derived from its Session: built
        once per (re)decode, and kept across Link switches so a switch rebuilds nothing.
        Writes the Link's own state, which need not be the active one."""
        session, st = link.session, link.view
        extras = extras or {}
        # Command table shows the decoded commands PLUS synthetic 'Commit Point' rows at
        # each confirmed sync-point commit's SSP (where it takes effect) — sorted in with
        # the commands. PROTOTYPE: these extra rows are table-only (not in session.commands),
        # so the timeline ticks, error counts and audio are unchanged. _starts/_kinds are
        # built from this augmented list so cursor-selection + filtering stay aligned.
        # session.commands is chronological (decoder emits in ascending start_sample) and
        # commit_point_rows() is a small, sorted set of table-only rows; merge the two sorted
        # streams (O(N+M)). heapq.merge breaks ties by iterable order, so commands precede a
        # commit row at the same start_sample.
        table_cmds = list(heapq.merge(session.commands, session.commit_point_rows(),
                                      key=lambda c: int(c.get("start_sample", 0))))
        st.cmd_model = CommandTableModel(table_cmds, self._rmap,
                                         session.sample_rate_hz,
                                         peripheral_maps=session.peripheral_maps,
                                         row_origin=session.row_origin)
        # Cursor-selection / arrow-nav anchor samples: each command's Row Sync Point (its
        # cursor sample), NOT the raw Column-0 closing edge — so a cursor parked on a
        # command's RSP resolves to THAT command (bisect in RSP space), matching where
        # command_cursor_sample / symbol / commit navigation place the cursor.
        st.starts = [session.command_cursor_sample(c) for c in table_cmds]
        st.kinds = sorted({c.get("command", "") for c in table_cmds} - {""})
        st.audio_store = (audio_store if audio_store is not None
                          else session.audio_store())
        self._apply_stream_processing(st)         # a re-decode's new store keeps the setting
        st.sections = extras.get("sections")      # None → measured on first bind
        # capture_measurements iterates the whole audio store; Statistics is hidden at
        # load (Grid/Registers are raised), so defer the build to first show. It re-runs
        # on the next Statistics show after any re-decode (see _refresh_measurements).
        st.meas_dirty = True
        st.meas_rows = None
        st.eye_cache = {}                         # a new decode is a new measurement
        st.col_widths = None                      # refit to the new decode's content

    @contextlib.contextmanager
    def _as_link(self, index: int):
        """Run a pane group's work as if Link `index` were the active one, with the cursor
        re-counted in that Link's samples, so the (Link-unaware) pane code draws that Link
        at the same instant. If the code inside moves the cursor, the move is carried back
        into the active Link's samples on the way out."""
        a = self._links.active_index
        if index == a or not 0 <= index < len(self._links) or a < 0:
            yield
            return
        outer = self.cursor.sample
        inner = self._links.convert(outer, a, index)
        self.cursor.rebase(inner)
        self._links.set_active(index)
        try:
            yield
        finally:
            moved = self.cursor.sample
            self._links.set_active(a)
            self.cursor.rebase(outer if moved == inner
                               else self._links.convert(moved, index, a))

    def _bind_link(self, index: int, filters=None, feed_timeline: bool = True) -> None:
        """Point every pane group that shows Link `index` at it: after it (re)decodes, and
        when a group is switched to it.

        `filters` is the command filter to show: "keep" leaves the menus and proxy as they
        are (a re-decode of the Link on screen), None starts clean for this Link's command
        kinds (a new capture), and a snapshot from `_filter_snapshot` restores one.
        `feed_timeline` is False on a switch: each Link's ribbon already holds its data (fed
        when that Link decoded), and refeeding would reset the shared zoom."""
        for group in _PANE_GROUPS:
            if self._pane_link[group] != index:
                continue
            if group == "commands" and self._cmd_all:
                continue                        # All Links: refreshed below, not per Link
            with self._as_link(index):
                if group == "commands":
                    self._bind_commands(filters)
                elif group == "right":
                    self._bind_right()
                elif group == "grid":
                    self._bind_grid()
                else:
                    self._bind_bottom()
        if self._bottom_all and feed_timeline:
            # Its lane, after it (re)decoded. Not on a switch: the lane already shows it,
            # and a rebind would reset its channel choice and vertical zoom.
            self._bind_lane_of(index)
        if self._cmd_all and feed_timeline:
            self._refresh_all_model()           # every Link's table: any decode changes it
        self._bind_shared(index, feed_timeline)

    def _bind_commands(self, filters=None) -> None:
        """The Commands table for the Link it shows (run under `_as_link`)."""
        st = self._link_state()
        self._cmd_proxy.setSourceModel(self._cmd_model)
        # Column widths: fitted to content once per decode (measuring every cell is the
        # costliest step of a bind), then kept per Link, including any the user dragged.
        if not self._apply_column_widths(st.col_widths):
            self._cmd_view.fit_columns()
            st.col_widths = self._command_column_widths()
        if filters is None:
            self._kind_menu._fill([(k, k) for k in self._link_state().kinds])
            self._clear_all_filters()                        # don't carry a prior capture's filters
        elif filters != "keep":
            self._restore_filters(filters)
        # else: same capture re-decoded — keep the command filter (menu + proxy) intact.
        self._update_cmd_title()


    def _bind_right(self) -> None:
        """Registers and Statistics for the Link they show (run under `_as_link`)."""
        session = self._session
        assert session is not None
        st = self._link_state()
        # Initialize the register view and bus grid AS-OF the cursor's start (t=0),
        # not the whole-capture end state — so the far-left cursor shows the bus
        # before any configuration (defaults / empty grid), and, when a §5.1.2
        # bring-up is present, "No PHY Selected" until the PHY-select is decoded.
        # Both views then fill in as the cursor advances past the config commands.
        self._reg_view.set_files(session.register_files_at(0))
        self._reg_view.set_peripheral_maps(session.peripheral_maps)
        self._reg_view.set_device_names(session.device_names)
        self._reg_view.set_phy(session.phy_at(0))          # as of the files above
        # Statistics: this Link's rows if they were built, else built now when the pane is
        # showing, else on its first show (see _refresh_measurements).
        if st.meas_rows is not None and not st.meas_dirty:
            self._meas_view.set_rows(st.meas_rows)
        elif self._meas_dock.isVisible():
            self._refresh_measurements()
        else:
            self._meas_view.set_rows([])

    def _bind_grid(self) -> None:
        """The Bus Grid for the Link it shows (run under `_as_link`). Keep the TX-map
        scroll position across a re-decode (SSP step, override, CSV): _update_tx_scrollbar
        clamps it into the new capture's range rather than snapping back to row 0 on every
        re-decode. (A genuinely new/shorter capture just clamps to a valid row.)"""
        self._update_tx_scrollbar()
        self._apply_grid_for_sample(0)

    def _bind_bottom(self) -> None:
        """Capture, CDS, Audio, Samples and Timing for the Link they show (run under
        `_as_link`). In All Links, Capture and Audio are bound lane by lane instead
        (_bind_lane_of) and this binds the rest."""
        if not self._bottom_all:               # All Links: lane by lane (_bind_lane_of)
            self._bind_lane(self._raw_view, self._audio_view)
            self._bind_tables()
            self._bind_timing()

    def _bind_lane(self, raw, audio) -> None:
        """One Link's Capture and Audio views (run under that Link's `_as_link`).

        Each view first leaves its view of the Link it was showing in that Link's state (a
        switch, or a re-decode of the same Link), then shows this Link's as it was left."""
        st = self._link_state()
        self._keep_lane_view(raw, audio)
        self._bind_audio(audio)
        self._bind_capture(raw)
        # All Links lanes share one window (the lanes align it), so not their own zooms.
        view_state.restore_audio(audio, st.audio_view, zoom=not self._bottom_all)
        view_state.restore_capture(raw, st.capture_view, zoom=not self._bottom_all)
        audio._shown_state = raw._shown_state = st
        if not self._bottom_all:                       # All Links: after the lane (_bind_lane_of)
            self._update_capture_actions()             # a capture is now loaded
        self._build_decimation_menu()            # per-dataport decimation for this capture's streams

    @staticmethod
    def _keep_lane_view(raw, audio) -> None:
        """Leave a Capture and an Audio view's current state with the Link they show."""
        for view, key, read in ((audio, "audio_view", view_state.audio_state),
                                (raw, "capture_view", view_state.capture_state)):
            prev = getattr(view, "_shown_state", None)
            if prev is not None and getattr(view, "_store" if view is audio else "_plot",
                                            None) is not None:
                setattr(prev, key, read(view))

    def _bind_audio(self, view) -> None:
        session = self._session
        assert session is not None
        view.stop()                                # one output device; it played the old Link
        view.set_stream_colors(self._link_state().stream_colors, redraw=False)   # set_store draws
        view.set_stream_names(session.dp_names, redraw=False)
        # Audio waveforms are plotted in CAPTURE-sample space so they sit at their true
        # offset and line up under the timeline — give the view the capture rate + full
        # extent (commands + audio) BEFORE set_store (which rebuilds the plots).
        capture_total = max([c.get("end_sample", 0) for c in session.commands]
                            + [session.audio_end_sample, 1])
        view.set_capture_context(session.sample_rate_hz, capture_total)
        view.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        view.set_store(self._audio_store)

    def _bind_capture(self, view) -> None:
        session = self._session
        assert session is not None
        view.set_stream_colors(self._link_state().stream_colors, redraw=False)   # drawn below
        view.set_stream_names(session.dp_names, redraw=False)
        # DLV (PHY3): the Raw view collapses the complementary DP/DN pair to one
        # differential trace in the audio region and draws the recovered bit clock
        # beneath it; FBCSE passes no DLV info and keeps the two forwarded-clock traces.
        _dlv = session.is_dlv
        _rec = session.recovered_clock() if _dlv else None
        view.set_capture(session.capture,
                         dp_is_data_line=session.link_control.lc_on_data_line,
                         dlv=_dlv, audio_start=session.audio_start_sample,
                         recovered_clock=_rec,
                         segments=session.segments if _dlv else None)
        # Nominal samples per bus row (row rate → samples), for the zoom-to-row button.
        _row_hz = (session.row_rate_khz or 0.0) * 1000.0
        view.set_row_samples(session.sample_rate_hz / _row_hz if _row_hz else 0.0)
        # Samples per UI (max-zoom floor: don't let the user zoom in past ~1 UI).
        view.set_ui_samples(
            session.sample_rate_hz / session.ui_rate_hz if session.ui_rate_hz else 0.0)
        view.set_cds_provider(session.cds_column_samples)
        view.set_commit_provider(session.commit_column_samples)
        # DLV: flag recovered Row Sync Points with no 0→1 edge on the wire (PLL lock gaps)
        # and raise the unlock banner when there are more than the session's threshold.
        view.set_missing_provider(session.missing_rsp_samples)
        view.set_pll_unlocked(session.pll_unlocked)
        view.set_row_label(
            lambda smp: session.display_row(session.bus_row_for_sample(int(smp))))
        view.set_port_marks(session.port_bit_marks, self._audio_lanes_of_store())

    def _audio_lanes_of_store(self) -> list:
        """The active Link's (device, dp, channel) lanes, in audio-store order."""
        _store = self._audio_store
        return [(d, p, ch) for (d, p) in _store.streams() for ch in _store.channels(d, p)]

    def _bind_tables(self) -> None:
        """CDS and Samples for the Link they show (run under `_as_link`, or `_as_lane`
        for a lane's tables)."""
        session = self._session
        assert session is not None
        st = self._link_state()
        self._sample_view.set_stream_colors(st.stream_colors, redraw=False)   # filled below
        self._sample_view.set_stream_names(session.dp_names, redraw=False)
        self._symbol_view.set_sample_rate(session.sample_rate_hz)
        self._symbol_view.set_row_origin(session.row_origin)
        self._symbol_view.set_symbols(session.symbols())
        _lanes = self._audio_lanes_of_store()
        self._sample_view.set_sample_rate(session.sample_rate_hz)
        self._sample_view.set_row_origin(session.row_origin)
        self._sample_view.set_lanes(_lanes)
        # The Samples and CDS panes hold a window of whichever Link they last showed, so the
        # active Link's windows are rebuilt on demand (cheap: windowed re-decodes).
        self._sample_range = None          # (lo,hi) loaded row positions; None = needs (re)build
        self._sample_total = 0             # total filtered samples (full scrollable extent)
        self._sample_filters = None        # filters the loaded window was built with
        self._symbol_range = None          # (first,last) start_sample of the loaded CDS window

    def _bind_timing(self) -> None:
        """The Timing pane for the Link it shows (run under `_as_link`, or `_as_lane`)."""
        session = self._session
        assert session is not None
        st = self._link_state()
        _rec = session.recovered_clock() if session.is_dlv else None
        self._eye_view.set_port_names(session.dp_names)      # before the filter is built
        self._eye_view.set_analysis_context(session.segments, session.timing_regions(),
                                            session.timing_column_roles)
        self._eye_view.set_capture(session.capture, recovered_clock=_rec, cache=st.eye_cache)
        if st.sections is None:
            st.sections = session.section_ui_stats()          # per-section min/max UI
        self._eye_view.set_sections(st.sections)

    def _bind_shared(self, index: int, feed_timeline: bool) -> None:
        """What spans the Links: Link `index`'s timeline band, the title and the status."""
        # Remind the user which capture is loaded — but only in Analyzer mode (see
        # _update_title): the capture is irrelevant to the Visualizer/Timing views.
        self._update_title()
        self._sync_timeline_rows()
        if feed_timeline and 0 <= index < len(self._ribbons):
            with self._as_link(index):
                self._feed_timeline(self._timeline, self._session)
                self._timeline.set_cursor(0)
                self._timeline.set_commit_starts(self._sscr_samples())   # ⌘+arrow jump
            self._update_timeline_domain()
        self._update_nav_starts()               # arrow-nav starts (all visible at load)
        session = self._session
        if session is None:
            return
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

    def export_audio(self) -> None:
        if self._session is None or self._audio_store is None or self._audio_store.is_empty():
            return
        from .audio_export_dialog import AudioExportDialog
        dlg = AudioExportDialog(self._audio_store,
                                visible_sample_range=self._audio_lane().visible_sample_range(),
                                default_rates=self._audio_lane().play_rate_targets,
                                parent=self, names=self._session.dp_names)
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
        try:
            paths = []
            notes = []
            for dev, dp in dps:
                rate = dlg.target_rate(dev, dp)
                path = os.path.join(directory,
                                    f"swi3s{self._link_stem()}_dev{dev}_dp{dp}.wav")
                paths.append(self._audio_store.export_wav(dev, dp, path,
                                                          index_range=dlg.index_range(dev, dp),
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
        # With All Links on screen, export what it shows: every Link, in global time.
        merged = self._cmd_all and len(self._links) > 1
        stem = "_all_links" if merged else self._link_stem()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export commands CSV",
            os.path.join(self._last_capture_dir("analyzer"), f"commands{stem}.csv"),
            "CSV (*.csv)")
        if not path:
            return
        self._remember_capture_dir(path, "analyzer")
        try:
            if merged:
                write_links_commands_csv(
                    [(link.name, link.session.commands, link.to_ps) for link in self._links],
                    path, self._rmap)
            else:
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
            os.path.join(self._last_capture_dir(grid_mode),
                         f"bus_grid{self._link_stem() if grid is self._grid_view else ''}.svg"),
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
            os.path.join(self._last_capture_dir("visualizer"), f"bus_grid_settings{self._link_stem()}.csv"),
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
        self._stop_audio()
        keep_cursor = self.cursor.sample

        def done(_s, cur=keep_cursor):
            if _s is not self._session:
                return                  # another Link was shown meanwhile; nothing to restore
            self._restore_cursor(cur)
            if after is not None:
                after()
            if status:
                self._status.setText(status)

        self._load_async(build_fn, after=done, kind="redecode")

    def show_register_history(self, device: int, address: int, label: str = ""):
        """Registers ▸ right-click a register ▸ History…: every write and read of it on the
        Link the Registers pane shows, failed attempts included, in a window of its own.
        Double-clicking an access puts the cursor on its command's Row Sync Point, as the
        Commands table does, on that Link. The Link is found by its Session, so a removal
        that renumbers the Links does not retarget it; once that Session is no longer a
        Link the window is closed (`_close_stale_history`), and a re-decode of it re-reads
        the window's accesses (`_refresh_history`)."""
        from ..analysis.register_history import register_history
        from .register_history_dialog import RegisterHistoryDialog
        i = self._pane_link.get("right", self.links.active_index)
        if not 0 <= i < len(self._links):
            return None
        sess = self._links[i].session
        events = register_history(sess.commands, int(device), int(address), sess.register_map,
                                  resets=sess.bus_reset_samples())
        title = label or f"Device {device} · 0x{int(address):08X}"
        if len(self._links) > 1:
            title = f"{self._links[i].name} · {title}"
        dlg = RegisterHistoryDialog(title, events, sess.sample_rate_hz, sess.display_row, self,
                                    session=sess, device=int(device), address=int(address))

        def jump(sample: int) -> None:
            link = self._links.index_of(sess)
            if link < 0:
                return
            if link != self.links.active_index:
                self._activate_link(link)
            self._on_seek(int(sess.row_sync_sample(int(sample))))
        dlg.jump.connect(jump)
        dlg.show()
        return dlg

    def _history_dialogs(self) -> list:
        from .register_history_dialog import RegisterHistoryDialog
        return self.findChildren(RegisterHistoryDialog)

    def _refresh_history(self, session) -> None:
        """Re-read the accesses of each open History window on `session` from its current
        decode."""
        from ..analysis.register_history import register_history
        for dlg in self._history_dialogs():
            if dlg.session is not session or dlg.device is None or dlg.address is None:
                continue
            dlg.set_events(register_history(session.commands, int(dlg.device),
                                            int(dlg.address), session.register_map,
                                            resets=session.bus_reset_samples()),
                           session.sample_rate_hz)

    def _close_stale_history(self) -> None:
        """Close each open History window whose Session is no longer one of the Links."""
        for dlg in self._history_dialogs():
            if self._links.index_of(dlg.session) < 0:
                dlg.close()

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
        self._show_dock(self._raw_dock)
        self._capture_lane().center_on_sample(int(sample), 2)

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
        link, cmd, _pos = self._row_target(src.row())
        if cmd:
            if link != self._links.active_index:
                self.switch_link(link)            # an All Links row shows its own bus
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
        link = self._links.active_index           # a bookmark belongs to the Link it is on
        if not self._bookmarks.remove_at(s, tol=tol, link=link):
            bm = self._bookmarks.add(s, link=link)
            self._status.setText(f"Cursor {bm.display()} @ {s:,}")
        else:
            self._status.setText(f"{len(self._bookmarks)} cursor(s)")
        self._push_bookmarks()

    def _bookmark_tol(self) -> int:
        """Hit tolerance (samples) for removing/snapping a bookmark: ~half a UI so a
        Ctrl+B on an existing marker toggles it rather than stacking a second one."""
        try:
            spu = self._session.sample_rate_hz / self._session.ui_rate_hz
            return max(1, int(spu // 2))
        except Exception:                       # noqa: BLE001 — never break the toggle
            return 1

    def _bm_sample(self, bm) -> int:
        """Where a bookmark sits in the ACTIVE Link's samples, which is what the cursor and
        every per-Link pane count in. A bookmark on the active Link is its own sample."""
        if len(self._links) == 0:
            return int(bm.sample)
        return self._links.convert(bm.sample, bm.link, self._links.active_index)

    def _bm_marks(self, link: int) -> list:
        """(sample, label) for the bookmarks ON Link `link`, in its own samples: a view
        draws only its own Link's bookmarks; a pair across two Links is read in the
        Bookmarks pane."""
        return [(int(b.sample), b.display()) for b in self._bookmarks if b.link == link]

    def _bm_ps(self, bm) -> int:
        """A bookmark's global time (ps), for ordering and timing across Links."""
        return self._links[bm.link].to_ps(bm.sample)

    def _active_bookmark_samples(self) -> list:
        """The active Link's bookmarks, in its samples: the ones its band shows, and so the
        only ones next / previous may land on (another Link's would park the cursor on a
        mark nothing on this Link draws)."""
        a = self._links.active_index
        return [int(b.sample) for b in self._bookmarks if b.link == a]

    def next_bookmark(self) -> None:
        bms = sorted(self._active_bookmark_samples())
        if not bms:
            return
        cur = self.cursor.sample
        self.cursor.set_sample(self._clamp_to_active(next((s for s in bms if s > cur), bms[0])))

    def prev_bookmark(self) -> None:
        bms = sorted(self._active_bookmark_samples(), reverse=True)
        if not bms:
            return
        cur = self.cursor.sample
        self.cursor.set_sample(self._clamp_to_active(next((s for s in bms if s < cur), bms[0])))

    def clear_bookmarks(self) -> None:
        self._bookmarks.clear()
        self._push_bookmarks()

    def _push_bookmarks(self) -> None:
        """Refresh every bookmark consumer from the current set: the timeline, audio and
        raw panes (markers) and the Measurements pane (pair deltas)."""
        for j, ribbon in enumerate(self._ribbons[:max(1, len(self._links))]):
            ribbon.set_bookmarks(self._bm_marks(j))          # each band: its own Link's
        for k, raw, audio in self._bottom_lanes():           # each lane: its Link's
            marks = self._bm_marks(k)
            raw.set_bookmarks(marks)
            audio.set_bookmarks(marks)
        self._pair_view.set_rows(self._bookmark_measure_rows())
        self._pair_view.set_show_link(len(self._links) > 1)

    def _bookmark_measure_rows(self) -> list:
        """Bookmark rows for the pane: per group, an A1 row, an A2 row (if it exists), and
        an A1-A2 delta row. A lone bookmark shows just its A1 row (no waiting for a pair).
        Each row is (label, UI, Row, Time, seek, Link name, Link index); a pair's Link is
        "Link 1 → Link 2" when it spans two, and its index is its first member's.

        A bookmark's UI, Row and Time are read on ITS OWN Link; Time includes the Link's
        offset, so it is global time. The seek is GLOBAL time (ps), converted only when the
        row is clicked (`_on_pair_seek`), which first makes the row's Link active: a view
        draws only its own Link's bookmarks, so that is where the mark can be seen."""
        rows: list = []
        if len(self._links) == 0:
            return rows

        def rates(link):
            sess = self._links[link].session
            rate = float(getattr(sess, "sample_rate_hz", 0) or 0)
            ui_rate = float(getattr(sess, "ui_rate_hz", 0) or 0)
            return sess, rate, ui_rate, (rate / ui_rate) if (rate and ui_rate) else 0.0

        def ui_of(bm):
            try:                                    # a COUNT of UIs, exact on both PHYs
                return f"{self._links[bm.link].session.ui_index(int(bm.sample)):,}"
            except Exception:                       # noqa: BLE001
                return "—"

        def row_of(bm):
            sess = self._links[bm.link].session
            try:
                return f"{sess.display_row(sess.bus_row_for_sample(int(bm.sample))):,}"
            except Exception:                       # noqa: BLE001
                return "—"

        def link_name(i):
            return self._links[i].name if 0 <= i < len(self._links) else "?"

        def time_of(bm):
            rate = rates(bm.link)[1]
            if not rate:
                return f"{int(bm.sample):,} smp"
            return _fmt_duration(bm.sample / rate + self._links[bm.link].offset_ps / 1e12)

        # Group members by their stable group letter (items() is creation-ordered A1,A2,B1…).
        groups: Dict[str, dict] = {}
        for bm in self._bookmarks.items:            # `items` is a property (all bookmarks)
            groups.setdefault(bm.group, {})[bm.index] = bm
        for g in sorted(groups):
            a1 = groups[g].get(1)
            a2 = groups[g].get(2)
            for member in (a1, a2):
                if member is not None:
                    rows.append((member.display(), ui_of(member), row_of(member),
                                 time_of(member), self._bm_ps(member),
                                 link_name(member.link), member.link))
            if a1 is not None and a2 is not None:
                if a1.link == a2.link:
                    dui, drows_s, dt = self._same_link_delta(a1, a2, *rates(a1.link))
                    names = link_name(a1.link)
                else:
                    dui, drows_s, dt = self._cross_link_delta(a1, a2)
                    names = f"{link_name(a1.link)} → {link_name(a2.link)}"
                rows.append((f"{a1.display()}-{a2.display()}", dui, drows_s, dt,
                             self._bm_ps(a1), names, a1.link))
        return rows

    @staticmethod
    def _same_link_delta(a1, a2, sess, rate, ui_rate, spu):
        """(ΔUI, ΔRows, ΔTime) for a pair on one Link, counted in that Link's samples.
        ΔUI and ΔRows are COUNTS of the UIs and rows between the two, so they stay right
        across a geometry change (on DLV the UI rate changes fourfold at the Safe-Lock-4
        to 16-column commit)."""
        dsamp = a2.sample - a1.sample
        dt = _fmt_duration(dsamp / rate) if rate else f"{dsamp:,} smp"
        try:
            drows = abs(sess.bus_row_for_sample(a2.sample)
                        - sess.bus_row_for_sample(a1.sample))
            drows_s = f"{drows:,}"
        except Exception:                   # noqa: BLE001
            drows_s = "—"
        try:
            dui = f"{abs(sess.ui_index(a2.sample) - sess.ui_index(a1.sample)):,}"
        except Exception:                   # noqa: BLE001
            dui = "—"
        return dui, drows_s, dt

    def _cross_link_delta(self, a1, a2):
        """(ΔUI, ΔRows, ΔTime) for a pair on two Links. Time is exact global time. Row
        NUMBERS of two Links share no origin, so ΔRows and ΔUI are elapsed time at a
        common rate, and each is shown only when the Links SHARE that timing
        (`_shared_rates`): one UI (row) rate on both, across the whole interval."""
        dt_s = (self._bm_ps(a2) - self._bm_ps(a1)) / 1e12
        ui_hz, row_hz = self._shared_rates(a1, a2)
        dui = f"{abs(dt_s) * ui_hz:,.1f}" if ui_hz else "—"
        drows = f"{abs(dt_s) * row_hz:,.1f}" if row_hz else "—"
        return dui, drows, _fmt_duration(dt_s)

    def _shared_rates(self, a1, a2):
        """(UI rate, row rate) in Hz that two Links share over the interval between two
        bookmarks, each None where they do not.

        A Link's timing is not one number: a geometry change moves its row rate (FBCSE) or
        its UI rate (DLV). So each Link's stretch of the interval (in its own samples) must
        lie inside its capture, every region it crosses (`Session.rate_regions`) must have
        a measured rate, and all of them, on both Links, must agree. Measured rates of two
        captures never agree to the last digit (two analyzers' timebases differ by tens of
        ppm), so "agree" is `_SAME_RATE_REL_TOL`, not equality."""
        t_lo, t_hi = sorted((self._bm_ps(a1), self._bm_ps(a2)))
        found: list = []
        for i in sorted({a1.link, a2.link}):
            entry = self._links[i]
            lo, hi = entry.to_sample(t_lo), entry.to_sample(t_hi)
            if lo < 0 or hi > entry.session.last_sample():
                return None, None              # this Link was not recording throughout
            found += [(u, r) for s, e, u, r in entry.session.rate_regions()
                      if s <= hi and e > lo]

        def common(values):
            if not values or any(v is None for v in values):
                return None
            if all(math.isclose(v, values[0], rel_tol=_SAME_RATE_REL_TOL) for v in values):
                return sum(values) / len(values)
            return None

        return common([u for u, _r in found]), common([r for _u, r in found])

    def _on_bookmark_moved(self, label: str, sample: int, final: bool,
                           link: Optional[int] = None) -> None:
        """A bookmark was dragged in a pane. Move it (snapping to the nearest edge on
        release) and refresh all consumers so the pair measurement updates live.

        `sample` counts in Link `link` (default: the active Link), the Link of the view it
        was dragged in, which since each view draws only its own Link's bookmarks is the
        bookmark's own. It is converted anyway, so a caller that names another Link is
        still right. The bookmark snaps to its own Link's clock edges."""
        target = next((b for b in self._bookmarks if b.display() == label), None)
        if target is None:
            return
        src = self._links.active_index if link is None else int(link)
        s = self._links.convert(int(sample), src, target.link)
        if final:
            s = self._snap_to_edge(s, self._links[target.link].session)
        self._bookmarks.move(target, s)
        self._push_bookmarks()
        if final:
            self.cursor.set_sample(self._clamp_to_active(self._bm_sample(target)))

    def _snap_to_edge(self, sample: int, session=None) -> int:
        """Snap a dragged sample to the nearest clock edge of `session` (default: the active
        Link's), so a bookmark lands on a real UI boundary. Falls back to the raw sample if
        there are no edges."""
        try:
            ce = (session if session is not None else self._session).capture.clock_edges
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

    def _on_pair_seek(self, t_ps: int, link: int = -1) -> None:
        """A Bookmarks-pane row: its bookmark's Link made active (the one whose band draws
        it), then the cursor to its instant (global ps) there. Without a Link, the
        instant on the Link active now."""
        if 0 <= link < len(self._links) and link != self._links.active_index:
            self._activate_link(link)
        if self._links.active is not None:
            self._on_seek(self._clamp_to_active(self._links.active.to_sample(int(t_ps))))

    def _clamp_to_active(self, sample: int) -> int:
        """A sample converted from another Link can fall outside the active Link's
        capture; the panes are only defined inside it."""
        if len(self._links) < 2 or self._session is None:
            return int(sample)
        return min(max(0, int(sample)), max(0, int(self._session.last_sample())))

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
        self._sync_ribbon_cursors(sample)
        # The waveform cursor line, at this instant in the Link each lane shows.
        for k, _raw, audio in self._bottom_lanes():
            audio.set_cursor(self._in_link(k, sample))
        # During playback do NO heavy main-thread work. Re-rendering the raw-capture
        # envelope (auto-pan when zoomed), replaying the register files, the windowed
        # symbol re-decode and the grid rebuild are all heavy Python that holds the
        # GIL and starves the (Python) audio pull-source on its thread → glitchy audio
        # (and the raw view scrolling mid-play). Defer them all to one refresh at the
        # resting cursor on stop / clip end (_on_playback_stopped → _on_cursor).
        if self._audio_playing():
            return
        # Defer the heavy cascade so clicking around the timeline doesn't stack a full
        # rebuild per click on the main thread. Capture the guard flags NOW: the deferred
        # handler must see the sync / from-pane state as it was at THIS cursor change, not
        # whatever it is 20 ms later (from_symbol/from_sample are reset synchronously right
        # after set_sample returns). Rapid changes overwrite the pending entry, so only the
        # resting cursor triggers a rebuild.
        self._pending_cursor = (int(sample), self._syncing, self._from_symbol,
                                self._from_sample, self._origin_lane)
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
        sample, syncing, from_symbol, from_sample, origin = self._pending_cursor
        # Each pane group redraws at this instant on the Link IT shows (converted from the
        # captured sample, which may trail the live cursor by the coalescing interval).
        s_bottom, s_right = self._in_group("bottom", sample), self._in_group("right", sample)
        with self._as_link(self._pane_link["bottom"]):
            self._cursor_bottom(s_bottom, from_symbol, from_sample)
        with self._as_link(self._pane_link["right"]):
            self._cursor_right(s_right)
        with self._as_link(self._pane_link["grid"]):
            self._cursor_grid(self._in_group("grid", sample))
        if self._bottom_all:
            for k, raw, _audio in self._bottom_lanes():
                s_k = self._in_link(k, sample)
                raw.set_cursor(s_k)
                with self._as_lane(k):          # a lane's own click does not re-window it
                    self._cursor_tables(s_k, from_symbol and k == origin,
                                        from_sample and k == origin)
        if not syncing:
            self._cursor_commands(sample)

    def _in_group(self, group: str, sample: int) -> int:
        """An active-Link sample counted in the Link pane group `group` shows."""
        if len(self._links) < 2:
            return int(sample)
        return self._links.convert(int(sample), self._links.active_index,
                                   self._pane_link[group])

    def _cursor_bottom(self, sample: int, from_symbol: bool = False,
                       from_sample: bool = False) -> None:
        """Bottom group at the cursor (run with its Link active)."""
        if self._session is None:
            return
        if not self._bottom_all:                # All Links: each lane, in _apply_cursor_heavy
            self._raw_view.set_cursor(sample)
            self._cursor_tables(sample, from_symbol, from_sample)

    def _cursor_tables(self, sample: int, from_symbol: bool, from_sample: bool) -> None:
        """CDS and Samples at the cursor (run with their Link active, or `_as_lane`)."""
        # Symbol viewer follows the cursor via windowed (seeked) re-decode, so it
        # works on big captures without scanning from the start. Skip if hidden, and
        # skip when the cursor change *came from* the symbol pane (a click / arrow
        # key) — re-windowing there would snap the selection back to the command's
        # comma, trapping navigation on the first symbols.
        if not self._symbol_dock.isHidden() and not from_symbol:
            self._refresh_symbols_around(sample)
        if self._sample_dock.isVisible() and not from_sample:
            self._refresh_samples_around(sample)

    def _cursor_grid(self, sample: int) -> None:
        """The Bus Grid at the cursor (run with its Link active). If what-if edits are
        active, keep the projected grid; else show the grid as-of the cursor — it fills in
        as registers are written, and for a true multi-width reconfiguration (cold-start
        2col -> 8col) the per-segment geometry follows the cursor. In the scrollable TX
        map, geometry is scoped to the cursor's config region, so a move to a
        different-width region re-anchors the window on the cursor's row. The rebuild is
        heavy (full scene): skipped while the Bus Grid tab is hidden, and
        _on_grid_dock_visible re-renders it when re-shown."""
        if self._session is not None and not self._grid_dock.isHidden():
            self._update_grid_for_cursor(sample)

    def _cursor_right(self, sample: int) -> None:
        """Right group at the cursor (run with its Link active). Rebuilding the register
        tree replays command history (C++) — skip it when the Registers tab is hidden;
        _on_reg_dock_visible refreshes it when re-shown."""
        if self._session is not None and not self._reg_dock.isHidden():
            # The PHY block follows the bring-up in force: a later one can select another.
            self._reg_view.set_phy(self._session.phy_at(sample))
            self._reg_view.set_files(self._session.register_files_at(sample))

    def _cursor_commands(self, sample: int) -> None:
        """Select the command at the cursor (active-Link `sample`) in the Commands table.
        All Links bisects global time, so it runs as-is; a single Link's table runs with
        that Link active."""
        if self._cmd_all:
            self._select_command_for_sample(sample)
            return
        s_cmd = self._in_group("commands", sample)
        with self._as_link(self._pane_link["commands"]):
            self._select_command_for_sample(s_cmd)

    @_on_grid_link
    def _refresh_grid(self) -> None:
        """Re-render the Bus Grid at the cursor, on the Link the grid group shows."""
        if self._session is not None:
            self._apply_grid_for_sample(self.cursor.sample)

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
        # The bring-up the cursor is in, the first or a later one (the Link went inactive,
        # its clock stopped, and a new start sequence followed). A later one of either kind
        # gets the overlay: the bus stopped, so the old grid is not on the wire, and the
        # stretch the clock stopped for (its Idle) reads Link Inactive.
        lc = s.bringup_at(sample)
        later = lc is not None and lc is not s.link_control
        if lc is not None and later and lc.sequence == "warm":
            phase = next((sec["name"] for sec in lc.sections
                          if int(sec.get("start", 0)) <= sample < int(sec.get("end", 0))), "")
            if phase == "Idle":
                self._grid_view.show_message(
                    "Link Inactive", "The clock stopped; a new start sequence follows (§5.1.2)")
            else:
                sl = f" — Safe-Lock-{lc.safe_lock_columns}" if lc.safe_lock_columns else ""
                self._grid_view.show_message(
                    f"{lc.phy_name or 'PHY'} Reused",
                    f"Warm Start, audio not started yet{sl} (§5.1.2)")
            return
        if lc is not None and lc.sequence == "cold":
            audio_start = int(lc.audio_start_sample or 0)
            # The selected PHY is only KNOWN once its number has been fully clocked — at
            # PhyStart (the final DP falling edge → audio-mode PHY). Before that point
            # nothing on the wire names a PHY, so don't claim one (the decoded result knows
            # the answer, but at THIS cursor it can't be known yet). After it — but before
            # audio starts — the PHY is fixed, just with no geometry yet.
            phy_known_at = lc.phystart_sample or lc.phy_select_sample or audio_start
            if sample < int(phy_known_at):
                # Name the current bring-up sub-phase (Bus Reset / Cold Start / PhyStart)
                # for context — never the PHY, which isn't decoded yet at this cursor.
                phase = next((sec["name"] for sec in lc.sections
                              if int(sec.get("start", 0)) <= sample < int(sec.get("end", 0))),
                             "")
                if phase == "Idle" and later:
                    self._grid_view.show_message(
                        "Link Inactive",
                        "The clock stopped; a new start sequence follows (§5.1.2)")
                    return
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
                token = _TxPersistRequest(cds_col)
                self._tx_persist_token = token
                cached = s.tx_persist_cached(sample)
                if cached is not None:
                    self._render_tx_persist(cached[0], int(cached[1]), token)   # instant
                else:
                    origin, _c, end_ui = s._tx_persist_key(sample)
                    if (end_ui - origin) <= _TX_PERSIST_SYNC_MAX_UIS:
                        # Small region (_TX_PERSIST_SYNC_MAX_UIS): compute inline — the
                        # thread overhead + async round-trip isn't worth it, and it keeps the
                        # common case (demo, short captures) synchronous.
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
        unless a later cursor move / toggle / re-decode superseded this request. The CDS
        column is the request's (the Bus Grid Link's when it was asked for), not the
        active Link's."""
        if (self._session is None or not self._tx_map or not self._tx_persist
                or getattr(self, "_tx_persist_token", None) is not token):
            return
        n = max(1, self._grid_rows)
        cds_col = int(getattr(token, "cds_col", 0))
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

    @_on_grid_link
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

    @_on_grid_link
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

    @_on_grid_link
    def _on_tx_scroll(self, value: int) -> None:
        """Scroll the TX-map window to top row `value` (0-based) and re-render."""
        self._tx_start_row = max(0, int(value))
        if self._session is not None and self._tx_map:
            self._apply_grid_for_sample(self.cursor.sample)

    @_on_grid_link
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
        audio = self._audio_lane()
        keep_sel = audio.channel_selection()
        keep_zoom = audio.visible_index_range()
        r = int(row)

        def apply(s=self._session, r=r):
            s.set_ssp_row(r)
            return s

        def after(sel=keep_sel, zoom=keep_zoom, r=r, audio=audio):
            audio.set_channel_selection(sel)   # keep hidden tracks hidden
            audio.set_visible_index_range(zoom)  # then hold the zoom
            # Honest status: the decoder only arms the manual SSP when the payload
            # engine is enabled at decode start (a config CSV supplied the geometry and
            # audio decoded). On a snoop/no-audio capture it's a no-op — say so rather
            # than claiming a clean re-decode. Row shown 0-based (decoder keeps internal).
            sess = self._session
            if r < 0:
                msg = "Manual SSP cleared (auto anchor); re-decoded."
            elif sess is not None and not sess.ssp_effective:
                msg = (f"SSP set to row {sess.display_row(r)}, but it only re-phases audio "
                       f"when a config CSV is applied (Decode ▸ Import Visualizer CSV) — "
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
        skipping the C++ symbols_around re-decode AND the QTableWidget rebuild."""
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
            # Column-0 closing edge one UI later (as command / commit selection do).
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
        show) or the cursor moved outside it, and otherwise just re-selects."""
        if visible and self._session is not None:
            for k, _raw, _audio in self._bottom_lanes():
                with self._as_lane(k):
                    self._refresh_samples_around(self.cursor.sample)

    def _on_reg_dock_visible(self, visible: bool) -> None:
        """Refresh the register tree when its tab is shown/raised (its per-tick rebuild is
        skipped in _on_cursor while hidden)."""
        if visible and self._session is not None:
            with self._as_link(self._pane_link["right"]):
                self._reg_view.set_files(self._session.register_files_at(self.cursor.sample))

    def _on_grid_dock_visible(self, visible: bool) -> None:
        """Render the bus grid when its tab is shown/raised (its per-tick render is skipped
        in _on_cursor while hidden). Not during playback — that path defers heavy work."""
        if visible and self._session is not None and not self._audio_playing():
            with self._as_link(self._pane_link["grid"]):
                self._update_grid_for_cursor(self.cursor.sample)

    def _refresh_measurements(self) -> None:
        """(Re)compute the Statistics rows from the whole capture, once, when marked dirty.
        capture_measurements walks the full audio store, so this is deferred until the
        Statistics tab is actually shown (and re-armed on every re-decode)."""
        if self._session is None or not self._meas_dirty:
            return
        # capture_measurements emits the Link Control section (timing + PM actions) first,
        # then Regions, Commands, Data Ports — each a collapsible header.
        rows = capture_measurements(self._session, store=self._audio_store)
        self._meas_view.set_rows(rows)
        self._link_state().meas_rows = rows          # kept, so a Link switch shows it again
        self._meas_dirty = False

    def _on_meas_dock_visible(self, visible: bool) -> None:
        """Build the Statistics table on first show / after a re-decode (deferred from load
        so a hidden Statistics tab doesn't pay for a full-capture measurement pass)."""
        if visible:
            with self._as_link(self._pane_link["right"]):
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
        dlg.setWindowTitle("Dataport Scramblers")
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
        # opening a capture). Keeps the cursor + bookmarks; a re-decode isn't a new
        # capture and shouldn't drop them.
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
        registers differ. Reached from Decode ▸ Import Visualizer CSV (a CSV file or
        the current Visualizer authoring, when its Compare action is chosen)."""
        if self._session is None:
            return
        expected = swi3score.registers_from_csv(cfg_path)
        if not expected:
            QMessageBox.warning(self, "Compare", "No registers found in that config.")
            return

        # Expected (CSV) config in the register view (blue), beside the green values
        # snooped from the bus — the visual side of the register-map comparison.
        self._reg_view.set_expected(expected)
        self._show_dock(self._reg_dock)

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
            "The expected register config is shown in blue in the Registers pane "
            "(green = snooped from the bus). Its Clear Compare button removes it.")
        if detail:
            box.setDetailedText(detail)
        box.exec()
        self._status.setText(f"Config vs decoded: {len(diffs)} register(s) differ "
                             "(expected config shown in blue)")

    def clear_comparison(self) -> None:
        if self._session is not None:
            self._refresh_grid()
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
        bound = []
        for dev in range(12):
            new_map = dlg.regmaps.get(dev)
            if new_map is not sess.peripheral_maps.get(dev):
                sess.set_peripheral_map(dev, new_map, rebuild=not will_redecode)
                bound += dlg.map_notes.get(dev, [])
        # Refresh the register view (names + maps + as-of-cursor files) + command
        # table now; a hub-depth re-decode (below) rebuilds these again via
        # load_session, but names/maps must show even when depths didn't change.
        self._reg_view.set_device_names(sess.device_names)
        self._reg_view.set_peripheral_maps(sess.peripheral_maps)
        self._reg_view.set_files(sess.register_files_at(self.cursor.sample))
        self._cmd_model.set_peripheral_maps(sess.peripheral_maps)
        if self._cmd_all:
            self._refresh_all_model()        # it draws through that model
        # Hub depths change the decode, so re-decode — but OFF the GUI thread (with
        # the busy dialog), like the scrambler-override path.
        if will_redecode:
            self._apply_hub_depths_async(new_depths)
        else:
            self._status.setText("Devices updated")
        self.show_import_warnings(bound, "register map")

    def _apply_hub_depths_async(self, new_depths: dict) -> None:
        """Re-decode with new per-device hub depths on the worker thread (busy
        dialog, responsive UI), then restore the cursor + bookmarks that load_session
        clears. Mirrors scrambler_overrides_dialog. Playback is stopped first: the
        worker mutates the live session that a running play timer would read."""
        def rehub(s=self._session, d=dict(new_depths)):
            s.set_hub_depths(d)
            return s

        self._redecode_preserving(rehub, "Hub depths applied; re-decoded.")

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
        if self._cmd_all and self._all_model is not None:
            # All Links rows are ordered by global time: bisect the cursor's instant. The
            # half-ps slack absorbs float rounding of a cursor parked exactly on a row.
            pos = self._all_model.positions_ps
            if not len(pos) or self._links.active is None:
                return
            t = self._links.active.to_ps(int(sample)) + 0.5
            idx = max(0, int(np.searchsorted(pos, t, side="right")) - 1)
        else:
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
        proxy_idx = self._cmd_proxy.mapFromSource(self._table_model().index(src_row, 0))
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
