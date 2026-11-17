"""Preferences ▸ Timing Diagrams: the term diagram's and the whole-inequality view's colours
per theme, line weights and dash styles, sizes and fonts (`diagram_style`).

Edits a working copy of the settings and emits `changed` with it after every edit, for a live
preview; the dialog stores it on OK. A weight or a size is shown in px at the diagram's full
scale (`units`: its line height and body font size), and kept as a fraction of the line height
or of the body font, so it scales with the font. A colour below 3:1 on that theme's diagram
background is allowed and flagged. Export Settings… writes every setting, defaults included,
as JSON; Import Settings… reads such a file, each entry it skips or clamps an import warning.
"""
from __future__ import annotations

import copy
import math
from typing import Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QFontMetricsF
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import import_notes
from . import diagram_style as ds
from .save_path import suffixed_save_path
from .theme import _PALETTES, contrast

MIN_CONTRAST = 3.0
_SWATCH = 28
_MAX_FILE_BYTES = 1 << 20      # a settings file, at most
MODES = ("dark", "light")

# The Lines grid: label, colour key, weight key, dash key, size key and its spin's prefix.
LINE_ROWS: tuple[tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], str],
                 ...] = (
    ("Clock waveform", "clock", "clock", None, None, ""),
    ("Data, the Manager driving", "data", "data", None, None, ""),
    ("Data, a Peripheral (Peripheral A) driving", "data_per", None, None, None, ""),
    ("Data, Peripheral B driving", "data_per_b", None, None, None, ""),
    ("High-Z stretch (its signal's weight)", None, None, "high_z", None, ""),
    ("V_DD rails", "vdd", "vdd", "vdd", None, ""),
    ("GND rails", "gnd", "gnd", "gnd", None, ""),
    ("Receiver's offset GND", "gnd_offset", "gnd_offset", "gnd_offset", None, ""),
    ("20 % levels (references, anchors)", "reference", "reference", "reference", None, ""),
    ("80 % levels", "valid", "valid", "valid", None, ""),
    ("Thresholds (V_IH, V_IL)", "threshold", "threshold", "threshold", "threshold.head",
     "head "),
    ("Guides from a point", "guide_dot", "guide_dot", "guide_dot", None, ""),
    ("Guides from a point on no ramp", "guide_dash", "guide_dash", "guide_dash", None, ""),
    ("Span arrows", "span", "span", None, "span.head", "head "),
    ("Interval arrows", "interval", "interval", None, "interval.head", "head "),
    ("Flight arrows (their signal's colour)", None, "flight", None, "flight.head", "head "),
    ("Margin, a gap", "margin_ok", "margin", None, "margin.head", "head "),
    ("Margin, short", "margin_bad", None, None, None, ""),
    ("Release tick", "release", "release", None, "release.length", "length "),
    ("Keeper's response", "keeper", None, None, None, ""),
    ("Crossing dots (their level's colour)", None, None, None, "dot.radius", "radius "),
    ("Time axis", "axis", "axis", None, "axis.tick", "tick "),
    ("Label leader lines (their label's colour)", None, "leader", None, None, ""),
    ("Title and headings", "text", None, None, None, ""),
    ("Notes, high-Z and point labels", "dim_text", None, None, None, ""),
)


def diagram_bg(mode: str) -> str:
    """The diagram's background in theme `mode`."""
    return str(_PALETTES[mode]["FRAME_BG"])


def default_units() -> tuple[float, int]:
    """The application font's line height and pixel size: the units shown where no diagram
    gives its own."""
    f = QFont()
    return QFontMetricsF(f).height(), f.pixelSize()


