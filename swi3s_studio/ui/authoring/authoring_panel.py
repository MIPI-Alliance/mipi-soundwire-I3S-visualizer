"""Authoring panel — a faithful Qt port of the SWI3S Visualizer's parameter UI.

Layout matches the standalone Visualizer (see its `src/ui/parameter_panel.py`):

    ┌─ Data Port Parameters ───────────────┬─ Other Parameters ─┬─ Description ──┐
    │ <param labels> │ DP0 DP1 … DP11       │ Num Columns …      │ <text>         │
    │  (numeric rows, then checkbox rows,   │ … buttons …        ├─ Notifications ┤
    │   then Sample Rate [kHz])             │ bandwidth / SSP    │ <issues list>  │
    └───────────────────────────────────────┴────────────────────┴────────────────┘

The grid canvas lives below this panel (in the Visualization page splitter). The
panel owns no placement/rendering logic: on any edit it updates the `BusConfig`
and emits `configChanged`; the main window places it through the C++ cascade.
Field labels, ranges, row order, the colored DP headers, and the action buttons
are taken verbatim from the Visualizer.
"""
from __future__ import annotations

import math
from functools import reduce
from typing import Dict, List

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QIntValidator, QPalette
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ...model.bus_config import BusConfig, NUM_DATA_PORTS
from ...analysis.issues import Issue
from ..theme import VizTheme, authoring_stylesheet
from .dialogs import (DeviceSelectorDialog, ChannelSelectorDialog, GuardSelectorDialog,
                      FlowModeSelectorDialog, PortModeSelectorDialog,
                      DisplayOptionsDialog, DataPortIdentityDialog, MANAGER)
from .notifications import NotificationsPanel

# Action buttons — the Visualizer's customtkinter blue (from the shared theme).
_BTN_CSS = (f"QPushButton {{ background:{VizTheme.ACCENT}; color:white; border:none; "
            f"border-radius:{VizTheme.CORNER_RADIUS}px; padding:5px; }} "
            f"QPushButton:hover {{ background:{VizTheme.ACCENT_HOVER}; }}")


class _ClickLineEdit(QLineEdit):
    """A read-only entry that emits `clicked` on press — used for the Device and
    Num Channels cells, which open a picker dialog like the Visualizer."""
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e) -> None:  # noqa: N802 (Qt signature)
        self.clicked.emit()
        super().mousePressEvent(e)


# Eddie's 12 data-port colours (Colors.DP_COLORS) — the DP name headers.
_DP_COLORS = ["#FF80BF", "#FFA080", "#FFFF80", "#A0FF80", "#80FFFF", "#8080FF",
              "#BF80FF", "#FFBFFF", "#FFBFBF", "#FFFFBF", "#BFFFBF", "#BFFFFF"]


# Data-port numeric rows: (label, attr, min, max). "num_channels" is special
# (the channel count → low-N-bits of EnableCh).
_NUMERIC_ROWS = [
    ("Device (0-11,Manager)", "device_number", 0, 11),
    ("Num Channels (0-16)", "num_channels", 0, 16),
    ("Sample Size (0-31)", "sample_size", 0, 31),
    ("Sample Grouping (0-7)", "sample_grouping", 0, 7),
    ("Channel Grouping (0-15)", "channel_grouping", 0, 15),
    ("Spacing (0-16)", "spacing", 0, 16),
    ("Interval (0-4095)", "interval", 0, 4095),
    ("Offset (0-4095)", "offset", 0, 4095),
    ("Horizontal Start (0-31)", "horizontal_start", 0, 31),
    ("Horizontal Count (0-31)", "horizontal_count", 0, 31),
    ("Tail Width (0-2)", "tail_width", 0, 2),
    ("Bit Width (0-2)", "bit_width", 0, 2),
    ("Skipping Numerator (0-4095)", "skipping_numerator", 0, 4095),
]

# Data-port checkbox rows: (label, mode). "direction" inverts (checked = Source);
# "bool:<attr>" is a plain boolean; "dialog:<kind>" opens the matching dialog on
# click (its checked state reflects whether the feature is on).
_CHECK_ROWS = [
    ("Source [checked] / Sink", "direction"),
    ("Guard Enable", "dialog:guard"),
    ("Sub Row Interval", "bool:sub_row_interval"),
    ("Flow Control", "dialog:flow"),
    ("Port Test Mode", "dialog:port"),
    ("Scrambler Enable", "bool:scrambler_en"),
    ("Enforce Handover", "bool:enforce_handover"),
    ("Draw Data Port", "dialog:display"),
]

