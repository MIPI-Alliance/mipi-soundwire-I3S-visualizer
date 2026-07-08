"""Measurements panel: a flat metric/value table of derived capture metrics, plus a
Link Control Timing section (measured LC parameters vs the §5.2.3 spec limits)."""
from __future__ import annotations

from typing import List, Sequence, Tuple

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QTableWidget, QTableWidgetItem

from .theme import analyzer_stylesheet, VizTheme


class MeasurementsView(QTableWidget):
    def __init__(self) -> None:
        super().__init__(0, 2)
        self.setStyleSheet(analyzer_stylesheet())
        self.setHorizontalHeaderLabels(["Metric", "Value"])
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)

    def retheme(self) -> None:
        """Re-apply the item-view stylesheet after a theme switch."""
        self.setStyleSheet(analyzer_stylesheet())

    def set_rows(self, rows: Sequence[Tuple]) -> None:
        """Populate the table. Each row is (metric, value) or (metric, value, kind)
        where kind is 'header' (bold section title), 'pass'/'fail' (green/red value),
        or None (plain)."""
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            metric, value = row[0], row[1]
            kind = row[2] if len(row) > 2 else None
            m_item = QTableWidgetItem(metric)
            v_item = QTableWidgetItem(value)
            if kind == "header":
                f = QFont(self.font()); f.setBold(True)
                m_item.setFont(f)
                m_item.setForeground(QColor(VizTheme.ACCENT))
            elif kind == "fail":
                v_item.setForeground(QColor(VizTheme.SEM_ERROR))
            elif kind == "pass":
                v_item.setForeground(QColor(VizTheme.SEM_OTHER))
            self.setItem(r, 0, m_item)
            self.setItem(r, 1, v_item)
        self.resizeColumnToContents(0)

    @staticmethod
    def link_timing_rows(result) -> List[Tuple]:
        """Build the Link Control Timing section rows from a LinkControlResult: a header
        then one row per measured parameter, value = 'measured µs  [min–max]' with a
        PASS/FAIL colour. Empty when there's no bring-up / no measured timing."""
        timing = list(getattr(result, "timing", []) or [])
        if not timing:
            return []
        seq = getattr(result, "sequence", "none")
        rows: List[Tuple] = [(f"Link Control Timing (§5.2.3) — {seq.title()} Start", "", "header")]
        for t in timing:
            lo, hi = t.get("min_us"), t.get("max_us")
            if lo is not None and hi is not None:
                limit = f"[{lo:g}–{hi:g} µs]"
            elif lo is not None:
                limit = f"[≥ {lo:g} µs]"
            elif hi is not None:
                limit = f"[≤ {hi:g} µs]"
            else:
                limit = "(no limit)"
            ok = t.get("ok")
            status = "PASS" if ok else ("FAIL" if ok is False else "")
            kind = "pass" if ok else ("fail" if ok is False else None)
            val = f"{t['measured_us']:,.1f} µs   {limit}   {status}".rstrip()
            rows.append((t.get("param", ""), val, kind))
        return rows

    @property
    def metric_count(self) -> int:
        return self.rowCount()
