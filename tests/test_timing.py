"""Timing-mode tests.

- The ported compute core matches the source `swtiming.calculator` exactly (the
  source app validated the math against two reference spreadsheets, 56/56 + 58/58;
  we pin equality to it rather than re-deriving).
- The TimingView renders the four inequalities + binding summary, recomputes on
  input change, and its inputs round-trip through the workspace dict.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_timing.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from swi3s_studio.timing import INEQUALITIES, CalcInputs, compute


def test_compute_defaults_shape():
    res = compute(CalcInputs())
    assert set(res.breakdowns) == set(INEQUALITIES)
    for ineq, b in res.breakdowns.items():
        assert b.terms, ineq
        assert b.is_setup == ("setup" in ineq)      # MP_setup, MP_setup_ho, PM_setup*
    assert res.binding_inequality in INEQUALITIES
    assert res.F_max_binding_MHz >= 0.0


def test_view_renders_and_roundtrips():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    # The Spec/Proposed calculator shows the four inequalities in its summary.
    txt = tv.summary_text()
    for label in ("MP setup", "MP hold", "PM setup", "PM hold"):
        assert label in txt, label
    # Four inequality headings (blank spacer rows sit between them). The setup
    # headings carry two inequalities — the normal one and the handover (t_ZD)
    # variant — stacked under one title, each with an equation + Spec/Example rows.
    tops = [tv._tree.topLevelItem(i) for i in range(tv._tree.topLevelItemCount())]
    heads = [it for it in tops if it.text(0)]        # spacers have empty text
    # SIX setup/hold headings — Manager/Peripheral, Peripheral/Manager and the two
    # peripherals, setup and hold each — then the three handover ones, then ONE
    # BUS KEEPER HEADING PER RELEASING DEVICE: the same single keeper (it is in the Manager,
    # {ASW3805}) asked about each device that can be the one letting go, so a fail names the
    # device to change instead of leaving two margins under one title.
    assert len(heads) == 11
    # TITLED FOR THE OBLIGATION, not the direction: each of these legs pairs one device's
    # output timing against another's receiver window, and "Manager-to-Peripheral Hold" left
    # the reader to infer which was which. The REQUIREMENT names the leg rather than the
    # mechanism — "Setup", not "Launching", since the launch is a step on the way. The P->P
    # pair carries the same letters its terms do (A drives, B samples — see
    # test_A_drives_and_B_samples_on_every_pp_leg).
    assert [it.text(0) for it in heads if "Peripheral A" in it.text(0)] == \
        ["Peripheral A Setup for Peripheral B",
         "Peripheral A Holding for Peripheral B"]
    assert [it.text(0) for it in heads if it.text(0).startswith("Manager ")] == \
        ["Manager Setup for Peripheral", "Manager Holding for Peripheral"]
    assert [it.text(0) for it in heads
            if it.text(0).endswith("for Manager")] == \
        ["Peripheral Setup for Manager", "Peripheral Holding for Manager"]
    keeper_heads = [it.text(0) for it in heads if "Bus Keeper" in it.text(0)]
    assert keeper_heads == ["Bus Keeper — Manager Releasing",
                            "Bus Keeper — Peripheral Releasing"], keeper_heads
    setup_head = next(it for it in heads if "Setup" in it.text(0))
    assert setup_head.text(0) == "Manager Setup for Peripheral"
    assert setup_head.isExpanded()
    kids = [setup_head.child(c) for c in range(setup_head.childCount())]
    # Handover variant present (its equation swaps in t_ZD).
    assert any("ZD" in k.text(0) for k in kids)
    spec_rows = [k for k in kids if "Specification" in k.text(0)]
    assert len(spec_rows) == 2 and all(r.text(2) not in ("", "—") for r in spec_rows)

    # Corners are auto-selected (no pick UI); inputs() is the nominal midpoint, so
    # pin min == max to make the round-trip assertion exact.
    tv._min[("spec", "bus length (cm)")].setValue(60.0)
    tv._max[("spec", "bus length (cm)")].setValue(60.0)
    assert tv.inputs("spec").bus_length_cm == 60.0
    # workspace round-trip
    # The P→P placement is a control-bar selector (not a corner row), so it needs its own
    # key; a workspace written before it existed must fall back to "unknown", which is what
    # that version computed — each P→P leg at its own worst end of the flight bracket.
    tv._pp_combo.setCurrentIndex(tv._pp_combo.findData("driver_far"))
    d = tv.to_dict()
    assert d["pp_placement"] == "driver_far"
    tv2 = TimingView()
    tv2.set_from_dict(d)
    assert tv2.inputs("spec").pp_placement == "driver_far"
    tv3 = TimingView()
    tv3.set_from_dict({k: v for k, v in d.items() if k != "pp_placement"})
    assert tv3.inputs("spec").pp_placement == "unknown"
    assert tv2.inputs("spec").bus_length_cm == 60.0
    # Ramp models are shared across Spec/Example (one key each), and drive both.
    assert tv2.to_dict()["man_shape"] == d["man_shape"]
    assert tv2.inputs("spec").Man_shape == tv2.inputs("proposed").Man_shape


def test_phy1_selector_reseeds_spec_and_persists():
    """PHY1 is chosen per side, via the spec selector — there is no PHY picker.

    PHY1 (SWI3S v1.1r06 §11.2 Table 129) has slower Peripheral output / high-Z
    timing and a lower F_CLK target; the shared PHY1&2 values (Man_t_DD, V
    thresholds, t_IS/t_IH) are unchanged. The selection round-trips via to_dict.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    # Defaults to PHY2 on both sides, with the PHY2 spec values.
    assert tv._man_spec_combo.currentData() == "SWI3S_PHY2"
    assert "PHY2" in tv._subtitle.text()
    assert (tv._min[("spec", "Per_t_DD")].value(),
            tv._max[("spec", "Per_t_DD")].value()) == (2.0, 20.0)

    # The Spec column is read-only; only the Example column is editable — both PHYs.
    assert tv._min[("spec", "Per_t_DD")].isReadOnly()
    assert not tv._min[("proposed", "Per_t_DD")].isReadOnly()

    # Switch BOTH sides to PHY1: only the three split output-timing rows and
    # F_CLK change. (Per-side mixing is covered in test_timing_cross_spec.)
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY1"))
    assert "PHY1" in tv._subtitle.text()
    assert tv._fclk.value() == 6.4                                   # Table 126 target
    for name, lo, hi in [("Per_t_DD", 14.0, 50.0),                   # Table 129 PHY1
                         ("Per_t_ZD", 14.0, 50.0),
                         ("Man_t_ZD", 14.0, 30.0)]:
        assert tv._min[("spec", name)].value() == lo, name
        assert tv._max[("spec", name)].value() == hi, name
    # Shared PHY1&2 values are untouched by the switch.
    assert (tv._min[("spec", "Man_t_DD")].value(),
            tv._max[("spec", "Man_t_DD")].value()) == (7.0, 18.0)
    assert tv._max[("spec", "Per_t_IH")].value() == 4.5

    # The selection persists through the workspace dict.
    d = tv.to_dict()
    assert d["man_spec"] == "SWI3S_PHY1" and d["per_spec"] == "SWI3S_PHY1"
    tv2 = TimingView()
    tv2.set_from_dict(d)
    assert "PHY1" in tv2._subtitle.text()
    assert tv2._max[("spec", "Per_t_DD")].value() == 50.0
    # A legacy dict with no spec keys predates the selector → PHY2 both sides.
    for k in ("man_spec", "per_spec"):
        d.pop(k)
    tv3 = TimingView(); tv3.set_from_dict(d)
    assert tv3._man_spec_combo.currentData() == "SWI3S_PHY2"


