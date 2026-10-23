"""Registers ▸ right-click a register ▸ History…: every bus access to that register.

One row per write or read, in capture order: when (µs and bus row), which access, the byte
value it carried, whether the register model applied it, and what went wrong if anything did
(analysis.register_history). Failed attempts are tinted as the Commands table tints its error
rows and can be shown alone. Double-click a row to put the cursor on that command.

Not modal: the main window stays usable while it is open, and each register's history is its
own window.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..analysis.register_history import RegisterEvent
from .copyable import enable_copy
from .theme import VizTheme, analyzer_stylesheet

_HEADERS = ["Time (µs)", "Row", "Access", "Value", "Applied", "Status", "Response"]


class RegisterHistoryDialog(QDialog):
    """The events of one register; `jump(start_sample)` asks the window to move the cursor.
    `session` is the Session the events were read from, and `device` / `address` the
    register, kept for the window to match the dialog to its Link and to re-read the events
    after a re-decode (`set_events`); the dialog itself does not read them."""
    jump = Signal(int)

    def __init__(self, title: str, events: List[RegisterEvent], sample_rate_hz: float,
                 display_row: Callable[[int], int], parent=None, session: Any = None,
                 device: Optional[int] = None, address: Optional[int] = None) -> None:
        super().__init__(parent)
        self.session = session
        self.device = device
        self.address = address
        self._title = title
        self.setWindowTitle(f"History — {title}")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(analyzer_stylesheet())
        self._events: List[RegisterEvent] = []
        self._rate = float(sample_rate_hz) or 1.0
        self._display_row = display_row

        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        self._summary = QLabel()
        head.addWidget(self._summary, 1)
        self._failed_only = QCheckBox("Failed attempts only")
        self._failed_only.toggled.connect(self._fill)
        head.addWidget(self._failed_only)
        lay.addLayout(head)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        enable_copy(self._table, multi=True)
        lay.addWidget(self._table)
        self._shown: List[RegisterEvent] = []
        self.set_events(events)
        self.resize(900, 420)

    def set_events(self, events: List[RegisterEvent],
                   sample_rate_hz: Optional[float] = None) -> None:
        """Show `events` in place of the current ones: the summary, the failed-only toggle
        (unchecked and disabled when none failed) and the rows."""
        self._events = list(events)
        if sample_rate_hz is not None:
            self._rate = float(sample_rate_hz) or 1.0
        n_failed = sum(1 for e in self._events if e.failed)
        title = self._title
        self._summary.setText(
            f"<b>{title}</b> · {len(self._events)} access(es)"
            + (f" · {n_failed} did not land as asked" if n_failed else "")
            if self._events else f"<b>{title}</b> · not written or read in this capture")
        blocked = self._failed_only.blockSignals(True)
        if not n_failed:
            self._failed_only.setChecked(False)
        self._failed_only.setEnabled(n_failed > 0)
        self._failed_only.blockSignals(blocked)
        self._fill()

    def _fill(self, *_a) -> None:
        only = self._failed_only.isChecked()
        self._shown = [e for e in self._events if e.failed or not only]
        t = self._table
        t.setRowCount(len(self._shown))
        tint = QBrush(QColor(VizTheme.SEM_ERROR_BG))
        for r, e in enumerate(self._shown):
            cells = [f"{e.start_sample / self._rate * 1e6:,.2f}",
                     f"{self._display_row(e.bus_row):,}",
                     e.access,
                     "—" if e.value is None else f"0x{e.value:02X}",
                     "yes" if e.applied else "no",
                     e.status, e.response]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c in (0, 1, 3):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if e.failed:
                    item.setBackground(tint)
                t.setItem(r, c, item)
        t.resizeColumnsToContents()

    def retheme(self) -> None:
        """Re-apply the active palette: the stylesheet, and the failed rows' tint."""
        self.setStyleSheet(analyzer_stylesheet())
        self._fill()

    def event_at(self, row: int) -> Optional[RegisterEvent]:
        return self._shown[row] if 0 <= row < len(self._shown) else None

    def _on_double_click(self, row: int, _col: int) -> None:
        e = self.event_at(row)
        if e is not None:
            self.jump.emit(int(e.start_sample))
