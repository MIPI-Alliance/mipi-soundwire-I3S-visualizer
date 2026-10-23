"""The Import warnings window: what an import skipped, clamped, defaulted or ignored.

One window per import that raised warnings (import_notes.ImportWarning), titled with the
file(s) it read and listing every warning as `file: message`. Not modal: the main window
stays usable while it is open, and the list can be selected and copied.
"""
from __future__ import annotations

from typing import List, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QVBoxLayout,
)

from ..import_notes import ImportWarning
from .copyable import enable_copy
from .theme import analyzer_stylesheet


def sources_of(notes: Sequence[ImportWarning]) -> List[str]:
    """The distinct sources of `notes`, in first-seen order."""
    out: List[str] = []
    for n in notes:
        if n.source and n.source not in out:
            out.append(n.source)
    return out


def warnings_title(notes: Sequence[ImportWarning], what: str = "") -> str:
    """'Import warnings — <file>' (several files are joined; `what` stands in when the
    warnings name no file)."""
    names = sources_of(notes)
    label = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    label = label or what
    return f"Import warnings — {label}" if label else "Import warnings"


class ImportWarningsDialog(QDialog):
    """Lists `notes`; `warnings` holds them for a caller (or a test) to read back."""

    def __init__(self, notes: Sequence[ImportWarning], what: str = "", parent=None) -> None:
        super().__init__(parent)
        self.warnings: List[ImportWarning] = list(notes)
        self.setWindowTitle(warnings_title(self.warnings, what))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowModality(Qt.NonModal)
        self.setStyleSheet(analyzer_stylesheet())

        lay = QVBoxLayout(self)
        n = len(self.warnings)
        head = QLabel(f"{n} import warning{'s' if n != 1 else ''}. The import went ahead; "
                      "these are the parts of the file that were not read as written.")
        head.setWordWrap(True)
        lay.addWidget(head)
        self._list = QListWidget()
        self._list.setWordWrap(True)
        self._list.setSelectionBehavior(QAbstractItemView.SelectRows)
        for note in self.warnings:
            self._list.addItem(str(note))
        enable_copy(self._list, multi=True)
        lay.addWidget(self._list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        lay.addWidget(buttons)
        self.resize(720, 320)

    def lines(self) -> List[str]:
        """The listed lines, as shown."""
        return [self._list.item(i).text() for i in range(self._list.count())]

    def retheme(self) -> None:
        self.setStyleSheet(analyzer_stylesheet())
