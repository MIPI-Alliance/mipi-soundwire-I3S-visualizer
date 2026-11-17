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

from ..ingest.raw_export import bin_paths
from .save_path import suffixed_save_path
from .theme import analyzer_stylesheet

# The channels Export Capture writes the clock and the data line to.
CLOCK_CHANNEL, DATA_CHANNEL = 0, 1

# (label, format key, extension, save-dialog filter)
_FORMATS = [
    ("Logic 2 project (.sal)", "sal", ".sal", "Logic 2 project (*.sal)"),
    ("Saleae binary (.bin, per channel)", "bin", ".bin", "Saleae binary (*.bin)"),
    ("Digital CSV (.csv)", "csv", ".csv", "Digital CSV (*.csv)"),
]

# (label, range key). 'all' = whole capture; the rest take a start/end pair.
_RANGES = [("Whole capture", "all"), ("Time (s)", "time"),
           ("Bus rows", "rows"), ("UI", "ui")]


class CaptureExportDialog(QDialog):
    """Choose how to export the open capture: format, signals + names, range, file.

    `session` supplies the range bounds and unit→sample conversions (last_sample,
    total_bus_rows, total_uis, sample_at_bus_row, sample_at_ui). `default_dir` seeds
    the file field; `clock_name`/`data_name` seed the two signal names and are what a
    blanked name field falls back to.

    A Bus rows / UI range is inclusive at both ends (From 5, To 5 is one row); a Time
    range runs from its start up to its end, and an end at the last edge keeps that
    edge."""

    def __init__(self, session, *, default_dir: str = "",
                 clock_name: str = "SW_CLK", data_name: str = "SW_DATA",
                 default_name: str = "capture", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Capture")
        self.setStyleSheet(analyzer_stylesheet())
        self._session = session
        self._clock_name = clock_name or "SW_CLK"
        self._data_name = data_name or "SW_DATA"
        self._default_dir = default_dir or ""
        self._default_name = default_name or "capture"   # e.g. "capture_Amp_bus" per Link
        self._confirmed: set = set()      # paths Browse's save dialog confirmed replacing

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
        self._end_label = QLabel("To:")
        rform.addRow("From:", self._start)
        rform.addRow(self._end_label, self._end)
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
        # Re-point the file field's extension to the chosen format: only one of the
        # dialog's own formats is replaced, so a dot inside a name ("run 1.5") stays.
        cur = self._path.text().strip()
        base = cur or os.path.join(self._default_dir, self._default_name)
        root, old = os.path.splitext(base)
        if old.lower() not in {e for _l, _k, e, _f in _FORMATS}:
            root = base
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
        self._end_label.setText("To:" if key == "time" else "To (inclusive):")
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
            path = self._suffixed(path)
        if path:
            self._confirmed.add(path)   # the save dialog confirmed replacing this name
            self._path.setText(path)

    def _suffixed(self, path: str) -> Optional[str]:
        """`path` with the format's extension, None when the user declines replacing it.
        A .bin export never writes `<name>.bin` itself (it writes one file per signal), so
        that name is added with no replace prompt; `output_files` confirms the files
        written."""
        ext = self._format.currentData()[1]
        if self.fmt() == "bin":
            return path if path.lower().endswith(ext) else path + ext
        return suffixed_save_path(self, path, ext, "Export Capture")

    # ---- results ----
    def fmt(self) -> str:
        return self._format.currentData()[0]

    def signal_names(self) -> Tuple[str, str]:
        return (self._clk_name.text().strip() or self._clock_name,
                self._dat_name.text().strip() or self._data_name)

    def include(self) -> Tuple[bool, bool]:
        return self._clk_on.isChecked(), self._dat_on.isChecked()

    def output_path(self) -> str:
        return self._path.text().strip()

    def output_files(self) -> list:
        """Every file the export writes: the output path, or for a .bin export one file
        per included signal (`raw_export.bin_paths`)."""
        path = self.output_path()
        if self.fmt() != "bin":
            return [path]
        clk_on, dat_on = self.include()
        return bin_paths(path, [ch for ch, on in ((CLOCK_CHANNEL, clk_on),
                                                  (DATA_CHANNEL, dat_on)) if on])

    def sample_range(self) -> Optional[Tuple[int, int]]:
        """The chosen export window as (s0, s1) capture samples, or None for the whole
        capture. Units are converted through the session."""
        key = self._range.currentData()
        if key == "all":
            return None
        s = self._session
        a, b = self._start.value(), self._end.value()
        last = s.last_sample()
        if key == "time":
            rate = s.sample_rate_hz
            s1 = int(round(b * rate))
            at_end = b >= self._end.maximum() or s1 >= last   # the spin box rounds its top
            return int(round(a * rate)), (last + 1 if at_end else s1)
        if key == "rows":
            return (s.sample_at_bus_row(int(a)),
                    last + 1 if int(b) + 1 >= s.total_bus_rows() else s.sample_at_bus_row(int(b) + 1))
        return (s.sample_at_ui(int(a)),                         # ui
                last + 1 if int(b) + 1 >= s.total_uis() else s.sample_at_ui(int(b) + 1))

    # ---- validation ----
    def accept(self) -> None:
        if not self.output_path():
            QMessageBox.information(self, "Export Capture", "Choose an output file.")
            return
        clk_on, dat_on = self.include()
        if not (clk_on or dat_on):
            QMessageBox.information(self, "Export Capture", "Select at least one signal.")
            return
        key = self._range.currentData()
        if key in ("rows", "ui") and self._end.value() < self._start.value():
            QMessageBox.information(self, "Export Capture",
                                    "The range's end must not be before its start.")
            return
        rng = self.sample_range()
        if rng is not None and rng[1] <= rng[0]:
            QMessageBox.information(self, "Export Capture",
                                    "The range's end must be after its start.")
            return
        # A typed path takes the format's extension as a Browse choice does.
        typed = self.output_path()
        if typed not in self._confirmed:
            path = self._suffixed(typed)
            if path is None:
                return
            if path != typed:
                self._confirmed.add(path)      # suffixed_save_path confirmed a replacement
                self._path.setText(path)
        # A file the export replaces is confirmed once here, unless the save dialog or the
        # suffix check already confirmed that name.
        existing = [os.path.basename(p) for p in self.output_files()
                    if os.path.exists(p) and p not in self._confirmed]
        if existing and QMessageBox.question(
                self, "Export Capture",
                "These files exist and will be replaced:\n" + "\n".join(existing)
                + "\n\nReplace them?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        super().accept()
