"""Clipboard copy for the read-only data views (ui/copyable.enable_copy).

⌘/Ctrl+C copies the selected rows as TSV so decoded data can be pasted elsewhere; the
views stay read-only. Command table is single-select (its selection drives the shared
cursor); the register map, CDS symbols and decoded samples allow multi-row range copy.

Run: QT_QPA_PLATFORM=offscreen PYQTGRAPH_QT_LIB=PySide6 PYTHONPATH=. python3 tests/test_copyable.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QAbstractItemView
from PySide6.QtGui import QGuiApplication

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
    assert cv.selectionMode() == QAbstractItemView.SingleSelection   # cursor-sync stays single
    cv.selectRow(0)
    QGuiApplication.clipboard().clear()
    copy_selection(cv)
    text = QGuiApplication.clipboard().text()
    assert text.strip() and "\t" in text, repr(text)                 # a full row, tab-separated


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


if __name__ == "__main__":
    test_command_table_copies_selected_row_as_tsv()
    print("ok: command table copies the selected row as TSV")
    test_multi_row_views_copy_ranges()
    print("ok: symbols / samples / register map allow range selection + copy")
    test_copy_empty_selection_is_noop()
    print("ok: copy with no selection is a no-op")
    print("ALL COPYABLE TESTS PASSED")