def test_every_displayed_symbol_follows_one_naming_scheme():
    """The vocabulary on the page, pinned — it drifted four ways before this existed.

    THIS IS THE STYLE GATE FOR NEW INEQUALITIES. A leg added later inherits the scheme by
    failing this test if it does not, which is the only mechanism that has actually held:
    every drift so far (`X_PM,setup,clk`, an unsubscripted `X_PP`, `Man_tKeeper_Response`
    spelled the spec's way, a P→P leg charging the Manager's data lane) was invented in good
    faith by copying a neighbouring leg that happened to be the odd one out.

    The scheme, in one place because it is checked in one place:

      * A crossing is `X_<pair>,<role> · <device>_t_RF,<LANE>`.
      * `<pair>` ∈ MP / PM / PP. `<role>` ∈ late / early — setup is eroded by a LATE crossing
        and hold by an EARLY one — MANDATORY and LAST. Where a leg reads ONE lane twice the two
        crossings are GATHERED into a parenthesised expression,
        `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK`, because one pin has one slew; every
        symbol inside the bracket obeys this same grammar (see `_gather_lane_terms`). P→P inserts a device letter
        before it (A drives, B samples); the letter narrows the role, it never replaces it,
        which is how `X_PP,A` passed an earlier version of this check that read the qualifiers
        as an unordered bag. Never `clk`/`data`: that is the lane, and the lane is named by
        the t_RF beside it.
      * THE LANE IS ALWAYS QUALIFIED, `,CLK` or `,DATA`, on every term including the swing
        addend. It used to be dropped where the role was thought to imply it, which was true
        only for a reader who already knew how the envelope was built, and was never uniform
        anyway — X_MP had to qualify both of its Manager-driven lanes, so the page carried two
        spellings of one thing.
      * Every symbol subscripts. The underscore before a token is the marker, so a symbol
        rendering with a bare `_` left in it is misspelled — that is how
        `Man_tKeeper_Response` was caught (the spec writes `Man_tDD`; this file writes
        `Man_t_DD`).
      * A term multiplied by a lane's t_RF must actually MOVE with that lane. Format alone
        would not have caught the P→P legs charging `Man_t_RF,DATA` on a link the Manager does
        not drive; this does.
    """
    import re
    from dataclasses import replace

    from swi3s_studio.timing import CalcInputs, compute
    from swi3s_studio.ui.timing_view import _subscript

    _LANE_FIELD = {("Man", "CLK"): "tRF_Man_CLK_ns", ("Man", "DATA"): "tRF_Man_DATA_ns",
                   ("Per", "DATA"): "tRF_Per_DATA_ns"}

    configs = [{}, {"Man_ede": True, "Per_ede": True}, {"handover_UIs": 0.0},
               {"pp_placement": "driver_far"}]
    symbols = {}
    for kw in configs:
        for name, b in compute(replace(CalcInputs(), **kw)).breakdowns.items():
            for term in b.terms:
                symbols.setdefault(term.symbol, name)
    assert symbols, "no terms to check"

    def _heads(sym: str) -> list[str]:
        """The crossing symbols in a term's head.

        A GATHERED term's head is a parenthesised expression — `(X_PP,A,late - X_PP,B,early)`
        — because two crossings of one pin net into one product. Every symbol inside it must
        satisfy the same grammar as a lone one, which is stronger than the `X_PM,net` spelling
        this replaced: that was one valid symbol saying only that some netting happened, so a
        wrong pair could be combined and still pass.
        """
        head = sym.partition(" · ")[0]
        if head.startswith("(") and head.endswith(")"):
            return [h.strip() for h in re.split(r"[-+]", head[1:-1]) if h.strip()]
        return [head]

    for sym in sorted(symbols):
        rendered = _subscript(sym)
        assert "_" not in re.sub(r"<sub>.*?</sub>|[A-Za-z]+_t(?=<sub>)", "", rendered), \
            f"{sym!r} renders as {rendered!r} with an underscore still showing"

        if " · " not in sym:
            continue
        _head, _, lane_sym = sym.partition(" · ")
        m = re.fullmatch(r"(Man|Per)_t_RF,(CLK|DATA)", lane_sym)
        assert m, f"{sym!r}: the multiplied lane must read `<device>_t_RF,<CLK|DATA>`"
        assert (m.group(1), m.group(2)) in _LANE_FIELD, f"{sym!r}: no such lane"

        for head in _heads(sym):
            if not head.startswith("X_"):
                continue
            parts = head.split(",")
            assert parts[0] in ("X_MP", "X_PM", "X_PP"), sym
            for qual in parts[1:]:
                assert qual not in ("clk", "data"), \
                    f"{sym!r}: the lane is named by {lane_sym!r}; role must be late/early"
                assert qual in ("late", "early", "A", "B"), \
                    f"{sym!r}: bad qualifier {qual!r}"
            # THE ROLE IS MANDATORY AND IT COMES LAST. Checked positionally rather than
            # as a set, because as a set `X_PP,A` passed: `A` is a legal token, so a
            # symbol naming only the device satisfied every rule while saying nothing
            # about WHICH crossing of A's it is. A device letter narrows the role, it
            # does not replace it.
            assert parts[-1] in ("late", "early"), \
                f"{sym!r}: every crossing ends in the role — a device letter is not one"
            for qual in parts[1:-1]:
                assert qual in ("A", "B"), \
                    f"{sym!r}: only a device letter may sit between the pair and the role"

    # ...and the lane a term NAMES is the lane it READS. Checked through `factors`, not
    # through the term's value: `Term.factors` is `(coefficient, t_RF)` and the SECOND
    # element is the lane's own number, so it can be compared against the row directly.
    #
    # The value cannot be used for this. Σ_cross,PM,setup is a MAX over two ΔGND polarities
    # and which pair wins depends on BOTH t_RF values, so bumping the Per DATA row legitimately
    # changes the clock leg's coefficient — a term's value moving with another lane is not
    # evidence that it names the wrong one. (That is real behaviour, documented on
    # `sigma_pm_setup_lanes`; an earlier version of this check called it a bug.)
    base = CalcInputs()
    for (device, which), field in _LANE_FIELD.items():
        want = f"{device}_t_RF,{which}"
        for leg, b in compute(base).breakdowns.items():
            for term in b.terms:
                if not term.factors or " · " not in term.symbol:
                    continue
                if term.value_ns == 0.0:
                    # A structurally zero crossing, emitted on purpose so all three
                    # contention legs keep one shape (a Manager end gets no crossing,
                    # Fig. 176). A zero names a lane nominally and reads none.
                    continue
                if not term.symbol.endswith(want):
                    continue
                # `factors[-1]` IS the row value: a gathered term carries its coefficients
                # first and the t_RF last, so the position is what makes this read right for
                # both shapes (see `Term.factors`).
                allowed_values = {getattr(base, field)}
                # ONE CLOCK, ONE PIN, ONE VALUE. A previous revision allowed the ends of a
                # `tRF_CLK_tol_ns` band here as well, because a leg with two peripherals
                # cornered its two clock crossings independently. Withdrawn: the Manager
                # drives the clock from one pin, so every crossing on this lane multiplies the
                # row's picked value and a leg reading it twice gathers the two into one
                # product (`_gather_lane_terms`). So the allowed set is the row, full stop —
                # which is a stronger check than it was.
                assert any(term.factors[-1] == pytest.approx(v) for v in allowed_values), (
                    f"{leg}: {term.symbol!r} names {want} but multiplies {term.factors[-1]}, "
                    f"and that row offers {sorted(allowed_values)}")


