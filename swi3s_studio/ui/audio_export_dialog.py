"""Audio export options dialog: pick (device, dataport) streams, a range, and a
per-dataport resample (decimation) rate."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from .port_names import with_name

# Standard decimation presets offered per stream (label, Hz). None = native rate.
_RATE_PRESETS = [("Native", None), ("8 kHz", 8000), ("16 kHz", 16000),
                 ("32 kHz", 32000), ("44.1 kHz", 44100), ("48 kHz", 48000),
                 ("96 kHz", 96000)]


# The highest rate a stream may be resampled to, unless its own rate is higher.
_MAX_RATE_HZ = 768_000
# The lowest rate a stream may be resampled to. A typed number with no unit below this
# value is in kHz.
_MIN_RATE_HZ = 1_000


def _parse_rate(text: str) -> Optional[int]:
    """Parse a rate combo's text to Hz, or None for native. A unit always wins: "Hz" is
    Hz, and "kHz" or a bare "k" is kHz ("48 kHz", "22.05k", "32000 Hz"). A number with no
    unit is in kHz below 1000 ("48", "44.1") and in Hz from 1000 up ("32000"). Raises
    ValueError for text that is not a positive, finite rate; rate_problem refuses one
    below _MIN_RATE_HZ."""
    raw = (text or "").strip()
    low = raw.lower()
    if not low or "native" in low:
        return None
    if low.endswith("khz"):
        number, scale = low[:-3], 1000.0
    elif low.endswith("hz"):
        number, scale = low[:-2], 1.0
    elif low.endswith("k"):
        number, scale = low[:-1], 1000.0
    else:
        number, scale = low, None
    try:
        value = float(number.strip())
        if scale is None:
            scale = 1000.0 if abs(value) < _MIN_RATE_HZ else 1.0
        hz = value * scale
        if not math.isfinite(hz) or round(hz) < 1:
            raise ValueError
        return int(round(hz))
    except (ValueError, OverflowError):
        raise ValueError(f"“{raw}” is not a sample rate") from None


def _khz(hz: float) -> str:
    """`hz` in kHz as plain decimal text, never in exponent form, to three decimals at most
    and without trailing zeros ("22.05", "1000000"); _parse_rate reads it back."""
    return f"{hz / 1000:.3f}".rstrip("0").rstrip(".")


class AudioExportDialog(QDialog):
    """Choose which (device, dataport) audio streams and which range to export,
    each with its own optional resample rate.

    Range: whole capture, or the visible window the audio view is currently
    showing. `visible_sample_range` supplies that window as a capture-sample span
    (s0, s1); index_range(dev, dp) maps it to each stream's own [i0, i1) slice, as
    streams differ in rate and start position.

    A typed resample rate must be 1 kHz up to the larger of the stream's own rate and
    768 kHz, read as _parse_rate reads it; accept() names the first stream whose rate is
    not.

    `default_rates` ({(dev, dp): Hz}) seeds each stream's rate combo — typically
    the per-dataport decimation chosen for playback, so export matches playback.
    """

    def __init__(self, store, parent=None, default_rates: Optional[Dict[Tuple[int, int], int]] = None,
                 names=None,
                 visible_sample_range: Optional[Tuple[int, int]] = None) -> None:
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
            cb = QCheckBox(with_name(names or {}, dev, dp, f"Device {dev} · DP{dp}")
                           + f"  ({len(chs)} ch, "
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
        self._visible_samples: Optional[Tuple[int, int]] = None
        if visible_sample_range is not None:
            self._visible_samples = (int(visible_sample_range[0]),
                                     int(visible_sample_range[1]))
            self._range.addItem("Visible range (audio view)", self._visible_samples)
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
            combo.setEditText(f"{_khz(hz)} kHz")

    def selected_streams(self) -> List[Tuple[int, int]]:
        return [k for k, cb in self._checks.items() if cb.isChecked()]

    def sample_range(self) -> Optional[Tuple[int, int]]:
        """The chosen capture-sample span (s0, s1), or None for the whole capture."""
        return self._range.currentData()

    def index_range(self, dev: Optional[int] = None,
                    dp: Optional[int] = None) -> Optional[Tuple[int, int]]:
        """The chosen [i0, i1) slice of stream (dev, dp), or None for the whole
        capture. A capture-sample window maps through that stream's own sample
        positions, so it needs `dev` and `dp`; without them it raises ValueError
        rather than return indices that belong to no particular stream."""
        rng = self._range.currentData()
        if rng is None:
            return None
        if dev is None or dp is None:
            raise ValueError("a capture-sample window maps to indices per stream: "
                             "pass the stream's device and data port")
        return self._store.stream_index_range(int(dev), int(dp), *rng)

    def target_rate(self, dev: int, dp: int) -> Optional[int]:
        """Chosen resample rate in Hz for one stream, or None for native. Raises
        ValueError for text that is not a rate."""
        combo = self._rates.get((int(dev), int(dp)))
        return _parse_rate(combo.currentText()) if combo is not None else None

    def rate_problem(self, dev: int, dp: int) -> str:
        """Why stream (dev, dp)'s typed rate cannot be used, or "" when it can."""
        try:
            hz = self.target_rate(dev, dp)
        except ValueError as exc:
            return str(exc)
        ceiling = max(_MAX_RATE_HZ, int(self._store.rate(dev, dp)))
        if hz is not None and hz > ceiling:
            return f"{_khz(hz)} kHz is above the {_khz(ceiling)} kHz limit"
        if hz is not None and hz < _MIN_RATE_HZ:
            return f"{hz} Hz is below the {_khz(_MIN_RATE_HZ)} kHz minimum"
        return ""

    def accept(self) -> None:
        for dev, dp in self.selected_streams():
            problem = self.rate_problem(dev, dp)
            if problem:
                QMessageBox.information(
                    self, "Export audio",
                    f"Device {dev} · DP{dp}: {problem}. Pick a preset, or type a rate "
                    "such as 22.05 kHz (a number with no unit is in kHz below 1000, "
                    "else in Hz).")
                return
        super().accept()