class DiagramStylePage(QWidget):
    """The Timing Diagrams tab; `preferences()` is the working copy."""

    changed = Signal(dict)

    def __init__(self, prefs: dict, units: Optional[tuple[float, int]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._prefs = copy.deepcopy(prefs)
        self._prefs.setdefault("colors", {})
        self._prefs.setdefault("values", {})
        self._lh, self._px = 1.0, 1
        self._swatches: Dict[tuple[str, str], QToolButton] = {}    # (mode, key) -> button
        self._warnings: Dict[tuple[str, str], QLabel] = {}
        self._spins: Dict[str, QAbstractSpinBox] = {}              # value key -> spin box
        self._combos: Dict[str, QComboBox] = {}                    # value key -> combo box
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.addWidget(self._lines_box())
        lay.addWidget(self._fonts_box())
        note = QLabel(f"Swatches sit on each theme's diagram background; ⚠ marks a colour "
                      f"below {MIN_CONTRAST:g}:1 on it. Weights and sizes are in px at the "
                      f"diagram's full size, and follow its font.")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        scroll.setWidget(body)
        scroll.setMinimumSize(body.sizeHint().width() + scroll.verticalScrollBar().sizeHint()
                              .width() + 4, 480)
        outer.addWidget(scroll, 1)
        files = QHBoxLayout()
        self.export_btn = QPushButton("Export Settings…")
        self.export_btn.setToolTip("Write every Timing Diagrams setting to a JSON file")
        self.export_btn.clicked.connect(self._ask_export)
        self.import_btn = QPushButton("Import Settings…")
        self.import_btn.setToolTip("Read Timing Diagrams settings from a JSON file")
        self.import_btn.clicked.connect(self._ask_import)
        files.addWidget(self.export_btn)
        files.addWidget(self.import_btn)
        files.addStretch(1)
        outer.addLayout(files)
        self.set_units(*(units or default_units()))

    # ---- layout ----
    def _lines_box(self) -> QGroupBox:
        box = QGroupBox("Lines")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        for col, text in enumerate(("", "Dark", "Light", "Weight", "Style", "Size"), start=0):
            if text:
                grid.addWidget(QLabel(f"<b>{text}</b>"), 0, col)
        for row, (label, ckey, wkey, dkey, skey, prefix) in enumerate(LINE_ROWS, start=1):
            grid.addWidget(QLabel(label), row, 0)
            if ckey:
                for col, mode in enumerate(MODES, start=1):
                    grid.addWidget(self._swatch(mode, ckey, label), row, col)
            if wkey:
                grid.addWidget(self._number(f"{wkey}.weight"), row, 3)
            if dkey:
                grid.addWidget(self._choice(f"{dkey}.dash", ds.DASHES, ds.DASH_LABELS), row, 4)
            if skey:
                spin = self._number(skey)
                spin.setPrefix(prefix)
                grid.addWidget(spin, row, 5)
        grid.setColumnStretch(0, 1)
        return box

    def _fonts_box(self) -> QGroupBox:
        box = QGroupBox("Fonts")
        grid = QGridLayout(box)
        for col, text in enumerate(("Family", "Size", "Weight"), start=1):
            grid.addWidget(QLabel(f"<b>{text}</b>"), 0, col)
        families = QFontDatabase.families()
        for row, (key, label, _size, _weight) in enumerate(ds.FONTS, start=1):
            grid.addWidget(QLabel(label), row, 0)
            fam = QComboBox()
            fam.addItem("Default", "")
            for name in families:
                fam.addItem(name, name)
            fam.setMaxVisibleItems(20)
            fam.currentIndexChanged.connect(
                lambda _i, k=f"font.{key}.family", c=fam: self._set(k, c.currentData()))
            self._combos[f"font.{key}.family"] = fam
            grid.addWidget(fam, row, 1)
            grid.addWidget(self._number(f"font.{key}.size"), row, 2)
            grid.addWidget(self._choice(f"font.{key}.weight", ds.FONT_WEIGHTS,
                                        ds.FONT_WEIGHT_LABELS), row, 3)
        row = len(ds.FONTS) + 1
        grid.addWidget(QLabel("Labels in a narrow column"), row, 0)
        grid.addWidget(self._number("label.narrow"), row, 2)
        grid.setColumnStretch(0, 1)
        return box

    def _swatch(self, mode: str, key: str, label: str) -> QWidget:
        cell = QWidget()
        cl = QVBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(1)
        btn = QToolButton()
        btn.setFixedSize(_SWATCH * 2, _SWATCH)
        btn.clicked.connect(lambda _c=False, m=mode, k=key, t=label: self._pick(m, k, t))
        warn = QLabel()
        cl.addWidget(btn)
        cl.addWidget(warn)
        self._swatches[(mode, key)] = btn
        self._warnings[(mode, key)] = warn
        return cell

    def _number(self, key: str) -> QAbstractSpinBox:
        kind = ds.VALUES[key][1]
        shown = "percent" if key == "label.narrow" else kind
        decimals, step, suffix = {"weight": (2, 0.25, " px"), "length": (2, 0.25, " px"),
                                  "font_size": (0, 1.0, " px"),
                                  "percent": (0, 5.0, " %")}[shown]
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda v, k=key: self._spun(k, v))
        self._spins[key] = spin
        return spin

    def _choice(self, key: str, names: tuple[str, ...], labels: Dict[str, str]) -> QComboBox:
        combo = QComboBox()
        for name in names:
            combo.addItem(labels[name], name)
        combo.currentIndexChanged.connect(
            lambda _i, k=key, c=combo: self._set(k, c.currentData()))
        self._combos[key] = combo
        return combo

    # ---- units: a stored value and the number shown ----
    def set_units(self, lh: float, px: int) -> None:
        """Show weights and sizes in px at a line height of `lh` px and font sizes against a
        body font of `px` px (its pixel size measured where `px` is 0)."""
        self._lh = max(float(lh), 1.0)
        self._px = px if px > 0 else max(QFontInfo(QFont()).pixelSize(), 1)
        for key, spin in self._spins.items():
            _dflt, kind, lo, hi = ds.VALUES[key]
            k = self._factor(key)
            assert isinstance(spin, QDoubleSpinBox)
            spin.blockSignals(True)
            if kind == "font_size":
                spin.setRange(math.ceil(lo * k), math.floor(hi * k))
            else:
                spin.setRange(lo * k, hi * k)
            spin.blockSignals(False)
        self._refresh()

    def _factor(self, key: str) -> float:
        """A stored value times this is the number shown."""
        kind = ds.VALUES[key][1]
        if kind in ("weight", "length"):
            return self._lh
        if kind == "font_size":
            return float(self._px)
        if key == "label.narrow":
            return 100.0
        return 1.0

    def _spun(self, key: str, shown: float) -> None:
        self._set(key, shown / self._factor(key))

    # ---- values ----
    def value(self, key: str):
        """A setting as the working copy holds it: its choice, else the default."""
        return self._prefs["values"].get(key, ds.default(key))

    def color(self, mode: str, key: str) -> str:
        """The colour shown for one entry: the working copy's choice, else the default."""
        return self._prefs["colors"].get(mode, {}).get(key) or ds.default_color(key, mode)

    def set_value(self, key: str, v) -> None:
        """Set one `diagram_style.VALUES` setting, as stored (a fraction, a name)."""
        self._set(key, v)
        self._refresh()

    def set_color(self, mode: str, key: str, colour: str) -> None:
        self._prefs["colors"].setdefault(mode, {})[key] = QColor(colour).name()
        self._paint(mode, key)
        self._emit()

    def _set(self, key: str, v) -> None:
        if self._loading:
            return
        self._prefs["values"][key] = v
        self._emit()

    def low_contrast(self, mode: str, key: str) -> bool:
        return contrast(self.color(mode, key), diagram_bg(mode)) < MIN_CONTRAST

    def preferences(self) -> dict:
        """The edited settings, for diagram_style.set_preferences."""
        return copy.deepcopy(self._prefs)

    def set_preferences(self, prefs: dict) -> None:
        """Put `prefs` in the working copy, show it and emit `changed`."""
        self._prefs = copy.deepcopy(prefs)
        self._prefs.setdefault("colors", {})
        self._prefs.setdefault("values", {})
        self._refresh()
        self._emit()

    def reset_to_defaults(self) -> None:
        """Every Timing Diagrams setting back to its default."""
        self.set_preferences({"colors": {}, "values": {}})

    def _emit(self) -> None:
        if not self._loading:
            self.changed.emit(self.preferences())

    def _refresh(self) -> None:
        """Show the working copy in every control, emitting nothing."""
        self._loading = True
        try:
            for mode, key in self._swatches:
                self._paint(mode, key)
            for key, spin in self._spins.items():
                assert isinstance(spin, QDoubleSpinBox)
                spin.setValue(float(self.value(key)) * self._factor(key))
            for key, combo in self._combos.items():
                v = self.value(key)
                i = combo.findData(v)
                if i < 0:                  # a family not installed here: shown as itself
                    combo.addItem(f"{v} (not installed)", v)
                    i = combo.count() - 1
                combo.setCurrentIndex(i)
        finally:
            self._loading = False

    def _paint(self, mode: str, key: str) -> None:
        c = self.color(mode, key)
        bg = diagram_bg(mode)
        btn = self._swatches[(mode, key)]
        # The colour as a thick bar on the theme's own diagram background: how it will read.
        btn.setStyleSheet(f"QToolButton {{ background-color: {c}; border: 6px solid {bg};"
                          f" border-top-width: 10px; border-bottom-width: 10px; }}")
        ratio = contrast(c, bg)
        btn.setToolTip(f"{c} — {ratio:.1f}:1 on {bg}")
        self._warnings[(mode, key)].setText(f"⚠ {ratio:.1f}:1" if ratio < MIN_CONTRAST else "")

    def _pick(self, mode: str, key: str, label: str) -> None:
        c = QColorDialog.getColor(QColor(self.color(mode, key)), self,
                                  f"{label} ({mode} theme)")
        if c.isValid():
            self.set_color(mode, key, c.name())

    # ---- files ----
    def export_settings(self, path: str) -> None:
        """Write every setting the working copy gives, defaults included, to `path`.
        Raises OSError where it cannot be written."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(ds.to_json(self._prefs))

    def import_settings(self, path: str) -> list:
        """Read the settings file at `path` into the working copy; returns the import
        warnings (import_notes.ImportNote), each entry skipped or clamped. Raises OSError or
        ValueError where the file cannot be read as settings, the working copy unchanged."""
        with open(path, "rb") as f:
            data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(f"the file is larger than {_MAX_FILE_BYTES >> 10} KiB")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"the file is not UTF-8 text ({exc.reason})") from None
        with import_notes.collect() as notes:
            prefs = ds.from_json(text, import_notes.source_name(path))
        self.set_preferences(prefs)
        return list(notes)

    def _ask_export(self) -> None:
        title = "Export Diagram Settings"
        path, _ = QFileDialog.getSaveFileName(self, title, "timing_diagram_settings.json",
                                              "JSON (*.json)")
        if not path:
            return
        path = suffixed_save_path(self, path, ".json", title)
        if not path:
            return
        try:
            self.export_settings(path)
        except OSError as exc:
            QMessageBox.critical(self, title, f"Export failed: {exc}")

    def _ask_import(self) -> None:
        title = "Import Diagram Settings"
        path, _ = QFileDialog.getOpenFileName(self, title, "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            notes = self.import_settings(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, title, f"Import failed: {exc}")
            return
        if notes:
            from .import_warnings_dialog import ImportWarningsDialog
            # A child of the dialog, so it can be read while the dialog is open.
            ImportWarningsDialog(notes, import_notes.source_name(path), self.window()).show()