# Which t_RF lanes each leg is ALLOWED to read, declared per leg. Wrong-lane bugs are
# consistent — the symbol and the factors agree with each other and both name the wrong row —
# so no invariant over the symbol can catch them. Only a statement of intent can, and this is
# it: adding a leg means adding a row here and defending it, which is the review step.
#
#   Man CLK    the forwarded clock, wherever a device's detection of it enters
#   Man DATA   data the MANAGER drives
#   Per DATA   data a PERIPHERAL drives
#
# So an M→P leg reads the Manager's two lanes; P→M reads the Manager's clock and the
# peripheral's data; and P→P reads the Manager's clock (both peripherals take it) and the
# PERIPHERAL's data — never Man DATA, because no Manager drives that link. Reusing the M→P
# crossing tuples in the P→P legs put Man DATA there and nothing objected.
_LEGAL_LANES = {
    "MP_setup": {"Man CLK", "Man DATA"}, "MP_setup_ho": {"Man CLK", "Man DATA"},
    "MP_hold": {"Man CLK", "Man DATA"},  "MP_hold_ho": {"Man CLK", "Man DATA"},
    "PM_setup": {"Man CLK", "Per DATA"}, "PM_setup_ho": {"Man CLK", "Per DATA"},
    "PM_hold": {"Man CLK", "Per DATA"},  "PM_hold_ho": {"Man CLK", "Per DATA"},
    "PP_setup": {"Man CLK", "Per DATA"}, "PP_setup_ho": {"Man CLK", "Per DATA"},
    "PP_hold": {"Man CLK", "Per DATA"},  "PP_hold_ho": {"Man CLK", "Per DATA"},
    "MP_contention": {"Man CLK"}, "PM_contention": {"Man CLK"},
    "PP_contention": {"Man CLK"},
    "keeper_Man": {"Man DATA"}, "keeper_Per": {"Per DATA"},
}


def test_no_leg_reads_a_lane_it_has_no_business_reading():
    """Every leg's lanes, declared — the check the naming gate cannot make.

    A wrong-lane bug is CONSISTENT: the symbol says `Man_t_RF,DATA`, the factors carry the
    Man DATA value, and the term moves with the Man DATA row. Symbol, factors and behaviour
    all agree, so no invariant over them objects. The P→P legs shipped like that by reusing
    the M→P crossing tuples, and it was found by eye, not by a test.

    The only thing that catches it is a statement of what each leg SHOULD read. Adding a leg
    means adding a row to `_LEGAL_LANES` and defending it — which is the review step this
    file exists to force, and the point at which "does this apply elsewhere?" gets asked.
    """
    from dataclasses import replace

    lane_of = {"Man_t_RF,CLK": "Man CLK", "Man_t_RF,DATA": "Man DATA",
               "Per_t_RF,DATA": "Per DATA"}

    for kw in ({}, {"Man_ede": True, "Per_ede": True}, {"handover_UIs": 0.0},
               {"pp_placement": "driver_far"}):
        for leg, b in compute(replace(CalcInputs(), **kw)).breakdowns.items():
            allowed = _LEGAL_LANES.get(leg)
            assert allowed is not None, (
                f"{leg} has no declared lane set — add one to _LEGAL_LANES and say why")
            for term in b.terms:
                if " · " not in term.symbol:
                    continue
                lane_sym = term.symbol.split(" · ")[1]
                lane = lane_of.get(lane_sym)
                assert lane is not None, f"{leg}: {term.symbol!r} names an unknown lane"
                assert lane in allowed, (
                    f"{leg}: reads {lane!r} via {term.symbol!r}, but that leg may only read "
                    f"{sorted(allowed)} — either the leg is wrong or _LEGAL_LANES is")

