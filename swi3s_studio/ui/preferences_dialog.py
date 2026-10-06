"""Preferences ▸ Waveforms: line colours per theme, and line weights.

Edits a working copy of line_style's settings; nothing changes until OK. A colour below
3:1 on that theme's plot background is allowed and flagged, since it is the user's
call, but it is the threshold the default palettes are held to.
"""
from __future__ import annotations

import copy
from typing import Dict, Optional

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

from . import line_style
from .theme import _PALETTES, analyzer_stylesheet

MIN_CONTRAST = 3.0
_SWATCH = 28

# (kind, group title, entry labels). Data-port slots are (device × 32 + dp) mod 12, so
# on device 0 slot n is DPn; other devices start the rotation elsewhere.
_GROUPS = (
    ("dp", "Data-port lines (slot n is DPn on device 0)", [str(i) for i in range(12)]),
    ("bookmark", "Bookmark pairs", [chr(65 + i) for i in range(12)]),
    ("capture", "Capture traces", ["DP", "DN"]),
    ("timing", "Timing edge flavours", ["clk↑ dat↑", "clk↑ dat↓", "clk↓ dat↑", "clk↓ dat↓"]),
)


def _plot_bg(mode: str) -> str:
    return str(_PALETTES[mode]["PLOT_BG"])


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours."""
    def lum(c: str) -> float:
        q = QColor(c)
        out = 0.0
        for w, v in zip((0.2126, 0.7152, 0.0722), (q.redF(), q.greenF(), q.blueF())):
            out += w * (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
        return out
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class PreferencesDialog(QDialog):
    """Per-theme line colours (data ports, bookmark pairs, Capture DP/DN, Timing
    flavours) and the Audio / Capture line weights."""

    def __init__(self, prefs: dict, parent=None, mode: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences — Waveforms")
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
        if mode == "light":
            self._tabs.setCurrentIndex(1)
        lay.addWidget(self._tabs)

        weights = QGroupBox("Line weight (px), both themes")
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
        self._reset_btn.setToolTip("Every colour in both themes, and both weights")
        buttons.addButton(self._reset_btn, QDialogButtonBox.ResetRole)
        self._reset_btn.clicked.connect(self.reset_to_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

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
        self._prefs = {"colors": {}, "weights": {}}
        for (mode, kind, i) in self._swatches:
            self._paint(mode, kind, i)
        for kind, spin in self._weight_spins.items():
            spin.setValue(line_style.DEFAULT_WEIGHTS[kind])

    def preferences(self) -> dict:
        """The edited settings, for line_style.set_preferences."""
        return copy.deepcopy(self._prefs)
