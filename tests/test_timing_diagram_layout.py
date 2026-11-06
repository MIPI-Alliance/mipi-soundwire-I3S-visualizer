"""The Timing view's term diagram: layout rules (`swi3s_studio/ui/timing_diagram.py`).

Pinned here:
- a row holds the edges of one signal at one pin, and only where they alternate in
  polarity; two same-polarity edges at a pin are on separate rows;
- a Peripheral-to-Peripheral panel names its driver's levels "Per A";
- a reference or anchor level's label sits above its line unless the edge rests above it at
  the left end, so it never sits on the resting trace;
- a panel paints within the height the diagram reserves for it, whatever it draws, and a
  label for a point on no drawn ramp sits in a strip above the first row, clear of the
  levels' labels;
- no text a panel paints overlaps another text or a drawn line or dot, and all of it lies
  within the panel's plot;
- a panel's labels take the same spots wherever the panel sits, a label whose padded box is
  tangent to its own line and a label on the fallback grid included;
- a signal row is at least `_row_floor` tall, grows with the pane and stops at `_ROW_ASPECT`
  of the column's width;
- a shown diagram finds its floor once a width change settles, keeping the last one until
  then, and keeps it across a `show_term` that draws the same panels.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing_diagram_layout.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import timing_diagram_support as support

from swi3s_studio.timing import CalcInputs
from swi3s_studio.timing.timeline import SUPPORTED_LEGS

pytestmark = pytest.mark.gui


def _diagram():
    """A diagram 900 px wide, the width the tests lay it out at: its minimum height, which
    follows its width, is then read at that width."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript, _term_key
    d = TimingDiagram(_subscript, _term_key)
    d.resize(900, 600)
    return d


_ROW_INPUTS = (
    CalcInputs(), CalcInputs(handover_UIs=1.0), CalcInputs(handover_UIs=2.0),
    CalcInputs(Man_ede=True, t_keeper_ns=8.0),
    CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2"),
    CalcInputs(Man_shape="exp", Per_shape="exp"),
)


def test_a_row_holds_one_signal_at_one_pin_with_alternating_edges():
    from swi3s_studio.ui.timing_diagram import panels
    from swi3s_studio.ui.timing_view import _term_key
    split = False
    for inp in _ROW_INPUTS:
        for leg in SUPPORTED_LEGS:
            secs = support.sections(inp, leg)
            for key in support.keys(secs):
                for pn in panels(secs, key, _term_key):
                    for row in pn.rows:
                        assert len({(r.pin, r.signal) for r in row}) == 1, (leg, key)
                        assert all(a.rising != b.rising for a, b in zip(row, row[1:])), row
                    same = [r for row in pn.rows for r in row
                            if r.key in ("clk@man", "clk_l@man")]
                    if len(same) == 2 and same[0].rising == same[1].rising:
                        assert not any(
                            len({"clk@man", "clk_l@man"} & {r.key for r in row}) == 2
                            for row in pn.rows)
                        split = True
    assert split, "no panel holds two same-polarity edges at one pin"


def test_a_peripheral_to_peripheral_panel_names_its_driver_per_a(monkeypatch):
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    d = _diagram()
    texts = []
    place = TimingDiagram._place_labels
    monkeypatch.setattr(TimingDiagram, "_place_labels", lambda self, p, labs, *a: (
        texts.extend(lab.text for lab in labs), place(self, p, labs, *a))[-1])
    secs = support.sections(CalcInputs(), "PP_setup")
    assert "Per_t_DD" in support.keys(secs)
    d.show_term("PP_setup", "Per_t_DD", secs)
    d.resize(900, d.minimumHeight())
    assert not d.grab().isNull()
    assert any(t.startswith("Per A gnd") for t in texts), texts
    assert any(t.startswith("Per A V_DD") for t in texts), texts
    assert not any(t.startswith("Per gnd") or t.startswith("Per V_DD") for t in texts), texts