def _term_view():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    tv.resize(1500, 1200)
    tv.show()
    QApplication.processEvents()
    return tv


def test_the_click_regions_line_up_with_the_painted_glyphs():
    """Where the terms are DRAWN and where they are CLICKABLE must be the same places.

    THIS SHIPPED BROKEN and no other test saw it. The hit-test built its own
    QStyleOptionViewItem and called `initFrom(view)`, which copies the palette and the font
    METRICS but leaves `opt.font` at Qt's default 9 pt — while paint receives the view's
    15 px font. The two documents then laid out at different widths (512.8 against 606.5 on
    leg 1), so the error grew with x: clicking `DATA` in `Man_t_RF,DATA` selected `Per_t_IS`,
    two terms further along. Sharing `_doc` and `_origin` was not enough, because the drift
    entered through the option, one level above the guard.

    So the invariant is checked the only way that catches it: for every sample point on every
    row, what the PAINT path has drawn there must equal what the CLICK path resolves there.
    No hardcoded coordinates — a fixed x would have to be updated whenever a symbol changes
    and would test the expectation rather than the agreement.
    """
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtWidgets import QStyleOptionViewItem

    tv = _term_view()
    tree, delegate = tv._tree, tv._tree._delegate

    def painted_key(index, point):
        """The key the PAINT path draws at `point` — its own option, doc and origin."""
        opt = QStyleOptionViewItem()
        tree.initViewItemOption(opt)
        opt.rect = tree.visualRect(index)
        delegate.initStyleOption(opt, index)
        doc = delegate._doc(opt)
        href = doc.documentLayout().anchorAt(
            QPointF(point) - delegate._origin(opt, doc, tree))
        return href[2:] if href.startswith("k:") else None

    rows = samples = 0
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for c in range(head.childCount()):
            item = head.child(c)
            index = tree.indexFromItem(item, 0)
            rect = tree.visualRect(index)
            if rect.width() <= 0 or not item.text(0):
                continue
            rows += 1
            y = rect.center().y()
            for x in range(rect.left(), rect.right(), 3):
                point = QPoint(x, y)
                samples += 1
                assert painted_key(index, point) == tree._key_at(point), (
                    f"row {i}.{c} at x={x}: painted "
                    f"{painted_key(index, point)!r} but clicks as {tree._key_at(point)!r}")
    assert rows > 40 and samples > 5000, (rows, samples)


