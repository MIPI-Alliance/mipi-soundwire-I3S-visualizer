"""Export Capture dialog: pick a format, which signals + their names, a range
(whole capture, or a time / bus-row / UI window), and the output file — in one
place. Dispatched by MainWindow.export_capture to Session.export_sal / export_bin
/ export_csv."""
from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

# (label, format key, extension, save-dialog filter)
_FORMATS = [
    ("Logic 2 project (.sal)", "sal", ".sal", "Logic 2 project (*.sal)"),
    ("Saleae binary (.bin, per channel)", "bin", ".bin", "Saleae binary (*.bin)"),
    ("Digital CSV (.csv)", "csv", ".csv", "Digital CSV (*.csv)"),
]

# (label, range key). Sample = whole capture; the rest take a start/end pair.
_RANGES = [("Whole capture", "all"), ("Time (s)", "time"),
           ("Bus rows", "rows"), ("UI", "ui")]


class CaptureExportDialog(QDialog):
    """Choose how to export the open capture: format, signals + names, range, file.

    `session` supplies the range bounds and unit→sample conversions (last_sample,
    total_bus_rows, total_uis, sample_at_bus_row, sample_at_ui). `default_dir` seeds
    the file field; `clock_name`/`data_name` seed the two signal names."""

    def __init__(self, session, *, default_dir: str = "",
                 clock_name: str = "SW_CLK", data_name: str = "SW_DATA",
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Capture")
        self._session = session
        self._default_dir = default_dir or ""

        lay = QVBoxLayout(self)

        # --- Format ---
        fbox = QGroupBox("Format")
        fform = QFormLayout(fbox)
        self._format = QComboBox()
        for label, key, ext, filt in _FORMATS:
            self._format.addItem(label, (key, ext, filt))
        fform.addRow("Save as:", self._format)
        lay.addWidget(fbox)

        # --- Signals (a SWI3S capture is two wires: clock/DP + data/DN) ---
        sbox = QGroupBox("Signals")
        sgrid = QGridLayout(sbox)
        sgrid.addWidget(QLabel("<b>Include</b>"), 0, 0)
        sgrid.addWidget(QLabel("<b>Name</b>"), 0, 1)
        self._clk_on = QCheckBox("Clock / DP")
        self._dat_on = QCheckBox("Data / DN")
        self._clk_name = QLineEdit(clock_name)
        self._dat_name = QLineEdit(data_name)
        for r, (cb, name) in enumerate(((self._clk_on, self._clk_name),
                                        (self._dat_on, self._dat_name)), start=1):
            cb.setChecked(True)
            sgrid.addWidget(cb, r, 0)
            sgrid.addWidget(name, r, 1)
        lay.addWidget(sbox)

        # --- Range ---
        rbox = QGroupBox("Range")
        rform = QFormLayout(rbox)
        self._range = QComboBox()
        for label, key in _RANGES:
            self._range.addItem(label, key)
        rform.addRow("Export:", self._range)
        self._start = QDoubleSpinBox()
        self._end = QDoubleSpinBox()
        for sp in (self._start, self._end):
            sp.setKeyboardTracking(False)
            sp.setGroupSeparatorShown(True)
        rform.addRow("From:", self._start)
        rform.addRow("To:", self._end)
        lay.addWidget(rbox)

        # --- File ---
        filebox = QGroupBox("File")
        fl = QHBoxLayout(filebox)
        self._path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        fl.addWidget(self._path, 1)
        fl.addWidget(browse)
        lay.addWidget(filebox)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._format.currentIndexChanged.connect(self._on_format_changed)
        self._range.currentIndexChanged.connect(self._on_range_changed)
        self._on_range_changed()
        self._on_format_changed()

    # ---- reactive UI ----
    def _on_format_changed(self) -> None:
        _key, ext, _filt = self._format.currentData()
        # Re-point the file field's extension to the chosen format.
        cur = self._path.text().strip()
        base = cur or os.path.join(self._default_dir, "capture")
        root, _old = os.path.splitext(base)
        self._path.setText(root + ext)
        # A .sal project stores the whole two-wire bus — both signals required; the raw
        # .bin/.csv formats can drop one. Force + lock the checkboxes for .sal.
        sal = self.fmt() == "sal"
        for cb in (self._clk_on, self._dat_on):
            if sal:
                cb.setChecked(True)
            cb.setEnabled(not sal)
            cb.setToolTip("A .sal project stores both wires of the bus" if sal else "")

    def _on_range_changed(self) -> None:
        key = self._range.currentData()
        s = self._session
        if key == "all":
            for sp in (self._start, self._end):
                sp.setEnabled(False)
            return
        if key == "time":
            hi = s.last_sample() / s.sample_rate_hz if s.sample_rate_hz else 0.0
            dec, step, suffix = 6, 0.001, " s"
        elif key == "rows":
            hi = float(max(0, s.total_bus_rows() - 1))
            dec, step, suffix = 0, 1.0, ""
        else:                                            # ui
            hi = float(max(0, s.total_uis() - 1))
            dec, step, suffix = 0, 1.0, ""
        for sp in (self._start, self._end):
            sp.setEnabled(True)
            sp.setDecimals(dec)
            sp.setSingleStep(step)
            sp.setSuffix(suffix)
            sp.setRange(0.0, hi)
        self._start.setValue(0.0)
        self._end.setValue(hi)

    def _browse(self) -> None:
        _key, _ext, filt = self._format.currentData()
        start = self._path.text().strip() or self._default_dir
        path, _ = QFileDialog.getSaveFileName(self, "Export Capture", start, filt)
        if path:
            self._path.setText(path)

    # ---- results ----
    def fmt(self) -> str:
        return self._format.currentData()[0]

    def signal_names(self) -> Tuple[str, str]:
        return self._clk_name.text().strip() or "SW_CLK", self._dat_name.text().strip() or "SW_DATA"

    def include(self) -> Tuple[bool, bool]:
        return self._clk_on.isChecked(), self._dat_on.isChecked()

    def output_path(self) -> str:
        return self._path.text().strip()

    def sample_range(self) -> Optional[Tuple[int, int]]:
        """The chosen export window as (s0, s1) capture samples, or None for the whole
        capture. Units are converted through the session."""
        key = self._range.currentData()
        if key == "all":
            return None
        s = self._session
        a, b = self._start.value(), self._end.value()
        if key == "time":
            rate = s.sample_rate_hz
            return int(round(a * rate)), int(round(b * rate))
        if key == "rows":
            return s.sample_at_bus_row(int(a)), s.sample_at_bus_row(int(b))
        return s.sample_at_ui(int(a)), s.sample_at_ui(int(b))     # ui

    # ---- validation ----
    def accept(self) -> None:
        if not self.output_path():
            QMessageBox.information(self, "Export Capture", "Choose an output file.")
            return
        clk_on, dat_on = self.include()
        if not (clk_on or dat_on):
            QMessageBox.information(self, "Export Capture", "Select at least one signal.")
            return
        rng = self.sample_range()
        if rng is not None and rng[1] <= rng[0]:
            QMessageBox.information(self, "Export Capture",
                                    "The range's end must be after its start.")
            return
        super().accept()
