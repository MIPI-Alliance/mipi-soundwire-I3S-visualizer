"""The Timing view's diagram pane: the term diagram, or one inequality drawn whole.

`InequalityDiagram` is a `timing_diagram.TimingDiagram` that also draws a whole inequality
(`show_inequality`): every term of one inequality on one time axis, one panel per
page column (Specification over Example, or side by side), each at its column's worst corner,
from the leg's `timing.timeline.WholeView`. `show_term` draws one term, as the term diagram
does. A panel shows:
- a one-line heading, the column's name and its terms' values in the order the inequality
  is written, ending in the margin (`margin_text`), green where it is a gap and red where it
  is short;
- the signals at the pins the view names (`WholeView.pins`), the pin the leg is checked at
  last, each pin's data above its clock, each clock row holding every edge the clock makes
  at its pin in the panel's window (`timeline.clock_edges`), each row named in its first
  edge's colour, each data edge in the colour of the device driving it (`timing_diagram._ink`), each edge the curve its ramp model draws, an undriven
  stretch dotted and labelled "high-Z" at its start;
- on each row the rails of the device that receives there, its GND solid and its V_DD
  dotted, across the row and labelled with the device and the rail's offset from nominal
  where it has one; a row no threshold is read on takes the rails of the device at its pin;
- at each point a chain's span ends on, a dot, and the level it is measured at as a short
  line at that point's edge alone: a reference or an anchor at its ramp's 20 % point, and a
  ramp's 80 % point (each labelled as a percentage of the driver's V_DD), a detection or a
  crossing at the receiver's threshold (labelled with the bound), a release with its tick.
  Two points get two lines, whatever their levels;
- the two chains of spans below the rows, the first above the second, every span on its
  chain's dimension line, labelled with its term and its length in ns, with a dotted guide
  down to it from each of its ends drawn on a waveform; a
  point on no drawn ramp has no guide, so every guide drops from a point on a waveform. A
  span that runs back in time, a term of the wrong sign among them, is drawn half a line
  under its chain, joined to the chain at its start. The second chain ends in the
  margin, drawn heavier with an arrowhead at its end, green or red, labelled with its name
  and its sign;
- a time axis in ns from the chains' start, the one scale in every panel, its unit under it
  at its right end by preference.
A row is at least `_ROW_LINES` line heights tall. Side by side, the panels level with each
other (`_peers`) share one height: the tallest heading, the most rows, and the most tiers of
chain labels each side, so their rows, chains and time axes line up.
The worst corners are a footnote under the panels, one line where both columns share one, and
only the drawn column's where one is drawn alone (`set_shown_column`), with the view's own note
on how it is drawn (`WholeView.note`) in place of the leg's.
A leg with no `WholeView` draws its title and a line saying the view does not cover it, and
nothing to export.

Every label takes the first free spot of its sequence, as in the term diagram (`_put`), the
margin's first. A label's spots are all tried clear of the guides before any over them
(`_over_guides`); one sits over a guide only where no spot in the panel is clear of them. A span's label is tried on its chain's side (the first chain's over,
the second's under but the margin's over, a span run back under), then the other side, then
further tiers (`_chain_places`), at each within its span, or for a span narrower than it
centred on it or flush with an end, and then beside it, at most its own height past an end and
over no other span of its chain; where none of those is free, at the nearest free spot of the
grid over or under its line, with a leader whose last piece points at its line at its middle
and ends, straight over or under that point where it can, in an arrowhead a clear gap short of
the line (`_leader_gap`), its head clear of the other leaders' heads, the leader crossing none
of them; where a spot allows, the leader crosses no other leader, and where one allows that,
the head is clear of every line and the leader crosses none but a guide, a rail or a level
line. A
level line's label over or under its line near its point, beside the line's ends, further
along the line, one label further off, then the grid. A row's name sits at the left over the
row's highest level, else where a level's label would.
Every pen, brush, font and size is read from `diagram_style` at paint time, as in the term
diagram: the margin's line class is `margin` (its weight and its arrowhead),
a span's `span`, a span run back is joined to its chain in the interval weight, and the row
names, the span labels, the level labels and the axis numbers take their own text classes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import partial
from typing import Iterator, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter

from ..timing.timeline import Event, LegTimeline, Ramp, WholeView
from . import diagram_style as ds
from . import timing_diagram as td
from .theme import VizTheme
from .timing_diagram import (
    DiagramSection,
    Panel,
    TimingDiagram,
    _Ink,
    _Label,
    _Spots,
)

# Layout, in multiples of the font's line height.
_LANE_H = 3.9         # from the first chain's line to the second's, at least (`_lane_h`)
_LANE_GAP = 0.45      # between a chain's line and the labels beside it, at least (`_gap`)
_LANE_ROOM = 0.45     # between the labels beside a chain and the rows or the axis, at least
_BACK = 0.55          # a span that runs back, under its chain's line
_BESIDE = 0.1         # a span's label beside it, past the clearance (`_pad`) from its end
_EDGE_PAD = 0.6       # a level line, past its edge's ramp at each end
_EDGE_MIN = 1.0       # and at least this far each side of its point
_NEAR = 4.0           # a level line's label, first tried within this of its point
_UNDER = 1.0          # a row's room under its lowest level
_ROW_LINES = 7.0      # a signal row, at least
_WINDOW_PAD = 0.03    # the time window, past the drawn extent at each end, as a fraction

NOT_COVERED = "The whole-inequality view does not cover this inequality."


@dataclass
class WholePanel(Panel):
    """A panel of one inequality drawn whole: its chains, and its heading's numbers."""
    whole: Optional[WholeView] = None
    caption: str = ""


