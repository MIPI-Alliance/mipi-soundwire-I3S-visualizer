"""The Timing view's term diagram (`swi3s_studio/ui/timing_diagram.py`).

Pinned here:
- clicking a term in an inequality opens the diagram pane and draws that term, from each
  column, and nothing else of the inequality; clearing its highlight clears the diagram;
- a click on a highlighted term in another inequality draws it there, the highlight kept,
  and a click on the term drawn, in its inequality, clears it; a recompute with no term
  drawn builds no timeline;
- a launch is drawn with its launcher's clock and data at its own pin, and a clock row with
  each edge the clock makes at its pin within the panel's window;
- a flight mismatch is drawn as the clock's and the data's flights, with no signal rows,
  and a signal row is named Clk or Data @ its pin;
- a term the leg holds twice gets a panel per occurrence, and a launch and its SoundWire
  anchor correction one panel between them, its heading's operators and total agreeing with
  the spans' lengths;
- a column without the term, and a section with no timeline, say so;
- a click on every term of every inequality, the contention and keeper legs too, draws a
  panel in every column; a release, an EDE Delay and a keeper's response are drawn with the
  data they connect, and the drawing of P→M and P→P contention says its two flights net to zero;
- a driver's high-Z stretches are dotted, after a release and before a turn-on, and a margin
  is named "margin" on every leg, red short of zero and green where it is a gap;
- a percentage of nominal reads to a tenth where it is not whole, and a margin guide's value
  prints a true minus and no sign where it rounds to zero;
- a level's label at the right end sits on the side away from where the edge rests, chained
  spans are on separate dimension lines, and every label's symbols are subscripted;
- a shown diagram is redrawn when an input changes, its corner in rich text;
- the diagram's ⤢ gives it the page, its columns side by side, and it or a double-click
  restores it;
- maximized, a choice draws the Specification column alone, the Example column alone, or
  both, kept across restoring, which stacks both and hides the choice;
- the labels are the diagram's body font in demi-bold in a wide column, smaller in a narrow
  one, and the strokes scale with the line height;
- a compute error closes the diagram and clears the highlight of the term it drew;
- every term of every leg paints;
- the title wraps beside the column choice, or below it at the full width where beside it it
  would take more lines.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing_diagram.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import timing_diagram_support as support

from swi3s_studio.timing import CalcInputs

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("shared_compute")]


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


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


def _term_key():
    from swi3s_studio.ui.timing_view import _term_key
    return _term_key


def _equation_rows(tv, leg):
    """The index of each of `leg`'s equation rows in the inequalities tree: one, or one per
    column where the columns' terms differ."""
    from PySide6.QtCore import Qt
    tree = tv._tree
    rows = []
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for c in range(head.childCount()):
            if "≥ 0" in head.child(c).text(0) and head.child(c).data(0, Qt.UserRole) == leg:
                rows.append(tree.indexFromItem(head.child(c), 0))
    assert rows, leg
    return rows


def _term_point(tv, leg, key):
    """A point on `key` in `leg`'s equation row of the inequalities tree: the leftmost, on the
    first row holding it."""
    from PySide6.QtCore import QPoint
    tree = tv._tree
    for idx in _equation_rows(tv, leg):
        tree.scrollTo(idx)
        rect = tree.visualRect(idx)
        hit = next((QPoint(x, rect.center().y()) for x in range(rect.left(), rect.right(), 3)
                    if tree._key_at(QPoint(x, rect.center().y())) == key), None)
        if hit is not None:
            return hit
    raise AssertionError((leg, key))


def _term_points(tv, leg):
    """{key: (row index, offset from the row's left)} of every term on `leg`'s equation rows,
    each at the point `_term_point` finds, from one pass along each row."""
    from PySide6.QtCore import QPoint
    tree = tv._tree
    out = {}
    for idx in _equation_rows(tv, leg):
        tree.scrollTo(idx)
        rect = tree.visualRect(idx)
        for x in range(rect.left(), rect.right(), 3):
            key = tree._key_at(QPoint(x, rect.center().y()))
            if key is not None and key not in out:
                out[key] = (idx, x - rect.left())
    return out


