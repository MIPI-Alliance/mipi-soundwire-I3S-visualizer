"""Decoded-sample viewer: a filterable table of the reconstructed audio samples.

Each row is one decoded sample with its MSB bus row, the port that carried it,
and the value in binary / hex / (signed) decimal. Samples are windowed around the
shared cursor (like the CDS symbol viewer) so the table stays light on multi-
million-sample mic-array captures; clicking a row seeks the shared cursor to that
sample's MSB so all views stay in sync.

A filter bar narrows the stream by **data port** and by a **value predicate**
(e.g. Decimal ≤ 0). Filtering is applied vectorised over the whole capture in the
session (not just the visible window), so it stays cheap while still surfacing
matching samples wherever they are.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QTableWidget, QTableWidgetItem, QToolButton,
                               QVBoxLayout, QWidget)

from .row_origin import RowOriginMixin
from .theme import VizTheme, analyzer_stylesheet

_COLS = ["Row", "Time (µs)", "Port", "Binary", "Hex", "Decimal"]
_OPS = ["—", "≤", "<", "=", "≠", "≥", ">"]        # "—" = no value filter
_OP_MAP = {"≤": "<=", "<": "<", "=": "==", "≠": "!=", "≥": ">=", ">": ">"}


class _KeepOpenMenu(QMenu):
    """A menu that stays open while its checkable items are toggled (so several
    values can be picked in one go), like the command-table filter menus."""

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 (Qt signature)
        act = self.activeAction()
        if act is not None and act.isCheckable() and act.isEnabled():
            act.trigger()                    # toggle + emit; keep the menu open
            return
        super().mouseReleaseEvent(e)


class _MultiSelect(QToolButton):
    """A compact dropdown of checkable items for multi-selecting a filter dimension
    (data port / channel). `selected()` returns the checked item data; an empty list
    means 'nothing checked' which the owner treats as 'all'. Emits `changed` on any
    toggle."""

    changed = Signal()

    def __init__(self, all_label: str) -> None:
        super().__init__()
        self._all = all_label
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._menu = _KeepOpenMenu(self)
        self.setMenu(self._menu)
        self.setText(all_label)

    def set_items(self, items) -> None:
        """items: list of (label, data). Preserves selection for data still present."""
        prev = set(self.selected())
        self._menu.clear()
        for label, data in items:
            act = QAction(label, self._menu)
            act.setCheckable(True)
            act.setData(data)
            act.setChecked(data in prev)
            act.toggled.connect(self._on_toggle)
            self._menu.addAction(act)
        self._update_text()

    def _on_toggle(self, _on: bool) -> None:
        self._update_text()
        self.changed.emit()

    def selected(self) -> list:
        return [a.data() for a in self._menu.actions() if a.isChecked()]

    def _update_text(self) -> None:
        sel = self.selected()
        self.setText(self._all if not sel else f"{len(sel)} selected")


def _binary(value: int, bits: int) -> str:
    bits = max(1, int(bits))
    b = format(int(value) & ((1 << bits) - 1), f"0{bits}b")
    return " ".join(b[max(0, len(b) - i - 4):len(b) - i]
                    for i in range(len(b) - 4, -4, -4)).strip() or b


def _hex(value: int, bits: int) -> str:
    nyb = max(1, (max(1, int(bits)) + 3) // 4)
    return f"0x{int(value) & ((1 << max(1, int(bits))) - 1):0{nyb}X}"


class DecodedSampleView(RowOriginMixin, QWidget):
    # Emitted with a sample's MSB start_sample when the user selects a row.
    sampleSelected = Signal('qlonglong')
    # Emitted when the port / value filter changes (the owner re-queries + repopulates).
    filtersChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(analyzer_stylesheet())
        self._rate = 0.0
        self._samples: List[dict] = []
        self._lane_colors = {}                 # (device, dp, channel) -> QColor

        # Filter bar: multi-select port + channel + value predicate (op + number).
        self._port_cb = _MultiSelect("All ports")
        self._port_cb.changed.connect(self.filtersChanged.emit)
        self._chan_cb = _MultiSelect("All ch")
        self._chan_cb.changed.connect(self.filtersChanged.emit)
        self._op_cb = QComboBox()
        self._op_cb.addItems(_OPS)
        self._op_cb.setMinimumContentsLength(3)
        self._op_cb.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._op_cb.setMinimumWidth(60)                 # wide enough to show ≤ ≥ ≠ + arrow
        self._op_cb.currentIndexChanged.connect(lambda _i: self.filtersChanged.emit())
        self._val_edit = QLineEdit()
        self._val_edit.setPlaceholderText("value")
        self._val_edit.setFixedWidth(90)
        self._val_edit.editingFinished.connect(self.filtersChanged.emit)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.addWidget(QLabel("Port:"))
        bar.addWidget(self._port_cb)
        bar.addWidget(QLabel("Ch:"))
        bar.addWidget(self._chan_cb)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Decimal"))
        bar.addWidget(self._op_cb)
        bar.addWidget(self._val_edit)
        bar.addStretch(1)

        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._on_select)
        from .copyable import enable_copy
        enable_copy(self._table, multi=True)      # ⌘/Ctrl+C copies selected sample rows (TSV)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self._table)

    # ---- configuration ----
    def set_sample_rate(self, rate_hz: float) -> None:
        self._rate = float(rate_hz or 0.0)

    def set_lanes(self, lanes) -> None:
        """Per-(device, dp, channel) colours (same order as the Raw Capture / Audio
        views) so the Port column matches those overlays — and populate the Port /
        Channel filter selectors from the same lane list."""
        lanes = [(int(d), int(p), int(ch)) for d, p, ch in (lanes or [])]
        palette = VizTheme.TRACE_PALETTE
        self._lane_colors = {key: QColor(palette[i % len(palette)])
                             for i, key in enumerate(lanes)}
        seen = []
        for d, p, _ch in lanes:
            if (d, p) not in seen:
                seen.append((d, p))
        self._port_cb.set_items([(f"Dev{d} DP{p}", (d, p)) for d, p in seen])
        self._chan_cb.set_items([(f"CH{c}", c) for c in sorted({ch for _d, _p, ch in lanes})])

    def filters(self):
        """Current (ports, channels, value_pred): ports is a list of (device, dp) or
        None (all); channels is a list of channel numbers or None (all); value_pred is
        (op, threshold) or None. Consumed by Session.samples_around."""
        ports = self._port_cb.selected() or None
        channels = self._chan_cb.selected() or None
        pred = None
        op = _OP_MAP.get(self._op_cb.currentText())
        txt = self._val_edit.text().strip()
        if op and txt:
            try:
                pred = (op, int(txt, 0))
            except ValueError:
                pred = None
        return ports, channels, pred

    def retheme(self) -> None:
        self.setStyleSheet(analyzer_stylesheet())
        self.set_lanes(list(self._lane_colors))
        self.set_samples(self._samples)

    # ---- data ----
    def set_samples(self, samples: List[dict]) -> None:
        self._samples = list(samples)
        t = self._table
        t.blockSignals(True)
        try:
            t.setRowCount(len(self._samples))
            for r, s in enumerate(self._samples):
                bits = int(s.get("sample_size", 0))
                val = int(s.get("value", 0))
                start = int(s.get("start_sample", 0))
                t_us = f"{start / self._rate * 1e6:,.2f}" if self._rate else "—"
                dev, dp, ch = int(s.get("device", -1)), int(s.get("dp", -1)), int(s.get("channel", 0))
                cells = [f"{self.display_row(s.get('row', 0)):,}",
                         t_us, f"Dev{dev} DP{dp} CH{ch}",
                         _binary(val, bits), _hex(val, bits), f"{int(s.get('signed', 0)):,}"]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if c == 2:
                        col = self._lane_colors.get((dev, dp, ch))
                        if col is not None:
                            item.setForeground(col)
                    t.setItem(r, c, item)
            t.resizeColumnsToContents()
        finally:
            t.blockSignals(False)             # always restore, even on error

    def _on_select(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if rows and 0 <= rows[0].row() < len(self._samples):
            self.sampleSelected.emit(int(self._samples[rows[0].row()].get("start_sample", 0)))

    def select_sample(self, sample: int) -> None:
        """Select the sample whose MSB is at/just before `sample` and scroll it into
        view — programmatic, so it must not echo back as a user selection."""
        if not self._samples:
            return
        idx = 0
        for i, s in enumerate(self._samples):
            if int(s["start_sample"]) <= sample:
                idx = i
            else:
                break
        t = self._table
        t.blockSignals(True)
        t.selectRow(idx)
        t.blockSignals(False)
        item = t.item(idx, 0)
        if item is not None:
            t.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def shown_span(self) -> Optional[Tuple[int, int]]:
        """(first, last) start_sample currently displayed, or None if empty — lets
        the owner skip a rebuild while the cursor stays within the shown window."""
        if not self._samples:
            return None
        return (int(self._samples[0]["start_sample"]),
                int(self._samples[-1]["start_sample"]))

    @property
    def sample_count(self) -> int:
        return self._table.rowCount()
