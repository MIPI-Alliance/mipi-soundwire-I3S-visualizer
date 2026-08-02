"""CDS 8b/10b symbol viewer: a colour-coded table of decoded symbols.

Each row is one Control Data Stream symbol with its 10-bit codeword, kind
(comma / robust token / D-code / K-code / invalid, coloured to match the spec
classes), and decoded value. Symbols are re-decoded from the capture on demand
(not persisted) via swi3score.decode_symbols.
"""
from __future__ import annotations

import bisect
from typing import List

import swi3score
from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
)

from ..analysis.cds_meaning import annotate
from .row_origin import RowOriginMixin
from .theme import VizTheme, analyzer_stylesheet

_KIND_COLOR = {
    0: QColor(VizTheme.SYM_INVALID),     # Invalid
    1: QColor(VizTheme.SYM_COMMA),       # Comma
    2: QColor(VizTheme.SYM_ROBUST),      # RobustToken
    3: QColor(VizTheme.SYM_DCODE),       # Dcode
    4: QColor(VizTheme.SYM_KCODE),       # Kcode
    5: QColor(VizTheme.AXIS),            # NoResponse (undriven all-ones) — muted grey
}
_COLS = ["Row", "Time (µs)", "Codeword", "Disp", "RD", "Kind", "Decoded", "Meaning"]


def _codeword(raw: int) -> str:
    b = format(raw & 0x3FF, "010b")
    return f"{b[:5]} {b[5:]}  (0x{raw:03X})"     # abcdei fghj


def _disparity(raw: int) -> int:
    """Symbol disparity = (#ones − #zeros) over the 10-bit codeword: −2, 0 or +2
    for a legal 8b/10b symbol (other values mean an illegal/undriven codeword)."""
    ones = bin(raw & 0x3FF).count("1")
    return 2 * ones - 10


def _decoded(kind: int, value: int) -> str:
    if kind == 1:
        return "K.28.7 — SPM (comma)"
    if kind == 2:
        return f"RT{value}"
    if kind == 3:
        return f"D 0x{value:02X}"
    if kind == 4:
        return f"K 0x{value:02X}"
    if kind == 5:
        return "0x3FF"                 # NO_RESPONSE — the undriven all-ones codeword
    return "—"