def test_a_click_between_terms_is_inert_and_only_empty_space_clears():
    """A 2-pixel miss must not throw away the reader's work.

    The operators are deliberately not click targets, so a click on a `−` lands on the row's
    text but on no term. That is INERT; clearing needs a click in genuinely empty space. The
    first version cleared on any non-term click, which meant aiming at a term and catching
    the sign beside it wiped every highlight — the least forgiving possible response.
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    tv = _term_view()
    tree = tv._tree

    index = None
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for c in range(head.childCount()):
            if "≥ 0" in head.child(c).text(0):
                index = tree.indexFromItem(head.child(c), 0)
                break
        if index is not None:
            break
    rect = tree.visualRect(index)
    y = rect.center().y()

    # Map the row: term runs, and the gaps between them (the operators).
    keys = [(x, tree._key_at(QPoint(x, y))) for x in range(rect.left(), rect.right())]
    on_term = [x for x, k in keys if k]
    gaps = [x for x, k in keys if k is None and on_term and on_term[0] < x < on_term[-1]]
    assert gaps, "there should be operator gaps between the terms"

    tv._toggle_term("Per_t_IS")
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(gaps[len(gaps) // 2], y))
    assert tv._hl == {"Per_t_IS"}, "a click on an operator must change nothing"

    # Past the end of the equation is empty space, and that does clear.
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(rect.right() - 2, y))
    assert tv._hl == set(), tv._hl


def test_every_printed_term_resolves_to_a_key():
    """Every term the model can print must map to an input row, or be declared row-less.

    THE FALLBACK IS SILENT BY DESIGN -- `_term_key` never raises, because a term that
    cannot be identified should still be clickable and self-consistent rather than crash a
    render. That makes this test the only thing standing between a new term and a highlight
    that quietly matches nothing, so it sweeps the configurations that change which terms
    exist at all: EDE swaps in `Per_t_hold`, a clocked launch changes nothing but a
    SoundWire side changes the anchor conversions, `t_keeper_ns=0` drops a term entirely,
    and a pinned P->P placement adds the two traversals.
    """
    from dataclasses import replace

    from swi3s_studio.timing import default_swi3s_rows
    from swi3s_studio.timing.spec_source import LaunchMode
    from swi3s_studio.ui.timing_view import _ROWLESS_TERM_KEYS, _key_class, _term_key

    rows = {r.name for r in default_swi3s_rows()}
    syms = set()
    for kw in ({}, {"handover_UIs": 0.0}, {"Man_ede": True, "Per_ede": True},
               {"pp_placement": "driver_far"}, {"launch_mode": LaunchMode.CLK4},
               {"Man_spec": "SW13_1V8"}, {"Per_spec": "SW13_1V8"}, {"t_keeper_ns": 0.0}):
        for b in compute(replace(CalcInputs(), **kw)).breakdowns.values():
            syms |= {t.symbol for t in b.terms}
    assert len(syms) > 30, f"the sweep stopped covering the term set: {len(syms)}"

    keys = {}
    for sym in syms:
        key = _term_key(sym)
        assert key in rows or key in _ROWLESS_TERM_KEYS, (
            f"{sym!r} keys on {key!r}, which is neither an input row nor declared "
            f"row-less -- add the row or add it to _ROWLESS_TERM_KEYS")
        keys[sym] = key

    # The suffixed and bare spellings of one row are ONE key -- that is the whole point of
    # keying on the row: `,pure` is an anchor conversion of a value, not a second parameter.
    assert keys["Man_t_DD,pure"] == keys.get("Man_t_DD", keys["Man_t_DD,pure"]) == "Man_t_DD"
    assert keys["Man_t_DZ,EDE"] == keys["Man_t_DZ"] == "Man_t_DZ"
    # A crossing keys on its LANE, gathered or not: the coefficient has no row.
    assert keys["X_MP,late · Man_t_RF,CLK"] == "t_RF Man CLK"
    assert keys["(X_PP,A,late - X_PP,B,early) · Man_t_RF,CLK"] == "t_RF Man CLK"
    assert keys["1.67·Man_t_RF,DATA"] == "t_RF Man DATA"
    # t_PD IS the bus length in ns, and the mismatch is its own row.
    assert keys["t_PD"] == keys["2·t_PD"] == "bus length (cm)"
    assert keys["t_PD,mis"] == "t_PD,mis fraction"

    # Distinct keys must give distinct CSS classes, or one term would wash another: the
    # class is a sanitised key, and row names carry spaces, parentheses and Greek.
    distinct = set(keys.values())
    assert len({_key_class(k) for k in distinct}) == len(distinct), \
        "two keys collapsed to one CSS class"


def test_clicking_a_term_highlights_it_everywhere_and_a_second_click_clears():
    """The whole feature, end to end, through real mouse events.

    Hit-testing is geometry, so this clicks the VIEWPORT rather than calling the mapper: a
    delegate whose paint origin and hit origin disagree would pass a direct call and fail
    here, and that drift is invisible on screen until someone clicks near a term's edge.
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from swi3s_studio.ui.theme import VizTheme

    tv = _term_view()
    tree = tv._tree
    assert tv._hl == set(), "nothing is highlighted before a click"

    # The first equation row, and the x of each distinct term on it -- discovered by
    # hit-testing rather than by counting characters.
    idx = None
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for c in range(head.childCount()):
            if "≥ 0" in head.child(c).text(0):
                idx = tree.indexFromItem(head.child(c), 0)
                break
        if idx is not None:
            break
    rect = tree.visualRect(idx)
    hits = {}
    for x in range(rect.left(), rect.right(), 3):
        key = tree._key_at(QPoint(x, rect.center().y()))
        if key and key not in hits:
            hits[key] = x
    assert "t_RF Man CLK" in hits and "Per_t_IS" in hits, hits

    x = hits["t_RF Man CLK"]
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(x, rect.center().y()))
    assert tv._hl == {"t_RF Man CLK"}, tv._hl
    # The input row is washed too, which is what keying on rows buys.
    assert VizTheme.HILITE_BG in tv._row_labels["t_RF Man CLK"].styleSheet()
    assert tv._row_labels["Per_t_IS"].styleSheet() == "", "only the clicked row"

    # A SECOND term adds rather than replaces.
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(hits["Per_t_IS"], rect.center().y()))
    assert tv._hl == {"t_RF Man CLK", "Per_t_IS"}, tv._hl

    # Clicking the SAME term again removes it -- and from a different occurrence, since the
    # toggle is on the key and not on the glyph the reader started from.
    other = None
    for i in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(i)
        for c in range(head.childCount()):
            if "≥ 0" not in head.child(c).text(0):
                continue
            r2 = tree.visualRect(tree.indexFromItem(head.child(c), 0))
            if r2 == rect:
                continue
            for x2 in range(r2.left(), r2.right(), 3):
                if tree._key_at(QPoint(x2, r2.center().y())) == "t_RF Man CLK":
                    other = QPoint(x2, r2.center().y())
                    break
            if other:
                break
        if other:
            break
    assert other is not None, "t_RF Man CLK should appear on more than one leg"
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier, other)
    assert tv._hl == {"Per_t_IS"}, tv._hl

    # And a click that lands on no term clears everything.
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(rect.right() - 2, rect.center().y()))
    assert tv._hl == set(), tv._hl


def test_a_highlight_is_a_repaint_and_not_a_recompute():
    """Toggling must not re-evaluate the model or re-lay-out the tree.

    The margins are expensive (a corner search per leg) and the tree's width is fitted to
    its content, so a toggle that rebuilt either would both stutter and visibly reflow. The
    wash is applied by the delegate at paint time from a stylesheet, which is why the item
    TEXT is byte-identical before and after.
    """
    tv = _term_view()
    before = [tv._tree.topLevelItem(i).child(c).text(0)
              for i in range(tv._tree.topLevelItemCount())
              for c in range(tv._tree.topLevelItem(i).childCount())]
    width = tv._tree.columnWidth(0)

    calls = []
    real = tv._per_inequality_worst
    tv._per_inequality_worst = lambda side: (calls.append(side), real(side))[1]
    tv._toggle_term("t_RF Man CLK")

    assert calls == [], "a toggle recomputed the margins"
    after = [tv._tree.topLevelItem(i).child(c).text(0)
             for i in range(tv._tree.topLevelItemCount())
             for c in range(tv._tree.topLevelItem(i).childCount())]
    assert after == before, "a toggle rewrote the tree text"
    assert tv._tree.columnWidth(0) == width, "a toggle moved the fitted width"


