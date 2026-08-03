"""Analyzer ▸ Open Analog CSV dialog.

The CSV (a Tektronix scope analog export) is read first so the actual channel
names in the file (e.g. "CH1", "CH2", "CH3", "CH4") populate the clock/data
dropdowns — like .wfm, the "busier = clock" heuristic doesn't hold in general,
so the assignment is always an explicit user pick, never auto-detected.

Also offers an optional per-channel Schmitt threshold (hi, lo) override; left
blank, Session.from_analog_csv auto-detects each channel's own mid-rail +
hysteresis band (see ingest.analog.auto_threshold).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class OpenAnalogCsvDialog(QDialog):
    """Result attributes (valid after exec() returns Accepted):
        clock         : str             — channel name assigned as the clock
        data          : str             — channel name assigned as the data line
        clock_thresh  : (hi, lo) | None — None => auto-detect
        data_thresh   : (hi, lo) | None — None => auto-detect
    """

    def __init__(self, parent, *, channel_names: List[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open Analog CSV")
        self.setMinimumWidth(420)
        self.clock = ""
        self.data = ""
        self.clock_thresh: Optional[Tuple[float, float]] = None
        self.data_thresh: Optional[Tuple[float, float]] = None

        root = QVBoxLayout(self)

        pick = QGroupBox("Channel assignment")
        pl = QFormLayout(pick)
        self._clock_box = QComboBox()
        self._data_box = QComboBox()
        self._clock_box.addItems(channel_names)
        self._data_box.addItems(channel_names)
        if len(channel_names) > 1:
            self._data_box.setCurrentIndex(1)
        pl.addRow("Clock channel:", self._clock_box)
        pl.addRow("Data channel:", self._data_box)
        root.addWidget(pick)

        thr = QGroupBox("Threshold override (optional — blank = auto per channel)")
        tl = QFormLayout(thr)
        self._clock_hi = QLineEdit()
        self._clock_lo = QLineEdit()
        self._data_hi = QLineEdit()
        self._data_lo = QLineEdit()
        for e in (self._clock_hi, self._clock_lo, self._data_hi, self._data_lo):
            e.setPlaceholderText("auto")
        tl.addRow("Clock hi / lo (V):", self._pair_row(self._clock_hi, self._clock_lo))
        tl.addRow("Data hi / lo (V):", self._pair_row(self._data_hi, self._data_lo))
        root.addWidget(thr)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    @staticmethod
    def _pair_row(hi: QLineEdit, lo: QLineEdit) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(hi)
        row.addWidget(QLabel("/"))
        row.addWidget(lo)
        return row

    @staticmethod
    def _parse_thresh(hi_edit: QLineEdit, lo_edit: QLineEdit) -> Optional[Tuple[float, float]]:
        hi, lo = hi_edit.text().strip(), lo_edit.text().strip()
        if not hi and not lo:
            return None
        try:
            return float(hi), float(lo)
        except ValueError:
            return None          # malformed override => fall back to auto rather than crash

    def _accept(self) -> None:
        self.clock = self._clock_box.currentText()
        self.data = self._data_box.currentText()
        if not self.clock or not self.data or self.clock == self.data:
            return                # keep the dialog open — need two distinct channels
        self.clock_thresh = self._parse_thresh(self._clock_hi, self._clock_lo)
        self.data_thresh = self._parse_thresh(self._data_hi, self._data_lo)
        self.accept()