class SymbolView(RowOriginMixin, QTableWidget):
    # Emitted with a symbol's start sample when the user selects a row, so the
    # shared cursor (and the command table) can follow. 64-bit: samples > 2**31.
    sampleSelected = Signal('qlonglong')
    # Emitted when the user scrolls to the TOP of the window and there may be earlier
    # symbols to show — carries the earliest currently-shown start sample so the owner
    # can decode the preceding chunk and prepend_symbols() it (decode-on-scroll).
    moreAboveRequested = Signal('qlonglong')

    # Soft cap on accumulated back-scroll history, so a long scroll-up doesn't grow
    # the table without bound (each fetched chunk is ~512 rows).
    _MAX_ACCUM = 16000

    def __init__(self) -> None:
        super().__init__(0, len(_COLS))
        self.setStyleSheet(analyzer_stylesheet())
        self.setHorizontalHeaderLabels(_COLS)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Per-cell selection (spreadsheet-style), matching the Command table: copy one
        # cell, or drag out an arbitrary range. Safe even though this pane drives the
        # shared cursor, because the cursor now follows the CURRENT cell's row
        # (currentCellChanged -> _on_current_cell) rather than selectedRows() — a
        # partial-row cell selection would report no selected row. See copyable.py.
        from .copyable import enable_copy
        enable_copy(self, cells=True)             # ⌘/Ctrl+C copies the selected cells (TSV)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)   # drop the row-index column (always 1,2,3… — the "Row" column carries the meaningful bus row)
        self.horizontalHeader().setStretchLastSection(True)
        self._rate = 0.0          # capture sample rate, for the Time column
        self._symbols: List[dict] = []
        self._symbol_rows: List[int] = []   # ascending per-symbol bus rows (select_bus_row bisect)
        self._at_start = False    # reached the earliest symbol — nothing more above
        self._loading_above = False   # a decode-on-scroll fetch is in flight
        self._populating = False  # suppress the scroll handler while (re)filling rows
        self._suppress_fetch = False  # suppress fetch during a programmatic select-scroll
        self._suppress_select = False  # suppress the selection echo during a programmatic
        #                                select_bus_row / repopulate (see _on_select)
        self._cols_sized = False  # size columns once per capture, not on every window refresh
        # Cursor follows the CURRENT cell's row, not the selection: with per-cell
        # selection a user can select part of a row (or a block spanning rows), and
        # selectedRows() reports nothing for a partial row.
        self.currentCellChanged.connect(self._on_current_cell)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _on_scroll(self, value: int) -> None:
        """When the user scrolls to (near) the top, ask the owner for the preceding
        symbols so they can keep scrolling back — bounded by _MAX_ACCUM."""
        if (self._populating or self._suppress_fetch or self._loading_above
                or self._at_start or not self._symbols
                or len(self._symbols) >= self._MAX_ACCUM):
            return
        if value <= self.verticalScrollBar().minimum() + 2:
            self._loading_above = True
            self.moreAboveRequested.emit(int(self._symbols[0]["start_sample"]))

    def prepend_symbols(self, symbols: List[dict]) -> None:
        """Add earlier symbols ABOVE the current window (decode-on-scroll), keeping the
        previously-top row in place so the view doesn't jump. Empty means there's
        nothing earlier — stop asking."""
        self._loading_above = False
        if not symbols:
            self._at_start = True
            return
        n = len(symbols)
        self._symbols = list(symbols) + self._symbols
        self._populate()
        anchor = self.item(n, 0)          # the row that was at the top before
        if anchor is not None:
            # Suppress the fetch during this programmatic scroll: if a small chunk
            # lands the anchor within the top threshold it would otherwise re-fire.
            self._suppress_fetch = True
            try:
                self.scrollToItem(anchor, QAbstractItemView.PositionAtTop)
            finally:
                self._suppress_fetch = False

    def set_sample_rate(self, rate_hz: float) -> None:
        self._rate = float(rate_hz or 0.0)
        self._cols_sized = False          # new capture -> re-measure columns on the next fill

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the per-kind symbol
        colours and the item-view stylesheet, then repopulate so each row's
        foreground picks up the new theme."""
        global _KIND_COLOR
        _KIND_COLOR = {
            0: QColor(VizTheme.SYM_INVALID), 1: QColor(VizTheme.SYM_COMMA),
            2: QColor(VizTheme.SYM_ROBUST), 3: QColor(VizTheme.SYM_DCODE),
            4: QColor(VizTheme.SYM_KCODE), 5: QColor(VizTheme.AXIS),
        }
        self.setStyleSheet(analyzer_stylesheet())
        self.set_symbols(self._symbols)

    def set_symbols(self, symbols: List[dict]) -> None:
        """Replace the shown symbols (a fresh window around the cursor). Resets the
        decode-on-scroll state so back-scroll starts over for the new window."""
        self._symbols = list(symbols)
        self._at_start = False
        self._loading_above = False
        self._populate()

    def _populate(self) -> None:
        symbols = self._symbols
        # Ascending per-symbol bus rows, for select_bus_row's bisect (symbols are a
        # time-ordered window, so rows are non-decreasing).
        self._symbol_rows = [int(s.get("row", 0)) for s in symbols]
        meanings = annotate(symbols)              # role of each symbol in its phase (may raise → do first)
        self._populating = True
        prev_sel = self._suppress_select
        self._suppress_select = True          # setRowCount churns selection; don't echo it
        self.setUpdatesEnabled(False)         # bulk-fill a VISIBLE table without a per-item
        #                                       relayout/repaint (offscreen elides these; on a real
        #                                       display filling up to 1024 rows x 8 cols one setItem
        #                                       at a time otherwise triggers a repaint storm on every
        #                                       settled cursor move — same fix as decoded_sample_view).
        self.blockSignals(True)               # repopulating shouldn't emit selection
        try:
            self.setRowCount(len(symbols))
            rd = -1                                   # running disparity sign (start assume RD-)
            for r, s in enumerate(symbols):
                kind = s["kind"]
                start = int(s["start_sample"])
                t_us = f"{start / self._rate * 1e6:,.2f}" if self._rate else "—"
                # Running disparity, shown as -1 / +1. A legal symbol ends the line
                # negative (#ones 4), positive (#ones 6) or neutral (#ones 5, RD
                # unchanged). A ±2 symbol sent when RD already has that sign is a
                # disparity violation (suffix "!"); an illegal-disparity codeword (e.g.
                # undriven all-ones NO_RESPONSE) has no valid RD → "?".
                d = _disparity(s["raw"])
                disp_txt = f"{d:+d}" if d else "0"    # this codeword's own disparity (−2/0/+2; other = illegal)
                viol = False
                if d == 2:
                    viol = rd > 0; rd = 1
                elif d == -2:
                    viol = rd < 0; rd = -1
                if d in (-2, 0, 2):
                    rd_txt = ("+1" if rd > 0 else "-1") + ("!" if viol else "")
                else:
                    rd_txt = "?"                      # not a legal 8b/10b disparity
                cells = [f"{self.display_row(s.get('row', 0)):,}",
                         t_us, _codeword(s["raw"]),
                         disp_txt, rd_txt, swi3score.SYMBOL_KINDS[kind], _decoded(kind, s["value"]),
                         meanings[r]]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if c == 5:                        # Kind column (shifted by the new Disp + RD columns)
                        item.setForeground(_KIND_COLOR.get(kind, QColor(VizTheme.SYM_UNKNOWN)))
                    if c == 4 and "!" in text:        # disparity (RD) violation stands out
                        item.setForeground(QColor(VizTheme.SEM_ERROR))
                    self.setItem(r, c, item)
            if not self._cols_sized and symbols:
                # Column widths depend only on the fixed cell shapes (row/time, 10-bit
                # codeword, ±1 disparity/RD, kind + decoded names) — size ONCE per capture,
                # not on every window refresh (each resize scans rows + relays out).
                self.resizeColumnsToContents()
                self._cols_sized = True
        finally:
            self.blockSignals(False)              # always restore, even on error
            self.setUpdatesEnabled(True)
            self._populating = False
            self._suppress_select = prev_sel

    def _on_current_cell(self, row: int, _col: int, _prev_row: int, _prev_col: int) -> None:
        # A programmatic select_bus_row() / repopulate must not echo back out as a user
        # selection: currentCellChanged fires even under blockSignals() in the live
        # widget, so gate on an explicit flag (see decoded_sample_view._on_select).
        if self._suppress_select:
            return
        if 0 <= row < len(self._symbols):
            self.sampleSelected.emit(int(self._symbols[row]["start_sample"]))

    def select_bus_row(self, bus_row) -> None:
        """Select the symbol at SWI3S bus row `bus_row` (a command's SPM comma) and
        scroll it to the TOP of the pane, so picking a command brings its first CDS
        symbol to a predictable place. To see earlier symbols, scroll up — the pane
        decodes and prepends the preceding history on demand (see moreAboveRequested).
        Falls back to the first symbol. Programmatic, so it must not echo back as a
        user selection (block signals)."""
        if not self._symbols:
            return
        idx = 0
        if bus_row is not None:
            # Symbol rows are ascending, so bisect to the first symbol at that bus row
            # (was a linear next() scan over up to 1024 symbols per cursor move); fall
            # back to the first symbol when there's no exact match.
            br = int(bus_row)
            i = bisect.bisect_left(self._symbol_rows, br)
            idx = i if (i < len(self._symbol_rows) and self._symbol_rows[i] == br) else 0
        prev_sel = self._suppress_select
        self._suppress_select = True
        self.blockSignals(True)
        try:
            # Highlight the WHOLE row, not one cell: under per-cell selection
            # (SelectItems) selectRow() marks only the current column's cell, which
            # reads as a stray dot rather than "this is the symbol you picked".
            self.setCurrentCell(idx, max(0, self.currentColumn()))
            self.setRangeSelected(
                QTableWidgetSelectionRange(idx, 0, idx, self.columnCount() - 1), True)
        finally:
            self.blockSignals(False)
            self._suppress_select = prev_sel
        item = self.item(idx, 0)
        if item is not None:
            # Suppress decode-on-scroll for this programmatic jump: if the target lands
            # at the very top the scrollbar hits its minimum, which would otherwise fire
            # a prepend and push the selection back down. The user's own scroll-up still
            # triggers back-history.
            self._suppress_fetch = True
            try:
                self.scrollToItem(item, QAbstractItemView.PositionAtTop)
            finally:
                self._suppress_fetch = False

    def sample_at_row(self, row: int):
        if 0 <= row < len(self._symbols):
            return int(self._symbols[row]["start_sample"])
        return None

    @property
    def symbol_count(self) -> int:
        return self.rowCount()