def test_a_reference_label_sits_away_from_the_resting_edge(monkeypatch):
    from PySide6.QtGui import QColor

    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_diagram import _EPS, TimingDiagram, _ends
    d = _diagram()
    seen = []
    real = TimingDiagram._paint_signal_row

    def spy(self, p, pn, i, row, row_top, ends, level_y, font):
        got = real(self, p, pn, i, row, row_top, ends, level_y, font)
        ordered = sorted(row, key=lambda r: r.start_ns)
        rest = ordered[0].level(ordered[0].start_ns)
        keys = {r.key for r in row}
        levels = [pn.timeline.ramp(e.ramp).level(e.t_ns) for e in _ends(pn)
                  if e.ramp in keys and e.kind in ("reference", "anchor")]
        green = [lab for lab in got if lab.colour == QColor(VizTheme.DIAG_OK)]
        assert len(green) == len(levels)
        seen.extend((lab.above, rest <= level + _EPS) for lab, level in zip(green, levels))
        return got

    monkeypatch.setattr(TimingDiagram, "_paint_signal_row", spy)
    for inp in (CalcInputs(), CalcInputs(handover_UIs=2.0),
                CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")):
        for leg in SUPPORTED_LEGS:
            secs = support.sections(inp, leg)
            for key in support.keys(secs):
                d.show_term(leg, key, secs)
                d.resize(900, d.minimumHeight())
                assert not d.grab().isNull()
    assert seen and all(above == expected for above, expected in seen), seen
    assert (True, True) in seen and (False, False) in seen


def _widget_paint(p):
    """Whether `p` paints the widget, not the unseen layout its minimum height is found by."""
    from PySide6.QtGui import QPainter
    return isinstance(p, QPainter)


def test_a_panel_paints_within_the_height_reserved_for_it(monkeypatch):
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    d = _diagram()
    ends = []
    real = TimingDiagram._paint_panel

    def spy(self, p, pn, y0, row_h):
        end = real(self, p, pn, y0, row_h)
        if _widget_paint(p):
            ends.append((pn.title, bool(pn.rows), end))
        return end

    monkeypatch.setattr(TimingDiagram, "_paint_panel", spy)
    kinds = set()
    for inp in (CalcInputs(), CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")):
        for leg in SUPPORTED_LEGS:
            secs = support.sections(inp, leg)
            for key in support.keys(secs):
                d.show_term(leg, key, secs)
                d.resize(900, d.minimumHeight())
                ends.clear()
                assert not d.grab().isNull()
                kinds.update(has_rows for _, has_rows, _ in ends)
                assert ends and max(e[2] for e in ends) <= d.minimumHeight() + 1.0, (leg, key)
    assert kinds == {True, False}, "both panel kinds drawn"


class _PaintSpy:
    """A painter that passes every call through and records where static text is drawn."""

    def __init__(self, painter, tops):
        self._painter, self._tops = painter, tops

    def drawStaticText(self, pos, text):
        self._tops.append(pos.y())
        return self._painter.drawStaticText(pos, text)

    def __getattr__(self, name):
        return getattr(self._painter, name)


def _mark_strip(monkeypatch, inp, leg, key):
    """Paint `key` of `leg` and return (the y of every label above the first row's top, that
    top, the tops of the first row's level labels, the line height, the strip's line
    pitch)."""
    with monkeypatch.context() as patch:
        return _mark_strip_in(patch, inp, leg, key)


def _mark_strip_in(monkeypatch, inp, leg, key):
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    d = _diagram()
    drawn, rows, label_tops = [], [], []
    rich, row = TimingDiagram._rich, TimingDiagram._paint_signal_row
    place, put = TimingDiagram._place_labels, TimingDiagram._put
    calls, placing = [], []

    def placed(self, *a, **k):
        placing.append(True)
        try:
            return put(self, *a, **k)
        finally:
            placing.pop()

    # A strip label is drawn as rich text outside `_put`, which places every other label.
    monkeypatch.setattr(TimingDiagram, "_put", placed)
    monkeypatch.setattr(TimingDiagram, "_rich", lambda self, p, x, y, html, *a, **k: (
        placing or drawn.append(y), rich(self, p, x, y, html, *a, **k))[-1])

    def first_labels(self, p, labs, *a):
        calls.append(True)
        return place(self, _PaintSpy(p, label_tops) if len(calls) == 1 else p, labs, *a)

    monkeypatch.setattr(TimingDiagram, "_place_labels", first_labels)
    monkeypatch.setattr(TimingDiagram, "_paint_signal_row", lambda self, p, pn, i, r, row_top,
                        *a: (i or rows.append(row_top), row(self, p, pn, i, r, row_top, *a))[-1])
    columns = TimingDiagram._paint_columns
    monkeypatch.setattr(TimingDiagram, "_paint_columns", lambda self, p, row_h: (
        _widget_paint(p) and [seen.clear() for seen in (drawn, rows, label_tops, calls)],
        columns(self, p, row_h))[-1])                   # what the widget's paint draws
    secs = support.sections(inp, leg)
    d.show_term(leg, key, secs)
    d.resize(900, d.minimumHeight())
    assert not d.grab().isNull()
    return [y for y in drawn if y < rows[0]], rows[0], label_tops, d._lh(), d.strip_pitch()


def test_a_label_for_a_point_on_no_drawn_ramp_clears_the_first_rows_labels(monkeypatch):
    """The labels of points on no drawn ramp stand on one line each (two at most, the second
    one line lower, a line `strip_pitch` tall), and the strip's last line ends the margin
    short of the first row, above that row's own labels."""
    from swi3s_studio.ui import timing_diagram
    one = _mark_strip(monkeypatch, CalcInputs(Man_spec="SWI3S_PHY2", Per_spec="SW13_1V2"),
                      "PM_setup", "Per_t_DD")
    two = _mark_strip(monkeypatch, CalcInputs(), "PP_setup", "bus length (cm)")
    assert len(one[0]) == 1 and len(two[0]) == 2, (one[0], two[0])
    for marks, row_top, label_tops, lh, pitch in (one, two):
        assert label_tops
        strip_end = max(marks) + pitch
        assert strip_end + timing_diagram._MARK_MARGIN * lh <= row_top + 1e-6, (marks, row_top)
        assert strip_end <= min(label_tops) + 1e-6, (marks, label_tops)
    assert max(two[0]) - min(two[0]) == pytest.approx(two[4]), "the second mark is one line lower"


class _Recorder:
    """A painter that passes every call through and records, in the widget's coordinates,
    each text box (a static text's laid-out size, a drawn text's bounding rect), each line
    segment (a line, a path's polygons, a polygon's edges) and each dot."""

    def __init__(self, painter, texts, segs, dots):
        from PySide6.QtGui import QFont
        self._painter, self.texts, self.segs, self.dots = painter, texts, segs, dots
        self._font = QFont(painter.font())

    def setFont(self, font):
        from PySide6.QtGui import QFont
        self._font = QFont(font)
        return self._painter.setFont(font)

    def drawStaticText(self, pos, text):
        from PySide6.QtCore import QRectF
        self.texts.append((QRectF(pos, text.size()), text.text()))
        return self._painter.drawStaticText(pos, text)

    def drawText(self, *a):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QFontMetricsF
        if len(a) == 3 and isinstance(a[0], QRectF):
            self.texts.append((QFontMetricsF(self._font).boundingRect(a[0], int(a[1]), a[2]),
                               a[2]))
        return self._painter.drawText(*a)

    def drawLine(self, *a):
        if len(a) == 2:
            self.segs.append((a[0].x(), a[0].y(), a[1].x(), a[1].y()))
        else:
            self.segs.append((a[0].x1(), a[0].y1(), a[0].x2(), a[0].y2()))
        return self._painter.drawLine(*a)

    def drawPath(self, path):
        for poly in path.toSubpathPolygons():
            pts = list(poly)
            self.segs += [(a.x(), a.y(), b.x(), b.y()) for a, b in zip(pts, pts[1:])]
        return self._painter.drawPath(path)

    def drawPolygon(self, pts, *a):
        pts = list(pts)
        self.segs += [(p.x(), p.y(), q.x(), q.y()) for p, q in zip(pts, pts[1:] + pts[:1])]
        return self._painter.drawPolygon(pts, *a)

    def drawEllipse(self, centre, rx, ry):
        from PySide6.QtCore import QRectF
        self.dots.append(QRectF(centre.x() - rx, centre.y() - ry, 2 * rx, 2 * ry))
        return self._painter.drawEllipse(centre, rx, ry)

    def __getattr__(self, name):
        return getattr(self._painter, name)


def _clashes(plot, texts, segs, dots):
    """Every text box outside `plot`, overlapping another, or with a drawn line or dot inside
    it. Lines are sampled every half pixel and a box is taken half a pixel in from its edges,
    so a line along or ending on its edge, a leader line included, is not inside it."""
    import numpy as np
    pts = [np.zeros((0, 2))]
    for x0, y0, x1, y1 in segs:
        t = np.linspace(0.0, 1.0, max(2, int(np.hypot(x1 - x0, y1 - y0) / 0.5) + 2))
        pts.append(np.stack([x0 + t * (x1 - x0), y0 + t * (y1 - y0)], axis=1))
    xy = np.concatenate(pts)
    found = []
    for i, (box, text) in enumerate(texts):
        inner = box.adjusted(0.5, 0.5, -0.5, -0.5)
        if not plot.adjusted(-0.5, -0.5, 0.5, 0.5).contains(box):
            found.append(("outside the plot", text))
        found += [("over a text", text, other) for other_box, other in texts[i + 1:]
                  if inner.intersects(other_box.adjusted(0.5, 0.5, -0.5, -0.5))]
        if np.any((xy[:, 0] > inner.left()) & (xy[:, 0] < inner.right())
                  & (xy[:, 1] > inner.top()) & (xy[:, 1] < inner.bottom())):
            found.append(("over a line", text))
        found += [("over a dot", text) for dot in dots if inner.intersects(dot)]
    return found


def _page_sections(man, per):
    """For each leg, the Timing page's two columns at a Manager on `man` and Peripheral on
    `per`, each at its worst corner, as the page draws them."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.timing import compute
    from swi3s_studio.timing.timeline import leg_timeline
    from swi3s_studio.ui.timing_diagram import DiagramSection
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
    tv.flush_recompute()
    worst = {side: {leg: tv._worst[side][leg].inputs for leg in SUPPORTED_LEGS}
             for side in ("spec", "proposed")}
    return lambda leg: [
        DiagramSection(label, leg_timeline(worst[side][leg], leg),
                       list(compute(worst[side][leg]).breakdowns[leg].terms))
        for side, label in (("spec", "Specification"), ("proposed", "Example"))]


_CLASH_CASES = [(f"inputs {i}", inp) for i, inp in enumerate(_ROW_INPUTS)] + [
    ("EDE pairing", ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")),
    ("SoundWire Manager", ("SW13_1V2", "SWI3S_PHY2"))]


@pytest.mark.parametrize("case", [c for _, c in _CLASH_CASES], ids=[n for n, _ in _CLASH_CASES])
def test_no_text_overlaps_another_or_a_line_and_all_stays_in_its_plot(monkeypatch, case):
    """Over every leg and every term, stacked at 380 px (the pane's least width) and 900 px
    and side by side at 1580 px, each at its minimum height, and side by side also 1000 px
    tall: no text box painted in a panel overlaps another, none has a drawn line or dot
    inside it, and each lies within the panel's plot (the plot's width, from the panel's top
    to where it ends)."""
    if isinstance(case, CalcInputs):
        def sections_of(leg, inp=case):
            return support.sections(inp, leg)
    else:
        sections_of = _page_sections(*case)
    _assert_no_clashes(monkeypatch, _diagram(), sections_of)


# A label font's mean glyph advance, in line heights, that `_wide_glyphs` sets.
_WIDE_GLYPH = 0.82


def _wide_glyphs(d):
    """`d` with its font's letter spacing widened so the narrow label font's mean glyph
    advance is at least `_WIDE_GLYPH` line heights (the box glyphs of a platform with no
    fonts are about that wide); a font already that wide is left as it is."""
    from PySide6.QtGui import QFont, QFontMetricsF
    d._frame = (0.0, 380.0)
    advance = QFontMetricsF(d._label_font()).averageCharWidth() / d._lh()
    if advance < _WIDE_GLYPH:
        font = QFont(d.font())
        font.setLetterSpacing(QFont.PercentageSpacing, 100.0 * _WIDE_GLYPH / advance)
        d.setFont(font)
    return d


def test_no_text_overlaps_another_or_a_line_with_wide_glyphs(monkeypatch):
    """The no-overlap check over the default inputs with every glyph `_WIDE_GLYPH` line
    heights wide: a label wider than its row's gaps takes a taller row, one wider than the
    room beside its point wraps, and a heading wider than the plot wraps within it."""
    _assert_no_clashes(monkeypatch, _wide_glyphs(_diagram()),
                       lambda leg: support.sections(CalcInputs(), leg))


def _assert_no_clashes(monkeypatch, d, sections_of):
    """`d` over every leg and term of `sections_of(leg)` at the layouts of
    `test_no_text_overlaps_another_or_a_line_and_all_stays_in_its_plot`: no `_clashes`."""
    from PySide6.QtCore import QRectF

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    painted = []
    real = TimingDiagram._paint_panel

    def spy(self, p, pn, y0, row_h):
        if not _widget_paint(p):
            return real(self, p, pn, y0, row_h)
        texts, segs, dots = [], [], []
        end = real(self, _Recorder(p, texts, segs, dots), pn, y0, row_h)
        left, right = self._plot_x()
        painted.append((pn.title, QRectF(left, y0, right - left, end - y0), texts, segs, dots))
        return end

    monkeypatch.setattr(TimingDiagram, "_paint_panel", spy)
    found, panels = [], 0
    for width, side_by_side, talls in ((380, False, (0,)), (900, False, (0,)),
                                       (1580, True, (0, 1000))):
        d.set_side_by_side(side_by_side)
        d.resize(width, d.height())
        for leg in SUPPORTED_LEGS:
            secs = sections_of(leg)
            for key in sorted(support.keys(secs)):
                d.show_term(leg, key, secs)
                for tall in talls:
                    d.resize(width, max(d.minimumHeight(), tall))
                    painted.clear()
                    assert not d.grab().isNull()
                    panels += len(painted)
                    found += [(width, tall, leg, key, title, c)
                              for title, plot, texts, segs, dots in painted
                              for c in _clashes(plot, texts, segs, dots)]
    assert panels and not found, found[:20]


def test_a_box_tangent_to_a_placed_box_or_line_is_free_wherever_it_sits():
    """A box whose padded edge meets a placed box's edge, or lies along a line, is free at
    every panel top, however the sum of the top and the offsets rounds."""
    from PySide6.QtCore import QPointF, QRectF

    from swi3s_studio.ui.timing_diagram import _Ink
    pad = 1.8
    for k in range(200):
        y0 = 55.1234567 + 7.31 * k + k / 3.0
        ink = _Ink(QRectF(0.0, y0, 400.0, 400.0), pad)
        ink.boxes.append(QRectF(7.2, y0, 365.6, 28.0))
        ink.line(QPointF(0.0, y0 + 80.0), QPointF(400.0, y0 + 80.0))
        assert ink.free(QRectF(255.2, y0 + 28.0 + pad, 60.0, 12.0)), y0
        assert ink.free(QRectF(20.0, y0 + 80.0 + pad, 60.0, 12.0)), y0
        assert not ink.free(QRectF(255.2, y0 + 28.0 + pad - 0.01, 60.0, 12.0)), y0


def _label_spots(d, pn, y0, row_h):
    """Each label `pn` places laid out unseen from `y0`: its text and its box's top-left
    corner, relative to `y0`."""
    from swi3s_studio.ui.timing_diagram import _Unseen
    out = []
    d._rich = lambda p, x, y, html, font, col, **_: out.append((html, x, y - y0))
    try:
        d._misses = d._strays = 0
        d._paint_panel(_Unseen(), pn, y0, row_h)
    finally:
        del d._rich
    return out


def _same_spots(got, ref):
    """Two `_label_spots` lists agree: the same texts in the same order, each corner within
    1e-5 px."""
    return len(got) == len(ref) and all(
        g[0] == r[0] and g[1:] == pytest.approx(r[1:], abs=1e-5) for g, r in zip(got, ref))


@pytest.mark.parametrize("width,side_by_side", [(380, False), (900, False), (1580, True)],
                         ids=["stacked 380", "stacked 900", "side by side 1580"])
def test_a_panels_labels_take_the_same_spots_wherever_the_panel_sits(width, side_by_side):
    """A level's first spot leaves its padded box tangent to its own line; the panel laid out
    from tops a fraction of a pixel apart in their rounding places every label at the same
    spot relative to its top, a label on the fallback grid included. Over every leg and
    term, stacked at 380 px and 900 px and side by side at 1580 px."""
    d = _diagram()
    d.set_side_by_side(side_by_side)
    checked = 0
    for leg in SUPPORTED_LEGS:
        secs = support.sections(CalcInputs(), leg)
        for key in sorted(support.keys(secs)):
            d.show_term(leg, key, secs)
            d.resize(width, d.minimumHeight())
            row_h = d._row_h()
            for _, col in d._each_column():
                for pn in col:
                    if not pn.rows:
                        continue
                    ref = _label_spots(d, pn, 100.0, row_h)
                    for y0 in (55.1234567, 333.3 + 1.0 / 3.0, 2048.03125):
                        assert _same_spots(_label_spots(d, pn, y0, row_h), ref), (
                            leg, key, pn.title, y0)
                    checked += 1
    assert checked >= 80, checked


def test_a_row_is_the_floor_grows_with_the_pane_and_stops_at_the_aspect_cap():
    """MP_setup's Per_t_IS at 900 px: at the minimum height a row is `_row_floor`; a taller
    pane draws taller rows; a very tall one stops at `_ROW_ASPECT` of the column's width."""
    from swi3s_studio.ui.timing_diagram import _ROW_ASPECT
    d = _diagram()
    secs = support.sections(CalcInputs(), "MP_setup")
    d.show_term("MP_setup", "Per_t_IS", secs)
    heights = (d.minimumHeight(), 700, 1200, 3000)
    rows = []
    for h in heights:
        d.resize(900, h)
        floor, width = d._row_floor(), d._frames()[0][1]
        cap = max(_ROW_ASPECT * width, floor)
        row_h = d._row_h()
        assert floor - 1e-9 <= row_h <= cap + 1e-9, (h, row_h, floor, cap)
        rows.append((row_h, floor, cap))
    assert rows[0][0] == pytest.approx(rows[0][1], abs=1.0)
    assert rows[0][0] < rows[1][0] <= rows[2][0] <= rows[3][0], rows
    assert rows[3][0] == pytest.approx(rows[3][2]) and rows[3][2] > rows[3][1], rows


def test_a_shown_diagram_finds_its_floor_once_a_width_change_settles(monkeypatch):
    """Width steps on a shown diagram lay nothing out unseen while they follow each other;
    `_FLOOR_DELAY_MS` after the last, the floor is found at the new width. A `show_term` that
    draws the same panels keeps the floor, and one that draws others finds it anew.

    The steps run with the timer's interval held long, so a slow resize and paint cannot
    let it fire between two of them; the interval is then restored and the timer started
    as the last step starts it."""
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_diagram import _FLOOR_DELAY_MS, TimingDiagram
    d = _diagram()
    unseen = []
    columns = TimingDiagram._paint_columns
    monkeypatch.setattr(TimingDiagram, "_paint_columns", lambda self, p, row_h: (
        _widget_paint(p) or unseen.append(row_h), columns(self, p, row_h))[-1])
    secs = support.sections(CalcInputs(), "PM_hold")
    d.show_term("PM_hold", "bus length (cm)", secs)
    d.show()
    QApplication.processEvents()
    try:
        unseen.clear()
        d._floor_timer.setInterval(600_000)
        for w in range(901, 911):
            d.resize(w, d.height())
            QApplication.processEvents()
        assert not unseen and d._floor_timer.isActive()
        d._floor_timer.setInterval(_FLOOR_DELAY_MS)
        d._floor_timer.start()
        QTest.qWait(3 * _FLOOR_DELAY_MS)
        assert unseen and d._floor[0][0] == d.width() == 910
        unseen.clear()
        d.show_term("PM_hold", "bus length (cm)", secs)
        assert not unseen
        d.show_term("PM_hold", "Per_t_DD", secs)
        assert unseen
    finally:
        d.hide()
