"""The Timing view's term diagram: layout rules (`swi3s_studio/ui/timing_diagram.py`).

Pinned here:
- a row holds the edges of one signal at one pin, alternating in polarity, and a clock is one
  row at a pin, two same-polarity edges of it drawn with the edge between them;
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
  then, and keeps it across a `show_term` that draws the same panels;
- the drawn segments are held as contiguous columns (`_seg_array`), and `_crosses` over them
  agrees with `_seg_hits` over each segment;
- the line height is measured once per font, again after the widget's font changes;
- a paint at the minimum height places every label where `_row_floor`'s unseen layout placed
  it, searching for none; a font, palette or width change, or other panels, lay out anew, and
  at most `_SPOTS_MAX` layouts are kept;
- a sweep's `cover` part (`timing_diagram_support.PARTS`) holds every leg and every term key,
  and its `rest` the other pairs.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing_diagram_layout.py
"""
import os
from functools import partial

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import timing_diagram_support as support

from swi3s_studio.timing import CalcInputs
from swi3s_studio.timing.timeline import SUPPORTED_LEGS

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]


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
    CalcInputs(), CalcInputs(handover_UIs=2.0),
    CalcInputs(Man_ede=True, t_keeper_ns=8.0),
    CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2"),
    CalcInputs(Man_shape="exp", Per_shape="exp"),
)


def test_a_row_holds_one_signal_at_one_pin_with_alternating_edges():
    """A row holds one signal at one pin, its edges alternating in polarity; a clock is one
    row at a pin, so two same-polarity edges of it (a handover's launching edge two UIs on)
    are drawn with the edge between them, in one row."""
    from swi3s_studio.ui.timing_diagram import panels
    from swi3s_studio.ui.timing_view import _term_key
    joined = False
    for inp in _ROW_INPUTS:
        for leg in SUPPORTED_LEGS:
            secs = support.sections(inp, leg)
            for key in support.keys(secs):
                for pn in panels(secs, key, _term_key):
                    for row in pn.rows:
                        assert len({(r.pin, r.signal) for r in row}) == 1, (leg, key)
                        assert all(a.rising != b.rising for a, b in zip(row, row[1:])), row
                    clocks = [(row[0].pin) for row in pn.rows if row[0].signal == "clock"]
                    assert len(clocks) == len(set(clocks)), (leg, key, clocks)
                    for row in pn.rows:
                        same = [r for r in row if r.key in ("clk@man", "clk_l@man")]
                        if len(same) == 2 and same[0].rising == same[1].rising:
                            joined = True
    assert joined, "no panel draws two same-polarity edges of the clock at a pin"


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