def test_anchored_terms_keep_the_palette_ink_in_both_modes():
    """An anchor must not change the colour of the text it wraps.

    THIS EXACT BUG SHIPPED FOR ABOUT TEN MINUTES and is worth a test because it looked like
    a font problem rather than a CSS one. `a { color: inherit }` reads as obviously correct
    and is wrong: Qt's rich-text CSS resolves `inherit` to BLACK, not to the paint context's
    Text role -- so on the dark palette every anchored symbol went near-invisible against
    the panel while the substitution numbers stayed bright (their table cell sets its own
    colour), leaving a half-dimmed page.

    Checked by INK, in both palettes, because that is the only way to see it: the document's
    charFormat reports a default black brush whether or not a colour was set, so querying
    the format cannot distinguish the broken case from the working one.

    IT COMPARES THE TWO RENDERS, NOT THE PALETTE VALUE, and the first version got that wrong.
    Asserting that some pixel equals VizTheme.TEXT is a statement about RASTERISATION rather
    than about colour: the Ubuntu CI job draws this text entirely in antialiased mid-tones
    (#92969c and #94999f, between the background and the ink) and never lands a pixel on the
    full colour, where macOS does — so the assertion failed on a tree whose rendering was
    correct. The property under test is that anchoring changes NOTHING, so the anchored render
    is compared against the plain one, which is what catches the bug: `inherit` made one
    near-black while the other stayed light. An absence of glyphs is a SKIP, so the comparison
    cannot pass vacuously.
    """
    import collections

    from PySide6.QtGui import (
        QAbstractTextDocumentLayout,
        QColor,
        QImage,
        QPainter,
        QPalette,
    )
    from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

    from swi3s_studio.ui import theme
    from swi3s_studio.ui.timing_view import _anchored, _RichTextDelegate, _subscript

    _term_view()          # a QApplication must exist before QImage/QPainter
    delegate = _RichTextDelegate(keys=lambda: frozenset())
    for mode in ("dark", "light"):
        theme.apply_palette(mode)
        plain = "1·UI − " + _subscript("Man_t_DZ")
        anchored = (_anchored("UI", "1·UI") + " − "
                    + _anchored("Man_t_DZ", _subscript("Man_t_DZ")))

        def ink(html):
            # THROUGH THE DELEGATE'S OWN `_doc`, not a re-typed copy of its stylesheet. The
            # first version of this test built the document itself with the correct CSS
            # hardcoded, so it exercised its own string and not the product: re-injecting
            # `color: inherit` into the delegate left it green. It is the delegate's
            # stylesheet that is under test, so the delegate has to be the one that builds
            # the document.
            opt = QStyleOptionViewItem()
            opt.text = html
            opt.font = QApplication.instance().font()
            doc = delegate._doc(opt)
            img = QImage(int(doc.idealWidth()) + 4, int(doc.size().height()) + 4,
                         QImage.Format_RGB32)
            img.fill(QColor(theme.VizTheme.FRAME_BG))
            painter = QPainter(img)
            ctx = QAbstractTextDocumentLayout.PaintContext()
            ctx.palette.setColor(QPalette.Text, QColor(theme.VizTheme.TEXT))
            doc.documentLayout().draw(painter, ctx)
            painter.end()
            hist = collections.Counter()
            for y in range(img.height()):
                for x in range(img.width()):
                    hist[QColor(img.pixel(x, y)).name()] += 1
            return {c for c, n in hist.most_common(3)}

        want, got = ink(plain), ink(anchored)
        bg = theme.VizTheme.FRAME_BG.lower()

        # NOT VERIFIABLE WHERE NOTHING RASTERISES, so say so rather than passing vacuously:
        # with no glyphs both renders are pure background, the comparison below is trivially
        # true, and a green test would be claiming a check it never made.
        if not (want - {bg}):
            pytest.skip("no glyphs rasterised here — the ink is unverifiable")

        # THE PROPERTY IS THAT ANCHORING CHANGES NOTHING. With `color: inherit` the anchored
        # ink went near-black while the plain ink stayed light, so the two sets differ and
        # this fails loudly — without depending on which pixels a platform's antialiasing
        # happens to produce.
        assert got == want, f"{mode}: anchoring changed the ink: {sorted(want)} -> {sorted(got)}"

    theme.apply_palette("dark")



def test_the_wash_is_applied_by_class_to_exactly_the_active_keys():
    """The mechanism, at the level where it can silently stop working.

    A class selector that Qt did not honour would leave the page looking un-highlighted with
    every other test still green, since the keys, the clicks and the row labels would all be
    correct. So this asserts the DOCUMENT carries a background on the active term and on
    nothing else.
    """
    from PySide6.QtGui import QTextCursor, QTextDocument

    from swi3s_studio.ui.theme import VizTheme
    from swi3s_studio.ui.timing_view import _anchored, _key_class

    _term_view()
    html = _anchored("UI", "UI") + " − " + _anchored("Man_t_DZ", "Man_t_DZ")
    doc = QTextDocument()
    doc.setDefaultStyleSheet(
        f"a {{ color: {VizTheme.TEXT}; text-decoration: none; }}"
        f".{_key_class('UI')} {{ background-color: {VizTheme.HILITE_BG}; }}")
    doc.setHtml(html)

    washed = set()
    for pos in range(1, doc.characterCount()):
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        fmt = cur.charFormat()
        if fmt.background().color().name() == VizTheme.HILITE_BG.lower():
            washed.add(fmt.anchorHref())
    assert washed == {"k:UI"}, f"the wash landed on {washed}"


def test_every_leg_reads_in_physical_time_order():
    """The terms are a NARRATIVE, and the order is part of what the page says.

    Four rules, each of which was violated somewhere before this test existed:

      1. The receiver's window is LAST. It is the requirement the whole leg is spent
         satisfying, so it belongs at the end, not two terms in.
      2. A clock-lane crossing comes before a data-lane crossing. The clock edge is what
         a device detects; the data crossing is judged against it. Delta_cross,MP SWAPS
         which lane carries the late crossing between setup and hold, so without a rule
         the two families printed their lanes in opposite orders on one page.
      3. On a leg whose launcher is a PERIPHERAL, the launch sits BETWEEN the two
         crossings: the peripheral must detect the clock before it can launch, and the
         sampler sees the result after the data has crossed the bus.
      4. The two bus traversals are separate terms bracketing that launch — one for the
         clock reaching the launcher, one for the data coming back — not a single
         `2·t_PD` off to the side.

    ORDER IS FREE TO GET WRONG, which is why it needs a test: the margin is a sum, so a
    reordering changes no number and no other test would notice. The whole benefit is to a
    reader, and the whole cost of losing it is silent.
    """
    from dataclasses import replace

    SETUP_WINDOW = ("Per_t_IS", "Man_t_IS")
    HOLD_WINDOW = ("Per_t_IH", "Man_t_IH")
    PER_LAUNCHED = ("PM_setup", "PM_setup_ho", "PM_hold", "PM_hold_ho",
                    "PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho")

    for kw in ({}, {"handover_UIs": 0.0}, {"pp_placement": "driver_far"}):
        b = compute(replace(CalcInputs(), **kw)).breakdowns
        for leg in INEQUALITIES:
            if leg.startswith("keeper") or leg.endswith("contention"):
                continue                      # their own shapes; see the trio's comment
            syms = [t.symbol for t in b[leg].terms]
            heads = [s.split(" · ")[0].split(",")[0] for s in syms]

            # 1. the receiver's window is the last term
            want = SETUP_WINDOW if "setup" in leg else HOLD_WINDOW
            assert heads[-1] in want, f"{leg}: {heads[-1]} is last, expected one of {want}"

            # 2. clock before data
            clk = [i for i, s in enumerate(syms) if "_t_RF,CLK" in s]
            data = [i for i, s in enumerate(syms) if "_t_RF,DATA" in s and "X_" in s]
            assert clk and data, (leg, syms)
            assert max(clk) < min(data), f"{leg}: a data crossing precedes a clock one"

            # 3 & 4. a peripheral launcher detects, launches, then the data crosses back
            if leg in PER_LAUNCHED:
                launch = next(i for i, h in enumerate(heads)
                              if h in ("Per_t_DD", "Per_t_ZD"))
                assert max(clk) < launch < min(data), (
                    f"{leg}: the launch must sit between the clock and data crossings")
                pd = [i for i, s in enumerate(syms) if s == "t_PD"]
                if pd:                        # zero traversals on some P->P placements
                    assert len(pd) == 2, f"{leg}: two flights, two terms: {pd}"
                    assert pd[0] < launch < pd[1], (
                        f"{leg}: the traversals must bracket the launch")


