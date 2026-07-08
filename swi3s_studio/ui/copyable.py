"""Clipboard copy for the read-only data views (command table, register map, CDS
symbols, decoded samples).

`enable_copy(view)` wires ⌘/Ctrl+C to copy the current selection as TSV (tab between
columns, newline between rows) — so decoded data can be pasted into a spreadsheet or
notes. The views stay read-only; this only reads the selection. `multi=True` also
switches the view to extended (range) selection so several rows can be copied at once
(leave it False where a single-row selection drives a shared cursor).
"""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractItemView


def enable_copy(view, *, multi: bool = False) -> None:
    if multi:
        view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    sc = QShortcut(QKeySequence.Copy, view)
    sc.setContext(Qt.WidgetWithChildrenShortcut)     # active when the view has focus
    sc.activated.connect(lambda v=view: copy_selection(v))


def copy_selection(view) -> None:
    """Copy the view's selected cells to the clipboard as TSV (rows in visual order,
    columns left-to-right). No-op when nothing is selected."""
    indexes = view.selectedIndexes() if hasattr(view, "selectedIndexes") else []
    if not indexes:
        return
    rows: dict = defaultdict(dict)
    for ix in indexes:
        val = ix.data(Qt.DisplayRole)
        rows[ix.row()][ix.column()] = "" if val is None else str(val)
    lines = []
    for r in sorted(rows):
        cols = rows[r]
        lines.append("\t".join(cols[c] for c in sorted(cols)))
    QGuiApplication.clipboard().setText("\n".join(lines))
