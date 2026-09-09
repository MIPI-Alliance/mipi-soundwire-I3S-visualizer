"""Timing view — where data transitions land relative to the forwarded clock.

A probe placed at a sub-optimal point on the bus (eroding setup/hold margin) shows
up as transitions creeping toward the sample point. Two histograms share the Y axis
and meet at the sample point: setup on the left (time before the sample point) and
hold on the right (time after). Each side scales and bins to its own margins, so a
long-UI region (a slow-clock section) keeps hold detail that a single symmetric axis
would crush. Each transition contributes one setup and one hold, split into the four
(clock-edge polarity × data-transition direction) types so a duty-skewed edge or an
asymmetric data slew (rise vs fall) stands out. Log Y so a single edge too close to
the sample point (count = 1) is still visible.

A multi-select filter narrows the transitions to any union of drivers (manager /
peripheral), data ports, or columns, so you can see whose edges are marginal (CDS /
Column-0 is the control stream, off by default). A "View Worst UI" navigator jumps the
shared cursor to the worst transitions so they can be inspected in the Raw Capture pane.

Compute lives in `analysis.bus_timing`; this view only renders it. Aggregate over the
capture (not cursor-driven), so it has no time cursor.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..analysis.bus_timing import measure_bus_timing
from .theme import VizTheme, analyzer_stylesheet

# Y is plotted as log10(count) in a LINEAR view (we transform ourselves rather than use
# pyqtgraph's log mode, whose fillLevel/fill polygon disagree with the pen). This floor
# is the display baseline: the x-axis line and the level bars fill down to. It sits below
# log10(1)=0 so a single-count bar still draws a visible sliver above the axis.
_LOG_FLOOR = -0.35


def _cat_specs():
    """The four transition categories — (clock rising?, data rising?, label, colour) —
    read live from the palette so a theme switch recolours them. Clock polarity picks the
    hue family (rising = cool, falling = warm); data direction picks the shade within it."""
    p = VizTheme.TRACE_PALETTE
    return [
        (True,  True,  "clk↑ dat↑", p[0]),   # blue
        (True,  False, "clk↑ dat↓", p[5]),   # cyan
        (False, True,  "clk↓ dat↑", p[3]),   # gold
        (False, False, "clk↓ dat↓", p[2]),   # red
    ]


class _MultiMenu(QMenu):
    """A menu that stays open when a checkable item is toggled (so several filter groups
    can be (un)checked in one visit); non-checkable items (the presets) close it."""

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt signature)
        act = self.activeAction()
        if act is not None and act.isCheckable() and act.isEnabled():
            act.trigger()                 # toggle in place, keep the menu up
            return
        super().mouseReleaseEvent(event)


class _ClickLabel(QLabel):
    """A QLabel that emits `clicked` on a left-button release — used for the timing
    view's clickable colour-key entries (click to hide/show that edge flavour)."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt signature)
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EyeView(QWidget):
    """Combined setup/hold margin histogram, rendered from a Capture's raw edges."""

    sampleSelected = Signal('qlonglong')     # jump the shared cursor to a tight transition

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(analyzer_stylesheet())
        self._timing = None
        self._capture = None
        self._recovered_clock = None  # DLV (row_sync_samples, ui) — mid-UI sample-point model
        self._sections: list[dict] = []   # per-section UI stats (kept for callers; not drawn)
        self._segments: list[dict] = []   # decode segments (start_ui, column_count) for tr_column
        self._regions: list[dict] = []    # audio-mode regions (Session.timing_regions)
        self._roles_fn = None         # Session.timing_column_roles — column roles on demand
        self._roles_cache: dict[int, dict] = {}  # region index -> column-role map (lazy, cached)
        self._col_roles: dict[int, dict] = {}    # column -> driver role (from the current region)
        self._start_sample = 0        # current region's sample range (excludes link control)
        self._end_sample = None
        self._filter_items: list[dict] = []      # [{action, cols(frozenset), kind}] checkable filter items
        self._dirty = False
        self._cat_hidden: set[tuple] = set()     # hidden edge flavours: {(clk_rising, data_rising)} keys
        self._worst: list[tuple] = []            # tightest transitions (sample, setup_ns, hold_ns)
        self._worst_i = 0
        self._navigating = False
        self._rate = 1

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # One control row: region (only when the clock rate changes) · filter · colour
        # key · … · worst-UI readout + button. One row leaves the histogram max height.
        row = QHBoxLayout()
        self._region_label = QLabel("Region:")
        row.addWidget(self._region_label)
        self._region = QComboBox()
        self._region.setToolTip("A commit can change the bus clock rate mid-capture "
                                "(e.g. 2 col → 16 col); setup/hold is measured per region "
                                "since it only makes sense within one clock rate.")
        self._region.setMinimumWidth(150)
        self._region.currentIndexChanged.connect(lambda _=0: self._on_region_changed())
        row.addWidget(self._region)
        row.addSpacing(16)
        row.addWidget(QLabel("Filter:"))
        self._filter = QToolButton()
        self._filter.setPopupMode(QToolButton.InstantPopup)
        self._filter.setToolTip("Restrict the margins to any combination of drivers "
                                "(manager / peripheral), data ports or columns. CDS "
                                "(Column 0) is the control stream, off by default.")
        self._filter.setMinimumWidth(210)
        self._filter_menu = _MultiMenu(self._filter)
        self._filter.setMenu(self._filter_menu)
        row.addWidget(self._filter)
        row.addSpacing(16)
        # Clickable colour key: click a flavour to hide/show it; hidden ones grey out.
        self._legend_labels = {}      # (ck, dk) -> _ClickLabel
        for ck, dk, label, _color in _cat_specs():
            lab = _ClickLabel()
            lab.setTextFormat(Qt.RichText)
            lab.setToolTip("Click to hide/show this clock/data edge flavour")
            lab.clicked.connect(lambda c=ck, d=dk: self._toggle_category(c, d))
            self._legend_labels[(ck, dk)] = lab
            row.addWidget(lab)
        self._update_legend()
        row.addStretch(1)

        # Worst-UI navigator: a single 'View Worst UI' button that becomes prev/next
        # once pressed. (No running readout — it clutters the bar and users rarely
        # step through all of the offered UIs.)
        self._worst_view = QPushButton("View Worst UI")
        self._worst_view.setToolTip("Jump the cursor to the worst-margin UI and show it "
                                    "centered in the Raw Capture pane")
        self._worst_view.clicked.connect(self._view_worst)
        self._worst_prev = QPushButton("◀ View Previous UI")
        self._worst_prev.setToolTip("Step back toward the worst-margin UI")
        self._worst_prev.clicked.connect(lambda: self._jump_worst(-1))
        self._worst_next = QPushButton("View Next Worst UI ▶")
        self._worst_next.setToolTip("Step to the next-tightest UI (worst first)")
        self._worst_next.clicked.connect(lambda: self._jump_worst(+1))
        row.addWidget(self._worst_view)
        row.addWidget(self._worst_prev)
        row.addWidget(self._worst_next)
        root.addLayout(row)

        # Two plots sharing the Y axis: setup (left, 0 = sample point on the inner/right
        # edge) and hold (right, 0 on the inner/left edge), meeting at the sample point.
        # Each side scales + bins to its own margins, so a long-UI region (slow clock)
        # keeps hold detail that a single symmetric axis would crush into a sliver.
        plots = QHBoxLayout()
        plots.setSpacing(0)
        self._setup_plot = self._make_plot("← setup (ns)")
        self._setup_plot.getViewBox().invertX(True)     # 0 on the right, at the join
        self._setup_plot.setLabel("left", "transitions (log)")
        self._hold_plot = self._make_plot("hold (ns) →")
        self._hold_plot.setYLink(self._setup_plot)       # shared Y range
        # Drop the hold plot's Y axis entirely (label lives on the left plot). hideAxis
        # RECLAIMS the axis's layout width — setVisible(False) leaves it reserved, which
        # opened a blank strip on the hold plot's inner edge and split the two 0-ns points.
        self._hold_plot.hideAxis("left")
        # The horizontal (Y) grid lines are painted by the left axis, so hiding it left
        # the hold plot with only the vertical (bottom-axis) grid. Route the Y grid
        # through the OUTER right axis instead — hide its ticks + numbers, but leave the
        # pen at the axis default (pyqtgraph paints grid lines in the axis's OWN pen
        # colour, so a background pen would make them invisible) so it matches setup's.
        self._hold_plot.showAxis("right")
        _right = self._hold_plot.getAxis("right")
        _right.setStyle(showValues=False, tickLength=0)
        _right.setGrid(round(0.15 * 255))                # same alpha as _make_plot's showGrid
        for p in (self._setup_plot, self._hold_plot):    # sample-point line at the join
            p.addItem(pg.InfiniteLine(pos=0.0, angle=90,
                      pen=pg.mkPen(VizTheme.CURSOR, width=1, style=Qt.DashLine)))
        plots.addWidget(self._setup_plot, 1)
        plots.addWidget(self._hold_plot, 1)
        root.addLayout(plots, 1)

        self._rebuild_filter()
        self._reset_worst_buttons()

    def _update_legend(self) -> None:
        """Refresh each clickable colour-key entry: full colour when the flavour is
        shown, greyed (swatch + label in the dim text colour) when it's hidden."""
        for (ck, dk), label, color in ((k, self._legend_labels[k], c)
                                       for k, c in self._cat_colors().items()):
            hidden = (ck, dk) in self._cat_hidden
            swatch = QColor(VizTheme.TEXT_DIM).name() if hidden else QColor(color).name()
            text = VizTheme.TEXT_DIM if hidden else VizTheme.TEXT
            label.setText(
                f'<span style="color:{swatch}; font-size:13px;">&#9632;</span>'
                f' <span style="color:{text};">{self._cat_label(ck, dk)}</span>')

    @staticmethod
    def _cat_colors() -> dict:
        return {(ck, dk): color for ck, dk, _label, color in _cat_specs()}

    @staticmethod
    def _cat_label(ck: bool, dk: bool) -> str:
        for c, d, label, _color in _cat_specs():
            if (c, d) == (ck, dk):
                return label
        return ""

    def _toggle_category(self, ck: bool, dk: bool) -> None:
        """Hide/show one clock/data edge flavour from the color-key click."""
        key = (ck, dk)
        if key in self._cat_hidden:
            self._cat_hidden.discard(key)
        else:
            self._cat_hidden.add(key)
        self._update_legend()
        self._redraw()

    def _make_plot(self, xlabel: str) -> pg.PlotWidget:
        p = pg.PlotWidget(background=VizTheme.PLOT_BG)
        p.showGrid(x=True, y=True, alpha=0.15)
        p.setMenuEnabled(False)
        p.getPlotItem().hideButtons()      # drop the corner auto-range "A" button
        # A fixed-scale histogram, not an explorable trace — disable mouse pan/zoom so a
        # stray scroll/drag doesn't rescale the axes.
        p.setMouseEnabled(x=False, y=False)
        p.getViewBox().setMouseEnabled(x=False, y=False)
        p.setLabel("bottom", xlabel)
        p.setLabel("left", "transitions")
        p.getAxis("left").setTextPen(VizTheme.AXIS)
        p.getAxis("bottom").setTextPen(VizTheme.AXIS)
        return p

    @staticmethod
    def _hist_curve(plot, centers, counts, color):
        """Filled step histogram in log10(count) space. Each contiguous run of non-zero
        bins is one explicit closed polyline that starts/ends on the floor, so the
        left/right end risers are drawn, the fill (down to the pinned floor) reaches the
        x-axis with no gap and never overshoots the pen. Empty regions stay blank."""
        pen = pg.mkPen(color, width=1.3)
        brush = pg.mkBrush(pg.mkColor(color).red(), pg.mkColor(color).green(),
                           pg.mkColor(color).blue(), 55)
        if centers.size >= 2:
            w = centers[1] - centers[0]
            edges = np.concatenate([centers - w / 2, [centers[-1] + w / 2]])
        else:
            edges = np.array([0.0, 1.0])
        counts = np.asarray(counts)
        n = len(counts)
        i = 0
        while i < n:
            if counts[i] <= 0:
                i += 1
                continue
            j = i
            while j < n and counts[j] > 0:      # contiguous non-zero run [i, j)
                j += 1
            xs = [edges[i]]
            ys = [_LOG_FLOOR]                     # riser up from the floor at the run's left
            for k in range(i, j):
                h = float(np.log10(counts[k]))    # count -> log10 (count >= 1 in a run)
                xs += [edges[k], edges[k + 1]]
                ys += [h, h]
            xs.append(edges[j])
            ys.append(_LOG_FLOOR)                 # drop back to the floor at the run's right
            plot.plot(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float),
                      fillLevel=_LOG_FLOOR, pen=pen, brush=brush)
            i = j

    @staticmethod
    def _x_ticks(hi: float):
        """Major x ticks from 0 up to `hi` (ns) at a 1/2/5·10ⁿ step (~4 divisions), so a
        '0' label always lands at the inner edge. pyqtgraph's auto-ticker drops the
        boundary value, so with 0 pinned flush at the join (padding=0) the sample-point
        '0' vanished; forcing the tick set restores it. Both plots pin 0 at the join, so
        their two '0' labels meet to read as one centred at the sample point."""
        if not (hi > 0):
            return [(0.0, "0")]
        import math
        target = hi / 4.0
        mag = 10 ** math.floor(math.log10(target))
        step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= target)
        ticks = []
        v = 0.0
        while v <= hi * 1.0001:
            ticks.append((v, f"{v:g}"))
            v += step
        return ticks

    @staticmethod
    def _decade_ticks(max_count: int):
        """Major y ticks at each decade (1, 10, 100, …) in log10 space, labelled in
        plain count units, up to the decade holding `max_count`."""
        top = max(1, int(np.ceil(np.log10(max(max_count, 1) + 0.5))))
        ticks = []
        for k in range(0, top + 1):
            v = 10 ** k
            lab = str(v) if v < 1000 else (f"{v // 1000}k" if v < 1_000_000
                                           else f"{v // 1_000_000}M")
            ticks.append((float(k), lab))
        return ticks

    def set_sections(self, sections) -> None:
        """Per config-section UI stats (from Session.section_ui_stats). Kept for callers
        but no longer displayed."""
        self._sections = list(sections or [])

    def set_analysis_context(self, segments, regions, roles_fn=None) -> None:
        """Decode context: `segments` (list of {start_ui, column_count}) tags each
        transition with its column; `regions` (Session.timing_regions) are the audio-mode
        regions to choose between (one per clock-rate/geometry change); `roles_fn`
        (Session.timing_column_roles) computes a region's column→driver map on demand, so
        the grid replay runs only for the region actually shown, not every region up
        front. Populates the region selector (shown only when >1) and defaults to the
        largest region. Does NOT render — set_capture (always called next on load) does
        the single render, so we don't measure the stale/previous capture here."""
        self._segments = list(segments or [])
        self._regions = list(regions or [])
        self._roles_fn = roles_fn
        self._roles_cache = {}
        self._region.blockSignals(True)
        self._region.clear()
        for r in self._regions:
            self._region.addItem(r.get("label", "region"))
        if self._regions:
            default = max(range(len(self._regions)),
                          key=lambda i: self._regions[i].get("edges", 0))
            self._region.setCurrentIndex(default)
        self._region.blockSignals(False)
        multi = len(self._regions) > 1
        self._region.setVisible(multi)
        self._region_label.setVisible(multi)
        self._apply_region()
        self._dirty = True

    def _apply_region(self) -> None:
        """Adopt the selected region's sample range + column roles (computed lazily via
        roles_fn and cached per region), and rebuild the filter for that geometry."""
        i = self._region.currentIndex()
        r = self._regions[i] if 0 <= i < len(self._regions) else {}
        self._start_sample = int(r.get("start_sample") or 0)
        self._end_sample = r.get("end_sample")
        if i not in self._roles_cache:
            self._roles_cache[i] = (dict(self._roles_fn(r.get("mid_sample")) or {})
                                    if self._roles_fn is not None and r else {})
        self._col_roles = self._roles_cache[i]
        self._rebuild_filter()

    def _on_region_changed(self) -> None:
        self._apply_region()
        self._dirty = True
        if self.isVisible():
            self._render()               # re-measure for the new region's rate/geometry

    def set_capture(self, capture, recovered_clock=None) -> None:
        """Stash the capture and render lazily — the setup/hold measurement scans
        millions of edges, so we don't want it on the capture-load critical path.
        It runs when the Timing tab is first shown (or immediately if already visible).

        `recovered_clock` = (row_sync_samples, ui_samples) selects the PHY3 (DLV) model:
        setup/hold measured against the recovered mid-UI sample point, not a forwarded
        clock edge (None for FBCSE)."""
        self._capture = capture
        self._recovered_clock = recovered_clock
        self._dirty = True
        if self.isVisible():
            self._render()

    def showEvent(self, event):  # noqa: N802 (Qt signature)
        super().showEvent(event)
        if self._dirty:
            self._render()

    def _render(self) -> None:
        """Measure the stashed capture (expensive) then draw. Filter changes call
        _redraw() only (no re-measure)."""
        self._dirty = False
        capture = self._capture
        t = (measure_bus_timing(capture, segments=self._segments,
                                start_sample=self._start_sample, end_sample=self._end_sample,
                                recovered_clock=self._recovered_clock)
             if capture is not None else None)
        self._timing = t
        self._rate = int(t.sample_rate_hz) if t is not None else 1
        self._redraw()

    # ---- filter -----------------------------------------------------------------

    def _rebuild_filter(self) -> None:
        """(Re)build the multi-select menu from the column roles: checkable items for
        Manager, each peripheral port (Device N DPm), CDS, and each column, plus preset
        actions that set those checkboxes. Union of the checked items is the filter."""
        roles = self._col_roles
        self._filter_menu.clear()
        self._filter_items = []
        if not roles:
            self._filter.setText("All  ▾")
            self._filter.setEnabled(False)
            return
        self._filter.setEnabled(True)

        self._filter_menu.addAction("All", lambda: self._apply_preset("all"))
        self._filter_menu.addAction("All excl. CDS", lambda: self._apply_preset("data"))
        kinds = lambda k: frozenset(c for c, r in roles.items() if r.get("kind") == k)
        cds, mgr, per = kinds("cds"), kinds("manager"), kinds("peripheral")
        if mgr:
            self._filter_menu.addAction("Manager", lambda: self._apply_preset("manager"))
        if per:
            self._filter_menu.addAction("Peripherals", lambda: self._apply_preset("peripherals"))
        self._filter_menu.addSeparator()

        def add_check(label, cols, kind):
            act = self._filter_menu.addAction(label)
            act.setCheckable(True)
            act.toggled.connect(lambda _=False: self._on_filter_changed())
            self._filter_items.append({"action": act, "cols": frozenset(cols), "kind": kind})

        if mgr:
            add_check("Manager", mgr, "manager")
        for d, p in sorted({(r["device"], r["dp"]) for r in roles.values()
                            if r.get("kind") == "peripheral"}):
            cs = frozenset(c for c, r in roles.items() if r.get("kind") == "peripheral"
                           and r["device"] == d and r["dp"] == p)
            add_check(f"Device {d} DP{p}", cs, "peripheral")
        if cds:
            add_check("CDS (Col 0)", cds, "cds")
        sub = self._filter_menu.addMenu("Columns")
        for c in sorted(x for x, r in roles.items() if r.get("kind") != "cds"):
            act = sub.addAction(f"Col {c}")
            act.setCheckable(True)
            act.toggled.connect(lambda _=False: self._on_filter_changed())
            self._filter_items.append({"action": act, "cols": frozenset({c}), "kind": "column"})

        self._apply_preset("all", redraw=False)        # default: everything (incl. CDS)

    def _apply_preset(self, which: str, redraw: bool = True) -> None:
        """Set the coarse (Manager / port / CDS) checkboxes for a preset. Per-column
        checkboxes are left alone — they're the fine control."""
        for item in self._filter_items:
            if item["kind"] == "column":
                continue
            want = (which == "all"
                    or (which == "data" and item["kind"] != "cds")
                    or (which == "manager" and item["kind"] == "manager")
                    or (which == "peripherals" and item["kind"] == "peripheral"))
            act = item["action"]
            act.blockSignals(True)
            act.setChecked(want)
            act.blockSignals(False)
        self._on_filter_changed(redraw)

    def _selected_cols(self):
        """Union of the columns of every checked filter item; None when there's no
        geometry (filter disabled → show everything)."""
        if not self._filter_items:
            return None
        sel = set()
        for item in self._filter_items:
            if item["action"].isChecked():
                sel |= item["cols"]
        return frozenset(sel)

    def _on_filter_changed(self, redraw: bool = True) -> None:
        self._update_filter_text()
        if redraw:
            self._redraw()

    def _update_filter_text(self) -> None:
        roles = self._col_roles
        sel = self._selected_cols()
        allc = frozenset(roles)
        datac = frozenset(c for c, r in roles.items() if r.get("kind") != "cds")
        perc = frozenset(c for c, r in roles.items() if r.get("kind") == "peripheral")
        checked = [it for it in self._filter_items if it["action"].isChecked()]
        if sel is None or sel == allc:
            text = "All"
        elif sel == datac:
            text = "All excl. CDS"
        elif perc and sel == perc:
            text = "Peripherals"
        elif not checked:
            text = "(none)"
        elif len(checked) == 1:
            text = checked[0]["action"].text()
        else:
            text = f"{len(checked)} selected"
        self._filter.setText(text + "  ▾")

    def _filter_mask(self, t) -> np.ndarray:
        sel = self._selected_cols()
        if sel is None:                                  # no geometry → everything
            return np.ones(t.tr_column.shape, dtype=bool)
        if not sel:                                      # nothing checked
            return np.zeros(t.tr_column.shape, dtype=bool)
        return np.isin(t.tr_column, np.fromiter(sel, dtype=np.int64, count=len(sel)))

    # ---- draw -------------------------------------------------------------------

    def _redraw(self) -> None:
        """Draw both histograms + recompute the worst list for the current filter, from
        the already-measured timing (cheap — no re-measure)."""
        for p in (self._setup_plot, self._hold_plot):
            p.clear()
            p.addItem(pg.InfiniteLine(pos=0.0, angle=90,       # sample-point line at the join
                      pen=pg.mkPen(VizTheme.CURSOR, width=1, style=Qt.DashLine)))
        t = self._timing
        if t is None:
            self._worst = []
            self._reset_worst_buttons()
            return

        m = self._filter_mask(t)
        su, ho = t.tr_setup_ns[m], t.tr_hold_ns[m]
        dr = t.tr_data_rising[m]
        scr, hcr = t.tr_setup_clk_rising[m], t.tr_hold_clk_rising[m]
        samp = t.tr_sample[m]

        def draw_side(plot, vals, pol):
            """Histogram `vals` (setup or hold ns) into `plot` over its own [0, max] range
            (independent per-side scale) and return (x-max, tallest-bar count). Flavours
            hidden via the colour key are skipped."""
            hi = float(np.max(vals)) if vals.size else 1.0
            hi = hi if hi > 0 else 1.0
            # No bar narrower than the capture's timing resolution (one sample period):
            # setup/hold are quantised to 1/sample_rate, so finer bins would imply a
            # precision the capture doesn't have (e.g. a 500 MHz capture -> 2 ns floor).
            res_ns = (1e9 / self._rate) if self._rate else 0.0
            nbins = 40 if res_ns <= 0 else int(max(1, min(40, np.floor(hi / res_ns))))
            edges = np.linspace(0.0, hi, nbins + 1)
            centers = 0.5 * (edges[:-1] + edges[1:])
            mx = 1
            for ck, dk, _label, color in _cat_specs():
                if (ck, dk) in self._cat_hidden:
                    continue
                sel = (pol == ck) & (dr == dk)
                hist, _ = np.histogram(vals[sel], bins=edges)
                self._hist_curve(plot, centers, hist, color)
                mx = max(mx, int(hist.max(initial=0)))
            return hi, mx

        su_hi, su_mx = draw_side(self._setup_plot, su, scr)
        ho_hi, ho_mx = draw_side(self._hold_plot, ho, hcr)
        mx = max(su_mx, ho_mx)
        # Shared log-Y range + decade ticks (on the left plot's visible axis); each side
        # keeps its own x scale so 0 (the sample point) meets at the join.
        y_top = float(np.log10(mx)) + 0.15
        self._setup_plot.setYRange(_LOG_FLOOR, y_top, padding=0)
        self._setup_plot.getAxis("left").setTicks([self._decade_ticks(mx)])
        # 0 (the sample point) must sit flush at the inner edge of EACH plot so the two
        # 0-ns points meet at the join with no gap; put the small headroom on the OUTER
        # edge only (range [0, hi*1.03] with padding=0), not symmetric padding (which
        # pushed 0 inward on both plots and opened a gap between them).
        self._setup_plot.setXRange(0, su_hi * 1.03, padding=0)
        self._hold_plot.setXRange(0, ho_hi * 1.03, padding=0)
        # Pin explicit x ticks (incl. 0) on each side — auto-ticking drops the boundary
        # value, so the sample-point 0 at the join would otherwise be missing.
        self._setup_plot.getAxis("bottom").setTicks([self._x_ticks(su_hi)])
        self._hold_plot.getAxis("bottom").setTicks([self._x_ticks(ho_hi)])

        # Worst-UI navigator excludes hidden flavours: a transition's setup is flavour
        # (setup-clk-pol, data-dir) and its hold (hold-clk-pol, data-dir), so mask each
        # side to +inf when its flavour is hidden, and drop transitions with both sides
        # hidden (they no longer contribute a visible margin).
        if self._cat_hidden:
            su_w, ho_w = su.astype(float).copy(), ho.astype(float).copy()
            for ck, dk in self._cat_hidden:
                su_w[(scr == ck) & (dr == dk)] = np.inf
                ho_w[(hcr == ck) & (dr == dk)] = np.inf
            keep = np.isfinite(np.minimum(su_w, ho_w))
            self._worst = self._compute_worst(samp[keep], su_w[keep], ho_w[keep])
        else:
            self._worst = self._compute_worst(samp, su, ho)
        self._reset_worst_buttons()

    @staticmethod
    def _compute_worst(sample, setup, hold, take: int = 500):
        margin = np.minimum(setup, hold)
        if margin.size == 0:
            return []
        take = int(min(take, margin.size))
        part = np.argpartition(margin, take - 1)[:take]
        sel = part[np.lexsort((sample[part], margin[part]))]     # by margin, then time
        return [(int(sample[i]), float(setup[i]), float(hold[i])) for i in sel]

    # ---- worst-UI navigation ----------------------------------------------------

    # Aggregate view — no time cursor to track, but main_window calls set_cursor on
    # every analyzer view; accept and ignore it.
    def set_cursor(self, sample: int) -> None:
        pass

    def _reset_worst_buttons(self) -> None:
        """Back to the initial state: just the 'View Worst UI' button (called whenever
        the worst list changes — new capture or filter)."""
        self._navigating = False
        self._worst_i = 0
        has = bool(self._worst)
        self._worst_view.setVisible(True)
        self._worst_view.setEnabled(has)
        self._worst_prev.setVisible(False)
        self._worst_next.setVisible(False)
        self._update_worst_buttons()

    def _view_worst(self) -> None:
        """'View Worst UI': jump to the single worst UI and switch to prev/next stepping."""
        if not self._worst:
            return
        self._navigating = True
        self._worst_i = 0
        self._worst_view.setVisible(False)
        self._worst_prev.setVisible(True)
        self._worst_next.setVisible(True)
        self._update_worst_buttons()
        self.sampleSelected.emit(int(self._worst[0][0]))

    def _jump_worst(self, direction: int) -> None:
        """Step to the previous / next-worst UI and move the shared cursor there."""
        if not self._worst:
            return
        self._worst_i = max(0, min(len(self._worst) - 1, self._worst_i + direction))
        self._update_worst_buttons()
        self.sampleSelected.emit(int(self._worst[self._worst_i][0]))

    def _update_worst_buttons(self) -> None:
        """Enable/disable prev/next at the ends of the worst list."""
        n = len(self._worst)
        self._worst_prev.setEnabled(n > 0 and self._worst_i > 0)
        self._worst_next.setEnabled(n > 0 and self._worst_i < n - 1)

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the legend + category
        colours (read live from the palette), the plot background + axis pens and the
        item-view stylesheet, then re-draw (lazily — only if visible, else on next show)."""
        self.setStyleSheet(analyzer_stylesheet())
        self._update_legend()
        for p in (self._setup_plot, self._hold_plot):
            p.setBackground(VizTheme.PLOT_BG)
            p.getAxis("left").setTextPen(VizTheme.AXIS)
            p.getAxis("bottom").setTextPen(VizTheme.AXIS)
        self._dirty = True
        if self.isVisible():
            self._render()
