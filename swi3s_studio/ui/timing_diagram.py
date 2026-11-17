"""The Timing view's term diagram: one term of an inequality, drawn to scale in each column.

`TimingDiagram.show_term` draws the term the user clicked from each column's
`timing.timeline.LegTimeline` (the Timing view's two columns, stacked, or side by side with
`set_side_by_side`), each at the corner the calculator found worst for that
inequality. A panel shows only what the term connects:
- the ramps the term's span names (`Span.view`), and a clock's other edges at its pin within
  the panel's time window, one row per signal at each pin, named
  "Clk @ Peripheral" or "Data @ Manager" in its first edge's colour over the driver's V_DD
  label, a data edge in the colour of the device driving it (`_ink`: the Manager, a
  Peripheral or Peripheral A, Peripheral B), at every pin it is seen,
  each edge drawn as the curve the calculator models (linear or exponential) at its own t_RF,
  from the driver's ground (solid) to its rail;
- the level each of the term's ends is measured at, as a line: a clock reference or a
  launch anchor at 20 % of its ramp, across the left half (to the point, where that is
  further right), as a percentage of the driver's V_DD
  (20 % rising, 80 % falling); a detection or a crossing at the receiver's threshold, drawn
  from the receiver's ground (dashed) where that is offset from the driver's, with an arrow
  from that ground; under the line, the threshold's percentage of the driver's V_DD at the
  left, its bound at the centre and its percentage of the receiver's V_DD at the right;
- the driver's V_DD across the left half, and its ground across the row, or the left half
  where a receiver's offset ground takes the right half; the receiver's V_DD across the right
  half; each V_DD and an offset ground labelled as a percentage of nominal;
- a driver's state on its trace: solid while it drives, dotted while it is high-Z (after a
  release in the releasing driver's colour, and before a turn-on in the colour of the device
  that last drove the wire, the other device on a handover and the releaser on contention),
  labelled "high-Z"; a level held through the leg is drawn
  flat across the row;
- a ramp's 80 % point as a level line like an anchor's, a release as a tick across the trace,
  and the keeper having followed as a labelled point;
- the term as a dimensioned span with its length in ns, and below it, dim, the intervals the
  span's `guides` name (the margin beside a setup or hold window, a release, a turn-on or a
  keeper's response), over a time axis in ns from the term's start. The margin is labelled
  "margin" on every leg, green where it is a gap and red where it is negative. A point on no drawn ramp is a dashed line, labelled but for a deadline, which
  the window drawn to it names.
A span with no ramps (a flight mismatch) is drawn as its guides alone: each an arrow from its
start to its end in its signal's colour, labelled with its length, and the term between the
arrows' ends.
A row is scaled to the levels it draws, from the lowest ground to the highest rail.
EVERY TEXT IS PLACED CLEAR OF EVERY OTHER TEXT AND EVERY DRAWN LINE, INSIDE ITS PANEL'S PLOT.
A panel draws its lines first, then its text in order: the labels of points on no drawn ramp
(`_strip`), the dimensions' and the axis's labels, then each row's. Each label takes the
first spot of a fixed sequence whose box lies within the plot and, grown by `_pad`, meets no
line, dot or text box already drawn (`_put`):
- a level's label: its line's end (the driver's at the left, the receiver's at the right, a
  bound at the centre) on its side (away from where the edge rests at that end), slid along
  the line; then one label further off, slid, where no line but a guide lies between it and
  its line (a row's name, over its rail's label, starts one label off); then the same on the
  other side; then two to four labels off with a leader line to the line;
- a point's label: beside it to the right, to the left, centred, above it by preference and
  then under it, then moved out along its side;
- a dimension's label: centred under its line, slid along it, then over it;
- an axis number: under its tick, or none.
A label no spot of its sequence frees is placed on a grid around its anchor, nearest first,
with a leader line. The spots with no leader are tried before any with one, first clear of
the guides (the dotted and dashed lines from a point down to the dimension lines), then over
them, stage by stage or, where `_over_guides` does not hold, only once no stage has a spot
clear of them; a guide is drawn last, broken behind any
label placed over it. Every label is rich
text, its symbols subscripted (V_DD, Man_t_DD) as in the heading.
A widget given to `set_corner_widget` sits at the top right, and the leg's title wraps at the
width left of it (`_title_w`), so the title is never cut off or drawn under it; where the
title wraps onto more lines there than at the full width, it goes below the widget at the full
width instead (`_title_below`); a double-click emits `doubleClicked`. `set_shown_column` draws one column alone, in either layout.
While `set_pending` holds (a recompute runs), the diagram keeps its panels and its size but
draws only the leg's title and "Computing…" in their place, so no number is drawn from a
corner the recompute replaces.
A term the leg holds more than once (P→M's two flights) gets one panel per occurrence; a
launch and its SoundWire anchor correction are one panel, as they chain, each span on its
own dimension line. A panel's heading names its terms and their total: a span running
against the first is subtracted (−), so the spans' lengths, drawn unsigned, add up to the
total.

Text is laid out at the column's width, and the rows share the height the text leaves, so a
taller pane draws taller edges. A row is never shorter than the first of `_ROW_STEPS` at which
every label finds a free position with no leader line, else the step with the fewest labels
short of one (`_row_floor`), and the diagram's minimum height reserves it.

Every pen, brush, font and size is read from `diagram_style` at paint time: a line's
colour (per theme), weight and dash, the arrowheads, dots and ticks, and each text class's
font. A weight or a size is a fraction of the font's line height, so the strokes keep their
weight against the text at any font size, and a font's size is a multiple of the body font
(the widget's). Labels in a column narrower than `_LABEL_FULL_W` line heights take
`label.narrow` of their size. Every text box keeps `_PAD` line heights clear of the lines,
and at least half the widest pen (`_pad`). While `set_reduced` holds (the diagram is not maximized)
the body font, and with it every font, weight and size, is `REDUCED_SCALE` of its size. After
a settings change, `restyle` lays the diagram out again; a theme switch needs only a repaint.

The widget computes no timing: it draws what the timeline holds.

`paint_diagram` paints onto any painter's device laid out at the widget's size, and the
widget's own paint is that call. `export` writes the diagram through it as PNG (at a scale),
SVG or PDF, in the current palette or the light one, so a vector file holds vector lines and
text; it holds while `can_export` does (panels drawn, no recompute running), and
`exportableChanged` reports each change of that. Every font the diagram draws in takes
horizontal design metrics (`_design`), so a text is as wide on the screen as on an image at
any scale, in an SVG and in a PDF, and every device paints the layout the widget shows.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import partial
from html import escape
from typing import Any, Callable, Iterator, Optional, Sequence, cast

import numpy as np
from PySide6.QtCore import (
    QBuffer,
    QEvent,
    QIODevice,
    QMarginsF,
    QPointF,
    QRectF,
    QSize,
    QSizeF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
    QPen,
    QStaticText,
    QTextOption,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..timing.timeline import Event, LegTimeline, Ramp, Span, clock_edges
from . import diagram_style as ds
from .theme import VizTheme, palette_applied


@dataclass
class DiagramSection:
    """One column: its heading, the timeline (None with a `note` in its place), the
    breakdown's terms the spans index, and the corner. The corner and the note are rich text
    (`<sub>` subscripts, as the heading's symbols)."""
    title: str
    timeline: Optional[LegTimeline]
    terms: list = field(default_factory=list)
    corner: str = ""
    note: str = ""


@dataclass
class Panel:
    """One occurrence of the term in one column: the chained spans, the rows of ramps and
    the time window drawn. `timeline` None is a panel with only its note. `column` is the
    section's index."""
    title: str
    corner: str
    timeline: Optional[LegTimeline] = None
    spans: list[Span] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    rows: list[list[Ramp]] = field(default_factory=list)
    window: tuple[float, float] = (0.0, 1.0)
    note: str = ""
    column: int = 0
    guides: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class _Label:
    """A label on a line at `y` running from `x0` to `x1` (the plot's edges where None): at
    the line's left end, centre or right end (`at`), above or under it by preference
    (`above`), and on the line (`off` 0) or one label further off (1). A label at "point" is
    beside the point (`x0`, `y`), above it by preference. `font` names its text class."""
    y: float
    text: str
    colour: QColor
    at: str
    above: bool
    x0: Optional[float] = None
    x1: Optional[float] = None
    off: int = 0
    font: str = "level"        # its `diagram_style.FONTS` text class


# A label's candidate position: its box's top-left corner, and the point a leader line joins
# it to (None: no leader).
_Spot = tuple[float, float, Optional[QPointF]]
# A label's candidate positions, in order; a None between two ends a stage (`_put`).
_Spots = Iterator[Optional[_Spot]]
# A leader line: its points, from just off its label's box to its end.
_Lead = tuple[QPointF, ...]
# The spot `_put` chose for a label: its box and leader line (None: no leader); None, none.
_Chosen = Optional[tuple[QRectF, Optional[_Lead]]]
# A label `_put` placed: its text, its font's key, its spot and whether no spot was free.
_Placed = tuple[str, str, _Chosen, bool]
# The widget events after which the line height is measured and the labels placed anew: a
# font, style, palette or screen change.
_LAYOUT_EVENTS = frozenset({
    QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange,
    QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
    QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange})
_SPOTS_MAX = 256      # panel layouts `TimingDiagram._spots` keeps, the oldest dropped first
EXPORT_FORMATS = ("png", "svg", "pdf")    # the file formats `TimingDiagram.export` writes
_EXPORT_SCALE_MAX = 8.0                   # a PNG export's largest scale
_EXPORT_PNG_BYTES = 1 << 30               # a PNG export's largest raster, ARGB32
_SVG_FAMILY = re.compile(rb'font-family="([^"&]*)"')     # a font as the SVG writer names it


# Layout, in multiples of the font's line height.
_ROW_MIN = 4.0        # a signal row, at least; `_row_floor` tries `_ROW_STEPS` up from it
_ROW_STEPS = (_ROW_MIN, 5.0, 6.0, 7.0, 8.0, 10.0)    # the rows `_row_floor` tries, in order
_ROW_GROW = 1.25      # and then, while a label finds no free spot, each this much taller
_ROW_MAX = 40.0       # up to this
_ROW_ASPECT = 0.6     # and at most this fraction of the column's width
_DIM_H = 1.7          # a dimension line and its label
_AXIS_H = 1.8         # the time axis
_GAP_H = 1.4          # between panels
_TITLE_H = 1.9        # the leg's title, on one line (`_title_h`)
_FLIGHT_H = 2.6       # a flight's arrow and its label
_MARK_MARGIN = 0.6    # between the labels of points on no ramp and the first row
_FLIGHT_LEAD = 0.9    # above a flights panel's first arrow
_LEVEL_TOP = 1.2      # the highest level a row is scaled for, as a fraction of V_nom
_SAMPLES = 400        # points across a row
_PAD = 0.15           # the clearance kept around a text box, at least
_LABEL_GAP = 0.15     # between a level's label and its line
_STEP = 0.5           # a label's step along its line, and the leader grid's pitch
_GRID_NEAR = (24, 12)  # the leader grid's near cells: this many steps across and up or down
_LEADER_GAP = 0.3     # a headed leader's tip, short of the line it names, at least (`_leader_gap`)
_EPS = 1e-9
_FLOOR_DELAY_MS = 120  # a width change's `_row_floor` waits this long for the next one
_TOUCH = 1e-6         # px: a line or box this close inside a box's edge lies along it

# The scale of every font, weight and size while the diagram is not maximized (`set_reduced`).
REDUCED_SCALE = 0.7

# A column at least `_LABEL_FULL_W` line heights wide takes labels at their size, a narrower
# one at `label.narrow` of it.
_LABEL_FULL_W = 34.0

# `diagram_style` dash names as pen styles.
_DASH = {"solid": Qt.SolidLine, "dash": Qt.DashLine, "dot": Qt.DotLine,
         "dashdot": Qt.DashDotLine}
_FONT_WEIGHT = {"normal": QFont.Normal, "demibold": QFont.DemiBold, "bold": QFont.Bold}


def _row_name(r: Ramp) -> str:
    """A signal row's name: Clk @ Peripheral, Data @ Peripheral B."""
    return f"{'Clk' if r.signal == 'clock' else 'Data'} @ {r.pin}"


def _device(r: Ramp) -> str:
    """The device driving `r`, short: Man, Per, or Per A or Per B on a P→P leg."""
    if r.device:
        return r.device
    if r.driver == "Man":
        return "Man"
    return "Per A" if r.pin in ("Peripheral A", "Peripheral B") else "Per"


def _short_device(name: str) -> str:
    """A receiver's name, short: Manager → Man, Peripheral B → Per B."""
    return name.replace("Manager", "Man").replace("Peripheral", "Per")


def pct_text(frac: float) -> str:
    """A signed percentage of nominal, to a tenth where it is not whole: +5 %, −2.5 %, ±0 %."""
    pct = round(100.0 * frac, 1)
    if pct == 0:
        return "±0 %"
    return f"{pct:+.1f}".rstrip("0").rstrip(".").replace("-", "−") + " %"


def _settle_ns(r: Ramp) -> float:
    """From a ramp's start to where it is drawn as settled: the rail (linear), or 95 % of it
    (exponential)."""
    if r.shape == "linear":
        return r.t_rf_ns / max(r.swing_frac, 1e-9)
    return r.t_rf_ns * math.log(20.0, 4.0)


# Between a crossing's pair and its lane word in a span's label, X_MP,CLK (`crossing_label`).
LANE_SEP = ","


def crossing_label(pair: str, lane: str, sub: Callable[[str], str]) -> str:
    """A crossing's span label as rich text: its pair (`X_MP`, or `X_MP,B` naming a
    peripheral) and the lane word of its row (`Man_t_RF,CLK` gives CLK) in one subscript,
    joined by `LANE_SEP`: X<sub>MP,CLK</sub>, X<sub>MP,B,CLK</sub>."""
    html, word = sub(pair), lane.split(",")[-1]
    if html.endswith("</sub>"):
        return f"{html[:-len('</sub>')]}{LANE_SEP}{word}</sub>"
    return f"{html}<sub>{word}</sub>"


def _short_label(symbol: str, sub: Callable[[str], str]) -> str:
    """A span's label: the term's parameter, or for a crossing the coefficient's pair and the
    lane (`crossing_label`), as rich text."""
    if " · " in symbol:
        head, _, lane = symbol.partition(" · ")
        first = head.strip("()").split(" ")[0]          # the first crossing named
        pair = first.split(",")[0]                       # e.g. X_PM
        return crossing_label(pair, lane, sub)
    return sub(symbol)


def _rows(tl: LegTimeline, keys: tuple[str, ...],
          window: tuple[float, float] = (-math.inf, math.inf)) -> list[list[Ramp]]:
    """`keys`' ramps in rows: one per signal at a pin, clocks first, a row holding several
    edges only where they alternate in polarity. A clock row also holds every other edge the
    clock makes at its pin that is drawn in `window`, from its start to where it settles
    (`timeline.clock_edges`), so the clock is drawn with each edge it makes there."""
    rows: list[list[Ramp]] = []
    ramps = [tl.ramp(k) for k in dict.fromkeys(keys)]
    pins = list(dict.fromkeys(r.pin for r in ramps if r.signal == "clock"))
    lo, hi = window
    ramps += [r for pin in pins for r in clock_edges(tl, pin, lo, hi)
              if r.key not in keys and r.start_ns < hi and r.start_ns + _settle_ns(r) > lo]
    for want in ("clock", "data"):
        for r in sorted((r for r in ramps if r.signal == want), key=lambda r: r.start_ns):
            for row in rows:
                last = row[-1]
                if (last.pin, last.signal) == (r.pin, r.signal) and last.rising != r.rising:
                    row.append(r)
                    break
            else:
                rows.append([r])
    return rows


def panels(sections: list[DiagramSection], key: str,
           term_key: Callable[[str], str]) -> list[Panel]:
    """The panels drawing the term keyed `key` in each section."""
    out: list[Panel] = []
    for col, s in enumerate(sections):
        tl = s.timeline
        if tl is None:
            out.append(Panel(s.title, s.corner, note=s.note, column=col))
            continue
        spans = [sp for sp in tl.spans
                 if sp.term is not None and term_key(s.terms[sp.term].symbol) == key]
        if not spans:
            out.append(Panel(s.title, s.corner,
                             note="This term is not in this column's inequality.", column=col))
            continue
        groups: list[list[Span]] = []
        for sp in spans:                 # chained spans share an event and draw as one
            for g in groups:
                if any({sp.start, sp.end} & {o.start, o.end} for o in g):
                    g.append(sp)
                    break
            else:
                groups.append([sp])
        for i, g in enumerate(groups):
            view = tuple(k for sp in g for k in sp.view)
            guides = list(dict.fromkeys(gd for sp in g for gd in sp.guides))
            ts = [tl.event(e).t_ns for sp in g for e in (sp.start, sp.end)]
            ts += [tl.event(e).t_ns for gd in guides for e in gd[:2]]
            for k in view:
                r = tl.ramp(k)
                if r.edge:                  # a held level has no edge to show
                    ts += [r.start_ns, r.start_ns + _settle_ns(r)]
                if r.off_ns is not None:
                    ts.append(r.off_ns)
            lo, hi = min(ts), max(ts)
            pad = 0.08 * (hi - lo) + 0.3
            title = s.title + (f" ({i + 1} of {len(groups)})" if len(groups) > 1 else "")
            out.append(Panel(title, s.corner, tl, g,
                             [s.terms[sp.term].symbol for sp in g if sp.term is not None],
                             _rows(tl, view, (lo - pad, hi + pad)), (lo - pad, hi + pad),
                             column=col,
                             guides=guides))
    return out


def _ends(pn: Panel) -> list[Event]:
    """The panel's span and guide ends, each once, in time order."""
    assert pn.timeline is not None
    keys = dict.fromkeys([k for sp in pn.spans for k in (sp.start, sp.end)]
                         + [k for gd in pn.guides for k in gd[:2]])
    return sorted((pn.timeline.event(k) for k in keys), key=lambda e: e.t_ns)


def _dim_lanes(pn: Panel) -> int:
    """A panel's dimension lines: one per chained span, and one for the guides."""
    return max(len(pn.spans), 1) + (1 if pn.guides else 0)


def _on_drawn_ramp(pn: Panel, e: Event) -> bool:
    """Whether the event `e` is a point on one of the panel's drawn ramps."""
    return e.ramp is not None and any(r.key == e.ramp for row in pn.rows for r in row)


def _nice_step(span_ns: float) -> float:
    """A tick step giving about six ticks across `span_ns`."""
    raw = span_ns / 6.0
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1.0, 2.0, 5.0, 10.0):
        if m * mag >= raw:
            return m * mag
    return 10.0 * mag


