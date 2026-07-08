"""Shared paging/jog navigation for the waveform panes (Audio, Raw Capture).

`paged_range` shifts a view window by one full width, clamped to the data extent;
`install_jog_shortcuts` wires the ``<`` / ``>`` keys (focus-scoped, so the two
panes' identical shortcuts don't clash) and the pane's focus policy.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut


def paged_range(x0: float, x1: float, lo: float, hi: float,
                direction: int) -> Optional[Tuple[float, float]]:
    """Shift the window [x0, x1] by one full width (direction < 0 = left, > 0 = right),
    keeping the span, clamped to [lo, hi]. Returns the new (x0, x1), or None when the
    span is degenerate (nothing to page)."""
    span = x1 - x0
    if span <= 0:
        return None
    d = span if direction >= 0 else -span
    nx0, nx1 = x0 + d, x1 + d
    if nx0 < lo:                       # clamp at the left, keep the span
        nx0, nx1 = lo, lo + span
    elif nx1 > hi:                     # clamp at the right, keep the span
        nx1, nx0 = hi, max(lo, hi - span)
    return nx0, nx1


def install_jog_shortcuts(widget, jog) -> None:
    """Bind ``<`` / ``>`` on `widget` to `jog(-1)` / `jog(+1)`, focus-scoped to the
    widget (or a child) so the Audio and Raw panes' identical keys fire only for the
    focused pane. Also makes the pane focusable."""
    widget.setFocusPolicy(Qt.StrongFocus)
    for keyseq, d in ((Qt.Key_Less, -1), (Qt.Key_Greater, +1)):
        sc = QShortcut(QKeySequence(keyseq), widget)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(lambda d=d: jog(d))
