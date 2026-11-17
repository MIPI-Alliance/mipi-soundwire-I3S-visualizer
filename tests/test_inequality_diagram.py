"""The Timing page's whole-inequality view (`swi3s_studio/ui/inequality_diagram.py`).

Pinned here, for every inequality (`WHOLE_LEGS`) over `_CASES`, which hold each with margins
of both signs:
- no text a panel paints overlaps another text or a drawn line or dot, and all of it lies
  within the panel's plot, at the term diagram's layouts, and the footnote lies under the last
  panel;
- every span's label shows its term (a gathered term's detection by its own crossing) and
  its length, the margin's "margin" and its sign on every leg, also
  where it is negative, every label finds a free spot, and
  each reads as its own span's: over it, just beside it, or with a leader pointing at it;
- a leader's arrowhead stops a clear gap short of its span's line, its head clear of every
  other's and of every leader, and by default inequality 8)'s leaders cross no other;
- no chain label holds a guide or any other line, and every one lies within the room its
  panel keeps by the chains, also at a SoundWire Peripheral and with the peripherals apart;
- the chains close on the margin: the margin's line ends where the first chain does,
  pointing back where the margin is negative; no band is filled through the rows;
- each row draws its pin's device's GND and V_DD across the row, labelled with the device;
- side by side, the two columns' panels share one height, their rows, chains and time axes
  level, also where one column's labels take a tier more;
- a level line is drawn at its point's edge alone, one per edge and level, lines at one
  height apart;
- the view exports as PNG, SVG and PDF, on the light palette too;
- on the page, a click on an inequality's heading or on its equation row off its terms draws
  it whole, a second click on it or one in blank space closes it, and a click on a term still
  draws the term; every inequality's row draws it whole.
The platform scope sweeps the default and EDE cases of the label and line tests, the full
suite all of them (docs/TESTING.md §1.1).

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_inequality_diagram.py
"""
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from timing_diagram_support import left_to_the_full_suite

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.timeline import WHOLE_LEGS, leg_timeline

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]

# The term diagram's no-overlap layouts: id, width, side by side, and heights besides the
# minimum.
_LAYOUTS = (("stacked 380", 380, False, ()), ("stacked 900", 900, False, ()),
            ("side by side 1580", 1580, True, (1000,)))


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _diagram(width=900):
    """A diagram `width` px wide in the page's diagram font."""
    _app()
    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    from swi3s_studio.ui.timing_view import _FONT_DIAGRAM, _base_font, _subscript, _term_key
    d = InequalityDiagram(_subscript, _term_key)
    font = _base_font()
    font.setPixelSize(_FONT_DIAGRAM)
    d.setFont(font)
    d.resize(width, 600)
    return d


def _sections(spec, example, leg):
    """Two columns of `leg`: "Specification" at `spec` and "Example" at `example`, each with
    a corner line."""
    from swi3s_studio.ui.timing_diagram import DiagramSection
    return [DiagramSection(label, leg_timeline(inp, leg),
                           list(compute(inp).breakdowns[leg].terms), corner=f"Corner: {label}")
            for label, inp in (("Specification", spec), ("Example", example))]