@pytest.mark.parametrize("inp,part", support.sweep_params((
    ("default", CalcInputs()), ("handover 2 UIs", CalcInputs(handover_UIs=2.0)),
    ("SW13 1.2 V", CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")))))
def test_a_reference_label_sits_away_from_the_resting_edge(monkeypatch, request, inp, part):
    """Over every leg and term (`part` of them, `timing_diagram_support.PARTS`) at the
    default inputs, two UIs of handover and SW13 at 1.2 V: a reference or anchor level's
    label sits above its line unless the edge rests above it at the left end. The default
    inputs, whole, see both sides. On the platform scope every input set but the default is
    swept over its `cover` part only."""
    if part == "rest":
        support.left_to_the_full_suite(request)
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
    for leg, key, secs in support.sweep(inp, part):
        d.show_term(leg, key, secs)
        d.resize(900, d.minimumHeight())
        assert not d.grab().isNull()
    assert seen and all(above == expected for above, expected in seen), seen
    if part == "whole":
        assert (True, True) in seen and (False, False) in seen


def _widget_paint(p):
    """Whether `p` paints the widget, not the unseen layout its minimum height is found by."""
    from PySide6.QtGui import QPainter
    return isinstance(p, QPainter)


@pytest.mark.parametrize("inp,part", support.sweep_params((
    ("default", CalcInputs()),
    ("SW13 1.2 V", CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")))))
def test_a_panel_paints_within_the_height_reserved_for_it(monkeypatch, request, inp, part):
    """Over every leg and term (`part` of them, `timing_diagram_support.PARTS`) at the
    default inputs and SW13 at 1.2 V: every panel painted ends within the diagram's minimum
    height. The default inputs, whole, draw panels with signal rows and without. On the
    platform scope SW13 is swept over its `cover` part only."""
    if part == "rest":
        support.left_to_the_full_suite(request)
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
    for leg, key, secs in support.sweep(inp, part):
        d.show_term(leg, key, secs)
        d.resize(900, d.minimumHeight())
        ends.clear()
        assert not d.grab().isNull()
        kinds.update(has_rows for _, has_rows, _ in ends)
        assert ends and max(e[2] for e in ends) <= d.minimumHeight() + 1.0, (leg, key)
    if part == "whole":
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


# `_page_sections`' worst corners, by (Manager spec, Peripheral spec), for this process.
_PAGE_WORST: dict = {}


def _page_sections(man, per):
    """For each leg, the Timing page's two columns at a Manager on `man` and Peripheral on
    `per`, each at its worst corner, as the page draws them; the page is built once per pair
    in a process."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.timing import compute
    from swi3s_studio.timing.timeline import leg_timeline
    from swi3s_studio.ui.timing_diagram import DiagramSection
    from swi3s_studio.ui.timing_view import TimingView
    if (man, per) not in _PAGE_WORST:
        tv = TimingView()
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        tv.flush_recompute()
        _PAGE_WORST[man, per] = {side: {leg: tv._worst[side][leg].inputs
                                        for leg in SUPPORTED_LEGS}
                                 for side in ("spec", "proposed")}
    worst = _PAGE_WORST[man, per]
    return lambda leg: [
        DiagramSection(label, leg_timeline(worst[side][leg], leg),
                       list(compute(worst[side][leg]).breakdowns[leg].terms))
        for side, label in (("spec", "Specification"), ("proposed", "Example"))]


_CLASH_CASES = [(f"inputs {i}", inp) for i, inp in enumerate(_ROW_INPUTS)] + [
    ("EDE pairing", ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")),
    ("SoundWire Manager", ("SW13_1V2", "SWI3S_PHY2"))]
# The layouts the no-overlap check paints: its id, the width, side by side, and the heights
# it is painted at besides its minimum.
_LAYOUTS = (("stacked 380", 380, False, ()), ("stacked 900", 900, False, ()),
            ("side by side 1580", 1580, True, (1000,)))
_NARROWEST = _LAYOUTS[0]


def test_the_input_sets_are_distinct():
    """No two of the no-overlap check's input sets are equal."""
    cases = [c for _, c in _CLASH_CASES]
    assert all(a != b for i, a in enumerate(cases) for b in cases[i + 1:])


_AT_THE_NARROWEST = "the platform scope sweeps this at stacked 380 only (docs/TESTING.md §1.1)"


def _left_out_of_the_platform_scope(request, layout) -> None:
    """Skip a layout but `_NARROWEST` on the platform scope (`conftest.platform_scope`),
    which sweeps the default inputs alone at every layout."""
    if layout != _NARROWEST:
        support.left_to_the_full_suite(request, _AT_THE_NARROWEST)


@pytest.mark.parametrize("layout", _LAYOUTS, ids=[lay[0] for lay in _LAYOUTS])
@pytest.mark.parametrize("case", [c for _, c in _CLASH_CASES], ids=[n for n, _ in _CLASH_CASES])
def test_no_text_overlaps_another_or_a_line_and_all_stays_in_its_plot(
        monkeypatch, request, case, layout):
    """Over every leg and every term, stacked at 380 px (the pane's least width) and 900 px
    and side by side at 1580 px, each at its minimum height, and side by side also 1000 px
    tall: no text box painted in a panel overlaps another, none has a drawn line or dot
    inside it, and each lies within the panel's plot (the plot's width, from the panel's top
    to where it ends). A term whose panels equal those an earlier input set draws is left to
    that set's case. On the platform scope every input set but the default, and each
    pairing, is swept at stacked 380 only."""
    if case != CalcInputs():
        _left_out_of_the_platform_scope(request, layout)
    if isinstance(case, CalcInputs):
        def sections_of(leg, inp=case):
            return support.sections(inp, leg)
        earlier = [partial(support.sections, inp) for inp in _ROW_INPUTS[:_ROW_INPUTS.index(case)]]
    else:
        sections_of, earlier = _page_sections(*case), []
    _assert_no_clashes(monkeypatch, _diagram(), sections_of, layout, earlier)


# The no-overlap cases the platform scope checks besides its sweep, each its input set's name
# in `_CLASH_CASES`, its layout's id in `_LAYOUTS`, the leg and the term: with them the
# platform scope reaches every branch of timing_diagram.py the full sweep reaches
# (tools/diagram_cover.py).
_COVER = (("EDE pairing", "side by side 1580", "keeper_Per", "Per_t_DZ"),
          ("EDE pairing", "stacked 900", "MP_contention", "Man_t_DZ"))


@pytest.mark.parametrize("name,layout,leg,key", _COVER, ids=[" ".join(c) for c in _COVER])
def test_no_text_overlaps_another_or_a_line_at_the_platform_scopes_cover(
        monkeypatch, request, name, layout, leg, key):
    """The no-overlap check at a `_COVER` case, which names an input set, a layout, a leg
    and one of its terms; on the platform scope only, as the full sweep checks every case."""
    from conftest import platform_scope
    if not platform_scope(request.config):
        pytest.skip("the no-overlap sweep checks this case")
    case = dict(_CLASH_CASES)[name]
    lay = next(lay for lay in _LAYOUTS if lay[0] == layout)
    sections_of = ((lambda leg: support.sections(case, leg)) if isinstance(case, CalcInputs)
                   else _page_sections(*case))
    assert key in support.keys(sections_of(leg)), (leg, key)
    _assert_no_clashes(monkeypatch, _diagram(), sections_of, lay, only={(leg, key)})


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


@pytest.mark.parametrize("layout", _LAYOUTS, ids=[lay[0] for lay in _LAYOUTS])
def test_no_text_overlaps_another_or_a_line_with_wide_glyphs(monkeypatch, request, layout):
    """The no-overlap check over the default inputs with every glyph `_WIDE_GLYPH` line
    heights wide: a label wider than its row's gaps takes a taller row, one wider than the
    room beside its point wraps, and a heading wider than the plot wraps within it. The
    platform scope checks it at stacked 380 only."""
    _left_out_of_the_platform_scope(request, layout)
    _assert_no_clashes(monkeypatch, _wide_glyphs(_diagram()),
                       lambda leg: support.sections(CalcInputs(), leg), layout)


def _terms_to_check(sections_of, earlier=(), only=None):
    """(leg, its sections, term key) for every leg and term of `sections_of(leg)`, or for the
    (leg, term) pairs `only` names, but those whose panels equal the ones the term draws in
    the sections an `earlier` function gives the leg."""
    from swi3s_studio.ui.timing_diagram import panels
    from swi3s_studio.ui.timing_view import _term_key
    for leg in SUPPORTED_LEGS:
        if only is not None and all(leg != lg for lg, _ in only):
            continue
        secs = sections_of(leg)
        before = [e(leg) for e in earlier]
        for key in sorted(support.keys(secs)):
            if only is not None and (leg, key) not in only:
                continue
            drawn = panels(secs, key, _term_key)
            if not any(key in support.keys(b) and panels(b, key, _term_key) == drawn
                       for b in before):
                yield leg, secs, key


def test_a_term_an_earlier_input_set_draws_alike_is_left_to_it():
    """`_terms_to_check` leaves out exactly the terms whose panels the earlier input set
    draws alike."""
    from swi3s_studio.ui.timing_diagram import panels
    from swi3s_studio.ui.timing_view import _term_key
    every = list(_terms_to_check(partial(support.sections, _ROW_INPUTS[1])))
    left = list(_terms_to_check(partial(support.sections, _ROW_INPUTS[1]),
                                [partial(support.sections, _ROW_INPUTS[0])]))
    assert 0 < len(left) < len(every)
    for leg, secs, key in every:
        alike = panels(secs, key, _term_key) == panels(
            support.sections(_ROW_INPUTS[0], leg), key, _term_key)
        assert ((leg, secs, key) in left) != alike, (leg, key)


def _assert_no_clashes(monkeypatch, d, sections_of, layout, earlier=(), only=None):
    """`d` over every leg and term of `sections_of(leg)` at `layout` (a `_LAYOUTS` entry), or
    over the (leg, term) pairs `only` names: no `_clashes`. A term whose panels equal those it
    draws in the sections an `earlier` function gives the leg is left out
    (`_terms_to_check`)."""
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
    _, width, side_by_side, talls = layout
    d.set_side_by_side(side_by_side)
    d.resize(width, d.height())
    for leg, secs, key in _terms_to_check(sections_of, earlier, only):
        d.show_term(leg, key, secs)
        for tall in (0, *talls):
            d.resize(width, max(d.minimumHeight(), tall))
            painted.clear()
            assert not d.grab().isNull()
            panels += len(painted)
            found += [(width, tall, leg, key, title, c)
                      for title, plot, texts, segs, dots in painted
                      for c in _clashes(plot, texts, segs, dots)]
    assert panels and not found, found[:20]


def test_a_sweeps_cover_holds_every_leg_and_term_and_its_rest_the_others():
    """`timing_diagram_support.cover` holds every leg and every term key of the pairs it is
    given, fewer pairs than all, and `part_of`'s cover and rest split the whole between
    them, in its order."""
    for inp in (CalcInputs(), CalcInputs(Man_ede=True, Per_ede=True, handover_UIs=0.0)):
        every = support.pairs(inp)
        held = support.part_of(every, "cover")
        rest = support.part_of(every, "rest")
        assert held == support.cover(every) and support.part_of(every, "whole") == every
        assert {leg for leg, _ in held} == {leg for leg, _ in every} == set(SUPPORTED_LEGS)
        assert {key for _, key in held} == {key for _, key in every}
        assert 0 < len(held) < len(every) // 3 and rest
        assert sorted(held + rest, key=every.index) == every and not set(held) & set(rest)
    two = [("a", "x"), ("a", "y"), ("b", "x")]
    assert support.cover(two) == [("a", "x"), ("a", "y"), ("b", "x")]
    assert support.cover([("a", "x"), ("b", "x"), ("b", "y")]) == [("a", "x"), ("b", "y")]


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


def test_a_leaders_arrowhead_crosses_no_box_placed_before_it():
    """A label with a headed leader takes no spot whose leader passes clear of a box placed
    before it but whose arrowhead crosses that box; without a head the same spot is free."""
    from PySide6.QtCore import QPointF, QRectF

    from swi3s_studio.ui.timing_diagram import _Ink, _seg_hits
    d = _diagram()
    st = d._static("label", d.font(), -1.0)
    head = d._size("span.head")
    assert head > 2.0
    a = QPointF(150.0, 100.0)                   # the point the leader names
    placed = QRectF(a.x() + 0.3 * head, a.y() + 0.2 * head, 60.0, 18.0)

    def spots(w, h):
        yield a.x() - 0.5 * w, a.y() + placed.height() + 4.0 * head, a   # leader up to a
        yield 300.0, 300.0, None

    for head_on, want_x in ((True, 300.0), (False, a.x() - 0.5 * st.size().width())):
        d._ink = _Ink(QRectF(0.0, 0.0, 400.0, 400.0), 1.8)
        d._ink.boxes.append(placed)
        (box, lead), missed = d._search(st, spots, True, head_on)
        assert not missed and box.x() == pytest.approx(want_x), head_on
    lead = (QPointF(a.x(), a.y() + placed.height() + 3.0 * head), a)
    assert not _seg_hits(lead[0].x(), lead[0].y(), a.x(), a.y(), placed)
    assert any(_seg_hits(p.x(), p.y(), q.x(), q.y(), placed)
               for p, q in d._head_strokes(*lead))


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


@pytest.mark.parametrize("width,side_by_side,part", [
    pytest.param(380, False, "cover", id="stacked 380 cover"),
    pytest.param(380, False, "rest", id="stacked 380 rest"),
    pytest.param(900, False, "whole", id="stacked 900"),
    pytest.param(1580, True, "whole", id="side by side 1580")])
def test_a_panels_labels_take_the_same_spots_wherever_the_panel_sits(
        request, width, side_by_side, part):
    """A level's first spot leaves its padded box tangent to its own line; the panel laid out
    from tops a fraction of a pixel apart in their rounding places every label at the same
    spot relative to its top, a label on the fallback grid included. Over every leg and
    term at the default inputs, stacked at 380 px (in two parts, `cover` and `rest`) and
    900 px and side by side at 1580 px. Every leg and term but a setup or hold leg's
    t_PD,mis, a panel of the two flights alone, has a panel with signal rows. The platform
    scope checks the `cover` part at stacked 380 only."""
    if width != 380:
        support.left_to_the_full_suite(request, _AT_THE_NARROWEST)
    elif part == "rest":
        support.left_to_the_full_suite(request)
    d = _diagram()
    d.set_side_by_side(side_by_side)
    rowless = []
    swept = list(support.sweep(CalcInputs(), part))
    for leg, key, secs in swept:
        d.show_term(leg, key, secs)
        d.resize(width, d.minimumHeight())
        row_h = d._row_h()
        checked = 0
        for _, col in d._each_column():
            for pn in col:
                if not pn.rows:
                    continue
                ref = _label_spots(d, pn, 100.0, row_h)
                for y0 in (55.1234567, 333.3 + 1.0 / 3.0, 2048.03125):
                    assert _same_spots(_label_spots(d, pn, y0, row_h), ref), (
                        leg, key, pn.title, y0)
                checked += 1
        if not checked:
            rowless.append((leg, key))
    assert all(key == "t_PD,mis fraction" and leg[:3] in ("MP_", "PM_")
               and not leg.endswith("_contention") for leg, key in rowless), rowless


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


def test_segment_bounds_are_contiguous_columns_and_crossing_reads_them():
    """`_seg_array` holds x0, y0, x1, y1 and the bounds (x min, x max, y min, y max) as eight
    contiguous rows, one column per segment, and `_crosses` is `_seg_hits` over them."""
    import numpy as np
    from PySide6.QtCore import QRectF

    from swi3s_studio.ui.timing_diagram import _crosses, _seg_array, _seg_hits
    rng = np.random.default_rng(7)
    segs = [tuple(v) for v in rng.uniform(0.0, 100.0, (60, 4)).tolist()]
    a = _seg_array(segs)
    assert a.shape == (8, 60) and a.flags.c_contiguous
    assert a[4].tolist() == [min(s[0], s[2]) for s in segs]
    assert a[7].tolist() == [max(s[1], s[3]) for s in segs]
    assert _seg_array([]).shape == (8, 0) and not _crosses(_seg_array([]), QRectF(0, 0, 9, 9))
    for k in range(400):
        x, y, w, h = rng.uniform(0.0, 100.0, 2).tolist() + rng.uniform(0.0, 25.0, 2).tolist()
        r, some = QRectF(x, y, w, h), segs[k % 55:k % 55 + 6]
        assert _crosses(_seg_array(some), r) == any(_seg_hits(*s, r) for s in some), (k, r)


def test_the_line_height_is_measured_once_per_font(monkeypatch):
    """`_lh` measures the font once and again after the widget's font changes, its own or
    one it inherits."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QWidget

    from swi3s_studio.ui import timing_diagram
    d = _diagram()
    host = QWidget()
    d.setParent(host)
    measured = []
    real = timing_diagram.QFontMetricsF
    monkeypatch.setattr(timing_diagram, "QFontMetricsF",
                        lambda f: (measured.append(f.key()), real(f))[-1])
    d._lh_px = None
    assert d._lh() == d._lh() == real(timing_diagram._design(d.font())).height()
    assert len(measured) == 1
    inherited = QFont(host.font())
    inherited.setPixelSize(19)
    host.setFont(inherited)
    assert d._lh() == real(timing_diagram._design(inherited)).height() and len(measured) == 2
    own = QFont(d.font())
    own.setPixelSize(23)
    d.setFont(own)
    assert d._lh() == d._lh() == real(timing_diagram._design(own)).height()
    assert len(measured) == 3


def _painted_texts(monkeypatch, d):
    """Each text `d`'s widget paint draws, (x, y, html), and how many labels it searched
    a spot for."""
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    texts, searched = [], []
    rich, search = TimingDiagram._rich, TimingDiagram._search
    with monkeypatch.context() as patch:
        patch.setattr(TimingDiagram, "_rich", lambda self, p, x, y, html, *a, **k: (
            _widget_paint(p) and texts.append((x, y, html)), rich(self, p, x, y, html, *a, **k))[-1])
        patch.setattr(TimingDiagram, "_search", lambda self, *a: (
            searched.append(True), search(self, *a))[-1])
        assert not d.grab().isNull()
    return texts, len(searched)


def _fresh_texts(monkeypatch, leg, key, secs, font, width):
    """`_painted_texts` of a new diagram showing `key` of `leg` in `font` at `width` and its
    minimum height, which replays no layout."""
    d = _diagram()
    d.setFont(font)
    d._replayed = lambda html, font: None
    d.resize(width, 600)
    d.show_term(leg, key, secs)
    d.resize(width, d.minimumHeight())
    return _painted_texts(monkeypatch, d)[0]


def test_a_paint_at_the_floor_places_its_labels_where_the_floor_search_did(monkeypatch):
    """At its minimum height a diagram paints every label where `_row_floor`'s unseen layout
    placed it, searching for none, as a diagram that replays nothing does. A font, palette or
    width change, or a `show_term` drawing other panels, lays out anew; one drawing the same
    panels keeps the layouts; at most `_SPOTS_MAX` are kept."""
    from PySide6.QtGui import QFont, QPalette

    from swi3s_studio.ui import timing_diagram
    d = _diagram()
    secs = support.sections(CalcInputs(), "PM_setup")
    d.show_term("PM_setup", "Per_t_DD", secs)
    d.resize(900, d.minimumHeight())
    texts, searched = _painted_texts(monkeypatch, d)
    assert texts and searched == 0
    assert texts == _fresh_texts(monkeypatch, "PM_setup", "Per_t_DD", secs, d.font(), 900)
    kept = dict(d._spots)
    d.show_term("PM_setup", "Per_t_DD", support.sections(CalcInputs(), "PM_setup"))
    assert d._spots == kept and _painted_texts(monkeypatch, d)[1] == 0

    font = QFont(d.font())
    font.setPixelSize(font.pixelSize() + 3 if font.pixelSize() > 0 else 16)
    d.setFont(font)
    assert not d._spots
    d.set_side_by_side(False)                     # the minimum height at the new font
    d.resize(900, d.minimumHeight())
    texts, searched = _painted_texts(monkeypatch, d)
    assert searched == 0
    assert texts == _fresh_texts(monkeypatch, "PM_setup", "Per_t_DD", secs, font, 900)

    d.resize(640, d.height())
    d.set_side_by_side(False)                     # the minimum height at the new width
    d.resize(640, d.minimumHeight())
    texts, searched = _painted_texts(monkeypatch, d)
    assert searched == 0
    assert texts == _fresh_texts(monkeypatch, "PM_setup", "Per_t_DD", secs, font, 640)

    d.setPalette(QPalette(d.palette().color(QPalette.Window).darker()))
    assert not d._spots
    d.resize(640, d.minimumHeight())
    assert not d.grab().isNull() and d._spots
    d.show_term("PM_setup", "UI", secs)
    assert all(any(pn is shown for shown in d.panels) for pn, _ in d._spots.values())
    monkeypatch.setattr(timing_diagram, "_SPOTS_MAX", 3)
    for h in range(d.minimumHeight(), d.minimumHeight() + 60, 10):
        d.resize(640, h)
        assert not d.grab().isNull()
        assert len(d._spots) <= 3
