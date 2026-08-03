"""Measurements pane: one row per completed bookmark PAIR (A, B, …) showing the delta
between its two members in time, bus rows, and UIs. Updates live as bookmarks are
added/dragged; clicking a row seeks the cursor to that pair's left member.

Distinct from the "Statistics" pane (MeasurementsView), which shows whole-capture facts;
this pane is purely the user's paired-bookmark measurements."""
from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from .grid_view import bookmark_pair_color
from .theme import analyzer_stylesheet

# Pull the leading pair letter out of a row label ("A1", "A2", "A1-A2" → "A"); rows whose
# label doesn't start with a bookmark letter (unexpected/blank) fall back to the default
# text colour rather than guessing.
_PAIR_LETTER_RE = re.compile(r"^([A-Za-z])")


class PairMeasureView(QTableWidget):
    #: emitted with the sample to seek to when a pair row is activated
    sampleSelected = Signal("qlonglong")

    # One ROW each for A1, A2 and their delta (A1-A2) — the members go down the rows, the
    # metrics across the (few, equal-width) columns. A lone bookmark shows just its A1 row.
    _COLS = ["", "UI", "Row", "Time"]

    def __init__(self) -> None:
        super().__init__(0, len(self._COLS))
        self.setStyleSheet(analyzer_stylesheet())
        self.setHorizontalHeaderLabels(self._COLS)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)   # metric cols equal-width
        self.horizontalHeader().setStretchLastSection(True)
        # The label column (e.g. "Sub 1/1  score=1.00") is wider than an equal share of a
        # narrow pane, so size it to its text instead of eliding to "Sub 1/1 …".
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        from .copyable import enable_copy
        enable_copy(self)          # ⌘/Ctrl+C copies the selected row (TSV); row-select is
                                   # kept so activating a row still drives the seek.
        self._seek_samples: List[int] = []          # per-row sample to seek to
        self.cellClicked.connect(self._on_cell)

    def retheme(self) -> None:
        self.setStyleSheet(analyzer_stylesheet())

    def set_rows(self, rows: Sequence[Tuple]) -> None:
        """Each row is (label, ui, row, time, seek_sample) — an A1, A2 or A1-A2 line."""
        self._seek_samples = [int(r[4]) for r in rows]
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            label = str(row[0])
            m = _PAIR_LETTER_RE.match(label)
            brush = QBrush(bookmark_pair_color(m.group(1))) if m else None
            for c in range(len(self._COLS)):
                item = QTableWidgetItem(str(row[c]))
                item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(str(row[c]))        # full text on hover if a cell is narrow
                if brush is not None:
                    item.setForeground(brush)
                self.setItem(r, c, item)

    def _on_cell(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._seek_samples):
            self.sampleSelected.emit(self._seek_samples[row])