# Data-port cells that open a picker dialog instead of being typed in.
_CLICK_NUMERIC = {"device_number", "num_channels"}

# Interface ("Other Parameters") rows: (label, attr, kind, min, max, phy3_only).
_IFACE_ROWS = [
    ("Num Columns (1-31)", "num_columns", "int", 1, 31, False),
    ("Skipping Denominator (1-4096)", "skipping_denominator", "int", 1, 4096, False),
    ("PHY3 Enabled", "phy3_enabled", "bool", 0, 0, False),
    ("S0 Width (1-8)", "s0_width", "int", 1, 8, True),
    ("S1 Tail Width (0-2)", "s1_tail_width", "int", 0, 2, True),
    ("Enforce S1 Handover", "enforce_s1_handover", "bool", 0, 0, True),
    ("CDS Bit Width (0-7)", "cds_bit_width", "int", 0, 7, False),
    ("CDS Guard Enabled", "cds_guard_enabled", "bool", 0, 0, False),
    ("CDS Tail Width (0-3)", "cds_tail_width", "int", 0, 3, False),
    ("Enforce CDS Handover", "enforce_cds_handover", "bool", 0, 0, False),
    ("Row Rate [kHz] (1-48000)", "row_rate_khz", "float", 1, 48000, False),
    ("Rows To Draw (1-10240)", "rows_to_draw", "int", 1, 10240, False),
]

_ENTRY_W = 48
_ROW_H = 24                # uniform row height so the frozen label column and the
                           # scrollable entry columns line up row-for-row
_HEADER_CSS = VizTheme.HEADER_CSS
# Initial height for the (internally-scrolling) Notifications panel. It is then
# tuned at runtime (_align_file_status) so the file-status note below it lines up
# with the sample-rate readouts in the middle column, independent of font metrics.
_NOTIF_H = 120