def refusal(sections: list[DiagramSection]) -> str:
    """Why the whole-inequality view does not draw the leg of `sections`, or "" where it
    does: a leg with no `WholeView`."""
    if any(s.timeline is not None and s.timeline.whole is None for s in sections):
        return NOT_COVERED
    return ""


def chain_label(symbol: str, sub) -> str:
    """A chain span's label: the term's parameter, or for a crossing the coefficient's pair
    and the lane (`timing_diagram.crossing_label`), naming the peripheral where it is one of a
    gathered term's detections, as rich text."""
    if " · " in symbol:
        head, _, lane = symbol.partition(" · ")
        fields = head.strip("()").split(" ")[0].split(",")
        pair = ",".join(fields[:2]) if len(fields) > 1 and fields[1] in ("A", "B") else fields[0]
        return td.crossing_label(pair, lane, sub)
    return sub(symbol)


def margin_text(margin_ns: float) -> str:
    """The margin's label: "margin" and its value with its sign, on every leg."""
    return f"margin {td._signed_ns(margin_ns)} ns"


def caption(terms, margin_ns: float) -> str:
    """A column's numbers: each term's own value with its operator, a negative one in
    parentheses as the substitution row writes it (`− (−3.00)`), then the margin
    (`margin_text`) in its colour."""
    out = []
    for i, t in enumerate(terms):
        op = ("" if t.op == "+" else "−") if i == 0 else (" + " if t.op == "+" else " − ")
        own = t.value_ns if t.op == "+" else -t.value_ns
        out.append(op + (f"{abs(own):.2f}" if round(own, 2) >= 0 else f"(−{abs(own):.2f})"))
    colour = td._margin_colour(margin_ns)
    return ("".join(out) + f" = <b><span style='color:{colour.name()}'>"
            f"{margin_text(margin_ns)}</span></b>")


def _corner(s: DiagramSection) -> str:
    """A column's corner line as the whole view states it: the leg's note at its end
    (`LegTimeline.note`) replaced by the view's own where it has one (`WholeView.note`)."""
    tl = s.timeline
    assert tl is not None
    w = tl.whole
    if w is None or w.note is None:
        return s.corner
    base = s.corner.removesuffix(tl.note).rstrip() if tl.note else s.corner
    return f"{base.rstrip('.')}. {w.note}" if w.note else base


def footnote(sections: list[DiagramSection]) -> str:
    """The columns' worst corners, each ending in one full stop: one line where they are the
    same, else one per column; a view that says how it is drawn says it in place of the
    leg's note (`_corner`)."""
    shown = [s for s in sections if s.timeline is not None]
    corners = [_corner(s).removeprefix("Corner: ").rstrip(".") for s in shown]
    if not corners:
        return ""
    if len(set(corners)) == 1:
        both = "Both columns" if len(shown) > 1 else shown[0].title
        return f"{both} at the worst corner: {corners[0]}."
    return "<br>".join(f"{s.title}, worst corner: {c}." for s, c in zip(shown, corners))


def _rows_at(tl: LegTimeline, w: WholeView,
             window: tuple[float, float] = (math.inf, -math.inf)) -> list[list[Ramp]]:
    """The ramps at the view's pins in rows, one per signal at a pin (`timing_diagram._rows`),
    in the pins' order, each pin's data above its clock; each clock row also holds the edges
    the clock makes at its pin in `window` (none by default) past the timeline's own."""
    keys = tuple(r.key for r in tl.ramps if r.pin in w.pins)
    return sorted(td._rows(tl, keys, window),
                  key=lambda row: (w.pins.index(row[0].pin), row[0].signal != "data"))


def _ends(w: WholeView) -> list[str]:
    """The chains' span ends, each once, in chain order."""
    return list(dict.fromkeys(k for c in w.chains for sp in c.spans for k in (sp.start, sp.end)))