_PROP = CalcInputs(Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP")
# Column pairs: the default and proposed sources; A far from B, and both far; a SoundWire
# Manager on exponential ramps, and a SoundWire Peripheral at two handover UIs; every leg
# short on the left and with room on the right; SWI3S PHY2 EDE on both sides, and EDE on
# both at two handover UIs; turn-ons late, and releases late at short turn-ons; a slow keeper
# and long hold times.
_CASES = (("default", CalcInputs(), _PROP),
          ("driver far", CalcInputs(pp_placement="driver_far"),
           CalcInputs(pp_placement="both_far")),
          ("SoundWire Manager", CalcInputs(Man_spec="SW13_1V2", Man_shape="exp",
                                           Per_shape="exp"),
           CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")),
          ("short and long", CalcInputs(Man_t_DD_max_ns=30.0, Man_t_DD_min_ns=0.0,
                               Per_t_DD_max_ns=27.0, Per_t_DD_min_ns=0.0, Man_t_IH_max_ns=8.0),
           CalcInputs(Per_t_DD_max_ns=9.0, Man_t_DD_min_ns=12.0, Per_t_DD_min_ns=9.0)),
          ("EDE", CalcInputs(Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE",
                             handover_UIs=0.0),
           CalcInputs(Man_ede=True, Per_ede=True, handover_UIs=2.0, Per_shape="exp")),
          ("turn-on late", CalcInputs(handover_UIs=0.0, Man_t_ZD_ns=40.0, Per_t_ZD_ns=40.0),
           CalcInputs(handover_UIs=0.0, Man_t_DZ_max_ns=30.0, Per_t_DZ_max_ns=30.0,
                      Man_t_ZD_ns=2.0, Per_t_ZD_ns=2.0)),
          ("keeper slow", CalcInputs(t_keeper_ns=30.0, Man_t_IH_max_ns=60.0,
                                     Per_t_IH_max_ns=60.0),
           CalcInputs(handover_UIs=2.0, Per_spec="SW13_1V2", Per_t_DD_min_ns=30.0)))
# The cases the platform scope sweeps.
_PLATFORM = ("default", "EDE", "turn-on late", "keeper slow")


def _case_params():
    return [pytest.param(*c, id=c[0]) for c in _CASES]


def _scoped(request, name):
    """Skip case `name` on the platform scope where it is not one of `_PLATFORM`."""
    if name not in _PLATFORM:
        left_to_the_full_suite(request)


def _legs(*inputs):
    """The legs that apply at every one of `inputs`."""
    return [leg for leg in WHOLE_LEGS
            if not any(compute(inp).breakdowns[leg].not_applicable for inp in inputs)]


def _show(d, leg, spec, example):
    d.show_inequality(f"1) {leg}", _sections(spec, example, leg))
    d.resize(d.width(), d.minimumHeight())


def test_the_cases_hold_margins_of_both_signs_on_every_leg():
    """The no-overlap cases draw every leg with a margin below zero and one above, the
    platform scope's at least one of each class: a handover, a contention, an EDE contention
    and a keeper leg below zero."""
    for leg in WHOLE_LEGS:
        signs = {compute(inp).breakdowns[leg].margin_ns > 0
                 for _, a, b in _CASES for inp in (a, b)}
        assert signs == {True, False}, leg
    short = {leg for name, a, b in _CASES if name in _PLATFORM for inp in (a, b)
             for leg in WHOLE_LEGS if compute(inp).breakdowns[leg].margin_ns < 0}
    assert {"MP_hold_ho", "PP_contention", "keeper_Man"} <= short, short


@pytest.mark.parametrize("layout", _LAYOUTS, ids=[lay[0] for lay in _LAYOUTS])
@pytest.mark.parametrize("name,spec,example", _case_params())
def test_no_text_overlaps_another_or_a_line_and_the_footnote_is_under_the_panels(
        monkeypatch, request, layout, name, spec, example):
    """Every leg, at each layout at its minimum height (and a side-by-side one
    1000 px tall): no text box a panel paints overlaps another or holds a drawn line or dot,
    each lies within its panel's plot, and the footnote starts under every panel's end."""
    from PySide6.QtCore import QRectF
    from test_timing_diagram_layout import _clashes, _Recorder, _widget_paint

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

    _scoped(request, name)
    monkeypatch.setattr(TimingDiagram, "_paint_panel", spy)
    _, width, side_by_side, talls = layout
    d = _diagram(width)
    d.set_side_by_side(side_by_side)
    found = []
    for leg in _legs(spec, example):
        for tall in (0, *talls):
            _show(d, leg, spec, example)
            d.resize(width, max(d.minimumHeight(), tall))
            painted.clear()
            assert not d.grab().isNull()
            assert len(painted) == 2, leg
            found += [(leg, tall, title, c) for title, plot, texts, segs, dots in painted
                      for c in _clashes(plot, texts, segs, dots)]
            note = d._footnote_height()
            assert note > 0
            assert max(plot.bottom() for _, plot, *_ in painted) <= d.height() - note + 1e-6
    assert not found, found[:20]


# Column pairs the chain labels are checked at besides `_CASES`: a SoundWire Peripheral, and
# peripherals a flight apart.
_LABEL_CASES = (("SoundWire Peripheral", CalcInputs(Per_spec="SW13_1V2"),
                 CalcInputs(Per_spec="SW13_1V2", Per_shape="exp")),
                ("peripherals apart", CalcInputs(pp_placement="driver_far"),
                 CalcInputs(pp_placement="both_far")))
# The layouts the chain labels are checked at: the pane's and the maximized page's.
_LABEL_LAYOUTS = (("stacked 900", 900, False), ("side by side 1580", 1580, True))


def _label_params():
    return [pytest.param(*c, id=c[0]) for c in (*_LABEL_CASES, *_CASES)]


def _chain_layout(monkeypatch, d):
    """A list that fills, as `d` paints on the widget, with one entry per panel: its chain
    labels, each a dict of its html, box, leader (None, or its points from its box to its
    end), chain, span ends' x and line's y, and the other spans of its chain; the panel's
    guides and other segments (each x0, y0, x1, y1), its plot, its chains' lines' y and the
    room each side of them (`_lanes`), the gap a leader ends short of its line
    (`_leader_gap`) and the arrowhead's strokes (`_head_strokes`)."""
    from test_timing_diagram_layout import _widget_paint

    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    out, cur = [], {}
    real_chains, real_put = InequalityDiagram._paint_chains, InequalityDiagram._put
    real_search, real_replayed = InequalityDiagram._search, InequalityDiagram._replayed
    real_draw = InequalityDiagram._draw_guides

    def chains(self, p, pn, lane_y, font):
        if _widget_paint(p):
            ys = self._span_y(pn, lane_y)
            labels = self._chain_labels(pn)
            want = [{"html": html, "k": k, "x0": x0, "x1": x1, "y": ys[id(sp)],
                     "others": [(min(a, b), max(a, b)) for kk, s, _h, a, b, *_ in labels
                                if kk == k and s is not sp]}
                    for k, sp, html, x0, x1, *_ in sorted(labels,
                                                         key=lambda c: c[1] is not pn.whole.margin)]
            cur.update(want=want, labels=[], lane_y=tuple(lane_y), lanes=self._lanes(pn))
        return real_chains(self, p, pn, lane_y, font)

    def search(self, st, spots, need, *a):
        got = real_search(self, st, spots, need, *a)
        self._spied = got[0]
        return got

    def replayed(self, html, font):
        got = real_replayed(self, html, font)
        self._spied = None if got is None else got[0]
        return got

    def put(self, p, html, *a, **k):
        self._spied = None
        box = real_put(self, p, html, *a, **k)
        if _widget_paint(p) and cur.get("want") and cur["want"][0]["html"] == html:
            lab = cur["want"].pop(0)
            lab.update(box=box, lead=None if self._spied is None else self._spied[1])
            cur["labels"].append(lab)
        return box

    def draw(self, p):
        if _widget_paint(p) and "labels" in cur:
            ink = self._ink
            out.append({"labels": cur["labels"], "plot": QRectF(ink.plot),
                        "guides": [(a.x(), a.y(), b.x(), b.y()) for a, b, _ in ink.guides],
                        "segs": list(ink._segs), "lane_y": cur["lane_y"],
                        "lanes": cur["lanes"], "gap": self._leader_gap(),
                        "strokes": self._head_strokes})
            cur.clear()
        return real_draw(self, p)

    from PySide6.QtCore import QRectF
    monkeypatch.setattr(InequalityDiagram, "_paint_chains", chains)
    monkeypatch.setattr(InequalityDiagram, "_search", search)
    monkeypatch.setattr(InequalityDiagram, "_replayed", replayed)
    monkeypatch.setattr(InequalityDiagram, "_put", put)
    monkeypatch.setattr(InequalityDiagram, "_draw_guides", draw)
    return out


def _each_chain_layout(monkeypatch, request, name, spec, example, layout):
    """(leg, panel entry of `_chain_layout`) for every leg of the case, painted at `layout`."""
    if name not in {c[0] for c in _LABEL_CASES}:
        _scoped(request, name)
    _, width, side_by_side = layout
    panels = _chain_layout(monkeypatch, d := _diagram(width))
    d.set_side_by_side(side_by_side)
    for leg in _legs(spec, example):
        _show(d, leg, spec, example)
        panels.clear()
        d._spots.clear()
        assert not d.grab().isNull()
        assert len(panels) == 2, leg
        for pn in panels:
            assert pn["labels"] and all(lab["box"] is not None for lab in pn["labels"]), leg
            yield leg, pn


def _inner(box):
    return box.adjusted(0.5, 0.5, -0.5, -0.5)


@pytest.mark.parametrize("layout", _LABEL_LAYOUTS, ids=[lay[0] for lay in _LABEL_LAYOUTS])
@pytest.mark.parametrize("name,spec,example", _label_params())
def test_no_chain_label_sits_over_a_guide_or_a_line(monkeypatch, request, layout, name, spec,
                                                    example):
    """No chain span's label, on any leg, holds a guide (a dotted line from a point down to
    the chains) or any other drawn line, a span's line included, anywhere within its box: no
    label falls back over a guide."""
    from swi3s_studio.ui.timing_diagram import _seg_hits
    found = []
    for leg, pn in _each_chain_layout(monkeypatch, request, name, spec, example, layout):
        for lab in pn["labels"]:
            box = _inner(lab["box"])
            for kind in ("guides", "segs"):
                if any(_seg_hits(*s, box) for s in pn[kind]):
                    found.append((leg, kind, lab["html"]))
    assert not found, found[:10]


@pytest.mark.parametrize("layout", _LABEL_LAYOUTS, ids=[lay[0] for lay in _LABEL_LAYOUTS])
@pytest.mark.parametrize("name,spec,example", _label_params())
def test_a_span_label_reads_as_its_own_span_s(monkeypatch, request, layout, name, spec,
                                              example):
    """A chain span's label, with no leader, lies over its own span (within it, centred on it
    or flush with an end), or beside it, its near edge at most its own height past an end and
    over no other span of its chain, where it would read as that span's. A label with a
    leader has its leader end in an arrowhead, at least two strokes from that end, its last
    piece pointing at its span's line between its ends. By default, inequality 3)'s t_PD,mis
    takes one."""
    found, leads = [], set()
    for leg, pn in _each_chain_layout(monkeypatch, request, name, spec, example, layout):
        left, right = pn["plot"].left(), pn["plot"].right()
        for lab in pn["labels"]:
            box, lead = lab["box"], lab["lead"]
            a, b = min(lab["x0"], lab["x1"]), max(lab["x0"], lab["x1"])
            if b < left or a > right:
                continue
            if lead is not None:
                leads.add((leg, re.sub(r"<[^>]+>", "", lab["html"])))
                knee, end = lead[-2], lead[-1]
                at = end.x() + (end.x() - knee.x()) * (lab["y"] - end.y()) / (end.y() - knee.y())
                heads = [s for s in pn["segs"] if s[0] == end.x() and s[1] == end.y()
                         and (s[2], s[3]) != (end.x(), end.y())]
                if not (a - 1e-6 <= at <= b + 1e-6 and len(heads) >= 2):
                    found.append((leg, "leader off its span", lab["html"]))
                continue
            over = box.left() <= b + 1e-6 and box.right() >= a - 1e-6
            near = min(abs(box.left() - b), abs(a - box.right()))
            beside = near <= box.height() + 1e-6 and not any(
                box.left() < d - 1e-6 and box.right() > c + 1e-6
                for c, d in lab["others"] if d > c)
            if not (over or beside):
                found.append((leg, "not by its span", lab["html"]))
    assert not found, found[:10]
    if name == "default" and layout[2]:
        assert ("MP_hold", "tPD,mis 0.20 ns") in leads, sorted(leads)


def _pieces(lead):
    return list(zip(lead, lead[1:]))


def _bounds(segs):
    from PySide6.QtCore import QRectF
    xs = [q.x() for s in segs for q in s]
    ys = [q.y() for s in segs for q in s]
    return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


@pytest.mark.parametrize("layout", _LABEL_LAYOUTS, ids=[lay[0] for lay in _LABEL_LAYOUTS])
@pytest.mark.parametrize("name,spec,example", _label_params())
def test_a_leader_s_arrowhead_stops_short_of_its_span_and_clear_of_the_others(
        monkeypatch, request, layout, name, spec, example):
    """Every chain label's leader, on every leg and in both columns, ends in a last piece
    that points at a point of its span, between its ends, its tip at least the leader gap
    (`_leader_gap`) from its span's line and so from that point; no two leaders' arrowheads'
    bounds meet, and no leader crosses another's head. By default, inequality 8)'s leaders
    also cross no other leader."""
    from swi3s_studio.ui.timing_diagram import _seg_hits, _segs_cross
    found = []
    for leg, pn in _each_chain_layout(monkeypatch, request, name, spec, example, layout):
        gap, heads, leads = pn["gap"], [], []
        assert gap >= 3.0
        for lab in pn["labels"]:
            lead = lab["lead"]
            if lead is None:
                continue
            text = re.sub(r"<[^>]+>", "", lab["html"])
            a, b, y = min(lab["x0"], lab["x1"]), max(lab["x0"], lab["x1"]), lab["y"]
            knee, tip = lead[-2], lead[-1]
            toward = (knee.y() - tip.y()) * (tip.y() - y) > 0
            at = tip.x() + (tip.x() - knee.x()) * (y - tip.y()) / (tip.y() - knee.y())
            if not (toward and a - 1e-6 <= at <= b + 1e-6 and abs(tip.y() - y) >= gap - 1e-6):
                found.append((leg, "tip not short of its line, pointing at its span", text))
            box = _bounds(pn["strokes"](knee, tip))
            if any(box.intersects(o) for o in heads):
                found.append((leg, "heads meet", text))
            for p, q in _pieces(lead):
                if any(_seg_hits(p.x(), p.y(), q.x(), q.y(), o) for o in heads):
                    found.append((leg, "leader over a head", text))
                if name == "default" and leg == "PM_hold_ho" and any(
                        _segs_cross(p, q, c, d) for other in leads for c, d in _pieces(other)):
                    found.append((leg, "leaders cross", text))
            heads.append(box)
            leads.append(lead)
    assert not found, found[:10]


@pytest.mark.parametrize("layout", _LABEL_LAYOUTS, ids=[lay[0] for lay in _LABEL_LAYOUTS])
@pytest.mark.parametrize("name,spec,example", _label_params())
def test_the_room_by_the_chains_holds_every_chain_label(monkeypatch, request, layout, name,
                                                        spec, example):
    """Every chain label lies within its panel's plot, and one with no leader within the room
    the panel keeps over the first chain and under the second (`_lanes`), which `_tiers`
    counts from the same places the painter tries (`_chain_places`)."""
    found = []
    for leg, pn in _each_chain_layout(monkeypatch, request, name, spec, example, layout):
        (y0, y1), (above, below) = pn["lane_y"], pn["lanes"]
        for lab in pn["labels"]:
            box = lab["box"]
            if not pn["plot"].adjusted(-0.01, -0.01, 0.01, 0.01).contains(box):
                found.append((leg, "outside the plot", lab["html"]))
            elif lab["lead"] is None and not (y0 - above - 1e-6 <= box.top()
                                              and box.bottom() <= y1 + below + 1e-6):
                found.append((leg, "outside the chains' room", lab["html"]))
    assert not found, found[:10]


def _placed(monkeypatch, d):
    """Every label `d` places as it paints: (its html, its box, or None where it is not
    drawn)."""
    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    got = []
    real = InequalityDiagram._put

    def spy(self, p, html, *a, **k):
        r = real(self, p, html, *a, **k)
        got.append((html, r))
        return r

    monkeypatch.setattr(InequalityDiagram, "_put", spy)
    return got


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_every_span_label_shows_its_term_and_its_length(monkeypatch, request, name, spec,
                                                         example):
    """Each chain span's label is its term (`chain_label`) and its length to the hundredth, the margin's "margin" and its sign, each drawn, on every leg and of either
    sign, never "overlap" or "short"; a gathered term's two detections
    are labelled with their own crossings, A's and B's; no label is placed without a free
    spot."""
    from swi3s_studio.ui.inequality_diagram import chain_label
    from swi3s_studio.ui.timing_diagram import _signed_ns
    from swi3s_studio.ui.timing_view import _subscript
    _scoped(request, name)
    d = _diagram(1200)
    for leg in _legs(spec, example):
        got = _placed(monkeypatch, d)
        d._misses = 0
        _show(d, leg, spec, example)
        d._spots.clear()
        d.grab()
        assert d._misses == 0, leg
        drawn = {html for html, r in got if r is not None}
        for pn in d.panels:
            tl, w = pn.timeline, pn.whole
            for c in w.chains:
                for sp in c.spans:
                    n = tl.duration(sp)
                    want = (f"<b>margin {_signed_ns(n)} ns</b>" if sp is w.margin
                            else f"{chain_label(sp.label, _subscript)} {abs(n):.2f} ns")
                    assert want in drawn, (leg, want)
            assert not any("overlap" in h or "short" in h for h in drawn), (leg, drawn)
            assert "margin" in pn.caption and "overlap" not in pn.caption \
                and "short" not in pn.caption, (leg, pn.caption)
            if leg.startswith("PP"):
                assert any("MP,A" in h for h in drawn) and any("MP,B" in h for h in drawn)
            # A crossing's lane word is in its subscript (`crossing_label`).
            assert not any(re.search(r"</sub> (CLK|DATA)\b", h) for h in drawn), (leg, drawn)


def test_a_crossing_label_puts_its_lane_word_in_the_subscript(monkeypatch):
    """A crossing's span label, in the whole view (`chain_label`) and the term diagram
    (`_short_label`), sets its lane word in the pair's subscript after `LANE_SEP`: X_MP on
    the clock reads X<sub>MP,CLK</sub>, a peripheral's detection X<sub>MP,B,CLK</sub>, the
    keeper's X<sub>K,DATA</sub>; the term diagram names a gathered term by its pair alone.
    The separator is read from `LANE_SEP` alone."""
    from swi3s_studio.ui import timing_diagram as td
    from swi3s_studio.ui.inequality_diagram import chain_label
    from swi3s_studio.ui.timing_view import _subscript
    cases = (("X_MP,fall,early · Man_t_RF,CLK", "MP", "MP"),
             ("X_MP,rise,late · Man_t_RF,DATA", "MP", "MP"),
             ("X_PM,rf,late · Per_t_RF,DATA", "PM", "PM"),
             ("X_PM,rf,late · Man_t_RF,CLK", "PM", "PM"),
             ("X_MP,B,fall,early · Man_t_RF,CLK", "MP,B", "MP"),
             ("X_PP,B,rise,late · Per_t_RF,DATA", "PP,B", "PP"),
             ("X_K,rise,late · Man_t_RF,DATA", "K", "K"),
             ("(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK", "MP,A", "MP"))
    for sep in (",", "_"):
        monkeypatch.setattr(td, "LANE_SEP", sep)
        for symbol, whole, term in cases:
            word = symbol.rsplit(",", 1)[1]
            assert chain_label(symbol, _subscript) == f"X<sub>{whole}{sep}{word}</sub>", symbol
            assert td._short_label(symbol, _subscript) == f"X<sub>{term}{sep}{word}</sub>", \
                symbol


def test_a_columns_numbers_add_up_to_its_margin():
    """Each column's heading numbers, a term of the wrong sign in parentheses, add up to the
    margin it ends in, within the rounding of the numbers printed."""
    import re

    from swi3s_studio.ui.inequality_diagram import caption
    seen = set()
    for _, spec, example in (*_CASES, ("negative", CalcInputs(Man_t_IH_max_ns=-3.0),
                                       CalcInputs(Per_t_IS_max_ns=-1.0))):
        for inp in (spec, example):
            res = compute(inp).breakdowns
            for leg in _legs(inp):
                terms = res[leg].terms
                text = re.sub(r"<[^>]+>", "", caption(terms, res[leg].margin_ns))
                left, _, right = text.partition(" = ")
                seen.add("(" in left)
                got = eval(left.replace("−", "-"))      # noqa: S307 - digits and operators
                assert got == pytest.approx(res[leg].margin_ns, abs=0.006 * len(terms)), \
                    (leg, text)
                assert right.split()[1].replace("−", "-").lstrip("+") == \
                    f"{res[leg].margin_ns:.2f}".lstrip("+") or abs(res[leg].margin_ns) < 0.005
    assert seen == {True, False}


def test_the_time_axis_counts_from_the_chains_start(monkeypatch):
    """The axis's 0 is under the chains' start, also where a span of the wrong sign runs back
    before it."""
    d = _diagram(1200)
    got = _placed(monkeypatch, d)
    for leg, inp in (("PM_hold_ho", CalcInputs(Man_t_IH_max_ns=-3.0)),
                     ("MP_setup", CalcInputs()), ("keeper_Per", CalcInputs())):
        got.clear()
        _show(d, leg, inp, inp)
        d._spots.clear()
        d.grab()
        zeros = sorted({round(r.center().x(), 3) for html, r in got
                        if html == "0" and r is not None})
        starts = sorted({round(d.x_of(pn, pn.timeline.event(pn.whole.start).t_ns), 3)
                         for pn in d.panels})
        assert zeros == pytest.approx(starts, abs=0.5), leg


def test_the_chains_close_on_the_margin(monkeypatch):
    """The margin's line ends where the first chain ends (`WholeView.end`), its last span's
    end; its arrowhead points back where the margin
    is negative; it is the second chain's last span."""
    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    lines, margins = [], []
    span, margin = InequalityDiagram._span_line, InequalityDiagram._margin_line
    monkeypatch.setattr(InequalityDiagram, "_span_line",
                        lambda self, p, x0, x1, *a: (lines.append((x0, x1)),
                                                     span(self, p, x0, x1, *a))[1])
    monkeypatch.setattr(InequalityDiagram, "_margin_line",
                        lambda self, p, x0, x1, *a: (margins.append((x0, x1)),
                                                     margin(self, p, x0, x1, *a))[1])
    d = _diagram(1200)
    seen = set()
    for _, spec, example in _CASES:
        for leg in _legs(spec, example):
            _show(d, leg, spec, example)
            for pn in d.panels:
                lines.clear()
                margins.clear()
                d._spots.clear()
                d._frame = (0.0, d.width())
                _paint_unseen(d, pn)
                (m0, m1), = margins
                end = lines[len(pn.whole.chains[0].spans) - 1][1]
                assert m1 == pytest.approx(end, abs=1e-6), leg
                assert (m1 < m0) == (pn.timeline.margin_ns < 0), leg
                seen.add(pn.timeline.margin_ns < 0)
    assert seen == {True, False}


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_every_vertical_guide_drops_from_a_point_drawn_on_a_waveform(request, name, spec,
                                                                     example):
    """Every guide a whole-inequality panel draws is vertical and starts at a dot drawn on a
    waveform; a chain point on no drawn ramp, such as a joint of the chain inside it, has no
    guide. On inequality 1) with the default inputs both panels hold such a point."""
    from swi3s_studio.ui.inequality_diagram import _ends
    from swi3s_studio.ui.timing_diagram import _Unseen
    _scoped(request, name)
    d = _diagram(1200)

    def off_rows(pn, e):
        return e.ramp is None or not any(r.key == e.ramp for row in pn.rows for r in row)

    unguided = 0
    for leg in _legs(spec, example):
        _show(d, leg, spec, example)
        for pn in d.panels:
            d._spots.clear()
            d._frame = (0.0, d.width())
            d._paint_panel(_Unseen(), pn, 0.0, 200.0)
            ink = d._ink
            centres = [r.center() for r in ink.dots]
            for a, b, _pen in ink.guides:
                assert a.x() == pytest.approx(b.x()), (leg, a, b)
                assert any(abs(a.x() - c.x()) < 1e-6 and abs(a.y() - c.y()) < 1e-6
                           for c in centres), (leg, a)
            ends = [pn.timeline.event(k) for k in _ends(pn.whole)]
            unguided += sum(off_rows(pn, e) for e in ends)
            assert len(ink.guides) == len(ends) - sum(off_rows(pn, e) for e in ends)
    if name == "default":
        assert unguided > 0
        _show(d, "MP_setup", spec, example)
        for pn in d.panels:
            ends = [pn.timeline.event(k) for k in _ends(pn.whole)]
            assert any(off_rows(pn, e) for e in ends), pn.title


def test_no_band_is_filled_through_the_rows():
    """A panel fills nothing: no band of the margin's colour runs through the rows, on a leg
    with a gap or a short margin, a contention leg or a keeper leg."""
    from swi3s_studio.ui.timing_diagram import _Unseen

    class Fills(_Unseen):
        def __init__(self):
            super().__init__()
            self.fills = []

        def fillRect(self, *a):        # noqa: N802 - QPainter's name
            self.fills.append(a)

    d = _diagram(1200)
    for leg, inp in (("MP_setup", CalcInputs()), ("MP_hold", CalcInputs()),
                     ("PM_contention", CalcInputs(handover_UIs=0.0)),
                     ("keeper_Man", CalcInputs(Man_ede=True, t_keeper_ns=8.0))):
        _show(d, leg, inp, inp)
        for pn in d.panels:
            p = Fills()
            d._spots.clear()
            d._frame = (0.0, d.width())
            d._paint_panel(p, pn, 0.0, 200.0)
            assert not p.fills, (leg, p.fills)


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_side_by_side_panels_share_one_height_and_their_axes_line_up(monkeypatch, request,
                                                                      name, spec, example):
    """Side by side, the two columns' panels of every leg start their rows, draw their chains
    and their time axis at one height and end at one height, also where one column's labels
    take a tier more than the other's (MP_contention on the EDE case, among others) and at a
    taller widget."""
    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    _scoped(request, name)
    axes, chains = [], []
    real_axis, real_chains = InequalityDiagram._paint_axis, InequalityDiagram._paint_chains
    monkeypatch.setattr(InequalityDiagram, "_paint_axis", lambda self, p, pn, y, *a, **k: (
        axes.append(y), real_axis(self, p, pn, y, *a, **k))[-1])
    monkeypatch.setattr(InequalityDiagram, "_paint_chains", lambda self, p, pn, lane_y, *a: (
        chains.append(tuple(lane_y)), real_chains(self, p, pn, lane_y, *a))[-1])
    d = _diagram(1580)
    d.set_side_by_side(True)
    tiers = set()
    for leg in _legs(spec, example):
        _show(d, leg, spec, example)
        for tall in (0, 1400):
            d.resize(1580, max(d.minimumHeight(), tall))
            axes.clear()
            chains.clear()
            d._spots.clear()
            d.grab()
            assert len(axes) == 2 and len(chains) == 2, leg
            assert axes[0] == pytest.approx(axes[1]), (leg, tall, axes)
            assert chains[0] == pytest.approx(chains[1]), (leg, tall, chains)
            heights = []
            for (frame, col) in d._each_column():
                pn = col[0]
                heights.append(d._head_height(pn) + d._body_height(pn, d._row_h()))
                tiers.add((leg, d._tiers(pn)))
            assert heights[0] == pytest.approx(heights[1]), (leg, tall, heights)
    if name == "EDE":
        own = {t for leg, t in tiers if leg == "MP_contention"}
        assert len(own) == 2, own


def _paint_unseen(d, pn):
    """`pn` painted onto a painter that draws nothing, at a row of 200 px."""
    from swi3s_studio.ui.timing_diagram import _Unseen
    d._paint_panel(_Unseen(), pn, 0.0, 200.0)


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_each_row_draws_its_receivers_rails(monkeypatch, request, name, spec, example):
    """Each row draws two lines across the plot, its receiver's GND and V_DD, labelled with
    the device that receives there, or where no threshold is read on it the device at its
    pin: the device at its pin on every row, the Manager's on its pins, a Peripheral's on its
    own, A's and B's on P→P, but keeper_Per's data at the Peripheral, where the keeper's
    threshold is read, the Manager's."""
    from test_timing_diagram_layout import _widget_paint

    from swi3s_studio.ui.inequality_diagram import InequalityDiagram
    rows = []
    real = InequalityDiagram._paint_rails

    def spy(self, p, row, scale, level_y):
        before = len(self._ink._segs)
        labels = real(self, p, row, scale, level_y)
        if _widget_paint(p):
            rows.append((row[0].pin, [lab.text for lab in labels], self._ink._segs[before:],
                         self._plot_x()))
        return labels

    monkeypatch.setattr(InequalityDiagram, "_paint_rails", spy)
    want = {"Peripheral": "Per", "Manager": "Man", "Peripheral A": "Per A",
            "Peripheral B": "Per B"}
    _scoped(request, name)
    d = _diagram(900)
    for leg in _legs(spec, example):
        rows.clear()
        _show(d, leg, spec, example)
        d._spots.clear()
        d.grab()
        assert len(rows) == sum(len(pn.rows) for pn in d.panels), leg
        for pin, (name, v_dd, gnd), segs, (left, right) in rows:
            assert name.endswith(f"@ {pin}")
            # keeper_Per's data at the Peripheral holds the keeper's threshold: the Manager's.
            dev = "Man" if (leg, name) == ("keeper_Per", "Data @ Peripheral") else want[pin]
            assert v_dd.startswith(f"{dev} V_DD") and gnd.startswith(f"{dev} GND"), (leg, name)
            assert len(segs) == 2 and all(s[0] == left and s[2] == right and s[1] == s[3]
                                          for s in segs), (leg, pin)


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_a_level_line_is_drawn_at_its_points_edge_alone(monkeypatch, request, name, spec,
                                                        example):
    """Each chain point on a drawn ramp gets its level as one horizontal line, short of the
    plot's width, through the point and within its edge's ramp (or a line height each side
    of it): one line per edge and level, the lines at one height not meeting."""
    from swi3s_studio.ui import inequality_diagram as idg
    from swi3s_studio.ui.timing_diagram import _settle_ns
    calls = []
    real = idg.InequalityDiagram._paint_levels

    def spy(self, p, pn, event_y):
        segs = []
        seg = self._seg
        self._seg = lambda p_, a, b, **k: (segs.append((a.x(), a.y(), b.x(), b.y())),
                                           seg(p_, a, b, **k))
        try:
            out = real(self, p, pn, event_y)
        finally:
            del self._seg
        calls.append((pn, segs, event_y))
        return out

    _scoped(request, name)
    monkeypatch.setattr(idg.InequalityDiagram, "_paint_levels", spy)
    d = _diagram(1200)
    lh = d._lh()
    for leg in _legs(spec, example):
        calls.clear()
        _show(d, leg, spec, example)
        d._spots.clear()
        d.grab()
        left, right = d._plot_x()
        for pn, segs, event_y in calls:
            tl = pn.timeline
            points = {}
            for k in idg._ends(pn.whole):
                e = tl.event(k)
                y = event_y(e)
                if y is not None and e.kind in ("reference", "anchor", "valid",
                                                "detection", "crossing"):
                    ly = y if e.kind in ("reference", "anchor", "valid") else None
                    points.setdefault((e.ramp, ly), e)
            assert len(segs) >= 1
            for x0, y0, x1, y1 in segs:
                assert y0 == y1 and x1 - x0 < right - left - 1.0, (leg, segs)
            by_row = {}
            for x0, y, x1, _ in segs:
                by_row.setdefault(round(y, 6), []).append((x0, x1))
            for e in {id(e): e for e in points.values()}.values():
                r = tl.ramp(e.ramp)
                x = d.x_of(pn, e.t_ns)
                lo = min(d.x_of(pn, r.start_ns) - idg._EDGE_PAD * lh, x - lh) - 1e-6
                hi = max(d.x_of(pn, r.start_ns + _settle_ns(r)) + idg._EDGE_PAD * lh,
                         x + lh) + 1e-6
                assert any(max(lo, left - 1e-6) <= x0 <= x <= x1 <= min(hi, right + 1e-6)
                           for x0, _, x1, _ in segs), (leg, e.key)
            assert len(segs) == len({(e.ramp, round(event_y(e), 6))
                                     for e in points.values()}), (leg, segs)
            for spans in by_row.values():
                spans.sort()
                assert all(a[1] < b[0] for a, b in zip(spans, spans[1:])), (leg, spans)


def test_the_view_exports_and_covers_every_leg():
    """PNG at the size times the scale, SVG holding the margin's text, a one-page PDF, and on
    the dark palette the light one; every leg at every case is drawn whole and exports, and
    so is a negative setup time, drawn back."""
    import xml.etree.ElementTree as ET

    from PySide6.QtGui import QImage

    from swi3s_studio.ui.theme import VizTheme, apply_palette
    d = _diagram(900)
    _show(d, "PM_setup", CalcInputs(), _PROP)
    assert d.can_export()
    img = QImage.fromData(d.export("png", 2.0))
    assert (img.width(), img.height()) == (2 * d.width(), 2 * d.height())
    root = ET.fromstring(d.export("svg"))
    text = " ".join(t for el in root.iter() if el.tag.endswith("text") for t in el.itertext())
    assert "margin" in text and "Clk @ Manager" in text and "Data @ Peripheral" in text
    assert d.export("pdf").startswith(b"%PDF")
    before = VizTheme.MODE
    try:
        apply_palette("dark")
        lit = QImage.fromData(d.export("png", 1.0, light=True))
        assert VizTheme.MODE == "dark" and not lit.isNull()
    finally:
        apply_palette(before)
    for _, spec, example in (*_CASES, ("negative", CalcInputs(Per_t_IS_max_ns=-1.0),
                                       CalcInputs(Per_t_IS_max_ns=-1.0))):
        for leg in _legs(spec, example):
            d.show_inequality(f"2) {leg}", _sections(spec, example, leg))
            assert d.can_export() and len(d.panels) == 2 and not d.refused, leg
    d.show_term("1) MP_setup", "Man_t_DD", _sections(CalcInputs(), CalcInputs(), "MP_setup"))
    assert not d.whole and not d.refused and d.can_export()


# ---- the page -----------------------------------------------------------------------------


def _view():
    from PySide6.QtWidgets import QApplication
    _app()
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.resize(1800, 1100)
    tv.show()
    tv.flush_recompute()
    QApplication.processEvents()
    return tv


def _row(tv, leg, heading=False):
    """The tree's heading of `leg`'s group (`heading`), or `leg`'s first equation row."""
    from swi3s_studio.ui.timing_view import _WHOLE_ROLE
    tree = tv._tree
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        if heading and head.data(0, _WHOLE_ROLE) == leg:
            return head
        for j in range(head.childCount()):
            item = head.child(j)
            if not heading and item.data(0, _WHOLE_ROLE) == leg:
                return item
    raise AssertionError(leg)


def _point(tv, item, on_term):
    """A point on `item`'s text: on a term with `on_term`, else off every term."""
    from PySide6.QtCore import QPoint
    tree = tv._tree
    rect = tree.visualRect(tree.indexFromItem(item, 0))
    for x in range(rect.left(), rect.right(), 2):
        pos = QPoint(x, rect.center().y())
        key, on_text = tree._hit(pos)
        if on_text and bool(key) == on_term:
            return pos
    raise AssertionError(item.text(0))


def _click(tv, pos):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.mouseClick(tv._tree.viewport(), Qt.LeftButton, pos=pos)


def test_a_click_on_a_heading_or_an_equation_draws_the_inequality_whole():
    """A click on a heading's text draws its first inequality whole in the opened pane; one
    on another equation row off its terms draws that one; a second click on it closes the
    pane; a click on a term draws the term; a click in blank space closes a whole view. The
    export actions follow, and the offered name is the leg's."""
    from swi3s_studio.ui.timing_view import _export_name
    tv = _view()
    d = tv._diagram
    plain, _light = tv.diagram_export_actions()
    # The page opens on 1) drawn whole; a click on its heading closes it, a second draws it.
    assert tv._diagram_whole and tv._diagram_leg == "MP_setup"
    _click(tv, _point(tv, _row(tv, "MP_setup", heading=True), False))
    assert tv._diagram_pane.isHidden() and not tv._diagram_whole
    _click(tv, _point(tv, _row(tv, "MP_setup", heading=True), False))
    assert not tv._diagram_pane.isHidden() and tv._diagram_whole
    assert d.whole and len(d.panels) == 2 and tv._diagram_leg == "MP_setup"
    assert d.leg_title == "1) Manager Setup for Peripheral, t_DD" and plain.isEnabled()
    assert _export_name(tv._diagram_leg, "inequality") == "MP_setup_inequality"
    _click(tv, _point(tv, _row(tv, "PM_hold"), False))
    assert tv._diagram_leg == "PM_hold" and d.whole and d.panels[0].whole.checked_at == "Manager"
    _click(tv, _point(tv, _row(tv, "PM_hold"), False))
    assert tv._diagram_pane.isHidden() and not tv._diagram_whole
    _click(tv, _point(tv, _row(tv, "PP_setup"), False))
    assert d.whole and tv._diagram_leg == "PP_setup"
    _click(tv, _point(tv, _row(tv, "PP_setup"), True))
    assert not d.whole and tv._diagram_term and tv._diagram_leg == "PP_setup"
    assert d.panels and d.term == tv._diagram_term
    tv.set_diagram_maximized(True)
    assert tv.is_diagram_maximized()
    tv.set_diagram_maximized(False)
    _click(tv, _point(tv, _row(tv, "MP_hold", heading=True), False))
    assert d.whole and tv._diagram_leg == "MP_hold" and tv._hl
    tv.set_diagram_maximized(True)
    assert tv.is_diagram_maximized() and d.side_by_side
    tv.set_diagram_maximized(False)
    tv._on_cleared()
    assert tv._diagram_pane.isHidden() and not tv._hl and not plain.isEnabled()
    tv.close()


def test_the_whole_inequality_title_is_its_number_and_name():
    """Drawn whole, every inequality is titled with the number its equation row starts with
    and its name: the text of the heading it is under and, for the setup and hold legs that
    share a heading, the launch (t_DD, or t_ZD for the handover leg) after a comma; a leg with
    a heading to itself takes no suffix. So is the pane while the result is computed."""
    import re

    from swi3s_studio.timing.calculator import INEQUALITIES
    tv = _view()
    d = tv._diagram

    def plain(item):
        return re.sub(r"<[^>]+>", "", item.text(0))

    for leg in INEQUALITIES:
        row = _row(tv, leg)
        number = plain(row).split(")")[0]
        launch = ""
        if leg.startswith(("MP_setup", "MP_hold", "PM_setup", "PM_hold", "PP_setup", "PP_hold")):
            launch = ", t_ZD" if leg.endswith("_ho") else ", t_DD"
        expected = f"{number}) {plain(row.parent())}{launch}"
        tv._draw_whole(leg)
        assert d.whole and d.leg_title == expected, (leg, d.leg_title)
    assert d.leg_title == "17) Bus Keeper — Peripheral Releasing"
    tv._draw_whole("PP_hold_ho")
    assert d.leg_title == "12) Peripheral A Holding for Peripheral B, t_ZD"
    tv._worst = {}
    tv._draw_whole("MP_setup_ho")
    assert d.leg_title == "2) Manager Setup for Peripheral, t_ZD"
    tv.close()


_LEG_NAMES = [
    "Manager Setup for Peripheral, t_DD", "Manager Setup for Peripheral, t_ZD",
    "Manager Holding for Peripheral, t_DD", "Manager Holding for Peripheral, t_ZD",
    "Peripheral Setup for Manager, t_DD", "Peripheral Setup for Manager, t_ZD",
    "Peripheral Holding for Manager, t_DD", "Peripheral Holding for Manager, t_ZD",
    "Peripheral A Setup for Peripheral B, t_DD", "Peripheral A Setup for Peripheral B, t_ZD",
    "Peripheral A Holding for Peripheral B, t_DD", "Peripheral A Holding for Peripheral B, t_ZD",
    "Handover Contention — Manager to Peripheral",
    "Handover Contention — Peripheral to Manager",
    "Handover Contention — Peripheral to Peripheral",
    "Bus Keeper — Manager Releasing", "Bus Keeper — Peripheral Releasing"]


def test_a_single_leg_is_named_by_its_heading_and_its_launch_everywhere():
    """Every place that names one leg — the diagram's title (whole and for a term), the
    copied summary and Export Results — gives "<heading>, t_DD" or "<heading>, t_ZD" for the
    setup and hold legs and the plain heading for a contention or keeper leg; the tree's
    headings, each over both launches, keep the plain title; the title draws t_DD with a
    subscript."""
    import re

    from swi3s_studio.timing.calculator import INEQUALITIES
    from swi3s_studio.ui.timing_view import _ascii
    tv = _view()
    d = tv._diagram
    assert len(_LEG_NAMES) == len(INEQUALITIES) == 17
    for n, (leg, name) in enumerate(zip(INEQUALITIES, _LEG_NAMES), start=1):
        tv._draw_whole(leg)
        assert d.leg_title == f"{n}) {name}", leg
        assert "handover" not in d.leg_title.lower().replace("handover contention", "")
    summary = tv.summary_text().split("\n\n")[0].splitlines()[1:]
    assert [ln.split(" | ")[0] for ln in summary] == [
        f"{n}) {name}" for n, name in enumerate(_LEG_NAMES, start=1)]
    rows = tv.results_rows()
    header = next(i for i, r in enumerate(rows) if r and r[0] == "Number")
    assert [r[1] for r in rows[header + 1::2]] == [_ascii(name) for name in _LEG_NAMES]
    heads = [re.sub(r"<[^>]+>", "", tv._tree.topLevelItem(i).text(0))
             for i in range(tv._tree.topLevelItemCount())]
    assert "Manager Setup for Peripheral" in heads
    assert not [h for h in heads if h.endswith(("t_DD", "t_ZD"))]
    # A term's diagram is titled with the same name.
    _click(tv, _point(tv, _row(tv, "PM_hold_ho"), True))
    assert not d.whole and d.leg_title == "8) Peripheral Holding for Manager, t_ZD"
    # Drawn, the launch is a subscript; exported, it is the plain t_ZD.
    assert "t<sub>ZD</sub>" in d._title_text().text()
    tv.close()


def test_every_leg_s_equation_row_draws_it_whole_on_the_page():
    """A click on every inequality's equation row off its terms opens the pane with it drawn
    whole, a panel per column, and the export actions enabled."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    tv = _view()
    plain, _light = tv.diagram_export_actions()
    tv._on_cleared()                      # the page opens on 1) drawn whole
    for leg in INEQUALITIES:
        _click(tv, _point(tv, _row(tv, leg), False))
        d = tv._diagram
        assert not tv._diagram_pane.isHidden() and tv._diagram_leg == leg and d.whole, leg
        assert not d.refused and len(d.panels) == 2 and plain.isEnabled(), leg
        _click(tv, _point(tv, _row(tv, leg), False))
        assert tv._diagram_pane.isHidden(), leg
    tv.close()


def test_the_title_wraps_clear_of_the_corner_widget_and_the_panels_start_under_it():
    """The leg's title wraps at the width left of the corner widget, or below the widget at the
    full width where beside it it would take more lines, so it is neither cut off nor drawn
    under the widget, and the panels start under its last line; on one line where it fits."""
    from swi3s_studio.ui import timing_diagram as td
    from swi3s_studio.ui.timing_view import _diagram_sections
    tv = _view()
    d = tv._diagram
    lh = d._lh()
    from PySide6.QtWidgets import QApplication
    _title, sections = _diagram_sections("MP_setup", tv._worst, tv._worst_picks,
                                         tv._column_labels())
    d.show_inequality("1) Manager Setup for Peripheral, with words enough to wrap at the "
                      "pane's narrowest width", sections)
    tv._splitter.setSizes([10000, 1])                  # the pane at its minimum width
    QApplication.processEvents()
    assert tv._diagram_pane.width() == tv._diagram_min_width()
    one = d._static("X", d._title_font(), -1.0).size().height()
    title = d._title_text()
    assert title.size().height() > 1.5 * one, d.leg_title
    assert title.size().width() <= d._title_w()
    corner_bottom = d._corner.y() + d._corner.height()
    if d._title_below():
        assert d._title_top() >= corner_bottom
        assert 0.6 * lh + d._title_w() <= d.width()
    else:
        assert 0.6 * lh + d._title_w() + d._corner.width() <= d.width()
    assert d._title_h() == pytest.approx(td._TITLE_H * lh + title.size().height() - one
                                         + d._title_drop())
    narrow = d._fixed_height(d.panels)
    tv._splitter.setSizes([1, 10000])
    QApplication.processEvents()
    assert d.width() > 1000
    assert not d._title_below()
    assert d._title_h() == td._TITLE_H * lh
    assert narrow - d._fixed_height(d.panels) >= title.size().height() - one - 1e-6
    tv.close()


# A release in the middle of its ramp, before the keeper has followed: keeper legs short.
_MID_RAMP = ("released mid-ramp",
             CalcInputs(Per_t_DZ_max_ns=0.0, Man_t_DZ_max_ns=0.0, t_keeper_ns=5.0,
                        Per_t_DD_max_ns=25.0, Man_t_DD_max_ns=28.0),
             CalcInputs(Per_t_DZ_max_ns=0.0, Man_t_DZ_max_ns=0.0, t_keeper_ns=8.0,
                        Per_t_DD_max_ns=27.0, Man_t_DD_max_ns=30.0))


@pytest.mark.parametrize("name,spec,example",
                         [*_case_params(), pytest.param(*_MID_RAMP, id=_MID_RAMP[0])])
def test_every_dot_sits_on_a_drawn_line(request, name, spec, example):
    """Every dot a whole-inequality panel draws lies on a line it draws: a point past its
    driver's high-Z, such as the keeper having followed after a release in the middle of its
    ramp, on the level the wire holds there (`Ramp.wire`), not on the ramp continued."""
    if name != _MID_RAMP[0]:
        _scoped(request, name)
    from swi3s_studio.ui.timing_diagram import _Unseen
    d = _diagram(1200)
    for leg in _legs(spec, example):
        _show(d, leg, spec, example)
        for pn in d.panels:
            d._spots.clear()
            d._frame = (0.0, d.width())
            d._paint_panel(_Unseen(), pn, 0.0, 200.0)
            segs = d._ink._segs
            for dot in d._ink.dots:
                c = dot.center()
                assert any(_on_seg(c.x(), c.y(), *s) for s in segs), (leg, pn.title, c)


def _on_seg(x, y, x0, y0, x1, y1, tol=0.75):
    """Whether (x, y) lies within `tol` px of the segment from (x0, y0) to (x1, y1)."""
    dx, dy = x1 - x0, y1 - y0
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / n))
    return (x0 + t * dx - x) ** 2 + (y0 + t * dy - y) ** 2 <= tol * tol


def _clock_refs(tl, row):
    """A clock row's edges as (20 % point, rising, ramp), in time order."""
    return sorted(((r.start_ns + tl.clock_ref_frac[r.rising] * r.t_rf_ns, r.rising, r)
                   for r in row), key=lambda e: e[0])


def _assert_physical_clock(tl, rows, window, what):
    """Each clock row in `rows` runs as a clock across `window`: edges one UI apart and
    alternating, with no edge before its first or after its last that the window would
    draw; and two pins' clock rows are one edge sequence, the second shifted by the clock's
    flight between them: every edge one pin draws whose shifted twin the window holds whole
    is drawn at the other pin too."""
    from swi3s_studio.ui.timing_diagram import _settle_ns
    ui = tl.ui_ns
    lo, hi = window
    clocks = [row for row in rows if row[0].signal == "clock"]
    for row in clocks:
        es = _clock_refs(tl, row)
        for (a, ra, _), (b, rb, _) in zip(es, es[1:]):
            assert abs(b - a - ui) < 1e-6 and ra != rb, (what, row[0].pin, a, b)
        for (ref, rising, r), step in ((es[0], -1), (es[-1], 1)):
            twin = ref + step * ui
            start = twin - tl.clock_ref_frac[not rising] * r.t_rf_ns
            assert not (start < hi and start + _settle_ns(r) > lo), \
                (what, row[0].pin, "holds a level through an edge at", twin)
    for i, p in enumerate(clocks):
        for q in clocks[i + 1:]:
            ep, eq = _clock_refs(tl, p), _clock_refs(tl, q)
            d = min((b - a for a, ra, _ in ep for b, rb, _ in eq if ra == rb),
                    key=abs)
            for src, dst, sign in ((ep, eq, 1), (eq, ep, -1)):
                for ref, rising, r in src:
                    t = ref + sign * d
                    start = t - tl.clock_ref_frac[rising] * r.t_rf_ns
                    if lo < start and start + _settle_ns(r) < hi:
                        assert any(abs(t - u) < 1e-6 and rising == ru for u, ru, _ in dst), \
                            (what, src[0][2].pin, "edge at", ref, "missing at the other pin")


@pytest.mark.parametrize("name,spec,example", _case_params())
def test_every_pin_draws_the_clock_as_it_runs_there(request, name, spec, example):
    """At every pin a panel draws the clock at, the clock runs: every edge it makes in the
    panel's window is drawn, one UI apart and alternating, and at two pins the edges are one
    sequence, shifted by the clock's flight between them, so no pin holds a level through an
    edge the other pin makes. Pinned for the whole-inequality view of every leg and the term
    diagram of every term."""
    _scoped(request, name)
    from swi3s_studio.ui import timing_diagram as td
    from swi3s_studio.ui.timing_view import _term_key
    d = _diagram(1200)
    for leg in _legs(spec, example):
        _show(d, leg, spec, example)
        for pn in d.panels:
            _assert_physical_clock(pn.timeline, pn.rows, pn.window, (leg, pn.title))
        sections = _sections(spec, example, leg)
        keys = {_term_key(t.symbol) for s in sections for t in s.terms}
        for key in sorted(keys):
            for pn in td.panels(sections, key, _term_key):
                if pn.timeline is not None and pn.rows:
                    _assert_physical_clock(pn.timeline, pn.rows, pn.window,
                                           (leg, key, pn.title))


def test_a_views_own_note_takes_the_place_of_the_legs_in_the_footnote():
    """PM_contention, drawn whole at the Peripheral alone, and PP_contention, drawn at A
    alone, each say so in the footnote in place of the leg's note that the flights net to
    zero, which the term diagram's corner line keeps."""
    from swi3s_studio.timing.timeline import NET_ZERO_NOTE, PM_ONE_PIN_NOTE, PP_ONE_PIN_NOTE
    from swi3s_studio.ui.inequality_diagram import footnote
    for leg, says, not_says in (("PM_contention", PM_ONE_PIN_NOTE, NET_ZERO_NOTE),
                                ("PP_contention", PP_ONE_PIN_NOTE, NET_ZERO_NOTE)):
        secs = _sections(CalcInputs(), _PROP, leg)
        for s in secs:
            s.corner = f"Corner: rows. {s.timeline.note}"
        text = footnote(secs)
        assert says in text and not_says not in text, (leg, text)
        assert text.count("Corner") == 0 and text.endswith("."), text


# ---- stepping through the inequalities while the diagram has the page ---------------------


def _maximized_on(leg):
    """A page with `leg` drawn whole and the diagram given the page."""
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv._draw_whole(leg)
    tv.set_diagram_maximized(True)
    QApplication.processEvents()
    assert tv.is_diagram_maximized() and tv._diagram_leg == leg
    return tv


def _tree_order(tv):
    """The inequalities in the order the tree lists them, top to bottom."""
    from swi3s_studio.ui.timing_view import _WHOLE_ROLE
    tree, out = tv._tree, []
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for j in range(head.childCount()):
            leg = head.child(j).data(0, _WHOLE_ROLE)
            if leg and leg not in out:
                out.append(leg)
    return out


def test_next_and_previous_draw_the_neighbouring_inequality_and_disable_at_the_ends():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    tv = _maximized_on("MP_setup")
    d, n = tv._diagram, len(_tree_order(tv))
    assert n == 17 and tv._nav_prev.isVisibleTo(tv) and tv._nav_next.isVisibleTo(tv)
    assert not tv._nav_prev.isEnabled() and tv._nav_next.isEnabled()
    QTest.mouseClick(tv._nav_next, Qt.LeftButton)
    assert tv._diagram_leg == "MP_setup_ho" and tv._diagram_whole and d.whole
    assert d.leg_title == "2) Manager Setup for Peripheral, t_ZD"
    assert tv._nav_prev.isEnabled()
    QTest.mouseClick(tv._nav_prev, Qt.LeftButton)
    assert tv._diagram_leg == "MP_setup" and d.leg_title.startswith("1) ")
    tv.step_inequality(-1)                                   # the first: nothing before it
    assert tv._diagram_leg == "MP_setup" and not tv._nav_prev.isEnabled()
    last = _tree_order(tv)[-1]
    tv._draw_whole(last)
    assert d.leg_title == "17) Bus Keeper — Peripheral Releasing"
    assert not tv._nav_next.isEnabled() and tv._nav_prev.isEnabled()
    tv.step_inequality(+1)                                   # the last: nothing after it
    assert tv._diagram_leg == last and tv.is_diagram_maximized()
    tv.close()


def test_the_arrows_are_corner_buttons_built_and_styled_as_the_maximize_button():
    """Previous and Next are tool buttons in the diagram's corner row, directly left of the
    ⤢, of its class and size, filled at rest as the ⤢ is while the diagram has the page, in
    either palette; they show only while the diagram has the page; there is no header row or
    label for the position."""
    from PySide6.QtWidgets import QApplication, QToolButton

    from swi3s_studio.ui.theme import VizTheme, apply_palette
    tv = _view()
    prev, nxt, big = tv._nav_prev, tv._nav_next, tv._diagram_max_btn
    assert not hasattr(tv, "_diagram_nav") and not hasattr(tv, "_nav_label")
    assert not prev.isVisibleTo(tv) and not nxt.isVisibleTo(tv)      # only while maximized
    tv.set_diagram_maximized(True)
    QApplication.processEvents()
    corner = big.parentWidget()
    assert type(prev) is type(nxt) is type(big) is QToolButton
    assert prev.parentWidget() is corner and nxt.parentWidget() is corner
    assert corner.parentWidget() is tv._diagram
    lay = corner.layout()
    at = lay.indexOf(big)
    assert (lay.indexOf(prev), lay.indexOf(nxt)) == (at - 2, at - 1)
    assert prev.isVisibleTo(tv) and nxt.isVisibleTo(tv)
    assert prev.x() < nxt.x() < big.x() and nxt.geometry().right() < big.x()
    assert prev.y() == nxt.y() == big.y() and prev.height() == nxt.height()
    assert abs(prev.height() - big.height()) <= 2           # the glyph's font sets the height
    assert prev.width() == nxt.width() and abs(prev.width() - big.width()) <= 8
    assert prev.toolTip() == "Previous inequality (←)" and nxt.toolTip() == "Next inequality (→)"
    assert prev.focusPolicy() == nxt.focusPolicy() == big.focusPolicy()
    assert ":disabled" in prev.styleSheet()
    before = VizTheme.MODE
    try:
        for mode in ("dark", "light"):
            apply_palette(mode)
            tv.retheme()
            assert prev.styleSheet() == nxt.styleSheet(), mode
            base = prev.styleSheet().split("}")[0]               # the arrows' resting rule
            # Filled at rest as the ⤢ is while checked: its fill, glyph colour and corners.
            for part in (f"background:{VizTheme.BORDER}", f"color:{VizTheme.TEXT};",
                         f"border-radius:{VizTheme.CORNER_RADIUS}px"):
                assert part in base and part in big.styleSheet(), (mode, part)
    finally:
        apply_palette(before)
        tv.retheme()
    tv.set_diagram_maximized(False)
    QApplication.processEvents()
    assert not prev.isVisibleTo(tv) and not nxt.isVisibleTo(tv)
    tv.close()


def test_stepping_follows_the_trees_order_in_either_column_choice():
    from swi3s_studio.ui.timing_view import _INEQ_NUMBER
    tv = _maximized_on("MP_setup")
    order = _tree_order(tv)
    assert [_INEQ_NUMBER[leg] for leg in order] == list(range(1, 18))
    for choice in ("proposed", "both"):
        tv._set_diagram_show(choice)
        tv._draw_whole(order[0])
        seen = [tv._diagram_leg]
        for _ in range(len(order) - 1):
            tv.step_inequality(+1)
            seen.append(tv._diagram_leg)
        assert seen == order, choice
        assert tv._diagram.shown_column == {"proposed": 1}.get(choice)
        for _ in range(len(order) - 1):
            tv.step_inequality(-1)
        assert tv._diagram_leg == order[0]
    tv.close()


def test_stepping_clears_a_highlighted_term_and_leaves_the_whole_drawing():
    tv = _view()
    _click(tv, _point(tv, _row(tv, "PP_setup"), True))
    key = tv._diagram_term
    assert key and key in tv._hl and tv._diagram_leg == "PP_setup"
    tv.set_diagram_maximized(True)
    assert tv.is_diagram_maximized() and tv._diagram_leg == "PP_setup"
    tv.step_inequality(+1)
    assert tv.is_diagram_maximized() and tv._diagram_leg == "PP_setup_ho"
    assert tv._diagram_whole and tv._diagram_term == "" and tv._diagram.whole
    assert not tv._hl and key not in tv._hl
    tv.close()


def test_restoring_the_diagram_shows_the_inequality_stepped_to_selected():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_view import _whole_marked
    tv = _view()
    tv.resize(1800, 500)
    QApplication.processEvents()
    tv._draw_whole("MP_setup")
    tv.set_diagram_maximized(True)
    for _ in range(13):
        tv.step_inequality(+1)
    assert tv._diagram_leg == "PM_contention"
    tv.set_diagram_maximized(False)
    QApplication.processEvents()
    QApplication.processEvents()
    assert not tv.is_diagram_maximized() and not tv._nav_prev.isVisibleTo(tv)
    assert tv._diagram_leg == "PM_contention" and tv._whole_drawn() == "PM_contention"
    head = _row(tv, "PM_contention", heading=True)
    assert head.text(0) == _whole_marked("PM_contention",
                                         "Handover Contention — Peripheral to Manager")
    scroll = tv._ineq_scroll
    assert scroll.verticalScrollBar().value() > 0
    spot = tv._tree.mapTo(scroll.viewport(), tv._tree.visualItemRect(head).topLeft())
    assert 0 <= spot.y() < scroll.viewport().height()
    tv.close()


def test_restoring_a_stepped_diagram_calls_back_only_a_view_still_alive(monkeypatch):
    """Restoring a diagram that stepped reveals the inequality drawn on the next turn of the
    event loop, on a one-shot owned by the view (`timers.after`): a view deleted before then
    is not called back, one still alive is, once."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid

    from swi3s_studio.ui.timing_view import TimingView
    called = []
    monkeypatch.setattr(TimingView, "_reveal_drawn", lambda self: called.append(isValid(self)))
    gone = _maximized_on("MP_setup")
    gone.step_inequality(+1)
    gone.set_diagram_maximized(False)
    gone.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not isValid(gone)
    alive = _maximized_on("MP_setup")
    alive.step_inequality(+1)
    alive.set_diagram_maximized(False)
    for _ in range(3):
        QApplication.processEvents()
    assert called == [True]
    alive.close()


def test_the_arrow_keys_step_only_while_the_diagram_has_the_page():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv.activateWindow()
    QApplication.processEvents()
    tv._draw_whole("MP_hold")
    QTest.keyClick(tv, Qt.Key_Right)
    assert tv._diagram_leg == "MP_hold" and not tv._nav_next.isVisibleTo(tv)
    tv.set_diagram_maximized(True)
    QTest.keyClick(tv, Qt.Key_Right)
    assert tv._diagram_leg == "MP_hold_ho"
    QTest.keyClick(tv, Qt.Key_Left)
    QTest.keyClick(tv, Qt.Key_Left)
    assert tv._diagram_leg == "MP_setup_ho"
    QTest.keyClick(tv, Qt.Key_Right, Qt.ShiftModifier)          # modified: not ours
    assert tv._diagram_leg == "MP_setup_ho"
    tv.set_diagram_maximized(False)
    QTest.keyClick(tv, Qt.Key_Right)
    assert tv._diagram_leg == "MP_setup_ho"
    # The view's own inequality tree keeps its arrow keys: nothing takes them from it.
    tree, got = tv._tree, []
    tree.keyPressEvent = lambda event: got.append(event.key())      # what the tree receives
    tree.setFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() in (tree, tree.viewport())
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Right)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Left)
    assert got == [Qt.Key_Right, Qt.Key_Left] and tv._diagram_leg == "MP_setup_ho"
    tv.close()


def _beside_a_tree():
    """A window holding a tree of two expandable rows beside a maximized page drawn at
    MP_hold, the window active."""
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QWidget
    _app()
    from swi3s_studio.ui.timing_view import TimingView
    host = QWidget()
    row = QHBoxLayout(host)
    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    for name in ("a", "b"):
        QTreeWidgetItem(QTreeWidgetItem(tree, [name]), ["child"])
    tv = TimingView()
    row.addWidget(tree)
    row.addWidget(tv, 1)
    host.resize(1800, 1100)
    host.show()
    tv.flush_recompute()
    tv._draw_whole("MP_hold")
    tv.set_diagram_maximized(True)
    host.activateWindow()
    QApplication.processEvents()
    return host, tree, tv


def test_a_focused_tree_outside_the_view_keeps_its_arrow_keys():
    """While the diagram has the page, Right and Left on a tree elsewhere in the window
    expand and collapse its row, and the page does not step."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    host, tree, tv = _beside_a_tree()
    top = tree.topLevelItem(0)
    tree.setFocus()
    tree.setCurrentItem(top)
    QApplication.processEvents()
    assert QApplication.focusWidget() in (tree, tree.viewport()) and not top.isExpanded()
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Right)
    assert top.isExpanded() and tv._diagram_leg == "MP_hold"
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Left)
    assert not top.isExpanded() and tv._diagram_leg == "MP_hold"
    host.close()


