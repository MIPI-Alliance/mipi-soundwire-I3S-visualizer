"""High-Pass & Gain: one audio stream's 4th-order zero-phase Butterworth high-pass and its
gain, laid out like Audacity's Amplify.

Amplification and New Peak Amplitude are two views of one number, both in dB: the peak is
measured after the high-pass, so changing the corner moves it, and while the gain has not
been edited it follows the peak to keep the stream normalised to 0 dBFS. With Allow
clipping off, a gain that would take the peak above full scale cannot be applied, as in
Audacity. Preview shows the result in the waveform while the dialog is open; Cancel puts
back what was there, and Apply keeps it. Revert, offered when the stream has a setting,
removes it and returns the decoded samples.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from ..dsp import filters
from ..store.audio_store import StreamProcessing

_GAIN_LIMIT_DB = 100.0      # the field's range; the slider covers the common ±50
_SLIDER_DB = 50.0
_LOG_STEPS = 1000           # corner slider resolution, log-spaced from 1 Hz to the maximum
_DEFAULT_CORNER_HZ = 20.0
_CLIP_EPS_DB = 1e-6         # a normalising gain lands exactly on 0 dB, not just above it


class StreamProcessingDialog(QDialog):
    def __init__(self, store, device: int, dp: int,
                 preview: Callable[[Optional[StreamProcessing]], None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"High-Pass & Gain: Dev{device} DP{dp}")
        self._store, self._dev, self._dp = store, int(device), int(dp)
        self._preview_fn = preview
        self._original = store.processing(device, dp)
        self._previewed = False
        self._reverted = False
        self._gain_touched = self._original is not None
        self._peak: Optional[float] = None
        rate = float(store.rate(device, dp) or 0.0)
        filterable = rate > 2 * filters.MIN_CUTOFF_HZ    # an unknown rate has no Nyquist
        self._max_hz = filters.max_cutoff_hz(rate) if filterable else filters.MIN_CUTOFF_HZ * 2
        start = self._original or StreamProcessing(
            highpass_hz=_DEFAULT_CORNER_HZ if filterable else None)

        lay = QVBoxLayout(self)

        hbox = QGroupBox("High-pass filter")
        hform = QFormLayout(hbox)
        self._hp_on = QCheckBox("4th-order Butterworth, zero phase")
        self._hp_on.setChecked(start.highpass_hz is not None)
        hform.addRow(self._hp_on)
        self._corner = QDoubleSpinBox()
        self._corner.setRange(filters.MIN_CUTOFF_HZ, self._max_hz)
        self._corner.setDecimals(1)
        self._corner.setSuffix(" Hz")
        self._corner.setValue(min(start.highpass_hz or _DEFAULT_CORNER_HZ, self._max_hz))
        hform.addRow("Corner frequency:", self._corner)
        self._corner_slider = QSlider(Qt.Horizontal)
        self._corner_slider.setRange(0, _LOG_STEPS)
        hform.addRow(self._corner_slider)
        hform.addRow(QLabel(f"From {filters.MIN_CUTOFF_HZ:g} Hz to {self._max_hz:g} Hz "
                            f"(this stream is {rate / 1000:g} kHz)." if filterable else
                            "The stream's sample rate is unknown, so it cannot be filtered."))
        self._hp_on.setEnabled(filterable)
        lay.addWidget(hbox)

        gbox = QGroupBox("Gain")
        gform = QFormLayout(gbox)
        self._gain = QDoubleSpinBox()
        self._gain.setRange(-_GAIN_LIMIT_DB, _GAIN_LIMIT_DB)
        self._gain.setDecimals(1)
        self._gain.setSingleStep(0.5)
        gform.addRow("Amplification (dB):", self._gain)
        self._gain_slider = QSlider(Qt.Horizontal)
        self._gain_slider.setRange(int(-_SLIDER_DB * 10), int(_SLIDER_DB * 10))
        gform.addRow(self._gain_slider)
        self._new_peak = QDoubleSpinBox()
        self._new_peak.setRange(-_GAIN_LIMIT_DB * 2, _GAIN_LIMIT_DB)
        self._new_peak.setDecimals(1)
        self._new_peak.setSingleStep(0.5)
        gform.addRow("New Peak Amplitude (dB):", self._new_peak)
        self._clip = QCheckBox("Allow clipping")
        self._clip.setChecked(start.allow_clipping)
        self._clip.setToolTip("Let the gain take the peak above 0 dB. The samples then "
                              "saturate at full scale, as a DAC would.")
        gform.addRow(self._clip)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        # Two lines reserved, so a longer message never resizes or clips the dialog.
        self._note.setMinimumHeight(2 * self._note.fontMetrics().lineSpacing())
        gform.addRow(self._note)
        lay.addWidget(gbox)

        buttons = QHBoxLayout()
        self._revert_btn = QPushButton("Revert")
        self._revert_btn.setEnabled(self._original is not None)
        self._revert_btn.setToolTip("Remove this stream's high-pass and gain: the decoded "
                                    "samples again.")
        self._preview_btn = QPushButton("Preview")
        cancel = QPushButton("Cancel")
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setDefault(True)
        for b in (self._revert_btn, self._preview_btn, cancel, self._apply_btn):
            buttons.addWidget(b)
        lay.addLayout(buttons)

        # Measuring the peak filters the whole stream, so typing a corner waits for a pause.
        self._remeasure = QTimer(self)
        self._remeasure.setSingleShot(True)
        self._remeasure.setInterval(200)
        self._remeasure.timeout.connect(self._measure)

        self._hp_on.toggled.connect(self._on_filter_changed)
        self._corner.valueChanged.connect(self._on_corner_spin)
        self._corner_slider.valueChanged.connect(self._on_corner_slider)
        self._gain.valueChanged.connect(self._on_gain)
        self._gain_slider.valueChanged.connect(self._on_gain_slider)
        self._new_peak.valueChanged.connect(self._on_new_peak)
        self._clip.toggled.connect(lambda _on: self._update_state())
        self._revert_btn.clicked.connect(self.revert)
        self._preview_btn.clicked.connect(self.preview)
        cancel.clicked.connect(self.reject)
        self._apply_btn.clicked.connect(self.accept)

        self._set_corner_slider(self._corner.value())
        self._corner.setEnabled(self._hp_on.isChecked())
        self._corner_slider.setEnabled(self._hp_on.isChecked())
        self._set_gain(start.gain_db)
        self._measure()

    # ---- the setting ----
    def processing(self) -> StreamProcessing:
        return StreamProcessing(
            highpass_hz=self._corner.value() if self._hp_on.isChecked() else None,
            gain_db=round(self._gain.value(), 6),
            allow_clipping=self._clip.isChecked())

    def peak_db(self) -> Optional[float]:
        """The stream's peak after the high-pass as set, before gain (None: silent)."""
        return self._peak

    def clips(self) -> bool:
        return self._peak is not None and self._peak + self._gain.value() > _CLIP_EPS_DB

    def chosen(self) -> Optional[StreamProcessing]:
        """What an accepted dialog asks for: the setting, or None after Revert."""
        return None if self._reverted else self.processing()

    # ---- buttons ----
    def revert(self) -> None:
        self._reverted = True
        super().accept()

    def preview(self) -> None:
        if not self._apply_btn.isEnabled():
            return
        self._previewed = True
        self._preview_fn(self.processing())

    def accept(self) -> None:
        if not self._apply_btn.isEnabled():
            return
        super().accept()

    def reject(self) -> None:
        if self._previewed:
            self._preview_fn(self._original)       # put back what was there
            self._previewed = False
        super().reject()

    # ---- keeping the fields consistent ----
    def _measure(self) -> None:
        hp = self._corner.value() if self._hp_on.isChecked() else None
        self._peak = self._store.peak_db(self._dev, self._dp, highpass_hz=hp)
        if self._peak is not None and not self._gain_touched:
            self._set_gain(-self._peak)             # normalise to 0 dBFS
        self._sync_new_peak()
        self._update_state()

    def _on_filter_changed(self, _on=None) -> None:
        self._corner.setEnabled(self._hp_on.isChecked())
        self._corner_slider.setEnabled(self._hp_on.isChecked())
        self._remeasure.start()

    def _on_corner_spin(self, hz: float) -> None:
        self._set_corner_slider(hz)
        self._remeasure.start()

    def _on_corner_slider(self, pos: int) -> None:
        hz = 10.0 ** (math.log10(filters.MIN_CUTOFF_HZ)
                      + pos / _LOG_STEPS * (math.log10(self._max_hz)
                                            - math.log10(filters.MIN_CUTOFF_HZ)))
        self._corner.blockSignals(True)
        self._corner.setValue(hz)
        self._corner.blockSignals(False)
        self._remeasure.start()

    def _set_corner_slider(self, hz: float) -> None:
        lo, hi = math.log10(filters.MIN_CUTOFF_HZ), math.log10(self._max_hz)
        pos = 0 if hi <= lo else round((math.log10(max(hz, 1e-9)) - lo) / (hi - lo) * _LOG_STEPS)
        self._corner_slider.blockSignals(True)
        self._corner_slider.setValue(max(0, min(_LOG_STEPS, pos)))
        self._corner_slider.blockSignals(False)

    def _set_gain(self, db: float) -> None:
        db = max(-_GAIN_LIMIT_DB, min(_GAIN_LIMIT_DB, db))
        for w, v in ((self._gain, db), (self._gain_slider, round(db * 10))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self._sync_new_peak()
        self._update_state()

    def _on_gain(self, db: float) -> None:
        self._gain_touched = True
        self._set_gain(db)

    def _on_gain_slider(self, pos: int) -> None:
        self._gain_touched = True
        self._set_gain(pos / 10.0)

    def _on_new_peak(self, db: float) -> None:
        if self._peak is None:
            return
        self._gain_touched = True
        self._set_gain(db - self._peak)

    def _sync_new_peak(self) -> None:
        self._new_peak.blockSignals(True)
        if self._peak is not None:
            self._new_peak.setValue(self._peak + self._gain.value())
        self._new_peak.blockSignals(False)
        self._new_peak.setEnabled(self._peak is not None)

    def _update_state(self) -> None:
        blocked = self.clips() and not self._clip.isChecked()
        self._apply_btn.setEnabled(not blocked)
        self._preview_btn.setEnabled(not blocked)
        if self._peak is None:
            self._note.setText("The stream is silent: there is no peak to normalise to.")
        elif blocked:
            self._note.setText("The peak would be above 0 dB: lower the gain, or allow "
                               "clipping.")
        else:
            self._note.setText(f"Peak after the filter: {self._peak:.1f} dBFS.")
