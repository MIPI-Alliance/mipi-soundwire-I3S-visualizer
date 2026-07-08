"""Raw-capture view: the two physical link lines (DP, DN) as digital waveforms,
reconstructed straight from the capture's transition edges — an alternative to the
decoded timeline for inspecting the wire itself (cold-start LC signaling, PHY
edges, framing) without any protocol interpretation.

Edges are stored as sample numbers, so the level at any sample is the initial
level XORed with the parity of the edges before it. To stay light on multi-GB
captures we only build the step arrays for the currently visible sample window
(re-centered on the shared cursor), not the whole capture.

Optionally the view overlays a marker at every Column-0 (the CDS column) — the
rising clock edge (Row Sync Point) that opens each frame column — supplied per
visible window by a provider callback (:meth:`set_cds_provider`); they're drawn
as faint neutral vertical lines with a down-arrow head and can be toggled off.

It can also overlay the decoder's **port sample points**: for the *active* data
ports only (those that transported audio), one stacked lane per (device, dp,
channel) on the data trace — a filled circle at every sampled data bit, drawn
larger at each sample interval's start (the MSB), colour-coded to match the Audio
view. A coloured horizontal line joins the bits of each multi-bit word MSB→LSB so
you can see at a glance which sampled bits form one sample (a 1-bit PDM word gets
no line). The legend keys each port colour plus the CDS marker. Overlaid on the RSP
grid this shows exactly which clock edges are a channel's bit samples relative to
Column 0, which is how you eyeball whether a peripheral-source port is read the
expected number of UIs late.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .theme import VizTheme, analyzer_stylesheet
from .plot_widgets import XWheelViewBox
from .nav import install_jog_shortcuts, paged_range
from ..nputil import searchsorted as _ss

_DP_COLOR = VizTheme.RAW_DP
_DN_COLOR = VizTheme.RAW_DN
_CURSOR_COLOR = VizTheme.CURSOR
_CDS_COLOR = VizTheme.TEXT           # Column-0 (CDS) markers: neutral high-contrast (the repeating grid)
# A confirmed commit's marker is drawn in the gold SSP colour so it matches the
# timeline's confirmed-commit (Commit + SSP) tick and pops against the neutral CDS
# row-syncs it sits among. Kept as a solid full-height vertical.
_COMMIT_COLOR = VizTheme.SEM_SSP

# Vertical extent of a CDS-column marker line (spans just past both traces) and the
# y at which its down-pointing arrow head sits, in the plot's 0..2.8 trace space.
_CDS_Y_BOT, _CDS_Y_TOP, _CDS_Y_ARROW = -0.1, 2.5, 2.62
_DP_BAND_LO, _DP_BAND_HI = 1.6, 2.4        # DP trace band (port dots stack within it)
_DN_BAND_LO, _DN_BAND_HI = 0.1, 0.9        # DN trace band
_MAX_PORT_MARKS = 4000                     # hide port dots above this many in view (too dense)
_BIT_DOT, _MSB_DOT = 6, 9                   # bit-sample dot px size; larger for the MSB (interval start)
_RIGHT_PAD = 0.16                           # blank fraction on the right so the legend clears the trace


def _step_xy_from(win: np.ndarray, init_level: bool, lo: int, hi: int, y0: float, y1: float):
    """Step (x, y) arrays for a digital line over [lo, hi] samples given the edges
    already windowed to that range (`win`) and the level just before `lo`
    (`init_level`). Drawn between `y0` (low) and `y1` (high); x stays in samples."""
    level = bool(init_level)
    xs = [lo]
    ys = [y1 if level else y0]
    for e in win:
        e = int(e)
        xs.append(e); ys.append(y1 if level else y0)  # hold up to the edge
        level = not level
        xs.append(e); ys.append(y1 if level else y0)  # transition
    xs.append(hi); ys.append(y1 if level else y0)
    return xs, ys


class _RawTimeAxis(pg.AxisItem):
    """Bottom time axis whose tick labels adapt their unit to the zoom (seconds when
    coarse, else milliseconds or microseconds) and comma-separate thousands — so a
    zoomed-in tick reads `4,302,195.5 µs` instead of `4.3021955`. The raw view's x is
    already in seconds, so no rate conversion is needed; the unit is shown per tick, so
    the axis label carries no fixed unit."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.enableAutoSIPrefix(False)        # we format the time ourselves

    def tickStrings(self, values, scale, spacing):
        step_s = float(spacing) if spacing else 0.0     # x is seconds; spacing is too
        if step_s == 0 or step_s >= 1.0:
            unit, mul = "s", 1.0
        elif step_s >= 1e-3:
            unit, mul = "ms", 1e3
        else:
            unit, mul = "µs", 1e6
        step_u = step_s * mul
        dec = 0 if step_u >= 1 else int(min(6, np.ceil(-np.log10(step_u)) + 1))
        return [f"{v * mul:,.{dec}f} {unit}" for v in values]


