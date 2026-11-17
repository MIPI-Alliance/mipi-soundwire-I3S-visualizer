"""The Timing page's columns at the calculator: what the page shows for every spec pairing,
computed from the inputs its seed tables give (`timing_page_inputs.column`) with
`find_worst_corner_rows_all` and `compute`, building no page.

For every pairing of the spec selectors, a device's t_DZ and its EDE Delay (the two ways of
deciding when to stop driving) are never priced in one column of one inequality, and every
term of a column's equation and substitution text is a clickable anchor on its own key. For
the three term-diagram setups, every term a column prints has a panel in each column: drawn
where the column holds it, with the note that the flights net to zero on the P→M and P→P contention legs only,
and "not in this column" where it does not; the EDE Delay terms appear where an EDE side is.

The page passes for what the page itself adds are in `test_timing_view_page.py` (the rows of
one pairing, and of the EDE buses) and `test_timing_diagram.py` (clicks on one setup). The one
test here that builds the page (gui) pins `column` against it: for every pairing, and the
variants the core tests use, the page reads the inputs `column` gives, and on the default pair
it shows the calculator's worst corners at them."""
import functools
import os
import re
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from timing_page_inputs import OFFERED, column, page, worst

from swi3s_studio.timing import CalcRow, ParamRange
from swi3s_studio.timing.calculator import INEQUALITIES
from swi3s_studio.timing.timeline import NET_ZERO_NOTE
from swi3s_studio.ui.timing_diagram import panels

pytestmark = pytest.mark.usefixtures("shared_compute")

_PAIRS = [(man, per) for man in OFFERED for per in OFFERED]
_SIDES = ("spec", "proposed")


@functools.lru_cache(maxsize=None)
def _worst_of(man, per, side):
    """`worst(*column(man, per, side))`, computed once for the tests that read it."""
    return worst(*column(man, per, side))


def _anchors(html):
    """[(key, shown text)] of every clickable term in a row, in order."""
    return [(k, re.sub(r"<[^>]+>", "", body))
            for k, body in re.findall(r'<a href="k:([^"]*)"[^>]*>(.*?)</a>', html)]


@pytest.mark.parametrize("man, per", _PAIRS, ids=[f"{m}-{p}" for m, p in _PAIRS])
def test_no_column_prices_a_release_by_t_dz_and_by_an_ede_delay_of_one_device(man, per):
    """Each column of the pairing, at each inequality's worst corner: no device's t_DZ and
    EDE Delay together, and the equation and substitution texts anchor every term on its key,
    in order."""
    for side in _SIDES:
        results, _ = _worst_of(man, per, side)
        for leg in INEQUALITIES:
            b = results[leg].breakdowns[leg]
            if b.not_applicable:
                continue
            keys = [page._term_key(t.symbol) for t in b.terms]
            for dev in ("Man", "Per"):
                assert not {f"{dev}_t_DZ", f"{dev}_t_EDE"} <= set(keys), (man, per, side, leg)
            eq = page.TimingView._equation_text(b.terms)
            assert eq.count("<a ") == len(_anchors(eq)), (man, per, side, leg)
            assert [k for k, _ in _anchors(eq)] == keys, (man, per, side, leg)
            sub = page.TimingView._numeric_only(b.terms)
            assert [k for k, _ in _anchors(sub)] == keys, (man, per, side, leg)


def _shown_clashes(terms):
    """The distinct symbols of `terms`, and the distinct crossings within them, that print
    alike (`_shown_symbol`), each clash as its sorted symbols."""
    out = []
    for parts in ([t.symbol for t in terms],
                  [x for t in terms for x in page._CROSSING_RE.findall(t.symbol)]):
        shown: dict = {}
        for raw in dict.fromkeys(parts):
            shown.setdefault(page._shown_symbol(raw), set()).add(raw)
        out += [sorted(v) for v in shown.values() if len(v) > 1]
    return out