def _signed_ns(d_ns: float) -> str:
    """A margin to the hundredth with its sign (U+2212 for a minus); a value that rounds to
    zero carries no sign."""
    r = round(d_ns, 2)
    if r == 0.0:
        return "0.00"
    return f"+{r:.2f}" if r > 0 else f"\u2212{abs(r):.2f}"


def _colour(key: str) -> QColor:
    """A `diagram_style` colour in the active theme."""
    return QColor(ds.color(key))


def _margin_colour(d_ns: float) -> QColor:
    """A margin's colour: the gap colour for a gap, the short colour where it is negative."""
    return _colour("margin_ok" if d_ns >= 0.0 else "margin_bad")


def _point_colour(kind: str) -> QColor:
    """A point's colour by its event kind: a reference or an anchor as the 20 % level, a
    ramp's 80 % point as its level line, a release as its tick, the keeper's response its
    own, the rest (a detection or a crossing) as the threshold it is read on."""
    return _colour({"reference": "reference", "anchor": "reference", "valid": "valid",
                    "release": "release", "keeper": "keeper"}.get(kind, "threshold"))


def _signal_key(signal: str) -> str:
    """A signal's line class: "clock" or "data"."""
    return "clock" if signal == "clock" else "data"


def _ink_of(signal: str) -> QColor:
    """A signal's edge colour: "clock" or "data"."""
    return _colour(_signal_key(signal))


def _ink_key(r: Ramp) -> str:
    """The colour key a ramp is drawn in: "clock" for a clock edge; for data the device
    driving it (`_device`), "data" for the Manager, "data_per_b" for Peripheral B and
    "data_per" for a Peripheral or Peripheral A, at whichever pin it is seen."""
    if r.signal == "clock":
        return "clock"
    return _device_key(_device(r))


def _device_key(device: str) -> str:
    """A driving device's data colour key: "data" for the Manager ("Man"), "data_per_b" for
    Peripheral B, "data_per" for a Peripheral or Peripheral A."""
    return {"Man": "data", "Per B": "data_per_b"}.get(device, "data_per")


def _undriven_colour(r: Ramp) -> QColor:
    """The colour of turn-on `r`'s undriven stretch: the device that last drove the wire
    (`Ramp.before`), whose level the keeper holds; the data colour where the leg does not
    say."""
    return _colour(_device_key(r.before) if r.before else "data")


def _ink(r: Ramp) -> QColor:
    """A ramp's colour (`_ink_key`)."""
    return _colour(_ink_key(r))


def _design(font: QFont) -> QFont:
    """A copy of `font` hinted vertically at most, so its glyphs advance by their design widths
    on every paint device: the widths an SVG or a PDF lays its text out in, and an image's at
    any scale."""
    f = QFont(font)
    f.setHintingPreference(QFont.PreferVerticalHinting)
    return f


def _seg_hits(x0: float, y0: float, x1: float, y1: float, r: QRectF) -> bool:
    """Whether the segment from (x0, y0) to (x1, y1) passes through the inside of `r`; a
    segment along or ending on its edge does not. A segment parallel to an edge and within
    `_TOUCH` px inside it lies along it, so a box tangent to a line is judged the same
    wherever rounding puts the two."""
    lo, hi = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for pk, qk in ((-dx, x0 - r.left()), (dx, r.right() - x0),
                   (-dy, y0 - r.top()), (dy, r.bottom() - y0)):
        if abs(pk) < 1e-12:
            if qk <= _TOUCH:
                return False
            continue
        tk = qk / pk
        if pk < 0:
            lo = max(lo, tk)
        else:
            hi = min(hi, tk)
        if hi - lo <= 1e-9:
            return False
    return True


def _seg_array(segs: list[tuple[float, float, float, float]]) -> np.ndarray:
    """`segs` as eight contiguous columns, one row of the array each: x0, y0, x1, y1 and
    their bounds, x min, x max, y min, y max."""
    a = np.array(segs, dtype=float).reshape(-1, 4).T
    return np.ascontiguousarray(np.vstack([a, np.minimum(a[0], a[2]), np.maximum(a[0], a[2]),
                                           np.minimum(a[1], a[3]), np.maximum(a[1], a[3])]))


def _crosses(segs: np.ndarray, r: QRectF) -> bool:
    """Whether any of `segs` (a `_seg_array`) passes through the inside of `r`."""
    if not segs.shape[1]:
        return False
    near = np.flatnonzero((segs[4] < r.right()) & (segs[5] > r.left())
                          & (segs[6] < r.bottom()) & (segs[7] > r.top()))
    if not len(near):
        return False
    return any(_seg_hits(x0, y0, x1, y1, r) for x0, y0, x1, y1 in segs[:4, near].T.tolist())


