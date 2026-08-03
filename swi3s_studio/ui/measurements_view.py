"""Statistics panel: a grouped metric/value table with collapsible sections.

Rows come from `analysis.capture_measurements` as `(metric, value)` or
`(metric, value, kind)`. A `kind == "header"` row starts a collapsible SECTION
(Link Control / Regions / Commands / Data Ports); clicking it folds the rows
beneath it away, so a long capture's Statistics pane stays scannable. 'pass'/'fail'
colour the value green/red.

`kind == "group"` is a SECOND fold level nested inside a section: a per-command row
(Ping, WriteA32, …) whose `kind == "detail"` rows below it hold the breakdown —
per-peripheral Ping responses, per-device Read/Write counts and byte totals. Groups
start collapsed so the section still reads as a one-line-per-command summary, and a
group's details stay hidden while its section is folded.
"""
from __future__ import annotations

from typing import Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QTableWidget, QTableWidgetItem

from .theme import VizTheme, analyzer_stylesheet

_COLLAPSED = "▸ "
_EXPANDED = "▾ "

# Kinds that are a CHILD of the enclosing `group`, i.e. that fold away with it. 'detail'
# is the plain case; a child may also be a 'fail'/'pass' row, because a group's breakdown
# reports its own error count ("errors", red) — see analysis.measurements._rw_detail_rows.
# Membership is about position in the tree, styling is a separate axis, and conflating the
# two is what let the fold leak: 'fail' was styled but never adopted, so the errors row
# stayed visible under a collapsed group AND ended the group, orphaning every later device.
_GROUPED_KINDS = ("detail", "fail", "pass")


class MeasurementsView(QTableWidget):
    def __init__(self) -> None:
        super().__init__(0, 2)
        self.setStyleSheet(analyzer_stylesheet())
        self.setHorizontalHeaderLabels(["Metric", "Value"])
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        # header row index -> [child row indices]; and remembered collapsed titles so a
        # re-populate (cursor move / re-decode) keeps the user's fold state.
        self._sections: dict = {}
        self._collapsed_titles: set = set()
        # group row index -> [detail row indices], and group row -> owning section row.
        # Groups are keyed by title for fold memory exactly as sections are, but start
        # COLLAPSED, so an unseen title means collapsed (the inverse of a section).
        self._groups: dict = {}
        self._group_owner: dict = {}
        self._expanded_groups: set = set()
        self.cellClicked.connect(self._on_cell_clicked)
        from .copyable import enable_copy
        enable_copy(self, cells=True)          # ⌘/Ctrl+C copies selected cells (TSV)

    def retheme(self) -> None:
        """Re-apply the item-view stylesheet after a theme switch."""
        self.setStyleSheet(analyzer_stylesheet())

    def set_rows(self, rows: Sequence[Tuple]) -> None:
        """Populate the table. Each row is (metric, value) or (metric, value, kind)
        where kind is 'header' (bold, collapsible section title), 'group' (a
        collapsible per-command row), 'detail' (a child of the preceding group),
        'pass'/'fail' (green/red value), or None (plain). 'pass'/'fail' are ALSO
        children of the enclosing group when there is one (see _GROUPED_KINDS); a
        'header' or a plain row ends the group."""
        self.setRowCount(len(rows))
        self._sections = {}
        self._groups = {}
        self._group_owner = {}
        cur_header = None
        cur_group = None
        header_font = QFont(self.font()); header_font.setBold(True)
        for r, row in enumerate(rows):
            metric, value = row[0], row[1]
            kind = row[2] if len(row) > 2 else None
            if kind == "header":
                cur_header = r
                cur_group = None
                self._sections[r] = []
                m_item = QTableWidgetItem(metric)     # arrow prefix added by _apply_fold
                m_item.setFont(header_font)
                m_item.setForeground(QColor(VizTheme.ACCENT))
                m_item.setData(Qt.UserRole, metric)   # the bare title (no arrow), for fold state
                v_item = QTableWidgetItem(value)
            else:
                if cur_header is not None:
                    self._sections[cur_header].append(r)
                if kind == "group":
                    cur_group = r
                    self._groups[r] = []
                    self._group_owner[r] = cur_header
                    m_item = QTableWidgetItem(metric)
                    m_item.setData(Qt.UserRole, metric)
                    v_item = QTableWidgetItem(value)
                else:
                    if kind in _GROUPED_KINDS and cur_group is not None:
                        self._groups[cur_group].append(r)
                    else:
                        cur_group = None          # a plain row ends the current group
                    m_item = QTableWidgetItem(metric)
                    v_item = QTableWidgetItem(value)
                    if kind == "fail":
                        v_item.setForeground(QColor(VizTheme.SEM_ERROR))
                    elif kind == "pass":
                        v_item.setForeground(QColor(VizTheme.SEM_OTHER))
            self.setItem(r, 0, m_item)
            self.setItem(r, 1, v_item)
        self.resizeColumnToContents(0)
        self._apply_folds()

    def _title_of(self, row: int):
        item = self.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.UserRole) or item.text()

    def _apply_folds(self) -> None:
        """Set each header's / group's ▸ ▾ prefix and show/hide rows to match the
        remembered fold state (so folds survive a re-populate).

        Two levels: a detail row is visible only when BOTH its group is expanded and
        its section is not collapsed."""
        collapsed_sections = set()
        for hr, children in self._sections.items():
            title = self._title_of(hr)
            if title is None:
                continue
            collapsed = title in self._collapsed_titles
            if collapsed:
                collapsed_sections.add(hr)
            self.item(hr, 0).setText((_COLLAPSED if collapsed else _EXPANDED) + title)
            for cr in children:
                self.setRowHidden(cr, collapsed)

        for gr, details in self._groups.items():
            title = self._title_of(gr)
            if title is None:
                continue
            expanded = title in self._expanded_groups
            self.item(gr, 0).setText((_EXPANDED if expanded else _COLLAPSED) + title)
            section_folded = self._group_owner.get(gr) in collapsed_sections
            for dr in details:
                self.setRowHidden(dr, section_folded or not expanded)

    def _on_cell_clicked(self, row: int, _col: int) -> None:
        if row in self._sections:
            title = self._title_of(row)
            if title is None:
                return
            if title in self._collapsed_titles:
                self._collapsed_titles.discard(title)
            else:
                self._collapsed_titles.add(title)
            self._apply_folds()
        elif row in self._groups:
            title = self._title_of(row)
            if title is None:
                return
            if title in self._expanded_groups:
                self._expanded_groups.discard(title)
            else:
                self._expanded_groups.add(title)
            self._apply_folds()

    @property
    def metric_count(self) -> int:
        return self.rowCount()