def test_a_clash_of_two_crossings_that_print_alike_is_found():
    """The check below has teeth: two terms that differ only by ,late / ,early clash, as do
    an anchor correction or a ramp with and without ,fast; crossings that differ by ,fast
    do not."""
    from swi3s_studio.timing.calculator import Term
    late = Term("X_MP,rise,late · Man_t_RF,CLK", 1.0, op="+")
    early = Term("X_MP,rise,early · Man_t_RF,DATA", 1.0, op="+")
    assert _shown_clashes([late, early]) == [["X_MP,rise,early", "X_MP,rise,late"]]
    assert _shown_clashes([late, Term("X_MP,fall,early · Man_t_RF,CLK", 1.0, op="+")]) == []
    for sym in ("Man_t_DD,anchor,rr", "Per_t_ZD,anchor,fall", "Man_t_RF,DATA,rise"):
        pair = [Term(sym, 1.0, op="+"), Term(f"{sym},fast", 1.0, op="+")]
        assert _shown_clashes(pair) == [[sym, f"{sym},fast"]], sym
    assert _shown_clashes([Term("X_PM,rf,late · Per_t_RF,DATA", 1.0, op="+"),
                           Term("X_PM,rf,fast,late · Per_t_RF,DATA", 1.0, op="+")]) == []


@pytest.mark.parametrize("man, per", _PAIRS, ids=[f"{m}-{p}" for m, p in _PAIRS])
def test_no_two_terms_of_an_inequality_print_alike(man, per):
    """Each column of the pairing, at each inequality's worst corner: no two distinct terms,
    and no two distinct crossings within them, print alike once the crossings drop ,late and
    ,early, the anchor corrections and ramps drop ,fast and the keeper's response reads
    Man_t_Keeper."""
    for side in _SIDES:
        results, _ = _worst_of(man, per, side)
        for leg in INEQUALITIES:
            terms = results[leg].breakdowns[leg].terms
            assert not _shown_clashes(terms), (man, per, side, leg, _shown_clashes(terms))