def _click(tv, leg, key, points=None):
    """Click `key` in `leg`'s equation row, at `_term_point`, or at its entry of `points` (from
    `_term_points`), which must still hit it."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    if points is None:
        at = _term_point(tv, leg, key)
    else:
        assert key in points, (leg, key)
        idx, dx = points[key]
        tv._tree.scrollTo(idx)
        rect = tv._tree.visualRect(idx)
        at = QPoint(rect.left() + dx, rect.center().y())
        assert tv._tree._key_at(at) == key, (leg, key)
    QTest.mouseClick(tv._tree.viewport(), Qt.LeftButton, Qt.NoModifier, at)


def test_clicking_a_term_draws_only_that_term():
    """A click on a term of an inequality's equation row opens the pane and draws that term:
    one panel per column, each holding only spans of the clicked term."""
    tv = _view()
    assert tv._diagram_pane.isVisible() and tv._diagram_whole       # the default view of 1)
    _click(tv, "MP_setup", "Man_t_DD")
    assert tv._diagram_pane.isVisible() and not tv._diagram_whole
    d = tv._diagram
    assert d.term == "Man_t_DD"
    assert [pn.title for pn in d.panels] == list(tv._column_labels())
    key = _term_key()
    for pn in d.panels:
        assert pn.spans and all(key(sym) == "Man_t_DD" for sym in pn.symbols)
        assert [(row[0].pin, row[0].signal) for row in pn.rows] == [
            ("Manager", "clock"), ("Manager", "data")]
        assert all(len({(r.pin, r.signal) for r in row}) == 1 for row in pn.rows)
    # Clicking it again clears its highlight, and the diagram with it.
    _click(tv, "MP_setup", "Man_t_DD")
    assert tv._hl == set() and d.panels == []


def test_a_click_in_another_inequality_draws_the_term_there():
    """With a term drawn for one inequality, one click on it in another draws it there and
    keeps it highlighted; a click on it there again clears the highlight and the diagram."""
    tv = _view()
    d = tv._diagram
    _click(tv, "MP_setup", "Man_t_DD")
    assert (tv._diagram_leg, d.term) == ("MP_setup", "Man_t_DD")
    _click(tv, "MP_hold", "Man_t_DD")
    assert tv._hl == {"Man_t_DD"}
    assert (tv._diagram_leg, d.term) == ("MP_hold", "Man_t_DD")
    assert d.leg_title.startswith("3)") and d.panels
    assert all(pn.spans for pn in d.panels)
    _click(tv, "MP_hold", "Man_t_DD")
    assert tv._hl == set() and d.panels == [] and tv._diagram_leg is None


def test_a_compute_error_clears_the_drawn_term_with_its_highlight():
    """When the inputs cannot be evaluated the diagram closes and the term it drew is no
    longer highlighted; restoring the inputs leaves both cleared, and a click draws again."""
    tv = _view()
    d = tv._diagram
    _click(tv, "MP_setup", "Man_t_DD")
    assert tv._hl == {"Man_t_DD"} and d.panels
    old = {(side, end): (tv._min if end == "min" else tv._max)[(side, "V_IH")].value()
           for side in ("spec", "proposed") for end in ("min", "max")}
    for side in ("spec", "proposed"):
        for box in (tv._min[(side, "V_IH")], tv._max[(side, "V_IH")]):
            box.setValue(100.0)
    tv.flush_recompute()
    assert tv._compute_error and tv._tree.topLevelItemCount() == 0
    assert tv._hl == set() and tv._diagram_leg is None and not tv._diagram_pane.isVisible()
    assert all(lab.styleSheet() == "" for lab in tv._row_labels.values())
    for (side, end), value in old.items():
        (tv._min if end == "min" else tv._max)[(side, "V_IH")].setValue(value)
    tv.flush_recompute()
    assert not tv._compute_error and tv._tree.topLevelItemCount()
    assert tv._hl == set() and not tv._diagram_pane.isVisible()
    _click(tv, "MP_setup", "Man_t_DD")
    assert tv._hl == {"Man_t_DD"} and d.panels and tv._diagram_pane.isVisible()


def test_a_recompute_with_no_term_drawn_builds_no_timeline(monkeypatch):
    """Once the drawn term's highlight is cleared, an input edit builds no timeline."""
    import swi3s_studio.ui.timing_view as view
    tv = _view()
    built = []
    real = view.leg_timeline
    monkeypatch.setattr(view, "leg_timeline", lambda *a: built.append(a) or real(*a))
    _click(tv, "MP_setup", "Per_t_IS")
    mx = tv._max[("proposed", "Per_t_IS")]
    built.clear()
    mx.setValue(mx.value() + 1.0)
    tv.flush_recompute()
    assert built, "an edit with a term drawn builds its timeline"
    _click(tv, "MP_setup", "Per_t_IS")
    built.clear()
    mx.setValue(mx.value() + 1.0)
    tv.flush_recompute()
    assert built == [] and tv._diagram.panels == []


def test_a_launch_is_drawn_at_its_own_pin():
    """A launch shows its launcher's clock and data at its own pin, and a crossing only the
    ramp that crosses."""
    from swi3s_studio.ui.timing_diagram import panels
    for leg, key, pin in (("MP_setup", "Man_t_DD", "Manager"),
                          ("PM_setup", "Per_t_DD", "Peripheral"),
                          ("PP_setup", "Per_t_DD", "Peripheral A")):
        (pn,) = panels(support.sections(CalcInputs(), leg), key, _term_key())
        assert {(row[0].pin, row[0].signal) for row in pn.rows} == {(pin, "clock"),
                                                                     (pin, "data")}
    (pn,) = panels(support.sections(CalcInputs(), "MP_setup"), "t_RF Man DATA", _term_key())
    assert [[r.key for r in row] for row in pn.rows] == [["data@per"]]