class AuthoringPanel(QWidget):
    configChanged = Signal()
    issueActivated = Signal(list)
    loadInitRequested = Signal()
    resetRequested = Signal()
    loadRequested = Signal()
    saveRequested = Signal()
    saveSvgRequested = Signal()
    exportJsonRequested = Signal()
    maximizeRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._cfg = BusConfig()   # start in the reset (empty) state — no ports enabled
        self._loading = False
        self.setStyleSheet(authoring_stylesheet())   # match the old Visualizer look

        # widget stores
        self._dp_name: List[QLineEdit] = []
        self._dp_num: Dict[str, List[QLineEdit]] = {a: [] for _, a, _, _ in _NUMERIC_ROWS}
        self._dp_check: Dict[str, List[QCheckBox]] = {m: [] for _, m in _CHECK_ROWS}
        self._dp_rate: List[QLabel] = []
        self._iface: Dict[str, QWidget] = {}
        self._action_btns: List[QPushButton] = []   # re-styled on theme switch
        # arrow-key navigation grid: widget -> (row, col) + the reverse list
        self._nav_pos: Dict[QLineEdit, tuple] = {}
        self._nav_list: List[tuple] = []

        root = QHBoxLayout(self)
        root.addWidget(self._build_dp_panel(), 1)
        root.addWidget(self._build_other_panel())
        root.addWidget(self._build_side_panel())

        self._populate()

    # ---- arrow-key navigation between entry boxes (port of the Visualizer's
    # _wire_vertical_nav): Up/Down jump to the nearest entry in the same column;
    # Left/Right jump within the row, but only at the text edge with no selection
    # so normal cursor movement still works inside a field. ----
    def _register_nav(self, w: QLineEdit, row: int, col: int) -> None:
        self._nav_pos[w] = (row, col)
        self._nav_list.append((row, col, w))
        w.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt signature)
        if event.type() == QEvent.KeyPress and obj in self._nav_pos:
            key = event.key()
            if key == Qt.Key_Up:
                self._nav_move(obj, "v", -1); return True
            if key == Qt.Key_Down:
                self._nav_move(obj, "v", 1); return True
            if key == Qt.Key_Left and self._nav_move(obj, "h", -1):
                return True
            if key == Qt.Key_Right and self._nav_move(obj, "h", 1):
                return True
        return super().eventFilter(obj, event)

    def _nav_move(self, w: QLineEdit, axis: str, direction: int) -> bool:
        row, col = self._nav_pos[w]
        if axis == "h":
            if w.hasSelectedText():
                return False                      # let default clear the selection
            pos, length = w.cursorPosition(), len(w.text())
            at_edge = (direction < 0 and pos == 0) or (direction > 0 and pos == length)
            if not at_edge:
                return False                      # normal cursor movement in-field
            cands = [(rr, cc, e) for rr, cc, e in self._nav_list
                     if rr == row and (cc - col) * direction > 0]
            key = lambda p: abs(p[1] - col)
        else:                                     # vertical: same column
            cands = [(rr, cc, e) for rr, cc, e in self._nav_list
                     if cc == col and (rr - row) * direction > 0]
            key = lambda p: abs(p[0] - row)
        if cands:
            tgt = min(cands, key=key)[2]
            tgt.setFocus()
            tgt.selectAll()
            return True
        return axis == "v"                        # always consume Up/Down

    # ---- left: Data Port Parameters ----
    def _build_dp_panel(self) -> QWidget:
        # The parameter-name column is FROZEN: it sits in a fixed widget to the
        # left of a horizontally-scrolling area that holds the 12 DP entry
        # columns. An outer vertical scroll moves both together vertically, so
        # the names stay visible while you scroll the data-port boxes sideways.
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)

        label_grid = QGridLayout()
        label_grid.setHorizontalSpacing(2)
        label_grid.setVerticalSpacing(2)
        label_grid.setContentsMargins(0, 0, 0, 0)
        label_host = QWidget()
        label_host.setLayout(label_grid)

        entry_grid = QGridLayout()
        entry_grid.setHorizontalSpacing(2)
        entry_grid.setVerticalSpacing(2)
        entry_grid.setContentsMargins(0, 0, 0, 0)
        entry_host = QWidget()
        entry_host.setLayout(entry_grid)

        def add_label(text: str, row: int) -> None:
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignCenter)
            lab.setFixedHeight(_ROW_H)
            label_grid.addWidget(lab, row, 0)

        # header row: title in the corner (above the parameter-name column,
        # beside the DP name chips — matches the Visualizer) + the DP chips.
        corner = QLabel("Data Port Parameters")
        corner.setStyleSheet(_HEADER_CSS)
        corner.setAlignment(Qt.AlignCenter)
        corner.setFixedHeight(_ROW_H)
        label_grid.addWidget(corner, 0, 0)
        for c in range(NUM_DATA_PORTS):
            e = _ClickLineEdit()
            e.setAlignment(Qt.AlignCenter)
            e.setFixedWidth(_ENTRY_W)
            e.setFixedHeight(_ROW_H)
            e.setStyleSheet(
                f"background:{_DP_COLORS[c]}; color:black; "
                f"border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
                f"border-radius:{VizTheme.CORNER_RADIUS}px;")
            e.clicked.connect(lambda col=c: self._open_dp_dialog("identity", col))
            entry_grid.addWidget(e, 0, c)
            self._dp_name.append(e)
            self._register_nav(e, 0, c)

        r = 1
        # numeric rows (Device + Num Channels are click-to-open-dialog cells)
        for label, attr, lo, hi in _NUMERIC_ROWS:
            add_label(label, r)
            for c in range(NUM_DATA_PORTS):
                if attr in _CLICK_NUMERIC:
                    e = _ClickLineEdit()
                    e.setFixedWidth(_ENTRY_W)
                    kind = "device" if attr == "device_number" else "channels"
                    e.clicked.connect(lambda k=kind, col=c: self._open_dp_dialog(k, col))
                else:
                    e = QLineEdit()
                    e.setAlignment(Qt.AlignCenter)
                    e.setFixedWidth(_ENTRY_W)
                    e.setValidator(QIntValidator(lo, hi, self))
                    e.editingFinished.connect(self._on_edit)
                e.setFixedHeight(_ROW_H)
                entry_grid.addWidget(e, r, c)
                self._dp_num[attr].append(e)
                self._register_nav(e, r, c)
            r += 1
        # checkbox rows (some open a dialog on click)
        for label, mode in _CHECK_ROWS:
            add_label(label, r)
            for c in range(NUM_DATA_PORTS):
                cb = QCheckBox()
                if mode.startswith("dialog:"):
                    kind = mode.split(":", 1)[1]
                    cb.clicked.connect(lambda checked=False, k=kind, col=c:
                                       self._open_dp_dialog(k, col, checked))
                else:
                    cb.toggled.connect(self._on_edit)
                cell = QWidget()
                cell.setFixedHeight(_ROW_H)
                h = QHBoxLayout(cell); h.setContentsMargins(0, 0, 0, 0)
                # Symmetric stretches centre the checkbox in its column, so it lines
                # up with the (centred) text entries above it.
                h.addStretch(1); h.addWidget(cb); h.addStretch(1)
                entry_grid.addWidget(cell, r, c)
                self._dp_check[mode].append(cb)
            r += 1
        # computed sample-rate row
        add_label("Sample Rate [kHz]", r)
        for c in range(NUM_DATA_PORTS):
            v = QLabel("0.0")
            v.setAlignment(Qt.AlignCenter)
            v.setFixedHeight(_ROW_H)
            entry_grid.addWidget(v, r, c)
            self._dp_rate.append(v)
        # trailing stretch keeps rows top-aligned in both grids
        label_grid.setRowStretch(r + 1, 1)
        entry_grid.setRowStretch(r + 1, 1)
        # A trailing stretch COLUMN absorbs any extra width when the panel is wider
        # than the 12 fixed-width DP columns; without it the grid spreads the extra
        # across the columns, so the centred checkboxes drift right of the (fixed-
        # width) text entries above them.
        entry_grid.setColumnStretch(NUM_DATA_PORTS, 1)

        # Horizontal scroll for the 12 DP entry columns; the frozen label column
        # stays put. No visible scrollbars — a trackpad/mouse horizontal swipe still
        # scrolls the columns, and the panel is sized to its content height so every
        # row is always visible (the vertical scroll that only existed to expose the
        # horizontal bar is gone).
        n_rows = 1 + len(_NUMERIC_ROWS) + len(_CHECK_ROWS) + 1
        content_h = n_rows * (_ROW_H + 2)
        inner = QScrollArea()
        inner.setWidgetResizable(True)
        inner.setFrameShape(QFrame.NoFrame)
        inner.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner.setWidget(entry_host)
        inner.setFixedHeight(content_h)

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(label_host)
        bl.addWidget(inner, 1)

        outer.addWidget(body)
        outer.addStretch(1)               # keep the rows top-aligned if over-sized
        # The panel is at least its content tall so no row is clipped without a
        # scrollbar; the splitter can still collapse it to 0 for "Maximize Frame".
        host.setMinimumHeight(content_h)
        return host

    # ---- middle: Other Parameters + buttons + readouts ----
    def _build_other_panel(self) -> QWidget:
        host = QWidget()
        host.setFixedWidth(320)
        col = QVBoxLayout(host)

        hdr = QLabel("Other Parameters")
        hdr.setStyleSheet(_HEADER_CSS)
        hdr.setAlignment(Qt.AlignCenter)
        col.addWidget(hdr)

        form = QGridLayout()
        form.setVerticalSpacing(3)
        for i, (label, attr, kind, lo, hi, _phy3) in enumerate(_IFACE_ROWS):
            lab = QLabel(label)
            lab.setAlignment(Qt.AlignCenter)
            form.addWidget(lab, i, 0)
            if kind == "bool":
                w = QCheckBox()
                w.toggled.connect(self._on_iface_edit)
                # centre the checkbox within the same 64px band the text boxes
                # occupy, so the value column lines up.
                cell = QWidget()
                cell.setFixedWidth(64)
                ch = QHBoxLayout(cell); ch.setContentsMargins(0, 0, 0, 0)
                ch.addStretch(1); ch.addWidget(w); ch.addSpacing(10); ch.addStretch(1)
                form.addWidget(cell, i, 1, alignment=Qt.AlignRight)
            else:
                w = QLineEdit()
                w.setAlignment(Qt.AlignCenter)
                w.setFixedWidth(64)
                w.setValidator(QDoubleValidator(float(lo), float(hi), 3, self)
                               if kind == "float" else QIntValidator(lo, hi, self))
                w.editingFinished.connect(self._on_iface_edit)
                form.addWidget(w, i, 1, alignment=Qt.AlignRight)
            self._iface[attr] = (lab, w)
        # Derived readouts share the form's value column so the numbers line up
        # directly under the text boxes / checkboxes.
        self._bw_value = QLabel("0")
        self._ssp_value = QLabel("1")
        base = len(_IFACE_ROWS)
        for off, (text, val) in enumerate(
                (("Raw Bus Bandwidth [Mbps]", self._bw_value),
                 ("System SSP Interval [Rows]", self._ssp_value))):
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignCenter)
            form.addWidget(lab, base + off, 0)
            val.setAlignment(Qt.AlignCenter)
            val.setFixedWidth(64)
            form.addWidget(val, base + off, 1, alignment=Qt.AlignRight)
        # The readouts are bare labels (shorter than the entry rows), so their
        # mutual gap looks tighter than the rows above. Pin both rows to the entry
        # row height so the spacing is uniform.
        row_h = max((w.sizeHint().height() for _l, w in self._iface.values()
                     if isinstance(w, QLineEdit)), default=24)
        form.setRowMinimumHeight(base, row_h)
        form.setRowMinimumHeight(base + 1, row_h)
        col.addLayout(form)

        col.addSpacing(6)
        btns = QVBoxLayout()
        btns.setSpacing(2)
        for label, sig in (("Load Init (CSV)", self.loadInitRequested),
                           ("Reset", self.resetRequested),
                           ("Load Settings (CSV)", self.loadRequested),
                           ("Save Settings (CSV)", self.saveRequested),
                           ("Save Output (SVG)", self.saveSvgRequested),
                           ("Save Output (JSON)", self.exportJsonRequested),
                           ("Maximize Frame", self.maximizeRequested)):
            b = QPushButton(label)
            b.clicked.connect(sig.emit)
            b.setFixedWidth(180)
            b.setStyleSheet(_BTN_CSS)
            self._action_btns.append(b)
            btns.addWidget(b, alignment=Qt.AlignHCenter)
        col.addLayout(btns)
        col.addStretch(1)
        return host

    def set_file_status(self, text: str) -> None:
        self._file_status.setText(text)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        super().showEvent(event)
        # Real font/row metrics are only known once shown; defer one tick so the
        # layout has settled, then align the file note with the readouts.
        QTimer.singleShot(0, self._align_file_status)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        super().resizeEvent(event)
        # The panel gets its real size from the splitter after the first show, and
        # again whenever it's dragged; re-align (a no-op once already aligned).
        QTimer.singleShot(0, self._align_file_status)

    def _align_file_status(self) -> None:
        """Size the Notifications panel so the file-status note's top lines up with
        the "Sample Rate [kHz]" row at the bottom of the Data Port Parameters (left)
        column. Uses live geometry (independent of font metrics / DPI) and computes
        the absolute target height from stable anchors — the notifications panel's
        top (which doesn't depend on its own height) plus the layout spacing below
        it — so re-running is idempotent (no incremental drift on repeat calls)."""
        ref = self._dp_rate[0] if self._dp_rate else None
        notif = self._notifications
        if ref is None or not (self.isVisible() and ref.isVisible()):
            return
        self._side_col.activate()        # flush any pending layout so tops are current
        target = ref.mapTo(self, ref.rect().topLeft()).y()       # Sample Rate row top
        notif_top = notif.mapTo(self, notif.rect().topLeft()).y()  # stable vs notif height
        gap = max(0, self._side_col.spacing())   # spacing between notif and the note
        needed = max(0, target - notif_top - gap)
        if abs(needed - notif.height()) > 1:
            notif.setFixedHeight(needed)

    def commit_edits(self) -> None:
        """Flush all in-progress widget edits into the config (e.g. a DP name still
        focused when Save is clicked — a styled button may not fire editingFinished)."""
        self._on_edit()
        self._on_iface_edit()
        self._cfg.description = self._desc.toPlainText()

    # ---- right: Description + Notifications ----
    def _build_side_panel(self) -> QWidget:
        host = QWidget()
        host.setFixedWidth(300)
        col = QVBoxLayout(host)
        self._side_col = col

        dh = QLabel("Description"); dh.setStyleSheet(_HEADER_CSS); dh.setAlignment(Qt.AlignCenter)
        col.addWidget(dh)
        self._desc = QPlainTextEdit()
        self._desc.setPlaceholderText("Enter a description to be saved/recalled here.")
        self._desc.setMaximumHeight(120)
        # The panel-wide `QWidget` background rule would otherwise paint the text
        # area's viewport with the frame colour, leaving the interior darker than
        # the entries; pin the viewport to the entry fill so it matches.
        self._desc.viewport().setStyleSheet(f"background:{VizTheme.ENTRY_BG};")
        # Match the description's text + placeholder to the other label text (not a
        # dimmer grey).
        dpal = self._desc.palette()
        dpal.setColor(QPalette.Text, QColor(VizTheme.TEXT))
        dpal.setColor(QPalette.PlaceholderText, QColor(VizTheme.TEXT))
        self._desc.setPalette(dpal)
        self._desc.textChanged.connect(self._on_desc_changed)
        col.addWidget(self._desc)

        nh = QLabel("Notifications"); nh.setStyleSheet(_HEADER_CSS); nh.setAlignment(Qt.AlignCenter)
        col.addWidget(nh)
        self._notifications = NotificationsPanel()
        self._notifications.issueActivated.connect(self.issueActivated)
        # Shorten the panel (it scrolls internally) so the file-status note below it
        # lands level with the "Sample Rate [kHz]" row at the bottom of the left
        # column. The height is tuned at runtime by _align_file_status (font metrics
        # aren't known until shown), and re-tuned on resize.
        self._notifications.setFixedHeight(_NOTIF_H)
        col.addWidget(self._notifications)
        # Last loaded / saved file, shown like the Visualizer's file status — under
        # Notifications (not under the buttons, which cost the grid vertical space).
        self._file_status = QLabel("")
        self._file_status.setAlignment(Qt.AlignCenter)
        self._file_status.setWordWrap(True)
        self._file_status.setStyleSheet(f"color:{VizTheme.TEXT};")
        col.addWidget(self._file_status)
        col.addStretch(1)
        return host

    # ---- public API ----
    def config(self) -> BusConfig:
        return self._cfg

    def set_config(self, cfg: BusConfig) -> None:
        self._cfg = cfg
        self._populate()
        self.configChanged.emit()

    def set_issues(self, issues: List[Issue]) -> None:
        self._notifications.set_issues(issues)

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch. Re-applying the panel
        stylesheet re-themes every cascaded child (labels, entries, checkboxes); the
        few widgets with their own inline style (the DP-name colour chips, the action
        buttons, the description's pinned viewport/palette, the file-status note) and
        the Notifications sub-panel are refreshed explicitly."""
        global _BTN_CSS
        _BTN_CSS = (f"QPushButton {{ background:{VizTheme.ACCENT}; color:white; border:none; "
                    f"border-radius:{VizTheme.CORNER_RADIUS}px; padding:5px; }} "
                    f"QPushButton:hover {{ background:{VizTheme.ACCENT_HOVER}; }}")
        self.setStyleSheet(authoring_stylesheet())
        for c, e in enumerate(self._dp_name):
            e.setStyleSheet(
                f"background:{_DP_COLORS[c]}; color:black; "
                f"border:{VizTheme.BORDER_WIDTH}px solid {VizTheme.BORDER}; "
                f"border-radius:{VizTheme.CORNER_RADIUS}px;")
        for b in self._action_btns:
            b.setStyleSheet(_BTN_CSS)
        self._desc.viewport().setStyleSheet(f"background:{VizTheme.ENTRY_BG};")
        dpal = self._desc.palette()
        dpal.setColor(QPalette.Text, QColor(VizTheme.TEXT))
        dpal.setColor(QPalette.PlaceholderText, QColor(VizTheme.TEXT))
        self._desc.setPalette(dpal)
        self._file_status.setStyleSheet(f"color:{VizTheme.TEXT};")
        self._notifications.retheme()

    # ---- populate widgets from cfg ----
    def _populate(self) -> None:
        self._loading = True
        for c in range(NUM_DATA_PORTS):
            dp = self._cfg.dataports[c]
            self._dp_name[c].setText(self._chip_text(dp, c))
            self._dp_name[c].setToolTip(
                f"Data port {dp.number(c)}" + (f" — {dp.name}" if dp.name else "")
                + "\nClick to edit number / name")
            for _, attr, _, _ in _NUMERIC_ROWS:
                if attr == "num_channels":
                    text = str(dp.num_channels())
                elif attr == "device_number":
                    text = "M" if dp.device_number == MANAGER else str(dp.device_number)
                else:
                    text = str(int(getattr(dp, attr)))
                self._dp_num[attr][c].setText(text)
            for _, mode in _CHECK_ROWS:
                self._dp_check[mode][c].setChecked(self._check_state(dp, mode))
        for attr, (lab, w) in self._iface.items():
            val = getattr(self._cfg, attr)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(val))
            elif attr == "row_rate_khz":
                w.setText(f"{val:g}")
            else:
                w.setText(str(int(val)))
        self._desc.setPlainText(self._cfg.description)
        self._loading = False
        self._update_phy3_enabled()
        self._update_derived()

    @staticmethod
    def _chip_text(dp, c: int) -> str:
        """DP header chip label: number + optional name, e.g. 'DP17' or '17·MicL'."""
        n = dp.number(c)
        return f"{n}·{dp.name}" if dp.name else f"DP{n}"

    @staticmethod
    def _check_state(dp, mode: str) -> bool:
        if mode == "direction":
            return not dp.port_direction          # checked = Source
        if mode == "dialog:guard":
            return dp.guard_enable
        if mode == "dialog:flow":
            return dp.flow_mode != 0
        if mode == "dialog:port":
            return dp.port_mode != 0
        if mode == "dialog:display":
            return dp.enabled
        return bool(getattr(dp, mode.split(":", 1)[1]))

    # ---- edits -> cfg ----
    def _on_edit(self, *_a) -> None:
        if self._loading:
            return
        for c in range(NUM_DATA_PORTS):
            dp = self._cfg.dataports[c]
            # Name + dp_number are set via the identity dialog (read-only chip).
            for _, attr, _, _ in _NUMERIC_ROWS:
                if attr in _CLICK_NUMERIC:           # set via picker dialogs
                    continue
                setattr(dp, attr, int(self._dp_num[attr][c].text() or 0))
            for _, mode in _CHECK_ROWS:
                if mode.startswith("dialog:"):       # set via dialogs
                    continue
                self._apply_check(dp, mode, self._dp_check[mode][c].isChecked())
        self._update_derived()
        self.configChanged.emit()

    @staticmethod
    def _apply_check(dp, mode: str, checked: bool) -> None:
        if mode == "direction":
            dp.port_direction = not checked       # checked = Source
        else:
            setattr(dp, mode.split(":", 1)[1], checked)

    def _open_dp_dialog(self, kind: str, c: int, checked: bool | None = None) -> None:
        """Open the per-DP picker dialog for the Visualizer-style controls
        (Device, Num Channels, Guard, Flow Control, Port Test Mode, Draw). For the
        Draw checkbox, the click also sets `enabled` from the checkbox state (the
        Visualizer's checkbox is bound to `enabled` and opens the dialog on top)."""
        dp = self._cfg.dataports[c]
        if kind == "identity":
            dlg = DataPortIdentityDialog(self, dp.number(c), dp.name, c)
            if dlg.exec():
                prev = dp.dp_number
                dp.dp_number = dlg.dp_number
                # Enforce (device, dp_number) uniqueness; a device may not reuse a
                # number. duplicate_dp_numbers() returns every slot in a colliding
                # group, so this catches the clash regardless of which slot was
                # edited. Revert the number first (keep the name), then warn — so the
                # message reports the kept (reverted) number, not the rejected one.
                if c in self._cfg.duplicate_dp_numbers():
                    dp.dp_number = prev
                    dev = "Manager" if dp.device_number == MANAGER else f"Device {dp.device_number}"
                    QMessageBox.warning(
                        self, "Duplicate data-port number",
                        f"{dev} already uses data-port number {dlg.dp_number}. "
                        f"Numbers must be unique within a device — keeping {dp.number(c)}.")
                dp.name = dlg.name
        elif kind == "device":
            dlg = DeviceSelectorDialog(self, dp.device_number, c)
            if dlg.exec():
                dp.device_number = dlg.device
        elif kind == "channels":
            dlg = ChannelSelectorDialog(self, dp.enable_ch, c)
            if dlg.exec():
                dp.enable_ch = dlg.result_mask
        elif kind == "guard":
            dlg = GuardSelectorDialog(self, dp.guard_enable,
                                      1 if dp.guard_polarity else 0, c)
            if dlg.exec():
                dp.guard_enable = dlg.guard_enabled
                dp.guard_polarity = bool(dlg.guard_polarity)
        elif kind == "flow":
            dlg = FlowModeSelectorDialog(
                self, dp.flow_mode, c, dp.fcp_horizontal_start, dp.fcp_bit_width,
                dp.fcp_tail_width, dp.fcp_offset, dp.fcp_guard_enable,
                1 if dp.fcp_guard_polarity else 0)
            if dlg.exec():
                dp.flow_mode = dlg.flow_mode
                dp.fcp_horizontal_start = dlg.fcp_h_start
                dp.fcp_bit_width = dlg.fcp_bit_width
                dp.fcp_tail_width = dlg.fcp_tail_width
                dp.fcp_offset = dlg.fcp_offset
                dp.fcp_guard_enable = dlg.fcp_guard_enable
                dp.fcp_guard_polarity = bool(dlg.fcp_guard_polarity)
        elif kind == "port":
            dlg = PortModeSelectorDialog(self, dp.port_mode, c)
            if dlg.exec():
                dp.port_mode = dlg.port_mode
        elif kind == "display":
            # Clicking the checkbox only *enables* Draw (turning it off is done in
            # the dialog, matching the Visualizer); the dialog then refines it.
            if checked is not None:
                dp.enabled = True
            dlg = DisplayOptionsDialog(self, dp.enabled, dp.display_fields, c)
            if dlg.exec():
                dp.enabled = dlg.enabled
                dp.display_fields = dlg.display_fields
        # Re-sync widgets to the model (reverts the checkbox toggle if cancelled)
        # and re-render.
        self._populate()
        self.configChanged.emit()

    def _on_iface_edit(self, *_a) -> None:
        if self._loading:
            return
        # A blanked field commits its row MINIMUM, not 0 — several interface rows
        # have a min > 0 (num_columns, skipping_denominator, row_rate_khz, …) and 0
        # would be out of range (and can divide-by-zero downstream).
        mins = {a: lo for _l, a, kind, lo, _h, _p in _IFACE_ROWS if kind != "bool"}
        for attr, (lab, w) in self._iface.items():
            if isinstance(w, QCheckBox):
                setattr(self._cfg, attr, w.isChecked())
            elif attr == "row_rate_khz":
                txt = w.text()
                try:
                    setattr(self._cfg, attr, float(txt) if txt else float(mins.get(attr, 0)))
                except ValueError:
                    pass
            else:
                txt = w.text()
                try:
                    setattr(self._cfg, attr, int(txt) if txt else int(mins.get(attr, 0)))
                except ValueError:
                    pass                              # ignore a QIntValidator 'Intermediate' value
        self._update_phy3_enabled()
        self._update_derived()
        self.configChanged.emit()

    def _on_desc_changed(self) -> None:
        if self._loading:
            return
        self._cfg.description = self._desc.toPlainText()

    # ---- PHY3-dependent enable + derived readouts ----
    def _update_phy3_enabled(self) -> None:
        on = self._cfg.phy3_enabled
        for label, attr, kind, lo, hi, phy3 in _IFACE_ROWS:
            if phy3:
                lab, w = self._iface[attr]
                w.setEnabled(on)
                lab.setEnabled(on)

    def _update_derived(self) -> None:
        # Raw bus bandwidth [Mbps] = row rate (kHz) × columns / 1000.
        self._bw_value.setText(
            f"{self._cfg.row_rate_khz * self._cfg.column_count() / 1000:g}")
        # System SSP interval [rows] = LCM of each enabled DP's interval (rows).
        intervals = [dp.interval + 1 for dp in self._cfg.dataports if dp.enabled]
        ssp = reduce(math.lcm, intervals, 1) if intervals else 1
        self._ssp_value.setText(str(ssp))
        # Per-DP sample rate.
        for c in range(NUM_DATA_PORTS):
            self._dp_rate[c].setText(self._sample_rate(self._cfg.dataports[c]))

    def _sample_rate(self, dp) -> str:
        rr = self._cfg.row_rate_khz
        if not dp.sub_row_interval:
            base = (dp.sample_grouping + 1) * rr / (dp.interval + 1)
            if dp.skipping_numerator and self._cfg.skipping_denominator:
                base *= (self._cfg.skipping_denominator - dp.skipping_numerator) \
                        / self._cfg.skipping_denominator
            return f"{base:.1f}"
        # SRI: transports repeat within the horizontal window each row.
        nch = dp.num_channels()
        txp = 1 if dp.flow_mode in (1, 3) else 0
        tw = nch * (dp.sample_size + 1 + txp) * (dp.sample_grouping + 1) * (dp.bit_width + 1)
        avail = dp.horizontal_count + 1
        if tw == 0 or avail < tw:
            groups = 0
        elif dp.spacing == 0:
            groups = 1
        else:
            gap = dp.spacing - 1
            groups = (avail + gap) // (tw + gap)
        if groups <= 0:
            return "Error"
        return f"{groups * (dp.sample_grouping + 1) * rr:.1f}"