class InequalityDiagram(TimingDiagram):
    """The term diagram, or one inequality drawn whole (`show_inequality`); see the module
    docstring."""

    def __init__(self, subscript, term_key, parent=None) -> None:
        super().__init__(subscript, term_key, parent)
        self.whole = False          # an inequality is drawn whole, or refused
        self.refused = ""           # why the leg is not drawn whole; "" where it is
        self._sections: list[DiagramSection] = []   # the leg drawn whole's sections
        # Each panel layout's tiers of chain labels (`_tiers`), by panel, frame, line height
        # and font; cleared by `restyle`.
        self._tier_cache: dict[tuple, tuple[int, int]] = {}

    def _forget_layout(self) -> None:
        self._tier_cache.clear()
        super()._forget_layout()

    # --- state ------------------------------------------------------------------------
    def show_term(self, title: str, key: str, sections: list[DiagramSection]) -> None:
        self.whole, self.refused, self._sections = False, "", []
        super().show_term(title, key, sections)

    def show_inequality(self, title: str, sections: list[DiagramSection]) -> None:
        """Draw the leg of `sections` whole, one panel per section, under `title`; where the
        view does not cover the leg, `title` and the line saying so."""
        was = self.panels
        self.whole, self.term = True, ""
        self.refused = refusal(sections)
        new: list[Panel] = []
        if not self.refused:
            new = [self._panel(s, i) for i, s in enumerate(sections)]
            self._share_window(new)
        self.leg_title = title
        self._sections = [] if self.refused else list(sections)
        if new != was:
            self.panels = new
            self._spots.clear()
            self._tier_cache.clear()
        self._relayout()
        self._emit_exportable()

    @property
    def footnote(self) -> str:
        """The worst corners under the leg drawn whole (`footnote`), of the columns drawn:
        `shown_column`'s where one is chosen."""
        return footnote([s for i, s in enumerate(self._sections)
                         if self.shown_column is None or i == self.shown_column])

    def _panel(self, s: DiagramSection, col: int) -> Panel:
        tl = s.timeline
        if tl is None or tl.whole is None:
            return WholePanel(s.title, "", note=s.note, column=col)
        w = tl.whole
        spans = [sp for c in w.chains for sp in c.spans]
        return WholePanel(s.title, "", tl, spans, [sp.label for sp in spans],
                          _rows_at(tl, w), (0.0, 1.0), column=col, whole=w,
                          caption=caption(s.terms, tl.margin_ns))

    @staticmethod
    def _share_window(pns: list[Panel]) -> None:
        """One time scale for every panel: each from its chains' start, wide enough for every
        panel's chains and the edges of its rows; then each panel's clock rows take every
        edge the clock makes in its window."""
        lo, hi, starts = 0.0, 0.0, {}
        for pn in pns:
            tl = pn.timeline
            if tl is None:
                continue
            assert isinstance(pn, WholePanel) and pn.whole is not None
            t0 = tl.event(pn.whole.start).t_ns
            starts[id(pn)] = t0
            ts = [tl.event(k).t_ns for k in _ends(pn.whole)]
            ts += [t for row in pn.rows for r in row if r.edge
                   for t in (r.start_ns, r.start_ns + td._settle_ns(r))]
            ts += [r.off_ns for row in pn.rows for r in row if r.off_ns is not None]
            lo, hi = min(lo, min(ts) - t0), max(hi, max(ts) - t0)
        pad = _WINDOW_PAD * (hi - lo) + 0.3
        for pn in pns:
            if id(pn) in starts:
                pn.window = (starts[id(pn)] + lo - pad, starts[id(pn)] + hi + pad)
                assert isinstance(pn, WholePanel) and pn.timeline is not None
                assert pn.whole is not None
                pn.rows = _rows_at(pn.timeline, pn.whole, pn.window)

    def can_export(self) -> bool:
        return super().can_export() and not self.refused

    def _over_guides(self) -> bool:
        """Drawn whole, a label takes a spot over a guide only when no spot clear of the
        guides is free, leader spots included (`_put`)."""
        return not self.whole

    # --- geometry ---------------------------------------------------------------------
    def _heading(self, pn: Panel) -> str:
        if isinstance(pn, WholePanel) and pn.timeline is not None:
            return f"<b>{pn.title}</b>&nbsp;&nbsp; {pn.caption}"
        return super()._heading(pn)

    def _mark_lines(self, pn: Panel) -> float:
        return 0.0 if isinstance(pn, WholePanel) else super()._mark_lines(pn)

    def _peers(self, pn: Panel) -> list[tuple[tuple[float, float], Panel]]:
        """The panels drawn level with `pn`, each with its column's frame: side by side, the
        panel at `pn`'s place in every column; else `pn` alone, in the current frame."""
        if self.side_by_side and isinstance(pn, WholePanel):
            cols = self.columns()
            at = next((col.index(pn) for col in cols if pn in col), None)
            if at is not None:
                return [(frame, col[at]) for frame, col in zip(self._frames(), cols)
                        if len(col) > at and col[at].timeline is not None]
        return [(self._frame, pn)]

    def _level(self, pn: Panel, measure) -> list:
        """`measure(panel)` of each of `pn`'s peers (`_peers`), each in its own frame."""
        own, out = self._frame, []
        try:
            for frame, q in self._peers(pn):
                self._frame = frame
                out.append(measure(q))
        finally:
            self._frame = own
        return out

    def _head_height(self, pn: Panel) -> float:
        """The heading's height: the tallest of its peers' (`_peers`), so that panels side by
        side start their rows level."""
        if not isinstance(pn, WholePanel) or pn.timeline is None:
            return super()._head_height(pn)
        return max(self._level(pn, super()._head_height))

    def _row_pad(self, pn: Panel, row_h: float) -> float:
        """The room, in px, under `pn`'s rows that keeps its chains level with a peer's that
        has more rows."""
        return (max(len(q.rows) for q in self._level(pn, lambda q: q)) - len(pn.rows)) * row_h

    def _body_height(self, pn: Panel, row_h: float) -> float:
        if not isinstance(pn, WholePanel):
            return super()._body_height(pn, row_h)
        if pn.timeline is None:
            return 0.0
        top, bottom = self._lanes(pn)
        return (top + self._lane_h(pn) + td._AXIS_H * self._lh() + bottom
                + len(pn.rows) * row_h + self._row_pad(pn, row_h))

    def _row_steps(self) -> tuple[float, ...]:
        """`_ROW_LINES` and the term diagram's steps above it, for an inequality drawn whole."""
        if self.whole:
            return (_ROW_LINES, *(n for n in td._ROW_STEPS if n > _ROW_LINES))
        return super()._row_steps()

    def _footnote_height(self) -> float:
        if not self.whole or not self.footnote:
            return 0.0
        st = self._static(self.footnote, self._small(), self._footnote_w())
        return st.size().height() + 0.4 * self._lh()

    def _footnote_w(self) -> float:
        return max(self.width() - 1.2 * self._lh(), 10.0)

    def _fixed_height(self, col: list[Panel]) -> float:
        return super()._fixed_height(col) + self._footnote_height()

    def _min_height(self) -> int:
        if self.whole and not self.panels:
            return int(self._title_h() + 3.0 * self._lh())
        return super()._min_height()

    # --- painting ---------------------------------------------------------------------
    def paint_diagram(self, p: QPainter) -> None:
        if not self.whole:
            super().paint_diagram(p)
            return
        lh = self._lh()
        if not self.panels:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.fillRect(QRectF(0, 0, self.width(), self.height()), QColor(VizTheme.FRAME_BG))
            self._paint_title(p)
            p.setFont(self._body_font())
            p.setPen(td._colour("dim_text"))
            p.drawText(QRectF(0.6 * lh, self._title_h(), self.width() - 1.2 * lh, 3 * lh),
                       Qt.AlignLeft | Qt.TextWordWrap,
                       "Computing…" if self.pending else self.refused)
            return
        super().paint_diagram(p)
        if self.pending or not self.footnote:
            return
        st = self._static(self.footnote, self._small(), self._footnote_w())
        p.setFont(self._small())
        p.setPen(td._colour("dim_text"))
        p.drawStaticText(QPointF(0.6 * lh, self.height() - st.size().height() - 0.2 * lh), st)

    def _paint_panel_at(self, p: QPainter, pn: Panel, y0: float, row_h: float) -> float:
        if not isinstance(pn, WholePanel):
            return super()._paint_panel_at(p, pn, y0, row_h)
        lh = self._lh()
        left, right = self._plot_x()
        end = y0 + self._head_height(pn) + self._body_height(pn, row_h)
        self._ink = ink = _Ink(QRectF(left, y0, right - left, end - y0), self._pad() * lh)
        self._pending = []
        st = self._static(self._heading(pn), self._head_font(), self._text_w())
        p.setFont(self._head_font())
        p.setPen(td._colour("text"))
        p.drawStaticText(QPointF(left, y0), st)
        ink.boxes.append(QRectF(QPointF(left, y0), st.size()))
        if pn.note:
            p.setFont(self._small())
            p.setPen(td._colour("dim_text"))
            nt = self._static(pn.note, self._small(), self._text_w())
            at = QPointF(left, y0 + st.size().height() + 0.15 * lh)
            p.drawStaticText(at, nt)
            ink.boxes.append(QRectF(at, nt.size()))
        top = y0 + self._head_height(pn)
        tl, w = pn.timeline, pn.whole
        if tl is None or w is None:
            return top
        rows_end = top + len(pn.rows) * row_h
        above, below = self._lanes(pn)
        above += self._row_pad(pn, row_h)
        lane_y = (rows_end + above, rows_end + above + self._lane_h(pn))
        scales = [self._rails(tl, row, w) for row in pn.rows]
        head = self._row_head()

        def level_y(i: int, level: float) -> float:
            lo, hi = scales[i][1], scales[i][2]
            base = top + (i + 1) * row_h - _UNDER * lh
            return base - (level - lo) / max(hi - lo, 1e-9) * (row_h - head - _UNDER * lh)

        row_labels: list[list[_Label]] = []
        for i, row in enumerate(pn.rows):
            row_labels.append(self._paint_rails(p, row, scales[i], partial(level_y, i)))
            # A turn-on's high-Z is labelled at its stretch's start, clear of the levels
            # labelled where its edge starts.
            row_labels[-1] += [replace(lab, at="left") for lab in
                               self._paint_row(p, pn, row, partial(level_y, i))]
        edge_labels = self._paint_levels(p, pn, partial(self._event_y, pn, level_y))
        dots = self._paint_ends(p, pn, lane_y, level_y)
        self._paint_chains(p, pn, lane_y, self._text_font("span"))
        self._paint_axis(p, pn, lane_y[1] + below - 0.2 * lh, self._text_font("axis"),
                         unit_under=True, origin=tl.event(w.start).t_ns)
        self._flush(p)
        for labels in row_labels:
            name = labels[0]
            self._put(p, self._html(name.text), self._text_font(name.font), name.colour,
                      partial(self._name_spots, name))
        for html, colour, spots in edge_labels:
            self._put(p, html, self._label_font(), colour, spots)
        for labels in row_labels:
            self._place_labels(p, labels[1:])
        self._draw_guides(p)
        p.setPen(Qt.NoPen)
        r = self._size("dot.radius")
        for at, colour in dots:
            p.setBrush(colour)
            p.drawEllipse(at, r, r)
        p.setBrush(Qt.NoBrush)
        return end

    @staticmethod
    def _rails(tl: LegTimeline, row: list[Ramp], w: WholeView) -> tuple[str, float, float,
                                                                           float, float]:
        """A row's rails: the device receiving there, its GND and V_DD as fractions of V_nom on
        the drawn ground, and the levels the row is scaled to, from the lowest to the
        highest. The receiver is the one whose threshold a chain's point on the row is read
        on; a row with none takes the rails of the device at its pin where that drives an edge
        of it, else its first edge's driver's."""
        keys = {r.key for r in row}
        ends = {tl.event(k).ramp for k in _ends(w)
                if tl.event(k).kind in ("detection", "crossing")}
        th = next((t for t in tl.thresholds if t.ramp in keys and t.ramp in ends), None)
        top = max(r.vdd_tx for r in row)
        if th is None:
            own = td._short_device(row[0].pin)
            drv = next((r for r in row if td._device(r) == own), row[0])
            name, gnd, vdd = td._device(drv), 0.0, drv.vdd_tx
        else:
            c = th.corner
            name, gnd, vdd = td._short_device(th.receiver), c.ground_shift, \
                c.ground_shift + c.rx_supply
        return name, min(0.0, gnd), max(top, vdd), gnd, vdd

    def _paint_rails(self, p: QPainter, row: list[Ramp], scale, level_y) -> list[_Label]:
        """A row's receiver's GND (solid) and V_DD (dotted) across the row; returns the row's
        name, over the row's highest level at its left, and the rails' labels."""
        left, right = self._plot_x()
        name, _, _, gnd, vdd = scale
        gy, vy = level_y(gnd), level_y(vdd)
        p.setPen(self._line("gnd"))
        self._seg(p, QPointF(left, gy), QPointF(right, gy), soft=True)
        p.setPen(self._line("vdd"))
        self._seg(p, QPointF(left, vy), QPointF(right, vy), soft=True)
        g_text = f"{name} GND" + (f" {td.pct_text(gnd)}" if abs(gnd) > 1e-12 else "")
        v_off = vdd - gnd - 1.0
        v_text = f"{name} V_DD" + (f" {td.pct_text(v_off)}" if abs(v_off) > 1e-12 else "")
        return [_Label(level_y(scale[2]), td._row_name(row[0]), td._ink(row[0]),
                       "left", True, font="row"),
                _Label(vy, v_text, td._colour("vdd"), "right", True),
                _Label(gy, g_text, td._colour("gnd"), "right", False)]

    def _name_spots(self, lab: _Label, w: float, h: float) -> _Spots:
        """A row's name: at the left over its line; then as any level's label
        (`_level_spots`)."""
        lh = self._lh()
        left, _ = self._plot_x()
        yield left + 0.3 * lh, lab.y - td._LABEL_GAP * lh - h, None
        yield None
        yield from self._level_spots(lab, w, h)

    @staticmethod
    def _row_of(pn: WholePanel, e: Event) -> Optional[int]:
        """The row the point `e` is drawn on, or None where it lies on no drawn ramp."""
        if e.ramp is None:
            return None
        return next((i for i, row in enumerate(pn.rows) for r in row if r.key == e.ramp), None)

    def _event_y(self, pn: WholePanel, level_y, e: Event) -> Optional[float]:
        """Where the point `e` is drawn on its row, or None where it lies on no drawn ramp."""
        tl = pn.timeline
        assert tl is not None
        row = self._row_of(pn, e)
        if row is None or e.ramp is None:
            return None
        return level_y(row, tl.ramp(e.ramp).wire(e.t_ns))

    def _paint_levels(self, p: QPainter, pn: WholePanel, event_y):
        """Each chain point's level as a short line at its edge alone, one per point and
        level; returns each line's label, colour and spots."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        lh = self._lh()
        left, right = self._plot_x()
        out, seen = [], set()
        for k in _ends(w):
            e = tl.event(k)
            y = event_y(e)
            if (y is None or e.ramp is None
                    or e.kind not in ("reference", "anchor", "valid", "detection", "crossing")):
                continue
            r = tl.ramp(e.ramp)
            if e.kind in ("reference", "anchor", "valid"):
                level = r.wire(e.t_ns)
                key = "valid" if e.kind == "valid" else "reference"
                pct = 100.0 * level / r.vdd_tx if r.vdd_tx else 0.0
                text = f"{pct:.0f} % {td._device(r)} V_DD"
            else:
                th = next(t for t in tl.thresholds if t.ramp == r.key)
                key, text = "threshold", th.corner.bound
            colour = td._colour(key)
            if (r.key, round(y, 6)) in seen:
                continue
            seen.add((r.key, round(y, 6)))
            x = self.x_of(pn, e.t_ns)
            x0 = min(self.x_of(pn, r.start_ns) - _EDGE_PAD * lh, x - _EDGE_MIN * lh)
            x1 = max(self.x_of(pn, r.start_ns + td._settle_ns(r)) + _EDGE_PAD * lh,
                     x + _EDGE_MIN * lh)
            x0, x1 = max(x0, left), min(x1, right)
            p.setPen(self._line(key))
            self._seg(p, QPointF(x0, y), QPointF(x1, y), soft=True)
            out.append((self._html(text), colour, partial(self._edge_spots, x0, x1, x, y)))
        return out

    def _edge_spots(self, x0: float, x1: float, xp: float, y: float, w: float,
                    h: float) -> _Spots:
        """A level line's label, for the line from `x0` to `x1` through the point at `xp`:
        over it and under it, slid from the point along the line up to `_NEAR` line heights;
        beside its right end, then its left, on the line; over and under it further along the
        line; one label further off; then the grid around the point. Each a stage."""
        lh = self._lh()
        g = 0.3 * lh
        xs = td._slide(xp - 0.5 * w, x0 - 0.5 * w, max(x1 - 0.5 * w, x0), td._STEP * lh, 0)
        near = [x for x in xs if abs(x + 0.5 * w - xp) <= _NEAR * lh]
        sides = (y - td._LABEL_GAP * lh - h, y + td._LABEL_GAP * lh)
        for yy in sides:
            for x in near:
                yield x, yy, None
        yield None
        yield x1 + g, y - 0.5 * h, None
        yield x0 - g - w, y - 0.5 * h, None
        yield None
        for yy in sides:
            for x in xs:
                yield x, yy, None
        yield None
        pitch = h + 2.0 * self._pad() * lh + 1.0
        for yy in (sides[0] - pitch, sides[1] + pitch):
            for x in xs:
                yield x, yy, None
        yield None
        yield from self._grid(xp, y, w, h)

    def _span_y(self, pn: WholePanel, lane_y) -> dict[int, float]:
        """Each chain span's line, by id: its chain's, or under it where the span runs
        back."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        lh = self._lh()
        return {id(sp): lane_y[k] + (_BACK * lh if tl.duration(sp) < -1e-9 else 0.0)
                for k, c in enumerate(w.chains) for sp in c.spans}

    def _paint_ends(self, p: QPainter, pn: WholePanel, lane_y, level_y):
        """A dotted guide from each chain point on a drawn ramp, from its dot down to the lowest
        span line it bounds; a point on no drawn ramp has none. Returns the dots."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        ys = self._span_y(pn, lane_y)
        dots = []
        r = self._size("dot.radius")
        for k in _ends(w):
            e = tl.event(k)
            low = max(ys[id(sp)] for c in w.chains for sp in c.spans if k in (sp.start, sp.end))
            x = self.x_of(pn, e.t_ns)
            y = self._event_y(pn, level_y, e)
            if y is None:
                continue
            p.setPen(self._line("guide_dot"))
            self._guide(p, QPointF(x, y), QPointF(x, low))
            dots.append((QPointF(x, y), td._point_colour(e.kind)))
            self._ink.dots.append(QRectF(x - r, y - r, 2 * r, 2 * r))
            if e.kind == "release":
                tick = self._size("release.length")
                p.setPen(self._line("release"))
                self._seg(p, QPointF(x, y - tick), QPointF(x, y + tick))
        return dots

    def _chain_labels(self, pn: WholePanel):
        """Each chain span's label: (its chain, the span, its html, its line's ends' x, whether
        it runs back, and whether its label goes over its line)."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        out = []
        for k, c in enumerate(w.chains):
            for sp in c.spans:
                d = tl.duration(sp)
                x0, x1 = (self.x_of(pn, tl.event(e).t_ns) for e in (sp.start, sp.end))
                back = d < -1e-9
                html = (f"<b>{margin_text(d)}</b>" if sp is w.margin
                        else f"{chain_label(sp.label, self._sub)} {abs(d):.2f} ns")
                over = not back and (k == 0 or sp is w.margin)
                out.append((k, sp, html, x0, x1, back, over))
        return out

    def _drops(self, pn: WholePanel) -> tuple[float, float]:
        """How far, in px, the place under each chain is moved down: `_BACK` where a span of
        it runs back in `pn` or in a peer (`_peers`), else not."""
        def backs(q: WholePanel) -> set[int]:
            return {k for k, _sp, _h, _a, _b, back, _o in self._chain_labels(q) if back}
        hit = set().union(*self._level(pn, backs))
        d0, d1 = (_BACK * self._lh() if k in hit else 0.0 for k in (0, 1))
        return d0, d1

    def _gap(self) -> float:
        """Between a chain's line and the labels beside it, in px: `_LANE_GAP` line heights,
        at least the text clearance (`_pad`) past the larger of its arrowheads, and at least
        half of it past a leader's arrowhead ending `_leader_gap` short of the line."""
        lh = self._lh()
        heads = max(0.6 * float(ds.value("span.head")), 0.5 * float(ds.value("margin.head")))
        lead = self._leader_gap() / lh + float(ds.value("span.head")) + 0.5 * self._pad()
        return max(_LANE_GAP, self._pad() + heads, lead) * lh

    def _room(self) -> float:
        """Between the labels beside a chain and the rows above or the axis below, in px:
        `_LANE_ROOM` line heights, and at least the text clearance (`_pad`) past the axis's
        offset."""
        return max(_LANE_ROOM, self._pad() + 0.2) * self._lh()

    def _lane_h(self, pn: WholePanel) -> float:
        """From the first chain's line to the second's, in px: `_LANE_H` line heights, and at
        least room for a label under the first (moved down by its `_drops`) and one over the
        second, each `_gap` off its line, clear of each other."""
        lh = self._lh()
        need = (2.0 * (self._gap() + self._label_h()) + self._drops(pn)[0]
                + self._pad() * lh + 1.0)
        return max(_LANE_H * lh, need)

    def _pitch(self) -> float:
        """From one tier of chain labels to the next, in px."""
        return self._label_h() + 2.0 * self._pad() * self._lh() + 1.0

    @staticmethod
    def _slots(k: int, back: bool, over: bool, tiers: tuple[int, int]) -> list[tuple[str, int]]:
        """Where a chain span's label is tried, in order, as (place, tier): over the first
        chain ("over", tiers up), just under it ("under0"), just over the second ("over1") or
        under the second ("under", tiers down), on its own side's first tier, then the
        place between the chains on its side, then its own side's further tiers; a span run
        back on the first chain just under it, then over it."""
        if k == 0 and not back:
            return [("over", 0), ("under0", 0)] + [("over", j) for j in range(1, tiers[0])]
        if k == 0:
            return [("under0", 0)] + [("over", j) for j in range(tiers[0])]
        if over:
            return [("over1", 0), ("under", 0)] + [("under", j) for j in range(1, tiers[1])]
        return ([("under", 0)] + ([] if back else [("over1", 0)])
                + [("under", j) for j in range(1, tiers[1])])

    def _tiers(self, pn: WholePanel) -> tuple[int, int]:
        """The tiers of labels over the first chain and under the second that their labels
        take laid side by side, the margin's first as `_flush` places it: each label at the
        first of its `_chain_places` clear of the guides through its place (`_guide_xs`) and
        of the labels laid before it, a tier more on its side where none is, as `_put` places
        it; a label with no place clear of the guides takes none, as `_chain_spots` sends it
        to the grid with a leader. At least one each. Kept per panel layout."""
        key = (id(pn), self._frame, self._lh(), self._body_font().key())
        if key in self._tier_cache:
            return self._tier_cache[key]
        font, gap = self._text_font("span"), 2.0 * self._pad() * self._lh()
        laid: dict[tuple[str, int], list[tuple[float, float]]] = {}
        most = [1, 1]
        labels = self._chain_labels(pn)
        assert pn.whole is not None
        margin = pn.whole.margin
        pad = self._pad() * self._lh()
        guides = self._guide_xs(pn)
        for label in sorted(labels, key=lambda c: c[1] is not margin):
            size = self._static(label[2], font, -1.0).size()
            w, h = size.width(), size.height()

            def clear(slot: tuple[str, int], x: float, w: float = w) -> bool:
                return not any(x - pad < g < x + w + pad for g in guides[slot[0]])

            def fits(slot: tuple[str, int], x: float, w: float = w) -> bool:
                return all(x + w + gap <= a or x >= b + gap for a, b in laid.get(slot, ()))

            first = [pl for pl in self._chain_places(label, labels, (1, 1), w, h)
                     if pl is not None and clear(*pl)]
            if not first:
                continue
            tiered = any(slot[0] in ("over", "under") for slot, _x in first)
            hit = None
            for n in range(1, 64 if tiered else 2):
                hit = next((pl for pl in self._chain_places(label, labels, (n, n), w, h)
                            if pl is not None and clear(*pl) and fits(*pl)), None)
                if hit is not None:
                    break
            if hit is None:
                continue
            (place, j), x = hit
            laid.setdefault((place, j), []).append((x, x + w))
            if place in ("over", "under"):
                i = 0 if place == "over" else 1
                most[i] = max(most[i], j + 1)
        got = (most[0], most[1])
        if len(self._tier_cache) > 256:
            self._tier_cache.clear()
        self._tier_cache[key] = got
        return got

    def _guide_xs(self, pn: WholePanel) -> dict[str, list[float]]:
        """The x of each guide that runs through each place of chain labels (`_slots`), a
        guide being a chain point's on a drawn ramp (`_paint_ends`): over the first chain every
        such point's, between the chains those of the points the second chain's spans end on,
        under the second none."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        guided = [k for k in _ends(w) if self._row_of(pn, tl.event(k)) is not None]
        every = [self.x_of(pn, tl.event(k).t_ns) for k in guided]
        second = {k for sp in w.chains[1].spans for k in (sp.start, sp.end)}
        between = [self.x_of(pn, tl.event(k).t_ns) for k in guided if k in second]
        return {"over": every, "under0": between, "over1": between, "under": []}

    def _lanes(self, pn: WholePanel) -> tuple[float, float]:
        """The room, in px, from the last row to the first chain's line and from the second
        chain's line to the time axis: the most tiers of labels each side that `pn` or a peer
        takes (`_tiers`, `_peers`), and under the second a span run back."""
        h, pitch = self._label_h(), self._pitch()
        tiers = self._level(pn, self._tiers)
        over, under = max(t[0] for t in tiers), max(t[1] for t in tiers)
        return ((self._gap() + h + (over - 1) * pitch + self._room()),
                (self._drops(pn)[1] + self._gap() + h + (under - 1) * pitch
                 + self._room()))

    def _paint_chains(self, p: QPainter, pn: WholePanel, lane_y, font) -> None:
        """Each chain's spans on its line, labels queued for `_flush`."""
        tl, w = pn.timeline, pn.whole
        assert tl is not None and w is not None
        lh = self._lh()
        text = td._colour("span")
        tiers = self._tiers(pn)
        labels = self._chain_labels(pn)
        for label in labels:
            k, sp, html, x0, x1, back, over = label
            d = tl.duration(sp)
            y = lane_y[k] + (_BACK * lh if back else 0.0)
            colour = text if sp is not w.margin else td._margin_colour(d)
            if back:                            # joined to the chain at its start
                p.setPen(self._line("interval", colour))
                self._seg(p, QPointF(x0, lane_y[k]), QPointF(x0, y))
            if sp is w.margin:
                self._margin_line(p, x0, x1, y, colour)
            else:
                self._span_line(p, x0, x1, y, colour)
            job = partial(self._put, p, html, font, colour,
                          partial(self._chain_spots, label, labels, y, lane_y,
                                  self._drops(pn), tiers), head=True)
            if sp is w.margin:                  # the margin's label takes the first spot
                self._pending.insert(0, job)
            else:
                self._pending.append(job)

    def _span_line(self, p: QPainter, x0: float, x1: float, y: float, colour: QColor) -> None:
        """A term's dimension line, an open arrowhead at each end where it is long enough."""
        p.setPen(self._line("span", colour))
        self._seg(p, QPointF(x0, y), QPointF(x1, y))
        head = self._size("span.head")
        if abs(x1 - x0) > 2 * head:
            for x, d in ((x0, 1 if x1 >= x0 else -1), (x1, -1 if x1 >= x0 else 1)):
                self._seg(p, QPointF(x, y), QPointF(x + d * head, y - 0.6 * head))
                self._seg(p, QPointF(x, y), QPointF(x + d * head, y + 0.6 * head))

    def _margin_line(self, p: QPainter, x0: float, x1: float, y: float, colour: QColor) -> None:
        """The margin's line, heavier, a filled arrowhead at its end pointing the way it runs,
        whatever its length."""
        d = 1.0 if x1 >= x0 else -1.0
        head = self._size("margin.head")
        p.setPen(self._line("margin", colour))
        if abs(x1 - x0) > head:
            self._seg(p, QPointF(x0, y), QPointF(x1 - d * head, y))
        tip = [QPointF(x1, y), QPointF(x1 - d * head, y - 0.5 * head),
               QPointF(x1 - d * head, y + 0.5 * head)]
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        p.drawPolygon(tip)
        p.setBrush(Qt.NoBrush)
        self._ink.polyline(tip + tip[:1])

    def _span_xs(self, x0: float, x1: float, w: float) -> list[float]:
        """A span's label's left edges along it, within the plot: within the span, slid from
        its middle, where it is wider than the label; else centred on it or flush with either
        of its ends; with none within the plot, centred on it at the plot's nearest edge."""
        lh = self._lh()
        left, right = self._plot_x()
        a, b = min(x0, x1), max(x0, x1)
        mid = 0.5 * (a + b)
        if w + 0.4 * lh <= b - a:
            xs = td._slide(mid - 0.5 * w, a + 0.2 * lh, b - 0.2 * lh - w, td._STEP * lh, 0)
        else:
            xs = [mid - 0.5 * w, a, b - w]
        xs = [x for x in xs if left - 1e-6 <= x <= right - w + 1e-6]
        return xs or [min(max(mid - 0.5 * w, left), right - w)]

    def _beside_xs(self, x0: float, x1: float, w: float, h: float,
                   others: list[tuple[float, float]]) -> list[float]:
        """A span's label's left edges beside it, within the plot: its near edge `_BESIDE`
        line heights past an end, clear of that end's guide, then slid away from the end up
        to the label's height `h` past it, the right end's and the left's alternately; none
        that lies over another span of its chain (`others`, each from its lower x), where the
        label would read as that span's."""
        lh = self._lh()
        left, right = self._plot_x()
        a, b = min(x0, x1), max(x0, x1)
        g = self._pad() * lh + _BESIDE * lh
        steps = [g + i * td._STEP * lh for i in range(int(max(h - g, 0.0) / (td._STEP * lh)) + 1)]
        xs = [x for d in steps for x in (b + d, a - d - w)]
        return [x for x in xs if left - 1e-6 <= x <= right - w + 1e-6
                and not any(x < d - 1e-6 and x + w > c + 1e-6 for c, d in others if d > c)]

    def _chain_places(self, label, labels, tiers: tuple[int, int], w: float,
                      h: float) -> Iterator[Optional[tuple[tuple[str, int], float]]]:
        """Where a chain span's label, a `_chain_labels` entry, is tried, as (slot, left edge)
        in order, a None ending each stage: at each of its `_slots` in turn, along its span
        (`_span_xs`), then beside it (`_beside_xs`). The one sequence `_tiers` lays out and
        `_chain_spots` paints."""
        k, sp, _html, x0, x1, back, over = label
        along = self._span_xs(x0, x1, w)
        beside = self._beside_xs(x0, x1, w, h, [(min(a, b), max(a, b)) for kk, s, _h, a, b, *_
                                                in labels if kk == k and s is not sp])
        for slot in self._slots(k, back, over, tiers):
            for x in along:
                yield slot, x
            yield None
            for x in beside:
                yield slot, x
            yield None

    def _chain_spots(self, label, labels, y: float, lane_y, drops, tiers: tuple[int, int],
                     w: float, h: float) -> _Spots:
        """A span's label, its line at `y`: at each of its `_chain_places`, a place under a
        chain moved down by its `drops`, clear of its spans run back; then the grid around
        the span's middle, over or under its line and not across it, with a leader to its line
        there, so that no leader runs along the line: each box at least `_gap` off the line
        (`timing_diagram._bent` bends the leader)."""
        pitch, gap = self._pitch(), self._gap()
        y_of = {"over": lambda j: lane_y[0] - gap - h - j * pitch,
                "under0": lambda j: lane_y[0] + drops[0] + gap,
                "over1": lambda j: lane_y[1] - gap - h,
                "under": lambda j: lane_y[1] + drops[1] + gap + j * pitch}
        for place in self._chain_places(label, labels, tiers, w, h):
            if place is None:
                yield None
                continue
            (slot, j), x = place
            yield x, y_of[slot](j), None
        off = gap
        for spot in self._grid(0.5 * (label[3] + label[4]), y, w, h):
            if spot is None or spot[1] + h <= y - off or spot[1] >= y + off:
                yield spot
