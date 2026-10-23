"""The clickable colour-key entries, each clicked to hide or show what it names: ClickLabel in
the Timing pane (an edge flavour) and KeyToggle in the Registers pane (a value's
provenance)."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QLabel, QToolButton

from .theme import VizTheme


class ClickLabel(QLabel):
    """A QLabel that emits `clicked` on a left-button release inside it."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt signature)
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class KeyToggle(QToolButton):
    """A colour-key entry drawn as a swatch and its name, and a checkable button: checked
    while what it names is shown. It takes the focus on Tab, toggles on Space, Enter or
    Return as on a click, and gives assistive technology its name and checked state."""

    _SWATCH = 10

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setText(name)
        self.setAccessibleName(name)
        self.setCheckable(True)
        self.setChecked(True)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(self._SWATCH, self._SWATCH))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)

    def set_colors(self, swatch: str, text: str) -> None:
        """Draw the swatch in `swatch` and the name in `text` (both hex)."""
        pix = QPixmap(self._SWATCH, self._SWATCH)
        pix.fill(QColor(swatch))
        self.setIcon(QIcon(pix))
        # Flat, like a label; the focus shows as an outline in the theme's accent.
        self.setStyleSheet(
            f"QToolButton, QToolButton:checked, QToolButton:hover {{ color: {text}; "
            f"background: transparent; font-size: 12px; padding: 0px 2px; "
            f"border: 1px solid transparent; border-radius: 4px; }}"
            f"QToolButton:focus {{ border-color: {VizTheme.ACCENT}; }}")

    def keyPressEvent(self, event):  # noqa: N802 (Qt signature)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.isAutoRepeat():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)