def test_a_clock_row_draws_each_edge_its_pin_makes_in_the_window():
    """A clock row holds the timeline's other edges at its pin that are drawn in the panel's
    window, so a launch whose window reaches the next clock edge draws the clock falling
    there; an edge outside the window is left out."""
    from dataclasses import replace

    from swi3s_studio.ui.timing_diagram import _settle_ns, panels
    for inp, held in ((replace(CalcInputs(), Man_t_DD_max_ns=23.0, F_CLK_target_MHz=13.65),
                       True), (CalcInputs(), False)):
        secs = support.sections(inp, "MP_setup")
        (pn,) = panels(secs, "Man_t_DD", _term_key())
        tl = secs[0].timeline
        lo, hi = pn.window
        edges = [r for r in tl.ramps if r.signal == "clock" and r.pin == "Manager"]
        drawn = {r.key for row in pn.rows for r in row if r.signal == "clock"}
        shown = {r.key for r in edges if r.start_ns < hi and r.start_ns + _settle_ns(r) > lo}
        assert drawn == shown, (drawn, shown)
        assert ("clk_e1@man" in drawn) is held, drawn
        (row,) = [row for row in pn.rows if row[0].signal == "clock"]
        assert all(a.rising != b.rising for a, b in zip(row, row[1:]))


def test_a_ramp_named_for_its_edge_draws_on_its_data_lanes_panel():
    """The ramp to V_OH / V_OL is `<dev>_t_RF,DATA,rise` or `,fall`, here on an exponential
    ramp at V_OH + V_OL ≠ 1; it keys on its lane's row, and the click on that row
    draws its span with the releasing data ramp, of the polarity its symbol names."""
    from swi3s_studio.ui.timing_diagram import panels
    inp = CalcInputs(Man_shape="exp", Per_shape="exp", V_OH_min_frac=0.85, V_OL_max_frac=0.25,
                     Man_ede=True, Per_ede=True, handover_UIs=0.0)
    for leg, side in (("MP_contention", "Man"), ("PM_contention", "Per")):
        secs = support.sections(inp, leg)
        (ramp,) = [t.symbol for t in secs[0].terms
                   if t.symbol.startswith(f"{side}_t_RF,DATA,")]
        assert _term_key()(ramp) == f"t_RF {side} DATA", ramp
        (pn,) = panels(secs, f"t_RF {side} DATA", _term_key())
        assert pn.symbols == [ramp], pn.symbols
        (row,) = pn.rows
        assert row[0].signal == "data" and row[0].rising is ramp.endswith(",rise"), leg


def test_a_mismatch_is_drawn_as_the_two_flights_and_rows_are_named_short():
    """t_PD,mis is a panel with no signal rows: the clock's and the data's flights from one
    start, and the term between their ends. A signal row is named Clk or Data @ its pin."""
    from swi3s_studio.ui.timing_diagram import _row_name, panels
    for leg in ("MP_setup", "MP_hold_ho", "PM_setup", "PM_hold_ho"):
        (pn,) = panels(support.sections(CalcInputs(), leg), "t_PD,mis fraction", _term_key())
        assert pn.rows == [] and [g[2] for g in pn.guides] == ["Clock propagation",
                                                              "Data propagation"]
        assert pn.guides[0][0] == pn.guides[1][0]
        (sp,) = pn.spans
        assert {sp.start, sp.end} == {pn.guides[0][1], pn.guides[1][1]}
    (pn,) = panels(support.sections(CalcInputs(), "PP_setup"), "Per_t_IS", _term_key())
    assert [_row_name(row[0]) for row in pn.rows] == ["Clk @ Peripheral B",
                                                     "Data @ Peripheral B"]


def test_a_repeated_term_gets_a_panel_each_and_a_chained_one_shares_one():
    """P→M setup's two flights (the clock to the peripheral, the data back) are two panels;
    a SoundWire launch's row and its anchor correction chain into one."""
    from swi3s_studio.ui.timing_diagram import panels
    two = panels(support.sections(CalcInputs(), "PM_setup"), "bus length (cm)", _term_key())
    assert len(two) == 2
    assert {row[0].signal for row in two[0].rows} == {"clock"}
    assert {row[0].signal for row in two[1].rows} == {"data"}
    sw = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2", tRF_Man_CLK_ns=8.3,
                    tRF_Man_DATA_ns=8.3)
    secs = support.sections(sw, "MP_setup")
    (one,) = panels(secs, "Man_t_DD", _term_key())
    (anchor,) = [t.symbol for t in secs[0].terms if t.symbol.startswith("Man_t_DD,anchor,")]
    assert one.symbols == ["Man_t_DD", anchor]


