"""Clipboard copy for the read-only data views (ui/copyable.enable_copy).

⌘/Ctrl+C copies the selected rows as TSV so decoded data can be pasted elsewhere; the
views stay read-only. Command table is single-select (its selection drives the shared
cursor); the register map, CDS symbols and decoded samples allow multi-row range copy.

Run: QT_QPA_PLATFORM=offscreen PYQTGRAPH_QT_LIB=PySide6 PYTHONPATH=. python3 -m pytest tests/test_copyable.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QAbstractItemView, QApplication

from swi3s_studio.ui.copyable import copy_selection


def _win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    return win


def test_command_table_copies_selected_row_as_tsv():
    win = _win()
    cv = win._cmd_view
    # Per-cell selection now (the cursor follows the CURRENT cell, not selectedRows),
    # so cells are individually selectable/copyable and ranges drag out.
    assert cv.selectionMode() == QAbstractItemView.ExtendedSelection
    assert cv.selectionBehavior() == QAbstractItemView.SelectItems
    cv.selectRow(0)                                                  # whole-row select still works
    QGuiApplication.clipboard().clear()
    copy_selection(cv)
    text = QGuiApplication.clipboard().text()
    assert text.strip() and "\t" in text, repr(text)                 # a full row, tab-separated


def test_command_current_cell_drives_cursor():
    """Selecting an individual CELL (not the whole row) moves the shared cursor to that
    command — the cursor follows the current cell, which is what makes per-cell
    selection safe. An external cursor move keeps the current cell on the matching row."""
    from PySide6.QtCore import QModelIndex
    win = _win()
    cv = win._cmd_view
    assert cv.model().rowCount() >= 2
    # A single non-first-column cell in row 1 (a cell, not a row selection).
    idx = cv.model().index(1, 1)
    cv.setCurrentIndex(idx)
    cmd = win._cmd_model.command_at(win._cmd_proxy.mapToSource(idx).row())
    assert cmd is not None
    assert win.cursor.sample == win._session.command_cursor_sample(cmd)
    # External cursor move to row 0's command -> current cell lands on that row.
    cmd0 = win._cmd_model.command_at(win._cmd_proxy.mapToSource(cv.model().index(0, 0)).row())
    win.cursor.set_sample(win._session.command_cursor_sample(cmd0))
    assert cv.currentIndex().row() == 0
    assert cv.selectionModel().isRowSelected(0, QModelIndex())


def test_multi_row_views_copy_ranges():
    win = _win()
    # Symbols / decoded samples / register map allow extended (range) selection + copy.
    assert win._symbol_view.selectionMode() == QAbstractItemView.ExtendedSelection
    assert win._sample_view._table.selectionMode() == QAbstractItemView.ExtendedSelection
    tree = win._reg_view._tree
    assert tree.selectionMode() == QAbstractItemView.ExtendedSelection
    if tree.topLevelItemCount():
        tree.setCurrentItem(tree.topLevelItem(0))
        QGuiApplication.clipboard().clear()
        copy_selection(tree)
        assert QGuiApplication.clipboard().text().strip()


def test_copy_empty_selection_is_noop():
    win = _win()
    cv = win._cmd_view
    cv.clearSelection()
    QGuiApplication.clipboard().setText("sentinel")
    copy_selection(cv)                                               # nothing selected
    assert QGuiApplication.clipboard().text() == "sentinel"         # clipboard untouched


def test_cds_pane_copies_a_single_cell():
    """The CDS symbol pane supports per-cell copy, like the Command table.

    It used to be row-select only (`enable_copy(multi=True)`), so ⌘C always copied
    the whole symbol row — you could not lift out just a Time or a Raw value. The
    blocker was that its cursor sync read `selectedRows()`, which reports nothing for
    a partial-row selection; it now follows the CURRENT cell's row instead, the same
    way the Command table does.
    """
    win = _win()
    sv = win._symbol_view
    assert sv.selectionBehavior() == QAbstractItemView.SelectItems
    if sv.rowCount() < 3 or sv.columnCount() < 2:
        return
    sv.clearSelection()
    sv.setCurrentCell(2, 1)
    sv.item(2, 1).setSelected(True)
    QGuiApplication.clipboard().clear()
    copy_selection(sv)
    got = QGuiApplication.clipboard().text()
    assert got == str(sv.item(2, 1).text()), got
    assert "\t" not in got and "\n" not in got, f"copied more than one cell: {got!r}"


def test_cds_pane_copies_a_cell_block_as_tsv():
    """Dragging out a rectangle copies a TSV grid — spreadsheet-ready.

    NOTE the behaviour assert: setSelected() bypasses SelectionBehavior (that policy
    governs MOUSE selection), so the copy half of this test passes under row-select
    too. The assert is what ties it to the user-visible change.
    """
    win = _win()
    sv = win._symbol_view
    assert sv.selectionBehavior() == QAbstractItemView.SelectItems
    if sv.rowCount() < 3 or sv.columnCount() < 2:
        return
    sv.clearSelection()
    for r in (1, 2):
        for c in (0, 1):
            sv.item(r, c).setSelected(True)
    QGuiApplication.clipboard().clear()
    copy_selection(sv)
    lines = QGuiApplication.clipboard().text().split("\n")
    assert len(lines) == 2, lines
    assert all(len(l.split("\t")) == 2 for l in lines), lines
    assert lines[0] == f"{sv.item(1, 0).text()}\t{sv.item(1, 1).text()}"


def test_cds_cell_selection_still_drives_the_cursor():
    """Per-cell selection must not break the pane's cursor sync: clicking any cell
    still emits the row's sample. This is what made `cells=True` unsafe before —
    selectedRows() is empty for a partial row, so the cursor stopped following."""
    win = _win()
    sv = win._symbol_view
    assert sv.selectionBehavior() == QAbstractItemView.SelectItems
    if sv.rowCount() < 4:
        return
    seen = []
    sv.sampleSelected.connect(lambda s: seen.append(s))
    sv.setCurrentCell(3, 2)                      # a cell in the MIDDLE of the row
    assert seen, "selecting a cell did not move the shared cursor"
    assert seen[-1] == int(sv._symbols[3]["start_sample"])


def test_cds_programmatic_select_highlights_the_whole_row():
    """select_bus_row() (a cursor move from another pane) highlights the entire row,
    not the single current cell — otherwise the match reads as a stray dot."""
    win = _win()
    sv = win._symbol_view
    if not sv._symbol_rows:
        return
    sv.select_bus_row(sv._symbol_rows[min(5, len(sv._symbol_rows) - 1)])
    rows = sv.selectionModel().selectedRows()
    assert rows, "programmatic select did not select a full row"
    assert sv.currentRow() == rows[0].row(), "current cell left on a different row"