def test_the_arrow_keys_step_with_the_focus_in_the_view_or_nowhere():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    host, tree, tv = _beside_a_tree()
    tv._diagram_scroll.setFocus()
    QApplication.processEvents()
    assert tv.isAncestorOf(QApplication.focusWidget())
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Right)
    assert tv._diagram_leg == "MP_hold_ho"
    QApplication.focusWidget().clearFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() is None
    QTest.keyClick(host, Qt.Key_Left)
    assert tv._diagram_leg == "MP_hold"
    host.close()


def test_the_page_stops_taking_the_arrow_keys_when_the_diagram_gives_the_page_back():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    host, tree, tv = _beside_a_tree()
    tv.set_diagram_maximized(False)
    tree.setFocus()
    tree.setCurrentItem(tree.topLevelItem(0))
    QApplication.processEvents()
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Right)
    assert tree.topLevelItem(0).isExpanded() and tv._diagram_leg == "MP_hold"
    host.close()


def _block_inside_the_viewport(tv, leg):
    """Whether `leg`'s whole block, its heading to its last row, lies inside the visible
    part of the inequality scroll area."""
    scroll = tv._ineq_scroll
    head = _row(tv, leg, heading=True)
    first = tv._tree.visualItemRect(head)
    last = tv._tree.visualItemRect(head.child(head.childCount() - 1))
    to_view = lambda r: tv._tree.viewport().mapTo(scroll.viewport(), r.topLeft())   # noqa: E731
    return (to_view(first).y() >= 0
            and to_view(last).y() + last.height() <= scroll.viewport().height())


@pytest.mark.parametrize("start, steps, shown, height", [("MP_setup", 13, "PM_contention", 500),
                                                         ("PM_contention", -13, "MP_setup", 500),
                                                         ("PM_contention", -1, "MP_contention", 300)])
def test_restoring_the_diagram_shows_the_whole_block_stepped_to(start, steps, shown, height):
    """The block of the inequality stepped to, from its heading to its last row, is in view
    after a restore, whether it lay below the view or above it."""
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv.resize(1800, height)
    QApplication.processEvents()
    tv._draw_whole(start)
    tv.set_diagram_maximized(True)
    for _ in range(abs(steps)):
        tv.step_inequality(1 if steps > 0 else -1)
    assert tv._diagram_leg == shown
    tv.set_diagram_maximized(False)
    QApplication.processEvents()
    QApplication.processEvents()
    assert _block_inside_the_viewport(tv, shown)
    tv.close()
