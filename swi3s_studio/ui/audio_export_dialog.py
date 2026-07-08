"""Audio export options dialog: pick (device, dataport) streams, a range, and a
per-dataport resample (decimation) rate."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QGroupBox, QFormLayout, QLabel,
                               QVBoxLayout)

# Standard decimation presets offered per stream (label, Hz). None = native rate.
_RATE_PRESETS = [("Native", None), ("8 kHz", 8000), ("16 kHz", 16000),
                 ("32 kHz", 32000), ("44.1 kHz", 44100), ("48 kHz", 48000),
                 ("96 kHz", 96000)]


def _parse_rate(text: str) -> Optional[int]:
    """Parse a rate combo's text to Hz, or None for native. Accepts presets
    ("48 kHz"), bare Hz ("32000"), and typed kHz ("22.05 kHz")."""
    text = (text or "").strip().lower()
    if not text or "native" in text:
        return None
    text = text.replace("hz", "").strip()
    try:
        if "k" in text:
            return int(round(float(text.replace("k", "").strip()) * 1000))
        return int(round(float(text)))
    except ValueError:
        return None


class AudioExportDialog(QDialog):
    """Choose which (device, dataport) audio streams and which range to export,
    each with its own optional resample rate.

    Range: whole capture, or the visible window the audio view is currently
    showing, supplied as a sample-index range (i0, i1).

    `default_rates` ({(dev, dp): Hz}) seeds each stream's rate combo — typically
    the per-dataport decimation chosen for playback, so export matches playback.
    """

    def __init__(self, store, visible_index_range: Optional[Tuple[int, int]] = None,
                 parent=None, default_rates: Optional[Dict[Tuple[int, int], int]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export audio as WAV")
        self._store = store
        self._checks: Dict[Tuple[int, int], QCheckBox] = {}
        self._rates: Dict[Tuple[int, int], QComboBox] = {}
        default_rates = default_rates or {}

        lay = QVBoxLayout(self)
        box = QGroupBox("Audio streams (per-dataport resample)")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("<b>Stream</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Resample to</b>"), 0, 1)
        for r, (dev, dp) in enumerate(store.streams(), start=1):
            chs = store.channels(dev, dp)
            cb = QCheckBox(f"Device {dev} · DP{dp}  ({len(chs)} ch, "
                           f"{store.rate(dev, dp)/1000:.1f} kHz)")
            cb.setChecked(True)
            self._checks[(dev, dp)] = cb
            grid.addWidget(cb, r, 0)

            combo = QComboBox()
            combo.setEditable(True)
            for label, hz in _RATE_PRESETS:
                combo.addItem(label, hz)
            self._select_rate(combo, default_rates.get((dev, dp)))
            self._rates[(dev, dp)] = combo
            grid.addWidget(combo, r, 1)
        lay.addWidget(box)

        self._range = QComboBox()
        self._range.addItem("Whole capture", None)
        if visible_index_range is not None:
            self._range.addItem("Visible range (audio view)", visible_index_range)
        rbox = QGroupBox("Range")
        rform = QFormLayout(rbox)
        rform.addRow("Samples:", self._range)
        lay.addWidget(rbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    @staticmethod
    def _select_rate(combo: QComboBox, hz: Optional[int]) -> None:
        """Preselect the combo entry matching `hz` (or 'Native' when None)."""
        for i in range(combo.count()):
            if combo.itemData(i) == hz:
                combo.setCurrentIndex(i)
                return
        if hz:
            combo.setEditText(f"{hz/1000:g} kHz")

    def selected_streams(self) -> List[Tuple[int, int]]:
        return [k for k, cb in self._checks.items() if cb.isChecked()]

    def index_range(self) -> Optional[Tuple[int, int]]:
        return self._range.currentData()

    def target_rate(self, dev: int, dp: int) -> Optional[int]:
        """Chosen resample rate in Hz for one stream, or None for native."""
        combo = self._rates.get((int(dev), int(dp)))
        return _parse_rate(combo.currentText()) if combo is not None else None