def test_a_chained_panel_heading_adds_up_to_its_total():
    """A launch and its SoundWire anchor correction: the heading subtracts a span that runs
    against the first, with the operator the inequality gives the correction relative to the
    launch, so the spans' unsigned lengths with the heading's operators make its total. The
    Peripheral's case runs its clock at t_Slew_Clock's min, where its min-side correction is
    the data ramp's alone and runs against the launch."""
    import re

    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    QApplication.instance() or QApplication([])
    d = TimingDiagram(_subscript, _term_key())
    sw = dict(Man_spec="SW13_1V2", Per_spec="SW13_1V2")
    negative = False
    for inp, leg, key in ((CalcInputs(**sw, tRF_Man_CLK_ns=2.0), "PM_hold", "Per_t_DD"),
                          (CalcInputs(**sw, tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3),
                           "MP_setup", "Man_t_DD"),
                          (CalcInputs(**sw, tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3,
                                      Man_t_DD_min_ns=0.2, Man_t_DD_max_ns=0.2),
                           "MP_setup", "Man_t_DD")):
        sections = support.sections(inp, leg)
        d.show_term(leg, key, sections)
        (pn,) = d.panels
        (anchor,) = [t.symbol for t in sections[0].terms
                     if t.symbol.startswith(f"{key},anchor,")]
        assert pn.symbols == [key, anchor]
        head = re.sub(r"<[^>]+>", "", d._heading(pn))
        ops = re.findall(r" ([+−]) ", head)
        terms = {t.symbol: t for t in sections[0].terms}
        relative = "+" if terms[key].op == terms[anchor].op else "−"
        assert ops == [relative] == ["−"], (leg, head)
        lengths = [abs(pn.timeline.duration(sp)) for sp in pn.spans]
        shown = re.search(r"= ([-\u2212]?[\d.]+) ns", head).group(1)
        total = float(shown.replace("\u2212", "-"))
        assert lengths[0] - lengths[1] == pytest.approx(total, abs=0.006), (leg, head)
        negative |= total < 0
    assert negative, "a launch shorter than its anchor correction has a negative total"


def test_a_heading_with_a_zero_lead_span_reads_along_the_first_non_zero_span():
    """A chained heading whose first span is zero long takes its direction from the first
    span with a length, so a later span against it is subtracted and the total is the
    spans' unsigned lengths combined with the operators shown."""
    import re
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    QApplication.instance() or QApplication([])
    d = TimingDiagram(_subscript, _term_key())
    for durations, ops, total in (([0.0, -0.8], [" + "], "0.80"),
                                  ([0.0, 0.8, -0.3], [" + ", " − "], "0.50"),
                                  ([0.0, -0.8, 0.3], [" + ", " − "], "0.50")):
        spans = list(range(len(durations)))
        pn = SimpleNamespace(
            title="T", spans=spans, symbols=[f"S{i}" for i in spans],
            timeline=SimpleNamespace(duration=lambda sp, d=durations: d[sp]))
        head = re.sub(r"<[^>]+>", "", d._heading(pn))
        assert re.findall(r" ([+−]) ", head) == [o.strip() for o in ops], head
        assert head.endswith(f"= {total} ns"), head


def test_a_margin_guide_value_uses_the_true_minus_and_no_sign_at_zero():
    from swi3s_studio.ui.timing_diagram import _signed_ns
    assert _signed_ns(0.123) == "+0.12"
    assert _signed_ns(-0.123) == "\u22120.12"
    assert _signed_ns(-0.001) == "0.00"
    assert _signed_ns(0.0) == "0.00"


def test_the_shown_diagram_follows_an_input_change():
    """With a term's diagram shown, editing that term's input redraws it from the new
    worst corner: the span is the new value, and the corner names its rows with subscripts
    rather than run together."""
    tv = _view()
    tv._hl = {"Per_t_IS"}
    tv._draw_term("Per_t_IS", "MP_setup")
    d = tv._diagram

    def lengths():
        return [abs(pn.timeline.duration(sp)) for pn in d.panels for sp in pn.spans]

    before = lengths()
    assert before and "Per_t<sub>IS</sub>" in d.panels[0].corner
    mn, mx = tv._min[("spec", "Per_t_IS")], tv._max[("spec", "Per_t_IS")]
    mx.setValue(mx.value() + 1.0)
    mn.setValue(mn.value() + 1.0)
    tv.flush_recompute()
    after = lengths()
    assert len(after) == len(before)
    assert "Per_t<sub>IS</sub>" in d.panels[0].corner
    assert after[0] == pytest.approx(before[0] + 1.0, abs=1e-6), (before, after)
    assert after[1:] == pytest.approx(before[1:]), "only the Specification column was edited"


def test_a_column_without_the_term_and_a_section_without_a_timeline_say_so():
    from swi3s_studio.ui.timing_diagram import DiagramSection, panels
    (pn,) = panels(support.sections(CalcInputs(), "MP_setup"), "Per_t_DD", _term_key())
    assert pn.timeline is None and "not in this column" in pn.note
    (pn,) = panels([DiagramSection("Specification", None, note="none")], "UI", _term_key())
    assert pn.timeline is None and pn.note == "none"


