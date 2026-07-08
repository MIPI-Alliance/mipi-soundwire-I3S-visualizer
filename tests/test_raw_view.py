"""Raw Capture pane regressions.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_raw_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.raw_view import RawCaptureView


class _Cap:
    def __init__(self):
        self.clock_edges = np.arange(0, 1000, 2, dtype=np.uint64)
        self.data_edges = np.arange(1, 1000, 3, dtype=np.uint64)
        self.initial_clock = False
        self.initial_data = False
        self.sample_rate_hz = 1000


def test_hide_clock_survives_fbcse_flip():
    """Hiding the clock, then loading a capture with the opposite DP/DN orientation
    (FBCSE flip), must not leave the new DATA curve hidden — _apply_clock_visibility
    re-shows the data curve, hiding only the (now other) clock curve."""
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.set_capture(_Cap(), dp_is_data_line=False)   # clock = DP
    rv._clock_btn.setChecked(False)                  # Hide Clock -> hides DP (the clock)
    assert not rv._dp_curve.isVisible() and rv._dn_curve.isVisible()

    rv.set_capture(_Cap(), dp_is_data_line=True)      # flip: clock = DN, data = DP
    assert rv._dp_curve.isVisible(), "data curve (DP) hidden after FBCSE flip"
    assert not rv._dn_curve.isVisible(), "clock curve (DN) should stay hidden"

    rv._clock_btn.setChecked(True)                    # Show Clock -> both visible
    assert rv._dp_curve.isVisible() and rv._dn_curve.isVisible()


if __name__ == "__main__":
    test_hide_clock_survives_fbcse_flip()
    print("ok: Hide Clock survives an FBCSE orientation flip")
    print("ALL RAW-VIEW TESTS PASSED")