def test_the_inequalities_are_numbered_for_reference_in_display_order():
    """1) … 17), one numbering, shared by the tree and the summary.

    The numbers exist so a leg can be named in conversation without spelling out
    "Peripheral A Holding for Peripheral B, the t_ZD variant". That only works if the
    number means the same row everywhere, so `_INEQ_NUMBER` is derived from the DISPLAY
    grouping and both renderers read it — two independent numberings would drift the
    moment either order changed, and a stale number is worse than no number.

    ")" AND NOT ".", deliberately: the equations carry the "·" multiplication operator and
    decimals like "1.44·8.30", so a leading "1." reads as arithmetic.
    """
    from swi3s_studio.ui.timing_view import _INEQ_GROUPS, _INEQ_NUMBER

    flat = [m for _base, members in _INEQ_GROUPS for m in members]
    assert sorted(_INEQ_NUMBER.values()) == list(range(1, len(INEQUALITIES) + 1))
    assert set(_INEQ_NUMBER) == set(INEQUALITIES), "every inequality gets a number"
    # The display order and INEQUALITIES agree today. They are two separate lists, so if
    # one is ever reordered this says so rather than letting the numbers silently move.
    assert flat == list(INEQUALITIES), (flat, list(INEQUALITIES))

    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    tv = TimingView()
    txt = tv.summary_text()
    assert "1) MP setup" in txt and "17) keeper Per" in txt, txt.splitlines()[:3]
    assert "1. MP setup" not in txt, "a period would read as part of the arithmetic"

    # And on screen: every equation row is numbered, in order, once each.
    seen = []
    tops = [tv._tree.topLevelItem(i) for i in range(tv._tree.topLevelItemCount())]
    for head in tops:
        for c in range(head.childCount()):
            t = head.child(c).text(0)
            if "≥ 0" in t:
                seen.append(int(t.split(")")[0]))
    assert seen == list(range(1, len(INEQUALITIES) + 1)), seen


def test_A_drives_and_B_samples_on_every_pp_leg():
    """A is the driver and B the sampler, on all four P→P legs, and never the reverse.

    THIS NEEDS A HUMAN'S STATEMENT OF INTENT, like `_LEGAL_LANES` does. Nothing in the
    arithmetic prevents a fifth P→P leg from lettering the sampler A: the margin would come
    out identical, since both letters read the same `Per` rows, and the equation on screen
    would be self-consistent while telling the reader the wrong device to change. The
    convention lived in two `_pp_device_note` call sites and in nothing that fails.

    THE RULE. On every P→P leg:
      * A contributes the LAUNCH — `Per_t_DD` on the t_DD legs, `Per_t_ZD` on the `_ho`
        ones — and its own clock detection, `X_PP,A,*`.
      * B contributes the RECEIVER WINDOW — `Per_t_IS` for setup, `Per_t_IH` for hold —
        and its crossing, `X_PP,B,*`.

    What flips between the families is A's CORNER, not its identity: `X_PP,A,late` is
    charged on setup (a late launch eats the setup window) and `X_PP,A,early` is credited on
    hold (an early launch leaves the level valid longer past B's edge). A letter that
    followed the corner instead of the device would look just as consistent.

    Note also what "A drives" does NOT settle, which the notes now say per leg: on a HOLD
    leg A's launch is the aggressor, and on `PP_hold_ho` the level it destroys belongs to the
    peripheral that just released, not to A.
    """
    from dataclasses import replace

    LAUNCH = {"PP_setup": "Per_t_DD", "PP_setup_ho": "Per_t_ZD",
              "PP_hold": "Per_t_DD", "PP_hold_ho": "Per_t_ZD"}
    WINDOW = {"PP_setup": "Per_t_IS", "PP_setup_ho": "Per_t_IS",
              "PP_hold": "Per_t_IH", "PP_hold_ho": "Per_t_IH"}

    for kw in ({}, {"handover_UIs": 1.0}, {"pp_placement": "driver_far"},
               {"Man_ede": True, "Per_ede": True}):
        b = compute(replace(CalcInputs(), **kw)).breakdowns
        for leg in LAUNCH:
            terms = {t.symbol.split(" · ")[0].split(",")[0]: t for t in b[leg].terms}
            launch, window = terms.get(LAUNCH[leg]), terms.get(WINDOW[leg])
            assert launch is not None and window is not None, (leg, sorted(terms))
            assert launch.note.startswith("A"), \
                f"{leg}: the launch ({LAUNCH[leg]}) must be A's — got {launch.note[:40]!r}"
            assert "driver" in launch.note, (leg, launch.note[:60])
            assert window.note.startswith("B") and "sampler" in window.note, \
                f"{leg}: the receiver window ({WINDOW[leg]}) must be B's"
            # And the crossings letter the same way round, which is where a swap would
            # actually change a number: A's is the launcher's detection of the clock.
            crossings = [t.symbol for t in b[leg].terms if "X_PP" in t.symbol]
            assert crossings, leg
            joined = " ".join(crossings)
            assert "X_PP,A" in joined and "X_PP,B" in joined, (leg, crossings)
            # A's corner follows the FAMILY, not the letter: late erodes setup, early
            # credits hold. If these ever swap, one of the two is reading the wrong end.
            want = "X_PP,A,late" if leg.startswith("PP_setup") else "X_PP,A,early"
            assert want in joined, (leg, crossings)