@pytest.mark.parametrize("leg, key, rows", [
    ("MP_contention", "Man_t_DZ", {("Manager", "clock"), ("Manager", "data")}),
    ("PM_contention", "Man_t_ZD", {("Manager", "clock"), ("Manager", "data"),
                                   ("Peripheral", "data")}),
    ("keeper_Man", "Man_t_Keeper_Response", {("Manager", "data")}),
    ("keeper_Per", "Man_t_Keeper_Response", {("Manager", "data")})])
def test_a_click_on_a_contention_or_keeper_term_draws_it_in_every_column(leg, key, rows):
    tv = _view()
    _click(tv, leg, key)
    d = tv._diagram
    assert tv._diagram_pane.isVisible() and d.term == key
    assert [pn.title for pn in d.panels] == list(tv._column_labels())
    for pn in d.panels:
        assert pn.timeline is not None and pn.spans and not pn.note, pn.note
        assert {(row[0].pin, row[0].signal) for row in pn.rows} == rows
        assert [g[2] for g in pn.guides] == ["margin"]
    assert not d.grab().isNull()


def test_a_click_on_every_term_of_every_leg_draws_a_panel():
    """A click on every term the equation row prints, for every inequality at the view's own
    inputs with a SoundWire Manager and an EDE Peripheral: each column holding the term draws
    it, and a P→M or P→P contention column says its two flights net to zero.
    The panels of every term are checked at the calculator, for this setup, the default one
    and EDE on both sides, in `test_timing_view_columns.py`."""
    from swi3s_studio.timing.calculator import INEQUALITIES
    from swi3s_studio.timing.timeline import NET_ZERO_NOTE
    tv = _view()
    d = tv._diagram
    for man, per, ede_terms in (("SW13_1V2", "SWI3S_PHY2_EDE", {"Per_t_EDE"}),):
        setup = (man, per)
        for combo, spec in ((tv._man_spec_combo, man), (tv._per_spec_combo, per)):
            assert combo.findData(spec) >= 0, spec
            combo.setCurrentIndex(combo.findData(spec))
            assert combo.currentData() == spec
        tv.flush_recompute()
        swept = set()
        for leg in INEQUALITIES:
            held = {side: {_term_key()(t.symbol)
                           for t in tv._worst[side][leg].breakdowns[leg].terms}
                    for side in ("spec", "proposed")}
            keys = held["spec"] | held["proposed"]
            swept |= keys
            points = None
            for key in keys:
                tv._clear_terms()
                if points is None:
                    _click(tv, leg, key)         # the pane opens at the first click
                    points = _term_points(tv, leg)
                else:
                    _click(tv, leg, key, points)
                assert (tv._diagram_leg, d.term) == (leg, key) and d.panels, (setup, leg, key)
                for col, side in enumerate(("spec", "proposed")):
                    mine = [pn for pn in d.panels if pn.column == col]
                    assert mine, (setup, leg, key, side)
                    for pn in mine:
                        if key not in held[side]:
                            assert pn.timeline is None and "not in this column" in pn.note, (
                                setup, leg, key, side, pn.note)
                            continue
                        assert pn.timeline is not None and pn.spans, (setup, leg, key, side)
                        co = NET_ZERO_NOTE in pn.corner
                        assert co is (leg in ("PM_contention", "PP_contention")), (
                            leg, pn.corner)
        assert ede_terms <= swept, (setup, ede_terms - swept)
        for symbol in ("Man_t_EDE", "Per_t_EDE"):
            assert (symbol in swept) is (symbol in ede_terms), (setup, symbol)


def test_a_high_z_stretch_is_dotted_and_a_short_margin_is_red(monkeypatch):
    """A conventional release goes dotted at its high-Z and a turn-on leaves a dotted
    stretch; a margin short of zero is red and one with room green, each named "margin", on a
    contention and a keeper leg."""
    from PySide6.QtGui import QColor

    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    _app()
    d = TimingDiagram(_subscript, _term_key())
    dotted, dims = [], []
    high_z, dimension = TimingDiagram._high_z, TimingDiagram._dimension
    monkeypatch.setattr(TimingDiagram, "_high_z", lambda self, p, pn, level, t0, t1, *a, **k: (
        dotted.append((pn.timeline.leg, t0, t1)),
        high_z(self, p, pn, level, t0, t1, *a, **k))[-1])
    monkeypatch.setattr(TimingDiagram, "_dimension", lambda self, p, x0, x1, y, label, col,
                        *a: (dims.append((label, col.name())),
                             dimension(self, p, x0, x1, y, label, col, *a))[-1])
    red, green = QColor(VizTheme.DIAG_BAD).name(), QColor(VizTheme.DIAG_OK).name()
    for inp, leg, key, name, colour in (
            (CalcInputs(handover_UIs=0.0), "PM_contention", "Per_t_DZ", "margin", red),
            (CalcInputs(), "PM_contention", "Man_t_ZD", "margin", green),
            (CalcInputs(Man_ede=True, t_keeper_ns=8.0), "keeper_Man", "Man_t_EDE", "margin",
             red)):
        dotted.clear()
        dims.clear()
        d.show_term(leg, key, support.sections(inp, leg))
        d.resize(900, d.minimumHeight())
        assert not d.grab().isNull()
        tl = d.panels[0].timeline
        rel = tl.event("release")
        assert any(t0 == pytest.approx(rel.t_ns) for _, t0, _ in dotted), (leg, dotted)
        guides = [(label, col) for label, col in dims if label.startswith(name)]
        assert guides and all(col == colour for _, col in guides), (leg, dims)
        assert not any(label.startswith(("overlap", "short")) for label, _ in dims), dims
    # A turn-on's dotted stretch ends where its ramp leaves high-Z.
    dotted.clear()
    d.show_term("MP_hold_ho", "Man_t_ZD", support.sections(CalcInputs(), "MP_hold_ho"))
    d.resize(900, d.minimumHeight())
    assert not d.grab().isNull()
    start = d.panels[0].timeline.ramp("data@man").start_ns
    assert any(t1 == pytest.approx(start) for _, _, t1 in dotted), dotted