class RawCaptureView(QWidget):
    """Two stacked digital traces (DP above DN) over a sample window, with a cursor
    line that tracks the shared time cursor. Wheel/drag zoom & pan the X axis."""

    sampleSelected = Signal('qlonglong')             # x clicked → shared cursor
    portSamplesRequested = Signal()                  # user enabled Port Samples (may need lazy collect)

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(analyzer_stylesheet())
        self._dp_edges = np.zeros(0, dtype=np.uint64)
        self._dn_edges = np.zeros(0, dtype=np.uint64)
        self._dp_init = False
        self._dn_init = False
        self._rate = 1
        self._total = 1                              # last sample (full extent)
        self._rendering = False                      # guard re-entrant range updates
        self._cds_provider = None                    # fn(lo, hi) -> sample array (Column-0 edges)
        self._commit_provider = None                 # fn(lo, hi) -> sample array (commit RSP edges)
        self._show_cds = True
        self._port_provider = None                   # fn(lo, hi) -> {sample,device,dp,channel,is_start}
        self._show_ports = False                     # off until the user asks (bit collection is lazy)
        self._dp_is_data = False                     # which physical band the data line is drawn in
        self._row_samples = 0.0                      # nominal samples per bus row (zoom-to-row hint)
        self._ui_samples = 0.0                       # samples per UI (max-zoom-in floor)
        self._lanes = []                             # ordered (device, dp, channel) lane keys
        self._lane_dots = {}                         # lane -> ScatterPlotItem (per-bit filled circles)
        self._lane_lines = {}                        # lane -> PlotDataItem (word MSB→LSB join line)
        self._port_colors = {}                       # lane -> QColor (stable across windows)

        self._plot = pg.PlotWidget(background=VizTheme.PLOT_BG, viewBox=XWheelViewBox(),
                                   axisItems={"bottom": _RawTimeAxis(orientation="bottom")})
        self._plot.showGrid(x=True, y=False, alpha=0.2)
        self._plot.setMouseEnabled(x=True, y=False)
        self._plot.setMenuEnabled(False)
        self._plot.getAxis("left").setTicks([[(2.0, "DP"), (0.5, "DN")]])
        self._plot.setLabel("bottom", "Time")    # unit (s/ms/µs) shown per tick, comma-separated
        self._plot.setYRange(-0.3, 2.8)
        # CDS/commit marker Y extents (line bottom/top, arrow-head). Collapse with the
        # Y range when the clock is hidden so the markers stay visible over the rescaled
        # data instead of being clipped off the top.
        self._cds_y_bot, self._cds_y_top, self._cds_y_arrow = _CDS_Y_BOT, _CDS_Y_TOP, _CDS_Y_ARROW
        self._dp_curve = self._plot.plot(pen=pg.mkPen(_DP_COLOR, width=2))
        self._dn_curve = self._plot.plot(pen=pg.mkPen(_DN_COLOR, width=2))
        # CDS-column markers: faint full-height verticals (one x-pair per column,
        # drawn as disjoint segments) plus a down-arrow head at the top of each.
        self._cds_lines = pg.PlotDataItem(connect="pairs",
                                          pen=pg.mkPen(self._cds_pen_color(), width=1))
        self._cds_arrows = pg.ScatterPlotItem(symbol="t", size=9, pxMode=True,
                                              brush=pg.mkBrush(_CDS_COLOR), pen=None)
        self._plot.addItem(self._cds_lines)
        self._plot.addItem(self._cds_arrows)
        # Commit Row-Sync-Point markers: a bolder SOLID full-height gold vertical +
        # a same-size arrow, so a confirmed commit's RSP matches the timeline's
        # confirmed-commit tick and stands out from the neutral CDS row-syncs.
        self._commit_lines = pg.PlotDataItem(connect="pairs",
                                             pen=pg.mkPen(_COMMIT_COLOR, width=2))
        self._commit_arrows = pg.ScatterPlotItem(symbol="t", size=9, pxMode=True,
                                                 brush=pg.mkBrush(_COMMIT_COLOR), pen=None)
        self._plot.addItem(self._commit_lines)
        self._plot.addItem(self._commit_arrows)
        # Per-port sample markers are ScatterPlotItems created on demand in
        # set_port_marks (one per active device/dp/channel, so the legend + colours
        # are stable); a legend maps colour -> port and also keys the CDS marker.
        self._port_legend = self._plot.addLegend(offset=(-8, 8), labelTextColor=VizTheme.TEXT)
        # A semi-opaque background so the legend stays readable over the waveform.
        _lg_bg = QColor(VizTheme.FRAME_BG); _lg_bg.setAlpha(220)
        self._port_legend.setBrush(pg.mkBrush(_lg_bg))
        self._port_legend.setPen(pg.mkPen(VizTheme.BORDER))
        self._port_legend.addItem(self._cds_arrows, "CDS · Row Sync")
        self._port_legend.addItem(self._commit_arrows, "Commit")
        self._cursor = pg.InfiniteLine(angle=90, movable=False,
                                       pen=pg.mkPen(_CURSOR_COLOR, width=1))
        self._plot.addItem(self._cursor)
        self._plot.scene().sigMouseClicked.connect(self._on_click)
        # Re-render the step traces for whatever sample window is in view as the
        # user zooms/pans — so the whole capture is always available (independent
        # of the time cursor) without building step arrays for every edge at once.
        # Coalesce bursts through a short timer: a fast wheel-zoom fires many range
        # changes, and rebuilding the step arrays synchronously on each one backs up
        # the event loop (laggy, and the zoom overshoots). The timer throttles the
        # rebuild to ~1 per interval, always rendering the LATEST range.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(30)
        self._render_timer.timeout.connect(self._render_visible)
        self._plot.getViewBox().sigXRangeChanged.connect(self._on_xrange)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self._cds_btn = QPushButton("Hide CDS Markers")
        self._cds_btn.setCheckable(True)
        self._cds_btn.setChecked(True)
        self._cds_btn.setToolTip("Mark the clock edge that opens each Column 0 (CDS) and each Commit")
        self._cds_btn.toggled.connect(self._on_cds_toggled)
        self._samp_btn = QPushButton("Show Samples")
        self._samp_btn.setCheckable(True)
        self._samp_btn.setChecked(False)
        self._samp_btn.setToolTip("Mark where each active data port's value is sampled "
                                  "(colour-coded per device/dp) — collected on first use, "
                                  "visible when zoomed in")
        self._samp_btn.toggled.connect(self._on_ports_toggled)
        # Hide/Show the forwarded-clock trace so the data line is easier to read.
        self._clock_btn = QPushButton("Hide Clock")
        self._clock_btn.setCheckable(True)
        self._clock_btn.setChecked(True)
        self._clock_btn.setToolTip("Hide the forwarded-clock trace (DP or DN, whichever "
                                   "carries the clock) to declutter the data line")
        self._clock_btn.toggled.connect(self._on_clock_toggled)
        self._show_clock = True
        two_row = QPushButton("Row Zoom")
        two_row.setToolTip("Zoom to two bus rows around the cursor")
        two_row.clicked.connect(lambda: self.zoom_to_rows(2))
        reset = QPushButton("Reset Zoom")
        reset.clicked.connect(self.reset_zoom)
        # Jog: page one whole visible width left/right (keyboard < and >).
        jog_l = QPushButton("Jog <")
        jog_l.setToolTip("Page one whole visible width left (shortcut: <)")
        jog_l.clicked.connect(lambda: self.jog(-1))
        jog_r = QPushButton("Jog >")
        jog_r.setToolTip("Page one whole visible width right (shortcut: >)")
        jog_r.clicked.connect(lambda: self.jog(+1))
        # Focus-scoped so </> jog THIS pane (not the Audio pane's identical shortcuts).
        install_jog_shortcuts(self, self.jog)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.addStretch(1)
        bar.addWidget(jog_l)
        bar.addWidget(jog_r)
        bar.addWidget(self._cds_btn)
        bar.addWidget(self._samp_btn)
        bar.addWidget(self._clock_btn)
        bar.addWidget(two_row)
        bar.addWidget(reset)
        lay.addLayout(bar)
        lay.addWidget(self._plot)

    def _cds_pen_color(self) -> QColor:
        """A translucent CDS colour for the marker verticals so they locate each CDS
        column without masking the underlying DP/DN waveform."""
        c = QColor(_CDS_COLOR)
        c.setAlpha(110)
        return c
    def reset_zoom(self) -> None:
        """Re-fit the X axis to the whole capture (the initial view), leaving blank
        space on the right so the (top-right) legend clears the waveform."""
        x1 = self._total / self._rate
        self._plot.setXRange(0, x1 * (1.0 + _RIGHT_PAD), padding=0.0)

    def jog(self, direction: int) -> None:
        """Page the view one whole visible width left (direction < 0) or right
        (> 0), keeping the zoom span — step across the capture a screen at a time.
        Clamped to the capture extent. No-op when nothing is loaded."""
        if self._total <= 1:
            return
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        total_s = (self._total / self._rate) * (1.0 + _RIGHT_PAD) if self._rate else x1
        rng = paged_range(x0, x1, 0.0, total_s, direction)
        if rng is not None:
            self._plot.setXRange(rng[0], rng[1], padding=0.0)

    def _on_clock_toggled(self, on: bool) -> None:
        """Hide/Show the forwarded-clock trace."""
        self._show_clock = bool(on)
        self._clock_btn.setText("Hide Clock" if on else "Show Clock")
        self._apply_clock_visibility()

    @property
    def show_clock(self) -> bool:
        """Whether the forwarded-clock trace is shown (for workspace save)."""
        return self._show_clock

    def set_show_clock(self, on: bool) -> None:
        """Show/hide the clock trace (workspace restore). Drives the button so the
        label + visibility follow."""
        self._clock_btn.setChecked(bool(on))

    def _apply_clock_visibility(self) -> None:
        """Show/hide whichever physical trace (DP or DN) carries the clock. The data
        line is on DP when _dp_is_data, so the clock is the OTHER trace. Always re-show
        the data curve: an FBCSE flip between captures swaps which curve is the clock,
        so without this the previously-hidden clock could stay hidden as the new data
        line and the data trace would vanish. When the clock is hidden, collapse the
        Y range/axis to just the data band so the data fills the pane (like AudioView),
        instead of leaving a blank half where the clock was."""
        clock_curve = self._dn_curve if self._dp_is_data else self._dp_curve
        data_curve = self._dp_curve if self._dp_is_data else self._dn_curve
        clock_curve.setVisible(self._show_clock)
        data_curve.setVisible(True)
        axis = self._plot.getAxis("left")
        if self._show_clock:
            self._plot.setYRange(-0.3, 2.8, padding=0)
            axis.setTicks([[(2.0, "DP"), (0.5, "DN")]])
            self._cds_y_bot, self._cds_y_top, self._cds_y_arrow = _CDS_Y_BOT, _CDS_Y_TOP, _CDS_Y_ARROW
        else:
            lo, hi, tick, name = ((_DP_BAND_LO, _DP_BAND_HI, 2.0, "DP") if self._dp_is_data
                                  else (_DN_BAND_LO, _DN_BAND_HI, 0.5, "DN"))
            ylo, yhi = lo - 0.2, hi + 0.4
            self._plot.setYRange(ylo, yhi, padding=0)
            axis.setTicks([[(tick, name)]])
            # Span the CDS/commit rules across the collapsed view; keep the arrow head
            # just inside the top so the markers (and per-bit sample dots) stay visible.
            self._cds_y_bot, self._cds_y_top, self._cds_y_arrow = ylo, yhi - 0.14, yhi - 0.05
        self._refresh_cds()
        self._refresh_commits()

    def set_row_samples(self, samples_per_row: float) -> None:
        """Nominal samples per bus row (from the decode's row rate) — the window
        size hint and fallback span for zoom-to-row. 0 clears it."""
        self._row_samples = float(samples_per_row) if samples_per_row and samples_per_row > 0 else 0.0

    def set_ui_samples(self, samples_per_ui: float) -> None:
        """Samples per unit interval (sample_rate / UI rate). Used to floor the
        maximum zoom-in at ~1 UI so the user can't zoom past a single UI into
        meaningless sub-UI space. 0 clears the limit."""
        self._ui_samples = float(samples_per_ui) if samples_per_ui and samples_per_ui > 0 else 0.0
        self._apply_zoom_limit()

    def _apply_zoom_limit(self) -> None:
        """Bound the X view: don't zoom OUT past the whole capture (plus the small
        right pad that clears the legend) and don't pan off either end; floor the
        max zoom-IN at ~1 UI."""
        vb = self._plot.getViewBox()
        total_s = (self._total / self._rate) if self._rate else 0.0
        max_s = total_s * (1.0 + _RIGHT_PAD) if total_s > 0 else None
        min_x = (self._ui_samples / self._rate) if (self._ui_samples > 0 and self._rate) else None
        vb.setLimits(xMin=(0.0 if total_s > 0 else None), xMax=max_s,
                     maxXRange=max_s, minXRange=min_x)

    def _row_bounds(self, cur: int, n_rows: int = 1):
        """The [start, end] samples spanning `n_rows` bus rows around sample `cur`,
        snapped to the CDS Row-Sync-Point edges (so zoom-to-row lands on real row
        boundaries, honouring any mid-stream column-count/rate change). The row under
        the cursor is included and roughly centred. None if the CDS marks aren't
        available to snap to."""
        if self._cds_provider is None:
            return None
        hint = self._row_samples or max(1.0, self._total / 400.0)
        span = int((n_rows + 6) * hint)
        lo = max(0, int(cur - span))
        hi = min(self._total, int(cur + span))
        edges = np.asarray(self._cds_provider(lo, hi))
        if edges.size < 2:
            return None
        edges = np.sort(edges.astype(np.int64))
        le = int(np.searchsorted(edges, cur, side="right")) - 1   # row start at/before cur
        if le < 0:
            le = 0
        start_i = max(0, le - (n_rows - 1) // 2)
        end_i = start_i + n_rows
        if end_i > edges.size - 1:
            end_i = edges.size - 1
            start_i = max(0, end_i - n_rows)
        if end_i <= start_i:
            return None
        return int(edges[start_i]), int(edges[end_i])

    def zoom_to_rows(self, n_rows: int = 1) -> None:
        """Zoom the X axis to `n_rows` bus rows around the cursor — snapped to the CDS
        Row-Sync-Point edges when available, else a nominal window centred on the
        cursor. A small margin keeps the row edges off the frame."""
        if self._total <= 1:
            return
        n_rows = max(1, int(n_rows))
        cur = int(self._cursor.value() * self._rate)
        bounds = self._row_bounds(cur, n_rows)
        if bounds is not None:
            lo, hi = bounds
        else:
            w = (self._row_samples or max(1.0, self._total / 400.0)) * n_rows
            lo, hi = cur - w / 2.0, cur + w / 2.0
        to_s = 1.0 / self._rate
        pad = (hi - lo) * 0.12
        self._plot.setXRange((lo - pad) * to_s, (hi + pad) * to_s, padding=0.0)

    def zoom_to_row(self) -> None:
        """Zoom to a single bus row around the cursor (the "1 Row" button)."""
        self.zoom_to_rows(1)

    def center_on_sample(self, sample: int, n_rows: int = 2) -> None:
        """Zoom to ~`n_rows` bus rows CENTERED on `sample`. Unlike zoom_to_rows (which
        snaps to the row boundaries at/after the cursor, leaving the event at the row's
        left edge), this puts `sample` in the middle — used by the Timing pane's 'jump to
        tightest margin' so the event's clock edge sits at the center of the pane."""
        if self._total <= 1:
            return
        row = self._row_samples or max(1.0, self._total / 400.0)
        half = max(1.0, n_rows * row) / 2.0
        to_s = 1.0 / self._rate
        self._plot.setXRange((sample - half) * to_s, (sample + half) * to_s, padding=0.0)

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the line colours, the
        plot background and the item-view stylesheet, then re-render the visible
        window with the new pens."""
        global _DP_COLOR, _DN_COLOR, _CURSOR_COLOR, _CDS_COLOR, _COMMIT_COLOR
        _DP_COLOR = VizTheme.RAW_DP
        _DN_COLOR = VizTheme.RAW_DN
        _CURSOR_COLOR = VizTheme.CURSOR
        _CDS_COLOR = VizTheme.TEXT          # neutral high-contrast (the repeating CDS grid)
        _COMMIT_COLOR = VizTheme.SEM_SSP    # gold, matching the timeline's confirmed-commit tick
        self.setStyleSheet(analyzer_stylesheet())
        self._plot.setBackground(VizTheme.PLOT_BG)
        self._dp_curve.setPen(pg.mkPen(_DP_COLOR, width=2))
        self._dn_curve.setPen(pg.mkPen(_DN_COLOR, width=2))
        self._cds_lines.setPen(pg.mkPen(self._cds_pen_color(), width=1))
        self._cds_arrows.setBrush(pg.mkBrush(_CDS_COLOR))
        self._commit_lines.setPen(pg.mkPen(_COMMIT_COLOR, width=2))
        self._commit_arrows.setBrush(pg.mkBrush(_COMMIT_COLOR))
        self._recolor_ports()             # port palette + swatches follow the theme
        self._cursor.setPen(pg.mkPen(_CURSOR_COLOR, width=1))
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        self._render_range(int(x0 * self._rate), int(x1 * self._rate))

    def set_cds_provider(self, provider) -> None:
        """Bind the callback that yields Column-0 (CDS) edge sample numbers for a
        visible window — `provider(lo_sample, hi_sample) -> np.ndarray`, empty when
        there are none or the window is too dense to mark. Pass None to clear. The
        current window is re-marked immediately."""
        self._cds_provider = provider
        self._refresh_cds()

    def _on_cds_toggled(self, on: bool) -> None:
        self._show_cds = bool(on)
        self._cds_btn.setText("Hide CDS Markers" if on else "Show CDS Markers")
        self._refresh_cds()
        self._refresh_commits()

    def _refresh_cds(self) -> None:
        """Query the provider for the visible window and redraw the CDS markers
        (verticals + arrow heads). Clears them when hidden or unavailable."""
        if not (self._show_cds and self._cds_provider is not None) or self._total <= 1:
            self._cds_lines.setData([], [])
            self._cds_arrows.setData([], [])
            return
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        lo, hi = int(x0 * self._rate), int(x1 * self._rate)
        samples = np.asarray(self._cds_provider(lo, hi))
        if samples.size == 0:
            self._cds_lines.setData([], [])
            self._cds_arrows.setData([], [])
            return
        xs = samples.astype(np.float64) / self._rate
        lx = np.repeat(xs, 2)                                  # x-pairs for disjoint verticals
        ly = np.tile((self._cds_y_bot, self._cds_y_top), xs.size)
        self._cds_lines.setData(lx, ly)
        self._cds_arrows.setData(xs, np.full(xs.size, self._cds_y_arrow))

    def set_commit_provider(self, provider) -> None:
        """Bind the callback that yields confirmed-Commit Row-Sync-Point sample
        numbers for a visible window — `provider(lo, hi) -> np.ndarray`. Pass None to
        clear. Drawn distinctly from CDS RSPs; shown under the same CDS toggle."""
        self._commit_provider = provider
        self._refresh_commits()

    def _refresh_commits(self) -> None:
        """Query the commit provider for the visible window and redraw commit RSP
        markers (solid blue verticals + larger arrows). Gated by the CDS toggle."""
        if not (self._show_cds and self._commit_provider is not None) or self._total <= 1:
            self._commit_lines.setData([], [])
            self._commit_arrows.setData([], [])
            return
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        lo, hi = int(x0 * self._rate), int(x1 * self._rate)
        samples = np.asarray(self._commit_provider(lo, hi))
        if samples.size == 0:
            self._commit_lines.setData([], [])
            self._commit_arrows.setData([], [])
            return
        xs = samples.astype(np.float64) / self._rate
        lx = np.repeat(xs, 2)
        ly = np.tile((self._cds_y_bot, self._cds_y_top), xs.size)
        self._commit_lines.setData(lx, ly)
        self._commit_arrows.setData(xs, np.full(xs.size, self._cds_y_arrow))

    def _on_ports_toggled(self, on: bool) -> None:
        self._show_ports = bool(on)
        self._samp_btn.setText("Hide Samples" if on else "Show Samples")
        if on:
            self.portSamplesRequested.emit()   # owner ensures bit samples are collected
        self._refresh_port_legend()            # only key the ports when they're shown
        self._refresh_ports()

    def _refresh_port_legend(self) -> None:
        """Show the per-port legend entries only while the overlay is enabled — so a
        capture opened with Sample Bits off doesn't list ports that aren't drawn
        (which reads as "decoded but not shown"). CDS/Commit keys always remain."""
        for item in self._lane_dots.values():
            try:
                self._port_legend.removeItem(item)
            except Exception:
                pass
        if self._show_ports:
            for key in self._lanes:
                d, p, ch = key
                self._port_legend.addItem(self._lane_dots[key], f"Dev{d} DP{p} CH{ch}")

    def set_port_marks(self, provider, lanes) -> None:
        """Bind the per-bit sample-point provider and the ordered list of active
        `(device, dp, channel)` lanes (from the audio store). `provider(lo, hi)`
        returns parallel arrays `sample, device, dp, channel, is_start` for the data
        bits sampled in the window (empty when none / too dense). Each lane gets its
        own stacked strip with a stable colour; a filled circle at every sampled bit,
        drawn larger at each sample-interval start (the MSB). Legend entries for the
        ports appear only while the overlay is enabled."""
        self._port_provider = provider
        for item in self._lane_dots.values():
            self._plot.removeItem(item)
            try:
                self._port_legend.removeItem(item)
            except Exception:
                pass
        for line in self._lane_lines.values():
            self._plot.removeItem(line)
        self._lane_dots.clear()
        self._lane_lines.clear()
        self._port_colors.clear()
        self._lanes = [(int(d), int(p), int(ch)) for d, p, ch in (lanes or [])]
        palette = VizTheme.TRACE_PALETTE
        for i, key in enumerate(self._lanes):
            color = QColor(palette[i % len(palette)])
            self._port_colors[key] = color
            # Line first (drawn under the dots): joins a word's bits MSB→LSB.
            line = pg.PlotDataItem(connect="pairs", pen=pg.mkPen(color, width=2))
            self._plot.addItem(line)
            self._lane_lines[key] = line
            dots = pg.ScatterPlotItem(symbol="o", size=_BIT_DOT, pxMode=True,
                                      brush=pg.mkBrush(color), pen=None)
            self._plot.addItem(dots)
            self._lane_dots[key] = dots
        self._refresh_port_legend()
        self._refresh_ports()

    def _recolor_ports(self) -> None:
        """Re-apply the (theme-derived) palette to the existing per-lane items."""
        palette = VizTheme.TRACE_PALETTE
        for i, key in enumerate(self._lanes):
            color = QColor(palette[i % len(palette)])
            self._port_colors[key] = color
            self._lane_dots[key].setBrush(pg.mkBrush(color))
            if key in self._lane_lines:
                self._lane_lines[key].setPen(pg.mkPen(color, width=2))

    def _lane_y(self, key) -> float:
        """Centre height for a lane's dots: lanes are stacked evenly inside the data
        trace band so each (device, dp, channel) is visually separate."""
        lo, hi = (_DP_BAND_LO, _DP_BAND_HI) if self._dp_is_data else (_DN_BAND_LO, _DN_BAND_HI)
        n = max(1, len(self._lanes))
        try:
            i = self._lanes.index(key)
        except ValueError:
            i = 0
        return lo + (hi - lo) * (i + 1) / (n + 1)

    def _refresh_ports(self) -> None:
        """For each active (device, dp, channel) lane in view, draw a filled circle
        at every sampled data bit (larger at each sample interval's start, the MSB),
        centred on the lane. This shows exactly which clock edges are the port's bit
        samples. Cleared when hidden, unavailable, or too dense."""
        if not (self._show_ports and self._port_provider is not None) or self._total <= 1:
            for it in self._lane_dots.values():
                it.setData([], [])
            for ln in self._lane_lines.values():
                ln.setData([], [])
            return
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        lo, hi = int(x0 * self._rate), int(x1 * self._rate)
        # Fetch a little beyond the visible window so a word straddling an edge is
        # seen whole (true MSB/LSB known). The line is then clamped to the visible
        # edges: it reaches the edge when its endpoint dot is off-screen, but never
        # extends past the real MSB/LSB. ~64 UIs covers any single word.
        pad = int((self._ui_samples or max(1.0, (hi - lo) / 200.0)) * 64)
        marks = self._port_provider(lo - pad, hi + pad) or {}
        sample = np.asarray(marks.get("sample", ()), dtype=np.int64)
        if sample.size == 0:
            for it in self._lane_dots.values():
                it.setData([], [])
            for ln in self._lane_lines.values():
                ln.setData([], [])
            return
        dev = np.asarray(marks.get("device", ()), dtype=np.int64)
        dp = np.asarray(marks.get("dp", ()), dtype=np.int64)
        ch = np.asarray(marks.get("channel", ()), dtype=np.int64)
        is_start = np.asarray(marks.get("is_start", ()), dtype=bool)
        to_s = 1.0 / self._rate
        for key in self._lanes:
            d, p, c = key
            sel = (dev == d) & (dp == p) & (ch == c)
            s = sample[sel]
            item = self._lane_dots[key]
            line = self._lane_lines[key]
            if s.size == 0:
                item.setData([], [])
                line.setData([], [])
                continue
            st = is_start[sel]
            order = np.argsort(s, kind="stable")       # bits in sample order (MSB→LSB)
            s = s[order]; st = st[order]
            y = self._lane_y(key)
            xs = s.astype(np.float64) * to_s
            # Dots only for bits actually in view (off-screen bits stay hidden).
            vis = (xs >= x0) & (xs <= x1)
            sizes = np.where(st[vis], _MSB_DOT, _BIT_DOT)   # MSB dots a bit larger
            item.setData(xs[vis], np.full(int(vis.sum()), y), size=sizes)
            # Join each word's bits with a horizontal segment MSB→LSB, clamped to the
            # visible edges. Words are delimited by is_start; 1-bit PDM gets no line.
            seg_x = self._word_segment_xs(xs, st, x0, x1)
            line.setData(seg_x, np.full(len(seg_x), y))

    @staticmethod
    def _word_segment_xs(xs: np.ndarray, is_start: np.ndarray,
                         x_lo: float, x_hi: float) -> list:
        """x pairs (for connect='pairs') joining each word's first bit (MSB) to its
        last, clamped to the visible [x_lo, x_hi] so a word whose MSB/LSB dot is
        off-screen still reaches the edge — but never extends past the real MSB/LSB.
        Bits are in sample order with word starts flagged by `is_start`; a single-bit
        word (e.g. 1-bit PDM) contributes no segment."""
        n = len(xs)
        if n == 0:
            return []
        starts = [i for i in range(n) if is_start[i]]
        bounds = sorted(set([0] + starts + [n]))       # leading partial word starts at 0
        out: list = []
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 2:                               # single bit → no line
                continue
            seg_lo = max(float(xs[a]), x_lo)            # clamp inward to the visible edge
            seg_hi = min(float(xs[b - 1]), x_hi)
            if seg_hi > seg_lo:                         # skip words entirely off-screen
                out.extend([seg_lo, seg_hi])
        return out

    def set_capture(self, capture, dp_is_data_line: bool = False) -> None:
        """Load the two link lines. The capture is stored in *audio* orientation
        (clock_edges = the audio clock). In FBCSE that audio clock is physically DN
        and the data line is DP, so `dp_is_data_line` (from the link-control decode)
        tells us which stored array is physically DP vs DN — we label/stack them
        accordingly so the bring-up (a DP event) shows on the DP trace."""
        clk = np.asarray(capture.clock_edges, dtype=np.uint64)
        dat = np.asarray(capture.data_edges, dtype=np.uint64)
        clk_init, dat_init = bool(capture.initial_clock), bool(capture.initial_data)
        if dp_is_data_line:                          # DP = data line, DN = clock line
            self._dp_edges, self._dp_init = dat, dat_init
            self._dn_edges, self._dn_init = clk, clk_init
        else:                                        # DP = clock line, DN = data line
            self._dp_edges, self._dp_init = clk, clk_init
            self._dn_edges, self._dn_init = dat, dat_init
        # Remember which physical band carries the data line, so the per-port dots
        # stack within that trace's band.
        self._dp_is_data = bool(dp_is_data_line)
        self._apply_clock_visibility()       # keep the clock hidden/shown per the toggle
        prev_total, prev_rate = self._total, self._rate
        prev_x0, prev_x1 = self._plot.getViewBox().viewRange()[0]
        self._rate = int(capture.sample_rate_hz) or 1
        last = 0
        for e in (clk, dat):
            if e.size:
                last = max(last, int(e[-1]))
        self._total = max(1, last)
        self._apply_zoom_limit()             # bound zoom/pan to the new capture extent
        # A re-decode of the SAME capture (identical extent + rate) — enabling the lazy
        # Sample Bits overlay, or a what-if register/scrambler override — must NOT
        # disturb the user's current zoom or the Sample Bits toggle (both would
        # otherwise reset here, which reads as the view "jumping" on every override).
        # Only a genuinely new capture re-fits the view and resets the lazy overlay.
        same_capture = (prev_total == self._total and prev_rate == self._rate
                        and prev_total > 1)
        if same_capture:
            self._plot.setXRange(prev_x0, prev_x1, padding=0.0)
            self._render_range(int(prev_x0 * self._rate), int(prev_x1 * self._rate))
            return
        # A fresh capture starts with the (lazy) Sample Bits overlay OFF, so the
        # button label matches reality — the toggle state persists on the reused
        # widget otherwise, reading "Hide Samples" with nothing drawn until the
        # user toggles it. Block signals so this reset doesn't trigger a collect.
        self._samp_btn.blockSignals(True)
        self._samp_btn.setChecked(False)
        self._samp_btn.setText("Show Samples")
        self._samp_btn.blockSignals(False)
        self._show_ports = False
        # Show the whole capture initially (the user zooms/pans from there), with
        # blank space on the right so the legend clears the trace.
        self._plot.setXRange(0, (self._total / self._rate) * (1.0 + _RIGHT_PAD), padding=0.0)
        self._render_range(0, self._total)

    _MAX_EDGES = 60000                               # cap points per render (zoom-out)

    def _render_range(self, lo: int, hi: int) -> None:
        """Build DP/DN step traces for the sample window [lo, hi]. When the window
        spans more edges than we'll draw, decimate so a zoomed-out overview still
        renders instantly (full detail returns as you zoom in)."""
        if self._dp_edges.size == 0 and self._dn_edges.size == 0:
            return
        lo = max(0, int(lo))
        hi = max(lo + 1, int(hi))
        to_s = 1.0 / self._rate
        xd, yd = self._steps(self._dp_edges, self._dp_init, lo, hi, 1.6, 2.4)
        xn, yn = self._steps(self._dn_edges, self._dn_init, lo, hi, 0.1, 0.9)
        self._dp_curve.setData(np.asarray(xd) * to_s, yd)
        self._dn_curve.setData(np.asarray(xn) * to_s, yn)
        self._refresh_cds()               # CDS marks follow the same visible window
        self._refresh_commits()           # commit RSP marks too
        self._refresh_ports()             # active-port sample dots too

    def _steps(self, edges, initial, lo, hi, y0, y1):
        i0 = int(_ss(edges, lo, side="left"))
        i1 = int(_ss(edges, hi, side="right"))
        win = edges[i0:i1]
        if win.size > self._MAX_EDGES:               # too many to draw — decimate (overview)
            # Each edge is a level toggle, so a kept edge's level depends on how many
            # real edges precede it. Dropping every Nth edge stays level-correct only
            # when N is ODD (an even number of dropped toggles between kept edges nets
            # to zero); an even stride would invert the trace downstream. (This still
            # aliases sub-stride activity — a per-pixel high/low/transition envelope
            # would also show that, but parity correctness comes first.)
            stride = (int(win.size // self._MAX_EDGES) + 1) | 1
            win = win[::stride]
        init = bool(initial) ^ bool(i0 & 1)
        return _step_xy_from(win, init, lo, hi, y0, y1)

    def _on_xrange(self, _vb, _rng) -> None:
        # Coalesce a burst of range changes: (re)arm the timer and render the LATEST
        # range when it fires, instead of rebuilding synchronously on every event.
        if self._total <= 1:
            return
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render_visible(self) -> None:
        """Rebuild the step traces for the current visible window (timer callback)."""
        if self._rendering or self._total <= 1:
            return
        self._rendering = True
        try:
            x0, x1 = self._plot.getViewBox().viewRange()[0]
            self._render_range(int(x0 * self._rate), int(x1 * self._rate))
        finally:
            self._rendering = False

    def set_cursor(self, sample: int) -> None:
        """Move the cursor line to `sample`. If the cursor would fall outside the
        current zoom window, pan the view (keeping the same span — zoom is never
        changed) so it stays visible. Panning is skipped while the view is hidden (a
        background tab) so a stale/zero view range can't perturb the zoom; the line
        is repositioned and the existing range is kept for when the tab is shown."""
        t = sample / self._rate
        self._cursor.setValue(t)
        if not self.isVisible():
            return
        x0, x1 = self._plot.getViewBox().viewRange()[0]
        span = x1 - x0
        if span > 0 and not (x0 <= t <= x1):
            self._plot.setXRange(t - span / 2, t + span / 2, padding=0)

    def _on_click(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        vb = self._plot.getViewBox()
        x_s = vb.mapSceneToView(ev.scenePos()).x()
        self.sampleSelected.emit(int(x_s * self._rate))
