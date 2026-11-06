"""F_max on legs whose terms scale with the UI, and the Manager notes under a clocked launch.

- A clocked Manager t_DD / t_ZD sits at a fixed fraction of the UI, so without floors a setup
  leg's F_max is solved with that fraction held: re-evaluated at the reported rate, the margin
  is zero.
- With floors (`CalcInputs.Man_t_DD_floor_ns` and its siblings) each clocked row is re-placed
  at every rate as the Timing Calculator re-places it, raised to a further grid point where
  its fraction falls below the floor, and F_max is the FIRST FAILURE, the lowest rate at
  which the leg fails as the rate rises with the rows so placed: it passes just below and
  fails just above, the margin reaching zero there or a placement step taking it through
  zero. A leg failing at the lowest rates reads 0.0, and the F_max note names any higher
  rates at which a leg passes again.
- An EDE release (launch + ramp to 80 % + EDE Delay) is referenced to the closing edge, so a
  contention leg on an EDE release is rate-dependent even at N_HO = 0; under a clocked launch
  the launch part scales with the UI too.
- Under a clocked launch the MP_setup / MP_hold Man_t_DD terms carry the snapped value, and
  their note says so.
- Hold, handover-hold and keeper legs whose margin carries a UI-scaled part (a clocked Manager
  t_DD / t_ZD / t_DZ, N_HO·UI, a conventional release's closing UI) report a finite F_max, and
  `F_max_binding_MHz` / `binding_inequality` read every bounding leg's F_max: just below the
  binding rate every bounding leg passes, just above it the binding leg fails.
- An EndDriveEarly keeper leg carries no UI, so it is rate-independent.
- The window solver: `_piece_windows` on a margin with no constant part, `_rate_windows` at
  a UI of zero, and the F_max note `_fmax_from_windows` builds, three later windows named
  and more counted and spanned. The floored solve searches up to `_FLOORED_FMAX_CAP_MHZ`: a
  first failure below the cap is reported, a leg passing at the cap reads inf.

Every test here solves without the toggle ceiling (the `no_toggle_ceiling` fixture), so the
solver is checked at rates past it; test_timing pins the ceiling.

Run: PYTHONPATH=. python3 -m pytest tests/test_timing_fmax_rate.py
"""
import math
import re
from dataclasses import replace

import pytest

from swi3s_studio.timing.calculator import CalcInputs, compute
from swi3s_studio.timing.spec_source import (
    REF_TRF_NS,
    SNAP_TOL_NS,
    LaunchMode,
    launch_grid_UI,
    launch_mode_max_fclk_MHz,
    preset,
    snap_to_launch_grid,
)

pytestmark = pytest.mark.usefixtures("no_toggle_ceiling")