def test_a_percentage_of_nominal_reads_to_a_tenth_where_it_is_not_whole():
    from swi3s_studio.ui.timing_diagram import pct_text
    from swi3s_studio.ui.timing_view import _supply_text
    assert [pct_text(f) for f in (0.05, -0.1, -0.025, 0.025, 1e-13, 0.0)] == [
        "+5 %", "−10 %", "−2.5 %", "+2.5 %", "±0 %", "±0 %"]
    assert pct_text(0.975 - 1.0) == "−2.5 %" and pct_text(1.025 - 1.0) == "+2.5 %"
    assert _supply_text("MP_setup", (0.975, 1.025, 1.0)) == (
        "V<sub>SEOS</sub> Manager −2.5 %, Peripheral +2.5 %")
    assert _supply_text("MP_setup", (1.0, 1.05, 1.0)) == (
        "V<sub>SEOS</sub> Manager ±0 %, Peripheral +5 %")


def _painted(d, monkeypatch, inp, leg, key):
    """Paint `key` of `leg`; returns the labels placed, the spans' dimension lines' y, and
    every rich text laid out, by the paint alone (not the layout the minimum height is
    found by)."""
    from PySide6.QtGui import QPainter

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    labels, span_y, html = [], [], []
    place, span, static = (TimingDiagram._place_labels, TimingDiagram._paint_span,
                           TimingDiagram._static)
    # A nested context reverts only these patches, not the fixture's own.
    with monkeypatch.context() as m:
        m.setattr(TimingDiagram, "_place_labels", lambda self, p, labs, *a: (
            labels.append(list(labs)), place(self, p, labs, *a))[-1])
        m.setattr(TimingDiagram, "_paint_span", lambda self, p, pn, sp, sym, y, f: (
            span_y.append((pn.title, y)), span(self, p, pn, sp, sym, y, f))[-1])
        m.setattr(TimingDiagram, "_static", lambda self, h, *a: (
            html.append(h), static(self, h, *a))[-1])
        columns = TimingDiagram._paint_columns
        m.setattr(TimingDiagram, "_paint_columns", lambda self, p, row_h: (
            isinstance(p, QPainter) and [seen.clear() for seen in (labels, span_y, html)],
            columns(self, p, row_h))[-1])
        d.show_term(leg, key, support.sections(inp, leg))
        d.resize(900, d.minimumHeight())
        assert not d.grab().isNull()
    return labels, span_y, html


def test_the_receivers_rail_label_sits_away_from_the_resting_edge(monkeypatch):
    """At the right end the edge rests where its last ramp settles: the receiver's V_DD label
    sits under its line where that is above the line, and above it otherwise."""
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    _app()
    d = TimingDiagram(_subscript, _term_key())
    seen = []
    real = TimingDiagram._paint_threshold

    def spy(self, p, th, r, level_y, g0, mid, end_level, font):
        labels = real(self, p, th, r, level_y, g0, mid, end_level, font)
        rail = th.corner.ground_shift + th.corner.rx_supply
        (lab,) = [lab for lab in labels if lab.at == "right" and " V_DD " in lab.text
                  and not lab.text[0].isdigit()]
        seen.append((end_level > rail + 1e-9, lab.above))
        return labels

    monkeypatch.setattr(TimingDiagram, "_paint_threshold", spy)
    sw = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")
    for leg, key in (("PM_setup", "Man_t_IS"), ("PM_hold", "Per_t_DD"),
                     ("MP_setup", "Per_t_IS"), ("MP_hold", "Per_t_IH")):
        d.show_term(leg, key, support.sections(sw, leg))
        d.resize(900, d.minimumHeight())
        assert not d.grab().isNull()
    assert seen and all(above is not rests_above for rests_above, above in seen), seen
    assert (True, False) in seen, "no row rests above the receiver's rail"


