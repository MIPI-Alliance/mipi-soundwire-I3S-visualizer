"""Preferences: the waveforms' line colours per theme and line weights, and the Timing
Diagrams' drawing settings.

The Dark theme and Light theme tabs edit a working copy of line_style's settings; nothing
changes until OK. The Timing Diagrams tab (`diagram_style_page.DiagramStylePage`) edits a
working copy of diagram_style's settings and emits `diagramChanged` with it after each edit,
for a live preview; it is stored on OK. A colour below 3:1 on that theme's background (the
threshold the default palettes are held to) is allowed and flagged. Reset to Defaults resets
the tab shown: the Timing Diagrams settings on their tab, else the waveforms'.
"""
from __future__ import annotations

import copy
from typing import Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import diagram_style, line_style
from .diagram_style_page import DiagramStylePage
from .theme import _PALETTES, analyzer_stylesheet, contrast

MIN_CONTRAST = 3.0
_SWATCH = 28

# (kind, group title, entry labels). Data-port slots are (device × 32 + dp) mod 12, so
# on device 0 slot n is DPn; other devices start the rotation elsewhere.
_GROUPS = (
    ("dp", "Data-port lines (slot n is DPn on device 0)", [str(i) for i in range(12)]),
    ("bookmark", "Cursor pairs", [chr(65 + i) for i in range(12)]),
    ("capture", "Capture traces", ["DP", "DN"]),
    ("timing", "Timing edge flavours", ["clk↑ dat↑", "clk↑ dat↓", "clk↓ dat↑", "clk↓ dat↓"]),
)


def _plot_bg(mode: str) -> str:
    return str(_PALETTES[mode]["PLOT_BG"])


