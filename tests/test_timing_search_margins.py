"""The margins the corner search reads (`_leg_margins`, `_margins_at`) and the runs `compute`
evaluates (`_runs_of`): each search margin is the one `compute` reports, from the same runs,
and a leg left out of a run could not have been the one `compute` reports there."""

import itertools
import random
from dataclasses import replace as dc_replace

import pytest
from timing_page_inputs import OFFERED, column

from swi3s_studio.timing import INEQUALITIES, apply_row_picks, compute
from swi3s_studio.timing.calculator import (
    _LEGS_READING_PERIPHERAL_B,
    _LEGS_READING_SLOW_EDGE,
    RowPoint,
    _compute_at,
    _leg_margins,
    _margins_at,
    _runs_of,
)

# Every pair the page offers, both columns, and the page's other choices on a few pairs: no
# handover UI, each placement, an exponential ramp, a forced keeper and a clocked launch.
_VARIANTS = (
    {}, {"handover_UIs": 0.0}, {"pp_placement": "driver_far"}, {"pp_placement": "both_far"},
    {"pp_placement": "both_near"}, {"Man_shape": "exp", "Per_shape": "exp"},
    {"Per_shape": "exp", "Man_keeper_forced": True},
)


def _cases():
    out = []
    for i, (man, per) in enumerate(itertools.product(OFFERED, OFFERED)):
        for side in ("spec", "proposed"):
            base, rows = column(man, per, side)
            for kw in (_VARIANTS if i % 5 == 0 else _VARIANTS[:2]):
                out.append((f"{man}/{per}/{side}/{kw}", dc_replace(base, **kw), rows))
    return out


_CASES = _cases()


def _corners(base, rows, n, seed):
    """`n` corners of `rows` about `base`: typ, then picks of min, typ, max and points inside
    the ranges at random."""
    rng = random.Random(seed)
    masters = [r for r in rows if r.linked_to is None and not r.is_window]
    yield base
    for _ in range(n - 1):
        picks = {}
        for r in masters:
            u = rng.random()
            picks[r.name] = (RowPoint(u, r.range.at_position(u)) if u < 0.2
                             else rng.choice(("min", "typ", "max")))
        yield apply_row_picks(base, rows, picks)


def _second_case(b):
    return ((b.sampling_rising is not None and b.sampling_rising == b.is_setup)
            or b.closing_rising is False)


@pytest.mark.parametrize("name,base,rows", _CASES, ids=[c[0] for c in _CASES])
def test_the_margins_the_corner_search_reads_are_the_ones_compute_reports(name, base, rows):
    """At corners of every pair and column the page offers: on every run `compute` evaluates,
    each leg's margin from `_margins_at` is `_compute_at`'s for that run, bit for bit, and so is
    whether its breakdown is the case `compute` breaks a tie against; and `_leg_margins` is
    the margin `compute` reports, for every leg."""
    for inp in _corners(base, rows, 3, name):
        for at, sup, legs in _runs_of(inp):
            got = _margins_at(at, sup, legs)
            full = _compute_at(at, sup).breakdowns
            assert set(got) == legs
            for leg in legs:
                assert got[leg] == (full[leg].margin_ns, _second_case(full[leg])), (leg, sup)
        reported = compute(inp).breakdowns
        assert _leg_margins(inp) == {leg: reported[leg].margin_ns for leg in INEQUALITIES}


def _b_readers(inp):
    """The legs whose margin or tie case differs between two supply assignments of `inp` that
    differ in Peripheral B's supply alone, on either data-edge set."""
    out = set()
    ends = sorted({s[2] for s in inp.supply_corners()})
    for edges in {frozenset(), frozenset(inp.fast_edge_lanes())}:
        at = dc_replace(inp, fast_data_edge=edges)
        for m, a in {s[:2] for s in inp.supply_corners()}:
            runs = [_compute_at(at, (m, a, b)).breakdowns for b in ends]
            out |= {leg for leg in INEQUALITIES
                    if len({(r[leg].margin_ns, _second_case(r[leg])) for r in runs}) > 1}
    return out


@pytest.mark.parametrize("name,base,rows", _CASES, ids=[c[0] for c in _CASES])
def test_only_the_pp_legs_read_peripheral_bs_supply(name, base, rows):
    """A leg outside `_LEGS_READING_PERIPHERAL_B` has the same margin and tie case at two supply
    assignments that differ in Peripheral B's supply alone, so `_runs_of` evaluates it at B's
    first end only."""
    for inp in _corners(base, rows, 2, name):
        assert _b_readers(inp) <= _LEGS_READING_PERIPHERAL_B


def test_every_leg_listed_as_reading_peripheral_bs_supply_reads_it():
    """Every leg `_LEGS_READING_PERIPHERAL_B` lists reads B's supply at a corner of the SWI3S
    PHY2 page."""
    base, rows = column("SWI3S_PHY2", "SWI3S_PHY2", "spec")
    seen = set()
    for inp in _corners(base, rows, 6, "B"):
        seen |= _b_readers(inp)
    assert seen == _LEGS_READING_PERIPHERAL_B, sorted(_LEGS_READING_PERIPHERAL_B ^ seen)


@pytest.mark.parametrize("name,base,rows", _CASES, ids=[c[0] for c in _CASES])
def test_only_the_listed_legs_read_a_data_lanes_slow_edge(name, base, rows):
    """With each DATA lane's slow-edge role on the fast edge (`CalcInputs.fast_data_edge`), a
    leg `_LEGS_READING_SLOW_EDGE` does not list for that lane has the margin and tie case it
    has on the slow edge, at every supply assignment, so `_runs_of` evaluates it on the slow
    set only."""
    for inp in _corners(base, rows, 2, name):
        for lane, readers in _LEGS_READING_SLOW_EDGE.items():
            fast = dc_replace(inp, fast_data_edge=frozenset({lane}))
            for sup in inp.supply_corners():
                slow_b, fast_b = _compute_at(inp, sup).breakdowns, _compute_at(fast, sup).breakdowns
                for leg in INEQUALITIES:
                    if leg not in readers:
                        assert (slow_b[leg].margin_ns, _second_case(slow_b[leg])) == (
                            fast_b[leg].margin_ns, _second_case(fast_b[leg])), (lane, leg, sup)
