"""The corner search's refinement of τ, α and δ inside their ranges (`_refine_fraction_row`):
samples bracket each minimum they mark and each bracket is minimised to `_REFINE_TOL_NS`."""

from dataclasses import replace as dc_replace

import pytest

from swi3s_studio.timing import (
    CalcInputs,
    ParamRange,
    apply_row_picks,
    compute,
    find_worst_corner_rows_all,
)
from swi3s_studio.timing.calculator import (
    _REFINE_SAMPLES,
    _REFINE_TOL_NS,
    RowPoint,
    _refine_fraction_row,
    default_swi3s_rows,
)

_TAU = "τ (per-edge slew)"


def _pos(pick):
    return pick.at if isinstance(pick, RowPoint) else {"min": 0.0, "typ": 0.5, "max": 1.0}[pick]


def test_the_corner_search_finds_a_tau_minimum_between_two_samples():
    """SWI3S PHY2 on both sides, 19.2 MHz, one handover UI, each slew row fixed (Man CLK
    7.95 ns, Man DATA 7.7 ns, Per DATA 7.55 ns), τ from 0 to 9 %, α to 15 % and δ to 10 %.
    MP_setup's margin along τ has a V-shaped minimum near 7.8 %, where Man DATA's slow edge
    reaches the end of its range: between the samples at three quarters and at max, and below
    every sample by more than 5 ps. The search reports τ inside that interval, and no point
    of a sweep of τ in steps of 1/500 of its range leaves less margin by more than
    `_REFINE_TOL_NS`."""
    ranges = {_TAU: (0.0, 0.045, 0.09), "t_RF Man CLK": (7.95,) * 3,
              "t_RF Man DATA": (7.7,) * 3, "t_RF Per DATA": (7.55,) * 3,
              "δ (V_SEOS tol)": (0.0, 0.05, 0.10), "α (V_noise / V_SEOS)": (0.0, 0.075, 0.15)}
    rows = [dc_replace(r, range=ParamRange(*ranges[r.name])) if r.name in ranges else r
            for r in default_swi3s_rows()]
    base = CalcInputs(Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", handover_UIs=1.0,
                      F_CLK_target_MHz=19.2)
    leg = "MP_setup"

    def margin(picks):
        return compute(apply_row_picks(base, rows, picks)).breakdowns[leg].margin_ns

    picks = find_worst_corner_rows_all((leg,), rows, base)[leg]
    tau = picks[_TAU]
    assert isinstance(tau, RowPoint) and 0.75 < tau.at < 1.0, picks
    found = margin(picks)
    samples = min(margin({**picks, _TAU: RowPoint(k / _REFINE_SAMPLES, 0.0)})
                  for k in range(_REFINE_SAMPLES + 1))
    assert found < samples - 5e-3, (found, samples)
    sweep = min(margin({**picks, _TAU: RowPoint(k / 500, 0.0)}) for k in range(501))
    assert found <= sweep + _REFINE_TOL_NS, (found, sweep)


def _refine(margin_of):
    """`_refine_fraction_row` on τ at the middle of a three-row combination."""
    (row,) = [r for r in default_swi3s_rows() if r.name == _TAU]
    held = ("typ", "typ", "max")
    return _refine_fraction_row(row, 1, margin_of(held), held, margin_of)


@pytest.mark.parametrize("vertex", [0.08, 0.37, 0.9])
def test_the_refinement_finds_a_v_shaped_minimum_inside_one_interval(vertex):
    """A margin |position − vertex|, its least point inside one interval between two samples,
    is found to `_REFINE_TOL_NS` from an end sample (0.08, 0.9) or an interior one (0.37), the
    other rows held."""
    m, c = _refine(lambda combo: abs(_pos(combo[1]) - vertex))
    assert m <= _REFINE_TOL_NS, (m, c)
    assert isinstance(c[1], RowPoint) and c[1].at == pytest.approx(vertex, abs=_REFINE_TOL_NS)
    assert (c[0], c[2]) == ("typ", "max")


def test_the_refinement_searches_every_bracket_the_samples_mark():
    """Two minima, a shallow one at 0.3 by the least sample and a deeper one at 0.8 by
    another sample no greater than its neighbours: the deeper one is the result."""
    def margin_of(combo):
        p = _pos(combo[1])
        return min((p - 0.3) ** 2 + 0.01, 0.5 * abs(p - 0.8))

    m, c = _refine(margin_of)
    assert m <= _REFINE_TOL_NS, (m, c)
    assert c[1].at == pytest.approx(0.8, abs=1e-3), c


def test_the_refinement_takes_no_evaluation_beyond_the_samples_of_a_row_a_leg_does_not_read():
    """A margin the row does not move is evaluated at the samples only, and the combination is
    returned as it was."""
    seen = []

    def margin_of(combo):
        seen.append(combo[1])
        return 1.0

    assert _refine(margin_of) == (1.0, ("typ", "typ", "max"))
    assert len(seen) == 1 + _REFINE_SAMPLES + 1, seen