def test_chained_spans_have_their_own_dimension_lines(monkeypatch):
    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    _app()
    d = TimingDiagram(_subscript, _term_key())
    sw = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")
    _, span_y, _ = _painted(d, monkeypatch, sw, "PM_hold", "Per_t_DD")
    ys = [y for _, y in span_y]
    assert len(ys) == 2 and ys[1] > ys[0] + d._lh()


def test_every_label_has_its_symbols_subscripted(monkeypatch):
    """A point on no drawn ramp, a threshold's bound and a level's V_DD read with subscripts
    as the heading does, and no label is laid out with a raw symbol."""
    import re

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    _app()
    d = TimingDiagram(_subscript, _term_key())
    sw = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")
    _, _, html = _painted(d, monkeypatch, sw, "MP_hold", "Man_t_DD")
    _, _, more = _painted(d, monkeypatch, sw, "MP_setup", "Per_t_IS")
    html += more
    _, _, more = _painted(d, monkeypatch, CalcInputs(Man_spec="SW13_1V2"), "keeper_Per",
                          "Man_t_Keeper_Response")
    html += more
    text = " | ".join(html)
    assert "Man_t<sub>DD</sub> as tabulated" in text
    assert re.search(r"V<sub>I[HL],(max|min)</sub>", text) and "V<sub>DD</sub>" in text
    assert re.search(r"V<sub>T[HL]_Keeper,(max|min)</sub>", text), text
    assert not re.search(r"V_(DD|IH|IL|TH|TL)|_t_[A-Z]", text), text


def test_maximizing_the_diagram_puts_its_columns_side_by_side():
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    d = tv._diagram
    assert len(d.columns()) == 1
    tv._diagram_max_btn.click()
    QApplication.processEvents()
    assert not tv._ineq_scroll.isVisible() and not tv._topbar.isVisible()
    assert [[pn.column for pn in col] for col in d.columns()] == [[0], [1]]
    assert not d.grab().isNull()
    d.doubleClicked.emit()           # a double-click on the diagram restores it, too
    QApplication.processEvents()
    assert tv._ineq_scroll.isVisible() and tv._topbar.isVisible()
    assert len(d.columns()) == 1 and not tv._diagram_max_btn.isChecked()


def test_maximized_the_diagram_draws_either_column_or_both():
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")
    d = tv._diagram
    btns = tv._diagram_show_btns
    tv._diagram_max_btn.click()
    QApplication.processEvents()
    assert all(b.isVisible() for b in btns.values())
    assert [btns[k].text() for k in ("spec", "proposed", "both")] == [
        *tv._column_labels(), "Both"]
    assert btns["both"].isChecked()
    for key, want in (("proposed", [[1]]), ("spec", [[0]]), ("both", [[0], [1]])):
        btns[key].click()
        QApplication.processEvents()
        assert [[pn.column for pn in col] for col in d.columns()] == want, key
        assert not d.grab().isNull()
    btns["proposed"].click()
    tv._diagram_max_btn.click()          # restored: the choice kept, stacked
    QApplication.processEvents()
    assert btns["proposed"].isChecked() and btns["proposed"].isVisible()
    assert [[pn.column for pn in col] for col in d.columns()] == [[1]]
    tv._diagram_max_btn.click()          # maximized again: the choice kept
    QApplication.processEvents()
    assert btns["proposed"].isChecked()
    assert [[pn.column for pn in col] for col in d.columns()] == [[1]]


