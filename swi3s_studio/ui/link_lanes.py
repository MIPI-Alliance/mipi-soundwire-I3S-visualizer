"""One pane per Link, stacked on one global time axis: the All Links form of the bottom panes.

Each lane is an ordinary single-Link view (RawCaptureView, AudioView) that never learns about
Links: the window binds lane k under `_as_link(k)` as it binds the one view today. What this
adds is the stack, a name over each lane, and the X sync: a lane's X is seconds of ITS
capture, so a range shown on lane k is moved into global time with k's offset and back out
with each other lane's. The lanes' axes are offset the same way, so they all read global
time.

A view takes part through `set_time_offset(seconds)` and, if it has an X axis, through
`viewRangeChanged(t0, t1)`, `x_range_seconds()` and `set_x_range_seconds(t0, t1)`, all in
seconds. The CDS and Samples tables have no X: they stack, each windowed on the cursor's
instant in its Link, with their Time columns offset into global time.

Lane 0 is the pane's own view, the one a single Link shows, so a window with one Link (or
with the group on one Link) has exactly the pane it had.
"""
from __future__ import annotations

from typing import Callable, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from .theme import VizTheme


class LinkLanes(QWidget):
    def __init__(self, primary: QWidget, make_view: Callable[[], QWidget]) -> None:
        super().__init__()
        self._make = make_view
        self._views: List[QWidget] = [primary]
        self._labels: List[QLabel] = []
        self._offsets: List[float] = [0.0]
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._split = QSplitter(Qt.Vertical)
        self._split.setChildrenCollapsible(False)
        lay.addWidget(self._split)
        self._add_lane(primary)

    # ---- lanes ----
    def _add_lane(self, view: QWidget) -> None:
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        label = QLabel()
        label.setTextFormat(Qt.PlainText)          # a Link's name, never markup
        label.setVisible(False)
        hl.addWidget(label)
        hl.addWidget(view, 1)
        self._split.addWidget(holder)
        self._labels.append(label)
        k = len(self._labels) - 1
        if hasattr(view, "viewRangeChanged"):       # a table has no X to keep in step
            view.viewRangeChanged.connect(lambda t0, t1, k=k: self._on_range(k, t0, t1))
        self._style_label(label)

    def _style_label(self, label: QLabel) -> None:
        label.setStyleSheet(f"color:{VizTheme.TEXT}; font-weight:600; padding:2px 4px;")

    def retheme(self) -> None:
        for label in self._labels:
            self._style_label(label)

    @property
    def views(self) -> List[QWidget]:
        """Every lane's view, shown or not; lane 0 is the pane's own."""
        return list(self._views)

    def shown(self) -> List[QWidget]:
        """The lanes in use: one per Link in All Links, else the pane's own view."""
        return self._views[:self.count()]

    def count(self) -> int:
        return sum(1 for i in range(len(self._views))
                   if not self._split.widget(i).isHidden())

    def set_lanes(self, names: List[str], offsets_s: List[float],
                  span_s=None) -> List[QWidget]:
        """Show one lane per name (Link order), each labelled and offset into global time
        by `offsets_s` (seconds); one name shows the pane's own view, unlabelled and
        unoffset. `span_s` is the global (start, end) every Link covers: each lane may
        range over it, so lanes of different lengths can show one window. Returns the
        views in use. Lanes beyond the count are deleted (all but the pane's own): each
        still holds the Link it showed, its capture, audio and edges, which a removed or
        replaced Link must not keep alive."""
        n = max(1, len(names))
        while len(self._views) > n:
            self._drop_last_lane()
        while len(self._views) < n:
            view = self._make()
            self._views.append(view)
            self._add_lane(view)
        many = n > 1
        self._offsets = [float(o) for o in offsets_s] if many else [0.0]
        self._offsets += [0.0] * (len(self._views) - len(self._offsets))
        for i, view in enumerate(self._views):
            self._split.widget(i).setVisible(i < n)
            self._labels[i].setVisible(many and i < n)
            if i < len(names):
                self._labels[i].setText(names[i])
            if hasattr(view, "set_time_offset"):   # Timing has no time axis
                view.set_time_offset(self._offsets[i] if many else 0.0)
            if hasattr(view, "set_x_bounds"):
                view.set_x_bounds((span_s[0] - self._offsets[i], span_s[1] - self._offsets[i])
                                  if many and span_s else None)
        return self._views[:n]

    def _drop_last_lane(self) -> None:
        holder = self._split.widget(len(self._views) - 1)
        self._views.pop()
        self._labels.pop()
        holder.hide()
        holder.setParent(None)
        holder.deleteLater()

    def lane_of(self, widget) -> int:
        """The lane a widget (or one of its children) is in, or -1."""
        w = widget
        while w is not None:
            for i in range(len(self._views)):
                if w is self._split.widget(i):
                    return i
            w = w.parentWidget()
        return -1

    # ---- X sync, through global time ----
    def _on_range(self, k: int, t0: float, t1: float) -> None:
        if self._syncing or self.count() < 2 or k >= self.count():
            return
        g0, g1 = t0 + self._offsets[k], t1 + self._offsets[k]
        self._syncing = True
        try:
            for j, view in enumerate(self.shown()):
                if j != k:
                    view.set_x_range_seconds(g0 - self._offsets[j], g1 - self._offsets[j])
        finally:
            self._syncing = False

    def align_to(self, k: int) -> None:
        """Put every lane on lane k's range (after a (re)bind reset one)."""
        if k >= self.count() or not hasattr(self._views[k], "x_range_seconds"):
            return
        rng = self._views[k].x_range_seconds()
        if rng is not None:
            self._on_range(k, *rng)