def test_no_pp_leg_reads_one_per_row_for_both_devices():
    """The condition under which A and B's independence starts costing something.

    A and B are unrelated parts and each realises its own value of every `Per` parameter —
    its own t_DD, its own t_IS — exactly as each realises its own slew inside
    the clock's slew. The symbols do not letter the device parameters anyway, and the reason
    is NOT that they are shared (they are not); it is that on every leg as built, each `Per`
    row is read AT MOST ONCE.

    That is what makes the independence free. It costs something only when one row appears
    twice on a leg with opposite signs, because then a single cornered value has to serve as
    both the charge and the credit, and the shared corner flatters the leg — which is exactly
    the clock lane, where `X_PP,A,late` and `X_PP,B,early` both read `Man_t_RF,CLK` and are
    pinned to opposite ends of the band, worth 2.6 ns on P→P hold. A row read once is already
    driven to the end that hurts by the ordinary corner search, and splitting it per device
    would change no number.

    SO THE ARGUMENT EXPIRES IF A LEG EVER READS ONE `Per` ROW FOR BOTH DEVICES, and this
    fails when it does rather than leaving it to be spotted. The fix at that point is a
    per-device band like the clock's, plus `Per_A_*` / `Per_B_*` symbols to name the two ends
    — not a note.
    """
    import re
    from collections import Counter
    from dataclasses import replace

    for kw in ({}, {"handover_UIs": 1.0}, {"Per_ede": True},
               {"pp_placement": "driver_far"}):
        for leg, b in compute(replace(CalcInputs(), **kw)).breakdowns.items():
            if not leg.startswith("PP_"):
                continue
            rows = Counter()
            for term in b.terms:
                head = term.symbol.split(" · ")[0]
                m = re.fullmatch(r"(Per_t_[A-Z]{2})(?:,\w+)*", head)
                if m:
                    rows[m.group(1)] += 1
            twice = [r for r, n in rows.items() if n > 1]
            assert not twice, (
                f"{leg} reads {twice} for both A and B: one cornered value is now serving as "
                f"both a charge and a credit for two independent parts. That needs a "
                f"per-device band and lettered symbols — see `_pp_device_note`")


def test_a_gathered_crossing_states_arithmetic_that_actually_adds_up():
    """A gathered term's SYMBOL, factors and note must all describe the same sum.

    WHY THIS NEEDS ITS OWN TEST. `_gather_lane_terms` folds two crossings of one lane into
    `(X_PP,A,late − X_PP,B,early) · Man_t_RF,CLK`, substituted as `(1.44 − 0.45)·5.00`. The
    term's VALUE is computed from the signed total and is correct however the decomposition is
    written, so no margin assertion can catch a wrong one — and the first version WAS wrong: it
    hoisted the operator out front without dividing the contributions by it, printing
    `(1.44 + 0.45)` under a minus. That reads as −1.89 where the leg is −0.99. Re-injecting the
    missing division passes every other test in this suite.

    So the check is the arithmetic itself, three ways round: the coefficients in `factors` must
    sum to the value in force, the bracketed symbol must carry one crossing name per
    coefficient, and the note must state the same sum in words.
    """
    import re
    from dataclasses import replace

    from swi3s_studio.timing import CalcInputs, compute

    seen = 0
    for kw in ({}, {"handover_UIs": 1.0}, {"Man_ede": True, "Per_ede": True},
               {"pp_placement": "driver_far"}):
        for leg, b in compute(replace(CalcInputs(), **kw)).breakdowns.items():
            for term in b.terms:
                if not term.factors or len(term.factors) < 3:
                    continue
                seen += 1
                *coeffs, trf = term.factors

                # 1. The coefficients, with their own signs, produce the term's value.
                total = sum(coeffs)
                assert abs(term.value_ns) == pytest.approx(abs(total) * trf, abs=1e-9), term
                assert (term.op == "-") == (term.value_ns < 0), term
                assert total > 0, (
                    f"{leg}: the coefficients must be signed RELATIVE to the hoisted operator, "
                    f"so their sum is positive: {coeffs}")
                assert coeffs[0] > 0, \
                    f"{leg}: the bracket must not open with a minus: {coeffs}"

                # 2. The symbol is a bracket holding one crossing per coefficient.
                head = term.symbol.partition(" · ")[0]
                assert head.startswith("(") and head.endswith(")"), term.symbol
                names = [h for h in re.split(r"[-+]", head[1:-1]) if h.strip()]
                assert len(names) == len(coeffs), (term.symbol, coeffs)
                # ...and the bracket's operators match the coefficients' signs.
                ops = re.findall(r"[-+]", head)
                assert ops == ["-" if c < 0 else "+" for c in coeffs[1:]], (head, coeffs)

                # 3. The note states the same sum, opening on the positive contribution.
                m = re.fullmatch(r"([\d.]+) = (.+), at t_RF ([\d.]+) ns", term.note)
                assert m, f"{leg}: unexpected gathered note {term.note!r}"
                assert float(m.group(1)) == pytest.approx(abs(total), abs=5e-3), term.note
                assert float(m.group(3)) == pytest.approx(trf, abs=5e-3), term.note
                bits = re.findall(r"([+-]?)\s*([\d.]+)\s+(\w+)", m.group(2))
                assert len(bits) == len(coeffs), (term.note, coeffs)
                assert not bits[0][0], f"{leg}: the note must not open with a sign: {term.note}"
                note_total = sum(-float(v) if s == "-" else float(v) for s, v, _r in bits)
                assert note_total == pytest.approx(abs(total), abs=5e-3), (
                    f"{leg}: the note says {m.group(2)!r} = {note_total:.3f} but claims "
                    f"{abs(total):.3f} — its signs are not relative to the hoisted operator")

    assert seen, "no gathered crossing found — has _gather_lane_terms stopped running?"