def test_beside_the_inequalities_the_picker_draws_either_column_or_both_and_is_kept():
    """Beside the inequalities the diagram shows the column picker, Both by default: Both
    stacks the two columns, either column alone takes the pane's whole width and leaves the
    other out of the drawing, the footnote and an export, and the choice is kept in QSettings
    for the next page; a kept value the picker does not offer opens on Both."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_view import TimingView
    QSettings().remove("timing_diagrams/columns")
    tv = _view()
    d = tv._diagram
    btns = tv._diagram_show_btns
    assert not tv.is_diagram_maximized() and d.whole
    assert all(b.isVisible() for b in btns.values()) and btns["both"].isChecked()
    assert [[pn.column for pn in col] for col in d.columns()] == [[0, 1]]
    left, right = tv._column_labels()
    assert left in d.footnote or d.footnote.startswith("Both columns")
    btns["proposed"].click()
    QApplication.processEvents()
    assert [[pn.column for pn in col] for col in d.columns()] == [[1]]
    assert d._frames() == [(0.0, float(d.width()))]
    assert d.footnote.startswith(right) and left not in d.footnote
    svg = d.export("svg").decode()
    assert right in svg and left not in svg
    assert QSettings().value("timing_diagrams/columns") == "proposed"
    tv._hl = {"Man_t_DD"}
    tv._draw_term("Man_t_DD", "MP_setup")        # a term follows the choice too
    QApplication.processEvents()
    assert [[pn.column for pn in col] for col in d.columns()] == [[1]]
    again = TimingView()
    assert again._diagram_show == "proposed" and again._diagram_show_btns["proposed"].isChecked()
    assert [pn.column for col in again._diagram.columns() for pn in col] in ([], [1])
    btns["both"].click()
    assert QSettings().value("timing_diagrams/columns") == "both"
    assert [[pn.column for pn in col] for col in d.columns()] == [[0, 1]]
    QSettings().setValue("timing_diagrams/columns", "sideways")
    assert TimingView()._diagram_show == "both"


def test_labels_take_the_body_size_in_a_wide_column_and_strokes_scale(monkeypatch):
    """A wide column's labels are the level labels' size of the diagram's font in their
    weight; a narrow column's are `label.narrow` of it; a pixel-sized font scales in pixels;
    an edge's pen is the clock's weight in line heights (`diagram_style`'s defaults)."""
    from PySide6.QtGui import QFont

    from swi3s_studio.ui import diagram_style as ds
    from swi3s_studio.ui import timing_diagram as td
    from swi3s_studio.ui.timing_view import _subscript
    _app()
    d = td.TimingDiagram(_subscript, _term_key())
    f = QFont()
    f.setPixelSize(18)
    d.setFont(f)
    lh = d.units()[0]
    d._frame = (0.0, td._LABEL_FULL_W * lh)
    wide = d._label_font()
    size = 18 * ds.default("font.level.size")
    assert wide.pixelSize() == round(size) == 15 and wide.weight() == QFont.Normal
    d._frame = (0.0, td._LABEL_FULL_W * lh - 1.0)
    assert d._label_font().pixelSize() == round(size * ds.default("label.narrow"))
    assert d._small().pixelSize() == round(18 * ds.default("font.note.size"))
    widths = []
    real = td.TimingDiagram._pen
    monkeypatch.setattr(td.TimingDiagram, "_pen", lambda self, c, w, *a: (
        widths.append(w), real(self, c, w, *a))[-1])
    d.show_term("MP_setup", "Man_t_DD", support.sections(CalcInputs(), "MP_setup"))
    d.resize(900, d.minimumHeight())
    assert not d.grab().isNull()
    trace = ds.default("clock.weight")
    assert trace in widths
    assert real(d, td.QColor("red"), trace).widthF() == trace * lh


# The input sets `test_every_term_of_every_leg_paints` sweeps, by name, and those it draws
# side by side.
_EVERY_TERM_INPUTS = (
    ("default", CalcInputs()),
    ("exp shapes and 2 UIs", CalcInputs(Man_shape="exp", Per_shape="exp", handover_UIs=2.0)),
    ("SW13 1.2 V", CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")),
    ("EDE and 0 UIs", CalcInputs(Man_ede=True, Per_ede=True, handover_UIs=0.0)),
    ("SW13 Manager and EDE", CalcInputs(Man_spec="SW13_1V2", Man_ede=True, Per_ede=True)))
_SIDE_BY_SIDE = (_EVERY_TERM_INPUTS[1][1], _EVERY_TERM_INPUTS[4][1])


@pytest.mark.parametrize("inp,part", support.sweep_params(_EVERY_TERM_INPUTS))
def test_every_term_of_every_leg_paints(request, inp, part):
    """Over every leg and term (`part` of them, `timing_diagram_support.PARTS`) at five
    input sets, 900 px wide, the second and the fifth side by side: a term draws panels,
    each with a timeline, and paints. On the platform scope every input set but the default
    is swept over its `cover` part only."""
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timing_diagram import TimingDiagram
    from swi3s_studio.ui.timing_view import _subscript
    if part == "rest":
        support.left_to_the_full_suite(request)
    _app()
    d = TimingDiagram(_subscript, _term_key())
    d.resize(900, 600)
    d.set_side_by_side(inp in _SIDE_BY_SIDE)
    for leg, key, sections in support.sweep(inp, part):
        d.show_term(leg, key, sections)
        assert d.panels and all(pn.timeline is not None for pn in d.panels), (leg, key)
        d.resize(900, d.minimumHeight())
        assert not d.grab().isNull()
        QApplication.processEvents()



@pytest.mark.parametrize("width, below", [(1100, True), (1800, False)])
def test_the_title_goes_below_the_column_choice_where_beside_it_it_would_wrap(width, below):
    """With an EDE pairing the column choice's headings are long. Beside the choice where it
    fits on as few lines as the full width allows, the title is otherwise below the choice at
    the full width: one line either way here, and never one character per line."""
    from PySide6.QtWidgets import QApplication
    tv = _view()
    tv.resize(width, 1000)
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SWI3S_PHY2_EDE"))
    tv.flush_recompute()
    QApplication.processEvents()
    tv._show_diagram()
    QApplication.processEvents()
    d = tv._diagram
    corner = d._corner
    assert d.leg_title and corner is not None
    one = d._static("X", d._title_font(), -1.0).size().height()
    assert d._title_text().size().height() < 1.5 * one          # one line
    assert d._title_below() is below
    if below:
        assert d._title_top() >= corner.y() + corner.height()
        assert d._title_h() >= d._title_top() + one
    else:
        assert d._title_top() < corner.y() + corner.height()
        assert d._title_w() <= corner.x()
    tv.close()