class _Ink:
    """What one panel has drawn, for placing its text: its line segments, its guides (the
    lines from a point down to the dimension lines, drawn last), its dots, the text boxes
    placed so far and the plot rect it draws in. A box is free where it lies within the plot
    and, grown by `pad`, meets no segment, dot or box (nor guide, unless `over_guides`), and
    its leader line, if it has one, crosses no box; one within `_TOUCH` of another's edge
    touches it and does not meet it; nor do the strokes of its arrowhead, where it has one.
    A headed leader's head also keeps clear of every other head and the leader crosses no
    other head, unless `strict` is negative; as `strict` asks, the leader also crosses no
    other headed leader, and the head keeps clear of every segment and the leader crosses no
    line but a guide or one drawn `soft` (a rail or a level line) (`head_clear`)."""

    def __init__(self, plot: QRectF, pad: float) -> None:
        self.plot = plot
        self.pad = pad
        self.boxes: list[QRectF] = []
        self.dots: list[QRectF] = []
        self.heads: list[QRectF] = []    # each headed leader's head (`head_bounds`)
        self.leads: list[tuple[QPointF, QPointF]] = []    # and the leader's pieces
        self.guides: list[tuple[QPointF, QPointF, QPen]] = []
        self._segs: list[tuple[float, float, float, float]] = []
        self._arr: Optional[np.ndarray] = None
        self._hard: list[tuple[float, float, float, float]] = []    # the segments not `soft`
        self._hard_arr: Optional[np.ndarray] = None
        self._guide_arr: Optional[np.ndarray] = None

    def line(self, a: QPointF, b: QPointF, soft: bool = False) -> None:
        self._segs.append((a.x(), a.y(), b.x(), b.y()))
        self._arr = None
        if not soft:
            self._hard.append(self._segs[-1])
            self._hard_arr = None

    def polyline(self, pts: list[QPointF]) -> None:
        for a, b in zip(pts, pts[1:]):
            self.line(a, b)

    def guide(self, a: QPointF, b: QPointF, pen: QPen) -> None:
        self.guides.append((a, b, QPen(pen)))
        self._guide_arr = None

    def free(self, r: QRectF, leader: Optional[_Lead] = None,
             over_guides: bool = False,
             strokes: Sequence[tuple[QPointF, QPointF]] = (), strict: int = 2) -> bool:
        pl, e = self.plot, 0.01
        if (r.left() < pl.left() - e or r.right() > pl.right() + e
                or r.top() < pl.top() - e or r.bottom() > pl.bottom() + e):
            return False
        g = r.adjusted(-self.pad, -self.pad, self.pad, self.pad)
        t = g.adjusted(_TOUCH, _TOUCH, -_TOUCH, -_TOUCH)    # a box tangent to g is clear
        if any(t.intersects(o) for o in self.boxes) or any(t.intersects(o) for o in self.dots):
            return False
        pieces = list(zip(leader, leader[1:])) if leader is not None else []
        for a, b in pieces + list(strokes):
            if any(_seg_hits(a.x(), a.y(), b.x(), b.y(), o) for o in self.boxes):
                return False
        if leader is not None and strokes and not self.head_clear(leader, strokes, strict):
            return False
        if self._arr is None:
            self._arr = _seg_array(self._segs)
        if _crosses(self._arr, g):
            return False
        if over_guides:
            return True
        if self._guide_arr is None:
            self._guide_arr = _seg_array([(a.x(), a.y(), b.x(), b.y())
                                          for a, b, _ in self.guides])
        return not _crosses(self._guide_arr, g)

    def head_bounds(self, strokes: Sequence[tuple[QPointF, QPointF]]) -> QRectF:
        """The bounds of an arrowhead's `strokes`, grown by half of `pad`."""
        xs = [q.x() for s in strokes for q in s]
        ys = [q.y() for s in strokes for q in s]
        g = 0.5 * self.pad
        return QRectF(min(xs) - g, min(ys) - g, max(xs) - min(xs) + 2.0 * g,
                      max(ys) - min(ys) + 2.0 * g)

    def head_clear(self, leader: _Lead, strokes: Sequence[tuple[QPointF, QPointF]],
                   strict: int = 2) -> bool:
        """Whether a headed leader's head (`head_bounds`) meets no head placed before it and
        the leader crosses no such head (with a negative `strict`, True); with `strict` 1 or
        more, also whether the leader crosses no headed leader placed before it; with `strict`
        2, also whether the head meets no segment and the leader crosses none but a `soft`
        one."""
        if strict < 0:
            return True
        g = self.head_bounds(strokes)
        if any(g.intersects(o) for o in self.heads):
            return False
        pieces = list(zip(leader, leader[1:]))
        for a, b in pieces:
            if any(_seg_hits(a.x(), a.y(), b.x(), b.y(), o) for o in self.heads):
                return False
            if strict and any(_segs_cross(a, b, c, d) for c, d in self.leads):
                return False
        if strict < 2:
            return True
        if self._arr is None:
            self._arr = _seg_array(self._segs)
        if _crosses(self._arr, g):
            return False
        if self._hard_arr is None:
            self._hard_arr = _seg_array(self._hard)
        segs = self._hard_arr
        for a, b in pieces:
            near = np.flatnonzero(
                (segs[4] <= max(a.x(), b.x())) & (segs[5] >= min(a.x(), b.x()))
                & (segs[6] <= max(a.y(), b.y())) & (segs[7] >= min(a.y(), b.y())))
            if any(_segs_cross(a, b, QPointF(x0, y0), QPointF(x1, y1))
                   for x0, y0, x1, y1 in segs[:4, near].T.tolist()):
                return False
        return True

    def add_head(self, leader: _Lead, strokes: Sequence[tuple[QPointF, QPointF]]) -> None:
        """Hold a headed leader drawn and its head, for `head_clear`."""
        if strokes:
            self.heads.append(self.head_bounds(strokes))
        self.leads += list(zip(leader, leader[1:]))

    def crosses(self, r: QRectF) -> bool:
        """Whether a segment other than a guide passes through `r`."""
        if self._arr is None:
            self._arr = _seg_array(self._segs)
        return _crosses(self._arr, r)

    def guide_pieces(self) -> list[tuple[QPointF, QPointF, QPen]]:
        """Each guide, broken where it passes behind a text box grown by `pad`."""
        out = []
        for a, b, pen in self.guides:
            d = b - a
            cuts = sorted(c for c in (_inside(a, d, o.adjusted(-self.pad, -self.pad, self.pad,
                                                                self.pad))
                                      for o in self.boxes) if c is not None)
            t = 0.0
            for t0, t1 in cuts + [(1.0, 1.0)]:
                if t0 > t + _EPS:
                    out.append((a + d * t, a + d * t0, pen))
                t = max(t, t1)
        return out


def _inside(a: QPointF, d: QPointF, r: QRectF) -> Optional[tuple[float, float]]:
    """The stretch, as fractions from 0 to 1, of the segment from `a` along `d` that lies
    inside `r`, or None."""
    lo, hi = 0.0, 1.0
    for pk, qk in ((-d.x(), a.x() - r.left()), (d.x(), r.right() - a.x()),
                   (-d.y(), a.y() - r.top()), (d.y(), r.bottom() - a.y())):
        if abs(pk) < 1e-12:
            if qk <= 0:
                return None
            continue
        tk = qk / pk
        if pk < 0:
            lo = max(lo, tk)
        else:
            hi = min(hi, tk)
    return (lo, hi) if hi - lo > 1e-9 else None


class _Unseen:
    """A painter that draws nothing, for laying a diagram out unseen: it keeps the pen it
    is given and takes every other call."""

    def __init__(self) -> None:
        self._pen = QPen()

    def setPen(self, pen) -> None:     # noqa: N802 - QPainter's name
        self._pen = QPen(pen)

    def pen(self) -> QPen:
        return self._pen

    def __getattr__(self, name: str) -> Callable[..., Any]:
        return lambda *a, **k: None


def _slide(start: float, lo: float, hi: float, step: float, toward: int) -> list[float]:
    """Positions from `start` (brought within `lo`..`hi`) by `step` within `lo`..`hi`: toward
    +1 rightward first, −1 leftward first, then the other way; 0 alternately either side."""
    hi = max(hi, lo)
    start = min(max(start, lo), hi)
    out = [start]
    n = int((hi - lo) / step) + 1
    if toward:
        out += [start + toward * k * step for k in range(1, n + 1)]
        out += [start - toward * k * step for k in range(1, n + 1)]
    else:
        out += [start + d * k * step for k in range(1, n + 1) for d in (1, -1)]
    return [x for x in out if lo - _EPS <= x <= hi + _EPS]


def _under(x: float, y: float, w: float, h: float) -> _Spots:
    """One spot: a box of `w` × `h` centred on `x`, its top at `y`."""
    yield x - 0.5 * w, y, None


def _leader(r: QRectF, a: QPointF) -> tuple[QPointF, QPointF]:
    """A leader line from just off the box `r` to the point `a`."""
    q = QPointF(min(max(a.x(), r.left()), r.right()), min(max(a.y(), r.top()), r.bottom()))
    d = a - q
    n = math.hypot(d.x(), d.y())
    if n > 1.5:
        q += d * (1.5 / n)
    return q, a


def _bent(r: QRectF, a: QPointF, gap: float, run: float, lean: float = 0.0) -> Optional[_Lead]:
    """A headed leader from just off the box `r`, over or under the horizontal line through
    the point `a`, to `a`: straight to its knee, the point `gap + run` px off the line and
    `lean` px across from `a`, then straight toward `a`, ending `gap` px short of the line, so
    its last piece points at `a` and its tip lies `lean * gap / (gap + run)` px across from
    it; a box over the knee drops straight to it, a box beside it runs to it. None where a box
    over the knee comes within `gap + run` px of the line, or the box lies across the line."""
    s = 1.0 if r.center().y() > a.y() else -1.0
    edge = r.top() if s > 0 else r.bottom()
    if s * (edge - a.y()) <= 0.0:
        return None
    knee = QPointF(a.x() + lean, a.y() + s * (gap + run))
    tip = a + (knee - a) * (gap / (gap + run))
    if r.left() <= knee.x() <= r.right():
        if s * (edge - a.y()) < gap + run - 1e-6:
            return None
        start = QPointF(knee.x(), edge - s * 1.5)
        return (start, tip) if lean == 0.0 else (start, knee, tip)
    return _leader(r, knee)[0], knee, tip


def _segs_cross(a: QPointF, b: QPointF, c: QPointF, d: QPointF) -> bool:
    """Whether the segments from `a` to `b` and from `c` to `d` meet, ends included."""
    def side(p: QPointF, q: QPointF, r: QPointF) -> float:
        return (q.x() - p.x()) * (r.y() - p.y()) - (q.y() - p.y()) * (r.x() - p.x())

    def within(p: QPointF, q: QPointF, r: QPointF) -> bool:
        return (min(p.x(), q.x()) - _EPS <= r.x() <= max(p.x(), q.x()) + _EPS
                and min(p.y(), q.y()) - _EPS <= r.y() <= max(p.y(), q.y()) + _EPS)

    d1, d2 = side(c, d, a), side(c, d, b)
    d3, d4 = side(a, b, c), side(a, b, d)
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and \
            ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)):
        return True
    return any(abs(s) <= _EPS and within(p, q, r) for s, p, q, r in
               ((d1, c, d, a), (d2, c, d, b), (d3, a, b, c), (d4, a, b, d)))