def test_no_two_terms_of_an_inequality_print_alike_across_the_models_options():
    """The same at the calculator, over every spec pairing with EndDriveEarly on either side,
    0, 1 and 2 handover UIs, both ramp shapes, every launch mode, every P→P placement, a forced
    keeper, and windows that move which polarity binds; and the qualifiers it drops are on
    both kinds of crossing, late and early, and on anchor corrections and ramps, which it
    sees with and without ,fast."""
    import itertools

    from swi3s_studio.timing import SPEC_SOURCES, CalcInputs, compute
    from swi3s_studio.timing.calculator import PP_PLACEMENT_LABELS
    from swi3s_studio.timing.spec_source import LaunchMode
    windows = ({}, {"V_IH_min_frac": 0.40, "V_IH_max_frac": 0.60, "V_IL_min_frac": 0.30,
                    "V_IL_max_frac": 0.50, "tRF_Per_DATA_ns": 6.0},
               {"V_OH_min_frac": 0.85, "V_OL_max_frac": 0.25}, {"V_noise_pp_frac": 0.0})
    placements, modes = list(PP_PLACEMENT_LABELS), list(LaunchMode)
    seen, symbols = set(), set()
    for i, ((man, per), ede, n) in enumerate(itertools.product(
            itertools.product(SPEC_SOURCES, SPEC_SOURCES),
            ((False, False), (True, False), (False, True), (True, True)), (0.0, 1.0, 2.0))):
        inp = CalcInputs(Man_spec=man, Per_spec=per, Man_ede=ede[0], Per_ede=ede[1],
                         handover_UIs=n, Man_shape=("linear", "exp")[i % 2],
                         Per_shape=("linear", "exp")[i // 2 % 2],
                         launch_mode=modes[i % len(modes)],
                         pp_placement=placements[i % len(placements)],
                         Man_keeper_forced=i % 7 == 0, **windows[i % len(windows)])
        for leg, b in compute(inp).breakdowns.items():
            assert not _shown_clashes(b.terms), (inp, leg, _shown_clashes(b.terms))
            seen.update(x for t in b.terms for x in page._CROSSING_RE.findall(t.symbol))
            symbols.update(t.symbol for t in b.terms)
    assert any(x.endswith(",late") for x in seen) and any(x.endswith(",early") for x in seen)
    for kind in (r"_t_DD,anchor,[a-z]+", r"_t_ZD,anchor,[a-z]+", r"_t_RF,DATA,(rise|fall)"):
        assert any(re.search(kind + r",fast$", s) for s in symbols), kind
        assert any(re.search(kind + r"$", s) for s in symbols), kind


@pytest.mark.parametrize("man, per, ede_terms", [
    ("SWI3S_PHY2", "SWI3S_PHY2", set()),
    ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE", {"Man_t_EDE", "Per_t_EDE"}),
    ("SW13_1V2", "SWI3S_PHY2_EDE", {"Per_t_EDE"})], ids=["phy2", "ede", "sw-ede"])
def test_every_term_a_column_prints_has_a_panel_in_each_column(man, per, ede_terms):
    """The term diagram's sections for each inequality, as the page builds them
    (`_diagram_sections`), and the panels for each term either column prints: a column holding
    the term draws it, with the net-to-zero note exactly on the P→M and P→P contention legs; a
    column without it says so. The EDE Delay terms are printed exactly where `ede_terms` says."""
    worsts, picks = {}, {}
    for side in _SIDES:
        worsts[side], picks[side] = worst(*column(man, per, side))
    labels = page._column_labels_for(man, per)
    swept = set()
    for leg in INEQUALITIES:
        held = {side: {page._term_key(t.symbol) for t in worsts[side][leg].breakdowns[leg].terms}
                for side in _SIDES}
        keys = held["spec"] | held["proposed"]
        swept |= keys
        _title, sections = page._diagram_sections(leg, worsts, picks, labels)
        for key in keys:
            drawn = panels(sections, key, page._term_key)
            for col, side in enumerate(_SIDES):
                mine = [pn for pn in drawn if pn.column == col]
                assert mine, (leg, key, side)
                for pn in mine:
                    if key not in held[side]:
                        assert pn.timeline is None and "not in this column" in pn.note, (
                            leg, key, side, pn.note)
                        continue
                    assert pn.timeline is not None and pn.spans, (leg, key, side)
                    co = NET_ZERO_NOTE in pn.corner
                    assert co is (leg in ("PM_contention", "PP_contention")), (leg, pn.corner)
    assert ede_terms <= swept, ede_terms - swept
    for symbol in ("Man_t_EDE", "Per_t_EDE"):
        assert (symbol in swept) is (symbol in ede_terms), symbol


@pytest.mark.gui
def test_a_pinned_placements_corner_line_names_the_peripherals_distance():
    """Under All near the Manager and All at the far end, every inequality's corner line in
    each column (`_diagram_sections`) opens with the peripherals' distance, 0 cm or the bus
    length row's max, and names no Bus Length pick, and the whole-inequality footnote carries
    it; under a driver placement each P->P setup and hold leg's line names the driver's and the
    sampler's distance, and every other leg's names none; under Worst Per Inequality no line
    names a distance and P->M hold's names the Bus Length row's min."""
    from swi3s_studio.ui.inequality_diagram import footnote
    labels = page._column_labels_for("SWI3S_PHY2", "SWI3S_PHY2")
    cols = {side: column("SWI3S_PHY2", "SWI3S_PHY2", side) for side in _SIDES}
    far = {next(r for r in rows if r.name == "bus length (cm)").range.max
           for _base, rows in cols.values()}
    assert far == {30.0}
    pp = {"PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"}
    for placement, text, legs in (
            ("unknown", "", set()), ("both_near", "peripherals at 0 cm", set(INEQUALITIES)),
            ("both_far", "peripherals at 30 cm", set(INEQUALITIES)),
            ("driver_near", "driver at 0 cm, sampler at 30 cm", pp),
            ("driver_far", "driver at 30 cm, sampler at 0 cm", pp)):
        worsts, picks = {}, {}
        for side, (base, rows) in cols.items():
            worsts[side], picks[side] = worst(replace(base, pp_placement=placement), rows)
        for leg in INEQUALITIES:
            _title, sections = page._diagram_sections(leg, worsts, picks, labels)
            for s in sections:
                if s.timeline is None:
                    continue
                if leg in legs:
                    assert s.corner.startswith(f"Corner: {text}"), (placement, leg, s.corner)
                    assert "Bus Length" not in s.corner, (placement, leg, s.corner)
                else:
                    assert not any(w in s.corner for w in ("peripherals at", "driver at")), (
                        placement, leg, s.corner)
                    if leg == "PM_hold":
                        assert "Bus Length min" in s.corner, s.corner
            if leg in legs:
                assert text in footnote(sections), (placement, leg)


@pytest.mark.gui
def test_the_page_reads_the_inputs_its_seed_tables_give():
    """On the default pair the page shows each inequality at the calculator's worst corner of
    `column`'s inputs. Reset to every pairing, and to the variants the core tests use (both
    sides SoundWire at 12.288 MHz; a PHY2 Manager on an exponential ramp with a SoundWire
    Peripheral), each column's inputs are `column`'s."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    tv = page.TimingView()
    tv.flush_recompute()
    for side in _SIDES:
        assert tv._typ_baseline(side) == column("SWI3S_PHY2", "SWI3S_PHY2", side), side
        results, picked = worst(*column("SWI3S_PHY2", "SWI3S_PHY2", side))
        assert tv._worst[side] == results and tv._worst_picks[side] == picked, side
    tv._job.submit = lambda fn: 0         # the results are not under test from here
    for man, per in _PAIRS:
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        assert (tv._man_spec_combo.currentData(), tv._per_spec_combo.currentData()) == (man, per)
        for side in _SIDES:
            assert tv._typ_baseline(side) == column(man, per, side), (man, per, side)
    for man, per, kw in (("SW13_1V2", "SW13_1V2", {"fclk": 12.288}),
                         ("SWI3S_PHY2", "SW13_1V2", {"man_shape": "exp"})):
        tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData(man))
        tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData(per))
        tv._reset_to_defaults()
        if "fclk" in kw:
            tv._fclk.setValue(kw["fclk"])
        if "man_shape" in kw:
            tv._set_shape(tv._man_shape, kw["man_shape"])
        for side in _SIDES:
            assert tv._typ_baseline(side) == column(man, per, side, **kw), (man, per, kw, side)
    tv.close()


_EDE = ("SWI3S_PHY2_EDE", "SWI3S_PHY2_EDE")


def _margins(base, rows):
    results, _ = worst(base, rows)
    return {leg: results[leg].breakdowns[leg].margin_ns for leg in INEQUALITIES}


def _with_range(rows, name, lo, hi):
    return [CalcRow(r.name, r.binds, ParamRange(lo, 0.5 * (lo + hi), hi), r.linked_to,
                    r.is_window) if r.name == name else r for r in rows]


@pytest.mark.parametrize("dev, contention", [
    ("Man", {"MP_contention"}), ("Per", {"PM_contention", "PP_contention"})])
def test_an_ede_delay_rows_max_moves_only_contention_and_its_min_only_the_keeper(dev,
                                                                                 contention):
    """On the Example column with both sides on the EDE source, through the worst-corner
    search: a 1 ns higher EDE Delay maximum moves only the contention legs the device releases
    on, each 1 ns shorter, and a 1 ns higher minimum moves only the device's keeper leg, 1 ns
    longer."""
    base, rows = column(*_EDE, "proposed")
    name = f"{dev}_t_EDE"
    seeded = next(r.range for r in rows if r.name == name)
    assert (seeded.min, seeded.max) == (3.0, 7.0)
    before = _margins(base, rows)
    for lo, hi, legs, step in ((3.0, 8.0, contention, -1.0),
                               (4.0, 7.0, {f"keeper_{dev}"}, +1.0)):
        after = _margins(base, _with_range(rows, name, lo, hi))
        moved = {leg for leg in INEQUALITIES if abs(after[leg] - before[leg]) > 1e-9}
        assert moved == legs, (name, lo, hi, moved)
        for leg in legs:
            assert after[leg] - before[leg] == pytest.approx(step, abs=1e-9), (name, leg)


_EDE_PAIRS = [(man, per) for man, per in _PAIRS if "SWI3S_PHY2_EDE" in (man, per)]


@pytest.mark.parametrize("man, per", _EDE_PAIRS, ids=[f"{m}-{p}" for m, p in _EDE_PAIRS])
def test_the_specification_column_reads_no_ede_delay(man, per):
    """On every pairing with a side on the EDE source, the Specification column holds no EDE
    Delay row, releases neither side by one, and its margins are the same whatever EDE Delay
    its inputs carry."""
    base, rows = column(man, per, "spec")
    assert not {r.name for r in rows} & {"Man_t_EDE", "Per_t_EDE"}
    assert not base.Man_ede and not base.Per_ede
    held = _margins(base, rows)
    for ede in (0.0, 50.0):
        assert _margins(replace(base, Man_t_EDE_ns=ede, Per_t_EDE_ns=ede), rows) == held, ede