_PROP = dict(Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP")
# The PHY2 revision's Manager rows, whose tabulated minima the Timing Calculator floors at.
_REV_MAN = preset("SWI3S_PHY2_PROP", "Man")


def _clocked(mode: LaunchMode, f_MHz: float = 12.288, **kw) -> CalcInputs:
    """A clocked-launch column with every governed Manager row on the grid at `f_MHz`."""
    base = CalcInputs(**_PROP, launch_mode=mode, F_CLK_target_MHz=f_MHz,
                      Per_t_IH_max_ns=1.5, **kw)
    dd = snap_to_launch_grid(_REV_MAN.t_DD_min_ns, mode, f_MHz, base.duty_min,
                             floor_ns=_REV_MAN.t_DD_min_ns)
    zd = snap_to_launch_grid(base.Man_t_ZD_ns, mode, f_MHz, base.duty_min)
    dz = snap_to_launch_grid(base.Man_t_DZ_max_ns, mode, f_MHz, base.duty_min)
    return replace(base, Man_t_DD_max_ns=dd, Man_t_DD_min_ns=dd,
                   Man_t_ZD_ns=zd, Man_t_DZ_max_ns=dz)


def _at_rate_holding_placement(inp: CalcInputs, f_MHz: float) -> CalcInputs:
    """`inp` at another rate, each clocked Manager row kept at its fraction of the UI."""
    ui0 = inp.UI_ns()
    out = replace(inp, F_CLK_target_MHz=f_MHz)
    k = out.UI_ns() / ui0
    return replace(out, Man_t_DD_max_ns=inp.Man_t_DD_max_ns * k,
                   Man_t_DD_min_ns=inp.Man_t_DD_min_ns * k,
                   Man_t_ZD_ns=inp.Man_t_ZD_ns * k,
                   Man_t_DZ_max_ns=inp.Man_t_DZ_max_ns * k)


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
@pytest.mark.parametrize("leg", ["MP_setup", "MP_setup_ho"])
def test_a_clocked_setup_fmax_closes_the_leg_when_evaluated_at_that_rate(mode, leg):
    inp = _clocked(mode)
    b = compute(inp).breakdowns[leg]
    assert b.margin_ns > 0, (leg, b.margin_ns)
    assert math.isfinite(b.F_max_MHz) and b.F_max_MHz > inp.F_CLK_target_MHz
    at = compute(_at_rate_holding_placement(inp, b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, mode, b.F_max_MHz)
    # Just above it the leg fails, just below it passes.
    assert compute(_at_rate_holding_placement(inp, b.F_max_MHz * 1.01)
                   ).breakdowns[leg].margin_ns < 0
    assert compute(_at_rate_holding_placement(inp, b.F_max_MHz * 0.99)
                   ).breakdowns[leg].margin_ns > 0


def test_the_2x_setup_fmax_agrees_with_the_launch_hint():
    """The 2x setup leg and `launch_mode_max_fclk_MHz` solve the same inequality: evaluated
    at the hint's rate with the placement held, the leg's margin is zero."""
    inp = _clocked(LaunchMode.CLK2)
    b = compute(inp).breakdowns["MP_setup"]
    f_hint, bound = launch_mode_max_fclk_MHz(LaunchMode.CLK2)
    assert bound == "setup"
    assert b.F_max_MHz == pytest.approx(f_hint, rel=1e-3)
    at = compute(_at_rate_holding_placement(inp, f_hint)).breakdowns["MP_setup"]
    assert at.margin_ns == pytest.approx(0.0, abs=5e-3), (f_hint, at.margin_ns)


def test_an_analog_setup_fmax_is_the_closed_form():
    inp = CalcInputs()
    b = compute(inp).breakdowns["MP_setup"]
    assert b.F_max_MHz == pytest.approx(1000.0 * inp.duty_min / b.required_ns, rel=1e-12)


_EDE_CASES = [
    # (leg, overrides)
    ("PM_contention", dict(Per_ede=True, Per_t_EDE_ns=20.0, handover_UIs=0.0)),
    ("PM_contention", dict(Per_ede=True, Per_t_EDE_ns=20.0, handover_UIs=1.0,
                           F_CLK_target_MHz=13.2)),
    ("PP_contention", dict(Per_ede=True, Per_t_EDE_ns=20.0, handover_UIs=0.0,
                           F_CLK_target_MHz=9.6)),
    # A Manager release on an analog launch.
    ("MP_contention", dict(Man_ede=True, Man_t_EDE_ns=7.0, Per_t_ZD_ns=0.0,
                           handover_UIs=0.0, bus_length_cm=0.0)),
]


@pytest.mark.parametrize("leg,kw", _EDE_CASES)
def test_an_ede_contention_fmax_is_where_the_margin_reaches_zero(leg, kw):
    inp = replace(CalcInputs(), **{"F_CLK_target_MHz": 12.288, **kw})
    b = compute(inp).breakdowns[leg]
    assert 0.0 < b.F_max_MHz < math.inf, (leg, kw, b.F_max_MHz, b.margin_ns)
    at = compute(replace(inp, F_CLK_target_MHz=b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, kw)
    # Pass at the target exactly when the target is at or below F_max.
    assert (b.margin_ns >= 0) == (inp.F_CLK_target_MHz <= b.F_max_MHz), (leg, kw)


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_clocked_manager_ede_release_fmax_holds_the_launch_placement(mode):
    """A Manager EDE release on a clocked launch: the launch is a fraction of the UI, the
    ramp and the EDE Delay are not."""
    inp = _clocked(mode, Man_ede=True, Man_t_EDE_ns=7.0, Per_t_ZD_ns=0.0,
                   handover_UIs=0.0, bus_length_cm=0.0)
    b = compute(inp).breakdowns["MP_contention"]
    assert 0.0 < b.F_max_MHz < math.inf, (mode, b.F_max_MHz)
    at = compute(_at_rate_holding_placement(inp, b.F_max_MHz)).breakdowns["MP_contention"]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), mode


def test_a_zero_ui_ede_deficit_is_closed_by_a_slower_clock():
    """At N_HO = 0 a peripheral EDE release still moves with the UI, so F_max is not 0.0."""
    inp = CalcInputs(Per_ede=True, Per_t_EDE_ns=20.0, handover_UIs=0.0,
                     F_CLK_target_MHz=12.288)
    b = compute(inp).breakdowns["PM_contention"]
    assert b.margin_ns < 0
    assert 9.6 < b.F_max_MHz < 12.288
    assert compute(replace(inp, F_CLK_target_MHz=9.6)
                   ).breakdowns["PM_contention"].margin_ns > 0


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_clocked_manager_turn_on_moves_the_pm_contention_fmax(mode):
    """P->M: the Manager acquires with a clocked t_ZD, a fraction of the UI."""
    inp = _clocked(mode, handover_UIs=1.0)
    b = compute(inp).breakdowns["PM_contention"]
    assert 0.0 < b.F_max_MHz < math.inf, (mode, b.F_max_MHz)
    at = compute(_at_rate_holding_placement(inp, b.F_max_MHz)).breakdowns["PM_contention"]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), mode


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_clocked_manager_release_that_never_contends_reports_no_ceiling(mode):
    """M->P: the clocked release is less than the allocated UI, and the rest is positive.

    The margin is then `(1 − p_DZ)·UI + B` with B > 0, non-negative at every rate.
    """
    inp = _clocked(mode, handover_UIs=1.0)
    b = compute(inp).breakdowns["MP_contention"]
    assert b.F_max_MHz == math.inf
    for f in (20.0, 50.0, 200.0):
        assert compute(_at_rate_holding_placement(inp, f)
                       ).breakdowns["MP_contention"].margin_ns > 0, (mode, f)


def _at_rate(inp: CalcInputs, f_MHz: float) -> CalcInputs:
    """`inp` at another rate: clocked Manager rows held at their UI fraction, analog ones as
    they are."""
    if launch_grid_UI(inp.launch_mode) is None:
        return replace(inp, F_CLK_target_MHz=f_MHz)
    return _at_rate_holding_placement(inp, f_MHz)


def test_the_4x_hold_fmax_agrees_with_the_launch_hint():
    """4x is bounded by hold: MP_hold's F_max is `launch_mode_max_fclk_MHz`'s 4x answer, and
    evaluated at that rate with the placement held the leg's margin is zero."""
    inp = _clocked(LaunchMode.CLK4)
    b = compute(inp).breakdowns["MP_hold"]
    f_hint, bound = launch_mode_max_fclk_MHz(LaunchMode.CLK4)
    assert bound == "hold"
    assert b.F_max_MHz == pytest.approx(f_hint, rel=1e-3)
    at = compute(_at_rate_holding_placement(inp, f_hint)).breakdowns["MP_hold"]
    assert at.margin_ns == pytest.approx(0.0, abs=5e-3), (f_hint, at.margin_ns)


_HOLD_CASES = [
    # (leg, inputs) -- each leg's margin carries a UI-scaled part and closes at a finite rate.
    ("MP_hold", _clocked(LaunchMode.CLK4)),
    ("MP_hold", _clocked(LaunchMode.CLK2)),
    ("MP_hold_ho", _clocked(LaunchMode.CLK4, handover_UIs=0.0)),
    ("keeper_Man", CalcInputs(Man_t_DZ_max_ns=0.0, F_CLK_target_MHz=12.288)),
    ("keeper_Man", _clocked(LaunchMode.CLK4)),
    ("keeper_Per", CalcInputs(Per_t_DZ_max_ns=0.0, F_CLK_target_MHz=12.288)),
    ("PM_hold_ho", CalcInputs(Per_t_ZD_ns=0.0, Per_spec="SW13_1V2", Man_spec="SW13_1V2",
                              Man_t_IH_max_ns=40.0, F_CLK_target_MHz=6.0)),
]


@pytest.mark.parametrize("leg,inp", _HOLD_CASES)
def test_a_ui_scaled_non_setup_leg_reports_the_rate_where_it_closes(leg, inp):
    b = compute(inp).breakdowns[leg]
    assert not b.is_setup
    assert 0.0 < b.F_max_MHz < math.inf, (leg, b.F_max_MHz, b.margin_ns)
    at = compute(_at_rate(inp, b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, b.F_max_MHz)
    assert compute(_at_rate(inp, b.F_max_MHz * 1.01)).breakdowns[leg].margin_ns < 0
    assert compute(_at_rate(inp, b.F_max_MHz * 0.99)).breakdowns[leg].margin_ns > 0


def test_a_rate_independent_hold_leg_still_reads_inf_or_zero():
    """No UI-scaled part: infinity when it passes, 0.0 when it fails."""
    r = compute(CalcInputs())
    assert r.breakdowns["PM_hold"].margin_ns > 0 and r.breakdowns["PM_hold"].F_max_MHz == math.inf
    assert r.breakdowns["PP_hold"].margin_ns < 0 and r.breakdowns["PP_hold"].F_max_MHz == 0.0


_BINDING_CASES = [
    # (binding leg, inputs)
    ("PM_contention", CalcInputs(Per_ede=True, Per_t_EDE_ns=20.0, handover_UIs=0.0,
                                 F_CLK_target_MHz=9.6, Man_t_DD_min_ns=12.0,
                                 Per_t_DD_min_ns=8.0)),
    ("MP_hold", _clocked(LaunchMode.CLK4, Per_t_DD_max_ns=2.0, Per_t_ZD_ns=2.0)),
    ("keeper_Man", CalcInputs(Man_t_DZ_max_ns=0.0, Man_t_DD_min_ns=12.0, Man_t_DD_max_ns=21.0,
                              Per_t_DD_max_ns=6.0, Per_t_IS_max_ns=2.0, t_keeper_ns=4.0,
                              F_CLK_target_MHz=12.288)),
]


@pytest.mark.parametrize("leg,inp", _BINDING_CASES)
def test_every_bounding_leg_closes_just_below_the_binding_rate(leg, inp):
    """F_max_binding is the lowest first failure over the bounding set: just below it every
    bounding leg passes.

    Each case is bound by a NON-setup leg, which a setup-only minimum misses: it reports a
    higher rate at which that leg already fails.
    """
    r = compute(inp)
    f = r.F_max_binding_MHz
    assert 0.0 < f < math.inf, (leg, f)
    assert r.binding_inequality == leg, (r.binding_inequality, f)
    below = compute(_at_rate(inp, f * 0.999))._bounding()
    assert all(b.margin_ns >= 0 for b in below.values()), \
        {k: b.margin_ns for k, b in below.items() if b.margin_ns < 0}
    assert compute(_at_rate(inp, f * 1.001)).breakdowns[leg].margin_ns < 0


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_the_mp_notes_name_the_value_in_force_under_a_clocked_launch(mode):
    """An off-grid Man_t_DD is snapped onto the launch grid, the term carries the snapped
    value, and its note says so. A SWI3S Manager's launch has no anchor correction."""
    inp = CalcInputs(launch_mode=mode, F_CLK_target_MHz=12.288,
                     Man_t_DD_max_ns=18.0, Man_t_DD_min_ns=7.0)
    snapped = snap_to_launch_grid(18.0, mode, 12.288, inp.duty_min)
    assert not math.isclose(snapped, 18.0, abs_tol=5e-3), "18.0 ns must be off the grid"
    r = compute(inp)
    for leg in ("MP_setup", "MP_hold"):
        terms = r.breakdowns[leg].terms
        t = next(t for t in terms if t.symbol == "Man_t_DD")
        assert abs(t.value_ns) == pytest.approx(snapped, abs=1e-9), (leg, t.value_ns)
        assert "18.00 ns is not on the" in t.note, (leg, t.note)
        on_grid = re.search(r"snapped to ([0-9]+\.[0-9]+) ns", t.note)
        assert on_grid and float(on_grid.group(1)) == pytest.approx(snapped, abs=5e-3), \
            (leg, t.note)
        assert not any(x.symbol.startswith("Man_t_DD,anchor") for x in terms), leg


def test_a_soundwire_manager_launch_is_its_equivalent_plus_its_anchor_correction():
    """A SoundWire Manager's t_DD rows hold its SWI3S equivalents; the launch shows the row
    and the anchor correction that moves it to the operating slews, with its provenance: the
    correction's note names the slews the row's equivalent is derived at
    (`sw13_equivalent_slews` at the row's corner), the operating slews and the correction
    itself. At the slews the max equivalent is derived at the correction is zero and is no
    term."""
    from swi3s_studio.timing.spec_source import sw13_equivalent_slews

    base = CalcInputs(Man_spec="SW13_1V2", Per_spec="SWI3S_PHY2", F_CLK_target_MHz=10.0,
                      Man_t_DD_min_ns=9.7, Man_t_DD_max_ns=26.7)
    assert not any(t.symbol.startswith("Man_t_DD,anchor")
                   for t in compute(base).breakdowns["MP_setup"].terms)
    inp = replace(base, tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3)
    r = compute(inp)
    for leg, field, corner in (("MP_setup", "Man_t_DD_max_ns", "max"),
                               ("MP_hold", "Man_t_DD_min_ns", "min")):
        terms = r.breakdowns[leg].terms
        t = next(t for t in terms if t.symbol == "Man_t_DD")
        assert abs(t.value_ns) == pytest.approx(getattr(inp, field))
        corr = next(t for t in terms if t.symbol.startswith("Man_t_DD,anchor,"))
        assert "SoundWire" in corr.note and "slews it is derived at" in corr.note, corr.note
        k, s_ = sw13_equivalent_slews("SW13_1V2", "Man", corner=corner)
        assert f"derived at (k = {k:.2f}, s = {s_:.2f} ns)" in corr.note, (leg, corr.note)
        assert "at k = 8.30" in corr.note, (leg, corr.note)
        # The anchor's move is charged on setup and credited on hold.
        moved = -corr.value_ns if corner == "max" else corr.value_ns
        assert f"moves by {moved:+.2f} ns" in corr.note, (leg, corr.note)


def test_the_soundwire_min_corner_starts_its_ramp_at_low_impedance():
    """The t_DD,min slot is t_ZD_Data,Min's equivalent, measured to 20 % of the ramp at the
    reference slew. At its anchor, at any data slew, the ramp starts at SoundWire's own
    low-impedance instant: t_ZD_Data,Min = 8.1 ns after the 50 % clock, the equivalent less
    5.0/3 rounded down. The min corner reads an early data edge (`edge_factor`)."""
    from swi3s_studio.timing.calculator import _launch_anchor_ns, anchor_frac, edge_factor

    p = preset("SW13_1V2", "Per")
    assert p.t_DD_min_ns == p.t_ZD_min_ns
    for s_ in (2.0, 3.5, 5.0):
        inp = CalcInputs(Per_spec="SW13_1V2", Per_t_DD_min_ns=p.t_DD_min_ns,
                         tRF_Per_DATA_ns=s_)
        edge = edge_factor(inp, "Per DATA", "early") * s_
        start = _launch_anchor_ns(inp, "Per", "DD", "min") - anchor_frac(inp, "Per") * edge
        assert start == pytest.approx(p.t_DD_min_ns - REF_TRF_NS / 3.0, abs=1e-12), s_
        assert start <= 8.1


def _growing_release() -> CalcInputs:
    """A clocked Manager release at 0.75 UI with no handover UI: MP_contention's UI-scaled
    part is negative, so the leg fails at the target and passes at higher rates."""
    inp = _clocked(LaunchMode.CLK4, handover_UIs=0.0)
    return replace(inp, Man_t_DZ_max_ns=0.75 * inp.UI_ns(), Per_t_ZD_ns=0.0,
                   bus_length_cm=0.0)


_GROWING = [
    pytest.param(_growing_release(), id="clocked-release"),
    # A Manager EDE release on a 4x launch at 30 MHz.
    pytest.param(CalcInputs(Man_ede=True, launch_mode=LaunchMode.CLK4, F_CLK_target_MHz=30.0,
                            Man_t_DD_min_ns=18.0, Man_t_DD_max_ns=18.0, handover_UIs=0.0,
                            Per_t_ZD_ns=8.0, bus_length_cm=0.0, Man_t_EDE_ns=3.0),
                 id="ede-release"),
]


@pytest.mark.parametrize("inp", _GROWING)
def test_a_leg_failing_where_its_margin_grows_with_the_rate_binds_at_zero(inp):
    """F_max is the first failure as the rate rises from zero: a leg that fails at the target
    and at every lower rate reads 0.0, its rate bound is 0.0, so F_max_binding is pinned
    there and the failure shows, and its note names the rates from which it passes."""
    res = compute(inp)
    b = res.breakdowns["MP_contention"]
    assert b.margin_ns < 0 and b.F_max_MHz == 0.0, (b.margin_ns, b.F_max_MHz)
    assert res._rate_bound_MHz(b) == 0.0
    m = re.match(r"passes from ([0-9.]+) (?:MHz up|to ([0-9.]+) MHz)", b.F_max_note)
    assert m, b.F_max_note
    f_pass = float(m.group(1))
    assert f_pass > inp.F_CLK_target_MHz
    checks = [(f_pass * 0.99, False), (f_pass * 1.01, True)]
    if m.group(2) is None:                       # an open window passes at every higher rate
        checks.append((f_pass * 10, True))
    for f, passes in checks:
        margin = compute(_at_rate_holding_placement(inp, f)).breakdowns["MP_contention"].margin_ns
        assert (margin >= 0) is passes, (f, margin)
    assert res.F_max_binding_MHz == 0.0
    assert res.breakdowns[res.binding_inequality].margin_ns < 0
    passing = compute(CalcInputs()).breakdowns["PM_hold"]
    assert passing.margin_ns > 0 and math.isinf(passing.F_max_MHz)
    assert res._rate_bound_MHz(passing) == passing.F_max_MHz


@pytest.mark.parametrize("leg", ["keeper_Per", "keeper_Man"])
def test_an_ede_keeper_leg_is_rate_independent(leg):
    """An EndDriveEarly keeper leg is its ramp to V_OH / V_OL and its EDE Delay less its
    crossing of the keeper's threshold and the keeper response: no UI in it, so a positive
    margin has no rate ceiling and the leg does not bound `F_max_binding_MHz`."""
    res = compute(CalcInputs(Per_ede=True, Man_ede=True))
    b = res.breakdowns[leg]
    assert b.margin_ns > 0 and math.isinf(b.F_max_MHz)
    assert res._rate_bound_MHz(b) == math.inf


# The Timing Calculator's floors for the PHY2 revision's Manager rows: each row's tabulated
# minimum, below which a clocked placement is raised to the next grid point.
_FLOORS = dict(Man_t_DD_floor_ns=_REV_MAN.t_DD_min_ns, Man_t_ZD_floor_ns=_REV_MAN.t_ZD_min_ns,
               Man_t_DZ_floor_ns=_REV_MAN.t_DZ_min_ns)


def _replaced_at(inp: CalcInputs, f_MHz: float) -> CalcInputs:
    """`inp` at another rate with each clocked Manager row re-placed as the Timing Calculator
    re-places it: its UI fraction kept, raised to the lowest grid point at or above its floor."""
    def place(v, floor):
        return snap_to_launch_grid(v * inp.F_CLK_target_MHz / f_MHz, inp.launch_mode, f_MHz,
                                   inp.duty_min, floor_ns=floor)
    return replace(inp, F_CLK_target_MHz=f_MHz,
                   Man_t_DD_max_ns=place(inp.Man_t_DD_max_ns, inp.Man_t_DD_floor_ns),
                   Man_t_DD_min_ns=place(inp.Man_t_DD_min_ns, inp.Man_t_DD_floor_ns),
                   Man_t_ZD_ns=place(inp.Man_t_ZD_ns, inp.Man_t_ZD_floor_ns),
                   Man_t_DZ_max_ns=place(inp.Man_t_DZ_max_ns, inp.Man_t_DZ_floor_ns))


def _floored_margin(inp: CalcInputs, leg: str, f_MHz: float) -> float:
    return compute(_replaced_at(inp, f_MHz)).breakdowns[leg].margin_ns


def _floored_cases():
    """The floored legs with a finite F_max, solved without the toggle ceiling: collection
    runs before the `no_toggle_ceiling` fixture does, so this lifts it itself."""
    from swi3s_studio.timing import calculator
    out = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calculator, "toggle_ceiling_MHz", lambda inp: math.inf)
        for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
            inp = replace(_clocked(mode), **_FLOORS)
            for leg, b in compute(inp).breakdowns.items():
                if 0.0 < b.F_max_MHz < 1e4:
                    out.append(pytest.param(inp, leg, id=f"{mode.value}-{leg}"))
    return out


# The legs whose floored F_max lies strictly between zero and the search's cap, in both modes.
_FLOORED_LEGS = ("MP_setup", "MP_setup_ho", "PM_setup", "PM_setup_ho", "PP_setup",
                 "PP_setup_ho", "PM_contention", "PP_contention", "keeper_Man", "keeper_Per")


def test_the_floored_cases_hold_every_rate_bounded_leg_in_both_modes():
    """The floored parametrisation is computed from the model, so it is pinned here: a change
    that turns one of these legs' F_max into 0.0 or inf fails rather than dropping a case."""
    got = {p.id for p in _floored_cases()}
    assert got == {f"{m.value}-{leg}" for m in (LaunchMode.CLK2, LaunchMode.CLK4)
                   for leg in _FLOORED_LEGS}, sorted(got)


@pytest.mark.parametrize("inp,leg", list(_floored_cases()))
def test_a_floored_clocked_fmax_is_the_first_rate_the_re_placed_leg_fails(inp, leg):
    """With floors, each leg passes just below its F_max and fails just above it, with its
    clocked rows re-placed at each rate: the margin reaches zero there, or a placement steps
    there and the margin jumps through zero. It passes at every lower rate: a floored row's
    placement steps where n grid points reach its floor, f = 1000·duty·grid·n / floor, and
    between two steps every placement keeps its UI fraction, so the margin is A·UI + B and
    monotone in the rate, and passing at both ends of each such span below F_max is passing
    throughout it."""
    f_max = compute(inp).breakdowns[leg].F_max_MHz
    below = _floored_margin(inp, leg, f_max * (1 - 1e-7))
    above = _floored_margin(inp, leg, f_max * (1 + 1e-7))
    assert below >= -1e-6 and above < 0.0
    if below - above < 1e-3:
        assert _floored_margin(inp, leg, f_max) == pytest.approx(0.0, abs=1e-6)
    grid = launch_grid_UI(inp.launch_mode)
    steps = set()
    for floor in (inp.Man_t_DD_floor_ns, inp.Man_t_ZD_floor_ns, inp.Man_t_DZ_floor_ns):
        if floor is None or floor <= SNAP_TOL_NS:
            continue
        n = 1
        while (f := 1000.0 * inp.duty_min * grid * n / (floor - SNAP_TOL_NS)) < f_max:
            steps.add(f)
            n += 1
    ends = [f_max * 1e-3, *sorted(steps), f_max]
    for lo, hi in zip(ends, ends[1:]):
        for f in (lo * (1 + 1e-9), hi * (1 - 1e-7)):
            assert _floored_margin(inp, leg, f) >= -1e-6, (leg, f, f_max)


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_floored_keeper_fmax_past_a_placement_step_closes_the_leg(mode):
    """keeper_Man pays the clocked Man_t_DD, which the floor raises to a further grid point
    before the leg closes: re-placed at F_max the margin is zero."""
    inp = replace(_clocked(mode), **_FLOORS)
    f_max = compute(inp).breakdowns["keeper_Man"].F_max_MHz
    at = _replaced_at(inp, f_max)
    assert at.Man_t_DD_max_ns / at.UI_ns() > inp.Man_t_DD_max_ns / inp.UI_ns() + 1e-9
    assert compute(at).breakdowns["keeper_Man"].margin_ns == pytest.approx(0.0, abs=1e-6)


def test_a_floored_setup_fmax_stops_at_the_step_that_fails_it():
    """Under 2x the floor raises Man_t_DD from 0.50 to 1.00 UI where 0.50 UI drops below
    its floor, and MP_setup still passing there fails past it: F_max is that rate."""
    inp = replace(_clocked(LaunchMode.CLK2), **_FLOORS)
    step = 0.5 * 1000.0 * inp.duty_min / (_FLOORS["Man_t_DD_floor_ns"] - SNAP_TOL_NS)
    assert compute(inp).breakdowns["MP_setup"].F_max_MHz == pytest.approx(step, rel=1e-9)


def test_a_floored_hold_failing_below_a_step_reports_its_first_failure():
    """A 4x Man_t_DD at 0.25 UI funds hold until the floor raises it to 0.50 UI: a leg
    failing at the target passes at low rates, fails from below the target up to the step,
    passes past it and fails again once the raised placement falls short. Its F_max is
    the first failure, below the target, and its note names the windows past the step."""
    inp = replace(_clocked(LaunchMode.CLK4), **_FLOORS)
    m0 = compute(inp).breakdowns["MP_hold"].margin_ns
    inp = replace(inp, Per_t_IH_max_ns=inp.Per_t_IH_max_ns + m0 + 0.09)
    b = compute(inp).breakdowns["MP_hold"]
    assert b.margin_ns == pytest.approx(-0.09)
    step = 0.25 * 1000.0 * inp.duty_min / (_FLOORS["Man_t_DD_floor_ns"] - SNAP_TOL_NS)
    assert _floored_margin(inp, "MP_hold", step * 0.999) < 0.0
    assert _floored_margin(inp, "MP_hold", step * 1.001) > 0.0
    assert b.F_max_MHz < inp.F_CLK_target_MHz < step
    assert _floored_margin(inp, "MP_hold", b.F_max_MHz) == pytest.approx(0.0, abs=1e-6)
    assert _floored_margin(inp, "MP_hold", b.F_max_MHz * 0.9999) > 0.0
    assert _floored_margin(inp, "MP_hold", b.F_max_MHz * 1.0001) < 0.0
    # The first window named after a count-and-span header, if the note has one.
    spans = re.findall(r"from ([0-9.]+) to ([0-9.]+) MHz", b.F_max_note)
    assert b.F_max_note.startswith("passes again ") and spans, b.F_max_note
    lo, hi = (float(x) for x in spans[1 if " windows " in b.F_max_note else 0])
    assert lo == pytest.approx(step, abs=0.01)
    assert _floored_margin(inp, "MP_hold", 0.5 * (lo + hi)) > 0.0
    assert _floored_margin(inp, "MP_hold", hi * 1.001) < 0.0


@pytest.mark.parametrize("mode,row,leg,value,floor", [
    (LaunchMode.CLK2, "DD", "MP_hold", 9.0, 9.0),
    (LaunchMode.CLK4, "DZ", "MP_contention", 3.0, 5.0),
    (LaunchMode.CLK2, "ZD", "MP_hold_ho", 9.0, 9.0),
])
def test_a_clocked_row_below_its_floor_is_placed_at_the_target_as_at_every_rate(
        mode, row, leg, value, floor):
    """A clocked Manager row whose nearest grid point lies below its floor is placed at the
    lowest grid point at or above the floor at the target rate too, as the rate solve places
    it at every other rate: the margin at the target is that placement's, the term carries it
    and says it was raised, and the target lies inside the leg's passing rates."""
    field = {"DD": ("Man_t_DD_min_ns", "Man_t_DD_max_ns"), "ZD": ("Man_t_ZD_ns",),
             "DZ": ("Man_t_DZ_max_ns",)}[row]
    inp = replace(CalcInputs(F_CLK_target_MHz=12.288, launch_mode=mode, handover_UIs=0.0,
                             **{f"Man_t_{row}_floor_ns": floor}),
                  **{f: value for f in field})
    placed = snap_to_launch_grid(value, mode, 12.288, inp.duty_min, floor_ns=floor)
    assert placed > snap_to_launch_grid(value, mode, 12.288, inp.duty_min) + 1e-6
    b = compute(inp).breakdowns[leg]
    term = next(t for t in b.terms if t.symbol == f"Man_t_{row}")
    assert abs(term.value_ns) == pytest.approx(placed, abs=1e-9), (term.value_ns, placed)
    assert "raised to" in term.note, term.note
    on_grid = replace(inp, **{f: placed for f in field})
    assert b.margin_ns == pytest.approx(compute(on_grid).breakdowns[leg].margin_ns, abs=1e-9)
    assert b.margin_ns == pytest.approx(_floored_margin(inp, leg, 12.288), abs=1e-9)
    # The passing rates the breakdown names: up to F_max, and each window its note names.
    named = [(0.0, b.F_max_MHz)] if b.F_max_MHz > 0 else []
    named += [(float(lo), float(hi) if hi else math.inf) for lo, hi in
              re.findall(r"from ([0-9.]+) (?:to ([0-9.]+) MHz|MHz up)", b.F_max_note)]
    assert b.margin_ns > 0
    assert any(lo < inp.F_CLK_target_MHz <= hi for lo, hi in named), (named, b.F_max_note)


def test_the_note_names_up_to_three_later_windows_and_counts_more():
    """`_fmax_from_windows`: three later windows are each named; four are counted and
    spanned, with the first two and the last named, after an F_max of 0.0 too, and an open
    last window reads "MHz up" in the span and in its own name."""
    from swi3s_studio.timing.calculator import _fmax_from_windows

    three = [(0.0, 10.0), (12.0, 14.0), (16.0, 18.0), (20.0, math.inf)]
    f_max, note = _fmax_from_windows(three)
    assert f_max == 10.0
    assert note == ("passes again from 12.00 to 14.00 MHz, and from 16.00 to 18.00 MHz, "
                    "and from 20.00 MHz up"), note
    f_max, note = _fmax_from_windows([*three[:-1], (20.0, 22.0), (24.0, 26.0)])
    assert note == ("passes again in 4 windows from 12.00 to 26.00 MHz: from 12.00 to "
                    "14.00 MHz, from 16.00 to 18.00 MHz, …, from 24.00 to 26.00 MHz"), note
    f_max, note = _fmax_from_windows([*three[:-1], (20.0, 22.0), (24.0, math.inf)])
    assert note == ("passes again in 4 windows from 12.00 MHz up: from 12.00 to 14.00 MHz, "
                    "from 16.00 to 18.00 MHz, …, from 24.00 MHz up"), note
    f_max, note = _fmax_from_windows([(12.0, 14.0), (16.0, 18.0), (20.0, 22.0), (24.0, 26.0)])
    assert f_max == 0.0
    assert note == ("passes in 4 windows from 12.00 to 26.00 MHz: from 12.00 to 14.00 MHz, "
                    "from 16.00 to 18.00 MHz, …, from 24.00 to 26.00 MHz"), note
    assert _fmax_from_windows([(5.0, 7.0)]) == (0.0, "passes from 5.00 to 7.00 MHz")
    assert _fmax_from_windows([(0.0, math.inf)]) == (math.inf, "")


def test_a_piece_with_no_constant_part_passes_by_its_slope_alone():
    """`_piece_windows` on a margin A·UI + B: with B = 0 a piece passes everywhere when A ≥ 0
    and nowhere when A < 0, without dividing by B; a small nonzero A still scales with the
    rate."""
    from swi3s_studio.timing.calculator import _piece_windows

    assert _piece_windows(0.45, 0.0, [(0.0, math.inf, -1.0)]) == []
    assert _piece_windows(0.45, 0.0, [(0.0, math.inf, 0.0)]) == [(0.0, math.inf)]
    assert _piece_windows(0.45, 0.0, [(0.0, math.inf, +1.0)]) == [(0.0, math.inf)]
    # A = 1e-6 per UI against B = -1 ns: passes up to f = 1000·0.45·1e-6 / 1 MHz.
    assert _piece_windows(0.45, -1.0, [(0.0, math.inf, 1e-6)]) == [(0.0, pytest.approx(4.5e-4))]


def test_a_margin_with_no_ui_passes_by_its_sign_alone():
    """`_rate_windows` at a UI of zero: a margin of zero passes at every rate, a negative one
    at none."""
    from swi3s_studio.timing.calculator import _rate_windows

    inp = CalcInputs(duty_min=0.0)
    assert _rate_windows(inp, margin_ns=0.0, ui_part_ns=0.0) == [(0.0, math.inf)]
    assert _rate_windows(inp, margin_ns=-1e-9, ui_part_ns=0.0) == []


@pytest.mark.parametrize("floor,deficit", [(0.03, 0.031), (0.01, 0.0125)])
def test_a_floored_leg_failing_above_a_gigahertz_reports_that_rate(floor, deficit):
    """The floored rate solve searches up to `_FLOORED_FMAX_CAP_MHZ` (10 GHz): a 4x hold leg
    whose clocked Man_t_DD is one grid step at the target and whose floor is below the leg's
    deficit keeps its fraction up to the rate where 0.25 UI covers the deficit, and fails
    there: about 3.6 GHz and 9 GHz, both under the cap."""
    from swi3s_studio.timing.calculator import (
        _FLOORED_FMAX_CAP_MHZ,
        _fmax_from_windows,
        _rate_windows,
    )

    inp = CalcInputs(launch_mode=LaunchMode.CLK4, F_CLK_target_MHz=12.288,
                     Man_t_DD_floor_ns=floor)
    v = 0.25 * inp.UI_ns()
    windows = _rate_windows(inp, margin_ns=v - deficit, ui_part_ns=v,
                            clocked=[("DD", +1.0, v)])
    f_max, _ = _fmax_from_windows(windows)
    assert 1000.0 < f_max < _FLOORED_FMAX_CAP_MHZ == 10_000.0, windows
    assert f_max == pytest.approx(0.25 * 1000.0 * inp.duty_min / deficit, rel=1e-9), f_max


@pytest.mark.parametrize("floor,deficit", [(0.005, 0.006), (0.03, 0.02)])
def test_a_floored_leg_passing_up_to_the_search_cap_reads_inf(floor, deficit):
    """A floored leg that passes at `_FLOORED_FMAX_CAP_MHZ` keeps that sign past it: one whose
    first failure, where 0.25 UI falls short of its deficit, lies above the cap, and one whose
    floor exceeds its deficit and so never fails, both read F_max = inf."""
    from swi3s_studio.timing.calculator import (
        _FLOORED_FMAX_CAP_MHZ,
        _fmax_from_windows,
        _rate_windows,
    )

    inp = CalcInputs(launch_mode=LaunchMode.CLK4, F_CLK_target_MHz=12.288,
                     Man_t_DD_floor_ns=floor)
    v = 0.25 * inp.UI_ns()
    first_failure = 0.25 * 1000.0 * inp.duty_min / deficit
    assert first_failure > _FLOORED_FMAX_CAP_MHZ or floor > deficit
    windows = _rate_windows(inp, margin_ns=v - deficit, ui_part_ns=v,
                            clocked=[("DD", +1.0, v)])
    assert _fmax_from_windows(windows) == (math.inf, ""), windows