class TimingDiagram(QWidget):
    """Paints the clicked term, one `Panel` per column and occurrence; see the module
    docstring."""

    doubleClicked = Signal()
    exportableChanged = Signal(bool)     # `can_export`, emitted where it changes

    def __init__(self, subscript: Callable[[str], str], term_key: Callable[[str], str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sub = subscript
        self._key = term_key
        self.leg_title = ""
        self.term = ""
        self.panels: list[Panel] = []
        self.side_by_side = False
        self.shown_column: Optional[int] = None    # the one section drawn; None for all
        self.pending = False             # a recompute runs: the panels are not drawn
        self.reduced = False             # not maximized: drawn at `REDUCED_SCALE` (`set_reduced`)
        self._exportable = False         # `can_export`, as `exportableChanged` last gave it
        self._frame = (0.0, 1.0)         # the column being laid out: its x and width
        self._corner: QWidget | None = None
        self._ink = _Ink(QRectF(), 0.0)  # the panel being painted
        self._pending: list[Callable[[], object]] = []  # its text, placed after its lines
        self._misses = 0                 # labels placed with no free spot, this paint
        self._strays = 0                 # and those placed with a leader line
        self._one_line: dict[tuple[str, str], QStaticText] = {}
        self._lh_px: Optional[float] = None          # `_lh`
        # Each panel layout's placed labels, by `_paint_panel`'s key: the panel, and per
        # `_put` in order its text, its font's key and the spot it took (`_put`).
        self._spots: dict[tuple, tuple[Panel, list[_Placed]]] = {}
        self._replay: Optional[Iterator[_Placed]] = None    # the panel painted: replayed
        self._record: Optional[list[_Placed]] = None        # or recorded
        self._floor: Optional[tuple[tuple[int, float], float]] = None   # (width, lh), row
        # A width change keeps the last floor until `_FLOOR_DELAY_MS` pass with no other.
        self._floor_timer = QTimer(self)
        self._floor_timer.setSingleShot(True)
        self._floor_timer.setInterval(_FLOOR_DELAY_MS)
        self._floor_timer.timeout.connect(self._refloor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # --- state ------------------------------------------------------------------------
    def show_term(self, title: str, key: str, sections: list[DiagramSection]) -> None:
        """Draw the term keyed `key` (the term tree's highlight key) from each section."""
        self.leg_title = title
        self.term = key
        new = panels(sections, key, self._key) if key else []
        same = new == self.panels
        if not same:                     # equal panels keep their placed labels
            self.panels = new
            self._spots.clear()
        self._relayout(keep_floor=same)
        self._emit_exportable()

    def set_side_by_side(self, on: bool, reduced: Optional[bool] = None) -> None:
        """Lay the columns out side by side, or stacked; with `reduced` given, `set_reduced`
        to it in the same layout."""
        self.side_by_side = bool(on)
        if reduced is not None and bool(reduced) != self.reduced:
            self.reduced = bool(reduced)
            self._forget_layout()
        self._relayout()

    def set_shown_column(self, column: Optional[int]) -> None:
        """Draw only the section at index `column`, or every section with None."""
        if column != self.shown_column:
            self.shown_column = column
            self._relayout()

    def set_pending(self, on: bool) -> None:
        """While `on`, draw "Computing…" in place of the panels, keeping them and the
        diagram's size; off, draw the panels again."""
        if bool(on) != self.pending:
            self.pending = bool(on)
            self.update()
            self._emit_exportable()

    def clear(self) -> None:
        self.show_term("", "", [])

    def set_reduced(self, on: bool) -> None:
        """Draw the body font, and with it every font, weight and size, at `REDUCED_SCALE` of
        its size while `on` (the diagram is not maximized); off, at its size."""
        if bool(on) != self.reduced:
            self.reduced = bool(on)
            self.restyle()

    def restyle(self) -> None:
        """Lay the diagram out again from the `diagram_style` settings in force, after they
        change."""
        self._forget_layout()
        self._relayout()
        self.place_corner()

    def _forget_layout(self) -> None:
        """Drop what was measured and placed with the settings or the scale in force."""
        self._lh_px = None
        self._spots.clear()

    def units(self) -> tuple[float, int]:
        """The widget font's line height in px, measured as the diagram draws it (`_design`),
        and its size in px (0 where it is set in points): what a weight of one line height and
        a font size of one body font draw at full scale."""
        return QFontMetricsF(_design(self.font())).height(), self.font().pixelSize()

    def set_corner_widget(self, w: QWidget) -> None:
        """Put `w` at the widget's top right."""
        w.setParent(self)
        self._corner = w
        self.place_corner()
        w.show()

    def place_corner(self) -> None:
        """Put the corner widget at the top right at its current size: called again when the
        widget's contents change size. The title wraps beside it, so a change of its width
        lays the diagram out again."""
        if self._corner is not None:
            was = self._corner.width()
            self._corner.adjustSize()
            self._corner.move(self.width() - self._corner.width() - 4, 2)
            if self._corner.width() != was:
                self.setMinimumHeight(self._min_height())
                self.update()

    def mouseDoubleClickEvent(self, event) -> None:     # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            event.accept()
            self.doubleClicked.emit()
            return
        super().mouseDoubleClickEvent(event)

    def _relayout(self, keep_floor: bool = False) -> None:
        """Lay the diagram out again: `_row_floor` anew, or with `keep_floor` (the panels are
        unchanged) the floor found at the current width."""
        self._floor_timer.stop()
        if not keep_floor:
            self._floor = None
        self.setMinimumHeight(self._min_height())
        self.updateGeometry()
        self.update()

    def _refloor(self) -> None:
        """`_row_floor` at the current width, once the width has settled."""
        self.setMinimumHeight(self._min_height())
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:     # noqa: N802 - Qt override
        # The text wraps at the width, so the height it needs follows it. On a shown diagram
        # the floor is found once the width settles (`_FLOOR_DELAY_MS`), the last one kept
        # until then; a hidden one finds it at once.
        super().resizeEvent(event)
        self.place_corner()
        if event.size().width() != event.oldSize().width():
            if self._floor is None or not self.isVisible():
                self._floor_timer.stop()
                self.setMinimumHeight(self._min_height())
            else:
                self._floor_timer.start()

    # --- geometry ---------------------------------------------------------------------
    def style_scale(self) -> float:
        """The scale every font, weight and size is drawn at: `REDUCED_SCALE` while `reduced`,
        else 1."""
        return REDUCED_SCALE if self.reduced else 1.0

    def _body_font(self) -> QFont:
        """The widget's font at `style_scale`: the font the line height is measured on."""
        f = _design(self.font())
        k = self.style_scale()
        if k != 1.0:
            if f.pixelSize() > 0:
                f.setPixelSize(max(round(f.pixelSize() * k), 1))
            else:
                f.setPointSizeF(max(f.pointSizeF() * k, 1.0))
        return f

    def _lh(self) -> float:
        """The body font's line height, in px, kept until a `_LAYOUT_EVENTS` event or a
        `restyle`."""
        if self._lh_px is None:
            self._lh_px = QFontMetricsF(self._body_font()).height()
        return self._lh_px

    def _pad(self) -> float:
        """The clearance kept around a text box, in line heights: `_PAD`, and at least half
        the widest pen."""
        return max(_PAD, 0.5 * ds.widest_weight())

    def changeEvent(self, event) -> None:     # noqa: N802 - Qt override
        if event.type() in _LAYOUT_EVENTS:
            self._lh_px = None
            self._spots.clear()
        super().changeEvent(event)

    def _font(self, key: str, k: float = 1.0) -> QFont:
        """The `diagram_style` text class `key`'s font: its family (the widget's where none
        is set), weight, and size times `k` and `style_scale`, in pixels where the widget's
        font is set in pixels, else in points."""
        family, size, weight = ds.font(key)
        f = _design(self.font())
        if family:
            f.setFamily(family)
        k = size * k * self.style_scale()
        if f.pixelSize() > 0:
            f.setPixelSize(max(round(f.pixelSize() * k), 8))
        else:
            f.setPointSizeF(max(f.pointSizeF() * k, 7.0))
        f.setWeight(_FONT_WEIGHT[weight])
        return f

    def _small(self) -> QFont:
        """The corner note's and the footnote's font."""
        return self._font("note")

    def _text_font(self, key: str) -> QFont:
        """A label's font, of text class `key` ("row", "span", "level" or "axis"): at its
        size in a column of at least `_LABEL_FULL_W` line heights, and `label.narrow` of it
        in a narrower one."""
        full = self._frame[1] >= _LABEL_FULL_W * self._lh()
        return self._font(key, 1.0 if full else float(ds.value("label.narrow")))

    def _label_font(self) -> QFont:
        """A level's or a point's label's font (`_text_font`)."""
        return self._text_font("level")

    def _head_font(self) -> QFont:
        """A panel heading's font."""
        return self._font("heading")

    def _pen(self, colour: QColor, width: float, style: Qt.PenStyle = Qt.SolidLine) -> QPen:
        """A pen `width` line heights wide: round-capped where solid, flat-capped where
        dashed or dotted, so a pattern keeps its gaps."""
        pen = QPen(colour, width * self._lh(), style)
        pen.setCapStyle(Qt.RoundCap if style == Qt.SolidLine else Qt.FlatCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _line(self, key: str, colour: Optional[QColor] = None, dashed: bool = True) -> QPen:
        """Line class `key`'s pen: its colour (or `colour`), its weight and, with `dashed`
        where it has one, its dash style."""
        style = _DASH[ds.dash(key)] if dashed and f"{key}.dash" in ds.VALUES else Qt.SolidLine
        return self._pen(_colour(key) if colour is None else colour, ds.weight(key), style)

    def _size(self, key: str) -> float:
        """A `diagram_style` length (an arrowhead, the dot's radius, a tick), in px."""
        return float(ds.value(key)) * self._lh()

    def columns(self) -> list[list[Panel]]:
        """The panels in drawing columns: one per section side by side, else one; only the
        `shown_column`'s panels where one is chosen."""
        shown = [pn for pn in self.panels
                 if self.shown_column is None or pn.column == self.shown_column]
        if not self.side_by_side:
            return [shown] if shown else []
        cols: dict[int, list[Panel]] = {}
        for pn in shown:
            cols.setdefault(pn.column, []).append(pn)
        return [cols[k] for k in sorted(cols)]

    def _frames(self) -> list[tuple[float, float]]:
        n = max(len(self.columns()), 1)
        w = self.width() / n
        return [(i * w, w) for i in range(n)]

    def _plot_x(self) -> tuple[float, float]:
        lh = self._lh()
        x0, w = self._frame
        return x0 + 0.6 * lh, x0 + w - 0.6 * lh

    def x_of(self, panel: Panel, t_ns: float) -> float:
        """The x of `t_ns` in `panel`: one scale for the whole panel."""
        lo, hi = panel.window
        left, right = self._plot_x()
        return left + (t_ns - lo) / (hi - lo) * (right - left)

    def _heading(self, pn: Panel) -> str:
        head = f"<b>{pn.title}</b>"
        if pn.timeline is not None:
            durations = [pn.timeline.duration(sp) for sp in pn.spans]
            lead = next((d for d in durations if d != 0.0), 0.0)
            parts = "".join(
                ("" if i == 0 else " + " if d * lead >= 0 else " − ") + self._sub(s)
                for i, (s, d) in enumerate(zip(pn.symbols, durations)))
            # The total is read along the first non-zero span's direction, so it equals the
            # spans' unsigned lengths combined with the operators shown, and is negative
            # where the spans against that direction are longer.
            total = sum(durations) * (-1.0 if lead < 0 else 1.0)
            sign = "\u2212" if round(total, 2) < 0 else ""
            head += f"&nbsp;&nbsp; {parts} = {sign}{abs(total):.2f} ns"
        return head

    def _static(self, html: str, font: QFont, width: float) -> QStaticText:
        """`html` wrapped at `width`, at a word boundary and within a word longer than it, or
        on one line where `width` is negative (kept for reuse, per font)."""
        key = (html, font.key()) if width < 0 else None
        if key is not None and key in self._one_line:
            return self._one_line[key]
        st = QStaticText(html)
        st.setTextFormat(Qt.RichText)
        st.setTextWidth(width)
        opt = QTextOption(Qt.AlignLeft)
        opt.setWrapMode(QTextOption.NoWrap if width < 0
                        else QTextOption.WrapAtWordBoundaryOrAnywhere)
        st.setTextOption(opt)
        st.prepare(font=font)
        if key is not None:
            if len(self._one_line) > 512:
                self._one_line.clear()
            self._one_line[key] = st
        return st

    def _text_w(self) -> float:
        return max(self._frame[1] - 1.2 * self._lh(), 10.0)

    def _title_font(self) -> QFont:
        return self._font("title")

    def _title_full_w(self) -> float:
        """The width the leg's title wraps at with nothing beside it: the widget's, less the
        title's indent."""
        return max(self.width() - 0.6 * self._lh(), 10.0)

    def _title_beside_w(self) -> float:
        """The width the leg's title wraps at beside the corner widget, with a gap."""
        corner = self._corner.width() + 8 if self._corner is not None else 0
        return max(self._title_full_w() - corner, 10.0)

    def _title_below(self) -> bool:
        """The title goes below the corner widget: beside it, it would wrap onto more lines
        than at the full width."""
        if self._corner is None:
            return False
        html, font = self._html(self.leg_title), self._title_font()
        return (self._static(html, font, self._title_beside_w()).size().height()
                > self._static(html, font, self._title_full_w()).size().height())

    def _title_w(self) -> float:
        """The width the leg's title wraps at: beside the corner widget, or the full width
        below it (`_title_below`)."""
        return self._title_full_w() if self._title_below() else self._title_beside_w()

    def _title_drop(self) -> float:
        """How far below its own line the title starts: the corner widget's bottom where the
        title goes below it, else 0."""
        if self._corner is not None and self._title_below():
            return float(self._corner.y() + self._corner.height())
        return 0.0

    def _title_top(self) -> float:
        """The top of the title's first line (`_title_drop` under the top line)."""
        return 0.2 * self._lh() + self._title_drop()

    def _title_text(self) -> QStaticText:
        """The leg's title, its symbols subscripted (`_html`), wrapped at `_title_w`."""
        return self._static(self._html(self.leg_title), self._title_font(), self._title_w())

    def _title_h(self) -> float:
        """The height of the band the title takes: `_TITLE_H` lines, the height of each line
        the title wraps onto beyond its first, and the corner widget's where the title goes
        below it."""
        one = self._static("X", self._title_font(), -1.0).size().height()
        return (_TITLE_H * self._lh() + max(0.0, self._title_text().size().height() - one)
                + self._title_drop())

    def _paint_title(self, p: QPainter) -> None:
        lh = self._lh()
        p.setFont(self._title_font())
        p.setPen(_colour("text"))
        p.drawStaticText(QPointF(0.6 * lh, self._title_top()), self._title_text())

    def _head_height(self, pn: Panel) -> float:
        """The heading and the corner (or the note), wrapped at the column's width."""
        lh = self._lh()
        h = self._static(self._heading(pn), self._head_font(),
                         self._text_w()).size().height()
        note = pn.note or pn.corner
        if note:
            h += 0.15 * lh + self._static(note, self._small(), self._text_w()).size().height()
        return h + 0.6 * lh

    def _label_h(self) -> float:
        """A label's height, in px, subscripts included: the tallest of the row-name, span
        and level label fonts."""
        return max(self._static(self._html("V_DD"), self._text_font(k), -1.0).size().height()
                   for k in ("row", "span", "level"))

    def strip_pitch(self) -> float:
        """The height of one of the strip's label lines (`_strip`), in px: a label's height,
        and at least a line height."""
        return max(self._lh(), self._label_h())

    def _row_head(self) -> float:
        """The room, in px, a signal row keeps above its highest level: the two labels
        stacked over a rail at that level (the row's name over the rail's own, `_level_spots`),
        and at least 1.5 line heights."""
        lh, h = self._lh(), self._label_h()
        return max(1.5 * lh, _LABEL_GAP * lh + 2.0 * (h + self._pad() * lh) + 1.0)

    def _mark_lines(self, pn: Panel) -> float:
        """The height, in lines, of the strip above a panel's first row that carries its
        labelled points on no drawn ramp: none, or its label lines (`_strip`, each
        `strip_pitch` tall) and a margin clear of the first row's labels."""
        if pn.timeline is None or not pn.rows:
            return 0.0
        strip = self._strip(pn)
        if not strip:
            return 0.0
        lanes = float(max(lane + n for _, _, lane, n, _ in strip))
        return lanes * self.strip_pitch() / self._lh() + _MARK_MARGIN

    def _strip(self, pn: Panel) -> list[tuple[Event, float, int, int, float]]:
        """The labelled points on no drawn ramp, in time order, each with its label's left x,
        its first line in the strip above the first row (0 the top), the lines it takes and
        the width it is wrapped at (negative: one line). The n-th label's forms, each tried
        on line n mod 2, then the other, then a new line below, before the next: on one line
        to the right of its point and else to the left, on one line over its point's line,
        wrapped at the room beside its point, right and else left, and wrapped at the plot's
        width over its line. It takes the first spot where it lies in the plot and
        `_strip_clear` holds; with none, a line below every label before it, over its
        point's line. A deadline's line, unlabelled, runs from the strip's top."""
        if pn.timeline is None or not pn.rows:
            return []
        lh = self._lh()
        left, right = self._plot_x()
        small, gap, pad = self._label_font(), 0.2 * lh, self._pad() * lh
        pitch = self.strip_pitch()
        off = [e for e in _ends(pn) if not _on_drawn_ramp(pn, e)]
        marks = [e for e in off if e.kind != "deadline"]
        out: list[tuple[Event, float, int, int, float]] = []
        # First line, last line, x0, x1, its point's x; a deadline's line runs down from the
        # strip's top.
        placed: list[tuple[int, int, float, float, float]] = [
            (0, 0, x, x, x)
            for x in (self.x_of(pn, e.t_ns) for e in off if e.kind == "deadline")]
        for i, e in enumerate(marks):
            x = self.x_of(pn, e.t_ns)
            html = self._html(e.label)
            w = self._static(html, small, -1.0).size().width()

            def wrapped(room: float, lx_of: Callable[[float], float]) -> list:
                if room <= 2.0 * lh:
                    return []
                size = self._static(html, small, room).size()
                n = max(math.ceil(size.height() / pitch - _EPS), 1)
                return [(lx_of(size.width()), size.width(), n, room)]

            # The label's forms, each its left x, width, lines and wrap width, in stages: on
            # one line beside its point, over its line, wrapped beside it, wrapped over it.
            fits = w <= right - left + _EPS
            over = [(min(max(x + gap, left), right - w), w, 1, -1.0)] if fits else []
            stages = [
                [(lx, w, 1, -1.0) for lx in (x + gap, x - gap - w)
                 if left - _EPS <= lx and lx + w <= right + _EPS],
                over,
                wrapped(right - x - gap, lambda _w: x + gap)
                + wrapped(x - gap - left, lambda fw: x - gap - fw),
                [] if fits else wrapped(right - left, lambda _w: left)]
            lanes = [i % 2, 1 - i % 2] + list(range(2, len(marks) + 2))
            choice = next(((lx, lane, n, wrap, fw) for forms in stages for lane in lanes
                           for lx, fw, n, wrap in forms
                           if self._strip_clear(placed, lx, fw, lane, lane + n - 1, x, pad)),
                          None)
            if choice is None:              # below every label, over its line
                lx, fw, n, wrap = (over or stages[3] or [(left, w, 1, -1.0)])[0]
                choice = (lx, 1 + max(l1 for _, l1, _, _, _ in placed) if placed else 0,
                          n, wrap, fw)
            lx, lane, n, wrap, fw = choice
            out.append((e, lx, lane, n, wrap))
            placed.append((lane, lane + n - 1, lx, lx + fw, x))
        return out

    @staticmethod
    def _strip_clear(placed: list[tuple[int, int, float, float, float]], lx: float, w: float,
                     first: float, last: float, x: float, pad: float) -> bool:
        """Whether a strip label from `lx`, `w` wide on lines `first` to `last`, for the
        point at `x`, overlaps none of `placed` (each its first and last lines, its x0 and
        x1, and its point's x), no earlier point's line (running down from its label)
        crosses it, and its own line crosses no label on or below `first`."""
        return not (
            any(l0 <= last and l1 >= first and lx - pad < b and lx + w + pad > a
                for l0, l1, a, b, _ in placed)
            or any(l0 <= last and lx - pad < px < lx + w + pad for l0, _, _, _, px in placed)
            or any(l1 >= first and a - pad < x < b + pad for _, l1, a, b, _ in placed))

    def _body_height(self, pn: Panel, row_h: float) -> float:
        """A panel's height under its heading, its signal rows `row_h` each."""
        lh = self._lh()
        if pn.timeline is None:
            return 0.0
        if not pn.rows:
            return (_FLIGHT_LEAD + _FLIGHT_H * len(pn.guides)
                    + _DIM_H * max(len(pn.spans), 1) + 0.5) * lh
        return ((self._mark_lines(pn) + _DIM_H * _dim_lanes(pn) + _AXIS_H) * lh
                + len(pn.rows) * row_h)

    def _fixed_height(self, col: list[Panel]) -> float:
        """A column's height but its signal rows, at the current frame's width."""
        lh = self._lh()
        h = self._title_h() + _GAP_H * lh * max(len(col) - 1, 0) + 0.5 * lh
        for pn in col:
            h += self._head_height(pn) + self._body_height(pn, 0.0)
        return h

    def _each_column(self):
        """(frame, panels) per column, with the frame set for the column's measurements."""
        for frame, col in zip(self._frames(), self.columns()):
            self._frame = frame
            yield frame, col

    def _row_floor(self) -> float:
        """The least signal row height, in px, at the current width: the first of
        `_row_steps` lines at which every label of every panel finds a free spot with no
        leader line, found by laying the panels out unseen; where none does, the first with
        the fewest labels without a free spot, then the fewest with a leader. Where every
        step leaves a label without a free spot, rows `_ROW_GROW` times taller each, up to
        `_ROW_MAX` lines, until one leaves none. Kept until the width, the font or the panels
        change; after a width change the last floor is kept while `_floor_timer` runs."""
        lh = self._lh()
        key = (self.width(), lh)
        if self._floor is not None and (self._floor[0] == key or (
                self._floor_timer.isActive() and self._floor[0][1] == lh)):
            return self._floor[1]
        floor = self._row_steps()[0] * lh
        if any(pn.rows for pn in self.panels):
            best: Optional[tuple[int, int]] = None

            def tried(lines: float) -> None:
                nonlocal best, floor
                self._misses = self._strays = 0
                self._paint_columns(cast(QPainter, _Unseen()), lines * lh)
                if best is None or (self._misses, self._strays) < best:
                    best, floor = (self._misses, self._strays), lines * lh

            steps = self._row_steps()
            for lines in steps:
                tried(lines)
                if best == (0, 0):
                    break
            lines = steps[-1]
            while best is not None and best[0] and lines < _ROW_MAX:
                lines = min(lines * _ROW_GROW, _ROW_MAX)
                tried(lines)
        self._floor = (key, floor)
        return floor

    def _row_steps(self) -> tuple[float, ...]:
        """The row heights, in lines, `_row_floor` tries first: `_ROW_STEPS`."""
        return _ROW_STEPS

    def _row_h(self) -> float:
        """One signal row's height, the same in every column: the height the text leaves,
        shared among the rows, at least `_row_floor` and at most `_ROW_ASPECT` of the
        column's width."""
        floor = self._row_floor()
        spare = []
        for _, col in self._each_column():
            n = sum(len(pn.rows) for pn in col)
            if n:
                spare.append((self.height() - self._fixed_height(col)) / n)
        if not spare:
            return floor
        cap = max(_ROW_ASPECT * self._frames()[0][1], floor)
        return min(max(min(spare), floor), cap)

    def _min_height(self) -> int:
        lh = self._lh()
        if not self.panels:
            return int(3 * lh)
        floor = self._row_floor()
        return int(max(self._fixed_height(col) + sum(len(pn.rows) for pn in col) * floor
                       for _, col in self._each_column()))

    def sizeHint(self) -> QSize:          # noqa: N802 - Qt override
        return QSize(int(30 * self._lh()), self._min_height())

    # --- export -------------------------------------------------------------------------
    def can_export(self) -> bool:
        """Whether `export` has a diagram to write: a term is drawn and no recompute runs."""
        return bool(self.panels) and not self.pending

    def _emit_exportable(self) -> None:
        """Emit `exportableChanged` where `can_export` differs from when it last did."""
        now = self.can_export()
        if now != self._exportable:
            self._exportable = now
            self.exportableChanged.emit(now)

    def export(self, fmt: str, scale: float = 2.0, light: bool = False) -> bytes:
        """The diagram as `paint_diagram` draws it at the widget's current size, as the bytes
        of a `fmt` file: "png" at `scale` times the size, "svg" or "pdf" (one page) as vector
        drawings, one SVG unit per widget pixel and a PDF page of the widget's size at its
        DPI. In the current palette, or the light palette with `light`. The corner widget is
        not drawn. Every device takes the widget's logical DPI, so the text is laid out as on
        screen. Raises ValueError where `can_export` does not hold or `fmt` or `scale` is not
        one of these, and OSError where the file cannot be made."""
        if fmt not in EXPORT_FORMATS:
            raise ValueError(f"unknown export format {fmt!r}")
        if not self.can_export():
            raise ValueError("no diagram is drawn")
        if not (isinstance(scale, (int, float)) and 0.0 < scale <= _EXPORT_SCALE_MAX):
            raise ValueError(f"the scale must be above 0 and at most {_EXPORT_SCALE_MAX:g}")
        with palette_applied("light" if light else VizTheme.MODE):
            return {"png": self._export_png, "svg": self._export_svg,
                    "pdf": self._export_pdf}[fmt](scale)

    def _export_png(self, scale: float) -> bytes:
        w, h = math.ceil(self.width() * scale), math.ceil(self.height() * scale)
        if w * h * 4 > _EXPORT_PNG_BYTES:
            raise OSError(f"the diagram is {w} x {h} pixels at {scale:g}x, too large for a "
                          "PNG; export it at a smaller scale, as SVG or as PDF")
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        if img.isNull():
            raise OSError(f"not enough memory for a {w} x {h} pixel PNG")
        img.setDevicePixelRatio(scale)
        img.setDotsPerMeterX(round(self.logicalDpiX() / 0.0254))
        img.setDotsPerMeterY(round(self.logicalDpiY() / 0.0254))
        img.fill(QColor(VizTheme.FRAME_BG))
        p = QPainter(img)
        try:
            self.paint_diagram(p)
        finally:
            p.end()
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not img.save(buf, "PNG"):
            raise OSError("the PNG could not be encoded")
        return bytes(buf.data())

    def _export_svg(self, _scale: float) -> bytes:
        from PySide6.QtSvg import QSvgGenerator
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        gen = QSvgGenerator()
        gen.setOutputDevice(buf)
        gen.setSize(QSize(self.width(), self.height()))
        gen.setViewBox(QRectF(0, 0, self.width(), self.height()))
        gen.setResolution(self.logicalDpiY())
        gen.setTitle(self.leg_title)
        self._paint_onto(gen)
        data = bytes(buf.data())
        if not data:
            raise OSError("the SVG could not be drawn")
        # Each text names its font, and a generic family after it for a viewer without it.
        return _SVG_FAMILY.sub(lambda m: b'font-family="&apos;' + m[1] + b'&apos;, sans-serif"',
                               data)

    def _export_pdf(self, _scale: float) -> bytes:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pdf = QPdfWriter(buf)
        dpi = self.logicalDpiY()
        pdf.setResolution(dpi)
        pdf.setPageMargins(QMarginsF(0, 0, 0, 0))
        pdf.setPageSize(QPageSize(QSizeF(self.width() * 72.0 / dpi, self.height() * 72.0 / dpi),
                                  QPageSize.Point, "", QPageSize.ExactMatch))
        pdf.setTitle(self.leg_title)
        self._paint_onto(pdf)
        data = bytes(buf.data())
        if not data:
            raise OSError("the PDF could not be drawn")
        return data

    def _paint_onto(self, device) -> None:
        """`paint_diagram` onto a vector `device`."""
        p = QPainter(device)
        if not p.isActive():
            raise OSError("the file could not be drawn")
        try:
            self.paint_diagram(p)
        finally:
            p.end()

    # --- painting -----------------------------------------------------------------------
    def paintEvent(self, _event) -> None:     # noqa: N802 - Qt override
        self.paint_diagram(QPainter(self))

    def paint_diagram(self, p: QPainter) -> None:
        """Paint the diagram onto `p`'s device, laid out at the widget's size and font: the
        widget's own paint, and `export`'s onto an image or a vector file."""
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(QRectF(0, 0, self.width(), self.height()), QColor(VizTheme.FRAME_BG))
        lh = self._lh()
        if not self.panels:
            p.setFont(self._body_font())
            p.setPen(_colour("dim_text"))
            p.drawText(QRectF(0, 0, self.width(), 3 * lh), Qt.AlignCenter | Qt.TextWordWrap,
                       "Click a term to draw it here, to scale.")
            return
        self._paint_title(p)
        if self.pending:
            p.setFont(self._body_font())
            p.setPen(_colour("dim_text"))
            p.drawText(QRectF(0, self._title_h(), self.width(), 3 * lh),
                       Qt.AlignCenter | Qt.TextWordWrap, "Computing…")
            return
        self._paint_columns(p, self._row_h())

    def _paint_columns(self, p: QPainter, row_h: float) -> None:
        """Every column's panels, top to bottom, their signal rows `row_h` each."""
        lh = self._lh()
        for _, col in self._each_column():
            y = self._title_h()
            for pn in col:
                y = self._paint_panel(p, pn, y, row_h) + _GAP_H * lh

    def _html(self, text: str) -> str:
        """A plain label as rich text, its symbols subscripted."""
        return self._sub(escape(text, quote=False))

    def _rich(self, p: QPainter, x: float, y: float, html: str, font: QFont, col: QColor,
              centre: bool = False, width: float = -1.0) -> None:
        """`html` from `x` (centred on it with `centre`) at `y`: on one line, or wrapped at a
        `width` that is not negative."""
        st = self._static(html, font, width)
        p.setFont(font)
        p.setPen(col)
        p.drawStaticText(QPointF(x - (0.5 * st.size().width() if centre else 0.0), y), st)

    def _seg(self, p: QPainter, a: QPointF, b: QPointF, soft: bool = False) -> None:
        """A line from `a` to `b` in the painter's pen, kept clear of the panel's text, and
        unless `soft` (a rail or a level line) of the headed leaders (`_Ink.head_clear`)."""
        p.drawLine(a, b)
        self._ink.line(a, b, soft)

    def _put(self, p: QPainter, html: str, font: QFont, col: QColor,
             spots: Callable[[float, float], _Spots],
             need: bool = True, head: bool = False) -> Optional[QRectF]:
        """`html` on one line at the first of `spots(width, height)` whose box is free in the
        panel's ink, with its leader line where the spot has one, ending in an open arrowhead
        the size of a span's (`span.head`) with `head`. A headed leader runs to the point on a
        horizontal line its spot names, bent so that its last piece points at it, ending
        `_leader_gap` short of the line, straight over or under it where it can (`_bent`),
        its head clear of every head drawn and the leader crossing
        none, and where a spot of the stage allows it, the leader crossing no other leader, and
        where one allows that, the head also clear of every line and the leader crossing no
        line but a guide or a soft one (`_Ink.head_clear`); a spot too near the line for that
        takes no headed leader. Only a label with no such spot clear or over the guides lets
        its head meet another's. The spots are tried in stages, each
        first clear of the guides and then over them, or where `_over_guides` does not hold
        every stage clear of them and then every stage over them: the spots with no leader,
        then those with one, the sequence's None marking a further stage's start; a guide a
        label is placed over is broken behind it. With no free spot the
        label is drawn at the first spot where `need`, else not at all. Returns the box drawn.
        A replayed layout (`_paint_panel`) gives the label the spot it took before, where its
        text and font are the same, and else searches from this label on."""
        st = self._static(html, font, -1.0)
        placed = self._replayed(html, font)
        if placed is not None:
            chosen, missed = placed
        else:
            chosen, missed = self._search(st, spots, need, head)
            if self._record is not None:
                self._record.append((html, font.key(), chosen, missed))
        if chosen is None:
            return None
        self._misses += missed
        r, lead = chosen
        if lead is not None:
            self._strays += 1
            p.setPen(self._line("leader", col))
            for a, b in zip(lead, lead[1:]):
                self._seg(p, a, b)
            if head:
                self._arrowhead(p, *lead[-2:])
                self._ink.add_head(lead, self._head_strokes(*lead[-2:]))
        self._ink.boxes.append(r)
        self._rich(p, r.left(), r.top(), html, font, col)
        return r

    def _replayed(self, html: str, font: QFont) -> Optional[tuple[_Chosen, bool]]:
        """The next label's spot and miss from the replayed layout, where its text and font
        are this label's; else None, and the panel's remaining labels are searched for."""
        if self._replay is None:
            return None
        got = next(self._replay, None)
        if got is not None and got[0] == html and got[1] == font.key():
            return got[2], got[3]
        self._replay = None
        return None

    def _search(self, st: QStaticText, spots: Callable[[float, float], _Spots],
                need: bool, head: bool = False) -> tuple[_Chosen, bool]:
        """`_put`'s spot for a label laid out as `st`: its box and leader line (None: not
        drawn), and whether no spot was free. With `head`, a leader is bent to end short of its
        line (`_bent`) and its arrowhead (`_head_strokes`) crosses no box placed before it, as
        the leader itself does not; a stage's headed spots are tried clear of the lines
        (`_Ink.head_clear`), then, before the stage ends, over them, and then over the other
        leaders."""
        w, h = st.size().width(), st.size().height()
        gap, run = self._leader_gap(), 2.0 * self._size("span.head")
        chosen: Optional[tuple[QRectF, Optional[_Lead]]] = None
        first: Optional[QRectF] = None
        tried: list[tuple[QRectF, Optional[_Lead]]] = []
        staged = self._over_guides()
        held: list[tuple[QRectF, Optional[_Lead]]] = []   # every stage's

        loose: list[tuple[QRectF, QPointF]] = []   # the stage's headed spots, for `over_lines`
        # The knee's offsets tried after a straight drop: a head's width and two, each way.
        leans = [k * 1.2 * self._size("span.head") + math.copysign(self._ink.pad, k)
                 for k in (-1, 1, -2, 2)]

        def free(r: QRectF, lead: Optional[_Lead], over: bool = False,
                 strict: int = 2) -> bool:
            strokes = self._head_strokes(*lead[-2:]) if head and lead is not None else []
            return self._ink.free(r, lead, over_guides=over, strokes=strokes, strict=strict)

        def over_lines() -> Optional[tuple[QRectF, Optional[_Lead]]]:
            found = next(((r, lead) for strict in (1, 0) for lean in (0.0, *leans)
                          for r, a in loose
                          if (lead := _bent(r, a, gap, run, lean)) is not None
                          and free(r, lead, strict=strict)), None)
            loose.clear()
            return found

        def over_guides() -> Optional[tuple[QRectF, Optional[_Lead]]]:
            found = None
            if staged:
                found = next(((r, lead) for r, lead in tried if free(r, lead, True)), None)
            else:
                held.extend(tried)
            tried.clear()
            return found

        for spot in spots(w, h):
            if spot is None:                 # a stage ends: its spots, now over the guides
                chosen = over_lines()
                if chosen is None:
                    chosen = over_guides()
                if chosen is not None:
                    break
                continue
            x, y, anchor = spot
            r = QRectF(x, y, w, h)
            first = first or r
            lead: Optional[_Lead] = None
            if anchor is not None and head:
                lead = _bent(r, anchor, gap, run)
                if lead is None:             # too near its line for a straight drop
                    loose.append((r, anchor))
                    continue
            elif anchor is not None:
                lead = _leader(r, anchor)
            if lead is not None and tried and tried[-1][1] is None:
                chosen = over_guides()       # the spots with no leader, now over the guides
                if chosen is not None:
                    break
            if free(r, lead):
                chosen = (r, lead)
                break
            tried.append((r, lead))
            if head and lead is not None:
                loose.append((r, anchor))
        if chosen is None:
            chosen = over_lines()
        if chosen is None:
            chosen = over_guides()
        if chosen is None and not staged:   # no spot of any stage clear: now over the guides
            chosen = next(((r, lead) for strict in (0, -1) for r, lead in held
                           if free(r, lead, True, strict)), None)
        if chosen is None:
            if not need or first is None:
                return None, False
            return (first, None), True
        return chosen, False

    def _over_guides(self) -> bool:
        """Whether a label no spot of a stage frees clear of the guides may take one of that
        stage over them (`_put`), else only once no spot of any stage is free clear of them:
        in the term diagram, the first."""
        return True

    def _leader_gap(self) -> float:
        """How far short of the line it names a headed leader ends, in px: `_LEADER_GAP` line
        heights, and at least a span's arrowhead (`span.head`)."""
        return max(_LEADER_GAP * self._lh(), self._size("span.head"))

    def _arrowhead(self, p: QPainter, a: QPointF, b: QPointF) -> None:
        """An open arrowhead at `b` on the line from `a`, in the painter's pen
        (`_head_strokes`)."""
        for s in self._head_strokes(a, b):
            self._seg(p, *s)

    def _head_strokes(self, a: QPointF, b: QPointF) -> list[tuple[QPointF, QPointF]]:
        """The two strokes of an open arrowhead at `b` on the line from `a`: each from `b`,
        `span.head` long, at most half the line's length."""
        d = b - a
        n = math.hypot(d.x(), d.y())
        if n < 1e-6:
            return []
        size = min(self._size("span.head"), 0.5 * n)
        ux, uy = d.x() / n, d.y() / n
        return [(b, QPointF(b.x() - size * ux - s * 0.6 * size * uy,
                            b.y() - size * uy + s * 0.6 * size * ux)) for s in (1.0, -1.0)]

    def _guide(self, p: QPainter, a: QPointF, b: QPointF) -> None:
        """A guide from `a` to `b` in the painter's pen, drawn by `_draw_guides` once the
        panel's text is placed, broken behind any label placed over it."""
        self._ink.guide(a, b, p.pen())

    def _draw_guides(self, p: QPainter) -> None:
        for a, b, pen in self._ink.guide_pieces():
            p.setPen(pen)
            p.drawLine(a, b)

    def _grid(self, ax: float, ay: float, w: float, h: float) -> _Spots:
        """Boxes of `w` × `h` on a grid of `_STEP` lines around (`ax`, `ay`), each with a
        leader line to the point: those within `_GRID_NEAR` of it, then, as a further stage
        of `_put`, the rest of the panel's plot, each set nearest first (then the nearer row,
        the upper, the nearer column, the left). The order is read from each box's offset
        from the point alone, so it is the same wherever the point sits, and a box and its
        mirror across the point lie at the same distance. A box outside the plot is not
        yielded, as it is never free."""
        lh = self._lh()
        s = _STEP * lh
        pl, e = self._ink.plot, 0.02
        ni, nj = _GRID_NEAR
        i_lo = math.ceil((pl.left() - e - ax + 0.5 * w) / s)
        i_hi = math.floor((pl.right() + e - ax - 0.5 * w) / s)
        j_lo = math.ceil((pl.top() - e - ay + 0.5 * h) / s)
        j_hi = math.floor((pl.bottom() + e - ay - 0.5 * h) / s)
        near: list[tuple[float, int, int, int, int, float, float]] = []
        far: list[tuple[float, int, int, int, int, float, float]] = []
        for i in range(i_lo, i_hi + 1):
            for j in range(j_lo, j_hi + 1):
                cx, cy = i * s, j * s
                d = math.hypot(max(abs(cx) - 0.5 * w, 0.0), max(abs(cy) - 0.5 * h, 0.0))
                if d >= 0.3 * lh:
                    (near if abs(i) <= ni and abs(j) <= nj else far).append(
                        (d, abs(j), j, abs(i), i, ax + cx - 0.5 * w, ay + cy - 0.5 * h))
        a = QPointF(ax, ay)
        near.sort()
        for c in near:
            yield c[5], c[6], a
        yield None
        far.sort()
        for c in far:
            yield c[5], c[6], a

    def _level_spots(self, lab: _Label, w: float, h: float) -> _Spots:
        """A level's label's spots, a box `w` × `h`, in the module docstring's order; a
        label at "point" takes `_point_spots`."""
        lh = self._lh()
        left, right = self._plot_x()
        if lab.at == "point":
            yield from self._point_spots(lab, w, h)
            return
        x0 = left if lab.x0 is None else max(lab.x0, left)
        x1 = right if lab.x1 is None else min(lab.x1, right)
        lo, hi = x0 + 0.3 * lh, x1 - 0.3 * lh - w
        start = {"left": lo, "centre": 0.5 * (x0 + x1 - w), "right": hi}[lab.at]
        xs = _slide(start, lo, hi, _STEP * lh, {"left": 1, "centre": 0, "right": -1}[lab.at])
        gap, pitch = _LABEL_GAP * lh, h + 2.0 * self._pad() * lh + 1.0  # a label further off

        def top(above: bool, j: int) -> float:
            return lab.y - gap - h - j * pitch if above else lab.y + gap + j * pitch

        def clear_to_line(x: float, above: bool, j: int) -> bool:
            """No line but a guide between the line and a spot `j` labels off it."""
            if not j:
                return True
            near, far = top(above, 0), top(above, j)
            band = (QRectF(x, far + h, w, near - far) if above
                    else QRectF(x, near, w, far - near))
            return not self._ink.crosses(band)

        sides = (lab.above, not lab.above)
        for above in sides:
            for j in (lab.off, 1 - lab.off):
                for x in xs:
                    if clear_to_line(x, above, j):
                        yield x, top(above, j), None
        for j in (2, 3, 4):
            for above in sides:
                for x in xs:
                    yield x, top(above, j), QPointF(min(max(x + 0.5 * w, x0), x1), lab.y)
        yield from self._grid(0.5 * (x0 + x1) if lab.at == "centre" else
                              x0 if lab.at == "left" else x1, lab.y, w, h)

    def _point_spots(self, lab: _Label, w: float, h: float) -> _Spots:
        """A point's label: beside it to the right, then the left, then centred, above it by
        preference and then under it; then each of those but centred moved up to four
        `_STEP`s further out; then the grid."""
        lh = self._lh()
        x = lab.x0 if lab.x0 is not None else 0.0
        g = 0.3 * lh
        ys = [lab.y - 0.15 * lh - h, lab.y + 0.15 * lh]
        if not lab.above:
            ys.reverse()
        for k in range(5):
            out = k * _STEP * lh
            for y in ys:
                for lx in ((x + g + out, x - g - w - out) if k else
                           (x + g, x - g - w, x - 0.5 * w)):
                    yield lx, y, None
        yield from self._grid(x, lab.y, w, h)

    def _dim_spots(self, x0: float, x1: float, y: float, w: float,
                   h: float) -> _Spots:
        """A dimension's label: centred under its line, slid along it, then over it, then
        under it as close as `_LABEL_GAP` allows, slid; then the grid."""
        lh = self._lh()
        left, right = self._plot_x()
        mid = 0.5 * (x0 + x1)
        xs = _slide(mid - 0.5 * w, left, right - w, _STEP * lh, 0)
        for x in xs:
            yield x, y + 0.3 * lh, None
        for x in xs:
            yield x, y - 0.3 * lh - h, None
        for x in xs:
            yield x, y + _LABEL_GAP * lh, None
        yield from self._grid(mid, y, w, h)

    def _flush(self, p: QPainter) -> None:
        """Place the text queued while the panel's lines were drawn, in order."""
        jobs, self._pending = self._pending, []
        for job in jobs:
            job()

    def _paint_panel(self, p: QPainter, pn: Panel, y0: float, row_h: float) -> float:
        """Paint `pn` from `y0` in the current frame; returns where it ends. A layout already
        painted at the same panel, `y0`, row height, frame, line height and font replays its
        labels' spots (`_spots`) in place of searching for them."""
        key = (id(pn), y0, row_h, self._frame, self._lh(), self._body_font().key())
        hit = self._spots.get(key)
        if hit is not None and hit[0] is pn:
            self._replay = iter(hit[1])
        else:
            self._record = []
        try:
            end = self._paint_panel_at(p, pn, y0, row_h)
            if self._record is not None:
                self._spots.pop(key, None)
                self._spots[key] = (pn, self._record)
                while len(self._spots) > _SPOTS_MAX:
                    del self._spots[next(iter(self._spots))]
        finally:
            self._replay = self._record = None
        return end

    def _paint_panel_at(self, p: QPainter, pn: Panel, y0: float, row_h: float) -> float:
        """`_paint_panel`'s drawing."""
        lh = self._lh()
        text, dim = _colour("text"), _colour("dim_text")
        small, span_font = self._label_font(), self._text_font("span")
        left, right = self._plot_x()
        end = y0 + self._head_height(pn) + self._body_height(pn, row_h)
        self._ink = ink = _Ink(QRectF(left, y0, right - left, end - y0), self._pad() * lh)
        self._pending = []
        st = self._static(self._heading(pn), self._head_font(), self._text_w())
        p.setFont(self._head_font())    # a static text draws in the painter's font
        p.setPen(text)
        p.drawStaticText(QPointF(left, y0), st)
        ink.boxes.append(QRectF(QPointF(left, y0), st.size()))
        note = pn.note or pn.corner
        if note:
            p.setFont(self._small())
            p.setPen(dim)
            nt = self._static(note, self._small(), self._text_w())
            at = QPointF(left, y0 + st.size().height() + 0.15 * lh)
            p.drawStaticText(at, nt)
            ink.boxes.append(QRectF(at, nt.size()))
        top = y0 + self._head_height(pn)
        if pn.timeline is None:
            return top
        if not pn.rows:
            end = self._paint_flights(p, pn, top, span_font)
            self._flush(p)
            self._draw_guides(p)
            return end
        tl = pn.timeline
        row_of = {r.key: i for i, row in enumerate(pn.rows) for r in row}
        ends = _ends(pn)
        strip = self._strip(pn)
        strip_top, pitch = top, self.strip_pitch()
        top += self._mark_lines(pn) * lh
        scales = [self._row_levels(pn, row, ends) for row in pn.rows]

        head = self._row_head()

        def level_y(i: int, level: float) -> float:
            lo, hi = scales[i]
            base = top + i * row_h + row_h - 1.0 * lh      # room for a label under ground
            return base - (level - lo) / (hi - lo) * (row_h - head - 1.0 * lh)

        row_labels = [self._paint_signal_row(p, pn, i, row, top + i * row_h, ends,
                                             lambda v, i=i: level_y(i, v), small)
                      for i, row in enumerate(pn.rows)]

        # The term's ends, each a dot with a guide down to the dimension line.
        dim_y = top + len(pn.rows) * row_h + 0.55 * lh
        dots = []
        r = self._size("dot.radius")
        for e in ends:
            if e.ramp is None or e.ramp not in row_of:
                continue
            x = self.x_of(pn, e.t_ns)
            y = level_y(row_of[e.ramp], tl.ramp(e.ramp).wire(e.t_ns))
            p.setPen(self._line("guide_dot"))
            self._guide(p, QPointF(x, y), QPointF(x, dim_y))
            dots.append((QPointF(x, y), _point_colour(e.kind)))
            ink.dots.append(QRectF(x - r, y - r, 2 * r, 2 * r))
        # A point on no drawn ramp: a dashed guide from its label in the strip above the rows
        # (`_strip`). A deadline is named by the window drawn to it.
        p.setPen(self._line("guide_dash"))
        for e in ends:
            if e.kind == "deadline" and not _on_drawn_ramp(pn, e):
                x = self.x_of(pn, e.t_ns)
                self._guide(p, QPointF(x, strip_top), QPointF(x, dim_y))
        for e, _, lane, _, _ in strip:
            x = self.x_of(pn, e.t_ns)
            self._guide(p, QPointF(x, strip_top + lane * pitch), QPointF(x, dim_y))
        # Chained spans each on their own dimension line, the guides on the next.
        for k, (sp, sym) in enumerate(zip(pn.spans, pn.symbols)):
            self._paint_span(p, pn, sp, sym, dim_y + k * _DIM_H * lh, span_font)
        guide_y = dim_y + len(pn.spans) * _DIM_H * lh
        for start, end_key, label in pn.guides:
            d = tl.event(end_key).t_ns - tl.event(start).t_ns
            ink_col = _colour("interval")
            if label == "margin":
                ink_col = _margin_colour(d)
                value = _signed_ns(d)
            else:
                value = f"{abs(d):.2f}"
            self._dimension(p, self.x_of(pn, tl.event(start).t_ns),
                            self.x_of(pn, tl.event(end_key).t_ns), guide_y,
                            f"{self._sub(label)} {value} ns", ink_col, span_font)
        lanes = _dim_lanes(pn)
        self._paint_axis(p, pn, dim_y + _DIM_H * lanes * lh - 0.2 * lh,
                         self._text_font("axis"))
        # The text, over the lines: the strip's, the dimensions' and the axis's, the rows'.
        for e, lx, lane, _, wrap in strip:
            html = self._html(e.label)
            self._rich(p, lx, strip_top + lane * pitch, html, small, dim, width=wrap)
            ink.boxes.append(QRectF(QPointF(lx, strip_top + lane * pitch),
                                    self._static(html, small, wrap).size()))
        self._flush(p)
        for labels in row_labels:
            self._place_labels(p, labels)
        self._draw_guides(p)
        p.setPen(Qt.NoPen)
        for at, colour in dots:
            p.setBrush(colour)
            p.drawEllipse(at, r, r)
        p.setBrush(Qt.NoBrush)
        return top + len(pn.rows) * row_h + (_DIM_H * lanes + _AXIS_H) * lh

    def _paint_flights(self, p: QPainter, pn: Panel, top: float, font: QFont) -> float:
        """A term drawn as the difference of two flights from one start: each guide an arrow
        from its start to its end, in its signal's colour, labelled under it, then the term's
        dimension between the arrows' ends, a dashed line down from each end; returns where
        the panel ends. The labels are queued for `_flush`."""
        lh = self._lh()
        tl = pn.timeline
        assert tl is not None
        y = top + _FLIGHT_LEAD * lh
        bottom = y + _FLIGHT_H * len(pn.guides) * lh
        xs = sorted({self.x_of(pn, tl.event(k).t_ns) for gd in pn.guides for k in gd[:2]})
        p.setPen(self._line("guide_dash"))
        for x in xs:
            self._guide(p, QPointF(x, top + 0.2 * lh), QPointF(x, bottom + 0.4 * lh))
        head = self._size("flight.head")
        for start, end, label in pn.guides:
            on = tl.event(end).ramp
            ink = (_ink_of("clock") if label.lower().startswith("clock")
                   else _ink(tl.ramp(on)) if on is not None else _ink_of("data"))
            x0, x1 = (self.x_of(pn, tl.event(k).t_ns) for k in (start, end))
            d = 1.0 if x1 >= x0 else -1.0
            p.setPen(self._line("flight", ink))
            self._seg(p, QPointF(x0, y), QPointF(x1 - d * head, y))
            tip = [QPointF(x1, y), QPointF(x1 - d * head, y - 0.45 * head),
                   QPointF(x1 - d * head, y + 0.45 * head)]
            p.setPen(Qt.NoPen)
            p.setBrush(ink)
            p.drawPolygon(tip)
            p.setBrush(Qt.NoBrush)
            self._ink.polyline(tip + tip[:1])
            d_ns = abs(tl.event(end).t_ns - tl.event(start).t_ns)
            html = f"{self._sub(label)} {d_ns:.2f} ns"
            self._pending.append(partial(self._put, p, html, font, ink,
                                         partial(self._dim_spots, x0, x1, y - 0.15 * lh)))
            y += _FLIGHT_H * lh
        for k, (sp, sym) in enumerate(zip(pn.spans, pn.symbols)):
            self._paint_span(p, pn, sp, sym, bottom + (0.4 + k * _DIM_H) * lh, font)
        return bottom + (_DIM_H * max(len(pn.spans), 1) + 0.5) * lh

    def _row_thresholds(self, pn: Panel, row: list[Ramp], ends: list[Event]) -> list:
        """The receivers' thresholds a row draws: those of its ramps that a detection or a
        crossing among the panel's ends is read on."""
        assert pn.timeline is not None
        keys = {r.key for r in row}
        return [th for th in pn.timeline.thresholds if th.ramp in keys and any(
            e.ramp == th.ramp and e.kind in ("detection", "crossing") for e in ends)]

    def _row_levels(self, pn: Panel, row: list[Ramp],
                    ends: list[Event]) -> tuple[float, float]:
        """The levels a row is scaled to, as fractions of V_nom: from the lowest ground it
        draws to `_LEVEL_TOP` or the highest rail, where that is higher."""
        ths = self._row_thresholds(pn, row, ends)
        lo = min([0.0] + [th.corner.ground_shift for th in ths])
        hi = max([_LEVEL_TOP] + [r.vdd_tx for r in row]
                 + [th.corner.ground_shift + th.corner.rx_supply for th in ths])
        return lo, hi

    def _paint_signal_row(self, p: QPainter, pn: Panel, i: int, row: list[Ramp],
                          row_top: float, ends: list[Event], level_y,
                          font: QFont) -> list[_Label]:
        """One row's lines: its levels, then its edges; returns its labels, for
        `_place_labels`."""
        lh = self._lh()
        tl = pn.timeline
        assert tl is not None
        left, right = self._plot_x()
        mid = 0.5 * (left + right)
        ink = _ink(row[0])
        keys = {r.key for r in row}
        drv = _device(row[0])
        ordered = sorted(row, key=lambda r: r.start_ns)
        start_level = ordered[0].level(ordered[0].start_ns)     # where the edge rests, left
        end_level = ordered[-1].level(ordered[-1].start_ns + 1e6)  # and right
        ths = self._row_thresholds(pn, row, ends)
        split = any(abs(th.corner.ground_shift) > 1e-12 for th in ths)
        labels: list[_Label] = []

        def line(level: float, x0: float, x1: float, key: str) -> float:
            y = level_y(level)
            p.setPen(self._line(key))
            self._seg(p, QPointF(x0, y), QPointF(x1, y))
            return y

        def away(level: float, rest: float) -> bool:
            """Above the line unless the edge rests above it at that end."""
            return rest <= level + _EPS

        # The driver's ground and rail, labelled at the left.
        g_end = mid if split else right
        g0 = line(0.0, left, g_end, "gnd")
        labels.append(_Label(g0, f"{drv} gnd", _colour("gnd"), "left", False, left, g_end))
        v_dd = max(r.vdd_tx for r in row)
        vy = line(v_dd, left, mid, "vdd")
        labels.append(_Label(vy, f"{drv} V_DD {pct_text(v_dd - 1.0)}", _colour("vdd"), "left",
                             True, left, mid))
        # The row's name, in its first edge's colour, over its rail's label.
        labels.append(_Label(vy, _row_name(row[0]), ink, "left", True, left, mid, off=1,
                             font="row"))
        for th in ths:
            labels += self._paint_threshold(p, th, tl.ramp(th.ramp), level_y, g0, mid,
                                            end_level, font)
        green = _colour("reference")
        for e in ends:
            if e.ramp in keys and e.kind in ("reference", "anchor"):
                r = tl.ramp(e.ramp)
                level = r.level(e.t_ns)
                # The driver's level: the left half, or to the point where that is further.
                x1 = max(mid, self.x_of(pn, e.t_ns) + lh)
                y = line(level, left, x1, "reference")
                pct = 100.0 * level / r.vdd_tx if r.vdd_tx else 0.0
                labels.append(_Label(y, f"{pct:.0f} % {_device(r)} V_DD", green, "left",
                                     away(level, start_level), left, x1))
        for e in ends:
            if e.ramp not in keys or e.kind not in ("valid", "release", "keeper"):
                continue
            r = tl.ramp(e.ramp)
            level = r.wire(e.t_ns)
            x = self.x_of(pn, e.t_ns)
            colour = _point_colour(e.kind)
            if e.kind == "valid":
                # The 80 % point's level, as an anchor's: V_OH rising, V_OL falling.
                x1 = max(mid, x + lh)
                y = line(level, left, x1, "valid")
                pct = 100.0 * level / r.vdd_tx if r.vdd_tx else 0.0
                labels.append(_Label(y, f"{pct:.0f} % {_device(r)} V_DD", colour, "left",
                                     away(level, start_level), left, x1))
            elif e.kind == "release":
                y = level_y(level)
                tick = self._size("release.length")
                p.setPen(self._line("release"))
                self._seg(p, QPointF(x, y - tick), QPointF(x, y + tick))
            else:
                labels.append(_Label(level_y(level), "keeper follows", colour, "point",
                                     level <= 0.5 * r.vdd_tx, x, x))
        labels += self._paint_row(p, pn, row, level_y)
        return labels

    def _place_labels(self, p: QPainter, labels: list[_Label]) -> None:
        """Each label, in its text class's font, at the first free spot of its sequence
        (`_level_spots`), in order."""
        for lab in labels:
            self._put(p, self._html(lab.text), self._text_font(lab.font), lab.colour,
                      partial(self._level_spots, lab))

    def _paint_threshold(self, p: QPainter, th, r: Ramp, level_y, g0: float, mid: float,
                         end_level: float, font: QFont) -> list[_Label]:
        """A receiver's threshold across the row (whose ramp is `r`), its ground where that
        is offset from the driver's and its rail across the right half, and an arrow from its
        ground to the threshold; returns the labels, under the threshold's line its
        percentage of the driver's V_DD at the left, its bound at the centre and its
        percentage of the receiver's V_DD at the right."""
        lh = self._lh()
        left, right = self._plot_x()
        c = th.corner
        rx, tx = _short_device(th.receiver), _device(r)
        labels = []
        gy = level_y(c.ground_shift)
        if abs(c.ground_shift) > 1e-12:
            gnd = _colour("gnd_offset")
            p.setPen(self._line("gnd_offset"))
            self._seg(p, QPointF(mid, gy), QPointF(right, gy))
            labels.append(_Label(gy, f"{rx} gnd {pct_text(c.ground_shift)}", gnd, "right",
                                 end_level <= c.ground_shift + _EPS, mid, right))
            # The offset itself, a step between the two halves, solid.
            p.setPen(self._line("gnd_offset", dashed=False))
            self._seg(p, QPointF(mid, g0), QPointF(mid, gy))
            for yy in (g0, gy):
                self._seg(p, QPointF(mid - 0.25 * lh, yy), QPointF(mid + 0.25 * lh, yy))
        rail = c.ground_shift + c.rx_supply
        ry = level_y(rail)
        p.setPen(self._line("vdd"))
        self._seg(p, QPointF(mid, ry), QPointF(right, ry))
        labels.append(_Label(ry, f"{rx} V_DD {pct_text(c.rx_supply - 1.0)}", _colour("vdd"),
                             "right", end_level <= rail + _EPS, mid, right))
        accent = _colour("threshold")
        ty = level_y(c.threshold)
        p.setPen(self._line("threshold"))
        self._seg(p, QPointF(left, ty), QPointF(right, ty))
        tx_pct = 100.0 * c.threshold / r.vdd_tx if r.vdd_tx else 0.0
        rx_pct = 100.0 * c.rx_threshold / c.rx_supply if c.rx_supply else 0.0
        labels += [_Label(ty, f"{tx_pct:.0f} % {tx} V_DD", accent, "left", False),
                   _Label(ty, c.bound, accent, "centre", False),
                   _Label(ty, f"{rx_pct:.0f} % {rx} V_DD", accent, "right", False)]
        # The arrow from the receiver's ground, at the right where the edge has settled, solid.
        ax = right - 0.6 * lh
        p.setPen(self._line("threshold", dashed=False))
        self._seg(p, QPointF(ax, gy), QPointF(ax, ty))
        head = self._size("threshold.head")
        d = 1.0 if ty > gy else -1.0
        self._seg(p, QPointF(ax, ty), QPointF(ax - 0.6 * head, ty - d * head))
        self._seg(p, QPointF(ax, ty), QPointF(ax + 0.6 * head, ty - d * head))
        return labels

    def _paint_row(self, p: QPainter, pn: Panel, row: list[Ramp], level_y) -> list[_Label]:
        """One row's edges across the window, each the curve of its own ramp from its start
        to the next edge's in its driver's colour (`_ink`), solid while its driver drives: a
        driver that goes high-Z drives to its `off_ns`, past the next edge's start where they
        overlap, and a turn-on from its start. An undriven stretch is dotted at the level the
        wire holds, labelled "high-Z": after a release in the releasing driver's colour, before
        a turn-on in the colour of the device that last drove the wire (`_undriven_colour`);
        returns those labels."""
        lo, hi = pn.window
        ordered = sorted(row, key=lambda r: r.start_ns)
        w = ds.weight(_signal_key(row[0].signal))
        dash = _DASH[ds.dash("high_z")]
        labels: list[Optional[_Label]] = []
        for i, r in enumerate(ordered):
            solid, dotted = self._pen(_ink(r), w), self._pen(_ink(r), w, dash)
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            start = r.start_ns if (i > 0 or r.from_hiz) else lo
            end = r.off_ns if r.off_ns is not None else (nxt.start_ns if nxt else hi)
            self._trace(p, pn, r.level, max(start, lo), min(end, hi), r.start_ns, level_y,
                        solid)
            if r.from_hiz and i == 0:
                labels.append(self._high_z(p, pn, r.level(r.start_ns), lo, r.start_ns, level_y,
                                           self._pen(_undriven_colour(r), w, dash),
                                           at_end=True))
            if r.off_ns is not None:
                labels.append(self._high_z(p, pn, r.level(r.off_ns), r.off_ns,
                                           nxt.start_ns if nxt else hi, level_y, dotted))
        return [lab for lab in labels if lab is not None]

    def _trace(self, p: QPainter, pn: Panel, level, t0: float, t1: float, knee: float,
               level_y, pen: QPen) -> None:
        """The curve `level` from `t0` to `t1`, sampled at the row's resolution and at `knee`
        (where the ramp leaves its rail)."""
        lo, hi = pn.window
        if t1 <= t0:
            return
        n = max(2, int(_SAMPLES * (t1 - t0) / (hi - lo)))
        ts = sorted({t0 + (t1 - t0) * j / n for j in range(n + 1)}
                    | ({knee} if t0 < knee < t1 else set()))
        pts = [QPointF(self.x_of(pn, t), level_y(level(t))) for t in ts]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        p.setPen(pen)
        p.drawPath(path)
        self._ink.polyline(pts)

    def _high_z(self, p: QPainter, pn: Panel, level: float, t0: float, t1: float, level_y,
                pen: QPen, at_end: bool = False) -> Optional[_Label]:
        """An undriven stretch from `t0` to `t1` (clipped to the window), dotted at `level`;
        returns its "high-Z" label where it is wide enough, on the stretch at its start, or
        with `at_end` at its end, above it where the wire is low."""
        lo, hi = pn.window
        t0, t1 = max(t0, lo), min(t1, hi)
        if t1 <= t0:
            return None
        lh = self._lh()
        x0, x1, y = self.x_of(pn, t0), self.x_of(pn, t1), level_y(level)
        p.setPen(pen)
        self._seg(p, QPointF(x0, y), QPointF(x1, y))
        if x1 - x0 <= 3.5 * lh:
            return None
        return _Label(y, "high-Z", _colour("dim_text"), "right" if at_end else "left",
                      level < 0.5, x0, x1)

    def _dimension(self, p: QPainter, x0: float, x1: float, y: float, label: str,
                   col: QColor, font: QFont, key: str = "interval") -> None:
        """A dimension line from `x0` to `x1` at `y` in `col`, of line class `key` ("span"
        or "interval"), arrowed at both ends; its label is queued for `_flush`, under the
        line by preference (`_dim_spots`)."""
        p.setPen(self._line(key, col))
        self._seg(p, QPointF(x0, y), QPointF(x1, y))
        head = self._size(f"{key}.head")
        for x, d in ((x0, 1 if x1 >= x0 else -1), (x1, -1 if x1 >= x0 else 1)):
            if abs(x1 - x0) > 2 * head:
                self._seg(p, QPointF(x, y), QPointF(x + d * head, y - 0.6 * head))
                self._seg(p, QPointF(x, y), QPointF(x + d * head, y + 0.6 * head))
        self._pending.append(lambda: self._put(
            p, label, font, col, lambda w, h: self._dim_spots(x0, x1, y, w, h)))

    def _paint_span(self, p: QPainter, pn: Panel, sp: Span, symbol: str, y: float,
                    font: QFont) -> None:
        tl = pn.timeline
        assert tl is not None
        x0, x1 = self.x_of(pn, tl.event(sp.start).t_ns), self.x_of(pn, tl.event(sp.end).t_ns)
        self._dimension(p, x0, x1, y,
                        f"{_short_label(symbol, self._sub)} {abs(tl.duration(sp)):.2f} ns",
                        _colour("span"), font, "span")

    def _paint_axis(self, p: QPainter, pn: Panel, y: float, font: QFont,
                    unit_under: bool = False, origin: Optional[float] = None) -> None:
        """A time axis in ns from the term's start, or from `origin`; its numbers, each under
        its tick where that is free, and its unit, over the axis at its right end by
        preference, are queued for `_flush`. With `unit_under` the unit is placed first, under the axis at its right
        end by preference, and a number that would meet it is not drawn."""
        lh = self._lh()
        tl = pn.timeline
        assert tl is not None
        lo, hi = pn.window
        t0 = (min(tl.event(sp.start).t_ns for sp in pn.spans) if origin is None
              else origin)
        step = _nice_step(hi - lo)
        dim = _colour("axis")
        left, right = self._plot_x()
        p.setPen(self._line("axis"))
        self._seg(p, QPointF(left, y), QPointF(right, y))
        ticks = []
        tick = self._size("axis.tick")
        k = math.ceil((lo - t0) / step)
        while t0 + k * step <= hi:
            x = self.x_of(pn, t0 + k * step)
            self._seg(p, QPointF(x, y), QPointF(x, y + tick))
            ticks.append((x, f"{k * step:g}"))
            k += 1

        under = y + 0.4 * lh + max(0.0, tick - 0.25 * lh)    # clear of a longer tick

        def place() -> None:
            s = _STEP * lh

            def unit(w: float, h: float) -> _Spots:
                sides = (y - 0.15 * lh - h, under)
                for yy in (sides[::-1] if unit_under else sides):
                    for x in _slide(right - w, left, right - w, s, -1):
                        yield x, yy, None
            if unit_under:                  # the unit first, at the right end under the axis
                self._put(p, "ns", font, dim, unit)
            for x, number in ticks:
                self._put(p, number, font, dim, partial(_under, x, under), need=False)
            if not unit_under:
                self._put(p, "ns", font, dim, unit)
        self._pending.append(place)