class PreferencesDialog(QDialog):
    """Per-theme line colours (data ports, bookmark pairs, Capture DP/DN, Timing
    flavours), the Audio / Capture line weights, and the Timing Diagrams tab, which starts
    from the stored diagram_style settings."""

    diagramChanged = Signal(dict)       # the Timing Diagrams tab's working copy, on each edit
    DIAGRAM_TAB = 2

    def __init__(self, prefs: dict, parent=None, mode: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setStyleSheet(analyzer_stylesheet())
        self._prefs = copy.deepcopy(prefs)
        self._prefs.setdefault("colors", {})
        self._prefs.setdefault("weights", {})
        self._swatches: Dict[tuple, QToolButton] = {}    # (mode, kind, i) -> button
        self._warnings: Dict[tuple, QLabel] = {}

        lay = QVBoxLayout(self)
        self._tabs = QTabWidget()
        for m, title in (("dark", "Dark theme"), ("light", "Light theme")):
            self._tabs.addTab(self._theme_page(m), title)
        self.diagram_page = DiagramStylePage(diagram_style.preferences())
        self.diagram_page.changed.connect(self.diagramChanged)
        self._tabs.addTab(self.diagram_page, "Timing Diagrams")
        if mode == "light":
            self._tabs.setCurrentIndex(1)
        lay.addWidget(self._tabs)

        self._weights = weights = QGroupBox("Waveform line weight (px), both themes")
        form = QFormLayout(weights)
        self._weight_spins: Dict[str, QSpinBox] = {}
        for kind, label in (("audio", "Audio waveforms"), ("capture", "Capture DP / DN")):
            spin = QSpinBox()
            spin.setRange(line_style.MIN_WEIGHT, line_style.MAX_WEIGHT)
            spin.setValue(int(self._prefs["weights"].get(kind, line_style.DEFAULT_WEIGHTS[kind])))
            spin.valueChanged.connect(lambda v, k=kind: self._prefs["weights"].__setitem__(k, v))
            form.addRow(label, spin)
            self._weight_spins[kind] = spin
        lay.addWidget(weights)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._reset_btn = QPushButton("Reset to Defaults")
        buttons.addButton(self._reset_btn, QDialogButtonBox.ResetRole)
        self._reset_btn.clicked.connect(self._reset_shown)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._tabs.currentChanged.connect(self._tab_shown)
        self._tab_shown(self._tabs.currentIndex())

    def show_diagram_tab(self) -> None:
        """Open on the Timing Diagrams tab."""
        self._tabs.setCurrentIndex(self.DIAGRAM_TAB)

    def set_diagram_units(self, lh: float, px: int) -> None:
        """Show the Timing Diagrams weights and sizes against a diagram whose line height is
        `lh` px and body font `px` px (`TimingDiagram.units`)."""
        self.diagram_page.set_units(lh, px)

    def diagram_preferences(self) -> dict:
        """The edited Timing Diagrams settings, for diagram_style.set_preferences."""
        return self.diagram_page.preferences()

    def _tab_shown(self, index: int) -> None:
        """The waveform weights show under the waveform tabs, and Reset says what it
        resets."""
        diagram = index == self.DIAGRAM_TAB
        self._weights.setVisible(not diagram)
        self._reset_btn.setToolTip("Every Timing Diagrams setting" if diagram
                                   else "Every waveform colour in both themes, and both "
                                        "weights")

    def _reset_shown(self) -> None:
        if self._tabs.currentIndex() == self.DIAGRAM_TAB:
            self.diagram_page.reset_to_defaults()
        else:
            self.reset_to_defaults()

    # ---- layout ----
    def _theme_page(self, mode: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        bg = _plot_bg(mode)
        for kind, title, labels in _GROUPS:
            box = QGroupBox(title)
            grid = QGridLayout(box)
            per_row = 6 if len(labels) > 4 else len(labels)
            for i, text in enumerate(labels):
                cell = QWidget()
                cl = QVBoxLayout(cell)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(1)
                btn = QToolButton()
                btn.setFixedSize(_SWATCH * (3 if kind == "timing" else 1), _SWATCH)
                btn.clicked.connect(lambda _c=False, m=mode, k=kind, j=i: self._pick(m, k, j))
                warn = QLabel()
                cl.addWidget(QLabel(text))
                cl.addWidget(btn)
                cl.addWidget(warn)
                grid.addWidget(cell, i // per_row, i % per_row)
                self._swatches[(mode, kind, i)] = btn
                self._warnings[(mode, kind, i)] = warn
                self._paint(mode, kind, i)
            lay.addWidget(box)
        note = QLabel(f"Swatches sit on this theme's plot background ({bg}); "
                      f"⚠ marks a colour below {MIN_CONTRAST:g}:1 on it.")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return page

    # ---- values ----
    def value(self, mode: str, kind: str, i: int) -> str:
        """The colour shown for one entry: the working copy's choice, else the default."""
        chosen = self._prefs["colors"].get(mode, {}).get(kind) or []
        return (chosen[i] if i < len(chosen) and chosen[i]
                else line_style.defaults(kind, mode)[i])

    def set_value(self, mode: str, kind: str, i: int, color: str) -> None:
        n = len(line_style.defaults(kind, mode))
        vals = self._prefs["colors"].setdefault(mode, {}).setdefault(kind, [None] * n)
        vals[i] = QColor(color).name()
        self._paint(mode, kind, i)

    def low_contrast(self, mode: str, kind: str, i: int) -> bool:
        return contrast(self.value(mode, kind, i), _plot_bg(mode)) < MIN_CONTRAST

    def _paint(self, mode: str, kind: str, i: int) -> None:
        c = self.value(mode, kind, i)
        bg = _plot_bg(mode)
        btn = self._swatches[(mode, kind, i)]
        # The colour as a thick bar on the theme's own plot background: how it will read.
        btn.setStyleSheet(f"QToolButton {{ background-color: {c}; border: 6px solid {bg};"
                          f" border-top-width: 10px; border-bottom-width: 10px; }}")
        ratio = contrast(c, bg)
        btn.setToolTip(f"{c} — {ratio:.1f}:1 on {bg}")
        self._warnings[(mode, kind, i)].setText(f"⚠ {ratio:.1f}:1" if ratio < MIN_CONTRAST else "")

    def _pick(self, mode: str, kind: str, i: int) -> None:
        title, labels = next((t, lb) for k, t, lb in _GROUPS if k == kind)
        c = QColorDialog.getColor(QColor(self.value(mode, kind, i)), self,
                                  f"{title}: {labels[i]} ({mode} theme)")
        if c.isValid():
            self.set_value(mode, kind, i, c.name())

    def reset_to_defaults(self) -> None:
        """Every waveform colour in both themes, and both weights."""
        self._prefs = {"colors": {}, "weights": {}}
        for (mode, kind, i) in self._swatches:
            self._paint(mode, kind, i)
        for kind, spin in self._weight_spins.items():
            spin.setValue(line_style.DEFAULT_WEIGHTS[kind])

    def preferences(self) -> dict:
        """The edited settings, for line_style.set_preferences."""
        return copy.deepcopy(self._prefs)
