"""Shared pyqtgraph helpers for the analyzer's waveform views."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt


class XWheelViewBox(pg.ViewBox):
    """A viewbox where a vertical two-finger gesture / wheel **zooms** the X axis
    (delegating to pyqtgraph's proportional zoom, so trackpad deltas stay gentle)
    and a horizontal gesture **scrolls** (pans) it. Y is left to the plot. Used by
    the audio and raw-capture views so both pan left/right with a two-finger swipe."""

    def wheelEvent(self, ev, axis=None):
        try:
            horizontal = ev.orientation() == Qt.Horizontal
        except Exception:                          # noqa: BLE001 — no orientation()
            horizontal = False
        if horizontal:
            (x0, x1), _ = self.viewRange()
            span = x1 - x0
            if span > 0:
                shift = -(ev.delta() / 120.0) * span * 0.15   # ~15% of view per notch
                self.setXRange(x0 + shift, x1 + shift, padding=0)
            ev.accept()
            return
        super().wheelEvent(ev, axis)               # vertical → default (proportional) zoom
